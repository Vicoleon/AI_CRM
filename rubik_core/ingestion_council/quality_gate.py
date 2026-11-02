"""Priorities Quality Gate — phase 2: OBSERVE-ONLY per-user bundle judging.

Runs between generation (06:00 UTC) and Slack delivery (08:30 UTC) and answers,
per user: *is what we're about to send genuinely their most important set for
today?* Design: ``docs/priorities_quality_gate_design.md``.

Phase 2 is **observe-only**: verdicts are persisted + DM'd to ops, and NOTHING
downstream reads them — delivery is completely unaffected. Phase 3 (repair +
verdict-gated delivery) only ships after a week of observed verdicts proves the
judge trustworthy.

Fail-open is load-bearing throughout: a judge LLM failure, a garbage response,
or the whole gate being disabled must never be able to make a user's morning
worse than the status quo. Deterministic checks (computed in Python) decide
missing/empty/degraded; the LLM judge only ever grades bundles that have real
content, and its failure degrades to PASS-with-reason, never FAIL.

Mode knob: ``CUBIE_PRIORITIES_GATE_MODE`` = ``off`` (default) | ``observe``.
Phase 3 will add ``enforce``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from rubik_core.config import settings
from rubik_core.ingestion_council.llm import AUDITOR_MODEL, council_llm
from rubik_core.ingestion_council.models import CouncilVerdict, Finding, Health

logger = logging.getLogger(__name__)

GATE_SCOPE = "priorities-gate"

# Judge failure / garbage → PASS with a reason, never FAIL. In observe mode this
# only affects reporting; in phase-3 enforce mode it's what guarantees a broken
# judge can't block a briefing.
_JUDGE_FALLBACK: dict[str, Any] = {
    "verdict": "pass",
    "reasons": ["judge_unavailable — fail-open"],
}

_MAX_REASONS = 5


def gate_mode() -> str:
    return (
        (getattr(settings, "CUBIE_PRIORITIES_GATE_MODE", "off") or "off")
        .strip()
        .lower()
    )


def _local(email: str) -> str:
    return email.split("@", 1)[0]


@dataclass
class UserBundleRecord:
    """One user's freshly generated bundle, as delivery would look it up."""

    email: str
    date: str  # the user's LOCAL today the lookup keyed on
    has_card: bool
    priorities: list[dict] = field(default_factory=list)
    audit_trail: list[str] = field(default_factory=list)
    # models.GENERATION_* on new cards; "" on legacy cards without the field.
    generation_status: str = ""
    crm_deal_ids: set[str] = field(default_factory=set)


@dataclass
class UserGateResult:
    email: str
    date: str
    n_priorities: int
    verdict: Health
    reasons: list[str] = field(default_factory=list)
    judged: bool = False  # True when the LLM judge graded this bundle

    def to_dict(self) -> dict[str, Any]:
        return {
            "user": _local(self.email),
            "date": self.date,
            "priorities": self.n_priorities,
            "verdict": self.verdict.value,
            "reasons": self.reasons,
            "judged": self.judged,
        }


def gather_user_bundle_records(db: Session) -> list[UserBundleRecord]:
    """Each active user's bundle for THEIR local today — the same
    ``date == user_today_str(email)`` lookup delivery uses, so the gate sees
    exactly what the 08:30 DM will see (minus the UC-25 memory filter, which is
    per-user state, not generation quality)."""
    from rubik_core.database.models import HomeCard
    from rubik_core.priorities import DAILY_BRIEFING_CATEGORY
    from rubik_core.priorities.batch import priorities_roster
    from rubik_core.priorities.cached_visibility import verified_bundle_deals
    from rubik_core.priorities.temporal import user_today_str

    # The batch's own roster (same rationale as
    # evidence._priorities_daily_evidence).
    active = priorities_roster(db)

    records: list[UserBundleRecord] = []
    for email in sorted(active):
        today = user_today_str(email)
        card = (
            db.query(HomeCard)
            .filter(HomeCard.user_email == email)
            .filter(HomeCard.date == today)
            .filter(HomeCard.category == DAILY_BRIEFING_CATEGORY)
            .order_by(HomeCard.created_at.desc())
            .first()
        )
        md = card.metadata_json if card is not None else None
        md = md if isinstance(md, dict) else {}
        verified = verified_bundle_deals(md) if card is not None else None
        if card is not None and verified is None:
            # A stored model answer can repeat revoked CRM facts even when an
            # individual card claims Gmail provenance. Do not send its text to
            # the quality judge or include it in a report.
            records.append(
                UserBundleRecord(
                    email=email,
                    date=today,
                    has_card=True,
                    priorities=[],
                    audit_trail=["[ERROR] saved priorities lack current source proof"],
                    generation_status="unavailable",
                )
            )
            continue
        records.append(
            UserBundleRecord(
                email=email,
                date=today,
                has_card=card is not None,
                priorities=list(md.get("priorities") or []),
                audit_trail=[str(line) for line in (md.get("audit_trail") or [])],
                generation_status=str(md.get("generation_status") or ""),
                crm_deal_ids=verified or set(),
            )
        )
    return records


def _generation_degraded(audit_trail: list[str]) -> bool:
    return any("[ERROR]" in line or "TIMEOUT" in line for line in audit_trail)


def _duplicate_reasons(priorities: list[dict]) -> list[str]:
    seen_entity: dict[str, str] = {}
    seen_title: set[str] = set()
    reasons: list[str] = []
    for p in priorities:
        title = (p.get("title") or "").strip().lower()
        entity = (p.get("entity_id") or "").strip()
        if title and title in seen_title:
            reasons.append(f"duplicate title: {title[:60]!r}")
        seen_title.add(title)
        if entity:
            if entity in seen_entity:
                reasons.append(f"two cards share entity {entity[:40]!r}")
            seen_entity[entity] = title
    return reasons


def deterministic_verdict(
    record: UserBundleRecord,
) -> tuple[Health, list[str], bool]:
    """Stage 1 — pure-Python checks. Returns (health, reasons, judge_eligible).

    Only bundles with real content are judge-eligible; the empty/missing cases
    are already decided here and an LLM opinion adds nothing but cost.
    """
    if not record.has_card:
        return (
            Health.FAIL,
            [f"no bundle for local today ({record.date}) — delivery would skip"],
            False,
        )
    if not record.priorities:
        # New cards say WHY they're empty (models.GENERATION_*). An
        # "empty_with_signals" card should no longer exist (the delivery gates
        # refuse to persist them) — one showing up means a gate was bypassed.
        if record.generation_status == "empty_with_signals":
            return (
                Health.FAIL,
                ["empty_with_signals card persisted — a delivery gate was bypassed"],
                False,
            )
        if _generation_degraded(record.audit_trail):
            return (
                Health.FAIL,
                ["empty bundle from DEGRADED generation (channel errors/timeouts)"],
                False,
            )
        if record.generation_status == "filtered_empty":
            return (
                Health.PASS,
                ["filtered_empty — decision items all already handled/stale"],
                False,
            )
        # A quiet day is a PASS, not a problem — pinning this is what stops the
        # future repair loop from spamming regenerations at people who
        # legitimately have nothing.
        return (Health.PASS, ["no_signals — legitimately quiet day"], False)

    reasons: list[str] = []
    health = Health.PASS
    dupes = _duplicate_reasons(record.priorities)
    if dupes:
        health = Health.WARN
        reasons.extend(dupes[:_MAX_REASONS])
    if _generation_degraded(record.audit_trail):
        health = Health.worst([health, Health.WARN])
        reasons.append("partial generation: some channels errored/timed out")
    return (health, reasons, True)


def _judge_prompt(record: UserBundleRecord) -> str:
    lines = [
        "You are the quality judge for a user's morning AI-Priorities briefing.",
        f"The user's local date is {record.date}.",
        "",
        "The priorities about to be delivered (in order):",
    ]
    for i, p in enumerate(record.priorities, 1):
        lines.append(
            f"{i}. [{(p.get('urgency') or '?').upper()}] {p.get('title', '')}\n"
            f"   why: {p.get('why_it_matters', '')}\n"
            f"   next: {p.get('suggested_action', '')}"
        )
    channel_lines = [line for line in record.audit_trail if line.startswith("channel ")]
    if channel_lines:
        lines.append("")
        lines.append("Evidence collected by the pipeline (channel summaries):")
        lines.extend(f"  - {line}" for line in channel_lines)
    lines += [
        "",
        "Grade the SET, strictly on:",
        "1. Is the #1 card plausibly this user's most important thing TODAY "
        "(imminence, stakes)?",
        "2. Is each 'why'/'next' grounded in the evidence — no embellishment, "
        "no invented facts?",
        "3. Given the evidence summaries, was anything obviously more important "
        "left out?",
        "4. Are any two cards really the same item?",
        "",
        'Reply as JSON: {"verdict": "pass"|"warn"|"fail", "reasons": ["..."]}. '
        "PASS = deliver as-is. WARN = deliver, but note the issue. FAIL = this "
        "set would mislead the user's morning. Be specific in reasons; empty "
        "reasons only on a clean PASS.",
    ]
    return "\n".join(lines)


def judge_bundle(
    record: UserBundleRecord,
    llm: Callable[..., Any] = council_llm,
) -> tuple[Health, list[str]]:
    """Stage 2 — one LLM call. Garbage or failure degrades to PASS-with-reason
    (fail-open), never to FAIL."""
    out = llm(_judge_prompt(record), model_key=AUDITOR_MODEL, fallback=_JUDGE_FALLBACK)
    if not isinstance(out, dict):
        return (Health.PASS, ["judge returned non-JSON — fail-open"])
    verdict = Health.coerce(out.get("verdict"), default=Health.PASS)
    reasons = [str(r).strip() for r in (out.get("reasons") or []) if str(r).strip()]
    return (verdict, reasons[:_MAX_REASONS])


def evaluate_records(
    records: list[UserBundleRecord],
    llm: Callable[..., Any] = council_llm,
) -> list[UserGateResult]:
    results: list[UserGateResult] = []
    for record in records:
        health, reasons, judge_eligible = deterministic_verdict(record)
        judged = False
        if judge_eligible:
            j_health, j_reasons = judge_bundle(record, llm=llm)
            health = Health.worst([health, j_health])
            reasons = (reasons + j_reasons)[: _MAX_REASONS * 2]
            judged = True
        results.append(
            UserGateResult(
                email=record.email,
                date=record.date,
                n_priorities=len(record.priorities),
                verdict=health,
                reasons=reasons,
                judged=judged,
            )
        )
    return results


def build_verdict(results: list[UserGateResult], mode: str) -> CouncilVerdict:
    overall = Health.worst([r.verdict for r in results]) if results else Health.PASS
    fails = [r for r in results if r.verdict is Health.FAIL]
    warns = [r for r in results if r.verdict is Health.WARN]
    headline = (
        f"Priorities quality gate ({mode.upper()} — delivery NOT gated): "
        f"{len(results)} bundles → {len(fails)} FAIL, {len(warns)} WARN, "
        f"{len(results) - len(fails) - len(warns)} PASS."
    )
    top_risks = [
        Finding(
            severity="critical" if r.verdict is Health.FAIL else "warning",
            title=f"{_local(r.email)}: {r.verdict.value.upper()}",
            detail="; ".join(r.reasons) or "(no reasons recorded)",
            run_kind=GATE_SCOPE,
        )
        for r in (fails + warns)[:8]
    ]
    one_action = (
        "Observe-only: compare these verdicts against real user feedback before "
        "enabling enforce mode."
        if not fails
        else "Check the named FAIL users' generation (batch logs / HomeCard "
        "dates) — in enforce mode these would have been repaired before the DM."
    )
    return CouncilVerdict(
        scope=GATE_SCOPE,
        overall_health=overall,
        headline=headline,
        generated_at=datetime.utcnow().isoformat() + "Z",
        top_risks=top_risks,
        one_action=one_action,
        per_kind_health={GATE_SCOPE: overall.value},
        per_user=[r.to_dict() for r in results],
    )


def run_gate(db: Session) -> CouncilVerdict | None:
    """Gather → evaluate → verdict. Returns ``None`` when the mode knob is off
    (the caller reports 'disabled' and nothing is persisted or DM'd)."""
    mode = gate_mode()
    if mode == "off":
        return None
    records = gather_user_bundle_records(db)
    results = evaluate_records(records)
    verdict = build_verdict(results, mode)
    from rubik_core.tenancy.context import current_workspace

    verdict.crm_provenance = {
        "workspace": current_workspace(),
        "deal_ids": sorted(set().union(*(r.crm_deal_ids for r in records))),
        "unprovable": False,
    }
    logger.info(
        "[priorities-gate] mode=%s users=%d judged=%d overall=%s",
        mode,
        len(results),
        sum(1 for r in results if r.judged),
        verdict.overall_health.value,
    )
    return verdict

"""The Ingestion Council orchestrator.

Stages (mirrors the source skill, retargeted at ingestion QA):

1. **Evidence** — gather deterministic facts for each run in scope.
2. **Convene** (parallel) — five adversarial auditors assess the same evidence.
3. **Peer review** (parallel) — each auditor critiques the panel *anonymised*
   A–E, so they judge the argument, not the persona.
4. **Chairman** — one reasoning-tier call synthesises a verdict: agreements,
   clashes, blind spots, top risks, one action.
5. **Floor** — a deterministic guard so the verdict can never be greener than
   the worst auditor or a failed check. The LLM can escalate, never whitewash.

Every LLM call is wrapped so a hang or error degrades to a placeholder instead
of taking the run down — the same resilience the priorities orchestrator has.
"""

from __future__ import annotations

import logging
import random
import string
from datetime import datetime

from sqlalchemy.orm import Session

from rubik_core.brand import PRODUCT_NAME
from rubik_core.cost.usage import attribute_to
from rubik_core.ingestion_council.auditors import AUDITORS, Auditor, run_auditor
from rubik_core.ingestion_council.evidence import gather_evidence
from rubik_core.ingestion_council.llm import CHAIRMAN_MODEL, council_llm
from rubik_core.ingestion_council.models import (
    AuditorFinding,
    CouncilVerdict,
    Finding,
    Health,
    PeerReview,
    RunEvidence,
    kinds_for_scope,
)
from rubik_core.tenancy.threads import ContextThreadPoolExecutor

logger = logging.getLogger(__name__)

# Per-stage backstops. Kept tight so the synchronous endpoint's worst-case
# wall-clock (evidence + auditors + peer review + chairman, run sequentially)
# stays well under Cloud Run's 300s request timeout: ~5 + 60 + 60 + 90 ≈ 215s.
AUDITOR_TIMEOUT_S = 60
CHAIRMAN_TIMEOUT_S = 90


def _placeholder(auditor: Auditor, reason: str) -> AuditorFinding:
    return AuditorFinding(
        auditor_id=auditor.id,
        lens=auditor.lens,
        health=Health.WARN,
        summary=f"No assessment: {reason}.",
        findings=[],
        confidence=0.0,
    )


def _run_pool(tasks: list[tuple[Auditor, callable]], timeout_s: int) -> list:
    """Run ``fn()`` for each (auditor, fn) concurrently; per-task timeout falls
    back to a WARN placeholder so one hung call can't stall or crash the run."""
    results: list = []
    with ContextThreadPoolExecutor(max_workers=max(1, len(tasks))) as ex:
        futures = [(aud, ex.submit(fn)) for aud, fn in tasks]
        for aud, fut in futures:
            try:
                results.append(fut.result(timeout=timeout_s))
            except Exception as exc:  # noqa: BLE001 — timeout or unexpected error
                logger.warning("[ingestion-council] %s failed: %s", aud.id, exc)
                results.append(_placeholder(aud, "timed out or errored"))
    return results


# ── Stage 3: anonymised peer review ───────────────────────────────────────────


def _anonymise(findings: list[AuditorFinding]) -> tuple[str, dict[str, str]]:
    """Render the panel with attribution stripped (labels A, B, C…) and return
    the block plus a label→lens map for de-anonymising afterwards."""
    order = list(range(len(findings)))
    random.shuffle(order)
    lines: list[str] = []
    label_to_lens: dict[str, str] = {}
    for i, pos in enumerate(order):
        label = string.ascii_uppercase[i]
        f = findings[pos]
        label_to_lens[label] = f.lens
        bullets = (
            "; ".join(f"[{x.severity}] {x.title}" for x in f.findings)
            or "(no findings)"
        )
        lines.append(
            f"[{label}] verdict={f.health.value} — {f.summary}\n     issues: {bullets}"
        )
    return "\n".join(lines), label_to_lens


def _peer_review_prompt(reviewer: Auditor, anon_block: str) -> str:
    return f"""You are {reviewer.name}. Below are five ingestion-QA analyses of \
the same runs, labelled A–E with authorship hidden. Judge the arguments, not the \
labels.

{anon_block}

Return ONLY JSON:
{{
  "strongest": "the single letter (A-E) whose analysis is most convincing",
  "biggest_blind_spot": "the most important thing ONE analysis missed",
  "what_all_missed": "something NONE of them caught, seen through your lens ({reviewer.lens}); '' if nothing"
}}"""


def _run_peer_reviews(
    anon_block: str, label_to_lens: dict[str, str]
) -> list[PeerReview]:
    def _mk(reviewer: Auditor):
        def _call():
            raw = council_llm(
                _peer_review_prompt(reviewer, anon_block),
                temperature=0.3,
                fallback={},
            )
            data = raw if isinstance(raw, dict) else {}
            strongest_label = str(data.get("strongest") or "").strip().upper()[:1]
            return PeerReview(
                reviewer_id=reviewer.id,
                # De-anonymise the "strongest" pick back to a lens for the report.
                strongest=label_to_lens.get(strongest_label, strongest_label),
                biggest_blind_spot=str(data.get("biggest_blind_spot") or "").strip(),
                what_all_missed=str(data.get("what_all_missed") or "").strip(),
            )

        return _call

    tasks = [(r, _mk(r)) for r in AUDITORS]
    return _run_pool(tasks, AUDITOR_TIMEOUT_S)


# ── Stage 4: chairman synthesis ────────────────────────────────────────────────


def _chairman_prompt(
    scope: str,
    evidence: list[RunEvidence],
    findings: list[AuditorFinding],
    reviews: list[PeerReview],
) -> str:
    panel = "\n".join(
        f"- [{f.lens}] verdict={f.health.value} (conf {f.confidence:.1f}): {f.summary}"
        + (
            "\n    "
            + "\n    ".join(
                f"• [{x.severity}] {x.title} — {x.detail}" for x in f.findings
            )
            if f.findings
            else ""
        )
        for f in findings
    )
    review_txt = "\n".join(
        f"- {r.reviewer_id}: strongest={r.strongest or '?'}; "
        f"blind spot={r.biggest_blind_spot or '—'}; all-missed={r.what_all_missed or '—'}"
        for r in reviews
    )
    failed = [
        f"{e.kind.label}: {c.name} — {c.detail}"
        for e in evidence
        for c in e.failed_checks()
    ]
    failed_txt = "\n".join(f"- {x}" for x in failed) or "- (none failed)"
    kinds = ", ".join(e.kind.value for e in evidence)

    return f"""You are the Chairman of {PRODUCT_NAME}'s Ingestion Council. Five adversarial \
auditors reviewed the {scope} ingestion runs ({kinds}); their analyses and an \
anonymous peer review follow. Synthesise a single operator-facing verdict.

FAILED DETERMINISTIC CHECKS (ground truth — your verdict must reflect these):
{failed_txt}

AUDITOR PANEL:
{panel}

PEER REVIEW:
{review_txt}

Weigh the evidence and commit. Do not hedge. If the auditors conflict, decide \
who is right and say why. Return ONLY JSON:
{{
  "overall_health": "pass" | "warn" | "fail",
  "headline": "one sentence an operator can read in 3 seconds",
  "top_risks": [
    {{"severity": "critical"|"warning"|"info", "title": "short", "detail": "specific + which run", "run_kind": "..."}}
  ],
  "one_action": "the single most valuable next step, concrete",
  "agreements": ["points the panel converged on"],
  "clashes": ["genuine disagreements and how you resolved them"],
  "blind_spots": ["things surfaced only via peer review"],
  "per_kind_health": {{"overnight_sales": "pass|warn|fail", "priorities_daily": "...", "transcript_sync": "...", "eos_weekly": "..."}}
}}
Rubric: "fail" = a critical problem needs action now (any unqueryable data or \
access-control leak is fail); "warn" = real issues, not blocking; "pass" = clean. \
Only include per_kind_health keys for runs actually in this scope."""


def _synthesise(
    scope: str,
    evidence: list[RunEvidence],
    findings: list[AuditorFinding],
    reviews: list[PeerReview],
    generated_at: str,
) -> CouncilVerdict:
    raw = council_llm(
        _chairman_prompt(scope, evidence, findings, reviews),
        model_key=CHAIRMAN_MODEL,
        temperature=0.2,
        fallback={},
    )
    data = raw if isinstance(raw, dict) else {}

    top_risks = [
        Finding.from_dict(f)
        for f in (data.get("top_risks") or [])
        if isinstance(f, dict) and str(f.get("title") or "").strip()
    ]
    per_kind = {
        str(k): Health.coerce(v).value
        for k, v in (data.get("per_kind_health") or {}).items()
        if isinstance(k, str)
    }

    chairman_health = Health.coerce(data.get("overall_health"), Health.WARN)
    worst_auditor = Health.worst(f.health for f in findings)
    # Deterministic floor: a failed check or a FAIL auditor can never be papered
    # over as PASS. The chairman may escalate above this, never below it.
    det_floor = Health.WARN if any(e.failed_checks() for e in evidence) else Health.PASS
    overall = Health.worst([chairman_health, worst_auditor, det_floor])

    headline = str(data.get("headline") or "").strip() or (
        f"{scope.title()} ingestion review: {overall.value.upper()}"
    )

    return CouncilVerdict(
        scope=scope,
        overall_health=overall,
        headline=headline,
        generated_at=generated_at,
        top_risks=top_risks,
        one_action=str(data.get("one_action") or "").strip(),
        agreements=[
            str(a).strip() for a in (data.get("agreements") or []) if str(a).strip()
        ],
        clashes=[str(a).strip() for a in (data.get("clashes") or []) if str(a).strip()],
        blind_spots=[
            str(a).strip() for a in (data.get("blind_spots") or []) if str(a).strip()
        ],
        per_kind_health=per_kind,
        auditor_findings=findings,
        peer_reviews=reviews,
        evidence=evidence,
    )


# ── Public entrypoint ──────────────────────────────────────────────────────────


@attribute_to("ingestion_council")
def run_council(
    db: Session, scope: str = "nightly", now: datetime | None = None
) -> CouncilVerdict:
    """Run the full council over the given scope and return the chairman verdict.

    Pure of I/O beyond the DB reads + LLM calls — no persistence, no delivery
    (those live in the backend layer). ``now`` is injectable for deterministic
    tests.
    """
    now = now or datetime.utcnow()
    generated_at = now.isoformat()
    kinds = kinds_for_scope(scope)
    logger.info(
        "[ingestion-council] run scope=%s kinds=%s", scope, [k.value for k in kinds]
    )

    evidence = gather_evidence(db, kinds, now)
    evidence_block = "\n\n".join(e.to_prompt_block() for e in evidence)

    # Stage 2 — convene (parallel).
    findings = _run_pool(
        [(a, (lambda a=a: run_auditor(a, evidence_block, scope))) for a in AUDITORS],
        AUDITOR_TIMEOUT_S,
    )

    # Stage 3 — anonymised peer review (parallel).
    anon_block, label_to_lens = _anonymise(findings)
    reviews = _run_peer_reviews(anon_block, label_to_lens)

    # Stage 4 + 5 — chairman synthesis with a deterministic health floor.
    verdict = _synthesise(scope, evidence, findings, reviews, generated_at)
    from rubik_core.tenancy.context import current_workspace

    # Classic evidence uses counts and source metadata; unscoped historical
    # recommendation prose is excluded by the evidence gatherer.
    verdict.crm_provenance = {
        "workspace": current_workspace(),
        "deal_ids": [],
        "unprovable": False,
    }
    logger.info(
        "[ingestion-council] verdict scope=%s health=%s risks=%d",
        scope,
        verdict.overall_health.value,
        len(verdict.top_risks),
    )
    return verdict

"""Data model for the Ingestion Council.

The council is our adaptation of the "LLM council" pattern
(https://github.com/tenfoldmarc/llm-council-skill) to ingestion QA. Where that
skill fights a single model's *agreeableness* on strategic questions, our
overnight/weekly ingestions have the objective analogue: they **succeed and
drift silently** — a transcript lands with no ACL row, VTO leaks into the shared
corpus, a freshness window is missed, chunks come back empty. A single
"did the job finish?" check rubber-stamps all of that.

So instead of five opinionated advisors we convene five *adversarial auditors*,
each hunting a different failure class over the same evidence, who peer-review
each other and hand a chairman a health verdict. This module is the pure data
model shared by every stage — no DB, no LLM, no I/O — so the evidence, auditor,
council and report layers all speak the same shapes and everything is trivially
serialisable for persistence.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Health(str, Enum):
    """Traffic-light verdict, per-auditor / per-run / overall.

    ``str`` subclass so it JSON-serialises to its value and compares to plain
    strings the LLM emits without extra coercion.
    """

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"

    @property
    def rank(self) -> int:
        return {"pass": 0, "warn": 1, "fail": 2}[self.value]

    @property
    def emoji(self) -> str:
        return {"pass": "🟢", "warn": "🟡", "fail": "🔴"}[self.value]

    @classmethod
    def coerce(cls, value: Any, default: Health = None) -> Health:
        """Best-effort parse of whatever the LLM handed back."""
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError:
            return default if default is not None else cls.WARN

    @classmethod
    def worst(cls, healths: Iterable[Any]) -> Health:
        """The most severe health in the set — how we roll per-auditor verdicts
        up into an overall one (any FAIL ⇒ FAIL, any WARN ⇒ WARN)."""
        out = cls.PASS
        for h in healths:
            h = cls.coerce(h)
            if h.rank > out.rank:
                out = h
        return out


class RunKind(str, Enum):
    """The ingestion runs the council reviews.

    ``overnight_sales``/``priorities_daily``/``transcript_sync`` fire nightly;
    ``eos_weekly`` fires Mondays. Grouped by :data:`SCOPE_KINDS` below.
    """

    OVERNIGHT_SALES = "overnight_sales"
    PRIORITIES_DAILY = "priorities_daily"
    TRANSCRIPT_SYNC = "transcript_sync"
    EOS_WEEKLY = "eos_weekly"

    @property
    def label(self) -> str:
        return {
            "overnight_sales": "Overnight Sales Agent",
            "priorities_daily": "AI Priorities (daily)",
            "transcript_sync": "Meeting-Transcript Sync",
            "eos_weekly": "EOS Weekly Ingestion",
        }[self.value]

    @property
    def cadence(self) -> str:
        return "weekly" if self is RunKind.EOS_WEEKLY else "nightly"


# Scope → the run kinds it covers. "nightly" reviews the AI-Priorities daily
# batch + the meeting-transcript sync; "weekly" is the Monday EOS sweep. The
# Overnight Sales Agent (OVERNIGHT_SALES) is intentionally NOT reviewed: it's a
# deprecated recommendation-generator superseded by AI Priorities, with no
# user-facing surface — the manager digest (its last consumer) now reads the
# AI-Priorities bundle instead. The enum value is kept for historical verdicts.
SCOPE_KINDS: dict[str, list[RunKind]] = {
    "nightly": [
        RunKind.PRIORITIES_DAILY,
        RunKind.TRANSCRIPT_SYNC,
    ],
    "weekly": [RunKind.EOS_WEEKLY],
    "all": [RunKind.PRIORITIES_DAILY, RunKind.TRANSCRIPT_SYNC, RunKind.EOS_WEEKLY],
}


def kinds_for_scope(scope: str) -> list[RunKind]:
    return SCOPE_KINDS.get(scope, SCOPE_KINDS["nightly"])


@dataclass
class CrossCheck:
    """A deterministic invariant computed in Python — never by the LLM.

    These are the load-bearing facts (ACL row present? department NULL? chunk
    count > 0?). The auditors *reason about* them but can't invent or overrule
    them, which keeps the verdict anchored to reality rather than model vibes.
    """

    name: str
    ok: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class RunEvidence:
    """Everything the council knows about one ingestion run kind.

    ``stats`` are aggregate counts/coverage; ``samples`` are a handful of
    redacted content peeks (titles, chunk counts — never full PII dumps);
    ``cross_checks`` are the deterministic invariants; ``notes`` capture any
    problem the gatherer itself hit (e.g. a source table missing).
    """

    kind: RunKind
    window_hours: int
    stats: dict[str, Any] = field(default_factory=dict)
    samples: list[dict[str, Any]] = field(default_factory=list)
    cross_checks: list[CrossCheck] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def failed_checks(self) -> list[CrossCheck]:
        return [c for c in self.cross_checks if not c.ok]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "label": self.kind.label,
            "cadence": self.kind.cadence,
            "window_hours": self.window_hours,
            "stats": self.stats,
            "samples": self.samples,
            "cross_checks": [c.to_dict() for c in self.cross_checks],
            "notes": self.notes,
        }

    def to_prompt_block(self) -> str:
        """Compact, LLM-legible rendering of this run's evidence."""
        lines = [
            f"### {self.kind.label}  (cadence: {self.kind.cadence}, "
            f"lookback: {self.window_hours}h)",
        ]
        if self.stats:
            lines.append("Stats:")
            for k, v in self.stats.items():
                lines.append(f"  - {k}: {v}")
        if self.cross_checks:
            lines.append("Deterministic checks (computed in Python, trust these):")
            for c in self.cross_checks:
                mark = "OK" if c.ok else "FAIL"
                extra = f" — {c.detail}" if c.detail else ""
                lines.append(f"  - [{mark}] {c.name}{extra}")
        if self.samples:
            lines.append(f"Content samples ({len(self.samples)}):")
            for s in self.samples:
                lines.append(f"  - {s}")
        if self.notes:
            lines.append("Gatherer notes:")
            for n in self.notes:
                lines.append(f"  - {n}")
        return "\n".join(lines)


@dataclass
class Finding:
    """One concrete problem an auditor (or the chairman) surfaced."""

    severity: str  # "critical" | "warning" | "info"
    title: str
    detail: str = ""
    run_kind: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "run_kind": self.run_kind,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Finding:
        return cls(
            severity=str(d.get("severity", "info")).strip().lower() or "info",
            title=str(d.get("title", "")).strip(),
            detail=str(d.get("detail", "")).strip(),
            run_kind=(str(d["run_kind"]).strip() if d.get("run_kind") else None),
        )


@dataclass
class AuditorFinding:
    """One auditor's shaped verdict over the shared evidence."""

    auditor_id: str
    lens: str
    health: Health
    summary: str
    findings: list[Finding] = field(default_factory=list)
    confidence: float = 0.5

    def to_dict(self) -> dict[str, Any]:
        return {
            "auditor_id": self.auditor_id,
            "lens": self.lens,
            "health": self.health.value,
            "summary": self.summary,
            "findings": [f.to_dict() for f in self.findings],
            "confidence": self.confidence,
        }


@dataclass
class PeerReview:
    """One auditor's anonymised critique of the whole panel.

    Reviewers see the findings labelled A–E (attribution stripped) so they judge
    the *argument*, not the persona — the same de-biasing move the source skill
    makes in its peer-review stage.
    """

    reviewer_id: str
    strongest: str = ""
    biggest_blind_spot: str = ""
    what_all_missed: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "reviewer_id": self.reviewer_id,
            "strongest": self.strongest,
            "biggest_blind_spot": self.biggest_blind_spot,
            "what_all_missed": self.what_all_missed,
        }


@dataclass
class CouncilVerdict:
    """The chairman's synthesis — what gets persisted, DM'd and rendered."""

    scope: str
    overall_health: Health
    headline: str
    generated_at: str
    top_risks: list[Finding] = field(default_factory=list)
    one_action: str = ""
    agreements: list[str] = field(default_factory=list)
    clashes: list[str] = field(default_factory=list)
    blind_spots: list[str] = field(default_factory=list)
    per_kind_health: dict[str, str] = field(default_factory=dict)
    auditor_findings: list[AuditorFinding] = field(default_factory=list)
    peer_reviews: list[PeerReview] = field(default_factory=list)
    evidence: list[RunEvidence] = field(default_factory=list)
    # Per-user gate results (priorities quality gate only; [] for classic
    # ingestion-QA scopes). Plain dicts so verdict_json stays self-describing.
    per_user: list[dict[str, Any]] = field(default_factory=list)
    # Server-stamped source scope for persisted prose. Missing on legacy rows.
    crm_provenance: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "overall_health": self.overall_health.value,
            "headline": self.headline,
            "generated_at": self.generated_at,
            "top_risks": [f.to_dict() for f in self.top_risks],
            "one_action": self.one_action,
            "agreements": self.agreements,
            "clashes": self.clashes,
            "blind_spots": self.blind_spots,
            "per_kind_health": self.per_kind_health,
            "auditor_findings": [a.to_dict() for a in self.auditor_findings],
            "peer_reviews": [p.to_dict() for p in self.peer_reviews],
            "evidence": [e.to_dict() for e in self.evidence],
            "per_user": self.per_user,
            "crm_provenance": self.crm_provenance,
        }

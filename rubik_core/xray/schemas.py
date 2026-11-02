"""Typed artifacts that cross module boundaries in UC-45.

Raw HubSpot / source payloads stay as plain dicts (their shape is HubSpot's to
define), but everything the pipeline *computes* and hands to another module has
a fixed contract here so the router, persistence, and the frontend agree.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class ChurnFactor:
    """One explainable churn factor. ``points`` is 0–100 risk for THIS factor."""

    name: str
    value: str  # human-readable observed value, e.g. "62 days"
    points: float  # 0–100 risk contribution of this factor
    weight: float  # relative weight (from config.CHURN_WEIGHTS)
    evidence: str  # what the number came from (for the UI + the analyst prompt)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "points": round(self.points, 1),
            "weight": round(self.weight, 3),
            "evidence": self.evidence,
        }


@dataclass
class ChurnResult:
    """Deterministic churn score. The LLM may write ``rationale_md`` from the
    factor table but can never change ``score`` or ``band``."""

    score: int  # 0–100 risk
    band: str  # Healthy | Watch | At-risk
    factors: list[ChurnFactor] = field(default_factory=list)
    rationale_md: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "band": self.band,
            "factors": [f.as_dict() for f in self.factors],
            "rationale_md": self.rationale_md,
        }


@dataclass
class TimelineDraft:
    """A candidate timeline event before it is deduped/persisted.

    ``event_key`` MUST be stable across refreshes for the same real-world
    moment — it is the ``(company_id, event_key)`` dedupe key.
    """

    event_key: str
    source: str  # hubspot_deal | hubspot_activity | gmail | meeting | doc | news | status_change
    title: str
    ts: datetime | None = None
    kind: str = ""
    detail: str = ""
    evidence: str = ""

    @staticmethod
    def news_key(url: str) -> str:
        """news:sha1(url) — the spec's dedupe key for a news item."""
        return "news:" + hashlib.sha1(url.encode("utf-8")).hexdigest()


@dataclass
class Opportunity:
    """A product-catalog whitespace opportunity (spec §3 ``opportunities``)."""

    title: str
    why_now: str
    product: str
    play: str
    #: The catalog category the product sits in, shown as a label.
    entity: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "why_now": self.why_now,
            "product": self.product,
            "play": self.play,
            "entity": self.entity,
        }


@dataclass
class CompanyBundle:
    """Everything fetched about a customer for one run.

    Fetchers fill this fail-soft: a source that errors appends to ``errors``
    and leaves its slice empty rather than aborting the whole run. ``cursors``
    and ``covered_through`` are the checkpoint watermarks persisted on the
    snapshot so a later refresh is delta-only.
    """

    company_id: str
    company: dict[str, Any] = field(default_factory=dict)  # HubSpot company props
    deals: list[dict[str, Any]] = field(default_factory=list)
    # engagement object type -> list of engagement dicts (calls/emails/notes/...)
    engagements: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    contacts: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    cursors: dict[str, Any] = field(default_factory=dict)
    covered_through: datetime | None = None

    # Convenience projections filled by the fetcher.
    company_name: str = ""
    company_domain: str = ""
    csm: str = ""
    client_status: str = ""

    def all_engagements(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for items in self.engagements.values():
            out.extend(items)
        return out


@dataclass
class ChannelDigest:
    """A Deep Research channel agent's structured output.

    Mirrors the priorities ChannelDigest role: ``summary`` is one factual
    sentence, ``highlights`` are extracted points, ``timeline`` are drafts to
    merge, ``evidence`` are citation strings, ``attrs`` is free-form enrichment
    the analyst reads, and ``error`` is the fail-soft marker the orchestrator
    keys off (a timed-out/crashed agent yields a digest with ``error`` set).
    """

    channel: str
    summary: str = ""
    highlights: list[str] = field(default_factory=list)
    timeline: list[TimelineDraft] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    attrs: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def as_progress(self) -> str:
        return "error" if self.error else "done"

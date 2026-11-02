"""Shared Signal contract for the UC-17 multi-source priority pipeline.

A `Signal` is one piece of evidence about a user's day, from one source. The
correlator (UC-17.7) groups Signals that point at the same underlying entity
(deal/contact/company) and the judge (UC-17.6) decides which groups become
homepage priorities. Everything downstream of source modules consumes Signals
through this dataclass — no source-specific row shapes leak past here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

KNOWN_SOURCES: frozenset[str] = frozenset(
    {"hubspot", "gmail", "slack", "calendar", "transcript", "drive"}
)

SeverityHint = str  # "low" | "medium" | "high" — see _ALLOWED_SEVERITIES below.
_ALLOWED_SEVERITIES: frozenset[str] = frozenset({"low", "medium", "high"})


@dataclass
class Signal:
    """One piece of evidence emitted by a single source.

    `entity_refs` is sparse — sources only set keys they actually know
    (e.g. Gmail knows `thread_id` and maybe `contact_email`; HubSpot knows
    `deal_id`, `contact_email`, `company_name`). The correlator walks these
    keys to join across sources.

    `severity_hint` is a weak prior. The judge can and will override it; it
    exists so the correlator can do cheap pre-ranking before the LLM call.
    """

    user_email: str
    source: str
    signal_type: str
    summary: str
    occurred_at: datetime
    entity_refs: dict[str, Any] = field(default_factory=dict)
    severity_hint: SeverityHint = "medium"
    payload: dict[str, Any] = field(default_factory=dict)
    signal_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def __post_init__(self) -> None:
        if self.source not in KNOWN_SOURCES:
            raise ValueError(
                f"Signal.source must be one of {sorted(KNOWN_SOURCES)}, "
                f"got {self.source!r}"
            )
        if self.severity_hint not in _ALLOWED_SEVERITIES:
            raise ValueError(
                f"Signal.severity_hint must be one of {sorted(_ALLOWED_SEVERITIES)}, "
                f"got {self.severity_hint!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Stable serialization for the future `agent_recommendations.sources`
        JSON column. `datetime` becomes ISO-8601 so Postgres' JSON encoder
        accepts it without a custom serializer."""
        return {
            "signal_id": self.signal_id,
            "source": self.source,
            "signal_type": self.signal_type,
            "user_email": self.user_email,
            "entity_refs": dict(self.entity_refs),
            "occurred_at": self.occurred_at.isoformat(),
            "summary": self.summary,
            "severity_hint": self.severity_hint,
            "payload": dict(self.payload),
        }


@runtime_checkable
class SignalSource(Protocol):
    """Each source module (Gmail, Slack, …) implements this.

    The orchestrator instantiates one per source and calls `.fetch()` per
    user per overnight tick. `since` lets sources do incremental pulls; the
    orchestrator passes the last successful run timestamp.
    """

    name: str

    def fetch(self, user_email: str, *, since: datetime) -> list[Signal]: ...

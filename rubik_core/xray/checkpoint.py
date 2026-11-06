"""Checkpoint & refresh semantics.

The rule: once a customer has been generated, save that checkpoint and build
on it. A refresh never restarts from zero — it loads the latest ready
snapshot, pulls only data newer than its coverage watermark (per-source
cursors), extends the append-only timeline, recomputes churn on the full
current state, and writes a NEW version chained via ``base_snapshot_id``.

This module is pure logic over data the service layer already loaded with a
short-lived session; it opens no long-lived connection itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from rubik_core.database.models import XraySnapshot
from rubik_core.xray import persistence

# Sections only a Deep run can produce (web research + citations). On a FAST
# refresh over a Deep checkpoint we carry these forward untouched, stamped with
# their original date, rather than dropping them (spec §5).
DEEP_CARRY_FORWARD = ("recent_news", "source_evidence")


@dataclass
class Checkpoint:
    """The state a refresh builds on. All fields safe defaults for a first run."""

    prev: XraySnapshot | None = None
    cursors: dict[str, Any] = field(default_factory=dict)
    prev_sections: dict[str, Any] = field(default_factory=dict)
    prev_signals: dict[str, Any] = field(default_factory=dict)
    prev_kind: str = ""
    prev_created_at: datetime | None = None
    version: int = 1
    base_snapshot_id: str | None = None

    @property
    def is_first_run(self) -> bool:
        return self.prev is None


def load(db: Session, company_id: str) -> Checkpoint:
    """Build the checkpoint for the next run from the latest READY snapshot."""
    prev = persistence.latest_ready(db, company_id)
    version = persistence.next_version(db, company_id)
    if prev is None:
        return Checkpoint(version=version)
    return Checkpoint(
        prev=prev,
        cursors=dict(prev.source_cursors or {}),
        prev_sections=dict(prev.sections or {}),
        prev_signals=dict(prev.signals or {}),
        prev_kind=prev.kind or "",
        prev_created_at=prev.created_at,
        version=version,
        base_snapshot_id=prev.id,
    )


def carry_forward_sections(
    prev_sections: dict[str, Any],
    new_sections: dict[str, Any],
    *,
    new_kind: str,
    prev_created_at: datetime | None,
) -> dict[str, Any]:
    """Merge carried-forward Deep-only sections into a Fast refresh's output.

    Deep runs regenerate everything, so no carry-forward. A Fast run over a
    Deep base keeps ``recent_news`` / ``source_evidence`` and records an
    ``_as_of`` stamp per carried key so the UI can label them ("News as of
    Jul 2").
    """
    merged = dict(new_sections or {})
    if new_kind != "fast" or not prev_sections:
        return merged
    as_of = dict(merged.get("_as_of") or {})
    stamp = prev_created_at.date().isoformat() if prev_created_at else ""
    for key in DEEP_CARRY_FORWARD:
        carried = prev_sections.get(key)
        if carried and not merged.get(key):
            merged[key] = carried
            if stamp:
                as_of[key] = stamp
    if as_of:
        merged["_as_of"] = as_of
    return merged

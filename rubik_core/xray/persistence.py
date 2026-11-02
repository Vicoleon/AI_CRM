"""Snapshot + timeline persistence.

All writes are SHORT transactions the service layer opens AFTER the LLM/fetch
work is done and closes immediately (docs/ENGINEERING_NOTES.md: hold no DB connection during
model/agent work). The timeline is append-only and deduped on ``(company_id,
event_key)`` so a refresh inserts only unseen moments.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from rubik_core.database.models import XraySnapshot, XrayTimelineEvent
from rubik_core.xray.schemas import TimelineDraft

logger = logging.getLogger(__name__)


# ── Snapshot writes ───────────────────────────────────────────────────────────
def next_version(db: Session, company_id: str) -> int:
    """The next per-company version number (max existing + 1, or 1)."""
    latest = (
        db.query(XraySnapshot)
        .filter(XraySnapshot.company_id == str(company_id))
        .order_by(XraySnapshot.version.desc())
        .first()
    )
    return (latest.version + 1) if latest else 1


def create_snapshot(db: Session, **fields: Any) -> XraySnapshot:
    """Insert a snapshot row (short txn) and return it."""
    now = datetime.utcnow()
    snap = XraySnapshot(
        id=uuid.uuid4().hex,
        created_at=now,
        updated_at=now,
        **fields,
    )
    db.add(snap)
    db.commit()
    db.refresh(snap)
    return snap


def update_snapshot(
    db: Session, snapshot_id: str, **fields: Any
) -> XraySnapshot | None:
    """Patch a snapshot (short txn) — advance a deep run's status/progress."""
    snap = db.get(XraySnapshot, snapshot_id)
    if snap is None:
        return None
    for k, v in fields.items():
        setattr(snap, k, v)
    snap.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(snap)
    return snap


# ── Timeline writes ───────────────────────────────────────────────────────────
def append_timeline(
    db: Session,
    company_id: str,
    drafts: list[TimelineDraft],
    *,
    snapshot_id: str | None = None,
) -> int:
    """Insert only unseen ``event_key``s for this company. Returns #inserted.

    Dedupe is done in-Python against the existing keys (the ``(company_id,
    event_key)`` unique constraint is the backstop). Drafts are batched into
    one commit.
    """
    if not drafts:
        return 0
    existing = {
        row[0]
        for row in db.execute(
            select(XrayTimelineEvent.event_key).where(
                XrayTimelineEvent.company_id == str(company_id)
            )
        ).all()
    }
    now = datetime.utcnow()
    seen_this_batch: set[str] = set()
    fresh: list[XrayTimelineEvent] = []
    for d in drafts:
        if not d.event_key or d.event_key in existing or d.event_key in seen_this_batch:
            continue
        seen_this_batch.add(d.event_key)
        fresh.append(
            XrayTimelineEvent(
                id=uuid.uuid4().hex,
                company_id=str(company_id),
                event_key=d.event_key,
                ts=d.ts,
                source=d.source,
                kind=d.kind or None,
                title=d.title[:500] if d.title else "(untitled)",
                detail=(d.detail or None),
                evidence=(d.evidence or None),
                first_seen_snapshot_id=snapshot_id,
                created_at=now,
            )
        )
    if not fresh:
        return 0
    for ev in fresh:
        db.add(ev)
    try:
        db.commit()
        return len(fresh)
    except IntegrityError:
        # A concurrent same-company refresh committed one of these keys between
        # our read and our commit (snapshots are a shared asset). Roll back the
        # whole batch and re-insert per row, skipping the rows that now conflict,
        # so a race never 500s the caller or drops the non-conflicting events.
        db.rollback()
        inserted = 0
        for ev in fresh:
            try:
                db.add(ev)
                db.commit()
                inserted += 1
            except IntegrityError:
                db.rollback()
        logger.info(
            "timeline append raced for %s — inserted %d/%d after retry",
            company_id,
            inserted,
            len(fresh),
        )
        return inserted


# ── Reads ─────────────────────────────────────────────────────────────────────
def latest_ready(db: Session, company_id: str) -> XraySnapshot | None:
    # "Latest" = most recently COMPLETED ready snapshot. Ordering by updated_at
    # (the moment the row flipped to ready), not version or created_at, means a
    # Deep run that finishes after a Fast refresh started during it is still the
    # latest — even though its version + created_at were stamped earlier.
    return (
        db.query(XraySnapshot)
        .filter(
            XraySnapshot.company_id == str(company_id),
            XraySnapshot.status == "ready",
        )
        .order_by(XraySnapshot.updated_at.desc(), XraySnapshot.version.desc())
        .first()
    )


def latest_any(db: Session, company_id: str) -> XraySnapshot | None:
    return (
        db.query(XraySnapshot)
        .filter(XraySnapshot.company_id == str(company_id))
        .order_by(XraySnapshot.version.desc())
        .first()
    )


def get_snapshot(db: Session, snapshot_id: str) -> XraySnapshot | None:
    return db.get(XraySnapshot, snapshot_id)


def active_deep_run(
    db: Session, company_id: str, *, max_age_minutes: int | None = None
) -> XraySnapshot | None:
    """A pending/running deep snapshot for this company (409 guard).

    ``max_age_minutes`` bounds how long a pending/running row is considered
    "active": a crashed or CPU-throttled run that never flipped to failed would
    otherwise 409-block every future deep run forever. Rows older than the TTL
    are treated as dead and no longer block (self-healing reaper).
    """
    q = db.query(XraySnapshot).filter(
        XraySnapshot.company_id == str(company_id),
        XraySnapshot.kind == "deep",
        XraySnapshot.status.in_(("pending", "running")),
    )
    if max_age_minutes is not None:
        cutoff = datetime.utcnow() - timedelta(minutes=max_age_minutes)
        q = q.filter(XraySnapshot.created_at >= cutoff)
    return q.order_by(XraySnapshot.created_at.desc()).first()


def history(db: Session, company_id: str, *, limit: int = 50) -> list[XraySnapshot]:
    return (
        db.query(XraySnapshot)
        .filter(XraySnapshot.company_id == str(company_id))
        .order_by(XraySnapshot.version.desc())
        .limit(limit)
        .all()
    )


def timeline(
    db: Session,
    company_id: str,
    *,
    source: str | None = None,
    limit: int = 50,
    before: datetime | None = None,
) -> list[XrayTimelineEvent]:
    """Newest-first timeline slice, optionally filtered by source / paged by ts."""
    q = db.query(XrayTimelineEvent).filter(
        XrayTimelineEvent.company_id == str(company_id)
    )
    if source:
        q = q.filter(XrayTimelineEvent.source == source)
    if before is not None:
        q = q.filter(XrayTimelineEvent.ts < before)
    # NULL ts sorts last; order by ts desc then created_at desc for stability.
    return (
        q.order_by(
            XrayTimelineEvent.ts.desc().nullslast(),
            XrayTimelineEvent.created_at.desc(),
        )
        .limit(limit)
        .all()
    )


def snapshot_index(db: Session, company_ids: list[str]) -> dict[str, XraySnapshot]:
    """Latest ready snapshot per company id (for the customers-list merge)."""
    out: dict[str, XraySnapshot] = {}
    if not company_ids:
        return out
    rows = (
        db.query(XraySnapshot)
        .filter(
            XraySnapshot.company_id.in_([str(c) for c in company_ids]),
            XraySnapshot.status == "ready",
        )
        .order_by(
            XraySnapshot.company_id,
            XraySnapshot.updated_at.desc(),
            XraySnapshot.version.desc(),
        )
        .all()
    )
    for r in rows:
        if r.company_id not in out:  # first per company = most recently completed
            out[r.company_id] = r
    return out


def recent_ready(db: Session, *, limit: int = 24) -> list[XraySnapshot]:
    """Most-recent ready snapshots (recent X-Rays grid), one per company."""
    rows = (
        db.query(XraySnapshot)
        .filter(XraySnapshot.status == "ready")
        .order_by(XraySnapshot.updated_at.desc())
        .limit(limit * 4)
        .all()
    )
    out: list[XraySnapshot] = []
    seen: set[str] = set()
    for r in rows:
        if r.company_id in seen:
            continue
        seen.add(r.company_id)
        out.append(r)
        if len(out) >= limit:
            break
    return out

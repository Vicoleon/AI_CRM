"""Read/write the shared attendee-research cache.

Every function here is fail-soft: a cache miss and a cache *error* must look the
same to the caller, because the fallback for both is "research them again", and
a DB hiccup should never be the reason a briefing loses its attendee cards.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from rubik_core.database.db import SessionLocal
from rubik_core.database.models import AttendeeProfile
from rubik_core.people import config
from rubik_core.people.schemas import AttendeeIntel

logger = logging.getLogger(__name__)


def _row_to_intel(row: AttendeeProfile) -> AttendeeIntel:
    return AttendeeIntel.from_dict(
        {
            "email": row.email,
            "name": row.name,
            "profile": row.profile,
            "activity": row.activity,
            "persona": row.persona,
            "sources": row.sources,
            "provider": row.provider,
            "researched_at": (
                row.researched_at.isoformat() if row.researched_at else ""
            ),
            "error": row.error,
        }
    )


def _is_fresh(row: AttendeeProfile, now: datetime) -> bool:
    """Freshness, with a much shorter grace period for failed rows.

    A successful row is good for `CACHE_TTL_DAYS`. A row that recorded an error
    is cached only for `ERROR_CACHE_TTL_HOURS` — long enough to stop a burst of
    retries hammering a broken provider during one prep session, short enough
    that a transient outage does not blank the person for two weeks.
    """
    if not row.researched_at:
        return False
    age = now - row.researched_at
    if row.error:
        return age < timedelta(hours=config.ERROR_CACHE_TTL_HOURS)
    return age < timedelta(days=config.CACHE_TTL_DAYS)


def load_fresh(
    emails: list[str], *, db: Session | None = None
) -> dict[str, AttendeeIntel]:
    """Fresh cached bundles for these emails, keyed by lowercased email.

    Returns ``{}`` on any error — the caller then simply researches everyone.
    """
    wanted = [e.strip().lower() for e in emails if e and e.strip()]
    if not wanted:
        return {}

    # Session construction is INSIDE the try: opening a connection is itself the
    # most likely thing to fail (pool exhaustion, a Cloud SQL cold start), and it
    # has to degrade to a miss like any other cache error.
    session = None
    try:
        session = db or SessionLocal()
        rows = (
            session.query(AttendeeProfile)
            .filter(AttendeeProfile.email.in_(wanted))
            .all()
        )
        now = datetime.utcnow()
        return {row.email: _row_to_intel(row) for row in rows if _is_fresh(row, now)}
    except Exception:  # noqa: BLE001 - a cache miss and a cache error are the same
        logger.warning("attendee intel: cache read failed", exc_info=True)
        return {}
    finally:
        if db is None and session is not None:
            session.close()


def save(intel: AttendeeIntel, *, db: Session | None = None) -> None:
    """Upsert one researched person. Best-effort; never raises."""
    email = (intel.email or "").strip().lower()
    if not email:
        return

    session = None
    try:
        session = db or SessionLocal()
        row = (
            session.query(AttendeeProfile)
            .filter(AttendeeProfile.email == email)
            .one_or_none()
        )
        if row is None:
            row = AttendeeProfile(id=uuid.uuid4().hex, email=email)
            session.add(row)
        row.name = intel.name or None
        row.linkedin_url = intel.profile.linkedin_url or None
        row.provider = intel.provider or None
        row.confidence = intel.persona.confidence or None
        row.profile = intel.profile.as_dict()
        row.activity = [a.as_dict() for a in intel.activity]
        row.persona = intel.persona.as_dict()
        row.sources = intel.sources
        row.error = intel.error or None
        row.researched_at = datetime.utcnow()
        session.commit()
    except Exception:  # noqa: BLE001 - a briefing must not fail on a cache write
        logger.warning(
            "attendee intel: cache write failed for %s", email, exc_info=True
        )
        try:
            if session is not None:
                session.rollback()
        except Exception:  # noqa: BLE001
            pass
    finally:
        if db is None and session is not None:
            session.close()

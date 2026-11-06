"""AI Priorities card history (``priority_events``).

``priority_memory`` is upserted and keeps only a card's LATEST state, so it
cannot answer "how many cards were acted on last month" or "how long after
delivery". This append-only log can. One row per thing that happened to a card:

* state transitions written by ``rubik_core/priorities/memory.py``: resolved
  (the person clicked ✓), auto_resolved (evidence did it: a meeting prep ran, an
  email went out, the EOS item was completed), dismissed, snoozed, reopened
  (by the person from the history list, by an EOS item being re-opened, or by
  the system when a snooze ran out and the card came back);
* the "acted on" card action in ``backend/routers/home.py``: slack_followup;
* delivered_slack, once per person per successful morning DM
  (``backend/agents/slack_delivery.py``).

A card that covers several entities (two overdue To-Dos) writes one transition
per covered entity, all carrying the card's ``priority_id``; count distinct
``priority_id`` for card-level numbers. ``actor`` is who caused it: the
person's email, someone else's email, or ``"system"``.

Best effort, always: recording never raises and never fails the action it
records. Ids and timestamps only — never a title, message or reply.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

KINDS: frozenset[str] = frozenset(
    {
        "resolved",
        "auto_resolved",
        "dismissed",
        "snoozed",
        "reopened",
        "slack_followup",
        "delivered_slack",
    }
)

SYSTEM_ACTOR = "system"


def _insights_enabled() -> bool:
    try:
        from rubik_core.config import settings

        return bool(getattr(settings, "INSIGHTS_ENABLED", True))
    except Exception:  # noqa: BLE001
        return False


def _str_or_none(value: Any) -> str | None:
    """Accept an enum (``EntityKind``/``PriorityKind``), a string or ``None``."""
    if value is None:
        return None
    value = getattr(value, "value", value)
    text = str(value).strip()
    return text or None


def _department_for(db, email: str) -> str | None:
    """Snapshot the person's department slug from ``users``. ``None`` if unknown."""
    try:
        from sqlalchemy import func

        from rubik_core.database.models import User
        from rubik_core.rag.config import slugify_department

        row = db.query(User.department).filter(func.lower(User.email) == email).first()
        if row is None or not row[0]:
            return None
        return slugify_department(row[0]) or None
    except Exception:  # noqa: BLE001 — a missing department is not a lost event
        logger.debug("priority_events: department lookup failed", exc_info=True)
        return None


def record_priority_event(
    *,
    user_email: str | None,
    kind: str,
    priority_id: str | None = None,
    entity_kind: Any = None,
    entity_id: Any = None,
    priority_kind: Any = None,
    actor: str | None = None,
    department_slug: str | None = None,
    at: datetime | None = None,
    db=None,
) -> bool:
    """Append one ``priority_events`` row. Returns True if it was written.

    Never raises: an unknown ``kind``, a missing email, a database error or the
    ``INSIGHTS_ENABLED`` kill switch all return False (logged at warning when
    something actually failed).

    ``db``: pass the caller's session to write inside its transaction. The row
    goes in under a SAVEPOINT, so a failure here rolls back only this row and
    leaves the caller's work, and its transaction, intact; the caller commits.
    Without ``db`` a short session of its own is opened and committed.
    ``department_slug`` is looked up from ``users`` when not given.
    """
    if not _insights_enabled():
        return False
    try:
        email = (user_email or "").strip().lower()
        if not email:
            return False
        if kind not in KINDS:
            logger.warning("priority_events: unknown kind %r ignored", kind)
            return False
        from rubik_core.database.insights import PriorityEvent

        def _event(session) -> PriorityEvent:
            dept = department_slug or _department_for(session, email)
            return PriorityEvent(
                user_email=email,
                kind=kind,
                priority_id=_str_or_none(priority_id),
                entity_kind=_str_or_none(entity_kind),
                entity_id=_str_or_none(entity_id),
                priority_kind=_str_or_none(priority_kind),
                actor=(actor or "").strip().lower() or None,
                department_slug=dept,
                at=at or datetime.utcnow(),
            )

        if db is not None:
            savepoint = db.begin_nested()
            try:
                db.add(_event(db))
                savepoint.commit()
            except Exception:
                savepoint.rollback()
                raise
            return True

        from rubik_core.database.db import SessionLocal

        with SessionLocal() as session:
            session.add(_event(session))
            session.commit()
        return True
    except Exception:  # noqa: BLE001 — telemetry never fails the action
        logger.warning(
            "priority_events: could not record %s for %s",
            kind,
            user_email,
            exc_info=True,
        )
        return False

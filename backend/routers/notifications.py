"""V2 Notifications — polling-transport backend for the TopBar bell.

V2_1 ships the storage + read endpoints only. Creation triggers
(agent recommendations crossing severity, deal stage advances, ingest
failures, doc approvals) wire up when their respective features land.
SSE (``GET /api/notifications/stream``) is deferred — the frontend polls at
60s.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from opentelemetry import trace
from sqlalchemy import desc
from sqlalchemy.orm import Session

from backend.auth import get_current_user
from rubik_core.auth.registry import UserProfile
from rubik_core.database.db import get_db
from rubik_core.database.models import Notification as NotificationModel
from rubik_core.database.models import UserPreference
from rubik_core.database.schemas import Notification as NotificationSchema

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

router = APIRouter(prefix="/notifications", tags=["notifications"])


def _muted_kinds(db: Session, user_email: str) -> list[str]:
    """The notification kinds this user has muted (General → Notifications).

    Returns ``[]`` on any failure or when nothing is muted, so the bell feed
    is never accidentally emptied by a lookup error.
    """
    try:
        muted = (
            db.query(UserPreference.muted_notification_kinds)
            .filter(UserPreference.user_email == user_email)
            .scalar()
        )
        if isinstance(muted, list):
            return [str(k) for k in muted if k]
    except Exception:  # noqa: BLE001 — muting is best-effort
        logger.warning("muted-kinds lookup failed for %s", user_email, exc_info=True)
    return []


def _expire_stale_meeting_prep_builds(db: Session, user_email: str) -> None:
    """Settle the caller's background Meeting Prep builds that outlived their
    clock BEFORE the feed is read, so the "couldn't be built" notification
    reaches this very poll — wherever the reader is in Cubie, not only when
    they reopen that meeting (``rubik_core/meeting_prep/background.py``).
    Fail-soft: never breaks the feed."""
    try:
        from rubik_core.meeting_prep.background import expire_stale_for_user

        expire_stale_for_user(db, user_email)
    except Exception:  # noqa: BLE001 — the bell feed must never break over this
        logger.warning(
            "meeting-prep build sweep failed for %s", user_email, exc_info=True
        )
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


@router.get("/", response_model=list[NotificationSchema])
async def list_notifications(
    unread: bool = Query(
        False,
        description="If true, only return notifications where read_at IS NULL.",
    ),
    limit: int = Query(
        20, ge=1, le=100, description="Max rows to return. Default 20, max 100."
    ),
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[NotificationModel]:
    """List the caller's notifications, newest first.

    Scoped to ``actor_email = user.email``. When ``unread=true``, filters
    to ``read_at IS NULL``. Ordered by ``created_at DESC`` to match the
    composite index.
    """
    with tracer.start_as_current_span("notifications.list") as span:
        span.set_attribute("actor_email", user.email)
        span.set_attribute("unread", unread)
        span.set_attribute("limit", limit)

        _expire_stale_meeting_prep_builds(db, user.email)

        query = db.query(NotificationModel).filter(
            NotificationModel.actor_email == user.email
        )
        if unread:
            query = query.filter(NotificationModel.read_at.is_(None))

        # Per-user muted kinds (General → Notifications) are hidden from the bell.
        muted = _muted_kinds(db, user.email)
        if muted:
            query = query.filter(NotificationModel.kind.notin_(muted))
            span.set_attribute("muted_kinds", len(muted))

        rows = query.order_by(desc(NotificationModel.created_at)).limit(limit).all()
        span.set_attribute("returned_count", len(rows))
        return rows


@router.post("/{notification_id}/read")
async def mark_notification_read(
    notification_id: str,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Mark a single notification as read.

    Returns 404 if the row doesn't exist OR if its ``actor_email`` doesn't
    match the caller (no information leak — both look identical to
    unauthorized callers).
    """
    with tracer.start_as_current_span("notifications.read") as span:
        span.set_attribute("actor_email", user.email)
        span.set_attribute("notification_id", notification_id)

        row = (
            db.query(NotificationModel)
            .filter(NotificationModel.id == notification_id)
            .first()
        )
        if row is None or row.actor_email != user.email:
            # Same 404 either way — don't leak existence to non-owners.
            raise HTTPException(status_code=404, detail="Notification not found")

        if row.read_at is None:
            row.read_at = datetime.utcnow()
            db.commit()

        span.set_attribute("kind", row.kind)
        span.set_attribute("severity", row.severity)
        return {"status": "ok"}


@router.post("/read-all")
async def mark_all_notifications_read(
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, int]:
    """Mark every unread notification for the caller as read.

    Returns the number of rows updated so the frontend can confirm /
    update its local badge count.
    """
    with tracer.start_as_current_span("notifications.read_all") as span:
        span.set_attribute("actor_email", user.email)

        now = datetime.utcnow()
        updated = (
            db.query(NotificationModel)
            .filter(
                NotificationModel.actor_email == user.email,
                NotificationModel.read_at.is_(None),
            )
            .update({NotificationModel.read_at: now}, synchronize_session=False)
        )
        db.commit()
        span.set_attribute("updated_count", updated)
        return {"updated": updated}

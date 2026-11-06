"""V2 Recent — recently-touched feed endpoints (``/api/recent``).

The frontend pings ``POST /api/recent/touch`` whenever the user opens a
record or fires a chat question. ``GET /api/recent`` returns the most
recent N entries (default 50). Per-user rows are private — no admin
override in V2_1.

Server-side truncation to the last 200 entries is intentionally deferred
to a Cloud Scheduler job (spec §Retention), not enforced here.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from opentelemetry import trace
from sqlalchemy.orm import Session

from backend.auth import get_current_user
from rubik_core.auth.registry import UserProfile
from rubik_core.database.db import get_db
from rubik_core.database.models import UserRecentTouch
from rubik_core.database.schemas import RecentItem, RecentTouchRequest

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

router = APIRouter(prefix="/recent", tags=["recent"])


def _to_recent_item(row: UserRecentTouch) -> RecentItem:
    return RecentItem(
        id=row.id,
        target_kind=row.target_kind,
        target_id=row.target_id,
        title=row.title,
        link=row.link,
        touched_at=row.touched_at,
    )


@router.get("/", response_model=list[RecentItem])
def list_recent(
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: UserProfile = Depends(get_current_user),
) -> list[RecentItem]:
    """Return the caller's most recently touched targets, newest first."""
    with tracer.start_as_current_span("recent.list") as span:
        span.set_attribute("actor_email", user.email)
        span.set_attribute("limit", limit)

        rows = (
            db.query(UserRecentTouch)
            .filter(UserRecentTouch.actor_email == user.email)
            .order_by(UserRecentTouch.touched_at.desc())
            .limit(limit)
            .all()
        )
        return [_to_recent_item(r) for r in rows]


@router.post("/touch", response_model=RecentItem)
def touch_recent(
    request: RecentTouchRequest,
    db: Session = Depends(get_db),
    user: UserProfile = Depends(get_current_user),
) -> RecentItem:
    """Upsert a recent-touch row.

    Existing entry → update ``touched_at`` + refresh ``title`` / ``link``
    (the label may have changed since the last touch — e.g. a deal got
    renamed). No existing entry → insert.
    """
    with tracer.start_as_current_span("recent.touch") as span:
        span.set_attribute("actor_email", user.email)
        span.set_attribute("target_kind", request.target_kind)
        span.set_attribute("target_id", request.target_id)

        now = datetime.utcnow()
        existing = (
            db.query(UserRecentTouch)
            .filter(
                UserRecentTouch.actor_email == user.email,
                UserRecentTouch.target_kind == request.target_kind,
                UserRecentTouch.target_id == request.target_id,
            )
            .one_or_none()
        )

        if existing is not None:
            existing.touched_at = now
            existing.title = request.title
            existing.link = request.link
            db.commit()
            db.refresh(existing)
            return _to_recent_item(existing)

        row = UserRecentTouch(
            id=str(uuid.uuid4()),
            actor_email=user.email,
            target_kind=request.target_kind,
            target_id=request.target_id,
            title=request.title,
            link=request.link,
            touched_at=now,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return _to_recent_item(row)

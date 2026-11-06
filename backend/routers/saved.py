"""V2 Saved — bookmark endpoints (``/api/saved``).

All rows are scoped to ``actor_email = current_user.email``; there is no
admin override in V2_1.
Title / subtitle / link are emitted as placeholders for now — the
domain-specific PRs (Library, Deals, Meetings, Contacts) will denormalize
them later.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from opentelemetry import trace
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.auth import get_current_user
from rubik_core.auth.registry import UserProfile
from rubik_core.database.db import get_db
from rubik_core.database.models import UserBookmark
from rubik_core.database.schemas import SavedCreateRequest, SavedItem

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

router = APIRouter(prefix="/saved", tags=["saved"])


def _to_saved_item(row: UserBookmark) -> SavedItem:
    """V2_1 placeholder hydration.

    Denormalization (title / subtitle / link from the actual target
    record) is intentionally deferred — see the docstring on
    ``SavedItem``. The link is a generic deep-link template the
    frontend can route on without needing a server lookup.
    """
    return SavedItem(
        id=row.id,
        target_kind=row.target_kind,
        target_id=row.target_id,
        title=f"{row.target_kind}:{row.target_id}",
        subtitle=None,
        saved_at=row.created_at,
        link=f"/v2/{row.target_kind}/{row.target_id}",
    )


@router.get("/", response_model=list[SavedItem])
def list_saved(
    db: Session = Depends(get_db),
    user: UserProfile = Depends(get_current_user),
) -> list[SavedItem]:
    """List the caller's bookmarks, newest first."""
    with tracer.start_as_current_span("saved.list") as span:
        span.set_attribute("actor_email", user.email)
        rows = (
            db.query(UserBookmark)
            .filter(UserBookmark.actor_email == user.email)
            .order_by(UserBookmark.created_at.desc())
            .all()
        )
        return [_to_saved_item(r) for r in rows]


@router.post("/", response_model=SavedItem)
def create_saved(
    request: SavedCreateRequest,
    db: Session = Depends(get_db),
    user: UserProfile = Depends(get_current_user),
) -> SavedItem:
    """Bookmark a target. Idempotent via the UNIQUE constraint.

    Concurrent inserts of the same (actor, kind, id) trio race into the
    UNIQUE constraint; we catch ``IntegrityError`` and return the
    pre-existing row so the client doesn't have to retry.
    """
    with tracer.start_as_current_span("saved.create") as span:
        span.set_attribute("actor_email", user.email)
        span.set_attribute("target_kind", request.target_kind)
        span.set_attribute("target_id", request.target_id)

        row = UserBookmark(
            id=str(uuid.uuid4()),
            actor_email=user.email,
            target_kind=request.target_kind,
            target_id=request.target_id,
            created_at=datetime.utcnow(),
        )
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = (
                db.query(UserBookmark)
                .filter(
                    UserBookmark.actor_email == user.email,
                    UserBookmark.target_kind == request.target_kind,
                    UserBookmark.target_id == request.target_id,
                )
                .one_or_none()
            )
            if existing is None:
                # Truly unexpected — re-raise as 500.
                logger.exception(
                    "Bookmark insert failed but no existing row found for %s / %s / %s",
                    user.email,
                    request.target_kind,
                    request.target_id,
                )
                raise HTTPException(
                    status_code=500,
                    detail="Failed to create bookmark",
                )
            return _to_saved_item(existing)

        db.refresh(row)
        return _to_saved_item(row)


@router.delete("/{bookmark_id}")
def delete_saved(
    bookmark_id: str,
    db: Session = Depends(get_db),
    user: UserProfile = Depends(get_current_user),
) -> dict[str, str]:
    """Delete the caller's bookmark.

    Returns 404 (NOT 403) on a foreign row to avoid leaking that a given
    bookmark id exists for someone else — per the privacy rule in the
    v2 spec.
    """
    with tracer.start_as_current_span("saved.delete") as span:
        span.set_attribute("actor_email", user.email)
        span.set_attribute("bookmark_id", bookmark_id)

        row = (
            db.query(UserBookmark)
            .filter(
                UserBookmark.id == bookmark_id,
                UserBookmark.actor_email == user.email,
            )
            .one_or_none()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Bookmark not found")

        db.delete(row)
        db.commit()
        return {"status": "success"}

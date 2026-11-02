"""WGLL feedback API — capture + view user ratings on Cubie's proposals/messages.

Surfaces:
  * POST   /api/feedback              — upsert a rating (any authenticated user)
  * GET    /api/feedback/mine         — caller's ratings for a surface (selected state)
  * GET    /api/feedback/admin        — admin: paginated list + summary counts
  * GET    /api/feedback/admin/export — admin: full JSON export of the dataset
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import desc
from sqlalchemy.orm import Session

from backend.auth import get_current_user
from rubik_core.auth.registry import UserProfile
from rubik_core.brand import PRODUCT_SLUG
from rubik_core.database.db import get_db
from rubik_core.database.models import FEEDBACK_RATINGS, AssistantFeedback, HomeCard
from rubik_core.feedback import store
from rubik_core.priorities.privacy import (
    is_private_gmail_bundle,
    public_priority_ids_for_users,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/feedback", tags=["feedback"])

Surface = Literal["priorities", "chat", "meeting_prep", "xray", "crm_sync"]
Rating = Literal["useful", "meh", "not_useful"]


class FeedbackIn(BaseModel):
    surface: Surface
    proposal_id: str
    # Snapshot of the proposal/message text the user is rating, captured client
    # side so it stays meaningful even if the source later changes.
    proposal_text: str = ""
    rating: Rating
    comment: str | None = None
    proposal_metadata: dict[str, Any] | None = None


class FeedbackOut(BaseModel):
    id: str
    surface: str
    proposal_id: str
    rating: str
    comment: str | None = None
    updated_at: datetime

    model_config = {"from_attributes": True}


class FeedbackRow(FeedbackOut):
    proposal_text: str
    proposal_metadata: dict[str, Any] | None = None
    user_email: str
    created_at: datetime


class FeedbackListOut(BaseModel):
    items: list[FeedbackRow]
    total: int
    counts: dict[str, int]


def _drew_on_private_files(db: Session, user: UserProfile, message_id: str) -> bool:
    """True when the rated chat answer is flagged ``private_files`` (it read the
    person's own attached files; ``backend/routers/chat.py``). Only the caller's
    own conversations are looked at."""
    from rubik_core.database.models import ChatMessage, ChatSession

    try:
        msg = (
            db.query(ChatMessage)
            .join(ChatSession, ChatSession.id == ChatMessage.session_id)
            .filter(ChatMessage.id == message_id, ChatSession.user_email == user.email)
            .first()
        )
    except Exception:  # noqa: BLE001 — fail closed: keep no text
        logger.warning("feedback: could not check a chat message", exc_info=True)
        return True
    return bool(msg is not None and (msg.metadata_json or {}).get("private_files"))


def _priority_index(db: Session, user_email: str) -> dict[str, tuple[bool, dict]]:
    """Resolve a priority ID from its owner's saved cards, never from client text.

    A mixed run is private in full: a work-looking card may still have been
    written with personal Gmail in the model context. If an ID occurs in more
    than one saved bundle, any private occurrence wins.
    """
    cards = (
        db.query(HomeCard.metadata_json)
        .filter(
            HomeCard.user_email == user_email,
            HomeCard.category.in_(("daily_briefing", "next_week_report")),
        )
        .order_by(desc(HomeCard.created_at))
        .all()
    )
    index: dict[str, tuple[bool, dict]] = {}
    for (metadata,) in cards:
        if not isinstance(metadata, dict):
            continue
        priorities = metadata.get("priorities") or []
        if not isinstance(priorities, list):
            continue
        bundle_private = is_private_gmail_bundle(metadata)
        for priority in priorities:
            if not isinstance(priority, dict):
                continue
            priority_id = priority.get("id")
            if not isinstance(priority_id, str) or not priority_id:
                continue
            previous = index.get(priority_id)
            if previous is None or (bundle_private and not previous[0]):
                index[priority_id] = (bundle_private, priority)
    return index


def _visible_admin_feedback(db: Session):
    """Stream shared feedback, omitting unverified/private priority cards.

    The lookup reads saved briefings once for the people who rated a priority;
    a page request does not load every feedback row or perform a HomeCard query
    per row. We still scan for exact visible totals and rating counts.
    """
    priority_owners = [
        email
        for (email,) in db.query(AssistantFeedback.user_email)
        .filter(AssistantFeedback.surface == "priorities")
        .distinct()
        .all()
    ]
    public_ids = public_priority_ids_for_users(db, priority_owners)
    rows = (
        db.query(AssistantFeedback)
        .order_by(desc(AssistantFeedback.updated_at))
        .yield_per(200)
    )
    for row in rows:
        if row.surface == "priorities" and row.proposal_id not in public_ids.get(
            row.user_email.strip().lower(), set()
        ):
            continue
        yield row


@router.post("", response_model=FeedbackOut)
@router.post("/", response_model=FeedbackOut)
async def submit_feedback(
    body: FeedbackIn,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Set (or change) the caller's rating for one proposal/message."""
    proposal_text = body.proposal_text or ""
    proposal_metadata = body.proposal_metadata
    if body.surface == "priorities":
        try:
            match = _priority_index(db, user.email).get(body.proposal_id)
        except Exception:  # noqa: BLE001 — never save unverified private text
            logger.warning("feedback: could not verify a priority", exc_info=True)
            raise HTTPException(
                status_code=503, detail="Priority unavailable"
            ) from None
        if match is None:
            raise HTTPException(status_code=404, detail="Priority unavailable")
        private, priority = match
        if private:
            raise HTTPException(
                status_code=403, detail="Private priority feedback unavailable"
            )
        proposal_text = "\n".join(
            part
            for part in (
                priority.get("title") or "",
                priority.get("why_it_matters") or "",
                f"Next: {priority['suggested_action']}"
                if priority.get("suggested_action")
                else "",
            )
            if isinstance(part, str) and part
        )
        proposal_metadata = {
            key: priority.get(key)
            for key in (
                "title",
                "urgency",
                "suggested_action",
                "priority_kind",
                "entity_kind",
                "entity_id",
            )
        }
    if body.surface == "chat" and _drew_on_private_files(db, user, body.proposal_id):
        # Admins read feedback text (Admin → Feedback, the export). An answer
        # built from the person's private Ask Cubie files is not copied there:
        # the rating and the comment are kept, the answer's text is not.
        proposal_text = ""
    try:
        row = store.upsert_feedback(
            db,
            user_email=user.email,
            surface=body.surface,
            proposal_id=body.proposal_id,
            proposal_text=proposal_text,
            rating=body.rating,
            comment=body.comment,
            proposal_metadata=proposal_metadata,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    logger.info(
        "feedback: %s rated %s/%s -> %s",
        user.email,
        body.surface,
        body.proposal_id,
        body.rating,
    )
    return FeedbackOut.model_validate(row)


@router.get("/mine")
async def my_feedback(
    surface: Surface = Query(...),
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, dict]:
    """``{proposal_id: {rating, comment}}`` for the caller on one surface.

    Lets the UI restore the selected face + saved note after a reload.
    """
    return store.get_user_ratings(db, user_email=user.email, surface=surface)


def _require_admin(user: UserProfile) -> None:
    if not getattr(user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin access required")


@router.get("/admin", response_model=FeedbackListOut)
async def admin_list_feedback(
    surface: Surface | None = Query(None),
    rating: Rating | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Admin view of all collected feedback (WGLL dataset)."""
    _require_admin(user)
    counts = {value: 0 for value in FEEDBACK_RATINGS}
    items = []
    total = 0
    for row in _visible_admin_feedback(db):
        counts[row.rating] += 1
        if (surface is not None and row.surface != surface) or (
            rating is not None and row.rating != rating
        ):
            continue
        if total >= offset and len(items) < limit:
            items.append(FeedbackRow.model_validate(row))
        total += 1
    return FeedbackListOut(
        items=items,
        total=total,
        counts=counts,
    )


@router.get("/admin/export")
async def admin_export_feedback(
    surface: Surface | None = Query(None),
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Download the full WGLL dataset as a JSON file (for eval/example reuse)."""
    _require_admin(user)
    data = [
        store.to_dict(row)
        for row in _visible_admin_feedback(db)
        if surface is None or row.surface == surface
    ]
    suffix = f"-{surface}" if surface else ""
    filename = f"{PRODUCT_SLUG}-feedback{suffix}.json"
    return JSONResponse(
        content=data,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

"""Persistence + query helpers for AssistantFeedback (WGLL ratings).

Kept out of the router so the same logic is reusable from the WGLL prompt
builder, scripts, and future exporters.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from rubik_core.database.models import (
    FEEDBACK_RATINGS,
    FEEDBACK_SURFACES,
    AssistantFeedback,
)


def upsert_feedback(
    db: Session,
    *,
    user_email: str,
    surface: str,
    proposal_id: str,
    proposal_text: str,
    rating: str,
    comment: str | None = None,
    proposal_metadata: dict | None = None,
) -> AssistantFeedback:
    """Insert or update a user's rating for a single proposal/message.

    Identity is ``(user_email, surface, proposal_id)``. Re-rating the same item
    updates the existing row (rating/comment/snapshot refreshed) so each row is
    the user's settled judgment, not an append-only log.
    """
    if surface not in FEEDBACK_SURFACES:
        raise ValueError(f"invalid surface: {surface!r}")
    if rating not in FEEDBACK_RATINGS:
        raise ValueError(f"invalid rating: {rating!r}")

    row = (
        db.query(AssistantFeedback)
        .filter(
            AssistantFeedback.user_email == user_email,
            AssistantFeedback.surface == surface,
            AssistantFeedback.proposal_id == proposal_id,
        )
        .first()
    )

    now = datetime.utcnow()
    if row is None:
        row = AssistantFeedback(
            id=str(uuid.uuid4()),
            user_email=user_email,
            surface=surface,
            proposal_id=proposal_id,
            proposal_text=proposal_text,
            proposal_metadata=proposal_metadata,
            rating=rating,
            comment=(comment or None),
            created_at=now,
            updated_at=now,
        )
        db.add(row)
    else:
        row.rating = rating
        row.comment = comment or None
        # Refresh the snapshot — the displayed proposal may have changed text
        # since the last rating, and we want the latest thing the user judged.
        row.proposal_text = proposal_text
        if proposal_metadata is not None:
            row.proposal_metadata = proposal_metadata
        row.updated_at = now

    db.commit()
    db.refresh(row)
    return row


def get_user_ratings(db: Session, *, user_email: str, surface: str) -> dict[str, dict]:
    """Return ``{proposal_id: {"rating", "comment"}}`` for one user + surface.

    Used by the UI to render the selected face (and any saved note) when a page
    or chat session reloads.
    """
    rows = (
        db.query(AssistantFeedback)
        .filter(
            AssistantFeedback.user_email == user_email,
            AssistantFeedback.surface == surface,
        )
        .all()
    )
    return {r.proposal_id: {"rating": r.rating, "comment": r.comment} for r in rows}


def list_feedback(
    db: Session,
    *,
    surface: str | None = None,
    rating: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[AssistantFeedback], int]:
    """Admin list: newest-first, optionally filtered by surface/rating.

    Returns ``(rows, total_count)`` so the UI can paginate.
    """
    q = db.query(AssistantFeedback)
    if surface:
        q = q.filter(AssistantFeedback.surface == surface)
    if rating:
        q = q.filter(AssistantFeedback.rating == rating)

    total = q.with_entities(func.count(AssistantFeedback.id)).scalar() or 0
    rows = (
        q.order_by(desc(AssistantFeedback.updated_at)).offset(offset).limit(limit).all()
    )
    return rows, int(total)


def counts_by_rating(db: Session) -> dict[str, int]:
    """Summary tallies for the admin header — counts per rating across surfaces."""
    rows = (
        db.query(AssistantFeedback.rating, func.count(AssistantFeedback.id))
        .group_by(AssistantFeedback.rating)
        .all()
    )
    out = {r: 0 for r in FEEDBACK_RATINGS}
    for rating, n in rows:
        out[rating] = int(n)
    return out


def to_dict(row: AssistantFeedback) -> dict:
    """Self-describing export shape — one WGLL example."""
    return {
        "id": row.id,
        "surface": row.surface,
        "proposal_id": row.proposal_id,
        "proposal_text": row.proposal_text,
        "proposal_metadata": row.proposal_metadata,
        "rating": row.rating,
        "comment": row.comment,
        "user_email": row.user_email,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def export_feedback(db: Session, *, surface: str | None = None) -> list[dict]:
    """Full export of the WGLL dataset (optionally one surface), newest-first."""
    q = db.query(AssistantFeedback)
    if surface:
        q = q.filter(AssistantFeedback.surface == surface)
    rows = q.order_by(desc(AssistantFeedback.updated_at)).all()
    return [to_dict(r) for r in rows]

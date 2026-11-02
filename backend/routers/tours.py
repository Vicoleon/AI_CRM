"""Page tours ("Walk me"): each person's memory of the tours they have seen.

`GET    /api/tours`           → ``{"enabled": bool, "seen": {tour_id: status}}``
`POST   /api/tours/seen`      → mark tours finished or skipped, answer the new state
`PUT    /api/tours/settings`  → ``{"enabled": bool}``: tours start by themselves, or not
`DELETE /api/tours/seen`      → forget every tour, so each page's tour runs again

The tours themselves are content shipped with the client
(``frontend/src/tours/``): which page, which cards, what each card says. The
server never needs to know them, so a tour id it has not heard of is stored
like any other (shape and count are capped, so the table cannot be flooded).

WHO. ``page_tours`` in Admin → Feature Access, declared on the router so no
endpoint can be added without it. Mounted OUTSIDE the copilot pilot (`core`)
on purpose: the EOS pages carry tours too, and most of the company is EOS-only,
so a `core` mount would 403 exactly the people a tour helps most the day the
roster widens. Nothing here reads anyone's data but the caller's own rows.

Admins viewing Cubie as someone else are refused by the impersonation
middleware (this router is not on its allowlist), so a tour finished while
impersonating can never be written to the person's memory.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.auth import get_current_user
from rubik_core.auth.registry import UserProfile
from rubik_core.brand import PRODUCT_NAME
from rubik_core.database.db import get_db
from rubik_core.database.models import TOURS_SWITCH_ID, UserTourProgress
from rubik_core.features import feature_allows

logger = logging.getLogger(__name__)

FEATURE_KEY = "page_tours"

#: A tour id as the client writes them: ``rocks.intro``, ``chat.new.file-uploads``.
TOUR_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,119}$")
#: One finished tour marks its page's newer tours too; a page has a handful.
MAX_IDS_PER_REQUEST = 50
#: Far above the number of tours Cubie has; only a scripted flood reaches it.
MAX_ROWS_PER_PERSON = 1000

Status = Literal["completed", "skipped"]


def require_page_tours(user: UserProfile = Depends(get_current_user)) -> UserProfile:
    """403 unless the caller is on the ``page_tours`` roster."""
    if not feature_allows(FEATURE_KEY, user):
        raise HTTPException(
            status_code=403, detail="Page tours aren't turned on for your team yet."
        )
    return user


router = APIRouter(
    prefix="/tours", tags=["tours"], dependencies=[Depends(require_page_tours)]
)


class TourState(BaseModel):
    enabled: bool = Field(
        description="False when the person turned off tours that start by themselves."
    )
    seen: dict[str, Status] = Field(
        description="Every tour the person finished or skipped, by tour id."
    )


class MarkSeen(BaseModel):
    tour_ids: list[str] = Field(min_length=1, max_length=MAX_IDS_PER_REQUEST)
    status: Status


class TourSettings(BaseModel):
    enabled: bool


def _key(user: UserProfile) -> str:
    return user.email.strip().lower()


def _state(db: Session, email: str) -> TourState:
    rows = db.query(UserTourProgress).filter(UserTourProgress.user_email == email).all()
    enabled = True
    seen: dict[str, Status] = {}
    for row in rows:
        if row.tour_id == TOURS_SWITCH_ID:
            enabled = row.status != "off"
        elif row.status in ("completed", "skipped"):
            seen[row.tour_id] = row.status  # type: ignore[assignment]
    return TourState(enabled=enabled, seen=seen)


def _unavailable(action: str) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail=f"{PRODUCT_NAME} couldn't {action} your page tours just now. Try again in a moment.",
    )


@router.get("", response_model=TourState)
async def get_tours(
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return _state(db, _key(user))
    except SQLAlchemyError:
        db.rollback()
        logger.warning("tours: progress could not be read", exc_info=True)
        raise _unavailable("load") from None


@router.post("/seen", response_model=TourState)
async def mark_seen(
    body: MarkSeen,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ids = list(dict.fromkeys(t.strip() for t in body.tour_ids))
    bad = [t for t in ids if not TOUR_ID_PATTERN.match(t)]
    if bad:
        raise HTTPException(status_code=422, detail="That isn't a page tour.")
    email = _key(user)
    now = datetime.utcnow()
    try:
        existing = {
            row.tour_id: row
            for row in db.query(UserTourProgress).filter(
                UserTourProgress.user_email == email,
                UserTourProgress.tour_id.in_(ids),
            )
        }
        new_ids = [t for t in ids if t not in existing]
        if new_ids:
            count = (
                db.query(func.count())
                .select_from(UserTourProgress)
                .filter(UserTourProgress.user_email == email)
                .scalar()
            )
            if count + len(new_ids) > MAX_ROWS_PER_PERSON:
                raise HTTPException(
                    status_code=422, detail="Too many page tours to remember."
                )
        for tour_id in ids:
            row = existing.get(tour_id)
            if row is None:
                db.add(
                    UserTourProgress(
                        user_email=email,
                        tour_id=tour_id,
                        status=body.status,
                        updated_at=now,
                    )
                )
            elif row.status != "completed" or body.status == "completed":
                # A finished tour stays finished: skipping a replay of it later
                # must not turn it into one the person "skipped".
                row.status = body.status
                row.updated_at = now
        db.commit()
        return _state(db, email)
    except SQLAlchemyError:
        db.rollback()
        logger.warning("tours: progress could not be saved", exc_info=True)
        raise _unavailable("save") from None


@router.put("/settings", response_model=TourState)
async def put_settings(
    body: TourSettings,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    email = _key(user)
    try:
        row = db.get(UserTourProgress, (email, TOURS_SWITCH_ID))
        if body.enabled:
            # On is the default: no row.
            if row is not None:
                db.delete(row)
        elif row is None:
            db.add(
                UserTourProgress(
                    user_email=email,
                    tour_id=TOURS_SWITCH_ID,
                    status="off",
                    updated_at=datetime.utcnow(),
                )
            )
        db.commit()
        return _state(db, email)
    except SQLAlchemyError:
        db.rollback()
        logger.warning("tours: the switch could not be saved", exc_info=True)
        raise _unavailable("save") from None


@router.delete("/seen", response_model=TourState)
async def forget_seen(
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Every page's tour runs again. The on/off switch is left as it is."""
    email = _key(user)
    try:
        db.query(UserTourProgress).filter(
            UserTourProgress.user_email == email,
            UserTourProgress.tour_id != TOURS_SWITCH_ID,
        ).delete(synchronize_session=False)
        db.commit()
        return _state(db, email)
    except SQLAlchemyError:
        db.rollback()
        logger.warning("tours: progress could not be cleared", exc_info=True)
        raise _unavailable("reset") from None

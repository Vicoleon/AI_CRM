"""EOS quarters — list + create.

The upstream EOS app only exposes list and create for quarters; we port
exactly. Full PATCH/DELETE/GET-by-id are gaps in upstream and will be
filled in a follow-up if needed.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, status

from backend.models.eos.traction import (
    QuarterCreate,
    QuarterResponse,
)
from backend.routers.eos._shared import (
    CurrentUser,
    DBDep,
    canonical_quarters,
    sync_current_quarter,
)
from rubik_core.database.eos import Quarter

router = APIRouter(prefix="/quarters", tags=["EOS: Quarters"])


@router.get("/", response_model=list[QuarterResponse])
def list_quarters(
    db: DBDep,
    current_user: CurrentUser,
    organization_id: UUID | None = None,
):
    # Self-heal the stored is_current flag to today's calendar quarter before
    # listing — it has no auto-advance, so a Q2 row lingers as "current" into
    # Q3 otherwise. Every consumer (Rocks default view, Exec Dashboard, L10)
    # reads this flag, so fixing it here fixes them all.
    sync_current_quarter(db)
    query = db.query(Quarter).filter(Quarter.is_deleted == False)  # noqa: E712
    if organization_id:
        query = query.filter(Quarter.organization_id == organization_id)
    # Drop test-pollution rows (e.g. "FT7 Q-2026-07-10T23:29:37") so they
    # never reach the period selectors that render this list.
    return canonical_quarters(query.all())


@router.post("/", response_model=QuarterResponse, status_code=status.HTTP_201_CREATED)
def create_quarter(db: DBDep, current_user: CurrentUser, quarter_in: QuarterCreate):
    quarter = Quarter(**quarter_in.model_dump(), created_by=current_user.email)
    db.add(quarter)
    db.commit()
    db.refresh(quarter)
    return quarter

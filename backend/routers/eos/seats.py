"""EOS seats — read-only accountability-chart seats.

Mounted at `/api/eos/seats`. Optional `department_name` query filter
(FKs into Cubie's existing departments table — not an `eos_departments`
table, which doesn't exist).

Seat *management* was removed along with the EOS People page;
people and org structure are managed in the main system now. The read
endpoints stay because existing seats still drive department attribution
in the Exec dashboard (`dashboard.py` joins Person → Seat → department).
There are intentionally no create/update/delete endpoints here anymore.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException

from backend.models.eos.people import SeatResponse
from backend.routers.eos._shared import CurrentUser, DBDep
from rubik_core.database.eos import Seat

router = APIRouter(prefix="/seats", tags=["EOS: Seats"])


@router.get("/", response_model=list[SeatResponse])
def list_seats(
    db: DBDep,
    current_user: CurrentUser,
    department_name: str | None = None,
):
    query = db.query(Seat).filter(Seat.is_deleted == False)  # noqa: E712
    if department_name:
        query = query.filter(Seat.department_name == department_name)
    return query.all()


@router.get("/{seat_id}", response_model=SeatResponse)
def get_seat(seat_id: UUID, db: DBDep, current_user: CurrentUser):
    seat = (
        db.query(Seat)
        .filter(Seat.id == seat_id, Seat.is_deleted == False)  # noqa: E712
        .first()
    )
    if not seat:
        raise HTTPException(status_code=404, detail="Seat not found")
    return seat

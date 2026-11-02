"""People + Seat schemas.

No HR-system id is carried: the column was dropped from the model until an
HR sync exists. `Seat.department_name` is a String FK to the app's
`departments.name` — EOS does not own its own department list.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel

from .base import AuditBase, BaseCreate, BaseUpdate

# -- Person -----------------------------------------------------------


class PersonBase(BaseModel):
    first_name: str
    last_name: str
    # Plain `str`, not `EmailStr`: this is a READ surface over rows synced
    # from the main `users` table, which legitimately contains non-RFC
    # addresses (e.g. impersonation pseudo-users `impersonated_user@dept.local`).
    # A strict EmailStr here 500s the whole roster on one odd value. The
    # create path that needed validation was removed.
    email: str
    seat_id: UUID | None = None
    google_id: str | None = None


class PersonCreate(PersonBase, BaseCreate):
    pass


class PersonUpdate(BaseUpdate):
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    seat_id: UUID | None = None
    google_id: str | None = None


class PersonResponse(PersonBase, AuditBase):
    pass


# -- Seat -------------------------------------------------------------


class SeatBase(BaseModel):
    department_name: str  # FK to departments.name
    title: str
    reports_to_id: UUID | None = None
    responsibilities: list[str] | None = None


class SeatCreate(SeatBase, BaseCreate):
    pass


class SeatUpdate(BaseUpdate):
    department_name: str | None = None
    title: str | None = None
    reports_to_id: UUID | None = None
    responsibilities: list[str] | None = None


class SeatResponse(SeatBase, AuditBase):
    pass

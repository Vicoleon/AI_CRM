"""Seats, people, and GWC evaluations.

`eos_people.email` is the bridge to Cubie's `users.email`; the link is
intentionally soft (no FK) so an EOS seat can exist for someone who has
not yet logged into Cubie. The reverse lookup is done in code, not via
constraint.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import JSON, Boolean, Column, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import relationship, validates

from rubik_core.database.models import Base

from ._base import AuditMixin


class Seat(Base, AuditMixin):
    __tablename__ = "eos_seats"

    # FK to Cubie's existing departments.name (String PK), not eos_departments.
    department_name = Column(
        String, ForeignKey("departments.name", ondelete="RESTRICT"), nullable=False
    )
    title = Column(String, nullable=False)
    reports_to_id = Column(sa.Uuid, ForeignKey("eos_seats.id"), nullable=True)
    responsibilities = Column(JSON)  # list[str]
    # Row-level access: a seat marked leadership can READ every row in its own
    # department(s), not just its own / its direct reports'. Cubie admins and
    # Exec-team members (``UserProfile.is_exec``) are leadership regardless of
    # this flag; it exists for department heads who are neither.
    is_leadership = Column(
        Boolean, default=False, nullable=False, server_default=sa.false()
    )

    reports_to = relationship("Seat", remote_side="Seat.id", backref="subordinates")
    person = relationship("Person", back_populates="seat", uselist=False)


class Person(Base, AuditMixin):
    __tablename__ = "eos_people"

    first_name = Column(String, nullable=False)
    last_name = Column(String, nullable=False)
    email = Column(String, nullable=False, index=True)
    seat_id = Column(sa.Uuid, ForeignKey("eos_seats.id"), nullable=True)
    google_id = Column(String, index=True)

    seat = relationship("Seat", back_populates="person")
    evaluations = relationship("GWCEvaluation", back_populates="person")

    __table_args__ = (UniqueConstraint("email", name="uq_eos_people_email"),)

    @validates("email")
    def _normalize_email(self, _key: str, value: str | None) -> str | None:
        """Store the bridge key folded to lower case, always.

        ``eos_people.email`` is the soft bridge to ``users.email`` (see the
        module docstring), and every identity lookup joins on it — the ACL
        (``backend/routers/eos/_authz.py::_person_for``), the AI-Priorities EOS
        agent, the L10 roster resolver. Google hands us a lower-cased address,
        so a row stored with any other casing simply never matches: the caller
        resolves to no ``Person``, ``_compute_scope`` yields empty
        ``owner_pids``, and they silently lose every owner/report/leadership
        grant — no error, just an empty EOS surface. One such row
        (``Jdoe@acme.example``) did exactly that in dev.

        Normalizing here rather than at each call site makes the invariant
        hold for every write path — the EOS people sync, the L10 calendar
        import's ``_ensure_person``, the admin people editor, seed scripts —
        including ones not yet written. The reads still fold with
        ``func.lower()`` so a row written around the ORM can't re-break it.
        """
        if value is None:
            return None
        return value.strip().lower()


class GWCEvaluation(Base, AuditMixin):
    __tablename__ = "eos_gwc_evaluations"

    person_id = Column(sa.Uuid, ForeignKey("eos_people.id"), nullable=False)
    seat_id = Column(sa.Uuid, ForeignKey("eos_seats.id"), nullable=False)
    get_it = Column(Boolean, nullable=False)
    want_it = Column(Boolean, nullable=False)
    capacity = Column(Boolean, nullable=False)

    person = relationship("Person", back_populates="evaluations")

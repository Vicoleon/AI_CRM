"""Quarterly Rocks — the EOS commitment unit.

Owner is a Person, scope is one Quarter. Visibility controls who sees the
rock in the UI (company-wide, department-only, or owner-only).

A Rock has a current `status` field (cached "latest known state" for fast
list queries) plus a history of dated `RockWeeklyUpdate` rows. Owners log a
weekly update with REQUIRED status + percent_complete and an
optional comment, so we can scroll the history per rock ("three weeks ago
this was off-track at 40%, now it's on-track at 80%"). The cached Rock
status is updated server-side whenever a new RockWeeklyUpdate is recorded so
existing list/aggregate queries keep working.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from rubik_core.database.models import Base

from ._base import AuditMixin, RockStatus, Visibility


class Rock(Base, AuditMixin):
    __tablename__ = "eos_rocks"

    quarter_id = Column(sa.Uuid, ForeignKey("eos_quarters.id"), nullable=False)
    owner_id = Column(sa.Uuid, ForeignKey("eos_people.id"), nullable=False)
    description = Column(Text, nullable=False)
    status = Column(
        Enum(
            RockStatus,
            name="eos_rock_status",
            values_callable=lambda x: [e.value for e in x],
        ),
        default=RockStatus.ON_TRACK,
        nullable=False,
    )
    visibility = Column(
        Enum(
            Visibility,
            name="eos_visibility",
            values_callable=lambda x: [e.value for e in x],
        ),
        default=Visibility.DEPARTMENT,
        nullable=False,
    )
    # Exec-team ("ET") rock. When true, the rock is visible to Executive-team
    # members ONLY (``UserProfile.is_exec``) regardless of ``visibility`` — the
    # visibility enum still scopes among execs, but a non-exec never sees an
    # ET rock (except one they personally own). Mirrors the ``is_exec`` flag on
    # Issue / Todo / ScorecardMetric; see backend/routers/eos/_authz.py.
    is_exec = Column(Boolean, default=False, nullable=False)
    # The team this rock belongs to. Set on DEPARTMENT-scoped rocks so the
    # Rocks page can filter by team ("show Sales rocks" vs "show Exec-team
    # rocks"); NULL for company-wide rocks and Exec-team rocks (which are
    # keyed off ``is_exec``, mirroring Issue / Todo / ScorecardMetric where
    # ``department_name`` is NULL on the cross-department Exec team).
    # A free-text team tag, deliberately NOT a key to ``departments.name``:
    # writers store a team as given (rock create keeps the team the client
    # sends; promote-to-EOS falls back to the caller's ``users.department``,
    # which no ``departments`` row has to match), and removing a department
    # must not clear the tag. The live schema never had a key here either.
    department_name = Column(String, nullable=True)
    # Milestones USED to live here as a JSON array of
    # ``{"description": str, "completed": bool, "due_date": ..., "weight"?: ...}``.
    # They were promoted to the rock-linked ``eos_rock_milestones`` table (see
    # ``RockMilestone`` below + the ``milestones`` relationship). This column is
    # KEPT IN PLACE as the migration backfill source and for rollback safety,
    # but is no longer written to by the API. The Python attribute is renamed to
    # ``milestones_json`` so the ``milestones`` name can carry the relationship;
    # the DB column is still named "milestones" (no data migration of the column
    # itself).
    milestones_json = Column("milestones", JSON, nullable=True)

    quarter = relationship("Quarter", back_populates="rocks")
    updates = relationship(
        "RockWeeklyUpdate",
        back_populates="rock",
        order_by="RockWeeklyUpdate.update_date.desc()",
        cascade="all, delete-orphan",
    )
    # Ordered milestones, promoted out of the legacy JSON column into
    # rock-linked child rows. Ordered by ``sort_order``; delete-orphan so a
    # whole-array-replace PATCH from the editor reconciles cleanly.
    milestones = relationship(
        "RockMilestone",
        back_populates="rock",
        order_by="RockMilestone.sort_order",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_eos_rocks_quarter_status", "quarter_id", "status"),
        Index("ix_eos_rocks_owner", "owner_id"),
        Index("ix_eos_rocks_department_name", "department_name"),
    )


class RockWeeklyUpdate(Base, AuditMixin):
    """One weekly status update on a Rock.

    Status + percent_complete are REQUIRED at the DB and API level so
    owners can't half-fill their L10 prep. The `comment` carries free-form
    notes tied to the `update_date`, which is what the rock detail page
    renders as a reverse-chronological history.
    """

    __tablename__ = "eos_rock_updates"

    rock_id = Column(
        sa.Uuid, ForeignKey("eos_rocks.id", ondelete="CASCADE"), nullable=False
    )
    # Its own Postgres type, separate from ``eos_rocks.status``'s: the weekly
    # updates table was added later and got the default type name.
    status = Column(
        Enum(
            RockStatus,
            name="rockstatus",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
    )
    percent_complete = Column(Integer, nullable=False)
    comment = Column(Text, nullable=True)
    update_date = Column(Date, nullable=False)

    rock = relationship("Rock", back_populates="updates")

    __table_args__ = (
        CheckConstraint(
            "percent_complete >= 0 AND percent_complete <= 100",
            name="ck_eos_rock_updates_percent_range",
        ),
        Index("ix_eos_rock_updates_rock_date", "rock_id", "update_date"),
    )


class RockMilestone(Base, AuditMixin):
    """One milestone on a Rock.

    Promoted out of the legacy ``eos_rocks.milestones`` JSON column into a
    proper rock-linked table so milestones are persistent, individually
    addressable (stable ``id``), and orderable. ``sort_order`` is the display
    position within the rock; the API treats the incoming milestone array as
    authoritative and reconciles child rows against it (whole-array replace).

    The API response shape is unchanged: ``RockResponse.milestones`` still emits
    an inline list of ``{description, completed, due_date, ...}`` — now sourced
    from these rows (plus the new ``id`` / ``sort_order``).
    """

    __tablename__ = "eos_rock_milestones"

    rock_id = Column(
        sa.Uuid, ForeignKey("eos_rocks.id", ondelete="CASCADE"), nullable=False
    )
    description = Column(Text, nullable=False)
    completed = Column(Boolean, default=False, nullable=False)
    due_date = Column(Date, nullable=True)
    sort_order = Column(Integer, nullable=False)

    rock = relationship("Rock", back_populates="milestones")

    __table_args__ = (Index("ix_eos_rock_milestones_rock", "rock_id"),)


class RockCarryover(Base, AuditMixin):
    """A Rock carried into a later quarter — the link from the old to the new.

    EOS practice at quarter end: an unfinished Rock is either dropped or
    carried into the next quarter, usually re-dated and re-scoped. Carrying
    one over CREATES A NEW Rock in the target quarter; the source Rock is left
    exactly as it was (its quarter, status, milestones and weekly-update
    history are the record of what happened). This row ties the two together
    so each side can say "carried over to Q1 2027" / "carried over from Q4
    2026".

    A separate table, not a column on ``eos_rocks``: an ALTER TABLE on a live
    EOS table at container boot can take the service down (see
    ``backend/tests/test_migration_lock_safety.py``). For the same reason
    neither column has a foreign key — a key to ``eos_rocks`` would lock it
    while a migration creates this table.

    A Rock has at most ONE live carry-over: a link counts only while BOTH rocks
    are live (``is_deleted`` False), so deleting the carried Rock frees the
    source to be carried again. ``carried_rock_id`` is unique because a Rock
    is born from at most one carry-over.
    """

    __tablename__ = "eos_rock_carryovers"

    source_rock_id = Column(sa.Uuid, nullable=False)
    carried_rock_id = Column(sa.Uuid, nullable=False, unique=True)

    __table_args__ = (Index("ix_eos_rock_carryovers_source", "source_rock_id"),)

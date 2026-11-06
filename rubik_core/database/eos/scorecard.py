"""Weekly Scorecard — Metrics + Entries.

`ScorecardMetric` corresponds to upstream `Measurable`; renamed to match the
table name. Each metric has many weekly entries.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import relationship

from rubik_core.database.models import Base

from ._base import AuditMixin


class ScorecardMetric(Base, AuditMixin):
    __tablename__ = "eos_scorecard_metrics"

    owner_id = Column(sa.Uuid, ForeignKey("eos_people.id"), nullable=False)
    department_name = Column(
        String, ForeignKey("departments.name", ondelete="SET NULL"), nullable=True
    )
    # Team-scoping — the Exec/Leadership team's metrics (cross-department)
    # are marked here since ``department_name`` is FK-constrained and can't hold
    # a sentinel. A team's L10 shows that team's metrics.
    is_exec = Column(Boolean, default=False, nullable=False, server_default=sa.false())
    name = Column(String, nullable=False)
    # Free-text explanation of what the measurable tracks / how it's counted.
    # Surfaced as a hover tooltip on the grid so the title can stay short.
    description = Column(Text)
    goal_description = Column(Text)
    target_value = Column(String)
    unit = Column(String)
    # Leadership Scorecard format: how to render a weekly value and
    # which direction the goal points, so the grid can show "$34,300" / "94.95%"
    # / "8" and colour each cell green/red against goal.
    #   value_type — "currency" | "percent" | "number" (default "number").
    #   comparator — ">" | "<" | ">=" | "<=" | "=" ; NULL = no goal comparison.
    value_type = Column(
        String, default="number", nullable=False, server_default=sa.text("'number'")
    )
    comparator = Column(String)
    frequency = Column(String, default="weekly", nullable=False)
    on_track = Column(Boolean, default=True, nullable=False)
    # Display position within an owner's block on the Scorecard grid. NULL until
    # the team drags a measurable for the first time; the grid falls back to
    # alphabetical for NULLs, so the order is stable before anyone reorders and
    # explicit after. Scoped per owner — two owners' blocks can both start at 0.
    sort_order = Column(sa.Integer, nullable=True)

    entries = relationship(
        "ScorecardEntry",
        back_populates="metric",
        cascade="all, delete-orphan",
    )


class ScorecardEntry(Base, AuditMixin):
    __tablename__ = "eos_scorecard_entries"

    metric_id = Column(
        sa.Uuid,
        ForeignKey("eos_scorecard_metrics.id", ondelete="CASCADE"),
        nullable=False,
    )
    week_start_date = Column(DateTime, nullable=False)
    value = Column(String)
    # Short annotation on this one cell — what the number is made of ("3
    # renewals, 2 upsells"). The spreadsheet cell comment, back; distinct from
    # ScorecardMetric.description, which explains the measurable and can't say
    # anything week-specific. One note per cell, editable in place.
    note = Column(Text, nullable=True)

    metric = relationship("ScorecardMetric", back_populates="entries")

    __table_args__ = (
        Index(
            "ix_eos_scorecard_entries_metric_week",
            "metric_id",
            "week_start_date",
            unique=True,
        ),
    )

"""Scorecard metric + weekly entry schemas.

The upstream EOS app calls this `Measurable`; we renamed it to
`ScorecardMetric` to match the table name. The weekly
entries reference the metric via `metric_id` (upstream: `measurable_id`).

The upstream HR-system id is omitted (column dropped pending sync rewrite).
`department_name` is a String FK to `departments.name`.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from .base import AuditBase, BaseCreate, BaseUpdate

# -- ScorecardMetric --------------------------------------------------


class ScorecardMetricBase(BaseModel):
    owner_id: UUID
    department_name: str | None = None
    # Team scope — True iff this belongs to the Exec/Leadership team.
    is_exec: bool = False
    name: str
    # Free-text explanation of the measurable; shown as a grid hover tooltip.
    description: str | None = None
    goal_description: str | None = None
    target_value: str | None = None
    unit: str | None = None
    # Leadership Scorecard format: render hint + goal direction.
    value_type: str = "number"  # currency | percent | number
    comparator: str | None = None  # > < >= <= = ; None = no goal comparison
    frequency: str = "weekly"
    on_track: bool = True
    # Display position within the owner's block; None = not yet reordered (the
    # grid falls back to alphabetical). Set via POST /metrics/reorder.
    sort_order: int | None = None


class ScorecardMetricCreate(ScorecardMetricBase, BaseCreate):
    pass


class ScorecardMetricUpdate(BaseUpdate):
    owner_id: UUID | None = None
    department_name: str | None = None
    name: str | None = None
    description: str | None = None
    goal_description: str | None = None
    target_value: str | None = None
    unit: str | None = None
    value_type: str | None = None
    comparator: str | None = None
    frequency: str | None = None
    on_track: bool | None = None
    sort_order: int | None = None


class ScorecardMetricResponse(ScorecardMetricBase, AuditBase):
    pass


class ScorecardReorderRequest(BaseModel):
    """New display order for one owner's block of measurables.

    ``ordered_ids`` is that owner's measurable ids in the order the team wants
    to see them; the server writes each one's array index to ``sort_order``.
    Ordering is per owner, so the list should hold a single owner's metrics —
    the endpoint enforces edit permission on every id regardless.
    """

    ordered_ids: list[UUID]


# -- ScorecardEntry ---------------------------------------------------


class ScorecardEntryBase(BaseModel):
    metric_id: UUID  # upstream named this measurable_id
    week_start_date: datetime
    value: str | None = None
    # Short annotation on this cell — what the number is made of. See
    # ScorecardEntry.note.
    note: str | None = None


class ScorecardEntryCreate(ScorecardEntryBase, BaseCreate):
    pass


class ScorecardEntryUpdate(BaseUpdate):
    value: str | None = None
    note: str | None = None


class ScorecardEntryResponse(ScorecardEntryBase, AuditBase):
    pass

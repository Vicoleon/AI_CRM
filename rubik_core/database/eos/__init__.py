"""EOS subdomain models.

Importing this package is sufficient to register every `eos_*` table on
`Base.metadata` — Alembic autogenerate and `Base.metadata.create_all()`
will pick them up. `rubik_core.database.models` re-exports `eos` so a
single `from rubik_core.database.models import Base` keeps everything
wired together.

See docs/ENGINEERING_NOTES.md "Package layout" for the rubik_core/ vs backend/
split.
"""

from __future__ import annotations

from ._base import (
    AuditMixin,
    IssuePriority,
    IssueStatus,
    L10InputKind,
    RockStatus,
    TodoStatus,
    Visibility,
    VTOSectionType,
)
from .events import EosEvent
from .issues import Issue, IssueNote
from .l10 import L10Attendance, L10Input, L10Meeting, L10Session, L10Signal
from .organization import Organization
from .people import GWCEvaluation, Person, Seat
from .processes import EosProcess
from .quarters import Quarter
from .rocks import Rock, RockCarryover, RockMilestone, RockWeeklyUpdate
from .scorecard import ScorecardEntry, ScorecardMetric
from .todos import Todo, TodoNote
from .vto import VTOSection, VTOVersion

__all__ = [
    # Mixin & enums
    "AuditMixin",
    "IssuePriority",
    "IssueStatus",
    "L10InputKind",
    "RockStatus",
    "TodoStatus",
    "Visibility",
    "VTOSectionType",
    # Models
    "EosEvent",
    "EosProcess",
    "GWCEvaluation",
    "Issue",
    "IssueNote",
    "L10Attendance",
    "L10Input",
    "L10Meeting",
    "L10Session",
    "L10Signal",
    "Organization",
    "Person",
    "Quarter",
    "Rock",
    "RockCarryover",
    "RockMilestone",
    "RockWeeklyUpdate",
    "ScorecardEntry",
    "ScorecardMetric",
    "Seat",
    "Todo",
    "TodoNote",
    "VTOSection",
    "VTOVersion",
]

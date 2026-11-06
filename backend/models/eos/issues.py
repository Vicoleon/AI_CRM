"""Issue schemas — items raised in L10 for IDS."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from rubik_core.database.eos import IssuePriority, IssueStatus

from .base import AuditBase, BaseCreate, BaseUpdate


def derive_issue_title(description: str | None) -> str:
    """Fallback headline when an issue is created without an explicit title.

    Uses the first line of the description, trimmed to a grid-friendly length.
    Keeps the IDS grid readable for issues raised via paths that don't collect
    a title (rock→IDS, legacy clients) without leaving the row blank.
    """
    text = (description or "").strip()
    if not text:
        return "Untitled issue"
    first_line = text.splitlines()[0].strip() or text
    limit = 120
    if len(first_line) <= limit:
        return first_line
    return first_line[: limit - 1].rstrip() + "…"


class IssueBase(BaseModel):
    owner_id: UUID | None = None
    department_name: str | None = None  # FK to departments.name
    # Team scope — True iff this belongs to the Exec/Leadership team.
    is_exec: bool = False
    l10_meeting_id: UUID | None = None
    # Short headline for the IDS grid. Optional on the wire: when omitted the
    # router derives it from the description, so every stored issue has one.
    title: str | None = None
    description: str
    priority: IssuePriority = IssuePriority.LOW
    status: IssueStatus = IssueStatus.OPEN
    # Soft back-pointer when the issue was spawned from a rock's
    # "Add to IDS" button. Null for issues raised standalone in the L10.
    source_rock_id: UUID | None = None


class IssueCreate(IssueBase, BaseCreate):
    pass


class IssueUpdate(BaseUpdate):
    owner_id: UUID | None = None
    department_name: str | None = None
    title: str | None = None
    description: str | None = None
    priority: IssuePriority | None = None
    status: IssueStatus | None = None


class IssueResponse(IssueBase, AuditBase):
    # Stamped when the issue was solved (NULL while open/archived). Read-only:
    # the router manages it on the status transition, so it's intentionally
    # absent from IssueCreate/IssueUpdate.
    solved_at: datetime | None = None


# Payload for `POST /api/eos/rocks/{rock_id}/ids`. The router
# pulls the source rock from the URL path, resolves the rock owner's
# department to set `department_name`, and stamps `source_rock_id` so
# the Issue carries a back-reference to its origin.


class IDSFromRockCreate(BaseModel):
    description: str
    priority: IssuePriority = IssuePriority.MEDIUM


# -- Issue notes ----------------------------------------------


class IssueNoteCreate(BaseCreate):
    content: str


class IssueNoteResponse(AuditBase):
    issue_id: UUID
    content: str

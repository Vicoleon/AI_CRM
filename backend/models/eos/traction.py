"""Quarter / Rock / Todo schemas — the planning surface.

Rock carries no HR-system id (the column was dropped from the model until
an HR sync exists). `quarter_id` on RockBase is optional in the schema
because the router auto-resolves to the current quarter when omitted — the
underlying column is NOT NULL.

RockWeeklyUpdate is the dated history row: owners post one
per L10 with REQUIRED status + percent_complete plus an optional
comment. Distinct from `RockUpdate` (the PATCH schema), which is the
generic partial-update payload.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from rubik_core.database.eos import RockStatus, TodoStatus, Visibility

from .base import AuditBase, BaseCreate, BaseUpdate

# -- Quarter ----------------------------------------------------------


class QuarterBase(BaseModel):
    organization_id: UUID
    name: str
    start_date: datetime
    end_date: datetime
    is_current: bool = False


class QuarterCreate(QuarterBase, BaseCreate):
    pass


class QuarterUpdate(BaseUpdate):
    name: str | None = None
    start_date: datetime | None = None
    end_date: datetime | None = None
    is_current: bool | None = None


class QuarterResponse(QuarterBase, AuditBase):
    pass


# -- Rock -------------------------------------------------------------


class RockMilestone(BaseModel):
    """Milestone shape. Milestones now persist as rock-linked rows in
    ``eos_rock_milestones`` (see ``rubik_core/database/eos/rocks.py``); this
    model is the inline read/write shape on the Rock API. ``from_attributes``
    lets FastAPI serialize the ORM relationship rows directly into this model.

    ``id`` is the persistent row id — ``None`` on create / for brand-new
    milestones the caller is adding. ``sort_order`` is the display position;
    it's echoed on reads but the server derives it from array position on
    write, so the incoming array order is authoritative.

    ``due_date`` (optional) is when the milestone is targeted for — shown
    in the editor and the Rock's milestone list, and surfaced on the Rocks
    card so a Rock's milestones are visible without opening it.

    ``weight`` is DEPRECATED. Progress is now a plain "completed / total"
    count (the weighted-average the field powered wasn't an intuitive
    concept for the team). The field is kept optional so legacy milestone
    payloads still validate; new milestones leave it null and the UI no
    longer reads or writes it."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID | None = None
    description: str
    completed: bool = False
    due_date: date | None = None
    sort_order: int | None = None
    weight: float | None = None


class RockBase(BaseModel):
    # quarter_id and owner_id are Optional at the schema layer because
    # the create endpoint resolves them from `owner_email` / "current
    # quarter" when the caller omits them. The DB columns are NOT NULL.
    quarter_id: UUID | None = None
    owner_id: UUID | None = None
    description: str
    status: RockStatus = RockStatus.ON_TRACK
    visibility: Visibility = Visibility.DEPARTMENT
    # Exec-team ("ET") rock — visible to Executive-team members only,
    # regardless of ``visibility``. See rubik_core/database/eos/rocks.py and
    # the exec gating in backend/routers/eos/_authz.py:visible_rock_ids.
    is_exec: bool = False
    # The team this rock belongs to (department name). Set for
    # DEPARTMENT-scoped rocks; NULL for company-wide and Exec-team rocks.
    # Drives the Rocks-page team filter.
    department_name: str | None = None
    # Ordered list of milestones. Stored as JSON on the rocks
    # table. ``None`` for legacy rocks that pre-date milestones — the UI
    # renders an empty placeholder in that case.
    milestones: list[RockMilestone] | None = None


class RockCreate(RockBase, BaseCreate):
    owner_email: str | None = None  # ergonomic shortcut; router resolves → owner_id
    # Choose the period a rock is for. When provided (1..4 + year),
    # the router find-or-creates the matching calendar Quarter and links it,
    # overriding the "current quarter" default. Request-only — not columns.
    quarter_year: int | None = None
    quarter_number: int | None = Field(default=None, ge=1, le=4)


class RockUpdate(BaseUpdate):
    owner_id: UUID | None = None
    description: str | None = None
    status: RockStatus | None = None
    visibility: Visibility | None = None
    is_exec: bool | None = None
    department_name: str | None = None
    milestones: list[RockMilestone] | None = None


class RockResponse(RockBase, AuditBase):
    # Whether the CALLER of this request may PATCH/DELETE this rock — the
    # row-level ``can_mutate`` verdict, computed per request and never stored.
    # The read gate is wider than the write gate (a whole team sees its team's
    # rocks; only the owner / creator / owner's manager / team leadership may
    # change one), so without this the UI showed everyone an Edit button and
    # the save came back 403. Defaults True: a path that forgets to stamp it
    # leaves the controls visible and lets the server do the refusing, rather
    # than silently hiding an action someone is entitled to.
    can_edit: bool = True
    # Deleting is NOT the same grant: any member of the Rock's team may update
    # it (the L10 Rock Review is a team activity), but removing the team's
    # commitment stays with the owner, the creator, the owner's manager and the
    # team's leadership — so the Delete control follows this flag, not can_edit.
    can_delete: bool = True
    # Carry-over links (``eos_rock_carryovers``), transient like the flags
    # above: computed per request, never stored on the rock. ``carried_from_*``
    # is set on a rock that was born from a carry-over, ``carried_to_*`` on the
    # rock it was carried from. A link shows only while BOTH rocks are live, so
    # deleting the carried rock clears both sides and frees the source to be
    # carried again.
    carried_from_rock_id: UUID | None = None
    carried_from_quarter_id: UUID | None = None
    carried_from_quarter_name: str | None = None
    carried_to_rock_id: UUID | None = None
    carried_to_quarter_id: UUID | None = None
    carried_to_quarter_name: str | None = None


# Payload for ``POST /api/eos/rocks/{rock_id}/carry-over``: the carried rock as
# the user edited it in the carry-over dialog. Carrying over creates a NEW rock
# in the target quarter and leaves the source untouched, so this is a create
# payload; every field the dialog leaves out falls back to the source rock.


class RockCarryOverCreate(BaseCreate):
    description: str
    # Same resolution as RockCreate: owner_email wins, then owner_id; with
    # neither, the source rock's owner keeps it.
    owner_id: UUID | None = None
    owner_email: str | None = None
    # A carried rock starts the new quarter on track, whatever the old one was.
    status: RockStatus = RockStatus.ON_TRACK
    # None = keep the source rock's value.
    visibility: Visibility | None = None
    is_exec: bool | None = None
    # Left out = the source rock's team; sent (even as null) = that value. The
    # router tells the two apart with ``model_fields_set``.
    department_name: str | None = None
    # Always inserted as NEW milestones on the new rock: ids are ignored, so a
    # source milestone is never moved off the rock it belongs to.
    milestones: list[RockMilestone] | None = None
    # The quarter to carry into (both or neither). Omitted = the default
    # target: the quarter after the source's, or today's if that is later.
    # The year is bounded so a typo answers 422, not a datetime error (500).
    quarter_year: int | None = Field(default=None, ge=2000, le=2100)
    quarter_number: int | None = Field(default=None, ge=1, le=4)


# Payload for ``POST /api/eos/rocks/{rock_id}/carry-over/ai-draft``: which
# quarter to re-date the rock for (omitted = the default target, as above).
# The response reuses ``RockAIDraftResponse``.


class RockCarryOverAIDraftRequest(BaseModel):
    quarter_year: int | None = Field(default=None, ge=2000, le=2100)
    quarter_number: int | None = Field(default=None, ge=1, le=4)


# Payload for ``POST /api/eos/rocks/ai-draft``. The user supplies
# a rough goal string; the endpoint calls Gemini and returns a
# structured draft (SMART rock title + 3–5 milestones).


class RockAIDraftRequest(BaseModel):
    goal: str


class RockAIDraftResponse(BaseModel):
    description: str
    milestones: list[RockMilestone]


# -- Rock weekly update ----------------------------------------
#
# A dated, REQUIRED status + percent_complete snapshot of a Rock. One
# per L10 per rock. Creating a new update also bumps the cached
# `rocks.status` field server-side so existing list/aggregate queries
# keep working without touching every consumer.


class RockWeeklyUpdateBase(BaseModel):
    # status and percent_complete are REQUIRED at the schema level so
    # the server returns 422 before the DB CHECK constraint kicks in.
    status: RockStatus
    percent_complete: int = Field(ge=0, le=100)
    comment: str | None = None


class RockWeeklyUpdateCreate(RockWeeklyUpdateBase, BaseCreate):
    # rock_id arrives in the URL path; not part of the body schema.
    # update_date defaults to "today" server-side when the caller
    # omits it (the common case — owners filling Monday before L10).
    update_date: date | None = None


class RockWeeklyUpdateResponse(RockWeeklyUpdateBase, AuditBase):
    rock_id: UUID
    update_date: date


# -- Todo -------------------------------------------------------------


# A To-Do is one small task, and every surface renders it on a line or two.
# Nothing enforced that, so paragraphs arrived from the paths that mint a to-do
# out of prose — an Issue body, a Slack `/todo` ramble, a meeting-prep idea —
# and the list clipped them with no detail view to recover the rest from.
# 120 matches `derive_issue_title`'s limit and the frontend's TODO_MAX_LEN, so
# Issues and To-Dos agree on what "one line" means. Longer input belongs in an
# Issue, which has a body and a detail modal.
TODO_TEXT_LIMIT = 120


def truncate_todo_text(text: str | None) -> str:
    """First line of ``text``, ellipsised to :data:`TODO_TEXT_LIMIT`.

    Mirrors :func:`backend.models.eos.issues.derive_issue_title` so a to-do
    derived from the same prose reads the same as the issue beside it. Applied
    on the write paths only — never to a response, so a pre-cap row keeps its
    full text and the UI's clamp-plus-tooltip can still reveal it.
    """
    stripped = (text or "").strip()
    if not stripped:
        return stripped
    first_line = stripped.splitlines()[0].strip() or stripped
    if len(first_line) <= TODO_TEXT_LIMIT:
        return first_line
    return first_line[: TODO_TEXT_LIMIT - 1].rstrip() + "…"


class TodoBase(BaseModel):
    owner_id: UUID | None = None
    rock_id: UUID | None = None
    # Scrum-style back-pointer to the Issue this to-do was spun off from
    # (see rubik_core/database/eos/todos.py). Set server-side by the
    # issue→todo endpoint; surfaced so the UI can render "from issue …".
    source_issue_id: UUID | None = None
    # Team scope — department team (``department_name``) or the
    # Exec/Leadership team (``is_exec=True``).
    department_name: str | None = None
    is_exec: bool = False
    description: str
    due_date: datetime | None = None
    status: TodoStatus = TodoStatus.PENDING


class TodoCreate(TodoBase, BaseCreate):
    owner_email: str | None = None
    l10_meeting_id: UUID | None = None

    @field_validator("description")
    @classmethod
    def _cap_description(cls, v: str) -> str:
        return truncate_todo_text(v)


class TodoUpdate(BaseUpdate):
    # owner_id is accepted so re-assigning a to-do actually sticks. Before,
    # the edit form PATCHed owner_id but the field was absent here, so
    # model_dump(exclude_unset=True) dropped it and reassignment silently
    # no-op'd. rock_id likewise editable for completeness.
    owner_id: UUID | None = None
    rock_id: UUID | None = None
    description: str | None = None
    due_date: datetime | None = None
    status: TodoStatus | None = None

    @field_validator("description")
    @classmethod
    def _cap_description(cls, v: str | None) -> str | None:
        return truncate_todo_text(v) if v is not None else None


class TodoResponse(TodoBase, AuditBase):
    pass


class TodoFromIssueCreate(BaseModel):
    """Payload for ``POST /api/eos/issues/{issue_id}/todos`` — spin a To-Do
    off an Issue (Scrum story→task).

    ``description`` defaults to the issue's own text when omitted. The
    owner resolves in order: ``owner_email`` → ``owner_id`` → the issue's
    ``owner_id`` → the caller's own Person (Todo.owner_id is NOT NULL, so
    one of these must land). ``department_name``/``is_exec`` are copied
    from the parent issue server-side so the to-do stays on the same team.
    """

    description: str | None = None
    owner_email: str | None = None
    owner_id: UUID | None = None
    due_date: datetime | None = None


# -- To-Do notes ------------------------------------------------------
# Progress / blocker notes on a To-Do. Mirrors the Issue-notes schemas
# (backend/models/eos/issues.py) so both threads share one frontend view.


class TodoNoteCreate(BaseCreate):
    content: str


class TodoNoteResponse(AuditBase):
    todo_id: UUID
    content: str

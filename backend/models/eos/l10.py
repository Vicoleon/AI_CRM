"""L10 meeting / session / attendance schemas.

`L10SessionResponse.is_manager` is a derived field set by the router
after model_validate — the manager check requires a Person lookup
that the schema layer doesn't do on its own.

`L10Input*` carries pre-meeting headlines + ideas. Kind is
the discriminator ('headline' | 'idea').
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from rubik_core.database.eos import IssuePriority, L10InputKind

from .base import AuditBase, BaseCreate, BaseUpdate
from .issues import IssueResponse
from .traction import TodoResponse

# -- Meeting ----------------------------------------------------------


class L10MeetingBase(BaseModel):
    department_name: str | None = None  # FK to departments.name
    manager_id: UUID | None = None
    name: str
    day_of_week: int | None = None  # 0=Mon..6=Sun
    start_time: str | None = None  # "HH:MM"
    duration_minutes: int = 90
    # True iff this meeting is the cross-department Exec leadership L10.
    is_exec_meeting: bool = False
    # Configurable per-meeting link slot for transient initiatives.
    # Both nullable but they ride together — the frontend treats the link
    # as visible only when both label + url are populated. A non-empty
    # label without a url (or vice versa) is allowed at the DB layer but
    # discouraged by the create modal.
    secondary_link_label: str | None = None
    secondary_link_url: str | None = None
    # Roster of attendee emails copied from the imported
    # Google Calendar event. Empty / null for manually-scheduled meetings.
    roster_emails: list[str] | None = None
    # Source Google Calendar event id, set when the meeting was imported
    # from a calendar event. Lets re-imports upsert.
    google_event_id: str | None = None
    # Calendar date of the specific occurrence most recently imported.
    # Informational only — day_of_week/start_time remain the recurring
    # pattern used for scheduling; this tells the UI which occurrence a
    # (re-)import last refreshed.
    last_imported_date: date | None = None
    # Reconciliation against the live Google Calendar (see
    # rubik_core/eos/calendar_sync.py). ``calendar_state`` is "active" |
    # "cancelled"; None means nobody has been able to ask yet, which readers
    # must treat as "no answer", never as a cancellation.
    calendar_state: str | None = None
    next_occurrence_at: datetime | None = None
    calendar_checked_at: datetime | None = None


class L10MeetingCreate(L10MeetingBase, BaseCreate):
    pass


class L10MeetingUpdate(BaseUpdate):
    name: str | None = None
    manager_id: UUID | None = None
    department_name: str | None = None
    day_of_week: int | None = None
    start_time: str | None = None
    duration_minutes: int | None = None
    is_exec_meeting: bool | None = None
    secondary_link_label: str | None = None
    secondary_link_url: str | None = None
    roster_emails: list[str] | None = None


class L10ManagerTransferRequest(BaseModel):
    """Payload for ``POST /api/eos/l10/{id}/transfer-manager``.

    A deliberately one-field body. The full ``PATCH`` can also change the
    manager, but it requires standing authority over the meeting; this endpoint
    exists so a team MEMBER can take an abandoned meeting over when its manager
    is away — and it must not become a way to rename, reschedule, or re-scope
    the meeting at the same time.

    ``new_manager_id`` of None clears the manager, which only standing authority
    may do (break-glass requires the caller name themselves).
    """

    new_manager_id: UUID | None = None
    # Free-text "why" recorded on the audit line. Optional: the reason a
    # break-glass takeover is allowed is derived from data, never from what the
    # claimant types, so this is context for humans reading the log — not part
    # of the authorization decision.
    note: str | None = None


# Payload for ``POST /api/eos/l10/calendar-import``.
# Identifies one Google Calendar event by id; server fetches its
# details and creates / upserts a full L10Meeting row.


class L10CalendarImportRequest(BaseModel):
    google_event_id: str


class L10MeetingResponse(L10MeetingBase, AuditBase):
    # Derived (populated by the list endpoint): the id of an in-progress
    # session for this meeting, if any (actual_end IS NULL). Lets the UI offer
    # "Rejoin" for a live meeting regardless of its scheduled weekday.
    live_session_id: UUID | None = None


class L10CalendarStatus(BaseModel):
    """What Google Calendar currently says about one imported L10.

    Served by ``GET /api/eos/l10/calendar-status`` as a SEPARATE call from the
    meetings list on purpose: it costs a Google round-trip per meeting, and the
    board must paint without waiting on a third party that can be slow or down.
    The list still carries the stored fields above; this refreshes them.
    """

    meeting_id: UUID
    #: "active" | "cancelled" | "unknown". Unknown means we could not ask —
    #: the client keeps showing the stored schedule rather than claiming a
    #: cancellation.
    state: str
    next_occurrence_at: datetime | None = None
    checked_at: datetime | None = None
    #: True when this answer was written back to the shared row (the caller is
    #: the meeting's manager, i.e. the calendar series' organizer).
    persisted: bool = False
    #: Plain-language "why", for the tooltip and the logs: on "unknown", what
    #: stopped us asking; on "cancelled", whether the event was deleted or
    #: simply has nothing scheduled in the look-ahead.
    reason: str | None = None
    #: Stable discriminator for a "cancelled" state — "event_deleted" or
    #: "nothing_scheduled". The client branches on this, never on ``reason``,
    #: so rewording the prose can't change what the card says.
    kind: str | None = None


class L10CalendarStatusResponse(BaseModel):
    calendar_available: bool
    statuses: list[L10CalendarStatus]


# -- Session ----------------------------------------------------------


class L10SessionBase(BaseModel):
    meeting_id: UUID
    actual_start: datetime
    actual_end: datetime | None = None
    average_rating: int | None = None
    notes: str | None = None


class L10SessionCreate(L10SessionBase, BaseCreate):
    pass


class L10SessionResponse(L10SessionBase, AuditBase):
    is_manager: bool = False
    # Live-session cursor (server-authoritative). Lets a (re)joining client
    # restore the real step + timer anchor instead of starting at step 0.
    current_step: int = 0
    step_started_at: datetime | None = None
    is_paused: bool = False
    state_version: int = 0
    # Cached AI recap of a finished session (null until generated). The
    # diary reads it inline; a lazy GET /sessions/{id}/recap fills it on demand.
    recap: str | None = None


class L10RecapResponse(BaseModel):
    """AI narrative recap of a concluded L10 session.

    ``recap`` is null when the feature flag (``CUBIE_L10_AI_ENABLED``) is off;
    otherwise it carries the cached-or-freshly-generated narrative. Kept as a
    thin standalone model (rather than reusing the full session response) so the
    lazy GET / manager regenerate endpoints return only the payload they own.
    """

    recap: str | None = None


# -- Live session state (the shared cursor, polled every ~2s) ----------


class L10SessionState(BaseModel):
    """Lean shared-state payload polled by every participant.

    The timer is delivered as an anchor (``step_started_at``) plus the
    server's clock (``server_now``); clients compute ``remaining`` locally as
    ``duration - (server_now - step_started_at)`` and self-correct for clock
    skew on each poll. ``manager_id`` lets a client know who drives the
    cursor (and recompute its own manager status, so a handed-off facilitator
    gains control within one tick). ``attendance_rev`` / ``items_rev`` are
    cheap change-markers (max updated_at) — when one differs from the value a
    client last saw, it refetches just that list, so a peer's check-in or rock
    toggle surfaces without a manual refresh. ``actual_end`` non-null tells a
    stale tab the meeting is over. ``actual_start`` is the meeting-level clock
    anchor (what the master pace bar measures the 90-minute box against); it
    normally never moves, but a Restart re-anchors it, so it travels with the
    cursor to keep a tab that's already in the room from holding a stale one.
    """

    session_id: UUID
    current_step: int
    actual_start: datetime
    step_started_at: datetime | None = None
    is_paused: bool = False
    state_version: int = 0
    manager_id: UUID | None = None
    server_now: datetime
    actual_end: datetime | None = None
    attendance_rev: datetime | None = None
    items_rev: datetime | None = None


class L10StepAdvance(BaseModel):
    """Manager-only step/timer write.

    ``current_step`` is the agenda index to move everyone to. Omit
    ``step_started_at`` to re-anchor the timer to "now" (the normal Next /
    jump-to-step case); pass an explicit anchor only for special cases.
    """

    current_step: int
    step_started_at: datetime | None = None


# -- Attendance -------------------------------------------------------


class L10AttendanceBase(BaseModel):
    session_id: UUID
    person_id: UUID
    rating: int | None = None  # Conclude per-person meeting rating, 1..10
    # Optional per-person survey comment submitted alongside the rating.
    rating_comment: str | None = None
    attended: bool = True
    # Structured Check-in / Segue payload. Personal + Professional are stored
    # separately so a person's two entries coexist (no overwrite). Each is a
    # nullable content line + 0..10 rating; a field left null means "no change"
    # on upsert (the router only writes fields the caller actually sent).
    checkin_personal_content: str | None = None
    checkin_personal_rating: int | None = None  # 0..10
    checkin_professional_content: str | None = None
    checkin_professional_rating: int | None = None  # 0..10


class L10AttendanceCreate(L10AttendanceBase, BaseCreate):
    pass


class L10AttendanceResponse(L10AttendanceBase, AuditBase):
    pass


# -- L10 Input (Headlines + Ideas) ------------------------------


class L10InputBase(BaseModel):
    meeting_id: UUID
    kind: L10InputKind
    text: str
    discussed: bool = False


class L10InputCreate(L10InputBase, BaseCreate):
    pass


class L10InputUpdate(BaseUpdate):
    text: str | None = None
    discussed: bool | None = None


class L10InputResponse(L10InputBase, AuditBase):
    pass


# -- L10 Input conversion (idea → IDS Issue / To-Do) ------------------


class L10InputConvertRequest(BaseModel):
    """Payload for ``POST /api/eos/l10/inputs/{input_id}/convert`` — promote a
    prep idea into an IDS Issue or a To-Do.

    ``target`` picks the destination. The idea's text becomes the new item's
    description; team scope (``department_name`` / ``is_exec``) and the
    ``l10_meeting_id`` back-pointer are derived server-side from the source
    idea's meeting so the spawned item lands on the same team — callers can't
    override them. For a To-Do an owner is required (``Todo.owner_id`` is NOT
    NULL); it resolves ``owner_email`` → ``owner_id`` → the caller's own
    Person. ``priority`` applies to Issues; ``due_date`` to To-Dos.
    ``mark_discussed`` (default True) flags the source idea as handled so it
    drops out of the live queue without being deleted (kept for history).
    """

    target: Literal["issue", "todo"]
    owner_id: UUID | None = None
    owner_email: str | None = None
    priority: IssuePriority = IssuePriority.MEDIUM
    due_date: datetime | None = None
    mark_discussed: bool = True


class L10InputConvertResponse(BaseModel):
    """Result of a convert: the created item plus the (now-updated) source
    idea, so the client can both link to the new Issue/To-Do and reflect the
    idea's ``discussed`` flag without a refetch."""

    target: Literal["issue", "todo"]
    issue: IssueResponse | None = None
    todo: TodoResponse | None = None
    input: L10InputResponse


# -- L10 Signal (sync — ephemeral in-meeting broadcasts) -----------------


class L10SignalCreate(BaseCreate):
    # Free-form discriminator so future broadcast events ("user_joined",
    # "manager_paused") don't need another migration / schema bump.
    kind: str


class L10SignalResponse(BaseModel):
    """Lean signal payload — no audit cruft. Polled by every session
    participant; keep the wire format tight so 2s polling is cheap."""

    id: UUID
    session_id: UUID
    kind: str
    created_at: datetime
    created_by: str | None = None

    model_config = ConfigDict(from_attributes=True)

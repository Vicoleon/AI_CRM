"""EOS L10 — recurring meetings, sessions, attendance, and aggregated items.

Mounted at `/api/eos/l10`. Surface mirrors the upstream EOS app:
- `GET/POST /` for meetings
- `GET /sessions`, `POST /sessions/start`, `POST /sessions/{id}/end`
- `POST /sessions/{id}/attendance` (upsert)
- `GET /meetings/{id}/items` — aggregated dashboard data (OPEN issues only)
- `GET /meetings/{id}/issues/history` — the team's recently solved issues
- `POST /sessions/{id}/rollover` — pending-item counts for the next session

Two upstream-bug fixes baked in:
1. Rollover counts use the typed enum (`TodoStatus.PENDING`, `IssueStatus.OPEN`)
   instead of upstream's uppercase string literals (`"PENDING"`, `"OPEN"`).
   Our enum values are lowercase per the model — the upstream string
   comparisons would never match.
2. `get_meeting_items` traversed `meeting.department.organization_id`,
   but EOS no longer owns departments — Cubie's `departments` table has
   no `organization_id`. Replaced with "the only organization" lookup,
   which is correct for a single-tenant deployment. Revisit for multi-tenancy.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from starlette.concurrency import run_in_threadpool

from backend.jobs.eos_ingest_hooks import (
    enqueue_issue_ingest,
    enqueue_l10_session_ingest,
)
from backend.jobs.launcher import reject_support_background_work
from backend.models.eos.issues import IssueResponse, derive_issue_title
from backend.models.eos.l10 import (
    L10AttendanceCreate,
    L10AttendanceResponse,
    L10CalendarImportRequest,
    L10CalendarStatus,
    L10CalendarStatusResponse,
    L10InputConvertRequest,
    L10InputConvertResponse,
    L10InputCreate,
    L10InputResponse,
    L10InputUpdate,
    L10ManagerTransferRequest,
    L10MeetingCreate,
    L10MeetingResponse,
    L10MeetingUpdate,
    L10RecapResponse,
    L10SessionCreate,
    L10SessionResponse,
    L10SessionState,
    L10SignalCreate,
    L10SignalResponse,
    L10StepAdvance,
)
from backend.models.eos.traction import TodoResponse, truncate_todo_text
from backend.routers.eos._authz import (
    _person_ids_in_depts,
    accessible_meeting_ids,
    can_manage_meeting,
    meeting_authority,
    person_by_email,
    require_exec_scope_allowed,
    require_meeting_access,
)
from backend.routers.eos._manager_access import (
    authorize_manager_transfer,
    manager_availability,
)
from backend.routers.eos._shared import CurrentUser, DBDep, team_scope_filter
from rubik_core.ai_providers.registry import (
    IncompatibleModelRoute,
    resolve_model_route,
)
from rubik_core.ai_providers.runtime import generate_text_on_route
from rubik_core.config import settings
from rubik_core.cost.usage import attribute_to
from rubik_core.database.eos import (
    Issue,
    IssueStatus,
    L10Attendance,
    L10Input,
    L10InputKind,
    L10Meeting,
    L10Session,
    L10Signal,
    Person,
    Quarter,
    Rock,
    RockWeeklyUpdate,
    ScorecardMetric,
    Todo,
    TodoStatus,
)
from rubik_core.database.models import User
from rubik_core.database.schemas import ALL_DEPARTMENTS
from rubik_core.eos.session_lifecycle import (
    close_abandoned_sessions,
    live_session_ids,
)
from rubik_core.rag.config import slugify_department

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/l10", tags=["EOS: L10 Meetings"])


def _require_department_scope(current_user, target_department: str) -> None:
    """Raise 403 unless the caller may point an L10 at ``target_department``.

    Shared by create and by the department-move branch of PATCH so both agree on
    who owns which team's cadence:
      * admin → any department;
      * org-wide exec (``*`` in managed_departments, i.e. the CEO) → any;
      * exec → their own department plus every department they manage;
      * everyone else → their own department only.

    Case-insensitive: a department's identity is its slug, so "Sales" and "sales"
    are the same team and a raw ``!=`` here would reject a legitimate move.
    """
    if "admin" in (current_user.roles or []):
        return
    is_exec = bool(getattr(current_user, "is_exec", False))
    managed = list(getattr(current_user, "managed_departments", None) or [])
    if is_exec and ALL_DEPARTMENTS in managed:
        return

    own_dept = getattr(current_user, "department", None)
    allowed = {own_dept, *managed} if is_exec else {own_dept}
    allowed.discard(None)
    if target_department.strip().lower() in {a.strip().lower() for a in allowed}:
        return
    raise HTTPException(
        status_code=403,
        detail=(
            f"You can only schedule L10s for: {sorted(allowed) or '(none)'} "
            "— ask an admin to add to your managed_departments if you "
            "need to schedule for another team."
        ),
    )


# -- Meetings ---------------------------------------------------------


@router.get("/", response_model=list[L10MeetingResponse])
def list_l10_meetings(
    db: DBDep,
    current_user: CurrentUser,
    department_name: str | None = None,
):
    query = db.query(L10Meeting).filter(L10Meeting.is_deleted == False)  # noqa: E712
    # Team confidentiality: only meetings the caller's teams can access.
    # The Exec L10 is visible to Exec team members ONLY; a department L10
    # to its members / manager / roster (see _authz.can_access_meeting).
    # Membership-scoped for everyone (admins/CEO included): nobody is shown
    # another team's L10 — see _authz.accessible_meeting_ids.
    query = query.filter(L10Meeting.id.in_(accessible_meeting_ids(current_user, db)))
    if department_name:
        query = query.filter(
            func.lower(L10Meeting.department_name) == department_name.lower()
        )
    meetings = query.all()
    if not meetings:
        return []

    # Annotate each meeting with an in-progress session id (if any) so the UI
    # can offer "Rejoin" for a live meeting regardless of weekday. Bulk-keyed
    # on the returned ids — no N+1 (one join, plus one aggregate per activity
    # signal, and only when some session is actually open).
    #
    # "In progress" is NOT the same as "open". A session stays open until a
    # facilitator clicks Conclude and almost nobody does, so a bare
    # ``actual_end IS NULL`` made the board advertise Rejoin for meetings that
    # had ended weeks earlier. ``live_session_ids`` applies the shared
    # abandonment rule (past its box AND hours without a write — see
    # rubik_core/eos/session_lifecycle.py) so a dead session drops off the
    # board the moment it goes stale, without waiting for the nightly sweep
    # to stamp ``actual_end``. This stays a pure read: the sweep and
    # ``/sessions/start`` are the paths that actually close rows.
    live_by_meeting = live_session_ids(db, [m.id for m in meetings])

    out: list[L10MeetingResponse] = []
    for m in meetings:
        resp = L10MeetingResponse.model_validate(m)
        resp.live_session_id = live_by_meeting.get(m.id)
        out.append(resp)
    return out


@router.post(
    "/", response_model=L10MeetingResponse, status_code=status.HTTP_201_CREATED
)
def create_l10_meeting(
    db: DBDep, current_user: CurrentUser, meeting_in: L10MeetingCreate
):
    """Schedule a new L10 meeting.

    Authorization:
    - Admins: any scope.
    - Exec users (``current_user.is_exec``): the Exec team, their own
      ``department``, or any department in their ``managed_departments``
      list (EOS Accountability Chart — e.g. a CTO managing both
      Engineering and Systems).
    - Regular users: only their own ``department``. They can NOT
      schedule the Exec L10.
    """
    if "admin" not in (current_user.roles or []):
        if meeting_in.is_exec_meeting:
            if not bool(getattr(current_user, "is_exec", False)):
                raise HTTPException(
                    status_code=403,
                    detail="Only admins or Exec team members can schedule the Exec L10.",
                )
        else:
            if not meeting_in.department_name:
                raise HTTPException(
                    status_code=400,
                    detail="department_name is required for non-Exec meetings.",
                )
            _require_department_scope(current_user, meeting_in.department_name)

    meeting = L10Meeting(**meeting_in.model_dump(), created_by=current_user.email)
    db.add(meeting)
    db.commit()
    db.refresh(meeting)
    return meeting


@router.patch("/{meeting_id}", response_model=L10MeetingResponse)
def update_l10_meeting(
    meeting_id: UUID,
    db: DBDep,
    current_user: CurrentUser,
    meeting_in: L10MeetingUpdate,
):
    """Partial update of an L10 meeting. Authorization: anyone holding
    structural authority over it — an admin, the org-wide Exec, the Exec team
    (for the Exec L10), the current manager, or the leadership of the meeting's
    own department (see ``_authz.meeting_authority``). The primary use case is
    "transfer management" — the current manager hands off to a new person — but
    this endpoint also supports renaming, rescheduling, or moving a meeting
    between departments.

    Department leadership is included deliberately: gating this on the current
    manager alone is what left a whole team unable to re-point its own L10 the
    moment that one person went on vacation. A team's own leader can now do it
    without an admin. A team MEMBER with no leadership rung can still take an
    abandoned meeting over, but only via ``POST /{id}/transfer-manager``, which
    touches nothing but the manager field.
    """
    meeting = (
        db.query(L10Meeting)
        .filter(L10Meeting.id == meeting_id, L10Meeting.is_deleted == False)  # noqa: E712
        .first()
    )
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    if not can_manage_meeting(current_user, meeting, db):
        raise HTTPException(
            status_code=403,
            detail=(
                "Only this meeting's manager, your team's leader, or an admin "
                "can modify it. If the manager is away, you can take the "
                "meeting over instead."
            ),
        )

    updates = meeting_in.model_dump(exclude_unset=True)
    # Moving a meeting onto / off the Exec team is exec-only — flipping the
    # flag off would declassify the Exec L10 (its sessions, notes, headlines).
    if "is_exec_meeting" in updates and bool(updates["is_exec_meeting"]) != bool(
        meeting.is_exec_meeting
    ):
        require_exec_scope_allowed(current_user, bool(updates["is_exec_meeting"]))

    # Moving a meeting to a DIFFERENT department needs authority over the
    # destination, not just the origin. POST already enforces this on create;
    # PATCH never did, so authority over your own team was enough to push a
    # meeting into someone else's — a hole that got wider when department
    # leadership gained the right to PATCH at all.
    new_dept = updates.get("department_name")
    if (
        "department_name" in updates
        and (new_dept or None) != (meeting.department_name or None)
        and new_dept
    ):
        _require_department_scope(current_user, new_dept)
    for field, value in updates.items():
        setattr(meeting, field, value)

    meeting.updated_by = current_user.email  # Who edited / handed off
    db.commit()
    db.refresh(meeting)
    return meeting


@router.delete("/{meeting_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_l10_meeting(meeting_id: UUID, db: DBDep, current_user: CurrentUser):
    """Soft-delete an L10 meeting.

    Authorization mirrors the PATCH endpoint: any holder of structural
    authority (``_authz.meeting_authority``) — admin, org-wide Exec, the Exec
    team for the Exec L10, the current manager, or the leadership of the
    meeting's own department. Sessions, inputs, and attendance rows are
    intentionally left intact — the recurring slot disappears but the
    historical record (diary view, ratings, notes) stays referenceable
    by id for anyone holding a direct link.
    """
    meeting = (
        db.query(L10Meeting)
        .filter(L10Meeting.id == meeting_id, L10Meeting.is_deleted == False)  # noqa: E712
        .first()
    )
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    if not can_manage_meeting(current_user, meeting, db):
        raise HTTPException(
            status_code=403,
            detail=(
                "Only this meeting's manager, your team's leader, or an admin "
                "can delete it."
            ),
        )

    meeting.is_deleted = True
    meeting.updated_by = current_user.email  # Attribute the delete
    db.commit()
    return None


# -- Manager hand-off (incl. the OOO break-glass) ---------------------
#
# Why a dedicated endpoint rather than just PATCH: PATCH requires standing
# authority (manager / team leadership / admin), and the whole problem is the
# team that has NONE of those available because the one person who did is on
# vacation. This endpoint adds exactly one more rung — a member may take the
# meeting over *for themselves* while the manager is verifiably unavailable —
# and touches exactly one field, so widening who can reach it can't also widen
# what they can do. See ``_manager_access`` for the rules.


def _local_today(current_user) -> date:
    """Today in the caller's own timezone.

    OOO periods are stored as local calendar dates, so comparing them against a
    UTC date silently shifts the boundary: at 5pm Pacific the UTC date is already
    tomorrow, which is enough to read "out through Friday" as over.
    """
    from rubik_core.priorities.temporal import resolve_user_now

    try:
        return resolve_user_now(current_user.email).today
    except Exception:  # noqa: BLE001 — never fail a permission check on tz lookup
        return datetime.utcnow().date()


@router.get("/{meeting_id}/manager-availability")
def get_manager_availability(meeting_id: UUID, db: DBDep, current_user: CurrentUser):
    """Can this meeting's manager be reached to hand the meeting over?

    Lets the UI explain the situation and show the right button *before* anyone
    attempts a write. Requires ordinary meeting access (so it leaks nothing a
    caller can't already see) and also reports the caller's own standing
    authority, so the frontend can tell "you may simply edit this" from "you may
    only take it over because they're away".

    Not cheap enough for a list endpoint: it may fall back to a live calendar
    probe when the OOO cache holds nothing for that manager.
    """
    meeting = (
        db.query(L10Meeting)
        .filter(L10Meeting.id == meeting_id, L10Meeting.is_deleted == False)  # noqa: E712
        .first()
    )
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    require_meeting_access(current_user, meeting, db)

    availability = manager_availability(meeting, db, today=_local_today(current_user))
    rung = meeting_authority(current_user, meeting, db)
    caller = person_by_email(db, current_user.email)
    return {
        **availability.as_dict(),
        "meeting_id": str(meeting.id),
        # The rung the caller stands on ("manager", "leadership", "admin", …) or
        # None. Non-null means they can edit the meeting outright.
        "your_authority": rung,
        # The caller's own eos_people id, so a takeover can name them without
        # the client having to load the whole people list (which non-admins
        # can't do anyway — that's what made "transfer it to yourself" a dead
        # end in the UI).
        "your_person_id": str(caller.id) if caller is not None else None,
        # Whether the "Take over as manager" path is open to them right now.
        "can_take_over": bool(
            rung is None and caller is not None and not availability.available
        ),
    }


@router.post("/{meeting_id}/transfer-manager", response_model=L10MeetingResponse)
def transfer_l10_manager(
    meeting_id: UUID,
    db: DBDep,
    current_user: CurrentUser,
    payload: L10ManagerTransferRequest,
):
    """Re-point ONLY the manager of an L10 meeting.

    Two callers in mind:
      * standing authority (manager / team leadership / admin / Exec) handing
        the role to anyone, including clearing it;
      * a team member taking an abandoned meeting over for themselves while the
        manager is verifiably unavailable — no manager set, offboarded, or out
        of office today or on the meeting's next occurrence.

    Every transfer is logged with the rung it rode in on, so a break-glass
    takeover is auditable after the fact rather than silent.
    """
    meeting = (
        db.query(L10Meeting)
        .filter(L10Meeting.id == meeting_id, L10Meeting.is_deleted == False)  # noqa: E712
        .first()
    )
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    rung, availability = authorize_manager_transfer(
        current_user,
        meeting,
        db,
        today=_local_today(current_user),
        new_manager_id=payload.new_manager_id,
    )

    new_manager: Person | None = None
    if payload.new_manager_id is not None:
        new_manager = (
            db.query(Person)
            .filter(
                Person.id == payload.new_manager_id,
                Person.is_deleted.is_(False),
            )
            .first()
        )
        if new_manager is None:
            raise HTTPException(status_code=400, detail="That person wasn't found.")

    previous_id = meeting.manager_id
    meeting.manager_id = payload.new_manager_id
    meeting.updated_by = current_user.email
    db.commit()
    db.refresh(meeting)

    logger.info(
        "[l10-handoff] meeting=%s manager %s -> %s by=%s rung=%s reason=%s note=%s",
        meeting.id,
        previous_id,
        payload.new_manager_id,
        current_user.email,
        rung,
        availability.reason if availability is not None else "standing_authority",
        (payload.note or "").strip()[:200] or "-",
    )
    return meeting


# -- Admin: structural console over every L10 -------------------------


@router.get("/admin/meetings")
def admin_list_all_l10_meetings(db: DBDep, current_user: CurrentUser):
    """Every L10 meeting's STRUCTURAL metadata, for admins only.

    Admins have always been allowed to re-point any meeting, but never to
    *enumerate* them: ``accessible_meeting_ids`` is membership-scoped for
    everyone, admins included, so an admin asked to fix a team's abandoned L10
    had the authority and no way to find the row. That is the other half of the
    lockout this change fixes.

    What it deliberately does NOT return: notes, diary, headlines, ideas,
    ratings, survey answers, attendee identities, or roster emails. The
    membership-only CONTENT fence stays exactly where it is — this is a
    switchboard (what exists, which team, who runs it, when it last met), not a
    reader. ``attendee_count`` is a number precisely so the Exec L10's
    membership isn't disclosed by listing it.
    """
    if "admin" not in (current_user.roles or []):
        raise HTTPException(status_code=403, detail="Admins only.")

    meetings = (
        db.query(L10Meeting)
        .filter(L10Meeting.is_deleted == False)  # noqa: E712
        .order_by(L10Meeting.name)
        .all()
    )

    managers = (
        {
            p.id: p
            for p in db.query(Person)
            .filter(Person.id.in_([m.manager_id for m in meetings if m.manager_id]))
            .all()
        }
        if any(m.manager_id for m in meetings)
        else {}
    )

    # Last session date per meeting, in one grouped query rather than N+1.
    last_sessions = dict(
        db.query(L10Session.meeting_id, func.max(L10Session.actual_start))
        .filter(L10Session.is_deleted == False)  # noqa: E712
        .group_by(L10Session.meeting_id)
        .all()
    )

    out = []
    for m in meetings:
        mgr = managers.get(m.manager_id)
        last = last_sessions.get(m.id)
        out.append(
            {
                "id": str(m.id),
                "name": m.name,
                "department_name": m.department_name,
                "is_exec_meeting": bool(m.is_exec_meeting),
                "day_of_week": m.day_of_week,
                "start_time": m.start_time,
                "duration_minutes": m.duration_minutes,
                "manager_id": str(m.manager_id) if m.manager_id else None,
                "manager_name": (
                    f"{mgr.first_name or ''} {mgr.last_name or ''}".strip()
                    if mgr
                    else None
                ),
                "manager_email": mgr.email if mgr else None,
                # A count, never the list — see the docstring.
                "attendee_count": len(m.roster_emails or []),
                "last_session_at": last.isoformat() if last else None,
            }
        )
    return {"meetings": out, "count": len(out)}


# -- Calendar-ingest of upcoming L10s ------------------------


@router.get("/calendar-upcoming")
def calendar_upcoming_l10s(
    db: DBDep,
    current_user: CurrentUser,
    limit: int = 5,
    fetch_window: int = 30,
):
    """Return the next ``limit`` calendar events whose title
    contains "L10" so the Meetings page can drop the manual scheduling
    form. Teams already calendar their L10s for the year; we just
    ingest those events instead of asking them to re-create them in
    Cubie.

    Match rule: case-insensitive ``"L10"`` substring on the event
    summary. Teams that use a different naming convention are asked
    to rename rather than us building per-team regexes: it is easier to
    ask people to rename a meeting than to bend the logic to every team's
    naming.

    Returns a normalized shape rather than the raw Google Calendar
    event JSON so the frontend doesn't have to know about Google's
    nested date.dateTime convention.
    """
    from rubik_core.tools.calendar import GoogleCalendarClient

    if limit <= 0:
        limit = 5
    if fetch_window <= 0:
        fetch_window = 30

    client = GoogleCalendarClient(user_email=current_user.email)
    if not getattr(client, "service", None):
        # Calendar not configured (e.g. local dev without GWS keys) —
        # return an empty list so the frontend renders the "nothing
        # ingested" empty state instead of throwing.
        return {"events": [], "calendar_available": False}

    raw = client.get_upcoming_events(max_results=fetch_window)
    out: list[dict] = []
    for e in raw:
        summary = (e.get("summary") or "").strip()
        if "l10" not in summary.lower():
            continue
        start_dt = (e.get("start") or {}).get("dateTime") or (e.get("start") or {}).get(
            "date"
        )
        end_dt = (e.get("end") or {}).get("dateTime") or (e.get("end") or {}).get(
            "date"
        )
        attendees = []
        for a in e.get("attendees") or []:
            email_addr = a.get("email")
            if email_addr:
                attendees.append(email_addr)
        out.append(
            {
                "google_event_id": e.get("id"),
                # Series id shared by every occurrence of a recurring event.
                # An imported meeting is keyed on this (see calendar-import),
                # so the frontend uses it to match an upcoming occurrence back
                # to the meeting it should refresh once the last one has passed.
                "recurring_event_id": e.get("recurringEventId"),
                "summary": summary,
                "start": start_dt,
                "end": end_dt,
                "location": e.get("location"),
                "hangout_link": e.get("hangoutLink"),
                "attendees": attendees,
                "organizer_email": (e.get("organizer") or {}).get("email"),
            }
        )
        if len(out) >= limit:
            break

    return {"events": out, "calendar_available": True}


# How many series we will ask Google about in one page load. Each is its own
# API round-trip, and the board must not hang behind an exec with a dozen
# imported L10s. Meetings past the cap fall back to their stored state, which
# the manager's own visit (or any earlier visit) will have filled in.
_CALENDAR_STATUS_MAX_LOOKUPS = 12


@router.get("/calendar-status", response_model=L10CalendarStatusResponse)
def l10_calendar_status(db: DBDep, current_user: CurrentUser):
    """What Google Calendar currently says about the caller's imported L10s.

    The board has always drawn its "Next: Wednesday, Sep 16" from the stored
    weekly PATTERN, which keeps counting forever — so a team that cancelled the
    calendar event went on being promised meetings that were not going to
    happen. This endpoint asks Google instead (see
    ``rubik_core.eos.calendar_sync``) and the client redraws the schedule line
    from the answer.

    Split out from ``GET /`` deliberately: it costs one Google round-trip per
    imported meeting, and the meetings board must paint without waiting on a
    third party. Manually-scheduled meetings (no ``google_event_id``) are not
    returned at all — there is no calendar to check, and their pattern remains
    the only truth they have.

    Writes back only for the meeting's MANAGER, who is the calendar organizer
    an import resolved the meeting from. An attendee who deleted their personal
    copy has not cancelled the team's L10 and must not be able to blank the
    card for everybody else — they still get the corrected read for their own
    view. A Google failure is reported as ``unknown`` and never persisted.
    """
    from rubik_core.eos.calendar_sync import (
        STATE_UNKNOWN,
        apply_to_meeting,
        may_persist,
        resolve_series_state,
    )
    from rubik_core.tools.calendar import GoogleCalendarClient

    meetings = (
        db.query(L10Meeting)
        .filter(
            L10Meeting.is_deleted == False,  # noqa: E712
            L10Meeting.google_event_id.isnot(None),
            L10Meeting.id.in_(accessible_meeting_ids(current_user, db)),
        )
        .order_by(L10Meeting.name)
        .all()
    )
    if not meetings:
        return L10CalendarStatusResponse(calendar_available=True, statuses=[])

    manager_emails: dict = {}
    manager_ids = [m.manager_id for m in meetings if m.manager_id]
    if manager_ids:
        manager_emails = {
            p.id: p.email
            for p in db.query(Person).filter(Person.id.in_(manager_ids)).all()
        }

    client = GoogleCalendarClient(user_email=current_user.email)
    calendar_available = bool(getattr(client, "service", None))

    if not calendar_available:
        skip_reason = (
            getattr(client, "init_error", None) or "Google Calendar is unavailable"
        )
    else:
        skip_reason = "too many imported meetings to refresh in one go"

    statuses: list[L10CalendarStatus] = []
    persisted_any = False
    lookups = 0
    for m in meetings:
        if calendar_available and lookups < _CALENDAR_STATUS_MAX_LOOKUPS:
            lookups += 1
            resolved = resolve_series_state(
                client, m.google_event_id, reader_email=current_user.email
            )
        else:
            # Over the cap, or no calendar at all: report what is on the row
            # rather than a fresh answer we did not actually obtain.
            resolved = None

        if resolved is not None and resolved.is_known:
            persisted = may_persist(
                m, current_user.email, manager_emails.get(m.manager_id)
            )
            if persisted:
                # Commit even when nothing moved: the check itself is news, and
                # ``calendar_checked_at`` is what tells a reader how fresh this
                # answer is. Only the CHANGES are worth a log line.
                changed = apply_to_meeting(m, resolved)
                persisted_any = True
                if changed:
                    logger.info(
                        "[l10-calendar-sync] meeting=%s state=%s next=%s by=%s",
                        m.id,
                        resolved.state,
                        resolved.next_start.isoformat() if resolved.next_start else "-",
                        current_user.email,
                    )
            statuses.append(
                L10CalendarStatus(
                    meeting_id=m.id,
                    state=resolved.state,
                    next_occurrence_at=resolved.next_start,
                    checked_at=datetime.utcnow(),
                    persisted=persisted,
                    reason=resolved.reason,
                    kind=resolved.kind,
                )
            )
            continue

        statuses.append(
            L10CalendarStatus(
                meeting_id=m.id,
                state=m.calendar_state or STATE_UNKNOWN,
                next_occurrence_at=m.next_occurrence_at,
                checked_at=m.calendar_checked_at,
                persisted=False,
                reason=resolved.reason if resolved is not None else skip_reason,
            )
        )

    if persisted_any:
        db.commit()

    return L10CalendarStatusResponse(
        calendar_available=calendar_available, statuses=statuses
    )


# The Exec team's L10 is named for the team that runs it — typically "ET L10"
# ("ET" = Executive Team), with occasional prefixes like "[Prep] ET L10". A
# calendar import used to hard-code ``is_exec_meeting=False``, so importing
# the ET L10 landed it as an ordinary department meeting: it got the weaker
# department-style fence on its notes / headlines / ratings, and (because
# ``team_scope_filter`` routes items by the flag) it aggregated the WRONG
# team's Rocks / IDS / Scorecard — in dev it aggregated nothing at all,
# because it had been stamped with a department that owns no EOS rows.
#
# Matching is deliberately narrow, because a false positive locks a team out
# of its own meeting: ``ET`` must be a standalone word AND lead the title,
# after any bracketed prefix ("[Prep] ET L10"). Leading-only is what keeps a
# title that merely *mentions* the timezone — "Sales L10 - 10am ET" — from
# being read as the Exec team's meeting. "Budget"/"Target" don't trip it
# either, since the word boundary rules out ``et`` inside a word. Anything
# this misses is still one exec-only click away: Meetings → edit → Scope.
_EXEC_MEETING_NAME_PATTERNS = (
    re.compile(r"^\s*(?:[\[(][^\])]*[\])]\s*)*ET\b", re.IGNORECASE),
)


def _infer_import_exec_flag(name: str) -> bool:
    """True when a calendar event's title names it as the Exec team's L10.

    The flag is a confidentiality DECLARATION, not a fact derivable from an
    invite list, which is why this reads the meeting's NAME and not its
    roster: any "mostly execs were invited" heuristic sits uncomfortably
    close to a department L10 that an exec attends as that team's leader
    (in dev, ET L10 is 4/7 exec and a Systems team L10 is 2/4).

    Callers MUST pass the result through ``require_exec_scope_allowed`` —
    inferring the flag must never become a way for a non-exec to mint an
    Exec-scoped meeting behind the gate that gates every other route onto
    the Exec surface.
    """
    return any(p.search(name or "") for p in _EXEC_MEETING_NAME_PATTERNS)


def _infer_import_department(db, roster_emails: list[str], importer) -> str | None:
    """Department to stamp on a calendar-imported L10 meeting.

    The meeting belongs to the TEAM being invited, not to whoever clicked
    Import: stamping the importer's own department mis-scopes the meeting
    whenever an admin — or a manager from another team — imports it, and a
    mis-scoped meeting aggregates the WRONG team's IDS / Scorecard / To-Dos
    (how a Delivery L10 ends up reviewing the Systems team's issues). Majority
    vote over the invitees' ``users.department`` (case-insensitive; blank
    and the 'general' catch-all dropped): a clear winner is used, a tie or
    an unknown roster falls back to the importer's own department — the
    common manager-imports-their-own-L10 case, where both agree anyway."""
    fallback = getattr(importer, "department", None)
    emails = [e.lower() for e in (roster_emails or []) if e]
    if not emails:
        return fallback
    rows = (
        db.query(User.department)
        .filter(func.lower(User.email).in_(emails), User.is_platform_support.is_(False))
        .all()
    )
    counts: dict[str, int] = {}
    display: dict[str, str] = {}
    for (dept,) in rows:
        d = (dept or "").strip()
        if not d or d.lower() == "general":
            continue
        counts[d.lower()] = counts.get(d.lower(), 0) + 1
        display.setdefault(d.lower(), d)
    if not counts:
        return fallback
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return fallback  # tie — no clear team; don't guess
    return display[ranked[0][0]]


@router.post(
    "/calendar-import",
    response_model=L10MeetingResponse,
    status_code=status.HTTP_201_CREATED,
)
def import_meeting_from_calendar(
    payload: L10CalendarImportRequest,
    db: DBDep,
    current_user: CurrentUser,
):
    """Promote a Google Calendar event into a full
    ``L10Meeting`` row so the team can run the session, log attendance,
    and accrue history through the normal flow — not just see the
    event in a strip.

    Behavior:
    1. Fetch the event from the caller's Google Calendar.
    2. Resolve the organizer email → ``Person`` (created if missing) →
       used as ``manager_id``.
    3. Resolve attendee emails → ``Person`` rows (created if missing)
       and store the original email list in ``roster_emails`` for ACL
       checks. The caller's email is always included.
    4. Infer ``day_of_week`` + ``start_time`` from the event's start
       timestamp (uses Python ``weekday()`` so Mon=0..Sun=6, matching
       the rest of the EOS code).
    5. Upsert by ``google_event_id`` — re-importing the same recurring
       event refreshes name / roster / schedule without creating a
       duplicate row.
    6. Recognize the Exec team's L10 by name ("ET L10") and scope it to the
       Exec team, gated on the caller being exec — see
       ``_infer_import_exec_flag``.

    Returns the newly-created or updated ``L10MeetingResponse``.
    """
    from datetime import datetime as _dt

    from rubik_core.eos.calendar_groups import expand_group_invitees
    from rubik_core.eos.calendar_sync import (
        STATE_ACTIVE,
        STATE_CANCELLED,
        SeriesState,
        apply_to_meeting,
        may_persist,
    )
    from rubik_core.tools.calendar import GoogleCalendarClient

    google_event_id = (payload.google_event_id or "").strip()
    if not google_event_id:
        raise HTTPException(status_code=422, detail="google_event_id must not be empty")

    client = GoogleCalendarClient(user_email=current_user.email)
    if not getattr(client, "service", None):
        raise HTTPException(
            status_code=503,
            detail="Google Calendar is not configured on this instance.",
        )

    event = client.get_event(google_event_id) or {}
    if not event or not event.get("id") or (event.get("status") or "") == "cancelled":
        # A Sync on a series the team has since CANCELLED lands here. Record
        # that on the row when the caller speaks for the series (its manager /
        # organizer) so the board stops promising occurrences of a meeting that
        # is not happening — the whole point of the reconciliation columns. Then
        # say so plainly instead of the old "not found (or you don't have
        # access)", which read as a permissions problem.
        cancelled_meeting = (
            db.query(L10Meeting)
            .filter(
                L10Meeting.is_deleted == False,  # noqa: E712
                L10Meeting.google_event_id == google_event_id,
            )
            .first()
        )
        if cancelled_meeting is not None:
            manager_email = None
            if cancelled_meeting.manager_id:
                mgr = (
                    db.query(Person)
                    .filter(Person.id == cancelled_meeting.manager_id)
                    .first()
                )
                manager_email = mgr.email if mgr else None
            if may_persist(cancelled_meeting, current_user.email, manager_email):
                apply_to_meeting(cancelled_meeting, SeriesState(STATE_CANCELLED))
                db.commit()
                logger.info(
                    "[l10-calendar-sync] meeting=%s marked cancelled on sync by=%s",
                    cancelled_meeting.id,
                    current_user.email,
                )
                raise HTTPException(
                    status_code=404,
                    detail=(
                        "That calendar event no longer exists — it looks like the "
                        "meeting was cancelled. This L10 is now marked as having "
                        "no upcoming occurrence; delete it when the team is done "
                        "with it, or re-import it from the calendar if it comes "
                        "back."
                    ),
                )
        raise HTTPException(
            status_code=404,
            detail="Calendar event not found (or you don't have access).",
        )

    # Dedup recurring series. A weekly L10 surfaces as many calendar
    # *instances* (Jul 1, Jul 8, …), each with its own event id but a shared
    # ``recurringEventId``. Key the meeting on the series id so importing ANY
    # occurrence upserts the one recurring slot instead of creating a separate
    # row per week. Single (non-recurring) events fall back to their own id.
    series_id = (
        event.get("recurringEventId") or event.get("id") or google_event_id
    ).strip()

    name = (event.get("summary") or "L10 (untitled)").strip()
    start_raw = (event.get("start") or {}).get("dateTime") or (
        event.get("start") or {}
    ).get("date")
    if not start_raw:
        raise HTTPException(
            status_code=422,
            detail="Calendar event has no start time; can't import.",
        )

    # ``fromisoformat`` accepts the trailing offset Google returns
    # ("2026-06-03T11:00:00-06:00"). All-day events come as plain
    # date strings — give them midnight UTC.
    try:
        start_dt = _dt.fromisoformat(start_raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            start_dt = _dt.fromisoformat(start_raw)
        except ValueError as e:
            raise HTTPException(
                status_code=422,
                detail=f"Could not parse event start time {start_raw!r}: {e}",
            )

    day_of_week = start_dt.weekday()
    start_time = start_dt.strftime("%H:%M")
    imported_date = start_dt.date()

    # Duration derivation — best-effort: end - start in minutes.
    duration_minutes = 90
    end_raw = (event.get("end") or {}).get("dateTime") or (event.get("end") or {}).get(
        "date"
    )
    if end_raw:
        try:
            end_dt = _dt.fromisoformat(end_raw.replace("Z", "+00:00"))
            delta_min = int((end_dt - start_dt).total_seconds() / 60)
            if 5 <= delta_min <= 8 * 60:
                duration_minutes = delta_min
        except ValueError:
            pass  # keep default

    # ── Attendees ────────────────────────────────────────────────────
    roster_emails: list[str] = []
    seen_emails: set[str] = set()
    for a in event.get("attendees") or []:
        em = (a.get("email") or "").strip().lower()
        if em and em not in seen_emails:
            seen_emails.add(em)
            roster_emails.append(em)
    # A Google Group on the invite arrives as ONE attendee carrying the
    # group's address — the Calendar API never expands it — so anyone invited
    # only through the group was missing from this roster and therefore from
    # the Check-in picker it scopes (and Sync re-read the same unexpanded
    # group). Replace each group with its people. Known users are never looked
    # up, so this costs one Directory call per unknown address; on any failure
    # the address is kept exactly as given and the import proceeds.
    known_user_emails = {
        (e or "").strip().lower()
        for (e,) in db.query(User.email)
        .filter(User.is_platform_support.is_(False))
        .all()
        if e
    }
    expansion = expand_group_invitees(roster_emails, skip=known_user_emails)
    if expansion.expanded:
        logger.info(
            "[l10-calendar-import] expanded group invitees %s by=%s",
            {g: len(ms) for g, ms in expansion.expanded.items()},
            current_user.email,
        )
    roster_emails = list(expansion.roster)
    seen_emails = set(roster_emails)
    organizer_email = (
        ((event.get("organizer") or {}).get("email") or current_user.email)
        .strip()
        .lower()
    )
    if organizer_email and organizer_email not in seen_emails:
        roster_emails.insert(0, organizer_email)
        seen_emails.add(organizer_email)
    # Caller always sees their own imported meeting.
    me_email = current_user.email.lower()
    if me_email not in seen_emails:
        roster_emails.append(me_email)

    # ── Resolve / create Person rows for the manager and roster ─────
    def _ensure_person(email: str) -> Person:
        # Case-insensitive: this is a create-on-miss path, so a casing mismatch
        # against an existing row doesn't just fail to find it — it tries to
        # INSERT a duplicate and trips the unique index on eos_people.email,
        # failing the whole calendar import.
        person = person_by_email(db, email)
        if person:
            return person
        # Derive a first/last from the email local part as a placeholder.
        # The owner can clean it up in /eos/people later. We DO NOT block
        # the import on this — the calendar import is meant to be one-click.
        local = email.split("@", 1)[0]
        parts = local.replace(".", " ").replace("_", " ").split()
        first = parts[0].capitalize() if parts else email
        last = parts[1].capitalize() if len(parts) > 1 else ""
        person = Person(
            first_name=first,
            last_name=last,
            email=email,
            created_by=current_user.email,
        )
        db.add(person)
        db.commit()
        db.refresh(person)
        return person

    manager = _ensure_person(organizer_email)
    # Ensure each roster email has a Person row too — even if we don't
    # store the FK directly, downstream features (attendance, "Add to IDS"
    # owner defaults, etc.) all look up by email → Person.
    for em in roster_emails:
        if em != organizer_email:
            _ensure_person(em)

    # ── Upsert: find the ONE meeting this series belongs to ─────────
    #
    # Historically this only matched ``google_event_id == series_id``, which
    # quietly minted duplicate rows: meetings imported before series-dedup
    # existed store an *instance* id (``<series>_<timestamp>``), and manually
    # created meetings store no id at all — so importing an occurrence
    # updated (or created) a sibling row while the user's original card,
    # with all its session history, sat stale. One real-world weekly L10
    # must be one row, so the match is now three-tiered:
    #   1. exact series id (normal path since series-dedup),
    #   2. instance-id prefix (``<series>_…`` — pre-dedup imports),
    #   3. same name + same team (slug-normalized, per docs/ENGINEERING_NOTES.md) — manual
    #      rows or rows linked to a recreated calendar series.
    # Among matches, the SURVIVOR is the row with session history (most
    # sessions, then inputs, then oldest) — never a fresh empty duplicate.
    # The Exec team's L10 is recognized by NAME (see _infer_import_exec_flag).
    # Gate it exactly like every other route onto the Exec surface: a non-exec
    # importing something titled "ET L10" gets a clear 403 rather than a
    # meeting that silently vanishes from their own list the moment it's
    # created. An Exec meeting is cross-department, so it carries no
    # department — matching what the Meetings scope picker sends.
    is_exec_meeting = _infer_import_exec_flag(name)
    if is_exec_meeting:
        require_exec_scope_allowed(current_user, True)
        meeting_department = None
    else:
        meeting_department = _infer_import_department(db, roster_emails, current_user)
    series_prefix = f"{series_id}_"

    def _norm(s: str | None) -> str:
        return (s or "").strip().lower()

    def _same_team(a: str | None, b: str | None) -> bool:
        if not a and not b:
            return True
        if not a or not b:
            return False
        return slugify_department(a) == slugify_department(b)

    live_meetings = (
        db.query(L10Meeting)
        .filter(L10Meeting.is_deleted == False)  # noqa: E712
        .all()
    )
    related = [
        m
        for m in live_meetings
        if (
            m.google_event_id
            and (
                m.google_event_id == series_id
                or m.google_event_id.startswith(series_prefix)
            )
        )
        or (
            # Name-match only within the same TEAM KIND: a department import
            # must never absorb the Exec L10 (that would declassify it), and
            # an Exec import must never absorb a same-named department
            # meeting. Two Exec meetings both carry a NULL department, which
            # ``_same_team`` already treats as a match.
            bool(m.is_exec_meeting) == is_exec_meeting
            and _norm(m.name) == _norm(name)
            and _same_team(m.department_name, meeting_department)
        )
    ]

    def _activity(m: L10Meeting) -> tuple[int, int]:
        sessions = (
            db.query(func.count(L10Session.id))
            .filter(L10Session.meeting_id == m.id)
            .scalar()
            or 0
        )
        inputs = (
            db.query(func.count(L10Input.id))
            .filter(L10Input.meeting_id == m.id)
            .scalar()
            or 0
        )
        return sessions, inputs

    activity = {m.id: _activity(m) for m in related}

    meeting: L10Meeting | None = None
    if related:
        meeting = sorted(
            related,
            key=lambda m: (
                -activity[m.id][0],
                -activity[m.id][1],
                m.created_at or datetime.min,
            ),
        )[0]

    # A series *master* event (fetched when Sync passes the stored series id
    # directly) carries the series' FIRST-ever start date — don't let it
    # rewind ``last_imported_date``; only concrete occurrences stamp it.
    is_series_master = bool(event.get("recurrence"))

    if meeting is None:
        meeting = L10Meeting(
            name=name,
            day_of_week=day_of_week,
            start_time=start_time,
            duration_minutes=duration_minutes,
            manager_id=manager.id,
            department_name=meeting_department,
            is_exec_meeting=is_exec_meeting,
            roster_emails=roster_emails,
            google_event_id=series_id,
            last_imported_date=None if is_series_master else imported_date,
            created_by=current_user.email,
        )
        # We just read this event off the calendar, so it is demonstrably
        # active. Stamping it here means a freshly imported meeting never sits
        # in the "never reconciled" state that reads as unknown.
        apply_to_meeting(meeting, SeriesState(STATE_ACTIVE), set_next=False)
        db.add(meeting)
    else:
        # Re-import refreshes the mutable bits but leaves
        # department / is_exec_meeting alone — those were set by the
        # original importer's policy and shouldn't silently flip when a
        # different teammate hits the Import button.
        #
        # ONE exception, and only in the confidential direction: an Exec-named
        # meeting imported before this inference existed sits unflagged
        # forever otherwise (that's how dev's ET L10 spent two days as an
        # "Operations" meeting). Heal False → True; NEVER the reverse — an
        # import must not be a back door for declassifying an Exec L10, which
        # is why the ``require_exec_scope_allowed`` gate above is the only
        # path that can reach this line with ``is_exec_meeting`` set.
        if is_exec_meeting and not meeting.is_exec_meeting:
            meeting.is_exec_meeting = True
            meeting.department_name = None
        meeting.name = name
        meeting.day_of_week = day_of_week
        meeting.start_time = start_time
        meeting.duration_minutes = duration_minutes
        meeting.manager_id = manager.id
        meeting.roster_emails = roster_emails
        # Heal the link: pre-dedup instance ids / recreated series ids all
        # converge on the current series id so the next import matches tier 1.
        meeting.google_event_id = series_id
        if not is_series_master:
            meeting.last_imported_date = imported_date
        # Re-import proves the series is live again — clear any cancellation
        # a previous reconciliation recorded.
        apply_to_meeting(meeting, SeriesState(STATE_ACTIVE), set_next=False)
        meeting.updated_by = current_user.email

    # ── Consolidate: soft-delete EMPTY duplicate siblings ────────────
    # Only rows that (a) matched this same series/name and (b) hold zero
    # sessions and zero prep inputs — i.e. pure noise minted by the old
    # duplicate-prone upsert. Anything with history is left alone.
    for dup in related:
        if dup.id == meeting.id:
            continue
        if activity[dup.id] == (0, 0):
            dup.is_deleted = True
            dup.updated_by = current_user.email

    db.commit()
    db.refresh(meeting)
    return meeting


# -- Sessions ---------------------------------------------------------


@router.get("/sessions", response_model=list[L10SessionResponse])
def list_sessions(
    db: DBDep,
    current_user: CurrentUser,
    meeting_id: UUID | None = None,
    finished_only: bool = False,
    limit: int | None = None,
):
    """List L10 sessions. Filters:

    - ``meeting_id``: restrict to a single L10 meeting (recurring slot).
    - ``finished_only``: only return sessions with ``actual_end`` set,
      ordered newest-first. This powers the past-meetings diary —
      a running history the team can scroll through.
    - ``limit``: cap the result set (combined with finished_only for the
      diary's "last N meetings" view).
    """
    query = db.query(L10Session).filter(L10Session.is_deleted == False)  # noqa: E712
    # Team confidentiality: sessions inherit their meeting's access —
    # membership-scoped for everyone (admins/CEO included), so nobody sees
    # another team's L10 diary (notes, ratings). See accessible_meeting_ids.
    query = query.filter(
        L10Session.meeting_id.in_(accessible_meeting_ids(current_user, db))
    )
    if meeting_id:
        query = query.filter(L10Session.meeting_id == meeting_id)
    if finished_only:
        query = query.filter(L10Session.actual_end.is_not(None))
        # Reverse-chronological is the natural "diary" order: latest at top.
        query = query.order_by(L10Session.actual_end.desc())
    if limit:
        query = query.limit(limit)
    return query.all()


# Manager-only edit endpoint for past session notes. Read access
# on the GET sessions endpoint is already open to all team members
# (the team-scope filtering happens on the frontend by passing
# ``meeting_id``); only the meeting's manager can edit a previously
# closed session's notes + average rating to correct typos or expand
# context after the fact.


@router.patch("/sessions/{session_id}", response_model=L10SessionResponse)
def update_session(
    session_id: UUID,
    db: DBDep,
    current_user: CurrentUser,
    session_in: L10SessionCreate,  # body shape is the same as create
):
    """Edit a past session's notes and/or average_rating.

    Restricted to holders of structural authority over the meeting — admin,
    org-wide Exec, the Exec team (Exec L10), the current manager, or the
    leadership of the meeting's own department (``_authz.meeting_authority``);
    every one of those can already READ the diary, so this widens who may
    *correct* it, not who may see it. Anything other than ``notes`` /
    ``average_rating`` is silently ignored — we don't let history get rewritten
    (meeting_id, actual_start, actual_end stay locked once the session is
    logged).
    """
    session = (
        db.query(L10Session)
        .filter(
            L10Session.id == session_id,
            L10Session.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    _require_session_access(db, session, current_user)

    meeting = db.query(L10Meeting).filter(L10Meeting.id == session.meeting_id).first()
    if meeting is None or not can_manage_meeting(current_user, meeting, db):
        raise HTTPException(
            status_code=403,
            detail=(
                "Only the meeting manager, your team's leader, or an admin can "
                "edit a past session."
            ),
        )

    # Only mutate the fields the diary cares about. Everything else is
    # locked once the session is logged.
    payload = session_in.model_dump()
    if "notes" in payload and payload["notes"] is not None:
        session.notes = payload["notes"]
    if "average_rating" in payload and payload["average_rating"] is not None:
        session.average_rating = payload["average_rating"]

    # The cached recap was generated from the pre-edit notes/rating, so an
    # edit stales it — clear it and the next GET /recap regenerates from fresh
    # material.
    if ("notes" in payload and payload["notes"] is not None) or (
        "average_rating" in payload and payload["average_rating"] is not None
    ):
        session.recap = None

    session.updated_by = current_user.email  # Who edited the diary entry
    db.commit()
    db.refresh(session)

    response = L10SessionResponse.model_validate(session)
    # Derived field — caller is the manager (else we'd have 403'd).
    response.is_manager = True
    return response


@router.post("/sessions/start", response_model=L10SessionResponse)
def start_session(db: DBDep, current_user: CurrentUser, session_in: L10SessionCreate):
    meeting = (
        db.query(L10Meeting).filter(L10Meeting.id == session_in.meeting_id).first()
    )
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    require_meeting_access(current_user, meeting, db)

    person = person_by_email(db, current_user.email)
    is_manager = person is not None and meeting.manager_id == person.id

    # Session locking: reuse the currently-open session for this meeting
    # if one exists, instead of creating a duplicate.
    #
    # But only if it is still LIVE. Locking onto any open row meant that a
    # team whose last facilitator never clicked Conclude silently resumed a
    # weeks-old session on their next occurrence — inheriting its
    # ``current_step`` cursor (so the agenda opened mid-meeting), its
    # attendance rows, and its ratings. Close the abandoned one first, on the
    # shared rule, so this week's meeting gets this week's row. No commit yet:
    # the close and the new session land in one transaction below.
    close_abandoned_sessions(db, meeting_ids=[session_in.meeting_id], commit=False)

    def _live_session() -> L10Session | None:
        return (
            db.query(L10Session)
            .filter(
                L10Session.meeting_id == session_in.meeting_id,
                L10Session.actual_end.is_(None),
                L10Session.is_deleted == False,  # noqa: E712
            )
            .order_by(L10Session.actual_start.desc())
            .first()
        )

    active_session = _live_session()

    if active_session:
        # Rejoin path: return the live session AS-IS. Critically we do NOT
        # touch current_step / step_started_at here — re-anchoring on every
        # (re)join is exactly the "timer restarts when I come back" bug. The
        # client restores the real step + anchor from this response.
        session = active_session
        db.commit()  # persist any sibling session the sweep above closed
    else:
        # Read-then-insert with no lock, called by EVERY participant's browser
        # on navigation: two people opening the meeting in the same instant both
        # read "no live session" and both insert. Each client then polls its own
        # session's cursor, so the facilitator advances the agenda and the other
        # room sits still, one screen stuck on the scorecard. A partial unique
        # index on (meeting_id) WHERE actual_end IS NULL stops it; the loser of
        # the race lands here, catches the IntegrityError, and rejoins the
        # winner instead of forking the room.
        session = L10Session(**session_in.model_dump(), created_by=current_user.email)
        # Initialize the shared cursor: everyone starts on the Segue (step 0)
        # with the timer anchored to the session's start.
        session.current_step = 0
        session.step_started_at = session.actual_start
        session.state_version = 0
        db.add(session)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            # The rollback also undid the abandoned-session sweep above (it was
            # flushed, not committed), so re-run it before re-reading. Otherwise
            # a weeks-old open row could be the "live session" we hand back, and
            # rejoining it is the stale-cursor bug that sweep exists to prevent.
            close_abandoned_sessions(
                db, meeting_ids=[session_in.meeting_id], commit=True
            )
            session = _live_session()
            if session is None:
                # The conflicting row was concluded between our insert and this
                # re-read (or the constraint fired for another reason entirely).
                # Surface it rather than looping — the client retries on the
                # user's next navigation.
                logger.warning(
                    "[l10] session start lost the race for meeting=%s but found "
                    "no live session on re-read",
                    session_in.meeting_id,
                )
                raise HTTPException(
                    status_code=409,
                    detail="Another participant is starting this meeting — retry.",
                ) from None
        db.refresh(session)

    response = L10SessionResponse.model_validate(session)
    response.is_manager = is_manager
    return response


@router.post("/sessions/{session_id}/end", response_model=L10SessionResponse)
def end_session(
    db: DBDep,
    current_user: CurrentUser,
    background_tasks: BackgroundTasks,
    session_id: UUID,
    rating: int | None = None,
    notes: str | None = None,
):
    session = db.query(L10Session).filter(L10Session.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    _require_session_access(db, session, current_user)
    if ((notes if notes is not None else session.notes) or "").strip():
        reject_support_background_work()

    # Upstream notes that ideally only the manager (or, fallback, the
    # session creator) ends the meeting. Upstream leaves this open as a
    # soft constraint; we mirror exactly. Future spec can tighten it.

    session.actual_end = datetime.utcnow()

    # The meeting rating is now an *individual* survey — each
    # attendee submits their own 1..10 on eos_l10_attendance.rating. The
    # session's headline ``average_rating`` is DERIVED from those rows so
    # the existing dashboard aggregate keeps working. Fall back to the
    # (now optional) ``rating`` query param for backward-compat when no
    # per-person ratings were submitted.
    per_person_ratings = [
        a.rating
        for a in (
            db.query(L10Attendance)
            .filter(
                L10Attendance.session_id == session_id,
                L10Attendance.is_deleted == False,  # noqa: E712
            )
            .all()
        )
        if a.rating is not None
    ]
    if per_person_ratings:
        session.average_rating = round(
            sum(per_person_ratings) / len(per_person_ratings)
        )
    elif rating is not None:
        session.average_rating = rating

    # Don't let "conclude with no closing notes" wipe notes that were
    # already saved on the session (e.g. via the diary edit endpoint).
    if notes is not None:
        session.notes = notes
    session.updated_by = current_user.email  # Who concluded the meeting

    db.commit()
    db.refresh(session)

    # Index the session's notes for retrieval (best-effort, post-response).
    if (session.notes or "").strip():
        enqueue_l10_session_ingest(background_tasks, session_id=str(session.id))

    return session


# -- Attendance -------------------------------------------------------


@router.post("/sessions/{session_id}/attendance", response_model=L10AttendanceResponse)
def update_attendance(
    db: DBDep,
    current_user: CurrentUser,
    session_id: UUID,
    attendance_in: L10AttendanceCreate,
):
    # Upsert by (session_id, person_id). Note: the URL session_id is
    # informational only — the stored value comes from the request body,
    # matching upstream behavior. Future spec may tighten this.
    target_session = (
        db.query(L10Session).filter(L10Session.id == attendance_in.session_id).first()
    )
    if not target_session:
        raise HTTPException(status_code=404, detail="Session not found")
    _require_session_access(db, target_session, current_user)
    attendance = (
        db.query(L10Attendance)
        .filter(
            L10Attendance.session_id == attendance_in.session_id,
            L10Attendance.person_id == attendance_in.person_id,
        )
        .first()
    )

    # Validate the optional Check-in rating ranges. 0..10 mirrors the
    # frontend control; None is allowed (attendee may submit attendance
    # without a check-in entry, or fill only one of the two categories).
    for _field, _value in (
        ("checkin_personal_rating", attendance_in.checkin_personal_rating),
        ("checkin_professional_rating", attendance_in.checkin_professional_rating),
    ):
        if _value is not None and not 0 <= _value <= 10:
            raise HTTPException(
                status_code=400, detail=f"{_field} must be between 0 and 10"
            )
    # The Conclude survey rating is 1..10 (a 0 would be meaningless
    # as a meeting score). None is allowed for the Check-in-only path.
    if attendance_in.rating is not None and not 1 <= attendance_in.rating <= 10:
        raise HTTPException(status_code=400, detail="rating must be between 1 and 10")

    if attendance:
        attendance.attended = attendance_in.attended
        # Only overwrite a field when the caller actually sent it. The
        # same upsert serves two flows: the start-of-meeting Check-in form
        # (sends checkin_*) and the Conclude survey (sends rating +
        # rating_comment). Guarding each field stops one flow from
        # nulling the other's data on the shared attendance row.
        if attendance_in.rating is not None:
            attendance.rating = attendance_in.rating
        if attendance_in.rating_comment is not None:
            attendance.rating_comment = attendance_in.rating_comment
        # Personal + Professional check-ins live in separate columns, so a
        # Personal submission never nulls a prior Professional one (the
        # overwrite bug this replaced). Each field is written only when the
        # caller actually sent it.
        if attendance_in.checkin_personal_content is not None:
            attendance.checkin_personal_content = attendance_in.checkin_personal_content
        if attendance_in.checkin_personal_rating is not None:
            attendance.checkin_personal_rating = attendance_in.checkin_personal_rating
        if attendance_in.checkin_professional_content is not None:
            attendance.checkin_professional_content = (
                attendance_in.checkin_professional_content
            )
        if attendance_in.checkin_professional_rating is not None:
            attendance.checkin_professional_rating = (
                attendance_in.checkin_professional_rating
            )
        attendance.updated_by = current_user.email  # Who submitted
    else:
        attendance = L10Attendance(
            **attendance_in.model_dump(), created_by=current_user.email
        )
        db.add(attendance)

    db.commit()
    db.refresh(attendance)
    return attendance


@router.get(
    "/sessions/{session_id}/attendance",
    response_model=list[L10AttendanceResponse],
)
def list_attendance(db: DBDep, current_user: CurrentUser, session_id: UUID):
    # An unknown session id stays permissive-empty (the frontend polls
    # fresh sessions before rows exist); a REAL session is access-gated so
    # the Exec L10's attendance/check-ins stay on the Exec team.
    session = (
        db.query(L10Session)
        .filter(L10Session.id == session_id, L10Session.is_deleted == False)  # noqa: E712
        .first()
    )
    if session is not None:
        _require_session_access(db, session, current_user)
    """Return every attendance row for a session — including check-in
    content/type/rating. Powers the Check-in segment's submitted list."""
    return (
        db.query(L10Attendance)
        .filter(
            L10Attendance.session_id == session_id,
            L10Attendance.is_deleted == False,  # noqa: E712
        )
        .all()
    )


# -- L10 live session state (shared cursor; polled every ~2s) --------------
#
# Server-authoritative meeting position so every participant sees the same
# step + timer. Same polling rationale as signals (l10.py "L10 Signals"
# block): a stateless GET that grabs/releases one connection — never a
# long-lived stream — which is what keeps a live meeting inside the
# small database's connection budget.


def _get_meeting_or_404(db, meeting_id, current_user) -> L10Meeting:
    """Fetch a live meeting the caller may ACCESS, else 404. Everything
    hanging off a meeting (sessions, attendance, headlines/ideas, items)
    inherits its team's confidentiality — the Exec L10 stays exec-only."""
    meeting = (
        db.query(L10Meeting)
        .filter(L10Meeting.id == meeting_id, L10Meeting.is_deleted == False)  # noqa: E712
        .first()
    )
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    require_meeting_access(current_user, meeting, db)
    return meeting


def _require_session_access(db, session, current_user) -> L10Meeting | None:
    """Access-gate a session via its parent meeting (404 on no access).
    Returns the meeting (None only for orphaned sessions)."""
    meeting = db.query(L10Meeting).filter(L10Meeting.id == session.meeting_id).first()
    if meeting is not None:
        require_meeting_access(current_user, meeting, db)
    return meeting


def _session_manager_or_403(db, session, current_user) -> None:
    """Allow only a holder of structural authority over the meeting (admin,
    org-wide Exec, Exec team for the Exec L10, the manager, or the meeting
    department's leadership) — or the session's creator as a fallback — to drive
    the shared cursor. The creator fallback avoids locking out a legitimate
    facilitator when the email→Person→manager_id lookup misses (e.g. the manager
    isn't in eos_people yet); whoever actually started today's session is
    running it.

    Case-folded via ``created_by``/``person_by_email``: comparing either key
    raw silently locks the real facilitator out of their own live session when
    their row was written with different casing.
    """
    if (
        session.created_by
        and session.created_by.strip().lower()
        == (current_user.email or "").strip().lower()
    ):
        return
    meeting = db.query(L10Meeting).filter(L10Meeting.id == session.meeting_id).first()
    if meeting is not None and can_manage_meeting(current_user, meeting, db):
        return
    raise HTTPException(
        status_code=403,
        detail="Only the meeting manager can advance the meeting.",
    )


def _current_quarter_ids(db) -> list:
    """Quarter ids to treat as "the current quarter" for the in-meeting
    Rock Review.

    Resolved by DATE first — every live quarter whose ``[start_date,
    end_date]`` window brackets *today* — because the stored ``is_current``
    flag only self-heals on the quarters / dashboard read paths
    (``sync_current_quarter``), so a meeting opened before either page can
    still see a stale flag (e.g. Q2 flagged current in July → empty Rock
    Review). Returning a LIST also tolerates duplicate rows for the same
    calendar quarter (seed + find-or-create races): rocks attached to any
    of them stay visible. Falls back to the flagged row(s) when no seeded
    window covers today, mirroring ``sync_current_quarter``'s "keep the
    last-known current" behavior."""
    now = datetime.now()
    live = (
        db.query(Quarter.id)
        .filter(
            Quarter.is_deleted == False,  # noqa: E712
            Quarter.start_date <= now,
            Quarter.end_date >= now,
        )
        .all()
    )
    if live:
        return [q_id for (q_id,) in live]
    flagged = (
        db.query(Quarter.id)
        .filter(
            Quarter.is_deleted == False,  # noqa: E712
            Quarter.is_current == True,  # noqa: E712
        )
        .all()
    )
    return [q_id for (q_id,) in flagged]


def _rock_team_filter(db, meeting):
    """Team predicate for Rocks in a meeting's Rock Review / items payload.

    Mirrors ``team_scope_filter`` (a department L10 sees its own team's rows;
    the Exec L10 sees Exec-team rows) — rocks were the one list team scoping
    never covered, so every team's L10 showed every other team's rocks. One
    extra tier: rocks created before ``eos_rocks.department_name`` existed
    (the column was added with NO backfill) carry a NULL tag, so a
    department meeting also accepts an untagged rock whose OWNER's seat sits
    in the meeting's department."""
    if getattr(meeting, "is_exec_meeting", False):
        return Rock.is_exec.is_(True)
    if meeting.department_name is None:
        return Rock.department_name.is_(None) & Rock.is_exec.is_(False)
    tagged = func.lower(Rock.department_name) == meeting.department_name.lower()
    legacy_owner_ids = _person_ids_in_depts({meeting.department_name}, db)
    if legacy_owner_ids:
        tagged = tagged | (
            Rock.department_name.is_(None) & Rock.owner_id.in_(legacy_owner_ids)
        )
    return tagged & Rock.is_exec.is_(False)


def _items_rev(db, meeting) -> datetime | None:
    """Cheap change-marker for the team's in-meeting lists: the latest
    ``updated_at`` across the team's To-Dos, Issues, Scorecard metrics, the
    current quarter's Rocks, and the meeting's headlines / parking-lot inputs.
    A client compares this to the value it last saw and refetches the items
    snapshot only when it moves.

    Inputs are keyed on the meeting rather than the team scope (they hang off
    ``meeting_id``), and they were missing from this marker until now — which
    is why a teammate's headline never appeared during a live session: the
    panel fetched once on mount and nothing ever told it to look again."""
    if meeting is None:
        return None
    todo_rev = (
        db.query(func.max(Todo.updated_at))
        .filter(team_scope_filter(Todo, meeting), Todo.is_deleted == False)  # noqa: E712
        .scalar()
    )
    issue_rev = (
        db.query(func.max(Issue.updated_at))
        .filter(team_scope_filter(Issue, meeting), Issue.is_deleted == False)  # noqa: E712
        .scalar()
    )
    metric_rev = (
        db.query(func.max(ScorecardMetric.updated_at))
        .filter(
            team_scope_filter(ScorecardMetric, meeting),
            ScorecardMetric.is_deleted == False,  # noqa: E712
        )
        .scalar()
    )
    rock_rev = None
    quarter_ids = _current_quarter_ids(db)
    if quarter_ids:
        rock_rev = (
            db.query(func.max(Rock.updated_at))
            .filter(
                Rock.quarter_id.in_(quarter_ids),
                _rock_team_filter(db, meeting),
                Rock.is_deleted == False,  # noqa: E712
            )
            .scalar()
        )
    input_rev = (
        db.query(func.max(L10Input.updated_at))
        .filter(
            L10Input.meeting_id == meeting.id,
            L10Input.is_deleted == False,  # noqa: E712
        )
        .scalar()
    )
    candidates = [
        t
        for t in (todo_rev, issue_rev, metric_rev, rock_rev, input_rev)
        if t is not None
    ]
    return max(candidates) if candidates else None


@router.get("/sessions/{session_id}/state", response_model=L10SessionState)
def get_session_state(session_id: UUID, db: DBDep, current_user: CurrentUser):
    """Shared live cursor for a session. Polled by every participant; the
    timer is delivered as an anchor + ``server_now`` so clients compute the
    countdown locally and self-correct for clock skew. Works after the
    session ends (so a stale tab learns it's over via ``actual_end``); the
    POST is the firewall against late writes."""
    session = (
        db.query(L10Session)
        .filter(L10Session.id == session_id, L10Session.is_deleted == False)  # noqa: E712
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    meeting = db.query(L10Meeting).filter(L10Meeting.id == session.meeting_id).first()
    if meeting is not None:
        require_meeting_access(current_user, meeting, db)
    attendance_rev = (
        db.query(func.max(L10Attendance.updated_at))
        .filter(
            L10Attendance.session_id == session_id,
            L10Attendance.is_deleted == False,  # noqa: E712
        )
        .scalar()
    )
    return L10SessionState(
        session_id=session.id,
        current_step=session.current_step or 0,
        actual_start=session.actual_start,
        step_started_at=session.step_started_at,
        is_paused=bool(session.is_paused),
        state_version=session.state_version or 0,
        manager_id=meeting.manager_id if meeting is not None else None,
        server_now=datetime.utcnow(),
        actual_end=session.actual_end,
        attendance_rev=attendance_rev,
        items_rev=_items_rev(db, meeting),
    )


@router.post("/sessions/{session_id}/state", response_model=L10SessionResponse)
def advance_session_state(
    session_id: UUID,
    db: DBDep,
    current_user: CurrentUser,
    advance_in: L10StepAdvance,
):
    """Manager-only: move every participant to ``current_step`` and re-anchor
    the timer (to ``step_started_at`` if given, else now). Bumps
    ``state_version`` so pollers notice. Refuses once the session has ended."""
    session = (
        db.query(L10Session)
        .filter(L10Session.id == session_id, L10Session.is_deleted == False)  # noqa: E712
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    _require_session_access(db, session, current_user)
    if session.actual_end is not None:
        raise HTTPException(
            status_code=409,
            detail="Session has already ended; the cursor is session-scoped.",
        )

    _session_manager_or_403(db, session, current_user)

    # Defensive bound — the agenda is short; reject obviously bogus indices
    # without coupling the API to the frontend's exact step count.
    if not 0 <= advance_in.current_step <= 100:
        raise HTTPException(status_code=422, detail="current_step out of range")

    session.current_step = advance_in.current_step
    session.step_started_at = advance_in.step_started_at or datetime.utcnow()
    session.state_version = (session.state_version or 0) + 1
    session.updated_by = current_user.email
    db.commit()
    db.refresh(session)

    response = L10SessionResponse.model_validate(session)
    # Caller passed the manager gate above.
    response.is_manager = True
    return response


#: Who may restart a live L10 (reset the agenda to the Segue with a fresh
#: timer): Admin → Feature Access data, judged on the person, never on the
#: meeting's team. Ships to admins only (rubik_core/features/registry.py).
RESTART_FEATURE_KEY = "l10_meeting_restart"


@router.post("/sessions/{session_id}/restart", response_model=L10SessionResponse)
def restart_session(session_id: UUID, db: DBDep, current_user: CurrentUser):
    """Manager-only: restart a live L10 — snap every participant back to the
    first agenda step (Segue) with a fresh timer, without creating a new
    session or discarding attendance/notes already captured. Handy after a
    false start, or when re-running the meeting during a demo.

    "Fresh timer" means BOTH clocks: the step countdown (``step_started_at``)
    and the meeting-level clock (``actual_start``), which the master pace bar
    reads to draw elapsed against the 90-minute box. Resetting only the step
    anchor left a restarted meeting claiming it was hours over its box — the
    thing a restart is meant to clear.

    Guards, in order:
      * session must exist and still be live (``actual_end`` is None) — a
        concluded session's cursor is frozen (409),
      * caller must be able to ACCESS the parent meeting (404 hides existence,
        so a non-exec can't probe Exec meeting ids),
      * caller must be the meeting's manager, an admin, or the session's
        creator (the same gate that drives the shared cursor),
      * never the Exec team's L10 (403), whoever asks,
      * the caller must be on the ``l10_meeting_restart`` feature
        (:data:`RESTART_FEATURE_KEY`, Admin → Feature Access; admins only by
        default), judged on the person rather than the meeting's team (403).
    """
    session = (
        db.query(L10Session)
        .filter(L10Session.id == session_id, L10Session.is_deleted == False)  # noqa: E712
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.actual_end is not None:
        raise HTTPException(
            status_code=409,
            detail="Session has already ended; start a new one instead of restarting.",
        )
    meeting = _require_session_access(db, session, current_user)
    _session_manager_or_403(db, session, current_user)

    if meeting is not None and meeting.is_exec_meeting:
        raise HTTPException(
            status_code=403,
            detail="The Exec team's L10 can't be restarted.",
        )
    from rubik_core.features import feature_allows  # local: keep the router light

    if not feature_allows(RESTART_FEATURE_KEY, current_user, db):
        raise HTTPException(
            status_code=403,
            detail=(
                "Restarting a live meeting isn't turned on for you. An admin can "
                "turn it on in Admin → Feature Access (Restart a live L10)."
            ),
        )

    # Snap back to the top: everyone returns to the Segue (step 0) with both
    # clocks re-anchored to now and any pause cleared. ``actual_start`` moves
    # with ``step_started_at`` (same instant, so the pace bar reads zero
    # variance) — a restart re-declares when the meeting began, which is what
    # makes "90:00 left of 90m" true again instead of carrying the false
    # start's elapsed time. History keeps the same session row: attendance,
    # check-ins and notes are untouched, and the diary simply reports the
    # restarted start time. Bump ``state_version`` so every poller notices and
    # re-syncs on its next ~2s tick — the same mechanism the step-advance
    # endpoint uses.
    restarted_at = datetime.utcnow()
    session.current_step = 0
    session.actual_start = restarted_at
    session.step_started_at = restarted_at
    session.is_paused = False
    session.state_version = (session.state_version or 0) + 1
    session.updated_by = current_user.email
    db.commit()
    db.refresh(session)

    response = L10SessionResponse.model_validate(session)
    response.is_manager = True  # caller passed the manager gate above
    return response


@router.get("/sessions/{session_id}/survey")
def get_session_survey(session_id: UUID, db: DBDep, current_user: CurrentUser):
    """Per-person meeting-survey results for a session.

    Returns each attendee who submitted a rating and/or comment, enriched
    with their name and a manager-vs-participant flag, plus the derived
    average. Powers the individual-survey breakdown shown on the dashboard
    and the Meetings diary — the request was that participants AND the
    manager each fill it out, and that the result is visible afterward.
    """
    session = (
        db.query(L10Session)
        .filter(
            L10Session.id == session_id,
            L10Session.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    meeting = db.query(L10Meeting).filter(L10Meeting.id == session.meeting_id).first()
    if meeting is not None:
        require_meeting_access(current_user, meeting, db)
    manager_id = meeting.manager_id if meeting else None

    rows = (
        db.query(L10Attendance)
        .filter(
            L10Attendance.session_id == session_id,
            L10Attendance.is_deleted == False,  # noqa: E712
        )
        .all()
    )
    person_ids = [r.person_id for r in rows]
    people = (
        {p.id: p for p in db.query(Person).filter(Person.id.in_(person_ids)).all()}
        if person_ids
        else {}
    )

    results = []
    ratings: list[int] = []
    for r in rows:
        # Only surface attendees who actually submitted the survey (a
        # rating and/or a written comment) — a bare attendance / check-in
        # row is not a survey response.
        if r.rating is None and not (r.rating_comment or "").strip():
            continue
        p = people.get(r.person_id)
        name = f"{p.first_name} {p.last_name}".strip() if p else None
        stamp = r.updated_at or r.created_at
        results.append(
            {
                "person_id": str(r.person_id),
                "person_name": name or (p.email if p else None),
                "person_email": p.email if p else None,
                "is_manager": manager_id is not None and r.person_id == manager_id,
                "rating": r.rating,
                "comment": r.rating_comment,
                "submitted_at": stamp.isoformat() if stamp else None,
            }
        )
        if r.rating is not None:
            ratings.append(r.rating)

    average = round(sum(ratings) / len(ratings), 1) if ratings else None
    return {
        "session_id": str(session_id),
        "average_rating": average,
        "submitted_count": len(results),
        "results": results,
    }


# -- AI recap of a finished session -----------------------------------


def _run_l10_recap_model(prompt: str) -> str:
    """Pin and revalidate this workspace's text route for one recap."""
    route = resolve_model_route(
        "l10_recap", "text", default_model=settings.VERTEX_LLM_MODEL_PRO
    )
    return generate_text_on_route(route, prompt).text


async def _generate_l10_recap(db, session) -> str:
    """Build a short narrative recap of a concluded L10 session.

    Grounds the model strictly in the session's own record (see
    ``backend/services/l10_session_digest.py``, which owns that assembly and is
    shared with the post-meeting summary email) so it can't invent outcomes.

    Returns the model's plain-text narrative. Raises ``HTTPException(502)`` if
    the LLM call fails.
    """
    from backend.services.l10_session_digest import build_session_digest

    digest = build_session_digest(db, session)

    # Nothing substantive recorded → skip the LLM (save cost + avoid padded
    # filler) and return an honest one-liner. The endpoint caches this like any
    # other recap, so it isn't re-generated on every view.
    if not digest.has_material:
        ended = (
            session.actual_end.strftime("%Y-%m-%d %H:%M")
            if session.actual_end
            else "unknown"
        )
        return (
            f"This L10 was concluded on {ended}. No closing "
            "notes, ratings, solved issues, or new to-dos were recorded for it, so "
            "there's nothing to summarize."
        )

    prompt = (
        "You are summarizing a finished EOS Level 10 (L10) leadership team "
        "meeting. Write a concise recap of 2 to 5 sentences for the leadership "
        "team — use fewer if little was recorded, and never pad. Cover: what was "
        "decided or solved, what new to-dos were created "
        "and who owns them, the overall meeting rating plus any notably low "
        "individual rating or critical comment, and anything explicitly carrying "
        "into next week. Ground every statement ONLY in the data below — do not "
        "invent issues, names, numbers, or commitments that are not present. "
        "Write in plain prose (no headings, no bullet lists).\n\n"
        "=== MEETING DATA ===\n"
        f"{digest.data_block}\n"
        "=== END DATA ===\n"
    )

    try:
        text = (await run_in_threadpool(_run_l10_recap_model, prompt)).strip()
    except IncompatibleModelRoute:
        raise HTTPException(
            status_code=502,
            detail="The selected AI provider is unavailable for L10 recaps. Ask a workspace admin to check its model and credentials.",
        ) from None
    except Exception:
        # Provider exceptions may quote the prompt, the meeting record or a
        # credential. Keep the HTTP response bounded and free of those details.
        raise HTTPException(
            status_code=502,
            detail="L10 recap generation failed. Please retry or ask a workspace admin to check the AI provider.",
        ) from None
    if not text:
        raise HTTPException(
            status_code=502,
            detail="The AI provider returned an empty L10 recap. Please retry.",
        )
    return text


@router.get("/sessions/{session_id}/recap", response_model=L10RecapResponse)
@attribute_to("l10_recap")
async def get_session_recap(session_id: UUID, db: DBDep, current_user: CurrentUser):
    """The AI recap for a concluded L10 session.

    Read access mirrors the other per-session reads (survey / attendance):
    ``_require_session_access`` gates on the parent meeting, so the Exec L10's
    recap stays exec-only and a department's stays on that team. Lazily fills
    the cached ``session.recap`` on first read; returns it thereafter.
    """
    session = (
        db.query(L10Session)
        .filter(L10Session.id == session_id, L10Session.is_deleted == False)  # noqa: E712
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    _require_session_access(db, session, current_user)

    # Feature off → surface nothing and never spend an LLM call.
    if not settings.CUBIE_L10_AI_ENABLED:
        return L10RecapResponse(recap=None)
    if session.actual_end is None:
        raise HTTPException(
            status_code=400,
            detail="Recap is only available for concluded meetings.",
        )
    # Cache hit: return the stored narrative as-is.
    if session.recap:
        return L10RecapResponse(recap=session.recap)

    text = await _generate_l10_recap(db, session)
    session.recap = text
    db.commit()
    return L10RecapResponse(recap=text)


@router.post("/sessions/{session_id}/recap/regenerate", response_model=L10RecapResponse)
@attribute_to("l10_recap")
async def regenerate_session_recap(
    session_id: UUID, db: DBDep, current_user: CurrentUser
):
    """Force a fresh recap for a concluded session (manager/admin only).

    Ignores any cached value and rewrites ``session.recap``. Authorization
    mirrors ``update_session`` (PATCH /sessions/{id}): admin OR the meeting's
    current manager — a plain attendee can read the recap but not regenerate it.
    """
    session = (
        db.query(L10Session)
        .filter(L10Session.id == session_id, L10Session.is_deleted == False)  # noqa: E712
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    _require_session_access(db, session, current_user)

    meeting = db.query(L10Meeting).filter(L10Meeting.id == session.meeting_id).first()
    if meeting is None or not can_manage_meeting(current_user, meeting, db):
        raise HTTPException(
            status_code=403,
            detail=(
                "Only the meeting manager, your team's leader, or an admin can "
                "regenerate the recap."
            ),
        )

    if not settings.CUBIE_L10_AI_ENABLED:
        raise HTTPException(status_code=400, detail="L10 AI is disabled.")
    if session.actual_end is None:
        raise HTTPException(
            status_code=400,
            detail="Recap is only available for concluded meetings.",
        )

    text = await _generate_l10_recap(db, session)
    session.recap = text
    session.updated_by = current_user.email  # Attribute the regenerate
    db.commit()
    return L10RecapResponse(recap=text)


# -- Aggregated views -------------------------------------------------


def _attendee_pool_emails(db, meeting) -> list[str]:
    """Emails that should populate the in-meeting Check-in attendee picker.

    Priority: the actual calendar invitees (``meeting.roster_emails``). If a
    meeting has no imported roster (manually-scheduled meetings have none),
    fall back to the meeting's TEAM so the picker is scoped to the right
    people instead of every user in the system:

    - Exec/Leadership L10 → all Exec-team members (``users.is_exec``).
    - Department L10 → all users in that department.

    Mirrors ``team_scope_filter`` but over the main ``users`` table (the
    source of truth for people; ``eos_people`` is synced from it). Returns
    ``[]`` only when the team genuinely has no members, in which case the
    frontend keeps its final fallback to the full list.
    """
    roster = meeting.roster_emails or []
    if roster:
        support = {
            (email or "").strip().lower()
            for (email,) in db.query(User.email)
            .filter(
                User.is_platform_support.is_(True),
                func.lower(User.email).in_(
                    [(email or "").strip().lower() for email in roster]
                ),
            )
            .all()
        }
        return [
            email
            for email in roster
            if isinstance(email, str) and email.strip().lower() not in support
        ]
    if getattr(meeting, "is_exec_meeting", False):
        rows = (
            db.query(User.email)
            .filter(User.is_exec.is_(True), User.is_platform_support.is_(False))
            .all()
        )
    elif meeting.department_name:
        rows = (
            db.query(User.email)
            .filter(
                func.lower(User.department) == meeting.department_name.lower(),
                User.is_platform_support.is_(False),
            )
            .all()
        )
    else:
        rows = []
    return [email for (email,) in rows if email]


def _attendee_people(db, emails: list[str]) -> list[dict]:
    """The attendee pool resolved to ``eos_people`` rows (PersonLite shape).

    The in-meeting pickers (Check-in, Conclude, Transfer management) used to
    render the *intersection* of this pool and the caller's own owner roster
    from ``GET /api/eos/people`` — which is scoped to the caller's team. Any
    attendee sitting outside it was silently dropped: a cross-department
    director who leads this team, or an invited guest from another department.
    They were in the room, on the calendar roster, and still unpickable — and
    a check-in they'd already recorded rendered as "—" because the frontend
    had no name for their ``person_id``.

    Resolving them here lets the meeting offer exactly who is in the room.
    Emails with no ``eos_people`` row yet (never-synced or external invitees)
    are simply absent; soft-deleted rows — which is what the people sync marks
    impersonation pseudo-users as — are excluded by the same filter.
    """
    keys = {e.strip().lower() for e in (emails or []) if e and e.strip()}
    if not keys:
        return []
    people = (
        db.query(Person)
        .filter(
            Person.is_deleted == False,  # noqa: E712
            func.lower(Person.email).in_(keys),
        )
        .all()
    )
    return [
        {
            "id": str(p.id),
            "first_name": p.first_name,
            "last_name": p.last_name,
            "email": p.email,
        }
        for p in people
    ]


@router.get("/meetings/{meeting_id}/items")
def get_meeting_items(meeting_id: UUID, db: DBDep, current_user: CurrentUser):
    meeting = _get_meeting_or_404(db, meeting_id, current_user)

    # Scope by the meeting's TEAM (department or Exec), so metrics
    # added on the standalone Scorecard page surface in the team's L10.
    metrics = (
        db.query(ScorecardMetric)
        .filter(
            team_scope_filter(ScorecardMetric, meeting),
            ScorecardMetric.is_deleted == False,  # noqa: E712
        )
        .all()
    )

    # Current-quarter rocks for the meeting's TEAM. Quarter resolution is
    # date-based with an ``is_current`` fallback (see ``_current_quarter_ids``
    # — the flag can be stale/duplicated when a meeting opens before any
    # quarters read has self-healed it, which blanked the Rock Review). Team
    # scoping mirrors To-Dos / Issues / Scorecard above: a department L10
    # reviews ITS rocks — not every team's — and Exec-team ("ET") rocks
    # belong to the Exec L10 only (see ``_rock_team_filter``).
    rocks: list[Rock] = []
    quarter_ids = _current_quarter_ids(db)
    if quarter_ids:
        rock_q = db.query(Rock).filter(
            Rock.quarter_id.in_(quarter_ids),
            _rock_team_filter(db, meeting),
            Rock.is_deleted == False,  # noqa: E712
        )
        # Eager-load milestones (now a relationship, not a JSON column) so
        # serializing each rock's milestones doesn't fire one SELECT per rock.
        rocks = rock_q.options(selectinload(Rock.milestones)).all()

    # The L10 reviews the TEAM's open lists (pre-work included), not
    # just items created inside this one meeting instance. To-Dos → the
    # team's still-pending commitments; Issues → the team's OPEN issues
    # (the IDS list). Both scoped by team (department or Exec).
    todos = (
        db.query(Todo)
        .filter(
            team_scope_filter(Todo, meeting),
            Todo.status == TodoStatus.PENDING,
            Todo.is_deleted == False,  # noqa: E712
        )
        .all()
    )
    issues = (
        db.query(Issue)
        .filter(
            team_scope_filter(Issue, meeting),
            Issue.status == IssueStatus.OPEN,
            Issue.is_deleted == False,  # noqa: E712
        )
        .all()
    )

    # Rock Review enrichment — the standalone Rock model only carries
    # the rock itself. The facilitator needs to read the owner's pre-meeting update
    # context (latest comment, last 3 dated entries) without opening
    # the detail modal. Eager-load both owners and recent updates in
    # bulk to avoid an N+1 fan-out from the frontend.
    rock_ids = [r.id for r in rocks]
    owners_by_id: dict = {}
    if rocks:
        owner_ids = list({r.owner_id for r in rocks if r.owner_id})
        if owner_ids:
            owner_rows = (
                db.query(Person)
                .filter(Person.id.in_(owner_ids), Person.is_deleted == False)  # noqa: E712
                .all()
            )
            owners_by_id = {p.id: p for p in owner_rows}

    updates_by_rock: dict[str, list[RockWeeklyUpdate]] = {}
    if rock_ids:
        # Pull the last 3 weekly updates per rock — enough for the
        # in-meeting view ("last week + the two before") without
        # blowing up the payload. The frontend caps display at 3 too;
        # the detail modal goes deeper when needed.
        rolling_window = (
            db.query(RockWeeklyUpdate)
            .filter(
                RockWeeklyUpdate.rock_id.in_(rock_ids),
                RockWeeklyUpdate.is_deleted == False,  # noqa: E712
            )
            .order_by(
                RockWeeklyUpdate.rock_id,
                RockWeeklyUpdate.update_date.desc(),
            )
            .all()
        )
        for u in rolling_window:
            bucket = updates_by_rock.setdefault(str(u.rock_id), [])
            if len(bucket) < 3:
                bucket.append(u)

    def _owner_label(person: Person | None) -> str | None:
        if person is None:
            return None
        parts = [
            (person.first_name or "").strip(),
            (person.last_name or "").strip(),
        ]
        return " ".join(p for p in parts if p) or person.email

    def _serialize_update(u: RockWeeklyUpdate) -> dict:
        return {
            "id": str(u.id),
            "status": u.status.value if hasattr(u.status, "value") else str(u.status),
            "percent_complete": u.percent_complete,
            "comment": u.comment,
            "update_date": u.update_date.isoformat() if u.update_date else None,
            "created_by": u.created_by,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        }

    def _serialize_milestone(m) -> dict:
        # Milestones now live in eos_rock_milestones (was a JSON column on the
        # rock). Same inline shape the card already consumed, plus id/sort_order.
        return {
            "id": str(m.id),
            "description": m.description,
            "completed": bool(m.completed),
            "due_date": m.due_date.isoformat() if m.due_date else None,
            "sort_order": m.sort_order,
        }

    rocks_enriched: list[dict] = []
    for r in rocks:
        owner = owners_by_id.get(r.owner_id) if r.owner_id else None
        recent_updates = updates_by_rock.get(str(r.id), [])
        last_update = recent_updates[0] if recent_updates else None
        rocks_enriched.append(
            {
                "id": str(r.id),
                "description": r.description,
                "status": r.status.value
                if hasattr(r.status, "value")
                else str(r.status),
                "visibility": (
                    r.visibility.value
                    if hasattr(r.visibility, "value")
                    else str(r.visibility)
                ),
                "owner_id": str(r.owner_id) if r.owner_id else None,
                "owner_name": _owner_label(owner),
                "owner_email": owner.email if owner else None,
                "quarter_id": str(r.quarter_id) if r.quarter_id else None,
                "milestones": [_serialize_milestone(m) for m in r.milestones],
                # Computed previews for the in-meeting card so the
                # frontend doesn't have to refetch /rocks/{id}/updates
                # just to render a single line of context.
                "last_update": _serialize_update(last_update) if last_update else None,
                "recent_updates": [_serialize_update(u) for u in recent_updates],
                "update_count_recent": len(recent_updates),
            }
        )

    # Resolved once — the emails go out for the picker's roster filter and the
    # people for its options (see ``_attendee_people``).
    attendee_emails = _attendee_pool_emails(db, meeting)

    return {
        # Lightweight meeting payload so callers (e.g. the L10Session
        # sidebar) don't need a second GET. Includes the secondary link slot.
        "meeting": {
            "id": str(meeting.id),
            "name": meeting.name,
            "department_name": meeting.department_name,
            "is_exec_meeting": meeting.is_exec_meeting,
            "secondary_link_label": meeting.secondary_link_label,
            "secondary_link_url": meeting.secondary_link_url,
            # Lets the Conclude survey flag the manager's response.
            "manager_id": str(meeting.manager_id) if meeting.manager_id else None,
            # The meeting's invitee roster (emails from the imported calendar
            # event), so the in-meeting Check-in picker can show only people
            # invited to this meeting. Null for manually-scheduled meetings.
            "roster_emails": meeting.roster_emails,
            # The effective attendee pool for the Check-in picker: the invitee
            # roster if there is one, else the meeting's TEAM (department or
            # Exec members). Lets the picker scope to the right people even
            # when there's no imported calendar roster, instead of listing
            # every user in the system.
            "attendee_emails": attendee_emails,
            # The same pool resolved to people, so the pickers can name and
            # offer an attendee who sits outside the CALLER's team (a
            # cross-department leader of this team, or an invited guest)
            # instead of quietly dropping them.
            "attendees": _attendee_people(db, attendee_emails),
        },
        "metrics": metrics,
        "rocks": rocks_enriched,
        "todos": todos,
        "issues": issues,
    }


@router.get(
    "/meetings/{meeting_id}/issues/history",
    response_model=list[IssueResponse],
)
def list_meeting_solved_issues(
    meeting_id: UUID,
    db: DBDep,
    current_user: CurrentUser,
    days: int = 90,
):
    """Issues this team SOLVED over a trailing window, newest-solved first.

    ``/items`` deliberately returns only OPEN issues — the IDS list is
    today's work, not the archive — and it should stay that way. But that
    left the live IDS step blind to its own outcomes: an issue solved in
    the room left the client on the next items poll, so the "Solved this
    week" tile read 0 immediately after a team solved three of them, and
    there was no way to look back at what was closed last month.

    So this is the Issues twin of ``/inputs/history`` (the Headlines
    step's 30-day lookback): same trailing-window shape, the same team
    scoping as ``/items`` (a department L10 sees its department's issues,
    the Exec L10 its own), behind the same meeting-access gate.

    Rows solved before ``solved_at`` was stamped carry NULL and are left
    out — there is no date to window them by or to show on the row.
    """
    meeting = _get_meeting_or_404(db, meeting_id, current_user)

    # Clamp: 0/negative → the default, and never more than a year. The
    # in-meeting drawer offers 30/60/90; the team's full archive lives on
    # the Issues page, which loads every status it may read.
    days = 90 if days <= 0 else min(days, 365)
    cutoff = datetime.utcnow() - timedelta(days=days)

    return (
        db.query(Issue)
        .filter(
            team_scope_filter(Issue, meeting),
            Issue.status == IssueStatus.SOLVED,
            Issue.is_deleted == False,  # noqa: E712
            Issue.solved_at.isnot(None),
            Issue.solved_at >= cutoff,
        )
        .order_by(Issue.solved_at.desc())
        .all()
    )


@router.post("/sessions/{session_id}/rollover")
def rollover_session_items(session_id: UUID, db: DBDep, current_user: CurrentUser):
    session = db.query(L10Session).filter(L10Session.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    meeting = db.query(L10Meeting).filter(L10Meeting.id == session.meeting_id).first()
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    require_meeting_access(current_user, meeting, db)

    # Count the TEAM's carry-over (matches what the meeting shows),
    # not just items keyed to this meeting instance. Typed enums — upstream's
    # uppercase literals never matched our lowercase column values.
    pending_todos = (
        db.query(Todo)
        .filter(
            team_scope_filter(Todo, meeting),
            Todo.status == TodoStatus.PENDING,
            Todo.is_deleted == False,  # noqa: E712
        )
        .count()
    )
    open_issues = (
        db.query(Issue)
        .filter(
            team_scope_filter(Issue, meeting),
            Issue.status == IssueStatus.OPEN,
            Issue.is_deleted == False,  # noqa: E712
        )
        .count()
    )

    return {
        "status": "success",
        "rolled_over_todos": pending_todos,
        "rolled_over_issues": open_issues,
    }


# -- L10 Signals — ephemeral broadcasts during a live session -
#
# Polling-based on purpose. Cloud Run doesn't love long-lived SSE/WS
# connections and EOS L10s are at most ~10 participants per session,
# so a 2s GET poll keyed by ``since`` is dirt-cheap and tolerates the
# wonky office WiFi the team actually uses without re-handshake pain.


@router.post(
    "/sessions/{session_id}/signals",
    response_model=L10SignalResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_session_signal(
    session_id: UUID,
    db: DBDep,
    current_user: CurrentUser,
    signal_in: L10SignalCreate,
):
    """Broadcast an in-meeting signal (rabbit-hole, etc.) to every
    participant of an active session. Anyone in the session can fire
    one — the rabbit-hole instigator role is shared, not manager-only,
    matching the EOS spec ("anybody can call out a rabbit hole").

    The session must be live (no ``actual_end`` set) — refusing to
    broadcast after the session is closed keeps zombie polls from a
    forgotten browser tab from firing rabbits at the next meeting.
    """
    session = (
        db.query(L10Session)
        .filter(L10Session.id == session_id, L10Session.is_deleted == False)  # noqa: E712
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    _require_session_access(db, session, current_user)
    if session.actual_end is not None:
        raise HTTPException(
            status_code=409,
            detail="Session has already ended; signals are session-scoped",
        )

    kind = (signal_in.kind or "").strip()
    if not kind:
        raise HTTPException(status_code=422, detail="kind must not be empty")
    # Loose guard so a malicious / fat-fingered client can't shove a
    # megabyte of "kind" into the table. 64 chars is plenty for the
    # discriminator pattern we use.
    if len(kind) > 64:
        raise HTTPException(status_code=422, detail="kind too long (max 64)")

    row = L10Signal(
        session_id=session_id,
        kind=kind,
        created_by=current_user.email,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.get(
    "/sessions/{session_id}/signals",
    response_model=list[L10SignalResponse],
)
def list_session_signals(
    session_id: UUID,
    db: DBDep,
    current_user: CurrentUser,
    since: datetime | None = None,
    limit: int = 20,
):
    """Polling endpoint — return signals fired since ``since`` (ISO 8601).

    Client tracks the timestamp of the latest signal it has rendered
    and passes it on the next tick; server returns only what's newer.
    Capped at ``limit`` so a pathological burst can't blow the
    response. Newest-last so the client can iterate in fire order.
    """
    # Session must exist; don't bother with ``actual_end`` here — even
    # after a session ends, clients may want to fetch the last few
    # signals for a quick replay. The POST endpoint is the firewall
    # against late writes.
    session = (
        db.query(L10Session)
        .filter(L10Session.id == session_id, L10Session.is_deleted == False)  # noqa: E712
        .first()
    )
    if session is not None:
        _require_session_access(db, session, current_user)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if limit <= 0 or limit > 100:
        limit = 20

    query = db.query(L10Signal).filter(
        L10Signal.session_id == session_id,
        L10Signal.is_deleted == False,  # noqa: E712
    )
    if since is not None:
        query = query.filter(L10Signal.created_at > since)

    rows = query.order_by(L10Signal.created_at.desc()).limit(limit).all()
    # Server kept them DESC for the limit; flip ASC so the client
    # processes them in fire order (oldest first).
    return list(reversed(rows))


# -- L10 Inputs — Headlines + Ideas ----------------------------
#
# Anyone on the team can drop a headline or idea on the next L10 at any
# point before the session starts. During the session they surface in
# the matching panel (Headlines step). After the session a manager can
# flip ``discussed=True`` to fade items without losing history.


@router.get(
    "/meetings/{meeting_id}/inputs",
    response_model=list[L10InputResponse],
)
def list_meeting_inputs(
    meeting_id: UUID,
    db: DBDep,
    current_user: CurrentUser,
    kind: L10InputKind | None = None,
    include_discussed: bool = True,
):
    """List headlines + ideas queued on a meeting. Filter by ``kind``
    to fetch just one panel. ``include_discussed=False`` hides items
    a manager has already flagged as covered."""
    meeting = _get_meeting_or_404(db, meeting_id, current_user)

    query = db.query(L10Input).filter(
        L10Input.meeting_id == meeting_id,
        L10Input.is_deleted == False,  # noqa: E712
    )
    if kind is not None:
        query = query.filter(L10Input.kind == kind)
    if not include_discussed:
        query = query.filter(L10Input.discussed == False)  # noqa: E712
    return query.order_by(L10Input.created_at.asc()).all()


@router.get(
    "/meetings/{meeting_id}/inputs/history",
    response_model=list[L10InputResponse],
)
def list_meeting_inputs_history(
    meeting_id: UUID,
    db: DBDep,
    current_user: CurrentUser,
    days: int = 30,
    kind: L10InputKind | None = None,
):
    """Headlines + ideas window for the in-meeting "recent context" view.

    The live Headlines step needs more than just this week's queue — the
    team often wants to recall what was raised over the prior month ("three weeks ago we
    said X, has that moved?"). This endpoint returns the trailing
    ``days``-day window, newest-first, including items already marked
    ``discussed=True`` so the historical context is intact.

    Distinct from ``/inputs`` (which orders ASC and defaults to the
    full unbounded list) so the in-meeting drawer can fetch a bounded
    lookback without disrupting the active queue's ordering / pagination
    behavior elsewhere in the app.
    """
    meeting = _get_meeting_or_404(db, meeting_id, current_user)

    if days <= 0:
        days = 30
    cutoff = datetime.utcnow() - timedelta(days=days)

    query = db.query(L10Input).filter(
        L10Input.meeting_id == meeting_id,
        L10Input.is_deleted == False,  # noqa: E712
        L10Input.created_at >= cutoff,
    )
    if kind is not None:
        query = query.filter(L10Input.kind == kind)
    return query.order_by(L10Input.created_at.desc()).all()


@router.post(
    "/meetings/{meeting_id}/inputs",
    response_model=L10InputResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_meeting_input(
    meeting_id: UUID,
    db: DBDep,
    current_user: CurrentUser,
    input_in: L10InputCreate,
):
    """Add one headline or idea to a meeting.

    The URL ``meeting_id`` wins over the body's ``meeting_id`` so a
    client mistake can't silently file the input against the wrong
    meeting (we'd rather refuse than mis-route)."""
    meeting = _get_meeting_or_404(db, meeting_id, current_user)
    if input_in.meeting_id != meeting_id:
        raise HTTPException(
            status_code=400,
            detail="meeting_id in body must match the URL path",
        )

    if not input_in.text or not input_in.text.strip():
        raise HTTPException(status_code=422, detail="text must not be empty")

    row = L10Input(
        meeting_id=meeting_id,
        kind=input_in.kind,
        text=input_in.text.strip(),
        discussed=input_in.discussed,
        created_by=current_user.email,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.patch("/inputs/{input_id}", response_model=L10InputResponse)
def update_meeting_input(
    input_id: UUID,
    db: DBDep,
    current_user: CurrentUser,
    input_in: L10InputUpdate,
):
    """Edit an input — typically flipping ``discussed`` from the L10
    Headlines panel as the team covers each item."""
    row = (
        db.query(L10Input)
        .filter(L10Input.id == input_id, L10Input.is_deleted == False)  # noqa: E712
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Input not found")
    _get_meeting_or_404(db, row.meeting_id, current_user)

    for field, value in input_in.model_dump(exclude_unset=True).items():
        setattr(row, field, value)
    row.updated_by = current_user.email  # Who marked it discussed/edited
    db.commit()
    db.refresh(row)
    return row


@router.delete("/inputs/{input_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_meeting_input(input_id: UUID, db: DBDep, current_user: CurrentUser):
    row = (
        db.query(L10Input)
        .filter(L10Input.id == input_id, L10Input.is_deleted == False)  # noqa: E712
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Input not found")
    _get_meeting_or_404(db, row.meeting_id, current_user)
    row.is_deleted = True
    row.updated_by = current_user.email  # Attribute the delete
    db.commit()
    return None


def _resolve_convert_owner(
    db, payload: L10InputConvertRequest, current_user, *, require_owner: bool
) -> UUID | None:
    """Resolve the owner for a convert: ``owner_email`` → ``owner_id`` → (for
    To-Dos only) the caller's own Person. A directly-supplied ``owner_id`` is
    validated to exist so a bad id 400s cleanly instead of hitting an FK 500.

    ``require_owner`` is True for To-Dos (``Todo.owner_id`` is NOT NULL, so one
    of the sources must land) and False for Issues (owner is optional — None is
    a valid unassigned issue)."""
    owner_id = payload.owner_id
    if payload.owner_email:
        person = (
            db.query(Person)
            .filter(
                Person.email == payload.owner_email,
                Person.is_deleted == False,  # noqa: E712
            )
            .first()
        )
        if not person:
            raise HTTPException(status_code=400, detail="Owner email not found")
        owner_id = person.id
    elif owner_id is not None:
        # Validate a directly-passed id so a stale/deleted owner 400s here
        # rather than surfacing as an opaque FK violation on commit.
        person = (
            db.query(Person)
            .filter(
                Person.id == owner_id,
                Person.is_deleted == False,  # noqa: E712
            )
            .first()
        )
        if not person:
            raise HTTPException(status_code=400, detail="Owner not found")
    if owner_id is None and require_owner:
        # Case-folded (``person_by_email``): a raw match drops the caller's own
        # id here and the convert fails with "pass owner_email" for someone who
        # does have an EOS row.
        caller = person_by_email(db, current_user.email)
        owner_id = caller.id if caller else None
    if owner_id is None and require_owner:
        raise HTTPException(
            status_code=400,
            detail="Could not resolve a to-do owner — pass owner_email.",
        )
    return owner_id


@router.post(
    "/inputs/{input_id}/convert",
    response_model=L10InputConvertResponse,
    status_code=status.HTTP_201_CREATED,
)
def convert_meeting_input(
    input_id: UUID,
    db: DBDep,
    current_user: CurrentUser,
    background_tasks: BackgroundTasks,
    payload: L10InputConvertRequest,
):
    """Promote a prep idea into an IDS Issue or a To-Do.

    The idea's text becomes the new item's description; team scope
    (``department_name`` / ``is_exec``) and the ``l10_meeting_id`` back-pointer
    are inherited from the source meeting so the spawned item stays on the same
    team. By default the source idea is flagged ``discussed`` — kept for history
    but dropped from the live queue. Mirrors the rock→IDS / issue→to-do
    spin-offs, and honours the same Exec-scope gate when the meeting is the
    Exec/Leadership L10."""
    row = (
        db.query(L10Input)
        .filter(L10Input.id == input_id, L10Input.is_deleted == False)  # noqa: E712
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Input not found")
    meeting = _get_meeting_or_404(db, row.meeting_id, current_user)

    text = (row.text or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="Idea text is empty")

    dept = meeting.department_name
    is_exec = bool(meeting.is_exec_meeting)
    # Creating on the Exec team requires exec scope — same gate as
    # create_issue / create_todo. Department meetings need nothing beyond the
    # meeting-access check already done in _get_meeting_or_404.
    if is_exec:
        require_exec_scope_allowed(current_user, True)

    if payload.target == "issue":
        reject_support_background_work()

    issue_out = None
    todo_out = None

    if payload.target == "issue":
        # Owner is optional for an issue (None = unassigned); validate it when
        # supplied so a bad owner_id / owner_email 400s instead of FK-500ing.
        issue_owner_id = _resolve_convert_owner(
            db, payload, current_user, require_owner=False
        )
        issue = Issue(
            description=text,
            title=derive_issue_title(text),
            department_name=dept,
            is_exec=is_exec,
            l10_meeting_id=meeting.id,
            owner_id=issue_owner_id,
            priority=payload.priority,
            status=IssueStatus.OPEN,
            created_by=current_user.email,
        )
        db.add(issue)
        db.flush()
        issue_out = issue
        enqueue_issue_ingest(background_tasks)
    else:  # target == "todo"
        owner_id = _resolve_convert_owner(db, payload, current_user, require_owner=True)
        todo = Todo(
            owner_id=owner_id,
            # The issue branch above derives a capped title from the same text;
            # the to-do has only this one field, so cap it here too rather than
            # letting a whole parked paragraph become a single list row.
            description=truncate_todo_text(text),
            due_date=payload.due_date,
            department_name=dept,
            is_exec=is_exec,
            l10_meeting_id=meeting.id,
            status=TodoStatus.PENDING,
            created_by=current_user.email,
        )
        db.add(todo)
        db.flush()
        todo_out = todo

    # Keep the idea (history) but flag it handled so it drops off the live queue.
    if payload.mark_discussed and not row.discussed:
        row.discussed = True
        row.updated_by = current_user.email

    db.commit()
    if issue_out is not None:
        db.refresh(issue_out)
    if todo_out is not None:
        db.refresh(todo_out)
    db.refresh(row)

    return L10InputConvertResponse(
        target=payload.target,
        issue=IssueResponse.model_validate(issue_out) if issue_out else None,
        todo=TodoResponse.model_validate(todo_out) if todo_out else None,
        input=L10InputResponse.model_validate(row),
    )

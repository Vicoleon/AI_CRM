"""Daily briefing L10 prep status for the caller.

`GET /api/eos/me/l10-prep-status` returns everything the briefing
needs to nudge an owner about their next L10:

- ``next_l10``: the soonest upcoming L10 the caller is on the hook
  for (by department / exec team / explicit manager), with
  ``hours_until`` so the briefing can highlight "<24h to go".
- ``missing_rock_updates``: rocks the caller owns that have no
  ``RockWeeklyUpdate`` in the past 7 days. These are the items the
  briefing should call out by name.
- ``missing_scorecard_entries``: scorecard metrics the caller owns
  where the current week's ``ScorecardEntry.value`` is unset.
- ``needs_prep``: convenience boolean = True when there's a next
  L10 within the lookahead AND something is missing.

The endpoint is read-only and self-scoped (the caller's own data
only). Per-user; no admin override needed.

The frontend calls this directly; the nudge does not ride on the AI
Priorities cards.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter

from backend.routers.eos._authz import (
    meeting_roster,
    roster_dept_names,
    sees_every_team_rock,
)
from backend.routers.eos._shared import CurrentUser, DBDep
from rubik_core.database.eos import (
    L10Meeting,
    Person,
    Rock,
    RockStatus,
    RockWeeklyUpdate,
    ScorecardEntry,
    ScorecardMetric,
)
from rubik_core.eos.calendar_sync import is_calendar_cancelled
from rubik_core.priorities.temporal import resolve_tz
from rubik_core.rag.config import canonical_department_labels, slugify_department

router = APIRouter(prefix="/me", tags=["EOS: Me"])


# How far ahead we look for "your next L10". 7 days covers the normal
# weekly cadence with a buffer for a slipped meeting.
_NEXT_L10_LOOKAHEAD_DAYS = 7

# How recent a RockWeeklyUpdate has to be to count as "you've already
# updated this rock". 6 days (instead of 7) so the Sunday-before-Monday
# meeting cadence doesn't drift into "stale" the morning of.
_ROCK_UPDATE_FRESHNESS_DAYS = 6

# User-facing name of the Exec scope. This endpoint is the source of truth
# for every team/scope picker in the app, so the app-wide name lives here.
# The Slack modals mirror it in backend/routers/slack_commands.py
# (``_EXEC_SCOPE_LABEL``) and the frontend in
# frontend/src/pages/eos/_utils.ts (``EXEC_SCOPE_LABEL``) — keep all three
# in step or the same scope shows up under two different names.
EXEC_SCOPE_LABEL = "Executive View"

# Sentinel scope value for the cross-department Exec/Leadership team.
# ``department_name`` is FK-constrained and can't hold a sentinel, so the team
# travels as this value and lands as ``is_exec=True``. Mirrored by the frontend
# (``EXEC_SCOPE`` in frontend/src/pages/eos/_utils.ts).
EXEC_SCOPE = "__exec__"


@dataclass
class _NextL10:
    meeting_id: str
    meeting_name: str
    department_name: str | None
    is_exec_meeting: bool
    # When the next session is *scheduled* for. We don't track future
    # scheduled L10Sessions in the DB (sessions are created at start),
    # so this is computed from the meeting's day_of_week + start_time.
    scheduled_for: str  # ISO-8601 UTC
    hours_until: int


def _coerce_uuid(v) -> UUID | None:
    """SQLAlchemy returns Uuid columns as Python UUID instances on
    Postgres; on SQLite the same Uuid type yields the same — but our
    sync script lands them as 32-hex strings (no dashes). Cast safely
    so consumers can stringify."""
    if v is None:
        return None
    if isinstance(v, UUID):
        return v
    try:
        return UUID(str(v))
    except (TypeError, ValueError):
        return None


def _next_scheduled_dt(
    day_of_week: int | None, start_time: str | None, *, now: datetime
) -> datetime | None:
    """Next occurrence of (day_of_week, HH:MM) after ``now``, as naive UTC.
    Returns ``None`` when either field is missing — caller treats that as
    "we don't know when this meeting is".

    ``start_time`` is a WALL-CLOCK time, not UTC: calendar import stores the
    event's local "HH:MM" (Google returns "11:00:00-05:00" and the offset is
    dropped), and a manager typing a time into the schedule form types the
    one on their own clock. Neither records a zone, so it is read in the
    company's time zone (``resolve_tz()``, UTC until an admin sets one). Reading it as UTC showed an
    11:00 Central meeting as 6:00 AM.

    The weekday and the date are taken on that same clock, so a Thursday
    6:00 PM meeting is still Thursday's even though it is Friday in UTC, and
    each occurrence gets its own UTC offset across a DST change.

    day_of_week uses the project's convention (0 = Monday, 6 = Sunday)
    which matches Python's ``datetime.weekday()`` — not ``isoweekday()``.
    """
    if day_of_week is None or start_time is None:
        return None
    try:
        hh, mm = start_time.split(":")
        slot = time(int(hh), int(mm))
    except (ValueError, AttributeError):
        return None
    if not (0 <= day_of_week <= 6):
        return None
    zone, _ = resolve_tz()
    local_today = now.replace(tzinfo=timezone.utc).astimezone(zone).date()
    days_ahead = (day_of_week - local_today.weekday()) % 7
    # Today's slot may already have passed; next week's is then the answer.
    for days in (days_ahead, days_ahead + 7):
        candidate = (
            datetime.combine(local_today + timedelta(days=days), slot, tzinfo=zone)
            .astimezone(timezone.utc)
            .replace(tzinfo=None)
        )
        if candidate > now:
            return candidate
    return None


def _calendar_next_dt(meeting, *, now: datetime) -> datetime | None:
    """The reconciled next occurrence, when it is still in the future.

    ``next_occurrence_at`` is stored naive UTC (see the migration). A stale one
    — the meeting has since happened and nothing has re-reconciled — is
    discarded rather than returned as "next", which would hand the caller a
    date in the past.
    """
    stored = getattr(meeting, "next_occurrence_at", None)
    if stored is None:
        return None
    if stored.tzinfo is not None:
        stored = stored.astimezone(timezone.utc).replace(tzinfo=None)
    return stored if stored > now else None


def _find_next_l10_for(
    *, db, user_email: str, user_department: str | None, is_exec: bool, now: datetime
) -> _NextL10 | None:
    """Pick the meeting whose next scheduled occurrence is the soonest
    among the meetings the caller is on the hook for.

    Inclusion rules — a meeting matches if any of these hold:
    1. ``manager_id`` resolves to the caller's Person row.
    2. ``is_exec_meeting`` and the caller has the exec flag.
    3. It was imported from a calendar (non-empty ``roster_emails``) and the
       caller is ON that invite list. The roster is authoritative here for the
       same reason it is in ``_authz._meeting_accessible``: an imported
       meeting's department is a routing hint, not an invitation, so matching
       on department alone nags people about a meeting they aren't in.
    4. It has no roster and its ``department_name`` equals the caller's
       department — a manually-scheduled team L10, where the team IS the roster.

    DELIBERATELY narrower than ``_authz._meeting_accessible``: the org-wide
    Exec (CEO) can *see* every team's L10, but is not *on the hook* for them.
    Mirroring that bypass here would hand the org-wide Exec whichever team's
    L10 happens to be soonest as "your next L10" and nag them for rock updates
    they don't own — a nudge is about your own prep, not your read access.
    """
    candidate_q = db.query(L10Meeting).filter(
        L10Meeting.is_deleted == False  # noqa: E712
    )
    meetings = candidate_q.all()

    # Find the caller's Person id once so the manager-of-meeting check
    # is a cheap dict lookup. Caller might not have a Person row in EOS
    # yet (e.g. a brand-new hire) — that's fine, we just skip the rule.
    me_person = db.query(Person).filter(Person.email == user_email).first()
    me_person_id = me_person.id if me_person else None

    cutoff = now + timedelta(days=_NEXT_L10_LOOKAHEAD_DAYS)
    me_email = (user_email or "").lower()
    best: tuple[datetime, L10Meeting] | None = None
    for m in meetings:
        roster = meeting_roster(m)
        if me_person_id is not None and m.manager_id == me_person_id:
            matches = True
        elif m.is_exec_meeting:
            matches = is_exec
        elif roster:
            matches = bool(me_email) and me_email in roster
        else:
            matches = bool(user_department) and slugify_department(
                m.department_name
            ) == slugify_department(user_department)
        if not matches:
            continue

        # A meeting whose calendar series was cancelled is not a meeting you
        # are on the hook for, however faithfully its weekly pattern keeps
        # rolling forward. Only a RECONCILED cancellation counts — a NULL
        # ``calendar_state`` (manually scheduled, or never checked) behaves
        # exactly as it always has. See rubik_core/eos/calendar_sync.py.
        if is_calendar_cancelled(m):
            continue
        # Prefer Google's own answer for WHEN, so a moved meeting or a skipped
        # week is reflected instead of the pattern's guess. Falls back to the
        # pattern whenever we have no reconciled date.
        scheduled = _calendar_next_dt(m, now=now) or _next_scheduled_dt(
            m.day_of_week, m.start_time, now=now
        )
        if scheduled is None or scheduled > cutoff:
            continue
        if best is None or scheduled < best[0]:
            best = (scheduled, m)

    if best is None:
        return None

    scheduled, meeting = best
    hours_until = max(0, int((scheduled - now).total_seconds() // 3600))
    return _NextL10(
        meeting_id=str(meeting.id),
        meeting_name=meeting.name,
        department_name=meeting.department_name,
        is_exec_meeting=bool(meeting.is_exec_meeting),
        scheduled_for=scheduled.replace(tzinfo=timezone.utc).isoformat(),
        hours_until=hours_until,
    )


def _missing_rock_updates_for(*, db, person_id, now: datetime) -> list[dict]:
    """Rocks owned by ``person_id`` that lack a recent ``RockWeeklyUpdate``.

    "Recent" = an update with ``update_date`` within the last
    ``_ROCK_UPDATE_FRESHNESS_DAYS`` days. Done rocks are excluded — the
    briefing shouldn't nag about rocks already shipped this quarter.
    """
    if person_id is None:
        return []

    rocks = (
        db.query(Rock)
        .filter(
            Rock.owner_id == person_id,
            Rock.is_deleted == False,  # noqa: E712
            Rock.status != RockStatus.DONE,
        )
        .all()
    )
    if not rocks:
        return []

    rock_ids = [r.id for r in rocks]
    freshness_cutoff = now.date() - timedelta(days=_ROCK_UPDATE_FRESHNESS_DAYS)

    recent_updates = (
        db.query(RockWeeklyUpdate.rock_id)
        .filter(
            RockWeeklyUpdate.rock_id.in_(rock_ids),
            RockWeeklyUpdate.is_deleted == False,  # noqa: E712
            RockWeeklyUpdate.update_date >= freshness_cutoff,
        )
        .all()
    )
    fresh_rock_ids = {row[0] for row in recent_updates}

    out = []
    for rock in rocks:
        if rock.id in fresh_rock_ids:
            continue
        # Find the last known update date (if any) so the UI can render
        # "last updated 9 days ago" instead of just "missing".
        last_update = (
            db.query(RockWeeklyUpdate.update_date)
            .filter(
                RockWeeklyUpdate.rock_id == rock.id,
                RockWeeklyUpdate.is_deleted == False,  # noqa: E712
            )
            .order_by(RockWeeklyUpdate.update_date.desc())
            .first()
        )
        days_since = None
        if last_update is not None and last_update[0] is not None:
            days_since = (now.date() - last_update[0]).days
        out.append(
            {
                "rock_id": str(rock.id),
                "rock_description": rock.description,
                "status": rock.status.value
                if hasattr(rock.status, "value")
                else str(rock.status),
                "days_since_last_update": days_since,
            }
        )
    return out


def _current_week_start(now: datetime) -> datetime:
    """Sunday of the current week — matches the scorecard page's
    ``getPastWeeks()`` so the "is this week's entry filled?" check
    aligns with what the user actually sees in the UI."""
    # Python's weekday() has Monday=0 .. Sunday=6.
    days_back = (now.weekday() + 1) % 7  # Sunday → 0, Monday → 1 …
    sunday = now - timedelta(days=days_back)
    return sunday.replace(hour=0, minute=0, second=0, microsecond=0)


def _missing_scorecard_entries_for(*, db, person_id, now: datetime) -> list[dict]:
    """Scorecard metrics owned by ``person_id`` whose current-week
    entry doesn't exist OR is blank."""
    if person_id is None:
        return []

    metrics = (
        db.query(ScorecardMetric)
        .filter(
            ScorecardMetric.owner_id == person_id,
            ScorecardMetric.is_deleted == False,  # noqa: E712
        )
        .all()
    )
    if not metrics:
        return []

    week_start = _current_week_start(now)
    metric_ids = [m.id for m in metrics]

    entries = (
        db.query(ScorecardEntry)
        .filter(
            ScorecardEntry.metric_id.in_(metric_ids),
            ScorecardEntry.week_start_date >= week_start,
            ScorecardEntry.is_deleted == False,  # noqa: E712
        )
        .all()
    )
    filled = {e.metric_id for e in entries if e.value is not None and e.value.strip()}

    out = []
    for m in metrics:
        if m.id in filled:
            continue
        out.append(
            {
                "metric_id": str(m.id),
                "metric_name": m.name,
                "target_value": m.target_value,
                "week_start": week_start.date().isoformat(),
            }
        )
    return out


@router.get("/l10-prep-status")
def l10_prep_status(db: DBDep, current_user: CurrentUser):
    """What's missing for the caller's next L10."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    user_department = getattr(current_user, "department", None)
    is_exec = bool(getattr(current_user, "is_exec", False))

    me_person = db.query(Person).filter(Person.email == current_user.email).first()
    me_person_id = me_person.id if me_person else None

    next_l10 = _find_next_l10_for(
        db=db,
        user_email=current_user.email,
        user_department=user_department,
        is_exec=is_exec,
        now=now,
    )

    missing_rocks = _missing_rock_updates_for(db=db, person_id=me_person_id, now=now)
    missing_metrics = _missing_scorecard_entries_for(
        db=db, person_id=me_person_id, now=now
    )

    # Only call out the prep nudge when there's actually a meeting
    # coming up. If the next L10 is > 7 days away (or unknown), we
    # don't nag — the briefing is about today.
    needs_prep = next_l10 is not None and (bool(missing_rocks) or bool(missing_metrics))

    return {
        "next_l10": (
            None
            if next_l10 is None
            else {
                "meeting_id": next_l10.meeting_id,
                "meeting_name": next_l10.meeting_name,
                "department_name": next_l10.department_name,
                "is_exec_meeting": next_l10.is_exec_meeting,
                "scheduled_for": next_l10.scheduled_for,
                "hours_until": next_l10.hours_until,
            }
        ),
        "missing_rock_updates": missing_rocks,
        "missing_scorecard_entries": missing_metrics,
        "needs_prep": needs_prep,
    }


# ── Meeting scopes (multi-team membership) ──────────────────────────


def team_scopes_for(db, current_user) -> list[dict]:
    """The team scopes the caller may publish to — see ``my_meeting_scopes``.

    Factored out of the endpoint so server-side writers can enforce the same
    vocabulary the UI offers. The Scorecard uploader uses it twice: to build
    the template's Team dropdown, and to reject a row that tags a measurable
    with a team the caller doesn't belong to.
    """
    return _team_scopes(db, current_user)


@router.get("/meeting-scopes")
def my_meeting_scopes(db: DBDep, current_user: CurrentUser, surface: str | None = None):
    """Return the meeting scopes the caller can schedule / edit L10s
    under. Drives the scope dropdown on the Meetings create/edit modal.

    Rules:
    - Admins: every Cubie department + the Exec scope ("Executive View").
    - Exec users: the Exec scope + their own ``department`` + each entry
      in ``managed_departments`` (or every department when they hold the
      ``*`` org-wide wildcard, e.g. the CEO).
    - Team Managers (non-exec, non-empty ``managed_departments``): their
      own ``department`` + each managed department — but NOT the Exec
      team. This lets a people-manager schedule / edit their team's L10s
      without any Exec-team access.
    - Everyone else: just their own ``department``. The frontend
      hides the dropdown entirely when only one scope is returned.

    Each scope is { kind: "exec" | "department", label, value }. The
    value matches what the create endpoint expects:
    ``__exec__`` sentinel for the Exec team, raw department name
    otherwise.

    ``?surface=rocks`` (the Rocks page) also offers every department to the
    people who keep every team's Rocks (``_authz.sees_every_team_rock``), as
    filing scopes only. Every other page asks without it, so that roster
    never widens an Issues, To-Dos, Scorecard or Meetings picker.
    """
    every_team = surface == "rocks" and sees_every_team_rock(current_user, db)
    return {"scopes": _team_scopes(db, current_user, every_team=every_team)}


def _canonical_departments(labels: Iterable[str]) -> list[str]:
    """Collapse case-variant department labels and drop the ``general`` sentinel.

    ``departments.name`` is the raw, case-sensitive PK, so a table can hold
    ``sales``/``Sales`` or ``engineering``/``Engineering`` as separate rows
    (a local test run against a shared database writes the lowercase ones;
    the root ``conftest.py`` now closes that hole). A department's identity
    is its RAG slug, so those pairs are ONE team and must render once, or
    the team dropdown shows every duplicate ("2 sales, 2 engineering").
    Folding here rather than only deleting the rows keeps the dropdown
    correct whatever a future stray row comes from.

    Which spelling wins is decided by ``canonical_department_labels``, so a
    data cleanup must consolidate onto the label this dropdown shows.

    ``general`` is the default value of ``users.department``, i.e. "no department
    assigned" — never a team anyone can publish to. Every sibling branch here and
    every check in ``_authz`` / ``l10`` / ``people`` / ``slack_commands`` already
    excludes it; the enumerate-every-department branches did not, so it rendered
    as a selectable team for admins and org-wide execs.
    """
    canonical = canonical_department_labels(labels)
    return sorted(
        (label for slug, label in canonical.items() if slug not in ("general", "all")),
        key=lambda s: (s.lower(), s),
    )


def _team_scopes(db, current_user, *, every_team: bool = False) -> list[dict]:
    # Local import keeps this endpoint contained.
    from rubik_core.database.models import Department

    is_admin = "admin" in (current_user.roles or [])
    is_exec = bool(getattr(current_user, "is_exec", False))
    own_dept = (getattr(current_user, "department", None) or "").strip()
    managed = list(getattr(current_user, "managed_departments", None) or [])

    scopes: list[dict] = []
    seen_values: set[str] = set()

    def _push(
        kind: str,
        label: str,
        value: str,
        can_schedule: bool = True,
        is_primary: bool = False,
    ):
        # Department scopes de-dupe on the RAG slug, not the literal string, so
        # the caller's own stored spelling ("sales") and the canonical table row
        # ("Sales") can never both land in one dropdown. First push wins, which
        # keeps a member's own department showing the spelling their rows are
        # actually tagged with — and, since the roster pass runs last, keeps a
        # team you both belong to AND sit on the L10 of schedulable.
        key = slugify_department(value) if kind == "department" else value
        if key in seen_values:
            return
        seen_values.add(key)
        scopes.append(
            {
                "kind": kind,
                "label": label,
                "value": value,
                "can_schedule": can_schedule,
                # The caller's OWN department — the team whose list they mean
                # when they say "my Issues page". The list views default their
                # team filter to it rather than to "All my teams", because the
                # moment someone holds a second team the combined default reads
                # as another team's rows leaking into theirs. At most one entry
                # carries this; nobody's primary department is the Exec team.
                "is_primary": is_primary,
            }
        )

    def _all_departments() -> list[str]:
        rows = db.query(Department.name).all()
        return _canonical_departments(name for (name,) in rows)

    if is_admin:
        # Admins see Exec + every real Cubie department. Their own department is
        # still flagged: an admin has a home team like anyone else, and their
        # list views should open on it rather than on all twelve.
        own_slug = slugify_department(own_dept) if own_dept else None
        _push("exec", EXEC_SCOPE_LABEL, EXEC_SCOPE)
        for name in _all_departments():
            _push(
                "department",
                name,
                name,
                is_primary=bool(own_slug) and slugify_department(name) == own_slug,
            )
    else:
        # The Exec-team scope is Exec-team-only.
        if is_exec:
            _push("exec", EXEC_SCOPE_LABEL, EXEC_SCOPE)
        # Everyone gets their own department, in the spelling stored on the
        # user row. NOT canonicalised: that would silently re-tag what this
        # user's own rocks / issues get written with. Cross-casing matches are
        # handled by folding case in the EOS team filters instead.
        if own_dept and slugify_department(own_dept) != "general":
            _push("department", own_dept, own_dept, is_primary=True)
        # ...plus every department they manage. This is the Team Manager
        # path: a people-manager who leads one or more teams can schedule /
        # edit those teams' L10s (and file their Issues / To-Dos) WITHOUT
        # being on the Exec team. The ``*`` wildcard is an Exec-only,
        # org-wide (CEO) concept, so expand it only for execs; for a
        # non-exec it is ignored.
        if is_exec and "*" in managed:
            for name in _all_departments():
                _push("department", name, name)
        else:
            for d in _canonical_departments(x for x in managed if x != "*"):
                _push("department", d, d)

    # ...and finally the teams whose L10 the caller sits on without belonging
    # to them on paper (``_authz.roster_dept_names``). These are FILING scopes
    # only: they make the Issues / To-Dos / Rocks / Scorecard team pickers
    # offer the team the caller actually meets with, but they must NOT appear
    # in the Meetings create/edit dropdown — ``_require_department_scope``
    # would 403 the save, and offering an option the backend rejects is worse
    # than not offering it. Pushed LAST so a team the caller both belongs to
    # and is rostered on keeps its schedulable entry.
    for d in sorted(roster_dept_names(current_user, db), key=lambda x: x.lower()):
        _push("department", d, d, can_schedule=False)

    # ...and, on the Rocks page only, every other team for the people who keep
    # every team's Rocks. Filing scopes like the roster pass above: never the
    # Exec team (no department row is the Exec team), never schedulable.
    if every_team:
        for name in _all_departments():
            _push("department", name, name, can_schedule=False)

    return scopes

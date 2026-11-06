"""EOS row-level READ access control.

Deletes are gated by `require_delete_permission`, but nothing else filters
what a user may *see*. This module adds that: per-resource
"visible id" helpers the list endpoints fold into their query as
`Model.id.in_(<ids>)`.

Each `visible_*_ids()` returns either:
  * ``None`` — caller is unrestricted (Cubie admin, or an org-wide Exec / CEO
    via ``UserProfile.sees_all_departments``); the caller skips filtering.
  * ``list[UUID]`` (possibly empty) — the ids the caller may read.

Visibility model — team scoping, which marks the
cross-department Exec/Leadership team with ``is_exec=True`` (because
``department_name`` is FK-constrained and can't hold a sentinel):

  * Department-scoped rows (Issue / Todo / ScorecardMetric carry
    ``department_name`` + ``is_exec`` + ``owner_id``) are visible when the row
    is in one of the caller's departments, is an Exec-team row and the caller
    is on the Exec team (``is_exec``), is owned by the caller or a direct
    report, or is unscoped (NULL department and not exec → company-wide).
  * Rocks carry an explicit ``visibility`` enum and derive department from the
    owner's seat:
      - COMPANY     → everyone
      - DEPARTMENT  → owner sits in one of the caller's departments
      - INDIVIDUAL  → owner is the caller, a direct report, or (for
                      leadership) anyone in the caller's department(s)
    Rocks ALSO carry an ``is_exec`` flag ("ET" rock): when set, the rock is
    visible to the Exec team ONLY (and to a non-exec ONLY when they own it),
    regardless of the ``visibility`` enum — the same Exec-only boundary as the
    department-scoped resources above.
  * The people on the ``eos_rocks_all_teams`` roster (Admin → Feature Access;
    whoever keeps every team's Rocks) read and update every non-ET Rock, of
    any visibility. ET Rocks and deleting stay exactly as above.

"Leadership" = Cubie admin, an Exec-team member (``UserProfile.is_exec``), a
**Team Manager** (any non-empty ``UserProfile.managed_departments``), or the
holder of a seat flagged ``is_leadership``.
Leadership sees and manages every row in the department(s) it leads — but
Exec-team rows (``is_exec``) stay visible to the Exec team ONLY. A Team
Manager therefore runs their team's L10 and owns their team's
Rocks/Issues/To-Dos/Scorecard **without** any access to the confidential
Exec/Leadership team. This is what decouples "manages a team" from "is on
the Exec team".

A caller with no EOS ``Person``/seat row still sees company-scoped rows and
anything in their ``UserProfile.department`` — they just get no
owner/report/leadership expansion.

PUBLIC SCOPE SURFACE (Cubie Sales Cockpit)
------------------------------------------
The scope helpers below are exported under public names because this module
holds the **only** real supervisor-of edge in Cubie: ``eos_seats.reports_to_id``
bridged to ``users`` by ``eos_people.email``. The ``users`` table has no
``manager_id``, no ``team_id`` and no ``hubspot_owner_id``, and
``managed_departments`` is a JSON list of department NAME STRINGS with no FK —
it says which teams you lead, never who reports to you. So any feature needing
a manager→team roll-up (the cockpit's commitment views) resolves it HERE rather
than building a second org structure: one implementation, promoted, never
copied into another package.

Underscore aliases are kept for every promoted name — several modules import
them (``backend/routers/home.py``, ``backend/routers/eos/l10.py``,
``rubik_core/availability/handoff.py``,
``rubik_core/priorities/agents/eos_agent.py``) and one test patches
``_compute_scope`` by string. Patch whichever name the consumer actually
imports; both resolve to the same function object.

``email_scope`` / ``team_scope_problem`` exist for the surfaces keyed by EMAIL
rather than by ``eos_people.id`` (``commitments.owner_email`` has no FK).
They also make the trap in this module LOUD: a caller
with no ``eos_people`` row resolves to an empty scope, and on an email-keyed
surface an empty scope is indistinguishable from "you have no commitments".
Never let that read as an empty result set.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from rubik_core.auth.registry import UserProfile
from rubik_core.brand import PRODUCT_NAME
from rubik_core.config import settings
from rubik_core.database.eos import (
    Issue,
    Person,
    Rock,
    ScorecardMetric,
    Seat,
    Todo,
    Visibility,
)
from rubik_core.database.schemas import ALL_DEPARTMENTS
from rubik_core.features import feature_allows

__all__ = [
    "EmailScope",
    "Scope",
    "accessible_meeting_ids",
    "can_access_meeting",
    "can_manage_meeting",
    "can_mutate",
    "can_mutate_in_scope",
    "can_update_rock",
    "can_update_rock_in_scope",
    "can_read",
    "compute_scope",
    "direct_report_person_ids",
    "eligible_roster_dept_names",
    "email_scope",
    "emails_for_person_ids",
    "is_leadership",
    "led_depts",
    "managed_depts",
    "meeting_authority",
    "mutable_ids",
    "mutate_scope",
    "meeting_roster",
    "person_by_email",
    "owner_seat_depts",
    "person_ids_in_depts",
    "require_can_mutate",
    "require_can_read",
    "require_can_update_rock",
    "require_exec_scope_allowed",
    "require_meeting_access",
    "require_team_scope",
    "rock_mutate_scope",
    "rock_write_flags",
    "roster_dept_names",
    "seat_for_person",
    "sees_every_team_rock",
    "team_optins",
    "team_scope_problem",
    "user_depts",
    "visible_issue_ids",
    "visible_measurable_ids",
    "visible_rock_ids",
    "visible_todo_ids",
]


# ---- user / seat lookup ---------------------------------------------------


def _is_admin(user: UserProfile) -> bool:
    return "admin" in (user.roles or [])


def _is_unrestricted(user: UserProfile) -> bool:
    """Admin, or an org-wide Exec / CEO who sees every department's EOS."""
    if _is_admin(user):
        return True
    return bool(getattr(user, "sees_all_departments", False))


#: Admin → Feature Access key for the named people who keep every team's Rocks.
ROCKS_ALL_TEAMS_FEATURE = "eos_rocks_all_teams"


def sees_every_team_rock(user: UserProfile, db: Session) -> bool:
    """Is the caller on the roster that keeps every team's Rocks?

    Rocks only, and never the Exec team's: whoever writes up each team's
    rock-setting session needs every team's Rocks, not that team's Issues,
    To-Dos or Scorecard, so this is NOT a Team Manager grant and nothing outside
    the Rock paths reads it. The roster is data (``eos_rocks_all_teams``), so
    adding the next person is an admin click, not a deploy.
    """
    return feature_allows(ROCKS_ALL_TEAMS_FEATURE, user, db)


def person_by_email(db: Session, email: str | None) -> Person | None:
    """Resolve an ``eos_people`` row by email, case-insensitively.

    THE identity join for the whole EOS surface, and it must fold case on
    BOTH sides. ``eos_people.email`` is written by several paths (the EOS
    people sync, the L10 calendar import, the admin people editor, seed
    scripts) and was historically stored verbatim, while ``user.email`` comes
    from Google lower-cased. A row saved as ``Jdoe@acme.example``
    therefore matched nothing: the caller resolved to no ``Person``,
    ``_compute_scope`` returned empty ``owner_pids``, and they lost every
    owner / direct-report / leadership grant — silently, with an empty EOS
    page and no EOS content at all in AI Priorities (the priorities EOS agent
    early-returns on empty ``owner_pids``).

    ``Person._normalize_email`` lower-cases on write, so this fold is defence
    in depth: it keeps the lookup correct for a row written around the ORM
    (raw SQL, a restore from an older backup, a bulk import) instead of
    silently revoking someone's access again.
    """
    key = (email or "").strip().lower()
    if not key:
        return None
    return (
        db.query(Person)
        .filter(func.lower(Person.email) == key, Person.is_deleted.is_(False))
        .first()
    )


def _person_for(user: UserProfile, db: Session) -> Person | None:
    return person_by_email(db, user.email)


def seat_for_person(person: Person | None, db: Session) -> Seat | None:
    """The caller's Accountability-Chart seat, or None.

    Public because ``led_depts`` / ``is_leadership`` both take a seat, so a
    caller outside this module cannot use them without it. Every hop here can
    be missing: no Person row, a Person with no seat, a seat with no
    ``reports_to_id``.
    """
    if person is None or person.seat_id is None:
        return None
    return (
        db.query(Seat)
        .filter(Seat.id == person.seat_id, Seat.is_deleted.is_(False))
        .first()
    )


_seat_for_person = seat_for_person


def _seat_for_owner(owner_id: UUID, db: Session) -> Seat | None:
    """The seat of the Person who owns a resource (for department derivation)."""
    person = (
        db.query(Person)
        .filter(Person.id == owner_id, Person.is_deleted.is_(False))
        .first()
    )
    return _seat_for_person(person, db)


def user_depts(user: UserProfile, seat: Seat | None) -> set[str]:
    """Department names the caller belongs to: their primary department, any
    departments they manage (Accountability-Chart integrator), and their EOS
    seat's department. Blank / 'general' is dropped."""
    depts: set[str] = set()
    primary = (user.department or "").strip()
    if primary and primary.lower() != "general":
        depts.add(primary)
    for raw in getattr(user, "managed_departments", None) or []:
        d = (raw or "").strip()
        if d and d != "*" and d.lower() != "general":
            depts.add(d)
    if seat and seat.department_name:
        depts.add(seat.department_name)
    return depts


_user_depts = user_depts


def eligible_roster_dept_names(user: UserProfile, db: Session) -> set[str]:
    """Departments the caller COULD claim by being ON an L10's calendar roster.

    A person is routinely a working member of a team they don't "belong" to on
    paper: a marketer may sit on both the Marketing and the Sales L10, but
    ``users.department`` holds ONE value and ``managed_departments`` is a
    leadership grant, not a membership one. Without this, everything they file
    from outside a meeting (Issues / To-Dos / Rocks pages, Slack ``/todo`` and
    ``/ids``) silently lands on their home team, and the standalone pages show
    them only that team's rows — while the L10 they're sitting in shows them
    the whole other team's. The roster is the membership signal we already trust
    everywhere else: ``_meeting_accessible`` treats a calendar-imported
    meeting's invite list AS the ACL, and ``get_meeting_items`` serves that
    meeting's full team lists off it with no per-row filter.

    This is the ELIGIBLE set, not the granted one — see ``roster_dept_names``,
    which intersects it with the user's admin-set opt-ins. A roster is a
    snapshot of one calendar invite, so on its own it would let a single guest
    appearance at another team's L10 put that team in someone's picker forever,
    removable only by a re-import nobody thinks to run.

    Deliberately narrow:

    * **Exec meetings are excluded.** The Exec/Leadership surface is gated on
      ``is_exec`` alone and a calendar invite must never grant it — the same
      rule ``_meeting_accessible`` enforces for reads.
    * **Membership only, never leadership.** ``compute_scope`` unions the
      granted set in AFTER ``led_depts`` has been computed from ``user_depts``,
      so a roster seat widens what you can SEE and FILE, never what you can
      mutate on other people's rows. Being invited to a team's L10 does not
      make you its Team Manager.
    * **Rosterless meetings contribute nothing.** A manually-scheduled meeting
      has no ``roster_emails``, so it carries no membership claim; department
      match already covers who sees it.

    Returns the raw ``departments.name`` spellings (the callers case-fold).
    """
    if not settings.EOS_L10_ROSTER_SCOPE_ENABLED:
        return set()
    email = (user.email or "").strip().lower()
    if not email:
        return set()

    from rubik_core.database.eos import L10Meeting  # local: avoid import cycle

    rows = (
        db.query(L10Meeting.department_name, L10Meeting.roster_emails)
        .filter(
            L10Meeting.is_deleted.is_(False),
            L10Meeting.is_exec_meeting.is_(False),
            L10Meeting.department_name.isnot(None),
        )
        .all()
    )
    out: set[str] = set()
    for dept, roster in rows:
        # ``roster_emails`` is JSON: NULL for manually-scheduled meetings, and
        # historically written with whatever casing Google returned.
        if not roster:
            continue
        if email not in {(e or "").strip().lower() for e in roster}:
            continue
        d = (dept or "").strip()
        if d and d.lower() != "general":
            out.add(d)
    return out


def team_optins(user: UserProfile, db: Session) -> set[str]:
    """The department names an admin has switched on for this user.

    Raw stored strings from ``user_preferences.eos_team_optins``; meaningless
    on their own — ``roster_dept_names`` is what turns them into access, and
    only where a live roster agrees.
    """
    from rubik_core.database.models import UserPreference  # local: import cycle

    email = (user.email or "").strip()
    if not email:
        return set()
    row = (
        db.query(UserPreference.eos_team_optins)
        .filter(func.lower(UserPreference.user_email) == email.lower())
        .first()
    )
    if row is None or not row[0]:
        return set()
    return {(d or "").strip() for d in row[0] if (d or "").strip()}


def roster_dept_names(user: UserProfile, db: Session) -> set[str]:
    """Additional EOS teams this caller actually has: opted in AND rostered.

    The intersection is the whole design. Storing the opt-in alone would let a
    revoked or transferred person keep a team until somebody remembered to
    edit their row; deriving from the roster alone would make one guest
    appearance permanent. Requiring both means an admin's grant is real only
    while the calendar still says they meet with that team — so a transfer
    (Delivery → Sales) revokes the old team by itself, with no cleanup step.

    Case-folded on both sides: a department's identity is its slug, and the
    opt-in list is typed/stored separately from ``departments.name``.
    """
    eligible = eligible_roster_dept_names(user, db)
    if not eligible:
        return set()
    opted = _lower_set(team_optins(user, db))
    if not opted:
        return set()
    # Return the ROSTER's spelling — it came from ``departments.name`` via the
    # meeting's FK, so it matches what rows are actually tagged with.
    return {d for d in eligible if d.lower() in opted}


def managed_depts(user: UserProfile) -> set[str]:
    """Departments the caller is a **Team Manager** of — the teams they lead
    WITHOUT being on the Exec team. Excludes the ``*`` org-wide wildcard (an
    Exec-only, CEO concept) and the ``general`` catch-all."""
    out: set[str] = set()
    for raw in getattr(user, "managed_departments", None) or []:
        d = (raw or "").strip()
        if d and d != ALL_DEPARTMENTS and d.lower() != "general":
            out.add(d)
    return out


_managed_depts = managed_depts


def is_leadership(user: UserProfile, seat: Seat | None) -> bool:
    """Leadership = reads/manages a whole team, not just its own items.

    Cubie admin, an Exec-team member, a Team Manager (any managed
    departments), or a seat flagged ``is_leadership``. NOTE: leadership does
    NOT by itself grant Exec-team (``is_exec``) row access — that stays gated
    on ``is_exec`` in ``_dept_scoped_conditions`` / ``can_mutate``."""
    if _is_admin(user) or bool(getattr(user, "is_exec", False)):
        return True
    if managed_depts(user):
        return True
    return bool(getattr(seat, "is_leadership", False)) if seat is not None else False


_is_leadership = is_leadership


def led_depts(user: UserProfile, seat: Seat | None, dept_names: set[str]) -> set[str]:
    """Departments where the caller has *leadership* authority (owner-level
    read + mutate over the whole team, not just their own row).

    Admin / Exec lead their entire scope (primary + managed + seat dept). A
    non-exec Team Manager leads exactly the departments they manage; a
    seat-``is_leadership`` head leads their seat's department. Fencing the
    expansion to led departments keeps a Team Manager's power on their own
    team(s) — being a member of some other department doesn't make them its
    leader.

    ``dept_names`` is the caller's membership set (``user_depts``); the
    parameter is named for the value, not for the function, now that
    ``user_depts`` is itself a public name in this module."""
    if _is_admin(user) or bool(getattr(user, "is_exec", False)):
        return set(dept_names)
    led = managed_depts(user)
    if (
        seat is not None
        and getattr(seat, "is_leadership", False)
        and seat.department_name
    ):
        led.add(seat.department_name)
    return led


_led_depts = led_depts


def _lower_set(names: set[str]) -> set[str]:
    """Lower-cased copy for case-insensitive department comparison. A
    department's identity is its slug (see docs/ENGINEERING_NOTES.md) — ``"Sales"`` and
    ``"sales"`` are the same team, so raw ``==`` / ``IN`` matching is a bug."""
    return {n.lower() for n in names}


def person_ids_in_depts(dept_names: set[str], db: Session) -> set[UUID]:
    """Person ids seated in any of ``dept_names`` (case-insensitive)."""
    if not dept_names:
        return set()
    seat_ids = [
        r[0]
        for r in db.query(Seat.id)
        .filter(
            func.lower(Seat.department_name).in_(_lower_set(dept_names)),
            Seat.is_deleted.is_(False),
        )
        .all()
    ]
    if not seat_ids:
        return set()
    return {
        r[0]
        for r in db.query(Person.id)
        .filter(Person.seat_id.in_(seat_ids), Person.is_deleted.is_(False))
        .all()
    }


_person_ids_in_depts = person_ids_in_depts


def direct_report_person_ids(seat: Seat | None, db: Session) -> set[UUID]:
    """Person ids whose seat reports (one level) up to ``seat``.

    ONE level, deliberately: this is the only real supervisor-of edge in the
    system (``eos_seats.reports_to_id``) and it is not walked transitively
    anywhere in EOS. A cockpit roll-up that wants a whole department uses
    ``person_ids_in_depts`` on the led departments instead — that is what
    ``compute_scope`` does.
    """
    if seat is None:
        return set()
    sub_seat_ids = [
        r[0]
        for r in db.query(Seat.id)
        .filter(Seat.reports_to_id == seat.id, Seat.is_deleted.is_(False))
        .all()
    ]
    if not sub_seat_ids:
        return set()
    return {
        r[0]
        for r in db.query(Person.id)
        .filter(Person.seat_id.in_(sub_seat_ids), Person.is_deleted.is_(False))
        .all()
    }


_direct_report_person_ids = direct_report_person_ids


@dataclass
class Scope:
    """One caller's resolved EOS reach.

    ``person_id`` and ``led_dept_names`` are appended (with defaults) rather
    than inserted: ``_compute_scope`` is patched by string in
    ``backend/tests/test_priorities_multi_entity_suppression.py`` with a
    ``MagicMock(owner_pids=…)``, and every construction site is by keyword.

    ``person_id is None`` is THE trap in this module: the caller has no
    ``eos_people`` row, so ``owner_pids`` carries no self / report / leadership
    grant. On the EOS surfaces that is survivable (they still see company-wide
    and own-department rows). On an email-keyed surface it silently revokes
    everything — see ``email_scope``.
    """

    dept_names: set[str] = field(default_factory=set)
    owner_pids: set[UUID] = field(default_factory=set)
    is_leader: bool = False
    person_id: UUID | None = None
    led_dept_names: set[str] = field(default_factory=set)

    @property
    def has_org_record(self) -> bool:
        """False when the caller has no ``eos_people`` row at all."""
        return self.person_id is not None


_Scope = Scope


def compute_scope(user: UserProfile, db: Session) -> Scope:
    person = _person_for(user, db)
    seat = seat_for_person(person, db)
    dept_names = user_depts(user, seat)
    is_leader = is_leadership(user, seat)
    # ORDER IS LOAD-BEARING: leadership is derived from MEMBERSHIP
    # (``user_depts``) before the roster widens the read scope below. An
    # admin / exec ``led_depts`` returns ``set(dept_names)`` wholesale, so
    # computing it after the union would hand an exec leadership over every
    # team whose L10 they were merely invited to.
    led = led_depts(user, seat, dept_names)
    # Being on a team's L10 roster makes you a member of that team for READ
    # and FILING purposes — see ``roster_dept_names``. Never for mutation:
    # ``led`` above is already fixed.
    dept_names = dept_names | roster_dept_names(user, db)

    owner_pids: set[UUID] = set()
    if person is not None:
        owner_pids.add(person.id)
    owner_pids |= direct_report_person_ids(seat, db)
    if is_leader and led:
        # Leaders see every teammate's items — but only for the team(s) they
        # actually lead, not merely belong to.
        owner_pids |= person_ids_in_depts(led, db)

    return Scope(
        dept_names=dept_names,
        owner_pids=owner_pids,
        is_leader=is_leader,
        person_id=person.id if person is not None else None,
        led_dept_names=led,
    )


_compute_scope = compute_scope


# ---- email-keyed scope (non-EOS surfaces: the Sales Cockpit) ----------------
#
# EOS rows carry ``owner_id`` → ``eos_people.id``. The cockpit's rows carry
# ``commitments.owner_email`` — a lower-folded String with NO FK to
# ``users.email``, because offboarding deletes that row and "missing users row"
# is the load-bearing departed signal. So the team roll-up needs the same org
# graph expressed as EMAILS, and it needs the missing-org-record case to be loud
# rather than empty.


def emails_for_person_ids(db: Session, person_ids: set[UUID]) -> set[str]:
    """Lower-folded ``eos_people.email`` for the given Person ids.

    Folded on the way out for the same reason ``person_by_email`` folds on the
    way in: these emails are compared against ``commitments.owner_email``,
    which is lower-folded at write time, while ``eos_people.email`` has been
    written verbatim by several paths historically (a restore or a raw-SQL
    insert can still land mixed case). Blanks are dropped — an empty string
    would match nothing and silently widen nothing, but it makes an `IN ()`
    clause look intentional when it is not.
    """
    if not person_ids:
        return set()
    rows = (
        db.query(Person.email)
        .filter(Person.id.in_(list(person_ids)), Person.is_deleted.is_(False))
        .all()
    )
    return {(r[0] or "").strip().lower() for r in rows} - {""}


@dataclass(frozen=True)
class EmailScope:
    """Whose rows an email-keyed surface may show this caller.

    ``unrestricted`` means "apply no owner filter to the ROW LIST" (Cubie admin
    or the org-wide Exec, exactly as ``_is_unrestricted`` decides it for EOS).
    It must NEVER be reused to widen access to CONTENT: transcript text is
    decided by ``backend.auth.user_can_read_transcript_text``, which takes a
    team-email set and no unrestricted sentinel. The precedent is this module's
    own L10 rule — a Cubie admin is an operator, not an executive, and does not
    get to read another team's diary by holding the role.
    """

    owner_emails: frozenset[str]
    person_ids: frozenset[UUID]
    unrestricted: bool
    has_org_record: bool
    is_leader: bool
    led_dept_names: frozenset[str]


def email_scope(db: Session, user: UserProfile) -> EmailScope:
    """Resolve ``user``'s reach on an email-keyed surface.

    ``owner_emails`` always contains the caller's own address even when they
    have no ``eos_people`` row — their OWN commitments key on
    ``users.email`` and must never depend on the EOS bridge. Everything beyond
    that (direct reports, led departments) comes from ``compute_scope``, i.e.
    from ``eos_seats.reports_to_id`` and ``managed_departments``.

    Callers that show another person's rows must pair this with
    ``team_scope_problem`` / ``require_team_scope``: an unresolvable team comes
    back here as a set of exactly one email, which renders as a believable but
    WRONG empty team.
    """
    scope = compute_scope(user, db)
    own = (getattr(user, "email", "") or "").strip().lower()
    emails = emails_for_person_ids(db, scope.owner_pids)
    if own:
        emails.add(own)
    return EmailScope(
        owner_emails=frozenset(emails),
        person_ids=frozenset(scope.owner_pids),
        unrestricted=_is_unrestricted(user),
        has_org_record=scope.has_org_record,
        is_leader=scope.is_leader,
        led_dept_names=frozenset(scope.led_dept_names),
    )


def team_scope_problem(scope: EmailScope) -> str | None:
    """Machine code for a team that could not be resolved, else None.

    THE trap, made nameable. A caller with no ``eos_people`` row resolves to a
    scope containing only themselves, and on a team view that is
    indistinguishable from "your team has nothing outstanding" — which is the
    worst possible failure mode for a cockpit whose entire purpose is that
    nothing outstanding is invisible. Two distinguishable causes:

    * ``org_record_missing`` — no ``eos_people`` row for this address at all.
      Usually a user who was never synced into the Accountability Chart, or the
      admin department-impersonation pseudo-user (``impersonated_user@….local``,
      see ``backend.auth._apply_impersonation``), which is not a person and
      correctly has no org record.
    * ``team_unresolved`` — the row exists but resolves to nobody but the
      caller: no seat (so no ``reports_to_id`` edge), or a led department in
      which nobody is seated. Both are the same visible symptom and the same
      fix — fill in the Accountability Chart — and the roll-up cannot show a
      single other person either way, so they share one code. Note that leading
      a department on paper is NOT enough: the AEs have to be seated in it,
      because the only email→person bridge is ``eos_people.email``.

    Never default open on either. ``unrestricted`` callers (admin / org-wide
    Exec) short-circuit, since they are not filtering by owner at all.
    """
    if scope.unrestricted:
        return None
    if not scope.has_org_record:
        return "org_record_missing"
    if len(scope.owner_emails) <= 1:
        return "team_unresolved"
    return None


def require_team_scope(db: Session, user: UserProfile) -> EmailScope:
    """``email_scope``, or a 409 naming why the caller's team is unresolvable.

    409 with a ``detail`` dict: "your org record is missing" is a normal state
    with a data fix, not a permission failure (403) and emphatically not a 200
    with an empty list. The UI branches on ``code`` rather than string-matching
    a sentence.

    Use this on the manager / supervisor / exec views, never on the AE's own
    view — an AE's own commitments key on their email and work with no EOS row
    at all.
    """
    scope = email_scope(db, user)
    problem = team_scope_problem(scope)
    if problem is None:
        return scope
    message = (
        f"{PRODUCT_NAME} can't tell who reports to you yet — your record is missing from "
        "the Accountability Chart. Ask an admin to add you."
        if problem == "org_record_missing"
        else (
            f"{PRODUCT_NAME} can't tell who is on your team yet — nobody reports to your "
            "Accountability Chart seat, and nobody is seated in the "
            "department(s) you manage. Ask an admin to fill in the chart."
        )
    )
    raise HTTPException(status_code=409, detail={"code": problem, "message": message})


# ---- visibility helpers ---------------------------------------------------


def _dept_scoped_conditions(model, scope: Scope, user: UserProfile) -> list:
    """OR-conditions shared by department-scoped resources (Issue / Todo /
    ScorecardMetric — they carry ``department_name`` + ``is_exec`` +
    ``owner_id``)."""
    conds = [
        # Unscoped / company-wide rows (e.g. orphaned by a department delete).
        model.department_name.is_(None) & model.is_exec.is_(False)
    ]
    if scope.dept_names:
        conds.append(
            func.lower(model.department_name).in_(_lower_set(scope.dept_names))
        )
    # Exec-team rows are visible to the Exec team ONLY. Department leadership
    # (a Team Manager, or a seat flagged is_leadership) never sees them — that
    # boundary is what separates "manages a team" from "on the Exec team".
    if bool(getattr(user, "is_exec", False)):
        conds.append(model.is_exec.is_(True))
    if scope.owner_pids:
        conds.append(model.owner_id.in_(scope.owner_pids))
    return conds


def _dept_scoped_ids(model, user: UserProfile, db: Session) -> list[UUID] | None:
    if _is_unrestricted(user):
        return None
    scope = _compute_scope(user, db)
    q = (
        db.query(model.id)
        .filter(model.is_deleted.is_(False))
        .filter(or_(*_dept_scoped_conditions(model, scope, user)))
    )
    if not bool(getattr(user, "is_exec", False)):
        # A non-exec sees an Exec-team row ONLY if they personally own it
        # (e.g. a department head who reports one number on the otherwise
        # confidential Leadership Scorecard). Never via department match,
        # a direct report, or leadership team-expansion — that would leak
        # the Exec/Leadership surface to a Team Manager.
        person = _person_for(user, db)
        own_pid = person.id if person is not None else None
        if own_pid is not None:
            q = q.filter(or_(model.is_exec.is_(False), model.owner_id == own_pid))
        else:
            q = q.filter(model.is_exec.is_(False))
    return [r[0] for r in q.all()]


def visible_issue_ids(user: UserProfile, db: Session) -> list[UUID] | None:
    return _dept_scoped_ids(Issue, user, db)


def visible_measurable_ids(user: UserProfile, db: Session) -> list[UUID] | None:
    return _dept_scoped_ids(ScorecardMetric, user, db)


def visible_todo_ids(user: UserProfile, db: Session) -> list[UUID] | None:
    return _dept_scoped_ids(Todo, user, db)


def visible_rock_ids(user: UserProfile, db: Session) -> list[UUID] | None:
    """Rocks the caller may read (uses the explicit ``visibility`` enum)."""
    if _is_unrestricted(user):
        return None
    scope = _compute_scope(user, db)
    dept_pids = _person_ids_in_depts(scope.dept_names, db)
    person = _person_for(user, db)
    own_pid = person.id if person is not None else None

    conds = [Rock.visibility == Visibility.COMPANY]
    # A DEPARTMENT rock is the team's rock two ways: via its own team tag
    # (``eos_rocks.department_name``, the Rocks-page team model) or,
    # for legacy/untagged rows, via the owner's seat. The tag condition is
    # what keeps a team's rocks visible to the whole team even when the
    # owner's Person row has no seat — seat-only derivation hid e.g. every
    # Delivery rock from every Delivery member, and the resulting "my rock vanished"
    # loop is how duplicate rocks get minted.
    if scope.dept_names:
        conds.append(
            (Rock.visibility == Visibility.DEPARTMENT)
            & func.lower(Rock.department_name).in_(_lower_set(scope.dept_names))
        )
    if dept_pids:
        conds.append(
            (Rock.visibility == Visibility.DEPARTMENT) & Rock.owner_id.in_(dept_pids)
        )
    if scope.owner_pids:
        conds.append(
            (Rock.visibility == Visibility.INDIVIDUAL)
            & Rock.owner_id.in_(scope.owner_pids)
        )
    # The owner always sees their own rock, whatever its visibility — a
    # DEPARTMENT rock owned by a seatless Person must not be invisible to the
    # very person who created it. (The ET restriction below still holds: this
    # is an OR-condition, and the non-exec tail keeps ET rocks
    # owner-or-exec-only.)
    if own_pid is not None:
        conds.append(Rock.owner_id == own_pid)
    # Exec-team ("ET") rocks are visible to the Exec team in full, regardless of
    # the visibility enum — the same grant the department-scoped resources give
    # execs (see ``_dept_scoped_conditions``). Without this, an exec who is NOT
    # org-wide ("*") only sees ET rocks that happen to be COMPANY-visible or
    # owned inside their own department scope, so an exec's Rocks page comes
    # back empty even though the ET rocks (default DEPARTMENT visibility,
    # department_name NULL) are theirs to see.
    if bool(getattr(user, "is_exec", False)):
        conds.append(Rock.is_exec.is_(True))
    # The people who keep every team's Rocks see every non-ET Rock, of any
    # visibility (see ``sees_every_team_rock``). ``is_exec`` is False here by
    # construction, and the non-exec tail below would drop an ET Rock anyway.
    if sees_every_team_rock(user, db):
        conds.append(Rock.is_exec.is_(False))
    q = db.query(Rock.id).filter(Rock.is_deleted.is_(False)).filter(or_(*conds))

    # Exec-team ("ET") rocks are visible to the Exec team ONLY, regardless of
    # the visibility enum. A non-exec sees an ET rock ONLY if they personally
    # own it (same owner carve-out as the department-scoped resources — a
    # non-exec owner must still see/maintain their own row). Never via a
    # company/department/individual match — that would leak the confidential
    # Exec surface to a Team Manager or teammate.
    if not bool(getattr(user, "is_exec", False)):
        if own_pid is not None:
            q = q.filter(or_(Rock.is_exec.is_(False), Rock.owner_id == own_pid))
        else:
            q = q.filter(Rock.is_exec.is_(False))

    return [r[0] for r in q.all()]


# ---- mutation gate (PATCH / DELETE) ---------------------------------------


def can_mutate(user: UserProfile, resource: object, db: Session) -> bool:
    """True iff `user` may PATCH/DELETE `resource`.

    A superset of the original delete rule (so nobody loses a permission
    they had) plus owner / manager / leadership:
      * admin, org-wide Exec (``sees_all_departments``), or Exec-team member
      * the row's creator (``created_by`` == email)
      * the owner (``owner_id`` == caller's Person id)
      * the owner's direct manager (owner sits in a seat reporting to the
        caller's seat)
      * leadership, when the row is COMPANY-scoped or in a department the
        caller actually *leads* (Team Manager → their managed team(s); dept
        derived from the owner's seat when the row carries no
        ``department_name``)

    Exec-team rows (``is_exec``) are special: a non-exec may mutate one ONLY
    when they personally own it (so a department head can still maintain their
    own Leadership-Scorecard measurable and its weekly entries). Creator,
    direct manager, and department leadership are all barred — the confidential
    Exec/Leadership surface is otherwise exec-only.

    Resources without ``owner_id`` / ``department_name`` (e.g. an IssueNote)
    fall back to the admin / Exec / creator rules — i.e. exactly the original
    delete rule.
    """
    return can_mutate_in_scope(
        mutate_scope(user, db),
        resource,
        lambda owner_id: _owner_seat_dept(owner_id, db),
    )


@dataclass
class MutateScope:
    """One caller's write reach, resolved once.

    ``can_mutate`` walks the caller's Person → seat → direct reports on every
    call. That is right for a single PATCH, but a list endpoint answering "may
    I edit this?" for every row would pay that walk N times. Resolving the
    CALLER half up front leaves the per-row check pure Python; the ROW half
    (the owner's department) stays lazy, because the short-circuits above it
    mean most rows never need it.
    """

    #: Admin / org-wide Exec / Exec-team member — may mutate anything.
    unrestricted: bool = False
    #: Case-folded, for comparison against the free-text ``created_by``.
    email: str = ""
    person_id: UUID | None = None
    report_pids: set[UUID] = field(default_factory=set)
    is_leader: bool = False
    led_dept_names: set[str] = field(default_factory=set)
    #: Teams the caller BELONGS to (not leads): their department, the teams
    #: they manage, their seat's department, and any admin-granted additional
    #: team a live L10 roster still carries. Used ONLY by
    #: ``can_update_rock_in_scope`` — membership is not a mutation grant
    #: anywhere else.
    team_dept_names: set[str] = field(default_factory=set)
    #: On the roster that keeps every team's Rocks (``sees_every_team_rock``).
    #: Set ONLY by ``rock_mutate_scope`` and read ONLY by
    #: ``can_update_rock_in_scope``; every other write path ignores it.
    every_team_rock: bool = False


def mutate_scope(user: UserProfile, db: Session) -> MutateScope:
    """Resolve everything ``can_mutate`` needs to know about ``user``."""
    email = (getattr(user, "email", None) or "").strip().lower()
    if _is_unrestricted(user) or bool(getattr(user, "is_exec", False)):
        return MutateScope(unrestricted=True, email=email)
    person = _person_for(user, db)
    seat = _seat_for_person(person, db)
    memberships = _user_depts(user, seat)
    # ORDER IS LOAD-BEARING, exactly as in ``compute_scope``: leadership is
    # derived from MEMBERSHIP before the roster widens it, or an exec would
    # lead every team whose L10 they were merely invited to.
    led = _led_depts(user, seat, memberships)
    return MutateScope(
        email=email,
        person_id=person.id if person is not None else None,
        report_pids=_direct_report_person_ids(seat, db),
        is_leader=_is_leadership(user, seat),
        led_dept_names=led,
        team_dept_names=memberships | roster_dept_names(user, db),
    )


def rock_mutate_scope(user: UserProfile, db: Session) -> MutateScope:
    """``mutate_scope`` plus the every-team-Rocks roster, for the Rock paths only."""
    scope = mutate_scope(user, db)
    if not scope.unrestricted:
        scope.every_team_rock = sees_every_team_rock(user, db)
    return scope


def can_mutate_in_scope(
    scope: MutateScope,
    resource: object,
    owner_seat_dept: Callable[[UUID], str | None],
) -> bool:
    """THE mutation rule, against an already-resolved caller (see ``can_mutate``).

    ``owner_seat_dept`` maps an owner's Person id to their seat's department.
    It is called at most once, and only on the leadership path for a row that
    carries no ``department_name`` of its own — so a caller holding many rows
    can hand over a batched lookup (``owner_seat_depts``) instead of paying a
    query per row.
    """
    if scope.unrestricted:
        return True

    owner_id = getattr(resource, "owner_id", None)
    is_owner = (
        owner_id is not None
        and scope.person_id is not None
        and owner_id == scope.person_id
    )

    # Exec-team rows are exec-only to mutate — EXCEPT the owner, who must keep
    # maintaining their own row (a non-exec department head entering their own
    # number on the confidential Leadership Scorecard; the scorecard entry
    # writes ride on this). Creator / direct manager / department leadership
    # are all barred from the Exec/Leadership surface.
    if bool(getattr(resource, "is_exec", False)) and not is_owner:
        return False

    # Case-folded for the same reason as ``person_by_email``: ``created_by`` is
    # a free-text audit column written by whatever path created the row, so
    # comparing it raw can strip a creator of the creator grant on their own row.
    created_by = getattr(resource, "created_by", None)
    if created_by and created_by.strip().lower() == scope.email:
        return True

    if is_owner:
        return True
    if owner_id is not None and owner_id in scope.report_pids:
        return True

    if not scope.is_leader:
        return False

    # Leadership path: COMPANY-scoped rows, or rows in the department(s) the
    # caller actually leads (a Team Manager → only their managed team(s)).
    if getattr(resource, "visibility", None) == Visibility.COMPANY:
        return True
    res_dept = getattr(resource, "department_name", None)
    if res_dept is None and owner_id is not None:
        res_dept = owner_seat_dept(owner_id)
    return bool(
        res_dept is not None and res_dept.lower() in _lower_set(scope.led_dept_names)
    )


def _row_team_dept(
    row: object, owner_seat_dept: Callable[[UUID], str | None]
) -> str | None:
    """The team a Rock or measurable belongs to: its own tag, else the owner's seat.

    Same derivation the leadership path uses — the team is ``department_name``
    (the Rocks-page team model; a measurable's Team) and, for legacy rows
    that were never tagged, the department of the owner's seat.
    """
    dept = getattr(row, "department_name", None)
    if dept is None:
        owner_id = getattr(row, "owner_id", None)
        if owner_id is not None:
            dept = owner_seat_dept(owner_id)
    return dept


def can_update_rock_in_scope(
    scope: MutateScope,
    rock: object,
    owner_seat_dept: Callable[[UUID], str | None],
) -> bool:
    """May this caller UPDATE this Rock? ``can_mutate`` widened by team membership.

    A Rock is the team's commitment, and the team reviews it together: the L10
    Rock Review is where status moves and milestones get ticked, usually by
    whoever is running the meeting rather than by each owner in turn. Gating
    that on owner / creator / manager / leadership blocked the meeting itself:
    most of the people updating a Rock during an L10 were refused. So ANY member
    of the Rock's team may update it.

    This is deliberately narrower than "membership implies write" in three ways:

      * **Rocks only.** Issues, To-Dos and Scorecard measurables keep the
        owner/manager/leadership rule; this function is not generic. (A
        measurable's weekly VALUES have their own, equally narrow team grant:
        ``can_record_scorecard_value_in_scope``.)
      * **Update only, never delete.** ``can_mutate`` still gates DELETE, so a
        teammate can move a Rock's status but cannot remove the team's
        commitment. Deleting stays with the owner, the creator, the owner's
        manager and the team's leadership.
      * **Never the Exec team, never someone's personal Rock.** An ``is_exec``
        Rock stays exec-or-owner, and an INDIVIDUAL-visibility Rock is personal
        — a teammate cannot even READ one, and write must never exceed read.

    The one wider grant is the every-team-Rocks roster (``every_team_rock``,
    resolved by ``rock_mutate_scope``): those people READ every non-ET Rock,
    personal ones included, so they may update every one of them. Still
    update only, and still never the Exec team.
    """
    if can_mutate_in_scope(scope, rock, owner_seat_dept):
        return True
    # Belt and braces: ``can_mutate_in_scope`` already refuses a non-owner on an
    # Exec row, but this grant must never be the thing that opens that surface.
    if bool(getattr(rock, "is_exec", False)):
        return False
    if scope.every_team_rock:
        return True
    if getattr(rock, "visibility", None) == Visibility.INDIVIDUAL:
        return False
    dept = _row_team_dept(rock, owner_seat_dept)
    return bool(dept is not None and dept.lower() in _lower_set(scope.team_dept_names))


def can_record_scorecard_value_in_scope(
    scope: MutateScope,
    metric: object,
    owner_seat_dept: Callable[[UUID], str | None],
) -> bool:
    """May this caller record a WEEKLY VALUE on this measurable?

    ``can_mutate`` widened by team membership — the Scorecard twin of
    ``can_update_rock_in_scope``. A team's weekly numbers are usually typed in
    by one person before or during the L10, not by each owner on their own row.
    Gating every cell on owner / manager / leadership refused that person on
    every teammate's row, behind a bare "Failed to save weekly value", so most
    of the saves typed in by that one person were refused. So ANY member of the
    measurable's team may record its weekly value.

    Deliberately narrow, the same way the Rock grant is:

      * **Values only.** Renaming a measurable, changing its goal, owner or
        team, and deleting it keep ``can_mutate`` (PATCH / DELETE /metrics).
      * **Never the Exec team.** An ``is_exec`` measurable (the confidential
        Leadership Scorecard) stays exec-or-owner.
      * **Write never exceeds read.** ``team_dept_names`` is exactly the
        membership that makes a team's measurables visible
        (``compute_scope.dept_names``), so this never reaches a row the caller
        cannot already see.
    """
    if can_mutate_in_scope(scope, metric, owner_seat_dept):
        return True
    if bool(getattr(metric, "is_exec", False)):
        return False
    dept = _row_team_dept(metric, owner_seat_dept)
    return bool(dept is not None and dept.lower() in _lower_set(scope.team_dept_names))


def can_record_scorecard_value(user: UserProfile, metric: object, db: Session) -> bool:
    """``can_record_scorecard_value_in_scope`` for one measurable."""
    return can_record_scorecard_value_in_scope(
        mutate_scope(user, db), metric, lambda oid: _owner_seat_dept(oid, db)
    )


def require_can_record_scorecard_value(
    user: UserProfile, metric: object, db: Session
) -> None:
    """Raise 403 unless `user` may record a weekly value on `metric`."""
    if not can_record_scorecard_value(user, metric, db):
        raise HTTPException(
            status_code=403,
            detail=(
                "Only this measurable's team can record its weekly numbers. "
                "Ask its owner or the team's manager to enter it."
            ),
        )


def scorecard_value_writable_ids(
    user: UserProfile, metrics: Sequence[object], db: Session
) -> dict[UUID, bool]:
    """``{metric.id: may this caller record a weekly value}`` for a whole list."""
    scope = mutate_scope(user, db)
    if scope.unrestricted:
        return {m.id: True for m in metrics}
    depts = owner_seat_depts((getattr(m, "owner_id", None) for m in metrics), db)
    return {
        m.id: can_record_scorecard_value_in_scope(scope, m, depts.get) for m in metrics
    }


def can_update_rock(user: UserProfile, rock: object, db: Session) -> bool:
    """``can_update_rock_in_scope`` for a single Rock (see it for the rule)."""
    return can_update_rock_in_scope(
        rock_mutate_scope(user, db), rock, lambda oid: _owner_seat_dept(oid, db)
    )


def require_can_update_rock(user: UserProfile, rock: object, db: Session) -> None:
    """Raise 403 unless `user` may update `rock`."""
    if not can_update_rock(user, rock, db):
        raise HTTPException(
            status_code=403,
            detail="You don't have permission to modify this Rock.",
        )


def _owner_seat_dept(owner_id: UUID, db: Session) -> str | None:
    owner_seat = _seat_for_owner(owner_id, db)
    return owner_seat.department_name if owner_seat else None


def owner_seat_depts(
    owner_ids: Iterable[UUID | None], db: Session
) -> dict[UUID, str | None]:
    """``{owner Person id: their seat's department}`` for many owners, in ONE
    query — the batched form of ``_owner_seat_dept``."""
    ids = {oid for oid in owner_ids if oid is not None}
    if not ids:
        return {}
    rows = (
        db.query(Person.id, Seat.department_name)
        .join(Seat, Seat.id == Person.seat_id)
        .filter(
            Person.id.in_(ids),
            Person.is_deleted.is_(False),
            Seat.is_deleted.is_(False),
        )
        .all()
    )
    return {pid: dept for pid, dept in rows}


def rock_write_flags(
    user: UserProfile, rocks: Sequence[object], db: Session
) -> dict[UUID, tuple[bool, bool]]:
    """``{rock.id: (may update, may delete)}`` for a whole list, cheaply.

    The two verbs differ for Rocks (see ``can_update_rock_in_scope``), so the
    client needs both: the Edit / Add-milestone / Log-update controls follow
    the first, the Delete control follows the second.
    """
    scope = rock_mutate_scope(user, db)
    if scope.unrestricted:
        return {rock.id: (True, True) for rock in rocks}
    depts = owner_seat_depts((getattr(r, "owner_id", None) for r in rocks), db)
    return {
        rock.id: (
            can_update_rock_in_scope(scope, rock, depts.get),
            can_mutate_in_scope(scope, rock, depts.get),
        )
        for rock in rocks
    }


def mutable_ids(
    user: UserProfile, rows: Sequence[object], db: Session
) -> dict[UUID, bool]:
    """``{row.id: may this caller PATCH/DELETE it}`` for a whole list.

    Same rule as ``can_mutate``, one caller-resolution and one owner-department
    query for the entire list. List endpoints use this to tell the client which
    rows it may write, so the UI never offers an Edit button the gate refuses.
    """
    scope = mutate_scope(user, db)
    if scope.unrestricted:
        return {row.id: True for row in rows}
    depts = owner_seat_depts((getattr(r, "owner_id", None) for r in rows), db)
    return {row.id: can_mutate_in_scope(scope, row, depts.get) for row in rows}


def require_can_mutate(user: UserProfile, resource: object, db: Session) -> None:
    """Raise 403 unless `user` may mutate `resource` (see `can_mutate`)."""
    if not can_mutate(user, resource, db):
        raise HTTPException(
            status_code=403,
            detail="You don't have permission to modify this item.",
        )


# ---- per-row READ gate (GET-by-id and sub-resource reads) ------------------
#
# The list endpoints fold `visible_*_ids` into their query, but a by-id GET
# (or a sub-resource read like an issue's notes / a rock's weekly updates)
# used to return the row to ANY authenticated caller. That leaks the
# confidential Exec-team surface to whoever holds an id. These helpers run
# the same visibility conditions against a single row.

_VISIBLE_IDS_BY_MODEL = {
    Issue: visible_issue_ids,
    Todo: visible_todo_ids,
    ScorecardMetric: visible_measurable_ids,
    Rock: visible_rock_ids,
}


def can_read(user: UserProfile, resource: object, db: Session) -> bool:
    """True iff `user` may READ `resource` (Issue / Todo / ScorecardMetric /
    Rock). Resources without a visibility model fall back to allowed."""
    helper = _VISIBLE_IDS_BY_MODEL.get(type(resource))
    if helper is None:
        return True
    visible = helper(user, db)
    if visible is None:  # unrestricted caller
        return True
    return resource.id in set(visible)


def require_can_read(user: UserProfile, resource: object, db: Session) -> None:
    """Raise 404 unless `user` may read `resource`.

    404 — not 403 — so a caller probing ids can't distinguish "doesn't
    exist" from "exists but is confidential" (e.g. an Exec-team issue).
    """
    if not can_read(user, resource, db):
        raise HTTPException(status_code=404, detail="Not found")


# ---- exec-scope write gate --------------------------------------------------


def require_exec_scope_allowed(user: UserProfile, is_exec: bool | None) -> None:
    """Raise 403 when a non-exec touches the Exec-team scope. Callers pass
    ``is_exec`` only when it matters: the flag on a create payload when True,
    or the new value of a PATCH that would CHANGE the flag (either direction —
    flipping an Exec row to a department row would declassify confidential
    content). Pass ``None`` when no exec-relevant change is happening."""
    if is_exec is None:
        return
    if _is_unrestricted(user) or bool(getattr(user, "is_exec", False)):
        return
    raise HTTPException(
        status_code=403,
        detail="Only Exec team members can manage Exec-team items.",
    )


# ---- L10 meeting access (meetings, sessions, headlines/inputs) --------------
#
# An L10 meeting belongs to a TEAM: the Exec/Leadership team
# (``is_exec_meeting``) or a department. Everything hanging off the meeting —
# sessions (notes, ratings, surveys), attendance, headlines & ideas
# (``L10Input``), the in-meeting items payload — inherits that team's
# confidentiality:
#
#   * Exec meeting → Exec team members (``is_exec``) ONLY. A calendar roster
#     does NOT override this — being invited without ``is_exec`` does not
#     grant access to the confidential Exec surface.
#   * Calendar-imported meeting (non-empty ``roster_emails``) → the INVITE
#     LIST, plus the meeting's manager. Department membership does NOT grant
#     access. When we know who was invited, that list IS the ACL: the
#     department stamped by ``_infer_import_department`` is a ROUTING hint
#     (which team's rocks / IDS / scorecard the meeting aggregates), never a
#     grant. Treating it as a grant is how one person's own imported meeting
#     showed up for their whole department — the organizer had invited nobody
#     else.
#   * Manually-scheduled department meeting (no roster) → members of that
#     department (case-insensitive), Team Managers of it, the holder of a
#     seat in it, or the meeting's manager. With no invite list to consult,
#     the team IS the roster.
#   * Unscoped meeting (no department, no roster, not exec) → company-wide.
#
# NOTE: meeting access is MEMBERSHIP-only, with exactly ONE exception — the
# org-wide Exec (``sees_all_departments``: on the Exec team AND holding the
# ``*`` wildcard, i.e. the CEO). That role already reads every department's
# Rocks / Issues / To-Dos / Scorecard via ``_is_unrestricted``, so it sees
# every department's L10s as well: a person in that role who co-leads
# departments sees their L10s alongside the rest of their EOS content. The bypass is
# deliberately keyed on that ROLE and not on ``admin``: a Cubie admin is an
# operator, not an executive, and must NOT gain the right to read another
# team's diary just by holding the role. Everyone else — admins, scoped
# execs, Team Managers, teammates — stays membership-only: nobody browses
# another team's L10 (its schedule, diary, notes, headlines or items). Admins
# keep STRUCTURAL management of any meeting (create / rename / move / delete)
# via the explicit role check on those endpoints, and may break-glass drive a
# *live* session's cursor (``_session_manager_or_403``) — neither of which
# reads confidential content.
#
# The roster-is-the-ACL rule is otherwise untouched: a calendar-imported
# meeting's invite list still overrides department membership for every
# non-CEO caller, which is what stops one person's solo imported meeting from
# surfacing to their whole department.


@dataclass
class _MeetingScope:
    is_exec: bool
    dept_keys: set[str]  # lower-cased department names the caller belongs to
    person_id: UUID | None
    email: str
    # Org-wide Exec (the CEO): the ONE role that reads every team's L10.
    # NOT admin — see the module note above.
    sees_all: bool = False


def meeting_roster(meeting) -> list[str]:
    """Lower-cased invite list of a calendar-imported meeting, or ``[]`` for a
    manually-scheduled one. A non-empty roster makes the meeting roster-scoped
    (see the module note) — exported so every "is this meeting mine?" check
    reads the same signal instead of re-deriving it."""
    return [e.lower() for e in (getattr(meeting, "roster_emails", None) or []) if e]


def _meeting_scope(user: UserProfile, db: Session) -> _MeetingScope:
    person = _person_for(user, db)
    seat = _seat_for_person(person, db)
    return _MeetingScope(
        is_exec=bool(getattr(user, "is_exec", False)),
        dept_keys=_lower_set(_user_depts(user, seat)),
        person_id=person.id if person is not None else None,
        email=(user.email or "").lower(),
        sees_all=bool(getattr(user, "sees_all_departments", False)),
    )


def _meeting_accessible(meeting, scope: _MeetingScope) -> bool:
    # The org-wide Exec (CEO) sees every team's L10 — the single exception to
    # the membership-only rule, and the only check that outranks a calendar
    # roster. Everyone else falls through to the membership tests below.
    if scope.sees_all:
        return True
    # Membership only for everyone else — no admin bypass (see the module note
    # above): nobody else browses another team's L10.
    if bool(getattr(meeting, "is_exec_meeting", False)):
        # Exec team ONLY — no roster / manager / department override.
        return scope.is_exec
    # Whoever runs the meeting always sees it, roster or not.
    if scope.person_id is not None and meeting.manager_id == scope.person_id:
        return True
    roster = meeting_roster(meeting)
    if roster:
        # Calendar-imported: the invite list IS the ACL (see the module note).
        # Not on it → not your meeting, even if it carries your department.
        return bool(scope.email) and scope.email in roster
    dept = getattr(meeting, "department_name", None)
    if not dept:
        return True  # unscoped, company-wide meeting
    return dept.lower() in scope.dept_keys


def can_access_meeting(user: UserProfile, meeting, db: Session) -> bool:
    """True iff `user` may see this L10 meeting and its sessions / inputs."""
    return _meeting_accessible(meeting, _meeting_scope(user, db))


def require_meeting_access(user: UserProfile, meeting, db: Session) -> None:
    """Raise 404 unless `user` may access `meeting` (404 hides existence —
    non-exec callers must not learn which ids are Exec meetings)."""
    if not can_access_meeting(user, meeting, db):
        raise HTTPException(status_code=404, detail="Meeting not found")


# ---- L10 structural authority (who may re-point a meeting) ------------------
#
# Distinct from READ access above. Reading an L10 exposes confidential CONTENT
# (diary, notes, headlines, ratings) and so stays membership-only. *Managing*
# one — renaming, rescheduling, moving teams, handing the facilitator role over
# — exposes nothing, and gating it on "is the current manager" alone is what
# locks a whole team out the moment that one person goes on vacation: only they
# or an admin could re-point the meeting, and admins can't ENUMERATE meetings
# they aren't in (``accessible_meeting_ids`` is membership-scoped for everyone),
# so in practice nobody could.
#
# ``meeting_authority`` names the rung the caller stands on so callers can both
# gate and explain. The rungs, most to least privileged:
#
#   "admin"      — Cubie operator. Structural only; grants NO content read.
#   "org_exec"   — org-wide Exec (the CEO); already reads every team's L10.
#   "exec_team"  — an Exec-team member, for the Exec L10 only.
#   "manager"    — the meeting's current manager (the normal handoff path).
#   "leadership" — Team Manager of / leadership seat in the meeting's own
#                  department. THE fix for the vacation lockout: a team's own
#                  leader can re-point their team's meeting without an admin.
#   None         — no structural authority. Such a caller may still take the
#                  meeting over when the manager is verifiably unavailable, but
#                  that break-glass rung is decided in ``_manager_access`` (it
#                  needs the manager's calendar) — never here.
#
# The Exec fence is preserved exactly: a Team Manager's leadership rung is
# checked against the meeting's department, and an Exec meeting carries none, so
# leadership can never reach the Exec L10.

_MeetingAuthority = str | None


def meeting_authority(user: UserProfile, meeting, db: Session) -> _MeetingAuthority:
    """Name the structural-authority rung `user` holds over `meeting`, or None.

    See the section note above for the rung ladder. Returned as a string (not a
    bool) so a 403 can say *why* the caller is short, and so the audit log can
    record which rung was used to re-point a meeting.
    """
    if _is_admin(user):
        return "admin"
    if bool(getattr(user, "sees_all_departments", False)):
        return "org_exec"

    is_exec = bool(getattr(user, "is_exec", False))
    if bool(getattr(meeting, "is_exec_meeting", False)):
        # The Exec L10 is managed by the Exec team and nobody else — no
        # manager / leadership / department override reaches inside the fence.
        # Any ET member may re-point it, which is what stops the ET from being
        # locked out when whoever runs it is away.
        return "exec_team" if is_exec else None

    person = _person_for(user, db)
    if person is not None and meeting.manager_id == person.id:
        return "manager"

    seat = _seat_for_person(person, db)
    if not _is_leadership(user, seat):
        return None
    dept = getattr(meeting, "department_name", None)
    if not dept:
        # An unscoped (company-wide) meeting has no team to lead, so leadership
        # confers nothing here. Deliberately narrow: letting every Team Manager
        # re-point every unscoped meeting would be a far wider grant than the
        # lockout this helper exists to fix.
        return None
    led = _led_depts(user, seat, _user_depts(user, seat))
    return "leadership" if dept.lower() in _lower_set(led) else None


def can_manage_meeting(user: UserProfile, meeting, db: Session) -> bool:
    """True iff `user` may structurally manage `meeting` (see `meeting_authority`)."""
    return meeting_authority(user, meeting, db) is not None


def accessible_meeting_ids(user: UserProfile, db: Session) -> list[UUID]:
    """Ids of the L10 meetings the caller may see. Membership-scoped for
    EVERYONE (admins and the CEO included — see the module note): the list
    endpoints (meetings, sessions) fold this into their query so nobody is
    shown another team's L10. May be empty."""
    from rubik_core.database.eos import L10Meeting  # local: avoid import cycle

    scope = _meeting_scope(user, db)
    meetings = db.query(L10Meeting).filter(L10Meeting.is_deleted.is_(False)).all()
    return [m.id for m in meetings if _meeting_accessible(m, scope)]

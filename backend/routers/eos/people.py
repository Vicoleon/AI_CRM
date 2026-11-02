"""EOS people — read-only roster, sourced from the main system.

Mounted at `/api/eos/people`. Optional `email` query filter exposes a
single-row lookup used by the frontend to bridge a Cubie `users.email`
to an `eos_people.id`.

The dedicated EOS People/Seats management UI was removed because
it duplicated the main system's user & department management. People are
managed in the main system (the `users` table) now; `eos_people` is kept
only as the identity backbone that EOS ownership FKs point at
(rocks/todos/issues/scorecard owners, L10 manager + attendance, GWC). To
keep that backbone populated without a manual page, this router lazily
**syncs** an `eos_people` row for every main-system user on read, so the
owner-pickers across the EOS pages always reflect the main system.

There are intentionally NO create/update/delete endpoints here anymore —
the main system owns people. Rows can still be auto-created by the L10
calendar import (`_ensure_person`) and by this sync.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException

from backend.models.eos.people import PersonResponse
from backend.routers.eos._authz import sees_every_team_rock
from backend.routers.eos._shared import CurrentUser, DBDep
from rubik_core.auth.registry import UserProfile
from rubik_core.database.eos import Person
from rubik_core.database.models import User
from rubik_core.database.schemas import ALL_DEPARTMENTS

router = APIRouter(prefix="/people", tags=["EOS: People"])


def _split_name(full_name: str, email: str) -> tuple[str, str]:
    """Best-effort first/last split from a main-system display name.

    Falls back to the email local-part when the name is blank, mirroring
    the L10 calendar-import `_ensure_person` placeholder behavior.
    """
    name = (full_name or "").strip()
    if not name:
        local = email.split("@", 1)[0]
        parts = local.replace(".", " ").replace("_", " ").split()
        first = parts[0].capitalize() if parts else email
        last = parts[1].capitalize() if len(parts) > 1 else ""
        return first, last
    bits = name.split(" ", 1)
    return bits[0], (bits[1] if len(bits) > 1 else "")


def _is_real_user(email: str | None) -> bool:
    """Exclude synthetic / non-person accounts from the EOS roster.

    Cubie's impersonation feature mints pseudo-users like
    `impersonated_user@compliance.local`; the `.local` TLD (and the
    impersonation marker) are not real people and shouldn't appear in
    owner-pickers — nor should they be carried into `eos_people`.
    """
    if not email or "@" not in email:
        return False
    if email.endswith(".local"):
        return False
    if email.split("@", 1)[0] == "impersonated_user":
        return False
    return True


def _sync_people_from_users(db) -> None:
    """Upsert an `eos_people` row for each REAL main-system user.

    The main `users` table is the source of truth for who exists and what
    their name is, so we refresh name + google_id from it. Matching is by
    email (the soft bridge documented on `eos_people.email`). Synthetic
    accounts (impersonation pseudo-users) are skipped, and any that were
    previously synced in are soft-deleted so the picker self-heals. Best-
    effort: a failure here must not break the owner-picker, so we roll back
    and let the caller return whatever rows already exist.
    """
    try:
        users = db.query(User).all()
        # Keyed case-insensitively: eos_people.email is lower-cased on write
        # (``Person._normalize_email``), but a row written around the ORM would
        # otherwise MISS here and send us down the create branch — where the
        # unique constraint on email raises on commit, the bare except below
        # rolls back, and EVERY user's name/google_id refresh is lost silently
        # along with it.
        existing = {
            (p.email or "").strip().lower(): p
            for p in db.query(Person).filter(Person.email.isnot(None)).all()
        }
        changed = False
        for u in users:
            person = existing.get((u.email or "").strip().lower())
            if u.is_platform_support or not _is_real_user(u.email):
                # Don't create; retire any pseudo-user row a prior sync added.
                if person is not None and not person.is_deleted:
                    person.is_deleted = True
                    person.updated_by = "system-sync"
                    changed = True
                continue
            first, last = _split_name(u.name, u.email)
            if person is None:
                db.add(
                    Person(
                        first_name=first,
                        last_name=last,
                        email=u.email,
                        google_id=u.id,
                        is_deleted=False,
                        created_by="system-sync",
                    )
                )
                changed = True
            else:
                # Keep the main system authoritative for identity fields,
                # and un-delete anyone who is an active main-system user.
                if (
                    person.first_name != first
                    or person.last_name != last
                    or person.google_id != u.id
                    or person.is_deleted
                ):
                    person.first_name = first
                    person.last_name = last
                    person.google_id = u.id
                    person.is_deleted = False
                    person.updated_by = "system-sync"
                    changed = True
        if changed:
            db.commit()
    except Exception:
        db.rollback()


def _concrete_department(dept: str | None) -> str | None:
    """Return a real, assignable department name, or ``None``.

    A user is only a valid Rock owner if they sit in an actual department —
    NOT the ``general`` catch-all and NOT the ``*`` org-wide wildcard. This
    is what keeps test/seed accounts (``*@example.com``), service accounts
    (``notifications@``, ``systems@``) and unassigned users out of the
    owner picker: they carry ``general`` / blank departments.
    """
    d = (dept or "").strip()
    if not d or d == ALL_DEPARTMENTS or d.lower() in ("general", "all"):
        return None
    return d


def _departmental_users_by_email(db) -> dict[str, User]:
    """Real main-system users who have a concrete department, keyed by email."""
    return {
        u.email: u
        for u in db.query(User).filter(User.is_platform_support.is_(False)).all()
        if _is_real_user(u.email) and _concrete_department(u.department) is not None
    }


def _managing_emails_for(dept_keys: set[str], db) -> set[str]:
    """Emails of the real users who **manage** one of ``dept_keys``.

    The department match above is one-directional: it answers "who else sits
    in my team", so a manager sees *down* into the teams they lead but a team
    member never sees *up* to whoever leads theirs. That hid a
    cross-department director — an Exec who leads several teams from a single
    primary ``department`` (e.g. a Director of Operations over Compliance /
    Support / Delivery) — from every team they actually run: their own team couldn't
    check them in, hand them the L10, or assign them a Rock / To-Do / Issue.

    Matched on ``managed_departments``, so a person is pulled in only for the
    teams an admin explicitly put them in charge of. The ``*`` org-wide
    wildcard is NOT a management claim over every team (``_concrete_department``
    drops it), so an org-wide Exec / CEO does not land in all 60-odd users'
    owner pickers. Unlike the department match this does not require a concrete
    primary ``department``, so a leader who sits in no single team still
    resolves for the teams they lead.
    """
    if not dept_keys:
        return set()
    emails: set[str] = set()
    for u in db.query(User).filter(User.is_platform_support.is_(False)).all():
        if not _is_real_user(u.email):
            continue
        for raw in u.managed_departments or []:
            managed = _concrete_department(raw)
            if managed and managed.lower() in dept_keys:
                emails.add(u.email)
                break
    return emails


def _visible_emails_for(current_user: UserProfile, db) -> set[str] | None:
    """Emails of the departmental users the caller may see as owners.

    Only users with a concrete department are ever eligible (see
    :func:`_concrete_department`). On top of that:

    - Admin / org-wide Exec (CEO, ``*``) → every departmental user
      (returned as ``None`` = unrestricted; the caller filters to the
      departmental set separately).
    - A departmental user sees only their own ``department``.
    - An Exec / Team Manager sees their own department plus every
      department in ``managed_departments`` (the teams they manage).
    - Anyone who **manages** one of those departments, whichever team they
      sit in themselves (see :func:`_managing_emails_for`).

    The caller's own email is included when the caller is themselves a
    departmental user, so they can always assign a rock to themselves.
    """
    if "admin" in (current_user.roles or []) or current_user.sees_all_departments:
        return None

    visible_depts: set[str] = set()
    own = _concrete_department(current_user.department)
    if own:
        visible_depts.add(own)
    for raw in getattr(current_user, "managed_departments", None) or []:
        d = _concrete_department(raw)
        if d:
            visible_depts.add(d)

    # Case-insensitive department match against departmental users only.
    wanted = {d.lower() for d in visible_depts}
    emails = {
        email
        for email, u in _departmental_users_by_email(db).items()
        if (u.department or "").strip().lower() in wanted
    }
    # ...plus whoever LEADS one of those departments from another team.
    emails |= _managing_emails_for(wanted, db)
    # The caller sees themselves iff they are a departmental user.
    if current_user.email and _concrete_department(current_user.department):
        emails.add(current_user.email)
    return emails


def _exec_team_emails(db) -> set[str]:
    """Emails of every real Exec-team member (``users.is_exec``).

    Exec-team members can assign a Rock / To-Do / IDS issue to one another
    regardless of department, so for an exec caller they are always eligible
    owners — even the ones who sit outside the caller's own / managed teams
    (or carry a non-concrete ``general`` / ``*`` department, like an org-wide
    Exec). Every real user has a synced ``eos_people`` row, so these resolve.
    """
    return {
        u.email
        for u in db.query(User)
        .filter(User.is_exec.is_(True), User.is_platform_support.is_(False))
        .all()
        if _is_real_user(u.email)
    }


def eligible_owner_people(
    db, current_user: UserProfile, *, every_team: bool = False
) -> list[Person]:
    """The people the caller may assign as an owner — i.e. exactly what the
    owner dropdown on every EOS page contains.

    Factored out of ``list_people`` so a server-side writer can enforce the
    same roster the UI offers: the Scorecard uploader uses it both to build the
    template's Owner dropdown and to reject a row that hands a measurable to
    somebody outside the caller's teams.

    ``every_team`` (the Rocks page, for the people who keep every team's
    Rocks) widens the roster to every departmental user, as for an admin, so
    every team's Rock shows its owner and can be handed to one.
    """
    # Sync from the main system first so the roster always reflects the
    # current admin-managed users without a manual EOS People page.
    _sync_people_from_users(db)

    # Restrict to real, admin-managed users who sit in a concrete department
    # (drops calendar-import placeholders, test/seed accounts and service
    # accounts), then scope to the department(s) the caller may see.
    visible_emails = None if every_team else _visible_emails_for(current_user, db)
    if visible_emails is None:
        # Unrestricted, but still only departmental users.
        real_emails = set(_departmental_users_by_email(db))
    else:
        real_emails = set(visible_emails)
    # An Exec-team member can assign to any other Exec-team member, across
    # departments — union them in on top of the department-scoped set.
    if bool(getattr(current_user, "is_exec", False)):
        real_emails |= _exec_team_emails(db)
    return (
        db.query(Person)
        .filter(Person.is_deleted == False, Person.email.in_(real_emails))  # noqa: E712
        .all()
    )


@router.get("/", response_model=list[PersonResponse])
def list_people(
    db: DBDep,
    current_user: CurrentUser,
    email: str | None = None,
    surface: str | None = None,
):
    # A single-row bridge lookup (users.email -> eos_people.id) is used by
    # the frontend to resolve a known owner; return it directly, unscoped.
    if email:
        _sync_people_from_users(db)
        return (
            db.query(Person)
            .filter(Person.is_deleted == False, Person.email == email)  # noqa: E712
            .all()
        )

    # Otherwise this is the owner picker. ``?surface=rocks`` is the Rocks page:
    # the people who keep every team's Rocks need every team's owners there,
    # and only there (see ``_authz.sees_every_team_rock``).
    every_team = surface == "rocks" and sees_every_team_rock(current_user, db)
    return eligible_owner_people(db, current_user, every_team=every_team)


@router.get("/{person_id}", response_model=PersonResponse)
def get_person(person_id: UUID, db: DBDep, current_user: CurrentUser):
    person = (
        db.query(Person)
        .filter(Person.id == person_id, Person.is_deleted == False)  # noqa: E712
        .first()
    )
    if not person:
        raise HTTPException(status_code=404, detail="Person not found")
    return person

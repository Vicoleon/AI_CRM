"""L10 manager hand-off — the break-glass rung, and how we prove it's open.

`_authz.meeting_authority` covers everyone who holds standing authority over a
meeting (admin, org-wide Exec, the Exec team over the Exec L10, the current
manager, and the meeting department's own leadership). This module covers the
case none of those rungs reach:

    The manager left for vacation without handing the L10 over, and the team
    has no Team Manager on it. Nobody can re-point the meeting, so the team
    can't run its own L10 until that one person comes back.

The rung added here lets a **member of the meeting take it over for
themselves** — but only while the manager is *verifiably* unavailable, and only
onto themselves. Both limits matter:

* **Verifiably** — unavailability is read from data (no manager set, the manager
  is offboarded, or an OOO period covers today or the meeting's next
  occurrence), never from the claimant asserting an emergency. A "force
  takeover, type a reason" button would let anyone seize any meeting they can
  see, whenever they liked; the fence would be gone rather than widened.
* **Onto themselves** — break-glass promotes the claimant, it does not let them
  appoint a third party. Handing the role to someone else stays with the
  manager and the team's leadership, so the widest thing this rung can do is
  "someone on the team is now running the team's own meeting".

Everything else about the EOS fence is untouched: the claimant must already be
able to ACCESS the meeting (roster / department membership per
`can_access_meeting`), so this grants nothing to an outsider, and the Exec L10
is unreachable — a non-exec can't access it at all, and any Exec-team member
already holds the ``exec_team`` rung, so no Exec meeting ever lands on this
path.

Availability is read from the cached ``ooo_periods`` table (materialised daily
by :func:`rubik_core.availability.store.refresh_ooo_periods`) rather than by
impersonating the manager's calendar — a permission check must not depend on a
live Google round-trip. A live probe is used only as a *fallback*, when the
cache holds nothing at all for that manager: someone who books PTO and leaves
the same afternoon isn't in the table until the next refresh, and that is
exactly the person whose team is stuck. Both entry points here are explicit,
user-initiated single-meeting actions, so one calendar call is affordable —
never call this from a list endpoint.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.routers.eos._authz import (
    can_access_meeting,
    meeting_authority,
    person_by_email,
)
from rubik_core.auth.registry import UserProfile
from rubik_core.database.eos import Person
from rubik_core.database.models import User

logger = logging.getLogger(__name__)

__all__ = [
    "ManagerAvailability",
    "authorize_manager_transfer",
    "manager_availability",
    "next_occurrence",
]

# Reason codes. The UI branches on these; ``detail`` carries the sentence.
REASON_AVAILABLE = "available"
REASON_NO_MANAGER = "no_manager"
REASON_OFFBOARDED = "offboarded"
REASON_ON_PTO = "on_pto"

# The authority label recorded when a takeover rides the break-glass rung.
AUTHORITY_BREAK_GLASS = "break_glass"


@dataclass(frozen=True)
class ManagerAvailability:
    """Whether a meeting's manager can currently be reached to hand it over.

    ``available`` False is what opens the break-glass rung; ``reason`` says
    which of the three unavailability cases fired, and ``detail`` is the
    sentence the UI shows so the team knows why the button appeared.
    """

    available: bool
    reason: str
    detail: str
    manager_name: str | None = None
    manager_email: str | None = None
    out_from: str | None = None  # ISO date, set when reason == on_pto
    out_until: str | None = None  # ISO date, set when reason == on_pto
    returns_on: str | None = None  # ISO date, set when reason == on_pto

    def as_dict(self) -> dict:
        return {
            "available": self.available,
            "reason": self.reason,
            "detail": self.detail,
            "manager_name": self.manager_name,
            "manager_email": self.manager_email,
            "out_from": self.out_from,
            "out_until": self.out_until,
            "returns_on": self.returns_on,
        }


def next_occurrence(meeting, today: date) -> date | None:
    """The next calendar date this recurring meeting lands on, today included.

    An L10 stores a recurring ``day_of_week`` (0=Mon..6=Sun) rather than dated
    occurrences, so "is the manager out when we next meet?" has to be derived.
    Returns None when the meeting carries no weekday (an unscheduled slot), in
    which case only *today* is checked.
    """
    dow = getattr(meeting, "day_of_week", None)
    if dow is None:
        return None
    try:
        dow = int(dow)
    except (TypeError, ValueError):
        return None
    if not 0 <= dow <= 6:
        return None
    return today + timedelta(days=(dow - today.weekday()) % 7)


def _person_name(person: Person | None) -> str | None:
    if person is None:
        return None
    name = f"{person.first_name or ''} {person.last_name or ''}".strip()
    return name or (person.email or None)


def _has_active_account(db: Session, email: str | None) -> bool:
    """True iff ``email`` still has a row in ``users``.

    Offboarding removes the ``users`` row (a roster reconciliation can drop
    a third of the table at once) while the ``eos_people`` row
    is deliberately kept so historical Rocks / To-Dos / meetings keep their
    owner. So "has an EOS Person but no user account" is precisely the
    departed-employee case — and a meeting still pointed at them can never be
    handed over by its manager, because that manager no longer exists.
    """
    key = (email or "").strip().lower()
    if not key:
        return False
    return (
        db.query(User.email)
        .filter(func.lower(User.email) == key, User.is_platform_support.is_(False))
        .first()
        is not None
    )


def _cached_ooo_covering(db: Session, email: str, days: list[date]):
    """First cached ``ooo_periods`` row covering any of ``days``, else None."""
    from rubik_core.availability.store import get_user_ooo_on

    for day in days:
        row = get_user_ooo_on(db, email, day)
        if row is not None:
            return row
    return None


def _has_any_cached_ooo(db: Session, email: str) -> bool:
    """Whether the OOO cache holds anything at all for this user.

    Distinguishes "the daily refresh has seen them and they're working" from
    "we have no idea" — only the latter justifies a live calendar probe.
    """
    from rubik_core.database.models import OOOPeriod

    return (
        db.query(OOOPeriod.id).filter(OOOPeriod.user_email == email).first() is not None
    )


def _live_ooo_covering(email: str, days: list[date]):
    """Live PTO probe, returning the period covering any of ``days`` or None.

    Best-effort by contract (``detect_pto`` swallows its own failures and
    reports "not on PTO"), so a Google outage leaves the manager looking
    *available* — the conservative direction: a failed probe must never hand
    someone else's meeting away.
    """
    from rubik_core.availability.pto import detect_pto
    from rubik_core.priorities.temporal import resolve_user_now

    try:
        ctx = resolve_user_now(email)
        status = detect_pto(email, today=ctx.today, tz_name=ctx.tz_name)
    except Exception:  # noqa: BLE001 — probe is best-effort, see docstring
        logger.warning("[l10-handoff] live PTO probe failed for %s", email)
        return None
    for period in status.upcoming:
        if any(period.covers(day) for day in days):
            return period
    return None


def manager_availability(
    meeting,
    db: Session,
    *,
    today: date,
    live_probe: bool = True,
) -> ManagerAvailability:
    """Can this meeting's manager currently hand the meeting over?

    Checks *today* and the meeting's next occurrence, so a team whose Thursday
    L10 falls inside their manager's Thu–Fri absence can hand it off on the
    Tuesday rather than waiting for the meeting to be missed first.
    """
    manager: Person | None = None
    if getattr(meeting, "manager_id", None) is not None:
        manager = (
            db.query(Person)
            .filter(Person.id == meeting.manager_id, Person.is_deleted.is_(False))
            .first()
        )

    if manager is None:
        return ManagerAvailability(
            available=False,
            reason=REASON_NO_MANAGER,
            detail="This meeting has no manager, so nobody can currently run it.",
        )

    name = _person_name(manager)
    email = (manager.email or "").strip().lower()

    if not _has_active_account(db, email):
        return ManagerAvailability(
            available=False,
            reason=REASON_OFFBOARDED,
            detail=(
                f"{name} no longer has an account, so this meeting can't be "
                "handed over by its manager."
            ),
            manager_name=name,
            manager_email=email,
        )

    days = [today]
    nxt = next_occurrence(meeting, today)
    if nxt is not None and nxt != today:
        days.append(nxt)

    period = _cached_ooo_covering(db, email, days)
    from_d = end_d = ret_d = None
    if period is not None:
        from_d, end_d, ret_d = period.start_date, period.end_date, period.return_date
    elif live_probe and not _has_any_cached_ooo(db, email):
        live = _live_ooo_covering(email, days)
        if live is not None:
            from_d = live.start.isoformat()
            end_d = live.end.isoformat()
            ret_d = live.return_date.isoformat()

    if end_d is not None:
        back = f" They're back {ret_d}." if ret_d else ""
        return ManagerAvailability(
            available=False,
            reason=REASON_ON_PTO,
            detail=f"{name} is out of office through {end_d}.{back}",
            manager_name=name,
            manager_email=email,
            out_from=from_d,
            out_until=end_d,
            returns_on=ret_d,
        )

    return ManagerAvailability(
        available=True,
        reason=REASON_AVAILABLE,
        detail=(
            f"{name} manages this meeting and is available — ask them to hand "
            "it over, or ask your team's leader."
        ),
        manager_name=name,
        manager_email=email,
    )


def authorize_manager_transfer(
    user: UserProfile,
    meeting,
    db: Session,
    *,
    today: date,
    new_manager_id: UUID | None,
) -> tuple[str, ManagerAvailability | None]:
    """Authorize re-pointing ``meeting``'s manager. Returns (rung, availability).

    Standing authority (see ``_authz.meeting_authority``) may set any manager,
    including clearing it. Everyone else falls to the break-glass rung, which
    requires all of: meeting access, an EOS Person row, the target being the
    caller themselves, and a verifiably unavailable manager.

    Raises 404 when the caller can't see the meeting at all (existence stays
    hidden, matching ``require_meeting_access``) and 403 with the specific
    reason otherwise.
    """
    rung = meeting_authority(user, meeting, db)
    if rung is not None:
        return rung, None

    # Not a 403: someone with no access to a meeting must not learn it exists.
    if not can_access_meeting(user, meeting, db):
        raise HTTPException(status_code=404, detail="Meeting not found")

    # Defence in depth. Unreachable today — a non-exec can't ACCESS the Exec
    # L10, and every Exec-team member already holds the ``exec_team`` rung — so
    # if this ever fires, an access rule changed and the Exec fence is the thing
    # that must not silently widen.
    if bool(getattr(meeting, "is_exec_meeting", False)):
        raise HTTPException(
            status_code=403,
            detail="Only the Exec team can manage the Exec L10.",
        )

    caller = person_by_email(db, user.email)
    if caller is None:
        raise HTTPException(
            status_code=403,
            detail=(
                "You don't have an EOS profile yet, so you can't be made "
                "manager. Ask an admin to add you to the Accountability Chart."
            ),
        )

    if new_manager_id is None or new_manager_id != caller.id:
        raise HTTPException(
            status_code=403,
            detail=(
                "The manager is away, so you can take this meeting over "
                "yourself — but only the current manager or your team's leader "
                "can hand it to someone else."
            ),
        )

    availability = manager_availability(meeting, db, today=today)
    if availability.available:
        raise HTTPException(status_code=403, detail=availability.detail)

    return AUTHORITY_BREAK_GLASS, availability

"""Shared dependencies and authorization helpers for EOS routers.

This module is the single point EOS routers import from for:
- `CurrentUser` — Cubie's authenticated user (Annotated alias for ergonomic Depends).
- `DBDep` — SQLAlchemy session.
- `admin_required` — re-exported gate for admin-only endpoints.
- `require_seat_or_admin` — finer-grained gate: admin OR the person sitting in `seat_id`.

EOS's parallel Google-ID-Token verification (in the standalone EOS app)
is **not** ported. Every EOS endpoint runs through Cubie's
`backend.auth.get_current_user`, full stop.

Role mapping:
- App admins → `UserProfile.roles` contains `"admin"`.
- Everyone else → role `"user"`.
- Impersonation uses Cubie's existing `X-Impersonate-Department` header
  (not `X-Impersonate-Dept`).
"""

from __future__ import annotations

import re
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.auth import admin_required, get_current_user
from rubik_core.auth.registry import UserProfile
from rubik_core.database.db import get_db

# Re-exported so EOS routers have one import surface.
__all__ = [
    "CurrentUser",
    "DBDep",
    "admin_required",
    "get_current_user",
    "require_seat_or_admin",
    "sync_current_quarter",
    "team_scope_filter",
    "canonical_quarters",
]

# A real EOS quarter is named ``Q<1-4> <YYYY>`` (e.g. "Q3 2026") — this is the
# only shape the seed script and the rock find-or-create ever produce. Test
# fixtures that commit against a shared DB leave junk rows named like
# ``FT7 Q-2026-07-10T23:29:37`` / ``FT10 Q-…`` / ``Fb Q-…`` (a framework tag +
# raw ISO timestamp), which then pollute every period dropdown. We defensively
# filter to canonically-named quarters at the read surfaces so that noise never
# renders, regardless of what stray rows exist in the table.
_CANONICAL_QUARTER_NAME = re.compile(r"^Q[1-4] \d{4}$")


def canonical_quarters(quarters):
    """Drop quarter rows whose ``name`` isn't the canonical ``Q<n> YYYY`` form.

    Filters an already-fetched iterable of ``Quarter`` (or dict-like objects
    exposing ``name``) so test-pollution rows never reach a period selector.
    """

    def _name(q):
        return q["name"] if isinstance(q, dict) else q.name

    return [q for q in quarters if _CANONICAL_QUARTER_NAME.match(_name(q) or "")]


def team_scope_filter(model, meeting):
    """SQLAlchemy filter selecting rows belonging to a meeting's TEAM.

    EOS scopes every list (Scorecard, Issues, To-Dos) to a *team*, and the
    L10 is a view over that team's lists. A team is either a department or
    the cross-department Exec/Leadership team:

    - Exec/Leadership L10 (``meeting.is_exec_meeting``) → rows flagged
      ``is_exec`` (``department_name`` is FK-constrained and can't hold a
      sentinel, so the Exec team is marked with the boolean).
    - Department L10 → rows whose ``department_name`` matches the meeting's.

    Works for any model carrying ``is_exec`` + ``department_name``
    (``Issue``, ``Todo``, ``ScorecardMetric``).
    """
    if getattr(meeting, "is_exec_meeting", False):
        return model.is_exec.is_(True)
    if meeting.department_name is None:
        return model.department_name.is_(None) & model.is_exec.is_(False)
    # Case-insensitive: a department's identity is its slug (see docs/ENGINEERING_NOTES.md),
    # so "Sales" and "sales" are the same team. Exec rows never leak into a
    # department meeting regardless of any department tag they carry.
    return (
        func.lower(model.department_name) == meeting.department_name.lower()
    ) & model.is_exec.is_(False)


# Annotated aliases — use as `user: CurrentUser` / `db: DBDep` in handlers.
CurrentUser = Annotated[UserProfile, Depends(get_current_user)]
DBDep = Annotated[Session, Depends(get_db)]


def require_seat_or_admin(
    user: CurrentUser,
    db: DBDep,
    seat_id: UUID,
) -> None:
    """Allow if the caller is admin or is the person currently in `seat_id`.

    Raises 403 otherwise. Used by per-seat mutation endpoints (rocks,
    todos, GWC evaluations) to prevent a user from editing someone
    else's record.

    Imports are local to defer model registration until call time —
    this module is imported at app startup, but `eos_people` may not
    exist in the metadata yet during early bootstrap.
    """
    if "admin" in (user.roles or []):
        return

    from rubik_core.database.eos import Person  # local import

    person = db.query(Person).filter(Person.email == user.email).first()
    if person is None or person.seat_id != seat_id:
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to modify records for this seat.",
        )


def sync_current_quarter(db: Session) -> None:
    """Reconcile the stored ``Quarter.is_current`` flag to the calendar quarter
    that actually contains *today* — per organization.

    ``is_current`` is a persisted flag with **no auto-advance**: the EOS seed
    script (``scripts/seed_eos_minimum.py``) sets it once, so it goes stale the
    moment the calendar rolls into a new quarter (e.g. a ``Q2 2026`` row left
    flagged current in July, when it's really Q3). Every surface that resolves
    "the current quarter" reads this flag — the Rocks default view, the Exec
    Dashboard's default period, the L10 rock review — so one stale flag shows
    *last* quarter everywhere.

    On call we self-heal it: the quarter whose ``[start_date, end_date]`` window
    brackets ``now`` becomes the sole ``is_current=True`` row for its org, and
    any other row still flagged current is cleared. Idempotent — a no-op once
    the right row is flagged, so it's cheap to call on every quarters read.
    Creates nothing: if no quarter row covers today (none seeded for this
    period yet) existing flags are left untouched rather than inventing a row.

    Model import is deferred (see module docstring) to preserve app-startup
    import ordering.
    """
    from datetime import datetime

    from rubik_core.database.eos import Quarter  # local import

    now = datetime.now()
    # The live quarter per org: its window brackets today. A quarter is stored
    # as calendar bounds (e.g. Q3 → 2026-07-01T00:00:00 … 2026-09-30T23:59:59).
    live_by_org = {
        q.organization_id: q
        for q in db.query(Quarter)
        .filter(
            Quarter.is_deleted == False,  # noqa: E712
            Quarter.start_date <= now,
            Quarter.end_date >= now,
        )
        .all()
    }

    changed = False
    # For each org that HAS a live quarter, make it the sole is_current row.
    # Orgs with no quarter covering today are left untouched — we keep the
    # last-known current (a safe fallback for rock-create / L10, which query
    # is_current directly) rather than zeroing it out; it self-corrects once
    # this period's row exists.
    for org_id, live in live_by_org.items():
        if not live.is_current:
            live.is_current = True
            changed = True
        for other in (
            db.query(Quarter)
            .filter(
                Quarter.is_deleted == False,  # noqa: E712
                Quarter.organization_id == org_id,
                Quarter.is_current == True,  # noqa: E712
                Quarter.id != live.id,
            )
            .all()
        ):
            other.is_current = False
            changed = True

    if changed:
        db.commit()

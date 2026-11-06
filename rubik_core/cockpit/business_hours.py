"""The cockpit's business-hours clock — the ONLY place elapsed time is computed.

Every elapsed-time question the Cubie Sales Cockpit asks runs through this
module: ``compute_breach_at``, the ``0.7 * sla_hours`` at-risk threshold, and
both escalation steps. Nothing in ``rubik_core/cockpit/`` may reach for a raw
``timedelta`` to age a commitment. A Friday-afternoon SOW request that
"breaches" on Monday morning because someone counted calendar hours is exactly
the false positive that ends this feature's credibility in week one — and the
AE who gets that alert is right and the system is wrong, which is the worst
possible way to lose an argument.

``Commitment.sla_hours`` is therefore in BUSINESS hours, where one business day
is ``settings.COCKPIT_BUSINESS_DAY_HOURS`` (8): an SLA of "3 business days"
is stored as 24, not 72.

**The working week, exactly as implemented.** Monday–Friday, in
the workspace's control-row time zone, one window per day of
``COCKPIT_BUSINESS_DAY_HOURS`` hours starting
at :data:`BUSINESS_DAY_START_HOUR`. Time outside a window does not count, so an
18:00 request and a 07:00 request the next morning are the same age.

Three things are deliberately NOT honoured. Each is a scope decision, not an
oversight, and each is cheap to add later in exactly one place:

* **Holidays.** There is no holiday calendar anywhere in this repo and
  inventing one is a separate piece of work with its own owner. Christmas Day
  currently counts as a working day, which makes a commitment opened on
  23 December look one day older than it is. :func:`_is_non_working_day` is the
  single named hook: adding a holiday source is a one-function change and
  touches no arithmetic.
* **Per-user working hours.** Cubie does store per-user timezone and working
  hours in Settings, and this module ignores them on purpose. An SLA that means
  something different for each AE is not auditable — the sales manager cannot
  reason about "breached" if breach depends on whose row it is, and the first
  disagreement about a red row becomes an argument about somebody's calendar
  preferences. One org-wide clock, documented, is worth more than a more
  accurate one nobody can defend.
* **PTO.** ``rubik_core/availability/pto.py`` exists and knows who is out.
  Subtracting it would make "breached" depend on a second system's freshness, so
  a stale PTO feed would silently move every clock. If the sales manager asks
  for it, that is a config decision with a named owner, not a bug.

The active workspace's validated IANA zone is required. A missing or malformed
control row refuses the calculation rather than moving an SLA to another day.

**Datetime convention.** Every ``DateTime`` column in this repo is naive and
holds UTC (``datetime.utcnow``), so a naive input is read as UTC and a naive
result comes back. An aware input returns an aware UTC result — awareness is
preserved so a caller never gets a silent naive/aware mismatch out of a
function it handed an aware value to.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import wraps
from inspect import iscoroutinefunction
from typing import Any
from zoneinfo import ZoneInfo

from rubik_core.config import settings
from rubik_core.tenancy.context import current_workspace
from rubik_core.tenancy.workspace_time import (
    WorkspaceTimeUnavailable,
    workspace_time_zone,
)

logger = logging.getLogger(__name__)

#: Local wall-clock hour the business day opens. A module constant and NOT a
#: setting: the moment this is configurable somebody makes it per-user, and the
#: module docstring explains why one org-wide clock is the point. It is only
#: ever the *anchor* of the window — the window's length is
#: ``settings.COCKPIT_BUSINESS_DAY_HOURS``.
BUSINESS_DAY_START_HOUR = 9

# The day-stepping loop in add_business_hours is bounded so a future
# _is_non_working_day that answers True too often (a bad holiday feed, a
# timezone with no valid window) fails loudly instead of spinning a request
# thread forever. 3650 working days is ~14 years — far past any real SLA.
_MAX_DAY_STEPS = 3650

# Float hours never land exactly on a window boundary. A tenth of a second of
# slack keeps "consume the rest of today, then start tomorrow at 09:00" from
# emitting a 09:00:00.000001 result or an extra loop iteration.
_EPSILON_HOURS = 1e-6


# A request or scan can age hundreds of rows. Keep its validated zone in the
# task's own context; direct utility calls still read the control row each time.
# Lazy resolution preserves empty-batch behavior (zero control reads). The
# mutable closed marker is shared by child tasks that inherit this context, so
# they cannot reuse the zone after the parent request or scan exits.
@dataclass
class _BusinessZonePin:
    workspace: str
    zone: ZoneInfo | None = None
    closed: bool = False


_PIN: ContextVar[_BusinessZonePin | None] = ContextVar(
    "cockpit_business_zone_pin", default=None
)


@contextmanager
def pinned_business_time_zone() -> Iterator[None]:
    """Resolve at most one control-row zone for this request or batch.

    The pin is released even on error, so the next request sees an admin edit
    or control outage. A nested switch to another workspace refuses to reuse
    the first workspace's zone.
    """
    pin = _BusinessZonePin(workspace=current_workspace())
    token = _PIN.set(pin)
    try:
        yield
    finally:
        pin.closed = True
        _PIN.reset(token)


def pin_business_time_zone(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Apply one business clock to a synchronous or async batch entry point."""
    if iscoroutinefunction(fn):

        @wraps(fn)
        async def async_wrapped(*args: Any, **kwargs: Any) -> Any:
            with pinned_business_time_zone():
                return await fn(*args, **kwargs)

        return async_wrapped

    @wraps(fn)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        with pinned_business_time_zone():
            return fn(*args, **kwargs)

    return wrapped


def _is_non_working_day(d: date) -> bool:
    """Whether ``d`` carries no business hours at all.

    **This is the holiday hook.** Weekends only, today. To honour holidays,
    add the lookup here and nothing else in the module changes — every other
    function asks this question rather than testing ``weekday()`` itself, and
    that is the whole reason it is a named function for one line of logic.
    """
    return d.weekday() >= 5  # Mon=0 … Sat=5, Sun=6


def _business_day_hours() -> float:
    """``COCKPIT_BUSINESS_DAY_HOURS``, validated.

    Read at call time, not import time, so a test can monkeypatch the setting.
    A non-positive value would make :func:`add_business_hours` unable to
    consume its remainder and the day-step loop would run to its bound, so it
    raises here where the message names the setting.
    """
    hours = float(getattr(settings, "COCKPIT_BUSINESS_DAY_HOURS", 8) or 0)
    if not 0 < hours <= 24:
        raise ValueError(
            f"COCKPIT_BUSINESS_DAY_HOURS must be in (0, 24], got {hours!r}"
        )
    return hours


def _business_tz() -> ZoneInfo:
    pin = _PIN.get()
    if pin is None:
        return workspace_time_zone()
    if pin.closed:
        raise WorkspaceTimeUnavailable("Business clock scope has expired")
    if current_workspace() != pin.workspace:
        raise WorkspaceTimeUnavailable("Business clock belongs to another workspace")
    if pin.zone is None:
        pin.zone = workspace_time_zone()
    return pin.zone


def as_naive_utc(value: datetime) -> datetime:
    """Normalize an instant to the naive-UTC shape the DB columns hold.

    Public because commitment_engine compares ``breach_at`` against ``now`` and
    stores both, and a second copy of this conversion is how a naive/aware
    ``TypeError`` reaches production from one careless caller. A naive input is
    already UTC by repo convention and passes through untouched.
    """
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _to_business_local(value: datetime, tz: ZoneInfo | None = None) -> datetime:
    """An instant as an aware datetime in the business timezone."""
    aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return aware.astimezone(tz or _business_tz())


def _window(d: date, tz: ZoneInfo | None = None) -> tuple[datetime, datetime]:
    """The (open, close) instants of ``d``'s business window, aware and local.

    The start hour slides earlier rather than letting the window run past
    midnight when ``COCKPIT_BUSINESS_DAY_HOURS`` is large: a 24-hour "business
    day" becomes 00:00–24:00 instead of 09:00–24:00 silently losing nine hours
    a day. At the shipped value of 8 this is a no-op and the window is
    09:00–17:00.

    Adding a ``timedelta`` to an *aware* datetime is wall-clock arithmetic, so
    the close instant is 17:00 local on both sides of a DST change. That is the
    intended reading — a business window is wall-clock — and it is also why the
    window is built here rather than by callers.
    """
    day_hours = _business_day_hours()
    tz = tz or _business_tz()
    start_hour = min(BUSINESS_DAY_START_HOUR, 24 - day_hours)
    opens = datetime.combine(d, time(0), tzinfo=tz) + timedelta(hours=start_hour)
    return opens, opens + timedelta(hours=day_hours)


def _next_business_instant(value: datetime, tz: ZoneInfo | None = None) -> datetime:
    """``value`` clamped forward to the next instant inside a business window.

    Already inside one → unchanged. Before today's open → today's open. After
    today's close, or on a non-working day → the next working day's open.
    """
    tz = tz or _business_tz()
    cursor = value
    for _ in range(_MAX_DAY_STEPS):
        if not _is_non_working_day(cursor.date()):
            opens, closes = _window(cursor.date(), tz)
            if cursor < opens:
                return opens
            if cursor < closes:
                return cursor
        cursor = datetime.combine(cursor.date() + timedelta(days=1), time(0), tzinfo=tz)
    raise ValueError(
        "no business day found within "
        f"{_MAX_DAY_STEPS} days of {value.isoformat()} — check _is_non_working_day"
    )


def add_business_hours(start: datetime, hours: float) -> datetime:
    """``start`` advanced by ``hours`` BUSINESS hours.

    This is how ``breach_at`` and both escalation steps are computed. Time
    outside a business window is skipped, so 8 business hours from Friday 15:00
    lands Monday 15:00 (with the shipped 09:00–17:00 window), not Saturday
    morning.

    ``hours`` of 0 returns the next business instant at or after ``start``,
    which is what makes "opened at the weekend" behave: the clock starts Monday
    at open, not Saturday at midnight. A negative value raises — an SLA cannot
    run backwards, and the ``sla_hours > 0`` CHECK means reaching here with one
    is a bug worth surfacing rather than absorbing.
    """
    if hours < 0:
        raise ValueError(f"business hours cannot be negative, got {hours!r}")

    naive_in = start.tzinfo is None
    tz = _business_tz()
    cursor = _next_business_instant(_to_business_local(start, tz), tz)
    remaining = float(hours)

    for _ in range(_MAX_DAY_STEPS):
        _, closes = _window(cursor.date(), tz)
        available = (closes - cursor).total_seconds() / 3600.0
        if remaining <= available + _EPSILON_HOURS:
            result = cursor + timedelta(hours=remaining)
            return as_naive_utc(result) if naive_in else result.astimezone(timezone.utc)
        remaining -= available
        cursor = _next_business_instant(
            datetime.combine(cursor.date() + timedelta(days=1), time(0), tzinfo=tz),
            tz,
        )

    raise ValueError(
        f"adding {hours} business hours to {start.isoformat()} did not converge "
        f"within {_MAX_DAY_STEPS} business days"
    )


def business_hours_between(a: datetime, b: datetime) -> float:
    """Business hours elapsed from ``a`` to ``b``, never negative.

    The at-risk threshold's numerator: ``business_hours_between(opened_at, now)
    >= 0.7 * sla_hours``.

    ``b`` at or before ``a`` returns 0.0 rather than a negative number. A
    commitment back-dated to a future ``opened_at`` (a mis-parsed date, a clock
    skew) is "not started yet", not "-6 hours old" — and a negative elapsed
    would sail through the ``>=`` comparisons above as "fine" while a 0.0 says
    plainly that nothing has run.
    """
    tz = _business_tz()
    a_local = _to_business_local(a, tz)
    b_local = _to_business_local(b, tz)
    if b_local <= a_local:
        return 0.0

    total = 0.0
    day = a_local.date()
    last = b_local.date()
    while day <= last:
        if not _is_non_working_day(day):
            opens, closes = _window(day, tz)
            lo = max(a_local, opens)
            hi = min(b_local, closes)
            if hi > lo:
                total += (hi - lo).total_seconds() / 3600.0
        day += timedelta(days=1)
    return total

"""Detect a user's out-of-office / PTO window from their Google Calendar.

Google Calendar marks time off with ``eventType == "outOfOffice"`` (the native
"Out of office" entry) — and people also just type an all-day event titled
"Vacation" / "PTO" / "OOO". Elsewhere in the app those events are *dropped* so
they don't spawn junk "prepare for your OOO" cards
(:func:`rubik_core.tools.calendar.is_non_meeting_event`). This module does the
opposite: it reads exactly those events and turns them into a structured
:class:`PtoStatus` — "on PTO today, out Mon–Fri, back Monday" — that the
priorities pipeline, the daily briefing, chat, and the teammate ``ooo_periods``
table all consume.

Design notes
------------
* **All-day OOO is the canonical case.** "On PTO all week" is one (or a few)
  all-day ``outOfOffice`` events. A *timed* OOO block (e.g. "OOO 2–3pm") only
  counts toward a PTO day when it covers most of the working day
  (``_FULL_DAY_HOURS``) — a short block is an errand, not time off.
* **Weekends bridge, they don't split.** Out Friday and back the next
  Wednesday reads as one continuous period even though Sat/Sun carry no OOO
  event — the gap is bridged when every day in it is a weekend.
* **Return date skips the weekend.** Out through Friday → back Monday.
* **Pure core, thin shell.** :func:`pto_periods_from_events` is a pure function
  over a list of raw event dicts (unit-testable with no Google call);
  :func:`detect_pto` is the thin fetch-then-delegate wrapper.
"""

from __future__ import annotations

import datetime
import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# A timed OOO block must cover at least this many hours of a calendar day to
# make that day count as PTO. Below this it's an errand/appointment, not a day
# off. All-day OOO events bypass this entirely.
_FULL_DAY_HOURS = 6.0

# How far back / forward :func:`detect_pto` looks. Back a week so an in-progress
# stretch ("out since Monday") reports its true START, not just "today". Forward
# far enough to capture the return date of a multi-week absence.
_DEFAULT_BACK_DAYS = 7
_DEFAULT_FORWARD_DAYS = 45

# Manually-typed all-day titles that mean "I'm off" (no Google eventType). These
# are matched EXACTLY against the normalised title, reusing calendar.py's
# normaliser, so "Team vacation planning" is untouched while "🌴 Vacation" hits.
# Google's native OOO feature sets eventType instead and is handled separately.
_OOO_HOLD_TITLES = frozenset(
    {
        "ooo",
        "out of office",
        "out of the office",
        "pto",
        "on pto",
        "vacation",
        "on vacation",
        "annual leave",
        "leave",
        "on leave",
        "holiday",
        "sick",
        "sick day",
        "out sick",
    }
)


@dataclass(frozen=True)
class PtoPeriod:
    """One continuous stretch of time off, in the user's local calendar days.

    ``start`` and ``end`` are inclusive local dates; ``return_date`` is the
    first working day after ``end`` (weekends skipped) — the day the user is
    back at their desk.
    """

    start: date
    end: date
    return_date: date

    def covers(self, day: date) -> bool:
        return self.start <= day <= self.end

    @property
    def is_single_day(self) -> bool:
        return self.start == self.end


@dataclass(frozen=True)
class PtoStatus:
    """A user's out-of-office picture over the look-around window.

    ``on_pto_today`` is the single fact the priorities pipeline branches on;
    ``current`` is the period covering today (``None`` unless on PTO today);
    ``upcoming`` is every period intersecting the window (including ``current``),
    sorted by start date — this is what the teammate ``ooo_periods`` refresh
    persists.
    """

    user_email: str
    on_pto_today: bool
    current: PtoPeriod | None
    upcoming: tuple[PtoPeriod, ...]

    @property
    def return_date(self) -> date | None:
        return self.current.return_date if self.current else None


# ── event → OOO-day extraction ────────────────────────────────────────────────


def _normalize_title(summary: str | None) -> str:
    """Reduce a title to its lowercase word core for exact hold matching.

    Same normalisation as :func:`rubik_core.tools.calendar._normalize_hold_title`
    ("🌴 Vacation" -> "vacation", "[OOO]" -> "ooo"), inlined here so the PURE
    detection path (:func:`pto_periods_from_events`) has no dependency on the
    calendar module — which drags in the Google client + app config and would
    make this logic un-importable in a lightweight/test context.
    """
    if not summary:
        return ""
    collapsed = re.sub(r"[^a-z0-9]+", " ", summary.casefold())
    return " ".join(collapsed.split())


def _is_ooo_event(event: dict[str, Any]) -> bool:
    """True for a Google native OOO event or a manually-typed time-off hold."""
    if event.get("eventType") == "outOfOffice":
        return True
    return _normalize_title(event.get("summary")) in _OOO_HOLD_TITLES


def _ooo_dates(event: dict[str, Any], tz: ZoneInfo) -> set[date]:
    """The local calendar dates a single OOO event makes "off".

    All-day events contribute every date in ``[start.date, end.date)`` (Google's
    end date is exclusive). Timed events contribute a date only when the block
    covers at least :data:`_FULL_DAY_HOURS` of that local day.
    """
    if not _is_ooo_event(event):
        return set()

    start_info = event.get("start", {}) or {}
    end_info = event.get("end", {}) or {}

    start_date_str = start_info.get("date")
    end_date_str = end_info.get("date")
    if start_date_str and end_date_str and "dateTime" not in start_info:
        # All-day: [start, end) exclusive.
        try:
            sd = date.fromisoformat(start_date_str)
            ed = date.fromisoformat(end_date_str)
        except ValueError:
            return set()
        if ed <= sd:
            ed = sd + timedelta(days=1)
        return {sd + timedelta(days=i) for i in range((ed - sd).days)}

    # Timed OOO — count each local day it covers for >= _FULL_DAY_HOURS.
    start_dt = _parse_dt(start_info.get("dateTime"), tz)
    end_dt = _parse_dt(end_info.get("dateTime"), tz)
    if start_dt is None or end_dt is None or end_dt <= start_dt:
        return set()

    days: set[date] = set()
    day = start_dt.date()
    while day <= end_dt.date():
        day_start = datetime.datetime.combine(day, datetime.time.min, tzinfo=tz)
        day_end = day_start + timedelta(days=1)
        overlap = min(end_dt, day_end) - max(start_dt, day_start)
        if overlap.total_seconds() / 3600.0 >= _FULL_DAY_HOURS:
            days.add(day)
        day += timedelta(days=1)
    return days


def _parse_dt(value: str | None, tz: ZoneInfo) -> datetime.datetime | None:
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


# ── dates → periods ───────────────────────────────────────────────────────────


def _next_working_day(day: date) -> date:
    """The first Mon–Fri strictly after ``day``."""
    nxt = day + timedelta(days=1)
    while nxt.weekday() >= 5:  # 5 = Sat, 6 = Sun
        nxt += timedelta(days=1)
    return nxt


def _only_weekend_between(a: date, b: date) -> bool:
    """True when every date strictly between ``a`` and ``b`` is a weekend day.

    Used to bridge "out Friday, back Wednesday" (Sat/Sun carry no OOO event but
    shouldn't split the period) while keeping a real mid-week working gap as two
    separate periods.
    """
    cur = a + timedelta(days=1)
    while cur < b:
        if cur.weekday() < 5:
            return False
        cur += timedelta(days=1)
    return True


def _dates_to_periods(days: set[date]) -> list[PtoPeriod]:
    """Collapse a set of OOO dates into merged, weekend-bridged periods."""
    if not days:
        return []
    ordered = sorted(days)
    periods: list[PtoPeriod] = []
    run_start = ordered[0]
    prev = ordered[0]
    for d in ordered[1:]:
        # Same run if consecutive, or only weekend days separate them.
        if d == prev + timedelta(days=1) or _only_weekend_between(prev, d):
            prev = d
            continue
        periods.append(PtoPeriod(run_start, prev, _next_working_day(prev)))
        run_start = d
        prev = d
    periods.append(PtoPeriod(run_start, prev, _next_working_day(prev)))
    return periods


def pto_periods_from_events(
    events: list[dict[str, Any]], *, tz_name: str
) -> list[PtoPeriod]:
    """Pure core: raw calendar events → merged PTO periods (testable, no I/O)."""
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 — unknown tz must not break detection
        tz = ZoneInfo("UTC")
    all_days: set[date] = set()
    for ev in events or []:
        all_days |= _ooo_dates(ev, tz)
    return _dates_to_periods(all_days)


def status_from_events(
    events: list[dict[str, Any]],
    *,
    user_email: str,
    today: date,
    tz_name: str,
) -> PtoStatus:
    """Assemble a :class:`PtoStatus` from already-fetched events (pure)."""
    periods = pto_periods_from_events(events, tz_name=tz_name)
    current = next((p for p in periods if p.covers(today)), None)
    return PtoStatus(
        user_email=user_email,
        on_pto_today=current is not None,
        current=current,
        upcoming=tuple(periods),
    )


# ── fetch-then-delegate wrapper ───────────────────────────────────────────────


def detect_pto(
    user_email: str,
    *,
    today: date,
    tz_name: str,
    back_days: int = _DEFAULT_BACK_DAYS,
    forward_days: int = _DEFAULT_FORWARD_DAYS,
) -> PtoStatus:
    """Read ``user_email``'s calendar and compute their PTO status around ``today``.

    Impersonates the user via domain-wide delegation (the same model every other
    calendar read uses). Best-effort: any failure (auth, outage, bad tz) yields a
    "not on PTO" status rather than raising — a detection error must never block a
    priorities run or a briefing.
    """
    empty = PtoStatus(
        user_email=user_email, on_pto_today=False, current=None, upcoming=()
    )
    try:
        from rubik_core.tools.calendar import GoogleCalendarClient

        client = GoogleCalendarClient(user_email=user_email)
        if client.service is None:
            return empty

        time_min = datetime.datetime.combine(
            today - timedelta(days=back_days), datetime.time.min
        )
        time_max = datetime.datetime.combine(
            today + timedelta(days=forward_days), datetime.time.max
        )
        # A wide window with a generous ceiling — we filter to OOO ourselves, so
        # the count only needs to cover a few weeks of a normal calendar.
        events = client.get_upcoming_events(
            max_results=250,
            time_min=time_min,
            time_max=time_max,
            time_zone=tz_name,
        )
        return status_from_events(
            events, user_email=user_email, today=today, tz_name=tz_name
        )
    except Exception:  # noqa: BLE001 — detection is best-effort
        logger.warning("PTO detection failed for %s", user_email, exc_info=True)
        return empty

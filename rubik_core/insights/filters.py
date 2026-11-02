"""Date filters for Insights: the period, the one before it, and local days.

Contract: ``docs/insights/README.md`` §3. Every dashboard reads the same
``range`` / ``start`` / ``end`` query string, so the parsing lives here once.

Why the company's own time zone and not plain UTC: the database stores naive
UTC instants, but a person reads "Tuesday" as their Tuesday. The company sets
its business time zone (the workspace's validated control-row IANA zone), so a
question asked at 8 pm local time on Monday belongs to Monday
even when it is already Tuesday in UTC. Every window is therefore a span of
calendar days in that zone ("local days" below), converted to naive UTC bounds
only at the edge where a query filters a timestamp column.

Why aggregate in Python: the dashboards fetch narrow rows (an email, a
timestamp, a couple of flags) for the window and bucket them here. Date
truncation in a time zone is a different SQL dialect on Postgres and on the
SQLite the tests run against, and at the size of one company (tens to a few
hundred people) the rows are few enough that portability is worth more than
pushing the math into SQL.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import wraps
from typing import TypeVar
from zoneinfo import ZoneInfo

RANGES: tuple[str, ...] = ("7d", "30d", "90d", "qtd", "ytd", "custom")
DEFAULT_RANGE = "30d"
#: The longest period a custom range may span, inclusive.
MAX_DAYS = 366
#: Custom dates must fall in these years. Anything outside is a typo (or a
#: probe), and year 1 or 9999 overflows the time-zone arithmetic below.
MIN_YEAR = 2000
MAX_YEAR = 2100
#: Periods up to this many days are bucketed by day; longer ones by week.
DAILY_MAX_DAYS = 31

_FIXED_DAYS = {"7d": 7, "30d": 30, "90d": 90}
_LABELS = {
    "7d": "Last 7 days",
    "30d": "Last 30 days",
    "90d": "Last 90 days",
    "qtd": "Quarter to date",
    "ytd": "Year to date",
}

_PINNED_ZONE: ContextVar[tuple[str, ZoneInfo] | None] = ContextVar(
    "insights_pinned_business_zone", default=None
)
_Result = TypeVar("_Result")


class FilterError(ValueError):
    """A filter value the API refuses (HTTP 400). ``str(exc)`` is safe to show."""


def _scope_zone() -> ZoneInfo | None:
    pinned = _PINNED_ZONE.get()
    if pinned is None:
        return None
    from rubik_core.tenancy.context import current_workspace
    from rubik_core.tenancy.workspace_time import WorkspaceTimeUnavailable

    if current_workspace() != pinned[0]:
        raise WorkspaceTimeUnavailable(
            "Insights business zone belongs to another workspace"
        )
    return pinned[1]


def tz_name() -> str:
    pinned = _scope_zone()
    if pinned is not None:
        return pinned.key
    from rubik_core.tenancy.workspace_time import workspace_time_zone_name

    return workspace_time_zone_name()


def business_tz() -> ZoneInfo:
    return _scope_zone() or ZoneInfo(tz_name())


@contextmanager
def pinned_business_tz() -> Iterator[ZoneInfo]:
    """Resolve the control row once for one dashboard calculation.

    The pin ends with the call, so the next request sees an admin zone edit or
    a control-DB outage immediately. Direct utility callers still resolve the
    control row themselves and fail closed when it is unavailable.
    """
    from rubik_core.tenancy.context import current_workspace
    from rubik_core.tenancy.workspace_time import workspace_time_zone

    slug = current_workspace()
    zone = workspace_time_zone()
    token = _PINNED_ZONE.set((slug, zone))
    try:
        yield zone
    finally:
        _PINNED_ZONE.reset(token)


def pin_business_tz(fn: Callable[..., _Result]) -> Callable[..., _Result]:
    """Apply one request-scoped zone to an Insights service entry point."""

    @wraps(fn)
    def wrapped(*args, **kwargs):
        with pinned_business_tz():
            return fn(*args, **kwargs)

    return wrapped


def today_local(now: datetime | None = None) -> date:
    """Today's date in the business time zone. ``now`` may be aware, or naive UTC."""
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(business_tz()).date()


def to_local(ts: datetime) -> datetime:
    """A stored naive-UTC instant as an aware business-time-zone datetime."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(business_tz())


def local_day(ts: datetime | date | str | None) -> date | None:
    """The local calendar day (business time zone) a stored value belongs to.

    A ``datetime`` is a naive-UTC instant and is converted. A ``date`` (an
    ``activity_daily.day``) is already a local day. A ``YYYY-MM-DD`` string
    (``home_cards.date``) is read as written. Anything unparseable is None.
    """
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return to_local(ts).date()
    if isinstance(ts, date):
        return ts
    try:
        return date.fromisoformat(str(ts)[:10])
    except ValueError:
        return None


def local_midnight_utc(d: date) -> datetime:
    """Local midnight at the start of ``d``, as a naive-UTC instant."""
    aware = datetime.combine(d, time.min, tzinfo=business_tz())
    return aware.astimezone(timezone.utc).replace(tzinfo=None)


def week_start(d: date) -> date:
    """The Monday of ``d``'s week."""
    return d - timedelta(days=d.weekday())


@dataclass(frozen=True)
class Window:
    """An inclusive span of local calendar days."""

    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def dates(self) -> list[date]:
        return [self.start + timedelta(days=i) for i in range(self.days)]

    def contains(self, d: date | None) -> bool:
        return d is not None and self.start <= d <= self.end

    def utc_bounds(self) -> tuple[datetime, datetime]:
        """``[lo, hi)`` naive-UTC bounds for filtering a timestamp column."""
        return local_midnight_utc(self.start), local_midnight_utc(
            self.end + timedelta(days=1)
        )

    def trailing(self, days: int) -> Window:
        """The ``days`` days ending on this window's last day."""
        return Window(self.end - timedelta(days=days - 1), self.end)


@dataclass(frozen=True)
class Bucket:
    """One point on a chart: its label (``key``) and the days it covers.

    A weekly bucket is labelled with its Monday but covers only the days that
    fall inside the period, so the first and last weeks are not padded with
    days the reader did not ask for.
    """

    key: str
    window: Window


@dataclass(frozen=True)
class Period:
    range_key: str
    current: Window
    previous: Window
    label: str
    bucket: str  # "day" | "week"
    buckets: tuple[Bucket, ...]

    def to_dict(self) -> dict:
        return {
            "start": self.current.start.isoformat(),
            "end": self.current.end.isoformat(),
            "prev_start": self.previous.start.isoformat(),
            "prev_end": self.previous.end.isoformat(),
            "days": self.current.days,
            "bucket": self.bucket,
            "tz": tz_name(),
            "label": self.label,
        }

    def cache_key(self) -> tuple:
        return (self.range_key, self.current.start, self.current.end)


def _parse_date(value: str | None, name: str) -> date:
    if not value:
        raise FilterError(f"Choose a {name} date for a custom range.")
    try:
        parsed = date.fromisoformat(value.strip())
    except ValueError:
        raise FilterError(
            f"The {name} date must look like 2026-10-08 (year-month-day)."
        ) from None
    if not MIN_YEAR <= parsed.year <= MAX_YEAR:
        raise FilterError(f"The {name} date must be between {MIN_YEAR} and {MAX_YEAR}.")
    return parsed


def _custom_label(start: date, end: date) -> str:
    def fmt(d: date, with_year: bool) -> str:
        text = f"{d:%b} {d.day}"
        return f"{text}, {d.year}" if with_year else text

    if start.year == end.year:
        return f"{fmt(start, False)} – {fmt(end, True)}"
    return f"{fmt(start, True)} – {fmt(end, True)}"


def make_buckets(window: Window) -> tuple[str, tuple[Bucket, ...]]:
    if window.days <= DAILY_MAX_DAYS:
        return "day", tuple(Bucket(d.isoformat(), Window(d, d)) for d in window.dates())
    buckets: list[Bucket] = []
    monday = week_start(window.start)
    while monday <= window.end:
        lo = max(monday, window.start)
        hi = min(monday + timedelta(days=6), window.end)
        buckets.append(Bucket(monday.isoformat(), Window(lo, hi)))
        monday += timedelta(days=7)
    return "week", tuple(buckets)


def parse_period(
    range_key: str | None = None,
    start: str | None = None,
    end: str | None = None,
    *,
    today: date | None = None,
) -> Period:
    """The period a request asks for, per contract §3. Raises FilterError."""
    key = (range_key or DEFAULT_RANGE).strip().lower()
    if key not in RANGES:
        raise FilterError("Unknown date range. Use one of: " + ", ".join(RANGES) + ".")
    today = today or today_local()
    if key in _FIXED_DAYS:
        cur = Window(today - timedelta(days=_FIXED_DAYS[key] - 1), today)
        label = _LABELS[key]
    elif key == "qtd":
        first_month = 3 * ((today.month - 1) // 3) + 1
        cur = Window(date(today.year, first_month, 1), today)
        label = _LABELS[key]
    elif key == "ytd":
        cur = Window(date(today.year, 1, 1), today)
        label = _LABELS[key]
    else:
        s = _parse_date(start, "start")
        e = _parse_date(end, "end")
        if e < s:
            raise FilterError("The end date is before the start date.")
        cur = Window(s, e)
        if cur.days > MAX_DAYS:
            raise FilterError(f"A custom range can cover at most {MAX_DAYS} days.")
        label = _custom_label(s, e)
    prev_end = cur.start - timedelta(days=1)
    prev = Window(prev_end - timedelta(days=cur.days - 1), prev_end)
    bucket, buckets = make_buckets(cur)
    return Period(
        range_key=key,
        current=cur,
        previous=prev,
        label=label,
        bucket=bucket,
        buckets=buckets,
    )

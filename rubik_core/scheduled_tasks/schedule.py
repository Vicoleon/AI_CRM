"""When does a scheduled task fire next?

Pure functions over the schedule fields — no DB, no clock of their own (every
entry point takes ``now_utc``), so the DST and month-end cases below are
directly testable.

Two rules the rest of the feature depends on:

1. **Wall clock in, instants out.** A user says "8am"; they mean 8am where they
   are, on whichever side of a DST boundary the day falls. So the local time is
   combined with a local date and only THEN converted to UTC, which is what the
   database stores and what the dispatcher compares against.

2. **Compare instants, never wall clocks.** Two aware datetimes in the same
   ``ZoneInfo`` compare by wall clock, so across the autumn fold ``01:30``
   (before) reads as "not before" ``01:30`` (after) even though an hour
   separates them. Every comparison here happens after ``.astimezone(UTC)``.

Stdlib only (``zoneinfo``): no cron parser is added for this. The schedule
vocabulary is deliberately small enough to describe in a sentence, which is
also what lets the chat tool read one back to the user for confirmation.
"""

from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

SCHEDULE_KINDS = ("once", "daily", "weekdays", "weekly", "monthly")
# Monday=0, matching date.weekday().
WEEKDAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
_WEEKDAY_MASK = (0, 1, 2, 3, 4)  # Mon-Fri

# How far ahead to look for the next matching local date. A monthly task needs
# at most ~62 days (Jan 31 -> Feb 28); the margin covers nothing exotic.
_MAX_LOOKAHEAD_DAYS = 400


class ScheduleError(ValueError):
    """The schedule as described cannot be honoured.

    The message is user-facing prose: the router returns it as a 400 detail and
    the chat tool hands it straight to the model to relay.
    """


@dataclass(frozen=True)
class Schedule:
    """A validated schedule, ready to store or to compute occurrences from."""

    kind: str
    timezone: str
    time_local: str | None = None  # "HH:MM"
    days_of_week: tuple[int, ...] = ()
    day_of_month: int | None = None
    run_at: datetime | None = None  # naive UTC, `once` only


def resolve_zone(tz_name: str | None) -> tuple[ZoneInfo, str]:
    """``(ZoneInfo, canonical name)``, falling back LOUDLY to UTC.

    Mirrors ``rubik_core.priorities.temporal.resolve_tz``: a task whose stored
    timezone stopped resolving must still run (at a wrong-but-logged hour)
    rather than silently never firing again.
    """
    name = (tz_name or "").strip() or "UTC"
    try:
        return ZoneInfo(name), name
    except Exception:  # noqa: BLE001 — an unknown zone must not strand a task
        logger.warning("scheduled_tasks: unknown timezone %r — using UTC", name)
        return ZoneInfo("UTC"), "UTC"


def parse_time_local(value: str | None) -> time:
    """Parse "HH:MM" (24-hour). Raises :class:`ScheduleError` on anything else."""
    raw = (value or "").strip()
    if not raw:
        raise ScheduleError("I need a time of day, like 08:00.")
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return time(hour=parsed.hour, minute=parsed.minute)
    raise ScheduleError(
        f"{value!r} is not a time I can read — use 24-hour HH:MM, like 08:00 or 17:30."
    )


def _normalize_days(days: object) -> tuple[int, ...]:
    """Coerce a days-of-week list to a sorted tuple of ints (Monday=0)."""
    if days is None:
        return ()
    if isinstance(days, int):
        days = [days]
    if not isinstance(days, (list, tuple, set)):
        raise ScheduleError(
            "Days of the week must be a list, like Monday and Thursday."
        )
    out: set[int] = set()
    for raw in days:
        try:
            day = int(raw)
        except (TypeError, ValueError):
            raise ScheduleError(f"{raw!r} is not a day of the week.") from None
        if not 0 <= day <= 6:
            raise ScheduleError(
                f"Days of the week run 0 (Monday) to 6 (Sunday); {day} is outside that."
            )
        out.add(day)
    return tuple(sorted(out))


def validate_schedule(
    *,
    schedule_kind: str,
    timezone_name: str,
    time_local: str | None = None,
    days_of_week: object = None,
    day_of_month: int | None = None,
    run_at_local: str | None = None,
    now_utc: datetime | None = None,
) -> Schedule:
    """Validate a proposed schedule, or raise :class:`ScheduleError`.

    ``run_at_local`` (for ``once``) is an ISO local datetime, e.g.
    "2026-09-09T08:00". It is converted to a naive UTC instant here so the
    stored row needs no further interpretation.

    The timezone is validated STRICTLY at this point (unlike at run time, where
    an unresolvable zone degrades to UTC): a typo caught now is a question the
    user can answer, and one caught later is a task that fires at the wrong hour.
    """
    kind = (schedule_kind or "").strip().lower()
    if kind not in SCHEDULE_KINDS:
        raise ScheduleError(
            f"{schedule_kind!r} is not a schedule I support — use one of: "
            + ", ".join(SCHEDULE_KINDS)
            + "."
        )

    tz_raw = (timezone_name or "").strip()
    if not tz_raw:
        raise ScheduleError("I need to know which timezone the time is in.")
    try:
        tz = ZoneInfo(tz_raw)
    except Exception:  # noqa: BLE001
        raise ScheduleError(
            f"{tz_raw!r} is not a timezone I recognise — use an IANA name like "
            "America/Chicago."
        ) from None

    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    if kind == "once":
        if not (run_at_local or "").strip():
            raise ScheduleError(
                "I need the date and time to run this, like 2026-09-09 08:00."
            )
        local = _parse_local_datetime(run_at_local, tz)
        if local.astimezone(timezone.utc) <= now:
            raise ScheduleError(
                "That moment has already passed — give me a date and time in the future."
            )
        return Schedule(
            kind=kind,
            timezone=tz_raw,
            run_at=local.astimezone(timezone.utc).replace(tzinfo=None),
        )

    parsed_time = parse_time_local(time_local)
    days = _normalize_days(days_of_week)
    dom = day_of_month

    if kind == "weekly":
        if not days:
            raise ScheduleError(
                "Which days of the week should this run? Tell me at least one."
            )
    else:
        days = ()

    if kind == "monthly":
        if dom is None:
            raise ScheduleError("Which day of the month should this run on?")
        try:
            dom = int(dom)
        except (TypeError, ValueError):
            raise ScheduleError(
                f"{day_of_month!r} is not a day of the month."
            ) from None
        if not 1 <= dom <= 31:
            raise ScheduleError("The day of the month must be between 1 and 31.")
    else:
        dom = None

    return Schedule(
        kind=kind,
        timezone=tz_raw,
        time_local=f"{parsed_time.hour:02d}:{parsed_time.minute:02d}",
        days_of_week=days,
        day_of_month=dom,
    )


def _parse_local_datetime(value: str, tz: ZoneInfo) -> datetime:
    """Parse an ISO local datetime and attach ``tz``."""
    raw = (value or "").strip().replace(" ", "T")
    # Tolerate a trailing "Z" or offset by stripping it: the caller said LOCAL.
    if raw.endswith("Z"):
        raw = raw[:-1]
    try:
        naive = datetime.fromisoformat(raw)
    except ValueError:
        raise ScheduleError(
            f"{value!r} is not a date and time I can read — use "
            "YYYY-MM-DDTHH:MM, like 2026-09-09T08:00."
        ) from None
    if naive.tzinfo is not None:
        naive = naive.replace(tzinfo=None)
    return naive.replace(tzinfo=tz)


def schedule_from_task(task) -> Schedule:
    """Rebuild a :class:`Schedule` from a stored ``ScheduledTask`` row.

    Tolerant on purpose — this reads rows that may predate a later validation
    rule, and a run must not be lost to a strictness change.
    """
    return Schedule(
        kind=(task.schedule_kind or "").strip().lower(),
        timezone=task.timezone or "UTC",
        time_local=task.time_local,
        days_of_week=tuple(task.days_of_week or ()),
        day_of_month=task.day_of_month,
        run_at=task.run_at,
    )


def next_run_utc(schedule: Schedule, after_utc: datetime) -> datetime | None:
    """The first occurrence strictly after ``after_utc``, as naive UTC.

    ``None`` means "never again" — a ``once`` schedule whose moment has passed.

    DST behaviour, both inherited from ``zoneinfo`` and both the reading a user
    expects: a time that does not exist on a spring-forward day (02:30 where
    the clocks jump 02:00 -> 03:00) resolves at the pre-transition offset, so
    the task still runs that day; a time that happens twice on a fall-back day
    runs at the FIRST of the two.
    """
    tz, _ = resolve_zone(schedule.timezone)
    after = _as_aware_utc(after_utc)

    if schedule.kind == "once":
        if schedule.run_at is None:
            return None
        moment = _as_aware_utc(schedule.run_at)
        return schedule.run_at if moment > after else None

    local_time = parse_time_local(schedule.time_local)
    start_local_date = after.astimezone(tz).date()

    for offset in range(_MAX_LOOKAHEAD_DAYS):
        candidate_date = start_local_date + timedelta(days=offset)
        if not _date_matches(schedule, candidate_date):
            continue
        candidate = datetime.combine(candidate_date, local_time, tzinfo=tz)
        as_utc = candidate.astimezone(timezone.utc)
        if as_utc > after:
            return as_utc.replace(tzinfo=None)

    logger.warning(
        "scheduled_tasks: no occurrence within %d days for kind=%s days=%s dom=%s",
        _MAX_LOOKAHEAD_DAYS,
        schedule.kind,
        schedule.days_of_week,
        schedule.day_of_month,
    )
    return None


def _as_aware_utc(value: datetime) -> datetime:
    """Treat a naive datetime as UTC (the storage convention here)."""
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def _date_matches(schedule: Schedule, day: date) -> bool:
    kind = schedule.kind
    if kind == "daily":
        return True
    if kind == "weekdays":
        return day.weekday() in _WEEKDAY_MASK
    if kind == "weekly":
        return day.weekday() in set(schedule.days_of_week)
    if kind == "monthly":
        target = schedule.day_of_month or 1
        last = calendar.monthrange(day.year, day.month)[1]
        # "The 31st" in a 30-day month means the last day, not "skip this month".
        return day.day == min(target, last)
    return False


def describe_schedule(schedule: Schedule) -> str:
    """Plain-English schedule, e.g. "every weekday at 8:00 AM (America/Chicago)".

    The chat tool reads this back so the user can correct a misheard time
    before the task ever fires.
    """
    tz_label = schedule.timezone or "UTC"

    if schedule.kind == "once":
        if schedule.run_at is None:
            return "once (no time set)"
        tz, _ = resolve_zone(tz_label)
        local = _as_aware_utc(schedule.run_at).astimezone(tz)
        return f"once on {_format_date(local)} at {_format_time(local.time())} ({tz_label})"

    at = f"at {_format_time(parse_time_local(schedule.time_local))} ({tz_label})"
    if schedule.kind == "daily":
        return f"every day {at}"
    if schedule.kind == "weekdays":
        return f"every weekday {at}"
    if schedule.kind == "weekly":
        names = [WEEKDAY_NAMES[d] for d in schedule.days_of_week]
        return f"every {_join_names(names)} {at}"
    if schedule.kind == "monthly":
        return f"on the {_ordinal(schedule.day_of_month or 1)} of each month {at}"
    return f"{schedule.kind} {at}"


def format_local(moment: datetime | None, tz_name: str | None) -> str:
    """A UTC instant rendered in ``tz_name``, e.g. "Tue Sep 9, 8:00 AM"."""
    if moment is None:
        return ""
    tz, _ = resolve_zone(tz_name)
    local = _as_aware_utc(moment).astimezone(tz)
    return f"{local.strftime('%a %b')} {local.day}, {_format_time(local.time())}"


def _format_time(value: time) -> str:
    hour = value.hour % 12 or 12
    suffix = "AM" if value.hour < 12 else "PM"
    return f"{hour}:{value.minute:02d} {suffix}"


def _format_date(value: datetime) -> str:
    return f"{value.strftime('%A, %B')} {value.day}, {value.year}"


def _join_names(names: list[str]) -> str:
    if not names:
        return "day"
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + f" and {names[-1]}"


def _ordinal(number: int) -> str:
    if 10 <= number % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"

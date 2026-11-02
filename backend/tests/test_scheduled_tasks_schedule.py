"""Scheduled tasks — when does the next run land?

The DST cases are the reason this module is pure and takes an injectable
``now``: they are impossible to exercise reliably against a real clock, and
getting them wrong means a task that silently fires an hour early for half the
year (or, on a spring-forward day, not at all).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from rubik_core.scheduled_tasks import schedule as sched

CHICAGO = "America/Chicago"


def _utc(value: str) -> datetime:
    """Naive UTC, matching what the DB stores."""
    return datetime.fromisoformat(value)


def _aware(value: str) -> datetime:
    return _utc(value).replace(tzinfo=timezone.utc)


def _daily(time_local: str, tz: str = CHICAGO):
    return sched.validate_schedule(
        schedule_kind="daily",
        timezone_name=tz,
        time_local=time_local,
        now_utc=_aware("2026-09-08T12:00"),
    )


# ── the ordinary cases ──────────────────────────────────────────────────────


def test_weekdays_skips_the_weekend():
    spec = sched.validate_schedule(
        schedule_kind="weekdays",
        timezone_name=CHICAGO,
        time_local="08:00",
        now_utc=_aware("2026-09-08T12:00"),
    )
    # Friday 2026-09-11, just after that day's 08:00 local (13:00Z).
    assert sched.next_run_utc(spec, _utc("2026-09-11T13:05")) == _utc(
        "2026-09-14T13:00"
    )


def test_a_late_tick_does_not_fire_twice_the_same_day():
    """The tick runs every few minutes, so it routinely arrives late.

    The next occurrence must be tomorrow, not the one that just passed —
    otherwise a task fires again on every tick until the clock rolls over.
    """
    spec = _daily("08:00")
    assert sched.next_run_utc(spec, _utc("2026-09-09T13:04")) == _utc(
        "2026-09-10T13:00"
    )


def test_weekly_honours_the_chosen_days():
    spec = sched.validate_schedule(
        schedule_kind="weekly",
        timezone_name=CHICAGO,
        time_local="17:30",
        days_of_week=[3, 0],  # Thursday, Monday — order must not matter
        now_utc=_aware("2026-09-08T12:00"),
    )
    assert spec.days_of_week == (0, 3)
    # Tuesday → the coming Thursday.
    assert sched.next_run_utc(spec, _utc("2026-09-08T12:00")) == _utc(
        "2026-09-10T22:30"
    )


@pytest.mark.parametrize(
    ("after", "expected"),
    [
        ("2027-01-15T00:00", "2027-01-31T09:00"),
        # February has no 31st: the last day is what "the 31st" means.
        ("2027-02-01T00:00", "2027-02-28T09:00"),
        ("2027-04-01T00:00", "2027-04-30T09:00"),
    ],
)
def test_monthly_clamps_to_the_end_of_a_short_month(after, expected):
    spec = sched.validate_schedule(
        schedule_kind="monthly",
        timezone_name="UTC",
        time_local="09:00",
        day_of_month=31,
        now_utc=_aware("2027-01-01T00:00"),
    )
    assert sched.next_run_utc(spec, _utc(after)) == _utc(expected)


def test_once_fires_exactly_once():
    spec = sched.validate_schedule(
        schedule_kind="once",
        timezone_name=CHICAGO,
        run_at_local="2026-09-09T08:00",
        now_utc=_aware("2026-09-08T12:00"),
    )
    assert spec.run_at == _utc("2026-09-09T13:00")
    assert sched.next_run_utc(spec, _utc("2026-09-08T12:00")) == _utc(
        "2026-09-09T13:00"
    )
    # Strictly after: the moment it fires, there is no next one.
    assert sched.next_run_utc(spec, _utc("2026-09-09T13:00")) is None


# ── daylight saving ─────────────────────────────────────────────────────────


def test_a_time_that_does_not_exist_still_runs_that_day():
    """2027-03-14 in Chicago jumps 02:00 → 03:00, so 02:30 never happens.

    The task must still run that morning (at the pre-transition offset) rather
    than skipping the day.
    """
    spec = _daily("02:30")
    assert sched.next_run_utc(spec, _utc("2027-03-13T20:00")) == _utc(
        "2027-03-14T08:30"
    )


def test_a_time_that_happens_twice_runs_at_the_first_one():
    """2027-11-07 in Chicago repeats 01:00-02:00, so 01:30 happens twice."""
    spec = _daily("01:30")
    assert sched.next_run_utc(spec, _utc("2027-11-06T20:00")) == _utc(
        "2027-11-07T06:30"
    )


def test_the_local_hour_holds_across_a_dst_boundary():
    """8am stays 8am — the UTC instant is what moves.

    This is the whole reason the schedule stores a wall clock and a zone rather
    than a UTC time: a task set in November must not arrive at 7am in March.
    """
    spec = _daily("08:00")
    winter = sched.next_run_utc(spec, _utc("2027-01-10T00:00"))
    summer = sched.next_run_utc(spec, _utc("2027-07-10T00:00"))
    assert winter.hour == 14  # CST, UTC-6
    assert summer.hour == 13  # CDT, UTC-5


# ── validation ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"schedule_kind": "weekly", "time_local": "08:00"}, "at least one"),
        (
            {
                "schedule_kind": "daily",
                "time_local": "08:00",
                "timezone_name": "Mars/Olympus",
            },
            "not a timezone",
        ),
        ({"schedule_kind": "daily", "time_local": "8am"}, "not a time"),
        ({"schedule_kind": "hourly", "time_local": "08:00"}, "not a schedule"),
        (
            {"schedule_kind": "monthly", "time_local": "08:00", "day_of_month": 44},
            "between 1 and 31",
        ),
        ({"schedule_kind": "monthly", "time_local": "08:00"}, "day of the month"),
        (
            {"schedule_kind": "once", "run_at_local": "2020-01-01T08:00"},
            "already passed",
        ),
        ({"schedule_kind": "once"}, "date and time"),
        (
            {"schedule_kind": "weekly", "time_local": "08:00", "days_of_week": [9]},
            "0 (Monday)",
        ),
    ],
)
def test_an_unusable_schedule_is_refused_in_words_the_user_can_act_on(kwargs, expected):
    kwargs.setdefault("timezone_name", CHICAGO)
    with pytest.raises(sched.ScheduleError) as exc:
        sched.validate_schedule(now_utc=_aware("2026-09-08T12:00"), **kwargs)
    assert expected in str(exc.value)


def test_an_unknown_timezone_at_run_time_degrades_to_utc_rather_than_stranding():
    """Validation is strict, but a STORED zone that stopped resolving must not
    mean a task never fires again."""
    zone, name = sched.resolve_zone("Mars/Olympus")
    assert name == "UTC"
    assert zone is not None


# ── the text the user reads ─────────────────────────────────────────────────


def test_the_schedule_reads_back_in_plain_english():
    """The chat tool repeats this so a misheard hour is caught before it fires."""
    assert (
        sched.describe_schedule(_daily("08:00"))
        == "every day at 8:00 AM (America/Chicago)"
    )
    weekdays = sched.validate_schedule(
        schedule_kind="weekdays",
        timezone_name=CHICAGO,
        time_local="08:00",
        now_utc=_aware("2026-09-08T12:00"),
    )
    assert sched.describe_schedule(weekdays).startswith("every weekday at 8:00 AM")
    weekly = sched.validate_schedule(
        schedule_kind="weekly",
        timezone_name=CHICAGO,
        time_local="17:30",
        days_of_week=[0, 3],
        now_utc=_aware("2026-09-08T12:00"),
    )
    assert "every Monday and Thursday at 5:30 PM" in sched.describe_schedule(weekly)
    monthly = sched.validate_schedule(
        schedule_kind="monthly",
        timezone_name="UTC",
        time_local="09:00",
        day_of_month=1,
        now_utc=_aware("2026-09-08T12:00"),
    )
    assert "on the 1st of each month" in sched.describe_schedule(monthly)


def test_the_next_run_is_rendered_in_the_users_own_timezone():
    assert (
        sched.format_local(_utc("2026-09-14T13:00"), CHICAGO) == "Mon Sep 14, 8:00 AM"
    )

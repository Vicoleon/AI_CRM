"""An L10's weekly slot is read on the org's clock, not UTC.

``L10Meeting.start_time`` is the "HH:MM" a person sees on their calendar:
calendar import keeps the event's local time and drops the offset, and the
schedule form stores what the manager typed. ``_next_scheduled_dt`` used to
read it as UTC, so an 11:00 Central meeting came back as 6:00 AM in "your
next L10" and in voice "open my L10". What this file pins:

1. The slot is read on the org timezone (America/Chicago by default) and
   returned as naive UTC.
2. Each occurrence gets its own UTC offset across a DST change.
3. The weekday is the org's weekday, so an evening meeting that is already
   tomorrow in UTC still lands on its own day.
4. A slot that has started rolls to next week; bad input yields None.

The org timezone is pinned by STRING target, so a reloaded config or a
``PRIORITIES_DEFAULT_TZ`` in the environment can't move the answers.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest

ORG_TZ = "rubik_core.priorities.temporal._org_tz_name"

# Naive UTC, the way the EOS tables and the callers carry "now".
TUE_NOON_CDT = datetime(2026, 10, 6, 17, 0)  # Tuesday 12:00 Central (UTC-5)


@pytest.fixture(autouse=True)
def central_org_clock():
    with patch(ORG_TZ, return_value="America/Chicago"):
        yield


def _next(day_of_week, start_time, now):
    from backend.routers.eos.me import _next_scheduled_dt

    return _next_scheduled_dt(day_of_week, start_time, now=now)


def test_start_time_is_central_wall_clock_returned_as_naive_utc():
    found = _next(2, "11:00", TUE_NOON_CDT)  # Wednesday 11:00 Central
    assert found == datetime(2026, 10, 7, 16, 0)
    assert found.tzinfo is None


def test_each_occurrence_takes_its_own_dst_offset():
    # Daylight time ends Sunday 2026-11-01. A Monday 9:00 meeting is 14:00 UTC
    # the week before and 15:00 UTC the week after.
    assert _next(0, "09:00", datetime(2026, 10, 24, 15, 0)) == datetime(
        2026, 10, 26, 14, 0
    )
    assert _next(0, "09:00", datetime(2026, 10, 31, 15, 0)) == datetime(
        2026, 11, 2, 15, 0
    )


def test_an_evening_meeting_keeps_its_central_day():
    # Thursday 19:30 Central is 00:30 Friday in UTC. At 18:00 Thursday Central
    # (23:00 UTC) it is still tonight's meeting, not next week's.
    assert _next(3, "19:30", datetime(2026, 10, 8, 23, 0)) == datetime(
        2026, 10, 9, 0, 30
    )


def test_a_slot_that_has_started_rolls_to_next_week():
    wed_11_cdt = datetime(2026, 10, 7, 16, 0)
    assert _next(2, "11:00", wed_11_cdt) == datetime(2026, 10, 14, 16, 0)
    assert _next(2, "11:00", datetime(2026, 10, 7, 17, 0)) == datetime(
        2026, 10, 14, 16, 0
    )


def test_the_org_timezone_setting_decides_the_clock():
    with patch(ORG_TZ, return_value="America/New_York"):
        found = _next(2, "11:00", TUE_NOON_CDT)
    assert found == datetime(2026, 10, 7, 15, 0)


@pytest.mark.parametrize(
    ("day_of_week", "start_time"),
    [(None, "11:00"), (2, None), (2, "eleven"), (2, "25:00"), (7, "11:00")],
)
def test_an_unusable_slot_has_no_next_occurrence(day_of_week, start_time):
    assert _next(day_of_week, start_time, TUE_NOON_CDT) is None

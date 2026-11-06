"""Insights date filters (README §3): ranges, the previous period, Chicago days.

Pure functions, no database. "Today" is passed in, never read from the clock.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from rubik_core.insights.filters import (
    FilterError,
    Window,
    local_day,
    local_midnight_utc,
    make_buckets,
    parse_period,
    week_start,
)

TODAY = date(2026, 10, 8)  # a Thursday


def test_the_default_is_the_last_30_days_ending_today():
    p = parse_period(today=TODAY)
    assert p.range_key == "30d"
    assert (p.current.start, p.current.end) == (date(2026, 9, 9), TODAY)
    assert p.current.days == 30
    assert p.label == "Last 30 days"
    assert p.to_dict() == {
        "start": "2026-09-09",
        "end": "2026-10-08",
        "prev_start": "2026-08-10",
        "prev_end": "2026-09-08",
        "days": 30,
        "bucket": "day",
        "tz": "America/Chicago",
        "label": "Last 30 days",
    }


@pytest.mark.parametrize(
    ("key", "start", "label"),
    [
        ("7d", date(2026, 10, 2), "Last 7 days"),
        ("90d", date(2026, 7, 11), "Last 90 days"),
        ("qtd", date(2026, 10, 1), "Quarter to date"),
        ("ytd", date(2026, 1, 1), "Year to date"),
        ("YTD", date(2026, 1, 1), "Year to date"),
    ],
)
def test_presets(key, start, label):
    p = parse_period(key, today=TODAY)
    assert p.current == Window(start, TODAY)
    assert p.label == label


def test_the_previous_period_is_the_same_length_immediately_before():
    for key in ("7d", "30d", "90d", "qtd", "ytd"):
        p = parse_period(key, today=TODAY)
        assert p.previous.days == p.current.days
        assert (p.current.start - p.previous.end).days == 1


def test_a_custom_range_is_inclusive_and_labelled_by_its_dates():
    p = parse_period("custom", "2026-09-01", "2026-09-30", today=TODAY)
    assert p.current == Window(date(2026, 9, 1), date(2026, 9, 30))
    assert p.current.days == 30
    assert p.label == "Sep 1 – Sep 30, 2026"
    assert p.previous == Window(date(2026, 8, 2), date(2026, 8, 31))
    across = parse_period("custom", "2025-12-15", "2026-01-15", today=TODAY)
    assert across.label == "Dec 15, 2025 – Jan 15, 2026"


@pytest.mark.parametrize(
    ("args", "fragment"),
    [
        (("forever",), "Unknown date range"),
        (("custom",), "start date"),
        (("custom", "2026-09-01"), "end date"),
        (("custom", "09/01/2026", "2026-09-30"), "year-month-day"),
        (("custom", "2026-09-30", "2026-09-01"), "before the start"),
        (("custom", "2025-10-01", "2026-10-02"), "at most 366 days"),
        (("custom", "0001-01-01", "0001-01-10"), "between 2000 and 2100"),
        (("custom", "9999-12-01", "9999-12-31"), "between 2000 and 2100"),
        (("custom", "2026-01-01", "2101-01-01"), "between 2000 and 2100"),
    ],
)
def test_bad_ranges_raise_a_readable_error(args, fragment):
    with pytest.raises(FilterError, match=fragment):
        parse_period(*args, today=TODAY)


def test_a_full_366_day_range_is_allowed():
    p = parse_period("custom", "2025-10-08", "2026-10-08", today=TODAY)
    assert p.current.days == 366


def test_periods_up_to_31_days_are_daily_and_longer_ones_weekly_from_monday():
    bucket, buckets = make_buckets(Window(date(2026, 9, 8), TODAY))
    assert bucket == "day" and len(buckets) == 31
    bucket, buckets = make_buckets(Window(date(2026, 9, 7), TODAY))
    assert bucket == "week"
    assert [b.key for b in buckets] == [
        "2026-09-07",
        "2026-09-14",
        "2026-09-21",
        "2026-09-28",
        "2026-10-05",
    ]
    # The last week is cut at the period's end, not padded past it.
    assert buckets[-1].window == Window(date(2026, 10, 5), TODAY)
    # A week that starts before the period is labelled by its Monday but
    # covers only the period's days.
    _b, mid = make_buckets(Window(date(2026, 7, 11), TODAY))  # a Saturday
    assert mid[0].key == "2026-07-06"
    assert mid[0].window == Window(date(2026, 7, 11), date(2026, 7, 12))
    assert sum(b.window.days for b in mid) == 90


def test_week_start_is_monday():
    assert week_start(TODAY) == date(2026, 10, 5)
    assert week_start(date(2026, 10, 5)) == date(2026, 10, 5)
    assert week_start(date(2026, 10, 11)) == date(2026, 10, 5)


def test_days_are_local_days_not_utc_days():
    # 02:30 UTC on Oct 9 is 21:30 on Oct 8 in Chicago (CDT, UTC-5).
    assert local_day(datetime(2026, 10, 9, 2, 30)) == date(2026, 10, 8)
    assert local_day(datetime(2026, 10, 9, 5, 0)) == date(2026, 10, 9)
    # In winter (CST, UTC-6) the boundary moves an hour.
    assert local_day(datetime(2026, 12, 2, 5, 30)) == date(2026, 12, 1)
    # A date is already a Chicago day; a card's "YYYY-MM-DD" string reads as written.
    assert local_day(date(2026, 10, 8)) == date(2026, 10, 8)
    assert local_day("2026-10-08") == date(2026, 10, 8)
    assert local_day("not a date") is None
    assert local_day(None) is None


def test_window_bounds_are_naive_utc_chicago_midnights():
    lo, hi = Window(date(2026, 10, 8), date(2026, 10, 8)).utc_bounds()
    assert lo == datetime(2026, 10, 8, 5, 0) and lo.tzinfo is None
    assert hi == datetime(2026, 10, 9, 5, 0)
    assert local_midnight_utc(date(2026, 12, 1)) == datetime(2026, 12, 1, 6, 0)
    # Across the November DST change the window is 25 hours long.
    lo, hi = Window(date(2026, 11, 1), date(2026, 11, 1)).utc_bounds()
    assert (hi - lo).total_seconds() == 25 * 3600


def test_trailing_windows_end_on_the_window_end():
    w = Window(date(2026, 10, 1), TODAY)
    assert w.trailing(30) == Window(date(2026, 9, 9), TODAY)
    assert (
        w.contains(TODAY) and not w.contains(date(2026, 9, 30)) and not w.contains(None)
    )

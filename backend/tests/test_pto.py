"""Unit tests for PTO / out-of-office detection (rubik_core.availability.pto).

These exercise the PURE core (:func:`pto_periods_from_events` /
:func:`status_from_events`) with synthetic calendar events — no Google calls,
no app config — which is exactly why the detection logic was decoupled from
``rubik_core.tools.calendar``.
"""

from datetime import date

from rubik_core.availability.pto import (
    PtoStatus,
    pto_periods_from_events,
    status_from_events,
)

TZ = "America/Chicago"


def _allday(
    start: str,
    end: str,
    *,
    event_type: str | None = None,
    summary: str = "Out of office",
) -> dict:
    ev = {"start": {"date": start}, "end": {"date": end}, "summary": summary}
    if event_type:
        ev["eventType"] = event_type
    return ev


def _timed(
    start_iso: str,
    end_iso: str,
    *,
    event_type: str = "outOfOffice",
    summary: str = "OOO",
) -> dict:
    return {
        "start": {"dateTime": start_iso},
        "end": {"dateTime": end_iso},
        "eventType": event_type,
        "summary": summary,
    }


def test_all_day_ooo_week_is_detected_with_correct_window_and_return():
    # Google all-day end date is EXCLUSIVE, so 13..18 covers Mon 13 – Fri 17.
    events = [_allday("2026-07-13", "2026-07-18", event_type="outOfOffice")]
    st = status_from_events(
        events, user_email="u@x.example", today=date(2026, 7, 16), tz_name=TZ
    )
    assert st.on_pto_today is True
    assert st.current is not None
    assert st.current.start == date(2026, 7, 13)
    assert st.current.end == date(2026, 7, 17)
    # Out through Friday -> back Monday (weekend skipped).
    assert st.current.return_date == date(2026, 7, 20)
    assert st.return_date == date(2026, 7, 20)


def test_typed_vacation_title_without_event_type_counts():
    events = [_allday("2026-07-16", "2026-07-17", summary="🌴 Vacation")]
    st = status_from_events(
        events, user_email="u@x.example", today=date(2026, 7, 16), tz_name=TZ
    )
    assert st.on_pto_today is True
    assert st.current.is_single_day


def test_real_meeting_containing_a_hold_word_is_not_ooo():
    # Exact-match discipline: "Busy season planning" must NOT read as OOO.
    events = [_allday("2026-07-16", "2026-07-17", summary="Busy season planning")]
    st = status_from_events(
        events, user_email="u@x.example", today=date(2026, 7, 16), tz_name=TZ
    )
    assert st.on_pto_today is False
    assert st.upcoming == ()


def test_weekend_bridges_two_adjacent_ooo_events_into_one_period():
    events = [
        _allday("2026-07-17", "2026-07-18", event_type="outOfOffice"),  # Fri
        _allday("2026-07-20", "2026-07-21", event_type="outOfOffice"),  # Mon
    ]
    periods = pto_periods_from_events(events, tz_name=TZ)
    assert len(periods) == 1
    assert periods[0].start == date(2026, 7, 17)
    assert periods[0].end == date(2026, 7, 20)


def test_real_working_gap_stays_two_periods():
    events = [
        _allday("2026-07-13", "2026-07-14", event_type="outOfOffice"),  # Mon
        _allday("2026-07-16", "2026-07-17", event_type="outOfOffice"),  # Thu
    ]
    periods = pto_periods_from_events(events, tz_name=TZ)
    assert len(periods) == 2


def test_short_timed_ooo_block_is_not_a_pto_day():
    events = [_timed("2026-07-16T14:00:00-05:00", "2026-07-16T15:00:00-05:00")]
    st = status_from_events(
        events, user_email="u@x.example", today=date(2026, 7, 16), tz_name=TZ
    )
    assert st.on_pto_today is False


def test_full_day_timed_ooo_block_counts():
    events = [_timed("2026-07-16T08:00:00-05:00", "2026-07-16T17:00:00-05:00")]
    st = status_from_events(
        events, user_email="u@x.example", today=date(2026, 7, 16), tz_name=TZ
    )
    assert st.on_pto_today is True


def test_no_events_is_not_on_pto():
    st = status_from_events(
        [], user_email="u@x.example", today=date(2026, 7, 16), tz_name=TZ
    )
    assert isinstance(st, PtoStatus)
    assert st.on_pto_today is False
    assert st.current is None


def test_not_on_pto_today_but_upcoming_period_still_reported():
    # Next week's vacation should show in `upcoming` without flagging today.
    events = [_allday("2026-07-27", "2026-07-31", event_type="outOfOffice")]
    st = status_from_events(
        events, user_email="u@x.example", today=date(2026, 7, 16), tz_name=TZ
    )
    assert st.on_pto_today is False
    assert st.current is None
    assert len(st.upcoming) == 1
    assert st.upcoming[0].start == date(2026, 7, 27)

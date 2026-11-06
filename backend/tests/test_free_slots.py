"""Tests for free-slot computation (backend.routers.home._compute_free_slots).

Regression coverage for the timezone bug where a user's 9am-6pm work window was
built in UTC while their real meetings were in local time — so a packed
calendar showed as fully free and slot labels were garbage (e.g. the
"9:00 AM – 9:00 AM" zero-width slot seen in the Productivity page).
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from backend.routers.home import _NO_SLOTS_SENTINEL, _compute_free_slots

PACIFIC = ZoneInfo("America/Los_Angeles")
EASTERN = ZoneInfo("America/New_York")


def _timed(day, sh, sm, eh, em, offset, **extra):
    """A timed Google Calendar event in a fixed UTC offset (e.g. '-07:00')."""
    return {
        "summary": "Meeting",
        "start": {"dateTime": f"2026-06-{day:02d}T{sh:02d}:{sm:02d}:00{offset}"},
        "end": {"dateTime": f"2026-06-{day:02d}T{eh:02d}:{em:02d}:00{offset}"},
        **extra,
    }


def _all_day(start_date, end_date, **extra):
    return {
        "summary": "OOO",
        "start": {"date": start_date},
        "end": {"date": end_date},
        **extra,
    }


def _all_slots(free_slots):
    return [s for fs in free_slots for s in fs.slots]


def _today(free_slots):
    """First FreeSlot row == 'today' by the backend's 4-day-lookahead convention."""
    return free_slots[0]


# ── The reported bug ────────────────────────────────────────────────────────


def test_packed_pacific_day_is_not_shown_as_free():
    """A Pacific user with a packed 9-5 calendar must not show phantom free
    slots, and must never emit a zero-width '9:00 AM – 9:00 AM' label."""
    # 9-10 standup, 10-11 checkpoint, 1-2 1:1, 3:15-4 lunch, 3:30-5 L10 (PDT).
    events = [
        _timed(24, 9, 0, 10, 0, "-07:00"),
        _timed(24, 10, 0, 11, 0, "-07:00"),
        _timed(24, 13, 0, 14, 0, "-07:00"),
        _timed(24, 15, 15, 16, 0, "-07:00"),
        _timed(24, 15, 30, 17, 0, "-07:00"),
    ]
    start = datetime(2026, 6, 24, 16, 0, tzinfo=timezone.utc)  # 9:00 AM PDT
    free = _compute_free_slots(events, start, start, tz=PACIFIC, now=start)

    today = _today(free)
    assert today.date == "Wednesday, June 24"
    # Real gaps only: 11-1, 2-3:15, 5-6. Never the whole day, never zero-width.
    assert today.slots == [
        "11:00 AM – 1:00 PM",
        "2:00 PM – 3:15 PM",
        "5:00 PM – 6:00 PM",
    ]
    # The exact screenshot artifact must be gone everywhere.
    assert "9:00 AM – 9:00 AM" not in _all_slots(free)
    assert "9:00 AM – 6:00 PM" not in today.slots


def test_no_zero_width_or_backwards_slots_anywhere():
    """Across all produced slots, start time is strictly before end time."""
    events = [
        _timed(24, 9, 0, 10, 0, "-07:00"),
        _timed(24, 15, 30, 17, 0, "-07:00"),
    ]
    start = datetime(2026, 6, 24, 16, 0, tzinfo=timezone.utc)
    free = _compute_free_slots(events, start, start, tz=PACIFIC, now=start)
    for label in _all_slots(free):
        if label == _NO_SLOTS_SENTINEL:
            continue
        left, right = [p.strip() for p in label.split("–")]
        assert left != right, f"zero-width slot: {label}"


# ── Window + timezone correctness ───────────────────────────────────────────


def test_empty_day_is_one_full_local_window():
    """A day with no meetings is one big 9-6 free block (in local time)."""
    start = datetime(2026, 6, 24, 13, 0, tzinfo=timezone.utc)  # 9 AM EDT
    free = _compute_free_slots([], start, start, tz=EASTERN, now=start)
    # Tomorrow (offset 1) has no "now" clamp, so it's the clean full window.
    assert free[1].slots == ["9:00 AM – 6:00 PM"]


def test_today_window_starts_at_now_not_9am():
    """You can't book the past: today's window begins at `now`."""
    # now = 2:00 PM Eastern, no meetings.
    now = datetime(2026, 6, 24, 18, 0, tzinfo=timezone.utc)  # 2 PM EDT
    free = _compute_free_slots([], now, now, tz=EASTERN, now=now)
    assert free[0].slots == ["2:00 PM – 6:00 PM"]


def test_workday_already_over_today():
    """If it's past 6 PM local, today has no bookable window."""
    now = datetime(2026, 6, 25, 1, 0, tzinfo=timezone.utc)  # 9 PM EDT (Jun 24)
    free = _compute_free_slots([], now, now, tz=EASTERN, now=now)
    assert free[0].slots == [_NO_SLOTS_SENTINEL]


def test_utc_user_baseline():
    """UTC user: a 10-11 UTC meeting yields 9-10 and 11-6."""
    events = [_timed(24, 10, 0, 11, 0, "+00:00")]
    start = datetime(2026, 6, 24, 8, 0, tzinfo=timezone.utc)
    free = _compute_free_slots(events, start, start, tz=timezone.utc, now=start)
    assert free[0].slots == ["9:00 AM – 10:00 AM", "11:00 AM – 6:00 PM"]


# ── Busy semantics ──────────────────────────────────────────────────────────


def test_overlapping_meetings_merge_no_phantom_gap():
    """Double-booked meetings must merge, not create a backwards gap."""
    events = [
        _timed(24, 13, 0, 14, 0, "+00:00"),
        _timed(24, 13, 30, 15, 0, "+00:00"),  # overlaps the first
    ]
    start = datetime(2026, 6, 24, 8, 0, tzinfo=timezone.utc)
    free = _compute_free_slots(events, start, start, tz=timezone.utc, now=start)
    # Busy block is 1-3 PM merged → free 9-1 and 3-6.
    assert free[0].slots == ["9:00 AM – 1:00 PM", "3:00 PM – 6:00 PM"]


def test_all_day_event_blocks_the_day():
    """An opaque all-day event (PTO/OOO) marks the whole day fully booked."""
    events = [_all_day("2026-06-24", "2026-06-25")]
    start = datetime(2026, 6, 24, 13, 0, tzinfo=timezone.utc)
    free = _compute_free_slots(events, start, start, tz=EASTERN, now=start)
    assert free[0].slots == [_NO_SLOTS_SENTINEL]


def test_declined_event_does_not_block():
    """An event the user declined is not busy time."""
    events = [
        _timed(
            24,
            10,
            0,
            17,
            0,
            "+00:00",
            attendees=[{"self": True, "responseStatus": "declined"}],
        )
    ]
    start = datetime(2026, 6, 24, 8, 0, tzinfo=timezone.utc)
    free = _compute_free_slots(events, start, start, tz=timezone.utc, now=start)
    assert free[0].slots == ["9:00 AM – 6:00 PM"]


def test_transparent_event_does_not_block():
    """An event marked 'Free' (transparent) is not busy time."""
    events = [_timed(24, 10, 0, 17, 0, "+00:00", transparency="transparent")]
    start = datetime(2026, 6, 24, 8, 0, tzinfo=timezone.utc)
    free = _compute_free_slots(events, start, start, tz=timezone.utc, now=start)
    assert free[0].slots == ["9:00 AM – 6:00 PM"]


def test_busy_placeholder_still_blocks_time():
    """A "Busy" hold is hidden from the rendered agenda (see is_placeholder_hold)
    but is still genuine busy time — the free-slot math counts it, so
    availability stays correct even though the event never displays."""
    events = [_timed(24, 10, 0, 11, 0, "+00:00", summary="Busy")]
    start = datetime(2026, 6, 24, 8, 0, tzinfo=timezone.utc)
    free = _compute_free_slots(events, start, start, tz=timezone.utc, now=start)
    assert free[0].slots == ["9:00 AM – 10:00 AM", "11:00 AM – 6:00 PM"]


def test_event_outside_window_is_ignored():
    """An 8 PM meeting (outside 9-6) does not corrupt the day's free slots."""
    events = [_timed(24, 20, 0, 21, 0, "+00:00")]
    start = datetime(2026, 6, 24, 8, 0, tzinfo=timezone.utc)
    free = _compute_free_slots(events, start, start, tz=timezone.utc, now=start)
    assert free[0].slots == ["9:00 AM – 6:00 PM"]

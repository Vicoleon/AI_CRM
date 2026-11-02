"""Pipeline #2 staleness guard — a recommendation anchored on a calendar
event that has already passed must not keep surfacing the next day (the
analogue of the "prep for yesterday's L10" bug on the overnight-recs path).
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from backend.routers.agents import _rec_event_is_past

NOW_UTC = datetime(2026, 6, 25, 16, 0, tzinfo=timezone.utc)


def _rec(sources):
    return SimpleNamespace(sources=sources)


def _cal_source(starts_at: str) -> dict:
    return {
        "source": "calendar",
        "occurred_at": "2026-06-25T12:00:00+00:00",
        "payload": {"event_title": "Acme sync", "starts_at": starts_at},
    }


def test_past_calendar_event_marks_rec_stale():
    rec = _rec([_cal_source("2026-06-24T14:00:00Z")])
    assert _rec_event_is_past(rec, NOW_UTC) is True


def test_future_calendar_event_is_not_stale():
    rec = _rec([_cal_source("2026-06-26T14:00:00Z")])
    assert _rec_event_is_past(rec, NOW_UTC) is False


def test_non_calendar_rec_is_never_event_stale():
    # A HubSpot/deal rec carries no calendar event → age floor governs it,
    # not the event-passed check.
    rec = _rec([{"source": "hubspot", "payload": {"dealstage": "qualified"}}])
    assert _rec_event_is_past(rec, NOW_UTC) is False
    assert _rec_event_is_past(_rec(None), NOW_UTC) is False


def test_mixed_events_only_stale_when_all_past():
    rec = _rec(
        [
            _cal_source("2026-06-24T14:00:00Z"),  # past
            _cal_source("2026-06-27T14:00:00Z"),  # future
        ]
    )
    # One future event is enough to keep the rec alive.
    assert _rec_event_is_past(rec, NOW_UTC) is False

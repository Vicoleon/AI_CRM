"""UC-17.4 — Calendar SignalSource.

Mocks the Calendar service entirely (no live API). Covers:
  - upcoming meeting <4h away with external attendee → high severity
  - upcoming meeting ~24h away with external attendee → medium severity
  - internal-only meeting → no signal
  - past meeting (3d ago) with external + no follow-up → missed_followup
  - past meeting (3d ago) with external + future follow-up event → no signal
  - recurring internal sync ("Weekly Engineering Standup") → no signal
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

REP = "rep1@acme.example"
NOW = datetime(2026, 5, 13, 15, 0, tzinfo=timezone.utc)


# ─── Fixture helpers ──────────────────────────────────────────────────────────


def _event(
    *,
    event_id: str,
    summary: str,
    start: datetime,
    end: datetime | None = None,
    attendees: list[str] | None = None,
    organizer: str = REP,
    recurring_event_id: str | None = None,
    meet_link: str | None = None,
) -> dict[str, Any]:
    end = end or (start + timedelta(minutes=30))
    body: dict[str, Any] = {
        "id": event_id,
        "summary": summary,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
        "organizer": {"email": organizer},
        "attendees": [{"email": e} for e in (attendees or [])],
    }
    if recurring_event_id:
        body["recurringEventId"] = recurring_event_id
    if meet_link:
        body["conferenceData"] = {
            "entryPoints": [{"entryPointType": "video", "uri": meet_link}],
        }
    return body


def _service_mock(events: list[dict[str, Any]]) -> MagicMock:
    svc = MagicMock()
    svc.list_events_for_signals.return_value = events
    return svc


# ─── Upcoming meeting severity ────────────────────────────────────────────────


def test_upcoming_meeting_within_4h_is_high_severity():
    from backend.signals.calendar import CalendarSignalSource

    start = NOW + timedelta(hours=3)
    ev = _event(
        event_id="evt-1",
        summary="Q2 expansion check-in",
        start=start,
        attendees=[REP, "jane@acmecorp.alt"],
        meet_link="https://meet.google.com/abc-def-ghi",
    )
    src = CalendarSignalSource(calendar_service=_service_mock([ev]), now=NOW)

    sigs = src.fetch(REP, since=NOW)

    assert len(sigs) == 1
    s = sigs[0]
    assert s.signal_type == "upcoming_meeting_with_customer"
    assert s.source == "calendar"
    assert s.severity_hint == "high"
    assert s.entity_refs["event_id"] == "evt-1"
    assert s.entity_refs["contact_email"] == "jane@acmecorp.alt"
    assert s.entity_refs["company_name"] == "acmecorp"
    assert s.payload["attendee_emails"] == ["jane@acmecorp.alt"]
    assert s.payload["organizer_email"] == REP
    assert s.payload["meet_link"] == "https://meet.google.com/abc-def-ghi"
    assert s.payload["is_recurring"] is False
    assert "jane@acmecorp.alt" in s.summary
    assert "Q2 expansion check-in" in s.summary
    assert len(s.summary) <= 140


def test_upcoming_meeting_24h_away_is_medium_severity():
    from backend.signals.calendar import CalendarSignalSource

    start = NOW + timedelta(hours=24)
    ev = _event(
        event_id="evt-2",
        summary="Discovery call",
        start=start,
        attendees=[REP, "buyer@bigco.com"],
    )
    src = CalendarSignalSource(calendar_service=_service_mock([ev]), now=NOW)

    sigs = src.fetch(REP, since=NOW)

    assert len(sigs) == 1
    assert sigs[0].severity_hint == "medium"
    assert sigs[0].entity_refs["contact_email"] == "buyer@bigco.com"


# ─── Internal-only meeting is filtered ────────────────────────────────────────


def test_internal_only_meeting_emits_no_signal():
    from backend.signals.calendar import CalendarSignalSource

    ev = _event(
        event_id="evt-internal",
        summary="Pipeline review",
        start=NOW + timedelta(hours=2),
        attendees=[REP, "boss@acme.example", "peer@acme.example"],
    )
    src = CalendarSignalSource(calendar_service=_service_mock([ev]), now=NOW)

    assert src.fetch(REP, since=NOW) == []


def test_recurring_internal_standup_emits_no_signal():
    from backend.signals.calendar import CalendarSignalSource

    ev = _event(
        event_id="evt-standup",
        summary="Weekly Engineering Standup",
        start=NOW + timedelta(hours=1),
        attendees=[REP, "peer@acme.example"],
        recurring_event_id="recurring-1",
    )
    src = CalendarSignalSource(calendar_service=_service_mock([ev]), now=NOW)

    assert src.fetch(REP, since=NOW) == []


# ─── Missed follow-up detection ───────────────────────────────────────────────


def test_past_meeting_with_external_no_followup_emits_missed_followup():
    from backend.signals.calendar import CalendarSignalSource

    past = NOW - timedelta(days=3)
    ev = _event(
        event_id="evt-past",
        summary="Renewal kickoff",
        start=past,
        attendees=[REP, "buyer@acmecorp.alt"],
    )
    src = CalendarSignalSource(calendar_service=_service_mock([ev]), now=NOW)

    sigs = src.fetch(REP, since=NOW)

    assert len(sigs) == 1
    s = sigs[0]
    assert s.signal_type == "missed_followup"
    assert s.severity_hint == "medium"
    assert s.entity_refs["contact_email"] == "buyer@acmecorp.alt"
    assert s.entity_refs["company_name"] == "acmecorp"
    assert s.payload["event_id"] == "evt-past"
    assert "buyer@acmecorp.alt" in s.summary
    assert len(s.summary) <= 140


def test_past_meeting_with_future_followup_emits_no_signal():
    from backend.signals.calendar import CalendarSignalSource

    past = NOW - timedelta(days=3)
    future = NOW + timedelta(days=4)
    past_event = _event(
        event_id="evt-past",
        summary="Renewal kickoff",
        start=past,
        attendees=[REP, "buyer@acmecorp.alt"],
    )
    followup = _event(
        event_id="evt-followup",
        summary="Renewal kickoff — follow-up",
        # Push outside the 48h lookahead so we don't also get an
        # upcoming_meeting_with_customer signal muddying the assertion.
        start=future,
        attendees=[REP, "buyer@acmecorp.alt"],
    )
    src = CalendarSignalSource(
        calendar_service=_service_mock([past_event, followup]),
        now=NOW,
    )

    sigs = src.fetch(REP, since=NOW)

    # No missed_followup; followup itself is >48h out so no upcoming signal.
    assert [s.signal_type for s in sigs] == []


# ─── Protocol conformance ─────────────────────────────────────────────────────


def test_calendar_source_satisfies_signal_source_protocol():
    from backend.signals import SignalSource
    from backend.signals.calendar import CalendarSignalSource

    src = CalendarSignalSource(calendar_service=_service_mock([]))
    assert isinstance(src, SignalSource)

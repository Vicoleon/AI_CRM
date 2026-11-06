"""The calendar call-capture read path, and the arming write it hands off to.

Under test, in this order: the day window (``day_window_utc``), the Meet-link
parsing (``meet_uri_for_event`` / ``meeting_code_from_uri``), the filter chain
(``is_capturable_meeting``), the rendered rows (``build_rows`` — the company
block especially), the arming write through ``POST /api/calls/start``, and
``scan_day``'s outage behaviour.

Everything asserted here follows from one framing. The calendar screen's PRIMARY
job is to bind a meeting to the right HubSpot company and hand the AE the
calendar's own Meet link; arming transcription is SECONDARY and best-effort,
because a conference already running cannot be armed, a client-hosted space
lives in another Workspace tenant and cannot be patched at all, and a recurring
series reuses ONE space. So a row Cubie cannot arm is still a valuable row, and
the two failures worth defending against are mirror images of each other: **an
unarmable row must never look armed, and an armed row must never look broken.**
Both are silent, which is why every verdict in this file is asserted by name.

The other rule these tests encode: a silent no-op is the worst outcome. "We
could not ask HubSpot" (``match == "error"``) and "HubSpot has no such company"
(``match == "not_found"``) lead to different actions, so no test lets them
collapse; the same goes for ``calendar_available: False`` with no meetings
("we could not look") versus ``True`` with no meetings ("you have no client
calls today"), which is a sentence the AE BELIEVES.

Nothing here talks to Google or HubSpot. Both boundaries are faked at their
client and patched **by string target**, because other suites
``importlib.reload()`` modules mid-run and an object-target patch goes stale in
a full-suite run.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth import get_current_user
from backend.main import app
from rubik_core.call_capture import calendar_scan, domain_match
from rubik_core.call_capture.calendar_scan import (
    ARMING_REASONS,
    ARMING_STATES,
    CAPTURE_STATES,
    COMPANY_MATCH_STATES,
    build_rows,
    day_window_utc,
    is_capturable_meeting,
    meet_uri_for_event,
    meeting_code_from_uri,
    scan_day,
)
from rubik_core.call_capture.provenance import stamp_session_source
from rubik_core.config import settings
from rubik_core.database.call_capture import CallSession
from rubik_core.database.db import get_db
from rubik_core.database.schemas import UserProfile
from rubik_core.identifiers import CALL_ORIGIN_ASSISTANT
from rubik_core.retrieval.hubspot_api import CompanyMatch, DomainMatch

client = TestClient(app)

AE = "dakota@acme.example"
COLLEAGUE = "sam@acme.example"
CLIENT = "vp@acmeindustries.alt"
CLIENT_DOMAIN = "acmeindustries.alt"
ROOM = "boardroom@resource.calendar.google.com"

CODE = "abc-defg-hij"
URI = f"https://meet.google.com/{CODE}"
EVENT = "evt_20260828T190000Z"
CANON = "spaces/CANONICAL1111"

CHI = ZoneInfo("America/Chicago")
#: 13:56 local, four minutes before the 14:00 base event starts.
BEFORE = datetime(2026, 8, 28, 13, 56, tzinfo=CHI)
#: 14:04 local, four minutes after it started and inside its 30 minutes.
DURING = datetime(2026, 8, 28, 14, 4, tzinfo=CHI)


# ── Scaffolding ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_domain_cache(monkeypatch):
    """The domain verdict cache is module-level; a leak makes order matter."""
    monkeypatch.setattr(domain_match, "workspace_secret", lambda *_: "test-token")
    calendar_scan.clear_cache()
    yield
    calendar_scan.clear_cache()


@pytest.fixture(autouse=True)
def flags(monkeypatch, set_test_workspace_domains):
    """Both switches ON, so a test exercises the RULE and not the flag.

    ``CCS_MEET_ENABLED`` matters to the READ path too: with it off every
    otherwise-armable row reads ``write_disabled``, which would mask the
    precondition each filter test is actually about.
    """
    monkeypatch.setattr(settings, "CCS_ENABLED", True)
    monkeypatch.setattr(settings, "CCS_MEET_ENABLED", True)
    set_test_workspace_domains("acme.example")
    monkeypatch.setattr(domain_match, "_workspace_deal_pipelines", lambda: {"default"})
    monkeypatch.setattr(
        "rubik_core.tenancy.crm_visibility.visible_source_ids",
        lambda companies, contacts, deals: (companies, contacts, deals),
    )


def _attendee(email: str, **over) -> dict:
    row = {"email": email, "responseStatus": "accepted"}
    row.update(over)
    return row


def _event(**over) -> dict:
    """A capturable client call: timed, Meet-linked, AE-organized, one external.

    Every filter test below takes this and breaks exactly one thing, so a
    failure names the rule instead of the fixture.
    """
    event = {
        "id": EVENT,
        "summary": "Acme Industries - Rollout scope",
        "start": {
            "dateTime": "2026-08-28T14:00:00-05:00",
            "timeZone": "America/Chicago",
        },
        "end": {"dateTime": "2026-08-28T14:30:00-05:00"},
        "organizer": {"email": AE, "self": True},
        "hangoutLink": URI,
        "attendees": [
            _attendee(AE, self=True, organizer=True),
            _attendee(CLIENT, displayName="Val Prospect"),
        ],
    }
    event.update(over)
    return event


def _company(
    cid: str = "123",
    name: str = "Acme Industries LLC",
    domain: str = CLIENT_DOMAIN,
    stage: str = "customer",
) -> CompanyMatch:
    return CompanyMatch(
        hubspot_company_id=cid, name=name, domain=domain, lifecyclestage=stage
    )


_UNIQUE = {CLIENT_DOMAIN: DomainMatch(domain=CLIENT_DOMAIN, candidates=(_company(),))}


def _rows(
    events,
    *,
    now: datetime | None = None,
    matches: dict | None = None,
    sessions: dict | None = None,
    ae: str = AE,
) -> list[dict]:
    return build_rows(
        events,
        ae_email=ae,
        tz_name="America/Chicago",
        now_local=now or BEFORE,
        domain_matches=_UNIQUE if matches is None else matches,
        sessions_by_event=sessions or {},
    )


def _one(event: dict, **kw) -> dict:
    rows = _rows([event], **kw)
    assert len(rows) == 1, f"expected exactly one row, got {rows}"
    return rows[0]


def _profile(email: str = AE, *, department: str = "Systems") -> UserProfile:
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name=email.split("@")[0].title(),
        department=department,
        roles=["user"],
        is_exec=False,
        managed_departments=[],
        # Both are REQUIRED strings on this schema.
        created_at="2026-08-28T00:00:00",
        updated_at="2026-08-28T00:00:00",
    )


@pytest.fixture
def db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from rubik_core.database import eos  # noqa: F401 — registers the eos_* tables
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr("rubik_core.database.db.SessionLocal", Session)
    session = Session()
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: _profile()
    try:
        yield session
    finally:
        app.dependency_overrides.clear()
        session.close()
        engine.dispose()


# ═══ 1. The day window ══════════════════════════════════════════════════════
#
# The naive-datetime bug this guards against is invisible until someone travels
# or works late: passing a NAIVE local midnight makes tools/calendar.py stamp a
# "Z" on it, and the whole day shifts by the UTC offset. availability/pto.py has
# exactly that bug; home.py builds the boundaries aware and then converts.


def test_a_pacific_ae_late_at_night_still_gets_their_own_local_day():
    """The window is the LOCAL day, converted — not the local clock stamped "Z".

    A Pacific AE at 23:30 on 2026-08-28 is at 06:30Z on the 29th. The correct
    window (07:00Z→07:00Z) contains that instant; the naive window
    (00:00Z→00:00Z) excludes it, so the AE's 11:30pm call would vanish and
    tomorrow's early meetings would appear as today's.
    """
    time_min, time_max = day_window_utc("America/Los_Angeles", date(2026, 8, 28))

    assert (time_min, time_max) == ("2026-08-28T07:00:00Z", "2026-08-29T07:00:00Z")

    # Converted back, the endpoints are local midnight and the NEXT local
    # midnight — which is the property, not the literal strings above.
    la = ZoneInfo("America/Los_Angeles")
    start = datetime.fromisoformat(time_min.replace("Z", "+00:00")).astimezone(la)
    end = datetime.fromisoformat(time_max.replace("Z", "+00:00")).astimezone(la)
    assert (start.date(), start.hour, start.minute) == (date(2026, 8, 28), 0, 0)
    assert (end.date(), end.hour, end.minute) == (date(2026, 8, 29), 0, 0)

    # The 23:30-local instant the naive form would drop.
    late = datetime(2026, 8, 28, 23, 30, tzinfo=la).astimezone(timezone.utc)
    assert start.astimezone(timezone.utc) <= late < end.astimezone(timezone.utc)
    assert late > datetime.fromisoformat("2026-08-29T00:00:00+00:00"), (
        "the naive window would have ended before this AE's last call"
    )


def test_a_utc_ae_gets_midnight_to_midnight():
    assert day_window_utc("UTC", date(2026, 8, 28)) == (
        "2026-08-28T00:00:00Z",
        "2026-08-29T00:00:00Z",
    )


def test_a_chicago_ae_gets_the_cdt_offset():
    assert day_window_utc("America/Chicago", date(2026, 8, 28)) == (
        "2026-08-28T05:00:00Z",
        "2026-08-29T05:00:00Z",
    )


def test_a_dst_transition_day_is_23_hours_and_neither_endpoint_raises():
    """Spring-forward: 2026-03-08 in Chicago is a 23-hour local day.

    Both endpoints are still real instants and the offsets DIFFER (CST at the
    start, CDT at the end), which a single fixed-offset shortcut would get
    wrong in one direction or the other.
    """
    time_min, time_max = day_window_utc("America/Chicago", date(2026, 3, 8))
    assert (time_min, time_max) == ("2026-03-08T06:00:00Z", "2026-03-09T05:00:00Z")
    start = datetime.fromisoformat(time_min.replace("Z", "+00:00"))
    end = datetime.fromisoformat(time_max.replace("Z", "+00:00"))
    assert (end - start).total_seconds() == 23 * 3600


def test_an_unknown_timezone_falls_back_instead_of_raising():
    """A screen that must still render cannot have its window raise."""
    time_min, time_max = day_window_utc("Mars/Olympus_Mons", date(2026, 8, 28))
    assert time_min.endswith("Z") and time_max.endswith("Z")
    assert time_min < time_max


def test_the_endpoints_are_z_stamped_and_never_carry_an_offset():
    """Google's list API dislikes ``+00:00``; every caller passes these raw."""
    for name in ("UTC", "America/Chicago", "America/Los_Angeles", "Asia/Tokyo"):
        for value in day_window_utc(name, date(2026, 8, 28)):
            assert value.endswith("Z"), value
            assert "+" not in value, value


# ═══ 2. Meet links — and the extract_meeting_code trap ══════════════════════


def test_a_meet_hangout_link_yields_its_uri_and_code():
    event = _event()
    assert meet_uri_for_event(event) == URI
    assert meeting_code_from_uri(meet_uri_for_event(event)) == CODE


def test_a_video_entry_point_is_used_when_there_is_no_hangout_link():
    event = _event(
        hangoutLink=None,
        conferenceData={
            "conferenceId": CODE,
            "entryPoints": [
                {"entryPointType": "phone", "uri": "tel:+1-555-0100"},
                {"entryPointType": "video", "uri": f"{URI}?hs=224"},
            ],
        },
    )
    assert meet_uri_for_event(event) == f"{URI}?hs=224"
    assert meeting_code_from_uri(meet_uri_for_event(event)) == CODE


def test_a_lookalike_host_is_not_google_meet():
    """Parsed host, never a substring — ``meet.google.com.evil.example`` is not Meet."""
    assert (
        meet_uri_for_event({"hangoutLink": "https://meet.google.com.evil.example/x"})
        is None
    )
    assert (
        meet_uri_for_event({"hangoutLink": "https://zoom.us/j/meet.google.com"}) is None
    )


def test_a_multi_segment_meet_path_is_not_a_meeting_code():
    """``lookup/<hash>`` and ``tel/...`` are Meet URLs but not codes.

    Handing one to ``spaces.get`` would 404 and the row would blame the wrong
    thing, so the code is ``None`` and the arming reason says ``lookup_failed``.
    """
    assert meeting_code_from_uri("https://meet.google.com/lookup/abcdef1234") is None
    assert meeting_code_from_uri("https://meet.google.com/tel/1-555-0100") is None


# ═══ 3. The filter chain ════════════════════════════════════════════════════


def test_the_base_client_call_is_capturable():
    """The control. Without this, every drop below could be the fixture."""
    assert is_capturable_meeting(_event(), ae_email=AE) is True


def test_a_focus_block_is_dropped():
    event = _event(eventType="focusTime", summary="Focus time")
    assert is_capturable_meeting(event, ae_email=AE) is False
    assert _rows([event]) == []


def test_a_typed_ooo_hold_is_dropped():
    """No ``eventType`` at all — just the title someone typed."""
    event = _event(summary="OOO")
    assert is_capturable_meeting(event, ae_email=AE) is False
    assert _rows([event]) == []


def test_a_real_meeting_that_merely_contains_a_hold_word_survives():
    """ "Focus group with Acme" is a client call, not a hold."""
    assert is_capturable_meeting(_event(summary="Focus group with Acme"), ae_email=AE)


def test_a_declined_invite_is_dropped():
    """A decision the AE already made. Organized by a colleague, because
    ``self_response_status`` (correctly) ignores the organizer's own RSVP."""
    event = _event(
        organizer={"email": COLLEAGUE},
        attendees=[
            _attendee(COLLEAGUE, organizer=True),
            _attendee(AE, self=True, responseStatus="declined"),
            _attendee(CLIENT),
        ],
    )
    assert is_capturable_meeting(event, ae_email=AE) is False
    assert _rows([event]) == []


def test_a_tentative_invite_is_kept():
    """Only ``declined`` is a decision; tentative is still a call to capture."""
    event = _event(
        organizer={"email": COLLEAGUE},
        attendees=[
            _attendee(COLLEAGUE, organizer=True),
            _attendee(AE, self=True, responseStatus="tentative"),
            _attendee(CLIENT),
        ],
    )
    assert is_capturable_meeting(event, ae_email=AE) is True
    assert _one(event)["rsvp"] == "tentative"


def test_an_all_day_event_is_dropped():
    """A client call is an instant. An all-day row has no clock to arm against."""
    event = _event(start={"date": "2026-08-28"}, end={"date": "2026-08-29"})
    assert is_capturable_meeting(event, ae_email=AE) is False
    assert _rows([event]) == []


def test_a_zoom_event_is_dropped_and_its_conference_id_never_becomes_a_meet_code():
    """The ``extract_meeting_code`` trap, pinned from both sides.

    That helper ends with an unconditional ``conferenceData.conferenceId``
    fallback, so a Zoom event yields ZOOM's id — behaviour its own callers rely
    on and ``test_meet_transcripts.py`` pins, which is why it is left alone.
    On this screen the Start button is a Meet operation, so a Zoom id reaching
    ``meeting_code`` would send ``spaces.get`` after a space that does not
    exist and blame the wrong thing.
    """
    from rubik_core.tools.calendar import GoogleCalendarClient

    zoom_id = "987654321"
    event = _event(
        summary="Acme Industries <> our team (Zoom)",
        hangoutLink=None,
        conferenceData={
            "conferenceId": zoom_id,
            "conferenceSolution": {"key": {"type": "addOn"}},
            "entryPoints": [
                {
                    "entryPointType": "video",
                    "uri": f"https://us02web.zoom.us/j/{zoom_id}",
                }
            ],
        },
    )

    # The trap itself, documented rather than assumed.
    assert GoogleCalendarClient.extract_meeting_code(event) == zoom_id

    assert meet_uri_for_event(event) is None
    assert is_capturable_meeting(event, ae_email=AE) is False
    assert _rows([event]) == []

    # And the mixed case, which is where a trivially-empty assertion would hide
    # the bug: an event that DOES have a Meet link but also carries a foreign
    # conferenceId must take its code from the Meet URI alone.
    mixed = dict(event, hangoutLink=URI)
    assert GoogleCalendarClient.extract_meeting_code(mixed) == CODE
    row = _one(mixed)
    assert row["meeting_code"] == CODE
    assert row["meeting_code"] != zoom_id
    assert row["meet_uri"] == URI

    # The nastiest shape: a foreign conference whose id is Meet-SHAPED, so the
    # fallback returns something that looks exactly like a real meeting code.
    disguised = dict(
        event,
        conferenceData={
            "conferenceId": "xyz-uvwx-yzz",
            "entryPoints": [
                {"entryPointType": "video", "uri": "https://teams.microsoft.com/l/m/1"}
            ],
        },
    )
    assert GoogleCalendarClient.extract_meeting_code(disguised) == "xyz-uvwx-yzz"
    assert meet_uri_for_event(disguised) is None
    assert is_capturable_meeting(disguised, ae_email=AE) is False


def test_a_phone_only_event_is_dropped():
    event = _event(
        hangoutLink=None,
        conferenceData={
            "entryPoints": [{"entryPointType": "phone", "uri": "tel:+15550100"}]
        },
    )
    assert is_capturable_meeting(event, ae_email=AE) is False


def test_an_internal_only_meeting_is_dropped():
    event = _event(
        summary="Pipeline review",
        attendees=[
            _attendee(AE, self=True, organizer=True),
            _attendee(COLLEAGUE),
        ],
    )
    assert is_capturable_meeting(event, ae_email=AE) is False
    assert _rows([event]) == []


def test_a_room_resource_is_never_an_external_attendee():
    """A room address read as a company is a wrong CRM write.

    The room is dropped from the attendee list AND from the domain set, and a
    meeting whose only outside "attendee" is a room is internal.
    """
    event = _event(
        attendees=[
            _attendee(AE, self=True, organizer=True),
            _attendee(CLIENT),
            _attendee(ROOM, resource=True, displayName="Boardroom"),
        ]
    )
    row = _one(event)
    assert row["external_domains"] == [CLIENT_DOMAIN]
    assert ROOM not in {a["email"] for a in row["attendees"]}
    assert [a["email"] for a in row["attendees"] if a["is_external"]] == [CLIENT]

    room_only = _event(
        attendees=[
            _attendee(AE, self=True, organizer=True),
            _attendee(ROOM, resource=True),
        ]
    )
    assert is_capturable_meeting(room_only, ae_email=AE) is False


def test_a_secondary_calendar_pseudo_domain_is_not_a_client():
    """``*@group.calendar.google.com`` arrives looking like a person and carries
    no ``resource`` flag — it must not make an internal meeting look external."""
    event = _event(
        attendees=[
            _attendee(AE, self=True, organizer=True),
            _attendee("sales-team@group.calendar.google.com"),
        ]
    )
    assert is_capturable_meeting(event, ae_email=AE) is False


def test_an_optional_external_guest_is_surfaced_but_changes_nothing():
    """``optional`` is read raw and shown; it must never affect the company."""
    event = _event(
        attendees=[
            _attendee(AE, self=True, organizer=True),
            _attendee(CLIENT, optional=True),
        ]
    )
    row = _one(event)
    assert [a["is_optional"] for a in row["attendees"] if a["email"] == CLIENT] == [
        True
    ]
    assert row["company"]["match"] == "unique"


def test_a_recurring_instance_is_kept_once_and_keyed_on_the_occurrence():
    """``singleEvents=True`` pre-flattens a series: one instance per occurrence,
    each with its own id. ``recurringEventId`` is the SERIES id and keying on it
    would collapse every week's wrap-up into the first occurrence's."""
    first = _event(
        id="evt_series_20260828T190000Z",
        recurringEventId="series_weeklyqc",
        start={"dateTime": "2026-08-28T14:00:00-05:00"},
        end={"dateTime": "2026-08-28T14:30:00-05:00"},
    )
    second = _event(
        id="evt_series_20260904T190000Z",
        recurringEventId="series_weeklyqc",
        start={"dateTime": "2026-09-04T14:00:00-05:00"},
        end={"dateTime": "2026-09-04T14:30:00-05:00"},
    )

    rows = _rows([first, second])
    assert [r["google_event_id"] for r in rows] == [first["id"], second["id"]]
    assert {r["recurring_event_id"] for r in rows} == {"series_weeklyqc"}
    assert "series_weeklyqc" not in {r["google_event_id"] for r in rows}

    # And the same occurrence arriving twice is still one row.
    assert len(_rows([first, dict(first)])) == 1


def test_rows_are_chronological_whatever_order_they_arrive_in():
    late = _event(
        id="evt_late",
        start={"dateTime": "2026-08-28T16:00:00-05:00"},
        end={"dateTime": "2026-08-28T16:30:00-05:00"},
    )
    early = _event(
        id="evt_early",
        start={"dateTime": "2026-08-28T09:00:00-05:00"},
        end={"dateTime": "2026-08-28T09:30:00-05:00"},
    )
    assert [r["google_event_id"] for r in _rows([late, early])] == [
        "evt_early",
        "evt_late",
    ]


def test_the_clock_fields_are_rendered_once_by_format_when_local():
    """The repo has a recorded +2h drift bug from letting anything else render
    a time, so ``start_local`` is pinned to the character."""
    before = _one(_event())
    assert before["start_local"] == "Friday Aug 28, 2:00 PM CDT"
    assert before["starts_in_minutes"] == 4
    assert before["is_live"] is False

    during = _one(_event(), now=DURING)
    assert during["starts_in_minutes"] == -4
    assert during["is_live"] is True

    after = _one(_event(), now=datetime(2026, 8, 28, 15, 0, tzinfo=CHI))
    assert after["is_live"] is False


# ═══ 4. The company block ═══════════════════════════════════════════════════
#
# ``hubspot_company_id`` is NON-NULL only for ``match == "unique"``. That is the
# whole contract: an ambiguous row is resolved by a human with the picker, never
# by list order — the ``candidates[0]`` bug this design exists to prevent.


def test_a_single_company_prefills_its_id():
    row = _one(_event())
    company = row["company"]
    assert company["match"] == "unique"
    assert company["hubspot_company_id"] == "123"
    assert company["name"] == "Acme Industries LLC"
    assert company["domain"] == CLIENT_DOMAIN
    assert company["candidates"] == []


def test_two_companies_on_one_domain_offer_both_and_prefill_nothing():
    matches = {
        CLIENT_DOMAIN: DomainMatch(
            domain=CLIENT_DOMAIN,
            candidates=(
                _company("777", "Acme Industries Holdings"),
                _company("123", "Acme Industries LLC"),
            ),
        )
    }
    company = _one(_event(), matches=matches)["company"]
    assert company["match"] == "ambiguous"
    assert company["hubspot_company_id"] is None
    assert [c["hubspot_company_id"] for c in company["candidates"]] == ["777", "123"]
    assert [c["name"] for c in company["candidates"]] == [
        "Acme Industries Holdings",
        "Acme Industries LLC",
    ]


def test_no_company_echoes_the_domain_and_prefills_nothing():
    matches = {CLIENT_DOMAIN: DomainMatch(domain=CLIENT_DOMAIN)}
    company = _one(_event(), matches=matches)["company"]
    assert company["match"] == "not_found"
    assert company["hubspot_company_id"] is None
    assert company["domain"] == CLIENT_DOMAIN, "the AE needs to know which domain"
    assert company["candidates"] == []


def test_a_hubspot_failure_is_error_and_never_not_found():
    """ "We could not ask" and "there is no such company" lead to different
    actions. Collapsing them tells an AE a live client is not a customer."""
    matches = {
        CLIENT_DOMAIN: DomainMatch(domain=CLIENT_DOMAIN, error="429 rate limited")
    }
    company = _one(_event(), matches=matches)["company"]
    assert company["match"] == "error"
    assert company["match"] != "not_found"
    assert company["hubspot_company_id"] is None


def test_an_error_on_one_of_two_domains_errors_the_whole_row():
    """Prefilling from half an answer is how a proposal lands on the wrong record."""
    other = "brightpath.example.net"
    event = _event(
        attendees=[
            _attendee(AE, self=True, organizer=True),
            _attendee(CLIENT),
            _attendee(f"cfo@{other}"),
        ]
    )
    matches = {
        CLIENT_DOMAIN: DomainMatch(domain=CLIENT_DOMAIN, candidates=(_company(),)),
        other: DomainMatch(domain=other, error="hubspot token rejected"),
    }
    company = _one(event, matches=matches)["company"]
    assert company["match"] == "error"
    assert company["hubspot_company_id"] is None
    # The clean sibling's company is still offered as a suggestion.
    assert [c["hubspot_company_id"] for c in company["candidates"]] == ["123"]


def test_a_domain_nobody_looked_up_is_an_error_not_a_miss():
    """A missing key is the caller's bug. Reporting it as ``not_found`` would
    put HubSpot's name on Cubie's mistake."""
    company = _one(_event(), matches={})["company"]
    assert company["match"] == "error"
    assert company["hubspot_company_id"] is None


def test_two_domains_owned_by_one_company_are_still_unique():
    other = "acme-ind.example.org"
    event = _event(
        attendees=[
            _attendee(AE, self=True, organizer=True),
            _attendee(CLIENT),
            _attendee(f"ops@{other}"),
        ]
    )
    matches = {
        CLIENT_DOMAIN: DomainMatch(domain=CLIENT_DOMAIN, candidates=(_company(),)),
        other: DomainMatch(domain=other, candidates=(_company(domain=other),)),
    }
    company = _one(event, matches=matches)["company"]
    assert company["match"] == "unique"
    assert company["hubspot_company_id"] == "123"


def test_a_gmail_only_client_is_skipped_but_still_a_row_and_still_external():
    """A blocked domain suppresses the MATCH, never the ROW.

    The screen must read "attendee on a personal address — pick the company",
    never "internal" and never "no external attendees".
    """
    personal = "val.prospect@gmail.com"
    event = _event(
        attendees=[
            _attendee(AE, self=True, organizer=True),
            _attendee(personal, displayName="Val Prospect"),
        ]
    )
    row = _one(event, matches={})
    assert row["company"]["match"] == "skipped"
    assert row["company"]["match"] != "not_found"
    assert row["company"]["hubspot_company_id"] is None
    assert row["external_domains"] == []
    assert [a["email"] for a in row["attendees"] if a["is_external"]] == [personal]


def test_a_role_mailbox_on_a_client_domain_still_binds_the_company():
    """``sales@acmecorp.alt`` is still Acme. The role list answers a different
    question (is there a PERSON to research) and must not suppress the match."""
    event = _event(
        attendees=[
            _attendee(AE, self=True, organizer=True),
            _attendee(f"sales@{CLIENT_DOMAIN}"),
        ]
    )
    row = _one(event)
    assert row["external_domains"] == [CLIENT_DOMAIN]
    assert row["company"]["match"] == "unique"


def test_attendees_omitted_forces_ambiguous_even_on_a_clean_unique_match():
    """Google sets ``attendeesOmitted`` when it truncates a large invite, so the
    only external attendee we can see may not be the only one there was."""
    row = _one(_event(attendeesOmitted=True))
    assert row["attendees_truncated"] is True
    assert row["company"]["match"] == "ambiguous"
    assert row["company"]["hubspot_company_id"] is None
    # The candidate found is still offered as a suggestion, not as a prefill.
    assert [c["hubspot_company_id"] for c in row["company"]["candidates"]] == ["123"]


def test_every_row_field_and_vocabulary_is_the_documented_one():
    """A typo'd state does not fail loudly — it renders as an empty badge and
    the AE reads "nothing is wrong". So the closed vocabularies are asserted."""
    row = _one(_event())
    assert set(row) == {
        "google_event_id",
        "recurring_event_id",
        "title",
        "start",
        "end",
        "start_local",
        "starts_in_minutes",
        "is_live",
        "meet_uri",
        "meeting_code",
        "organizer_email",
        "organizer_is_me",
        "rsvp",
        "attendees",
        "external_domains",
        "attendees_truncated",
        "company",
        "call_session_id",
        "capture_state",
        "arming",
    }
    assert row["capture_state"] in CAPTURE_STATES
    assert row["arming"]["state"] in ARMING_STATES
    assert row["arming"]["reason"] in ARMING_REASONS
    assert row["company"]["match"] in COMPANY_MATCH_STATES
    assert row["organizer_is_me"] is True
    assert row["call_session_id"] is None
    assert row["capture_state"] == "not_started"


# ═══ 5. The precondition arming verdict (a pure read, no Meet call) ═════════


@pytest.mark.parametrize(
    ("label", "over", "now", "state", "reason"),
    [
        ("armable", {}, BEFORE, "unknown", "not_checked_yet"),
        (
            "client hosts it",
            {"organizer": {"email": CLIENT}},
            BEFORE,
            "not_armable",
            "external_host",
        ),
        (
            "a colleague owns it",
            {"organizer": {"email": COLLEAGUE}},
            BEFORE,
            "not_armable",
            "not_owner",
        ),
        ("already running", {}, DURING, "not_armable", "already_started"),
        (
            "no resolvable code",
            {"hangoutLink": "https://meet.google.com/lookup/abcdef1234"},
            BEFORE,
            "not_armable",
            "lookup_failed",
        ),
        (
            "recurring occurrence",
            {"recurringEventId": "series_weeklyqc"},
            BEFORE,
            "unknown",
            "recurring_series",
        ),
    ],
)
def test_the_read_path_names_every_precondition_it_can_know_for_free(
    label, over, now, state, reason
):
    """No ``spaces.get``, no patch, no guess — and never a blank verdict.

    A recurring occurrence stays ``unknown``: the series warning informs (one
    space is shared by the whole series, so arming one arms them all), it does
    not block.
    """
    verdict = _one(_event(**over), now=now)["arming"]
    assert (verdict["state"], verdict["reason"]) == (state, reason), label
    if reason != "not_checked_yet":
        assert verdict["detail"], f"{label}: an unarmable row with no reason is a lie"


def test_the_switch_being_off_is_reported_as_write_disabled(monkeypatch):
    monkeypatch.setattr(settings, "CCS_MEET_ENABLED", False)
    verdict = _one(_event())["arming"]
    assert (verdict["state"], verdict["reason"]) == ("not_armable", "write_disabled")


def test_a_shared_calendar_organizer_is_not_the_client(monkeypatch):
    """A ``…@group.calendar.google.com`` organizer is our own shared calendar.

    Calling it ``external_host`` would tell the AE their own team's calendar
    belongs to the client; ``not_owner`` is the honest answer.
    """
    event = _event(organizer={"email": "sales-team@group.calendar.google.com"})
    verdict = _one(event)["arming"]
    assert (verdict["state"], verdict["reason"]) == ("not_armable", "not_owner")


def test_a_started_sessions_stored_verdict_wins_over_the_precondition():
    """An armed row must never render as unchecked. The write path's verdict is
    the real one, and it is what tells the sweep armed-but-empty from
    never-armed."""

    class _Session:
        id = "sess-1"
        status = "in_progress"
        wrapped_at = None
        attrs = {
            "capture_arming": {
                "state": "armed",
                "reason": "armed_by_cubie",
                "detail": None,
            }
        }

    row = _one(_event(), now=DURING, sessions={EVENT: _Session()})
    assert row["capture_state"] == "in_progress"
    assert row["call_session_id"] == "sess-1"
    # Without the stored verdict this would be already_started.
    assert (row["arming"]["state"], row["arming"]["reason"]) == (
        "armed",
        "armed_by_cubie",
    )


def test_a_stored_verdict_outside_the_vocabulary_is_ignored_not_rendered():
    """A badge nobody can read is worse than the free precondition."""

    class _Session:
        id = "sess-2"
        status = "scheduled"
        wrapped_at = None
        attrs = {"capture_arming": {"state": "totally-armed", "reason": "vibes"}}

    row = _one(_event(), sessions={EVENT: _Session()})
    assert row["capture_state"] == "session_exists"
    assert row["arming"]["state"] in ARMING_STATES
    assert row["arming"]["reason"] in ARMING_REASONS
    assert (row["arming"]["state"], row["arming"]["reason"]) == (
        "unknown",
        "not_checked_yet",
    )


# ═══ 6. POST /api/calls/start — the calendar path arms, it does not create ══


class _FakeMeet:
    """``GoogleMeetClient`` minus Google. Records what it was asked to do."""

    armed: list[dict] = []
    created: list[dict] = []
    #: A result dict, or an exception INSTANCE to raise.
    outcome: object = None

    def __init__(self, user_email: str):
        self.user_email = user_email

    def arm_space_transcription(self, **kw) -> dict:
        _FakeMeet.armed.append(dict(kw))
        if isinstance(_FakeMeet.outcome, BaseException):
            raise _FakeMeet.outcome
        return _FakeMeet.outcome or {
            "space_name": CANON,
            "meeting_code": CODE,
            "state": "armed",
            "artifact_config_before": {
                "smartNotesConfig": {"autoSmartNotesGeneration": "ON"}
            },
            "artifact_config_after": {
                "transcriptionConfig": {"autoTranscriptionGeneration": "ON"},
                "smartNotesConfig": {"autoSmartNotesGeneration": "ON"},
            },
        }

    def create_space(self, **kw) -> dict:
        _FakeMeet.created.append(dict(kw))
        nth = len(_FakeMeet.created)
        return {
            "name": f"spaces/CREATED{nth}",
            "meetingCode": f"zzz-zzzz-z{nth:02d}",
            "meetingUri": f"https://meet.google.com/zzz-zzzz-z{nth:02d}",
            "config": {},
        }


@pytest.fixture
def meet(monkeypatch) -> type[_FakeMeet]:
    _FakeMeet.armed, _FakeMeet.created, _FakeMeet.outcome = [], [], None
    monkeypatch.setattr("rubik_core.tools.meet.GoogleMeetClient", _FakeMeet)
    monkeypatch.setattr(
        "backend.routers.calls._company_name", lambda _cid: "Acme Industries LLC"
    )
    return _FakeMeet


def _body(**extra) -> dict:
    body = {
        "hubspot_company_id": "123",
        "calendar_event_id": EVENT,
        "meet_meeting_code": CODE,
        "meet_uri": URI,
    }
    body.update(extra)
    return body


def test_a_calendar_start_arms_the_existing_space_and_creates_none(db, meet):
    """The calendar already made a space and the AE is joining THAT one. A
    second Meet link would leave the client sitting in the other meeting."""
    resp = client.post("/api/calls/start", json=_body())
    assert resp.status_code == 200, resp.text
    payload = resp.json()

    assert meet.created == [], "the calendar path must never create a space"
    assert meet.armed == [{"meeting_code": CODE}]

    assert payload["origin"] == "calendar"
    assert payload["meeting_code"] == CODE
    assert payload["meet_uri"] == URI
    assert payload["arming"] == {
        "state": "armed",
        "reason": "armed_by_cubie",
        "detail": None,
    }

    session = db.query(CallSession).one()
    assert session.origin == "calendar"
    assert session.calendar_event_id == EVENT
    assert session.hubspot_company_id == "123"
    assert session.attrs["capture_arming"]["reason"] == "armed_by_cubie"
    # The evidence that makes "Cubie turned my Gemini notes off" settleable.
    assert session.attrs["arming_evidence"]["artifact_config_before"] == {
        "smartNotesConfig": {"autoSmartNotesGeneration": "ON"}
    }


def test_a_confidential_calendar_call_is_not_armed_at_all(db, meet):
    """The confidential modal promises nothing is captured. Not armed — not
    armed-and-then-ignored."""
    resp = client.post("/api/calls/start", json=_body(capture_consent="suppressed"))
    assert resp.status_code == 200, resp.text

    assert meet.armed == [], "a suppressed call must not reach Google at all"
    assert meet.created == []

    verdict = resp.json()["arming"]
    assert verdict["state"] == "unknown"
    assert verdict["state"] != "armed"
    assert verdict["reason"] == "not_checked_yet"
    assert "confidential" in (verdict["detail"] or "")

    session = db.query(CallSession).one()
    assert session.capture_consent == "suppressed"
    assert session.attrs["capture_arming"]["state"] == "unknown"


def _meet_error(kind: str) -> Exception:
    from rubik_core.tools.meet import (
        MeetArtifactsUnsupported,
        MeetSpaceNotFound,
        MeetSpaceNotPatchable,
        MeetSpaceWriteDisabled,
    )

    return {
        "not_found": lambda: MeetSpaceNotFound("no space visible for this credential"),
        "not_patchable": lambda: MeetSpaceNotPatchable(
            "refused", subject=AE, space_name=CANON
        ),
        "unsupported": lambda: MeetArtifactsUnsupported("edition does not include it"),
        "write_disabled": lambda: MeetSpaceWriteDisabled("CCS_MEET_ENABLED=false"),
        "unclassified": lambda: RuntimeError("transport blew up"),
    }[kind]()


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        ("not_found", "external_host"),
        ("not_patchable", "not_owner"),
        ("unsupported", "arming_unsupported"),
        ("write_disabled", "write_disabled"),
        ("unclassified", "google_error"),
    ],
)
def test_every_arming_failure_is_still_a_started_call_with_a_named_reason(
    db, meet, kind, reason
):
    """A call Cubie cannot transcribe is still a call Cubie should know about.

    The company binding is the half that always works, and it is what lets the
    wrap-up write to the right CRM record without asking. Turning an arming
    failure into a 502 would throw the working half away at the moment the AE
    is trying to join a client call.
    """
    meet.outcome = _meet_error(kind)
    resp = client.post("/api/calls/start", json=_body())
    assert resp.status_code == 200, resp.text

    verdict = resp.json()["arming"]
    assert verdict["reason"] == reason
    assert verdict["state"] == "not_armable"
    assert verdict["state"] != "armed", "an unarmable row must never look armed"
    assert verdict["detail"], "a failure with no reason is the silent no-op"
    assert verdict["state"] in ARMING_STATES and verdict["reason"] in ARMING_REASONS

    session = db.query(CallSession).one()
    assert session.origin == "calendar"
    assert session.calendar_event_id == EVENT
    assert session.hubspot_company_id == "123", "the binding survives the failure"
    assert session.attrs["capture_arming"]["reason"] == reason
    assert session.attrs["arming_evidence"] is None, "nothing was armed, so no evidence"


def test_an_unusable_meeting_code_names_the_code_not_the_exception(db, meet):
    meet.outcome = ValueError("Not a Meet meeting code: 'spaces/'")
    resp = client.post("/api/calls/start", json=_body())
    assert resp.status_code == 200, resp.text
    verdict = resp.json()["arming"]
    assert verdict["reason"] == "lookup_failed"
    assert CODE in verdict["detail"]
    assert "ValueError" not in verdict["detail"]


def test_a_calendar_start_without_a_meeting_code_is_a_422(db, meet):
    """Never a silent fall-through to ``create_space``: that would hand the AE a
    SECOND Meet link for a meeting that already has one."""
    resp = client.post("/api/calls/start", json=_body(meet_meeting_code=None))
    assert resp.status_code == 422, resp.text
    assert meet.created == [] and meet.armed == []
    assert db.query(CallSession).count() == 0


def test_two_identical_posts_yield_one_session_and_no_500(db, meet):
    """Idempotent at the DATABASE on ``UNIQUE(ae_email, calendar_event_id)``.

    By the time an AE clicks twice the call is usually starting, and Meet cannot
    arm a conference already in progress — so the second attempt must not burn a
    quota'd write to replace one honest verdict with another.
    """
    first = client.post("/api/calls/start", json=_body())
    second = client.post("/api/calls/start", json=_body())

    assert (first.status_code, second.status_code) == (200, 200), second.text
    assert first.json()["call_session_id"] == second.json()["call_session_id"]
    assert db.query(CallSession).count() == 1
    assert len(meet.armed) == 1, "the second click must not re-arm"
    assert second.json()["arming"]["reason"] == "armed_by_cubie"


def test_a_second_occurrence_of_a_series_gets_its_own_session(db, meet):
    """One space is shared by a whole series, and ``meet_space_name`` is UNIQUE.

    The ROW matters more than the denormalised name: losing the session would
    lose the wrap-up, so the second occurrence keeps its row and the sweep
    correlates on the meeting code instead.
    """
    assert client.post("/api/calls/start", json=_body()).status_code == 200
    resp = client.post(
        "/api/calls/start", json=_body(calendar_event_id="evt_next_week")
    )
    assert resp.status_code == 200, resp.text

    sessions = db.query(CallSession).all()
    assert {s.calendar_event_id for s in sessions} == {EVENT, "evt_next_week"}
    # ``meet_space_name`` is UNIQUE, so only the first occurrence can hold it.
    assert [s.meet_space_name for s in sessions].count(CANON) == 1
    # Both still resolved to the same space, recorded off the unique column.
    assert {s.attrs["meet_space_name_resolved"] for s in sessions} == {CANON}


def test_an_adhoc_start_still_creates_a_space(db, meet):
    """The other origin is untouched: no ``calendar_event_id``, so Cubie makes
    its own space with ``artifactConfig`` set at creation."""
    resp = client.post("/api/calls/start", json={"hubspot_company_id": "123"})
    assert resp.status_code == 200, resp.text
    assert len(meet.created) == 1 and meet.armed == []
    payload = resp.json()
    assert payload["origin"] == CALL_ORIGIN_ASSISTANT
    assert payload["arming"] == {
        "state": "armed",
        "reason": "armed_by_cubie",
        "detail": None,
    }
    assert db.query(CallSession).one().calendar_event_id is None


# ═══ 7. The patch mask — only the transcription leaf, never the parent ══════
#
# Meet rejects ``updateMask="config.artifactConfig"`` (the parent message) with
# ``400 Invalid update mask provided``. The mask is one LEAF path per flag
# sent, so arming names only ``…transcriptionConfig.autoTranscriptionGeneration``
# and the host's recording and smart-notes settings are neither re-sent nor
# reset. These fakes cannot prove what Google accepts; the leaf mask was checked
# against a live tenant.


class _Exec:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class _Spaces:
    def __init__(self, space: dict):
        self.space = space
        self.gets: list[str] = []
        self.patches: list[dict] = []
        #: An exception INSTANCE to raise from ``get``/``patch`` instead.
        self.get_raises: BaseException | None = None
        self.patch_raises: BaseException | None = None

    def get(self, *, name):
        self.gets.append(name)
        if self.get_raises is not None:
            raise self.get_raises
        return _Exec(self.space)

    def patch(self, *, name, updateMask, body):  # noqa: N803 — Google's kwarg
        self.patches.append({"name": name, "updateMask": updateMask, "body": body})
        if self.patch_raises is not None:
            raise self.patch_raises
        # Echo the Space, as spaces.patch documents it does.
        return _Exec(
            {**self.space, "name": name, "config": dict(body.get("config") or {})}
        )


class _Service:
    def __init__(self, space: dict):
        self._spaces = _Spaces(space)

    def spaces(self):
        return self._spaces


def _meet_client(space: dict):
    """A real ``GoogleMeetClient`` wired to a fake Meet service.

    ``__new__`` skips the domain-wide-delegation build (the pattern
    ``test_meet_transcripts.py`` already uses), and setting ``_space_service``
    makes ``_space_write_service`` return early without touching credentials —
    it still checks ``CCS_MEET_ENABLED`` first, which the ``flags`` fixture has
    turned on.
    """
    from rubik_core.tools.meet import GoogleMeetClient

    m = GoogleMeetClient.__new__(GoogleMeetClient)
    m.user_email = AE
    service = _Service(space)
    m.service = service
    m._space_service = service
    return m, service.spaces()


def _space(**artifacts) -> dict:
    return {
        "name": CANON,
        "meetingCode": CODE,
        "meetingUri": URI,
        "config": {"artifactConfig": dict(artifacts)},
    }


TRANSCRIBE_MASK = (
    "config.artifactConfig.transcriptionConfig.autoTranscriptionGeneration"
)


def test_arming_patches_only_the_transcription_leaf():
    """Transcription flips ON; recording and smart notes are not in the request."""
    m, spaces = _meet_client(
        _space(
            transcriptionConfig={"autoTranscriptionGeneration": "OFF"},
            recordingConfig={"autoRecordingGeneration": "ON"},
            smartNotesConfig={"autoSmartNotesGeneration": "ON"},
        )
    )

    result = m.arm_space_transcription(meeting_code=CODE)

    # Looked up by the code alias, patched by the CANONICAL name that came back.
    assert spaces.gets == [f"spaces/{CODE}"]
    assert len(spaces.patches) == 1
    patch = spaces.patches[0]
    assert patch["name"] == CANON, "patching by meeting code is undocumented"
    assert patch["updateMask"] == TRANSCRIBE_MASK
    assert patch["updateMask"] != "config.artifactConfig", (
        "Meet answers the parent mask with 400 Invalid update mask provided"
    )

    sent = patch["body"]["config"]["artifactConfig"]
    assert sent == {"transcriptionConfig": {"autoTranscriptionGeneration": "ON"}}, (
        "the host's recording and smart notes are theirs — not re-sent, not reset"
    )

    assert result["state"] == "armed"
    assert result["space_name"] == CANON
    assert result["meeting_code"] == CODE
    assert result["artifact_config_before"]["recordingConfig"] == {
        "autoRecordingGeneration": "ON"
    }
    assert (
        result["artifact_config_after"]["transcriptionConfig"][
            "autoTranscriptionGeneration"
        ]
        == "ON"
    )


def test_the_mask_names_one_leaf_per_flag_sent():
    """Every sub-message in the body has its leaf in the mask, and nothing else.

    A flag left as ``None`` is out of both, so the space keeps its value.
    """
    m, spaces = _meet_client(_space())

    m.patch_space_artifacts(CANON, auto_transcribe=True)
    m.patch_space_artifacts(
        CANON, auto_transcribe=True, auto_record=False, auto_smart_notes=True
    )

    only, full = spaces.patches
    assert only["updateMask"] == TRANSCRIBE_MASK
    assert set(only["body"]["config"]["artifactConfig"]) == {"transcriptionConfig"}
    assert full["updateMask"].split(",") == [
        TRANSCRIBE_MASK,
        "config.artifactConfig.recordingConfig.autoRecordingGeneration",
        "config.artifactConfig.smartNotesConfig.autoSmartNotesGeneration",
    ]
    assert spaces.gets == [], "the raw patch reads nothing"


def test_a_thin_patch_echo_reports_what_the_space_now_holds():
    """No config in the echo → the config we read, with transcription flipped.

    The leaf mask left the other sub-messages alone, so they are reported as
    they were, not dropped.
    """
    m, spaces = _meet_client(
        _space(
            transcriptionConfig={"autoTranscriptionGeneration": "OFF"},
            smartNotesConfig={"autoSmartNotesGeneration": "ON"},
        )
    )
    spaces.patch = lambda **kw: (spaces.patches.append(kw), _Exec({}))[1]

    result = m.arm_space_transcription(meeting_code=CODE)

    assert result["artifact_config_after"] == {
        "transcriptionConfig": {"autoTranscriptionGeneration": "ON"},
        "smartNotesConfig": {"autoSmartNotesGeneration": "ON"},
    }


def test_an_invalid_mask_400_is_left_unclassified():
    """Our bug, not a verdict about the meeting: it surfaces as the HttpError,
    which the router files under ``google_error`` with the status."""
    from googleapiclient.errors import HttpError

    m, spaces = _meet_client(_space())
    spaces.patch_raises = _http_error(400, "Invalid update mask provided")
    with pytest.raises(HttpError):
        m.arm_space_transcription(meeting_code=CODE)


def test_transcription_already_on_sends_no_patch_at_all():
    """A host default, a previous run, or an earlier occurrence of the series.

    ``already_armed`` is a distinct state precisely so a quota'd write is not
    spent proving something that is already true.
    """
    m, spaces = _meet_client(
        _space(
            transcriptionConfig={"autoTranscriptionGeneration": "ON"},
            smartNotesConfig={"autoSmartNotesGeneration": "ON"},
        )
    )
    result = m.arm_space_transcription(meeting_code=CODE)

    assert spaces.patches == []
    assert result["state"] == "already_armed"
    assert result["space_name"] == CANON
    assert result["artifact_config_before"]["smartNotesConfig"] == {
        "autoSmartNotesGeneration": "ON"
    }


def test_arming_by_space_name_skips_the_code_validation():
    m, spaces = _meet_client(_space())
    m.arm_space_transcription(space_name=CANON)
    assert spaces.gets == [CANON]
    assert spaces.patches[0]["name"] == CANON


def test_arming_with_neither_argument_is_a_valueerror():
    m, _spaces = _meet_client(_space())
    with pytest.raises(ValueError):
        m.arm_space_transcription()


def _http_error(status: int, message: str):
    from googleapiclient.errors import HttpError

    class _Resp:
        def __init__(self, code):
            self.status = code
            self.reason = message

    body = json.dumps({"error": {"code": status, "message": message}}).encode()
    return HttpError(_Resp(status), body, uri="https://meet.googleapis.com/v2/spaces")


def test_a_404_on_the_lookup_is_a_foreign_or_expired_space():
    """The client-hosted-Meet answer. A 403 is NOT invented into a verdict."""
    from rubik_core.tools.meet import MeetSpaceNotFound

    m, spaces = _meet_client(_space())
    spaces.get_raises = _http_error(404, "Requested entity was not found.")
    with pytest.raises(MeetSpaceNotFound):
        m.arm_space_transcription(meeting_code=CODE)
    assert spaces.patches == []


def test_a_licence_flavoured_refusal_is_the_tenant_wide_edition_answer():
    """Classified by MESSAGE as well as status, the way the probe script does.

    A 403 naming a SCOPE is a delegation-grant problem and says nothing about
    the edition; a 400/403 naming the FEATURE or the LICENCE is tenant-wide and
    should be said once for the whole day, not once per row.
    """
    from rubik_core.tools.meet import MeetArtifactsUnsupported

    m, spaces = _meet_client(_space())
    spaces.patch_raises = _http_error(
        403, "Automatic transcription is not available for this Workspace edition."
    )
    with pytest.raises(MeetArtifactsUnsupported):
        m.arm_space_transcription(meeting_code=CODE)


def test_a_plain_403_on_the_patch_is_this_subject_being_refused():
    """Not a claim about who MAY patch a space they did not create — that is
    unverified on this tenant. A claim about what happened."""
    from rubik_core.tools.meet import MeetSpaceNotPatchable

    m, spaces = _meet_client(_space())
    spaces.patch_raises = _http_error(403, "The caller does not have permission")
    with pytest.raises(MeetSpaceNotPatchable) as caught:
        m.arm_space_transcription(meeting_code=CODE)
    assert caught.value.subject == AE
    assert caught.value.space_name == CANON


def test_a_scope_refusal_is_re_raised_and_never_filed_against_the_meeting():
    """A revoked grant is infrastructure. Reporting it as "the client hosts this
    Meet" would be a wrong reason, and nobody looks behind a wrong reason."""
    from googleapiclient.errors import HttpError

    m, spaces = _meet_client(_space())
    spaces.patch_raises = _http_error(
        403, "Request had insufficient authentication scopes."
    )
    with pytest.raises(HttpError):
        m.arm_space_transcription(meeting_code=CODE)


# ═══ 8. scan_day — an outage is never an empty day ══════════════════════════


class _FakeCalendar:
    """``GoogleCalendarClient``, minus Google."""

    def __init__(
        self,
        events: list[dict] | None = None,
        *,
        service: object = object(),
        tz: str | None = "America/Chicago",
        last_error: str | None = None,
        raise_on_list: BaseException | None = None,
    ):
        self.service = service
        self._events = events or []
        self._tz = tz
        self.init_error = None
        self.last_error = last_error
        self._raise = raise_on_list
        self.calls: list[dict] = []

    def get_calendar_timezone(self):
        return self._tz

    def get_upcoming_events(self, **kwargs):
        self.calls.append(dict(kwargs))
        if self._raise is not None:
            raise self._raise
        return list(self._events)


class _FakeHubSpot:
    def __init__(self, verdicts=None, *, raises: BaseException | None = None):
        self.token = "test-token"
        self.verdicts = verdicts or {}
        self.raises = raises
        self.hits: list[list[str]] = []

    def companies_for_domains(self, domains, *, properties=None):
        self.hits.append(list(domains))
        if self.raises is not None:
            raise self.raises
        return {d: self.verdicts.get(d, DomainMatch(domain=d)) for d in domains}


def _scan(cal, hubspot=None, **kw) -> dict:
    return scan_day(
        AE,
        local_date=date(2026, 8, 28),
        calendar_client=cal,
        hubspot_client=hubspot
        or _FakeHubSpot(
            {CLIENT_DOMAIN: DomainMatch(domain=CLIENT_DOMAIN, candidates=(_company(),))}
        ),
        now_utc=datetime(2026, 8, 28, 18, 56, tzinfo=timezone.utc),
        **kw,
    )


def test_a_normal_day_returns_the_documented_envelope(db):
    result = _scan(_FakeCalendar([_event()]), db=db)
    assert set(result) == {
        "calendar_available",
        "timezone",
        "local_date",
        "meetings",
        "script_catalogue",
    }
    assert result["calendar_available"] is True
    # Read and empty — nothing is approved in this database — never None,
    # which would say the lookup failed.
    assert result["script_catalogue"] == []
    assert result["timezone"] == "America/Chicago"
    assert result["local_date"] == "2026-08-28"
    assert [m["google_event_id"] for m in result["meetings"]] == [EVENT]
    assert result["meetings"][0]["company"]["hubspot_company_id"] == "123"


def test_the_day_window_handed_to_google_is_the_local_day(db):
    cal = _FakeCalendar([_event()])
    _scan(cal, db=db)
    assert cal.calls[0]["time_min"] == "2026-08-28T05:00:00Z"
    assert cal.calls[0]["time_max"] == "2026-08-29T05:00:00Z"
    assert cal.calls[0]["time_zone"] == "America/Chicago"


def test_a_pacific_ae_at_2330_local_is_scanned_for_the_day_they_are_living(db):
    """The travel bug, end to end: 06:30Z on the 29th is 23:30 on the 28th in LA."""
    cal = _FakeCalendar([], tz="America/Los_Angeles")
    result = scan_day(
        AE,
        db=db,
        calendar_client=cal,
        hubspot_client=_FakeHubSpot(),
        now_utc=datetime(2026, 8, 29, 6, 30, tzinfo=timezone.utc),
    )
    assert result["local_date"] == "2026-08-28", "not tomorrow's date"
    assert cal.calls[0]["time_min"] == "2026-08-28T07:00:00Z"
    assert cal.calls[0]["time_max"] == "2026-08-29T07:00:00Z"


def test_no_calendar_service_is_an_unreadable_day_not_an_empty_one(db):
    """``available: True`` with no meetings is a sentence the AE BELIEVES."""
    result = _scan(_FakeCalendar([_event()], service=None), db=db)
    assert result["calendar_available"] is False
    assert result["meetings"] == []
    assert result["local_date"] == "2026-08-28"
    assert result["timezone"], "the UI still needs a zone to render in"
    # Same envelope on the outage path, and nothing read: there are no rows to
    # show a checklist on.
    assert result["script_catalogue"] is None


def test_an_unbuildable_calendar_client_is_an_unreadable_day(db, monkeypatch):
    def _boom(**_kw):
        raise RuntimeError("no Workspace credentials")

    monkeypatch.setattr(
        "rubik_core.call_capture.calendar_scan.GoogleCalendarClient", _boom
    )
    result = scan_day(AE, local_date=date(2026, 8, 28), db=db)
    assert result["calendar_available"] is False
    assert result["meetings"] == []


def test_a_raising_events_list_is_an_unreadable_day(db):
    result = _scan(
        _FakeCalendar([_event()], raise_on_list=RuntimeError("502 from Google")), db=db
    )
    assert result["calendar_available"] is False
    assert result["meetings"] == []


def test_an_empty_list_plus_a_recorded_error_is_an_outage(db):
    """``get_upcoming_events`` swallows its own errors and returns ``[]``, so the
    error attributes are the only way to tell an outage from an empty calendar."""
    result = _scan(_FakeCalendar([], last_error="429 rate limited"), db=db)
    assert result["calendar_available"] is False
    assert result["meetings"] == []


def test_a_genuinely_empty_calendar_is_available_and_empty(db):
    result = _scan(_FakeCalendar([]), db=db)
    assert result["calendar_available"] is True
    assert result["meetings"] == []
    # Still read on a day with no rows, so None keeps meaning "could not look".
    assert result["script_catalogue"] == []


def test_a_hubspot_outage_still_renders_the_day_with_a_company_error(db):
    """The company binding failing does not cost the AE the row, the Meet link,
    or the ability to pick the company by hand."""
    result = _scan(
        _FakeCalendar([_event()]),
        _FakeHubSpot(raises=RuntimeError("hubspot unreachable")),
        db=db,
    )
    assert result["calendar_available"] is True
    row = result["meetings"][0]
    assert row["company"]["match"] == "error"
    assert row["company"]["match"] != "not_found"
    assert row["company"]["hubspot_company_id"] is None
    assert row["meet_uri"] == URI


def test_one_batched_hubspot_call_for_the_whole_day(db):
    """Two external calls for the page, not one lookup per row."""
    other = "brightpath.example.net"
    events = [
        _event(id="evt_a"),
        _event(
            id="evt_b",
            start={"dateTime": "2026-08-28T16:00:00-05:00"},
            end={"dateTime": "2026-08-28T16:30:00-05:00"},
            attendees=[
                _attendee(AE, self=True, organizer=True),
                _attendee(f"cfo@{other}"),
            ],
        ),
    ]
    hubspot = _FakeHubSpot()
    result = _scan(_FakeCalendar(events), hubspot, db=db)
    assert len(result["meetings"]) == 2
    assert len(hubspot.hits) == 1
    assert sorted(hubspot.hits[0]) == sorted([CLIENT_DOMAIN, other])


def test_an_existing_session_is_joined_by_the_occurrence_id(db):
    """The reason ``calendar_event_id`` is a real indexed column: nothing in this
    repo queries inside a JSON column, and this value is FOUND BY."""
    db.add(
        CallSession(
            id="sess-live",
            ae_email=AE,
            calendar_event_id=EVENT,
            origin="calendar",
            status="in_progress",
            capture_consent="granted",
            attrs={
                "crm_provenance": stamp_session_source(None, None, None),
                "capture_arming": {
                    "state": "armed",
                    "reason": "already_on",
                    "detail": None,
                },
            },
        )
    )
    db.commit()

    row = _scan(_FakeCalendar([_event()]), db=db)["meetings"][0]
    assert row["call_session_id"] == "sess-live"
    assert row["capture_state"] == "in_progress"
    assert (row["arming"]["state"], row["arming"]["reason"]) == ("armed", "already_on")


def test_scheduled_does_not_replay_a_revoked_mixed_pipeline_session(db, monkeypatch):
    db.add(
        CallSession(
            id="sess-revoked",
            ae_email=AE,
            calendar_event_id=EVENT,
            origin="calendar",
            status="in_progress",
            capture_consent="granted",
            hubspot_company_id="co-42",
            hubspot_deal_id="deal-outside",
            attrs={
                "company_name": "Former client",
                "crm_provenance": stamp_session_source("co-42", None, "deal-outside"),
                "capture_arming": {"state": "armed", "reason": "already_on"},
            },
        )
    )
    db.commit()
    monkeypatch.setattr(
        "rubik_core.tenancy.crm_visibility.visible_source_ids",
        lambda companies, contacts, deals: (companies, contacts, set()),
    )

    row = _scan(_FakeCalendar([_event()]), db=db)["meetings"][0]
    assert row["call_session_id"] is None
    assert row["capture_state"] == "not_started"
    assert row["arming"]["reason"] != "already_on"


def test_scheduled_does_not_replay_an_unproved_legacy_session(db):
    db.add(
        CallSession(
            id="sess-legacy",
            ae_email=AE,
            calendar_event_id=EVENT,
            origin="calendar",
            status="in_progress",
            capture_consent="granted",
            attrs={
                "company_name": "Former client",
                "capture_arming": {"state": "armed", "reason": "already_on"},
            },
        )
    )
    db.commit()

    row = _scan(_FakeCalendar([_event()]), db=db)["meetings"][0]
    assert row["call_session_id"] is None
    assert row["capture_state"] == "not_started"


def test_another_aes_session_on_the_same_meeting_is_not_mine(db):
    """Two colleagues are routinely on the same client call and each owns their
    own capture, so the join is on ``(ae_email, calendar_event_id)``."""
    db.add(
        CallSession(
            id="sess-theirs",
            ae_email=COLLEAGUE,
            calendar_event_id=EVENT,
            origin="calendar",
            status="in_progress",
            capture_consent="granted",
        )
    )
    db.commit()

    row = _scan(_FakeCalendar([_event()]), db=db)["meetings"][0]
    assert row["call_session_id"] is None
    assert row["capture_state"] == "not_started"


# ═══ 9. The domain cache — an error is never remembered as an answer ════════


def test_a_hubspot_error_and_positive_verdict_are_checked_again():
    """Caching a 429 as "no company for acmeindustries.alt" is exactly the silent
    wrong answer this whole design exists to prevent."""
    hubspot = _FakeHubSpot(
        {
            CLIENT_DOMAIN: DomainMatch(
                domain=CLIENT_DOMAIN, error="hubspot token rejected"
            )
        }
    )

    first = domain_match.resolve_domain_matches(
        [CLIENT_DOMAIN], ae_email=AE, client=hubspot
    )
    second = domain_match.resolve_domain_matches(
        [CLIENT_DOMAIN], ae_email=AE, client=hubspot
    )

    assert first[CLIENT_DOMAIN].error and second[CLIENT_DOMAIN].error
    assert len(hubspot.hits) == 2, "an error must be asked again, never replayed"

    # A positive verdict needs live rechecking: its company may have moved to
    # a pipeline outside this workspace since the first lookup.
    calendar_scan.clear_cache()
    good = _FakeHubSpot(
        {CLIENT_DOMAIN: DomainMatch(domain=CLIENT_DOMAIN, candidates=(_company(),))}
    )
    domain_match.resolve_domain_matches([CLIENT_DOMAIN], ae_email=AE, client=good)
    replay = domain_match.resolve_domain_matches(
        [CLIENT_DOMAIN], ae_email=AE, client=good
    )
    assert len(good.hits) == 2
    assert replay[CLIENT_DOMAIN].matched is True


def test_clear_cache_is_the_one_alias_and_it_actually_clears():
    """A second TTL dict would be the same mistake as a fifth blocklist."""
    hubspot = _FakeHubSpot(
        {CLIENT_DOMAIN: DomainMatch(domain=CLIENT_DOMAIN, candidates=(_company(),))}
    )
    domain_match.resolve_domain_matches([CLIENT_DOMAIN], ae_email=AE, client=hubspot)
    calendar_scan.clear_cache()
    domain_match.resolve_domain_matches([CLIENT_DOMAIN], ae_email=AE, client=hubspot)
    assert len(hubspot.hits) == 2

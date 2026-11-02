"""Cubie in Google Meet — ``GET /api/meet-addon/context`` (``backend/routers/meet_addon.py``).

The side panel inside Meet knows one thing: the meeting code Meet hands it. This
endpoint turns that code, for the signed-in AE only, into the call Cubie knows
about and the checklist the call has to cover. Every test below is one of four
sentences:

* **the right call** — a code on today's calendar resolves to that row, the
  live one wins when a code is shared, and last week's session on a recurring
  code is NOT this call;
* **the right products** — a captured call shows what is ATTACHED to its
  session (what the wrap-up grades), an uncaptured one what the AE picked for
  this client last time, and only approved checklists ever reach the panel;
* **never more sure than Cubie is** — an ambiguous company is not named, a
  calendar outage and a script-table failure are each reported rather than
  turned into "nothing here";
* **only your own** — another AE's session on the same code is invisible.

Nothing here talks to Google or HubSpot: ``calendar_scan.scan_day`` is replaced
by a fake day, patched **by string target** because other suites
``importlib.reload()`` modules mid-run.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth import get_current_user
from backend.main import app
from backend.tests._rosters import pin_rosters
from rubik_core.call_capture.calendar_scan import normalize_meeting_code
from rubik_core.call_capture.provenance import ATTR_KEY, stamp_session_source
from rubik_core.config import settings
from rubik_core.database.call_capture import CallSession
from rubik_core.database.call_scripts import (
    CallScript,
    CallSessionScript,
    CompanyProductDefault,
    ScriptItem,
)
from rubik_core.database.db import get_db
from rubik_core.database.schemas import UserProfile
from rubik_core.features import FeaturePolicy
from rubik_core.identifiers import CALL_ORIGIN_ASSISTANT

client = TestClient(app)

AE = "parker@acme.example"
OTHER_AE = "dakota@acme.example"
CODE = "abc-defg-hij"
COMPANY = "co-42"
EVENT = "evt_20260930T150000Z"
URL = "/api/meet-addon/context"

SCAN = "rubik_core.call_capture.calendar_scan.scan_day"

GUEST = "jane.doe@acmecorp.example"


# ── Scaffolding ──────────────────────────────────────────────────────────────


def _profile(email: str = AE, *, department: str = "Systems") -> UserProfile:
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name=email.split("@")[0].title(),
        department=department,
        roles=["user"],
        is_exec=False,
        managed_departments=[],
        created_at="2026-09-30T00:00:00",
        updated_at="2026-09-30T00:00:00",
    )


def _as(email: str = AE, *, department: str = "Systems") -> UserProfile:
    """Authenticate by overriding the dependency — never by sending a header."""
    profile = _profile(email, department=department)
    app.dependency_overrides[get_current_user] = lambda: profile
    return profile


def _narrow_meet_addon(monkeypatch) -> None:
    """The add-on ships with call capture's access: everyone. A case about
    someone OUTSIDE the audience installs the roster an admin would narrow it
    to (Systems), as Feature Access data."""
    pin_rosters(
        monkeypatch,
        {
            "sales_meet_addon": FeaturePolicy(
                departments=frozenset({"systems"}), admin_allowed=True
            )
        },
    )


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(
        "backend.routers.meet_addon.visible_source_ids", lambda *sources: sources
    )
    monkeypatch.setattr(
        "rubik_core.tenancy.crm_visibility.visible_source_ids", lambda *sources: sources
    )
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from rubik_core.database import eos  # noqa: F401 — registers eos_* tables
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr("rubik_core.database.db.SessionLocal", Session)
    s = Session()
    app.dependency_overrides[get_db] = lambda: s
    try:
        yield s
    finally:
        app.dependency_overrides.clear()
        s.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def switched_on(monkeypatch):
    """On, so a test exercises the RULE and not the flag."""
    monkeypatch.setattr(settings, "MEET_ADDON_ENABLED", True)


@pytest.fixture(autouse=True)
def _fresh_feature_cache():
    """The roster cache is process-wide; keep it from leaking across tests."""
    from rubik_core.features import clear_cache

    clear_cache()
    yield
    clear_cache()


def _guest(email: str = GUEST, **over) -> dict:
    """One invite row, in the shape ``calendar_scan._attendee_rows`` emits."""
    attendee = {
        "email": email,
        "display_name": None,
        "is_organizer": False,
        "is_self": False,
        "is_external": True,
        "is_optional": False,
        "response_status": "accepted",
    }
    attendee.update(over)
    return attendee


def _me() -> dict:
    return _guest(AE, is_self=True, is_external=False, is_organizer=True)


def _day(*rows: dict, available: bool = True):
    """A fake ``scan_day`` that returns these rows, and records who asked."""
    asked: list[str] = []

    def scan(ae_email, **_kw):
        asked.append(ae_email)
        return {
            "calendar_available": available,
            "timezone": "America/Chicago",
            "local_date": "2026-09-30",
            "meetings": list(rows),
        }

    scan.asked = asked
    return scan


def _row(**over) -> dict:
    """One Today's Calls row, in the shape ``calendar_scan.build_rows`` emits."""
    row = {
        "google_event_id": EVENT,
        "title": "Acme — discovery",
        "start_local": "Wednesday Sep 30, 10:00 AM CDT",
        "starts_in_minutes": -5,
        "is_live": True,
        "meet_uri": f"https://meet.google.com/{CODE}",
        "meeting_code": CODE,
        "company": {
            "match": "unique",
            "hubspot_company_id": COMPANY,
            "name": "Acme Industries",
            "domain": "acmecorp.example",
            "candidates": [],
        },
        "call_session_id": None,
        "capture_state": "not_started",
        "scripts": {"selected_product_keys": [], "available": [], "sections": []},
        "attendees": [_me(), _guest(display_name="Jane Doe")],
        "external_domains": ["acmecorp.example"],
        "attendees_truncated": False,
    }
    row.update(over)
    return row


def _script(db, product_key: str, name: str, facts: list[str], *, approved=True) -> str:
    script = CallScript(
        id=uuid.uuid4().hex,
        product_key=product_key,
        name=name,
        version=1,
        is_active=True,
        approved_by="harper@acme.example" if approved else None,
        approved_at=datetime.utcnow() if approved else None,
        source="manual",
    )
    for i, fact in enumerate(facts, start=1):
        script.items.append(
            ScriptItem(
                id=uuid.uuid4().hex,
                sequence=i,
                prompt_text=f"Ask about {fact.replace('_', ' ')}?",
                fact_key=fact,
                required=i != len(facts),
            )
        )
    db.add(script)
    db.commit()
    return script.id


def _session(db, *, ae=AE, code=CODE, created=None, **over) -> CallSession:
    session = CallSession(
        id=uuid.uuid4().hex,
        ae_email=ae,
        hubspot_company_id=COMPANY,
        meet_meeting_code=code,
        meet_uri=f"https://meet.google.com/{code}",
        capture_consent="granted",
        status="scheduled",
        origin="calendar",
        calendar_event_id=EVENT,
        attrs={"company_name": "Acme Industries"},
        created_at=created or datetime.utcnow(),
    )
    for key, value in over.items():
        setattr(session, key, value)
    session.attrs = {
        **(session.attrs or {}),
        ATTR_KEY: stamp_session_source(
            session.hubspot_company_id,
            session.hubspot_contact_id,
            session.hubspot_deal_id,
        ),
    }
    db.add(session)
    db.commit()
    return session


def _attach(db, session: CallSession, script_id: str, product_key: str) -> None:
    db.add(
        CallSessionScript(
            id=uuid.uuid4().hex,
            call_session_id=session.id,
            script_id=script_id,
            product_key=product_key,
        )
    )
    db.commit()


def _get(code: str = CODE):
    return client.get(URL, params={"meeting_code": code})


# ── The gates ────────────────────────────────────────────────────────────────


def test_outside_the_audience_is_403_whether_the_feature_is_on_or_off(db, monkeypatch):
    """The audience gate runs first, so the answer never reveals the switch."""
    _narrow_meet_addon(monkeypatch)
    monkeypatch.setattr(SCAN, _day())
    _as(department="Marketing")
    assert _get().status_code == 403
    monkeypatch.setattr(settings, "MEET_ADDON_ENABLED", False)
    assert _get().status_code == 403


def test_switched_off_is_404_for_the_audience(db, monkeypatch):
    monkeypatch.setattr(settings, "MEET_ADDON_ENABLED", False)
    monkeypatch.setattr(SCAN, _day())
    _as()
    assert _get().status_code == 404


def test_the_status_probe_answers_only_when_the_panel_would_work(db, monkeypatch):
    """Today's Calls asks this before pointing an AE at Meet; it must fail closed."""
    _narrow_meet_addon(monkeypatch)
    _as(department="Marketing")
    assert client.get("/api/meet-addon/status").status_code == 403
    _as()
    assert client.get("/api/meet-addon/status").json() == {"enabled": True}
    monkeypatch.setattr(settings, "MEET_ADDON_ENABLED", False)
    assert client.get("/api/meet-addon/status").status_code == 404


def test_a_value_that_cannot_be_a_meeting_code_is_422(db, monkeypatch):
    monkeypatch.setattr(SCAN, _day())
    _as()
    assert _get("not a code!").status_code == 422


def test_the_code_is_folded_the_same_way_the_calendar_rows_fold_it():
    """A casing mismatch would not raise — it would call a real client call
    "not one of yours". Both sides go through one function."""
    assert normalize_meeting_code("  ABC-DEFG-HIJ ") == CODE
    assert normalize_meeting_code("abc/defg") is None
    assert normalize_meeting_code("") is None
    assert normalize_meeting_code(None) is None


# ── The right call ───────────────────────────────────────────────────────────


def test_only_the_callers_own_calendar_is_read(db, monkeypatch):
    scan = _day(_row())
    monkeypatch.setattr(SCAN, scan)
    _as()
    assert _get().status_code == 200
    assert scan.asked == [AE]


def test_a_code_on_todays_calendar_resolves_to_that_call(db, monkeypatch):
    monkeypatch.setattr(SCAN, _day(_row(title="Acme — discovery")))
    _as()
    body = _get().json()
    assert body["match"] == "calendar"
    assert body["meeting_code"] == CODE
    assert body["call"]["title"] == "Acme — discovery"
    assert body["call"]["is_live"] is True
    assert body["call"]["call_session_id"] is None
    assert body["company"] == {
        "state": "suggested",
        "hubspot_company_id": COMPANY,
        "name": "Acme Industries",
        "domain": "acmecorp.example",
    }


def test_the_live_call_wins_when_two_invites_share_one_meet_link(db, monkeypatch):
    later = _row(
        google_event_id="evt_later",
        title="Later call",
        is_live=False,
        starts_in_minutes=180,
    )
    live = _row(
        google_event_id="evt_live", title="The call happening now", is_live=True
    )
    monkeypatch.setattr(SCAN, _day(later, live))
    _as()
    body = _get().json()
    assert body["call"]["title"] == "The call happening now"
    assert body["calls_sharing_code"] == 2


def test_without_a_live_call_the_nearest_start_wins(db, monkeypatch):
    morning = _row(
        google_event_id="evt_am", title="Morning", is_live=False, starts_in_minutes=-240
    )
    soon = _row(
        google_event_id="evt_soon",
        title="Starting soon",
        is_live=False,
        starts_in_minutes=4,
    )
    monkeypatch.setattr(SCAN, _day(morning, soon))
    _as()
    assert _get().json()["call"]["title"] == "Starting soon"


def test_a_code_nobody_knows_is_said_as_such(db, monkeypatch):
    monkeypatch.setattr(SCAN, _day(_row(meeting_code="zzz-zzzz-zzz")))
    _as()
    body = _get().json()
    assert body["match"] == "none"
    assert body["call"] is None
    assert body["company"] is None
    assert body["selected"] == []
    assert body["products_source"] == "none"


def test_an_adhoc_session_on_the_code_resolves_without_a_calendar_row(db, monkeypatch):
    session = _session(db, origin=CALL_ORIGIN_ASSISTANT, calendar_event_id=None)
    monkeypatch.setattr(SCAN, _day())
    _as()
    body = _get().json()
    assert body["match"] == "session"
    assert body["call"]["call_session_id"] == session.id
    assert body["company"]["state"] == "confirmed"
    assert body["company"]["name"] == "Acme Industries"


def test_last_weeks_session_on_a_recurring_code_is_not_this_call(db, monkeypatch):
    """One Meet code serves every occurrence of a series; a week-old session on
    it must not hand this week's panel last week's wrap-up."""
    _session(db, created=datetime.utcnow() - timedelta(days=7))
    monkeypatch.setattr(SCAN, _day())
    _as()
    assert _get().json()["match"] == "none"


def test_another_aes_session_on_the_same_code_is_invisible(db, monkeypatch):
    _session(db, ae=OTHER_AE, origin=CALL_ORIGIN_ASSISTANT, calendar_event_id=None)
    monkeypatch.setattr(SCAN, _day())
    _as()
    body = _get().json()
    assert body["match"] == "none"
    assert body["call"] is None


def test_a_row_pointing_at_someone_elses_session_does_not_leak_it(db, monkeypatch):
    """Defence in depth: the id on a row is re-checked against the caller."""
    theirs = _session(db, ae=OTHER_AE)
    monkeypatch.setattr(SCAN, _day(_row(call_session_id=theirs.id)))
    _as()
    body = _get().json()
    assert body["match"] == "calendar"
    assert body["call"]["call_session_id"] is None
    assert body["company"]["state"] == "suggested"


# ── The right products ───────────────────────────────────────────────────────


def test_a_captured_call_shows_what_is_attached_not_what_was_remembered(
    db, monkeypatch
):
    """The attached products are what the wrap-up grades; the panel must match."""
    plan = _script(
        db, "implementation_plan", "Implementation Plan", ["sites", "hours", "rate"]
    )
    _script(db, "staff_training", "Staff Training", ["trainees"])
    session = _session(db)
    _attach(db, session, plan, "implementation_plan")
    row = _row(
        call_session_id=session.id,
        capture_state="session_exists",
        scripts={
            "selected_product_keys": ["staff_training"],
            "available": [],
            "sections": [],
        },
    )
    monkeypatch.setattr(SCAN, _day(row))
    _as()
    body = _get().json()
    assert body["products_source"] == "call"
    assert body["selected"] == [
        {
            "product_key": "implementation_plan",
            "name": "Implementation Plan",
            "has_checklist": True,
        }
    ]
    assert body["call"]["call_session_id"] == session.id
    assert body["call"]["capture_state"] == "session_exists"
    assert body["company"]["state"] == "confirmed"


def test_a_captured_call_with_nothing_attached_falls_back_to_the_remembered_pick(
    db, monkeypatch
):
    """Captured from the ad-hoc form (which takes no products) or from a row
    whose checklist block failed: nothing is attached, but the AE's last pick
    for the BOUND company is still the right checklist to read — labelled as
    remembered, never as what the wrap-up will grade."""
    _script(db, "implementation_plan", "Implementation Plan", ["sites", "hours"])
    session = _session(db, origin=CALL_ORIGIN_ASSISTANT, calendar_event_id=None)
    db.add(
        CompanyProductDefault(
            id=uuid.uuid4().hex,
            ae_email=AE,
            hubspot_company_id=COMPANY,
            product_keys=["implementation_plan"],
        )
    )
    db.commit()
    monkeypatch.setattr(SCAN, _day())
    _as()
    body = _get().json()
    assert body["match"] == "session"
    assert body["call"]["call_session_id"] == session.id
    assert body["products_source"] == "remembered"
    assert [s["product_key"] for s in body["selected"]] == ["implementation_plan"]


def test_an_uncaptured_call_uses_the_pick_remembered_for_this_client(db, monkeypatch):
    _script(db, "implementation_plan", "Implementation Plan", ["sites", "hours"])
    row = _row(
        scripts={
            "selected_product_keys": ["implementation_plan", "team_training"],
            "available": [],
            "sections": [],
        }
    )
    monkeypatch.setattr(SCAN, _day(row))
    _as()
    body = _get().json()
    assert body["products_source"] == "remembered"
    # A remembered product with no live checklist stays selected, labelled from
    # its key — "not written yet" is a different fact from "not on this call".
    assert body["selected"] == [
        {
            "product_key": "implementation_plan",
            "name": "Implementation Plan",
            "has_checklist": True,
        },
        {
            "product_key": "team_training",
            "name": "Team training",
            "has_checklist": False,
        },
    ]


def test_the_catalogue_carries_every_approved_checklist_and_no_draft(db, monkeypatch):
    _script(
        db, "implementation_plan", "Implementation Plan", ["sites", "hours", "rate"]
    )
    _script(db, "change_review", "Change Review", ["scope"])
    _script(db, "draft_only", "Draft Only", ["x"], approved=False)
    monkeypatch.setattr(SCAN, _day())
    _as()
    body = _get().json()
    assert body["catalogue_available"] is True
    assert [s["product_key"] for s in body["catalogue"]] == [
        "change_review",
        "implementation_plan",
    ]
    plan = body["catalogue"][1]
    assert [i["fact_key"] for i in plan["items"]] == ["sites", "hours", "rate"]
    # Required is the default; only the last seeded item is optional.
    assert [i["required"] for i in plan["items"]] == [True, True, False]


def test_a_confidential_call_is_flagged_so_the_panel_can_say_so(db, monkeypatch):
    session = _session(db, capture_consent="suppressed")
    monkeypatch.setattr(
        SCAN, _day(_row(call_session_id=session.id, capture_state="session_exists"))
    )
    _as()
    assert _get().json()["call"]["confidential"] is True


# ── Never more sure than Cubie is ────────────────────────────────────────────


def test_an_ambiguous_company_is_never_named(db, monkeypatch):
    row = _row(
        company={
            "match": "ambiguous",
            "hubspot_company_id": None,
            "name": None,
            "domain": "acmecorp.example",
            "candidates": [
                {
                    "hubspot_company_id": "co-1",
                    "name": "Acme A",
                    "domain": "acmecorp.example",
                },
                {
                    "hubspot_company_id": "co-2",
                    "name": "Acme B",
                    "domain": "acmecorp.example",
                },
            ],
        }
    )
    monkeypatch.setattr(SCAN, _day(row))
    _as()
    company = _get().json()["company"]
    assert company["state"] == "uncertain"
    assert company["name"] is None and company["hubspot_company_id"] is None
    assert company["reason"] == "ambiguous"


def test_a_calendar_outage_is_reported_not_turned_into_nothing(db, monkeypatch):
    monkeypatch.setattr(SCAN, _day(available=False))
    _as()
    body = _get().json()
    assert body["calendar_available"] is False
    assert body["match"] == "none"


def test_a_calendar_outage_still_finds_a_call_captured_today(db, monkeypatch):
    session = _session(db)
    monkeypatch.setattr(SCAN, _day(available=False))
    _as()
    body = _get().json()
    assert body["calendar_available"] is False
    assert body["match"] == "session"
    assert body["call"]["call_session_id"] == session.id


def test_revoked_session_company_is_not_returned_or_used_for_picks(db, monkeypatch):
    session = _session(db)
    monkeypatch.setattr(SCAN, _day())
    monkeypatch.setattr(
        "backend.routers.meet_addon.visible_source_ids",
        lambda companies, contacts, deals: (set(), contacts, deals),
    )
    monkeypatch.setattr(
        "rubik_core.tenancy.crm_visibility.visible_source_ids",
        lambda companies, contacts, deals: (set(), contacts, deals),
    )
    _as()
    body = _get().json()
    assert body["match"] == "none"
    assert body["call"] is None
    assert body["company"] is None
    assert body["selected"] == []
    assert session.id not in str(body)


def test_revoked_calendar_company_drops_suggested_name_and_remembered_picks(
    db, monkeypatch
):
    row = _row(scripts={"selected_product_keys": ["private-product"]})
    monkeypatch.setattr(SCAN, _day(row))
    monkeypatch.setattr(
        "backend.routers.meet_addon.visible_source_ids",
        lambda companies, contacts, deals: (set(), contacts, deals),
    )
    _as()
    body = _get().json()
    assert body["company"]["state"] == "uncertain"
    assert body["company"]["hubspot_company_id"] is None
    assert body["company"]["name"] is None
    assert body["selected"] == []


def test_a_scan_that_raises_is_an_outage_not_a_500(db, monkeypatch):
    def boom(*_a, **_kw):
        raise RuntimeError("calendar exploded")

    monkeypatch.setattr(SCAN, boom)
    _as()
    response = _get()
    assert response.status_code == 200
    assert response.json()["calendar_available"] is False


def test_an_unreadable_script_table_is_reported_not_hidden(db, monkeypatch):
    def boom(_db):
        raise RuntimeError("call_scripts is unreadable")

    monkeypatch.setattr(
        "rubik_core.call_capture.calendar_scan._live_script_payloads", boom
    )
    monkeypatch.setattr(SCAN, _day(_row()))
    _as()
    response = _get()
    assert response.status_code == 200
    body = response.json()
    assert body["catalogue_available"] is False
    assert body["catalogue"] == []
    assert body["match"] == "calendar"


def test_a_swallowed_db_failure_hands_the_next_read_a_live_transaction(db, monkeypatch):
    """On Postgres a failed statement aborts the transaction, and every later
    read on the session raises — turning a reported outage into a 500. SQLite
    cannot show that, so pin the rollback itself: once after the scan (which
    swallows its own failures) and once after the catalogue read fails."""
    rollbacks: list[int] = []
    real_rollback = db.rollback

    def spy():
        rollbacks.append(1)
        real_rollback()

    monkeypatch.setattr(db, "rollback", spy)

    def boom(_db):
        raise RuntimeError("canceling statement due to statement timeout")

    monkeypatch.setattr(
        "rubik_core.call_capture.calendar_scan._live_script_payloads", boom
    )
    monkeypatch.setattr(SCAN, _day(_row()))
    _as()
    assert _get().status_code == 200
    assert len(rollbacks) >= 2

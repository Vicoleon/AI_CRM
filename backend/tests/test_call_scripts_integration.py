"""Call Scripts, end to end — authoring form → calendar row → start → wrap-up.

The sibling suites each own one layer: ``test_call_scripts_router.py`` owns the
authoring rules, ``test_call_scripts_engine.py`` owns resolution and coverage,
``test_call_capture_calendar.py`` owns ``scan_day``'s outage behaviour, and
``test_call_capture_approve.py`` owns the wrap-up's gaps block. Every one of
them stubs the layer next to it. This file stubs NOTHING between them: a script
is authored through ``POST /api/scripts``, signed through ``POST
/{id}/approve``, picked up by ``GET /api/calls/scheduled``, attached by ``POST
/api/calls/start`` and reported by ``GET /api/calls/{id}/wrapup``, over one
SQLite database, through the real gates. Only Google and HubSpot are faked, at
their client factories, by string target.

That is the point. The feature rests on one fact: what is not asked on the
call is not in the record, and no automation afterwards can recover it. Each
layer can be individually correct and the chain still deliver nothing — an
approved script that never reaches the row, a pick
that never becomes an attachment, an attachment the wrap-up never reads — and
every one of those failures is SILENT. The panel renders, the wrap-up renders,
the AE is told their call was complete.

Five seams are pinned here, and none of them is visible from inside one layer:

1. **Approval is what crosses the boundary.** A draft authored through the form
   must not appear on a calendar row, and signing it must make it appear —
   through two different code paths (``scripts._live_script_payloads`` for the
   catalogue, ``scripts.active_script_for`` for the editor) that read the same
   three columns and could drift.
2. **Multi-product survives every hop.** Two products picked on the row become
   two attachments, two panel sections and two INDEPENDENT coverages.
   Lead↔deal is 1:1, so each product belongs on its own lead, and a merged
   verdict is wrong for both products and hides the one nobody asked about.
3. **The checklist never costs the day or the call.** A failing script lookup
   yields ``scripts: null`` with every meeting still rendered, and an unknown or
   unapproved product key is skipped at ``POST /start`` rather than 422'ing a
   call that is about to begin.
4. **The AE is graded on what they were shown.** A required fact the call never
   surfaced comes back as the QUESTION to ask, and a product discussed with no
   checklist loaded comes back as a split to make.
5. **A call with no checklist is unchanged.** Which is every call in the product
   until Indigo and Harper have authored content — the tables ship EMPTY.

Nothing here seeds company content. A sample script is a SHAPE for
the fixtures and is authored through the API in every test that needs it,
because the feature's promise — a script author adds a product script
through a form, with no deploy — is only proved by going through the door.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth import get_current_user
from backend.main import app
from rubik_core.config import settings
from rubik_core.database.call_capture import (
    CallExtraction,
    CallExtractionItem,
    CallSession,
    CallTranscriptEntry,
)
from rubik_core.database.call_scripts import CallScript, CallSessionScript
from rubik_core.database.db import get_db
from rubik_core.database.schemas import UserProfile
from rubik_core.retrieval.hubspot_api import CompanyMatch, DomainMatch
from rubik_core.tenancy.hubspot_config import HubSpotConnectorConfig

client = TestClient(app)

# As in test_call_scripts_router.py: the ``db`` fixture narrows Call Capture and
# the supervisor (authoring) roster to Systems, as an admin would in Feature
# Access (``_install_systems_rosters``), so everybody who has to REACH an
# endpoint is a **Systems** user standing in for the role their name gives them.
# Read "Harper the script author".
AUTHOR = "harper@acme.example"
AE = "parker@acme.example"

CLIENT_A = "vp@acmeindustries.alt"
DOMAIN_A = "acmeindustries.alt"
CO_A = "co-acme"
EVENT_A = "evt_20260828T190000Z"
CODE_A = "abc-defg-hij"
URI_A = f"https://meet.google.com/{CODE_A}"

CLIENT_B = "cfo@brightpath.example.net"
DOMAIN_B = "brightpath.example.net"
CO_B = "co-brightpath"
EVENT_B = "evt_20260828T210000Z"
CODE_B = "klm-nopq-rst"
URI_B = f"https://meet.google.com/{CODE_B}"

SPACE = "spaces/AAAA1111"
QUOTE = "About sixty hours each, and we'd want to start in January."

#: A sample script: the five facts an author would otherwise reverse-engineer
#: out of a call summary to build an SOW. A SHAPE for the fixtures, authored
#: through the API every time; nothing seeds it.
PLAN_ITEMS = [
    {
        "fact_key": "discovery_workshop",
        "prompt_text": "Do you need a discovery workshop first?",
        "required": True,
    },
    {
        "fact_key": "site_count",
        "prompt_text": "How many sites are in scope?",
        "required": True,
    },
    {
        "fact_key": "hours_per_site",
        "prompt_text": "How many hours per site?",
        "required": True,
        "help_text": "Harper needs this to price the SOW.",
    },
    {
        "fact_key": "rate",
        "prompt_text": "What rate did we quote?",
        "required": True,
    },
    {
        "fact_key": "commencement_date",
        "prompt_text": "When would you want to start?",
        "required": False,
    },
]

#: A second product, so "two products on one call" is a real second checklist
#: with its own facts rather than the same one twice. No fact key is shared with
#: PLAN_ITEMS: a shared key would make an independent-coverage assertion pass for
#: the wrong reason.
TRAINING_ITEMS = [
    {
        "fact_key": "trainee_count",
        "prompt_text": "How many trainees are in scope?",
        "required": True,
    },
    {
        "fact_key": "session_frequency",
        "prompt_text": "How often do they want training sessions?",
        "required": True,
    },
]


# ── Identity ─────────────────────────────────────────────────────────────────


def _profile(email: str, *, department: str = "Systems") -> UserProfile:
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name=email.split("@")[0].title(),
        department=department,
        roles=["user"],
        is_exec=False,
        managed_departments=[],
        created_at="2026-08-28T00:00:00",
        updated_at="2026-08-28T00:00:00",
    )


def _as(email: str, *, department: str = "Systems") -> UserProfile:
    """Authenticate by overriding the dependency — never by sending a header."""
    profile = _profile(email, department=department)
    app.dependency_overrides[get_current_user] = lambda: profile
    return profile


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _install_systems_rosters(db) -> None:
    """The narrowed configuration these tests exercise, written the way an admin
    would in Admin → Feature Access: Call Capture and the supervisor (authoring)
    roster both Systems-only. The shipped defaults — Call Capture open to
    everyone, the supervisor roster admins only — are pinned in
    test_default_access.py and test_sales_kaizen_access.py §12."""
    from rubik_core.features import FeaturePolicy, clear_cache, save_policy

    systems = FeaturePolicy(departments=frozenset({"systems"}), admin_allowed=True)
    for key in ("sales_call_capture", "sales_cockpit_supervisor"):
        save_policy(db, key, systems, actor_email="test@acme.example")
    clear_cache()


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from rubik_core.database import eos  # noqa: F401 — registers the eos_* tables
    from rubik_core.database.models import Base

    # models.py's last-line imports are what register these tables; dropping a
    # name there does not fail loudly, create_all simply stops seeing them.
    for table in ("call_scripts", "call_script_items", "call_session_scripts"):
        assert table in Base.metadata.tables, table
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr("rubik_core.database.db.SessionLocal", Session)
    session = Session()
    _install_systems_rosters(session)
    app.dependency_overrides[get_db] = lambda: session
    try:
        yield session
    finally:
        app.dependency_overrides.clear()
        session.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def flags(monkeypatch, set_test_workspace_domains):
    """Both switches on, so a test exercises the RULE and not the flag.

    The control workspace domain matters to the calendar scan: ``own_domains``
    decides which attendee is external, and without it every fixture meeting is
    internal-only and no row renders at all.
    """
    monkeypatch.setattr(settings, "CCS_ENABLED", True)
    monkeypatch.setattr(settings, "CCS_MEET_ENABLED", True)
    # Authoring uses the Nudge Worklist feature, whose platform ceiling now
    # participates in feature_allows as well as its workspace roster.
    monkeypatch.setattr(settings, "COCKPIT_ENABLED", True)
    set_test_workspace_domains("acme.example")


@pytest.fixture(autouse=True)
def no_company_lookup(monkeypatch):
    """``_company_name`` is a nicety that costs a live HubSpot read. Stub it."""
    monkeypatch.setattr("backend.routers.calls._company_name", lambda _cid: "Acme")


@pytest.fixture(autouse=True)
def owned_crm_companies(monkeypatch):
    """The two calendar companies have deals in this workspace's pipeline.

    Start and wrap-up must still pass the real CRM provenance check. Only the
    external HubSpot client and the workspace connector config are faked.
    """
    from rubik_core.retrieval import hubspot_api
    from rubik_core.tenancy import hubspot_config

    monkeypatch.setattr(
        hubspot_config,
        "get_hubspot_config",
        lambda: HubSpotConnectorConfig(pipeline_ids=("fixture-pipeline",)),
    )

    class OwnedHubSpot:
        def fully_owned_deal_associations(self, object_type, ids):
            assert object_type == "companies"
            return {
                company_id: ["deal-in-fixture-pipeline"]
                for company_id in ids
                if company_id in {CO_A, CO_B}
            }

    monkeypatch.setattr(hubspot_api, "HubSpotAPIClient", OwnedHubSpot)


class _FakeCalendar:
    """``GoogleCalendarClient``, minus Google."""

    def __init__(self, events: list[dict]):
        self.service = object()
        self.init_error = None
        self.last_error = None
        self._events = events

    def get_calendar_timezone(self) -> str:
        return "America/Chicago"

    def get_upcoming_events(self, **_kwargs) -> list[dict]:
        return list(self._events)


def _attendee(email: str, **over) -> dict:
    row = {"email": email, "responseStatus": "accepted"}
    row.update(over)
    return row


def _event(event_id: str, client_email: str, uri: str, hour: str) -> dict:
    """A capturable client call: timed, Meet-linked, AE-organized, one external."""
    return {
        "id": event_id,
        "summary": "Acme / client - scope",
        "start": {
            "dateTime": f"2026-08-28T{hour}:00:00-05:00",
            "timeZone": "America/Chicago",
        },
        "end": {"dateTime": f"2026-08-28T{hour}:30:00-05:00"},
        "organizer": {"email": AE, "self": True},
        "hangoutLink": uri,
        "attendees": [
            _attendee(AE, self=True, organizer=True),
            _attendee(client_email, displayName="Val Prospect"),
        ],
    }


DAY = [
    _event(EVENT_A, CLIENT_A, URI_A, "14"),
    _event(EVENT_B, CLIENT_B, URI_B, "16"),
]

MATCHES = {
    DOMAIN_A: DomainMatch(
        domain=DOMAIN_A,
        candidates=(
            CompanyMatch(
                hubspot_company_id=CO_A,
                name="Acme Industries LLC",
                domain=DOMAIN_A,
                lifecyclestage="customer",
            ),
        ),
    ),
    DOMAIN_B: DomainMatch(
        domain=DOMAIN_B,
        candidates=(
            CompanyMatch(
                hubspot_company_id=CO_B,
                name="Brightpath Logistics",
                domain=DOMAIN_B,
                lifecyclestage="lead",
            ),
        ),
    ),
}


@pytest.fixture(autouse=True)
def calendar(monkeypatch):
    """Two bound client meetings, and no Google or HubSpot on the wire.

    Patched by STRING TARGET, per the repo convention: sibling suites
    ``importlib.reload()`` modules mid-run and an object-target patch goes stale
    in a full-suite run.
    """
    monkeypatch.setattr(
        "rubik_core.call_capture.calendar_scan.GoogleCalendarClient",
        lambda **_kw: _FakeCalendar(DAY),
    )
    monkeypatch.setattr(
        "rubik_core.call_capture.calendar_scan.resolve_domain_matches",
        lambda domains, **_kw: {d: MATCHES[d] for d in domains if d in MATCHES},
    )


class _FakeMeet:
    """``GoogleMeetClient``, minus Google. Records what it was asked to do."""

    armed: list[dict] = []
    created: list[dict] = []

    def __init__(self, user_email: str):
        self.user_email = user_email

    def arm_space_transcription(self, **kw) -> dict:
        _FakeMeet.armed.append(dict(kw))
        return {
            "space_name": f"{SPACE}-{len(_FakeMeet.armed)}",
            "meeting_code": kw.get("meeting_code"),
            "state": "armed",
            "artifact_config_before": {},
            "artifact_config_after": {},
        }

    def create_space(self, **kw) -> dict:  # pragma: no cover — never reached
        _FakeMeet.created.append(dict(kw))
        raise AssertionError("the calendar path must never create a space")


@pytest.fixture(autouse=True)
def meet(monkeypatch) -> type[_FakeMeet]:
    _FakeMeet.armed = []
    _FakeMeet.created = []
    monkeypatch.setattr("rubik_core.tools.meet.GoogleMeetClient", _FakeMeet)
    return _FakeMeet


# ── Helpers that go through the API, never around it ─────────────────────────


def _draft(product_key: str, name: str, items: list[dict]) -> str:
    """Author a draft as Harper would — through the form. Returns its id."""
    _as(AUTHOR)
    res = client.post(
        "/api/scripts", json={"product_key": product_key, "name": name, "items": items}
    )
    assert res.status_code == 201, res.text
    return res.json()["script_id"]


def _sign(script_id: str) -> dict:
    _as(AUTHOR)
    res = client.post(f"/api/scripts/{script_id}/approve")
    assert res.status_code == 200, res.text
    return res.json()


def _publish(product_key: str, name: str, items: list[dict]) -> str:
    """Draft AND sign — the two-step every product goes through to reach an AE."""
    script_id = _draft(product_key, name, items)
    _sign(script_id)
    return script_id


def _day() -> dict:
    """The whole ``GET /scheduled`` envelope, as the AE's screen receives it."""
    _as(AE)
    res = client.get("/api/calls/scheduled")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["calendar_available"] is True, body
    return body


def _scheduled() -> list[dict]:
    return _day()["meetings"]


def _catalogue() -> list[dict] | None:
    return _day()["script_catalogue"]


def _row(event_id: str = EVENT_A) -> dict:
    rows = {row["google_event_id"]: row for row in _scheduled()}
    assert event_id in rows, sorted(rows)
    return rows[event_id]


def _start(
    *,
    event_id: str = EVENT_A,
    code: str = CODE_A,
    uri: str = URI_A,
    company: str = CO_A,
    **extra,
) -> dict:
    _as(AE)
    body = {
        "hubspot_company_id": company,
        "calendar_event_id": event_id,
        "meet_meeting_code": code,
        "meet_uri": uri,
    }
    body.update(extra)
    res = client.post("/api/calls/start", json=body)
    assert res.status_code == 200, res.text
    return res.json()


def _wrapup(session_id: str) -> dict:
    _as(AE)
    res = client.get(f"/api/calls/{session_id}/wrapup")
    assert res.status_code == 200, res.text
    return res.json()


def _extraction(db, session_id: str, *, facts: list[dict], leads: list[dict] = ()):
    """The extraction run's output, written the way ``extraction.py`` writes it.

    Script facts live in ``raw_output["script_coverage"]`` and not in
    ``call_extraction_items`` — that table's ``kind`` is CHECK-constrained and a
    script fact is not a proposed HubSpot write, so there is nothing to approve.
    Lead candidates ARE rows, and their ``payload["product"]`` is the signal
    ``unscripted_products`` reads.
    """
    extraction = CallExtraction(
        id=uuid.uuid4().hex,
        call_session_id=session_id,
        model="gemini-test",
        prompt_version="ccs-extract-v1",
        schema_version="1",
        raw_output={"script_coverage": list(facts)},
    )
    db.add(extraction)
    db.flush()
    entry_id = uuid.uuid4().hex
    db.add(
        CallTranscriptEntry(
            id=entry_id,
            call_session_id=session_id,
            source="meet",
            external_id=f"ext-{entry_id[:6]}",
            speaker_name="Client",
            text=QUOTE,
            sequence=1,
        )
    )
    for lead in leads:
        db.add(
            CallExtractionItem(
                id=uuid.uuid4().hex,
                extraction_id=extraction.id,
                call_session_id=session_id,
                kind="lead",
                proposed_value=lead.get("name", "Acme - something"),
                payload={"product": lead["product"]},
                evidence_text=QUOTE,
                evidence_entry_id=entry_id,
                confidence=0.9,
                status="proposed",
            )
        )
    db.commit()
    return extraction


def _fact(fact_key: str, value: str, product_key: str = "implementation_plan") -> dict:
    return {
        "fact_key": fact_key,
        "product_key": product_key,
        "value": value,
        "evidence_text": QUOTE,
        "evidence_entry_id": None,
        "confidence": 0.9,
    }


# ═══ 1. Authoring reaches the calendar row only after a human signs ══════════


def test_a_draft_never_reaches_a_calendar_row_and_approving_it_does(db):
    """Seam 1, in the order it actually happens.

    The design rule is explicit — *"Machine-drafted, human-approved… Do not ship
    an AI-generated script as authoritative"* — and the failure it names is silent:
    the panel renders, the questions look plausible, and the first sign of
    trouble is an SOW built on a fact nobody at the company agreed to ask for. Two
    different readers answer "is this live?" (the calendar catalogue and the
    editor's own resolver) and they must not disagree, which is why both are
    asserted at each stage.
    """
    script_id = _draft("implementation_plan", "Implementation Plan", PLAN_ITEMS)

    row = _row()
    assert row["scripts"] is not None, "a lookup failure, not an empty catalogue"
    assert row["scripts"]["available"] == [], "an unsigned script reached an AE"
    assert row["scripts"]["sections"] == []
    # The day's catalogue is a THIRD door onto the AE's screen, and it carries
    # the full prompts rather than a count — the gate has to hold on it too.
    assert _catalogue() == [], "an unsigned checklist reached an AE"
    _as(AE)
    assert client.get("/api/scripts/implementation_plan").status_code == 404

    _sign(script_id)

    row = _row()
    assert row["scripts"]["available"] == [
        {
            "product_key": "implementation_plan",
            "name": "Implementation Plan",
            "script_id": script_id,
            "item_count": len(PLAN_ITEMS),
        }
    ]
    assert [s["script_id"] for s in _catalogue()] == [script_id]
    _as(AE)
    assert client.get("/api/scripts/implementation_plan").status_code == 200


def test_the_picker_offers_the_product_before_the_ae_has_ever_picked_it(db):
    """``available`` is the picker; ``sections`` is what they picked.

    They are different lists on purpose. A first-ever call with a client has no
    remembered pick, so the AE must still be able to CHOOSE implementation plan —
    ``sections: []`` with a populated ``available`` is that state, and collapsing
    the two would make the checklist unreachable until it was already used once.
    """
    _publish("implementation_plan", "Implementation Plan", PLAN_ITEMS)

    row = _row()
    assert [p["product_key"] for p in row["scripts"]["available"]] == [
        "implementation_plan"
    ]
    assert row["scripts"]["selected_product_keys"] == []
    assert row["scripts"]["sections"] == []


def test_a_first_call_pick_has_its_checklist_before_anything_is_remembered(db):
    """The checklist has to render the moment the AE ticks a product — on the
    FIRST call with a client too, when ``sections`` is empty by definition.

    The row's ``sections`` are what this client's memory holds, and the memory
    is only written by ``POST /start``. A panel that could resolve nothing else
    showed "no approved checklist" under a chip reading "5 facts to cover", so
    the checklist first appeared on the SECOND call with every client, and never
    for a product added on the day. The day's ``script_catalogue`` is what the
    panel resolves a fresh pick against: every question, before any memory
    exists.
    """
    script_id = _publish("implementation_plan", "Implementation Plan", PLAN_ITEMS)

    day = _day()
    row = {r["google_event_id"]: r for r in day["meetings"]}[EVENT_A]
    assert row["scripts"]["selected_product_keys"] == [], "not a first call"
    assert row["scripts"]["sections"] == [], "sections still mean the memory"

    [section] = day["script_catalogue"]
    assert section["script_id"] == script_id
    assert section["product_key"] == "implementation_plan"
    assert section["name"] == "Implementation Plan"
    assert [i["fact_key"] for i in section["items"]] == [
        i["fact_key"] for i in PLAN_ITEMS
    ]
    # The panel renders these words, not the key — they have to survive the hop.
    assert section["items"][2]["prompt_text"] == "How many hours per site?"
    assert section["items"][2]["help_text"] == "Harper needs this to price the SOW."
    assert section["items"][4]["required"] is False


def test_the_catalogue_is_the_picker_in_full_and_sent_once(db):
    """``available`` is the chip ("5 facts to cover"); the catalogue is the
    checklist under it. They must describe the SAME scripts in the SAME order,
    or the chip and the panel contradict each other on one screen.

    And the catalogue is the day's, not the row's: it sits on the envelope once,
    so two rows cannot disagree about it and a twelve-meeting day does not carry
    twelve copies of every question.
    """
    _publish("staff_training", "Staff Training", TRAINING_ITEMS)
    _publish("implementation_plan", "Implementation Plan", PLAN_ITEMS)

    day = _day()
    catalogue = day["script_catalogue"]
    for row in day["meetings"]:
        assert "script_catalogue" not in row["scripts"], sorted(row["scripts"])
        available = row["scripts"]["available"]
        assert [(s["product_key"], s["script_id"]) for s in catalogue] == [
            (a["product_key"], a["script_id"]) for a in available
        ]
        assert [len(s["items"]) for s in catalogue] == [
            a["item_count"] for a in available
        ]
    assert [s["name"] for s in catalogue] == ["Implementation Plan", "Staff Training"]


def test_the_pick_is_remembered_and_renders_as_a_section_next_time(db):
    """The whole of the "self-service" feel, and it is deliberately cheap.

    A better idea is on record — read the products off the lead —
    and there is no HubSpot Leads READ anywhere in this repo to build it from.
    The weekly call with the same client defaulting to the same products is most
    of the value, and this is the hop where it either survives or does not:
    ``POST /start`` writes the memory, ``GET /scheduled`` reads it back.
    """
    _publish("implementation_plan", "Implementation Plan", PLAN_ITEMS)
    _start(product_keys=["implementation_plan"])

    row = _row()
    assert row["scripts"]["selected_product_keys"] == ["implementation_plan"]
    sections = row["scripts"]["sections"]
    assert len(sections) == 1
    assert sections[0]["product_key"] == "implementation_plan"
    assert [i["fact_key"] for i in sections[0]["items"]] == [
        i["fact_key"] for i in PLAN_ITEMS
    ]
    # The question the AE reads, and the flag that decides whether a miss is a
    # gap — both have to survive the hop or the panel is a list of bare keys.
    assert sections[0]["items"][2]["prompt_text"] == "How many hours per site?"
    assert sections[0]["items"][4]["required"] is False


def test_the_memory_is_per_client_and_does_not_leak_across_the_day(db):
    """A pick on Acme's row must not appear on Brightpath's.

    Keyed by ``(ae_email, hubspot_company_id)`` precisely so this cannot happen,
    and the day is scanned in ONE batched read across the day's companies —
    which is the shape most likely to hand every row the first row's answer.
    """
    _publish("implementation_plan", "Implementation Plan", PLAN_ITEMS)
    _start(product_keys=["implementation_plan"])

    assert _row(EVENT_A)["scripts"]["selected_product_keys"] == ["implementation_plan"]
    assert _row(EVENT_B)["scripts"]["selected_product_keys"] == []
    assert _row(EVENT_B)["scripts"]["sections"] == []
    # Both rows still offer the product — the catalogue is the day's, the pick
    # is the client's.
    assert len(_row(EVENT_B)["scripts"]["available"]) == 1


def test_a_retired_script_stops_reaching_the_row_but_stays_selected(db):
    """ "What the AE picked" and "what Cubie can show a checklist for" differ.

    A product whose checklist was retired should still render as SELECTED with
    no section, rather than silently unpicking itself — otherwise the AE's own
    choice disappears from their screen because of an authoring action taken by
    somebody else, with nothing said about it.
    """
    script_id = _publish("implementation_plan", "Implementation Plan", PLAN_ITEMS)
    _start(product_keys=["implementation_plan"])
    _as(AUTHOR)
    assert client.delete(f"/api/scripts/{script_id}").status_code == 200

    row = _row()
    assert row["scripts"]["selected_product_keys"] == ["implementation_plan"]
    assert row["scripts"]["available"] == []
    assert row["scripts"]["sections"] == []
    # Gone from the catalogue as well, so the panel's "no approved checklist"
    # section for the remembered pick is the true sentence.
    assert _catalogue() == []


# ═══ 2. Two products on one call, all the way through ═══════════════════════


def test_two_selected_products_render_two_sections_with_their_own_items(db):
    """Seam 2 at the calendar. The plural is the feature, not an edge case.

    AEs failing to split leads into multiple products is the failure this
    workstream exists to fix, and lead↔deal is 1:1, so each product belongs on
    its own lead. A panel that can only show one checklist makes the three-product
    call unrepresentable, so the sections are asserted to be two DISTINCT
    checklists in the AE's pick order, not one list twice.
    """
    _publish("implementation_plan", "Implementation Plan", PLAN_ITEMS)
    _publish("staff_training", "Staff Training", TRAINING_ITEMS)
    _start(product_keys=["staff_training", "implementation_plan"])

    sections = _row()["scripts"]["sections"]
    assert [s["product_key"] for s in sections] == [
        "staff_training",
        "implementation_plan",
    ], "the panel lost the AE's pick order"
    assert [s["name"] for s in sections] == ["Staff Training", "Implementation Plan"]
    assert [i["fact_key"] for i in sections[0]["items"]] == [
        "trainee_count",
        "session_frequency",
    ]
    assert [i["fact_key"] for i in sections[1]["items"]] == [
        i["fact_key"] for i in PLAN_ITEMS
    ]
    # Two script ids, so neither section is the other one rendered twice.
    assert sections[0]["script_id"] != sections[1]["script_id"]


def test_two_products_keep_independent_coverage_at_the_wrap_up(db):
    """Seam 2 at the wrap-up, and the assertion that a merged number fails.

    One blended percentage over a two-product call is wrong for both products
    and hides the one that was never asked about. Here implementation plan is fully
    covered and staff training is not: a merged verdict would read "most
    of it went fine" and the AE would close the wrap-up.
    """
    _publish("implementation_plan", "Implementation Plan", PLAN_ITEMS)
    _publish("staff_training", "Staff Training", TRAINING_ITEMS)
    started = _start(product_keys=["implementation_plan", "staff_training"])
    _extraction(
        db,
        started["call_session_id"],
        facts=[
            _fact("discovery_workshop", "yes"),
            _fact("site_count", "4"),
            _fact("hours_per_site", "60"),
            _fact("rate", "185"),
            _fact("commencement_date", "January"),
            _fact("trainee_count", "3", product_key="staff_training"),
        ],
    )

    payload = _wrapup(started["call_session_id"])

    assert [s["product_key"] for s in payload["script_coverage"]] == [
        "implementation_plan",
        "staff_training",
    ], "the wrap-up lost the order the AE picked the products in"
    sections = {s["product_key"]: s for s in payload["script_coverage"]}
    assert sections["implementation_plan"]["missing_required"] == []
    assert sections["staff_training"]["missing_required"] == ["session_frequency"]
    # And the covered product's facts did not leak into the other's items.
    assert [i["fact_key"] for i in sections["staff_training"]["items"]] == [
        "trainee_count",
        "session_frequency",
    ]
    assert all(i["found"] for i in sections["implementation_plan"]["items"])


# ═══ 3. The checklist never costs the day, and never costs the call ═════════


def test_a_failing_script_lookup_is_null_scripts_and_a_day_that_still_renders(
    db, monkeypatch
):
    """The fail-soft contract, and the one that protects an AE's whole day.

    ``scan_day`` is documented as a pure read that never raises, and ``GET
    /scheduled`` must never 500 — a blank day is a sentence the AE BELIEVES and
    acts on by not capturing a real call. So an unreadable ``call_scripts`` table
    costs the CHECKLIST and nothing else: every meeting, every company binding
    and every Meet link still renders, and ``scripts`` is ``None`` rather than an
    empty block, because "we could not look" and "nothing is authored yet" are
    different sentences.
    """
    _publish("implementation_plan", "Implementation Plan", PLAN_ITEMS)

    def _boom(_db):
        raise RuntimeError("no such table: call_script_items")

    monkeypatch.setattr(
        "rubik_core.call_capture.calendar_scan._live_script_payloads", _boom
    )

    day = _day()
    rows = day["meetings"]

    assert [r["google_event_id"] for r in rows] == [EVENT_A, EVENT_B], (
        "the day lost its meetings to a checklist failure"
    )
    assert [r["scripts"] for r in rows] == [None, None]
    # "We could not look", not "nothing is authored" — the same sentence the
    # rows say, and present rather than absent.
    assert "script_catalogue" in day
    assert day["script_catalogue"] is None
    # Everything the AE actually needs to take the call is untouched.
    assert rows[0]["company"]["hubspot_company_id"] == CO_A
    assert rows[1]["company"]["hubspot_company_id"] == CO_B
    assert rows[0]["meet_uri"] == URI_A
    assert rows[1]["meet_uri"] == URI_B


def test_every_row_carries_the_scripts_key_even_when_the_lookup_fails(db, monkeypatch):
    """``None``, never absent. A missing key reads as ``undefined`` in the
    frontend and renders "no products" — the "nothing is wrong" failure this
    whole block exists to avoid."""
    monkeypatch.setattr(
        "rubik_core.call_capture.calendar_scan._remembered_by_company",
        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("defaults unreadable")),
    )

    day = _day()
    for row in day["meetings"]:
        assert "scripts" in row, sorted(row)
        assert row["scripts"] is None
    # One failure domain: rows without a script block get no catalogue either.
    assert day["script_catalogue"] is None


def test_start_attaches_what_resolves_and_skips_the_rest_without_a_422(db):
    """Seam 3 at ``POST /start``. A checklist never refuses to start a call.

    Two ordinary ways a key does not resolve, and neither is a client error: the
    product is real but Harper has not authored its checklist yet (the tables
    ship EMPTY on purpose), or a draft exists and nobody has signed it — and an
    unapproved script is a machine's guess, which never reaches an AE. Refusing
    the start would cost the AE the capture, the company binding and the
    transcript for a call that is about to begin.
    """
    _publish("implementation_plan", "Implementation Plan", PLAN_ITEMS)
    _draft("staff_training", "Staff Training", TRAINING_ITEMS)  # unsigned

    started = _start(
        product_keys=["implementation_plan", "staff_training", "no_such_product"]
    )

    session = db.query(CallSession).one()
    assert session.id == started["call_session_id"], "the call was not started"
    attached = db.query(CallSessionScript).all()
    assert [row.product_key for row in attached] == ["implementation_plan"]
    assert attached[0].call_session_id == session.id

    # The AE's PICK is remembered whole — including what did not resolve — so an
    # approval tomorrow lights the section up without them re-picking.
    assert _row()["scripts"]["selected_product_keys"] == [
        "implementation_plan",
        "staff_training",
        "no_such_product",
    ]
    assert [s["product_key"] for s in _row()["scripts"]["sections"]] == [
        "implementation_plan"
    ]


def test_approving_the_second_script_later_lights_up_the_remembered_pick(db):
    """The pay-off of remembering what did not resolve, end to end.

    Harper signs staff training after the AE already picked it. Nothing
    else happens — no second start, no re-pick — and the row now renders both
    sections, which is what makes the authoring form worth having.
    """
    _publish("implementation_plan", "Implementation Plan", PLAN_ITEMS)
    draft_id = _draft("staff_training", "Staff Training", TRAINING_ITEMS)
    _start(product_keys=["implementation_plan", "staff_training"])
    assert [s["product_key"] for s in _row()["scripts"]["sections"]] == [
        "implementation_plan"
    ]

    _sign(draft_id)

    assert [s["product_key"] for s in _row()["scripts"]["sections"]] == [
        "implementation_plan",
        "staff_training",
    ]


def test_a_repeat_start_does_not_duplicate_an_attachment(db):
    """The double-click, the second tab, the AE who reopened the row.

    ``UNIQUE(call_session_id, product_key)`` is the backstop, but a blind insert
    would make the ordinary repeat an exception instead of a no-op — and
    ``_attach_products`` swallows exceptions, so the visible symptom would be an
    attachment that silently stopped being written.
    """
    _publish("implementation_plan", "Implementation Plan", PLAN_ITEMS)
    first = _start(product_keys=["implementation_plan"])
    second = _start(product_keys=["implementation_plan"])

    assert second["call_session_id"] == first["call_session_id"], "two sessions"
    assert db.query(CallSession).count() == 1
    assert db.query(CallSessionScript).count() == 1
    assert len(_row()["scripts"]["sections"]) == 1


def test_a_second_start_adds_a_newly_picked_product_to_the_same_call(db):
    """Find-or-create, not insert-or-explode, and not ignore-everything.

    The AE reopened the row before the call to add a product. The attach has to
    ADD it to the session that already exists, or the second checklist never
    reaches the wrap-up and the split prompt fires for a product they DID pick.
    """
    _publish("implementation_plan", "Implementation Plan", PLAN_ITEMS)
    _publish("staff_training", "Staff Training", TRAINING_ITEMS)
    first = _start(product_keys=["implementation_plan"])
    second = _start(product_keys=["implementation_plan", "staff_training"])

    assert second["call_session_id"] == first["call_session_id"]
    assert sorted(r.product_key for r in db.query(CallSessionScript).all()) == [
        "implementation_plan",
        "staff_training",
    ]


def test_a_start_that_omits_product_keys_leaves_last_weeks_pick_alone(db):
    """OMITTED and EMPTY are different requests, and the difference is real.

    ``remember_products`` treats an empty list as *clear the pick*, which is
    right for an AE who deselected everything and catastrophic for an older UI
    (or a retry) that never sends the field. ``model_fields_set`` is the only
    thing that can tell them apart.
    """
    _publish("implementation_plan", "Implementation Plan", PLAN_ITEMS)
    _start(product_keys=["implementation_plan"])

    _start()  # the field is absent, not empty

    assert _row()["scripts"]["selected_product_keys"] == ["implementation_plan"]
    assert db.query(CallSessionScript).count() == 1


def test_a_start_that_sends_an_empty_list_clears_the_pick(db):
    """The mirror: the AE deselected every product and that has to stick.

    The already-attached checklist is NOT removed — the call ran against it and
    the wrap-up cites it — but next week's row comes back unpicked.
    """
    _publish("implementation_plan", "Implementation Plan", PLAN_ITEMS)
    _start(product_keys=["implementation_plan"])

    _start(product_keys=[])

    assert _row()["scripts"]["selected_product_keys"] == []
    assert db.query(CallSessionScript).count() == 1, "history was rewritten"


# ═══ 4. The wrap-up says what the call did not surface ══════════════════════


def test_a_missed_required_fact_comes_back_as_the_question_to_ask(db):
    """Seam 4, and the acceptance criterion, over the whole chain.

    ``rate`` and ``commencement_date`` were never discussed. The AE reads *"What
    rate did we quote?"* — not a bare key, not a percentage, and not silence.
    ``commencement_date`` is optional, so it is SHOWN and not DEMANDED: a gap
    list that demands optional facts is one an AE learns to dismiss, and then the
    required one costs nothing to miss.
    """
    _publish("implementation_plan", "Implementation Plan", PLAN_ITEMS)
    started = _start(product_keys=["implementation_plan"])
    _extraction(
        db,
        started["call_session_id"],
        facts=[
            _fact("discovery_workshop", "yes"),
            _fact("site_count", "4"),
            _fact("hours_per_site", "60"),
        ],
    )

    payload = _wrapup(started["call_session_id"])

    section = payload["script_coverage"][0]
    assert section["product_key"] == "implementation_plan"
    assert section["name"] == "Implementation Plan"
    assert section["missing_required"] == ["rate"]
    by_key = {row["fact_key"]: row for row in section["items"]}
    assert by_key["hours_per_site"]["found"] is True
    assert by_key["hours_per_site"]["value"] == "60"
    assert by_key["rate"]["found"] is False
    assert by_key["rate"]["value"] is None
    assert by_key["rate"]["prompt_text"] == "What rate did we quote?"
    assert by_key["commencement_date"]["found"] is False
    assert "commencement_date" not in section["missing_required"]


def test_a_call_that_covered_everything_has_nothing_to_go_back_for(db):
    """The other half of the criterion, and the one that keeps the gap list
    credible: a complete call must produce an EMPTY ``missing_required``.

    A checklist that always finds something wrong is a checklist an AE stops
    reading, and then the incomplete call it was built for goes through clean.
    """
    _publish("implementation_plan", "Implementation Plan", PLAN_ITEMS)
    started = _start(product_keys=["implementation_plan"])
    _extraction(
        db,
        started["call_session_id"],
        facts=[_fact(item["fact_key"], "answered") for item in PLAN_ITEMS],
    )

    payload = _wrapup(started["call_session_id"])

    section = payload["script_coverage"][0]
    assert section["missing_required"] == []
    assert [i["found"] for i in section["items"]] == [True] * len(PLAN_ITEMS)


def test_a_product_discussed_but_never_picked_is_offered_as_a_split(db):
    """The sentence the whole multi-product design exists for.

    *"Staff training was discussed but this call is only scripted for the
    implementation plan — split it into its own lead?"* Lead↔deal is 1:1, and AEs
    generally do not split. This is the one place the manual step they skip
    becomes one click, and it has to fire for a product
    that has no checklist authored at all — requiring one would mute the feature
    exactly while the tables are still filling up, which is now.
    """
    _publish("implementation_plan", "Implementation Plan", PLAN_ITEMS)
    started = _start(product_keys=["implementation_plan"])
    _extraction(
        db,
        started["call_session_id"],
        facts=[_fact("hours_per_site", "60")],
        leads=[{"product": "staff_training", "name": "Acme - Staff"}],
    )

    payload = _wrapup(started["call_session_id"])

    assert payload["unscripted_products"] == ["staff_training"]


def test_a_product_the_call_was_picked_for_is_never_offered_as_a_split(db):
    """The mirror, and the false alarm that would cost the prompt its credit:
    being told to split out a product the AE already loaded a checklist for."""
    _publish("implementation_plan", "Implementation Plan", PLAN_ITEMS)
    started = _start(product_keys=["implementation_plan"])
    _extraction(
        db,
        started["call_session_id"],
        facts=[],
        leads=[
            {"product": "implementation_plan", "name": "Acme - Implementation Plan"}
        ],
    )

    payload = _wrapup(started["call_session_id"])

    assert payload["unscripted_products"] == []


def test_a_call_with_no_checklist_keeps_todays_wrap_up_shape(db):
    """Seam 5. Every call in the product is in this state right now.

    The tables ship EMPTY — the content is Indigo's and Harper's to author —
    so the un-scripted call is not an edge case, it is the whole population on
    day one. Both keys are always present and both are empty lists, so the screen
    never branches on their existence and nothing about the existing wrap-up
    moves.
    """
    started = _start()  # no products picked, and no scripts authored anywhere
    _extraction(db, started["call_session_id"], facts=[])

    payload = _wrapup(started["call_session_id"])

    assert set(payload) == {
        "session",
        "groups",
        "script_coverage",
        "unscripted_products",
    }
    assert payload["script_coverage"] == []
    assert payload["unscripted_products"] == []
    assert db.query(CallSessionScript).count() == 0


def test_a_split_is_offered_on_day_one_before_any_script_exists(db):
    """The one way an un-scripted call is NOT identical to today's wrap-up, pinned.

    ``unscripted_products`` deliberately does NOT require an approved script for
    a product to be reported: splitting the lead is worth doing whether or not
    Harper has authored that product's checklist, and requiring one would mute
    the feature exactly while the tables are still filling up — which is now.

    So the day-one call, with an empty ``call_scripts`` table and nothing
    picked, still offers the split. Asserted rather than assumed, because it is
    the single behavioural change an AE sees before anybody has authored
    anything, and a reviewer reading "nothing changes until content exists"
    would otherwise be wrong about it.
    """
    started = _start()
    _extraction(
        db,
        started["call_session_id"],
        facts=[],
        leads=[{"product": "staff_training", "name": "Acme - Staff"}],
    )

    payload = _wrapup(started["call_session_id"])

    assert db.query(CallScript).count() == 0, "this test must run on empty tables"
    assert payload["script_coverage"] == []
    assert payload["unscripted_products"] == ["staff_training"]


def test_the_wrap_up_grades_the_checklist_the_ae_was_actually_shown(db):
    """A script approved BETWEEN the call and the wrap-up must not invent a gap.

    ``call_session_scripts`` points at the exact script id the call ran under and
    the wrap-up loads by that id rather than re-resolving the product's newest
    approved version. Without it, an authoring action taken after the call
    rewrites the record of what the AE covered — silently, and in the direction
    that makes them look worse.
    """
    _publish("implementation_plan", "Implementation Plan", PLAN_ITEMS[:3])
    started = _start(product_keys=["implementation_plan"])
    _extraction(
        db,
        started["call_session_id"],
        facts=[_fact(item["fact_key"], "answered") for item in PLAN_ITEMS[:3]],
    )
    assert (
        _wrapup(started["call_session_id"])["script_coverage"][0]["missing_required"]
        == []
    )

    # Harper adds two more facts and signs the fork, after the call.
    _as(AUTHOR)
    listing = client.get("/api/scripts", params={"include_drafts": True}).json()
    live_id = [s["script_id"] for s in listing["scripts"] if s["is_live"]][0]
    forked = client.put(f"/api/scripts/{live_id}", json={"items": PLAN_ITEMS})
    assert forked.status_code == 200, forked.text
    _sign(forked.json()["script_id"])

    payload = _wrapup(started["call_session_id"])

    section = payload["script_coverage"][0]
    assert section["missing_required"] == [], (
        "a script approved after the call grew a gap on a finished wrap-up"
    )
    assert [i["fact_key"] for i in section["items"]] == [
        i["fact_key"] for i in PLAN_ITEMS[:3]
    ]
    # ...while the NEXT call gets the new checklist, questions and all.
    assert _row(EVENT_B)["scripts"]["available"][0]["item_count"] == len(PLAN_ITEMS)
    assert [i["fact_key"] for i in _catalogue()[0]["items"]] == [
        i["fact_key"] for i in PLAN_ITEMS
    ]


# ═══ 5. Authoring authz — reaching the feature is not authoring it ══════════


def _widen_supervisor_roster_to_support(db) -> None:
    """Hand the supervisor roster to another team (Support) the way an admin would:
    an override row in Admin → Feature Access, which is the only thing the gate
    reads (``rubik_core.features``)."""
    from rubik_core.features import FeaturePolicy, feature_by_key, save_policy

    default = feature_by_key("sales_cockpit_supervisor").default()
    save_policy(
        db,
        "sales_cockpit_supervisor",
        FeaturePolicy(
            departments=frozenset({"support"}),
            extra_emails=default.extra_emails,
            admin_allowed=True,
        ),
        actor_email="test@acme.example",
    )


@pytest.fixture(autouse=True)
def _fresh_feature_cache():
    """The roster cache is process-wide; an override written here must not
    outlive the in-memory database it was written to."""
    from rubik_core.features import clear_cache

    clear_cache()
    yield
    clear_cache()


def test_an_ae_inside_call_capture_cannot_sign_a_script(db):
    """The authoring gate is a REAL second gate, invisible under the fixture.

    The fixture narrows the supervisor view and Sales Call Capture to one
    ``{"systems"}`` roster, so every Systems caller passes both and a broken
    gate looks exactly like a working one. Handing the supervisor roster to
    another team (Support) through Admin → Feature Access is the only way to see it.

    The consequence is the one that matters: an AE who could sign their own
    script could put an unreviewed checklist in front of themselves, and
    ``approved_by`` is the only evidence that each script was approved by a
    named human, none generated-and-shipped.
    """
    script_id = _draft("implementation_plan", "Implementation Plan", PLAN_ITEMS)
    _widen_supervisor_roster_to_support(db)

    _as(AE)
    res = client.post(f"/api/scripts/{script_id}/approve")
    assert res.status_code == 403, res.text
    assert "Nudge Worklist" in res.json()["detail"]

    # Refused, not merely unreported: the row is still unsigned and still
    # unreachable from the AE's own calendar row.
    assert db.get(CallScript, script_id).approved_by is None
    assert _row()["scripts"]["available"] == []


def test_an_ae_still_reads_the_catalogue_they_cannot_author(db):
    """The AE needs the panel; they do not need the editor.

    Splitting these two is what lets an admin open Call Capture to the whole
    sales floor without handing every AE the ability to rewrite the company's
    checklists.
    """
    _publish("implementation_plan", "Implementation Plan", PLAN_ITEMS)
    _widen_supervisor_roster_to_support(db)

    _as(AE)
    listing = client.get("/api/scripts")
    assert listing.status_code == 200, listing.text
    assert [s["product_key"] for s in listing.json()["scripts"]] == [
        "implementation_plan"
    ]
    assert listing.json()["can_author"] is False
    assert client.get("/api/scripts/implementation_plan").status_code == 200
    assert len(_row()["scripts"]["available"]) == 1

    for method, path, body in (
        ("post", "/api/scripts", {"product_key": "x", "name": "X", "items": []}),
        ("put", f"/api/scripts/{uuid.uuid4().hex}", {"name": "Renamed"}),
        ("delete", f"/api/scripts/{uuid.uuid4().hex}", None),
    ):
        res = getattr(client, method)(
            path, **({"json": body} if body is not None else {})
        )
        assert res.status_code == 403, f"{method} {path} → {res.status_code}"


def test_a_signature_is_the_caller_and_never_the_body(db):
    """``approved_by`` is stamped from the authenticated user, over the whole
    chain, and the calendar catalogue only ever shows a script carrying one.

    A signature a caller can supply is not a signature. This asserts it at the
    end of the chain rather than at the endpoint, because the column's whole
    purpose is to gate what reaches an AE's screen.
    """
    script_id = _draft("implementation_plan", "Implementation Plan", PLAN_ITEMS)
    _as(AUTHOR)
    res = client.post(
        f"/api/scripts/{script_id}/approve",
        json={
            "approved_by": "nobody@example.com",
            "approved_at": "2020-01-01T00:00:00",
        },
    )
    assert res.status_code == 200, res.text

    row = db.get(CallScript, script_id)
    assert row.approved_by == AUTHOR
    assert row.approved_at is not None
    assert row.approved_at != datetime(2020, 1, 1), "the body set the timestamp"
    assert _row()["scripts"]["available"][0]["script_id"] == script_id

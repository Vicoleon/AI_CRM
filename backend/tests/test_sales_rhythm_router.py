"""GET /api/sales/rhythm/{part} — My Day's Morning / Mid-day / End-of-day section.

Gates first (switch, roster, unknown part), then the three honest failure
states ("HubSpot could not be read", "you are not a HubSpot owner"), then what a
real day looks like: three rows a block, the overflow as a count, one primary
action per row, and the overflow question equal to the Ask Cubie prompt.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from backend.routers import sales_rhythm
from backend.tests.test_sales_rhythm_checks import TODAY, FakeHubSpot, _iso
from rubik_core.config import settings
from rubik_core.database.schemas import UserProfile
from rubik_core.features import clear_cache as clear_feature_cache
from rubik_core.sales import rhythm_checks as rc
from rubik_core.sales import rhythm_signals
from rubik_core.tenancy.hubspot_config import HubSpotConnectorConfig

client = TestClient(app)


def _as(email: str = "ae@acme.example", department: str = "Systems") -> UserProfile:
    profile = UserProfile(
        id=email.split("@")[0],
        email=email,
        name="Avery Exec",
        department=department,
        roles=["user"],
        created_at="2026-10-01T00:00:00",
        updated_at="2026-10-01T00:00:00",
    )
    app.dependency_overrides[get_current_user] = lambda: profile
    return profile


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """The app's engine on a throwaway sqlite file with the schema built, and
    the feature-policy cache empty before and after.

    ``/api/users/me`` reads ``roles`` and the roster check reads
    ``feature_access`` through the shared engine. Without this the file only
    passed when an earlier test in the same worker had built that schema, and
    the roster verdicts came from whatever rows that test left behind.
    """
    import rubik_core.database.db as dbmod
    from rubik_core.database.models import Base

    monkeypatch.setattr(
        settings, "DATABASE_URL", f"sqlite:///{tmp_path / 'sales_rhythm.db'}"
    )
    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)
    Base.metadata.create_all(bind=engine)
    clear_feature_cache()
    try:
        yield
    finally:
        clear_feature_cache()
        engine.dispose()
        dbmod._engine = None


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    monkeypatch.setattr(settings, "COCKPIT_ENABLED", True)
    monkeypatch.setattr(settings, "SALES_RHYTHM_ENABLED", True)
    monkeypatch.setattr(
        rc,
        "get_hubspot_config",
        lambda: HubSpotConnectorConfig(pipeline_ids=("default",), portal_id="9100001"),
    )
    monkeypatch.setattr(rc, "_today", lambda: TODAY)
    # No Google call from a test: the rep's own email/calendar come back empty.
    monkeypatch.setattr(
        rc,
        "SIGNALS",
        lambda mailbox, terms, **_k: rhythm_signals.SignalReport(
            touches={d: rhythm_signals.Touch() for d in terms}
        ),
    )
    monkeypatch.setattr(
        rc, "DAY_ACTIVITY", lambda *_a, **_k: rhythm_signals.DayActivity()
    )
    rc.clear_cache()
    yield
    app.dependency_overrides.clear()
    rc.clear_cache()


@pytest.fixture
def hs(monkeypatch):
    fake = FakeHubSpot()
    monkeypatch.setattr(
        "rubik_core.retrieval.hubspot_api.HubSpotAPIClient", lambda: fake
    )
    return fake


# ── gates ────────────────────────────────────────────────────────────────────


def test_switched_off_is_a_404(monkeypatch, hs):
    _as()
    monkeypatch.setattr(settings, "SALES_RHYTHM_ENABLED", False)
    assert client.get("/api/sales/rhythm/midday").status_code == 404


def test_off_the_roster_is_a_403_from_this_gate(monkeypatch, hs):
    _as()
    monkeypatch.setattr(sales_rhythm, "feature_allows", lambda key, user: False)
    res = client.get("/api/sales/rhythm/midday")
    assert res.status_code == 403
    assert "daily rhythm" in res.json()["detail"]


def test_an_unknown_part_is_a_404(hs):
    _as()
    assert client.get("/api/sales/rhythm/lunch").status_code == 404


# ── honest failure states ────────────────────────────────────────────────────


def test_no_hubspot_token_marks_every_block_unavailable(monkeypatch):
    _as()
    monkeypatch.setattr(
        "rubik_core.retrieval.hubspot_api.workspace_secret", lambda *_: None
    )
    body = client.get("/api/sales/rhythm/eod").json()
    assert body["crm_available"] is False
    assert [b["key"] for b in body["blocks"]] == ["missing_fields", "overdue"]
    assert {b["unavailable_reason"] for b in body["blocks"]} == {"crm_unavailable"}
    assert body["recap_prompt"] == sales_rhythm.RECAP_PROMPT


def test_an_unreadable_owner_list_is_an_outage_not_a_missing_owner(hs):
    # owner_directory() swallows its own errors into {}: that must read as
    # "HubSpot could not be read", never as "you are not a HubSpot owner".
    _as()
    hs.owners = {}
    body = client.get("/api/sales/rhythm/midday").json()
    assert body["crm_available"] is False
    assert {b["unavailable_reason"] for b in body["blocks"]} == {"crm_unavailable"}


def test_a_rep_with_no_hubspot_owner_is_told_so(hs):
    _as("nobody@acme.example")
    body = client.get("/api/sales/rhythm/morning").json()
    assert body["crm_available"] is True
    assert body["blocks"][0]["unavailable_reason"] == "no_hubspot_owner"
    assert body["blocks"][0]["items"] == []


# ── a real day ───────────────────────────────────────────────────────────────


def test_midday_caps_each_block_at_three_with_the_rest_as_a_count(hs):
    _as()
    for i in range(5):
        hs.add(
            "deals",
            f"d{i}",
            dealname=f"Deal {i}",
            amount=str(40_000 + i),
            hubspot_owner_id="77",
            num_associated_contacts="1",
        )

    body = client.get("/api/sales/rhythm/midday").json()

    keys = [b["key"] for b in body["blocks"]]
    assert keys == list(sales_rhythm.PARTS["midday"])
    single = next(b for b in body["blocks"] if b["key"] == "single_threaded")
    assert len(single["items"]) == 3
    assert single["suppressed_count"] == 2
    assert single["scanned"] == 5
    assert single["items"][0]["title"] == "Deal 4 · $40,004"
    assert single["items"][0]["primary_action"] == {
        "label": "Add a contact in HubSpot",
        "kind": "open_hubspot",
        "href": "https://app.hubspot.com/contacts/9100001/deal/d4",
    }
    assert (
        single["ask_prompt"]
        == "List my active deals over $30k that only have one contact."
    )


def test_a_reengagement_row_drafts_the_check_in_through_the_assistant(hs):
    _as()
    lost = {
        "hs_is_closed": "true",
        "hs_is_closed_won": "false",
        "hubspot_owner_id": "77",
    }
    hs.add(
        "deals",
        "1",
        dealname="Acme - IA",
        amount="88000",
        closedate=_iso(2025, 4, 30),
        loss_reason="Pricing",
        **lost,
    )
    hs.add(
        "contacts",
        "c1",
        firstname="Ann",
        lastname="Lee",
        email="ann@acmecorp.example",
        associatedcompanyid="co1",
    )
    hs.add("companies", "co1", name="Acme Industries")
    hs.link("deals", "contacts", "1", "c1")
    hs.link("deals", "companies", "1", "co1")

    body = client.get("/api/sales/rhythm/midday").json()

    block = next(b for b in body["blocks"] if b["key"] == "reengage_lost")
    action = block["items"][0]["primary_action"]
    assert action["kind"] == "ask_cubie"
    href = urlparse(action["href"])
    assert href.path == "/chat"
    prompt = parse_qs(href.query)["q"][0]
    assert "Ann Lee (ann@acmecorp.example) at Acme Industries" in prompt
    assert "paused over budget in 2025-04" in prompt
    assert "Gmail draft" in prompt
    assert block["items"][0]["secondary_action"]["kind"] == "open_hubspot"
    assert "cannot confirm employment" in block["note"]


def test_every_item_has_exactly_one_primary_action_of_a_known_kind(hs):
    _as()
    hs.add(
        "deals",
        "1",
        dealname="Past due",
        hubspot_owner_id="77",
        closedate=_iso(2026, 9, 1),
    )
    hs.add(
        "tasks",
        "t1",
        hs_task_subject="Call back",
        hs_timestamp=_iso(2026, 9, 20),
        hs_task_status="NOT_STARTED",
        hubspot_owner_id="77",
    )
    for part in sales_rhythm.PARTS:
        body = client.get(f"/api/sales/rhythm/{part}").json()
        for block in body["blocks"]:
            for item in block["items"]:
                assert item["primary_action"]["kind"] in {"open_hubspot", "ask_cubie"}
    overdue = next(
        b
        for b in client.get("/api/sales/rhythm/eod").json()["blocks"]
        if b["key"] == "overdue"
    )
    assert [i["primary_action"]["label"] for i in overdue["items"]] == [
        "Re-date it in HubSpot",
        "Open my HubSpot tasks",
    ]


def test_capped_blocks_link_to_an_uncapped_question(hs):
    _as()
    body = client.get("/api/sales/rhythm/morning").json()
    block = body["blocks"][0]
    assert block["ask_prompt"].startswith("Which 3 deals")
    assert block["more_prompt"].startswith("List all")
    midday = client.get("/api/sales/rhythm/midday").json()["blocks"]
    single = next(b for b in midday if b["key"] == "single_threaded")
    assert single["more_prompt"] == single["ask_prompt"]  # already uncapped


def test_the_kill_switch_hides_the_chips_too(monkeypatch):
    _as(department="Sales")
    on = client.get("/api/users/me").json()["features"]
    assert on["sales_rhythm"] is True
    monkeypatch.setattr(settings, "SALES_RHYTHM_ENABLED", False)
    off = client.get("/api/users/me").json()["features"]
    assert off["sales_rhythm"] is False and off["sales_rhythm_team"] is False

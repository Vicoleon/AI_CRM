"""Tests for the sales-model admin endpoints + the per-role resolver.

Covers:
  - GET /api/admin/sales/catalog returns every role with GA-safe choices
  - GET /api/admin/sales/config returns config.py defaults when no row exists
  - PUT persists and is reflected on the next GET
  - PUT rejects a model outside a role's choices, an unknown role, and a
    non-grounding model for the X-Ray web agent
  - A stored role whose feature was removed is dropped on read, so the admin
    page never sends it back into that rejection
  - model_for() picks up an admin-saved model without a restart, and falls back
    when the row is missing, blank, or the DB read fails
  - The X-Ray accessors resolve per call rather than freezing at import — the
    defect the store exists to fix
  - Non-admin users are rejected
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from backend.auth import admin_required, get_current_user
from backend.main import app
from rubik_core.config import settings
from rubik_core.sales.model_config import ROLE_KEYS, model_for


def _mock_admin():
    user = MagicMock()
    user.email = "admin@example.com"
    user.roles = ["admin", "user"]
    return user


def _mock_user():
    user = MagicMock()
    user.email = "user@example.com"
    user.roles = ["user"]
    return user


class _InMemoryStore:
    """Minimal stand-in for the WorkspaceSetting query/upsert dance."""

    def __init__(self):
        self.row = None

    def session(self) -> MagicMock:
        store = self
        mock_db = MagicMock()

        def query_first():
            return store.row

        chain = mock_db.query.return_value.filter.return_value
        chain.first = query_first

        def add(obj):
            store.row = obj

        mock_db.commit = lambda: None
        mock_db.add = add
        return mock_db


@pytest.fixture
def store(monkeypatch):
    s = _InMemoryStore()

    def fake_session_local():
        mock = MagicMock()
        mock.__enter__.return_value = s.session()
        mock.__exit__.return_value = False
        return mock

    monkeypatch.setattr(
        "rubik_core.sales.model_config.SessionLocal", fake_session_local
    )
    return s


@pytest.fixture
def admin_client():
    prev_admin = app.dependency_overrides.get(admin_required)
    prev_user = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[admin_required] = _mock_admin
    app.dependency_overrides[get_current_user] = _mock_admin
    client = TestClient(app)
    yield client
    if prev_admin is None:
        app.dependency_overrides.pop(admin_required, None)
    else:
        app.dependency_overrides[admin_required] = prev_admin
    if prev_user is None:
        app.dependency_overrides.pop(get_current_user, None)
    else:
        app.dependency_overrides[get_current_user] = prev_user


# ── Catalog ──────────────────────────────────────────────────────────────────


def test_catalog_lists_every_role_with_choices(admin_client):
    res = admin_client.get("/api/admin/sales/catalog")
    assert res.status_code == 200, res.text
    body = res.json()

    keys = [r["key"] for r in body["roles"]]
    assert keys == list(ROLE_KEYS)
    # Every sales surface is represented — a new AI call site added to one of
    # these packages without a role here would silently stay un-tunable.
    for expected in (
        "proposals",
        "call_extraction",
        "xray_analyst",
        "crm_sync",
    ):
        assert expected in keys

    for role in body["roles"]:
        assert role["label"] and role["description"] and role["group"]
        # The default must itself be selectable, or the dropdown opens on a
        # value the PUT would reject.
        assert role["default_model"] in role["models"]
        # Preview ids are quota-starved; 3.8-pro does not exist on Vertex.
        assert not any(m.endswith("-preview") for m in role["models"])
        # Re-probed in full 2026-09-17: no Pro-tier 3.x id serves on Vertex
        # global. Offering one 404s every run of the role that picks it.
        for pro in (
            "gemini-3.8-pro",
            "gemini-3.7-pro",
            "gemini-3.1-pro",
            "gemini-pro-latest",
            "gemini-2.5-pro-latest",
        ):
            assert pro not in role["models"]


def test_catalog_defaults_match_config_tiers(admin_client):
    res = admin_client.get("/api/admin/sales/catalog")
    by_key = {r["key"]: r for r in res.json()["roles"]}
    assert by_key["proposals"]["default_model"] == settings.VERTEX_LLM_MODEL_PRO
    assert by_key["xray_analyst"]["default_model"] == settings.VERTEX_LLM_MODEL_PRO
    assert by_key["xray_channel"]["default_model"] == settings.VERTEX_LLM_MODEL_FLASH
    assert by_key["crm_sync"]["default_model"] == settings.VERTEX_LLM_MODEL_FLASH


def test_web_role_offers_only_grounding_capable_models(admin_client):
    res = admin_client.get("/api/admin/sales/catalog")
    by_key = {r["key"]: r for r in res.json()["roles"]}
    web = by_key["xray_web"]
    # Pro cannot serve a Google-Search-grounded call today; offering it would
    # break the web channel of every Deep run.
    assert "gemini-2.5-pro" not in web["models"]
    assert "gemini-3.8-flash" in web["models"]
    # Probed 2026-09-17 with a search-forcing prompt: 3.7 Flash issued real
    # web_search_queries 5/5, so it belongs in the narrowed web dropdown too.
    assert "gemini-3.7-flash" in web["models"]
    assert len(web["models"]) < len(res.json()["all_models"])


# ── Config round-trip ────────────────────────────────────────────────────────


def test_get_config_returns_defaults_when_no_row(admin_client, store):
    res = admin_client.get("/api/admin/sales/config")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["schema_version"] == 1
    assert set(body["roles"]) == set(ROLE_KEYS)
    assert body["roles"]["proposals"]["model_id"] == settings.VERTEX_LLM_MODEL_PRO


def test_put_persists_and_round_trips(admin_client, store):
    res = admin_client.put(
        "/api/admin/sales/config",
        json={"roles": {"proposals": {"model_id": "gemini-3.8-flash"}}},
    )
    assert res.status_code == 200, res.text
    assert res.json()["roles"]["proposals"]["model_id"] == "gemini-3.8-flash"

    res2 = admin_client.get("/api/admin/sales/config")
    body = res2.json()
    assert body["roles"]["proposals"]["model_id"] == "gemini-3.8-flash"
    # Untouched roles keep their defaults rather than being wiped by the merge.
    assert body["roles"]["xray_analyst"]["model_id"] == settings.VERTEX_LLM_MODEL_PRO


def test_put_rejects_model_outside_catalog(admin_client, store):
    res = admin_client.put(
        "/api/admin/sales/config",
        json={"roles": {"proposals": {"model_id": "gemini-3-flash-preview"}}},
    )
    assert res.status_code == 400, res.text


def test_put_rejects_ungrounded_model_for_web_role(admin_client, store):
    res = admin_client.put(
        "/api/admin/sales/config",
        json={"roles": {"xray_web": {"model_id": "gemini-2.5-pro"}}},
    )
    assert res.status_code == 400, res.text
    # …while the same id is fine on a role that does not need grounding.
    ok = admin_client.put(
        "/api/admin/sales/config",
        json={"roles": {"xray_channel": {"model_id": "gemini-2.5-pro"}}},
    )
    assert ok.status_code == 200, ok.text


def test_put_rejects_unknown_role(admin_client, store):
    res = admin_client.put(
        "/api/admin/sales/config",
        json={"roles": {"not_a_role": {"model_id": "gemini-3.8-flash"}}},
    )
    assert res.status_code == 400, res.text


def test_stored_role_of_a_removed_feature_is_ignored(admin_client, store):
    """A row saved before a feature was deleted still names its role. GET must
    leave it out, so the page's save (which sends back every role it read)
    is not refused as an unknown role."""
    from rubik_core.database.models import WorkspaceSetting
    from rubik_core.sales.model_config import SETTINGS_KEY

    store.row = WorkspaceSetting(
        key=SETTINGS_KEY,
        value={
            "schema_version": 1,
            "roles": {
                "compliance_score": {"model_id": "gemini-3.8-flash"},
                "conference_parse": {"model_id": "gemini-3.8-flash"},
                "conference_intel": {"model_id": "gemini-3.8-flash"},
                "buyer_persona_signals": {"model_id": "gemini-3.8-flash"},
                "proposals": {"model_id": "gemini-3.8-flash"},
            },
        },
    )

    body = admin_client.get("/api/admin/sales/config").json()
    assert set(body["roles"]) == set(ROLE_KEYS)
    assert body["roles"]["proposals"]["model_id"] == "gemini-3.8-flash"

    res = admin_client.put("/api/admin/sales/config", json={"roles": body["roles"]})
    assert res.status_code == 200, res.text
    stored = res.json()["roles"]
    assert {
        "compliance_score",
        "conference_parse",
        "conference_intel",
        "buyer_persona_signals",
    }.isdisjoint(stored)


def test_sales_admin_rejects_non_admin():
    prev = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = _mock_user
    client = TestClient(app)
    try:
        res = client.get("/api/admin/sales/config")
        assert res.status_code == 403
    finally:
        if prev is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = prev


# ── Resolver ─────────────────────────────────────────────────────────────────


def test_model_for_picks_up_saved_model(store):
    from rubik_core.sales.model_config import save_settings_blob

    save_settings_blob(
        {"roles": {"call_extraction": {"model_id": "gemini-3.8-flash"}}},
        actor_email="admin@example.com",
    )
    assert model_for("call_extraction") == "gemini-3.8-flash"
    # Roles left alone still resolve to their tier.
    assert model_for("proposals") == settings.VERTEX_LLM_MODEL_PRO


def test_model_for_falls_back_on_blank_value(store):
    from rubik_core.sales.model_config import save_settings_blob

    save_settings_blob({"roles": {"proposals": {"model_id": "  "}}})
    assert model_for("proposals") == settings.VERTEX_LLM_MODEL_PRO


def test_model_for_falls_back_when_db_read_fails(monkeypatch):
    from rubik_core.sales import model_config

    def boom():
        raise RuntimeError("cloud sql is having a day")

    monkeypatch.setattr(model_config, "SessionLocal", boom)
    # A DB hiccup must degrade to the config tier, never break a sales run.
    assert model_config.model_for("proposals") == settings.VERTEX_LLM_MODEL_PRO


def test_xray_accessors_resolve_per_call_not_at_import(store):
    """The whole point of the store: X-Ray used module constants, which froze
    the model at import and made an admin's change a no-op until redeploy."""
    from rubik_core.sales.model_config import save_settings_blob
    from rubik_core.xray import config as xray_config

    assert xray_config.model_analyst() == settings.VERTEX_LLM_MODEL_PRO
    save_settings_blob({"roles": {"xray_analyst": {"model_id": "gemini-3.8-flash"}}})
    # No reimport, no restart.
    assert xray_config.model_analyst() == "gemini-3.8-flash"


def test_xray_config_exposes_no_frozen_model_constants():
    """Guard the regression: re-adding `MODEL_X = settings...` at module level
    would silently reintroduce the import-time freeze."""
    from rubik_core.xray import config as xray_config

    frozen = [
        name
        for name in dir(xray_config)
        if name.startswith("MODEL_") and isinstance(getattr(xray_config, name), str)
    ]
    assert frozen == [], f"frozen model constants are back: {frozen}"


# ── Deep Prep (every team, not only Sales) ──────────────────────────────────


def test_deep_prep_offers_the_three_replayed_models(admin_client):
    """2.5 Pro (the default), Argon at high thinking, and the FLASH tier: the
    three models replayed side by side on real briefs. Argon is offered only at
    high, because at its inherited level it thought 0 tokens on every brief."""
    res = admin_client.get("/api/admin/sales/catalog")
    deep = {r["key"]: r for r in res.json()["roles"]}["deep_prep"]
    assert deep["default_model"] == settings.VERTEX_LLM_MODEL_PRO
    assert deep["models"] == [
        settings.VERTEX_LLM_MODEL_PRO,
        f"{settings.VERTEX_LLM_MODEL_ARGON}@high",
        settings.VERTEX_LLM_MODEL_FLASH,
    ]
    assert deep["choices_note"]


def test_deep_prep_choice_round_trips_and_is_validated(admin_client, store):
    ok = admin_client.put(
        "/api/admin/sales/config",
        json={"roles": {"deep_prep": {"model_id": "gemini-4-argon@high"}}},
    )
    assert ok.status_code == 200, ok.text
    assert model_for("deep_prep") == "gemini-4-argon@high"
    # Plain Argon is not offered: it is the zero-thinking variant.
    bad = admin_client.put(
        "/api/admin/sales/config",
        json={"roles": {"deep_prep": {"model_id": "gemini-4-argon"}}},
    )
    assert bad.status_code == 400, bad.text

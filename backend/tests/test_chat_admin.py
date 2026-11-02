"""Tests for the chat-model admin endpoints + DB-backed chat config loader.

Covers:
  - GET /api/admin/chat/catalog returns the GA-safe model list + default
  - GET /api/admin/chat/config returns the settings default when no row exists
  - PUT /api/admin/chat/config persists and is reflected on next GET
  - PUT rejects a model_id outside the curated catalog (guards against
    re-introducing a quota-starved *-preview id)
  - load_chat_config() picks up an admin-saved model without a restart
  - Non-admin users are rejected
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from backend.auth import admin_required, get_current_user
from backend.main import app
from rubik_core.config import settings


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
        "rubik_core.agent_runtime.chat_config.SessionLocal", fake_session_local
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


# ── Endpoint tests ───────────────────────────────────────────────────────────


def test_get_catalog_returns_ga_models_and_default(admin_client):
    res = admin_client.get("/api/admin/chat/catalog")
    assert res.status_code == 200, res.text
    body = res.json()
    assert "gemini-flash-latest" in body["models"]
    # Newly-GA Gemini 3 tiers that replaced Gemini 2.5 in the chat options.
    assert "gemini-3.8-flash" in body["models"]
    assert "gemini-3.6-flash" in body["models"]  # kept as a roll-back option
    # The roll-back rung that needs no second change: 3.7 and 3.8 share the
    # re-based thinking scale, so CHAT_LLM_THINKING_LEVEL keeps its meaning
    # across that swap (dropping to 3.6 does not — see config.py).
    assert "gemini-3.7-flash" in body["models"]
    # Ruled out by the same 2026-09-17 sweep: these 404 on Vertex global, and
    # gemini-3.5-flash serves only 429s. A dropdown entry for any of them is a
    # hard failure on the turn an admin picks it.
    for dead in ("gemini-3.9-flash", "gemini-4.0-flash", "gemini-3.5-flash-lite-002"):
        assert dead not in body["models"]
    assert "gemini-3.5-flash" not in body["models"]
    assert "gemini-3.5-flash-lite" in body["models"]
    # Neither exists on Vertex (both 404) — offering one would 404 every turn.
    assert "gemini-3.8-flash-lite" not in body["models"]
    assert "gemini-3.8-pro" not in body["models"]
    # The tier default must itself be selectable, or the dropdown opens on a
    # value it would reject.
    assert settings.VERTEX_LLM_MODEL_FLASH in body["models"]
    # Retired Gemini 2.5 ids must no longer be offered.
    assert "gemini-2.5-flash" not in body["models"]
    assert "gemini-2.5-pro" not in body["models"]
    # Preview ids must NOT be offered.
    assert "gemini-3-flash-preview" not in body["models"]
    assert body["default_model"] == settings.VERTEX_LLM_MODEL_FLASH


def test_get_config_returns_default_when_no_row(admin_client, store):
    res = admin_client.get("/api/admin/chat/config")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["schema_version"] == 1
    assert body["model_id"] == settings.VERTEX_LLM_MODEL_FLASH


def test_put_config_persists_and_round_trips(admin_client, store):
    res = admin_client.put(
        "/api/admin/chat/config", json={"model_id": "gemini-3.6-flash"}
    )
    assert res.status_code == 200, res.text
    assert res.json()["model_id"] == "gemini-3.6-flash"

    # Reflected on the next GET (shares the in-memory row).
    res2 = admin_client.get("/api/admin/chat/config")
    assert res2.json()["model_id"] == "gemini-3.6-flash"


def test_put_rejects_model_outside_catalog(admin_client, store):
    res = admin_client.put(
        "/api/admin/chat/config", json={"model_id": "gemini-3-flash-preview"}
    )
    assert res.status_code == 400, res.text


def test_chat_admin_rejects_non_admin():
    prev = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = _mock_user
    client = TestClient(app)
    try:
        res = client.get("/api/admin/chat/config")
        assert res.status_code == 403
    finally:
        if prev is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = prev


def test_load_chat_config_picks_up_saved_model(store):
    from rubik_core.agent_runtime.chat_config import (
        load_chat_config,
        save_settings_blob,
    )

    save_settings_blob(
        {"model_id": "gemini-3.5-flash-lite"}, actor_email="admin@example.com"
    )
    assert load_chat_config().model_id == "gemini-3.5-flash-lite"

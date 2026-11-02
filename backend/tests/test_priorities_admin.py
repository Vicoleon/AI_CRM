"""Tests for the admin endpoints + DB-backed PrioritiesConfig loader.

Covers:
  - GET /api/admin/priorities/config returns defaults when no row exists
  - GET /api/admin/priorities/catalog returns the static catalog
  - PUT /api/admin/priorities/config persists and is reflected on next GET
  - PUT redacts api_key in the response (api_key_configured boolean only)
  - PrioritiesOrchestrator picks up admin-saved config without a restart
  - Non-admin users are rejected
"""

from __future__ import annotations

import copy
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from backend.auth import admin_required, get_current_user
from backend.main import app

# ── Test fixtures ────────────────────────────────────────────────────────────


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


# ── In-memory WorkspaceSetting stub ──────────────────────────────────────────


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
        chain.with_for_update.return_value.first = query_first

        def commit():
            # commit just flushes — we capture the row via add() / mutation
            return None

        def add(obj):
            store.row = obj

        mock_db.commit = commit
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
        "rubik_core.priorities.settings_store.SessionLocal", fake_session_local
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
    # Restore (don't clobber) other tests' overrides.
    if prev_admin is None:
        app.dependency_overrides.pop(admin_required, None)
    else:
        app.dependency_overrides[admin_required] = prev_admin
    if prev_user is None:
        app.dependency_overrides.pop(get_current_user, None)
    else:
        app.dependency_overrides[get_current_user] = prev_user


# ── Endpoint tests ───────────────────────────────────────────────────────────


def test_get_catalog_returns_roles_and_providers(admin_client):
    res = admin_client.get("/api/admin/priorities/catalog")
    assert res.status_code == 200, res.text
    body = res.json()
    assert "gmail_agent" in body["agent_roles"]
    assert "summarizer" in body["pipeline_agent_roles"]
    assert "vertex" in body["supported_providers"]
    assert "gemini-pro" in body["provider_model_catalog"]["vertex"]


def test_get_config_returns_defaults_when_no_row(admin_client, store):
    res = admin_client.get("/api/admin/priorities/config")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["schema_version"] == 1
    # Default channel agents run on gemini-flash, pipeline on gemini-pro.
    assert body["agents"]["gmail_agent"]["model_id"] == "gemini-flash"
    assert body["agents"]["critic"]["model_id"] == "gemini-pro"
    # Lowered 7.0 -> 6.0: the live distribution (mean 4.61 over a measured
    # week) made "revise" the default path at 7.0.
    assert body["policy"]["revision_threshold"] == 6.0
    # API keys must never come back raw.
    for provider, entry in body["providers"].items():
        assert "api_key" not in entry, f"raw api_key leaked for {provider}"
        assert "api_key_secret_ref" not in entry


def test_put_partial_config_persists_and_round_trips(admin_client, store):
    patch_payload = {
        "agents": {
            "summarizer": {
                "provider": "vertex",
                "model_id": "gemini-pro",
                "temperature": 0.7,
            },
        },
        "policy": {"revision_threshold": 8.0, "max_revisions": 2},
    }
    res = admin_client.put("/api/admin/priorities/config", json=patch_payload)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["agents"]["summarizer"]["temperature"] == 0.7
    assert body["policy"]["revision_threshold"] == 8.0
    assert body["policy"]["max_revisions"] == 2

    # Channel agents untouched by the partial payload keep their defaults.
    assert body["agents"]["gmail_agent"]["model_id"] == "gemini-flash"


def test_put_api_key_marks_provider_configured_but_redacts_value(
    admin_client, store, monkeypatch
):
    from backend.routers import priorities_admin

    calls = []
    monkeypatch.setattr(
        priorities_admin,
        "upsert_workspace_secret",
        lambda connector, field, value: (
            calls.append(("upsert", connector, field, value))
            or "ws-test-ai_provider-anthropic_api_key"
        ),
    )
    monkeypatch.setattr(
        priorities_admin,
        "connector_secret_ref",
        lambda *_: "ws-test-ai_provider-anthropic_api_key",
        raising=False,
    )
    res = admin_client.put(
        "/api/admin/priorities/config",
        json={"providers": {"anthropic": {"enabled": True, "api_key": "sk-secret"}}},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    provider = body["providers"]["anthropic"]
    assert provider["enabled"] is True
    assert provider["api_key_configured"] is True
    # Raw secret never echoed back to the client.
    assert "api_key" not in provider
    assert "api_key_secret_ref" not in provider
    assert calls == [
        ("upsert", "ai_provider", "anthropic_api_key", "sk-secret"),
    ]


def test_policy_save_preserves_configured_provider_key(
    admin_client, store, monkeypatch
):
    from backend.routers import priorities_admin

    ref = "ws-test-ai_provider-anthropic_api_key"
    writes = []
    clears = []
    monkeypatch.setattr(
        priorities_admin,
        "upsert_workspace_secret",
        lambda *args: writes.append(args) or ref,
    )
    monkeypatch.setattr(
        priorities_admin, "clear_workspace_secret", lambda *args: clears.append(args)
    )
    monkeypatch.setattr(
        priorities_admin,
        "connector_secret_ref",
        lambda _slug, connector, field: (
            ref if (connector, field) == ("ai_provider", "anthropic_api_key") else None
        ),
    )

    first = admin_client.put(
        "/api/admin/priorities/config",
        json={"providers": {"anthropic": {"enabled": True, "api_key": "sk-secret"}}},
    )
    assert first.status_code == 200, first.text
    public = admin_client.get("/api/admin/priorities/config").json()
    assert public["providers"]["anthropic"]["api_key_configured"] is True
    public["policy"]["revision_threshold"] = 7.0

    second = admin_client.put("/api/admin/priorities/config", json=public)
    assert second.status_code == 200, second.text
    assert second.json()["providers"]["anthropic"]["api_key_configured"] is True
    assert "api_key_secret_ref" not in store.row.value["providers"]["anthropic"]
    assert writes == [("ai_provider", "anthropic_api_key", "sk-secret")]
    assert ("ai_provider", "anthropic_api_key") not in clears

    partial = admin_client.put(
        "/api/admin/priorities/config", json={"policy": {"max_revisions": 2}}
    )
    assert partial.status_code == 200, partial.text
    assert partial.json()["providers"]["anthropic"]["api_key_configured"] is True
    assert "api_key_secret_ref" not in store.row.value["providers"]["anthropic"]


def test_stale_unconfigured_badge_cannot_clear_new_provider_key(
    admin_client, store, monkeypatch
):
    from backend.routers import priorities_admin

    ref = "ws-test-ai_provider-anthropic_api_key"
    binding = {"ref": None}
    clears = []
    monkeypatch.setattr(
        priorities_admin,
        "connector_secret_ref",
        lambda *_: binding["ref"],
    )
    monkeypatch.setattr(
        priorities_admin,
        "upsert_workspace_secret",
        lambda *_: binding.update(ref=ref) or ref,
    )
    monkeypatch.setattr(
        priorities_admin,
        "clear_workspace_secret",
        lambda *_: clears.append(True),
    )

    stale = admin_client.get("/api/admin/priorities/config").json()
    assert stale["providers"]["anthropic"]["api_key_configured"] is False
    added = admin_client.put(
        "/api/admin/priorities/config",
        json={"providers": {"anthropic": {"api_key": "new-key"}}},
    )
    assert added.status_code == 200, added.text

    stale["policy"]["revision_threshold"] = 7.0
    saved = admin_client.put("/api/admin/priorities/config", json=stale)
    assert saved.status_code == 200, saved.text
    assert saved.json()["providers"]["anthropic"]["api_key_configured"] is True
    assert "api_key_secret_ref" not in store.row.value["providers"]["anthropic"]
    assert clears == []


def test_failed_provider_write_does_not_publish_new_binding(
    admin_client, store, monkeypatch
):
    from backend.routers import priorities_admin

    ref = "ws-test-ai_provider-anthropic_api_key"
    binding = {"ref": None}
    monkeypatch.setattr(
        priorities_admin,
        "upsert_workspace_secret",
        lambda *_: (_ for _ in ()).throw(RuntimeError("Secret Manager unavailable")),
    )
    response = admin_client.put(
        "/api/admin/priorities/config",
        json={"providers": {"anthropic": {"api_key": "replacement"}}},
    )
    assert response.status_code == 503
    assert "Earlier credential changes may have applied" in response.json()["detail"]
    assert binding["ref"] is None
    assert store.row is None


def test_put_read_failure_preserves_provider_ref_and_writes_nothing(
    admin_client, store, monkeypatch
):
    from backend.routers import priorities_admin
    from rubik_core.database.models import WorkspaceSetting
    from rubik_core.priorities import settings_store

    ref = "ws-test-ai_provider-anthropic_api_key"
    current = settings_store._default_config_blob()
    current["providers"]["anthropic"]["api_key_secret_ref"] = ref
    store.row = WorkspaceSetting(key=settings_store.SETTINGS_KEY, value=current)
    before = copy.deepcopy(store.row.value)
    sessions = []
    secret_writes = []

    def failing_session_local():
        db = store.session()
        db.query.side_effect = RuntimeError("database read failed")
        db.add = MagicMock()
        db.commit = MagicMock()
        sessions.append(db)
        return _to_context(db)

    monkeypatch.setattr(settings_store, "SessionLocal", failing_session_local)
    monkeypatch.setattr(
        priorities_admin,
        "upsert_workspace_secret",
        lambda *_: secret_writes.append("upsert"),
    )

    res = admin_client.put(
        "/api/admin/priorities/config",
        json={
            "policy": {"max_revisions": 2},
            "providers": {"anthropic": {"api_key": "replacement"}},
        },
    )
    assert res.status_code == 503
    assert res.json()["detail"] == "Priorities settings are unavailable"
    assert store.row.value == before
    assert secret_writes == []
    assert len(sessions) == 1
    sessions[0].add.assert_not_called()
    sessions[0].commit.assert_not_called()


def test_malformed_saved_provider_shape_blocks_secret_write(
    admin_client, store, monkeypatch
):
    from backend.routers import priorities_admin
    from rubik_core.database.models import WorkspaceSetting
    from rubik_core.priorities.settings_store import SETTINGS_KEY

    store.row = WorkspaceSetting(key=SETTINGS_KEY, value={"providers": []})
    writes = []
    monkeypatch.setattr(
        priorities_admin,
        "upsert_workspace_secret",
        lambda *_: writes.append(True),
    )
    response = admin_client.put(
        "/api/admin/priorities/config",
        json={"providers": {"anthropic": {"api_key": "new-key"}}},
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "Priorities settings are unavailable"
    assert writes == []


@pytest.mark.parametrize(
    "malformed",
    [{"agents": []}, {"agents": {"summarizer": []}}, {"policy": []}],
)
def test_malformed_other_config_blocks_secret_write(
    admin_client, store, monkeypatch, malformed
):
    from backend.routers import priorities_admin

    writes = []
    monkeypatch.setattr(
        priorities_admin,
        "upsert_workspace_secret",
        lambda *_: writes.append(True),
    )
    response = admin_client.put(
        "/api/admin/priorities/config",
        json={**malformed, "providers": {"anthropic": {"api_key": "new-key"}}},
    )
    assert response.status_code == 400
    assert writes == []
    assert store.row is None


@pytest.mark.parametrize("providers", [[], {"anthropic": []}, {"anthropic": "enabled"}])
def test_put_rejects_malformed_provider_payloads(admin_client, store, providers):
    res = admin_client.put(
        "/api/admin/priorities/config", json={"providers": providers}
    )
    assert res.status_code == 400
    assert store.row is None


def test_invalid_later_provider_writes_no_earlier_key(admin_client, store, monkeypatch):
    from backend.routers import priorities_admin

    secret_writes = []
    monkeypatch.setattr(
        priorities_admin,
        "upsert_workspace_secret",
        lambda *_: secret_writes.append("upsert"),
    )

    res = admin_client.put(
        "/api/admin/priorities/config",
        json={
            "providers": {
                "anthropic": {"api_key": "sk-valid"},
                "openai": {"api_key": "  \n  "},
            }
        },
    )
    assert res.status_code == 400
    assert store.row is None
    assert secret_writes == []


def test_clear_provider_api_key_uses_workspace_secret_clear(
    admin_client, store, monkeypatch
):
    from backend.routers import priorities_admin

    calls = []
    monkeypatch.setattr(
        priorities_admin,
        "clear_workspace_secret",
        lambda connector, field: calls.append((connector, field)),
        raising=False,
    )
    monkeypatch.setattr(priorities_admin, "connector_secret_ref", lambda *_: None)
    res = admin_client.put(
        "/api/admin/priorities/config",
        json={"providers": {"anthropic": {"clear_api_key": True}}},
    )
    assert res.status_code == 200, res.text
    assert calls == [("ai_provider", "anthropic_api_key")]
    assert res.json()["providers"]["anthropic"]["api_key_configured"] is False


def test_stale_provider_ref_is_not_reported_configured(admin_client, monkeypatch):
    from backend.routers import priorities_admin

    monkeypatch.setattr(
        priorities_admin,
        "load_settings_blob",
        lambda: {
            "providers": {
                "anthropic": {
                    "enabled": True,
                    "api_key_secret_ref": "ws-test-ai_provider-anthropic_api_key",
                }
            }
        },
    )
    monkeypatch.setattr(
        priorities_admin,
        "connector_secret_ref",
        lambda *_: None,
        raising=False,
    )

    res = admin_client.get("/api/admin/priorities/config")
    assert res.status_code == 200
    assert res.json()["providers"]["anthropic"]["api_key_configured"] is False


def test_provider_clear_failure_never_persists_unconfigured(monkeypatch, store):
    from backend.routers import priorities_admin

    monkeypatch.setattr(
        priorities_admin,
        "clear_workspace_secret",
        lambda *_: (_ for _ in ()).throw(RuntimeError("backend unavailable")),
    )

    with pytest.raises(priorities_admin.HTTPException) as error:
        priorities_admin.put_priorities_config(
            {"providers": {"anthropic": {"clear_api_key": True}}},
            user=_mock_admin(),
        )
    assert error.value.status_code == 503
    assert "Earlier credential changes may have applied" in error.value.detail
    assert store.row is None


def test_admin_endpoint_rejects_non_admin():
    prev = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = _mock_user
    # admin_required is NOT overridden — let the real one run + reject.
    client = TestClient(app)
    try:
        res = client.get("/api/admin/priorities/config")
        assert res.status_code == 403
    finally:
        if prev is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = prev


# ── PrioritiesConfig.load() integration ──────────────────────────────────────


def test_load_priorities_config_picks_up_saved_overrides(monkeypatch, store):
    from rubik_core.priorities.settings_store import (
        load_priorities_config,
        save_settings_blob,
    )

    # Re-route SessionLocal in BOTH save + load paths to the in-memory store.
    monkeypatch.setattr(
        "rubik_core.priorities.settings_store.SessionLocal",
        lambda: _to_context(store.session()),
    )

    save_settings_blob(
        {
            "agents": {
                "decision_maker": {
                    "provider": "vertex",
                    "model_id": "gemini-flash",  # override pro → flash
                    "temperature": 0.9,
                }
            },
            "policy": {"revision_threshold": 6.5, "max_revisions": 2},
        },
        actor_email="admin@example.com",
    )

    cfg = load_priorities_config()
    assert cfg.models.decision_maker == "gemini-flash"
    assert cfg.temperatures.decision_maker == 0.9
    assert cfg.revision_threshold == 6.5
    assert cfg.max_revisions == 2
    # Roles not touched keep dataclass defaults.
    assert cfg.models.gmail_agent == "gemini-flash"  # always was


# ── Helper ───────────────────────────────────────────────────────────────────


def _to_context(db):
    """Wrap a mock db in something that supports ``with SessionLocal() as db``."""
    ctx = MagicMock()
    ctx.__enter__.return_value = db
    ctx.__exit__.return_value = False
    return ctx

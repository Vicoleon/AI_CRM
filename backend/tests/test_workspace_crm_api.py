"""Workspace-admin HubSpot configuration API keeps values in its control row."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from backend.auth import admin_required, get_current_user
from backend.main import app
from rubik_core.database.db import get_db


@pytest.fixture
def admin_client():
    admin = MagicMock(email="admin@acme.example", roles=["admin"])
    previous = {
        dependency: app.dependency_overrides.get(dependency)
        for dependency in (admin_required, get_current_user, get_db)
    }
    app.dependency_overrides[admin_required] = lambda: admin
    app.dependency_overrides[get_current_user] = lambda: admin
    app.dependency_overrides[get_db] = lambda: MagicMock()
    try:
        yield TestClient(app)
    finally:
        for dependency, old in previous.items():
            if old is None:
                app.dependency_overrides.pop(dependency, None)
            else:
                app.dependency_overrides[dependency] = old


def test_crm_config_get_and_patch_use_control_store_and_audit_fields(
    admin_client, monkeypatch
):
    from backend.routers import users
    from rubik_core.tenancy import hubspot_config

    calls = []
    data = {"pipeline_ids": ["pipeline-a"], "portal_id": "12345"}
    monkeypatch.setattr(
        hubspot_config,
        "get_hubspot_config",
        lambda: SimpleNamespace(to_dict=lambda: data),
    )
    monkeypatch.setattr(
        hubspot_config,
        "patch_hubspot_config",
        lambda changes: calls.append(changes) or SimpleNamespace(to_dict=lambda: data),
    )
    monkeypatch.setattr(users, "is_platform_admin", lambda _email: True)
    audits = []
    monkeypatch.setattr(users, "record_audit", lambda *_args, **kw: audits.append(kw))

    fetched = admin_client.get("/api/users/workspace/crm-settings")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json() == {**data, "pipeline_ids_editable": True}
    saved = admin_client.patch(
        "/api/users/workspace/crm-settings", json={"pipeline_ids": ["pipeline-a"]}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json() == data
    assert calls == [{"pipeline_ids": ["pipeline-a"]}]
    assert audits[0]["action"] == "HUBSPOT_CONFIG_UPDATE"
    assert audits[0]["details"] == {"fields": ["pipeline_ids"]}


def test_workspace_admin_cannot_expand_pipeline_access(admin_client, monkeypatch):
    from backend.routers import users
    from rubik_core.tenancy import hubspot_config

    monkeypatch.setattr(users, "is_platform_admin", lambda _email: False)
    calls = []
    monkeypatch.setattr(
        hubspot_config,
        "get_hubspot_config",
        lambda: SimpleNamespace(to_dict=lambda: {"pipeline_ids": ["pipeline-a"]}),
    )
    monkeypatch.setattr(
        hubspot_config,
        "patch_hubspot_config",
        lambda changes: (
            calls.append(changes)
            or SimpleNamespace(to_dict=lambda: {"pipeline_ids": ["pipeline-a"]})
        ),
    )
    assert admin_client.get("/api/users/workspace/crm-settings").json() == {
        "pipeline_ids": ["pipeline-a"],
        "pipeline_ids_editable": False,
    }
    denied = admin_client.patch(
        "/api/users/workspace/crm-settings", json={"pipeline_ids": ["pipeline-b"]}
    )
    assert denied.status_code == 403
    assert calls == []
    allowed = admin_client.patch(
        "/api/users/workspace/crm-settings", json={"portal_id": "12345"}
    )
    assert allowed.status_code == 200
    assert calls == [{"portal_id": "12345"}]


def test_crm_config_rejects_invalid_input_before_audit(admin_client, monkeypatch):
    from backend.routers import users
    from rubik_core.tenancy import hubspot_config

    def invalid(_changes):
        raise hubspot_config.HubSpotConfigInvalid("Unknown config field")

    monkeypatch.setattr(hubspot_config, "patch_hubspot_config", invalid)
    audits = []
    monkeypatch.setattr(users, "record_audit", lambda *_args, **kw: audits.append(kw))
    result = admin_client.patch(
        "/api/users/workspace/crm-settings", json={"api_key": "secret"}
    )
    assert result.status_code == 400
    assert audits == []


def test_crm_config_unavailable_fails_closed(admin_client, monkeypatch):
    from backend.routers import users
    from rubik_core.tenancy import hubspot_config

    def unavailable(*_args):
        raise hubspot_config.HubSpotConfigUnavailable("control unavailable")

    monkeypatch.setattr(hubspot_config, "get_hubspot_config", unavailable)
    monkeypatch.setattr(hubspot_config, "patch_hubspot_config", unavailable)
    audits = []
    monkeypatch.setattr(users, "record_audit", lambda *_args, **kw: audits.append(kw))
    assert admin_client.get("/api/users/workspace/crm-settings").status_code == 503
    assert (
        admin_client.patch("/api/users/workspace/crm-settings", json={}).status_code
        == 503
    )
    assert audits == []


def test_crm_config_requires_admin(monkeypatch):
    from backend.routers import users

    ordinary = MagicMock(email="user@acme.example", roles=["user"])
    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: ordinary
    seen = []
    monkeypatch.setattr(users, "record_audit", lambda *_args, **kw: seen.append(kw))
    try:
        client = TestClient(app)
        assert client.get("/api/users/workspace/crm-settings").status_code == 403
        assert (
            client.patch("/api/users/workspace/crm-settings", json={}).status_code
            == 403
        )
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous
    assert seen == []

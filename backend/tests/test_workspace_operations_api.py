"""Workspace operations and time-zone endpoints preserve admin boundaries."""

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
    dependencies = (admin_required, get_current_user, get_db)
    previous = {key: app.dependency_overrides.get(key) for key in dependencies}
    app.dependency_overrides[admin_required] = lambda: admin
    app.dependency_overrides[get_current_user] = lambda: admin
    app.dependency_overrides[get_db] = lambda: MagicMock()
    try:
        yield TestClient(app)
    finally:
        for key, old in previous.items():
            if old is None:
                app.dependency_overrides.pop(key, None)
            else:
                app.dependency_overrides[key] = old


def test_workspace_admin_operations_hide_and_reject_platform_grants(
    admin_client, monkeypatch
):
    from backend.routers import users
    from rubik_core.company import operations

    current = operations.OperationsConfig(
        ingestion_council_recipients=("ops@acme.example",),
        impersonation_allowed_emails=("platform@acme.example",),
        user_impersonation_targets=("rep@acme.example",),
    )
    saved = []
    monkeypatch.setattr(operations, "get_operations_config", lambda: current)
    monkeypatch.setattr(
        operations,
        "patch_operations_config",
        lambda changes: saved.append(changes) or current,
    )
    audits = []
    monkeypatch.setattr(users, "record_audit", lambda *_args, **kw: audits.append(kw))

    result = admin_client.get("/api/users/workspace/operations")
    assert result.status_code == 200, result.text
    assert result.json()["ingestion_council_recipients"] == ["ops@acme.example"]
    assert "impersonation_allowed_emails" not in result.json()
    assert "user_impersonation_targets" not in result.json()

    forbidden = admin_client.patch(
        "/api/users/workspace/operations",
        json={"impersonation_allowed_emails": ["admin@acme.example"]},
    )
    assert forbidden.status_code == 403
    assert saved == [] and audits == []

    edited = admin_client.patch(
        "/api/users/workspace/operations",
        json={"ingestion_council_department": "Operations"},
    )
    assert edited.status_code == 200, edited.text
    assert saved == [{"ingestion_council_department": "Operations"}]
    assert audits[0]["details"] == {"fields": ["ingestion_council_department"]}


def test_workspace_operations_reject_bad_and_unavailable_policy(
    admin_client, monkeypatch
):
    from rubik_core.company import operations

    def invalid(_changes):
        raise operations.OperationsConfigInvalid("Invalid recipient")

    monkeypatch.setattr(operations, "patch_operations_config", invalid)
    assert (
        admin_client.patch(
            "/api/users/workspace/operations",
            json={"ingestion_council_recipients": ["bad"]},
        ).status_code
        == 400
    )

    def unavailable(*_args):
        raise operations.OperationsConfigUnavailable("tenant unavailable")

    monkeypatch.setattr(operations, "get_operations_config", unavailable)
    monkeypatch.setattr(operations, "patch_operations_config", unavailable)
    assert admin_client.get("/api/users/workspace/operations").status_code == 503
    assert (
        admin_client.patch("/api/users/workspace/operations", json={}).status_code
        == 503
    )


def test_workspace_time_zone_api_validates_and_audits(admin_client, monkeypatch):
    from backend.routers import users
    from rubik_core.tenancy import workspace_time

    writes = []
    audits = []
    monkeypatch.setattr(
        workspace_time, "workspace_time_zone_name", lambda: "America/Costa_Rica"
    )
    monkeypatch.setattr(
        workspace_time,
        "set_workspace_time_zone",
        lambda value: writes.append(value) or value,
    )
    monkeypatch.setattr(users, "record_audit", lambda *_args, **kw: audits.append(kw))
    assert admin_client.get("/api/users/workspace/time-zone").json() == {
        "time_zone": "America/Costa_Rica"
    }
    result = admin_client.patch(
        "/api/users/workspace/time-zone", json={"time_zone": "Europe/London"}
    )
    assert result.status_code == 200, result.text
    assert result.json() == {"time_zone": "Europe/London"}
    assert writes == ["Europe/London"]
    assert audits[0]["action"] == "WORKSPACE_TIME_ZONE_UPDATE"

    def invalid(_value):
        raise workspace_time.WorkspaceTimeInvalid("Invalid IANA time zone")

    monkeypatch.setattr(workspace_time, "set_workspace_time_zone", invalid)
    assert (
        admin_client.patch(
            "/api/users/workspace/time-zone", json={"time_zone": "Mars"}
        ).status_code
        == 400
    )


def test_operations_and_time_zone_require_workspace_admin(monkeypatch):
    from backend.routers import users

    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        email="rep@acme.example", roles=["user"]
    )
    audits = []
    monkeypatch.setattr(users, "record_audit", lambda *_args, **kw: audits.append(kw))
    try:
        client = TestClient(app)
        for path in (
            "/api/users/workspace/operations",
            "/api/users/workspace/time-zone",
        ):
            assert client.get(path).status_code == 403
            assert client.patch(path, json={}).status_code == 403
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous
    assert audits == []

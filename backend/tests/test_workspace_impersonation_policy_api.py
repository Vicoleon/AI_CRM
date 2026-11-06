"""Only a live platform support grant can change person-view allowlists."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from backend.routers import platform_support
from rubik_core.company.operations import OperationsConfig
from rubik_core.tenancy.context import use_workspace
from rubik_core.tenancy.support_context import SupportScope, use_support_session


def _actor():
    return SimpleNamespace(email="platform@acme.example")


def test_ordinary_workspace_admin_cannot_edit_platform_grants():
    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = _actor
    try:
        response = TestClient(app).patch(
            "/api/platform/support/impersonation-policy",
            json={"impersonation_allowed_emails": ["admin@acme.example"]},
        )
        assert response.status_code == 403
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous


def test_platform_policy_patch_uses_live_target_and_only_grant_fields(monkeypatch):
    from rubik_core.company import operations

    monkeypatch.setattr(
        platform_support, "is_platform_admin", lambda email: email == _actor().email
    )
    saved = []
    config = OperationsConfig(
        impersonation_allowed_emails=("admin@acme.example",),
        user_impersonation_targets=("rep@acme.example",),
    )
    monkeypatch.setattr(
        operations,
        "patch_operations_config",
        lambda changes, *, platform_authorized, audit_actor, audit_sid: (
            saved.append((changes, platform_authorized, audit_actor, audit_sid))
            or config
        ),
    )
    grant = SupportScope(
        actor=_actor().email,
        sid="sid-1",
        workspace_slug="test",
        workspace_name="Test Workspace",
    )
    with use_workspace("test"), use_support_session(grant):
        result = asyncio.run(
            platform_support.update_impersonation_policy(
                {"impersonation_allowed_emails": ["admin@acme.example"]}, _actor()
            )
        )
        assert result == {
            "impersonation_allowed_emails": ["admin@acme.example"],
            "user_impersonation_targets": ["rep@acme.example"],
        }
        assert saved == [
            (
                {"impersonation_allowed_emails": ["admin@acme.example"]},
                True,
                _actor().email,
                "sid-1",
            )
        ]
        with pytest.raises(HTTPException) as error:
            asyncio.run(
                platform_support.update_impersonation_policy(
                    {"l10_sender_mailbox": ["attacker@acme.example"]}, _actor()
                )
            )
        assert error.value.status_code == 400
    assert len(saved) == 1


def test_revoked_or_foreign_support_scope_cannot_edit_policy(monkeypatch):
    monkeypatch.setattr(platform_support, "is_platform_admin", lambda _email: True)
    with use_workspace("test"):
        with pytest.raises(HTTPException) as missing:
            platform_support._require_platform_support_policy_actor(_actor())
        assert missing.value.status_code == 403

        foreign = SupportScope(
            actor=_actor().email,
            sid="sid-2",
            workspace_slug="foreign",
            workspace_name="Foreign",
        )
        with use_support_session(foreign):
            with pytest.raises(HTTPException) as mismatch:
                platform_support._require_platform_support_policy_actor(_actor())
            assert mismatch.value.status_code == 403

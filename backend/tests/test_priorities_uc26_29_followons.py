"""Tests for the UC-26 → UC-29 follow-on hooks + features.

These are wire-up changes that ride on top of UC-20→25 — small, targeted
tests confirm each hook fires and the underlying contracts are honored.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from rubik_core.priorities import (
    EntityKind,
    PriorityKind,
)

# ── UC-26: meeting prep resolves PREP_DEBT ──────────────────────────────────


@patch("rubik_core.priorities.memory.resolve")
def test_uc26_resolve_prep_debt_fires(mock_resolve):
    from backend.routers.home import _resolve_prep_debt

    _resolve_prep_debt("me@x.example", "evt-42")
    # Resolves BOTH the legacy prep_debt and the forward-looking upcoming_prep
    # kind — the dreamer now emits upcoming_prep, so prep must clear either.
    resolved_kinds = {c.kwargs["priority_kind"] for c in mock_resolve.call_args_list}
    assert resolved_kinds == {PriorityKind.PREP_DEBT, PriorityKind.UPCOMING_PREP}
    for c in mock_resolve.call_args_list:
        kwargs = c.kwargs
        assert kwargs["user_email"] == "me@x.example"
        assert kwargs["entity_kind"] == EntityKind.EVENT
        assert kwargs["entity_id"] == "evt-42"
        assert kwargs["evidence"]["kind"] == "meeting_prep"


@patch("rubik_core.priorities.memory.resolve")
def test_uc26_resolve_skipped_when_no_event_id(mock_resolve):
    from backend.routers.home import _resolve_prep_debt

    _resolve_prep_debt("me@x.example", None)
    mock_resolve.assert_not_called()


@patch("rubik_core.priorities.memory.resolve")
def test_uc26_swallows_memory_errors(mock_resolve):
    """A failing memory write must NEVER raise out of the prep callsite."""
    from backend.routers.home import _resolve_prep_debt

    mock_resolve.side_effect = RuntimeError("DB down")
    _resolve_prep_debt("me@x.example", "evt-42")  # must not raise


# ── UC-27: Gmail draft resolves FOLLOW_UP_DEBT ──────────────────────────────


@patch("rubik_core.priorities.memory.resolve")
def test_uc27_resolve_followup_debt_fires(mock_resolve):
    from backend.routers.email import _resolve_followup_debt

    _resolve_followup_debt("me@x.example", "thread-abc")
    # Drafting the outbound touch clears BOTH the legacy follow_up_debt and the
    # forward-looking pre_touchpoint_followup kind for the thread.
    resolved_kinds = {c.kwargs["priority_kind"] for c in mock_resolve.call_args_list}
    assert resolved_kinds == {
        PriorityKind.FOLLOW_UP_DEBT,
        PriorityKind.PRE_TOUCHPOINT_FOLLOWUP,
    }
    for c in mock_resolve.call_args_list:
        kwargs = c.kwargs
        assert kwargs["entity_kind"] == EntityKind.THREAD
        assert kwargs["entity_id"] == "thread-abc"
        assert kwargs["evidence"]["kind"] == "gmail_draft_created"


@patch("rubik_core.priorities.memory.resolve")
def test_uc27_swallows_memory_errors(mock_resolve):
    from backend.routers.email import _resolve_followup_debt

    mock_resolve.side_effect = RuntimeError("DB down")
    _resolve_followup_debt("me@x.example", "thread-abc")  # must not raise


# ── UC-28: Secret Manager write side ────────────────────────────────────────


@patch("rubik_core.secrets.secretmanager.SecretManagerServiceClient")
def test_uc28_put_secret_creates_container_then_adds_version(
    mock_client_cls, monkeypatch
):
    monkeypatch.setenv("GCP_PROJECT_ID", "acme-data-dev")
    mock_client = mock_client_cls.return_value
    mock_client.add_secret_version.return_value = MagicMock(
        name="projects/123/secrets/priorities-anthropic-api-key/versions/3",
    )
    mock_client.add_secret_version.return_value.name = (
        "projects/123/secrets/priorities-anthropic-api-key/versions/3"
    )

    from rubik_core.secrets import put_secret

    ref = put_secret("priorities-anthropic-api-key", "sk-fake-secret")
    assert ref and ref.endswith("/versions/3")

    # Either way, we called add_secret_version with the payload.
    mock_client.add_secret_version.assert_called_once()
    payload = mock_client.add_secret_version.call_args.kwargs["request"]["payload"]
    assert payload["data"] == b"sk-fake-secret"


def test_uc28_put_secret_no_project_returns_none(monkeypatch):
    monkeypatch.delenv("GCP_PROJECT_ID", raising=False)
    from rubik_core.secrets import put_secret

    assert put_secret("foo", "bar") is None


@patch("rubik_core.secrets.secretmanager.SecretManagerServiceClient")
def test_uc28_disable_latest_resolves_alias_before_disabling(
    mock_client_cls, monkeypatch
):
    """Secret Manager's disable endpoint does NOT accept the 'latest'
    alias — we must resolve it to a concrete version name first.
    """
    monkeypatch.setenv("GCP_PROJECT_ID", "acme-data-dev")
    mock_client = mock_client_cls.return_value
    resolved_version = MagicMock()
    resolved_version.name = "projects/p/secrets/priorities-anthropic-api-key/versions/3"
    mock_client.get_secret_version.return_value = resolved_version

    from rubik_core.secrets import disable_latest_version

    ok = disable_latest_version("priorities-anthropic-api-key")
    assert ok is True

    # The disable call must use the resolved concrete name, not the alias.
    disable_request = mock_client.disable_secret_version.call_args.kwargs["request"]
    assert disable_request["name"].endswith("/versions/3")
    assert "/versions/latest" not in disable_request["name"]


def test_uc28_admin_put_writes_secret_for_new_key(monkeypatch):
    """The admin PUT binds and writes only the selected workspace's key."""
    from fastapi.testclient import TestClient

    from backend.auth import admin_required, get_current_user
    from backend.main import app

    mock_user = MagicMock()
    mock_user.email = "admin@example.com"
    mock_user.roles = ["admin"]
    prev_admin = app.dependency_overrides.get(admin_required)
    prev_user = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[admin_required] = lambda: mock_user
    app.dependency_overrides[get_current_user] = lambda: mock_user

    ref = "ws-test-ai_provider-anthropic_api_key"
    upsert = MagicMock(return_value=ref)
    initial = {
        "schema_version": 1,
        "agents": {},
        "policy": {"revision_threshold": 7.0, "max_revisions": 1},
        "providers": {"anthropic": {"enabled": True, "api_key_secret_ref": None}},
    }

    try:
        with (
            patch("backend.routers.priorities_admin.upsert_workspace_secret", upsert),
            patch(
                "backend.routers.priorities_admin.connector_secret_ref",
                return_value=ref,
            ),
            patch(
                "backend.routers.priorities_admin.update_settings_blob",
                side_effect=lambda edit, actor_email: edit(initial),
            ),
        ):
            client = TestClient(app)
            res = client.put(
                "/api/admin/priorities/config",
                json={
                    "providers": {"anthropic": {"enabled": True, "api_key": "sk-real"}}
                },
            )
    finally:
        if prev_admin is None:
            app.dependency_overrides.pop(admin_required, None)
        else:
            app.dependency_overrides[admin_required] = prev_admin
        if prev_user is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = prev_user

    assert res.status_code == 200, res.text
    upsert.assert_called_once_with("ai_provider", "anthropic_api_key", "sk-real")
    # The response NEVER echoes the raw key.
    body = res.json()
    provider = body["providers"]["anthropic"]
    assert "api_key" not in provider
    assert "api_key_secret_ref" not in provider
    assert provider["api_key_configured"] is True


def test_uc28_admin_put_disables_secret_on_explicit_clear():
    from fastapi.testclient import TestClient

    from backend.auth import admin_required, get_current_user
    from backend.main import app

    mock_user = MagicMock()
    mock_user.email = "admin@example.com"
    mock_user.roles = ["admin"]
    prev_admin = app.dependency_overrides.get(admin_required)
    prev_user = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[admin_required] = lambda: mock_user
    app.dependency_overrides[get_current_user] = lambda: mock_user

    clear = MagicMock()
    upsert = MagicMock()
    initial = {
        "schema_version": 1,
        "agents": {},
        "policy": {},
        "providers": {
            "anthropic": {
                "enabled": True,
                "api_key_secret_ref": "ws-test-ai_provider-anthropic_api_key",
            }
        },
    }
    try:
        with (
            patch("backend.routers.priorities_admin.clear_workspace_secret", clear),
            patch("backend.routers.priorities_admin.upsert_workspace_secret", upsert),
            patch(
                "backend.routers.priorities_admin.update_settings_blob",
                side_effect=lambda edit, actor_email: edit(initial),
            ),
        ):
            client = TestClient(app)
            res = client.put(
                "/api/admin/priorities/config",
                json={
                    "providers": {
                        "anthropic": {"clear_api_key": True, "enabled": False}
                    }
                },
            )
    finally:
        if prev_admin is None:
            app.dependency_overrides.pop(admin_required, None)
        else:
            app.dependency_overrides[admin_required] = prev_admin
        if prev_user is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = prev_user

    assert res.status_code == 200, res.text
    upsert.assert_not_called()
    clear.assert_called_once_with("ai_provider", "anthropic_api_key")


# ── UC-29: Drive recent-activity extractor ───────────────────────────────────


def test_uc29_drive_agent_parses_signals_with_entity_tags():
    from rubik_core.priorities.agents import DriveChannelAgent
    from rubik_core.priorities.config import PrioritiesConfig

    raw = [
        {
            "id": "f-1",
            "name": "Acme proposal v3.docx",
            "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "modifiedTime": "2026-05-26T18:00:00Z",
            "createdTime": "2026-05-24T10:00:00Z",
            "lastModifyingUser": {"emailAddress": "ceo@acmecorp.example"},
            "owners": [{"emailAddress": "me@acme.example"}],
            "shared": True,
            "webViewLink": "https://docs.google.com/document/d/f-1/edit",
            "viewedByMe": True,
            "viewedByMeTime": "2026-05-26T18:30:00Z",
        },
    ]
    agent = DriveChannelAgent(
        user_email="me@acme.example",
        config=PrioritiesConfig.default(),
    )
    signals = agent.parse_signals(raw)
    assert len(signals) == 1
    s = signals[0]
    assert s.entity_kind == EntityKind.DOCUMENT
    assert s.entity_id == "f-1"
    assert s.attrs["shared"] is True
    assert s.attrs["owned_by_user"] is True
    assert s.attrs["last_modifier_email"] == "ceo@acmecorp.example"
    assert s.attrs["modified_by_user"] is False


def test_uc29_drive_prompt_passes_no_judgment_regression():
    """The new Drive extractor must obey the UC-22 no-judgment rule."""
    import re

    from rubik_core.priorities.agents import DriveChannelAgent
    from rubik_core.priorities.config import PrioritiesConfig

    agent = DriveChannelAgent(
        user_email="me@acme.example",
        config=PrioritiesConfig.default(),
    )
    prompt = agent.build_prompt([])
    forbidden = re.compile(
        r"\bimportant\b|\burgent\b|\bflag\b|\bcritical\b|\bpriority\b",
        re.IGNORECASE,
    )
    matches = forbidden.findall(prompt)
    assert not matches, f"judgment vocabulary leaked: {matches}"


def test_uc29_drive_added_to_default_channels():
    from rubik_core.priorities.models import ChannelKind, PrioritiesRequest

    req = PrioritiesRequest(user_email="me@x.example")
    assert ChannelKind.DRIVE in req.channels

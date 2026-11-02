"""Additional Gmail OAuth accounts stay browser, user and workspace bound."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, text

from rubik_core.config import settings
from rubik_core.control.db import control_session
from rubik_core.control.models import Workspace
from rubik_core.database.db import SessionLocal, get_engine
from rubik_core.database.models import (
    Base,
    GmailAccount,
    GmailOAuthAttempt,
    Notification,
    User,
    WorkspaceSetting,
)
from rubik_core.database.repository import SQLUserRegistry
from rubik_core.tenancy import gmail_accounts as gmail_accounts_module
from rubik_core.tenancy.context import use_workspace
from rubik_core.tenancy.gmail_accounts import (
    GMAIL_COMPOSE_SCOPE,
    GMAIL_READ_SCOPE,
    GmailOAuthRejected,
    acknowledge_ai_provider,
    complete_connection,
    disconnect_account,
    get_account_credentials,
    list_accounts,
    mark_account_reauth_required,
    multi_gmail_enabled,
)
from rubik_core.tenancy.gmail_accounts import (
    begin_connection as begin_connection_with_consent,
)
from rubik_core.tenancy.registry import clear_engines

OWNER = "gmail-owner@acme.example"
OTHER = "gmail-other@acme.example"
MAILBOX = "private.owner@gmail.com"
_BROWSER = "browser-nonce-for-owner-very-long-and-private"


def begin_connection(
    owner_email: str, browser_nonce: str, expected_ai_fingerprint: str | None = None
) -> str:
    return begin_connection_with_consent(
        owner_email,
        browser_nonce,
        expected_ai_fingerprint or gmail_accounts_module._ai_processing_route()[2],
    )


@pytest.fixture
def gmail_setup(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "LOCAL_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite:///{tmp_path / 'tenant.db'}")
    monkeypatch.setattr(
        settings, "WORKSPACE_DATABASE_DIR", str(tmp_path / "workspaces")
    )
    monkeypatch.setattr(
        settings,
        "GOOGLE_GMAIL_OAUTH_CLIENT_ID",
        "gmail-client.apps.googleusercontent.com",
    )
    monkeypatch.setattr(settings, "GOOGLE_GMAIL_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setattr(
        settings,
        "GOOGLE_GMAIL_OAUTH_REDIRECT_URI",
        "http://127.0.0.1:8001/api/email/accounts/callback",
    )
    clear_engines()
    with use_workspace("test"):
        Base.metadata.create_all(get_engine())
        with SessionLocal() as db:
            existing = db.get(WorkspaceSetting, "general")
            previous_settings = deepcopy(existing.value) if existing else None
            db.add_all(
                [
                    User(email=OWNER, id="gmail-owner", name="Owner"),
                    User(email=OTHER, id="gmail-other", name="Other"),
                ]
            )
            db.commit()
        try:
            yield
        finally:
            with SessionLocal() as db:
                db.execute(
                    delete(GmailOAuthAttempt).where(
                        GmailOAuthAttempt.owner_email.in_((OWNER, OTHER))
                    )
                )
                db.execute(
                    delete(GmailAccount).where(
                        GmailAccount.owner_email.in_((OWNER, OTHER))
                    )
                )
                db.execute(delete(User).where(User.email.in_((OWNER, OTHER))))
                setting = db.get(WorkspaceSetting, "general")
                if previous_settings is None:
                    if setting is not None:
                        db.delete(setting)
                elif setting is not None:
                    setting.value = previous_settings
                else:
                    db.add(WorkspaceSetting(key="general", value=previous_settings))
                db.commit()
    clear_engines()


def _enabled() -> None:
    SQLUserRegistry().update_workspace_settings({"multi_gmail_enabled": True})


def _state(url: str) -> str:
    return parse_qs(urlparse(url).query)["state"][0]


def _google(
    monkeypatch, *, sub="google-sub-1", mailbox=MAILBOX, refresh="refresh-secret"
):
    from rubik_core.tenancy import gmail_accounts

    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/revoke"):
            return SimpleNamespace(status_code=200)
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "access_token": "access-secret",
                "refresh_token": refresh,
                "scope": f"openid email {GMAIL_READ_SCOPE} {GMAIL_COMPOSE_SCOPE}",
            },
        )

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"sub": sub, "email": mailbox, "email_verified": True},
        )

    monkeypatch.setattr(gmail_accounts.requests, "post", post)
    monkeypatch.setattr(gmail_accounts.requests, "get", get)
    return calls


def test_workspace_admin_option_is_off_until_enabled(gmail_setup):
    assert multi_gmail_enabled() is False
    with pytest.raises(PermissionError, match="disabled"):
        begin_connection(OWNER, _BROWSER)
    _enabled()
    assert multi_gmail_enabled() is True
    assert SQLUserRegistry().get_workspace_settings()["multi_gmail_enabled"] is True


def test_connection_requires_browser_cookie_and_state_is_one_use(
    gmail_setup, monkeypatch
):
    _enabled()
    calls = _google(monkeypatch)
    url = begin_connection(OWNER, _BROWSER)
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    params = parse_qs(urlparse(url).query)
    assert params["access_type"] == ["offline"]
    assert params["code_challenge_method"] == ["S256"]
    state = _state(url)
    with pytest.raises(PermissionError, match="browser binding"):
        complete_connection(state, "code", None)
    with pytest.raises(PermissionError, match="browser binding"):
        complete_connection(
            state, "code", "another-browser-nonce-with-enough-characters"
        )
    assert calls == []
    result = complete_connection(state, "code", _BROWSER)
    assert result["email"] == MAILBOX
    with pytest.raises(PermissionError, match="already used"):
        complete_connection(state, "code", _BROWSER)
    with pytest.raises(PermissionError, match="Invalid Gmail authorization state"):
        complete_connection(state + "forged", "code", _BROWSER)
    assert len([url for url, _ in calls if url.endswith("/token")]) == 1


def test_refresh_token_is_encrypted_and_other_user_cannot_use_it(
    gmail_setup, monkeypatch
):
    _enabled()
    calls = _google(monkeypatch)
    result = complete_connection(
        _state(begin_connection(OWNER, _BROWSER)), "code", _BROWSER
    )
    with get_engine().connect() as connection:
        raw = connection.execute(
            text("SELECT refresh_token FROM gmail_accounts WHERE id = :id"),
            {"id": result["id"]},
        ).scalar_one()
    assert raw != "refresh-secret"
    assert "refresh-secret" not in raw
    assert list_accounts(OWNER) == [result]
    credential, mailbox = get_account_credentials(OWNER, result["id"])
    assert credential.refresh_token == "refresh-secret"
    assert mailbox == MAILBOX
    with pytest.raises(PermissionError, match="unavailable"):
        get_account_credentials(OTHER, result["id"])
    mark_account_reauth_required(
        OTHER, result["id"], failed_refresh_token="refresh-secret"
    )
    assert list_accounts(OWNER)[0]["status"] == "connected"
    SQLUserRegistry().update_workspace_settings({"multi_gmail_enabled": False})
    with pytest.raises(PermissionError, match="disabled"):
        get_account_credentials(OWNER, result["id"])
    _enabled()
    mark_account_reauth_required(OWNER, result["id"], failed_refresh_token="old-token")
    assert list_accounts(OWNER)[0]["status"] == "connected"
    mark_account_reauth_required(
        OWNER, result["id"], failed_refresh_token="refresh-secret"
    )
    assert list_accounts(OWNER)[0]["status"] == "reauth_required"
    with pytest.raises(PermissionError, match="unavailable"):
        get_account_credentials(OWNER, result["id"])
    assert list_accounts(OTHER) == []
    assert disconnect_account(OTHER, result["id"]) is False
    assert disconnect_account(OWNER, result["id"]) is True
    with pytest.raises(PermissionError, match="unavailable"):
        get_account_credentials(OWNER, result["id"])
    assert calls[-1][0] == "https://oauth2.googleapis.com/revoke"


def test_reconnect_deduplicates_verified_google_identity(gmail_setup, monkeypatch):
    _enabled()
    _google(monkeypatch)
    first = complete_connection(
        _state(begin_connection(OWNER, _BROWSER)), "code", _BROWSER
    )
    second = complete_connection(
        _state(begin_connection(OWNER, _BROWSER)), "code", _BROWSER
    )
    assert first == second
    assert len(list_accounts(OWNER)) == 1


def test_provider_change_pauses_personal_ai_until_owner_reviews(
    gmail_setup, monkeypatch
):
    from rubik_core.priorities import PrioritiesConfig
    from rubik_core.priorities.agents.gmail_agent import GmailChannelAgent
    from rubik_core.tenancy import gmail_accounts

    _enabled()
    _google(monkeypatch)
    current = ["vertex_managed", "managed-model", "a" * 64]
    monkeypatch.setattr(gmail_accounts, "_ai_processing_route", lambda: tuple(current))
    with use_workspace("test"):
        result = complete_connection(
            _state(begin_connection(OWNER, _BROWSER)), "code", _BROWSER
        )
        assert result["ai_processing_review_required"] is False

        current[:] = ["anthropic", "claude-sonnet-4-6", "b" * 64]
        paused = list_accounts(OWNER)[0]
        assert paused["status"] == "connected"
        assert paused["ai_processing_review_required"] is True
        assert paused["ai_provider"] == "anthropic"
        with SessionLocal() as db:
            notices = (
                db.query(Notification)
                .filter(
                    Notification.actor_email == OWNER,
                    Notification.kind == "gmail_ai_provider_review",
                )
                .all()
            )
            assert len(notices) == 1
            assert "claude-sonnet-4-6" in notices[0].body
            assert "refresh-secret" not in notices[0].body
        list_accounts(OWNER)
        with SessionLocal() as db:
            assert (
                db.query(Notification)
                .filter(
                    Notification.actor_email == OWNER,
                    Notification.kind == "gmail_ai_provider_review",
                )
                .count()
                == 1
            )
        assert get_account_credentials(OWNER, result["id"])[1] == MAILBOX

        called_accounts = []

        class GmailClient:
            def __init__(self, *, user_email, account_id=None):
                assert user_email == OWNER
                called_accounts.append(account_id)

            def search_threads(self, *, query, max_results):
                return []

        monkeypatch.setattr("rubik_core.tools.gmail.GmailClient", GmailClient)
        monkeypatch.setattr(
            "rubik_core.priorities.agents._google_auth.require_live_client",
            lambda *_args, **_kwargs: None,
        )
        agent = GmailChannelAgent(user_email=OWNER, config=PrioritiesConfig.default())
        agent.fetch_signals()
        assert called_accounts == [None]
        assert agent._provider_review_pending == [result["id"]]

        with pytest.raises(PermissionError):
            acknowledge_ai_provider(
                OTHER, result["id"], paused["ai_provider_fingerprint"]
            )
        with pytest.raises(ValueError, match="changed"):
            acknowledge_ai_provider(OWNER, result["id"], "a" * 64)
        approved = acknowledge_ai_provider(
            OWNER, result["id"], paused["ai_provider_fingerprint"]
        )
        assert approved["ai_processing_review_required"] is False
        assert list_accounts(OWNER)[0]["ai_processing_review_required"] is False
        agent.fetch_signals()
        assert called_accounts[-2:] == [None, result["id"]]


def test_provider_switch_during_google_oauth_requires_review(gmail_setup, monkeypatch):
    from rubik_core.tenancy import gmail_accounts

    _enabled()
    _google(monkeypatch)
    current = ["vertex_managed", "managed-model", "a" * 64]
    monkeypatch.setattr(gmail_accounts, "_ai_processing_route", lambda: tuple(current))
    state = _state(begin_connection(OWNER, _BROWSER, "a" * 64))
    current[:] = ["openai", "gpt-6-luna", "b" * 64]
    connected = complete_connection(state, "code", _BROWSER)
    assert connected["status"] == "connected"
    assert connected["ai_processing_review_required"] is True
    assert list_accounts(OWNER)[0]["ai_processing_review_required"] is True
    with pytest.raises(ValueError, match="changed"):
        begin_connection(OWNER, _BROWSER, "a" * 64)


def test_unavailable_selected_provider_never_fetches_personal_mail(
    gmail_setup, monkeypatch
):
    from rubik_core.ai_providers import registry
    from rubik_core.priorities import PrioritiesConfig
    from rubik_core.priorities.agents.gmail_agent import GmailChannelAgent

    _enabled()
    _google(monkeypatch)
    with use_workspace("test"):
        connected = complete_connection(
            _state(begin_connection(OWNER, _BROWSER)), "code", _BROWSER
        )

        def unavailable(*_args, **_kwargs):
            raise registry.IncompatibleModelRoute("Selected provider disconnected")

        monkeypatch.setattr(registry, "resolve_model_route", unavailable)
        listed = list_accounts(OWNER)[0]
        assert listed["ai_processing_review_required"] is False
        assert listed["ai_processing_unavailable"] is True
        with pytest.raises(ValueError, match="unavailable"):
            acknowledge_ai_provider(
                OWNER, connected["id"], connected["ai_provider_fingerprint"]
            )
        with pytest.raises(ValueError, match="unavailable"):
            begin_connection(OWNER, _BROWSER, connected["ai_provider_fingerprint"])

        called_accounts = []

        class GmailClient:
            def __init__(self, *, user_email, account_id=None):
                called_accounts.append(account_id)

            def search_threads(self, *, query, max_results):
                return []

        monkeypatch.setattr("rubik_core.tools.gmail.GmailClient", GmailClient)
        monkeypatch.setattr(
            "rubik_core.priorities.agents._google_auth.require_live_client",
            lambda *_args, **_kwargs: None,
        )
        agent = GmailChannelAgent(user_email=OWNER, config=PrioritiesConfig.default())
        agent.fetch_signals()
        assert called_accounts == [None]
        assert agent._provider_unavailable is True


def test_route_switch_after_personal_mail_fetch_blocks_model_call(
    gmail_setup, monkeypatch
):
    from datetime import datetime, timezone

    from rubik_core.ai_providers import registry
    from rubik_core.ai_providers.registry import IncompatibleModelRoute, ModelRoute
    from rubik_core.priorities import PrioritiesConfig
    from rubik_core.priorities.agents.gmail_agent import GmailChannelAgent
    from rubik_core.priorities.models import (
        ChannelDigest,
        ChannelKind,
        PrioritiesRequest,
    )
    from rubik_core.priorities.orchestrator import PrioritiesOrchestrator
    from rubik_core.priorities.temporal import TemporalContext
    from rubik_core.tenancy import gmail_accounts

    _enabled()
    _google(monkeypatch)
    selected = ["vertex_managed", "managed-model", "a" * 64]
    monkeypatch.setattr(gmail_accounts, "_ai_processing_route", lambda: tuple(selected))

    class GmailClient:
        def __init__(self, *, user_email, account_id=None):
            pass

        def search_threads(self, *, query, max_results):
            return []

    monkeypatch.setattr("rubik_core.tools.gmail.GmailClient", GmailClient)
    monkeypatch.setattr(
        "rubik_core.priorities.agents._google_auth.require_live_client",
        lambda *_args, **_kwargs: None,
    )

    with use_workspace("test"):
        complete_connection(_state(begin_connection(OWNER, _BROWSER)), "code", _BROWSER)
        agent = GmailChannelAgent(user_email=OWNER, config=PrioritiesConfig.default())
        agent.fetch_signals()
        assert agent.personal_mail_guard is not None
        monkeypatch.setattr(
            registry,
            "resolve_model_route",
            lambda feature, _capability, *, default_model: ModelRoute(
                "test",
                feature,
                selected[0],
                selected[1] if selected[0] != "vertex_managed" else default_model,
                "client" if selected[0] != "vertex_managed" else "cubie",
            ),
        )

        class NeverCallModel:
            @property
            def models(self):
                raise AssertionError("Personal mail reached a model")

        agent.client._client = NeverCallModel()
        selected[:] = ["openai", "gpt-6-luna", "b" * 64]
        with pytest.raises(IncompatibleModelRoute, match="consent changed"):
            agent.client.invoke_json("fetched personal mail")

        selected[:] = ["vertex_managed", "managed-model", "a" * 64]
        with SessionLocal() as db:
            account = db.get(GmailAccount, agent.personal_mail_guard.account_ids[0])
            account.ai_provider_ack = None
            db.commit()
        with pytest.raises(IncompatibleModelRoute, match="consent changed"):
            agent.client.invoke_json("fetched personal mail")
        with SessionLocal() as db:
            account = db.get(GmailAccount, agent.personal_mail_guard.account_ids[0])
            account.ai_provider_ack = "a" * 64
            db.commit()

        orchestrator = PrioritiesOrchestrator(config=PrioritiesConfig.default())
        orchestrator.summarizer.client._client = NeverCallModel()
        monkeypatch.setattr(
            "rubik_core.priorities.orchestrator.resolve_user_now",
            lambda *_args, **_kwargs: TemporalContext(
                now_utc=datetime(2026, 10, 30, 12, tzinfo=timezone.utc),
                now_local=datetime(2026, 10, 30, 12, tzinfo=timezone.utc),
                tz_name="UTC",
            ),
        )
        monkeypatch.setattr(
            "rubik_core.availability.pto.detect_pto",
            lambda *_args, **_kwargs: SimpleNamespace(on_pto_today=False),
        )

        def fetched_then_switched(_request, _audit, _temporal):
            orchestrator._personal_mail_guard = agent.personal_mail_guard
            selected[:] = ["openai", "gpt-6-luna", "b" * 64]
            return [ChannelDigest(channel=ChannelKind.GMAIL, narrative="private mail")]

        monkeypatch.setattr(orchestrator, "_run_channels", fetched_then_switched)
        with pytest.raises(IncompatibleModelRoute, match="consent changed"):
            orchestrator.generate(
                PrioritiesRequest(user_email=OWNER, channels=[ChannelKind.GMAIL])
            )


def test_missing_scope_or_unverified_email_is_never_saved(gmail_setup, monkeypatch):
    from rubik_core.tenancy import gmail_accounts

    _enabled()
    _google(monkeypatch)
    monkeypatch.setattr(
        gmail_accounts.requests,
        "post",
        lambda *a, **kw: SimpleNamespace(
            status_code=200,
            json=lambda: {
                "access_token": "access",
                "refresh_token": "refresh",
                "scope": GMAIL_READ_SCOPE,
            },
        ),
    )
    with pytest.raises(GmailOAuthRejected, match="Required Gmail access"):
        complete_connection(_state(begin_connection(OWNER, _BROWSER)), "code", _BROWSER)
    assert list_accounts(OWNER) == []
    monkeypatch.setattr(
        gmail_accounts.requests,
        "post",
        lambda *a, **kw: SimpleNamespace(
            status_code=200,
            json=lambda: {
                "access_token": "access",
                "refresh_token": "refresh",
                "scope": f"{GMAIL_READ_SCOPE} {GMAIL_COMPOSE_SCOPE}",
            },
        ),
    )
    monkeypatch.setattr(
        gmail_accounts.requests,
        "get",
        lambda *a, **kw: SimpleNamespace(
            status_code=200,
            json=lambda: {
                "sub": "google-sub",
                "email": MAILBOX,
                "email_verified": False,
            },
        ),
    )
    with pytest.raises(GmailOAuthRejected, match="verification failed"):
        complete_connection(_state(begin_connection(OWNER, _BROWSER)), "code", _BROWSER)
    assert list_accounts(OWNER) == []


def test_default_work_mailbox_cannot_be_added_as_extra(gmail_setup, monkeypatch):
    _enabled()
    _google(monkeypatch, mailbox=OWNER)
    with pytest.raises(GmailOAuthRejected, match="already the default"):
        complete_connection(_state(begin_connection(OWNER, _BROWSER)), "code", _BROWSER)
    assert list_accounts(OWNER) == []


def test_account_id_cannot_open_another_workspace(gmail_setup, monkeypatch):
    _enabled()
    state = _state(begin_connection(OWNER, _BROWSER))
    _google(monkeypatch)
    result = complete_connection(state, "code", _BROWSER)
    with control_session() as db:
        db.add(Workspace(slug="gmail-other", name="Other", status="active"))
        db.commit()
    try:
        with use_workspace("gmail-other"):
            Base.metadata.create_all(get_engine())
            with SessionLocal() as db:
                db.add(User(email=OWNER, id="other-owner", name="Owner"))
                db.commit()
            SQLUserRegistry().update_workspace_settings({"multi_gmail_enabled": True})
            assert list_accounts(OWNER) == []
            with pytest.raises(PermissionError, match="unavailable"):
                get_account_credentials(OWNER, result["id"])
            with pytest.raises(PermissionError, match="unavailable"):
                acknowledge_ai_provider(
                    OWNER, result["id"], result["ai_provider_fingerprint"]
                )
        assert list_accounts(OWNER) == [result]
    finally:
        clear_engines()
        with control_session() as db:
            db.delete(db.get(Workspace, "gmail-other"))
            db.commit()


def test_http_callback_requires_same_browser_cookie(gmail_setup, monkeypatch):
    from backend.auth import get_current_user
    from backend.main import app
    from backend.routers import gmail_accounts as accounts_router
    from rubik_core.auth.registry import UserProfile
    from rubik_core.tenancy import gmail_accounts

    _enabled()
    _google(monkeypatch)
    current = ["vertex_managed", "managed-model", "a" * 64]
    monkeypatch.setattr(gmail_accounts, "_ai_processing_route", lambda: tuple(current))
    monkeypatch.setattr(accounts_router, "_ai_processing_route", lambda: tuple(current))
    profile = UserProfile(
        id="gmail-owner",
        email=OWNER,
        name="Owner",
        roles=["user"],
        created_at="2026-10-01T00:00:00",
        updated_at="2026-10-01T00:00:00",
    )
    app.dependency_overrides[get_current_user] = lambda: profile
    try:
        client = TestClient(app)
        settings_response = client.get("/api/email/accounts")
        assert settings_response.status_code == 200
        assert client.post("/api/email/accounts/connect").status_code == 422
        started = client.post(
            "/api/email/accounts/connect",
            json={"fingerprint": settings_response.json()["ai_provider_fingerprint"]},
        )
        assert started.status_code == 200, started.text
        assert "HttpOnly" in started.headers["set-cookie"]
        assert "SameSite=lax" in started.headers["set-cookie"]
        state = _state(started.json()["authorization_url"])
        forged_browser = TestClient(app)
        refused = forged_browser.get(
            "/api/email/accounts/callback",
            params={"code": "code", "state": state},
            follow_redirects=False,
        )
        assert refused.status_code == 401
        connected = client.get(
            "/api/email/accounts/callback",
            params={"code": "code", "state": state},
            follow_redirects=False,
        )
        assert connected.status_code == 302, connected.text
        assert "gmail_connection=connected" in connected.headers["location"]
        assert "refresh-secret" not in connected.text
        listed = client.get("/api/email/accounts")
        assert listed.status_code == 200, listed.text
        assert listed.json()["accounts"][0]["email"] == MAILBOX
        assert "refresh-secret" not in listed.text
        account_id = listed.json()["accounts"][0]["id"]
        current[:] = ["anthropic", "claude-sonnet-4-6", "b" * 64]
        changed = client.get("/api/email/accounts")
        assert changed.json()["accounts"][0]["ai_processing_review_required"] is True
        stale = client.post(
            f"/api/email/accounts/{account_id}/ai-provider-consent",
            json={"fingerprint": "a" * 64},
        )
        assert stale.status_code == 409
        reviewed = client.post(
            f"/api/email/accounts/{account_id}/ai-provider-consent",
            json={"fingerprint": "b" * 64},
        )
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["ai_processing_review_required"] is False
        other = UserProfile(
            id="gmail-other",
            email=OTHER,
            name="Other",
            roles=["user"],
            created_at="2026-10-01T00:00:00",
            updated_at="2026-10-01T00:00:00",
        )
        app.dependency_overrides[get_current_user] = lambda: other
        refused = client.post(
            f"/api/email/accounts/{account_id}/ai-provider-consent",
            json={"fingerprint": "b" * 64},
        )
        assert refused.status_code == 404
    finally:
        app.dependency_overrides.pop(get_current_user, None)

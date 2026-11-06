"""Slack OAuth state, installations, and tokens stay in one workspace."""

from __future__ import annotations

import base64
import os
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.main import app
from backend.services.slack_service import SlackAPIError
from rubik_core.auth.session_tokens import SessionClaims
from rubik_core.config import AppMode, settings
from rubik_core.control.db import control_session
from rubik_core.control.models import (
    SlackInstallation,
    Workspace,
    WorkspaceConnector,
    WorkspaceDomain,
)
from rubik_core.control.repository import clear_module_cache
from rubik_core.database.db import SessionLocal, get_engine
from rubik_core.database.models import Base, SlackCredential, User
from rubik_core.tenancy.context import use_workspace
from rubik_core.tenancy.registry import clear_engines
from rubik_core.tenancy.secrets import upsert_workspace_secret, workspace_secret

CLIENT = TestClient(app)
SLUGS = ("alpha-oauth", "beta-oauth")


@pytest.fixture
def oauth_workspaces(tmp_path, monkeypatch):
    from backend.routers import slack

    # Other tests may have cached a disabled module for these same slugs on
    # this xdist worker. This fixture creates fresh active workspaces.
    clear_module_cache()
    monkeypatch.setattr(settings, "APP_MODE", AppMode.LOCAL)
    monkeypatch.setattr(settings, "CLOUDSQL_CONNECTION_NAME", "")
    monkeypatch.setattr(settings, "ENABLE_MOCK_AUTH", False)
    monkeypatch.setattr(settings, "WORKSPACE_DATABASE_DIR", str(tmp_path / "dbs"))
    monkeypatch.setattr(settings, "LOCAL_DATA_DIR", str(tmp_path / "secrets"))
    monkeypatch.setenv(
        "CUBIE_OAUTH_STATE_SECRET", base64.urlsafe_b64encode(os.urandom(32)).decode()
    )
    monkeypatch.delenv("CUBIE_PERSONAL_TOKEN_FERNET_KEY", raising=False)
    with control_session() as db:
        for slug in SLUGS:
            db.add(Workspace(slug=slug, name=slug, status="active"))
            db.add(
                WorkspaceDomain(
                    domain=f"{slug}.example", workspace_slug=slug, is_primary=True
                )
            )
            db.add(
                WorkspaceConnector(
                    workspace_slug=slug,
                    connector="slack",
                    config={
                        "client_id": f"C_{slug}",
                        "redirect_uri": "http://127.0.0.1:8001/api/slack/callback",
                    },
                )
            )
        db.commit()
    for slug in SLUGS:
        with use_workspace(slug):
            upsert_workspace_secret("slack", "client_secret", f"client-{slug}")
            upsert_workspace_secret("slack", "signing_secret", f"signing-{slug}")
            Base.metadata.create_all(get_engine())
            with SessionLocal() as db:
                db.add(
                    User(
                        email=f"owner@{slug}.example",
                        id=f"user-{slug}",
                        name="Owner",
                        created_at=datetime.utcnow(),
                    )
                )
                db.commit()
    lookup_calls = []

    class StubBot:
        def __init__(self, bot_token, *, timeout, max_retries):
            assert timeout == 5.0
            assert max_retries == 1
            self.bot_token = bot_token

        def lookup_user_id(self, email):
            lookup_calls.append((self.bot_token, email))
            return f"U_{self.bot_token.removeprefix('xoxb-private-')}"

    monkeypatch.setattr(slack, "SlackBotClient", StubBot)
    try:
        yield lookup_calls
    finally:
        clear_module_cache()
        clear_engines()
        with control_session() as db:
            for slug in SLUGS:
                for row in list(
                    db.query(SlackInstallation).filter(
                        SlackInstallation.workspace_slug == slug
                    )
                ):
                    db.delete(row)
                connector = db.get(WorkspaceConnector, (slug, "slack"))
                if connector is not None:
                    db.delete(connector)
                domain = db.get(WorkspaceDomain, f"{slug}.example")
                if domain is not None:
                    db.delete(domain)
            db.flush()
            for slug in SLUGS:
                row = db.get(Workspace, slug)
                if row is not None:
                    db.delete(row)
            db.commit()


def _slack_response(slug: str) -> dict:
    return {
        "ok": True,
        "app_id": f"A_{slug}",
        "team": {"id": f"T_{slug}", "name": slug},
        "authed_user": {
            "id": f"U_{slug}",
            "access_token": f"xoxp-private-{slug}",
            "scope": "search:read",
        },
        "access_token": f"xoxb-private-{slug}",
        "scope": "chat:write",
    }


def test_oauth_state_needs_explicit_strong_platform_secret(
    oauth_workspaces, monkeypatch
):
    from backend.routers import slack

    email = "owner@alpha-oauth.example"
    with use_workspace("alpha-oauth"):
        good = slack._sign_state(email, "alpha-oauth.example")
    assert slack._parse_state(good) == ("alpha-oauth", email, "alpha-oauth.example")
    monkeypatch.delenv("CUBIE_OAUTH_STATE_SECRET")
    with use_workspace("alpha-oauth"), pytest.raises(RuntimeError):
        slack._sign_state(email, "alpha-oauth.example")
    with pytest.raises(RuntimeError):
        slack._parse_state(good)
    monkeypatch.setenv("CUBIE_OAUTH_STATE_SECRET", "weak-secret")
    with use_workspace("alpha-oauth"), pytest.raises(RuntimeError):
        slack._sign_state(email, "alpha-oauth.example")


def test_oauth_state_rejects_forgery_expiry_and_wrong_hosted_domain(
    oauth_workspaces, monkeypatch
):
    from backend.routers import slack

    now = 1_800_000_000
    monkeypatch.setattr(slack.time, "time", lambda: now)
    with use_workspace("alpha-oauth"):
        state = slack._sign_state("owner@alpha-oauth.example", "alpha-oauth.example")
    assert slack._parse_state(state) == (
        "alpha-oauth",
        "owner@alpha-oauth.example",
        "alpha-oauth.example",
    )
    assert slack._parse_state(state[:-1] + ("0" if state[-1] != "0" else "1")) is None
    monkeypatch.setattr(slack.time, "time", lambda: now + 601)
    assert slack._parse_state(state) is None
    with use_workspace("alpha-oauth"), pytest.raises(ValueError):
        slack._sign_state("owner@alpha-oauth.example", "beta-oauth.example")


def test_callback_refuses_invalid_state_before_tenant_db(oauth_workspaces, monkeypatch):
    from backend.routers import slack

    monkeypatch.setattr(
        slack, "SessionLocal", lambda: pytest.fail("invalid state opened tenant DB")
    )
    response = CLIENT.get(
        "/api/slack/callback",
        params={"code": "code", "state": "forged"},
        follow_redirects=False,
    )
    assert response.status_code == 401


@pytest.mark.parametrize("change", ["domain", "suspend"])
def test_callback_rechecks_active_workspace_and_domain_before_tenant_db(
    oauth_workspaces, monkeypatch, change
):
    from backend.routers import slack

    with use_workspace("alpha-oauth"):
        state = slack._sign_state("owner@alpha-oauth.example", "alpha-oauth.example")
    with control_session() as db:
        if change == "domain":
            db.delete(db.get(WorkspaceDomain, "alpha-oauth.example"))
        else:
            db.get(Workspace, "alpha-oauth").status = "suspended"
        db.commit()
    monkeypatch.setattr(
        slack, "SessionLocal", lambda: pytest.fail("revoked state opened tenant DB")
    )
    monkeypatch.setattr(
        slack,
        "exchange_code_for_token",
        lambda *args: pytest.fail("revoked state exchanged code"),
    )
    response = CLIENT.get(
        "/api/slack/callback",
        params={"code": "code", "state": state},
        follow_redirects=False,
    )
    assert response.status_code == 401


def test_callback_installs_only_selected_workspace_and_encrypts_tokens(
    oauth_workspaces, monkeypatch
):
    from backend.routers import slack
    from backend.services.slack_service import get_workspace_bot_token

    seen = []

    def exchange(code, redirect_uri, client_id, client_secret):
        seen.append((code, redirect_uri, client_id, client_secret))
        return _slack_response("alpha-oauth")

    monkeypatch.setattr(slack, "exchange_code_for_token", exchange)
    with use_workspace("alpha-oauth"):
        state = slack._sign_state("owner@alpha-oauth.example", "alpha-oauth.example")
    response = CLIENT.get(
        "/api/slack/callback",
        params={"code": "single-use-code", "state": state},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303), response.text
    assert "slack_connected=1" in response.headers["location"]
    assert seen == [
        (
            "single-use-code",
            "http://127.0.0.1:8001/api/slack/callback",
            "C_alpha-oauth",
            "client-alpha-oauth",
        )
    ]
    assert oauth_workspaces == [
        ("xoxb-private-alpha-oauth", "owner@alpha-oauth.example")
    ]
    with use_workspace("alpha-oauth"):
        with SessionLocal() as db:
            cred = db.get(SlackCredential, "owner@alpha-oauth.example")
            assert cred.access_token == "xoxp-private-alpha-oauth"
            assert cred.hosted_domain == "alpha-oauth.example"
            raw = db.execute(
                text(
                    "SELECT access_token FROM slack_credentials WHERE user_email=:email"
                ),
                {"email": "owner@alpha-oauth.example"},
            ).scalar_one()
            assert "xoxp-private-alpha-oauth" not in raw
        assert workspace_secret("slack", "bot_token") == "xoxb-private-alpha-oauth"
        assert get_workspace_bot_token("T_alpha-oauth") == "xoxb-private-alpha-oauth"
    with use_workspace("beta-oauth"):
        with SessionLocal() as db:
            assert db.get(SlackCredential, "owner@alpha-oauth.example") is None
        assert workspace_secret("slack", "bot_token") is None
        assert get_workspace_bot_token("T_alpha-oauth") is None
    with control_session() as db:
        installation = db.get(SlackInstallation, ("A_alpha-oauth", "T_alpha-oauth"))
        assert installation.workspace_slug == "alpha-oauth"
        assert installation.signing_secret_ref == "ws-alpha-oauth-slack-signing_secret"


def test_oauth_start_uses_workspace_client_and_signed_state(oauth_workspaces):
    from backend.routers import slack

    with use_workspace("beta-oauth"):
        url = slack.oauth_start(
            request=SimpleNamespace(
                state=SimpleNamespace(
                    session_claims=SessionClaims(
                        sub="owner@beta-oauth.example",
                        ws="beta-oauth",
                        aud="cubie-session",
                        hd="beta-oauth.example",
                    )
                )
            ),
            user=type(
                "U", (), {"email": "owner@beta-oauth.example", "is_admin": True}
            )(),
        ).authorize_url
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    assert params["client_id"] == ["C_beta-oauth"]
    assert params["redirect_uri"] == ["http://127.0.0.1:8001/api/slack/callback"]
    assert slack._parse_state(params["state"][0]) == (
        "beta-oauth",
        "owner@beta-oauth.example",
        "beta-oauth.example",
    )


def test_email_alias_uses_verified_hosted_domain_for_oauth(
    oauth_workspaces, monkeypatch
):
    from backend.routers import slack

    alias = "owner@alias.example"
    with use_workspace("alpha-oauth"):
        with SessionLocal() as db:
            db.add(User(email=alias, id="alias-user", name="Alias Owner"))
            db.commit()
        started = slack.oauth_start(
            request=SimpleNamespace(
                state=SimpleNamespace(
                    session_claims=SessionClaims(
                        sub=alias,
                        ws="alpha-oauth",
                        aud="cubie-session",
                        hd="alpha-oauth.example",
                    )
                )
            ),
            user=SimpleNamespace(email=alias, is_admin=True),
        )
    state = parse_qs(urlparse(started.authorize_url).query)["state"][0]
    assert slack._parse_state(state) == ("alpha-oauth", alias, "alpha-oauth.example")
    monkeypatch.setattr(
        slack, "exchange_code_for_token", lambda *args: _slack_response("alpha-oauth")
    )
    response = CLIENT.get(
        "/api/slack/callback",
        params={"code": "code", "state": state},
        follow_redirects=False,
    )
    assert response.status_code == 302, response.text
    assert oauth_workspaces == [("xoxb-private-alpha-oauth", alias)]
    with use_workspace("alpha-oauth"):
        with SessionLocal() as db:
            cred = db.get(SlackCredential, alias)
            assert cred.slack_user_id == "U_alpha-oauth"
            assert cred.hosted_domain == "alpha-oauth.example"


def test_removed_domain_revokes_existing_personal_slack_token(
    oauth_workspaces, monkeypatch
):
    from backend.routers import slack
    from backend.services.slack_service import SlackNotConnectedError, SlackService

    monkeypatch.setattr(
        slack, "exchange_code_for_token", lambda *args: _slack_response("alpha-oauth")
    )
    with use_workspace("alpha-oauth"):
        state = slack._sign_state("owner@alpha-oauth.example", "alpha-oauth.example")
    response = CLIENT.get(
        "/api/slack/callback",
        params={"code": "code", "state": state},
        follow_redirects=False,
    )
    assert response.status_code == 302
    with use_workspace("alpha-oauth"):
        svc = SlackService("owner@alpha-oauth.example")
        assert svc.is_connected
        with SessionLocal() as db:
            assert slack.status(
                user=SimpleNamespace(email="owner@alpha-oauth.example"), db=db
            ).is_connected
    with control_session() as db:
        db.delete(db.get(WorkspaceDomain, "alpha-oauth.example"))
        db.commit()
    monkeypatch.setattr(
        "backend.services.slack_service.httpx.get",
        lambda *args, **kwargs: pytest.fail("revoked token reached Slack"),
    )
    with use_workspace("alpha-oauth"):
        assert not svc.is_connected
        with pytest.raises(SlackNotConnectedError):
            svc._call("auth.test")
        assert not SlackService("owner@alpha-oauth.example").is_connected
        with SessionLocal() as db:
            assert not slack.status(
                user=SimpleNamespace(email="owner@alpha-oauth.example"), db=db
            ).is_connected


def test_removed_installation_revokes_cached_personal_and_bot_tokens(
    oauth_workspaces, monkeypatch
):
    from backend.routers import slack
    from backend.services.slack_service import (
        SlackBotClient,
        SlackNotConnectedError,
        SlackService,
    )

    monkeypatch.setattr(
        slack, "exchange_code_for_token", lambda *args: _slack_response("alpha-oauth")
    )
    with use_workspace("alpha-oauth"):
        state = slack._sign_state("owner@alpha-oauth.example", "alpha-oauth.example")
    response = CLIENT.get(
        "/api/slack/callback",
        params={"code": "code", "state": state},
        follow_redirects=False,
    )
    assert response.status_code == 302
    with use_workspace("alpha-oauth"):
        svc = SlackService("owner@alpha-oauth.example")
        bot = SlackBotClient()
        assert svc.is_connected and bot.is_available
    with control_session() as db:
        db.delete(db.get(SlackInstallation, ("A_alpha-oauth", "T_alpha-oauth")))
        db.commit()
    monkeypatch.setattr(
        "backend.services.slack_service.httpx.get",
        lambda *args, **kwargs: pytest.fail("revoked personal token reached Slack"),
    )
    monkeypatch.setattr(
        "backend.services.slack_service.httpx.post",
        lambda *args, **kwargs: pytest.fail("revoked bot token reached Slack"),
    )
    with use_workspace("alpha-oauth"):
        assert not svc.is_connected
        assert not bot.is_available
        with pytest.raises(SlackNotConnectedError):
            svc._call("auth.test")
        with pytest.raises(SlackNotConnectedError):
            bot.post_message("C1", text="must not send")
        with SessionLocal() as db:
            assert not slack.status(
                user=SimpleNamespace(email="owner@alpha-oauth.example"), db=db
            ).is_connected


def test_disconnect_revokes_cached_personal_token(oauth_workspaces, monkeypatch):
    from backend.routers import slack
    from backend.services.slack_service import SlackNotConnectedError, SlackService

    monkeypatch.setattr(
        slack, "exchange_code_for_token", lambda *args: _slack_response("alpha-oauth")
    )
    with use_workspace("alpha-oauth"):
        state = slack._sign_state("owner@alpha-oauth.example", "alpha-oauth.example")
    response = CLIENT.get(
        "/api/slack/callback",
        params={"code": "code", "state": state},
        follow_redirects=False,
    )
    assert response.status_code == 302
    with use_workspace("alpha-oauth"):
        svc = SlackService("owner@alpha-oauth.example")
        assert svc.is_connected
        with SessionLocal() as db:
            db.delete(db.get(SlackCredential, "owner@alpha-oauth.example"))
            db.commit()
        monkeypatch.setattr(
            "backend.services.slack_service.httpx.get",
            lambda *args, **kwargs: pytest.fail("disconnected token reached Slack"),
        )
        assert not svc.is_connected
        with pytest.raises(SlackNotConnectedError):
            svc._call("auth.test")


def test_parallel_token_read_does_not_depend_on_shared_cached_credential(monkeypatch):
    from backend.services.slack_service import SlackService

    svc = object.__new__(SlackService)
    svc.user_email = "owner@alpha-oauth.example"
    svc._cred = None
    credential = SimpleNamespace(access_token="xoxp-current")
    monkeypatch.setattr(svc, "_active_credential", lambda: credential)
    # A second worker can clear the shared cache between a connection check
    # and a token read. The token must come from one local validated row.
    with monkeypatch.context() as scoped:
        scoped.setattr(
            SlackService,
            "is_connected",
            property(lambda self: setattr(self, "_cred", None) or True),
        )
        assert svc._require_token() == "xoxp-current"

    class CacheClearedByPeer(SlackService):
        def __setattr__(self, name, value):
            super().__setattr__(name, value)
            if name == "_cred":
                super().__setattr__(name, None)

    peer = object.__new__(CacheClearedByPeer)
    peer.user_email = svc.user_email
    monkeypatch.setattr(peer, "_active_credential", lambda: credential)
    assert peer.is_connected


def test_control_outage_revokes_cached_bot_token(oauth_workspaces, monkeypatch):
    from backend.routers import slack
    from backend.services import slack_service

    monkeypatch.setattr(
        slack, "exchange_code_for_token", lambda *args: _slack_response("alpha-oauth")
    )
    with use_workspace("alpha-oauth"):
        state = slack._sign_state("owner@alpha-oauth.example", "alpha-oauth.example")
    response = CLIENT.get(
        "/api/slack/callback",
        params={"code": "code", "state": state},
        follow_redirects=False,
    )
    assert response.status_code == 302
    with use_workspace("alpha-oauth"):
        bot = slack_service.SlackBotClient()
        assert bot.is_available
    monkeypatch.setattr(
        slack_service,
        "slack_route_for",
        lambda *args: (_ for _ in ()).throw(RuntimeError("control unavailable")),
    )
    with use_workspace("alpha-oauth"):
        assert not bot.is_available
        with pytest.raises(slack_service.SlackNotConnectedError):
            bot.post_message("C1", text="must not send")


def test_domain_removed_during_oauth_exchange_never_persists_credential(
    oauth_workspaces, monkeypatch
):
    from backend.routers import slack

    with use_workspace("alpha-oauth"):
        state = slack._sign_state("owner@alpha-oauth.example", "alpha-oauth.example")

    def exchange(*args):
        with control_session() as db:
            db.delete(db.get(WorkspaceDomain, "alpha-oauth.example"))
            db.commit()
        return _slack_response("alpha-oauth")

    monkeypatch.setattr(slack, "exchange_code_for_token", exchange)
    response = CLIENT.get(
        "/api/slack/callback",
        params={"code": "code", "state": state},
        follow_redirects=False,
    )
    assert response.status_code == 401
    with use_workspace("alpha-oauth"):
        with SessionLocal() as db:
            assert db.get(SlackCredential, "owner@alpha-oauth.example") is None
    with control_session() as db:
        assert db.get(SlackInstallation, ("A_alpha-oauth", "T_alpha-oauth")) is None


def test_control_outage_rejects_personal_slack_token(oauth_workspaces, monkeypatch):
    from backend.routers import slack
    from backend.services import slack_service

    monkeypatch.setattr(
        slack, "exchange_code_for_token", lambda *args: _slack_response("alpha-oauth")
    )
    with use_workspace("alpha-oauth"):
        state = slack._sign_state("owner@alpha-oauth.example", "alpha-oauth.example")
    response = CLIENT.get(
        "/api/slack/callback",
        params={"code": "code", "state": state},
        follow_redirects=False,
    )
    assert response.status_code == 302
    with use_workspace("alpha-oauth"):
        svc = slack_service.SlackService("owner@alpha-oauth.example")
        assert svc.is_connected
    monkeypatch.setattr(
        slack_service,
        "domain_belongs_to_workspace",
        lambda *args: (_ for _ in ()).throw(RuntimeError("control unavailable")),
    )
    with use_workspace("alpha-oauth"):
        assert not svc.is_connected
        assert not slack_service.SlackService("owner@alpha-oauth.example").is_connected


def test_two_workspaces_keep_distinct_bot_tokens(oauth_workspaces, monkeypatch):
    from backend.routers import slack
    from backend.services.slack_service import SlackBotClient, get_workspace_bot_token

    monkeypatch.setattr(
        slack,
        "exchange_code_for_token",
        lambda code, redirect, client_id, secret: _slack_response(
            client_id.removeprefix("C_")
        ),
    )
    for slug in SLUGS:
        with use_workspace(slug):
            state = slack._sign_state(f"owner@{slug}.example", f"{slug}.example")
        response = CLIENT.get(
            "/api/slack/callback",
            params={"code": "code", "state": state},
            follow_redirects=False,
        )
        assert response.status_code == 302, response.text
    with use_workspace("alpha-oauth"):
        assert get_workspace_bot_token("T_alpha-oauth") == "xoxb-private-alpha-oauth"
        assert get_workspace_bot_token("T_beta-oauth") is None
        alpha_bot = SlackBotClient()
        assert alpha_bot.is_available
    with use_workspace("beta-oauth"):
        assert get_workspace_bot_token("T_beta-oauth") == "xoxb-private-beta-oauth"
        assert get_workspace_bot_token("T_alpha-oauth") is None
        assert not alpha_bot.is_available


def test_first_slack_installation_requires_workspace_admin(oauth_workspaces):
    from backend.routers import slack

    with use_workspace("alpha-oauth"), pytest.raises(HTTPException) as error:
        slack.oauth_start(
            request=SimpleNamespace(state=SimpleNamespace(session_claims=None)),
            user=type(
                "U", (), {"email": "owner@alpha-oauth.example", "is_admin": False}
            )(),
        )
    assert error.value.status_code == 403


def test_callback_rejects_mismatched_existing_installation_without_tenant_write(
    oauth_workspaces, monkeypatch
):
    from backend.routers import slack

    with control_session() as db:
        db.add(
            SlackInstallation(
                app_id="A_alpha-oauth",
                team_id="T_alpha-oauth",
                workspace_slug="beta-oauth",
                signing_secret_ref="ws-beta-oauth-slack-signing_secret",
            )
        )
        db.commit()
    monkeypatch.setattr(
        slack, "exchange_code_for_token", lambda *args: _slack_response("alpha-oauth")
    )
    with use_workspace("alpha-oauth"):
        state = slack._sign_state("owner@alpha-oauth.example", "alpha-oauth.example")
    response = CLIENT.get(
        "/api/slack/callback",
        params={"code": "code", "state": state},
        follow_redirects=False,
    )
    assert response.status_code == 409
    with use_workspace("alpha-oauth"):
        with SessionLocal() as db:
            assert db.get(SlackCredential, "owner@alpha-oauth.example") is None
        assert workspace_secret("slack", "bot_token") is None
    with control_session() as db:
        config = db.get(WorkspaceConnector, ("alpha-oauth", "slack")).config
        assert "installed_app_id" not in config
        assert "installed_team_id" not in config


@pytest.mark.parametrize(
    ("second_app", "second_team"),
    [
        ("A_alpha-oauth", "T_alpha-oauth"),
        ("A_alpha-oauth", "T_beta-alt"),
        ("A_beta-alt", "T_alpha-oauth"),
    ],
)
def test_second_workspace_cannot_claim_same_slack_app_or_team(
    oauth_workspaces, monkeypatch, second_app, second_team
):
    from backend.routers import slack

    def exchange(_code, _redirect, client_id, _secret):
        data = _slack_response("alpha-oauth")
        if client_id == "C_beta-oauth":
            data["app_id"] = second_app
            data["team"]["id"] = second_team
        return data

    monkeypatch.setattr(slack, "exchange_code_for_token", exchange)
    for slug, expected_status in (("alpha-oauth", 302), ("beta-oauth", 409)):
        with use_workspace(slug):
            state = slack._sign_state(f"owner@{slug}.example", f"{slug}.example")
        response = CLIENT.get(
            "/api/slack/callback",
            params={"code": "code", "state": state},
            follow_redirects=False,
        )
        assert response.status_code == expected_status, response.text
    with use_workspace("beta-oauth"):
        with SessionLocal() as db:
            assert db.get(SlackCredential, "owner@beta-oauth.example") is None
        assert workspace_secret("slack", "bot_token") is None
    with control_session() as db:
        config = db.get(WorkspaceConnector, ("beta-oauth", "slack")).config
        assert "installed_app_id" not in config
        assert "installed_team_id" not in config


@pytest.mark.parametrize(
    ("lookup_result", "expected_status"),
    [
        ("U_someone-else", 403),
        (None, 403),
        (SlackAPIError("missing_scope"), 502),
        (SlackAPIError("timeout"), 502),
    ],
)
def test_callback_rejects_unverified_slack_account_before_token_storage(
    oauth_workspaces, monkeypatch, lookup_result, expected_status
):
    from backend.routers import slack

    class LookupBot:
        def __init__(self, bot_token, *, timeout, max_retries):
            assert bot_token == "xoxb-private-alpha-oauth"

        def lookup_user_id(self, email):
            assert email == "owner@alpha-oauth.example"
            if isinstance(lookup_result, Exception):
                raise lookup_result
            return lookup_result

    monkeypatch.setattr(slack, "SlackBotClient", LookupBot)
    monkeypatch.setattr(
        slack, "exchange_code_for_token", lambda *args: _slack_response("alpha-oauth")
    )
    with use_workspace("alpha-oauth"):
        state = slack._sign_state("owner@alpha-oauth.example", "alpha-oauth.example")
    response = CLIENT.get(
        "/api/slack/callback",
        params={"code": "code", "state": state},
        follow_redirects=False,
    )
    assert response.status_code == expected_status
    assert "xox" not in response.text
    with use_workspace("alpha-oauth"):
        with SessionLocal() as db:
            assert db.get(SlackCredential, "owner@alpha-oauth.example") is None
        assert workspace_secret("slack", "bot_token") is None
    with control_session() as db:
        assert db.get(SlackInstallation, ("A_alpha-oauth", "T_alpha-oauth")) is None

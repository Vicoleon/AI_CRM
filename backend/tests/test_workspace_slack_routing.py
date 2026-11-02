"""Signed Slack inbound requests select one active workspace before tenant I/O."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from starlette.responses import Response

from backend.main import app
from rubik_core.control.db import control_session
from rubik_core.control.models import (
    SlackInstallation,
    Workspace,
    WorkspaceDomain,
    WorkspaceModule,
)
from rubik_core.tenancy.context import current_workspace, use_workspace

CLIENT = TestClient(app)
SECRETS = {"alpha": "alpha-signing-secret", "beta": "beta-signing-secret"}


@pytest.fixture
def installations(monkeypatch):
    from backend.routers import slack_commands
    from rubik_core.control.repository import clear_module_cache

    with control_session() as db:
        for slug in ("alpha", "beta"):
            db.add(Workspace(slug=slug, name=slug, status="active"))
        db.flush()
        for slug, app_id, team_id in (
            ("alpha", "A_ALPHA", "T_ALPHA"),
            ("beta", "A_BETA", "T_BETA"),
        ):
            db.add(
                WorkspaceDomain(
                    workspace_slug=slug,
                    domain=f"{slug}.example",
                    is_primary=True,
                )
            )
            db.add(WorkspaceModule(workspace_slug=slug, module="eos", enabled=True))
            db.add(
                SlackInstallation(
                    app_id=app_id,
                    team_id=team_id,
                    workspace_slug=slug,
                    signing_secret_ref=f"ws-{slug}-slack-signing_secret",
                )
            )
        db.commit()
    clear_module_cache("alpha")
    clear_module_cache("beta")

    def secret_for_ref(ref: str) -> str:
        slug = current_workspace()
        assert ref == f"ws-{slug}-slack-signing_secret"
        return SECRETS[slug]

    monkeypatch.setattr(slack_commands, "read_secret_ref", secret_for_ref)
    try:
        yield
    finally:
        with control_session() as db:
            for slug, app_id, team_id in (
                ("alpha", "A_ALPHA", "T_ALPHA"),
                ("beta", "A_BETA", "T_BETA"),
            ):
                row = db.get(SlackInstallation, (app_id, team_id))
                if row is not None:
                    db.delete(row)
                row = db.get(WorkspaceModule, (slug, "eos"))
                if row is not None:
                    db.delete(row)
                row = db.get(WorkspaceDomain, f"{slug}.example")
                if row is not None:
                    db.delete(row)
            db.flush()
            for slug in ("alpha", "beta"):
                row = db.get(Workspace, slug)
                if row is not None:
                    db.delete(row)
            db.commit()
        clear_module_cache("alpha")
        clear_module_cache("beta")


def _post(
    fields: dict[str, str],
    *,
    secret: str,
    path: str = "/api/slack/commands",
    extra_headers: dict[str, str] | None = None,
):
    body = urlencode(fields).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(
        secret.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256
    ).hexdigest()
    return CLIENT.post(
        path,
        content=body,
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "x-slack-request-timestamp": timestamp,
            "x-slack-signature": f"v0={signature}",
            **(extra_headers or {}),
        },
    )


def _command(app_id="A_ALPHA", team_id="T_ALPHA"):
    return {
        "api_app_id": app_id,
        "team_id": team_id,
        "command": "/todo",
        "user_id": "U_UNKNOWN",
        "trigger_id": "trigger",
    }


def test_slack_signature_cannot_cross_workspaces(installations, monkeypatch):
    from backend.routers import slack_commands

    opened = []

    class FakeDB:
        def close(self):
            pass

    def open_db():
        opened.append(current_workspace())
        return FakeDB()

    monkeypatch.setattr(slack_commands, "SessionLocal", open_db)
    monkeypatch.setattr(
        slack_commands, "_resolve_user", lambda db, user_id, team_id: None
    )

    wrong = _post(_command(), secret=SECRETS["beta"])
    assert wrong.status_code == 401
    assert opened == []

    acme = _post(_command(), secret=SECRETS["alpha"])
    beta = _post(_command("A_BETA", "T_BETA"), secret=SECRETS["beta"])
    assert acme.status_code == beta.status_code == 200
    assert opened == ["alpha", "beta"]


def test_slack_command_handler_runs_off_event_loop(installations, monkeypatch):
    from backend.routers import slack_commands

    seen = []

    def blocking_handler(slug, team_id, form):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        seen.append((slug, team_id, form["trigger_id"]))
        return Response(status_code=200)

    monkeypatch.setattr(slack_commands, "_handle_command", blocking_handler)
    assert _post(_command(), secret=SECRETS["alpha"]).status_code == 200
    assert seen == [("alpha", "T_ALPHA", "trigger")]


def test_slack_submission_handler_runs_off_event_loop(installations, monkeypatch):
    from backend.routers import slack_commands

    seen = []

    def blocking_handler(slug, team_id, slack_user_id, *args):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        seen.append((slug, team_id, slack_user_id))
        return Response(status_code=200)

    monkeypatch.setattr(slack_commands, "_handle_submission", blocking_handler)
    payload = {
        "api_app_id": "A_ALPHA",
        "team": {"id": "T_ALPHA"},
        "type": "view_submission",
        "user": {"id": "U1"},
        "view": {
            "id": "V1",
            "callback_id": "eos_issue_create",
            "private_metadata": json.dumps(
                {"email": "owner@alpha.example", "workspace": "alpha"}
            ),
            "state": {"values": {}},
        },
    }
    assert (
        _post(
            {"payload": json.dumps(payload)},
            secret=SECRETS["alpha"],
            path="/api/slack/interactivity",
        ).status_code
        == 200
    )
    assert seen == [("alpha", "T_ALPHA", "U1")]


def test_unknown_and_suspended_installation_never_opens_tenant_db(
    installations, monkeypatch
):
    from backend.routers import slack_commands

    monkeypatch.setattr(
        slack_commands,
        "SessionLocal",
        lambda: pytest.fail("unverified Slack request opened tenant DB"),
    )
    assert (
        _post(_command("A_MISSING", "T_MISSING"), secret="anything").status_code == 401
    )
    with control_session() as db:
        db.get(Workspace, "alpha").status = "suspended"
        db.commit()
    assert _post(_command(), secret=SECRETS["alpha"]).status_code == 401


def test_eos_off_refuses_before_tenant_db(installations, monkeypatch):
    from backend.routers import slack_commands
    from rubik_core.control.repository import clear_module_cache

    monkeypatch.setattr(
        slack_commands,
        "SessionLocal",
        lambda: pytest.fail("EOS-off Slack request opened tenant DB"),
    )
    with control_session() as db:
        db.get(WorkspaceModule, ("alpha", "eos")).enabled = False
        db.commit()
    clear_module_cache("alpha")
    assert _post(_command(), secret=SECRETS["alpha"]).status_code == 404


def test_modal_metadata_cannot_cross_workspaces(installations, monkeypatch):
    from backend.routers import slack_commands

    monkeypatch.setattr(
        slack_commands,
        "SessionLocal",
        lambda: pytest.fail("cross-workspace modal opened tenant DB"),
    )
    payload = {
        "api_app_id": "A_BETA",
        "team": {"id": "T_BETA"},
        "type": "view_submission",
        "view": {
            "id": "V1",
            "callback_id": "eos_todo_create",
            "private_metadata": json.dumps(
                {"email": "owner@alpha.example", "workspace": "alpha"}
            ),
            "state": {"values": {}},
        },
    }
    response = _post(
        {"payload": json.dumps(payload)},
        secret=SECRETS["beta"],
        path="/api/slack/interactivity",
    )
    assert response.status_code == 401


@pytest.mark.parametrize("view_id", [None, "", "bad/id", "V" * 129])
def test_modal_requires_bounded_view_id_before_tenant_db(
    installations, monkeypatch, view_id
):
    from backend.routers import slack_commands

    monkeypatch.setattr(
        slack_commands,
        "SessionLocal",
        lambda: pytest.fail("modal without a view ID opened tenant DB"),
    )
    payload = {
        "api_app_id": "A_ALPHA",
        "team": {"id": "T_ALPHA"},
        "type": "view_submission",
        "user": {"id": "U1"},
        "view": {
            "id": view_id,
            "callback_id": "eos_todo_create",
            "private_metadata": json.dumps(
                {"email": "owner@alpha.example", "workspace": "alpha"}
            ),
            "state": {"values": {}},
        },
    }
    assert (
        _post(
            {"payload": json.dumps(payload)},
            secret=SECRETS["alpha"],
            path="/api/slack/interactivity",
        ).status_code
        == 400
    )


def test_slack_ignores_bearer_identity_and_uses_signed_installation(
    installations, monkeypatch
):
    from backend.routers import slack_commands

    seen = []

    class FakeDB:
        def close(self):
            pass

    def open_db():
        seen.append(current_workspace())
        return FakeDB()

    monkeypatch.setattr(slack_commands, "SessionLocal", open_db)
    monkeypatch.setattr(
        slack_commands, "_resolve_user", lambda db, user_id, team_id: None
    )
    response = _post(
        _command(),
        secret=SECRETS["alpha"],
        extra_headers={
            "authorization": "Bearer mock-token",
            "x-mock-user": "b@beta.example",
        },
    )
    assert response.status_code == 200
    assert seen == ["alpha"]


def test_installation_ref_cannot_redirect_secret_lookup(installations, monkeypatch):
    from backend.routers import slack_commands

    with control_session() as db:
        db.get(
            SlackInstallation, ("A_ALPHA", "T_ALPHA")
        ).signing_secret_ref = "ws-alpha-other-secret"
        db.commit()
    monkeypatch.setattr(
        slack_commands,
        "read_secret_ref",
        lambda ref: pytest.fail("installation selected another secret binding"),
    )
    monkeypatch.setattr(
        slack_commands,
        "SessionLocal",
        lambda: pytest.fail("malformed installation opened tenant DB"),
    )
    assert _post(_command(), secret=SECRETS["alpha"]).status_code == 401


def test_slack_alias_email_requires_current_tenant_user_row(installations, monkeypatch):
    from types import SimpleNamespace

    from backend.routers import slack_commands

    class FakeDB:
        def query(self, model):
            return self

        def filter(self, *criteria):
            return self

        def first(self):
            return SimpleNamespace(
                user_email="owner@alias.example", hosted_domain="alpha.example"
            )

    present = True
    tenant_db = FakeDB()

    def get_user(email):
        assert email == "owner@alias.example"
        return (
            SimpleNamespace(email=email, is_platform_support=False) if present else None
        )

    monkeypatch.setattr(
        slack_commands,
        "SQLUserRegistry",
        lambda db: SimpleNamespace(get_user=get_user) if db is tenant_db else None,
    )
    with use_workspace("alpha"):
        assert slack_commands._resolve_user(tenant_db, "U1", "T_ALPHA") is not None
    present = False
    with use_workspace("alpha"):
        assert slack_commands._resolve_user(tenant_db, "U1", "T_ALPHA") is None


def test_removed_hosted_domain_revokes_linked_slack_command(installations, monkeypatch):
    from types import SimpleNamespace

    from backend.routers import slack_commands

    class FakeDB:
        def query(self, model):
            return self

        def filter(self, *criteria):
            return self

        def first(self):
            return SimpleNamespace(
                user_email="owner@alias.example", hosted_domain="alpha.example"
            )

    monkeypatch.setattr(
        slack_commands,
        "SQLUserRegistry",
        lambda db: SimpleNamespace(
            get_user=lambda email: SimpleNamespace(
                email=email, is_platform_support=False
            )
        ),
    )
    with use_workspace("alpha"):
        assert slack_commands._resolve_user(FakeDB(), "U1", "T_ALPHA") is not None
    with control_session() as db:
        db.delete(db.get(WorkspaceDomain, "alpha.example"))
        db.commit()
    with use_workspace("alpha"):
        assert slack_commands._resolve_user(FakeDB(), "U1", "T_ALPHA") is None


def test_slack_command_control_outage_denies_linked_identity(
    installations, monkeypatch
):
    from types import SimpleNamespace

    from backend.routers import slack_commands
    from backend.services import slack_service

    class FakeDB:
        def query(self, model):
            return self

        def filter(self, *criteria):
            return self

        def first(self):
            return SimpleNamespace(
                user_email="owner@alias.example", hosted_domain="alpha.example"
            )

    monkeypatch.setattr(
        slack_service,
        "domain_belongs_to_workspace",
        lambda *args: (_ for _ in ()).throw(RuntimeError("control unavailable")),
    )
    with use_workspace("alpha"):
        assert slack_commands._resolve_user(FakeDB(), "U1", "T_ALPHA") is None


def test_platform_support_slack_user_is_refused(installations, monkeypatch):
    from types import SimpleNamespace

    from backend.routers import slack_commands

    class FakeDB:
        def query(self, model):
            return self

        def filter(self, *criteria):
            return self

        def first(self):
            return SimpleNamespace(
                user_email="support@alpha.example", hosted_domain="alpha.example"
            )

    monkeypatch.setattr(
        slack_commands,
        "SQLUserRegistry",
        lambda db: SimpleNamespace(
            get_user=lambda email: SimpleNamespace(
                email=email, is_platform_support=True
            )
        ),
    )
    with use_workspace("alpha"):
        assert slack_commands._resolve_user(FakeDB(), "U_SUPPORT", "T_ALPHA") is None


def test_unlinked_slack_user_must_connect_before_acting(installations, monkeypatch):
    from types import SimpleNamespace

    from backend.routers import slack_commands

    seen = []

    class FakeDB:
        def query(self, model):
            return self

        def filter(self, *criteria):
            return self

        def first(self):
            return None

    class FakeBot:
        is_available = True

        def __init__(self, *args, **kwargs):
            seen.append(kwargs)

        def get_user_email(self, user_id):
            return "owner@alpha.example"

    monkeypatch.setattr(slack_commands, "SlackBotClient", FakeBot)
    monkeypatch.setattr(
        slack_commands,
        "SQLUserRegistry",
        lambda db: SimpleNamespace(
            get_user=lambda email: SimpleNamespace(
                email=email, is_platform_support=False
            )
        ),
    )
    with use_workspace("alpha"):
        user = slack_commands._resolve_user(FakeDB(), "U1", "T_ALPHA")
    assert user is None
    assert seen == []


def test_modal_re_resolves_signed_user_and_rejects_other_metadata_email(
    installations, monkeypatch
):
    from types import SimpleNamespace

    from backend.routers import slack_commands

    class FakeDB:
        def close(self):
            pass

    monkeypatch.setattr(slack_commands, "SessionLocal", FakeDB)
    monkeypatch.setattr(
        slack_commands,
        "_resolve_user",
        lambda db, user_id, team_id: SimpleNamespace(email="actual@alpha.example"),
    )
    payload = {
        "api_app_id": "A_ALPHA",
        "team": {"id": "T_ALPHA"},
        "type": "view_submission",
        "user": {"id": "U_ACTUAL"},
        "view": {
            "id": "V2",
            "callback_id": "eos_todo_create",
            "private_metadata": json.dumps(
                {"email": "other@alpha.example", "workspace": "alpha"}
            ),
            "state": {"values": {}},
        },
    }
    response = _post(
        {"payload": json.dumps(payload)},
        secret=SECRETS["alpha"],
        path="/api/slack/interactivity",
    )
    assert response.status_code == 401


def test_issue_reindex_fallback_rebinds_original_workspace(installations, monkeypatch):
    from backend.jobs import eos_ingest_hooks as hooks

    captured = []
    observed = []
    monkeypatch.setattr(hooks.debounce, "request_reingest", lambda kind: True)
    monkeypatch.setattr(hooks.debounce, "record_dispatch", lambda *args: None)
    monkeypatch.setattr(
        hooks.debounce,
        "run_debounced_reingest",
        lambda kind, **kwargs: observed.append(current_workspace()),
    )

    def fake_submit(background_tasks, **kwargs):
        captured.append(kwargs["fallback"])
        return "inline", None

    monkeypatch.setattr(hooks, "submit_background_work", fake_submit)
    with use_workspace("alpha"):
        hooks.enqueue_issue_ingest(object())
    assert len(captured) == 1
    captured[0]()
    assert observed == ["alpha"]


def test_untrusted_body_is_bounded_before_installation_lookup(
    installations, monkeypatch
):
    from backend.routers import slack_commands

    monkeypatch.setattr(
        slack_commands,
        "slack_route_for",
        lambda app_id, team_id: pytest.fail("oversized body queried control DB"),
    )
    body = _command()
    body["text"] = "a" * (128 * 1024)
    assert _post(body, secret=SECRETS["alpha"]).status_code == 413

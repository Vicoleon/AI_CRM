"""Tests for the Slack slash commands (/todo, /ids → EOS To-Dos & Issues).

Covers the two-step Slack modal flow end to end:
  * request-signature verification (valid / tampered / stale / unconfigured),
  * POST /api/slack/commands opens the right modal via views.open,
  * POST /api/slack/interactivity creates a Todo / Issue from the submission,
  * Exec-team scope is enforced server-side,
  * an unrecognized Slack user gets a friendly ephemeral reply.

The endpoints take no bearer token (Slack signs the request), so identity is
resolved from the Slack user id via the SlackCredential table / bot lookup —
these tests seed a SlackCredential and stub SlackBotClient so nothing hits the
network.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from base64 import urlsafe_b64encode
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from backend.main import app
from rubik_core.auth.registry import UserRegistry
from rubik_core.config import settings

client = TestClient(app)

_SECRET = "test-signing-secret"


def _sign(body: bytes, *, ts: str | None = None) -> dict[str, str]:
    ts = ts or str(int(time.time()))
    base = b"v0:" + ts.encode() + b":" + body
    sig = "v0=" + hmac.new(_SECRET.encode(), base, hashlib.sha256).hexdigest()
    return {
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": sig,
        "Content-Type": "application/x-www-form-urlencoded",
    }


def _post(
    path: str, fields: dict[str, str], *, ts: str | None = None, sign: bool = True
):
    fields = dict(fields)
    if path.endswith("/commands"):
        fields.setdefault("api_app_id", "A_TEST")
        fields.setdefault("team_id", "T1")
    body = urlencode(fields).encode("utf-8")
    headers = _sign(body, ts=ts)
    if not sign:
        headers["X-Slack-Signature"] = "v0=deadbeef"
    return client.post(path, content=body, headers=headers)


class _FakeBot:
    """Stub SlackBotClient — records views.open, no network."""

    opened: list = []
    email_for: dict[str, str | None] = {}
    team_ids: list[str | None] = []
    budgets: list[tuple[float | None, int | None]] = []

    def __init__(self, *args, **kwargs):
        _FakeBot.team_ids.append(kwargs.get("team_id"))
        _FakeBot.budgets.append((kwargs.get("timeout"), kwargs.get("max_retries")))

    @property
    def is_available(self) -> bool:
        return True

    def get_user_email(self, user_id: str):
        return self.email_for.get(user_id)

    def open_view(self, trigger_id: str, view: dict):
        _FakeBot.opened.append((trigger_id, view))
        return {"ok": True}


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test_slack_commands.db"
    settings.DATABASE_URL = f"sqlite:///{db_path}"

    import rubik_core.database.db as dbmod

    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database import eos  # noqa: F401  (register EOS models)
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)

    from rubik_core.control.db import control_session
    from rubik_core.control.models import SlackInstallation
    from rubik_core.tenancy.context import current_workspace

    with control_session() as control:
        control.add(
            SlackInstallation(
                app_id="A_TEST",
                team_id="T1",
                workspace_slug="test",
                signing_secret_ref="ws-test-slack-signing_secret",
            )
        )
        control.commit()
    monkeypatch.setattr(
        "backend.routers.slack_commands.read_secret_ref",
        lambda ref: (
            _SECRET if ref == f"ws-{current_workspace()}-slack-signing_secret" else None
        ),
    )

    # Stub the outbound bot everywhere the router constructs it.
    _FakeBot.opened = []
    _FakeBot.email_for = {}
    _FakeBot.team_ids = []
    _FakeBot.budgets = []
    monkeypatch.setattr("backend.routers.slack_commands.SlackBotClient", _FakeBot)

    yield

    with control_session() as control:
        control.delete(control.get(SlackInstallation, ("A_TEST", "T1")))
        control.commit()


def _seed_user(
    email: str,
    *,
    department: str = "Systems",
    is_exec: bool = False,
    slack_user_id: str | None = "U123",
):
    """Register a Cubie user (+ department, EOS Person, and optionally a linked
    Slack credential). Returns the person's id string for owner-value building."""
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import Person
    from rubik_core.database.models import Department, SlackCredential

    reg = UserRegistry()
    # The FIRST user ever registered is auto-promoted to admin (repository.py).
    # Burn that slot with a sentinel so seeded users get realistic non-admin
    # roles — otherwise the exec-scope gate is trivially satisfied.
    if not reg.get_user("seed-admin@acme.example"):
        reg.register_user(
            email="seed-admin@acme.example", name="Seed Admin", id="seed-admin"
        )
    if not reg.get_user(email):
        reg.register_user(
            email=email,
            name=email.split("@")[0].title(),
            id=email,
            department=department,
        )
    if is_exec:
        reg.update_user(email, {"is_exec": True})

    with SessionLocal() as db:
        if (
            department
            and not db.query(Department).filter(Department.name == department).first()
        ):
            db.add(Department(name=department))
            db.commit()
        person = db.query(Person).filter(Person.email == email).first()
        if not person:
            person = Person(
                first_name=email.split("@")[0].title(),
                last_name="Tester",
                email=email,
                created_by="test",
            )
            db.add(person)
            db.commit()
            db.refresh(person)
        pid = str(person.id)
        if slack_user_id:
            if (
                not db.query(SlackCredential)
                .filter(SlackCredential.user_email == email)
                .first()
            ):
                db.add(
                    SlackCredential(
                        user_email=email,
                        slack_user_id=slack_user_id,
                        slack_team_id="T1",
                        hosted_domain="acme.example",
                        access_token="xoxp-test",
                    )
                )
                db.commit()
    return pid


# ─── Signature verification (unit) ───────────────────────────────────────────


def test_verify_signature_accepts_valid():
    from backend.routers.slack_commands import _verify_signature

    body = b"command=/todo&user_id=U123"
    # Wrap in Starlette Headers (case-insensitive), matching the runtime
    # `request.headers` the endpoint hands _verify_signature.
    assert _verify_signature(body, Headers(_sign(body)), secret=_SECRET) is True


def test_verify_signature_rejects_tampered_body():
    from backend.routers.slack_commands import _verify_signature

    headers = Headers(_sign(b"command=/todo"))
    assert _verify_signature(b"command=/ids", headers, secret=_SECRET) is False


def test_verify_signature_rejects_stale_timestamp():
    from backend.routers.slack_commands import _verify_signature

    body = b"x=1"
    old = str(int(time.time()) - 60 * 10)  # 10 minutes ago
    assert (
        _verify_signature(body, Headers(_sign(body, ts=old)), secret=_SECRET) is False
    )


def test_verify_signature_rejects_when_secret_unset(monkeypatch):
    from backend.routers import slack_commands

    body = b"x=1"
    assert (
        slack_commands._verify_signature(body, Headers(_sign(body)), secret=None)
        is False
    )


def test_oauth_start_uses_workspace_config_not_process_client(monkeypatch):
    from types import SimpleNamespace
    from urllib.parse import parse_qs, urlparse

    from backend.routers import slack

    monkeypatch.setattr(slack, "domain_belongs_to_workspace", lambda *args: True)

    monkeypatch.setenv(
        "CUBIE_OAUTH_STATE_SECRET",
        urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("="),
    )
    monkeypatch.setenv("SLACK_CLIENT_ID", "process-client")
    monkeypatch.setenv("SLACK_REDIRECT_URI", "https://process.example/callback")
    monkeypatch.setattr(
        slack,
        "_oauth_config",
        lambda: (
            {
                "client_id": "workspace-client",
                "redirect_uri": "https://ws.example/api/slack/callback",
            },
            True,
        ),
    )
    params = parse_qs(
        urlparse(
            slack.oauth_start(
                request=SimpleNamespace(
                    state=SimpleNamespace(
                        session_claims=SimpleNamespace(
                            typ="user",
                            ws="test",
                            sub="user@acme.example",
                            hd="acme.example",
                        )
                    )
                ),
                user=SimpleNamespace(email="user@acme.example", is_admin=True),
            ).authorize_url
        ).query
    )
    assert params["client_id"] == ["workspace-client"]
    assert params["redirect_uri"] == ["https://ws.example/api/slack/callback"]


def test_oauth_state_uses_platform_secret(monkeypatch):
    from backend.routers import slack

    monkeypatch.setattr(slack, "domain_belongs_to_workspace", lambda *args: True)

    monkeypatch.setenv(
        "CUBIE_OAUTH_STATE_SECRET",
        urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("="),
    )
    names = []
    monkeypatch.setattr(
        slack,
        "platform_secret",
        lambda name: names.append(name) or "platform-test-key",
    )
    state = slack._sign_state("user@acme.example", "acme.example")
    assert slack._parse_state(state) == ("test", "user@acme.example", "acme.example")
    assert names == ["CUBIE_OAUTH_STATE_SECRET", "CUBIE_OAUTH_STATE_SECRET"]


@pytest.mark.parametrize("path", ["/api/slack/commands", "/api/slack/interactivity"])
def test_unknown_slack_inbound_returns_401_before_tenant_db(path, monkeypatch):
    from backend.routers import slack_commands

    monkeypatch.setattr(
        slack_commands,
        "SessionLocal",
        lambda: pytest.fail("Slack inbound opened tenant DB before workspace routing"),
    )
    if path.endswith("/interactivity"):
        fields = {
            "payload": json.dumps(
                {"api_app_id": "A_MISSING", "team": {"id": "T_MISSING"}}
            )
        }
    else:
        fields = {
            "api_app_id": "A_MISSING",
            "team_id": "T_MISSING",
            "command": "/todo",
            "user_id": "U123",
        }
    response = _post(path, fields)
    assert response.status_code == 401


# ─── /commands → opens a modal ───────────────────────────────────────────────


def test_todo_command_opens_todo_modal():
    _seed_user("owner@acme.example")
    resp = _post(
        "/api/slack/commands",
        {"command": "/todo", "user_id": "U123", "trigger_id": "TRIG1", "text": ""},
    )
    assert resp.status_code == 200
    assert len(_FakeBot.opened) == 1
    assert _FakeBot.team_ids == ["T1"]
    assert _FakeBot.budgets == [(2.0, 1)]
    trigger_id, view = _FakeBot.opened[0]
    assert trigger_id == "TRIG1"
    assert view["callback_id"] == "eos_todo_create"
    block_ids = {b.get("block_id") for b in view["blocks"]}
    assert {"desc", "team", "owner", "due"} <= block_ids


def test_ids_command_opens_issue_modal_and_prefills_text():
    _seed_user("owner@acme.example")
    resp = _post(
        "/api/slack/commands",
        {
            "command": "/ids",
            "user_id": "U123",
            "trigger_id": "TRIG2",
            "text": "Deploy window keeps slipping",
        },
    )
    assert resp.status_code == 200
    _, view = _FakeBot.opened[0]
    assert view["callback_id"] == "eos_issue_create"
    block_ids = {b.get("block_id") for b in view["blocks"]}
    assert {"title", "desc", "team", "priority", "owner"} <= block_ids
    desc_block = next(b for b in view["blocks"] if b.get("block_id") == "desc")
    assert desc_block["element"]["initial_value"] == "Deploy window keeps slipping"


def test_unknown_slack_user_gets_ephemeral():
    # No SlackCredential seeded, and the bot can't resolve the id → ephemeral.
    resp = _post(
        "/api/slack/commands",
        {"command": "/todo", "user_id": "U_UNKNOWN", "trigger_id": "T", "text": ""},
    )
    assert resp.status_code == 200
    assert resp.json()["response_type"] == "ephemeral"
    assert _FakeBot.opened == []  # no modal opened


def test_signed_slack_installation_accepts_tenant_user_with_alias_email():
    email = "owner@alias.example"
    owner_id = _seed_user(email, department="Systems")
    command = _post(
        "/api/slack/commands",
        {"command": "/todo", "user_id": "U123", "trigger_id": "TRIG1"},
    )
    assert command.status_code == 200
    assert len(_FakeBot.opened) == 1
    assert json.loads(_FakeBot.opened[0][1]["private_metadata"])["email"] == email

    values = {
        "desc": _text("Send renewal paperwork"),
        "team": _select("dept:Systems"),
        "owner": _select(f"person:{owner_id}"),
    }
    submission = _post(
        "/api/slack/interactivity", _submission("eos_todo_create", email, values)
    )
    assert submission.status_code == 200
    assert submission.json()["response_action"] == "update"

    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import Todo

    with SessionLocal() as db:
        todos = db.query(Todo).all()
    assert len(todos) == 1
    assert todos[0].created_by == email


def test_command_rejects_bad_signature():
    _seed_user("owner@acme.example")
    resp = _post(
        "/api/slack/commands",
        {"command": "/todo", "user_id": "U123", "trigger_id": "T", "text": ""},
        sign=False,
    )
    assert resp.status_code == 401


# ─── /interactivity → creates rows ───────────────────────────────────────────


def _submission(
    callback_id: str, email: str, values: dict, *, view_id: str = "V_TEST"
) -> dict[str, str]:
    payload = {
        "api_app_id": "A_TEST",
        "team": {"id": "T1"},
        "type": "view_submission",
        "user": {"id": "U123"},
        "view": {
            "id": view_id,
            "callback_id": callback_id,
            "private_metadata": json.dumps({"email": email, "workspace": "test"}),
            "state": {"values": values},
        },
    }
    return {"payload": json.dumps(payload)}


def _select(value: str) -> dict:
    return {"value": {"selected_option": {"value": value}}}


def _text(value: str) -> dict:
    return {"value": {"value": value}}


def test_interactivity_creates_todo():
    email = "owner@acme.example"
    pid = _seed_user(email, department="Systems")
    values = {
        "desc": _text("Send the renewal paperwork"),
        "team": _select("dept:systems"),
        "owner": _select(f"person:{pid}"),
        "due": {"value": {"selected_date": "2026-08-01"}},
    }
    resp = _post(
        "/api/slack/interactivity", _submission("eos_todo_create", email, values)
    )
    assert resp.status_code == 200
    assert resp.json()["response_action"] == "update"
    retry = _post(
        "/api/slack/interactivity", _submission("eos_todo_create", email, values)
    )
    assert retry.status_code == 200

    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import Todo
    from rubik_core.database.models import SlackViewSubmission

    with SessionLocal() as db:
        todos = db.query(Todo).filter(Todo.is_deleted == False).all()  # noqa: E712
        assert db.query(SlackViewSubmission).count() == 1
    assert len(todos) == 1
    todo = todos[0]
    assert todo.description == "Send the renewal paperwork"
    assert todo.department_name == "Systems"
    assert todo.is_exec is False
    assert str(todo.owner_id) == pid
    assert todo.created_by == email
    assert todo.due_date is not None


def test_interactivity_creates_issue_with_derived_title(monkeypatch):
    from backend.routers import slack_commands

    dispatched = []
    monkeypatch.setattr(
        slack_commands,
        "enqueue_issue_ingest",
        lambda background_tasks: dispatched.append(background_tasks),
    )
    email = "owner@acme.example"
    _seed_user(email, department="Systems")
    values = {
        "title": _text(""),  # left blank → derived from description
        "desc": _text("The deploy window keeps getting missed every sprint"),
        "team": _select("dept:Systems"),
        "priority": _select("high"),
        "owner": _select("none"),
    }
    resp = _post(
        "/api/slack/interactivity", _submission("eos_issue_create", email, values)
    )
    assert resp.status_code == 200
    assert resp.json()["response_action"] == "update"
    retry = _post(
        "/api/slack/interactivity", _submission("eos_issue_create", email, values)
    )
    assert retry.status_code == 200
    assert len(dispatched) == 1

    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import Issue, IssuePriority
    from rubik_core.database.models import SlackViewSubmission

    with SessionLocal() as db:
        issues = db.query(Issue).filter(Issue.is_deleted == False).all()  # noqa: E712
        assert db.query(SlackViewSubmission).count() == 1
    assert len(issues) == 1
    issue = issues[0]
    assert issue.description == "The deploy window keeps getting missed every sprint"
    assert issue.title == "The deploy window keeps getting missed every sprint"
    assert issue.priority == IssuePriority.HIGH
    assert issue.department_name == "Systems"
    assert issue.owner_id is None
    assert issue.created_by == email


def test_my_todos_submission_replay_creates_one_promise(monkeypatch):
    from backend.routers import slack_commands
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.models import SlackViewSubmission
    from rubik_core.database.promises import Promise, PromiseEvent

    email = "owner@acme.example"
    owner_id = _seed_user(email, department="Systems")
    monkeypatch.setattr(slack_commands, "_todos_enabled_for", lambda user: True)
    values = {
        "desc": _text("Send the renewal paperwork"),
        "where": _select("mine"),
        "owner": _select(f"person:{owner_id}"),
        "due": {"value": {"selected_date": "2026-08-01"}},
    }
    fields = _submission("eos_todo_create", email, values)
    first = _post("/api/slack/interactivity", fields)
    retry = _post("/api/slack/interactivity", fields)
    assert first.status_code == retry.status_code == 200
    assert first.json()["response_action"] == "update"

    with SessionLocal() as db:
        assert db.query(Promise).count() == 1
        assert db.query(PromiseEvent).count() == 1
        assert db.query(SlackViewSubmission).count() == 1


def test_submission_marker_rolls_back_business_row_on_unique_race():
    from backend.routers import slack_commands
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import Issue, IssuePriority, IssueStatus
    from rubik_core.database.models import SlackViewSubmission

    with SessionLocal() as db:
        db.add(SlackViewSubmission(view_id="V_DUPLICATE"))
        db.commit()
        db.add(
            Issue(
                title="Should roll back",
                description="The duplicate view already created its issue",
                priority=IssuePriority.MEDIUM,
                status=IssueStatus.OPEN,
                created_by="owner@acme.example",
            )
        )
        assert slack_commands._commit_submission(db, "V_DUPLICATE") is False
        assert db.query(Issue).count() == 0
        assert db.query(SlackViewSubmission).count() == 1


def test_interactivity_blocks_non_exec_from_exec_team():
    email = "rep@acme.example"
    _seed_user(email, department="Systems", is_exec=False)
    values = {
        "desc": _text("Confidential exec item"),
        "team": _select("exec"),
        "priority": _select("medium"),
        "owner": _select("none"),
    }
    resp = _post(
        "/api/slack/interactivity", _submission("eos_issue_create", email, values)
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["response_action"] == "errors"
    assert "team" in data["errors"]

    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import Issue

    with SessionLocal() as db:
        assert db.query(Issue).count() == 0  # nothing created
        from rubik_core.database.models import SlackViewSubmission

        assert db.query(SlackViewSubmission).count() == 0


@pytest.mark.parametrize("callback_id", ["eos_todo_create", "eos_issue_create"])
def test_interactivity_rejects_forged_department(callback_id):
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import Issue, Todo
    from rubik_core.database.models import Department, SlackViewSubmission

    email = "rep@acme.example"
    owner_id = _seed_user(email, department="Systems")
    with SessionLocal() as db:
        db.add(Department(name="Sales"))
        db.commit()
    values = {
        "desc": _text("Not my team"),
        "team": _select("dept:Sales"),
        "owner": _select(f"person:{owner_id}"),
    }
    response = _post(
        "/api/slack/interactivity", _submission(callback_id, email, values)
    )
    assert response.status_code == 200
    assert response.json()["response_action"] == "errors"
    assert "team" in response.json()["errors"]
    with SessionLocal() as db:
        assert db.query(Todo).count() == 0
        assert db.query(Issue).count() == 0
        assert db.query(SlackViewSubmission).count() == 0


def test_interactivity_rechecks_roster_after_modal_was_opened(monkeypatch):
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import Issue, L10Meeting
    from rubik_core.database.models import (
        Department,
        SlackViewSubmission,
        UserPreference,
    )

    monkeypatch.setattr(settings, "EOS_L10_ROSTER_SCOPE_ENABLED", True)
    email = "rep@acme.example"
    _seed_user(email, department="Systems")
    with SessionLocal() as db:
        db.add(Department(name="Sales"))
        db.add(UserPreference(user_email=email, eos_team_optins=["Sales"]))
        db.add(
            L10Meeting(
                name="Sales L10",
                department_name="Sales",
                roster_emails=[email],
                google_event_id="evt-sales",
                created_by="seed",
            )
        )
        db.commit()

    opened = _post(
        "/api/slack/commands",
        {"command": "/ids", "user_id": "U123", "trigger_id": "TRIG1"},
    )
    assert opened.status_code == 200
    team_block = next(
        block
        for block in _FakeBot.opened[0][1]["blocks"]
        if block.get("block_id") == "team"
    )
    assert "dept:Sales" in {
        option["value"] for option in team_block["element"]["options"]
    }

    with SessionLocal() as db:
        meeting = (
            db.query(L10Meeting).filter(L10Meeting.google_event_id == "evt-sales").one()
        )
        meeting.roster_emails = []
        db.commit()

    values = {
        "desc": _text("Sales pipeline concern"),
        "team": _select("dept:Sales"),
        "priority": _select("medium"),
        "owner": _select("none"),
    }
    submitted = _post(
        "/api/slack/interactivity", _submission("eos_issue_create", email, values)
    )
    assert submitted.status_code == 200
    assert submitted.json()["response_action"] == "errors"
    assert "team" in submitted.json()["errors"]
    with SessionLocal() as db:
        assert db.query(Issue).count() == 0
        assert db.query(SlackViewSubmission).count() == 0


def test_interactivity_requires_description():
    email = "owner@acme.example"
    pid = _seed_user(email, department="Systems")
    values = {
        "desc": _text("   "),
        "team": _select("dept:Systems"),
        "owner": _select(f"person:{pid}"),
        "due": {"value": {"selected_date": None}},
    }
    resp = _post(
        "/api/slack/interactivity", _submission("eos_todo_create", email, values)
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["response_action"] == "errors"
    assert "desc" in data["errors"]


def test_issue_owner_options_capped_at_100_and_pollution_filtered():
    """Regression: /ids `views.open` failed with invalid_arguments
    ('no more than 100 items allowed .../owner/options') when eos_people had
    100+ rows — the Issue modal prepends '(unassigned)', so 1 + 100 = 101.
    The owner select must stay <= 100 AND drop .local/impersonation pollution."""
    from backend.routers import slack_commands as sc
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import Person

    email = "owner@acme.example"
    _seed_user(email, department="Systems")
    # 120 real people + one impersonation/.local pollution row, inserted
    # directly (sync only retires pseudo-users that exist in `users`).
    with SessionLocal() as db:
        for i in range(120):
            db.add(
                Person(
                    first_name=f"User{i:03d}",
                    last_name="X",
                    email=f"user{i:03d}@acme.example",
                    created_by="test",
                )
            )
        db.add(
            Person(
                first_name="Imp",
                last_name="Local",
                email="impersonated_user@systems.local",
                created_by="test",
            )
        )
        db.commit()

    with SessionLocal() as db:
        user = UserRegistry().get_user(email)
        view = sc._build_issue_view(user, db, prefill="", private_metadata="{}")

    owner_block = next(b for b in view["blocks"] if b.get("block_id") == "owner")
    opts = owner_block["element"]["options"]
    assert len(opts) <= 100  # Slack's hard limit — the bug was 101
    assert opts[0]["value"] == sc._UNASSIGNED
    # .local / impersonation rows never reach the picker.
    assert all(not o["value"].endswith("local") for o in opts)
    labels = [o["text"]["text"] for o in opts]
    assert "Imp Local" not in labels

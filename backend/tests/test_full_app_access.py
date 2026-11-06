"""The copilot (non-EOS) gate — ``require_full_app_access``.

The AI copilot (Chat, Daily Briefing, Productivity, Assets, …) ships open to
everyone in the workspace, department or not, and **EOS is company-wide**. An
admin can narrow the copilot by department in Feature Access; whoever is
narrowed out gets 403 on the copilot ``/api/*`` routes but keeps EOS + identity
endpoints so they can still work in EOS. The narrowed cases below install that
roster as data (``narrowed_copilot``).
"""

from __future__ import annotations

import base64
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user, user_can_access_full_app
from backend.main import app
from backend.tests._rosters import pin_rosters
from rubik_core.config import settings
from rubik_core.database.schemas import UserProfile
from rubik_core.features import FeaturePolicy

client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db_path = tmp_path / "test_full_app.db"
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    import rubik_core.database.db as dbmod

    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database import eos  # noqa: F401  — register EOS tables
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def narrowed_copilot(monkeypatch):
    """An admin narrowed the copilot to four departments (Exec team and admins
    still in) — the configuration the "outside the copilot" cases need."""
    pin_rosters(
        monkeypatch,
        {
            "copilot": FeaturePolicy(
                departments=frozenset({"sales", "marketing", "systems", "support"}),
                exec_allowed=True,
                admin_allowed=True,
            )
        },
    )


def _profile(email, *, department="general", roles=None, is_exec=False, managed=None):
    now = "2026-07-08T00:00:00"
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name=email.split("@")[0].title(),
        roles=roles or ["user"],
        department=department,
        is_exec=is_exec,
        managed_departments=managed or [],
        created_at=now,
        updated_at=now,
    )


# A simple copilot GET mounted behind require_full_app_access.
GATED = "/api/recent/"


def test_everyone_gets_the_copilot_by_default():
    for user in (
        _profile("a@x.example", roles=["admin"]),
        _profile("e@x.example", is_exec=True),
        _profile("hr@x.example", department="HR"),
        _profile("g@x.example", department="general"),
        _profile("n@x.example", department=""),
    ):
        assert user_can_access_full_app(user), user.email


def test_a_narrowed_copilot_matches_by_slug_and_keeps_the_role_doors(
    narrowed_copilot,
):
    # Admins and the Exec team are still in.
    assert user_can_access_full_app(_profile("a@x.example", roles=["admin"]))
    assert user_can_access_full_app(_profile("e@x.example", is_exec=True))
    # Rostered teams — matched by RAG slug, case-insensitively.
    for dept in ("Sales", "sales", " Marketing ", "SYSTEMS", "Support", "support"):
        assert user_can_access_full_app(_profile("u@x.example", department=dept)), dept
    # A managed (Accountability-Chart) rostered dept also qualifies.
    assert user_can_access_full_app(_profile("m@x.example", managed=["Systems"]))
    # Everyone else is limited to EOS.
    assert not user_can_access_full_app(_profile("hr@x.example", department="HR"))
    assert not user_can_access_full_app(_profile("g@x.example", department="general"))
    assert not user_can_access_full_app(_profile("n@x.example"))  # default 'general'


def test_a_person_with_no_department_reads_a_copilot_verdict_of_true():
    """The client reads the server's verdict from /api/users/me, so the nav and
    the API cannot disagree. Assert the verdict it would read."""
    app.dependency_overrides[get_current_user] = lambda: _profile(
        "new@x.example", department=""
    )
    r = client.get("/api/users/me")
    assert r.status_code == 200, r.text
    assert r.json()["features"]["copilot"] is True


def test_a_user_narrowed_out_gets_403_on_a_copilot_endpoint(narrowed_copilot):
    app.dependency_overrides[get_current_user] = lambda: _profile(
        "hr@x.example", department="HR"
    )
    r = client.get(GATED)
    assert r.status_code == 403
    assert "not turned on for you" in r.json()["detail"]


def test_everyone_with_the_copilot_passes_the_gate():
    # The gate runs before the handler; a pass is anything that isn't the 403
    # the gate would raise (the handler itself may 200 or error on empty state).
    for user in (
        _profile("s@x.example", department="sales"),
        _profile("mk@x.example", department="Marketing"),
        _profile("sy@x.example", department="Systems"),
        _profile("e@x.example", is_exec=True),
        _profile("a@x.example", roles=["admin"]),
    ):
        app.dependency_overrides[get_current_user] = lambda u=user: u
        r = client.get(GATED)
        assert r.status_code != 403, (user.email, r.status_code, r.text)


def test_eos_is_company_wide_even_for_someone_narrowed_out(narrowed_copilot):
    # EOS is ungated — someone outside the copilot must still reach it.
    app.dependency_overrides[get_current_user] = lambda: _profile(
        "hr@x.example", department="HR"
    )
    r = client.get("/api/eos/rocks/")
    assert r.status_code == 200, r.text


def test_identity_endpoint_ungated_for_someone_narrowed_out(narrowed_copilot):
    # /users/me bootstraps auth for EVERYONE (incl. EOS-only users) — the
    # copilot gate must never touch it.
    app.dependency_overrides[get_current_user] = lambda: _profile(
        "hr@x.example", department="HR"
    )
    r = client.get("/api/users/me")
    assert r.status_code != 403, r.text


# ── Regression: scheduler endpoints must be reachable by a key-only call ───────
#
# overnight/run, ingest/eos, priorities/deliver-slack and manager-digest
# authenticate with an X-Scheduler-Key and NO user token. When they sat under the
# copilot gate it 401'd them before their own auth ran — silently killing the
# nightly sales-agent, Slack delivery and manager digest (confirmed in Cloud Run
# logs). They now live on the UNGATED agents.scheduler_router (same
# /api/agents URLs), so a valid key reaches the handler while a keyless call is
# still rejected by each endpoint's own _is_authorized_run_caller.

_SCHED_KEY = "sched-secret-test"
_SCHED_ENDPOINTS = (
    "/api/agents/overnight/run",
    "/api/agents/ingest/eos",
    "/api/agents/priorities/deliver-slack",
    "/api/agents/priorities/manager-digest",
)


def test_scheduler_endpoints_reach_handler_with_key(monkeypatch):
    from backend.agents.slack_delivery import ManagerDigestResult, SlackDeliveryResult

    monkeypatch.setattr(settings, "SCHEDULER_API_KEY", _SCHED_KEY)
    monkeypatch.setattr(settings, "ENABLE_MOCK_AUTH", False)
    headers = {"X-Scheduler-Key": _SCHED_KEY, "X-Cubie-Workspace": "test"}

    overnight_fake = SimpleNamespace(
        run_id="t", written=0, by_type={}, plugin_results=[], errors=[]
    )
    eos_fake = SimpleNamespace(summary=lambda: "ok", succeeded=[], failed=[])

    with (
        patch("backend.agents.overnight.OvernightAgent") as overnight_agent,
        patch("rubik_core.ingestion.eos.ingest_all_sources", return_value=eos_fake),
        patch(
            "backend.jobs.launcher.launch_module_execution",
            return_value={"name": "executions/test"},
        ),
        patch(
            "backend.agents.slack_delivery.deliver_slack_priorities",
            return_value=SlackDeliveryResult(),
        ),
        patch(
            "backend.agents.slack_delivery.deliver_manager_digest",
            return_value=ManagerDigestResult(),
        ),
    ):
        overnight_agent.return_value.run.return_value = overnight_fake
        for path in _SCHED_ENDPOINTS:
            r = client.post(path, headers=headers)
            assert r.status_code == 200, (path, r.status_code, r.text)
            assert r.json()["status"] == (
                "dispatched"
                if path.endswith(("/overnight/run", "/ingest/eos"))
                else "completed"
            ), path


def test_scheduler_endpoints_reject_without_key(monkeypatch):
    # Ungated by the copilot gate, but still protected: no key + no user token → 403.
    monkeypatch.setattr(settings, "SCHEDULER_API_KEY", _SCHED_KEY)
    monkeypatch.setattr(settings, "ENABLE_MOCK_AUTH", False)
    for path in _SCHED_ENDPOINTS:
        assert client.post(path).status_code == 403, path


# ── Regression: the Slack OAuth callback must be reachable without a token ────
#
# slack.com redirects the BROWSER to /api/slack/callback without a bearer
# token. The public route now verifies the signed workspace state before it
# can open a tenant database.


@pytest.fixture
def slack_oauth_configured(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_MOCK_AUTH", False)
    monkeypatch.setenv(
        "CUBIE_OAUTH_STATE_SECRET", base64.urlsafe_b64encode(os.urandom(32)).decode()
    )


def test_slack_callback_without_a_bearer_rejects_unsigned_state(slack_oauth_configured):
    r = client.get(
        "/api/slack/callback",
        params={"code": "c", "state": "unbound"},
        follow_redirects=False,
    )
    assert r.status_code == 401, r.text
    assert "Invalid Slack OAuth state" in r.json()["detail"]


def test_slack_callback_refuses_forged_state(slack_oauth_configured):
    # A forged state cannot select a workspace or open its tenant DB.
    r = client.get(
        "/api/slack/callback",
        params={"code": "c", "state": "victim@x.example:nonce:0000000000000000"},
        follow_redirects=False,
    )
    assert r.status_code == 401, r.text


def test_slack_oauth_start_stays_gated(slack_oauth_configured, narrowed_copilot):
    # Only the callback moved: minting a signed state still needs a signed-in
    # copilot user.
    assert client.get("/api/slack/oauth-start").status_code == 401
    app.dependency_overrides[get_current_user] = lambda: _profile(
        "hr@x.example", department="HR"
    )
    assert client.get("/api/slack/oauth-start").status_code == 403

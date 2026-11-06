"""Person impersonation — "see Cubie as Kendall", read-only, no private data.

The locks are tested separately from the identity swap because they must hold
on their own: a request that carries the header is restricted whether or not
the swap was granted.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from backend import auth
from backend.auth import get_current_user
from backend.main import app
from backend.routers import users as users_router
from rubik_core.auth import impersonation
from rubik_core.company.operations import OperationsConfig, OperationsConfigUnavailable
from rubik_core.config import settings
from rubik_core.database.db import get_db
from rubik_core.database.schemas import UserProfile
from rubik_core.tools import _workspace_auth

client = TestClient(app)

ADMIN = "bellison@acme.example"
KENDALL = "knolan@acme.example"
H = {impersonation.HEADER: KENDALL}


def _profile(email: str, roles: list[str], department: str = "Systems") -> UserProfile:
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name="Kendall Nolan" if email == KENDALL else "Blair Ellison",
        department=department,
        roles=roles,
        created_at="2026-10-01T00:00:00",
        updated_at="2026-10-01T00:00:00",
    )


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    monkeypatch.setattr(settings, "USER_IMPERSONATION_ENABLED", True)
    monkeypatch.setattr(
        auth,
        "get_operations_config",
        lambda: OperationsConfig(
            impersonation_allowed_emails=(ADMIN,),
            user_impersonation_targets=(KENDALL,),
        ),
    )
    monkeypatch.setattr(
        auth.user_registry,
        "get_user",
        lambda email: (
            _profile(KENDALL, ["user", "admin"], "Sales") if email == KENDALL else None
        ),
    )
    yield
    app.dependency_overrides.clear()


def _request(headers: dict[str, str] | None = None) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request(
        {"type": "http", "method": "GET", "path": "/api/users/me", "headers": raw}
    )


# ── lock 1 + 2: read-only, deny-by-default allowlist ─────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "/api/users/me",
        "/api/departments/",
        "/api/sales/my-day",
        "/api/sales/rhythm/morning",
        "/api/eos/rocks",
        "/api/scripts",
        "/api/admin/impersonation/targets",
    ],
)
def test_hubspot_eos_and_profile_reads_are_open(path):
    assert impersonation.is_allowed("GET", path)


@pytest.mark.parametrize(
    "path",
    [
        "/api/email/inbox",
        "/api/calendar/events",
        "/api/drive/files",
        "/api/transcripts",
        "/api/calls/pending",
        "/api/fathom/recordings",
        "/api/meet-addon/context",
        "/api/chat/sessions",
        "/api/homefeed",
        "/api/homefeed/prep-runs",
        "/api/promises",
        "/api/proposals",
        "/api/crm-sync/suggestions",
        "/api/rag/search",
        "/api/xray/customers",
        "/api/scriptsX",
        "//api/email/inbox",
        "/API/email",
        "/api/some-router-added-next-year",
    ],
)
def test_everything_else_is_closed(path):
    assert not impersonation.is_allowed("GET", path)


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_every_write_is_closed_even_on_allowed_surfaces(method):
    assert not impersonation.is_allowed(method, "/api/eos/todos")
    assert not impersonation.is_allowed(method, "/api/sales/my-day")


def test_the_session_mint_and_the_spa_stay_open():
    assert impersonation.is_allowed("POST", "/api/auth/session")
    assert impersonation.is_allowed("GET", "/sales")
    assert impersonation.is_allowed("GET", "/assets/index.js")


def test_the_middleware_refuses_on_the_header_alone_before_auth():
    # No credentials at all: the header is enough to be restricted.
    res = client.get("/api/email/inbox", headers=H)
    assert res.status_code == 403
    assert res.json()["code"] == "impersonation_blocked"
    res = client.post("/api/eos/todos", headers=H, json={})
    assert res.status_code == 403


# ── lock 3: no Google account access ─────────────────────────────────────────


def test_no_google_account_opens_while_impersonating():
    token = impersonation.set_active(True)
    try:
        assert _workspace_auth.load_workspace_credentials(["scope"]) is None
    finally:
        impersonation.reset_active(token)


def test_transcript_text_is_withheld_on_the_cockpit():
    token = impersonation.set_active(True)
    try:
        assert auth.cockpit_transcript_policy(_profile(KENDALL, ["user"])) == "none"
        assert not auth.user_can_read_transcript_text(
            _profile(KENDALL, ["user"]), call_owner_email=KENDALL
        )
    finally:
        impersonation.reset_active(token)


# ── the identity swap ────────────────────────────────────────────────────────


def test_an_allowlisted_admin_becomes_tianas_real_profile_without_admin():
    req = _request(H)
    out = auth._apply_impersonation(req, _profile(ADMIN, ["user", "admin"]))
    assert out.email == KENDALL
    assert out.department == "Sales"
    assert "admin" not in out.roles
    assert req.state.impersonation == {
        "mode": "user",
        "actor_email": ADMIN,
        "target_email": KENDALL,
        "target_name": "Kendall Nolan",
    }


def test_an_admin_off_the_allowlist_stays_themselves():
    other = _profile("pquill@acme.example", ["user", "admin"])
    assert auth._apply_impersonation(_request(H), other).email == other.email


def test_another_workspace_policy_cannot_grant_person_impersonation(monkeypatch):
    monkeypatch.setattr(
        auth,
        "get_operations_config",
        lambda: OperationsConfig(
            impersonation_allowed_emails=("admin@bravo.example",),
            user_impersonation_targets=(KENDALL,),
        ),
    )
    actor = _profile(ADMIN, ["user", "admin"])
    assert auth._apply_impersonation(_request(H), actor).email == ADMIN


def test_unavailable_workspace_policy_refuses_impersonation(monkeypatch):
    def unavailable():
        raise OperationsConfigUnavailable("tenant settings unavailable")

    monkeypatch.setattr(auth, "get_operations_config", unavailable)
    actor = _profile(ADMIN, ["user", "admin"])
    assert auth._apply_impersonation(_request(H), actor).email == ADMIN
    assert not auth._may_impersonate(ADMIN)


def test_a_non_admin_stays_themselves():
    me = _profile(ADMIN, ["user"])
    assert auth._apply_impersonation(_request(H), me).email == ADMIN


def test_a_person_not_on_the_list_is_refused():
    req = _request({impersonation.HEADER: "emery@acme.example"})
    with pytest.raises(HTTPException) as exc:
        auth._apply_impersonation(req, _profile(ADMIN, ["user", "admin"]))
    assert exc.value.status_code == 403


def test_switched_off_means_no_swap(monkeypatch):
    monkeypatch.setattr(settings, "USER_IMPERSONATION_ENABLED", False)
    me = _profile(ADMIN, ["user", "admin"])
    assert auth._apply_impersonation(_request(H), me).email == ADMIN


def test_the_person_header_wins_over_the_department_header():
    req = _request({**H, "X-Impersonate-Department": "Marketing"})
    out = auth._apply_impersonation(req, _profile(ADMIN, ["user", "admin"]))
    assert out.email == KENDALL


def test_users_me_reports_and_audits_the_impersonation(monkeypatch):
    rows: list[dict] = []
    monkeypatch.setattr(users_router, "record_audit", lambda db, **kw: rows.append(kw))
    monkeypatch.setattr(users_router, "visible_permissions", lambda user: {})
    monkeypatch.setattr(
        "rubik_core.features.resolve_all",
        lambda user, db, modules=None: {"sales_cockpit": True},
    )
    admin = _profile(ADMIN, ["user", "admin"])

    def as_admin(request: Request) -> UserProfile:
        return auth._apply_impersonation(request, admin)

    app.dependency_overrides[get_current_user] = as_admin
    app.dependency_overrides[get_db] = lambda: object()

    res = client.get("/api/users/me", headers=H)
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["email"] == KENDALL
    assert body["impersonation"]["actor_email"] == ADMIN
    assert rows == [
        {
            "actor_email": ADMIN,
            "action": "IMPERSONATE_USER",
            "target": KENDALL,
            "details": {"mode": "read_only"},
        }
    ]


# ── Ask Cubie while impersonating: HubSpot only, nothing saved ───────────────


def test_the_hubspot_only_chat_is_the_only_write_allowed():
    assert impersonation.is_allowed("POST", "/api/chat/impersonated")
    assert not impersonation.is_allowed("POST", "/api/chat/")
    assert not impersonation.is_allowed("POST", "/api/chat/stream")
    assert not impersonation.is_allowed("GET", "/api/chat/impersonated")
    assert not impersonation.is_allowed("POST", "/api/chat/impersonated/x")


def test_the_hubspot_only_agent_has_exactly_two_hubspot_tools():
    from rubik_core.agent_runtime.hubspot_only_chat import build_hubspot_only_agent

    agent, _ = build_hubspot_only_agent(user_email=KENDALL, user_name="Kendall Nolan")
    assert sorted(t.__name__ for t in agent.tools) == [
        "find_hubspot_contact",
        "search_hubspot_data",
    ]
    assert agent.sub_agents == []
    assert all(t.__self__.user_email == KENDALL for t in agent.tools)


def _impersonating_admin(monkeypatch):
    from backend.routers import chat_impersonated

    admin = _profile(ADMIN, ["user", "admin"])

    def as_admin(request: Request) -> UserProfile:
        return auth._apply_impersonation(request, admin)

    app.dependency_overrides[get_current_user] = as_admin
    app.dependency_overrides[get_db] = lambda: object()
    monkeypatch.setattr("rubik_core.features.policy.feature_allows", lambda k, u: True)
    monkeypatch.setattr(auth, "feature_allows", lambda k, u: True)
    audits: list[dict] = []
    monkeypatch.setattr(
        chat_impersonated, "record_audit", lambda db, **kw: audits.append(kw)
    )
    return chat_impersonated, audits


def test_chat_answers_as_the_person_and_saves_nothing(monkeypatch):
    from rubik_core.agent_runtime.hubspot_only_chat import HubSpotOnlyResult

    mod, audits = _impersonating_admin(monkeypatch)
    seen: dict = {}

    async def fake_turn(**kw):
        seen.update(kw)
        return HubSpotOnlyResult(
            answer="4 of 27 deals", model_id="m", tool_calls=["search_hubspot_data"]
        )

    monkeypatch.setattr(mod, "run_hubspot_only_turn", fake_turn)
    res = client.post(
        "/api/chat/impersonated",
        headers=H,
        json={
            "query": "which of my deals are waiting on the decision-maker?",
            "history": [{"role": "user", "content": "hi"}],
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["answer"] == "4 of 27 deals"
    assert seen["user_email"] == KENDALL
    assert seen["history"] == [("user", "hi")]
    assert audits[0]["action"] == "IMPERSONATE_CHAT"
    assert audits[0]["actor_email"] == ADMIN and audits[0]["target"] == KENDALL
    assert "query" not in audits[0]["details"]  # the question is not stored


def test_chat_refuses_without_a_granted_impersonation(monkeypatch):
    mod, _ = _impersonating_admin(monkeypatch)
    called = []
    monkeypatch.setattr(mod, "run_hubspot_only_turn", lambda **kw: called.append(1))
    # No header at all: not an impersonation.
    assert client.post("/api/chat/impersonated", json={"query": "x"}).status_code == 403
    # Header refused (admin off the allowlist): restricted, but no swap → 403.
    monkeypatch.setattr(auth, "get_operations_config", OperationsConfig)
    res = client.post("/api/chat/impersonated", headers=H, json={"query": "x"})
    assert res.status_code == 403
    assert called == []

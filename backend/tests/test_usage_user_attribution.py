"""AI spend carries the person it was spent for (``llm_usage_events.user_email``).

Before this, nearly every recorded AI call had no user, so neither Admin → AI
Cost nor Insights' AI Spend could split spend by person or department. Two
paths now stamp one:

1. **Requests.** ``bind_usage_user`` (``backend/auth.py``) sits on the ``core``
   parent router and sets the ``current_user_email`` ContextVar. It must be an
   ASYNC dependency: FastAPI runs a sync one in a worker thread on a COPY of
   the context, so the value would never reach the endpoint. Both halves are
   proven here, the working pattern and the broken one, plus a real ``core``
   endpoint of the real app.
2. **Batch runs.** ``attribute_to(..., user_arg="user_email")`` stamps the user
   the run is for on the three priorities runners (the Cloud Run Job, the
   in-service scheduler path and the on-demand path).

Collaborators are faked by STRING target (sibling suites ``importlib.reload()``
modules mid-run).
"""

from __future__ import annotations

import importlib
from unittest.mock import patch

import pytest
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.testclient import TestClient

from backend.auth import bind_usage_user, get_current_user
from rubik_core.database.schemas import UserProfile

PERSON = "Rep.One@ACME.example"


def _usage():
    # Resolved at call time: a sibling suite may have reloaded the module.
    return importlib.import_module("rubik_core.cost.usage")


@pytest.fixture(autouse=True)
def _buffer(monkeypatch):
    """An empty usage buffer that never flushes to a database mid-test."""
    usage = _usage()
    monkeypatch.setattr("rubik_core.cost.usage._FLUSH_THRESHOLD", 10**9)
    monkeypatch.setattr("rubik_core.cost.usage._FLUSH_INTERVAL_S", 1e9)
    monkeypatch.setenv("AI_COST_TRACKING_ENABLED", "true")
    with usage._LOCK:
        usage._BUFFER.clear()
    yield
    with usage._LOCK:
        usage._BUFFER.clear()


def _profile(email=PERSON):
    now = "2026-10-08T00:00:00"
    return UserProfile(
        id="rep",
        email=email,
        name="Rep One",
        roles=["user"],
        department="Sales",
        created_at=now,
        updated_at=now,
    )


def _spend():
    """One LLM call's usage record, exactly as the shared client makes it."""
    _usage().record_usage(
        operation="generate",
        model="gemini-2.5-flash",
        prompt_tokens=100,
        completion_tokens=20,
    )


def _recorded_users():
    usage = _usage()
    with usage._LOCK:
        return [
            event["user_email"] for events in usage._BUFFER.values() for event in events
        ]


# ── 1. the request path ──────────────────────────────────────────────────────


def _mini_app(dependency):
    """The ``core`` pattern from backend/main.py, reduced to two endpoints."""
    mini = FastAPI()
    core = APIRouter(prefix="/api", dependencies=[Depends(dependency)])

    @core.get("/async-llm")
    async def async_llm():
        _spend()
        return {"ok": True}

    @core.get("/sync-llm")
    def sync_llm():
        _spend()
        return {"ok": True}

    mini.include_router(core)
    mini.dependency_overrides[get_current_user] = lambda: _profile()
    return mini


@pytest.mark.parametrize("path", ["/api/async-llm", "/api/sync-llm"])
def test_an_llm_call_inside_a_core_endpoint_carries_the_user(path):
    with TestClient(_mini_app(bind_usage_user)) as client:
        assert client.get(path).status_code == 200
    assert _recorded_users() == ["rep.one@acme.example"]


def test_a_sync_dependency_would_not_reach_the_endpoint():
    """Why ``bind_usage_user`` is async: the same body as a SYNC dependency sets
    the ContextVar in a worker thread's copy of the context, and the endpoint
    never sees it. If this ever starts passing, the docstring is out of date."""

    def sync_bind(user: UserProfile = Depends(get_current_user)) -> None:
        _usage().current_user_email.set(user.email.lower())

    with TestClient(_mini_app(sync_bind)) as client:
        assert client.get("/api/async-llm").status_code == 200
    assert _recorded_users() == [None]


def test_the_real_actor_wins_over_an_impersonated_identity():
    """``get_current_user`` stashes the authenticated person before masking;
    the spend is theirs, not the impersonated pseudo-user's."""

    def _stash(request: Request) -> None:
        request.state.actor_email = "admin@acme.example"

    mini = FastAPI()
    core = APIRouter(
        prefix="/api", dependencies=[Depends(_stash), Depends(bind_usage_user)]
    )

    @core.get("/llm")
    async def llm():
        _spend()
        return {"ok": True}

    mini.include_router(core)
    mini.dependency_overrides[get_current_user] = lambda: _profile(
        "impersonated_user@sales.local"
    )
    with TestClient(mini) as client:
        assert client.get("/api/llm").status_code == 200
    assert _recorded_users() == ["admin@acme.example"]


def test_a_real_core_endpoint_records_the_signed_in_person():
    """The real app, the real ``core`` mount: GET /api/preferences, with the one
    collaborator it calls standing in for an LLM call."""
    from backend.auth import require_full_app_access
    from backend.main import app
    from rubik_core.database.db import get_db

    def _prefs_with_spend(db, email):
        _spend()
        return None

    app.dependency_overrides[get_current_user] = lambda: _profile()
    app.dependency_overrides[require_full_app_access] = lambda: None
    app.dependency_overrides[get_db] = lambda: None
    try:
        with (
            patch(
                "backend.routers.preferences.get_user_preferences",
                side_effect=_prefs_with_spend,
            ),
            patch("backend.routers.preferences._read_shortcut", return_value=None),
            TestClient(app) as client,
        ):
            assert client.get("/api/preferences").status_code == 200
    finally:
        app.dependency_overrides.clear()
    assert _recorded_users() == ["rep.one@acme.example"]


def test_a_refused_caller_never_reaches_the_binding():
    """``bind_usage_user`` comes after the copilot gate on ``core``."""
    from fastapi import HTTPException

    from backend.auth import require_full_app_access
    from backend.main import app

    calls = []

    def _refuse():
        raise HTTPException(status_code=403, detail="pilot")

    def _never():
        calls.append("bound")

    app.dependency_overrides[require_full_app_access] = _refuse
    app.dependency_overrides[bind_usage_user] = _never
    try:
        with TestClient(app) as client:
            assert client.get("/api/preferences").status_code == 403
    finally:
        app.dependency_overrides.clear()
    assert calls == []


# ── 2. batch runs ────────────────────────────────────────────────────────────


def test_attribute_to_stamps_the_named_user_argument_and_restores_it():
    usage = _usage()
    seen = []

    @usage.attribute_to("ai_priorities", user_arg="user_email")
    def run(user_email, report_kind="daily"):
        seen.append((usage.current_feature.get(), usage.current_user_email.get()))
        _spend()

    @usage.attribute_to("ai_priorities", user_arg="user_email")
    async def run_async(*, user_email):
        seen.append((usage.current_feature.get(), usage.current_user_email.get()))

    run(PERSON)
    run(user_email="other@acme.example")
    import asyncio

    asyncio.run(run_async(user_email=PERSON))
    assert seen == [
        ("ai_priorities", "rep.one@acme.example"),
        ("ai_priorities", "other@acme.example"),
        ("ai_priorities", "rep.one@acme.example"),
    ]
    assert _recorded_users() == ["rep.one@acme.example", "other@acme.example"]
    assert usage.current_user_email.get() is None
    assert usage.current_feature.get() == "unattributed"


class _Captured(Exception):
    pass


def _capture_into(seen):
    def _capture(*_a, **_k):
        usage = _usage()
        seen.append((usage.current_feature.get(), usage.current_user_email.get()))
        raise _Captured

    return _capture


def test_the_priorities_job_runner_records_the_user_it_runs_for():
    seen = []
    with (
        patch("rubik_core.promises.accountability.run_accountability_pass"),
        patch(
            "rubik_core.priorities.PrioritiesOrchestrator.generate",
            side_effect=_capture_into(seen),
        ),
    ):
        from rubik_core.priorities.batch import run_priorities_for_user

        result = run_priorities_for_user(PERSON)
    assert result.succeeded is False  # the capture stopped the run
    assert seen == [("ai_priorities", "rep.one@acme.example")]


def test_the_in_service_scheduler_runner_records_the_user():
    seen = []
    with patch(
        "rubik_core.priorities.PrioritiesOrchestrator.generate",
        side_effect=_capture_into(seen),
    ):
        from backend.routers.jobs import _run_priorities_for_user

        _run_priorities_for_user(PERSON)
    assert seen == [("ai_priorities", "rep.one@acme.example")]


def test_the_on_demand_runner_records_the_user():
    seen = []
    with patch(
        "backend.routers.home._load_priorities_ui_prefs",
        side_effect=_capture_into(seen),
    ):
        from backend.routers.home import _run_and_persist_priorities

        with pytest.raises(_Captured):
            _run_and_persist_priorities(PERSON, None)
    assert seen == [("ai_priorities", "rep.one@acme.example")]

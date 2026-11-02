"""Insights usage telemetry: what counts as usage, and how it lands in ``activity_daily``.

What this file pins (``rubik_core/insights/activity.py``):

1. **The area map.** Every product router maps to its area; plumbing (health,
   ``/users/me``, session mint, the notification bell, the Recent list),
   status polls, Smart Teams and anything outside ``/api`` do not count.
2. **The route template is the FULL one.** FastAPI 0.13x keeps included routers
   lazily, and ``scope["route"].path`` lost the include prefix there; a real
   request through the real app must still land in the right area.
3. **Who counts.** The authenticated person ``get_current_user`` stashed, never
   an impersonated identity, only on a 2xx/3xx answer.
4. **Buffered, folded, upserted.** Requests fold in memory by (person, local
   day, area); a flush is one upsert that ADDS to the stored count.
5. **Local days.** The day is the company's business time zone's, not UTC's:
   with the fixture clock (America/Chicago), 03:00 UTC is still the previous day.
6. **Kill switch, and never raises.**

Collaborators are faked by STRING target (sibling suites ``importlib.reload()``
modules mid-run).
"""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from rubik_core.insights import activity

PERSON = "Rep.One@ACME.example"


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """The app's ``SessionLocal`` bound to a throwaway sqlite file with every
    table, and an empty activity buffer before and after."""
    import rubik_core.database.db as dbmod
    from rubik_core.config import settings
    from rubik_core.database.models import Base

    monkeypatch.setattr(
        settings, "DATABASE_URL", f"sqlite:///{tmp_path / 'insights_activity.db'}"
    )
    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)
    Base.metadata.create_all(bind=engine)
    activity.reset_buffer()
    try:
        yield dbmod.SessionLocal
    finally:
        activity.reset_buffer()
        app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _no_background_flush(monkeypatch):
    """Flushes run inline when due, so no daemon thread races a test."""

    def _inline():
        import rubik_core.insights.activity as mod

        mod.flush_activity()

    monkeypatch.setattr("rubik_core.insights.activity._start_background_flush", _inline)
    activity.reset_buffer()
    yield
    activity.reset_buffer()


def _rows(session_factory):
    from rubik_core.database.insights import ActivityDaily

    with session_factory() as db:
        return {
            (r.user_email, r.day, r.area): r
            for r in db.query(ActivityDaily).order_by(ActivityDaily.id).all()
        }


# ── 1. the area map ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("template", "area"),
    [
        ("/api/chat/stream", "chat"),
        ("/api/chat/sessions/{session_id}", "chat"),
        ("/api/homefeed/priorities", "priorities"),
        ("/api/homefeed/priorities/{priority_id}/dismiss", "priorities"),
        ("/api/homefeed/priorities/{priority_id}/slack-followup", "priorities"),
        ("/api/homefeed/prep-runs/{run_id}", "meeting_prep"),
        ("/api/homefeed/meeting-prep/{event_id:path}", "meeting_prep"),
        ("/api/homefeed/generate", "briefing"),
        ("/api/email/summary", "briefing"),
        ("/api/proposals/{proposal_id}/generate", "proposals"),
        ("/api/crm-sync/suggestions", "crm_sync"),
        ("/api/calls/start", "call_capture"),
        ("/api/scripts/{product_key}", "call_capture"),
        ("/api/sales/loose-ends", "loose_ends"),
        ("/api/sales/loose-ends/{item_id}/log", "loose_ends"),
        ("/api/sales/my-day", "cockpit"),
        ("/api/todos", "todos"),
        ("/api/todos/{promise_id}/kept", "todos"),
        ("/api/eos/todos/{todo_id}", "eos"),  # EOS To-Dos are EOS, not My To-Dos
        ("/api/eos/l10/sessions/start", "eos"),
        ("/api/assets/{doc_id:path}", "assets"),
        ("/api/dam/upload", "assets"),
        ("/api/drive/{file_id}/add-to-assets", "assets"),
        ("/api/drive/preferences", "settings"),
        ("/api/xray/{company_id}", "xray"),
        ("/api/voice/commands", "voice"),
        ("/api/scheduled-tasks/{task_id}/run-now", "scheduled_tasks"),
        ("/api/directory/search", "search"),
        ("/api/insights/dashboards/{slug}", "insights"),
        ("/api/preferences", "settings"),
        ("/api/admin/feature-access", "admin"),
        ("/api/users/{email}", "admin"),
        ("/api/feedback/admin", "admin"),
        ("/api/a-router-nobody-mapped-yet", "other"),
    ],
)
def test_product_routes_map_to_their_area(template, area):
    assert activity.area_for_route(template) == area


@pytest.mark.parametrize(
    "template",
    [
        None,
        "",
        "/health",
        "/docs",
        "/{full_path:path}",
        "/api/health/models",
        "/api/users/me",
        "/api/auth/session",
        "/api/notifications/",
        "/api/notifications/{notification_id}/read",
        "/api/recent/touch",
        "/api/recent/",
        "/api/departments/",
        "/api/ingest/meet-transcripts",
        "/api/fathom/ingest",
        "/api/jobs/priorities/run-all",
        "/api/agents/jobs/cockpit/daily-digest",
        "/api/slack/commands",
        "/api/smart-teams/guides/generate",
        # status polls
        "/api/eos/l10/sessions/{session_id}/state",
        "/api/homefeed/priorities/runs/{run_id}",
        "/api/homefeed/prep-runs/{run_id}/status",
        "/api/homefeed/deep-prep/{run_id}/status",
        "/api/dam/{doc_id:path}/status",
        "/api/crm-sync/runs/{run_id}",
        "/api/xray/runs/{snapshot_id}",
    ],
)
def test_plumbing_polls_and_non_api_routes_are_not_usage(template):
    assert activity.area_for_route(template) is None


def test_the_action_that_started_a_polled_run_still_counts():
    assert activity.area_for_route("/api/homefeed/priorities") == "priorities"
    assert activity.area_for_route("/api/crm-sync/scan") == "crm_sync"
    assert activity.area_for_route("/api/eos/l10/sessions/{session_id}") == "eos"


def test_every_real_route_maps_into_the_vocabulary():
    from fastapi.routing import iter_route_contexts

    seen = set()
    for ctx in iter_route_contexts(app.routes):
        area = activity.area_for_route(ctx.path)
        assert area is None or area in activity.AREAS, (ctx.path, area)
        seen.add(area)
    # The big product areas are all reachable through a real mounted route.
    assert {"chat", "priorities", "eos", "assets", "admin", "settings"} <= seen


# ── 2/3. the middleware, through the real app ───────────────────────────────


def _real_auth(monkeypatch, session_factory, *, department="Sales"):
    """Mock auth (``X-Mock-User``) through the REAL ``get_current_user``, so
    the stash the middleware reads is the production one. The full-app gate is
    overridden: it is not what this file is about."""
    from backend.auth import require_full_app_access
    from rubik_core.config import settings
    from rubik_core.database.models import User

    monkeypatch.setattr(settings, "ENABLE_MOCK_AUTH", True)
    with session_factory() as db:
        db.add(
            User(
                email=PERSON.lower(),
                id="rep-one",
                name="Rep One",
                department=department,
            )
        )
        db.commit()
    app.dependency_overrides[require_full_app_access] = lambda: None


def test_a_real_request_is_counted_in_its_area_with_a_department_snapshot(
    fresh_db, monkeypatch
):
    _real_auth(monkeypatch, fresh_db)
    with TestClient(app) as client:
        for _ in range(3):
            assert (
                client.get("/api/preferences", headers={"X-Mock-User": PERSON.lower()})
            ).status_code == 200
        # Plumbing on the same request path does not count.
        client.get("/api/users/me", headers={"X-Mock-User": PERSON.lower()})
    assert activity.flush_activity() == 1
    rows = _rows(fresh_db)
    [(key, row)] = rows.items()
    assert key[0] == "rep.one@acme.example"
    assert key[2] == "settings"
    assert row.requests == 3
    assert row.department_slug == "sales"


def test_the_full_template_is_used_for_included_routers(fresh_db, monkeypatch):
    """``/api/eos/...`` is two include levels deep; the router-local path is
    ``/rocks/...``, which would map to nothing."""
    seen = []

    def _capture(request, status_code):
        seen.append(activity.route_template(request.scope))

    with (
        patch("rubik_core.insights.activity.record_request", side_effect=_capture),
        TestClient(app) as client,
    ):
        client.get("/api/eos/rocks/abc")
        client.get("/health")
    assert seen[0].startswith("/api/eos/rocks")
    assert activity.area_for_route(seen[0]) == "eos"
    assert seen[1] == "/health"


def test_an_unauthenticated_request_is_not_counted(fresh_db):
    with TestClient(app) as client:
        client.get("/health")
    assert activity.flush_activity() == 0


def test_a_failing_request_is_not_counted(fresh_db, monkeypatch):
    _real_auth(monkeypatch, fresh_db)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/api/homefeed/priorities/no-such-card/dismiss",
            headers={"X-Mock-User": PERSON.lower()},
        )
    assert resp.status_code == 404
    assert activity.flush_activity() == 0


def test_the_middleware_never_fails_a_request(fresh_db):
    with (
        patch(
            "rubik_core.insights.activity.record_request",
            side_effect=RuntimeError("telemetry down"),
        ),
        TestClient(app) as client,
    ):
        assert client.get("/health").status_code == 200


def _request(path="/api/chat/stream", query_string=b"", **state):
    return SimpleNamespace(
        state=SimpleNamespace(**state),
        scope={
            "path": path,
            "query_string": query_string,
            "route": SimpleNamespace(path=path),
        },
    )


def test_personal_gmail_requests_do_not_count_as_shared_activity():
    for request in (
        _request("/api/email/accounts/connect", actor_email=PERSON),
        _request(
            "/api/email/inbox",
            query_string=b"account_id=personal-account",
            actor_email=PERSON,
        ),
        _request(
            "/api/email/drafts",
            actor_email=PERSON,
            private_gmail_activity=True,
        ),
    ):
        activity.record_request(request, 200)
    assert activity._BUFFER == {}

    activity.record_request(_request("/api/email/inbox", actor_email=PERSON), 200)
    assert len(activity._BUFFER) == 1


def test_impersonated_requests_are_skipped():
    activity.record_request(
        _request(
            actor_email="admin@acme.example",
            impersonation={"mode": "user", "target_email": "rep@acme.example"},
        ),
        200,
    )
    activity.record_request(
        _request(actor_email="admin@acme.example", actor_impersonating=True), 200
    )
    assert activity._BUFFER == {}
    activity.record_request(_request(actor_email="admin@acme.example"), 200)
    assert len(activity._BUFFER) == 1


@pytest.mark.parametrize("status", [199, 401, 403, 404, 500])
def test_only_2xx_and_3xx_count(status):
    activity.record_request(_request(actor_email="rep@acme.example"), status)
    assert activity._BUFFER == {}


def test_department_impersonation_is_flagged_on_the_request_state():
    """Department impersonation sets no ``request.state.impersonation`` of its
    own; ``get_current_user`` must flag it from the identity swap."""
    from backend.auth import get_current_user
    from rubik_core.database.schemas import UserProfile

    now = "2026-10-08T00:00:00"

    def _profile(email, dept):
        return UserProfile(
            id=email,
            email=email,
            name=email,
            roles=["admin", "user"],
            department=dept,
            created_at=now,
            updated_at=now,
        )

    admin = _profile("Admin@ACME.example", "Systems")
    masked = _profile("impersonated_user@sales.local", "Sales")
    request = SimpleNamespace(state=SimpleNamespace(), headers={})
    with (
        patch("backend.auth._authenticate_user", return_value=admin),
        patch("backend.auth._apply_impersonation", return_value=masked),
    ):
        assert get_current_user(request, None) is masked
    assert request.state.actor_email == "admin@acme.example"
    assert request.state.actor_department == "Systems"
    assert request.state.actor_impersonating is True

    plain = SimpleNamespace(state=SimpleNamespace(), headers={})
    with (
        patch("backend.auth._authenticate_user", return_value=admin),
        patch("backend.auth._apply_impersonation", side_effect=lambda r, u: u),
    ):
        get_current_user(plain, None)
    assert plain.state.actor_email == "admin@acme.example"
    assert not getattr(plain.state, "actor_impersonating", False)


# ── 4. buffered, folded, upserted ────────────────────────────────────────────


def test_requests_fold_in_memory_and_the_upsert_adds_to_the_stored_count(fresh_db):
    t1 = datetime(2026, 10, 8, 15, 0)
    t2 = datetime(2026, 10, 8, 16, 30)
    t0 = datetime(2026, 10, 8, 13, 5)
    for at in (t1, t2, t1):
        activity.record_activity(PERSON, "chat", department="Sales", at=at)
    activity.record_activity(PERSON, "eos", department="Sales", at=t1)
    assert len(activity._BUFFER) == 2  # folded: 4 requests, 2 rows
    assert activity.flush_activity() == 2

    rows = _rows(fresh_db)
    chat = rows[("rep.one@acme.example", date(2026, 10, 8), "chat")]
    assert (chat.requests, chat.first_at, chat.last_at) == (3, t1, t2)
    assert chat.department_slug == "sales"

    # A second batch for the same key ADDS and widens the window.
    activity.record_activity(PERSON, "chat", department="Sales", at=t0)
    activity.record_activity(PERSON, "chat", department="Sales", at=t1)
    assert activity.flush_activity() == 1
    chat = _rows(fresh_db)[("rep.one@acme.example", date(2026, 10, 8), "chat")]
    assert (chat.requests, chat.first_at, chat.last_at) == (5, t0, t2)


def test_a_flush_happens_at_the_threshold_and_not_before(fresh_db):
    at = datetime(2026, 10, 8, 15, 0)
    with patch(
        "rubik_core.insights.activity.flush_activity",
        wraps=activity.flush_activity,
    ) as flush:
        activity._last_flush_monotonic = activity.time.monotonic()
        for _ in range(activity._FLUSH_THRESHOLD - 1):
            activity.record_activity(PERSON, "chat", at=at)
        assert flush.call_count == 0
        activity.record_activity(PERSON, "chat", at=at)
        assert flush.call_count == 1
    row = _rows(fresh_db)[("rep.one@acme.example", date(2026, 10, 8), "chat")]
    assert row.requests == activity._FLUSH_THRESHOLD


def test_unknown_areas_and_missing_people_are_ignored():
    activity.record_activity(PERSON, "not_an_area")
    activity.record_activity("", "chat")
    activity.record_activity(None, "chat")
    assert activity._BUFFER == {}


def test_a_failed_flush_drops_the_batch_and_never_raises(fresh_db):
    activity.record_activity(PERSON, "chat")
    with patch(
        "rubik_core.database.db.SessionLocal", side_effect=RuntimeError("db down")
    ):
        assert activity.flush_activity() == 0
    assert activity._BUFFER == {}


# ── 5. Local days ────────────────────────────────────────────────────────────


def test_three_am_utc_is_the_previous_local_day(fresh_db):
    assert activity.local_day(datetime(2026, 10, 8, 3, 0)) == date(2026, 10, 7)
    assert activity.local_day(datetime(2026, 10, 8, 5, 0)) == date(2026, 10, 8)
    # Winter: CST is UTC-6, so 05:30 UTC is still the previous day.
    assert activity.local_day(datetime(2026, 1, 15, 5, 30)) == date(2026, 1, 14)

    activity.record_activity(PERSON, "chat", at=datetime(2026, 10, 8, 3, 0))
    activity.flush_activity()
    assert set(_rows(fresh_db)) == {("rep.one@acme.example", date(2026, 10, 7), "chat")}


# ── 6. the kill switch ───────────────────────────────────────────────────────


def test_the_kill_switch_records_nothing(monkeypatch):
    from rubik_core.config import settings

    monkeypatch.setattr(settings, "INSIGHTS_ENABLED", False, raising=False)
    activity.record_activity(PERSON, "chat")
    activity.record_request(_request(actor_email="rep@acme.example"), 200)
    assert activity._BUFFER == {}

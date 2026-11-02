"""Scheduled tasks — the HTTP surface and the scheduler tick.

The gate matrix: the env switch answers "does this exist here at all" (404),
the roster answers "may this person reach it" (403), and ownership answers
"whose is it" (404 for somebody else's, never 403, so an id cannot be probed).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.auth import get_current_user
from backend.main import app
from rubik_core.auth.registry import UserProfile
from rubik_core.config import settings
from rubik_core.database.db import get_db
from rubik_core.database.models import Base
from rubik_core.scheduled_tasks import schedule as sched
from rubik_core.scheduled_tasks import store

OWNER = "bellison@acme.example"
STRANGER = "stranger@acme.example"

client = TestClient(app)


def _profile(email: str, *, is_admin=False, department="Systems") -> UserProfile:
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name=email.split("@")[0].title(),
        department=department,
        roles=["admin"] if is_admin else ["user"],
        is_exec=False,
        managed_departments=[],
        created_at="2026-09-01T00:00:00",
        updated_at="2026-09-01T00:00:00",
    )


def _as(email: str, **kw) -> None:
    app.dependency_overrides[get_current_user] = lambda: _profile(email, **kw)


@pytest.fixture
def db(monkeypatch, tmp_path):
    # A FILE-backed sqlite db, not ":memory:" + StaticPool. The code under test
    # opens its own short-lived sessions (as it does in production), and
    # StaticPool pins every one of them to a SINGLE connection — so two
    # overlapping transactions raise "cannot start a transaction within a
    # transaction". That surfaced only on CI, only in a full-suite run, because
    # it needs another session to be mid-transaction. A file gives each session
    # its own connection, which is the production shape.
    engine = create_engine(
        f"sqlite:///{tmp_path}/scheduled_tasks.db",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr("rubik_core.database.db.SessionLocal", Session)
    s = Session()
    app.dependency_overrides[get_db] = lambda: s
    try:
        yield s
    finally:
        app.dependency_overrides.clear()
        s.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def _feature_on(monkeypatch):
    monkeypatch.setattr(settings, "SCHEDULED_TASKS_ENABLED", True)
    monkeypatch.setattr(settings, "SCHEDULED_TASKS_MAX_PER_USER", 10)
    # The router and the copilot gate both resolve rosters; admit everyone by
    # default so each test asserts the ONE gate it is about.
    monkeypatch.setattr(
        "rubik_core.features.policy.feature_allows", lambda *a, **k: True
    )
    monkeypatch.setattr(
        "backend.routers.scheduled_tasks.feature_allows", lambda *a, **k: True
    )
    monkeypatch.setattr("backend.auth.feature_allows", lambda *a, **k: True)


def _make(db, *, user=OWNER, title="Morning emails"):
    spec = sched.validate_schedule(
        schedule_kind="weekdays",
        timezone_name="America/Chicago",
        time_local="08:00",
        now_utc=datetime(2026, 9, 8, 12, tzinfo=timezone.utc),
    )
    task = store.create_task(
        db,
        user_email=user,
        title=title,
        prompt="Summarize my unread email.",
        schedule=spec,
        next_run_at=datetime(2026, 9, 10, 13, 0),
        delivery_channels=["slack"],
    )
    db.commit()
    return task


# ── reading ─────────────────────────────────────────────────────────────────


def test_the_list_is_scoped_to_the_signed_in_user(db):
    mine = _make(db, title="Mine")
    _make(db, user=STRANGER, title="Theirs")
    _as(OWNER)

    body = client.get("/api/scheduled-tasks").json()

    assert [t["id"] for t in body["tasks"]] == [mine.id]
    assert body["max_per_user"] == 10


def test_the_list_renders_the_schedule_in_words(db):
    _make(db)
    _as(OWNER)
    task = client.get("/api/scheduled-tasks").json()["tasks"][0]
    assert task["schedule_text"] == "every weekday at 8:00 AM (America/Chicago)"
    assert task["next_run_text"] == "Thu Sep 10, 8:00 AM"


def test_runs_history_is_owner_scoped(db):
    task = _make(db)
    store.create_run(db, task, scheduled_for=None)
    db.commit()

    _as(OWNER)
    assert len(client.get(f"/api/scheduled-tasks/{task.id}/runs").json()) == 1
    _as(STRANGER)
    assert client.get(f"/api/scheduled-tasks/{task.id}/runs").status_code == 404


# ── gates ───────────────────────────────────────────────────────────────────


def test_the_env_switch_hides_the_surface_entirely(db, monkeypatch):
    monkeypatch.setattr(settings, "SCHEDULED_TASKS_ENABLED", False)
    _as(OWNER)
    # 404, not 403: in this environment there is no such surface.
    assert client.get("/api/scheduled-tasks").status_code == 404


def test_someone_off_the_roster_is_refused_in_plain_words(db, monkeypatch):
    monkeypatch.setattr(
        "backend.routers.scheduled_tasks.feature_allows", lambda *a, **k: False
    )
    _as(OWNER)
    response = client.get("/api/scheduled-tasks")
    assert response.status_code == 403
    assert "not turned on for you" in response.json()["detail"]


def test_another_users_task_reads_as_missing_not_forbidden(db):
    task = _make(db, user=STRANGER)
    _as(OWNER)
    # A 403 here would confirm the id exists.
    assert (
        client.patch(
            f"/api/scheduled-tasks/{task.id}", json={"status": "paused"}
        ).status_code
        == 404
    )
    assert client.delete(f"/api/scheduled-tasks/{task.id}").status_code == 404
    assert client.post(f"/api/scheduled-tasks/{task.id}/run-now").status_code == 404


# ── editing ─────────────────────────────────────────────────────────────────


def test_pausing_and_resuming_round_trips(db):
    task = _make(db)
    _as(OWNER)

    paused = client.patch(f"/api/scheduled-tasks/{task.id}", json={"status": "paused"})
    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"
    # A paused task advertises no next run.
    assert paused.json()["next_run_text"] == ""

    resumed = client.patch(f"/api/scheduled-tasks/{task.id}", json={"status": "active"})
    assert resumed.json()["status"] == "active"
    assert resumed.json()["next_run_at"] is not None


def test_a_new_time_recomputes_the_next_run(db):
    task = _make(db)
    _as(OWNER)

    body = client.patch(
        f"/api/scheduled-tasks/{task.id}", json={"time_local": "07:30"}
    ).json()

    assert body["time_local"] == "07:30"
    assert "7:30 AM" in body["schedule_text"]
    db.rollback()
    # Recomputed from now, so a task whose 08:00 already went out today does
    # not immediately fire again at the new time.
    assert store.get_task(db, task.id).next_run_at > datetime.utcnow()


def test_switching_to_weekly_needs_days_and_says_so(db):
    task = _make(db)
    _as(OWNER)
    response = client.patch(
        f"/api/scheduled-tasks/{task.id}", json={"schedule_kind": "weekly"}
    )
    assert response.status_code == 400
    assert "at least one" in response.json()["detail"]


def test_an_impossible_time_is_refused(db):
    task = _make(db)
    _as(OWNER)
    response = client.patch(
        f"/api/scheduled-tasks/{task.id}", json={"time_local": "half past eight"}
    )
    assert response.status_code == 400
    assert "not a time" in response.json()["detail"]


def test_delivery_channels_can_be_toggled(db):
    task = _make(db)
    _as(OWNER)
    body = client.patch(
        f"/api/scheduled-tasks/{task.id}",
        json={"deliver_by_slack": False, "deliver_by_email": True},
    ).json()
    assert body["delivery_channels"] == ["email"]


def test_an_empty_patch_is_a_bad_request(db):
    task = _make(db)
    _as(OWNER)
    assert client.patch(f"/api/scheduled-tasks/{task.id}", json={}).status_code == 400


def test_deleting_reports_what_went(db):
    task = _make(db)
    _as(OWNER)
    body = client.delete(f"/api/scheduled-tasks/{task.id}").json()
    assert body == {"status": "deleted", "title": "Morning emails"}
    assert store.get_task(db, task.id) is None


# ── run now ─────────────────────────────────────────────────────────────────


def test_run_now_queues_a_manual_run(db, monkeypatch):
    launched: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        "rubik_core.scheduled_tasks.dispatch._default_launcher",
        lambda module, args: launched.append((module, args)) or "exec-1",
    )
    task = _make(db)
    _as(OWNER)

    body = client.post(f"/api/scheduled-tasks/{task.id}/run-now").json()

    assert body["trigger"] == "manual"
    assert body["status"] == "queued"
    assert launched and launched[0][1][0] == "--run-id"


def test_run_now_is_refused_while_one_is_in_flight(db, monkeypatch):
    monkeypatch.setattr(
        "rubik_core.scheduled_tasks.dispatch._default_launcher",
        lambda module, args: "exec-1",
    )
    task = _make(db)
    _as(OWNER)
    client.post(f"/api/scheduled-tasks/{task.id}/run-now")
    second = client.post(f"/api/scheduled-tasks/{task.id}/run-now")
    assert second.status_code == 409
    assert "already running" in second.json()["detail"]


# ── the scheduler tick ──────────────────────────────────────────────────────


def test_the_tick_needs_the_scheduler_key(db):
    app.dependency_overrides.pop(get_current_user, None)
    response = client.post("/api/agents/scheduled-tasks/tick")
    assert response.status_code == 403


def test_the_tick_no_ops_when_the_feature_is_off(db, monkeypatch):
    """A 404 on a scheduler target reads as a broken job in Cloud Scheduler's
    own alerting, and this flag defaults false."""
    monkeypatch.setattr(settings, "SCHEDULED_TASKS_ENABLED", False)
    monkeypatch.setattr(settings, "SCHEDULER_API_KEY", "s3cret")
    response = client.post(
        "/api/agents/scheduled-tasks/tick",
        headers={"X-Scheduler-Key": "s3cret", "X-Cubie-Workspace": "test"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "disabled"
    assert body["flag"] == "SCHEDULED_TASKS_ENABLED"


def test_a_dry_run_reports_what_is_due_and_claims_nothing(db, monkeypatch):
    monkeypatch.setattr(settings, "SCHEDULER_API_KEY", "s3cret")
    spec = sched.validate_schedule(
        schedule_kind="daily",
        timezone_name="UTC",
        time_local="08:00",
        now_utc=datetime(2026, 9, 8, 12, tzinfo=timezone.utc),
    )
    task = store.create_task(
        db,
        user_email=OWNER,
        title="Due now",
        prompt="p",
        schedule=spec,
        next_run_at=datetime(2020, 1, 1, 0, 0),
        delivery_channels=[],
    )
    db.commit()

    body = client.post(
        "/api/agents/scheduled-tasks/tick?dry_run=true",
        headers={"X-Scheduler-Key": "s3cret", "X-Cubie-Workspace": "test"},
    ).json()

    assert body["status"] == "dry_run"
    assert body["due"] == 1
    assert body["tasks"][0]["title"] == "Due now"
    db.rollback()
    # Untouched: a dry run spends nothing and claims nothing.
    assert store.get_task(db, task.id).next_run_at == datetime(2020, 1, 1, 0, 0)

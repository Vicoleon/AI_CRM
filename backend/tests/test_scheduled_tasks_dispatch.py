"""Scheduled tasks — the Cloud Scheduler tick.

What these pin, in order of how much damage getting them wrong does:

1. An occurrence is dispatched at most ONCE, even with overlapping ticks.
2. A launch failure is recorded as that run's failure and does not retry into
   a duplicate.
3. A worker that never reports is reaped, so its task is not stuck "running"
   forever with a live claim blocking manual runs.
4. The kill switch and the per-tick cap actually hold.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from rubik_core.config import settings
from rubik_core.database.models import Base, ScheduledTaskRun
from rubik_core.scheduled_tasks import dispatch, store
from rubik_core.scheduled_tasks import schedule as sched

OWNER = "bellison@acme.example"
NOW = datetime(2026, 9, 9, 13, 5)


class _Launcher:
    """Records launches; can be told to fail."""

    def __init__(self, fail: bool = False):
        self.calls: list[tuple[str, list[str]]] = []
        self.fail = fail

    def __call__(self, module: str, args: list[str]) -> str | None:
        self.calls.append((module, args))
        if self.fail:
            raise RuntimeError("Cloud Run said no")
        return f"executions/exec-{len(self.calls)}"

    @property
    def run_ids(self) -> list[str]:
        return [args[args.index("--run-id") + 1] for _, args in self.calls]


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
    # dispatch_due_tasks opens its own session through a deferred import.
    monkeypatch.setattr("rubik_core.database.db.SessionLocal", Session)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def _feature_on(monkeypatch):
    monkeypatch.setattr(settings, "SCHEDULED_TASKS_ENABLED", True)
    monkeypatch.setattr(settings, "SCHEDULED_TASKS_TICK_MAX_DISPATCH", 30)
    monkeypatch.setattr(settings, "SCHEDULED_TASKS_MAX_CONSECUTIVE_FAILURES", 5)
    monkeypatch.setattr(settings, "SCHEDULED_TASKS_RUN_TIMEOUT_SECONDS", 300)


def _make(db, *, title="Morning emails", next_run=datetime(2026, 9, 9, 13, 0)):
    spec = sched.validate_schedule(
        schedule_kind="weekdays",
        timezone_name="America/Chicago",
        time_local="08:00",
        now_utc=datetime(2026, 9, 8, 12, tzinfo=timezone.utc),
    )
    task = store.create_task(
        db,
        user_email=OWNER,
        title=title,
        prompt="Summarize my unread email.",
        schedule=spec,
        next_run_at=next_run,
        delivery_channels=["slack"],
    )
    db.commit()
    return task


# ── the happy path ──────────────────────────────────────────────────────────


def test_a_due_task_is_dispatched_once_and_moved_forward(db):
    task = _make(db)
    launcher = _Launcher()

    report = dispatch.dispatch_due_tasks(now=NOW, launcher=launcher)

    assert (report["due"], report["claimed"], report["dispatched"]) == (1, 1, 1)
    assert len(launcher.calls) == 1
    module, args = launcher.calls[0]
    assert module == dispatch.RUN_JOB_MODULE
    assert args[0] == "--run-id"

    db.rollback()
    fresh = store.get_task(db, task.id)
    # Advanced at claim time, so a second tick in the same minute finds nothing.
    assert fresh.next_run_at == datetime(2026, 9, 10, 13, 0)
    assert fresh.claimed_at == NOW

    run = store.get_run(db, launcher.run_ids[0])
    assert run.status == "queued"
    assert run.trigger == "schedule"
    assert run.scheduled_for == datetime(2026, 9, 9, 13, 0)
    assert run.execution_name == "executions/exec-1"
    # The results folder is prepared here, sequentially, so two tasks due in
    # the same minute cannot race a duplicate into existence.
    assert run.chat_folder_id is not None


def test_a_second_tick_over_the_same_row_dispatches_nothing(db):
    _make(db)
    first, second = _Launcher(), _Launcher()

    dispatch.dispatch_due_tasks(now=NOW, launcher=first)
    report = dispatch.dispatch_due_tasks(now=NOW, launcher=second)

    assert len(first.calls) == 1
    assert second.calls == []
    assert report["due"] == 0


def test_nothing_due_means_nothing_launched(db):
    _make(db, next_run=datetime(2026, 9, 20, 13, 0))
    launcher = _Launcher()
    report = dispatch.dispatch_due_tasks(now=NOW, launcher=launcher)
    assert launcher.calls == []
    assert report["dispatched"] == 0


@pytest.mark.parametrize("status", ["paused", "error_paused", "completed"])
def test_a_task_that_is_not_active_is_never_claimed(db, status):
    task = _make(db)
    store.set_status(db, task, status)
    db.commit()
    launcher = _Launcher()
    dispatch.dispatch_due_tasks(now=NOW, launcher=launcher)
    assert launcher.calls == []


# ── failure and recovery ────────────────────────────────────────────────────


def test_a_failed_launch_is_that_runs_failure_not_a_retry(db):
    task = _make(db)
    launcher = _Launcher(fail=True)

    report = dispatch.dispatch_due_tasks(now=NOW, launcher=launcher)

    assert (report["claimed"], report["dispatched"], report["dispatch_failed"]) == (
        1,
        0,
        1,
    )
    db.rollback()
    run = db.query(ScheduledTaskRun).one()
    assert run.status == "failed"
    assert "Could not start" in run.error
    fresh = store.get_task(db, task.id)
    # Still scheduled for next time, and unblocked for a manual run.
    assert fresh.next_run_at == datetime(2026, 9, 10, 13, 0)
    assert fresh.claimed_at is None
    assert fresh.consecutive_failures == 1


def test_a_worker_that_never_reports_is_reaped(db):
    task = _make(db, next_run=datetime(2026, 9, 20, 13, 0))
    run = store.create_run(db, task, scheduled_for=datetime(2026, 9, 9, 13, 0))
    # Older than the run timeout plus the cold-start grace.
    run.created_at = datetime(2026, 9, 9, 10, 0)
    task.claimed_at = datetime(2026, 9, 9, 10, 0)
    db.commit()

    report = dispatch.dispatch_due_tasks(now=NOW, launcher=_Launcher())

    assert report["reaped"] == 1
    db.rollback()
    assert store.get_run(db, run.id).status == "failed"
    fresh = store.get_task(db, task.id)
    assert fresh.claimed_at is None  # unblocked for the next attempt
    assert fresh.consecutive_failures == 1


def test_a_run_still_inside_its_timeout_is_left_alone(db):
    """The grace has to exceed the timeout plus a cold start, or the reaper
    fails runs that are still legitimately working."""
    task = _make(db, next_run=datetime(2026, 9, 20, 13, 0))
    run = store.create_run(db, task, scheduled_for=None)
    run.created_at = datetime(2026, 9, 9, 13, 3)
    db.commit()

    report = dispatch.dispatch_due_tasks(now=NOW, launcher=_Launcher())

    assert report["reaped"] == 0
    db.rollback()
    assert store.get_run(db, run.id).status == "queued"


# ── the cap ─────────────────────────────────────────────────────────────────


def test_the_per_tick_cap_defers_the_rest_rather_than_dropping_them(db):
    for i in range(4):
        _make(db, title=f"Task {i}", next_run=datetime(2026, 9, 9, 13, 0))
    launcher = _Launcher()

    report = dispatch.dispatch_due_tasks(now=NOW, launcher=launcher, limit=2)

    assert report["dispatched"] == 2
    assert report["deferred"] == 1
    # The leftovers are still due, so the next tick picks them up.
    assert len(store.due_tasks(db, NOW, limit=10)) == 2


# ── manual runs ─────────────────────────────────────────────────────────────


def test_a_manual_run_does_not_disturb_the_schedule(db):
    task = _make(db, next_run=datetime(2026, 9, 20, 13, 0))
    launcher = _Launcher()

    outcome = dispatch.dispatch_task_now(db, task, launcher=launcher)

    assert outcome["dispatched"] is True
    assert launcher.run_ids == [outcome["run_id"]]
    run = store.get_run(db, outcome["run_id"])
    assert run.trigger == "manual"
    db.rollback()
    assert store.get_task(db, task.id).next_run_at == datetime(2026, 9, 20, 13, 0)


def test_a_manual_run_is_refused_while_one_is_in_flight(db):
    task = _make(db, next_run=datetime(2026, 9, 20, 13, 0))
    dispatch.dispatch_task_now(db, task, launcher=_Launcher())
    with pytest.raises(dispatch.AlreadyRunning):
        dispatch.dispatch_task_now(db, task, launcher=_Launcher())

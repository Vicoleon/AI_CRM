"""Scheduled tasks — persistence, and the two claims that make it safe.

The claim tests are the point of this file. Both are compare-and-swap UPDATEs
whose whole purpose is that a SECOND caller loses, so each one exercises the
losing path as well as the winning one — a pre-check plus a write would pass
the happy-path assertions and still double-deliver in production.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from rubik_core.database.models import Base, ChatFolder
from rubik_core.scheduled_tasks import schedule as sched
from rubik_core.scheduled_tasks import store

OWNER = "bellison@acme.example"
OTHER = "someone@acme.example"


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
    # Modules under test open their own sessions via deferred imports of
    # SessionLocal — patch by string target so those late imports see this engine.
    monkeypatch.setattr("rubik_core.database.db.SessionLocal", Session)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _spec(kind="weekdays", time_local="08:00", **kw):
    return sched.validate_schedule(
        schedule_kind=kind,
        timezone_name="America/Chicago",
        time_local=time_local,
        now_utc=datetime(2026, 9, 8, 12, tzinfo=timezone.utc),
        **kw,
    )


# Sentinel so `next_run=None` means "this never fires again", which is a real
# state (a completed one-off), rather than silently taking the default.
_DEFAULT_NEXT_RUN = object()


def _make(db, *, user=OWNER, title="Morning emails", next_run=_DEFAULT_NEXT_RUN, **kw):
    task = store.create_task(
        db,
        user_email=user,
        title=title,
        prompt="Summarize my unread email from the last 24 hours.",
        schedule=_spec(),
        next_run_at=(
            datetime(2026, 9, 9, 13, 0) if next_run is _DEFAULT_NEXT_RUN else next_run
        ),
        delivery_channels=["slack"],
        **kw,
    )
    db.commit()
    return task


# ── ownership ───────────────────────────────────────────────────────────────


def test_a_task_is_only_reachable_by_its_owner(db):
    task = _make(db)
    assert store.get_task_for_user(db, task.id, OWNER) is not None
    # Somebody else's id must read as absent, so it cannot be probed.
    assert store.get_task_for_user(db, task.id, OTHER) is None
    assert store.get_task_for_user(db, "no-such-id", OWNER) is None


def test_the_owner_email_is_stored_verbatim(db):
    """Chat's own lookups are case-exact, so normalising here would hide a
    user's conversations from them."""
    mixed = "BEllison@ACME.example"
    task = _make(db, user=mixed)
    assert task.user_email == mixed


def test_the_per_user_cap_is_enforced(db):
    for i in range(3):
        _make(db, title=f"Task {i}")
    with pytest.raises(ValueError) as exc:
        _make(db, title="One too many", max_per_user=3)
    assert "limit" in str(exc.value)


def test_a_task_needs_an_owner_a_title_and_something_to_do(db):
    for kwargs in ({"user_email": ""}, {"title": " "}, {"prompt": ""}):
        with pytest.raises(ValueError):
            store.create_task(
                db,
                user_email=kwargs.get("user_email", OWNER),
                title=kwargs.get("title", "T"),
                prompt=kwargs.get("prompt", "P"),
                schedule=_spec(),
                next_run_at=datetime(2026, 9, 9, 13, 0),
                delivery_channels=[],
            )


# ── the task claim ──────────────────────────────────────────────────────────


def test_only_one_of_two_racing_ticks_claims_an_occurrence(db):
    """Both ticks read the same due row; exactly one may move it forward."""
    due = datetime(2026, 9, 9, 13, 0)
    task = _make(db, next_run=due)
    upcoming = datetime(2026, 9, 10, 13, 0)

    first = store.claim_task_for_run(
        db, task, seen_next_run_at=due, new_next_run_at=upcoming
    )
    second = store.claim_task_for_run(
        db, task, seen_next_run_at=due, new_next_run_at=upcoming
    )
    assert (first, second) == (True, False)

    db.rollback()
    assert store.get_task(db, task.id).next_run_at == upcoming


def test_a_paused_task_is_never_claimed(db):
    due = datetime(2026, 9, 9, 13, 0)
    task = _make(db, next_run=due)
    store.set_status(db, task, "paused")
    db.commit()
    assert not store.claim_task_for_run(
        db, task, seen_next_run_at=due, new_next_run_at=datetime(2026, 9, 10, 13, 0)
    )


def test_due_tasks_returns_the_longest_overdue_first(db):
    """When more is due than one tick dispatches, the oldest must go first."""
    late = _make(db, title="Late", next_run=datetime(2026, 9, 9, 6, 0))
    recent = _make(db, title="Recent", next_run=datetime(2026, 9, 9, 12, 0))
    _make(db, title="Future", next_run=datetime(2026, 9, 20, 13, 0))
    _make(db, title="Never", next_run=None)

    due = store.due_tasks(db, datetime(2026, 9, 9, 13, 0), limit=10)
    assert [t.id for t in due] == [late.id, recent.id]


# ── the run claim ───────────────────────────────────────────────────────────


def test_a_retried_worker_loses_the_run_claim(db):
    """Cloud Run re-runs a crashed job task. The second invocation must lose,
    or the user gets the same digest twice."""
    task = _make(db)
    run = store.create_run(db, task, scheduled_for=task.next_run_at)
    db.commit()

    assert store.claim_run_running(db, run.id) is True
    assert store.claim_run_running(db, run.id) is False

    db.rollback()
    assert store.get_run(db, run.id).status == "running"


def test_an_unfinished_run_blocks_a_second_manual_run(db):
    task = _make(db)
    assert store.has_unfinished_run(db, task.id) is False
    run = store.create_run(db, task, scheduled_for=None, trigger="manual")
    db.commit()
    assert store.has_unfinished_run(db, task.id) is True
    store.finish_run(db, run.id, status="succeeded")
    assert store.has_unfinished_run(db, task.id) is False


def test_stale_runs_are_the_ones_past_the_cutoff(db):
    task = _make(db)
    old = store.create_run(db, task, scheduled_for=None)
    old.created_at = datetime(2026, 9, 9, 1, 0)
    fresh = store.create_run(db, task, scheduled_for=None)
    fresh.created_at = datetime(2026, 9, 9, 12, 59)
    db.commit()

    stale = store.stale_runs(db, cutoff=datetime(2026, 9, 9, 12, 0))
    assert [r.id for r in stale] == [old.id]


# ── outcomes ────────────────────────────────────────────────────────────────


def test_a_failure_streak_auto_pauses_the_task_and_says_why(db):
    task = _make(db)
    for _ in range(2):
        run = store.create_run(db, task, scheduled_for=None)
        db.commit()
        assert (
            store.record_task_outcome(
                db,
                task.id,
                run_id=run.id,
                status="failed",
                max_consecutive_failures=3,
            )
            is None
        )

    run = store.create_run(db, task, scheduled_for=None)
    db.commit()
    paused = store.record_task_outcome(
        db, task.id, run_id=run.id, status="failed", max_consecutive_failures=3
    )
    assert paused is not None
    db.rollback()
    fresh = store.get_task(db, task.id)
    assert fresh.status == "error_paused"
    assert "3 failed runs" in fresh.paused_reason
    # Nothing more fires until a human resumes it.
    assert fresh.next_run_at is None


def test_a_skip_counts_toward_the_streak(db):
    """A task nobody can run is not healthy, even though nothing broke."""
    task = _make(db)
    run = store.create_run(db, task, scheduled_for=None)
    db.commit()
    store.record_task_outcome(
        db, task.id, run_id=run.id, status="skipped", max_consecutive_failures=5
    )
    db.rollback()
    assert store.get_task(db, task.id).consecutive_failures == 1


def test_success_resets_the_streak_and_counts_the_run(db):
    task = _make(db)
    task.consecutive_failures = 2
    db.commit()
    run = store.create_run(db, task, scheduled_for=None)
    db.commit()
    store.record_task_outcome(
        db, task.id, run_id=run.id, status="succeeded", max_consecutive_failures=5
    )
    db.rollback()
    fresh = store.get_task(db, task.id)
    assert (fresh.consecutive_failures, fresh.run_count) == (0, 1)
    assert fresh.claimed_at is None


def test_a_one_off_completes_after_its_single_run(db):
    task = _make(db)
    run = store.create_run(db, task, scheduled_for=None)
    db.commit()
    store.record_task_outcome(
        db,
        task.id,
        run_id=run.id,
        status="succeeded",
        max_consecutive_failures=5,
        completed=True,
    )
    db.rollback()
    fresh = store.get_task(db, task.id)
    assert fresh.status == "completed"
    assert fresh.next_run_at is None


def test_resuming_clears_the_failure_streak(db):
    """The user has looked at it, so the next failure starts a fresh streak."""
    task = _make(db)
    task.consecutive_failures = 4
    store.set_status(db, task, "error_paused", reason="too many failures")
    db.commit()
    store.set_status(db, task, "active", next_run_at=datetime(2026, 9, 10, 13, 0))
    db.commit()
    assert task.consecutive_failures == 0
    assert task.paused_reason is None


# ── the results folder ──────────────────────────────────────────────────────


def test_the_results_folder_is_created_once_and_reused(db):
    first = store.ensure_results_folder(db, OWNER)
    second = store.ensure_results_folder(db, OWNER)
    assert first == second
    folders = db.query(ChatFolder).filter(ChatFolder.user_email == OWNER).all()
    assert len(folders) == 1
    assert folders[0].name == store.RESULTS_FOLDER_NAME


def test_each_user_gets_their_own_results_folder(db):
    assert store.ensure_results_folder(db, OWNER) != store.ensure_results_folder(
        db, OTHER
    )


def test_deleting_a_task_takes_its_runs_and_leaves_the_conversations(db):
    from rubik_core.database.models import ChatSession, ScheduledTaskRun

    task = _make(db)
    run = store.create_run(db, task, scheduled_for=None)
    db.add(ChatSession(id="sess-1", user_email=OWNER, title="Morning emails — Sep 9"))
    db.commit()
    run_id = run.id

    store.delete_task(db, task)
    db.commit()

    assert db.get(ScheduledTaskRun, run_id) is None
    # The user's copy of a result they already read is theirs, not the schedule's.
    assert db.get(ChatSession, "sess-1") is not None


def test_a_schedule_change_repoints_the_next_run(db):
    task = _make(db)
    weekly = _spec(kind="weekly", time_local="17:30", days_of_week=[0])
    store.apply_schedule(
        db, task, schedule=weekly, next_run_at=datetime(2026, 9, 14, 22, 30)
    )
    db.commit()
    db.rollback()
    fresh = store.get_task(db, task.id)
    assert fresh.schedule_kind == "weekly"
    assert fresh.days_of_week == [0]
    assert fresh.time_local == "17:30"
    assert fresh.next_run_at == datetime(2026, 9, 14, 22, 30)


def test_latest_runs_by_task_returns_the_newest_per_task(db):
    task = _make(db)
    older = store.create_run(db, task, scheduled_for=None)
    older.created_at = datetime(2026, 9, 8, 13, 0)
    newer = store.create_run(db, task, scheduled_for=None)
    newer.created_at = datetime(2026, 9, 9, 13, 0)
    db.commit()
    assert store.latest_runs_by_task(db, [task.id])[task.id].id == newer.id
    assert store.latest_runs_by_task(db, []) == {}


def test_release_claim_frees_a_task_for_the_next_attempt(db):
    task = _make(db)
    task.claimed_at = datetime(2026, 9, 9, 13, 0)
    db.commit()
    store.release_claim(db, task.id)
    db.rollback()
    assert store.get_task(db, task.id).claimed_at is None


def test_an_unknown_status_is_refused_rather_than_stored(db):
    task = _make(db)
    with pytest.raises(ValueError):
        store.set_status(db, task, "sleeping")
    with pytest.raises(ValueError):
        run = store.create_run(db, task, scheduled_for=None)
        db.commit()
        store.finish_run(db, run.id, status="finished-ish")


def test_a_timedelta_free_sanity_check_on_the_reaper_cutoff(db):
    """The cutoff must be a real instant, not a naive arithmetic slip."""
    now = datetime(2026, 9, 9, 13, 0)
    assert now - timedelta(seconds=1200) == datetime(2026, 9, 9, 12, 40)

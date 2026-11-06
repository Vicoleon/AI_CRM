"""DB-backed on-demand priorities run state (UC-41 → #9).

Exercises rubik_core/priorities/run_state.py against a throwaway SQLite file —
the same rows Cloud SQL holds in dev/prod — so the trigger/executor/poll
contract is tested for real, not through mocks.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from rubik_core.config import settings
from rubik_core.priorities import run_state


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    settings.DATABASE_URL = f"sqlite:///{tmp_path / 'test_run_state.db'}"
    import rubik_core.database.db as dbmod

    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    yield


def test_create_get_roundtrip():
    run = run_state.create_run("j@acme.example", trigger=run_state.TRIGGER_MORE)
    got = run_state.get_run(run["run_id"])
    assert got is not None
    assert got["user_email"] == "j@acme.example"
    assert got["status"] == run_state.RUN_PENDING
    assert got["trigger"] == run_state.TRIGGER_MORE
    assert got["queued_at"] is not None
    assert run_state.get_run("nope") is None


def test_active_run_dedupes_pending_only():
    email = "j@acme.example"
    assert run_state.active_run(email) is None
    run = run_state.create_run(email)
    active = run_state.active_run(email)
    assert active is not None and active["run_id"] == run["run_id"]

    # Finished runs never dedupe.
    run_state.mark_finished(run["run_id"], succeeded=True)
    assert run_state.active_run(email) is None


def test_active_run_ignores_stale_pending():
    """A zombie pending row older than the timeout must not block new runs."""
    email = "stale@acme.example"
    run = run_state.create_run(email)

    from rubik_core.database.db import SessionLocal
    from rubik_core.database.models import PrioritiesRun

    with SessionLocal() as db:
        db.query(PrioritiesRun).filter(PrioritiesRun.run_id == run["run_id"]).update(
            {
                "queued_at": datetime.utcnow()
                - timedelta(seconds=run_state.RUN_TIMEOUT_SECONDS + 60)
            }
        )
        db.commit()

    assert run_state.active_run(email) is None


def test_mark_finished_and_executor():
    run = run_state.create_run("j@acme.example")
    run_state.mark_executor(run["run_id"], run_state.EXECUTOR_JOB, "exec-123")
    run_state.mark_finished(run["run_id"], succeeded=False, error="boom")
    got = run_state.get_run(run["run_id"])
    assert got["status"] == run_state.RUN_FAILED
    assert got["error"] == "boom"
    assert got["executor"] == run_state.EXECUTOR_JOB
    assert got["execution_name"] == "exec-123"
    assert got["finished_at"] is not None

    # mark_* are best-effort: unknown run ids must not raise.
    run_state.mark_finished("nope", succeeded=True)
    run_state.mark_executor("nope", run_state.EXECUTOR_INLINE)


def test_fail_timed_out_only_flips_pending():
    run = run_state.create_run("j@acme.example")
    run_state.mark_finished(run["run_id"], succeeded=True)
    run_state.fail_timed_out(run["run_id"])
    assert run_state.get_run(run["run_id"])["status"] == run_state.RUN_READY

    run2 = run_state.create_run("j@acme.example")
    run_state.fail_timed_out(run2["run_id"])
    got = run_state.get_run(run2["run_id"])
    assert got["status"] == run_state.RUN_FAILED
    assert "timed out" in (got["error"] or "")


def test_fresh_card_since_belt_and_braces():
    """The poll's fallback signal: a daily-briefing card created after the run
    was queued means the run's work landed even if its state write was lost."""
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.models import HomeCard
    from rubik_core.priorities import DAILY_BRIEFING_CATEGORY
    from rubik_core.priorities.temporal import user_today_str

    email = "fresh@acme.example"
    queued = datetime.utcnow() - timedelta(minutes=1)
    assert run_state.fresh_card_since(email, queued) is False

    with SessionLocal() as db:
        db.add(
            HomeCard(
                id="card-1",
                user_email=email,
                date=user_today_str(email),
                category=DAILY_BRIEFING_CATEGORY,
                content_md="# fresh",
                metadata_json={"priorities": []},
                created_at=datetime.utcnow(),
            )
        )
        db.commit()

    assert run_state.fresh_card_since(email, queued) is True
    # A card older than the run does not count.
    assert (
        run_state.fresh_card_since(email, datetime.utcnow() + timedelta(minutes=5))
        is False
    )

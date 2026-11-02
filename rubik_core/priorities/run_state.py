"""DB-backed state for on-demand AI-Priorities runs (UC-41 → #9).

Run state used to live in per-instance dicts in ``backend/routers/home.py``.
Two failure modes: a poll that landed on a different Cloud Run instance never
saw the run, and the CPU-throttled instance could be recycled mid-run, losing
both the run and its state. ``priorities_runs`` rows replace those dicts —
the trigger endpoint inserts ``pending``, the executor (the single-task
``cubie-priorities-batch`` Cloud Run Job execution via
``backend.jobs.priorities_on_demand_job``, or the local-dev BackgroundTask
fallback) flips it to ``ready``/``failed``, and the poll endpoint reads only
the DB, so any instance can serve any poll.

Every helper opens its own short-lived session (the callers are background
executors and pollers whose request-scoped sessions, if any, are long gone)
and returns plain-dict snapshots so no ORM object outlives its session.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from rubik_core.database.db import SessionLocal
from rubik_core.database.models import HomeCard, PrioritiesRun

logger = logging.getLogger(__name__)

RUN_PENDING = "pending"
RUN_READY = "ready"
RUN_FAILED = "failed"

TRIGGER_ON_DEMAND = "on_demand"
TRIGGER_MORE = "more"

EXECUTOR_JOB = "job"
EXECUTOR_INLINE = "inline"

# Hard ceiling before a poll declares a pending run dead: Job cold start
# (~30s) + the 3–5 min pipeline + retry headroom. Also bounds the dedupe
# window — a pending row older than this never blocks a new run.
RUN_TIMEOUT_SECONDS = 900


def _snapshot(row: PrioritiesRun) -> dict:
    return {
        "run_id": row.run_id,
        "user_email": row.user_email,
        "status": row.status,
        "trigger": row.trigger,
        "executor": row.executor,
        "execution_name": row.execution_name,
        "error": row.error,
        "queued_at": row.queued_at,
        "finished_at": row.finished_at,
    }


def create_run(user_email: str, *, trigger: str = TRIGGER_ON_DEMAND) -> dict:
    """Insert a pending run row and return its snapshot."""
    row = PrioritiesRun(
        run_id=str(uuid.uuid4()),
        user_email=user_email,
        status=RUN_PENDING,
        trigger=trigger,
        executor=EXECUTOR_INLINE,
        queued_at=datetime.utcnow(),
    )
    with SessionLocal() as db:
        db.add(row)
        db.commit()
        return _snapshot(row)


def get_run(run_id: str) -> dict | None:
    with SessionLocal() as db:
        row = db.query(PrioritiesRun).filter(PrioritiesRun.run_id == run_id).first()
        return _snapshot(row) if row else None


def active_run(user_email: str) -> dict | None:
    """The user's newest still-credible pending run, if any.

    Dedupes rapid re-POSTs across instances (the old per-instance dict only
    deduped within one instance). Rows older than ``RUN_TIMEOUT_SECONDS`` are
    ignored so a zombie run can never block new requests forever.
    """
    cutoff = datetime.utcnow() - timedelta(seconds=RUN_TIMEOUT_SECONDS)
    with SessionLocal() as db:
        row = (
            db.query(PrioritiesRun)
            .filter(PrioritiesRun.user_email == user_email)
            .filter(PrioritiesRun.status == RUN_PENDING)
            .filter(PrioritiesRun.queued_at > cutoff)
            .order_by(PrioritiesRun.queued_at.desc())
            .first()
        )
        return _snapshot(row) if row else None


def mark_executor(
    run_id: str, executor: str, execution_name: str | None = None
) -> None:
    """Record which executor picked the run up. Never raises (best-effort)."""
    try:
        with SessionLocal() as db:
            values: dict = {"executor": executor}
            if execution_name:
                values["execution_name"] = execution_name
            db.query(PrioritiesRun).filter(PrioritiesRun.run_id == run_id).update(
                values
            )
            db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("[priorities-run] mark_executor failed for %s", run_id)


def mark_finished(run_id: str, *, succeeded: bool, error: str | None = None) -> None:
    """Flip a run to its terminal state. Never raises — the executors call
    this as their last act and a state-write failure must not fail the run
    (the poll endpoint's fresh-card check covers exactly this gap)."""
    try:
        with SessionLocal() as db:
            db.query(PrioritiesRun).filter(PrioritiesRun.run_id == run_id).update(
                {
                    "status": RUN_READY if succeeded else RUN_FAILED,
                    "error": None if succeeded else (error or "priorities run failed"),
                    "finished_at": datetime.utcnow(),
                }
            )
            db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("[priorities-run] mark_finished failed for %s", run_id)


def fail_timed_out(run_id: str) -> None:
    """Poll-side lazy timeout: mark a still-pending run failed. Never raises."""
    try:
        with SessionLocal() as db:
            db.query(PrioritiesRun).filter(
                PrioritiesRun.run_id == run_id,
                PrioritiesRun.status == RUN_PENDING,
            ).update(
                {
                    "status": RUN_FAILED,
                    "error": f"run timed out after {RUN_TIMEOUT_SECONDS}s",
                    "finished_at": datetime.utcnow(),
                }
            )
            db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("[priorities-run] fail_timed_out failed for %s", run_id)


def fresh_card_since(user_email: str, since: datetime) -> bool:
    """True when a daily-briefing HomeCard for the user's LOCAL today was
    created at/after ``since``.

    Belt-and-braces for the poll: the Job persists the card and *then* marks
    the run ready — if the state write fails (or an old image without the
    state write runs), the card's existence is still authoritative. Never
    raises; an errored check just reports "not yet".
    """
    try:
        from rubik_core.priorities import DAILY_BRIEFING_CATEGORY
        from rubik_core.priorities.temporal import user_today_str

        today_str = user_today_str(user_email)
        with SessionLocal() as db:
            card = (
                db.query(HomeCard)
                .filter(HomeCard.user_email == user_email)
                .filter(HomeCard.date == today_str)
                .filter(HomeCard.category == DAILY_BRIEFING_CATEGORY)
                .filter(HomeCard.created_at >= since)
                .first()
            )
            return card is not None
    except Exception:  # noqa: BLE001
        logger.exception("[priorities-run] fresh_card_since failed for %s", user_email)
        return False

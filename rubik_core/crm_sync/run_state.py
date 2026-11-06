"""DB-backed state for CRM Sync scan runs.

Same contract and reasoning as ``rubik_core/priorities/run_state.py``: a poll
can land on any Cloud Run instance, so run state lives in the database rather
than in a per-instance dict, and every helper opens its own short-lived session
because the callers are background executors and pollers whose request-scoped
sessions are long gone.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from rubik_core.database.db import SessionLocal
from rubik_core.database.models import CrmSyncRun

logger = logging.getLogger(__name__)

RUN_PENDING = "pending"
RUN_READY = "ready"
RUN_FAILED = "failed"

TRIGGER_ON_DEMAND = "on_demand"
TRIGGER_NIGHTLY = "nightly"

EXECUTOR_JOB = "job"
EXECUTOR_INLINE = "inline"

# Job cold start + a 25-transcript scan (each one HubSpot lookups plus an LLM
# call) + headroom. Also bounds the dedupe window, so a zombie run can never
# block a user's next scan for more than this.
RUN_TIMEOUT_SECONDS = 900


def _snapshot(row: CrmSyncRun) -> dict:
    return {
        "run_id": row.run_id,
        "user_email": row.user_email,
        "status": row.status,
        "trigger": row.trigger,
        "executor": row.executor,
        "execution_name": row.execution_name,
        "error": row.error,
        "window_start": row.window_start,
        "stats": row.stats or {},
        "queued_at": row.queued_at,
        "finished_at": row.finished_at,
    }


def create_run(
    user_email: str,
    *,
    trigger: str = TRIGGER_ON_DEMAND,
    window_start: datetime | None = None,
) -> dict:
    row = CrmSyncRun(
        run_id=uuid.uuid4().hex,
        user_email=(user_email or "").lower(),
        status=RUN_PENDING,
        trigger=trigger,
        executor=EXECUTOR_INLINE,
        window_start=window_start,
        queued_at=datetime.utcnow(),
    )
    with SessionLocal() as db:
        db.add(row)
        db.commit()
        return _snapshot(row)


def get_run(run_id: str) -> dict | None:
    with SessionLocal() as db:
        row = db.query(CrmSyncRun).filter(CrmSyncRun.run_id == run_id).first()
        return _snapshot(row) if row else None


def active_run(user_email: str) -> dict | None:
    """The user's newest still-credible pending run, if any."""
    cutoff = datetime.utcnow() - timedelta(seconds=RUN_TIMEOUT_SECONDS)
    with SessionLocal() as db:
        row = (
            db.query(CrmSyncRun)
            .filter(CrmSyncRun.user_email == (user_email or "").lower())
            .filter(CrmSyncRun.status == RUN_PENDING)
            .filter(CrmSyncRun.queued_at > cutoff)
            .order_by(CrmSyncRun.queued_at.desc())
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
            db.query(CrmSyncRun).filter(CrmSyncRun.run_id == run_id).update(values)
            db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("[crm-sync-run] mark_executor failed for %s", run_id)


def mark_finished(
    run_id: str,
    *,
    succeeded: bool,
    error: str | None = None,
    stats: dict | None = None,
) -> None:
    """Flip a run to its terminal state. Never raises — this is the executor's
    last act and a state-write failure must not turn a completed scan into a
    crash."""
    try:
        with SessionLocal() as db:
            db.query(CrmSyncRun).filter(CrmSyncRun.run_id == run_id).update(
                {
                    "status": RUN_READY if succeeded else RUN_FAILED,
                    "error": None if succeeded else (error or "CRM Sync scan failed"),
                    "stats": stats or {},
                    "finished_at": datetime.utcnow(),
                }
            )
            db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("[crm-sync-run] mark_finished failed for %s", run_id)


def fail_timed_out(run_id: str) -> None:
    """Poll-side lazy timeout: mark a still-pending run failed. Never raises."""
    try:
        with SessionLocal() as db:
            db.query(CrmSyncRun).filter(
                CrmSyncRun.run_id == run_id,
                CrmSyncRun.status == RUN_PENDING,
            ).update(
                {
                    "status": RUN_FAILED,
                    "error": f"scan timed out after {RUN_TIMEOUT_SECONDS}s",
                    "finished_at": datetime.utcnow(),
                }
            )
            db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("[crm-sync-run] fail_timed_out failed for %s", run_id)

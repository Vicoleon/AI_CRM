"""Claim due scheduled tasks and hand each to a worker.

Called every few minutes by a Cloud Scheduler tick (the endpoint lives in
``backend/routers/agents.py``). This module is the ONLY place that decides a
task is due, and it is deliberately small: select, claim, record, launch.

The correctness argument, because it is easy to break:

* ``next_run_at`` moves forward at CLAIM time, not at completion. So a task
  can be missed (recorded as a failed run) but never run twice, which is the
  right trade for a digest — a duplicate 8am DM is worse than a gap.
* The claim is a compare-and-swap on the ``next_run_at`` the caller read
  (:func:`~rubik_core.scheduled_tasks.store.claim_task_for_run`), so two
  overlapping ticks cannot both claim one occurrence.
* The worker is launched AFTER the claim commits. A launch failure releases the
  claim and records a failed run; the next occurrence is already scheduled.
* Crashed workers are swept by :func:`reap_stale_runs`, well past the run
  timeout plus a cold start, so a slow run is never failed out from under itself.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from rubik_core.config import settings
from rubik_core.scheduled_tasks import schedule as sched
from rubik_core.scheduled_tasks import store

logger = logging.getLogger(__name__)

RUN_JOB_MODULE = "backend.jobs.scheduled_task_run_job"
# Extra slack beyond the run timeout before the reaper calls a run dead: a
# Cloud Run Job task has to be scheduled and cold-start the image first.
REAP_GRACE_SECONDS = 900


class AlreadyRunning(RuntimeError):
    """A manual run was asked for while one is already in flight."""


def _default_launcher(module: str, args: list[str]) -> str | None:
    """Launch one ``cubie-worker`` execution; return its execution name.

    Off GCP there is no Jobs API, so the work runs inline in this thread. That
    is correct for local development (where a dev is watching a request finish)
    and never used on Cloud Run, which routes through the Jobs API so the run
    gets a CPU-allocated task of its own.
    """
    from rubik_core.config import AppMode

    if settings.APP_MODE == AppMode.GCP:
        from backend.jobs.launcher import launch_module_execution

        execution = launch_module_execution(module, args)
        return execution.get("name") or execution.get("metadata", {}).get("name")

    logger.warning(
        "[scheduled-tasks] APP_MODE is not gcp — running %s %s inline. This is "
        "the local-development path; on Cloud Run the work belongs on a Job "
        "task, which is CPU-allocated to completion.",
        module,
        args,
    )
    from backend.jobs.scheduled_task_run_job import main as run_main

    run_main(args)
    return None


def dispatch_due_tasks(
    *,
    now: datetime | None = None,
    launcher=None,
    limit: int | None = None,
) -> dict:
    """One tick: reap dead runs, then claim and launch everything due.

    Returns named outcome buckets rather than a bare count, so the scheduler's
    log line says which of "nothing was due", "we launched four" and "four
    launches failed" actually happened.
    """
    stamp = now or datetime.utcnow()
    launch = launcher or _default_launcher
    cap = limit if limit is not None else settings.SCHEDULED_TASKS_TICK_MAX_DISPATCH

    report = {
        "due": 0,
        "claimed": 0,
        "dispatched": 0,
        "dispatch_failed": 0,
        "lost_claim": 0,
        "no_next_run": 0,
        "reaped": 0,
        "deferred": 0,
    }

    from rubik_core.database.db import SessionLocal

    with SessionLocal() as db:
        report["reaped"] = reap_stale_runs(db, now=stamp)

        due = store.due_tasks(db, stamp, cap + 1)
        report["due"] = min(len(due), cap)
        if len(due) > cap:
            due = due[:cap]
            report["deferred"] = 1  # at least one; still due next tick

        for task in due:
            seen = task.next_run_at
            spec = sched.schedule_from_task(task)
            try:
                upcoming = sched.next_run_utc(spec, stamp)
            except Exception:  # noqa: BLE001 — a bad row must not stall the tick
                logger.exception(
                    "[scheduled-tasks] cannot compute next run for task=%s", task.id
                )
                report["no_next_run"] += 1
                continue

            if not store.claim_task_for_run(
                db,
                task,
                seen_next_run_at=seen,
                new_next_run_at=upcoming,
                now=stamp,
            ):
                # Another tick got this occurrence — not an error.
                report["lost_claim"] += 1
                continue
            report["claimed"] += 1

            folder_id = None
            try:
                folder_id = store.ensure_results_folder(db, task.user_email)
            except Exception:  # noqa: BLE001 — the runner retries this itself
                logger.exception(
                    "[scheduled-tasks] could not prepare the results folder for %s",
                    task.user_email,
                )

            run = store.create_run(
                db,
                task,
                scheduled_for=seen,
                trigger="schedule",
                chat_folder_id=folder_id,
            )
            db.commit()

            if _launch(db, launch, run.id, task.id, label=task.title):
                report["dispatched"] += 1
            else:
                report["dispatch_failed"] += 1

    logger.info(
        "[scheduled-tasks] tick due=%(due)s claimed=%(claimed)s "
        "dispatched=%(dispatched)s dispatch_failed=%(dispatch_failed)s "
        "lost_claim=%(lost_claim)s reaped=%(reaped)s deferred=%(deferred)s",
        report,
    )
    return report


def dispatch_task_now(db: Session, task, *, launcher=None) -> dict:
    """Run ``task`` immediately without touching its schedule.

    Raises :class:`AlreadyRunning` when a run is in flight — a second
    concurrent run of the same task would double-charge tokens and deliver the
    same answer twice.
    """
    if store.has_unfinished_run(db, task.id):
        raise AlreadyRunning(task.id)

    launch = launcher or _default_launcher
    folder_id = None
    try:
        folder_id = store.ensure_results_folder(db, task.user_email)
    except Exception:  # noqa: BLE001
        logger.exception(
            "[scheduled-tasks] could not prepare the results folder for %s",
            task.user_email,
        )

    run = store.create_run(
        db,
        task,
        scheduled_for=datetime.utcnow(),
        trigger="manual",
        chat_folder_id=folder_id,
    )
    db.commit()

    dispatched = _launch(db, launch, run.id, task.id, label=task.title)
    return {"run_id": run.id, "dispatched": dispatched}


def _launch(db: Session, launch, run_id: str, task_id: str, *, label: str) -> bool:
    """Launch the worker for ``run_id``; record the outcome either way."""
    try:
        execution_name = launch(RUN_JOB_MODULE, ["--run-id", run_id])
    except Exception as exc:  # noqa: BLE001 — a launch failure is this run's failure
        logger.exception(
            "[scheduled-tasks] launch failed for run=%s task=%s (%s)",
            run_id,
            task_id,
            label,
        )
        store.finish_run(
            db,
            run_id,
            status="failed",
            error=f"Could not start the run: {exc}",
        )
        store.record_task_outcome(
            db,
            task_id,
            run_id=run_id,
            status="failed",
            max_consecutive_failures=settings.SCHEDULED_TASKS_MAX_CONSECUTIVE_FAILURES,
        )
        return False

    store.record_dispatch(db, run_id, execution_name)
    logger.info(
        "[scheduled-tasks] dispatched run=%s task=%s execution=%s",
        run_id,
        task_id,
        execution_name,
    )
    return True


def reap_stale_runs(db: Session, *, now: datetime | None = None) -> int:
    """Fail runs whose worker never reported, and free their tasks.

    Without this a crashed worker leaves a task's ``claimed_at`` set forever,
    which blocks manual runs and hides a real failure as "still running".
    """
    stamp = now or datetime.utcnow()
    cutoff = stamp - timedelta(
        seconds=settings.SCHEDULED_TASKS_RUN_TIMEOUT_SECONDS + REAP_GRACE_SECONDS
    )
    stale = store.stale_runs(db, cutoff)
    for run in stale:
        logger.warning(
            "[scheduled-tasks] reaping run=%s task=%s (stuck in %s since %s)",
            run.id,
            run.task_id,
            run.status,
            run.created_at,
        )
        store.finish_run(
            db,
            run.id,
            status="failed",
            error="The run never reported back — its worker was lost.",
        )
        store.record_task_outcome(
            db,
            run.task_id,
            run_id=run.id,
            status="failed",
            max_consecutive_failures=settings.SCHEDULED_TASKS_MAX_CONSECUTIVE_FAILURES,
        )
    return len(stale)

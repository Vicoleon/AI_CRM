"""Persistence for scheduled tasks and their runs.

Every function takes an open session — callers own transaction scope, matching
``rubik_core/offboarding/store.py``.

Two functions here are concurrency-critical and are written as
compare-and-swap UPDATEs rather than read-then-write, because two Cloud
Scheduler ticks (or a tick and a retrying worker) genuinely do race:

* :func:`claim_task_for_run` moves a task's ``next_run_at`` forward only if it
  still holds the value the caller saw. Exactly one caller wins.
* :func:`claim_run_running` moves a run ``queued -> running``. Cloud Run
  retries a crashed job task, and this is what stops the retry re-delivering
  work the first attempt already finished.

Do not "improve" either into a pre-check plus a write: both callers would pass
the check.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from rubik_core.database.models import (
    ChatFolder,
    ScheduledTask,
    ScheduledTaskRun,
)

logger = logging.getLogger(__name__)

TASK_STATUSES = ("active", "paused", "completed", "error_paused")
RUN_STATUSES = ("queued", "running", "succeeded", "failed", "skipped")
# The chat folder every scheduled result lands in. One per user, created on
# first delivery; the name is what the user sees in the Chat sidebar.
RESULTS_FOLDER_NAME = "Scheduled tasks"


def _now() -> datetime:
    return datetime.utcnow()


# ── tasks ───────────────────────────────────────────────────────────────────


def create_task(
    db: Session,
    *,
    user_email: str,
    title: str,
    prompt: str,
    schedule,
    next_run_at: datetime | None,
    delivery_channels: list[str],
    source_session_id: str | None = None,
    created_via: str = "chat",
    max_per_user: int | None = None,
) -> ScheduledTask:
    """Store a validated schedule.

    ``schedule`` is a :class:`rubik_core.scheduled_tasks.schedule.Schedule` —
    validation happens before this call, so anything reaching here is storable.
    Raises ``ValueError`` when the user is at their task limit; the router
    turns that into a 400 and the chat tool relays the text.
    """
    email = (user_email or "").strip()
    if not email:
        raise ValueError("a scheduled task needs an owner")
    if not (title or "").strip():
        raise ValueError("a scheduled task needs a title")
    if not (prompt or "").strip():
        raise ValueError("a scheduled task needs something to do")

    if max_per_user is not None:
        live = (
            db.query(ScheduledTask)
            .filter(
                ScheduledTask.user_email == email,
                ScheduledTask.status.in_(("active", "paused", "error_paused")),
            )
            .count()
        )
        if live >= max_per_user:
            raise ValueError(
                f"You already have {live} scheduled tasks, which is the limit "
                f"({max_per_user}). Delete one before adding another."
            )

    task = ScheduledTask(
        id=str(uuid.uuid4()),
        user_email=email,
        title=title.strip()[:200],
        prompt=prompt.strip(),
        schedule_kind=schedule.kind,
        time_local=schedule.time_local,
        days_of_week=list(schedule.days_of_week) or None,
        day_of_month=schedule.day_of_month,
        run_at=schedule.run_at,
        timezone=schedule.timezone,
        delivery_channels=list(delivery_channels or []),
        status="active",
        next_run_at=next_run_at,
        consecutive_failures=0,
        run_count=0,
        source_session_id=source_session_id,
        created_via=created_via,
        created_at=_now(),
    )
    db.add(task)
    db.flush()
    return task


def get_task(db: Session, task_id: str) -> ScheduledTask | None:
    return db.get(ScheduledTask, task_id)


def get_task_for_user(
    db: Session, task_id: str, user_email: str
) -> ScheduledTask | None:
    """The task, only if it belongs to ``user_email``.

    Every user-facing path goes through this rather than :func:`get_task`, so
    another person's id reads as "not found" instead of leaking its existence.
    """
    task = db.get(ScheduledTask, task_id)
    if task is None:
        return None
    if (task.user_email or "").lower() != (user_email or "").lower():
        return None
    return task


def list_tasks(db: Session, user_email: str) -> list[ScheduledTask]:
    return (
        db.query(ScheduledTask)
        .filter(ScheduledTask.user_email == user_email)
        .order_by(ScheduledTask.created_at.desc())
        .all()
    )


def count_active_tasks(db: Session, user_email: str) -> int:
    return (
        db.query(ScheduledTask)
        .filter(
            ScheduledTask.user_email == user_email,
            ScheduledTask.status == "active",
        )
        .count()
    )


def apply_schedule(
    db: Session,
    task: ScheduledTask,
    *,
    schedule,
    next_run_at: datetime | None,
) -> ScheduledTask:
    """Re-point a task at a new (already validated) schedule."""
    task.schedule_kind = schedule.kind
    task.time_local = schedule.time_local
    task.days_of_week = list(schedule.days_of_week) or None
    task.day_of_month = schedule.day_of_month
    task.run_at = schedule.run_at
    task.timezone = schedule.timezone
    task.next_run_at = next_run_at
    task.updated_at = _now()
    db.flush()
    return task


def set_status(
    db: Session,
    task: ScheduledTask,
    status: str,
    *,
    reason: str | None = None,
    next_run_at: datetime | None = None,
) -> ScheduledTask:
    """Move a task between active/paused/completed/error_paused.

    Resuming clears the failure counter: the user has looked at it, so the next
    failure starts a fresh streak toward the auto-pause threshold.
    """
    if status not in TASK_STATUSES:
        raise ValueError(f"unknown scheduled-task status {status!r}")
    task.status = status
    task.paused_reason = reason
    if status == "active":
        task.consecutive_failures = 0
        task.claimed_at = None
        if next_run_at is not None:
            task.next_run_at = next_run_at
    task.updated_at = _now()
    db.flush()
    return task


def delete_task(db: Session, task: ScheduledTask) -> None:
    """Delete a task and its run history.

    The chat conversations its runs produced are ordinary ``chat_sessions``
    rows and are deliberately left alone — the user's copies of past results
    are theirs, not the schedule's.
    """
    db.delete(task)
    db.flush()


def claim_task_for_run(
    db: Session,
    task: ScheduledTask,
    *,
    seen_next_run_at: datetime | None,
    new_next_run_at: datetime | None,
    now: datetime | None = None,
) -> bool:
    """Compare-and-swap a due task forward; True when this caller won.

    Guarded on the ``next_run_at`` the caller read, so a second tick that saw
    the same due row updates nothing and skips it. ``new_next_run_at`` of
    ``None`` means nothing more will fire — the caller marks a ``once`` task
    completed separately, after the run is recorded.
    """
    stamp = now or _now()
    claimed = (
        db.query(ScheduledTask)
        .filter(
            ScheduledTask.id == task.id,
            ScheduledTask.status == "active",
            ScheduledTask.next_run_at == seen_next_run_at,
        )
        .update(
            {
                "next_run_at": new_next_run_at,
                "claimed_at": stamp,
                "updated_at": stamp,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    return claimed == 1


def release_claim(db: Session, task_id: str) -> None:
    """Clear the in-flight marker (dispatch failed, or a run finished)."""
    db.query(ScheduledTask).filter(ScheduledTask.id == task_id).update(
        {"claimed_at": None, "updated_at": _now()}, synchronize_session=False
    )
    db.commit()


def due_tasks(db: Session, now: datetime, limit: int) -> list[ScheduledTask]:
    """Active tasks whose next occurrence has arrived, oldest first.

    Ordered by ``next_run_at`` so that when more tasks are due than one tick
    dispatches, the longest-overdue go first and the rest are simply still due
    next tick.
    """
    return (
        db.query(ScheduledTask)
        .filter(
            ScheduledTask.status == "active",
            ScheduledTask.next_run_at.isnot(None),
            ScheduledTask.next_run_at <= now,
        )
        .order_by(ScheduledTask.next_run_at.asc())
        .limit(limit)
        .all()
    )


# ── runs ────────────────────────────────────────────────────────────────────


def create_run(
    db: Session,
    task: ScheduledTask,
    *,
    scheduled_for: datetime | None,
    trigger: str = "schedule",
    chat_folder_id: str | None = None,
) -> ScheduledTaskRun:
    run = ScheduledTaskRun(
        id=str(uuid.uuid4()),
        task_id=task.id,
        user_email=task.user_email,
        scheduled_for=scheduled_for,
        trigger=trigger,
        status="queued",
        chat_folder_id=chat_folder_id,
        created_at=_now(),
    )
    db.add(run)
    db.flush()
    return run


def get_run(db: Session, run_id: str) -> ScheduledTaskRun | None:
    return db.get(ScheduledTaskRun, run_id)


def list_runs(db: Session, task_id: str, limit: int = 20) -> list[ScheduledTaskRun]:
    return (
        db.query(ScheduledTaskRun)
        .filter(ScheduledTaskRun.task_id == task_id)
        .order_by(ScheduledTaskRun.created_at.desc())
        .limit(limit)
        .all()
    )


def latest_runs_by_task(
    db: Session, task_ids: list[str]
) -> dict[str, ScheduledTaskRun]:
    """Newest run per task, for the list view. One query, not N.

    Bounded deliberately: this runs on every page load, and a daily task
    accumulates a run row per day forever, so loading a user's whole history
    just to keep the last row of each would grow without limit. The rows come
    back newest-first across all of the user's tasks, and the window is
    generous enough to contain the newest run of every one of them.
    """
    if not task_ids:
        return {}
    window = max(len(task_ids) * 4, 40)
    rows = (
        db.query(ScheduledTaskRun)
        .filter(ScheduledTaskRun.task_id.in_(task_ids))
        .order_by(ScheduledTaskRun.created_at.desc())
        .limit(window)
        .all()
    )
    latest: dict[str, ScheduledTaskRun] = {}
    for row in rows:  # newest first, so the first sighting per task wins
        latest.setdefault(row.task_id, row)
    return latest


def has_unfinished_run(db: Session, task_id: str) -> bool:
    """True when a run is queued or running — used to refuse an overlapping
    manual run rather than double-charge the user's tokens."""
    return (
        db.query(ScheduledTaskRun)
        .filter(
            ScheduledTaskRun.task_id == task_id,
            ScheduledTaskRun.status.in_(("queued", "running")),
        )
        .count()
        > 0
    )


def record_dispatch(db: Session, run_id: str, execution_name: str | None) -> None:
    db.query(ScheduledTaskRun).filter(ScheduledTaskRun.id == run_id).update(
        {"execution_name": execution_name}, synchronize_session=False
    )
    db.commit()


def claim_run_running(db: Session, run_id: str, *, now: datetime | None = None) -> bool:
    """CAS a run ``queued -> running``; True when this process owns it.

    False means somebody already claimed it — which on Cloud Run means THIS IS
    A RETRY of a task that already ran. The caller must exit successfully
    without doing the work again.
    """
    stamp = now or _now()
    claimed = (
        db.query(ScheduledTaskRun)
        .filter(
            ScheduledTaskRun.id == run_id,
            ScheduledTaskRun.status == "queued",
        )
        .update({"status": "running", "started_at": stamp}, synchronize_session=False)
    )
    db.commit()
    return claimed == 1


def finish_run(
    db: Session,
    run_id: str,
    *,
    status: str,
    error: str | None = None,
    chat_session_id: str | None = None,
    delivery: dict | None = None,
) -> None:
    if status not in RUN_STATUSES:
        raise ValueError(f"unknown run status {status!r}")
    values: dict = {"status": status, "finished_at": _now()}
    if error is not None:
        values["error"] = error[:4000]
    if chat_session_id is not None:
        values["chat_session_id"] = chat_session_id
    if delivery is not None:
        values["delivery"] = delivery
    db.query(ScheduledTaskRun).filter(ScheduledTaskRun.id == run_id).update(
        values, synchronize_session=False
    )
    db.commit()


def stale_runs(
    db: Session, cutoff: datetime, limit: int = 50
) -> list[ScheduledTaskRun]:
    """Runs left queued/running past ``cutoff`` — a worker that never reported.

    The cutoff must be well beyond the run timeout plus a cold start, or the
    reaper fails runs that are still legitimately working.
    """
    return (
        db.query(ScheduledTaskRun)
        .filter(
            ScheduledTaskRun.status.in_(("queued", "running")),
            ScheduledTaskRun.created_at < cutoff,
        )
        .order_by(ScheduledTaskRun.created_at.asc())
        .limit(limit)
        .all()
    )


def record_task_outcome(
    db: Session,
    task_id: str,
    *,
    run_id: str,
    status: str,
    max_consecutive_failures: int,
    completed: bool = False,
) -> ScheduledTask | None:
    """Fold one run's outcome into the task; auto-pause a persistently failing one.

    ``skipped`` counts toward the failure streak (a task whose owner lost
    access is not succeeding), but never resets it. Returns the task when it
    was just auto-paused, so the caller can tell the user, else ``None``.
    """
    task = db.get(ScheduledTask, task_id)
    if task is None:
        return None

    now = _now()
    task.last_run_at = now
    task.last_run_status = status
    task.last_run_id = run_id
    task.claimed_at = None
    task.updated_at = now
    if status == "succeeded":
        task.run_count = (task.run_count or 0) + 1
        task.consecutive_failures = 0
    else:
        task.consecutive_failures = (task.consecutive_failures or 0) + 1

    auto_paused = None
    if completed and task.status == "active":
        task.status = "completed"
        task.next_run_at = None
    elif (
        task.status == "active"
        and task.consecutive_failures >= max_consecutive_failures
    ):
        task.status = "error_paused"
        task.paused_reason = (
            f"Paused automatically after {task.consecutive_failures} failed runs "
            "in a row."
        )
        task.next_run_at = None
        auto_paused = task
        logger.warning(
            "[scheduled-tasks] task=%s auto-paused after %d consecutive failures",
            task_id,
            task.consecutive_failures,
        )

    db.commit()
    return auto_paused


# ── chat folder ─────────────────────────────────────────────────────────────


def ensure_results_folder(db: Session, user_email: str) -> str:
    """The id of this user's "Scheduled tasks" chat folder, creating it once.

    Called from the DISPATCHER, which handles due tasks sequentially, so two
    tasks firing in the same minute cannot race a duplicate into existence
    (``chat_folders`` has no unique constraint on name).
    """
    existing = (
        db.query(ChatFolder)
        .filter(
            ChatFolder.user_email == user_email,
            ChatFolder.name == RESULTS_FOLDER_NAME,
        )
        .order_by(ChatFolder.created_at.asc())
        .first()
    )
    if existing is not None:
        return existing.id

    folder = ChatFolder(
        id=str(uuid.uuid4()),
        user_email=user_email,
        name=RESULTS_FOLDER_NAME,
        created_at=_now(),
    )
    db.add(folder)
    db.commit()
    return folder.id

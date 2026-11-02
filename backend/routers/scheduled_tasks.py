"""Manage the recurring tasks a user scheduled from Chat.

Creation lives in the CHAT tools, not here: describing a standing request in a
sentence is the feature, and a form asking for a cron expression is the thing
it replaces. This router is the management surface — see what you scheduled,
change the time, pause it, run it now, delete it.

Every endpoint is scoped to the signed-in user (another person's id reads as
404, never 403, so an id cannot be probed for existence) and gated on the
``scheduled_tasks`` feature, which an admin widens from Admin → Feature Access.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.auth import get_current_user
from rubik_core.auth.registry import UserProfile
from rubik_core.config import settings
from rubik_core.database.db import get_db
from rubik_core.features import feature_allows
from rubik_core.scheduled_tasks import dispatch, store
from rubik_core.scheduled_tasks import schedule as sched

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/scheduled-tasks", tags=["scheduled-tasks"])


def require_scheduled_tasks(
    user: UserProfile = Depends(get_current_user),
) -> UserProfile:
    """Gate for the whole router.

    The env switch answers "does this run in this environment at all" (404 —
    there is no such surface here); the roster answers "may this person reach
    it" (403 — the surface exists and they are not on it yet).
    """
    if not settings.SCHEDULED_TASKS_ENABLED:
        raise HTTPException(status_code=404, detail="Not found.")
    if not feature_allows("scheduled_tasks", user):
        raise HTTPException(
            status_code=403,
            detail="Scheduled tasks are not turned on for you. Ask your admin.",
        )
    return user


# ── payloads ────────────────────────────────────────────────────────────────


class RunOut(BaseModel):
    id: str
    status: str
    trigger: str
    scheduled_for: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    chat_session_id: str | None = None
    delivery: dict | None = None
    error: str | None = None
    created_at: datetime


class TaskOut(BaseModel):
    id: str
    title: str
    prompt: str
    schedule_kind: str
    # Plain English, e.g. "every weekday at 8:00 AM (America/Chicago)" — the
    # client renders this rather than re-deriving it from the parts.
    schedule_text: str
    timezone: str
    time_local: str | None = None
    days_of_week: list[int] | None = None
    day_of_month: int | None = None
    status: str
    paused_reason: str | None = None
    next_run_at: datetime | None = None
    next_run_text: str = ""
    delivery_channels: list[str] = Field(default_factory=list)
    last_run_at: datetime | None = None
    last_run_status: str | None = None
    run_count: int = 0
    consecutive_failures: int = 0
    created_at: datetime
    last_run: RunOut | None = None


class TaskListOut(BaseModel):
    tasks: list[TaskOut] = Field(default_factory=list)
    max_per_user: int


class TaskPatch(BaseModel):
    """Partial update. Only the fields present are changed."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    prompt: str | None = Field(default=None, min_length=1)
    status: Literal["active", "paused"] | None = None
    schedule_kind: Literal["daily", "weekdays", "weekly", "monthly"] | None = None
    time_local: str | None = Field(default=None, description='24-hour "HH:MM" local')
    days_of_week: list[int] | None = None
    day_of_month: int | None = Field(default=None, ge=1, le=31)
    timezone: str | None = None
    deliver_by_slack: bool | None = None
    deliver_by_email: bool | None = None


def _serialize(task, last_run=None) -> TaskOut:
    spec = sched.schedule_from_task(task)
    return TaskOut(
        id=task.id,
        title=task.title,
        prompt=task.prompt,
        schedule_kind=task.schedule_kind,
        schedule_text=sched.describe_schedule(spec),
        timezone=task.timezone,
        time_local=task.time_local,
        days_of_week=list(task.days_of_week or []) or None,
        day_of_month=task.day_of_month,
        status=task.status,
        paused_reason=task.paused_reason,
        next_run_at=task.next_run_at,
        next_run_text=sched.format_local(task.next_run_at, task.timezone)
        if task.status == "active"
        else "",
        delivery_channels=list(task.delivery_channels or []),
        last_run_at=task.last_run_at,
        last_run_status=task.last_run_status,
        run_count=task.run_count or 0,
        consecutive_failures=task.consecutive_failures or 0,
        created_at=task.created_at,
        last_run=RunOut.model_validate(last_run, from_attributes=True)
        if last_run is not None
        else None,
    )


def _load(db: Session, task_id: str, user: UserProfile):
    task = store.get_task_for_user(db, task_id, user.email)
    if task is None:
        # 404 for somebody else's id too: a 403 would confirm it exists.
        raise HTTPException(status_code=404, detail="Scheduled task not found.")
    return task


# ── endpoints ───────────────────────────────────────────────────────────────


@router.get("", response_model=TaskListOut)
async def list_scheduled_tasks(
    user: UserProfile = Depends(require_scheduled_tasks),
    db: Session = Depends(get_db),
):
    """This user's scheduled tasks, newest first, each with its latest run."""
    tasks = store.list_tasks(db, user.email)
    latest = store.latest_runs_by_task(db, [t.id for t in tasks])
    return TaskListOut(
        tasks=[_serialize(t, latest.get(t.id)) for t in tasks],
        max_per_user=settings.SCHEDULED_TASKS_MAX_PER_USER,
    )


@router.get("/{task_id}/runs", response_model=list[RunOut])
async def list_task_runs(
    task_id: str,
    user: UserProfile = Depends(require_scheduled_tasks),
    db: Session = Depends(get_db),
):
    """Recent attempts at one task — what ran, when, and what went wrong."""
    task = _load(db, task_id, user)
    return [
        RunOut.model_validate(run, from_attributes=True)
        for run in store.list_runs(db, task.id)
    ]


@router.patch("/{task_id}", response_model=TaskOut)
async def update_scheduled_task(
    task_id: str,
    body: TaskPatch,
    user: UserProfile = Depends(require_scheduled_tasks),
    db: Session = Depends(get_db),
):
    """Edit, pause or resume one task.

    Any change to the schedule recomputes ``next_run_at`` from now, so moving
    8:00 to 7:30 after today's run has already gone out takes effect tomorrow
    rather than firing again immediately.
    """
    task = _load(db, task_id, user)
    payload = body.model_dump(exclude_unset=True)
    if not payload:
        raise HTTPException(status_code=400, detail="Nothing to change.")

    if task.status == "completed" and payload.get("status") == "active":
        raise HTTPException(
            status_code=400,
            detail="That was a one-off run and it has already happened.",
        )

    schedule_fields = {
        "schedule_kind",
        "time_local",
        "days_of_week",
        "day_of_month",
        "timezone",
    }
    if schedule_fields & payload.keys():
        if task.schedule_kind == "once" and "schedule_kind" not in payload:
            raise HTTPException(
                status_code=400,
                detail=(
                    "That one is a one-off run. Delete it and schedule a new "
                    "one for the time you want."
                ),
            )
        try:
            spec = sched.validate_schedule(
                schedule_kind=payload.get("schedule_kind", task.schedule_kind),
                timezone_name=payload.get("timezone", task.timezone),
                time_local=payload.get("time_local", task.time_local),
                days_of_week=payload.get("days_of_week", task.days_of_week),
                day_of_month=payload.get("day_of_month", task.day_of_month),
            )
        except sched.ScheduleError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        next_run = sched.next_run_utc(spec, datetime.now(timezone.utc))
        if next_run is None:
            raise HTTPException(
                status_code=400, detail="That schedule never comes around."
            )
        store.apply_schedule(db, task, schedule=spec, next_run_at=next_run)

    if "title" in payload:
        task.title = payload["title"].strip()[:200]
    if "prompt" in payload:
        task.prompt = payload["prompt"].strip()

    if {"deliver_by_slack", "deliver_by_email"} & payload.keys():
        channels = set(task.delivery_channels or [])
        if "deliver_by_slack" in payload:
            channels.add("slack") if payload["deliver_by_slack"] else channels.discard(
                "slack"
            )
        if "deliver_by_email" in payload:
            channels.add("email") if payload["deliver_by_email"] else channels.discard(
                "email"
            )
        task.delivery_channels = sorted(channels)

    if "status" in payload:
        next_run = None
        if payload["status"] == "active":
            next_run = sched.next_run_utc(
                sched.schedule_from_task(task), datetime.now(timezone.utc)
            )
            if next_run is None:
                raise HTTPException(
                    status_code=400,
                    detail="That schedule has no future runs left.",
                )
        store.set_status(db, task, payload["status"], next_run_at=next_run)

    task.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(task)
    logger.info(
        "[scheduled-tasks] task=%s updated by %s fields=%s",
        task.id,
        user.email,
        sorted(payload.keys()),
    )
    return _serialize(task)


@router.post("/{task_id}/run-now", response_model=RunOut)
async def run_scheduled_task_now(
    task_id: str,
    user: UserProfile = Depends(require_scheduled_tasks),
    db: Session = Depends(get_db),
):
    """Run one task immediately, leaving its schedule untouched.

    409 when a run is already in flight: a second concurrent run would spend
    the tokens twice and deliver the same answer twice.
    """
    task = _load(db, task_id, user)
    try:
        outcome = dispatch.dispatch_task_now(db, task)
    except dispatch.AlreadyRunning:
        raise HTTPException(
            status_code=409,
            detail="That task is already running — its result is on the way.",
        ) from None

    run = store.get_run(db, outcome["run_id"])
    logger.info(
        "[scheduled-tasks] task=%s manual run=%s requested by %s",
        task.id,
        outcome["run_id"],
        user.email,
    )
    return RunOut.model_validate(run, from_attributes=True)


@router.delete("/{task_id}")
async def delete_scheduled_task(
    task_id: str,
    user: UserProfile = Depends(require_scheduled_tasks),
    db: Session = Depends(get_db),
):
    """Delete a task and its run history.

    The chat conversations its past runs produced are left alone — those are
    the user's copies of results they already read.
    """
    task = _load(db, task_id, user)
    title = task.title
    store.delete_task(db, task)
    db.commit()
    logger.info("[scheduled-tasks] task=%s deleted by %s", task_id, user.email)
    return {"status": "deleted", "title": title}

"""Chat tools for scheduling recurring Cubie work.

Identity comes from the constructor — same contract as ``AgentTools``: no tool
signature takes an email, so the model cannot
schedule work for, or read the schedules of, anybody but the signed-in user.

Its own container rather than a method on ``AgentTools`` because that class is
constructed with a caller-supplied address in several non-chat paths
(``routers/home.py``, the nightly pipeline, the X-Ray agents, and now the
scheduled-task runner itself). A tool that CREATES recurring spend must never
be reachable from a path where the address was passed in.

Every tool returns a dict with ``status`` and a ``report`` the model relays.
Where a schedule is involved the report contains the schedule in plain English
and the next run time, so the user can catch a misheard "8" before it fires.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

MANAGE_LINK = "/settings?tab=scheduled-tasks"


class ScheduledTaskChatTools:
    """Scheduled-task tools bound to one signed-in user."""

    def __init__(
        self,
        user_email: str,
        user_timezone: str = "UTC",
        source_session_id: str | None = None,
    ):
        self.user_email = user_email
        # The browser's timezone for this chat turn. A saved preference wins
        # over it (see _resolve_timezone) because that is the user's deliberate
        # answer, whereas this is wherever their laptop happens to be.
        self.user_timezone = user_timezone or "UTC"
        self.source_session_id = source_session_id

    # ── plumbing ────────────────────────────────────────────────────────────

    def _session(self):
        """Open a short-lived session — chat tools never get the request's."""
        from rubik_core.database.db import SessionLocal

        return SessionLocal()

    def _resolve_timezone(self) -> str:
        """The user's saved timezone, else this turn's browser timezone.

        A scheduled task outlives the browser session that created it, so a
        saved Settings → General timezone is the better answer when it exists.
        """
        try:
            from rubik_core.database.models import UserPreference

            with self._session() as db:
                saved = (
                    db.query(UserPreference.timezone)
                    .filter(UserPreference.user_email == self.user_email)
                    .scalar()
                )
            if (saved or "").strip():
                return saved.strip()
        except Exception:  # noqa: BLE001 — fall back to the browser's answer
            logger.debug(
                "scheduled_tasks: saved-timezone lookup failed for %s",
                self.user_email,
                exc_info=True,
            )
        return self.user_timezone or "UTC"

    def _channels(self, deliver_by_slack: bool, deliver_by_email: bool) -> list[str]:
        channels = []
        if deliver_by_slack:
            channels.append("slack")
        if deliver_by_email:
            channels.append("email")
        return channels

    def _describe(self, task) -> str:
        from rubik_core.scheduled_tasks import schedule as sched

        spec = sched.schedule_from_task(task)
        text = sched.describe_schedule(spec)
        when = sched.format_local(task.next_run_at, task.timezone)
        extras = list(task.delivery_channels or [])
        where = "your chat"
        if "slack" in extras and "email" in extras:
            where = "your chat, a Slack DM and email"
        elif "slack" in extras:
            where = "your chat and a Slack DM"
        elif "email" in extras:
            where = "your chat and email"

        parts = [f"**{task.title}** — {text}."]
        if task.status == "active" and when:
            parts.append(f"Next run: {when}.")
        elif task.status != "active":
            parts.append(f"Status: {task.status.replace('_', ' ')}.")
        parts.append(f"Delivered to {where}.")
        return " ".join(parts)

    def _refusal(self, message: str) -> dict[str, Any]:
        return {"status": "error", "report": message}

    # ── tools ───────────────────────────────────────────────────────────────

    def create_scheduled_task(
        self,
        title: str,
        prompt: str,
        schedule_kind: str,
        time_local: str = "08:00",
        days_of_week: list[int] | None = None,
        day_of_month: int | None = None,
        run_at_local: str = "",
        deliver_by_slack: bool = True,
        deliver_by_email: bool = False,
    ) -> dict:
        """Schedule a task for Cubie to run for this user automatically, on a repeating schedule.

        Use this whenever the user asks for something to happen on a cadence or
        at a future time: "every morning send me my important emails", "each
        Monday summarize last week's meetings", "every weekday at 7 give me my
        calendar and open to-dos", "remind me monthly to review the pipeline",
        "tomorrow at 9 pull the Acme account history".

        The task runs LATER, with nobody watching and NO memory of this
        conversation, so `prompt` must stand completely on its own: write it as
        a full instruction naming the sources and the shape of the answer
        (e.g. "Summarize my unread Gmail from the last 24 hours, grouping by
        sender and flagging anything that needs a reply today"), never as
        "do that again" or "the same as above". Do not put a date or time
        inside `prompt` — the schedule handles when, and the run is told the
        current date itself.

        Args:
            title: Short label the user will recognise in their list, e.g.
                "Morning important emails". 4-8 words.
            prompt: The self-contained instruction to run each time.
            schedule_kind: One of "daily", "weekdays" (Mon-Fri), "weekly"
                (specific days), "monthly" (a day of the month), or "once".
            time_local: 24-hour "HH:MM" in the user's own timezone. Convert
                what they said ("8am" → "08:00", "5:30pm" → "17:30"). Ignored
                for "once".
            days_of_week: For "weekly" only — the days as integers with
                Monday=0 and Sunday=6 (e.g. [0, 3] for Monday and Thursday).
            day_of_month: For "monthly" only — 1 to 31. A 29-31 falls on the
                last day of shorter months.
            run_at_local: For "once" only — the local date and time as
                "YYYY-MM-DDTHH:MM", e.g. "2026-09-09T08:00".
            deliver_by_slack: Send the result as a Slack DM too. Default True;
                set False if the user says chat only.
            deliver_by_email: Also email the result. Only when the user asks
                for email.

        Returns:
            dict with the created task and a `report` naming the schedule and
            next run. Relay that back so the user can correct the time. If it
            returns an error, the `report` explains what to fix — ask them.
        """
        from rubik_core.config import settings
        from rubik_core.scheduled_tasks import schedule as sched
        from rubik_core.scheduled_tasks import store

        tz_name = self._resolve_timezone()
        try:
            spec = sched.validate_schedule(
                schedule_kind=schedule_kind,
                timezone_name=tz_name,
                time_local=time_local,
                days_of_week=days_of_week,
                day_of_month=day_of_month,
                run_at_local=run_at_local,
            )
        except sched.ScheduleError as exc:
            return self._refusal(str(exc))

        now = datetime.now(timezone.utc)
        next_run = sched.next_run_utc(spec, now)
        if next_run is None:
            return self._refusal(
                "That schedule never comes around — check the date and time."
            )

        try:
            with self._session() as db:
                task = store.create_task(
                    db,
                    user_email=self.user_email,
                    title=title,
                    prompt=prompt,
                    schedule=spec,
                    next_run_at=next_run,
                    delivery_channels=self._channels(
                        deliver_by_slack, deliver_by_email
                    ),
                    source_session_id=self.source_session_id,
                    created_via="chat",
                    max_per_user=settings.SCHEDULED_TASKS_MAX_PER_USER,
                )
                db.commit()
                summary = self._describe(task)
                task_id = task.id
        except ValueError as exc:
            return self._refusal(str(exc))
        except Exception:  # noqa: BLE001
            logger.exception("scheduled_tasks: create failed for %s", self.user_email)
            return self._refusal(
                "I couldn't save that schedule just now. Try again in a moment."
            )

        logger.info(
            "[scheduled-tasks] created task=%s user=%s kind=%s next=%s",
            task_id,
            self.user_email,
            spec.kind,
            next_run,
        )
        note = ""
        if deliver_by_email and not settings.SCHEDULED_TASKS_EMAIL_AUTOSEND:
            note = (
                " Email delivery is not switched on in this environment yet, so "
                "for now the result will be in your chat and Slack."
            )
        return {
            "status": "created",
            "task_id": task_id,
            "report": (
                f"Scheduled ✅ {summary}{note} "
                f"They can pause or change it any time at [Scheduled tasks]({MANAGE_LINK})."
            ),
        }

    def list_scheduled_tasks(self) -> dict:
        """List the recurring tasks this user has scheduled, with their next run times.

        Use for "what have I scheduled?", "what are my scheduled tasks?", "am I
        still getting the morning email summary?", or before changing one, to
        find its id.
        """
        from rubik_core.scheduled_tasks import store

        try:
            with self._session() as db:
                tasks = store.list_tasks(db, self.user_email)
                if not tasks:
                    return {
                        "status": "empty",
                        "count": 0,
                        "report": (
                            "You have no scheduled tasks yet. You can ask me to "
                            "schedule one — for example, 'every weekday at 8am "
                            "send me my important emails'."
                        ),
                    }
                lines = []
                items = []
                for task in tasks:
                    lines.append(f"- {self._describe(task)} (id: `{task.id}`)")
                    items.append(
                        {
                            "task_id": task.id,
                            "title": task.title,
                            "status": task.status,
                            "next_run_at": task.next_run_at.isoformat()
                            if task.next_run_at
                            else None,
                            "last_run_status": task.last_run_status,
                        }
                    )
        except Exception:  # noqa: BLE001
            logger.exception("scheduled_tasks: list failed for %s", self.user_email)
            return self._refusal("I couldn't read your scheduled tasks just now.")

        return {
            "status": "ok",
            "count": len(items),
            "tasks": items,
            "report": "Your scheduled tasks:\n" + "\n".join(lines),
        }

    def update_scheduled_task(
        self,
        task_id: str,
        time_local: str = "",
        schedule_kind: str = "",
        days_of_week: list[int] | None = None,
        day_of_month: int | None = None,
        title: str = "",
        prompt: str = "",
    ) -> dict:
        """Change the time, cadence, title or instruction of an existing scheduled task.

        Use for "make that 7:30 instead", "change it to weekdays only", "have
        it also mention my calendar". Get `task_id` from
        `list_scheduled_tasks` first — never guess one. Only the fields you
        pass are changed; leave the rest empty.

        Args:
            task_id: The task's id from `list_scheduled_tasks`.
            time_local: New 24-hour "HH:MM" local time.
            schedule_kind: New cadence — "daily", "weekdays", "weekly" or
                "monthly". Changing to "weekly" needs `days_of_week`; to
                "monthly" needs `day_of_month`.
            days_of_week: Monday=0 … Sunday=6.
            day_of_month: 1 to 31.
            title: New short label.
            prompt: New self-contained instruction (same rules as when
                creating: it runs with no conversation history).
        """
        from rubik_core.scheduled_tasks import schedule as sched
        from rubik_core.scheduled_tasks import store

        try:
            with self._session() as db:
                task = store.get_task_for_user(db, task_id, self.user_email)
                if task is None:
                    return self._refusal(
                        "I can't find that scheduled task. Check the list again."
                    )
                if task.schedule_kind == "once" and not schedule_kind:
                    # Re-timing a one-off is a new task, not an edit: its
                    # single moment may already have passed.
                    return self._refusal(
                        "That one is a one-off run. Delete it and schedule a "
                        "new one for the time you want."
                    )

                kind = (schedule_kind or task.schedule_kind).strip().lower()
                try:
                    spec = sched.validate_schedule(
                        schedule_kind=kind,
                        timezone_name=task.timezone,
                        time_local=time_local or task.time_local,
                        days_of_week=(
                            days_of_week
                            if days_of_week is not None
                            else task.days_of_week
                        ),
                        day_of_month=(
                            day_of_month
                            if day_of_month is not None
                            else task.day_of_month
                        ),
                    )
                except sched.ScheduleError as exc:
                    return self._refusal(str(exc))

                next_run = sched.next_run_utc(spec, datetime.now(timezone.utc))
                store.apply_schedule(db, task, schedule=spec, next_run_at=next_run)
                if title.strip():
                    task.title = title.strip()[:200]
                if prompt.strip():
                    task.prompt = prompt.strip()
                db.commit()
                summary = self._describe(task)
        except Exception:  # noqa: BLE001
            logger.exception("scheduled_tasks: update failed for %s", task_id)
            return self._refusal("I couldn't change that schedule just now.")

        return {"status": "updated", "report": f"Updated ✅ {summary}"}

    def pause_scheduled_task(self, task_id: str) -> dict:
        """Pause a scheduled task so it stops running until resumed.

        Use for "stop the morning summary for now", "pause that", "I'm on
        vacation". Nothing is lost — resuming picks the schedule back up. Get
        `task_id` from `list_scheduled_tasks`.
        """
        return self._set_status(task_id, "paused")

    def resume_scheduled_task(self, task_id: str) -> dict:
        """Resume a paused scheduled task from its next scheduled occurrence.

        Also the fix when a task paused itself after repeated failures. Get
        `task_id` from `list_scheduled_tasks`.
        """
        return self._set_status(task_id, "active")

    def _set_status(self, task_id: str, status: str) -> dict:
        from rubik_core.scheduled_tasks import schedule as sched
        from rubik_core.scheduled_tasks import store

        try:
            with self._session() as db:
                task = store.get_task_for_user(db, task_id, self.user_email)
                if task is None:
                    return self._refusal(
                        "I can't find that scheduled task. Check the list again."
                    )
                if task.status == "completed":
                    return self._refusal(
                        "That was a one-off run and it has already happened."
                    )
                next_run = None
                if status == "active":
                    next_run = sched.next_run_utc(
                        sched.schedule_from_task(task), datetime.now(timezone.utc)
                    )
                    if next_run is None:
                        return self._refusal(
                            "That schedule has no future runs left — schedule a "
                            "new one instead."
                        )
                store.set_status(db, task, status, next_run_at=next_run)
                db.commit()
                summary = self._describe(task)
        except Exception:  # noqa: BLE001
            logger.exception("scheduled_tasks: status change failed for %s", task_id)
            return self._refusal("I couldn't change that task just now.")

        verb = "Paused" if status == "paused" else "Resumed"
        return {"status": status, "report": f"{verb} ✅ {summary}"}

    def run_scheduled_task_now(self, task_id: str) -> dict:
        """Run a scheduled task immediately, without changing its schedule.

        Use for "run that now", "don't wait until tomorrow, do it now". The
        result arrives as its own chat conversation (and Slack DM, if that task
        uses Slack) in a minute or two — it does NOT appear in this
        conversation, so tell the user where to look. Get `task_id` from
        `list_scheduled_tasks`.
        """
        from rubik_core.scheduled_tasks import dispatch, store

        try:
            with self._session() as db:
                task = store.get_task_for_user(db, task_id, self.user_email)
                if task is None:
                    return self._refusal(
                        "I can't find that scheduled task. Check the list again."
                    )
                # Read the title INSIDE the session: the instance detaches when
                # the context manager exits, and a lazy attribute load then
                # raises rather than returning the name.
                title = task.title
                outcome = dispatch.dispatch_task_now(db, task)
        except dispatch.AlreadyRunning:
            return self._refusal(
                "That task is already running right now — the result will land "
                "in your chat shortly."
            )
        except Exception:  # noqa: BLE001
            logger.exception("scheduled_tasks: manual run failed for %s", task_id)
            return self._refusal("I couldn't start that run just now.")

        return {
            "status": "started",
            "run_id": outcome.get("run_id"),
            "report": (
                f"Running **{title}** now. The result will arrive as its own "
                "conversation in your *Scheduled tasks* chat folder in a minute "
                "or two — it won't appear here."
            ),
        }

    def delete_scheduled_task(self, task_id: str) -> dict:
        """Delete a scheduled task permanently, so it never runs again.

        Use only when the user clearly wants it GONE ("delete that", "cancel
        the morning summary for good"). For a temporary stop use
        `pause_scheduled_task` instead. Past results stay in their chat
        history. Get `task_id` from `list_scheduled_tasks`.
        """
        from rubik_core.scheduled_tasks import store

        try:
            with self._session() as db:
                task = store.get_task_for_user(db, task_id, self.user_email)
                if task is None:
                    return self._refusal(
                        "I can't find that scheduled task. Check the list again."
                    )
                title = task.title
                store.delete_task(db, task)
                db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("scheduled_tasks: delete failed for %s", task_id)
            return self._refusal("I couldn't delete that task just now.")

        return {
            "status": "deleted",
            "report": (
                f"Deleted ✅ **{title}** will not run again. Past results are "
                "still in your chat history."
            ),
        }


# The tools the coordinator arms with ADK's confirmation pause. Pinned here so
# a test can assert none of them is registered bare, rather than trusting the
# registration by eye. Create and update write the standing instruction a run
# follows later with the user's access and nobody watching, so an instruction
# planted by an email or document Cubie read must never be saved without the
# user seeing it (docs/prompt_injection_hardening.md, P3). Delete cannot be
# undone. Pause, resume and run-now act on an instruction already approved.
SCHEDULED_TASK_CONFIRM_TOOLS = (
    "create_scheduled_task",
    "update_scheduled_task",
    "delete_scheduled_task",
)

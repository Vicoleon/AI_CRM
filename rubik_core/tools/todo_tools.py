"""Chat tools for My To-Dos — "remind me to send Jordan the SOW Thursday".

Identity comes from the constructor — same contract as ``AgentTools`` and
``ScheduledTaskChatTools``: no tool signature takes an email, so the model
can only ever read or write the signed-in user's own list. The container is
built (and gated) in ``backend/services/chat_orchestrator.py`` and registered
in ``rubik_core/agent_runtime/coordinator.py`` only when that gate passed.

Every tool returns a dict with ``status`` and a human ``report`` and never
raises — a refusal is an answer the model can relay, an exception is a broken
turn.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

logger = logging.getLogger(__name__)


class TodoChatTools:
    def __init__(self, user_email: str, user_timezone: str = "UTC"):
        self.user_email = user_email
        self.user_timezone = user_timezone or "UTC"

    # ── plumbing ────────────────────────────────────────────────────────────

    def _session(self):
        """Open a short-lived session — chat tools never get the request's."""
        from rubik_core.database.db import SessionLocal

        return SessionLocal()

    def _refusal(self, message: str) -> dict[str, Any]:
        return {"status": "error", "report": message}

    def _today(self, db) -> tuple[str, date]:
        from rubik_core.promises import store

        tz = store.owner_timezone(db, self.user_email) or self.user_timezone
        return tz, store.local_today(tz)

    @staticmethod
    def _line(row) -> str:
        who = f" — for {row.counterparty_label}" if row.counterparty_label else ""
        when = f", due {row.due_local.isoformat()}" if row.due_local else ""
        flag = " (slipping)" if row.status == "slipping" else ""
        return f"- {row.title}{who}{when}{flag} [id {row.id}]"

    # ── tools ───────────────────────────────────────────────────────────────

    def create_todo(
        self, title: str, due: str = "", for_whom: str = "", detail: str = ""
    ) -> dict:
        """Add a to-do to the user's own My To-Dos list.

        Use this when the user says they need to do something, asks to be
        reminded, or commits to something in conversation: "remind me to send
        Jordan the SOW Thursday", "add a to-do: vendor list for the renewal",
        "I need to get Sam the deck by Friday". Do NOT use it for recurring
        work (that is `create_scheduled_task`) or for team EOS To-Dos.

        Args:
            title: The thing to do, as a short imperative phrase WITHOUT the
                date words or the person's name ("Send the revised SOW").
            due: When it is due — an ISO date (YYYY-MM-DD) or the user's own
                words ("Thursday", "tomorrow", "end of week"). Empty when no
                date was implied; never invent one.
            for_whom: Who is waiting for it — a colleague's name or email, or a
                company. Empty when it is just for the user.
            detail: Anything else worth keeping from what they said.

        Returns:
            dict with the created to-do and a `report` to relay, naming the
            date Cubie understood so a misheard day gets corrected now.
        """
        from rubik_core.promises import parse, store

        title = (title or "").strip()
        if not title:
            return self._refusal("Tell me what the to-do is first.")
        try:
            with self._session() as db:
                tz, today = self._today(db)
                due_local: date | None = None
                due = (due or "").strip()
                if due:
                    try:
                        due_local = date.fromisoformat(due[:10])
                    except ValueError:
                        due_local, _ = parse.resolve_due_phrase(due, today=today)
                        if due_local is None:
                            return self._refusal(
                                f"I couldn't read '{due}' as a date. Ask the user for a day "
                                "(e.g. 'Friday' or '2026-09-18') and try again."
                            )
                counterparty = parse.resolve_counterparty(db, for_whom) or (
                    {
                        "counterparty_kind": "person",
                        "counterparty_id": None,
                        "counterparty_label": for_whom.strip(),
                    }
                    if (for_whom or "").strip()
                    else {}
                )
                row = store.create_promise(
                    db,
                    owner_email=self.user_email,
                    title=title,
                    detail=(detail or "").strip() or None,
                    due_local=due_local,
                    timezone=tz,
                    source="chat",
                    actor=self.user_email,
                    **counterparty,
                )
                db.commit()
                db.refresh(row)
                who = f" for {row.counterparty_label}" if row.counterparty_label else ""
                when = (
                    f", due {row.due_local.strftime('%A %b %-d')}"
                    if row.due_local
                    else ", no date"
                )
                return {
                    "status": "created",
                    "todo_id": row.id,
                    "report": (
                        f"Added to your To-Dos: **{row.title}**{who}{when}. "
                        "It is at /todos and will show in your daily priorities until you mark it kept."
                    ),
                }
        except Exception as exc:  # noqa: BLE001 — never break the turn
            logger.exception("todo_tools.create_todo failed for %s", self.user_email)
            return self._refusal(f"Couldn't save that to-do: {exc}")

    def list_my_todos(self) -> dict:
        """List the user's open To-Dos (their own promises list), soonest first.

        Use for "what's on my list?", "what do I owe Sam?", "what's overdue?",
        and to find a to-do's id before marking it kept or snoozing it. Ids
        are UUIDs — never invent one.
        """
        from rubik_core.promises import store

        try:
            with self._session() as db:
                _, today = self._today(db)
                store.refresh_slipping(db, self.user_email, today=today)
                db.commit()
                rows = store.list_open(db, self.user_email)
                if not rows:
                    return {
                        "status": "success",
                        "count": 0,
                        "report": "Your To-Dos list is empty.",
                    }
                lines = [self._line(r) for r in rows]
                return {
                    "status": "success",
                    "count": len(rows),
                    "report": f"{len(rows)} open To-Do(s):\n" + "\n".join(lines),
                }
        except Exception as exc:  # noqa: BLE001
            logger.exception("todo_tools.list_my_todos failed for %s", self.user_email)
            return self._refusal(f"Couldn't read the list: {exc}")

    def mark_todo_kept(self, todo_id: str) -> dict:
        """Mark one of the user's To-Dos as kept (done). Needs the id from
        `list_my_todos`. Say what was marked so a wrong pick is caught."""
        from rubik_core.promises import store
        from rubik_core.promises.priority_sync import sync_promise_priority

        try:
            with self._session() as db:
                row = store.get_owned(db, self.user_email, (todo_id or "").strip())
                if row is None:
                    return self._refusal("No to-do with that id is on the user's list.")
                store.mark_kept(db, row, actor=self.user_email, via="chat")
                db.commit()
                title = row.title
                rid = row.id
            sync_promise_priority(
                promise_id=rid, closed=True, actor=self.user_email, via="kept"
            )
            return {"status": "success", "report": f"Marked kept: **{title}**."}
        except ValueError as exc:
            return self._refusal(str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("todo_tools.mark_todo_kept failed for %s", self.user_email)
            return self._refusal(f"Couldn't update it: {exc}")

    def snooze_todo(self, todo_id: str, until: str) -> dict:
        """Move one of the user's To-Dos to a later day. `until` is an ISO date
        or the user's words ("Friday", "next week"). Needs the id from
        `list_my_todos`."""
        from rubik_core.promises import parse, store

        try:
            with self._session() as db:
                row = store.get_owned(db, self.user_email, (todo_id or "").strip())
                if row is None:
                    return self._refusal("No to-do with that id is on the user's list.")
                _, today = self._today(db)
                until = (until or "").strip()
                try:
                    target = date.fromisoformat(until[:10])
                except ValueError:
                    target, _ = parse.resolve_due_phrase(until, today=today)
                if target is None:
                    return self._refusal(f"I couldn't read '{until}' as a date.")
                store.snooze(db, row, actor=self.user_email, until=target, today=today)
                db.commit()
                return {
                    "status": "success",
                    "report": f"Moved **{row.title}** to {target.strftime('%A %b %-d')}.",
                }
        except ValueError as exc:
            return self._refusal(str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("todo_tools.snooze_todo failed for %s", self.user_email)
            return self._refusal(f"Couldn't move it: {exc}")

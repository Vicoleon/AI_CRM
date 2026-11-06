"""Promises channel — the reader's own My To-Dos, as signals.

Deterministic reader over ``promises`` (rubik_core/promises): no LLM, no
external API. Emits one signal per open promise that is overdue, due within
the horizon, or undated (capped, overdue first) so the decision-maker can
weigh "you said you would send Jordan the deck by Friday" against everything
else in the reader's day.

Signal contract:

- ``entity_kind = PROMISE``, ``entity_id = promises.id`` — the dismiss / resolve
  memory keys on the PROMISE ROW, never on the thread / deal / person it is
  about (the read-filter-poisoning shape).
- ``priority_kind`` is always ``PROMISE_DUE`` (the decision-maker is told so),
  and ``rubik_core/promises/priority_sync.py`` resolves that exact key when the
  owner marks the promise kept or drops it.
- ``attrs["due_date"]`` + ``attrs["mentioned_dates"]`` give the temporal layer
  an anchor; ``counterparty_label`` and ``promise_status`` render on the card.

Silent when ``TODOS_ENABLED`` is off or the reader has no open promises.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from rubik_core.priorities.agents.base import BaseChannelAgent
from rubik_core.priorities.models import (
    ChannelDigest,
    ChannelKind,
    ChannelSignal,
    EntityKind,
    PriorityKind,
)

logger = logging.getLogger(__name__)

#: A heard promise below this extraction confidence never becomes a "you said
#: you would" card, even though it may sit in the My To-Dos suggestion tray.
UNKEPT_MIN_CONFIDENCE = 0.75


class PromisesChannelAgent(BaseChannelAgent):
    channel = ChannelKind.PROMISES

    DUE_SOON_DAYS = 7
    MAX_SIGNALS_OUT = 12

    def fetch_signals(self) -> list:
        """Two feeds, one channel:

        * the reader's open My To-Dos (``TODOS_ENABLED``) → ``promise_due``;
        * promises Cubie HEARD them make whose moment has passed with no
          evidence they were kept (``PROMISE_ACCOUNTABILITY_ENABLED`` + the
          ``promise_accountability`` roster) → ``promise_unkept``.

        A heard promise that qualifies as unkept is emitted ONLY as unkept,
        never also as due — one row, one card.
        """
        from rubik_core.config import settings

        todos_on = bool(getattr(settings, "TODOS_ENABLED", False))
        from rubik_core.promises.accountability import enrolled

        heard_on = enrolled(self.user_email)
        if not (todos_on or heard_on):
            return []
        from rubik_core.database.db import SessionLocal
        from rubik_core.promises import store

        with SessionLocal() as db:
            tz_name = store.owner_timezone(db, self.user_email)
            # The run's pinned "now" when the orchestrator gave one, so "past
            # its date" and the MAX_AGE_DAYS age-out use the same instant as
            # every other stage (and tests can pin it); wall clock otherwise.
            now = (
                self.temporal.now_utc
                if self.temporal is not None
                else datetime.now(timezone.utc)
            )
            today = store.local_today(tz_name, now)
            unkept_ids: set[str] = set()
            out: list[dict] = []
            if heard_on:
                for r in store.list_unkept_candidates(db, self.user_email):
                    if not self._is_unkept(r, today=today, now=now):
                        continue
                    unkept_ids.add(r.id)
                    out.append(self._raw(r, today, kind="unkept"))
            if todos_on:
                for r in store.list_open(db, self.user_email):
                    if r.id not in unkept_ids:
                        out.append(self._raw(r, today, kind="due"))
            return out

    @staticmethod
    def _is_unkept(row, *, today: date, now: datetime) -> bool:
        """Past its moment, checked, and the check found nothing."""
        from rubik_core.promises import evidence

        if (row.confidence or 0) < UNKEPT_MIN_CONFIDENCE:
            return False
        ev = row.evidence or {}
        # Only a promise the evidence check actually looked at — never one
        # that has not been checked yet, or whose lanes were all unreadable.
        if ev.get("verdict") not in ("not_found", "unclear") or not ev.get("checked"):
            return False
        when = evidence.said_at(row)
        if when is None or (now - when).days > evidence.MAX_AGE_DAYS:
            return False
        if row.due_local is not None:
            return row.due_local < today
        return (now - when).days >= evidence.UNDATED_GRACE_DAYS

    @staticmethod
    def _raw(r, today: date, *, kind: str) -> dict:
        # Detach plain dicts — the session closes before parse_signals.
        ref = r.source_ref or {}
        ev = r.evidence or {}
        return {
            "kind": kind,
            "id": r.id,
            "title": r.title,
            "detail": r.detail,
            "status": r.status,
            "due_local": r.due_local.isoformat() if r.due_local else None,
            "counterparty_label": r.counterparty_label,
            "counterparty_kind": r.counterparty_kind,
            "object_kind": r.object_kind,
            "object_label": r.object_label,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "today": today.isoformat(),
            "snooze_count": r.snooze_count or 0,
            "quote": ref.get("quote"),
            "said_at": ref.get("at"),
            "where": ref.get("where"),
            "source_link": ref.get("link"),
            "checked": list(ev.get("checked") or []),
            "verdict": ev.get("verdict"),
        }

    def parse_signals(self, raw_items: list) -> list[ChannelSignal]:
        scored: list[tuple[tuple, ChannelSignal]] = []
        for raw in raw_items:
            if raw.get("kind") == "unkept":
                scored.append(((-1, 0), self._unkept_signal(raw)))
                continue
            today = date.fromisoformat(raw["today"])
            due = date.fromisoformat(raw["due_local"]) if raw.get("due_local") else None
            if due is None:
                bucket = (2, 0)
                due_text = "no date"
            elif due < today:
                bucket = (0, due.toordinal())
                due_text = f"overdue (promised for {due.isoformat()})"
            elif (due - today).days <= self.DUE_SOON_DAYS:
                bucket = (1, due.toordinal())
                due_text = f"due {due.isoformat()}"
            else:
                continue  # comfortably in the future — not today's business
            who = raw.get("counterparty_label")
            bits = [f"For {who}" if who else "For yourself", due_text]
            if raw.get("object_label"):
                bits.append(f"About: {raw['object_label']}")
            if raw.get("detail"):
                bits.append(raw["detail"][:200])
            attrs = {
                "is_own": True,
                "owner_email": self.user_email,
                "due_date": raw.get("due_local"),
                "counterparty_label": who,
                "counterparty_kind": raw.get("counterparty_kind"),
                "object_kind": raw.get("object_kind"),
                "promise_status": raw["status"],
                "snooze_count": raw.get("snooze_count", 0),
                "promise_priority_kind": PriorityKind.PROMISE_DUE.value,
            }
            if raw.get("due_local"):
                attrs["mentioned_dates"] = [
                    {"raw": raw["due_local"], "resolved": raw["due_local"]}
                ]
            sig = ChannelSignal(
                id=f"promise:{raw['id']}",
                channel=self.channel,
                title=f"[Your To-Do] {raw['title']}",
                snippet=" · ".join(bits)[:400],
                link="/todos",
                timestamp=raw.get("created_at"),
                entity_kind=EntityKind.PROMISE,
                entity_id=str(raw["id"]),
                entity_label=raw["title"],
                attrs=attrs,
            )
            scored.append((bucket, sig))
        scored.sort(key=lambda pair: pair[0])
        return [sig for _, sig in scored[: self.MAX_SIGNALS_OUT]]

    def _unkept_signal(self, raw: dict) -> ChannelSignal:
        said_on = (raw.get("said_at") or raw.get("created_at") or "")[:10]
        who = raw.get("counterparty_label")
        checked = raw.get("checked") or []
        # The snippet is what the model writes the card from, and only its
        # first 200 chars reach it — so the words and the verdict come first
        # and the (long) meeting / email name lives in ``promise_where``.
        bits = []
        if raw.get("quote"):
            bits.append(f"“{raw['quote'][:100]}”")
        bits.append(f"said {said_on}" + (f" to {who}" if who else ""))
        if raw.get("due_local"):
            bits.append(f"promised for {raw['due_local']}")
        bits.append(
            "no sign it happened in " + (", ".join(checked) if checked else "activity")
        )
        attrs = {
            "is_own": True,
            "owner_email": self.user_email,
            "due_date": raw.get("due_local"),
            "counterparty_label": who,
            "promise_status": raw["status"],
            "promise_priority_kind": PriorityKind.PROMISE_UNKEPT.value,
            "promise_quote": raw.get("quote"),
            "promise_said_on": said_on or None,
            "promise_where": raw.get("where"),
            "promise_checked": ", ".join(checked) or None,
            # Where the card's "See where you said it" goes. Read from the
            # ledger, never from the model's source_links.
            "promise_link": raw.get("source_link"),
        }
        if raw.get("due_local"):
            attrs["mentioned_dates"] = [
                {"raw": raw["due_local"], "resolved": raw["due_local"]}
            ]
        return ChannelSignal(
            id=f"promise:{raw['id']}",
            channel=self.channel,
            title=f"[You said you would] {raw['title']}",
            snippet=" · ".join(bits)[:400],
            link=raw.get("source_link") or "/todos",
            timestamp=raw.get("said_at") or raw.get("created_at"),
            entity_kind=EntityKind.PROMISE,
            entity_id=str(raw["id"]),
            entity_label=raw["title"],
            attrs=attrs,
        )

    def build_prompt(self, signals: list) -> str:  # pragma: no cover — run() skips it
        raise NotImplementedError("promises channel is deterministic")

    def run(self) -> ChannelDigest:
        """Deterministic digest — no LLM call, ever."""
        try:
            raw = self.fetch_signals()
            signals = self.parse_signals(raw)
        except Exception as exc:  # noqa: BLE001 — pipeline must stay alive
            logger.warning("promises channel failed for %s: %s", self.user_email, exc)
            return ChannelDigest(channel=self.channel, error=f"fetch_failed: {exc}")
        if not signals:
            return ChannelDigest(channel=self.channel, summary="")
        overdue = sum(
            1
            for s in signals
            if s.attrs.get("due_date")
            and date.fromisoformat(s.attrs["due_date"]) < datetime.utcnow().date()
        )
        unkept = sum(
            1
            for s in signals
            if s.attrs.get("promise_priority_kind") == PriorityKind.PROMISE_UNKEPT.value
        )
        if unkept:
            summary_head = (
                f"{unkept} promise(s) the reader made with no sign they were kept"
            )
        else:
            summary_head = ""
        summary = f"{len(signals)} open To-Do(s) of the reader's own"
        if overdue:
            summary += f", {overdue} past the promised date"
        if summary_head:
            summary = summary_head + "; " + summary
        return ChannelDigest(
            channel=self.channel, summary=summary + ".", signals=signals
        )

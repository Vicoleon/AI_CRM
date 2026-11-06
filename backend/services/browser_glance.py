"""The new tab's payload for Cubie for Chrome — one read, no model calls.

The extension asks for this every 15 minutes and whenever a new tab finds its
copy older than ten. It must therefore never cost anything:

* **Priorities** come from this morning's stored card through the home
  router's own read path (``_load_today_priorities``), which applies the
  done / dismissed / snoozed / deferred filters but NEVER starts a run. The
  home page's ``POST /api/homefeed/priorities`` does start the 3–5 minute
  pipeline on a miss; calling that from a new tab would multiply the largest
  line on the AI bill by the number of tabs people open. A miss here is
  reported as ``not_ready`` and the tab links to Cubie, whose home page makes
  the card the normal way.
* **Promises not kept** are read in place from the ledgers that already hold
  them: the person's overdue (or due today) EOS To-Dos, their My To-Dos ledger
  when they have it, and the "You said you would…" cards Cubie put in this
  morning's priorities. A promise in two of those shows once.

Each section fails soft to empty, like ``rubik_core/promises/readthrough.py``,
so one broken table never blanks the tab.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from rubik_core.brand import PRODUCT_NAME
from rubik_core.config import settings
from rubik_core.database.db import SessionLocal
from rubik_core.database.schemas import UserProfile
from rubik_core.features import feature_allows
from rubik_core.priorities.models import is_eos_priority_card
from rubik_core.priorities.temporal import user_today_str

logger = logging.getLogger(__name__)

MAX_PRIORITIES = 5
MAX_PROMISES = 6
WHY_MAX = 160

# The kinds whose "done" the extension can send, mapped by the router.
KIND_EOS_TODO = "eos_todo"
KIND_PROMISE = "promise"
KIND_PRIORITY = "priority"

# Promise card kinds (rubik_core/priorities/models.py, PriorityKind).
_PROMISE_UNKEPT = "promise_unkept"


def _first_name(user: UserProfile) -> str:
    name = (getattr(user, "name", "") or "").strip()
    if name and "@" not in name:
        return name.split()[0]
    return (user.email or "").split("@")[0].split(".")[0].capitalize()


def _days_late(due: str | None, today: date) -> int | None:
    if not due:
        return None
    try:
        return (today - date.fromisoformat(due[:10])).days
    except ValueError:
        return None


def _clip(text: str | None, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _card_href(p: dict[str, Any], *, has_todos: bool) -> str:
    """Where a priority card opens — the same place the home panel sends it."""
    ek = p.get("entity_kind")
    eid = p.get("entity_id")
    if ek == "event" and eid:
        return f"/meeting-prep/{eid}"
    if ek == "promise":
        link = (p.get("attrs") or {}).get("promise_link")
        if p.get("priority_kind") == _PROMISE_UNKEPT and isinstance(link, str) and link:
            return link
        return "/todos" if has_todos else "/"
    if ek == "todo":
        return "/eos/todos"
    return "/"


def _card_can_done(p: dict[str, Any]) -> bool:
    # The resolve route refuses a card that is not entity-tagged (pre-UC-24).
    return bool(p.get("entity_kind") and p.get("entity_id") and p.get("priority_kind"))


def _covered_ids(p: dict[str, Any]) -> set[str]:
    ids = {str(i) for i in (p.get("covered_entity_ids") or []) if i}
    if p.get("entity_id"):
        ids.add(str(p["entity_id"]))
    return ids


def _eos_promises(db, email: str, today: date) -> list[dict[str, Any]]:
    from rubik_core.control.repository import module_enabled
    from rubik_core.promises import readthrough

    if not module_enabled("eos"):
        return []
    out = []
    for t in readthrough.eos_todos_for(db, email):
        late = _days_late(t.get("due_local"), today)
        if late is None or late < 0:
            continue
        out.append(
            {
                "kind": KIND_EOS_TODO,
                "id": t["id"],
                "title": t.get("title") or "",
                "to": t.get("team") or "",
                "source": "L10 To-Do",
                "due": t.get("due_local"),
                "days_late": late,
                "overdue": late > 0,
                "href": "/eos/todos",
            }
        )
    return out


def _ledger_promises(db, email: str, today: date) -> list[dict[str, Any]]:
    from rubik_core.promises import store

    out = []
    for row in store.list_for_owner(db, email):
        if row.status not in ("open", "slipping") or not row.due_local:
            continue
        due = row.due_local.isoformat()
        late = _days_late(due, today)
        if late is None or late < 0:
            continue
        out.append(
            {
                "kind": KIND_PROMISE,
                "id": row.id,
                "title": row.title or "",
                "to": row.counterparty_label or "",
                "source": "My To-Dos",
                "due": due,
                "days_late": late,
                "overdue": late > 0,
                "href": "/todos",
            }
        )
    return out


def _load_bundle(email: str) -> dict[str, Any] | None:
    # Imported here: the home router is large and this module is imported by a
    # router that loads on every boot.
    from backend.routers.home import _load_today_priorities

    return _load_today_priorities(email)


def build_glance(user: UserProfile) -> dict[str, Any]:
    from rubik_core.control.repository import module_enabled

    email = (user.email or "").strip().lower()
    today_str = user_today_str(email)
    today = date.fromisoformat(today_str)
    has_copilot = feature_allows("copilot", user)
    has_todos = bool(settings.TODOS_ENABLED) and feature_allows("todos", user)
    try:
        has_eos = module_enabled("eos")
    except Exception:  # noqa: BLE001 — a broken sibling must not blank the tab
        logger.warning("glance: EOS module lookup failed for %s", email, exc_info=True)
        has_eos = False

    promises: list[dict[str, Any]] = []
    with SessionLocal() as db:
        try:
            if has_eos:
                promises.extend(_eos_promises(db, email, today))
        except Exception:  # noqa: BLE001 — one ledger never blanks the tab
            logger.warning("glance: EOS to-dos failed for %s", email, exc_info=True)
        if has_todos:
            try:
                promises.extend(_ledger_promises(db, email, today))
            except Exception:  # noqa: BLE001
                logger.warning("glance: ledger failed for %s", email, exc_info=True)

    status = "not_included"
    headline = ""
    as_of = None
    generated_count = 0
    cards: list[dict[str, Any]] = []
    if has_copilot:
        bundle = None
        try:
            bundle = _load_bundle(email)
        except Exception:  # noqa: BLE001
            logger.warning("glance: priorities failed for %s", email, exc_info=True)
            status = "unavailable"
        if bundle is not None:
            status = "ready"
            headline = _clip(bundle.get("headline"), 280)
            as_of = bundle.get("generated_at") or None
            generated_count = int(bundle.get("generated_count") or 0)
            cards = [
                p
                for p in bundle.get("priorities") or []
                if p.get("origin") not in ("next_week", "recommendation")
                and (
                    has_eos
                    or not is_eos_priority_card(p)
                    and not _card_href(p, has_todos=has_todos).startswith("/eos/")
                )
            ]
        elif status != "unavailable":
            status = "not_ready"

    # A promise card ("You said you would…", "Your To-Do") belongs in the
    # promises list, not in Today — unless the ledger row is already there.
    shown_promise_ids = {p["id"] for p in promises if p["kind"] == KIND_PROMISE}
    shown_todo_ids = {p["id"] for p in promises if p["kind"] == KIND_EOS_TODO}
    today_cards: list[dict[str, Any]] = []
    for p in cards:
        ek = p.get("entity_kind")
        if ek == "promise":
            if _covered_ids(p) & shown_promise_ids:
                continue
            unkept = p.get("priority_kind") == _PROMISE_UNKEPT
            promises.append(
                {
                    "kind": KIND_PRIORITY,
                    "id": p.get("id") or "",
                    "title": p.get("title") or "",
                    "to": p.get("target_label") or p.get("entity_label") or "",
                    "source": f"Heard by {PRODUCT_NAME}" if unkept else "My To-Dos",
                    "due": None,
                    "days_late": None,
                    "overdue": unkept,
                    "href": _card_href(p, has_todos=has_todos),
                    "can_done": _card_can_done(p),
                }
            )
            continue
        if ek == "todo" and _covered_ids(p) & shown_todo_ids:
            continue
        today_cards.append(p)

    for item in promises:
        item.setdefault("can_done", True)
    # Longest overdue first; then due today; then the heard ones with no date.
    promises.sort(
        key=lambda i: (
            not i["overdue"],
            i["days_late"] is None,
            -(i["days_late"] or 0),
            i["title"].lower(),
        )
    )

    priorities = [
        {
            "id": p.get("id") or "",
            "title": p.get("title") or "",
            "why": _clip(p.get("why_it_matters"), WHY_MAX),
            "urgency": p.get("urgency") or "medium",
            "href": _card_href(p, has_todos=has_todos),
            "can_done": _card_can_done(p),
        }
        for p in today_cards[:MAX_PRIORITIES]
    ]

    return {
        "today": today_str,
        "user": {"first_name": _first_name(user)},
        "can_ask": has_copilot,
        "headline": headline,
        "priorities": {
            "status": status,
            "as_of": as_of,
            "generated_count": generated_count,
            "items": priorities,
            "more": max(0, len(today_cards) - MAX_PRIORITIES),
        },
        "promises": {
            "items": promises[:MAX_PROMISES],
            "more": max(0, len(promises) - MAX_PROMISES),
        },
        "counts": {"overdue": sum(1 for p in promises if p["overdue"])},
    }

"""The other ledgers this person already owes into, read in place.

The My To-Dos page shows one list across three systems of record — promises
(the only new writer), EOS To-Dos and Sales Cockpit commitments. Nothing here
copies a row; each system keeps its own truth and its own verbs. Every function
fails soft to an empty list so a broken sibling table never blanks the page.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _fold(email: str) -> str:
    return (email or "").strip().lower()


def _iso_date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    return str(value)[:10]


def eos_todos_for(db: Session, owner_email: str) -> list[dict[str, Any]]:
    """The owner's open EOS To-Dos — team work assigned to them in Traction."""
    try:
        from rubik_core.control.repository import module_enabled

        if not module_enabled("eos"):
            return []
        from rubik_core.database.eos._base import TodoStatus
        from rubik_core.database.eos.people import Person
        from rubik_core.database.eos.todos import Todo

        person = (
            db.query(Person)
            .filter(
                func.lower(Person.email) == _fold(owner_email),
                Person.is_deleted.is_(False),
            )
            .first()
        )
        if person is None:
            return []
        rows = (
            db.query(Todo)
            .filter(
                Todo.owner_id == person.id,
                Todo.status == TodoStatus.PENDING,
                Todo.is_deleted.is_(False),
            )
            .all()
        )
        out = []
        for t in rows:
            team = (
                "Executive team" if t.is_exec else (t.department_name or "Company-wide")
            )
            out.append(
                {
                    "id": str(t.id),
                    "title": t.description,
                    "due_local": _iso_date(t.due_date),
                    "team": team,
                    "l10_meeting_id": str(t.l10_meeting_id)
                    if t.l10_meeting_id
                    else None,
                    "link": "/eos/todos",
                }
            )
        out.sort(key=lambda r: (r["due_local"] is None, r["due_local"] or ""))
        return out
    except Exception:  # noqa: BLE001 — a sibling ledger must never blank the page
        logger.warning(
            "promises: EOS read-through failed for %s", owner_email, exc_info=True
        )
        return []


def commitments_for(db: Session, owner_email: str) -> list[dict[str, Any]]:
    """The AE's open Sales Cockpit commitments (SOW / wrap-up / lead touch).

    A deal commitment on a deal the AE deferred is left out until the follow-up
    date, exactly as on My Day's Deals block; wrap-ups and lead touches stay,
    as they do there (``rubik_core/sales/deal_defer.py``).
    """
    try:
        from rubik_core.database.commitments import Commitment
        from rubik_core.sales.deal_defer import deferred_deal_ids

        rows = (
            db.query(Commitment)
            .filter(
                Commitment.owner_email == _fold(owner_email),
                Commitment.closed_at.is_(None),
            )
            .all()
        )
        own_rows = ("wrapup", "lead_touch")
        hidden = deferred_deal_ids(
            {
                str(c.hubspot_deal_id)
                for c in rows
                if c.hubspot_deal_id and c.kind not in own_rows
            },
            reader=_fold(owner_email),
            db=db,
        )
        rows = [
            c
            for c in rows
            if not (
                c.hubspot_deal_id
                and c.kind not in own_rows
                and str(c.hubspot_deal_id) in hidden
            )
        ]
        labels = {
            "sow": "Send the SOW",
            "wrapup": "Call wrap-up",
            "lead_touch": "Touch the lead",
        }
        out = []
        for c in rows:
            out.append(
                {
                    "id": c.id,
                    "kind": c.kind,
                    "title": labels.get(c.kind, c.kind.replace("_", " ").title()),
                    "state": c.state,
                    "hubspot_deal_id": c.hubspot_deal_id,
                    "hubspot_company_id": c.hubspot_company_id,
                    "promised_at": _iso_date(c.promised_at),
                    "breach_at": _iso_date(c.breach_at),
                    "link": "/sales/my-day",
                }
            )
        out.sort(key=lambda r: (r["breach_at"] is None, r["breach_at"] or ""))
        return out
    except Exception:  # noqa: BLE001
        logger.warning(
            "promises: commitments read-through failed for %s",
            owner_email,
            exc_info=True,
        )
        return []

"""Privacy boundary for owner-only priorities built with personal Gmail.

An entire run is private when personal mail was in the model context. A card
that happens to cite only work mail can still contain facts from that context.
Readers outside the owner must therefore inspect the stored bundle before
using any card or its action history.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy.orm import Session


def is_private_gmail_bundle(metadata: object) -> bool:
    """Recognize owner-only runs; malformed provenance is private to its owner."""
    if not isinstance(metadata, dict):
        return True
    if any(
        marker in metadata and metadata[marker] is not False
        for marker in ("private_gmail_context", "personal_gmail")
    ):
        return True
    priorities = metadata.get("priorities")
    if not isinstance(priorities, list):
        return True
    for priority in priorities:
        if not isinstance(priority, dict):
            return True
        if any(
            marker in priority and priority[marker] is not False
            for marker in ("private_gmail_context", "personal_gmail")
        ):
            return True
        attrs = priority.get("attrs")
        if "attrs" in priority and not isinstance(attrs, dict):
            return True
        if isinstance(attrs, dict) and any(
            marker in attrs and attrs[marker] is not False
            for marker in ("private_gmail_context", "personal_gmail")
        ):
            return True
    return False


def public_priority_ids(db: Session, user_email: str) -> set[str]:
    """Only priority IDs proven to come from this owner's public briefings.

    Unknown and malformed historical rows are absent. If an ID was ever in a
    private bundle, that classification wins over a duplicate public row.
    Database errors deliberately propagate so callers can fail closed.
    """
    normalized = user_email.strip().lower()
    return public_priority_ids_for_users(db, (normalized,)).get(normalized, set())


def public_priority_ids_for_users(
    db: Session, user_emails: Iterable[str]
) -> dict[str, set[str]]:
    """Bulk version for shared reports so the roster needs one card scan."""
    from rubik_core.database.models import HomeCard

    wanted = frozenset(
        email.strip().lower() for email in user_emails if isinstance(email, str)
    )
    if not wanted:
        return {}
    public: dict[str, set[str]] = {email: set() for email in wanted}
    private: dict[str, set[str]] = {email: set() for email in wanted}
    names = sorted(wanted)
    for start in range(0, len(names), 500):
        rows = (
            db.query(HomeCard.user_email, HomeCard.metadata_json)
            .filter(
                HomeCard.user_email.in_(names[start : start + 500]),
                HomeCard.category.in_(("daily_briefing", "next_week_report")),
            )
            .yield_per(200)
        )
        for email, metadata in rows:
            if not isinstance(metadata, dict):
                continue
            priorities: Any = metadata.get("priorities")
            if not isinstance(priorities, list):
                continue
            ids = {
                card["id"]
                for card in priorities
                if isinstance(card, dict)
                and isinstance(card.get("id"), str)
                and card["id"]
            }
            if is_private_gmail_bundle(metadata):
                private[email].update(ids)
            else:
                public[email].update(ids)
    return {email: public[email] - private[email] for email in wanted}

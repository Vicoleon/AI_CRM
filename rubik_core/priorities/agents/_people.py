"""Turn email addresses into the names a human would recognize.

The AI-Priorities extractors deal in email addresses, but a briefing that says
"your 1:1 with bellison@acme.example" is unreadable — and a briefing that
guesses the name out of the event *title* gets it wrong. The failure this
exists to prevent: an internal 1:1 titled "Alex / Blair" produced
"Prepare for your 1:1 with Alex" **for Alex**, because the extractor dropped
every internal attendee and the model had nothing but the title to go on.

So: resolve names from our own ``users`` table (the same source the meeting-prep
attendee resolver uses), fall back to Google's ``displayName``, then to the
email's local part. Every helper here is best-effort — a DB hiccup degrades to
email addresses, it never breaks a run.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

logger = logging.getLogger(__name__)


def resolve_display_names(emails: Iterable[str]) -> dict[str, str]:
    """Map lower-cased email → full name from the ``users`` table.

    Returns ``{}`` on any failure (and for addresses we have no row for), so
    callers must always keep a fallback.
    """
    wanted = {e.strip().lower() for e in emails if e and e.strip()}
    if not wanted:
        return {}
    try:
        from rubik_core.database.db import SessionLocal
        from rubik_core.database.models import User

        with SessionLocal() as db:
            rows = db.query(User.email, User.name).filter(User.email.in_(wanted)).all()
            return {
                (email or "").lower(): name.strip()
                for email, name in rows
                if email and name and name.strip()
            }
    except Exception:  # noqa: BLE001 — a name is never worth failing a run over
        logger.warning("display-name resolution failed", exc_info=True)
        return {}


def _humanize_local_part(email: str) -> str:
    """Last-resort label from an address: "bellison@x.com" -> "bellison"."""
    return (email or "").split("@", 1)[0] or (email or "")


def person_label(
    email: str,
    name_by_email: dict[str, str],
    *,
    google_display_name: str | None = None,
) -> str:
    """Best available human label for ``email``.

    Priority: our ``users`` table > Google's calendar ``displayName`` > the
    email's local part. Google's display name is often a bare username
    ("jdoe"), which is why our own directory wins.
    """
    key = (email or "").strip().lower()
    resolved = name_by_email.get(key)
    if resolved:
        return resolved
    gdn = (google_display_name or "").strip()
    # A displayName that is just the address again adds nothing.
    if gdn and gdn.lower() != key:
        return gdn
    return _humanize_local_part(key)

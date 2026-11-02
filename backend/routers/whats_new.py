"""What's New — the drawer's feed of Cubie release notes, and the opened count.

GET  /api/whats-new                — published notes this person may see
POST /api/whats-new/{entry_id}/open — count one open (204)

The notes come from the Cubie changelog (spec §4.6.1) through
:func:`~rubik_core.whats_new.changelog.get_changelog_source`, a FastAPI
dependency here. Phase 1's source is empty, so the feed is ``{"items": []}``
and the drawer says "No updates yet." A note about a feature reaches a person
only while that feature runs here and Feature Access lets them use it
(``rubik_core/whats_new/audience.py``); a key the registry no longer knows
hides the note, never a crash.

Company-wide: mounted on ``app`` with ``prefix="/api"``, NOT on ``core``. Its
own gate, :func:`require_whats_new`, sits on the ``APIRouter`` so no endpoint
can be added without it: ``WHATS_NEW_ENABLED`` off answers 404 ("What's New is
not switched on."), off the Feature Access roster answers 403.

Opens are counted as people, never who (``rubik_core/whats_new/stats.py``);
nothing here returns a viewer key or the email of someone who opened a note.
Contract: ``docs/whats_new/README.md``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.auth import get_current_user
from rubik_core.config import settings
from rubik_core.database.db import get_db
from rubik_core.database.schemas import UserProfile
from rubik_core.features import feature_allows
from rubik_core.logsafe import safe_error
from rubik_core.whats_new import audience, stats
from rubik_core.whats_new import settings as wn_settings
from rubik_core.whats_new.changelog import (
    ChangelogEntry,
    ChangelogSource,
    app_path,
    get_changelog_source,
    is_entry_id,
)

logger = logging.getLogger(__name__)

#: The kill switch's 404. The client tells it apart from a note's 404 by text.
NOT_SWITCHED_ON = "What's New is not switched on."
#: A note that is not published, not for this person, or gone.
NO_LONGER_AVAILABLE = "This update is no longer available."

#: Most notes the drawer shows.
FEED_LIMIT = 60


def require_whats_new(user: UserProfile = Depends(get_current_user)) -> UserProfile:
    """Gate for the whole router — 404 when switched off, 403 when not on it."""
    if not settings.WHATS_NEW_ENABLED:
        raise HTTPException(status_code=404, detail=NOT_SWITCHED_ON)
    if not feature_allows("whats_new", user):
        raise HTTPException(
            status_code=403,
            detail=(
                "What's New isn't open to your team. Ask an admin to add it in "
                "Admin → Feature Access."
            ),
        )
    return user


router = APIRouter(
    prefix="/whats-new",
    tags=["whats-new"],
    dependencies=[Depends(require_whats_new)],
)


class OpenIn(BaseModel):
    source: Literal["slack", "try_it"]


def _visible(source: ChangelogSource, user: UserProfile) -> list[ChangelogEntry]:
    """The notes ``user`` may see now, newest first. A source that fails reads
    as no notes: the drawer must never take the page down with it."""
    try:
        entries = source.list_published()
    except Exception as exc:  # noqa: BLE001 — an empty drawer, not a 500
        logger.warning("[whats-new] changelog unreadable: %s", safe_error(exc))
        return []
    now = datetime.utcnow()
    verdicts: dict[str | None, bool] = {}
    out: list[ChangelogEntry] = []
    for entry in entries:
        if entry.published_at > now:
            continue
        key = entry.feature_key or None
        if key not in verdicts:
            verdicts[key] = audience.can_see(entry, user)
        if verdicts[key]:
            out.append(entry)
    out.sort(key=lambda e: (e.published_at, e.id), reverse=True)
    return out


def _entry_view(entry: ChangelogEntry) -> dict[str, Any]:
    """One note, as the drawer shows it (naive UTC, the house wire format)."""
    return {
        "id": entry.id,
        "title": entry.title,
        "body": entry.body,
        "path": app_path(entry.path),
        "feature_label": audience.feature_label(entry.feature_key),
        "published_at": entry.published_at.isoformat(),
    }


@router.get("")
def get_feed(
    item: str | None = Query(None, max_length=64),
    user: UserProfile = Depends(require_whats_new),
    source: ChangelogSource = Depends(get_changelog_source),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """The published notes this person may see, newest first, and the tease
    switch. ``item`` (a Slack link's note) is added when it is older than the
    newest :data:`FEED_LIMIT` but this person may still see it, so an old link
    opens on its note; a note still missing is for another team or gone, and
    the answer never says which."""
    visible = _visible(source, user)
    items = visible[:FEED_LIMIT]
    if item is not None and all(entry.id != item for entry in items):
        items += [entry for entry in visible[FEED_LIMIT:] if entry.id == item][:1]
    return {
        "items": [_entry_view(entry) for entry in items],
        "tease_enabled": bool(wn_settings.load(db).get("tease_enabled")),
    }


@router.post("/{entry_id}/open", status_code=204)
def record_open(
    entry_id: str,
    payload: OpenIn,
    request: Request,
    user: UserProfile = Depends(require_whats_new),
    source: ChangelogSource = Depends(get_changelog_source),
    db: Session = Depends(get_db),
) -> Response:
    """Count one person arriving at a note: from the channel post's link
    (``slack``) or the drawer's "Try it" (``try_it``). Once per person per
    note per source (the server dedups, so a double fire — React StrictMode —
    still counts once). Stored as a keyed hash and the department at the time,
    never the email. An admin looking as someone else is never counted."""
    if not is_entry_id(entry_id) or not any(
        entry.id == entry_id for entry in _visible(source, user)
    ):
        raise HTTPException(status_code=404, detail=NO_LONGER_AVAILABLE)
    if getattr(request.state, "actor_impersonating", False):
        return Response(status_code=204)
    email = getattr(request.state, "actor_email", None) or user.email or ""
    department = getattr(request.state, "actor_department", None)
    if department is None:
        department = user.department or ""
    try:
        stats.record_open(
            db, entry_id, email=email, department=department, source=payload.source
        )
    except Exception as exc:  # noqa: BLE001 — a lost count never breaks the link
        db.rollback()
        logger.warning(
            "[whats-new] open of %s not counted: %s", entry_id, safe_error(exc)
        )
    return Response(status_code=204)

"""Admin → What's New: the morning post's switch and channel, and the tease.

Spec §4.6.1: each workspace opts into a digest of new release notes. In
Phase 1 that digest is one Slack post (``digest.py``); Phase 3 moves it onto
the workspace's messaging connector. An admin changes all of it on the
Admin → What's New tab, never in code. Stored in the existing
``workspace_settings`` key-value table, so there is no migration, under TWO
keys that never write each other:

* :data:`SETTINGS_KEY` (``"whats_new"``) — the admin's choices. Only
  :func:`save` writes it (plus what a channel post learns about the bot,
  through :func:`_record`, under a row lock and only for the same channel).
  Keys an older release stored (the approvers, the approver DMs) are ignored.
* :data:`DIGEST_KEY` (``"whats_new_digest"``) — the morning post's mark: the
  last release note it carried. Only the post writes it.

Rules this module keeps (the ``loop_closure/digest_settings.py`` pattern):

* **Fail closed.** :func:`load` never raises. A read failure returns the
  defaults with posting OFF: a database hiccup must never post to a channel.
* **On means it can actually post.** :func:`save` resolves the channel with
  the Slack bot. Switching posting ON needs a channel Slack knows, not
  archived, with the bot in it; every refusal is a ``ValueError`` carrying the
  sentence the admin reads (the router returns it as the 400 ``detail``).
  Saving with posting OFF always works and stores the typed channel.

``rubik_core`` does not import ``backend`` at module scope (the worker image
loads this package without FastAPI), so the Slack client is imported inside
:func:`save`.
"""

from __future__ import annotations

import copy
import logging
import re
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from rubik_core.brand import PRODUCT_NAME
from rubik_core.logsafe import safe_error
from rubik_core.whats_new.changelog import is_entry_id

logger = logging.getLogger(__name__)

SETTINGS_KEY = "whats_new"
DIGEST_KEY = "whats_new_digest"

DEFAULT_CHANNEL = "#announcements"

#: What a fresh install reads. Posting ships OFF until an admin has created
#: the channel, invited the bot and switched it on.
DEFAULTS: dict[str, Any] = {
    "slack_enabled": False,
    "channel_name": DEFAULT_CHANNEL,
    "channel_id": None,
    "bot_in_channel": None,
    "channel_archived": False,
    "tease_enabled": True,
    "updated_by": None,
    "updated_at": None,
}

#: A Slack channel id: public ``C…`` or legacy private ``G…``, upper-case.
_CHANNEL_ID = re.compile(r"[CG][A-Z0-9]{6,}")
#: A Slack channel name: lower-case letters, digits, ``-`` and ``_``, ≤ 80.
_CHANNEL_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,79}")


def _fold(email: Any) -> str:
    return email.strip().lower() if isinstance(email, str) else ""


def _defaults() -> dict[str, Any]:
    return copy.deepcopy(DEFAULTS)


def _clean(stored: Any) -> dict[str, Any]:
    """The stored blob merged onto the defaults, every field type-checked.

    ``slack_enabled`` is True only for a literal ``True`` with a channel id
    beside it: a half-written or hand-edited row reads as OFF.
    """
    out = _defaults()
    if not isinstance(stored, dict):
        return out
    channel_id = stored.get("channel_id")
    if isinstance(channel_id, str) and _CHANNEL_ID.fullmatch(channel_id.strip()):
        out["channel_id"] = channel_id.strip()
    name = stored.get("channel_name")
    if isinstance(name, str) and name.strip().lstrip("#").strip():
        out["channel_name"] = "#" + name.strip().lstrip("#").strip()
    if isinstance(stored.get("bot_in_channel"), bool):
        out["bot_in_channel"] = stored["bot_in_channel"]
    for key in ("channel_archived", "tease_enabled"):
        if isinstance(stored.get(key), bool):
            out[key] = stored[key]
    for key in ("updated_by", "updated_at"):
        if isinstance(stored.get(key), str) and stored[key]:
            out[key] = stored[key]
    out["slack_enabled"] = stored.get("slack_enabled") is True and bool(
        out["channel_id"]
    )
    return out


def _row(db: Session, key: str, *, lock: bool = False):
    from rubik_core.database.models import WorkspaceSetting

    query = db.query(WorkspaceSetting).filter(WorkspaceSetting.key == key)
    if lock:
        query = query.with_for_update()
    return query.first()


def _rollback_quietly(db: Session | None) -> None:
    if db is None:
        return
    try:
        db.rollback()
    except Exception:  # noqa: BLE001
        pass


def load(db: Session | None = None) -> dict[str, Any]:
    """The admin settings, merged onto the defaults. NEVER raises.

    Any failure returns the defaults with posting off. With ``db`` given the
    read runs on the caller's session, and a failed read rolls that session
    back, so call it before writing anything you mean to keep. Without ``db``
    it opens its own session.
    """
    try:
        if db is not None:
            row = _row(db, SETTINGS_KEY)
            return _clean(row.value if row is not None else None)
        # Through the module attribute, so a test that swaps SessionLocal lands.
        from rubik_core.database import db as database

        with database.SessionLocal() as own:
            row = _row(own, SETTINGS_KEY)
            return _clean(row.value if row is not None else None)
    except Exception as exc:  # noqa: BLE001 — fail closed
        logger.warning(
            "[whats-new] settings unreadable; posting stays off: %s", safe_error(exc)
        )
        _rollback_quietly(db)
        return _defaults()


# ── the channel ──────────────────────────────────────────────────────────────


def _looks_like_id(raw: str, bare: str) -> bool:
    """A channel id is typed bare (Slack never shows one with a ``#``) and has
    a digit in it, so ``#GROWTH1`` stays a channel name."""
    return (
        not raw.startswith("#")
        and bool(_CHANNEL_ID.fullmatch(bare))
        and any(c.isdigit() for c in bare)
    )


def normalise_channel(channel: str) -> tuple[str, str | None]:
    """``"#Announcements"`` → ``("announcements", None)``; ``"C0123ABCD"`` →
    ``(id, id)``. Raises ``ValueError`` (a sentence for the admin) for an
    empty or impossible channel name."""
    raw = (channel or "").strip()
    bare = raw.lstrip("#").strip()
    if not bare:
        raise ValueError(f"Type the channel to post to, for example {DEFAULT_CHANNEL}.")
    if _looks_like_id(raw, bare):
        return bare, bare
    name = bare.lower()
    if not _CHANNEL_NAME.fullmatch(name):
        raise ValueError(
            f"“{raw}” isn't a Slack channel name. Use the name as Slack shows it, "
            f"for example {DEFAULT_CHANNEL} (lower-case letters, numbers, - and _ "
            "only)."
        )
    return name, None


def _find_channel(
    bot: Any, key: str, typed_id: str | None, *, slack_enabled: bool, display: str
) -> tuple[dict[str, Any] | None, bool]:
    """Ask Slack for the channel: ``(found, looked)``. ``looked`` is False when
    the bot is not there to ask. A Slack error refuses turning posting on."""
    if not getattr(bot, "is_available", False):
        if slack_enabled:
            raise ValueError(
                f"{PRODUCT_NAME} isn't connected to Slack, so it can't post. Install "
                f"the {PRODUCT_NAME} Slack app in the workspace first, then turn this on."
            )
        return None, False
    try:
        found = bot.find_channel(key)
        if found is None and typed_id is not None:
            # Something id-shaped Slack has no id for may still be a name
            # typed in capitals: try it as one before giving up.
            as_name = typed_id.lower()
            if _CHANNEL_NAME.fullmatch(as_name):
                found = bot.find_channel(as_name)
        return found, True
    except Exception as exc:  # noqa: BLE001 — a Slack error is a sentence
        logger.warning("[whats-new] channel lookup failed: %s", safe_error(exc))
        if slack_enabled:
            raise ValueError(
                f"{PRODUCT_NAME} couldn't check {display} with Slack just now, so "
                "posting was not turned on. Try again in a minute."
            ) from exc
        return None, False


def save(
    db: Session,
    *,
    slack_enabled: bool,
    channel: str,
    tease_enabled: bool,
    actor_email: str,
    bot: Any = None,
) -> dict[str, Any]:
    """Check the channel with the Slack bot, then store.

    Returns the stored settings (the :func:`load` shape). Raises
    ``ValueError`` with the sentence the admin reads when the change cannot be
    saved; nothing is stored then.
    """
    current = load(db)

    typed = (channel or "").strip()
    if not typed and not slack_enabled:
        # Posting off and the field cleared: keep the channel already stored.
        typed = current.get("channel_id") or current.get("channel_name") or ""
    key, typed_id = normalise_channel(typed)
    display = f"#{key}" if typed_id is None else typed_id

    if bot is None:
        from backend.services.slack_service import SlackBotClient

        bot = SlackBotClient()
    found, looked = _find_channel(
        bot, key, typed_id, slack_enabled=slack_enabled, display=display
    )

    same = key in {
        (current.get("channel_id") or "").strip(),
        (current.get("channel_name") or "").lstrip("#").lower(),
    }
    if found is not None:
        channel_id = found.get("id") or typed_id
        channel_name = "#" + (found.get("name") or key)
        bot_in_channel: bool | None = bool(found.get("is_member"))
        archived = bool(found.get("is_archived"))
    elif slack_enabled:
        raise ValueError(
            f"{PRODUCT_NAME} can't find {display}. Create it in Slack, then save again."
        )
    elif same:
        channel_id = current.get("channel_id") or typed_id
        channel_name = current.get("channel_name") or display
        archived = bool(current.get("channel_archived"))
        bot_in_channel = False if looked else current.get("bot_in_channel")
    else:
        channel_id = typed_id
        channel_name = display
        archived = False
        bot_in_channel = False if looked else None

    if slack_enabled and archived:
        raise ValueError(
            f"Slack says {channel_name} is archived, so {PRODUCT_NAME} can't post there. "
            "Pick another channel, then turn this on."
        )
    if slack_enabled and not bot_in_channel:
        raise ValueError(
            f"Invite {PRODUCT_NAME} to {channel_name} first: type "
            f"/invite @{PRODUCT_NAME} in that channel."
        )

    blob = {
        "slack_enabled": bool(slack_enabled),
        "channel_name": channel_name,
        "channel_id": channel_id,
        "bot_in_channel": bot_in_channel,
        "channel_archived": archived,
        "tease_enabled": bool(tease_enabled),
        "updated_by": _fold(actor_email) or None,
        "updated_at": datetime.utcnow().isoformat(timespec="seconds"),
    }

    from rubik_core.database.models import WorkspaceSetting

    row = _row(db, SETTINGS_KEY)
    if row is None:
        db.add(WorkspaceSetting(key=SETTINGS_KEY, value=blob))
    else:
        row.value = blob  # a new dict, so the JSON column sees the change
    db.commit()
    logger.info(
        "[whats-new] settings saved (slack=%s, channel=%s)",
        blob["slack_enabled"],
        blob["channel_id"] or blob["channel_name"],
    )
    return _clean(blob)


def _record(db: Session, channel_id: str, changes: dict[str, Any]) -> None:
    """Stamp what a post learned onto the stored settings, but only while they
    still point at ``channel_id``. Read ``FOR UPDATE`` so an admin save cannot
    commit in between and be overwritten. Best effort: a failure is logged."""
    try:
        row = _row(db, SETTINGS_KEY, lock=True)
        stored = row.value if row is not None else None
        if (
            not isinstance(stored, dict)
            or not channel_id
            or stored.get("channel_id") != channel_id
            or all(stored.get(k) is v for k, v in changes.items())
        ):
            db.commit()  # nothing to write; release the row lock
            return
        row.value = {**stored, **changes}  # a new dict, so the JSON column sees it
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[whats-new] could not record what the channel post learned: %s",
            safe_error(exc),
        )
        _rollback_quietly(db)


def record_bot_membership(db: Session, in_channel: bool, *, channel_id: str) -> None:
    """A post to ``channel_id`` showed whether the bot is in it."""
    changes: dict[str, Any] = {"bot_in_channel": bool(in_channel)}
    if in_channel:
        changes["channel_archived"] = False
    _record(db, channel_id, changes)


def record_channel_archived(db: Session, *, channel_id: str) -> None:
    """Slack refused a post to ``channel_id`` because it is archived."""
    _record(db, channel_id, {"channel_archived": True})


# ── the morning post's mark ──────────────────────────────────────────────────


def _clean_mark(stored: Any) -> tuple[datetime, str] | None:
    """``(published_at, entry_id)`` of the last note posted, or None."""
    if not isinstance(stored, dict):
        return None
    at = stored.get("at")
    entry_id = stored.get("entry_id")
    if not isinstance(at, str) or not isinstance(entry_id, str):
        return None
    if entry_id and not is_entry_id(entry_id):
        return None
    try:
        when = datetime.fromisoformat(at)
    except ValueError:
        return None
    return when.replace(tzinfo=None), entry_id


def read_digest_mark(db: Session, *, lock: bool = False) -> tuple[datetime, str] | None:
    """The mark on the caller's transaction (``FOR UPDATE`` with ``lock``);
    None before the first post. RAISES on a read failure: the post must not
    mistake "unreadable" for "never posted" and start over."""
    row = _row(db, DIGEST_KEY, lock=lock)
    return _clean_mark(row.value if row is not None else None)


def write_digest_mark(db: Session, at: datetime, entry_id: str) -> None:
    """Move the mark WITHOUT committing; the post commits it with its outcome."""
    from rubik_core.database.models import WorkspaceSetting

    value = {"at": at.isoformat(), "entry_id": entry_id}
    row = _row(db, DIGEST_KEY, lock=True)
    if row is None:
        db.add(WorkspaceSetting(key=DIGEST_KEY, value=value))
    else:
        row.value = value  # a new dict, so the JSON column sees it
    db.flush()

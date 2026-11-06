"""Admin → Loose Ends: whether the daily Slack post runs, and to which channel.

The post goes to ``#sales``, but it ships OFF.
A Cubie admin switches it on and off, and changes the channel, from the Admin
screen — no env var, no redeploy. Stored in the existing ``workspace_settings``
key-value table under :data:`SETTINGS_KEY` (the same store as
``rubik_core/sales/model_config.py``), so there is no migration.

Two rules this module exists to keep:

* **Fail closed.** :func:`load` never raises. Any read failure — no row, a
  malformed value, the database not answering — returns the defaults, and the
  defaults are OFF. A database hiccup must never post to a channel.
* **On means it can actually post.** :func:`save` resolves the channel with the
  Cubie Slack bot before it stores anything. Switching the post ON needs a
  channel Slack knows, that is not archived, AND the bot in it; anything else
  is a ``ValueError`` whose message is the sentence the admin reads (the
  router returns it as the 400 ``detail``), naming the exact fix. Switching it
  OFF is always allowed — so the channel can be changed while the post is off,
  even before the bot has been invited — and a channel the bot cannot see is
  refused only when it is a NEW channel (a typo), never when the admin is
  turning the current one off. A save with nothing changed is how the admin
  re-checks the bot after an invite: every save asks Slack again.
* **A post's answer lands on the channel it was for.** What a post learns
  (the bot was not in the channel, the channel is archived, the post went
  through) is recorded only while the settings still point at that channel,
  under a row lock (:func:`_record`).

``rubik_core`` does not import ``backend`` at module scope (the cubie-worker
image loads this package without FastAPI), so the Slack client is imported
inside :func:`save`.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from rubik_core.brand import PRODUCT_NAME
from rubik_core.logsafe import safe_error

logger = logging.getLogger(__name__)

SETTINGS_KEY = "loop_closure_digest"

#: What a fresh install reads: OFF, pointed at #sales, nothing resolved yet.
DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "channel_name": "#sales",
    "channel_id": None,
}

#: The full shape :func:`load` returns. ``bot_in_channel`` is None until a save
#: has checked it; ``channel_archived`` is True once Slack said the channel is
#: archived (a save, or a post Slack refused with ``is_archived``);
#: ``updated_*`` are None until an admin saves.
_FIELDS: dict[str, Any] = {
    **DEFAULTS,
    "bot_in_channel": None,
    "channel_archived": False,
    "updated_by": None,
    "updated_at": None,
}

#: A Slack channel id: public ``C…`` or legacy private ``G…``, upper-case.
_CHANNEL_ID = re.compile(r"[CG][A-Z0-9]{6,}")
#: A Slack channel name: lower-case letters, digits, ``-`` and ``_``, ≤ 80.
_CHANNEL_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,79}")

_INVITE_FIX = f"type /invite @{PRODUCT_NAME} in {{name}}"


def _defaults() -> dict[str, Any]:
    return dict(_FIELDS)


def _clean(stored: Any) -> dict[str, Any]:
    """The stored blob merged onto the defaults, every field type-checked.

    ``enabled`` is True only for a literal ``True`` with a channel id beside
    it: a half-written or hand-edited row reads as OFF, never as "post
    somewhere".
    """
    out = _defaults()
    if not isinstance(stored, dict):
        return out
    channel_id = stored.get("channel_id")
    if isinstance(channel_id, str) and _CHANNEL_ID.fullmatch(channel_id.strip()):
        out["channel_id"] = channel_id.strip()
    name = stored.get("channel_name")
    if isinstance(name, str) and name.strip():
        out["channel_name"] = "#" + name.strip().lstrip("#")
    if isinstance(stored.get("bot_in_channel"), bool):
        out["bot_in_channel"] = stored["bot_in_channel"]
    if isinstance(stored.get("channel_archived"), bool):
        out["channel_archived"] = stored["channel_archived"]
    for key in ("updated_by", "updated_at"):
        if isinstance(stored.get(key), str) and stored[key]:
            out[key] = stored[key]
    out["enabled"] = stored.get("enabled") is True and bool(out["channel_id"])
    return out


def _read(db: Session) -> dict[str, Any]:
    from rubik_core.database.models import WorkspaceSetting

    row = (
        db.query(WorkspaceSetting).filter(WorkspaceSetting.key == SETTINGS_KEY).first()
    )
    return _clean(row.value if row is not None else None)


def load(db: Session | None = None) -> dict[str, Any]:
    """The digest settings, merged onto the defaults. NEVER raises.

    Any failure returns the defaults — the post OFF. With ``db`` given the read
    runs on the caller's session, and a failed read rolls that session back (on
    Postgres a failed statement aborts the transaction), so call it before
    writing anything you mean to keep. Without ``db`` it opens its own session.
    """
    try:
        if db is not None:
            return _read(db)
        # Through the module attribute, so a test that swaps SessionLocal lands.
        from rubik_core.database import db as database

        with database.SessionLocal() as own:
            return _read(own)
    except Exception as exc:  # noqa: BLE001 — fail closed: a hiccup never posts
        logger.warning(
            "[loop-closure] digest settings unreadable; the post stays off: %s",
            safe_error(exc),
        )
        if db is not None:
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
        return _defaults()


def _looks_like_id(raw: str, bare: str) -> bool:
    """A channel id is typed bare (Slack never shows one with a ``#``) and has
    a digit in it, so ``#CUSTOMERSUCCESS`` or ``#GROWTH1`` stay channel names."""
    return (
        not raw.startswith("#")
        and bool(_CHANNEL_ID.fullmatch(bare))
        and any(c.isdigit() for c in bare)
    )


def normalise_channel(channel: str) -> tuple[str, str | None]:
    """``"#Sales"`` → ``("sales", None)``; ``"C0123ABCD"`` → ``(id, id)``.

    Returns ``(lookup_key, channel_id_or_None)``. Raises ``ValueError`` (a
    sentence for the admin) for an empty or impossible channel name. Only a
    bare upper-case token with a digit is an id; anything typed with a ``#``
    is a name, whatever its case.
    """
    raw = (channel or "").strip()
    bare = raw.lstrip("#").strip()
    if not bare:
        raise ValueError("Type the channel to post to, for example #sales.")
    if _looks_like_id(raw, bare):
        return bare, bare
    name = bare.lower()
    if not _CHANNEL_NAME.fullmatch(name):
        raise ValueError(
            f"“{raw}” isn't a Slack channel name. Use the name as Slack shows it, "
            "for example #sales (lower-case letters, numbers, - and _ only)."
        )
    return name, None


def save(
    db: Session,
    *,
    enabled: bool,
    channel: str,
    actor_email: str,
    bot: Any = None,
) -> dict[str, Any]:
    """Check the channel with the Slack bot, then store the settings.

    Returns the stored settings (the :func:`load` shape). Raises ``ValueError``
    with the sentence the admin should read when the change cannot be saved:
    an impossible or unknown channel, or switching ON without the bot being
    able to post there.
    """
    key, typed_id = normalise_channel(channel)
    current = load(db)
    display = f"#{key}" if typed_id is None else typed_id

    if bot is None:
        from backend.services.slack_service import SlackBotClient

        bot = SlackBotClient()

    found: dict[str, Any] | None = None
    looked = False
    if getattr(bot, "is_available", False):
        try:
            found = bot.find_channel(key)
            if found is None and typed_id is not None:
                # Something id-shaped Slack has no id for may still be a name
                # typed in capitals ("SALES2"): try it as one before refusing.
                as_name = typed_id.lower()
                if _CHANNEL_NAME.fullmatch(as_name):
                    found = bot.find_channel(as_name)
            looked = True
        except Exception as exc:  # noqa: BLE001 — a Slack error is a sentence, not a 500
            logger.warning(
                "[loop-closure] digest channel lookup failed: %s", safe_error(exc)
            )
            if enabled and current.get("enabled"):
                raise ValueError(
                    f"{PRODUCT_NAME} couldn't check {display} with Slack just now, so nothing "
                    f"was changed. The post is still on for "
                    f"{current.get('channel_name')}. Try again in a minute."
                ) from exc
            if enabled:
                raise ValueError(
                    f"{PRODUCT_NAME} couldn't check {display} with Slack just now, so the post "
                    "was not turned on. Try again in a minute."
                ) from exc
    elif enabled:
        raise ValueError(
            f"{PRODUCT_NAME} isn't connected to Slack, so it can't post. Install the {PRODUCT_NAME} "
            "Slack app in the workspace first, then turn this on."
        )

    if found is not None:
        channel_id = found.get("id") or typed_id
        channel_name = "#" + (found.get("name") or key)
        bot_in_channel: bool | None = bool(found.get("is_member"))
        archived = bool(found.get("is_archived"))
    else:
        unchanged = key in {
            (current.get("channel_id") or "").strip(),
            (current.get("channel_name") or "").lstrip("#").lower(),
        }
        if looked and not (unchanged and not enabled):
            raise ValueError(
                f"{PRODUCT_NAME} can't see a channel called {display}. Check the spelling; "
                f"if it is a private channel, invite {PRODUCT_NAME} to it first "
                f"({_INVITE_FIX.format(name=display)}), then save again. If it "
                "was archived, pick another channel."
            )
        # Turning the current channel off, or Slack not answering while off:
        # keep what is known and do not claim a membership nobody checked.
        if unchanged:
            channel_id = current.get("channel_id") or typed_id
            channel_name = current.get("channel_name") or display
            archived = bool(current.get("channel_archived"))
        else:
            channel_id = typed_id
            channel_name = display
            archived = False
        bot_in_channel = False if looked else None

    if enabled and archived:
        raise ValueError(
            f"Slack says {channel_name} is archived, so {PRODUCT_NAME} can't post there. "
            "Pick another channel, then turn this on."
        )
    if enabled and not bot_in_channel:
        raise ValueError(
            f"Invite {PRODUCT_NAME} to {channel_name} first: "
            f"{_INVITE_FIX.format(name=channel_name)}, then turn this on."
        )

    blob = {
        "enabled": bool(enabled),
        "channel_id": channel_id,
        "channel_name": channel_name,
        "bot_in_channel": bot_in_channel,
        "channel_archived": archived,
        "updated_by": (actor_email or "").strip().lower() or None,
        "updated_at": datetime.utcnow().isoformat(timespec="seconds"),
    }

    from rubik_core.database.models import WorkspaceSetting

    row = (
        db.query(WorkspaceSetting).filter(WorkspaceSetting.key == SETTINGS_KEY).first()
    )
    if row is None:
        db.add(WorkspaceSetting(key=SETTINGS_KEY, value=blob))
    else:
        row.value = blob  # a new dict, so the JSON column sees the change
    db.commit()
    logger.info(
        "[loop-closure] digest settings saved (enabled=%s, channel=%s)",
        blob["enabled"],
        blob["channel_id"] or blob["channel_name"],
    )
    return _clean(blob)


def _record(db: Session, channel_id: str, changes: dict[str, Any]) -> None:
    """Stamp what a post learned onto the stored settings — but only while they
    still point at ``channel_id``, the channel that post went to.

    A post can take seconds (Slack retries and rate limits); an admin who moved
    the post to another channel meanwhile must not have the old channel's
    answer written onto the new one. The row is read ``FOR UPDATE`` so an
    admin save cannot commit between this read and this write and be
    overwritten with the older value. Best-effort: a failure is logged and
    forgotten — the digest's own status already says what happened.
    """
    from rubik_core.database.models import WorkspaceSetting

    try:
        row = (
            db.query(WorkspaceSetting)
            .filter(WorkspaceSetting.key == SETTINGS_KEY)
            .with_for_update()
            .first()
        )
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
            "[loop-closure] could not record what the digest post learned: %s",
            safe_error(exc),
        )
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def record_bot_membership(db: Session, in_channel: bool, *, channel_id: str) -> None:
    """Remember what the last post to ``channel_id`` learned about the bot.

    Slack refusing a post with ``not_in_channel`` records False, so Admin →
    Loose Ends shows the invite fix; a post that went through records True
    (and that the channel is not archived), so the card stops asking for a fix
    that already happened. Written only while the settings still point at
    ``channel_id`` (see :func:`_record`).
    """
    changes: dict[str, Any] = {"bot_in_channel": bool(in_channel)}
    if in_channel:
        changes["channel_archived"] = False
    _record(db, channel_id, changes)


def record_channel_archived(db: Session, *, channel_id: str) -> None:
    """Slack refused a post to ``channel_id`` with ``is_archived``: remember it,
    so the card says the channel needs changing instead of "On" with a last
    post date that silently stops moving."""
    _record(db, channel_id, {"channel_archived": True})

"""The morning post: the release notes published since the last one, in ONE message.

Spec §4.6.1: each workspace may opt into a digest of the Cubie changelog.
Admin → What's New holds the switch and the channel (``settings.py``); Cloud
Scheduler calls ``POST /api/agents/whats-new/digest`` each weekday morning,
which runs :func:`post_digest`. Phase 3 moves the post onto the workspace's
messaging connector; the rules below stay.

Rules it keeps:

* **What is due.** Every note the changelog source lists, published after the
  mark (the last note a post carried, ``settings.DIGEST_KEY``) and not in the
  future, about a feature that runs here (``audience.feature_switched_on``;
  a channel has no single reader, so Feature Access rosters do not apply).
  Oldest first, ordered by ``(published_at, id)`` so two notes published in
  the same second are never confused. At most :data:`MAX_ITEMS` per post, and
  fewer when the text would pass :data:`MAX_TEXT_CHARS`; the rest wait.
* **The first run starts the mark.** With no mark yet (posting was just
  switched on, or never ran), the run records "now" and posts nothing, so a
  workspace is never flooded with every note ever written.
* **One post at a time.** On Postgres the run holds an advisory lock for its
  whole transaction (the mark is read ``FOR UPDATE`` too), so two overlapping
  runs never post the same notes twice; the loser answers ``busy``.
* **Outcomes.** Posted → the mark moves to the last note carried. The bot
  missing from the channel, the channel archived, or no Slack bot → the mark
  stays (the notes go out once that is fixed) and what the post learned is
  stamped on the settings. Any other error, or a timeout (the post may have
  gone through) → ``failed`` and the mark moves anyway: ``chat.postMessage``
  is not idempotent, so a note is lost rather than posted twice.
* **Plain words, nothing smuggled.** An opener, then one bullet per note:
  its title, its body on one line, and a link to it with ``?src=slack`` so
  arrivals are counted. Every piece of note text goes through :func:`esc`,
  so nothing can smuggle in ``<!channel>`` or a disguised link.
* **Posting off is not an error.** The kill switch off → ``disabled``.
  Posting off in Admin → What's New, or no channel → ``not_configured``.

``rubik_core`` never imports ``backend`` at module scope: the Slack client is
imported inside :func:`post_digest`.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from rubik_core.brand import PRODUCT_NAME
from rubik_core.logsafe import safe_error
from rubik_core.whats_new import audience, changelog, links
from rubik_core.whats_new import settings as wn_settings
from rubik_core.whats_new.changelog import ChangelogEntry, ChangelogSource

logger = logging.getLogger(__name__)

#: Most notes in one post, oldest first; the rest wait for the next.
MAX_ITEMS = 12
#: Slack asks for a message's text to stay under 4,000 characters.
MAX_TEXT_CHARS = 3_900
#: A note's body in its bullet is cut to this, on a word.
MAX_BODY_CHARS = 280
#: One attempt at ``chat.postMessage``, never retried (it is not idempotent).
POST_TIMEOUT = 8.0
#: pg_try_advisory_xact_lock key: "WNDG" in ASCII.
_LOCK_KEY = 0x574E4447

#: Every named outcome of :func:`post_digest`.
STATUSES: tuple[str, ...] = (
    "disabled",
    "not_configured",
    "started",
    "quiet",
    "dry_run",
    "posted",
    "busy",
    "bot_unavailable",
    "not_in_channel",
    "channel_archived",
    "failed",
)

#: Slack errors meaning the bot cannot post in the channel as it stands.
_NOT_IN_CHANNEL_ERRORS = frozenset({"not_in_channel", "channel_not_found"})
_ARCHIVED_ERRORS = frozenset({"is_archived"})
#: Outcomes after which the same notes go out again with the next post.
_KEEP_MARK = frozenset({"bot_unavailable", "not_in_channel", "channel_archived"})


# ── the message ──────────────────────────────────────────────────────────────


def esc(text: str | None) -> str:
    """Escape the three characters Slack mrkdwn treats as control."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _one_line(text: str, limit: int) -> str:
    words = " ".join((text or "").split())
    if len(words) <= limit:
        return words
    return words[:limit].rsplit(" ", 1)[0].rstrip(",.;:") + "…"


def opener(count: int) -> str:
    """The post's first line, chosen by how many notes it carries."""
    if count == 1:
        return f"One new thing in {PRODUCT_NAME}:"
    return f"{count} new things in {PRODUCT_NAME}:"


def line(entry: ChangelogEntry) -> str:
    """One note: ``• *<title>*: <body> <link|See it>``. Without a public app
    address there is no link and the bullet ends with the words."""
    text = f"• *{esc(_one_line(entry.title, 150))}*"
    body = _one_line(entry.body, MAX_BODY_CHARS)
    if body:
        text += f": {esc(body)}"
    link = links.deep_link(entry.id, src="slack")
    if link:
        text += f" <{link}|See it>"
    return text


def message(entries: list[ChangelogEntry]) -> str:
    """The whole post: the opener, then one bullet per note."""
    return "\n".join([opener(len(entries)), *(line(e) for e in entries)])


def _fit(entries: list[ChangelogEntry]) -> tuple[list[ChangelogEntry], str]:
    """The leading notes whose message fits :data:`MAX_TEXT_CHARS` (always at
    least one: a single note is far under it), and that message."""
    count = min(len(entries), MAX_ITEMS)
    while count > 1 and len(message(entries[:count])) > MAX_TEXT_CHARS:
        count -= 1
    return entries[:count], message(entries[:count])


def _key(entry: ChangelogEntry) -> tuple[datetime, str]:
    return entry.published_at, entry.id


def due(
    source: ChangelogSource, mark: tuple[datetime, str], now: datetime
) -> list[ChangelogEntry]:
    """Notes published after ``mark`` and not after ``now``, about features
    that run here, oldest first."""
    entries = [
        entry
        for entry in source.list_published()
        if mark < _key(entry)
        and entry.published_at <= now
        and audience.feature_switched_on(entry.feature_key)
    ]
    return sorted(entries, key=_key)


# ── the run ──────────────────────────────────────────────────────────────────


def _try_lock(db: Session) -> bool:
    """One post at a time (Postgres). Held until this transaction ends."""
    if db.get_bind().dialect.name != "postgresql":
        return True
    return bool(
        db.execute(
            sql_text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": _LOCK_KEY}
        ).scalar()
    )


def _done(result: dict[str, Any], error: str | None = None) -> dict[str, Any]:
    """Log the run's one line and hand the result back."""
    status = result["status"]
    args = (
        "[whats-new] morning post: %s (%s in the post, %s still waiting)%s",
        status,
        result.get("items", 0),
        result.get("waiting", 0),
        f": {error}" if error else "",
    )
    if status in ("failed", "not_in_channel", "channel_archived", "bot_unavailable"):
        logger.warning(*args)
    else:
        logger.info(*args)
    return result


def _send(bot: Any, channel_id: str, text: str) -> tuple[str, dict[str, Any]]:
    """Post once; ``(status, extra)`` where ``extra`` carries ``message_ts``,
    ``error`` (a short code), ``learned`` and ``error_text`` (for the log)."""
    try:
        result = bot.post_message(channel_id, text=text) or {}
    except Exception as exc:  # noqa: BLE001 — every failure is a named status
        error = getattr(exc, "error", None)
        extra: dict[str, Any] = {"error_text": safe_error(exc)}
        if error in _NOT_IN_CHANNEL_ERRORS:
            return "not_in_channel", {**extra, "error": error, "learned": "not_member"}
        if error in _ARCHIVED_ERRORS:
            return "channel_archived", {**extra, "error": error, "learned": "archived"}
        if type(exc).__name__ == "SlackNotConnectedError":
            return "bot_unavailable", extra
        code = error if isinstance(error, str) and error else type(exc).__name__
        return "failed", {**extra, "error": code[:120]}
    ts = str(result.get("ts") or "")[:40] or None
    return "posted", {"message_ts": ts, "learned": "member"}


def _learn(db: Session, config: dict, channel_id: str, learned: str | None) -> None:
    """Stamp what the post showed about the bot and the channel."""
    if learned == "member":
        if config.get("bot_in_channel") is not True or config.get("channel_archived"):
            wn_settings.record_bot_membership(db, True, channel_id=channel_id)
    elif learned == "not_member":
        wn_settings.record_bot_membership(db, False, channel_id=channel_id)
    elif learned == "archived":
        wn_settings.record_channel_archived(db, channel_id=channel_id)


def post_digest(
    db: Session,
    *,
    now: datetime | None = None,
    bot: Any = None,
    dry_run: bool = False,
    source: ChangelogSource | None = None,
) -> dict[str, Any]:
    """Post the release notes published since the last post, in one message.

    Returns a NAMED status (:data:`STATUSES`) with ``items`` (notes in the
    message) and ``waiting`` (notes due but left for a later post):

    ``disabled`` (``WHATS_NEW_ENABLED`` off) | ``not_configured`` (posting off
    or no channel) | ``started`` (the first run: the mark is set, nothing
    posted) | ``quiet`` (nothing due) | ``dry_run`` (``items`` and the exact
    ``text``; nothing posted or recorded, works with posting off too) |
    ``posted`` (plus ``message_ts``) | ``busy`` (another post is in flight) |
    ``bot_unavailable`` / ``not_in_channel`` / ``channel_archived`` (the notes
    wait, plus ``error``) | ``failed`` (plus ``error``; the notes are not
    posted again).
    """
    from rubik_core.config import settings

    if not settings.WHATS_NEW_ENABLED:
        return _done({"status": "disabled", "items": 0, "waiting": 0})
    now = now or datetime.utcnow()
    source = source or changelog.get_changelog_source()
    config = wn_settings.load(db)
    channel_id = config.get("channel_id")
    ready = bool(config.get("slack_enabled") and channel_id)

    if not dry_run and not ready:
        db.rollback()
        return _done({"status": "not_configured", "items": 0, "waiting": 0})
    if not dry_run and not _try_lock(db):
        db.rollback()
        return _done({"status": "busy", "items": 0, "waiting": 0})

    mark = wn_settings.read_digest_mark(db, lock=not dry_run)
    if mark is None:
        if dry_run:
            db.rollback()
            return _done({"status": "dry_run", "items": 0, "waiting": 0, "text": None})
        wn_settings.write_digest_mark(db, now, "")
        db.commit()
        return _done({"status": "started", "items": 0, "waiting": 0})

    try:
        pending = due(source, mark, now)
    except Exception as exc:  # noqa: BLE001 — an unreadable source posts nothing
        # Not a quiet morning: the run reports "failed", and the mark stays
        # where it was, so the next run picks up the same notes.
        db.rollback()
        logger.warning("[whats-new] changelog unreadable: %s", safe_error(exc))
        return _done({"status": "failed", "items": 0, "waiting": 0, "error": "source"})
    if not pending:
        db.rollback()
        return _done({"status": "quiet", "items": 0, "waiting": 0})
    picked, text = _fit(pending)
    waiting = len(pending) - len(picked)

    if dry_run:
        db.rollback()
        return _done(
            {
                "status": "dry_run",
                "items": len(picked),
                "waiting": waiting,
                "posting_on": ready,
                "text": text,
            }
        )

    if bot is None:
        from backend.services.slack_service import SlackBotClient

        bot = SlackBotClient(timeout=POST_TIMEOUT, max_retries=1)
    if not getattr(bot, "is_available", True):
        db.rollback()
        return _done({"status": "bot_unavailable", "items": 0, "waiting": len(pending)})

    status, extra = _send(bot, channel_id, text)
    if status in _KEEP_MARK:
        db.rollback()
    else:
        last = picked[-1]
        wn_settings.write_digest_mark(db, last.published_at, last.id)
        db.commit()
    _learn(db, config, channel_id, extra.get("learned"))

    out: dict[str, Any] = {
        "status": status,
        "items": 0 if status in _KEEP_MARK else len(picked),
        "waiting": len(pending) if status in _KEEP_MARK else waiting,
    }
    if extra.get("message_ts"):
        out["message_ts"] = extra["message_ts"]
    if extra.get("error"):
        out["error"] = extra["error"]
    return _done(out, extra.get("error_text"))

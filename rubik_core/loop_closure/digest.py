"""The daily Loose Ends digest — one post a day to ONE shared Slack channel.

The second sanctioned sales sender, next to ``rubik_core/cockpit/digest.py``
(whose module docstring names this one, so neither can be cited as precedent
for a third). It obeys the same anti-tsunami rules:

* **One post per channel per local day**, enforced by the database: UNIQUE
  ``(channel_key, local_date)`` on ``loop_digest_posts``. Insert + flush to
  claim the day, post, stamp ``posted_at`` / ``message_ts``, commit — and roll
  back on a failed post so a retry (or tomorrow) can post. Never a pre-check:
  two scheduler executions racing would both pass one.
* **Quiet days post nothing**, and write no row: no open loose end anywhere on
  the roster is not news — unless someone could not be checked, which is.
* **"Could not look" is never "all clear".** People whose scan failed or whose
  mailbox could not be read are named on their own line and left out of the
  counts and the "of N people scanned" denominator.
* **Metadata only, by construction.** The text is built from a whitelist —
  each person's name, their open count by kind, how many are over the
  escalation age — and never from an item's title, headline, subject or
  snippet. Mailbox content never leaves its owner (README §2).
* **No ranking.** People are listed A–Z, in the same words; nobody is "worst".

* **Off unless an admin turned it on.** Whether it posts, and to which
  channel, is Admin → Loose Ends (``digest_settings``, stored in
  ``workspace_settings``). It ships OFF, pointed at ``#sales``; a settings read
  that fails reads as OFF. Off is a normal, quiet outcome (``status: off``)
  that touches nothing — not Slack, not the guard table.

The channel is the stored channel id (also the guard's ``channel_key``) and the
day is the workspace's business-timezone day: a channel has
no recipient timezone of its own. Delivery is ``SlackBotClient.post_message`` from
``backend/services/slack_service.py`` — the repo's one outbound Slack path —
imported inside the function because ``rubik_core`` does not import
``backend`` at module scope. :func:`render_today` builds the same text for the
admin preview without posting or writing anything.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from rubik_core.brand import PRODUCT_NAME
from rubik_core.database.loop_closure import LoopDigestPost
from rubik_core.logsafe import safe_error
from rubik_core.loop_closure import constants

logger = logging.getLogger(__name__)

#: Slack renders up to 3,000 characters in a section; stay under it with room
#: for the "and N more" line.
MAX_TEXT_CHARS = 2900


def _local_date(now: datetime | None) -> str:
    from rubik_core.cockpit.business_hours import _business_tz

    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(_business_tz()).date().isoformat()


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _person_line(person: dict, escalation_days: int) -> str:
    """One line per person, from whitelisted fields only."""
    kinds = person.get("by_kind") or {}
    parts = [
        f"{kinds.get(k, 0)} {constants.KIND_LABELS[k].lower()}"
        for k in constants.KINDS
        if kinds.get(k, 0)
    ]
    over = int(person.get("over_escalation") or 0)
    tail = f"; {over} open {escalation_days}+ days" if over else ""
    return (
        f"• {person['name']} — {_plural(int(person['open']), 'open')} "
        f"({', '.join(parts)}){tail}"
    )


def build_text(
    team: dict,
    *,
    escalation_days: int,
    link: str | None = None,
    unchecked: set[str] | frozenset[str] = frozenset(),
) -> str:
    """The digest text from a ``team_view``-shaped dict. Never reads an item.

    ``unchecked`` (emails) are the people the latest scan could not check —
    their scan failed or their mailbox could not be read. They are left out of
    the counts and the denominator, and named on a line of their own, so a
    person nobody could read is never counted as "scanned" or as clean.
    """
    everyone = list(team.get("people") or [])
    skip = {(e or "").strip().lower() for e in unchecked}
    checked = [p for p in everyone if (p.get("email") or "").lower() not in skip]
    not_checked = [p for p in everyone if (p.get("email") or "").lower() in skip]
    people = [p for p in checked if int(p.get("open") or 0) > 0]
    total = sum(int(p["open"]) for p in people)
    over = sum(int(p.get("over_escalation") or 0) for p in people)
    header = (
        f"*Loose ends this morning:* {_plural(total, 'open item')} across "
        f"{len(people)} of {len(checked)} people scanned; {over} of the {total} "
        f"open {escalation_days}+ days."
    )
    footer = (
        f"Each person sees their own list in {PRODUCT_NAME} → Sales → Loose Ends: "
        f"{link}"
        if link
        else f"Each person sees their own list in {PRODUCT_NAME} → Sales → Loose Ends."
    )
    lines = [header]
    gap = ""
    if not_checked:
        names = ", ".join(str(p.get("name") or p.get("email")) for p in not_checked)
        gap = (
            f"Not checked this morning ({len(not_checked)} of "
            f"{len(everyone)} on the roster; the mailbox could not be read or the "
            f"scan failed, so their lists may be out of date): {names}"
        )
        if len(gap) > 600:
            gap = gap[:599].rstrip() + "…"
    # Room kept for the footer, the not-checked line and an "…and N more" line.
    reserve = len(footer) + len(gap) + 40
    for i, person in enumerate(people):  # already A–Z
        line = _person_line(person, escalation_days)
        if len("\n".join([*lines, line])) + reserve > MAX_TEXT_CHARS:
            rest = len(people) - i
            lines.append(f"…and {_plural(rest, 'more person', 'more people')}")
            break
        lines.append(line)
    if gap:
        lines.append(gap)
    lines.append(footer)
    text = "\n".join(lines)
    return text[:MAX_TEXT_CHARS]


#: Slack errors that mean the bot cannot post in the channel (it was never
#: invited, was removed, or the private channel is invisible to it).
_NOT_IN_CHANNEL_ERRORS = frozenset({"not_in_channel", "channel_not_found"})

#: Slack's refusal for an archived channel. Its fix is a different channel,
#: not an invite, so it is its own status and its own flag on the card.
_ARCHIVED_ERRORS = frozenset({"is_archived"})

#: Every status :func:`post_daily_digest` returns. ``off`` and ``quiet`` are
#: normal, quiet outcomes, never failures.
STATUSES = (
    "off",
    "quiet",
    "dry_run",
    "posted",
    "already_posted",
    "bot_unavailable",
    "not_in_channel",
    "channel_archived",
    "failed",
)


def _compose(
    db: Session,
    *,
    now: datetime | None,
    emails: list[str] | None,
    unchecked: set[str] | None,
) -> dict:
    """Today's digest as data: ``{"total", "quiet", "text", "local_date"}``.

    The one builder behind both the post and the admin preview. ``text`` is
    None on a quiet day (nothing open among the people who WERE checked, and
    nobody left unchecked); otherwise it is the metadata-only digest.
    """
    from rubik_core.config import settings
    from rubik_core.loop_closure.views import team_view

    limits = constants.thresholds()
    team = team_view(db, now=now, emails=emails)
    if unchecked is None:
        from rubik_core.loop_closure.views import unchecked_emails

        unchecked = unchecked_emails(
            db, [p.get("email") or "" for p in team.get("people") or []]
        )
    skip = {(e or "").strip().lower() for e in unchecked}
    total = sum(
        int(p.get("open") or 0)
        for p in team.get("people") or []
        if (p.get("email") or "").lower() not in skip
    )
    on_roster = {(p.get("email") or "").lower() for p in team.get("people") or []}
    local_date = _local_date(now)
    # Quiet means nothing open among the people who WERE checked. Someone
    # nobody could read is not quiet: that day still posts, to say so.
    if total == 0 and not (skip & on_roster):
        return {"total": 0, "quiet": True, "text": None, "local_date": local_date}

    base = (getattr(settings, "FRONTEND_URL", "") or "").rstrip("/")
    # ``?view=team`` is the query the page reads for its Team tab
    # (frontend/src/pages/sales/LooseEnds.tsx); a reader without the team
    # roster lands on their own list.
    link = f"{base}/sales/loose-ends?view=team" if base else None
    text = build_text(
        team, escalation_days=limits["escalation_days"], link=link, unchecked=skip
    )
    return {"total": total, "quiet": False, "text": text, "local_date": local_date}


def post_daily_digest(
    db: Session,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    bot: Any = None,
    emails: list[str] | None = None,
    unchecked: set[str] | None = None,
) -> dict:
    """Post today's digest once, or nothing. Returns a NAMED status
    (:data:`STATUSES`):

    ``off`` (an admin has not switched it on) | ``quiet`` | ``posted`` |
    ``already_posted`` | ``bot_unavailable`` | ``not_in_channel`` (Slack says
    the bot is not in the channel; Admin → Loose Ends then shows the invite
    fix) | ``channel_archived`` (Slack says the channel is archived; the card
    then says to pick another one) | ``failed`` — and ``dry_run`` for a dry
    run with something to say (the text is returned; it is metadata only).
    """
    from rubik_core.loop_closure import digest_settings

    config = digest_settings.load(db)
    if not config["enabled"] or not config["channel_id"]:
        return {"status": "off"}
    channel = config["channel_id"]

    built = _compose(db, now=now, emails=emails, unchecked=unchecked or set())
    total = built["total"]
    local_date = built["local_date"]
    if built["quiet"]:
        return {"status": "quiet", "item_count": 0}
    text = built["text"]
    if dry_run:
        return {
            "status": "dry_run",
            "item_count": total,
            "text": text,
            "local_date": local_date,
        }

    if bot is None:
        from backend.services.slack_service import SlackBotClient

        bot = SlackBotClient()
    if not getattr(bot, "is_available", True):
        # Not a failure of today's digest: no row, so it posts once the app is in.
        logger.warning("[loop-closure] no Slack bot token — digest not posted")
        return {"status": "bot_unavailable", "item_count": total}

    row = LoopDigestPost(channel_key=channel, local_date=local_date, item_count=total)
    try:
        db.add(row)
        db.flush()  # claim the day
    except IntegrityError:
        db.rollback()
        return {"status": "already_posted", "local_date": local_date}

    try:
        result = bot.post_message(channel, text=text) or {}
    except Exception as exc:  # noqa: BLE001 — a failed post leaves no row
        db.rollback()
        error = getattr(exc, "error", None)
        if error in _NOT_IN_CHANNEL_ERRORS:
            # The admin's fix is an invite, not a retry: say so on the card.
            logger.warning("[loop-closure] digest not posted: bot not in the channel")
            digest_settings.record_bot_membership(db, False, channel_id=channel)
            return {"status": "not_in_channel", "error": error, "item_count": total}
        if error in _ARCHIVED_ERRORS:
            # The fix is another channel; without this the card would keep
            # saying "On" while every morning's post fails.
            logger.warning("[loop-closure] digest not posted: the channel is archived")
            digest_settings.record_channel_archived(db, channel_id=channel)
            return {"status": "channel_archived", "error": error, "item_count": total}
        logger.warning("[loop-closure] digest post failed: %s", safe_error(exc))
        return {"status": "failed", "error": type(exc).__name__}

    row.posted_at = datetime.utcnow()
    row.message_ts = str(result.get("ts") or "") or None
    db.commit()
    if config.get("bot_in_channel") is not True or config.get("channel_archived"):
        digest_settings.record_bot_membership(db, True, channel_id=channel)
    return {
        "status": "posted",
        "item_count": total,
        "local_date": local_date,
        "message_ts": row.message_ts,
    }


def render_today(db: Session, *, now: datetime | None = None) -> dict:
    """Today's post as the admin preview: ``{"text", "would_post", "reason"}``.

    The same builder as the post, over the roster the scan posts over —
    ``roster_emails`` with its default "has left" lookup (Slack and Google), so
    someone who has left is not named or counted here when the post would
    leave them out — and the same "could not check" rule, so the preview is
    what the channel would get. No Slack call and no guard row: it only reads.

    ``text`` is empty on a quiet day. ``would_post`` is whether the next
    morning's scan will post it; ``reason`` says why not when it is False,
    and is a caveat when it is True but the last post was refused (the scan
    tries again; Save re-checks now). None when there is nothing to add.
    """
    from rubik_core.config import settings
    from rubik_core.loop_closure import digest_settings
    from rubik_core.loop_closure.roster import roster_emails

    config = digest_settings.load(db)
    built = _compose(db, now=now, emails=roster_emails(db), unchecked=None)
    if built["quiet"]:
        return {
            "text": "",
            "would_post": False,
            "reason": "Nothing open today — a quiet day posts nothing.",
        }
    if not config["enabled"] or not config["channel_id"]:
        return {
            "text": built["text"],
            "would_post": False,
            "reason": (
                "The daily post is off, so nothing is posted. Turn it on to post "
                f"this to {config['channel_name']} each weekday morning after the scan."
            ),
        }
    if not getattr(settings, "LOOP_CLOSURE_ENABLED", False):
        return {
            "text": built["text"],
            "would_post": False,
            "reason": "The Loose Ends scan is switched off on this server, so nothing posts.",
        }
    name = config["channel_name"]
    if config.get("channel_archived"):
        return {
            "text": built["text"],
            "would_post": False,
            "reason": (
                f"Slack refused the last post because {name} is archived, so "
                "nothing posts there. Pick another channel and Save."
            ),
        }
    posted = (
        db.query(LoopDigestPost)
        .filter(
            LoopDigestPost.channel_key == config["channel_id"],
            LoopDigestPost.local_date == built["local_date"],
            LoopDigestPost.posted_at.isnot(None),
        )
        .first()
    )
    if posted is not None:
        return {
            "text": built["text"],
            "would_post": False,
            "reason": (
                f"Today's post already went to {name}. It posts "
                "once a day; this is what it would say now."
            ),
        }
    if config.get("bot_in_channel") is False:
        # The post does not read this flag: the next scan tries again, and a
        # post that goes through clears it. Say so instead of "would not post".
        return {
            "text": built["text"],
            "would_post": True,
            "reason": (
                f"Slack refused the last post to {name} because {PRODUCT_NAME} wasn't in "
                "the channel. The next morning's scan will try again; after you "
                f"type /invite @{PRODUCT_NAME} in {name}, Save re-checks it now."
            ),
        }
    return {"text": built["text"], "would_post": True, "reason": None}


def last_post(db: Session, config: dict | None = None) -> dict | None:
    """The most recent digest that actually went out, for Admin → Loose Ends:
    ``{"local_date", "posted_at", "item_count", "channel_name"}``, or None.

    A claimed day whose post failed is rolled back, so every row with a
    ``posted_at`` is a post Slack accepted. The channel is named from the
    current settings when it is the current channel, else shown as its id.
    """
    if config is None:
        from rubik_core.loop_closure import digest_settings

        config = digest_settings.load(db)
    row = (
        db.query(LoopDigestPost)
        .filter(LoopDigestPost.posted_at.isnot(None))
        .order_by(LoopDigestPost.posted_at.desc())
        .first()
    )
    if row is None:
        return None
    current = config.get("channel_id")
    return {
        "local_date": row.local_date,
        "posted_at": row.posted_at.isoformat(timespec="seconds"),
        "item_count": int(row.item_count or 0),
        "channel_name": (
            config.get("channel_name")
            if current and row.channel_key == current
            else row.channel_key
        ),
    }

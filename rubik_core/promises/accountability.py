"""The nightly promise-accountability pass — "you said you would, and it
hasn't happened".

Runs inside the AI Priorities batch for each person, BEFORE their briefing is
written, so a promise found broken tonight is on this morning's card:

1. ``heard.hear`` — read new meetings, L10 notes, sent email and Slack;
   write each first-person promise as a ``suggested`` ledger row.
2. ``evidence.check`` — for promises whose moment has come, look for proof
   they were kept and record the verdict.

The ``promises`` priorities channel then offers the unkept ones to the
decision-maker as ``promise_unkept`` signals.

Never raises, and stops starting new model calls once its time budget is
spent: it shares the per-user timeout with the briefing itself, and a slow
listener must never cost anyone their morning priorities.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from rubik_core.cost.usage import attribute_to

logger = logging.getLogger(__name__)

FEATURE_KEY = "promise_accountability"
#: Wall-clock budget for the whole pass. The per-user batch timeout is 600s and
#: the briefing typically needs 180-300s of it.
TIME_BUDGET_SECONDS = 180
#: Sent mail is read once for both halves; the evidence window is the longer.
SENT_LOOKBACK_DAYS = 30


def enrolled(user_email: str, user: Any | None = None) -> bool:
    """Kill switch on AND the person is on the roster. An unreadable gate is a
    closed gate."""
    from rubik_core.config import settings

    if not getattr(settings, "PROMISE_ACCOUNTABILITY_ENABLED", False):
        return False
    try:
        from rubik_core.features import feature_allows

        if user is None:
            from rubik_core.database.repository import SQLUserRegistry

            user = SQLUserRegistry().get_user(user_email)
        return bool(user is not None and feature_allows(FEATURE_KEY, user))
    except Exception:  # noqa: BLE001
        logger.warning("promises: gate failed for %s", user_email, exc_info=True)
        return False


def _invoker():
    from rubik_core.priorities.agents.base import LLMJsonClient

    client = LLMJsonClient(model_key="gemini-flash", temperature=0.0)

    def invoke(prompt: str) -> Any:
        try:
            return client.invoke_json(prompt, fallback=None, attempts=2)
        except Exception:  # noqa: BLE001 — one failed read is retried next night
            logger.warning("promises: model call failed", exc_info=True)
            return None

    return invoke


@attribute_to(FEATURE_KEY)
def run_accountability_pass(user_email: str) -> dict:
    """Hear and check one person's promises. Never raises."""
    try:
        return _run(user_email)
    except Exception:  # noqa: BLE001
        logger.exception("promises: accountability pass failed for %s", user_email)
        return {"status": "error"}


def _run(user_email: str) -> dict:
    from rubik_core.database.repository import SQLUserRegistry

    email = (user_email or "").strip().lower()
    # ``users.email`` is the primary key and stored verbatim from the Google
    # token, so look it up as given before trying the folded form.
    registry = SQLUserRegistry()
    user = registry.get_user(user_email) or registry.get_user(email)
    if not enrolled(email, user):
        return {"status": "not_enrolled"}

    started = time.monotonic()

    def deadline() -> bool:
        return time.monotonic() - started > TIME_BUDGET_SECONDS

    from rubik_core.database.db import SessionLocal
    from rubik_core.promises import evidence, heard, store

    now = datetime.now(timezone.utc)
    # Each lane is fetched once and shared by both halves. ``None`` means the
    # lane could not be read — distinct from "read, and empty", because the
    # card may only say "checked your sent email" about a lane it really read.
    sent_threads: list[dict] | None
    try:
        sent_threads = heard.fetch_sent_threads(email, days=SENT_LOOKBACK_DAYS)
    except Exception:  # noqa: BLE001
        logger.info("promises: Gmail unavailable for %s", email, exc_info=True)
        sent_threads = None
    slack_messages: list[Any] | None
    try:
        slack_messages = heard.fetch_own_slack(
            email, since=now - timedelta(days=SENT_LOOKBACK_DAYS)
        )
    except Exception:  # noqa: BLE001
        logger.info("promises: Slack unavailable for %s", email, exc_info=True)
        slack_messages = None

    invoke = _invoker()
    with SessionLocal() as db:
        heard_stats = heard.hear(
            email,
            reader_name=getattr(user, "name", "") or "",
            db=db,
            invoke=invoke,
            deadline=deadline,
            sent_threads=sent_threads,
            slack_messages=slack_messages,
            now=now,
        )
        today = store.local_today(store.owner_timezone(db, email))
        check_stats = evidence.check(
            email,
            db=db,
            invoke=invoke,
            deadline=deadline,
            sent_threads=sent_threads,
            slack_messages=slack_messages,
            today=today,
            now=now,
        )
    result = {
        "status": "ok",
        **heard_stats,
        **{f"check_{k}": v for k, v in check_stats.items()},
        "seconds": round(time.monotonic() - started, 1),
    }
    logger.info("promises: accountability pass for %s: %s", email, result)
    return result

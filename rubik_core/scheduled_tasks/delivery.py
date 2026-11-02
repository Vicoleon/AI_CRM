"""Deliver a finished scheduled run to its owner.

The chat conversation is written by the runner and is always the canonical
copy; this module handles the extra channels the user asked for. Every channel
is best-effort and independently reported: a Slack outage must not turn a run
that produced a good answer into a failure, because the answer is already in
the user's chat.

Outcome vocabulary, recorded per channel on the run row:
``sent`` | ``skipped`` (nothing to deliver to — e.g. not in the Slack
workspace) | ``disabled`` (the channel's kill switch is off) | ``failed``.
"""

from __future__ import annotations

import html
import logging
import uuid
from datetime import datetime

from rubik_core.brand import PRODUCT_NAME
from rubik_core.config import settings

logger = logging.getLogger(__name__)

# Slack caps a section's mrkdwn at 3000 chars and rejects the WHOLE message if
# one block is over, so trim well below it and leave room for the footer.
_SLACK_TEXT_LIMIT = 2600


def deliver(
    *,
    task,
    run_id: str,
    recipient_email: str,
    title: str,
    answer_md: str,
    chat_session_id: str,
    local_when: str = "",
) -> dict:
    """Fan out to every extra channel on the task. Never raises."""
    channels = list(task.delivery_channels or [])
    outcome: dict[str, str] = {"chat": "sent"}

    if "slack" in channels:
        outcome["slack"] = _deliver_slack(
            recipient_email=recipient_email,
            title=title,
            answer_md=answer_md,
            chat_session_id=chat_session_id,
            local_when=local_when,
        )
    if "email" in channels:
        outcome["email"] = _deliver_email(
            recipient_email=recipient_email,
            title=title,
            answer_md=answer_md,
            chat_session_id=chat_session_id,
            local_when=local_when,
        )

    outcome["notification"] = notify(
        recipient_email=recipient_email,
        title=title,
        chat_session_id=chat_session_id,
        body=f"Your scheduled task “{title}” has a new result.",
    )
    logger.info(
        "[scheduled-tasks] delivery run=%s user=%s outcome=%s",
        run_id,
        recipient_email,
        outcome,
    )
    return outcome


def chat_link(chat_session_id: str) -> str:
    """Absolute link to the conversation holding the result.

    ``FRONTEND_URL`` defaults to localhost, so a worker deployed without it
    would mail dead links. The platform deploy sets the public origin on every
    Job for exactly this reason.
    """
    base = (settings.FRONTEND_URL or "").rstrip("/")
    return f"{base}/chat?session={chat_session_id}"


def _truncate(text: str, limit: int = _SLACK_TEXT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    newline = cut.rfind("\n")
    if newline >= limit * 0.6:
        cut = cut[:newline]
    # Never leave a half-open <url|label>: a dangling "<" is invalid Block Kit.
    if cut.count("<") > cut.count(">"):
        cut = cut[: cut.rfind("<")].rstrip()
    return cut.rstrip() + "\n…"


def _deliver_slack(
    *,
    recipient_email: str,
    title: str,
    answer_md: str,
    chat_session_id: str,
    local_when: str,
) -> str:
    try:
        from backend.services import slack_service

        bot = slack_service.SlackBotClient()
        if not bot.is_available:
            logger.info(
                "[scheduled-tasks] Slack bot unavailable — no DM for %s",
                recipient_email,
            )
            return "disabled"

        header = f"*{title}*" + (f" · {local_when}" if local_when else "")
        body = _truncate(answer_md or "")
        footer = f"\n\n<{chat_link(chat_session_id)}|Open in {PRODUCT_NAME}>"
        result = bot.send_dm(recipient_email, text=f"{header}\n\n{body}{footer}")
        if result is None:
            # Not a member of the Slack workspace — a roster fact, not an outage.
            return "skipped"
        return "sent"
    except Exception:  # noqa: BLE001 — the answer is already in their chat
        logger.exception(
            "[scheduled-tasks] Slack delivery failed for %s", recipient_email
        )
        return "failed"


def _deliver_email(
    *,
    recipient_email: str,
    title: str,
    answer_md: str,
    chat_session_id: str,
    local_when: str,
) -> str:
    try:
        from rubik_core.tools.email_sender import EmailMessageSpec, send_email
        from rubik_core.tools.gmail import _body_to_html

        link = chat_link(chat_session_id)
        subject = f"{title}" + (f" — {local_when}" if local_when else "")
        text_body = f"{answer_md}\n\nOpen in {PRODUCT_NAME}: {link}\n"
        # The title is the user's, but the answer is model output that may have
        # read outside email: escape everything, and render the answer with the
        # drafts' escape-first markdown subset (http/mailto links, no images).
        html_body = (
            f"<p><strong>{html.escape(title)}</strong>"
            + (f" · {html.escape(local_when)}" if local_when else "")
            + "</p><div>"
            + _body_to_html(answer_md or "")
            + f'</div><p><a href="{html.escape(link)}">Open in {PRODUCT_NAME}</a></p>'
        )
        # Impersonate the recipient so it lands as their own artifact. Keep this
        # feature's own autosend switch independent of Deep Prep.
        result = send_email(
            EmailMessageSpec(
                to=[recipient_email],
                subject=subject,
                text_body=text_body,
                html_body=html_body,
            ),
            impersonate=recipient_email,
            sender_name=PRODUCT_NAME,
            autosend=settings.SCHEDULED_TASKS_EMAIL_AUTOSEND,
        )
        return result.status
    except Exception:  # noqa: BLE001
        logger.exception(
            "[scheduled-tasks] email delivery failed for %s", recipient_email
        )
        return "failed"


def notify(
    *,
    recipient_email: str,
    title: str,
    chat_session_id: str | None,
    body: str,
    severity: str = "info",
) -> str:
    """Write one in-app notification row. A courtesy, never a contract.

    The TopBar bell is still a placeholder in the client, so this is storage
    for when it is wired; the Help page says as much rather than promising a
    badge that does not appear yet.
    """
    try:
        from rubik_core.database.db import SessionLocal
        from rubik_core.database.models import Notification

        with SessionLocal() as db:
            db.add(
                Notification(
                    id=str(uuid.uuid4()),
                    actor_email=recipient_email,
                    kind="scheduled_task_result",
                    title=title,
                    body=body,
                    severity=severity,
                    link=f"/chat?session={chat_session_id}"
                    if chat_session_id
                    else "/settings?tab=scheduled-tasks",
                    created_at=datetime.utcnow(),
                )
            )
            db.commit()
        return "sent"
    except Exception:  # noqa: BLE001
        logger.exception(
            "[scheduled-tasks] notification insert failed for %s", recipient_email
        )
        return "failed"

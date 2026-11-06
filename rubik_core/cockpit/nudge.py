"""The nudge — the sales supervisor's one button, and the record that it happened.

The role already exists: the sales supervisor is the one responsible for
chasing the AEs, and once the cockpit shows what to look for, they can own it.

A nudge is **three writes and all three are required**: a HubSpot task assigned
to the AE and associated to the deal, a Slack DM linking to the cockpit row, and
a ``commitment_nudges`` row. The third is not bookkeeping — it is the point. It
turns "I told them" into a record, and it makes *"nudged twice, still open"* an
escalation trigger rather than somebody's memory of a conversation.

Which is the rule this module enforces and the reason it refuses work:
**two unacknowledged nudges on one commitment is an ESCALATION, not a third
nudge.** The digest's rule 4 — escalation changes who is accountable, it does
not increase volume. A third DM to the same person about the same late SOW is how
the tsunami starts, and it is also an admission that nudging is not working.

**This runs in the REQUEST path, never in a job.** The write client resolves
the active workspace's bound ``hubspot/write_token`` secret and refuses Cloud
Run Jobs in code, even if that workspace has a token. A scheduled nudge would
need a deliberate change to that write policy.

Unlike the digest, a nudge is not gated on ``COCKPIT_DIGEST_ENABLED``: that flag
holds back scheduled VOLUME while the cockpit is read on screen for a week. A
nudge is one person pressing one button about one row they are looking at, and
the notification it produces is the entire action.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from rubik_core.brand import PRODUCT_NAME
from rubik_core.cockpit.business_hours import add_business_hours
from rubik_core.cockpit.digest import (
    _ACTION_LABELS,
    _KIND_LABELS,
    _cockpit_link,
    _short_date,
)
from rubik_core.config import settings
from rubik_core.database.commitments import Commitment, CommitmentNudge
from rubik_core.retrieval.hubspot_api import HubSpotAPIError

# ``_epoch_ms`` is private but shared on purpose: it already treats a naive
# datetime as UTC, which is exactly how every stamp in this database is stored,
# and a fourth local copy of epoch-millisecond conversion is how one of them
# ends up off by a timezone.
from rubik_core.retrieval.hubspot_write import (
    HubSpotWriteClient,
    HubSpotWriteError,
    _epoch_ms,
    friendly_write_error,
)

logger = logging.getLogger(__name__)

#: Rule 4's threshold. The third nudge does not exist.
MAX_UNACKNOWLEDGED_NUDGES = 2

# ── Named outcome buckets ────────────────────────────────────────────────────
REASON_ESCALATION_REQUIRED = "escalation_required"
REASON_ALREADY_CLOSED = "already_closed"
REASON_NO_CRM_RECORD = "no_crm_record"
REASON_SLACK_DELIVERED = "delivered"
REASON_SLACK_NOT_A_MEMBER = "not_a_slack_workspace_member"
REASON_SLACK_NO_BOT = "slack_bot_unavailable"
REASON_SLACK_FAILED = "failed"

# HubSpot task properties. Nothing here is guessed: every name is in
# ``TASK_WRITABLE`` (hubspot_write.py), the closed whitelist that is the repo's
# authority on what a write may touch, and every VALUE is from HubSpot's task
# API reference.
#
#   hs_timestamp     — the ONLY required task property, and it means the DUE
#                      DATE, not creation time.
#   hs_task_subject  — read live from this portal by rubik_core/xray/fetchers.py
#                      as the task title property.
#   hs_task_body     — whitelisted by the write layer, but not verified
#                      against the portal. The failure mode is loud rather than
#                      silent — an unknown property is a 400 that
#                      ``friendly_write_error`` renders for the sales
#                      supervisor, not a field that quietly vanishes — which is
#                      why the detail and the Cubie link live here instead of
#                      being crammed into the subject. TODO: verify it against
#                      the portal's own property list.
#   hs_task_status   — documented values are COMPLETED and NOT_STARTED ONLY.
#                      The HubSpot UI shows more (Waiting, Deferred); the API
#                      reference does not, so anything else must be checked
#                      against the portal's own property options first.
#   hs_task_priority — LOW | MEDIUM | HIGH.
#   hs_task_type     — EMAIL | CALL | TODO.
_TASK_STATUS_NOT_STARTED = "NOT_STARTED"
_TASK_PRIORITY_HIGH = "HIGH"
_TASK_TYPE_TODO = "TODO"

# Neither field's max length is documented, so cap conservatively rather than
# discover the limit as a 400 in front of the sales supervisor.
_SUBJECT_MAX = 255
_BODY_MAX = 2000


def nudge(
    db: Session,
    commitment: Commitment,
    *,
    from_email: str,
    to_email: str,
) -> dict:
    """Nudge the AE about one commitment. HubSpot task + Slack DM + a row.

    Returns a status dict with a named ``reason`` rather than a bare boolean, so
    the worklist can say *why* a button did nothing — "this needs escalating,
    not nudging" and "this deal has no HubSpot record yet" are different
    sentences and both are actionable.

    Order of the three writes, and it is deliberate:

    1. **HubSpot task first.** It is the durable, auditable limb: it survives
       Slack being down, it lands in the AE's own task list where they already
       work, and it is the one that can fail for a reason worth surfacing (a
       missing write scope). A failure here aborts the whole nudge, so nobody
       gets a DM about a task that does not exist.
    2. **Slack DM.** Metadata and a link only — never transcript text.
       Client-confidential content does not belong in Slack, and the sales
       supervisor does not get transcript text at all.
    3. **The row**, with ``sent_at`` stamped and committed immediately. Neither
       external write can be rolled back, so a caller's later failure must not
       erase the only record that they happened.

    A Slack DM that returns ``None`` because the AE is not a workspace member is
    a SKIP, not a failure: the task landed, the nudge is real, and the row
    records ``channel="hubspot_task"`` so "nudged twice" stays countable. It is
    reported in its own bucket, never rolled into an error count.

    Raises ``ValueError`` for a caller mistake (missing addresses); operational
    refusals come back as ``{"sent": False, "reason": ...}``.
    """
    sender = (from_email or "").strip().lower()
    recipient = (to_email or "").strip().lower()
    if not sender or not recipient:
        raise ValueError("nudge requires both from_email and to_email")
    if commitment is None:
        raise ValueError("nudge requires a commitment")

    def _result(*, sent: bool, reason: str | None, **extra) -> dict:
        return {
            "commitment_id": commitment.id,
            "from_email": sender,
            "to_email": recipient,
            "sent": sent,
            "reason": reason,
            **extra,
        }

    if commitment.closed_at is not None:
        return _result(sent=False, reason=REASON_ALREADY_CLOSED)

    # Rule 4. Counting only SENT rows: a row with no ``sent_at`` never reached
    # anybody, and letting it block a real nudge would silence the worklist on
    # the strength of a failed attempt.
    unacknowledged = (
        db.query(CommitmentNudge)
        .filter(
            CommitmentNudge.commitment_id == commitment.id,
            CommitmentNudge.sent_at.isnot(None),
            CommitmentNudge.acknowledged_at.is_(None),
        )
        .count()
    )
    if unacknowledged >= MAX_UNACKNOWLEDGED_NUDGES:
        # Not a failure and not a no-op: this is the trigger firing. The state
        # transition itself belongs to the engine's ladder (rule 5 — one
        # escalation state per commitment), so this reports rather than mutates.
        logger.info(
            "[cockpit-nudge] %s has %d unacknowledged nudges — escalation, not a "
            "third nudge",
            commitment.id,
            unacknowledged,
        )
        return _result(
            sent=False,
            reason=REASON_ESCALATION_REQUIRED,
            unacknowledged_nudges=unacknowledged,
            escalated_to=commitment.escalated_to,
        )

    deal_id = commitment.hubspot_deal_id
    company_id = commitment.hubspot_company_id
    if not deal_id and not company_id:
        # A call-sourced commitment can legitimately precede the company, and a
        # HubSpot task attached to nothing is invisible in the AE's context —
        # so this refuses instead of creating one. Resolve the company on the
        # cockpit row first; the clock keeps running either way.
        return _result(sent=False, reason=REASON_NO_CRM_RECORD)

    now = datetime.utcnow()
    label = _KIND_LABELS.get(commitment.kind, commitment.kind)
    ref = f"deal {deal_id}" if deal_id else f"company {company_id}"
    detail = (
        f"overdue since {_short_date(commitment.breach_at)}"
        if commitment.breach_at is not None
        else "age unknown — needs a start date"
    )
    action = _ACTION_LABELS.get(commitment.kind, f"Open it in {PRODUCT_NAME}")

    # hs_timestamp is the DUE date, so it has to be a date the AE can still act
    # on. A commitment that has ALREADY breached would otherwise produce a task
    # that is overdue the instant it appears, which reads as an accusation
    # rather than a reminder (``create_task``'s own docstring makes that point)
    # — and an overdue task lands in the same pile the AE is already ignoring.
    # So: the real deadline when there is still one, otherwise one business day,
    # through the package's single elapsed-time calculator rather than a raw
    # timedelta that would fall on a Saturday.
    breach_at = commitment.breach_at
    due = (
        breach_at
        if breach_at is not None and breach_at > now
        else add_business_hours(now, settings.COCKPIT_BUSINESS_DAY_HOURS)
    )
    subject = f"{PRODUCT_NAME}: {label} for {ref} — {detail}"[:_SUBJECT_MAX]
    link = _cockpit_link()
    body_lines = [
        f"{sender} flagged this in the {PRODUCT_NAME} sales cockpit.",
        "",
        f"{label} · {ref} · {detail}",
        action + ".",
    ]
    if link:
        body_lines += ["", f"Open the cockpit: {link}"]
    properties = {
        "hs_timestamp": _epoch_ms(due),
        "hs_task_subject": subject,
        # Metadata and a link. No evidence quote — the same rule as the Slack DM
        # below, and HubSpot is no more the right home for a client's words than
        # Slack is.
        "hs_task_body": "\n".join(body_lines)[:_BODY_MAX],
        "hs_task_status": _TASK_STATUS_NOT_STARTED,
        "hs_task_priority": _TASK_PRIORITY_HIGH,
        "hs_task_type": _TASK_TYPE_TODO,
    }

    client = HubSpotWriteClient()
    try:
        # Association is the writer's job (Task->Deal / Task->Company); the ids
        # live in the writer, not repeated here. Ownership is by EMAIL — there
        # is no stored HubSpot owner id, by design, so
        # the writer resolves it at call time through ``owner_directory()``.
        task = client.create_task(
            properties,
            owner_email=recipient,
            deal_id=deal_id,
            company_id=None if deal_id else company_id,
        )
    except (HubSpotWriteError, HubSpotAPIError) as exc:
        # Never let a raw HubSpot body reach a user or a row. The common cause
        # of a 403 here is a missing task write scope on the write app, and
        # ``friendly_write_error`` is the string that says so.
        #
        # ``HubSpotAPIError`` is caught alongside the write errors because the
        # write path's gates READ live state (the pipeline fence on the deal
        # association, the owner directory), so an unset READ token surfaces as
        # a read error from inside a write — a 500 for the supervisor if it is not
        # caught here.
        logger.warning(
            "[cockpit-nudge] HubSpot task failed for %s on %s: %s",
            recipient,
            commitment.id,
            exc,
        )
        raise ValueError(friendly_write_error("the nudge task", exc)) from exc

    task_id = str(task.get("id") or "") or None

    slack_state, error = _send_slack_dm(
        recipient,
        title=f"{label} · {ref}",
        detail=detail,
        action=action,
        from_email=sender,
    )

    channel = "both" if slack_state == REASON_SLACK_DELIVERED else "hubspot_task"
    row = CommitmentNudge(
        id=uuid.uuid4().hex,
        commitment_id=commitment.id,
        from_email=sender,
        to_email=recipient,
        channel=channel,
        hubspot_task_id=task_id,
        # Stamped because a write DID land — the HubSpot task. Leaving it null
        # would make this nudge uncountable, and "nudged twice, still open" is
        # the whole reason the table exists.
        sent_at=now,
        created_at=now,
    )
    db.add(row)
    db.commit()

    logger.info(
        "[cockpit-nudge] %s nudged %s on %s (task=%s, slack=%s)",
        sender,
        recipient,
        commitment.id,
        task_id,
        slack_state,
    )
    return _result(
        sent=True,
        reason=None,
        nudge_id=row.id,
        channel=channel,
        hubspot_task_id=task_id,
        slack=slack_state,
        slack_error=error,
        unacknowledged_nudges=unacknowledged + 1,
    )


def _send_slack_dm(
    recipient: str,
    *,
    title: str,
    detail: str,
    action: str,
    from_email: str,
) -> tuple[str, str | None]:
    """DM the AE about one row. Returns ``(named_state, error)``.

    Metadata and a link. No transcript text, no evidence quote — the cockpit row
    carries those, behind the cockpit's access rules.

    Imported inside the function because ``rubik_core/`` does not import
    ``backend/`` at module scope (docs/ENGINEERING_NOTES.md layering rule); the sanctioned shape
    is a lazy import in a function body.
    """
    from backend.agents.slack_delivery import _truncate_mrkdwn
    from backend.services.slack_service import SlackAPIError, SlackBotClient

    bot = SlackBotClient()
    if not bot.is_available:
        return REASON_SLACK_NO_BOT, None

    header = "A sales commitment needs your attention"
    body = (
        f"🔴 *{title}*\n{detail}\n_{action}_\n"
        f"Flagged by {from_email} in {PRODUCT_NAME}."
    )
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": header[:150]}},
        {"type": "section", "text": {"type": "mrkdwn", "text": _truncate_mrkdwn(body)}},
    ]
    fallback = f"{header}: {title} — {detail}. {action}."
    link = _cockpit_link()
    if link:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": f"Open {PRODUCT_NAME}",
                        },
                        "url": link,
                    }
                ],
            }
        )

    try:
        delivered = bot.send_dm(recipient, text=fallback, blocks=blocks)
    except SlackAPIError as exc:
        if exc.error == "invalid_blocks":
            # Slack posts NOTHING when it rejects a block set — not even the
            # text fallback — so degrade rather than drop the person.
            try:
                delivered = bot.send_dm(recipient, text=fallback)
            except SlackAPIError as exc2:
                return REASON_SLACK_FAILED, exc2.error
        else:
            return REASON_SLACK_FAILED, exc.error

    if delivered is None:
        # Not in the Slack workspace. A skip, in its own bucket: the task landed
        # and the nudge is on the record either way.
        return REASON_SLACK_NOT_A_MEMBER, None
    return REASON_SLACK_DELIVERED, None

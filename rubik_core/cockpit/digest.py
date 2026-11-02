"""The sales digest — the anti-tsunami constraint, as enforceable code.

**NO OTHER CODE PATH MAY SEND A SALES NOTIFICATION.** If a cockpit feature needs
to tell somebody something, it adds a row to what :func:`gather_actionable`
returns; it does not send. That is the rule, it is written here because it is
exactly the rule that erodes — a "quick" second notifier is how a sales team's
last notification system dies — and there is precisely one carve-out, named in
:func:`send_digest` so it cannot be cited as precedent for a second one.

Why this matters more than any feature in the cockpit: an SLA that sends an
alert per breach buries the team in notifications, and a team that is drowning
addresses none of them.

The five anti-tsunami rules, and where each one lives:

* **Rule 1 — one digest of EACH KIND per person per day.** Enforced by the
  database, not by a pre-check: ``UniqueConstraint(recipient_email, kind,
  local_date)`` on ``digest_logs``. Two scheduler executions racing both pass a
  pre-check and both send; only one wins an INSERT. An empty item list sends
  nothing and writes no row — silence is a feature, not a missed run.
* **Rule 2 — hard cap of** :data:`MAX_INAPP_ITEMS`. Overflow is a count, never
  a chevron. A list of twenty is a list of zero.
* **Rule 3** (one primary action per worklist row) belongs to the surfaces that
  render rows; this module carries the action LABEL per item so the wording is
  the same in Slack and on screen.
* **Rule 4 — escalation changes who is accountable, it does not add volume.**
  So a breached commitment that has moved to ``escalated`` / ``exec_visible``
  still appears in its OWNER's digest and nowhere else new: the manager sees it
  on their view, not in a second DM.
* **Rule 5** (one escalation state per commitment) is the engine's, not ours.

Two deliberate design notes a reviewer will otherwise flag:

**No elapsed-time arithmetic happens in this module**, even though every line it
renders is about time — it borrows only ``as_naive_utc`` from
``rubik_core/cockpit/business_hours.py``, which is the single calculator.
The digest needs no second copy of it: for a fixed
"now", business hours elapsed is monotonic in the raw interval, so ordering by
``breach_at`` / ``opened_at`` ascending is *identical* to ordering by
business-hours-elapsed descending. Every line therefore names a DATE
("overdue since 25 Aug") rather than restating an age — the age chip on the
cockpit row is where business-day units are shown, in the one place that
computes them.

**Two different clocks, on purpose.** ``local_date`` is the RECIPIENT's local
today, because keying the guard on UTC gives someone working in the evening a
second digest and someone early in the morning none. The SLA clock is the one
workspace business time zone, because an SLA that means something different
per AE is not auditable. These are different questions and they get
different answers.

Delivery is ``SlackBotClient.send_dm`` from ``backend/services/slack_service.py``
— the repo's one outbound Slack path — imported inside the function because
``rubik_core/`` does not import ``backend/`` at module scope. ``send_dm``
returning ``None`` for a non-workspace-member is a SKIP, not a failure, and it
gets its own named bucket.

Ignored on purpose: ``AgentRecommendation.slack_delivered_at`` and
``Department.slack_manager_channel`` — both dead columns.

**The one other sanctioned sales sender** is the Loose Ends channel digest,
``rubik_core/loop_closure/digest.py``: one post a day to ONE
shared channel (``#sales``, switched on and pointed by an admin in Admin →
Loose Ends; it ships off), never a DM, guarded by
``loop_digest_posts`` — UNIQUE ``(channel_key, local_date)``, the same
insert-flush-post-stamp-commit order as ``digest_logs`` — so it posts at most
once per channel per local day, and a quiet day posts nothing and writes no
row. It carries names, counts and ages only. It is named here so that neither
it nor the CCS wrap-up below can be cited as precedent for a third: anything
else that wants to tell a salesperson something still adds an item to
:func:`gather_actionable`.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from rubik_core.brand import PRODUCT_NAME
from rubik_core.cockpit.business_hours import as_naive_utc
from rubik_core.config import settings
from rubik_core.database.commitments import (
    DIGEST_KINDS,
    Commitment,
    CommitmentStageLag,
    DigestLog,
)
from rubik_core.priorities.temporal import user_today_str

logger = logging.getLogger(__name__)

#: Rule 2. Server-side, so no later feature can route around it by rendering
#: "just one more" row. Read once at import like every other tuning constant in
#: this package; changing it is a config change plus a restart, never a deploy.
MAX_INAPP_ITEMS: int = settings.COCKPIT_MAX_INAPP_ITEMS

# ── Item buckets, in digest order ────────────────────────────────────────────
# Ordering is "oldest breach first, then at-risk by age, then wrap-ups", with
# pipeline hygiene last: a stage-lag exception is a
# correction to a forecast, not a promise to a client that is running late.
BUCKET_BREACHED = "breached"
BUCKET_AT_RISK = "at_risk"
BUCKET_WRAPUP = "wrapup"
BUCKET_STAGE_LAG = "stage_lag"

_BUCKET_ORDER: dict[str, int] = {
    BUCKET_BREACHED: 0,
    BUCKET_AT_RISK: 1,
    BUCKET_WRAPUP: 2,
    BUCKET_STAGE_LAG: 3,
}

# Every state at or past a breach. All three stay in the OWNER's digest: rule 4
# says escalation moves accountability onto another person's view, so dropping
# the row from the AE's own list at exactly the moment it got worse would be the
# opposite of the intent.
_BREACHED_STATES: tuple[str, ...] = ("breached", "escalated", "exec_visible")

_KIND_LABELS: dict[str, str] = {
    "sow": "SOW",
    "wrapup": "Call wrap-up",
    "lead_touch": "Lead first touch",
}

# Rule 3's wording, chosen once so Slack and the screen say the same thing. Each
# is a single action that writes: closing, setting an ETA, approving a wrap-up,
# advancing a stage.
_ACTION_LABELS: dict[str, str] = {
    "sow": "Mark the SOW sent, or set an ETA with a reason",
    "wrapup": "Review the wrap-up",
    "lead_touch": "Log the first touch",
    BUCKET_STAGE_LAG: "Advance the stage, or say it is not right",
}

_STATE_DOT = {
    BUCKET_BREACHED: "🔴",
    BUCKET_AT_RISK: "🟡",
    BUCKET_WRAPUP: "🟢",
    BUCKET_STAGE_LAG: "⚠️",
}

_HEADERS = {
    "daily": "Your sales day",
    "weekly": "Sales aging — this week",
}

# Named outcome buckets. A caller aggregating a sweep reports these by name; a
# bare count of "not sent" hides a Slack-membership problem behind what looks
# like a quiet day, which is the failure the priorities delivery run spent three
# rounds of de-noising to fix.
REASON_COCKPIT_DISABLED = "cockpit_disabled"
REASON_DIGEST_DISABLED = "digest_disabled"
REASON_NOTHING_ACTIONABLE = "nothing_actionable"
REASON_ALREADY_SENT = "already_sent_today"
REASON_DRY_RUN = "dry_run"
REASON_NO_SLACK_BOT = "slack_bot_unavailable"
REASON_NOT_A_MEMBER = "not_a_slack_workspace_member"
REASON_SEND_FAILED = "send_failed"


def _naive_utc(value: datetime | None) -> datetime | None:
    """:func:`business_hours.as_naive_utc`, tolerating ``None``.

    Every nullable clock column on a commitment can legitimately be NULL — an
    unknown ``opened_at`` is a designed state, not a missing value — so the
    None-handling lives here rather than in the shared coercion.
    """
    return as_naive_utc(value) if value is not None else None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _short_date(value: datetime | None) -> str:
    """A date a human reads at a glance, e.g. "25 Aug"."""
    if value is None:
        return "date unknown"
    return f"{value.day} {value.strftime('%b')}"


def _cockpit_link(page: str = "sales") -> str:
    """Deep link into the cockpit, or "" when no frontend URL is configured.

    There is deliberately no per-row route. The frontend declares four cockpit
    pages and no detail route, so a
    ``?commitment=<id>`` param would be a link that silently does nothing —
    worse than a link to the block the row lives in, which the line already
    names.
    """
    base = (settings.FRONTEND_URL or "").rstrip("/")
    return f"{base}/{page}" if base.startswith("http") else ""


def gather_actionable(db: Session, owner_email: str) -> list[dict]:
    """Everything one person is expected to DO, oldest problem first.

    Uncapped on purpose: the caller applies :data:`MAX_INAPP_ITEMS` and reports
    the overflow as a count, and the cockpit's My Day needs the same list to cap
    its own blocks the same way. Returning three would make the "+4 more" count
    unknowable.

    Four buckets, in this order:

    1. **Breached** commitments (``breached`` / ``escalated`` / ``exec_visible``),
       oldest ``breach_at`` first.
    2. **At risk** — past 70% of the SLA, oldest first.
    3. **Wrap-ups** still open. A wrap-up ages as its own commitment kind
       rather than as a lock on anything: it lands on a worklist
       when it breaches instead of blocking the AE out of their own CRM.
    4. **Stage-lag exceptions** on deals this person has a live commitment on.

    Deliberately absent:

    * ``provisional`` commitments. The clock runs from the call whether or not
      the AE ever did the wrap-up, but an unreviewed AI guess does not get to
      generate a notification — that is what "escalation disarmed" means. The
      state filter here is what implements it, since the recompute sweep never
      moves a provisional row into an at-risk or breached state.
    * ``accepted`` breaches. Somebody took the hit on the record, with a reason.
      Re-raising it daily would make acceptance meaningless.
    * **Any transcript text.** ``opened_evidence_text`` and
      ``CommitmentStageLag.evidence_text`` are quotes from a client call. They
      are what makes the cockpit row persuasive and they are exactly what must
      not travel to Slack or email, so they are not read here at all.
    * **Deals the person deferred**, until the follow-up date — the same rule as
      My Day's deal blocks (``rubik_core/sales/deal_defer.py``). Their deal
      commitments and stage lags drop out; a wrap-up stays, because My Day's
      wrap-up block keeps it too (the call happened, and it decays).
    * **Company and deal NAMES.** Cubie stores neither (``graph_entities`` keys
      companies by domain, not by portal id), and resolving them would put a
      HubSpot read inside the delivery path — a digest that fails because
      HubSpot is rate-limited is worse than one that names the record by its id
      and links to the row. The ids are returned so the cockpit page, where a
      failed lookup degrades one screen instead of a whole sweep, can label
      them.
    """
    email = (owner_email or "").strip().lower()
    if not email:
        return []

    owned = func.lower(Commitment.owner_email) == email
    live = Commitment.closed_at.is_(None)

    rows: list[Commitment] = (
        db.query(Commitment)
        .filter(
            owned,
            live,
            or_(
                Commitment.state.in_(_BREACHED_STATES),
                Commitment.state == "at_risk",
                # A wrap-up is actionable while it is merely open — it is the
                # one kind whose whole point is "do this today".
                and_(Commitment.kind == "wrapup", Commitment.state == "open"),
            ),
        )
        .all()
    )

    items: list[dict] = [_commitment_item(row) for row in rows]

    # Stage lags carry no owner — only a deal id — and resolving a HubSpot deal
    # owner is a runtime lookup with a TTL cache. Cubie can answer the
    # question locally for the deals that matter: a deal this person already
    # holds a live commitment on is a deal they are working. A stage lag on a
    # deal with NO live Cubie commitment is not attributable to an AE from local
    # data, so it surfaces in the cockpit's pipeline-hygiene block (which
    # resolves the owner through HubSpot) and never in a digest — better absent
    # from a DM than sent to the wrong person.
    deal_ids = {
        d
        for (d,) in db.query(Commitment.hubspot_deal_id)
        .filter(owned, live, Commitment.hubspot_deal_id.isnot(None))
        .distinct()
        .all()
    }
    if deal_ids:
        lags: list[CommitmentStageLag] = (
            db.query(CommitmentStageLag)
            .filter(
                CommitmentStageLag.status == "open",
                CommitmentStageLag.hubspot_deal_id.in_(deal_ids),
            )
            .all()
        )
        items += [_stage_lag_item(row) for row in lags]

    items = _without_deferred(db, email, items)

    # One sort, on raw timestamps. See the module docstring: with a fixed "now"
    # this is the same order business hours would produce, without a second
    # elapsed-time calculator. ``datetime.max`` puts an unknown date last rather
    # than first — a row with no clock must never outrank a real breach.
    items.sort(
        key=lambda it: (
            _BUCKET_ORDER.get(it["bucket"], 99),
            it["_sort_at"] or datetime.max,
        )
    )
    for it in items:
        it.pop("_sort_at", None)
    return items


#: Commitment kinds My Day shows in their own blocks, not as deal rows — a
#: defer does not hide them (``backend/routers/cockpit.py``).
_NOT_DEAL_ROWS: tuple[str, ...] = ("wrapup", "lead_touch")


def _without_deferred(db: Session, email: str, items: list[dict]) -> list[dict]:
    """Drop the deal rows this person deferred. Fails open, like every reader."""
    from rubik_core.sales.deal_defer import deferred_deal_ids

    def deal_row(it: dict) -> bool:
        return bool(it.get("deal_id")) and it.get("kind") not in _NOT_DEAL_ROWS

    hidden = deferred_deal_ids(
        {str(it["deal_id"]) for it in items if deal_row(it)}, reader=email, db=db
    )
    if not hidden:
        return items
    return [it for it in items if not (deal_row(it) and str(it["deal_id"]) in hidden)]


def _commitment_item(row: Commitment) -> dict:
    """One commitment as a renderable, JSON-safe item."""
    if row.state in _BREACHED_STATES:
        bucket = BUCKET_BREACHED
        sort_at = _naive_utc(row.breach_at)
        detail = f"overdue since {_short_date(row.breach_at)}"
        if row.state == "escalated" and row.escalated_to:
            # Rule 4 made visible: the row says who now owns it, so the AE
            # learns it moved from the digest they already get rather than from
            # a second alert about the same commitment.
            detail += f" · with {row.escalated_to}"
        elif row.state == "exec_visible":
            detail += " · now in the exec count"
    elif row.state == "at_risk":
        bucket = BUCKET_AT_RISK
        sort_at = _naive_utc(row.opened_at)
        detail = f"due {_short_date(row.breach_at)}"
    else:
        bucket = BUCKET_WRAPUP
        sort_at = _naive_utc(row.opened_at)
        detail = f"call ended {_short_date(row.opened_at)}"

    ref = (
        f"deal {row.hubspot_deal_id}"
        if row.hubspot_deal_id
        else f"company {row.hubspot_company_id}"
        if row.hubspot_company_id
        else "no CRM record yet"
    )
    return {
        "id": row.id,
        "source": "commitment",
        "bucket": bucket,
        "kind": row.kind,
        "state": row.state,
        "title": f"{_KIND_LABELS.get(row.kind, row.kind)} · {ref}",
        "detail": detail,
        "company_id": row.hubspot_company_id,
        "deal_id": row.hubspot_deal_id,
        "opened_at": _iso(row.opened_at),
        "breach_at": _iso(row.breach_at),
        "sla_hours": row.sla_hours,
        "action_label": _ACTION_LABELS.get(row.kind, f"Open it in {PRODUCT_NAME}"),
        "link": _cockpit_link(),
        "_sort_at": sort_at,
    }


def _stage_lag_item(row: CommitmentStageLag) -> dict:
    """One stage-lag exception, without the quote that makes it convincing."""
    return {
        "id": row.id,
        "source": "stage_lag",
        "bucket": BUCKET_STAGE_LAG,
        "kind": BUCKET_STAGE_LAG,
        "state": row.status,
        "title": f"Stage disagrees with the evidence · deal {row.hubspot_deal_id}",
        # No quote, and no stage LABELS either: mapping a stage id to its label
        # means ``deal_pipeline_stages()`` over the network, and the ids are
        # meaningless to a reader anyway. The cockpit card resolves both.
        "detail": f"what {PRODUCT_NAME} heard does not match the HubSpot stage",
        "company_id": None,
        "deal_id": row.hubspot_deal_id,
        "hubspot_stage_id": row.hubspot_stage_id,
        "evidence_stage_id": row.evidence_stage_id,
        "confidence": row.confidence,
        "opened_at": _iso(row.created_at),
        "breach_at": None,
        "action_label": _ACTION_LABELS[BUCKET_STAGE_LAG],
        "link": _cockpit_link(),
        "_sort_at": _naive_utc(row.created_at),
    }


def send_digest(
    db: Session,
    owner_email: str,
    *,
    kind: str,
    now: datetime | None = None,
    dry_run: bool = False,
) -> dict:
    """Send one person one digest of one kind for their local today, or nothing.

    Returns a status dict with a NAMED reason, never a bare boolean: a sweep
    reporting "12 sent, 4 not" cannot tell a quiet day from four people who are
    not in the Slack workspace.

    **The one carve-out in the whole feature, and it is named here so it cannot
    be cited as precedent:** the CCS end-of-call wrap-up prompt goes to the
    call's OWN owner, at the moment their call ends, once. It is not a digest,
    it does not pass through this module, and nothing else gets the same
    exemption. Anything else that wants to tell a salesperson something adds an
    item to :func:`gather_actionable`.

    **This function owns its transaction**, which is the only way the
    exactly-once ordering works (``digest_logs`` class docstring, mirroring
    ``eos_l10_sessions.summary_email_sent_at``):

    1. INSERT + ``flush`` — claims the person-kind-day slot. The UNIQUE
       constraint decides a race between two scheduler executions; there is no
       pre-check, because both racers pass a pre-check and both send.
    2. Send.
    3. Stamp ``sent_at`` and ``commit`` — only after a CONFIRMED send.
    4. Any failure or skip rolls back, so the row is absent and the next sweep
       retries. A committed row with a NULL ``sent_at`` would be a claim that
       somebody was told when they were not, which is precisely the evidence
       they will be shown when they say nobody told them.

    So pass a session with no other pending work in it.

    ``dry_run=True`` renders what WOULD be sent, writes nothing, and ignores the
    two feature flags — inspecting the output is how anyone decides whether to
    flip ``COCKPIT_DIGEST_ENABLED``, so refusing to render while it is off would
    make the flag un-reviewable.
    """
    if kind not in DIGEST_KINDS:
        raise ValueError(
            f"not a digest kind: {kind!r} (expected one of {DIGEST_KINDS})"
        )

    email = (owner_email or "").strip().lower()
    if not email:
        raise ValueError("send_digest requires a recipient email")

    now_utc = _naive_utc(now) or datetime.utcnow()
    local_date = user_today_str(email, now_utc=now)

    def _result(*, sent: bool, reason: str | None, **extra) -> dict:
        return {
            "recipient": email,
            "kind": kind,
            "local_date": local_date,
            "sent": sent,
            "reason": reason,
            **extra,
        }

    # Both switches, not just the digest one. A DM that links into a cockpit
    # whose router returns 404 is worse than no DM: the reader concludes the
    # feature is broken, and they are right.
    if not dry_run and not settings.COCKPIT_ENABLED:
        return _result(sent=False, reason=REASON_COCKPIT_DISABLED)
    if not dry_run and not settings.COCKPIT_DIGEST_ENABLED:
        return _result(sent=False, reason=REASON_DIGEST_DISABLED)

    items = gather_actionable(db, email)
    if not items:
        # RULE 1. No message and NO ROW: silence is a feature, and a row here
        # would also burn the day's slot, so a commitment that breaches at 11am
        # would wait until tomorrow to be mentioned.
        return _result(
            sent=False,
            reason=REASON_NOTHING_ACTIONABLE,
            item_count=0,
            suppressed_count=0,
        )

    shown = items[:MAX_INAPP_ITEMS]
    suppressed = items[MAX_INAPP_ITEMS:]
    fallback, blocks = _render(kind, shown, len(suppressed))

    if dry_run:
        return _result(
            sent=False,
            reason=REASON_DRY_RUN,
            item_count=len(shown),
            suppressed_count=len(suppressed),
            items=shown,
            preview_text=fallback,
        )

    from backend.services.slack_service import SlackAPIError, SlackBotClient

    bot = SlackBotClient()
    if not bot.is_available:
        # No workspace bot token. Not this person's problem and not a
        # per-recipient failure — no row, so the whole roster retries once the
        # app is installed.
        return _result(
            sent=False,
            reason=REASON_NO_SLACK_BOT,
            item_count=len(shown),
            suppressed_count=len(suppressed),
        )

    row = DigestLog(
        id=uuid.uuid4().hex,
        recipient_email=email,
        kind=kind,
        local_date=local_date,
        channel="slack",
        item_count=len(shown),
        suppressed_count=len(suppressed),
        # Ids and buckets only. The digest never quotes transcript text, so
        # neither does its own audit trail — and the suppressed ids are half the
        # answer to "I never got told": they say what was held back, not just
        # how much.
        detail={
            "shown": [
                {"id": i["id"], "source": i["source"], "bucket": i["bucket"]}
                for i in shown
            ],
            "suppressed": [
                {"id": i["id"], "source": i["source"], "bucket": i["bucket"]}
                for i in suppressed
            ],
        },
        created_at=now_utc,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        logger.info(
            "[cockpit-digest] %s already has a %s digest for %s — skipping",
            email,
            kind,
            local_date,
        )
        return _result(sent=False, reason=REASON_ALREADY_SENT)

    try:
        delivered = bot.send_dm(email, text=fallback, blocks=blocks)
    except SlackAPIError as exc:
        if exc.error == "invalid_blocks":
            # Block Kit rejected the rich message and Slack posts NOTHING in
            # that case — not even the text fallback. Truncation should prevent
            # it; degrade to text-only rather than drop the person silently.
            logger.warning(
                "[cockpit-digest] invalid_blocks for %s — retrying text-only", email
            )
            try:
                delivered = bot.send_dm(email, text=fallback)
            except SlackAPIError as exc2:
                db.rollback()
                return _result(sent=False, reason=REASON_SEND_FAILED, error=exc2.error)
        else:
            db.rollback()
            return _result(sent=False, reason=REASON_SEND_FAILED, error=exc.error)

    if delivered is None:
        # Not a workspace member. A SKIP, in its own bucket: counting it as a
        # failure sends ops chasing an outage that is really one roster row, and
        # counting it as a send tells them somebody was informed who was not.
        db.rollback()
        logger.info(
            "[cockpit-digest] %s is not a Slack workspace member — skipped", email
        )
        return _result(
            sent=False,
            reason=REASON_NOT_A_MEMBER,
            item_count=len(shown),
            suppressed_count=len(suppressed),
        )

    row.sent_at = now_utc
    db.commit()
    logger.info(
        "[cockpit-digest] sent %s digest to %s for %s (%d shown, %d suppressed)",
        kind,
        email,
        local_date,
        len(shown),
        len(suppressed),
    )
    return _result(
        sent=True,
        reason=None,
        digest_log_id=row.id,
        channel="slack",
        item_count=len(shown),
        suppressed_count=len(suppressed),
    )


def _render(
    kind: str, shown: list[dict], suppressed_count: int
) -> tuple[str, list[dict]]:
    """Render the DM: ``(fallback_text, blocks)``, both always passed to Slack.

    Every renderer in this repo returns the pair and every send passes both —
    the text is the notification preview AND the body if the blocks ever have to
    be dropped.

    One Slack channel, not two. The rules allow "a Slack DM and/or email"; a
    person receiving both gets two interruptions carrying identical information,
    which is the tsunami in miniature. ``digest_logs.channel`` records which one
    was used so adding email later is a change of value, not of shape.
    """
    from backend.agents.slack_delivery import _truncate_mrkdwn

    header = _HEADERS.get(kind, "Your sales day")
    blocks: list[dict] = [
        # Slack caps a header's plain_text at 150.
        {"type": "header", "text": {"type": "plain_text", "text": header[:150]}}
    ]
    fallback_lines = [header]

    for item in shown:
        dot = _STATE_DOT.get(item["bucket"], "•")
        line = f"{dot} *{item['title']}*\n{item['detail']}\n_{item['action_label']}_"
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": _truncate_mrkdwn(line)},
            }
        )
        fallback_lines.append(f"{dot} {item['title']} — {item['detail']}")

    if suppressed_count > 0:
        # RULE 2. A count, and it stays a count: the link goes to the full list
        # in Cubie. An inline expander would defeat the cap with a chevron.
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"+{suppressed_count} more in {PRODUCT_NAME}",
                    }
                ],
            }
        )
        fallback_lines.append(f"+{suppressed_count} more in {PRODUCT_NAME}")

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
    return "\n".join(fallback_lines), blocks

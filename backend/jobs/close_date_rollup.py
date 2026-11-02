"""Weekly close-date roll-up — the pipeline question, asked once a week.

    python -m backend.jobs.close_date_rollup [--dry-run]

A sales leader reading the pipeline asks the team: "you have an estimated
close date of 9/30, it's been three weeks since you engaged the client, why do
you think it'll close by 9/30?". The morning card
(``close_date_at_risk``) now asks the rep. This asks the MANAGER, weekly, about
the whole team at once — including the band that is deliberately too quiet to
interrupt a rep over, and the deals no rep's briefing can reach.

WHY IT DOES NOT RIDE THE EXISTING MANAGER DIGEST. That digest aggregates each
member's PERSISTED HomeCard (``_load_bundle_priority_items`` →
``home._load_today_priorities``), so it can only ever show something that
already became a card. The watch band never becomes one. It is also addressed
to a department's L10 manager, which cannot address an org-wide exec, and it
aborts wholesale when the nightly priorities run did not produce cards — which
would cancel a HubSpot-sourced roll-up for a reason that has nothing to do with
it.

WHY IT RUNS ITS OWN QUERY rather than reusing the per-rep sweeps. Those iterate
Cubie's active users and return nothing for anyone without a matching HubSpot
owner id — so a deal owned by a departed rep whose book was never reassigned is
invisible on every per-rep path. Those are exactly the deals with the least
credible dates. One team-wide search, once a week, costs one or two API calls,
plus one association read and one company read for the flagged deals' clients.

IT IS READ-ONLY, DELIBERATELY. ``resolve_entity_everywhere`` and
``reopen_entity_everywhere`` take no ``user_email`` — they update the row for
EVERY user who holds that (entity, kind) pair. A manager pressing a button here
would silence the rep who owns the deal. So this module renders header, context
and section blocks only: no ``action_id``, no Cubie deep link, and it imports
nothing from ``rubik_core.priorities.memory``. Rows link to HubSpot or nowhere.
Keep it that way — a "mark handled for the team" verb is the one change that
would turn this into the bug it was written to avoid.

DEFERRED DEALS ARE LISTED, NEVER HIDDEN. A rep can defer a deal (set it aside
until a follow-up date, with a note — ``rubik_core/sales/deal_defer.py``), which
takes it off their own lists. It does not take it off this one: a deferred deal
leaves the ranking and appears in its own "Deferred by their reps" section with
its owner, follow-up date, note, and — when it would have been flagged — what
its close date looks like. That section is the reason a defer cannot be used to
bury a bad deal. Reading the defers is still read-only, and it never writes.

No LLM call: ranking is arithmetic and the rendering is an f-string, so there
is no new AI Cost category. Adding a model here would buy a cost line, a
latency budget and a hallucination surface for five rows.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Same as the sibling jobs: without this the root logger sits at WARNING on
# Cloud Run and every INFO line below — the dry-run rendering, the end-of-run
# summary — is silently dropped. Found on the first real dry-run.
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1

# Rows in the message. The ask was "the 5 least credible close dates".
MAX_ROWS = 5

# Deferred deals listed by name; the rest are a count. Every one is named in the
# count, so the cap shortens the message, never the record.
MAX_DEFERRED_ROWS = 10

# How much of a rep's defer note a line quotes. The note is the reason, and the
# first sentence or two is what a reader on a phone takes in.
_NOTE_CHARS = 160

# The properties the ranking needs. Deliberately the same set the per-rep probe
# asks for, so the two readers cannot disagree about a deal's facts.
_PROPS = (
    "dealname",
    "amount",
    "dealstage",
    "hubspot_owner_id",
    "closedate",
    "notes_last_contacted",
    "pipeline",
)


@dataclass
class RollupRow:
    deal_id: str
    deal_name: str
    owner: str
    stage: str
    close_date: str
    days_to_close: int
    #: Days since contact was logged on the DEAL itself. The tier reads this
    #: and only this, so the rep's card and this list agree on every deal.
    quiet_days: int | None
    tier: int
    amount: float
    #: The client company, when its record was contacted more recently than
    #: the deal (an email to someone at the client who is not on the deal is
    #: logged there): its name and days since. Shown beside ``quiet_days``,
    #: never instead of it.
    client_name: str | None = None
    client_quiet_days: int | None = None
    #: The client was contacted inside the quiet threshold: only the deal
    #: record is quiet, so the row ranks after its tier's quiet-client rows.
    deal_only: bool = False

    @property
    def silence_days(self) -> int | None:
        """The newer of the deal's and its client's silence."""
        if self.client_quiet_days is not None:
            return self.client_quiet_days
        return self.quiet_days


@dataclass
class DeferredRow:
    """A deal its rep deferred: out of the ranking, never out of the message."""

    deal_id: str
    deal_name: str
    owner: str
    follow_up_on: str
    note: str
    #: What its close date looks like when it would have been flagged
    #: ("close date passed 20d ago"), else None.
    flag: str | None = None


@dataclass
class RollupResult:
    status: str = "ok"  # ok | disabled | fetch_failed | no_recipients | skipped
    checked: int = 0
    rows: list[RollupRow] = field(default_factory=list)
    tier_counts: dict[int, int] = field(default_factory=dict)
    uncontacted: int = 0
    # Past their close date AND silent beyond the 180-day ceiling. Clean-up,
    # not forecast: counted so the manager knows the book needs grooming,
    # never ranked, so a 1.6-years-dead record cannot outrank a live one.
    abandoned: int = 0
    #: Open deals the sweep read — the denominator for the deferred count.
    open_deals: int = 0
    #: Deals their reps deferred, follow-up date soonest first. Listed in their
    #: own section; excluded from ``rows`` and ``tier_counts``.
    deferred: list[DeferredRow] = field(default_factory=list)
    recipients: list[str] = field(default_factory=list)
    sent: int = 0
    # Named buckets, never a rolled-up "errors": already_sent is the guard
    # working, not_in_slack is not this person's problem, and errors are the
    # only bucket worth waking somebody for.
    already_sent: int = 0
    not_in_slack: int = 0
    truncated: bool = False
    #: The flagged deals' client companies could not be read, so the rows
    #: stand on each deal's own date and the message says what that misses.
    client_records_unread: bool = False
    errors: list[str] = field(default_factory=list)


def _owner_label(entry) -> str:
    """A person's name for the message, from whatever shape the owner directory
    hands back. `owner_directory()` maps id -> {"email", "name"}; the first real
    dry-run printed that dict verbatim on every row. Name first (this is read by
    a human on a phone), email if there is no name, "unassigned" if there is no
    owner at all — and that last case is deliberately kept, not dropped: a deal
    whose rep has left is the coverage this roll-up adds over the per-rep card."""
    if not entry:
        return "unassigned"
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return (entry.get("name") or entry.get("email") or "unassigned").strip()
    return str(entry)


def _sort_key(r: RollupRow) -> tuple:
    """Least credible first.

    Tier leads, because a date that has already passed is wrong in a way no
    amount of silence on a future date is. Within a tier the longer problem
    ranks first, and money only breaks ties — a large healthy deal above a
    small dead one is how a list like this stops being read.

    In the two silence tiers, a deal whose client was contacted inside the
    quiet threshold (only the deal record is quiet) ranks after the deals
    whose whole client has gone quiet, and silence is the newer of the deal's
    and its client's. A passed date ranks on how long ago it passed, whoever
    the client has heard from since.
    """
    if r.tier == 1:
        return (r.tier, False, -abs(r.days_to_close), -(r.amount or 0.0))
    return (r.tier, r.deal_only, -(r.silence_days or 0), -(r.amount or 0.0))


def _with_clients(client, res: RollupResult, now: datetime) -> None:
    """Add each flagged deal's client company contact, when newer than its own.

    Read only for the rows that are quiet on their own (nothing logged on the
    deal, or nothing inside the watch window) — one association read and one
    company read for the lot. A failure leaves every row on its deal's own
    date, as before, and sets ``client_records_unread`` so the message says
    so. Never raises.
    """
    from backend.signals.hubspot import (
        _close_date_quiet_days,
        _close_date_watch_days,
        _to_datetime,
    )
    from rubik_core.logsafe import safe_error
    from rubik_core.sales.rhythm_checks import client_contacts

    watch = _close_date_watch_days()
    quiet_rows = [
        r
        for r in res.rows
        if r.deal_id and (r.quiet_days is None or r.quiet_days >= watch)
    ]
    if not quiet_rows:
        return
    try:
        clients = client_contacts(client, [r.deal_id for r in quiet_rows])
    except Exception as exc:  # noqa: BLE001 — the deal-only rows still stand
        # safe_error, never exc_info: these are authenticated httpx calls.
        logger.warning(
            "[close-date-rollup] client company read failed: %s", safe_error(exc)
        )
        res.client_records_unread = True
        return
    threshold = _close_date_quiet_days()
    for r in quiet_rows:
        contact = clients.get(r.deal_id)
        last = contact.last_contact if contact else None
        if last is None:
            continue
        days = max((now - _to_datetime(last)).days, 0)
        # Only a NEWER client date says anything the deal's own does not.
        if r.quiet_days is not None and days >= r.quiet_days:
            continue
        r.client_name = contact.name
        r.client_quiet_days = days
        r.deal_only = days < threshold


def collect_rows() -> RollupResult:
    """One team-wide sweep of open deals carrying a close date."""
    from backend.routers.cockpit import _pipeline_filter
    from backend.signals.hubspot import _exceeds_max_age, _to_datetime, close_date_tier
    from rubik_core.retrieval.hubspot_api import (
        _OPEN,
        HubSpotAPIClient,
        HubSpotAPIError,
        _plausible_date,
    )
    from rubik_core.tenancy.hubspot_config import get_hubspot_config

    res = RollupResult()
    try:
        allowed_pipelines = get_hubspot_config().pipeline_ids
        if not allowed_pipelines:
            res.status = "skipped"
            res.errors.append(
                "No HubSpot deal pipelines are configured for this workspace"
            )
            return res
        client = HubSpotAPIClient()
        owners = client.owner_directory() or {}
        stages = client.deal_stage_labels() or {}
        # No owner clause — that is the whole point. The pipeline allowlist is
        # NOT optional: a portal can also host a client's pipeline that the
        # company's execs own as consultants, and leaking those into the roll-up
        # would show its reader another company's deals as their team's.
        deals = client.search_all(
            "deals", [_OPEN] + _pipeline_filter(allowed_pipelines), list(_PROPS)
        )
    except HubSpotAPIError as e:
        # Never an all-clear. A swallowed failure here hands the reader a
        # confident "nothing to flag" during an outage, which is worse than
        # sending nothing at all.
        logger.error("[close-date-rollup] HubSpot fetch failed: %s", e)
        res.status = "fetch_failed"
        res.errors.append(str(e))
        return res
    except Exception as e:  # noqa: BLE001
        logger.exception("[close-date-rollup] could not reach HubSpot")
        res.status = "fetch_failed"
        res.errors.append(str(e))
        return res

    if len(deals) >= getattr(HubSpotAPIClient, "MAX_RECORDS", 10_000):
        # A short read presented as the whole book is a silent lie.
        res.truncated = True

    deals = [
        deal
        for deal in deals
        if str((deal.get("properties") or {}).get("pipeline") or "")
        in allowed_pipelines
    ]

    from rubik_core.sales.deal_defer import active_by_deal, org_today

    # Every open defer, read once. Fails open to {}: then nothing is listed as
    # deferred and every deal is ranked as before — for a manager's view the
    # safe direction is "shown", never "hidden".
    defers = active_by_deal(None, today=org_today())

    res.open_deals = len(deals)
    now = datetime.now(timezone.utc)
    for d in deals:
        p = d.get("properties", {}) or {}
        deal_id = str(p.get("hs_object_id") or d.get("id") or "")
        close_date = _plausible_date(p.get("closedate"))
        info = defers.get(deal_id)
        if info is not None:
            res.deferred.append(
                _deferred_row(info, p, owners, close_date=close_date, now=now)
            )
        if not close_date:
            continue
        res.checked += 1
        if info is not None:
            continue  # listed in its own section, never ranked out of sight
        contacted = _plausible_date(p.get("notes_last_contacted"))
        quiet = (now - _to_datetime(contacted)).days if contacted else None
        if quiet is None:
            res.uncontacted += 1
        days_to_close = (_to_datetime(close_date).date() - now.date()).days
        tier = close_date_tier(days_to_close, quiet)
        if tier is None:
            if days_to_close < 0 and quiet is not None and _exceeds_max_age(int(quiet)):
                res.abandoned += 1
            continue
        res.tier_counts[tier] = res.tier_counts.get(tier, 0) + 1
        owner_id = str(p.get("hubspot_owner_id") or "")
        try:
            amount = float(p.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0.0
        res.rows.append(
            RollupRow(
                deal_id=deal_id,
                deal_name=(p.get("dealname") or "(no deal name)").strip(),
                # An unknown owner id still renders — a departed rep's book is
                # the coverage this roll-up exists to add.
                owner=_owner_label(owners.get(owner_id)),
                stage=str(
                    stages.get(str(p.get("dealstage")), p.get("dealstage") or "")
                ),
                close_date=close_date[:10],
                days_to_close=days_to_close,
                quiet_days=quiet,
                tier=tier,
                amount=amount,
            )
        )
    _with_clients(client, res, now)
    res.rows.sort(key=_sort_key)
    res.deferred.sort(key=lambda r: (r.follow_up_on, r.deal_name.lower()))
    return res


def _deferred_row(info, p: dict, owners: dict, *, close_date, now) -> DeferredRow:
    """One deferred deal, with its close-date problem when it has one.

    The tier is computed exactly as for a ranked row, so "what would this deal
    have said" is the same arithmetic, just shown beside the rep's note.
    """
    from backend.signals.hubspot import _to_datetime, close_date_tier
    from rubik_core.retrieval.hubspot_api import _plausible_date

    flag = None
    if close_date:
        contacted = _plausible_date(p.get("notes_last_contacted"))
        quiet = (now - _to_datetime(contacted)).days if contacted else None
        days_to_close = (_to_datetime(close_date).date() - now.date()).days
        tier = close_date_tier(days_to_close, quiet)
        if tier == 1:
            flag = f"close date passed {abs(days_to_close)}d ago"
        elif tier is not None:
            silence = (
                f"no client contact in {quiet}d"
                if quiet is not None
                else "no client contact ever logged"
            )
            flag = f"closes {close_date[:10]} with {silence}"
    owner = _owner_label(owners.get(str(p.get("hubspot_owner_id") or "")))
    if owner == "unassigned":
        owner = info.owner_email
    return DeferredRow(
        deal_id=info.deal_id,
        deal_name=(p.get("dealname") or info.deal_name or "(no deal name)").strip(),
        owner=owner,
        follow_up_on=info.follow_up_on.isoformat(),
        note=info.note,
        flag=flag,
    )


def _row_line(r: RollupRow) -> str:
    from backend.routers.cockpit import _hubspot_deal_url

    url = _hubspot_deal_url(r.deal_id)
    name = f"<{url}|{r.deal_name}>" if url else r.deal_name
    if r.tier == 1:
        head = f"closed {abs(r.days_to_close)}d ago on paper"
    else:
        head = f"closes {r.close_date} ({r.days_to_close}d)"
    if r.client_quiet_days is None:
        tail = (
            f"no client contact in {r.quiet_days}d"
            if r.quiet_days is not None
            else "no client contact ever logged"
        )
    else:
        # The deal's own number always stays on the row, beside the client's.
        on_deal = (
            "nothing ever logged on this deal"
            if r.quiet_days is None
            else f"nothing logged on this deal in {r.quiet_days}d"
        )
        if r.deal_only:
            c = r.client_quiet_days
            ago = "today" if c <= 0 else "yesterday" if c == 1 else f"{c}d ago"
            tail = f"{r.client_name or 'the client'} contacted {ago}, {on_deal}"
        else:
            tail = f"no client contact in {r.client_quiet_days}d ({on_deal})"
    money = f" · ${r.amount:,.0f}" if r.amount else ""
    return f"*{name}* — {head}, {tail}\n_{r.owner} · {r.stage}_{money}"


def build_blocks(res: RollupResult) -> tuple[str, list[dict]]:
    """The Monday message. Read-only by construction: no actions block."""
    from backend.agents.slack_delivery import _truncate_mrkdwn

    shown = res.rows[:MAX_ROWS]
    header = "Close dates worth a question this week"
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": header}}
    ]
    lines = [header]

    if not shown:
        # A good week must still say something. Silence reads as "it broke",
        # and a reader who thinks it broke switches it off.
        body = (
            f"Nothing to flag. I checked {res.checked} open deals with a close "
            "date and every one has either a date still ahead of it or recent "
            "client contact behind it."
        )
        if res.deferred:
            body = (
                f"Nothing to flag among the deals still on the reps' lists. I "
                f"checked {res.checked} open deals with a close date; the "
                "deferred ones are listed below, whatever their dates say."
            )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": body}})
        lines.append(body)
        _append_deferred(res, blocks, lines)
        return "\n".join(lines), blocks

    t1 = res.tier_counts.get(1, 0)
    t2 = res.tier_counts.get(2, 0)
    t3 = res.tier_counts.get(3, 0)
    # Tier 2 is "nothing logged on the deal in 3+ weeks". When some of those
    # deals' clients were contacted inside that window (logged on the client's
    # company, not the deal), "the client quiet 3+ weeks" would be untrue of
    # them, so the line says what was measured and how many it does not mean.
    t2_deal_only = sum(1 for r in res.rows if r.tier == 2 and r.deal_only)
    soon = (
        f"{t2} closing soon with no contact logged on the deal in 3+ weeks "
        f"({t2_deal_only} of those {t2} with the client contacted inside those "
        "3 weeks)"
        if t2_deal_only
        else f"{t2} closing soon with the client quiet 3+ weeks"
    )
    summary = (
        f"{len(res.rows)} of {res.checked} open deals have a close date that is "
        f"hard to believe: {t1} already past, {soon}, {t3} worth watching."
    )
    if res.deferred:
        summary += (
            f" Deferred deals are not ranked; all {len(res.deferred)} are "
            "listed at the end."
        )
    blocks.append(
        {"type": "context", "elements": [{"type": "mrkdwn", "text": summary}]}
    )
    lines.append(summary)

    for r in shown:
        line = _row_line(r)
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": _truncate_mrkdwn(line)},
            }
        )
        lines.append(f"{r.deal_name} — {r.owner}")

    tail_bits = []
    if len(res.rows) > len(shown):
        tail_bits.append(f"{len(res.rows) - len(shown)} more not shown")
    if res.uncontacted:
        # Counted, never ranked: with no logged contact we cannot say how long
        # the client has been quiet, and inventing a number to sort on is the
        # one thing that would make this list untrustworthy. "On the deal
        # itself": the count reads the deal's own record, and its client may
        # have contact logged on the company.
        tail_bits.append(
            f"{res.uncontacted} deals have no client contact logged on the deal itself"
        )
    if res.client_records_unread:
        tail_bits.append(
            "the client company records could not be read, so contact logged on "
            "the client but not on the deal is not counted this week"
        )
    if res.abandoned:
        tail_bits.append(
            f"{res.abandoned} past their close date with no client contact in "
            "6+ months — clean-up, not forecast"
        )
    if res.truncated:
        tail_bits.append("this is a partial read of the pipeline")
    if tail_bits:
        tail = " · ".join(tail_bits)
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": tail}]}
        )
        lines.append(tail)
    _append_deferred(res, blocks, lines)
    return "\n".join(lines), blocks


def _mrkdwn_text(text: str) -> str:
    """A rep's free text, safe inside Slack mrkdwn: no links, mentions or
    ``<!channel>`` can be smuggled in through a defer note."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _deferred_line(r: DeferredRow) -> str:
    from backend.routers.cockpit import _hubspot_deal_url

    url = _hubspot_deal_url(r.deal_id)
    name = _mrkdwn_text(r.deal_name)
    name = f"<{url}|{name}>" if url else name
    note = " ".join((r.note or "").split())
    if len(note) > _NOTE_CHARS:
        note = note[: _NOTE_CHARS - 1].rstrip() + "…"
    flag = f" · {r.flag}" if r.flag else ""
    return (
        f"*{name}* — {_mrkdwn_text(r.owner)} · back on {r.follow_up_on}{flag}\n"
        f"_“{_mrkdwn_text(note)}”_"
    )


def _append_deferred(res: RollupResult, blocks: list[dict], lines: list[str]) -> None:
    """The "Deferred by their reps" section — every deferred deal, counted.

    Deferred deals are off the reps' own lists until their follow-up date, so
    the manager sees each one here with its owner, date and note. Listed by
    date, never by person; past ``MAX_DEFERRED_ROWS`` the rest are a count.
    """
    from backend.agents.slack_delivery import _truncate_mrkdwn

    if not res.deferred:
        return
    count = len(res.deferred)
    noun = "deal" if count == 1 else "deals"
    head = (
        f"*Deferred by their reps* — {count} of {res.open_deals} open {noun}, "
        "off the reps' lists until the follow-up date:"
    )
    shown = res.deferred[:MAX_DEFERRED_ROWS]
    body = "\n".join([head, *(_deferred_line(r) for r in shown)])
    if count > len(shown):
        body += f"\n_{count - len(shown)} more deferred, not shown._"
    blocks.append({"type": "divider"})
    blocks.append(
        {"type": "section", "text": {"type": "mrkdwn", "text": _truncate_mrkdwn(body)}}
    )
    lines.append(f"Deferred by their reps: {count} of {res.open_deals} open {noun}")
    lines.extend(f"{r.deal_name} — {r.owner}, until {r.follow_up_on}" for r in shown)


def run_rollup(*, dry_run: bool = False) -> RollupResult:
    from rubik_core.config import settings

    # dry_run ignores the kill switch on purpose: rendering without sending is
    # how the switch gets reviewed before it is flipped.
    if not dry_run and not getattr(settings, "CLOSE_DATE_ROLLUP_ENABLED", False):
        logger.info("[close-date-rollup] disabled")
        return RollupResult(status="disabled")

    res = collect_rows()
    if res.status != "ok":
        return res

    res.recipients = resolve_recipients()
    fallback, blocks = build_blocks(res)

    if dry_run:
        # Render FIRST, gate on recipients second. On a laptop the local
        # database has no users, so a dry run that checked recipients first
        # showed nothing at all — and the message is the whole point of a
        # dry run. Recipients are reported so an empty list is visible too.
        logger.info(
            "[close-date-rollup] DRY RUN recipients=%s (%d) rows=%d\n%s",
            res.recipients or "none resolved",
            len(res.recipients),
            len(res.rows),
            fallback,
        )
        return res

    if not res.recipients:
        logger.warning("[close-date-rollup] no recipients — nothing sent")
        res.status = "no_recipients"
        return res

    for email in res.recipients:
        _send_once(email, fallback, blocks, res)
    return res


def _send_once(
    email: str, fallback: str, blocks: list[dict], res: RollupResult
) -> None:
    """Deliver to one recipient at most once per their local day.

    The guard is the UNIQUE(recipient_email, kind, local_date) index, NOT a
    pre-check: two scheduler executions racing would both pass a pre-check and
    both send. The write order is the one digest_logs documents — insert and
    FLUSH to claim the slot, send, stamp ``sent_at``, commit; on a failed send
    roll back, so the row is absent and the next sweep retries.

    Keyed on the RECIPIENT's local today. Keying on UTC would give somebody
    reading in the evening a second copy and somebody early in the morning
    none, with no timezone to blame it on.
    """
    import uuid

    from sqlalchemy.exc import IntegrityError

    from backend.services.slack_service import SlackBotClient
    from rubik_core.database.commitments import DigestLog
    from rubik_core.database.db import SessionLocal
    from rubik_core.priorities.temporal import user_today_str

    bot = SlackBotClient()
    if not getattr(bot, "is_available", True):
        # No workspace bot token: not a per-recipient failure, and no row, so
        # the whole list retries once the app is installed.
        logger.warning("[close-date-rollup] no Slack bot available — nothing sent")
        res.errors.append("slack bot unavailable")
        return

    local_date = user_today_str(email)
    try:
        with SessionLocal() as db:
            db.add(
                DigestLog(
                    id=uuid.uuid4().hex,
                    recipient_email=email.lower(),
                    kind="close_date_rollup",
                    local_date=local_date,
                    channel="slack",
                    item_count=min(len(res.rows), MAX_ROWS),
                    suppressed_count=max(0, len(res.rows) - MAX_ROWS),
                    # Ids and counts only — a delivery log, not a copy of the
                    # message, and never anything a deal's notes said.
                    detail={
                        "deal_ids": [r.deal_id for r in res.rows[:MAX_ROWS]],
                        "tier_counts": {str(k): v for k, v in res.tier_counts.items()},
                        "checked": res.checked,
                        "deferred_deal_ids": [r.deal_id for r in res.deferred],
                    },
                    created_at=datetime.now(timezone.utc),
                )
            )
            try:
                db.flush()  # claim the slot
            except IntegrityError:
                db.rollback()
                logger.info(
                    "[close-date-rollup] %s already got this week's roll-up (%s)",
                    email,
                    local_date,
                )
                res.already_sent += 1
                return

            try:
                ok = bot.send_dm(email, text=fallback, blocks=blocks)
            except Exception as e:  # noqa: BLE001 — one recipient must not stop the rest
                db.rollback()
                logger.warning("[close-date-rollup] send to %s failed: %s", email, e)
                res.errors.append(f"{email}: {e}")
                return

            if ok is None:
                # Not a Slack workspace member. A skip, not a failure, and no
                # row — so they get it the week they are added.
                db.rollback()
                logger.info("[close-date-rollup] %s is not in the workspace", email)
                res.not_in_slack += 1
                return

            db.query(DigestLog).filter(
                DigestLog.recipient_email == email.lower(),
                DigestLog.kind == "close_date_rollup",
                DigestLog.local_date == local_date,
            ).update({"sent_at": datetime.now(timezone.utc)})
            db.commit()
            res.sent += 1
    except Exception as e:  # noqa: BLE001 — delivery must never take the job down
        logger.exception("[close-date-rollup] delivery failed for %s", email)
        res.errors.append(f"{email}: {e}")


def resolve_recipients() -> list[str]:
    """Who gets it — an EXPLICIT list, then vetoed by the feature gate.

    Never enumerated from the roster. ``user_allowed`` grants on
    ``admin_allowed`` before it ever looks at a department, so enumerating
    would quietly mail every admin a weekly list of the sales team's deals,
    forever, with nobody having asked for it.
    """
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.models import User
    from rubik_core.features import feature_allows

    out: list[str] = []
    try:
        with SessionLocal() as db:
            # No is_active column on users; the gate below is the filter
            # that matters, and it is a small table.
            for row in db.query(User).filter(User.is_platform_support.is_(False)).all():
                if feature_allows("close_date_rollup", row, db):
                    out.append(row.email)
    except Exception:  # noqa: BLE001
        logger.exception("[close-date-rollup] recipient lookup failed")
        return []
    return sorted(out)


def main(argv: list[str] | None = None) -> int:
    from backend.jobs.bootstrap import bootstrap_job_workspace

    if not bootstrap_job_workspace():
        return 2

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="render and log the message without sending it",
    )
    args = ap.parse_args(argv)

    res = run_rollup(dry_run=args.dry_run)
    logger.info(
        "[close-date-rollup] status=%s checked=%d flagged=%d deferred=%d sent=%d",
        res.status,
        res.checked,
        len(res.rows),
        len(res.deferred),
        res.sent,
    )
    # Finding nothing is success. Being unable to LOOK is not — a backstop that
    # stays quiet when it could not read is the original bug in a new hat.
    return EXIT_ERROR if res.status == "fetch_failed" else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

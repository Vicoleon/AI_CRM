"""HubSpot SignalSource — reuses UC-10's BigQuery probes from sales_hubspot.

The original `SalesHubspotPlugin` that consumed these probes was unenrolled
when UC-17 landed and has since been deleted; the fetch functions now exist
for this SignalSource alone. No queries are duplicated — we import them and
reshape rows into `Signal`s.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

from backend.agents.sales_hubspot import (
    LOOKBACK_DAYS,
    STAGE_STUCK_DAYS,
    STALE_LEAD_DAYS,
    fetch_market_triggers,
    fetch_stage_stuck_deals,
    fetch_stale_open_deals,
)
from backend.signals.types import Signal
from rubik_core.adapters.bigquery import (
    BigQueryAdapter,
    WorkspaceBigQueryUnavailable,
    require_workspace_crm_bigquery,
)
from rubik_core.config import settings
from rubik_core.retrieval.hubspot_api import _plausible_date
from rubik_core.retrieval.hubspot_properties import deal_product_property
from rubik_core.tenancy.context import current_workspace
from rubik_core.tenancy.hubspot_config import get_hubspot_config
from rubik_core.tenancy.secrets import workspace_secret

logger = logging.getLogger(__name__)


# Stages we treat as deprecated and drop at emission time. The today-pipeline
# is dominated by deals stuck in "Hold (DO NOT USE)" etc. — surfacing them as
# priorities is the single biggest source of homepage noise. Until product
# defines a canonical deprecated-stage list, we match these substrings
# case-insensitively against `stage_label` / `dealstage`.
# TODO(UC-17 follow-up): replace with a config-driven list owned by Sales ops.
DEPRECATED_STAGE_PATTERNS: tuple[str, ...] = (
    "do not use",
    "deprecated",
    "archive",
)


def _is_deprecated_stage(*candidates: Any) -> bool:
    for c in candidates:
        if not c:
            continue
        s = str(c).lower()
        for pat in DEPRECATED_STAGE_PATTERNS:
            if pat in s:
                return True
    return False


# ── Age ceilings: a deal can be stale enough to stop being an action item ────
#
# A stalled deal used to get MORE urgent forever — `_stage_stuck_severity` said "high"
# for anything over 30 days, with no upper bound at all. That is how a deal stalled
# **254 days** became corroborating evidence that an invoice reminder was a "critical
# financial deadline", when the deal had in fact closed long ago. The truth was never in
# the CRM — which is the point. Past a few months of total silence the honest reading is
# "this record is out of date", not "act on this today".
#
#   > DEAL_PRESUMED_DEAD_DAYS → severity forced to `low`, summary says the
#     record looks out of date. Still visible so someone can clean it up, but
#     it can never justify urgency.
#   > DEAL_MAX_AGE_DAYS       → dropped entirely. Six months of no movement on
#     an open deal means the record is abandoned, not actionable.
#
# Both are declared in rubik_core/config.py so Sales ops can tune them from the
# environment without a code change (same intent as the DEPRECATED_STAGE_PATTERNS
# TODO above). Read at call time, not import time, so a test or a settings
# override takes effect without reloading this module.
def _presumed_dead_days() -> int:
    return int(getattr(settings, "HUBSPOT_DEAL_PRESUMED_DEAD_DAYS", 90))


def _max_age_days() -> int:
    return int(getattr(settings, "HUBSPOT_DEAL_MAX_AGE_DAYS", 180))


# Module-level aliases kept for readability at the call sites and in tests.
DEAL_PRESUMED_DEAD_DAYS: int = _presumed_dead_days()
DEAL_MAX_AGE_DAYS: int = _max_age_days()


# Close-date credibility. Read at call time like the ceilings above, so a test
# or an ops override takes effect without reloading this module.
def _close_date_horizon_days() -> int:
    return int(getattr(settings, "HUBSPOT_CLOSE_DATE_HORIZON_DAYS", 45))


def _close_date_quiet_days() -> int:
    return int(getattr(settings, "HUBSPOT_CLOSE_DATE_QUIET_DAYS", 21))


def _close_date_watch_days() -> int:
    return int(getattr(settings, "HUBSPOT_CLOSE_DATE_WATCH_DAYS", 14))


CLOSE_DATE_TIER_PASSED = 1  # the date is already wrong
CLOSE_DATE_TIER_QUIET = 2  # date near AND the client quiet past the threshold
CLOSE_DATE_TIER_WATCH = 3  # quiet enough to watch, not enough to interrupt


def close_date_tier(days_to_close: int | None, quiet: int | None) -> int | None:
    """Which credibility band a deal is in, or None for "no finding".

    THE ONE DEFINITION. Two readers depend on it — the AE's morning card
    (tiers 1 and 2) and the weekly manager roll-up (all three) — and a leader
    asking a rep about a deal whose card that rep never saw is exactly how the
    feature gets ignored. Thresholds are read at CALL time, like the ceilings
    above, so tuning one from the environment moves both readers together.

    Tier 3 is half-open against tier 2 (``watch <= quiet < quiet_days``) so no
    deal can land in both bands.
    """
    if days_to_close is None:
        return None
    # The silence ceiling applies to EVERY tier, before the tier is decided. A
    # deal nobody has spoken to in six months is an abandoned record — data
    # hygiene, not a forecast question — whether or not its date has passed.
    # The probe always did this first; the first real roll-up dry-run showed
    # what happens without it: a deal 591 days silent ranked #1 in the roll-up
    # and would have been the rep's daily card too.
    if quiet is not None and _exceeds_max_age(int(quiet)):
        return None
    if days_to_close < 0:
        overdue = abs(int(days_to_close))
        return None if _exceeds_max_age(overdue) else CLOSE_DATE_TIER_PASSED
    if quiet is None or days_to_close > _close_date_horizon_days():
        return None
    quiet = int(quiet)
    if quiet >= _close_date_quiet_days():
        return CLOSE_DATE_TIER_QUIET
    if quiet >= _close_date_watch_days():
        return CLOSE_DATE_TIER_WATCH
    return None


#: Set on a row only when the deal's client company was contacted more
#: recently than the deal itself (:func:`_live_rows`).
_CLIENT_KEYS = (
    "client_name",
    "client_days_since_engagement",
    "client_last_engagement_at",
)


def _client_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {k: row[k] for k in _CLIENT_KEYS if row.get(k) is not None}


def _ago(days: int) -> str:
    if days <= 0:
        return "today"
    return "yesterday" if days == 1 else f"{days} days ago"


def _client_silence(row: dict[str, Any]) -> tuple[str | None, bool]:
    """How long the client has gone without logged contact, and whether only
    the deal has.

    ``days_since_engagement`` is the deal's own "Last Contacted". The
    ``client_*`` keys are on the row only when the client's company record
    was contacted more recently than that: an email to someone at the client
    who is not on the deal is logged on their company, never on the deal
    (so a deal can read 82 days quiet while the client was emailed 13 days
    earlier). Returns ``(phrase, deal_only)``: the phrase is
    None when there is nothing to say, and ``deal_only`` is True when the
    client was contacted inside the quiet threshold, so only the deal record
    is quiet. Which deals are flagged never depends on this, and the deal's
    own number is always in the phrase.
    """
    quiet = row.get("days_since_engagement")
    client_days = row.get("client_days_since_engagement")
    if client_days is None:
        phrase = f"no logged client contact for {int(quiet)} days" if quiet else None
        return phrase, False
    client_days = int(client_days)
    if client_days >= _close_date_quiet_days():
        on_deal = (
            "none ever on this deal"
            if quiet is None
            else f"none on this deal itself for {int(quiet)} days"
        )
        return f"no logged client contact for {client_days} days ({on_deal})", False
    on_deal = (
        "nothing has ever been logged on this deal"
        if quiet is None
        else f"nothing has been logged on this deal for {int(quiet)} days"
    )
    client = row.get("client_name") or "the client"
    return f"{on_deal}, though {client} was contacted {_ago(client_days)}", True


def _is_presumed_dead(days: int) -> bool:
    """True when a deal has been silent long enough to be treated as stale data."""
    return days > _presumed_dead_days()


def _exceeds_max_age(days: int) -> bool:
    """True when a deal is too old to be an action item at all."""
    return days > _max_age_days()


def _stale_severity(days: int) -> str:
    if _is_presumed_dead(days):
        return "low"
    if days > 30:
        return "high"
    if days >= 22:
        return "medium"
    return "low"


def _stage_stuck_severity(days: int) -> str:
    if _is_presumed_dead(days):
        return "low"
    if days > 30:
        return "high"
    if days >= 21:
        return "medium"
    return "low"


def _activity_time(value: Any) -> datetime | None:
    """BigQuery's date/datetime/string variants as a UTC datetime, or None when
    the value is missing or unparseable.

    Never fabricates. A deal with no logged activity has no activity time, and
    "now" in its place reads downstream as something that just happened: a
    dismissed card citing that deal counted it as new evidence and came back
    every morning.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if hasattr(value, "year") and hasattr(value, "month"):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def _activity_at(value: Any) -> str | None:
    """The payload's ``activity_at``: :func:`_activity_time` as ISO-8601."""
    when = _activity_time(value)
    return when.isoformat() if when else None


def _to_datetime(value: Any) -> datetime:
    """Coerce BigQuery's date/datetime/string variants to a UTC datetime.

    Falls back to now, which suits the day counts and sort keys that read it.
    It is never a record of activity: the payload's ``activity_at`` is.
    """
    return _activity_time(value) or datetime.now(timezone.utc)


def _recently_added_products() -> list[str]:
    raw = os.environ.get("CUBIE_RECENTLY_ADDED_PRODUCTS", "")
    return [p.strip() for p in raw.split(",") if p.strip()]


# ─── Row → Signal mappers ─────────────────────────────────────────────────────


def stale_lead_signal(row: dict[str, Any]) -> Signal | None:
    if _is_deprecated_stage(row.get("dealstage"), row.get("stage_label")):
        logger.warning(
            "[signals.hubspot] dropping stale_lead in deprecated stage "
            "deal_id=%s stage=%r — revisit DEPRECATED_STAGE_PATTERNS",
            row.get("deal_id"),
            row.get("dealstage") or row.get("stage_label"),
        )
        return None
    owner = (row.get("owner_email") or "").lower()
    if not owner:
        return None
    days = int(row.get("days_since_last_activity") or 0)
    if days <= STALE_LEAD_DAYS:
        return None
    if _exceeds_max_age(days):
        logger.info(
            "[signals.hubspot] dropping stale_lead past the age ceiling "
            "deal_id=%s days=%d (> %d) — abandoned record, not an action item",
            row.get("deal_id"),
            days,
            DEAL_MAX_AGE_DAYS,
        )
        return None
    company = row.get("company_name") or row.get("deal_name") or "this lead"
    deal_name = row.get("deal_name") or "(no deal name)"
    last_date = row.get("last_activity_date")
    if _is_presumed_dead(days):
        # Say the quiet part out loud, so the decision-maker cannot read a
        # months-old silence as urgency.
        summary = (
            f"Deal '{deal_name}' ({company}) has had no activity for {days} days "
            f"(last activity {last_date}) — the CRM record is likely out of date "
            f"(closed, lost, or the client moved on) rather than a live deal"
        )
    else:
        summary = (
            f"Deal '{deal_name}' ({company}) has been stale for {days} days "
            f"(last activity {last_date})"
        )
    return Signal(
        user_email=owner,
        source="hubspot",
        signal_type="stale_lead",
        summary=summary,
        occurred_at=_to_datetime(last_date),
        entity_refs={
            k: v
            for k, v in {
                "deal_id": row.get("deal_id"),
                "contact_email": row.get("contact_email"),
                "company_name": row.get("company_name"),
            }.items()
            if v
        },
        severity_hint=_stale_severity(days),
        payload={
            "deal_name": deal_name,
            "owner_email": owner,
            "dealstage": row.get("dealstage"),
            "amount": row.get("amount"),
            "days_since_last_activity": days,
            "last_activity_date": str(last_date) if last_date else None,
            # When something last happened on the deal, or None when nothing
            # was logged. The priorities agent stamps the card's signal with
            # it rather than `occurred_at`, which falls back to "now".
            "activity_at": _activity_at(last_date),
            "products": row.get("products"),
            # ── Keys the priorities HubSpot agent reads ──────────────────
            # The agent asks for `stage`, `stage_age_days`,
            # `days_since_engagement`, `last_engagement_at` and `close_date`;
            # this writer only ever emitted `dealstage`/`stage_label`,
            # `days_in_stage`, `days_since_last_activity` and
            # `last_activity_date`, so all five were None on every production
            # deal and the decision-maker prompt reasoned about fields that
            # were never populated. Added rather than renamed: judge.py reads
            # the original names.
            "stage": row.get("stage_label") or row.get("dealstage"),
            "close_date": row.get("close_date"),
            # Client contact only — None when nobody logged one. A card must
            # never turn an internal note into "the client has gone quiet".
            "days_since_engagement": row.get("days_since_engagement"),
            "last_engagement_at": row.get("last_engagement_at"),
            # The client company's newer contact, when it is newer: beside
            # the deal's own date, never instead of it (_client_silence).
            **_client_payload(row),
            # Read by the decision-maker prompt: a presumed-dead deal may be
            # mentioned but must never be cited as evidence of urgency.
            "presumed_dead": _is_presumed_dead(days),
        },
    )


def stage_stuck_signal(row: dict[str, Any]) -> Signal | None:
    if _is_deprecated_stage(row.get("stage_label"), row.get("dealstage")):
        logger.warning(
            "[signals.hubspot] dropping stage_stuck in deprecated stage "
            "deal_id=%s stage=%r — revisit DEPRECATED_STAGE_PATTERNS",
            row.get("deal_id"),
            row.get("stage_label") or row.get("dealstage"),
        )
        return None
    owner = (row.get("owner_email") or "").lower()
    if not owner:
        return None
    days = int(row.get("days_in_stage") or 0)
    if days <= STAGE_STUCK_DAYS:
        return None
    if _exceeds_max_age(days):
        logger.info(
            "[signals.hubspot] dropping stage_stuck past the age ceiling "
            "deal_id=%s days=%d (> %d) — abandoned record, not an action item",
            row.get("deal_id"),
            days,
            DEAL_MAX_AGE_DAYS,
        )
        return None
    company = row.get("company_name") or row.get("deal_name") or "this account"
    deal_name = row.get("deal_name") or "(no deal name)"
    stage = row.get("stage_label") or "current stage"
    entered = row.get("stage_entered_ts")
    if _is_presumed_dead(days):
        summary = (
            f"Deal '{deal_name}' ({company}) has sat in '{stage}' for {days} days "
            f"— the CRM record is likely out of date (closed, lost, or the client "
            f"moved on) rather than a live deal"
        )
    else:
        summary = f"Deal '{deal_name}' ({company}) stuck in '{stage}' for {days} days"
    return Signal(
        user_email=owner,
        source="hubspot",
        signal_type="stage_stuck",
        summary=summary,
        occurred_at=_to_datetime(entered),
        entity_refs={
            k: v
            for k, v in {
                "deal_id": row.get("deal_id"),
                "company_name": row.get("company_name"),
            }.items()
            if v
        },
        severity_hint=_stage_stuck_severity(days),
        payload={
            "deal_name": deal_name,
            "owner_email": owner,
            "stage_label": stage,
            "days_in_stage": days,
            "stage_entered_ts": str(entered) if entered else None,
            # The newest thing this probe knows happened on the deal: the stage
            # move, or a client contact logged since. A contact after a ✗ is
            # real news even though the deal has not changed stage. None when
            # neither is known, never "now" (see stale_lead_signal).
            "activity_at": _activity_at(
                max(
                    (
                        t
                        for t in (
                            _activity_time(entered),
                            _activity_time(row.get("last_engagement_at")),
                        )
                        if t is not None
                    ),
                    default=None,
                )
            ),
            "amount": row.get("amount"),
            "presumed_dead": _is_presumed_dead(days),
            # ── Keys the priorities HubSpot agent reads ──────────────────
            # The agent asks for `stage`, `stage_age_days`,
            # `days_since_engagement`, `last_engagement_at` and `close_date`;
            # this writer only ever emitted `dealstage`/`stage_label`,
            # `days_in_stage`, `days_since_last_activity` and
            # `last_activity_date`, so all five were None on every production
            # deal and the decision-maker prompt reasoned about fields that
            # were never populated. Added rather than renamed: judge.py reads
            # the original names.
            "stage": stage,
            "stage_age_days": days,
            "close_date": row.get("close_date"),
            "days_since_engagement": row.get("days_since_engagement"),
            "last_engagement_at": row.get("last_engagement_at"),
            # The client company's newer contact, when it is newer: beside
            # the deal's own date, never instead of it (_client_silence).
            **_client_payload(row),
        },
    )


def close_date_at_risk_signal(row: dict[str, Any]) -> Signal | None:
    """A deal whose estimated close date is no longer believable.

    This is the check a sales leader runs by hand — they read a deal's close
    date, compare it against how long the client has actually been silent, and
    ask the AE "why do you think it'll close by then?". Two
    tiers fire:

    * **the date has already passed** and the deal is still open — the record
      is simply wrong, and somebody has to move it or close the deal;
    * **the date is near and the client is quiet** — within the horizon, with
      no logged client contact for longer than the quiet threshold.

    Silence is measured ONLY from ``days_since_engagement``, which is derived
    from ``notes_last_contacted``. When nobody logged a client contact that
    value is None and this probe stays silent: we do not know how long the
    client has been quiet, and an internal note is not a contact. Guessing
    there is the exact false negative the whole feature exists to remove.

    The tier never reads the client's company record, so the morning card and
    the weekly roll-up still agree on every deal. What the company record
    changes is the wording (:func:`_client_silence`), and a near-date card
    whose client was contacted inside the quiet threshold drops to ``medium``:
    the deal record is quiet, the client is not, and it ranks after the deals
    whose whole client has gone quiet.
    """
    if _is_deprecated_stage(row.get("dealstage"), row.get("stage_label")):
        return None
    owner = (row.get("owner_email") or "").lower()
    if not owner:
        return None
    close_date = row.get("close_date")
    days_to_close = row.get("days_to_close")
    if not close_date or days_to_close is None:
        return None

    quiet = row.get("days_since_engagement")
    deal_name = row.get("deal_name") or "(no deal name)"
    company = row.get("company_name") or deal_name
    # The live rows carry no company name, so "(company)" repeated the deal
    # name, and the snippet the decision-maker reads is cut at 200 characters.
    named = f" ({company})" if company != deal_name else ""

    # Tiers 1 and 2 only. The watch band (tier 3) is deliberately NOT a signal:
    # a Signal riding along would be merged onto the deal's other signals by
    # deal_id, altering their evidence and severity, and would consume one of
    # the agent's 30 output slots — for a finding the rep should not be
    # interrupted by. It reaches the manager through the weekly roll-up, which
    # runs its own query.
    tier = close_date_tier(days_to_close, quiet)
    if tier not in (CLOSE_DATE_TIER_PASSED, CLOSE_DATE_TIER_QUIET):
        return None

    phrase, deal_only = _client_silence(row)
    if days_to_close < 0:
        overdue = abs(int(days_to_close))
        quiet_phrase = f", and {phrase}" if phrase else ""
        summary = (
            f"Deal '{deal_name}'{named} has an estimated close date of "
            f"{close_date[:10]}, which passed {overdue} days ago, and the deal is "
            f"still open{quiet_phrase} — the date needs moving or the deal "
            f"needs closing"
        )
        # A passed date is wrong however recently the client heard from us.
        severity = "low" if _is_presumed_dead(overdue) else "high"
    else:
        # Tier 2 — the classifier has already established that the date is
        # inside the horizon and the measured silence is past the threshold.
        quiet = int(quiet)
        summary = (
            f"Deal '{deal_name}'{named} is estimated to close "
            f"{close_date[:10]} ({int(days_to_close)} days away)"
            + (f", and {phrase}" if deal_only else f" but there has been {phrase}")
        )
        if _is_presumed_dead(quiet):
            severity = "low"
        else:
            severity = "medium" if deal_only else "high"

    return Signal(
        user_email=owner,
        source="hubspot",
        signal_type="close_date_at_risk",
        summary=summary,
        occurred_at=_to_datetime(row.get("last_engagement_at"))
        or datetime.now(timezone.utc),
        entity_refs={
            k: v
            for k, v in {
                "deal_id": row.get("deal_id"),
                "company_name": row.get("company_name"),
            }.items()
            if v
        },
        severity_hint=severity,
        payload={
            "deal_name": deal_name,
            "owner_email": owner,
            "stage": row.get("stage_label") or row.get("dealstage"),
            "amount": row.get("amount"),
            # The date part only: this is what HubSpot shows the AE, and a
            # card quoting a UTC timestamp would disagree with the record.
            "close_date": close_date[:10],
            "days_to_close": int(days_to_close),
            "days_since_engagement": quiet,
            "last_engagement_at": row.get("last_engagement_at"),
            # The deal's own last client contact, or None when nobody logged
            # one. A passed close date fires with no contact at all, and
            # `occurred_at` stamped those deals "now" on every run, so a
            # dismissed card on one came back every morning as "new evidence".
            "activity_at": _activity_at(row.get("last_engagement_at")),
            # The client company's newer contact, when it is newer: beside
            # the deal's own date, never instead of it (_client_silence).
            **_client_payload(row),
            "close_date_passed": days_to_close < 0,
            "tier": tier,
            # Silence, never date-slip. Everywhere else in this module the
            # flag means "the client has been quiet this long", the merge in
            # hubspot_agent propagates it across the deal's other signals and
            # forces severity to low — so measuring a slipped date here would
            # demote a deal's stale_lead card because its DATE moved, while
            # the client was spoken to yesterday. De-escalation for an ancient
            # slipped date stays local, in `severity` above.
            "presumed_dead": _is_presumed_dead(int(quiet or 0)),
        },
    )


def new_product_match_signal(
    row: dict[str, Any],
    matched: list[str],
) -> Signal | None:
    if _is_deprecated_stage(row.get("dealstage"), row.get("stage_label")):
        return None
    owner = (row.get("owner_email") or "").lower()
    if not owner or not matched:
        return None
    days = int(row.get("days_since_last_activity") or 0)
    company = row.get("company_name") or row.get("deal_name") or "this lead"
    products = ", ".join(matched)
    summary = f"{company} matches newly-added product(s) {products}; quiet {days} days"
    return Signal(
        user_email=owner,
        source="hubspot",
        signal_type="new_product_match",
        summary=summary,
        occurred_at=_to_datetime(row.get("last_activity_date")),
        entity_refs={
            k: v
            for k, v in {
                "deal_id": row.get("deal_id"),
                "company_name": row.get("company_name"),
            }.items()
            if v
        },
        severity_hint="medium",
        payload={
            "deal_name": row.get("deal_name"),
            "owner_email": owner,
            "matched_products": matched,
            "products": row.get("products"),
            "days_since_last_activity": days,
            "activity_at": _activity_at(row.get("last_activity_date")),
        },
    )


def market_trigger_signal(
    trig: dict[str, Any],
    deal_row: dict[str, Any],
) -> Signal | None:
    if _is_deprecated_stage(deal_row.get("dealstage"), deal_row.get("stage_label")):
        return None
    owner = (deal_row.get("owner_email") or "").lower()
    if not owner:
        return None
    company = trig.get("company_name") or deal_row.get("company_name") or "company"
    text = trig.get("trigger_text") or ""
    summary = f'Market trigger for {company}: "{text}"'
    return Signal(
        user_email=owner,
        source="hubspot",
        signal_type="market_trigger",
        summary=summary,
        occurred_at=_to_datetime(trig.get("triggered_at")),
        entity_refs={
            k: v
            for k, v in {
                "deal_id": deal_row.get("deal_id"),
                "company_name": deal_row.get("company_name"),
            }.items()
            if v
        },
        severity_hint="high",
        payload={
            "trigger_text": text,
            "source_url": trig.get("source_url"),
            "triggered_at": str(trig.get("triggered_at"))
            if trig.get("triggered_at")
            else None,
            "activity_at": _activity_at(trig.get("triggered_at")),
            "owner_email": owner,
            "deal_name": deal_row.get("deal_name"),
        },
    )


# ─── Live HubSpot API path (preferred when workspace read token is set) ──
#
# The mechanical row→Signal mappers above are data-source-agnostic, so the live
# path just produces rows in the same shape from the live HubSpot REST API
# (open deals owned by the rep + `notes_last_updated` for staleness +
# `hs_v2_date_entered_current_stage` for stage-stuck). This replaces the stale
# BigQuery mirror (hubspot_sales_marts / hubspot_raw) for these signals.
# `market_trigger` stays BigQuery-only — its `market_triggers` table is not
# HubSpot CRM data — so it is simply absent from the live path.

# Owner and stage maps belong to a workspace's HubSpot account. Keep each
# workspace's copy separate and refresh when its token changes.
_DIR_CACHE: dict[str, dict[str, Any]] = {}
_DIR_CACHE_TTL_S = 1800


def _cached_directories(client) -> tuple[dict, dict]:
    now = datetime.now(timezone.utc)
    slug = current_workspace()
    token = getattr(client, "token", None)
    token_hash = sha256(token.encode()).hexdigest() if isinstance(token, str) else None
    entry = _DIR_CACHE.get(slug)
    if (
        entry is None
        or entry["token_hash"] != token_hash
        or (now - entry["ts"]).total_seconds() > _DIR_CACHE_TTL_S
    ):
        entry = {
            "owner_dir": client.owner_directory(),
            "stage_labels": client.deal_stage_labels(),
            "token_hash": token_hash,
            "ts": now,
        }
        _DIR_CACHE[slug] = entry
    return entry["owner_dir"], entry["stage_labels"]


def _deal_contact(p: dict, now: datetime) -> tuple[str | None, int | None]:
    """The deal's own last logged client contact, and the days since it.

    Client contact, not internal activity. HubSpot returns epoch-ish junk
    (1970-01-21) for unset date rollups, so this is guarded the same way the
    chat path guards it — an unset rollup must never read as a real "last
    contacted 20,000 days ago".
    """
    contacted = _plausible_date(p.get("notes_last_contacted"))
    return contacted, ((now - _to_datetime(contacted)).days if contacted else None)


def _client_fields(contact, contacted: str | None, now: datetime) -> dict[str, Any]:
    """The client company's last contact, when it is newer than the deal's own.

    ``contact`` is a ``rhythm_checks.ClientContact`` (or None). A company date
    that is not at least a day newer says nothing the deal's own does not, so
    it adds no keys and the row reads exactly as it did before.
    """
    last = getattr(contact, "last_contact", None)
    if last is None:
        return {}
    last = _to_datetime(last)
    days = max((now - last).days, 0)
    if contacted and days >= (now - _to_datetime(contacted)).days:
        return {}
    return {
        "client_name": contact.name,
        "client_days_since_engagement": days,
        "client_last_engagement_at": last.isoformat(),
    }


def _quiet_deal_clients(client, deals: list[dict], user: str) -> dict:
    """``{deal_id: ClientContact}`` for the deals that are quiet on their own.

    Quiet on its own: nothing logged on the deal, or nothing for at least the
    watch window, the shortest silence any HubSpot card or the weekly roll-up
    acts on. A deal contacted more recently than that is not read at all. One
    association read and one company read cover the lot.

    A failed read returns ``{}``, so every row stands on its deal's own date
    exactly as before; the warning below is the only record of it, because a
    briefing card has no notes line to carry the caveat. Never raises.
    """
    from rubik_core.logsafe import safe_error
    from rubik_core.sales.rhythm_checks import client_contacts

    now = datetime.now(timezone.utc)
    watch = _close_date_watch_days()
    quiet = []
    for d in deals:
        p = d.get("properties") or {}
        _contacted, days = _deal_contact(p, now)
        if days is None or days >= watch:
            quiet.append(str(p.get("hs_object_id") or d.get("id") or ""))
    if not any(quiet):
        return {}
    try:
        return client_contacts(client, quiet)
    except Exception as exc:  # noqa: BLE001 — the deal's own date still stands
        # safe_error, never exc_info: these are authenticated httpx calls.
        logger.warning(
            "[signals.hubspot] (live) client company read failed for %s; cards "
            "use each deal's own last contact only: %s",
            user,
            safe_error(exc),
        )
        return {}


def _live_rows(
    deals: list[dict],
    owner_email: str,
    owner_id: str,
    stage_labels: dict,
    clients: dict | None = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Split an owner's open deals into stale-lead, stage-stuck and
    close-date rows, in the same dict shape the BigQuery fetchers produced.

    The third list is NOT a subset of the first two. A deal can have a close
    date nobody has tested while still looking healthy on both other probes —
    an AE who logs an internal note weekly keeps `notes_last_updated` fresh
    and never appears in `stale`, which is exactly the deal a leader catches by
    hand. So the close-date rows are built from EVERY open deal that carries a
    plausible close date.

    ``clients`` is :func:`_quiet_deal_clients`. ``days_since_engagement`` and
    ``last_engagement_at`` stay the deal's own; when the client's company was
    contacted more recently, the ``client_*`` keys carry that date beside
    them. Only the wording and weight of a card read those keys: which deals
    become rows never does.
    """
    now = datetime.now(timezone.utc)
    product_prop = deal_product_property()
    stale: list[dict] = []
    stuck: list[dict] = []
    at_risk: list[dict] = []
    for d in deals:
        p = d.get("properties", {})
        stage_id = p.get("dealstage")
        contacted, quiet_days = _deal_contact(p, now)
        deal_id = p.get("hs_object_id") or d.get("id")
        base = {
            "deal_id": deal_id,
            "deal_name": p.get("dealname"),
            "owner_email": owner_email,
            "owner_id": owner_id,
            "company_name": None,  # deal names already carry the client
            "dealstage": stage_id,
            "stage_label": stage_labels.get(str(stage_id), stage_id),
            "amount": p.get("amount"),
            "products": p.get(product_prop) if product_prop else None,
            "close_date": _plausible_date(p.get("closedate")),
            # Deliberately None rather than falling back to notes_last_updated:
            # if nobody logged a client contact we do not know how long the
            # client has been silent, and guessing from an internal note is the
            # exact false negative this pair exists to eliminate.
            "last_engagement_at": contacted,
            "days_since_engagement": quiet_days,
            **_client_fields((clients or {}).get(str(deal_id)), contacted, now),
        }
        la = p.get("notes_last_updated")
        if la:
            days = (now - _to_datetime(la)).days
            # Match the BigQuery window: recently went stale, not ancient.
            if STALE_LEAD_DAYS < days <= LOOKBACK_DAYS + STALE_LEAD_DAYS:
                stale.append(
                    {**base, "last_activity_date": la, "days_since_last_activity": days}
                )
        entered = p.get("hs_v2_date_entered_current_stage")
        if entered:
            din = (now - _to_datetime(entered)).days
            # >14d stuck; cap at a year to mirror BQ's history window + drop zombies.
            if STAGE_STUCK_DAYS < din <= 365:
                stuck.append(
                    {**base, "stage_entered_ts": entered, "days_in_stage": din}
                )
        # Every open deal carrying a real close date is a candidate — the probe
        # decides which of them is actually implausible.
        if base["close_date"]:
            at_risk.append(
                {
                    **base,
                    "days_to_close": (
                        _to_datetime(base["close_date"]).date() - now.date()
                    ).days,
                }
            )
    return stale, stuck, at_risk


def _deferred_ids(user: str, deal_ids) -> set[str]:
    """The deals among ``deal_ids`` this rep deferred. Never raises (fails open)."""
    from rubik_core.sales.deal_defer import deferred_deal_ids

    return deferred_deal_ids([str(d) for d in deal_ids if d], reader=user)


def _without_deferred(deals: list[dict], user: str) -> list[dict]:
    """Drop the deals the rep deferred, before any probe sees them.

    A deferred deal emits NO signal — not the close-date probe, not stale-lead,
    not stage-stuck — until its follow-up date (rubik_core/sales/deal_defer.py).
    Dropping it here rather than per probe is what keeps a deferred deal from
    reaching the card through a sibling signal. The weekly roll-up runs its own
    query and lists deferred deals for the manager instead.
    """
    ids = [
        str(d.get("id") or (d.get("properties") or {}).get("hs_object_id") or "")
        for d in deals
    ]
    hidden = _deferred_ids(user, ids)
    if not hidden:
        return deals
    return [d for d, did in zip(deals, ids, strict=True) if did not in hidden]


# ─── SignalSource ─────────────────────────────────────────────────────────────


class HubspotSignalSource:
    """Implements `SignalSource` using this workspace's live HubSpot account."""

    name = "hubspot"

    def __init__(self, bq: BigQueryAdapter | None = None):
        self._bq = bq

    def _bq_or_none(self) -> BigQueryAdapter | None:
        if self._bq is not None:
            return self._bq
        try:
            return BigQueryAdapter()
        except Exception as e:
            logger.warning("[signals.hubspot] BigQueryAdapter init failed: %s", e)
            return None

    def fetch(self, user_email: str, *, since: datetime) -> list[Signal]:
        if (workspace_secret("hubspot", "read_token") or "").strip():
            return self._fetch_live(user_email, since=since)
        logger.info("[signals.hubspot] no HubSpot read token for this workspace")
        return []

    def _fetch_live(self, user_email: str, *, since: datetime) -> list[Signal]:
        from rubik_core.retrieval.hubspot_api import HubSpotAPIClient, HubSpotAPIError

        allowed_pipelines = get_hubspot_config().pipeline_ids
        if not allowed_pipelines:
            logger.info("[signals.hubspot] no deal pipelines for this workspace")
            return []

        try:
            client = HubSpotAPIClient()
        except HubSpotAPIError as e:
            logger.warning("[signals.hubspot] live client unavailable: %s", e)
            return []

        user = user_email.lower()
        owner_dir, stage_labels = _cached_directories(client)
        email_to_id = {
            d["email"]: oid for oid, d in owner_dir.items() if d.get("email")
        }
        owner_id = email_to_id.get(user)
        if not owner_id:
            logger.info(
                "[signals.hubspot] (live) no HubSpot owner for %s — 0 signals", user
            )
            return []

        deal_filters = [
            {
                "propertyName": "hs_is_closed",
                "operator": "EQ",
                "value": "false",
            },
            {
                "propertyName": "hubspot_owner_id",
                "operator": "EQ",
                "value": owner_id,
            },
        ]
        # Only the company's own pipelines feed priorities — a portal can also
        # host a pipeline the company runs for a client, and those deals must
        # not surface as the company's own. An empty allowlist was refused
        # before constructing the client.
        product_prop = deal_product_property()
        deal_filters.append(
            {
                "propertyName": "pipeline",
                "operator": "IN",
                "values": list(allowed_pipelines),
            }
        )

        try:
            deals = client.search_all(
                "deals",
                deal_filters,
                [
                    "dealname",
                    "amount",
                    "dealstage",
                    "pipeline",
                    "hubspot_owner_id",
                    "notes_last_updated",
                    # The leader's question — "close date is 9/30, why?" — cannot
                    # be asked without these two, and neither was requested.
                    # closedate is the date somebody is relying on;
                    # notes_last_contacted is the LAST TIME A CLIENT WAS
                    # SPOKEN TO, which is not the same thing as
                    # notes_last_updated (an internal note the rep wrote to
                    # themselves bumps that one without anyone being
                    # contacted).
                    "closedate",
                    "notes_last_contacted",
                    "hs_v2_date_entered_current_stage",
                    # What the deal is for, when the company named its field:
                    # new-product matches read it.
                    *([product_prop] if product_prop else []),
                ],
            )
        except HubSpotAPIError as e:
            logger.warning(
                "[signals.hubspot] (live) deal fetch failed for %s: %s", user, e
            )
            return []

        allowed = set(allowed_pipelines)
        deals = _without_deferred(
            [
                deal
                for deal in deals
                if str((deal.get("properties") or {}).get("pipeline") or "") in allowed
            ],
            user,
        )
        stale_rows, stuck_rows, at_risk_rows = _live_rows(
            deals,
            user,
            owner_id,
            stage_labels,
            clients=_quiet_deal_clients(client, deals, user),
        )
        signals: list[Signal] = []
        for row in stale_rows:
            sig = stale_lead_signal(row)
            if sig:
                signals.append(sig)
        for row in stuck_rows:
            sig = stage_stuck_signal(row)
            if sig:
                signals.append(sig)
        for row in at_risk_rows:
            sig = close_date_at_risk_signal(row)
            if sig:
                signals.append(sig)

        products = _recently_added_products()
        if products:
            lowered = [p.lower() for p in products]
            for row in stale_rows:
                interests = (row.get("products") or "").lower()
                if not interests:
                    continue
                matched = [products[i] for i, p in enumerate(lowered) if p in interests]
                sig = new_product_match_signal(row, matched)
                if sig:
                    signals.append(sig)

        logger.info(
            "[signals.hubspot] (live) user=%s emitted=%d (since=%s)",
            user,
            len(signals),
            since.isoformat() if since else None,
        )
        return signals

    def _fetch_bigquery(self, user_email: str, *, since: datetime) -> list[Signal]:
        try:
            require_workspace_crm_bigquery()
        except WorkspaceBigQueryUnavailable:
            return []
        bq = self._bq_or_none()
        if bq is None:
            return []

        user = user_email.lower()
        stale_rows = fetch_stale_open_deals(bq)
        stage_rows = fetch_stage_stuck_deals(bq)
        triggers = fetch_market_triggers(bq)
        hidden = _deferred_ids(
            user, [r.get("deal_id") for r in stale_rows + stage_rows]
        )
        if hidden:
            stale_rows = [r for r in stale_rows if str(r.get("deal_id")) not in hidden]
            stage_rows = [r for r in stage_rows if str(r.get("deal_id")) not in hidden]

        signals: list[Signal] = []

        for row in stale_rows:
            if (row.get("owner_email") or "").lower() != user:
                continue
            sig = stale_lead_signal(row)
            if sig:
                signals.append(sig)

        for row in stage_rows:
            if (row.get("owner_email") or "").lower() != user:
                continue
            sig = stage_stuck_signal(row)
            if sig:
                signals.append(sig)

        products = _recently_added_products()
        if products:
            lowered = [p.lower() for p in products]
            for row in stale_rows:
                if (row.get("owner_email") or "").lower() != user:
                    continue
                interests = (row.get("products") or "").lower()
                if not interests:
                    continue
                matched = [products[i] for i, p in enumerate(lowered) if p in interests]
                sig = new_product_match_signal(row, matched)
                if sig:
                    signals.append(sig)

        if triggers:
            by_company: dict[str, list[dict]] = {}
            for row in stale_rows:
                name = (row.get("company_name") or "").strip().lower()
                if name:
                    by_company.setdefault(name, []).append(row)
            for trig in triggers:
                company = (trig.get("company_name") or "").strip().lower()
                for deal_row in by_company.get(company, []):
                    if (deal_row.get("owner_email") or "").lower() != user:
                        continue
                    sig = market_trigger_signal(trig, deal_row)
                    if sig:
                        signals.append(sig)

        logger.info(
            "[signals.hubspot] user=%s emitted=%d (since=%s)",
            user,
            len(signals),
            since.isoformat() if since else None,
        )
        return signals

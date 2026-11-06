"""Cubie Sales Cockpit — the four role-scoped read surfaces.

One thing seen four ways: the AE's My Day, the supervisor's nudge worklist,
the manager's team roll-up, the exec's org numbers. Mounted on the
copilot-gated ``core`` parent in
``backend/main.py``, so ``require_full_app_access`` already applies; each
endpoint then declares the narrower cockpit gate that is its effective rule.

**The feature flag lives in the gates, not here.** All four of
``require_cockpit`` / ``require_cockpit_manager`` / ``require_cockpit_supervisor``
/ ``require_cockpit_exec`` call ``backend.auth._require_cockpit_enabled()``,
which 404s unless ``settings.COCKPIT_ENABLED``. Every endpoint in this module
carries exactly one of them, so a half-provisioned environment cannot half-run
the cockpit. Adding a fifth endpoint without one of those
four dependencies would silently opt out of the flag — there is no belt-and-
braces check here to catch it, on purpose: two copies of a 404 rule is how the
two copies drift.

**Access policy — the authoritative table.** Enforced here, never only in
the UI:

    Role         Commitments             Transcript text   Aggregates
    AE           own                     own calls         own
    Manager      team                    team calls        team
    Supervisor   team METADATA only      NONE, ever        none
    Exec         counts only             NONE              org

The supervisor rule is per-FIELD, so it is enforced by *not selecting* the
evidence column (``_METADATA_COLUMNS``) rather than by dropping it from a dict
later. A filter can be forgotten; a column that was never in the SELECT cannot
be.

**Whose rows** is a separate question from **who may open the page**, and it is
answered by one org graph: ``backend/routers/eos/_authz.py``'s promoted
email-keyed helpers. There is no manager→team relation on ``users`` (no
``manager_id``, no ``team_id``), and the only real supervisor-of edge in Cubie
is ``eos_seats.reports_to_id`` bridged to ``users`` by ``eos_people.email``.
This module resolves nothing itself — it calls ``require_team_scope``, which
raises 409 with a machine code when the chart cannot answer. That 409 is the
whole point: a caller with no ``eos_people`` row resolves to a scope containing
only themselves, and on a team view that is indistinguishable from "your team
has nothing outstanding" — the worst possible failure for a cockpit whose
purpose is that nothing outstanding is invisible.

**The aging clock is Cubie's own.** Every age chip here comes from the
commitment's ``state`` plus ``breach_at``, computed through
``rubik_core/cockpit/business_hours.py``. Never from a HubSpot stage timestamp:
the stage clock restarts when a deal moves, so the oldest stalls look freshest,
and rebuilding that is the thing the constraint gate rejected.

HubSpot is read here for labels, amounts and last-contact only — names for ids
Cubie does not store, and the two numbers that genuinely live in the CRM. Every
one of those reads degrades to "unlabelled" rather than failing the page.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from backend.audit import record_audit
from backend.auth import (
    cockpit_transcript_policy,
    require_cockpit,
    require_cockpit_exec,
    require_cockpit_manager,
    require_cockpit_supervisor,
    user_can_read_transcript_text,
)
from backend.routers.eos import _authz
from rubik_core.brand import PRODUCT_NAME
from rubik_core.call_capture.review import (
    failed_analysis_marker_exists,
    reviewable_items_exist,
)
from rubik_core.cockpit.business_hours import (
    as_naive_utc,
    business_hours_between,
    pin_business_time_zone,
)
from rubik_core.cockpit.digest import MAX_INAPP_ITEMS
from rubik_core.config import settings
from rubik_core.database.call_capture import (
    CallExtractionItem,
    CallSession,
    CallTranscriptEntry,
)
from rubik_core.database.commitments import (
    COMMITMENT_STATES,
    Commitment,
    CommitmentSlaConfig,
    CommitmentStageLag,
)
from rubik_core.database.db import get_db
from rubik_core.database.eos.people import Person
from rubik_core.database.models import User
from rubik_core.database.schemas import UserProfile
from rubik_core.tenancy.hubspot_config import (
    HubSpotConfigUnavailable,
    get_hubspot_config,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sales", tags=["sales-cockpit"])


# ── Schemas ──────────────────────────────────────────────────────────────────


class Action(BaseModel):
    """One thing a person can do to a row. ``kind`` is the machine code.

    ``href`` is populated only for a link OUT of Cubie (a HubSpot record). No
    in-app route is emitted: the frontend contract declares four cockpit pages
    and no detail route, so a path invented here would be a link that silently
    does nothing. The frontend maps ``kind`` to the mutation endpoint it calls.
    """

    label: str
    kind: str
    href: str | None = None


class AgeChip(BaseModel):
    """The coloured age chip. ``label`` always carries the number.

    Colour is never the only signal — it also has to survive a
    screenshot pasted into Slack, and it has to be readable by someone who
    cannot distinguish amber from red.
    """

    state: str
    label: str
    escalated_to: str | None = None


class DeferTarget(BaseModel):
    """The deal a row is about, when the reader may defer it.

    Present only on a row about one HubSpot deal, and only for someone on the
    ``deal_defer`` roster. It is not a second primary action: deferring takes
    the deal off the list until a date, it does not do the row's work, so the
    client renders it as a quiet "Defer" beside the row's own buttons and posts
    to ``/api/sales/deals/{deal_id}/defer`` (``backend/routers/deal_defer.py``).
    """

    deal_id: str
    deal_name: str | None = None


class Item(BaseModel):
    """One worklist row. Exactly ONE primary action, and it writes something.

    If a row in a worklist genuinely has nothing to do, it does not belong in a
    worklist — that is the product rule made structural.
    """

    id: str
    title: str
    subtitle: str | None = None
    age: AgeChip | None = None
    primary_action: Action
    secondary_action: Action | None = None
    evidence_text: str | None = None
    sla_is_placeholder: bool | None = None
    defer: DeferTarget | None = None


class Block(BaseModel):
    """A capped worklist block.

    ``unavailable_reason`` is the difference between "you have nothing here"
    and "Cubie could not look" — a named code, never an empty list standing in
    for both.
    """

    items: list[Item]
    suppressed_count: int
    unavailable_reason: str | None = None


class MyDayBlocks(BaseModel):
    """The AE's own morning order. Do not reorder — see :func:`my_day`."""

    wrapups_pending: Block
    leads: Block
    pipeline_hygiene: Block
    deals: Block
    no_contact_90_days: Block


class MyDayResponse(BaseModel):
    blocks: MyDayBlocks
    #: "team" | "own" | "none" — so the UI can label a metadata-only view
    #: honestly instead of rendering an empty quote box.
    transcript_policy: str
    #: False when HubSpot could not be read: ids appear unlabelled and the
    #: CRM-sourced block says why, rather than the page failing.
    crm_available: bool


class SupervisorResponse(BaseModel):
    items: list[Item]
    suppressed_count: int
    #: How many AEs the Accountability Chart resolved for this caller. A
    #: worklist that is empty because the chart is thin looks identical to a
    #: quiet week without this.
    team_size: int


class Tile(BaseModel):
    """A display-ready aggregate. ``value`` is pre-formatted text on purpose."""

    label: str
    value: str
    hint: str | None = None


class Breach(BaseModel):
    owner_name: str
    company: str
    kind: str
    age_label: str
    #: The recorded human reason, when there is one — an accepted-breach reason
    #: or the reason given with an ETA. NEVER transcript text: this is an
    #: aggregate reporting surface and a client quote does not belong on it.
    reason: str | None = None


class WrapupRate(BaseModel):
    owner_name: str
    rate: float


class StageLagCount(BaseModel):
    owner_name: str
    count: int


class DeferredDealRow(BaseModel):
    """A deal its rep deferred — listed on the manager view, never hidden.

    Deferring takes a deal off the REP's lists until the follow-up date. The
    manager keeps seeing it, with who owns it, until when and why, so a defer
    cannot be used to bury a bad deal (``rubik_core/sales/deal_defer.py``).
    """

    owner_name: str
    deal_name: str
    #: ISO date the deal comes back on the rep's lists.
    follow_up_on: str
    #: ISO date it was deferred.
    deferred_on: str
    note: str
    hubspot_url: str | None = None


class ForecastIntegrity(BaseModel):
    count: int
    dollar_value: float
    #: False when HubSpot amounts could not be read — a 0.0 that means "we
    #: could not look" must not read as "nothing is at stake".
    dollar_value_available: bool


class ManagerResponse(BaseModel):
    tiles: list[Tile]
    breaches: list[Breach]
    wrapup_rates: list[WrapupRate]
    stage_lag_counts: list[StageLagCount]
    forecast_integrity: ForecastIntegrity
    team_size: int
    crm_available: bool
    #: Every deal the team has deferred, follow-up date soonest first. Empty is
    #: a real answer here: the list reads Cubie's own table, not HubSpot.
    deferred_deals: list[DeferredDealRow] = []


class TrendPoint(BaseModel):
    period: str
    value: float


class ExecResponse(BaseModel):
    tiles: list[Tile]
    trend: list[TrendPoint]
    crm_available: bool


# ── Constants ────────────────────────────────────────────────────────────────

#: Age-chip states, and the closed set of values ``AgeChip.state`` may hold.
#: The three beyond the four colours exist because the honest answer
#: is sometimes not one of the four:
#:
#: * ``unknown``   — ``opened_at`` IS NULL. The clock exists, its start does
#:   not (a back-filled row with no usable date). Unknown shown as unknown
#:   beats unknown shown as zero: an age of zero on a deal stuck for five
#:   months is a lie the cockpit would be telling on its first day.
#: * ``untracked`` — no commitment governs the row at all, so there is no SLA
#:   to be within or past. Elapsed time is shown; no lateness is claimed.
#: * ``exec_visible`` — breach + 5 business days. Rendered like ``breached``.
#:
#: A frontend that only knows four should render anything it does not
#: recognise as the neutral chip and print ``label`` verbatim.
AGE_STATES: tuple[str, ...] = (
    "untracked",
    "unknown",
    "open",
    "at_risk",
    "breached",
    "escalated",
    "exec_visible",
)

#: ``Commitment.state`` → chip state. ``provisional`` reads as ``open`` (its
#: clock runs but escalation is disarmed, and the label says "unconfirmed");
#: ``accepted`` reads as ``breached``, because it IS late — somebody took the
#: hit on the record, which changes the accountability rather than the fact.
_STATE_TO_AGE_STATE: dict[str, str] = {
    "provisional": "open",
    "open": "open",
    "at_risk": "at_risk",
    "breached": "breached",
    "escalated": "escalated",
    "exec_visible": "exec_visible",
    "accepted": "breached",
    "closed": "open",
}

#: Every state at or past a breach — the supervisor worklist's whole content
#: and the manager's breach list. ``accepted`` is deliberately absent: it is a
#: recorded, reasoned acceptance and is terminal until close, so re-raising it
#: on a worklist would make acceptance meaningless.
_BREACH_STATES: tuple[str, ...] = ("breached", "escalated", "exec_visible")

#: Sessions the AE still owes a wrap-up on. ``no_transcript``
#: belongs here: the call happened and the AE is told so even though nothing
#: was captured. Dropping it is how a call becomes invisible, which is the one
#: guarantee CCS exists to make.
_WRAPUP_PENDING_STATUSES: tuple[str, ...] = (
    "transcript_ready",
    "extracted",
    "no_transcript",
)

#: Sessions that reached a state where a wrap-up was possible — the wrap-up
#: completion-rate denominator. A session still in ``scheduled`` /
#: ``in_progress`` / ``ended`` has nothing to review yet, and counting it would
#: make the rate a measure of how recently somebody dialled.
_WRAPPABLE_STATUSES: tuple[str, ...] = ("extracted", "no_transcript", "wrapped")

#: Kinds surfaced by their own My Day block. Listing them again under "deals"
#: would double-count one obligation against a three-item cap.
_DEALS_BLOCK_EXCLUDED_KINDS: tuple[str, ...] = ("wrapup", "lead_touch")

#: Chip-state ordering for the in-block sort. Oldest-first WITHIN a severity
#: tier, not across the whole block: a pure age sort would let three older
#: on-track rows suppress a fresh breach, and a suppressed breach is exactly
#: what this feature exists to prevent. ``rubik_core/cockpit/digest.py`` orders
#: the same way.
_AGE_SEVERITY: dict[str, int] = {
    "exec_visible": 0,
    "escalated": 1,
    "breached": 2,
    "at_risk": 3,
    "open": 4,
    "untracked": 5,
    "unknown": 6,
}

#: Days without logged client contact before an open deal makes the no-contact
#: list. A module constant rather than a setting: nobody has asked for it to be
#: tunable, and an unused setting is a promise the code has not made.
NO_CONTACT_DAYS = 90

#: Windows for the aggregate views. Both are rolling on purpose — a lifetime
#: wrap-up rate can never improve, which turns a process metric into a
#: permanent record of a bad fortnight.
_RATE_WINDOW_DAYS = 30
_MEDIAN_WINDOW_DAYS = 180
_TREND_PERIODS = 6

#: HubSpot's search filters take a bounded ``values`` list, so a lookup of many
#: ids is several searches. Batched rather than truncated: a missing LABEL is
#: cosmetic, but a missing AMOUNT silently shrinks the two dollar figures this
#: module reports, and a number that is quietly short is worse than one marked
#: unavailable.
_HS_ID_BATCH = 100

#: Batches attempted per object type. The search API allows 5 requests/second
#: per account, so this is one second of that budget. Callers are already
#: bounded by ``_MAX_ROWS_SCANNED``; hitting this means something pathological,
#: and it sets ``_Crm.partial`` rather than shortening a total in silence.
_HS_MAX_BATCHES = 5

#: Rows fetched before capping. Generous: the cap is applied after sorting, so
#: the limit only exists to stop one pathological owner scanning the table.
_MAX_ROWS_SCANNED = 500

#: Metadata the supervisor may see: company, owner, age, state — and nothing
#: else. This tuple IS the enforcement of the access policy's "no transcript text, no
#: exception, including deals they are working". ``opened_evidence_text`` is
#: absent from the SELECT, so no later edit to a serializer can leak it and no
#: reviewer has to trust that a filter is still in place.
_METADATA_COLUMNS = (
    Commitment.id,
    Commitment.kind,
    Commitment.owner_email,
    Commitment.hubspot_company_id,
    Commitment.hubspot_deal_id,
    Commitment.state,
    Commitment.sow_kind,
    Commitment.sla_hours,
    Commitment.opened_at,
    Commitment.breach_at,
    Commitment.promised_reason,
    Commitment.accepted_reason,
    Commitment.escalated_to,
)


# ── HubSpot enrichment: labels, amounts, last contact ────────────────────────
#
# Cubie stores HubSpot IDS, not names — ``graph_entities`` keys companies by
# domain, not by portal id — so a readable row needs a live lookup. Three of
# the cockpit's numbers also only exist in the CRM: the dollar value behind a
# forecast disagreement, and the "no contact in 90 days" list.
#
# Everything in this section is best-effort and never raises. A HubSpot outage
# must cost labels and one block, not the page: an AE who cannot see their
# breached SOWs because a name lookup 429'd has lost the feature entirely.


@dataclass(frozen=True)
class _Crm:
    """What HubSpot could tell us this request. Empty is a valid answer."""

    available: bool = False
    reason: str | None = "crm_unavailable"
    #: True when HubSpot answered but not about everything we asked — a failed
    #: batch, or more ids than ``_HS_MAX_BATCHES`` covers. Labels degrade fine
    #: under this (an id renders instead of a name), but any SUM over
    #: ``deal_amounts`` is then an undercount, so the dollar figures gate on
    #: ``amounts_available`` and report "—" rather than a short number.
    partial: bool = False
    #: The caller's HubSpot owner id. ``None`` with ``owner_miss`` True means
    #: their Workspace address does not match any HubSpot owner — the failure
    #: that otherwise produces zero signals with no error.
    owner_id: str | None = None
    owner_miss: bool = False
    deal_names: dict[str, str] = field(default_factory=dict)
    deal_amounts: dict[str, float] = field(default_factory=dict)
    company_names: dict[str, str] = field(default_factory=dict)
    stage_labels: dict[str, str] = field(default_factory=dict)
    #: Open deals owned by the caller with no logged client contact inside the
    #: window, never-contacted first. Only populated when asked for.
    stale_deals: list[dict] = field(default_factory=list)
    #: True when the stale-deal search was wanted and did not complete. Kept
    #: separate from ``available``, because that search is the ONLY source for
    #: block 5: without this flag its failure renders as an empty list, i.e. as
    #: "every deal has been contacted recently" — a reassuring lie.
    stale_unavailable: bool = False
    #: True when the stale deals' client companies could not be read. The list
    #: still stands on each deal's own date, but contact logged only on the
    #: client is missing from it, and block 5 has to say so.
    stale_clients_unread: bool = False

    @property
    def amounts_available(self) -> bool:
        """Whether a SUM over :attr:`deal_amounts` may be reported as a total."""
        return self.available and not self.partial


def _hs_id_batches(values) -> tuple[list[list[str]], bool]:
    """De-duplicated HubSpot ids in ``IN``-filter-sized batches.

    Returns the batches and whether any id was dropped. The flag is the point:
    a caller that summed a truncated batch would publish a total that is short
    by however many deals fell off the end, with nothing in the response
    admitting it.
    """
    ids: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip() if value is not None else ""
        if not text or text in seen:
            continue
        seen.add(text)
        ids.append(text)

    limit = _HS_ID_BATCH * _HS_MAX_BATCHES
    dropped = len(ids) > limit
    if dropped:
        logger.warning(
            "[cockpit] hubspot_id_batch_overflow: %d ids exceeds %d — the "
            "remainder is unlabelled and dollar totals report unavailable",
            len(ids),
            limit,
        )
        ids = ids[:limit]
    return [
        ids[i : i + _HS_ID_BATCH] for i in range(0, len(ids), _HS_ID_BATCH)
    ], dropped


def _pipeline_filter(allowed: tuple[str, ...]) -> list[dict]:
    """Restrict a deal search to the current workspace's own pipelines.

    A portal can also host a pipeline the company runs for a client. Without
    this, another company's deals appear on an AE's My Day — the same leak the
    priorities allowlist was added to stop. Callers refuse an empty allowlist.
    """
    return [{"propertyName": "pipeline", "operator": "IN", "values": list(allowed)}]


def _crm_context(
    *,
    owner_email: str | None,
    deal_ids: list[str],
    company_ids: list[str],
    want_stale: bool = False,
) -> _Crm:
    """One bounded set of HubSpot reads per request. Never raises.

    Four kinds of call: the owner directory and stage labels (shared, cached
    for 30 minutes by ``backend.signals.hubspot``), a deals search by id, a
    companies search by id, and one open-deals search when the caller wants the
    stale list. The two id searches are batched at ``_HS_ID_BATCH``, so they
    cost one request per hundred ids. Account-wide lookups are fetched once and
    passed down, never per record — those are the calls that burn the rate
    limit.

    Any read that comes back short — a dropped batch or a failed one — sets
    ``partial``, which is what keeps a truncated amount lookup from being
    summed into a dollar figure that reads as complete.

    Runs in a threadpool (``run_in_threadpool``): these are synchronous httpx
    calls and the handlers are ``async def``, so calling them inline would
    block the event loop for the whole request.
    """
    from rubik_core.retrieval.hubspot_api import (  # local: heavy leaf
        HubSpotAPIClient,
        _days_since,
        _parse_hs_datetime,
        _plausible_date,
        _status_filters,
    )

    try:
        allowed_pipelines = get_hubspot_config().pipeline_ids
    except Exception as exc:  # noqa: BLE001 — optional CRM enrichment
        logger.info("[cockpit] workspace HubSpot config unavailable: %s", exc)
        return _Crm(reason="crm_unavailable")
    if not allowed_pipelines:
        return _Crm(reason="crm_unavailable")

    try:
        client = HubSpotAPIClient()
    except Exception as exc:  # noqa: BLE001 — an unset token is the normal local state
        logger.info("[cockpit] HubSpot unavailable: %s", exc)
        return _Crm(reason="crm_unavailable")

    owner_id: str | None = None
    owner_miss = False
    partial = False
    stage_labels: dict[str, str] = {}
    if owner_email:
        try:
            from backend.signals.hubspot import _cached_directories

            owner_dir, stage_labels = _cached_directories(client)
            by_email = {
                (info.get("email") or "").lower(): str(oid)
                for oid, info in (owner_dir or {}).items()
                if info.get("email")
            }
            owner_id = by_email.get(owner_email)
            if owner_id is None:
                owner_miss = True
                # A named bucket: an AE whose HubSpot address differs
                # from their Workspace address produces zero CRM rows, and
                # without this line it produces them silently.
                logger.warning(
                    "[cockpit] hubspot_owner_miss: no HubSpot owner matches %s — "
                    "their CRM-sourced blocks will be empty",
                    owner_email,
                )
        except Exception:
            logger.warning("[cockpit] owner directory read failed", exc_info=True)

    deal_names: dict[str, str] = {}
    deal_amounts: dict[str, float] = {}
    batches, dropped = _hs_id_batches(deal_ids)
    partial = partial or dropped
    for batch in batches:
        try:
            rows = client.search_all(
                "deals",
                [{"propertyName": "hs_object_id", "operator": "IN", "values": batch}]
                + _pipeline_filter(allowed_pipelines),
                ["dealname", "amount", "pipeline"],
            )
        except Exception:
            # One failed batch leaves the others usable, so the labels we DID
            # get are still shown — but the amounts are now incomplete, and
            # ``partial`` is what stops them being totalled.
            partial = True
            logger.warning("[cockpit] deal label read failed", exc_info=True)
            continue
        for row in rows:
            props = row.get("properties") or {}
            if str(props.get("pipeline") or "") not in allowed_pipelines:
                partial = True
                continue
            deal_id = str(props.get("hs_object_id") or row.get("id") or "")
            if not deal_id:
                continue
            if props.get("dealname"):
                deal_names[deal_id] = str(props["dealname"])
            try:
                deal_amounts[deal_id] = float(props.get("amount") or 0)
            except (TypeError, ValueError):
                # An amount HubSpot returned but we could not parse is a gap in
                # any total over these deals. An UNSET amount is not: it takes
                # the ``or 0`` above, because a deal nobody has valued
                # contributes nothing to "value at stake" in HubSpot's own
                # reckoning either, and marking that partial would blank the
                # tile on most portals.
                partial = True

    company_names: dict[str, str] = {}
    # Tenant rows may outlive a workspace's HubSpot pipeline assignment. A
    # saved company id alone is not proof that its live portal record belongs
    # to this workspace; X-Ray's strict full-company check also rejects mixed
    # companies whose deals cross pipeline boundaries.
    from rubik_core.xray.fetchers import visible_company_ids

    permitted_company_ids = visible_company_ids(company_ids)
    partial = partial or len(permitted_company_ids) != len(set(company_ids))
    batches, dropped = _hs_id_batches(sorted(permitted_company_ids))
    partial = partial or dropped
    for batch in batches:
        try:
            rows = client.search_all(
                "companies",
                [{"propertyName": "hs_object_id", "operator": "IN", "values": batch}],
                ["name"],
            )
        except Exception:
            partial = True
            logger.warning("[cockpit] company label read failed", exc_info=True)
            continue
        for row in rows:
            props = row.get("properties") or {}
            company_id = str(props.get("hs_object_id") or row.get("id") or "")
            if company_id in permitted_company_ids and props.get("name"):
                company_names[company_id] = str(props["name"])

    stale: list[dict] = []
    stale_unavailable = False
    stale_clients_unread = False
    if want_stale and owner_id:
        try:
            rows = client.search_all(
                "deals",
                _status_filters("open")
                + [
                    {
                        "propertyName": "hubspot_owner_id",
                        "operator": "EQ",
                        "value": owner_id,
                    }
                ]
                + _pipeline_filter(allowed_pipelines),
                ["dealname", "amount", "notes_last_contacted", "pipeline"],
            )
            for row in rows:
                props = row.get("properties") or {}
                if str(props.get("pipeline") or "") not in allowed_pipelines:
                    continue
                # notes_last_contacted ONLY. A fresh internal note
                # (notes_last_updated) must never mask a stale CLIENT contact,
                # and _plausible_date drops the 1970-ish junk HubSpot returns
                # for an unset date rollup.
                last = _plausible_date(props.get("notes_last_contacted"))
                days = _days_since(last)
                if days is not None and days <= NO_CONTACT_DAYS:
                    continue
                stale.append(
                    {
                        "id": str(props.get("hs_object_id") or row.get("id") or ""),
                        "name": props.get("dealname") or None,
                        "days": days,
                        "last": _parse_hs_datetime(last),
                    }
                )
            # Never-contacted (days is None) to the very top — they are the
            # worst offenders and an API sort cannot express them.
            stale.sort(key=lambda d: (d["days"] is not None, -(d["days"] or 0)))
        except Exception:
            # Partial results are worse than none here: this list is ordered
            # worst-first and capped at three, so half an answer is a
            # confidently wrong "worst three".
            stale = []
            stale_unavailable = True
            logger.warning("[cockpit] stale-deal read failed", exc_info=True)
        if stale:
            stale, stale_clients_unread = _with_client_contact(client, stale)

    return _Crm(
        available=True,
        reason=None,
        partial=partial,
        owner_id=owner_id,
        owner_miss=owner_miss,
        deal_names=deal_names,
        deal_amounts=deal_amounts,
        company_names=company_names,
        stage_labels=stage_labels or {},
        stale_deals=stale,
        stale_unavailable=stale_unavailable,
        stale_clients_unread=stale_clients_unread,
    )


def _with_client_contact(client, stale: list[dict]) -> tuple[list[dict], bool]:
    """The 90-day list, re-read against each deal's client company.

    A deal's own "Last Contacted" only moves when the email or call is logged
    on that deal. An email to someone at the client who is not on the deal is
    logged on their company instead, so a deal can sit here at "120 days"
    while the client heard from us last week. Every deal stays on the list,
    since its own record really has gone quiet, but the row now gives the
    client's newer date beside the deal's, and a deal whose client was
    contacted inside the 90 days ranks after the deals whose whole client has
    gone quiet.

    Returns ``(rows, unread)``. One association read and one company read,
    and a failure of either keeps today's deal-only list with ``unread`` set,
    so the block can say what it did not count. Never raises.
    """
    from rubik_core.logsafe import safe_error
    from rubik_core.sales.rhythm_checks import DEAL_ONLY, client_contacts, silence

    now = datetime.now(timezone.utc)
    try:
        clients = client_contacts(client, [d["id"] for d in stale if d["id"]])
        rows: list[dict] = []
        for deal in stale:
            contact = clients.get(deal["id"])
            last = deal.get("last")
            # silence() flags at ">= quiet_days"; this list has always meant
            # "more than NO_CONTACT_DAYS".
            quiet = silence(
                last,
                contact,
                now=now,
                quiet_days=NO_CONTACT_DAYS + 1,
                phrase="logged contact",
            )
            # silence() counts from the client only when its record is newer,
            # so a different count means the client's record says something
            # the deal's own date does not.
            newer = quiet.days != deal.get("days")
            rows.append(
                {
                    **deal,
                    "state": quiet.state,
                    "quiet_days": quiet.days,
                    # Only when the client's record says something the deal's
                    # own date does not; otherwise the row reads as it always has.
                    "reason": quiet.reason if newer else None,
                }
            )
    except Exception as exc:  # noqa: BLE001 — the deal-only list still stands
        # safe_error, never exc_info: these are authenticated httpx calls.
        logger.warning("[cockpit] client company read failed: %s", safe_error(exc))
        return stale, True
    # Whole client quiet first, then deals whose client was contacted inside
    # the window; inside each, never-contacted at the very top, then the
    # longest silence.
    rows.sort(
        key=lambda d: (
            d["state"] == DEAL_ONLY,
            d["quiet_days"] is not None,
            -(d["quiet_days"] or 0),
        )
    )
    return rows, False


def _deal_label(crm: _Crm, deal_id: str | None) -> str | None:
    if not deal_id:
        return None
    return crm.deal_names.get(str(deal_id)) or f"deal {deal_id}"


def _company_label(crm: _Crm, company_id: str | None) -> str | None:
    if not company_id:
        return None
    return crm.company_names.get(str(company_id))


def _record_label(crm: _Crm, row) -> str:
    """The most specific human label for whatever CRM record a row hangs off."""
    return (
        _company_label(crm, row.hubspot_company_id)
        or _deal_label(crm, row.hubspot_deal_id)
        or "no CRM record yet"
    )


def _session_label(crm: _Crm, session) -> str:
    """A call's company name, preferring the one stored on the session.

    ``backend/routers/calls.py`` resolves the company name once at call start
    and stores it in ``attrs["company_name"]``; ``GET /api/calls/pending`` and
    the wrap-up header both read it from there. My Day reads it from there too,
    so the same session cannot be labelled two different ways on two screens —
    and so a HubSpot outage cannot turn a named call into "company <an id>" on
    the one block that already knows its name.
    """
    company_id = getattr(session, "hubspot_company_id", None)
    stored = ((getattr(session, "attrs", None) or {}).get("company_name") or "").strip()
    if company_id and str(company_id) not in crm.company_names:
        # A saved HubSpot-derived name is stale until live ownership is proved.
        stored = ""
    return stored or _record_label(crm, session)


def _hubspot_deal_url(deal_id: str) -> str | None:
    try:
        portal = get_hubspot_config().portal_id
    except HubSpotConfigUnavailable:
        return None
    if not portal or not deal_id:
        return None
    return f"https://app.hubspot.com/contacts/{portal}/deal/{deal_id}"


# ── Age chips ────────────────────────────────────────────────────────────────


def _defer_target(crm: _Crm, deal_id, *, can_defer: bool) -> DeferTarget | None:
    """The row's Defer handle, or None when there is no deal or no permission."""
    if not can_defer or not deal_id:
        return None
    deal_id = str(deal_id)
    return DeferTarget(deal_id=deal_id, deal_name=crm.deal_names.get(deal_id))


def _day_hours() -> float:
    return float(getattr(settings, "COCKPIT_BUSINESS_DAY_HOURS", 8) or 8)


def _fmt_business_days(hours: float) -> str:
    """Business hours as a chip-sized string: "<1d", "2d", "9d"."""
    days = max(hours, 0.0) / _day_hours()
    if days < 0.95:
        return "<1d"
    return f"{round(days)}d"


def _age_chip(row, *, now: datetime, names: dict[str, str] | None = None) -> AgeChip:
    """The chip for one commitment: its own ``state`` plus ``breach_at``.

    Never a HubSpot stage timestamp, and never a raw
    ``timedelta`` — a Friday-afternoon request that "breaches" on Monday
    morning because someone counted calendar hours is the false positive that
    ends this feature's credibility in week one.
    """
    if row.opened_at is None:
        # The clock exists; its start does not. Emitted as its own state so a
        # back-filled row cannot render as brand new.
        return AgeChip(state="unknown", label="age unknown")

    state = _STATE_TO_AGE_STATE.get(str(row.state), "open")
    elapsed = business_hours_between(row.opened_at, now)
    label = _fmt_business_days(elapsed)

    if str(row.state) == "provisional":
        # An unreviewed AI guess. The clock runs from the call, but nobody has
        # confirmed the client actually asked, so the chip says so.
        label = f"{label} · unconfirmed"
    elif str(row.state) == "accepted":
        label = f"{label} · accepted with a reason"
    elif state in ("breached", "escalated", "exec_visible") and row.breach_at:
        over = business_hours_between(row.breach_at, now)
        label = f"{label} · {_fmt_business_days(over)} over"
        if state == "escalated" and row.escalated_to:
            # ``escalated_to`` is lower-folded by the column's validator, so the
            # raw value renders as "with jordan hayes". Resolved through the
            # caller's name map when it has one — a chip is read by a person.
            label = f"{label} · with {_display_name(row.escalated_to, names)}"
    elif state == "at_risk" and row.sla_hours:
        label = f"{label} · SLA {_fmt_business_days(float(row.sla_hours))}"

    return AgeChip(
        state=state,
        label=label,
        escalated_to=row.escalated_to or None,
    )


def _display_name(value: str | None, names: dict[str, str] | None) -> str:
    """A name map lookup that degrades to the value it was given.

    ``escalated_to`` is not guaranteed to be an address — the engine leaves it
    for whoever resolves the manager chain — so an unresolvable value is
    printed as-is rather than replaced by a placeholder.
    """
    folded = (value or "").strip().lower()
    return (names or {}).get(folded) or (value or "")


def _untracked_chip(anchor: datetime | None, *, now: datetime, noun: str) -> AgeChip:
    """Elapsed time for a row no commitment governs — no lateness claimed."""
    if anchor is None:
        return AgeChip(state="unknown", label="age unknown")
    elapsed = business_hours_between(anchor, now)
    return AgeChip(
        state="untracked", label=f"{_fmt_business_days(elapsed)} since the {noun}"
    )


def _placeholder_index(db: Session) -> dict[tuple[str, str], bool]:
    """``commitment_sla_configs`` as ``{(kind, sow_kind): is_placeholder}``.

    One query for the whole table (seven rows). Read at request time rather
    than snapshotted, unlike ``sla_hours``: the NUMBER a commitment aged
    against must not change under it, but whether anyone has *agreed* to that
    number is current information the UI needs today.
    """
    return {
        (str(kind), str(sow_kind)): bool(is_placeholder)
        for kind, sow_kind, is_placeholder in db.query(
            CommitmentSlaConfig.kind,
            CommitmentSlaConfig.sow_kind,
            CommitmentSlaConfig.is_placeholder,
        ).all()
    }


def _sla_is_placeholder(
    index: dict[tuple[str, str], bool], kind: str | None, sow_kind: str | None
) -> bool:
    """Whether this commitment's SLA is still an unagreed placeholder.

    Same fall-through as ``commitment_engine._resolve_sla_hours``: the exact
    ``(kind, sow_kind)`` row, then the kind's generic ``"any"`` row. A missing
    row defaults to **True** — the engine fell back to its built-in constant,
    which came from a current-state map and not from a person agreeing to it,
    so it is a placeholder by construction. Presenting an unagreed number as
    agreed is how a working system gets called wrong in its first review.
    """
    key = (str(kind or ""), str(sow_kind or "any"))
    if key in index:
        return index[key]
    generic = (str(kind or ""), "any")
    return index.get(generic, True)


def _sorted_and_capped(items: list[tuple[int, datetime | None, Item]]) -> Block:
    """Rule 2: the three oldest, then a count. Never an inline expander.

    A list of twenty is a list of zero, and the cap is enforced here —
    server-side — so no later feature can route around it by
    rendering "just one more". Overflow is a number the UI links to a full
    list; it must not expand in place, or the cap is defeated by a chevron.

    Sorted by severity tier, then oldest first inside the tier, with an unknown
    date LAST: a row with no clock must never outrank a real breach.
    """
    items.sort(key=lambda t: (t[0], t[1] or datetime.max))
    shown = [it for _, _, it in items[:MAX_INAPP_ITEMS]]
    return Block(items=shown, suppressed_count=max(len(items) - len(shown), 0))


def _severity(chip: AgeChip | None) -> int:
    return _AGE_SEVERITY.get(chip.state if chip else "open", 9)


# ── People ───────────────────────────────────────────────────────────────────


def _name_map(db: Session, emails: set[str]) -> dict[str, str]:
    """``{email: display name}`` for a set of lower-folded addresses.

    Two queries, ``users`` first and ``eos_people`` for the rest. Both are
    needed: offboarding DELETES the ``users`` row while commitments keep the
    address (there is no FK, deliberately), so a departed AE's breached SOW
    would otherwise render as a bare email on the manager's view. Falls back to the
    address's local part, never to a blank.
    """
    if not emails:
        return {}
    out: dict[str, str] = {}
    for email, name in (
        db.query(User.email, User.name).filter(func.lower(User.email).in_(emails)).all()
    ):
        folded = (email or "").strip().lower()
        if folded and name:
            out[folded] = str(name)
    missing = emails - set(out)
    if missing:
        for email, first, last in (
            db.query(Person.email, Person.first_name, Person.last_name)
            .filter(func.lower(Person.email).in_(missing))
            .all()
        ):
            folded = (email or "").strip().lower()
            full = f"{first or ''} {last or ''}".strip()
            if folded and full:
                out[folded] = full
    for email in emails:
        out.setdefault(email, email.split("@")[0] or email)
    return out


def _may_read_evidence(
    user: UserProfile, owner_email: str | None, team_emails: frozenset[str]
) -> bool:
    """Field-level transcript gate — ``backend.auth`` decides, not this module.

    Commitment evidence is a verbatim quote from a client call. The AE who
    hosted it may read it; their manager may read their team's; the supervisor
    and exec may read none, with no exception including a deal they are
    working. An unattributed row yields False: unknown provenance is not a
    reason to show content.
    """
    return user_can_read_transcript_text(
        user, call_owner_email=owner_email, team_emails=team_emails
    )


def call_hosts_for_evidence_refs(db: Session, refs: list[str | None]) -> dict[str, str]:
    """Evidence ref → the address of the AE who **hosted** the call it came from.

    The one input ``user_can_read_transcript_text`` actually wants. A quote on a
    ``commitment_stage_lags`` row is a client sentence from somebody's call, and
    the only honest attribution for it is ``call_sessions.ae_email`` — reached
    through whichever of the two id spaces ``stage_lag._ccs_signal_evidence``
    wrote into ``evidence_ref`` (``item.evidence_entry_id or item.id``, i.e. a
    ``call_transcript_entries`` id or a ``call_extraction_items`` id).

    Attributing it to anything else is how the check becomes a tautology: on a
    co-sold deal the commitment owner and the call host are different people,
    and passing the *reader's own* deal-side owner in makes the gate compare a
    caller to themselves and always pass.

    **Two queries, whatever the row count.** The refs are resolved as a batch
    precisely so nobody is tempted to drop the check for being a per-row lookup
    inside a loop.

    A ref that resolves to no call session is absent from the result, and the
    caller must read that as "unattributed" → deny. That deliberately also hides
    the non-CCS lane's canned sentence ("Somebody confirmed in Cubie that the SOW
    went out"), whose ref is a message or commitment id: losing one sentence of
    Cubie prose is the cheap direction, and the alternative is a serializer that has to
    decide per row whether text is client-confidential.
    """
    wanted = {str(ref).strip() for ref in refs if ref}
    wanted.discard("")
    if not wanted:
        return {}

    ids = list(wanted)
    out: dict[str, str] = {}
    for model in (CallTranscriptEntry, CallExtractionItem):
        rows = (
            db.query(model.id, CallSession.ae_email)
            .join(CallSession, CallSession.id == model.call_session_id)
            .filter(model.id.in_(ids))
            .all()
        )
        for ref, ae_email in rows:
            folded = (ae_email or "").strip().lower()
            if folded:
                out.setdefault(str(ref), folded)
    return out


# ── My Day blocks ────────────────────────────────────────────────────────────


def _wrapups_block(
    db: Session,
    email: str,
    *,
    now: datetime,
    crm: _Crm,
    commitments: list[Commitment],
    placeholders: dict[tuple[str, str], bool],
) -> Block:
    """Block 1 — calls whose wrap-up the AE still owes.

    Sessions in ``transcript_ready`` / ``extracted`` / ``no_transcript`` with
    no ``wrapped_at``, plus a wrapped call with new suggestions, a failed
    HubSpot write, or an analysis failure to retry. A ready transcript stays
    visible while extraction retries.
    The age comes from the ``wrapup`` commitment opened against the session
    when one exists (its ``evidence_ref`` is the session id); until that
    opener is wired the chip is ``untracked`` and shows elapsed time without
    claiming lateness.
    """
    if not settings.CCS_ENABLED:
        # Distinguishable from "nothing pending": with capture off there are no
        # sessions to owe a wrap-up on, and an empty list would read as an AE
        # who is fully caught up.
        return Block(
            items=[], suppressed_count=0, unavailable_reason="capture_disabled"
        )

    sessions = (
        db.query(CallSession)
        .filter(
            func.lower(CallSession.ae_email) == email,
            or_(
                and_(
                    CallSession.status.in_(_WRAPUP_PENDING_STATUSES),
                    CallSession.wrapped_at.is_(None),
                ),
                and_(
                    CallSession.status == "wrapped",
                    or_(reviewable_items_exist(), failed_analysis_marker_exists()),
                ),
            ),
        )
        .order_by(CallSession.ended_at.asc())
        .limit(_MAX_ROWS_SCANNED)
        .all()
    )
    by_session = {
        str(c.opened_evidence_ref): c
        for c in commitments
        if c.kind == "wrapup" and c.opened_evidence_ref
    }

    rows: list[tuple[int, datetime | None, Item]] = []
    for session in sessions:
        late_review = session.status == "wrapped"
        governing = None if late_review else by_session.get(str(session.id))
        chip = (
            AgeChip(state="untracked", label="review needed")
            if late_review
            else (
                _age_chip(governing, now=now)
                if governing is not None
                else _untracked_chip(session.ended_at, now=now, noun="call")
            )
        )
        if late_review:
            subtitle = (
                "AI suggestions retrying; review when analysis finishes"
                if isinstance((session.attrs or {}).get("extraction_retry"), dict)
                else "new suggestions or a failed write need your review"
            )
        elif session.status == "no_transcript":
            subtitle = "nothing was captured on this call"
        elif session.status == "transcript_ready":
            subtitle = (
                "AI suggestions retrying; your call is ready to review"
                if isinstance((session.attrs or {}).get("extraction_retry"), dict)
                else "AI suggestions being prepared; your call is ready to review"
            )
        else:
            subtitle = (
                f"call ended {session.ended_at.strftime('%d %b')}"
                if session.ended_at
                else "call end time unknown"
            )
        rows.append(
            (
                _severity(chip),
                as_naive_utc(session.ended_at) if session.ended_at else None,
                Item(
                    id=session.id,
                    title=(
                        f"Review call items · {_session_label(crm, session)}"
                        if late_review
                        else f"Wrap-up · {_session_label(crm, session)}"
                    ),
                    subtitle=subtitle,
                    age=chip,
                    primary_action=Action(
                        label="Review the wrap-up", kind="review_wrapup"
                    ),
                    sla_is_placeholder=(
                        _sla_is_placeholder(placeholders, "wrapup", None)
                        if governing is not None
                        else None
                    ),
                ),
            )
        )
    return _sorted_and_capped(rows)


def _leads_block(
    *,
    now: datetime,
    crm: _Crm,
    commitments: list[Commitment],
    temperature: str | None,
    placeholders: dict[tuple[str, str], bool],
) -> Block:
    """Block 2 — leads awaiting a first touch, temperature filter requested.

    ``temperature`` is accepted and honestly refused. HubSpot's lead-temperature
    property's internal name is not known for every portal and
    property names are never invented, so filtering silently by a guessed
    property — or returning an unfiltered list under a filter request — would
    both be lies. The rows come back with a named reason instead.
    """
    reason: str | None = None
    if not settings.CCS_LEADS_ENABLED:
        # HubSpot's LEAD object is REQUIRES_REAUTHORIZATION on this portal, so
        # no lead is created and no lead_touch commitment can exist.
        reason = "lead_capture_disabled"
    elif temperature:
        reason = "lead_temperature_unavailable"

    rows: list[tuple[int, datetime | None, Item]] = []
    for row in commitments:
        # ``accepted`` is skipped for the same reason ``_deals_block`` skips it:
        # it is terminal until close, so re-surfacing it daily against a
        # three-item cap would push live work off the screen and make
        # acceptance mean nothing. Leaving the two blocks asymmetric would make
        # "accepted" quietly depend on which block a commitment landed in.
        if row.kind != "lead_touch" or row.state == "accepted":
            continue
        chip = _age_chip(row, now=now)
        rows.append(
            (
                _severity(chip),
                as_naive_utc(row.opened_at) if row.opened_at else None,
                Item(
                    id=row.id,
                    title=f"Lead untouched · {_record_label(crm, row)}",
                    subtitle="no first touch logged yet",
                    age=chip,
                    # Closing the commitment IS logging the touch: it writes to
                    # the Cubie DB, which is what rule 3 asks of a primary
                    # action. Setting an ETA is the honest alternative.
                    primary_action=Action(
                        label="Log the first touch", kind="close_commitment"
                    ),
                    secondary_action=Action(label="Set an ETA", kind="set_eta"),
                    sla_is_placeholder=_sla_is_placeholder(
                        placeholders, row.kind, row.sow_kind
                    ),
                ),
            )
        )
    block = _sorted_and_capped(rows)
    return Block(
        items=block.items,
        suppressed_count=block.suppressed_count,
        unavailable_reason=reason,
    )


def _pipeline_hygiene_block(
    db: Session,
    user: UserProfile,
    *,
    now: datetime,
    crm: _Crm,
    owner_by_deal: dict[str, str],
    team_emails: frozenset[str],
    deferred: frozenset[str] = frozenset(),
    can_defer: bool = False,
) -> Block:
    """Block 3 — stage-lag exceptions on deals this person is working.

    Attribution is LOCAL: a deal the caller holds a live commitment on is a
    deal they are working. ``commitment_stage_lags`` carries no owner column —
    only a deal id — and resolving a HubSpot deal owner per row would put a
    network call inside a loop. A lag on a deal with no local commitment is not
    attributable from Cubie data and is left out rather than shown to the wrong
    person.

    The evidence quote is the thing that makes an exception convincing, so it
    is included — gated per row by the transcript policy, attributed to **the AE
    who hosted the call the quote came from** (``call_hosts_for_evidence_refs``),
    never to the commitment owner on the deal.

    That attribution is the whole check. ``owner_by_deal`` maps every deal to the
    *caller* on My Day, so gating on it would ask "may this person read their own
    call?" and answer yes every time. On a co-sold deal the host and the deal-side
    commitment owner are different people, and a quote from a call the reader was
    never on is precisely what the access policy withholds. ``owner_by_deal`` still says
    which lags are attributable enough to SHOW; it says nothing about the text.

    A deal the rep deferred (``deferred``) is left out until its follow-up date.
    """
    owner_by_deal = {d: o for d, o in owner_by_deal.items() if d not in deferred}
    if not owner_by_deal:
        return Block(items=[], suppressed_count=0)

    lags = (
        db.query(CommitmentStageLag)
        .filter(
            CommitmentStageLag.status == "open",
            CommitmentStageLag.hubspot_deal_id.in_(list(owner_by_deal)),
        )
        .order_by(CommitmentStageLag.created_at.asc())
        .limit(_MAX_ROWS_SCANNED)
        .all()
    )

    # One batch for the whole block: whose call each quote actually came from.
    hosts = call_hosts_for_evidence_refs(db, [lag.evidence_ref for lag in lags])

    rows: list[tuple[int, datetime | None, Item]] = []
    for lag in lags:
        deal_id = str(lag.hubspot_deal_id)
        # Stage LABELS come from HubSpot; the ids are meaningless to a reader,
        # so a failed label read degrades to a plain sentence rather than
        # printing "HubSpot says <a stage id>" at an AE.
        heard = crm.stage_labels.get(str(lag.evidence_stage_id or "")) or None
        says = crm.stage_labels.get(str(lag.hubspot_stage_id)) or None
        if says and heard:
            subtitle = f"HubSpot says {says}; {PRODUCT_NAME} heard {heard}"
        elif says:
            subtitle = f"HubSpot says {says}, and the evidence disagrees"
        else:
            subtitle = f"the HubSpot stage does not match what {PRODUCT_NAME} heard"
        chip = _untracked_chip(lag.created_at, now=now, noun="exception")
        rows.append(
            (
                _severity(chip),
                as_naive_utc(lag.created_at) if lag.created_at else None,
                Item(
                    id=lag.id,
                    title=f"Stage disagrees with the evidence · {_deal_label(crm, deal_id)}",
                    subtitle=subtitle,
                    age=chip,
                    # Only offer the one-click advance when there is a target
                    # stage to advance TO: not every signal maps to a stage on
                    # this portal ("verbal agreement" has none), and a button
                    # that cannot act is worse than none.
                    primary_action=(
                        Action(label="Advance the stage", kind="apply_stage")
                        if lag.evidence_stage_id
                        else Action(label="Not right", kind="reject_stage")
                    ),
                    secondary_action=(
                        Action(label="Not right", kind="reject_stage")
                        if lag.evidence_stage_id
                        else None
                    ),
                    evidence_text=(
                        lag.evidence_text
                        if _may_read_evidence(
                            user,
                            hosts.get(str(lag.evidence_ref or "").strip()),
                            team_emails,
                        )
                        else None
                    ),
                    defer=_defer_target(crm, deal_id, can_defer=can_defer),
                ),
            )
        )
    return _sorted_and_capped(rows)


def _deals_block(
    user: UserProfile,
    *,
    now: datetime,
    crm: _Crm,
    commitments: list[Commitment],
    team_emails: frozenset[str],
    names: dict[str, str],
    placeholders: dict[tuple[str, str], bool],
    deferred: frozenset[str] = frozenset(),
    can_defer: bool = False,
) -> Block:
    """Block 4 — the AE's own commitments, with age chips.

    ``wrapup`` and ``lead_touch`` are excluded: each has its own block, and
    listing one obligation twice would burn two of the three slots.
    ``accepted`` rows are excluded too — somebody recorded that this will be
    late, with a reason, and re-surfacing it daily under a three-item cap would
    push live problems off the screen while making acceptance meaningless.

    A commitment on a deal the rep deferred is left out until the deal's
    follow-up date. The manager view still counts it (``manager``).
    """
    rows: list[tuple[int, datetime | None, Item]] = []
    for row in commitments:
        if row.kind in _DEALS_BLOCK_EXCLUDED_KINDS or row.state == "accepted":
            continue
        if row.hubspot_deal_id and str(row.hubspot_deal_id) in deferred:
            continue
        chip = _age_chip(row, now=now, names=names)
        breached = chip.state in ("breached", "escalated", "exec_visible")
        rows.append(
            (
                _severity(chip),
                as_naive_utc(row.opened_at) if row.opened_at else None,
                Item(
                    id=row.id,
                    title=f"SOW · {_record_label(crm, row)}"
                    if row.kind == "sow"
                    else f"{row.kind} · {_record_label(crm, row)}",
                    subtitle=_deal_label(crm, row.hubspot_deal_id),
                    age=chip,
                    primary_action=Action(
                        label="Mark the SOW sent"
                        if row.kind == "sow"
                        else "Mark it done",
                        kind="close_commitment",
                    ),
                    # Past the breach the useful second option is taking the
                    # hit on the record; before it, it is moving the date.
                    secondary_action=(
                        Action(label="Accept with a reason", kind="accept_breach")
                        if breached
                        else Action(label="Set an ETA", kind="set_eta")
                    ),
                    evidence_text=(
                        row.opened_evidence_text
                        if _may_read_evidence(user, row.owner_email, team_emails)
                        else None
                    ),
                    sla_is_placeholder=_sla_is_placeholder(
                        placeholders, row.kind, row.sow_kind
                    ),
                    defer=_defer_target(crm, row.hubspot_deal_id, can_defer=can_defer),
                ),
            )
        )
    return _sorted_and_capped(rows)


def _no_contact_block(
    *,
    crm: _Crm,
    deferred: frozenset[str] = frozenset(),
    can_defer: bool = False,
) -> Block:
    """Block 5 — open deals with no logged client contact in 90 days.

    Purely a CRM question, so it degrades with the CRM rather than with Cubie.
    The primary action links OUT to the HubSpot record: logging outreach is a
    HubSpot write, and Cubie has no endpoint that would make the same thing
    true. It still satisfies rule 3 — pressing it leads to a write — and
    pretending otherwise with a no-op Cubie button would not.

    A row whose client company was contacted more recently than the deal
    itself says so (:func:`_with_client_contact`). When the client was
    contacted inside the 90 days the row is titled for the deal, not the
    client, because the client has heard from us: what is missing is the
    contact logged on this deal, which the same "Log the outreach" button
    fixes.

    A deal the rep deferred is left out until its follow-up date — "no
    contact in 90 days" is exactly the noise a long-term deal makes.
    """
    from rubik_core.sales.rhythm_checks import DEAL_ONLY

    if not crm.available or crm.stale_unavailable:
        # ``crm_unavailable`` in BOTH cases, and it is not a fudge: the
        # stale-deal search is this block's only source, so a search that failed
        # while the rest of HubSpot answered still means "HubSpot could not be
        # read" *for this block* — which is exactly what the code's copy says.
        # It must also stay inside the closed set the client declares
        # (``BLOCK_UNAVAILABLE_REASONS`` in frontend/src/api/cockpit.ts): a code
        # invented here falls through to the generic "could not load" sentence
        # and loses the one thing the reader needs, which is why.
        return Block(
            items=[],
            suppressed_count=0,
            unavailable_reason=crm.reason or "crm_unavailable",
        )
    if crm.owner_miss:
        return Block(
            items=[], suppressed_count=0, unavailable_reason="no_hubspot_owner"
        )

    rows: list[tuple[int, datetime | None, Item]] = []
    for index, deal in enumerate(crm.stale_deals):
        days = deal.get("days")
        deal_id = str(deal.get("id") or "")
        if deal_id and deal_id in deferred:
            continue
        name = deal.get("name") or (f"deal {deal_id}" if deal_id else "unnamed deal")
        reason = deal.get("reason")
        if deal.get("state") == DEAL_ONLY:
            title = f"No contact logged on this deal in {NO_CONTACT_DAYS} days · {name}"
            label = "none on this deal" if days is None else f"{days}d on this deal"
            subtitle = reason or label
        else:
            # Silence counted from the newer of the deal and its client, so a
            # client contacted 95 days ago reads 95 here, not the deal's 120.
            quiet = deal.get("quiet_days", days)
            title = f"No contact in {NO_CONTACT_DAYS} days · {name}"
            label = (
                "no logged contact on record"
                if quiet is None
                else f"{quiet}d since contact"
            )
            subtitle = (reason[:1].upper() + reason[1:]) if reason else label
        rows.append(
            (
                # Already ordered worst-first by _crm_context; the index keeps
                # that order through the shared sort without a second clock.
                0,
                datetime.min + timedelta(seconds=index),
                Item(
                    id=deal_id or f"deal-{index}",
                    title=title,
                    subtitle=subtitle,
                    age=AgeChip(state="untracked", label=label),
                    primary_action=Action(
                        label="Log the outreach in HubSpot",
                        kind="log_outreach",
                        href=_hubspot_deal_url(deal_id),
                    ),
                    defer=(
                        DeferTarget(deal_id=deal_id, deal_name=deal.get("name"))
                        if can_defer and deal_id
                        else None
                    ),
                ),
            )
        )
    block = _sorted_and_capped(rows)
    if crm.stale_clients_unread:
        # The rows still stand on each deal's own date; this says what they
        # could not count. Declared in the client's closed set, like
        # ``lead_temperature_unavailable``, which also rides on rows.
        block.unavailable_reason = "client_records_unavailable"
    return block


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.get("/my-day", response_model=MyDayResponse, response_model_exclude_none=True)
@pin_business_time_zone
async def my_day(
    temperature: str | None = None,
    user: UserProfile = Depends(require_cockpit),
    db: Session = Depends(get_db),
):
    """The AE surface — five blocks, in the AE's own morning order.

    Deals the AE deferred are left out of ``pipeline_hygiene``, ``deals`` and
    ``no_contact_90_days`` until their follow-up date; those rows carry a
    ``defer`` handle for the client's Defer button.

    **The order is load-bearing and must not change.** A parallel ritual will
    not survive: this screen works because it follows the order an AE already
    works in, not because it is a tidier grouping.

      1. ``wrapups_pending``   — from CCS, first because it is time-sensitive
      2. ``leads``             — temperature filter requested
      3. ``pipeline_hygiene``  — stage-lag exceptions, one click each
      4. ``deals``             — commitments with age chips
      5. ``no_contact_90_days``

    Every item carries exactly ONE primary action and that action writes
    something. Every block is capped at ``MAX_INAPP_ITEMS`` with the overflow
    as a count.

    Own rows only. No team scope is resolved here on purpose: an AE's own
    commitments key on ``users.email`` and must keep working for someone with
    no ``eos_people`` row at all, which is the case ``require_team_scope``
    exists to refuse on the *team* views.
    """
    email = (user.email or "").strip().lower()
    now = datetime.utcnow()

    commitments: list[Commitment] = (
        db.query(Commitment)
        .filter(
            func.lower(Commitment.owner_email) == email,
            Commitment.closed_at.is_(None),
        )
        .order_by(Commitment.opened_at.asc())
        .limit(_MAX_ROWS_SCANNED)
        .all()
    )

    # Deals this person is working, for local stage-lag attribution.
    owner_by_deal = {
        str(c.hubspot_deal_id): email for c in commitments if c.hubspot_deal_id
    }

    crm = await run_in_threadpool(
        _crm_context,
        owner_email=email,
        deal_ids=[c.hubspot_deal_id for c in commitments if c.hubspot_deal_id],
        company_ids=[c.hubspot_company_id for c in commitments if c.hubspot_company_id],
        want_stale=True,
    )

    # The caller's own address, and only that: an AE reads their own calls'
    # transcript text and nobody else's.
    own_only: frozenset[str] = frozenset()

    # ``escalated_to`` is stored lower-folded; resolve it once so the chip reads
    # "with Jordan Hayes" rather than "with jordan hayes".
    escalation_names = _name_map(
        db, {c.escalated_to for c in commitments if c.escalated_to}
    )

    # Read once and threaded through, like ``crm`` and ``now``: three blocks
    # need it and it is the same seven rows for all of them.
    placeholders = _placeholder_index(db)

    # Deals this person deferred stay off the three deal blocks until their
    # follow-up date (rubik_core/sales/deal_defer.py). Read once, after the CRM
    # call, because the 90-day block's deals only exist once HubSpot answered.
    # Fails open: an unreadable defer table shows every deal.
    from rubik_core.features import feature_allows
    from rubik_core.sales.deal_defer import deferred_deal_ids

    deferred = frozenset(
        deferred_deal_ids(
            set(owner_by_deal)
            | {str(d.get("id")) for d in crm.stale_deals if d.get("id")},
            reader=email,
            db=db,
        )
    )
    can_defer = feature_allows("deal_defer", user)

    blocks = MyDayBlocks(
        wrapups_pending=_wrapups_block(
            db,
            email,
            now=now,
            crm=crm,
            commitments=commitments,
            placeholders=placeholders,
        ),
        leads=_leads_block(
            now=now,
            crm=crm,
            commitments=commitments,
            temperature=temperature,
            placeholders=placeholders,
        ),
        pipeline_hygiene=_pipeline_hygiene_block(
            db,
            user,
            now=now,
            crm=crm,
            owner_by_deal=owner_by_deal,
            team_emails=own_only,
            deferred=deferred,
            can_defer=can_defer,
        ),
        deals=_deals_block(
            user,
            now=now,
            crm=crm,
            commitments=commitments,
            team_emails=own_only,
            names=escalation_names,
            placeholders=placeholders,
            deferred=deferred,
            can_defer=can_defer,
        ),
        no_contact_90_days=_no_contact_block(
            crm=crm, deferred=deferred, can_defer=can_defer
        ),
    )
    return MyDayResponse(
        blocks=blocks,
        transcript_policy=cockpit_transcript_policy(user),
        crm_available=crm.available,
    )


@router.get(
    "/supervisor", response_model=SupervisorResponse, response_model_exclude_none=True
)
@pin_business_time_zone
async def supervisor(
    user: UserProfile = Depends(require_cockpit_supervisor),
    db: Session = Depends(get_db),
):
    """The supervisor's worklist — breached commitments, metadata only, one action each.

    **No transcript text reaches this response, with no exception including a
    deal they are working.** That is enforced structurally: the query selects
    ``_METADATA_COLUMNS`` and ``opened_evidence_text`` is not in it, so there
    is no code path where a serializer could include it and no reviewer has to
    verify that a filter is still there. If they need the content, they ask the
    AE — which is the design, not a limitation of the query.

    Rows are their team's, resolved through the one org graph
    (``_authz.require_team_scope``). A caller whose Accountability Chart record
    is missing or resolves to nobody but themselves gets a **409 with a machine
    code**, never a 200 with an empty list: on a worklist those two look
    identical, and "your team has nothing outstanding" is the most damaging
    thing this feature could say wrongly.
    """
    scope = _authz.require_team_scope(db, user)
    now = datetime.utcnow()

    query = db.query(*_METADATA_COLUMNS).filter(
        Commitment.closed_at.is_(None),
        Commitment.state.in_(_BREACH_STATES),
    )
    if not scope.unrestricted:
        query = query.filter(func.lower(Commitment.owner_email).in_(scope.owner_emails))
    rows = query.order_by(Commitment.breach_at.asc()).limit(_MAX_ROWS_SCANNED).all()

    placeholders = _placeholder_index(db)
    names = _name_map(
        db,
        {(r.owner_email or "").strip().lower() for r in rows}
        | {r.escalated_to for r in rows if r.escalated_to},
    )
    crm = await run_in_threadpool(
        _crm_context,
        owner_email=None,
        deal_ids=[r.hubspot_deal_id for r in rows if r.hubspot_deal_id],
        company_ids=[r.hubspot_company_id for r in rows if r.hubspot_company_id],
    )

    items: list[tuple[int, datetime | None, Item]] = []
    for row in rows:
        chip = _age_chip(row, now=now, names=names)
        owner = (row.owner_email or "").strip().lower()
        items.append(
            (
                _severity(chip),
                as_naive_utc(row.opened_at) if row.opened_at else None,
                Item(
                    id=row.id,
                    title=f"{_record_label(crm, row)} · {row.kind}",
                    subtitle=names.get(owner, owner),
                    age=chip,
                    # The one action, and it writes three times: a HubSpot task
                    # on the AE, a Slack DM, and a nudge row. The log is what
                    # turns "I told them" into a record, and what makes "nudged
                    # twice, still open" an escalation trigger.
                    primary_action=Action(label="Nudge", kind="nudge"),
                    sla_is_placeholder=_sla_is_placeholder(
                        placeholders, row.kind, row.sow_kind
                    ),
                ),
            )
        )

    block = _sorted_and_capped(items)
    record_audit(
        db,
        actor_email=user.email,
        action="COCKPIT_SUPERVISOR_VIEW",
        target="cockpit:supervisor",
        details={
            "team_size": len(scope.owner_emails),
            "unrestricted": scope.unrestricted,
            "rows": len(rows),
        },
    )
    return SupervisorResponse(
        items=block.items,
        suppressed_count=block.suppressed_count,
        team_size=len(scope.owner_emails),
    )


@router.get(
    "/manager", response_model=ManagerResponse, response_model_exclude_none=True
)
@pin_business_time_zone
async def manager(
    user: UserProfile = Depends(require_cockpit_manager),
    db: Session = Depends(get_db),
):
    """The manager's team roll-up — the view that replaces the exec being the detector.

    Team roll-up by state, breaches by AE **with the reason**, wrap-up
    completion rate by AE, stage-lag exception count by AE, and forecast
    integrity: deals whose stage and evidence disagree plus the dollar value
    sitting behind that disagreement.

    **These are process metrics, not a leaderboard, and the ordering enforces
    it.** Google's Meet API documentation states the API "isn't intended for
    performance tracking or user evaluation within your domain", and part of
    what feeds these numbers is Meet-derived. So: named exceptions to work on
    ("three commitments past SLA, here they are"), never a ranked list. State
    tiles follow the escalation ladder, breaches are ordered by age, and the
    per-AE rows are ordered **alphabetically by name** — never by rate, never
    by count, and with no sort-by-worst default. A per-person ordering that
    reads as a league table is exactly the performance tracking Google rules
    out, and it arrives by someone reaching for
    ``sorted(..., reverse=True)`` because it "reads better".

    No transcript text is returned here either, even though the access policy grants
    a manager their team's: ``reason`` is a human-entered acceptance or ETA
    reason. Team transcript text belongs on the wrap-up and detail surfaces,
    not on an aggregate roll-up.
    """
    scope = _authz.require_team_scope(db, user)
    now = datetime.utcnow()
    window_start = now - timedelta(days=_RATE_WINDOW_DAYS)

    def _own_scope(query):
        if scope.unrestricted:
            return query
        return query.filter(func.lower(Commitment.owner_email).in_(scope.owner_emails))

    # ── Roll-up by state ─────────────────────────────────────────────────────
    state_counts = dict(
        _own_scope(
            db.query(Commitment.state, func.count(Commitment.id)).filter(
                Commitment.closed_at.is_(None)
            )
        )
        .group_by(Commitment.state)
        .all()
    )
    # Ladder order, not size order. Sorting tiles by count would put whichever
    # state is worst this morning first, which is the same reading as a rank.
    tiles = [
        Tile(
            label=label,
            value=str(int(state_counts.get(key, 0) or 0)),
            hint=hint,
        )
        for key, label, hint in (
            ("provisional", "Provisional", "clock running, escalation disarmed"),
            ("open", "Open", "inside SLA"),
            ("at_risk", "At risk", "past 70% of the SLA"),
            ("breached", "Breached", "past SLA"),
            ("escalated", "Escalated", "breached for 2 business days"),
            ("exec_visible", "In the exec count", "breached for 5 business days"),
            ("accepted", "Accepted", "late, with a recorded reason"),
        )
    ]

    # ── Breaches, oldest first, with the reason ──────────────────────────────
    breach_rows = (
        _own_scope(
            db.query(*_METADATA_COLUMNS).filter(
                Commitment.closed_at.is_(None),
                Commitment.state.in_(_BREACH_STATES),
            )
        )
        .order_by(Commitment.breach_at.asc())
        .limit(_MAX_ROWS_SCANNED)
        .all()
    )
    placeholders = _placeholder_index(db)
    crm = await run_in_threadpool(
        _crm_context,
        owner_email=None,
        deal_ids=[r.hubspot_deal_id for r in breach_rows if r.hubspot_deal_id],
        company_ids=[r.hubspot_company_id for r in breach_rows if r.hubspot_company_id],
    )
    names = _name_map(
        db,
        {(r.owner_email or "").strip().lower() for r in breach_rows}
        | {r.escalated_to for r in breach_rows if r.escalated_to},
    )
    breaches = [
        Breach(
            owner_name=names.get(
                (r.owner_email or "").strip().lower(), r.owner_email or "unknown"
            ),
            company=_record_label(crm, r),
            kind=str(r.kind),
            age_label=_age_chip(r, now=now, names=names).label,
            # The recorded reason, in the order a reader wants it: why it was
            # accepted beats why the date moved.
            reason=(r.accepted_reason or r.promised_reason or None),
        )
        for r in breach_rows
    ]

    # ── Wrap-up completion rate, by AE, alphabetical ─────────────────────────
    team = (
        None
        if scope.unrestricted
        else {(e or "").strip().lower() for e in scope.owner_emails}
    )
    rate_query = db.query(
        func.lower(CallSession.ae_email),
        func.count(CallSession.id),
        func.count(CallSession.wrapped_at),
    ).filter(
        CallSession.status.in_(_WRAPPABLE_STATUSES),
        CallSession.created_at >= window_start,
    )
    if team:
        rate_query = rate_query.filter(func.lower(CallSession.ae_email).in_(team))
    rate_rows = rate_query.group_by(func.lower(CallSession.ae_email)).all()
    rate_names = _name_map(db, {r[0] for r in rate_rows if r[0]})
    wrapup_rates = sorted(
        (
            WrapupRate(
                owner_name=rate_names.get(ae, ae),
                rate=round((wrapped or 0) / total, 3) if total else 0.0,
            )
            for ae, total, wrapped in rate_rows
            if ae
        ),
        key=lambda r: r.owner_name.lower(),
    )

    # ── Stage-lag exception count, by AE, alphabetical ───────────────────────
    # Attributed the same local way My Day does it: a deal with a live
    # commitment belongs to that commitment's owner. Lags on deals with no
    # Cubie commitment are unattributable from local data and are counted in
    # forecast integrity below rather than pinned on a person.
    owner_by_deal: dict[str, str] = {}
    for deal_id, owner_email in (
        _own_scope(
            db.query(Commitment.hubspot_deal_id, func.lower(Commitment.owner_email))
            .filter(
                Commitment.closed_at.is_(None), Commitment.hubspot_deal_id.isnot(None)
            )
            .distinct()
        )
    ).all():
        owner_by_deal.setdefault(str(deal_id), owner_email)

    lag_counts: dict[str, int] = {}
    open_lag_deals: list[str] = []
    if owner_by_deal:
        for (deal_id,) in (
            db.query(CommitmentStageLag.hubspot_deal_id)
            .filter(
                CommitmentStageLag.status == "open",
                CommitmentStageLag.hubspot_deal_id.in_(list(owner_by_deal)),
            )
            .all()
        ):
            open_lag_deals.append(str(deal_id))
            owner = owner_by_deal.get(str(deal_id))
            if owner:
                lag_counts[owner] = lag_counts.get(owner, 0) + 1
    lag_names = _name_map(db, set(lag_counts))
    stage_lag_counts = sorted(
        (
            StageLagCount(owner_name=lag_names.get(email, email), count=count)
            for email, count in lag_counts.items()
        ),
        key=lambda r: r.owner_name.lower(),
    )

    # ── Forecast integrity ───────────────────────────────────────────────────
    # The dollar value behind the disagreement, which only HubSpot knows.
    lag_crm = (
        await run_in_threadpool(
            _crm_context, owner_email=None, deal_ids=open_lag_deals, company_ids=[]
        )
        if open_lag_deals
        else _Crm()
    )
    dollar_value = sum(
        lag_crm.deal_amounts.get(deal_id, 0.0) for deal_id in set(open_lag_deals)
    )
    forecast = ForecastIntegrity(
        count=len(set(open_lag_deals)),
        dollar_value=round(dollar_value, 2),
        # ``amounts_available``, not ``available``: a HubSpot that answered
        # about only some of these deals produces a real total that is short,
        # and a short total presented as complete is the number a manager would
        # take into a forecast meeting.
        dollar_value_available=lag_crm.amounts_available,
    )

    # ── Deferred deals ───────────────────────────────────────────────────────
    # Off the reps' lists until their follow-up dates, so listed here with the
    # owner, the date and the note: a defer must never hide a deal from the
    # person reviewing the pipeline. Nothing above excludes them either — a
    # deferred deal's breaches and stage lags still count. Ordered by date,
    # never by person.
    deferred_deals = _deferred_rows(db, scope)

    record_audit(
        db,
        actor_email=user.email,
        action="COCKPIT_MANAGER_VIEW",
        target="cockpit:manager",
        details={
            "team_size": len(scope.owner_emails),
            "unrestricted": scope.unrestricted,
            "breaches": len(breaches),
            "deferred_deals": len(deferred_deals),
        },
    )
    return ManagerResponse(
        tiles=tiles,
        breaches=breaches,
        wrapup_rates=wrapup_rates,
        stage_lag_counts=stage_lag_counts,
        forecast_integrity=forecast,
        team_size=len(scope.owner_emails),
        crm_available=crm.available,
        deferred_deals=deferred_deals,
    )


def _deferred_rows(db: Session, scope) -> list[DeferredDealRow]:
    """The team's deferred deals for the manager view. Never raises.

    Today is the org's calendar day: the view reads many owners, and a defer
    that ends today has already put its deal back on the rep's lists.
    """
    from rubik_core.sales.deal_defer import active_defers, local_day, org_today

    try:
        # A SAVEPOINT, so a failed read cannot poison the request's transaction
        # (the audit row written after this would fail with it on Postgres).
        with db.begin_nested():
            found = active_defers(
                db,
                today=org_today(),
                owner_emails=None if scope.unrestricted else scope.owner_emails,
            )
    except Exception:  # noqa: BLE001 — one section must not take the page down
        logger.warning("[cockpit] deferred deals could not be read", exc_info=True)
        return []
    names = _name_map(db, {d.owner_email for d in found})
    return [
        DeferredDealRow(
            owner_name=names.get(d.owner_email, d.owner_email),
            deal_name=d.deal_name or f"deal {d.deal_id}",
            follow_up_on=d.follow_up_on.isoformat(),
            deferred_on=local_day(d.deferred_by, d.created_at).isoformat(),
            note=d.note,
            hubspot_url=_hubspot_deal_url(d.deal_id),
        )
        for d in found
    ]


@router.get("/exec", response_model=ExecResponse, response_model_exclude_none=True)
@pin_business_time_zone
async def exec_summary(
    user: UserProfile = Depends(require_cockpit_exec),
    db: Session = Depends(get_db),
):
    """The exec roll-up — three numbers and a trend, org-wide, no per-person drill.

    Deliberately thin, and the thinness IS the design: keeping
    the exec view free of per-person breakdown is what stops it becoming the
    leaderboard Google's Meet API terms warn against — the API "isn't intended
    for performance tracking or user evaluation within your domain", and the
    capture-coverage number below is Meet-derived. If a name needs clicking
    into, the manager view exists behind ``require_cockpit_manager``.

    **Do not add a per-person breakdown to this response.** The gate cannot
    stop you; only this comment and a reviewer can.

      * open commitments past SLA — count and dollar value
      * median elapsed client-request → SOW-sent, trending by month
      * capture coverage — share of AE calls with a transcript

    Org-wide with no owner filter, which is what "Aggregates: org" means in the
    access table. No team scope is resolved: there is nothing to scope, and
    calling ``require_team_scope`` would 409 the org-wide exec over a missing
    Accountability Chart row for a view that does not use one.
    """
    now = datetime.utcnow()

    # ── Past SLA: count + dollar value ───────────────────────────────────────
    past_sla = (
        db.query(Commitment.id, Commitment.hubspot_deal_id)
        .filter(
            Commitment.closed_at.is_(None),
            Commitment.state.in_(_BREACH_STATES),
        )
        .limit(_MAX_ROWS_SCANNED)
        .all()
    )
    crm = await run_in_threadpool(
        _crm_context,
        owner_email=None,
        deal_ids=[d for _, d in past_sla if d],
        company_ids=[],
    )
    at_stake = sum(crm.deal_amounts.get(str(d), 0.0) for _, d in past_sla if d)

    # ── Median request → sent, and the same number by month ──────────────────
    # Business hours, through the one calculator. Closed SOW commitments only:
    # "sent" is the human-confirmed close (there is no stage close source and
    # no email close source, by design), so an open row has no elapsed time
    # yet and including it would drag the median toward zero as volume grows.
    sows = (
        db.query(Commitment.opened_at, Commitment.closed_at)
        .filter(
            Commitment.kind == "sow",
            Commitment.closed_at.isnot(None),
            Commitment.opened_at.isnot(None),
            Commitment.closed_at >= now - timedelta(days=_MEDIAN_WINDOW_DAYS),
        )
        .all()
    )
    day_hours = _day_hours()
    elapsed_days: list[float] = []
    by_period: dict[str, list[float]] = {}
    for opened_at, closed_at in sows:
        days = business_hours_between(opened_at, closed_at) / day_hours
        elapsed_days.append(days)
        by_period.setdefault(closed_at.strftime("%Y-%m"), []).append(days)
    median_days = round(statistics.median(elapsed_days), 1) if elapsed_days else 0.0
    trend = [
        TrendPoint(period=period, value=round(statistics.median(values), 1))
        for period, values in sorted(by_period.items())[-_TREND_PERIODS:]
    ]

    # ── Capture coverage ─────────────────────────────────────────────────────
    # Share of calls Cubie actually captured. Suppressed sessions are excluded
    # from BOTH halves: a confidential call is a first-class state (it produces
    # a session row and zero entries by design), so counting it as a capture
    # failure would make honouring consent look like a broken pipeline.
    captured_ids = db.query(CallTranscriptEntry.call_session_id).distinct()
    total_calls = (
        db.query(func.count(CallSession.id))
        .filter(
            CallSession.capture_consent == "granted",
            CallSession.ended_at.isnot(None),
        )
        .scalar()
        or 0
    )
    captured_calls = (
        db.query(func.count(CallSession.id))
        .filter(
            CallSession.capture_consent == "granted",
            CallSession.ended_at.isnot(None),
            CallSession.id.in_(captured_ids),
        )
        .scalar()
        or 0
    )
    coverage = round(100.0 * captured_calls / total_calls, 1) if total_calls else 0.0

    tiles = [
        Tile(
            label="Open commitments past SLA",
            value=str(len(past_sla)),
            hint=f"{PRODUCT_NAME}'s own clock, in business hours — not a HubSpot stage date",
        ),
        Tile(
            label="Value behind them",
            value=f"${at_stake:,.0f}" if crm.amounts_available else "—",
            hint=None
            if crm.amounts_available
            else "HubSpot could not be read in full, so this would be short",
        ),
        Tile(
            label="Median client request → SOW sent",
            value=f"{median_days:g} business days" if elapsed_days else "—",
            hint=f"{len(elapsed_days)} SOW{'' if len(elapsed_days) == 1 else 's'} "
            f"closed in the last {_MEDIAN_WINDOW_DAYS} days",
        ),
        Tile(
            label="Capture coverage",
            value=f"{coverage:g}%" if total_calls else "—",
            hint=f"{captured_calls} of {total_calls} calls have a transcript; "
            "confidential calls are excluded from both halves",
        ),
    ]

    record_audit(
        db,
        actor_email=user.email,
        action="COCKPIT_EXEC_VIEW",
        target="cockpit:exec",
        details={"past_sla": len(past_sla), "sows_measured": len(elapsed_days)},
    )
    return ExecResponse(tiles=tiles, trend=trend, crm_available=crm.available)


# ── Import-time invariants ───────────────────────────────────────────────────
# ``AGE_STATES`` is the contract the frontend's AgeChip is built against, and
# ``_STATE_TO_AGE_STATE`` is the only thing that produces a value for it. A
# one-sided edit — a new commitment state mapped to a chip state nobody
# renders — would ship a chip with no colour and no error anywhere. Fail the
# import instead. ``rubik_core/cockpit/stage_lag.py`` guards its enum pair the
# same way, for the same reason.
assert set(_STATE_TO_AGE_STATE.values()) <= set(AGE_STATES), (
    "_STATE_TO_AGE_STATE maps to a chip state the frontend contract does not "
    f"declare: {sorted(set(_STATE_TO_AGE_STATE.values()) - set(AGE_STATES))}"
)
assert set(_AGE_SEVERITY) == set(AGE_STATES), (
    "every AGE_STATES value needs a severity rank or it sorts last by accident "
    f"— missing {sorted(set(AGE_STATES) - set(_AGE_SEVERITY))}"
)
# The other side of the same map. ``_age_chip`` resolves an unmapped state to
# "open" by default, so a state added to the model and not to the map would
# render a breach as on-track — the one wrong answer this feature cannot
# afford, and one nothing else would report.
assert set(_STATE_TO_AGE_STATE) == set(COMMITMENT_STATES), (
    "_STATE_TO_AGE_STATE must cover every COMMITMENT_STATES value exactly — "
    f"missing {sorted(set(COMMITMENT_STATES) - set(_STATE_TO_AGE_STATE))}, "
    f"unknown {sorted(set(_STATE_TO_AGE_STATE) - set(COMMITMENT_STATES))}"
)

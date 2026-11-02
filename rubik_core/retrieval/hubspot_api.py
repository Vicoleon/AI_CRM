"""Live HubSpot CRM retrieval via the HubSpot REST API (CRM v3).

Replaces the BigQuery-backed ``HubSpotRetriever`` for the chat CRM path so
results are always current. Won deals are identified by HubSpot's computed
``hs_is_closed_won`` flag (stage-agnostic) — a portal with custom pipeline
stages can have ``dealstage = 'closedwon'`` match only a handful of the deals
actually won.

The REST API has no SQL/aggregate endpoint, so sum/avg KPIs page through all
matching records and aggregate client-side; count KPIs read the cheap ``total``
field. Blocked KPIs (ARR, churn) return a clear "needs HubSpot setup" message
rather than a wrong number.

Flow: an LLM maps the question to one intent + a timeframe + an optional owner;
Python runs the matching KPI recipe and formats a context block the coordinator
narrates.
"""

from __future__ import annotations

import json
import logging
import re
import statistics
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from rubik_core.ai_providers.registry import (
    IncompatibleModelRoute,
    resolve_model_route,
)
from rubik_core.ai_providers.runtime import ProviderRequestError
from rubik_core.brand import PRODUCT_NAME
from rubik_core.config import settings
from rubik_core.cost.usage import attribute_to
from rubik_core.graph.keys import normalize_domain
from rubik_core.inference_workloads import run_text_with_route
from rubik_core.logsafe import safe_error
from rubik_core.models import get_genai_client
from rubik_core.retrieval.hubspot_properties import (
    deal_product_property,
    deal_turnaround_property,
)
from rubik_core.tenancy.hubspot_config import get_hubspot_config
from rubik_core.tenancy.secrets import workspace_secret

logger = logging.getLogger(__name__)

# Engagement object types that count as a logged "activity".
_ACTIVITY_OBJECTS = ("calls", "emails", "notes", "tasks", "meetings")

_CRM_KEYWORDS = (
    "deal",
    "deals",
    "pipeline",
    "revenue",
    "won",
    "win",
    "lost",
    "closed",
    "close",
    "booked",
    "sales",
    "crm",
    "hubspot",
    "quota",
    "bookings",
    "client",
    "clients",
    "customer",
    "contact",
    "contacts",
    "meeting",
    "meetings",
    "activity",
    "activities",
    "quote",
    "proposal",
    "forecast",
    "cycle",
    "conversion",
    "churn",
    "arr",
    "upsell",
    "turn time",
    "turnaround",
    # Outreach/last-touch verbs — the flagship "when did we last reach out to X"
    # family used to miss the gate and silently fall through. NOT bare "account"
    # (HR/finance collision).
    "reach out",
    "reached out",
    "reaching out",
    "touch",
    "follow up",
    "followup",
    "follow-up",
    "hear from",
    "heard from",
    "check in",
    "check-in",
    "last contact",
    "last contacted",
    "gone cold",
    "going cold",
    "cold",
    # The sales daily-rhythm checklist's own words (rubik_core/sales/
    # rhythm_checks.py) — "opportunities over $30k with a single contact",
    # "waiting on the decision-maker", "close date pushed more than twice".
    "opportunity",
    "opportunities",
    "decision maker",
    "decision-maker",
    "single-threaded",
    "single threaded",
    "stakeholder",
    "stakeholders",
    "close date",
    "close dates",
    "overdue",
)
# Word-boundary match so "win" doesn't fire on "winterize", "cycle" on "bicycle", etc.
_CRM_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in _CRM_KEYWORDS) + r")\b", re.IGNORECASE
)

_CLARIFY = (
    "I can report these live HubSpot facts — for a timeframe (this or next week/"
    "month/quarter, etc.), optionally scoped to a specific rep or to you ('my'): "
    "deals won, deals created, deals expected to close in a period (the pipeline "
    "forecast), when a client/deal/contact was last contacted (last touch), which "
    "deals have gone cold, the stage / amount / owner of a specific deal, win rate, "
    "average deal value, sales-cycle length, open pipeline value, meetings booked/"
    "completed, new contacts, activities logged, turn times by product, and "
    "expected-to-close rate. For your day (or your team's) I can also list the "
    "deals closing this month that need action, deals waiting on the decision-"
    "maker, deals whose close date keeps moving, single-threaded deals, lost deals "
    "worth a check-in, records touched today missing a field, overdue tasks and "
    "close dates, and today's logged activity. Which would you like, and for what "
    "client/period?"
)

#: Planner intent → the daily-rhythm check that answers it
#: (``rubik_core/sales/rhythm_checks.py``). One definition per question, shared
#: with the My Day "Daily rhythm" cards, so chat and the card agree.
_RHYTHM_INTENTS: dict[str, str] = {
    "closing_focus": "closing_focus",
    "waiting_on_decision_maker": "waiting_on_decision_maker",
    "close_date_slippage": "close_date_slippage",
    "single_threaded": "single_threaded",
    "reengage_lost": "reengage_lost",
    "missing_fields": "missing_fields",
    "overdue_items": "overdue",
    "activity_recap": "activity_today",
}

#: How many rows a rhythm answer lists when the question names no number. The
#: checklist's own numbers for a rep ("which 3 deals", "find 5 deals"); the
#: team view asks for a "top 10".
_RHYTHM_DEFAULT_LIMITS: dict[str, tuple[int, int]] = {  # (mine, team)
    "closing_focus": (3, 10),
    "reengage_lost": (5, 10),
}


class HubSpotAPIError(RuntimeError):
    """Raised when the HubSpot REST API cannot be reached or returns an error.

    Its message never carries the token. A transport failure is where httpx
    quotes a header value it refused, ``Bearer`` token and all, so that
    message goes through
    ``safe_error`` and is raised ``from None``: a chained httpx error would
    print, unmasked, in every ``exc_info=True`` traceback.
    """


def _configured_deal_pipelines() -> frozenset[str]:
    try:
        return frozenset(get_hubspot_config().pipeline_ids)
    except Exception as exc:  # noqa: BLE001 - control failure closes CRM access
        logger.warning("HubSpot workspace config unavailable (%s)", type(exc).__name__)
        raise HubSpotAPIError(
            "HubSpot workspace configuration is unavailable"
        ) from None


def _workspace_deal_pipelines() -> frozenset[str]:
    """The selected workspace's deal boundary, required before a deal read."""
    allowed = _configured_deal_pipelines()
    if not allowed:
        raise HubSpotAPIError(
            "HubSpot deal pipelines are not configured for this workspace"
        )
    return allowed


def _scoped_deal_filters(filters: Sequence[dict]) -> list[dict]:
    return [
        *filters,
        {
            "propertyName": "pipeline",
            "operator": "IN",
            "values": sorted(_workspace_deal_pipelines()),
        },
    ]


# ── Date windows (the business time zone, half-open [start, end)) ───────────


def _tz() -> ZoneInfo:
    """KPI windows use the active workspace's validated control-row clock."""
    from rubik_core.tenancy.workspace_time import workspace_time_zone

    return workspace_time_zone()


def _ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=_tz()).timestamp() * 1000)


def _today() -> date:
    return datetime.now(_tz()).date()


def _quarter_start(d: date) -> date:
    return date(d.year, 3 * ((d.month - 1) // 3) + 1, 1)


def _add_months(d: date, n: int) -> date:
    """First day of the month ``n`` months after ``d`` (n may be negative)."""
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def resolve_window(
    relative_timeframe: str | None,
    start_date: str | None,
    end_date: str | None,
    full_period: bool = False,
) -> tuple[int | None, int | None, str]:
    """Return (start_ms, end_ms_exclusive, label). (None, None, "") if no window.

    ``full_period`` controls how the *current* period (this_week/this_month/
    this_quarter/this_year) ends. Backward-looking metrics (deals won, meetings
    held) want period-TO-DATE — you can't count the future — so the window ends
    at tomorrow (the default). Forward-looking metrics (deals expected to close)
    want the FULL calendar period so a deal projected to close later this month
    is not dropped; pass ``full_period=True`` for those. Past/future periods
    (last_*, next_*) are always full and ignore this flag.
    """
    if start_date and end_date:
        s = datetime.strptime(start_date, "%Y-%m-%d").date()
        e = datetime.strptime(end_date, "%Y-%m-%d").date() + timedelta(days=1)
        return _ms(s), _ms(e), f"{start_date} → {end_date}"

    if not relative_timeframe:
        return None, None, ""

    t = _today()
    tomorrow = t + timedelta(days=1)
    p = relative_timeframe.lower().strip().replace(" ", "_")
    s = e = t
    if p == "today":
        s, e = t, tomorrow
    elif p == "yesterday":
        s, e = t - timedelta(days=1), t
    elif p == "this_week":
        s = t - timedelta(days=t.weekday())
        e = s + timedelta(days=7) if full_period else tomorrow
    elif p == "last_week":
        this_mon = t - timedelta(days=t.weekday())
        s, e = this_mon - timedelta(days=7), this_mon
    elif p == "this_month":
        s = t.replace(day=1)
        e = _add_months(t, 1) if full_period else tomorrow
    elif p == "last_month":
        first = t.replace(day=1)
        s, e = (first - timedelta(days=1)).replace(day=1), first
    elif p == "next_month":
        s, e = _add_months(t, 1), _add_months(t, 2)
    elif p == "this_quarter":
        qs = _quarter_start(t)
        s, e = qs, (_add_months(qs, 3) if full_period else tomorrow)
    elif p == "last_quarter":
        qs = _quarter_start(t)
        s, e = _quarter_start(qs - timedelta(days=1)), qs
    elif p == "next_quarter":
        qs = _quarter_start(t)
        s, e = _add_months(qs, 3), _add_months(qs, 6)
    elif p == "next_week":
        this_mon = t - timedelta(days=t.weekday())
        s, e = this_mon + timedelta(days=7), this_mon + timedelta(days=14)
    elif p == "this_year":
        s = t.replace(month=1, day=1)
        e = date(t.year + 1, 1, 1) if full_period else tomorrow
    elif p == "last_year":
        s, e = date(t.year - 1, 1, 1), date(t.year, 1, 1)
    elif p == "next_year":
        s, e = date(t.year + 1, 1, 1), date(t.year + 2, 1, 1)
    elif p == "last_7_days":
        s, e = t - timedelta(days=7), t + timedelta(days=1)
    elif p == "last_30_days":
        s, e = t - timedelta(days=30), t + timedelta(days=1)
    elif p == "last_90_days":
        s, e = t - timedelta(days=90), t + timedelta(days=1)
    elif p == "next_7_days":
        s, e = t, t + timedelta(days=8)
    elif p == "next_30_days":
        s, e = t, t + timedelta(days=31)
    elif p == "next_90_days":
        s, e = t, t + timedelta(days=91)
    else:
        return None, None, ""
    label = f"{p.replace('_', ' ')} ({s.isoformat()} → {(e - timedelta(days=1)).isoformat()})"
    return _ms(s), _ms(e), label


def _date_range_filters(date_prop: str, start_ms: int, end_ms: int) -> list[dict]:
    return [
        {"propertyName": date_prop, "operator": "GTE", "value": str(start_ms)},
        {"propertyName": date_prop, "operator": "LT", "value": str(end_ms)},
    ]


def _fmt_money(v: float) -> str:
    return f"${v:,.0f}"


# ── REST client ──────────────────────────────────────────────────────────────


# Contact properties worth returning on a "find this person" lookup.
CONTACT_SEARCH_PROPERTIES = [
    "firstname",
    "lastname",
    "email",
    "phone",
    "company",
    "jobtitle",
    "lifecyclestage",
]


# ── Domain → company matching (calendar call capture) ────────────────────────

# The only properties a "which company is this meeting with?" decision needs.
# ``hs_object_id`` is requested as well as read off ``id`` so a response shape
# that omits the envelope id still yields a usable company id.
_COMPANY_MATCH_PROPS = ["name", "domain", "lifecyclestage", "hs_object_id"]

# Domains per search request. HubSpot's `IN` operator accepts a values array;
# 100 mirrors the batch size already used for id searches in backend/routers/
# cockpit.py, and keeps one calendar day's worth of domains in a single call.
_DOMAIN_MATCH_BATCH = 100


@dataclass(frozen=True)
class CompanyMatch:
    """One HubSpot company that claims an email domain.

    ``domain`` is the *requested* domain this company was paired on, not the raw
    property value — HubSpot stores ``www.acme.com`` and full URLs, and the UI
    has to show the same string the meeting's attendee list shows.
    """

    hubspot_company_id: str
    name: str
    domain: str
    lifecyclestage: str = ""


@dataclass(frozen=True)
class DomainMatch:
    """What HubSpot knows about one email domain — in three distinct states.

    The distinction is the whole point. ``unmatched`` ("no company owns this
    domain") and ``error`` ("we could not find out") are NOT the same answer, and
    collapsing them is how a sales wrap-up ends up writing to the wrong CRM
    record. ``matched`` is deliberately false for two or more candidates, so no
    caller can reproduce the ``candidates[0]`` bug this type exists to prevent —
    an ambiguous domain must be resolved by a human, not by list order.
    """

    domain: str
    candidates: tuple[CompanyMatch, ...] = ()
    error: str | None = None

    @property
    def matched(self) -> bool:
        """Exactly one candidate — the ONLY state that may prefill a company."""
        return self.error is None and len(self.candidates) == 1

    @property
    def ambiguous(self) -> bool:
        """Several companies share this domain; a human has to pick."""
        return self.error is None and len(self.candidates) > 1

    @property
    def unmatched(self) -> bool:
        """The lookup succeeded and no company owns this domain."""
        return self.error is None and not self.candidates

    @property
    def company(self) -> CompanyMatch | None:
        """The single company, or None. Never index ``candidates`` directly."""
        return self.candidates[0] if self.matched else None


def _fold_domain(value: str | None) -> str:
    """Lowercase + trim only — the exact form results are keyed by."""
    return (value or "").strip().lower()


def _pair_domain(row_domain: str | None, requested: set[str]) -> str | None:
    """Which requested domain a returned company row belongs to, or None.

    Tolerant on the way in (HubSpot stores domains as ``www.acme.com`` and as
    full URLs) via the repo's single normalizer, but strict about the answer: a
    row that pairs with nothing is dropped by the caller rather than assigned to
    the nearest-looking domain, which would bind a meeting to another client.
    """
    folded = _fold_domain(row_domain)
    if folded in requested:
        return folded
    normalized = normalize_domain(row_domain)
    if normalized and normalized in requested:
        return normalized
    return None


class HubSpotAPIClient:
    """Synchronous client over the HubSpot CRM v3 REST API."""

    PAGE = 200  # HubSpot search max page size
    MAX_RECORDS = 5000  # safety cap for client-side aggregation paging

    def __init__(self, token: str | None = None, base_url: str | None = None):
        raw_token = (
            token if token is not None else workspace_secret("hubspot", "read_token")
        )
        self.token = raw_token.strip() if raw_token else None
        self.base_url = (base_url or settings.HUBSPOT_API_BASE).rstrip("/")
        if not self.token:
            raise HubSpotAPIError(
                "HubSpot read token is not configured for this workspace"
            )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def _search(self, object_type: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}/crm/v3/objects/{object_type}/search"
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(url, headers=self._headers(), json=body)
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as e:
            body_txt = e.response.text[:300] if e.response is not None else ""
            raise HubSpotAPIError(
                f"HubSpot {object_type} search failed: "
                f"HTTP {e.response.status_code}: {body_txt}"
            ) from e
        except httpx.HTTPError as e:
            raise HubSpotAPIError(f"HubSpot transport error: {safe_error(e)}") from None

    def count(self, object_type: str, filters: list[dict]) -> int:
        """Cheap count via the search ``total`` field (one call, limit 1)."""
        if object_type == "deals":
            filters = _scoped_deal_filters(filters)
        data = self._search(
            object_type, {"filterGroups": [{"filters": filters}], "limit": 1}
        )
        return int(data.get("total", 0))

    def search_all(
        self,
        object_type: str,
        filters: list[dict],
        properties: list[str],
        sorts: list[dict] | None = None,
    ) -> list[dict]:
        """Page through ALL matches (capped) for client-side aggregation."""
        allowed = None
        if object_type == "deals":
            allowed = _workspace_deal_pipelines()
            filters = _scoped_deal_filters(filters)
            properties = list(dict.fromkeys((*properties, "pipeline")))
        out: list[dict] = []
        after: str | None = None
        # Count API pages, not just returned rows: a broken server-side filter
        # must not make a shared portal scan indefinitely while we discard rows.
        for _ in range((self.MAX_RECORDS + self.PAGE - 1) // self.PAGE):
            body: dict[str, Any] = {
                "filterGroups": [{"filters": filters}],
                "properties": properties,
                "limit": self.PAGE,
            }
            if sorts:
                body["sorts"] = sorts
            if after:
                body["after"] = after
            data = self._search(object_type, body)
            rows = data.get("results", [])
            if allowed is not None:
                rows = [
                    row
                    for row in rows
                    if (row.get("properties") or {}).get("pipeline") in allowed
                ]
            out.extend(rows)
            after = data.get("paging", {}).get("next", {}).get("after")
            if not after:
                break
        return out

    def search_all_groups(
        self,
        object_type: str,
        filter_groups: list[list[dict]],
        properties: list[str],
        sorts: list[dict] | None = None,
    ) -> list[dict]:
        """:meth:`search_all` with several OR'd filter groups.

        HubSpot ANDs the filters inside one group and ORs the groups, which is
        the only way to ask "created by me today OR last updated by me today"
        in one search. HubSpot caps the number of groups in one request.
        """
        allowed = None
        if object_type == "deals":
            allowed = _workspace_deal_pipelines()
            filter_groups = [
                _scoped_deal_filters(group) for group in (filter_groups or [[]])
            ]
            properties = list(dict.fromkeys((*properties, "pipeline")))
        out: list[dict] = []
        after: str | None = None
        for _ in range((self.MAX_RECORDS + self.PAGE - 1) // self.PAGE):
            body: dict[str, Any] = {
                "filterGroups": [{"filters": group} for group in filter_groups],
                "properties": properties,
                "limit": self.PAGE,
            }
            if sorts:
                body["sorts"] = sorts
            if after:
                body["after"] = after
            data = self._search(object_type, body)
            rows = data.get("results", [])
            if allowed is not None:
                rows = [
                    row
                    for row in rows
                    if (row.get("properties") or {}).get("pipeline") in allowed
                ]
            out.extend(rows)
            after = data.get("paging", {}).get("next", {}).get("after")
            if not after:
                break
        return out

    #: Records per batch read. HubSpot takes 100, but only 50 once
    #: ``propertiesWithHistory`` is asked for, so the history path uses the
    #: smaller number rather than discovering the limit as a 400.
    BATCH_READ = 100
    BATCH_READ_WITH_HISTORY = 50

    def batch_read(
        self,
        object_type: str,
        ids: Sequence[str],
        properties: Sequence[str],
        *,
        properties_with_history: Sequence[str] | None = None,
    ) -> list[dict]:
        """Read records by id, optionally with the full history of some properties.

        Returns HubSpot's raw result rows (``id``, ``properties`` and, when
        asked, ``propertiesWithHistory``). Raises ``HubSpotAPIError`` on any
        failed batch: a caller ranking by a property's history must not rank on
        half the records and present it as the whole list.
        """
        wanted = [str(i) for i in dict.fromkeys(str(i) for i in ids if i)]
        if not wanted:
            return []
        if object_type in ("contacts", "companies"):
            # A batch read exposes the complete shared record, not just the
            # requested deal. Prove every live deal association first. The
            # proof reads only deals, so this cannot recurse.
            owned = self.fully_owned_deal_associations(object_type, wanted)
            wanted = [record_id for record_id in wanted if record_id in owned]
            if not wanted:
                return []
        allowed = _workspace_deal_pipelines() if object_type == "deals" else None
        requested_properties = list(properties)
        read_properties = (
            list(dict.fromkeys((*requested_properties, "pipeline")))
            if allowed is not None
            else requested_properties
        )
        size = (
            self.BATCH_READ_WITH_HISTORY if properties_with_history else self.BATCH_READ
        )
        url = f"{self.base_url}/crm/v3/objects/{object_type}/batch/read"
        out: list[dict] = []
        try:
            with httpx.Client(timeout=30.0) as client:
                for start in range(0, len(wanted), size):
                    body: dict[str, Any] = {
                        "properties": read_properties,
                        "inputs": [{"id": i} for i in wanted[start : start + size]],
                    }
                    if properties_with_history:
                        body["propertiesWithHistory"] = list(properties_with_history)
                    resp = client.post(url, headers=self._headers(), json=body)
                    resp.raise_for_status()
                    rows = resp.json().get("results", [])
                    if allowed is not None:
                        rows = [
                            row
                            for row in rows
                            if (row.get("properties") or {}).get("pipeline") in allowed
                        ]
                    out.extend(rows)
        except httpx.HTTPStatusError as e:
            body_txt = e.response.text[:300] if e.response is not None else ""
            raise HubSpotAPIError(
                f"HubSpot {object_type} batch read failed: "
                f"HTTP {e.response.status_code}: {body_txt}"
            ) from e
        except httpx.HTTPError as e:
            raise HubSpotAPIError(f"HubSpot transport error: {safe_error(e)}") from None
        return out

    def batch_associations(
        self,
        from_type: str,
        to_type: str,
        ids: Sequence[str],
        *,
        prefer_type_id: int | None = None,
    ) -> dict[str, list[str]]:
        """``{from_id: [to_id, ...]}`` for many records in one v4 call per 500.

        Every requested id is a key in the result, so "no associations" and
        "not asked" cannot be confused. Raises ``HubSpotAPIError`` on failure.

        ``prefer_type_id`` keeps only a record's associations of that type when
        it has any, and all of them when it has none: deal→company type 5 is
        "Primary", so a deal linked to two companies reads its primary one, and
        an older deal with no primary label still reads the company it has.
        """
        wanted = [str(i) for i in dict.fromkeys(str(i) for i in ids if i)]
        out: dict[str, list[str]] = {i: [] for i in wanted}
        if not wanted:
            return out
        if from_type == "deals":
            # Association IDs do not include a pipeline. Resolve each source
            # deal through the fenced read before returning its relationships.
            allowed_ids = {
                str(row.get("id"))
                for row in self.batch_read("deals", wanted, ["pipeline"])
            }
            wanted = [deal_id for deal_id in wanted if deal_id in allowed_ids]
            if not wanted:
                return out
        if to_type == "deals":
            _workspace_deal_pipelines()
        url = f"{self.base_url}/crm/v4/associations/{from_type}/{to_type}/batch/read"
        try:
            with httpx.Client(timeout=30.0) as client:
                for start in range(0, len(wanted), 500):
                    resp = client.post(
                        url,
                        headers=self._headers(),
                        json={
                            "inputs": [{"id": i} for i in wanted[start : start + 500]]
                        },
                    )
                    # 207 = some inputs had no associations; the rows that did
                    # still come back in ``results``.
                    if resp.status_code not in (200, 207):
                        resp.raise_for_status()
                    for row in resp.json().get("results", []):
                        src = str((row.get("from") or {}).get("id") or "")
                        if not src:
                            continue
                        to = [t for t in row.get("to", []) if t.get("toObjectId")]
                        if prefer_type_id is not None:
                            preferred = [
                                t
                                for t in to
                                if any(
                                    a.get("typeId") == prefer_type_id
                                    for a in t.get("associationTypes") or []
                                )
                            ]
                            to = preferred or to
                        out.setdefault(src, []).extend(
                            str(t.get("toObjectId")) for t in to
                        )
        except httpx.HTTPStatusError as e:
            body_txt = e.response.text[:300] if e.response is not None else ""
            raise HubSpotAPIError(
                f"HubSpot {from_type}→{to_type} associations failed: "
                f"HTTP {e.response.status_code}: {body_txt}"
            ) from e
        except httpx.HTTPError as e:
            raise HubSpotAPIError(f"HubSpot transport error: {safe_error(e)}") from None
        if to_type == "deals":
            associated_ids = sorted(
                {deal_id for values in out.values() for deal_id in values}
            )
            visible_ids = {
                str(row.get("id"))
                for row in self.batch_read("deals", associated_ids, ["pipeline"])
            }
            out = {
                source: [deal_id for deal_id in values if deal_id in visible_ids]
                for source, values in out.items()
            }
        return out

    def _raw_association_ids(
        self, client: httpx.Client, object_type: str, record_id: str, to_type: str
    ) -> list[str]:
        """Read every v4 association page without the deal filter in batch_associations."""
        url = (
            f"{self.base_url}/crm/v4/objects/{object_type}/"
            f"{record_id}/associations/{to_type}"
        )
        related_ids: list[str] = []
        after: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(20):
            params: dict[str, Any] = {"limit": 500}
            if after is not None:
                params["after"] = after
            response = client.get(url, headers=self._headers(), params=params)
            response.raise_for_status()
            try:
                data = response.json()
            except ValueError:
                raise HubSpotAPIError(
                    "HubSpot association response is invalid"
                ) from None
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise HubSpotAPIError("HubSpot association response is incomplete")
            for row in data["results"]:
                if not isinstance(row, dict) or not re.fullmatch(
                    r"[A-Za-z0-9_-]{1,64}", str(row.get("toObjectId") or "")
                ):
                    raise HubSpotAPIError("HubSpot association response is invalid")
                related_ids.append(str(row["toObjectId"]))
            paging = data.get("paging")
            if paging is None:
                paging = {}
            if not isinstance(paging, dict):
                raise HubSpotAPIError("HubSpot association paging is invalid")
            next_page = paging.get("next")
            if next_page is None:
                break
            if (
                not isinstance(next_page, dict)
                or not isinstance(next_page.get("after"), str)
                or not next_page["after"]
                or next_page["after"] in seen_cursors
            ):
                raise HubSpotAPIError("HubSpot association paging is incomplete")
            after = next_page["after"]
            seen_cursors.add(after)
        else:
            raise HubSpotAPIError("HubSpot association scan exceeded safe limit")
        return list(dict.fromkeys(related_ids))

    def fully_owned_deal_associations(
        self, object_type: str, ids: Sequence[str]
    ) -> dict[str, list[str]]:
        """Return records only when their complete CRM associations are owned.

        The v4 batch association reader filters foreign deals, which is useful
        for deal-level views but cannot authorize a shared company or contact.
        Scan each record's complete paginated association set before publishing
        any record-level fields or writing to it. Activities and tasks also
        require every linked contact and company to pass the same strict proof.
        An incomplete scan is a denial.
        """
        if object_type not in ("companies", "contacts", *_ACTIVITY_OBJECTS):
            raise ValueError("Full ownership proof requires a supported CRM object")
        allowed = _workspace_deal_pipelines()  # refuse before any HTTP request
        wanted = list(dict.fromkeys(str(value) for value in ids if value))
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) for value in wanted):
            raise HubSpotAPIError("Invalid HubSpot record ID")
        associations: dict[str, list[str]] = {}
        try:
            with httpx.Client(timeout=30.0) as client:
                for record_id in wanted:
                    unique = self._raw_association_ids(
                        client, object_type, record_id, "deals"
                    )
                    if unique:
                        associations[record_id] = unique
        except httpx.HTTPError as exc:
            raise HubSpotAPIError(
                f"HubSpot ownership verification failed: {safe_error(exc)}"
            ) from None
        all_deal_ids = sorted(
            {deal_id for linked in associations.values() for deal_id in linked}
        )
        if not all_deal_ids:
            return {}
        rows = self.batch_read("deals", all_deal_ids, ["pipeline"])
        visible = {
            str(row.get("id"))
            for row in rows
            if (row.get("properties") or {}).get("pipeline") in allowed
        }
        owned = {
            record_id: linked
            for record_id, linked in associations.items()
            if set(linked).issubset(visible)
        }
        if object_type in ("contacts", "companies") or not owned:
            return owned

        # A task or engagement attached to an allowed deal can also carry a
        # foreign contact/company. Its subject, timestamp, and count are then
        # shared portal facts. Prove every other linked record before use.
        related: dict[str, dict[str, list[str]]] = {
            "contacts": {},
            "companies": {},
        }
        try:
            with httpx.Client(timeout=30.0) as client:
                for record_id in owned:
                    for to_type in related:
                        related[to_type][record_id] = self._raw_association_ids(
                            client, object_type, record_id, to_type
                        )
        except httpx.HTTPError as exc:
            raise HubSpotAPIError(
                f"HubSpot ownership verification failed: {safe_error(exc)}"
            ) from None
        trusted_contacts = set(
            self.fully_owned_deal_associations(
                "contacts",
                sorted(
                    {cid for values in related["contacts"].values() for cid in values}
                ),
            )
        )
        trusted_companies = set(
            self.fully_owned_deal_associations(
                "companies",
                sorted(
                    {cid for values in related["companies"].values() for cid in values}
                ),
            )
        )
        return {
            record_id: linked
            for record_id, linked in owned.items()
            if set(related["contacts"][record_id]).issubset(trusted_contacts)
            and set(related["companies"][record_id]).issubset(trusted_companies)
        }

    def search_objects(
        self,
        object_type: str,
        query: str,
        properties: list[str],
        limit: int = 10,
    ) -> list[dict]:
        """Full-text search via HubSpot's top-level ``query`` field.

        Unlike :meth:`search_all` (property filters, for aggregation), this uses
        HubSpot's cross-property full-text search — for contacts it spans name,
        email, phone and company — so a lookup by *either* a person's name or
        their email address hits the same record. Single page, capped at
        ``limit``; the right primitive for "find this person" lookups.
        """
        allowed = _workspace_deal_pipelines() if object_type == "deals" else None
        body: dict[str, Any] = {
            "query": query,
            "properties": (
                list(dict.fromkeys((*properties, "pipeline")))
                if allowed is not None
                else properties
            ),
            "limit": limit,
        }
        if allowed is not None:
            body["filterGroups"] = [
                {
                    "filters": [
                        {
                            "propertyName": "pipeline",
                            "operator": "IN",
                            "values": sorted(allowed),
                        }
                    ]
                }
            ]
        data = self._search(
            object_type,
            body,
        )
        rows = data.get("results", [])
        if allowed is not None:
            return [
                row
                for row in rows
                if (row.get("properties") or {}).get("pipeline") in allowed
            ]
        return rows

    def search_owned_objects(
        self,
        object_type: str,
        query: str,
        properties: list[str],
        limit: int = 10,
    ) -> list[dict]:
        """Search contacts/companies only after proving a workspace deal link.

        A shared portal can return a foreign contact or company for any text
        query. The association read verifies a live deal in this workspace's
        configured pipeline before any row reaches a caller.
        """
        if object_type not in ("contacts", "companies"):
            raise ValueError("Ownership search supports contacts and companies only")
        _workspace_deal_pipelines()  # refuse before the first HubSpot request
        rows = self.search_objects(
            object_type, query, properties, limit=max(limit, 100)
        )
        ids = [str(row.get("id")) for row in rows if row.get("id")]
        if not ids:
            return []
        owned = self.fully_owned_deal_associations(object_type, ids)
        return [row for row in rows if owned.get(str(row.get("id")))][:limit]

    def search_all_owned(
        self, object_type: str, filters: list[dict], properties: list[str]
    ) -> list[dict]:
        """Return only contacts, companies, or activities linked to owned deals.

        Search has no pipeline filter for these objects. Prove each result's
        live deal association before using it in a workspace answer. A capped
        search cannot yield a trustworthy total, so refuse that answer.
        """
        if object_type not in ("contacts", "companies", *_ACTIVITY_OBJECTS):
            raise ValueError("Ownership sweep does not support this object type")
        _workspace_deal_pipelines()  # refuse before the first HubSpot request
        rows = self.search_all(object_type, filters, properties)
        if len(rows) >= self.MAX_RECORDS:
            raise HubSpotAPIError(
                "HubSpot ownership sweep exceeds the safe result limit"
            )
        ids = [str(row.get("id")) for row in rows if row.get("id")]
        if not ids:
            return []
        owned = self.fully_owned_deal_associations(object_type, ids)
        return [row for row in rows if owned.get(str(row.get("id")))]

    def _deals_for_company(
        self, company_id: str, allowed_pipelines: set[str]
    ) -> list[dict]:
        """Associated deals for a company (v4 assoc → v3 batch read). Best-effort.

        Restricts to the active workspace's pipelines and any narrower caller
        selection, so a shared portal cannot expose another company's deals.
        """
        allowed_pipelines = _workspace_deal_pipelines().intersection(allowed_pipelines)
        if not allowed_pipelines or not company_id.strip():
            return []
        ids = self.fully_owned_deal_associations("companies", [company_id]).get(
            company_id, []
        )
        if not ids:
            return []
        rows = self.batch_read(
            "deals",
            ids,
            [
                "dealname",
                "hs_is_closed_won",
                "hs_is_closed",
                "amount",
                "closedate",
                "dealstage",
                "pipeline",
            ],
        )
        if {str(row.get("id")) for row in rows} != set(ids):
            return []
        return [
            {"id": row.get("id"), "props": row.get("properties") or {}}
            for row in rows
            if (row.get("properties") or {}).get("pipeline") in allowed_pipelines
        ]

    def company_relationships_for_domains(
        self, domains: list[str], allowed_pipelines: list[str] | None = None
    ) -> list[dict]:
        """Resolve each email domain → its HubSpot company + deal status.

        Powers grounded client-history in meeting prep: rather than let the model
        guess whether a client is active or a past bid was won, we read it from
        the CRM. Best-effort per domain — a lookup failure yields a row with
        ``found=False`` instead of raising.
        """
        try:
            configured = _workspace_deal_pipelines()
        except HubSpotAPIError as exc:
            return [
                {"domain": dom, "found": False, "error": str(exc)}
                for raw in domains
                if (dom := (raw or "").strip().lower())
            ]
        allowed = (
            configured.intersection(allowed_pipelines)
            if allowed_pipelines is not None
            else configured
        )
        if not allowed:
            return [
                {"domain": dom, "found": False, "error": "No HubSpot pipelines allowed"}
                for raw in domains
                if (dom := (raw or "").strip().lower())
            ]
        out: list[dict] = []
        for domain in domains:
            dom = (domain or "").strip().lower()
            if not dom:
                continue
            rel: dict[str, Any] = {"domain": dom, "found": False}
            try:
                companies = self._search(
                    "companies",
                    {
                        "filterGroups": [
                            {
                                "filters": [
                                    {
                                        "propertyName": "domain",
                                        "operator": "EQ",
                                        "value": dom,
                                    }
                                ]
                            }
                        ],
                        "properties": ["name", "domain", "lifecyclestage"],
                        "limit": 1,
                    },
                ).get("results", [])
                if not companies:
                    out.append(rel)
                    continue
                company = companies[0]
                company_id = str(company.get("id") or "").strip()
                if not company_id:
                    out.append(rel)
                    continue
                # A domain match in a shared portal proves nothing about this
                # workspace. Keep the company name/status private until an
                # associated deal passes the workspace pipeline fence.
                deals = self._deals_for_company(company_id, allowed)
                if not deals:
                    out.append(rel)
                    continue
                props = company.get("properties", {}) or {}
                rel.update(
                    found=True,
                    company_name=props.get("name") or dom,
                    lifecyclestage=props.get("lifecyclestage") or "",
                )
                won = [d for d in deals if d["props"].get("hs_is_closed_won") == "true"]
                open_deals = [
                    d for d in deals if d["props"].get("hs_is_closed") == "false"
                ]
                rel["won_count"] = len(won)
                rel["open_count"] = len(open_deals)
                rel["open_value"] = sum(
                    _num(d["props"], "amount") or 0.0 for d in open_deals
                )
                won_sorted = sorted(
                    won,
                    key=lambda d: d["props"].get("closedate") or "",
                    reverse=True,
                )
                if won_sorted:
                    p = won_sorted[0]["props"]
                    rel["last_won"] = {
                        "name": p.get("dealname") or "(unnamed deal)",
                        "closedate": (p.get("closedate") or "")[:10],
                    }
            except Exception as exc:  # best-effort per domain — never break prep
                logger.warning(
                    "HubSpot company lookup failed for %s (%s)",
                    dom,
                    type(exc).__name__,
                )
                rel = {"domain": dom, "found": False, "error": "HubSpot lookup failed"}
            out.append(rel)
        return out

    def companies_for_domains(
        self, domains: Sequence[str], *, properties: Sequence[str] | None = None
    ) -> dict[str, DomainMatch]:
        """Email domain → the HubSpot companies that claim it, in ONE search.

        Written for the calendar call-capture screen, where the company binding
        decides which CRM record a wrap-up writes to. Three properties follow
        from that:

        * **Every requested domain is a key in the result.** A caller can never
          read a miss as "I forgot to ask" — absence is impossible, and the
          three ``DomainMatch`` states carry the verdict instead.
        * **It never raises.** A failed batch marks only its own domains with
          ``error``; the rest of the day's meetings still resolve.
        * **It never guesses.** A returned row whose ``domain`` cannot be paired
          back to a requested domain is dropped with a warning rather than
          attached to whichever domain looks close, and a truncated response
          errors the batch rather than letting two candidates look like one.

        Batches of ``_DOMAIN_MATCH_BATCH`` domains per request via the ``IN``
        operator. ``IN`` is an established pattern in this client, but ``IN`` on
        the ``domain`` property specifically is not otherwise exercised against
        this portal: if HubSpot rejects it, the fallback is the ``EQ``-per-domain
        loop that ``company_relationships_for_domains`` already uses (one call
        per domain instead of one per hundred).

        No retry lives here — ``_search`` raises on 429/5xx and this method
        converts that to ``error``. Callers that want backoff should go through
        ``rubik_core.call_capture.domain_match.resolve_domain_matches``, which
        wraps this in ``xray.fetchers._with_backoff``. Being sync ``httpx``, an
        ``async def`` handler must call it through ``run_in_threadpool``.
        """
        props = list(properties or _COMPANY_MATCH_PROPS)
        wanted: list[str] = []
        for raw in domains or []:
            dom = _fold_domain(raw)
            if dom and dom not in wanted:
                wanted.append(dom)
        out: dict[str, DomainMatch] = {d: DomainMatch(domain=d) for d in wanted}
        try:
            _workspace_deal_pipelines()
        except HubSpotAPIError as exc:
            return {d: DomainMatch(domain=d, error=str(exc)) for d in wanted}

        for start in range(0, len(wanted), _DOMAIN_MATCH_BATCH):
            batch = wanted[start : start + _DOMAIN_MATCH_BATCH]
            try:
                data = self._search(
                    "companies",
                    {
                        "filterGroups": [
                            {
                                "filters": [
                                    {
                                        "propertyName": "domain",
                                        "operator": "IN",
                                        "values": batch,
                                    }
                                ]
                            }
                        ],
                        "properties": props,
                        # Explicit: the default page size is smaller than the
                        # documented max and would truncate silently.
                        "limit": self.PAGE,
                    },
                )
            except Exception as exc:  # noqa: BLE001 - any failure is one verdict
                message = "HubSpot company lookup failed"
                logger.warning(
                    "HubSpot domain batch failed (%d domains; %s)",
                    len(batch),
                    type(exc).__name__,
                )
                for dom in batch:
                    out[dom] = DomainMatch(domain=dom, error=message)
                continue

            results = data.get("results") or []
            total = int(data.get("total") or 0)
            more = bool((data.get("paging") or {}).get("next"))
            if more or total > len(results):
                # A truncated page can hide a second company for a domain, which
                # would render as a confident (wrong) prefill. Refuse instead.
                message = (
                    f"HubSpot returned {len(results)} of {total or 'unknown'} "
                    "matching companies; results incomplete"
                )
                logger.warning("%s for %d domains", message, len(batch))
                for dom in batch:
                    out[dom] = DomainMatch(domain=dom, error=message)
                continue

            paired: list[tuple[str, str, dict]] = []
            requested = set(batch)
            for row in results:
                row_props = row.get("properties") or {}
                key = _pair_domain(row_props.get("domain"), requested)
                if key is None:
                    logger.warning("HubSpot company has an unrequested domain; dropped")
                    continue
                company_id = str(
                    row.get("id") or row_props.get("hs_object_id") or ""
                ).strip()
                if not company_id:
                    logger.warning("HubSpot company row for %s has no id", key)
                    continue
                paired.append((key, company_id, row_props))

            try:
                # A matching domain can belong to more than one workspace's
                # deals. Company fields are safe only after a full proof.
                visible = self.fully_owned_deal_associations(
                    "companies", [company_id for _, company_id, _ in paired]
                )
            except Exception as exc:  # noqa: BLE001 - ownership failure is one verdict
                logger.warning(
                    "HubSpot company ownership lookup failed (%d domains; %s)",
                    len(batch),
                    type(exc).__name__,
                )
                for dom in batch:
                    out[dom] = DomainMatch(
                        domain=dom, error="HubSpot company ownership lookup failed"
                    )
                continue

            grouped: dict[str, list[CompanyMatch]] = {}
            for key, company_id, row_props in paired:
                if not visible.get(company_id):
                    continue
                grouped.setdefault(key, []).append(
                    CompanyMatch(
                        hubspot_company_id=company_id,
                        name=(row_props.get("name") or "").strip() or key,
                        domain=key,
                        lifecyclestage=(row_props.get("lifecyclestage") or "").strip(),
                    )
                )

            for dom in batch:
                found = grouped.get(dom) or []
                # Stable order so an ambiguous row renders identically each poll.
                found.sort(key=lambda c: (c.name.lower(), c.hubspot_company_id))
                out[dom] = DomainMatch(domain=dom, candidates=tuple(found))

        return out

    def owner_directory(self) -> dict[str, dict[str, str]]:
        """{ownerId: {"email": <lower>, "name": "First Last", "user_id": "…"}}.

        Best-effort. ``user_id`` is the HubSpot USER id, a different number from
        the owner id — it is what ``hs_created_by_user_id`` and
        ``hs_updated_by_user_id`` hold, so "records I touched today" needs it.
        """
        url = f"{self.base_url}/crm/v3/owners"
        out: dict[str, dict[str, str]] = {}
        try:
            with httpx.Client(timeout=20.0) as client:
                after: str | None = None
                for _ in range(10):
                    params: dict[str, Any] = {"limit": 100}
                    if after:
                        params["after"] = after
                    resp = client.get(url, headers=self._headers(), params=params)
                    resp.raise_for_status()
                    data = resp.json()
                    for o in data.get("results", []):
                        nm = f"{o.get('firstName', '')} {o.get('lastName', '')}".strip()
                        out[str(o.get("id"))] = {
                            "email": (o.get("email") or "").lower(),
                            "name": nm or o.get("email", "Unknown"),
                            "user_id": str(o.get("userId") or ""),
                        }
                    after = (
                        data.get("paging", {}).get("next", {}).get("after")
                        if data.get("paging")
                        else None
                    )
                    if not after:
                        break
        except httpx.HTTPError as e:
            logger.warning("Could not load HubSpot owners: %s", e)
        return out

    def owner_name_map(self) -> dict[str, str]:
        """{ownerId: 'First Last'} (best-effort, never raises)."""
        return {oid: d["name"] for oid, d in self.owner_directory().items()}

    def deal_pipeline_stages(self) -> dict[str, dict[str, str]]:
        """{pipelineId: {stageId: 'Stage Label'}} (best-effort, never raises).

        Unlike :meth:`deal_stage_labels`, which flattens every pipeline into one
        map, this keeps the pipeline grouping — the only way to answer "is this
        stage valid *for this deal*". Stage ids are shared across the company's
        pipelines, so a flat lookup would happily accept a stage from a pipeline
        the deal isn't in. CRM Sync needs the grouped form to validate a
        proposed ``dealstage`` before it is ever offered for approval.
        """
        url = f"{self.base_url}/crm/v3/pipelines/deals"
        out: dict[str, dict[str, str]] = {}
        try:
            with httpx.Client(timeout=20.0) as client:
                resp = client.get(url, headers=self._headers())
                resp.raise_for_status()
                for pipe in resp.json().get("results", []):
                    stages = {
                        str(st.get("id")): st.get("label") or str(st.get("id"))
                        for st in pipe.get("stages", [])
                    }
                    out[str(pipe.get("id"))] = stages
        except httpx.HTTPError as e:
            logger.warning("Could not load HubSpot deal pipelines: %s", e)
        return out

    def deal_stage_labels(self) -> dict[str, str]:
        """{stageId: 'Stage Label'} across all deal pipelines (best-effort)."""
        url = f"{self.base_url}/crm/v3/pipelines/deals"
        out: dict[str, str] = {}
        try:
            with httpx.Client(timeout=20.0) as client:
                resp = client.get(url, headers=self._headers())
                resp.raise_for_status()
                for pipe in resp.json().get("results", []):
                    for st in pipe.get("stages", []):
                        out[str(st.get("id"))] = st.get("label") or str(st.get("id"))
        except httpx.HTTPError as e:
            logger.warning("Could not load HubSpot deal stages: %s", e)
        return out


def format_client_relationship_block(rels: list[dict]) -> str:
    """Render company relationships (from company_relationships_for_domains) as a
    grounded CONTEXT block for a prompt, or "" if there's nothing to say."""
    lines: list[str] = []
    for r in rels or []:
        if not r.get("found"):
            lines.append(f"- {r.get('domain')}: no matching company found in HubSpot.")
            continue
        parts = [r.get("company_name") or r.get("domain")]
        stage = (r.get("lifecyclestage") or "").strip()
        if stage:
            parts.append(f"lifecycle stage: {stage}")
        won = r.get("won_count", 0)
        if won:
            lw = r.get("last_won") or {}
            lw_txt = (
                f' (most recent: "{lw.get("name")}", {lw.get("closedate")})'
                if lw
                else ""
            )
            parts.append(f"{won} won deal(s){lw_txt}")
        else:
            parts.append("no won deals on record")
        opn = r.get("open_count", 0)
        if opn:
            val = r.get("open_value") or 0.0
            parts.append(f"{opn} open deal(s)" + (f" (~${val:,.0f})" if val else ""))
        lines.append("- " + "; ".join(str(p) for p in parts) + ".")
    if not lines:
        return ""
    return (
        "CLIENT RELATIONSHIP (HubSpot CRM, live — authoritative for deal/"
        "engagement status; prefer this over any inference from transcripts):\n"
        + "\n".join(lines)
    )


# ── Filter helpers ───────────────────────────────────────────────────────────

_WON = {"propertyName": "hs_is_closed_won", "operator": "EQ", "value": "true"}
_OPEN = {"propertyName": "hs_is_closed", "operator": "EQ", "value": "false"}


def _owner_filter(owner_id: str | None) -> list[dict]:
    return (
        [{"propertyName": "hubspot_owner_id", "operator": "EQ", "value": owner_id}]
        if owner_id
        else []
    )


def _status_filters(status: str | None) -> list[dict]:
    """Deal-search filters for a status keyword. Default/'open' → open deals only
    (byte-stable with the pre-existing hard-coded ``[_OPEN]``)."""
    s = (status or "open").lower()
    if s == "won":
        return [_WON]
    if s == "lost":
        return [
            {"propertyName": "hs_is_closed", "operator": "EQ", "value": "true"},
            {"propertyName": "hs_is_closed_won", "operator": "EQ", "value": "false"},
        ]
    if s == "closed":
        return [{"propertyName": "hs_is_closed", "operator": "EQ", "value": "true"}]
    if s == "any":
        return []
    return [_OPEN]


def _modeled_filters(filters: Any) -> tuple[list[dict], list[str]]:
    """Translate planner ``filters`` qualifiers into deal-search filters.

    Returns ``(search_filters, dropped)`` where ``dropped`` names qualifier fields
    we can't express as a live filter (product/dealtype) — the caller
    caveats the answer and logs ``partial_filter`` rather than silently pretending
    the qualifier was applied.
    """
    out: list[dict] = []
    dropped: list[str] = []
    for f in filters or []:
        if not isinstance(f, dict):
            continue
        fld = (f.get("field") or "").lower()
        val = f.get("value")
        if fld == "amount_gte" and val not in (None, ""):
            out.append({"propertyName": "amount", "operator": "GTE", "value": str(val)})
        elif fld == "dealstage" and val:
            out.append(
                {"propertyName": "dealstage", "operator": "EQ", "value": str(val)}
            )
        elif fld:
            dropped.append(fld)
    return out, dropped


def _num(props: dict, key: str) -> float | None:
    try:
        v = props.get(key)
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _parse_hs_datetime(v: Any) -> datetime | None:
    """Parse a HubSpot datetime value (ISO-8601 string or epoch-ms) to aware UTC."""
    if v in (None, ""):
        return None
    s = str(v)
    try:
        if s.isdigit():  # epoch milliseconds
            return datetime.fromtimestamp(int(s) / 1000, tz=timezone.utc)
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _days_since(v: Any, *, tz: ZoneInfo | None = None) -> int | None:
    """Whole days between a HubSpot datetime and now (None if unparseable/empty)."""
    dt = _parse_hs_datetime(v)
    if dt is None:
        return None
    return (datetime.now(tz or _tz()) - dt).days


def _plausible_date(v: Any) -> str | None:
    """Return ``v`` only if it's a real, recent date. HubSpot returns epoch-ish
    junk (e.g. ``1970-01-21``) for unset date rollups; those must not read as a
    real "last activity". Anything that fails to parse or predates 2005 is None.
    A company can carry ``hs_last_sales_activity_date`` = ``1970-01-21`` while
    its ``notes_last_contacted`` is a real, recent date."""
    dt = _parse_hs_datetime(v)
    if dt is None or dt.year < 2005:
        return None
    return v


def _max_plausible(*values: Any) -> str | None:
    """Most-recent plausible date among ``values`` (junk/None dropped), or None.

    Used for company last-touch = MAX(company rollup, deal-level max): a
    lagging-but-present company rollup must NOT mask a fresher deal contact.
    """
    best: str | None = None
    best_dt: datetime | None = None
    for v in values:
        pv = _plausible_date(v)
        if pv is None:
            continue
        dt = _parse_hs_datetime(pv)
        if dt is None:
            continue
        if best_dt is None or dt > best_dt:
            best, best_dt = pv, dt
    return best


# ── Target resolution (company | deal | contact | rep) ───────────────────────
# The general outreach/last-touch resolver turns a planner spec into ONE concrete
# HubSpot entity (single object) or an owner-scoped SET, so a single last-touch
# primitive answers "when did we last contact <anything>" without a bespoke intent
# per phrasing. `status` drives an honest answer on every non-happy path — a
# fabricated number is never returned. `match_basis` records HOW confidently the
# entity was picked so low-confidence auto-picks can be watched.

_RESOLVE_OK = "ok"
_RESOLVE_NOT_FOUND = "not_found"
_RESOLVE_MULTI = "multi_match"
_RESOLVE_NO_OWNER = "no_owner_match"
_RESOLVE_UNAVAILABLE = "unavailable"
_RESOLVE_DEFERRED = "deferred"

# Recency rollups live on all three object types; owners (reps) have none — a
# rep's recency is aggregated over the objects they own (open deals).
_RECENCY_PROPS = ["notes_last_contacted", "notes_last_updated"]


@dataclass
class ResolvedTarget:
    """The concrete HubSpot entity (or owner-scoped set) a CRM question is about."""

    target_type: str  # company | deal | contact | rep
    is_set: bool  # False = one named object (point); True = an owner/team sweep
    status: str = _RESOLVE_OK
    obj: dict | None = None  # the resolved single object's HubSpot record
    obj_id: str | None = None
    display_name: str = ""
    owner_id: str | None = None  # set for owner-scoped sets
    set_object_type: str | None = None  # "deals" | "companies" | "contacts"
    candidates: list[dict] = field(default_factory=list)  # multi_match options
    match_basis: str = ""  # exact | single_fuzzy | first_of_many | owner_email | none
    note: str = ""


# ── KPI implementations (each returns a formatted context block) ─────────────


def _kpi_deals_created(c, w, owner, scope):
    s, e, label = w
    if s is None:
        return "Specify a timeframe for deals created (e.g. this month)."
    n = c.count("deals", _date_range_filters("createdate", s, e) + _owner_filter(owner))
    return f"Deals created {label}{scope}: **{n}**."


def _kpi_deals_won(c, w, owner, scope):
    s, e, label = w
    filt = [_WON] + _owner_filter(owner)
    if s is not None:
        filt += _date_range_filters("closedate", s, e)
    # Count cheaply, then page for the $ sum + a sample list.
    n = c.count("deals", filt)
    deals = c.search_all(
        "deals",
        filt,
        ["dealname", "amount", "closedate", "hubspot_owner_id"],
        sorts=[{"propertyName": "closedate", "direction": "DESCENDING"}],
    )
    total_amt = sum(_num(d["properties"], "amount") or 0 for d in deals)
    # `n` (cheap `total`) is the true count; the $ sum covers only the paged set.
    # They agree unless the won set exceeds the paging cap — caveat the total if so.
    capped = len(deals) >= HubSpotAPIClient.MAX_RECORDS and len(deals) < n
    total_str = _fmt_money(total_amt) + (
        f" (sum over the first {len(deals):,} of {n:,})" if capped else ""
    )
    names = c.owner_name_map()
    lines = [
        f"Deals won{(' ' + label) if label else ' (all time)'}{scope}: "
        f"**{n}** deal(s), total value **{total_str}**."
    ]
    for i, d in enumerate(deals[:15], 1):
        p = d["properties"]
        own = names.get(str(p.get("hubspot_owner_id")), "—")
        amt = _num(p, "amount")
        lines.append(
            f"{i}. {p.get('dealname', 'Unnamed')} — "
            f"{_fmt_money(amt) if amt is not None else '—'} | "
            f"closed {(p.get('closedate') or '')[:10]} | {own}"
        )
    if n > 15:
        lines.append(f"…and {n - 15} more.")
    return "\n".join(lines)


def _kpi_win_rate(c, w, owner, scope):
    s, e, label = w
    if s is None:
        return "Specify a timeframe for win rate (e.g. this quarter)."
    created_filt = _date_range_filters("createdate", s, e) + _owner_filter(owner)
    denom = c.count("deals", created_filt)
    numer = c.count("deals", created_filt + [_WON])
    if denom == 0:
        return f"No deals were created {label}{scope}, so win rate is undefined."
    rate = numer / denom * 100
    return (
        f"Win rate {label}{scope}: **{rate:.1f}%** "
        f"({numer} won of {denom} created in the period). "
        f"(Cohort = deals *created* in the window that are now won.)"
    )


def _kpi_avg_deal_value(c, w, owner, scope):
    s, e, label = w
    filt = [_WON] + _owner_filter(owner)
    if s is not None:
        filt += _date_range_filters("closedate", s, e)
    deals = c.search_all("deals", filt, ["amount"])
    amts = [
        a for a in (_num(d["properties"], "amount") for d in deals) if a is not None
    ]
    if not amts:
        return (
            f"No won deals with an amount found{(' ' + label) if label else ''}{scope}."
        )
    avg = sum(amts) / len(amts)
    return (
        f"Average deal value{(' ' + label) if label else ' (all time)'}{scope}: "
        f"**{_fmt_money(avg)}** across {len(amts)} won deal(s) "
        f"(total {_fmt_money(sum(amts))})."
    )


def _kpi_sales_cycle(c, w, owner, scope):
    s, e, label = w
    filt = [_WON] + _owner_filter(owner)
    if s is not None:
        filt += _date_range_filters("closedate", s, e)
    deals = c.search_all("deals", filt, ["days_to_close"])
    # days_to_close is a HubSpot computed property already expressed in days.
    days = [
        v
        for v in (_num(x["properties"], "days_to_close") for x in deals)
        if v is not None
    ]
    if not days:
        return f"No won deals with a close time found{(' ' + label) if label else ''}{scope}."
    avg = sum(days) / len(days)
    median = statistics.median(days)
    return (
        f"Sales-cycle length{(' ' + label) if label else ' (all time)'}{scope}: "
        f"average **{avg:.0f} days**, median **{median:.0f} days** "
        f"(n={len(days)} won deals; median is the better guide — the distribution "
        f"is right-skewed)."
    )


def _kpi_pipeline_value(c, w, owner, scope):
    filt = [_OPEN] + _owner_filter(owner)
    deals = c.search_all(
        "deals",
        filt,
        ["dealname", "amount", "dealstage", "hubspot_owner_id"],
        sorts=[{"propertyName": "amount", "direction": "DESCENDING"}],
    )
    total = sum(_num(d["properties"], "amount") or 0 for d in deals)
    return (
        f"Open pipeline value{scope}: **{_fmt_money(total)}** across "
        f"**{len(deals)}** open deal(s) (excludes won and lost)."
    )


def _kpi_meetings(c, w, owner, scope):
    s, e, label = w
    if s is None:
        return "Specify a timeframe for meetings (e.g. this week)."
    rng = _date_range_filters("hs_meeting_start_time", s, e) + _owner_filter(owner)
    meetings = c.search_all_owned("meetings", rng, ["hs_meeting_outcome"])
    booked = len(meetings)
    held = sum(
        (row.get("properties") or {}).get("hs_meeting_outcome") == "COMPLETED"
        for row in meetings
    )
    return (
        f"Meetings {label}{scope}: **{booked} booked**, **{held} completed**. "
        "(Completed depends on the meeting outcome being logged.)"
    )


def _kpi_new_contacts(c, w, owner, scope):
    s, e, label = w
    if s is None:
        return "Specify a timeframe for new contacts (e.g. this month)."
    n = len(
        c.search_all_owned(
            "contacts",
            _date_range_filters("createdate", s, e) + _owner_filter(owner),
            [],
        )
    )
    return f"New contacts created {label}{scope}: **{n}**."


def _kpi_activities_logged(c, w, owner, scope):
    s, e, label = w
    if s is None:
        return "Specify a timeframe for activities (e.g. last 30 days)."
    rng = _date_range_filters("hs_timestamp", s, e) + _owner_filter(owner)
    per: dict[str, int] = {}
    for obj in _ACTIVITY_OBJECTS:
        per[obj] = len(c.search_all_owned(obj, rng, []))
    total = sum(per.values())
    breakdown = ", ".join(f"{k}: {v}" for k, v in per.items())
    return (
        f"Activities logged {label}{scope}: **{total}** "
        f"({breakdown}). (Tasks count by due date.)"
    )


#: What the turn-times answer says while no turnaround field is named.
TURN_TIMES_NOT_SET_UP = (
    "Turn times aren't set up yet: they come from a deal field your company "
    "keeps in HubSpot for how long a deal took to turn around, and an admin "
    f"needs to tell {PRODUCT_NAME} which field that is."
)


def _kpi_turn_times_by_product(c, w, owner, scope):
    """Median turnaround of won deals, grouped by what each deal was for.

    Both fields are the company's own (``rubik_core/retrieval/
    hubspot_properties.py``): with no turnaround field named the answer says
    so; with no product field named every deal falls in one group.
    """
    turnaround = deal_turnaround_property()
    if not turnaround:
        return TURN_TIMES_NOT_SET_UP
    product = deal_product_property()
    s, e, label = w
    filt = [
        _WON,
        {"propertyName": turnaround, "operator": "HAS_PROPERTY"},
    ] + _owner_filter(owner)
    if s is not None:
        filt += _date_range_filters("closedate", s, e)
    deals = c.search_all("deals", filt, [p for p in (turnaround, product) if p])
    buckets: dict[str, list[float]] = {}
    for d in deals:
        tt = _num(d["properties"], turnaround)
        if tt is None:
            continue
        days = tt / 86400000.0  # HubSpot stores a duration in milliseconds
        if product:
            prod = d["properties"].get(product) or "(unspecified)"
        else:
            prod = "All won deals"
        buckets.setdefault(prod, []).append(days)
    if not buckets:
        return f"No closed-won deals with a turn time found{(' ' + label) if label else ''}{scope}."
    lines = [
        f"Turn times by product{(' ' + label) if label else ''}{scope} (median days):"
    ]
    for prod, vals in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        med = statistics.median(vals)
        lines.append(f"- {prod}: median **{med:.0f}d** (n={len(vals)})")
    return "\n".join(lines)


def _kpi_expected_to_close(c, w, owner, scope):
    """Open deals whose *expected* close date falls in the window — the pipeline
    forecast ("what's expected/estimated to close this month / next month").

    Filters OPEN deals by ``closedate``. On an open deal ``closedate`` is the
    PROJECTED close date, so this mirrors HubSpot's "Deals Estimated Close"
    dashboard. It must never be confused with ``createdate`` (when the deal was
    created) — filtering the pipeline by ``createdate`` silently undercounts a
    forecast question, which is exactly the bug this handler exists to prevent.
    """
    s, e, label = w
    if s is None:
        return (
            "Specify a timeframe for expected-to-close deals "
            "(e.g. this month, next month, this quarter)."
        )
    filt = [_OPEN] + _owner_filter(owner) + _date_range_filters("closedate", s, e)
    deals = c.search_all(
        "deals",
        filt,
        [
            "dealname",
            "amount",
            "closedate",
            "hubspot_owner_id",
            "hs_deal_stage_probability",
        ],
        sorts=[{"propertyName": "closedate", "direction": "ASCENDING"}],
    )
    if not deals:
        return (
            f"No open deals are expected to close {label}{scope} "
            f"(by HubSpot close date)."
        )
    names = c.owner_name_map()
    total = sum(_num(d["properties"], "amount") or 0 for d in deals)
    weighted = sum(
        (_num(d["properties"], "amount") or 0)
        * (_num(d["properties"], "hs_deal_stage_probability") or 0)
        for d in deals
    )
    lines = [
        f"Deals expected to close {label}{scope}: **{len(deals)}** open deal(s), "
        f"total value **{_fmt_money(total)}** "
        f"(probability-weighted **{_fmt_money(weighted)}**). "
        f"[open deals whose HubSpot close date falls in the window]"
    ]
    for i, d in enumerate(deals[:20], 1):
        p = d["properties"]
        own = names.get(str(p.get("hubspot_owner_id")), "—")
        amt = _num(p, "amount")
        lines.append(
            f"{i}. {p.get('dealname', 'Unnamed')} — "
            f"{_fmt_money(amt) if amt is not None else '—'} | "
            f"expected close {(p.get('closedate') or '')[:10]} | {own}"
        )
    if len(deals) > 20:
        lines.append(f"…and {len(deals) - 20} more.")
    return "\n".join(lines)


def _kpi_expected_close_rate(c, w, owner, scope):
    s, e, _label = w
    filt = [_OPEN] + _owner_filter(owner)
    # If a timeframe is given, scope to open deals whose PROJECTED close date
    # (closedate) falls in it — matching the "expected to close" framing.
    if s is not None:
        filt += _date_range_filters("closedate", s, e)
    deals = c.search_all("deals", filt, ["amount", "hs_deal_stage_probability"])
    probs = [
        p
        for p in (_num(d["properties"], "hs_deal_stage_probability") for d in deals)
        if p is not None
    ]
    if not probs:
        return f"No open deals with a stage probability found{scope}."
    avg_prob = sum(probs) / len(probs) * 100
    # Amount-weighted expected revenue.
    weighted = sum(
        (_num(d["properties"], "amount") or 0)
        * (_num(d["properties"], "hs_deal_stage_probability") or 0)
        for d in deals
    )
    return (
        f"Expected-to-close (open pipeline){scope}: average stage probability "
        f"**{avg_prob:.1f}%** across {len(probs)} open deal(s); "
        f"probability-weighted expected revenue **{_fmt_money(weighted)}**."
    )


# Feasible KPIs: intent_key → handler
_KPI_HANDLERS = {
    "deals_created": _kpi_deals_created,
    "deals_won": _kpi_deals_won,
    "win_rate": _kpi_win_rate,
    "avg_deal_value": _kpi_avg_deal_value,
    "sales_cycle": _kpi_sales_cycle,
    "pipeline_value": _kpi_pipeline_value,
    "meetings_booked_held": _kpi_meetings,
    "new_contacts": _kpi_new_contacts,
    "activities_logged": _kpi_activities_logged,
    "turn_times_by_product": _kpi_turn_times_by_product,
    "expected_to_close": _kpi_expected_to_close,
    "expected_close_rate": _kpi_expected_close_rate,
}

# Intents that are blocked or need a business decision — return an honest message
# instead of a wrong number. Each message says what the portal would need; none
# claims to know how this company's portal is filled in.
_UNAVAILABLE = {
    "arr": (
        "ARR isn't reportable from chat yet. It needs HubSpot's recurring-revenue "
        "fields (`hs_mrr`, `hs_arr`) or MRR/contract-term fields filled in on deals "
        "(or a recurring-revenue pipeline), backfilled for past deals."
    ),
    "churn_rate": (
        "Churn rate isn't reportable yet. HubSpot has no standard churn-date or "
        "active/churned status field on companies or deals, and the only proxies "
        "(lifecycle re-stamps) are unreliable. To enable it, create a Company-level "
        "`churn_date` plus an explicit Active/Churned status, set when a client "
        "leaves, and backfill."
    ),
    "avg_rev_per_client": (
        "Average revenue per client needs deal→company association walking (not a "
        "single REST call). I can report total won revenue and deal count now; the "
        "per-client average is a follow-up."
    ),
    "quotes_proposals_sent": (
        "Quotes/proposals sent isn't cleanly reportable: many teams send proposals "
        "outside HubSpot's Quotes object. It can be approximated by deals entering a "
        "'proposal sent' stage, but the exact stage to count needs confirming."
    ),
    "stage_conversion_rate": (
        "Stage conversion rate needs a chosen stage pair and pipeline (stages "
        "can be shared across pipelines with the same IDs) plus stage-history fields. "
        "Tell me the two stages and the pipeline and I can wire it up."
    ),
    "upsell_rate": (
        "Upsell/cross-sell rate has no backing field — HubSpot has no standard "
        "Upsell/Cross-Sell Y/N property on deals. It can be proxied via the deal "
        "type (existing business), but that undercounts every deal left without a "
        "deal type. A dedicated deal flag is the real fix."
    ),
    "lead_response_time": (
        "Lead response time isn't computable live — there's no 'first sales activity' "
        "field, so it requires walking every contact's engagements (a batch job). "
        "Worth building as a scheduled metric rather than an on-demand chat answer."
    ),
    "forecasting_score": (
        "A forecasting score isn't computed yet. The closest live signal is the "
        "expected-to-close rate (stage probability), which I can report."
    ),
    "followup_ratio": (
        "Follow-up ratio needs its definition pinned down (which numerator: total "
        "contacts, customer-stage contacts, or summed follow-up activities?). Tell me "
        "the definition and I'll wire it up."
    ),
}


def _rhythm_profile(caller_email: str | None):
    """The signed-in caller's Cubie profile, for the rhythm roster checks.

    None when there is no caller (the MCP server, repro scripts) or no user row
    — which the rhythm path treats as a closed door.
    """
    if not caller_email:
        return None
    try:
        from rubik_core.auth.registry import UserRegistry  # local: DB-backed

        return UserRegistry().get_user(caller_email)
    except Exception:  # noqa: BLE001 — an unreadable registry is a closed door
        logger.warning("[rhythm] profile lookup failed for %s", caller_email)
        return None


class HubSpotAPIRetriever:
    """LLM-routed, REST-executed CRM/KPI retrieval against the live HubSpot account."""

    def __init__(self):
        self._client = None
        self.model_id = settings.VERTEX_LLM_MODEL_FLASH

    @property
    def client(self):
        if self._client is None:
            self._client = get_genai_client()
        return self._client

    @client.setter
    def client(self, value):
        self._client = value

    @attribute_to("hubspot_retrieval", only_if_unset=True)
    def _plan(self, query: str) -> dict[str, Any]:
        today = _today().isoformat()
        intents = (
            list(_KPI_HANDLERS)
            + list(_UNAVAILABLE)
            + list(_RHYTHM_INTENTS)
            + ["deal_list", "outreach_recency", "general"]
        )
        prompt = f"""Today is {today}. Map the user's HubSpot/CRM question to ONE intent
plus a few ORTHOGONAL routing fields (the entity it's about, the scope, the verb).
Return ONLY a JSON object, no prose.

Fields:
- "intent": one of {intents}.
    deals_created/deals_won/win_rate/avg_deal_value/sales_cycle/pipeline_value/
    meetings_booked_held/new_contacts/activities_logged/turn_times_by_product/
    expected_to_close/expected_close_rate are KPIs. arr, churn_rate,
    avg_rev_per_client, quotes_proposals_sent, stage_conversion_rate, upsell_rate,
    lead_response_time, forecasting_score, followup_ratio are recognized but not
    yet computable.
    IMPORTANT — forecast questions:
    "expected_to_close" = the LIST/total of open deals PROJECTED to close in a
    period. Use it for "deals expected to close", "estimated to close", "deals
    closing this month/next month", "what's going to close", "pipeline closing
    this quarter", "forecast for next month". ALWAYS pair it with a
    relative_timeframe (this_month, next_month, this_quarter, next_quarter, …).
    "expected_close_rate" = ONLY the aggregate stage-probability metric ("expected
    close RATE", "average win probability", "how likely is the pipeline to close").
    Do NOT use expected_close_rate when the user wants a list/total of deals.
    "outreach_recency" = ANY question about WHEN we last reached out / HOW LONG since
    contact / which things have GONE COLD / the stage-amount-owner of a SPECIFIC
    entity, for whichever entity the question names. This is the general path — set
    "target_type", "target_name", "owner_scope", "answer_kind" (below) instead of
    inventing a new intent. Examples:
      "when did we last reach out to Globex?" -> {{"intent":"outreach_recency",
        "answer_kind":"recency","target_type":"company","target_name":"Globex"}}
      "which of my deals have I not contacted in 30 days?" -> {{"intent":
        "outreach_recency","answer_kind":"recency","target_type":"rep",
        "owner_scope":"me","stale_days":30}}
      "what stage is the Northwind Expansion deal in?" -> {{"intent":"outreach_recency",
        "answer_kind":"status","target_type":"deal","target_name":"Northwind Expansion"}}
      "when did we last talk to Pat at Globex?" -> {{"intent":"outreach_recency",
        "answer_kind":"recency","target_type":"contact","target_name":"Pat at Globex"}}
    "deal_list" = a plain list of deals (won/open/lost) with no forecast/recency framing.
    SALES DAILY-RHYTHM intents — ranked worklists; always set owner_scope ("me" for
    my/I, "team" for my team / across the team / we):
    "closing_focus" = which deals closing THIS MONTH need action today or are at
      risk of slipping ("which 3 deals closing this month need immediate action",
      "top 10 deals closing this month across my team"). NOT a plain forecast list.
    "waiting_on_decision_maker" = deals in a proposal / negotiation / SOW / contract
      stage waiting on a reply from the client or decision-maker.
    "close_date_slippage" = deals whose close date has been pushed / moved
      repeatedly, optionally together with "no activity in N days". A question ONLY
      about deals going cold / not contacted stays outreach_recency.
    "single_threaded" = open deals / opportunities above a value with only one
      (a single) contact — single-threaded risk.
    "reengage_lost" = previously LOST deals worth re-engaging (lost to timing or
      budget, contact still at the company), for a check-in message.
    "missing_fields" = contacts / accounts / deals created or updated TODAY that are
      missing fields (job title, persona, buying role, next steps) — CRM hygiene.
    "overdue_items" = overdue / past-due tasks and deals whose close date passed.
    "activity_recap" = today's logged calls, emails, meetings, notes and stage
      changes — an end-of-day recap or update for my manager.
    "general" = anything else CRM-related.
- "answer_kind": one of ["recency","attribute","status","owner","summary"] or null
    (default recency). recency = when / how long since / gone cold. attribute = a
    field value (amount, close date). status = deal stage. owner = who owns it.
    summary = everything on the named entity.
- "target_type": one of ["company","deal","contact","rep"] or null. The ENTITY the
    question is about (not the metric). REQUIRED when intent=outreach_recency. Pick by
    the noun: an account/client name -> company; a specific deal/renewal/project name ->
    deal; a person's name/email/"Pat at Globex" -> contact; a rep name or "me" -> rep.
- "target_name": the literal company/deal/contact name or email the question names
    (e.g. "Northwind Expansion"), else null when it's about a whole book of business.
- "owner_scope": one of ["me","named","team"] or null. "me" for first person ("my
    deals","who have I not contacted","deals I own"); "named" when a specific rep is
    named (ALSO fill owner_name); "team"/"we"/"across the book" for the whole team.
- "relative_timeframe": one of [today, yesterday, this_week, last_week, next_week,
    this_month, last_month, next_month, this_quarter, last_quarter, next_quarter,
    this_year, last_year, next_year, last_7_days, last_30_days, last_90_days,
    next_7_days, next_30_days, next_90_days] or null.
- "stale_days": for a cold-scan — the number of days of no outreach that counts as
    "gone cold" (e.g. 10 for "not reached out to in the past 10 days"); default 7.
    Null when the user only asks WHEN (a single named entity's last touch).
- "filters": a list (possibly empty) of {{"field":..,"value":..}} qualifiers where
    field is one of ["amount_gte","dealstage","product","dealtype"]; else [].
- "start_date"/"end_date": "YYYY-MM-DD" if an explicit range is given, else null.
- "owner_name": a sales rep name if the question names one, else null.
- "deal_status": "won","open","lost","closed","any" (else null). Applies to deal_list
    AND to an outreach cold-scan (e.g. "which WON clients haven't I checked in with").
- "limit": how many items the user asked for ("3 deals" -> 3, "top 10" -> 10,
    "find 5 deals" -> 5, "list all" / "every" / "the full list" -> 50), else null.

USER QUESTION: {query}

JSON:"""
        pinned_route = None
        try:
            pinned_route = resolve_model_route(
                "crm_retrieval_plan", "json", default_model=self.model_id
            )
            response_text, _ = run_text_with_route(
                "crm_retrieval_plan",
                prompt,
                default_model=self.model_id,
                json_mode=True,
                pinned_route=pinned_route,
                managed=lambda pinned: (
                    self.client.models.generate_content(
                        model=pinned.model, contents=prompt
                    ).text
                ),
            )
            m = re.search(r"\{.*\}", response_text or "", re.DOTALL)
            spec = json.loads(m.group(0) if m else (response_text or "{}"))
            if not isinstance(spec, dict):
                raise ValueError("CRM plan must be a JSON object")
            return spec
        except (IncompatibleModelRoute, ProviderRequestError):
            raise HubSpotAPIError(
                "The selected AI model is unavailable for CRM planning. Check the workspace AI provider settings."
            ) from None
        except Exception:
            if pinned_route is not None and pinned_route.provider != "vertex_managed":
                raise HubSpotAPIError(
                    "The selected AI model returned an invalid CRM plan. Check the workspace AI provider settings."
                ) from None
            logger.warning("HubSpot plan failed; will ask for clarification")
            return {"intent": "general", "_plan_error": "CRM planning unavailable"}

    def retrieve(
        self, query: str, caller_email: str | None = None
    ) -> tuple[str, str | None]:
        """Returns (context_block, debug_json). Empty block if not CRM-related.

        ``caller_email`` (the signed-in user) lets "my"/"me"/"I" questions
        resolve to the caller's own HubSpot owner. Optional so non-chat callers
        (the MCP server, repro scripts) keep working unchanged.

        Every call funnels through exactly ONE structured ``[crm-query]`` log line
        (see :meth:`_log_crm_query`) so unhandled/ambiguous phrasings surface
        automatically as coverage gaps instead of silently degrading to _CLARIFY.
        """
        t0 = time.monotonic()
        outcome: dict[str, Any] = {"resolution": "plan_error", "gap": True}
        try:
            return self._retrieve_impl(query, caller_email, outcome)
        except HubSpotAPIError:
            outcome.update(resolution="api_error", gap=True)
            raise
        finally:
            outcome["latency_ms"] = int((time.monotonic() - t0) * 1000)
            try:
                self._log_crm_query(query, outcome, caller_email)
            except Exception:  # a logging failure must never break the answer
                logger.debug("crm-query log failed", exc_info=True)

    def _retrieve_impl(
        self, query: str, caller_email: str | None, outcome: dict[str, Any]
    ) -> tuple[str, str | None]:
        # Gate: not obviously CRM. The coordinator already classified this as a CRM
        # question by calling the tool, so a keyword miss is itself signal — record a
        # gate_miss gap so flagship phrasings the regex omits still surface.
        if not _CRM_RE.search(query):
            outcome.update(resolution="gate_miss", gap=True, intent=None)
            return "", None

        # The chat planner can turn even a company question into a deal walk.
        # Refuse the whole CRM answer until the workspace names its pipelines;
        # this check runs before constructing a client or making any HubSpot call.
        allowed_pipelines = set(_configured_deal_pipelines())
        if not allowed_pipelines:
            outcome.update(resolution="unavailable", gap=True)
            return (
                "--- HubSpot CRM (live) ---\nAn admin needs to choose this "
                "workspace's HubSpot deal pipelines in Admin → Workspace "
                "before I can answer CRM questions.",
                None,
            )

        client = HubSpotAPIClient()  # raises HubSpotAPIError if no token
        spec = self._plan(query)

        # Legacy alias shim: older planners / monkeypatched tests may still emit the
        # narrow intents that outreach_recency subsumes. Normalize BEFORE dispatch so
        # The deals_needing_followup and last_touch behavior routes unchanged.
        intent = (spec.get("intent") or "general").lower()
        if intent == "deals_needing_followup":
            intent = "outreach_recency"
            spec["intent"] = intent
            spec.setdefault("target_type", "deal")
            spec.setdefault("answer_kind", "recency")
        elif intent == "last_touch":
            intent = "outreach_recency"
            spec["intent"] = intent
            spec["target_type"] = "company"
            spec.setdefault("answer_kind", "recency")
        # last_touch back-compat: company_name -> target_name.
        if spec.get("company_name") and not spec.get("target_name"):
            spec["target_name"] = spec.get("company_name")

        owner_scope = (spec.get("owner_scope") or "").strip().lower()
        owner_name = (spec.get("owner_name") or "").strip().lower()
        outcome.update(
            intent=intent,
            answer_kind=(spec.get("answer_kind") or "recency"),
            target_type=spec.get("target_type"),
            target_name_present=bool(spec.get("target_name")),
            owner_scope=owner_scope or None,
        )
        if spec.get("_plan_error"):
            outcome["resolution"] = "plan_error"

        # Forward-looking intents window the CURRENT period to its full calendar span.
        forward = intent in (
            "expected_to_close",
            "expected_close_rate",
            "outreach_recency",
        ) or (
            intent == "deal_list" and (spec.get("deal_status") or "").lower() == "open"
        )
        window = resolve_window(
            spec.get("relative_timeframe"),
            spec.get("start_date"),
            spec.get("end_date"),
            full_period=forward,
        )
        debug = json.dumps({"spec": spec})

        # Resolve owner scope -> owner_id (+ label). owner_scope=me matches the
        # caller's email to a HubSpot owner; a named rep uses the exact-then-substring
        # map (unchanged behavior). owner_failed records a miss so downstream paths
        # degrade honestly rather than answering account-wide as if it were the rep.
        owner_id: str | None = None
        owner_failed = False
        scope = ""
        if owner_scope == "me":
            owner_id, who = self._resolve_owner_self(client, caller_email)
            if owner_id:
                scope = f" (owner: {who})"
                outcome["match_basis"] = "owner_email"
            else:
                owner_failed = True
                outcome["owner_note"] = who
        elif owner_name:
            owners = client.owner_name_map()
            exact = [
                (oid, nm) for oid, nm in owners.items() if owner_name == nm.lower()
            ]
            partial = [
                (oid, nm) for oid, nm in owners.items() if owner_name in nm.lower()
            ]
            match = exact or partial
            if match:
                owner_id, nm = match[0]
                scope = f" (owner: {nm})"
            else:
                owner_failed = True
                scope = f' (⚠️ no HubSpot owner matched "{owner_name}"; account-wide)'

        # A KPI/deal_list "my X" with no owner match degrades honestly (never answers
        # account-wide as if it were the caller's own book). outreach_recency handles
        # its own no_owner_match inside the resolver so it can still answer a NAMED
        # entity even when self-scoping fails.
        if owner_scope == "me" and owner_failed and intent != "outreach_recency":
            outcome.update(resolution="no_owner_match", gap=True)
            note = outcome.get("owner_note", "")
            return (
                f"--- HubSpot CRM (live) ---\nI couldn't match you "
                f"({caller_email or 'unknown'}) to a HubSpot owner, so I can't scope "
                f"this to just your records{(' — ' + note) if note else ''}. Ask about "
                f"a named rep, or the team as a whole.",
                debug,
            )

        # Sales daily-rhythm worklists — one definition shared with the My Day
        # "Daily rhythm" cards (rubik_core/sales/rhythm_checks.py).
        if intent in _RHYTHM_INTENTS:
            block = self._rhythm(
                client,
                spec,
                intent,
                owner_id,
                owner_scope,
                owner_failed,
                caller_email,
                outcome,
            )
            return block, debug

        # Blocked / decision-needed KPIs: honest message, no fake number.
        if intent in _UNAVAILABLE:
            outcome.update(resolution="unavailable", gap=True)
            return f"--- HubSpot CRM (live) ---\n{_UNAVAILABLE[intent]}", debug

        # Feasible KPI (optionally self-scoped via owner_scope=me above).
        handler = _KPI_HANDLERS.get(intent)
        if handler:
            block = handler(client, window, owner_id, scope)
            outcome.update(resolution="answered", gap=False)
            return f"--- HubSpot CRM (live) ---\n{block}", debug

        # General outreach/last-touch resolver (company | deal | contact | rep).
        # The resolver returns a header-complete block (its cold-scan delegate emits
        # its own header), so it is NOT re-wrapped here.
        if intent == "outreach_recency":
            block = self._outreach_recency(
                client,
                spec,
                window,
                owner_id,
                owner_scope,
                owner_failed,
                scope,
                allowed_pipelines,
                outcome,
                hide_deferred_for=caller_email if owner_scope == "me" else None,
            )
            return block, debug

        # Plain deal list (won/open/etc. + timeframe).
        if intent == "deal_list":
            outcome.update(resolution="answered", gap=False)
            return self._deal_list(client, spec, window, owner_id, scope), debug

        # "general" / unrecognized CRM question — clarify instead of dumping won deals.
        outcome.update(resolution="clarify", gap=True)
        return f"--- HubSpot CRM (live) ---\n{_CLARIFY}", debug

    def _rhythm(
        self,
        c,
        spec,
        intent,
        owner_id,
        owner_scope,
        owner_failed,
        caller_email,
        outcome,
    ) -> str:
        """Run one daily-rhythm check for the caller, a named rep, or the team.

        The rosters are enforced HERE, not by which chips the client shows:

        * your own deals need ``sales_rhythm`` (or the team key);
        * the team, or a NAMED colleague, needs ``sales_rhythm_team`` — those
          are cross-person views and go to people added by name;
        * the activity log (``activity_recap``) is only ever your own, and the
          team or a colleague's overdue view leaves their tasks out — a
          colleague's to-dos and call notes are a view of the person, past-due
          close dates are a view of the pipeline.

        A question with no stated scope is about YOU, never the team: widening
        "summarize my calls today" to everyone's calls because the planner left
        ``owner_scope`` empty would be a confident wrong answer and a leak.
        """
        from rubik_core.features import feature_allows
        from rubik_core.sales.rhythm_checks import (  # local: avoid an import cycle
            chat_copy,
            format_for_chat,
            resolve_scope,
            run_check,
        )

        h = "--- HubSpot CRM (live) ---\n"
        check = _RHYTHM_INTENTS[intent]
        outcome["rhythm_check"] = check
        if not getattr(settings, "SALES_RHYTHM_ENABLED", True):
            outcome.update(resolution="unavailable", gap=True)
            return h + "The sales daily-rhythm checks are switched off here."
        if owner_failed:
            outcome.update(resolution="no_owner_match", gap=True)
            return (
                h + "I couldn't match that rep to a HubSpot owner, so I can't run "
                "this check for them. Ask about yourself, or about the team."
            )
        profile = _rhythm_profile(caller_email)
        if profile is None:
            outcome.update(resolution="unavailable", gap=True)
            return h + f"These checks need a signed-in {PRODUCT_NAME} user."

        def allowed(key: str) -> bool:
            try:
                return bool(feature_allows(key, profile))
            except Exception:  # noqa: BLE001 — an unreadable roster is a closed door
                logger.warning("[rhythm] feature check failed for %s", key)
                return False

        # The signed-in chat user's own deals: their own Gmail and Calendar
        # count — except while an admin is impersonating them, which stays
        # HubSpot-only (the Workspace credential lock refuses it anyway).
        from rubik_core.auth.impersonation import impersonation_active

        own = resolve_scope(c, caller_email, own_mailbox=not impersonation_active())
        own_id = own.owner_ids[0] if own and own.owner_ids else None
        if owner_scope == "team" and owner_id is None:
            kind = "team"
        elif owner_id is not None and owner_id != own_id:
            kind = "named"
        else:
            kind = "self"

        if kind == "self":
            if not (allowed("sales_rhythm") or allowed("sales_rhythm_team")):
                outcome.update(resolution="unavailable", gap=True)
                return (
                    h + "The sales daily-rhythm checks aren't switched on for your "
                    "team yet. An admin can add your team in Admin → Feature Access."
                )
            if own is None:
                outcome.update(resolution="no_owner_match", gap=True)
                return (
                    h + f"I couldn't match you ({caller_email}) to a HubSpot owner, "
                    "so I can't run this check on your deals. Ask about the team as "
                    "a whole if you are on the team view."
                )
            scope = own
        else:
            if not allowed("sales_rhythm_team"):
                outcome.update(resolution="unavailable", gap=True)
                return (
                    h + "This check reads other people's deals, and that view is "
                    "only open to people an admin has added by name (Sales daily "
                    "rhythm — team view). I can run it on your own deals instead."
                )
            if check == "activity_today":
                outcome.update(resolution="unavailable", gap=True)
                return (
                    h + "Today's activity log is only available for your own "
                    "records — a colleague's calls and notes are a view of the "
                    "person, not of the pipeline."
                )
            scope = resolve_scope(
                c, caller_email, team=kind == "team", owner_id=owner_id
            )
            if scope is None:  # pragma: no cover — team/named always resolve
                outcome.update(resolution="no_owner_match", gap=True)
                return h + "I couldn't work out whose deals to read."

        kwargs: dict[str, Any] = {}
        if check == "overdue":
            kwargs["include_tasks"] = kind == "self"
        # Thresholds the question named ("over $50k", "in 10 days") are used,
        # never silently replaced by the checklist's defaults.
        min_amount = None
        for f in spec.get("filters") or []:
            if isinstance(f, dict) and (f.get("field") or "").lower() == "amount_gte":
                try:
                    min_amount = float(f.get("value"))
                except (TypeError, ValueError):
                    min_amount = None
        quiet_days = None
        try:
            quiet_days = int(spec.get("stale_days")) if spec.get("stale_days") else None
        except (TypeError, ValueError):
            quiet_days = None
        if check == "single_threaded" and min_amount:
            kwargs["min_amount"] = min_amount
        if check in ("close_date_slippage", "closing_focus") and quiet_days:
            kwargs["quiet_days"] = quiet_days

        result = run_check(check, c, scope, **kwargs)
        # Deferred deals (rubik_core/sales/deal_defer.py): off the caller's own
        # answer until their follow-up date, with a note saying how many; kept
        # and marked on a team or colleague view, like the manager roll-ups.
        from rubik_core.sales.deal_defer import active_by_deal, apply_to_check

        result = apply_to_check(
            result,
            active_by_deal(
                {str(r.record_id) for r in result.rows if r.object_type == "deal"},
                reader=caller_email,
            ),
            hide=kind == "self",
        )
        mine, theirs = _RHYTHM_DEFAULT_LIMITS.get(check, (20, 20))
        raw_limit = spec.get("limit")
        if isinstance(raw_limit, str) and raw_limit.strip().lower() in ("all", "every"):
            asked = 50
        else:
            try:
                asked = int(raw_limit or 0)
            except (TypeError, ValueError):
                asked = 0
        limit = max(1, min(asked or (theirs if kind != "self" else mine), 50))
        if result.available:
            outcome.update(resolution="answered", gap=False)
        else:
            outcome.update(resolution="unavailable", gap=True)
        return format_for_chat(
            result,
            scope,
            limit=limit,
            copy=chat_copy(
                check,
                min_amount=min_amount,
                quiet_days=quiet_days,
                own=bool(scope.mailbox),
            ),
        )

    def _deal_list(self, c, spec, window, owner_id, scope) -> str:
        raw = (spec.get("deal_status") or "won").lower()
        status = raw if raw in ("won", "open", "lost", "closed", "any") else "any"
        s, e, label = window
        filt: list[dict] = list(_owner_filter(owner_id))
        if status == "won":
            filt.append(_WON)
        elif status == "open":
            filt.append(_OPEN)
        elif status == "closed":
            filt.append(
                {"propertyName": "hs_is_closed", "operator": "EQ", "value": "true"}
            )
        elif status == "lost":
            filt += [
                {"propertyName": "hs_is_closed", "operator": "EQ", "value": "true"},
                {
                    "propertyName": "hs_is_closed_won",
                    "operator": "EQ",
                    "value": "false",
                },
            ]
        # "any" adds no status filter — avoid an unbounded all-deals dump by
        # defaulting to this year when no timeframe was given.
        # Open deals are windowed by closedate — their PROJECTED close date — so
        # "open deals this/next month" means the pipeline expected to CLOSE then,
        # not deals created then (filtering the pipeline by createdate silently
        # undercounts a forecast question). Won/lost/closed use their actual
        # closedate; only the mixed "any" set falls back to createdate.
        date_prop = "createdate" if status == "any" else "closedate"
        if s is None and status == "any":
            s, e, label = resolve_window("this_year", None, None)
        if s is not None:
            filt += _date_range_filters(date_prop, s, e)
        deals = c.search_all(
            "deals",
            filt or [{"propertyName": "createdate", "operator": "HAS_PROPERTY"}],
            ["dealname", "amount", "closedate", "hubspot_owner_id"],
            sorts=[{"propertyName": date_prop, "direction": "DESCENDING"}],
        )
        disp = "all" if status == "any" else status
        if not deals:
            return f"--- HubSpot CRM (live) ---\nNo {disp} deals found{(' ' + label) if label else ''}{scope}."
        names = c.owner_name_map()
        total = sum(_num(d["properties"], "amount") or 0 for d in deals)
        head = (
            f"--- HubSpot CRM (live) — {disp} deals"
            f"{(', ' + label) if label else ''}{scope} ---"
        )
        lines = [head, f"{len(deals)} deal(s), total {_fmt_money(total)}."]
        for i, d in enumerate(deals[:20], 1):
            p = d["properties"]
            own = names.get(str(p.get("hubspot_owner_id")), "—")
            amt = _num(p, "amount")
            lines.append(
                f"{i}. {p.get('dealname', 'Unnamed')} — "
                f"{_fmt_money(amt) if amt is not None else '—'} | "
                f"closed {(p.get('closedate') or '')[:10]} | {own}"
            )
        if len(deals) > 20:
            lines.append(f"…and {len(deals) - 20} more.")
        return "\n".join(lines)

    def _deals_needing_followup(
        self,
        c,
        spec,
        window,
        owner_id,
        scope,
        allowed_pipelines=None,
        hide_deferred_for: str | None = None,
    ) -> str:
        """Deals with no logged outreach in the last ``stale_days`` days.

        The canonical deal cold-scan; the general resolver's cold-scan path
        delegates here. Answers "which of these deals haven't I reached out to in N
        days". Uses HubSpot's ``notes_last_contacted`` (last logged contact with the
        client) — never ``notes_last_updated`` as the signal (a fresh internal note
        must not mask a stale client contact). A deal with NO plausible logged contact
        is treated as needing follow-up. Completeness is by design: it fetches the
        FULL status set and computes "cold" client-side (never-contacted deals — the
        worst offenders — sort to the very top), so nothing depends on an API sort.

        ``deal_status`` (default open) lets post-close check-ins work ("which WON
        clients haven't I contacted since closing"); ``allowed_pipelines`` drops
        client-pipeline deals; ``filters`` (amount_gte/dealstage) narrow the set.
        Defaults reproduce the original cold-scan byte-for-byte. Reflects only what's
        LOGGED in HubSpot.

        ``hide_deferred_for`` (the caller, on their own deals) leaves out the deals
        they deferred until the follow-up date and says how many
        (rubik_core/sales/deal_defer.py) — a quiet long-term deal is exactly what
        a defer is for.
        """
        try:
            stale_days = int(spec.get("stale_days") or 7)
        except (TypeError, ValueError):
            stale_days = 7
        s, e, label = window
        status = (spec.get("deal_status") or "open").lower()
        filt = _status_filters(status) + _owner_filter(owner_id)
        if allowed_pipelines:
            filt.append(
                {
                    "propertyName": "pipeline",
                    "operator": "IN",
                    "values": sorted(allowed_pipelines),
                }
            )
        modeled, _dropped = _modeled_filters(spec.get("filters"))
        filt += modeled
        if s is not None:
            filt += _date_range_filters("closedate", s, e)
        deals = c.search_all(
            "deals",
            filt,
            [
                "dealname",
                "amount",
                "closedate",
                "hubspot_owner_id",
                "notes_last_contacted",
            ],
            sorts=[{"propertyName": "closedate", "direction": "ASCENDING"}],
        )
        sw = status if status != "any" else "open"  # "any" reads oddly as a status word
        scoped = f" expected to close {label}" if label else ""
        if not deals:
            return f"--- HubSpot CRM (live) ---\nNo {sw} deals found{scoped}{scope}."
        from rubik_core.sales.rhythm_checks import (  # local: avoid an import cycle
            DEAL_ONLY,
            client_contacts,
            silence,
        )

        now = datetime.now(timezone.utc)
        stale: list[tuple[str, dict, datetime | None]] = []
        for d in deals:
            p = d["properties"]
            # notes_last_contacted ONLY (client outreach); guard epoch junk. A fresh
            # internal note (notes_last_updated) must never mask a stale contact.
            last_dt = _parse_hs_datetime(_plausible_date(p.get("notes_last_contacted")))
            days = (now - last_dt).days if last_dt else None
            if days is None or days > stale_days:
                did = str(d.get("id") or p.get("hs_object_id") or "")
                stale.append((did, d, last_dt))
        deferred_note = ""
        if hide_deferred_for and stale:
            from rubik_core.sales.deal_defer import deferred_deal_ids, hidden_note

            hidden = deferred_deal_ids(
                [did for did, _, _ in stale], reader=hide_deferred_for
            )
            if hidden:
                stale = [t for t in stale if t[0] not in hidden]
                deferred_note = "Note: " + hidden_note(len(hidden))
        # Contact logged on the client's company but not on the deal (an email to
        # someone at the client who isn't on the deal) is shown beside the deal's
        # own date — never instead of it — so "82 days" can't stand alone when the
        # client heard from us 13 days ago (rhythm_checks.client_contacts).
        clients: dict = {}
        clients_ok = True
        if stale:
            try:
                clients = client_contacts(c, [did for did, _, _ in stale])
            except HubSpotAPIError:
                logger.warning("[crm] cold-scan client company read failed")
                clients_ok = False
        flagged = []
        for did, d, last_dt in stale:
            # silence() flags at ">= quiet_days"; this scan has always meant
            # "more than stale_days".
            q = silence(
                last_dt,
                clients.get(did),
                now=now,
                quiet_days=stale_days + 1,
                phrase="logged contact",
            )
            flagged.append((q, did, d, last_dt))
        # Whole client quiet first, then deals whose client was contacted inside
        # the window; inside each, never-contacted at the very top, then the
        # most overdue.
        flagged.sort(
            key=lambda t: (
                t[0].state == DEAL_ONLY,
                t[0].days is not None,
                -(t[0].days or 0),
            )
        )
        head = (
            f"--- HubSpot CRM (live) — {sw} deals{scoped}{scope} with no logged "
            f"outreach in the last {stale_days} days ---"
        )
        if not flagged:
            return (
                f"{head}\nNone — every one of the {len(deals)} {sw} deal(s) has "
                f"logged contact within the last {stale_days} days."
                if not deferred_note
                else f"{head}\nNone besides the deals you deferred.\n{deferred_note}"
            )
        names = c.owner_name_map()
        lines = [
            head,
            f"{len(flagged)} of {len(deals)} {sw} deal(s) need follow-up "
            f"(last contact by HubSpot's `notes_last_contacted` on the deal, with the "
            f"client company's own date beside it when that is newer — an email "
            f"logged to someone at the client who isn't on the deal lands there; "
            f"deals whose client was contacted within the last {stale_days} days "
            f"are listed last; reflects only logged activity):",
        ]
        for i, (_q, did, d, last_dt) in enumerate(flagged[:20], 1):
            p = d["properties"]
            own = names.get(str(p.get("hubspot_owner_id")), "—")
            amt = _num(p, "amount")
            last = _plausible_date(p.get("notes_last_contacted"))
            days = (now - last_dt).days if last_dt else None
            client = clients.get(did)
            if (
                client is not None
                and client.last_contact is not None
                and (last_dt is None or client.last_contact > last_dt)
            ):
                cdays = max((now - client.last_contact).days, 0)
                when = (
                    "nothing logged on this deal"
                    if days is None
                    else f"last contact on this deal {str(last)[:10]} ({days}d ago)"
                ) + (
                    f"; {client.name} last contacted "
                    f"{client.last_contact.date().isoformat()} ({cdays}d ago)"
                )
            else:
                when = (
                    "no logged contact on record"
                    if days is None
                    else f"last contact {str(last)[:10]} ({days}d ago)"
                )
            lines.append(
                f"{i}. {p.get('dealname', 'Unnamed')} — "
                f"{_fmt_money(amt) if amt is not None else '—'} | "
                f"expected close {(p.get('closedate') or '')[:10]} | {when} | {own}"
            )
        if len(flagged) > 20:
            lines.append(f"…and {len(flagged) - 20} more.")
        if not clients_ok:
            lines.append(
                "Note: the client company records could not be read from HubSpot, "
                "so contact logged on the client but not on the deal is not shown."
            )
        if deferred_note:
            lines.append(deferred_note)
        return "\n".join(lines)

    # ── General outreach/last-touch resolver ─────────────────────────────────
    # ONE path answers "when did we last contact / how long since / who's gone cold
    # / what stage-amount-owner" for whichever entity a question names, so a new
    # phrasing reuses target_type/owner_scope/answer_kind instead of a new intent.

    def _resolve_owner_self(self, c, caller_email):
        """Match the signed-in user's email to a HubSpot owner → (owner_id, name).

        (None, reason) when there is no match — the caller is not a HubSpot owner
        (e.g. an exec who owns no deals) or their login email differs from HubSpot.
        Never falls back to account-wide for a personal ("my") question.
        """
        if not caller_email:
            return None, "no signed-in email available"
        want = caller_email.strip().lower()
        for oid, info in c.owner_directory().items():
            if (info.get("email") or "").lower() == want:
                return oid, info.get("name") or caller_email
        return None, f"{caller_email} is not a HubSpot deal owner"

    def _match_one(self, results, name_field, query_name, target_type):
        """Integrity-first entity match: exact name (case-insensitive) auto-picks even
        amid several fuzzy hits; else a lone hit auto-picks (single_fuzzy); else >1 →
        multi_match (ask, never guess); else not_found. For contacts, exact also
        matches the email, and multiple fuzzy hits stay multi_match (token-OR search
        makes wrong-person picks likely)."""
        qn = (query_name or "").strip().lower()

        def disp(r):
            p = r.get("properties", {}) or {}
            if target_type == "contact":
                nm = f"{p.get('firstname', '')} {p.get('lastname', '')}".strip()
                return nm or p.get("email") or "(unnamed)"
            return p.get(name_field) or "(unnamed)"

        exact = []
        for r in results:
            p = r.get("properties", {}) or {}
            names = [str(disp(r)).lower()]
            if target_type == "contact":
                names.append(str(p.get("email") or "").lower())
            if qn and qn in [n for n in names if n]:
                exact.append(r)
        if len(exact) == 1:
            r = exact[0]
            return ResolvedTarget(
                target_type,
                False,
                _RESOLVE_OK,
                obj=r,
                obj_id=str(r.get("id")),
                display_name=str(disp(r)),
                match_basis="exact",
            )
        if len(results) == 1 and not exact:
            r = results[0]
            return ResolvedTarget(
                target_type,
                False,
                _RESOLVE_OK,
                obj=r,
                obj_id=str(r.get("id")),
                display_name=str(disp(r)),
                match_basis="single_fuzzy",
            )
        if len(results) > 1 or len(exact) > 1:
            cands = exact if len(exact) > 1 else results
            return ResolvedTarget(
                target_type,
                False,
                _RESOLVE_MULTI,
                match_basis="first_of_many",
                candidates=[
                    {"id": str(r.get("id")), "name": str(disp(r))} for r in cands[:3]
                ],
            )
        return ResolvedTarget(
            target_type, False, _RESOLVE_NOT_FOUND, match_basis="none"
        )

    def _resolve_target(
        self, c, spec, owner_id, owner_scope, owner_failed, allowed_pipelines
    ):
        """Turn the planner spec into a concrete single object OR an owner-scoped set."""
        target_type = (spec.get("target_type") or "").lower()
        target_name = (spec.get("target_name") or "").strip()
        is_set = bool(owner_scope) or target_type == "rep" or not target_name

        if is_set:
            set_obj = {
                "deal": "deals",
                "contact": "contacts",
                "company": "companies",
            }.get(target_type, "deals")
            tt = target_type or "rep"
            if owner_scope in ("me", "named") and owner_failed:
                return ResolvedTarget(
                    tt,
                    True,
                    _RESOLVE_NO_OWNER,
                    set_object_type=set_obj,
                    match_basis="none",
                )
            # Owner is often unset on companies/contacts (it lives on the deal), so an
            # owner-scoped company/contact sweep would be a false "0 cold".
            if set_obj in ("companies", "contacts") and owner_id:
                return ResolvedTarget(
                    tt,
                    True,
                    _RESOLVE_UNAVAILABLE,
                    owner_id=owner_id,
                    set_object_type=set_obj,
                    note=(
                        "I can't reliably scope companies/contacts to one owner here — "
                        "HubSpot's owner lives on the deal, not the account. Ask me "
                        "which of your DEALS have gone cold, or name a client."
                    ),
                )
            return ResolvedTarget(
                tt, True, _RESOLVE_OK, owner_id=owner_id, set_object_type=set_obj
            )

        # SINGLE named entity.
        if target_type == "deal":
            res = c.search_objects(
                "deals",
                target_name,
                [
                    "dealname",
                    "amount",
                    "closedate",
                    "dealstage",
                    "pipeline",
                    "hubspot_owner_id",
                    "notes_last_contacted",
                ],
                limit=5,
            )
            if allowed_pipelines:
                res = [
                    r
                    for r in res
                    if (r.get("properties", {}) or {}).get("pipeline")
                    in allowed_pipelines
                ]
            return self._match_one(res, "dealname", target_name, "deal")
        if target_type == "contact":
            res = c.search_owned_objects(
                "contacts",
                target_name,
                CONTACT_SEARCH_PROPERTIES
                + ["notes_last_contacted", "hubspot_owner_id"],
                limit=5,
            )
            return self._match_one(res, "lastname", target_name, "contact")
        # company (explicit, or a bare name with no clear type → default to company)
        res = c.search_owned_objects(
            "companies",
            target_name,
            [
                "name",
                "domain",
                "lifecyclestage",
                "notes_last_contacted",
                "notes_last_updated",
                "hs_last_sales_activity_date",
                "hubspot_owner_id",
            ],
            limit=5,
        )
        return self._match_one(res, "name", target_name, "company")

    def _outreach_recency(
        self,
        c,
        spec,
        window,
        owner_id,
        owner_scope,
        owner_failed,
        scope,
        allowed_pipelines,
        outcome,
        hide_deferred_for: str | None = None,
    ):
        """Dispatch a recency/attribute question to the resolved target.

        ``hide_deferred_for`` (the caller, on a "my deals" question) drops the
        deals they deferred from a cold-scan list (rubik_core/sales/deal_defer.py).
        """
        target = self._resolve_target(
            c, spec, owner_id, owner_scope, owner_failed, allowed_pipelines
        )
        outcome["match_basis"] = target.match_basis or outcome.get("match_basis")
        h = "--- HubSpot CRM (live) ---\n"
        name = spec.get("target_name") or ""

        if target.status == _RESOLVE_NOT_FOUND:
            outcome.update(resolution="not_found", gap=True)
            return (
                f'{h}No HubSpot {target.target_type or "record"} matched "{name}". '
                f"Try the exact name, or ask me to find a contact there."
            )
        if target.status == _RESOLVE_MULTI:
            outcome.update(
                resolution="multi_match", gap=True, match_count=len(target.candidates)
            )
            lines = [
                f'{h}Several {target.target_type} records match "{name}" — which do '
                f"you mean?"
            ]
            lines += [f"- {cand.get('name')}" for cand in target.candidates]
            return "\n".join(lines)
        if target.status == _RESOLVE_NO_OWNER:
            outcome.update(resolution="no_owner_match", gap=True)
            return (
                f"{h}I couldn't scope this to your own records (no HubSpot owner "
                f"matched you). Ask about a named rep, or the team as a whole."
            )
        if target.status in (_RESOLVE_UNAVAILABLE, _RESOLVE_DEFERRED):
            outcome.update(
                resolution=(
                    "unavailable"
                    if target.status == _RESOLVE_UNAVAILABLE
                    else "deferred"
                ),
                gap=True,
            )
            return f"{h}{target.note}"

        # status == ok
        answer_kind = (spec.get("answer_kind") or "recency").lower()
        if not target.is_set and answer_kind != "recency":
            outcome.update(resolution="answered", gap=False)
            return self._attribute_answer(c, target, answer_kind)
        if target.is_set:
            return self._recency_cold_scan(
                c,
                spec,
                window,
                target,
                scope,
                allowed_pipelines,
                outcome,
                hide_deferred_for=hide_deferred_for,
            )
        block = self._recency_point(c, target, allowed_pipelines, outcome)
        # A point answer picked on a non-exact match is watched separately (a confident
        # wrong-entity answer is otherwise invisible).
        if outcome.get("resolution") == "answered" and target.match_basis != "exact":
            outcome["resolution_flag"] = "low_confidence_autopick"
        return block

    def _recency_point(self, c, target, allowed_pipelines, outcome):
        """Last logged contact with ONE resolved entity (company | deal | contact)."""
        p = target.obj.get("properties", {}) or {}
        name = target.display_name or "this record"
        src = ""
        if target.target_type == "company":
            # MAX(company rollup, deal-level max) — a lagging-but-present company
            # rollup must not mask a fresher deal contact.
            rollup_p = _plausible_date(p.get("notes_last_contacted"))
            deal_p = _plausible_date(
                self._company_deal_last_contacted(c, target.obj_id, allowed_pipelines)
            )
            last = _max_plausible(rollup_p, deal_p)
            if last and last == deal_p and last != rollup_p:
                src = " (from an associated deal)"
        else:
            last = _plausible_date(p.get("notes_last_contacted"))

        header = f"--- HubSpot CRM (live) — last touch: {name} ---\n"
        if not last:
            outcome.update(resolution="no_data", gap=False)
            return (
                f"{header}No logged contact is recorded on {name} in HubSpot. "
                f"(Outreach that wasn't logged won't appear — this reflects only "
                f"what's recorded in the CRM.)"
            )
        d = _days_since(last)
        ago = f" ({d} days ago)" if d is not None else ""
        outcome.update(resolution="answered", gap=False)
        line = f"Last logged contact with {name}: **{str(last)[:10]}**{ago}{src}."
        if target.target_type == "company":
            stage = (p.get("lifecyclestage") or "").strip()
            if stage:
                line += f"\nLifecycle stage: {stage}."
        return header + line

    def _recency_cold_scan(
        self,
        c,
        spec,
        window,
        target,
        scope,
        allowed_pipelines,
        outcome,
        hide_deferred_for: str | None = None,
    ):
        """A rep/team's objects with no recent outreach. Deal sets delegate to the
        canonical cold-scan (fetch-all + client-side compute, so never-contacted
        deals are never dropped)."""
        _modeled, dropped = _modeled_filters(spec.get("filters"))
        caveat = ""
        if dropped:
            outcome.update(resolution="partial_filter", gap=True)
            caveat = (
                f"Note: I couldn't filter on {', '.join(dropped)} (no live filter "
                f"for it); showing the broader set.\n"
            )
        else:
            outcome.update(resolution="answered", gap=False)

        if target.set_object_type == "deals":
            return caveat + self._deals_needing_followup(
                c,
                spec,
                window,
                target.owner_id,
                scope,
                allowed_pipelines,
                hide_deferred_for=hide_deferred_for,
            )

        # Team-scope company/contact sweep (owner-scoped is unavailable upstream).
        # HubSpot cannot filter these object types by deal pipeline; prove a
        # live workspace deal association for each row before using its fields.
        obj = target.set_object_type
        try:
            stale_days = int(spec.get("stale_days") or 7)
        except (TypeError, ValueError):
            stale_days = 7
        rows = c.search_all_owned(
            obj,
            [{"propertyName": "notes_last_contacted", "operator": "HAS_PROPERTY"}],
            (["name"] if obj == "companies" else ["firstname", "lastname"])
            + ["notes_last_contacted"],
        )
        scan_tz = _tz()
        flagged = []
        for r in rows:
            pr = r.get("properties", {}) or {}
            last = _plausible_date(pr.get("notes_last_contacted"))
            days = _days_since(last, tz=scan_tz)
            if days is None or days > stale_days:
                flagged.append((days, pr))
        flagged.sort(key=lambda t: (t[0] is not None, t[0] if t[0] is not None else 0))
        noun = "companies" if obj == "companies" else "contacts"
        head = (
            f"--- HubSpot CRM (live) — {noun} with no logged outreach in the last "
            f"{stale_days} days{scope} ---"
        )
        if not flagged:
            return caveat + f"{head}\nNone found."
        lines = [
            head,
            f"{len(flagged)} {noun} with a workspace deal are cold:",
        ]
        for i, (days, pr) in enumerate(flagged[:20], 1):
            nm = (
                pr.get("name")
                if obj == "companies"
                else f"{pr.get('firstname', '')} {pr.get('lastname', '')}".strip()
            ) or "(unnamed)"
            when = "no logged contact" if days is None else f"{days}d ago"
            lines.append(f"{i}. {nm} — last contact {when}")
        if len(flagged) > 20:
            lines.append(f"…and {len(flagged) - 20} more.")
        return caveat + "\n".join(lines)

    def _attribute_answer(self, c, target, answer_kind):
        """A specific entity's stage / amount+close-date / owner / compact summary."""
        p = target.obj.get("properties", {}) or {}
        name = target.display_name or "this record"
        header = f"--- HubSpot CRM (live) — {name} ---\n"
        if answer_kind == "status":
            stage = p.get("dealstage")
            if not stage:
                return f"{header}{name} has no deal stage (is it a {target.target_type}, not a deal?)."
            label = c.deal_stage_labels().get(str(stage), str(stage))
            return f"{header}{name} is in stage **{label}**."
        if answer_kind == "owner":
            own = c.owner_name_map().get(str(p.get("hubspot_owner_id")), "—")
            return f"{header}{name} is owned by **{own}**."
        if answer_kind == "attribute":
            parts = []
            amt = _num(p, "amount")
            if amt is not None:
                parts.append(f"amount {_fmt_money(amt)}")
            if p.get("closedate"):
                parts.append(f"close date {(p.get('closedate') or '')[:10]}")
            return (
                f"{header}{name}: "
                + (", ".join(parts) if parts else "no amount or close date recorded")
                + "."
            )
        # summary
        bits = [f"owner {c.owner_name_map().get(str(p.get('hubspot_owner_id')), '—')}"]
        amt = _num(p, "amount")
        if amt is not None:
            bits.append(f"amount {_fmt_money(amt)}")
        if p.get("dealstage"):
            bits.append(
                f"stage {c.deal_stage_labels().get(str(p.get('dealstage')), p.get('dealstage'))}"
            )
        last = _plausible_date(p.get("notes_last_contacted"))
        if last:
            bits.append(f"last contact {str(last)[:10]}")
        return f"{header}{name}: " + "; ".join(bits) + "."

    def _company_deal_last_contacted(self, c, company_id, allowed_pipelines=None):
        """Most recent plausible ``notes_last_contacted`` across a company's deals
        (best-effort). Ported from the last_touch intent, extended to drop
        client-pipeline deals so a deal the company runs for a client can't
        supply the date."""
        configured = _workspace_deal_pipelines()
        allowed = (
            configured.intersection(allowed_pipelines)
            if allowed_pipelines is not None
            else configured
        )
        if not allowed:
            return None
        try:
            with httpx.Client(timeout=20.0) as client:
                assoc = client.get(
                    f"{c.base_url}/crm/v4/objects/companies/{company_id}/associations/deals",
                    headers=c._headers(),
                    params={"limit": 100},
                )
                assoc.raise_for_status()
                ids = [
                    str(r.get("toObjectId"))
                    for r in assoc.json().get("results", [])
                    if r.get("toObjectId")
                ]
                if not ids:
                    return None
                read = client.post(
                    f"{c.base_url}/crm/v3/objects/deals/batch/read",
                    headers=c._headers(),
                    json={
                        "properties": ["notes_last_contacted", "pipeline"],
                        "inputs": [{"id": i} for i in ids[:100]],
                    },
                )
                read.raise_for_status()
                dates = []
                for r in read.json().get("results", []):
                    props = r.get("properties", {}) or {}
                    if props.get("pipeline") not in allowed:
                        continue
                    d = _plausible_date(props.get("notes_last_contacted"))
                    if d:
                        dates.append(d)
                return _max_plausible(*dates) if dates else None
        except Exception as e:  # best-effort — never break the answer
            logger.warning("last-touch deal walk failed for %s: %s", company_id, e)
            return None

    def _log_crm_query(self, query, outcome, caller_email):
        """Emit exactly one structured ``[crm-query]`` line so unhandled/ambiguous
        phrasings surface as coverage gaps (WARNING) vs. answered queries (INFO)."""
        payload = {
            "intent": outcome.get("intent"),
            "answer_kind": outcome.get("answer_kind"),
            "target_type": outcome.get("target_type"),
            "target_name_present": bool(outcome.get("target_name_present")),
            "owner_scope": outcome.get("owner_scope"),
            "resolution": outcome.get("resolution"),
            "resolution_flag": outcome.get("resolution_flag"),
            "rhythm_check": outcome.get("rhythm_check"),
            "match_basis": outcome.get("match_basis") or None,
            "match_count": outcome.get("match_count"),
            "gap": bool(outcome.get("gap")),
            "latency_ms": outcome.get("latency_ms"),
            "caller": caller_email or None,
            "model": self.model_id,
        }
        if getattr(settings, "CRM_QUERY_LOG_INCLUDE_TEXT", True):
            payload["query"] = (query or "")[:500]
        line = json.dumps(payload, default=str)
        if payload["gap"]:
            logger.warning("[crm-query] %s", line)
        else:
            logger.info("[crm-query] %s", line)

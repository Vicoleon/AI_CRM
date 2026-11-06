"""HubSpot customer fetchers for UC-45.

Wraps ``HubSpotAPIClient`` to pull the full customer picture the X-Ray needs:
company profile (status/CSM/engagement props), deal history with products +
renewal fields, engagement activity (calls/emails/notes/tasks/meetings) and
contacts. Two things the base client does NOT do, added here:

1. **Backoff.** ``hubspot_api.py`` has no retry/429 handling and a full-history
   first run pages politely; ``_with_backoff`` wraps every call so a transient
   429/5xx retries with exponential sleep instead of surfacing immediately.
2. **The company's own properties, by configuration.** X-Ray asks HubSpot
   for its built-in deal and company properties (``hs_acv`` included) plus
   the properties an admin named for a role (renewal date, account status,
   CSM, products …) or as an extra (``rubik_core/xray/properties.py``). A role
   with no property, or a customer with no value, is simply absent and the
   churn scorer skips that factor.

Every fetch is fail-soft: an error appends to ``bundle.errors`` and leaves that
slice empty rather than aborting the run (spec: per-source failure = partial
brief, not failure). No DB connection is touched here — this is pure network
I/O the service layer runs with no session held (docs/ENGINEERING_NOTES.md connection rule).
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from rubik_core.logsafe import safe_error
from rubik_core.retrieval.hubspot_api import (
    _ACTIVITY_OBJECTS,
    HubSpotAPIClient,
    HubSpotAPIError,
)
from rubik_core.tenancy.hubspot_config import get_hubspot_config
from rubik_core.xray import config
from rubik_core.xray import properties as xray_properties
from rubik_core.xray.schemas import CompanyBundle, TimelineDraft
from rubik_core.xray.text import plain_text

logger = logging.getLogger(__name__)

# The deal and company properties X-Ray reads: HubSpot's built-ins plus the
# ones an admin named (rubik_core/xray/properties.py).
deal_properties = xray_properties.deal_properties
company_properties = xray_properties.company_properties

_CONTACT_PROPS = [
    "email",
    "firstname",
    "lastname",
    "jobtitle",
    "lastmodifieddate",
    "notes_last_contacted",
    "hs_last_sales_activity_timestamp",
]

# Timestamp property per activity object (HubSpot is inconsistent across them).
_ACTIVITY_TS_PROP = {
    "calls": "hs_timestamp",
    "emails": "hs_timestamp",
    "notes": "hs_timestamp",
    "tasks": "hs_timestamp",
    "meetings": "hs_meeting_start_time",
}
_ACTIVITY_TITLE_PROP = {
    "calls": "hs_call_title",
    "emails": "hs_email_subject",
    "notes": "hs_note_body",
    "tasks": "hs_task_subject",
    "meetings": "hs_meeting_title",
}


def _with_backoff(fn, *args, retries: int = 4, base_delay: float = 1.0, **kwargs):
    """Call ``fn`` retrying transient HubSpot failures with exponential backoff.

    The base client raises ``HubSpotAPIError`` for any HTTP error, including
    429 (rate limit) which it does NOT otherwise handle. We retry those; a
    persistent error re-raises so the caller can record a partial-error.
    """
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return fn(*args, **kwargs)
        except HubSpotAPIError as exc:
            last = exc
            msg = str(exc)
            transient = ("429" in msg) or (" 5" in msg) or ("transport" in msg.lower())
            if not transient or attempt == retries:
                raise
            time.sleep(base_delay * (2 ** (attempt - 1)))
    if last:
        raise last


def _friendly_error(label: str, exc: Exception) -> str:
    """Turn a raw HubSpot/httpx error into a concise, human Intelligence-Gap line.

    The verbose ``Client error '403 Forbidden' for url … mozilla.org`` text is
    noise for a sales user; a 403 almost always means the private-app token is
    missing a read scope for that object (e.g. emails).
    """
    msg = str(exc)
    if "403" in msg:
        return (
            f"{label} unavailable — the HubSpot token is missing the read scope "
            f"for {label} (add it to the private app to include this)."
        )
    if "429" in msg:
        return f"{label} unavailable — HubSpot rate-limited the request; try again shortly."
    if "404" in msg:
        return f"{label} unavailable — not found in HubSpot."
    return f"{label} unavailable — HubSpot request failed."


# ── HTTP primitives the base client doesn't expose ───────────────────────────
# HubSpot hard limits, not tunables: v4 associations pages at 500 and v3
# batch/read accepts 100 inputs per call. Both used to be treated as "the
# answer" rather than "one page", which silently dropped records.
_ASSOC_PAGE = 500
_BATCH_READ_CHUNK = 100


def _associated_ids(
    client: HubSpotAPIClient,
    from_id: str,
    to_object: str,
    *,
    limit: int = 100,
    max_pages: int = 20,
) -> tuple[list[str], int]:
    """v4 associations: the ids of ``to_object`` linked to a company, plus the
    total number that exist.

    Pages through the whole association set (HubSpot returns at most 500 per
    page with a ``paging.next.after`` cursor) and returns the ids HubSpot issued
    most recently, newest first: object ids are allocated monotonically, so
    sorting them descending orders records by creation without having to read
    every one first. That makes ``limit`` mean "the most recent N" — what the
    caller and the config comment always assumed, where previously a single
    unordered page was taken and the excess simply vanished.

    ``total`` is what the caller compares against ``limit`` to tell the user the
    timeline is capped, instead of leaving the gap silent.
    """
    url = (
        f"{client.base_url}/crm/v4/objects/companies/{from_id}/associations/{to_object}"
    )

    def _page(after: str | None) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": _ASSOC_PAGE}
        if after:
            params["after"] = after
        with httpx.Client(timeout=20.0) as h:
            resp = h.get(url, headers=client._headers(), params=params)
            resp.raise_for_status()
            return resp.json()

    collected: list[str] = []
    after: str | None = None
    seen_cursors: set[str] = set()
    try:
        for _ in range(max_pages):
            data = _with_backoff(_page, after)
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise HubSpotAPIError("HubSpot association page is invalid")
            for row in data["results"]:
                if (
                    not isinstance(row, dict)
                    or not isinstance(row.get("toObjectId"), (str, int))
                    or not str(row["toObjectId"])
                ):
                    raise HubSpotAPIError("HubSpot association result is invalid")
                collected.append(str(row["toObjectId"]))
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
            # An incomplete ownership scan cannot prove that a company has no
            # foreign-pipeline deals farther back in its history.
            raise HubSpotAPIError("HubSpot association scan is incomplete")
    except (HubSpotAPIError, httpx.HTTPError) as exc:
        logger.warning(
            "assoc %s for company %s failed: %s", to_object, from_id, safe_error(exc)
        )
        raise HubSpotAPIError(
            f"HubSpot association failed: {safe_error(exc)}"
        ) from None

    unique = list(dict.fromkeys(collected))
    # Newest first by issued id; non-numeric ids (never seen in practice) sort last.
    unique.sort(key=lambda i: (i.isdigit(), int(i) if i.isdigit() else 0), reverse=True)
    return unique[:limit], len(unique)


def _raw_batch_read(
    client: HubSpotAPIClient, obj: str, ids: list[str], props: list[str]
) -> list[dict[str, Any]]:
    """v3 batch read of ``obj`` records with the given properties.

    Reads **every** id in chunks of 100. This used to send ``ids[:100]`` in a
    single call, so with ``MAX_DEALS``/``MAX_ENGAGEMENTS_PER_TYPE`` at 200 more
    than half of a busy customer's records were fetched, discarded, and never
    reported — the timeline just came up short with no error anywhere.
    """
    if not ids:
        return []
    url = f"{client.base_url}/crm/v3/objects/{obj}/batch/read"

    def _call(chunk: list[str]) -> list[dict[str, Any]]:
        with httpx.Client(timeout=30.0) as h:
            resp = h.post(
                url,
                headers=client._headers(),
                json={"properties": props, "inputs": [{"id": i} for i in chunk]},
            )
            resp.raise_for_status()
            return resp.json().get("results", [])

    out: list[dict[str, Any]] = []
    try:
        for start in range(0, len(ids), _BATCH_READ_CHUNK):
            out.extend(_with_backoff(_call, ids[start : start + _BATCH_READ_CHUNK]))
    except (HubSpotAPIError, httpx.HTTPError) as exc:
        logger.warning("batch read %s failed: %s", obj, safe_error(exc))
        raise HubSpotAPIError(f"HubSpot batch read failed: {safe_error(exc)}") from None
    return out


def _batch_read(
    client: HubSpotAPIClient, obj: str, ids: list[str], props: list[str]
) -> list[dict[str, Any]]:
    """Read a slice, checking deal pipeline after HubSpot replies."""
    if obj != "deals" or not ids:
        return _raw_batch_read(client, obj, ids, props)
    try:
        allowed = frozenset(get_hubspot_config().pipeline_ids)
    except Exception:  # noqa: BLE001 — control errors deny a deal read
        raise HubSpotAPIError("Workspace HubSpot configuration unavailable") from None
    if not allowed:
        raise HubSpotAPIError("Workspace deal pipelines are not configured")
    rows = _raw_batch_read(client, obj, ids, list(dict.fromkeys([*props, "pipeline"])))
    return [
        row for row in rows if (row.get("properties") or {}).get("pipeline") in allowed
    ]


def _authorized_deals(
    client: HubSpotAPIClient, company_id: str, allowed: frozenset[str]
) -> list[str]:
    """Prove a company belongs wholly to this workspace's HubSpot pipelines.

    Company contacts and engagements are shared at company level, so a mixed
    company cannot safely expose those slices until there is a finer ownership
    model. Reading only the deal pipeline field here never publishes foreign
    deal data to the caller.
    """
    if not allowed:
        raise HubSpotAPIError("Workspace deal pipelines are not configured")
    if not re.fullmatch(r"[0-9]{1,20}", company_id):
        raise HubSpotAPIError("Company is not available in this workspace")
    deal_ids, total = _associated_ids(client, company_id, "deals", limit=10_000)
    if not deal_ids or total != len(deal_ids):
        raise HubSpotAPIError("Company is not available in this workspace")
    rows = _raw_batch_read(client, "deals", deal_ids, ["pipeline"])
    by_id = {str(row.get("id")): row for row in rows}
    if set(by_id) != set(deal_ids) or any(
        (row.get("properties") or {}).get("pipeline") not in allowed
        for row in by_id.values()
    ):
        raise HubSpotAPIError("Company is not available in this workspace")
    return deal_ids


_TRUNCATION_LABEL = {
    "deals": "deals",
    "contacts": "contacts",
    "calls": "logged calls",
    "emails": "logged emails",
    "notes": "notes",
    "tasks": "tasks",
    "meetings": "meetings",
}


def _note_truncation(bundle: CompanyBundle, obj: str, total: int, cap: int) -> None:
    """Record an Intelligence Gap when a fetch cap actually bound.

    A capped fetch used to be invisible: the timeline simply came up short and
    nothing said so, which reads as "that record isn't in HubSpot" when in fact
    it is. Saying which slice was cut, and how deep the coverage goes, is the
    difference between a known limit and a phantom data gap.
    """
    if total <= cap:
        return
    label = _TRUNCATION_LABEL.get(obj, obj)
    bundle.errors.append(
        f"Showing the {cap} most recent {label} of {total} in HubSpot — older "
        f"{label} are not included in this timeline."
    )


# ── Public API ───────────────────────────────────────────────────────────────
def visible_company_ids(company_ids: list[str]) -> set[str]:
    """Prove stored X-Ray company IDs are still owned under live CRM config."""
    candidates = list(dict.fromkeys(str(value) for value in company_ids if value))
    if not candidates:
        return set()
    try:
        allowed = frozenset(get_hubspot_config().pipeline_ids)
        if not allowed:
            return set()
        client = HubSpotAPIClient()
    except Exception:  # noqa: BLE001 — a missing token/config denies all snapshots
        logger.exception("X-Ray ownership prerequisite unavailable")
        return set()
    visible: set[str] = set()
    for company_id in candidates:
        try:
            _authorized_deals(client, company_id, allowed)
            visible.add(company_id)
        except Exception:  # noqa: BLE001 — one broken proof never grants data
            logger.info("X-Ray company ownership not proven for %s", company_id)
    return visible


def company_available(company_id: str) -> bool:
    return str(company_id) in visible_company_ids([company_id])


def search_companies(query: str, *, limit: int = 10) -> list[dict[str, Any]]:
    """Live HubSpot company search for the ``/api/xray/customers?q=`` typeahead.

    Returns lightweight rows: ``{company_id, name, domain, client_status, csm}``.
    Fail-soft to ``[]`` (surface a message upstream, never 500 the typeahead).
    """
    query = (query or "").strip()
    if not query:
        return []
    try:
        allowed = frozenset(get_hubspot_config().pipeline_ids)
    except Exception:  # noqa: BLE001 — unavailable control config denies search
        logger.exception("Workspace HubSpot config unavailable for company search")
        return []
    if not allowed:
        return []
    try:
        client = HubSpotAPIClient()
    except HubSpotAPIError as exc:
        logger.info("HubSpot not configured for company search: %s", exc)
        return []

    try:
        rows = _with_backoff(
            client.search_objects,
            "companies",
            query,
            # Name and domain, plus the status and CSM fields an admin named.
            xray_properties.search_properties(),
            limit=limit,
        )
    except HubSpotAPIError as exc:
        logger.warning("company search failed: %s", exc)
        return []

    visible: list[dict[str, Any]] = []
    for row in rows:
        company_id = str(row.get("id") or "")
        if not company_id:
            continue
        try:
            _authorized_deals(client, company_id, allowed)
        except HubSpotAPIError:
            # The typeahead never distinguishes a foreign company from an
            # unavailable or unowned one by returning its identity.
            continue
        visible.append(row)
    # The owner directory is only needed after at least one company is proven
    # in-scope, and only when a CSM property is configured.
    owner_names = (
        _safe_owner_map(client)
        if visible and xray_properties.csm_owner_property()
        else {}
    )
    out: list[dict[str, Any]] = []
    for r in visible:
        props = r.get("properties", {}) or {}
        out.append(
            {
                "company_id": str(r.get("id")),
                "name": props.get("name") or "",
                "domain": props.get("domain") or "",
                "client_status": xray_properties.account_status(props),
                "csm": owner_names.get(xray_properties.csm_owner_id(props), ""),
            }
        )
    return out


def _safe_owner_map(client: HubSpotAPIClient) -> dict[str, str]:
    try:
        return _with_backoff(client.owner_name_map)
    except (HubSpotAPIError, AttributeError) as exc:
        logger.info("owner directory unavailable: %s", exc)
        return {}


def fetch_company_bundle(
    company_id: str,
    *,
    cursors: dict[str, Any] | None = None,
) -> CompanyBundle:
    """Pull everything about a customer. Delta-aware via ``cursors``.

    ``cursors`` carries per-source watermarks from the last snapshot (max
    engagement timestamp seen, etc.). Churn is always recomputed on the full
    current state (cheap); only the timeline evidence is filtered to unseen
    items downstream — so this returns the current full picture and stamps a
    fresh ``covered_through`` + updated cursors for the next refresh.
    """
    cursors = dict(cursors or {})
    bundle = CompanyBundle(company_id=str(company_id), cursors=cursors)
    bundle.covered_through = datetime.now(timezone.utc).replace(tzinfo=None)

    try:
        allowed = frozenset(get_hubspot_config().pipeline_ids)
    except Exception:  # noqa: BLE001 — no company data without trusted config
        logger.exception("Workspace HubSpot config unavailable for X-Ray")
        bundle.errors.append("Workspace HubSpot configuration is unavailable.")
        return bundle
    if not allowed:
        bundle.errors.append("Workspace deal pipelines are not configured.")
        return bundle
    try:
        client = HubSpotAPIClient()
    except HubSpotAPIError as exc:
        bundle.errors.append(f"HubSpot not configured: {exc}")
        return bundle

    # Authorize before reading company fields, contacts or engagements. A
    # company shared with foreign-pipeline deals is not safe at company level.
    try:
        deal_ids = _authorized_deals(client, str(company_id), allowed)
        selected_ids = deal_ids[: config.MAX_DEALS]
        deal_recs = _batch_read(client, "deals", selected_ids, deal_properties())
        if {str(row.get("id")) for row in deal_recs} != set(selected_ids):
            raise HubSpotAPIError("Company deal ownership changed during read")
    except HubSpotAPIError:
        bundle.errors.append("Company is not available in this workspace.")
        return bundle

    bundle.deals = [
        {"id": row.get("id"), **(row.get("properties", {}) or {})} for row in deal_recs
    ]
    _note_truncation(bundle, "deals", len(deal_ids), config.MAX_DEALS)

    # Company profile
    try:
        recs = _batch_read(client, "companies", [company_id], company_properties())
        props = (recs[0].get("properties", {}) if recs else {}) or {}
        bundle.company = props
        bundle.company_name = props.get("name") or ""
        bundle.company_domain = props.get("domain") or ""
        bundle.client_status = xray_properties.account_status(props)
        csm_id = xray_properties.csm_owner_id(props)
        bundle.csm = _safe_owner_map(client).get(csm_id, "") if csm_id else ""
    except HubSpotAPIError as exc:
        bundle.errors.append(_friendly_error("company profile", exc))

    # Engagements per activity object
    eng_cursor: dict[str, Any] = dict(cursors.get("engagements", {}) or {})
    new_eng_cursor: dict[str, Any] = {}
    for obj in _ACTIVITY_OBJECTS:
        ts_prop = _ACTIVITY_TS_PROP.get(obj, "hs_timestamp")
        title_prop = _ACTIVITY_TITLE_PROP.get(obj, "")
        props_wanted = [ts_prop, "hs_lastmodifieddate"]
        if title_prop:
            props_wanted.append(title_prop)
        try:
            ids, total = _associated_ids(
                client, company_id, obj, limit=config.MAX_ENGAGEMENTS_PER_TYPE
            )
            owned = client.fully_owned_deal_associations(obj, ids) if ids else {}
            safe_ids = [item_id for item_id in ids if item_id in owned]
            recs = _batch_read(client, obj, safe_ids, props_wanted)
            items = [
                {"id": r.get("id"), **(r.get("properties", {}) or {})} for r in recs
            ]
            # Id order approximates creation order; re-sort on the real activity
            # timestamp so "most recent first" holds for back-dated entries too.
            items.sort(key=lambda i: _parse_ts_ms(i.get(ts_prop)), reverse=True)
            bundle.engagements[obj] = items
            if len(safe_ids) != len(ids) or total > config.MAX_ENGAGEMENTS_PER_TYPE:
                bundle.errors.append(
                    f"Some {obj} could not be checked in this workspace."
                )
            # advance cursor to the newest ts we saw for this object
            newest = max(
                (_parse_ts_ms(i.get(ts_prop)) for i in items if i.get(ts_prop)),
                default=eng_cursor.get(obj, 0),
            )
            new_eng_cursor[obj] = newest
        except HubSpotAPIError as exc:
            bundle.errors.append(_friendly_error(obj, exc))
            bundle.engagements[obj] = []
            new_eng_cursor[obj] = eng_cursor.get(obj, 0)

    # Contacts
    try:
        contact_ids, contact_total = _associated_ids(
            client, company_id, "contacts", limit=config.MAX_CONTACTS
        )
        owned = (
            client.fully_owned_deal_associations("contacts", contact_ids)
            if contact_ids
            else {}
        )
        safe_ids = [item_id for item_id in contact_ids if item_id in owned]
        recs = _batch_read(client, "contacts", safe_ids, _CONTACT_PROPS)
        bundle.contacts = [
            {"id": r.get("id"), **(r.get("properties", {}) or {})} for r in recs
        ]
        if len(safe_ids) != len(contact_ids) or contact_total > config.MAX_CONTACTS:
            bundle.errors.append(
                "Some contacts could not be checked in this workspace."
            )
    except HubSpotAPIError as exc:
        bundle.errors.append(_friendly_error("contacts", exc))

    bundle.cursors = {**cursors, "engagements": new_eng_cursor}

    # HubSpot has no snapshot transaction. Recheck before publishing or
    # persisting the bundle so a deal/pipeline reassignment during the longer
    # engagement fetch cannot turn a formerly owned company into a leak.
    try:
        if frozenset(get_hubspot_config().pipeline_ids) != allowed or set(
            _authorized_deals(client, str(company_id), allowed)
        ) != set(deal_ids):
            raise HubSpotAPIError("Company ownership changed during read")
    except Exception:  # noqa: BLE001 — no partial bundle escapes on auth drift
        logger.warning("X-Ray company ownership changed or became unavailable")
        return CompanyBundle(
            company_id=str(company_id),
            errors=["Company is not available in this workspace."],
        )

    # Lazy-warm the relationship graph with the associations we just fetched:
    # this run already paid for them, so persisting costs no extra HubSpot call.
    # Fail-soft inside the writer — an X-Ray brief never depends on it.
    from rubik_core.graph.writers import record_company_bundle

    record_company_bundle(
        bundle.company_id,
        bundle.company_name,
        bundle.company_domain,
        bundle.deals,
        bundle.contacts,
    )
    return bundle


# ── Timeline construction ────────────────────────────────────────────────────
def build_timeline_events(bundle: CompanyBundle) -> list[TimelineDraft]:
    """Turn a fetched bundle into deduplicable timeline drafts.

    Deterministic ``event_key``s so a refresh re-inserts nothing already seen:
      * ``deal:{id}:created`` / ``deal:{id}:closed``
      * ``activity:{obj}:{id}``
    """
    drafts: list[TimelineDraft] = []

    for d in bundle.deals:
        did = str(d.get("id"))
        name = d.get("dealname") or f"Deal {did}"
        created = _parse_ts(d.get("createdate"))
        if created:
            drafts.append(
                TimelineDraft(
                    event_key=f"deal:{did}:created",
                    source="hubspot_deal",
                    kind="deal_created",
                    title=f"Deal created — {name}",
                    ts=created,
                    detail=_deal_detail(d),
                    evidence=f"HubSpot deal {did}",
                )
            )
        # A closed-won/lost deal is a moment that matters.
        if str(d.get("hs_is_closed")) == "true":
            closed = _parse_ts(d.get("closedate"))
            won = str(d.get("hs_is_closed_won")) == "true"
            drafts.append(
                TimelineDraft(
                    event_key=f"deal:{did}:closed",
                    source="hubspot_deal",
                    kind="deal_closed_won" if won else "deal_closed_lost",
                    title=f"Deal {'won' if won else 'closed'} — {name}",
                    ts=closed,
                    detail=_deal_detail(d),
                    evidence=f"HubSpot deal {did}",
                )
            )

    for obj, items in bundle.engagements.items():
        ts_prop = _ACTIVITY_TS_PROP.get(obj, "hs_timestamp")
        title_prop = _ACTIVITY_TITLE_PROP.get(obj, "")
        for it in items:
            iid = str(it.get("id"))
            ts = _parse_ts(it.get(ts_prop))
            # Note bodies are HTML in HubSpot; everything else is already plain.
            label = plain_text(it.get(title_prop), max_len=120) if title_prop else ""
            title = f"{obj.rstrip('s').capitalize()}"
            if label:
                title += f" — {label}"
            drafts.append(
                TimelineDraft(
                    event_key=f"activity:{obj}:{iid}",
                    source="hubspot_activity",
                    kind=obj,
                    title=title,
                    ts=ts,
                    evidence=f"HubSpot {obj} {iid}",
                )
            )

    return drafts


# ── Timestamp helpers (HubSpot uses epoch-ms strings and ISO) ────────────────
def _parse_ts_ms(value: Any) -> int:
    dt = _parse_ts(value)
    if dt is None:
        return 0
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _parse_ts(value: Any) -> datetime | None:
    """Parse a HubSpot timestamp (epoch-ms string/int or ISO 8601) → naive UTC."""
    if value in (None, ""):
        return None
    # epoch millis / seconds
    try:
        ms = int(value)
        if ms > 10_000_000_000:  # 13-digit ms
            return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).replace(
                tzinfo=None
            )
        return datetime.fromtimestamp(ms, tz=timezone.utc).replace(tzinfo=None)
    except (ValueError, TypeError):
        pass
    try:
        s = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except ValueError:
        return None


def _deal_detail(d: dict[str, Any]) -> str:
    bits = []
    products = xray_properties.products(d)
    if products:
        bits.append(f"Products: {products}")
    if d.get("amount"):
        bits.append(f"Amount: {d['amount']}")
    if d.get("dealstage"):
        bits.append(f"Stage: {d['dealstage']}")
    return " · ".join(bits)

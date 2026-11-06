"""Read-side HubSpot lookups for the CRM Sync context bundle.

Everything here is read-only and best-effort per object: a company we can't
resolve is one fewer record in the prompt, not a failed scan. Each call is
wrapped in backoff because the base client has no 429 handling and a nightly
batch walks these lookups once per meeting.
"""

from __future__ import annotations

import logging

from rubik_core.retrieval.hubspot_write import _with_backoff
from rubik_core.tenancy.hubspot_config import get_hubspot_config

logger = logging.getLogger(__name__)

_DEAL_PROPERTIES = [
    "dealname",
    "pipeline",
    "dealstage",
    "amount",
    "closedate",
    "hubspot_owner_id",
]

_MAX_DEALS_PER_COMPANY = 25


def fetch_contacts_for_emails(
    client, emails: list[str], *, properties: list[str]
) -> list[dict]:
    """One entry per attendee: their contact record, or a NOT-IN-CRM marker.

    The absent ones matter as much as the present ones — "this person isn't in
    HubSpot" is exactly the gap the feature exists to close, and the model can
    only propose creating someone it was told is missing.
    """
    out: list[dict] = []
    if not get_hubspot_config().pipeline_ids:
        return out
    for email in emails:
        addr = (email or "").strip().lower()
        if not addr:
            continue
        try:
            results = _with_backoff(
                client.search_all,
                "contacts",
                [{"propertyName": "email", "operator": "EQ", "value": addr}],
                properties,
            )
        except Exception:  # noqa: BLE001
            logger.warning("[crm-sync] contact lookup failed for %s", addr)
            continue
        if not results:
            out.append({"email": addr, "hubspot_id": None, "properties": {}})
            continue
        candidate_ids = [str(item.get("id") or "") for item in results]
        try:
            owned = client.fully_owned_deal_associations("contacts", candidate_ids)
        except Exception:  # noqa: BLE001 — never show an unproven contact to the model
            logger.warning("[crm-sync] contact ownership check failed for %s", addr)
            continue
        record = next(
            (item for item in results if str(item.get("id") or "") in owned), None
        )
        if record is None:
            # A foreign or mixed-pipeline contact exists in the portal. Do not
            # call it absent: that could invite a duplicate create suggestion.
            continue
        out.append(
            {
                "email": addr,
                "hubspot_id": str(record.get("id")),
                "properties": {
                    k: v for k, v in (record.get("properties") or {}).items() if v
                },
            }
        )
    return out


def fetch_companies_for_domains(client, domains: list[str]) -> list[dict]:
    out: list[dict] = []
    if not get_hubspot_config().pipeline_ids:
        return out
    for domain in domains:
        try:
            results = _with_backoff(
                client.search_all,
                "companies",
                [{"propertyName": "domain", "operator": "EQ", "value": domain}],
                ["name", "domain"],
            )
        except Exception:  # noqa: BLE001
            logger.warning("[crm-sync] company lookup failed for %s", domain)
            continue
        if not results:
            continue
        candidate_ids = [str(item.get("id") or "") for item in results]
        try:
            owned = client.fully_owned_deal_associations("companies", candidate_ids)
        except Exception:  # noqa: BLE001 — no unproven company in the prompt
            logger.warning("[crm-sync] company ownership check failed for %s", domain)
            continue
        record = next(
            (item for item in results if str(item.get("id") or "") in owned), None
        )
        if record is None:
            continue
        props = record.get("properties") or {}
        out.append(
            {
                "hubspot_id": str(record.get("id")),
                "name": props.get("name"),
                "domain": props.get("domain") or domain,
            }
        )
    return out


def fetch_deals_for_companies(
    client,
    company_ids: list[str],
    *,
    ae_owner_id: str | None,
    stage_labels: dict,
    owner_names: dict,
) -> list[dict]:
    """Deals associated with these companies, restricted to the company's own pipelines.

    Deals the AE doesn't own are still included: they ground the model in what
    the account already looks like. They are labelled read-only in the prompt
    and rejected at validation if proposed for update, so including them adds
    context without widening what can actually be changed.
    """
    allowed = set(get_hubspot_config().pipeline_ids)
    out: list[dict] = []
    seen: set[str] = set()
    if not allowed:
        return out

    for company_id in company_ids:
        # Searched by association rather than walked via the v4 association
        # endpoint: one call instead of two, the pipeline filter applies
        # server-side, and unlike the shared ``_deals_for_company`` helper this
        # can ask for hubspot_owner_id — which the ownership check needs.
        filters = [
            {
                "propertyName": "associations.company",
                "operator": "EQ",
                "value": str(company_id),
            }
        ]
        filters.append(
            {
                "propertyName": "pipeline",
                "operator": "IN",
                "values": sorted(allowed),
            }
        )
        try:
            deals = _with_backoff(client.search_all, "deals", filters, _DEAL_PROPERTIES)
        except Exception:  # noqa: BLE001
            logger.warning("[crm-sync] deal lookup failed for company %s", company_id)
            continue

        for deal in (deals or [])[:_MAX_DEALS_PER_COMPANY]:
            deal_id = str(deal.get("id"))
            if deal_id in seen:
                continue
            props = deal.get("properties") or {}
            pipeline = str(props.get("pipeline") or "")
            # Belt and braces: never let a returned client pipeline reach the prompt.
            if pipeline not in allowed:
                continue
            seen.add(deal_id)
            owner_id = str(props.get("hubspot_owner_id") or "")
            stage = str(props.get("dealstage") or "")
            out.append(
                {
                    "hubspot_id": deal_id,
                    "company_id": str(company_id),
                    "dealname": props.get("dealname"),
                    "pipeline": pipeline,
                    "dealstage": stage,
                    "dealstage_label": stage_labels.get(stage, stage),
                    "amount": props.get("amount"),
                    "closedate": (props.get("closedate") or "")[:10] or None,
                    "owner_id": owner_id,
                    "owner_name": owner_names.get(owner_id) or "unassigned",
                    "owned_by_caller": bool(ae_owner_id)
                    and owner_id == str(ae_owner_id),
                }
            )
    return out

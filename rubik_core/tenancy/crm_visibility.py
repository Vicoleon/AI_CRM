"""Live workspace ownership proof for persisted CRM-derived content.

Stored prose can outlive a change to the workspace's allowed HubSpot pipelines.
Callers must compare each saved source set with this live result before reading
or reusing that prose. A failed or incomplete proof returns empty sets.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_RECORD_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


def visible_source_ids(
    company_ids: set[str], contact_ids: set[str], deal_ids: set[str]
) -> tuple[set[str], set[str], set[str]]:
    """Return currently owned companies, contacts and deals from three sets."""
    requested = (company_ids, contact_ids, deal_ids)
    if any(
        not isinstance(values, set)
        or any(
            not isinstance(value, str) or not _RECORD_ID.fullmatch(value)
            for value in values
        )
        for values in requested
    ):
        return set(), set(), set()
    if not any(requested):
        return set(), set(), set()
    try:
        from rubik_core.retrieval.hubspot_api import HubSpotAPIClient
        from rubik_core.tenancy.hubspot_config import get_hubspot_config

        allowed = set(get_hubspot_config().pipeline_ids)
        if not allowed:
            return set(), set(), set()
        client = HubSpotAPIClient()
        companies = (
            set(client.fully_owned_deal_associations("companies", sorted(company_ids)))
            if company_ids
            else set()
        )
        contacts = (
            set(client.fully_owned_deal_associations("contacts", sorted(contact_ids)))
            if contact_ids
            else set()
        )
        rows = (
            client.batch_read("deals", sorted(deal_ids), ["pipeline"])
            if deal_ids
            else []
        )
        deals = {
            str(row.get("id"))
            for row in rows
            if isinstance(row, dict)
            and str(row.get("id")) in deal_ids
            and (row.get("properties") or {}).get("pipeline") in allowed
        }
        return companies & company_ids, contacts & contact_ids, deals
    except Exception:  # noqa: BLE001 - no source proof means no stored prose
        logger.warning("Stored CRM source ownership could not be verified")
        return set(), set(), set()

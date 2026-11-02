"""Live CRM source proof for stored Call Capture sessions."""

from __future__ import annotations

import re
from typing import Any

from rubik_core.tenancy.context import current_workspace

_RECORD_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
ATTR_KEY = "crm_provenance"


def _id_set(value: Any) -> set[str] | None:
    if (
        not isinstance(value, list)
        or any(
            not isinstance(item, str) or not _RECORD_ID.fullmatch(item)
            for item in value
        )
        or value != sorted(set(value))
    ):
        return None
    return set(value)


def stamp_session_source(
    company_id: str | None, contact_id: str | None, deal_id: str | None
) -> dict[str, Any]:
    """Stamp the complete CRM binding when a server creates or updates a row."""
    return {
        "schema_version": 1,
        "workspace": current_workspace(),
        "company_ids": [company_id] if company_id else [],
        "contact_ids": [contact_id] if contact_id else [],
        "deal_ids": [deal_id] if deal_id else [],
        "unprovable": False,
    }


def _stamped_ids(session: Any) -> tuple[set[str], set[str], set[str]] | None:
    """Return exact saved scope, or None when its provenance is untrustworthy."""
    attrs = getattr(session, "attrs", None)
    marker = attrs.get(ATTR_KEY) if isinstance(attrs, dict) else None
    if (
        not isinstance(marker, dict)
        or type(marker.get("schema_version")) is not int
        or marker["schema_version"] != 1
        or marker.get("unprovable") is not False
    ):
        return None
    try:
        if marker.get("workspace") != current_workspace():
            return None
    except Exception:  # noqa: BLE001 - a missing tenant context never grants access
        return None
    companies = _id_set(marker.get("company_ids"))
    contacts = _id_set(marker.get("contact_ids"))
    deals = _id_set(marker.get("deal_ids"))
    if companies is None or contacts is None or deals is None:
        return None
    if companies != (
        {session.hubspot_company_id} if session.hubspot_company_id else set()
    ):
        return None
    if contacts != (
        {session.hubspot_contact_id} if session.hubspot_contact_id else set()
    ):
        return None
    if deals != ({session.hubspot_deal_id} if session.hubspot_deal_id else set()):
        return None
    if attrs.get("company_name") and not companies:
        return None
    return companies, contacts, deals


def visible_session_ids(sessions: list[Any]) -> set[str]:
    """Prove a bounded page of sessions with one live HubSpot batch."""
    stamped = {session.id: _stamped_ids(session) for session in sessions}
    company_ids = set().union(*(scope[0] for scope in stamped.values() if scope))
    contact_ids = set().union(*(scope[1] for scope in stamped.values() if scope))
    deal_ids = set().union(*(scope[2] for scope in stamped.values() if scope))
    if company_ids or contact_ids or deal_ids:
        from rubik_core.tenancy.crm_visibility import visible_source_ids

        visible_companies, visible_contacts, visible_deals = visible_source_ids(
            company_ids, contact_ids, deal_ids
        )
    else:
        visible_companies, visible_contacts, visible_deals = set(), set(), set()
    return {
        session_id
        for session_id, scope in stamped.items()
        if scope is not None
        and scope[0] <= visible_companies
        and scope[1] <= visible_contacts
        and scope[2] <= visible_deals
    }


def session_source_visible(session: Any) -> bool:
    """Deny legacy, changed, malformed, or now-unowned CRM-backed sessions."""
    return session.id in visible_session_ids([session])

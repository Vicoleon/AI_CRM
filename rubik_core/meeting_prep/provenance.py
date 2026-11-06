"""Server-stamped CRM source scope for persisted Meeting Prep prose."""

from __future__ import annotations

import re
from typing import Any

from rubik_core.tenancy.context import current_workspace

_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


def stamp(
    *, company_ids: set[str], deal_ids: set[str], unprovable: bool = False
) -> dict[str, Any]:
    """Record every CRM source supplied to a brief, including copied facts."""
    if any(
        not isinstance(value, str) or not _ID.fullmatch(value)
        for value in company_ids | deal_ids
    ):
        unprovable = True
    return {
        "schema_version": 1,
        "workspace": current_workspace(),
        "company_ids": sorted(company_ids),
        "deal_ids": sorted(deal_ids),
        "unprovable": unprovable,
    }


def _ids(marker: dict, name: str) -> set[str] | None:
    values = marker.get(name)
    if (
        not isinstance(values, list)
        or any(
            not isinstance(value, str) or not _ID.fullmatch(value) for value in values
        )
        or values != sorted(set(values))
    ):
        return None
    return set(values)


def _context_sources(bundle: Any) -> tuple[set[str], set[str]] | None:
    """Validate the server marker and return its source IDs without remote I/O."""
    if not isinstance(bundle, dict):
        return None
    marker = bundle.get("crm_provenance")
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
    except Exception:  # noqa: BLE001 - no tenant scope means no saved prose
        return None
    companies = _ids(marker, "company_ids")
    deals = _ids(marker, "deal_ids")
    if companies is None or deals is None:
        return None
    snapshot = bundle.get("account_snapshot") or {}
    if not isinstance(snapshot, dict):
        return None
    rows = snapshot.get("companies") or []
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return None
    cited = {
        str(row.get("hubspot_company_id"))
        for row in rows
        if row.get("hubspot_company_id")
    }
    if not cited <= companies:
        return None
    blocks = bundle.get("signal_blocks") or {}
    if not isinstance(blocks, dict):
        return None
    if blocks.get("xray") and not companies:
        return None
    if blocks.get("flagged") and not bundle.get("flagged_provenance_stamped"):
        return None
    return companies, deals


def contexts_visible(bundles: list[Any]) -> list[bool]:
    """Check many saved briefs with one live ownership scan.

    Legacy or malformed markers stay hidden, even if another row's sources
    are owned. Reusing one proof keeps long version histories affordable.
    """
    scopes = [_context_sources(bundle) for bundle in bundles]
    companies = set().union(*(scope[0] for scope in scopes if scope is not None))
    deals = set().union(*(scope[1] for scope in scopes if scope is not None))
    if not companies and not deals:
        return [scope is not None for scope in scopes]
    from rubik_core.tenancy.crm_visibility import visible_source_ids

    visible_companies, _, visible_deals = visible_source_ids(companies, set(), deals)
    return [
        scope is not None
        and scope[0] <= visible_companies
        and scope[1] <= visible_deals
        for scope in scopes
    ]


def context_visible(bundle: Any) -> bool:
    """Require a saved briefing's whole prompt scope to remain owned now."""
    return contexts_visible([bundle])[0]

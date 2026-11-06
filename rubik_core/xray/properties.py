"""Which HubSpot properties Customer X-Ray reads, and reading them off a record.

X-Ray always reads HubSpot's built-in deal and company properties. The rest of
what it uses (when a contract renews, whether it auto-renews, the account's
status, its customer-success manager, what the customer bought) lives in
properties each company creates for itself, under names of its own choosing.
So every one of them is a workspace HubSpot connector setting naming the
property by its internal name, empty by default:

* ``XRAY_RENEWAL_DATE_PROPERTY``: a deal date; the churn score's renewal
  factor and the renewal countdown.
* ``XRAY_AUTO_RENEW_PROPERTIES``: deal yes/no fields; any one true means the
  contract renews by itself.
* ``XRAY_ACCOUNT_STATUS_PROPERTIES``: company fields that state the account's
  status, read in order; the churn score's status factor and the status tile.
* ``XRAY_CSM_OWNER_PROPERTY``: a company field holding a HubSpot owner, shown
  as the CSM.
* ``HUBSPOT_DEAL_PRODUCT_PROPERTY`` (shared, ``rubik_core/retrieval/
  hubspot_properties.py``): what each deal is for; what the customer owns.
* ``XRAY_EXTRA_DEAL_PROPERTIES`` / ``XRAY_EXTRA_COMPANY_PROPERTIES``: any more
  fields the analyst should see, shown by name beside the deal and company
  facts.

A role left empty is simply absent: the churn scorer drops that factor and
renormalizes (it never scores the unknown as healthy), the tile stays empty,
and with no product property X-Ray proposes no expansion opportunity, since it
cannot tell what the customer already owns.
"""

from __future__ import annotations

from typing import Any

from rubik_core.retrieval.hubspot_properties import deal_product_property
from rubik_core.tenancy.hubspot_config import get_hubspot_config

#: Deal properties, HubSpot built-ins only. Beside the basics, the ACV and the
#: stage history the churn scorer and commercial-health section read.
DEAL_BUILT_INS: tuple[str, ...] = (
    "dealname",
    "amount",
    "hs_acv",
    "dealstage",
    "pipeline",
    "createdate",
    "closedate",
    "hs_is_closed",
    "hs_is_closed_won",
    "hs_v2_date_entered_current_stage",
    "hubspot_owner_id",
    "hs_lastmodifieddate",
)

#: Company properties, HubSpot built-ins only.
COMPANY_BUILT_INS: tuple[str, ...] = (
    "name",
    "domain",
    "notes_last_updated",
    "notes_last_contacted",
    "hs_last_sales_activity_timestamp",
    "hs_v2_latest_time_in_customer",
    "lifecyclestage",
    "createdate",
)


# ── The configured properties ────────────────────────────────────────────────


def renewal_date_property() -> str:
    return get_hubspot_config().xray_renewal_date_property


def auto_renew_properties() -> list[str]:
    return list(get_hubspot_config().xray_auto_renew_properties)


def account_status_properties() -> list[str]:
    return list(get_hubspot_config().xray_account_status_properties)


def csm_owner_property() -> str:
    return get_hubspot_config().xray_csm_owner_property


def extra_deal_properties() -> list[str]:
    return list(get_hubspot_config().xray_extra_deal_properties)


def extra_company_properties() -> list[str]:
    return list(get_hubspot_config().xray_extra_company_properties)


def _unique(*groups: Any) -> list[str]:
    names: list[str] = []
    for group in groups:
        for name in [group] if isinstance(group, str) else group:
            if name and name not in names:
                names.append(name)
    return names


def deal_properties() -> list[str]:
    """Every deal property X-Ray asks HubSpot for: built-ins, roles, extras."""
    return _unique(
        DEAL_BUILT_INS,
        deal_product_property(),
        renewal_date_property(),
        auto_renew_properties(),
        extra_deal_properties(),
    )


def company_properties() -> list[str]:
    """Every company property X-Ray asks HubSpot for: built-ins, roles, extras."""
    return _unique(
        COMPANY_BUILT_INS,
        account_status_properties(),
        csm_owner_property(),
        extra_company_properties(),
    )


def search_properties() -> list[str]:
    """What the company typeahead shows: name, domain, status and CSM."""
    return _unique(
        ("name", "domain"), account_status_properties(), csm_owner_property()
    )


# ── Reading a record ─────────────────────────────────────────────────────────


def _truthy(value: Any) -> bool:
    """HubSpot booleans arrive as the STRINGS 'true'/'false'. ``bool('false')``
    is True, so never use raw bool() on them: parse the string."""
    return str(value).strip().lower() in {"true", "yes", "1", "t", "y"}


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def products(deal: dict[str, Any]) -> str:
    """What a deal is for, as HubSpot holds it (``""`` when unknown)."""
    prop = deal_product_property()
    return _text(deal.get(prop)) if prop else ""


def renewal_date(deal: dict[str, Any]) -> Any:
    """A deal's raw renewal date value, or ``None``."""
    prop = renewal_date_property()
    return deal.get(prop) if prop else None


def auto_renews(deal: dict[str, Any]) -> bool:
    """Does any configured auto-renew field say yes on this deal?"""
    return any(_truthy(deal.get(prop)) for prop in auto_renew_properties())


def account_status(company: dict[str, Any]) -> str:
    """The account's status: every configured status field set, in order."""
    values = [_text(company.get(prop)) for prop in account_status_properties()]
    return " / ".join(dict.fromkeys(v for v in values if v))


def csm_owner_id(company: dict[str, Any]) -> str:
    """The HubSpot owner id the CSM field holds, or ``""``."""
    prop = csm_owner_property()
    return _text(company.get(prop)) if prop else ""


def extra_facts(record: dict[str, Any], names: list[str]) -> list[tuple[str, str]]:
    """``(property, value)`` for each listed property the record has a value for."""
    return [
        (name, _text(record.get(name))) for name in names if _text(record.get(name))
    ]

"""UC-45 product-catalog whitespace.

Runs against a small fictional catalog in the generic upload shape. Asserts
that owned categories (matched by category or product name, from closed-won
deals only) are excluded from whitespace, that each opportunity is labelled
with its catalog category, and that an empty catalog (a workspace that has
not uploaded one) offers nothing.
"""

from __future__ import annotations

import pytest

from rubik_core.retrieval import hubspot_properties
from rubik_core.retrieval.product_catalog import ProductCatalog
from rubik_core.tenancy.hubspot_config import HubSpotConnectorConfig
from rubik_core.xray import opportunities
from rubik_core.xray.schemas import CompanyBundle

#: One fictional product per category, plus a second product in Cleaning.
_CATALOG_ROWS = (
    {"name": "Nightly office clean", "category": "Cleaning"},
    {"name": "Deep carpet clean", "category": "Cleaning"},
    {"name": "Pest inspection", "category": "Pest Control"},
    {"name": "HVAC tune-up", "category": "Maintenance"},
    {"name": "Lobby plants", "category": ""},
)


@pytest.fixture(autouse=True)
def _named_product_field(monkeypatch):
    """What a deal is for is the company's own field; these tests name theirs."""
    current = HubSpotConnectorConfig(deal_product_property="services_sold")

    def configure(value: str):
        nonlocal current
        current = HubSpotConnectorConfig(deal_product_property=value)

    monkeypatch.setattr(hubspot_properties, "get_hubspot_config", lambda: current)
    return configure


@pytest.fixture
def stub_catalog(monkeypatch):
    monkeypatch.setattr(
        opportunities, "get_product_catalog", lambda: ProductCatalog(_CATALOG_ROWS)
    )


def _deal(products: str, *, won: bool = True, closed: bool = True) -> dict:
    return {
        "services_sold": products,
        "hs_is_closed": "true" if closed else "false",
        "hs_is_closed_won": "true" if won else "false",
    }


def _bundle(owned: str) -> CompanyBundle:
    b = CompanyBundle(company_id="1", company_name="Test Co")
    if owned:
        b.deals = [_deal(owned)]
    return b


def test_owned_categories_match_a_category_or_a_product_name(stub_catalog):
    owned = opportunities.owned_categories(
        _bundle("Deep Carpet Clean - annual; pest control contract")
    )
    assert owned == {"Cleaning", "Pest Control"}


def test_whitespace_excludes_owned_and_labels_the_category(stub_catalog):
    ws = opportunities.whitespace(_bundle("Nightly office clean"))
    by_cat = {w["category"]: w for w in ws}
    assert "Cleaning" not in by_cat
    assert by_cat["Pest Control"]["product"] == "Pest inspection"
    assert by_cat["Pest Control"]["entity"] == "Pest Control"
    # A row with no category is its own category, named after the product.
    assert by_cat["Lobby plants"]["entity"] == "Lobby plants"


def test_fallback_opportunities_are_whitespace_only(stub_catalog):
    opps = opportunities.fallback_opportunities(_bundle("Nightly office clean"))
    assert opps  # there IS whitespace
    for o in opps:
        assert o.product
        assert o.entity in {"Pest Control", "Maintenance", "Lobby plants"}


def test_only_closed_won_deals_count_as_owned(stub_catalog):
    b = CompanyBundle(company_id="1", company_name="Test Co")
    b.deals = [
        _deal("Pest inspection", won=True),
        _deal("HVAC tune-up", won=False),  # closed lost
        _deal("Nightly office clean", won=False, closed=False),  # still open
    ]

    assert opportunities.owned_categories(b) == {"Pest Control"}
    cats = {w["category"] for w in opportunities.whitespace(b, limit=50)}
    assert {"Maintenance", "Cleaning"} <= cats
    assert opportunities.owned_products_display(b) == ["Pest Control"]


def test_company_fields_are_not_products(stub_catalog):
    # Only the named deal field says what a customer owns; a company-level
    # field (an engagement count, say) never does.
    b = CompanyBundle(company_id="1", company_name="Test Co")
    b.company = {"services_sold": "Pest inspection", "active_projects": "3"}

    assert opportunities.owned_categories(b) == set()


def test_with_no_product_field_named_nothing_is_offered(
    stub_catalog, _named_product_field
):
    """Without the field X-Ray can't tell what the customer owns, so it offers
    nothing rather than a product they may already have."""
    _named_product_field("")
    b = _bundle("Nightly office clean")

    assert opportunities.owned_categories(b) == set()
    assert opportunities.whitespace(b) == []
    assert opportunities.fallback_opportunities(b) == []


def test_an_empty_catalog_offers_no_whitespace():
    """No catalog uploaded yet: nothing to offer, and no invented product."""
    assert opportunities.whitespace(_bundle("")) == []
    assert opportunities.fallback_opportunities(_bundle("Pest inspection")) == []
    assert opportunities.owned_categories(_bundle("Pest inspection")) == set()

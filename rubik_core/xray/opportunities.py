"""Product-catalog whitespace → expansion opportunities (spec §3).

Whitespace = catalog categories the customer does NOT already own. The catalog
is the workspace's own upload (Admin → Company setup); with none uploaded
there is no whitespace and X-Ray proposes nothing rather than inventing a
product.

* **What a customer owns** is the deal product field an admin named
  (``HUBSPOT_DEAL_PRODUCT_PROPERTY``) on its closed-won deals. Open and lost
  deals are not owned. With no product field named, X-Ray cannot tell what a
  customer owns, so it proposes nothing rather than offering a product the
  customer may already have.
* **Matching is by name, not by string equality.** A category counts as owned
  when the owned text mentions the category or any product in it
  (case-insensitive substring), because CRM picklist values rarely spell a
  catalog row exactly.
* **``entity`` is the catalog category**, the label X-Ray shows beside an
  opportunity.

We compute WHITESPACE deterministically; the LLM turns it into phrased
``opportunities`` (why_now/play). A deterministic fallback covers the case
where the model omits them.
"""

from __future__ import annotations

import logging
from typing import Any

from rubik_core.retrieval.hubspot_properties import deal_product_property
from rubik_core.retrieval.product_catalog import get_product_catalog
from rubik_core.xray import properties as xray_properties
from rubik_core.xray.schemas import CompanyBundle, Opportunity

logger = logging.getLogger(__name__)


def _owned_text(bundle: CompanyBundle) -> str:
    """Products on the customer's closed-won deals, lowercased for matching."""
    parts = [
        xray_properties.products(d)
        for d in bundle.deals
        if str(d.get("hs_is_closed_won") or "").lower() == "true"
    ]
    return " ".join(parts).lower()


def _category_of(row: dict[str, str]) -> str:
    return (row.get("category") or row.get("name") or "").strip()


def _categories(catalog: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    """Catalog rows grouped by category, in catalog order."""
    out: dict[str, list[dict[str, str]]] = {}
    for row in catalog:
        cat = _category_of(row)
        if cat:
            out.setdefault(cat, []).append(row)
    return out


def _owns(text: str, category: str, rows: list[dict[str, str]]) -> bool:
    tokens = {category.lower(), *((r.get("name") or "").strip().lower() for r in rows)}
    return any(tok and tok in text for tok in tokens)


def owned_categories(
    bundle: CompanyBundle, catalog: list[dict[str, str]] | None = None
) -> set[str]:
    rows = get_product_catalog().get_all_products() if catalog is None else catalog
    text = _owned_text(bundle)
    if not text:
        return set()
    return {
        cat for cat, members in _categories(rows).items() if _owns(text, cat, members)
    }


def owned_products_display(
    bundle: CompanyBundle, catalog: list[dict[str, str]] | None = None
) -> list[str]:
    """Human-readable list of what the customer appears to own (for the UI)."""
    rows = get_product_catalog().get_all_products() if catalog is None else catalog
    owned = owned_categories(bundle, rows)
    return [cat for cat in _categories(rows) if cat in owned]


def whitespace(bundle: CompanyBundle, *, limit: int = 12) -> list[dict[str, Any]]:
    """Categories the customer lacks, one representative product each.

    Returns ``[{product, category, entity, cross_sell_from, persona}]`` — the
    raw material the synthesis LLM phrases into opportunities.
    """
    if not deal_product_property():
        logger.info("no deal product field named; no whitespace computable")
        return []
    catalog = get_product_catalog().get_all_products()
    if not catalog:
        logger.info("product catalog empty; no whitespace computable")
        return []

    owned = owned_categories(bundle, catalog)
    owned_list = ", ".join(sorted(owned)) or "none on file"
    out: list[dict[str, Any]] = []
    for cat, rows in _categories(catalog).items():
        if cat in owned:
            continue
        row = rows[0]
        out.append(
            {
                "product": row.get("name") or cat,
                "category": cat,
                "entity": cat,
                "cross_sell_from": owned_list,
                "persona": row.get("description") or "",
            }
        )
    return out[:limit]


def fallback_opportunities(
    bundle: CompanyBundle, *, limit: int = 3
) -> list[Opportunity]:
    """Deterministic opportunities when the LLM omits them.

    Pure whitespace with a generic why_now/play so the section is never empty
    when there IS whitespace.
    """
    ws = whitespace(bundle, limit=limit)
    owned = owned_products_display(bundle)
    owned_txt = ", ".join(owned) if owned else "their current engagement"
    out: list[Opportunity] = []
    for w in ws[:limit]:
        out.append(
            Opportunity(
                title=f"Introduce {w['product']}",
                why_now=(
                    f"Not on file for this account; adjacent to {owned_txt}. "
                    "A whitespace expansion play."
                ),
                product=w["product"],
                play=f"Position {w['category']} as the natural next step in the relationship.",
                entity=w["entity"],
            )
        )
    return out

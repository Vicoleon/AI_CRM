"""The product and service catalog that X-Ray, Meeting Prep and the home feed read.

Each workspace uploads its own catalog in Admin → Company setup (spec §4.2.5,
``rubik_core/company/catalog.py``). This module hands it to the readers as
plain dicts with the generic column names (``name``, ``description``,
``category``, ``price``, ``unit``, ``url``; ``price`` is the printable label,
such as ``"$1,200/month"``, or ``""``).

The catalog is empty until an admin uploads one, and a database error reads
as empty too. Every caller treats an empty list as "no product knowledge":
X-Ray computes no whitespace, and Meeting Prep lists matched names without
details.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class ProductCatalog:
    """Catalog rows keyed by column name.

    Every accessor hands back copies, so a caller that edits a row cannot
    change it for the next caller.
    """

    def __init__(self, products: Iterable[Mapping[str, str]] = ()) -> None:
        self.products = [dict(p) for p in products]

    def get_all_products(self) -> list[dict[str, str]]:
        return [dict(p) for p in self.products]

    def search_products(self, query: str) -> list[dict[str, str]]:
        """Rows whose name or description contains any word of ``query``."""
        if not query:
            return []

        words = query.lower().split()
        results = []
        for product in self.products:
            name = product.get("name", "").lower()
            desc = product.get("description", "").lower()
            if any(word in name or word in desc for word in words):
                results.append(dict(product))
        return results

    def get_product_details(self, product_name: str) -> dict[str, str]:
        for product in self.products:
            if product.get("name", "").lower() == product_name.lower():
                return dict(product)
        return {}


def _read(db: Session) -> ProductCatalog:
    from rubik_core.company.catalog import list_catalog, price_label

    return ProductCatalog(
        {
            "name": row.name,
            "description": row.description,
            "category": row.category,
            "price": price_label(row),
            "unit": row.unit,
            "url": row.url,
        }
        for row in list_catalog(db)
    )


def get_product_catalog(db: Session | None = None) -> ProductCatalog:
    """The workspace's catalog; empty when none was uploaded. Never raises."""
    try:
        if db is not None:
            return _read(db)
        from rubik_core.database.db import SessionLocal

        with SessionLocal() as own:
            return _read(own)
    except Exception:  # noqa: BLE001 — no catalog is a normal state for a reader
        logger.warning("could not read the product catalog; treating it as empty")
        return ProductCatalog()

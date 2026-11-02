"""The product catalog is empty until a workspace uploads its own.

Every reader (X-Ray whitespace, Meeting Prep product knowledge, the home prep)
gets an empty catalog until an admin uploads one, never an error, and a
catalog built from rows hands out copies. Rows use the generic column names
(``name``, ``description``, …); ``test_company_catalog.py`` pins the upload.
"""

from __future__ import annotations

from rubik_core.retrieval.product_catalog import ProductCatalog, get_product_catalog

_ROWS = (
    {
        "name": "Window Cleaning - Monthly",
        "description": "Exterior glass, every month.",
    },
    {
        "name": "Carpet Care",
        "description": "Deep clean for office floors.",
    },
)


def test_the_workspace_catalog_is_empty_by_default():
    catalog = get_product_catalog()

    assert catalog.get_all_products() == []
    assert catalog.search_products("window cleaning") == []
    assert catalog.get_product_details("Window Cleaning - Monthly") == {}


def test_search_matches_a_word_in_the_name_or_the_description():
    catalog = ProductCatalog(_ROWS)

    assert [p["name"] for p in catalog.search_products("floors")] == ["Carpet Care"]
    assert [p["name"] for p in catalog.search_products("WINDOW")] == [
        "Window Cleaning - Monthly"
    ]
    assert catalog.search_products("") == []


def test_details_match_the_name_whatever_its_case():
    catalog = ProductCatalog(_ROWS)

    assert catalog.get_product_details("carpet care")["name"] == "Carpet Care"
    assert catalog.get_product_details("Gutters") == {}


def test_a_catalog_hands_out_copies():
    catalog = ProductCatalog(_ROWS)
    name = catalog.get_all_products()[0]["name"]

    catalog.get_all_products()[0]["name"] = "edited"
    catalog.get_product_details(name)["name"] = "edited"
    for row in catalog.search_products(name):
        row["name"] = "edited"

    assert catalog.get_all_products()[0]["name"] == name
    assert _ROWS[0]["name"] == name

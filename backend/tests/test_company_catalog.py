"""The product/service catalog (``rubik_core/company/catalog.py``).

Pinned: the generic CSV shape (only ``name`` required, ``price``/``unit``
optional), per-row errors named the way a spreadsheet numbers rows, an upload
that REPLACES the old catalog, an upload with errors that changes nothing, and
the X-Ray / Meeting Prep reader (``retrieval/product_catalog.py``) reading the
same rows.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth import get_current_user
from backend.main import app
from rubik_core.company import catalog
from rubik_core.database.db import get_db
from rubik_core.database.models import Base
from rubik_core.database.schemas import UserProfile

client = TestClient(app)

GOOD = (
    "name,description,category,price,unit,url\n"
    'Nightly office clean,Every weeknight,Cleaning,"$1,200",month,'
    "https://acme.example/clean\n"
    "Window wash,,Cleaning,,,\n"
)


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    def _get_db():
        yield session

    app.dependency_overrides[get_db] = _get_db
    try:
        yield session
    finally:
        app.dependency_overrides.clear()
        session.close()
        engine.dispose()


def _as(email: str, roles: list[str]) -> None:
    app.dependency_overrides[get_current_user] = lambda: UserProfile(
        id=email.split("@")[0],
        email=email,
        name=email.split("@")[0].title(),
        department="Operations",
        roles=roles,
        created_at="2026-10-28T00:00:00",
        updated_at="2026-10-28T00:00:00",
    )


# ── Parsing ──────────────────────────────────────────────────────────────────


def test_a_clean_file_parses_with_optional_price_and_unit():
    rows, errors = catalog.parse_catalog_csv(GOOD)
    assert errors == []
    assert rows[0] == catalog.CatalogRow(
        name="Nightly office clean",
        description="Every weeknight",
        category="Cleaning",
        price=Decimal("1200.00"),
        unit="month",
        url="https://acme.example/clean",
    )
    assert rows[1].price is None and rows[1].unit == ""
    assert catalog.price_label(rows[0]) == "$1,200/month"
    assert catalog.price_label(rows[1]) == ""


def test_a_missing_name_is_reported_on_its_spreadsheet_row():
    rows, errors = catalog.parse_catalog_csv("name,price\n,100\nWindow wash,50\n")
    assert errors == ["row 2: name is required"]
    assert [r.name for r in rows] == ["Window wash"]


def test_a_bad_price_is_an_error():
    _, errors = catalog.parse_catalog_csv("name,price\nWindow wash,cheap\n")
    assert errors == ['row 2: price "cheap" is not a number']
    _, negative = catalog.parse_catalog_csv("name,price\nWindow wash,-5\n")
    assert negative == ['row 2: price "-5" is not a number']


INT4_MAX = 2_147_483_647  # catalog_items.price_cents is a 32-bit integer


def test_the_price_cap_fits_the_cents_column():
    assert catalog.MAX_PRICE == Decimal("20000000")
    assert int(catalog.MAX_PRICE * 100) <= INT4_MAX


def test_twenty_million_is_the_highest_price_accepted():
    rows, errors = catalog.parse_catalog_csv(
        'name,price\nFleet contract,"$20,000,000.00"\n'
    )
    assert errors == [] and rows[0].price == Decimal("20000000.00")
    _, over = catalog.parse_catalog_csv("name,price\nFleet contract,20000000.01\n")
    assert over == ['row 2: price "20000000.01" is over the 20,000,000 limit']


def test_a_price_over_the_cap_says_so_rather_than_calling_it_not_a_number():
    """An extra zero is a number, just too big: the error has to say which."""
    _, errors = catalog.parse_catalog_csv('name,price\nFleet contract,"$200,000,000"\n')
    assert errors == ['row 2: price "$200,000,000" is over the 20,000,000 limit']


def test_the_highest_price_round_trips_through_the_cents_column(db):
    rows, _ = catalog.parse_catalog_csv("name,price\nFleet contract,20000000\n")
    catalog.replace_catalog(db, rows)
    (stored,) = catalog.list_catalog(db)
    assert stored.price == Decimal("20000000.00")


def test_header_column_order_and_case_do_not_matter_but_name_is_required():
    rows, errors = catalog.parse_catalog_csv("Price,NAME\n10.5,Mop\n")
    assert errors == [] and rows[0].price == Decimal("10.50")
    _, missing = catalog.parse_catalog_csv("title,price\nMop,10\n")
    assert missing and missing[0].startswith("row 1:")
    assert catalog.parse_catalog_csv("")[1][0].startswith("row 1:")


def test_duplicate_names_and_bad_urls_are_errors():
    _, errors = catalog.parse_catalog_csv(
        "name,url\nMop,\nmop,\nBucket,ftp://acme.example\n"
    )
    assert errors[0].startswith("row 3:") and "already on row 2" in errors[0]
    assert errors[1] == "row 4: url must start with http:// or https://"


# ── Replace and list ─────────────────────────────────────────────────────────


def test_replace_swaps_the_whole_catalog(db):
    first, _ = catalog.parse_catalog_csv(GOOD)
    assert catalog.replace_catalog(db, first) == 2
    second, _ = catalog.parse_catalog_csv("name\nCarpet care\n")
    assert catalog.replace_catalog(db, second) == 1
    assert [r.name for r in catalog.list_catalog(db)] == ["Carpet care"]


def test_rows_named_matches_case_insensitively_in_the_order_asked(db):
    rows, _ = catalog.parse_catalog_csv(GOOD)
    catalog.replace_catalog(db, rows)
    found = catalog.rows_named(db, ["window WASH", "gone", "Nightly office clean"])
    assert [r.name for r in found] == ["Window wash", "Nightly office clean"]


def test_the_product_catalog_reader_returns_the_uploaded_rows(db, monkeypatch):
    from rubik_core.retrieval import product_catalog

    assert product_catalog.get_product_catalog(db).get_all_products() == []
    rows, _ = catalog.parse_catalog_csv(GOOD)
    catalog.replace_catalog(db, rows)
    products = product_catalog.get_product_catalog(db).get_all_products()
    assert products[0]["name"] == "Nightly office clean"
    assert products[0]["price"] == "$1,200/month"
    assert products[0]["category"] == "Cleaning"


# ── API ──────────────────────────────────────────────────────────────────────


def _upload(body: str):
    return client.post(
        "/api/company/catalog/import",
        content=body.encode("utf-8"),
        headers={"Content-Type": "text/csv"},
    )


def test_import_replaces_the_old_rows(db):
    _as("admin@acme.example", ["admin", "user"])
    assert _upload(GOOD).json() == {"created": 2, "errors": []}
    res = _upload("name,price\nCarpet care,300\n")
    assert res.status_code == 200
    assert res.json() == {"created": 1, "errors": []}
    items = client.get("/api/company/catalog").json()["items"]
    assert [i["name"] for i in items] == ["Carpet care"]
    assert items[0]["price"] == "300.00"


def test_an_import_with_errors_changes_nothing(db):
    _as("admin@acme.example", ["admin", "user"])
    _upload(GOOD)
    res = _upload("name,price\nCarpet care,300\n,5\n")
    assert res.json() == {"created": 0, "errors": ["row 3: name is required"]}
    names = [i["name"] for i in client.get("/api/company/catalog").json()["items"]]
    assert names == ["Nightly office clean", "Window wash"]


def test_import_is_admin_only(db):
    _as("rep@acme.example", ["user"])
    assert _upload(GOOD).status_code == 403
    assert catalog.list_catalog(db) == []

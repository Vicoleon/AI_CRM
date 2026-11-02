"""The company profile (``rubik_core/company/profile.py``, Admin → Company setup).

Pinned (Review Focus 1): an EMPTY profile reads naturally — the context block
is empty and the name falls back to "the company", never "None" or a blank —
a saved profile round-trips, only an admin may change it, and
``/api/users/me`` carries ``workspace.company_name`` ("" until one is saved).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth import get_current_user
from backend.main import app
from rubik_core.company.profile import (
    CompanyProfile,
    company_context_block,
    company_name_for_copy,
    company_name_or_default,
    company_possessive,
    get_company_profile,
    profile_from_dict,
    save_company_profile,
)
from rubik_core.database.db import get_db
from rubik_core.database.models import Base, WorkspaceSetting
from rubik_core.database.schemas import UserProfile

client = TestClient(app)

ACME = CompanyProfile(
    name="Acme Corp",
    legal_name="Acme Corporation LLC",
    website="https://acme.example",
    address="1 Main St\nSpringfield",
    blurb="Spotless offices since 1999.",
    industry="Industrial cleaning",
    offering="Office and warehouse cleaning contracts",
    customers="Facilities managers at mid-size companies",
    sales_process="Site visit, quote, two-week trial",
    products=("Nightly office clean", "Warehouse floor care"),
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


# ── Empty profile ────────────────────────────────────────────────────────────


def test_an_empty_profile_has_no_context_and_reads_as_the_company():
    empty = CompanyProfile()
    assert empty.is_empty
    assert company_context_block(empty) == ""
    assert company_name_or_default(empty) == "the company"
    assert company_possessive(empty) == "the company's"


def test_the_context_block_names_only_what_is_set():
    block = company_context_block(CompanyProfile(name="Acme Corp"))
    assert "Acme Corp" in block
    assert "None" not in block
    assert "Industry" not in block  # an unset field gets no empty label
    full = company_context_block(ACME)
    for fragment in ("Industrial cleaning", "Nightly office clean; Warehouse floor"):
        assert fragment in full
    assert "None" not in full and "{" not in full


def test_a_legal_name_stands_in_for_a_missing_name():
    only_legal = CompanyProfile(legal_name="Acme Corporation LLC")
    assert company_name_or_default(only_legal) == "Acme Corporation LLC"
    assert company_possessive(CompanyProfile(name="Widgets")) == "Widgets'"


def test_reading_with_no_row_or_a_bad_row_gives_the_empty_profile(db):
    assert get_company_profile(db) == CompanyProfile()
    db.add(WorkspaceSetting(key="company_profile", value=["not", "a", "dict"]))
    db.commit()
    assert get_company_profile(db) == CompanyProfile()


def test_copy_for_the_companys_own_people_says_your_company_until_named(db):
    """UI copy addressed to the company's own people ("outside {company}")
    says "your company" with no profile saved, never "None" or a blank."""
    assert company_name_for_copy(db) == "your company"
    save_company_profile(db, CompanyProfile(legal_name="Acme Corporation LLC"))
    assert company_name_for_copy(db) == "Acme Corporation LLC"
    save_company_profile(db, ACME)
    assert company_name_for_copy(db) == "Acme Corp"


def test_profile_from_dict_drops_non_text_and_duplicate_products():
    p = profile_from_dict(
        {"name": None, "industry": 7, "products": ["A", "A", " ", 3, "B"], "x": "y"}
    )
    assert p.name == "" and p.industry == ""
    assert p.products == ("A", "B")


# ── Round trip ───────────────────────────────────────────────────────────────


def test_save_and_load_round_trip(db):
    saved = save_company_profile(db, ACME)
    assert saved == ACME
    assert get_company_profile(db) == ACME
    changed = save_company_profile(db, CompanyProfile(name="Acme Two"))
    assert get_company_profile(db) == changed
    assert db.query(WorkspaceSetting).count() == 1


# ── API ──────────────────────────────────────────────────────────────────────


def test_put_profile_is_admin_only(db):
    _as("rep@acme.example", ["user"])
    res = client.put("/api/company/profile", json={"name": "Hijacked"})
    assert res.status_code == 403
    assert get_company_profile(db) == CompanyProfile()
    # Reading is open to anyone the copilot lets in.
    assert client.get("/api/company/profile").json()["name"] == ""


def test_an_admin_saves_the_profile_and_everyone_reads_it(db):
    _as("admin@acme.example", ["admin", "user"])
    body = {"name": "Acme Corp", "industry": "Industrial cleaning", "products": ["A"]}
    res = client.put("/api/company/profile", json=body)
    assert res.status_code == 200, res.text
    assert res.json()["name"] == "Acme Corp"
    assert res.json()["products"] == ["A"]
    _as("rep@acme.example", ["user"])
    assert client.get("/api/company/profile").json()["industry"] == (
        "Industrial cleaning"
    )


def test_users_me_carries_the_workspace_company_name(db):
    _as("rep@acme.example", ["user"])
    res = client.get("/api/users/me")
    assert res.status_code == 200, res.text
    assert res.json()["workspace"] == {
        "slug": "test",
        "name": "Test Workspace",
        "company_name": "",
    }
    save_company_profile(db, CompanyProfile(name="Acme Corp"))
    assert client.get("/api/users/me").json()["workspace"] == {
        "slug": "test",
        "name": "Test Workspace",
        "company_name": "Acme Corp",
    }

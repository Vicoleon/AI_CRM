"""Smoke tests for V2 Recent endpoints.

Covers the touch / list loop, upsert behavior (second touch updates the
existing row rather than creating a duplicate), and per-user scoping.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth import get_current_user
from backend.main import app
from rubik_core.auth.registry import UserProfile
from rubik_core.database.db import get_db
from rubik_core.database.models import Base, UserRecentTouch

SQLALCHEMY_DATABASE_URL = "sqlite:///:memory:"
engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    try:
        db = TestingSessionLocal()
        yield db
    finally:
        db.close()


def _profile(email: str = "test@acme.example") -> UserProfile:
    return UserProfile(
        id="test-user-prof",
        email=email,
        name=email.split("@")[0].title(),
        department="sales",  # any department: the copilot is open to everyone
        roles=["user"],
        created_at="2026-03-04T10:00:00Z",
        updated_at="2026-03-04T10:00:00Z",
    )


def mock_get_current_user():
    return _profile()


@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


def setup_module(module):
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = mock_get_current_user


def teardown_module(module):
    app.dependency_overrides.clear()


def test_touch_then_list_returns_upserted_row():
    """Two touches of the same target → one row, latest title/link wins."""
    client = TestClient(app)

    # Empty list initially.
    r = client.get("/api/recent/")
    assert r.status_code == 200
    assert r.json() == []

    # First touch.
    body1 = {
        "target_kind": "deal",
        "target_id": "deal-42",
        "title": "Acme Q3 Renewal",
        "link": "/v2/deals/deal-42",
    }
    r = client.post("/api/recent/touch", json=body1)
    assert r.status_code == 200
    first = r.json()
    assert first["title"] == "Acme Q3 Renewal"
    first_id = first["id"]

    # Second touch of the same (kind, id) — the deal got renamed.
    body2 = {
        "target_kind": "deal",
        "target_id": "deal-42",
        "title": "Acme Inc Q3 Renewal",  # updated label
        "link": "/v2/deals/deal-42",
    }
    r = client.post("/api/recent/touch", json=body2)
    assert r.status_code == 200
    second = r.json()
    # Same row id — upsert, not insert.
    assert second["id"] == first_id
    assert second["title"] == "Acme Inc Q3 Renewal"

    # Exactly one row in the list.
    r = client.get("/api/recent/")
    items = r.json()
    assert len(items) == 1
    assert items[0]["id"] == first_id
    assert items[0]["title"] == "Acme Inc Q3 Renewal"


def test_recent_list_is_scoped_to_caller():
    """Foreign rows are invisible to the caller — privacy rule from §Privacy."""
    # Seed a recent-touch row owned by someone else.
    db = TestingSessionLocal()
    now = datetime.utcnow()
    foreign = UserRecentTouch(
        id="foreign-id",
        actor_email="someone-else@acme.example",
        target_kind="deal",
        target_id="deal-secret",
        title="Confidential Deal",
        link="/v2/deals/deal-secret",
        touched_at=now,
    )
    own = UserRecentTouch(
        id="own-id",
        actor_email="test@acme.example",
        target_kind="meeting",
        target_id="meet-1",
        title="My L10",
        link="/v2/meetings/meet-1",
        touched_at=now - timedelta(minutes=5),
    )
    db.add_all([foreign, own])
    db.commit()
    db.close()

    client = TestClient(app)
    r = client.get("/api/recent/")
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 1
    assert items[0]["id"] == "own-id"
    assert items[0]["title"] == "My L10"

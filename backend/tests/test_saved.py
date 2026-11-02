"""Smoke tests for V2 Saved (bookmark) endpoints.

Covers the create / list / delete loop, idempotency on the UNIQUE
constraint, and the privacy rule: bookmarks belonging to another user
must surface as 404, not 403 (so existence isn't leaked).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth import get_current_user
from backend.main import app
from rubik_core.auth.registry import UserProfile
from rubik_core.database.db import get_db
from rubik_core.database.models import Base, UserBookmark

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


def test_create_list_delete_saved():
    """Full CRUD loop for a single user."""
    client = TestClient(app)

    # Empty list to start
    r = client.get("/api/saved/")
    assert r.status_code == 200
    assert r.json() == []

    # Create
    payload = {"target_kind": "deal", "target_id": "deal-123"}
    r = client.post("/api/saved/", json=payload)
    assert r.status_code == 200
    created = r.json()
    assert created["target_kind"] == "deal"
    assert created["target_id"] == "deal-123"
    assert created["id"]
    bookmark_id = created["id"]

    # Idempotent — second POST returns the same row
    r2 = client.post("/api/saved/", json=payload)
    assert r2.status_code == 200
    assert r2.json()["id"] == bookmark_id

    # List shows exactly one
    r = client.get("/api/saved/")
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 1
    assert items[0]["id"] == bookmark_id

    # Delete
    r = client.delete(f"/api/saved/{bookmark_id}")
    assert r.status_code == 200

    # List is empty again
    r = client.get("/api/saved/")
    assert r.json() == []


def test_delete_other_users_bookmark_returns_404():
    """Caller cannot delete (or even discover) another user's bookmark."""
    # Seed a bookmark owned by someone else directly via the DB.
    db = TestingSessionLocal()
    other_row = UserBookmark(
        id="other-bookmark-id",
        actor_email="someone-else@acme.example",
        target_kind="deal",
        target_id="deal-xyz",
        created_at=__import__("datetime").datetime.utcnow(),
    )
    db.add(other_row)
    db.commit()
    db.close()

    client = TestClient(app)
    # Caller is test@acme.example (from mock_get_current_user) — should NOT see
    # the foreign row.
    r = client.get("/api/saved/")
    assert r.status_code == 200
    assert r.json() == []

    # Delete must 404 (not 403) per the v2 spec privacy rule.
    r = client.delete("/api/saved/other-bookmark-id")
    assert r.status_code == 404

    # Foreign row must still exist (delete shouldn't have leaked through).
    db = TestingSessionLocal()
    assert db.query(UserBookmark).filter_by(id="other-bookmark-id").count() == 1
    db.close()

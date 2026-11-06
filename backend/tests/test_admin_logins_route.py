"""GET /api/admin/logins — admin-gated login activity listing.

Exercises the endpoint end-to-end through the FastAPI app (which also proves
the admin router imports cleanly and the route is wired into ``backend.main``).
Run with the local-DB env guard so it never touches Cloud SQL:

    APP_MODE=local CLOUDSQL_CONNECTION_NAME="" DATABASE_URL="sqlite:///..." \\
        uv run pytest backend/tests/test_admin_logins_route.py
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from rubik_core.database.db import SessionLocal, engine
from rubik_core.database.models import Base, LoginEvent


@pytest.fixture(scope="module", autouse=True)
def _bootstrap_schema():
    Base.metadata.create_all(bind=engine)
    yield


def _client(roles: list[str]) -> TestClient:
    from backend.auth import get_current_user
    from rubik_core.database.schemas import UserProfile

    def _override():
        return UserProfile(
            id="admin@x.example",
            email="admin@x.example",
            name="Admin",
            roles=roles,
            created_at="2026-01-01T00:00:00",
            updated_at="2026-01-01T00:00:00",
        )

    app.dependency_overrides[get_current_user] = _override
    client = TestClient(app)
    client._cleanup = lambda: app.dependency_overrides.pop(get_current_user, None)
    return client


@pytest.fixture()
def seeded_logins():
    """Two login rows for one user + one for another; returns the marker email."""
    db = SessionLocal()
    marker = f"user-{uuid.uuid4().hex[:6]}@acme.example"
    other = f"other-{uuid.uuid4().hex[:6]}@acme.example"
    now = datetime.utcnow()
    try:
        db.add_all(
            [
                LoginEvent(
                    user_email=marker,
                    timestamp=now - timedelta(hours=2),
                    ip_address="10.0.0.1",
                    user_agent="Chrome",
                ),
                LoginEvent(
                    user_email=marker,
                    timestamp=now,
                    ip_address="10.0.0.2",
                    user_agent="Safari",
                ),
                LoginEvent(user_email=other, timestamp=now - timedelta(hours=1)),
            ]
        )
        db.commit()
        yield marker, other
        db.query(LoginEvent).filter(LoginEvent.user_email.in_([marker, other])).delete(
            synchronize_session=False
        )
        db.commit()
    finally:
        db.close()


def test_admin_can_list_logins_newest_first(seeded_logins):
    marker, _ = seeded_logins
    client = _client(["admin", "user"])
    try:
        resp = client.get("/api/admin/logins", params={"email": marker})
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 2
        # Newest first.
        assert rows[0]["timestamp"] >= rows[1]["timestamp"]
        assert rows[0]["ip_address"] == "10.0.0.2"
        assert set(rows[0]) >= {
            "id",
            "user_email",
            "timestamp",
            "ip_address",
            "user_agent",
        }
    finally:
        client._cleanup()


def test_email_filter_scopes_to_one_user(seeded_logins):
    marker, other = seeded_logins
    client = _client(["admin"])
    try:
        resp = client.get("/api/admin/logins", params={"email": other})
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 1
        assert rows[0]["user_email"] == other
    finally:
        client._cleanup()


def test_non_admin_is_forbidden(seeded_logins):
    client = _client(["user"])
    try:
        resp = client.get("/api/admin/logins")
        assert resp.status_code == 403
    finally:
        client._cleanup()

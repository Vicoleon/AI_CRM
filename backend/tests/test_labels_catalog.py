"""Drive label → priority catalog API (/api/labels).

Admin-gated CRUD with the priority weight clamped to [1.0, 2.0]. Mocks auth via
the standard get_current_user dependency override; uses an isolated tmp SQLite.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from backend.models.auth import User
from rubik_core.config import settings

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_test_env(tmp_path):
    db_path = tmp_path / "test_labels.db"
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    from rubik_core.database.db import engine
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    yield
    app.dependency_overrides.clear()


def _as(role_admin: bool):
    app.dependency_overrides[get_current_user] = lambda: User(
        id="u1",
        email="u1@acme.example",
        name="User One",
        roles=["admin"] if role_admin else [],
        department="Sales",
    )


def test_non_admin_is_denied_everywhere():
    _as(role_admin=False)
    assert client.get("/api/labels/").status_code == 403
    assert (
        client.post(
            "/api/labels/", json={"label_value": "Important", "priority": 1.5}
        ).status_code
        == 403
    )
    assert client.delete("/api/labels/:important").status_code == 403


def test_create_list_update_delete_flow():
    _as(role_admin=True)

    # Create
    r = client.post("/api/labels/", json={"label_value": "Important", "priority": 1.5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == ":important"
    assert body["label_value"] == "Important"
    assert body["priority"] == 1.5

    # List
    r = client.get("/api/labels/")
    assert r.status_code == 200
    ids = {row["id"] for row in r.json()}
    assert ":important" in ids

    # Update priority
    r = client.patch("/api/labels/:important", json={"priority": 1.2})
    assert r.status_code == 200
    assert r.json()["priority"] == 1.2

    # Delete
    r = client.delete("/api/labels/:important")
    assert r.status_code == 200
    r = client.get("/api/labels/")
    assert ":important" not in {row["id"] for row in r.json()}


def test_priority_is_clamped_to_1_2_range():
    _as(role_admin=True)
    r = client.post("/api/labels/", json={"label_value": "Loud", "priority": 9.9})
    assert r.status_code == 200
    assert r.json()["priority"] == 2.0


def test_create_with_label_key_and_idempotent_upsert():
    _as(role_admin=True)
    r1 = client.post(
        "/api/labels/",
        json={
            "label_key": "sensitivity",
            "label_value": "Confidential",
            "priority": 1.3,
        },
    )
    assert r1.status_code == 200
    assert r1.json()["id"] == "sensitivity:confidential"

    # Re-posting the same key/value updates rather than duplicating.
    r2 = client.post(
        "/api/labels/",
        json={
            "label_key": "sensitivity",
            "label_value": "Confidential",
            "priority": 1.6,
        },
    )
    assert r2.status_code == 200
    rows = [
        row
        for row in client.get("/api/labels/").json()
        if row["id"] == "sensitivity:confidential"
    ]
    assert len(rows) == 1
    assert rows[0]["priority"] == 1.6


def test_update_missing_returns_404():
    _as(role_admin=True)
    assert client.patch("/api/labels/:nope", json={"priority": 1.5}).status_code == 404
    assert client.delete("/api/labels/:nope").status_code == 404

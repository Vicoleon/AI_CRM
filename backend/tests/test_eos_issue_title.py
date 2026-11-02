"""Tests for the EOS Issue ``title`` field.

Issues gained a short ``title`` (the IDS grid headline) alongside the full
``description`` (shown in a read-only detail modal). The server guarantees a
title is always populated:

- an explicit client title is preserved verbatim (create + edit),
- when omitted/blank it is derived from the description's first line,
  trimmed to 120 chars with a trailing ellipsis,
- an edit that would leave the title blank re-derives it,
- ``title`` is part of the serialized response contract (the frontend grid
  renders only the title).

Uses the same tmp-file SQLite fixture as ``test_eos_add_to_ids.py`` — fully
isolated from any deployed database.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from rubik_core.auth.registry import UserRegistry
from rubik_core.config import settings
from rubik_core.database.schemas import UserProfile

client = TestClient(app)

_DEPT = "Systems"


def _profile(email: str, roles: list[str] | None = None) -> UserProfile:
    now = "2026-05-21T00:00:00"
    stored = UserRegistry().get_user(email)
    return UserProfile(
        id=stored.id if stored else email.split("@")[0],
        email=email,
        name=stored.name if stored else email.split("@")[0].title(),
        roles=roles or (stored.roles if stored else ["user"]),
        department=stored.department if stored else "general",
        created_at=now,
        updated_at=now,
        is_exec=bool(stored.is_exec) if stored else False,
    )


def _as_user(email: str, roles: list[str] | None = None):
    def _factory():
        return _profile(email, roles)

    app.dependency_overrides[get_current_user] = _factory


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db_path = tmp_path / "test_issue_title.db"
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    import rubik_core.database.db as dbmod

    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database import eos  # noqa: F401
    from rubik_core.database.models import Base, Department

    Base.metadata.create_all(bind=engine)

    # Seat the department the issues are scoped to (FK target).
    from rubik_core.database.db import SessionLocal

    db = SessionLocal()
    try:
        if not db.query(Department).filter(Department.name == _DEPT).first():
            db.add(Department(name=_DEPT))
            db.commit()
    finally:
        db.close()

    reg = UserRegistry()
    if not reg.get_user("admin@example.com"):
        reg.register_user(
            email="admin@example.com",
            name="Admin",
            id="admin@example.com",
            roles=["admin"],
        )
    yield
    app.dependency_overrides.clear()


def _create(**overrides) -> dict:
    """POST /api/eos/issues/ as admin and return the 201 body."""
    _as_user("admin@example.com", roles=["admin"])
    payload = {
        "description": "Body text",
        "priority": "medium",
        "department_name": _DEPT,
    }
    payload.update(overrides)
    r = client.post("/api/eos/issues/", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


# -- Create -----------------------------------------------------------


def test_create_preserves_explicit_title():
    body = _create(
        title="Custom headline",
        description="A much longer body that should NOT become the title.",
    )
    assert body["title"] == "Custom headline"


def test_create_without_title_derives_from_first_line():
    body = _create(title="", description="First line\nSecond line\nThird")
    assert body["title"] == "First line"


def test_create_without_title_truncates_long_description():
    body = _create(title=None, description="x" * 200)
    # 120-char cap with a trailing ellipsis (119 chars + "…").
    assert len(body["title"]) == 120
    assert body["title"].endswith("…")


def test_created_issue_serializes_title_on_get_and_list():
    created = _create(title="Serialize me", description="body")
    issue_id = created["id"]

    got = client.get(f"/api/eos/issues/{issue_id}")
    assert got.status_code == 200, got.text
    assert got.json()["title"] == "Serialize me"

    rows = client.get("/api/eos/issues/", params={"department_name": _DEPT}).json()
    match = [r for r in rows if r["id"] == issue_id]
    assert len(match) == 1
    assert match[0]["title"] == "Serialize me"


# -- Update -----------------------------------------------------------


def test_update_preserves_new_explicit_title():
    created = _create(title="Original")
    r = client.patch(
        f"/api/eos/issues/{created['id']}",
        json={"title": "Renamed"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Renamed"


def test_update_clearing_title_re_derives_from_description():
    created = _create(
        title="Will be cleared",
        description="Recovered headline\nplus detail",
    )
    r = client.patch(
        f"/api/eos/issues/{created['id']}",
        json={"title": ""},
    )
    assert r.status_code == 200, r.text
    # Blank title is repaired from the (unchanged) description's first line,
    # never persisted empty.
    assert r.json()["title"] == "Recovered headline"

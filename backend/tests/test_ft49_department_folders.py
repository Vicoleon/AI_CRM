"""Per-department shared Drive folders: store resolution + endpoint.

Uses a real in-memory SQLite DB (so the model + query logic are exercised for
real) with the store's ``SessionLocal`` monkeypatched to point at it.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import rubik_core.productivity.department_folders_store as store
from backend.auth import get_current_user
from backend.main import app
from backend.routers.department_folders import _normalize_folder_id
from rubik_core.database.models import Base


@pytest.fixture
def memory_db(monkeypatch):
    """Point the store at a fresh in-memory SQLite DB for each test."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(store, "SessionLocal", TestSession)
    return TestSession


# ── _normalize_folder_id (admins paste either a bare id or a Drive URL) ────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1AbCdEf", "1AbCdEf"),
        ("https://drive.google.com/drive/folders/1AbCdEf", "1AbCdEf"),
        ("https://drive.google.com/drive/u/0/folders/1AbCdEf?usp=sharing", "1AbCdEf"),
        ("  1AbCdEf?usp=drive_link  ", "1AbCdEf"),
    ],
)
def test_normalize_folder_id(raw, expected):
    assert _normalize_folder_id(raw) == expected


# ── store resolution: dept rows first, then company-wide; inactive excluded ────


def test_list_for_user_dept_then_company_wide(memory_db):
    store.create_folder(
        folder_id="co",
        folder_name="Company HR Docs",
        department=None,
        actor_email="a@x.example",
        sort_order=10,
    )
    store.create_folder(
        folder_id="sales1",
        folder_name="Sales Shared",
        department="Sales",
        actor_email="a@x.example",
        sort_order=20,
    )
    store.create_folder(
        folder_id="hr1",
        folder_name="HR Only",
        department="HR",
        actor_email="a@x.example",
        sort_order=5,
    )
    store.create_folder(
        folder_id="dead",
        folder_name="Archived",
        department="Sales",
        actor_email="a@x.example",
        is_active=False,
    )

    # Sales: dept row first, then company-wide; inactive excluded; HR not shown.
    assert [r.folder_id for r in store.list_for_user("Sales")] == ["sales1", "co"]
    # HR sees its own + company-wide.
    assert [r.folder_id for r in store.list_for_user("HR")] == ["hr1", "co"]
    # No / general department → only company-wide.
    assert [r.folder_id for r in store.list_for_user(None)] == ["co"]


def test_crud_roundtrip(memory_db):
    row = store.create_folder(
        folder_id="f1", folder_name="F1", department="Sales", actor_email="a@x.example"
    )
    assert store.get_folder(row.id).folder_name == "F1"

    store.update_folder(
        row.id, actor_email="b@x.example", folder_name="F1b", is_active=False
    )
    updated = store.get_folder(row.id)
    assert updated.folder_name == "F1b"
    assert updated.is_active is False
    assert updated.updated_by == "b@x.example"

    assert store.delete_folder(row.id) is True
    assert store.get_folder(row.id) is None
    assert store.delete_folder(row.id) is False  # already gone


# ── /mine endpoint resolves the caller's department ───────────────────────────


def test_mine_endpoint_returns_dept_and_company(memory_db):
    store.create_folder(
        folder_id="co",
        folder_name="Company",
        department=None,
        actor_email="a@x.example",
        sort_order=10,
    )
    store.create_folder(
        folder_id="s1",
        folder_name="Sales",
        department="Sales",
        actor_email="a@x.example",
        sort_order=5,
    )

    user = MagicMock()
    user.email = "rep@example.com"
    user.department = "Sales"
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        client = TestClient(app)
        res = client.get("/api/department-folders/mine")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["department"] == "Sales"
        assert [f["folder_id"] for f in body["folders"]] == ["s1", "co"]
    finally:
        app.dependency_overrides.pop(get_current_user, None)

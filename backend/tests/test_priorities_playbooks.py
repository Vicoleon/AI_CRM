"""UC-23 tests — department playbook CRUD + effective resolution.

The playbook_store talks straight to ``SessionLocal()`` so tests use a
small in-memory pretend store the same way ``test_priorities_admin.py``
does. End-to-end DB integration is covered separately by alembic
runs in dev.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from backend.auth import admin_required, get_current_user
from backend.main import app
from rubik_core.priorities.playbook_store import (
    PlaybookRow,
    resolve_effective,
)

# ── Fixtures ─────────────────────────────────────────────────────────────────


def _mock_admin():
    user = MagicMock()
    user.email = "admin@example.com"
    user.roles = ["admin", "user"]
    return user


def _mock_user():
    user = MagicMock()
    user.email = "user@example.com"
    user.roles = ["user"]
    return user


class _InMemoryPlaybookStore:
    """Stand-in that mimics enough of SQLAlchemy's chained API for the
    handful of queries playbook_store + routers actually run."""

    def __init__(self) -> None:
        self.rows: dict[str, MagicMock] = {}

    # ── DB-side handles ────────────────────────────────────────────────

    def session_factory(self):
        """Return a ``SessionLocal()`` substitute that yields a MagicMock
        whose query/filter/order_by/all + add/delete/commit work like a
        thin wrapper around our in-memory dict."""
        store = self
        mock_db = MagicMock()

        def query_factory(_model):
            q = _Query(store)
            return q

        mock_db.query.side_effect = query_factory

        def add(obj):
            store.rows[obj.id] = obj

        def delete(obj):
            store.rows.pop(obj.id, None)

        def commit():
            return None

        def refresh(obj):
            # Simulate server defaults: created_at / updated_at set if missing.
            now = datetime.utcnow()
            if not getattr(obj, "created_at", None):
                obj.created_at = now
            if not getattr(obj, "updated_at", None):
                obj.updated_at = now

        mock_db.add = add
        mock_db.delete = delete
        mock_db.commit = commit
        mock_db.refresh = refresh

        # SessionLocal() must support ``with ... as db`` — wrap.
        ctx = MagicMock()
        ctx.__enter__.return_value = mock_db
        ctx.__exit__.return_value = False
        return ctx


class _Query:
    """Mimics SQLAlchemy's chained Query just well enough for our store."""

    def __init__(self, store: _InMemoryPlaybookStore):
        self.store = store
        self._dept_filter: tuple[str, Any] | None = None
        self._active_filter: bool | None = None
        self._id_filter: str | None = None

    def filter(self, expr):
        # Best-effort introspection of the SQLAlchemy clause. The store
        # only needs to handle the four filter patterns playbook_store uses.
        text = str(expr).lower()
        if "priority_playbook.id" in text:
            # ".id == 'foo'"
            right = getattr(expr, "right", None)
            if right is not None:
                self._id_filter = right.value
        elif "priority_playbook.department" in text and "is null" in text:
            self._dept_filter = ("eq", None)
        elif "priority_playbook.department" in text:
            right = getattr(expr, "right", None)
            if right is not None:
                self._dept_filter = ("eq", right.value)
        elif "priority_playbook.is_active" in text and "true" in text:
            self._active_filter = True
        return self

    def order_by(self, *_):
        return self

    def all(self):
        results = list(self.store.rows.values())
        if self._dept_filter is not None:
            _, want = self._dept_filter
            results = [r for r in results if r.department == want]
        if self._active_filter is True:
            results = [r for r in results if r.is_active]
        results.sort(key=lambda r: (r.sort_order, r.created_at))
        return results

    def first(self):
        if self._id_filter is not None:
            return self.store.rows.get(self._id_filter)
        results = self.all()
        return results[0] if results else None


@pytest.fixture
def store(monkeypatch):
    s = _InMemoryPlaybookStore()
    monkeypatch.setattr(
        "rubik_core.priorities.playbook_store.SessionLocal",
        s.session_factory,
    )
    return s


@pytest.fixture
def admin_client():
    prev_admin = app.dependency_overrides.get(admin_required)
    prev_user = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[admin_required] = _mock_admin
    app.dependency_overrides[get_current_user] = _mock_admin
    client = TestClient(app)
    yield client
    if prev_admin is None:
        app.dependency_overrides.pop(admin_required, None)
    else:
        app.dependency_overrides[admin_required] = prev_admin
    if prev_user is None:
        app.dependency_overrides.pop(get_current_user, None)
    else:
        app.dependency_overrides[get_current_user] = prev_user


# ── Endpoint tests ───────────────────────────────────────────────────────────


def test_list_empty_when_no_rows(admin_client, store):
    res = admin_client.get("/api/admin/priorities/playbooks")
    assert res.status_code == 200, res.text
    assert res.json() == []


def test_create_and_round_trip(admin_client, store):
    res = admin_client.post(
        "/api/admin/priorities/playbooks",
        json={
            "name": "Sales — Pipeline",
            "department": "Sales",
            "body": "# matters\n- Decision-stage deals\n",
            "is_active": True,
            "sort_order": 100,
        },
    )
    assert res.status_code == 201, res.text
    pb = res.json()
    assert pb["name"] == "Sales — Pipeline"
    assert pb["department"] == "Sales"
    assert pb["body"].startswith("# matters")

    list_res = admin_client.get("/api/admin/priorities/playbooks")
    assert list_res.status_code == 200
    assert len(list_res.json()) == 1
    # List view returns body_preview, NOT the full body
    assert "body_preview" in list_res.json()[0]


def test_update_partial_payload(admin_client, store):
    create = admin_client.post(
        "/api/admin/priorities/playbooks",
        json={"name": "n1", "department": "Ops", "body": "v1"},
    ).json()
    pid = create["id"]

    res = admin_client.put(
        f"/api/admin/priorities/playbooks/{pid}",
        json={"body": "v2 only"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["body"] == "v2 only"
    assert res.json()["department"] == "Ops"  # untouched


def test_delete_removes_row(admin_client, store):
    pid = admin_client.post(
        "/api/admin/priorities/playbooks",
        json={"name": "to-delete", "body": "x"},
    ).json()["id"]

    del_res = admin_client.delete(f"/api/admin/priorities/playbooks/{pid}")
    assert del_res.status_code == 204
    get_res = admin_client.get(f"/api/admin/priorities/playbooks/{pid}")
    assert get_res.status_code == 404


def test_non_admin_rejected():
    prev_user = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = _mock_user
    try:
        client = TestClient(app)
        res = client.get("/api/admin/priorities/playbooks")
        assert res.status_code == 403
    finally:
        if prev_user is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = prev_user


# ── Effective resolution ─────────────────────────────────────────────────────


def test_effective_concatenates_dept_and_default(admin_client, store):
    # Default-everyone row first.
    admin_client.post(
        "/api/admin/priorities/playbooks",
        json={
            "name": "default",
            "department": None,
            "body": "DEFAULT_BODY",
            "sort_order": 50,
        },
    )
    # Ops-specific row.
    admin_client.post(
        "/api/admin/priorities/playbooks",
        json={
            "name": "ops-vendor",
            "department": "Ops",
            "body": "OPS_BODY",
            "sort_order": 100,
        },
    )
    res = admin_client.get(
        "/api/admin/priorities/playbooks/effective", params={"department": "Ops"}
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["department"] == "Ops"
    # Dept rows come BEFORE default rows.
    ordered_names = [p["name"] for p in body["playbooks"]]
    assert ordered_names == ["ops-vendor", "default"]
    assert "OPS_BODY" in body["concatenated_text"]
    assert "DEFAULT_BODY" in body["concatenated_text"]
    # OPS appears before DEFAULT in the concatenated text.
    assert body["concatenated_text"].index("OPS_BODY") < body[
        "concatenated_text"
    ].index("DEFAULT_BODY")


def test_effective_respects_sort_order_within_dept(admin_client, store):
    admin_client.post(
        "/api/admin/priorities/playbooks",
        json={
            "name": "low-order",
            "department": "Sales",
            "body": "LOW",
            "sort_order": 10,
        },
    )
    admin_client.post(
        "/api/admin/priorities/playbooks",
        json={
            "name": "high-order",
            "department": "Sales",
            "body": "HIGH",
            "sort_order": 200,
        },
    )
    res = admin_client.get(
        "/api/admin/priorities/playbooks/effective", params={"department": "Sales"}
    )
    names = [p["name"] for p in res.json()["playbooks"]]
    assert names == ["low-order", "high-order"]


def test_inactive_playbooks_excluded_from_effective(admin_client, store):
    admin_client.post(
        "/api/admin/priorities/playbooks",
        json={
            "name": "active",
            "department": "Marketing",
            "body": "ON",
            "is_active": True,
        },
    )
    admin_client.post(
        "/api/admin/priorities/playbooks",
        json={
            "name": "inactive",
            "department": "Marketing",
            "body": "OFF",
            "is_active": False,
        },
    )
    res = admin_client.get(
        "/api/admin/priorities/playbooks/effective", params={"department": "Marketing"}
    )
    body = res.json()
    assert len(body["playbooks"]) == 1
    assert body["playbooks"][0]["name"] == "active"
    assert "OFF" not in body["concatenated_text"]


def test_effective_with_no_match_returns_empty_text(admin_client, store):
    res = admin_client.get(
        "/api/admin/priorities/playbooks/effective",
        params={"department": "DepartmentThatHasNoPlaybooks"},
    )
    assert res.status_code == 200
    assert res.json()["concatenated_text"] == ""


# ── Direct store smoke test (no HTTP layer) ──────────────────────────────────


def test_resolve_effective_returns_playbook_row_objects(store):
    from rubik_core.priorities.playbook_store import create_playbook

    create_playbook(
        name="x",
        body="X",
        department=None,
        actor_email="me@x.example",
        is_active=True,
        sort_order=50,
    )
    eff = resolve_effective(department="Sales")
    # No Sales-specific row but the default-everyone applies.
    assert isinstance(eff.playbooks[0], PlaybookRow)
    assert "X" in eff.concatenated_text

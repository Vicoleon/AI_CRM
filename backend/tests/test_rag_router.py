from __future__ import annotations

"""Tests for /api/rag — RBAC, validation, and happy-path with mocked RagManager."""

import base64
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from backend.models.auth import User
from backend.models.role import ResourcePermissions, Role
from backend.routers.rag import get_rag_manager
from rubik_core.auth.registry import UserRegistry
from rubik_core.auth.session_tokens import mint_session_token
from rubik_core.config import AppMode, settings
from rubik_core.rag import RagDocument, build_registry
from rubik_core.tenancy.context import use_workspace

client = TestClient(app)

# Pretend SQL has these departments — `current_registry()` reads from it.
TEST_DEPARTMENTS = ["Sales", "HR", "Finance"]
TEST_REGISTRY = build_registry(TEST_DEPARTMENTS)


@pytest.fixture(autouse=True)
def setup_test_env(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "CUBIE_SESSION_SECRET", base64.urlsafe_b64encode(bytes(range(32))).decode()
    )
    db_path = tmp_path / "test_rag.db"
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    from rubik_core.database.db import engine
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)

    dam_dir = tmp_path / "dam_data"
    dam_dir.mkdir()
    settings.DAM_DATA_DIR = str(dam_dir)

    # Force GCP mode so the get_rag_manager dependency doesn't bail at the
    # gate — we substitute a mock manager so no real Vertex/GCS calls happen.
    prev_mode = settings.APP_MODE
    settings.APP_MODE = AppMode.GCP

    user_reg = UserRegistry()
    user_reg.add_role(
        Role(
            name="viewer",
            permissions={
                "assets": ResourcePermissions(
                    create=False, read=True, update=False, delete=False
                )
            },
        )
    )
    user_reg.add_role(
        Role(
            name="editor",
            permissions={
                "assets": ResourcePermissions(
                    create=True, read=True, update=True, delete=False
                )
            },
        )
    )

    yield

    settings.APP_MODE = prev_mode
    app.dependency_overrides.clear()
    client.headers.pop("Authorization", None)


def _login_as(user: User, *, registry=TEST_REGISTRY) -> MagicMock:
    """Override the auth dependency and return a fresh mock RagManager
    configured with a known test registry."""
    with use_workspace("test"):
        token, _, _ = mint_session_token(user.email, "test", hours=1.0)
    client.headers["Authorization"] = f"Bearer {token}"
    app.dependency_overrides[get_current_user] = lambda: user
    mock_mgr = MagicMock()
    mock_mgr.registry = registry
    app.dependency_overrides[get_rag_manager] = lambda: mock_mgr
    return mock_mgr


def _patch_current_registry():
    """Patch the dynamic registry resolver used by GET /api/rag (no GCP path)."""
    return patch(
        "backend.routers.rag.current_registry",
        return_value=TEST_REGISTRY,
    )


# ── /api/rag (registry view) ────────────────────────────────────────────────


def test_list_rags_returns_companywide_plus_departments():
    UserRegistry().register_user(
        email="v@example.com", name="V", id="v1", roles=["viewer"]
    )
    _login_as(
        User(
            id="v1",
            email="v@example.com",
            name="V",
            roles=["viewer"],
            department="Sales",
        )
    )

    with _patch_current_registry():
        resp = client.get("/api/rag")
    assert resp.status_code == 200
    slugs = {r["slug"] for r in resp.json()}
    assert slugs == {"companywide", "sales", "hr", "finance"}


def test_list_rags_denied_without_read_permission():
    UserRegistry().register_user(
        email="nobody@example.com", name="N", id="n1", roles=[]
    )
    _login_as(
        User(
            id="n1", email="nobody@example.com", name="N", roles=[], department="Sales"
        )
    )

    with _patch_current_registry():
        resp = client.get("/api/rag")
    assert resp.status_code == 403


# ── exec-only (VTO) gating ─────────────────────────────────────────────────


def test_list_rags_hides_vto_from_non_exec():
    """The confidential VTO corpus must not appear in the registry list for a
    non-exec user, even one with assets:read."""
    UserRegistry().register_user(
        email="v2@example.com", name="V2", id="v2", roles=["viewer"]
    )
    _login_as(
        User(
            id="v2",
            email="v2@example.com",
            name="V2",
            roles=["viewer"],
            department="Sales",
        )
    )
    with _patch_current_registry():
        resp = client.get("/api/rag")
    assert resp.status_code == 200
    assert "vto" not in {r["slug"] for r in resp.json()}


def test_list_rags_shows_vto_to_admin():
    UserRegistry().register_user(
        email="adm@example.com", name="Adm", id="adm", roles=["admin"]
    )
    _login_as(
        User(
            id="adm",
            email="adm@example.com",
            name="Adm",
            roles=["admin"],
            department="Sales",
        )
    )
    with _patch_current_registry():
        resp = client.get("/api/rag")
    assert resp.status_code == 200
    assert "vto" in {r["slug"] for r in resp.json()}


def test_vto_documents_404_for_non_exec():
    """Listing the VTO corpus's documents 404s for a non-exec user, hiding its
    existence (rather than 403, which would confirm it exists)."""
    UserRegistry().register_user(
        email="v3@example.com", name="V3", id="v3", roles=["viewer"]
    )
    _login_as(
        User(
            id="v3",
            email="v3@example.com",
            name="V3",
            roles=["viewer"],
            department="Sales",
        )
    )
    resp = client.get("/api/rag/vto/documents")
    assert resp.status_code == 404


def test_vto_documents_visible_to_admin():
    UserRegistry().register_user(
        email="adm2@example.com", name="Adm2", id="adm2", roles=["admin"]
    )
    mock_mgr = _login_as(
        User(
            id="adm2",
            email="adm2@example.com",
            name="Adm2",
            roles=["admin"],
            department="Sales",
        )
    )
    mock_mgr.list_documents.return_value = []
    resp = client.get("/api/rag/vto/documents")
    assert resp.status_code == 200


# ── unknown slug ────────────────────────────────────────────────────────────


def test_unknown_rag_returns_404():
    UserRegistry().register_user(
        email="v@example.com", name="V", id="v1", roles=["viewer"]
    )
    _login_as(
        User(
            id="v1",
            email="v@example.com",
            name="V",
            roles=["viewer"],
            department="Sales",
        )
    )

    resp = client.get("/api/rag/imaginary/documents")
    assert resp.status_code == 404
    assert "imaginary" in resp.json()["detail"]


# ── list_documents happy path ───────────────────────────────────────────────


def test_list_documents_returns_manager_output():
    UserRegistry().register_user(
        email="v@example.com", name="V", id="v1", roles=["viewer"]
    )
    mock_mgr = _login_as(
        User(
            id="v1",
            email="v@example.com",
            name="V",
            roles=["viewer"],
            department="Sales",
        )
    )
    mock_mgr.list_documents.return_value = [
        RagDocument(
            rag_file_name="projects/p/locations/l/ragCorpora/c/ragFiles/123",
            display_name="thing.md",
            gcs_uri="gs://bucket/workspace/rags/sales/thing.md",
            create_time="2026-04-01T00:00:00Z",
        )
    ]

    resp = client.get("/api/rag/sales/documents")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["display_name"] == "thing.md"
    mock_mgr.list_documents.assert_called_once_with("sales")


# ── add document RBAC + happy path ──────────────────────────────────────────


def test_add_document_denied_for_viewer():
    UserRegistry().register_user(
        email="v@example.com", name="V", id="v1", roles=["viewer"]
    )
    _login_as(
        User(
            id="v1",
            email="v@example.com",
            name="V",
            roles=["viewer"],
            department="Sales",
        )
    )

    resp = client.post(
        "/api/rag/sales/documents",
        files={"file": ("hi.md", b"hello", "text/markdown")},
    )
    assert resp.status_code == 403


def test_add_document_allowed_for_editor():
    UserRegistry().register_user(
        email="e@example.com", name="E", id="e1", roles=["editor"]
    )
    mock_mgr = _login_as(
        User(
            id="e1",
            email="e@example.com",
            name="E",
            roles=["editor"],
            department="Sales",
        )
    )
    mock_mgr.add_document.return_value = {
        "rag": "sales",
        "gcs_uri": "gs://bucket/workspace/rags/sales/hi.md",
        "corpus_name": "projects/p/locations/l/ragCorpora/c",
    }

    resp = client.post(
        "/api/rag/sales/documents",
        files={"file": ("hi.md", b"hello", "text/markdown")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"
    assert resp.json()["rag"] == "sales"
    mock_mgr.add_document.assert_called_once()


# ── delete: admin-only and requires identifier ──────────────────────────────


def test_delete_requires_admin():
    UserRegistry().register_user(
        email="e@example.com", name="E", id="e1", roles=["editor"]
    )
    _login_as(
        User(
            id="e1",
            email="e@example.com",
            name="E",
            roles=["editor"],
            department="Sales",
        )
    )

    resp = client.delete("/api/rag/sales/documents?gcs_uri=gs://b/x")
    assert resp.status_code == 403


def test_delete_requires_identifier():
    admin = User(
        id="a1",
        email="admin@example.com",
        name="Admin",
        roles=["admin"],
        department="Sales",
    )
    _login_as(admin)

    resp = client.delete("/api/rag/sales/documents")
    assert resp.status_code == 400


def test_delete_calls_manager_with_uri():
    admin = User(
        id="a1",
        email="admin@example.com",
        name="Admin",
        roles=["admin"],
        department="Sales",
    )
    mock_mgr = _login_as(admin)
    mock_mgr.delete_document.return_value = {"rag_deleted": True, "gcs_deleted": True}

    resp = client.delete(
        "/api/rag/sales/documents?gcs_uri=gs://b/workspace/rags/sales/hi.md"
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "rag": "sales",
        "rag_deleted": True,
        "gcs_deleted": True,
    }
    mock_mgr.delete_document.assert_called_once()

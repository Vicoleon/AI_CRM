"""Tests for DAM version history (zc066): /api/dam/{doc_id}/versions.

Local mode throughout (Chroma/Vertex never touched — the vector purge is
mocked); the ingestion processor is a MagicMock, as in test_dam_processor.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from backend.models.auth import User
from backend.models.role import ResourcePermissions, Role
from backend.routers.dam import FileProcessor, get_file_processor
from backend.routers.ingest import get_processor
from rubik_core.auth.registry import UserRegistry
from rubik_core.config import AppMode, settings

client = TestClient(app)

UPLOAD_META = {"topic": "test", "department": "Engineering", "is_company_wide": "false"}


@pytest.fixture(autouse=True)
def setup_test_env(tmp_path, monkeypatch):
    db_path = tmp_path / "test_dam_versions.db"
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    settings.CLOUDSQL_CONNECTION_NAME = None
    settings.DB_USER = None
    settings.LOCAL_DATA_DIR = str(tmp_path / "data")
    settings.DAM_DATA_DIR = str(tmp_path / "dam_data")
    settings.APP_MODE = AppMode.LOCAL
    # No project → summarize_changes takes the diff heuristic, not Gemini.
    # monkeypatch (not assignment) so it is restored: the sibling suites run
    # alphabetically after this file and several of them read GCP_PROJECT_ID.
    monkeypatch.setattr(settings, "GCP_PROJECT_ID", None)
    Path(settings.LOCAL_DATA_DIR).mkdir(parents=True, exist_ok=True)
    Path(settings.DAM_DATA_DIR).mkdir(parents=True, exist_ok=True)

    import importlib

    from rubik_core.database import db

    importlib.reload(db)
    from rubik_core.database import repository

    importlib.reload(repository)
    from rubik_core.auth import registry

    importlib.reload(registry)

    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=db.engine)

    user_reg = UserRegistry()
    user_reg.add_role(
        Role(
            name="editor",
            permissions={
                "assets": ResourcePermissions(
                    create=True, read=True, update=True, delete=True
                )
            },
        )
    )
    user_reg.add_role(
        Role(
            name="viewer",
            permissions={"assets": ResourcePermissions(read=True)},
        )
    )
    from rubik_core.database.repository import SQLMetadataRegistry

    metareg = SQLMetadataRegistry()
    for d in ["Sales", "Engineering"]:
        metareg.add_department(d)

    # Chroma is never touched: the purge is a MagicMock we can assert on.
    purge = MagicMock(return_value=True)
    monkeypatch.setattr("backend.routers.dam.delete_document_vectors", purge)
    monkeypatch.setattr("backend.routers.assets.delete_document_vectors", purge)

    yield
    app.dependency_overrides.clear()


def _user(email: str, role: str, uid: str) -> User:
    UserRegistry().register_user(email=email, name=email, id=uid, roles=[role])
    return User(id=uid, email=email, name=email, roles=[role], department="sales")


def _editor_user():
    return _user("editor@example.com", "editor", "e1")


def _viewer_user():
    return _user("viewer@example.com", "viewer", "v1")


def _install_mock_processor() -> MagicMock:
    mock_processor = MagicMock()
    mock_processor.process_file.return_value = {
        "result": SimpleNamespace(success=True, count=1, error=None),
        "doc_id": "stub",
    }
    mock_processor.loader = MagicMock()
    mock_processor.loader.generate_doc_id.return_value = "doc-ver-1"
    # The auto-description step reads the file through the loader; give it text.
    mock_processor.loader.load_file.return_value = SimpleNamespace(text="hello")
    fp = FileProcessor(processor=mock_processor)
    fp.get_or_create_corpus_id = MagicMock(return_value=None)
    app.dependency_overrides[get_processor] = lambda: mock_processor
    app.dependency_overrides[get_file_processor] = lambda: fp
    app.dependency_overrides[get_current_user] = _editor_user
    return mock_processor


V1_BYTES = b"Pricing policy 2025. Standard rate is 100 dollars per audit. " * 20
V2_BYTES = b"Pricing policy 2026. Standard rate is 120 dollars per audit. " * 20


def _upload_v1() -> str:
    r = client.post(
        "/api/dam/upload", files={"file": ("pricing.txt", V1_BYTES)}, data=UPLOAD_META
    )
    assert r.status_code == 200, r.text
    return r.json()["doc_id"]


def test_new_version_keeps_doc_id_and_records_history():
    mock_processor = _install_mock_processor()
    doc_id = _upload_v1()

    # History exists before any new version: v1 is captured from the row.
    hist = client.get(f"/api/dam/{doc_id}/versions")
    assert hist.status_code == 200, hist.text
    assert [v["version_number"] for v in hist.json()] == [1]
    assert hist.json()[0]["is_current"] is True

    r = client.post(
        f"/api/dam/{doc_id}/versions",
        files={"file": ("pricing-2026.txt", V2_BYTES)},
        data={"note": "2026 rates"},
    )
    assert r.status_code == 200, r.text
    v2 = r.json()
    assert v2["doc_id"] == doc_id
    assert v2["version_number"] == 2
    assert v2["is_current"] is True
    assert v2["note"] == "2026 rates"
    assert v2["uploaded_by"] == "editor@example.com"

    # The asset itself moved to the new file but kept its identity.
    asset = client.get(f"/api/assets/{doc_id}").json()
    assert asset["doc_id"] == doc_id
    assert asset["current_version"] == 2
    assert asset["original_filename"] == "pricing-2026.txt"
    assert asset["status"] == "approved"
    assert asset["processing"] is False  # TestClient ran the background ingest

    # Both files are still on disk, at distinct paths.
    hist = client.get(f"/api/dam/{doc_id}/versions").json()
    assert [v["version_number"] for v in hist] == [2, 1]
    assert hist[0]["is_current"] and not hist[1]["is_current"]
    assert hist[0]["source_path"] != hist[1]["source_path"]
    from rubik_core.tenancy.storage import assert_own_uri

    assert Path(assert_own_uri(hist[1]["source_path"])).read_bytes() == V1_BYTES
    assert Path(assert_own_uri(hist[0]["source_path"])).read_bytes() == V2_BYTES
    # The "what changed" note was written by the background step (heuristic here).
    assert hist[0]["change_summary"]
    assert "changed" in hist[0]["change_summary"]
    assert hist[1]["change_summary"] is None

    # Ingest ran for v1 and again for v2; the old chunks were purged.
    assert mock_processor.process_file.call_count == 2

    # Every version downloads by number.
    d1 = client.get(f"/api/dam/{doc_id}/versions/1/download")
    assert d1.status_code == 200 and d1.content == V1_BYTES
    assert 'filename="pricing.txt"' in d1.headers["content-disposition"]
    d2 = client.get(f"/api/dam/{doc_id}/versions/2/download")
    assert d2.status_code == 200 and d2.content == V2_BYTES
    # ...and the asset's own download is the current version.
    cur = client.get(f"/api/assets/{doc_id}/download")
    assert cur.status_code == 200 and cur.content == V2_BYTES


def test_identical_bytes_are_not_a_new_version():
    _install_mock_processor()
    doc_id = _upload_v1()
    r = client.post(
        f"/api/dam/{doc_id}/versions", files={"file": ("pricing-copy.txt", V1_BYTES)}
    )
    assert r.status_code == 409, r.text
    assert "identical" in r.json()["detail"]
    assert len(client.get(f"/api/dam/{doc_id}/versions").json()) == 1
    # No stray temp file left in the data dir.
    assert not [
        p
        for p in Path(settings.LOCAL_DATA_DIR).iterdir()
        if p.name.startswith(".upload-")
    ]


def test_restore_publishes_a_new_version_from_old_bytes():
    _install_mock_processor()
    doc_id = _upload_v1()
    assert (
        client.post(
            f"/api/dam/{doc_id}/versions",
            files={"file": ("pricing-2026.txt", V2_BYTES)},
        ).status_code
        == 200
    )

    # Restoring the current version is a no-op request.
    assert client.post(f"/api/dam/{doc_id}/versions/2/restore").status_code == 409

    r = client.post(f"/api/dam/{doc_id}/versions/1/restore", json={"note": ""})
    assert r.status_code == 200, r.text
    v3 = r.json()
    assert v3["version_number"] == 3
    assert v3["restored_from"] == 1
    assert v3["note"] == "Restored version 1"
    assert v3["original_filename"] == "pricing.txt"

    asset = client.get(f"/api/assets/{doc_id}").json()
    assert asset["current_version"] == 3
    assert asset["original_filename"] == "pricing.txt"
    assert asset["processing"] is False
    cur = client.get(f"/api/assets/{doc_id}/download")
    assert cur.content == V1_BYTES
    # History is append-only: v1 → v2 → v3.
    assert [
        v["version_number"] for v in client.get(f"/api/dam/{doc_id}/versions").json()
    ] == [3, 2, 1]


def test_readers_see_history_but_cannot_add_versions():
    _install_mock_processor()
    doc_id = _upload_v1()
    app.dependency_overrides[get_current_user] = _viewer_user
    # Same department (the asset is Engineering; viewer is sales) → not readable
    # at all, so give the viewer explicit access first.
    from backend.routers.dam import dam_registry

    dam_registry.update_asset(doc_id, {"allowed_emails": ["viewer@example.com"]})

    assert client.get(f"/api/dam/{doc_id}/versions").status_code == 200
    assert client.get(f"/api/dam/{doc_id}/versions/1/download").status_code == 200
    r = client.post(f"/api/dam/{doc_id}/versions", files={"file": ("x.txt", V2_BYTES)})
    assert r.status_code == 403
    assert client.post(f"/api/dam/{doc_id}/versions/1/restore").status_code == 403


def test_archived_documents_refuse_new_versions():
    _install_mock_processor()
    doc_id = _upload_v1()
    from backend.routers.dam import dam_registry

    dam_registry.update_asset(doc_id, {"status": "archived"})
    r = client.post(f"/api/dam/{doc_id}/versions", files={"file": ("x.txt", V2_BYTES)})
    assert r.status_code == 400
    assert "archived" in r.json()["detail"]


def test_delete_removes_every_versions_file():
    _install_mock_processor()
    doc_id = _upload_v1()
    assert (
        client.post(
            f"/api/dam/{doc_id}/versions",
            files={"file": ("pricing-2026.txt", V2_BYTES)},
        ).status_code
        == 200
    )
    hist = client.get(f"/api/dam/{doc_id}/versions").json()
    from rubik_core.tenancy.storage import assert_own_uri

    old_path = Path(assert_own_uri(hist[1]["source_path"]))
    assert old_path.exists()

    assert client.delete(f"/api/dam/{doc_id}").status_code == 200
    # The superseded file is swept with the asset (the current one goes with
    # the vector-store delete, which is mocked here).
    assert not old_path.exists()
    assert client.get(f"/api/dam/{doc_id}/versions").status_code == 404

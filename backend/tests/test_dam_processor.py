"""Tests for the DAM FileProcessor and the /api/dam/upload deduplication flow.

The Vertex corpus call is mocked so this can run without GCP credentials.
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


@pytest.fixture(autouse=True)
def setup_test_env(tmp_path):
    # Per-test SQLite + data dir. Cloud SQL is force-disabled so the engine
    # picks up the SQLite URL on reload.
    db_path = tmp_path / "test_dam.db"
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    settings.CLOUDSQL_CONNECTION_NAME = None
    settings.DB_USER = None
    settings.LOCAL_DATA_DIR = str(tmp_path / "data")
    settings.DAM_DATA_DIR = str(tmp_path / "dam_data")
    settings.APP_MODE = AppMode.LOCAL  # bypasses Vertex/GCS in dam.py
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
    editor_role = Role(
        name="editor",
        permissions={
            "assets": ResourcePermissions(
                create=True, read=True, update=True, delete=True
            )
        },
    )
    user_reg.add_role(editor_role)

    # Seed the departments table — FileProcessor.resolve_corpus_display_name()
    # uses current_registry() which reads this table on every call.
    from rubik_core.database.repository import SQLMetadataRegistry

    metareg = SQLMetadataRegistry()
    for d in ["Sales", "HR", "Compliance/Audit", "Engineering", "Marketing"]:
        metareg.add_department(d)

    yield
    app.dependency_overrides.clear()


# ───────────────────────── pure-unit tests ─────────────────────────


def test_compute_content_hash_is_deterministic(tmp_path):
    f = tmp_path / "doc.txt"
    f.write_bytes(b"hello world")
    h1 = FileProcessor.compute_content_hash(f)
    h2 = FileProcessor.compute_content_hash(f)
    # SHA-256 of "hello world"
    assert h1 == "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
    assert h1 == h2


def test_compute_content_hash_differs_for_different_bytes(tmp_path):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_bytes(b"alpha")
    b.write_bytes(b"beta")
    assert FileProcessor.compute_content_hash(a) != FileProcessor.compute_content_hash(
        b
    )


def test_resolve_corpus_routing():
    """Routing pulls the corpus display_name from the live UC-1 registry
    (current_registry → departments table). Names are `{slug}_rag`."""
    fp = FileProcessor(processor=MagicMock())
    # Sales → sales_rag (slug-derived from the departments row)
    assert fp.resolve_corpus_display_name("Sales", False) == "sales_rag"
    # Case-insensitive: HR → hr_rag
    assert fp.resolve_corpus_display_name("HR", False) == "hr_rag"
    # Slug normalization: "Compliance/Audit" → compliance_audit_rag
    assert (
        fp.resolve_corpus_display_name("Compliance/Audit", False)
        == "compliance_audit_rag"
    )
    # Company-wide overrides department
    assert fp.resolve_corpus_display_name("Sales", True) == "companywide_rag"
    # Unknown department (not in registry) → None (default fallback)
    assert fp.resolve_corpus_display_name("Astrology", False) is None
    assert fp.resolve_corpus_display_name(None, False) is None


def test_get_or_create_corpus_id_is_noop_in_local_mode():
    settings.APP_MODE = AppMode.LOCAL
    fp = FileProcessor(processor=MagicMock())
    # Even with a display_name, local mode ignores corpus routing.
    assert fp.get_or_create_corpus_id("sales_rag") is None
    assert fp.get_or_create_corpus_id(None) is None


def test_ingest_passes_resolved_corpus_to_processor(tmp_path):
    """FileProcessor.ingest must hand the resolved target_corpus_id down to
    IngestionProcessor.process_file. We assert the mapping by spying on the
    underlying processor — corpus resolution itself is mocked."""
    inner = MagicMock()
    inner.process_file.return_value = {
        "result": SimpleNamespace(success=True, count=1, error=None)
    }

    fp = FileProcessor(processor=inner)
    # Stub corpus resolution so we don't hit Vertex.
    fp.get_or_create_corpus_id = MagicMock(
        side_effect=lambda name: f"corpora/{name}" if name else None
    )

    f = tmp_path / "playbook.txt"
    f.write_bytes(b"sales playbook")

    fp.ingest(
        f, department="Sales", is_company_wide=False, manual_metadata={"owner": "x"}
    )
    _, kwargs = inner.process_file.call_args
    assert kwargs["target_corpus_id"] == "corpora/sales_rag"
    assert kwargs["manual_metadata"]["corpus"] == "sales_rag"

    # Company-wide overrides department.
    fp.ingest(
        f, department="Sales", is_company_wide=True, manual_metadata={"owner": "x"}
    )
    _, kwargs = inner.process_file.call_args
    assert kwargs["target_corpus_id"] == "corpora/companywide_rag"

    # Unknown department → default corpus (None target_corpus_id).
    fp.ingest(
        f, department="Astrology", is_company_wide=False, manual_metadata={"owner": "x"}
    )
    _, kwargs = inner.process_file.call_args
    assert kwargs["target_corpus_id"] is None


# ───────────────────────── upload-route dedup tests ─────────────────────────


def _editor_user():
    user_reg = UserRegistry()
    user_reg.register_user(
        email="editor@example.com", name="Editor", id="e1", roles=["editor"]
    )
    return User(
        id="e1",
        email="editor@example.com",
        name="Editor",
        roles=["editor"],
        department="sales",
    )


def _install_mock_processor() -> MagicMock:
    """Wire a MagicMock IngestionProcessor through the FastAPI dependency graph."""
    mock_processor = MagicMock()
    mock_processor.process_file.return_value = {
        "result": SimpleNamespace(success=True, count=1, error=None),
        "doc_id": "stub",
    }
    mock_processor.loader = MagicMock()
    # Stable doc_id so the second upload finds the registered asset by hash, not by doc_id.
    mock_processor.loader.generate_doc_id.return_value = "doc-abc-123"

    # Build a FileProcessor that uses the mock and skips real corpus calls.
    fp = FileProcessor(processor=mock_processor)
    fp.get_or_create_corpus_id = MagicMock(return_value=None)

    app.dependency_overrides[get_processor] = lambda: mock_processor
    app.dependency_overrides[get_file_processor] = lambda: fp
    app.dependency_overrides[get_current_user] = _editor_user
    return mock_processor


def test_first_upload_returns_processing_status():
    mock_processor = _install_mock_processor()

    files = {"file": ("doc.txt", b"first upload bytes")}
    data = {"topic": "test", "department": "Engineering", "is_company_wide": "false"}
    response = client.post("/api/dam/upload", files=files, data=data)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "processing"
    assert body["doc_id"]
    # Background task ran inside TestClient → process_file got called once.
    assert mock_processor.process_file.call_count == 1


def test_duplicate_upload_is_rejected_by_hash():
    """Re-uploading the same bytes must return 409 — even with a different filename."""
    mock_processor = _install_mock_processor()

    payload = b"identical payload - should dedup on second post"
    data = {"topic": "test", "department": "Engineering", "is_company_wide": "false"}

    first = client.post(
        "/api/dam/upload", files={"file": ("first.txt", payload)}, data=data
    )
    assert first.status_code == 200, first.text
    first_doc_id = first.json()["doc_id"]

    # Same bytes, different filename → still a duplicate.
    second = client.post(
        "/api/dam/upload", files={"file": ("second.txt", payload)}, data=data
    )
    assert second.status_code == 409, second.text
    detail = second.json()["detail"]
    assert detail["existing_doc_id"] == first_doc_id
    # The 409 detail must tell the UI a force-override is available.
    assert detail.get("can_force") is True
    assert detail.get("force_param") == "force"

    # The mock processor should only have been invoked for the first upload.
    assert mock_processor.process_file.call_count == 1


def test_force_upload_bypasses_dedup():
    """force=true lets the user explicitly ingest a new version of an existing
    file. Both records co-exist (same content_hash, different doc_ids)."""
    mock_processor = _install_mock_processor()

    payload = b"new version of the same content"
    data = {"topic": "test", "department": "Engineering", "is_company_wide": "false"}

    first = client.post(
        "/api/dam/upload", files={"file": ("v1.txt", payload)}, data=data
    )
    assert first.status_code == 200, first.text

    # Without force: 409.
    rej = client.post("/api/dam/upload", files={"file": ("v2.txt", payload)}, data=data)
    assert rej.status_code == 409

    # Bump the doc_id stub so the second register doesn't collide on PK.
    mock_processor.loader.generate_doc_id.return_value = "doc-abc-456"

    # With force: 200, processed.
    forced = client.post(
        "/api/dam/upload",
        files={"file": ("v2.txt", payload)},
        data={**data, "force": "true"},
    )
    assert forced.status_code == 200, forced.text
    assert forced.json()["doc_id"] != first.json()["doc_id"]

    # The processor was invoked twice in total (first + forced; the rejected one was a 409 before processing).
    assert mock_processor.process_file.call_count == 2


def test_status_endpoint_reports_ready_after_processing():
    """After the background task flips processing=False, /status returns 'ready'."""
    _install_mock_processor()

    files = {"file": ("status-check.txt", b"hello status")}
    data = {"topic": "test", "department": "Engineering", "is_company_wide": "false"}

    upload = client.post("/api/dam/upload", files=files, data=data)
    assert upload.status_code == 200
    doc_id = upload.json()["doc_id"]

    # TestClient runs background tasks before the response is delivered to the client,
    # so by now processing should already be False.
    status_resp = client.get(f"/api/dam/{doc_id}/status")
    assert status_resp.status_code == 200, status_resp.text
    body = status_resp.json()
    assert body["status"] == "ready"
    assert body["doc_id"] == doc_id

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from backend.models.auth import User
from rubik_core.config import settings
from rubik_core.database.schemas import AssetMetadata

client = TestClient(app)


def get_mock_user(email="test@example.com", roles=None):
    return User(
        id="test-id",
        email=email,
        name="Test User",
        roles=roles or [],
        department="sales",
    )


@pytest.fixture(autouse=True)
def setup_db(tmp_path):
    # Use a temporary database for tests
    db_path = tmp_path / "test_dam.db"
    settings.DATABASE_URL = f"sqlite:///{db_path}"

    # Create tables
    from rubik_core.database.db import engine
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)

    yield

    # Cleanup overrides if needed (though pytest-mock usually handles things, settings is global)


def test_audit_log_ingest_success():
    app.dependency_overrides[get_current_user] = lambda: get_mock_user(roles=["editor"])
    files = {"file": ("test.txt", b"content")}
    data = {"topic": "test", "department": "Engineering"}

    from backend.routers.ingest import get_processor

    mock_processor = MagicMock()
    mock_processor.process_file.return_value = {
        "result": MagicMock(success=True, count=1),
        "doc_id": "doc123",
    }
    mock_processor.loader = MagicMock()
    mock_processor.loader.generate_doc_id.return_value = "doc123"

    app.dependency_overrides[get_processor] = lambda: mock_processor

    with (
        patch("backend.routers.ingest.has_permission", return_value=True),
        patch("backend.routers.ingest.dam_registry.register_asset"),
    ):
        response = client.post("/api/ingest/", files=files, data=data)
        assert response.status_code == 200

        # Check audit db
        from rubik_core.database.db import SessionLocal
        from rubik_core.database.models import AuditLog

        db = SessionLocal()
        log = (
            db.query(AuditLog)
            .filter_by(action="ingest", actor_email="test@example.com")
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert log is not None
        assert log.actor_email == "test@example.com"
        assert log.action == "ingest"
        # We accept any doc_id returned by the processor
        assert log.doc_id is not None
        assert log.status == "success"
        db.close()

    app.dependency_overrides = {}


def test_audit_log_delete_success():
    app.dependency_overrides[get_current_user] = lambda: get_mock_user(roles=["admin"])
    doc_id = "doc123"

    mock_asset = AssetMetadata(
        doc_id=doc_id,
        owner="test@example.com",
        topic="test",
        department="sales",
        status="draft",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )

    with (
        patch("backend.routers.assets.dam_registry.get_asset", return_value=mock_asset),
        patch("backend.routers.assets.dam_registry.delete_asset", return_value=True),
    ):
        response = client.delete(f"/api/assets/{doc_id}")
        assert response.status_code == 200

        from rubik_core.database.db import SessionLocal
        from rubik_core.database.models import AuditLog

        db = SessionLocal()
        log = (
            db.query(AuditLog)
            .filter_by(action="delete", status="success", doc_id=doc_id)
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert log is not None
        assert log.doc_id == doc_id
        db.close()

    app.dependency_overrides = {}


def test_audit_log_delete_failure_vector_store():
    app.dependency_overrides[get_current_user] = lambda: get_mock_user(roles=["admin"])
    doc_id = "doc123"

    mock_asset = AssetMetadata(
        doc_id=doc_id,
        owner="test@example.com",
        topic="test",
        department="sales",
        status="draft",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )

    with patch(
        "backend.routers.assets.dam_registry.get_asset", return_value=mock_asset
    ):
        response = client.delete(f"/api/assets/{doc_id}")
        assert response.status_code == 500
        # The error comes from the vector/storage delete or the registry delete.
        assert "Failed to delete" in response.json()["detail"]

        from rubik_core.database.db import SessionLocal
        from rubik_core.database.models import AuditLog

        db = SessionLocal()
        log = (
            db.query(AuditLog)
            .filter_by(action="delete", status="failure", doc_id=doc_id)
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert log is not None
        # Either Vector store or Registry failure
        assert "Vector search down" in str(log.details) or "Failed to delete" in str(
            log.details
        )
        db.close()

    app.dependency_overrides = {}

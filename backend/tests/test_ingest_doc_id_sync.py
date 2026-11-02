from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from backend.models.auth import User
from rubik_core.config import settings

client = TestClient(app)


# Mock user dependency
def mock_get_current_user():
    return User(
        id="test-user",
        email="test@acme.example",
        name="Test User",
        department="hr",
        roles=["admin"],
    )


@pytest.fixture(autouse=True)
def setup_db(tmp_path):
    # Use a temporary database for tests
    db_path = tmp_path / "test_ingest.db"
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    settings.CLOUDSQL_CONNECTION_NAME = None
    settings.DB_USER = None

    import importlib

    from rubik_core.database import db

    importlib.reload(db)
    from rubik_core.database import repository

    importlib.reload(repository)
    from rubik_core.auth import registry as auth_registry

    importlib.reload(auth_registry)

    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=db.engine)

    # Register the admin user in the DB so has_permission works
    from rubik_core.auth.registry import UserRegistry

    reg = UserRegistry()
    reg.register_user(
        email="test@acme.example", name="Test User", id="test-user", roles=["admin"]
    )

    yield


@pytest.fixture(autouse=True)
def setup_overrides():
    app.dependency_overrides[get_current_user] = mock_get_current_user
    yield
    app.dependency_overrides.clear()


def test_ingest_doc_id_sync_and_delete():
    # 1. Prepare a test file
    test_file_path = Path("test_upload.txt")
    test_file_path.write_text("This is a test document for doc_id synchronization.")

    try:
        # 2. Mock the embeddings
        with patch(
            "rubik_core.ingestion.processor.get_embeddings"
        ) as mock_embed_factory:
            mock_emb = MagicMock()
            mock_emb.embed_documents.return_value = [[0.1] * 768]
            mock_embed_factory.return_value = mock_emb

            # 3. Call the ingest endpoint
            with open(test_file_path, "rb") as f:
                response = client.post(
                    "/api/ingest/",
                    files={"file": ("test_upload.txt", f, "text/plain")},
                    data={"topic": "Testing", "department": "hr", "status": "draft"},
                )

            assert response.status_code == 200
            data = response.json()
            returned_doc_id = data["doc_id"]

            # Verify doc_id is a hash (typical length for sha256 hex is 64)
            assert len(returned_doc_id) == 64

            # 4. Verify registry
            asset_response = client.get(f"/api/assets/{returned_doc_id}")
            assert asset_response.status_code == 200
            asset_data = asset_response.json()
            assert asset_data["doc_id"] == returned_doc_id
            assert asset_data["department"] == "hr"

            # 5. Call DELETE and verify vector store call
            # The delete endpoint instantiates its own adapter (ChromaAdapter in
            # non-GCP mode).
            with patch("rubik_core.adapters.chroma.ChromaAdapter") as mock_adapter_cls:
                mock_adapter = mock_adapter_cls.return_value
                mock_adapter.delete_document.return_value = True

                del_response = client.delete(f"/api/assets/{returned_doc_id}")
                assert del_response.status_code == 200
                # Assert delete_document was called
                mock_adapter.delete_document.assert_called_with(
                    returned_doc_id, source_path=asset_data["source_path"]
                )

    finally:
        if test_file_path.exists():
            test_file_path.unlink()

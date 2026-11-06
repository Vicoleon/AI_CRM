from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from backend.models.auth import User
from backend.models.role import ResourcePermissions, Role
from rubik_core.auth.permissions import KNOWLEDGE_BASE_UPLOAD_DENIED
from rubik_core.auth.registry import UserRegistry
from rubik_core.config import settings

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_test_env(tmp_path):
    # SQL isolation
    db_path = tmp_path / "test_ingest_rbac.db"
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

    # DAM Data Dir isolation
    dam_dir = tmp_path / "dam_data"
    dam_dir.mkdir()
    settings.DAM_DATA_DIR = str(dam_dir)

    # Setup roles
    user_reg = UserRegistry()
    viewer_role = Role(
        name="viewer",
        permissions={
            "assets": ResourcePermissions(
                create=False, read=True, update=False, delete=False
            )
        },
    )
    editor_role = Role(
        name="editor",
        permissions={
            "assets": ResourcePermissions(
                create=True, read=True, update=True, delete=False
            )
        },
    )
    user_reg.add_role(viewer_role)
    user_reg.add_role(editor_role)

    yield
    app.dependency_overrides.clear()


def test_ingest_denied_for_viewer():
    user_reg = UserRegistry()
    user_reg.register_user(
        email="viewer@example.com", name="Viewer", id="v1", roles=["viewer"]
    )

    app.dependency_overrides[get_current_user] = lambda: User(
        id="v1",
        email="viewer@example.com",
        name="Viewer",
        roles=["viewer"],
        department="sales",  # the role check still applies
    )

    files = {"file": ("test.txt", b"content")}
    data = {"topic": "test", "department": "Engineering"}

    response = client.post("/api/ingest/", files=files, data=data)
    assert response.status_code == 403
    # User-facing copy, not jargon: names the capability and the way out.
    assert response.json()["detail"] == KNOWLEDGE_BASE_UPLOAD_DENIED


def test_ingest_allowed_for_editor():
    user_reg = UserRegistry()
    user_reg.register_user(
        email="editor@example.com", name="Editor", id="e1", roles=["editor"]
    )

    app.dependency_overrides[get_current_user] = lambda: User(
        id="e1",
        email="editor@example.com",
        name="Editor",
        roles=["editor"],
        department="sales",  # the role check still applies
    )

    files = {"file": ("test.txt", b"content")}
    data = {"topic": "test", "department": "Engineering"}

    # Mock processor to avoid actual Vector Store calls
    mock_processor = MagicMock()
    mock_processor.process_file.return_value = {
        "result": MagicMock(success=True, count=1),
        "doc_id": "123",
    }
    mock_processor.loader = MagicMock()
    mock_processor.loader.generate_doc_id.return_value = "123"

    from backend.routers.ingest import get_processor

    app.dependency_overrides[get_processor] = lambda: mock_processor

    response = client.post("/api/ingest/", files=files, data=data)
    assert response.status_code == 200
    assert "Ingestion successful" in response.json()["message"]

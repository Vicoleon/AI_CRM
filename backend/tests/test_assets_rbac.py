from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from backend.models.auth import User
from backend.models.role import ResourcePermissions, Role
from rubik_core.auth.registry import UserRegistry
from rubik_core.config import settings
from rubik_core.dam.registry import AssetMetadata, DAMRegistry

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_db(tmp_path):
    # Use a temporary database for tests
    db_path = tmp_path / "test_assets_rbac.db"
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    settings.CLOUDSQL_CONNECTION_NAME = None
    settings.DB_USER = None

    import importlib

    from rubik_core.database import db

    importlib.reload(db)
    from rubik_core.database import repository

    importlib.reload(repository)
    from rubik_core.auth import registry

    importlib.reload(registry)

    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=db.engine)

    yield


# Helper to clear dependency overrides
@pytest.fixture(autouse=True)
def cleanup_overrides():
    yield
    app.dependency_overrides.clear()


def test_rbac_list_assets_department_isolation():
    # 1. Setup users and assets in real DB
    user_reg = UserRegistry()

    user_role = Role(
        name="user",
        permissions={
            "assets": ResourcePermissions(
                create=False, read=True, update=False, delete=False
            )
        },
    )
    user_reg.add_role(user_role)
    user_reg.register_user(
        email="user1@example.com",
        name="User 1",
        id="user1",
        roles=["user"],
        department="hr",
    )
    user_reg.register_user(
        email="user2@example.com",
        name="User 2",
        id="user2",
        roles=["user"],
        department="marketing",
    )

    dam_reg = DAMRegistry()
    dam_reg.register_asset(
        AssetMetadata(
            doc_id="doc1",
            owner="user1@example.com",
            department="systems",
            status="draft",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
    )
    dam_reg.register_asset(
        AssetMetadata(
            doc_id="doc2",
            owner="user2@example.com",
            department="marketing",
            status="draft",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
    )

    # 2. Test user from 'hr' department
    def mock_user_hr():
        return User(
            id="user1",
            email="user1@example.com",
            name="User 1",
            department="systems",
            roles=["user"],
        )

    app.dependency_overrides[get_current_user] = mock_user_hr

    response = client.get("/api/assets/")
    assert response.status_code == 200
    assets = response.json()["assets"]

    # Should see doc1 but NOT doc2 (department isolation)
    assert "doc1" in assets
    assert "doc2" not in assets


def test_rbac_editor_can_update_but_not_delete():
    user_reg = UserRegistry()
    user_reg.register_user(
        email="editor@example.com",
        name="Editor",
        id="editor",
        roles=["editor"],
        department="hr",
    )

    # Editor role needs 'assets.read' and 'assets.update'
    editor_role = Role(
        name="editor",
        permissions={
            "assets": ResourcePermissions(
                create=False, read=True, update=True, delete=False
            )
        },
    )
    user_reg.add_role(editor_role)
    user_reg.register_user(
        email="other@example.com",
        name="Other",
        id="other",
        roles=["user"],
        department="hr",
    )

    dam_reg = DAMRegistry()
    dam_reg.register_asset(
        AssetMetadata(
            doc_id="doc1",
            owner="other@example.com",
            department="systems",
            status="draft",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
    )

    def mock_editor_hr():
        return User(
            id="editor",
            email="editor@example.com",
            name="Editor",
            department="systems",
            roles=["editor"],
        )

    app.dependency_overrides[get_current_user] = mock_editor_hr

    # Try update
    patch_resp = client.patch("/api/assets/doc1", json={"status": "approved"})
    assert patch_resp.status_code == 200

    # Try delete
    del_resp = client.delete("/api/assets/doc1")
    # Should be forbidden because only owner or admin can delete
    assert del_resp.status_code == 403
    assert "Only the owner" in del_resp.json()["detail"]


def test_rbac_admin_full_access():
    user_reg = UserRegistry()
    user_reg.register_user(
        email="admin@example.com",
        name="Admin",
        id="admin",
        roles=["admin"],
        department="it",
    )
    user_reg.register_user(
        email="user1@example.com",
        name="User 1",
        id="user1",
        roles=["user"],
        department="hr",
    )

    dam_reg = DAMRegistry()
    dam_reg.register_asset(
        AssetMetadata(
            doc_id="doc1",
            owner="user1@example.com",
            department="systems",
            status="draft",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
    )

    def mock_admin():
        return User(
            id="admin",
            email="admin@example.com",
            name="Admin",
            department="it",
            roles=["admin"],
        )

    app.dependency_overrides[get_current_user] = mock_admin

    response = client.get("/api/assets/")
    assert response.status_code == 200
    # Admin sees everything
    assert "doc1" in response.json()["assets"]

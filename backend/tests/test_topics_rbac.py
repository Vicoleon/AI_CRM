import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from backend.models.auth import User
from backend.models.role import ResourcePermissions, Role
from rubik_core.auth.registry import UserRegistry
from rubik_core.config import settings

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_test_env(tmp_path):
    # SQL isolation
    db_path = tmp_path / "test_topics_rbac.db"
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    from rubik_core.database.db import engine
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)

    # DAM Data Dir isolation
    dam_dir = tmp_path / "dam_data_topics"
    dam_dir.mkdir()
    settings.DAM_DATA_DIR = str(dam_dir)

    # Setup roles
    user_reg = UserRegistry()
    viewer_role = Role(
        name="viewer",
        permissions={
            "topics": ResourcePermissions(
                create=False, read=True, update=False, delete=False
            )
        },
    )
    editor_role = Role(
        name="editor",
        permissions={
            "topics": ResourcePermissions(
                create=True, read=True, update=True, delete=False
            )
        },
    )
    admin_role = Role(
        name="admin",
        permissions={
            "topics": ResourcePermissions(
                create=True, read=True, update=True, delete=True
            )
        },
    )
    user_reg.add_role(viewer_role)
    user_reg.add_role(editor_role)
    user_reg.add_role(admin_role)

    yield
    app.dependency_overrides.clear()


def test_topics_list_allowed_without_role():
    """Reading the topic taxonomy is open to any authenticated user.

    Topic is a required field in the (non-admin) document Upload modal, so a
    role-less user must still be able to populate the dropdown — mirrors
    ``GET /api/departments/``. Managing topics (add/remove) stays admin-only.
    """
    user_reg = UserRegistry()
    user_reg.register_user(
        email="noroles@example.com", name="No Roles", id="h1", roles=[]
    )  # No roles = no perms, but reads are still allowed

    app.dependency_overrides[get_current_user] = lambda: User(
        id="h1",
        email="noroles@example.com",
        name="No Roles",
        roles=[],
        department="sales",  # any department: the copilot is open to everyone
    )

    response = client.get("/api/topics/")
    assert response.status_code == 200


def test_topics_list_allowed():
    from rubik_core.database.repository import SQLMetadataRegistry

    SQLMetadataRegistry().add_topic("Compliance")

    user_reg = UserRegistry()
    user_reg.register_user(
        email="viewer@example.com", name="Viewer", id="v1", roles=["viewer"]
    )

    app.dependency_overrides[get_current_user] = lambda: User(
        id="v1",
        email="viewer@example.com",
        name="Viewer",
        roles=["viewer"],
        department="sales",  # copilot open to all; role check still applies
    )

    response = client.get("/api/topics/")
    assert response.status_code == 200
    assert "Compliance" in response.json()  # Default topics


def test_topics_create_denied():
    app.dependency_overrides[get_current_user] = lambda: User(
        id="v1",
        email="viewer@example.com",
        name="Viewer",
        roles=["viewer"],
        department="sales",  # copilot open to all; role check still applies
    )

    response = client.post("/api/topics/", json={"topic": "new-topic"})
    assert response.status_code == 403


def test_topics_create_allowed():
    user_reg = UserRegistry()
    user_reg.register_user(
        email="admin@example.com", name="Admin", id="a1", roles=["admin"]
    )

    app.dependency_overrides[get_current_user] = lambda: User(
        id="a1",
        email="admin@example.com",
        name="Admin",
        roles=["admin"],
        department="sales",  # copilot open to all; role check still applies
    )

    import uuid

    topic_name = f"new-topic-{uuid.uuid4()}"
    response = client.post("/api/topics/", json={"topic": topic_name})
    assert response.status_code == 200

    # Verify it exists
    get_resp = client.get("/api/topics/")
    assert topic_name in get_resp.json()


def test_topics_delete_denied_for_editor():
    from rubik_core.database.repository import SQLMetadataRegistry

    SQLMetadataRegistry().add_topic("Compliance")

    user_reg = UserRegistry()
    user_reg.register_user(
        email="editor@example.com", name="Editor", id="e1", roles=["editor"]
    )

    app.dependency_overrides[get_current_user] = lambda: User(
        id="e1",
        email="editor@example.com",
        name="Editor",
        roles=["editor"],
        department="sales",  # copilot open to all; role check still applies
    )

    response = client.delete("/api/topics/Compliance")
    assert response.status_code == 403


def test_topics_delete_allowed_for_admin():
    from rubik_core.database.repository import SQLMetadataRegistry

    SQLMetadataRegistry().add_topic("Compliance")

    user_reg = UserRegistry()
    user_reg.register_user(
        email="admin@example.com", name="Admin", id="a1", roles=["admin"]
    )

    app.dependency_overrides[get_current_user] = lambda: User(
        id="a1",
        email="admin@example.com",
        name="Admin",
        roles=["admin"],
        department="sales",  # copilot open to all; role check still applies
    )

    response = client.delete("/api/topics/Compliance")
    assert response.status_code == 200

    # Verify it's gone
    get_resp = client.get("/api/topics/")
    assert "Compliance" not in get_resp.json()

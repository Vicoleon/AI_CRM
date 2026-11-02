from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from backend.models.role import ResourcePermissions, Role
from rubik_core.auth.registry import UserRegistry
from rubik_core.config import settings
from rubik_core.dam.registry import AssetMetadata, DAMRegistry

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_db(tmp_path):
    # Use a temporary database for tests
    db_path = tmp_path / "test_granular_access.db"

    # Force SQLite for tests and disable Cloud SQL
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    settings.CLOUDSQL_CONNECTION_NAME = None
    settings.DB_USER = None

    # Force re-initialization of db module
    import importlib

    from rubik_core.database import db

    importlib.reload(db)

    # Also reload repositories since they might have captured the engine
    from rubik_core.database import repository

    importlib.reload(repository)

    from rubik_core.auth import registry

    importlib.reload(registry)

    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=db.engine)

    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=db.engine)

    yield


@pytest.fixture(autouse=True)
def cleanup_overrides():
    yield
    app.dependency_overrides.clear()


def test_granular_access_policy_grant():
    user_reg = UserRegistry()
    # Setup roles
    user_role = Role(
        name="user",
        permissions={
            "assets": ResourcePermissions(
                create=True, read=True, update=True, delete=True
            )
        },
    )
    user_reg.add_role(user_role)

    # Register user in 'systems' department
    user_reg.register_user(
        email="user@example.com",
        name="User",
        id="user",
        roles=["user"],
        department="systems",
    )
    # Register HR Admin to satisfy FK constraint
    user_reg.register_user(
        email="hr_admin@example.com",
        name="HR Admin",
        id="hr_admin",
        roles=["user"],
        department="hr",
    )

    dam_reg = DAMRegistry()
    # Asset in HR department
    dam_reg.register_asset(
        AssetMetadata(
            doc_id="hr_doc",
            owner="hr_admin@example.com",
            department="hr",
            topic="benefits",
            status="approved",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
    )

    def mock_user():
        return user_reg.get_user("user@example.com")

    app.dependency_overrides[get_current_user] = mock_user

    # 1. Initially should NOT have access (different department)
    resp = client.get("/api/assets/hr_doc")
    assert resp.status_code == 403

    # 2. Add an explicit policy for this user to access 'hr' department
    # Note: repo/registry adds policies to the DB
    user_reg.add_access_policy(
        user_email="user@example.com", department="hr", can_read=True
    )

    # 3. Now should HAVE access
    resp = client.get("/api/assets/hr_doc")
    assert resp.status_code == 200
    assert resp.json()["doc_id"] == "hr_doc"


def test_granular_access_group_policy_grant():
    user_reg = UserRegistry()
    user_role = Role(
        name="user",
        permissions={
            "assets": ResourcePermissions(
                create=True, read=True, update=True, delete=True
            )
        },
    )
    user_reg.add_role(user_role)

    user_reg.register_user(
        email="user@example.com",
        name="User",
        id="user",
        roles=["user"],
        department="systems",
    )

    # Create a group and add user
    user_reg.register_user(
        email="hr_admin@example.com",
        name="HR Admin",
        id="hr_admin",
        roles=["user"],
        department="hr",
    )
    group = user_reg.add_group(
        name="HR Readers", description="People who can read HR docs"
    )
    user_reg.add_user_to_group(email="user@example.com", group_id=group.id)

    dam_reg = DAMRegistry()
    # Asset in HR department
    dam_reg.register_asset(
        AssetMetadata(
            doc_id="hr_doc_2",
            owner="hr_admin@example.com",
            department="hr",
            topic="payroll",
            status="approved",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
    )

    def mock_user():
        return user_reg.get_user("user@example.com")

    app.dependency_overrides[get_current_user] = mock_user

    # 1. No policy yet
    resp = client.get("/api/assets/hr_doc_2")
    assert resp.status_code == 403

    # 2. Add policy to the GROUP
    user_reg.add_access_policy(group_id=group.id, department="hr", can_read=True)

    # 3. User should now have access via group membership
    resp = client.get("/api/assets/hr_doc_2")
    assert resp.status_code == 200


def test_granular_access_topic_specificity():
    user_reg = UserRegistry()
    user_role = Role(
        name="user",
        permissions={
            "assets": ResourcePermissions(
                create=True, read=True, update=True, delete=True
            )
        },
    )
    user_reg.add_role(user_role)

    user_reg.register_user(
        email="user@example.com",
        name="User",
        id="user",
        roles=["user"],
        department="systems",
    )
    user_reg.register_user(
        email="hr@example.com",
        name="HR User",
        id="hr_user",
        roles=["user"],
        department="hr",
    )

    dam_reg = DAMRegistry()
    dam_reg.register_asset(
        AssetMetadata(
            doc_id="hr_benefits",
            owner="hr@example.com",
            department="hr",
            topic="benefits",
            status="approved",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
    )
    dam_reg.register_asset(
        AssetMetadata(
            doc_id="hr_payroll",
            owner="hr@example.com",
            department="hr",
            topic="payroll",
            status="approved",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
    )

    def mock_user():
        return user_reg.get_user("user@example.com")

    app.dependency_overrides[get_current_user] = mock_user

    # Policy ONLY for 'benefits' topic
    user_reg.add_access_policy(
        user_email="user@example.com", department="hr", topic="benefits", can_read=True
    )

    # Should see benefits
    assert client.get("/api/assets/hr_benefits").status_code == 200
    # Should NOT see payroll
    assert client.get("/api/assets/hr_payroll").status_code == 403


def test_granular_access_list_filtering():
    user_reg = UserRegistry()
    user_role = Role(
        name="user",
        permissions={
            "assets": ResourcePermissions(
                create=True, read=True, update=True, delete=True
            )
        },
    )
    user_reg.add_role(user_role)

    user_reg.register_user(
        email="user@example.com",
        name="User",
        id="user",
        roles=["user"],
        department="systems",
    )
    user_reg.register_user(
        email="admin@example.com",
        name="Admin Account",
        id="admin_acct",
        roles=["user"],
        department="systems",
    )
    user_reg.register_user(
        email="sales@example.com",
        name="Sales User",
        id="sales_user",
        roles=["user"],
        department="sales",
    )

    dam_reg = DAMRegistry()
    dam_reg.register_asset(
        AssetMetadata(
            doc_id="it_doc",
            owner="admin@example.com",
            department="systems",
            status="approved",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
    )
    dam_reg.register_asset(
        AssetMetadata(
            doc_id="sales_doc",
            owner="sales@example.com",
            department="sales",
            status="approved",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
    )

    def mock_user():
        # User is IT department, should see IT docs automatically but not Sales
        return user_reg.get_user("user@example.com")

    app.dependency_overrides[get_current_user] = mock_user

    # Initially sees 1 asset (IT). The list endpoint returns a pagination
    # envelope {"assets": {...}, "total": N, "limit": ..., "offset": ...}.
    resp = client.get("/api/assets/")
    data = resp.json()["assets"]
    assert "it_doc" in data
    assert "sales_doc" not in data

    # Add policy for Sales
    user_reg.add_access_policy(
        user_email="user@example.com", department="sales", can_read=True
    )

    # Now should see both
    resp = client.get("/api/assets/")
    data = resp.json()["assets"]
    assert "it_doc" in data
    assert "sales_doc" in data


def test_granular_access_admin_bypass():
    user_reg = UserRegistry()
    # Setup roles
    admin_role = Role(
        name="admin",
        permissions={
            "assets": ResourcePermissions(
                create=True, read=True, update=True, delete=True
            )
        },
    )
    user_reg.add_role(admin_role)

    # Admin in 'it' department
    user_reg.register_user(
        email="admin@example.com",
        name="Admin",
        id="admin",
        roles=["admin", "user"],
        department="systems",
    )
    user_reg.register_user(
        email="hr@example.com",
        name="HR Staff",
        id="hr_staff",
        roles=["user"],
        department="hr",
    )

    dam_reg = DAMRegistry()
    # Random doc in HR
    dam_reg.register_asset(
        AssetMetadata(
            doc_id="secret_hr",
            owner="hr@example.com",
            department="hr",
            status="approved",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
    )

    def mock_admin():
        return user_reg.get_user("admin@example.com")

    app.dependency_overrides[get_current_user] = mock_admin

    # Admin should see it without any policies
    resp = client.get("/api/assets/secret_hr")
    assert resp.status_code == 200

"""Regression: a Sales user with only the base ``user`` role hit
"Permission denied" *after* filling in the whole upload form.

The `user` role grants ``assets: read`` and nothing else, so uploading has
always required a second, hand-assigned role (``editor``). Moving someone into
a department doesn't grant it, so a promoted user kept seeing an enabled
"Upload a document" button that could only ever 403 on submit.

Covered here:

* ``GET /api/users/me`` publishes the caller's effective permissions, so the UI
  can disable the control up front. This is the load-bearing contract — the
  frontend gate reads ``permissions.assets.create`` and nothing else.
* Those published verdicts agree with ``has_permission``, which is what the
  endpoints enforce. If the two ever drift, the UI lies in one direction or the
  other.
* The upload gate itself still denies / allows the right people, now with
  user-facing copy instead of "cannot upload to DAM".
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from backend.models.role import ResourcePermissions
from backend.models.role import Role as RoleDTO
from rubik_core.config import settings

client = TestClient(app)

# Any department passes the copilot gate on /api/dam/* (it is open to
# everyone), so the permission check under test is the one that decides.
DEPARTMENT = "Sales"


@pytest.fixture(autouse=True)
def setup_db(tmp_path):
    """Fresh sqlite DB + reload of the modules that bind an engine at import."""
    db_path = tmp_path / "test_upload_permission_gate.db"
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    settings.CLOUDSQL_CONNECTION_NAME = None
    settings.DB_USER = None

    from rubik_core.database import db

    importlib.reload(db)
    # Refuse to run against anything but the throwaway sqlite file — a
    # regression in the override above would otherwise write to dev Cloud SQL.
    assert db.engine.dialect.name == "sqlite", f"refusing engine {db.engine.url!r}"

    from rubik_core.database import repository

    importlib.reload(repository)
    from rubik_core.auth import registry as auth_registry

    importlib.reload(auth_registry)
    from rubik_core.auth import permissions as permissions_mod

    importlib.reload(permissions_mod)

    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=db.engine)

    _seed_roles()

    yield
    app.dependency_overrides.clear()


def _seed_roles() -> None:
    """The roles as they exist in the live workspace: ``user`` can read
    assets but not create them; ``editor`` can create."""
    from rubik_core.auth.registry import UserRegistry

    reg = UserRegistry()
    reg.add_role(
        RoleDTO(
            name="user",
            description="",
            permissions={
                "assets": ResourcePermissions(read=True),
                "documents": ResourcePermissions(read=True),
                "topics": ResourcePermissions(read=True),
            },
        )
    )
    reg.add_role(
        RoleDTO(
            name="editor",
            description="",
            permissions={
                "assets": ResourcePermissions(create=True, read=True, update=True),
            },
        )
    )


def _sign_in(email: str, roles: list[str], department: str = DEPARTMENT):
    """Register a user, then authenticate every request as them.

    Overrides with the real ``UserProfile`` the registry returns — the same
    object production hands the routers — so the permission map under test is
    built from a faithful profile.
    """
    from rubik_core.auth.registry import UserRegistry

    reg = UserRegistry()
    reg.register_user(
        email=email,
        name=email.split("@")[0],
        id=email.split("@")[0],
        roles=roles,
        department=department,
    )
    profile = reg.get_user(email)
    assert profile is not None
    app.dependency_overrides[get_current_user] = lambda: profile
    return profile


def test_me_reports_no_upload_permission_for_base_user_role():
    """Harper's exact state: department Sales, roles ['user']."""
    _sign_in("promoted@example.com", ["user"])

    body = client.get("/api/users/me").json()

    assert body["permissions"]["assets"]["create"] is False
    # Read still works — they can browse the knowledge base, just not add to it.
    assert body["permissions"]["assets"]["read"] is True


def test_me_reports_upload_permission_for_editor_role():
    _sign_in("contributor@example.com", ["user", "editor"])

    body = client.get("/api/users/me").json()

    assert body["permissions"]["assets"]["create"] is True


def test_me_reports_every_resource_so_ui_needs_no_none_checks():
    _sign_in("promoted@example.com", ["user"])

    from rubik_core.auth.permissions import RESOURCE_TYPES

    perms = client.get("/api/users/me").json()["permissions"]

    assert set(perms) == set(RESOURCE_TYPES)


def test_me_grants_admin_everything_even_without_permission_rows():
    """``has_permission`` short-circuits for admins, so the published map has
    to as well — otherwise the UI would grey out controls an admin can use."""
    _sign_in("boss@example.com", ["admin"])

    perms = client.get("/api/users/me").json()["permissions"]

    assert all(all(resource.values()) for resource in perms.values()), (
        f"admin should hold every CRUD verdict, got {perms}"
    )


@pytest.mark.parametrize(
    "roles",
    [["user"], ["user", "editor"], ["admin"], []],
)
def test_published_permissions_match_what_endpoints_enforce(roles):
    """The invariant that keeps the UI honest: every verdict published to the
    client equals the verdict ``has_permission`` gives the routers."""
    profile = _sign_in("someone@example.com", roles)

    from rubik_core.auth.permissions import (
        RESOURCE_TYPES,
        has_permission,
        visible_permissions,
    )

    published = visible_permissions(profile)
    for resource in RESOURCE_TYPES:
        for action in ("create", "read", "update", "delete"):
            assert getattr(published[resource], action) == has_permission(
                profile, resource, action
            ), f"{resource}.{action} disagrees for roles={roles}"


def test_upload_denied_with_user_facing_message():
    """The 403 a base-role user gets must say what to do about it — the old
    copy was "Permission denied: cannot upload to DAM"."""
    from rubik_core.auth.permissions import KNOWLEDGE_BASE_UPLOAD_DENIED

    _sign_in("promoted@example.com", ["user"])

    response = client.post(
        "/api/dam/upload",
        files={"file": ("cheat_sheet.pdf", b"content")},
        data={"topic": "SOP", "department": DEPARTMENT},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == KNOWLEDGE_BASE_UPLOAD_DENIED
    assert "DAM" not in response.json()["detail"]


def test_ai_description_denied_with_the_same_message():
    """ "Generate with AI" shares the gate, so it must share the wording."""
    from rubik_core.auth.permissions import KNOWLEDGE_BASE_UPLOAD_DENIED

    _sign_in("promoted@example.com", ["user"])

    response = client.post(
        "/api/dam/describe",
        files={"file": ("cheat_sheet.pdf", b"content")},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == KNOWLEDGE_BASE_UPLOAD_DENIED


def test_upload_gate_passes_for_editor():
    """Sanity check the gate isn't simply closed for everyone: an editor gets
    past it (and on to the real upload work, which this test stops short of)."""
    from rubik_core.auth.permissions import has_permission

    profile = _sign_in("contributor@example.com", ["user", "editor"])

    assert has_permission(profile, "assets", "create") is True

"""Unit tests for ``rubik_core.auth.permissions.has_permission``.

Covers the two coexisting code paths after the v2 ``permissions.action``
column landed:

* Legacy CRUD-boolean path — unchanged from v1.
* v2 fine-grained capability path — namespaced action strings such as
  ``"deal:advance_stage"`` matched against ``Permission.action``.
"""

from __future__ import annotations

import importlib

import pytest

from backend.models.role import ResourcePermissions
from backend.models.role import Role as RoleDTO
from rubik_core.config import settings


@pytest.fixture(autouse=True)
def setup_db(tmp_path):
    """Spin up a fresh sqlite DB and reload the db/registry modules.

    Mirrors the pattern in ``test_granular_access.py`` — settings is a
    module-level singleton, so we mutate it then force a reimport of the
    modules that captured the engine at import time.
    """
    db_path = tmp_path / "test_permissions.db"
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    settings.CLOUDSQL_CONNECTION_NAME = None
    settings.DB_USER = None

    from rubik_core.database import db

    importlib.reload(db)
    from rubik_core.database import repository

    importlib.reload(repository)
    from rubik_core.auth import registry

    importlib.reload(registry)
    from rubik_core.auth import permissions as permissions_mod

    importlib.reload(permissions_mod)

    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=db.engine)

    yield


def _seed_role_with_action(action: str, role_name: str = "sales") -> None:
    """Insert a Permission row carrying a fine-grained ``action`` string.

    Mirrors what a v2 seed migration / policies router would do once the
    feature PRs ship — for now we write directly to the table so we can
    exercise the new code path in isolation.
    """
    from rubik_core.auth.registry import UserRegistry
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.models import Permission

    UserRegistry().add_role(RoleDTO(name=role_name, description="", permissions={}))

    db = SessionLocal()
    try:
        db.add(Permission(role_name=role_name, resource="deal", action=action))
        db.commit()
    finally:
        db.close()


def test_action_permission_grants_when_role_has_matching_action_row():
    """A Permission row with ``action="deal:advance_stage"`` grants the
    capability to any user whose roles include that row's role."""
    from rubik_core.auth.permissions import has_permission
    from rubik_core.auth.registry import UserRegistry

    reg = UserRegistry()
    _seed_role_with_action("deal:advance_stage", role_name="sales")
    reg.register_user(
        email="rep@example.com",
        name="Rep",
        id="rep",
        roles=["sales"],
        department="sales",
    )

    user = reg.get_user("rep@example.com")
    assert user is not None

    assert has_permission(user, "deal", "deal:advance_stage") is True


def test_action_permission_denies_when_no_matching_action_row():
    """A different action on the same resource must NOT match."""
    from rubik_core.auth.permissions import has_permission
    from rubik_core.auth.registry import UserRegistry

    reg = UserRegistry()
    _seed_role_with_action("deal:advance_stage", role_name="sales")
    reg.register_user(
        email="rep@example.com",
        name="Rep",
        id="rep",
        roles=["sales"],
        department="sales",
    )

    assert (
        has_permission(reg.get_user("rep@example.com"), "deal", "deal:write") is False
    )


def test_action_permission_denies_user_without_role():
    """A user whose roles don't include the granting role must not match."""
    from rubik_core.auth.permissions import has_permission
    from rubik_core.auth.registry import UserRegistry

    reg = UserRegistry()
    _seed_role_with_action("deal:advance_stage", role_name="sales")
    reg.register_user(
        email="bystander@example.com",
        name="Bystander",
        id="bystander",
        roles=["user"],
        department="general",
    )

    assert (
        has_permission(
            reg.get_user("bystander@example.com"),
            "deal",
            "deal:advance_stage",
        )
        is False
    )


def test_legacy_crud_permission_path_still_works():
    """The v1 CRUD-boolean path must keep working unchanged when the
    caller passes ``"read" / "create" / "update" / "delete"``."""
    from rubik_core.auth.permissions import has_permission
    from rubik_core.auth.registry import UserRegistry

    reg = UserRegistry()
    reg.add_role(
        RoleDTO(
            name="editor",
            description="",
            permissions={
                "assets": ResourcePermissions(
                    create=True,
                    read=True,
                    update=True,
                    delete=False,
                )
            },
        )
    )
    reg.register_user(
        email="editor@example.com",
        name="Editor",
        id="editor",
        roles=["editor"],
        department="general",
    )

    user = reg.get_user("editor@example.com")
    assert has_permission(user, "assets", "read") is True
    assert has_permission(user, "assets", "create") is True
    assert has_permission(user, "assets", "delete") is False


def test_admin_bypasses_action_check():
    """Admins pass every check, including fine-grained actions they were
    never explicitly granted."""
    from rubik_core.auth.permissions import has_permission
    from rubik_core.auth.registry import UserRegistry

    reg = UserRegistry()
    reg.register_user(
        email="admin@example.com",
        name="Admin",
        id="admin",
        roles=["admin"],
        department="general",
    )

    assert (
        has_permission(reg.get_user("admin@example.com"), "deal", "deal:advance_stage")
        is True
    )

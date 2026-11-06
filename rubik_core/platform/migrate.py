"""Explicit, serialized control and workspace Alembic upgrades.

The service never migrates on boot. A deploy migration Job or an explicit local
CLI calls this module with a named workspace, and a pending provisioning
workspace may supply only a validated database identity, never an arbitrary URL.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import select, text
from sqlalchemy.engine import Connection, Engine

from alembic import command
from rubik_core.config import AppMode, settings
from rubik_core.control.db import control_session
from rubik_core.control.db import get_engine as control_engine
from rubik_core.control.models import (
    ProvisioningStep,
    Workspace,
    WorkspaceConnectionSpec,
)
from rubik_core.database.migration_guard import (
    assert_migration_target_allowed,
)
from rubik_core.tenancy.context import _canonical_slug
from rubik_core.tenancy.registry import (
    _local_url,
    engine_from_connection_spec,
)

ROOT = Path(__file__).resolve().parents[2]
PENDING_IDENTITY_STEP = "create_database"


class UnknownMigrationRevision(RuntimeError):
    """A live DB has a revision absent from this image's Alembic tree."""


def _advisory_key(scope: str) -> int:
    digest = hashlib.sha256(f"cubie:migrate:{scope}".encode()).digest()[:8]
    return int.from_bytes(digest, "big", signed=True)


@contextmanager
def migration_advisory_lock(connection: Connection, scope: str) -> Iterator[None]:
    """Hold a session-level Postgres lock through the committed upgrade.

    SQLite has no cross-process advisory locks, and its own writer lock protects
    local development. A Postgres lock uses a stable signed 64-bit key rather
    than Python's randomized hash().
    """
    if connection.dialect.name != "postgresql":
        yield
        return
    params = {"key": _advisory_key(scope)}
    connection.execute(text("SELECT pg_advisory_lock(:key)"), params)
    if hasattr(connection, "commit"):
        connection.commit()
    try:
        yield
    except BaseException:
        if hasattr(connection, "rollback"):
            connection.rollback()
        raise
    finally:
        try:
            if hasattr(connection, "commit"):
                connection.commit()
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), params)
            if hasattr(connection, "commit"):
                connection.commit()


def _config(
    ini: str, *, workspace: str | None = None, connection: Connection
) -> Config:
    config = Config(str(ROOT / ini))
    config.attributes["connection"] = connection
    if workspace is not None:
        config.attributes["workspace"] = workspace
        config.cmd_opts = SimpleNamespace(x=[f"workspace={workspace}"])
    return config


def _assert_known_revision(connection: Connection, config: Config) -> None:
    script = ScriptDirectory.from_config(config)
    known = {item.revision for item in script.walk_revisions()}
    live = MigrationContext.configure(connection).get_current_heads()
    unknown = sorted(set(live) - known)
    if unknown:
        raise UnknownMigrationRevision(
            "Database revision absent from this image: " + ", ".join(unknown)
        )
    if len(script.get_heads()) != 1:
        raise RuntimeError("Alembic tree must have exactly one head")


def _upgrade(engine: Engine, ini: str, scope: str, workspace: str | None = None) -> str:
    with engine.connect() as connection, migration_advisory_lock(connection, scope):
        config = _config(ini, workspace=workspace, connection=connection)
        _assert_known_revision(connection, config)
        command.upgrade(config, "head")
        # Alembic may join our implicit transaction instead of owning it.
        connection.commit()
        heads = MigrationContext.configure(connection).get_current_heads()
        if len(heads) != 1:
            raise RuntimeError(f"Expected one database revision for {scope}")
        return heads[0]


def migrate_control() -> str:
    """Upgrade platform routing tables before reading registered workspaces."""
    assert_migration_target_allowed(
        command="upgrade",
        cloudsql_connection_name="",
        database_url=settings.CONTROL_DATABASE_URL,
    )
    engine = control_engine()
    return _upgrade(engine, "alembic_control.ini", "control")


def registered_workspaces() -> list[Workspace]:
    """All registered DBs need schema parity, including suspended workspaces."""
    with control_session() as session:
        return list(
            session.scalars(
                select(Workspace)
                .where(Workspace.status.in_(("active", "suspended")))
                .order_by(Workspace.slug)
            )
        )


def _registered_spec(workspace: Workspace) -> WorkspaceConnectionSpec:
    if settings.APP_MODE != AppMode.GCP and not settings.CLOUDSQL_CONNECTION_NAME:
        # The registry uses only the slug to construct a local SQLite path.
        return WorkspaceConnectionSpec("local_db", "local_user", "local-secret-ref")
    if not all(
        isinstance(value, str) and value
        for value in (
            workspace.db_name,
            workspace.db_user,
            workspace.db_password_secret_ref,
        )
    ):
        raise ValueError(f"Workspace {workspace.slug!r} lacks DB identity")
    return WorkspaceConnectionSpec(
        workspace.db_name,
        workspace.db_user,
        workspace.db_password_secret_ref,
    )


def migrate_workspace(
    slug: str, connection_spec: WorkspaceConnectionSpec | None = None
) -> str:
    """Upgrade a registered DB, or a vetted pending DB before registration."""
    slug = _canonical_slug(slug)
    if connection_spec is not None and not isinstance(
        connection_spec, WorkspaceConnectionSpec
    ):
        raise TypeError("Pending migration requires WorkspaceConnectionSpec")
    with control_session() as session:
        workspace = session.get(Workspace, slug)
        if connection_spec is None:
            if workspace is None or workspace.status not in ("active", "suspended"):
                raise ValueError(
                    "Migration needs a registered workspace or explicit spec"
                )
            spec = _registered_spec(workspace)
        else:
            if workspace is not None:
                raise ValueError(
                    "Explicit spec is only for unregistered pending workspaces"
                )
            identity = session.get(ProvisioningStep, (slug, PENDING_IDENTITY_STEP))
            if (
                identity is None
                or identity.status != "complete"
                or (
                    identity.db_name,
                    identity.db_user,
                    identity.db_password_secret_ref,
                )
                != (
                    connection_spec.db_name,
                    connection_spec.db_user,
                    connection_spec.db_password_secret_ref,
                )
            ):
                raise ValueError(
                    "Pending migration spec must match persisted DB identity"
                )
            spec = connection_spec

    # Guard before constructing a Cloud SQL engine: that constructor resolves
    # the secret reference, which a blocked laptop migration must never read.
    assert_migration_target_allowed(
        command="upgrade",
        cloudsql_connection_name=settings.CLOUDSQL_CONNECTION_NAME,
        database_url=(
            settings.DATABASE_URL
            if settings.CLOUDSQL_CONNECTION_NAME or settings.APP_MODE == AppMode.GCP
            else _local_url(slug)
        ),
    )
    engine = engine_from_connection_spec(slug, spec, registered=connection_spec is None)
    try:
        revision = _upgrade(engine, "alembic.ini", f"workspace:{slug}", slug)
    finally:
        engine.dispose()

    with control_session() as session:
        if connection_spec is None:
            row = session.get(Workspace, slug)
            if (
                row is None
                or row.status not in ("active", "suspended")
                or _registered_spec(row) != spec
            ):
                raise RuntimeError("Workspace DB identity changed during migration")
            row.schema_revision = revision
        else:
            identity = session.get(ProvisioningStep, (slug, PENDING_IDENTITY_STEP))
            if (
                identity is None
                or identity.status != "complete"
                or (
                    identity.db_name,
                    identity.db_user,
                    identity.db_password_secret_ref,
                )
                != (spec.db_name, spec.db_user, spec.db_password_secret_ref)
            ):
                raise RuntimeError("Pending DB identity changed during migration")
            step = session.get(ProvisioningStep, (slug, "migrate"))
            if step is None:
                step = ProvisioningStep(workspace_slug=slug, step="migrate")
                session.add(step)
            step.status = "complete"
            step.db_name = spec.db_name
            step.db_user = spec.db_user
            step.db_password_secret_ref = spec.db_password_secret_ref
            step.schema_revision = revision
        session.commit()
    return revision


def migrate_all() -> dict[str, str]:
    """Control first, then each registered workspace in slug order; stop on error."""
    migrate_control()
    revisions: dict[str, str] = {}
    for workspace in registered_workspaces():
        revisions[workspace.slug] = migrate_workspace(workspace.slug)
    return revisions

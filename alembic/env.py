"""Workspace schema migrations; a target is mandatory, never ambient context."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from rubik_core.config import AppMode, settings
from rubik_core.control.models import WorkspaceConnectionSpec
from rubik_core.database.migration_guard import assert_migration_target_allowed
from rubik_core.database.models import Base
from rubik_core.tenancy.context import _canonical_slug
from rubik_core.tenancy.registry import _local_url, engine_from_connection_spec

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata


def _target_workspace() -> str:
    arguments = context.get_x_argument(as_dictionary=True)
    cli_slug = arguments.get("workspace")
    env_slug = os.environ.get("CUBIE_WORKSPACE")
    if cli_slug and env_slug and cli_slug != env_slug:
        raise ValueError("-x workspace and CUBIE_WORKSPACE disagree")
    if "workspace" in arguments and not cli_slug:
        raise ValueError("-x workspace requires a nonempty slug")
    if not cli_slug and not env_slug:
        raise ValueError("Alembic requires -x workspace=<slug> or CUBIE_WORKSPACE")
    return _canonical_slug(cli_slug or env_slug)


def _invoked_command() -> str | None:
    cmd = getattr(getattr(config, "cmd_opts", None), "cmd", None)
    fn = cmd[0] if isinstance(cmd, (tuple, list)) else cmd
    return getattr(fn, "__name__", None)


def _local_test_engine(slug: str):
    # CI's explicit test target has no control row. It still gets its own file,
    # with the path selected by WORKSPACE_DATABASE_DIR, not DATABASE_URL.
    if slug != "test" or settings.APP_MODE == AppMode.GCP:
        return None
    spec = WorkspaceConnectionSpec("local_db", "local_user", "local-secret-ref")
    return engine_from_connection_spec(slug, spec)


def _registered_engine(slug: str):
    from rubik_core.control.repository import get_workspace

    workspace = get_workspace(slug)
    if workspace is None or workspace.status not in ("active", "suspended"):
        raise ValueError(f"Workspace {slug!r} is not registered")
    if settings.APP_MODE == AppMode.GCP or settings.CLOUDSQL_CONNECTION_NAME:
        spec = WorkspaceConnectionSpec(
            workspace.db_name,
            workspace.db_user,
            workspace.db_password_secret_ref,
        )
    else:
        spec = WorkspaceConnectionSpec("local_db", "local_user", "local-secret-ref")
    return engine_from_connection_spec(slug, spec, registered=True)


def run_migrations_offline() -> None:
    slug = _target_workspace()
    if settings.APP_MODE == AppMode.GCP or settings.CLOUDSQL_CONNECTION_NAME:
        raise ValueError("Offline workspace SQL requires a local SQLite target")
    context.configure(
        url=_local_url(slug),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    slug = _target_workspace()
    injected = config.attributes.get("connection")
    if injected is not None:
        if config.attributes.get("workspace") != slug:
            raise ValueError("Injected migration connection has a different workspace")
        connection = injected
        owns_connection = False
    else:
        assert_migration_target_allowed(
            command=_invoked_command(),
            cloudsql_connection_name=settings.CLOUDSQL_CONNECTION_NAME,
            database_url=(
                settings.DATABASE_URL
                if settings.CLOUDSQL_CONNECTION_NAME or settings.APP_MODE == AppMode.GCP
                else _local_url(slug)
            ),
        )
        engine = _local_test_engine(slug) or _registered_engine(slug)
        connection = engine.connect()
        owns_connection = True
    try:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    finally:
        if owns_connection:
            connection.close()
            engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

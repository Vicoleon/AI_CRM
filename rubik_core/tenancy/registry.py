"""Lazy, bounded workspace engine pools, independent of the legacy DB seam."""

from __future__ import annotations

import sys
from collections import OrderedDict
from functools import partial
from pathlib import Path
from threading import RLock
from weakref import WeakValueDictionary

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url

from rubik_core.config import settings
from rubik_core.control.models import WorkspaceConnectionSpec
from rubik_core.control.repository import get_workspace
from rubik_core.tenancy.context import (
    NoWorkspaceContext,
    _canonical_slug,
    current_workspace,
)
from rubik_core.tenancy.platform_secrets import is_production_runtime
from rubik_core.tenancy.secrets import (
    read_pending_secret,
    read_registered_db_password,
)
from rubik_core.tenancy.support_context import current_support_session

_lock = RLock()
_engines: OrderedDict[str, tuple[object, Engine]] = OrderedDict()
# Weak values bound lock bookkeeping to in-flight work, including waiters.
# A held/waited lock remains strongly referenced until its resolution finishes.
_workspace_locks = WeakValueDictionary()


# Tests must inject the remote connector boundary explicitly. Merely changing
# Settings cannot authorize network access during pytest.
_pytest_cloud_stubs = None


def _cloud_dependencies():
    if "pytest" in sys.modules:
        if _pytest_cloud_stubs is None:
            raise RuntimeError("Cloud workspace engines are disabled during pytest")
        # Earlier registry tests passed (secret_reader, connector_factory).
        # Only the connector is used now; password access runs through the
        # exact control-bound accessors, which tests may stub independently.
        return (
            _pytest_cloud_stubs[1]
            if isinstance(_pytest_cloud_stubs, tuple)
            else _pytest_cloud_stubs
        )
    from google.cloud.sql.connector import Connector

    return Connector


def cached_engines() -> tuple[Engine, ...]:
    """Snapshot existing engines for lifecycle/safety checks; never build one."""
    with _lock:
        return tuple(engine for _, engine in _engines.values())


def clear_engines() -> None:
    """Dispose all cached pools at process/test teardown."""
    with _lock:
        entries = list(_engines.values())
        _engines.clear()
        for _, engine in entries:
            engine.dispose()


def _discard(slug: str) -> None:
    entry = _engines.pop(slug, None)
    if entry is not None:
        entry[1].dispose()


def _sqlite_engine(url: str) -> Engine:
    if make_url(url).get_backend_name() != "sqlite":
        raise ValueError("Local and pytest workspace databases must use SQLite")
    return create_engine(url, connect_args={"check_same_thread": False})


def _local_url(slug: str) -> str:
    directory = Path(settings.WORKSPACE_DATABASE_DIR).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{directory / (slug + '.db')}"


def _cloud_engine(
    slug: str, spec: WorkspaceConnectionSpec, *, registered: bool
) -> Engine:
    from google.cloud.sql.connector import IPTypes

    connector_factory = _cloud_dependencies()

    instance = settings.CLOUDSQL_CONNECTION_NAME
    if not instance:
        raise ValueError("Workspace Cloud SQL requires CLOUDSQL_CONNECTION_NAME")

    def current_password() -> str:
        password = (
            read_registered_db_password(slug, spec)
            if registered
            else read_pending_secret(slug, spec)
        )
        if not password:
            raise ValueError("Workspace database password secret is unavailable")
        return password

    # Fail before publishing the pool if its exact control-bound secret is
    # unavailable. Every physical connection checks again to see rotations.
    current_password()
    # Freeze routing here; later Settings edits cannot switch an existing pool
    # to another tenant when it opens a fresh connection.
    user, database = spec.db_user, spec.db_name
    ip_type = IPTypes.PRIVATE if settings.USE_PRIVATE_IP else IPTypes.PUBLIC
    managed_runtime = is_production_runtime()
    connector = None
    connector_lock = RLock()

    def connect():
        nonlocal connector
        with connector_lock:
            password = current_password()
            if connector is None:
                if managed_runtime:
                    from google.auth import compute_engine

                    connector = connector_factory(
                        credentials=compute_engine.Credentials()
                    )
                else:
                    connector = connector_factory()
            return connector.connect(
                instance,
                "pg8000",
                user=user,
                password=password,
                db=database,
                ip_type=ip_type,
            )

    engine = create_engine(
        "postgresql+pg8000://",
        creator=connect,
        pool_size=2,
        max_overflow=2,
        pool_timeout=30,
        pool_recycle=1800,
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "engine_disposed")
    def close_connector(_engine):
        nonlocal connector
        with connector_lock:
            if connector is not None:
                connector.close()
                connector = None

    return engine


def _assert_support_target(slug: str) -> None:
    support = current_support_session()
    if support is None:
        return
    try:
        selected_slug = current_workspace()
    except NoWorkspaceContext:
        selected_slug = None
    if support.workspace_slug != slug or selected_slug != slug:
        raise PermissionError("Support sessions may open only their target workspace")


def engine_from_connection_spec(
    slug: str, spec: WorkspaceConnectionSpec, *, registered: bool = False
) -> Engine:
    """Migration-only pending engine; caller owns disposal, never cached.

    This private provisioning seam accepts vetted DB identity, never a URL or
    plaintext password. Normal application callers must use engine_for().
    """
    slug = _canonical_slug(slug)
    if current_support_session() is not None:
        raise PermissionError("Support sessions cannot use provisioning engines")
    if not isinstance(spec, WorkspaceConnectionSpec):
        raise TypeError("A vetted WorkspaceConnectionSpec is required")
    if settings.CLOUDSQL_CONNECTION_NAME or is_production_runtime():
        return _cloud_engine(slug, spec, registered=registered)
    return _sqlite_engine(_local_url(slug))


def engine_for(slug: str) -> Engine:
    """Resolve a registered active workspace, including on every cache hit."""
    _canonical_slug(slug)
    _assert_support_target(slug)
    with _lock:
        workspace_lock = _workspace_locks.get(slug)
        if workspace_lock is None:
            workspace_lock = RLock()
            _workspace_locks[slug] = workspace_lock
    # A later suspended lookup cannot be overtaken by an earlier active lookup
    # publishing its pool. Only this slug waits through control/secret I/O.
    with workspace_lock:
        return _resolve_engine(slug)


def _resolve_engine(slug: str) -> Engine:
    if slug == "test" and "pytest" in sys.modules:
        identity = settings.DATABASE_URL
        factory = partial(_sqlite_engine, identity)
    else:
        # Control and secret I/O must not serialize unrelated workspaces.
        workspace = get_workspace(slug)
        if workspace is None or workspace.status != "active":
            with _lock:
                _discard(slug)
            raise ValueError(f"Workspace {slug!r} is unknown or inactive")
        if settings.CLOUDSQL_CONNECTION_NAME or is_production_runtime():
            _cloud_dependencies()  # Fail closed even for a cached cloud pool.
            if not all(
                isinstance(value, str) and value
                for value in (
                    workspace.db_name,
                    workspace.db_user,
                    workspace.db_password_secret_ref,
                )
            ):
                raise ValueError("Workspace database identity is incomplete")
            spec = WorkspaceConnectionSpec(
                workspace.db_name, workspace.db_user, workspace.db_password_secret_ref
            )
            identity = (
                spec,
                settings.CLOUDSQL_CONNECTION_NAME,
                settings.USE_PRIVATE_IP,
            )
            factory = partial(_cloud_engine, slug, spec, registered=True)
        else:
            identity = _local_url(slug)
            factory = partial(_sqlite_engine, identity)
    with _lock:
        entry = _engines.get(slug)
        if entry is not None and entry[0] == identity:
            _engines.move_to_end(slug)
            return entry[1]
    candidate = factory()
    with _lock:
        _discard(slug)
        _engines[slug] = (identity, candidate)
        while len(_engines) > max(1, settings.WORKSPACE_ENGINE_CACHE_SIZE):
            _, (_, evicted) = _engines.popitem(last=False)
            evicted.dispose()
        return candidate

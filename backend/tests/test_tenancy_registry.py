"""Workspace engines cannot cross tenant, lifecycle, or pytest boundaries."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import text

from rubik_core.config import settings
from rubik_core.control.models import WorkspaceConnectionSpec
from rubik_core.tenancy import registry


@pytest.fixture(autouse=True)
def registry_setup(monkeypatch, tmp_path):
    registry.clear_engines()
    monkeypatch.setattr(registry, "_pytest_cloud_stubs", None)
    monkeypatch.setattr(settings, "WORKSPACE_DATABASE_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "WORKSPACE_ENGINE_CACHE_SIZE", 8)
    monkeypatch.setattr(
        registry, "get_workspace", lambda slug: SimpleNamespace(status="active")
    )
    yield
    registry.clear_engines()


def test_unknown_and_inactive_workspaces_are_rejected(monkeypatch):
    for row in (
        None,
        SimpleNamespace(status="suspended"),
        SimpleNamespace(status="provisioning"),
    ):
        monkeypatch.setattr(registry, "get_workspace", lambda slug, row=row: row)
        with pytest.raises(ValueError):
            registry.engine_for("acme")
    assert registry.cached_engines() == ()


def test_cached_engine_rechecks_active_row(monkeypatch):
    engine = registry.engine_for("acme")
    monkeypatch.setattr(
        registry, "get_workspace", lambda slug: SimpleNamespace(status="suspended")
    )
    with pytest.raises(ValueError):
        registry.engine_for("acme")
    assert engine not in registry.cached_engines()


def test_support_scope_rejects_cached_foreign_and_uncached_local_pools(tmp_path):
    from rubik_core.tenancy.context import use_workspace
    from rubik_core.tenancy.support_context import SupportScope, use_support_session

    target = registry.engine_for("acme")
    foreign = registry.engine_for("other")
    scope = SupportScope(
        actor="support@example.test",
        sid="live-support-sid",
        workspace_slug="acme",
        workspace_name="Acme",
    )
    with use_workspace("acme"), use_support_session(scope):
        assert registry.engine_for("acme") is target
        with pytest.raises(PermissionError, match="Support"):
            registry.engine_for("other")
        with pytest.raises(PermissionError, match="Support"):
            registry.engine_for("third")
        with pytest.raises(PermissionError, match="Support"):
            registry.engine_from_connection_spec(
                "acme", WorkspaceConnectionSpec("acme", "acme", "ws-acme-db-password")
            )
        with use_workspace("other"):
            with pytest.raises(PermissionError, match="Support"):
                registry.engine_for("acme")
    assert target in registry.cached_engines()
    assert foreign in registry.cached_engines()
    assert not (tmp_path / "third.db").exists()


def test_concurrent_workspace_files_remain_separate():
    def write(slug):
        engine = registry.engine_for(slug)
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE evidence (slug TEXT)"))
            connection.execute(
                text("INSERT INTO evidence VALUES (:slug)"), {"slug": slug}
            )
        return engine

    with ThreadPoolExecutor(max_workers=2) as pool:
        engines = list(pool.map(write, ["acme", "other"]))
    assert engines[0].url != engines[1].url
    for engine, slug in zip(engines, ["acme", "other"]):
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT slug FROM evidence")) == slug


def test_test_workspace_tracks_database_url(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite:///{tmp_path / 'one.db'}")
    first = registry.engine_for("test")
    disposed = Mock(wraps=first.dispose)
    monkeypatch.setattr(first, "dispose", disposed)
    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite:///{tmp_path / 'two.db'}")
    second = registry.engine_for("test")
    assert first is not second
    assert str(second.url) == settings.DATABASE_URL
    disposed.assert_called_once()


def test_lru_eviction_disposes_least_recent_engine(monkeypatch):
    monkeypatch.setattr(settings, "WORKSPACE_ENGINE_CACHE_SIZE", 2)
    first = registry.engine_for("acme")
    second = registry.engine_for("other")
    disposed = Mock(wraps=second.dispose)
    monkeypatch.setattr(second, "dispose", disposed)
    assert registry.engine_for("acme") is first
    third = registry.engine_for("third")
    assert registry.cached_engines() == (first, third)
    disposed.assert_called_once()


@pytest.mark.parametrize("slug", ["../escape", "", "Acme", "a/b"])
def test_invalid_slugs_cannot_choose_local_paths(slug):
    with pytest.raises(ValueError):
        registry.engine_for(slug)


def test_pending_spec_is_not_registered_or_cached():
    spec = WorkspaceConnectionSpec("pending", "pending", "ws-pending-db-password")
    engine = registry.engine_from_connection_spec("pending", spec)
    try:
        assert engine not in registry.cached_engines()
    finally:
        engine.dispose()


def test_cloud_creators_freeze_tenant_credentials_and_close_connectors(monkeypatch):
    from google.cloud.sql import connector as connector_module

    monkeypatch.setattr(settings, "CLOUDSQL_CONNECTION_NAME", "project:region:instance")
    monkeypatch.setattr(
        registry,
        "read_pending_secret",
        lambda slug, spec: f"password-for-{spec.db_password_secret_ref}",
    )
    connector = Mock()
    connector_class = Mock(return_value=connector)
    monkeypatch.setattr(connector_module, "Connector", connector_class)
    monkeypatch.setattr(registry, "_pytest_cloud_stubs", connector_class)
    first = registry.engine_from_connection_spec(
        "acme", WorkspaceConnectionSpec("acme_db", "acme_user", "acme-secret")
    )
    second = registry.engine_from_connection_spec(
        "other", WorkspaceConnectionSpec("other_db", "other_user", "other-secret")
    )
    try:
        monkeypatch.setattr(settings, "CLOUDSQL_CONNECTION_NAME", "changed")
        monkeypatch.setattr(settings, "DB_USER", "unrelated")
        # Invoke SQLAlchemy's supplied creators directly; no DBAPI or network I/O.
        first.pool._creator()
        second.pool._creator()
        calls = connector.connect.call_args_list
        assert calls[0].args == ("project:region:instance", "pg8000")
        assert calls[0].kwargs["user"] == "acme_user"
        assert calls[0].kwargs["db"] == "acme_db"
        assert calls[0].kwargs["password"] == "password-for-acme-secret"
        assert calls[1].kwargs["user"] == "other_user"
        assert calls[1].kwargs["db"] == "other_db"
        assert calls[1].kwargs["password"] == "password-for-other-secret"
        assert first.pool.size() == 2
        assert first.pool._max_overflow == 2
        assert registry.cached_engines() == ()
    finally:
        first.dispose()
        second.dispose()
    assert connector.close.call_count == 2


@pytest.mark.parametrize("marker", ["CLOUD_RUN_JOB", "CLOUD_RUN_EXECUTION"])
def test_cloud_job_connector_uses_metadata_even_with_hostile_adc(monkeypatch, marker):
    from unittest.mock import patch

    from google.cloud.sql import connector as connector_module

    from rubik_core.config import AppMode

    monkeypatch.setattr(settings, "APP_MODE", AppMode.LOCAL)
    monkeypatch.setattr(settings, "CLOUDSQL_CONNECTION_NAME", "project:region:instance")
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("CLOUD_RUN_JOB", raising=False)
    monkeypatch.delenv("CLOUD_RUN_EXECUTION", raising=False)
    monkeypatch.setenv(marker, "cubie-worker")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/hostile-key.json")
    monkeypatch.setattr(registry, "read_pending_secret", lambda slug, spec: "password")
    connector = Mock()
    connector_factory = Mock(return_value=connector)
    monkeypatch.setattr(connector_module, "Connector", connector_factory)
    monkeypatch.setattr(registry, "_pytest_cloud_stubs", connector_factory)
    metadata_credentials = Mock()
    with (
        patch(
            "google.auth.compute_engine.Credentials", return_value=metadata_credentials
        ) as metadata_factory,
        patch(
            "google.auth.default", side_effect=AssertionError("ADC is forbidden")
        ) as adc,
    ):
        engine = registry.engine_from_connection_spec(
            "acme", WorkspaceConnectionSpec("acme_db", "acme_user", "secret")
        )
        try:
            engine.pool._creator()
        finally:
            engine.dispose()

    metadata_factory.assert_called_once_with()
    connector_factory.assert_called_once_with(credentials=metadata_credentials)
    adc.assert_not_called()


@pytest.mark.parametrize("registered", [False, True])
def test_cloud_creator_reloads_exact_password_for_each_physical_connection(
    monkeypatch, registered
):
    from google.cloud.sql import connector as connector_module

    monkeypatch.setattr(settings, "CLOUDSQL_CONNECTION_NAME", "project:region:instance")
    current_password = {"value": "before-rotation"}
    read = Mock(side_effect=lambda slug, spec: current_password["value"])
    monkeypatch.setattr(
        registry,
        "read_registered_db_password" if registered else "read_pending_secret",
        read,
    )
    connector = Mock()
    connector_factory = Mock(return_value=connector)
    monkeypatch.setattr(connector_module, "Connector", connector_factory)
    monkeypatch.setattr(registry, "_pytest_cloud_stubs", connector_factory)
    spec = WorkspaceConnectionSpec("acme_db", "acme_user", "ws-acme-db-password")
    engine = registry.engine_from_connection_spec("acme", spec, registered=registered)
    try:
        engine.pool._creator()
        current_password["value"] = "after-rotation"
        engine.pool._creator()
        passwords = [
            call.kwargs["password"] for call in connector.connect.call_args_list
        ]
        assert passwords == ["before-rotation", "after-rotation"]
        assert read.call_count >= 2
        assert all(call.args == ("acme", spec) for call in read.call_args_list)
        current_password["value"] = None
        with pytest.raises(ValueError, match="secret is unavailable"):
            engine.pool._creator()
        assert connector.connect.call_count == 2
    finally:
        engine.dispose()


def test_missing_cloud_secret_fails_before_connector_creation(monkeypatch):
    from google.cloud.sql import connector as connector_module

    monkeypatch.setattr(settings, "CLOUDSQL_CONNECTION_NAME", "project:region:instance")
    monkeypatch.setattr(registry, "read_pending_secret", lambda slug, spec: None)
    constructor = Mock()
    monkeypatch.setattr(connector_module, "Connector", constructor)
    monkeypatch.setattr(registry, "_pytest_cloud_stubs", constructor)
    with pytest.raises(ValueError, match="secret is unavailable"):
        registry.engine_from_connection_spec(
            "acme", WorkspaceConnectionSpec("acme", "acme", "secret")
        )
    constructor.assert_not_called()


def test_pytest_test_alias_rejects_non_sqlite_url(monkeypatch):
    monkeypatch.setattr(settings, "DATABASE_URL", "postgresql://invalid/unused")
    with pytest.raises(ValueError, match="must use SQLite"):
        registry.engine_for("test")


def test_test_alias_is_not_available_outside_pytest(monkeypatch):
    monkeypatch.setattr(registry, "sys", SimpleNamespace(modules={}))
    monkeypatch.setattr(registry, "get_workspace", lambda slug: None)
    with pytest.raises(ValueError, match="unknown or inactive"):
        registry.engine_for("test")


def test_registry_guard_inspects_without_creating_engine(monkeypatch, request):
    root_config = next(
        plugin
        for plugin in request.config.pluginmanager.get_plugins()
        if hasattr(plugin, "_databases_are_sqlite")
    )

    unsafe = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    with monkeypatch.context() as patch:
        patch.setattr(registry, "cached_engines", lambda: (unsafe,))
        patch.setattr(
            registry,
            "create_engine",
            Mock(side_effect=AssertionError("must not build")),
        )
        ok, description = root_config._databases_are_sqlite()
        assert not ok
        assert "registry engine: postgresql" in description


def test_registered_cloud_engine_reuses_cache_but_revalidates_row(monkeypatch):
    from google.cloud.sql import connector as connector_module

    row = SimpleNamespace(
        status="active", db_name="acme", db_user="acme", db_password_secret_ref="secret"
    )
    lookup = Mock(return_value=row)
    monkeypatch.setattr(registry, "get_workspace", lookup)
    monkeypatch.setattr(settings, "CLOUDSQL_CONNECTION_NAME", "project:region:instance")
    monkeypatch.setattr(
        registry, "read_registered_db_password", lambda slug, spec: "secret-value"
    )
    constructor = Mock()
    monkeypatch.setattr(connector_module, "Connector", constructor)
    monkeypatch.setattr(registry, "_pytest_cloud_stubs", constructor)
    first = registry.engine_for("acme")
    assert registry.engine_for("acme") is first
    assert lookup.call_count == 2
    constructor.assert_not_called()
    registry.clear_engines()


def test_pending_spec_never_admits_unregistered_workspace(monkeypatch):
    engine = registry.engine_from_connection_spec(
        "pending", WorkspaceConnectionSpec("pending", "pending", "secret")
    )
    monkeypatch.setattr(registry, "get_workspace", lambda slug: None)
    try:
        with pytest.raises(ValueError, match="unknown or inactive"):
            registry.engine_for("pending")
        assert registry.cached_engines() == ()
    finally:
        engine.dispose()


def test_real_control_row_revocation_rejects_cached_engine(monkeypatch):
    from rubik_core.control import repository
    from rubik_core.control.db import control_session, get_engine
    from rubik_core.control.models import Base, Workspace

    monkeypatch.setattr(registry, "get_workspace", repository.get_workspace)
    Base.metadata.create_all(get_engine())
    with control_session() as session:
        session.add(Workspace(slug="registered", name="Registered", status="active"))
        session.commit()
    try:
        engine = registry.engine_for("registered")
        with control_session() as session:
            row = session.get(Workspace, "registered")
            row.status = "suspended"
            session.commit()
        with pytest.raises(ValueError, match="unknown or inactive"):
            registry.engine_for("registered")
        assert engine not in registry.cached_engines()
    finally:
        with control_session() as session:
            session.delete(session.get(Workspace, "registered"))
            session.commit()


@pytest.mark.parametrize("pending", [False, True])
def test_pytest_cloud_selection_refuses_before_secret_or_connector(
    monkeypatch, pending
):
    from google.cloud.sql import connector as connector_module

    monkeypatch.setattr(settings, "CLOUDSQL_CONNECTION_NAME", "project:region:instance")
    monkeypatch.setattr(
        registry,
        "get_workspace",
        lambda slug: SimpleNamespace(
            status="active",
            db_name="acme",
            db_user="acme",
            db_password_secret_ref="secret",
        ),
    )
    secret = Mock()
    connector = Mock()
    monkeypatch.setattr(registry, "read_pending_secret", secret)
    monkeypatch.setattr(registry, "read_registered_db_password", secret)
    monkeypatch.setattr(connector_module, "Connector", connector)
    with pytest.raises(RuntimeError, match="pytest"):
        if pending:
            registry.engine_from_connection_spec(
                "acme", WorkspaceConnectionSpec("acme", "acme", "secret")
            )
        else:
            registry.engine_for("acme")
    secret.assert_not_called()
    connector.assert_not_called()


def test_parallel_control_resolution_does_not_hold_registry_lock(monkeypatch):
    from threading import Barrier

    barrier = Barrier(2)

    def lookup(slug):
        barrier.wait(timeout=3)
        return SimpleNamespace(status="active")

    monkeypatch.setattr(registry, "get_workspace", lookup)
    with ThreadPoolExecutor(max_workers=2) as pool:
        # Different workspaces must reach control concurrently.
        engines = list(pool.map(registry.engine_for, ["acme", "other"]))
    assert engines[0] is not engines[1]
    assert set(registry.cached_engines()) == set(engines)


def test_parallel_secret_resolution_does_not_hold_registry_lock(monkeypatch):
    from threading import Barrier

    barrier = Barrier(2)

    def secret(slug, spec):
        barrier.wait(timeout=3)
        return "stubbed-secret"

    monkeypatch.setattr(settings, "CLOUDSQL_CONNECTION_NAME", "project:region:instance")
    monkeypatch.setattr(
        registry,
        "get_workspace",
        lambda slug: SimpleNamespace(
            status="active",
            db_name=slug,
            db_user=slug,
            db_password_secret_ref=f"ws-{slug}-db-password",
        ),
    )
    connector = Mock(side_effect=AssertionError("lazy connector must not be created"))
    monkeypatch.setattr(registry, "read_registered_db_password", secret)
    monkeypatch.setattr(registry, "_pytest_cloud_stubs", connector)
    with ThreadPoolExecutor(max_workers=2) as pool:
        engines = list(pool.map(registry.engine_for, ["acme", "other"]))
    assert engines[0] is not engines[1]
    registry.clear_engines()


def test_backend_registry_teardown_checks_before_disposal(monkeypatch, request):
    plugin = next(
        plugin
        for plugin in request.config.pluginmanager.get_plugins()
        if hasattr(plugin, "_fresh_workspace_registry")
    )
    fixture = plugin._fresh_workspace_registry.__wrapped__()
    next(fixture)
    unsafe = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), dispose=Mock())
    registry._engines["unsafe"] = ("unsafe", unsafe)
    with pytest.raises(AssertionError, match="Non-SQLite workspace registry"):
        next(fixture)
    unsafe.dispose.assert_called_once()
    assert registry.cached_engines() == ()


def test_concurrent_revocation_cannot_be_overwritten_by_stale_active_lookup(
    monkeypatch,
):
    from threading import Event

    active_read = Event()
    release_active = Event()
    revocation_attempted = Event()
    suspended_read = Event()

    def lookup(slug):
        if not active_read.is_set():
            active_read.set()
            assert release_active.wait(timeout=3)
            return SimpleNamespace(status="active")
        suspended_read.set()
        return SimpleNamespace(status="suspended")

    def revoke():
        revocation_attempted.set()
        with pytest.raises(ValueError, match="inactive"):
            registry.engine_for("acme")

    monkeypatch.setattr(registry, "get_workspace", lookup)
    with ThreadPoolExecutor(max_workers=2) as pool:
        active = pool.submit(registry.engine_for, "acme")
        assert active_read.wait(timeout=3)
        revoked = pool.submit(revoke)
        assert revocation_attempted.wait(timeout=3)
        try:
            # The active lookup and its cache insertion must finish before the
            # later suspended lookup can discard that same workspace's pool.
            assert not suspended_read.wait(timeout=0.1)
        finally:
            release_active.set()
        active.result(timeout=3)
        revoked.result(timeout=3)
    assert suspended_read.is_set()
    assert registry.cached_engines() == ()

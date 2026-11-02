"""Tests for the ADK session-service factory (chat-hallucination RCA — Part B2).

Verifies the phantom ``FirestoreSessionService`` is gone, that GCP mode selects
the Cloud SQL ``DatabaseSessionService``, and that a build failure falls back to
InMemory *loudly* and is reported truthfully by ``get_session_backend()``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from rubik_core.config import AppMode, settings
from rubik_core.control.models import WorkspaceConnectionSpec
from rubik_core.orchestration import session_service as ss
from rubik_core.tenancy.context import use_workspace


@pytest.fixture(autouse=True)
def _reset_singleton():
    """The factory memoizes; reset module state around each test."""
    ss._session_services.clear()
    ss._session_backends.clear()
    yield
    ss._session_services.clear()
    ss._session_backends.clear()


def test_firestore_session_service_does_not_exist():
    # Root cause: the old code imported this; it never existed in google-adk
    # 1.30.0, so the GCP branch always silently fell back to InMemory.
    import google.adk.sessions as adk_sessions

    assert not hasattr(adk_sessions, "FirestoreSessionService")


def test_local_uses_in_memory(monkeypatch):
    from google.adk.sessions import InMemorySessionService

    monkeypatch.setattr(settings, "APP_MODE", AppMode.LOCAL)
    monkeypatch.setattr(settings, "CLOUDSQL_CONNECTION_NAME", None)

    svc = ss.get_session_service()
    assert isinstance(svc, InMemorySessionService)
    assert ss.get_session_backend() == "in_memory:local"


def test_gcp_without_cloudsql_uses_in_memory(monkeypatch):
    from google.adk.sessions import InMemorySessionService

    monkeypatch.setattr(settings, "APP_MODE", AppMode.GCP)
    monkeypatch.setattr(settings, "CLOUDSQL_CONNECTION_NAME", None)

    svc = ss.get_session_service()
    assert isinstance(svc, InMemorySessionService)
    assert ss.get_session_backend() == "in_memory:local"


def test_gcp_with_cloudsql_builds_database_backend(monkeypatch):
    monkeypatch.setattr(settings, "APP_MODE", AppMode.GCP)
    monkeypatch.setattr(settings, "CLOUDSQL_CONNECTION_NAME", "proj:region:inst")

    sentinel = object()
    monkeypatch.setattr(ss, "_build_database_session_service", lambda slug: sentinel)

    assert ss.get_session_service() is sentinel
    assert ss.get_session_backend() == "database:cloudsql"


def test_gcp_db_build_failure_falls_back_loudly(monkeypatch, caplog):
    from google.adk.sessions import InMemorySessionService

    monkeypatch.setattr(settings, "APP_MODE", AppMode.GCP)
    monkeypatch.setattr(settings, "CLOUDSQL_CONNECTION_NAME", "proj:region:inst")

    def _boom(_slug):
        raise RuntimeError("no asyncpg / bad creds")

    monkeypatch.setattr(ss, "_build_database_session_service", _boom)

    with caplog.at_level("ERROR"):
        svc = ss.get_session_service()

    assert isinstance(svc, InMemorySessionService)
    assert ss.get_session_backend() == "in_memory:fallback"
    # Fallback must be loud (ERROR), not silent like the old phantom-Firestore path.
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_real_database_service_constructs_without_connecting(monkeypatch):
    # Only runs where the async driver is installed (CI after requirements
    # update). Construction is lazy — it builds the engine but opens no
    # connection — so this is safe without a live Cloud SQL.
    pytest.importorskip("asyncpg")

    monkeypatch.setattr(settings, "APP_MODE", AppMode.GCP)
    monkeypatch.setattr(settings, "CLOUDSQL_CONNECTION_NAME", "proj:region:inst")
    monkeypatch.setattr(
        ss,
        "get_workspace",
        lambda slug: SimpleNamespace(
            status="active",
            db_user="u",
            db_name="d",
            db_password_secret_ref="ws-test-db-password",
        ),
    )
    monkeypatch.setattr(ss, "read_registered_db_password", lambda slug, spec: "p")

    from google.adk.sessions import DatabaseSessionService

    svc = ss.get_session_service()
    assert isinstance(svc, DatabaseSessionService)
    assert ss.get_session_backend() == "database:cloudsql"


def test_local_sessions_are_separate_for_identical_user_and_session_id(monkeypatch):
    monkeypatch.setattr(settings, "APP_MODE", AppMode.LOCAL)
    monkeypatch.setattr(settings, "CLOUDSQL_CONNECTION_NAME", None)
    with use_workspace("alpha"):
        alpha = ss.get_session_service()
    with use_workspace("beta"):
        beta = ss.get_session_service()
    assert alpha is not beta

    async def check():
        await alpha.create_session(
            app_name="cubie", user_id="same-user", session_id="same"
        )
        assert (
            await beta.get_session(
                app_name="cubie", user_id="same-user", session_id="same"
            )
            is None
        )

    asyncio.run(check())


def test_unbound_health_backend_does_not_select_a_tenant(monkeypatch):
    from rubik_core.tenancy import context
    from rubik_core.tenancy.context import NoWorkspaceContext

    monkeypatch.setattr(context, "_process_workspace", None)
    assert ss.get_session_backend() == "workspace_scoped"
    assert ss._session_services == {}
    with pytest.raises(NoWorkspaceContext):
        ss.get_session_service()


def test_cloud_creator_freezes_registered_workspace_db_identity(monkeypatch):
    import google.adk.sessions as adk_sessions
    from google.cloud.sql import connector as cloud_connector

    specs = {
        "alpha": WorkspaceConnectionSpec(
            "alpha_db", "alpha_user", "ws-alpha-db-password"
        ),
        "beta": WorkspaceConnectionSpec("beta_db", "beta_user", "ws-beta-db-password"),
    }
    calls = []
    monkeypatch.setattr(ss, "_registered_spec", lambda slug: specs[slug])
    monkeypatch.setattr(
        ss, "read_registered_db_password", lambda slug, spec: f"{slug}-pass"
    )
    monkeypatch.setattr(settings, "CLOUDSQL_CONNECTION_NAME", "project:region:instance")
    monkeypatch.setattr(settings, "DB_USER", "wrong_global_user")
    monkeypatch.setattr(settings, "DB_NAME", "wrong_global_db")

    async def connector_factory():
        class Connector:
            async def connect_async(self, instance, driver, **kwargs):
                calls.append((instance, driver, kwargs))
                return object()

        return Connector()

    monkeypatch.setattr(cloud_connector, "create_async_connector", connector_factory)
    monkeypatch.setattr(
        adk_sessions,
        "DatabaseSessionService",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )
    alpha = ss._build_database_session_service("alpha")
    beta = ss._build_database_session_service("beta")

    async def connect():
        with use_workspace("beta"):
            await alpha.async_creator()
        with use_workspace("alpha"):
            await beta.async_creator()

    asyncio.run(connect())
    assert [(row[2]["user"], row[2]["db"]) for row in calls] == [
        ("alpha_user", "alpha_db"),
        ("beta_user", "beta_db"),
    ]
    assert all(row[0] == "project:region:instance" for row in calls)

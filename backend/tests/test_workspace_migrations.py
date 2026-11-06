"""Workspace migrations select a target explicitly and serialize each upgrade."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from threading import Event, get_ident
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect, text

from rubik_core.control.models import WorkspaceConnectionSpec

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def local_control(tmp_path, monkeypatch):
    from rubik_core.config import settings
    from rubik_core.control import db as control_db

    monkeypatch.setattr(control_db, "_engine", None)
    monkeypatch.setattr(
        settings, "CONTROL_DATABASE_URL", f"sqlite:///{tmp_path / 'control.db'}"
    )
    monkeypatch.setattr(settings, "WORKSPACE_DATABASE_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "CLOUDSQL_CONNECTION_NAME", "")
    yield tmp_path
    if control_db._engine is not None:
        control_db._engine.dispose()


def test_migrate_all_stops_on_first_failed_workspace(monkeypatch):
    from rubik_core.platform import migrate

    calls = []
    monkeypatch.setattr(migrate, "migrate_control", lambda: calls.append("control"))
    monkeypatch.setattr(
        migrate,
        "registered_workspaces",
        lambda: [SimpleNamespace(slug=slug) for slug in ("a", "b", "c")],
    )

    def upgrade(slug):
        calls.append(slug)
        if slug == "b":
            raise RuntimeError("B failed")
        return f"{slug}-head"

    monkeypatch.setattr(migrate, "migrate_workspace", upgrade)
    with pytest.raises(RuntimeError, match="B failed"):
        migrate.migrate_all()
    assert calls == ["control", "a", "b"]


def test_pending_workspace_migrates_only_with_explicit_spec(local_control):
    from rubik_core.control import db as control_db
    from rubik_core.control.models import ProvisioningStep
    from rubik_core.platform import migrate

    migrate.migrate_control()
    spec = WorkspaceConnectionSpec("acme_db", "acme_user", "local-secret-ref")
    with control_db.control_session() as session:
        session.add(
            ProvisioningStep(
                workspace_slug="acme",
                step=migrate.PENDING_IDENTITY_STEP,
                status="complete",
                db_name=spec.db_name,
                db_user=spec.db_user,
                db_password_secret_ref=spec.db_password_secret_ref,
            )
        )
        session.commit()

    with pytest.raises(ValueError, match="registered|explicit"):
        migrate.migrate_workspace("acme")
    assert migrate.migrate_all() == {}

    revision = migrate.migrate_workspace("acme", connection_spec=spec)
    engine = create_engine(f"sqlite:///{local_control / 'acme.db'}")
    assert "users" in inspect(engine).get_table_names()
    with engine.connect() as conn:
        assert (
            conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            == revision
        )
    with control_db.control_session() as session:
        step = session.get(ProvisioningStep, ("acme", "migrate"))
        assert step is not None
        assert step.status == "complete"
        assert step.schema_revision == revision

    # A retry after a crash before registration reuses the same pending DB.
    assert migrate.migrate_workspace("acme", connection_spec=spec) == revision
    assert migrate.migrate_all() == {}


def test_pending_spec_must_match_persisted_identity(local_control):
    from rubik_core.control import db as control_db
    from rubik_core.control.models import ProvisioningStep
    from rubik_core.platform import migrate

    migrate.migrate_control()
    spec = WorkspaceConnectionSpec("acme_db", "acme_user", "local-secret-ref")
    with pytest.raises(ValueError, match="persisted DB identity"):
        migrate.migrate_workspace("acme", connection_spec=spec)
    with control_db.control_session() as session:
        session.add(
            ProvisioningStep(
                workspace_slug="acme",
                step=migrate.PENDING_IDENTITY_STEP,
                status="complete",
                db_name="other_db",
                db_user="other_user",
                db_password_secret_ref="other-ref",
            )
        )
        session.commit()
    with pytest.raises(ValueError, match="persisted DB identity"):
        migrate.migrate_workspace("acme", connection_spec=spec)
    assert not (local_control / "acme.db").exists()


def test_remote_workspace_guard_runs_before_secret_resolution(
    local_control, monkeypatch
):
    from rubik_core.config import settings
    from rubik_core.control import db as control_db
    from rubik_core.control.models import ProvisioningStep
    from rubik_core.database.migration_guard import RemoteMigrationBlocked
    from rubik_core.platform import migrate

    migrate.migrate_control()
    spec = WorkspaceConnectionSpec("acme_db", "acme_user", "secret-ref")
    with control_db.control_session() as session:
        session.add(
            ProvisioningStep(
                workspace_slug="acme",
                step=migrate.PENDING_IDENTITY_STEP,
                status="complete",
                db_name=spec.db_name,
                db_user=spec.db_user,
                db_password_secret_ref=spec.db_password_secret_ref,
            )
        )
        session.commit()
    monkeypatch.setattr(settings, "CLOUDSQL_CONNECTION_NAME", "project:region:instance")
    monkeypatch.delenv("CLOUD_RUN_JOB", raising=False)
    monkeypatch.delenv("CLOUD_RUN_EXECUTION", raising=False)
    monkeypatch.delenv("ALEMBIC_ALLOW_REMOTE", raising=False)
    with pytest.raises(RemoteMigrationBlocked):
        migrate.migrate_workspace("acme", connection_spec=spec)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+pg8000://user:password@remote.example/control",
        "postgresql+psycopg2://user:password@/control?host=/cloudsql/project:region:instance",
        "postgresql+pg8000://user:password@/control?unix_sock=/cloudsql/project:region:instance/.s.PGSQL.5432",
    ],
)
def test_control_remote_guard_runs_before_engine_creation(monkeypatch, url):
    from rubik_core.config import settings
    from rubik_core.database.migration_guard import RemoteMigrationBlocked
    from rubik_core.platform import migrate

    called = []
    monkeypatch.setattr(settings, "CONTROL_DATABASE_URL", url)
    monkeypatch.setattr(migrate, "control_engine", lambda: called.append(True))
    monkeypatch.delenv("CLOUD_RUN_JOB", raising=False)
    monkeypatch.delenv("CLOUD_RUN_EXECUTION", raising=False)
    monkeypatch.delenv("ALEMBIC_ALLOW_REMOTE", raising=False)
    with pytest.raises(RemoteMigrationBlocked) as excinfo:
        migrate.migrate_control()
    assert called == []
    assert "password" not in str(excinfo.value)
    assert "/cloudsql" not in str(excinfo.value)


def test_migrate_all_updates_active_and_suspended_revisions(local_control):
    from rubik_core.control import db as control_db
    from rubik_core.control.models import Workspace
    from rubik_core.platform import migrate

    migrate.migrate_control()
    with control_db.control_session() as session:
        session.add_all(
            [
                Workspace(slug="acme", name="Acme", status="active"),
                Workspace(slug="paused", name="Paused", status="suspended"),
                Workspace(slug="pending", name="Pending", status="provisioning"),
            ]
        )
        session.commit()
    revisions = migrate.migrate_all()
    assert set(revisions) == {"acme", "paused"}
    assert revisions["acme"] == revisions["paused"]
    assert not (local_control / "pending.db").exists()
    with control_db.control_session() as session:
        assert session.get(Workspace, "acme").schema_revision == revisions["acme"]
        assert session.get(Workspace, "paused").schema_revision == revisions["paused"]


def test_workspace_alembic_requires_named_target(tmp_path):
    env = {
        **os.environ,
        "APP_MODE": "local",
        "DATABASE_URL": f"sqlite:///{tmp_path / 'wrong.db'}",
        "WORKSPACE_DATABASE_DIR": str(tmp_path),
        "CLOUDSQL_CONNECTION_NAME": "",
        "CUBIE_WORKSPACE": "",
        "GCP_PROJECT_ID": "",
        "PYTHONPATH": str(ROOT),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode != 0
    assert "workspace" in proc.stderr.lower()
    assert not (tmp_path / "wrong.db").exists()


def test_sqlite_migration_advisory_lock_is_noop(tmp_path):
    from rubik_core.platform.migrate import migration_advisory_lock

    engine = create_engine(f"sqlite:///{tmp_path / 'local.db'}")
    with engine.connect() as conn, migration_advisory_lock(conn, "acme"):
        conn.execute(text("SELECT 1"))


def test_postgres_migration_advisory_lock_releases_after_failure():
    from rubik_core.platform.migrate import migration_advisory_lock

    class FakeConnection:
        dialect = SimpleNamespace(name="postgresql")

        def __init__(self):
            self.calls = []

        def execute(self, statement, params):
            self.calls.append((str(statement), params))

    connection = FakeConnection()
    with pytest.raises(RuntimeError, match="upgrade failed"):
        with migration_advisory_lock(connection, "acme"):
            raise RuntimeError("upgrade failed")
    assert len(connection.calls) == 2
    assert "pg_advisory_lock" in connection.calls[0][0]
    assert "pg_advisory_unlock" in connection.calls[1][0]
    assert connection.calls[0][1] == connection.calls[1][1]


def test_unknown_workspace_revision_blocks_upgrade(local_control):
    from rubik_core.control import db as control_db
    from rubik_core.control.models import Base, ProvisioningStep
    from rubik_core.platform import migrate

    Base.metadata.create_all(control_db.get_engine())
    with control_db.control_session() as session:
        session.add(
            ProvisioningStep(
                workspace_slug="acme",
                step=migrate.PENDING_IDENTITY_STEP,
                status="complete",
                db_name="acme_db",
                db_user="acme_user",
                db_password_secret_ref="local-secret-ref",
            )
        )
        session.commit()
    engine = create_engine(f"sqlite:///{local_control / 'acme.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        conn.execute(text("INSERT INTO alembic_version VALUES ('unknown_revision')"))

    spec = WorkspaceConnectionSpec("acme_db", "acme_user", "local-secret-ref")
    with pytest.raises(RuntimeError, match="unknown_revision"):
        migrate.migrate_workspace("acme", connection_spec=spec)
    with engine.connect() as conn:
        assert (
            conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            == "unknown_revision"
        )


def test_preflight_reads_explicit_slug_inside_worker(tmp_path, monkeypatch):
    from rubik_core.database import migration_preflight as pf

    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    seen = []
    caller = get_ident()

    def select_engine(slug):
        seen.append((slug, get_ident()))
        return engine

    monkeypatch.setattr(pf, "_preflight_engine", select_engine)
    assert pf.preflight_workspace("acme").status == "fresh"
    assert len(seen) == 1
    assert seen[0][0] == "acme"
    assert seen[0][1] != caller


def test_preflight_timeout_does_not_wait_for_hung_connector(monkeypatch):
    from rubik_core.database import migration_preflight as pf

    release = Event()

    def hung_engine(_slug):
        release.wait(5)
        raise OSError("unreachable")

    monkeypatch.setattr(pf, "_preflight_engine", hung_engine)
    try:
        assert pf.database_revisions("acme", timeout=0.01) is None
    finally:
        release.set()

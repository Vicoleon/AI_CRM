"""Control data is independent of workspace state and isolated in tests."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def control_db(tmp_path, monkeypatch):
    from rubik_core.config import settings
    from rubik_core.control import db
    from rubik_core.control.models import Base

    monkeypatch.setattr(
        settings, "CONTROL_DATABASE_URL", f"sqlite:///{tmp_path / 'control.db'}"
    )
    monkeypatch.setattr(db, "_engine", None)
    engine = db.get_engine()
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def test_control_domain_uniqueness(control_db):
    from rubik_core.control.db import control_session
    from rubik_core.control.models import Workspace, WorkspaceDomain

    with control_session() as session:
        session.add(Workspace(slug=" ACME ", name="Acme"))
        session.add(
            WorkspaceDomain(
                workspace_slug="acme", domain=" Acme.Example ", is_primary=True
            )
        )
        session.commit()
        assert session.get(Workspace, "acme").slug == "acme"
        session.add(WorkspaceDomain(workspace_slug="acme", domain="acme.example"))
        with pytest.raises(IntegrityError):
            session.commit()


@pytest.mark.parametrize(
    ("app_id", "team_id"),
    [(" A1 ", "T1"), ("A1", "T2"), ("A2", "T1")],
)
def test_unique_slack_installation(control_db, app_id, team_id):
    from rubik_core.control.db import control_session
    from rubik_core.control.models import SlackInstallation, Workspace

    with control_session() as session:
        session.add_all(
            [Workspace(slug="acme", name="Acme"), Workspace(slug="other", name="Other")]
        )
        session.flush()
        session.add(
            SlackInstallation(
                workspace_slug="acme",
                app_id="A1",
                team_id="T1",
                signing_secret_ref="ref",
            )
        )
        session.commit()
        session.add(
            SlackInstallation(
                workspace_slug="other",
                app_id=app_id,
                team_id=team_id,
                signing_secret_ref="other",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_repository_normalizes_and_filters_active(control_db):
    from rubik_core.control import repository as repo
    from rubik_core.control.db import control_session
    from rubik_core.control.models import (
        PlatformAdmin,
        SlackInstallation,
        Workspace,
        WorkspaceDomain,
        WorkspaceModule,
    )

    with control_session() as session:
        session.add_all(
            [
                Workspace(slug="acme", name="Acme"),
                Workspace(slug="paused", name="Paused", status="suspended"),
            ]
        )
        session.flush()
        session.add_all(
            [
                WorkspaceDomain(domain="Acme.Example", workspace_slug="acme"),
                WorkspaceDomain(domain="paused.example", workspace_slug="paused"),
                WorkspaceModule(workspace_slug="acme", module=" EOS ", enabled=True),
                WorkspaceModule(workspace_slug="paused", module="eos", enabled=True),
                SlackInstallation(
                    workspace_slug="acme",
                    app_id="A1",
                    team_id="T1",
                    signing_secret_ref="ref",
                ),
                SlackInstallation(
                    workspace_slug="paused",
                    app_id="A2",
                    team_id="T2",
                    signing_secret_ref="ref2",
                ),
                PlatformAdmin(email=" OWNER@EXAMPLE.COM ", home_workspace_slug="acme"),
            ]
        )
        session.commit()
    assert repo.get_workspace(" ACME ").slug == "acme"
    assert repo.get_workspace("paused").status == "suspended"
    assert repo.get_workspace("unknown") is None
    assert repo.workspace_for_domain(" ACME.EXAMPLE ").slug == "acme"
    assert repo.workspace_for_domain("paused.example") is None
    assert [row.slug for row in repo.active_workspaces()] == ["acme"]
    assert [row.slug for row in repo.active_workspaces(" EoS ")] == ["acme"]
    assert repo.active_workspaces("unknown") == []
    assert repo.workspace_for_slack(" A1 ", "T1").slug == "acme"
    assert repo.workspace_for_slack("A2", "T2") is None
    assert repo.workspace_for_slack("unknown", "T1") is None
    assert repo.is_platform_admin("owner@example.com")
    assert not repo.is_platform_admin("unknown@example.com")


def test_control_session_never_uses_workspace_engine(control_db, monkeypatch):
    from rubik_core.control.db import control_session
    from rubik_core.database import db as workspace_db

    def forbidden():
        raise AssertionError("workspace database accessed")

    monkeypatch.setattr(workspace_db, "get_engine", forbidden)
    monkeypatch.setattr(workspace_db, "SessionLocal", forbidden)
    with control_session() as session:
        assert session.get_bind() is control_db
        assert session.scalar(text("select 1")) == 1


def test_pending_provisioning_has_no_workspace_foreign_key(control_db):
    from rubik_core.control.db import control_session
    from rubik_core.control.models import ProvisioningStep

    assert not ProvisioningStep.__table__.foreign_keys
    assert "password" not in ProvisioningStep.__table__.columns
    with control_session() as session:
        session.add(
            ProvisioningStep(
                workspace_slug=" Pending ",
                step="database",
                status="complete",
                db_name="cubie_pending",
                db_user="pending",
                db_password_secret_ref="pending-password",
            )
        )
        session.commit()
        row = session.scalar(select(ProvisioningStep))
        assert row.workspace_slug == "pending"
        assert row.db_password_secret_ref == "pending-password"


def test_control_base_is_separate():
    from rubik_core.control.models import Base
    from rubik_core.database.models import Base as WorkspaceBase

    assert Base is not WorkspaceBase
    assert not set(Base.metadata.tables).intersection(WorkspaceBase.metadata.tables)


def test_pytest_database_guards_cover_control_and_workspace(pytestconfig):
    root_fixtures = next(
        plugin
        for plugin in pytestconfig.pluginmanager.get_plugins()
        if hasattr(plugin, "_databases_are_sqlite")
    )
    from rubik_core.config import settings
    from rubik_core.control import db as control_db
    from rubik_core.database import db as workspace_db

    for url in (settings.DATABASE_URL, settings.CONTROL_DATABASE_URL):
        assert make_url(url).get_backend_name() == "sqlite"
    for db in (control_db, workspace_db):
        assert db._engine is None or db._engine.dialect.name == "sqlite"
    assert os.environ["CONTROL_DATABASE_URL"].startswith("sqlite:")
    assert "cubie-control-pytest-" in settings.CONTROL_DATABASE_URL
    assert root_fixtures._databases_are_sqlite()[0]


def test_safety_guard_checks_urls_without_connecting(monkeypatch, pytestconfig):
    root_fixtures = next(
        plugin
        for plugin in pytestconfig.pluginmanager.get_plugins()
        if hasattr(plugin, "_databases_are_sqlite")
    )
    from rubik_core.config import settings

    with monkeypatch.context() as patch:
        patch.setattr(
            settings, "CONTROL_DATABASE_URL", "postgresql://example.invalid/control"
        )
        assert not root_fixtures._databases_are_sqlite()[0]
    with monkeypatch.context() as patch:
        patch.setattr(
            settings, "DATABASE_URL", "postgresql://example.invalid/workspace"
        )
        assert not root_fixtures._databases_are_sqlite()[0]


def test_control_alembic_baseline(tmp_path):
    from rubik_core.control.models import Base

    url = f"sqlite:///{tmp_path / 'migration.db'}"
    env = dict(
        os.environ,
        CONTROL_DATABASE_URL=url,
        CLOUDSQL_CONNECTION_NAME="",
        APP_MODE="local",
    )
    command = [sys.executable, "-m", "alembic", "-c", "alembic_control.ini"]
    heads = subprocess.run(
        [*command, "heads"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert heads.stdout.count("(head)") == 1
    assert "0002_slack_unique_ids" in heads.stdout
    subprocess.run(
        [*command, "upgrade", "head"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    from sqlalchemy import create_engine

    engine = create_engine(url)
    try:
        schema = inspect(engine)
        assert set(schema.get_table_names()) == {
            *Base.metadata.tables,
            "alembic_version",
        }
        for name, table in Base.metadata.tables.items():
            assert {col["name"] for col in schema.get_columns(name)} == set(
                table.columns.keys()
            )
        with engine.connect() as connection:
            from alembic.autogenerate import compare_metadata
            from alembic.migration import MigrationContext

            assert (
                compare_metadata(MigrationContext.configure(connection), Base.metadata)
                == []
            )
            assert (
                connection.scalar(text("select version_num from alembic_version"))
                == "0002_slack_unique_ids"
            )
    finally:
        engine.dispose()


def test_pending_connection_spec_is_nonsecret_and_validated():
    from dataclasses import fields

    from rubik_core.control.models import WorkspaceConnectionSpec

    spec = WorkspaceConnectionSpec(
        db_name="cubie_acme",
        db_user="acme_user",
        db_password_secret_ref="projects/local/secrets/acme-password",
    )
    assert {field.name for field in fields(spec)} == {
        "db_name",
        "db_user",
        "db_password_secret_ref",
    }
    assert spec.db_name == "cubie_acme"
    for invalid in ("", "acme;drop database other", "postgresql://host/acme"):
        with pytest.raises(ValueError):
            WorkspaceConnectionSpec(
                db_name=invalid, db_user="acme", db_password_secret_ref="ref"
            )
    with pytest.raises(ValueError):
        WorkspaceConnectionSpec(
            db_name="acme", db_user="acme", db_password_secret_ref=""
        )


@pytest.mark.parametrize("database", ["workspace", "control"])
def test_safety_guard_checks_cached_engines_without_connecting(
    monkeypatch, pytestconfig, database
):
    from types import SimpleNamespace

    from rubik_core.control import db as control_db
    from rubik_core.database import db as workspace_db

    root_fixtures = next(
        plugin
        for plugin in pytestconfig.pluginmanager.get_plugins()
        if hasattr(plugin, "_databases_are_sqlite")
    )
    db = control_db if database == "control" else workspace_db
    with monkeypatch.context() as patch:
        patch.setattr(
            db,
            "_engine",
            SimpleNamespace(
                dialect=SimpleNamespace(name="postgresql"),
                url="postgresql://example.invalid/db",
            ),
        )
        assert not root_fixtures._databases_are_sqlite()[0]


def test_control_sqlite_rejects_orphan_workspace_domain(control_db):
    from rubik_core.control.db import control_session
    from rubik_core.control.models import WorkspaceDomain

    with control_session() as session:
        assert session.scalar(text("PRAGMA foreign_keys")) == 1
        session.add(WorkspaceDomain(domain="orphan.example", workspace_slug="missing"))
        with pytest.raises(IntegrityError):
            session.commit()
    # A second DBAPI connection receives the same safety setting.
    with control_db.connect() as first, control_db.connect() as second:
        assert first.scalar(text("PRAGMA foreign_keys")) == 1
        assert second.scalar(text("PRAGMA foreign_keys")) == 1


def test_removing_platform_admin_revokes_historical_support_sessions(control_db):
    from datetime import datetime, timedelta, timezone

    from rubik_core.control.db import control_session
    from rubik_core.control.models import PlatformAdmin, SupportSession, Workspace

    with control_session() as session:
        session.add(Workspace(slug="acme", name="Acme"))
        session.flush()
        session.add(
            PlatformAdmin(email="owner@example.com", home_workspace_slug="acme")
        )
        session.flush()
        session.add(
            SupportSession(
                sid="support-1",
                actor="owner@example.com",
                target_workspace_slug="acme",
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
        )
        session.commit()
        session.delete(session.get(PlatformAdmin, "owner@example.com"))
        session.commit()
    with control_session() as session:
        assert session.get(PlatformAdmin, "owner@example.com") is None
        assert session.get(SupportSession, "support-1") is None


def test_workspace_has_at_most_one_primary_domain(control_db):
    from rubik_core.control.db import control_session
    from rubik_core.control.models import Workspace, WorkspaceDomain

    with control_session() as session:
        session.add(Workspace(slug="acme", name="Acme"))
        session.flush()
        session.add_all(
            [
                WorkspaceDomain(
                    domain="acme.example", workspace_slug="acme", is_primary=True
                ),
                WorkspaceDomain(
                    domain="alias.example", workspace_slug="acme", is_primary=False
                ),
                WorkspaceDomain(
                    domain="another.example", workspace_slug="acme", is_primary=False
                ),
            ]
        )
        session.commit()
        session.add(
            WorkspaceDomain(
                domain="second-primary.example", workspace_slug="acme", is_primary=True
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_control_foreign_key_lookup_columns_are_indexed(control_db):
    schema = inspect(control_db)
    for table, column in (
        ("platform_admins", "home_workspace_slug"),
        ("support_sessions", "actor"),
    ):
        assert any(
            index["column_names"] == [column] for index in schema.get_indexes(table)
        )


def test_control_engine_acquisition_blocks_non_sqlite_during_pytest(monkeypatch):
    from rubik_core.config import settings
    from rubik_core.control import db

    def must_not_construct(*args, **kwargs):
        pytest.fail("A non-SQLite engine was constructed during pytest")

    with monkeypatch.context() as patch:
        patch.setattr(
            settings, "CONTROL_DATABASE_URL", "postgresql://example.invalid/control"
        )
        patch.setattr(db, "_engine", None)
        patch.setattr(db, "create_engine", must_not_construct)
        with pytest.raises(RuntimeError, match="SQLite"):
            db.get_engine()


@pytest.mark.parametrize("url", ["", "sqlite:///./dam_data/control.db"])
def test_gcp_control_requires_explicit_database_configuration(monkeypatch, url):
    from rubik_core.config import AppMode, settings
    from rubik_core.control import db

    with monkeypatch.context() as patch:
        patch.setattr(settings, "APP_MODE", AppMode.GCP)
        patch.setattr(settings, "CONTROL_DATABASE_URL", url)
        with pytest.raises(RuntimeError, match="CONTROL_DATABASE_URL"):
            db.get_engine()


def test_gcp_allows_explicit_throwaway_sqlite_for_tests(control_db, monkeypatch):
    from rubik_core.config import AppMode, settings
    from rubik_core.control import db

    monkeypatch.setattr(settings, "APP_MODE", AppMode.GCP)
    assert db.get_engine() is control_db


def test_control_alembic_enables_foreign_keys_on_migration_connection(
    tmp_path, monkeypatch
):
    from alembic.config import Config
    from sqlalchemy import create_engine

    from alembic import command
    from rubik_core.config import settings
    from rubik_core.control import db

    url = f"sqlite:///{tmp_path / 'control-migration.db'}"
    # Deliberately omit the application's connect listener: env.py must
    # establish FK enforcement on its migration connection independently.
    engine = create_engine(url)
    monkeypatch.setattr(settings, "CONTROL_DATABASE_URL", url)
    monkeypatch.setattr(db, "get_engine", lambda: engine)
    try:
        command.upgrade(Config(str(ROOT / "alembic_control.ini")), "head")
        with engine.connect() as connection:
            assert connection.scalar(text("PRAGMA foreign_keys")) == 1
            with pytest.raises(IntegrityError):
                connection.execute(
                    text(
                        "INSERT INTO workspace_domains (domain, workspace_slug, is_primary) VALUES ('orphan.example', 'missing', 0)"
                    )
                )
    finally:
        engine.dispose()

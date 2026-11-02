"""A workspace is not routable until its pending database has migrated."""

from __future__ import annotations

import base64
import os
import secrets
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.engine import make_url

from rubik_core.control.db import control_session
from rubik_core.control.models import (
    PlatformAdmin,
    ProvisioningStep,
    Workspace,
    WorkspaceDomain,
    WorkspaceModule,
)
from rubik_core.platform.provision import (
    ConnectionBudget,
    CreateSpec,
    Provisioner,
    check_connection_headroom,
    database_grants_sql,
)


@pytest.fixture
def cleanup_workspace():
    yield
    from rubik_core.control.repository import clear_module_cache

    slugs = ("acme2", "acme3")
    with control_session() as session:
        session.execute(
            delete(PlatformAdmin).where(PlatformAdmin.home_workspace_slug.in_(slugs))
        )
        session.execute(
            delete(WorkspaceModule).where(WorkspaceModule.workspace_slug.in_(slugs))
        )
        session.execute(
            delete(WorkspaceDomain).where(WorkspaceDomain.workspace_slug.in_(slugs))
        )
        session.execute(delete(Workspace).where(Workspace.slug.in_(slugs)))
        session.execute(
            delete(ProvisioningStep).where(ProvisioningStep.workspace_slug.in_(slugs))
        )
        session.commit()
    for slug in slugs:
        clear_module_cache(slug)


@pytest.fixture
def spec():
    return CreateSpec(
        slug="acme2",
        name="Acme Two",
        domains=("acme2.example",),
        first_admins=("admin@acme2.example",),
        dwd_subject="admin@acme2.example",
        time_zone="America/Chicago",
    )


class FakeExecutor:
    def __init__(self):
        self.events = []
        self.fail_storage_once = False
        self.existing_names = []
        self.deleted_names = []

    def ensure_database(self, spec, password):
        assert password
        self.events.append("create_database")

    def ensure_storage(self, spec):
        self.events.append("create_storage")
        if self.fail_storage_once:
            self.fail_storage_once = False
            raise RuntimeError("storage unavailable")

    def corpus_resources(self, spec):
        return {}

    def ensure_service_account(self, spec):
        self.events.append("create_service_account")

    def service_account_identity(self, spec):
        return None, None

    def upsert_schedule(self, row):
        self.events.append(f"schedule:{row['name']}")

    def list_schedule_names(self):
        return list(self.existing_names)

    def delete_schedule(self, name):
        self.deleted_names.append(name)

    def checklist(self, spec, client_id):
        self.events.append("checklist")
        return ("Grant domain-wide delegation",)


def _provisioner(executor):
    return Provisioner(
        executor=executor,
        budget=ConnectionBudget(
            max_connections=10000, service_max_instances=1, job_max_instances=1
        ),
    )


def test_create_resumes_after_step_failure(cleanup_workspace, spec, monkeypatch):
    from rubik_core.platform import provision

    executor = FakeExecutor()
    executor.fail_storage_once = True
    monkeypatch.setattr(
        provision, "migrate_workspace", lambda slug, connection_spec: "0004"
    )
    monkeypatch.setattr(
        provision, "read_pending_secret", lambda slug, db_spec: "stored"
    )
    monkeypatch.setattr(
        provision, "write_pending_secret", lambda slug, db_spec, value: None
    )
    monkeypatch.setattr(provision, "_seed_registered_workspace", lambda value: None)
    with pytest.raises(RuntimeError, match="storage unavailable"):
        _provisioner(executor).create(spec)
    with control_session() as session:
        assert session.get(Workspace, spec.slug) is None
        done = {
            row.step
            for row in session.query(ProvisioningStep).filter_by(
                workspace_slug=spec.slug
            )
            if row.status == "complete"
        }
    assert done == {"validate", "check_headroom", "create_database", "migrate"}
    with pytest.raises(ValueError, match="recorded spec"):
        _provisioner(executor).create(replace(spec, accent="#123456"))

    result = _provisioner(executor).create(spec)
    assert result.status == "complete"
    assert executor.events.count("create_database") == 1
    assert executor.events.count("create_storage") == 2
    with control_session() as session:
        assert session.get(Workspace, spec.slug).status == "active"
        assert {
            row.step
            for row in session.query(ProvisioningStep).filter_by(
                workspace_slug=spec.slug
            )
        } == set(provision.STEPS)


def test_migration_resume_uses_persisted_spec(cleanup_workspace, spec, monkeypatch):
    from rubik_core.platform import provision

    executor = FakeExecutor()
    seen = []

    def migrate(slug, connection_spec):
        seen.append(connection_spec)
        if len(seen) == 1:
            raise RuntimeError("migration failed")
        return "0004"

    monkeypatch.setattr(provision, "migrate_workspace", migrate)
    monkeypatch.setattr(
        provision, "read_pending_secret", lambda slug, db_spec: "stored"
    )
    monkeypatch.setattr(
        provision, "write_pending_secret", lambda slug, db_spec, value: None
    )
    monkeypatch.setattr(provision, "_seed_registered_workspace", lambda value: None)
    with pytest.raises(RuntimeError, match="migration failed"):
        _provisioner(executor).create(spec)
    with control_session() as session:
        assert session.get(Workspace, spec.slug) is None
    _provisioner(executor).create(spec)
    assert seen[0] == seen[1]
    assert executor.events.count("create_database") == 1


def test_concurrent_create_serializes_one_database_identity(
    cleanup_workspace, spec, monkeypatch
):
    from rubik_core.platform import provision

    executor = FakeExecutor()
    monkeypatch.setattr(
        provision, "migrate_workspace", lambda slug, connection_spec: "0004"
    )
    monkeypatch.setattr(
        provision, "read_pending_secret", lambda slug, db_spec: "stored"
    )
    monkeypatch.setattr(
        provision, "write_pending_secret", lambda slug, db_spec, value: None
    )
    monkeypatch.setattr(provision, "_seed_registered_workspace", lambda value: None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: _provisioner(executor).create(spec), range(2))
        )
    assert all(result.status == "complete" for result in results)
    assert executor.events.count("create_database") == 1


def test_concurrent_distinct_slugs_reserve_only_available_capacity(
    cleanup_workspace, spec, monkeypatch
):
    from rubik_core.platform import provision

    other = replace(
        spec,
        slug="acme3",
        name="Acme Three",
        domains=("acme3.example",),
        first_admins=("admin@acme3.example",),
        dwd_subject="admin@acme3.example",
    )
    with control_session() as session:
        existing = {row[0] for row in session.execute(select(Workspace.slug))}
        existing.update(
            row[0]
            for row in session.execute(
                select(ProvisioningStep.workspace_slug).where(
                    ProvisioningStep.step == "validate",
                    ProvisioningStep.status == "complete",
                )
            )
        )
    # Two process instances need 4 control plus 9 workspace connections each.
    one_more = len(existing) + 1
    budget = ConnectionBudget(
        max_connections=2 * (4 + 9 * one_more),
        service_max_instances=1,
        job_max_instances=1,
        reserve=0,
    )
    monkeypatch.setattr(
        provision, "migrate_workspace", lambda slug, connection_spec: "0004"
    )
    monkeypatch.setattr(
        provision, "read_pending_secret", lambda slug, db_spec: "stored"
    )
    monkeypatch.setattr(
        provision, "write_pending_secret", lambda slug, db_spec, value: None
    )
    monkeypatch.setattr(provision, "_seed_registered_workspace", lambda value: None)
    executor = FakeExecutor()
    barrier = Barrier(2)

    def create(candidate):
        barrier.wait()
        return Provisioner(executor, budget).create(candidate)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(create, candidate) for candidate in (spec, other)]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result())
            except ValueError as exc:
                assert "connection headroom" in str(exc)
    assert len(outcomes) == 1
    assert executor.events.count("create_database") == 1


@pytest.mark.parametrize("journal_state", ["missing", "pending", "changed"])
def test_registered_workspace_never_rotates_password_with_bad_journal(
    cleanup_workspace, spec, journal_state
):
    from rubik_core.platform.provision import (
        _database_spec,
        _normalize_spec,
        _spec_fingerprint,
    )

    db_spec = _database_spec(spec.slug)
    with control_session() as session:
        session.add(
            Workspace(
                slug=spec.slug,
                name=spec.name,
                status="active",
                time_zone=spec.time_zone,
                db_name=db_spec.db_name,
                db_user=db_spec.db_user,
                db_password_secret_ref=db_spec.db_password_secret_ref,
            )
        )
        session.add(WorkspaceDomain(workspace_slug=spec.slug, domain=spec.domains[0]))
        session.add(
            ProvisioningStep(
                workspace_slug=spec.slug,
                step="validate",
                status="complete",
                schema_revision=_spec_fingerprint(_normalize_spec(spec)),
            )
        )
        if journal_state != "missing":
            session.add(
                ProvisioningStep(
                    workspace_slug=spec.slug,
                    step="create_database",
                    status="pending" if journal_state == "pending" else "complete",
                    db_name="wrong_database"
                    if journal_state == "changed"
                    else db_spec.db_name,
                    db_user=db_spec.db_user,
                    db_password_secret_ref=db_spec.db_password_secret_ref,
                )
            )
        session.commit()
    executor = FakeExecutor()
    with pytest.raises((RuntimeError, PermissionError), match="database identity"):
        _provisioner(executor).create(spec)
    assert "create_database" not in executor.events


def test_dry_run_has_no_control_or_cloud_writes(cleanup_workspace, spec):
    executor = FakeExecutor()
    result = _provisioner(executor).create(spec, dry_run=True)
    assert result.status == "dry_run"
    assert not executor.events
    with control_session() as session:
        assert session.get(Workspace, spec.slug) is None
        assert (
            not session.query(ProvisioningStep)
            .filter_by(workspace_slug=spec.slug)
            .count()
        )


def test_domain_collision_and_first_admin_validation(cleanup_workspace, spec):
    with pytest.raises(ValueError, match="domain"):
        _provisioner(FakeExecutor()).create(
            replace(spec, domains=("acme.example",)), dry_run=True
        )
    with pytest.raises(ValueError, match="admin"):
        _provisioner(FakeExecutor()).create(
            replace(spec, first_admins=()), dry_run=True
        )
    with pytest.raises(ValueError, match="Freeze"):
        _provisioner(FakeExecutor()).create(
            replace(
                spec,
                freeze_windows=(
                    {
                        "day": "FRI",
                        "start": "15:00",
                        "end": "13:00",
                        "time_zone": "America/Chicago",
                    },
                ),
            ),
            dry_run=True,
        )


def test_headroom_counts_sync_adk_and_control_pools():
    budget = ConnectionBudget(
        max_connections=30, service_max_instances=2, job_max_instances=1, reserve=5
    )
    with pytest.raises(ValueError, match="connection headroom"):
        check_connection_headroom(3, budget)
    assert check_connection_headroom(1, ConnectionBudget(100, 2, 1)) > 0


def test_database_grants_deny_public_and_other_workspace_role():
    sql = database_grants_sql("cubie_acme2", "cubie_acme2_user")
    assert 'REVOKE CONNECT ON DATABASE "cubie_acme2" FROM PUBLIC' in sql
    assert 'GRANT CONNECT ON DATABASE "cubie_acme2" TO "cubie_acme2_user"' in sql
    assert "other" not in sql


def test_cloud_role_password_ddl_uses_safe_literal_and_sanitizes_error(monkeypatch):
    from rubik_core.control.models import WorkspaceConnectionSpec
    from rubik_core.platform import executor as executor_module
    from rubik_core.platform.executor import CloudExecutor

    password = "safe_Pass-123"

    class Connection:
        def __init__(self):
            self.statements = []
            self.bound_queries = []
            self.fail_password = False
            self.fail_quote = False

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def scalar(self, statement, params):
            self.bound_queries.append((str(statement), params))
            if "quote_literal" in str(statement):
                if self.fail_quote:
                    raise RuntimeError(f"driver included secret {password}")
                return f"'{params['password']}'"
            return None

        def exec_driver_sql(self, statement):
            self.statements.append(statement)
            if self.fail_password and statement.startswith("ALTER ROLE"):
                raise RuntimeError(f"driver included secret {password}")

    class Engine:
        def __init__(self, connection):
            self.connection = connection

        def connect(self):
            return self.connection

        def dispose(self):
            pass

    connection = Connection()
    monkeypatch.setattr(
        executor_module, "create_engine", lambda *_args, **_kwargs: Engine(connection)
    )
    executor = object.__new__(CloudExecutor)
    executor.admin_database_url = "postgresql+pg8000://unused"
    db_spec = WorkspaceConnectionSpec("cubie_safe", "cubie_safe_user", "secret-ref")
    executor.ensure_database(db_spec, password)
    assert (
        f"ALTER ROLE \"cubie_safe_user\" WITH PASSWORD '{password}'"
        in connection.statements
    )
    assert (
        "SELECT quote_literal(:password)",
        {"password": password},
    ) in connection.bound_queries
    assert not any(":password" in statement for statement in connection.statements)
    with pytest.raises(ValueError, match="URL-safe"):
        executor.ensure_database(db_spec, "bad'password")
    connection.fail_password = True
    with pytest.raises(
        RuntimeError, match="Could not set workspace database password"
    ) as error:
        executor.ensure_database(db_spec, password)
    assert password not in str(error.value)
    connection.fail_password = False
    connection.fail_quote = True
    with pytest.raises(
        RuntimeError, match="Could not set workspace database password"
    ) as quote_error:
        executor.ensure_database(db_spec, password)
    assert password not in str(quote_error.value)


def test_workspace_db_roles_cannot_cross_connect_when_local_postgres_is_supplied():
    """Opt-in real ACL check against an operator-provided throwaway local PG."""
    admin_url = os.environ.get("CUBIE_TEST_PG_ADMIN_URL", "")
    if not admin_url:
        pytest.skip("Set CUBIE_TEST_PG_ADMIN_URL for local PostgreSQL ACL integration")
    parsed = make_url(admin_url)
    if parsed.get_backend_name() != "postgresql" or parsed.host not in (
        None,
        "",
        "localhost",
        "127.0.0.1",
        "::1",
    ):
        pytest.skip("PostgreSQL ACL integration accepts only a local throwaway server")
    from rubik_core.control.models import WorkspaceConnectionSpec
    from rubik_core.platform.executor import CloudExecutor

    suffix = secrets.token_hex(4)
    first = WorkspaceConnectionSpec(
        f"cubie_acl_a_{suffix}", f"cubie_acl_a_{suffix}_user", "test"
    )
    second = WorkspaceConnectionSpec(
        f"cubie_acl_b_{suffix}", f"cubie_acl_b_{suffix}_user", "test"
    )
    executor = object.__new__(CloudExecutor)
    executor.admin_database_url = admin_url
    password = secrets.token_urlsafe(32)
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        executor.ensure_database(first, password)
        executor.ensure_database(second, password)
        owner_url = parsed.set(
            username=first.db_user, password=password, database=first.db_name
        )
        owner = create_engine(owner_url)
        try:
            with owner.connect() as connection:
                assert (
                    connection.scalar(text("SELECT current_database()"))
                    == first.db_name
                )
        finally:
            owner.dispose()
        foreign_url = parsed.set(
            username=second.db_user, password=password, database=first.db_name
        )
        foreign = create_engine(foreign_url)
        try:
            with pytest.raises(Exception, match="permission denied"):
                with foreign.connect():
                    pass
        finally:
            foreign.dispose()
    finally:
        with engine.connect() as connection:
            for spec in (first, second):
                connection.exec_driver_sql(
                    f'DROP DATABASE IF EXISTS "{spec.db_name}" WITH (FORCE)'
                )
                connection.exec_driver_sql(f'DROP ROLE IF EXISTS "{spec.db_user}"')
        engine.dispose()


def test_keyless_service_account_id_is_stable_and_bounded():
    from rubik_core.platform.executor import service_account_id

    slug = "long-workspace-name-with-many-characters-for-a-client"
    value = service_account_id(slug)
    assert value == service_account_id(slug)
    assert len(value) <= 30
    assert value != service_account_id(slug + "2")


def test_cloud_scheduler_upsert_preserves_workspace_header_and_json_body(monkeypatch):
    from rubik_core.platform.executor import CloudExecutor

    class Response:
        def __init__(self, status):
            self.status_code = status
            self.ok = status < 400

        def json(self):
            return {}

    class Session:
        def __init__(self):
            self.posts = []

        def get(self, url):
            return Response(404)

        def post(self, url, *, json):
            self.posts.append((url, json))
            return Response(200)

    session = Session()
    executor = object.__new__(CloudExecutor)
    executor.project = "example-project"
    executor.region = "us-central1"
    executor.service_url = "https://cubie.example"
    monkeypatch.setattr(executor, "_authorized_session", lambda: session)
    monkeypatch.setenv("SCHEDULER_API_KEY", "private-scheduler-key")
    row = {
        "name": "priorities-deliver-next-week--acme2",
        "target": "http",
        "path": "/api/agents/priorities/deliver-slack",
        "method": "POST",
        "schedule": "0 16 * * 5",
        "time_zone": "America/Chicago",
        "headers": {
            "X-Cubie-Workspace": "acme2",
            "X-Scheduler-Key": {"secret": "SCHEDULER_API_KEY"},
            "Content-Type": "application/json",
        },
        "body": '{"report":"next_week"}',
    }
    executor.upsert_schedule(row)
    posted = session.posts[0][1]
    target = posted["httpTarget"]
    assert target["headers"]["X-Cubie-Workspace"] == "acme2"
    assert target["headers"]["X-Scheduler-Key"] == "private-scheduler-key"
    assert target["headers"]["Content-Type"] == "application/json"
    assert base64.b64decode(target["body"]) == b'{"report":"next_week"}'


def test_delete_requires_exact_typed_slug(cleanup_workspace, spec):
    with pytest.raises(ValueError, match="exact slug"):
        _provisioner(FakeExecutor()).delete(spec.slug, typed_slug="ACME2", dry_run=True)


def test_new_workspace_gets_timezone_schedules_and_json_body():
    from rubik_core.platform.schedules import load_manifest, reconcile_schedules

    executor = FakeExecutor()
    rows = reconcile_schedules(
        load_manifest(),
        [SimpleNamespace(slug="acme2", status="active", time_zone="America/Chicago")],
        executor,
    )
    delivery = next(
        row for row in rows if row["name"] == "priorities-deliver-next-week--acme2"
    )
    assert delivery["schedule"] == "0 16 * * 5"
    assert delivery["time_zone"] == "America/Chicago"
    assert delivery["headers"]["X-Cubie-Workspace"] == "acme2"
    assert delivery["headers"]["Content-Type"] == "application/json"
    assert delivery["body"] == '{"report":"next_week"}'
    assert "schedule:priorities-deliver-next-week--acme2" in executor.events


def test_schedule_reconcile_prunes_suspended_workspace_only():
    from rubik_core.platform.schedules import load_manifest, reconcile_schedules

    executor = FakeExecutor()
    executor.existing_names = [
        "priorities-run-all--paused",
        "priorities-run-all--acme2",
        "priorities-run-all--acme2-extra!",
        "unrelated-job--paused",
        "scheduled-tasks-tick",
    ]
    active = [
        SimpleNamespace(slug="acme2", status="active", time_zone="America/Chicago")
    ]
    manifest = load_manifest()
    reconcile_schedules(manifest, active, executor)
    assert executor.deleted_names == ["priorities-run-all--paused"]
    executor.deleted_names.clear()
    executor.events.clear()
    reconcile_schedules(manifest, active, executor, dry_run=True)
    assert not executor.deleted_names
    assert not executor.events


def test_registration_clears_stale_module_cache_before_schedule_expansion(
    cleanup_workspace, spec, monkeypatch
):
    from rubik_core.control.repository import module_enabled
    from rubik_core.platform import provision

    assert not module_enabled("eos", spec.slug)  # Cache the pre-registration result.
    monkeypatch.setattr(
        provision, "migrate_workspace", lambda slug, connection_spec: "0004"
    )
    monkeypatch.setattr(
        provision, "read_pending_secret", lambda slug, db_spec: "stored"
    )
    monkeypatch.setattr(
        provision, "write_pending_secret", lambda slug, db_spec, value: None
    )
    monkeypatch.setattr(provision, "_seed_registered_workspace", lambda value: None)
    executor = FakeExecutor()
    _provisioner(executor).create(replace(spec, modules=("eos",)))
    assert module_enabled("eos", spec.slug)
    assert "schedule:eos-ingest--acme2" in executor.events


def test_platform_admin_remove_keeps_one_active_operator(cleanup_workspace, spec):
    from rubik_core.platform.cli import _platform_admin

    first = "first@acme2.example"
    second = "second@acme2.example"
    with control_session() as session:
        session.add(Workspace(slug=spec.slug, name=spec.name, status="active"))
        session.flush()
        session.add_all(
            [
                PlatformAdmin(email=first, home_workspace_slug=spec.slug),
                PlatformAdmin(email=second, home_workspace_slug=spec.slug),
            ]
        )
        session.commit()
    assert _platform_admin(
        SimpleNamespace(action="remove", email=first, confirm_email=first)
    ) == {"email": first, "status": "remove"}
    with pytest.raises(ValueError, match="last active platform admin"):
        _platform_admin(
            SimpleNamespace(action="remove", email=second, confirm_email=second)
        )
    with control_session() as session:
        assert session.get(PlatformAdmin, second) is not None


def test_local_create_migrates_and_seeds_first_admin(
    cleanup_workspace, spec, tmp_path, monkeypatch
):
    from sqlalchemy.orm import sessionmaker

    from rubik_core.config import settings
    from rubik_core.database.models import User
    from rubik_core.platform.executor import LocalExecutor
    from rubik_core.tenancy.context import use_workspace
    from rubik_core.tenancy.registry import clear_engines, engine_for

    monkeypatch.setattr(settings, "WORKSPACE_DATABASE_DIR", str(tmp_path / "databases"))
    monkeypatch.setattr(settings, "LOCAL_DATA_DIR", str(tmp_path / "data"))
    try:
        outcome = _provisioner(LocalExecutor()).create(spec)
        assert outcome.status == "complete"
        with use_workspace(spec.slug):
            with sessionmaker(bind=engine_for(spec.slug))() as session:
                admin = session.get(User, spec.first_admins[0])
                assert admin is not None
                assert {role.name for role in admin.roles} >= {"admin", "user"}
        assert (tmp_path / "data" / "workspaces" / spec.slug).is_dir()
        assert _provisioner(LocalExecutor()).create(spec).status == "complete"
        from rubik_core.control.repository import (
            active_workspaces,
            workspace_for_domain,
        )

        provisioner = _provisioner(LocalExecutor())
        assert (
            provisioner.delete(spec.slug, typed_slug=spec.slug, dry_run=True)
            == "would_suspend"
        )
        assert provisioner.delete(spec.slug, typed_slug=spec.slug) == "suspended"
        assert provisioner.delete(spec.slug, typed_slug=spec.slug) == "suspended"
        assert spec.slug not in {row.slug for row in active_workspaces()}
        assert workspace_for_domain(spec.domains[0]) is None
        with pytest.raises(ValueError, match="unknown or inactive"):
            engine_for(spec.slug)
    finally:
        clear_engines()


def test_shell_dry_run_needs_no_gcloud_and_prints_no_secret(
    cleanup_workspace, spec, tmp_path
):
    from rubik_core.config import settings

    root = Path(__file__).resolve().parents[2]
    env_file = tmp_path / "provision.env"
    marker = "private-marker-that-must-not-print"
    env_file.write_text(
        "APP_MODE=local\n"
        "CLOUDSQL_CONNECTION_NAME=\n"
        f"CONTROL_DATABASE_URL={settings.CONTROL_DATABASE_URL}\n"
        f"LOCAL_DATA_DIR={tmp_path / 'data'}\n"
        f"CUBIE_PROVISION_BOOTSTRAP_ADMIN_EMAIL={spec.first_admins[0]}\n"
        f"CUBIE_PROVISION_TEST_SECRET={marker}\n"
    )
    result = subprocess.run(
        [
            str(root / "scripts" / "platform" / "workspace.sh"),
            "--env",
            str(env_file),
            "workspace",
            "create",
            "--slug",
            spec.slug,
            "--name",
            spec.name,
            "--domain",
            spec.domains[0],
            "--first-admin",
            spec.first_admins[0],
            "--dwd-subject",
            spec.dwd_subject,
            "--time-zone",
            spec.time_zone,
            "--dry-run",
            "--actor-email",
            spec.first_admins[0],
        ],
        cwd=root,
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert '"status": "dry_run"' in result.stdout
    assert marker not in result.stdout + result.stderr
    with control_session() as session:
        assert session.get(Workspace, spec.slug) is None

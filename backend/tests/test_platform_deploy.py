"""Task 19 release guards and dry runs; never invoke gcloud or mutate SQL."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text

from rubik_core.control.models import Base, Workspace
from rubik_core.platform import deployment


@pytest.fixture
def control(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'control.db'}"
    monkeypatch.setenv("CONTROL_DATABASE_URL", url)
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
        )
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:head)"),
            {"head": deployment._head("alembic_control.ini")[1]},
        )
    yield engine
    engine.dispose()


@pytest.fixture
def platform_env(monkeypatch):
    for name, value in {
        "GCP_PROJECT_ID": "test-project",
        "GCP_REGION": "us-central1",
        "CLOUDSQL_INSTANCE": "test-sql",
        "GCS_BUCKET_NAME": "test-bucket",
        "GOOGLE_CLIENT_ID": "public-client-id.apps.googleusercontent.com",
        "CUBIE_RUNTIME_SERVICE_ACCOUNT": "cubie-runtime@test-project.iam.gserviceaccount.com",
        "CUBIE_OPERATOR_SQL_INSTANCE": "test-project:us-central1:test-sql",
        "CUBIE_SQL_EDITION": "ENTERPRISE",
        "CUBIE_SQL_TIER": "db-custom-1-3840",
        "CUBIE_SQL_STORAGE_GB": "10",
        "CUBIE_SQL_AVAILABILITY_TYPE": "ZONAL",
        "CUBIE_PROVISION_DB_MAX_CONNECTIONS": "100",
        "CUBIE_PROVISION_SERVICE_MAX_INSTANCES": "1",
        "CUBIE_PROVISION_JOB_MAX_INSTANCES": "5",
        "CUBIE_PROVISION_RESERVED_CONNECTIONS": "10",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("DEPLOY_FREEZE_OVERRIDE_REASON", raising=False)
    monkeypatch.delenv("DEPLOY_ALLOW_UNMERGED", raising=False)


def _workspace(engine, slug: str, *, freeze_windows=None, status="active"):
    from sqlalchemy.orm import Session

    with Session(engine) as session:
        session.add(
            Workspace(
                slug=slug,
                name=slug,
                status=status,
                freeze_windows=freeze_windows or [],
                time_zone="UTC",
            )
        )
        session.commit()


def test_unmigrated_or_corrupt_control_fails_closed_before_build(
    tmp_path, platform_env, monkeypatch
):
    url = f"sqlite:///{tmp_path / 'missing.db'}"
    monkeypatch.setenv("CONTROL_DATABASE_URL", url)
    monkeypatch.setattr(
        deployment,
        "_gcloud",
        lambda *_a, **_k: pytest.fail("cloud call before control preflight"),
    )
    with pytest.raises(deployment.DeployBlocked) as error:
        deployment.deploy(dry_run=True)
    assert error.value.code == 7
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE workspaces (slug TEXT PRIMARY KEY)"))
    engine.dispose()
    with pytest.raises(deployment.DeployBlocked) as corrupt:
        deployment.deploy(dry_run=True)
    assert corrupt.value.code == 7


def test_pre_migration_freeze_projection_works_with_baseline_only_columns(
    tmp_path, monkeypatch
):
    url = f"sqlite:///{tmp_path / 'older-control.db'}"
    monkeypatch.setenv("CONTROL_DATABASE_URL", url)
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE workspaces (slug TEXT PRIMARY KEY, status TEXT, "
                "freeze_windows JSON, time_zone TEXT, schema_revision TEXT)"
            )
        )
        connection.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
        )
        connection.execute(
            text("INSERT INTO alembic_version VALUES ('0001_control_baseline')")
        )
        connection.execute(
            text(
                "INSERT INTO workspaces VALUES "
                "('acme','active','[]','America/Costa_Rica',NULL)"
            )
        )
    state = deployment.read_control()
    assert state.revision == "0001_control_baseline"
    assert state.workspaces[0].slug == "acme"
    assert state.workspaces[0].freeze_windows == []
    engine.dispose()


def test_freeze_and_its_twenty_minute_lead_block_without_build(
    control, platform_env, monkeypatch
):
    now = datetime.now(timezone.utc)
    start = (now + timedelta(minutes=10)).replace(second=0, microsecond=0)
    end = start + timedelta(hours=1)
    if end.date() != start.date():
        start = now.replace(hour=0, minute=0)
        end = now.replace(hour=23, minute=59)
    window = {
        "day": start.strftime("%a").upper(),
        "start": start.strftime("%H:%M"),
        "end": end.strftime("%H:%M"),
        "time_zone": "UTC",
    }
    _workspace(control, "acme", freeze_windows=[window])
    monkeypatch.setattr(
        deployment, "_gcloud", lambda *_a, **_k: pytest.fail("cloud call during freeze")
    )
    with pytest.raises(deployment.DeployBlocked) as error:
        deployment.deploy(dry_run=True)
    assert error.value.code == 7
    assert "acme" in str(error.value)


def test_malformed_freeze_fails_closed_even_with_manual_override(
    control, platform_env, monkeypatch
):
    _workspace(control, "acme", freeze_windows=[{"day": "BOGUS"}])
    monkeypatch.setenv("DEPLOY_FREEZE_OVERRIDE_REASON", "service outage")
    with pytest.raises(deployment.DeployBlocked) as error:
        deployment.check_freezes(deployment.read_control())
    assert error.value.code == 7


def test_zero_workspaces_after_control_baseline_can_dry_run(
    control, platform_env, monkeypatch, capsys
):
    monkeypatch.setattr(deployment, "check_tree", lambda: "a" * 40)
    monkeypatch.setattr(
        deployment, "_gcloud", lambda *_a, **_k: pytest.fail("gcloud in dry run")
    )
    deployment.deploy(dry_run=True)
    report = capsys.readouterr().out
    assert "0 registered workspaces" in report
    assert "DRY RUN" in report


def test_resource_bootstrap_dry_run_is_write_free(
    tmp_path, platform_env, monkeypatch, capsys
):
    target = tmp_path / "control.db"
    monkeypatch.setenv("CONTROL_DATABASE_URL", f"sqlite:///{target}")
    monkeypatch.setattr(
        deployment, "_gcloud", lambda *_a, **_k: pytest.fail("gcloud in dry run")
    )
    monkeypatch.setattr(
        deployment, "_gcloud_maybe", lambda *_a: pytest.fail("cloud read in dry run")
    )
    monkeypatch.setattr(
        deployment, "create_engine", lambda *_a, **_k: pytest.fail("SQL in resources")
    )
    deployment.bootstrap(dry_run=True, fresh_install=False, phase="resources")
    assert not target.exists()
    report = capsys.readouterr().out
    assert "ENTERPRISE" in report
    assert "db-custom-1-3840" in report
    assert "10" in report
    assert "ZONAL" in report


def test_fresh_platform_bootstrap_enables_registry_and_iam_apis(
    tmp_path, platform_env, monkeypatch
):
    monkeypatch.setenv("CONTROL_DATABASE_URL", f"sqlite:///{tmp_path / 'control.db'}")
    monkeypatch.setattr(deployment, "_runtime_control_url", lambda *a: "control")
    monkeypatch.setattr(deployment, "_runtime_admin_url", lambda *a: "admin")

    class CapturedEnable(Exception):
        pass

    def capture(*args, **kwargs):
        assert args[:2] == ("services", "enable")
        assert "artifactregistry.googleapis.com" in args
        assert "iam.googleapis.com" in args
        raise CapturedEnable

    monkeypatch.setattr(deployment, "_gcloud", capture)
    with pytest.raises(CapturedEnable):
        deployment.bootstrap(dry_run=False, fresh_install=False, phase="resources")


@pytest.mark.parametrize(
    "name,value",
    [
        ("CUBIE_SQL_EDITION", ""),
        ("CUBIE_SQL_EDITION", "UNKNOWN"),
        ("CUBIE_SQL_TIER", ""),
        ("CUBIE_SQL_TIER", "db-custom-1-3840,--project=other-project"),
        ("CUBIE_SQL_STORAGE_GB", "0"),
        ("CUBIE_SQL_STORAGE_GB", "1.5"),
        ("CUBIE_SQL_AVAILABILITY_TYPE", ""),
        ("CUBIE_SQL_AVAILABILITY_TYPE", "automatic"),
    ],
)
@pytest.mark.parametrize("dry_run", [False, True])
def test_resource_bootstrap_rejects_invalid_cost_inputs_before_cloud(
    platform_env, monkeypatch, name, value, dry_run
):
    monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        deployment,
        "_gcloud",
        lambda *_a, **_k: pytest.fail("cloud write before validation"),
    )
    monkeypatch.setattr(
        deployment,
        "_gcloud_maybe",
        lambda *_a: pytest.fail("cloud read before validation"),
    )
    with pytest.raises(deployment.DeployBlocked):
        deployment.bootstrap(dry_run=dry_run, fresh_install=False, phase="resources")


def test_resource_bootstrap_never_connects_sql_or_writes_secret_values(
    platform_env, monkeypatch
):
    writes = []
    reads = []
    monkeypatch.setattr(
        deployment,
        "_gcloud",
        lambda *args, **kwargs: writes.append(args) or "",
    )
    monkeypatch.setattr(deployment, "_gcloud_maybe", lambda *args: reads.append(args))
    monkeypatch.setattr(
        deployment, "create_engine", lambda *_a, **_k: pytest.fail("SQL in resources")
    )

    deployment.bootstrap(dry_run=False, fresh_install=False, phase="resources")

    sql_create = next(
        args for args in writes if args[:3] == ("sql", "instances", "create")
    )
    assert "--edition=ENTERPRISE" in sql_create
    assert "--tier=db-custom-1-3840" in sql_create
    assert "--storage-size=10" in sql_create
    assert "--availability-type=ZONAL" in sql_create
    assert not any(args[:2] == ("secrets", "versions") for args in writes + reads)
    assert not any(args[:2] == ("sql", "users") for args in writes)


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "other-sql"),
        ("connectionName", "other-project:us-central1:test-sql"),
        ("state", "PENDING_CREATE"),
        ("edition", "ENTERPRISE_PLUS"),
        ("tier", "db-custom-2-7680"),
        ("dataDiskSizeGb", "20"),
        ("dataDiskType", "PD_HDD"),
        ("availabilityType", "REGIONAL"),
        ("storageAutoResize", True),
    ],
)
def test_resource_retry_refuses_mismatched_sql_without_creating_or_patching_it(
    platform_env, monkeypatch, field, value
):
    metadata = {
        "name": "test-sql",
        "region": "us-central1",
        "connectionName": "test-project:us-central1:test-sql",
        "state": "RUNNABLE",
        "databaseVersion": "POSTGRES_16",
        "settings": {
            "edition": "ENTERPRISE",
            "tier": "db-custom-1-3840",
            "dataDiskSizeGb": "10",
            "dataDiskType": "PD_SSD",
            "availabilityType": "ZONAL",
            "storageAutoResize": False,
        },
    }
    if field in metadata:
        metadata[field] = value
    else:
        metadata["settings"][field] = value
    writes = []
    monkeypatch.setattr(
        deployment, "_gcloud", lambda *args, **kwargs: writes.append(args) or ""
    )

    def read(*args):
        assert args[:3] == ("sql", "instances", "describe")
        return json.dumps(metadata)

    monkeypatch.setattr(deployment, "_gcloud_maybe", read)
    with pytest.raises(deployment.DeployBlocked):
        deployment.bootstrap(dry_run=False, fresh_install=False, phase="resources")
    assert len(writes) == 1, "mismatched SQL caused mutation after API enablement"
    assert writes[0][:2] == ("services", "enable")


def test_bootstrap_cli_passes_explicit_phase(monkeypatch):
    calls = []
    monkeypatch.setattr(deployment, "bootstrap", lambda **kwargs: calls.append(kwargs))
    assert deployment.main(["bootstrap", "--phase", "resources", "--dry-run"]) == 0
    assert calls == [{"dry_run": True, "fresh_install": False, "phase": "resources"}]


def _control_urls(monkeypatch):
    socket = "/cloudsql/test-project:us-central1:test-sql/.s.PGSQL.5432"
    values = {
        "CONTROL_DATABASE_URL": "postgresql+pg8000://control:control-pass@127.0.0.1:5432/control",
        "CUBIE_PROVISION_ADMIN_DATABASE_URL": "postgresql+pg8000://provisioner:admin-pass@127.0.0.1:5432/postgres",
        "CUBIE_RUNTIME_CONTROL_DATABASE_URL": f"postgresql+pg8000://control:control-pass@/control?unix_sock={socket}",
        "CUBIE_RUNTIME_ADMIN_DATABASE_URL": f"postgresql+pg8000://provisioner:admin-pass@/postgres?unix_sock={socket}",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


def test_control_dry_run_runs_preflight_without_mutations(
    platform_env, monkeypatch, capsys
):
    _control_urls(monkeypatch)
    checks = []
    monkeypatch.setattr(
        deployment,
        "_control_bootstrap_preflight",
        lambda *args, **kwargs: checks.append((args, kwargs)),
    )
    monkeypatch.setattr(deployment, "_control_secret_plan", lambda *a: [])
    monkeypatch.setattr(
        deployment, "_gcloud", lambda *_a, **_k: pytest.fail("cloud write in dry run")
    )
    monkeypatch.setattr(
        deployment,
        "_create_fresh_control_database",
        lambda: pytest.fail("SQL write in dry run"),
    )
    deployment.bootstrap(dry_run=True, fresh_install=True, phase="control")
    assert checks == [
        (("test-project", "us-central1", "test-sql"), {"fresh_install": True})
    ]
    report = capsys.readouterr().out
    assert "DRY RUN" in report
    assert "control-pass" not in report
    assert "admin-pass" not in report


@pytest.mark.parametrize("dry_run", [False, True])
def test_all_bootstrap_requires_prepared_sql_before_resource_writes(
    platform_env, monkeypatch, dry_run
):
    _control_urls(monkeypatch)

    def unavailable(*args, **kwargs):
        raise deployment.DeployBlocked("SQL connection preflight failed")

    monkeypatch.setattr(deployment, "_control_bootstrap_preflight", unavailable)
    monkeypatch.setattr(
        deployment,
        "_gcloud",
        lambda *_a, **_k: pytest.fail("resource write before SQL preflight"),
    )
    with pytest.raises(deployment.DeployBlocked, match="SQL connection preflight"):
        deployment.bootstrap(dry_run=dry_run, fresh_install=True, phase="all")


def test_control_bootstrap_populates_existing_containers_without_resource_creation(
    platform_env, monkeypatch
):
    from rubik_core.platform import migrate

    urls = _control_urls(monkeypatch)
    mutations = []
    monkeypatch.setattr(
        deployment, "_control_bootstrap_preflight", lambda *a, **k: None
    )
    monkeypatch.setattr(
        deployment,
        "_create_fresh_control_database",
        lambda: mutations.append("control-database"),
    )
    monkeypatch.setattr(
        migrate, "migrate_control", lambda: mutations.append("alembic") or "head"
    )

    def existing(*args):
        assert args[0] == "secrets", "control phase read an unrelated cloud resource"
        assert args[1] == "describe"
        return "existing-container"

    def secret_read(*args, **kwargs):
        assert args[:2] == ("secrets", "versions"), "control created a cloud resource"
        if args[2] == "list":
            return json.dumps([{"name": "versions/1", "state": "ENABLED"}])
        assert args[2] == "access", "control rotated an existing secret"
        if "--secret=CUBIE_CONTROL_DATABASE_URL" in args:
            return urls["CUBIE_RUNTIME_CONTROL_DATABASE_URL"]
        if "--secret=CUBIE_PROVISION_ADMIN_DATABASE_URL" in args:
            return urls["CUBIE_RUNTIME_ADMIN_DATABASE_URL"]
        return "existing-signing-secret"

    monkeypatch.setattr(deployment, "_gcloud_maybe", existing)
    monkeypatch.setattr(deployment, "_gcloud", secret_read)
    deployment.bootstrap(dry_run=False, fresh_install=True, phase="control")
    assert mutations == ["control-database", "alembic"]


def test_control_secret_mismatch_stops_before_sql_mutations(platform_env, monkeypatch):
    _control_urls(monkeypatch)
    monkeypatch.setattr(
        deployment, "_control_bootstrap_preflight", lambda *a, **k: None
    )
    monkeypatch.setattr(deployment, "_gcloud_maybe", lambda *a: "existing-container")
    monkeypatch.setattr(
        deployment,
        "_create_fresh_control_database",
        lambda: pytest.fail("SQL write before secret identity verification"),
    )

    def read(*args, **kwargs):
        assert args[:2] == ("secrets", "versions")
        if args[2] == "list":
            return json.dumps([{"name": "versions/1", "state": "ENABLED"}])
        assert args[2] == "access", "preflight created a secret version"
        return "wrong-or-unrelated-value"

    monkeypatch.setattr(deployment, "_gcloud", read)
    with pytest.raises(deployment.DeployBlocked, match="differs from declared target"):
        deployment.bootstrap(dry_run=False, fresh_install=True, phase="control")


def _stub_control_preflight(
    monkeypatch,
    *,
    privileged=True,
    max_connections=100,
    owner="control",
    tables=(),
    database_exists=True,
    role_exists=True,
    can_set=True,
    can_inherit=True,
    invalid_target_credentials=False,
):
    """Only catalog reads work; an accidental mutation fails the test."""
    statements = []
    connections = []
    metadata = {
        "name": "test-sql",
        "region": "us-central1",
        "connectionName": "test-project:us-central1:test-sql",
        "state": "RUNNABLE",
        "databaseVersion": "POSTGRES_16",
    }
    monkeypatch.setattr(deployment, "_gcloud_maybe", lambda *a: json.dumps(metadata))
    monkeypatch.setattr(
        deployment, "_gcloud", lambda *a, **k: pytest.fail("preflight cloud mutation")
    )

    class Result:
        def __init__(self, value):
            self.value = value

        def mappings(self):
            return self

        def one(self):
            return self.value

        def first(self):
            return self.value

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def execute(self, statement, params=None):
            sql = str(statement)
            statements.append(sql)
            assert sql.startswith("SELECT "), "SQL preflight performed a write"
            if "rolsuper" in sql:
                return Result(
                    {
                        "rolsuper": False,
                        "rolcreatedb": privileged,
                        "rolcreaterole": privileged,
                    }
                )
            if "pg_has_role" in sql:
                return Result({"can_set": can_set, "can_inherit": can_inherit})
            assert "FROM pg_database" in sql
            return Result({"owner": owner} if database_exists else None)

        def scalar(self, statement, params=None):
            sql = str(statement)
            statements.append(sql)
            assert sql.startswith(("SELECT ", "SHOW ")), (
                "SQL preflight performed a write"
            )
            if sql == "SHOW max_connections":
                return max_connections
            assert "FROM pg_roles" in sql
            return 1 if role_exists else None

    class Engine:
        def connect(self):
            return Connection()

        def dispose(self):
            pass

    def engine(url, **kwargs):
        connections.append(url)
        if invalid_target_credentials and url.username == "control":
            raise RuntimeError("bad password control-pass admin-pass")
        return Engine()

    class Inspector:
        def get_table_names(self):
            return list(tables)

    monkeypatch.setattr(deployment, "create_engine", engine)
    monkeypatch.setattr(deployment, "inspect", lambda connection: Inspector())
    return statements, connections


def test_control_preflight_is_read_only_with_existing_empty_owned_database(
    platform_env, monkeypatch
):
    _control_urls(monkeypatch)
    statements, connections = _stub_control_preflight(monkeypatch)
    deployment._control_bootstrap_preflight(
        "test-project", "us-central1", "test-sql", fresh_install=True
    )
    assert any(sql == "SHOW max_connections" for sql in statements)
    assert [url.username for url in connections] == ["provisioner", "control"]


@pytest.mark.parametrize(
    "settings,reason",
    [
        ({"privileged": False}, "create roles and databases"),
        ({"max_connections": 99}, "max_connections"),
        ({"owner": "other_role"}, "different owner"),
        ({"tables": ["workspaces"]}, "already has tables"),
        ({"can_set": False}, "membership"),
        ({"can_inherit": False}, "membership"),
        ({"invalid_target_credentials": True}, "could not be verified"),
    ],
)
def test_control_preflight_refuses_unsafe_operator_or_target_before_writes(
    platform_env, monkeypatch, settings, reason
):
    _control_urls(monkeypatch)
    _stub_control_preflight(monkeypatch, **settings)
    with pytest.raises(deployment.DeployBlocked, match=reason) as error:
        deployment._control_bootstrap_preflight(
            "test-project", "us-central1", "test-sql", fresh_install=True
        )
    assert "control-pass" not in str(error.value)
    assert "admin-pass" not in str(error.value)


def test_control_preflight_refuses_missing_instance_without_opening_sql(
    platform_env, monkeypatch
):
    for name in (
        "CONTROL_DATABASE_URL",
        "CUBIE_PROVISION_ADMIN_DATABASE_URL",
        "CUBIE_RUNTIME_CONTROL_DATABASE_URL",
        "CUBIE_RUNTIME_ADMIN_DATABASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(deployment, "_gcloud_maybe", lambda *a: None)
    monkeypatch.setattr(
        deployment,
        "create_engine",
        lambda *a, **k: pytest.fail("SQL before instance check"),
    )
    with pytest.raises(deployment.DeployBlocked, match="--phase resources"):
        deployment._control_bootstrap_preflight(
            "test-project", "us-central1", "test-sql", fresh_install=True
        )


@pytest.mark.parametrize("owner", ["control", "unrelated_role"])
def test_control_database_retry_preserves_existing_role_credentials(
    platform_env, monkeypatch, owner
):
    _control_urls(monkeypatch)
    writes = []
    connections = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def scalar(self, statement, params=None):
            sql = str(statement)
            if "FROM pg_database" in sql:
                return owner
            assert "FROM pg_roles" in sql, "existing role password was reprocessed"
            return 1

        def exec_driver_sql(self, sql):
            writes.append(sql)

    class Engine:
        def connect(self):
            return Connection()

        def dispose(self):
            pass

    class Inspector:
        def get_table_names(self):
            return []

    monkeypatch.setattr(
        deployment,
        "create_engine",
        lambda url, **kwargs: connections.append(url) or Engine(),
    )
    monkeypatch.setattr(deployment, "inspect", lambda connection: Inspector())
    if owner != "control":
        with pytest.raises(deployment.DeployBlocked, match="different owner"):
            deployment._create_fresh_control_database()
        assert writes == []
        return

    deployment._create_fresh_control_database()
    assert [url.username for url in connections] == [
        "provisioner",
        "control",
        "control",
    ]
    assert writes == [
        'REVOKE CONNECT ON DATABASE "control" FROM PUBLIC',
        'GRANT CONNECT ON DATABASE "control" TO "control"',
    ]


@pytest.mark.parametrize("create_fails", [False, True])
def test_new_control_role_configures_pg16_membership_only_while_creating_role(
    platform_env, monkeypatch, create_fails
):
    _control_urls(monkeypatch)
    writes = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def scalar(self, statement, params=None):
            sql = str(statement)
            if "quote_literal" in sql:
                return "'control-pass'"
            assert "FROM pg_database" in sql or "FROM pg_roles" in sql
            return None

        def exec_driver_sql(self, sql):
            writes.append(sql)
            if create_fails and sql.startswith("CREATE ROLE "):
                raise RuntimeError("simulated role creation failure")

    class Engine:
        def connect(self):
            return Connection()

        def dispose(self):
            pass

    class Inspector:
        def get_table_names(self):
            return []

    monkeypatch.setattr(deployment, "create_engine", lambda *a, **k: Engine())
    monkeypatch.setattr(deployment, "inspect", lambda connection: Inspector())
    if create_fails:
        with pytest.raises(deployment.DeployBlocked, match="creation or inspection"):
            deployment._create_fresh_control_database()
    else:
        deployment._create_fresh_control_database()

    setting = next(
        i for i, sql in enumerate(writes) if sql.startswith("SET createrole_self_grant")
    )
    create = next(i for i, sql in enumerate(writes) if sql.startswith("CREATE ROLE "))
    reset = writes.index("RESET createrole_self_grant")
    assert "set,inherit" in writes[setting].replace(" ", "").lower()
    assert setting < create < reset
    assert reset == create + 1
    if create_fails:
        assert len(writes) == reset + 1


def test_runtime_env_carries_capacity_settings(platform_env):
    env = dict(
        item.split("=", 1)
        for item in deployment._runtime_env(
            "test-project",
            "us-central1",
            "test-sql",
            "a" * 40,
            "client.apps.googleusercontent.com",
            "test-bucket",
        ).split(",")
    )
    for name in (
        "CUBIE_PROVISION_DB_MAX_CONNECTIONS",
        "CUBIE_PROVISION_SERVICE_MAX_INSTANCES",
        "CUBIE_PROVISION_JOB_MAX_INSTANCES",
        "CUBIE_PROVISION_RESERVED_CONNECTIONS",
    ):
        assert env[name] == os.environ[name]


def test_service_deploy_honors_declared_max_instances(
    control, platform_env, monkeypatch
):
    monkeypatch.setenv("CUBIE_PROVISION_SERVICE_MAX_INSTANCES", "2")
    monkeypatch.setenv("CUBIE_PROVISION_DB_MAX_CONNECTIONS", "200")
    monkeypatch.setenv("VITE_MEET_ADDON_CLOUD_PROJECT_NUMBER", "123")
    monkeypatch.setenv("CUBIE_PUBLIC_ORIGIN", "https://app.example.com")
    monkeypatch.setattr(deployment, "check_tree", lambda: "a" * 40)
    monkeypatch.setattr(deployment, "_runtime_control_url", lambda *a: "control-url")
    monkeypatch.setattr(deployment, "_runtime_admin_url", lambda *a: "admin-url")
    monkeypatch.setattr(deployment, "_validate_release_secrets", lambda *a: None)
    monkeypatch.setattr(
        deployment,
        "_secret_value",
        lambda project, name: {
            "CUBIE_CONTROL_DATABASE_URL": "control-url",
            "CUBIE_PROVISION_ADMIN_DATABASE_URL": "admin-url",
        }.get(name, "secret"),
    )
    monkeypatch.setattr(deployment, "_schedules", lambda *a, **k: 0)

    class CapturedService(Exception):
        pass

    def cloud(*args, **kwargs):
        if args[:3] == ("container", "images", "describe"):
            return "sha256:" + "b" * 64
        if args[:2] == ("run", "deploy"):
            assert "--max-instances=2" in args
            assert "--max-instances=3" not in args
            raise CapturedService
        return ""

    monkeypatch.setattr(deployment, "_gcloud", cloud)
    with pytest.raises(CapturedService):
        deployment.deploy(dry_run=False)


def test_control_revision_must_match_an_exact_file(control):
    with control.begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num='head'"))
    with pytest.raises(deployment.DeployBlocked, match="unknown") as error:
        deployment.read_control()
    assert error.value.code == 5


def test_unknown_workspace_revision_stops_before_build(control):
    _workspace(control, "acme")
    with control.begin() as connection:
        connection.execute(
            text("UPDATE workspaces SET schema_revision='future_revision'")
        )
    with pytest.raises(deployment.DeployBlocked, match="Workspace revision") as error:
        deployment.read_control()
    assert error.value.code == 5


def test_migrate_rejects_any_other_cloud_run_job(monkeypatch):
    monkeypatch.setenv("CLOUD_RUN_JOB", "cubie-worker")
    with pytest.raises(deployment.DeployBlocked, match="dedicated"):
        deployment.migrate()


def test_runtime_control_url_rejects_host_and_extra_query(platform_env, monkeypatch):
    monkeypatch.setenv(
        "CONTROL_DATABASE_URL", "postgresql+pg8000://role:pass@127.0.0.1:5432/control"
    )
    socket = "/cloudsql/test-project:us-central1:test-sql/.s.PGSQL.5432"
    valid = f"postgresql+pg8000://role:pass@/control?unix_sock={socket}"
    monkeypatch.setenv("CUBIE_RUNTIME_CONTROL_DATABASE_URL", valid)
    assert (
        deployment._runtime_control_url("test-project", "us-central1", "test-sql")
        == valid
    )
    monkeypatch.setenv(
        "CUBIE_RUNTIME_CONTROL_DATABASE_URL", valid.replace("@/", "@evil.example/")
    )
    with pytest.raises(deployment.DeployBlocked):
        deployment._runtime_control_url("test-project", "us-central1", "test-sql")
    monkeypatch.setenv("CUBIE_RUNTIME_CONTROL_DATABASE_URL", valid + "&sslmode=disable")
    with pytest.raises(deployment.DeployBlocked):
        deployment._runtime_control_url("test-project", "us-central1", "test-sql")
    monkeypatch.setenv("CUBIE_RUNTIME_CONTROL_DATABASE_URL", valid)
    monkeypatch.setenv(
        "CUBIE_OPERATOR_SQL_INSTANCE", "other-project:us-central1:test-sql"
    )
    with pytest.raises(deployment.DeployBlocked):
        deployment._runtime_control_url("test-project", "us-central1", "test-sql")


def test_runtime_admin_url_is_service_only_secret(platform_env, monkeypatch):
    monkeypatch.setenv(
        "CUBIE_PROVISION_ADMIN_DATABASE_URL",
        "postgresql+pg8000://admin:pass@127.0.0.1:5432/postgres",
    )
    socket = "/cloudsql/test-project:us-central1:test-sql/.s.PGSQL.5432"
    runtime = f"postgresql+pg8000://admin:pass@/postgres?unix_sock={socket}"
    monkeypatch.setenv("CUBIE_RUNTIME_ADMIN_DATABASE_URL", runtime)
    assert (
        deployment._runtime_admin_url("test-project", "us-central1", "test-sql")
        == runtime
    )
    assert "CUBIE_PROVISION_ADMIN_DATABASE_URL=" in deployment._service_secrets()
    assert "CUBIE_PROVISION_ADMIN_DATABASE_URL=" not in deployment._runtime_secrets()
    env = deployment._runtime_env(
        "test-project",
        "us-central1",
        "test-sql",
        "a" * 40,
        "client.apps.googleusercontent.com",
        "test-bucket",
        "https://app.example.com",
    )
    assert "CUBIE_PROVISION_SERVICE_URL=https://app.example.com" in env
    assert "CUBIE_PROVISION_REGION=us-central1" in env


def test_optional_gmail_oauth_only_binds_complete_configuration(
    platform_env, monkeypatch
):
    assert "GOOGLE_GMAIL_OAUTH_CLIENT_SECRET=" not in deployment._service_secrets()
    monkeypatch.setenv(
        "GOOGLE_GMAIL_OAUTH_CLIENT_ID", "gmail.apps.googleusercontent.com"
    )
    with pytest.raises(deployment.DeployBlocked, match="requires"):
        deployment._service_secrets()
    monkeypatch.setenv("GOOGLE_GMAIL_OAUTH_CLIENT_SECRET", "private-secret")
    monkeypatch.setenv(
        "GOOGLE_GMAIL_OAUTH_REDIRECT_URI",
        "https://app.example.com/api/email/accounts/callback",
    )
    assert "GOOGLE_GMAIL_OAUTH_CLIENT_SECRET=" in deployment._service_secrets()
    assert "GOOGLE_GMAIL_OAUTH_CLIENT_SECRET=" not in deployment._runtime_secrets()
    env = deployment._runtime_env(
        "test-project",
        "us-central1",
        "test-sql",
        "a" * 40,
        "client.apps.googleusercontent.com",
        "test-bucket",
        "https://app.example.com",
    )
    assert "GOOGLE_GMAIL_OAUTH_CLIENT_ID=gmail.apps.googleusercontent.com" in env
    assert (
        "GOOGLE_GMAIL_OAUTH_REDIRECT_URI=https://app.example.com/api/email/accounts/callback"
        in env
    )
    assert "private-secret" not in env
    monkeypatch.setenv(
        "GOOGLE_GMAIL_OAUTH_REDIRECT_URI", "https://app.example.com/api/gmail/callback"
    )
    with pytest.raises(deployment.DeployBlocked, match="configuration is invalid"):
        deployment._service_secrets()
    monkeypatch.setenv(
        "GOOGLE_GMAIL_OAUTH_REDIRECT_URI",
        "https://app.example.com/api/gmail,APP_MODE=local",
    )
    with pytest.raises(deployment.DeployBlocked, match="invalid"):
        deployment._runtime_env(
            "test-project",
            "us-central1",
            "test-sql",
            "a" * 40,
            "client.apps.googleusercontent.com",
            "test-bucket",
            "https://app.example.com",
        )


def test_schedule_reconcile_uses_secret_version_over_stale_operator_env(
    control, platform_env, monkeypatch
):
    from rubik_core.platform import executor, schedules

    monkeypatch.setenv("SCHEDULER_API_KEY", "stale-key")
    monkeypatch.setattr(deployment, "_secret_value", lambda *_a: "current-key")
    monkeypatch.setattr(executor, "CloudExecutor", lambda: object())
    observed = []

    def reconcile(_manifest, _active, _executor):
        observed.append(os.environ["SCHEDULER_API_KEY"])
        return []

    monkeypatch.setattr(schedules, "reconcile_schedules", reconcile)
    assert (
        deployment._schedules(
            deployment.read_control(), dry_run=False, service_url="https://example.com"
        )
        == 0
    )
    assert observed == ["current-key"]
    assert os.environ["SCHEDULER_API_KEY"] == "stale-key"


def test_clean_commit_required_and_unknown_revision_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(deployment, "ROOT", tmp_path)
    with pytest.raises(deployment.DeployBlocked) as unknown:
        deployment.check_tree()
    assert unknown.value.code == 4
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "test"], check=True
    )
    (tmp_path / "readme").write_text("ready\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "readme"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "base"], check=True)
    assert len(deployment.check_tree()) == 40
    (tmp_path / "readme").write_text("dirty\n")
    with pytest.raises(deployment.DeployBlocked) as dirty:
        deployment.check_tree()
    assert dirty.value.code == 4


def test_manifest_covers_every_cloud_run_job_and_uses_one_image_digest(platform_env):
    jobs = deployment.load_jobs()
    assert {job["name"] for job in jobs} == {
        "cubie-migrate",
        "cubie-worker",
        "cubie-priorities-batch",
        "cubie-deep-prep",
        "cubie-xray-deep",
    }
    image = "gcr.io/test-project/cubie@sha256:" + "a" * 64
    args = [
        deployment._job_args(
            job,
            image,
            "test-project",
            "us-central1",
            "test-sql",
            "cubie-runtime@test-project.iam.gserviceaccount.com",
            "APP_MODE=gcp",
        )
        for job in jobs
    ]
    assert all(f"--image={image}" in command for command in args)
    assert "--args=-m,rubik_core.platform.deployment,migrate" in args[0]
    assert all("CUBIE_WORKSPACE=" not in " ".join(command) for command in args)
    assert all(
        "CUBIE_SESSION_SECRET=CUBIE_SESSION_SECRET:latest" in " ".join(command)
        for command in args
    )

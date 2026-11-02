"""Task 19 release guards and dry runs; never invoke gcloud or mutate SQL."""

from __future__ import annotations

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


def test_bootstrap_fresh_dry_run_is_write_free(tmp_path, platform_env, monkeypatch):
    target = tmp_path / "control.db"
    monkeypatch.setenv("CONTROL_DATABASE_URL", f"sqlite:///{target}")
    monkeypatch.setattr(
        deployment, "_gcloud", lambda *_a, **_k: pytest.fail("gcloud in dry run")
    )
    deployment.bootstrap(dry_run=True, fresh_install=True)
    assert not target.exists()


def test_bootstrap_refuses_to_baseline_an_existing_schema(control, platform_env):
    with pytest.raises(deployment.DeployBlocked, match="already has tables") as error:
        deployment.bootstrap(dry_run=True, fresh_install=True)
    assert error.value.code == 7


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
        deployment.bootstrap(dry_run=False, fresh_install=False)


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

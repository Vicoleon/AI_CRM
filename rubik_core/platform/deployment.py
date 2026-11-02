"""Guarded platform release entry point. No import or dry-run mutates cloud/SQL.

The serving process never runs Alembic. Only the dedicated Cloud Run migration
Job invokes ``migrate``; deploy reads the control registry before building.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[2]
JOBS_MANIFEST = ROOT / "deploy" / "jobs.yaml"
_DAY = {
    name: i for i, name in enumerate(("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"))
}
_REV = re.compile(r"[a-zA-Z0-9_]{1,32}\Z")


class DeployBlocked(RuntimeError):
    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ControlState:
    revision: str
    workspace_head: str
    control_head: str
    workspaces: tuple[WorkspaceState, ...]


@dataclass(frozen=True)
class WorkspaceState:
    slug: str
    status: str
    freeze_windows: list[dict]
    time_zone: str
    schema_revision: str | None


def _head(ini: str) -> tuple[ScriptDirectory, str]:
    scripts = ScriptDirectory.from_config(Config(str(ROOT / ini)))
    heads = scripts.get_heads()
    if len(heads) != 1:
        raise DeployBlocked(f"{ini} must have exactly one migration head", 5)
    return scripts, heads[0]


def read_control(*, require_current: bool = False) -> ControlState:
    """Read existing control tables; never call create_all or Alembic here."""
    url = os.environ.get("CONTROL_DATABASE_URL", "").strip()
    if not url:
        raise DeployBlocked("CONTROL_DATABASE_URL is required", 7)
    try:
        parsed_url = make_url(url)
    except Exception:
        raise DeployBlocked("CONTROL_DATABASE_URL is invalid", 7) from None
    if parsed_url.get_backend_name() == "sqlite" and parsed_url.database != ":memory:":
        if not Path(parsed_url.database or "").exists():
            raise DeployBlocked("Control database is absent", 7)
    control_scripts, control_head = _head("alembic_control.ini")
    workspace_scripts, workspace_head = _head("alembic.ini")
    known_workspace_revisions = {
        item.revision for item in workspace_scripts.walk_revisions()
    }
    try:
        engine = create_engine(url, pool_pre_ping=True)
        try:
            with engine.connect() as connection:
                tables = set(inspect(connection).get_table_names())
                if not {"workspaces", "alembic_version"}.issubset(tables):
                    raise DeployBlocked("Control database is not migrated", 7)
                revisions = MigrationContext.configure(connection).get_current_heads()
                if len(revisions) != 1 or not _REV.fullmatch(revisions[0]):
                    raise DeployBlocked("Control revision is missing or invalid", 7)
                revision = revisions[0]
                if revision not in {
                    item.revision for item in control_scripts.walk_revisions()
                }:
                    raise DeployBlocked("Control revision is unknown to this image", 5)
                if require_current and revision != control_head:
                    raise DeployBlocked(
                        "Control migration Job did not reach image head", 5
                    )
                # Select only baseline columns, so a new image can inspect the
                # previous control schema before its migration Job runs.
                raw_rows = connection.execute(
                    text(
                        "SELECT slug, status, freeze_windows, time_zone, schema_revision "
                        "FROM workspaces ORDER BY slug"
                    )
                ).mappings()
                rows = []
                for item in raw_rows:
                    windows = item["freeze_windows"]
                    if isinstance(windows, str):
                        windows = json.loads(windows)
                    rows.append(
                        WorkspaceState(
                            slug=item["slug"],
                            status=item["status"],
                            freeze_windows=windows,
                            time_zone=item["time_zone"],
                            schema_revision=item["schema_revision"],
                        )
                    )
                rows = tuple(rows)
                for row in rows:
                    if row.status not in ("active", "suspended", "provisioning"):
                        raise DeployBlocked("Control workspace status is invalid", 7)
                    if not isinstance(row.freeze_windows, list):
                        raise DeployBlocked("Control freeze data is unreadable", 7)
                    if not isinstance(row.time_zone, str):
                        raise DeployBlocked("Control workspace time zone is invalid", 7)
                    try:
                        ZoneInfo(row.time_zone)
                    except (ZoneInfoNotFoundError, ValueError):
                        raise DeployBlocked(
                            "Control workspace time zone is invalid", 7
                        ) from None
                    if (
                        row.schema_revision
                        and row.schema_revision not in known_workspace_revisions
                    ):
                        raise DeployBlocked(
                            "Workspace revision is unknown to this image", 5
                        )
                    if (
                        require_current
                        and row.status in ("active", "suspended")
                        and row.schema_revision != workspace_head
                    ):
                        raise DeployBlocked(
                            "Workspace migration Job did not reach image head", 5
                        )
                return ControlState(revision, workspace_head, control_head, rows)
        finally:
            engine.dispose()
    except DeployBlocked:
        raise
    except Exception:
        # SQLAlchemy errors may include the database URL and password. Never
        # include them in operator output or an exception chain.
        raise DeployBlocked(
            "Control database or freeze data is unreadable", 7
        ) from None


def _freeze_active(window: dict, now: datetime) -> bool:
    if not isinstance(window, dict) or set(window) != {
        "day",
        "start",
        "end",
        "time_zone",
    }:
        raise DeployBlocked("Control freeze window is malformed", 7)
    day = window["day"]
    start = window["start"]
    end = window["end"]
    zone = window["time_zone"]
    if (
        not all(isinstance(value, str) for value in (day, start, end, zone))
        or day not in _DAY
    ):
        raise DeployBlocked("Control freeze window is malformed", 7)
    if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", start) or not re.fullmatch(
        r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", end
    ):
        raise DeployBlocked("Control freeze window is malformed", 7)
    try:
        local = now.astimezone(ZoneInfo(zone))
    except (ZoneInfoNotFoundError, ValueError):
        raise DeployBlocked("Control freeze time zone is invalid", 7) from None
    start_min = int(start[:2]) * 60 + int(start[3:])
    end_min = int(end[:2]) * 60 + int(end[3:])
    if start_min >= end_min:
        raise DeployBlocked("Control freeze window is malformed", 7)
    now_week = local.weekday() * 1440 + local.hour * 60 + local.minute
    opens = _DAY[day] * 1440 + start_min - 20
    return (now_week - opens) % (7 * 1440) < (end_min - start_min + 20)


def check_freezes(state: ControlState, *, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    blocked: list[str] = []
    legacy = os.environ.get("CUBIE_DEPLOY_FREEZE", "").strip()
    if legacy:
        match = re.fullmatch(
            r"(MON|TUE|WED|THU|FRI|SAT|SUN) (\d\d:\d\d)-(\d\d:\d\d) ([A-Za-z0-9_+/\-]+)",
            legacy,
        )
        if match is None:
            raise DeployBlocked("Platform freeze window is malformed", 7)
        if _freeze_active(
            dict(zip(("day", "start", "end", "time_zone"), match.groups())), now
        ):
            blocked.append("platform")
    for row in state.workspaces:
        if row.status != "active":
            continue
        for window in row.freeze_windows:
            if _freeze_active(window, now):
                blocked.append(row.slug)
    if blocked and not os.environ.get("DEPLOY_FREEZE_OVERRIDE_REASON", "").strip():
        raise DeployBlocked("Deploy freeze is active for: " + ", ".join(blocked), 7)


def check_tree() -> str:
    """Require an exact, committed image identity even without origin/main."""
    try:
        sha = subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        status = subprocess.check_output(
            ["git", "-C", str(ROOT), "status", "--porcelain"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        raise DeployBlocked("A known Git commit is required", 4) from None
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise DeployBlocked("A known Git commit is required", 4)
    if status:
        raise DeployBlocked(
            "Deploy tree is dirty; commit the reviewed release first", 4
        )
    return sha


def load_jobs(path: Path = JOBS_MANIFEST) -> list[dict]:
    manifest = yaml.safe_load(path.read_text())
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise DeployBlocked("Invalid Job manifest")
    jobs = manifest.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise DeployBlocked("Job manifest has no jobs")
    names: set[str] = set()
    for job in jobs:
        if (
            not isinstance(job, dict)
            or not isinstance(job.get("name"), str)
            or not re.fullmatch(r"cubie-[a-z0-9-]+", job["name"])
        ):
            raise DeployBlocked("Job manifest has an invalid name")
        if (
            job["name"] in names
            or not isinstance(job.get("module"), str)
            or not re.fullmatch(r"[a-z_][a-z0-9_.]+", job["module"])
        ):
            raise DeployBlocked("Job manifest has a duplicate or invalid module")
        if not isinstance(job.get("args", []), list) or not all(
            isinstance(value, str) and re.fullmatch(r"[a-z0-9_-]+", value)
            for value in job.get("args", [])
        ):
            raise DeployBlocked("Job manifest has invalid arguments")
        for field in ("timeout", "tasks", "parallelism"):
            if field in job and (not isinstance(job[field], int) or job[field] < 1):
                raise DeployBlocked("Job manifest has invalid resources")
        names.add(job["name"])
    if "cubie-migrate" not in names:
        raise DeployBlocked("Job manifest must include cubie-migrate")
    return jobs


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise DeployBlocked(f"{name} is required")
    return value


def _validate_targets(
    project: str,
    region: str,
    instance: str,
    bucket: str,
    runtime_account: str,
) -> None:
    if (
        not re.fullmatch(r"[a-z][a-z0-9-]{4,62}", project)
        or not re.fullmatch(r"[a-z]+-[a-z]+[0-9]+", region)
        or not re.fullmatch(r"[a-z][a-z0-9-]{0,97}", instance)
        or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket)
        or runtime_account != f"cubie-runtime@{project}.iam.gserviceaccount.com"
    ):
        raise DeployBlocked(
            "Platform project, region, SQL instance, bucket or identity is invalid"
        )


def _runtime_database_url(
    operator_name: str,
    runtime_name: str,
    project: str,
    region: str,
    instance: str,
) -> str:
    """Keep an operator proxy URL distinct from its Cloud Run socket URL."""
    try:
        operator = make_url(_required(operator_name))
        runtime = make_url(_required(runtime_name))
    except Exception:
        raise DeployBlocked("Platform database URLs are invalid") from None
    socket = f"/cloudsql/{project}:{region}:{instance}/.s.PGSQL.5432"
    operator_instance = _required("CUBIE_OPERATOR_SQL_INSTANCE")
    if (
        operator_instance != f"{project}:{region}:{instance}"
        or operator.drivername != "postgresql+pg8000"
        or operator.host not in ("127.0.0.1", "localhost", "::1")
        or not operator.port
        or operator.query
        or not all((operator.database, operator.username, operator.password))
        or runtime.drivername != "postgresql+pg8000"
        or runtime.host is not None
        or runtime.port is not None
        or dict(runtime.query) != {"unix_sock": socket}
        or (runtime.database, runtime.username, runtime.password)
        != (operator.database, operator.username, operator.password)
    ):
        raise DeployBlocked(
            "Runtime database URL must use the declared Cloud SQL socket and operator DB identity"
        )
    return _required(runtime_name)


def _runtime_control_url(project: str, region: str, instance: str) -> str:
    return _runtime_database_url(
        "CONTROL_DATABASE_URL",
        "CUBIE_RUNTIME_CONTROL_DATABASE_URL",
        project,
        region,
        instance,
    )


def _runtime_admin_url(project: str, region: str, instance: str) -> str:
    return _runtime_database_url(
        "CUBIE_PROVISION_ADMIN_DATABASE_URL",
        "CUBIE_RUNTIME_ADMIN_DATABASE_URL",
        project,
        region,
        instance,
    )


def _gmail_oauth_config() -> tuple[str, str, str] | None:
    names = (
        "GOOGLE_GMAIL_OAUTH_CLIENT_ID",
        "GOOGLE_GMAIL_OAUTH_CLIENT_SECRET",
        "GOOGLE_GMAIL_OAUTH_REDIRECT_URI",
    )
    values = tuple(os.environ.get(name, "").strip() for name in names)
    if not any(values):
        return None
    if not all(values):
        raise DeployBlocked("Gmail OAuth requires client ID, secret and redirect URI")
    client_id, secret, redirect = values
    parsed = urlsplit(redirect)
    if (
        not re.fullmatch(r"[A-Za-z0-9.-]+\.apps\.googleusercontent\.com", client_id)
        or not secret
        or parsed.scheme != "https"
        or not parsed.hostname
        or not re.fullmatch(r"[A-Za-z0-9.-]+", parsed.hostname)
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path != "/api/email/accounts/callback"
    ):
        raise DeployBlocked("Gmail OAuth configuration is invalid")
    return client_id, secret, redirect


def _gcloud(*args: str, label: str, input_text: str | None = None) -> str:
    try:
        done = subprocess.run(
            ["gcloud", *args],
            input=input_text,
            text=True,
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        # Output may include an env var, token, URL or SQL password.
        raise DeployBlocked(f"{label} failed") from None
    return done.stdout.strip()


def _gcloud_maybe(*args: str) -> str | None:
    """Read an optional cloud resource; never surface its command output."""
    try:
        result = subprocess.run(["gcloud", *args], text=True, capture_output=True)
    except OSError:
        raise DeployBlocked("gcloud is unavailable") from None
    return result.stdout.strip() if result.returncode == 0 else None


def _secret_value(project: str, name: str) -> str:
    value = _gcloud(
        "secrets",
        "versions",
        "access",
        "latest",
        f"--secret={name}",
        f"--project={project}",
        label=f"{name} read",
    )
    if not value:
        raise DeployBlocked(f"{name} has no usable version")
    return value


def _validate_release_secrets(project: str) -> None:
    from rubik_core.tenancy.platform_secrets import validate_platform_secrets

    names = (
        "CUBIE_SESSION_SECRET",
        "CUBIE_OAUTH_STATE_SECRET",
        "CUBIE_PERSONAL_TOKEN_FERNET_KEY",
    )
    previous = {name: os.environ.get(name) for name in names}
    try:
        for name in names:
            os.environ[name] = _secret_value(project, name)
        validate_platform_secrets()
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _runtime_env(
    project: str,
    region: str,
    instance: str,
    sha: str,
    client_id: str,
    bucket: str,
    public_origin: str = "",
) -> str:
    values = {
        "APP_MODE": "gcp",
        "GIT_SHA": sha[:12],
        "GCP_PROJECT_ID": project,
        "GCP_REGION": region,
        "CLOUDSQL_CONNECTION_NAME": f"{project}:{region}:{instance}",
        "GCS_BUCKET_NAME": bucket,
        "GOOGLE_CLIENT_ID": client_id,
        "AUTO_MIGRATE": "false",
        "ENABLE_MULTI_AGENT_RUNTIME": "true",
        "SCHEDULED_TASKS_ENABLED": "true",
        "MEETING_SKILLS_ENABLED": "true",
        "TODOS_ENABLED": "true",
        "PROMISE_ACCOUNTABILITY_ENABLED": "true",
        "PROPOSALS_ENABLED": "true",
        "CCS_ENABLED": "true",
        "CCS_MEET_ENABLED": "true",
        "MEET_ADDON_ENABLED": "true",
        "LOOP_CLOSURE_ENABLED": "true",
        "WHATS_NEW_ENABLED": "true",
        "CHROME_EXTENSION_ENABLED": "true",
    }
    if public_origin:
        values["FRONTEND_URL"] = public_origin
        values["CUBIE_PROVISION_SERVICE_URL"] = public_origin
    values["CUBIE_PROVISION_REGION"] = region
    values["CUBIE_PROVISION_RUNTIME_SERVICE_ACCOUNT"] = (
        f"cubie-runtime@{project}.iam.gserviceaccount.com"
    )
    gmail = _gmail_oauth_config()
    if gmail:
        values["GOOGLE_GMAIL_OAUTH_CLIENT_ID"] = gmail[0]
        values["GOOGLE_GMAIL_OAUTH_REDIRECT_URI"] = gmail[2]
    return ",".join(f"{key}={value}" for key, value in values.items())


def _runtime_secrets() -> str:
    return ",".join(
        (
            "CONTROL_DATABASE_URL=CUBIE_CONTROL_DATABASE_URL:latest",
            "CUBIE_SESSION_SECRET=CUBIE_SESSION_SECRET:latest",
            "CUBIE_OAUTH_STATE_SECRET=CUBIE_OAUTH_STATE_SECRET:latest",
            "CUBIE_PERSONAL_TOKEN_FERNET_KEY=CUBIE_PERSONAL_TOKEN_FERNET_KEY:latest",
            "SCHEDULER_API_KEY=SCHEDULER_API_KEY:latest",
        )
    )


def _service_secrets() -> str:
    secrets = [
        _runtime_secrets(),
        "CUBIE_PROVISION_ADMIN_DATABASE_URL=CUBIE_PROVISION_ADMIN_DATABASE_URL:latest",
    ]
    if _gmail_oauth_config():
        secrets.append(
            "GOOGLE_GMAIL_OAUTH_CLIENT_SECRET=GOOGLE_GMAIL_OAUTH_CLIENT_SECRET:latest"
        )
    return ",".join(secrets)


def _job_args(
    job: dict,
    image: str,
    project: str,
    region: str,
    instance: str,
    runtime_account: str,
    env: str,
) -> list[str]:
    return [
        "run",
        "jobs",
        "deploy",
        job["name"],
        "--quiet",
        f"--project={project}",
        f"--region={region}",
        f"--image={image}",
        f"--service-account={runtime_account}",
        f"--set-cloudsql-instances={project}:{region}:{instance}",
        "--command=python",
        f"--args=-m,{job['module']}"
        + ("," + ",".join(job["args"]) if job.get("args") else ""),
        f"--task-timeout={int(job.get('timeout', 3600))}",
        f"--tasks={int(job.get('tasks', 1))}",
        f"--parallelism={int(job.get('parallelism', 1))}",
        "--max-retries=1",
        "--cpu=2",
        "--memory=2Gi",
        f"--set-env-vars={env}",
        f"--set-secrets={_runtime_secrets()}",
    ]


def _schedules(state: ControlState, *, dry_run: bool, service_url: str = "") -> int:
    from rubik_core.platform.schedules import load_manifest, reconcile_schedules

    active = [row for row in state.workspaces if row.status == "active"]
    if dry_run:
        return len(reconcile_schedules(load_manifest(), active, object(), dry_run=True))
    from rubik_core.platform.executor import CloudExecutor

    os.environ["CUBIE_PROVISION_SERVICE_URL"] = service_url
    os.environ["CUBIE_PROVISION_REGION"] = _required("GCP_REGION")
    os.environ["CUBIE_PROVISION_RUNTIME_SERVICE_ACCOUNT"] = _required(
        "CUBIE_RUNTIME_SERVICE_ACCOUNT"
    )
    previous = os.environ.get("SCHEDULER_API_KEY")
    try:
        os.environ["SCHEDULER_API_KEY"] = _secret_value(
            _required("GCP_PROJECT_ID"), "SCHEDULER_API_KEY"
        )
        return len(reconcile_schedules(load_manifest(), active, CloudExecutor()))
    finally:
        if previous is None:
            os.environ.pop("SCHEDULER_API_KEY", None)
        else:
            os.environ["SCHEDULER_API_KEY"] = previous


def deploy(*, dry_run: bool) -> None:
    project = _required("GCP_PROJECT_ID")
    region = _required("GCP_REGION")
    instance = _required("CLOUDSQL_INSTANCE")
    bucket = _required("GCS_BUCKET_NAME")
    client_id = _required("GOOGLE_CLIENT_ID")
    runtime_account = _required("CUBIE_RUNTIME_SERVICE_ACCOUNT")
    _validate_targets(project, region, instance, bucket, runtime_account)
    if not re.fullmatch(r"[A-Za-z0-9.-]+\.apps\.googleusercontent\.com", client_id):
        raise DeployBlocked("GOOGLE_CLIENT_ID is invalid")
    state = read_control()
    check_freezes(state)
    sha = check_tree()
    jobs = load_jobs()
    gmail = _gmail_oauth_config()
    if not dry_run:
        _required("VITE_MEET_ADDON_CLOUD_PROJECT_NUMBER")
        if not re.fullmatch(
            r"[0-9]+", os.environ["VITE_MEET_ADDON_CLOUD_PROJECT_NUMBER"]
        ):
            raise DeployBlocked("VITE_MEET_ADDON_CLOUD_PROJECT_NUMBER must be numeric")
        control_url = _runtime_control_url(project, region, instance)
        admin_url = _runtime_admin_url(project, region, instance)
        public_origin = _required("CUBIE_PUBLIC_ORIGIN")
        parsed_origin = urlsplit(public_origin)
        if (
            parsed_origin.scheme != "https"
            or not parsed_origin.hostname
            or not re.fullmatch(r"[A-Za-z0-9.-]+", parsed_origin.hostname)
            or parsed_origin.username
            or parsed_origin.password
            or parsed_origin.path not in ("", "/")
            or parsed_origin.query
            or parsed_origin.fragment
        ):
            raise DeployBlocked("CUBIE_PUBLIC_ORIGIN must be one HTTPS origin")
        os.environ["APP_MODE"] = "gcp"
        _validate_release_secrets(project)
        _secret_value(project, "SCHEDULER_API_KEY")
        if _secret_value(project, "CUBIE_CONTROL_DATABASE_URL") != control_url:
            raise DeployBlocked("Control URL secret differs from declared target")
        if _secret_value(project, "CUBIE_PROVISION_ADMIN_DATABASE_URL") != admin_url:
            raise DeployBlocked("Admin URL secret differs from declared target")
        if (
            gmail
            and _secret_value(project, "GOOGLE_GMAIL_OAUTH_CLIENT_SECRET") != gmail[1]
        ):
            raise DeployBlocked("Gmail OAuth secret differs from declared value")
    else:
        public_origin = ""
    if state.revision == state.control_head:
        count = _schedules(state, dry_run=True)
        schedule_label = "schedules"
    else:
        from rubik_core.platform.schedules import load_manifest

        count = len(load_manifest()["schedules"])
        schedule_label = "schedule definitions (expanded after migration)"
    image_tag = f"gcr.io/{project}/cubie:{sha[:12]}"
    print(
        f"Preflight passed: {len(state.workspaces)} registered workspaces; {count} {schedule_label}; {len(jobs)} Jobs; commit {sha[:12]}"
    )
    if dry_run:
        print(
            "DRY RUN: build one image, migrate all registered databases, deploy service and Jobs, reconcile schedules, check /health"
        )
        return
    _gcloud(
        "builds",
        "submit",
        str(ROOT),
        "--config",
        str(ROOT / "deploy" / "cloudbuild.yaml"),
        f"--project={project}",
        f"--substitutions=_IMAGE={image_tag},_VITE_GOOGLE_CLIENT_ID={client_id},_VITE_MEET_ADDON_CLOUD_PROJECT_NUMBER={_required('VITE_MEET_ADDON_CLOUD_PROJECT_NUMBER')}",
        label="Cloud Build",
    )
    digest = _gcloud(
        "container",
        "images",
        "describe",
        image_tag,
        "--format=value(image_summary.digest)",
        f"--project={project}",
        label="Image digest lookup",
    )
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise DeployBlocked("Built image digest is missing")
    image = f"gcr.io/{project}/cubie@{digest}"
    env = _runtime_env(project, region, instance, sha, client_id, bucket, public_origin)
    migration_job = next(job for job in jobs if job["name"] == "cubie-migrate")
    check_freezes(read_control())
    _gcloud(
        *_job_args(
            migration_job, image, project, region, instance, runtime_account, env
        ),
        label="Migration Job deploy",
    )
    check_freezes(read_control())
    _gcloud(
        "run",
        "jobs",
        "execute",
        "cubie-migrate",
        f"--region={region}",
        f"--project={project}",
        "--wait",
        label="Migration Job execution",
    )
    state = read_control(require_current=True)
    check_freezes(state)
    _gcloud(
        "run",
        "deploy",
        "cubie",
        "--quiet",
        f"--project={project}",
        f"--region={region}",
        f"--image={image}",
        "--allow-unauthenticated",
        f"--service-account={runtime_account}",
        f"--set-cloudsql-instances={project}:{region}:{instance}",
        "--memory=2Gi",
        "--cpu=2",
        "--min-instances=1",
        "--max-instances=3",
        "--concurrency=8",
        "--timeout=300",
        f"--update-labels=commit={sha[:12]}",
        f"--set-env-vars={env},CUBIE_CONTROL_SCHEMA_REVISION={state.revision},CUBIE_WORKSPACE_SCHEMA_REVISION={state.workspace_head}",
        f"--set-secrets={_service_secrets()}",
        label="Service deploy",
    )
    try:
        service = json.loads(
            _gcloud(
                "run",
                "services",
                "describe",
                "cubie",
                f"--region={region}",
                f"--project={project}",
                "--format=json",
                label="Serving revision lookup",
            )
        )
        status = service["status"]
        latest = status["latestReadyRevisionName"]
        if not any(
            item.get("percent") == 100
            and (item.get("latestRevision") or item.get("revisionName") == latest)
            for item in status["traffic"]
        ):
            raise DeployBlocked(
                "New Service revision is not serving all traffic; Jobs were not changed",
                6,
            )
    except (KeyError, ValueError, TypeError):
        raise DeployBlocked(
            "Serving revision status is unreadable; Jobs were not changed", 6
        ) from None
    service_url = _gcloud(
        "run",
        "services",
        "describe",
        "cubie",
        f"--region={region}",
        f"--project={project}",
        "--format=value(status.url)",
        label="Service URL lookup",
    )
    if not service_url.startswith("https://"):
        raise DeployBlocked("Service URL is invalid")
    for job in jobs:
        if job["name"] == "cubie-migrate":
            continue
        check_freezes(read_control())
        _gcloud(
            *_job_args(job, image, project, region, instance, runtime_account, env),
            label=f"Job {job['name']} deploy",
        )
    check_freezes(read_control())
    _schedules(state, dry_run=False, service_url=service_url)
    import requests

    try:
        response = requests.get(
            f"{service_url}/health", timeout=20, allow_redirects=False
        )
        payload = response.json()
    except (requests.RequestException, ValueError):
        raise DeployBlocked("Service health check failed") from None
    if (
        response.status_code != 200
        or payload.get("git_sha") != sha[:12]
        or payload.get("control_schema_revision") != state.revision
    ):
        raise DeployBlocked("Serving revision or control schema does not match release")
    print(
        f"Deployed {sha[:12]} to {service_url} with one image digest and {len(jobs)} Jobs"
    )


def migrate() -> None:
    if os.environ.get("CLOUD_RUN_JOB") != "cubie-migrate":
        raise DeployBlocked("migrate runs only in the dedicated Cloud Run Job")
    from rubik_core.platform.migrate import migrate_all

    result = migrate_all()
    print(f"Migrated control and {len(result)} registered workspace databases")


def preflight() -> None:
    state = read_control()
    check_freezes(state)
    sha = check_tree()
    print(
        f"Control preflight passed: {len(state.workspaces)} workspaces; commit {sha[:12]}"
    )


def _create_fresh_control_database() -> None:
    """Create one empty control DB/role from a separate admin connection."""
    target = make_url(_required("CONTROL_DATABASE_URL"))
    admin = make_url(_required("CUBIE_PROVISION_ADMIN_DATABASE_URL"))
    if (
        target.get_backend_name() != "postgresql"
        or admin.get_backend_name() != "postgresql"
    ):
        raise DeployBlocked("Fresh control target must be PostgreSQL")
    if (target.host, target.port, target.query) != (
        admin.host,
        admin.port,
        admin.query,
    ):
        raise DeployBlocked("Control and admin URLs must target the same SQL instance")
    user, database, password = (
        target.username or "",
        target.database or "",
        target.password or "",
    )
    if (
        not all(
            re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", item) for item in (user, database)
        )
        or not password
    ):
        raise DeployBlocked("Control URL needs a named database, role and password")
    try:
        engine = create_engine(admin, isolation_level="AUTOCOMMIT")
        try:
            with engine.connect() as connection:
                existing_database = (
                    connection.scalar(
                        text("SELECT 1 FROM pg_database WHERE datname=:name"),
                        {"name": database},
                    )
                    is not None
                )
                if existing_database:
                    inspect_engine = create_engine(admin.set(database=database))
                    try:
                        with inspect_engine.connect() as db_connection:
                            if inspect(db_connection).get_table_names():
                                raise DeployBlocked(
                                    "Fresh baseline refused: control database already has tables",
                                    7,
                                )
                    finally:
                        inspect_engine.dispose()
                if (
                    connection.scalar(
                        text("SELECT 1 FROM pg_roles WHERE rolname=:name"),
                        {"name": user},
                    )
                    is None
                ):
                    connection.exec_driver_sql(f'CREATE ROLE "{user}" LOGIN')
                quoted_password = connection.scalar(
                    text("SELECT quote_literal(:password)"), {"password": password}
                )
                if not isinstance(quoted_password, str):
                    raise DeployBlocked("Control password quoting failed")
                connection.exec_driver_sql(
                    f'ALTER ROLE "{user}" WITH PASSWORD {quoted_password}'
                )
                if not existing_database:
                    connection.exec_driver_sql(
                        f'CREATE DATABASE "{database}" OWNER "{user}"'
                    )
                connection.exec_driver_sql(
                    f'REVOKE CONNECT ON DATABASE "{database}" FROM PUBLIC'
                )
                connection.exec_driver_sql(
                    f'GRANT CONNECT ON DATABASE "{database}" TO "{user}"'
                )
        finally:
            engine.dispose()
        target_engine = create_engine(target)
        try:
            with target_engine.connect() as connection:
                if inspect(connection).get_table_names():
                    raise DeployBlocked(
                        "Fresh baseline refused: control database already has tables", 7
                    )
        finally:
            target_engine.dispose()
    except DeployBlocked:
        raise
    except Exception:
        raise DeployBlocked("Control database creation or inspection failed") from None


def bootstrap(*, dry_run: bool, fresh_install: bool) -> None:
    project = _required("GCP_PROJECT_ID")
    region = _required("GCP_REGION")
    instance = _required("CLOUDSQL_INSTANCE")
    bucket = _required("GCS_BUCKET_NAME")
    _required("CONTROL_DATABASE_URL")
    _validate_targets(
        project, region, instance, bucket, _required("CUBIE_RUNTIME_SERVICE_ACCOUNT")
    )
    gmail = _gmail_oauth_config()
    if not dry_run:
        _runtime_control_url(project, region, instance)
        _runtime_admin_url(project, region, instance)
    if fresh_install:
        try:
            url = make_url(os.environ["CONTROL_DATABASE_URL"])
        except Exception:
            raise DeployBlocked("CONTROL_DATABASE_URL is invalid", 7) from None
        target_missing = (
            url.get_backend_name() == "sqlite" and not Path(url.database or "").exists()
        )
        if not target_missing and not (
            dry_run and url.get_backend_name() == "postgresql"
        ):
            try:
                engine = create_engine(url)
                try:
                    with engine.connect() as connection:
                        tables = set(inspect(connection).get_table_names())
                finally:
                    engine.dispose()
            except Exception:
                # A Postgres target may not exist yet, but we must verify that
                # against the separately configured admin connection below.
                if url.get_backend_name() != "postgresql":
                    raise DeployBlocked(
                        "Fresh control target is unreadable", 7
                    ) from None
                tables = set()
            if tables:
                raise DeployBlocked(
                    "Fresh baseline refused: control database already has tables", 7
                )
    if dry_run:
        print(
            f"DRY RUN: enable platform APIs, ensure bucket {bucket}, SQL instance {instance}, runtime identity and six secret resources in {project}/{region}"
        )
        if gmail:
            print("DRY RUN: ensure optional Gmail OAuth client secret")
        if fresh_install:
            print("DRY RUN: initialize the empty control Alembic baseline")
        return
    apis = [
        "run.googleapis.com",
        "cloudbuild.googleapis.com",
        "artifactregistry.googleapis.com",
        "iam.googleapis.com",
        "sqladmin.googleapis.com",
        "secretmanager.googleapis.com",
        "cloudscheduler.googleapis.com",
        "aiplatform.googleapis.com",
        "storage.googleapis.com",
    ]
    _gcloud(
        "services",
        "enable",
        *apis,
        f"--project={project}",
        label="Platform API enablement",
    )
    if (
        _gcloud_maybe(
            "storage", "buckets", "describe", f"gs://{bucket}", f"--project={project}"
        )
        is None
    ):
        _gcloud(
            "storage",
            "buckets",
            "create",
            f"gs://{bucket}",
            f"--project={project}",
            f"--location={region}",
            label="Bucket create",
        )
    if (
        _gcloud_maybe("sql", "instances", "describe", instance, f"--project={project}")
        is None
    ):
        tier = _required("CUBIE_SQL_TIER")
        _gcloud(
            "sql",
            "instances",
            "create",
            instance,
            f"--project={project}",
            f"--region={region}",
            f"--tier={tier}",
            "--database-version=POSTGRES_16",
            label="SQL instance create",
        )
    runtime_account = _required("CUBIE_RUNTIME_SERVICE_ACCOUNT")
    if (
        _gcloud_maybe(
            "iam",
            "service-accounts",
            "describe",
            runtime_account,
            f"--project={project}",
        )
        is None
    ):
        expected = f"cubie-runtime@{project}.iam.gserviceaccount.com"
        if runtime_account != expected:
            raise DeployBlocked(
                "Runtime identity must be the declared cubie-runtime account"
            )
        _gcloud(
            "iam",
            "service-accounts",
            "create",
            "cubie-runtime",
            f"--project={project}",
            label="Runtime identity create",
        )
    for role in (
        "roles/cloudsql.client",
        "roles/secretmanager.secretAccessor",
        "roles/aiplatform.user",
        "roles/storage.objectAdmin",
        "roles/run.developer",
    ):
        _gcloud(
            "projects",
            "add-iam-policy-binding",
            project,
            f"--member=serviceAccount:{runtime_account}",
            f"--role={role}",
            "--quiet",
            label="Runtime role grant",
        )
    if fresh_install:
        _create_fresh_control_database()
    for name in (
        "CUBIE_SESSION_SECRET",
        "CUBIE_OAUTH_STATE_SECRET",
        "CUBIE_PERSONAL_TOKEN_FERNET_KEY",
        "SCHEDULER_API_KEY",
        "CUBIE_CONTROL_DATABASE_URL",
        "CUBIE_PROVISION_ADMIN_DATABASE_URL",
    ):
        if _gcloud_maybe("secrets", "describe", name, f"--project={project}") is None:
            _gcloud(
                "secrets",
                "create",
                name,
                "--replication-policy=automatic",
                f"--project={project}",
                label="Secret create",
            )
        existing = _gcloud_maybe(
            "secrets",
            "versions",
            "access",
            "latest",
            f"--secret={name}",
            f"--project={project}",
        )
        if name == "CUBIE_CONTROL_DATABASE_URL":
            wanted = _runtime_control_url(project, region, instance)
            if existing and existing != wanted:
                raise DeployBlocked("Control URL secret differs from declared target")
        elif name == "CUBIE_PROVISION_ADMIN_DATABASE_URL":
            wanted = _runtime_admin_url(project, region, instance)
            if existing and existing != wanted:
                raise DeployBlocked("Admin URL secret differs from declared target")
        else:
            wanted = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
        if existing is None:
            _gcloud(
                "secrets",
                "versions",
                "add",
                name,
                "--data-file=-",
                f"--project={project}",
                input_text=wanted,
                label="Secret version add",
            )
    if gmail:
        name = "GOOGLE_GMAIL_OAUTH_CLIENT_SECRET"
        if _gcloud_maybe("secrets", "describe", name, f"--project={project}") is None:
            _gcloud(
                "secrets",
                "create",
                name,
                "--replication-policy=automatic",
                f"--project={project}",
                label="Gmail OAuth secret create",
            )
        existing = _gcloud_maybe(
            "secrets",
            "versions",
            "access",
            "latest",
            f"--secret={name}",
            f"--project={project}",
        )
        if existing and existing != gmail[1]:
            raise DeployBlocked("Gmail OAuth secret differs from declared value")
        if existing is None:
            _gcloud(
                "secrets",
                "versions",
                "add",
                name,
                "--data-file=-",
                f"--project={project}",
                input_text=gmail[1],
                label="Gmail OAuth secret version add",
            )
    if fresh_install:
        from rubik_core.platform.migrate import migrate_control

        os.environ["APP_MODE"] = "gcp"
        previous = os.environ.get("ALEMBIC_ALLOW_REMOTE")
        try:
            # The named fresh installer has already proved that this exact DB
            # is empty. This is the only local path allowed to run its baseline.
            os.environ["ALEMBIC_ALLOW_REMOTE"] = "1"
            migrated = migrate_control()
        finally:
            if previous is None:
                os.environ.pop("ALEMBIC_ALLOW_REMOTE", None)
            else:
                os.environ["ALEMBIC_ALLOW_REMOTE"] = previous
        print(f"Control baseline initialized at {migrated}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    deploy_parser = commands.add_parser("deploy")
    deploy_parser.add_argument("--dry-run", action="store_true")
    bootstrap_parser = commands.add_parser("bootstrap")
    bootstrap_parser.add_argument("--dry-run", action="store_true")
    bootstrap_parser.add_argument("--fresh-install", action="store_true")
    commands.add_parser("migrate")
    commands.add_parser("preflight")
    args = parser.parse_args(argv)
    try:
        if args.command == "deploy":
            deploy(dry_run=args.dry_run)
        elif args.command == "bootstrap":
            bootstrap(dry_run=args.dry_run, fresh_install=args.fresh_install)
        elif args.command == "preflight":
            preflight()
        else:
            migrate()
        return 0
    except DeployBlocked as error:
        print(f"Release refused: {error}", file=sys.stderr)
        return error.code


if __name__ == "__main__":
    raise SystemExit(main())

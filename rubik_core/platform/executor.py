"""Local and Google Cloud side effects for explicit workspace provisioning."""

from __future__ import annotations

import base64
import hashlib
import os
import re
from typing import Any

from sqlalchemy import create_engine, text

from rubik_core.brand import PRODUCT_NAME
from rubik_core.config import AppMode, settings
from rubik_core.control.models import WorkspaceConnectionSpec
from rubik_core.platform.provision import CreateSpec, database_grants_sql
from rubik_core.tenancy.context import use_workspace
from rubik_core.tenancy.storage import local_data_dir

_CORPUS_KINDS = (
    "default",
    "meetings",
    "private_uploads",
    "companywide_rag",
    "vto_rag",
    "eos_exec_rag",
)
_CLOUD_SCOPE = ["https://www.googleapis.com/auth/cloud-platform"]


def service_account_id(slug: str) -> str:
    """Stable IAM account ID within Google's 30-character limit."""
    from rubik_core.tenancy.context import _canonical_slug

    slug = _canonical_slug(slug)
    digest = hashlib.sha256(slug.encode()).hexdigest()[:12]
    return f"cubie-{slug[:11]}-{digest}"


class LocalExecutor:
    def ensure_database(self, spec: WorkspaceConnectionSpec, password: str) -> None:
        # SQLite creates its separate file during the pending migration.
        del spec, password

    def ensure_storage(self, spec: CreateSpec) -> None:
        with use_workspace(spec.slug):
            local_data_dir().mkdir(parents=True, exist_ok=True)

    def corpus_resources(self, spec: CreateSpec) -> dict[str, str]:
        del spec
        return {}

    def ensure_service_account(self, spec: CreateSpec) -> None:
        del spec

    def service_account_identity(self, spec: CreateSpec) -> tuple[None, None]:
        del spec
        return None, None

    def upsert_schedule(self, row: dict[str, Any]) -> None:
        # Local test/development jobs are invoked explicitly, never installed
        # into a scheduler hidden in the developer's machine.
        del row

    def list_schedule_names(self) -> tuple[str, ...]:
        return ()

    def delete_schedule(self, name: str) -> None:
        del name

    def pause_schedule(self, name: str) -> None:
        del name

    def checklist(self, spec: CreateSpec, client_id: str | None) -> tuple[str, ...]:
        del client_id
        return (
            f"Local workspace {spec.slug} is ready; choose an admin at {spec.first_admins[0]}.",
            "For production, grant Google delegation and connect Slack, HubSpot, and Fathom.",
        )


class CloudExecutor:
    """Real cloud operations through SQLAlchemy and Google client APIs.

    Construction does not contact Google or a database. All required targets
    are explicit; missing values fail before a partial provision is attempted.
    """

    def __init__(self) -> None:
        self.project = (settings.GCP_PROJECT_ID or "").strip()
        self.bucket = (settings.GCS_BUCKET_NAME or "").strip()
        self.admin_database_url = os.environ.get(
            "CUBIE_PROVISION_ADMIN_DATABASE_URL", ""
        )
        self.region = os.environ.get("CUBIE_PROVISION_REGION", "")
        self.service_url = os.environ.get("CUBIE_PROVISION_SERVICE_URL", "").rstrip("/")
        self.runtime_account = os.environ.get(
            "CUBIE_PROVISION_RUNTIME_SERVICE_ACCOUNT", ""
        )
        if not all(
            (
                self.project,
                self.bucket,
                self.admin_database_url,
                self.region,
                self.service_url,
                self.runtime_account,
            )
        ):
            raise ValueError(
                "Set GCP_PROJECT_ID, GCS_BUCKET_NAME and all CUBIE_PROVISION_"
                "ADMIN_DATABASE_URL, REGION, SERVICE_URL and "
                "RUNTIME_SERVICE_ACCOUNT values"
            )
        if not self.admin_database_url.startswith("postgresql"):
            raise ValueError("Provisioning admin database must be PostgreSQL")
        if not self.service_url.startswith("https://"):
            raise ValueError("Provisioning service URL must use HTTPS")
        if not self.runtime_account.endswith(
            f"@{self.project}.iam.gserviceaccount.com"
        ):
            raise ValueError(
                "Runtime service account must belong to the platform project"
            )

    def _authorized_session(self):
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        credentials, _ = google.auth.default(scopes=_CLOUD_SCOPE)
        return AuthorizedSession(credentials)

    @staticmethod
    def _json_response(response):
        if not response.ok:
            raise RuntimeError(
                f"Google API request failed: HTTP {response.status_code}"
            )
        return response.json()

    def ensure_database(self, spec: WorkspaceConnectionSpec, password: str) -> None:
        # Provisioner generates URL-safe passwords. Reject anything else
        # before embedding a DDL literal, including control characters.
        if not isinstance(password, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]+", password
        ):
            raise ValueError("A URL-safe generated database password is required")
        engine = create_engine(self.admin_database_url, isolation_level="AUTOCOMMIT")
        try:
            with engine.connect() as connection:
                role = connection.scalar(
                    text("SELECT rolname FROM pg_roles WHERE rolname = :name"),
                    {"name": spec.db_user},
                )
                if role is None:
                    connection.exec_driver_sql(f'CREATE ROLE "{spec.db_user}" LOGIN')
                try:
                    # ALTER ROLE cannot accept a bind parameter. Have PostgreSQL
                    # quote the bound secret, then use that literal in the DDL.
                    password_literal = connection.scalar(
                        text("SELECT quote_literal(:password)"),
                        {"password": password},
                    )
                    if not password_literal:
                        raise ValueError("PostgreSQL did not quote the password")
                    connection.exec_driver_sql(
                        f'ALTER ROLE "{spec.db_user}" WITH PASSWORD {password_literal}'
                    )
                except Exception:
                    # SQLAlchemy exceptions may include bound parameters or the
                    # DDL literal. Keep the generated password out of CLI output.
                    raise RuntimeError(
                        "Could not set workspace database password"
                    ) from None
                existing_owner = connection.scalar(
                    text(
                        "SELECT pg_get_userbyid(datdba) FROM pg_database "
                        "WHERE datname = :name"
                    ),
                    {"name": spec.db_name},
                )
                if existing_owner is None:
                    connection.exec_driver_sql(
                        f'CREATE DATABASE "{spec.db_name}" OWNER "{spec.db_user}"'
                    )
                elif existing_owner != spec.db_user:
                    raise PermissionError("Database exists under a different role")
                for statement in database_grants_sql(spec.db_name, spec.db_user).split(
                    ";\n"
                ):
                    statement = statement.strip().rstrip(";")
                    if statement:
                        connection.exec_driver_sql(statement)
        finally:
            engine.dispose()

    def ensure_storage(self, spec: CreateSpec) -> None:
        from google.cloud import storage

        from rubik_core.tenancy.corpora import provision_corpus_resource

        client = storage.Client(project=self.project)
        blob = client.bucket(self.bucket).blob(f"workspaces/{spec.slug}/.keep")
        if not blob.exists():
            blob.upload_from_string(b"", content_type="application/octet-stream")
        for kind in _CORPUS_KINDS:
            provision_corpus_resource(spec.slug, kind)

    def corpus_resources(self, spec: CreateSpec) -> dict[str, str]:
        from rubik_core.tenancy.corpora import existing_corpus_resource

        found: dict[str, str] = {}
        for kind in _CORPUS_KINDS:
            resource = existing_corpus_resource(spec.slug, kind)
            if not resource:
                raise RuntimeError(f"Physical {kind} corpus is missing")
            found[kind] = resource
        return found

    def _account_name(self, spec: CreateSpec) -> str:
        email = (
            f"{service_account_id(spec.slug)}@{self.project}.iam.gserviceaccount.com"
        )
        return f"projects/{self.project}/serviceAccounts/{email}"

    def _account(self, spec: CreateSpec) -> dict[str, Any]:
        session = self._authorized_session()
        response = session.get(
            f"https://iam.googleapis.com/v1/{self._account_name(spec)}"
        )
        return self._json_response(response)

    def ensure_service_account(self, spec: CreateSpec) -> None:
        session = self._authorized_session()
        parent = f"projects/{self.project}"
        created = session.post(
            f"https://iam.googleapis.com/v1/{parent}/serviceAccounts",
            json={
                "accountId": service_account_id(spec.slug),
                "serviceAccount": {"displayName": f"{PRODUCT_NAME} {spec.name}"[:100]},
            },
        )
        if created.status_code != 409:
            self._json_response(created)
        account_name = self._account_name(spec)
        policy_url = f"https://iam.googleapis.com/v1/{account_name}:getIamPolicy"
        policy = self._json_response(
            session.post(
                policy_url, params={"options.requestedPolicyVersion": 3}, json={}
            )
        )
        bindings = list(policy.get("bindings") or [])
        member = f"serviceAccount:{self.runtime_account}"
        role = "roles/iam.serviceAccountTokenCreator"
        binding = next((row for row in bindings if row.get("role") == role), None)
        if binding is None:
            bindings.append({"role": role, "members": [member]})
        elif member not in (binding.get("members") or []):
            binding["members"] = [*(binding.get("members") or []), member]
        else:
            return
        policy["bindings"] = bindings
        self._json_response(
            session.post(
                f"https://iam.googleapis.com/v1/{account_name}:setIamPolicy",
                json={"policy": policy},
            )
        )

    def service_account_identity(self, spec: CreateSpec) -> tuple[str, str]:
        account = self._account(spec)
        email = account.get("email")
        client_id = account.get("oauth2ClientId")
        expected = (
            f"{service_account_id(spec.slug)}@{self.project}.iam.gserviceaccount.com"
        )
        if email != expected or not isinstance(client_id, str) or not client_id:
            raise RuntimeError("Google service account identity is incomplete")
        return email, client_id

    def upsert_schedule(self, row: dict[str, Any]) -> None:
        if row.get("target", "http") != "http":
            raise ValueError("Only HTTP schedules can be reconciled by this executor")
        from urllib.parse import quote

        path = row.get("path")
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("Schedule path must be rooted")
        name = row["name"]
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9-]+", name):
            raise ValueError("Invalid schedule name")
        from rubik_core.automation import automations_enabled

        if not automations_enabled():
            self.pause_schedule(name)
            return
        headers = dict(row.get("headers") or {})
        for key, value in list(headers.items()):
            if isinstance(value, dict) and value == {"secret": "SCHEDULER_API_KEY"}:
                scheduler_key = os.environ.get("SCHEDULER_API_KEY", "")
                if not scheduler_key:
                    raise RuntimeError("SCHEDULER_API_KEY is required for schedules")
                headers[key] = scheduler_key
            elif not isinstance(value, str):
                raise ValueError("Unsupported schedule header value")
        job = {
            "name": f"projects/{self.project}/locations/{self.region}/jobs/{name}",
            "schedule": row["schedule"],
            "timeZone": row["time_zone"],
            "httpTarget": {
                "uri": f"{self.service_url}{path}",
                "httpMethod": row.get("method", "POST"),
                "headers": headers,
            },
        }
        body = row.get("body")
        if body is not None:
            if not isinstance(body, str):
                raise ValueError("Schedule body must be serialized JSON")
            job["httpTarget"]["body"] = base64.b64encode(body.encode()).decode()
        session = self._authorized_session()
        collection = f"https://cloudscheduler.googleapis.com/v1/projects/{self.project}/locations/{self.region}/jobs"
        existing = session.get(f"{collection}/{quote(name)}")
        if existing.status_code == 404:
            self._json_response(session.post(collection, json=job))
        else:
            self._json_response(existing)
            self._json_response(
                session.patch(
                    f"{collection}/{quote(name)}",
                    params={"updateMask": "schedule,timeZone,httpTarget"},
                    json=job,
                )
            )

    def list_schedule_names(self) -> list[str]:
        session = self._authorized_session()
        collection = (
            f"https://cloudscheduler.googleapis.com/v1/projects/"
            f"{self.project}/locations/{self.region}/jobs"
        )
        found = []
        page_token = None
        while True:
            params = {"pageSize": 500}
            if page_token:
                params["pageToken"] = page_token
            page = self._json_response(session.get(collection, params=params))
            found.extend(
                name.rsplit("/", 1)[-1]
                for job in page.get("jobs", [])
                if isinstance(name := job.get("name"), str)
            )
            page_token = page.get("nextPageToken")
            if not page_token:
                return found

    def delete_schedule(self, name: str) -> None:
        from urllib.parse import quote

        if not re.fullmatch(r"[a-z0-9-]+", name):
            raise ValueError("Invalid schedule name")
        url = (
            f"https://cloudscheduler.googleapis.com/v1/projects/"
            f"{self.project}/locations/{self.region}/jobs/{quote(name)}"
        )
        response = self._authorized_session().delete(url)
        if not response.ok and response.status_code != 404:
            raise RuntimeError(
                f"Cloud Scheduler delete failed: HTTP {response.status_code}"
            )

    def pause_schedule(self, name: str) -> None:
        """Keep an existing job inactive without ever creating or resuming it."""
        from urllib.parse import quote

        if not re.fullmatch(r"[a-z0-9-]+", name):
            raise ValueError("Invalid schedule name")
        url = (
            f"https://cloudscheduler.googleapis.com/v1/projects/"
            f"{self.project}/locations/{self.region}/jobs/{quote(name)}"
        )
        session = self._authorized_session()
        existing = session.get(url)
        if existing.status_code == 404:
            return
        job = self._json_response(existing)
        if job.get("state") in ("PAUSED", "DISABLED"):
            return
        self._json_response(session.post(f"{url}:pause", json={}))

    def checklist(self, spec: CreateSpec, client_id: str | None) -> tuple[str, ...]:
        from rubik_core.auth.workspace import GROUP_SCOPES as directory_group_scopes
        from rubik_core.tools.calendar import SCOPES as calendar_scopes
        from rubik_core.tools.directory import SCOPES as directory_scopes
        from rubik_core.tools.drive import LABELS_SCOPES
        from rubik_core.tools.drive import SCOPES as drive_scopes
        from rubik_core.tools.email_sender import SEND_SCOPE
        from rubik_core.tools.gmail import SCOPES as gmail_scopes
        from rubik_core.tools.meet import SCOPES as meet_scopes
        from rubik_core.tools.meet import SPACE_WRITE_SCOPES

        scopes = sorted(
            set(
                calendar_scopes
                + directory_scopes
                + directory_group_scopes
                + drive_scopes
                + LABELS_SCOPES
                + gmail_scopes
                + meet_scopes
                + SPACE_WRITE_SCOPES
                + [SEND_SCOPE]
            )
        )
        return (
            f"Ask the client admin to grant domain-wide delegation to client ID {client_id or 'unavailable'} for: {', '.join(scopes)}",
            f"Confirm delegated subject {spec.dwd_subject} and install this workspace's Slack app.",
            "In Admin → Connectors, add this workspace's HubSpot and Fathom credentials.",
        )


def default_executor():
    if settings.APP_MODE == AppMode.GCP or settings.CLOUDSQL_CONNECTION_NAME:
        return CloudExecutor()
    return LocalExecutor()

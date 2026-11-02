"""Workspace secret references are authorized by control data before I/O."""

from __future__ import annotations

import base64
import hashlib
import os
import stat
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import delete

from rubik_core.config import AppMode, settings
from rubik_core.control.db import control_session
from rubik_core.control.models import (
    ProvisioningStep,
    Workspace,
    WorkspaceConnectionSpec,
    WorkspaceConnector,
)
from rubik_core.tenancy.context import NoWorkspaceContext, use_workspace
from rubik_core.tenancy.support_context import SupportScope, use_support_session


@pytest.fixture
def secret_workspaces(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "APP_MODE", AppMode.LOCAL)
    monkeypatch.setattr(settings, "CLOUDSQL_CONNECTION_NAME", "")
    monkeypatch.delenv("CUBIE_PERSONAL_TOKEN_FERNET_KEY", raising=False)
    with control_session() as session:
        session.add_all(
            [
                Workspace(slug="acme", name="Acme", status="active"),
                Workspace(slug="acme-sales", name="Acme Sales", status="active"),
            ]
        )
        session.commit()
    try:
        yield tmp_path
    finally:
        with control_session() as session:
            session.execute(
                delete(WorkspaceConnector).where(
                    WorkspaceConnector.workspace_slug.in_(("acme", "acme-sales"))
                )
            )
            for slug in ("acme", "acme-sales"):
                row = session.get(Workspace, slug)
                if row is not None:
                    session.delete(row)
            session.commit()


def test_workspace_secret_values_never_collide(secret_workspaces):
    from rubik_core.tenancy.secrets import (
        bind_workspace_secret_ref,
        read_secret_ref,
        write_secret,
    )

    with use_workspace("acme"):
        acme_ref = bind_workspace_secret_ref("hubspot", "read_token")
        write_secret(acme_ref, "acme-private-token")
    with use_workspace("acme-sales"):
        sales_ref = bind_workspace_secret_ref("hubspot", "read_token")
        write_secret(sales_ref, "sales-private-token")

    assert acme_ref == "ws-acme-hubspot-read_token"
    assert sales_ref == "ws-acme-sales-hubspot-read_token"
    with use_workspace("acme"):
        assert read_secret_ref(acme_ref) == "acme-private-token"
    with use_workspace("acme-sales"):
        assert read_secret_ref(sales_ref) == "sales-private-token"
    encrypted = (secret_workspaces / "workspace-secrets.bin").read_bytes()
    assert b"acme-private-token" not in encrypted
    assert b"sales-private-token" not in encrypted
    assert (
        stat.S_IMODE((secret_workspaces / "workspace-secrets.bin").stat().st_mode)
        == 0o600
    )


def test_overlapping_slugs_require_exact_secret_binding(secret_workspaces, monkeypatch):
    from rubik_core.tenancy import secrets

    with use_workspace("acme-sales"):
        sales_ref = secrets.bind_workspace_secret_ref("hubspot", "read_token")
    read = Mock(side_effect=AssertionError("backend called"))
    write = Mock(side_effect=AssertionError("backend called"))
    monkeypatch.setattr(secrets, "_read_backend", read)
    monkeypatch.setattr(secrets, "_write_backend", write)

    with use_workspace("acme"):
        with pytest.raises(PermissionError):
            secrets.read_secret_ref(sales_ref)
        with pytest.raises(PermissionError):
            secrets.write_secret(sales_ref, "wrong")
    read.assert_not_called()
    write.assert_not_called()


@pytest.mark.parametrize(
    "ref",
    [
        "ws-acme-hubspot-read_token/../other",
        "ws-acme-hubspot-read_token/other",
        "ws-acme-hubspot-read_token\\other",
        "ws-acme-hubspot-%2e%2e-read_token",
        "ws-acme-hubspot-%2f-read_token",
        "ws-acme-hubspot-read_token%252fother",
        "ws-acme-hubspot-read_token..",
        "ws-acme-other-unbound",
    ],
)
def test_foreign_or_malformed_ref_never_calls_backend(
    secret_workspaces, monkeypatch, ref
):
    from rubik_core.tenancy import secrets

    read = Mock(side_effect=AssertionError("backend called"))
    write = Mock(side_effect=AssertionError("backend called"))
    monkeypatch.setattr(secrets, "_read_backend", read)
    monkeypatch.setattr(secrets, "_write_backend", write)
    with use_workspace("acme"):
        with pytest.raises((ValueError, PermissionError)):
            secrets.read_secret_ref(ref)
        with pytest.raises((ValueError, PermissionError)):
            secrets.write_secret(ref, "wrong")
    read.assert_not_called()
    write.assert_not_called()


def test_pending_password_ref_requires_matching_spec(secret_workspaces):
    from rubik_core.tenancy.secrets import read_pending_secret, write_pending_secret

    spec = WorkspaceConnectionSpec(
        "pending_db", "pending_user", "ws-pending-db-password"
    )
    with control_session() as session:
        session.add(
            ProvisioningStep(
                workspace_slug="pending",
                step="create_database",
                status="complete",
                db_name=spec.db_name,
                db_user=spec.db_user,
                db_password_secret_ref=spec.db_password_secret_ref,
            )
        )
        session.commit()
    try:
        write_pending_secret("pending", spec, "pending-private-password")
        assert read_pending_secret("pending", spec) == "pending-private-password"
        wrong = WorkspaceConnectionSpec(
            "other_db", "pending_user", "ws-pending-db-password"
        )
        with pytest.raises(PermissionError):
            read_pending_secret("pending", wrong)
        with pytest.raises(PermissionError):
            write_pending_secret("pending", wrong, "wrong")
        with pytest.raises((PermissionError, ValueError)):
            read_pending_secret("acme", spec)
    finally:
        with control_session() as session:
            session.delete(
                session.get(ProvisioningStep, ("pending", "create_database"))
            )
            session.commit()


def test_no_context_refuses_company_secret(secret_workspaces, monkeypatch):
    from rubik_core.tenancy import context, secrets

    with use_workspace("acme"):
        ref = secrets.bind_workspace_secret_ref("hubspot", "read_token")
    monkeypatch.setattr(context, "_process_workspace", None)
    with pytest.raises(NoWorkspaceContext):
        secrets.read_secret_ref(ref)


def test_personal_token_ciphertext_does_not_contain_plaintext(secret_workspaces):
    from rubik_core.tenancy.secrets import decrypt_personal, encrypt_personal

    ciphertext = encrypt_personal("xoxp-private-user-token")
    assert "xoxp-private-user-token" not in ciphertext
    assert decrypt_personal(ciphertext) == "xoxp-private-user-token"
    key = secret_workspaces / "cubie-local-fernet.key"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600


def test_local_fernet_key_rejects_permissive_mode_or_symlink(
    secret_workspaces, monkeypatch
):
    from rubik_core.tenancy.platform_secrets import platform_secret

    monkeypatch.delenv("CUBIE_PERSONAL_TOKEN_FERNET_KEY", raising=False)
    key = secret_workspaces / "cubie-local-fernet.key"
    platform_secret("CUBIE_PERSONAL_TOKEN_FERNET_KEY")
    key.chmod(0o644)
    with pytest.raises(RuntimeError, match="mode 0600"):
        platform_secret("CUBIE_PERSONAL_TOKEN_FERNET_KEY")
    key.unlink()
    key.symlink_to(secret_workspaces / "missing")
    with pytest.raises(RuntimeError, match="private regular file"):
        platform_secret("CUBIE_PERSONAL_TOKEN_FERNET_KEY")


def test_db_connector_is_reserved_before_control_or_backend_write(
    secret_workspaces, monkeypatch
):
    from rubik_core.tenancy import secrets

    read = Mock(side_effect=AssertionError("backend called"))
    write = Mock(side_effect=AssertionError("backend called"))
    monkeypatch.setattr(secrets, "_read_backend", read)
    monkeypatch.setattr(secrets, "_write_backend", write)
    db_ref = "ws-acme-db-password"
    with use_workspace("acme"):
        with pytest.raises(PermissionError, match="privileged"):
            secrets.bind_workspace_secret_ref("db", "password")
    with control_session() as session:
        assert session.get(WorkspaceConnector, ("acme", "db")) is None
        row = session.get(Workspace, "acme")
        row.db_password_secret_ref = db_ref
        session.commit()
    with use_workspace("acme"):
        with pytest.raises(PermissionError):
            secrets.read_secret_ref(db_ref)
        with pytest.raises(PermissionError):
            secrets.write_secret(db_ref, "clobber")
    read.assert_not_called()
    write.assert_not_called()


def test_clear_workspace_secret_removes_only_its_binding(secret_workspaces):
    from rubik_core.tenancy.secrets import (
        bind_workspace_secret_ref,
        clear_workspace_secret,
        read_secret_ref,
        write_secret,
    )

    with use_workspace("acme"):
        keep = bind_workspace_secret_ref("hubspot", "read_token")
        remove = bind_workspace_secret_ref("hubspot", "write_token")
        write_secret(keep, "keep-value")
        write_secret(remove, "remove-value")
        clear_workspace_secret("hubspot", "write_token")
        assert read_secret_ref(keep) == "keep-value"
        with pytest.raises(PermissionError):
            read_secret_ref(remove)


def test_clear_failure_keeps_control_binding(secret_workspaces, monkeypatch):
    from rubik_core.tenancy import secrets

    with use_workspace("acme"):
        ref = secrets.bind_workspace_secret_ref("ai_provider", "anthropic_api_key")
        monkeypatch.setattr(
            secrets,
            "_clear_backend",
            Mock(side_effect=RuntimeError("Secret Manager unavailable")),
        )
        with pytest.raises(RuntimeError, match="unavailable"):
            secrets.clear_workspace_secret("ai_provider", "anthropic_api_key")
    with control_session() as session:
        row = session.get(WorkspaceConnector, ("acme", "ai_provider"))
        assert row.secret_refs["anthropic_api_key"] == ref


def test_first_secret_write_failure_does_not_publish_binding(
    secret_workspaces, monkeypatch
):
    from rubik_core.tenancy import secrets

    monkeypatch.setattr(
        secrets,
        "_write_backend",
        Mock(side_effect=RuntimeError("Secret Manager unavailable")),
    )
    with use_workspace("acme"):
        with pytest.raises(RuntimeError, match="unavailable"):
            secrets.upsert_workspace_secret("ai_provider", "anthropic_api_key", "key")
    with control_session() as session:
        assert session.get(WorkspaceConnector, ("acme", "ai_provider")) is None


def test_clear_and_rotation_serialize_on_one_connector(secret_workspaces, monkeypatch):
    from threading import Event, Thread

    from rubik_core.tenancy import secrets

    with use_workspace("acme"):
        secrets.upsert_workspace_secret("hubspot", "read_token", "old-token")

    clearing = Event()
    release_clear = Event()
    rotation_attempted = Event()
    backend_write_started = Event()
    errors = []
    original_clear = secrets._clear_backend
    original_write = secrets._write_backend
    original_operate = secrets.operate_connector_secret_ref

    def blocked_clear(ref):
        clearing.set()
        if not release_clear.wait(5):
            raise TimeoutError("clear test timed out")
        original_clear(ref)

    def traced_write(ref, value):
        backend_write_started.set()
        original_write(ref, value)

    def traced_operate(*args, **kwargs):
        if kwargs.get("create"):
            rotation_attempted.set()
        return original_operate(*args, **kwargs)

    monkeypatch.setattr(secrets, "_clear_backend", blocked_clear)
    monkeypatch.setattr(secrets, "_write_backend", traced_write)
    monkeypatch.setattr(secrets, "operate_connector_secret_ref", traced_operate)

    def clear():
        try:
            with use_workspace("acme"):
                secrets.clear_workspace_secret("hubspot", "read_token")
        except Exception as exc:
            errors.append(exc)

    def rotate():
        try:
            with use_workspace("acme"):
                secrets.upsert_workspace_secret("hubspot", "read_token", "new-token")
        except Exception as exc:
            errors.append(exc)

    clear_thread = Thread(target=clear, daemon=True)
    rotate_thread = Thread(target=rotate, daemon=True)
    clear_thread.start()
    try:
        assert clearing.wait(5)
        rotate_thread.start()
        assert rotation_attempted.wait(5)
        assert not backend_write_started.wait(0.1)
    finally:
        release_clear.set()
    clear_thread.join(5)
    rotate_thread.join(5)
    assert not clear_thread.is_alive()
    assert not rotate_thread.is_alive()
    assert errors == []
    with use_workspace("acme"):
        assert secrets.workspace_secret("hubspot", "read_token") == "new-token"


def test_sqlite_connector_mutation_locks_other_connections(secret_workspaces):
    import sqlite3

    from rubik_core.control import repository
    from rubik_core.control.db import get_engine

    db_file = get_engine().url.database
    with control_session() as session:
        repository._locked_workspace_for_secret_mutation(session, "acme")
        with sqlite3.connect(db_file, timeout=0) as competing:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                competing.execute("BEGIN IMMEDIATE")
        session.rollback()
    with sqlite3.connect(db_file, timeout=0) as competing:
        competing.execute("BEGIN IMMEDIATE")


def test_support_session_cannot_read_or_write_secrets(secret_workspaces, monkeypatch):
    from rubik_core.tenancy import secrets

    with use_workspace("acme"):
        ref = secrets.bind_workspace_secret_ref("hubspot", "read_token")
    read = Mock(side_effect=AssertionError("backend called"))
    write = Mock(side_effect=AssertionError("backend called"))
    monkeypatch.setattr(secrets, "_read_backend", read)
    monkeypatch.setattr(secrets, "_write_backend", write)
    support = SupportScope(
        actor="support@example.test",
        sid="test-sid",
        workspace_slug="acme",
        workspace_name="Acme",
    )
    with use_workspace("acme"), use_support_session(support):
        with pytest.raises(PermissionError, match="Support"):
            secrets.read_secret_ref(ref)
        with pytest.raises(PermissionError, match="Support"):
            secrets.write_secret(ref, "wrong")
        with pytest.raises(PermissionError, match="Support"):
            secrets.bind_workspace_secret_ref("hubspot", "write_token")
    read.assert_not_called()
    write.assert_not_called()


def test_secret_manager_cache_is_keyed_by_project_and_ref(monkeypatch):
    from rubik_core import secrets

    calls = []

    class Client:
        def access_secret_version(self, request, timeout):
            calls.append(request["name"])
            return SimpleNamespace(
                payload=SimpleNamespace(data=request["name"].encode())
            )

    monkeypatch.setattr(secrets.secretmanager, "SecretManagerServiceClient", Client)
    secrets._SECRET_CACHE.clear()
    try:
        one = secrets.get_secret("ws-acme-db-password", project_id="project-one")
        two = secrets.get_secret("ws-acme-db-password", project_id="project-two")
        assert one != two
        assert (
            secrets.get_secret("ws-acme-db-password", project_id="project-one") == one
        )
        assert len(calls) == 2
    finally:
        secrets._SECRET_CACHE.clear()


def test_secret_manager_cache_expires_stale_values(monkeypatch):
    from rubik_core import secrets

    now = [100.0]
    calls = []

    class Client:
        def access_secret_version(self, request, timeout):
            calls.append(request["name"])
            return SimpleNamespace(
                payload=SimpleNamespace(data=f"version-{len(calls)}".encode())
            )

    monkeypatch.setattr(secrets.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(secrets.secretmanager, "SecretManagerServiceClient", Client)
    secrets._SECRET_CACHE.clear()
    try:
        assert secrets.get_secret("ws-acme-db-password", "project-one") == "version-1"
        assert secrets.get_secret("ws-acme-db-password", "project-one") == "version-1"
        now[0] += secrets.SECRET_CACHE_TTL_SECONDS + 1
        assert secrets.get_secret("ws-acme-db-password", "project-one") == "version-2"
        assert len(calls) == 2
    finally:
        secrets._SECRET_CACHE.clear()


def test_existing_secret_accepts_runtime_version_adder_without_admin(monkeypatch):
    from rubik_core import secrets

    calls = []

    class Client:
        def create_secret(self, request, timeout):
            raise secrets.exceptions.PermissionDenied("container creation denied")

        def add_secret_version(self, request, timeout):
            calls.append(request)
            return SimpleNamespace(
                name="projects/p/secrets/ws-acme-hubspot-read_token/versions/2"
            )

    monkeypatch.setattr(secrets.secretmanager, "SecretManagerServiceClient", Client)
    result = secrets.put_secret("ws-acme-hubspot-read_token", "new", project_id="p")
    assert result == "projects/p/secrets/ws-acme-hubspot-read_token/versions/2"
    assert calls[0]["payload"]["data"] == b"new"


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("DISABLED", True),
        ("DESTROYED", False),
    ],
)
def test_disable_precondition_only_succeeds_for_disabled_version(
    monkeypatch, state, expected
):
    from rubik_core import secrets

    class Client:
        def get_secret_version(self, request, timeout):
            return SimpleNamespace(
                name="projects/p/secrets/ws-acme-hubspot-read_token/versions/2",
                state=getattr(secrets.secretmanager.SecretVersion.State, state),
            )

        def disable_secret_version(self, request, timeout):
            raise secrets.exceptions.FailedPrecondition("cannot disable")

    monkeypatch.setattr(secrets.secretmanager, "SecretManagerServiceClient", Client)
    assert secrets.disable_latest_version("ws-acme-hubspot-read_token", "p") is expected


@pytest.mark.parametrize("operation", ["read", "write", "disable"])
def test_secret_manager_errors_never_log_secret_payload(monkeypatch, caplog, operation):
    from rubik_core import secrets

    private_token = "private-oauth-token-from-sdk-error"

    class Client:
        def access_secret_version(self, request, timeout):
            raise RuntimeError(private_token)

        def create_secret(self, request, timeout):
            return None

        def add_secret_version(self, request, timeout):
            raise RuntimeError(private_token)

        def get_secret_version(self, request, timeout):
            return SimpleNamespace(name="projects/p/secrets/ws-acme-token/versions/1")

        def disable_secret_version(self, request, timeout):
            raise RuntimeError(private_token)

    monkeypatch.setattr(secrets.secretmanager, "SecretManagerServiceClient", Client)
    secrets._SECRET_CACHE.clear()
    try:
        with caplog.at_level("ERROR", logger="rubik_core.secrets"):
            if operation == "read":
                assert secrets.get_secret("ws-acme-token", "p") is None
            elif operation == "write":
                assert secrets.put_secret("ws-acme-token", "value", "p") is None
            else:
                assert not secrets.disable_latest_version("ws-acme-token", "p")
        assert private_token not in caplog.text
    finally:
        secrets._SECRET_CACHE.clear()


def test_production_platform_secrets_are_strong_and_separate(monkeypatch):
    from rubik_core.tenancy.platform_secrets import (
        platform_secret,
        validate_platform_secrets,
    )

    monkeypatch.setattr(settings, "APP_MODE", AppMode.GCP)
    names = (
        "CUBIE_SESSION_SECRET",
        "CUBIE_OAUTH_STATE_SECRET",
        "CUBIE_PERSONAL_TOKEN_FERNET_KEY",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)
        with pytest.raises(RuntimeError, match=name):
            platform_secret(name)
        monkeypatch.setenv(name, "weak")
        with pytest.raises(RuntimeError, match=name):
            platform_secret(name)
    for name in names:
        monkeypatch.setenv(
            name,
            base64.urlsafe_b64encode(hashlib.sha256(name.encode()).digest()).decode(),
        )
    validate_platform_secrets()
    monkeypatch.setenv(
        "CUBIE_PERSONAL_TOKEN_FERNET_KEY", os.environ["CUBIE_SESSION_SECRET"]
    )
    with pytest.raises(RuntimeError, match="separate"):
        validate_platform_secrets()


def test_production_platform_keys_compare_decoded_bytes(monkeypatch):
    from rubik_core.tenancy.platform_secrets import validate_platform_secrets

    monkeypatch.setattr(settings, "APP_MODE", AppMode.GCP)
    same_key = base64.urlsafe_b64encode(hashlib.sha256(b"same").digest()).decode(
        "ascii"
    )
    monkeypatch.setenv("CUBIE_SESSION_SECRET", same_key.rstrip("="))
    monkeypatch.setenv(
        "CUBIE_OAUTH_STATE_SECRET",
        base64.urlsafe_b64encode(hashlib.sha256(b"other").digest()).decode(),
    )
    monkeypatch.setenv("CUBIE_PERSONAL_TOKEN_FERNET_KEY", same_key)
    with pytest.raises(RuntimeError, match="separate"):
        validate_platform_secrets()


@pytest.mark.parametrize(
    "value",
    [
        base64.urlsafe_b64encode(bytes(32)).decode("ascii"),
        base64.urlsafe_b64encode(b"x" * 32).decode("ascii"),
    ],
)
def test_production_rejects_predictable_signing_keys(monkeypatch, value):
    from rubik_core.tenancy.platform_secrets import platform_secret

    monkeypatch.setattr(settings, "APP_MODE", AppMode.GCP)
    monkeypatch.setenv("CUBIE_SESSION_SECRET", value)
    with pytest.raises(RuntimeError, match="CUBIE_SESSION_SECRET"):
        platform_secret("CUBIE_SESSION_SECRET")


def test_production_rejects_source_local_signing_default(monkeypatch):
    from rubik_core.tenancy.platform_secrets import _LOCAL_DEFAULTS, platform_secret

    monkeypatch.setattr(settings, "APP_MODE", AppMode.GCP)
    monkeypatch.setenv("CUBIE_SESSION_SECRET", _LOCAL_DEFAULTS["CUBIE_SESSION_SECRET"])
    with pytest.raises(RuntimeError, match="CUBIE_SESSION_SECRET"):
        platform_secret("CUBIE_SESSION_SECRET")


def test_production_rejects_predictable_fernet_key(monkeypatch):
    from rubik_core.tenancy.platform_secrets import platform_secret

    monkeypatch.setattr(settings, "APP_MODE", AppMode.GCP)
    monkeypatch.setenv(
        "CUBIE_PERSONAL_TOKEN_FERNET_KEY",
        base64.urlsafe_b64encode(bytes(32)).decode("ascii"),
    )
    with pytest.raises(RuntimeError, match="CUBIE_PERSONAL_TOKEN_FERNET_KEY"):
        platform_secret("CUBIE_PERSONAL_TOKEN_FERNET_KEY")


def test_production_startup_requires_oauth_and_fernet_keys(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.main import app

    monkeypatch.setattr(settings, "APP_MODE", AppMode.GCP)
    monkeypatch.setattr(settings, "ENABLE_MOCK_AUTH", False)
    monkeypatch.setenv(
        "CUBIE_SESSION_SECRET",
        base64.urlsafe_b64encode(hashlib.sha256(b"session").digest()).decode(),
    )
    monkeypatch.delenv("CUBIE_OAUTH_STATE_SECRET", raising=False)
    monkeypatch.delenv("CUBIE_PERSONAL_TOKEN_FERNET_KEY", raising=False)
    with pytest.raises(RuntimeError, match="CUBIE_OAUTH_STATE_SECRET"):
        with TestClient(app):
            pytest.fail("Production started without OAuth state key")
    monkeypatch.setenv(
        "CUBIE_OAUTH_STATE_SECRET",
        base64.urlsafe_b64encode(hashlib.sha256(b"oauth").digest()).decode(),
    )
    with pytest.raises(RuntimeError, match="CUBIE_PERSONAL_TOKEN_FERNET_KEY"):
        with TestClient(app):
            pytest.fail("Production started without Fernet key")


@pytest.mark.parametrize("marker", ["CLOUD_RUN_JOB", "CLOUD_RUN_EXECUTION"])
def test_cloud_run_job_markers_never_use_local_secret_defaults(
    secret_workspaces, monkeypatch, marker
):
    from rubik_core.tenancy import secrets
    from rubik_core.tenancy.platform_secrets import (
        platform_secret,
        validate_platform_secrets,
    )

    monkeypatch.setattr(settings, "APP_MODE", AppMode.LOCAL)
    monkeypatch.setattr(settings, "CLOUDSQL_CONNECTION_NAME", "")
    for name in ("K_SERVICE", "CLOUD_RUN_JOB", "CLOUD_RUN_EXECUTION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(marker, "cubie-job")
    for name in (
        "CUBIE_SESSION_SECRET",
        "CUBIE_OAUTH_STATE_SECRET",
        "CUBIE_PERSONAL_TOKEN_FERNET_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
        with pytest.raises(RuntimeError, match=name):
            platform_secret(name)
    with pytest.raises(RuntimeError, match="CUBIE_SESSION_SECRET"):
        validate_platform_secrets()
    assert secrets._remote_store() is True
    monkeypatch.setattr(settings, "GCP_PROJECT_ID", None)
    with use_workspace("acme"):
        ref = secrets.bind_workspace_secret_ref("hubspot", "read_token")
        with pytest.raises(RuntimeError, match="GCP_PROJECT_ID"):
            secrets.write_secret(ref, "job-token")
    assert not (secret_workspaces / "workspace-secrets.bin").exists()


def test_registered_db_password_requires_exact_control_binding(
    secret_workspaces, monkeypatch
):
    from rubik_core.tenancy import secrets

    read = Mock(return_value="password")
    monkeypatch.setattr(secrets, "_read_backend", read)
    spec = WorkspaceConnectionSpec("acme_db", "acme_user", "ws-acme-db-password")
    with pytest.raises(PermissionError):
        secrets.read_registered_db_password("acme", spec)
    read.assert_not_called()
    with control_session() as session:
        row = session.get(Workspace, "acme")
        row.db_name = spec.db_name
        row.db_user = spec.db_user
        row.db_password_secret_ref = spec.db_password_secret_ref
        session.commit()
    assert secrets.read_registered_db_password("acme", spec) == "password"
    read.assert_called_once_with(spec.db_password_secret_ref)


def test_support_can_open_cold_target_db_pool_but_not_other_secrets(
    secret_workspaces, monkeypatch
):
    from rubik_core.tenancy import registry, secrets

    specs = {
        slug: WorkspaceConnectionSpec(
            f"{slug.replace('-', '_')}_db",
            f"{slug.replace('-', '_')}_user",
            f"ws-{slug}-db-password",
        )
        for slug in ("acme", "acme-sales")
    }
    with control_session() as session:
        for slug, spec in specs.items():
            row = session.get(Workspace, slug)
            row.db_name = spec.db_name
            row.db_user = spec.db_user
            row.db_password_secret_ref = spec.db_password_secret_ref
        session.commit()
    with use_workspace("acme"):
        connector_ref = secrets.bind_workspace_secret_ref("hubspot", "read_token")

    monkeypatch.setattr(settings, "CLOUDSQL_CONNECTION_NAME", "project:region:instance")
    backend_read = Mock(return_value="db-password")
    monkeypatch.setattr(secrets, "_read_backend", backend_read)
    connector = Mock()
    connector_factory = Mock(return_value=connector)
    monkeypatch.setattr(registry, "_pytest_cloud_stubs", connector_factory)
    registry.clear_engines()
    scope = SupportScope(
        actor="support@example.test",
        sid="live-support-sid",
        workspace_slug="acme",
        workspace_name="Acme",
    )
    try:
        with use_workspace("acme"), use_support_session(scope):
            assert registry.cached_engines() == ()
            engine = registry.engine_for("acme")
            engine.pool._creator()
            with pytest.raises(PermissionError, match="Support"):
                secrets.read_secret_ref(connector_ref)
            with pytest.raises(PermissionError, match="Support"):
                secrets.read_pending_secret("acme", specs["acme"])
            with pytest.raises(PermissionError, match="Support"):
                secrets.read_registered_db_password("acme-sales", specs["acme-sales"])
            with use_workspace("acme-sales"):
                with pytest.raises(PermissionError, match="Support"):
                    secrets.read_registered_db_password("acme", specs["acme"])
            with pytest.raises(PermissionError, match="not bound"):
                secrets.read_registered_db_password(
                    "acme",
                    WorkspaceConnectionSpec(
                        "different_db",
                        specs["acme"].db_user,
                        specs["acme"].db_password_secret_ref,
                    ),
                )
            with control_session() as session:
                session.get(Workspace, "acme").status = "suspended"
                session.commit()
            with pytest.raises(PermissionError, match="not bound"):
                secrets.read_registered_db_password("acme", specs["acme"])
            with pytest.raises(PermissionError, match="not bound"):
                engine.pool._creator()
        assert backend_read.call_count == 2
        assert all(
            call.args == (specs["acme"].db_password_secret_ref,)
            for call in backend_read.call_args_list
        )
        connector_factory.assert_called_once_with()
        connector.connect.assert_called_once()
    finally:
        registry.clear_engines()


def test_connector_binding_updates_lock_existing_row(monkeypatch):
    from contextlib import contextmanager

    from sqlalchemy.dialects import postgresql

    from rubik_core.control import repository

    row = SimpleNamespace(secret_refs={"write_token": "ws-acme-hubspot-write_token"})
    session = Mock()
    session.scalar.side_effect = [SimpleNamespace(status="active"), row]

    @contextmanager
    def fake_control_session():
        yield session

    monkeypatch.setattr(repository, "control_session", fake_control_session)
    read_ref = "ws-acme-hubspot-read_token"
    assert (
        repository.bind_connector_secret_ref("acme", "hubspot", "read_token", read_ref)
        == read_ref
    )
    assert row.secret_refs == {
        "write_token": "ws-acme-hubspot-write_token",
        "read_token": read_ref,
    }
    bind_query = session.scalar.call_args.args[0]
    assert "FOR UPDATE" in str(bind_query.compile(dialect=postgresql.dialect()))

    session.scalar.reset_mock()
    session.scalar.side_effect = [SimpleNamespace(status="active"), row]
    repository.unbind_connector_secret_ref("acme", "hubspot", "read_token", read_ref)
    assert row.secret_refs == {"write_token": "ws-acme-hubspot-write_token"}
    unbind_query = session.scalar.call_args.args[0]
    assert "FOR UPDATE" in str(unbind_query.compile(dialect=postgresql.dialect()))


def test_concurrent_first_connector_insert_fails_closed(monkeypatch):
    from contextlib import contextmanager

    from sqlalchemy.exc import IntegrityError

    from rubik_core.control import repository

    session = Mock()
    session.scalar.side_effect = [SimpleNamespace(status="active"), None]
    session.commit.side_effect = IntegrityError("insert", {}, RuntimeError("duplicate"))

    @contextmanager
    def fake_control_session():
        yield session

    monkeypatch.setattr(repository, "control_session", fake_control_session)
    with pytest.raises(PermissionError, match="Concurrent"):
        repository.bind_connector_secret_ref(
            "acme", "hubspot", "read_token", "ws-acme-hubspot-read_token"
        )
    session.rollback.assert_called_once()

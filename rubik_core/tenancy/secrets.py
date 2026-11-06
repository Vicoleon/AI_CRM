"""Control-bound workspace secrets and encrypted local development storage.

No connector can address Secret Manager or local storage with an unbound ref.
Provisioning and the DB registry have separate, exact-spec password accessors.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from rubik_core.config import settings
from rubik_core.control.db import control_session
from rubik_core.control.models import (
    ProvisioningStep,
    Workspace,
    WorkspaceConnectionSpec,
)
from rubik_core.control.repository import (
    bind_connector_secret_ref,
    connector_secret_ref,
    operate_bound_secret_ref,
    operate_connector_secret_ref,
    workspace_secret_ref_is_bound,
)
from rubik_core.secrets import disable_latest_version, get_secret, put_secret
from rubik_core.tenancy.context import (
    NoWorkspaceContext,
    _canonical_slug,
    current_workspace,
)
from rubik_core.tenancy.platform_secrets import is_production_runtime, platform_secret
from rubik_core.tenancy.support_context import current_support_session

_PART = re.compile(r"[a-z][a-z0-9_]*\Z")
_REF = re.compile(r"ws-[a-z0-9_-]+\Z")
_LOCAL_FILE = "workspace-secrets.bin"


def _validate_part(value: str, name: str) -> str:
    if not isinstance(value, str) or not _PART.fullmatch(value):
        raise ValueError(f"A normalized {name} is required")
    return value


def _make_ref(slug: str, connector: str, field: str) -> str:
    slug = _canonical_slug(slug)
    connector = _validate_part(connector, "connector")
    field = _validate_part(field, "secret field")
    ref = f"ws-{slug}-{connector}-{field}"
    if len(ref) > 255:
        raise ValueError("Workspace secret reference is too long")
    return ref


def _validate_ref(ref: str, slug: str) -> str:
    """Reject path, encoding and ambiguous text before cache/backend access."""
    _canonical_slug(slug)
    if (
        not isinstance(ref, str)
        or len(ref) > 255
        or not _REF.fullmatch(ref)
        or ".." in ref
        or "%" in ref
        or not ref.startswith(f"ws-{slug}-")
    ):
        raise ValueError("Malformed workspace secret reference")
    return ref


def _assert_no_support() -> None:
    if current_support_session() is not None:
        raise PermissionError("Support sessions cannot access workspace secrets")


def _assert_connector_not_reserved(connector: str) -> None:
    if connector == "db":
        raise PermissionError("Database credentials use privileged accessors")


def ws_secret_id(connector: str, field: str) -> str:
    """Canonical atomic Secret Manager ID for the selected workspace."""
    _assert_connector_not_reserved(connector)
    return _make_ref(current_workspace(), connector, field)


def bind_workspace_secret_ref(connector: str, field: str) -> str:
    """Register a typed connector/field binding before writing its value."""
    _assert_no_support()
    _assert_connector_not_reserved(connector)
    slug = current_workspace()
    ref = _make_ref(slug, connector, field)
    return bind_connector_secret_ref(slug, connector, field, ref)


def read_secret_ref(ref: str) -> str | None:
    """Read only a complete, live control-bound ref for the current workspace."""
    _assert_no_support()
    slug = current_workspace()
    _validate_ref(ref, slug)
    if not workspace_secret_ref_is_bound(slug, ref):
        raise PermissionError("Workspace secret reference is not bound")
    return _read_backend(ref)


def write_secret(ref: str, value: str) -> None:
    """Write only a complete, live control-bound ref for this workspace."""
    _assert_no_support()
    slug = current_workspace()
    _validate_ref(ref, slug)
    if not isinstance(value, str) or not value:
        raise ValueError("A nonempty secret value is required")
    operate_bound_secret_ref(slug, ref, lambda: _write_backend(ref, value))


def upsert_workspace_secret(connector: str, field: str, value: str) -> str:
    """Write a connector value, publishing a new binding only after success."""
    _assert_no_support()
    _assert_connector_not_reserved(connector)
    if not isinstance(value, str) or not value:
        raise ValueError("A nonempty secret value is required")
    slug = current_workspace()
    ref = _make_ref(slug, connector, field)
    operate_connector_secret_ref(
        slug, connector, field, ref, lambda: _write_backend(ref, value), create=True
    )
    return ref


def workspace_secret(connector: str, field: str) -> str | None:
    """Read a typed connector field, or None when it has not been bound."""
    _assert_no_support()
    _assert_connector_not_reserved(connector)
    slug = current_workspace()
    ref = _make_ref(slug, connector, field)
    bound = connector_secret_ref(slug, connector, field)
    if bound is None:
        return None
    if bound != ref:
        raise PermissionError("Connector secret binding is malformed")
    return read_secret_ref(ref)


def clear_workspace_secret(connector: str, field: str) -> None:
    """Clear a value, then unbind exactly that connector field on success."""
    _assert_no_support()
    _assert_connector_not_reserved(connector)
    slug = current_workspace()
    ref = _make_ref(slug, connector, field)
    operate_connector_secret_ref(
        slug,
        connector,
        field,
        ref,
        lambda: _clear_backend(ref),
        clear=True,
        missing_ok=True,
    )


def _db_ref(slug: str, ref: str) -> str:
    _validate_ref(ref, slug)
    if ref != _make_ref(slug, "db", "password"):
        raise PermissionError("Database password reference is not canonical")
    return ref


def _assert_registered_spec(
    slug: str, spec: WorkspaceConnectionSpec, *, active_only: bool = False
) -> str:
    slug = _canonical_slug(slug)
    if not isinstance(spec, WorkspaceConnectionSpec):
        raise TypeError("A vetted WorkspaceConnectionSpec is required")
    ref = _db_ref(slug, spec.db_password_secret_ref)
    allowed_statuses = ("active",) if active_only else ("active", "suspended")
    with control_session() as session:
        workspace = session.get(Workspace, slug)
        if (
            workspace is None
            or workspace.status not in allowed_statuses
            or (
                workspace.db_name,
                workspace.db_user,
                workspace.db_password_secret_ref,
            )
            != (spec.db_name, spec.db_user, spec.db_password_secret_ref)
        ):
            raise PermissionError("Registered DB secret spec is not bound")
    return ref


def read_registered_db_password(slug: str, spec: WorkspaceConnectionSpec) -> str | None:
    """Registry/migrator only: resolve the exact registered DB identity."""
    slug = _canonical_slug(slug)
    support = current_support_session()
    if support is not None:
        try:
            selected_slug = current_workspace()
        except NoWorkspaceContext:
            selected_slug = None
        if support.workspace_slug != slug or selected_slug != slug:
            raise PermissionError(
                "Support sessions may open only their target database"
            )
    return _read_backend(
        _assert_registered_spec(slug, spec, active_only=support is not None)
    )


def _assert_pending_spec(slug: str, spec: WorkspaceConnectionSpec) -> str:
    slug = _canonical_slug(slug)
    if not isinstance(spec, WorkspaceConnectionSpec):
        raise TypeError("A vetted WorkspaceConnectionSpec is required")
    ref = _db_ref(slug, spec.db_password_secret_ref)
    with control_session() as session:
        registered = session.get(Workspace, slug)
        step = session.get(ProvisioningStep, (slug, "create_database"))
        if (
            registered is not None
            or step is None
            or step.status != "complete"
            or (step.db_name, step.db_user, step.db_password_secret_ref)
            != (spec.db_name, spec.db_user, spec.db_password_secret_ref)
        ):
            raise PermissionError("Pending DB secret spec is not bound")
    return ref


def read_pending_secret(slug: str, spec: WorkspaceConnectionSpec) -> str | None:
    """Provisioning only: exact completed create_database identity is required."""
    _assert_no_support()
    return _read_backend(_assert_pending_spec(slug, spec))


def write_pending_secret(slug: str, spec: WorkspaceConnectionSpec, value: str) -> None:
    """Provisioning only: persist a password after its step was committed."""
    _assert_no_support()
    ref = _assert_pending_spec(slug, spec)
    if not isinstance(value, str) or not value:
        raise ValueError("A nonempty secret value is required")
    _write_backend(ref, value)


def encrypt_personal(raw: str) -> str:
    """Encrypt a personal OAuth token for storage in its workspace DB."""
    if not isinstance(raw, str):
        raise TypeError("Personal token must be a string")
    return _fernet().encrypt(raw.encode("utf-8")).decode("ascii")


def decrypt_personal(ciphertext: str) -> str:
    """Decrypt only Fernet ciphertext; plaintext never passes through."""
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, AttributeError) as exc:
        raise ValueError("Invalid personal token ciphertext") from exc


def _fernet() -> Fernet:
    return Fernet(platform_secret("CUBIE_PERSONAL_TOKEN_FERNET_KEY").encode("ascii"))


def _remote_store() -> bool:
    return bool(is_production_runtime() or settings.CLOUDSQL_CONNECTION_NAME)


def _project_id() -> str:
    project = (settings.GCP_PROJECT_ID or "").strip()
    if not project:
        raise RuntimeError("GCP_PROJECT_ID is required for workspace secrets")
    return project


def _read_backend(ref: str) -> str | None:
    if _remote_store():
        return get_secret(ref, _project_id())
    with _local_lock() as path:
        return _local_values(path).get(ref)


def _write_backend(ref: str, value: str) -> None:
    if _remote_store():
        if put_secret(ref, value, _project_id()) is None:
            raise RuntimeError("Workspace secret write failed")
        return
    with _local_lock() as path:
        values = _local_values(path)
        values[ref] = value
        _save_local_values(path, values)


def _clear_backend(ref: str) -> None:
    if _remote_store():
        if not disable_latest_version(ref, _project_id()):
            raise RuntimeError("Workspace secret clear failed")
        return
    with _local_lock() as path:
        values = _local_values(path)
        values.pop(ref, None)
        _save_local_values(path, values)


@contextmanager
def _local_lock() -> Iterator[Path]:
    directory = Path(settings.LOCAL_DATA_DIR).resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = directory / f"{_LOCAL_FILE}.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield directory / _LOCAL_FILE
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _local_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        payload = _fernet().decrypt(path.read_bytes())
        values = json.loads(payload)
    except (InvalidToken, ValueError, UnicodeError) as exc:
        raise RuntimeError("Local workspace secret store cannot be decrypted") from exc
    if not isinstance(values, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in values.items()
    ):
        raise RuntimeError("Local workspace secret store is invalid")
    return values


def _save_local_values(path: Path, values: dict[str, str]) -> None:
    ciphertext = _fernet().encrypt(
        json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    descriptor, temp_name = tempfile.mkstemp(
        prefix=".workspace-secrets-", dir=path.parent
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as file:
            file.write(ciphertext)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_name, path)
    finally:
        Path(temp_name).unlink(missing_ok=True)

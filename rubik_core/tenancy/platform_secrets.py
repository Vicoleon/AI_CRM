"""Platform signing values available before a workspace is selected."""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import stat
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

from rubik_core.config import AppMode, settings

_SIGNING_NAMES = ("CUBIE_SESSION_SECRET", "CUBIE_OAUTH_STATE_SECRET")
_FERNET_NAME = "CUBIE_PERSONAL_TOKEN_FERNET_KEY"
_LOCAL_DEFAULTS = {
    name: base64.urlsafe_b64encode(
        hashlib.sha256(f"cubie-local-only:{name}".encode()).digest()
    ).decode("ascii")
    for name in _SIGNING_NAMES
}


def is_production_runtime() -> bool:
    """Cloud Run services and Jobs must never use local secret defaults."""
    return settings.APP_MODE == AppMode.GCP or any(
        os.environ.get(name)
        for name in ("K_SERVICE", "CLOUD_RUN_JOB", "CLOUD_RUN_EXECUTION")
    )


def _local_fernet_key() -> str:
    """Keep a random local encryption key across restarts, outside Git."""
    directory = Path(settings.LOCAL_DATA_DIR).resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / "cubie-local-fernet.key"
    if not os.path.lexists(path):
        descriptor, temp_name = tempfile.mkstemp(prefix=".cubie-key-", dir=directory)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as file:
                file.write(Fernet.generate_key())
                file.flush()
                os.fsync(file.fileno())
            try:
                os.link(temp_name, path)
            except FileExistsError:
                pass
        finally:
            Path(temp_name).unlink(missing_ok=True)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise RuntimeError("Local Fernet key must be a private regular file") from exc
    with os.fdopen(descriptor, "rb") as file:
        mode = os.fstat(file.fileno()).st_mode
        if not stat.S_ISREG(mode) or stat.S_IMODE(mode) & 0o077:
            raise RuntimeError("Local Fernet key must have mode 0600")
        return file.read(128).decode("ascii")


def _strong_signing_key(value: str) -> bool:
    try:
        raw = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, binascii.Error):
        return False
    return (
        len(raw) == 32
        and len(set(raw)) > 1
        and value.rstrip("=")
        not in {default.rstrip("=") for default in _LOCAL_DEFAULTS.values()}
        and base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        == value.rstrip("=")
    )


def platform_secret(name: str) -> str:
    """Return a platform value without reading a tenant DB or context.

    Production receives these values through the service's secret-backed
    environment. Missing or weak signing values fail closed at boot and use.
    """
    if name not in (*_SIGNING_NAMES, _FERNET_NAME):
        raise ValueError("Unknown platform secret")
    value = os.environ.get(name, "")
    production = is_production_runtime()
    if not value and not production:
        value = _local_fernet_key() if name == _FERNET_NAME else _LOCAL_DEFAULTS[name]
    if name in _SIGNING_NAMES and production:
        if not _strong_signing_key(value):
            raise RuntimeError(f"{name} must be a base64url-encoded 32-byte key")
    if not value:
        raise RuntimeError(f"{name} is required")
    if name == _FERNET_NAME:
        try:
            Fernet(value.encode("ascii"))
        except (ValueError, TypeError, UnicodeEncodeError) as exc:
            raise RuntimeError(f"{name} must be a valid Fernet key") from exc
        if production and not _strong_signing_key(value):
            raise RuntimeError(f"{name} must be a random 32-byte Fernet key")
    return value


def validate_platform_secrets() -> None:
    """Fail production startup before accepting requests or issuing tokens."""
    production = is_production_runtime()
    names = (*_SIGNING_NAMES, _FERNET_NAME) if production else _SIGNING_NAMES
    values = {name: platform_secret(name) for name in names}
    if production:
        # A signing key may omit padding while Fernet requires it; compare
        # decoded key material so different spellings cannot reuse one key.
        raw_keys = {
            base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
            for value in values.values()
        }
        if len(raw_keys) != len(values):
            raise RuntimeError("Platform signing and encryption keys must be separate")


def validate_session_secret() -> None:
    """Compatibility wrapper for pre-workspace session startup checks."""
    validate_platform_secrets()

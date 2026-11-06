from __future__ import annotations

# rubik_core/secrets.py
import logging
import os
import time

from google.api_core import exceptions
from google.cloud import secretmanager

logger = logging.getLogger(__name__)

# A short TTL limits stale credentials after out-of-band rotation/revocation.
SECRET_CACHE_TTL_SECONDS = 60.0
SECRET_CACHE_MAX_ENTRIES = 512
_SECRET_CACHE: dict[tuple[str, str], tuple[float, str]] = {}


def get_secret(secret_id: str, project_id: str = None) -> str | None:
    """
    Fetches a secret from Google Cloud Secret Manager.
    If project_id is not provided, it attempts to use GCP_PROJECT_ID from environment.
    """
    project_id = project_id or os.getenv("GCP_PROJECT_ID")
    if not project_id:
        logger.debug(
            f"GCP_PROJECT_ID not set, skipping Secret Manager fetch for {secret_id}"
        )
        return None
    cache_key = (project_id, secret_id)
    cached = _SECRET_CACHE.get(cache_key)
    if cached is not None:
        if cached[0] > time.monotonic():
            return cached[1]
        _SECRET_CACHE.pop(cache_key, None)

    try:
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
        response = client.access_secret_version(request={"name": name}, timeout=10)
        payload = response.payload.data.decode("UTF-8")

        if len(_SECRET_CACHE) >= SECRET_CACHE_MAX_ENTRIES:
            _SECRET_CACHE.pop(next(iter(_SECRET_CACHE)))
        _SECRET_CACHE[cache_key] = (
            time.monotonic() + SECRET_CACHE_TTL_SECONDS,
            payload,
        )
        logger.info(f"Successfully fetched secret: {secret_id}")
        return payload
    except exceptions.NotFound:
        logger.debug(f"Secret {secret_id} not found in Secret Manager.")
    except exceptions.PermissionDenied:
        logger.warning(
            f"Permission denied accessing secret {secret_id}. Check Service Account roles."
        )
    except Exception:
        logger.error("Secret Manager read failed")

    return None


# Write side for control-bound workspace refs.


def put_secret(secret_id: str, value: str, project_id: str = None) -> str | None:
    """Write ``value`` as a new version of ``secret_id`` in Secret Manager.

    Creates the secret (with automatic replication) if it doesn't exist.
    Returns the full version resource name on success, or ``None`` if
    Secret Manager isn't available (e.g. running locally without
    ``GCP_PROJECT_ID``). Invalidates the read-cache for that secret.

    The caller validates workspace ownership before reaching this low-level
    primitive. The raw value is never echoed back to the client.
    """
    project_id = project_id or os.getenv("GCP_PROJECT_ID")
    if not project_id:
        logger.warning(
            f"GCP_PROJECT_ID not set; cannot put_secret for {secret_id}",
        )
        return None
    if not value:
        return None

    parent = f"projects/{project_id}"
    secret_path = f"{parent}/secrets/{secret_id}"

    try:
        client = secretmanager.SecretManagerServiceClient()

        # A runtime with secretVersionAdder may lack permission to create a
        # container provisioned earlier. Try the add even when creation is
        # denied; if the container is absent, that add fails closed below.
        try:
            client.create_secret(
                request={
                    "parent": parent,
                    "secret_id": secret_id,
                    "secret": {"replication": {"automatic": {}}},
                },
                timeout=10,
            )
            logger.info(f"Created secret container {secret_id}")
        except exceptions.AlreadyExists:
            pass
        except exceptions.PermissionDenied:
            pass

        # 2. Add a new version with the payload.
        response = client.add_secret_version(
            request={
                "parent": secret_path,
                "payload": {"data": value.encode("UTF-8")},
            },
            timeout=10,
        )

        # Invalidate read cache so the next get_secret picks up the new value.
        _SECRET_CACHE.pop((project_id, secret_id), None)

        logger.info(f"Added new version for {secret_id}: {response.name}")
        return response.name
    except exceptions.PermissionDenied:
        logger.warning(
            f"Permission denied writing secret {secret_id}. "
            "Service account needs roles/secretmanager.admin or "
            "roles/secretmanager.secretVersionAdder."
        )
    except Exception:
        logger.error("Secret Manager write failed")
    return None


def disable_latest_version(secret_id: str, project_id: str = None) -> bool:
    """Mark the most recent version of ``secret_id`` as DISABLED.

    The workspace store uses this after exact control-binding checks. We keep
    historical versions for audit while fresh reads fail closed.

    Secret Manager's enable/disable/destroy APIs do NOT accept the
    ``latest`` alias (unlike access/get), so we resolve it to a
    concrete version resource name first.
    """
    project_id = project_id or os.getenv("GCP_PROJECT_ID")
    if not project_id:
        return False

    version = None
    try:
        client = secretmanager.SecretManagerServiceClient()
        latest_alias = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
        version = client.get_secret_version(request={"name": latest_alias}, timeout=10)
        # version.name is the fully-qualified resource — e.g. .../versions/3
        client.disable_secret_version(request={"name": version.name}, timeout=10)
        _SECRET_CACHE.pop((project_id, secret_id), None)
        logger.info(f"Disabled {version.name}")
        return True
    except exceptions.NotFound:
        return False
    except exceptions.FailedPrecondition:
        # A precondition can also mean a destroyed/inaccessible version. Only
        # a known DISABLED state is safe to treat as a successful clear.
        if version is not None and getattr(version, "state", None) == (
            secretmanager.SecretVersion.State.DISABLED
        ):
            logger.info(f"Latest version of {secret_id} was already disabled")
            _SECRET_CACHE.pop((project_id, secret_id), None)
            return True
        return False
    except Exception:
        logger.error("Secret Manager disable failed")
        return False

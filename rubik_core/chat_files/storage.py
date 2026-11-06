"""Where an attached file's original bytes live.

Same bucket as the other upload features, with the workspace's local data
directory outside GCP. Its own subprefix, outside ``rags/``,
so no corpus import can sweep a person's private file into a shared knowledge
base, and so a per-person purge can never reach another feature's files. The
object name carries a hash of the owner, not their address, and a random id,
not the file name: two people attaching "Q3 report.pdf" never touch each
other's object.

The original is kept so its owner can download it from Assets → My private
files, and so a picture can be shown to the chat model again.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from rubik_core.config import AppMode, settings
from rubik_core.tenancy.storage import assert_own_uri, local_data_dir, object_name

logger = logging.getLogger(__name__)

GCS_SUBPREFIX = "chat-uploads"


def owner_key(email: str) -> str:
    return hashlib.sha256((email or "").strip().lower().encode()).hexdigest()[:20]


def _object_name(owner_email: str, upload_id: str, suffix: str) -> str:
    return object_name(GCS_SUBPREFIX, owner_key(owner_email), f"{upload_id}{suffix}")


def store_bytes(
    owner_email: str, upload_id: str, suffix: str, data: bytes, mime_type: str
) -> str:
    """Persist the original; return the URI recorded in ``storage_uri``."""
    name = _object_name(owner_email, upload_id, suffix)
    if settings.APP_MODE == AppMode.GCP:
        from google.cloud import storage

        client = storage.Client(project=settings.GCP_PROJECT_ID)
        blob = client.bucket(settings.GCS_BUCKET_NAME).blob(name)
        blob.upload_from_string(data, content_type=mime_type)
        return f"gs://{settings.GCS_BUCKET_NAME}/{name}"

    local_dir = local_data_dir() / GCS_SUBPREFIX / owner_key(owner_email)
    local_dir.mkdir(parents=True, exist_ok=True)
    path = local_dir / name.rsplit("/", 1)[-1]
    path.write_bytes(data)
    return path.as_uri()


def load_bytes(uri: str) -> bytes:
    checked = assert_own_uri(uri)
    if uri.startswith("gs://"):
        from google.cloud import storage

        bucket_name, _, blob_name = checked[len("gs://") :].partition("/")
        client = storage.Client(project=settings.GCP_PROJECT_ID)
        return client.bucket(bucket_name).blob(blob_name).download_as_bytes()
    return Path(checked).read_bytes()


def delete_bytes(uri: str | None) -> bool:
    """Best-effort removal; True when the object is gone."""
    if not uri:
        return True
    checked = assert_own_uri(uri)
    try:
        if uri.startswith("gs://"):
            from google.api_core.exceptions import NotFound
            from google.cloud import storage

            bucket_name, _, blob_name = checked[len("gs://") :].partition("/")
            client = storage.Client(project=settings.GCP_PROJECT_ID)
            try:
                client.bucket(bucket_name).blob(blob_name).delete()
            except NotFound:
                pass
        else:
            Path(checked).unlink(missing_ok=True)
        return True
    except Exception:  # noqa: BLE001 — cleanup must never block the delete
        logger.warning("[chat-files] could not delete stored object", exc_info=True)
        return False

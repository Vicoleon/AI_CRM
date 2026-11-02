"""Canonical workspace roots for local and GCS stored objects."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

from rubik_core.config import settings
from rubik_core.tenancy.context import current_workspace

_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}\Z")


def _safe_segment(part: str) -> str:
    if not isinstance(part, str) or part in (".", "..") or not _SEGMENT.fullmatch(part):
        raise ValueError("Stored object names require safe atomic path segments")
    return part


def object_name(*parts: str) -> str:
    """Build one GCS blob name beneath this workspace's exclusive prefix."""
    if not parts:
        raise ValueError("A stored object needs at least one name segment")
    slug = current_workspace()
    return "/".join(("workspaces", slug, *(_safe_segment(p) for p in parts)))


def safe_upload_name(filename: str) -> str:
    """Turn a user-visible filename into one safe stored path component.

    Display metadata keeps the original name. Ambiguous path syntax is refused
    before writing bytes; ordinary spaces and Unicode are normalized, with a
    short digest to keep normalized names distinct.
    """
    if (
        not isinstance(filename, str)
        or not filename
        or filename in (".", "..")
        or "/" in filename
        or "\\" in filename
        or "%" in filename
    ):
        raise ValueError("Invalid upload filename")
    normalized = (
        unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode()
    )
    stem, dot, extension = normalized.rpartition(".")
    if not dot:
        stem, extension = normalized, ""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "upload"
    extension = re.sub(r"[^A-Za-z0-9]+", "", extension)[:15]
    suffix = f".{extension}" if extension else ""
    digest = ""
    if (
        normalized != filename
        or re.sub(r"[^A-Za-z0-9._-]+", "_", normalized) != normalized
    ):
        digest = f"__{hashlib.sha256(filename.encode()).hexdigest()[:8]}"
    name = f"{stem[: 255 - len(suffix) - len(digest)]}{digest}{suffix}"
    return _safe_segment(name)


def local_data_dir() -> Path:
    """Return this workspace's canonical local storage directory."""
    root = Path(settings.LOCAL_DATA_DIR).expanduser().resolve() / "workspaces"
    selected = root / current_workspace()
    if selected.is_symlink():
        raise ValueError("Workspace data root cannot be a symlink")
    return selected.resolve()


def assert_own_uri(uri: str) -> str:
    """Validate a stored GCS or file URI before reading or deleting bytes.

    Returns a canonical local path or the original GCS URI for the storage
    client. Bare paths and encoded separators are refused so every caller uses
    the same unambiguous boundary.
    """
    if not isinstance(uri, str) or not uri or "%" in uri or "\\" in uri:
        raise ValueError("Stored object URI is invalid")
    parsed = urlsplit(uri)
    if parsed.query or parsed.fragment:
        raise ValueError("Stored object URI cannot have query or fragment")
    if parsed.scheme == "gs":
        if not settings.GCS_BUCKET_NAME or parsed.netloc != settings.GCS_BUCKET_NAME:
            raise ValueError("Stored object is outside this workspace's bucket")
        prefix = f"/workspaces/{current_workspace()}/"
        if not parsed.path.startswith(prefix):
            raise ValueError("Stored object is outside this workspace")
        pieces = parsed.path[1:].split("/")
        if len(pieces) < 4:
            raise ValueError("Stored object is outside this workspace")
        for part in pieces:
            _safe_segment(part)
        return uri
    if parsed.scheme == "file":
        if parsed.netloc or not parsed.path.startswith("/"):
            raise ValueError("Stored local URI is invalid")
        if any(part in (".", "..") for part in parsed.path.split("/")):
            raise ValueError("Stored local URI contains traversal")
        root = local_data_dir()
        candidate = Path(parsed.path).resolve()
        if candidate == root or not candidate.is_relative_to(root):
            raise ValueError("Stored object is outside this workspace")
        return str(candidate)
    raise ValueError("Stored object URI must be gs:// or file://")

"""Drive router — powers the Productivity → Documents tab.

UC-43 surfaces the user's recent Drive activity. UC-44 layers on:
  - folder picker: store per-user folder ids in ``WorkspaceSetting`` and
    scope ``/recent-activity`` to those folders if any are selected
  - "Add to Assets": download a Drive file's bytes and ingest into the
    same RAG corpus the upload-flow already feeds

UC-29 already calls ``GoogleDriveClient.list_recent_files`` inside the
priorities pipeline; we reuse the same client.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.auth import get_current_user
from rubik_core.auth.permissions import (
    KNOWLEDGE_BASE_UPLOAD_DENIED,
    has_permission,
)
from rubik_core.auth.registry import UserProfile
from rubik_core.config import AppMode, settings
from rubik_core.dam.registry import AssetMetadata, DAMRegistry
from rubik_core.database.db import get_db
from rubik_core.database.models import AuditLog, WorkspaceSetting
from rubik_core.tenancy.storage import local_data_dir, object_name

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/drive", tags=["Drive"])
dam_registry = DAMRegistry()


def _drive_prefs_key(user_email: str) -> str:
    """WorkspaceSetting key for this user's selected folder list.

    Stored as a per-user JSON blob to avoid a schema migration:
      ``user:<email>:drive_folder_ids = {"folders": [{"id": ..., "name": ...}]}``
    """
    return f"user:{user_email.lower()}:drive_folder_ids"


def _load_user_drive_folders(db: Session, user_email: str) -> list[dict]:
    row = (
        db.query(WorkspaceSetting)
        .filter(WorkspaceSetting.key == _drive_prefs_key(user_email))
        .one_or_none()
    )
    if not row or not isinstance(row.value, dict):
        return []
    return list(row.value.get("folders") or [])


class DriveActivityItem(BaseModel):
    """One row in the Productivity → Documents list."""

    id: str
    name: str
    mime_type: str
    modified_time: str | None = None
    created_time: str | None = None
    last_modifying_user_email: str | None = None
    last_modifying_user_name: str | None = None
    is_shared: bool = False
    is_owned_by_me: bool = False
    web_view_link: str | None = None
    viewed_by_me_time: str | None = None


class DriveActivityResponse(BaseModel):
    days_back: int
    items: list[DriveActivityItem] = []


def _normalize_drive_file(raw: dict[str, Any], me: str) -> DriveActivityItem:
    last_mod = raw.get("lastModifyingUser") or {}
    owners = raw.get("owners") or []
    is_owned = any((o.get("emailAddress") or "").lower() == me.lower() for o in owners)
    return DriveActivityItem(
        id=raw.get("id", ""),
        name=raw.get("name", "(untitled)"),
        mime_type=raw.get("mimeType", ""),
        modified_time=raw.get("modifiedTime"),
        created_time=raw.get("createdTime"),
        last_modifying_user_email=last_mod.get("emailAddress"),
        last_modifying_user_name=last_mod.get("displayName"),
        is_shared=bool(raw.get("shared")),
        is_owned_by_me=is_owned,
        web_view_link=raw.get("webViewLink"),
        viewed_by_me_time=raw.get("viewedByMeTime"),
    )


@router.get("/recent-activity", response_model=DriveActivityResponse)
def get_recent_drive_activity(
    days_back: int = Query(
        7,
        ge=1,
        le=30,
        description="Look back this many days (1–30). Default: 7.",
    ),
    max_results: int = Query(
        25,
        ge=1,
        le=100,
        description="Cap on items returned (1–100). Default: 25.",
    ),
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DriveActivityResponse:
    """Return the current user's recently modified Drive files.

    Powers the Productivity → Documents tab. Uses the same
    ``GoogleDriveClient.list_recent_files`` primitive UC-29 calls.
    Strictly per-user — domain-wide delegation impersonates only
    ``user.email``.
    """
    from rubik_core.tools.drive import GoogleDriveClient

    try:
        db.add(
            AuditLog(
                actor_email=user.email,
                action="ACCESS_DRIVE_RECENT_ACTIVITY",
                status="success",
                details=f"days_back={days_back} max_results={max_results}",
            )
        )
        db.commit()
    except Exception:  # noqa: BLE001
        # Audit-log failure must not block the actual response.
        db.rollback()
        logger.exception("Failed to write AuditLog for drive recent-activity")

    client = GoogleDriveClient(user_email=user.email)
    if not client.service:
        raise HTTPException(
            status_code=503,
            detail="Drive service not initialized for the current user.",
        )

    # UC-44: if the user has selected folders, scope the listing to them.
    # Otherwise fall back to UC-43's "all of Drive" recent activity.
    selected_folders = _load_user_drive_folders(db, user.email)
    folder_ids = [f["id"] for f in selected_folders if f.get("id")]

    try:
        if folder_ids:
            raw_files = client.list_files_in_folders(
                folder_ids,
                days_back=days_back,
                max_results=max_results,
            )
        else:
            raw_files = client.list_recent_files(
                days_back=days_back,
                max_results=max_results,
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Drive listing failed for %s", user.email)
        raise HTTPException(
            status_code=502,
            detail=f"Drive listing failed: {exc}",
        )

    items = [_normalize_drive_file(f, me=user.email) for f in raw_files]
    return DriveActivityResponse(days_back=days_back, items=items)


# ── UC-44: folder picker ─────────────────────────────────────────────────────


class DriveFolder(BaseModel):
    id: str
    name: str
    web_view_link: str | None = None


class DriveFoldersResponse(BaseModel):
    items: list[DriveFolder] = []


@router.get("/folders", response_model=DriveFoldersResponse)
def list_drive_folders(
    q: str = Query(
        "",
        max_length=120,
        description="Optional name-contains filter. Empty returns the user's most-recent folders.",
    ),
    max_results: int = Query(50, ge=1, le=100),
    user: UserProfile = Depends(get_current_user),
) -> DriveFoldersResponse:
    """Search the user's Drive folders by name.

    Used by the Documents tab's folder-picker modal. Returns only
    folder metadata (no contents). Empty query returns the most-recently
    modified folders so the user has something to scroll through.
    """
    from rubik_core.tools.drive import GoogleDriveClient

    client = GoogleDriveClient(user_email=user.email)
    if not client.service:
        raise HTTPException(
            status_code=503,
            detail="Drive service not initialized for the current user.",
        )

    try:
        raw = client.list_folders(query=q, max_results=max_results)
    except Exception as exc:  # noqa: BLE001
        logger.exception("list_folders failed for %s", user.email)
        raise HTTPException(
            status_code=502, detail=f"Drive folder search failed: {exc}"
        )

    items = [
        DriveFolder(
            id=f.get("id", ""),
            name=f.get("name", "(untitled)"),
            web_view_link=f.get("webViewLink"),
        )
        for f in raw
        if f.get("id")
    ]
    return DriveFoldersResponse(items=items)


class DrivePreferences(BaseModel):
    """Per-user Drive surfacing preferences."""

    folders: list[DriveFolder] = []


@router.get("/preferences", response_model=DrivePreferences)
def get_drive_preferences(
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DrivePreferences:
    """Return the user's saved Drive folder selection (UC-44)."""
    rows = _load_user_drive_folders(db, user.email)
    return DrivePreferences(
        folders=[DriveFolder(**r) for r in rows if r.get("id")],
    )


@router.put("/preferences", response_model=DrivePreferences)
def save_drive_preferences(
    body: DrivePreferences,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DrivePreferences:
    """Save the user's selected folder list (UC-44).

    Stored in ``WorkspaceSetting`` keyed by ``user:<email>:drive_folder_ids``
    — no schema migration needed. Saving an empty list resets to "all
    of Drive recent activity" (UC-43 default).
    """
    folders_json = [
        {"id": f.id, "name": f.name, "web_view_link": f.web_view_link}
        for f in body.folders
        if f.id
    ]
    key = _drive_prefs_key(user.email)
    row = db.query(WorkspaceSetting).filter(WorkspaceSetting.key == key).one_or_none()
    if row is None:
        row = WorkspaceSetting(key=key, value={"folders": folders_json})
        db.add(row)
    else:
        row.value = {"folders": folders_json}
    db.commit()

    logger.info(
        "[uc44] %s saved %d Drive folder(s) for surfacing",
        user.email,
        len(folders_json),
    )
    return DrivePreferences(folders=body.folders)


# ── UC-44: add-to-assets ─────────────────────────────────────────────────────


def _safe_drive_filename(name: str) -> str:
    """Collapse a Drive file name into a single safe filesystem component.

    Drive names are free-form and routinely contain ``/`` — e.g. the date in
    a Gemini-notes doc, ``"L10 - 2026/06/24 17:26 EDT - Notes by Gemini"``.
    Used verbatim as a path component, those slashes are read as directory
    separators and ``open(local_dir / name, "wb")`` blows up with
    ``FileNotFoundError`` (the intermediate dirs don't exist), surfacing to
    the user as a bare 500. Browser uploads never hit this because filenames
    can't contain slashes; Drive exports can. Replace any path separator or
    otherwise illegal character so the whole thing stays one file.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    # Avoid leading/trailing dots or spaces (hidden files / Windows quirks).
    cleaned = cleaned.strip("._")
    return cleaned[:255] or "drive-file"


class AddDriveFileToAssetsRequest(BaseModel):
    topic: str
    department: str
    is_company_wide: bool = False


class AddDriveFileToAssetsResponse(BaseModel):
    doc_id: str
    status: str
    original_filename: str


@router.post(
    "/{file_id}/add-to-assets",
    response_model=AddDriveFileToAssetsResponse,
)
def add_drive_file_to_assets(
    file_id: str,
    body: AddDriveFileToAssetsRequest,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AddDriveFileToAssetsResponse:
    """Download a Drive file and ingest it into the RAG corpus as an Asset.

    Surfaces the existing ``IngestionProcessor`` pipeline behind a one-
    click button on each Documents-tab row. Native Google Docs / Sheets /
    Slides are exported to their .docx / .xlsx / .pptx equivalents before
    ingestion. Binary files (PDF / docx / txt / csv) download as-is.
    """
    # Same gate as POST /api/ingest/ and POST /api/dam/upload — one capability,
    # checked one way. This used to hardcode `"editor" in user.roles`, which
    # denied anyone holding assets:create through a differently-named role
    # (e.g. "super user") even though the other two upload paths let them in.
    if not has_permission(user, "assets", "create"):
        raise HTTPException(status_code=403, detail=KNOWLEDGE_BASE_UPLOAD_DENIED)

    from rubik_core.tools.drive import GoogleDriveClient

    drive = GoogleDriveClient(user_email=user.email)
    if not drive.service:
        raise HTTPException(
            status_code=503,
            detail="Drive service not initialized for the current user.",
        )

    # 1. Resolve metadata + bytes from Drive.
    try:
        meta = (
            drive.service.files()
            .get(
                fileId=file_id,
                fields="id, name, mimeType",
            )
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Drive metadata fetch failed for %s", file_id)
        raise HTTPException(
            status_code=404 if "404" in str(exc) else 502,
            detail=f"Drive file lookup failed: {exc}",
        )

    file_bytes = drive.download_file_bytes(file_id)
    if not file_bytes:
        raise HTTPException(
            status_code=502,
            detail=(
                "Drive returned no bytes for this file — unsupported mime type "
                "or upstream error."
            ),
        )

    base_name = meta.get("name") or f"drive-{file_id}"
    # If the Drive name lacks an extension (typical for Google-native docs),
    # append the one matching our export target so the downstream loader
    # picks the right parser.
    ext = drive.get_export_extension(meta.get("mimeType", ""))
    if ext and not base_name.lower().endswith(ext):
        base_name = base_name + ext
    # Drive names can contain "/" and other path-hostile characters — collapse
    # them so the name is a single, writable filesystem component.
    base_name = _safe_drive_filename(base_name)

    # 2. Hand to the same processor /api/ingest uses.
    from backend.routers.ingest import get_processor

    processor = get_processor()

    if settings.APP_MODE == AppMode.GCP:
        tmp_dir = tempfile.TemporaryDirectory()
        file_path = Path(tmp_dir.name) / base_name
    else:
        local_dir = local_data_dir()
        local_dir.mkdir(parents=True, exist_ok=True)
        file_path = local_dir / base_name

    try:
        with open(file_path, "wb") as fh:
            fh.write(file_bytes)

        doc_id = processor.loader.generate_doc_id(file_path)

        if settings.APP_MODE == AppMode.GCP:
            recorded_path = (
                f"gs://{settings.GCS_BUCKET_NAME}/{object_name('rag', base_name)}"
            )
        else:
            recorded_path = file_path.as_uri()

        now = datetime.utcnow()
        asset = AssetMetadata(
            doc_id=doc_id,
            owner=user.email,
            topic=body.topic,
            department=body.department,
            # Uploads land live in the RAG (the approval workflow was removed) —
            # "draft" would make a Drive-added doc permanently non-retrievable.
            status="approved",
            is_company_wide=body.is_company_wide,
            source_path=recorded_path,
            original_filename=base_name,
            created_at=now,
            updated_at=now,
        )
        dam_registry.register_asset(asset)

        result_dict = processor.process_file(
            file_path,
            manual_metadata={
                "owner": user.email,
                "approver": "",
                "topic": body.topic,
                "department": body.department,
                "status": "approved",
                "is_company_wide": body.is_company_wide,
                "source": "drive",
                "drive_file_id": file_id,
            },
        )
        result = result_dict.get("result") if isinstance(result_dict, dict) else None
        if not result or not getattr(result, "success", False):
            dam_registry.update_asset(doc_id, {"status": "failed"})
            raise HTTPException(
                status_code=500,
                detail=(
                    f"Ingestion failed for {base_name}: "
                    f"{getattr(result, 'error', 'unknown error')}"
                ),
            )

        try:
            db.add(
                AuditLog(
                    actor_email=user.email,
                    action="ADD_DRIVE_FILE_TO_ASSETS",
                    status="success",
                    details=(
                        f"file_id={file_id} doc_id={doc_id} "
                        f"topic={body.topic} department={body.department}"
                    ),
                )
            )
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("Failed to write AuditLog for add-drive-to-assets")

        return AddDriveFileToAssetsResponse(
            doc_id=doc_id,
            status="ingested",
            original_filename=base_name,
        )
    finally:
        try:
            if file_path.exists():
                os.unlink(file_path)
        except Exception:  # noqa: BLE001
            pass

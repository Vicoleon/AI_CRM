"""Files in Ask Cubie: ``/api/chat/files``. Everything here is the caller's own.

* ``POST /api/chat/files`` attaches one file (multipart ``file``, optional
  ``session_id``). Gate, per the repo convention: ``CHAT_FILE_UPLOAD_ENABLED``
  off answers 404, a person outside the ``chat_file_upload`` roster 403.
* ``POST /api/chat/files/{id}/publish`` copies one of the caller's files into
  the shared Library (Assets) on their say-so, through the ordinary Assets
  upload and its permission (``assets:create``, the same as Drive's "Add to
  Knowledge"). The private file stays private.
* ``GET /api/chat/files``, ``GET /api/chat/files/{id}/download`` and
  ``DELETE /api/chat/files/{id}`` are NOT roster-gated: a person taken off the
  roster, or an environment with the switch off, still lets them see, download
  and delete what they attached. A file that is not theirs is a 404 whether or
  not it exists.

Mounted on ``core`` in ``backend/main.py``, so the copilot pilot gate applies
first. Not on the impersonation allowlist (``rubik_core/auth/impersonation.py``),
so an admin viewing Cubie as someone else gets a 403 on every route here.
"""

from __future__ import annotations

import io
import logging
from urllib.parse import quote

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
)
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from backend.audit import record_audit
from backend.auth import get_current_user
from backend.jobs.launcher import reject_support_background_work
from rubik_core.auth.permissions import KNOWLEDGE_BASE_UPLOAD_DENIED, has_permission
from rubik_core.brand import PRODUCT_NAME
from rubik_core.chat_files import service, storage
from rubik_core.chat_files.extract import ChatFileError
from rubik_core.config import settings
from rubik_core.database.db import get_db
from rubik_core.database.schemas import UserProfile
from rubik_core.features import feature_allows

logger = logging.getLogger(__name__)

FEATURE_KEY = "chat_file_upload"

router = APIRouter(prefix="/chat/files", tags=["chat-files"])


def require_chat_file_upload(
    user: UserProfile = Depends(get_current_user),
) -> UserProfile:
    """Gate for attaching: 404 when switched off, 403 when not on the roster."""
    if not settings.CHAT_FILE_UPLOAD_ENABLED:
        raise HTTPException(status_code=404, detail="Not found.")
    if not feature_allows(FEATURE_KEY, user):
        raise HTTPException(
            status_code=403,
            detail="Attaching files is not turned on for you. Ask your admin.",
        )
    return user


@router.post("")
async def attach_file(
    file: UploadFile = File(...),
    session_id: str | None = Form(None),
    user: UserProfile = Depends(require_chat_file_upload),
    db: Session = Depends(get_db),
) -> dict:
    limit = settings.CHAT_FILE_MAX_BYTES
    data = await file.read(limit + 1)
    try:
        added = await service.add_upload(
            db,
            owner_email=user.email,
            filename=file.filename or "",
            data=data,
            session_id=session_id,
        )
    except ChatFileError as exc:
        status = 413 if len(data) > limit else 422
        raise HTTPException(status_code=status, detail=str(exc)) from None
    return {**service.to_public(added.upload), "reused": added.reused}


@router.get("")
async def list_files(
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    rows = await run_in_threadpool(service.list_uploads, db, user.email)
    published = service.publications_by_upload(db, user.email, [r.id for r in rows])
    return {
        "files": [
            {**service.to_public(r), "published": published.get(r.id, [])} for r in rows
        ],
        "can_attach": service.enabled_for(user),
        "can_publish": has_permission(user, "assets", "create"),
    }


@router.get("/{upload_id}/download")
async def download_file(
    upload_id: str,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    try:
        (row,) = service.owned(db, user.email, [upload_id])
    except LookupError:
        raise HTTPException(status_code=404, detail="File not found.") from None
    if not row.storage_uri:
        raise HTTPException(status_code=404, detail="File not found.")
    try:
        data = await run_in_threadpool(storage.load_bytes, row.storage_uri)
    except Exception:  # noqa: BLE001
        logger.warning("[chat-files] could not load %s for download", row.id)
        raise HTTPException(
            status_code=502,
            detail=f"{PRODUCT_NAME} couldn't fetch that file. Try again.",
        ) from None
    return Response(
        content=data,
        media_type=row.mime_type or "application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(row.filename)}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


@router.delete("/{upload_id}")
async def delete_file(
    upload_id: str,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    try:
        deleted = await run_in_threadpool(
            service.delete_upload, db, user.email, upload_id
        )
    except service.DeleteFailed:
        raise HTTPException(
            status_code=502,
            detail=f"{PRODUCT_NAME} couldn't delete that file just now. Try again in a minute.",
        ) from None
    if not deleted:
        raise HTTPException(status_code=404, detail="File not found.")
    return {"deleted": True}


class PublishBody(BaseModel):
    topic: str
    department: str
    is_company_wide: bool = False
    # Blank = the summary Cubie wrote when the file was attached.
    description: str = ""


@router.post("/{upload_id}/publish")
async def publish_file(
    upload_id: str,
    body: PublishBody,
    background_tasks: BackgroundTasks,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Copy one of the caller's private files into Assets.

    Runs the ordinary Assets upload (``backend.routers.dam.upload_document``)
    with the file's bytes, so the copy is filed, deduplicated, described and
    indexed exactly like a document uploaded there: into its department's or
    the company-wide knowledge base. A file Assets cannot index as itself (a
    picture, a scan, a spreadsheet) goes in as a Word copy of what Cubie read.
    """
    reject_support_background_work()
    if not has_permission(user, "assets", "create"):
        raise HTTPException(status_code=403, detail=KNOWLEDGE_BASE_UPLOAD_DENIED)
    try:
        (row,) = service.owned(db, user.email, [upload_id])
    except LookupError:
        raise HTTPException(status_code=404, detail="File not found.") from None
    topic, department = body.topic.strip(), body.department.strip()
    if not topic or not department:
        raise HTTPException(
            status_code=422, detail="Pick a topic and a department to publish it to."
        )
    if not row.storage_uri:
        raise HTTPException(status_code=404, detail="File not found.")
    try:
        original = await run_in_threadpool(storage.load_bytes, row.storage_uri)
        name, payload, rendition = await run_in_threadpool(
            service.publish_payload, db, row, original
        )
    except ChatFileError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except Exception:  # noqa: BLE001
        logger.exception("[chat-files] could not prepare %s for Assets", row.id)
        raise HTTPException(
            status_code=502,
            detail=f"{PRODUCT_NAME} couldn't prepare that file. Try again.",
        ) from None

    from backend.routers.dam import get_file_processor, upload_document
    from backend.routers.ingest import get_processor

    try:
        result = await upload_document(
            background_tasks=background_tasks,
            file=UploadFile(file=io.BytesIO(payload), filename=name),
            topic=topic,
            department=department,
            description=body.description.strip() or (row.summary or ""),
            tags="from-ask-cubie",
            is_company_wide=body.is_company_wide,
            force=False,
            replace_doc_id="",
            user=user,
            file_processor=get_file_processor(get_processor()),
        )
    except HTTPException as exc:
        if exc.status_code == 409 and isinstance(exc.detail, dict):
            existing = exc.detail.get("existing_filename") or "another document"
            raise HTTPException(
                status_code=409,
                detail=f'This file is already in Assets, as "{existing}".',
            ) from None
        raise
    service.record_publication(
        db,
        row,
        doc_id=result["doc_id"],
        filename=name,
        department=department,
        is_company_wide=body.is_company_wide,
        rendition=rendition,
    )
    record_audit(
        db,
        actor_email=user.email,
        action="CHAT_FILE_PUBLISH",
        target=result["doc_id"],
        details={
            "upload_id": row.id,
            "published_as": name,
            "department": department,
            "is_company_wide": body.is_company_wide,
            "rendition": rendition,
        },
    )
    return {
        "doc_id": result["doc_id"],
        "filename": name,
        "rendition": rendition,
        "status": result.get("status", "processing"),
    }

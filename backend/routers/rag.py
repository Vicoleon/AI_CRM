from __future__ import annotations

"""
/api/rag — CRUD for the audience-shaped RAG corpora.

Each RAG slug maps to a Vertex AI RAG corpus: `companywide` plus one entry
per row in the `departments` table. All write paths require authentication;
deletes and the bootstrap endpoint are admin-only because they touch shared,
prod-adjacent corpora.

Routes:
    GET    /api/rag                       — list known RAGs (registry view)
    POST   /api/rag/bootstrap             — create any missing corpora (admin)
    POST   /api/rag/reconcile             — alias for bootstrap; convenience for
                                            backfilling after a department was
                                            added before this code shipped (admin)
    GET    /api/rag/{slug}/documents      — list documents in a RAG
    POST   /api/rag/{slug}/documents      — upload + import a document
    PUT    /api/rag/{slug}/documents      — replace a document (same filename)
    DELETE /api/rag/{slug}/documents      — delete by gcs_uri or rag_file_name (admin)
"""

import logging
import shutil
import tempfile
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    UploadFile,
)
from pydantic import BaseModel

from backend.auth import get_current_user
from backend.models.auth import User
from rubik_core.auth.permissions import has_permission
from rubik_core.config import AppMode, settings
from rubik_core.rag import (
    RagManager,
    RagOperationError,
    current_registry,
    is_exec_only_slug,
)
from rubik_core.tenancy.storage import safe_upload_name

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/rag", tags=["rag"])


# ─────────────────────────────────────────────────────────────────────────────
# Dependencies
# ─────────────────────────────────────────────────────────────────────────────


def get_rag_manager() -> RagManager:
    """
    Build a `RagManager` for the current request.

    Refuses to construct outside of GCP mode — these RAGs are GCP-native and
    silently swapping to a local fake would mask "data didn't actually land
    in prod" bugs.
    """
    if settings.APP_MODE != AppMode.GCP:
        raise HTTPException(
            status_code=400,
            detail="RAG operations require APP_MODE=gcp.",
        )
    try:
        return RagManager()
    except RagOperationError as e:
        logger.error("RagManager construction failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def _validate_slug(slug: str, manager: RagManager) -> None:
    if slug not in manager.registry:
        valid = ", ".join(sorted(manager.registry))
        raise HTTPException(
            status_code=404,
            detail=f"Unknown RAG {slug!r}. Known: {valid}",
        )


def _can_see_exec_only(user: User) -> bool:
    """Exec-only RAGs (the confidential VTO) are visible only to the
    Exec team and admins."""
    return getattr(user, "is_admin", False) or getattr(user, "is_exec", False)


def _authorize_slug(slug: str, user: User, manager: RagManager) -> None:
    """Validate the slug and enforce Exec-only visibility. For a restricted
    slug requested by a non-exec user we return 404 (identical to an unknown
    slug) so the corpus's very existence stays hidden, matching how the VTO
    page/nav are hidden."""
    if is_exec_only_slug(slug) and not _can_see_exec_only(user):
        raise HTTPException(status_code=404, detail=f"Unknown RAG {slug!r}.")
    _validate_slug(slug, manager)


def _require_admin(user: User) -> None:
    if not getattr(user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin access required")


# ─────────────────────────────────────────────────────────────────────────────
# Response models
# ─────────────────────────────────────────────────────────────────────────────


class RagInfo(BaseModel):
    slug: str
    display_name: str
    description: str
    gcs_subprefix: str


class BootstrapResponse(BaseModel):
    status: str
    corpora: dict[str, str]


class RagDocumentInfo(BaseModel):
    rag_file_name: str
    display_name: str
    gcs_uri: str | None
    create_time: str | None


class DocumentMutationResponse(BaseModel):
    status: str
    rag: str
    gcs_uri: str
    corpus_name: str


class DocumentDeleteResponse(BaseModel):
    status: str
    rag: str
    rag_deleted: bool
    gcs_deleted: bool


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────


@router.get("", response_model=list[RagInfo])
async def list_rags(user: User = Depends(get_current_user)):
    """List all RAGs in the live registry (companywide + departments).
    No GCP calls — safe in any APP_MODE."""
    if not has_permission(user, "assets", "read"):
        raise HTTPException(
            status_code=403, detail="Permission denied: cannot read RAGs"
        )
    can_see_exec_only = _can_see_exec_only(user)
    return [
        RagInfo(
            slug=spec.slug,
            display_name=spec.display_name,
            description=spec.description,
            gcs_subprefix=spec.gcs_subprefix,
        )
        for spec in current_registry().values()
        if can_see_exec_only or not is_exec_only_slug(spec.slug)
    ]


@router.post("/bootstrap", response_model=BootstrapResponse)
async def bootstrap_rags(
    user: User = Depends(get_current_user),
    manager: RagManager = Depends(get_rag_manager),
):
    """Create any missing Vertex AI RAG corpora for every slug in the live
    registry. Admin-only. Idempotent: existing corpora are returned untouched."""
    _require_admin(user)
    try:
        corpora = manager.ensure_all_corpora()
    except RagOperationError as e:
        logger.error("RAG bootstrap failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e
    return BootstrapResponse(status="ok", corpora=corpora)


@router.post("/reconcile", response_model=BootstrapResponse)
async def reconcile_rags(
    user: User = Depends(get_current_user),
    manager: RagManager = Depends(get_rag_manager),
):
    """Alias for /bootstrap — backfills any RAGs that were added to the
    registry after the initial bootstrap (e.g. department added before this
    code shipped, or department-create hook failed transiently)."""
    return await bootstrap_rags(user=user, manager=manager)


@router.get("/{slug}/documents", response_model=list[RagDocumentInfo])
async def list_rag_documents(
    slug: str,
    user: User = Depends(get_current_user),
    manager: RagManager = Depends(get_rag_manager),
):
    """List all documents in a RAG's corpus."""
    if not has_permission(user, "assets", "read"):
        raise HTTPException(
            status_code=403, detail="Permission denied: cannot read RAGs"
        )
    _authorize_slug(slug, user, manager)
    try:
        docs = manager.list_documents(slug)
    except RagOperationError as e:
        logger.error("list_documents failed for rag=%s: %s", slug, e)
        raise HTTPException(status_code=500, detail=str(e)) from e
    return [
        RagDocumentInfo(
            rag_file_name=d.rag_file_name,
            display_name=d.display_name,
            gcs_uri=d.gcs_uri,
            create_time=d.create_time,
        )
        for d in docs
    ]


@router.post("/{slug}/documents", response_model=DocumentMutationResponse)
async def add_rag_document(
    slug: str,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    manager: RagManager = Depends(get_rag_manager),
):
    """Upload a file to a RAG's GCS sub-prefix and import it into the corpus."""
    if not has_permission(user, "assets", "create"):
        raise HTTPException(
            status_code=403, detail="Permission denied: cannot add to RAGs"
        )
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")
    _authorize_slug(slug, user, manager)
    try:
        stored_name = safe_upload_name(file.filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp) / stored_name
        try:
            with open(tmp_path, "wb") as out:
                shutil.copyfileobj(file.file, out)
            result = manager.add_document(slug, tmp_path)
        except RagOperationError as e:
            logger.error(
                "add_document failed | rag=%s | file=%s | user=%s: %s",
                slug,
                file.filename,
                user.email,
                e,
            )
            raise HTTPException(status_code=500, detail=str(e)) from e

    logger.info(
        "RAG document added | rag=%s | file=%s | user=%s | uri=%s",
        slug,
        file.filename,
        user.email,
        result["gcs_uri"],
    )
    return DocumentMutationResponse(status="ok", **result)


@router.put("/{slug}/documents", response_model=DocumentMutationResponse)
async def update_rag_document(
    slug: str,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    manager: RagManager = Depends(get_rag_manager),
):
    """Replace a RAG document keyed by filename: deletes the old ragFile, re-imports."""
    if not has_permission(user, "assets", "update"):
        raise HTTPException(
            status_code=403, detail="Permission denied: cannot update RAGs"
        )
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")
    _authorize_slug(slug, user, manager)
    try:
        stored_name = safe_upload_name(file.filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp) / stored_name
        try:
            with open(tmp_path, "wb") as out:
                shutil.copyfileobj(file.file, out)
            result = manager.update_document(slug, tmp_path)
        except RagOperationError as e:
            logger.error(
                "update_document failed | rag=%s | file=%s | user=%s: %s",
                slug,
                file.filename,
                user.email,
                e,
            )
            raise HTTPException(status_code=500, detail=str(e)) from e

    logger.info(
        "RAG document updated | rag=%s | file=%s | user=%s | uri=%s",
        slug,
        file.filename,
        user.email,
        result["gcs_uri"],
    )
    return DocumentMutationResponse(status="ok", **result)


@router.delete("/{slug}/documents", response_model=DocumentDeleteResponse)
async def delete_rag_document(
    slug: str,
    gcs_uri: str | None = Query(default=None, description="gs:// URI to delete"),
    rag_file_name: str | None = Query(
        default=None, description="Vertex ragFile resource name to delete"
    ),
    also_delete_gcs: bool = Query(
        default=True, description="Also delete the underlying GCS blob"
    ),
    user: User = Depends(get_current_user),
    manager: RagManager = Depends(get_rag_manager),
):
    """Delete one document from a RAG. Admin-only — these corpora are shared
    infrastructure and a misconfigured client should never wipe one out."""
    _require_admin(user)
    if not gcs_uri and not rag_file_name:
        raise HTTPException(
            status_code=400,
            detail="Provide gcs_uri or rag_file_name to identify the document.",
        )
    _authorize_slug(slug, user, manager)
    try:
        result = manager.delete_document(
            slug,
            gcs_uri=gcs_uri,
            rag_file_name=rag_file_name,
            also_delete_gcs=also_delete_gcs,
        )
    except RagOperationError as e:
        logger.error(
            "delete_document failed | rag=%s | uri=%s | rag_file=%s | user=%s: %s",
            slug,
            gcs_uri,
            rag_file_name,
            user.email,
            e,
        )
        raise HTTPException(status_code=500, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    logger.info(
        "RAG document deleted | rag=%s | uri=%s | rag_file=%s | user=%s | result=%s",
        slug,
        gcs_uri,
        rag_file_name,
        user.email,
        result,
    )
    return DocumentDeleteResponse(
        status="ok",
        rag=slug,
        rag_deleted=result["rag_deleted"],
        gcs_deleted=result["gcs_deleted"],
    )


__all__ = ["router", "get_rag_manager"]

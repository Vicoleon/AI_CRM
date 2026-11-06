from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.auth import get_current_user
from rubik_core.config import AppMode, settings
from rubik_core.database.repository import SQLMetadataRegistry
from rubik_core.database.schemas import UserProfile
from rubik_core.rag import slugify_department

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/departments", tags=["departments"])

dept_manager = SQLMetadataRegistry()


class DepartmentRequest(BaseModel):
    department: str


def _ensure_rag_for_department(label: str) -> dict[str, str | None]:
    """Best-effort: create a Vertex RAG corpus for a newly-added department.

    Failure is logged but does NOT roll back the department insert — admins
    can run `POST /api/rag/reconcile` later to backfill any missed corpus.
    Returns a dict the route handler can include in the response body.
    """
    if settings.APP_MODE != AppMode.GCP:
        return {
            "slug": slugify_department(label),
            "rag_status": "skipped (not GCP mode)",
        }

    slug = slugify_department(label)
    if not slug:
        return {"slug": "", "rag_status": "skipped (empty slug)"}

    try:
        # Lazy import: avoids loading vertexai SDK in non-GCP processes.
        from rubik_core.rag import RagManager

        mgr = RagManager()
        # The department was just inserted, but `current_registry()` reads it
        # fresh from SQL inside RagManager.__init__, so the new slug is in
        # mgr.registry without needing an explicit refresh.
        corpus_name = mgr.ensure_corpus(slug)
        logger.info(
            "Ensured RAG corpus for new department | label=%r | slug=%s | corpus=%s",
            label,
            slug,
            corpus_name,
        )
        return {"slug": slug, "rag_status": "ok", "corpus_name": corpus_name}
    except Exception as e:
        logger.exception(
            "Failed to ensure RAG corpus for department %r (slug=%s); "
            "department insert NOT rolled back. Admin can backfill via "
            "POST /api/rag/reconcile.",
            label,
            slug,
        )
        return {"slug": slug, "rag_status": f"failed: {e}"}


@router.get("/", response_model=list[str])
async def list_departments(user: UserProfile = Depends(get_current_user)):
    """Get all available departments. Any authenticated user can read."""
    return dept_manager.get_departments()


@router.post("/")
async def add_department(
    request: DepartmentRequest,
    user: UserProfile = Depends(get_current_user),
):
    """Add a new department, then ensure its RAG corpus exists. Admin only."""
    if not user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="Permission denied: only Admins can add departments",
        )

    success = dept_manager.add_department(request.department)
    if not success:
        raise HTTPException(status_code=400, detail="Department already exists")

    rag_info = _ensure_rag_for_department(request.department)
    return {
        "message": f"Department '{request.department}' added successfully",
        "rag": rag_info,
    }


@router.delete("/{department}")
async def remove_department(
    department: str,
    user: UserProfile = Depends(get_current_user),
):
    """Remove a department. Admin only.

    Note: the underlying Vertex RAG corpus is NOT auto-deleted — that's a
    manual admin action via the Vertex console (or a future explicit
    /api/rag/{slug}/destroy endpoint). The corpus simply leaves the live
    registry and stops being queryable through `rags_for_user_email`.
    """
    if not user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="Permission denied: only Admins can remove departments",
        )

    success = dept_manager.remove_department(department)
    if success:
        slug = slugify_department(department)
        logger.warning(
            "Removed department %r (slug=%s); Vertex RAG corpus '%s_rag' was NOT deleted "
            "and may still hold documents. Admin must clean up manually.",
            department,
            slug,
            slug,
        )
        return {
            "message": f"Department '{department}' removed successfully",
            "rag_corpus_warning": (
                f"Vertex RAG corpus '{slug}_rag' still exists and was not auto-deleted. "
                "Delete via Vertex console if intended."
            ),
        }
    raise HTTPException(status_code=404, detail="Department not found")

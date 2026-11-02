"""Per-department shared Drive folders on the Productivity page.

Two surfaces on one router (prefix ``/department-folders``):

* ``GET /department-folders/mine`` — any authenticated user; returns the folders
  for *their* department plus company-wide ones (resolved by
  :func:`department_folders_store.list_for_user`).
* ``/department-folders/admin*`` — admin-only CRUD (``admin_required``) for
  curating the folders. Modeled on the UC-23 playbook admin endpoints.

Departments are data (the ``departments`` table); a NULL department row is
company-wide. Nothing here is Sales-specific — it works for any department.
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.auth import admin_required, get_current_user
from rubik_core.database.schemas import UserProfile
from rubik_core.productivity import department_folders_store as store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/department-folders", tags=["productivity"])


# Matches the folder id in a Drive URL like
#   https://drive.google.com/drive/folders/<ID>
#   https://drive.google.com/drive/u/0/folders/<ID>?usp=sharing
_DRIVE_FOLDER_RE = re.compile(r"/folders/([A-Za-z0-9_-]+)")


def _normalize_folder_id(raw: str) -> str:
    """Accept either a bare Drive folder id or a full Drive URL and return the
    id. Admins paste whatever they copy from the browser — be forgiving."""
    raw = (raw or "").strip()
    m = _DRIVE_FOLDER_RE.search(raw)
    if m:
        return m.group(1)
    # Strip any stray query string if they pasted ".../<id>?usp=..."
    return raw.split("?", 1)[0].rstrip("/")


class FolderCreate(BaseModel):
    folder_id: str = Field(min_length=1)  # bare id OR a Drive URL
    folder_name: str = Field(min_length=1, max_length=200)
    department: str | None = None  # NULL = company-wide
    web_view_link: str | None = None
    is_active: bool = True
    sort_order: int = 100


# ── User-facing read ──────────────────────────────────────────────────────────


@router.get("/mine")
def my_department_folders(user: UserProfile = Depends(get_current_user)) -> dict:
    """Shared Drive folders for the caller's department + company-wide ones."""
    rows = store.list_for_user(user.department)
    return {
        "department": user.department,
        "folders": [r.to_dict() for r in rows],
    }


# ── Admin CRUD ────────────────────────────────────────────────────────────────


@router.get("/admin")
def list_department_folders(
    department: str | None = None,
    include_inactive: bool = True,
    _: UserProfile = Depends(admin_required),
) -> list[dict]:
    """List curated folders; optionally filter by department."""
    rows = store.list_folders(
        department=department if department is not None else "__any__",
        include_inactive=include_inactive,
    )
    return [r.to_dict() for r in rows]


@router.get("/admin/{row_id}")
def get_department_folder(
    row_id: str,
    _: UserProfile = Depends(admin_required),
) -> dict:
    row = store.get_folder(row_id)
    if row is None:
        raise HTTPException(status_code=404, detail="folder not found")
    return row.to_dict()


@router.post("/admin", status_code=201)
def create_department_folder(
    payload: FolderCreate,
    user: UserProfile = Depends(admin_required),
) -> dict:
    folder_id = _normalize_folder_id(payload.folder_id)
    if not folder_id:
        raise HTTPException(status_code=400, detail="folder_id is required")
    # Default the deep link to the canonical Drive folder URL when omitted.
    link = (
        payload.web_view_link or f"https://drive.google.com/drive/folders/{folder_id}"
    )
    row = store.create_folder(
        folder_id=folder_id,
        folder_name=payload.folder_name,
        department=payload.department,
        actor_email=user.email,
        web_view_link=link,
        is_active=payload.is_active,
        sort_order=payload.sort_order,
    )
    return row.to_dict()


@router.put("/admin/{row_id}")
def update_department_folder(
    row_id: str,
    payload: dict,  # raw dict so callers can omit nullable fields
    user: UserProfile = Depends(admin_required),
) -> dict:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")

    folder_id = payload.get("folder_id")
    if folder_id is not None:
        folder_id = _normalize_folder_id(folder_id)

    row = store.update_folder(
        row_id,
        actor_email=user.email,
        folder_id=folder_id,
        folder_name=payload.get("folder_name"),
        web_view_link=payload.get("web_view_link", "__unchanged__"),
        department=payload.get("department", "__unchanged__"),
        is_active=payload.get("is_active"),
        sort_order=payload.get("sort_order"),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="folder not found")
    return row.to_dict()


@router.delete("/admin/{row_id}", status_code=204)
def delete_department_folder(
    row_id: str,
    _: UserProfile = Depends(admin_required),
) -> None:
    if not store.delete_folder(row_id):
        raise HTTPException(status_code=404, detail="folder not found")

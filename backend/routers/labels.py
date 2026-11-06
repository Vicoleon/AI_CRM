"""Admin-managed Google Drive label → importance-priority catalog.

Backs the Drive-labels AI-Priorities signal: an admin maps a Drive label value
(e.g. "Important", "Cubie", "Project") to a 1.0–2.0 priority weight. The signal
source (``backend/signals/drive.py``) reads this map to decide which recently-
updated Drive files surface as high-importance priorities.

All endpoints are admin-only — this is workspace configuration, and the signal
source reads the catalog from the DB directly, not through this API.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.auth import get_current_user
from backend.models.auth import User
from rubik_core.database import schemas
from rubik_core.database.repository import SQLMetadataRegistry

router = APIRouter(prefix="/labels", tags=["labels"])

registry = SQLMetadataRegistry()

PRIORITY_MIN = 1.0
PRIORITY_MAX = 2.0


def _clamp(priority: float) -> float:
    return max(PRIORITY_MIN, min(PRIORITY_MAX, float(priority)))


def _require_admin(user: User) -> None:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Permission denied: admin only")


class LabelPriorityRequest(BaseModel):
    label_value: str = Field(..., min_length=1)
    label_key: str | None = None
    priority: float = 1.0


class LabelPriorityUpdate(BaseModel):
    priority: float = Field(..., ge=PRIORITY_MIN, le=PRIORITY_MAX)


@router.get("/", response_model=list[schemas.DriveLabelPriority])
async def list_labels(user: User = Depends(get_current_user)):
    """List all Drive label → priority mappings."""
    _require_admin(user)
    return registry.list_drive_label_priorities()


@router.post("/", response_model=schemas.DriveLabelPriority)
async def create_label(
    request: LabelPriorityRequest, user: User = Depends(get_current_user)
):
    """Create (or update, if it already exists) a label → priority mapping."""
    _require_admin(user)
    return registry.upsert_drive_label_priority(
        label_value=request.label_value.strip(),
        label_key=(request.label_key or None),
        priority=_clamp(request.priority),
    )


@router.patch("/{label_id}", response_model=schemas.DriveLabelPriority)
async def update_label(
    label_id: str,
    request: LabelPriorityUpdate,
    user: User = Depends(get_current_user),
):
    """Update an existing mapping's priority weight."""
    _require_admin(user)
    updated = registry.update_drive_label_priority(
        label_id, priority=_clamp(request.priority)
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Label mapping not found")
    return updated


@router.delete("/{label_id}")
async def remove_label(label_id: str, user: User = Depends(get_current_user)):
    """Delete a label → priority mapping."""
    _require_admin(user)
    if registry.remove_drive_label_priority(label_id):
        return {"message": f"Label mapping '{label_id}' removed"}
    raise HTTPException(status_code=404, detail="Label mapping not found")

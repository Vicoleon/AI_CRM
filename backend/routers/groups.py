from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.auth import get_current_user
from backend.models.auth import User
from rubik_core.auth.registry import UserRegistry
from rubik_core.database.schemas import Group

router = APIRouter(prefix="/groups", tags=["groups"])
user_registry = UserRegistry()


class GroupCreate(BaseModel):
    name: str
    description: str | None = None


class MemberRequest(BaseModel):
    email: str


@router.get("/", response_model=list[Group])
async def list_groups(user: User = Depends(get_current_user)):
    """List all available groups (Admin only)."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user_registry.get_groups()


@router.post("/", response_model=Group)
async def create_group(request: GroupCreate, user: User = Depends(get_current_user)):
    """Create a new group (Admin only)."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user_registry.add_group(request.name, request.description)


@router.delete("/{group_id}")
async def delete_group(group_id: str, user: User = Depends(get_current_user)):
    """Delete a group (Admin only)."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    success = user_registry.delete_group(group_id)
    if not success:
        raise HTTPException(status_code=404, detail="Group not found")
    return {"status": "success"}


@router.post("/{group_id}/members")
async def add_member(
    group_id: str, request: MemberRequest, user: User = Depends(get_current_user)
):
    """Add a user to a group (Admin only)."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    try:
        success = user_registry.add_user_to_group(request.email, group_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not success:
        raise HTTPException(
            status_code=400, detail="Failed to add member (user or group may not exist)"
        )
    return {"status": "success"}


@router.delete("/{group_id}/members/{email}")
async def remove_member(
    group_id: str, email: str, user: User = Depends(get_current_user)
):
    """Remove a user from a group (Admin only)."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    success = user_registry.remove_user_from_group(email, group_id)
    if not success:
        raise HTTPException(status_code=400, detail="Failed to remove member")
    return {"status": "success"}

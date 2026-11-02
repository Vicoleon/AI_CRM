from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from backend.auth import get_current_user
from backend.models.auth import User
from rubik_core.auth.registry import UserRegistry
from rubik_core.database.schemas import AccessPolicy

router = APIRouter(prefix="/policies", tags=["policies"])
user_registry = UserRegistry()

from pydantic import BaseModel


class PolicyRequest(BaseModel):
    user_email: str | None = None
    group_id: str | None = None
    department: str | None = None
    topic: str | None = None
    can_read: bool = True
    can_update: bool = False


@router.get("/", response_model=list[AccessPolicy])
async def list_policies(
    user_email: str | None = None,
    group_id: str | None = None,
    user: User = Depends(get_current_user),
):
    """List access policies with filtering (Admin only)."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user_registry.get_access_policies(user_email, group_id)


@router.post("/", response_model=AccessPolicy)
async def create_policy(policy: PolicyRequest, user: User = Depends(get_current_user)):
    """Create a new access policy (Admin only)."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user_registry.add_access_policy(
        user_email=policy.user_email,
        group_id=policy.group_id,
        department=policy.department,
        topic=policy.topic,
        can_read=policy.can_read,
        can_update=policy.can_update,
    )


@router.delete("/{policy_id}")
async def delete_policy(policy_id: int, user: User = Depends(get_current_user)):
    """Delete an access policy (Admin only)."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    success = user_registry.delete_access_policy(policy_id)
    if not success:
        raise HTTPException(status_code=404, detail="Policy not found")
    return {"status": "success"}

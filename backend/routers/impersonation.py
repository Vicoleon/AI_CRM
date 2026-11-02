"""Admin → the impersonation menu's people.

    GET /api/admin/impersonation/targets

The people an allowlisted admin may impersonate (read-only, without email,
calendar, Drive, transcripts or chat — see ``rubik_core/auth/impersonation.py``).
Resolved against the caller's REAL identity (``get_current_user_unmasked``) so
the menu still lists them while an impersonation is running, which is how the
admin switches back.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from backend.auth import (
    _impersonation_policy,
    _may_impersonate,
    get_current_user_unmasked,
    user_registry,
)
from rubik_core.config import settings
from rubik_core.database.schemas import UserProfile

router = APIRouter(prefix="/admin/impersonation", tags=["admin-impersonation"])


class ImpersonationTarget(BaseModel):
    email: str
    name: str


class ImpersonationTargetsResponse(BaseModel):
    targets: list[ImpersonationTarget]


@router.get("/targets", response_model=ImpersonationTargetsResponse)
async def targets(user: UserProfile = Depends(get_current_user_unmasked)):
    """Empty (never an error) for anyone who may not impersonate a person."""
    policy = await run_in_threadpool(_impersonation_policy)
    if (
        not settings.USER_IMPERSONATION_ENABLED
        or "admin" not in (user.roles or [])
        or policy is None
        or not _may_impersonate(user.email or "", policy)
    ):
        return ImpersonationTargetsResponse(targets=[])
    out: list[ImpersonationTarget] = []
    for email in sorted(policy.user_impersonation_targets):
        profile = user_registry.get_user(email)
        if profile is None:
            continue  # no Cubie account: impersonating them would 403
        out.append(ImpersonationTarget(email=email, name=profile.name or email))
    return ImpersonationTargetsResponse(targets=out)

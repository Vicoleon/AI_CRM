"""Enter and exit short, audited RubikSoft platform support sessions."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from backend.auth import get_current_user
from rubik_core.auth.session_tokens import mint_session_token
from rubik_core.control.repository import (
    create_support_session,
    is_platform_admin,
    revoke_support_session,
    support_enter_target,
)
from rubik_core.database.db import SessionLocal
from rubik_core.database.models import User
from rubik_core.database.schemas import UserProfile
from rubik_core.tenancy.context import _canonical_slug, current_workspace, use_workspace
from rubik_core.tenancy.support_context import current_support_session

router = APIRouter(prefix="/platform/support", tags=["platform support"])


def _require_platform_support_policy_actor(user: UserProfile) -> None:
    grant = current_support_session()
    if (
        grant is None
        or grant.actor != user.email
        or grant.workspace_slug != current_workspace()
    ):
        raise HTTPException(status_code=403, detail="Platform support session required")
    if not is_platform_admin(user.email):
        raise HTTPException(status_code=403, detail="Platform authority required")


class EnterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace: str


def _ensure_support_user(slug: str, actor: str) -> None:
    """Keep a non-admin FK target without borrowing a client's user roles."""
    with use_workspace(slug):
        with SessionLocal() as db:
            row = db.get(User, actor)
            if row is not None:
                if (
                    not row.is_platform_support
                    or bool(row.is_exec)
                    or bool(row.roles)
                    or bool(row.groups)
                    or bool(row.workspace_groups)
                    or bool(row.managed_departments)
                    or row.department != "general"
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="This support identity already belongs to a regular user",
                    )
                return
            db.add(
                User(
                    email=actor,
                    id=f"platform-support:{actor}",
                    name="RubikSoft platform support",
                    department="general",
                    is_exec=False,
                    is_platform_support=True,
                )
            )
            try:
                db.commit()
            except IntegrityError:
                # A concurrent Enter may have inserted the same actor. Accept
                # only the same safe flag and role-free state after rollback.
                db.rollback()
                row = db.get(User, actor)
                if (
                    row is None
                    or not row.is_platform_support
                    or bool(row.is_exec)
                    or bool(row.roles)
                    or bool(row.groups)
                    or bool(row.workspace_groups)
                    or bool(row.managed_departments)
                    or row.department != "general"
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="This support identity already belongs to a regular user",
                    )


@router.post("/enter")
async def enter_support(
    body: EnterRequest,
    request: Request,
    user: UserProfile = Depends(get_current_user),
):
    claims = getattr(request.state, "session_claims", None)
    if (
        claims is None
        or claims.typ != "user"
        or claims.sub != user.email
        or claims.ws != current_workspace()
    ):
        raise HTTPException(status_code=403, detail="Normal home session required")
    try:
        target_slug = _canonical_slug(body.workspace)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid workspace")
    try:
        target = await run_in_threadpool(
            support_enter_target, user.email, claims.ws, target_slug
        )
    except PermissionError:
        raise HTTPException(status_code=403, detail="Platform support access required")
    except ValueError:
        raise HTTPException(status_code=404, detail="Workspace unavailable")
    await run_in_threadpool(_ensure_support_user, target.slug, user.email)
    try:
        grant = await run_in_threadpool(
            create_support_session, user.email, claims.ws, target_slug
        )
    except PermissionError:
        raise HTTPException(status_code=403, detail="Platform support access changed")
    except ValueError:
        raise HTTPException(status_code=404, detail="Workspace unavailable")
    try:
        token, expires_at, _ = mint_session_token(
            user.email,
            target_slug,
            typ="support",
            sup=user.email,
            sid=grant.sid,
            hours=0.25,
        )
    except Exception:
        await run_in_threadpool(
            revoke_support_session, grant.sid, user.email, target_slug
        )
        raise
    return {
        "token": token,
        "expires_at": expires_at.isoformat(),
        "workspace": {"slug": target.slug, "name": target.name},
    }


@router.post("/exit")
async def exit_support(user: UserProfile = Depends(get_current_user)):
    grant = current_support_session()
    if grant is None or grant.actor != user.email:
        raise HTTPException(status_code=403, detail="Platform support session required")
    revoked = await run_in_threadpool(
        revoke_support_session, grant.sid, grant.actor, grant.workspace_slug
    )
    if not revoked:
        raise HTTPException(
            status_code=401, detail="Support session is no longer active"
        )
    return {"revoked": True}


@router.get("/impersonation-policy")
async def get_impersonation_policy(
    user: UserProfile = Depends(get_current_user),
) -> dict[str, list[str]]:
    """Read a target workspace's platform-managed person-view grants."""
    from rubik_core.company.operations import (
        OperationsConfigUnavailable,
        get_operations_config,
    )

    await run_in_threadpool(_require_platform_support_policy_actor, user)
    try:
        policy = await run_in_threadpool(get_operations_config)
    except OperationsConfigUnavailable as exc:
        raise HTTPException(
            status_code=503, detail="Workspace policy unavailable"
        ) from exc
    return {
        "impersonation_allowed_emails": list(policy.impersonation_allowed_emails),
        "user_impersonation_targets": list(policy.user_impersonation_targets),
    }


@router.patch("/impersonation-policy")
async def update_impersonation_policy(
    changes: dict[str, list[str]],
    user: UserProfile = Depends(get_current_user),
) -> dict[str, list[str]]:
    """A live platform support grant may edit only person-view grants."""
    from rubik_core.company.operations import (
        OperationsConfigInvalid,
        OperationsConfigUnavailable,
        patch_operations_config,
    )

    await run_in_threadpool(_require_platform_support_policy_actor, user)
    if set(changes) - {"impersonation_allowed_emails", "user_impersonation_targets"}:
        raise HTTPException(
            status_code=400, detail="Unknown impersonation policy field"
        )
    grant = current_support_session()
    if grant is None:
        raise HTTPException(status_code=403, detail="Platform support session required")
    try:
        policy = await run_in_threadpool(
            patch_operations_config,
            changes,
            platform_authorized=True,
            audit_actor=user.email,
            audit_sid=grant.sid,
        )
    except OperationsConfigInvalid as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OperationsConfigUnavailable as exc:
        raise HTTPException(
            status_code=503, detail="Workspace policy unavailable"
        ) from exc
    return {
        "impersonation_allowed_emails": list(policy.impersonation_allowed_emails),
        "user_impersonation_targets": list(policy.user_impersonation_targets),
    }

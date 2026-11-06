"""Exchange a verified Google identity for a workspace-bound Cubie session."""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, Security
from fastapi.concurrency import run_in_threadpool
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel

from backend.auth import (
    _apply_access_gates,
    _client_ip,
    _user_agent,
    security,
    user_registry,
)
from rubik_core.auth.google_id_token import (
    GoogleIdentity,
    GoogleIdentityError,
    verify_google_access_token,
    verify_google_id_token,
)
from rubik_core.auth.session_tokens import (
    SessionExpiredError,
    decode_session_token,
    mint_session_token,
)
from rubik_core.brand import PRODUCT_NAME
from rubik_core.control.repository import workspace_for_domain_any
from rubik_core.tenancy.context import use_workspace

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)


class SessionResponse(BaseModel):
    token: str
    expires_at: datetime
    lifetime_hours: float


def _upsert_verified_user(request: Request, identity: GoogleIdentity):
    """Sync the verified person only after their workspace has been selected."""
    email = identity.email
    user = user_registry.get_user(email)
    if not user:
        user = user_registry.register_user(
            email=email,
            name=identity.name,
            id=identity.sub,
            picture=identity.picture,
            roles=["user"],  # provisioning, not first-login order, seeds admins
        )
    else:
        updates = {}
        if identity.name:
            updates["name"] = identity.name
        if identity.picture:
            updates["picture"] = identity.picture
        if updates:
            user = user_registry.update_user(email, updates) or user
        try:
            user = user_registry.sync_user_workspace_roles(email) or user
        except Exception:
            # Existing accounts can sign in if Workspace role sync is down.
            logger.warning(
                "Workspace role sync failed during sign-in for %s", email, exc_info=True
            )
    user = _apply_access_gates(user, email)
    try:
        user_registry.record_login(email, _client_ip(request), _user_agent(request))
    except Exception:
        # A failed login-audit write should not strand a verified employee.
        logger.warning("Login audit failed during sign-in for %s", email, exc_info=True)
    return user


@router.post("/session", response_model=SessionResponse)
async def create_session(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(security),
) -> SessionResponse:
    raw = credentials.credentials if credentials else ""
    if not raw:
        raise HTTPException(status_code=401, detail="Google sign-in is required")
    try:
        if decode_session_token(raw):
            raise HTTPException(
                status_code=400,
                detail="Session tokens cannot be renewed; sign in with Google.",
            )
    except SessionExpiredError:
        raise HTTPException(
            status_code=401, detail="Session expired. Please sign in again."
        )
    try:
        # Meet's embedded panel may opt into the one-time access-token path.
        # Every other caller supplies a GIS ID token verified by Google keys.
        if request.headers.get("X-Cubie-Meet-Access-Token") == "true":
            identity = await run_in_threadpool(verify_google_access_token, raw)
        else:
            identity = await run_in_threadpool(verify_google_id_token, raw)
    except GoogleIdentityError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc

    workspace = await run_in_threadpool(workspace_for_domain_any, identity.hd)
    if workspace is None:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "workspace_unknown",
                "message": f"Your company is not set up on {PRODUCT_NAME}",
            },
        )
    if workspace.status != "active":
        raise HTTPException(
            status_code=403,
            detail={
                "code": "workspace_suspended",
                "message": "Workspace is unavailable",
            },
        )
    with use_workspace(workspace.slug):
        user = await run_in_threadpool(_upsert_verified_user, request, identity)
        token, expires_at, hours = await run_in_threadpool(
            mint_session_token, user.email, workspace.slug, hd=identity.hd
        )
    return SessionResponse(token=token, expires_at=expires_at, lifetime_hours=hours)

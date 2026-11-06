"""Opt-in additional Gmail accounts for a user in one workspace."""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from backend.auth import get_current_user
from rubik_core.auth.registry import UserProfile
from rubik_core.config import settings
from rubik_core.tenancy.gmail_accounts import (
    GmailOAuthRejected,
    GmailOAuthUnavailable,
    _ai_processing_route,
    _consume_state,
    acknowledge_ai_provider,
    begin_connection,
    complete_connection,
    disconnect_account,
    gmail_oauth_available,
    list_accounts,
    multi_gmail_enabled,
)
from rubik_core.tenancy.platform_secrets import is_production_runtime
from rubik_core.tenancy.support_context import current_support_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/email/accounts", tags=["Email accounts"])
# Google's redirect has no Cubie bearer token; signed one-use state supplies
# the workspace/user identity, so this callback mounts outside the core gate.
callback_router = APIRouter(prefix="/email/accounts", tags=["Email accounts"])
_BROWSER_COOKIE = "cubie_gmail_oauth_browser"
_COOKIE_PATH = "/api/email/accounts"


class ProviderConsentRequest(BaseModel):
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


def _real_user(request: Request, user: UserProfile) -> None:
    if (
        user.is_platform_support
        or current_support_session() is not None
        or getattr(request.state, "impersonation", None)
    ):
        raise HTTPException(
            status_code=403, detail="Gmail connections require your own session"
        )


@router.get("")
def accounts(
    request: Request,
    user: UserProfile = Depends(get_current_user),
) -> dict:
    _real_user(request, user)
    provider, model, fingerprint = _ai_processing_route()
    return {
        "enabled": multi_gmail_enabled(),
        "oauth_available": gmail_oauth_available(),
        "default_account": user.email,
        "accounts": list_accounts(user.email),
        "ai_provider": provider,
        "ai_model": model,
        "ai_provider_fingerprint": fingerprint,
    }


@router.post("/connect")
def connect(
    request: Request,
    response: Response,
    payload: ProviderConsentRequest,
    user: UserProfile = Depends(get_current_user),
) -> dict[str, str]:
    _real_user(request, user)
    try:
        browser_nonce = secrets.token_urlsafe(32)
        authorize_url = begin_connection(
            user.email,
            browser_nonce,
            payload.fingerprint,
        )
        response.set_cookie(
            _BROWSER_COOKIE,
            browser_nonce,
            max_age=600,
            path=_COOKIE_PATH,
            secure=is_production_runtime(),
            httponly=True,
            samesite="lax",
        )
        return {"authorization_url": authorize_url}
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except GmailOAuthUnavailable as exc:
        raise HTTPException(
            status_code=503, detail="Gmail OAuth is unavailable"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.delete("/{account_id}")
def disconnect(
    account_id: str,
    request: Request,
    user: UserProfile = Depends(get_current_user),
) -> dict[str, bool]:
    _real_user(request, user)
    try:
        deleted = disconnect_account(user.email, account_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid Gmail account ID") from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Gmail account not found")
    return {"disconnected": True}


@router.post("/{account_id}/ai-provider-consent")
def consent_to_ai_provider(
    account_id: str,
    payload: ProviderConsentRequest,
    request: Request,
    user: UserProfile = Depends(get_current_user),
) -> dict[str, str | bool]:
    _real_user(request, user)
    try:
        return acknowledge_ai_provider(user.email, account_id, payload.fingerprint)
    except PermissionError:
        raise HTTPException(
            status_code=404, detail="Gmail account is unavailable"
        ) from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


def _settings_redirect(status: str) -> RedirectResponse:
    # An operator-owned origin, never a browser-supplied redirect target.
    origin = settings.FRONTEND_URL.rstrip("/")
    response = RedirectResponse(
        f"{origin}/settings?tab=email&gmail_connection={status}", status_code=302
    )
    response.delete_cookie(_BROWSER_COOKIE, path=_COOKIE_PATH)
    return response


@callback_router.get("/callback")
def callback(
    request: Request,
    state: str = Query(..., max_length=2048),
    code: str | None = Query(None, max_length=4096),
    error: str | None = Query(None, max_length=100),
):
    """Consume the state exactly once before any token exchange or redirect."""
    if error:
        try:
            _consume_state(state, request.cookies.get(_BROWSER_COOKIE))
        except (PermissionError, GmailOAuthUnavailable, RuntimeError) as exc:
            raise HTTPException(
                status_code=401, detail="Invalid Gmail authorization state"
            ) from exc
        return _settings_redirect("denied")
    if not code:
        raise HTTPException(status_code=400, detail="Missing Gmail authorization code")
    try:
        complete_connection(state, code, request.cookies.get(_BROWSER_COOKIE))
    except PermissionError as exc:
        raise HTTPException(
            status_code=401, detail="Invalid Gmail authorization state"
        ) from exc
    except GmailOAuthUnavailable as exc:
        raise HTTPException(
            status_code=503, detail="Gmail OAuth is unavailable"
        ) from exc
    except GmailOAuthRejected:
        return _settings_redirect("failed")
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="Invalid Gmail authorization code"
        ) from exc
    except Exception as exc:
        logger.warning("Gmail OAuth callback failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=502, detail="Gmail authorization failed"
        ) from exc
    return _settings_redirect("connected")

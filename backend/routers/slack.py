"""UC-15: Slack OAuth + connection status.

Endpoints:
  GET  /api/slack/status         → is_connected, team_name (no token leak)
  GET  /api/slack/oauth-start    → returns the Slack authorize URL the
                                    frontend redirects the user to
  GET  /api/slack/callback       → Slack redirects here with ?code=&state=
                                    we exchange, persist, then bounce to
                                    /settings?tab=slack&slack_connected=1
  POST /api/slack/disconnect     → delete the user's SlackCredential row

The OAuth `state` parameter binds the originating user, verified Google hosted
domain and selected workspace for ten minutes with a platform HMAC key.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.auth import get_current_user
from backend.services.slack_service import (
    SlackAPIError,
    SlackBotClient,
    SlackNotConnectedError,
    SlackService,
    credential_domain_active,
    exchange_code_for_token,
    workspace_has_slack_team,
)
from rubik_core.auth.registry import UserProfile
from rubik_core.brand import PRODUCT_NAME
from rubik_core.config import AppMode, settings
from rubik_core.control.db import control_session
from rubik_core.control.models import SlackInstallation, Workspace, WorkspaceConnector
from rubik_core.control.repository import (
    domain_belongs_to_workspace,
    get_workspace,
    module_enabled,
)
from rubik_core.database.db import SessionLocal, get_db
from rubik_core.database.models import AuditLog, SlackCredential, User
from rubik_core.tenancy.context import current_workspace, use_workspace
from rubik_core.tenancy.platform_secrets import _strong_signing_key, platform_secret
from rubik_core.tenancy.secrets import (
    upsert_workspace_secret,
    workspace_secret,
    ws_secret_id,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/slack", tags=["slack"])
# The OAuth callback (same ``/slack`` prefix) is mounted UNGATED in main.py.
# Slack redirects the browser to it from slack.com, so no Authorization header
# rides along, and the pilot gate on ``core`` resolves a user and would 401 it
# before the handler runs — every "Connect Slack" ended on a bare
# ``{"detail":"Missing authentication credentials"}`` page. Identity comes from
# the HMAC-signed ``state`` instead, which only the gated ``/oauth-start`` mints.
callback_router = APIRouter(prefix="/slack", tags=["slack"])


# Scopes requested in the OAuth flow. Keep in sync with the Slack app manifest —
# new scopes are NOT granted to an existing install until the app is re-installed
# / the user re-authorizes.
USER_SCOPES = [
    "channels:history",
    "channels:read",
    "groups:history",
    "groups:read",
    "im:history",
    "im:read",
    "mpim:history",
    "mpim:read",
    "search:read",
    "users:read",
]
BOT_SCOPES = [
    "channels:history",
    "channels:read",
    "commands",  # slash commands (/todo, /ids) → backend/routers/slack_commands.py
    "groups:read",
    "im:read",
    "im:write",  # Open a DM channel with a rep (conversations.open)
    "mpim:read",
    "users:read",
    "users:read.email",  # Map a rep's company email → Slack user ID
    "chat:write",  # Post the AI-Priorities DM as the Cubie bot
]


_STATE_TTL_SECONDS = 600
_STATE_MAX_LENGTH = 2048


def _state_secret() -> str:
    """OAuth state must not use the local signing default or a weak override."""
    raw = os.environ.get("CUBIE_OAUTH_STATE_SECRET", "")
    if not _strong_signing_key(raw):
        raise RuntimeError("CUBIE_OAUTH_STATE_SECRET needs an explicit strong key")
    return platform_secret("CUBIE_OAUTH_STATE_SECRET")


def _oauth_config() -> tuple[dict, bool]:
    """Current workspace's public OAuth config and client-secret binding."""
    slug = current_workspace()
    with control_session() as db:
        row = db.get(WorkspaceConnector, (slug, "slack"))
        if row is None:
            return {}, False
        refs = row.secret_refs or {}
        return dict(row.config or {}), bool(
            refs.get("client_secret") == ws_secret_id("slack", "client_secret")
            and refs.get("signing_secret") == ws_secret_id("slack", "signing_secret")
        )


def _oauth_configured() -> bool:
    try:
        _state_secret()
    except RuntimeError:
        return False
    config, has_secret_ref = _oauth_config()
    return bool(
        config.get("client_id")
        and _valid_redirect_uri(config.get("redirect_uri"))
        and has_secret_ref
    )


def _sign_state(user_email: str, hosted_domain: str) -> str:
    """Bind the authenticated user, verified hosted domain, and workspace."""
    slug = current_workspace()
    email = user_email.strip().lower()
    hd = hosted_domain.strip().lower()
    if (
        len(email) > 320
        or email.count("@") != 1
        or not domain_belongs_to_workspace(slug, hd)
    ):
        raise ValueError("The user no longer belongs to this workspace")
    payload = {
        "ws": slug,
        "email": email,
        "hd": hd,
        "nonce": secrets.token_urlsafe(16),
        "exp": int(time.time()) + _STATE_TTL_SECONDS,
    }
    encoded = (
        base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        .decode("ascii")
        .rstrip("=")
    )
    signed = f"v1.{encoded}"
    sig = hmac.new(
        _state_secret().encode(), signed.encode(), hashlib.sha256
    ).hexdigest()
    return f"{signed}.{sig}"


def _parse_state(state: str) -> tuple[str, str, str] | None:
    """Verify state before opening control or tenant data."""
    key = _state_secret()
    if not isinstance(state, str) or len(state) > _STATE_MAX_LENGTH:
        return None
    try:
        version, encoded, sig = state.split(".")
        if version != "v1" or len(sig) != 64:
            return None
        signed = f"{version}.{encoded}"
        expected = hmac.new(key.encode(), signed.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return None
        payload = json.loads(
            base64.b64decode(
                encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
            )
        )
    except (ValueError, UnicodeError, binascii.Error):
        return None
    if not isinstance(payload, dict) or set(payload) != {
        "ws",
        "email",
        "hd",
        "nonce",
        "exp",
    }:
        return None
    slug, email, hd, nonce, exp = (
        payload["ws"],
        payload["email"],
        payload["hd"],
        payload["nonce"],
        payload["exp"],
    )
    if (
        not isinstance(slug, str)
        or not isinstance(email, str)
        or not isinstance(hd, str)
        or not isinstance(nonce, str)
        or not isinstance(exp, int)
        or isinstance(exp, bool)
        or len(nonce) < 12
        or len(nonce) > 64
        or email != email.strip().lower()
        or hd != hd.strip().lower()
        or not hd
        or len(hd) > 253
        or email.count("@") != 1
        or len(email) > 320
        or exp < time.time()
        or exp > time.time() + _STATE_TTL_SECONDS
    ):
        return None
    try:
        from rubik_core.tenancy.context import _canonical_slug

        _canonical_slug(slug)
    except ValueError:
        return None
    return slug, email, hd


def _log(db: Session, actor: str, action: str, details: dict) -> None:
    try:
        db.add(
            AuditLog(
                actor_email=actor, action=action, status="success", details=details
            )
        )
        db.commit()
    except Exception as exc:
        logger.warning("Slack audit log failed: %s", type(exc).__name__)
        db.rollback()


def _valid_redirect_uri(raw: object) -> str | None:
    if not isinstance(raw, str) or len(raw) > 2048:
        return None
    try:
        parsed = urlparse(raw)
        hostname = parsed.hostname
    except ValueError:
        return None
    if (
        not hostname
        or parsed.username
        or parsed.password
        or parsed.path != "/api/slack/callback"
        or parsed.query
        or parsed.fragment
    ):
        return None
    if parsed.scheme == "https":
        return raw
    if (
        parsed.scheme == "http"
        and settings.APP_MODE != AppMode.GCP
        and hostname in {"localhost", "127.0.0.1"}
    ):
        return raw
    return None


def _configured_oauth_client() -> tuple[str, str]:
    config, has_secret_ref = _oauth_config()
    client_id = config.get("client_id")
    redirect_uri = _valid_redirect_uri(config.get("redirect_uri"))
    if not (
        has_secret_ref
        and isinstance(client_id, str)
        and client_id.strip()
        and len(client_id) <= 100
        and redirect_uri
    ):
        raise HTTPException(
            status_code=503, detail="Slack OAuth is not configured for this workspace"
        )
    return client_id.strip(), redirect_uri


def _slack_id(value: object, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{2,100}", value):
        raise HTTPException(status_code=502, detail=f"Slack returned no valid {name}")
    return value


def _token(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise HTTPException(
            status_code=502, detail="Slack returned an incomplete token set"
        )
    return value


def _validated_exchange(data: dict) -> tuple[str, str, str, str, str, str, str, str]:
    """Extract IDs and tokens without ever serializing a Slack response."""
    if (
        not isinstance(data, dict)
        or data.get("ok") is not True
        or data.get("is_enterprise_install") is True
    ):
        raise HTTPException(status_code=502, detail="Unsupported Slack installation")
    team = data.get("team")
    authed_user = data.get("authed_user")
    if not isinstance(team, dict) or not isinstance(authed_user, dict):
        raise HTTPException(
            status_code=502, detail="Slack returned an incomplete installation"
        )
    app_id = _slack_id(data.get("app_id"), "app ID")
    team_id = _slack_id(team.get("id"), "team ID")
    user_id = _slack_id(authed_user.get("id"), "user ID")
    user_token = _token(authed_user.get("access_token"))
    bot_token = _token(data.get("access_token"))
    team_name = str(team.get("name") or "")[:255]
    user_scope = str(authed_user.get("scope") or "")[:2048]
    bot_scope = str(data.get("scope") or "")[:2048]
    return (
        app_id,
        team_id,
        user_id,
        user_token,
        bot_token,
        team_name,
        user_scope,
        bot_scope,
    )


def _claim_installation(app_id: str, team_id: str) -> None:
    """Atomically pin the app/team and claim its signed inbound route."""
    slug = current_workspace()
    signing_ref = ws_secret_id("slack", "signing_secret")
    if not workspace_secret("slack", "signing_secret"):
        raise HTTPException(
            status_code=503, detail="Slack signing secret is unavailable"
        )
    with control_session() as db:
        if db.get_bind().dialect.name == "sqlite":
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        workspace = db.scalar(
            select(Workspace).where(Workspace.slug == slug).with_for_update()
        )
        row = db.get(WorkspaceConnector, (slug, "slack"))
        if workspace is None or workspace.status != "active" or row is None:
            raise HTTPException(
                status_code=503, detail="Slack workspace is unavailable"
            )
        config = dict(row.config or {})
        reserved = (config.get("installed_app_id"), config.get("installed_team_id"))
        if any(reserved) and reserved != (app_id, team_id):
            raise HTTPException(
                status_code=409, detail="Another Slack team is already connected"
            )
        collision = db.scalar(
            select(SlackInstallation).where(
                (SlackInstallation.app_id == app_id)
                | (SlackInstallation.team_id == team_id),
                (SlackInstallation.app_id != app_id)
                | (SlackInstallation.team_id != team_id)
                | (SlackInstallation.workspace_slug != slug),
            )
        )
        if collision is not None:
            raise HTTPException(
                status_code=409, detail="Slack app or team belongs to another workspace"
            )
        existing = db.get(SlackInstallation, (app_id, team_id))
        other = db.scalar(
            select(SlackInstallation).where(
                SlackInstallation.workspace_slug == slug,
                (SlackInstallation.app_id != app_id)
                | (SlackInstallation.team_id != team_id),
            )
        )
        if other is not None:
            raise HTTPException(
                status_code=409, detail="Another Slack team is already connected"
            )
        config["installed_app_id"] = app_id
        config["installed_team_id"] = team_id
        row.config = config
        if existing is None:
            db.add(
                SlackInstallation(
                    app_id=app_id,
                    team_id=team_id,
                    workspace_slug=slug,
                    signing_secret_ref=signing_ref,
                )
            )
        elif existing.signing_secret_ref != signing_ref:
            raise HTTPException(status_code=409, detail="Slack installation changed")
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(
                status_code=409, detail="Slack installation changed"
            ) from exc


# ─── Response models ──────────────────────────────────────────────────────


class SlackStatus(BaseModel):
    is_connected: bool
    is_configured: bool
    team_name: str | None = None
    slack_user_id: str | None = None
    connected_at: datetime | None = None


class OAuthStartResponse(BaseModel):
    authorize_url: str


class SlackActivityChannel(BaseModel):
    id: str
    name: str | None = None
    is_private: bool = False
    unread_count: int
    app_link: str | None = None


class SlackActivityMention(BaseModel):
    channel_id: str
    channel_name: str | None = None
    user_name: str | None = None
    text: str
    ts: str
    permalink: str | None = None
    app_link: str | None = None


class SlackActivityDM(BaseModel):
    channel_id: str
    counterparty_name: str
    unread_count: int
    is_group: bool = False
    app_link: str | None = None
    ts: str = ""  # latest-message ts (feed ordering)
    preview: str = ""  # latest-message text preview


class SlackActivity(BaseModel):
    """Read-only Slack inbox snapshot for the homepage strip + Productivity tab."""

    is_connected: bool
    is_configured: bool
    channel_count: int = 0  # channels with unread messages
    unread_message_count: int = 0  # total unread across those channels
    mention_count: int = 0
    dm_count: int = 0  # DM threads with unread messages
    channels: list[SlackActivityChannel] = []
    mentions: list[SlackActivityMention] = []
    dms: list[SlackActivityDM] = []
    # Set on a soft failure (e.g. token revoked / scope missing) so the UI can
    # nudge a reconnect without the dashboard load 500'ing.
    error: str | None = None


class SlackChannelSummary(BaseModel):
    """On-demand AI summary of ONE channel's unread messages — backs the hover
    popover on the Productivity → Slack tab. Generated lazily, per channel, only
    when the user hovers a chip (not an always-on digest of everything)."""

    summary_md: str = ""
    unread_count: int = 0
    error: str | None = None


# ─── Endpoints ────────────────────────────────────────────────────────────


@router.get("/status", response_model=SlackStatus)
def status(
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    cred = (
        db.query(SlackCredential)
        .filter(SlackCredential.user_email == user.email)
        .one_or_none()
    )
    connected = False
    if credential_domain_active(cred):
        try:
            connected = workspace_has_slack_team(cred.slack_team_id)
        except Exception:  # noqa: BLE001 - control outage cannot show connected
            connected = False
    return SlackStatus(
        is_connected=connected,
        is_configured=_oauth_configured(),
        team_name=cred.slack_team_name if connected else None,
        slack_user_id=cred.slack_user_id if connected else None,
        connected_at=cred.connected_at if connected else None,
    )


@router.get("/oauth-start", response_model=OAuthStartResponse)
def oauth_start(
    request: Request,
    user: UserProfile = Depends(get_current_user),
):
    config, _ = _oauth_config()
    if not config.get("installed_team_id") and not user.is_admin:
        raise HTTPException(
            status_code=403, detail="A workspace admin must connect Slack first"
        )
    client_id, redirect_uri = _configured_oauth_client()
    claims = getattr(request.state, "session_claims", None)
    if claims is not None:
        if (
            claims.typ != "user"
            or claims.ws != current_workspace()
            or claims.sub != user.email.strip().lower()
            or not claims.hd
        ):
            raise HTTPException(status_code=401, detail="Slack session is unavailable")
        hosted_domain = claims.hd
    elif settings.APP_MODE != AppMode.GCP and settings.ENABLE_MOCK_AUTH:
        # Local mock auth already checked this email's domain before resolving
        # the tenant user. Real OAuth always carries Google's verified hd.
        hosted_domain = user.email.rsplit("@", 1)[-1]
    else:
        raise HTTPException(status_code=401, detail="Slack session is unavailable")
    try:
        state = _sign_state(user.email, hosted_domain)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503, detail="Slack OAuth is unavailable"
        ) from exc
    params = {
        "client_id": client_id,
        "scope": ",".join(BOT_SCOPES),
        "user_scope": ",".join(USER_SCOPES),
        "redirect_uri": redirect_uri,
        "state": state,
    }
    return OAuthStartResponse(
        authorize_url=f"https://slack.com/oauth/v2/authorize?{urlencode(params)}"
    )


@callback_router.get("/callback")
def callback(
    state: str = Query(..., max_length=_STATE_MAX_LENGTH),
    code: str | None = Query(None, max_length=2048),
    error: str | None = Query(None, max_length=100),
):
    """Verify platform state, then select a workspace before tenant I/O."""
    try:
        identity = _parse_state(state)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503, detail="Slack OAuth is unavailable"
        ) from exc
    if identity is None:
        raise HTTPException(status_code=401, detail="Invalid Slack OAuth state")
    slug, email, hosted_domain = identity
    try:
        workspace = get_workspace(slug)
        belongs = domain_belongs_to_workspace(slug, hosted_domain)
        core_enabled = module_enabled("core", slug)
    except Exception as exc:
        logger.warning("Slack OAuth workspace lookup failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=503, detail="Slack workspace is unavailable"
        ) from exc
    if workspace is None or workspace.status != "active" or not belongs:
        raise HTTPException(status_code=401, detail="Slack workspace is unavailable")
    if not core_enabled:
        raise HTTPException(status_code=404, detail="Slack is unavailable")
    frontend = settings.FRONTEND_URL.rstrip("/")
    if error:
        return RedirectResponse(
            f"{frontend}/settings?tab=slack&slack_error=authorization_denied",
            status_code=302,
        )
    if not code:
        raise HTTPException(status_code=400, detail="Missing Slack OAuth code")

    with use_workspace(slug):
        with SessionLocal() as db:
            account = db.get(User, email)
            if account is None or account.is_platform_support:
                raise HTTPException(status_code=401, detail="Slack user is unavailable")
        client_id, redirect_uri = _configured_oauth_client()
        try:
            client_secret = workspace_secret("slack", "client_secret")
        except (PermissionError, RuntimeError) as exc:
            raise HTTPException(
                status_code=503, detail="Slack client secret is unavailable"
            ) from exc
        if not client_secret:
            raise HTTPException(
                status_code=503, detail="Slack client secret is unavailable"
            )
        try:
            response = exchange_code_for_token(
                code, redirect_uri, client_id, client_secret
            )
        except SlackAPIError as exc:
            # Error codes may be shown to the user, but never echo Slack's
            # response body, code, client secret or issued tokens.
            safe_error = "authorization_failed"
            if exc.error in {"access_denied", "bad_redirect_uri", "invalid_client_id"}:
                safe_error = exc.error
            return RedirectResponse(
                f"{frontend}/settings?tab=slack&slack_error={safe_error}",
                status_code=302,
            )
        except Exception as exc:
            logger.warning("Slack OAuth exchange failed: %s", type(exc).__name__)
            raise HTTPException(
                status_code=502, detail="Slack authorization failed"
            ) from exc
        (
            app_id,
            team_id,
            user_id,
            user_token,
            bot_token,
            team_name,
            user_scope,
            bot_scope,
        ) = _validated_exchange(response)
        # The signed state identifies a Cubie user, not the Slack account that
        # approved the consent screen. Confirm Slack's bot-visible email maps
        # to the same member ID as the newly issued personal user token.
        try:
            lookup_id = SlackBotClient(
                bot_token=bot_token, timeout=5.0, max_retries=1
            ).lookup_user_id(email)
        except SlackAPIError as exc:
            logger.warning("Slack OAuth identity lookup failed: %s", exc.error)
            raise HTTPException(
                status_code=502, detail="Slack account verification failed"
            ) from exc
        except Exception as exc:
            logger.warning("Slack OAuth identity lookup failed: %s", type(exc).__name__)
            raise HTTPException(
                status_code=502, detail="Slack account verification failed"
            ) from exc
        if lookup_id != user_id:
            raise HTTPException(
                status_code=403,
                detail=f"Slack account does not match your {PRODUCT_NAME} email",
            )
        # Code exchange and Slack identity lookup can take seconds. A domain
        # removed during that wait must not publish a new personal credential.
        try:
            still_member = domain_belongs_to_workspace(slug, hosted_domain)
        except Exception as exc:
            raise HTTPException(
                status_code=503, detail="Slack workspace is unavailable"
            ) from exc
        if not still_member:
            raise HTTPException(
                status_code=401, detail="Slack workspace is unavailable"
            )
        # The control route and team reservation commit together. A competing
        # workspace must lose the claim before either token is persisted.
        _claim_installation(app_id, team_id)
        try:
            upsert_workspace_secret("slack", "bot_token", bot_token)
        except (PermissionError, RuntimeError) as exc:
            raise HTTPException(
                status_code=503, detail="Slack bot secret is unavailable"
            ) from exc
        with SessionLocal() as db:
            cred = db.get(SlackCredential, email)
            if cred is None:
                cred = SlackCredential(user_email=email)
                db.add(cred)
            cred.slack_user_id = user_id
            cred.slack_team_id = team_id
            cred.slack_team_name = team_name
            cred.hosted_domain = hosted_domain
            cred.access_token = user_token
            cred.scope = user_scope
            cred.bot_scope = bot_scope
            db.commit()
            _log(db, email, "SLACK_CONNECTED", {"team_id": team_id})
    return RedirectResponse(
        f"{frontend}/settings?tab=slack&slack_connected=1", status_code=302
    )


@router.get("/channels/search")
def channels_search(
    q: str = Query(
        "", description="Substring filter; empty returns most-recent channels"
    ),
    limit: int = Query(8, ge=1, le=20),
    user: UserProfile = Depends(get_current_user),
):
    """Autocomplete source for the chat input's `#` channel mention.

    Returns the channels the user is a member of (public + private), filtered
    by case-insensitive substring match on `q`. Empty `q` returns the first
    `limit` channels in `users.conversations` order (typically most-recent).
    """
    from backend.services.slack_service import (
        SlackNotConnectedError,
        SlackService,
    )

    try:
        svc = SlackService(user_email=user.email)
    except Exception as exc:
        logger.warning("Slack service init failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=500, detail="Slack service unavailable"
        ) from exc
    if not svc.is_connected:
        return []
    try:
        all_channels = svc.list_member_channels()
    except SlackNotConnectedError:
        return []
    needle = q.strip().lstrip("#").lower()
    if needle:
        matches = [c for c in all_channels if needle in (c.get("name") or "").lower()]
    else:
        matches = all_channels
    return matches[:limit]


@router.get("/activity", response_model=SlackActivity)
async def activity(
    mention_lookback_days: int = Query(30, ge=1, le=90),
    user: UserProfile = Depends(get_current_user),
):
    """Read-only snapshot of the user's UNREAD Slack inbox.

    Powers the homepage strip and the Productivity → Slack tab: channels with a
    real Slack unread badge, unread @-mentions (read ones are dropped), and
    unread DMs. All reads use the user's own Slack token. Degrades to an empty
    (``is_connected=False``) payload when the user hasn't authorized Slack, and
    to ``error=<code>`` on a token/scope failure — it never 500s a dashboard
    load.
    """
    is_configured = _oauth_configured()
    try:
        svc = SlackService(user_email=user.email)
    except Exception as exc:  # pragma: no cover - defensive init guard
        logger.warning("Slack service init failed: %s", type(exc).__name__)
        return SlackActivity(is_connected=False, is_configured=is_configured)
    if not svc.is_connected:
        return SlackActivity(is_connected=False, is_configured=is_configured)

    try:
        overview = await svc.get_activity_overview(
            mention_lookback_days=mention_lookback_days
        )
    except SlackNotConnectedError:
        return SlackActivity(is_connected=False, is_configured=is_configured)
    except SlackAPIError as e:
        logger.info(f"Slack activity fetch failed for {user.email}: {e.error}")
        return SlackActivity(
            is_connected=True, is_configured=is_configured, error=e.error
        )
    except Exception as exc:
        logger.warning("Slack activity unexpected error: %s", type(exc).__name__)
        return SlackActivity(
            is_connected=True, is_configured=is_configured, error="unknown_error"
        )

    channels = overview["channels"]
    dms = overview["dms"]
    mentions = overview["mentions"]
    return SlackActivity(
        is_connected=True,
        is_configured=is_configured,
        channel_count=len(channels),
        unread_message_count=sum(c["unread_count"] for c in channels),
        mention_count=len(mentions),
        dm_count=len(dms),
        channels=[SlackActivityChannel(**c) for c in channels],
        mentions=[SlackActivityMention(**m) for m in mentions],
        dms=[SlackActivityDM(**d) for d in dms],
    )


@router.get("/channels/{channel_id}/summary", response_model=SlackChannelSummary)
async def channel_summary(
    channel_id: str,
    user: UserProfile = Depends(get_current_user),
):
    """On-demand AI summary of one channel's unread messages (hover popover).

    Reads the channel's unread messages plus a little preceding context (NOT the
    whole history) and returns a short digest of what's being discussed. Fetched
    lazily, per channel, only when the user hovers a chip on the Slack tab — so
    we never summarize everything on load. Degrades to ``error=<code>`` on a
    token/scope failure rather than 500'ing the popover.
    """
    try:
        svc = SlackService(user_email=user.email)
    except Exception as exc:  # pragma: no cover - defensive init guard
        logger.warning("Slack service init failed: %s", type(exc).__name__)
        return SlackChannelSummary(error="init_failed")
    if not svc.is_connected:
        return SlackChannelSummary(error="not_connected")

    try:
        result = await svc.summarize_channel_unread(channel_id)
    except SlackNotConnectedError:
        return SlackChannelSummary(error="not_connected")
    except SlackAPIError as e:
        logger.info(f"Slack channel summary failed for {user.email}: {e.error}")
        return SlackChannelSummary(error=e.error)
    except Exception as exc:
        logger.warning("Slack channel summary unexpected error: %s", type(exc).__name__)
        return SlackChannelSummary(error="unknown_error")

    return SlackChannelSummary(
        summary_md=result["summary_md"], unread_count=result["unread_count"]
    )


@router.post("/disconnect")
def disconnect(
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    cred = (
        db.query(SlackCredential)
        .filter(SlackCredential.user_email == user.email)
        .one_or_none()
    )
    if cred is not None:
        team_id = cred.slack_team_id
        db.delete(cred)
        db.commit()
        _log(db, user.email, "SLACK_DISCONNECTED", {"team_id": team_id})
    return {"ok": True}

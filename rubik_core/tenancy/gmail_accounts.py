"""Per-user Gmail OAuth connections inside an explicitly selected workspace.

The signed, one-use browser state identifies the Cubie user after Google's
redirect, which carries no Cubie Authorization header. A personal mailbox is
never inferred from the user's work email or accessed with domain delegation.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import urlencode, urlparse

import requests
from google.oauth2.credentials import Credentials
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from rubik_core.brand import PRODUCT_NAME
from rubik_core.config import AppMode, settings
from rubik_core.control.repository import get_workspace
from rubik_core.database.db import SessionLocal
from rubik_core.database.models import (
    AuditLog,
    GmailAccount,
    GmailOAuthAttempt,
    Notification,
    User,
    WorkspaceSetting,
)
from rubik_core.tenancy.context import _canonical_slug, current_workspace, use_workspace
from rubik_core.tenancy.platform_secrets import platform_secret

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from rubik_core.ai_providers.registry import ModelRoute

GMAIL_READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
OAUTH_SCOPES = ("openid", "email", GMAIL_READ_SCOPE, GMAIL_COMPOSE_SCOPE)
_REQUIRED_GMAIL_SCOPES = frozenset((GMAIL_READ_SCOPE, GMAIL_COMPOSE_SCOPE))
_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
_STATE_SECONDS = 600
_MAX_STATE_LENGTH = 2048
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+\Z")


class GmailOAuthUnavailable(RuntimeError):
    """The workspace option or platform OAuth setup is unavailable."""


class GmailOAuthRejected(RuntimeError):
    """Google authorization did not yield a verified, usable mailbox."""


def _owner(email: str) -> str:
    if not isinstance(email, str):
        raise ValueError("Invalid account owner")
    value = email.strip().lower()
    if len(value) > 320 or not _EMAIL.fullmatch(value):
        raise ValueError("Invalid account owner")
    return value


def _account_id(value: str) -> str:
    if not isinstance(value, str) or len(value) != 36:
        raise ValueError("Invalid Gmail account ID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError("Invalid Gmail account ID") from exc
    if str(parsed) != value:
        raise ValueError("Invalid Gmail account ID")
    return value


def multi_gmail_enabled() -> bool:
    """Only an explicit workspace-admin opt-in opens extra mailbox access."""
    with SessionLocal() as db:
        row = db.get(WorkspaceSetting, "general")
        value = row.value if row is not None else None
        return isinstance(value, dict) and value.get("multi_gmail_enabled") is True


def _require_enabled() -> None:
    if not multi_gmail_enabled():
        raise PermissionError(
            "Additional Gmail accounts are disabled for this workspace"
        )


def _ai_processing_route() -> tuple[str, str, str]:
    """Public provider/model identity; never fetch or include a credential."""
    from rubik_core.ai_providers.registry import model_routes

    selected = model_routes().get("ai_priorities")
    if selected is None:
        provider, model = "vertex_managed", settings.VERTEX_LLM_MODEL_FLASH
    elif (
        isinstance(selected, dict)
        and isinstance(selected.get("provider"), str)
        and isinstance(selected.get("model"), str)
    ):
        provider, model = selected["provider"], selected["model"]
    else:
        # A corrupt route cannot silently inherit the prior consent.
        provider, model = "unavailable", "invalid"
    fingerprint = hashlib.sha256(f"{provider}\0{model}".encode()).hexdigest()
    return provider, model, fingerprint


def _ai_processing_available() -> bool:
    """Fail closed if the selected route cannot currently process mail."""
    from rubik_core.ai_providers.registry import resolve_model_route

    try:
        resolve_model_route(
            "ai_priorities", "json", default_model=settings.VERTEX_LLM_MODEL_FLASH
        )
    except Exception as exc:
        logger.warning(
            "AI Priorities route unavailable for Gmail: %s", type(exc).__name__
        )
        return False
    return True


@dataclass(frozen=True)
class PersonalMailConsentGuard:
    """Pin one run's approved accounts and route before any model receives mail."""

    workspace_slug: str
    owner_email: str
    account_ids: tuple[str, ...]
    fingerprint: str

    def assert_route(self, route: ModelRoute) -> None:
        from rubik_core.ai_providers.registry import IncompatibleModelRoute

        denied = "Personal Gmail AI consent changed during this priorities run"
        try:
            provider, model, current_fingerprint = _ai_processing_route()
            if (
                current_workspace() != self.workspace_slug
                or route.workspace != self.workspace_slug
                or route.feature != "ai_priorities"
                or current_fingerprint != self.fingerprint
                or route.provider != provider
                # The managed default has per-stage models; an explicit
                # workspace route instead pins one model for every stage.
                or (provider != "vertex_managed" and route.model != model)
                or not multi_gmail_enabled()
            ):
                raise IncompatibleModelRoute(denied)
            with SessionLocal() as db:
                rows = db.scalars(
                    select(GmailAccount).where(GmailAccount.id.in_(self.account_ids))
                ).all()
                if len(rows) != len(self.account_ids) or any(
                    row.owner_email != self.owner_email
                    or row.status != "connected"
                    or row.ai_provider_ack != self.fingerprint
                    for row in rows
                ):
                    raise IncompatibleModelRoute(denied)
        except IncompatibleModelRoute:
            raise
        except Exception as exc:
            raise IncompatibleModelRoute(denied) from exc


def _account_public(
    row: GmailAccount, provider: str, model: str, fingerprint: str, available: bool
) -> dict[str, str | bool]:
    return {
        "id": row.id,
        "email": row.email,
        "status": row.status,
        "ai_processing_review_required": row.status == "connected"
        and row.ai_provider_ack != fingerprint,
        "ai_processing_unavailable": not available,
        "ai_provider": provider,
        "ai_model": model,
        "ai_provider_fingerprint": fingerprint,
    }


def _oauth_config() -> tuple[str, str, str]:
    client_id = settings.GOOGLE_GMAIL_OAUTH_CLIENT_ID or ""
    client_secret = settings.GOOGLE_GMAIL_OAUTH_CLIENT_SECRET or ""
    redirect_uri = settings.GOOGLE_GMAIL_OAUTH_REDIRECT_URI or ""
    try:
        parsed = urlparse(redirect_uri)
    except ValueError as exc:
        raise GmailOAuthUnavailable("Gmail OAuth is unavailable") from exc
    local_http = (
        parsed.scheme == "http"
        and parsed.hostname in {"localhost", "127.0.0.1"}
        and settings.APP_MODE != AppMode.GCP
    )
    if (
        not client_id.endswith(".apps.googleusercontent.com")
        or not client_secret
        or len(client_secret) > 2048
        or len(redirect_uri) > 2048
        or not (parsed.scheme == "https" or local_http)
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path != "/api/email/accounts/callback"
    ):
        raise GmailOAuthUnavailable("Gmail OAuth is unavailable")
    return client_id, client_secret, redirect_uri


def gmail_oauth_available() -> bool:
    """Tell the owner whether connecting can start, without exposing secrets."""
    try:
        _oauth_config()
    except GmailOAuthUnavailable:
        return False
    return True


def _sign_state(
    slug: str, owner_email: str, nonce: str, exp: int, ai_fingerprint: str
) -> str:
    payload = {
        "ws": slug,
        "owner": owner_email,
        "nonce": nonce,
        "exp": exp,
        "ai_fp": ai_fingerprint,
    }
    encoded = (
        base64.urlsafe_b64encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        .decode("ascii")
        .rstrip("=")
    )
    signed = f"v1.{encoded}"
    sig = hmac.new(
        platform_secret("CUBIE_OAUTH_STATE_SECRET").encode("ascii"),
        signed.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return f"{signed}.{sig}"


def _parse_state(state: str) -> tuple[str, str, str, int, str] | None:
    if not isinstance(state, str) or len(state) > _MAX_STATE_LENGTH:
        return None
    try:
        version, encoded, sig = state.split(".")
        if version != "v1" or len(sig) != 64:
            return None
        signed = f"{version}.{encoded}"
        expected = hmac.new(
            platform_secret("CUBIE_OAUTH_STATE_SECRET").encode("ascii"),
            signed.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return None
        payload = json.loads(
            base64.b64decode(
                encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
            )
        )
    except (ValueError, UnicodeError, binascii.Error, TypeError):
        return None
    if not isinstance(payload, dict) or set(payload) != {
        "ws",
        "owner",
        "nonce",
        "exp",
        "ai_fp",
    }:
        return None
    slug, owner_email, nonce, exp, ai_fingerprint = (
        payload["ws"],
        payload["owner"],
        payload["nonce"],
        payload["exp"],
        payload["ai_fp"],
    )
    if (
        not isinstance(slug, str)
        or not isinstance(owner_email, str)
        or not isinstance(nonce, str)
        or not isinstance(exp, int)
        or isinstance(exp, bool)
        or len(nonce) < 16
        or len(nonce) > 128
        or exp < time.time()
        or exp > time.time() + _STATE_SECONDS
        or not isinstance(ai_fingerprint, str)
        or not re.fullmatch(r"[0-9a-f]{64}", ai_fingerprint)
    ):
        return None
    try:
        _canonical_slug(slug)
        if _owner(owner_email) != owner_email:
            return None
    except ValueError:
        return None
    return slug, owner_email, nonce, exp, ai_fingerprint


def _nonce_hash(nonce: str) -> str:
    return hashlib.sha256(nonce.encode("ascii")).hexdigest()


def begin_connection(
    owner_email: str, browser_nonce: str, expected_ai_fingerprint: str
) -> str:
    """Reserve a browser-bound OAuth attempt and return Google's authorize URL."""
    _require_enabled()
    owner_email = _owner(owner_email)
    if (
        not isinstance(browser_nonce, str)
        or len(browser_nonce) < 32
        or len(browser_nonce) > 128
    ):
        raise ValueError("Invalid Gmail connection browser nonce")
    client_id, _, redirect_uri = _oauth_config()
    slug = current_workspace()
    _, _, ai_fingerprint = _ai_processing_route()
    if expected_ai_fingerprint != ai_fingerprint:
        raise ValueError("The workspace AI provider changed. Refresh and review again.")
    if not _ai_processing_available():
        raise ValueError("The workspace AI provider is unavailable. Try again later.")
    nonce = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    challenge = challenge.decode("ascii").rstrip("=")
    expiry = datetime.utcnow() + timedelta(seconds=_STATE_SECONDS)
    with SessionLocal() as db:
        user = db.get(User, owner_email)
        if user is None or user.is_platform_support:
            raise PermissionError("Gmail account owner is unavailable")
        db.execute(
            delete(GmailOAuthAttempt).where(
                GmailOAuthAttempt.expires_at < datetime.utcnow()
            )
        )
        db.add(
            GmailOAuthAttempt(
                nonce_hash=_nonce_hash(nonce),
                browser_nonce_hash=_nonce_hash(browser_nonce),
                owner_email=owner_email,
                code_verifier=verifier,
                expires_at=expiry,
            )
        )
        db.commit()
    state = _sign_state(
        slug, owner_email, nonce, int(time.time()) + _STATE_SECONDS, ai_fingerprint
    )
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(OAUTH_SCOPES),
        "access_type": "offline",
        "prompt": "select_account consent",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{_AUTH_URL}?{urlencode(params)}"


def _consume_state(state: str, browser_nonce: str | None) -> tuple[str, str, str, str]:
    if (
        not isinstance(browser_nonce, str)
        or len(browser_nonce) < 32
        or len(browser_nonce) > 128
    ):
        raise PermissionError("Gmail authorization browser binding is missing")
    identity = _parse_state(state)
    if identity is None:
        raise PermissionError("Invalid Gmail authorization state")
    slug, owner_email, nonce, _, ai_fingerprint = identity
    workspace = get_workspace(slug)
    if workspace is None or workspace.status != "active":
        raise PermissionError("Gmail workspace is unavailable")
    with use_workspace(slug):
        _require_enabled()
        with SessionLocal() as db:
            user = db.get(User, owner_email)
            if user is None or user.is_platform_support:
                raise PermissionError("Gmail account owner is unavailable")
            attempt = db.get(GmailOAuthAttempt, _nonce_hash(nonce))
            if attempt is None or not hmac.compare_digest(
                attempt.browser_nonce_hash, _nonce_hash(browser_nonce)
            ):
                raise PermissionError("Gmail authorization browser binding is invalid")
            changed = db.execute(
                update(GmailOAuthAttempt)
                .where(
                    GmailOAuthAttempt.nonce_hash == _nonce_hash(nonce),
                    GmailOAuthAttempt.owner_email == owner_email,
                    GmailOAuthAttempt.expires_at > datetime.utcnow(),
                    GmailOAuthAttempt.consumed_at.is_(None),
                )
                .values(consumed_at=datetime.utcnow())
            )
            if changed.rowcount != 1:
                db.rollback()
                raise PermissionError("Gmail authorization state was already used")
            verifier = attempt.code_verifier
            db.commit()
    return slug, owner_email, verifier, ai_fingerprint


def _exchange_code(code: str, verifier: str) -> dict:
    client_id, client_secret, redirect_uri = _oauth_config()
    try:
        response = requests.post(
            _TOKEN_URL,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": verifier,
            },
            timeout=10,
            allow_redirects=False,
        )
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Gmail OAuth token exchange failed: %s", type(exc).__name__)
        raise GmailOAuthRejected("Gmail authorization failed") from exc
    if response.status_code != 200 or not isinstance(payload, dict):
        raise GmailOAuthRejected("Gmail authorization failed")
    return payload


def _verified_identity(access_token: str) -> tuple[str, str]:
    try:
        response = requests.get(
            _USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
            allow_redirects=False,
        )
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Gmail OAuth userinfo failed: %s", type(exc).__name__)
        raise GmailOAuthRejected("Google account verification failed") from exc
    if response.status_code != 200 or not isinstance(payload, dict):
        raise GmailOAuthRejected("Google account verification failed")
    sub, email = payload.get("sub"), payload.get("email")
    if (
        not isinstance(sub, str)
        or not sub
        or len(sub) > 255
        or payload.get("email_verified") is not True
    ):
        raise GmailOAuthRejected("Google account verification failed")
    try:
        normalized_email = _owner(email)
    except ValueError as exc:
        raise GmailOAuthRejected("Google account verification failed") from exc
    return sub, normalized_email


def complete_connection(
    state: str, code: str, browser_nonce: str | None
) -> dict[str, str]:
    """Consume signed state once, exchange code, and persist verified identity."""
    if not isinstance(code, str) or not code or len(code) > 4096:
        raise ValueError("Missing Gmail authorization code")
    slug, owner_email, verifier, accepted_fingerprint = _consume_state(
        state, browser_nonce
    )
    with use_workspace(slug):
        token = _exchange_code(code, verifier)
        access = token.get("access_token")
        refresh = token.get("refresh_token")
        granted = token.get("scope")
        scopes = granted.split() if isinstance(granted, str) else []
        if (
            not isinstance(access, str)
            or not access
            or not isinstance(refresh, str)
            or not refresh
            or len(refresh) > 8192
            or not _REQUIRED_GMAIL_SCOPES.issubset(scopes)
        ):
            raise GmailOAuthRejected("Required Gmail access was not granted")
        google_sub, mailbox_email = _verified_identity(access)
        if mailbox_email == owner_email:
            raise GmailOAuthRejected(
                "Your work mailbox is already the default Gmail account"
            )
        _require_enabled()
        workspace = get_workspace(slug)
        if workspace is None or workspace.status != "active":
            raise PermissionError("Gmail workspace is unavailable")
        provider, model, fingerprint = _ai_processing_route()
        available = _ai_processing_available()
        with SessionLocal() as db:
            user = db.get(User, owner_email)
            if user is None or user.is_platform_support:
                raise PermissionError("Gmail account owner is unavailable")
            row = db.scalar(
                select(GmailAccount).where(
                    GmailAccount.owner_email == owner_email,
                    GmailAccount.google_sub == google_sub,
                )
            )
            if row is None:
                row = GmailAccount(
                    id=str(uuid.uuid4()),
                    owner_email=owner_email,
                    google_sub=google_sub,
                    email=mailbox_email,
                    refresh_token=refresh,
                    scopes=scopes,
                    status="connected",
                    ai_provider_ack=accepted_fingerprint,
                )
                db.add(row)
            else:
                row.email = mailbox_email
                row.refresh_token = refresh
                row.scopes = scopes
                row.status = "connected"
                row.ai_provider_ack = accepted_fingerprint
            row.ai_provider_notice = None
            db.add(
                AuditLog(
                    actor_email=owner_email,
                    action="GMAIL_ACCOUNT_CONNECTED",
                    status="success",
                    details={"account_id": row.id},
                )
            )
            try:
                db.commit()
            except IntegrityError as exc:
                db.rollback()
                raise GmailOAuthRejected("Gmail account is already connected") from exc
            return _account_public(row, provider, model, fingerprint, available)


def list_accounts(owner_email: str) -> list[dict[str, str | bool]]:
    owner_email = _owner(owner_email)
    provider, model, fingerprint = _ai_processing_route()
    available = _ai_processing_available()
    with SessionLocal() as db:
        rows = db.scalars(
            select(GmailAccount)
            .where(GmailAccount.owner_email == owner_email)
            .order_by(GmailAccount.email)
        ).all()
        notices_added = False
        if multi_gmail_enabled():
            for row in rows:
                if (
                    row.status != "connected"
                    or row.ai_provider_ack == fingerprint
                    or row.ai_provider_notice == fingerprint
                ):
                    continue
                notification_id = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"gmail-ai:{current_workspace()}:{row.id}:{fingerprint}",
                    )
                )
                if db.get(Notification, notification_id) is None:
                    db.add(
                        Notification(
                            id=notification_id,
                            actor_email=owner_email,
                            kind="gmail_ai_provider_review",
                            title="Review AI access for your Gmail account",
                            body=(
                                f"Your workspace changed its AI Priorities model to "
                                f"{provider} {model}. Review {row.email} in Settings "
                                f"before {PRODUCT_NAME} includes it in AI Priorities and scheduled summaries again."
                            ),
                            severity="warn",
                            link="/settings?tab=email",
                        )
                    )
                row.ai_provider_notice = fingerprint
                notices_added = True
        result = [
            _account_public(row, provider, model, fingerprint, available)
            for row in rows
        ]
        if notices_added:
            try:
                db.commit()
            except IntegrityError:
                # Another run created the same deterministic notice first.
                db.rollback()
        return result


def acknowledge_ai_provider(
    owner_email: str, account_id: str, expected_fingerprint: str
) -> dict[str, str | bool]:
    """Allow owner-only automatic AI use under exactly the model they saw."""
    _require_enabled()
    owner_email = _owner(owner_email)
    account_id = _account_id(account_id)
    provider, model, fingerprint = _ai_processing_route()
    if expected_fingerprint != fingerprint:
        raise ValueError("The workspace AI provider changed. Refresh and review again.")
    if not _ai_processing_available():
        raise ValueError("The workspace AI provider is unavailable. Try again later.")
    with SessionLocal() as db:
        row = db.get(GmailAccount, account_id)
        if row is None or row.owner_email != owner_email or row.status != "connected":
            raise PermissionError("Gmail account is unavailable")
        row.ai_provider_ack = fingerprint
        db.add(
            AuditLog(
                actor_email=owner_email,
                action="GMAIL_AI_PROVIDER_ACKNOWLEDGED",
                status="success",
                details={
                    "account_id": row.id,
                    "provider": provider,
                    "model": model,
                },
            )
        )
        db.commit()
        return _account_public(row, provider, model, fingerprint, True)


def get_account_credentials(
    owner_email: str, account_id: str
) -> tuple[Credentials, str]:
    """Resolve a connected mailbox for this owner and current workspace."""
    from rubik_core.auth.impersonation import impersonation_active
    from rubik_core.tenancy.support_context import current_support_session

    if current_support_session() is not None or impersonation_active():
        raise PermissionError("Gmail account is unavailable")
    _require_enabled()
    owner_email = _owner(owner_email)
    account_id = _account_id(account_id)
    client_id, client_secret, _ = _oauth_config()
    with SessionLocal() as db:
        user = db.get(User, owner_email)
        row = db.get(GmailAccount, account_id)
        if (
            user is None
            or user.is_platform_support
            or row is None
            or row.owner_email != owner_email
            or row.status != "connected"
            or not isinstance(row.scopes, list)
            or not _REQUIRED_GMAIL_SCOPES.issubset(row.scopes)
        ):
            raise PermissionError("Gmail account is unavailable")
        refresh_token = row.refresh_token
        mailbox_email = row.email
        scopes = row.scopes
    if not refresh_token:
        raise PermissionError("Gmail account needs reconnection")
    credential = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=_TOKEN_URL,
        client_id=client_id,
        client_secret=client_secret,
        scopes=scopes,
    )
    return credential, mailbox_email


def mark_account_reauth_required(
    owner_email: str, account_id: str, *, failed_refresh_token: str
) -> None:
    """Stop using only the credential Google rejected, preserving rotations."""
    owner_email = _owner(owner_email)
    account_id = _account_id(account_id)
    with SessionLocal() as db:
        row = db.get(GmailAccount, account_id)
        if (
            row is None
            or row.owner_email != owner_email
            or row.refresh_token != failed_refresh_token
        ):
            return
        row.status = "reauth_required"
        db.commit()


def disconnect_account(owner_email: str, account_id: str) -> bool:
    """Disable local access first, then best-effort revoke at Google."""
    owner_email = _owner(owner_email)
    account_id = _account_id(account_id)
    with SessionLocal() as db:
        row = db.get(GmailAccount, account_id)
        if row is None or row.owner_email != owner_email:
            return False
        refresh_token = row.refresh_token
        db.delete(row)
        db.add(
            AuditLog(
                actor_email=owner_email,
                action="GMAIL_ACCOUNT_DISCONNECTED",
                status="success",
                details={"account_id": account_id},
            )
        )
        db.commit()
    if refresh_token:
        try:
            requests.post(
                _REVOKE_URL,
                data={"token": refresh_token},
                timeout=5,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            logger.warning("Gmail token revoke failed: %s", type(exc).__name__)
    return True

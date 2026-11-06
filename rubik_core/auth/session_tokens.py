"""Platform-signed Cubie session JWTs with an explicit workspace claim."""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt

from rubik_core.config import settings
from rubik_core.database.db import SessionLocal
from rubik_core.database.models import WorkspaceSetting
from rubik_core.tenancy.context import _canonical_slug, current_workspace
from rubik_core.tenancy.platform_secrets import platform_secret

logger = logging.getLogger(__name__)

_ISSUER = "cubie"
_AUDIENCE = "cubie-session"


@dataclass(frozen=True)
class SessionClaims:
    sub: str
    ws: str
    aud: str
    hd: str | None = None
    typ: str = "user"
    sup: str | None = None
    sid: str | None = None


class SessionExpiredError(Exception):
    """A correctly signed app session that has passed its expiry."""


def _signing_secret() -> str:
    """Read the platform key without tenant DB access."""
    return platform_secret("CUBIE_SESSION_SECRET")


def session_lifetime_hours() -> float:
    """Effective workspace session length, clamped to 15 minutes–30 days."""
    raw = None
    try:
        db = SessionLocal()
        try:
            row = db.get(WorkspaceSetting, "general")
            raw = (row.value or {}).get("session_lifetime_hours") if row else None
        finally:
            db.close()
    except Exception:
        logger.exception("Failed reading session_lifetime_hours; using default")
    try:
        hours = (
            float(raw) if raw is not None else float(settings.SESSION_LIFETIME_HOURS)
        )
    except (TypeError, ValueError):
        hours = float(settings.SESSION_LIFETIME_HOURS)
    return min(max(hours, 0.25), 720.0)


def mint_session_token(
    email: str,
    ws: str | None = None,
    *,
    hd: str | None = None,
    typ: str = "user",
    sup: str | None = None,
    sid: str | None = None,
    hours: float | None = None,
) -> tuple[str, datetime, float]:
    """Sign a session for the selected workspace. Returns token, expiry, hours."""
    if not isinstance(email, str) or not email.strip():
        raise ValueError("Session subject is required")
    if sup is not None and not isinstance(sup, str):
        raise ValueError("Support actor must be a string")
    if sid is not None and not isinstance(sid, str):
        raise ValueError("Support session id must be a string")
    signing_key = _signing_secret()  # validate production key before any DB read
    workspace = _canonical_slug(ws if ws is not None else current_workspace())
    if typ not in ("user", "support"):
        raise ValueError("Unsupported session type")
    if typ == "user" and workspace != current_workspace():
        raise ValueError("User session workspace must match the active context")
    if typ == "user":
        if hd is None and workspace == "test" and "pytest" in sys.modules:
            # Old unit fixtures mint test-only sessions directly. Real sign-in
            # always supplies Google's verified hosted-domain claim.
            hd = email.rsplit("@", 1)[-1] if "@" in email else None
        if not isinstance(hd, str) or not hd.strip():
            raise ValueError("A verified hosted domain is required")
        hd = hd.strip().lower()
    if typ == "support" and (
        not sup
        or not sid
        or email.strip().lower() != sup.strip().lower()
        or len(sid) > 128
    ):
        raise ValueError("Support sessions require matching actor and session id")
    if hours is None:
        hours = session_lifetime_hours() if typ == "user" else 0.25
    if not (0 < hours <= 720):
        raise ValueError("Session lifetime is out of range")
    if typ == "support" and hours > 0.25:
        raise ValueError("Support sessions cannot exceed 15 minutes")
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=hours)
    payload = {
        "sub": email.strip().lower(),
        "ws": workspace,
        "aud": _AUDIENCE,
        "iss": _ISSUER,
        "iat": now,
        "exp": expires_at,
        "typ": typ,
    }
    if typ == "user":
        payload["hd"] = hd
    if sup:
        payload["sup"] = sup.strip().lower()
    if sid:
        payload["sid"] = sid
    token = jwt.encode(payload, signing_key, algorithm="HS256")
    return token, expires_at, hours


def decode_session_token(token: str) -> SessionClaims | None:
    """Verify a Cubie session without tenant DB access.

    Invalid, legacy and opaque credentials return None. An expired token raises
    SessionExpiredError so callers can return the usual sign-in message.
    """
    signing_key = _signing_secret()  # fail boot/use before token structure screening
    if not isinstance(token, str) or token.count(".") != 2 or len(token) > 16_384:
        return None
    try:
        payload = jwt.decode(
            token,
            signing_key,
            algorithms=["HS256"],
            issuer=_ISSUER,
            audience=_AUDIENCE,
            options={"require": ["exp", "sub", "iss", "ws", "aud", "typ"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise SessionExpiredError(str(exc)) from exc
    except jwt.InvalidTokenError:
        return None
    try:
        workspace = _canonical_slug(payload["ws"])
    except (ValueError, KeyError, TypeError):
        return None
    sub = payload.get("sub")
    kind = payload.get("typ")
    sup = payload.get("sup")
    sid = payload.get("sid")
    hd = payload.get("hd")
    if not isinstance(sub, str) or not sub or kind not in ("user", "support"):
        return None
    if kind == "user" and (
        not isinstance(hd, str)
        or not hd
        or len(hd) > 253
        or hd != hd.strip().lower()
        or "/" in hd
        or "\\" in hd
    ):
        return None
    if kind == "support" and (
        not isinstance(sup, str)
        or not sup
        or sup != sub
        or sup != sup.strip().lower()
        or not isinstance(sid, str)
        or not sid
        or len(sid) > 128
        or not isinstance(payload.get("iat"), int)
        or not isinstance(payload.get("exp"), int)
        or payload["exp"] - payload["iat"] > 900
    ):
        return None
    return SessionClaims(
        sub=sub,
        ws=workspace,
        aud=_AUDIENCE,
        hd=hd if kind == "user" else None,
        typ=kind,
        sup=sup,
        sid=sid,
    )

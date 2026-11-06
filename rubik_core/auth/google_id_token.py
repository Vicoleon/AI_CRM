"""Verify the Google ID token from GIS before routing a sign-in."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock

import jwt
import requests
from google.auth.exceptions import TransportError
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token

from rubik_core.config import settings


@dataclass(frozen=True)
class GoogleIdentity:
    email: str
    hd: str
    sub: str
    name: str
    picture: str | None


class GoogleIdentityError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 401):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class _CachedCertResponse:
    status: int
    headers: dict[str, str]
    data: bytes


_CERT_URL = id_token._GOOGLE_OAUTH2_CERTS_URL
_cert_lock = Lock()
_cert_cache: tuple[float, _CachedCertResponse] | None = None
_rotation_retry_after = 0.0
_cert_failure_retry_after = 0.0


def _cert_ttl(headers) -> int:
    directives = headers.get("Cache-Control", "")
    match = re.search(r"(?:^|,)\s*max-age=(\d+)", directives, re.IGNORECASE)
    # Google's max-age controls rotation; the cap prevents stale keys after a
    # bad upstream cache header. A short default covers proxies that omit it.
    return min(int(match.group(1)), 300) if match else 60


def _cached_kids(data: bytes) -> set[str]:
    try:
        certs = json.loads(data)
    except (TypeError, ValueError):
        return set()
    if isinstance(certs, dict) and isinstance(certs.get("keys"), list):
        return {
            key["kid"]
            for key in certs["keys"]
            if isinstance(key, dict) and isinstance(key.get("kid"), str)
        }
    if isinstance(certs, dict):
        return set(certs)
    return set()


def _refresh_for_new_key(raw: str) -> None:
    """Refresh cached certs once when a newly rotated signing kid appears."""
    global _cert_cache, _rotation_retry_after
    try:
        kid = jwt.get_unverified_header(raw).get("kid")
    except jwt.InvalidTokenError:
        return
    if not isinstance(kid, str) or not kid:
        return
    now = time.monotonic()
    with _cert_lock:
        if _cert_cache is None or now >= _cert_cache[0]:
            return
        if kid in _cached_kids(_cert_cache[1].data):
            return
        # Invalid random kids on the public sign-in path must not force one
        # certificate fetch each. A legitimate rotation can wait at most 30 s
        # if another unknown kid just triggered a refresh.
        if now < _rotation_retry_after:
            return
        _cert_cache = None
        _rotation_retry_after = now + 30


class _BoundedGoogleRequest(GoogleRequest):
    """Cap and briefly cache Google's public certificate fetch."""

    def __call__(self, url, method="GET", body=None, headers=None, timeout=5, **kwargs):
        global _cert_cache, _cert_failure_retry_after
        bounded = min(timeout or 5, 5)
        if method.upper() != "GET" or url != _CERT_URL:
            return super().__call__(
                url,
                method=method,
                body=body,
                headers=headers,
                timeout=bounded,
                **kwargs,
            )
        with _cert_lock:
            now = time.monotonic()
            if _cert_cache is not None and now < _cert_cache[0]:
                return _cert_cache[1]
            if now < _cert_failure_retry_after:
                raise requests.exceptions.ConnectionError(
                    "Google certificates temporarily unavailable"
                )
            try:
                response = super().__call__(
                    url,
                    method=method,
                    body=body,
                    headers=headers,
                    timeout=bounded,
                    **kwargs,
                )
            except Exception:
                # One failed fetch is enough to pause retries from concurrent
                # sign-ins. Keep the pause short so recovery remains prompt.
                _cert_failure_retry_after = time.monotonic() + 5
                raise
            if response.status != 200:
                _cert_failure_retry_after = time.monotonic() + 5
                return response
            _cert_failure_retry_after = 0.0
            ttl = _cert_ttl(response.headers)
            if ttl > 0:
                _cert_cache = (
                    time.monotonic() + ttl,
                    _CachedCertResponse(
                        response.status, dict(response.headers), response.data
                    ),
                )
            return response


def _verify_google_token(raw: str, client_id: str) -> dict:
    # Google's library checks the signature against current keys and expiry.
    _refresh_for_new_key(raw)
    return id_token.verify_oauth2_token(raw, _BoundedGoogleRequest(), client_id)


def verify_google_id_token(raw: str) -> GoogleIdentity:
    """Require Google signature, audience, issuer and hosted-domain identity."""
    client_id = settings.GOOGLE_CLIENT_ID
    if not isinstance(client_id, str) or not client_id.strip():
        raise GoogleIdentityError(
            "sign_in_not_configured", "Google sign-in is not configured", 503
        )
    if not isinstance(raw, str) or not raw or len(raw) > 16_384:
        raise GoogleIdentityError("invalid_google_token", "Invalid Google ID token")
    try:
        claims = _verify_google_token(raw, client_id)
    except (requests.exceptions.RequestException, TransportError) as exc:
        raise GoogleIdentityError(
            "google_unavailable", "Google sign-in is temporarily unavailable", 503
        ) from exc
    except Exception as exc:
        raise GoogleIdentityError(
            "invalid_google_token", "Invalid Google ID token"
        ) from exc
    if claims.get("aud") != client_id or claims.get("iss") not in (
        "accounts.google.com",
        "https://accounts.google.com",
    ):
        raise GoogleIdentityError("invalid_google_token", "Invalid Google ID token")
    try:
        expires_at = float(claims["exp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise GoogleIdentityError(
            "invalid_google_token", "Google token has no expiry"
        ) from exc
    if expires_at <= datetime.now(timezone.utc).timestamp():
        raise GoogleIdentityError("invalid_google_token", "Google token has expired")
    verified = claims.get("email_verified")
    if verified is not True and verified != "true":
        raise GoogleIdentityError(
            "invalid_google_token", "Google email is not verified"
        )
    hd = str(claims.get("hd") or "").strip().lower()
    if not hd:
        raise GoogleIdentityError(
            "workspace_personal", "Use a company Google Workspace account", 403
        )
    email = str(claims.get("email") or "").strip().lower()
    sub = str(claims.get("sub") or "").strip()
    if not email or "@" not in email or not sub:
        raise GoogleIdentityError(
            "invalid_google_token", "Google identity is incomplete"
        )
    return GoogleIdentity(
        email=email,
        hd=hd,
        sub=sub,
        name=str(claims.get("name") or email.split("@", 1)[0]),
        picture=str(claims["picture"]) if claims.get("picture") else None,
    )


def verify_google_access_token(raw: str) -> GoogleIdentity:
    """Meet-only one-time fallback, checked online with bounded requests."""
    client_id = settings.GOOGLE_CLIENT_ID
    if not isinstance(client_id, str) or not client_id.strip():
        raise GoogleIdentityError(
            "sign_in_not_configured", "Google sign-in is not configured", 503
        )
    if not isinstance(raw, str) or not raw or len(raw) > 16_384:
        raise GoogleIdentityError("invalid_google_token", "Invalid Google access token")
    try:
        profile_response = requests.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {raw}"},
            timeout=10,
        )
        audience_response = requests.get(
            "https://oauth2.googleapis.com/tokeninfo",
            params={"access_token": raw},
            timeout=10,
        )
    except requests.exceptions.RequestException as exc:
        raise GoogleIdentityError(
            "google_unavailable", "Google sign-in is temporarily unavailable", 503
        ) from exc
    if profile_response.status_code >= 500 or audience_response.status_code >= 500:
        raise GoogleIdentityError(
            "google_unavailable", "Google sign-in is temporarily unavailable", 503
        )
    if profile_response.status_code != 200 or audience_response.status_code != 200:
        raise GoogleIdentityError("invalid_google_token", "Invalid Google access token")
    try:
        profile = profile_response.json()
        audience = audience_response.json()
    except (TypeError, ValueError) as exc:
        raise GoogleIdentityError(
            "invalid_google_token", "Invalid Google access token"
        ) from exc
    if (audience.get("aud") or audience.get("azp")) != client_id:
        raise GoogleIdentityError(
            "invalid_google_token", "Invalid Google access token audience"
        )
    verified = profile.get("email_verified")
    if verified is not True and verified != "true":
        raise GoogleIdentityError(
            "invalid_google_token", "Google email is not verified"
        )
    hd = str(profile.get("hd") or "").strip().lower()
    if not hd:
        raise GoogleIdentityError(
            "workspace_personal", "Use a company Google Workspace account", 403
        )
    email = str(profile.get("email") or "").strip().lower()
    sub = str(profile.get("sub") or "").strip()
    if not email or "@" not in email or not sub:
        raise GoogleIdentityError(
            "invalid_google_token", "Google identity is incomplete"
        )
    return GoogleIdentity(
        email=email,
        hd=hd,
        sub=sub,
        name=str(profile.get("name") or email.split("@", 1)[0]),
        picture=str(profile["picture"]) if profile.get("picture") else None,
    )

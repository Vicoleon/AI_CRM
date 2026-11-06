"""Mint, check, list and revoke the per-browser key Cubie for Chrome uses.

The table and why it exists: ``rubik_core/database/browser_connections.py``.

A raw key is ``cbx_<workspace>.<nonce>~<routing MAC>``. The MAC binds the slug
before any tenant DB is opened; the full key's SHA-256 is then checked in only
that workspace's database. The random nonce holds 256 bits. The MAC is keyed
by the platform session secret with a separate purpose label.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from rubik_core.config import settings
from rubik_core.control.repository import domain_belongs_to_workspace
from rubik_core.database.browser_connections import BrowserConnection
from rubik_core.tenancy.context import current_workspace
from rubik_core.tenancy.platform_secrets import platform_secret

TOKEN_PREFIX = "cbx_"
_TOKEN_RE = re.compile(
    r"\Acbx_([a-z0-9]+(?:-[a-z0-9]+)*)\.([A-Za-z0-9_-]{43})~([A-Za-z0-9_-]{43})\Z"
)
_MAC_PURPOSE = b"cubie-extension-route-v1:"
# One connected person rarely has more than two browsers; the cap stops a
# script (or a stuck Connect button) from piling up live keys.
MAX_LIVE_PER_USER = 5
# ``last_used_at`` is "roughly when", not an audit trail: stamp it at most this
# often so the extension's 15-minute refresh is not a write per call.
LAST_USED_RESOLUTION = timedelta(minutes=10)
LABEL_MAX = 60


def _fold(email: str) -> str:
    return (email or "").strip().lower()


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _route_mac(slug: str, nonce: str) -> str:
    key = platform_secret("CUBIE_SESSION_SECRET").encode("utf-8")
    digest = hmac.new(
        key, _MAC_PURPOSE + f"{slug}.{nonce}".encode("ascii"), hashlib.sha256
    ).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def mint_extension_key() -> str:
    """Create a random key whose workspace routing prefix cannot be changed."""
    slug = current_workspace()
    nonce = secrets.token_urlsafe(32)
    return f"{TOKEN_PREFIX}{slug}.{nonce}~{_route_mac(slug, nonce)}"


def parse_extension_workspace(raw: str | None) -> str | None:
    """Verify the route MAC without a DB read; None for old or tampered keys."""
    if not isinstance(raw, str) or len(raw) > 160:
        return None
    match = _TOKEN_RE.fullmatch(raw)
    if match is None or len(match.group(1)) > 63:
        return None
    slug, nonce, presented = match.groups()
    return slug if hmac.compare_digest(presented, _route_mac(slug, nonce)) else None


def looks_like_token(raw: str | None) -> bool:
    return parse_extension_workspace(raw) is not None


@dataclass(frozen=True)
class Minted:
    connection: BrowserConnection
    #: Shown once, to the Connect page, then handed to the extension.
    raw_token: str


def _live_query(db: Session, user_email: str, now: datetime):
    return db.query(BrowserConnection).filter(
        BrowserConnection.user_email == _fold(user_email),
        BrowserConnection.hosted_domain.isnot(None),
        BrowserConnection.revoked_at.is_(None),
        BrowserConnection.expires_at > now,
    )


def mint(
    db: Session,
    user_email: str,
    *,
    hosted_domain: str,
    label: str | None,
    replaces: str | None = None,
    now: datetime | None = None,
) -> Minted:
    """Create one live key for ``user_email``. The caller commits.

    ``replaces`` is the id of the key this browser held before (the extension
    sends it when it connects again), which is revoked as ``replaced`` so a
    reconnect does not leave the old key live. Past ``MAX_LIVE_PER_USER`` the
    oldest live keys are revoked the same way.
    """
    now = now or datetime.utcnow()
    email = _fold(user_email)
    domain = (hosted_domain or "").strip().lower()
    if not domain or not domain_belongs_to_workspace(current_workspace(), domain):
        raise PermissionError("Verified hosted domain is unavailable")
    if replaces:
        old = (
            _live_query(db, email, now)
            .filter(BrowserConnection.id == replaces)
            .one_or_none()
        )
        if old is not None:
            old.revoked_at = now
            old.revoked_reason = "replaced"

    raw = mint_extension_key()
    row = BrowserConnection(
        user_email=email,
        hosted_domain=domain,
        token_hash=hash_token(raw),
        token_hint=raw[-4:],
        label=(label or "").strip()[:LABEL_MAX] or "Chrome",
        created_at=now,
        expires_at=now + timedelta(days=settings.CHROME_EXTENSION_TOKEN_TTL_DAYS),
    )
    db.add(row)
    db.flush()

    live = (
        _live_query(db, email, now)
        .order_by(BrowserConnection.created_at.desc(), BrowserConnection.id.desc())
        .all()
    )
    for extra in live[MAX_LIVE_PER_USER:]:
        extra.revoked_at = now
        extra.revoked_reason = "replaced"
    return Minted(connection=row, raw_token=raw)


def resolve(
    db: Session, raw: str | None, *, now: datetime | None = None
) -> BrowserConnection | None:
    """The live connection a raw key belongs to, else None.

    Stamps ``last_used_at`` (coarsely, see ``LAST_USED_RESOLUTION``); the
    caller commits.
    """
    slug = parse_extension_workspace(raw)
    if slug is None or slug != current_workspace():
        return None
    now = now or datetime.utcnow()
    row = (
        db.query(BrowserConnection)
        .filter(BrowserConnection.token_hash == hash_token(raw))
        .one_or_none()
    )
    if row is None or row.revoked_at is not None or row.expires_at <= now:
        return None
    if not row.hosted_domain:
        return None
    try:
        if not domain_belongs_to_workspace(slug, row.hosted_domain):
            return None
    except Exception:  # noqa: BLE001 - a control DB outage never admits a key
        return None
    if row.last_used_at is None or now - row.last_used_at >= LAST_USED_RESOLUTION:
        row.last_used_at = now
    return row


def list_live(
    db: Session, user_email: str, *, now: datetime | None = None
) -> list[BrowserConnection]:
    now = now or datetime.utcnow()
    rows = (
        _live_query(db, user_email, now)
        .order_by(BrowserConnection.created_at.desc())
        .all()
    )
    try:
        return [
            row
            for row in rows
            if domain_belongs_to_workspace(current_workspace(), row.hosted_domain)
        ]
    except Exception:  # noqa: BLE001 - control data must be readable to claim a live key
        return []


def revoke(
    db: Session,
    user_email: str,
    connection_id: str,
    *,
    reason: str = "disconnected",
    now: datetime | None = None,
) -> bool:
    """Revoke one of ``user_email``'s own keys. False when it is not theirs or
    already dead. The caller commits."""
    now = now or datetime.utcnow()
    row = (
        db.query(BrowserConnection)
        .filter(
            BrowserConnection.id == connection_id,
            BrowserConnection.user_email == _fold(user_email),
            BrowserConnection.revoked_at.is_(None),
        )
        .one_or_none()
    )
    if row is None:
        return False
    row.revoked_at = now
    row.revoked_reason = reason
    return True


def revoke_all(
    db: Session, user_email: str, *, reason: str, now: datetime | None = None
) -> int:
    """Revoke every live key a person holds (offboarding). The caller commits."""
    now = now or datetime.utcnow()
    return (
        db.query(BrowserConnection)
        .filter(
            BrowserConnection.user_email == _fold(user_email),
            BrowserConnection.revoked_at.is_(None),
        )
        .update(
            {
                BrowserConnection.revoked_at: now,
                BrowserConnection.revoked_reason: reason,
            },
            synchronize_session=False,
        )
    )


def to_public(row: BrowserConnection) -> dict[str, Any]:
    """What Settings shows. Never the hash."""

    def iso(value: datetime | None) -> str | None:
        return value.isoformat() + "Z" if value else None

    return {
        "id": row.id,
        "label": row.label,
        "hint": row.token_hint,
        "created_at": iso(row.created_at),
        "expires_at": iso(row.expires_at),
        "last_used_at": iso(row.last_used_at),
    }

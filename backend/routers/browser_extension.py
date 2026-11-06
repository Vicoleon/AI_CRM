"""Cubie for Chrome — ``/api/browser-extension``.

Two audiences, two gates, one router:

* **The Cubie web app** (signed-in session) connects and disconnects browsers:
  ``POST/GET /connections``, ``DELETE /connections/{id}``. Gate:
  ``require_chrome_extension`` on top of ``get_current_user``.
* **The extension** (its per-browser key, never a session) reads its new tab
  and ticks things off: ``GET /glance``, ``POST /done``, ``POST /disconnect``.
  Gate: ``require_extension_user``, which resolves the key, then applies the
  same department lockout every session gets (``_apply_access_gates``) and the
  same roster.

Both gates answer 404 while ``CHROME_EXTENSION_ENABLED`` is off (no such
surface here) and 403 to someone not on the ``chrome_extension`` roster.

Mounted OUTSIDE the copilot ``core`` parent on purpose. The extension's key is
not a session, so ``core``'s ``require_full_app_access`` would 401 every call;
and the roster is meant to widen to EOS-only teams, whose overdue L10 To-Dos are
the whole point for them. What each person sees still follows their other
access: priorities need the copilot row, My To-Dos needs the todos row
(``backend/services/browser_glance.py``).

Nothing here calls a model. ``/glance`` reads what Cubie already made; ``/done``
reuses the very handlers the Cubie pages call, so every write-back (a promise
card's ✓ marks the promise kept, a To-Do's done clears its card) happens
exactly as it does on the page.
"""

from __future__ import annotations

import logging
import sys
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from backend.audit import record_audit
from backend.auth import _apply_access_gates, get_current_user, user_registry
from rubik_core.brand import PRODUCT_NAME
from rubik_core.browser_extension import connections
from rubik_core.config import settings
from rubik_core.control.repository import domain_belongs_to_workspace
from rubik_core.database.db import SessionLocal
from rubik_core.database.schemas import UserProfile
from rubik_core.features import feature_allows
from rubik_core.identifiers import AUDIT_VIA_ASSISTANT
from rubik_core.tenancy.context import current_workspace
from rubik_core.tenancy.platform_secrets import is_production_runtime

logger = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False)

_NOT_ON_ROSTER = f"{PRODUCT_NAME} for Chrome is not switched on for your team yet."
_RECONNECT = f"Connect {PRODUCT_NAME} for Chrome again from {PRODUCT_NAME}."


def _require_enabled() -> None:
    if not settings.CHROME_EXTENSION_ENABLED:
        raise HTTPException(status_code=404, detail="Not found.")


def require_chrome_extension(
    user: UserProfile = Depends(get_current_user),
) -> UserProfile:
    """The web app's gate — 404 when switched off, 403 when not on the roster."""
    _require_enabled()
    if not feature_allows("chrome_extension", user):
        raise HTTPException(status_code=403, detail=_NOT_ON_ROSTER)
    return user


def require_extension_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> UserProfile:
    """The extension's gate: its key → the person, then the same checks a
    session gets. 401 means "connect again"; the extension shows exactly that."""
    _require_enabled()
    raw = credentials.credentials if credentials else None
    if not connections.looks_like_token(raw):
        raise HTTPException(status_code=401, detail=_RECONNECT)
    with SessionLocal() as db:
        row = connections.resolve(db, raw)
        if row is None:
            raise HTTPException(status_code=401, detail=_RECONNECT)
        email, connection_id = row.user_email, row.id
        db.commit()  # the coarse last_used_at stamp
    try:
        user = user_registry.get_user(email)
    except Exception:
        logger.exception("browser-extension: user lookup failed for %s", email)
        raise HTTPException(
            status_code=500, detail=f"{PRODUCT_NAME} could not be reached."
        )
    if user is None:
        # Offboarded (the users row is gone) — the key is dead with them.
        raise HTTPException(status_code=401, detail=_RECONNECT)
    user = _apply_access_gates(user, email)
    if not feature_allows("chrome_extension", user):
        raise HTTPException(status_code=403, detail=_NOT_ON_ROSTER)
    request.state.browser_connection_id = connection_id
    return user


router = APIRouter(prefix="/browser-extension", tags=["browser-extension"])


# ── The web app: connect and disconnect browsers ─────────────────────────────


class ConnectIn(BaseModel):
    label: str | None = Field(default=None, max_length=connections.LABEL_MAX)
    #: The connection this browser held before, revoked as "replaced".
    replaces: str | None = Field(default=None, max_length=64)


def _verified_connection_domain(request: Request, user: UserProfile) -> str:
    """Use the hosted domain proven at sign-in, not the email's alias suffix."""
    claims = getattr(request.state, "session_claims", None)
    if claims is not None:
        if (
            claims.typ != "user"
            or claims.ws != current_workspace()
            or claims.sub != user.email.strip().lower()
        ):
            raise HTTPException(status_code=401, detail=_RECONNECT)
        domain = claims.hd
    elif not is_production_runtime() and (
        settings.ENABLE_MOCK_AUTH
        or ("pytest" in sys.modules and current_workspace() == "test")
    ):
        # Mock sign-in has no Google claims. This fallback is local/test only,
        # and still must match a currently bound workspace domain.
        domain = user.email.rsplit("@", 1)[-1] if "@" in user.email else None
    else:
        raise HTTPException(status_code=401, detail=_RECONNECT)
    if not domain or not domain_belongs_to_workspace(current_workspace(), domain):
        raise HTTPException(status_code=401, detail=_RECONNECT)
    return domain


@router.post("/connections")
def connect_browser(
    body: ConnectIn,
    request: Request,
    user: UserProfile = Depends(require_chrome_extension),
):
    """Mint this browser's key. The raw key is in this response and nowhere
    else; the Connect page hands it straight to the extension."""
    domain = _verified_connection_domain(request, user)
    with SessionLocal() as db:
        try:
            minted = connections.mint(
                db,
                user.email,
                hosted_domain=domain,
                label=body.label,
                replaces=body.replaces,
            )
        except PermissionError:
            raise HTTPException(status_code=401, detail=_RECONNECT)
        db.commit()
        public = connections.to_public(minted.connection)
        record_audit(
            db,
            actor_email=user.email,
            action="BROWSER_EXTENSION_CONNECT",
            target=public["id"],
            details={"label": public["label"], "replaces": body.replaces},
        )
    return {"token": minted.raw_token, "connection": public}


@router.get("/connections")
def list_connections(user: UserProfile = Depends(require_chrome_extension)):
    with SessionLocal() as db:
        rows = connections.list_live(db, user.email)
        return {"connections": [connections.to_public(r) for r in rows]}


@router.delete("/connections/{connection_id}")
def disconnect_browser(
    connection_id: str, user: UserProfile = Depends(require_chrome_extension)
):
    with SessionLocal() as db:
        if not connections.revoke(db, user.email, connection_id):
            raise HTTPException(
                status_code=404, detail="That browser is not connected."
            )
        db.commit()
        record_audit(
            db,
            actor_email=user.email,
            action="BROWSER_EXTENSION_DISCONNECT",
            target=connection_id,
            details={"via": AUDIT_VIA_ASSISTANT},
        )
    return {"ok": True}


# ── The extension ────────────────────────────────────────────────────────────


@router.get("/glance")
async def glance(user: UserProfile = Depends(require_extension_user)):
    """Everything the new tab and the badge show. A read; never a model call."""
    from backend.services.browser_glance import build_glance

    return await run_in_threadpool(build_glance, user)


class DoneIn(BaseModel):
    kind: Literal["eos_todo", "promise", "priority"]
    id: str = Field(min_length=1, max_length=200)


def _eos_todo_done(user: UserProfile, todo_id: str) -> None:
    from backend.models.eos.traction import TodoUpdate
    from backend.routers.eos.todos import update_todo
    from rubik_core.database.eos._base import TodoStatus
    from rubik_core.promises.readthrough import eos_todos_for

    try:
        uid = UUID(todo_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="That To-Do is not yours.")
    with SessionLocal() as db:
        # Only the person's OWN open To-Dos. ``update_todo``'s own check would
        # also let a manager close a report's To-Do, which a new tab that only
        # ever lists your own must not do.
        if todo_id not in {t["id"] for t in eos_todos_for(db, user.email)}:
            raise HTTPException(status_code=404, detail="That To-Do is not yours.")
        update_todo(uid, db, user, TodoUpdate(status=TodoStatus.DONE))


@router.post("/done")
async def mark_done(body: DoneIn, user: UserProfile = Depends(require_extension_user)):
    """✓ from the new tab — the same handler the Cubie page calls for that kind."""
    if body.kind == "eos_todo":
        from rubik_core.control.repository import module_enabled

        if not await run_in_threadpool(module_enabled, "eos"):
            raise HTTPException(status_code=404, detail="EOS is not enabled")
        await run_in_threadpool(_eos_todo_done, user, body.id)
    elif body.kind == "promise":
        if not (settings.TODOS_ENABLED and feature_allows("todos", user)):
            raise HTTPException(status_code=403, detail="My To-Dos is not on for you.")
        from backend.routers.promises import kept

        with SessionLocal() as db:
            await kept(body.id, user=user, db=db)
    else:
        if not feature_allows("copilot", user):
            raise HTTPException(
                status_code=403, detail="Priorities are not on for you."
            )
        from backend.routers.home import resolve_priority

        # 404 when this morning's card was regenerated since the tab loaded;
        # the extension refreshes and the item is simply gone or renamed.
        await resolve_priority(body.id, user=user)
    return {"ok": True}


@router.post("/disconnect")
def disconnect_self(
    request: Request, user: UserProfile = Depends(require_extension_user)
):
    """ "Sign out" in the extension: kill this browser's own key."""
    connection_id = request.state.browser_connection_id
    with SessionLocal() as db:
        connections.revoke(db, user.email, connection_id)
        db.commit()
        record_audit(
            db,
            actor_email=user.email,
            action="BROWSER_EXTENSION_DISCONNECT",
            target=connection_id,
            details={"via": "extension"},
        )
    return {"ok": True}

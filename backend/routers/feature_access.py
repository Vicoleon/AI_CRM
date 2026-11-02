"""Admin → Feature Access: onboard a department to a feature without a deploy.

Every department-scoped gate resolves through ``rubik_core.features``; this
router is the maintenance page behind it. It lists each registered feature
with the roster it ships with, the roster in force, whether that roster is an
override, the env kill switch (read-only), and WHO the roster admits today —
so an admin sees "opening X-Ray to Marketing adds these six people" before
saving, rather than discovering it from a support ticket.

Mounted ungated by the copilot pilot (admins are pilot members anyway) and
gated per endpoint with ``admin_required``, like the other admin-family
routers. Every write is audited.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.audit import record_audit
from backend.auth import admin_required, user_registry
from backend.routers.departments import dept_manager
from rubik_core.auth.registry import UserProfile
from rubik_core.database.db import get_db
from rubik_core.database.models import FeatureAccess
from rubik_core.features import (
    FEATURES,
    FeaturePolicy,
    effective_policy,
    feature_by_key,
    reset_policy,
    save_policy,
    user_allowed,
)
from rubik_core.features.policy import kill_switch_on

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/feature-access", tags=["admin", "feature-access"])

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class PolicyIn(BaseModel):
    """The editable half of a feature's roster."""

    # Department LABELS or slugs — slugified server-side, so the page can send
    # what Admin → Departments shows.
    departments: list[str] = Field(default_factory=list)
    extra_emails: list[str] = Field(default_factory=list)
    exec_allowed: bool = False
    admin_allowed: bool = True
    everyone: bool = False
    note: str = Field(default="", max_length=500)


class MemberOut(BaseModel):
    email: str
    name: str
    department: str | None = None
    # How they qualify — the first door that admits them, so an admin can see
    # WHY a name is on the list.
    via: str


def _policy_from_input(body: PolicyIn) -> FeaturePolicy:
    bad = [e for e in body.extra_emails if e.strip() and not _EMAIL_RE.match(e.strip())]
    if bad:
        raise HTTPException(
            status_code=422, detail=f"Not an email address: {', '.join(bad)}"
        )
    return FeaturePolicy.from_dict(body.model_dump())


def _via(spec, policy: FeaturePolicy, user: UserProfile) -> str | None:
    """Which door admits ``user`` — mirrors the order in ``user_allowed``."""
    if not user_allowed(spec, policy, user):
        return None
    if policy.admin_allowed and user.is_admin:
        return "admin"
    if policy.exec_allowed and bool(getattr(user, "is_exec", False)):
        return "exec"
    if (user.email or "").strip().lower() in policy.extra_emails:
        return "named"
    if policy.everyone:
        return "everyone"
    return "managed team" if spec.membership == "managed_only" else "department"


def _members(spec, policy: FeaturePolicy, users: list[UserProfile]) -> list[MemberOut]:
    out: list[MemberOut] = []
    for u in users:
        email = (u.email or "").strip().lower()
        # The admin impersonation pseudo-users are not people.
        if not email or email.endswith(".local"):
            continue
        via = _via(spec, policy, u)
        if via is None:
            continue
        out.append(
            MemberOut(email=u.email, name=u.name, department=u.department, via=via)
        )
    out.sort(key=lambda m: (m.via, m.name.lower()))
    return out


def _serialize(spec, db: Session, users: list[UserProfile]) -> dict[str, Any]:
    policy, customized = effective_policy(spec.key, db)
    row = db.get(FeatureAccess, spec.key) if customized else None
    return {
        "key": spec.key,
        "label": spec.label,
        "description": spec.description,
        "surface": spec.surface,
        "group": spec.group,
        "module": spec.module,
        "notes": spec.notes,
        "membership": spec.membership,
        "kill_switch": (
            {"name": spec.kill_switch, "enabled": kill_switch_on(spec)}
            if spec.kill_switch
            else None
        ),
        "default": spec.default().to_dict(),
        "policy": policy.to_dict(),
        "customized": customized,
        "note": (row.note if row is not None else None),
        "updated_by": (row.updated_by if row is not None else None),
        "updated_at": (
            row.updated_at.isoformat() if row is not None and row.updated_at else None
        ),
        "members": [m.model_dump() for m in _members(spec, policy, users)],
    }


def _real_users() -> list[UserProfile]:
    try:
        return user_registry.list_users()
    except Exception:  # noqa: BLE001 — the roster editor must still render
        logger.warning("feature-access: could not list users for the member preview")
        return []


@router.get("")
async def list_features(
    admin: UserProfile = Depends(admin_required), db: Session = Depends(get_db)
):
    """Every registered feature with its live roster and who it admits today."""
    users = _real_users()
    return {
        "features": [_serialize(spec, db, users) for spec in FEATURES],
        # Live department labels for the picker — the same list Admin →
        # Departments manages, so nobody types a slug by hand.
        "departments": sorted(dept_manager.get_departments(), key=str.lower),
    }


@router.post("/{key}/preview")
async def preview_policy(
    key: str,
    body: PolicyIn,
    admin: UserProfile = Depends(admin_required),
):
    """Who WOULD have access under ``body`` — a what-if, nothing is saved."""
    try:
        spec = feature_by_key(key)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None
    policy = _policy_from_input(body)
    return {
        "key": key,
        "policy": policy.to_dict(),
        "members": [m.model_dump() for m in _members(spec, policy, _real_users())],
    }


@router.put("/{key}")
async def update_policy(
    key: str,
    body: PolicyIn,
    admin: UserProfile = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Replace the live roster for ``key``. Audited as FEATURE_ACCESS_UPDATE."""
    try:
        spec = feature_by_key(key)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None
    before, _ = effective_policy(key, db)
    policy = _policy_from_input(body)
    save_policy(db, key, policy, actor_email=admin.email, note=body.note)
    record_audit(
        db,
        actor_email=admin.email,
        action="FEATURE_ACCESS_UPDATE",
        target=key,
        details={
            "before": before.to_dict(),
            "after": policy.to_dict(),
            "note": body.note or None,
        },
    )
    return _serialize(spec, db, _real_users())


@router.delete("/{key}")
async def reset_to_default(
    key: str,
    admin: UserProfile = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Drop the override so ``key`` runs on the roster it shipped with."""
    try:
        spec = feature_by_key(key)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None
    before, customized = effective_policy(key, db)
    reset_policy(db, key)
    if customized:
        record_audit(
            db,
            actor_email=admin.email,
            action="FEATURE_ACCESS_RESET",
            target=key,
            details={"before": before.to_dict(), "after": spec.default().to_dict()},
        )
    return _serialize(spec, db, _real_users())

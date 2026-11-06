"""Admin endpoints for the sales-feature models.

GET  /api/admin/sales/config  — current per-role model config
PUT  /api/admin/sales/config  — update it (runtime model swap; no redeploy)
GET  /api/admin/sales/catalog — the roles, their GA-safe choices and defaults

All endpoints require the ``admin`` role via :func:`admin_required`. Mirrors
``chat_admin.py`` but multi-role, because the sales features deliberately split
across tiers (cheap extractors, expensive reasoning) and one id for all of them
would either cost a fortune or gut the output.

The PUT validates each ``model_id`` against that role's curated choices, so an
admin can't pin a starved ``*-preview`` id, a 404 id, or — for the X-Ray web
agent — a model that can't do Google Search grounding.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.auth import admin_required
from rubik_core.auth.registry import UserProfile
from rubik_core.sales.model_config import (
    SALES_MODEL_CATALOG,
    SALES_MODEL_ROLES,
    allowed_models_for,
    default_model_for,
    load_settings_blob,
    save_settings_blob,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/sales", tags=["admin", "sales"])


class SalesRoleSpec(BaseModel):
    key: str
    label: str
    group: str
    description: str
    tier: str
    default_model: str
    models: list[str]
    #: Why ``models`` is narrower than the full catalog, if it is.
    choices_note: str = ""


class SalesCatalogResponse(BaseModel):
    roles: list[SalesRoleSpec]
    #: Every id in play across all roles — handy for a legend in the UI.
    all_models: list[str]


@router.get("/catalog", response_model=SalesCatalogResponse)
def get_sales_catalog(_: UserProfile = Depends(admin_required)) -> SalesCatalogResponse:
    """The tunable roles, each with its GA-safe choices and config.py fallback."""
    return SalesCatalogResponse(
        roles=[
            SalesRoleSpec(
                key=role.key,
                label=role.label,
                group=role.group,
                description=role.description,
                tier=role.tier,
                default_model=default_model_for(role.key),
                models=list(allowed_models_for(role.key)),
                choices_note=role.choices_note,
            )
            for role in SALES_MODEL_ROLES
        ],
        all_models=list(SALES_MODEL_CATALOG),
    )


@router.get("/config")
def get_sales_config(_: UserProfile = Depends(admin_required)) -> dict[str, Any]:
    """Return the persisted per-role model config (defaults merged in)."""
    return load_settings_blob()


@router.put("/config")
def put_sales_config(
    payload: dict[str, Any],
    user: UserProfile = Depends(admin_required),
) -> dict[str, Any]:
    """Update sales model routing. Takes effect on the next run (no redeploy)."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")

    roles = payload.get("roles")
    if roles is not None:
        if not isinstance(roles, dict):
            raise HTTPException(status_code=400, detail="roles must be an object")
        known = {role.key for role in SALES_MODEL_ROLES}
        for role_key, entry in roles.items():
            if role_key not in known:
                raise HTTPException(
                    status_code=400, detail=f"unknown sales role '{role_key}'"
                )
            if not isinstance(entry, dict):
                raise HTTPException(
                    status_code=400, detail=f"roles.{role_key} must be an object"
                )
            model_id = entry.get("model_id")
            if model_id is None:
                continue
            allowed = allowed_models_for(role_key)
            if model_id not in allowed:
                raise HTTPException(
                    status_code=400,
                    detail=f"roles.{role_key}.model_id must be one of {list(allowed)}",
                )

    return save_settings_blob(payload, actor_email=user.email)

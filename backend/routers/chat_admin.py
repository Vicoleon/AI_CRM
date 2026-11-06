"""Admin endpoints for the chat-coordinator model.

GET  /api/admin/chat/config  — current chat model config
PUT  /api/admin/chat/config  — update it (runtime model swap; no redeploy)
GET  /api/admin/chat/catalog — GA-safe model choices + the current default

All endpoints require the ``admin`` role via :func:`admin_required`. Mirrors the
AI-Priorities admin store (``backend/routers/priorities_admin.py``) but for the
single chat coordinator. The PUT validates ``model_id`` against the curated
GA-safe catalog so an admin can't accidentally pin a starved ``*-preview`` id.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.auth import admin_required
from rubik_core.agent_runtime.chat_config import (
    CHAT_MODEL_CATALOG,
    load_settings_blob,
    save_settings_blob,
)
from rubik_core.auth.registry import UserProfile
from rubik_core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/chat", tags=["admin", "chat"])


class ChatCatalogResponse(BaseModel):
    models: list[str]
    default_model: str


@router.get("/catalog", response_model=ChatCatalogResponse)
def get_chat_catalog(_: UserProfile = Depends(admin_required)) -> ChatCatalogResponse:
    """GA-safe model choices for the dropdown + the config.py fallback."""
    return ChatCatalogResponse(
        models=list(CHAT_MODEL_CATALOG),
        default_model=settings.VERTEX_LLM_MODEL_FLASH,
    )


@router.get("/config")
def get_chat_config(_: UserProfile = Depends(admin_required)) -> dict[str, Any]:
    """Return the persisted chat model config (defaults merged in)."""
    return load_settings_blob()


@router.put("/config")
def put_chat_config(
    payload: dict[str, Any],
    user: UserProfile = Depends(admin_required),
) -> dict[str, Any]:
    """Update the chat model. Takes effect on the next chat turn (no redeploy).

    ``model_id`` must be one of the curated GA-safe ids — this is the guardrail
    that stops an admin from re-introducing a quota-starved ``*-preview`` model.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")

    model_id = payload.get("model_id")
    if model_id is not None and model_id not in CHAT_MODEL_CATALOG:
        raise HTTPException(
            status_code=400,
            detail=f"model_id must be one of {list(CHAT_MODEL_CATALOG)}",
        )

    return save_settings_blob(payload, actor_email=user.email)

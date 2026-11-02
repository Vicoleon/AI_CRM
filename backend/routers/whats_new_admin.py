"""Admin → What's New: the morning post's switch and channel, and the tease.

GET /api/admin/whats-new/settings  — the settings in force
PUT /api/admin/whats-new/settings  — ``{"slack_enabled", "channel",
                                       "tease_enabled"}``

Spec §4.6.1: each workspace opts into a digest of new Cubie release notes;
this is that switch (Phase 3 moves the post onto the messaging connector).
Mounted like ``loose_ends_admin`` (``app.include_router(..., prefix="/api")``,
not on ``core``), admin-only per endpoint through :func:`admin_required`.
Admin-only by ROLE, so there is no Feature Access key: these are settings, not
a roster. Not behind ``WHATS_NEW_ENABLED`` either, so the channel can be set
before the feature is switched on.

The checks live in ``rubik_core.whats_new.settings.save`` (a channel Slack
knows, the bot in it before posting is switched on). Its ``ValueError``
messages are written for the admin and are returned as the 400 ``detail`` as
they are. Every save is audited. A body from an older client that still sends
approvers is accepted; the extra fields are ignored.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.audit import record_audit
from backend.auth import admin_required
from rubik_core.brand import PRODUCT_NAME
from rubik_core.database.db import get_db
from rubik_core.database.schemas import UserProfile
from rubik_core.whats_new import settings as wn_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/whats-new", tags=["admin", "whats-new"])

AUDIT_ACTION = "WHATS_NEW_SETTINGS"


class SettingsIn(BaseModel):
    slack_enabled: bool
    #: As typed: "#announcements", "announcements" or an id.
    channel: str = Field("", max_length=120)
    tease_enabled: bool


def _view(config: dict[str, Any]) -> dict[str, Any]:
    """The settings as the Admin tab reads them."""
    return {
        "slack_enabled": bool(config.get("slack_enabled")),
        "channel_name": config.get("channel_name") or wn_settings.DEFAULT_CHANNEL,
        "channel_id": config.get("channel_id"),
        "bot_in_channel": config.get("bot_in_channel"),
        "channel_archived": bool(config.get("channel_archived")),
        "tease_enabled": bool(config.get("tease_enabled")),
        "updated_by": config.get("updated_by"),
        "updated_at": config.get("updated_at"),
    }


@router.get("/settings")
def get_settings(
    _: UserProfile = Depends(admin_required),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """The channel and whether the bot is in it, and the two switches."""
    return _view(wn_settings.load(db))


@router.put("/settings")
def put_settings(
    payload: SettingsIn,
    user: UserProfile = Depends(admin_required),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Save the channel and the switches. Takes effect on the next request; no
    redeploy. Every save asks Slack again, so saving the same values is how
    the tab re-checks the bot after an invite."""
    try:
        saved = wn_settings.save(
            db,
            slack_enabled=payload.slack_enabled,
            channel=payload.channel,
            tease_enabled=payload.tease_enabled,
            actor_email=user.email or "",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — never echo database internals
        logger.exception("whats-new admin: settings save failed")
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"{PRODUCT_NAME} could not save the What's New settings. Please try again.",
        ) from exc

    record_audit(
        db,
        actor_email=(user.email or "").strip().lower(),
        action=AUDIT_ACTION,
        target=wn_settings.SETTINGS_KEY,
        details={
            "slack_enabled": saved["slack_enabled"],
            "channel_name": saved["channel_name"],
            "channel_id": saved["channel_id"],
            "tease_enabled": saved["tease_enabled"],
        },
    )
    return _view(saved)

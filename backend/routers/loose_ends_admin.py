"""Admin → Loose Ends: the daily Slack post's on/off switch and its channel.

GET  /api/admin/loose-ends/digest          — the settings in force + the last post
PUT  /api/admin/loose-ends/digest          — ``{"enabled": bool, "channel": str}``
POST /api/admin/loose-ends/digest/preview  — today's post, rendered, not sent

Decision: the post goes to ``#sales`` but ships OFF, and a
Cubie admin switches it on and off and changes the channel here — no env var,
no redeploy. Contract: ``docs/loose_ends/README.md`` §5 ("Admin"). Mounted like
``sales_admin`` (``app.include_router(..., prefix="/api")``, not on ``core``),
admin-only per endpoint through :func:`admin_required`. Admin-only by ROLE, so
there is no Feature Access key: this is a setting, not a roster.

The checks live in ``rubik_core.loop_closure.digest_settings.save`` (a channel
Slack knows; switching on needs the Cubie bot in it). Its ``ValueError``
messages are written for the admin and are returned as the 400 ``detail`` as
they are. Every save is audited with the switch and the channel only.

The preview is metadata only by construction — it is the digest's own builder
(names, counts by kind, ages; never a subject) — and it neither posts nor
claims the day.
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
from rubik_core.config import settings
from rubik_core.database.db import get_db
from rubik_core.database.schemas import UserProfile
from rubik_core.loop_closure import digest, digest_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/loose-ends", tags=["admin", "loose-ends"])

AUDIT_ACTION = "LOOSE_ENDS_DIGEST_SETTINGS"


class DigestSettingsIn(BaseModel):
    enabled: bool
    channel: str = Field(..., max_length=120)


def _view(db: Session, config: dict[str, Any]) -> dict[str, Any]:
    """The settings as the Admin card reads them, plus the last post and
    whether the scan itself is switched on here (an "On" post with the scan
    off would never go out). ``channel_archived`` is True once Slack said the
    channel is archived — the card then asks for another channel."""
    try:
        last = digest.last_post(db, config)
    except Exception:  # noqa: BLE001 — the card still renders without it
        logger.exception("loose-ends admin: could not read the last digest post")
        db.rollback()
        last = None
    return {
        "enabled": bool(config.get("enabled")),
        "channel_name": config.get("channel_name"),
        "channel_id": config.get("channel_id"),
        "bot_in_channel": config.get("bot_in_channel"),
        "channel_archived": bool(config.get("channel_archived")),
        "updated_by": config.get("updated_by"),
        "updated_at": config.get("updated_at"),
        "scan_enabled": bool(getattr(settings, "LOOP_CLOSURE_ENABLED", False)),
        "last_post": last,
    }


@router.get("/digest")
def get_digest_settings(
    _: UserProfile = Depends(admin_required),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Whether the daily post is on, its channel, the bot check and the last post."""
    return _view(db, digest_settings.load(db))


@router.put("/digest")
def put_digest_settings(
    payload: DigestSettingsIn,
    user: UserProfile = Depends(admin_required),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Turn the post on or off and set its channel. Takes effect on the next
    morning's scan; no redeploy. Every save asks Slack again, so saving the
    same values is how the card re-checks the bot after an invite."""
    try:
        saved = digest_settings.save(
            db,
            enabled=payload.enabled,
            channel=payload.channel,
            actor_email=user.email or "",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — never echo database internals
        logger.exception("loose-ends admin: digest settings save failed")
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"{PRODUCT_NAME} could not save the Loose Ends post settings. Please try again.",
        ) from exc

    record_audit(
        db,
        actor_email=(user.email or "").strip().lower(),
        action=AUDIT_ACTION,
        target=digest_settings.SETTINGS_KEY,
        details={
            "enabled": saved["enabled"],
            "channel_name": saved["channel_name"],
            "channel_id": saved["channel_id"],
        },
    )
    return _view(db, saved)


@router.post("/digest/preview")
def preview_digest(
    _: UserProfile = Depends(admin_required),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Today's post as it would read now: ``{"text", "would_post", "reason"}``.
    Posts nothing and claims nothing."""
    try:
        return digest.render_today(db)
    except Exception as exc:  # noqa: BLE001
        logger.exception("loose-ends admin: digest preview failed")
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"{PRODUCT_NAME} could not build today's post just now. Please try again.",
        ) from exc

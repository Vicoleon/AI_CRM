"""Admin-managed chat-coordinator model config.

Stored in the existing ``workspace_settings`` table under the key
``chat_model_config`` (mirrors the AI-Priorities settings store). Lets an
admin swap the model the chat coordinator runs on **at runtime — no redeploy**.

This is operational insurance against a quota incident: when a
model id 429s (a starved ``*-preview`` alias) or Google deprecates/rotates one,
an admin flips a dropdown in Admin → Chat instead of waiting on a code deploy.

The orchestrator calls :func:`load_chat_config` when it builds the per-request
coordinator; the admin endpoints (``backend/routers/chat_admin.py``) read/write
the same JSON blob. Everything falls back to ``settings.VERTEX_LLM_MODEL_FLASH``
so chat never breaks if the row is missing or the DB read fails.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from typing import Any

from rubik_core.config import settings
from rubik_core.database.db import SessionLocal
from rubik_core.database.models import WorkspaceSetting

logger = logging.getLogger(__name__)

SETTINGS_KEY = "chat_model_config"
CURRENT_SCHEMA_VERSION = 1

# GA-safe servable Vertex model ids. Preview ids are deliberately EXCLUDED —
# they have no dedicated per-project quota bucket and 429 under light load (see
# the comment block in ``rubik_core/config.py``). The ``*-latest`` aliases
# auto-follow Google's newest GA model for that tier.
#
# 2026-07-21: retired Gemini 2.5 (gemini-2.5-flash / gemini-2.5-pro) from the
# chat options and added Gemini 3.6 Flash + Gemini 3.5 Flash-Lite. Both are
# documented as available on Vertex AI (Google Cloud model docs, 2026-07-21)
# under these exact ids — they are NOT ``*-preview`` ids, so they draw on GA
# quota rather than the starved shared-quota bucket. All Gemini 3.x require the
# *global* endpoint, which ``VERTEX_LLM_LOCATION`` already defaults to.
#
# 2026-08-12: all five probed live (``/api/health/models`` + a direct
# 2026-09-08: added gemini-3.8-flash, now the FLASH tier and the default here.
# Probed 200/ON_DEMAND on Vertex global before listing it (see config.py). Note
# 3.7+ rejects thinking_level="minimal" with a hard 400, so picking a 3.8 id
# while CHAT_LLM_THINKING_LEVEL is "minimal" would break every turn —
# gemini_tuning floors that rather than sending it. gemini-3.6-flash is kept as
# a roll-back option; there is no 3.8 Flash-Lite or 3.8 Pro to list (both 404).
# "gemini-flash-latest" was measured still serving Gemini **2.5** Flash —
# it 400s on thinking_level — so it is kept only as a roll-back escape hatch,
# not as a modern option; picking it silently moves chat back a generation.
#
# 2026-09-17: added gemini-3.7-flash. Probed 200/ON_DEMAND, then 5/5 on a real
# multi-paragraph workload (a bare ping proves an id exists, not that it serves
# a turn — one grounded call did 429 once and passed 5/5 on retry, i.e. the
# documented transient blip CHAT_LLM_RETRY_ATTEMPTS already covers, not a
# starved preview pool). It earns a slot because it is the only roll-back that
# does NOT need a second coordinated change: 3.7 and 3.8 share the re-based
# thinking scale, so CHAT_LLM_THINKING_LEVEL="medium" keeps its meaning. Rolling
# back to 3.6 silently doubles the thinking level (see config.py) and needs the
# level rolled back in the same change; dropping to 3.7 does not.
#
# Same sweep: gemini-3.9/4.0-flash and every 3.x Flash-Lite above 3.5 404, so
# there is nothing newer to list. gemini-3.5-flash 429s and is deliberately
# still absent.
CHAT_MODEL_CATALOG: tuple[str, ...] = (
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-flash-latest",
)


@dataclass
class ChatModelConfig:
    """Resolved chat-coordinator config for one request build."""

    model_id: str


def _default_blob() -> dict[str, Any]:
    return {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "model_id": settings.VERTEX_LLM_MODEL_FLASH,
    }


def load_settings_blob() -> dict[str, Any]:
    """Read the stored blob, merged onto defaults. Never raises."""
    blob = _default_blob()
    try:
        with SessionLocal() as db:
            row = (
                db.query(WorkspaceSetting)
                .filter(WorkspaceSetting.key == SETTINGS_KEY)
                .first()
            )
            if row and isinstance(row.value, dict):
                _deep_merge(blob, row.value)
    except Exception:  # noqa: BLE001 — a DB hiccup must never break chat
        logger.exception("Failed to load chat model settings, using defaults")
    return blob


def save_settings_blob(
    payload: dict[str, Any], *, actor_email: str | None = None
) -> dict[str, Any]:
    """Persist the chat model config under SETTINGS_KEY (upsert)."""
    blob = _default_blob()
    _deep_merge(blob, payload)
    blob["schema_version"] = CURRENT_SCHEMA_VERSION

    with SessionLocal() as db:
        row = (
            db.query(WorkspaceSetting)
            .filter(WorkspaceSetting.key == SETTINGS_KEY)
            .first()
        )
        if row is None:
            row = WorkspaceSetting(key=SETTINGS_KEY, value=blob)
            db.add(row)
        else:
            row.value = blob
        db.commit()

    logger.info("chat model settings updated by %s", actor_email or "(unknown)")
    return blob


def load_chat_config() -> ChatModelConfig:
    """Hydrate the chat-coordinator config from the stored blob.

    Falls back to ``settings.VERTEX_LLM_MODEL_FLASH`` if unset, so a fresh
    install (no row yet) behaves exactly like the hardcoded default.
    """
    blob = load_settings_blob()
    model_id = blob.get("model_id") or settings.VERTEX_LLM_MODEL_FLASH
    return ChatModelConfig(model_id=model_id)


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> None:
    """In-place: recursively merge ``overlay`` into ``base``."""
    for key, value in overlay.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)

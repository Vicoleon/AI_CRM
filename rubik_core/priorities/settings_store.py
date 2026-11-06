"""Admin-managed agent configuration store for the UC-20 pipeline.

Settings live in the existing ``workspace_settings`` table under the
key ``priorities_agent_config``. A single JSON row holds the per-role
model + temperature + revision policy + per-provider enabled state. API keys
and their references belong only to the workspace's control connector.

The orchestrator calls :func:`load_priorities_config` to hydrate a
:class:`PrioritiesConfig` from the DB at construction time; the admin
endpoint reads/writes the same JSON blob.

Schema (versioned via the ``schema_version`` key so future migrations
can detect older payloads):

.. code-block:: json

    {
      "schema_version": 1,
      "agents": {
        "gmail_agent":    {"provider": "vertex", "model_id": "gemini-flash", "temperature": 0.3},
        "calendar_agent": {"provider": "vertex", "model_id": "gemini-flash", "temperature": 0.3},
        "drive_agent":    {"provider": "vertex", "model_id": "gemini-flash", "temperature": 0.3},
        "meet_agent":     {"provider": "vertex", "model_id": "gemini-flash", "temperature": 0.3},
        "summarizer":     {"provider": "vertex", "model_id": "gemini-pro",   "temperature": 0.45},
        "decision_maker": {"provider": "vertex", "model_id": "gemini-pro",   "temperature": 0.55},
        "critic":         {"provider": "vertex", "model_id": "gemini-pro",   "temperature": 0.25}
      },
      "policy": {"revision_threshold": 6.0, "max_revisions": 1},
      "providers": {
        "vertex":    {"enabled": true},
        "anthropic": {"enabled": false},
        "openai":    {"enabled": false},
        "deepseek":  {"enabled": false}
      }
    }
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable
from typing import Any

from rubik_core.config import settings
from rubik_core.database.db import SessionLocal
from rubik_core.database.models import WorkspaceSetting
from rubik_core.priorities.config import PrioritiesConfig

logger = logging.getLogger(__name__)

SETTINGS_KEY = "priorities_agent_config"
CURRENT_SCHEMA_VERSION = 1


class SettingsReadError(RuntimeError):
    """The admin update could not safely read the current settings row."""


# Order matters: this is what the admin UI iterates over. New roles append.
AGENT_ROLES = (
    "gmail_agent",
    "calendar_agent",
    "drive_agent",
    "meet_agent",
    "hubspot_agent",
    "eos_agent",
    "slack_agent",
    "summarizer",
    "decision_maker",
    "critic",
)

# Roles that pull data from a single channel — used by the UI to group them
# under "Channel agents" vs "Pipeline agents".
CHANNEL_AGENT_ROLES = (
    "gmail_agent",
    "calendar_agent",
    "drive_agent",
    "meet_agent",
    "hubspot_agent",
    "eos_agent",
    "slack_agent",
)
PIPELINE_AGENT_ROLES = ("summarizer", "decision_maker", "critic")

SUPPORTED_PROVIDERS = ("vertex", "anthropic", "openai", "deepseek")

# Available models the admin UI offers per provider. Vertex pulls live from
# settings.VERTEX_LLM_MODEL_* via the dropdown but we keep friendly aliases
# here for the dropdown values.
PROVIDER_MODEL_CATALOG: dict[str, list[str]] = {
    "vertex": [
        "gemini-flash-lite",
        "gemini-flash",
        "gemini-pro",
        # Gemini 4 Argon at high thinking, the one way it is offered: at the
        # level it would otherwise inherit it thought 0 tokens on the summary,
        # ranking and review steps (replayed on real inputs). See config.py.
        f"{settings.VERTEX_LLM_MODEL_ARGON}@high",
    ],
    "anthropic": [
        "claude-haiku-4-5",
        "claude-sonnet-4-6",
        "claude-opus-4-5",
    ],
    "openai": [
        "gpt-4o-mini",
        "gpt-4o",
        "o3-mini",
    ],
    "deepseek": [
        "deepseek-chat",
        "deepseek-reasoner",
    ],
}


# ── Defaults (mirror PrioritiesConfig dataclass defaults) ────────────────────


def _default_config_blob() -> dict[str, Any]:
    cfg = PrioritiesConfig.default()
    return {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "agents": {
            role: {
                "provider": "vertex",
                "model_id": getattr(cfg.models, role),
                "temperature": getattr(cfg.temperatures, role),
            }
            for role in AGENT_ROLES
        },
        "policy": {
            "revision_threshold": cfg.revision_threshold,
            "max_revisions": cfg.max_revisions,
        },
        "providers": {
            provider: {"enabled": provider == "vertex"}
            for provider in SUPPORTED_PROVIDERS
        },
    }


# ── DB I/O ───────────────────────────────────────────────────────────────────


def load_settings_blob() -> dict[str, Any]:
    """Read the stored config; return defaults merged in where keys are missing.

    Returning a fully-populated blob (rather than partial) keeps the admin
    UI simple — it always renders every role.
    """
    blob = _default_config_blob()
    try:
        with SessionLocal() as db:
            row = (
                db.query(WorkspaceSetting)
                .filter(WorkspaceSetting.key == SETTINGS_KEY)
                .first()
            )
            if row and isinstance(row.value, dict):
                _deep_merge(blob, row.value)
    except Exception:  # noqa: BLE001
        # DB hiccups must not break the pipeline — fall back to defaults.
        logger.exception("Failed to load priorities settings, using defaults")
    return blob


def load_settings_blob_strict() -> dict[str, Any]:
    """Read admin settings or fail before a credential update can start."""
    try:
        with SessionLocal() as db:
            row = (
                db.query(WorkspaceSetting)
                .filter(WorkspaceSetting.key == SETTINGS_KEY)
                .first()
            )
            blob = _default_config_blob()
            if row is not None:
                if not isinstance(row.value, dict):
                    raise ValueError("Priorities settings must be an object")
                _deep_merge(blob, row.value)
            _validate_config_shape(blob)
            return blob
    except Exception as exc:
        raise SettingsReadError("Failed to read priorities settings") from exc


def save_settings_blob(
    payload: dict[str, Any], *, actor_email: str | None = None
) -> dict[str, Any]:
    """Persist the config under SETTINGS_KEY (upsert)."""
    blob = _default_config_blob()
    _deep_merge(blob, payload)
    _strip_provider_credentials(blob)
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

    logger.info("priorities settings updated by %s", actor_email or "(unknown)")
    return blob


def update_settings_blob(
    edit: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    actor_email: str | None = None,
) -> dict[str, Any]:
    """Read, edit and save one settings row in a single transaction.

    Locking the row keeps concurrent admin edits from losing unrelated config.
    ``edit`` must be a pure in-memory operation:
    external secret writes run after a strict preflight read and before this
    short transaction, so no network I/O holds the row lock.
    """
    read_complete = False
    try:
        with SessionLocal() as db:
            row = (
                db.query(WorkspaceSetting)
                .filter(WorkspaceSetting.key == SETTINGS_KEY)
                .with_for_update()
                .first()
            )
            current = _default_config_blob()
            if row is not None:
                if not isinstance(row.value, dict):
                    raise ValueError("Priorities settings must be an object")
                _deep_merge(current, row.value)
            read_complete = True

            blob = _default_config_blob()
            _deep_merge(blob, edit(current))
            _strip_provider_credentials(blob)
            blob["schema_version"] = CURRENT_SCHEMA_VERSION
            if row is None:
                db.add(WorkspaceSetting(key=SETTINGS_KEY, value=blob))
            else:
                row.value = blob
            db.commit()
    except Exception as exc:
        if not read_complete:
            raise SettingsReadError("Failed to read priorities settings") from exc
        raise

    logger.info("priorities settings updated by %s", actor_email or "(unknown)")
    return blob


def load_priorities_config() -> PrioritiesConfig:
    """Hydrate a :class:`PrioritiesConfig` from the stored settings blob.

    Used by the orchestrator construction path so every run picks up the
    latest admin-saved values without a process restart.
    """
    blob = load_settings_blob()
    cfg = PrioritiesConfig.default()

    agents = blob.get("agents", {})
    for role in AGENT_ROLES:
        entry = agents.get(role) or {}
        model_id = entry.get("model_id")
        temperature = entry.get("temperature")
        if model_id:
            setattr(cfg.models, role, model_id)  # type: ignore[arg-type]
        if isinstance(temperature, (int, float)):
            setattr(cfg.temperatures, role, float(temperature))  # type: ignore[arg-type]

    policy = blob.get("policy") or {}
    if isinstance(policy.get("revision_threshold"), (int, float)):
        cfg.revision_threshold = float(policy["revision_threshold"])
    if isinstance(policy.get("max_revisions"), int):
        cfg.max_revisions = max(0, int(policy["max_revisions"]))

    return cfg


# ── Helpers ──────────────────────────────────────────────────────────────────


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> None:
    """In-place: recursively merge ``overlay`` into ``base``."""
    for key, value in overlay.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)


def _validate_config_shape(blob: dict[str, Any]) -> None:
    for key in ("agents", "policy", "providers"):
        if not isinstance(blob.get(key), dict):
            raise ValueError(f"Priorities {key} must be an object")
    for key in ("agents", "providers"):
        if any(not isinstance(entry, dict) for entry in blob[key].values()):
            raise ValueError(f"Priorities {key} entries must be objects")


def _strip_provider_credentials(blob: dict[str, Any]) -> None:
    """Keep legacy or client-supplied credential metadata out of tenant rows."""
    for entry in (blob.get("providers") or {}).values():
        if isinstance(entry, dict):
            for key in (
                "api_key_secret_ref",
                "api_key",
                "api_key_configured",
                "clear_api_key",
            ):
                entry.pop(key, None)


def redact_secrets(blob: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``blob`` safe to ship to the frontend.

    The frontend never needs raw secret-manager refs — it only needs to
    know whether a provider has a key configured.
    """
    out = copy.deepcopy(blob)
    for _provider, entry in (out.get("providers") or {}).items():
        if not isinstance(entry, dict):
            continue
        entry.pop("api_key_secret_ref", None)
        entry.pop("api_key", None)
        entry["api_key_configured"] = False
    return out

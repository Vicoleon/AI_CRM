"""Admin endpoints for the UC-20 AI Priorities multi-agent pipeline.

GET  /api/admin/priorities/config — current config (api keys masked)
PUT  /api/admin/priorities/config — update config; partial payloads OK
GET  /api/admin/priorities/catalog — static catalog of roles + provider models

All endpoints require the ``admin`` role via :func:`admin_required`.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.auth import admin_required
from rubik_core.auth.registry import UserProfile
from rubik_core.control.repository import connector_secret_ref
from rubik_core.database.db import get_db
from rubik_core.database.models import Department
from rubik_core.priorities import (
    AGENT_ROLES,
    CHANNEL_AGENT_ROLES,
    PIPELINE_AGENT_ROLES,
    PROVIDER_MODEL_CATALOG,
    SUPPORTED_PROVIDERS,
    SettingsReadError,
    load_settings_blob,
    load_settings_blob_strict,
    playbook_store,
    redact_secrets,
    update_settings_blob,
)
from rubik_core.tenancy.context import current_workspace
from rubik_core.tenancy.secrets import (
    clear_workspace_secret,
    upsert_workspace_secret,
    ws_secret_id,
)

logger = logging.getLogger(__name__)
_CREDENTIAL_UPDATE_INCOMPLETE = (
    "Credential update could not complete. Earlier credential changes may have "
    "applied; reload settings before retrying."
)

router = APIRouter(prefix="/admin/priorities", tags=["admin", "priorities"])


# ── Schemas ──────────────────────────────────────────────────────────────────


class AgentSettings(BaseModel):
    provider: str = "vertex"
    model_id: str
    temperature: float = Field(ge=0.0, le=2.0)


class ProviderSettings(BaseModel):
    enabled: bool = False
    # Write-only: when present, the backend stores it as a secret-manager ref.
    # GET responses never echo this — they expose ``api_key_configured`` only.
    api_key: str | None = Field(default=None, exclude=True)
    api_key_configured: bool = False


class PolicySettings(BaseModel):
    revision_threshold: float = Field(default=6.0, ge=0.0, le=10.0)
    max_revisions: int = Field(default=1, ge=0, le=3)


class PrioritiesConfigPayload(BaseModel):
    """Full payload returned by GET and accepted by PUT.

    PUT accepts partial payloads (any subset of agents / policy / providers).
    """

    schema_version: int = 1
    agents: dict[str, AgentSettings] = Field(default_factory=dict)
    policy: PolicySettings = Field(default_factory=PolicySettings)
    providers: dict[str, ProviderSettings] = Field(default_factory=dict)


class CatalogResponse(BaseModel):
    agent_roles: list[str]
    channel_agent_roles: list[str]
    pipeline_agent_roles: list[str]
    supported_providers: list[str]
    provider_model_catalog: dict[str, list[str]]


# ── Endpoints ────────────────────────────────────────────────────────────────


def _redacted_current_workspace(blob: dict[str, Any]) -> dict[str, Any]:
    """Report a key as configured only while its exact control binding lives."""
    public = redact_secrets(blob)
    for provider, entry in (public.get("providers") or {}).items():
        if not isinstance(entry, dict) or provider not in SUPPORTED_PROVIDERS:
            continue
        field = f"{provider}_api_key"
        expected = ws_secret_id("ai_provider", field)
        entry["api_key_configured"] = bool(
            connector_secret_ref(current_workspace(), "ai_provider", field) == expected
        )
    return public


def _merge_config(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Apply a partial admin edit without discarding stored provider refs."""
    merged = copy.deepcopy(base)

    def merge_into(target: dict[str, Any], values: dict[str, Any]) -> None:
        for key, value in values.items():
            existing = target.get(key)
            if isinstance(existing, dict) and isinstance(value, dict):
                merge_into(existing, value)
            else:
                target[key] = copy.deepcopy(value)

    merge_into(merged, patch)
    return merged


@router.get("/catalog", response_model=CatalogResponse)
def get_catalog(_: UserProfile = Depends(admin_required)) -> CatalogResponse:
    """Static catalog the admin UI uses to populate dropdowns."""
    return CatalogResponse(
        agent_roles=list(AGENT_ROLES),
        channel_agent_roles=list(CHANNEL_AGENT_ROLES),
        pipeline_agent_roles=list(PIPELINE_AGENT_ROLES),
        supported_providers=list(SUPPORTED_PROVIDERS),
        provider_model_catalog={k: list(v) for k, v in PROVIDER_MODEL_CATALOG.items()},
    )


@router.get("/config")
def get_priorities_config(_: UserProfile = Depends(admin_required)) -> dict[str, Any]:
    """Return today's persisted priorities config; secrets redacted."""
    blob = load_settings_blob()
    return _redacted_current_workspace(blob)


@router.put("/config")
def put_priorities_config(
    payload: dict[str, Any],
    user: UserProfile = Depends(admin_required),
) -> dict[str, Any]:
    """Update the priorities config. Accepts partial payloads.

    ``providers[*].api_key`` (when supplied) is stored under the workspace's
    control-bound provider connector and never echoed back to the client.
    The configured badge is derived from the live control binding, never from
    a client-supplied boolean.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")

    incoming = copy.deepcopy(payload)
    for key in ("agents", "policy"):
        if key in incoming and not isinstance(incoming[key], dict):
            raise HTTPException(status_code=400, detail=f"{key} must be an object")
    if any(
        not isinstance(entry, dict) for entry in incoming.get("agents", {}).values()
    ):
        raise HTTPException(status_code=400, detail="agent settings must be an object")
    providers_in = incoming.get("providers")
    if "providers" in incoming:
        if not isinstance(providers_in, dict):
            raise HTTPException(status_code=400, detail="providers must be an object")
        for provider, entry in providers_in.items():
            if not isinstance(entry, dict):
                raise HTTPException(
                    status_code=400, detail="provider settings must be an object"
                )
            if provider not in SUPPORTED_PROVIDERS:
                raise HTTPException(status_code=400, detail="Unsupported AI provider")
            raw_key = entry.get("api_key")
            clear_key = entry.get("clear_api_key", False)
            if not isinstance(clear_key, bool):
                raise HTTPException(
                    status_code=400, detail="clear_api_key must be boolean"
                )
            if clear_key and raw_key not in (None, ""):
                raise HTTPException(
                    status_code=400, detail="Cannot set and clear one API key"
                )
            if raw_key is not None and raw_key != "":
                if not isinstance(raw_key, str):
                    raise HTTPException(
                        status_code=400, detail="API key must be a string"
                    )
                if not raw_key.strip():
                    raise HTTPException(status_code=400, detail="API key is empty")

    try:
        # Fail a missing or corrupt tenant settings read before touching the
        # external secret store. The final save rereads under a row lock.
        load_settings_blob_strict()
    except SettingsReadError as exc:
        raise HTTPException(
            status_code=503, detail="Priorities settings are unavailable"
        ) from exc

    edited = copy.deepcopy(incoming)
    providers = edited.get("providers")
    if isinstance(providers, dict):
        for provider, entry in providers.items():
            field = f"{provider}_api_key"
            # Neither a redacted GET nor a crafted client may replace a
            # server-owned reference. Only a new key or explicit clear does.
            entry.pop("api_key_secret_ref", None)
            raw_key = entry.pop("api_key", None)
            entry.pop("api_key_configured", None)
            clear_key = entry.pop("clear_api_key", False)
            if raw_key is not None and raw_key != "":
                try:
                    upsert_workspace_secret("ai_provider", field, raw_key.strip())
                except Exception as exc:
                    raise HTTPException(
                        status_code=503, detail=_CREDENTIAL_UPDATE_INCOMPLETE
                    ) from exc
            elif clear_key is True:
                try:
                    clear_workspace_secret("ai_provider", field)
                except Exception as exc:
                    raise HTTPException(
                        status_code=503, detail=_CREDENTIAL_UPDATE_INCOMPLETE
                    ) from exc

    try:
        blob = update_settings_blob(
            lambda current: _merge_config(current, edited), actor_email=user.email
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail=_CREDENTIAL_UPDATE_INCOMPLETE
        ) from exc
    return _redacted_current_workspace(blob)


# ─── UC-23: Department playbooks ──────────────────────────────────────────────


class PlaybookCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    department: str | None = None  # NULL = default for every user
    body: str = ""
    is_active: bool = True
    sort_order: int = 100


class PlaybookUpdate(BaseModel):
    name: str | None = None
    department: str | None = Field(default=None)
    department_set: bool = False  # explicit "did the client send a department?"
    body: str | None = None
    is_active: bool | None = None
    sort_order: int | None = None


@router.get("/playbooks")
def list_priority_playbooks(
    department: str | None = None,
    include_inactive: bool = True,
    _: UserProfile = Depends(admin_required),
) -> list[dict]:
    """List playbooks; optionally filter by department.

    Bodies are truncated to a 200-char preview in the list view; use
    ``GET /playbooks/{id}`` for the full body.
    """
    rows = playbook_store.list_playbooks(
        department=department if department is not None else "__any__",
        include_inactive=include_inactive,
    )
    return [r.to_dict(body_preview_chars=200) for r in rows]


@router.get("/playbooks/effective")
def get_effective_playbook(
    department: str | None = None,
    _: UserProfile = Depends(admin_required),
) -> dict:
    """Preview the concatenated playbook stack the decision-maker would
    see for a given department (UC-24 consumer).
    """
    return playbook_store.resolve_effective(department).to_dict()


@router.get("/playbooks/{playbook_id}")
def get_priority_playbook(
    playbook_id: str,
    _: UserProfile = Depends(admin_required),
) -> dict:
    row = playbook_store.get_playbook(playbook_id)
    if row is None:
        raise HTTPException(status_code=404, detail="playbook not found")
    return row.to_dict()


@router.post("/playbooks", status_code=201)
def create_priority_playbook(
    payload: PlaybookCreate,
    user: UserProfile = Depends(admin_required),
) -> dict:
    row = playbook_store.create_playbook(
        name=payload.name,
        body=payload.body,
        department=payload.department,
        actor_email=user.email,
        is_active=payload.is_active,
        sort_order=payload.sort_order,
    )
    return row.to_dict()


@router.put("/playbooks/{playbook_id}")
def update_priority_playbook(
    playbook_id: str,
    payload: dict,  # raw dict so callers can omit fields cleanly
    user: UserProfile = Depends(admin_required),
) -> dict:
    # Pydantic models can't distinguish "omitted" from "explicitly null" on
    # nullable fields like ``department`` without an extra flag — we accept
    # a raw dict and treat unset keys as "leave as-is".
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")

    dept = payload.get("department", "__unchanged__")
    row = playbook_store.update_playbook(
        playbook_id,
        actor_email=user.email,
        name=payload.get("name"),
        body=payload.get("body"),
        department=dept,
        is_active=payload.get("is_active"),
        sort_order=payload.get("sort_order"),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="playbook not found")
    return row.to_dict()


@router.delete("/playbooks/{playbook_id}", status_code=204)
def delete_priority_playbook(
    playbook_id: str,
    _: UserProfile = Depends(admin_required),
) -> None:
    ok = playbook_store.delete_playbook(playbook_id)
    if not ok:
        raise HTTPException(status_code=404, detail="playbook not found")


# ─── per-department Slack AI-Priorities delivery opt-in ────────────────


class SlackDeliveryDept(BaseModel):
    name: str
    slack_priorities_enabled: bool


class SlackDeliveryToggle(BaseModel):
    name: str
    enabled: bool


@router.get("/slack-delivery", response_model=list[SlackDeliveryDept])
def list_slack_delivery(
    _: UserProfile = Depends(admin_required),
    db: Session = Depends(get_db),
) -> list[SlackDeliveryDept]:
    """Every department + whether it's opted into the morning Cubie AI-Priorities
    Slack DM (``departments.slack_priorities_enabled``).

    Exec-team users (``User.is_exec``) always receive the DM regardless of
    department, so this list governs only whole-department enrollment.
    """
    rows = (
        db.query(Department.name, Department.slack_priorities_enabled)
        .order_by(func.lower(Department.name))
        .all()
    )
    return [
        SlackDeliveryDept(name=n, slack_priorities_enabled=bool(e)) for n, e in rows
    ]


@router.put("/slack-delivery", response_model=SlackDeliveryDept)
def set_slack_delivery(
    payload: SlackDeliveryToggle,
    user: UserProfile = Depends(admin_required),
    db: Session = Depends(get_db),
) -> SlackDeliveryDept:
    """Opt a department in/out of the Slack AI-Priorities DM.

    The department name comes from the request body (not the URL path) so names
    with slashes (e.g. "Compliance/Audit") or spaces work without URL-encoding
    pitfalls. Case-insensitive match (the ``departments`` table is uncurated, so
    names vary in case).
    """
    row = (
        db.query(Department)
        .filter(func.lower(Department.name) == payload.name.strip().lower())
        .first()
    )
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"Department '{payload.name}' not found"
        )
    row.slack_priorities_enabled = bool(payload.enabled)
    db.commit()
    logger.info(
        "[slack-delivery] %s set department %r -> slack_priorities_enabled=%s",
        user.email,
        row.name,
        row.slack_priorities_enabled,
    )
    return SlackDeliveryDept(
        name=row.name, slack_priorities_enabled=bool(row.slack_priorities_enabled)
    )


# ─── UC-25: priority_memory debug view (admin-only) ───────────────────────────


@router.get("/memory")
def get_priority_memory(
    user_email: str,
    db: Session = Depends(get_db),
    _: UserProfile = Depends(admin_required),
) -> list[dict]:
    """Inspect every memory row for a user.

    Includes resolved + dismissed rows so admins can debug "why isn't
    this priority showing up?" without running a full pipeline.
    """
    from rubik_core.priorities import memory as priority_memory
    from rubik_core.priorities.privacy import public_priority_ids

    try:
        public_ids = public_priority_ids(db, user_email)
    except Exception as exc:
        logger.warning("Could not verify priority memory visibility", exc_info=True)
        raise HTTPException(
            status_code=503, detail="Priorities history is unavailable"
        ) from exc
    rows = priority_memory.load_all(user_email)
    return [r.to_dict() for r in rows if r.last_priority_id in public_ids]

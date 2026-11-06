"""Control-plane provider and per-feature route selection.

The live connector row and secret binding are checked for every inference
call. Never cache a resolved credential or silently switch to managed Vertex.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select

from rubik_core.ai_providers.config import ROUTABLE_FEATURES, model_capabilities
from rubik_core.control.db import control_session
from rubik_core.control.models import Workspace, WorkspaceConnector
from rubik_core.tenancy.context import current_workspace
from rubik_core.tenancy.secrets import (
    clear_workspace_secret,
    upsert_workspace_secret,
    workspace_secret,
)

_FEATURE = re.compile(r"[a-z][a-z0-9_]{0,99}\Z")
_PROJECT = re.compile(r"[a-z][a-z0-9-]{4,61}[a-z0-9]\Z")
_REGION = re.compile(r"[a-z]+(?:-[a-z0-9]+)+\Z")
_EMAIL = re.compile(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.gserviceaccount\.com\Z")
KEY_PROVIDERS = frozenset({"anthropic", "deepseek", "openai", "gemini", "meta"})
PROVIDERS = KEY_PROVIDERS | {"vertex_client", "vertex_managed"}


class IncompatibleModelRoute(RuntimeError):
    """A configured route cannot execute the requested workload."""


@dataclass(frozen=True)
class ModelRoute:
    workspace: str
    feature: str
    provider: str
    model: str
    billing_owner: str
    project: str | None = None
    region: str | None = None
    service_identity: str | None = None


def _feature(value: str) -> str:
    if not isinstance(value, str) or not _FEATURE.fullmatch(value):
        raise ValueError("Invalid AI feature")
    return value


def _provider(value: str) -> str:
    if value not in PROVIDERS:
        raise ValueError("Unsupported AI provider")
    return value


def _connector(provider: str) -> str:
    return f"ai_{provider}"


def configure_provider(
    provider: str,
    *,
    api_key: str | None = None,
    project: str | None = None,
    region: str | None = None,
    service_identity: str | None = None,
) -> None:
    """Set credential or vetted Vertex identity for the current workspace.

    The key is write-only; the control row holds only non-secret configuration.
    A failed secret write never marks a connector connected.
    """
    provider = _provider(provider)
    if provider == "vertex_managed":
        raise ValueError("Managed Vertex uses platform credentials")
    config: dict[str, str] = {}
    if provider in KEY_PROVIDERS:
        if project or region or service_identity:
            raise ValueError("Key provider does not accept a project or identity")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("A provider API key is required")
        if len(api_key) > 4096:
            raise ValueError("Provider API key is too long")
    else:
        if api_key is not None:
            raise ValueError("Client Vertex requires an authorized service identity")
        if not (
            isinstance(project, str)
            and _PROJECT.fullmatch(project)
            and isinstance(region, str)
            and _REGION.fullmatch(region)
            and isinstance(service_identity, str)
            and _EMAIL.fullmatch(service_identity)
        ):
            raise ValueError(
                "Valid Vertex project, region and service identity required"
            )
        config = {
            "project": project,
            "region": region,
            "service_identity": service_identity,
        }
    slug = current_workspace()
    with control_session() as session:
        workspace = session.get(Workspace, slug)
        if workspace is None or workspace.status != "active":
            raise PermissionError("Active workspace required")
        if provider == "vertex_client" and (
            not workspace.google_service_account
            or service_identity != workspace.google_service_account
        ):
            raise PermissionError(
                "Client Vertex must use this workspace's provisioned service identity"
            )
    if provider in KEY_PROVIDERS:
        upsert_workspace_secret(_connector(provider), "api_key", api_key.strip())
    with control_session() as session:
        workspace = session.get(Workspace, slug)
        if workspace is None or workspace.status != "active":
            raise PermissionError("Active workspace required")
        row = session.get(WorkspaceConnector, (slug, _connector(provider)))
        if row is None:
            row = WorkspaceConnector(
                workspace_slug=slug, connector=_connector(provider)
            )
            session.add(row)
        row.status = "connected"
        row.config = config
        session.commit()


def revoke_provider(provider: str) -> None:
    provider = _provider(provider)
    if provider == "vertex_managed":
        raise ValueError("Managed Vertex cannot be revoked by a workspace")
    slug = current_workspace()
    if provider in KEY_PROVIDERS:
        clear_workspace_secret(_connector(provider), "api_key")
    with control_session() as session:
        row = session.get(WorkspaceConnector, (slug, _connector(provider)))
        if row is not None:
            row.status = "disconnected"
            row.config = {}
            session.commit()


def set_model_route(feature: str, provider: str, model: str) -> None:
    feature = _feature(feature)
    if feature == "embedding_index":
        raise IncompatibleModelRoute(
            "Prepare and activate an embedding index before changing its model"
        )
    capability = ROUTABLE_FEATURES.get(feature)
    if capability is None:
        raise ValueError("Workload is not available for provider routing yet")
    provider = _provider(provider)
    if capability not in model_capabilities(provider, model):
        raise IncompatibleModelRoute(
            f"{provider} {model} cannot run {capability} for {feature}"
        )
    slug = current_workspace()
    with control_session() as session:
        workspace = session.get(Workspace, slug)
        if workspace is None or workspace.status != "active":
            raise PermissionError("Active workspace required")
        row = session.get(WorkspaceConnector, (slug, "ai_routes"))
        if row is None:
            row = WorkspaceConnector(workspace_slug=slug, connector="ai_routes")
            session.add(row)
        routes = dict((row.config or {}).get("features") or {})
        routes[feature] = {"provider": provider, "model": model}
        row.config = {"features": routes}
        row.status = "connected"
        session.commit()


def model_routes() -> dict[str, dict[str, str]]:
    slug = current_workspace()
    with control_session() as session:
        row = session.get(WorkspaceConnector, (slug, "ai_routes"))
        routes = (row.config or {}).get("features") if row is not None else None
        return dict(routes) if isinstance(routes, dict) else {}


def provider_status() -> dict[str, dict[str, str | bool | None]]:
    slug = current_workspace()
    with control_session() as session:
        workspace = session.get(Workspace, slug)
        rows = session.scalars(
            select(WorkspaceConnector).where(
                WorkspaceConnector.workspace_slug == slug,
                WorkspaceConnector.connector.in_([_connector(p) for p in PROVIDERS]),
            )
        ).all()
    by_name = {row.connector: row for row in rows}
    result: dict[str, dict[str, str | bool | None]] = {}
    for provider in sorted(PROVIDERS):
        row = by_name.get(_connector(provider))
        result[provider] = {
            "connected": bool(row is not None and row.status == "connected"),
            "credential_configured": bool(
                row is not None
                and row.status == "connected"
                and row.secret_refs.get("api_key")
            )
            if provider in KEY_PROVIDERS
            else bool(row is not None and row.status == "connected"),
            "project": (row.config or {}).get("project")
            if row is not None and provider == "vertex_client"
            else None,
            "region": (row.config or {}).get("region")
            if row is not None and provider == "vertex_client"
            else None,
            "service_identity": workspace.google_service_account
            if workspace is not None and provider == "vertex_client"
            else None,
        }
    result["vertex_managed"] = {
        "connected": True,
        "credential_configured": True,
        "project": None,
        "region": None,
        "service_identity": None,
    }
    return result


def resolve_model_route(
    feature: str, capability: str, *, default_model: str
) -> ModelRoute:
    """Fail before model I/O if an explicit route is unavailable or incompatible."""
    feature = _feature(feature)
    if feature not in ROUTABLE_FEATURES:
        raise IncompatibleModelRoute(
            "Workload is not available for provider routing yet"
        )
    slug = current_workspace()
    with control_session() as session:
        workspace = session.get(Workspace, slug)
        if workspace is None or workspace.status != "active":
            raise IncompatibleModelRoute("Workspace is unavailable")
        row = session.get(WorkspaceConnector, (slug, "ai_routes"))
        routes = (row.config or {}).get("features") if row is not None else None
        chosen = routes.get(feature) if isinstance(routes, dict) else None
        if chosen is None:
            return ModelRoute(slug, feature, "vertex_managed", default_model, "cubie")
        if not isinstance(chosen, dict):
            raise IncompatibleModelRoute("Workspace AI route is invalid")
        provider = chosen.get("provider")
        model = chosen.get("model")
        if not isinstance(provider, str) or not isinstance(model, str):
            raise IncompatibleModelRoute("Workspace AI route is invalid")
        try:
            capabilities = model_capabilities(provider, model)
        except ValueError as exc:
            raise IncompatibleModelRoute(
                "Workspace model is no longer supported"
            ) from exc
        if capability not in capabilities:
            raise IncompatibleModelRoute(
                f"{provider} {model} cannot run {capability} for {feature}; choose a compatible model"
            )
    return provider_connection_route(
        provider, model, feature=feature, capability=capability
    )


def provider_connection_route(
    provider: str,
    model: str,
    *,
    feature: str = "connection_test",
    capability: str = "text",
) -> ModelRoute:
    """Resolve one provider directly for a test without changing feature policy."""
    feature = _feature(feature)
    try:
        capabilities = model_capabilities(provider, model)
    except ValueError as exc:
        raise IncompatibleModelRoute("Workspace model is not supported") from exc
    if capability not in capabilities:
        raise IncompatibleModelRoute(f"{provider} {model} cannot run {capability}")
    slug = current_workspace()
    if provider == "vertex_managed":
        return ModelRoute(slug, feature, provider, model, "cubie")
    with control_session() as session:
        workspace = session.get(Workspace, slug)
        connector = session.get(WorkspaceConnector, (slug, _connector(provider)))
        if workspace is None or workspace.status != "active":
            raise IncompatibleModelRoute("Workspace is unavailable")
        if connector is None or connector.status != "connected":
            raise IncompatibleModelRoute(
                f"{provider} is disconnected for this workspace"
            )
        config = connector.config or {}
        if provider in KEY_PROVIDERS:
            if (connector.secret_refs or {}).get(
                "api_key"
            ) != f"ws-{slug}-ai_{provider}-api_key":
                raise IncompatibleModelRoute(f"{provider} API key is unavailable")
        else:
            if not all(
                config.get(key) for key in ("project", "region", "service_identity")
            ):
                raise IncompatibleModelRoute("Client Vertex identity is incomplete")
            if config["service_identity"] != workspace.google_service_account:
                raise IncompatibleModelRoute(
                    "Client Vertex identity is no longer bound to this workspace"
                )
    if provider in KEY_PROVIDERS and not workspace_secret(
        _connector(provider), "api_key"
    ):
        raise IncompatibleModelRoute(f"{provider} API key is unavailable")
    return ModelRoute(
        slug,
        feature,
        provider,
        model,
        "client",
        project=config.get("project"),
        region=config.get("region"),
        service_identity=config.get("service_identity"),
    )

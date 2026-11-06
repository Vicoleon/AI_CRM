"""Workspace-pinned ADK models.

ADK's default Gemini model discovers credentials from the process environment.
That is unsuitable for client-owned routes: the same service hosts several
workspaces, and an agent may issue several model calls for one turn. This
module gives each agent invocation an explicit route and client, and checks
that the route is still authorized before every model call.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from google.adk.models.google_llm import Gemini
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from pydantic import PrivateAttr

from rubik_core.ai_providers.config import model_capabilities
from rubik_core.ai_providers.registry import (
    IncompatibleModelRoute,
    ModelRoute,
    resolve_model_route,
)
from rubik_core.ai_providers.runtime import ProviderRequestError, _genai_client, _key
from rubik_core.tenancy.context import current_workspace


class WorkspaceGemini(Gemini):
    """ADK Gemini with an explicit, invocation-bound workspace credential."""

    _route: ModelRoute = PrivateAttr()

    @classmethod
    def for_route(cls, route: ModelRoute) -> WorkspaceGemini:
        if route.provider not in {"gemini", "vertex_client"}:
            raise IncompatibleModelRoute("ADK Gemini requires a Google provider")
        model = cls(model=route.model)
        model._route = route
        return model

    @property
    def route(self) -> ModelRoute:
        return self._route

    @property
    def api_client(self):
        # Do not cache a credential: revocation and rotation must take effect
        # on the next call, even when a tool loop keeps the agent alive.
        if current_workspace() != self._route.workspace:
            raise IncompatibleModelRoute("Workspace AI route changed during this turn")
        return _genai_client(self._route, _key(self._route))

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        route = self._route
        if current_workspace() != route.workspace:
            raise IncompatibleModelRoute("Workspace AI route changed during this turn")
        capability = "tools_stream" if route.feature == "chat_adk" else "tools"
        live = resolve_model_route(route.feature, capability, default_model=route.model)
        if live != route:
            raise IncompatibleModelRoute("Workspace AI route changed during this turn")
        if stream and "stream" not in model_capabilities(route.provider, route.model):
            raise IncompatibleModelRoute("Selected model does not support streaming")
        # A cache created with another credential/model must never be reused.
        llm_request.cache_config = None
        if llm_request.config is not None:
            llm_request.config.cached_content = None
        try:
            async for response in super().generate_content_async(llm_request, stream):
                yield response
        except (IncompatibleModelRoute, ProviderRequestError):
            raise
        except Exception:
            # Provider SDK exceptions may embed URLs, headers, or prompt text.
            raise ProviderRequestError(
                f"{route.provider} model request failed"
            ) from None


def resolve_adk_route(feature: str, *, default_model: str) -> ModelRoute:
    """Pin a tool-capable route before constructing any ADK agent."""
    capability = "tools_stream" if feature == "chat_adk" else "tools"
    return resolve_model_route(feature, capability, default_model=default_model)


def model_for_route(route: ModelRoute, *, managed_factory):
    """Preserve the existing managed model; use explicit clients for BYOK."""
    if route.provider == "vertex_managed":
        return managed_factory(route.model)
    if route.provider in {"gemini", "vertex_client"}:
        return WorkspaceGemini.for_route(route)
    if route.provider in {"anthropic", "openai", "deepseek", "meta"}:
        from rubik_core.ai_providers.adk_remote import WorkspaceRemoteLlm

        return WorkspaceRemoteLlm.for_route(route)
    raise IncompatibleModelRoute(
        f"{route.provider} {route.model} has no verified ADK tool adapter"
    )

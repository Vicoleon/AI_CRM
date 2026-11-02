"""Channel-agent base + LLM helpers for Deep Research.

Mirrors the priorities BaseChannelAgent contract: ``run()`` does gather →
build prompt → one JSON LLM call → parse into a ``ChannelDigest``, and is
fail-soft (any exception becomes ``ChannelDigest(error=...)`` so one dead
channel never sinks the run). Agents run inside the orchestrator's thread pool,
so LLM calls here are synchronous.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from rubik_core.ai_providers.registry import (
    IncompatibleModelRoute,
    ModelRoute,
    resolve_model_route,
)
from rubik_core.ai_providers.runtime import (
    ProviderRequestError,
    generate_grounded_text_on_route,
    generate_text_on_route,
    parse_json_response,
)
from rubik_core.models import get_genai_client, safe_json
from rubik_core.models import llm_grounded as _llm_grounded
from rubik_core.xray import config
from rubik_core.xray.schemas import ChannelDigest, CompanyBundle

logger = logging.getLogger(__name__)
XRAY_JSON_FEATURE = "xray_json"
XRAY_GROUNDING_FEATURE = "xray_grounding"

# `safe_json` and `llm_grounded` moved to rubik_core.models (the neutral LLM
# layer) once attendee intelligence needed the same grounded call — one
# implementation, no third copy. Re-exported here because this module has been
# the import site for both since UC-45.
__all__ = [
    "BaseXrayAgent",
    "llm_grounded",
    "llm_json",
    "model_label",
    "resolve_xray_route",
    "safe_json",
]


def resolve_xray_route(default_model: str) -> ModelRoute:
    """Pin a JSON route before a run's model work begins."""
    return resolve_model_route(XRAY_JSON_FEATURE, "json", default_model=default_model)


def resolve_xray_grounding_route(default_model: str) -> ModelRoute:
    """Pin the web research provider before a Deep Research run."""
    return resolve_model_route(
        XRAY_GROUNDING_FEATURE, "grounding", default_model=default_model
    )


def model_label(route: ModelRoute) -> str:
    """Snapshot provenance: retain legacy model labels for managed Vertex."""
    if route.provider == "vertex_managed":
        return route.model
    return f"{route.provider}:{route.model}"


def llm_json(
    system: str,
    prompt: str,
    *,
    model_id: str,
    temperature: float = 0.2,
    route: ModelRoute | None = None,
) -> Any:
    """One JSON call; a pinned client route never falls back to managed Vertex."""
    if route is not None:
        if route.provider != "vertex_managed":
            try:
                response = generate_text_on_route(
                    route,
                    prompt,
                    json_mode=True,
                    system_instruction=system,
                    temperature=temperature,
                )
                data = parse_json_response(response)
                if not isinstance(data, dict):
                    raise ProviderRequestError(
                        "Selected AI provider returned an invalid X-Ray response"
                    )
                return data
            except IncompatibleModelRoute:
                raise
            except Exception:
                # Provider SDKs and JSON parsers can include response text in
                # exceptions. No transcript, customer data or key goes to a
                # snapshot's partial_errors, the API, or a warning log.
                raise ProviderRequestError(
                    "Selected AI provider failed for X-Ray analysis"
                ) from None
        current = resolve_xray_route(model_id)
        if current != route:
            raise IncompatibleModelRoute("Workspace AI route changed during X-Ray")

    from google.genai import types

    client = get_genai_client()
    res = client.models.generate_content(
        model=route.model if route is not None else model_id,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=system, temperature=temperature
        ),
    )
    return safe_json(res.text or "")


def llm_grounded(
    system: str,
    prompt: str,
    *,
    model_id: str | None = None,
    route: ModelRoute | None = None,
) -> tuple[str, list[str]]:
    """A Google-Search-grounded Gemini call, defaulted to the X-Ray web tier.

    ``None`` rather than a default argument of ``config.model_web()``: a default
    is evaluated once when this module is imported, which would pin the model for
    the life of the process and silently ignore an admin's change.
    """
    default_model = model_id or config.model_web()
    route = route or resolve_xray_grounding_route(default_model)
    if route.provider == "vertex_managed":
        if resolve_xray_grounding_route(default_model) != route:
            raise IncompatibleModelRoute(
                "Workspace AI route changed during X-Ray web research"
            )
        return _llm_grounded(system, prompt, model_id=route.model)
    try:
        response = generate_grounded_text_on_route(
            route, prompt, system_instruction=system, temperature=0.1
        )
        return response.text, list(response.citation_urls)
    except IncompatibleModelRoute:
        raise
    except Exception:
        raise ProviderRequestError(
            "Selected AI provider failed for X-Ray web research"
        ) from None


class BaseXrayAgent(ABC):
    """A Deep Research channel agent. Subclasses implement gather + build."""

    channel: str = "base"

    def __init__(
        self,
        *,
        bundle: CompanyBundle,
        requesting_user: str,
        model_id: str | None = None,
        route: ModelRoute | None = None,
    ) -> None:
        self.bundle = bundle
        self.requesting_user = requesting_user
        self.model_id = model_id or config.model_channel()
        self.route = route
        self.company_name = bundle.company_name or bundle.company_id
        self.company_domain = bundle.company_domain or ""

    @abstractmethod
    def gather(self) -> str:
        """Fetch this channel's raw evidence as a text block (may do I/O)."""

    @abstractmethod
    def build(self, evidence: str) -> tuple[str, str]:
        """Return (system_instruction, prompt) for the extraction call."""

    def parse(self, data: Any) -> ChannelDigest:
        """Default parse: {summary, highlights, attrs} → ChannelDigest."""
        if not isinstance(data, dict):
            return ChannelDigest(channel=self.channel, summary=str(data)[:400])
        return ChannelDigest(
            channel=self.channel,
            summary=str(data.get("summary") or "")[:600],
            highlights=[str(h) for h in (data.get("highlights") or [])][:25],
            attrs=data.get("attrs") if isinstance(data.get("attrs"), dict) else {},
        )

    def run(self) -> ChannelDigest:
        """gather → build → one JSON LLM call → parse. Never raises."""
        try:
            evidence = self.gather()
        except Exception as exc:  # noqa: BLE001 - fail-soft per channel
            logger.warning("xray %s gather failed: %s", self.channel, exc)
            return ChannelDigest(channel=self.channel, error=f"gather_failed: {exc}")
        try:
            system, prompt = self.build(evidence)
            data = llm_json(system, prompt, model_id=self.model_id, route=self.route)
            digest = self.parse(data)
            if not digest.evidence and evidence:
                digest.evidence = [f"{self.channel}: {evidence[:200]}"]
            return digest
        except Exception:  # noqa: BLE001
            logger.warning("xray %s analysis unavailable", self.channel)
            return ChannelDigest(
                channel=self.channel,
                error="extract_failed: AI analysis unavailable",
            )

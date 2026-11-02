"""JSON-returning model call for CRM Sync.

A local copy rather than a shared import, matching what AI Priorities and
Customer X-Ray each already do (``rubik_core/priorities/agents/base.py`` and
``rubik_core/xray/agents/base.py``): feature packages don't depend on each
other, and the tolerant-parse behaviour is small enough that a copy is cheaper
than the coupling.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from rubik_core.ai_providers.registry import (
    IncompatibleModelRoute,
    ModelRoute,
    resolve_model_route,
)
from rubik_core.ai_providers.runtime import ProviderRequestError, generate_text_on_route
from rubik_core.models import get_genai_client

logger = logging.getLogger(__name__)


def safe_json(raw: str) -> Any:
    """Parse LLM output as JSON, tolerating fences / leading prose."""
    if raw is None:
        raise ValueError("empty LLM response")
    text = raw.strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"[\{\[].*[\}\]]", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    # Model text may repeat private transcript and CRM data. Never put a
    # response excerpt in an exception that can reach a scan row or log.
    raise ValueError("could not parse JSON from LLM output")


def llm_json(
    system: str,
    prompt: str,
    *,
    model_id: str,
    temperature: float = 0.1,
    route: ModelRoute | None = None,
) -> Any:
    """One JSON extraction on a pinned workspace route (sync).

    Goes through ``get_genai_client()`` so the spend lands against this
    feature in the AI-cost ledger instead of "unattributed".
    """
    from google.genai import types

    pinned = route is not None
    if route is None:
        route = resolve_model_route("crm_sync", "json", default_model=model_id)
    if pinned:
        live = resolve_model_route("crm_sync", "json", default_model=model_id)
        if live != route:
            raise IncompatibleModelRoute("Workspace AI route changed during CRM Sync")
    if route.provider != "vertex_managed":
        response = generate_text_on_route(
            route,
            prompt,
            json_mode=True,
            system_instruction=system,
        )
        try:
            return safe_json(response.text)
        except ValueError:
            raise ProviderRequestError(
                "Selected AI provider returned invalid JSON for CRM Sync"
            ) from None

    client = get_genai_client()
    res = client.models.generate_content(
        model=route.model,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=system, temperature=temperature
        ),
    )
    return safe_json(res.text or "")

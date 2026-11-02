"""Pinned workspace inference for existing text and JSON call paths.

The caller supplies its managed Vertex implementation so existing Gemini
settings and response handling stay intact. A selected client route uses the
provider adapter and can never fall through to that managed implementation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from rubik_core.ai_providers.registry import ModelRoute, resolve_model_route
from rubik_core.ai_providers.runtime import generate_text_on_route


def run_text(
    feature: str,
    prompt: str,
    *,
    default_model: str,
    managed: Callable[[ModelRoute], str],
    json_mode: bool = False,
    system_instruction: str | None = None,
    temperature: float | None = None,
) -> str:
    return run_text_with_route(
        feature,
        prompt,
        default_model=default_model,
        managed=managed,
        json_mode=json_mode,
        system_instruction=system_instruction,
        temperature=temperature,
    )[0]


def run_text_with_route(
    feature: str,
    prompt: str,
    *,
    default_model: str,
    managed: Callable[[ModelRoute], str],
    json_mode: bool = False,
    system_instruction: str | None = None,
    temperature: float | None = None,
    pinned_route: ModelRoute | None = None,
) -> tuple[str, ModelRoute]:
    """Return the response and the route pinned for this invocation."""
    route = pinned_route or resolve_model_route(
        feature, "json" if json_mode else "text", default_model=default_model
    )
    if route.provider == "vertex_managed":
        return managed(route), route
    response = generate_text_on_route(
        route,
        prompt,
        json_mode=json_mode,
        system_instruction=system_instruction,
        temperature=temperature,
    )
    return response.text, route


async def run_text_async(
    feature: str,
    prompt: str,
    *,
    default_model: str,
    managed: Callable[[ModelRoute], Awaitable[str]],
    json_mode: bool = False,
    system_instruction: str | None = None,
    temperature: float | None = None,
) -> str:
    route = await asyncio.to_thread(
        resolve_model_route,
        feature,
        "json" if json_mode else "text",
        default_model=default_model,
    )
    if route.provider == "vertex_managed":
        return await managed(route)
    return (
        await asyncio.to_thread(
            generate_text_on_route,
            route,
            prompt,
            json_mode=json_mode,
            system_instruction=system_instruction,
            temperature=temperature,
        )
    ).text

"""Gemini with a one-shot switch to a second model when the first one fails upstream.

Why: on a "deals slipping" question the chat model returned 504
DEADLINE_EXCEEDED three times in a row on the final answer turn. The client
retried the SAME model each time, ~36s per attempt, so the user watched a
spinner for ~110s and then got an error. A different model is served
from different capacity, so one switch is worth more than two more retries.

Rules:
- Only upstream failures switch: a Vertex 5xx, a 429, or our own per-call
  HTTP timeout. A 4xx (bad request, permission) would fail on any model.
- Only before the first chunk. Once the primary has streamed anything, the
  turn is partly written and cannot be restarted on another model.
- The fallback runs from a copy of the original request with context caching
  off, because a Vertex context cache belongs to the model that created it,
  and with the thinking config recomputed for the fallback's own family.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator

import httpx
from google.adk.models.google_llm import Gemini
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import errors as genai_errors

from rubik_core.gemini_tuning import thinking_config_for

logger = logging.getLogger(__name__)


def is_upstream_failure(e: BaseException) -> bool:
    """True when the model service, not the request, is at fault."""
    if isinstance(e, genai_errors.ServerError):
        return True
    if isinstance(e, genai_errors.ClientError) and getattr(e, "code", None) == 429:
        return True
    return isinstance(e, (httpx.TimeoutException, TimeoutError))


class FallbackGemini(Gemini):
    """ADK Gemini that retries a failed call once on ``fallback_model``."""

    fallback_model: str | None = None

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        fallback = self.fallback_model
        primary_model = llm_request.model or self.model
        if not fallback or fallback == primary_model:
            async for response in super().generate_content_async(llm_request, stream):
                yield response
            return

        # Snapshot before the primary attempt mutates the request (headers,
        # cached_content, appended user content).
        pristine = llm_request.model_copy(deep=True)
        yielded = False
        try:
            async for response in super().generate_content_async(llm_request, stream):
                yielded = True
                yield response
            return
        except Exception as e:
            if yielded or not is_upstream_failure(e):
                raise
            logger.warning(
                "chat-llm-fallback: %s failed upstream (%s: %s); retrying once on %s",
                primary_model,
                type(e).__name__,
                str(e)[:200],
                fallback,
            )

        retry = pristine
        retry.model = fallback
        retry.cache_config = None
        if retry.config is not None:
            retry.config.cached_content = None
            if retry.config.thinking_config is not None:
                retry.config.thinking_config = thinking_config_for(fallback)
        async for response in super().generate_content_async(retry, stream):
            yield response

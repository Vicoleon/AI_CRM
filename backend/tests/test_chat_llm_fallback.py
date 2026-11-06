"""FallbackGemini: one switch to a second model on an upstream failure.

Regression for three 504s in a row on the same model, ~110s, then an error.
Pure logic — the parent Gemini call is replaced with a scripted fake, no
Vertex.
"""

from __future__ import annotations

import asyncio

import pytest
from google.adk.models.google_llm import Gemini
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import errors as genai_errors
from google.genai import types

from rubik_core.adk_agent.fallback_llm import FallbackGemini, is_upstream_failure
from rubik_core.config import settings

PRIMARY = "gemini-3.8-flash"
FALLBACK = "gemini-3.6-flash"


def _server_504() -> genai_errors.ServerError:
    return genai_errors.ServerError(
        504,
        {
            "error": {
                "code": 504,
                "message": "Deadline expired",
                "status": "DEADLINE_EXCEEDED",
            }
        },
    )


def _request() -> LlmRequest:
    return LlmRequest(
        model=PRIMARY,
        contents=[
            types.Content(role="user", parts=[types.Part(text="deals slipping?")])
        ],
        config=types.GenerateContentConfig(
            cached_content="projects/p/locations/global/cachedContents/primary-cache",
            thinking_config=types.ThinkingConfig(
                thinking_level=types.ThinkingLevel.MEDIUM
            ),
        ),
    )


def _install_fake(monkeypatch, script):
    """``script(model) -> list`` of LlmResponse or an Exception to raise."""
    calls: list[LlmRequest] = []

    async def fake(self, llm_request, stream=False):
        calls.append(llm_request.model_copy(deep=True))
        for item in script(llm_request.model):
            if isinstance(item, BaseException):
                raise item
            yield item

    monkeypatch.setattr(Gemini, "generate_content_async", fake)
    return calls


def _ok(text: str) -> LlmResponse:
    return LlmResponse(
        content=types.Content(role="model", parts=[types.Part(text=text)])
    )


async def _collect(model: FallbackGemini, req: LlmRequest) -> list[LlmResponse]:
    return [r async for r in model.generate_content_async(req, stream=True)]


def _run(coro):
    return asyncio.run(coro)


def test_504_before_first_chunk_switches_once_to_the_fallback(monkeypatch):
    calls = _install_fake(
        monkeypatch,
        lambda m: [_server_504()] if m == PRIMARY else [_ok("from fallback")],
    )
    model = FallbackGemini(model=PRIMARY, fallback_model=FALLBACK)

    out = _run(_collect(model, _request()))

    assert [r.content.parts[0].text for r in out] == ["from fallback"]
    assert [c.model for c in calls] == [PRIMARY, FALLBACK]
    retry = calls[1]
    # A context cache belongs to the primary model; the fallback must not use it.
    assert retry.config.cached_content is None
    assert retry.cache_config is None
    assert retry.config.thinking_config is not None


def test_failure_after_streaming_started_is_not_retried(monkeypatch):
    calls = _install_fake(monkeypatch, lambda m: [_ok("partial"), _server_504()])
    model = FallbackGemini(model=PRIMARY, fallback_model=FALLBACK)

    with pytest.raises(genai_errors.ServerError):
        _run(_collect(model, _request()))
    assert [c.model for c in calls] == [PRIMARY]


def test_a_bad_request_is_not_retried_on_another_model(monkeypatch):
    bad = genai_errors.ClientError(400, {"error": {"code": 400, "message": "invalid"}})
    calls = _install_fake(monkeypatch, lambda m: [bad])
    model = FallbackGemini(model=PRIMARY, fallback_model=FALLBACK)

    with pytest.raises(genai_errors.ClientError):
        _run(_collect(model, _request()))
    assert [c.model for c in calls] == [PRIMARY]


def test_fallback_failing_too_surfaces_its_error(monkeypatch):
    calls = _install_fake(monkeypatch, lambda m: [_server_504()])
    model = FallbackGemini(model=PRIMARY, fallback_model=FALLBACK)

    with pytest.raises(genai_errors.ServerError):
        _run(_collect(model, _request()))
    assert [c.model for c in calls] == [PRIMARY, FALLBACK]


@pytest.mark.parametrize("fallback", [None, PRIMARY])
def test_no_fallback_or_same_model_is_a_plain_passthrough(monkeypatch, fallback):
    calls = _install_fake(monkeypatch, lambda m: [_server_504()])
    model = FallbackGemini(model=PRIMARY, fallback_model=fallback)

    with pytest.raises(genai_errors.ServerError):
        _run(_collect(model, _request()))
    assert len(calls) == 1


def test_upstream_failure_classification():
    assert is_upstream_failure(_server_504())
    assert is_upstream_failure(genai_errors.ClientError(429, {"error": {"code": 429}}))
    assert is_upstream_failure(TimeoutError())
    assert not is_upstream_failure(
        genai_errors.ClientError(403, {"error": {"code": 403}})
    )
    assert not is_upstream_failure(ValueError("boom"))


def test_same_model_retries_skip_504():
    """A 504 means the call already spent its whole per-call deadline."""
    assert 504 not in settings.CHAT_LLM_RETRY_STATUS_CODES
    assert 429 in settings.CHAT_LLM_RETRY_STATUS_CODES
    assert settings.CHAT_LLM_FALLBACK_MODEL
    assert settings.CHAT_LLM_FALLBACK_MODEL != settings.VERTEX_LLM_MODEL_FLASH

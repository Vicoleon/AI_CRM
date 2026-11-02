"""ADK turns are pinned to a workspace route and never reuse another key."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from google.adk.models.google_llm import Gemini
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from sqlalchemy import delete

from rubik_core.ai_providers.adk_model import WorkspaceGemini, resolve_adk_route
from rubik_core.ai_providers.adk_remote import WorkspaceRemoteLlm
from rubik_core.ai_providers.registry import (
    IncompatibleModelRoute,
    ModelRoute,
    configure_provider,
    revoke_provider,
    set_model_route,
)
from rubik_core.ai_providers.runtime import ProviderRequestError
from rubik_core.config import AppMode, settings
from rubik_core.control.db import control_session
from rubik_core.control.models import Workspace, WorkspaceConnector
from rubik_core.identifiers import ADK_COORDINATOR_NAME
from rubik_core.tenancy.context import use_workspace


@pytest.fixture
def adk_workspaces(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "APP_MODE", AppMode.LOCAL)
    monkeypatch.setattr(settings, "CLOUDSQL_CONNECTION_NAME", "")
    with control_session() as session:
        session.add_all(
            [
                Workspace(slug="adk-key-a", name="ADK A", status="active"),
                Workspace(slug="adk-key-b", name="ADK B", status="active"),
            ]
        )
        session.commit()
    yield
    with control_session() as session:
        session.execute(
            delete(WorkspaceConnector).where(
                WorkspaceConnector.workspace_slug.in_(("adk-key-a", "adk-key-b"))
            )
        )
        for slug in ("adk-key-a", "adk-key-b"):
            session.delete(session.get(Workspace, slug))
        session.commit()


def _gemini_route(slug: str, key: str, feature: str = "chat_adk"):
    with use_workspace(slug):
        configure_provider("gemini", api_key=key)
        set_model_route(feature, "gemini", "gemini-2.5-pro")
        return resolve_adk_route(feature, default_model="gemini-2.5-pro")


def test_two_workspace_adk_models_get_distinct_keys(adk_workspaces, monkeypatch):
    from rubik_core.ai_providers import adk_model

    captured = []
    monkeypatch.setattr(
        adk_model,
        "_genai_client",
        lambda route, key: captured.append((route.workspace, key)) or object(),
    )
    route_a = _gemini_route("adk-key-a", "secret-A")
    route_b = _gemini_route("adk-key-b", "secret-B")
    with use_workspace("adk-key-a"):
        model_a = WorkspaceGemini.for_route(route_a)
        assert model_a.api_client is not None
    with use_workspace("adk-key-b"):
        model_b = WorkspaceGemini.for_route(route_b)
        assert model_b.api_client is not None
    assert captured == [("adk-key-a", "secret-A"), ("adk-key-b", "secret-B")]
    assert model_a.route != model_b.route


def test_adk_revalidates_route_before_every_model_call(adk_workspaces, monkeypatch):
    called = []

    async def fake_generate(self, request, stream=False):
        called.append((request.model, stream))
        yield LlmResponse(content=types.Content(parts=[types.Part(text="ok")]))

    monkeypatch.setattr(Gemini, "generate_content_async", fake_generate)
    route = _gemini_route("adk-key-a", "secret-A")
    model = WorkspaceGemini.for_route(route)
    request = LlmRequest(model=route.model)

    async def collect():
        return [item async for item in model.generate_content_async(request, True)]

    with use_workspace("adk-key-a"):
        assert asyncio.run(collect())[0].content.parts[0].text == "ok"
        revoke_provider("gemini")
        with pytest.raises(IncompatibleModelRoute, match="disconnected"):
            asyncio.run(collect())
    assert called == [("gemini-2.5-pro", True)]


def test_adk_refuses_cross_workspace_invocation(adk_workspaces, monkeypatch):
    async def fake_generate(self, request, stream=False):
        raise AssertionError("model must not run")
        yield  # pragma: no cover

    monkeypatch.setattr(Gemini, "generate_content_async", fake_generate)
    route = _gemini_route("adk-key-a", "secret-A")
    model = WorkspaceGemini.for_route(route)

    async def collect():
        return [item async for item in model.generate_content_async(LlmRequest())]

    with use_workspace("adk-key-b"):
        with pytest.raises(IncompatibleModelRoute, match="Workspace AI route"):
            asyncio.run(collect())


def test_adk_redacts_provider_exception(adk_workspaces, monkeypatch):
    async def fake_generate(self, request, stream=False):
        raise RuntimeError("key=secret-A request body contained private email")
        yield  # pragma: no cover

    monkeypatch.setattr(Gemini, "generate_content_async", fake_generate)
    route = _gemini_route("adk-key-a", "secret-A")
    model = WorkspaceGemini.for_route(route)

    async def collect():
        return [item async for item in model.generate_content_async(LlmRequest())]

    with use_workspace("adk-key-a"):
        with pytest.raises(ProviderRequestError) as exc:
            asyncio.run(collect())
    assert "secret-A" not in str(exc.value)
    assert "private email" not in str(exc.value)
    assert exc.value.__cause__ is None


def test_claude_and_muse_adk_routes_do_not_share_keys(adk_workspaces, monkeypatch):
    from rubik_core.ai_providers import adk_remote

    calls = []

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        calls.append(
            (
                str(request.url),
                request.headers.get("x-api-key")
                or request.headers.get("authorization"),
                body["model"],
            )
        )
        if "anthropic.com" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "content": [{"type": "text", "text": "Claude answer"}],
                    "usage": {"input_tokens": 2, "output_tokens": 2},
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Muse answer"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 2},
            },
        )

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        adk_remote.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    async def one_turn(route):
        model = WorkspaceRemoteLlm.for_route(route)
        request = LlmRequest(
            model=route.model,
            contents=[types.Content(role="user", parts=[types.Part(text="Hello")])],
        )
        return (
            [item async for item in model.generate_content_async(request)][0]
            .content.parts[0]
            .text
        )

    with use_workspace("adk-key-a"):
        configure_provider("anthropic", api_key="claude-only-key")
        set_model_route("chat_adk", "anthropic", "claude-sonnet-4-6")
        route_a = resolve_adk_route("chat_adk", default_model="unused")
        assert asyncio.run(one_turn(route_a)) == "Claude answer"
    with use_workspace("adk-key-b"):
        configure_provider("meta", api_key="muse-only-key")
        set_model_route("chat_adk", "meta", "muse-spark-1.3")
        route_b = resolve_adk_route("chat_adk", default_model="unused")
        assert asyncio.run(one_turn(route_b)) == "Muse answer"
    assert calls == [
        (
            "https://api.anthropic.com/v1/messages",
            "claude-only-key",
            "claude-sonnet-4-6",
        ),
        (
            "https://api.meta.ai/v1/chat/completions",
            "Bearer muse-only-key",
            "muse-spark-1.3",
        ),
    ]


def test_nested_sales_agent_tokens_use_its_own_model_and_billing_owner(monkeypatch):
    from backend.routers.chat import _record_chat_usage
    from rubik_core.cost import usage as cost_usage
    from rubik_core.orchestration.adk_orchestrator import _TurnUsage

    turn = _TurnUsage()

    def event(author, prompt, completion, *, partial=False):
        return SimpleNamespace(
            author=author,
            partial=partial,
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=prompt,
                candidates_token_count=completion,
                total_token_count=prompt + completion,
            ),
        )

    turn.add(event(ADK_COORDINATOR_NAME, 10, 3))
    turn.add(event("sales_email_agent", 8, 2))
    turn.add(event("sales_email_agent", 999, 999, partial=True))
    recorded = []
    monkeypatch.setattr(cost_usage, "record_usage", lambda **kw: recorded.append(kw))
    orchestrator = SimpleNamespace(
        model_route=ModelRoute("test", "chat_adk", "openai", "gpt-6-luna", "client"),
        sales_model_route=ModelRoute(
            "test",
            "sales_email_agent",
            "anthropic",
            "claude-sonnet-4-6",
            "client",
        ),
    )
    _record_chat_usage(
        orchestrator,
        turn.by_agent,
        {
            "prompt": turn.prompt,
            "completion": turn.completion,
            "thinking": turn.thinking,
            "cached": turn.cached,
            "total": turn.total,
        },
        user_email="rep@example.com",
        request_id="test-request",
    )
    assert [
        (row["feature"], row["model"], row["prompt_tokens"]) for row in recorded
    ] == [
        ("chat", "gpt-6-luna", 10),
        ("sales_email_agent", "claude-sonnet-4-6", 8),
    ]
    assert [row["metadata"]["provider"] for row in recorded] == [
        "openai",
        "anthropic",
    ]
    assert sum(row["total_tokens"] for row in recorded) == turn.total


def test_scheduled_chat_cost_records_selected_workspace_provider(monkeypatch):
    from rubik_core.cost import usage as cost_usage
    from rubik_core.scheduled_tasks.runner import _record_cost

    recorded = []
    monkeypatch.setattr(cost_usage, "record_usage", lambda **kw: recorded.append(kw))
    _record_cost(
        SimpleNamespace(
            model_route=ModelRoute(
                "test", "chat_adk", "meta", "muse-spark-1.3", "client"
            )
        ),
        SimpleNamespace(
            prompt_tokens=4,
            completion_tokens=2,
            total_tokens=6,
            thinking_tokens=0,
            cached_tokens=0,
        ),
        "employee@example.com",
        "scheduled-request",
    )
    assert len(recorded) == 1
    assert recorded[0]["feature"] == "scheduled_tasks"
    assert recorded[0]["model"] == "muse-spark-1.3"
    assert recorded[0]["metadata"] == {
        "provider": "meta",
        "billing_owner": "client",
    }


def test_headless_chat_keeps_selected_route_and_fails_after_revocation(
    adk_workspaces, monkeypatch
):
    from rubik_core.agent_runtime.coordinator import HeadlessRun, build_coordinator
    from rubik_core.company.profile import CompanyProfile

    called = []
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(
                lambda request: called.append(request) or httpx.Response(200)
            ),
            **kwargs,
        ),
    )
    with use_workspace("adk-key-a"):
        configure_provider("meta", api_key="headless-key")
        set_model_route("chat_adk", "meta", "muse-spark-1.3")
        route = resolve_adk_route("chat_adk", default_model="unused")
        agent = build_coordinator(
            "employee@acme.example",
            headless=HeadlessRun("weekly review"),
            model_route=route,
            company_profile=CompanyProfile(),
        )
        assert isinstance(agent.model, WorkspaceRemoteLlm)
        assert agent.sub_agents == []
        revoke_provider("meta")

        async def collect():
            return [
                item
                async for item in agent.model.generate_content_async(
                    LlmRequest(model=route.model)
                )
            ]

        with pytest.raises(IncompatibleModelRoute, match="disconnected"):
            asyncio.run(collect())
    assert called == []

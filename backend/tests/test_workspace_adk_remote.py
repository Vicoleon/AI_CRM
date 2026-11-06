"""Native ADK translation contracts for client-owned tool and stream routes."""

from __future__ import annotations

import asyncio
import json
import re

import httpx
import pytest
from google.adk.agents import Agent
from google.adk.agents.run_config import StreamingMode
from google.adk.models.llm_request import LlmRequest
from google.adk.runners import RunConfig, Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from rubik_core.ai_providers import adk_remote
from rubik_core.ai_providers.adk_remote import WorkspaceRemoteLlm
from rubik_core.ai_providers.config import PROVIDER_ENDPOINTS
from rubik_core.ai_providers.registry import IncompatibleModelRoute, ModelRoute
from rubik_core.ai_providers.runtime import ProviderRequestError
from rubik_core.tenancy.context import use_workspace

_MODELS = {
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-6-luna",
    "deepseek": "deepseek-v4-pro",
    "meta": "muse-spark-1.3",
}
_TOOL = types.FunctionDeclaration(
    name="lookup",
    description="Look up a value",
    parameters=types.Schema(
        type="OBJECT",
        properties={"query": types.Schema(type="STRING")},
        required=["query"],
    ),
)


def _route(provider: str, *, workspace: str = "test", feature: str = "chat_adk"):
    return ModelRoute(workspace, feature, provider, _MODELS[provider], "client")


def _request(*contents: types.Content) -> LlmRequest:
    return LlmRequest(
        model="unused",
        contents=list(contents)
        or [types.Content(role="user", parts=[types.Part(text="Find this")])],
        config=types.GenerateContentConfig(
            system_instruction="Be accurate",
            tools=[types.Tool(function_declarations=[_TOOL])],
        ),
    )


def _transport(monkeypatch, handler):
    real_client = httpx.AsyncClient
    mock_transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        adk_remote.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=mock_transport, **kwargs),
    )


def _authorized(monkeypatch, route: ModelRoute, key: str = "workspace-secret"):
    monkeypatch.setattr(
        adk_remote,
        "resolve_model_route",
        lambda feature, capability, default_model: route,
    )
    monkeypatch.setattr(adk_remote, "_key", lambda selected: key)


def test_nonstream_rejects_token_limit_truncation():
    with pytest.raises(ProviderRequestError, match="before completion"):
        adk_remote._openai_response(
            {
                "choices": [
                    {"message": {"content": "partial"}, "finish_reason": "length"}
                ]
            },
            "gpt-6-luna",
            set(),
        )
    with pytest.raises(ProviderRequestError, match="before completion"):
        adk_remote._anthropic_response(
            {
                "content": [{"type": "text", "text": "partial"}],
                "stop_reason": "max_tokens",
            },
            "claude-sonnet-4-6",
            set(),
        )


def test_stream_rejects_truncation_even_with_protocol_terminator():
    async def collect(parser, body, model):
        return [
            item async for item in parser(httpx.Response(200, text=body), model, set())
        ]

    openai = (
        'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n'
        "data: [DONE]\n\n"
    )
    anthropic = (
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":"partial"}}\n\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"max_tokens"}}\n\n'
        'data: {"type":"message_stop"}\n\n'
    )
    with pytest.raises(ProviderRequestError, match="before completion"):
        asyncio.run(collect(adk_remote._stream_openai, openai, "gpt-6-luna"))
    with pytest.raises(ProviderRequestError, match="before completion"):
        asyncio.run(
            collect(adk_remote._stream_anthropic, anthropic, "claude-sonnet-4-6")
        )


async def _collect(model, request, stream=False):
    return [item async for item in model.generate_content_async(request, stream)]


@pytest.mark.parametrize("provider", ["openai", "deepseek", "meta"])
def test_openai_compatible_tool_call_and_result(provider, monkeypatch):
    route = _route(provider)
    _authorized(monkeypatch, route)
    seen = []

    def handler(request: httpx.Request):
        assert str(request.url) == PROVIDER_ENDPOINTS[provider]
        assert request.headers["authorization"] == "Bearer workspace-secret"
        body = json.loads(request.content)
        seen.append(body)
        if len(seen) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "lookup",
                                            "arguments": '{"query":"this"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 3},
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Found it", "tool_calls": []}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 2},
            },
        )

    _transport(monkeypatch, handler)
    model = WorkspaceRemoteLlm.for_route(route)
    user = types.Content(role="user", parts=[types.Part(text="Find this")])
    with use_workspace("test"):
        first = asyncio.run(_collect(model, _request(user)))[0]
        call = first.content.parts[0].function_call
        assert call.name == "lookup" and call.id == "call-1"
        second = asyncio.run(
            _collect(
                model,
                _request(
                    user,
                    first.content,
                    types.Content(
                        role="user",
                        parts=[
                            types.Part(
                                function_response=types.FunctionResponse(
                                    id="call-1",
                                    name="lookup",
                                    response={"found": True},
                                )
                            )
                        ],
                    ),
                ),
            )
        )[0]
    assert second.content.parts[0].text == "Found it"
    assert seen[0]["tools"][0]["function"]["parameters"]["type"] == "object"
    assert seen[1]["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": '{"found": true}',
    }
    if provider == "deepseek":
        assert all(body["thinking"] == {"type": "disabled"} for body in seen)


def test_anthropic_tool_call_and_result(monkeypatch):
    route = _route("anthropic")
    _authorized(monkeypatch, route)
    seen = []

    def handler(request: httpx.Request):
        assert str(request.url) == PROVIDER_ENDPOINTS["anthropic"]
        assert request.headers["x-api-key"] == "workspace-secret"
        body = json.loads(request.content)
        seen.append(body)
        if len(seen) == 1:
            return httpx.Response(
                200,
                json={
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu-1",
                            "name": "lookup",
                            "input": {"query": "this"},
                        }
                    ],
                    "usage": {"input_tokens": 10, "output_tokens": 3},
                },
            )
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "Found it"}],
                "usage": {"input_tokens": 12, "output_tokens": 2},
            },
        )

    _transport(monkeypatch, handler)
    model = WorkspaceRemoteLlm.for_route(route)
    user = types.Content(role="user", parts=[types.Part(text="Find this")])
    with use_workspace("test"):
        first = asyncio.run(_collect(model, _request(user)))[0]
        assert first.content.parts[0].function_call.id == "toolu-1"
        second = asyncio.run(
            _collect(
                model,
                _request(
                    user,
                    first.content,
                    types.Content(
                        role="user",
                        parts=[
                            types.Part(
                                function_response=types.FunctionResponse(
                                    id="toolu-1",
                                    name="lookup",
                                    response={"found": True},
                                )
                            )
                        ],
                    ),
                ),
            )
        )[0]
    assert second.content.parts[0].text == "Found it"
    assert seen[0]["tools"][0]["input_schema"]["type"] == "object"
    assert seen[1]["messages"][-1]["content"][0]["tool_use_id"] == "toolu-1"


@pytest.mark.parametrize("provider", ["openai", "deepseek", "meta"])
def test_openai_compatible_stream_text_and_tool(provider, monkeypatch):
    route = _route(provider)
    _authorized(monkeypatch, route)
    stream = (
        'data: {"choices":[{"delta":{"content":"Hello "}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"there"}}]}\n\n'
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-2","function":{"name":"lookup","arguments":"{\\"query\\":"}}]}}]}\n\n'
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"now\\"}"}}]}}]}\n\n'
        'data: {"choices":[],"usage":{"prompt_tokens":8,"completion_tokens":3}}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request):
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, text=stream)

    _transport(monkeypatch, handler)
    with use_workspace("test"):
        result = asyncio.run(
            _collect(WorkspaceRemoteLlm.for_route(route), _request(), True)
        )
    assert [item.content.parts[0].text for item in result[:-1]] == [
        "Hello ",
        "there",
    ]
    final = result[-1]
    assert final.partial is False
    assert final.content.parts[0].text == "Hello there"
    assert final.content.parts[1].function_call.args == {"query": "now"}
    assert final.usage_metadata.prompt_token_count == 8


def test_anthropic_stream_text_and_tool(monkeypatch):
    route = _route("anthropic")
    _authorized(monkeypatch, route)
    stream = "\n\n".join(
        [
            'data: {"type":"message_start","message":{"usage":{"input_tokens":6}}}',
            'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}',
            'data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"toolu-2","name":"lookup","input":{}}}',
            'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"query\\":\\"now\\"}"}}',
            'data: {"type":"message_delta","usage":{"output_tokens":4}}',
            'data: {"type":"message_stop"}',
        ]
    )
    _transport(monkeypatch, lambda request: httpx.Response(200, text=stream))
    with use_workspace("test"):
        result = asyncio.run(
            _collect(WorkspaceRemoteLlm.for_route(route), _request(), True)
        )
    assert result[0].partial is True
    final = result[-1]
    assert final.content.parts[0].text == "Hello"
    assert final.content.parts[1].function_call.args == {"query": "now"}
    assert final.usage_metadata.candidates_token_count == 4


@pytest.mark.parametrize("provider", ["openai", "deepseek", "meta", "anthropic"])
def test_stream_early_eof_never_yields_a_final_answer(provider, monkeypatch):
    route = _route(provider)
    _authorized(monkeypatch, route)
    if provider == "anthropic":
        stream = "\n\n".join(
            [
                'data: {"type":"message_start","message":{"usage":{"input_tokens":6}}}',
                'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Partial answer"}}',
            ]
        )
    else:
        stream = 'data: {"choices":[{"delta":{"content":"Partial answer"}}]}\n\n'
    _transport(monkeypatch, lambda request: httpx.Response(200, text=stream))

    async def receive():
        yielded = []
        with pytest.raises(ProviderRequestError, match="ended before completion"):
            async for item in WorkspaceRemoteLlm.for_route(
                route
            ).generate_content_async(_request(), stream=True):
                yielded.append(item)
        return yielded

    with use_workspace("test"):
        yielded = asyncio.run(receive())
    assert yielded and all(item.partial is True for item in yielded)


@pytest.mark.parametrize("provider", ["openai", "deepseek", "meta", "anthropic"])
@pytest.mark.parametrize("stream", [False, True])
def test_allowed_function_subset_filters_payload_and_rejects_other_calls(
    provider, stream, monkeypatch
):
    route = _route(provider)
    _authorized(monkeypatch, route)
    request = _request()
    request.config.tools = [
        types.Tool(
            function_declarations=[
                _TOOL,
                types.FunctionDeclaration(name="lookup_other", description="Read only"),
                types.FunctionDeclaration(
                    name="dangerous_write", description="Write data"
                ),
            ]
        )
    ]
    request.config.tool_config = types.ToolConfig(
        function_calling_config=types.FunctionCallingConfig(
            mode="ANY", allowed_function_names=["lookup", "lookup_other"]
        )
    )
    seen = []

    def handler(http_request):
        body = json.loads(http_request.content)
        seen.append(body)
        names = [
            item["name"] if provider == "anthropic" else item["function"]["name"]
            for item in body["tools"]
        ]
        assert names == ["lookup", "lookup_other"]
        if provider == "anthropic":
            if stream:
                return httpx.Response(
                    200,
                    text="\n\n".join(
                        [
                            'data: {"type":"message_start","message":{"usage":{"input_tokens":1}}}',
                            'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"tool-1","name":"dangerous_write","input":{}}}',
                            'data: {"type":"message_stop"}',
                        ]
                    ),
                )
            return httpx.Response(
                200,
                json={
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "dangerous_write",
                            "input": {},
                        }
                    ]
                },
            )
        if stream:
            return httpx.Response(
                200,
                text=(
                    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1","function":{"name":"dangerous_write","arguments":"{}"}}]}}]}\n\n'
                    "data: [DONE]\n\n"
                ),
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "dangerous_write",
                                        "arguments": "{}",
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
        )

    _transport(monkeypatch, handler)
    with use_workspace("test"):
        with pytest.raises(ProviderRequestError, match="unknown tool"):
            asyncio.run(_collect(WorkspaceRemoteLlm.for_route(route), request, stream))
    assert len(seen) == 1


def test_unsupported_media_and_grounding_make_zero_requests(monkeypatch):
    route = _route("openai")
    _authorized(monkeypatch, route)
    called = []
    _transport(
        monkeypatch, lambda request: called.append(request) or httpx.Response(200)
    )
    request = _request(
        types.Content(
            role="user",
            parts=[
                types.Part(inline_data=types.Blob(mime_type="image/png", data=b"image"))
            ],
        )
    )
    with use_workspace("test"):
        with pytest.raises(IncompatibleModelRoute, match="attached media"):
            asyncio.run(_collect(WorkspaceRemoteLlm.for_route(route), request))
    request = _request()
    request.config.tools.append(types.Tool(google_search=types.GoogleSearch()))
    with use_workspace("test"):
        with pytest.raises(IncompatibleModelRoute, match="built-in tool"):
            asyncio.run(_collect(WorkspaceRemoteLlm.for_route(route), request))
    assert called == []


def test_cross_workspace_refused_before_key_or_network(monkeypatch):
    route = _route("openai", workspace="other")
    called = []
    monkeypatch.setattr(adk_remote, "_key", lambda selected: called.append("key"))
    monkeypatch.setattr(
        adk_remote,
        "resolve_model_route",
        lambda feature, capability, default_model: called.append("route"),
    )
    with use_workspace("test"):
        with pytest.raises(IncompatibleModelRoute, match="Workspace AI route"):
            asyncio.run(_collect(WorkspaceRemoteLlm.for_route(route), _request()))
    assert called == []


def test_provider_error_is_bounded_and_has_no_response_body(monkeypatch):
    route = _route("openai")
    _authorized(monkeypatch, route, key="secret-token")
    _transport(
        monkeypatch,
        lambda request: httpx.Response(
            401, text="key=secret-token private customer transcript"
        ),
    )
    with use_workspace("test"):
        with pytest.raises(ProviderRequestError) as exc:
            asyncio.run(_collect(WorkspaceRemoteLlm.for_route(route), _request()))
    assert "401" in str(exc.value)
    assert "secret-token" not in str(exc.value)
    assert "transcript" not in str(exc.value)


@pytest.mark.parametrize("provider", ["openai", "deepseek", "meta", "anthropic"])
def test_adk_runner_executes_remote_tool_loop(provider, monkeypatch):
    route = _route(provider)
    _authorized(monkeypatch, route)
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        if len(seen) == 1:
            if provider == "anthropic":
                return httpx.Response(
                    200,
                    json={
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "call_123",
                                "name": "lookup",
                                "input": {"query": "x"},
                            }
                        ],
                        "usage": {"input_tokens": 4, "output_tokens": 2},
                    },
                )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "call_123",
                                        "type": "function",
                                        "function": {
                                            "name": "lookup",
                                            "arguments": '{"query":"x"}',
                                        },
                                    }
                                ]
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 4, "completion_tokens": 2},
                },
            )
        if provider == "anthropic":
            return httpx.Response(
                200,
                json={
                    "content": [{"type": "text", "text": "Found x"}],
                    "usage": {"input_tokens": 5, "output_tokens": 3},
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Found x"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3},
            },
        )

    _transport(monkeypatch, handler)

    def lookup(query: str) -> dict:
        return {"found": query}

    async def run():
        sessions = InMemorySessionService()
        agent = Agent(
            name="remote_tool_contract",
            model=WorkspaceRemoteLlm.for_route(route),
            instruction="Use lookup.",
            tools=[lookup],
        )
        runner = Runner(
            agent=agent, app_name="remote_tool_contract", session_service=sessions
        )
        session = await sessions.create_session(
            app_name="remote_tool_contract", user_id="test-user"
        )
        return [
            event
            async for event in runner.run_async(
                user_id="test-user",
                session_id=session.id,
                new_message=types.Content(
                    role="user", parts=[types.Part(text="Find x")]
                ),
            )
        ]

    with use_workspace("test"):
        events = asyncio.run(run())
    assert len(seen) == 2
    if provider == "anthropic":
        assert seen[1]["messages"][-1]["content"][0]["type"] == "tool_result"
    else:
        assert seen[1]["messages"][-1]["role"] == "tool"
    assert events[-1].is_final_response()
    assert events[-1].content.parts[0].text == "Found x"


@pytest.mark.parametrize("provider", ["openai", "deepseek", "meta", "anthropic"])
def test_adk_runner_emits_remote_stream_chunks(provider, monkeypatch):
    route = _route(provider)
    _authorized(monkeypatch, route)
    if provider == "anthropic":
        stream = "\n\n".join(
            [
                'data: {"type":"message_start","message":{"usage":{"input_tokens":5}}}',
                'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello "}}',
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"world"}}',
                'data: {"type":"message_delta","usage":{"output_tokens":2}}',
                'data: {"type":"message_stop"}',
            ]
        )
    else:
        stream = (
            'data: {"choices":[{"delta":{"content":"Hello "}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"world"}}]}\n\n'
            'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n'
            "data: [DONE]\n\n"
        )
    _transport(monkeypatch, lambda request: httpx.Response(200, text=stream))

    async def run():
        sessions = InMemorySessionService()
        agent = Agent(
            name="remote_stream_contract",
            model=WorkspaceRemoteLlm.for_route(route),
            instruction="Say hello.",
        )
        runner = Runner(
            agent=agent, app_name="remote_stream_contract", session_service=sessions
        )
        session = await sessions.create_session(
            app_name="remote_stream_contract", user_id="test-user"
        )
        return [
            event
            async for event in runner.run_async(
                user_id="test-user",
                session_id=session.id,
                new_message=types.Content(role="user", parts=[types.Part(text="Hi")]),
                run_config=RunConfig(streaming_mode=StreamingMode.SSE),
            )
        ]

    with use_workspace("test"):
        events = asyncio.run(run())
    assert [event.content.parts[0].text for event in events if event.partial] == [
        "Hello ",
        "world",
    ]
    assert events[-1].is_final_response()
    assert events[-1].content.parts[0].text == "Hello world"


@pytest.mark.parametrize("provider", ["openai", "deepseek", "meta", "anthropic"])
@pytest.mark.parametrize("stream", [False, True])
def test_connection_probe_requires_tool_roundtrip(provider, stream, monkeypatch):
    route = _route(provider)
    monkeypatch.setattr(adk_remote, "_key", lambda selected: "workspace-secret")
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if provider == "anthropic":
            prompt = body["messages"][0]["content"][0]["text"]
        else:
            prompt = body["messages"][0]["content"]
        nonce = re.search(r"nonce ([0-9a-f]{12})", prompt).group(1)
        if len(calls) == 1:
            if provider == "anthropic":
                if stream:
                    return httpx.Response(
                        200,
                        text="\n\n".join(
                            [
                                'data: {"type":"message_start","message":{"usage":{"input_tokens":1}}}',
                                'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"tool-1","name":"cubie_connection_probe","input":{}}}',
                                'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"nonce\\":\\"'
                                + nonce
                                + '\\"}"}}',
                                'data: {"type":"message_delta","usage":{"output_tokens":1}}',
                                'data: {"type":"message_stop"}',
                            ]
                        ),
                    )
                return httpx.Response(
                    200,
                    json={
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "tool-1",
                                "name": "cubie_connection_probe",
                                "input": {"nonce": nonce},
                            }
                        ],
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    },
                )
            if stream:
                return httpx.Response(
                    200,
                    text=(
                        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1","function":{"name":"cubie_connection_probe","arguments":"{\\"nonce\\":\\"'
                        + nonce
                        + '\\"}"}}]}}]}\n\n'
                        'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\n'
                        "data: [DONE]\n\n"
                    ),
                )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "cubie_connection_probe",
                                            "arguments": json.dumps({"nonce": nonce}),
                                        },
                                    }
                                ]
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            )
        if stream:
            if provider == "anthropic":
                return httpx.Response(
                    200,
                    text="\n\n".join(
                        [
                            'data: {"type":"message_start","message":{"usage":{"input_tokens":1}}}',
                            'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
                            'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ready"}}',
                            'data: {"type":"message_delta","usage":{"output_tokens":1}}',
                            'data: {"type":"message_stop"}',
                        ]
                    ),
                )
            return httpx.Response(
                200,
                text=(
                    'data: {"choices":[{"delta":{"content":"ready"}}]}\n\n'
                    'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\n'
                    "data: [DONE]\n\n"
                ),
            )
        if provider == "anthropic":
            return httpx.Response(
                200,
                json={
                    "content": [{"type": "text", "text": "ready"}],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ready"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    _transport(monkeypatch, handler)
    with use_workspace("test"):
        asyncio.run(adk_remote.test_remote_adk_connection(route, stream=stream))
    assert len(calls) == 2
    assert calls[0]["tool_choice"] != calls[1]["tool_choice"]
    if provider == "openai":
        assert all(body["reasoning_effort"] == "none" for body in calls)


def test_full_chat_coordinator_tool_schema_translates_for_remote_provider(monkeypatch):
    from rubik_core.agent_runtime.coordinator import build_coordinator
    from rubik_core.company.profile import CompanyProfile

    route = _route("openai")
    _authorized(monkeypatch, route)
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Ready"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            },
        )

    _transport(monkeypatch, handler)

    async def run():
        agent = build_coordinator(
            "employee@acme.example",
            user_department="Marketing",
            model_route=route,
            company_profile=CompanyProfile(),
        )
        sessions = InMemorySessionService()
        runner = Runner(
            agent=agent, app_name="full_chat_tool_contract", session_service=sessions
        )
        session = await sessions.create_session(
            app_name="full_chat_tool_contract",
            user_id="employee@acme.example",
        )
        return [
            event
            async for event in runner.run_async(
                user_id="employee@acme.example",
                session_id=session.id,
                new_message=types.Content(role="user", parts=[types.Part(text="Hi")]),
            )
        ]

    with use_workspace("test"):
        events = asyncio.run(run())
    assert events[-1].is_final_response()
    assert events[-1].content.parts[0].text == "Ready"
    assert len(seen[0]["tools"]) >= 20
    assert all(tool["type"] == "function" for tool in seen[0]["tools"])

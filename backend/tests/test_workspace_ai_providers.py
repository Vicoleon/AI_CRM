"""A workspace route can spend only its own credential and never fail over."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import delete

from rubik_core.ai_providers.config import PROVIDER_ENDPOINTS, model_capabilities
from rubik_core.ai_providers.registry import (
    IncompatibleModelRoute,
    ModelRoute,
    configure_provider,
    provider_status,
    resolve_model_route,
    revoke_provider,
    set_model_route,
)
from rubik_core.ai_providers.runtime import ProviderRequestError, generate_text
from rubik_core.config import AppMode, settings
from rubik_core.control.db import control_session
from rubik_core.control.models import Workspace, WorkspaceConnector
from rubik_core.tenancy.context import use_workspace


@pytest.fixture
def ai_workspaces(tmp_path, monkeypatch):
    from rubik_core.ai_providers import runtime

    monkeypatch.setattr(runtime, "_record", lambda *a, **kw: None)
    monkeypatch.setattr(settings, "LOCAL_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "APP_MODE", AppMode.LOCAL)
    monkeypatch.setattr(settings, "CLOUDSQL_CONNECTION_NAME", "")
    with control_session() as session:
        session.add_all(
            [
                Workspace(
                    slug="ai-acme",
                    name="AI Acme",
                    status="active",
                    google_service_account="cubie-ai-acme@platform-test.iam.gserviceaccount.com",
                ),
                Workspace(
                    slug="ai-other",
                    name="AI Other",
                    status="active",
                    google_service_account="cubie-ai-other@platform-test.iam.gserviceaccount.com",
                ),
            ]
        )
        session.commit()
    yield
    with control_session() as session:
        session.execute(
            delete(WorkspaceConnector).where(
                WorkspaceConnector.workspace_slug.in_(("ai-acme", "ai-other"))
            )
        )
        for slug in ("ai-acme", "ai-other"):
            session.delete(session.get(Workspace, slug))
        session.commit()


def test_same_model_uses_distinct_workspace_keys_and_fixed_endpoint(
    ai_workspaces, monkeypatch
):
    from rubik_core.ai_providers import runtime

    calls = []

    def post(url, *, headers, json, timeout, allow_redirects):
        calls.append((url, headers["Authorization"], json["model"], allow_redirects))
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "choices": [{"message": {"content": "hello"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    monkeypatch.setattr(runtime.requests, "post", post)
    for slug, key in (("ai-acme", "acme-key"), ("ai-other", "other-key")):
        with use_workspace(slug):
            configure_provider("openai", api_key=key)
            set_model_route("ai_priorities", "openai", "gpt-6-luna")
            answer = generate_text("ai_priorities", "Say hello", default_model="unused")
            assert answer.text == "hello"
            assert answer.billing_owner == "client"
    assert calls == [
        (PROVIDER_ENDPOINTS["openai"], "Bearer acme-key", "gpt-6-luna", False),
        (PROVIDER_ENDPOINTS["openai"], "Bearer other-key", "gpt-6-luna", False),
    ]


def test_claude_and_muse_routes_keep_workspace_keys_separate(
    ai_workspaces, monkeypatch
):
    from rubik_core.ai_providers import runtime

    calls = []

    def post(url, *, headers, json, timeout, allow_redirects):
        calls.append((url, dict(headers), json["model"], allow_redirects))
        if url == PROVIDER_ENDPOINTS["anthropic"]:
            return SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "content": [{"type": "text", "text": "Claude reply"}],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            )
        assert url == PROVIDER_ENDPOINTS["meta"]
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "choices": [{"message": {"content": "Muse reply"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    monkeypatch.setattr(runtime.requests, "post", post)
    with use_workspace("ai-acme"):
        configure_provider("anthropic", api_key="claude-acme-key")
        set_model_route("l10_summary_email", "anthropic", "claude-sonnet-4-6")
        assert (
            generate_text("l10_summary_email", "Reply", default_model="unused").text
            == "Claude reply"
        )
    with use_workspace("ai-other"):
        configure_provider("meta", api_key="muse-other-key")
        set_model_route("l10_summary_email", "meta", "muse-spark-1.3")
        assert (
            generate_text("l10_summary_email", "Reply", default_model="unused").text
            == "Muse reply"
        )

    assert len(calls) == 2
    assert calls[0][0] == PROVIDER_ENDPOINTS["anthropic"]
    assert calls[0][1]["x-api-key"] == "claude-acme-key"
    assert "Authorization" not in calls[0][1]
    assert calls[0][2:] == ("claude-sonnet-4-6", False)
    assert calls[1][0] == PROVIDER_ENDPOINTS["meta"]
    assert calls[1][1]["Authorization"] == "Bearer muse-other-key"
    assert "x-api-key" not in calls[1][1]
    assert calls[1][2:] == ("muse-spark-1.3", False)


def test_json_connection_probe_checks_json_response(ai_workspaces, monkeypatch):
    from rubik_core.ai_providers import runtime

    replies = ['{"ok":true}', "plain text"]
    payloads = []

    def post(url, *, headers, json, timeout, allow_redirects):
        assert url == PROVIDER_ENDPOINTS["meta"]
        assert headers["Authorization"] == "Bearer muse-key"
        assert allow_redirects is False
        payloads.append(json)
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "choices": [{"message": {"content": replies.pop(0)}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    monkeypatch.setattr(runtime.requests, "post", post)
    monkeypatch.setattr(
        runtime,
        "_genai_client",
        lambda *a, **kw: pytest.fail("Managed Vertex fallback is forbidden"),
    )
    with use_workspace("ai-acme"):
        configure_provider("meta", api_key="muse-key")
        runtime.test_connection("meta", "muse-spark-1.3", capability="json")
        with pytest.raises(ProviderRequestError, match="JSON object"):
            runtime.test_connection("meta", "muse-spark-1.3", capability="json")
        with pytest.raises(IncompatibleModelRoute, match="cannot run json"):
            runtime.test_connection("anthropic", "claude-sonnet-4-6", capability="json")
    assert len(payloads) == 2
    assert all(
        payload["response_format"] == {"type": "json_object"} for payload in payloads
    )


def test_revoked_or_missing_key_does_not_fall_back_to_managed_vertex(
    ai_workspaces, monkeypatch
):
    from rubik_core.ai_providers import runtime

    calls = []
    monkeypatch.setattr(runtime.requests, "post", lambda *a, **kw: calls.append(a))
    monkeypatch.setattr(
        runtime,
        "_genai_client",
        lambda *a, **kw: pytest.fail("Managed Vertex fallback is forbidden"),
    )
    with use_workspace("ai-acme"):
        configure_provider("meta", api_key="muse-key")
        set_model_route("l10_summary_email", "meta", "muse-spark-1.3")
        revoke_provider("meta")
        with pytest.raises(IncompatibleModelRoute, match="disconnected"):
            generate_text("l10_summary_email", "hello", default_model="gemini")
        assert provider_status()["meta"]["credential_configured"] is False
    assert not calls


def test_insights_summary_broken_key_is_visible_and_never_calls_managed_vertex(
    ai_workspaces, monkeypatch
):
    from rubik_core.ai_providers import runtime
    from rubik_core.insights import summarize

    monkeypatch.setattr(summarize, "build_prompt", lambda inp: "Summarize this view")
    monkeypatch.setattr(
        runtime,
        "_genai_client",
        lambda *a, **kw: pytest.fail("Managed Vertex fallback is forbidden"),
    )
    calls = []

    def post(url, *, headers, json, timeout, allow_redirects):
        calls.append((url, headers["Authorization"]))
        return SimpleNamespace(status_code=401, text="secret provider detail")

    monkeypatch.setattr(runtime.requests, "post", post)
    payload = {"title": "Adoption", "scope": {"labels": ["Sales"]}, "kpis": []}
    with use_workspace("ai-acme"):
        configure_provider("openai", api_key="client-key")
        set_model_route("insights_summary", "openai", "gpt-6-luna")
        unavailable = summarize.summarize_payload(payload)
        assert unavailable["available"] is False
        assert "admin" in unavailable["reason"].lower()
        assert "secret provider detail" not in unavailable["reason"]
        revoke_provider("openai")
        revoked = summarize.summarize_payload(payload)
        assert revoked["available"] is False
    assert calls == [(PROVIDER_ENDPOINTS["openai"], "Bearer client-key")]


def test_capabilities_fail_before_any_model_request(ai_workspaces, monkeypatch):
    from rubik_core.ai_providers import runtime

    monkeypatch.setattr(
        runtime.requests,
        "post",
        lambda *a, **kw: pytest.fail("No provider request should be sent"),
    )
    with use_workspace("ai-acme"):
        configure_provider("anthropic", api_key="claude-key")
        with pytest.raises(IncompatibleModelRoute, match="cannot run json"):
            set_model_route("ai_priorities", "anthropic", "claude-sonnet-4-6")
        set_model_route("l10_summary_email", "anthropic", "claude-sonnet-4-6")
        assert (
            resolve_model_route(
                "l10_summary_email", "text", default_model="gemini"
            ).provider
            == "anthropic"
        )


def test_embedding_route_requires_a_prepared_index(ai_workspaces):
    with use_workspace("ai-acme"):
        with pytest.raises(IncompatibleModelRoute, match="Prepare and activate"):
            set_model_route("embedding_index", "vertex_managed", "text-embedding-004")


def test_tool_connection_probe_requires_round_trip(ai_workspaces, monkeypatch):
    from rubik_core.ai_providers import runtime

    calls = []

    def generate(*, model, contents, config):
        calls.append(("generate", model, contents, config))
        if len(calls) == 1:
            return SimpleNamespace(
                function_calls=[SimpleNamespace(name="cubie_probe", args={})]
            )
        return SimpleNamespace(text="OK")

    def generate_stream(*, model, contents, config):
        calls.append(("stream", model, contents, config))
        if len(calls) == 3:
            return iter(
                [
                    SimpleNamespace(
                        function_calls=[SimpleNamespace(name="cubie_probe", args={})]
                    )
                ]
            )
        return iter([SimpleNamespace(text="OK")])

    client = SimpleNamespace(
        models=SimpleNamespace(
            generate_content=generate,
            generate_content_stream=generate_stream,
        )
    )
    monkeypatch.setattr(runtime, "_genai_client", lambda *a, **kw: client)
    with use_workspace("ai-acme"):
        configure_provider("gemini", api_key="workspace-gemini-key")
        runtime.test_connection("gemini", "gemini-2.5-pro", capability="tools")
        runtime.test_connection("gemini", "gemini-2.5-pro", capability="tools_stream")
    assert [call[0] for call in calls] == [
        "generate",
        "generate",
        "stream",
        "stream",
    ]
    for call in (calls[1], calls[3]):
        conversation = call[2]
        assert [part.role for part in conversation] == ["user", "model", "user"]
        assert conversation[-1].parts[0].function_response.name == "cubie_probe"


def test_tool_connection_probe_rejects_plain_text(ai_workspaces, monkeypatch):
    from rubik_core.ai_providers import runtime

    monkeypatch.setattr(
        runtime,
        "_genai_client",
        lambda *a, **kw: SimpleNamespace(
            models=SimpleNamespace(
                generate_content=lambda **kw: SimpleNamespace(
                    text="I can call tools", function_calls=[]
                )
            )
        ),
    )
    with use_workspace("ai-acme"):
        configure_provider("gemini", api_key="workspace-gemini-key")
        with pytest.raises(ProviderRequestError, match="did not call"):
            runtime.test_connection("gemini", "gemini-2.5-pro", capability="tools")


def test_remote_tool_connection_probe_is_awaited(ai_workspaces, monkeypatch):
    from rubik_core.ai_providers import adk_remote, runtime

    calls = []

    async def probe(route, *, stream):
        calls.append((route.provider, stream))
        raise ProviderRequestError("Selected provider tool probe failed")

    monkeypatch.setattr(adk_remote, "test_remote_adk_connection", probe)
    with use_workspace("ai-acme"):
        configure_provider("anthropic", api_key="claude-key")
        with pytest.raises(ProviderRequestError, match="tool probe failed"):
            runtime.test_connection(
                "anthropic", "claude-sonnet-4-6", capability="tools_stream"
            )
    assert calls == [("anthropic", True)]


def test_selected_google_sdk_property_error_is_redacted(ai_workspaces, monkeypatch):
    from rubik_core.ai_providers import runtime

    class MalformedResponse:
        @property
        def text(self):
            raise RuntimeError("PRIVATE-PROMPT-IN-SDK-ERROR")

    monkeypatch.setattr(
        runtime,
        "_genai_client",
        lambda *a, **kw: SimpleNamespace(
            models=SimpleNamespace(generate_content=lambda **kw: MalformedResponse())
        ),
    )
    with use_workspace("ai-acme"):
        configure_provider("gemini", api_key="workspace-gemini-key")
        set_model_route("l10_summary_email", "gemini", "gemini-2.5-pro")
        with pytest.raises(ProviderRequestError) as text_error:
            runtime.generate_text(
                "l10_summary_email", "private prompt", default_model="gemini-2.5-pro"
            )
        assert "PRIVATE-PROMPT-IN-SDK-ERROR" not in str(text_error.value)

        set_model_route("people_web_search", "gemini", "gemini-2.5-pro")
        route = resolve_model_route(
            "people_web_search", "grounding", default_model="gemini-2.5-pro"
        )
        with pytest.raises(ProviderRequestError) as grounded_error:
            runtime.generate_grounded_text_on_route(route, "private prompt")
        assert "PRIVATE-PROMPT-IN-SDK-ERROR" not in str(grounded_error.value)


def test_grounded_client_route_uses_search_without_json_mime(
    ai_workspaces, monkeypatch
):
    from rubik_core.ai_providers import runtime

    calls = []
    response = SimpleNamespace(
        text='{"summary":"ready"}',
        candidates=[
            SimpleNamespace(
                grounding_metadata=SimpleNamespace(
                    grounding_chunks=[
                        SimpleNamespace(
                            web=SimpleNamespace(uri="https://example.com/a")
                        )
                    ]
                )
            )
        ],
    )
    monkeypatch.setattr(
        runtime,
        "_genai_client",
        lambda route, key: SimpleNamespace(
            models=SimpleNamespace(
                generate_content=lambda **kw: calls.append((route, key, kw)) or response
            )
        ),
    )
    with use_workspace("ai-acme"):
        configure_provider("gemini", api_key="client-gemini-key")
        set_model_route("xray_grounding", "gemini", "gemini-2.5-pro")
        route = resolve_model_route(
            "xray_grounding", "grounding", default_model="managed-default"
        )
        result = runtime.generate_grounded_text_on_route(
            route, "Research this company", system_instruction="Use sources"
        )

    assert result.text == '{"summary":"ready"}'
    assert result.citation_urls == ("https://example.com/a",)
    assert calls[0][1] == "client-gemini-key"
    config = calls[0][2]["config"]
    assert config.tools[0].google_search is not None
    assert config.response_mime_type is None


def test_grounded_client_failure_never_uses_managed_vertex(ai_workspaces, monkeypatch):
    from rubik_core.ai_providers import runtime

    def client(route, key):
        assert route.provider == "gemini"
        assert key == "client-gemini-key"
        return SimpleNamespace(
            models=SimpleNamespace(
                generate_content=lambda **kw: (_ for _ in ()).throw(
                    RuntimeError("provider echoed secret customer data")
                )
            )
        )

    monkeypatch.setattr(runtime, "_genai_client", client)
    with use_workspace("ai-acme"):
        configure_provider("gemini", api_key="client-gemini-key")
        set_model_route("xray_grounding", "gemini", "gemini-2.5-pro")
        route = resolve_model_route(
            "xray_grounding", "grounding", default_model="managed-default"
        )
        with pytest.raises(
            ProviderRequestError, match="grounding request failed"
        ) as exc:
            runtime.generate_grounded_text_on_route(route, "Research this company")
    assert "secret customer data" not in str(exc.value)


def test_media_connection_probe_exercises_json_and_audio_tools(
    ai_workspaces, monkeypatch
):
    from rubik_core.ai_providers import runtime

    seen = []

    def generate(*, model, contents, config=None):
        seen.append((model, contents, config))
        if config and config.tool_config:
            if len([call for call in seen if call[2] and call[2].tool_config]) == 1:
                return SimpleNamespace(
                    function_calls=[SimpleNamespace(name="cubie_probe", args={})]
                )
            return SimpleNamespace(text="OK")
        return SimpleNamespace(text='{"summary":"ready"}')

    monkeypatch.setattr(
        runtime,
        "_genai_client",
        lambda route, key: SimpleNamespace(
            models=SimpleNamespace(generate_content=generate)
        ),
    )
    with use_workspace("ai-acme"):
        configure_provider("gemini", api_key="client-gemini-key")
        runtime.test_connection("gemini", "gemini-2.5-pro", capability="vision_json")
        runtime.test_connection("gemini", "gemini-2.5-pro", capability="audio_json")
        runtime.test_connection("gemini", "gemini-2.5-pro", capability="audio_tools")

    assert seen[0][2].response_mime_type == "application/json"
    assert seen[0][1][1].inline_data.mime_type == "image/png"
    assert seen[1][2].response_mime_type == "application/json"
    assert seen[1][1][1].inline_data.mime_type == "application/pdf"
    assert seen[2][2].response_mime_type == "application/json"
    assert seen[2][1][1].inline_data.mime_type == "audio/wav"
    assert seen[3][1].parts[1].inline_data.mime_type == "audio/wav"
    assert seen[4][2].tool_config.function_calling_config.mode.value == "NONE"


def test_native_media_route_revalidates_before_selected_async_call(
    ai_workspaces, monkeypatch
):
    from rubik_core.ai_providers import runtime

    observed = []

    async def generate(**kwargs):
        observed.append(kwargs)
        return SimpleNamespace(text='{"summary":"ready"}')

    monkeypatch.setattr(
        runtime,
        "_genai_client",
        lambda route, key: SimpleNamespace(
            aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate))
        ),
    )
    with use_workspace("ai-acme"):
        configure_provider("gemini", api_key="client-gemini-key")
        set_model_route("chat_file_vision", "gemini", "gemini-2.5-pro")
        route = resolve_model_route(
            "chat_file_vision", "vision_json", default_model="managed-default"
        )
        result = asyncio.run(
            runtime.generate_native_content_async_on_route(
                route, ["prompt"], capability="vision_json"
            )
        )
    assert result.text == '{"summary":"ready"}'
    assert observed[0]["model"] == "gemini-2.5-pro"


def test_unregistered_workload_rejects_injected_provider_route(ai_workspaces):
    with use_workspace("ai-acme"):
        configure_provider("openai", api_key="client-key")
        with control_session() as session:
            session.add(
                WorkspaceConnector(
                    workspace_slug="ai-acme",
                    connector="ai_routes",
                    status="connected",
                    config={
                        "features": {
                            "unregistered_workload": {
                                "provider": "openai",
                                "model": "gpt-6-luna",
                            }
                        }
                    },
                )
            )
            session.commit()
        with pytest.raises(IncompatibleModelRoute, match="not available"):
            resolve_model_route("unregistered_workload", "text", default_model="gemini")


def test_rejects_unknown_provider_model_and_client_owned_vertex_key(ai_workspaces):
    with use_workspace("ai-acme"):
        with pytest.raises(ValueError, match="Unsupported AI provider"):
            configure_provider("custom", api_key="key")
        with pytest.raises(ValueError, match="verified provider catalog"):
            set_model_route("ai_priorities", "openai", "http://169.254.169.254/latest")
        with pytest.raises(ValueError, match="service identity"):
            configure_provider("vertex_client", api_key="a-browser-key")
    assert model_capabilities("meta", "muse-spark-1.3") == frozenset(
        {"text", "json", "tools", "stream", "tools_stream"}
    )


def test_client_vertex_cannot_impersonate_another_workspaces_identity(ai_workspaces):
    with use_workspace("ai-acme"):
        with pytest.raises(PermissionError, match="this workspace's provisioned"):
            configure_provider(
                "vertex_client",
                project="other-client-project",
                region="us-central1",
                service_identity="cubie-ai-other@platform-test.iam.gserviceaccount.com",
            )
        assert provider_status()["vertex_client"]["connected"] is False
        configure_provider(
            "vertex_client",
            project="acme-client-project",
            region="us-central1",
            service_identity="cubie-ai-acme@platform-test.iam.gserviceaccount.com",
        )
        assert (
            resolve_model_route(
                "l10_summary_email", "text", default_model="gemini-2.5-pro"
            ).provider
            == "vertex_managed"
        )


def test_gemini_api_route_explicitly_disables_vertex_env_mode(monkeypatch):
    from rubik_core.ai_providers import runtime

    observed = []
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    monkeypatch.setattr(
        runtime.genai, "Client", lambda **kwargs: observed.append(kwargs)
    )
    route = ModelRoute(
        "ai-acme", "l10_summary_email", "gemini", "gemini-2.5-pro", "client"
    )
    runtime._genai_client(route, "workspace-gemini-key")
    assert observed[0]["vertexai"] is False
    assert observed[0]["api_key"] == "workspace-gemini-key"


@pytest.mark.parametrize(
    ("provider", "model"),
    (
        ("anthropic", "claude-sonnet-4-6"),
        ("deepseek", "deepseek-v4-pro"),
        ("openai", "gpt-6-luna"),
        ("meta", "muse-spark-1.3"),
    ),
)
def test_remote_provider_receives_requested_temperature(monkeypatch, provider, model):
    from rubik_core.ai_providers import runtime

    sent = []

    def post_json(url, headers, payload):
        sent.append((url, payload))
        if provider == "anthropic":
            return {
                "content": [{"type": "text", "text": "OK"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        return {
            "choices": [{"message": {"content": "OK"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    monkeypatch.setattr(runtime, "_post_json", post_json)
    monkeypatch.setattr(runtime, "_record", lambda *a, **kw: None)
    route = ModelRoute("ai-acme", "l10_summary_email", provider, model, "client")
    assert (
        runtime._remote_text(
            route, "workspace-key", "prompt", json_mode=False, temperature=0.3
        ).text
        == "OK"
    )
    assert sent[0][0] == PROVIDER_ENDPOINTS[provider]
    assert sent[0][1]["temperature"] == 0.3
    if provider == "openai":
        # GPT-6 Luna accepts Chat Completions sampling only with reasoning off.
        assert sent[0][1]["reasoning_effort"] == "none"
    else:
        assert "reasoning_effort" not in sent[0][1]


def test_openai_default_omits_sampling_fields_and_unverified_model_fails(monkeypatch):
    from rubik_core.ai_providers import runtime

    sent = []

    def post_json(url, headers, payload):
        sent.append(payload)
        return {
            "choices": [{"message": {"content": "OK"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    monkeypatch.setattr(runtime, "_post_json", post_json)
    monkeypatch.setattr(runtime, "_record", lambda *a, **kw: None)
    route = ModelRoute("ai-acme", "l10_summary_email", "openai", "gpt-6-luna", "client")
    assert (
        runtime._remote_text(route, "workspace-key", "prompt", json_mode=False).text
        == "OK"
    )
    assert "temperature" not in sent[0]
    assert "reasoning_effort" not in sent[0]

    unknown_model = ModelRoute(
        "ai-acme", "l10_summary_email", "openai", "future-model", "client"
    )
    with pytest.raises(IncompatibleModelRoute, match="not verified"):
        runtime._remote_text(
            unknown_model, "workspace-key", "prompt", json_mode=False, temperature=0.3
        )
    assert len(sent) == 1


def test_google_provider_receives_requested_temperature(monkeypatch):
    from rubik_core.ai_providers import runtime

    calls = []

    def generate_content(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(text='{"ok":true}', usage_metadata=None)

    monkeypatch.setattr(runtime, "_key", lambda route: "workspace-key")
    monkeypatch.setattr(
        runtime,
        "_genai_client",
        lambda route, key: SimpleNamespace(
            models=SimpleNamespace(generate_content=generate_content)
        ),
    )
    monkeypatch.setattr(runtime, "_record", lambda *a, **kw: None)
    route = ModelRoute("ai-acme", "ai_priorities", "gemini", "gemini-2.5-pro", "client")
    result = runtime._generate_with_route(
        route,
        "prompt",
        json_mode=True,
        system_instruction="Return JSON",
        temperature=0.2,
    )
    assert result.text == '{"ok":true}'
    assert calls[0]["config"].temperature == 0.2
    assert calls[0]["config"].system_instruction == "Return JSON"
    assert calls[0]["config"].response_mime_type == "application/json"


def test_temperature_rejects_invalid_values_before_route_or_provider_call(monkeypatch):
    from rubik_core.ai_providers import runtime

    monkeypatch.setattr(
        runtime,
        "resolve_model_route",
        lambda *a, **kw: pytest.fail("Invalid temperature resolved a route"),
    )
    for value in (True, "0.2", -0.1, 1.1, 10**1000, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="Temperature"):
            runtime.generate_text(
                "l10_summary_email", "prompt", default_model="gemini", temperature=value
            )


def test_temperature_flows_through_workspace_and_pinned_routes(
    ai_workspaces, monkeypatch
):
    from rubik_core.ai_providers import runtime

    sent = []

    def post(url, *, headers, json, timeout, allow_redirects):
        sent.append((url, json))
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "choices": [{"message": {"content": "OK"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    monkeypatch.setattr(runtime.requests, "post", post)
    with use_workspace("ai-acme"):
        configure_provider("meta", api_key="workspace-key")
        set_model_route("l10_summary_email", "meta", "muse-spark-1.3")
        route = resolve_model_route(
            "l10_summary_email", "text", default_model="gemini-2.5-pro"
        )
        assert (
            generate_text(
                "l10_summary_email",
                "first",
                default_model="gemini-2.5-pro",
                temperature=0.4,
            ).text
            == "OK"
        )
        assert (
            runtime.generate_text_on_route(route, "second", temperature=0.6).text
            == "OK"
        )
    assert [payload["temperature"] for _, payload in sent] == [0.4, 0.6]
    assert all(url == PROVIDER_ENDPOINTS["meta"] for url, _ in sent)


@pytest.mark.parametrize(
    ("provider", "model", "usage", "prompt", "cached"),
    [
        (
            "openai",
            "gpt-6-luna",
            {
                "prompt_tokens": 100,
                "completion_tokens": 4,
                "prompt_tokens_details": {"cached_tokens": 80},
            },
            100,
            80,
        ),
        (
            "deepseek",
            "deepseek-v4-pro",
            {
                "prompt_tokens": 100,
                "completion_tokens": 4,
                "prompt_cache_hit_tokens": 75,
            },
            100,
            75,
        ),
        (
            "anthropic",
            "claude-sonnet-4-6",
            {
                "input_tokens": 20,
                "output_tokens": 4,
                "cache_read_input_tokens": 70,
                "cache_creation_input_tokens": 10,
            },
            100,
            70,
        ),
    ],
)
def test_provider_cache_usage_is_recorded_at_discounted_rate(
    monkeypatch, provider, model, usage, prompt, cached
):
    from rubik_core.ai_providers import runtime

    recorded = []
    monkeypatch.setattr(
        runtime, "_record", lambda *args, **kwargs: recorded.append((args, kwargs))
    )
    body = (
        {"content": [{"type": "text", "text": "OK"}], "usage": usage}
        if provider == "anthropic"
        else {"choices": [{"message": {"content": "OK"}}], "usage": usage}
    )
    monkeypatch.setattr(
        runtime.requests,
        "post",
        lambda *a, **kw: SimpleNamespace(status_code=200, json=lambda: body),
    )
    route = ModelRoute("ai-acme", "l10_summary_email", provider, model, "client")
    assert runtime._remote_text(route, "secret", "prompt", json_mode=False).text == "OK"
    assert recorded[0][0][1:] == (prompt, 4)
    assert recorded[0][1]["cached"] == cached


def test_provider_failure_omits_key_response_body_and_never_calls_vertex(
    ai_workspaces, monkeypatch
):
    from rubik_core.ai_providers import runtime

    secret = "client-secret-do-not-echo"
    monkeypatch.setattr(
        runtime.requests,
        "post",
        lambda *a, **kw: SimpleNamespace(
            status_code=401, text=f"bad key: {secret}", json=lambda: {}
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_genai_client",
        lambda *a, **kw: pytest.fail("Managed Vertex fallback is forbidden"),
    )
    with use_workspace("ai-acme"):
        configure_provider("meta", api_key=secret)
        set_model_route("l10_summary_email", "meta", "muse-spark-1.3")
        with pytest.raises(RuntimeError) as error:
            generate_text("l10_summary_email", "summary", default_model="gemini")
    assert str(error.value) == "AI provider returned HTTP 401"
    assert secret not in str(error.value)


@pytest.mark.parametrize(
    ("provider", "model", "reply"),
    [
        (
            "anthropic",
            "claude-sonnet-4-6",
            {
                "content": [{"type": "text", "text": "Claude reply"}],
                "usage": {"input_tokens": 2, "output_tokens": 3},
            },
        ),
        (
            "meta",
            "muse-spark-1.3",
            {
                "choices": [{"message": {"content": "Muse reply"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3},
            },
        ),
    ],
)
def test_claude_and_muse_generate_with_client_key(
    ai_workspaces, monkeypatch, provider, model, reply
):
    from rubik_core.ai_providers import runtime

    sent = []

    def post(url, *, headers, json, timeout, allow_redirects):
        sent.append((url, headers, json, allow_redirects))
        return SimpleNamespace(status_code=200, json=lambda: reply)

    monkeypatch.setattr(runtime.requests, "post", post)
    with use_workspace("ai-acme"):
        configure_provider(provider, api_key=f"{provider}-key")
        set_model_route("l10_summary_email", provider, model)
        response = generate_text(
            "l10_summary_email", "Summarize this", default_model="gemini"
        )
    assert response.provider == provider
    assert response.text in ("Claude reply", "Muse reply")
    assert sent[0][0] == PROVIDER_ENDPOINTS[provider]
    assert sent[0][2]["model"] == model
    assert sent[0][3] is False


def test_priorities_json_uses_muse_without_managed_vertex(ai_workspaces, monkeypatch):
    from rubik_core.ai_providers import runtime
    from rubik_core.priorities.agents.base import LLMJsonClient

    def post(url, *, headers, json, timeout, allow_redirects):
        assert url == PROVIDER_ENDPOINTS["meta"]
        assert json["response_format"] == {"type": "json_object"}
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "choices": [{"message": {"content": '{"result":"ok"}'}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3},
            },
        )

    monkeypatch.setattr(runtime.requests, "post", post)
    with use_workspace("ai-acme"):
        configure_provider("meta", api_key="muse-key")
        set_model_route("ai_priorities", "meta", "muse-spark-1.3")
        client = LLMJsonClient(
            model_key="gemini-flash", temperature=0.2, feature="ai_priorities"
        )
        monkeypatch.setattr(
            client,
            "_client",
            SimpleNamespace(
                models=SimpleNamespace(
                    generate_content=lambda **kw: pytest.fail(
                        "Managed Vertex fallback is forbidden"
                    )
                )
            ),
        )
        assert client.invoke_json("Return JSON", fallback={}) == {"result": "ok"}


def test_shared_json_client_does_not_inherit_priorities_provider(
    ai_workspaces, monkeypatch
):
    from rubik_core.ai_providers import runtime
    from rubik_core.priorities.agents.base import LLMJsonClient
    from rubik_core.priorities.agents.critic import CriticAgent
    from rubik_core.priorities.agents.decision_maker import DecisionMakerAgent
    from rubik_core.priorities.agents.summarizer import SummarizerAgent
    from rubik_core.priorities.config import PrioritiesConfig

    monkeypatch.setattr(
        runtime.requests,
        "post",
        lambda *a, **kw: pytest.fail("Unrelated workload used Priorities provider"),
    )
    with use_workspace("ai-acme"):
        configure_provider("meta", api_key="muse-key")
        set_model_route("ai_priorities", "meta", "muse-spark-1.3")
        client = LLMJsonClient(model_key="gemini-flash", temperature=0.2)
        client._client = SimpleNamespace(
            models=SimpleNamespace(
                generate_content=lambda **kw: SimpleNamespace(
                    text='{"source":"managed"}'
                )
            )
        )
        assert client.invoke_json("Return JSON") == {"source": "managed"}

        config = PrioritiesConfig.default()
        assert CriticAgent(config).client.feature == "ai_priorities"
        assert DecisionMakerAgent(config).client.feature == "ai_priorities"
        assert SummarizerAgent(config).client.feature == "ai_priorities"


def test_priorities_managed_route_uses_selected_model(ai_workspaces):
    from rubik_core.priorities.agents.base import LLMJsonClient

    calls = []
    with use_workspace("ai-acme"):
        set_model_route(
            "ai_priorities", "vertex_managed", settings.VERTEX_LLM_MODEL_PRO
        )
        client = LLMJsonClient(
            model_key="gemini-flash", temperature=0.2, feature="ai_priorities"
        )

        def generate_content(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(text='{"result":"ok"}')

        client._client = SimpleNamespace(
            models=SimpleNamespace(generate_content=generate_content)
        )
        assert client.invoke_json("Return JSON") == {"result": "ok"}

    assert len(calls) == 1
    assert calls[0]["model"] == settings.VERTEX_LLM_MODEL_PRO
    assert calls[0]["config"].response_mime_type == "application/json"
    assert calls[0]["config"].temperature == 0.2


def test_priorities_route_switch_during_retry_never_calls_vertex(
    ai_workspaces, monkeypatch
):
    from rubik_core.ai_providers import runtime
    from rubik_core.priorities.agents import base
    from rubik_core.priorities.agents.base import LLMJsonClient

    calls = []

    def failed_client_request(*args, **kwargs):
        calls.append(args[0])
        set_model_route(
            "ai_priorities", "vertex_managed", settings.VERTEX_LLM_MODEL_FLASH
        )
        return SimpleNamespace(status_code=401, json=lambda: {})

    monkeypatch.setattr(runtime.requests, "post", failed_client_request)
    monkeypatch.setattr(base.time, "sleep", lambda *_: None)
    with use_workspace("ai-acme"):
        configure_provider("meta", api_key="muse-key")
        set_model_route("ai_priorities", "meta", "muse-spark-1.3")
        client = LLMJsonClient(
            model_key="gemini-flash", temperature=0.2, feature="ai_priorities"
        )
        monkeypatch.setattr(
            client,
            "_client",
            SimpleNamespace(
                models=SimpleNamespace(
                    generate_content=lambda **kw: pytest.fail("Vertex fallback")
                )
            ),
        )
        with pytest.raises(IncompatibleModelRoute, match="changed"):
            client.invoke_json("Return JSON", fallback={}, attempts=2)
    assert calls == [PROVIDER_ENDPOINTS["meta"]]


def test_invalid_client_json_fails_visibly_without_logging_customer_text(
    ai_workspaces, monkeypatch, caplog
):
    from rubik_core.ai_providers import runtime
    from rubik_core.priorities.agents.base import LLMJsonClient

    private_text = "Customer confidential prompt echoed in model output"
    monkeypatch.setattr(
        runtime.requests,
        "post",
        lambda *a, **kw: SimpleNamespace(
            status_code=200,
            json=lambda: {
                "choices": [{"message": {"content": private_text}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4},
            },
        ),
    )
    with use_workspace("ai-acme"):
        configure_provider("meta", api_key="muse-key")
        set_model_route("ai_priorities", "meta", "muse-spark-1.3")
        client = LLMJsonClient(
            model_key="gemini-flash", temperature=0.2, feature="ai_priorities"
        )
        with pytest.raises(ProviderRequestError, match="invalid JSON"):
            client.invoke_json("Return JSON", fallback={})
    assert private_text not in caplog.text


def test_priorities_critic_does_not_swallow_client_provider_failure(monkeypatch):
    from rubik_core.priorities.agents.critic import CriticAgent
    from rubik_core.priorities.config import PrioritiesConfig
    from rubik_core.priorities.models import ConsolidatedDigest

    critic = CriticAgent(PrioritiesConfig.default())
    monkeypatch.setattr(
        critic.client,
        "invoke_json",
        lambda *a, **kw: (_ for _ in ()).throw(
            ProviderRequestError("Selected AI provider returned invalid JSON")
        ),
    )
    with pytest.raises(ProviderRequestError, match="invalid JSON"):
        critic.review(ConsolidatedDigest(narrative="n", all_signals=[]), [])


def test_priorities_decision_does_not_convert_client_failure_to_repairable_vertex_error(
    monkeypatch,
):
    from rubik_core.priorities.agents.decision_maker import DecisionMakerAgent
    from rubik_core.priorities.config import PrioritiesConfig
    from rubik_core.priorities.models import ConsolidatedDigest

    decision = DecisionMakerAgent(PrioritiesConfig.default())
    monkeypatch.setattr(
        decision.client,
        "invoke_json",
        lambda *a, **kw: (_ for _ in ()).throw(
            ProviderRequestError("Selected AI provider request failed")
        ),
    )
    with pytest.raises(ProviderRequestError, match="Selected AI provider"):
        decision.decide(ConsolidatedDigest(narrative="n", all_signals=[]))


def test_priorities_channel_client_failure_stops_partial_bundle(monkeypatch):
    from rubik_core.priorities import orchestrator as orchestration
    from rubik_core.priorities.config import PrioritiesConfig
    from rubik_core.priorities.models import ChannelKind, PrioritiesRequest
    from rubik_core.priorities.temporal import TemporalContext

    class BrokenChannel:
        channel = ChannelKind.GMAIL

        def __init__(self, **kwargs):
            pass

        def run(self):
            raise ProviderRequestError("Selected AI provider request failed")

    monkeypatch.setitem(
        orchestration.CHANNEL_AGENT_REGISTRY, ChannelKind.GMAIL, BrokenChannel
    )
    now = datetime.now(timezone.utc)
    runner = orchestration.PrioritiesOrchestrator(PrioritiesConfig.default())
    with pytest.raises(ProviderRequestError, match="Selected AI provider"):
        runner._run_channels(
            PrioritiesRequest(
                user_email="me@example.com", channels=[ChannelKind.GMAIL]
            ),
            [],
            TemporalContext(now_utc=now, now_local=now, tz_name="UTC"),
        )


def test_admin_api_is_write_only_and_tests_before_route_activation(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.auth import admin_required, get_current_user
    from backend.main import app
    from backend.routers import ai_providers

    admin = SimpleNamespace(email="admin@example.com", roles=["admin", "user"])
    previous_admin = app.dependency_overrides.get(admin_required)
    previous_user = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[admin_required] = lambda: admin
    app.dependency_overrides[get_current_user] = lambda: admin
    observed = []
    audits = []
    monkeypatch.setattr(
        ai_providers, "record_audit", lambda db, **kw: audits.append(kw)
    )
    monkeypatch.setattr(
        ai_providers,
        "configure_provider",
        lambda provider, **kwargs: observed.append((provider, kwargs["api_key"])),
    )
    monkeypatch.setattr(
        ai_providers,
        "provider_status",
        lambda: {"meta": {"connected": True, "credential_configured": True}},
    )
    monkeypatch.setattr(ai_providers, "model_routes", lambda: {})
    json_ok = [False]

    def probe(provider, model, *, capability="text"):
        observed.append(("test", provider, model, capability))
        if capability == "json" and not json_ok[0]:
            raise ProviderRequestError("AI provider did not return a JSON object")

    monkeypatch.setattr(
        ai_providers,
        "test_connection",
        probe,
    )
    monkeypatch.setattr(
        ai_providers,
        "set_model_route",
        lambda feature, provider, model: observed.append(
            ("route", feature, provider, model)
        ),
    )
    try:
        client = TestClient(app)
        save = client.put(
            "/api/admin/workspace/ai/providers/meta",
            json={"api_key": "write-only-secret"},
        )
        assert save.status_code == 200
        assert "write-only-secret" not in save.text
        activate = client.put(
            "/api/admin/workspace/ai/routes/l10_summary_email",
            json={"provider": "meta", "model": "muse-spark-1.3"},
        )
        assert activate.status_code == 200
        assert observed == [
            ("meta", "write-only-secret"),
            ("test", "meta", "muse-spark-1.3", "text"),
            ("route", "l10_summary_email", "meta", "muse-spark-1.3"),
        ]
        failed_json = client.put(
            "/api/admin/workspace/ai/routes/ai_priorities",
            json={"provider": "meta", "model": "muse-spark-1.3"},
        )
        assert failed_json.status_code == 502
        assert (
            failed_json.json()["detail"] == "AI provider did not return a JSON object"
        )
        assert observed[-1] == ("test", "meta", "muse-spark-1.3", "json")
        assert len([item for item in observed if item[0] == "route"]) == 1
        assert len(audits) == 2

        direct_embedding = client.put(
            "/api/admin/workspace/ai/routes/embedding_index",
            json={"provider": "meta", "model": "muse-spark-1.3"},
        )
        assert direct_embedding.status_code == 409
        assert "Prepare and activate" in direct_embedding.json()["detail"]
        assert len([item for item in observed if item[0] == "route"]) == 1

        json_ok[0] = True
        activated_json = client.put(
            "/api/admin/workspace/ai/routes/ai_priorities",
            json={"provider": "meta", "model": "muse-spark-1.3"},
        )
        assert activated_json.status_code == 200
        assert observed[-2:] == [
            ("test", "meta", "muse-spark-1.3", "json"),
            ("route", "ai_priorities", "meta", "muse-spark-1.3"),
        ]
        assert [row["action"] for row in audits] == [
            "WORKSPACE_AI_PROVIDER_SAVE",
            "WORKSPACE_AI_ROUTE_UPDATE",
            "WORKSPACE_AI_ROUTE_UPDATE",
        ]
        assert "write-only-secret" not in str(audits)
    finally:
        if previous_admin is None:
            app.dependency_overrides.pop(admin_required, None)
        else:
            app.dependency_overrides[admin_required] = previous_admin
        if previous_user is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous_user


def test_embedding_admin_prepare_and_activate_are_distinct_audited_steps(
    monkeypatch,
):
    from backend.routers import ai_providers
    from rubik_core.ai_providers import embedding_index

    observed = []
    monkeypatch.setattr(
        embedding_index,
        "prepare_embedding_index",
        lambda provider, model, *, corpus: (
            observed.append(("prepare", provider, model, corpus))
            or {
                "version": "1234567890abcdef",
                "provider": provider,
                "model": model,
                "dimension": 1536,
                "indexed_chunks": 2,
            }
        ),
    )
    monkeypatch.setattr(
        embedding_index,
        "activate_embedding_index",
        lambda version, *, corpus: (
            observed.append(("activate", version, corpus))
            or {
                "version": version,
                "provider": "openai",
                "model": "text-embedding-3-small",
                "dimension": 1536,
                "indexed_chunks": 2,
            }
        ),
    )
    monkeypatch.setattr(
        embedding_index,
        "embedding_index_status",
        lambda: {"active": {"version": "legacy"}, "candidate": None},
    )
    monkeypatch.setattr(ai_providers, "SessionLocal", lambda: nullcontext(object()))
    monkeypatch.setattr(
        ai_providers,
        "record_audit",
        lambda db, **kwargs: observed.append(("audit", kwargs["action"])),
    )
    admin = SimpleNamespace(email="admin@example.com")

    assert ai_providers.get_embedding_index(admin)["active"]["version"] == "legacy"
    prepared = ai_providers.prepare_embedding_index_route(
        ai_providers.EmbeddingPrepareRequest(
            provider="openai", model="text-embedding-3-small"
        ),
        admin,
    )
    assert prepared["version"] == "1234567890abcdef"
    activated = ai_providers.activate_embedding_index_route(
        ai_providers.EmbeddingActivateRequest(version=prepared["version"]),
        admin,
    )
    assert activated["version"] == prepared["version"]
    assert observed == [
        ("prepare", "openai", "text-embedding-3-small", "default"),
        ("audit", "WORKSPACE_EMBEDDING_INDEX_PREPARE"),
        ("activate", "1234567890abcdef", "default"),
        ("audit", "WORKSPACE_EMBEDDING_INDEX_ACTIVATE"),
    ]

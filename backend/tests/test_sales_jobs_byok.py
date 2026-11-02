"""Selected background and Sales workloads never spend on managed Vertex."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import delete

from rubik_core.ai_providers.registry import (
    configure_provider,
    set_model_route,
)
from rubik_core.ai_providers.runtime import ProviderRequestError
from rubik_core.config import AppMode, settings
from rubik_core.control.db import control_session
from rubik_core.control.models import Workspace, WorkspaceConnector
from rubik_core.tenancy.context import use_workspace


@pytest.fixture
def routed_workspaces(tmp_path, monkeypatch):
    from rubik_core.ai_providers import runtime

    monkeypatch.setattr(runtime, "_record", lambda *a, **kw: None)
    monkeypatch.setattr(settings, "LOCAL_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "APP_MODE", AppMode.LOCAL)
    monkeypatch.setattr(settings, "CLOUDSQL_CONNECTION_NAME", "")
    slugs = ("sales-byok-a", "sales-byok-b")
    with control_session() as session:
        session.add_all(
            Workspace(slug=slug, name=slug, status="active") for slug in slugs
        )
        session.commit()
    yield slugs
    with control_session() as session:
        session.execute(
            delete(WorkspaceConnector).where(
                WorkspaceConnector.workspace_slug.in_(slugs)
            )
        )
        for slug in slugs:
            session.delete(session.get(Workspace, slug))
        session.commit()


def test_rock_draft_uses_each_workspace_key_and_no_managed_vertex(
    routed_workspaces, monkeypatch
):
    from backend.routers.eos.rocks import _gemini_json
    from rubik_core.ai_providers import runtime

    monkeypatch.setattr(
        "rubik_core.models.get_genai_client",
        lambda: pytest.fail("managed Vertex called"),
    )
    calls = []

    def post(url, *, headers, json, timeout, allow_redirects):
        calls.append((headers["Authorization"], json["model"]))
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "choices": [
                    {
                        "message": {
                            "content": '{"description":"Quarterly focus","milestones":[]}'
                        }
                    }
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3},
            },
        )

    monkeypatch.setattr(runtime.requests, "post", post)
    for slug, key in zip(routed_workspaces, ("key-a", "key-b")):
        with use_workspace(slug):
            configure_provider("openai", api_key=key)
            set_model_route("eos_rock_drafts", "openai", "gpt-6-luna")
            assert asyncio.run(_gemini_json("Draft a rock"))["description"] == (
                "Quarterly focus"
            )
    assert calls == [
        ("Bearer key-a", "gpt-6-luna"),
        ("Bearer key-b", "gpt-6-luna"),
    ]


def test_broken_selected_rock_key_is_safe_and_never_calls_vertex(
    routed_workspaces, monkeypatch
):
    from backend.routers.eos.rocks import _gemini_json
    from rubik_core.ai_providers import runtime

    monkeypatch.setattr(
        "rubik_core.models.get_genai_client",
        lambda: pytest.fail("managed Vertex called"),
    )
    monkeypatch.setattr(
        runtime.requests,
        "post",
        lambda *a, **kw: SimpleNamespace(status_code=401, text="secret detail"),
    )
    with use_workspace(routed_workspaces[0]):
        configure_provider("openai", api_key="bad-private-key")
        set_model_route("eos_rock_drafts", "openai", "gpt-6-luna")
        with pytest.raises(HTTPException) as caught:
            asyncio.run(_gemini_json("Draft a rock"))
    assert caught.value.status_code == 502
    assert "bad-private-key" not in str(caught.value.detail)
    assert "secret detail" not in str(caught.value.detail)


def test_live_crm_planner_pins_each_workspace_key_without_managed_vertex(
    routed_workspaces, monkeypatch
):
    from rubik_core.ai_providers import runtime
    from rubik_core.retrieval import hubspot_api

    monkeypatch.setattr(
        hubspot_api,
        "get_genai_client",
        lambda: pytest.fail("managed Vertex called"),
    )
    seen = []

    def post(_url, *, headers, json, timeout, allow_redirects):
        prompt = json["messages"][-1]["content"]
        content = '{"intent":"general"}'
        seen.append((headers["Authorization"], json["model"]))
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3},
            },
        )

    monkeypatch.setattr(runtime.requests, "post", post)
    for slug, key in zip(routed_workspaces, ("crm-key-a", "crm-key-b")):
        with use_workspace(slug):
            configure_provider("openai", api_key=key)
            set_model_route("crm_retrieval_plan", "openai", "gpt-6-luna")
            assert (
                hubspot_api.HubSpotAPIRetriever()._plan("Show deals")["intent"]
                == "general"
            )
    assert seen == [
        (f"Bearer {key}", "gpt-6-luna") for key in ("crm-key-a", "crm-key-b")
    ]


def test_broken_selected_crm_key_surfaces_safe_error_without_vertex(
    routed_workspaces, monkeypatch, caplog
):
    from rubik_core.ai_providers import runtime
    from rubik_core.retrieval import hubspot_api

    monkeypatch.setattr(
        hubspot_api, "get_genai_client", lambda: pytest.fail("managed Vertex called")
    )
    monkeypatch.setattr(
        runtime.requests,
        "post",
        lambda *a, **kw: SimpleNamespace(status_code=401, text="secret detail"),
    )
    with use_workspace(routed_workspaces[0]):
        configure_provider("openai", api_key="bad-private-key")
        set_model_route("crm_retrieval_plan", "openai", "gpt-6-luna")
        with pytest.raises(hubspot_api.HubSpotAPIError) as caught:
            hubspot_api.HubSpotAPIRetriever()._plan("Show deals")
        assert "selected AI model is unavailable" in str(caught.value)
    assert "bad-private-key" not in caplog.text
    assert "secret detail" not in caplog.text


def test_selected_crm_generic_transport_error_is_not_swallowed(
    routed_workspaces, monkeypatch
):
    from rubik_core.retrieval import hubspot_api

    def broken_transport(*_args, **_kwargs):
        raise RuntimeError("private transport detail")

    monkeypatch.setattr(hubspot_api, "run_text_with_route", broken_transport)
    with use_workspace(routed_workspaces[0]):
        configure_provider("openai", api_key="crm-private-key")
        set_model_route("crm_retrieval_plan", "openai", "gpt-6-luna")
        with pytest.raises(hubspot_api.HubSpotAPIError) as api_error:
            hubspot_api.HubSpotAPIRetriever()._plan("Show deals")
        assert "private transport detail" not in str(api_error.value)


def test_deep_prep_selected_failure_does_not_build_managed_client(
    routed_workspaces, monkeypatch
):
    from rubik_core.ai_providers import runtime
    from rubik_core.deep_prep.council import generate_deep_prep

    monkeypatch.setattr(
        "rubik_core.models.get_genai_client",
        lambda: pytest.fail("managed Vertex called"),
    )
    monkeypatch.setattr(
        runtime.requests,
        "post",
        lambda *a, **kw: SimpleNamespace(status_code=401, text="secret detail"),
    )
    with use_workspace(routed_workspaces[0]):
        configure_provider("openai", api_key="bad-private-key")
        set_model_route("deep_prep_council", "openai", "gpt-6-luna")
        with pytest.raises(ProviderRequestError, match="Deep Prep provider failed"):
            asyncio.run(
                generate_deep_prep(
                    {"title": "Account review"},
                    user_email="reader@example.com",
                    is_sales_user=True,
                    is_internal=False,
                )
            )


def test_gmail_summary_selected_failure_is_labeled_and_not_heuristically_actionable(
    routed_workspaces, monkeypatch
):
    from backend.services.gmail_service import GmailService, ParsedMessage
    from rubik_core.ai_providers import runtime

    monkeypatch.setattr(
        "rubik_core.models.get_genai_client",
        lambda: pytest.fail("managed Vertex called"),
    )
    monkeypatch.setattr(
        runtime.requests,
        "post",
        lambda *a, **kw: SimpleNamespace(status_code=401, text="secret detail"),
    )
    service = object.__new__(GmailService)
    service.user_email = "reader@example.com"
    service.account_id = "personal-mailbox"
    mail = ParsedMessage(
        account_id="personal-mailbox",
        thread_id="thread-1",
        message_id="message-1",
        sender="client@example.com",
        subject="Please confirm",
        snippet="Reader, please confirm this by noon",
        date="2026-10-30",
        is_unread=True,
    )
    with use_workspace(routed_workspaces[0]):
        configure_provider("openai", api_key="bad-private-key")
        set_model_route("gmail_summaries", "openai", "gpt-6-luna")
        result = asyncio.run(service.summarize_messages([mail], "Reader"))
    assert result[0].summary.startswith("AI summary unavailable")
    assert result[0].action_required is False
    assert result[0].account_id == "personal-mailbox"


def test_voice_transcription_uses_selected_native_gemini_and_records_model(
    routed_workspaces, monkeypatch
):
    from rubik_core.ai_providers import runtime
    from rubik_core.voice import transcribe

    monkeypatch.setattr(
        transcribe, "get_genai_client", lambda: pytest.fail("managed Vertex called")
    )
    monkeypatch.setattr(
        transcribe, "build_instructions", lambda **kw: "Transcribe this clip."
    )
    observed = []

    class NativeModels:
        async def generate_content(self, *, model, contents, config):
            observed.append((model, contents[1]))
            return SimpleNamespace(
                text='{"text":"hello there","no_speech":false}',
                usage_metadata=None,
            )

    native = SimpleNamespace(aio=SimpleNamespace(models=NativeModels()))
    monkeypatch.setattr(
        runtime,
        "_genai_client",
        lambda route, key: (
            native
            if key == "gemini-private-key"
            else pytest.fail("wrong workspace credential")
        ),
    )
    with use_workspace(routed_workspaces[0]):
        configure_provider("gemini", api_key="gemini-private-key")
        set_model_route("voice_transcription", "gemini", "gemini-2.5-pro")
        transcript = asyncio.run(
            transcribe.transcribe_clip(
                b"RIFF test audio",
                "audio/wav",
                clip_seconds=2.0,
            )
        )
    assert transcript.text == "hello there"
    assert transcript.model == "gemini-2.5-pro"
    assert observed == [("gemini-2.5-pro", "Transcribe this clip.")]


def test_chat_file_image_uses_selected_native_gemini(routed_workspaces, monkeypatch):
    from rubik_core.ai_providers import runtime
    from rubik_core.chat_files import reader
    from rubik_core.chat_files.extract import Extracted

    monkeypatch.setattr(
        reader, "get_genai_client", lambda: pytest.fail("managed Vertex called")
    )
    monkeypatch.setattr(
        reader, "build_contents", lambda filename, ex: ["image", "Read the image."]
    )
    observed = []

    class NativeModels:
        async def generate_content(self, *, model, contents, config):
            observed.append((model, contents))
            return SimpleNamespace(
                text='{"summary":"A price chart.","text":"Revenue: $40"}',
                usage_metadata=None,
            )

    native = SimpleNamespace(aio=SimpleNamespace(models=NativeModels()))
    monkeypatch.setattr(runtime, "_genai_client", lambda route, key: native)
    with use_workspace(routed_workspaces[0]):
        configure_provider("gemini", api_key="gemini-private-key")
        set_model_route("chat_file_vision", "gemini", "gemini-2.5-pro")
        read = asyncio.run(
            reader.read_file(
                "chart.png",
                Extracted(
                    kind="image",
                    mime_type="image/png",
                    needs_model=True,
                    media=b"png bytes",
                    media_mime="image/png",
                ),
            )
        )
    assert read.summary == "A price chart."
    assert read.text == "Revenue: $40"
    assert read.model == "gemini-2.5-pro"
    assert observed == [("gemini-2.5-pro", ["image", "Read the image."])]


def test_typed_voice_command_uses_selected_native_tool_route(
    routed_workspaces, monkeypatch
):
    from rubik_core.ai_providers import runtime
    from rubik_core.voice import commands

    monkeypatch.setattr(
        commands, "get_genai_client", lambda: pytest.fail("managed Vertex called")
    )
    observed = []

    class NativeModels:
        async def generate_content(self, *, model, contents, config):
            observed.append((model, contents))
            return SimpleNamespace(
                function_calls=[
                    SimpleNamespace(name="not_understood", args={"heard": "cancel"})
                ],
                usage_metadata=None,
            )

    native = SimpleNamespace(aio=SimpleNamespace(models=NativeModels()))
    monkeypatch.setattr(runtime, "_genai_client", lambda route, key: native)
    with use_workspace(routed_workspaces[0]):
        configure_provider("gemini", api_key="gemini-private-key")
        set_model_route("voice_command_text", "gemini", "gemini-2.5-pro")
        result = asyncio.run(
            commands._call(
                instructions="Interpret this command.",
                functions=[
                    {
                        "name": "not_understood",
                        "description": "No matching command",
                        "parameters": {
                            "type": "object",
                            "properties": {"heard": {"type": "string"}},
                        },
                    }
                ],
                audio=None,
                mime_type="audio/wav",
                user_email="reader@example.com",
                clip_seconds=None,
            )
        )
    assert result.name == "not_understood"
    assert observed == [("gemini-2.5-pro", ["Interpret this command."])]


def test_sales_email_agent_records_selected_model_and_client_billing(monkeypatch):
    from google.adk import runners
    from google.adk.sessions import in_memory_session_service

    from backend.routers import email
    from rubik_core.adk_agent import sales_email_agent
    from rubik_core.adk_agent import tools as agent_tools
    from rubik_core.ai_providers import adk_model
    from rubik_core.ai_providers.registry import ModelRoute
    from rubik_core.cost import usage

    route = ModelRoute(
        "sales-byok-a", "sales_email_agent", "gemini", "gemini-2.5-pro", "client"
    )
    monkeypatch.setattr(adk_model, "resolve_adk_route", lambda *a, **kw: route)
    chosen = []
    monkeypatch.setattr(
        sales_email_agent,
        "build_sales_email_agent",
        lambda *a, **kw: chosen.append(kw["model_route"]) or object(),
    )

    def tool(*a, **kw):
        return None

    monkeypatch.setattr(
        agent_tools,
        "AgentTools",
        lambda **kw: SimpleNamespace(
            gmail_create_draft=tool,
            search_hubspot_data=tool,
            search_past_meetings=tool,
            search_knowledge_base=tool,
            search_products=tool,
        ),
    )
    monkeypatch.setattr(email, "_log_audit_action", lambda *a, **kw: None)
    monkeypatch.setattr(email, "build_user_context_block", lambda *a, **kw: "")

    class Sessions:
        async def create_session(self, **kw):
            return None

    monkeypatch.setattr(in_memory_session_service, "InMemorySessionService", Sessions)

    class Runner:
        def __init__(self, **kw):
            pass

        async def run_async(self, **kw):
            yield SimpleNamespace(
                partial=False,
                usage_metadata=SimpleNamespace(
                    prompt_token_count=5,
                    candidates_token_count=2,
                    total_token_count=7,
                ),
                is_final_response=lambda: False,
                content=None,
            )
            yield SimpleNamespace(
                partial=False,
                usage_metadata=None,
                is_final_response=lambda: True,
                content=SimpleNamespace(
                    parts=[
                        SimpleNamespace(
                            text="Subject: Follow-up\n\nThanks for your time."
                        )
                    ]
                ),
            )

    monkeypatch.setattr(runners, "Runner", Runner)
    recorded = []
    monkeypatch.setattr(usage, "record_usage", lambda **kw: recorded.append(kw))
    result = asyncio.run(
        email.draft_sales_email(
            email.EmailDraftRequest(recipient_email="buyer@example.com"),
            user=SimpleNamespace(email="rep@example.com"),
            db=object(),
        )
    )
    assert result.subject == "Follow-up"
    assert chosen == [route]
    assert len(recorded) == 1
    assert recorded[0]["model"] == "gemini-2.5-pro"
    assert recorded[0]["feature"] == "sales_email_agent"
    assert recorded[0]["prompt_tokens"] == 5
    assert recorded[0]["completion_tokens"] == 2
    assert recorded[0]["metadata"] == {
        "provider": "gemini",
        "billing_owner": "client",
    }

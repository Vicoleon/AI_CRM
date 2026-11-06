"""CRM extraction instructions keep their role at every supported provider."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from rubik_core.ai_providers import runtime
from rubik_core.ai_providers.registry import ModelRoute


@pytest.mark.parametrize(
    ("provider", "model", "role"),
    [
        ("openai", "gpt-6-luna", "system"),
        ("deepseek", "deepseek-v4-pro", "system"),
        ("meta", "muse-spark-1.3", "developer"),
    ],
)
def test_chat_compatible_provider_sends_system_instruction_as_separate_role(
    monkeypatch, provider, model, role
):
    route = ModelRoute("tenant", "crm_sync", provider, model, "client")
    seen = []

    def post(url, headers, payload):
        seen.append((url, payload))
        return {
            "choices": [{"message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    monkeypatch.setattr(runtime, "_post_json", post)
    monkeypatch.setattr(runtime, "_record", lambda *a, **kw: None)
    response = runtime._remote_text(
        route,
        "client transcript",
        "user payload",
        json_mode=True,
        system_instruction="CRM extraction rules",
    )
    assert response.text == "{}"
    assert seen[0][1]["messages"] == [
        {"role": role, "content": "CRM extraction rules"},
        {"role": "user", "content": "user payload"},
    ]


def test_anthropic_provider_uses_top_level_system_parameter(monkeypatch):
    route = ModelRoute(
        "tenant", "connection_test", "anthropic", "claude-sonnet-4-6", "client"
    )
    seen = []

    def post(url, headers, payload):
        seen.append(payload)
        return {
            "content": [{"type": "text", "text": "OK"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    monkeypatch.setattr(runtime, "_post_json", post)
    monkeypatch.setattr(runtime, "_record", lambda *a, **kw: None)
    response = runtime._remote_text(
        route,
        "client key",
        "user payload",
        json_mode=False,
        system_instruction="Behavior rules",
    )
    assert response.text == "OK"
    assert seen[0]["system"] == "Behavior rules"
    assert seen[0]["messages"] == [{"role": "user", "content": "user payload"}]


def test_gemini_provider_receives_system_instruction_in_generate_config(monkeypatch):
    route = ModelRoute("tenant", "crm_sync", "gemini", "gemini-2.5-pro", "client")
    seen = []

    def generate_content(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(text="{}")

    client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    monkeypatch.setattr(runtime, "_key", lambda selected: "client key")
    monkeypatch.setattr(runtime, "_genai_client", lambda selected, key: client)
    monkeypatch.setattr(runtime, "_record", lambda *a, **kw: None)
    monkeypatch.setattr(
        "rubik_core.cost.usage.extract_usage",
        lambda response: {"prompt": 1, "completion": 1, "thinking": 0, "cached": 0},
    )
    response = runtime._generate_with_route(
        route,
        "user payload",
        json_mode=True,
        system_instruction="CRM extraction rules",
    )
    assert response.text == "{}"
    assert seen[0]["config"].system_instruction == "CRM extraction rules"
    assert seen[0]["config"].response_mime_type == "application/json"

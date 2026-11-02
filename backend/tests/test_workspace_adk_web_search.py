"""Chat's web-search tool uses its own pinned grounding route."""

from __future__ import annotations

from rubik_core.adk_agent.tools import AgentTools
from rubik_core.ai_providers.registry import ModelRoute
from rubik_core.ai_providers.runtime import ModelTextResponse


def test_search_web_uses_selected_grounding_route_and_cites_sources(monkeypatch):
    from rubik_core.ai_providers import registry, runtime
    from rubik_core.cost.usage import current_feature, feature_context

    route = ModelRoute("test", "chat_web_search", "gemini", "gemini-2.5-pro", "client")
    seen = []

    def resolve(feature, capability, *, default_model):
        seen.append(("route", feature, capability, default_model))
        return route

    def generate(selected, prompt, *, temperature):
        seen.append(("generate", selected, prompt, temperature))
        seen.append(("feature", current_feature.get()))
        return ModelTextResponse(
            "Grounded answer",
            "gemini",
            "gemini-2.5-pro",
            "client",
            citation_urls=("https://example.com/source",),
        )

    monkeypatch.setattr(registry, "resolve_model_route", resolve)
    monkeypatch.setattr(runtime, "generate_grounded_text_on_route", generate)
    with feature_context("chat"):
        result = AgentTools(user_email="employee@example.com").search_web(
            "current news"
        )
    assert result["status"] == "success"
    assert "Grounded answer" in result["report"]
    assert "https://example.com/source" in result["report"]
    assert seen[0][:3] == ("route", "chat_web_search", "grounding")
    assert seen[1][1] == route
    assert seen[1][3] == 0.0
    assert seen[2] == ("feature", "web_search")


def test_search_web_redacts_selected_provider_error(monkeypatch, caplog):
    from rubik_core.ai_providers import registry, runtime

    route = ModelRoute("test", "chat_web_search", "gemini", "gemini-2.5-pro", "client")
    monkeypatch.setattr(
        registry, "resolve_model_route", lambda feature, capability, **kw: route
    )
    monkeypatch.setattr(
        runtime,
        "generate_grounded_text_on_route",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("secret-key customer prompt")
        ),
    )
    result = AgentTools(user_email="employee@example.com").search_web("current news")
    assert result["status"] == "error"
    assert "Admin → Workspace" in result["error_message"]
    assert "secret-key" not in str(result)
    assert "secret-key" not in caplog.text
    assert "customer prompt" not in caplog.text

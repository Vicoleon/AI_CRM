"""Workspace-owned X-Ray analysis and Google Search grounding."""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from rubik_core.ai_providers.registry import IncompatibleModelRoute, ModelRoute
from rubik_core.ai_providers.runtime import ModelTextResponse, ProviderRequestError
from rubik_core.xray import orchestrator, service
from rubik_core.xray.agents import base, critic
from rubik_core.xray.schemas import ChurnResult, CompanyBundle


def _route(provider: str = "meta", model: str = "muse-spark-1.3") -> ModelRoute:
    return ModelRoute("rubiksoft", "xray_json", provider, model, "client")


def test_xray_json_uses_the_pinned_client_route_without_vertex(monkeypatch):
    route = _route()
    calls = []

    def selected(*args, **kwargs):
        calls.append((args, kwargs))
        return ModelTextResponse('{"summary":"ready"}', "meta", route.model, "client")

    monkeypatch.setattr(base, "generate_text_on_route", selected)
    monkeypatch.setattr(
        base, "get_genai_client", lambda: pytest.fail("managed Vertex was called")
    )

    assert base.llm_json(
        "system", "customer prompt", model_id="managed-default", route=route
    ) == {"summary": "ready"}
    assert calls == [
        (
            (route, "customer prompt"),
            {
                "json_mode": True,
                "system_instruction": "system",
                "temperature": 0.2,
            },
        )
    ]


def test_xray_managed_route_uses_the_selected_model(monkeypatch):
    route = ModelRoute(
        "rubiksoft", "xray_json", "vertex_managed", "gemini-2.5-pro", "cubie"
    )
    monkeypatch.setattr(base, "resolve_xray_route", lambda *a: route)
    calls = []
    monkeypatch.setattr(
        base,
        "get_genai_client",
        lambda: SimpleNamespace(
            models=SimpleNamespace(
                generate_content=lambda **kw: (
                    calls.append(kw) or SimpleNamespace(text='{"summary":"ready"}')
                )
            )
        ),
    )

    assert base.llm_json(
        "system", "prompt", model_id="gemini-2.5-flash", route=route
    ) == {"summary": "ready"}
    assert calls[0]["model"] == route.model


def test_xray_critic_does_not_log_malformed_model_text(monkeypatch, caplog):
    secret = "PRIVATE CUSTOMER CONTRACT"
    monkeypatch.setattr(critic, "llm_json", lambda *a, **kw: {"score": secret})

    result = critic.review({"executive_summary": "summary"}, route=_route())

    assert result["critic_failed"] is True
    assert secret not in caplog.text
    assert secret not in str(result)


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("provider echoed customer secret"), ValueError("bad response")],
)
def test_xray_provider_failure_never_exposes_raw_error_or_falls_back(
    monkeypatch, failure, caplog
):
    monkeypatch.setattr(
        base, "generate_text_on_route", lambda *a, **kw: (_ for _ in ()).throw(failure)
    )
    monkeypatch.setattr(
        base, "get_genai_client", lambda: pytest.fail("managed Vertex was called")
    )

    with pytest.raises(
        ProviderRequestError, match="Selected AI provider failed"
    ) as exc:
        base.llm_json("system", "prompt", model_id="managed-default", route=_route())

    assert exc.value.__cause__ is None
    assert "customer secret" not in str(exc.value)
    assert "customer secret" not in caplog.text


def test_xray_route_change_refuses_the_call_without_vertex(monkeypatch):
    monkeypatch.setattr(
        base,
        "generate_text_on_route",
        lambda *a, **kw: (_ for _ in ()).throw(
            IncompatibleModelRoute("Workspace AI route changed during this request")
        ),
    )
    monkeypatch.setattr(
        base, "get_genai_client", lambda: pytest.fail("managed Vertex was called")
    )

    with pytest.raises(IncompatibleModelRoute, match="route changed"):
        base.llm_json("system", "prompt", model_id="managed-default", route=_route())


def test_deep_routes_preserve_stage_defaults_and_web_stays_grounded(monkeypatch):
    defaults = []

    def resolve(default_model):
        defaults.append(default_model)
        return _route()

    monkeypatch.setattr(orchestrator, "resolve_xray_route", resolve)
    web_route = ModelRoute(
        "rubiksoft", "xray_grounding", "gemini", "gemini-2.5-pro", "client"
    )
    monkeypatch.setattr(
        orchestrator,
        "resolve_xray_grounding_route",
        lambda default: defaults.append(default) or web_route,
    )
    monkeypatch.setattr(orchestrator.config, "model_channel", lambda: "channel-default")
    monkeypatch.setattr(orchestrator.config, "model_analyst", lambda: "analyst-default")
    monkeypatch.setattr(orchestrator.config, "model_critic", lambda: "critic-default")
    monkeypatch.setattr(orchestrator.config, "model_web", lambda: "web-default")

    routes = orchestrator.resolve_routes()
    agents = orchestrator._build_agents(
        ("hubspot", "web"),
        CompanyBundle(company_id="c1"),
        "ae@example.com",
        "",
        route=routes.channel,
        web_route=routes.web,
    )

    assert defaults == [
        "channel-default",
        "analyst-default",
        "critic-default",
        "web-default",
    ]
    assert agents["hubspot"].route == routes.channel
    assert agents["web"].route == web_route


def test_xray_web_grounding_uses_selected_workspace_route(monkeypatch):
    route = ModelRoute(
        "rubiksoft", "xray_grounding", "gemini", "gemini-2.5-pro", "client"
    )
    observed = []
    monkeypatch.setattr(
        base,
        "generate_grounded_text_on_route",
        lambda *args, **kwargs: (
            observed.append((args, kwargs))
            or ModelTextResponse(
                '{"news":[]}',
                "gemini",
                route.model,
                "client",
                citation_urls=("https://example.com/source",),
            )
        ),
    )
    monkeypatch.setattr(
        base, "_llm_grounded", lambda *a, **kw: pytest.fail("managed Vertex was called")
    )

    text, citations = base.llm_grounded(
        "system", "prompt", model_id="managed-default", route=route
    )
    assert text == '{"news":[]}'
    assert citations == ["https://example.com/source"]
    assert observed == [
        ((route, "prompt"), {"system_instruction": "system", "temperature": 0.1})
    ]


def test_xray_web_grounding_failure_is_redacted(monkeypatch, caplog):
    from rubik_core.xray.agents.web_agent import WebChannelAgent

    route = ModelRoute(
        "rubiksoft", "xray_grounding", "gemini", "gemini-2.5-pro", "client"
    )
    monkeypatch.setattr(
        base,
        "generate_grounded_text_on_route",
        lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("provider echoed private customer record")
        ),
    )
    monkeypatch.setattr(
        base, "_llm_grounded", lambda *a, **kw: pytest.fail("managed Vertex was called")
    )
    result = WebChannelAgent(
        bundle=CompanyBundle(company_id="c1"),
        requesting_user="ae@example.com",
        route=route,
    ).run()

    assert result.error == "web_research_failed: AI research unavailable"
    assert "private customer record" not in caplog.text
    assert "private customer record" not in str(result)


def test_deep_passes_pinned_routes_and_records_analyst_provider(monkeypatch):
    route = _route()
    routes = orchestrator.XrayRoutes(route, route, route)
    seen = []
    monkeypatch.setattr(
        orchestrator,
        "_build_agents",
        lambda *a, **kw: seen.append(("channel", kw["route"])) or {},
    )
    monkeypatch.setattr(
        orchestrator.churn_mod, "score", lambda *a, **kw: ChurnResult(7, "Healthy")
    )
    monkeypatch.setattr(
        orchestrator.analyst_mod,
        "synthesize",
        lambda *a, **kw: (
            seen.append(("analyst", kw["route"])) or {"executive_summary": "ok"}
        ),
    )
    monkeypatch.setattr(
        orchestrator.critic_mod,
        "review",
        lambda *a, **kw: (
            seen.append(("critic", kw["route"])) or {"needs_revision": False}
        ),
    )

    result = orchestrator.run_deep(
        CompanyBundle(company_id="c1"),
        [],
        [],
        requesting_user="ae@example.com",
        channels=(),
        routes=routes,
    )

    assert seen == [("channel", route), ("analyst", route), ("critic", route)]
    assert result.model_id == "meta:muse-spark-1.3"


def test_fast_records_actual_provider_and_failure_is_visible_without_raw_error(
    monkeypatch,
):
    route = _route()
    bundle = CompanyBundle(company_id="c1", company={"name": "Customer"})
    ckpt = SimpleNamespace(cursors={}, prev_sections=None, prev_created_at=None)
    monkeypatch.setattr(service, "SessionLocal", nullcontext)
    monkeypatch.setattr(service.checkpoint, "load", lambda *a: ckpt)
    monkeypatch.setattr(
        service.fetchers, "fetch_company_bundle", lambda *a, **kw: bundle
    )
    monkeypatch.setattr(service.fetchers, "build_timeline_events", lambda *a: [])
    monkeypatch.setattr(service, "_is_empty", lambda *a: False)
    monkeypatch.setattr(
        service.churn_mod, "score", lambda *a: ChurnResult(7, "Healthy")
    )
    monkeypatch.setattr(service.opportunities, "whitespace", lambda *a: [])
    monkeypatch.setattr(service.opportunities, "fallback_opportunities", lambda *a: [])
    monkeypatch.setattr(
        service.checkpoint,
        "carry_forward_sections",
        lambda _old, sections, **kw: sections,
    )
    monkeypatch.setattr(service, "_build_signals", lambda *a: {})
    monkeypatch.setattr(service.config, "model_fast_synth", lambda: "managed-default")
    monkeypatch.setattr(service, "resolve_xray_route", lambda *a: route)
    from rubik_core.xray import prompts

    monkeypatch.setattr(
        prompts, "build_fast_prompt", lambda *a, **kw: ("system", "prompt")
    )
    saved = []
    monkeypatch.setattr(
        service, "_persist_ready", lambda *a, **kw: saved.append(kw) or "s1"
    )
    monkeypatch.setattr(
        service,
        "llm_json",
        lambda *a, **kw: (_ for _ in ()).throw(
            ProviderRequestError("provider echoed customer secret")
        ),
    )

    assert service.run_fast("c1", requesting_user="ae@example.com") == "s1"
    assert saved[0]["model_id"] == "meta:muse-spark-1.3"
    assert saved[0]["partial_errors"] == [
        "X-Ray analysis unavailable. Check the selected AI provider in Admin."
    ]
    assert "customer secret" not in str(saved[0])

    saved.clear()
    monkeypatch.setattr(
        service,
        "resolve_xray_route",
        lambda *a: (_ for _ in ()).throw(IncompatibleModelRoute("API key unavailable")),
    )
    monkeypatch.setattr(
        service, "llm_json", lambda *a, **kw: pytest.fail("model was called")
    )
    assert service.run_fast("c1", requesting_user="ae@example.com") == "s1"
    assert saved[0]["model_id"] == ""
    assert saved[0]["partial_errors"] == [
        "X-Ray analysis unavailable. Check the selected AI provider in Admin."
    ]

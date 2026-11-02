"""The Insights AI summary (README §4 ``POST …/summary``).

* The model sees department-level numbers only: no table, no person-drilled
  breakdown, no email, no name — even when the viewer may see people.
* Every number in what comes back must be one the input holds (or a rounded
  or percentage form of one); a bullet with any other number is dropped, and
  a summary with nothing left is "not available".
* It is attributed to its own AI Cost category, cached by its input, and any
  failure is ``{"available": false, "reason": …}``, never an error page.

The model is always a stub (patched by STRING target, or injected).
"""

from __future__ import annotations

import json
from unittest.mock import patch

from backend.tests.test_insights_router import (  # noqa: F401 — fixtures
    ADMIN,
    BASE,
    SENTINEL,
    as_user,
    email,
    insights_db,
)
from rubik_core.ai_providers.registry import IncompatibleModelRoute, ModelRoute
from rubik_core.ai_providers.runtime import ModelTextResponse, ProviderRequestError
from rubik_core.insights import service
from rubik_core.insights import summarize as summary_module
from rubik_core.insights.summarize import (
    allowed_numbers,
    build_prompt,
    grounded,
    summarize_payload,
    summary_input,
)

INVOKE = "rubik_core.insights.summarize._default_invoke"


def _payload(slug: str = "adoption", **kw) -> dict:
    return service.dashboard(ADMIN, slug, can_view_people=True, **kw)


class Stub:
    def __init__(self, reply):
        self.reply = reply
        self.prompts: list[str] = []
        self.features: list[str] = []

    def __call__(self, prompt: str):
        from rubik_core.cost.usage import current_feature

        self.prompts.append(prompt)
        self.features.append(current_feature.get())
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply if isinstance(self.reply, str) else json.dumps(self.reply)


# ── what the model sees ──────────────────────────────────────────────────────


def test_the_input_holds_no_person_even_for_a_viewer_who_may_see_people():
    payload = _payload("adoption")
    assert any(s["kind"] == "table" for s in payload["sections"])  # people were in hand
    inp = summary_input(payload)
    text = json.dumps(inp)
    assert "@" not in text
    assert "Avery" not in text and "Stone" not in text
    assert SENTINEL not in text
    assert all(b["kind"] != "table" for b in inp["breakdowns"])
    prompt = build_prompt(inp)
    assert "@" not in prompt and "Avery Stone" not in prompt
    # The KPIs arrive with their meaning and their denominators.
    rate = next(k for k in inp["kpis"] if k["metric"] == "Active share")
    assert (
        rate["denominator"] == 25
        and rate["out_of"] == "people in the selected departments"
    )
    assert "value_percent" in rate


def test_numbers_must_come_from_the_input():
    inp = {
        "kpis": [
            {
                "value": 0.8,
                "numerator": 12,
                "denominator": 15,
                "previous_period_value": 0.7333,
            }
        ]
    }
    allowed = allowed_numbers(inp)
    assert grounded("12 of 15 people (80%) used Cubie.", allowed)
    assert grounded("Up from 73.3% last period.", allowed)
    assert grounded("No numbers here at all.", allowed)
    assert not grounded("17 people used Cubie.", allowed)
    assert not grounded("12 of 15 people, up 6.7 points.", allowed)
    assert grounded("1,234 questions", allowed_numbers({"v": 1234}))


# ── the call ─────────────────────────────────────────────────────────────────


def test_unsupported_bullets_are_dropped_and_the_rest_kept():
    payload = _payload("adoption")
    rate = next(k for k in payload["kpis"] if k["id"] == "active_rate")
    good = f"{rate['numerator']} of {rate['denominator']} people used Cubie."
    stub = Stub(
        {
            "headline": "Cubie reached 99999 people.",  # invented → replaced
            "bullets": [good, "Usage rose by 4242 sessions.", "Most teams are active."],
            "watch": ["Watch the 31337 dormant accounts.", "Keep an eye on new users."],
        }
    )
    out = summarize_payload(payload, invoke=stub)
    assert out["available"] is True and out["cached"] is False
    assert out["bullets"] == [good, "Most teams are active."]
    assert out["watch"] == ["Keep an eye on new users."]
    assert out["headline"] == good
    assert out["generated_at"].endswith("Z")
    assert stub.features == ["insights_summary"]


def test_a_summary_is_cached_by_its_input(monkeypatch):
    monkeypatch.setattr("rubik_core.insights.cache.MIN_REFRESH_AGE_SECONDS", 0)
    payload = _payload("ask-cubie")
    stub = Stub(
        {"headline": "Steady use.", "bullets": ["People keep asking."], "watch": []}
    )
    first = summarize_payload(payload, invoke=stub)
    second = summarize_payload(payload, invoke=stub)
    assert first["cached"] is False and second["cached"] is True
    assert len(stub.prompts) == 1
    summarize_payload(payload, invoke=stub, refresh=True)
    assert len(stub.prompts) == 2


def test_failures_are_not_available_never_an_error():
    payload = _payload("priorities")
    for reply in (
        RuntimeError("Vertex is down"),
        "not json at all",
        ["a", "list"],
        {"headline": "777 cards.", "bullets": ["888 more."], "watch": []},
    ):
        out = summarize_payload(payload, invoke=Stub(reply))
        assert out["available"] is False, reply
        assert out["reason"] and "Vertex" not in out["reason"]


def test_the_endpoint_summarizes_the_view_through_the_default_model(as_user):  # noqa: F811
    stub = Stub(
        {
            "headline": "Cubie is in steady use.",
            "bullets": ["Most people are active."],
            "watch": [],
        }
    )
    with patch(INVOKE, stub):
        r = as_user(ADMIN).post(
            f"{BASE}/dashboards/overview/summary?dept=sales&range=7d"
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is True
    assert body["headline"] == "Cubie is in steady use."
    assert len(stub.prompts) == 1
    assert "@" not in stub.prompts[0]
    assert '"Sales"' in stub.prompts[0]
    assert email("Avery", "Stone") not in stub.prompts[0]


def test_the_endpoint_answers_not_available_when_the_model_fails(as_user):  # noqa: F811
    with patch(INVOKE, Stub(RuntimeError("boom"))):
        r = as_user(ADMIN).post(f"{BASE}/dashboards/ask-cubie/summary")
    assert r.status_code == 200
    assert r.json() == {
        "available": False,
        "reason": "The AI summary is not available right now.",
    }


def _route(provider: str, model: str) -> ModelRoute:
    return ModelRoute(
        workspace="test",
        feature="insights_summary",
        provider=provider,
        model=model,
        billing_owner="client" if provider != "vertex_managed" else "cubie",
    )


def test_selected_provider_owns_the_call_and_cache_is_split_by_route(monkeypatch):
    payload = _payload("adoption")
    current = [_route("openai", "gpt-6-luna")]
    calls: list[ModelRoute] = []

    def resolve(feature, capability, *, default_model):
        assert (feature, capability) == ("insights_summary", "json")
        return current[0]

    def provider_call(route, prompt, *, json_mode):
        assert json_mode is True and '"kpis"' in prompt
        calls.append(route)
        headline = "OpenAI summary." if route.provider == "openai" else "Muse summary."
        return ModelTextResponse(
            json.dumps({"headline": headline, "bullets": [headline], "watch": []}),
            route.provider,
            route.model,
            route.billing_owner,
        )

    def wrong_managed_call(_prompt):
        raise AssertionError("selected client route used managed Vertex")

    monkeypatch.setattr(summary_module, "resolve_model_route", resolve)
    monkeypatch.setattr(summary_module, "generate_text_on_route", provider_call)
    monkeypatch.setattr(summary_module, "_default_invoke", wrong_managed_call)

    first = summarize_payload(payload)
    assert first["available"] is True and first["cached"] is False
    assert first["headline"] == "OpenAI summary."
    assert summarize_payload(payload)["cached"] is True

    current[0] = _route("meta", "muse-spark-1.3")
    second = summarize_payload(payload)
    assert second["available"] is True and second["cached"] is False
    assert second["headline"] == "Muse summary."
    assert [route.provider for route in calls] == ["openai", "meta"]


def test_failed_or_revoked_client_route_never_uses_managed_cache_or_vertex(monkeypatch):
    payload = _payload("ask-cubie")
    route = _route("openai", "gpt-6-luna")
    active = [True]
    managed_calls = []

    def resolve(feature, capability, *, default_model):
        if not active[0]:
            raise IncompatibleModelRoute("revoked credential")
        return route

    def failed_provider_call(_route, _prompt, *, json_mode):
        assert json_mode is True
        raise ProviderRequestError("HTTP 401 private provider response")

    def managed_call(prompt):
        managed_calls.append(prompt)
        raise AssertionError("managed Vertex must not be called")

    monkeypatch.setattr(summary_module, "resolve_model_route", resolve)
    monkeypatch.setattr(summary_module, "generate_text_on_route", failed_provider_call)
    monkeypatch.setattr(summary_module, "_default_invoke", managed_call)
    failure = summarize_payload(payload)
    assert failure["available"] is False
    assert "admin" in failure["reason"].lower()
    assert "401" not in failure["reason"]
    active[0] = False
    revoked = summarize_payload(payload)
    assert revoked["available"] is False
    assert "admin" in revoked["reason"].lower()
    assert managed_calls == []

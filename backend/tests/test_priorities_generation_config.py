"""Every AI-Priorities LLM call must SEND its generation config.

``LLMJsonClient.invoke_json`` once called
``generate_content(model=..., contents=prompt)`` with no ``config=``. The
per-role temperatures in ``PrioritiesConfig`` (and the admin-tuned ones stored
under ``priorities_agent_config``) were kept on the client and never used, so
every stage ran at Gemini's default temperature, and JSON was scraped out of
free text by ``safe_json`` with nothing asking the model for JSON at all.

These tests hold the call itself, not the attribute: a stored temperature that
never reaches ``generate_content`` is exactly the bug.

Pure logic — the genai client is a recording fake. No GCP / LLM / DB.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.genai import types

from rubik_core.gemini_tuning import thinking_config_for
from rubik_core.priorities import PrioritiesConfig, PrioritiesOrchestrator
from rubik_core.priorities.agents import (
    CalendarChannelAgent,
    CriticAgent,
    DecisionMakerAgent,
    GmailChannelAgent,
    SummarizerAgent,
)
from rubik_core.priorities.agents.base import LLMJsonClient, safe_json


class _RecordingGenai:
    """Stands in for the google-genai client. Records every call's kwargs."""

    def __init__(self, text: str = '{"ok": true}'):
        self.text = text
        self.calls: list[dict] = []
        self.models = SimpleNamespace(generate_content=self._generate_content)

    def _generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(text=self.text)


def _client(model_key: str = "gemini-flash", temperature: float = 0.3):
    llm = LLMJsonClient(model_key=model_key, temperature=temperature)
    fake = _RecordingGenai()
    llm._client = fake
    return llm, fake


def test_the_call_carries_the_roles_temperature_and_asks_for_json():
    llm, fake = _client(temperature=0.37)

    assert llm.invoke_json("prompt") == {"ok": True}

    [call] = fake.calls
    config = call.get("config")
    assert isinstance(config, types.GenerateContentConfig), (
        "generate_content was called without a config — the role's temperature "
        "never reaches the model"
    )
    assert config.temperature == 0.37
    assert config.response_mime_type == "application/json"
    # The repo's JSON convention is mime type + tolerant parse, never a schema.
    assert config.response_schema is None


@pytest.mark.parametrize(
    "model_key", ["gemini-flash", "gemini-flash-lite", "gemini-pro"]
)
def test_the_thinking_knob_is_the_one_gemini_tuning_picks_for_the_family(model_key):
    """2.5 Pro keeps its own thinking (it cannot be disabled); the 3.x Flash
    tiers get ``thinking_level``. Same helper every other direct call uses."""
    llm, fake = _client(model_key=model_key)
    llm.invoke_json("prompt")

    sent = fake.calls[0]["config"].thinking_config
    assert sent == thinking_config_for(llm.model_id)


def test_every_retry_sends_the_same_config():
    llm, fake = _client(temperature=0.2)
    fake.text = "not json at all"

    assert llm.invoke_json("prompt", fallback={"f": 1}, attempts=2) == {"f": 1}

    assert len(fake.calls) == 2
    assert all(c["config"].temperature == 0.2 for c in fake.calls)


def test_a_config_that_cannot_be_built_takes_the_fallback_path(monkeypatch):
    """The client's resilience contract: a bad agent never breaks the bundle.
    Building the config must not become a way around it."""
    llm, fake = _client()

    def _boom(self):
        raise ValueError("bad thinking level")

    monkeypatch.setattr(LLMJsonClient, "generation_config", _boom)

    assert llm.invoke_json("prompt", fallback={"f": 1}) == {"f": 1}
    assert fake.calls == []


def test_a_fenced_answer_still_parses():
    """JSON mode is the belt; ``safe_json`` stays the braces."""
    llm, fake = _client()
    fake.text = '```json\n{"summary": "x"}\n```'
    assert llm.invoke_json("prompt") == {"summary": "x"}


def test_invalid_model_json_never_exposes_private_mail_in_logs_or_errors(caplog):
    llm, fake = _client()
    fake.text = "PRIVATE-MAIL-SUBJECT: broken { json"

    with caplog.at_level("WARNING"):
        assert llm.invoke_json("prompt", fallback={}) == {}
    assert "PRIVATE-MAIL-SUBJECT" not in caplog.text

    with pytest.raises(ValueError) as exc:
        safe_json(fake.text)
    assert "PRIVATE-MAIL-SUBJECT" not in str(exc.value)


# ── each stage sends ITS OWN temperature ────────────────────────────────────


def _distinct_temperatures() -> PrioritiesConfig:
    config = PrioritiesConfig.default()
    config.temperatures.gmail_agent = 0.11
    config.temperatures.calendar_agent = 0.12
    config.temperatures.summarizer = 0.41
    config.temperatures.decision_maker = 0.51
    config.temperatures.critic = 0.21
    return config


def _sent_temperature(llm: LLMJsonClient) -> float:
    fake = _RecordingGenai()
    llm._client = fake
    llm.invoke_json("prompt", fallback={})
    return fake.calls[0]["config"].temperature


def test_each_pipeline_stage_sends_the_temperature_configured_for_its_role():
    config = _distinct_temperatures()

    sent = {
        "gmail_agent": _sent_temperature(
            GmailChannelAgent(user_email="me@acme.example", config=config).client
        ),
        "calendar_agent": _sent_temperature(
            CalendarChannelAgent(user_email="me@acme.example", config=config).client
        ),
        "summarizer": _sent_temperature(SummarizerAgent(config).client),
        "decision_maker": _sent_temperature(DecisionMakerAgent(config).client),
        "critic": _sent_temperature(CriticAgent(config).client),
    }

    assert sent == {
        "gmail_agent": 0.11,
        "calendar_agent": 0.12,
        "summarizer": 0.41,
        "decision_maker": 0.51,
        "critic": 0.21,
    }


def test_an_admin_tuned_temperature_reaches_the_call(monkeypatch):
    """Admin → Priorities stores temperatures in ``workspace_settings``. The
    orchestrator loads them when no config is pinned — and they must arrive at
    ``generate_content``, not stop at the client attribute."""
    import rubik_core.priorities.settings_store as store

    blob = store._default_config_blob()
    blob["agents"]["critic"]["temperature"] = 0.05
    blob["agents"]["decision_maker"]["temperature"] = 0.9
    monkeypatch.setattr(store, "load_settings_blob", lambda: blob)

    orch = PrioritiesOrchestrator()

    assert _sent_temperature(orch.critic.client) == 0.05
    assert _sent_temperature(orch.decision_maker.client) == 0.9


def test_an_argon_high_choice_sends_argon_with_high_thinking():
    """Admin → AI Priorities offers "gemini-4-argon@high". The id must reach
    Vertex bare (the suffix is not a model) and the level must reach the config,
    winning over the "medium" Argon would otherwise inherit, which thought 0
    tokens on every priorities step in the replay."""
    from rubik_core.priorities.settings_store import PROVIDER_MODEL_CATALOG

    assert "gemini-4-argon@high" in PROVIDER_MODEL_CATALOG["vertex"]

    llm, fake = _client(model_key="gemini-4-argon@high", temperature=0.55)
    llm.invoke_json("prompt")

    [call] = fake.calls
    assert call["model"] == "gemini-4-argon"
    config = call["config"]
    assert config.thinking_config.thinking_level == types.ThinkingLevel.HIGH
    assert config.thinking_config.thinking_budget is None
    assert config.temperature == 0.55
    assert config.response_mime_type == "application/json"

"""Regression tests for the chat latency/resilience tuning:

- ``low_latency_planner`` must curb Gemini "thinking" correctly per model family
  (disable on Flash, leave Pro alone, use thinking_level on Gemini 3.x), because
  the chat model is admin-selectable and a wrong knob 400s the whole turn.
- The ADK runaway-loop backstop error must classify to a specific, user-safe
  message instead of the generic runtime error.

Pure logic — no GCP/LLM/DB.
"""

from __future__ import annotations

import pytest
from google.genai.types import ThinkingLevel

from rubik_core.adk_agent.model_tuning import low_latency_planner
from rubik_core.agent_runtime.chat_config import CHAT_MODEL_CATALOG
from rubik_core.config import settings
from rubik_core.orchestration.adk_orchestrator import (
    ADKErrorCategory,
    _classify_adk_error,
    _user_safe_adk_message,
)


@pytest.mark.parametrize(
    "model_id",
    ["gemini-flash-latest", "gemini-2.5-flash", "gemini-flash-lite-latest"],
)
def test_25_family_tiers_use_configured_thinking_budget(model_id):
    """2.5-family Flash / Flash-Lite get a numeric thinking_budget equal to the
    configured setting. Post "Migration Plan" RCA the default is a small non-zero
    budget (512) — enough for the model to run its grounding check before
    answering — not 0.

    "gemini-flash-latest" is in here because it was still serving 2.5 Flash when
    probed on 2026-08-12 (it 400s on thinking_level). It is no longer the FLASH
    default, but stays in the Admin → Chat catalog as an escape hatch."""
    planner = low_latency_planner(model_id)
    assert planner is not None
    assert planner.thinking_config.thinking_budget == settings.CHAT_LLM_THINKING_BUDGET
    assert planner.thinking_config.thinking_level is None


def test_default_thinking_budget_is_small_nonzero():
    """Lock the post-RCA default: a small positive budget, not 0 (which skipped
    the grounding step and let the model confabulate a search)."""
    assert settings.CHAT_LLM_THINKING_BUDGET == 512


def test_zero_budget_still_disables_thinking(monkeypatch):
    """Setting the budget to 0 still fully disables thinking on Flash tiers."""
    monkeypatch.setattr(settings, "CHAT_LLM_THINKING_BUDGET", 0)
    planner = low_latency_planner("gemini-flash-latest")
    assert planner is not None
    assert planner.thinking_config.thinking_budget == 0


def test_pro_is_left_on_dynamic_default():
    """2.5 Pro cannot disable thinking (budget 0 is a 400) — must return None."""
    assert low_latency_planner("gemini-2.5-pro") is None


def test_gemini_3_uses_thinking_level_not_budget():
    """Gemini 3.x speaks thinking_level, not a numeric budget."""
    planner = low_latency_planner("gemini-3.5-flash-lite")
    assert planner is not None
    assert planner.thinking_config.thinking_budget is None
    assert planner.thinking_config.thinking_level is not None


def test_gemini_3_flash_lite_asks_for_minimal_thinking():
    """Google's recommended setting for 3.5 Flash-Lite — it's what makes it as
    fast as 2.5 Flash. Locked because the default is the whole point of the
    upgrade, not an incidental value."""
    assert settings.LITE_LLM_THINKING_LEVEL == "minimal"
    planner = low_latency_planner("gemini-3.5-flash-lite")
    assert planner.thinking_config.thinking_level == ThinkingLevel.MINIMAL


def test_chat_tier_thinks_above_the_rca_floor():
    """The RCA guard, and the reason there are two levels instead of one.

    A chat tier that emits ZERO thinking tokens is the "Migration Plan" state —
    thinking off, so the model skips "should I even search, and for what?" and
    confabulates a query. It's why the 2.5 budget went 0 → 512.

    Pinned to "medium" rather than "low" because the level scale was re-based
    when FLASH moved to 3.8: measured 2026-09-08, 3.8 at "low" emits 0 thinking
    tokens where 3.6 at "low" emitted 573, and 3.8 at "medium" emits 573. So
    carrying "low" across the bump would have re-entered the RCA state silently —
    a supported level that merely thinks less raises nothing. If FLASH is rolled
    back to 3.6 or older, this goes back to "low"."""
    assert settings.CHAT_LLM_THINKING_LEVEL == "medium"
    planner = low_latency_planner(settings.VERTEX_LLM_MODEL_FLASH)
    assert planner.thinking_config.thinking_level == ThinkingLevel.MEDIUM


def test_lite_and_chat_levels_are_independent(monkeypatch):
    """Turning one dial must not move the other — they exist precisely because
    the two tiers need different answers."""
    monkeypatch.setattr(settings, "LITE_LLM_THINKING_LEVEL", "high")
    assert (
        low_latency_planner("gemini-3.5-flash-lite").thinking_config.thinking_level
        == ThinkingLevel.HIGH
    )
    assert (
        low_latency_planner("gemini-3.6-flash").thinking_config.thinking_level
        == ThinkingLevel.MEDIUM
    )


def test_unpinned_catalog_lite_ids_take_the_lite_level():
    """The Admin → Chat catalog offers Lite ids we don't pin; they're still Lite
    work and must take the Lite level, not the chat one."""
    planner = low_latency_planner("gemini-3.1-flash-lite")
    assert planner.thinking_config.thinking_level == ThinkingLevel.MINIMAL


def test_gemini_3_pro_is_floored_at_low(monkeypatch):
    """Pro tiers don't offer "minimal" — asking for it is a 400, and the chat
    model is admin-selectable, so a 3.x Pro id can reach the planner."""
    monkeypatch.setattr(settings, "CHAT_LLM_THINKING_LEVEL", "minimal")
    planner = low_latency_planner("gemini-3.1-pro")
    assert planner is not None
    assert planner.thinking_config.thinking_level == ThinkingLevel.LOW


def test_flash_tier_is_floored_at_low_on_37_plus(monkeypatch):
    """Gemini dropped "minimal" at 3.7 — 3.7/3.8 Flash answer a hard
    ``400 … Thinking level is unsupported: THINKING_LEVEL_MINIMAL`` (probed
    2026-09-08), unlike 3.6 which still accepts it. The level is env-tunable
    with no deploy, so a stale "minimal" must degrade to latency, not 400 every
    turn of chat."""
    monkeypatch.setattr(settings, "CHAT_LLM_THINKING_LEVEL", "minimal")
    for model_id in ("gemini-3.7-flash", "gemini-3.8-flash"):
        planner = low_latency_planner(model_id)
        assert planner is not None, model_id
        assert planner.thinking_config.thinking_level == ThinkingLevel.LOW, model_id


def test_thinking_level_is_env_tunable(monkeypatch):
    """The dial back if 3.5 Flash-Lite's answer quality dips — no redeploy."""
    monkeypatch.setattr(settings, "LITE_LLM_THINKING_LEVEL", "low")
    planner = low_latency_planner("gemini-3.5-flash-lite")
    assert planner.thinking_config.thinking_level == ThinkingLevel.LOW


def test_empty_thinking_level_is_the_escape_hatch(monkeypatch):
    """Blank keeps the model's own default (the thinking_budget=-1 twin)."""
    monkeypatch.setattr(settings, "LITE_LLM_THINKING_LEVEL", "")
    assert low_latency_planner("gemini-3.5-flash-lite") is None
    monkeypatch.setattr(settings, "CHAT_LLM_THINKING_LEVEL", "")
    assert low_latency_planner("gemini-3.6-flash") is None


def test_bogus_thinking_level_falls_back_instead_of_400ing(monkeypatch):
    """google-genai does NOT reject an unknown level — ThinkingConfig accepts it
    with only a warning and Vertex 400s the turn. A typo'd env var must degrade
    to a valid level instead of taking chat down."""
    monkeypatch.setattr(settings, "LITE_LLM_THINKING_LEVEL", "minimial")
    planner = low_latency_planner("gemini-3.5-flash-lite")
    assert planner is not None
    assert planner.thinking_config.thinking_level == ThinkingLevel.LOW


def test_negative_budget_is_escape_hatch(monkeypatch):
    """A negative budget keeps the model's dynamic default (no planner)."""
    monkeypatch.setattr(settings, "CHAT_LLM_THINKING_BUDGET", -1)
    assert low_latency_planner("gemini-flash-latest") is None


def test_every_catalog_model_is_handled_without_raising():
    """Whatever an admin picks from the catalog must yield a valid planner or
    None — never an exception."""
    for model_id in CHAT_MODEL_CATALOG:
        planner = low_latency_planner(model_id)
        assert planner is None or planner.thinking_config is not None


def test_llm_calls_limit_classifies_to_loop_limit():
    """The ADK LlmCallsLimitExceededError message must map to AGENT_LOOP_LIMIT
    with its own user-facing message, not the generic runtime error."""
    err = Exception("Max number of llm calls limit of `15` exceeded")
    category = _classify_adk_error(err)
    assert category == ADKErrorCategory.AGENT_LOOP_LIMIT
    msg = _user_safe_adk_message(category)
    assert msg != _user_safe_adk_message(ADKErrorCategory.AGENT_RUNTIME_ERROR)
    assert "circles" in msg.lower() or "smaller" in msg.lower()


def test_vertex_504_classifies_as_downstream_not_simpler_question():
    """A Vertex 504 DEADLINE_EXCEEDED is the model service failing. It must not
    fall into TIMEOUT, whose message tells the user to ask a simpler question."""
    from google.genai.errors import ServerError

    err = ServerError(
        504,
        {
            "error": {
                "code": 504,
                "message": "Deadline expired before operation could complete.",
                "status": "DEADLINE_EXCEEDED",
            }
        },
    )
    category = _classify_adk_error(err)
    assert category == ADKErrorCategory.DOWNSTREAM_API_ERROR
    assert "simpler" not in _user_safe_adk_message(category).lower()


def test_plain_timeout_still_classifies_as_timeout():
    """Non-Vertex timeouts (a slow tool, our own deadline) keep the TIMEOUT path."""
    assert _classify_adk_error(TimeoutError("read timeout")) == ADKErrorCategory.TIMEOUT

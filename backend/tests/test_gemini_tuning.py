"""Tests for ``generation_config_for`` — the thinking tuning applied to plain
``generate_content`` calls (Slack summaries, DAM descriptions, the home-page
discovery hops), as opposed to the ADK planner path covered by
``test_chat_perf_tuning.py``.

These call sites pass the result straight through as ``config=``, so the
contract is narrow and load-bearing: never mutate a caller's config, never
override a config the caller tuned itself, and return ``None`` (not an empty
config object) when there is nothing to say.

Pure logic — no GCP/LLM/DB.
"""

from __future__ import annotations

from types import SimpleNamespace

from google.genai import types
from google.genai.types import ThinkingLevel

from rubik_core.config import settings
from rubik_core.gemini_tuning import (
    _supports_minimal,
    generation_config_for,
    thinking_config_for,
    uses_thinking_level,
)


def test_lite_tier_call_sites_get_minimal_thinking():
    """The LITE tier is Gemini 3.5 Flash-Lite, which thinks by default. The
    summarize/extract call sites were tuned for speed and must stay that way."""
    config = generation_config_for(settings.VERTEX_LLM_MODEL_LITE)
    assert config is not None
    assert config.thinking_config.thinking_level == ThinkingLevel.MINIMAL


def test_caller_config_is_preserved_and_not_mutated():
    base = types.GenerateContentConfig(temperature=0.1, max_output_tokens=256)
    config = generation_config_for("gemini-3.5-flash-lite", base)

    assert config.temperature == 0.1
    assert config.max_output_tokens == 256
    assert config.thinking_config.thinking_level == ThinkingLevel.MINIMAL
    assert base.thinking_config is None  # the caller's object is untouched


def test_explicit_thinking_config_wins():
    """A call site that deliberately tuned its own thinking keeps it."""
    base = types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_level="high")
    )
    config = generation_config_for("gemini-3.5-flash-lite", base)
    assert config.thinking_config.thinking_level == ThinkingLevel.HIGH


def test_returns_none_when_there_is_nothing_to_say(monkeypatch):
    """No base config and no opinion on thinking → None, so callers can pass the
    result through as config= without a branch."""
    monkeypatch.setattr(settings, "LITE_LLM_THINKING_LEVEL", "")
    assert generation_config_for("gemini-3.5-flash-lite") is None


def test_base_survives_when_thinking_is_left_alone(monkeypatch):
    monkeypatch.setattr(settings, "LITE_LLM_THINKING_LEVEL", "")
    base = types.GenerateContentConfig(temperature=0.1)
    assert generation_config_for("gemini-3.5-flash-lite", base) is base


def test_25_family_still_gets_the_numeric_budget():
    """The tier ids are env-overridable, so a 2.5 id can still reach here — it
    must get the budget knob, not a level (which 2.5 doesn't understand)."""
    thinking = thinking_config_for("gemini-2.5-flash")
    assert thinking.thinking_budget == settings.CHAT_LLM_THINKING_BUDGET
    assert thinking.thinking_level is None


def test_minimal_is_floored_where_the_family_dropped_it(monkeypatch):
    """Gemini dropped thinking_level="minimal" at 3.7: 3.5 Flash-Lite and 3.6
    Flash accept it, 3.7/3.8 Flash answer a hard ``400 … Thinking level is
    unsupported: THINKING_LEVEL_MINIMAL`` (probed on Vertex global 2026-09-08).

    It has to be floored rather than rejected, because "minimal" is a legitimate
    value that some models simply can't take: it passes level validation, both
    levels are env-tunable with no deploy, and the chat model is admin-selectable
    at runtime. Unfloored it would 400 every single turn."""
    monkeypatch.setattr(settings, "CHAT_LLM_THINKING_LEVEL", "minimal")
    for model_id in ("gemini-3.7-flash", "gemini-3.8-flash"):
        thinking = thinking_config_for(model_id)
        assert thinking.thinking_level == ThinkingLevel.LOW, model_id


def test_minimal_still_reaches_the_families_that_accept_it(monkeypatch):
    """The floor must not become a blanket ban — 3.6 and older still take
    "minimal", and the LITE tier depends on it for its speed."""
    monkeypatch.setattr(settings, "CHAT_LLM_THINKING_LEVEL", "minimal")
    for model_id in ("gemini-3.6-flash", "gemini-3-flash", "gemini-3.5-flash-lite"):
        thinking = thinking_config_for(model_id)
        assert thinking.thinking_level == ThinkingLevel.MINIMAL, model_id


def test_unversioned_id_is_assumed_not_to_support_minimal():
    """An id with no family version ("gemini-flash-latest") is a coin flip between
    accepting "minimal" and 400-ing the turn, so the helper resolves the ambiguity
    toward the level that always serves.

    Pins the helper's contract, not a live path: thinking_config_for only consults
    it behind a ``uses_thinking_level`` guard, and any id that passes carries a
    version. A future caller without that guard should still get the safe answer.

    Note "gemini-3-flash" is NOT this case — it parses as 3.0 and does support
    minimal (covered above)."""
    assert _supports_minimal("gemini-flash-latest") is False
    assert _supports_minimal("gemini-3-flash") is True
    assert _supports_minimal("gemini-3.8-flash") is False


def test_family_is_read_off_the_version_not_a_gemini_3_substring():
    """ "gemini-4-argon" has no "gemini-3" in it, so the old substring check sent
    it down the 2.5 budget branch. Argon answers any numeric budget, 0 included,
    with a hard 400 (probed on Vertex global 2026-10-14), so that was every call
    failing, not a quiet loss of tuning."""
    assert uses_thinking_level("gemini-4-argon") is True
    assert uses_thinking_level("gemini-3.8-flash") is True
    assert uses_thinking_level("gemini-3-flash") is True
    assert uses_thinking_level("gemini-2.5-pro") is False
    assert uses_thinking_level("gemini-2.5-flash") is False
    # An alias carries no family; it keeps the knob it has always served.
    assert uses_thinking_level("gemini-flash-latest") is False


def test_argon_gets_a_level_and_never_a_budget():
    thinking = thinking_config_for("gemini-4-argon")
    assert thinking.thinking_budget is None
    assert thinking.thinking_level == ThinkingLevel(
        settings.CHAT_LLM_THINKING_LEVEL.upper()
    )


def test_argon_floors_minimal(monkeypatch):
    """Argon 400s on "minimal" exactly like 3.7+ (probed 2026-10-14), and the
    version compare must reach a major-4 id without a new special case."""
    monkeypatch.setattr(settings, "CHAT_LLM_THINKING_LEVEL", "minimal")
    assert thinking_config_for("gemini-4-argon").thinking_level == ThinkingLevel.LOW


def test_argon_through_generation_config_for():
    """The priorities agents reach Argon through generation_config_for, so the
    level has to survive the merge with their JSON config."""
    base = types.GenerateContentConfig(
        temperature=0.2, response_mime_type="application/json"
    )
    config = generation_config_for("gemini-4-argon", base)
    assert config.thinking_config.thinking_budget is None
    assert config.thinking_config.thinking_level is not None
    assert config.response_mime_type == "application/json"


def test_quiet_thinking_helpers_send_a_level_to_argon():
    """Voice input and the Ask Cubie file reader tune their own thinking. They
    used their own copy of the "gemini-3" substring check, and would have sent
    Argon thinking_budget=0 — a hard 400."""
    from rubik_core.chat_files.reader import _quiet_thinking as reader_quiet
    from rubik_core.voice.transcribe import _quiet_thinking as voice_quiet

    for helper in (reader_quiet, voice_quiet):
        thinking = helper("gemini-4-argon")
        assert thinking.thinking_level == ThinkingLevel.LOW, helper.__module__
        assert thinking.thinking_budget is None, helper.__module__
        # …and 2.5 Flash still gets its zero budget.
        assert helper("gemini-2.5-flash").thinking_budget == 0, helper.__module__


def test_pro_is_still_floored_at_low(monkeypatch):
    """Pro never offered "minimal" — the version comparison must not regress the
    check that predates it."""
    monkeypatch.setattr(settings, "CHAT_LLM_THINKING_LEVEL", "minimal")
    assert thinking_config_for("gemini-3.1-pro").thinking_level == ThinkingLevel.LOW


def test_flash_tier_default_asks_for_a_level_the_flash_tier_accepts():
    """End-to-end on the shipped defaults: whatever FLASH and
    CHAT_LLM_THINKING_LEVEL are set to must be a combination Vertex accepts, so a
    tier bump can't quietly pair an id with a level it 400s on."""
    thinking = thinking_config_for(settings.VERTEX_LLM_MODEL_FLASH)
    assert thinking is not None
    assert thinking.thinking_level != ThinkingLevel.MINIMAL


def test_none_model_id_falls_back_to_the_flash_tier():
    assert thinking_config_for(None) == thinking_config_for(
        settings.VERTEX_LLM_MODEL_FLASH
    )


# ── /api/health/models probes the real call shape ──────────────────────────
def test_model_health_check_sends_the_tuned_call_shape():
    """A bare ping proves the model id resolves, not that our requests work: the
    thinking knob is env-tunable and google-genai doesn't validate the level
    client-side, so a bad one 400s every real call while a bare ping stays
    green. The diagnostic must exercise what production sends."""
    from unittest.mock import patch

    from rubik_core.models import check_configured_models

    seen = {}

    def _fake_generate_content(model, contents, config=None):
        seen[model] = config
        return SimpleNamespace(model_version=f"{model}-002")

    fake = SimpleNamespace(
        models=SimpleNamespace(generate_content=_fake_generate_content)
    )
    with patch("rubik_core.models.get_genai_client", return_value=fake):
        report = check_configured_models()

    assert report["ok"] is True

    lite_config = seen[settings.VERTEX_LLM_MODEL_LITE]
    assert lite_config.max_output_tokens == 1  # the cheap-ping intent survives
    assert lite_config.thinking_config.thinking_level == ThinkingLevel.MINIMAL
    # …and the payload says what was actually exercised.
    assert report["models"]["lite"]["thinking"] == "level=minimal"


def test_model_health_check_reports_what_a_floating_alias_resolved_to():
    """ "gemini-flash-latest: ok" doesn't say which family served it, and the
    family is what picks the thinking knob. Report the resolved version so a
    silent Google repoint is visible instead of showing up as a wrong knob on a
    green check."""
    from unittest.mock import patch

    from rubik_core.models import check_configured_models

    def _serve(model, contents, config=None):
        return SimpleNamespace(model_version="gemini-2.5-flash-002")

    fake = SimpleNamespace(models=SimpleNamespace(generate_content=_serve))
    with patch("rubik_core.models.get_genai_client", return_value=fake):
        report = check_configured_models()

    assert report["models"]["flash"]["served"] == "gemini-2.5-flash-002"
    assert report["models"]["flash"]["resolves_alias"] is True


def test_served_is_never_defaulted_to_the_requested_id():
    """ "No answer" must not be dressed up as "no drift". Vertex currently echoes
    the requested id (and returns nothing at all on some paths); defaulting the
    field to model_id would make both look identical to a real resolution."""
    from unittest.mock import patch

    from rubik_core.models import check_configured_models

    def _serve(model, contents, config=None):
        return SimpleNamespace(model_version=None)

    fake = SimpleNamespace(models=SimpleNamespace(generate_content=_serve))
    with patch("rubik_core.models.get_genai_client", return_value=fake):
        report = check_configured_models()

    assert report["models"]["lite"]["served"] is None
    assert report["models"]["lite"]["resolves_alias"] is False


def test_echoed_alias_is_not_reported_as_a_resolution():
    """Vertex echoing "gemini-flash-latest" back tells us nothing about which
    family served it — it must not read as the alias having been resolved."""
    from unittest.mock import patch

    from rubik_core.models import check_configured_models

    def _serve(model, contents, config=None):
        return SimpleNamespace(model_version=model)  # echo, not a resolution

    fake = SimpleNamespace(models=SimpleNamespace(generate_content=_serve))
    with patch("rubik_core.models.get_genai_client", return_value=fake):
        report = check_configured_models()

    assert report["models"]["flash"]["resolves_alias"] is False


def test_model_health_check_names_a_bad_call_shape_distinctly():
    """A 400 means the id is fine and the config isn't — the fix is
    CHAT_LLM_THINKING_LEVEL, not the model id, so it can't read as a generic
    error or as the model being gone."""
    from unittest.mock import patch

    from rubik_core.models import check_configured_models

    def _boom(model, contents, config=None):
        raise RuntimeError("400 INVALID_ARGUMENT: thinking_level not supported")

    fake = SimpleNamespace(models=SimpleNamespace(generate_content=_boom))
    with patch("rubik_core.models.get_genai_client", return_value=fake):
        report = check_configured_models()

    assert report["ok"] is False
    assert report["models"]["lite"]["status"] == "bad_request"
    assert report["models"]["lite"]["thinking"] == "level=minimal"


def test_split_model_choice():
    """Admin choices may pin a thinking level; plain ids never grow one, and a
    bad level is dropped rather than sent (Vertex would 400 it)."""
    from rubik_core.gemini_tuning import split_model_choice

    assert split_model_choice("gemini-4-argon@high") == ("gemini-4-argon", "high")
    assert split_model_choice("gemini-4-argon@HIGH ") == ("gemini-4-argon", "high")
    assert split_model_choice("gemini-2.5-pro") == ("gemini-2.5-pro", None)
    assert split_model_choice("gemini-4-argon@") == ("gemini-4-argon", None)
    assert split_model_choice("gemini-4-argon@extreme") == ("gemini-4-argon", None)

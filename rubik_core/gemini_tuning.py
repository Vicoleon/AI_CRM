"""Per-family Gemini "thinking" tuning — one source of truth.

Gemini's default *dynamic* thinking budget spends 1-5s of reasoning tokens
before the first answer/tool-call token. That is the stall class behind the 79s
first-turn incident (see ``CHAT_LLM_CALL_TIMEOUT_MS`` in ``config.py``), and the
knob that curbs it differs by model family:

- **Gemini 2.5 Flash / Flash-Lite** — ``thinking_budget=0`` disables it.
- **Gemini 2.5 Pro** — thinking CANNOT be disabled (forcing 0 is a 400), so the
  model's dynamic default is left alone.
- **Gemini 3.x and later** — the numeric budget is gone (3.x accepts one and
  silently ignores it, which is worse than erroring; 4 Argon rejects it with a
  hard 400); these speak ``thinking_level``. Flash-Lite takes
  ``LITE_LLM_THINKING_LEVEL`` ("minimal"), Flash and everything else take
  ``CHAT_LLM_THINKING_LEVEL`` ("medium"). "minimal" is not universal — Pro has
  never offered it and 3.7+ dropped it — so it is floored at "low" wherever it
  is unsupported.

The family is read off the version in the id (:func:`uses_thinking_level`), not
a ``"gemini-3"`` substring: "gemini-4-argon" carries no "gemini-3" and used to
fall through to the 2.5 budget branch, which 400s every Argon call.

This module deliberately imports only ``google.genai`` — no ADK. Plain
``client.models.generate_content`` call sites (Slack summaries, DAM
descriptions, the home-page discovery hops) need the same tuning as the ADK
agents, and most of them live in modules that have no other reason to pull ADK
in. The ADK-side wrapper is ``rubik_core.adk_agent.model_tuning``, which builds
a ``BuiltInPlanner`` around :func:`thinking_config_for`.
"""

from __future__ import annotations

import logging
import re

from google.genai import types as genai_types

from rubik_core.config import settings

logger = logging.getLogger(__name__)

# Levels Gemini 3.x accepts, cheapest-first. Kept as a plain tuple rather than
# read off google.genai's ThinkingLevel enum because that enum does NOT reject
# unknown values — ThinkingConfig(thinking_level="bogus") only emits a warning
# and then 400s at request time, which is exactly the failure an env-tunable
# setting invites.
_VALID_THINKING_LEVELS: tuple[str, ...] = ("minimal", "low", "medium", "high")

# Lowest level every 3.x family accepts, and the fallback for a setting we can't
# send as written. "minimal" is NOT universal — see _supports_minimal.
_MIN_THINKING_LEVEL = "low"

# "minimal" was dropped as of Gemini 3.7. Probed live on Vertex global
# (2026-09-08): 3.5 Flash-Lite and 3.6 Flash accept it, while 3.7 Flash and
# 3.8 Flash reject it with a hard
# ``400 INVALID_ARGUMENT … Thinking level is unsupported: THINKING_LEVEL_MINIMAL``
# that kills the whole turn. Pro tiers have never offered it. 4 Argon answers
# the same 400 (probed 2026-10-14), which the version compare already covers.
#
# This is a FLOOR, not a validation error, because "minimal" is a legitimate
# value that simply cannot be sent to some models: both levels are env-tunable
# with no deploy (CHAT_LLM_THINKING_LEVEL / LITE_LLM_THINKING_LEVEL) and the chat
# model is admin-selectable at runtime (Admin → Chat, ``CHAT_MODEL_CATALOG``).
# "minimal" passes _VALID_THINKING_LEVELS, so without this floor it sails past
# validation and 400s every single chat turn.
_MINIMAL_DROPPED_AT: tuple[int, int] = (3, 7)

# Family version out of an id: "gemini-3.8-flash" → (3, 8), "gemini-3-flash" →
# (3, 0), "gemini-4-argon" → (4, 0). Version-compared rather than listed
# id-by-id so the next release inherits the right answer instead of 400-ing
# until someone adds it here.
_FAMILY_VERSION_RE = re.compile(r"gemini-(\d+)(?:\.(\d+))?")

# First major family that speaks thinking_level instead of thinking_budget.
# Probed live on Vertex global (2026-10-14): 4 Argon rejects any numeric budget,
# 0 included, with ``400 INVALID_ARGUMENT … Thinking budget is not supported for
# this model. Use thinking level instead``, and accepts low/medium/high.
_THINKING_LEVEL_FROM_MAJOR = 3

# How a Flash-Lite id is told apart from a Flash one. Substring rather than an
# equality check against VERTEX_LLM_MODEL_LITE because the Admin → Chat catalog
# offers Lite ids we don't pin ("gemini-flash-lite-latest", "gemini-3.1-flash-
# lite"), and those want the Lite level too. Google has used this "-lite" suffix
# consistently across 2.5/3.1/3.5/3.6.
_LITE_MARKER = "-lite"


def _family_version(model_id: str) -> tuple[int, int] | None:
    """``(major, minor)`` parsed out of ``model_id``, or ``None`` when the id
    carries no version (a ``-latest`` alias)."""
    match = _FAMILY_VERSION_RE.search(model_id.lower())
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2) or 0)


def uses_thinking_level(model_id: str) -> bool:
    """Whether ``model_id`` takes ``thinking_level`` (Gemini 3.x and later)
    rather than ``thinking_budget`` (2.5).

    The one place that decides it, so call sites that tune their own thinking
    (voice input, the Ask Cubie file reader) can't drift from this module. An
    unversioned alias answers False: its family is unknown, and the budget knob
    is what a ``-latest`` alias has always served (see config.py, 2026-08-12).
    """
    version = _family_version(model_id)
    return version is not None and version[0] >= _THINKING_LEVEL_FROM_MAJOR


def _supports_minimal(model_id: str) -> bool:
    """Whether ``model_id``'s family accepts ``thinking_level="minimal"``."""
    if "pro" in model_id:
        return False
    version = _family_version(model_id)
    if version is None:
        # The family is unknown. Assume the newer behavior: flooring to "low"
        # always serves, sending "minimal" may 400.
        return False
    return version < _MINIMAL_DROPPED_AT


def _resolve_thinking_level(model_id: str) -> str | None:
    """Normalize the configured thinking level for ``model_id``.

    Flash-Lite tiers take ``LITE_LLM_THINKING_LEVEL`` ("minimal"), everything
    else takes ``CHAT_LLM_THINKING_LEVEL`` ("medium"). They differ for a reason —
    "minimal" is zero thinking tokens, which is safe for one-shot extraction and
    is the hallucination-RCA state on the chat coordinator. See config.py.

    A level the model cannot accept is floored to ``_MIN_THINKING_LEVEL`` rather
    than raising, so a stale env value degrades latency instead of 400-ing.

    Returns ``None`` when the model's own default should be left untouched
    (the empty-string escape hatch — the ``thinking_budget=-1`` twin).
    """
    if _LITE_MARKER in model_id:
        setting_name = "LITE_LLM_THINKING_LEVEL"
        configured = settings.LITE_LLM_THINKING_LEVEL
    else:
        setting_name = "CHAT_LLM_THINKING_LEVEL"
        configured = settings.CHAT_LLM_THINKING_LEVEL

    level = (configured or "").strip().lower()
    if not level:
        return None
    if level not in _VALID_THINKING_LEVELS:
        logger.warning(
            "%s=%r is not one of %s — falling back to %r. An unknown level is "
            "NOT rejected client-side; it 400s at Vertex.",
            setting_name,
            configured,
            _VALID_THINKING_LEVELS,
            _MIN_THINKING_LEVEL,
        )
        return _MIN_THINKING_LEVEL
    if level == "minimal" and not _supports_minimal(model_id):
        logger.warning(
            "%s=minimal is unsupported by %s (Gemini dropped it at %s) — using "
            "%r instead. Sending it would 400 the whole turn.",
            setting_name,
            model_id,
            ".".join(str(part) for part in _MINIMAL_DROPPED_AT),
            _MIN_THINKING_LEVEL,
        )
        return _MIN_THINKING_LEVEL
    return level


def split_model_choice(choice: str) -> tuple[str, str | None]:
    """An admin model choice → ``(model_id, thinking_level)``.

    A choice is a model id, optionally with ``@level`` when the level is part
    of what was chosen: ``"gemini-4-argon@high"``. Argon at the level it would
    otherwise inherit thought 0 tokens on every PRO-tier job in the replay
    (2026-10-14), so "Argon" and "Argon at high" are different choices. The id
    goes to Vertex; the level goes in the config.

    A plain id comes back with ``None``. So does an unknown level, logged rather
    than sent, because google-genai does not reject one and Vertex 400s it.
    """
    model_id, sep, level = (choice or "").partition("@")
    model_id = model_id.strip()
    level = level.strip().lower()
    if not sep or not level:
        return model_id, None
    if level not in _VALID_THINKING_LEVELS:
        logger.warning(
            "Model choice %r names thinking level %r, not one of %s; ignoring it.",
            choice,
            level,
            _VALID_THINKING_LEVELS,
        )
        return model_id, None
    return model_id, level


def thinking_config_for(model_id: str | None) -> genai_types.ThinkingConfig | None:
    """Return the ThinkingConfig that curbs latency-inflating thinking for
    ``model_id``, or ``None`` when the model's default should be left untouched.

    Safe for every id in ``CHAT_MODEL_CATALOG`` — the chat model is admin-
    selectable at runtime, so a wrong knob would 400 the whole turn.
    """
    mid = (model_id or settings.VERTEX_LLM_MODEL_FLASH).lower()

    # Gemini 3.x and later speak thinking_level, not thinking_budget.
    if uses_thinking_level(mid):
        level = _resolve_thinking_level(mid)
        if level is None:
            return None
        return genai_types.ThinkingConfig(thinking_level=level)
    # 2.5 Pro can't disable thinking — don't fight it.
    if "pro" in mid:
        return None
    # 2.5 Flash / Flash-Lite (incl. the flash-latest default).
    budget = settings.CHAT_LLM_THINKING_BUDGET
    if budget < 0:
        return None  # negative = keep the model's dynamic default
    return genai_types.ThinkingConfig(thinking_budget=budget)


def generation_config_for(
    model_id: str | None,
    base: genai_types.GenerateContentConfig | None = None,
) -> genai_types.GenerateContentConfig | None:
    """Build the ``config=`` for a one-shot ``generate_content`` call on
    ``model_id``, carrying the right thinking knob for its family.

    Returns ``None`` when there is nothing to say (no ``base``, and the model's
    default thinking should be left alone) so callers can pass the result
    straight through as ``config=`` without a branch.

    ``base`` is copied, never mutated, and an explicit ``thinking_config`` on it
    always wins — a caller that has deliberately tuned a call keeps its setting.
    """
    thinking = thinking_config_for(model_id)
    if base is None:
        if thinking is None:
            return None
        return genai_types.GenerateContentConfig(thinking_config=thinking)
    if base.thinking_config is not None or thinking is None:
        return base
    return base.model_copy(update={"thinking_config": thinking})

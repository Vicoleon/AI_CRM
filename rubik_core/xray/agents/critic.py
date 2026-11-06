"""Critic — grounding/completeness gate with one-retry semantics.

Mirrors the priorities critic's fail-open posture: on an LLM failure it does not
fake a pass — it falls back to the DETERMINISTIC check (any recent_news item
without a URL forces a revision) and marks ``critic_failed`` so the score is not
treated as a health metric.
"""

from __future__ import annotations

import logging
from typing import Any

from rubik_core.ai_providers.registry import ModelRoute
from rubik_core.xray import config, prompts
from rubik_core.xray.agents.base import llm_json

logger = logging.getLogger(__name__)


def review(
    sections: dict[str, Any], *, route: ModelRoute | None = None
) -> dict[str, Any]:
    """Score the brief; return {score, needs_revision, issues, suggestions,
    uncited_news, critic_failed}."""
    uncited = [
        (n.get("headline") or "?")
        for n in (sections.get("recent_news") or [])
        if not (isinstance(n, dict) and n.get("url"))
    ]
    det_needs = bool(uncited)

    try:
        system, prompt = prompts.build_critic_prompt(sections)
        data = llm_json(system, prompt, model_id=config.model_critic(), route=route)
        score = float(data.get("score", config.DEEP_CRITIC_THRESHOLD))
        needs = (
            bool(data.get("needs_revision"))
            or det_needs
            or score < config.DEEP_CRITIC_THRESHOLD
        )
        issues = list(data.get("issues") or [])
        if uncited:
            issues.append(f"uncited news items: {', '.join(uncited)}")
        return {
            "score": score,
            "needs_revision": needs,
            "issues": issues,
            "suggestions": list(data.get("suggestions") or []),
            "uncited_news": uncited,
            "critic_failed": False,
        }
    except Exception:  # noqa: BLE001 - fail-open to deterministic check
        logger.warning("xray critic AI analysis unavailable")
        return {
            "score": None,
            "needs_revision": det_needs,
            "issues": [f"uncited news items: {', '.join(uncited)}"] if uncited else [],
            "suggestions": [],
            "uncited_news": uncited,
            "critic_failed": True,
        }

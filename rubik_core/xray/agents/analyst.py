"""CS-expert analyst — merges channel digests into the sections contract.

Replaces the priorities decision-maker. One Pro call over the digests + churn
factor table + whitespace + timeline; returns the ``sections`` dict the UI
renders. ``normalize_sections`` guarantees every key exists with a safe shape so
the frontend never has to defend against a missing field.
"""

from __future__ import annotations

import logging
from typing import Any

from rubik_core.ai_providers.registry import ModelRoute
from rubik_core.xray import config, prompts
from rubik_core.xray.agents.base import llm_json
from rubik_core.xray.schemas import ChannelDigest, ChurnResult, CompanyBundle

logger = logging.getLogger(__name__)

_SECTION_KEYS = (
    "executive_summary",
    "relationship_timeline_highlights",
    "relationship_health",
    "commercial_health",
    "opportunities",
    "churn_rationale_md",
    "recent_news",
    "source_evidence",
)


def synthesize(
    bundle: CompanyBundle,
    churn: ChurnResult,
    whitespace: list[dict[str, Any]],
    digests: list[ChannelDigest],
    timeline_drafts: list,
    *,
    prev_sections: dict[str, Any] | None = None,
    focus: str = "",
    personalization: str = "",
    wgll: str = "",
    previous_attempt: dict[str, Any] | None = None,
    critique: dict[str, Any] | None = None,
    route: ModelRoute | None = None,
) -> dict[str, Any]:
    """One analyst LLM call → normalized sections dict. Raises on LLM failure."""
    system, prompt = prompts.build_analyst_prompt(
        bundle,
        churn,
        whitespace,
        digests,
        timeline_drafts,
        prev_sections=prev_sections,
        focus=focus,
        personalization=personalization,
        wgll=wgll,
        previous_attempt=previous_attempt,
        critique=critique,
    )
    data = llm_json(
        system,
        prompt,
        model_id=config.model_analyst(),
        temperature=0.3,
        route=route,
    )
    return normalize_sections(data, digests)


def normalize_sections(
    data: Any, digests: list[ChannelDigest] | None = None
) -> dict[str, Any]:
    """Coerce raw analyst JSON into the fixed sections shape.

    Falls back to the web digest's news if the analyst omitted ``recent_news``,
    so a good web pass is never lost to a formatting slip.
    """
    out: dict[str, Any] = {k: None for k in _SECTION_KEYS}
    if isinstance(data, dict):
        out.update({k: data.get(k) for k in _SECTION_KEYS if k in data})

    out["executive_summary"] = _as_str(out.get("executive_summary"))
    out["commercial_health"] = _as_str(out.get("commercial_health"))
    out["churn_rationale_md"] = _as_str(out.get("churn_rationale_md"))
    out["relationship_timeline_highlights"] = _as_str_list(
        out.get("relationship_timeline_highlights")
    )
    out["source_evidence"] = _as_str_list(out.get("source_evidence"))
    out["relationship_health"] = _as_health(out.get("relationship_health"))
    out["opportunities"] = _as_opportunities(out.get("opportunities"))
    out["recent_news"] = _as_news(out.get("recent_news"))

    if not out["recent_news"] and digests:
        for d in digests:
            if d.channel == "web" and d.attrs.get("news"):
                out["recent_news"] = _as_news(d.attrs["news"])
                break
    return out


# ── Coercion helpers ─────────────────────────────────────────────────────────
def _as_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return "\n".join(f"- {x}" for x in v)
    return str(v)


def _as_str_list(v: Any) -> list[str]:
    if not v:
        return []
    if isinstance(v, str):
        return [v]
    return [str(x) for x in v if x]


def _as_health(v: Any) -> dict[str, Any]:
    keys = ("narrative_md", "cadence", "breadth", "responsiveness", "sentiment_trend")
    if isinstance(v, dict):
        return {k: v.get(k) for k in keys}
    return {"narrative_md": _as_str(v), **{k: None for k in keys[1:]}}


def _as_opportunities(v: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for o in v or []:
        if not isinstance(o, dict):
            continue
        out.append(
            {
                "title": str(o.get("title") or ""),
                "why_now": str(o.get("why_now") or ""),
                "product": str(o.get("product") or ""),
                "play": str(o.get("play") or ""),
                "entity": str(o.get("entity") or ""),
            }
        )
    return out


def _as_news(v: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for n in v or []:
        if not isinstance(n, dict):
            continue
        url = str(n.get("url") or "").strip()
        if not url:
            continue  # cite-or-drop (belt-and-suspenders alongside the critic)
        out.append(
            {
                "date": str(n.get("date") or ""),
                "headline": str(n.get("headline") or ""),
                "why_it_matters": str(n.get("why_it_matters") or ""),
                "url": url,
            }
        )
    return out

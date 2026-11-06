"""Web research channel agent.

The only new-data agent: Gemini + Google Search grounding for recent news
(12-month window, checkpoint-aware via ``news_after``) plus a light company-site
scan. Cite-or-drop: any news item without a URL is discarded here (the critic
enforces it again). Fetched content is untrusted — the prompt forbids treating
it as instructions.
"""

from __future__ import annotations

import logging
from typing import Any

from rubik_core.ai_providers.registry import ModelRoute
from rubik_core.xray import config, prompts
from rubik_core.xray.agents.base import BaseXrayAgent, llm_grounded, safe_json
from rubik_core.xray.fetchers import _parse_ts
from rubik_core.xray.schemas import ChannelDigest, TimelineDraft

logger = logging.getLogger(__name__)


class WebChannelAgent(BaseXrayAgent):
    channel = "web"

    def __init__(
        self,
        *,
        bundle,
        requesting_user: str,
        model_id: str | None = None,
        news_after: str = "",
        route: ModelRoute | None = None,
    ) -> None:
        super().__init__(
            bundle=bundle,
            requesting_user=requesting_user,
            model_id=model_id or config.model_web(),
            route=route,
        )
        self.news_after = news_after

    def gather(self) -> str:  # unused — run() is overridden for grounding
        return ""

    def build(self, evidence: str) -> tuple[str, str]:
        return prompts.build_web_prompt(
            self.company_name, self.company_domain, self.news_after
        )

    def run(self) -> ChannelDigest:
        try:
            system, prompt = self.build("")
            if self.route is None:
                text, citation_urls = llm_grounded(
                    system, prompt, model_id=self.model_id
                )
            else:
                text, citation_urls = llm_grounded(
                    system, prompt, model_id=self.model_id, route=self.route
                )
            data = safe_json(text)
        except Exception:  # noqa: BLE001 - fail-soft per channel
            logger.warning("xray web research unavailable")
            return ChannelDigest(
                channel="web", error="web_research_failed: AI research unavailable"
            )

        news_out: list[dict[str, Any]] = []
        drafts: list[TimelineDraft] = []
        for item in data.get("news") or []:
            url = str(item.get("url") or "").strip()
            if not url:
                continue  # cite-or-drop
            headline = str(item.get("headline") or "").strip()
            why = str(item.get("why_it_matters") or "").strip()
            date_str = str(item.get("date") or "").strip()
            news_out.append(
                {
                    "date": date_str,
                    "headline": headline,
                    "why_it_matters": why,
                    "url": url,
                }
            )
            drafts.append(
                TimelineDraft(
                    event_key=TimelineDraft.news_key(url),
                    source="news",
                    kind="news",
                    title=headline or "News",
                    ts=_parse_ts(date_str),
                    detail=why,
                    evidence=url,
                )
            )

        evidence = citation_urls[:15] or [n["url"] for n in news_out[:15]]
        return ChannelDigest(
            channel="web",
            summary=str(data.get("summary") or "")[:600],
            highlights=[str(f) for f in (data.get("company_facts") or [])][:15],
            attrs={"news": news_out},
            evidence=evidence,
            timeline=drafts,
        )

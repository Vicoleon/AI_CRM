"""Public-web evidence via Gemini + Google Search grounding — the default source.

Google indexes public LinkedIn profiles and public LinkedIn posts, plus the rest
of a professional's public footprint (conference agendas, association bios,
podcast pages, trade-press quotes, company newsrooms). Grounded search reads all
of it through one already-deployed, already-paid-for path and returns citations,
so the reader can click through to whatever the card claims.

This source deliberately does NOT log into linkedin.com and parse HTML. That is
against LinkedIn's terms, breaks the moment they rotate markup, and gets the
account and egress IP banned — which for a single-tenant app means it breaks for
everyone at once. See ``docs/uc47_attendee_intelligence.md``.
"""

from __future__ import annotations

import logging

from rubik_core.ai_providers.registry import resolve_model_route
from rubik_core.ai_providers.runtime import generate_grounded_text_on_route
from rubik_core.models import llm_grounded
from rubik_core.people import config, persona
from rubik_core.people.schemas import RawEvidence
from rubik_core.people.sources.base import PersonQuery

logger = logging.getLogger(__name__)


class WebSearchSource:
    """Grounded public-web research for one person."""

    name = "web_search"

    def fetch(self, query: PersonQuery) -> RawEvidence:
        system, prompt = persona.build_research_prompt(query)
        route = resolve_model_route(
            "people_web_search", "grounding", default_model=config.MODEL_RESEARCH
        )
        if route.provider == "vertex_managed":
            text, citations = llm_grounded(
                system,
                prompt,
                model_id=route.model,
                thinking_level=config.RESEARCH_THINKING_LEVEL,
                # Our own budget, not the client's 120 s: past that the SDK cuts
                # the call off and retries it from scratch.
                timeout_s=config.PER_ATTENDEE_BUDGET_S,
            )
        else:
            answer = generate_grounded_text_on_route(
                route,
                prompt,
                system_instruction=system,
                temperature=0.1,
                timeout_s=config.PER_ATTENDEE_BUDGET_S,
            )
            text, citations = answer.text, list(answer.citation_urls)
        if not (text or "").strip():
            logger.info("attendee intel: empty grounded response for %s", query.email)
        return RawEvidence(text=text or "", citations=citations, provider=self.name)

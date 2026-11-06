"""Docs / RAG channel agent.

Retrieves proposals, reports, and review findings tied to the customer from
the enterprise knowledge base via ``AgentTools.search_knowledge_base``.
"""

from __future__ import annotations

import asyncio
import logging

from rubik_core.xray import prompts
from rubik_core.xray.agents.base import BaseXrayAgent

logger = logging.getLogger(__name__)


class DocsChannelAgent(BaseXrayAgent):
    channel = "docs"

    def gather(self) -> str:
        from rubik_core.adk_agent.tools import AgentTools  # lazy import

        tools = AgentTools(user_email=self.requesting_user)
        res = asyncio.run(
            tools.search_knowledge_base(
                f"{self.company_name} proposal report findings scope",
                top_k=5,
            )
        )
        if isinstance(res, dict):
            return res.get("report", "") or ""
        return str(res or "")

    def build(self, evidence: str) -> tuple[str, str]:
        return prompts.build_channel_prompt("docs", evidence, self.company_name)

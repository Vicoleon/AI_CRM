"""Meetings channel agent.

Two sources, deliberately combined:

* **Which meetings happened** comes from the relationship graph — company → its
  people → the meetings they attended. That is an identity join, so it finds a
  transcript even when the transcript never says the company's name.
* **What was said in them** still comes from the meetings RAG corpus via
  ``AgentTools.search_past_meetings``, which re-checks the meeting_participants
  ACL (that boundary is preserved).

The semantic search alone used to be the whole channel, and it answered
``"meetings with {company}"`` by hoping those words appeared in a transcript —
so a quarterly review that only ever named the people in the room was invisible
to it. The graph list fixes the "did we meet, when, how often" half without
touching the retrieval half.

Emits a sentiment trajectory in ``attrs.sentiment_trend`` (-1..1) that the churn
scorer consumes as its Deep-only meeting-sentiment factor.
"""

from __future__ import annotations

import asyncio
import logging

from rubik_core.xray import prompts
from rubik_core.xray.agents.base import BaseXrayAgent

logger = logging.getLogger(__name__)


class MeetingsChannelAgent(BaseXrayAgent):
    channel = "meetings"

    def _graph_history(self) -> str:
        """Attendance-record meeting list for this company, or "" if none.

        Fail-soft: the graph is an enhancement to this channel, never a
        dependency. Any problem here leaves the agent exactly as it was before
        the graph existed.
        """
        try:
            from rubik_core.config import settings

            if not getattr(settings, "CUBIE_GRAPH_READS_ENABLED", False):
                return ""
            from rubik_core.graph.queries import (
                company_meeting_history,
                format_meeting_history_block,
            )

            hits = company_meeting_history(
                self.bundle.company_domain,
                self.requesting_user,
                contact_emails=[
                    c.get("email")
                    for c in (self.bundle.contacts or [])
                    if c.get("email")
                ],
            )
            return format_meeting_history_block(hits, self.company_name)
        except Exception:  # noqa: BLE001 — never sink the channel over this
            logger.warning("xray meetings: graph history unavailable", exc_info=True)
            return ""

    def gather(self) -> str:
        from rubik_core.adk_agent.tools import AgentTools  # lazy import

        tools = AgentTools(user_email=self.requesting_user)
        # We run inside an orchestrator worker thread with no event loop, so
        # drive the async tool with asyncio.run.
        res = asyncio.run(
            tools.search_past_meetings(
                f"meetings with {self.company_name}", max_results=5
            )
        )
        excerpts = (
            res.get("report", "") or "" if isinstance(res, dict) else str(res or "")
        )

        history = self._graph_history()
        if not history:
            return excerpts
        if not excerpts:
            return history
        return f"{history}\n\n{excerpts}"

    def build(self, evidence: str) -> tuple[str, str]:
        return prompts.build_meetings_prompt(evidence, self.company_name)

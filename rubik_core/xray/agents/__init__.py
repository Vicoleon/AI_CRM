"""Deep Research channel agents + analyst + critic for UC-45.

Each channel agent is a pure extractor over one source; the orchestrator fans
them out in parallel with per-agent wall-clock budgets, then the analyst merges
their digests into the sections contract and the critic gates one revision —
mirroring rubik_core/priorities/orchestrator.py mechanics.
"""

from rubik_core.xray.agents.base import BaseXrayAgent, llm_grounded, llm_json
from rubik_core.xray.agents.docs_agent import DocsChannelAgent
from rubik_core.xray.agents.gmail_agent import GmailChannelAgent
from rubik_core.xray.agents.hubspot_agent import HubSpotChannelAgent
from rubik_core.xray.agents.meetings_agent import MeetingsChannelAgent
from rubik_core.xray.agents.web_agent import WebChannelAgent

CHANNEL_AGENTS = {
    "hubspot": HubSpotChannelAgent,
    "gmail": GmailChannelAgent,
    "meetings": MeetingsChannelAgent,
    "docs": DocsChannelAgent,
    "web": WebChannelAgent,
}

__all__ = [
    "BaseXrayAgent",
    "llm_json",
    "llm_grounded",
    "CHANNEL_AGENTS",
    "HubSpotChannelAgent",
    "GmailChannelAgent",
    "MeetingsChannelAgent",
    "DocsChannelAgent",
    "WebChannelAgent",
]

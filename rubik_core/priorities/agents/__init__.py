"""Agents for the priorities pipeline.

Channel agents (one per data source) feed the summarizer, which feeds
the decision-maker, which the critic guards with a bounded revision
loop.
"""

from rubik_core.priorities.agents.base import (
    BaseChannelAgent,
    LLMJsonClient,
    safe_json,
)
from rubik_core.priorities.agents.calendar_agent import CalendarChannelAgent
from rubik_core.priorities.agents.critic import CriticAgent
from rubik_core.priorities.agents.decision_maker import DecisionMakerAgent
from rubik_core.priorities.agents.drive_agent import DriveChannelAgent
from rubik_core.priorities.agents.eos_agent import EosChannelAgent
from rubik_core.priorities.agents.gmail_agent import GmailChannelAgent
from rubik_core.priorities.agents.hubspot_agent import HubSpotChannelAgent
from rubik_core.priorities.agents.inherited_agent import InheritedChannelAgent
from rubik_core.priorities.agents.meet_agent import MeetChannelAgent
from rubik_core.priorities.agents.promises_agent import PromisesChannelAgent
from rubik_core.priorities.agents.slack_agent import SlackChannelAgent
from rubik_core.priorities.agents.summarizer import SummarizerAgent

__all__ = [
    "BaseChannelAgent",
    "LLMJsonClient",
    "safe_json",
    "GmailChannelAgent",
    "CalendarChannelAgent",
    "DriveChannelAgent",
    "MeetChannelAgent",
    "HubSpotChannelAgent",
    "EosChannelAgent",
    "InheritedChannelAgent",
    "PromisesChannelAgent",
    "SlackChannelAgent",
    "SummarizerAgent",
    "DecisionMakerAgent",
    "CriticAgent",
]

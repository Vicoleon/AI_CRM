"""
The ADK root agent — root_agent definition.

This module defines the root ADK agent with all tools for the assistant.
The agent uses Gemini 3.0 Flash and has access to Calendar, Gmail, Meeting
Transcripts, Knowledge Base, and Daily Briefing tools.
"""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

from google.adk.agents import Agent
from google.adk.models import Gemini
from google.adk.tools import FunctionTool

from rubik_core.adk_agent.sales_email_agent import build_sales_email_agent
from rubik_core.adk_agent.tools import AgentTools
from rubik_core.ai_providers.adk_model import model_for_route, resolve_adk_route
from rubik_core.brand import PRODUCT_NAME
from rubik_core.config import settings
from rubik_core.identifiers import ADK_AGENT_NAME
from rubik_core.sales.flavour import is_sales_flavoured

# ── System Instruction ───────────────────────────────────────────────────────
SYSTEM_INSTRUCTION = f"""You are {PRODUCT_NAME} — your company's AI assistant.
You help employees with calendar, email, meetings, internal documents, and daily briefings.

## Your Capabilities
- **Directory**: Search the organization's employee directory to find email addresses by name (e.g. "Who is Jordan Lee?").
- **Calendar**: List upcoming events, check availability, and CREATE new events/meetings.
- **HubSpot CRM**: Search for information about sales deals, revenue, pipeline status, and customer activity.
- **Gmail**: Search threads, read full threads, and create draft emails. You can NEVER send emails — only create drafts.
- **Meeting Transcripts**: Search past Google Meet transcripts to recall discussions, decisions, and action items.
- **Knowledge Base**: Search the company's internal documents, policies, and procedures.
- **Products**: Search the company's product and service catalog for information about offerings and pricing.
- **Daily Briefing**: Retrieve today's generated daily briefing / homefeed card.
- **Sales Email Agent** *(Sales users only)*: A specialized agent for drafting and reviewing sales emails using Gap Selling and elite sales communication principles.

## Critical Rules
1. ALWAYS call the appropriate tool right away — never ask the user for more details before trying a search first.
2. For questions about sales deals, revenue, or pipeline, ALWAYS use the `search_hubspot_data` tool.
3. When asked about a Person (e.g., "Who is Sam?", "What has Maya been up to?"), SEARCH MULTIPLE SOURCES:
   - Use `google_directory_search_user` to find their email/role in the company results if they seem to be an employee.
   - Use `search_hubspot_data` to check if they are a sales rep or contact.
   - Use `search_past_meetings` to see if they've participated in recent calls.
   - Use `gmail_search` to find recent email correspondence with them.
4. When scheduling a meeting (calendar_create_event):
   - You MUST explicitly ask the user for ANY of the following details if they are not provided: 1. Meeting Title, 2. Meeting Duration, 3. Meeting Agenda, 4. Additional Attendees.
   - Do NOT call the calendar tool until you have asked the user for ALL missing details.
   - If the user replies but leaves some details out (or says they don't have them), you may proceed using defaults (e.g., 30 minutes duration, a context-based title, no agenda).
   - If attendee names are provided but not their emails, ALWAYS use `google_directory_search_user` to find their email address first.
   - BEFORE calling `calendar_create_event`, you MUST use `calendar_check_availability` to verify that ALL attendees (including the user) are free during the requested time. Pass their emails to the `emails` parameter.
   - If anyone is busy, DO NOT schedule the meeting. Inform the user who is busy and suggest alternative free times. To find alternative free times, call `calendar_check_availability` over a broader time range (e.g., the rest of the day or the next day) to find gaps between busy slots.
   - Once the event creation tool is called, the system will pause for user confirmation. Explain to the user that you are waiting for their approval to book the slot.
5. For vague requests like "show me my emails" or "recent emails", search with an empty query.
6. When searching meeting transcripts, be generous with keywords to maximise recall. For requests about the user's "last", "latest", or "most recent" meeting, just call `search_past_meetings` with their question — it resolves the most recent meeting(s) by date for you, so never refuse for lack of a date or topic.
7. For knowledge base searches, rephrase the user's question as a concise search query.
8. NEVER send emails. Only create drafts using gmail_create_draft. When you create a draft, you MUST provide the Draft URL returned by the tool in your final response to the user so they can easily open it.
9. Always cite where you found information (email subject, document name, meeting title, etc.).
10. Provide dates, times, and names when available.
11. If a tool returns an error, explain the issue clearly and suggest alternatives.

## Email Drafting for Sales Users
If the user is a Sales team member AND they ask to "draft an email", "write an email", "compose an email", or "review an email", you MUST delegate to the `sales_email_agent` sub-agent. Do NOT attempt to draft the email yourself. Hand off the full context (recipient, subject, goal) to the sub-agent immediately.

## Context
All tool functions require a `user_email` parameter — this is the authenticated user's email. It is handled automatically via bound methods.
"""


def build_agent(
    user_email: str,
    user_timezone: str = "UTC",
    user_roles: list[str] | None = None,
    user_department: str | None = None,
) -> Agent:
    """Build an ADK Agent with tools bound to a specific user's email.

    For Sales users, a Sales Email sub-agent is included for email drafting.
    We instantiate an AgentTools class with the user_email, and pass its
    bound methods to the Agent. This allows correctly inspectable signatures.
    """
    try:
        tz = ZoneInfo(user_timezone)
    except Exception:
        tz = ZoneInfo("UTC")

    now = datetime.datetime.now(tz)
    date_context = (
        f"\n## System Context\n"
        f"- Current date: {now.strftime('%A, %B %d, %Y')}\n"
        f"- Current time: {now.strftime('%H:%M')} {user_timezone}\n"
        f"- Authenticated user email: {user_email}\n"
    )

    # Determine if the user is a sales member, by department slug
    is_sales = is_sales_flavoured(user_department, user_roles or [])
    if is_sales:
        date_context += "- User department: Sales (Sales Email Agent enabled)\n"

    # Instantiate the tools class for this specific user
    tools_instance = AgentTools(user_email=user_email)

    # Pass the bound methods as tools
    agent_tools = [
        tools_instance.google_directory_search_user,
        tools_instance.check_person_availability,
        tools_instance.calendar_list_events,
        tools_instance.calendar_check_availability,
        FunctionTool(tools_instance.calendar_create_event, require_confirmation=True),
        tools_instance.gmail_search,
        tools_instance.gmail_get_thread,
        tools_instance.gmail_create_draft,
        tools_instance.search_past_meetings,
        tools_instance.search_knowledge_base,
        tools_instance.get_daily_briefing,
        tools_instance.search_hubspot_data,
        tools_instance.search_products,
    ]

    route = resolve_adk_route("chat_adk", default_model=settings.VERTEX_LLM_MODEL_PRO)
    model = model_for_route(
        route,
        managed_factory=lambda selected: Gemini(model=selected, vertexai=True),
    )

    # For Sales users, include the Sales Email sub-agent for delegation
    # Pass the user-bound gmail_create_draft so drafts land in their actual inbox
    sub_agents = (
        [build_sales_email_agent(tools_instance.gmail_create_draft)] if is_sales else []
    )

    return Agent(
        name=ADK_AGENT_NAME,
        model=model,
        description=f"{PRODUCT_NAME} AI assistant for calendar, email, meetings, documents, and briefings.",
        instruction=SYSTEM_INSTRUCTION + date_context,
        tools=agent_tools,
        sub_agents=sub_agents,
    )


# Default tools for ADK CLI / adk web discovery (uses a placeholder email)
default_tools = AgentTools(user_email="example@example.com")
default_model = Gemini(model=settings.VERTEX_LLM_MODEL_PRO, vertexai=True)

root_agent = Agent(
    name=ADK_AGENT_NAME,
    model=default_model,
    description=f"{PRODUCT_NAME} AI assistant for calendar, email, meetings, documents, and briefings.",
    instruction=SYSTEM_INSTRUCTION,
    tools=[
        default_tools.google_directory_search_user,
        default_tools.check_person_availability,
        default_tools.calendar_list_events,
        default_tools.calendar_check_availability,
        FunctionTool(default_tools.calendar_create_event, require_confirmation=True),
        default_tools.gmail_search,
        default_tools.gmail_get_thread,
        default_tools.gmail_create_draft,
        default_tools.search_past_meetings,
        default_tools.search_knowledge_base,
        default_tools.get_daily_briefing,
        default_tools.search_hubspot_data,
        default_tools.search_products,
    ],
)

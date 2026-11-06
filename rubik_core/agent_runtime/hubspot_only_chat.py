"""Ask Cubie while an admin impersonates a person — HubSpot only, nothing saved.

The normal chat coordinator carries ~40 tools (Gmail, Calendar, Drive, call
transcripts, the knowledge base, scheduling …) and persists every
turn to the person's own chat history. Neither is acceptable while an admin is
looking through someone else's eyes, so this is a separate, deliberately tiny
agent rather than a flag on ``build_coordinator``: a tool added to the shared
coordinator next month cannot leak in here.

* **Two tools**: ``search_hubspot_data`` (pipeline, deals, the daily-rhythm
  worklists — "my deals" resolves to the impersonated person) and
  ``find_hubspot_contact``. No sub-agents, so no email-drafting hand-off.
* **No storage**: a fresh in-memory ADK session per turn, discarded on return.
  Context for follow-ups comes from the ``history`` the browser sends.
* **No mailbox**: the HubSpot retriever skips the person's Gmail/Calendar while
  impersonating, and the Workspace credential lock refuses it regardless.

Cost is attributed to its own AI Cost category, ``chat_impersonation``.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from google.adk.agents import Agent
from google.adk.agents.run_config import RunConfig
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types

from rubik_core.adk_agent.fallback_llm import FallbackGemini
from rubik_core.adk_agent.model_tuning import low_latency_planner
from rubik_core.adk_agent.tools import AgentTools
from rubik_core.ai_providers.adk_model import model_for_route, resolve_adk_route
from rubik_core.ai_providers.registry import ModelRoute
from rubik_core.brand import PRODUCT_NAME
from rubik_core.config import settings
from rubik_core.cost.usage import attribute_to
from rubik_core.identifiers import IMPERSONATION_APP_NAME

logger = logging.getLogger(__name__)


INSTRUCTION = f"""You are {PRODUCT_NAME}, the company's assistant, answering questions about a
sales rep's HubSpot CRM data. An administrator is viewing {PRODUCT_NAME} as this rep,
so "my" and "me" in a question mean the rep named below.

You have exactly two tools:
- search_hubspot_data — pipeline, deals, stages, close dates, last contact, KPIs,
  and the daily sales-rhythm worklists (closing this month, waiting on the
  decision-maker, slipping close dates, single-threaded deals, re-engagement,
  missing fields, overdue tasks).
- find_hubspot_contact — one person's contact record.

Rules:
- Answer only from what these tools return. If they return nothing relevant, say
  so plainly; never guess or invent deals, amounts or dates.
- You cannot read email, calendar, files, call transcripts, meeting notes or the
  knowledge base, and you cannot draft or send anything. If asked, say that this
  view is limited to HubSpot.
- Every count you give states what it is out of (for example "4 of 27 open
  deals in a proposal or contract stage").
- Be concise. Use short bullet lists for deals, with the deal name first.
"""


@dataclass
class HubSpotOnlyResult:
    answer: str
    model_id: str
    provider: str = "vertex_managed"
    billing_owner: str = "cubie"
    tool_calls: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    thinking_tokens: int = 0
    total_tokens: int = 0


def build_hubspot_only_agent(
    *,
    user_email: str,
    user_name: str,
    timezone: str = "UTC",
    model_route: ModelRoute | None = None,
) -> tuple[Agent, str]:
    """The agent and the model id it runs on."""
    try:
        tz = ZoneInfo(timezone)
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("UTC")
    now = datetime.datetime.now(tz)
    route = model_route or resolve_adk_route(
        "chat_impersonation", default_model=settings.VERTEX_LLM_MODEL_FLASH
    )
    model_id = route.model
    tools = AgentTools(user_email=user_email)

    def _managed_model(selected_model: str):
        return FallbackGemini(
            model=selected_model,
            vertexai=True,
            retry_options=genai_types.HttpRetryOptions(
                attempts=settings.CHAT_LLM_RETRY_ATTEMPTS,
                http_status_codes=settings.CHAT_LLM_RETRY_STATUS_CODES,
            ),
            fallback_model=settings.CHAT_LLM_FALLBACK_MODEL or None,
        )

    agent = Agent(
        name="cubie_hubspot_only",
        model=model_for_route(route, managed_factory=_managed_model),
        description=f"HubSpot-only {PRODUCT_NAME} for admin impersonation.",
        static_instruction=INSTRUCTION,
        instruction=(
            f"\n## Context\n- The rep: {user_name} ({user_email})\n"
            f"- Today: {now.strftime('%A, %B %d, %Y')} ({timezone})\n"
        ),
        tools=[tools.search_hubspot_data, tools.find_hubspot_contact],
        sub_agents=[],
        planner=low_latency_planner(model_id)
        if route.provider == "vertex_managed"
        else None,
        generate_content_config=genai_types.GenerateContentConfig(
            temperature=settings.CHAT_LLM_TEMPERATURE,
            http_options=genai_types.HttpOptions(
                timeout=settings.CHAT_LLM_CALL_TIMEOUT_MS
            ),
        ),
    )
    return agent, model_id


def _with_history(query: str, history: list[tuple[str, str]]) -> str:
    text = ""
    if history:
        lines = [
            f"{'User' if role == 'user' else 'Assistant'}: {content}"
            for role, content in history[-6:]
        ]
        text += "[Previous conversation context]\n" + "\n".join(lines) + "\n\n"
    return text + f"[Current user query]\n{query}"


@attribute_to("chat_impersonation")
async def run_hubspot_only_turn(
    *,
    user_email: str,
    user_name: str,
    query: str,
    history: list[tuple[str, str]],
    timezone: str = "UTC",
    request_id: str = "",
) -> HubSpotOnlyResult:
    """One turn. Nothing outlives the call: the session service is local."""
    route = resolve_adk_route(
        "chat_impersonation", default_model=settings.VERTEX_LLM_MODEL_FLASH
    )
    agent, model_id = build_hubspot_only_agent(
        user_email=user_email,
        user_name=user_name,
        timezone=timezone,
        model_route=route,
    )
    sessions = InMemorySessionService()
    runner = Runner(
        agent=agent, app_name=IMPERSONATION_APP_NAME, session_service=sessions
    )
    session = await sessions.create_session(
        app_name=IMPERSONATION_APP_NAME, user_id=user_email, session_id=uuid.uuid4().hex
    )
    message = genai_types.Content(
        role="user",
        parts=[genai_types.Part.from_text(text=_with_history(query, history))],
    )
    result = HubSpotOnlyResult(
        answer="",
        model_id=model_id,
        provider=route.provider,
        billing_owner=route.billing_owner,
    )
    parts: list[str] = []
    async for event in runner.run_async(
        user_id=user_email,
        session_id=session.id,
        new_message=message,
        run_config=RunConfig(max_llm_calls=settings.CHAT_MAX_LLM_CALLS),
    ):
        usage = getattr(event, "usage_metadata", None)
        if usage is not None:
            result.prompt_tokens += getattr(usage, "prompt_token_count", 0) or 0
            result.completion_tokens += getattr(usage, "candidates_token_count", 0) or 0
            result.thinking_tokens += getattr(usage, "thoughts_token_count", 0) or 0
            result.total_tokens += getattr(usage, "total_token_count", 0) or 0
        for fc in event.get_function_calls():
            result.tool_calls.append(getattr(fc, "name", "?"))
        if event.is_final_response() and event.content and event.content.parts:
            text = "\n".join(p.text for p in event.content.parts if p.text)
            if text:
                parts.append(text)
    result.answer = "\n\n".join(parts).strip()
    logger.info(
        "[%s] impersonation chat for %s: tools=%s tokens=%s",
        request_id,
        user_email,
        result.tool_calls,
        result.total_tokens,
    )
    return result

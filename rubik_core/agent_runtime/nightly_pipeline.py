"""
Nightly Intelligence Pipeline using ADK's SequentialAgent.

Autonomous pipeline that collects signals, normalizes, enriches, and
publishes HomeCard artifacts for each user. Designed to be triggered
by Cloud Scheduler or manually via the /jobs/overnight-briefing endpoint.
"""

from __future__ import annotations

from google.adk.agents import LlmAgent, SequentialAgent
from google.adk.models import Gemini

from rubik_core.adk_agent.tools import AgentTools
from rubik_core.agent_runtime.nightly_prompts import (
    ENRICHER_INSTRUCTION,
    NORMALIZER_INSTRUCTION,
    SIGNAL_COLLECTOR_INSTRUCTION,
    VALIDATOR_INSTRUCTION,
)
from rubik_core.agent_runtime.nightly_tools import save_home_card
from rubik_core.ai_providers.adk_model import model_for_route, resolve_adk_route
from rubik_core.ai_providers.registry import ModelRoute
from rubik_core.config import settings


def build_nightly_pipeline(
    user_email: str, *, model_route: ModelRoute | None = None
) -> SequentialAgent:
    """Build the nightly intelligence pipeline for a specific user.

    The pipeline has four sequential stages:
      1. Signal Collector — gathers raw data from Gmail, Calendar, CRM
      2. Normalizer — deduplicates, classifies, and scores priority
      3. Enricher — adds KB and product context via RAG
      4. Validator — quality-checks and persists HomeCards to Cloud SQL

    Args:
        user_email: The email of the target user.

    Returns:
        A SequentialAgent representing the full pipeline.
    """
    # One workspace route is pinned for all four stages of this job.
    route = model_route or resolve_adk_route(
        "nightly_pipeline", default_model=settings.VERTEX_LLM_MODEL_FLASH
    )
    model = model_for_route(
        route,
        managed_factory=lambda selected: Gemini(model=selected, vertexai=True),
    )

    # Shared tools instance for this user
    tools_instance = AgentTools(user_email=user_email)

    # Load MCP toolsets dynamically (for CRM signals)
    from rubik_core.mcp_loader import load_mcp_toolsets

    dynamic_toolsets = load_mcp_toolsets()

    # ── Stage 1: Signal Collector ─────────────────────────────────────────────
    signal_collector = LlmAgent(
        name="signal_collector",
        model=model,
        instruction=SIGNAL_COLLECTOR_INSTRUCTION,
        tools=[
            tools_instance.gmail_search,
            tools_instance.calendar_list_events,
        ]
        + dynamic_toolsets,
        output_key="raw_signals",
    )

    # ── Stage 2: Normalizer ───────────────────────────────────────────────────
    normalizer = LlmAgent(
        name="normalizer",
        model=model,
        instruction=NORMALIZER_INSTRUCTION,
        output_key="normalized_signals",
    )

    # ── Stage 3: Enricher ─────────────────────────────────────────────────────
    enricher = LlmAgent(
        name="enricher",
        model=model,
        instruction=ENRICHER_INSTRUCTION,
        tools=[
            tools_instance.search_knowledge_base,
            tools_instance.search_products,
        ],
        output_key="enriched_signals",
    )

    # ── Stage 4: Validator & Publisher ────────────────────────────────────────
    validator = LlmAgent(
        name="validator",
        model=model,
        instruction=VALIDATOR_INSTRUCTION.replace(
            "save_home_card",
            "save_home_card",
        ),
        tools=[save_home_card],
        output_key="published_cards",
    )

    return SequentialAgent(
        name="nightly_pipeline",
        description="Autonomous nightly intelligence pipeline that collects, normalizes, enriches, and publishes intelligence cards.",
        sub_agents=[signal_collector, normalizer, enricher, validator],
    )

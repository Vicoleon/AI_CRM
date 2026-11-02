"""Latency-tuning helpers shared by the chat assistant's ADK agents.

Lives in ``adk_agent`` (not ``agent_runtime``) so both the coordinator
(``agent_runtime.coordinator``) and the sales-email sub-agent
(``adk_agent.sales_email_agent``) can import it without a package cycle —
``agent_runtime`` already depends on ``adk_agent``, never the reverse.

The per-family thinking rules themselves live in ``rubik_core.gemini_tuning``,
which has no ADK dependency, so plain ``generate_content`` call sites get the
same tuning as the agents. This module is only the ADK ``BuiltInPlanner``
wrapper around it.
"""

from __future__ import annotations

from google.adk.planners import BuiltInPlanner

from rubik_core.gemini_tuning import thinking_config_for


def low_latency_planner(model_id: str | None) -> BuiltInPlanner | None:
    """Return a planner that curbs latency-inflating "thinking" for ``model_id``,
    or ``None`` when the model's default should be left untouched.

    See :func:`rubik_core.gemini_tuning.thinking_config_for` for the per-family
    rules (2.5 uses a numeric budget, 3.x and later use a level, 2.5 Pro is
    left alone).
    """
    thinking_config = thinking_config_for(model_id)
    if thinking_config is None:
        return None
    return BuiltInPlanner(thinking_config=thinking_config)

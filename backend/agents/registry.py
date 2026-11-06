"""Where department plugins get wired up.

Today: Sales-only. Add an HR system, an accounting system, etc., by
appending instances to `default_plugins()` below. The orchestrator
(`backend.agents.overnight.OvernightAgent`) calls this function once per run.

Why a function instead of a module-level list:
  - Lazy: avoids constructing BigQueryAdapter etc. at import time.
  - Testable: tests can substitute a different list via `OvernightAgent(plugins=[…])`.
  - No global mutable state to reset between runs.
"""

from __future__ import annotations

from backend.agents.base import DepartmentAgentPlugin
from backend.agents.multi_source_priorities import MultiSourcePrioritiesPlugin


def default_plugins() -> list[DepartmentAgentPlugin]:
    """Production plugin list. Edit here to enroll a new department.

    Sales runs UC-17's multi-source LLM-judged pipeline. The legacy
    mechanical-rules `SalesHubspotPlugin` has been deleted (see git
    history); its BigQuery probes live on in
    `backend/agents/sales_hubspot.py` for the HubSpot SignalSource.
    """
    return [
        MultiSourcePrioritiesPlugin(),
        # HRSystemPlugin(),          # TODO when UC adds HR support
        # FinanceXeroPlugin(),       # TODO when UC adds Finance support
    ]

"""Ingestion Council — adversarial multi-agent QA over Cubie's overnight and
weekly ingestions.

Adaptation of the LLM-council pattern (https://github.com/tenfoldmarc/llm-council-skill):
five opinionated advisors become five adversarial *auditors*, strategic-decision
review becomes ingestion QA, and the chairman's recommendation becomes a
PASS/WARN/FAIL health verdict. Pure core (no Slack, no HTTP); delivery + CLI live
in ``backend/agents/ingestion_council.py``.
"""

from rubik_core.ingestion_council.council import run_council
from rubik_core.ingestion_council.models import (
    CouncilVerdict,
    Health,
    RunKind,
    kinds_for_scope,
)
from rubik_core.ingestion_council.persist import (
    get_verdict,
    latest_verdicts,
    persist_verdict,
)
from rubik_core.ingestion_council.report import build_slack_blocks, render_html

__all__ = [
    "run_council",
    "CouncilVerdict",
    "Health",
    "RunKind",
    "kinds_for_scope",
    "persist_verdict",
    "latest_verdicts",
    "get_verdict",
    "render_html",
    "build_slack_blocks",
]

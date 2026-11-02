"""The council's single LLM seam.

Every auditor, peer-review and chairman call goes through :func:`council_llm`.
Concentrating the model call in one place buys two things:

1. **Offline tests.** Monkeypatch this one function and the whole council runs
   deterministically with zero Vertex calls.
2. **Free resilience.** It wraps the same :class:`LLMJsonClient` the priorities
   engine uses — Vertex Gemini via ``get_genai_client``, best-effort JSON
   parsing, and fallback-on-failure — so a single flaky auditor degrades to its
   ``fallback`` instead of taking the run down.
"""

from __future__ import annotations

import logging
from typing import Any

from rubik_core.priorities.agents.base import LLMJsonClient

logger = logging.getLogger(__name__)

# Auditors + peer reviewers run on the cheap/fast tier (structured extraction
# over a bounded evidence block); the chairman gets the reasoning tier because
# it has to weigh conflicting findings and commit to a verdict.
AUDITOR_MODEL = "gemini-flash"
CHAIRMAN_MODEL = "gemini-pro"


def council_llm(
    prompt: str,
    *,
    model_key: str = AUDITOR_MODEL,
    temperature: float = 0.3,
    fallback: Any | None = None,
) -> Any:
    """Run one shaped JSON call. Returns parsed JSON, or ``fallback`` on failure.

    ``fallback`` should always be supplied by callers inside the parallel fan-out
    so one bad auditor never raises into the pool.
    """
    return LLMJsonClient(model_key=model_key, temperature=temperature).invoke_json(
        prompt, fallback=fallback
    )

"""
Orchestrator protocol — shared interface for all chat orchestration backends.

The ADKOrchestrator (and any future alternatives) must conform to this protocol
so the router layer can treat them interchangeably.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from rubik_core.orchestration.models import OrchestrationResult


@runtime_checkable
class ChatOrchestrator(Protocol):
    """Contract every orchestrator backend must satisfy."""

    async def run(
        self,
        request_id: str,
        query: str,
        history: list[tuple],
        top_k: int = 5,
        filters: dict | None = None,
        chat_session_id: str | None = None,
        db_session: Any | None = None,
    ) -> OrchestrationResult:
        """Execute a single chat turn and return a normalised result."""
        ...

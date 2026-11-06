from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class RouteIntent(str, Enum):
    CHAT_RAG = "CHAT_RAG"
    CRM = "CRM"
    CALENDAR_READ = "CALENDAR_READ"
    EMAIL_READ = "EMAIL_READ"
    EMAIL_DRAFT = "EMAIL_DRAFT"
    PROFILE_EDIT = "PROFILE_EDIT"
    MEET_READ = "MEET_READ"
    HOMEFEED = "HOMEFEED"
    MULTI_RETRIEVAL = "MULTI_RETRIEVAL"
    PRODUCTS = "PRODUCTS"
    CLARIFY = "CLARIFY"
    UNKNOWN = "UNKNOWN"


class RouteDecision(BaseModel):
    intent: RouteIntent
    explanation: str = ""
    confidence: float = 1.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolExecutionResult(BaseModel):
    tool_name: str
    status: str  # "success", "error", "partial"
    output: Any
    latency_ms: int = 0
    error: str | None = None


class ExecutionTrace(BaseModel):
    request_id: str
    route: RouteDecision
    steps: list[ToolExecutionResult] = Field(default_factory=list)
    total_duration_ms: int = 0
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class OrchestrationResult(BaseModel):
    answer: str
    citations: list[dict[str, Any]] = Field(default_factory=list)
    hubspot_data: str | None = None
    generated_sql: str | None = None
    trace: ExecutionTrace

    # Usage metrics, summed over every model call in the turn
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # The model's thinking (Vertex ``thoughts_token_count``), billed at the
    # output rate and not part of ``completion_tokens``.
    thinking_tokens: int = 0
    # Context-cache hits (Vertex ``cached_content_token_count``): a subset of
    # ``prompt_tokens``, billed at the cached-input rate.
    cached_tokens: int = 0
    total_tokens: int = 0
    # Model usage split by ADK author, so a Sales sub-agent routed to a
    # different provider is billed to its actual model and owner.
    usage_by_agent: dict[str, dict[str, int]] = Field(default_factory=dict)

    # Set when the turn paused for the user's approval: the card the Chat page
    # draws (confirmation_render.confirmation_payload), rendered from the
    # paused call's own arguments. ``answer`` holds the same card as text.
    confirmation: dict[str, Any] | None = None

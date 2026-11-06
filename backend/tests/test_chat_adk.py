"""
ADK Chat Tests.

These tests verify that the ADK orchestrator path behaves correctly:
  - Session persistence
  - History budget
  - Follow-up context
  - Citation propagation
  - HubSpot data / generated_sql propagation
  - Error handling (no raw tracebacks)
  - Debug payload structure
  - Interaction logging
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth import get_current_user
from backend.main import app
from backend.routers.chat import (
    MAX_HISTORY_CHARS,
    MAX_HISTORY_MESSAGES,
    _classify_error,
    _trim_history,
)
from rubik_core.ai_providers.registry import IncompatibleModelRoute
from rubik_core.auth.registry import UserProfile
from rubik_core.config import settings
from rubik_core.database.db import get_db
from rubik_core.database.models import Base, ChatMessage, ChatSession, InteractionLog

# Pre-import so patch() decorators can resolve the ADK orchestrator
from rubik_core.orchestration.models import (
    ExecutionTrace,
    OrchestrationResult,
    RouteDecision,
    RouteIntent,
    ToolExecutionResult,
)

SQLALCHEMY_DATABASE_URL = "sqlite:///:memory:"
engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    try:
        db = TestingSessionLocal()
        yield db
    finally:
        db.close()


def mock_get_current_user():
    return UserProfile(
        id="test-user-prof",
        email="test@acme.example",
        name="Test User",
        department="sales",
        roles=["user"],
        created_at="2026-03-04T10:00:00Z",
        updated_at="2026-03-04T10:00:00Z",
    )


def _make_orch_result(
    answer="Test answer",
    citations=None,
    hubspot_data=None,
    generated_sql=None,
    intent=RouteIntent.CHAT_RAG,
    tool_calls=None,
):
    """Factory for a fake OrchestrationResult."""
    return OrchestrationResult(
        answer=answer,
        citations=citations or [],
        hubspot_data=hubspot_data,
        generated_sql=generated_sql,
        trace=ExecutionTrace(
            request_id="test",
            route=RouteDecision(
                intent=intent,
                explanation="test route",
                metadata={
                    "backend": "adk",
                    "tool_calls": tool_calls or [],
                    "warnings": [],
                },
            ),
            steps=[
                ToolExecutionResult(tool_name="test", status="success", output="ok")
            ],
            total_duration_ms=100,
        ),
        prompt_tokens=10,
        completion_tokens=20,
        total_tokens=30,
    )


@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client():
    return TestClient(app)


def setup_module(module):
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = mock_get_current_user


def teardown_module(module):
    app.dependency_overrides.clear()


@patch("backend.routers.chat._build_orchestrator")
def test_selected_workspace_model_failure_is_visible_without_fallback(
    mock_build, client
):
    mock_build.side_effect = IncompatibleModelRoute("anthropic API key is unavailable")
    response = client.post("/api/chat/", json={"query": "Hello"})
    assert response.status_code == 200
    assert "selected workspace AI model" in response.json()["answer"]
    assert "API key" not in response.json()["answer"]
    assert mock_build.call_count == 1


@patch("backend.routers.chat._build_orchestrator")
def test_stream_selected_workspace_model_failure_is_visible(
    mock_build, client, monkeypatch
):
    monkeypatch.setattr(settings, "CHAT_STREAMING_ENABLED", True)
    mock_build.side_effect = IncompatibleModelRoute("anthropic API key is unavailable")
    response = client.post("/api/chat/stream", json={"query": "Hello"})
    assert response.status_code == 502
    assert "selected workspace AI model" in response.json()["detail"]
    assert "API key" not in response.json()["detail"]
    assert mock_build.call_count == 1


# ── WS1: Orchestrator Selection ──────────────────────────────────


@patch(
    "rubik_core.orchestration.adk_orchestrator.ADKOrchestrator.__init__",
    return_value=None,
)
def test_get_chat_orchestrator_returns_adk(mock_adk_init):
    """_build_orchestrator should return ADKOrchestrator."""
    from backend.routers.chat import _build_orchestrator
    from rubik_core.orchestration.adk_orchestrator import ADKOrchestrator

    user = mock_get_current_user()

    orch = _build_orchestrator(user, "UTC")
    assert isinstance(orch, ADKOrchestrator)


# ── WS2: Session Persistence ─────────────────────────────────────


@patch("backend.routers.chat._build_orchestrator")
def test_adk_chat_persists_new_session(mock_build, client):
    """A new chat request should create a DB session and persist messages."""
    mock_orch = AsyncMock()
    mock_orch.run = AsyncMock(return_value=_make_orch_result())
    mock_build.return_value = mock_orch

    response = client.post("/api/chat/", json={"query": "Hello"})
    assert response.status_code == 200
    data = response.json()

    session_id = data["session_id"]
    assert session_id

    # Verify the session and messages exist in DB
    db = next(override_get_db())
    session = db.query(ChatSession).filter(ChatSession.id == session_id).first()
    assert session is not None
    assert session.user_email == "test@acme.example"

    messages = db.query(ChatMessage).filter(ChatMessage.session_id == session_id).all()
    assert len(messages) == 2  # user + assistant
    assert messages[0].role == "user"
    assert messages[1].role == "assistant"
    db.close()


@patch("backend.routers.chat._build_orchestrator")
def test_adk_chat_reuses_existing_session_history(mock_build, client):
    """A follow-up with session_id should load existing history."""
    mock_orch = AsyncMock()
    mock_orch.run = AsyncMock(return_value=_make_orch_result(answer="First answer"))
    mock_build.return_value = mock_orch

    # Turn 1
    r1 = client.post("/api/chat/", json={"query": "First question"})
    session_id = r1.json()["session_id"]

    # Turn 2
    mock_orch.run = AsyncMock(return_value=_make_orch_result(answer="Second answer"))
    r2 = client.post(
        "/api/chat/", json={"query": "Follow-up", "session_id": session_id}
    )
    assert r2.status_code == 200
    assert r2.json()["session_id"] == session_id

    # Verify history was passed to orchestrator on turn 2
    call_args = mock_orch.run.call_args
    history_arg = call_args.kwargs.get("history", [])
    assert len(history_arg) > 0  # Should contain turn 1 messages


@patch("backend.routers.chat._build_orchestrator")
def test_adk_chat_applies_history_budget(mock_build, client):
    """History should be trimmed by MAX_HISTORY_MESSAGES and MAX_HISTORY_CHARS."""
    mock_orch = AsyncMock()
    mock_build.return_value = mock_orch

    session_id = None
    for i in range(12):
        mock_orch.run = AsyncMock(return_value=_make_orch_result(answer="A" * 2000))
        payload = {"query": f"Question {i}"}
        if session_id:
            payload["session_id"] = session_id
        r = client.post("/api/chat/", json=payload)
        assert r.status_code == 200
        if not session_id:
            session_id = r.json()["session_id"]

    # Check the last call's history
    last_call = mock_orch.run.call_args
    history = last_call.kwargs.get("history", [])
    assert len(history) <= MAX_HISTORY_MESSAGES
    total_chars = sum(len(c) for _, c in history)
    assert total_chars <= MAX_HISTORY_CHARS


# ── WS5: Citation Fidelity ────────────────────────────────────────


@patch("backend.routers.chat._build_orchestrator")
def test_adk_knowledge_search_returns_citations(mock_build, client):
    """Citations from knowledge_search should appear in the response."""
    result = _make_orch_result(
        answer="Here is the policy info.",
        citations=[
            {
                "source_path": "policies/hr.pdf",
                "page": 3,
                "chunk_index": 0,
                "snippet": "HR policy text...",
            },
            {
                "source_path": "policies/it.pdf",
                "page": 1,
                "chunk_index": 1,
                "snippet": "IT policy text...",
            },
        ],
    )
    mock_orch = AsyncMock()
    mock_orch.run = AsyncMock(return_value=result)
    mock_build.return_value = mock_orch

    r = client.post("/api/chat/", json={"query": "What is the HR policy?"})
    assert r.status_code == 200
    data = r.json()
    assert len(data["citations"]) == 2
    assert data["citations"][0]["source_path"] == "policies/hr.pdf"
    assert data["citations"][0]["snippet"] == "HR policy text..."


@patch("backend.routers.chat._build_orchestrator")
def test_adk_preserves_hubspot_data_and_generated_sql(mock_build, client):
    """hubspot_data and generated_sql should flow through to the response."""
    result = _make_orch_result(
        answer="Revenue last month was $50k.",
        hubspot_data="--- HubSpot CRM Context ---\n1. rep_name: John | deals_won: 5",
        generated_sql="SELECT COUNT(*) FROM deals WHERE closed_won",
        intent=RouteIntent.CRM,
    )
    mock_orch = AsyncMock()
    mock_orch.run = AsyncMock(return_value=result)
    mock_build.return_value = mock_orch

    r = client.post("/api/chat/", json={"query": "How many deals did John close?"})
    assert r.status_code == 200
    data = r.json()
    assert data["hubspot_data"] is not None
    assert "HubSpot" in data["hubspot_data"]
    assert data["generated_sql"] is not None
    assert "SELECT" in data["generated_sql"]


# ── WS6: Debug Payload ────────────────────────────────────────────


@patch("backend.routers.chat._build_orchestrator")
def test_adk_debug_payload_contains_route_and_backend(mock_build, client):
    """Debug payload should contain backend, route intent, and duration."""
    result = _make_orch_result()
    mock_orch = AsyncMock()
    mock_orch.run = AsyncMock(return_value=result)
    mock_build.return_value = mock_orch

    r = client.post("/api/chat/", json={"query": "Test question"})
    data = r.json()
    debug = data["debug"]
    assert debug is not None
    assert "backend" in debug
    assert debug["backend"] in ("adk", "legacy")
    assert "total_duration_ms" in debug
    assert "route" in debug
    assert debug["route"]["intent"] == "CHAT_RAG"

    # Flat fields the DebugStatsPanel reads at the top level. These must be
    # present (and not nested) or the test-user stats panel renders blanks.
    assert debug["intent"] == "CHAT_RAG"
    assert debug["prompt_tokens"] == 10
    assert debug["completion_tokens"] == 20
    assert debug["total_tokens"] == 30
    # exec_ms preserves the orchestrator's own wall-clock (trace.total_duration_ms)
    # before chat.py overwrites total_duration_ms with the full-endpoint time.
    assert debug["exec_ms"] == 100


# ── WS7: Error Handling ──────────────────────────────────────────


@patch("backend.routers.chat._build_orchestrator")
def test_adk_handles_tool_error_without_raw_traceback(mock_build, client):
    """Tool errors should return friendly messages, not raw tracebacks."""
    mock_orch = AsyncMock()
    mock_orch.run = AsyncMock(
        side_effect=Exception("google.api_core.exceptions.Forbidden: 403 Access denied")
    )
    mock_build.return_value = mock_orch

    r = client.post("/api/chat/", json={"query": "Read my emails"})
    assert r.status_code == 200  # Should not 500
    data = r.json()
    assert "Traceback" not in data["answer"]
    assert "403" not in data["answer"]
    assert data["session_id"]  # Session should still be valid


@patch("backend.routers.chat._build_orchestrator")
def test_adk_handles_framework_validation_error_cleanly(mock_build, client):
    """Validation errors should be caught and returned as user-safe messages."""
    mock_orch = AsyncMock()
    mock_orch.run = AsyncMock(
        side_effect=ValueError("pydantic validation error: field 'x' required")
    )
    mock_build.return_value = mock_orch

    r = client.post("/api/chat/", json={"query": "Do something"})
    assert r.status_code == 200
    data = r.json()
    assert "rephras" in data["answer"].lower() or "try again" in data["answer"].lower()


@patch("backend.routers.chat._build_orchestrator")
def test_adk_timeout_returns_graceful_response(mock_build, client):
    """Timeout should produce a user-friendly message."""
    import asyncio

    mock_orch = AsyncMock()
    mock_orch.run = AsyncMock(side_effect=asyncio.TimeoutError())
    mock_build.return_value = mock_orch

    r = client.post("/api/chat/", json={"query": "Complex request"})
    assert r.status_code == 200
    data = r.json()
    assert "timed out" in data["answer"].lower()
    assert data["session_id"]


# ── WS6: Interaction Logging ─────────────────────────────────────


@patch("backend.routers.chat._build_orchestrator")
def test_adk_records_interaction_log(mock_build, client):
    """Each chat request should produce an interaction log row."""
    result = _make_orch_result()
    mock_orch = AsyncMock()
    mock_orch.run = AsyncMock(return_value=result)
    mock_build.return_value = mock_orch

    r = client.post("/api/chat/", json={"query": "Log this"})
    assert r.status_code == 200

    db = next(override_get_db())
    logs = (
        db.query(InteractionLog)
        .filter(InteractionLog.user_email == "test@acme.example")
        .all()
    )
    assert len(logs) >= 1
    log = logs[-1]
    assert log.user_input == "Log this"
    assert log.llm_output == "Test answer"
    assert log.successful is True
    assert "Backend: adk" in log.system_prompt or "Backend: legacy" in log.system_prompt
    db.close()


@patch("rubik_core.cost.usage.record_usage")
@patch("backend.routers.chat._build_orchestrator")
def test_adk_chat_books_the_whole_turn_with_cache_hits(mock_build, mock_record, client):
    """The AI-Cost row and the interaction log carry the turn's summed usage,
    and cache hits reach record_usage so they are priced at the cached rate."""
    result = _make_orch_result()
    result.prompt_tokens = 2_500
    result.completion_tokens = 80
    result.thinking_tokens = 500
    result.cached_tokens = 1_600
    result.total_tokens = 3_080
    mock_orch = AsyncMock()
    mock_orch.run = AsyncMock(return_value=result)
    mock_orch.model_id = "gemini-2.5-flash"
    mock_build.return_value = mock_orch

    r = client.post("/api/chat/", json={"query": "Book this"})
    assert r.status_code == 200

    kwargs = mock_record.call_args.kwargs
    assert kwargs["feature"] == "chat"
    assert kwargs["model"] == "gemini-2.5-flash"
    assert (kwargs["prompt_tokens"], kwargs["completion_tokens"]) == (2_500, 80)
    assert kwargs["thinking_tokens"] == 500
    assert kwargs["cached_tokens"] == 1_600
    assert kwargs["total_tokens"] == 3_080

    db = next(override_get_db())
    log = (
        db.query(InteractionLog).filter(InteractionLog.user_input == "Book this").one()
    )
    assert (log.prompt_tokens, log.total_tokens) == (2_500, 3_080)
    db.close()


# ── Error Classification Unit Tests ──────────────────────────────


def test_classify_error_policy():
    assert (
        _classify_error(Exception("Policy denied for this action")) == "policy_denied"
    )


def test_classify_error_timeout():
    assert _classify_error(Exception("Deadline exceeded")) == "timeout"


def test_classify_error_quota():
    assert (
        _classify_error(Exception("429 Resource exhausted")) == "downstream_api_error"
    )


def test_classify_error_validation():
    class PydanticError(Exception):
        pass

    # The error_type check uses the class name
    assert (
        _classify_error(Exception("validation error on field x"))
        == "agent_validation_error"
    )


def test_classify_error_generic():
    assert _classify_error(Exception("something went wrong")) == "agent_runtime_error"


# ── History Trimming Unit Tests (kept for parity) ─────────────────


def test_trim_history_caps_messages():
    """History should be capped to MAX_HISTORY_MESSAGES."""
    history = [("user", f"msg-{i}") for i in range(20)]
    trimmed = _trim_history(history, max_messages=8)
    assert len(trimmed) <= 8
    assert trimmed[-1][1] == "msg-19"


def test_trim_history_caps_chars():
    """History should be trimmed by char budget, dropping oldest first."""
    history = [("user", "x" * 1000) for _ in range(10)]
    trimmed = _trim_history(history, max_messages=10, max_chars=5000)
    total_chars = sum(len(c) for _, c in trimmed)
    assert total_chars <= 5000
    assert len(trimmed) < 10


def test_trim_history_empty():
    """Empty history should return empty."""
    assert _trim_history([]) == []


def test_trim_history_within_budget():
    """History within budget should be unchanged."""
    history = [("user", "short")] * 4
    trimmed = _trim_history(history, max_messages=8, max_chars=8000)
    assert len(trimmed) == 4

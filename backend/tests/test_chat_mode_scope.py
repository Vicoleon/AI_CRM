"""V2 chat plumbing: `mode` + `scope` request fields.

These tests cover the V2_1 contract: the backend ACCEPTS and LOGS the new
fields, but doesn't change retrieval / routing / prompt behavior. V1
clients (no `mode`, no `scope`) must keep working unchanged.

Covered:
  - Back-compat: omitting both fields → defaults (`ask`, None).
  - All six `scope.kind` values parse correctly.
  - Invalid `mode` → 422.
  - Invalid `scope.kind` → 422.
  - `scope.id` and `scope.label` are optional.
  - Orchestrator receives `mode` + `scope` in its kwargs.
  - `InteractionLog` row records `mode` + `scope`.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Pre-import so patch decorators resolve the ADK orchestrator module.
import rubik_core.orchestration.adk_orchestrator  # noqa: F401
from backend.auth import get_current_user
from backend.main import app
from rubik_core.auth.registry import UserProfile
from rubik_core.database.db import get_db
from rubik_core.database.models import Base, InteractionLog
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


def _make_orch_result():
    return OrchestrationResult(
        answer="ok",
        citations=[],
        hubspot_data=None,
        generated_sql=None,
        trace=ExecutionTrace(
            request_id="test",
            route=RouteDecision(
                intent=RouteIntent.CHAT_RAG,
                explanation="test",
                metadata={"backend": "adk", "tool_calls": [], "warnings": []},
            ),
            steps=[ToolExecutionResult(tool_name="t", status="success", output="ok")],
            total_duration_ms=1,
        ),
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
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


# ── Back-compat: V1 client (no mode, no scope) still works ────────


@patch("backend.routers.chat._build_orchestrator")
def test_v1_request_without_mode_or_scope_defaults_to_ask(mock_build, client):
    mock_orch = AsyncMock()
    mock_orch.run = AsyncMock(return_value=_make_orch_result())
    mock_build.return_value = mock_orch

    response = client.post("/api/chat/", json={"query": "Hello"})
    assert response.status_code == 200

    # Orchestrator must receive defaults
    call_kwargs = mock_orch.run.call_args.kwargs
    assert call_kwargs["mode"] == "ask"
    assert call_kwargs["scope"] is None


# ── Mode field ─────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", ["ask", "draft", "investigate"])
@patch("backend.routers.chat._build_orchestrator")
def test_valid_modes_parse(mock_build, client, mode):
    mock_orch = AsyncMock()
    mock_orch.run = AsyncMock(return_value=_make_orch_result())
    mock_build.return_value = mock_orch

    response = client.post("/api/chat/", json={"query": "hi", "mode": mode})
    assert response.status_code == 200
    assert mock_orch.run.call_args.kwargs["mode"] == mode


@patch("backend.routers.chat._build_orchestrator")
def test_invalid_mode_returns_422(mock_build, client):
    response = client.post("/api/chat/", json={"query": "hi", "mode": "nonsense"})
    assert response.status_code == 422


# ── Scope kinds ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "kind",
    ["workspace", "department", "deal", "meeting", "asset", "contact"],
)
@patch("backend.routers.chat._build_orchestrator")
def test_all_scope_kinds_parse(mock_build, client, kind):
    """All six scope kinds in the spec must parse and reach the orchestrator."""
    mock_orch = AsyncMock()
    mock_orch.run = AsyncMock(return_value=_make_orch_result())
    mock_build.return_value = mock_orch

    payload = {
        "query": "q",
        "scope": {"kind": kind, "id": "abc-123", "label": f"Some {kind}"},
    }
    response = client.post("/api/chat/", json=payload)
    assert response.status_code == 200, response.text

    call_kwargs = mock_orch.run.call_args.kwargs
    assert call_kwargs["scope"] is not None
    assert call_kwargs["scope"]["kind"] == kind
    assert call_kwargs["scope"]["id"] == "abc-123"
    assert call_kwargs["scope"]["label"] == f"Some {kind}"


@patch("backend.routers.chat._build_orchestrator")
def test_invalid_scope_kind_returns_422(mock_build, client):
    payload = {"query": "q", "scope": {"kind": "rocketship"}}
    response = client.post("/api/chat/", json=payload)
    assert response.status_code == 422


@patch("backend.routers.chat._build_orchestrator")
def test_scope_id_and_label_are_optional(mock_build, client):
    """scope only requires `kind`; `id` and `label` may be omitted."""
    mock_orch = AsyncMock()
    mock_orch.run = AsyncMock(return_value=_make_orch_result())
    mock_build.return_value = mock_orch

    payload = {"query": "q", "scope": {"kind": "workspace"}}
    response = client.post("/api/chat/", json=payload)
    assert response.status_code == 200

    call_kwargs = mock_orch.run.call_args.kwargs
    assert call_kwargs["scope"] == {"kind": "workspace", "id": None, "label": None}


# ── Persistence: mode + scope land on InteractionLog ─────────────


@patch("backend.routers.chat._build_orchestrator")
def test_interaction_log_persists_mode_and_scope(mock_build, client):
    mock_orch = AsyncMock()
    mock_orch.run = AsyncMock(return_value=_make_orch_result())
    mock_build.return_value = mock_orch

    payload = {
        "query": "draft a follow-up",
        "mode": "draft",
        "scope": {"kind": "deal", "id": "deal-42", "label": "Acme Industries"},
    }
    response = client.post("/api/chat/", json=payload)
    assert response.status_code == 200

    db = next(override_get_db())
    log = (
        db.query(InteractionLog)
        .filter(InteractionLog.user_email == "test@acme.example")
        .order_by(InteractionLog.timestamp.desc())
        .first()
    )
    assert log is not None
    assert log.mode == "draft"
    assert log.scope == {"kind": "deal", "id": "deal-42", "label": "Acme Industries"}
    db.close()


@patch("backend.routers.chat._build_orchestrator")
def test_interaction_log_default_mode_no_scope(mock_build, client):
    """V1-style request: mode defaults to 'ask', scope null on the log."""
    mock_orch = AsyncMock()
    mock_orch.run = AsyncMock(return_value=_make_orch_result())
    mock_build.return_value = mock_orch

    response = client.post("/api/chat/", json={"query": "hi"})
    assert response.status_code == 200

    db = next(override_get_db())
    log = (
        db.query(InteractionLog)
        .filter(InteractionLog.user_email == "test@acme.example")
        .order_by(InteractionLog.timestamp.desc())
        .first()
    )
    assert log is not None
    assert log.mode == "ask"
    assert log.scope is None
    db.close()

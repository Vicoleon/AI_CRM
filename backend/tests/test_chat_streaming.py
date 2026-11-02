"""Streaming chat endpoint (SSE) tests — POST /api/chat/stream.

Verifies the flag gate, that model text streams as SSE `token` frames, that a
terminal `done` frame carries the persisted message id, and that the streamed
turn is saved through the get_db override (never a raw SessionLocal → pytest
can't write to a real DB).
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth import get_current_user
from backend.main import app
from rubik_core.auth.registry import UserProfile
from rubik_core.config import settings
from rubik_core.database.db import get_db
from rubik_core.database.models import Base, ChatMessage, ChatSession

engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


def _mock_user():
    return UserProfile(
        id="test-user",
        email="test@acme.example",
        name="Test User",
        department="sales",
        roles=["user"],
        created_at="2026-03-04T10:00:00Z",
        updated_at="2026-03-04T10:00:00Z",
    )


async def _fake_stream(**kwargs):
    """Stand-in for ADKOrchestrator.run_streaming: two tokens then a done frame."""
    yield {"type": "token", "text": "Hel"}
    yield {"type": "token", "text": "lo!"}
    yield {
        "type": "done",
        "answer": "Hello!",
        "citations": [],
        "hubspot_data": None,
        "generated_sql": None,
        "tool_calls": [],
        "prompt_tokens": 1,
        "completion_tokens": 2,
        "total_tokens": 3,
        "duration_ms": 5,
    }


@pytest.fixture(autouse=True)
def _setup_db():
    Base.metadata.create_all(bind=engine)
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = _mock_user
    yield
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client():
    return TestClient(app)


@patch("backend.routers.chat._build_orchestrator")
def test_stream_emits_tokens_and_persists(mock_build, client):
    mock_orch = MagicMock()
    mock_orch.run_streaming = _fake_stream  # calling it returns an async generator
    mock_build.return_value = mock_orch

    resp = client.post("/api/chat/stream", json={"query": "Hi"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    body = resp.text
    assert '"type": "token"' in body
    assert "Hel" in body and "lo!" in body
    assert '"type": "done"' in body
    assert '"message_id"' in body

    # The streamed turn persisted via the overridden session (not a real DB).
    db = TestingSessionLocal()
    try:
        sessions = db.query(ChatSession).all()
        assert len(sessions) == 1
        msgs = (
            db.query(ChatMessage).filter(ChatMessage.session_id == sessions[0].id).all()
        )
        assert sorted(m.role for m in msgs) == ["assistant", "user"]
        assistant = next(m for m in msgs if m.role == "assistant")
        assert assistant.content == "Hello!"
    finally:
        db.close()


@patch("backend.routers.chat._build_orchestrator")
def test_stream_disabled_returns_404(mock_build, client, monkeypatch):
    monkeypatch.setattr(settings, "CHAT_STREAMING_ENABLED", False)
    mock_orch = MagicMock()
    mock_orch.run_streaming = _fake_stream
    mock_build.return_value = mock_orch

    resp = client.post("/api/chat/stream", json={"query": "Hi"})
    assert resp.status_code == 404

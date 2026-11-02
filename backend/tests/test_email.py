from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth import get_current_user
from backend.main import app
from rubik_core.auth.registry import UserProfile
from rubik_core.database.db import get_db
from rubik_core.database.models import AuditLog, Base

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


@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@patch("backend.routers.email.GmailClient")
def test_search_threads(mock_client_class):
    mock_instance = MagicMock()
    mock_instance.service = True
    mock_instance.search_threads.return_value = [
        {"id": "thread123", "snippet": "Test snippet"}
    ]
    mock_client_class.return_value = mock_instance

    client = TestClient(app)
    response = client.get("/api/email/search?query=test&max_results=5")

    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["id"] == "thread123"

    mock_client_class.assert_called_once_with(user_email="test@acme.example")

    db = TestingSessionLocal()
    audit_logs = db.query(AuditLog).all()
    assert len(audit_logs) == 1
    assert audit_logs[0].actor_email == "test@acme.example"
    assert audit_logs[0].action == "SEARCH_EMAIL_THREADS"
    db.close()


@patch("backend.routers.email.GmailClient")
def test_get_thread(mock_client_class):
    mock_instance = MagicMock()
    mock_instance.service = True
    mock_instance.get_thread.return_value = {"id": "thread123", "messages": []}
    mock_client_class.return_value = mock_instance

    client = TestClient(app)
    response = client.get("/api/email/threads/thread123")

    assert response.status_code == 200
    assert response.json() == {"id": "thread123", "messages": [], "account_id": None}

    db = TestingSessionLocal()
    audit_logs = db.query(AuditLog).all()
    assert len(audit_logs) == 1
    assert audit_logs[0].action == "VIEW_EMAIL_THREAD"
    db.close()


@patch("backend.routers.email.GmailClient")
def test_create_draft(mock_client_class):
    mock_instance = MagicMock()
    mock_instance.service = True
    mock_instance.create_draft.return_value = {
        "draft_id": "draft456",
        "message_id": "msg789",
        "gmail_draft_url": "https://mail.google.com/test",
    }
    mock_client_class.return_value = mock_instance

    client = TestClient(app)
    payload = {
        "to": ["recipient@example.com"],
        "subject": "Test Draft",
        "body_markdown": "Hello world",
    }
    response = client.post("/api/email/drafts", json=payload)

    assert response.status_code == 200
    assert response.json()["draft_id"] == "draft456"
    assert response.json()["gmail_draft_url"] == "https://mail.google.com/test"

    db = TestingSessionLocal()
    audit_logs = db.query(AuditLog).all()
    assert len(audit_logs) == 1
    assert audit_logs[0].action == "CREATE_EMAIL_DRAFT"
    db.close()


def setup_module(module):
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = mock_get_current_user


def teardown_module(module):
    app.dependency_overrides.clear()

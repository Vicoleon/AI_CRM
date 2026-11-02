"""GET /api/drive/recent-activity — Productivity → Documents tab."""

from __future__ import annotations

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
        id="user",
        email="me@acme.example",
        name="Me",
        department="sales",
        roles=["user"],
        created_at="2026-05-27T10:00:00Z",
        updated_at="2026-05-27T10:00:00Z",
    )


@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def override_deps():
    saved = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = mock_get_current_user
    yield
    app.dependency_overrides.clear()
    app.dependency_overrides.update(saved)


@patch("rubik_core.tools.drive.GoogleDriveClient")
def test_recent_activity_happy_path(mock_client_cls):
    """Drive client returns 2 files → endpoint normalizes them to the
    Productivity row shape."""
    mock_client = MagicMock()
    mock_client.service = True
    mock_client.list_recent_files.return_value = [
        {
            "id": "f-1",
            "name": "Q3 plan.gdoc",
            "mimeType": "application/vnd.google-apps.document",
            "modifiedTime": "2026-05-28T15:00:00Z",
            "createdTime": "2026-05-20T09:00:00Z",
            "lastModifyingUser": {
                "emailAddress": "me@acme.example",
                "displayName": "Me",
            },
            "owners": [{"emailAddress": "me@acme.example"}],
            "shared": False,
            "webViewLink": "https://docs.google.com/d/f-1",
        },
        {
            "id": "f-2",
            "name": "Customer call notes.txt",
            "mimeType": "text/plain",
            "modifiedTime": "2026-05-27T22:30:00Z",
            "createdTime": "2026-05-27T22:00:00Z",
            "lastModifyingUser": {
                "emailAddress": "boss@acme.example",
                "displayName": "Boss",
            },
            "owners": [{"emailAddress": "boss@acme.example"}],
            "shared": True,
            "webViewLink": "https://drive.google.com/file/d/f-2",
        },
    ]
    mock_client_cls.return_value = mock_client

    client = TestClient(app)
    response = client.get("/api/drive/recent-activity")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["days_back"] == 7
    assert len(payload["items"]) == 2

    f1, f2 = payload["items"]
    # File 1: owned by me, not shared
    assert f1["id"] == "f-1"
    assert f1["name"] == "Q3 plan.gdoc"
    assert f1["mime_type"] == "application/vnd.google-apps.document"
    assert f1["is_owned_by_me"] is True
    assert f1["is_shared"] is False
    assert f1["last_modifying_user_email"] == "me@acme.example"
    assert f1["last_modifying_user_name"] == "Me"
    assert f1["web_view_link"] == "https://docs.google.com/d/f-1"

    # File 2: shared with me, owned by boss
    assert f2["id"] == "f-2"
    assert f2["is_owned_by_me"] is False
    assert f2["is_shared"] is True
    assert f2["last_modifying_user_email"] == "boss@acme.example"

    # The Drive client was called with the right per-user scope.
    mock_client_cls.assert_called_once_with(user_email="me@acme.example")
    mock_client.list_recent_files.assert_called_once_with(
        days_back=7,
        max_results=25,
    )


@patch("rubik_core.tools.drive.GoogleDriveClient")
def test_recent_activity_writes_audit_log(mock_client_cls):
    """Each call must produce one AuditLog row tied to the calling user."""
    mock_client = MagicMock()
    mock_client.service = True
    mock_client.list_recent_files.return_value = []
    mock_client_cls.return_value = mock_client

    client = TestClient(app)
    response = client.get("/api/drive/recent-activity?days_back=14&max_results=50")
    assert response.status_code == 200
    assert response.json()["days_back"] == 14

    # AuditLog row exists for the actor.
    db = TestingSessionLocal()
    try:
        logs = db.query(AuditLog).all()
        assert len(logs) == 1
        assert logs[0].actor_email == "me@acme.example"
        assert logs[0].action == "ACCESS_DRIVE_RECENT_ACTIVITY"
        assert "days_back=14" in (logs[0].details or "")
        assert "max_results=50" in (logs[0].details or "")
    finally:
        db.close()


@patch("rubik_core.tools.drive.GoogleDriveClient")
def test_recent_activity_503_when_drive_service_not_initialized(mock_client_cls):
    """If the Drive client failed to authenticate, return 503 cleanly."""
    mock_client = MagicMock()
    mock_client.service = None
    mock_client_cls.return_value = mock_client

    client = TestClient(app)
    response = client.get("/api/drive/recent-activity")
    assert response.status_code == 503
    assert "not initialized" in response.json()["detail"].lower()


@patch("rubik_core.tools.drive.GoogleDriveClient")
def test_recent_activity_502_on_drive_api_error(mock_client_cls):
    """An exception from list_recent_files → 502 (upstream failure)."""
    mock_client = MagicMock()
    mock_client.service = True
    mock_client.list_recent_files.side_effect = RuntimeError("Drive 500")
    mock_client_cls.return_value = mock_client

    client = TestClient(app)
    response = client.get("/api/drive/recent-activity")
    assert response.status_code == 502
    assert "Drive 500" in response.json()["detail"]


def test_recent_activity_validates_query_params():
    """days_back out of [1, 30] or max_results out of [1, 100] → 422."""
    client = TestClient(app)
    bad_days = client.get("/api/drive/recent-activity?days_back=99")
    assert bad_days.status_code == 422
    bad_max = client.get("/api/drive/recent-activity?max_results=0")
    assert bad_max.status_code == 422

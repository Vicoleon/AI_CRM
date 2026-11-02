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
from rubik_core.tools.calendar import GoogleCalendarClient, is_placeholder_hold

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


# --- Mocking the Calendar Service ---
from unittest.mock import MagicMock, patch


@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@patch("backend.routers.calendar.GoogleCalendarClient")
def test_get_calendar_events(mock_client_class):
    # Setup Mock
    mock_instance = MagicMock()
    mock_instance.service = True
    mock_instance.get_upcoming_events.return_value = [
        {"summary": "Test Event", "start": {"dateTime": "2026-03-04T12:00:00Z"}}
    ]
    mock_client_class.return_value = mock_instance

    client = TestClient(app)
    response = client.get("/api/calendar/events")

    # Assert successful API execution
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["summary"] == "Test Event"

    # Assert client uses exactly the authenticated user email
    mock_client_class.assert_called_once_with(user_email="test@acme.example")

    # Assert AuditLog was written
    db = TestingSessionLocal()
    audit_logs = db.query(AuditLog).all()
    assert len(audit_logs) == 1
    assert audit_logs[0].actor_email == "test@acme.example"
    assert audit_logs[0].action == "ACCESS_CALENDAR_EVENTS"
    db.close()


@patch("backend.routers.calendar.GoogleCalendarClient")
def test_get_calendar_availability(mock_client_class):
    mock_instance = MagicMock()
    mock_instance.service = True
    mock_instance.get_availability.return_value = {"is_busy": False, "busy_slots": []}
    mock_client_class.return_value = mock_instance

    client = TestClient(app)

    # Test valid ISO String
    response = client.get(
        "/api/calendar/availability?start_time=2026-03-04T00:00:00Z&end_time=2026-03-05T00:00:00Z"
    )

    assert response.status_code == 200
    assert response.json() == {"is_busy": False, "busy_slots": []}

    # Assert AuditLog was written
    db = TestingSessionLocal()
    audit_logs = db.query(AuditLog).all()
    assert len(audit_logs) == 1
    assert audit_logs[0].action == "ACCESS_CALENDAR_AVAILABILITY"
    db.close()


def test_is_placeholder_hold_matches_bare_hold_titles():
    """Bare self-hold titles — "Busy"/"Blocked"/"Focus" and close variants, any
    case/whitespace/emoji/punctuation — are holds. A real meeting that merely
    *contains* one of these words must be kept."""
    # The three the user called out, plus case/whitespace/emoji/punctuation.
    assert is_placeholder_hold({"summary": "Busy"})
    assert is_placeholder_hold({"summary": "  busy  "})
    assert is_placeholder_hold({"summary": "BUSY"})
    assert is_placeholder_hold({"summary": "Blocked"})
    assert is_placeholder_hold({"summary": "BLOCKED"})
    assert is_placeholder_hold({"summary": "🔴 Blocked"})
    assert is_placeholder_hold({"summary": "[Focus]"})
    assert is_placeholder_hold({"summary": "Focus Time"})
    assert is_placeholder_hold({"summary": "Do-Not-Book"})
    assert is_placeholder_hold({"summary": "OOO"})
    # Real meetings that merely contain a hold word are kept.
    assert not is_placeholder_hold({"summary": "Busy season planning"})
    assert not is_placeholder_hold({"summary": "Focus group with Acme"})
    assert not is_placeholder_hold({"summary": "Blocked-account remediation"})
    assert not is_placeholder_hold({"summary": "1:1 with Emery"})
    assert not is_placeholder_hold({"summary": ""})
    assert not is_placeholder_hold({})


@patch.object(GoogleCalendarClient, "_build_service")
def test_get_upcoming_events_bounds_by_time_max(mock_build):
    """A `time_max` is forwarded to the Calendar API as `timeMax`, so the fetch
    is bounded by a date horizon (same for everyone) rather than only the
    `max_results` event count (which a packed calendar exhausts in a few days)."""
    svc = MagicMock()
    events_ns = svc.events.return_value
    events_ns.list.return_value.execute.return_value = {"items": []}
    mock_build.return_value = svc

    client = GoogleCalendarClient(user_email="test@acme.example")
    client.get_upcoming_events(
        max_results=250,
        time_min="2026-07-10T00:00:00Z",
        time_max="2026-07-24T00:00:00Z",
    )
    kwargs = events_ns.list.call_args.kwargs
    assert kwargs["timeMax"] == "2026-07-24T00:00:00Z"
    assert kwargs["timeMin"] == "2026-07-10T00:00:00Z"
    assert kwargs["maxResults"] == 250


@patch.object(GoogleCalendarClient, "_build_service")
def test_get_upcoming_events_omits_time_max_when_not_given(mock_build):
    """Back-compat: callers that don't pass `time_max` get no `timeMax` kwarg."""
    svc = MagicMock()
    events_ns = svc.events.return_value
    events_ns.list.return_value.execute.return_value = {"items": []}
    mock_build.return_value = svc

    client = GoogleCalendarClient(user_email="test@acme.example")
    client.get_upcoming_events(max_results=10, time_min="2026-07-10T00:00:00Z")
    assert "timeMax" not in events_ns.list.call_args.kwargs


def setup_module(module):
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = mock_get_current_user


def teardown_module(module):
    app.dependency_overrides.clear()

"""Tests for UC-39 — Homefeed IA cleanup + smart action buttons.

Covers:
- BriefingResponse no longer carries an ``action_items`` field (regression).
- PriorityItem now exposes ``priority_kind`` / ``entity_kind`` / ``entity_id``
  via ``_bundle_dict_from_metadata`` so the frontend can render smart actions.
- ``POST /api/calendar/focus-block`` happy path + 409 ``no_free_slot``.
- ``POST /api/email/drafts/from-transcript`` happy path + UC-25
  COMMITMENT_DEBT auto-resolve hook firing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth import get_current_user
from backend.main import app
from rubik_core.auth.registry import UserProfile
from rubik_core.database.db import get_db
from rubik_core.database.models import Base

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
    """Save + restore dep overrides so we don't clobber sibling test files."""
    saved = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = mock_get_current_user
    yield
    app.dependency_overrides.clear()
    app.dependency_overrides.update(saved)


# ── UC-39 regression: BriefingResponse drops action_items ──────────────────


def test_uc39_briefing_response_has_no_action_items_field():
    """The Pydantic model must no longer carry ``action_items`` — the
    UC-20→25 AI Priorities pipeline owns "what to act on" now.
    """
    from backend.routers.home import BriefingResponse

    assert "action_items" not in BriefingResponse.model_fields


def test_uc39_action_item_class_removed():
    """``ActionItem`` was the per-email LLM extraction class. Killed in UC-39."""
    from backend.routers import home as home_module

    assert not hasattr(home_module, "ActionItem")
    assert not hasattr(home_module, "_extract_action_items")


# ── UC-39 entity tag exposure on PriorityItem ──────────────────────────────


def test_uc39_priority_item_exposes_entity_fields():
    """The Pydantic model on the API boundary must surface the entity tags
    the frontend uses to derive smart action affordances."""
    from backend.routers.home import PriorityItem

    fields = PriorityItem.model_fields
    for name in ("priority_kind", "entity_kind", "entity_id", "entity_label"):
        assert name in fields, f"PriorityItem missing UC-39 field: {name}"


def test_uc39_bundle_dict_projects_entity_tags():
    """``_bundle_dict_from_metadata`` must include UC-24 entity tags in the
    response shape so the frontend doesn't have to guess them."""
    from backend.routers.home import _bundle_dict_from_metadata

    metadata = {
        "schema_version": 2,
        "crm_provenance": {
            "workspace": "test",
            "deal_ids": [],
            "unprovable": False,
        },
        "generated_at": "2026-05-27T10:00:00Z",
        "headline": "3 things need you today",
        "revision_count": 1,
        "last_critique_score": 8.2,
        "priorities": [
            {
                "id": "p1",
                "title": "Follow up with ACME",
                "why_it_matters": "reply due",
                "urgency": "high",
                "source_channels": ["gmail"],
                "source_links": ["https://..."],
                "suggested_action": "Reply",
                "priority_kind": "follow_up_debt",
                "entity_kind": "thread",
                "entity_id": "thread-abc",
                "entity_label": "ACME — pricing question",
            }
        ],
    }
    out = _bundle_dict_from_metadata(metadata)
    p = out["priorities"][0]
    assert p["priority_kind"] == "follow_up_debt"
    assert p["entity_kind"] == "thread"
    assert p["entity_id"] == "thread-abc"
    assert p["entity_label"] == "ACME — pricing question"


def test_uc39_bundle_dict_hides_legacy_rows_without_provenance():
    """Old cards with no source or entity cannot safely be replayed."""
    from backend.routers.home import _bundle_dict_from_metadata

    metadata = {
        "schema_version": 2,
        "crm_provenance": {
            "workspace": "test",
            "deal_ids": [],
            "unprovable": False,
        },
        "generated_at": "",
        "headline": "",
        "revision_count": 0,
        "last_critique_score": None,
        "priorities": [
            {
                "id": "p1",
                "title": "Old priority",
                "why_it_matters": "",
                "urgency": "low",
                "source_channels": [],
                "source_links": [],
                "suggested_action": "",
            }
        ],
    }
    out = _bundle_dict_from_metadata(metadata)
    assert out["priorities"] == []
    assert out["headline"] == ""


# ── UC-39 /api/calendar/focus-block ─────────────────────────────────────────


@patch("backend.routers.calendar.GoogleCalendarClient")
def test_uc39_focus_block_happy_path(mock_client_class):
    """Free calendar → picks the first slot in the work window + creates an event."""
    mock_instance = MagicMock()
    mock_instance.service = True
    mock_instance.get_calendar_timezone.return_value = "UTC"
    # Empty `busy` list = totally free schedule. Matches the real
    # GoogleCalendarClient.get_availability() contract: {"is_busy", "busy_slots"}.
    mock_instance.get_availability.return_value = {
        "is_busy": False,
        "busy_slots": {"primary": []},
    }
    mock_instance.create_event.return_value = {
        "id": "evt-focus-1",
        "htmlLink": "https://calendar.google.com/?eid=evt-focus-1",
    }
    mock_client_class.return_value = mock_instance

    client = TestClient(app)
    response = client.post(
        "/api/calendar/focus-block",
        json={"title": "Deep work", "duration_minutes": 30, "priority_id": "p1"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["event_id"] == "evt-focus-1"
    assert payload["html_link"].startswith("https://calendar.google.com/")
    assert payload["start_iso"].endswith("Z")
    assert payload["end_iso"].endswith("Z")

    # Should have created exactly one event with the user-supplied title.
    create_kwargs = mock_instance.create_event.call_args.kwargs
    assert create_kwargs["summary"] == "Deep work"
    assert "Focus block created by Cubie." in create_kwargs["description"]
    assert "p1" in create_kwargs["description"]


@patch("backend.routers.calendar.GoogleCalendarClient")
def test_uc39_focus_block_409_no_free_slot(mock_client_class):
    """Wall-to-wall busy in the work window → 409 ``no_free_slot``."""
    from datetime import datetime, timedelta, timezone

    # Fabricate a 24h "everything is busy 8am-8pm every day" block so the
    # 9am-6pm work window has no gap large enough.
    now = datetime.now(timezone.utc)
    busy = []
    for day in range(2):
        d = now + timedelta(days=day)
        s = d.replace(hour=0, minute=0, second=0, microsecond=0)
        e = d.replace(hour=23, minute=59, second=0, microsecond=0)
        busy.append(
            {
                "start": s.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end": e.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        )

    mock_instance = MagicMock()
    mock_instance.service = True
    mock_instance.get_calendar_timezone.return_value = "UTC"
    mock_instance.get_availability.return_value = {
        "is_busy": True,
        "busy_slots": {"primary": busy},
    }
    mock_client_class.return_value = mock_instance

    client = TestClient(app)
    response = client.post(
        "/api/calendar/focus-block",
        json={"title": "Deep work", "duration_minutes": 30},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "no_free_slot"
    # Should NOT have attempted to create an event.
    mock_instance.create_event.assert_not_called()


@patch("backend.routers.calendar.GoogleCalendarClient")
def test_uc39_focus_block_validates_duration(mock_client_class):
    """duration_minutes outside [5, 240] → 400."""
    mock_instance = MagicMock()
    mock_instance.service = True
    mock_client_class.return_value = mock_instance

    client = TestClient(app)
    r1 = client.post(
        "/api/calendar/focus-block",
        json={"title": "x", "duration_minutes": 1},
    )
    assert r1.status_code == 400
    r2 = client.post(
        "/api/calendar/focus-block",
        json={"title": "x", "duration_minutes": 9000},
    )
    assert r2.status_code == 400
    # Neither attempted any calendar I/O.
    mock_instance.get_availability.assert_not_called()


# ── UC-39 /api/email/drafts/from-transcript ─────────────────────────────────


@patch("rubik_core.priorities.memory.resolve")
@patch("backend.routers.email.GmailClient")
@patch("rubik_core.models.get_genai_client")
@patch("rubik_core.tools.drive.GoogleDriveClient")
def test_uc39_draft_from_transcript_happy_path_resolves_commitment_debt(
    mock_drive_cls,
    mock_genai,
    mock_gmail_cls,
    mock_memory_resolve,
):
    """LLM produces a draft, Gmail creates it, UC-25 hook fires with
    EntityKind.TRANSCRIPT + PriorityKind.COMMITMENT_DEBT.
    """
    # Drive: transcript text mentions one external email.
    mock_drive = MagicMock()
    mock_drive.export_document_text.return_value = (
        "Meeting transcript.\n"
        "Bob promised to send the proposal by Friday.\n"
        "Attendees: bob@acme.io, test@acme.example\n"
    )
    mock_drive_cls.return_value = mock_drive

    # LLM: returns clean JSON.
    fake_llm = MagicMock()
    fake_response = MagicMock()
    fake_response.text = (
        '{"subject": "Re: ACME — proposal follow-up", '
        '"body_markdown": "Bob,\\n\\nLooping back on the proposal.\\n\\n—Test"}'
    )
    fake_llm.aio.models.generate_content = AsyncMock(return_value=fake_response)
    mock_genai.return_value = fake_llm

    # Gmail: returns a draft id + url.
    mock_gmail = MagicMock()
    mock_gmail.service = True
    mock_gmail.create_draft.return_value = {
        "draft_id": "d-42",
        "gmail_draft_url": "https://mail.google.com/mail/u/0/#drafts/d-42",
    }
    mock_gmail_cls.return_value = mock_gmail

    client = TestClient(app)
    response = client.post(
        "/api/email/drafts/from-transcript",
        json={
            "transcript_id": "drive-file-xyz",
            "focus_on": "send proposal recap",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["draft_id"] == "d-42"
    assert body["subject"].startswith("Re: ACME")
    assert "bob@acme.io" in body["to"]
    # User's own email (test@acme.example) must be filtered out of recipients.
    assert "test@acme.example" not in body["to"]

    # UC-25 hook must have fired with COMMITMENT_DEBT + transcript identity.
    mock_memory_resolve.assert_called_once()
    kwargs = mock_memory_resolve.call_args.kwargs
    from rubik_core.priorities.models import EntityKind, PriorityKind

    assert kwargs["user_email"] == "test@acme.example"
    assert kwargs["entity_kind"] == EntityKind.TRANSCRIPT
    assert kwargs["entity_id"] == "drive-file-xyz"
    assert kwargs["priority_kind"] == PriorityKind.COMMITMENT_DEBT
    assert kwargs["evidence"]["kind"] == "transcript_draft_created"
    assert kwargs["evidence"]["draft_id"] == "d-42"


@patch("backend.routers.email.GmailClient")
@patch("rubik_core.models.get_genai_client")
@patch("rubik_core.tools.drive.GoogleDriveClient")
def test_uc39_draft_from_transcript_404_when_no_text(
    mock_drive_cls, mock_genai, mock_gmail_cls
):
    """Missing transcript text → 404 (do NOT call LLM or Gmail)."""
    mock_drive = MagicMock()
    mock_drive.export_document_text.return_value = ""
    mock_drive_cls.return_value = mock_drive

    client = TestClient(app)
    response = client.post(
        "/api/email/drafts/from-transcript",
        json={"transcript_id": "drive-missing"},
    )
    assert response.status_code == 404
    # Should not have called the LLM or Gmail.
    mock_genai.assert_not_called()
    mock_gmail_cls.assert_not_called()


@patch("rubik_core.priorities.memory.resolve")
@patch("backend.routers.email.GmailClient")
@patch("rubik_core.models.get_genai_client")
@patch("rubik_core.tools.drive.GoogleDriveClient")
def test_uc39_draft_from_transcript_uc25_hook_failure_is_swallowed(
    mock_drive_cls,
    mock_genai,
    mock_gmail_cls,
    mock_memory_resolve,
):
    """UC-25 memory write blowing up must not 5xx the user — they got their draft."""
    mock_drive = MagicMock()
    mock_drive.export_document_text.return_value = "transcript body"
    mock_drive_cls.return_value = mock_drive

    fake_llm = MagicMock()
    fake_response = MagicMock()
    fake_response.text = '{"subject": "s", "body_markdown": "b"}'
    fake_llm.aio.models.generate_content = AsyncMock(return_value=fake_response)
    mock_genai.return_value = fake_llm

    mock_gmail = MagicMock()
    mock_gmail.service = True
    mock_gmail.create_draft.return_value = {"draft_id": "d-1", "gmail_draft_url": "x"}
    mock_gmail_cls.return_value = mock_gmail

    # Memory.resolve raises — should be swallowed, not propagate.
    mock_memory_resolve.side_effect = RuntimeError("DB down")

    client = TestClient(app)
    response = client.post(
        "/api/email/drafts/from-transcript",
        json={"transcript_id": "drive-file-xyz"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["draft_id"] == "d-1"

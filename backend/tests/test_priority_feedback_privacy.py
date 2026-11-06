"""Private Gmail priority text must not reach shared feedback or its export."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth import get_current_user
from backend.main import app
from rubik_core.database.db import get_db
from rubik_core.database.models import AssistantFeedback, Base, HomeCard, User
from rubik_core.database.schemas import UserProfile
from rubik_core.feedback import store

OWNER = "owner@acme.example"


@pytest.fixture
def feedback_client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False)
    db = Session()
    db.add(User(id="owner", email=OWNER, name="Owner", department="sales"))
    db.add(
        HomeCard(
            id="mixed-run",
            user_email=OWNER,
            date="2026-10-30",
            category="daily_briefing",
            content_md="private briefing",
            metadata_json={
                "priorities": [
                    {
                        "id": "personal-card",
                        "title": "Personal subject",
                        "attrs": {
                            "personal_gmail": True,
                            "private_gmail_context": True,
                        },
                    },
                    {
                        "id": "work-looking-card",
                        "title": "Work card influenced by personal mail",
                        "attrs": {},
                    },
                ]
            },
        )
    )
    db.add(
        HomeCard(
            id="work-run",
            user_email=OWNER,
            date="2026-10-29",
            category="daily_briefing",
            content_md="work briefing",
            metadata_json={
                "priorities": [
                    {
                        "id": "work-card",
                        "title": "Real work title",
                        "why_it_matters": "Real work reason",
                        "suggested_action": "Real work action",
                        "urgency": "high",
                        "attrs": {},
                    }
                ]
            },
        )
    )
    db.commit()
    roles = ["user"]

    def override_db():
        yield db

    def override_user():
        return UserProfile(
            id="owner",
            email=OWNER,
            name="Owner",
            department="sales",
            roles=roles,
            created_at="2026-10-30T00:00:00Z",
            updated_at="2026-10-30T00:00:00Z",
        )

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user
    try:
        yield TestClient(app), db, roles
    finally:
        app.dependency_overrides.clear()
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.mark.parametrize("priority_id", ["personal-card", "work-looking-card"])
def test_mixed_run_rejects_feedback_even_for_work_looking_card(
    feedback_client, priority_id
):
    client, db, _ = feedback_client
    response = client.post(
        "/api/feedback",
        json={
            "surface": "priorities",
            "proposal_id": priority_id,
            "proposal_text": "Private mailbox text",
            "proposal_metadata": {"mailbox": "personal@example.com"},
            "comment": "Private comment",
            "rating": "useful",
        },
    )
    assert response.status_code == 403
    assert db.query(AssistantFeedback).count() == 0


def test_public_priority_uses_trusted_snapshot_and_admin_export_filters_old_private_row(
    feedback_client,
):
    client, db, roles = feedback_client
    response = client.post(
        "/api/feedback",
        json={
            "surface": "priorities",
            "proposal_id": "work-card",
            "proposal_text": "Forged personal mailbox text",
            "proposal_metadata": {"mailbox": "personal@example.com"},
            "rating": "useful",
        },
    )
    assert response.status_code == 200, response.text
    saved = db.query(AssistantFeedback).filter_by(proposal_id="work-card").one()
    assert (
        saved.proposal_text
        == "Real work title\nReal work reason\nNext: Real work action"
    )
    assert saved.proposal_metadata["title"] == "Real work title"
    assert "mailbox" not in saved.proposal_metadata

    # A row written by an earlier build must also stay out of both admin APIs.
    store.upsert_feedback(
        db,
        user_email=OWNER,
        surface="priorities",
        proposal_id="personal-card",
        proposal_text="Old private subject",
        proposal_metadata={"mailbox": "personal@example.com"},
        rating="meh",
    )
    roles.append("admin")
    listing = client.get("/api/feedback/admin", params={"surface": "priorities"})
    assert listing.status_code == 200, listing.text
    assert listing.json()["total"] == 1
    assert [item["proposal_id"] for item in listing.json()["items"]] == ["work-card"]
    exported = client.get(
        "/api/feedback/admin/export", params={"surface": "priorities"}
    )
    assert exported.status_code == 200, exported.text
    assert [item["proposal_id"] for item in exported.json()] == ["work-card"]
    assert "Old private subject" not in exported.text


def test_unverified_priority_id_cannot_store_client_supplied_content(feedback_client):
    client, db, _ = feedback_client
    response = client.post(
        "/api/feedback",
        json={
            "surface": "priorities",
            "proposal_id": "another-users-card",
            "proposal_text": "Private mailbox text",
            "rating": "useful",
        },
    )
    assert response.status_code == 404
    assert db.query(AssistantFeedback).count() == 0

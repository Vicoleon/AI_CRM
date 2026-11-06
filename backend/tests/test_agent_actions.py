"""UC-10 follow-up: tests for the per-row action endpoints + feedback table.

Covers the contract every action shares (writes a feedback row, stamps
action_type/acted_on_at, scoped to the user's own rec) and the behaviors
unique to each action (snooze re-surfaces after expiry; dismiss is permanent;
duplicate-guard treats acted-on rows as handled).

Tests use an in-memory SQLite DB to avoid touching deployed Cloud SQL
(per the project's known test_pollution_bug — module-level SessionLocal
would otherwise read .env's DATABASE_URL).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth import get_current_user
from backend.main import app
from backend.routers import agents as agents_router
from rubik_core.auth.registry import UserProfile
from rubik_core.database.db import get_db
from rubik_core.database.models import (
    AgentRecommendation,
    AgentRecommendationFeedback,
    Base,
    User,
)

# ─── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _owned_test_deals(monkeypatch):
    """Action semantics use deals proven live-owned in this isolated fixture."""
    monkeypatch.setattr(agents_router, "visible_hubspot_deal_ids", lambda ids: ids)


@pytest.fixture
def db_session():
    """Per-test in-memory SQLite — overrides FastAPI's get_db dep AND any
    module-level SessionLocal lookups."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = Session()
    try:
        db.add(
            User(
                email="rep1@acme.example",
                id="u1",
                name="Rep One",
                department="sales",
            )
        )
        db.add(
            User(
                email="rep2@acme.example",
                id="u2",
                name="Rep Two",
                department="sales",
            )
        )
        db.commit()
    finally:
        db.close()
    yield Session
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client(db_session):
    """TestClient with rep1 acting + DB overridden to in-memory."""
    sess = db_session()

    def override_get_db():
        try:
            yield sess
        finally:
            pass  # session lifetime tied to fixture

    def override_user():
        return UserProfile(
            id="u1",
            email="rep1@acme.example",
            name="Rep One",
            department="sales",
            roles=["user"],
            created_at="2026-05-01T00:00:00Z",
            updated_at="2026-05-01T00:00:00Z",
        )

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_user
    yield TestClient(app)
    app.dependency_overrides.clear()
    sess.close()


def _seed_rec(db, **overrides) -> AgentRecommendation:
    defaults = dict(
        id="rec-1",
        user_email="rep1@acme.example",
        type="stale_lead",
        lead_id="deal-1",
        lead_name="ABC Corp",
        recommendation_text="Haven't contacted ABC Corp in 23 days.",
        evidence={"days_since_last_activity": 23},
        confidence=0.8,
        source_metadata={"plugin": "sales_hubspot"},
        created_at=datetime.utcnow(),
    )
    defaults.update(overrides)
    rec = AgentRecommendation(**defaults)
    db.add(rec)
    db.commit()
    return rec


# ─── Snooze ───────────────────────────────────────────────────────────────────


def test_snooze_hides_until_expiry_then_resurfaces(client, db_session):
    """A snoozed row falls out of fetch_top_recommendations until the snooze
    expires. Critical because it's the difference between 'remind me Wednesday'
    and silently losing the recommendation."""
    sess = db_session()
    rec = _seed_rec(sess, id="rec-snooze")
    sess.close()

    r = client.post("/api/agents/recommendations/rec-snooze/snooze", json={"days": 3})
    assert r.status_code == 200, r.text
    snoozed_until = datetime.fromisoformat(r.json()["snoozed_until"])
    assert snoozed_until > datetime.utcnow() + timedelta(days=2, hours=23)

    # While snoozed: not in top-N
    from backend.routers.agents import fetch_top_recommendations

    sess = db_session()
    assert fetch_top_recommendations(sess, "rep1@acme.example") == []

    # After expiry: surfaces again
    sess.query(AgentRecommendation).filter_by(id="rec-snooze").update(
        {"snoozed_until": datetime.utcnow() - timedelta(seconds=1)}
    )
    sess.commit()
    surfaced = fetch_top_recommendations(sess, "rep1@acme.example")
    assert [r.id for r in surfaced] == ["rec-snooze"]
    sess.close()


def test_snooze_writes_feedback_row(client, db_session):
    """Every action — including snooze — must persist a feedback row so the
    AI calibration loop has full action history. Verify schema + payload."""
    sess = db_session()
    _seed_rec(sess, id="rec-fb")
    sess.close()

    client.post("/api/agents/recommendations/rec-fb/snooze", json={"days": 3})

    sess = db_session()
    fb = sess.query(AgentRecommendationFeedback).all()
    assert len(fb) == 1
    assert fb[0].recommendation_id == "rec-fb"
    assert fb[0].action_type == "snoozed"
    assert "snoozed_until" in fb[0].action_metadata
    assert fb[0].action_metadata["days"] == 3
    sess.close()


# ─── Dismiss ──────────────────────────────────────────────────────────────────


def test_dismiss_is_permanent_and_writes_feedback(client, db_session):
    sess = db_session()
    _seed_rec(sess, id="rec-dismiss")
    sess.close()

    r = client.post("/api/agents/recommendations/rec-dismiss/dismiss")
    assert r.status_code == 200

    sess = db_session()
    rec = sess.query(AgentRecommendation).filter_by(id="rec-dismiss").one()
    assert rec.dismissed_at is not None
    # Permanent: even if we clear snoozed_until, dismissed should keep it hidden.
    rec.snoozed_until = None
    sess.commit()
    from backend.routers.agents import fetch_top_recommendations

    assert fetch_top_recommendations(sess, "rep1@acme.example") == []

    fb = sess.query(AgentRecommendationFeedback).all()
    assert [f.action_type for f in fb] == ["dismissed"]
    sess.close()


# ─── Open in HubSpot ──────────────────────────────────────────────────────────


def test_open_hubspot_returns_deep_link_when_portal_id_set(
    client, db_session, monkeypatch
):
    """Deep-link format is fixed; verify both happy path + falsy when portal
    id not configured. The frontend uses the link as-is, so format matters."""
    from rubik_core.tenancy import hubspot_config

    monkeypatch.setattr(
        hubspot_config,
        "get_hubspot_config",
        lambda: hubspot_config.HubSpotConnectorConfig(portal_id="12345678"),
    )
    sess = db_session()
    _seed_rec(sess, id="rec-hs", lead_id="deal-77")
    sess.close()

    r = client.post("/api/agents/recommendations/rec-hs/open-hubspot")
    assert r.status_code == 200
    body = r.json()
    assert body["deal_id"] == "deal-77"
    assert (
        body["deep_link"]
        == "https://app.hubspot.com/contacts/12345678/record/0-3/deal-77"
    )


def test_open_hubspot_returns_no_link_when_portal_id_unset(
    client, db_session, monkeypatch
):
    from rubik_core.tenancy import hubspot_config

    monkeypatch.setattr(
        hubspot_config,
        "get_hubspot_config",
        hubspot_config.HubSpotConnectorConfig,
    )
    sess = db_session()
    _seed_rec(sess, id="rec-hs2")
    sess.close()

    r = client.post("/api/agents/recommendations/rec-hs2/open-hubspot")
    assert r.status_code == 200
    assert r.json()["deep_link"] is None


# ─── Block time ───────────────────────────────────────────────────────────────


def test_block_time_creates_calendar_event_and_records_feedback(client, db_session):
    """The /block-time endpoint creates a real Google Calendar event via
    GoogleCalendarClient. Mock the client so the test doesn't need DWD creds,
    then verify the feedback row carries the event id."""
    sess = db_session()
    _seed_rec(sess, id="rec-block")
    sess.close()

    fake_event = {
        "id": "evt-abc",
        "htmlLink": "https://calendar.google.com/event?eid=evt-abc",
    }
    with patch("rubik_core.tools.calendar.GoogleCalendarClient") as Mock:
        Mock.return_value.create_event.return_value = fake_event
        r = client.post(
            "/api/agents/recommendations/rec-block/block-time",
            json={"duration_min": 30},
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["event_id"] == "evt-abc"
    assert body["html_link"] == "https://calendar.google.com/event?eid=evt-abc"

    sess = db_session()
    rec = sess.query(AgentRecommendation).filter_by(id="rec-block").one()
    assert rec.action_type == "meeting_blocked"
    assert rec.acted_on_at is not None
    fb = sess.query(AgentRecommendationFeedback).all()
    assert fb[0].action_metadata["event_id"] == "evt-abc"
    sess.close()


def test_block_time_surfaces_calendar_errors_as_502(client, db_session):
    sess = db_session()
    _seed_rec(sess, id="rec-fail")
    sess.close()
    with patch("rubik_core.tools.calendar.GoogleCalendarClient") as Mock:
        Mock.return_value.create_event.return_value = {"error": "rate limit hit"}
        r = client.post(
            "/api/agents/recommendations/rec-fail/block-time", json={"duration_min": 30}
        )
    assert r.status_code == 502


# ─── Authorization ─────────────────────────────────────────────────────────────


def test_cannot_act_on_someone_elses_recommendation(client, db_session):
    """Scoped to the row's owner — rep1 (logged in) cannot snooze rep2's row."""
    sess = db_session()
    _seed_rec(sess, id="rec-other", user_email="rep2@acme.example")
    sess.close()
    r = client.post("/api/agents/recommendations/rec-other/snooze", json={"days": 3})
    assert r.status_code == 403


def test_action_on_missing_rec_returns_404(client):
    r = client.post("/api/agents/recommendations/nope/snooze", json={"days": 3})
    assert r.status_code == 404


# ─── Duplicate-guard treats acted-on as handled ───────────────────────────────


def test_duplicate_guard_skips_acted_on_recs_indefinitely(db_session):
    """Once a rep has emailed/blocked/etc on a (lead, type), the agent must
    not re-emit the same finding — even after the 30-day duplicate horizon.
    This is the hard-signal feedback loop."""
    from backend.agents.base import Recommendation, is_duplicate

    sess = db_session()
    # Existing acted-on row from 60 days ago — older than the duplicate horizon.
    _seed_rec(
        sess,
        id="rec-old",
        created_at=datetime.utcnow() - timedelta(days=60),
        acted_on_at=datetime.utcnow() - timedelta(days=58),
        action_type="email_sent",
    )

    candidate = Recommendation(
        user_email="rep1@acme.example",
        type="stale_lead",
        entity_id="deal-1",
        entity_name="ABC Corp",
        text="(re-emit attempt)",
        evidence={},
        confidence=0.8,
    )
    assert is_duplicate(sess, candidate) is True
    sess.close()


def test_duplicate_guard_emits_when_no_prior_action(db_session):
    """Sanity: a fresh (user, type, lead) with no prior row passes the guard."""
    from backend.agents.base import Recommendation, is_duplicate

    sess = db_session()
    candidate = Recommendation(
        user_email="rep1@acme.example",
        type="stale_lead",
        entity_id="deal-NEW",
        entity_name="New Co",
        text="(first emission)",
        evidence={},
        confidence=0.8,
    )
    assert is_duplicate(sess, candidate) is False
    sess.close()

"""Meeting Prep is a first-class WGLL feedback surface (good/meh/bad).

Regression for adding ``meeting_prep`` to ``FEEDBACK_SURFACES``: the API accepts
a rating on the meeting-prep surface (it used to 422 on an unknown surface),
``/mine`` restores it keyed by the calendar event id, re-rating upserts rather
than appends, and the WGLL prompt builder emits a meeting-prep-labelled block
that ``generate_meeting_prep_analysis`` injects.

Uses an in-memory SQLite DB so it never touches deployed Cloud SQL
(per the project's known test_pollution_bug).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth import get_current_user
from backend.main import app
from rubik_core.auth.registry import UserProfile
from rubik_core.database.db import get_db
from rubik_core.database.models import FEEDBACK_SURFACES, Base, User

# ─── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def db_session():
    """Per-test in-memory SQLite shared across sessions via StaticPool."""
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
        db.commit()
    finally:
        db.close()
    yield Session
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client(db_session):
    """TestClient acting as rep1 with the DB overridden to in-memory."""
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


# ─── Tests ────────────────────────────────────────────────────────────────────


def test_meeting_prep_is_a_registered_surface():
    assert "meeting_prep" in FEEDBACK_SURFACES


def test_rate_meeting_prep_briefing_persists_and_reloads(client):
    """A good/meh/bad rating on a briefing is accepted and /mine restores it,
    keyed by the calendar event id the page rates on."""
    event_id = "evt-abc-123"
    resp = client.post(
        "/api/feedback",
        json={
            "surface": "meeting_prep",
            "proposal_id": event_id,
            "proposal_text": "Battle brief for ACME renewal",
            "rating": "useful",
            "comment": "Nailed the objections section",
            "proposal_metadata": {"origin": "meeting_prep", "is_internal": False},
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["surface"] == "meeting_prep"
    assert body["rating"] == "useful"

    mine = client.get("/api/feedback/mine", params={"surface": "meeting_prep"})
    assert mine.status_code == 200
    data = mine.json()
    assert data[event_id]["rating"] == "useful"
    assert data[event_id]["comment"] == "Nailed the objections section"


def test_rerating_meeting_prep_upserts_not_appends(client):
    """Clicking a different face on the same briefing updates the one settled
    row — the latest rating wins."""
    event_id = "evt-xyz-789"
    for rating in ("meh", "not_useful"):
        r = client.post(
            "/api/feedback",
            json={
                "surface": "meeting_prep",
                "proposal_id": event_id,
                "proposal_text": "Prep",
                "rating": rating,
            },
        )
        assert r.status_code == 200, r.text

    mine = client.get("/api/feedback/mine", params={"surface": "meeting_prep"}).json()
    assert mine[event_id]["rating"] == "not_useful"


def test_wgll_block_uses_rating_counts_without_old_briefing_text(db_session):
    """Feedback reaches the prompt without replaying old CRM or comment text."""
    from rubik_core.feedback import store
    from rubik_core.feedback.wgll import build_wgll_context

    sess = db_session()
    try:
        store.upsert_feedback(
            sess,
            user_email="rep1@acme.example",
            surface="meeting_prep",
            proposal_id="evt-1",
            proposal_text="A crisp, well-grounded battle brief",
            rating="useful",
            comment="loved the discovery questions",
        )
        block = build_wgll_context(
            sess, surface="meeting_prep", user_email="rep1@acme.example"
        )
        assert "meeting briefings" in block
        assert "1 were rated useful" in block
        assert "battle brief" not in block.lower()
        assert "discovery questions" not in block.lower()

        # Per-user scoping: another user's rating must not leak in, and
        # a user with no ratings gets an empty block.
        other = build_wgll_context(
            sess, surface="meeting_prep", user_email="stranger@acme.example"
        )
        assert other == ""
    finally:
        sess.close()

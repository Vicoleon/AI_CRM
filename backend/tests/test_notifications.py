"""V2 Notifications — smoke test for the polling-transport endpoints.

Covers the round-trip the v2 TopBar bell relies on:

1. A notification row inserted into the DB shows up in
   ``GET /api/notifications``.
2. ``POST /api/notifications/{id}/read`` marks it read.
3. A subsequent ``GET /api/notifications?unread=true`` returns empty.
4. ``POST /api/notifications/read-all`` clears the remaining unread set.
5. Cross-user isolation: a row owned by ``rep2`` is invisible to ``rep1``
   and cannot be marked read by ``rep1`` (returns 404, no leak).

Tests use an in-memory SQLite DB to avoid touching deployed Cloud SQL
(per the project's known test_pollution_bug — module-level SessionLocal
would otherwise read .env's DATABASE_URL).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth import get_current_user
from backend.main import app
from rubik_core.auth.registry import UserProfile
from rubik_core.database.db import get_db
from rubik_core.database.models import Base, Notification, User

# ─── Fixtures ─────────────────────────────────────────────────────────────────


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


def _seed_notification(db, **overrides) -> Notification:
    defaults = dict(
        id="notif-1",
        actor_email="rep1@acme.example",
        kind="agent_recommendation",
        title="Lead idle 23 days",
        body="ABC Corp hasn't been touched in 23 days.",
        severity="warn",
        link="/v2/leads/abc-corp",
        created_at=datetime.utcnow(),
        read_at=None,
    )
    defaults.update(overrides)
    n = Notification(**defaults)
    db.add(n)
    db.commit()
    return n


# ─── Smoke: list, read, list-unread, read-all ─────────────────────────────────


def test_list_returns_notification_for_owner(client, db_session):
    """A row owned by the caller is returned by GET /api/notifications."""
    sess = db_session()
    _seed_notification(sess, id="notif-1")
    sess.close()

    r = client.get("/api/notifications/")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) == 1
    assert body[0]["id"] == "notif-1"
    assert body[0]["kind"] == "agent_recommendation"
    assert body[0]["severity"] == "warn"
    assert body[0]["title"] == "Lead idle 23 days"
    assert body[0]["link"] == "/v2/leads/abc-corp"
    assert body[0]["read_at"] is None


def test_post_read_marks_notification_as_read(client, db_session):
    """POST /{id}/read stamps read_at; subsequent unread=true returns empty."""
    sess = db_session()
    _seed_notification(sess, id="notif-read")
    sess.close()

    # Sanity: unread=true sees it.
    r = client.get("/api/notifications/?unread=true")
    assert r.status_code == 200
    assert [n["id"] for n in r.json()] == ["notif-read"]

    # Mark read.
    r = client.post("/api/notifications/notif-read/read")
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ok"}

    # unread=true now empty.
    r = client.get("/api/notifications/?unread=true")
    assert r.status_code == 200
    assert r.json() == []

    # And the row's read_at is stamped.
    sess = db_session()
    row = sess.query(Notification).filter_by(id="notif-read").one()
    assert row.read_at is not None
    sess.close()


def test_default_unread_false_returns_read_and_unread(client, db_session):
    """Without ?unread=true, both read and unread rows are returned."""
    sess = db_session()
    _seed_notification(sess, id="notif-unread")
    _seed_notification(
        sess,
        id="notif-already-read",
        title="Old read row",
        body="Already seen.",
        read_at=datetime.utcnow() - timedelta(hours=1),
    )
    sess.close()

    r = client.get("/api/notifications/")
    assert r.status_code == 200
    ids = {n["id"] for n in r.json()}
    assert ids == {"notif-unread", "notif-already-read"}


def test_list_orders_newest_first(client, db_session):
    """Order is created_at DESC — newest first."""
    sess = db_session()
    older = datetime.utcnow() - timedelta(hours=2)
    newer = datetime.utcnow() - timedelta(minutes=5)
    _seed_notification(sess, id="notif-old", created_at=older, title="Old")
    _seed_notification(sess, id="notif-new", created_at=newer, title="New")
    sess.close()

    r = client.get("/api/notifications/")
    assert r.status_code == 200
    ids = [n["id"] for n in r.json()]
    assert ids == ["notif-new", "notif-old"]


def test_limit_caps_returned_rows(client, db_session):
    """?limit=N caps the response; max enforced at 100."""
    sess = db_session()
    base = datetime.utcnow()
    for i in range(5):
        _seed_notification(
            sess,
            id=f"notif-{i}",
            title=f"Row {i}",
            created_at=base - timedelta(minutes=i),
        )
    sess.close()

    r = client.get("/api/notifications/?limit=2")
    assert r.status_code == 200
    assert len(r.json()) == 2

    # 422 when above the max=100 ceiling
    r = client.get("/api/notifications/?limit=500")
    assert r.status_code == 422


def test_read_all_clears_remaining_unread(client, db_session):
    """POST /read-all marks every unread row for the caller and returns count."""
    sess = db_session()
    _seed_notification(sess, id="n-a")
    _seed_notification(sess, id="n-b", title="B", body="b")
    # An already-read row should not be re-counted.
    _seed_notification(
        sess,
        id="n-already",
        title="already",
        body="already",
        read_at=datetime.utcnow() - timedelta(minutes=10),
    )
    sess.close()

    r = client.post("/api/notifications/read-all")
    assert r.status_code == 200, r.text
    assert r.json() == {"updated": 2}

    # No more unread.
    r = client.get("/api/notifications/?unread=true")
    assert r.status_code == 200
    assert r.json() == []


# ─── Cross-user isolation ─────────────────────────────────────────────────────


def test_list_scopes_to_caller_only(client, db_session):
    """rep1 must not see rep2's notifications."""
    sess = db_session()
    _seed_notification(sess, id="mine", actor_email="rep1@acme.example")
    _seed_notification(
        sess,
        id="theirs",
        actor_email="rep2@acme.example",
        title="Not yours",
        body="Not yours",
    )
    sess.close()

    r = client.get("/api/notifications/")
    assert r.status_code == 200
    ids = {n["id"] for n in r.json()}
    assert ids == {"mine"}


def test_read_someone_elses_returns_404(client, db_session):
    """Marking another user's row as read returns 404, not 403 — same
    response as a non-existent id so we don't leak existence."""
    sess = db_session()
    _seed_notification(
        sess,
        id="theirs",
        actor_email="rep2@acme.example",
        title="Not yours",
        body="Not yours",
    )
    sess.close()

    r = client.post("/api/notifications/theirs/read")
    assert r.status_code == 404

    # And the row remains unread for rep2.
    sess = db_session()
    row = sess.query(Notification).filter_by(id="theirs").one()
    assert row.read_at is None
    sess.close()


def test_read_missing_returns_404(client):
    r = client.post("/api/notifications/does-not-exist/read")
    assert r.status_code == 404


def test_read_all_scopes_to_caller(client, db_session):
    """read-all must not touch other users' rows."""
    sess = db_session()
    _seed_notification(sess, id="mine", actor_email="rep1@acme.example")
    _seed_notification(
        sess,
        id="theirs",
        actor_email="rep2@acme.example",
        title="Not yours",
        body="Not yours",
    )
    sess.close()

    r = client.post("/api/notifications/read-all")
    assert r.status_code == 200
    assert r.json() == {"updated": 1}

    sess = db_session()
    mine = sess.query(Notification).filter_by(id="mine").one()
    theirs = sess.query(Notification).filter_by(id="theirs").one()
    assert mine.read_at is not None
    assert theirs.read_at is None  # untouched
    sess.close()


# ─── Regression: a kind this schema has never heard of must not 500 ──────────


def test_list_returns_kinds_outside_the_original_four(client, db_session):
    """The feed survives kinds added by later features.

    ``kind`` used to be a ``Literal`` of the four V2_1 kinds while two live
    producers — scheduled task results and asset shares — already wrote rows
    with other values. On a RESPONSE model that is a 500 for the WHOLE feed,
    not a skipped row, so any user who had ever run a scheduled task could not
    load their notifications at all. Nothing read the endpoint, so nothing
    caught it. Keep this test whenever a new producer lands.
    """
    sess = db_session()
    _seed_notification(
        sess,
        id="sched-1",
        kind="scheduled_task_result",
        title="Monday Pipeline Review",
        body="Your scheduled task has a new result.",
        severity="info",
        link="/chat?session=abc-123",
    )
    _seed_notification(
        sess,
        id="asset-1",
        kind="asset_shared",
        title="Q3 rollup shared with you",
        body="Indigo shared a document.",
        severity="info",
        link="/assets/doc-1",
    )
    sess.close()

    r = client.get("/api/notifications/")
    assert r.status_code == 200, r.text
    kinds = {row["kind"] for row in r.json()}
    assert {"scheduled_task_result", "asset_shared"} <= kinds


def test_muting_a_later_kind_still_works(client, db_session):
    """Mute is a plain string match, so it covers kinds added after V2_1."""
    from rubik_core.database.models import UserPreference

    sess = db_session()
    _seed_notification(
        sess,
        id="sched-1",
        kind="scheduled_task_result",
        title="Monday Pipeline Review",
        body="Your scheduled task has a new result.",
        link="/chat?session=abc-123",
    )
    sess.add(
        UserPreference(
            user_email="rep1@acme.example",
            muted_notification_kinds=["scheduled_task_result"],
        )
    )
    sess.commit()
    sess.close()

    r = client.get("/api/notifications/")
    assert r.status_code == 200, r.text
    assert r.json() == []

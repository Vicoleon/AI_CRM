"""The past-meetings diary.

Covers:
- `GET /api/eos/l10/sessions?finished_only=true&limit=N` filters out
  active sessions and orders newest-first.
- `PATCH /api/eos/l10/sessions/{id}` succeeds for the meeting's
  manager (and admin), and 403s for everyone else.
- PATCH ignores attempts to rewrite history (meeting_id / actual_start
  / actual_end stay locked once the session is logged).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from rubik_core.auth.registry import UserRegistry
from rubik_core.config import settings
from rubik_core.database.schemas import UserProfile

client = TestClient(app)


def _profile(email: str, roles: list[str] | None = None) -> UserProfile:
    now = "2026-05-21T00:00:00"
    stored = UserRegistry().get_user(email)
    return UserProfile(
        id=stored.id if stored else email.split("@")[0],
        email=email,
        name=stored.name if stored else email.split("@")[0].title(),
        roles=roles or (stored.roles if stored else ["user"]),
        department=stored.department if stored else "general",
        created_at=now,
        updated_at=now,
        is_exec=bool(stored.is_exec) if stored else False,
    )


def _as_user(email: str, roles: list[str] | None = None):
    def _factory():
        return _profile(email, roles)

    app.dependency_overrides[get_current_user] = _factory


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db_path = tmp_path / "test_ft3.db"
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    import rubik_core.database.db as dbmod

    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database import eos  # noqa: F401
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)

    reg = UserRegistry()
    for email, roles in [
        ("admin@example.com", ["admin"]),
        ("manager@example.com", ["user"]),
        ("teammate@example.com", ["user"]),
    ]:
        if not reg.get_user(email):
            reg.register_user(
                email=email, name=email.split("@")[0], id=email, roles=roles
            )
    yield
    app.dependency_overrides.clear()


def _seed_meeting_with_sessions(
    manager_email: str = "manager@example.com",
) -> tuple[str, list[str]]:
    """Seed a meeting whose manager is the Person with the given email,
    plus three sessions:

    - one still active (no actual_end),
    - one finished 2 hours ago,
    - one finished 8 days ago.

    Returns (meeting_id, [finished_session_ids_newest_first]).
    """
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import L10Meeting, L10Session, Person

    db = SessionLocal()
    try:
        manager = Person(
            first_name="Mgr",
            last_name="One",
            email=manager_email,
            created_by="admin@example.com",
        )
        db.add(manager)
        db.commit()
        db.refresh(manager)

        meeting = L10Meeting(
            name=f"FT-3 meeting {datetime.utcnow().isoformat()}",
            duration_minutes=90,
            manager_id=manager.id,
            created_by="admin@example.com",
        )
        db.add(meeting)
        db.commit()
        db.refresh(meeting)

        now = datetime.utcnow()
        active = L10Session(
            meeting_id=meeting.id,
            actual_start=now - timedelta(minutes=10),
            actual_end=None,
            notes="Still running",
            created_by="manager@example.com",
        )
        recent = L10Session(
            meeting_id=meeting.id,
            actual_start=now - timedelta(hours=3),
            actual_end=now - timedelta(hours=2),
            average_rating=8,
            notes="Recent finished session",
            created_by="manager@example.com",
        )
        older = L10Session(
            meeting_id=meeting.id,
            actual_start=now - timedelta(days=8, hours=1),
            actual_end=now - timedelta(days=8),
            average_rating=6,
            notes="Older finished session",
            created_by="manager@example.com",
        )
        db.add_all([active, recent, older])
        db.commit()
        db.refresh(active)
        db.refresh(recent)
        db.refresh(older)
        return str(meeting.id), [str(recent.id), str(older.id)]
    finally:
        db.close()


# -- finished_only filter --------------------------------------------


def test_list_sessions_finished_only_excludes_active():
    meeting_id, finished_ids = _seed_meeting_with_sessions()
    _as_user("teammate@example.com")

    r = client.get(
        "/api/eos/l10/sessions",
        params={"meeting_id": meeting_id, "finished_only": True},
    )
    assert r.status_code == 200
    rows = r.json()
    returned_ids = [row["id"] for row in rows]
    # Active session is filtered out.
    assert set(returned_ids) == set(finished_ids)
    # Newest-first ordering.
    assert returned_ids == finished_ids


def test_list_sessions_finished_only_respects_limit():
    meeting_id, finished_ids = _seed_meeting_with_sessions()
    _as_user("teammate@example.com")

    r = client.get(
        "/api/eos/l10/sessions",
        params={"meeting_id": meeting_id, "finished_only": True, "limit": 1},
    )
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    # Limit + newest-first ordering = the most recent finished session.
    assert rows[0]["id"] == finished_ids[0]


def test_list_sessions_without_finished_only_returns_active_too():
    meeting_id, _ = _seed_meeting_with_sessions()
    _as_user("teammate@example.com")

    r = client.get("/api/eos/l10/sessions", params={"meeting_id": meeting_id})
    assert r.status_code == 200
    rows = r.json()
    # All three sessions (one active + two finished).
    assert len(rows) == 3


# -- PATCH authorization ---------------------------------------------


def test_patch_session_notes_succeeds_for_manager():
    meeting_id, finished_ids = _seed_meeting_with_sessions("manager@example.com")
    session_id = finished_ids[0]
    _as_user("manager@example.com")

    r = client.patch(
        f"/api/eos/l10/sessions/{session_id}",
        json={
            "meeting_id": meeting_id,
            "actual_start": "2026-05-15T09:00:00",
            "notes": "Edited after the fact for clarity",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["notes"] == "Edited after the fact for clarity"


def test_patch_session_notes_succeeds_for_admin():
    meeting_id, finished_ids = _seed_meeting_with_sessions("manager@example.com")
    session_id = finished_ids[0]
    _as_user("admin@example.com", roles=["admin"])

    r = client.patch(
        f"/api/eos/l10/sessions/{session_id}",
        json={
            "meeting_id": meeting_id,
            "actual_start": "2026-05-15T09:00:00",
            "notes": "Admin override",
        },
    )
    assert r.status_code == 200, r.text


def test_patch_session_notes_403_for_other_team_member():
    meeting_id, finished_ids = _seed_meeting_with_sessions("manager@example.com")
    session_id = finished_ids[0]
    # `teammate@example.com` is not the manager and not an admin.
    _as_user("teammate@example.com")

    r = client.patch(
        f"/api/eos/l10/sessions/{session_id}",
        json={
            "meeting_id": meeting_id,
            "actual_start": "2026-05-15T09:00:00",
            "notes": "I shouldn't be able to do this",
        },
    )
    assert r.status_code == 403


# -- PATCH locks history ----------------------------------------------


def test_patch_session_does_not_rewrite_meeting_id_or_start_time():
    """Even if the caller sends a different meeting_id / actual_start
    in the body, the server must keep the original session's lineage
    intact — only ``notes`` and ``average_rating`` are mutable."""
    meeting_id, finished_ids = _seed_meeting_with_sessions("manager@example.com")
    session_id = finished_ids[0]
    _as_user("manager@example.com")

    # Send bogus values for the locked fields.
    bogus_meeting_id = "00000000-0000-0000-0000-000000000000"
    bogus_start = "1999-01-01T00:00:00"
    r = client.patch(
        f"/api/eos/l10/sessions/{session_id}",
        json={
            "meeting_id": bogus_meeting_id,
            "actual_start": bogus_start,
            "notes": "Just updating notes",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # Notes WAS updated.
    assert body["notes"] == "Just updating notes"
    # But the lineage stayed pinned to the original.
    assert body["meeting_id"] == meeting_id
    assert body["actual_start"] != bogus_start

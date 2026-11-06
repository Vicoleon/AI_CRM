"""Tests for pre-meeting L10 inputs (headlines + ideas)."""

from __future__ import annotations

from datetime import datetime

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
    db_path = tmp_path / "test_l10_inputs.db"
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    import rubik_core.database.db as dbmod

    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database import eos  # noqa: F401
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)

    reg = UserRegistry()
    if not reg.get_user("admin@example.com"):
        reg.register_user(
            email="admin@example.com",
            name="Admin",
            id="admin@example.com",
            roles=["admin"],
        )
    yield
    app.dependency_overrides.clear()


def _seed_meeting() -> str:
    """Seed a single L10Meeting and return its id (string).

    Unique name per call to stay resilient against the shared-prod-db
    isolation gap in fresh_db (the same pattern as the check-in and rock-update tests)."""
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import L10Meeting

    db = SessionLocal()
    try:
        meeting = L10Meeting(
            name=f"FT-2 prep meeting {datetime.utcnow().isoformat()}",
            duration_minutes=90,
            created_by="admin@example.com",
        )
        db.add(meeting)
        db.commit()
        db.refresh(meeting)
        return str(meeting.id)
    finally:
        db.close()


# -- Create ------------------------------------------------------------


def test_create_headline_and_idea_round_trip():
    meeting_id = _seed_meeting()
    _as_user("admin@example.com", roles=["admin"])

    h = client.post(
        f"/api/eos/l10/meetings/{meeting_id}/inputs",
        json={
            "meeting_id": meeting_id,
            "kind": "headline",
            "text": "Acme renewed for 3 years",
            "discussed": False,
        },
    )
    assert h.status_code == 201, h.text
    hb = h.json()
    assert hb["kind"] == "headline"
    assert hb["text"] == "Acme renewed for 3 years"
    assert hb["discussed"] is False

    i = client.post(
        f"/api/eos/l10/meetings/{meeting_id}/inputs",
        json={
            "meeting_id": meeting_id,
            "kind": "idea",
            "text": "Notify Slack on off-track rocks?",
            "discussed": False,
        },
    )
    assert i.status_code == 201
    assert i.json()["kind"] == "idea"


def test_create_rejects_empty_text():
    meeting_id = _seed_meeting()
    _as_user("admin@example.com", roles=["admin"])
    r = client.post(
        f"/api/eos/l10/meetings/{meeting_id}/inputs",
        json={
            "meeting_id": meeting_id,
            "kind": "headline",
            "text": "   ",
            "discussed": False,
        },
    )
    assert r.status_code == 422


def test_create_rejects_mismatched_meeting_id():
    meeting_id = _seed_meeting()
    other_id = _seed_meeting()
    _as_user("admin@example.com", roles=["admin"])
    r = client.post(
        f"/api/eos/l10/meetings/{meeting_id}/inputs",
        json={
            "meeting_id": other_id,
            "kind": "headline",
            "text": "Will this get filed against the wrong meeting?",
            "discussed": False,
        },
    )
    assert r.status_code == 400


def test_create_rejects_unknown_meeting():
    _as_user("admin@example.com", roles=["admin"])
    bogus = "00000000-0000-0000-0000-000000000000"
    r = client.post(
        f"/api/eos/l10/meetings/{bogus}/inputs",
        json={
            "meeting_id": bogus,
            "kind": "headline",
            "text": "test",
            "discussed": False,
        },
    )
    assert r.status_code == 404


# -- List --------------------------------------------------------------


def test_list_filters_by_kind():
    meeting_id = _seed_meeting()
    _as_user("admin@example.com", roles=["admin"])

    for kind, text in [
        ("headline", "h1"),
        ("headline", "h2"),
        ("idea", "i1"),
    ]:
        client.post(
            f"/api/eos/l10/meetings/{meeting_id}/inputs",
            json={
                "meeting_id": meeting_id,
                "kind": kind,
                "text": text,
                "discussed": False,
            },
        )

    all_rows = client.get(f"/api/eos/l10/meetings/{meeting_id}/inputs").json()
    assert len(all_rows) == 3

    only_headlines = client.get(
        f"/api/eos/l10/meetings/{meeting_id}/inputs?kind=headline"
    ).json()
    assert {r["text"] for r in only_headlines} == {"h1", "h2"}

    only_ideas = client.get(
        f"/api/eos/l10/meetings/{meeting_id}/inputs?kind=idea"
    ).json()
    assert {r["text"] for r in only_ideas} == {"i1"}


def test_list_hides_discussed_when_requested():
    meeting_id = _seed_meeting()
    _as_user("admin@example.com", roles=["admin"])

    posted = client.post(
        f"/api/eos/l10/meetings/{meeting_id}/inputs",
        json={
            "meeting_id": meeting_id,
            "kind": "headline",
            "text": "Already covered",
            "discussed": False,
        },
    ).json()
    client.patch(f"/api/eos/l10/inputs/{posted['id']}", json={"discussed": True})

    visible = client.get(
        f"/api/eos/l10/meetings/{meeting_id}/inputs?include_discussed=false"
    ).json()
    assert visible == []

    all_rows = client.get(
        f"/api/eos/l10/meetings/{meeting_id}/inputs?include_discussed=true"
    ).json()
    assert len(all_rows) == 1


# -- Patch + delete ----------------------------------------------------


def test_patch_discussed_flip():
    meeting_id = _seed_meeting()
    _as_user("admin@example.com", roles=["admin"])

    posted = client.post(
        f"/api/eos/l10/meetings/{meeting_id}/inputs",
        json={
            "meeting_id": meeting_id,
            "kind": "idea",
            "text": "Idea to flip",
            "discussed": False,
        },
    ).json()

    r = client.patch(f"/api/eos/l10/inputs/{posted['id']}", json={"discussed": True})
    assert r.status_code == 200
    assert r.json()["discussed"] is True


def test_delete_soft_deletes():
    meeting_id = _seed_meeting()
    _as_user("admin@example.com", roles=["admin"])

    posted = client.post(
        f"/api/eos/l10/meetings/{meeting_id}/inputs",
        json={
            "meeting_id": meeting_id,
            "kind": "headline",
            "text": "Delete me",
            "discussed": False,
        },
    ).json()

    r = client.delete(f"/api/eos/l10/inputs/{posted['id']}")
    assert r.status_code == 204

    remaining = client.get(f"/api/eos/l10/meetings/{meeting_id}/inputs").json()
    assert remaining == []


# -- Headlines are part of the live change-marker ---------------------------
#
# ``HeadlinesPanel`` fetches ``/meetings/{id}/inputs`` once on mount and only
# mutates locally, so it needs the session poll to tell it when to look again.
# ``items_rev`` on ``GET /sessions/{id}/state`` is that signal — and it covered
# to-dos, issues, metrics and rocks but not inputs, which is why a teammate's
# headline never appeared during a live session.


def test_items_rev_moves_when_a_headline_is_added():
    from datetime import datetime

    meeting_id = _seed_meeting()
    _as_user("admin@example.com", roles=["admin"])

    started = client.post(
        "/api/eos/l10/sessions/start",
        json={"meeting_id": meeting_id, "actual_start": datetime.utcnow().isoformat()},
    )
    assert started.status_code == 200, started.text
    session_id = started.json()["id"]

    before = client.get(f"/api/eos/l10/sessions/{session_id}/state").json()["items_rev"]

    created = client.post(
        f"/api/eos/l10/meetings/{meeting_id}/inputs",
        json={
            "meeting_id": meeting_id,
            "kind": "headline",
            "text": "Acme renewed for 3 years",
            "discussed": False,
        },
    )
    assert created.status_code in (200, 201), created.text

    after = client.get(f"/api/eos/l10/sessions/{session_id}/state").json()["items_rev"]
    assert after != before, "adding a headline did not move items_rev"
    assert after is not None

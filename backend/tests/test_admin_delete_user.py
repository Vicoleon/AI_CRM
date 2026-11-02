"""Admin → Users → Delete: junk accounts go, people with history are offboarded.

What this file pins (``DELETE /api/users/{email}`` in ``backend/routers/users.py``):

1. **History refuses a plain delete.** An account with priorities cards or owned
   docs answers 409 with a sentence an admin can read, because a real person
   should be offboarded so a successor inherits their work.
2. **``force=true`` deletes it anyway.** That is the second, explicit yes the
   Admin page asks for. Without it, a leaked test account (boss@ or
   member@acme.example) keeps getting a priorities run every morning with no
   way to remove it from the UI.
3. **Every delete is audited**, and only admins may delete.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from rubik_core.database.schemas import UserProfile

JUNK = "boss@acme.example"
CLEAN = "member@acme.example"


def _profile(email: str, roles: list[str]) -> UserProfile:
    now = "2026-10-16T00:00:00"
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name=email.split("@")[0].title(),
        roles=roles,
        department="Systems",
        is_exec=False,
        managed_departments=[],
        created_at=now,
        updated_at=now,
    )


ADMIN = _profile("admin.person@acme.example", ["admin", "user"])
PLAIN = _profile("plain.person@acme.example", ["user"])


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """``SessionLocal`` on a throwaway sqlite file holding JUNK (two empty
    priorities cards, as the nightly run writes them) and CLEAN (no history)."""
    import rubik_core.database.db as dbmod
    from rubik_core.config import settings
    from rubik_core.database.models import Base, HomeCard, User

    monkeypatch.setattr(
        settings, "DATABASE_URL", f"sqlite:///{tmp_path / 'delete_user.db'}"
    )
    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)
    Base.metadata.create_all(bind=engine)
    with dbmod.SessionLocal() as db:
        for email in (JUNK, CLEAN):
            db.add(User(email=email, id=email.split("@")[0], name="Fixture"))
        for day in ("2026-10-15", "2026-10-16"):
            db.add(
                HomeCard(
                    id=f"card-{day}",
                    user_email=JUNK,
                    date=day,
                    category="daily_briefing",
                    content_md="No high-priority items surfaced today.",
                    created_at=datetime(2026, 10, 16),
                )
            )
        db.commit()
    try:
        yield dbmod.SessionLocal
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
        dbmod._engine = None


def _as(user: UserProfile) -> TestClient:
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def _user_exists(session_local, email: str) -> bool:
    from rubik_core.database.models import User

    with session_local() as db:
        return db.get(User, email) is not None


def _audit(session_local) -> list[tuple[str, str]]:
    from rubik_core.database.models import AuditLog

    with session_local() as db:
        return [
            (r.action, r.status)
            for r in db.query(AuditLog).filter(AuditLog.action == "USER_DELETE")
        ]


def test_an_account_with_history_refuses_a_plain_delete(fresh_db):
    res = _as(ADMIN).delete(f"/api/users/{JUNK}")

    assert res.status_code == 409
    detail = res.json()["detail"]
    assert "2 priorities card(s) and 0 owned doc(s)" in detail
    assert "offboard them instead" in detail
    assert "force=true" not in detail, "the admin reads this sentence verbatim"
    assert _user_exists(fresh_db, JUNK)
    assert _audit(fresh_db) == []


def test_force_deletes_it_anyway_and_audits_it(fresh_db):
    res = _as(ADMIN).delete(f"/api/users/{JUNK}", params={"force": "true"})

    assert res.status_code == 200, res.text
    assert not _user_exists(fresh_db, JUNK)
    assert _audit(fresh_db) == [("USER_DELETE", "success")]


def test_an_account_with_no_history_needs_no_force(fresh_db):
    res = _as(ADMIN).delete(f"/api/users/{CLEAN}")

    assert res.status_code == 200, res.text
    assert not _user_exists(fresh_db, CLEAN)


def test_only_admins_may_delete(fresh_db):
    res = _as(PLAIN).delete(f"/api/users/{CLEAN}", params={"force": "true"})

    assert res.status_code == 403
    assert _user_exists(fresh_db, CLEAN)


def test_an_unknown_account_is_a_404(fresh_db):
    res = _as(ADMIN).delete("/api/users/nobody@acme.example")

    assert res.status_code == 404

"""Owner-picker (`GET /api/eos/people`) cross-team exec eligibility.

An Exec-team member can assign a Rock / To-Do / IDS issue to ANY other
Exec-team member, across departments — so the picker must offer every
Exec-team member to an exec caller, even ones outside the caller's own /
managed teams. A non-exec still only sees their own department.

No mocks — a temp SQLite DB per test, endpoint-level through TestClient.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from rubik_core.config import settings
from rubik_core.database.schemas import UserProfile

client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db_path = tmp_path / "test_people_picker.db"
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    import rubik_core.database.db as dbmod

    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database import eos  # noqa: F401
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    yield
    app.dependency_overrides.clear()


def _profile(
    email: str,
    *,
    department: str = "general",
    is_exec: bool = False,
    managed: list[str] | None = None,
) -> UserProfile:
    now = "2026-07-16T00:00:00"
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name=email.split("@")[0].title(),
        roles=["user"],
        department=department,
        is_exec=is_exec,
        managed_departments=managed or [],
        created_at=now,
        updated_at=now,
    )


def _as(profile: UserProfile) -> None:
    app.dependency_overrides[get_current_user] = lambda: profile


def _seed_users() -> None:
    """Operations + Sales, with an exec and a non-exec in each, plus Support —
    a team whose leader sits in ANOTHER department (a Director of Operations
    whose `managed_departments` span several teams)."""
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.models import Department, User

    db = SessionLocal()
    try:
        db.add_all(
            [
                Department(name="Operations"),
                Department(name="Sales"),
                Department(name="Support"),
            ]
        )
        db.add_all(
            [
                User(
                    email="tatum@x.example",
                    id="tatum",
                    name="Tatum Mercer",
                    department="Operations",
                    is_exec=True,
                    managed_departments=["Operations", "Support"],
                ),
                User(
                    email="support@x.example",
                    id="support",
                    name="Bea Tester",
                    department="Support",
                    is_exec=False,
                ),
                # The CEO: org-wide READ via the "*" wildcard, which is NOT a
                # claim to lead every team.
                User(
                    email="ceo@x.example",
                    id="ceo",
                    name="Cee Eeoh",
                    department="general",
                    is_exec=True,
                    managed_departments=["*"],
                ),
                User(
                    email="marin@x.example",
                    id="marin",
                    name="Marin Zane",
                    department="Sales",
                    is_exec=True,
                ),
                User(
                    email="sam@x.example",
                    id="sam",
                    name="Sam Sales",
                    department="Sales",
                    is_exec=False,
                ),
                User(
                    email="ops@x.example",
                    id="ops",
                    name="Ops User",
                    department="Operations",
                    is_exec=False,
                ),
            ]
        )
        db.commit()
    finally:
        db.close()


def _emails(resp) -> set[str]:
    return {p["email"] for p in resp.json()}


def test_exec_picker_includes_cross_team_exec():
    _seed_users()
    # Tatum is an Exec-team member + Operations manager (NOT org-wide).
    _as(
        _profile(
            "tatum@x.example",
            department="Operations",
            is_exec=True,
            managed=["Operations"],
        )
    )
    r = client.get("/api/eos/people/")
    assert r.status_code == 200
    emails = _emails(r)
    assert "marin@x.example" in emails  # Sales exec, cross-team — the fix
    assert "tatum@x.example" in emails  # self
    assert "ops@x.example" in emails  # own-department teammate
    assert "sam@x.example" not in emails  # Sales non-exec, outside Tatum's scope


def test_non_exec_picker_excludes_cross_team_exec():
    _seed_users()
    _as(_profile("ops@x.example", department="Operations", is_exec=False))
    r = client.get("/api/eos/people/")
    assert r.status_code == 200
    emails = _emails(r)
    assert "ops@x.example" in emails  # own department
    assert "marin@x.example" not in emails  # a Sales exec is NOT exposed to a non-exec
    assert "sam@x.example" not in emails  # Sales non-exec, other department


def test_member_sees_the_cross_department_leader_of_their_team():
    """A plain team member sees whoever LEADS their team, even when that
    leader's own primary department is a different one.

    Matching on primary ``department`` alone made visibility one-directional —
    a leader saw *down* into the teams they manage, but the team never saw
    *up*. Tatum (Operations, manages Support) was therefore missing from every
    Support picker: Tatum's own team could not check Tatum into their L10, hand
    Tatum the meeting, or assign Tatum a Rock / To-Do / Issue.
    """
    _seed_users()
    _as(_profile("support@x.example", department="Support", is_exec=False))
    r = client.get("/api/eos/people/")
    assert r.status_code == 200
    emails = _emails(r)
    assert "support@x.example" in emails  # self
    assert "tatum@x.example" in emails  # leads Support from Operations — the fix
    # Seeing your leader does NOT open up the leader's other teams.
    assert "ops@x.example" not in emails
    assert "marin@x.example" not in emails  # a Sales exec who leads nothing here
    assert "sam@x.example" not in emails


def test_orgwide_wildcard_is_not_a_claim_to_lead_every_team():
    """``managed_departments == ["*"]`` (the CEO) is a read-everything grant,
    not leadership of every team — so the org-wide exec must not land in
    every department's owner picker."""
    _seed_users()
    _as(_profile("support@x.example", department="Support", is_exec=False))
    r = client.get("/api/eos/people/")
    assert r.status_code == 200
    assert "ceo@x.example" not in _emails(r)

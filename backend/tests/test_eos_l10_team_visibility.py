"""One team sees another team's IDS, and rocks vanish: regressions.

A department team joined their L10 and saw ANOTHER team's IDS list, while
their own rocks showed nowhere. Three distinct defects, each pinned here:

1. ``get_meeting_items`` never team-scoped ROCKS (To-Dos / Issues / Scorecard
   were team-scoped; rocks only got a quarter + non-exec filter), so every
   L10 aggregated every team's rocks — and resolved "the current quarter"
   purely from the stored ``is_current`` flag, which only self-heals on the
   quarters/dashboard read paths and can sit stale (or on a duplicate row)
   when a meeting opens first → empty Rock Review.
2. ``visible_rock_ids`` derived a DEPARTMENT rock's team from the OWNER'S
   SEAT only — it ignored the rock's own ``department_name`` tag —
   so a team's rocks were invisible to the whole team (owner included)
   whenever the owner's Person row had no seat. The "my rock vanished"
   loop is how the duplicate rocks got minted.
3. ``calendar-import`` stamped the meeting with the IMPORTER's department,
   so an admin / other-team manager importing a team's L10 mis-scoped the
   meeting — its IDS / Scorecard / To-Dos then aggregated the wrong team.

No mocks beyond the Google Calendar client — temp SQLite DB per test,
endpoint-level through TestClient.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from rubik_core.config import settings
from rubik_core.database.schemas import UserProfile

client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db_path = tmp_path / "test_l10_team_visibility.db"
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    import rubik_core.database.db as dbmod

    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database import eos  # noqa: F401  — register EOS tables
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    yield
    app.dependency_overrides.clear()


def _profile(
    email: str,
    *,
    department: str = "general",
    roles: list[str] | None = None,
    is_exec: bool = False,
) -> UserProfile:
    now = "2026-07-22T00:00:00"
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name=email.split("@")[0].title(),
        roles=roles or ["user"],
        department=department,
        is_exec=is_exec,
        managed_departments=[],
        created_at=now,
        updated_at=now,
    )


def _as(profile: UserProfile) -> None:
    app.dependency_overrides[get_current_user] = lambda: profile


def _session():
    from rubik_core.database.db import SessionLocal

    return SessionLocal()


def _seed(*, stale_current_flag: bool = False) -> dict:
    """Support + Systems departments. Support's rocks are department-TAGGED but owned
    by a SEATLESS person (the real-world shape: eos_people rows made by the
    people picker carry no seat). Systems has one tagged rock and one legacy
    untagged rock whose owner IS seated in Systems. One ET rock.

    ``stale_current_flag=True`` reproduces the stale-``is_current`` shape:
    the quarter bracketing today is NOT flagged; an already-finished quarter
    still is.
    """
    from rubik_core.database.eos import (
        Issue,
        L10Meeting,
        Organization,
        Person,
        Quarter,
        Rock,
        Seat,
        Visibility,
    )
    from rubik_core.database.models import Department

    db = _session()
    ids: dict = {}
    try:
        db.add_all([Department(name="Support"), Department(name="Systems")])
        org = Organization(name="Acme", created_by="seed")
        db.add(org)
        db.commit()
        db.refresh(org)

        now = datetime.now()
        live_q = Quarter(
            organization_id=org.id,
            name="Q3 2026",
            start_date=now - timedelta(days=20),
            end_date=now + timedelta(days=60),
            is_current=not stale_current_flag,
            created_by="seed",
        )
        old_q = Quarter(
            organization_id=org.id,
            name="Q2 2026",
            start_date=now - timedelta(days=120),
            end_date=now - timedelta(days=30),
            is_current=stale_current_flag,
            created_by="seed",
        )
        db.add_all([live_q, old_q])

        sys_seat = Seat(
            title="Systems Analyst", department_name="Systems", created_by="seed"
        )
        db.add(sys_seat)
        db.commit()
        db.refresh(sys_seat)

        # Support owner: NO seat — the shape that used to hide the whole team's rocks.
        emery = Person(
            first_name="Emery",
            last_name="Abbott",
            email="emery@x.example",
            created_by="seed",
        )
        sys_p = Person(
            first_name="Parker",
            last_name="Ingram",
            email="pq@x.example",
            seat_id=sys_seat.id,
            created_by="seed",
        )
        db.add_all([emery, sys_p])
        db.commit()
        for k, obj in (("live_q", live_q), ("emery", emery), ("sys_p", sys_p)):
            db.refresh(obj)
            ids[k] = obj.id

        support_meeting = L10Meeting(
            name="Support L10", department_name="Support", created_by="seed"
        )
        systems_meeting = L10Meeting(
            name="Systems L10", department_name="Systems", created_by="seed"
        )
        exec_meeting = L10Meeting(
            name="Exec L10", is_exec_meeting=True, created_by="seed"
        )
        db.add_all([support_meeting, systems_meeting, exec_meeting])

        db.add_all(
            [
                Rock(
                    description="support rock",
                    visibility=Visibility.DEPARTMENT,
                    department_name="Support",
                    owner_id=emery.id,
                    quarter_id=live_q.id,
                    created_by="emery@x.example",
                ),
                Rock(
                    description="systems rock",
                    visibility=Visibility.DEPARTMENT,
                    department_name="Systems",
                    owner_id=sys_p.id,
                    quarter_id=live_q.id,
                    created_by="seed",
                ),
                # Legacy row: predates the department tag, so it has none — its team
                # derives from the owner's seat (Systems).
                Rock(
                    description="legacy untagged systems rock",
                    visibility=Visibility.DEPARTMENT,
                    owner_id=sys_p.id,
                    quarter_id=live_q.id,
                    created_by="seed",
                ),
                Rock(
                    description="et rock",
                    is_exec=True,
                    visibility=Visibility.DEPARTMENT,
                    owner_id=sys_p.id,
                    quarter_id=live_q.id,
                    created_by="seed",
                ),
                # The IDS side of the reported bug: a Systems-tagged issue
                # must stay OUT of Support's meeting payload.
                Issue(
                    description="Systems Bulletin",
                    department_name="Systems",
                    owner_id=sys_p.id,
                    created_by="pq@x.example",
                ),
            ]
        )
        db.commit()

        for key, meeting in (
            ("support_meeting", support_meeting),
            ("systems_meeting", systems_meeting),
            ("exec_meeting", exec_meeting),
        ):
            db.refresh(meeting)
            ids[key] = meeting.id
        return ids
    finally:
        db.close()


def _items(meeting_id) -> dict:
    r = client.get(f"/api/eos/l10/meetings/{meeting_id}/items")
    assert r.status_code == 200, r.text
    return r.json()


# ---- 1. get_meeting_items: rocks are team-scoped ---------------------------


def test_department_meeting_rock_review_shows_only_that_teams_rocks():
    ids = _seed()
    # Each team's own member reads their own L10 — meetings are membership-
    # scoped now (an admin no longer sees another team's L10 at all), so this
    # asserts the in-meeting Rock Review is team-scoped from the member's seat.
    _as(_profile("supportie@x.example", department="Support"))
    support = _items(ids["support_meeting"])
    assert {r["description"] for r in support["rocks"]} == {"support rock"}
    # The reported leak: another team's issues must not ride into this IDS.
    assert {i["description"] for i in support["issues"]} == set()

    _as(_profile("sysie@x.example", department="Systems"))
    systems = _items(ids["systems_meeting"])
    assert {r["description"] for r in systems["rocks"]} == {
        "systems rock",
        "legacy untagged systems rock",  # owner-seat fallback for untagged rows
    }
    assert {i["description"] for i in systems["issues"]} == {"Systems Bulletin"}


def test_exec_meeting_rock_review_shows_only_et_rocks():
    ids = _seed()
    _as(_profile("eve@x.example", department="general", is_exec=True))
    items = _items(ids["exec_meeting"])
    assert {r["description"] for r in items["rocks"]} == {"et rock"}


def test_meeting_rocks_survive_stale_current_quarter_flag():
    """The quarter bracketing today isn't flagged ``is_current`` (the flag
    only self-heals on the quarters/dashboard reads) — the Rock Review must
    still find the live quarter's rocks by date."""
    ids = _seed(stale_current_flag=True)
    _as(_profile("supportie@x.example", department="Support"))
    support = _items(ids["support_meeting"])
    assert {r["description"] for r in support["rocks"]} == {"support rock"}


# ---- 2. visible_rock_ids: the rock's own team tag counts -------------------


def test_team_member_sees_department_tagged_rock_with_seatless_owner():
    _seed()
    _as(_profile("teammate@x.example", department="Support"))
    listed = {r["description"] for r in client.get("/api/eos/rocks/").json()}
    assert "support rock" in listed  # was invisible: owner has no seat
    # Other teams' DEPARTMENT rocks stay out of scope.
    assert "systems rock" not in listed
    assert "et rock" not in listed


def test_owner_always_sees_their_own_rock():
    """Owner in the 'general' catch-all (no usable department, no seat):
    the owner carve-out must still surface their own DEPARTMENT rock."""
    _seed()
    _as(_profile("emery@x.example", department="general"))
    listed = {r["description"] for r in client.get("/api/eos/rocks/").json()}
    assert "support rock" in listed


# ---- 3. calendar-import: department comes from the roster ------------------


class _FakeCalendarClient:
    """Stands in for GoogleCalendarClient inside the import endpoint."""

    event: dict = {}

    def __init__(self, *args, **kwargs):
        self.service = object()  # truthy → "calendar configured"

    def get_event(self, event_id):
        return dict(self.event, id=event_id)


def test_calendar_import_stamps_roster_majority_department(monkeypatch):
    from rubik_core.auth.registry import UserRegistry

    _seed()
    reg = UserRegistry()
    for email, dept in [
        ("b1@x.example", "Support"),
        ("b2@x.example", "Support"),
        ("b3@x.example", "Support"),
        ("sysmgr@x.example", "Systems"),
    ]:
        if not reg.get_user(email):
            reg.register_user(
                email=email,
                name=email.split("@")[0],
                id=email,
                roles=["user"],
                department=dept,
            )

    _FakeCalendarClient.event = {
        "summary": "Support L10",
        "start": {"dateTime": "2026-07-21T10:00:00-06:00"},
        "end": {"dateTime": "2026-07-21T11:30:00-06:00"},
        "attendees": [
            {"email": "b1@x.example"},
            {"email": "b2@x.example"},
            {"email": "b3@x.example"},
        ],
        "organizer": {"email": "sysmgr@x.example"},
    }
    monkeypatch.setattr(
        "rubik_core.tools.calendar.GoogleCalendarClient", _FakeCalendarClient
    )

    # A SYSTEMS manager imports Support's meeting — the meeting must be Support's,
    # not the importer's team (the mis-scope that leaked Systems' IDS).
    _as(_profile("sysmgr@x.example", department="Systems"))
    r = client.post(
        "/api/eos/l10/calendar-import", json={"google_event_id": "evt-support-1"}
    )
    assert r.status_code == 201, r.text
    assert r.json()["department_name"] == "Support"

"""L10 manager hand-off: the authority ladder, the OOO break-glass, and the admin console.

The lockout being fixed: only the current manager (or an admin) could re-point an
L10 — and admins can't ENUMERATE meetings they aren't in — so a manager who left
for vacation without handing over took their team's L10 with them.

What these tests pin down:

* the ladder in ``_authz.meeting_authority`` (manager / department leadership /
  admin), including that a Team Manager of a DIFFERENT department gets nothing;
* the break-glass rung: open only while the manager is verifiably unavailable,
  only onto the claimant themselves, and closed the moment the manager is back;
* that the Exec L10 stays exec-only throughout;
* the admin console lists every meeting's structure while exposing no content.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from rubik_core.config import settings
from rubik_core.database.schemas import UserProfile

client = TestClient(app)

DEPT = "Systems"
OTHER_DEPT = "Compliance"


def _profile(
    email: str,
    roles: list[str] | None = None,
    *,
    department: str = "general",
    is_exec: bool = False,
    managed: list[str] | None = None,
) -> UserProfile:
    now = "2026-08-12T00:00:00"
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name=email.split("@")[0].title(),
        roles=roles or ["user"],
        department=department,
        created_at=now,
        updated_at=now,
        is_exec=is_exec,
        managed_departments=managed or [],
    )


def _as_user(email: str, **kwargs):
    app.dependency_overrides[get_current_user] = lambda: _profile(email, **kwargs)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db_path = tmp_path / "test_l10_breakglass.db"
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


def _seed(
    *,
    manager_email: str = "boss@acme.example",
    manager_has_account: bool = True,
    department: str | None = DEPT,
    is_exec_meeting: bool = False,
    day_of_week: int = 1,  # Tuesday
    members: tuple[str, ...] = ("member@acme.example",),
) -> dict:
    """Seed departments, a manager + members with seats, and one L10 meeting."""
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import L10Meeting, Person, Seat
    from rubik_core.database.models import Department, User

    db = SessionLocal()
    ids: dict = {}
    try:
        for name in (DEPT, OTHER_DEPT):
            if not db.query(Department).filter(Department.name == name).first():
                db.add(Department(name=name))
        db.commit()

        # ``Seat.department_name`` is NOT NULL, so people always sit in a real
        # department even when the MEETING is the (department-less) Exec L10.
        seat = Seat(
            title=f"{department or DEPT} Seat",
            department_name=department or DEPT,
            created_by="seed",
        )
        db.add(seat)
        db.commit()

        def _person(email: str, with_account: bool) -> Person:
            p = Person(
                first_name=email.split("@")[0].title(),
                last_name="Test",
                email=email,
                seat_id=seat.id,
                created_by="seed",
            )
            db.add(p)
            db.commit()
            if with_account and not db.query(User).filter(User.email == email).first():
                db.add(
                    User(
                        email=email,
                        id=email,
                        name=email.split("@")[0].title(),
                        department=department or DEPT,
                    )
                )
                db.commit()
            return p

        manager = _person(manager_email, manager_has_account)
        ids["manager_id"] = str(manager.id)
        ids["manager_email"] = manager_email
        ids["member_ids"] = {}
        for m in members:
            ids["member_ids"][m] = str(_person(m, True).id)

        meeting = L10Meeting(
            name="Team L10",
            department_name=None if is_exec_meeting else department,
            manager_id=manager.id,
            day_of_week=day_of_week,
            start_time="09:00",
            is_exec_meeting=is_exec_meeting,
            created_by="seed",
        )
        db.add(meeting)
        db.commit()
        ids["meeting_id"] = str(meeting.id)
        return ids
    finally:
        db.close()


#: How far before "now" a PTO seed starts, so the window covers the caller's
#: calendar date as well as the process's.
#:
#: The endpoint asks whether the manager is out on the CALLER's local date —
#: `l10._local_today`, resolved from that user's timezone — while a test computes
#: dates from the process clock, which in CI is UTC. Between 5pm and midnight
#: Pacific those are different days; `_local_today`'s own docstring names exactly
#: that case ("at 5pm Pacific the UTC date is already tomorrow"). A window seeded
#: to start "today" in UTC has therefore not begun yet on the caller's calendar,
#: the manager reads as available, and the assertion fails.
#:
#: This is a test bug, not a product bug: using the caller's timezone is the
#: behaviour `_local_today` was written to provide. CI fails this way in the
#: evening, Pacific time, even on a DOCS-ONLY commit, which is what rules the
#: product code out.
#:
#: Starting a day earlier weakens nothing — every test using it asserts only that
#: the manager is unavailable *now*, and `out_until` is the end date.
PTO_SEED_LEAD = timedelta(days=1)


def _put_manager_on_pto(email: str, *, start: date, end: date) -> None:
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.models import OOOPeriod

    db = SessionLocal()
    try:
        db.add(
            OOOPeriod(
                user_email=email.lower(),
                start_date=start.isoformat(),
                end_date=end.isoformat(),
                return_date=(end + timedelta(days=1)).isoformat(),
                synced_at=datetime.utcnow(),
            )
        )
        db.commit()
    finally:
        db.close()


def _mark_present(email: str) -> None:
    """Give the manager a cached OOO row that does NOT cover now.

    Without any cached row at all, ``manager_availability`` falls back to a live
    calendar probe; seeding a far-future period proves the daily refresh has seen
    them, which is what keeps the test off the network.
    """
    _put_manager_on_pto(email, start=date(2099, 1, 1), end=date(2099, 1, 5))


def _transfer(meeting_id: str, new_manager_id: str | None, note: str = "test"):
    return client.post(
        f"/api/eos/l10/{meeting_id}/transfer-manager",
        json={"new_manager_id": new_manager_id, "note": note},
    )


# ── the standing ladder ──────────────────────────────────────────────────────


def test_current_manager_can_hand_off():
    ids = _seed()
    _as_user(ids["manager_email"], department=DEPT)
    target = ids["member_ids"]["member@acme.example"]

    r = _transfer(ids["meeting_id"], target)
    assert r.status_code == 200, r.text
    assert r.json()["manager_id"] == target


def test_team_manager_of_the_department_can_reassign():
    """The rung that fixes the lockout without an admin or a break-glass."""
    ids = _seed()
    _mark_present(ids["manager_email"])
    _as_user("lead@acme.example", department=DEPT, managed=[DEPT])
    target = ids["member_ids"]["member@acme.example"]

    r = _transfer(ids["meeting_id"], target)
    assert r.status_code == 200, r.text
    assert r.json()["manager_id"] == target


def test_team_manager_of_another_department_gets_nothing():
    """Leadership is scoped to the team you lead, not every team you can see."""
    ids = _seed()
    _mark_present(ids["manager_email"])
    _as_user("outsider@acme.example", department=OTHER_DEPT, managed=[OTHER_DEPT])

    r = _transfer(ids["meeting_id"], ids["member_ids"]["member@acme.example"])
    # No access to a Systems meeting at all → existence stays hidden.
    assert r.status_code == 404, r.text


def test_admin_can_reassign_without_being_on_the_team():
    ids = _seed()
    _mark_present(ids["manager_email"])
    _as_user("admin@acme.example", roles=["admin"], department="general")

    r = _transfer(ids["meeting_id"], ids["member_ids"]["member@acme.example"])
    assert r.status_code == 200, r.text


def test_leadership_cannot_push_a_meeting_into_another_team():
    """Authority over your own team isn't authority over the destination.

    Create already checked this; PATCH never did, and letting department
    leadership PATCH would otherwise have let a Team Manager move their L10 onto
    a team they have nothing to do with.
    """
    ids = _seed()
    _mark_present(ids["manager_email"])
    _as_user("lead@acme.example", department=DEPT, managed=[DEPT])

    r = client.patch(
        f"/api/eos/l10/{ids['meeting_id']}",
        json={"department_name": OTHER_DEPT},
    )
    assert r.status_code == 403, r.text
    assert "only schedule L10s for" in r.json()["detail"]


def test_leadership_can_still_rename_its_own_meeting():
    """The guard is scoped to the department move — normal edits still work."""
    ids = _seed()
    _mark_present(ids["manager_email"])
    _as_user("lead@acme.example", department=DEPT, managed=[DEPT])

    r = client.patch(
        f"/api/eos/l10/{ids['meeting_id']}",
        json={"name": "Renamed L10", "department_name": DEPT},
    )
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Renamed L10"


# ── break-glass ──────────────────────────────────────────────────────────────


def test_member_cannot_take_over_while_the_manager_is_available():
    """The whole point of "verifiably": no availability problem, no takeover."""
    ids = _seed()
    _mark_present(ids["manager_email"])
    me = ids["member_ids"]["member@acme.example"]
    _as_user("member@acme.example", department=DEPT)

    r = _transfer(ids["meeting_id"], me)
    assert r.status_code == 403
    assert "available" in r.json()["detail"].lower()


def test_member_can_take_over_when_the_manager_is_on_pto():
    ids = _seed()
    today = date.today()
    _put_manager_on_pto(
        ids["manager_email"],
        start=today - PTO_SEED_LEAD,
        end=today + timedelta(days=4),
    )
    me = ids["member_ids"]["member@acme.example"]
    _as_user("member@acme.example", department=DEPT)

    r = _transfer(ids["meeting_id"], me)
    assert r.status_code == 200, r.text
    assert r.json()["manager_id"] == me


def test_takeover_covers_the_next_occurrence_not_just_today():
    """Out on the day the L10 lands → hand off in advance, before it's missed."""
    ids = _seed(day_of_week=(date.today() + timedelta(days=3)).weekday())
    absence_start = date.today() + timedelta(days=2)
    _put_manager_on_pto(
        ids["manager_email"], start=absence_start, end=absence_start + timedelta(days=3)
    )
    me = ids["member_ids"]["member@acme.example"]
    _as_user("member@acme.example", department=DEPT)

    r = _transfer(ids["meeting_id"], me)
    assert r.status_code == 200, r.text


def test_member_cannot_appoint_a_third_party_via_break_glass():
    """Break-glass promotes the claimant; it is not a way to appoint others."""
    ids = _seed(members=("member@acme.example", "other@acme.example"))
    today = date.today()
    _put_manager_on_pto(
        ids["manager_email"],
        start=today - PTO_SEED_LEAD,
        end=today + timedelta(days=4),
    )
    _as_user("member@acme.example", department=DEPT)

    r = _transfer(ids["meeting_id"], ids["member_ids"]["other@acme.example"])
    assert r.status_code == 403
    assert "someone else" in r.json()["detail"].lower()


def test_member_can_take_over_an_offboarded_managers_meeting():
    """No users row = departed employee; their meeting can never be handed over."""
    ids = _seed(manager_email="gone@acme.example", manager_has_account=False)
    me = ids["member_ids"]["member@acme.example"]
    _as_user("member@acme.example", department=DEPT)

    r = _transfer(ids["meeting_id"], me)
    assert r.status_code == 200, r.text
    assert r.json()["manager_id"] == me


def test_availability_endpoint_explains_the_situation():
    ids = _seed()
    today = date.today()
    _put_manager_on_pto(
        ids["manager_email"],
        start=today - PTO_SEED_LEAD,
        end=today + timedelta(days=2),
    )
    _as_user("member@acme.example", department=DEPT)

    r = client.get(f"/api/eos/l10/{ids['meeting_id']}/manager-availability")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is False
    assert body["reason"] == "on_pto"
    assert body["can_take_over"] is True
    assert body["your_authority"] is None
    assert body["out_until"] == (today + timedelta(days=2)).isoformat()


def test_availability_reports_standing_authority_for_the_manager():
    ids = _seed()
    _mark_present(ids["manager_email"])
    _as_user(ids["manager_email"], department=DEPT)

    body = client.get(f"/api/eos/l10/{ids['meeting_id']}/manager-availability").json()
    assert body["your_authority"] == "manager"
    # Already able to edit outright — no break-glass affordance needed.
    assert body["can_take_over"] is False


# ── the Exec fence ───────────────────────────────────────────────────────────


def test_exec_l10_is_invisible_to_a_non_exec_member():
    ids = _seed(is_exec_meeting=True, department=None)
    today = date.today()
    _put_manager_on_pto(
        ids["manager_email"],
        start=today - PTO_SEED_LEAD,
        end=today + timedelta(days=4),
    )
    _as_user("member@acme.example", department=DEPT)

    r = _transfer(ids["meeting_id"], ids["member_ids"]["member@acme.example"])
    assert r.status_code == 404, r.text


def test_exec_team_member_can_reassign_the_exec_l10():
    ids = _seed(is_exec_meeting=True, department=None)
    _mark_present(ids["manager_email"])
    _as_user("exec@acme.example", department=DEPT, is_exec=True)

    r = _transfer(ids["meeting_id"], ids["member_ids"]["member@acme.example"])
    assert r.status_code == 200, r.text


# ── admin console ────────────────────────────────────────────────────────────


def test_admin_console_lists_meetings_the_admin_is_not_in():
    """The missing half of the fix: authority is useless without enumeration."""
    ids = _seed()
    _as_user("admin@acme.example", roles=["admin"], department="general")

    r = client.get("/api/eos/l10/admin/meetings")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1
    row = body["meetings"][0]
    assert row["id"] == ids["meeting_id"]
    assert row["manager_email"] == ids["manager_email"]
    assert row["department_name"] == DEPT

    # Structure only — no content, and no roster identities.
    assert set(row) == {
        "id",
        "name",
        "department_name",
        "is_exec_meeting",
        "day_of_week",
        "start_time",
        "duration_minutes",
        "manager_id",
        "manager_name",
        "manager_email",
        "attendee_count",
        "last_session_at",
    }


def test_admin_console_is_admin_only():
    _seed()
    _as_user("member@acme.example", department=DEPT, managed=[DEPT])
    assert client.get("/api/eos/l10/admin/meetings").status_code == 403


def test_normal_list_still_hides_other_teams_from_an_admin():
    """The content fence is untouched: the console is the ONLY new visibility."""
    _seed()
    _as_user("admin@acme.example", roles=["admin"], department="general")

    r = client.get("/api/eos/l10/")
    assert r.status_code == 200, r.text
    assert r.json() == []

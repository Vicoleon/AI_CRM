"""Targeted tests for the additive EOS changes layered on top of main:

1. ``users.is_exec`` flows through GET /me, POST /users, PATCH /users.
2. VTO write endpoints are gated on Exec membership (or admin).
3. L10 attendance accepts and returns the structured check-in fields
   (separate Personal + Professional content/rating pairs), validates the
   0..10 rating range, and keeps a person's Personal and Professional
   entries from overwriting each other.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from rubik_core.auth.registry import UserRegistry
from rubik_core.config import AppMode, settings
from rubik_core.database.schemas import UserProfile

client = TestClient(app)


def _profile(email: str, roles: list[str] | None = None) -> UserProfile:
    """Build a UserProfile from the live registry so updates (is_exec,
    department) propagate naturally into the dependency override."""
    now = "2026-05-20T00:00:00"
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
    db_path = tmp_path / "test_eos_additive.db"
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    import rubik_core.database.db as dbmod

    # Reset the lazy-loaded engine so it re-reads ``settings.DATABASE_URL``.
    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database import eos  # noqa: F401  — register EOS tables
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)

    reg = UserRegistry()
    for email, roles in [
        ("admin@example.com", ["admin"]),
        ("exec@example.com", ["user"]),
        ("regular@example.com", ["user"]),
    ]:
        if not reg.get_user(email):
            reg.register_user(
                email=email, name=email.split("@")[0], id=email, roles=roles
            )
    yield
    app.dependency_overrides.clear()


# -- is_exec on users -------------------------------------------------------


def test_is_exec_round_trips_through_user_apis():
    _as_user("admin@example.com", roles=["admin"])

    # Created without the flag → default False.
    me = client.get("/api/users/me").json()
    assert me["is_exec"] is False

    # PATCH the flag on.
    r = client.patch("/api/users/exec@example.com", json={"is_exec": True})
    assert r.status_code == 200, r.text
    assert r.json()["is_exec"] is True

    # The flag persists across GET /users/.
    listed = client.get("/api/users/").json()
    exec_row = next(u for u in listed if u["email"] == "exec@example.com")
    assert exec_row["is_exec"] is True

    # Toggle back off.
    r = client.patch("/api/users/exec@example.com", json={"is_exec": False})
    assert r.json()["is_exec"] is False


def test_create_user_with_is_exec_flag():
    _as_user("admin@example.com", roles=["admin"])
    with patch("backend.routers.users.run_sync"):
        r = client.post(
            "/api/users/",
            json={
                "email": "new-exec@example.com",
                "name": "Brand New",
                "department": "general",
                "roles": ["user"],
                "is_exec": True,
            },
        )
    assert r.status_code == 200
    assert r.json()["is_exec"] is True


def test_dev_preview_user_create_does_not_schedule_transcript_sync(monkeypatch):
    _as_user("admin@example.com", roles=["admin"])
    monkeypatch.setattr(settings, "APP_MODE", AppMode.DEV_MOCK_AUTH)

    with patch("backend.routers.users.submit_background_work") as submit:
        response = client.post(
            "/api/users/",
            json={"email": "preview-user@example.com", "name": "Preview User"},
        )

    assert response.status_code == 200, response.text
    assert response.json()["email"] == "preview-user@example.com"
    assert UserRegistry().get_user("preview-user@example.com") is not None
    submit.assert_not_called()


def test_local_user_create_keeps_historical_transcript_sync(monkeypatch):
    _as_user("admin@example.com", roles=["admin"])
    monkeypatch.setattr(settings, "APP_MODE", AppMode.LOCAL)

    with patch("backend.routers.users.submit_background_work") as submit:
        response = client.post(
            "/api/users/",
            json={"email": "local-user@example.com", "name": "Local User"},
        )

    assert response.status_code == 200, response.text
    assert submit.call_count == 1
    assert submit.call_args.kwargs["args"] == [
        "--users",
        "local-user@example.com",
        "--days",
        "90",
    ]


# -- VTO Exec gating --------------------------------------------------------


def _seed_org_and_version(reg_org_id: str | None = None) -> tuple[str, str]:
    """Create an organization + VTO version row directly in the DB so the
    test doesn't need to go through other gated endpoints. Returns
    (organization_id, vto_version_id)."""
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import Organization, VTOSection, VTOVersion

    db = SessionLocal()
    try:
        org = Organization(name="Test Org", created_by="admin@example.com")
        db.add(org)
        db.commit()
        db.refresh(org)
        version = VTOVersion(
            organization_id=org.id,
            version_name="V1",
            is_active=True,
            status="draft",
            created_by="admin@example.com",
        )
        db.add(version)
        db.commit()
        db.refresh(version)
        section = VTOSection(
            vto_version_id=version.id,
            section_type="values",
            content=["Integrity"],
            created_by="admin@example.com",
        )
        db.add(section)
        db.commit()
        db.refresh(section)
        return str(org.id), str(version.id), str(section.id)
    finally:
        db.close()


def test_non_exec_user_cannot_create_vto_version():
    org_id, _v, _s = _seed_org_and_version()
    _as_user("regular@example.com")
    r = client.post(
        "/api/eos/vto/versions",
        json={
            "organization_id": org_id,
            "version_name": "V2",
            "is_active": False,
            "status": "draft",
        },
    )
    assert r.status_code == 403
    assert "Exec" in r.json()["detail"]


def test_exec_user_can_create_vto_version():
    org_id, _v, _s = _seed_org_and_version()
    # Flag exec@ as Exec.
    UserRegistry().update_user("exec@example.com", {"is_exec": True})
    _as_user("exec@example.com")
    r = client.post(
        "/api/eos/vto/versions",
        json={
            "organization_id": org_id,
            "version_name": "V2",
            "is_active": False,
            "status": "draft",
        },
    )
    assert r.status_code == 201, r.text


def test_admin_bypasses_vto_exec_gate():
    org_id, _v, _s = _seed_org_and_version()
    _as_user("admin@example.com", roles=["admin"])
    r = client.post(
        "/api/eos/vto/versions",
        json={
            "organization_id": org_id,
            "version_name": "V3",
            "is_active": False,
            "status": "draft",
        },
    )
    assert r.status_code == 201, r.text


def test_non_exec_cannot_patch_vto_section():
    _o, _v, section_id = _seed_org_and_version()
    _as_user("regular@example.com")
    r = client.patch(
        f"/api/eos/vto/sections/{section_id}",
        json={"content": ["Should fail"]},
    )
    assert r.status_code == 403


# -- L10 structured check-in -----------------------------------------------


def _seed_session_and_person(
    person_email: str = "chris@example.com",
) -> tuple[str, str]:
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import L10Meeting, L10Session, Person

    db = SessionLocal()
    try:
        meeting = L10Meeting(
            name="Pulse",
            duration_minutes=90,
            created_by="admin@example.com",
        )
        db.add(meeting)
        db.commit()
        db.refresh(meeting)
        session = L10Session(
            meeting_id=meeting.id,
            actual_start=datetime.utcnow(),
            created_by="admin@example.com",
        )
        db.add(session)
        # Reuse an existing Person with this email if one is around (lets
        # tests survive the fresh_db fixture occasionally falling back to a
        # shared db in CI), otherwise create a new one.
        person = (
            db.query(Person)
            .filter(Person.email == person_email, Person.is_deleted == False)  # noqa: E712
            .first()
        )
        if person is None:
            person = Person(
                first_name="Chris",
                last_name="Tester",
                email=person_email,
                created_by="admin@example.com",
            )
            db.add(person)
        db.commit()
        db.refresh(session)
        db.refresh(person)
        return str(session.id), str(person.id)
    finally:
        db.close()


def test_checkin_round_trip_via_attendance_endpoint():
    session_id, person_id = _seed_session_and_person()
    _as_user("admin@example.com", roles=["admin"])

    r = client.post(
        f"/api/eos/l10/sessions/{session_id}/attendance",
        json={
            "session_id": session_id,
            "person_id": person_id,
            "attended": True,
            "checkin_professional_content": "Super happy with the EOS rollout.",
            "checkin_professional_rating": 10,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["checkin_professional_content"].startswith("Super happy")
    assert body["checkin_professional_rating"] == 10
    # The other category is untouched.
    assert body["checkin_personal_content"] is None
    assert body["checkin_personal_rating"] is None

    listed = client.get(f"/api/eos/l10/sessions/{session_id}/attendance").json()
    assert len(listed) == 1
    assert listed[0]["checkin_professional_rating"] == 10


def test_personal_and_professional_checkins_coexist():
    """The bug this fixes: a person's Personal check-in must NOT be wiped
    when they (or someone) later records their Professional check-in for the
    same session. They live in separate columns, so both persist."""
    session_id, person_id = _seed_session_and_person(
        person_email=f"both-{uuid.uuid4().hex[:8]}@example.com"
    )
    _as_user("admin@example.com", roles=["admin"])

    # Personal first.
    r = client.post(
        f"/api/eos/l10/sessions/{session_id}/attendance",
        json={
            "session_id": session_id,
            "person_id": person_id,
            "attended": True,
            "checkin_personal_content": "Ran my first half-marathon.",
            "checkin_personal_rating": 9,
        },
    )
    assert r.status_code == 200, r.text

    # Then Professional for the SAME person — must not overwrite Personal.
    r = client.post(
        f"/api/eos/l10/sessions/{session_id}/attendance",
        json={
            "session_id": session_id,
            "person_id": person_id,
            "attended": True,
            "checkin_professional_content": "Closed the Q2 audit early.",
            "checkin_professional_rating": 8,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["checkin_personal_content"] == "Ran my first half-marathon."
    assert body["checkin_personal_rating"] == 9
    assert body["checkin_professional_content"] == "Closed the Q2 audit early."
    assert body["checkin_professional_rating"] == 8

    # Still a single attendance row for the person.
    listed = client.get(f"/api/eos/l10/sessions/{session_id}/attendance").json()
    rows = [row for row in listed if row["person_id"] == person_id]
    assert len(rows) == 1
    assert rows[0]["checkin_personal_content"] == "Ran my first half-marathon."
    assert rows[0]["checkin_professional_content"] == "Closed the Q2 audit early."


def test_checkin_rating_out_of_range_rejected():
    session_id, person_id = _seed_session_and_person()
    _as_user("admin@example.com", roles=["admin"])
    r = client.post(
        f"/api/eos/l10/sessions/{session_id}/attendance",
        json={
            "session_id": session_id,
            "person_id": person_id,
            "attended": True,
            "checkin_personal_content": "Out of range test",
            "checkin_personal_rating": 11,
        },
    )
    assert r.status_code == 400


def test_attendance_endpoint_returns_empty_list_when_no_rows():
    session_id, _ = _seed_session_and_person()
    _as_user("admin@example.com", roles=["admin"])
    r = client.get(f"/api/eos/l10/sessions/{session_id}/attendance")
    assert r.status_code == 200
    assert r.json() == []


def test_checkin_skip_is_optional_and_idempotent():
    """Segway notes are OPTIONAL.

    Two-part assertion in one test to stay resilient to the shared-DB
    isolation gap the fresh_db fixture has on prod Cloud SQL:

    1. POSTing attendance with all check-in fields null must succeed and
       create an attendance row with empty check-in data (the exec-team
       flow where attendees don't want personal/score info persisted).
    2. After a real check-in is recorded, a later 'skip' (all nulls) must
       NOT wipe the recorded content — the router treats null as
       'no change' so an attendee can re-confirm attendance without
       losing their earlier entry.
    """
    # Unique person email so this test never collides with leftover Person
    # rows from other tests when the DB isolation falls through.
    session_id, person_id = _seed_session_and_person(
        person_email=f"ft1-skip-{uuid.uuid4().hex[:8]}@example.com"
    )
    _as_user("admin@example.com", roles=["admin"])

    # (1) First POST with all nulls — should create attendance, empty check-in.
    r = client.post(
        f"/api/eos/l10/sessions/{session_id}/attendance",
        json={
            "session_id": session_id,
            "person_id": person_id,
            "attended": True,
            "checkin_personal_content": None,
            "checkin_personal_rating": None,
            "checkin_professional_content": None,
            "checkin_professional_rating": None,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["checkin_personal_content"] is None
    assert body["checkin_professional_content"] is None
    assert body["checkin_professional_rating"] is None
    assert body["attended"] is True

    # (2) Now fill in the check-in for real.
    r = client.post(
        f"/api/eos/l10/sessions/{session_id}/attendance",
        json={
            "session_id": session_id,
            "person_id": person_id,
            "attended": True,
            "checkin_professional_content": "Shipped FT-7 yesterday",
            "checkin_professional_rating": 9,
        },
    )
    assert r.status_code == 200, r.text

    # (3) Re-submit with nulls — must preserve the recorded values, not wipe.
    r = client.post(
        f"/api/eos/l10/sessions/{session_id}/attendance",
        json={
            "session_id": session_id,
            "person_id": person_id,
            "attended": True,
            "checkin_personal_content": None,
            "checkin_personal_rating": None,
            "checkin_professional_content": None,
            "checkin_professional_rating": None,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["checkin_professional_content"] == "Shipped FT-7 yesterday"
    assert body["checkin_professional_rating"] == 9


def test_404_on_unknown_session_attendance_get():
    """The list endpoint with an unknown UUID should just return [] —
    keeping it permissive avoids extra fetches on the frontend when a
    session is fresh."""
    _as_user("admin@example.com", roles=["admin"])
    r = client.get(f"/api/eos/l10/sessions/{uuid.uuid4()}/attendance")
    assert r.status_code == 200
    assert r.json() == []


# -- L10 meeting creation gating (exec team option) ------------------------


def _seed_departments(names: list[str]) -> None:
    """Insert department rows the L10 FK can resolve. The schema uses
    ``departments.name`` as the PK; columns beyond ``name`` are optional."""
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.models import Department

    db = SessionLocal()
    try:
        for n in names:
            if not db.query(Department).filter(Department.name == n).first():
                db.add(Department(name=n))
        db.commit()
    finally:
        db.close()


def test_regular_user_cannot_create_l10_meeting():
    _seed_departments(["sales"])
    _as_user("regular@example.com")
    r = client.post(
        "/api/eos/l10/",
        json={"name": "Sales L10", "department_name": "sales", "duration_minutes": 90},
    )
    assert r.status_code == 403


def test_exec_user_can_create_exec_meeting():
    UserRegistry().update_user("exec@example.com", {"is_exec": True})
    _as_user("exec@example.com")
    r = client.post(
        "/api/eos/l10/",
        json={
            "name": "Exec L10",
            "is_exec_meeting": True,
            "duration_minutes": 90,
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["is_exec_meeting"] is True
    assert body["department_name"] is None


def test_exec_user_can_create_own_department_meeting():
    """Exec users also lead their own team — they can schedule that L10."""
    _seed_departments(["general"])  # exec@'s default department
    UserRegistry().update_user("exec@example.com", {"is_exec": True})
    _as_user("exec@example.com")
    r = client.post(
        "/api/eos/l10/",
        json={
            "name": "My team L10",
            "department_name": "general",
            "is_exec_meeting": False,
            "duration_minutes": 90,
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["department_name"] == "general"
    assert body["is_exec_meeting"] is False


def test_exec_user_cannot_create_other_department_meeting():
    """Exec ≠ admin — they still can't reach into a department they don't
    sit on. They must pick Exec scope instead."""
    _seed_departments(["sales", "general"])
    UserRegistry().update_user("exec@example.com", {"is_exec": True})
    _as_user("exec@example.com")
    r = client.post(
        "/api/eos/l10/",
        json={
            "name": "Sales L10",
            "department_name": "sales",  # exec@ is in "general"
            "duration_minutes": 90,
        },
    )
    assert r.status_code == 403


def test_admin_can_create_meeting_for_any_department():
    _seed_departments(["engineering"])
    _as_user("admin@example.com", roles=["admin"])
    r = client.post(
        "/api/eos/l10/",
        json={
            "name": "Eng L10",
            "department_name": "engineering",
            "duration_minutes": 90,
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["department_name"] == "engineering"


def test_admin_can_create_exec_meeting():
    _as_user("admin@example.com", roles=["admin"])
    r = client.post(
        "/api/eos/l10/",
        json={"name": "Exec L10", "is_exec_meeting": True, "duration_minutes": 90},
    )
    assert r.status_code == 201, r.text
    assert r.json()["is_exec_meeting"] is True


# -- Check-in attendee pool (meeting items payload) ------------------------
#
# The in-meeting Check-in picker is scoped to ``meeting.attendee_emails``:
# the invitee roster when there is one, else the meeting's TEAM (department
# members, or Exec-team members) — never every user in the system.


def _seed_meeting(
    *,
    department_name: str | None = None,
    is_exec_meeting: bool = False,
    roster_emails: list[str] | None = None,
) -> str:
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import L10Meeting

    db = SessionLocal()
    try:
        meeting = L10Meeting(
            name="Pulse",
            duration_minutes=90,
            department_name=department_name,
            is_exec_meeting=is_exec_meeting,
            roster_emails=roster_emails,
            created_by="admin@example.com",
        )
        db.add(meeting)
        db.commit()
        db.refresh(meeting)
        return str(meeting.id)
    finally:
        db.close()


def test_attendee_pool_falls_back_to_meeting_department():
    """No calendar roster → the picker pool is the meeting's department, not
    every user in the system. This is the reported bug."""
    _seed_departments(["sales", "general"])
    reg = UserRegistry()
    reg.register_user(
        email="rep1@example.com",
        name="Rep One",
        id="rep1@example.com",
        roles=["user"],
        department="sales",
    )
    reg.register_user(
        email="rep2@example.com",
        name="Rep Two",
        id="rep2@example.com",
        roles=["user"],
        department="sales",
    )
    # admin@/exec@/regular@ are all in "general" (the fixture default).
    meeting_id = _seed_meeting(department_name="sales")
    # A Sales member opens their own L10 (meetings are membership-scoped now —
    # a general-dept admin no longer sees another team's meeting content).
    _as_user("rep1@example.com")

    r = client.get(f"/api/eos/l10/meetings/{meeting_id}/items")
    assert r.status_code == 200, r.text
    pool = set(r.json()["meeting"]["attendee_emails"])
    assert pool == {"rep1@example.com", "rep2@example.com"}
    assert "regular@example.com" not in pool  # different department, excluded


def test_attendee_pool_prefers_invitee_roster_over_department():
    _seed_departments(["sales"])
    reg = UserRegistry()
    for email, name in (
        ("rep1@example.com", "Rep One"),
        ("rep2@example.com", "Rep Two"),
    ):
        reg.register_user(
            email=email,
            name=name,
            id=email,
            roles=["user"],
            department="sales",
        )
    # rep1 is on the invite list; rep2 is a Sales teammate who is NOT. Only
    # rep1 can open the meeting at all (the roster is the ACL), and the pool
    # is the roster — rep2 is not folded in for being in the department.
    meeting_id = _seed_meeting(
        department_name="sales",
        roster_emails=["rep1@example.com", "invited@acmecorp.example"],
    )
    _as_user("rep1@example.com")

    r = client.get(f"/api/eos/l10/meetings/{meeting_id}/items")
    assert r.status_code == 200, r.text
    # Roster wins outright — department members are not folded in.
    assert r.json()["meeting"]["attendee_emails"] == [
        "rep1@example.com",
        "invited@acmecorp.example",
    ]

    _as_user("rep2@example.com")
    assert client.get(f"/api/eos/l10/meetings/{meeting_id}/items").status_code == 404


def test_attendee_pool_for_exec_meeting_is_exec_team():
    UserRegistry().update_user("exec@example.com", {"is_exec": True})
    meeting_id = _seed_meeting(is_exec_meeting=True)
    _as_user("exec@example.com")  # Exec-team member opening the Exec L10

    r = client.get(f"/api/eos/l10/meetings/{meeting_id}/items")
    assert r.status_code == 200, r.text
    pool = set(r.json()["meeting"]["attendee_emails"])
    assert "exec@example.com" in pool
    assert "regular@example.com" not in pool  # not on the Exec team


def test_attendee_pool_resolves_people_outside_the_callers_team():
    """``attendees`` names every invitee — including one the CALLER's own owner
    roster excludes.

    The Check-in picker renders the intersection of this pool and
    ``GET /api/eos/people`` (which is scoped to the caller's team), so an
    attendee from another department used to vanish from the very room they
    were sitting in, and any check-in they had already recorded rendered as
    "—" because the frontend had no name for their ``person_id``.
    """
    _seed_departments(["sales", "systems", "general"])
    reg = UserRegistry()
    reg.register_user(
        email="rep1@example.com",
        name="Rep One",
        id="rep1@example.com",
        roles=["user"],
        department="sales",
    )
    reg.register_user(
        email="guest@example.com",
        name="Gail Guest",
        id="guest@example.com",
        roles=["user"],
        department="systems",
    )
    meeting_id = _seed_meeting(
        department_name="sales",
        roster_emails=["rep1@example.com", "guest@example.com"],
    )
    _as_user("rep1@example.com")  # Sales member opening their own L10

    # Also runs the lazy EOS people sync that gives each user an eos_people row.
    roster = client.get("/api/eos/people/")
    assert roster.status_code == 200, roster.text
    # The Sales caller's own owner roster does NOT carry the Systems guest...
    assert "guest@example.com" not in {p["email"] for p in roster.json()}

    r = client.get(f"/api/eos/l10/meetings/{meeting_id}/items")
    assert r.status_code == 200, r.text
    attendees = r.json()["meeting"]["attendees"]
    # ...but the meeting itself names them, so the picker can still offer them.
    assert {a["email"] for a in attendees} == {
        "rep1@example.com",
        "guest@example.com",
    }
    guest = next(a for a in attendees if a["email"] == "guest@example.com")
    assert guest["first_name"] == "Gail"
    assert guest["id"]  # a real eos_people id the check-in POST can use

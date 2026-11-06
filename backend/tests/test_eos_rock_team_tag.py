"""A Department rock without a team takes its owner's seat department; an Exec
rock never carries one.

The case: three of a team's five Q3 rocks were "missing" from the
team's view of the Rocks page, including the one marked Done. They carried no
team tag (``eos_rocks.department_name`` was added with no backfill, and the
create form leaves it empty for admins / org-wide execs); the page's team
filter matches on the tag alone, while the L10 Rock Review already treated
them as the owner's team's rocks. These tests pin the rule now applied on
create and on update:

* Department visibility + no team  -> the owner's seat department
* an explicit team                 -> kept as sent
* Company visibility               -> untouched (no team by convention)
* a seatless owner                 -> stays untagged, never guessed
* an Exec-team rock                -> NEVER carries a team tag, whatever was
                                      sent, and stays invisible to a non-exec
                                      teammate of the same department
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

ADMIN = "admin@example.com"
EXEC = "exec@example.com"
MEMBER = "member@example.com"  # a Systems teammate with no special role


def _profile(email: str, *, roles=None, department="Systems", is_exec=False):
    now = "2026-10-05T00:00:00"
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name=email.split("@")[0].title(),
        roles=roles or ["user"],
        department=department,
        created_at=now,
        updated_at=now,
        is_exec=is_exec,
    )


def _as(email: str, **kw) -> None:
    app.dependency_overrides[get_current_user] = lambda: _profile(email, **kw)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """A throwaway SQLite file, and a hard guard against the dev Cloud SQL."""
    monkeypatch.setattr(settings, "CLOUDSQL_CONNECTION_NAME", "")
    monkeypatch.setattr(
        settings, "DATABASE_URL", f"sqlite:///{tmp_path / 'rock_team_tag.db'}"
    )
    import rubik_core.database.db as dbmod

    dbmod._engine = None
    engine = dbmod.get_engine()
    assert engine.dialect.name == "sqlite", "tests must never touch Cloud SQL"
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database import eos  # noqa: F401 — registers the eos_* tables
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def seeded() -> dict[str, str]:
    """Org + current quarter, Systems/Sales departments, a seated Systems owner,
    a seatless owner and a seated Systems teammate (MEMBER)."""
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import Organization, Person, Quarter, Seat
    from rubik_core.database.models import Department

    db = SessionLocal()
    try:
        org = Organization(name="Team Tag Org", created_by=ADMIN)
        db.add(org)
        db.add(Department(name="Systems"))
        db.add(Department(name="Sales"))
        db.commit()
        db.refresh(org)
        now = datetime.utcnow()
        quarter = Quarter(
            organization_id=org.id,
            name="Q4 2026",
            start_date=now - timedelta(days=1),
            end_date=now + timedelta(days=80),
            is_current=True,
            created_by=ADMIN,
        )
        seat = Seat(department_name="Systems", title="AI Lead", created_by=ADMIN)
        db.add_all([quarter, seat])
        db.commit()
        db.refresh(quarter)
        db.refresh(seat)
        seated = Person(
            first_name="Seated",
            last_name="Owner",
            email="seated@example.com",
            seat_id=seat.id,
            created_by=ADMIN,
        )
        seatless = Person(
            first_name="No",
            last_name="Seat",
            email="noseat@example.com",
            created_by=ADMIN,
        )
        member = Person(
            first_name="Team",
            last_name="Mate",
            email=MEMBER,
            seat_id=seat.id,
            created_by=ADMIN,
        )
        db.add_all([seated, seatless, member])
        db.commit()
        for p in (seated, seatless):
            db.refresh(p)
        return {
            "quarter_id": str(quarter.id),
            "seated": str(seated.id),
            "seatless": str(seatless.id),
        }
    finally:
        db.close()


def _post(payload: dict) -> dict:
    r = client.post("/api/eos/rocks/", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def _rock(seeded, owner="seated", **extra) -> dict:
    return {
        "description": "Deploy DD Warehouse AI Solutions",
        "owner_id": seeded[owner],
        "quarter_id": seeded["quarter_id"],
        **extra,
    }


def _insert_rock(seeded, *, owner="seated", **cols) -> str:
    """Seed a rock straight into the table (the pre-fix shape, no API rule)."""
    from uuid import UUID

    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import Rock, Visibility

    db = SessionLocal()
    try:
        rock = Rock(
            quarter_id=UUID(seeded["quarter_id"]),
            owner_id=UUID(seeded[owner]),
            description=cols.pop("description", "Legacy untagged rock"),
            visibility=cols.pop("visibility", Visibility.DEPARTMENT),
            created_by=cols.pop("created_by", "someone@example.com"),
            **cols,
        )
        db.add(rock)
        db.commit()
        return str(rock.id)
    finally:
        db.close()


def _read_rock(rock_id: str) -> dict:
    from uuid import UUID

    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import Rock

    db = SessionLocal()
    try:
        r = db.query(Rock).filter(Rock.id == UUID(rock_id)).one()
        return {
            "department_name": r.department_name,
            "is_exec": bool(r.is_exec),
            "updated_by": r.updated_by,
        }
    finally:
        db.close()


# -- create ------------------------------------------------------------------


def test_department_rock_without_team_takes_owner_seat(seeded):
    _as(ADMIN, roles=["admin"])
    body = _post(_rock(seeded, visibility="department"))
    assert body["department_name"] == "Systems"
    assert body["is_exec"] is False


def test_explicit_team_is_kept_as_sent(seeded):
    _as(ADMIN, roles=["admin"])
    body = _post(_rock(seeded, visibility="department", department_name="Sales"))
    assert body["department_name"] == "Sales"


def test_company_rock_stays_untagged(seeded):
    _as(ADMIN, roles=["admin"])
    body = _post(_rock(seeded, visibility="company"))
    assert body["department_name"] is None


def test_seatless_owner_is_never_guessed(seeded):
    _as(ADMIN, roles=["admin"])
    body = _post(_rock(seeded, owner="seatless", visibility="department"))
    assert body["department_name"] is None


def test_exec_rock_never_carries_a_team_and_stays_exec_only(seeded):
    _as(EXEC, is_exec=True)
    body = _post(
        _rock(seeded, visibility="department", is_exec=True, department_name="Systems")
    )
    assert body["is_exec"] is True
    assert body["department_name"] is None, "an Exec rock must never carry a team tag"

    # The Systems teammate asks for Systems rocks and must not get the Exec one,
    # tag or no tag — the exec fence is on is_exec, and the tag can't open it.
    _as(MEMBER)
    r = client.get("/api/eos/rocks/", params={"department_name": "Systems"})
    assert r.status_code == 200, r.text
    assert body["id"] not in {x["id"] for x in r.json()}
    r = client.get("/api/eos/rocks/")
    assert body["id"] not in {x["id"] for x in r.json()}


def test_member_sees_the_derived_team_rock_under_its_team(seeded):
    _as(ADMIN, roles=["admin"])
    body = _post(_rock(seeded, visibility="department"))
    _as(MEMBER)
    r = client.get("/api/eos/rocks/", params={"department_name": "Systems"})
    assert r.status_code == 200, r.text
    assert body["id"] in {x["id"] for x in r.json()}


# -- update ------------------------------------------------------------------


def test_patch_heals_a_legacy_untagged_rock(seeded):
    rock_id = _insert_rock(seeded)  # Department visibility, tag NULL
    _as(ADMIN, roles=["admin"])
    r = client.patch(f"/api/eos/rocks/{rock_id}", json={"status": "off_track"})
    assert r.status_code == 200, r.text
    assert r.json()["department_name"] == "Systems"


def test_patch_to_exec_sheds_the_team_and_back_restores_it(seeded):
    rock_id = _insert_rock(seeded, department_name="Systems")
    _as(ADMIN, roles=["admin"])  # unrestricted: may re-scope to/from Exec
    r = client.patch(f"/api/eos/rocks/{rock_id}", json={"is_exec": True})
    assert r.status_code == 200, r.text
    assert r.json()["is_exec"] is True
    assert r.json()["department_name"] is None

    r = client.patch(
        f"/api/eos/rocks/{rock_id}",
        json={"is_exec": True, "department_name": "Systems"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["department_name"] is None, "a tag sent on an Exec rock is dropped"

    r = client.patch(f"/api/eos/rocks/{rock_id}", json={"is_exec": False})
    assert r.status_code == 200, r.text
    assert r.json()["is_exec"] is False
    assert r.json()["department_name"] == "Systems"


def test_non_exec_cannot_declassify_an_exec_rock(seeded):
    rock_id = _insert_rock(seeded, is_exec=True, created_by=MEMBER)
    _as(MEMBER)
    r = client.patch(f"/api/eos/rocks/{rock_id}", json={"is_exec": False})
    assert r.status_code in (403, 404), r.text
    assert _read_rock(rock_id) == {
        "department_name": None,
        "is_exec": True,
        "updated_by": None,
    }

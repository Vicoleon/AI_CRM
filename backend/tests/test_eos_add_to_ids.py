"""The "Add to IDS" button on rocks.

POST /api/eos/rocks/{rock_id}/ids creates an Issue with:
- source_rock_id stamped to the rock for the soft back-pointer,
- department_name resolved from the rock owner's Seat (or the caller's
  department as fallback),
- status OPEN, priority configurable (defaults to MEDIUM).
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


def _profile(
    email: str, roles: list[str] | None = None, department: str = "general"
) -> UserProfile:
    now = "2026-05-21T00:00:00"
    stored = UserRegistry().get_user(email)
    return UserProfile(
        id=stored.id if stored else email.split("@")[0],
        email=email,
        name=stored.name if stored else email.split("@")[0].title(),
        roles=roles or (stored.roles if stored else ["user"]),
        department=stored.department if stored else department,
        created_at=now,
        updated_at=now,
        is_exec=bool(stored.is_exec) if stored else False,
    )


def _as_user(email: str, roles: list[str] | None = None, department: str = "general"):
    def _factory():
        return _profile(email, roles, department=department)

    app.dependency_overrides[get_current_user] = _factory


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db_path = tmp_path / "test_ft10.db"
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


def _seed_rock_with_seat(
    department: str = "Systems",
    owner_email: str | None = None,
) -> tuple[str, str]:
    """Seed Org → Quarter → Seat(dept) → Person(in seat) → Rock(owned by person).

    Returns (rock_id, owner_email). The owner is given a Seat so the
    department-resolution path in the Add to IDS endpoint exercises the Seat
    lookup, not the caller-fallback path."""
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import (
        Organization,
        Person,
        Quarter,
        Rock,
        RockStatus,
        Seat,
        Visibility,
    )

    # The app's departments table is required for Seat.department_name FK.
    from rubik_core.database.models import Department

    db = SessionLocal()
    try:
        if not db.query(Department).filter(Department.name == department).first():
            db.add(Department(name=department))
            db.commit()

        org = db.query(Organization).filter(Organization.name == "FT-10 Org").first()
        if org is None:
            org = Organization(name="FT-10 Org", created_by="admin@example.com")
            db.add(org)
            db.commit()
            db.refresh(org)

        quarter = Quarter(
            organization_id=org.id,
            name=f"FT10 Q-{datetime.utcnow().isoformat()[:19]}",
            start_date=datetime.utcnow(),
            end_date=datetime.utcnow() + timedelta(days=90),
            is_current=False,
            created_by="admin@example.com",
        )
        db.add(quarter)
        db.commit()
        db.refresh(quarter)

        owner_email = (
            owner_email
            or f"ft10-owner-{datetime.utcnow().strftime('%H%M%S%f')}@example.com"
        )
        seat = Seat(
            department_name=department,
            title="Owner",
            created_by="admin@example.com",
        )
        db.add(seat)
        db.commit()
        db.refresh(seat)

        person = Person(
            first_name="FT10",
            last_name="Owner",
            email=owner_email,
            seat_id=seat.id,
            created_by="admin@example.com",
        )
        db.add(person)
        db.commit()
        db.refresh(person)

        rock = Rock(
            quarter_id=quarter.id,
            owner_id=person.id,
            description="Ship FT-10 (Add to IDS button)",
            status=RockStatus.ON_TRACK,
            visibility=Visibility.COMPANY,
            created_by="admin@example.com",
        )
        db.add(rock)
        db.commit()
        db.refresh(rock)
        return str(rock.id), owner_email
    finally:
        db.close()


# -- Happy path -------------------------------------------------------


def test_add_to_ids_happy_path_stamps_source_rock_and_department():
    rock_id, _ = _seed_rock_with_seat(department="Systems")
    _as_user("admin@example.com", roles=["admin"])

    r = client.post(
        f"/api/eos/rocks/{rock_id}/ids",
        json={
            "description": "Discuss whether we should split this rock into two",
            "priority": "high",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["description"] == "Discuss whether we should split this rock into two"
    # rock→IDS supplies no explicit title, so the server derives one from the
    # description (single line, < 120 chars → used verbatim).
    assert body["title"] == "Discuss whether we should split this rock into two"
    assert body["priority"] == "high"
    assert body["status"] == "open"
    assert body["source_rock_id"] == rock_id
    # Department resolved from the rock owner's Seat — NOT the caller's
    # department. This is the assertion that proves the seat lookup ran.
    assert body["department_name"] == "Systems"
    # Owner of the issue defaults to the rock owner so the L10 facilitator
    # has someone to assign the discussion to immediately.
    assert body["owner_id"] is not None


def test_add_to_ids_defaults_priority_to_medium():
    rock_id, _ = _seed_rock_with_seat()
    _as_user("admin@example.com", roles=["admin"])

    r = client.post(
        f"/api/eos/rocks/{rock_id}/ids",
        json={"description": "Default priority test"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["priority"] == "medium"


# -- Validation -------------------------------------------------------


def test_add_to_ids_rejects_empty_description():
    rock_id, _ = _seed_rock_with_seat()
    _as_user("admin@example.com", roles=["admin"])

    r = client.post(
        f"/api/eos/rocks/{rock_id}/ids",
        json={"description": "   "},
    )
    assert r.status_code == 422


def test_add_to_ids_returns_404_for_unknown_rock():
    _as_user("admin@example.com", roles=["admin"])
    bogus = "00000000-0000-0000-0000-000000000000"
    r = client.post(
        f"/api/eos/rocks/{bogus}/ids",
        json={"description": "ghost"},
    )
    assert r.status_code == 404


# -- Department-fallback path ----------------------------------------


def test_add_to_ids_falls_back_to_caller_department_when_owner_has_no_seat():
    """When the rock owner exists but doesn't have a Seat yet, the
    endpoint falls back to the caller's department so the issue still
    lands on a team's IDS list rather than being orphaned."""
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import (
        Organization,
        Person,
        Quarter,
        Rock,
        RockStatus,
        Visibility,
    )

    db = SessionLocal()
    try:
        org = Organization(
            name=f"Fb Org {datetime.utcnow().isoformat()[:19]}",
            created_by="admin@example.com",
        )
        db.add(org)
        db.commit()
        db.refresh(org)

        quarter = Quarter(
            organization_id=org.id,
            name=f"Fb Q-{datetime.utcnow().isoformat()[:19]}",
            start_date=datetime.utcnow(),
            end_date=datetime.utcnow() + timedelta(days=90),
            is_current=False,
            created_by="admin@example.com",
        )
        db.add(quarter)
        db.commit()
        db.refresh(quarter)

        # Owner with NO Seat — that's the path under test.
        owner = Person(
            first_name="No",
            last_name="Seat",
            email=f"noseat-{datetime.utcnow().strftime('%H%M%S%f')}@example.com",
            created_by="admin@example.com",
        )
        db.add(owner)
        db.commit()
        db.refresh(owner)

        rock = Rock(
            quarter_id=quarter.id,
            owner_id=owner.id,
            description="Owner has no seat",
            status=RockStatus.ON_TRACK,
            visibility=Visibility.COMPANY,
            created_by="admin@example.com",
        )
        db.add(rock)
        db.commit()
        rock_id = str(rock.id)
    finally:
        db.close()

    # Caller has department "Sales" — the fallback should pick it up.
    # Use an email that is NOT in the UserRegistry: ``_profile`` prefers
    # the stored profile's department for registered users, and the
    # fixture registers admin@example.com with the default "general",
    # which would mask the caller-department fallback under test.
    _as_user("sales-caller@example.com", roles=["admin"], department="Sales")

    r = client.post(
        f"/api/eos/rocks/{rock_id}/ids",
        json={"description": "Fallback to caller dept"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["department_name"] == "Sales"


# -- Back-reference round-trip ---------------------------------------


def test_issue_created_by_add_to_ids_carries_source_rock_id_on_list():
    rock_id, _ = _seed_rock_with_seat(department="Systems")
    _as_user("admin@example.com", roles=["admin"])

    client.post(
        f"/api/eos/rocks/{rock_id}/ids",
        json={"description": "Surface me"},
    )
    rows = client.get("/api/eos/issues/", params={"department_name": "Systems"}).json()
    matching = [r for r in rows if r["description"] == "Surface me"]
    assert len(matching) == 1
    assert matching[0]["source_rock_id"] == rock_id

"""Tests for weekly rock updates with REQUIRED status + percent.

Covers the new POST /api/eos/rocks/{rock_id}/updates and
GET /api/eos/rocks/{rock_id}/updates endpoints plus the side effect of
the rock's cached status being bumped to match the latest update.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

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
    db_path = tmp_path / "test_rock_updates.db"
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
        ("owner@example.com", ["user"]),
    ]:
        if not reg.get_user(email):
            reg.register_user(
                email=email, name=email.split("@")[0], id=email, roles=roles
            )
    yield
    app.dependency_overrides.clear()


def _seed_rock(owner_email: str = "owner-ft7@example.com") -> str:
    """Seed an Organization, Quarter, Person, and Rock. Returns rock_id.

    Reuses existing rows by natural key to stay resilient to the
    shared-prod-db isolation gap in fresh_db (see the check-in fix in
    test_eos_exec_and_checkin.py)."""
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
        org = (
            db.query(Organization).filter(Organization.name == "FT-7 Test Org").first()
        )
        if org is None:
            org = Organization(
                name="FT-7 Test Org",
                created_by="admin@example.com",
            )
            db.add(org)
            db.commit()
            db.refresh(org)

        # Use a unique quarter name so we don't collide with other tests.
        quarter_name = f"FT7 Q-{datetime.utcnow().isoformat()[:19]}"
        quarter = Quarter(
            organization_id=org.id,
            name=quarter_name,
            start_date=datetime.utcnow(),
            end_date=datetime.utcnow() + timedelta(days=90),
            is_current=False,  # don't clobber a real current quarter
            created_by="admin@example.com",
        )
        db.add(quarter)
        db.commit()
        db.refresh(quarter)

        person = (
            db.query(Person)
            .filter(Person.email == owner_email, Person.is_deleted == False)  # noqa: E712
            .first()
        )
        if person is None:
            person = Person(
                first_name="FT7",
                last_name="Owner",
                email=owner_email,
                created_by="admin@example.com",
            )
            db.add(person)
            db.commit()
            db.refresh(person)

        rock = Rock(
            quarter_id=quarter.id,
            owner_id=person.id,
            description="Ship FT-7 (rocks weekly updates)",
            status=RockStatus.ON_TRACK,
            visibility=Visibility.COMPANY,
            created_by="admin@example.com",
        )
        db.add(rock)
        db.commit()
        db.refresh(rock)
        return str(rock.id)
    finally:
        db.close()


def _unique_email(suffix: str) -> str:
    return f"ft7-{suffix}-{datetime.utcnow().strftime('%H%M%S%f')}@example.com"


# -- Create endpoint --------------------------------------------------


def test_create_rock_update_happy_path():
    rock_id = _seed_rock(_unique_email("happy"))
    _as_user("admin@example.com", roles=["admin"])

    r = client.post(
        f"/api/eos/rocks/{rock_id}/updates",
        json={
            "status": "on_track",
            "percent_complete": 50,
            "comment": "Backend done; frontend next.",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "on_track"
    assert body["percent_complete"] == 50
    assert body["comment"] == "Backend done; frontend next."
    assert body["rock_id"] == rock_id
    assert body["update_date"] == date.today().isoformat()


def test_create_rock_update_status_is_required():
    rock_id = _seed_rock(_unique_email("nostatus"))
    _as_user("admin@example.com", roles=["admin"])

    r = client.post(
        f"/api/eos/rocks/{rock_id}/updates",
        json={"percent_complete": 50},
    )
    assert r.status_code == 422


def test_create_rock_update_percent_is_required():
    rock_id = _seed_rock(_unique_email("nopercent"))
    _as_user("admin@example.com", roles=["admin"])

    r = client.post(
        f"/api/eos/rocks/{rock_id}/updates",
        json={"status": "on_track"},
    )
    assert r.status_code == 422


def test_create_rock_update_percent_out_of_range_rejected():
    rock_id = _seed_rock(_unique_email("range"))
    _as_user("admin@example.com", roles=["admin"])

    over = client.post(
        f"/api/eos/rocks/{rock_id}/updates",
        json={"status": "on_track", "percent_complete": 150},
    )
    assert over.status_code == 422

    under = client.post(
        f"/api/eos/rocks/{rock_id}/updates",
        json={"status": "on_track", "percent_complete": -5},
    )
    assert under.status_code == 422


def test_create_rock_update_bumps_cached_rock_status():
    """Posting an off_track update flips Rock.status so list views show
    the latest known state without a JOIN."""
    rock_id = _seed_rock(_unique_email("bump"))
    _as_user("admin@example.com", roles=["admin"])

    r = client.post(
        f"/api/eos/rocks/{rock_id}/updates",
        json={"status": "off_track", "percent_complete": 20},
    )
    assert r.status_code == 201, r.text

    rock = client.get(f"/api/eos/rocks/{rock_id}").json()
    assert rock["status"] == "off_track"


def test_create_rock_update_for_unknown_rock_returns_404():
    _as_user("admin@example.com", roles=["admin"])
    bogus = "00000000-0000-0000-0000-000000000000"
    r = client.post(
        f"/api/eos/rocks/{bogus}/updates",
        json={"status": "on_track", "percent_complete": 0},
    )
    assert r.status_code == 404


# -- List endpoint ----------------------------------------------------


def test_list_rock_updates_returns_history_reverse_chronological():
    rock_id = _seed_rock(_unique_email("hist"))
    _as_user("admin@example.com", roles=["admin"])

    today = date.today()
    # Post three updates with explicit dates so order is deterministic.
    for day_offset, pct in [(0, 80), (-7, 60), (-14, 30)]:
        r = client.post(
            f"/api/eos/rocks/{rock_id}/updates",
            json={
                "status": "on_track",
                "percent_complete": pct,
                "update_date": (today + timedelta(days=day_offset)).isoformat(),
            },
        )
        assert r.status_code == 201, r.text

    r = client.get(f"/api/eos/rocks/{rock_id}/updates")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 3
    # Newest first.
    dates = [row["update_date"] for row in rows]
    assert dates == sorted(dates, reverse=True)
    assert rows[0]["percent_complete"] == 80
    assert rows[-1]["percent_complete"] == 30


def test_list_rock_updates_for_unknown_rock_returns_404():
    _as_user("admin@example.com", roles=["admin"])
    bogus = "00000000-0000-0000-0000-000000000000"
    r = client.get(f"/api/eos/rocks/{bogus}/updates")
    assert r.status_code == 404

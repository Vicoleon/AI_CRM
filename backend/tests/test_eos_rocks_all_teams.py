"""Every team's Rocks, except the Exec team's, for the people who keep them.

A rock keeper (Sloane, in Support) writes up every team's rock-setting
session, so Sloane has to see and fill in every team's Rocks in Cubie, not only
Support's. The grant is the ``eos_rocks_all_teams`` roster in
Admin → Feature Access, which ships with nobody on it; ``fresh_db`` names Sloane
the way an admin would. It must:

  * open every non-ET Rock to READ and UPDATE, personal ones included;
  * never open an Exec-team Rock, and never let the keeper mark one;
  * never widen DELETE, Issues, To-Dos, or any picker but the Rocks page's;
  * follow the roster as data: an admin override moves the grant.

No mocks — a temp SQLite DB per test, the same shape as
``test_eos_access_control.py``.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from backend.routers.eos import _authz
from rubik_core.config import settings
from rubik_core.database.schemas import UserProfile
from rubik_core.features import FeaturePolicy, clear_cache, save_policy

client = TestClient(app)

KEEPER = "sloane@acme.example"  # named on the roster by an admin


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db_path = tmp_path / "test_rocks_all_teams.db"
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    import rubik_core.database.db as dbmod

    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database import eos  # noqa: F401
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    # The policy cache is per process: an override saved by one test (or a
    # read against another test's DB) must not decide the next one.
    clear_cache()
    # The roster ships empty; name the keeper as an admin would.
    db = dbmod.SessionLocal()
    try:
        save_policy(
            db,
            "eos_rocks_all_teams",
            FeaturePolicy(
                extra_emails=frozenset({KEEPER}),
                exec_allowed=False,
                admin_allowed=False,
            ),
            actor_email="admin@x.example",
            note="the person who keeps every team's Rocks",
        )
    finally:
        db.close()
    yield
    clear_cache()
    app.dependency_overrides.clear()


def _profile(
    email: str,
    *,
    department: str = "general",
    roles: list[str] | None = None,
    is_exec: bool = False,
) -> UserProfile:
    now = "2026-10-13T00:00:00"
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


def _keeper() -> UserProfile:
    return _profile(KEEPER, department="Support")


def _session():
    from rubik_core.database.db import SessionLocal

    return SessionLocal()


def _seed(*, keeper_has_seat: bool = True) -> dict:
    """Sales / HR / Support teams, one person each, and one Rock of every kind."""
    from rubik_core.database.eos import (
        Issue,
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
        db.add_all([Department(name=n) for n in ("Sales", "HR", "Support")])
        org = Organization(name="Acme", created_by="seed")
        db.add(org)
        db.commit()
        db.refresh(org)
        q = Quarter(
            organization_id=org.id,
            name="Q4-2026",
            start_date=datetime(2026, 10, 1),
            end_date=datetime(2026, 12, 31),
            is_current=True,
            created_by="seed",
        )
        db.add(q)
        db.commit()
        db.refresh(q)

        people = {}
        for key, dept, email in (
            ("sales", "Sales", "ae@x.example"),
            ("hr", "HR", "hr@x.example"),
            (
                "support",
                "Support",
                KEEPER if keeper_has_seat else "supportmate@x.example",
            ),
        ):
            seat = Seat(department_name=dept, title=f"{dept} seat", created_by="seed")
            db.add(seat)
            db.commit()
            db.refresh(seat)
            person = Person(
                first_name=key,
                last_name="P",
                email=email,
                seat_id=seat.id,
                created_by="seed",
            )
            db.add(person)
            db.commit()
            db.refresh(person)
            people[key] = person
        ids["person_sales"] = people["sales"].id
        ids["person_hr"] = people["hr"].id

        def rock(desc, vis, owner, *, dept=None, is_exec=False):
            r = Rock(
                quarter_id=q.id,
                owner_id=owner.id,
                description=desc,
                visibility=vis,
                department_name=dept,
                is_exec=is_exec,
                created_by="seed",
            )
            db.add(r)
            db.commit()
            db.refresh(r)
            return r.id

        ids["sales_team"] = rock(
            "sales team rock", Visibility.DEPARTMENT, people["sales"], dept="Sales"
        )
        ids["hr_personal"] = rock(
            "hr personal rock", Visibility.INDIVIDUAL, people["hr"]
        )
        ids["company"] = rock("company rock", Visibility.COMPANY, people["sales"])
        # The ET's Rock: no department, owner outside the keeper's reach.
        ids["exec"] = rock(
            "exec team rock", Visibility.DEPARTMENT, people["hr"], is_exec=True
        )

        issue = Issue(
            description="sales issue", department_name="Sales", created_by="seed"
        )
        db.add(issue)
        db.commit()
        db.refresh(issue)
        ids["sales_issue"] = issue.id
        return ids
    finally:
        db.close()


def _rock(db, rock_id):
    from rubik_core.database.eos import Rock

    return db.query(Rock).filter(Rock.id == rock_id).one()


# ---- read ------------------------------------------------------------------


@pytest.mark.parametrize("keeper_has_seat", [True, False])
def test_the_keeper_sees_every_teams_rocks_but_not_the_exec_teams(keeper_has_seat):
    ids = _seed(keeper_has_seat=keeper_has_seat)
    db = _session()
    try:
        vis = set(_authz.visible_rock_ids(_keeper(), db))
        assert ids["sales_team"] in vis  # another team's Rock
        assert ids["hr_personal"] in vis  # someone's personal Rock
        assert ids["company"] in vis
        assert ids["exec"] not in vis  # the ET's stays with the ET
    finally:
        db.close()


def test_a_teammate_who_is_not_on_the_roster_gets_nothing_new():
    ids = _seed(keeper_has_seat=False)
    db = _session()
    try:
        vis = set(
            _authz.visible_rock_ids(
                _profile("supportmate@x.example", department="Support"), db
            )
        )
        assert ids["company"] in vis
        assert ids["sales_team"] not in vis
        assert ids["hr_personal"] not in vis
        assert ids["exec"] not in vis
    finally:
        db.close()


def test_the_grant_is_rocks_only():
    """Not a Team Manager: other teams' Issues stay closed."""
    ids = _seed()
    db = _session()
    try:
        assert ids["sales_issue"] not in set(_authz.visible_issue_ids(_keeper(), db))
    finally:
        db.close()


# ---- write -----------------------------------------------------------------


def test_the_keeper_may_update_every_non_exec_rock_but_delete_none():
    ids = _seed()
    db = _session()
    try:
        keeper = _keeper()
        for key in ("sales_team", "hr_personal", "company"):
            rock = _rock(db, ids[key])
            assert _authz.can_update_rock(keeper, rock, db), key
            assert not _authz.can_mutate(keeper, rock, db), key  # delete
        assert not _authz.can_update_rock(keeper, _rock(db, ids["exec"]), db)
    finally:
        db.close()


def test_rock_write_flags_match_the_single_row_gates_for_the_keeper():
    _seed()
    from rubik_core.database.eos import Rock

    db = _session()
    try:
        rocks = db.query(Rock).all()
        keeper = _keeper()
        flags = _authz.rock_write_flags(keeper, rocks, db)
        for rock in rocks:
            assert flags[rock.id] == (
                _authz.can_update_rock(keeper, rock, db),
                _authz.can_mutate(keeper, rock, db),
            ), rock.description
    finally:
        db.close()


def test_the_rocks_endpoints_follow_the_grant():
    ids = _seed()
    app.dependency_overrides[get_current_user] = _keeper

    listed = {r["id"]: r for r in client.get("/api/eos/rocks/").json()}
    assert str(ids["exec"]) not in listed
    for key in ("sales_team", "hr_personal", "company"):
        assert listed[str(ids[key])]["can_edit"] is True
        assert listed[str(ids[key])]["can_delete"] is False

    assert client.get(f"/api/eos/rocks/{ids['exec']}").status_code == 404
    assert client.get(f"/api/eos/rocks/{ids['hr_personal']}").status_code == 200

    sales = f"/api/eos/rocks/{ids['sales_team']}"
    r = client.patch(sales, json={"description": "sales team rock, filled in"})
    assert r.status_code == 200
    # Never into the Exec team, and never a delete.
    assert client.patch(sales, json={"is_exec": True}).status_code == 403
    assert client.delete(sales).status_code == 403
    assert (
        client.patch(
            f"/api/eos/rocks/{ids['exec']}", json={"description": "x"}
        ).status_code
        == 403
    )

    # Filing a Rock for another team works, and the keeper keeps it (creator).
    r = client.post(
        "/api/eos/rocks/",
        json={
            "description": "new sales rock",
            "owner_id": str(ids["person_sales"]),
            "visibility": "department",
            "department_name": "Sales",
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["department_name"] == "Sales"


# ---- the Rocks page's team list --------------------------------------------


def test_the_rocks_page_lists_every_team_for_the_keeper_only():
    _seed()
    app.dependency_overrides[get_current_user] = _keeper

    rocks_scopes = client.get(
        "/api/eos/me/meeting-scopes", params={"surface": "rocks"}
    ).json()["scopes"]
    by_value = {s["value"]: s for s in rocks_scopes}
    assert {"Support", "Sales", "HR"} <= set(by_value)
    assert all(s["kind"] == "department" for s in rocks_scopes)  # no Exec scope
    assert by_value["Support"]["is_primary"] is True
    assert by_value["Sales"]["can_schedule"] is False  # filing only

    # Every other page (Meetings, Issues, To-Dos, Scorecard) asks without it.
    plain = client.get("/api/eos/me/meeting-scopes").json()["scopes"]
    assert [s["value"] for s in plain] == ["Support"]

    # A teammate off the roster gets only their own team either way.
    app.dependency_overrides[get_current_user] = lambda: _profile(
        "supportmate@x.example", department="Support"
    )
    mate = client.get("/api/eos/me/meeting-scopes", params={"surface": "rocks"}).json()[
        "scopes"
    ]
    assert [s["value"] for s in mate] == ["Support"]


# ---- the roster is data ----------------------------------------------------


def test_an_admin_override_moves_the_grant():
    ids = _seed()
    db = _session()
    try:
        save_policy(
            db,
            "eos_rocks_all_teams",
            FeaturePolicy(
                extra_emails=frozenset({"hr@x.example"}), admin_allowed=False
            ),
            actor_email="admin@x.example",
            note="handed over",
        )
        assert ids["sales_team"] not in set(_authz.visible_rock_ids(_keeper(), db))
        hr = _profile("hr@x.example", department="HR")
        vis = set(_authz.visible_rock_ids(hr, db))
        assert ids["sales_team"] in vis
        assert ids["exec"] in vis  # hr owns the ET rock: the owner carve-out
        assert _authz.can_update_rock(hr, _rock(db, ids["sales_team"]), db)
    finally:
        db.close()


def test_the_rocks_page_owner_list_covers_every_team_for_the_keeper_only():
    """Every team's Rock needs its owner's name, and a Rock filed for another
    team needs an owner from that team."""
    _seed()
    from rubik_core.database.models import User

    db = _session()
    try:
        db.add_all(
            [
                User(email=KEEPER, id="sloane", name="Sloane", department="Support"),
                User(email="ae@x.example", id="ae", name="Ae", department="Sales"),
                User(email="hr@x.example", id="hr", name="Hr", department="HR"),
                User(
                    email="supportmate@x.example",
                    id="mate",
                    name="Mate",
                    department="Support",
                ),
            ]
        )
        db.commit()
    finally:
        db.close()

    def emails(params=None):
        r = client.get("/api/eos/people/", params=params or {})
        assert r.status_code == 200, r.text
        return {p["email"] for p in r.json()}

    app.dependency_overrides[get_current_user] = _keeper
    assert {"ae@x.example", "hr@x.example", KEEPER} <= emails({"surface": "rocks"})
    # Every other page's owner picker is unchanged: the keeper's own team only.
    assert "ae@x.example" not in emails()
    assert "hr@x.example" not in emails()

    app.dependency_overrides[get_current_user] = lambda: _profile(
        "supportmate@x.example", department="Support"
    )
    assert "ae@x.example" not in emails({"surface": "rocks"})

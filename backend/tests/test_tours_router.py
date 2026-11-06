"""Page tours ("Walk me"): each person's memory of the tours they have seen.

What this file pins:

1. **WHO.** Only the ``page_tours`` roster reaches ``/api/tours`` (403
   otherwise), and the router is mounted outside the copilot: an
   EOS-only person put on the roster gets in, because the EOS pages carry
   tours too.
2. **Memory.** Marking tours seen is per person and survives a reload; a
   finished tour stays finished when a replay of it is skipped; forgetting
   brings every tour back but keeps the person's on/off switch.
3. **The switch.** Off is one reserved row; on is no row; it never shows up
   as a "seen" tour.
4. **Bad input.** A malformed tour id, an empty or oversized list and a
   flood past the per-person cap are refused without writing anything.
5. **Impersonation.** An admin viewing Cubie as someone cannot read or write
   that person's tours (the router is not on the read-only allowlist).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from backend.routers import tours as tours_router
from backend.tests._rosters import pin_rosters
from rubik_core.auth import impersonation
from rubik_core.database.models import TOURS_SWITCH_ID, UserTourProgress
from rubik_core.database.schemas import UserProfile
from rubik_core.features import FeaturePolicy, clear_cache, feature_by_key

URL = "/api/tours"


def _profile(
    email: str,
    *,
    department: str = "general",
    roles: list[str] | None = None,
    is_exec: bool = False,
) -> UserProfile:
    now = "2026-10-14T00:00:00"
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


SYSTEMS = _profile("Sys.User@acme.example", department="Systems")
SYSTEMS_2 = _profile("other.sys@acme.example", department="Systems")
SALES = _profile("ae@acme.example", department="Sales")
EXEC = _profile("vp@acme.example", department="Sales", is_exec=True)
ADMIN = _profile("admin@acme.example", department="Sales", roles=["admin", "user"])
# Operations is outside the copilot: EOS-only today.
EOS_ONLY = _profile("ops@acme.example", department="Operations")


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """The app's ``SessionLocal`` on a throwaway sqlite file, and the
    feature-policy cache empty before and after."""
    import rubik_core.database.db as dbmod
    from rubik_core.config import settings
    from rubik_core.database import eos  # noqa: F401 — register EOS tables
    from rubik_core.database.models import Base

    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite:///{tmp_path / 'tours.db'}")
    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)
    Base.metadata.create_all(bind=engine)
    clear_cache()
    try:
        yield dbmod.SessionLocal
    finally:
        clear_cache()
        app.dependency_overrides.clear()
        engine.dispose()
        dbmod._engine = None


@pytest.fixture
def as_user():
    def _apply(user: UserProfile) -> TestClient:
        app.dependency_overrides[get_current_user] = lambda: user
        return TestClient(app)

    return _apply


def _rows(session_factory, email: str) -> list[UserTourProgress]:
    db = session_factory()
    try:
        return (
            db.query(UserTourProgress)
            .filter(UserTourProgress.user_email == email)
            .order_by(UserTourProgress.tour_id)
            .all()
        )
    finally:
        db.close()


# ── 1. who ──────────────────────────────────────────────────────────────────


@pytest.fixture
def systems_only(monkeypatch):
    """``page_tours`` ships open to everyone. A case about someone OFF the roster
    installs the narrowed roster an admin would set, as Feature Access data."""
    pin_rosters(
        monkeypatch,
        {
            "page_tours": FeaturePolicy(
                departments=frozenset({"systems"}),
                exec_allowed=False,
                admin_allowed=True,
            )
        },
    )


def test_the_feature_ships_open_to_everyone():
    policy = feature_by_key("page_tours").default()
    assert policy.everyone and not policy.departments
    assert feature_by_key("page_tours").kill_switch is None


@pytest.mark.parametrize("user", [SALES, EXEC, EOS_ONLY])
def test_people_off_the_roster_get_a_403_on_every_route(as_user, user, systems_only):
    client = as_user(user)
    assert client.get(URL).status_code == 403
    assert (
        client.post(
            f"{URL}/seen", json={"tour_ids": ["chat.intro"], "status": "completed"}
        ).status_code
        == 403
    )
    assert client.put(f"{URL}/settings", json={"enabled": False}).status_code == 403
    assert client.delete(f"{URL}/seen").status_code == 403


@pytest.mark.parametrize("user", [SYSTEMS, ADMIN])
def test_the_roster_and_admins_get_in(as_user, user):
    assert as_user(user).get(URL).json() == {"enabled": True, "seen": {}}


def test_an_eos_only_team_added_to_the_roster_gets_in(as_user, fresh_db, monkeypatch):
    """Not mounted on the copilot: with the copilot narrowed so Operations is
    outside it, Operations still reaches the tours once an admin adds it to a
    narrowed tours roster in Feature Access."""
    from backend.tests._rosters import narrow_copilot
    from rubik_core.features import FeaturePolicy, clear_cache, save_policy

    narrow_copilot(monkeypatch, "Sales", "Marketing", "Systems", "Support")

    def narrow_tours(*departments: str) -> None:
        db = fresh_db()
        try:
            save_policy(
                db,
                "page_tours",
                FeaturePolicy(departments=frozenset(departments)),
                actor_email=ADMIN.email,
                note="Narrowed for the test",
            )
        finally:
            db.close()
        clear_cache()

    narrow_tours("systems")
    assert as_user(EOS_ONLY).get(URL).status_code == 403
    narrow_tours("systems", "operations")
    assert as_user(EOS_ONLY).get(URL).status_code == 200


def test_the_router_is_not_mounted_under_the_copilot_gate():
    from backend.auth import require_full_app_access

    for route in app.routes:
        if getattr(route, "path", "").startswith("/api/tours"):
            deps = [d.call for d in route.dependant.dependencies]
            assert require_full_app_access not in deps, route.path
            assert tours_router.require_page_tours in deps, route.path


# ── 2. memory ───────────────────────────────────────────────────────────────


def test_marking_tours_seen_is_per_person_and_lower_cases_the_email(as_user, fresh_db):
    client = as_user(SYSTEMS)
    res = client.post(
        f"{URL}/seen",
        json={
            "tour_ids": ["rocks.intro", "rocks.new.carry-over"],
            "status": "completed",
        },
    )
    assert res.status_code == 200
    assert res.json() == {
        "enabled": True,
        "seen": {"rocks.intro": "completed", "rocks.new.carry-over": "completed"},
    }
    assert client.get(URL).json()["seen"] == res.json()["seen"]
    assert [r.tour_id for r in _rows(fresh_db, "sys.user@acme.example")] == [
        "rocks.intro",
        "rocks.new.carry-over",
    ]
    assert as_user(SYSTEMS_2).get(URL).json()["seen"] == {}


def test_a_finished_tour_stays_finished_when_a_replay_is_skipped(as_user):
    client = as_user(SYSTEMS)
    client.post(f"{URL}/seen", json={"tour_ids": ["chat.intro"], "status": "completed"})
    res = client.post(
        f"{URL}/seen",
        json={"tour_ids": ["chat.intro", "assets.intro"], "status": "skipped"},
    )
    assert res.json()["seen"] == {"chat.intro": "completed", "assets.intro": "skipped"}
    # …while a skipped tour that is later finished becomes finished.
    res = client.post(
        f"{URL}/seen", json={"tour_ids": ["assets.intro"], "status": "completed"}
    )
    assert res.json()["seen"]["assets.intro"] == "completed"


def test_repeated_ids_in_one_request_are_written_once(as_user, fresh_db):
    as_user(SYSTEMS).post(
        f"{URL}/seen",
        json={"tour_ids": ["chat.intro", "chat.intro"], "status": "skipped"},
    )
    assert len(_rows(fresh_db, "sys.user@acme.example")) == 1


def test_forgetting_brings_every_tour_back_but_keeps_the_switch(as_user):
    client = as_user(SYSTEMS)
    client.post(f"{URL}/seen", json={"tour_ids": ["chat.intro"], "status": "completed"})
    client.put(f"{URL}/settings", json={"enabled": False})
    res = client.delete(f"{URL}/seen")
    assert res.status_code == 200
    assert res.json() == {"enabled": False, "seen": {}}


# ── 3. the switch ───────────────────────────────────────────────────────────


def test_the_switch_is_one_reserved_row_and_never_a_seen_tour(as_user, fresh_db):
    client = as_user(SYSTEMS)
    assert client.put(f"{URL}/settings", json={"enabled": False}).json() == {
        "enabled": False,
        "seen": {},
    }
    client.put(f"{URL}/settings", json={"enabled": False})
    rows = _rows(fresh_db, "sys.user@acme.example")
    assert [(r.tour_id, r.status) for r in rows] == [(TOURS_SWITCH_ID, "off")]
    assert client.put(f"{URL}/settings", json={"enabled": True}).json()["enabled"]
    assert _rows(fresh_db, "sys.user@acme.example") == []


def test_the_reserved_switch_id_cannot_be_written_as_a_tour(as_user):
    res = as_user(SYSTEMS).post(
        f"{URL}/seen", json={"tour_ids": [TOURS_SWITCH_ID], "status": "completed"}
    )
    assert res.status_code == 422


# ── 4. bad input ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        {"tour_ids": [], "status": "completed"},
        {"tour_ids": ["chat.intro"], "status": "dismissed"},
        {"tour_ids": ["Chat.Intro"], "status": "completed"},
        {"tour_ids": ["../etc"], "status": "completed"},
        {"tour_ids": ["x" * 121], "status": "completed"},
        {"tour_ids": [f"t{i}.intro" for i in range(51)], "status": "completed"},
    ],
)
def test_malformed_requests_are_refused_and_write_nothing(as_user, fresh_db, body):
    assert as_user(SYSTEMS).post(f"{URL}/seen", json=body).status_code == 422
    assert _rows(fresh_db, "sys.user@acme.example") == []


def test_a_flood_past_the_per_person_cap_is_refused(as_user, fresh_db, monkeypatch):
    monkeypatch.setattr(tours_router, "MAX_ROWS_PER_PERSON", 3)
    client = as_user(SYSTEMS)
    ok = client.post(
        f"{URL}/seen", json={"tour_ids": ["a.intro", "b.intro"], "status": "completed"}
    )
    assert ok.status_code == 200
    res = client.post(
        f"{URL}/seen", json={"tour_ids": ["c.intro", "d.intro"], "status": "completed"}
    )
    assert res.status_code == 422
    assert len(_rows(fresh_db, "sys.user@acme.example")) == 2
    # Re-marking tours already remembered is not new rows, so it still works.
    again = client.post(
        f"{URL}/seen", json={"tour_ids": ["a.intro", "b.intro"], "status": "skipped"}
    )
    assert again.status_code == 200


# ── 5. impersonation ────────────────────────────────────────────────────────


def test_impersonation_cannot_read_or_write_a_persons_tours():
    assert not impersonation.is_allowed("GET", URL)
    assert not impersonation.is_allowed("POST", f"{URL}/seen")
    assert not impersonation.is_allowed("PUT", f"{URL}/settings")
    assert not impersonation.is_allowed("DELETE", f"{URL}/seen")

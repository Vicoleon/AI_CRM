"""Exec-team confidentiality + per-team filters across the EOS surface.

THE rule: Exec team members are the ONLY ones who can see
ANYTHING marked as Exec team — issues, to-dos, measurables, rocks, the Exec
L10 meeting and everything hanging off it (sessions/diary, attendance,
headlines & ideas, the in-meeting items payload). A calendar roster or a
Team-Manager role never overrides that boundary. Alongside, every list
endpoint accepts team filters (``is_exec`` / ``department_name``,
case-insensitive) so multi-team members can scope any page to one team.

No mocks — a temp SQLite DB per test, endpoint-level through TestClient.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from backend.tests._rosters import pin_rosters
from rubik_core.config import settings
from rubik_core.database.schemas import UserProfile

client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db_path = tmp_path / "test_exec_filtering.db"
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    import rubik_core.database.db as dbmod

    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database import eos  # noqa: F401
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    from rubik_core.features import clear_cache

    clear_cache()
    yield
    clear_cache()
    app.dependency_overrides.clear()


def _profile(
    email: str,
    *,
    department: str = "general",
    roles: list[str] | None = None,
    is_exec: bool = False,
    managed: list[str] | None = None,
) -> UserProfile:
    now = "2026-07-14T00:00:00"
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name=email.split("@")[0].title(),
        roles=roles or ["user"],
        department=department,
        is_exec=is_exec,
        managed_departments=managed or [],
        created_at=now,
        updated_at=now,
    )


def _as(profile: UserProfile) -> None:
    app.dependency_overrides[get_current_user] = lambda: profile


def _session():
    from rubik_core.database.db import SessionLocal

    return SessionLocal()


def _seed() -> dict:
    """Sales + HR departments; an Exec L10 and a Sales L10 (each with one
    open session + one headline); exec + sales rows on every resource."""
    from rubik_core.database.eos import (
        Issue,
        L10Input,
        L10InputKind,
        L10Meeting,
        L10Session,
        Organization,
        Person,
        Quarter,
        Rock,
        ScorecardMetric,
        Todo,
        Visibility,
    )
    from rubik_core.database.models import Department

    db = _session()
    ids: dict = {}
    try:
        db.add_all([Department(name="Sales"), Department(name="HR")])
        org = Organization(name="Acme", created_by="seed")
        db.add(org)
        db.commit()
        db.refresh(org)

        quarter = Quarter(
            organization_id=org.id,
            name="Q3-2026",
            start_date=datetime(2026, 7, 1),
            end_date=datetime(2026, 9, 30),
            is_current=True,
            created_by="seed",
        )
        db.add(quarter)

        exec_p = Person(
            first_name="Eve",
            last_name="Exec",
            email="exec@x.example",
            created_by="seed",
        )
        sales_p = Person(
            first_name="Sam",
            last_name="Sales",
            email="sales@x.example",
            created_by="seed",
        )
        db.add_all([exec_p, sales_p])
        db.commit()
        for k, obj in (("quarter", quarter), ("exec_p", exec_p), ("sales_p", sales_p)):
            db.refresh(obj)
            ids[k] = obj.id

        exec_meeting = L10Meeting(
            name="Exec L10", is_exec_meeting=True, created_by="seed"
        )
        # Manually scheduled, so no calendar roster: the TEAM is the roster.
        # Department stored with a different case than user profiles use —
        # matching must be slug/case-insensitive.
        sales_meeting = L10Meeting(
            name="Sales L10",
            department_name="Sales",
            created_by="seed",
        )
        # Calendar-imported sibling: same team, but an invite list of exactly
        # one HR person. The roster is the ACL here — Sales members are NOT
        # folded in (see test_eos_l10_roster_visibility.py).
        imported_meeting = L10Meeting(
            name="Sales Imported L10",
            department_name="Sales",
            roster_emails=["roster@x.example"],
            google_event_id="evt-sales-imported",
            created_by="seed",
        )
        db.add_all([exec_meeting, sales_meeting, imported_meeting])
        db.commit()
        db.refresh(exec_meeting)
        db.refresh(sales_meeting)
        db.refresh(imported_meeting)
        ids["exec_meeting"] = exec_meeting.id
        ids["sales_meeting"] = sales_meeting.id
        ids["imported_meeting"] = imported_meeting.id

        db.add_all(
            [
                L10Session(
                    meeting_id=exec_meeting.id,
                    notes="exec-only notes",
                    created_by="seed",
                ),
                L10Session(
                    meeting_id=sales_meeting.id,
                    notes="sales notes",
                    created_by="seed",
                ),
                L10Input(
                    meeting_id=exec_meeting.id,
                    kind=L10InputKind.HEADLINE,
                    text="exec headline",
                    created_by="seed",
                ),
                L10Input(
                    meeting_id=sales_meeting.id,
                    kind=L10InputKind.HEADLINE,
                    text="sales headline",
                    created_by="seed",
                ),
                Issue(
                    description="exec issue",
                    is_exec=True,
                    owner_id=exec_p.id,
                    created_by="seed",
                ),
                Issue(
                    description="sales issue",
                    department_name="Sales",
                    owner_id=sales_p.id,
                    created_by="seed",
                ),
                Todo(
                    description="exec todo",
                    is_exec=True,
                    owner_id=exec_p.id,
                    created_by="seed",
                ),
                Todo(
                    description="sales todo",
                    department_name="Sales",
                    owner_id=sales_p.id,
                    created_by="seed",
                ),
                ScorecardMetric(
                    name="exec metric",
                    is_exec=True,
                    owner_id=exec_p.id,
                    created_by="seed",
                ),
                ScorecardMetric(
                    name="sales metric",
                    department_name="Sales",
                    owner_id=sales_p.id,
                    created_by="seed",
                ),
                Rock(
                    description="exec rock",
                    is_exec=True,
                    visibility=Visibility.COMPANY,
                    owner_id=exec_p.id,
                    quarter_id=quarter.id,
                    created_by="seed",
                ),
                Rock(
                    description="sales rock",
                    visibility=Visibility.COMPANY,
                    department_name="Sales",
                    owner_id=sales_p.id,
                    quarter_id=quarter.id,
                    created_by="seed",
                ),
            ]
        )
        db.commit()

        from rubik_core.database.eos import (
            Issue as I,
        )
        from rubik_core.database.eos import (
            L10Session as S,
        )
        from rubik_core.database.eos import (
            Rock as R,
        )
        from rubik_core.database.eos import (
            ScorecardMetric as M,
        )
        from rubik_core.database.eos import (
            Todo as T,
        )

        ids["exec_issue"] = db.query(I).filter(I.is_exec.is_(True)).one().id
        ids["sales_issue"] = db.query(I).filter(I.is_exec.is_(False)).one().id
        ids["exec_todo"] = db.query(T).filter(T.is_exec.is_(True)).one().id
        ids["exec_metric"] = db.query(M).filter(M.is_exec.is_(True)).one().id
        ids["exec_rock"] = db.query(R).filter(R.is_exec.is_(True)).one().id
        ids["sales_rock"] = db.query(R).filter(R.is_exec.is_(False)).one().id
        ids["exec_session"] = (
            db.query(S).filter(S.meeting_id == exec_meeting.id).one().id
        )
        ids["sales_session"] = (
            db.query(S).filter(S.meeting_id == sales_meeting.id).one().id
        )
        return ids
    finally:
        db.close()


SALES = dict(department="sales")  # lower-case on purpose: slug-equal to "Sales"
EXEC = dict(department="general", is_exec=True)
# An exec who ALSO belongs to Sales (multi-team exec member).
EXEC_SALES = dict(department="sales", is_exec=True)


# ---- meeting visibility (the Exec L10 is invisible to non-exec) ------------


def test_meetings_list_hides_exec_meeting_from_non_exec():
    ids = _seed()
    _as(_profile("sales@x.example", **SALES))
    r = client.get("/api/eos/l10/")
    assert r.status_code == 200
    returned = {m["id"] for m in r.json()}
    assert str(ids["sales_meeting"]) in returned  # case-insensitive dept match
    assert str(ids["exec_meeting"]) not in returned
    # Same team, but a calendar invite they're not on — not theirs to see.
    assert str(ids["imported_meeting"]) not in returned


def test_meetings_list_shows_exec_meeting_to_exec():
    ids = _seed()
    _as(_profile("exec@x.example", **EXEC))
    r = client.get("/api/eos/l10/")
    returned = {m["id"] for m in r.json()}
    assert str(ids["exec_meeting"]) in returned


def test_roster_grants_department_meeting_but_never_exec():
    ids = _seed()
    # roster@x.example is on the imported meeting's roster but is in HR.
    _as(_profile("roster@x.example", department="HR"))
    returned = {m["id"] for m in client.get("/api/eos/l10/").json()}
    assert str(ids["imported_meeting"]) in returned
    assert str(ids["exec_meeting"]) not in returned
    # …and being invited to one team's meeting is not a key to that team's
    # OTHER meetings: the manually-scheduled Sales L10 stays out of scope.
    assert str(ids["sales_meeting"]) not in returned


def test_team_manager_sees_managed_team_not_exec():
    ids = _seed()
    _as(_profile("mgr@x.example", department="HR", managed=["sales"]))
    returned = {m["id"] for m in client.get("/api/eos/l10/").json()}
    assert str(ids["sales_meeting"]) in returned
    assert str(ids["exec_meeting"]) not in returned


# ---- everything hanging off the Exec meeting is exec-only ------------------


def test_exec_meeting_subresources_404_for_non_exec():
    ids = _seed()
    _as(_profile("sales@x.example", **SALES))
    em, es = ids["exec_meeting"], ids["exec_session"]
    assert client.get(f"/api/eos/l10/meetings/{em}/items").status_code == 404
    assert client.get(f"/api/eos/l10/meetings/{em}/inputs").status_code == 404
    assert client.get(f"/api/eos/l10/meetings/{em}/inputs/history").status_code == 404
    assert (
        client.post(
            f"/api/eos/l10/meetings/{em}/inputs",
            json={"meeting_id": str(em), "kind": "headline", "text": "sneaky"},
        ).status_code
        == 404
    )
    assert client.get(f"/api/eos/l10/sessions/{es}/state").status_code == 404
    assert client.get(f"/api/eos/l10/sessions/{es}/survey").status_code == 404
    assert client.get(f"/api/eos/l10/sessions/{es}/attendance").status_code == 404
    assert (
        client.post(
            "/api/eos/l10/sessions/start",
            json={"meeting_id": str(em), "actual_start": "2026-07-14T09:00:00"},
        ).status_code
        == 404
    )
    # The Exec diary (session notes) never shows up in the sessions list.
    sessions = client.get("/api/eos/l10/sessions").json()
    assert str(es) not in {s["id"] for s in sessions}


def test_exec_meeting_subresources_work_for_exec():
    ids = _seed()
    _as(_profile("exec@x.example", **EXEC))
    em, es = ids["exec_meeting"], ids["exec_session"]
    items = client.get(f"/api/eos/l10/meetings/{em}/items")
    assert items.status_code == 200
    assert {i["description"] for i in items.json()["issues"]} == {"exec issue"}
    inputs = client.get(f"/api/eos/l10/meetings/{em}/inputs")
    assert [i["text"] for i in inputs.json()] == ["exec headline"]
    assert client.get(f"/api/eos/l10/sessions/{es}/state").status_code == 200
    sessions = client.get("/api/eos/l10/sessions").json()
    assert str(es) in {s["id"] for s in sessions}


def test_headline_edit_delete_gated_via_parent_meeting():
    ids = _seed()
    _as(_profile("exec@x.example", **EXEC))
    exec_input = client.get(f"/api/eos/l10/meetings/{ids['exec_meeting']}/inputs")
    input_id = exec_input.json()[0]["id"]

    _as(_profile("sales@x.example", **SALES))
    assert (
        client.patch(
            f"/api/eos/l10/inputs/{input_id}", json={"discussed": True}
        ).status_code
        == 404
    )
    assert client.delete(f"/api/eos/l10/inputs/{input_id}").status_code == 404


# ---- by-id reads are gated (issues / todos / rocks) -------------------------


def test_exec_rows_by_id_404_for_non_exec():
    ids = _seed()
    _as(_profile("sales@x.example", **SALES))
    assert client.get(f"/api/eos/issues/{ids['exec_issue']}").status_code == 404
    assert client.get(f"/api/eos/issues/{ids['exec_issue']}/notes").status_code == 404
    assert (
        client.post(
            f"/api/eos/issues/{ids['exec_issue']}/notes", json={"content": "hi"}
        ).status_code
        == 404
    )
    assert client.get(f"/api/eos/todos/{ids['exec_todo']}").status_code == 404
    assert client.get(f"/api/eos/todos/{ids['exec_todo']}/notes").status_code == 404
    assert (
        client.post(
            f"/api/eos/todos/{ids['exec_todo']}/notes", json={"content": "hi"}
        ).status_code
        == 404
    )
    assert client.get(f"/api/eos/rocks/{ids['exec_rock']}").status_code == 404
    assert client.get(f"/api/eos/rocks/{ids['exec_rock']}/updates").status_code == 404


def test_exec_rows_by_id_readable_by_exec():
    ids = _seed()
    _as(_profile("exec@x.example", **EXEC))
    assert client.get(f"/api/eos/issues/{ids['exec_issue']}").status_code == 200
    assert client.get(f"/api/eos/todos/{ids['exec_todo']}").status_code == 200
    assert client.get(f"/api/eos/rocks/{ids['exec_rock']}").status_code == 200


def _add_exec_rock(ids: dict, *, visibility, owner_key: str) -> uuid.UUID:
    """Add an Exec-team rock with an explicit visibility enum, return its id."""
    from rubik_core.database.eos import Rock, Visibility  # noqa: F401

    db = _session()
    try:
        rock = Rock(
            description="exec dept-visibility rock",
            is_exec=True,
            visibility=visibility,
            owner_id=ids[owner_key],
            quarter_id=ids["quarter"],
            created_by="seed",
        )
        db.add(rock)
        db.commit()
        db.refresh(rock)
        return rock.id
    finally:
        db.close()


def test_exec_sees_department_visibility_exec_rock_outside_own_scope():
    """Regression (Tatum Mercer): an exec who is NOT org-wide ("*") must see
    Exec-team rocks even when the rock has the DEFAULT ``DEPARTMENT`` visibility
    and is owned outside the viewer's own department scope. Before the fix,
    ``visible_rock_ids`` only granted execs COMPANY-visible or own-scope rocks
    (it never OR-ed in ``Rock.is_exec``), so an exec's Rocks page came back
    empty when the ET rocks carried the default DEPARTMENT visibility."""
    from rubik_core.database.eos import Visibility

    ids = _seed()
    # Exactly how the create form saves an "Exec Team Rock" for a single-team
    # user: is_exec=True, DEPARTMENT visibility (model default), dept NULL.
    rock_id = _add_exec_rock(ids, visibility=Visibility.DEPARTMENT, owner_key="exec_p")

    # An exec scoped only to "general" (no "*") — outside the rock's scope.
    _as(_profile("exec@x.example", **EXEC))
    listed = {row["description"] for row in client.get("/api/eos/rocks/").json()}
    assert "exec dept-visibility rock" in listed  # the failing case, now fixed
    assert "exec rock" in listed  # the COMPANY-visible ET rock still shows
    assert client.get(f"/api/eos/rocks/{rock_id}").status_code == 200

    # No leak: a non-exec who does not own it must NOT see it.
    _as(_profile("sales@x.example", **SALES))
    leaked = {row["description"] for row in client.get("/api/eos/rocks/").json()}
    assert "exec dept-visibility rock" not in leaked
    assert client.get(f"/api/eos/rocks/{rock_id}").status_code == 404


# ---- team filters on the list endpoints -------------------------------------


def test_issue_list_team_filters():
    _seed()
    _as(_profile("exec@x.example", **EXEC_SALES))
    execs = client.get("/api/eos/issues/", params={"is_exec": True}).json()
    assert {i["description"] for i in execs} == {"exec issue"}
    # Case-insensitive department filter ("SALES" matches rows tagged "Sales").
    sales = client.get(
        "/api/eos/issues/", params={"department_name": "SALES", "is_exec": False}
    ).json()
    assert {i["description"] for i in sales} == {"sales issue"}


def test_todo_and_metric_list_team_filters():
    _seed()
    _as(_profile("exec@x.example", **EXEC_SALES))
    todos = client.get("/api/eos/todos/", params={"is_exec": True}).json()
    assert {t["description"] for t in todos} == {"exec todo"}
    metrics = client.get(
        "/api/eos/scorecard/metrics", params={"department_name": "sales"}
    ).json()
    assert {m["name"] for m in metrics} == {"sales metric"}


def test_non_exec_filtering_by_exec_team_gets_nothing():
    _seed()
    _as(_profile("sales@x.example", **SALES))
    assert client.get("/api/eos/issues/", params={"is_exec": True}).json() == []
    assert client.get("/api/eos/todos/", params={"is_exec": True}).json() == []


# ---- write-side gates --------------------------------------------------------


def test_non_exec_cannot_create_exec_scoped_rows():
    _seed()
    _as(_profile("sales@x.example", **SALES))
    assert (
        client.post(
            "/api/eos/issues/", json={"description": "sneak", "is_exec": True}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/eos/todos/",
            json={
                "description": "sneak",
                "is_exec": True,
                "owner_email": "sales@x.example",
            },
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/eos/scorecard/metrics",
            json={"name": "sneak", "is_exec": True, "owner_id": str(uuid.uuid4())},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/eos/rocks/",
            json={
                "description": "sneak",
                "is_exec": True,
                "owner_email": "sales@x.example",
            },
        ).status_code
        == 403
    )


def test_exec_can_create_exec_scoped_issue():
    _seed()
    _as(_profile("exec@x.example", **EXEC))
    r = client.post(
        "/api/eos/issues/", json={"description": "new exec issue", "is_exec": True}
    )
    assert r.status_code == 201
    assert r.json()["is_exec"] is True


def test_non_exec_owner_cannot_declassify_exec_rock():
    ids = _seed()
    # exec@x.example's Person owns the exec rock; simulate that user WITHOUT the
    # exec flag (the owner carve-out lets them see/maintain it, but flipping
    # is_exec off would declassify it → 403).
    _as(_profile("exec@x.example", department="general"))
    r = client.patch(f"/api/eos/rocks/{ids['exec_rock']}", json={"is_exec": False})
    assert r.status_code == 403


def test_ids_from_exec_rock_inherits_exec_scope():
    ids = _seed()
    _as(_profile("exec@x.example", **EXEC))
    r = client.post(
        f"/api/eos/rocks/{ids['exec_rock']}/ids",
        json={"description": "spawned from ET rock"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["is_exec"] is True
    assert body["department_name"] is None
    # And the same endpoint is invisible to a non-exec caller.
    _as(_profile("sales@x.example", **SALES))
    r2 = client.post(
        f"/api/eos/rocks/{ids['exec_rock']}/ids", json={"description": "nope"}
    )
    assert r2.status_code == 404


# ---- case-insensitive team scoping in the L10 items payload -----------------


def test_meeting_items_matches_department_case_insensitively():
    ids = _seed()
    db = _session()
    try:
        from rubik_core.database.eos import Issue

        db.add(
            Issue(
                description="lowercase dept issue",
                department_name="Sales",
                created_by="seed",
            )
        )
        db.commit()
    finally:
        db.close()
    _as(_profile("sales@x.example", **SALES))  # user dept "sales", rows "Sales"
    items = client.get(f"/api/eos/l10/meetings/{ids['sales_meeting']}/items")
    assert items.status_code == 200
    descs = {i["description"] for i in items.json()["issues"]}
    assert "sales issue" in descs and "lowercase dept issue" in descs
    assert "exec issue" not in descs


# ---- cross-department isolation (a team never sees ANOTHER team's L10) ------


def test_department_user_cannot_see_other_departments_meeting():
    """A plain department member sees neither the Exec L10 nor an unrelated
    department's L10. Closes the gap left by the exec-only tests above: an HR
    user (not on the Sales roster, not managing Sales) must not see the Sales
    meeting at all."""
    ids = _seed()
    _as(_profile("hr@x.example", department="HR"))
    returned = {m["id"] for m in client.get("/api/eos/l10/").json()}
    assert str(ids["sales_meeting"]) not in returned
    assert str(ids["exec_meeting"]) not in returned


def test_meetings_list_hides_other_teams_from_admin():
    """The heart of the visibility model: a Cubie admin is NOT shown another
    team's L10. Admins are *unrestricted* for rocks / issues / to-dos /
    scorecard, but meetings are membership-only, so each team's L10 (schedule
    + diary) stays private to that team. An admin is an operator, not an
    executive — the org-wide Exec exception below is deliberately not
    extended here."""
    ids = _seed()

    # Bare admin: no department, not on the roster, not the manager.
    _as(_profile("admin@x.example", roles=["admin"]))
    admin_seen = {m["id"] for m in client.get("/api/eos/l10/").json()}
    assert str(ids["sales_meeting"]) not in admin_seen  # another team → hidden
    assert str(ids["exec_meeting"]) not in admin_seen  # not on the Exec team


def test_meetings_list_shows_every_team_to_the_org_wide_exec():
    """The org-wide Exec (``is_exec`` + the ``*`` wildcard) co-leads
    departments they aren't on the calendar invite for, and already reads
    every team's rocks / issues / to-dos / scorecard, so L10 must not hide
    teams from them either. This is the ONLY role that outranks a team's L10
    membership; see
    ``_authz`` for why it is keyed on the role and not on ``admin``."""
    ids = _seed()

    _as(_profile("ceo@x.example", department="general", is_exec=True, managed=["*"]))
    ceo_seen = {m["id"] for m in client.get("/api/eos/l10/").json()}
    assert str(ids["exec_meeting"]) in ceo_seen  # on the Exec team
    assert str(ids["sales_meeting"]) in ceo_seen  # co-leads it → now visible


# ---- Restart (reset a live L10 to the top) — feature `l10_meeting_restart` ---
#
# Who may restart is Admin → Feature Access data (registry key
# ``l10_meeting_restart``), judged on the PERSON, never on the meeting's team.
# It ships to admins only. On top of it the caller must still be able to run
# that meeting (manager, admin or the session's creator), and the Exec team's
# L10 is never restartable.

ADMIN = dict(roles=["admin"])
OPS = dict(department="ops")


def _ops_live_session() -> tuple[str, str]:
    """As an Ops-team admin: create an Ops L10 and open a live session on it.
    Being on the Ops team is what grants session access — admins no longer
    bypass another team's meeting. Returns (meeting_id, session_id)."""
    _as(_profile("admin@acme.example", roles=["admin"], department="ops"))
    m = client.post(
        "/api/eos/l10/",
        json={
            "name": "Ops L10",
            "department_name": "Ops",
            "day_of_week": 2,
            "start_time": "11:00",
            "duration_minutes": 90,
            "is_exec_meeting": False,
        },
    )
    assert m.status_code == 201, m.text
    s = client.post(
        "/api/eos/l10/sessions/start",
        json={"meeting_id": m.json()["id"], "actual_start": "2026-07-15T11:00:00"},
    )
    assert s.status_code == 200, s.text
    return m.json()["id"], s.json()["id"]


def test_restart_resets_the_session_to_top():
    _seed()
    _mid, sid = _ops_live_session()
    # Advance to a later step, then restart back to the Segue.
    adv = client.post(f"/api/eos/l10/sessions/{sid}/state", json={"current_step": 3})
    assert adv.status_code == 200
    r = client.post(f"/api/eos/l10/sessions/{sid}/restart")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["current_step"] == 0
    assert body["is_paused"] is False
    # state_version bumped past the advance write.
    assert body["state_version"] > adv.json()["state_version"]
    # BOTH clocks re-anchor, to the same instant: the step countdown AND the
    # meeting-level clock the pace bar measures the 90-minute box against.
    # Without the latter, a restarted meeting still reads hours over its box.
    assert body["actual_start"] > "2026-07-15T11:00:00"
    assert body["actual_start"] == body["step_started_at"]


def test_restart_publishes_new_meeting_clock_to_pollers():
    """A tab already in the room only sees the shared cursor — so the restarted
    meeting-clock anchor has to travel on the polled state payload, else that
    tab keeps drawing elapsed from the pre-restart start."""
    _seed()
    _mid, sid = _ops_live_session()
    before = client.get(f"/api/eos/l10/sessions/{sid}/state")
    assert before.status_code == 200, before.text
    assert before.json()["actual_start"].startswith("2026-07-15T11:00:00")

    restarted = client.post(f"/api/eos/l10/sessions/{sid}/restart")
    assert restarted.status_code == 200, restarted.text

    after = client.get(f"/api/eos/l10/sessions/{sid}/state")
    assert after.status_code == 200, after.text
    assert after.json()["actual_start"] == restarted.json()["actual_start"]
    assert after.json()["actual_start"] > before.json()["actual_start"]


def test_restart_keeps_checkins_and_attendance():
    """Restart re-anchors the clocks without discarding what the room already
    captured — the promise the confirm dialog makes."""
    ids = _seed()
    _mid, sid = _ops_live_session()
    attendee = str(ids["sales_p"])
    att = client.post(
        f"/api/eos/l10/sessions/{sid}/attendance",
        json={
            "session_id": sid,
            "person_id": attendee,
            "checkin_professional_content": "shipped the pace bar",
            "checkin_professional_rating": 8,
        },
    )
    assert att.status_code == 200, att.text

    assert client.post(f"/api/eos/l10/sessions/{sid}/restart").status_code == 200

    rows = client.get(f"/api/eos/l10/sessions/{sid}/attendance").json()
    kept = [r for r in rows if r["person_id"] == attendee]
    assert len(kept) == 1
    assert kept[0]["checkin_professional_content"] == "shipped the pace bar"
    assert kept[0]["checkin_professional_rating"] == 8


def test_an_admin_restarts_any_teams_live_l10():
    """Judged on the person, not the meeting's team: a Sales admin running the
    Sales L10 may restart it (the old rule refused every team but one)."""
    ids = _seed()
    _as(_profile("admin@acme.example", roles=["admin"], department="sales"))
    # start reuses the seeded open session for this meeting.
    s = client.post(
        "/api/eos/l10/sessions/start",
        json={
            "meeting_id": str(ids["sales_meeting"]),
            "actual_start": "2026-07-15T11:00:00",
        },
    )
    assert s.status_code == 200
    r = client.post(f"/api/eos/l10/sessions/{s.json()['id']}/restart")
    assert r.status_code == 200, r.text


def test_a_team_manager_needs_the_feature_to_restart(monkeypatch):
    """The Ops lead runs the Ops L10, so the meeting gate lets them drive it;
    Restart ships to admins only, so they get the button once an admin adds
    Ops to the feature in Admin → Feature Access."""
    from rubik_core.features import FeaturePolicy

    _seed()
    _mid, sid = _ops_live_session()
    lead = _profile("lead@acme.example", department="ops", managed=["Ops"])

    _as(lead)
    refused = client.post(f"/api/eos/l10/sessions/{sid}/restart")
    assert refused.status_code == 403
    assert "Feature Access" in refused.json()["detail"]

    pin_rosters(
        monkeypatch,
        {"l10_meeting_restart": FeaturePolicy(departments=frozenset({"ops"}))},
    )
    _as(lead)
    assert client.post(f"/api/eos/l10/sessions/{sid}/restart").status_code == 200


def test_the_exec_teams_l10_is_never_restarted(monkeypatch):
    """Not by an exec admin, and not even with the feature open to everyone."""
    from rubik_core.features import FeaturePolicy

    ids = _seed()
    pin_rosters(
        monkeypatch,
        {"l10_meeting_restart": FeaturePolicy(everyone=True, exec_allowed=True)},
    )
    _as(
        _profile(
            "chief@acme.example", roles=["admin"], department="general", is_exec=True
        )
    )
    s = client.post(
        "/api/eos/l10/sessions/start",
        json={
            "meeting_id": str(ids["exec_meeting"]),
            "actual_start": "2026-07-15T11:00:00",
        },
    )
    assert s.status_code == 200, s.text
    r = client.post(f"/api/eos/l10/sessions/{s.json()['id']}/restart")
    assert r.status_code == 403
    assert "Exec" in r.json()["detail"]


def test_restart_requires_manager_admin_or_creator(monkeypatch):
    """An Ops teammate who is neither the manager, the session creator, nor
    an admin can access the meeting but cannot restart it, even with the
    feature open to everyone."""
    from rubik_core.features import FeaturePolicy

    _seed()
    _mid, sid = _ops_live_session()  # created by admin@acme.example
    pin_rosters(monkeypatch, {"l10_meeting_restart": FeaturePolicy(everyone=True)})
    _as(_profile("someone@acme.example", **OPS))
    r = client.post(f"/api/eos/l10/sessions/{sid}/restart")
    assert r.status_code == 403


def test_restart_409_after_session_ended():
    _seed()
    _mid, sid = _ops_live_session()
    assert client.post(f"/api/eos/l10/sessions/{sid}/end").status_code == 200
    r = client.post(f"/api/eos/l10/sessions/{sid}/restart")
    assert r.status_code == 409

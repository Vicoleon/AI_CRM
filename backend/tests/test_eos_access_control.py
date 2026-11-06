"""EOS row-level READ access control (visibility filters).

Unit-tests `backend.routers.eos._authz.visible_*_ids` against a seeded EOS
org, plus an endpoint test proving the list routes actually apply the
filter. No mocks — a temp SQLite DB per test.

Seed shape:
  Departments: Sales, HR
  Seats:   Sales Lead (is_leadership) ← Sales IC (reports to lead) ; HR
  People:  lead@ / ic@ / hr@  (one per seat)
  Rocks:   company, sales-dept, ic-individual, lead-individual, hr-individual
  Issues:  sales, hr, exec (is_exec), unscoped (NULL dept, not exec)
  Todos:   ic (Sales), hr (HR)
  Metrics: sales, hr, exec (is_exec)
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

client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db_path = tmp_path / "test_rlac.db"
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
    roles: list[str] | None = None,
    is_exec: bool = False,
    managed: list[str] | None = None,
) -> UserProfile:
    now = "2026-06-29T00:00:00"
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


def _session():
    from rubik_core.database.db import SessionLocal

    return SessionLocal()


def _seed() -> dict:
    from rubik_core.database.eos import (
        Issue,
        Organization,
        Person,
        Quarter,
        Rock,
        ScorecardMetric,
        Seat,
        Todo,
        Visibility,
    )
    from rubik_core.database.models import Department

    db = _session()
    ids: dict = {}
    try:
        db.add_all([Department(name="Sales"), Department(name="HR")])
        db.commit()

        org = Organization(name="Acme", created_by="seed")
        db.add(org)
        db.commit()
        db.refresh(org)

        q = Quarter(
            organization_id=org.id,
            name="Q3-2026",
            start_date=datetime(2026, 7, 1),
            end_date=datetime(2026, 9, 30),
            is_current=True,
            created_by="seed",
        )
        db.add(q)
        db.commit()
        db.refresh(q)

        lead_seat = Seat(
            department_name="Sales",
            title="Sales Lead",
            is_leadership=True,
            created_by="seed",
        )
        db.add(lead_seat)
        db.commit()
        db.refresh(lead_seat)
        ic_seat = Seat(
            department_name="Sales",
            title="Sales IC",
            reports_to_id=lead_seat.id,
            created_by="seed",
        )
        hr_seat = Seat(department_name="HR", title="HR Person", created_by="seed")
        db.add_all([ic_seat, hr_seat])
        db.commit()
        db.refresh(ic_seat)
        db.refresh(hr_seat)

        lead = Person(
            first_name="Lee",
            last_name="Ader",
            email="lead@x.example",
            seat_id=lead_seat.id,
            created_by="seed",
        )
        ic = Person(
            first_name="Ike",
            last_name="Cee",
            email="ic@x.example",
            seat_id=ic_seat.id,
            created_by="seed",
        )
        hr = Person(
            first_name="Ha",
            last_name="Are",
            email="hr@x.example",
            seat_id=hr_seat.id,
            created_by="seed",
        )
        db.add_all([lead, ic, hr])
        db.commit()
        for p in (lead, ic, hr):
            db.refresh(p)

        def rock(desc, vis, owner):
            r = Rock(
                quarter_id=q.id,
                owner_id=owner.id,
                description=desc,
                visibility=vis,
                created_by="seed",
            )
            db.add(r)
            db.commit()
            db.refresh(r)
            return r.id

        ids["rock_company"] = rock("company rock", Visibility.COMPANY, ic)
        ids["rock_sales_dept"] = rock("sales dept rock", Visibility.DEPARTMENT, ic)
        ids["rock_ic_indiv"] = rock("ic individual", Visibility.INDIVIDUAL, ic)
        ids["rock_lead_indiv"] = rock("lead individual", Visibility.INDIVIDUAL, lead)
        ids["rock_hr_indiv"] = rock("hr individual", Visibility.INDIVIDUAL, hr)

        def issue(desc, dept, is_exec=False, owner=None):
            i = Issue(
                description=desc,
                department_name=dept,
                is_exec=is_exec,
                owner_id=(owner.id if owner else None),
                created_by="seed",
            )
            db.add(i)
            db.commit()
            db.refresh(i)
            return i.id

        ids["issue_sales"] = issue("sales issue", "Sales")
        ids["issue_hr"] = issue("hr issue", "HR")
        ids["issue_exec"] = issue("exec issue", None, is_exec=True)
        ids["issue_unscoped"] = issue("unscoped issue", None, is_exec=False)

        def todo(desc, owner, dept):
            t = Todo(
                description=desc,
                owner_id=owner.id,
                department_name=dept,
                created_by="seed",
            )
            db.add(t)
            db.commit()
            db.refresh(t)
            return t.id

        ids["todo_ic"] = todo("ic todo", ic, "Sales")
        ids["todo_hr"] = todo("hr todo", hr, "HR")

        def metric(name, owner, dept, is_exec=False):
            m = ScorecardMetric(
                name=name,
                owner_id=owner.id,
                department_name=dept,
                is_exec=is_exec,
                created_by="seed",
            )
            db.add(m)
            db.commit()
            db.refresh(m)
            return m.id

        ids["metric_sales"] = metric("sales metric", ic, "Sales")
        ids["metric_hr"] = metric("hr metric", hr, "HR")
        ids["metric_exec"] = metric("exec metric", lead, None, is_exec=True)
        return ids
    finally:
        db.close()


# ---- unrestricted callers -------------------------------------------------


def test_admin_and_ceo_are_unrestricted():
    _seed()
    db = _session()
    try:
        admin = _profile("admin@x.example", roles=["admin"])
        ceo = _profile("ceo@x.example", is_exec=True, managed=["*"])
        for fn in (
            _authz.visible_rock_ids,
            _authz.visible_issue_ids,
            _authz.visible_measurable_ids,
            _authz.visible_todo_ids,
        ):
            assert fn(admin, db) is None
            assert fn(ceo, db) is None
    finally:
        db.close()


# ---- rocks (visibility enum) ----------------------------------------------


def test_ic_rock_visibility():
    ids = _seed()
    db = _session()
    try:
        ic = _profile("ic@x.example", department="Sales")
        vis = set(_authz.visible_rock_ids(ic, db))
        assert ids["rock_company"] in vis
        assert ids["rock_sales_dept"] in vis  # dept rock, owner in Sales
        assert ids["rock_ic_indiv"] in vis  # own individual
        assert ids["rock_lead_indiv"] not in vis  # not a leader → no others'
        assert ids["rock_hr_indiv"] not in vis
    finally:
        db.close()


def test_lead_sees_dept_members_not_exec():
    ids = _seed()
    db = _session()
    try:
        lead = _profile("lead@x.example", department="Sales")  # leadership via seat
        rvis = set(_authz.visible_rock_ids(lead, db))
        assert ids["rock_company"] in rvis
        assert ids["rock_sales_dept"] in rvis
        assert ids["rock_ic_indiv"] in rvis  # leader sees dept members' individual
        assert ids["rock_lead_indiv"] in rvis
        assert ids["rock_hr_indiv"] not in rvis  # other department

        ivis = set(_authz.visible_issue_ids(lead, db))
        assert ids["issue_sales"] in ivis
        # A department lead who is NOT on the Exec team must NOT see Exec-team
        # rows (they don't own issue_exec): the confidential Exec/Leadership
        # surface is exec-only. This decouples "leads a team" from "on the
        # Exec team".
        assert ids["issue_exec"] not in ivis
        assert ids["issue_unscoped"] in ivis
        assert ids["issue_hr"] not in ivis
    finally:
        db.close()


# ---- department-scoped resources ------------------------------------------


def test_ic_issue_and_todo_visibility():
    ids = _seed()
    db = _session()
    try:
        ic = _profile("ic@x.example", department="Sales")
        ivis = set(_authz.visible_issue_ids(ic, db))
        assert ids["issue_sales"] in ivis
        assert ids["issue_unscoped"] in ivis
        assert ids["issue_exec"] not in ivis  # IC is neither exec nor leadership
        assert ids["issue_hr"] not in ivis

        tvis = set(_authz.visible_todo_ids(ic, db))
        assert ids["todo_ic"] in tvis
        assert ids["todo_hr"] not in tvis
    finally:
        db.close()


def test_hr_isolated_from_sales():
    ids = _seed()
    db = _session()
    try:
        hr = _profile("hr@x.example", department="HR")
        rvis = set(_authz.visible_rock_ids(hr, db))
        assert ids["rock_company"] in rvis
        assert ids["rock_hr_indiv"] in rvis
        assert ids["rock_sales_dept"] not in rvis
        assert ids["rock_ic_indiv"] not in rvis

        ivis = set(_authz.visible_issue_ids(hr, db))
        assert ids["issue_hr"] in ivis
        assert ids["issue_sales"] not in ivis
    finally:
        db.close()


def test_exec_user_without_person_sees_exec_rows():
    ids = _seed()
    db = _session()
    try:
        execu = _profile(
            "exec@x.example", department="general", is_exec=True
        )  # no Person
        ivis = set(_authz.visible_issue_ids(execu, db))
        assert ids["issue_exec"] in ivis  # exec-team row
        assert ids["issue_unscoped"] in ivis
        assert ids["issue_sales"] not in ivis  # dept 'general' → no dept match
        assert ids["issue_hr"] not in ivis

        mvis = set(_authz.visible_measurable_ids(execu, db))
        assert ids["metric_exec"] in mvis

        rvis = set(_authz.visible_rock_ids(execu, db))
        assert ids["rock_company"] in rvis  # only company (no seat / dept)
        assert ids["rock_ic_indiv"] not in rvis
    finally:
        db.close()


def test_user_with_no_person_still_sees_own_department():
    ids = _seed()
    db = _session()
    try:
        # A Cubie user in Sales with NO eos_people row — dept comes from profile.
        ghost = _profile("ghost@x.example", department="Sales")
        ivis = set(_authz.visible_issue_ids(ghost, db))
        assert ids["issue_sales"] in ivis  # via UserProfile.department
        assert ids["issue_unscoped"] in ivis
        assert ids["issue_exec"] not in ivis
        assert ids["issue_hr"] not in ivis
    finally:
        db.close()


# ---- endpoint wiring ------------------------------------------------------


def test_list_rocks_endpoint_applies_filter():
    ids = _seed()

    app.dependency_overrides[get_current_user] = lambda: _profile(
        "ic@x.example", department="Sales"
    )
    r = client.get("/api/eos/rocks/")
    assert r.status_code == 200
    returned = {item["id"] for item in r.json()}
    assert str(ids["rock_company"]) in returned
    assert str(ids["rock_hr_indiv"]) not in returned  # filtered out

    # Admin sees everything (None bypass → no filter).
    app.dependency_overrides[get_current_user] = lambda: _profile(
        "admin@x.example", roles=["admin"]
    )
    r = client.get("/api/eos/rocks/")
    assert r.status_code == 200
    returned = {item["id"] for item in r.json()}
    assert str(ids["rock_hr_indiv"]) in returned


# ---- can_mutate (Phase 2 write-gating) ------------------------------------


def _get(db, model, rid):
    return db.query(model).filter(model.id == rid).first()


def test_can_mutate_owner_admin_exec_creator():
    ids = _seed()
    db = _session()
    try:
        from rubik_core.database.eos import Todo

        todo_ic = _get(db, Todo, ids["todo_ic"])  # owner=ic, created_by="seed"
        assert _authz.can_mutate(
            _profile("ic@x.example", department="Sales"), todo_ic, db
        )
        assert _authz.can_mutate(_profile("a@x.example", roles=["admin"]), todo_ic, db)
        assert _authz.can_mutate(_profile("e@x.example", is_exec=True), todo_ic, db)
        # creator (matches created_by) may mutate even with no other relation
        assert _authz.can_mutate(_profile("seed", department="general"), todo_ic, db)
        # unrelated user may not
        assert not _authz.can_mutate(
            _profile("hr@x.example", department="HR"), todo_ic, db
        )
    finally:
        db.close()


def test_can_mutate_manager_over_direct_report():
    ids = _seed()
    db = _session()
    try:
        from rubik_core.database.eos import Todo

        todo_ic = _get(db, Todo, ids["todo_ic"])  # owner = ic, who reports to lead
        lead = _profile("lead@x.example", department="Sales")
        assert _authz.can_mutate(lead, todo_ic, db)  # manager of the owner
    finally:
        db.close()


def test_can_mutate_leadership_vs_regular_on_company_rock():
    ids = _seed()
    db = _session()
    try:
        from rubik_core.database.eos import Rock

        rock_company = _get(db, Rock, ids["rock_company"])  # COMPANY visibility
        # Leadership may mutate; an unrelated non-leader may not.
        assert _authz.can_mutate(
            _profile("lead@x.example", department="Sales"), rock_company, db
        )
        assert not _authz.can_mutate(
            _profile("hr@x.example", department="HR"), rock_company, db
        )
    finally:
        db.close()


def test_update_todo_endpoint_enforces_can_mutate():
    ids = _seed()

    # Unrelated HR user → 403 editing a Sales IC's to-do.
    app.dependency_overrides[get_current_user] = lambda: _profile(
        "hr@x.example", department="HR"
    )
    r = client.patch(
        f"/api/eos/todos/{ids['todo_ic']}", json={"description": "hijacked"}
    )
    assert r.status_code == 403

    # The owner → 200.
    app.dependency_overrides[get_current_user] = lambda: _profile(
        "ic@x.example", department="Sales"
    )
    r = client.patch(
        f"/api/eos/todos/{ids['todo_ic']}", json={"description": "owner edit"}
    )
    assert r.status_code == 200
    assert r.json()["description"] == "owner edit"


# ---- scorecard entry upsert/create write-gating ---------------------------


def test_upsert_entry_endpoint_enforces_can_mutate():
    """POST /entries/upsert must gate on the parent metric's mutate permission.

    Regression for the pre-existing gap where any authenticated user could
    overwrite ANY metric's weekly value (including confidential
    Exec/Leadership-Scorecard rows), because upsert — unlike PATCH
    /entries/{id} — never called ``require_can_mutate``.
    """
    ids = _seed()
    week = "2026-07-06T00:00:00"

    def _upsert(metric_id):
        return client.post(
            "/api/eos/scorecard/entries/upsert",
            json={"metric_id": str(metric_id), "week_start_date": week, "value": "42"},
        )

    # Unrelated HR user → 403 writing a Sales metric, and the confidential
    # Exec/Leadership metric, that they neither own nor manage.
    app.dependency_overrides[get_current_user] = lambda: _profile(
        "hr@x.example", department="HR"
    )
    assert _upsert(ids["metric_sales"]).status_code == 403
    assert _upsert(ids["metric_exec"]).status_code == 403

    # The metric's owner (ic — non-exec, non-leadership) → 200, value lands.
    app.dependency_overrides[get_current_user] = lambda: _profile(
        "ic@x.example", department="Sales"
    )
    r = _upsert(ids["metric_sales"])
    assert r.status_code == 200, r.text
    assert r.json()["value"] == "42"
    # Idempotent second write updates the same row in place (same metric+week).
    r2 = _upsert(ids["metric_sales"])
    assert r2.status_code == 200, r2.text
    assert r2.json()["id"] == r.json()["id"]

    # A non-existent metric → 404 (can't record a value against nothing).
    assert _upsert("00000000-0000-0000-0000-000000000000").status_code == 404


def test_upsert_entry_owner_of_exec_metric_can_write():
    """A non-exec, non-leadership OWNER of a Leadership (is_exec=True) measurable
    must still enter their own weekly number — the confidential flag gates
    strangers, not the owner. Guards the standalone / in-meeting value-entry flow.
    """
    _seed()
    from rubik_core.database.eos import Person, ScorecardMetric

    # ic owns a Leadership-Scorecard measurable (is_exec=True) but is neither
    # exec nor leadership themselves.
    db = _session()
    try:
        ic = db.query(Person).filter(Person.email == "ic@x.example").first()
        m = ScorecardMetric(
            name="ic-owned exec measurable",
            owner_id=ic.id,
            department_name=None,
            is_exec=True,
            created_by="seed",
        )
        db.add(m)
        db.commit()
        db.refresh(m)
        exec_metric_id = m.id
    finally:
        db.close()

    app.dependency_overrides[get_current_user] = lambda: _profile(
        "ic@x.example", department="Sales"
    )
    r = client.post(
        "/api/eos/scorecard/entries/upsert",
        json={
            "metric_id": str(exec_metric_id),
            "week_start_date": "2026-07-06T00:00:00",
            "value": "7",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["value"] == "7"


def test_create_entry_endpoint_enforces_can_mutate():
    """POST /entries (the standalone page's brand-new-cell path) shares the same
    gate as upsert — otherwise a stranger could seed a value on a confidential
    metric via the never-existed-yet path that PATCH /entries/{id} can't reach.
    """
    ids = _seed()
    week = "2026-07-13T00:00:00"

    def _create(metric_id):
        return client.post(
            "/api/eos/scorecard/entries",
            json={"metric_id": str(metric_id), "week_start_date": week, "value": "9"},
        )

    # Unrelated HR user → 403 on a Sales metric and the exec metric.
    app.dependency_overrides[get_current_user] = lambda: _profile(
        "hr@x.example", department="HR"
    )
    assert _create(ids["metric_sales"]).status_code == 403
    assert _create(ids["metric_exec"]).status_code == 403

    # The owner → 201 (create returns 201), value persisted.
    app.dependency_overrides[get_current_user] = lambda: _profile(
        "ic@x.example", department="Sales"
    )
    r = _create(ids["metric_sales"])
    assert r.status_code == 201, r.text
    assert r.json()["value"] == "9"


# ---- Team Manager (managed_departments, non-exec) -------------------------


def test_team_manager_leads_managed_dept_without_exec():
    """A non-exec user with ``managed_departments=["Sales"]`` leads the Sales
    team — sees every Sales member's items — but gets ZERO Exec-team access.

    ``department="general"`` so the ONLY thing making them a Sales leader is
    ``managed_departments`` (not a primary-department membership) and they
    have no EOS seat of their own — exactly the "people-manager who isn't on
    the Exec team" case.
    """
    ids = _seed()
    db = _session()
    try:
        mgr = _profile("mgr@x.example", department="general", managed=["Sales"])

        ivis = set(_authz.visible_issue_ids(mgr, db))
        assert ids["issue_sales"] in ivis  # leads Sales
        assert ids["issue_unscoped"] in ivis  # company-wide
        assert ids["issue_exec"] not in ivis  # NOT on the Exec team
        assert ids["issue_hr"] not in ivis  # other team

        mvis = set(_authz.visible_measurable_ids(mgr, db))
        assert ids["metric_sales"] in mvis
        # Owned by the Sales lead, but flagged is_exec → confidential
        # Leadership Scorecard. Must NOT leak via owner-expansion.
        assert ids["metric_exec"] not in mvis
        assert ids["metric_hr"] not in mvis

        rvis = set(_authz.visible_rock_ids(mgr, db))
        assert ids["rock_company"] in rvis
        assert ids["rock_sales_dept"] in rvis
        # Leadership expansion: sees teammates' INDIVIDUAL rocks on the team
        # they manage, even without an EOS seat of their own.
        assert ids["rock_ic_indiv"] in rvis
        assert ids["rock_lead_indiv"] in rvis
        assert ids["rock_hr_indiv"] not in rvis
    finally:
        db.close()


def test_team_manager_can_mutate_own_team_not_exec():
    ids = _seed()
    db = _session()
    try:
        from rubik_core.database.eos import Issue, ScorecardMetric, Todo

        mgr = _profile("mgr@x.example", department="general", managed=["Sales"])

        todo_ic = _get(db, Todo, ids["todo_ic"])  # Sales team
        assert _authz.can_mutate(mgr, todo_ic, db)  # leads Sales → may manage

        todo_hr = _get(db, Todo, ids["todo_hr"])  # HR — not their team
        assert not _authz.can_mutate(mgr, todo_hr, db)

        issue_exec = _get(db, Issue, ids["issue_exec"])  # is_exec row
        assert not _authz.can_mutate(mgr, issue_exec, db)  # never — not exec

        metric_exec = _get(db, ScorecardMetric, ids["metric_exec"])  # is_exec
        assert not _authz.can_mutate(mgr, metric_exec, db)
    finally:
        db.close()


def test_nonexec_owner_still_sees_own_exec_row():
    """A non-exec who personally OWNS an exec-flagged row still sees it — a
    department head who reports one number on the confidential Leadership
    Scorecard keeps their own number, without seeing the rest of it."""
    ids = _seed()
    db = _session()
    try:
        # The Sales lead is NOT on the Exec team but owns ``metric_exec``.
        lead = _profile("lead@x.example", department="Sales")
        mvis = set(_authz.visible_measurable_ids(lead, db))
        assert ids["metric_exec"] in mvis  # owns it → visible
        assert ids["metric_sales"] in mvis  # leads Sales
        assert ids["metric_hr"] not in mvis  # other team, doesn't own
    finally:
        db.close()


def test_wildcard_without_exec_is_not_orgwide():
    """The ``*`` managed-departments wildcard is Exec-only. A non-exec who
    somehow carries it is NOT org-wide and gains no leadership from it."""
    ids = _seed()
    db = _session()
    try:
        ghost = _profile("wild@x.example", department="general", managed=["*"])
        vis = _authz.visible_issue_ids(ghost, db)
        assert vis is not None  # NOT unrestricted — the filter still applies
        ivis = set(vis)
        assert ids["issue_unscoped"] in ivis  # only company-wide rows
        assert ids["issue_exec"] not in ivis
        assert ids["issue_sales"] not in ivis  # '*' confers no real department
    finally:
        db.close()


# ---- can_edit / can_delete: read, update and delete are three gates --------
#
# The case: inside an L10, six people tried to update ONE rock in five
# minutes — two got 200, four got 403 — and the client rendered every 403 as
# "Failed to save Rock. Verify the owner is a registered user." Two separate
# defects: the message named the wrong cause, and the page offered an Edit
# button the gate would refuse.
#
# The rule itself: the L10 Rock Review is a TEAM activity, so any member of a
# rock's team may UPDATE it. Deleting the team's commitment stays narrow. That
# makes three gates, and these tests pin all three.


def test_teammate_may_update_the_teams_rock_but_not_delete_it():
    """The incident, as the rule now stands."""
    ids = _seed()
    rock_id = ids["rock_sales_dept"]

    # A Sales teammate who owns nothing and leads nothing.
    app.dependency_overrides[get_current_user] = lambda: _profile(
        "teammate@x.example", department="Sales"
    )

    listed = {r["id"]: r for r in client.get("/api/eos/rocks/").json()}
    assert listed[str(rock_id)]["can_edit"] is True
    assert listed[str(rock_id)]["can_delete"] is False

    # Update goes through — this is the L10 Rock Review case.
    r = client.patch(f"/api/eos/rocks/{rock_id}", json={"status": "off_track"})
    assert r.status_code == 200
    assert r.json()["can_delete"] is False

    # Logging weekly progress rides the same grant.
    r = client.post(
        f"/api/eos/rocks/{rock_id}/updates",
        json={"status": "on_track", "percent_complete": 40},
    )
    assert r.status_code == 201

    # Deleting the team's commitment does NOT.
    assert client.delete(f"/api/eos/rocks/{rock_id}").status_code == 403


def test_another_teams_member_may_not_touch_the_rock():
    """Membership is of ONE team — it is not a company-wide write grant."""
    ids = _seed()
    app.dependency_overrides[get_current_user] = lambda: _profile(
        "hr@x.example", department="HR"
    )
    r = client.patch(
        f"/api/eos/rocks/{ids['rock_sales_dept']}", json={"description": "hijacked"}
    )
    assert r.status_code == 403
    assert "permission" in r.json()["detail"].lower()


def test_team_grant_never_reaches_a_personal_or_exec_rock():
    """Write must never exceed read: INDIVIDUAL rocks stay personal."""
    ids = _seed()
    from rubik_core.database.eos import Rock

    db = _session()
    try:
        teammate = _profile("teammate@x.example", department="Sales")
        # The Sales lead's INDIVIDUAL rock — a teammate cannot even read it.
        personal = db.query(Rock).filter(Rock.id == ids["rock_lead_indiv"]).one()
        assert not _authz.can_update_rock(teammate, personal, db)

        # An Exec-team rock in the teammate's own department stays exec-only.
        team_rock = db.query(Rock).filter(Rock.id == ids["rock_sales_dept"]).one()
        assert _authz.can_update_rock(teammate, team_rock, db)
        team_rock.is_exec = True
        assert not _authz.can_update_rock(teammate, team_rock, db)
    finally:
        db.rollback()
        db.close()


def test_rock_write_flags_match_the_single_row_gates():
    """The batched flags must not drift from the rules the endpoints enforce."""
    _seed()
    from rubik_core.database.eos import Rock

    db = _session()
    try:
        rocks = db.query(Rock).all()
        for profile in (
            _profile("ic@x.example", department="Sales"),
            _profile("lead@x.example", department="Sales"),
            _profile("hr@x.example", department="HR"),
            _profile("teammate@x.example", department="Sales"),
            _profile("nobody@x.example", department="general"),
        ):
            flags = _authz.rock_write_flags(profile, rocks, db)
            for rock in rocks:
                may_update, may_delete = flags[rock.id]
                assert may_update == _authz.can_update_rock(profile, rock, db), (
                    f"update: {profile.email} on {rock.description}"
                )
                assert may_delete == _authz.can_mutate(profile, rock, db), (
                    f"delete: {profile.email} on {rock.description}"
                )
    finally:
        db.close()


# ---- scorecard weekly values: the team records them ----------------------
#
# The case: one Sales rep typing the team's weekly numbers had 25 of 38 saves
# refused (403) on teammates' rows, and the page said only "Failed to save
# weekly value". Same shape as the Rock case above, same answer: the TEAM may
# record a measurable's weekly value; changing or deleting the measurable
# itself stays owner / manager / leadership.


def _post_entry(metric_id, value="42", week="2026-10-04T00:00:00"):
    return client.post(
        "/api/eos/scorecard/entries",
        json={"metric_id": str(metric_id), "week_start_date": week, "value": value},
    )


def test_teammate_may_record_the_teams_weekly_values_but_not_edit_the_measurable():
    """The incident, as the rule now stands."""
    ids = _seed()
    metric_id = ids["metric_sales"]  # owned by ic@, Sales team

    # A Sales teammate who owns nothing, manages nobody and leads nothing.
    app.dependency_overrides[get_current_user] = lambda: _profile(
        "teammate@x.example", department="Sales"
    )

    # A brand-new cell (the standalone page's POST) …
    r = _post_entry(metric_id)
    assert r.status_code == 201, r.text
    entry_id = r.json()["id"]
    # … correcting it (the page's PATCH) …
    r = client.patch(f"/api/eos/scorecard/entries/{entry_id}", json={"value": "43"})
    assert r.status_code == 200, r.text
    assert r.json()["value"] == "43"
    # … and the in-meeting panel's upsert all ride the team grant.
    r = client.post(
        "/api/eos/scorecard/entries/upsert",
        json={
            "metric_id": str(metric_id),
            "week_start_date": "2026-10-04T00:00:00",
            "value": "44",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["id"] == entry_id

    # The measurable itself is NOT the team's to rename or delete.
    r = client.patch(
        f"/api/eos/scorecard/metrics/{metric_id}", json={"name": "renamed"}
    )
    assert r.status_code == 403
    assert client.delete(f"/api/eos/scorecard/metrics/{metric_id}").status_code == 403


def test_scorecard_value_grant_stops_at_the_team_and_the_exec_row():
    """Membership is of ONE team, and the Leadership Scorecard stays exec-only."""
    ids = _seed()
    from rubik_core.database.eos import ScorecardMetric

    # Another team's member: refused, and told why rather than "Failed to save".
    app.dependency_overrides[get_current_user] = lambda: _profile(
        "hr@x.example", department="HR"
    )
    r = _post_entry(ids["metric_sales"])
    assert r.status_code == 403
    assert "team" in r.json()["detail"].lower()

    # A Sales teammate on the Exec measurable: refused.
    app.dependency_overrides[get_current_user] = lambda: _profile(
        "teammate@x.example", department="Sales"
    )
    assert _post_entry(ids["metric_exec"]).status_code == 403

    # Even a measurable tagged with the teammate's own team stays closed once
    # it is an Exec row.
    db = _session()
    try:
        teammate = _profile("teammate@x.example", department="Sales")
        metric = (
            db.query(ScorecardMetric)
            .filter(ScorecardMetric.id == ids["metric_sales"])
            .one()
        )
        assert _authz.can_record_scorecard_value(teammate, metric, db)
        metric.is_exec = True
        assert not _authz.can_record_scorecard_value(teammate, metric, db)
    finally:
        db.rollback()
        db.close()


def test_untagged_measurable_belongs_to_its_owners_team():
    """A measurable with no Team falls back to the owner's seat, like a Rock."""
    _seed()
    from rubik_core.database.eos import Person, ScorecardMetric

    db = _session()
    try:
        ic = db.query(Person).filter(Person.email == "ic@x.example").one()
        untagged = ScorecardMetric(
            name="untagged ic measurable",
            owner_id=ic.id,
            department_name=None,
            is_exec=False,
            created_by="seed",
        )
        db.add(untagged)
        db.commit()
        db.refresh(untagged)

        assert _authz.can_record_scorecard_value(
            _profile("teammate@x.example", department="Sales"), untagged, db
        )
        assert not _authz.can_record_scorecard_value(
            _profile("hr@x.example", department="HR"), untagged, db
        )
    finally:
        db.close()


def test_scorecard_value_flags_match_the_single_row_gate():
    """The batched flags (voice commands) must not drift from the endpoint gate."""
    _seed()
    from rubik_core.database.eos import ScorecardMetric

    db = _session()
    try:
        metrics = db.query(ScorecardMetric).all()
        for profile in (
            _profile("ic@x.example", department="Sales"),
            _profile("lead@x.example", department="Sales"),
            _profile("hr@x.example", department="HR"),
            _profile("teammate@x.example", department="Sales"),
            _profile("nobody@x.example", department="general"),
        ):
            flags = _authz.scorecard_value_writable_ids(profile, metrics, db)
            for metric in metrics:
                assert flags[metric.id] == _authz.can_record_scorecard_value(
                    profile, metric, db
                ), f"{profile.email} on {metric.name}"
    finally:
        db.close()

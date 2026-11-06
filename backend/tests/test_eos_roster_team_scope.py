"""Multi-L10 membership: the roster is a team, not just an invite.

Reported by Linden Walsh in Slack: "I'm included in the sales L10 as well as the
Marketing L10, how does it decide which they go to?" — and asked on behalf of
Emery and Rowan, who also sit on several.

It didn't decide. An EOS item's team is a single tag (``department_name`` /
``is_exec``), and every path that *chose* that tag read ``users.department`` +
``managed_departments`` + the caller's seat — never the L10 rosters. So someone
on two L10s got:

  * ONE option in the team picker, which the UI hides entirely below two
    options, so everything filed from the Issues / To-Dos / Rocks pages and
    from Slack ``/todo`` + ``/ids`` landed silently on their home team;
  * only their home team's rows on those standalone pages, even though the
    other team's L10 — which they sit in every week — showed them all of it
    (``get_meeting_items`` is meeting-ACL gated and carries no per-row filter);
  * an empty solved-issue search *inside* that L10, because that one search
    escapes to the RLAC-filtered ``/api/eos/issues/``.

Fix: an admin GRANTS the extra team (Admin → Users → Additional EOS teams,
stored on ``user_preferences.eos_team_optins``), and ``_authz.roster_dept_names``
honours it only while the L10 roster still agrees.

Both halves are load-bearing, and Blair rejected each alone. Roster-only makes a
single guest appearance permanent — "if its a one time thing it should not show
forever". Grant-only rots: nobody remembers to revoke, so a transfer (Harper,
Support → Sales) leaves the old team behind. Intersecting them means a grant is
live exactly while the calendar says they meet with that team.

The guardrails are most of this file: membership must not become leadership,
and it must not reach the Exec team.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from rubik_core.config import settings
from rubik_core.database.schemas import UserProfile

client = TestClient(app)

LINDEN = "lwalsh@x.example"  # Marketing on paper, Sales L10 by roster
SHAY = "shay@x.example"  # Marketing, no Sales roster — the control
ROWAN = "rowan@x.example"  # exec, rostered on Sales
ADMIN = "admin@x.example"  # Cubie admin, grants the teams


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db_path = tmp_path / "test_roster_team_scope.db"
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    settings.EOS_L10_ROSTER_SCOPE_ENABLED = True
    import rubik_core.database.db as dbmod

    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database import eos  # noqa: F401 — register EOS tables
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    yield
    app.dependency_overrides.clear()


def _profile(email: str, *, department: str = "Marketing", **kw) -> UserProfile:
    now = "2026-09-01T00:00:00"
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name=email.split("@")[0].title(),
        roles=kw.pop("roles", None) or ["user"],
        department=department,
        is_exec=kw.pop("is_exec", False),
        managed_departments=kw.pop("managed", None) or [],
        created_at=now,
        updated_at=now,
    )


def _as(profile: UserProfile) -> None:
    app.dependency_overrides[get_current_user] = lambda: profile


def _session():
    from rubik_core.database.db import SessionLocal

    return SessionLocal()


def _seed() -> dict:
    """Sales + Marketing L10s, both calendar-imported. Linden is on both
    rosters but Linden's user row says Marketing. One open Issue per team."""
    from rubik_core.database.eos import Issue, L10Meeting
    from rubik_core.database.models import Department, User

    db = _session()
    ids: dict = {}
    try:
        db.add_all([Department(name="Sales"), Department(name="Marketing")])
        db.add_all(
            [
                User(email=LINDEN, id="lwalsh", name="Linden", department="Marketing"),
                User(email=SHAY, id="shay", name="Shay", department="Marketing"),
                User(email=ROWAN, id="rowan", name="Rowan", department="Sales"),
                User(email=ADMIN, id="admin", name="Admin", department="Systems"),
            ]
        )
        db.commit()

        sales_l10 = L10Meeting(
            name="L10 Meeting",
            department_name="Sales",
            day_of_week=0,
            start_time="14:00",
            # Casing deliberately differs from the users row: Google returns
            # whatever the invite carried.
            roster_emails=[LINDEN.upper(), ROWAN, "harper@x.example"],
            google_event_id="evt-sales",
            created_by="seed",
        )
        mkt_l10 = L10Meeting(
            name="Marketing L10",
            department_name="Marketing",
            day_of_week=3,
            start_time="12:00",
            roster_emails=[LINDEN, SHAY],
            google_event_id="evt-mkt",
            created_by="seed",
        )
        exec_l10 = L10Meeting(
            name="ET L10",
            department_name=None,
            is_exec_meeting=True,
            day_of_week=1,
            start_time="10:00",
            # Linden invited to the Exec L10 as a guest — must grant nothing.
            roster_emails=[LINDEN, ROWAN],
            google_event_id="evt-et",
            created_by="seed",
        )
        db.add_all([sales_l10, mkt_l10, exec_l10])

        sales_issue = Issue(
            title="Sales pipeline stalling",
            description="Sales pipeline stalling",
            department_name="Sales",
            created_by="harper@x.example",
        )
        mkt_issue = Issue(
            title="Campaign copy late",
            description="Campaign copy late",
            department_name="Marketing",
            created_by=SHAY,
        )
        exec_issue = Issue(
            title="Confidential exec matter",
            description="Confidential exec matter",
            is_exec=True,
            created_by=ROWAN,
        )
        db.add_all([sales_issue, mkt_issue, exec_issue])
        db.commit()
        for key, obj in (
            ("sales_issue", sales_issue),
            ("mkt_issue", mkt_issue),
            ("exec_issue", exec_issue),
        ):
            db.refresh(obj)
            ids[key] = str(obj.id)
        return ids
    finally:
        db.close()


def _grant(email: str, departments: list[str]) -> None:
    """Admin grant, written the way the endpoint writes it."""
    from rubik_core.database.models import UserPreference

    db = _session()
    try:
        row = (
            db.query(UserPreference).filter(UserPreference.user_email == email).first()
        )
        if row is None:
            row = UserPreference(user_email=email)
            db.add(row)
        row.eos_team_optins = departments
        db.commit()
    finally:
        db.close()


def _scope_values() -> list[str]:
    r = client.get("/api/eos/me/meeting-scopes")
    assert r.status_code == 200, r.text
    return [s["value"] for s in r.json()["scopes"]]


def _issue_titles() -> set[str]:
    r = client.get("/api/eos/issues/")
    assert r.status_code == 200, r.text
    return {i["title"] for i in r.json()}


# ---- the reported bug ------------------------------------------------------


def test_granted_and_rostered_member_can_pick_the_other_teams_scope():
    """Linden gets BOTH teams once granted — the whole point of the grant.

    Below two options the create form hides the select entirely, so a
    single-scope list is what made the routing silent."""
    _seed()
    _grant(LINDEN, ["Sales"])
    _as(_profile(LINDEN))
    values = _scope_values()
    assert "Marketing" in values
    assert "Sales" in values


def test_granted_member_sees_the_other_teams_issues_on_the_standalone_page():
    """Previously Linden saw all of this inside the Sales L10 and none of it
    on the Issues page — the disagreement that made items look lost."""
    _seed()
    _grant(LINDEN, ["Sales"])
    _as(_profile(LINDEN))
    assert _issue_titles() == {"Sales pipeline stalling", "Campaign copy late"}


# ---- opt-in: the roster alone must not grant anything ----------------------


def test_roster_alone_grants_nothing():
    """THE regression guard for opt-in. Being on the Sales L10 roster makes
    Linden *eligible*; until an admin grants it Linden is exactly where Linden
    was, so a one-off guest seat at another team's L10 can never put that team
    in anyone's picker."""
    _seed()
    _as(_profile(LINDEN))
    assert _scope_values() == ["Marketing"]
    assert _issue_titles() == {"Campaign copy late"}


def test_grant_without_a_roster_grants_nothing():
    """The transfer case: Harper is granted Support but has come off the
    Support L10's invite. The grant goes inert on its own — nobody has to remember to
    revoke it."""
    _seed()
    _grant(SHAY, ["Sales"])  # granted, but Shay is on no Sales roster
    _as(_profile(SHAY))
    assert _scope_values() == ["Marketing"]
    assert _issue_titles() == {"Campaign copy late"}


def test_non_rostered_teammate_is_unaffected():
    """Shay is Marketing with no Sales roster and no grant: nobody else moves."""
    _seed()
    _as(_profile(SHAY))
    assert _scope_values() == ["Marketing"]
    assert _issue_titles() == {"Campaign copy late"}


# ---- the guardrails --------------------------------------------------------


def test_roster_never_grants_exec_team_access():
    """Linden is on the ET L10 roster as a guest, and granted 'Exec team' by a
    (hypothetically) careless admin. An Exec meeting contributes no department,
    so it can never make anyone eligible — the same rule
    ``_meeting_accessible`` enforces for reads."""
    _seed()
    _grant(LINDEN, ["Sales", "__exec__", "Exec team"])
    _as(_profile(LINDEN))
    assert "__exec__" not in _scope_values()
    assert "Confidential exec matter" not in _issue_titles()


def test_grant_does_not_confer_leadership():
    """The cheap alternative was adding Sales to Linden's managed_departments,
    which flips ``is_leadership`` and hands Linden PATCH/DELETE over every
    Sales person's rows. The grant path must not do that."""
    from backend.routers.eos._authz import compute_scope

    _seed()
    _grant(LINDEN, ["Sales"])
    db = _session()
    try:
        scope = compute_scope(_profile(LINDEN), db)
        assert "Sales" in scope.dept_names  # member: can see + file
        assert scope.led_dept_names == set()  # but leads nothing
        assert scope.is_leader is False
    finally:
        db.close()


def test_granted_team_is_not_offered_for_scheduling():
    """Filing scope, not scheduling scope: ``_require_department_scope`` would
    403 a Sales meeting create, so the scope must be flagged unschedulable
    rather than offered and rejected."""
    _seed()
    _grant(LINDEN, ["Sales"])
    _as(_profile(LINDEN))
    r = client.get("/api/eos/me/meeting-scopes")
    by_value = {s["value"]: s for s in r.json()["scopes"]}
    assert by_value["Marketing"]["can_schedule"] is True
    assert by_value["Sales"]["can_schedule"] is False


def test_rosterless_meeting_never_makes_anyone_eligible():
    """A manually-scheduled meeting has no invite list, so it makes no
    membership claim even for someone granted that department."""
    from rubik_core.database.eos import L10Meeting

    _seed()
    db = _session()
    try:
        db.add(
            L10Meeting(
                name="Ops sync",
                department_name="Sales",
                day_of_week=2,
                start_time="09:00",
                created_by="seed",  # no roster_emails
            )
        )
        db.commit()
    finally:
        db.close()

    _grant(SHAY, ["Sales"])
    _as(_profile(SHAY))
    assert "Sales" not in _scope_values()


def test_kill_switch_restores_the_old_behaviour():
    """The flag is a revert lever, not a rollout gate — but it must actually
    revert, without a redeploy."""
    _seed()
    _grant(LINDEN, ["Sales"])
    settings.EOS_L10_ROSTER_SCOPE_ENABLED = False
    try:
        _as(_profile(LINDEN))
        assert _scope_values() == ["Marketing"]
        assert _issue_titles() == {"Campaign copy late"}
    finally:
        settings.EOS_L10_ROSTER_SCOPE_ENABLED = True


# ---- which scope the list views open on -------------------------------------


def test_own_department_is_flagged_primary():
    """The list views default their team filter to `is_primary` rather than to
    "All my teams". Without it, the first grant makes the Issues page look like
    another team's rows leaked in — reported from the Marketing Issues page the
    day after the first grants went out."""
    _seed()
    _grant(LINDEN, ["Sales"])
    _as(_profile(LINDEN))
    r = client.get("/api/eos/me/meeting-scopes")
    by_value = {s["value"]: s for s in r.json()["scopes"]}
    assert by_value["Marketing"]["is_primary"] is True
    # A granted team is emphatically NOT the default view.
    assert by_value["Sales"]["is_primary"] is False


def test_exec_scope_is_never_primary():
    """Nobody's home team is the Exec team — an exec's list views should open
    on their own department, not on the confidential Leadership surface."""
    _seed()
    _as(_profile(ROWAN, department="Sales", is_exec=True))
    scopes = client.get("/api/eos/me/meeting-scopes").json()["scopes"]
    primary = [s for s in scopes if s["is_primary"]]
    assert [s["value"] for s in primary] == ["Sales"]
    assert all(s["kind"] != "exec" for s in primary)


# ---- the admin surface -----------------------------------------------------


def test_admin_sees_only_eligible_teams_for_a_user():
    """The picker lists what the rosters actually support, so an admin can't
    grant a team the person doesn't meet with."""
    _seed()
    _as(_profile(ADMIN, department="Systems", roles=["admin", "user"]))
    r = client.get(f"/api/users/{LINDEN}/eos-teams")
    assert r.status_code == 200, r.text
    body = r.json()
    # Marketing is on Linden's rosters too, but it's already Linden's own
    # department — "Additional" means additional, so only Sales is offerable.
    assert [t["department"] for t in body["eligible"]] == ["Sales"]
    assert body["eligible"][0]["granted"] is False


def test_admin_grant_takes_effect_and_ignores_ineligible_names():
    """A name matching no current roster is reported back, never stored:
    'I granted Sales and nothing happened' has to be answerable."""
    _seed()
    _as(_profile(ADMIN, department="Systems", roles=["admin", "user"]))
    r = client.put(
        f"/api/users/{LINDEN}/eos-teams",
        json={"departments": ["Sales", "HR"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ignored"] == ["HR"]
    assert body["eligible"] == [{"department": "Sales", "granted": True}]

    _as(_profile(LINDEN))
    assert "Sales" in _scope_values()


def test_admin_can_revoke():
    _seed()
    _grant(LINDEN, ["Sales"])
    _as(_profile(ADMIN, department="Systems", roles=["admin", "user"]))
    r = client.put(f"/api/users/{LINDEN}/eos-teams", json={"departments": []})
    assert r.status_code == 200, r.text
    _as(_profile(LINDEN))
    assert _scope_values() == ["Marketing"]


def test_non_admin_cannot_grant_themselves_a_team():
    _seed()
    _as(_profile(LINDEN))
    r = client.put(f"/api/users/{LINDEN}/eos-teams", json={"departments": ["Sales"]})
    assert r.status_code in (401, 403), r.text

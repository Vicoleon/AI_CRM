"""EOS → Rocks: carry an unfinished rock over to a later quarter.

At quarter end an unfinished Rock is dropped or carried into the next quarter,
usually re-dated and re-scoped. These tests pin the rules:

* carrying over CREATES A NEW rock in the target quarter; the source keeps its
  quarter, status, milestones and history as the record of what happened
* the default target is the quarter after the source's, or today's quarter if
  that is later; an explicit pick must be strictly later (400 otherwise)
* one live carry-over per rock (409), freed again when the carried rock is
  deleted; the link (``eos_rock_carryovers``) shows on both rocks only while
  both are live
* source milestones are never moved: the new rock gets brand-new rows
* the same gates as editing a rock, and the Exec-team fence (404 / 403)
* the AI re-draft keeps due dates inside the target quarter only, returns open
  milestones, and has its own AI Cost category

Gemini is never called: ``rubik_core.models.get_genai_client`` is stubbed.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from backend.tests._legacy_brand import LEGACY_PATTERN
from rubik_core.config import settings
from rubik_core.database.schemas import UserProfile

client = TestClient(app)

ADMIN = "admin@example.com"
EXEC = "exec@example.com"
MEMBER = "member@example.com"  # a Systems teammate with no special role
OUTSIDER = "outsider@example.com"  # a Sales person: reads company rocks only

TODAY = "backend.routers.eos.rocks._today"  # patched by string: modules reload
GENAI_CLIENT = "rubik_core.models.get_genai_client"


def _profile(email: str, *, roles=None, department="Systems", is_exec=False):
    now = "2026-10-09T00:00:00"
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


def _as_admin() -> None:
    _as(ADMIN, roles=["admin"])


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """A throwaway SQLite file, a hard guard against the dev Cloud SQL, and
    "today" pinned to 2026-10-09 (Q4 2026)."""
    monkeypatch.setattr(settings, "CLOUDSQL_CONNECTION_NAME", "")
    monkeypatch.setattr(
        settings, "DATABASE_URL", f"sqlite:///{tmp_path / 'rock_carryover.db'}"
    )
    import rubik_core.database.db as dbmod

    dbmod._engine = None
    engine = dbmod.get_engine()
    assert engine.dialect.name == "sqlite", "tests must never touch Cloud SQL"
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database import eos  # noqa: F401 — registers the eos_* tables
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(TODAY, lambda: date(2026, 10, 9))
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def seeded() -> dict[str, str]:
    """Org; Q2, Q3 and (current) Q4 2026; Systems and Sales seats; a seated
    Systems owner, a Systems teammate (MEMBER) and a Sales person (OUTSIDER)."""
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import Organization, Person, Quarter, Seat
    from rubik_core.database.models import Department

    db = SessionLocal()
    try:
        org = Organization(name="Carry-over Org", created_by=ADMIN)
        db.add(org)
        db.add(Department(name="Systems"))
        db.add(Department(name="Sales"))
        db.commit()
        db.refresh(org)
        quarters = {}
        for key, q, current in (("q2", 2, False), ("q3", 3, False), ("q4", 4, True)):
            start_month = 3 * (q - 1) + 1
            quarters[key] = Quarter(
                organization_id=org.id,
                name=f"Q{q} 2026",
                start_date=datetime(2026, start_month, 1),
                end_date=datetime(2026, start_month + 2, 28),
                is_current=current,
                created_by=ADMIN,
            )
        systems = Seat(department_name="Systems", title="AI Lead", created_by=ADMIN)
        sales = Seat(department_name="Sales", title="AE", created_by=ADMIN)
        db.add_all([*quarters.values(), systems, sales])
        db.commit()
        owner = Person(
            first_name="Seated",
            last_name="Owner",
            email="owner@example.com",
            seat_id=systems.id,
            created_by=ADMIN,
        )
        member = Person(
            first_name="Team",
            last_name="Mate",
            email=MEMBER,
            seat_id=systems.id,
            created_by=ADMIN,
        )
        outsider = Person(
            first_name="Sales",
            last_name="Person",
            email=OUTSIDER,
            seat_id=sales.id,
            created_by=ADMIN,
        )
        db.add_all([owner, member, outsider])
        db.commit()
        return {
            **{key: str(qtr.id) for key, qtr in quarters.items()},
            "owner": str(owner.id),
            "outsider": str(outsider.id),
        }
    finally:
        db.close()


def _insert_rock(seeded, *, quarter="q3", milestones=True, **cols) -> str:
    """Seed a rock straight into the table: off track, one milestone done and
    one still open."""
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import Rock, RockMilestone, RockStatus, Visibility

    db = SessionLocal()
    try:
        rock = Rock(
            quarter_id=UUID(seeded[quarter]),
            owner_id=UUID(cols.pop("owner_id", seeded["owner"])),
            description=cols.pop("description", "Launch the AI pilot by Sep 30"),
            status=cols.pop("status", RockStatus.OFF_TRACK),
            visibility=cols.pop("visibility", Visibility.DEPARTMENT),
            department_name=cols.pop("department_name", "Systems"),
            created_by=cols.pop("created_by", "someone@example.com"),
            **cols,
        )
        if milestones:
            rock.milestones = [
                RockMilestone(
                    description="Sign the pilot spec",
                    completed=True,
                    due_date=date(2026, 8, 1),
                    sort_order=0,
                ),
                RockMilestone(
                    description="Pilot live for Sales",
                    completed=False,
                    due_date=date(2026, 9, 15),
                    sort_order=1,
                ),
            ]
        db.add(rock)
        db.commit()
        return str(rock.id)
    finally:
        db.close()


def _add_weekly_update(rock_id: str, comment: str, day: date) -> None:
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import RockStatus, RockWeeklyUpdate

    db = SessionLocal()
    try:
        db.add(
            RockWeeklyUpdate(
                rock_id=UUID(rock_id),
                status=RockStatus.OFF_TRACK,
                percent_complete=40,
                comment=comment,
                update_date=day,
                created_by=ADMIN,
            )
        )
        db.commit()
    finally:
        db.close()


def _db_rock(rock_id: str) -> dict:
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import Rock

    db = SessionLocal()
    try:
        r = db.query(Rock).filter(Rock.id == UUID(rock_id)).one()
        return {
            "quarter_id": str(r.quarter_id),
            "quarter_name": r.quarter.name,
            "quarter_start": r.quarter.start_date,
            "status": r.status.value,
            "description": r.description,
            "milestones": [
                (str(m.id), m.description, m.completed) for m in r.milestones
            ],
        }
    finally:
        db.close()


def _counts() -> tuple[int, int]:
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import Rock, RockCarryover

    db = SessionLocal()
    try:
        return db.query(Rock).count(), db.query(RockCarryover).count()
    finally:
        db.close()


def _carry(rock_id: str, **payload):
    payload.setdefault("description", "Launch the AI pilot by Dec 31")
    return client.post(f"/api/eos/rocks/{rock_id}/carry-over", json=payload)


def _carried(rock_id: str, **payload) -> dict:
    r = _carry(rock_id, **payload)
    assert r.status_code == 201, r.text
    return r.json()


# -- quarter math ------------------------------------------------------------


def test_default_target_is_the_later_of_next_quarter_and_today():
    from backend.routers.eos.rocks import _default_carry_target

    today = date(2026, 10, 9)  # Q4 2026
    assert _default_carry_target(datetime(2026, 7, 1), today) == (2026, 4)
    assert _default_carry_target(datetime(2026, 10, 1), today) == (2027, 1)
    # Q3 is already over: a Q2 rock carried in October lands in Q4.
    assert _default_carry_target(datetime(2026, 4, 1), today) == (2026, 4)
    assert _default_carry_target(datetime(2025, 10, 1), date(2026, 1, 2)) == (2026, 1)


# -- carry over --------------------------------------------------------------


def test_carry_over_creates_a_new_rock_and_leaves_the_source_untouched(seeded):
    source_id = _insert_rock(seeded)
    before = _db_rock(source_id)
    _as_admin()

    body = _carried(
        source_id,
        milestones=[{"description": "Pilot live for Sales", "due_date": "2026-11-15"}],
    )

    assert body["id"] != source_id
    assert body["quarter_id"] == seeded["q4"], "Q3 rock defaults to Q4 (today's)"
    assert body["description"] == "Launch the AI pilot by Dec 31"
    assert body["status"] == "on_track"
    assert body["owner_id"] == seeded["owner"]
    assert body["department_name"] == "Systems"
    assert body["visibility"] == "department"
    assert body["is_exec"] is False
    assert [
        (m["description"], m["completed"], m["due_date"]) for m in body["milestones"]
    ] == [("Pilot live for Sales", False, "2026-11-15")]
    assert body["carried_from_rock_id"] == source_id
    assert body["carried_from_quarter_id"] == seeded["q3"]
    assert body["carried_from_quarter_name"] == "Q3 2026"
    assert body["carried_to_rock_id"] is None
    assert body["can_edit"] is True and body["can_delete"] is True

    # The source is the record of what happened: nothing about it moved.
    assert _db_rock(source_id) == before
    assert before["status"] == "off_track"
    assert len(before["milestones"]) == 2


def test_default_target_after_a_current_quarter_rock_is_next_quarter(seeded):
    source_id = _insert_rock(seeded, quarter="q4")
    _as_admin()
    body = _carried(source_id)
    new = _db_rock(body["id"])
    assert new["quarter_name"] == "Q1 2027"
    assert new["quarter_start"] == datetime(2027, 1, 1)


def test_default_target_skips_quarters_that_are_already_over(seeded, monkeypatch):
    _as_admin()
    q2_rock = _insert_rock(seeded, quarter="q2")
    assert _carried(q2_rock)["quarter_id"] == seeded["q4"], "not Q3, it's over"

    # The same rule follows "today": in February a Q3 rock lands in Q1 2027.
    monkeypatch.setattr(TODAY, lambda: date(2027, 2, 1))
    q3_rock = _insert_rock(seeded, quarter="q3")
    assert _db_rock(_carried(q3_rock)["id"])["quarter_name"] == "Q1 2027"


def test_an_explicit_quarter_is_honoured(seeded):
    source_id = _insert_rock(seeded)
    _as_admin()
    body = _carried(source_id, quarter_year=2027, quarter_number=2)
    new = _db_rock(body["id"])
    assert new["quarter_name"] == "Q2 2027"
    assert new["quarter_start"] == datetime(2027, 4, 1)


@pytest.mark.parametrize("year,q", [(2026, 3), (2026, 2), (2025, 4)])
def test_the_same_or_an_earlier_quarter_is_refused(seeded, year, q):
    source_id = _insert_rock(seeded)
    _as_admin()
    before = _counts()
    r = _carry(source_id, quarter_year=year, quarter_number=q)
    assert r.status_code == 400, r.text
    assert "Pick a quarter after Q3 2026" in r.json()["detail"]
    assert _counts() == before, "nothing is created on a refusal"


def test_a_blank_description_is_refused(seeded):
    source_id = _insert_rock(seeded)
    _as_admin()
    r = _carry(source_id, description="   ")
    assert r.status_code == 422, r.text


def test_one_live_carry_over_per_rock_freed_by_deleting_the_copy(seeded):
    source_id = _insert_rock(seeded)
    _as_admin()
    first = _carried(source_id)

    r = _carry(source_id, quarter_year=2027, quarter_number=1)
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "This rock was already carried over to Q4 2026."

    assert client.delete(f"/api/eos/rocks/{first['id']}").status_code == 204
    second = _carried(source_id)
    assert second["id"] != first["id"]
    source = client.get(f"/api/eos/rocks/{source_id}").json()
    assert source["carried_to_rock_id"] == second["id"]


def test_a_carry_over_saved_meanwhile_is_caught_before_the_insert(seeded, monkeypatch):
    """Two people saving the same rock's carry-over at once: the second one
    passes the first check, then finds the first one's link when it re-checks
    under the row lock, and answers 409 instead of making a second copy.
    Simulated by committing a competing carry-over while the target quarter is
    being resolved (the step between the two checks)."""
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import RockCarryover

    source_id = _insert_rock(seeded)
    rival_id = _insert_rock(seeded, quarter="q4", milestones=False)
    target = "backend.routers.eos.rocks._resolve_or_create_quarter"
    import backend.routers.eos.rocks as rocks_mod

    original = rocks_mod._resolve_or_create_quarter

    def resolve_while_a_rival_saves(db, year, q, user):
        other = SessionLocal()
        try:
            other.add(
                RockCarryover(
                    source_rock_id=UUID(source_id),
                    carried_rock_id=UUID(rival_id),
                    created_by="rival@example.com",
                )
            )
            other.commit()
        finally:
            other.close()
        return original(db, year, q, user)

    monkeypatch.setattr(target, resolve_while_a_rival_saves)
    _as_admin()
    rocks_before, links_before = _counts()
    r = _carry(source_id)
    assert r.status_code == 409, r.text
    # No second copy: only the rival's link was added.
    assert _counts() == (rocks_before, links_before + 1)
    assert client.get(f"/api/eos/rocks/{source_id}").json()["carried_to_rock_id"] == (
        rival_id
    )


def test_a_quarter_year_out_of_range_is_a_422_not_a_500(seeded):
    source_id = _insert_rock(seeded)
    _as_admin()
    assert _carry(source_id, quarter_year=99999, quarter_number=1).status_code == 422
    r = client.post(
        f"/api/eos/rocks/{source_id}/carry-over/ai-draft",
        json={"quarter_year": 0, "quarter_number": 1},
    )
    assert r.status_code == 422


def test_source_milestones_are_never_reattached(seeded):
    source_id = _insert_rock(seeded)
    before = _db_rock(source_id)
    open_id = before["milestones"][1][0]
    _as_admin()

    body = _carried(
        source_id,
        milestones=[{"id": open_id, "description": "Pilot live", "sort_order": 7}],
    )

    assert len(body["milestones"]) == 1
    assert body["milestones"][0]["id"] != open_id, "a NEW row on the new rock"
    assert body["milestones"][0]["sort_order"] == 0
    assert _db_rock(source_id)["milestones"] == before["milestones"]


def test_team_tag_rules_still_apply(seeded):
    _as_admin()
    # Left out: the carried rock keeps the source's team, even a different one.
    tagged = _insert_rock(seeded, department_name="Sales")
    assert _carried(tagged)["department_name"] == "Sales"
    # Sent as null on a Department rock: the owner's seat department.
    untagged = _insert_rock(seeded, department_name="Sales")
    assert _carried(untagged, department_name=None)["department_name"] == "Systems"

    # An Exec rock never carries a team, whatever the dialog sent.
    _as(EXEC, is_exec=True)
    exec_rock = _insert_rock(seeded, is_exec=True, department_name=None)
    body = _carried(exec_rock, department_name="Systems")
    assert body["is_exec"] is True
    assert body["department_name"] is None


def test_a_teammate_may_carry_a_team_rock(seeded):
    source_id = _insert_rock(seeded)
    _as(MEMBER)
    body = _carried(source_id)
    assert body["carried_from_rock_id"] == source_id


def test_a_viewer_who_cannot_update_the_rock_gets_403(seeded):
    from rubik_core.database.eos import Visibility

    source_id = _insert_rock(seeded, visibility=Visibility.COMPANY)
    _as(OUTSIDER, department="Sales")
    assert client.get(f"/api/eos/rocks/{source_id}").status_code == 200
    assert _carry(source_id).status_code == 403
    r = client.post(f"/api/eos/rocks/{source_id}/carry-over/ai-draft", json={})
    assert r.status_code == 403


def test_a_non_exec_gets_404_for_an_exec_rock(seeded):
    source_id = _insert_rock(seeded, is_exec=True, department_name=None)
    _as(MEMBER)
    before = _counts()
    assert _carry(source_id).status_code == 404
    r = client.post(f"/api/eos/rocks/{source_id}/carry-over/ai-draft", json={})
    assert r.status_code == 404
    assert _counts() == before


def test_a_non_exec_cannot_carry_a_rock_into_the_exec_team(seeded):
    source_id = _insert_rock(seeded)
    _as(MEMBER)
    before = _counts()
    assert _carry(source_id, is_exec=True).status_code == 403
    assert _counts() == before


def test_a_non_exec_owner_carries_their_exec_rock_over_as_an_exec_rock(seeded):
    """The owner of an Exec rock may edit it without being an exec, so the page
    shows them the Carry-over button; the save must not then answer 403. The
    copy stays an Exec rock, and taking it OUT of the Exec team stays exec-only."""
    source_id = _insert_rock(seeded, is_exec=True, department_name=None)
    _as("owner@example.com")
    assert client.get(f"/api/eos/rocks/{source_id}").json()["can_edit"] is True
    before = _counts()
    assert _carry(source_id, is_exec=False).status_code == 403
    assert _counts() == before
    body = _carried(source_id)
    assert body["is_exec"] is True
    assert body["department_name"] is None
    # Their own Exec rock: they keep the owner's grant on the copy.
    assert body["can_edit"] is True


def test_the_response_reports_the_callers_real_grant_on_the_copy(seeded):
    """Handing a carried Exec rock to someone else leaves its non-exec carrier
    with no grant on the copy; the response must say so, or the page offers
    Edit and Delete that both answer 403."""
    source_id = _insert_rock(seeded, is_exec=True, department_name=None)
    _as("owner@example.com")
    body = _carried(source_id, owner_email=MEMBER)
    assert body["is_exec"] is True
    assert body["can_edit"] is False
    assert body["can_delete"] is False
    assert client.patch(
        f"/api/eos/rocks/{body['id']}", json={"description": "x"}
    ).status_code in (403, 404)


# -- the link on reads -------------------------------------------------------


def test_list_get_and_patch_show_both_sides_of_the_link(seeded):
    source_id = _insert_rock(seeded)
    other_id = _insert_rock(seeded, description="Unrelated rock")
    _as_admin()
    new_id = _carried(source_id)["id"]

    by_id = {r["id"]: r for r in client.get("/api/eos/rocks/").json()}
    assert by_id[source_id]["carried_to_rock_id"] == new_id
    assert by_id[source_id]["carried_to_quarter_id"] == seeded["q4"]
    assert by_id[source_id]["carried_to_quarter_name"] == "Q4 2026"
    assert by_id[source_id]["carried_from_rock_id"] is None
    assert by_id[new_id]["carried_from_rock_id"] == source_id
    assert by_id[new_id]["carried_from_quarter_name"] == "Q3 2026"
    assert by_id[new_id]["carried_to_rock_id"] is None
    assert by_id[other_id]["carried_to_rock_id"] is None
    assert by_id[other_id]["carried_from_rock_id"] is None

    source = client.get(f"/api/eos/rocks/{source_id}").json()
    assert source["carried_to_quarter_name"] == "Q4 2026"
    new = client.get(f"/api/eos/rocks/{new_id}").json()
    assert new["carried_from_quarter_name"] == "Q3 2026"

    r = client.patch(f"/api/eos/rocks/{source_id}", json={"status": "done"})
    assert r.status_code == 200, r.text
    assert r.json()["carried_to_rock_id"] == new_id


def test_the_link_disappears_when_either_rock_is_deleted(seeded):
    _as_admin()
    source_id = _insert_rock(seeded)
    new_id = _carried(source_id)["id"]
    assert client.delete(f"/api/eos/rocks/{source_id}").status_code == 204
    new = client.get(f"/api/eos/rocks/{new_id}").json()
    assert new["carried_from_rock_id"] is None
    assert new["carried_from_quarter_name"] is None

    source_id = _insert_rock(seeded)
    new_id = _carried(source_id)["id"]
    assert client.delete(f"/api/eos/rocks/{new_id}").status_code == 204
    source = client.get(f"/api/eos/rocks/{source_id}").json()
    assert source["carried_to_rock_id"] is None
    assert source["carried_to_quarter_name"] is None


# -- AI re-draft -------------------------------------------------------------


def _stub_gemini(monkeypatch, answer: str) -> dict:
    """Stub the Vertex client; returns what the call saw (prompt, AI Cost
    feature) once it has run."""
    from rubik_core.cost.usage import current_feature

    seen: dict = {}

    async def _generate_content(*, model, contents, config):
        seen["prompt"] = contents
        seen["feature"] = current_feature.get()
        seen["config"] = config
        return SimpleNamespace(text=answer)

    fake = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=_generate_content))
    )
    monkeypatch.setattr(GENAI_CLIENT, lambda: fake)
    return seen


def _ai_draft(rock_id: str, **payload):
    return client.post(f"/api/eos/rocks/{rock_id}/carry-over/ai-draft", json=payload)


def test_ai_draft_returns_open_milestones_dated_inside_the_target(seeded, monkeypatch):
    source_id = _insert_rock(seeded)
    answer = {
        "description": "Pilot live for Sales by December 31, 2026",
        "milestones": [
            {"description": "Fix the pilot blockers", "due_date": "2026-11-15"},
            {"description": "Pilot live", "due_date": "2027-02-01", "completed": True},
            {"description": "   ", "due_date": "2026-11-20"},
            {"description": "Train the team", "due_date": "soon"},
            "not a milestone",
        ],
    }
    seen = _stub_gemini(monkeypatch, "```json\n" + json.dumps(answer) + "\n```")
    _as_admin()

    r = _ai_draft(source_id)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["description"] == "Pilot live for Sales by December 31, 2026"
    assert [
        (m["description"], m["due_date"], m["completed"]) for m in body["milestones"]
    ] == [
        ("Fix the pilot blockers", "2026-11-15", False),
        ("Pilot live", None, False),  # Q1 2027 is outside Q4 2026
        ("Train the team", None, False),
    ]
    assert seen["feature"] == "eos_rock_carryover"
    assert seen["config"] == {"response_mime_type": "application/json"}


def test_ai_draft_prompt_carries_the_target_quarter_and_the_history(
    seeded, monkeypatch
):
    source_id = _insert_rock(seeded)
    _add_weekly_update(source_id, "Blocked on the data export", date(2026, 9, 20))
    _add_weekly_update(source_id, "x" * 900, date(2026, 9, 27))
    for day in (date(2026, 8, 1), date(2026, 8, 8)):
        _add_weekly_update(source_id, f"Old note {day.isoformat()}", day)
    seen = _stub_gemini(
        monkeypatch,
        json.dumps({"description": "Pilot live by Mar 31", "milestones": []}),
    )
    _as_admin()

    r = _ai_draft(source_id, quarter_year=2027, quarter_number=1)

    assert r.status_code == 200, r.text
    assert r.json() == {"description": "Pilot live by Mar 31", "milestones": []}
    prompt = seen["prompt"]
    assert "Q1 2027" in prompt
    assert "March 31, 2027" in prompt
    assert "Q3 2026" in prompt
    assert "Launch the AI pilot by Sep 30" in prompt
    assert "[done] Sign the pilot spec" in prompt
    assert "[not done] Pilot live for Sales (due 2026-09-15)" in prompt
    assert "Blocked on the data export" in prompt
    assert "x" * 500 in prompt and "x" * 501 not in prompt, "comments are capped"
    assert "Old note 2026-08-08" in prompt, "the three most recent updates"
    assert "Old note 2026-08-01" not in prompt


def test_ai_draft_caps_the_milestones(seeded, monkeypatch):
    source_id = _insert_rock(seeded)
    answer = {
        "description": "Pilot live by Dec 31",
        "milestones": [{"description": f"Step {i}"} for i in range(10)],
    }
    _stub_gemini(monkeypatch, json.dumps(answer))
    _as_admin()
    body = _ai_draft(source_id).json()
    assert [m["description"] for m in body["milestones"]] == [
        f"Step {i}" for i in range(6)
    ]


@pytest.mark.parametrize(
    "answer",
    ["Sure! Here is your rock.", json.dumps({"milestones": []}), json.dumps([1, 2])],
)
def test_ai_draft_502s_on_an_unusable_answer(seeded, monkeypatch, answer):
    source_id = _insert_rock(seeded)
    _stub_gemini(monkeypatch, answer)
    _as_admin()
    assert _ai_draft(source_id).status_code == 502


def test_ai_draft_refuses_a_quarter_that_is_not_later(seeded, monkeypatch):
    source_id = _insert_rock(seeded)
    seen = _stub_gemini(monkeypatch, "{}")
    _as_admin()
    r = _ai_draft(source_id, quarter_year=2026, quarter_number=3)
    assert r.status_code == 400, r.text
    assert "prompt" not in seen, "no AI spend on a refused request"


def test_the_ai_redraft_has_its_own_ai_cost_category():
    from backend.routers.admin import _FEATURE_LABELS

    label = _FEATURE_LABELS["eos_rock_carryover"]
    assert label == "EOS Rock carry-over (AI re-draft)"
    assert not LEGACY_PATTERN.search(label)

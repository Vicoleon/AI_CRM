"""Calendar-import recognizes the Exec team's L10 by name.

Found while fixing a roster-visibility leak: an Executive Team L10 named
"ET L10" was stored as an ordinary department meeting
(``is_exec_meeting=False``, ``department_name="Operations"``) because
``calendar-import`` hard-coded the flag to False. Two consequences:

  * its sessions / notes / headlines / ratings got the weaker
    department-style fence instead of the exec-only one
    (``_authz._meeting_accessible`` returns ``scope.is_exec`` for an exec
    meeting, with no roster / manager / department override), and
  * ``team_scope_filter`` routes a meeting's items by the same flag, so the
    ET L10 aggregated the Operations team's Rocks / IDS / Scorecard — in dev
    that was nothing at all, because Operations owns no EOS rows while the
    exec-flagged set held 20 scorecard metrics, 8 rocks and 4 issues.

Fix: infer the flag from the event's NAME ("ET" as a standalone word) and
gate it through ``require_exec_scope_allowed``, so inference can never
become a non-exec's path onto the Exec surface.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from backend.routers.eos.l10 import _infer_import_exec_flag
from rubik_core.config import settings
from rubik_core.database.schemas import UserProfile

client = TestClient(app)

TATUM = "tmercer@x.example"  # exec, organizes the ET L10
CATHY = "jhayes@x.example"  # invited to the ET L10 but NOT on the exec team


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db_path = tmp_path / "test_l10_import_exec.db"
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    import rubik_core.database.db as dbmod

    dbmod._engine = None
    engine = dbmod.get_engine()
    # Load-bearing: a non-sqlite engine here means the override regressed and
    # the test is about to write to deployed Cloud SQL (see docs/ENGINEERING_NOTES.md).
    assert engine.dialect.name == "sqlite", (
        f"refusing non-sqlite engine: {engine.url!r}"
    )
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database import eos  # noqa: F401  — register EOS tables
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    yield
    app.dependency_overrides.clear()
    dbmod._engine = None


def _profile(email: str, *, department: str = "Operations", **kw) -> UserProfile:
    now = "2026-08-06T00:00:00"
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


class _FakeCalendarClient:
    """Stand-in for GoogleCalendarClient. ``event`` is set per-test."""

    event: dict = {}

    def __init__(self, user_email=None):
        self.service = object()

    def get_event(self, event_id):
        return dict(self.event, id=event_id)


def _et_event(summary: str = "ET L10") -> dict:
    return {
        "summary": summary,
        "start": {"dateTime": "2026-08-11T10:00:00-06:00"},
        "end": {"dateTime": "2026-08-11T11:30:00-06:00"},
        "attendees": [{"email": TATUM}, {"email": CATHY}],
        "organizer": {"email": TATUM},
    }


def _use_calendar(monkeypatch, event: dict) -> None:
    _FakeCalendarClient.event = event
    monkeypatch.setattr(
        "rubik_core.tools.calendar.GoogleCalendarClient", _FakeCalendarClient
    )


def _import(event_id: str = "evt-et"):
    return client.post(
        "/api/eos/l10/calendar-import", json={"google_event_id": event_id}
    )


# ---- the name heuristic ----------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "ET L10",  # dev's real Executive Team L10
        "[Prep] ET L10",  # ...and its prep slot, both live in dev
        "(Prep) ET L10",
        "et l10",  # case-insensitive
        "ET Leadership L10",
        "  ET L10  ",  # calendar titles carry stray whitespace
    ],
)
def test_exec_names_are_recognized(name):
    assert _infer_import_exec_flag(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "Budget L10",  # "et" inside a word must not match
        "Target Review",
        "Marketing L10",
        "Support L10 Meeting",
        "Finance Management - L10 Meeting",
        "Delivery L10",
        # The reason "ET" must LEAD the title: a team that puts the timezone in
        # its meeting name must not be mistaken for the Exec team and locked
        # out of its own L10.
        "Sales L10 - 10am ET",
        "L10 (ET)",
        "Weekly sync 9am ET / 6am PT",
        "",
    ],
)
def test_ordinary_names_are_not_exec(name):
    assert _infer_import_exec_flag(name) is False


# ---- import behavior -------------------------------------------------------


def test_exec_importing_et_l10_scopes_it_to_the_exec_team(monkeypatch):
    """The regression this fixes: an imported "ET L10" must land exec-scoped,
    with no department (it is cross-cutting)."""
    _use_calendar(monkeypatch, _et_event())
    _as(_profile(TATUM, is_exec=True))

    r = _import()
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["is_exec_meeting"] is True
    assert body["department_name"] is None


def test_non_exec_importing_et_l10_is_403_not_a_silent_lockout(monkeypatch):
    """Inference must not route around the Exec-scope gate. Cathy is really
    invited to the ET L10, but is not on the exec team — Cathy gets the same
    403 the create / PATCH routes give, rather than an exec meeting Cathy then
    can't see (which would read as "Import did nothing")."""
    _use_calendar(monkeypatch, _et_event())
    _as(_profile(CATHY, department="HR"))

    r = _import()
    assert r.status_code == 403, r.text
    assert "Exec" in r.json()["detail"]

    # And nothing was written.
    from rubik_core.database.eos import L10Meeting

    db = _session()
    try:
        assert db.query(L10Meeting).count() == 0
    finally:
        db.close()


def test_ordinary_import_is_untouched(monkeypatch):
    """A normal team L10 still gets department inference and stays non-exec."""
    _use_calendar(monkeypatch, _et_event(summary="Budget L10"))
    _as(_profile(CATHY, department="HR"))

    r = _import("evt-budget")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["is_exec_meeting"] is False
    # Majority vote over the invitees; both are seeded without users rows, so
    # this falls back to the importer's own department.
    assert body["department_name"] == "HR"


def test_reimport_heals_a_meeting_that_predates_the_inference(monkeypatch):
    """dev's exact case: an "ET L10" row already sitting unflagged with a
    department stamped on it. Re-importing promotes it in place — same row,
    session history intact."""
    from rubik_core.database.eos import L10Meeting, Person

    db = _session()
    try:
        tatum = Person(
            first_name="Tatum", last_name="B", email=TATUM, created_by="seed"
        )
        db.add(tatum)
        db.commit()
        db.refresh(tatum)
        stale = L10Meeting(
            name="ET L10",
            department_name=None,  # FK-free: exercise the flag, not departments
            manager_id=tatum.id,
            day_of_week=1,
            start_time="10:00",
            is_exec_meeting=False,  # what the old hard-coded import wrote
            roster_emails=[TATUM, CATHY],
            google_event_id="evt-et",
            created_by=TATUM,
        )
        db.add(stale)
        db.commit()
        db.refresh(stale)
        stale_id = str(stale.id)
    finally:
        db.close()

    _use_calendar(monkeypatch, _et_event())
    _as(_profile(TATUM, is_exec=True))

    r = _import()
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"] == stale_id, "must upsert the existing row, not duplicate it"
    assert body["is_exec_meeting"] is True
    assert body["department_name"] is None


def test_reimport_never_declassifies_an_exec_meeting(monkeypatch):
    """The heal is one-directional. A calendar event renamed off "ET" must not
    turn an Exec L10 back into a department meeting — that would declassify
    its notes and headlines. Only the exec-gated PATCH can do that."""
    from rubik_core.database.eos import L10Meeting, Person

    db = _session()
    try:
        tatum = Person(
            first_name="Tatum", last_name="B", email=TATUM, created_by="seed"
        )
        db.add(tatum)
        db.commit()
        db.refresh(tatum)
        exec_meeting = L10Meeting(
            name="ET L10",
            manager_id=tatum.id,
            day_of_week=1,
            start_time="10:00",
            is_exec_meeting=True,
            roster_emails=[TATUM],
            google_event_id="evt-et",
            created_by=TATUM,
        )
        db.add(exec_meeting)
        db.commit()
        db.refresh(exec_meeting)
        exec_id = exec_meeting.id
    finally:
        db.close()

    # Renamed on the calendar so the name no longer reads as exec.
    _use_calendar(monkeypatch, _et_event(summary="Leadership Sync"))
    _as(_profile(TATUM, is_exec=True))

    r = _import()
    assert r.status_code == 201, r.text

    db = _session()
    try:
        after = db.query(L10Meeting).filter(L10Meeting.id == exec_id).first()
        assert after.is_exec_meeting is True, "an import must never declassify"
    finally:
        db.close()


def test_exec_import_does_not_absorb_a_same_named_department_meeting(monkeypatch):
    """The upsert's name-match tier is fenced by team KIND. A department
    meeting that happens to share the name must not be swallowed by an exec
    import (nor vice versa) — that would silently reclassify it."""
    from rubik_core.database.eos import L10Meeting, Person

    db = _session()
    try:
        tatum = Person(
            first_name="Tatum", last_name="B", email=TATUM, created_by="seed"
        )
        db.add(tatum)
        db.commit()
        db.refresh(tatum)
        dept_meeting = L10Meeting(
            name="ET L10",
            manager_id=tatum.id,
            day_of_week=1,
            start_time="10:00",
            is_exec_meeting=False,
            google_event_id=None,  # manually created — no event id to match on
            created_by=TATUM,
        )
        db.add(dept_meeting)
        db.commit()
        db.refresh(dept_meeting)
        dept_id = dept_meeting.id
    finally:
        db.close()

    _use_calendar(monkeypatch, _et_event())
    _as(_profile(TATUM, is_exec=True))

    r = _import()
    assert r.status_code == 201, r.text
    assert r.json()["id"] != str(dept_id), "exec import absorbed a department meeting"

    db = _session()
    try:
        untouched = db.query(L10Meeting).filter(L10Meeting.id == dept_id).first()
        assert untouched.is_exec_meeting is False
        assert untouched.is_deleted is False
    finally:
        db.close()

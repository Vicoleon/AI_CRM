"""A calendar-imported L10 is visible only to the people it invites.

Marlo creates a "Delivery L10" on Marlo's own calendar, inviting nobody else,
and imports it into Cubie. Blair and Devon are on the Systems team with Marlo
but were never invited, so the meeting must not appear in their L10 Meetings
list.

Why it could leak: ``calendar-import`` stamps a department on the meeting via
``_infer_import_department`` (majority vote over the invitees — here, a roster
of one, so Marlo's own department, Systems). The meeting ACL then granted
access to anyone in that department, so a routing hint became a grant.

Fix: when a meeting carries a calendar roster, the roster IS the ACL —
department membership no longer grants access. Meetings with no roster
(manually scheduled) keep team-wide visibility, since there is no invite list
to consult.

Covers both surfaces that matched on department: the meetings list / its
sub-resources (``_authz._meeting_accessible``) and the "your next L10" prep
nudge (``me._find_next_l10_for``).
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

MARLO = "marlo@x.example"
BLAIR = "blair@x.example"
DEVON = "devon@x.example"


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db_path = tmp_path / "test_l10_roster_visibility.db"
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    import rubik_core.database.db as dbmod

    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database import eos  # noqa: F401  — register EOS tables
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    yield
    app.dependency_overrides.clear()


def _profile(email: str, *, department: str = "Systems", **kw) -> UserProfile:
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


def _seed() -> dict:
    """Marlo's solo imported Delivery L10 alongside a normal, manually-scheduled
    Systems L10 — the control that must stay visible to the whole team.

    Both are scheduled for tomorrow so the ``me`` prep-nudge lookahead picks
    them up regardless of which weekday the suite runs on. Delivery is deliberately
    the EARLIER of the two: "your next L10" returns only the soonest match, so
    an equal-or-later Delivery would pass the nudge assertions whether or not the
    roster is honored.
    """
    from rubik_core.database.eos import L10Meeting, Person
    from rubik_core.database.models import Department

    tomorrow = datetime.now() + timedelta(days=1)
    db = _session()
    ids: dict = {}
    try:
        db.add(Department(name="Systems"))
        marlo = Person(
            first_name="Marlo",
            last_name="Ingram",
            email=MARLO,
            created_by="seed",
        )
        db.add(marlo)
        db.commit()
        db.refresh(marlo)
        ids["marlo_person"] = marlo.id

        delivery = L10Meeting(
            name="Delivery L10",
            department_name="Systems",
            manager_id=marlo.id,
            day_of_week=tomorrow.weekday(),
            start_time="08:00",
            # What the import wrote: Marlo invited only Marlo.
            roster_emails=[MARLO],
            google_event_id="evt-delivery",
            created_by=MARLO,
        )
        team = L10Meeting(
            name="Systems L10",
            department_name="Systems",
            day_of_week=tomorrow.weekday(),
            start_time="09:00",
            created_by="seed",  # manually scheduled — no roster
        )
        db.add_all([delivery, team])
        db.commit()
        for key, m in (("delivery", delivery), ("team", team)):
            db.refresh(m)
            ids[key] = m.id
        return ids
    finally:
        db.close()


def _listed() -> set[str]:
    r = client.get("/api/eos/l10/")
    assert r.status_code == 200, r.text
    return {m["id"] for m in r.json()}


# ---- the roster is the ACL ------------------------------------------------


def test_teammate_not_invited_does_not_see_imported_meeting():
    ids = _seed()
    for teammate in (BLAIR, DEVON):
        _as(_profile(teammate))
        listed = _listed()
        assert str(ids["delivery"]) not in listed, f"{teammate} still sees Delivery L10"
        # The team's own L10 is untouched — this is a scoping fix, not a blackout.
        assert str(ids["team"]) in listed


def test_organizer_still_sees_their_own_imported_meeting():
    ids = _seed()
    _as(_profile(MARLO))
    listed = _listed()
    assert str(ids["delivery"]) in listed
    assert str(ids["team"]) in listed


def test_invited_outsider_sees_it_even_from_another_department():
    """Being invited is what counts — not sharing the stamped department."""
    ids = _seed()
    db = _session()
    try:
        from rubik_core.database.eos import L10Meeting

        m = db.query(L10Meeting).filter(L10Meeting.id == ids["delivery"]).first()
        m.roster_emails = [MARLO, "guest@x.example"]
        db.commit()
    finally:
        db.close()

    _as(_profile("guest@x.example", department="Marketing"))
    listed = _listed()
    assert str(ids["delivery"]) in listed
    assert str(ids["team"]) not in listed  # not their team, not invited


def test_uninvited_teammate_is_404_on_every_subresource():
    """The list is only the front door: notes, headlines and the in-meeting
    items payload must be closed too, or the id alone reopens the leak."""
    ids = _seed()
    meeting_id = ids["delivery"]
    _as(_profile(BLAIR))
    assert client.get(f"/api/eos/l10/meetings/{meeting_id}/items").status_code == 404
    assert client.get(f"/api/eos/l10/meetings/{meeting_id}/inputs").status_code == 404
    assert (
        client.get(f"/api/eos/l10/meetings/{meeting_id}/inputs/history").status_code
        == 404
    )
    assert (
        client.post(
            f"/api/eos/l10/meetings/{meeting_id}/inputs",
            json={"meeting_id": str(meeting_id), "kind": "headline", "text": "sneaky"},
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/eos/l10/sessions/start",
            json={"meeting_id": str(meeting_id), "actual_start": "2026-08-06T12:30:00"},
        ).status_code
        == 404
    )


def test_uninvited_teammate_sessions_list_excludes_it():
    """The diary/history list folds in the same ACL — a past Delivery session must
    not leak its notes through /sessions."""
    ids = _seed()
    db = _session()
    try:
        from rubik_core.database.eos import L10Session

        db.add(
            L10Session(
                meeting_id=ids["delivery"],
                actual_start=datetime(2026, 8, 5, 12, 30),
                notes="Delivery private notes",
                created_by=MARLO,
            )
        )
        db.commit()
    finally:
        db.close()

    _as(_profile(BLAIR))
    r = client.get("/api/eos/l10/sessions")
    assert r.status_code == 200, r.text
    assert all(s["meeting_id"] != str(ids["delivery"]) for s in r.json())

    _as(_profile(MARLO))
    r = client.get("/api/eos/l10/sessions")
    assert any(s["meeting_id"] == str(ids["delivery"]) for s in r.json())


# ---- the second surface: the "your next L10" prep nudge --------------------


def _next_l10_name() -> str | None:
    r = client.get("/api/eos/me/l10-prep-status")
    assert r.status_code == 200, r.text
    nxt = r.json().get("next_l10")
    return None if nxt is None else nxt["meeting_name"]


def test_prep_nudge_ignores_meetings_the_caller_is_not_invited_to():
    """The old check matched on department too, so the uninvited teammate got nagged to
    prep for a meeting they can't even open. Delivery (08:00) is sooner than the
    team L10 (09:00), so it wins the nudge unless the roster is honored."""
    _seed()
    _as(_profile(BLAIR))
    assert _next_l10_name() == "Systems L10"


def test_prep_nudge_still_finds_an_imported_meeting_you_are_on():
    """Same seed, the invitee's view: the roster path finds Delivery for Marlo."""
    _seed()
    _as(_profile(MARLO))
    assert _next_l10_name() == "Delivery L10"


# ---- the recovery path: invite them, re-sync, access returns ---------------


class _FakeCalendarClient:
    """Stands in for GoogleCalendarClient inside the import endpoint."""

    event: dict = {}

    def __init__(self, *args, **kwargs):
        self.service = object()  # truthy → "calendar configured"

    def get_event(self, event_id):
        return dict(self.event, id=event_id)


def test_sync_after_inviting_the_team_restores_visibility(monkeypatch):
    """The fix is not a dead end: adding people to the calendar event and
    hitting Sync re-imports the roster, and they can see it again."""
    ids = _seed()
    _as(_profile(BLAIR))
    assert str(ids["delivery"]) not in _listed()

    _FakeCalendarClient.event = {
        "summary": "Delivery L10",
        "start": {"dateTime": "2026-08-13T12:30:00-06:00"},
        "end": {"dateTime": "2026-08-13T13:30:00-06:00"},
        "attendees": [{"email": MARLO}, {"email": BLAIR}, {"email": DEVON}],
        "organizer": {"email": MARLO},
    }
    monkeypatch.setattr(
        "rubik_core.tools.calendar.GoogleCalendarClient", _FakeCalendarClient
    )

    _as(_profile(MARLO))
    r = client.post(
        "/api/eos/l10/calendar-import", json={"google_event_id": "evt-delivery"}
    )
    assert r.status_code == 201, r.text
    assert r.json()["id"] == str(ids["delivery"])  # upserted, not duplicated

    _as(_profile(BLAIR))
    assert str(ids["delivery"]) in _listed()


# ---- the org-wide Exec exception -------------------------------------------
#
# The org-wide Exec reads every department's Rocks / Issues / To-Dos /
# Scorecard, so a person in that role who co-leads departments sees their L10s
# too, including a calendar-imported L10 they were not invited to.
# ``_authz._meeting_accessible`` lets the org-wide Exec — and ONLY that role —
# read every team's L10. The roster rule above still binds everyone else, which
# is what keeps an uninvited teammate out.

CEO = "ceo@x.example"


def _ceo() -> UserProfile:
    """Org-wide Exec: on the Exec team AND holding the ``*`` wildcard. That
    combination is what ``UserProfile.sees_all_departments`` keys on."""
    return _profile(CEO, department="general", is_exec=True, managed=["*"])


def test_ceo_sees_every_teams_l10_including_uninvited_imports():
    ids = _seed()
    _as(_ceo())
    listed = _listed()
    assert str(ids["delivery"]) in listed, "CEO cannot see the imported team L10"
    assert str(ids["team"]) in listed


def test_ceo_can_open_subresources_that_404_for_an_uninvited_teammate():
    """Listing the meeting is useless if its contents stay 404 — the org-wide
    Exec needs the items/inputs payload the Meetings page actually renders."""
    ids = _seed()
    meeting_id = ids["delivery"]

    _as(_profile(BLAIR))  # control: still shut out
    assert client.get(f"/api/eos/l10/meetings/{meeting_id}/items").status_code == 404

    _as(_ceo())
    assert client.get(f"/api/eos/l10/meetings/{meeting_id}/items").status_code == 200
    assert client.get(f"/api/eos/l10/meetings/{meeting_id}/inputs").status_code == 200


def test_admin_does_not_inherit_the_ceo_bypass():
    """The exception is keyed on the org-wide Exec ROLE, not on ``admin``: a
    Cubie admin is an operator, not an executive, and must not gain the right
    to read another team's diary just by holding the role."""
    ids = _seed()
    _as(_profile("admin@x.example", department="Systems", roles=["admin", "user"]))
    assert str(ids["delivery"]) not in _listed()


def test_scoped_exec_does_not_inherit_the_ceo_bypass():
    """``is_exec`` alone is not org-wide — without the ``*`` wildcard an exec
    stays membership-scoped, or every exec would read every team's L10."""
    ids = _seed()
    _as(
        _profile(
            "cfo@x.example", department="Finance", is_exec=True, managed=["Finance"]
        )
    )
    assert str(ids["delivery"]) not in _listed()


def test_team_manager_does_not_inherit_the_ceo_bypass():
    """The uninvited-teammate case, re-asserted from the other direction: managing
    the department the import was stamped with still grants nothing."""
    ids = _seed()
    _as(_profile(BLAIR, department="Systems", managed=["Systems"]))
    assert str(ids["delivery"]) not in _listed()


def test_ceo_read_access_does_not_widen_the_prep_nudge():
    """Seeing every L10 must not mean being nagged for every L10 — the nudge is
    about the caller's own prep. Delivery (08:00) is the soonest meeting in the
    seed, so a widened nudge would surface it here."""
    _seed()
    _as(_ceo())
    assert _next_l10_name() is None

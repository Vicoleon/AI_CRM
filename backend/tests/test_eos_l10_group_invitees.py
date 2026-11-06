"""A Google Group on an L10's calendar invite is expanded into its people.

Background. An Exec L10's invite carries an "Exec Team" Google Group. The
Calendar UI shows the group expanded, but the Calendar API returns it as ONE
attendee carrying the group's address, so ``roster_emails`` — and the Check-in
picker scoped to it — never held the members invited only that way. Sync
re-read the same unexpanded group, so it could not heal.

Two layers here: the pure expansion helper against a fake Directory, and the
import endpoint end to end — roster written, Person rows minted, the meeting
payload the picker reads, and Sync picking up a membership change.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from rubik_core.config import settings
from rubik_core.database.schemas import UserProfile
from rubik_core.eos import calendar_groups
from rubik_core.eos.calendar_groups import expand_group_invitees

client = TestClient(app)

TATUM = "tmercer@x.example"  # organizer, a known user
NICO = "ndiaz@x.example"  # invited ONLY through the group
EMERY = "ebrooks@x.example"  # invited ONLY through the group
JORDAN = "jhayes@x.example"  # invited individually AND through the group
GROUP = "exec-team@x.example"


# ---- a fake Directory API ----------------------------------------------------


class _NotFound(Exception):
    """What googleapiclient raises for members.list on a non-group address."""

    def __init__(self) -> None:
        super().__init__("404 Resource Not Found: groupKey")
        self.resp = SimpleNamespace(status=404)


class _Request:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class _FakeDirectory:
    """``members().list(groupKey=…)`` over an in-memory group table.

    ``groups`` maps a group address to its member dicts (``email``, ``type``,
    ``status``). Any other address is a 404 — a user, an outside guest.
    ``broken`` addresses raise a non-404 error. ``page_size`` paginates.
    """

    def __init__(self, groups, *, broken=(), page_size=None):
        self.groups = {k.lower(): v for k, v in groups.items()}
        self.broken = {b.lower() for b in broken}
        self.page_size = page_size
        self.calls: list[str] = []

    def members(self):
        return self

    def list(self, groupKey, maxResults=None, pageToken=None):
        key = groupKey.lower()

        def run():
            self.calls.append(key)
            if key in self.broken:
                raise RuntimeError("Directory 503")
            if key not in self.groups:
                raise _NotFound()
            members = self.groups[key]
            if not self.page_size:
                return {"members": members}
            start = int(pageToken or 0)
            end = start + self.page_size
            page = {"members": members[start:end]}
            if end < len(members):
                page["nextPageToken"] = str(end)
            return page

        return _Request(run)


def _user(email, status="ACTIVE"):
    return {"email": email, "type": "USER", "status": status}


def _group(email):
    return {"email": email, "type": "GROUP"}


def _factory(fake):
    return lambda: fake


# ---- the helper -----------------------------------------------------------------


def test_group_is_replaced_by_its_people_in_place():
    fake = _FakeDirectory({GROUP: [_user(NICO), _user(EMERY)]})
    out = expand_group_invitees([TATUM, GROUP, JORDAN], service_factory=_factory(fake))
    assert out.roster == [TATUM, NICO, EMERY, JORDAN]
    assert GROUP not in out.roster
    assert out.expanded == {GROUP: [NICO, EMERY]}
    assert out.failed == []


def test_member_also_invited_individually_appears_once():
    fake = _FakeDirectory({GROUP: [_user(JORDAN), _user(NICO)]})
    out = expand_group_invitees([JORDAN, GROUP], service_factory=_factory(fake))
    assert out.roster == [JORDAN, NICO]


def test_known_users_are_never_looked_up():
    fake = _FakeDirectory({GROUP: [_user(NICO)]})
    out = expand_group_invitees(
        [TATUM, JORDAN, GROUP], skip={TATUM, JORDAN}, service_factory=_factory(fake)
    )
    assert fake.calls == [GROUP]
    assert out.probed == [GROUP]
    assert out.roster == [TATUM, JORDAN, NICO]


def test_plain_addresses_survive_a_404():
    fake = _FakeDirectory({})
    out = expand_group_invitees(
        [TATUM, "guest@elsewhere.com"], service_factory=_factory(fake)
    )
    assert out.roster == [TATUM, "guest@elsewhere.com"]
    assert out.expanded == {} and out.failed == []


def test_lookup_failure_keeps_that_address_and_the_rest_still_expand():
    fake = _FakeDirectory({GROUP: [_user(NICO)]}, broken={"ops@x.example"})
    out = expand_group_invitees(
        [GROUP, "ops@x.example"], service_factory=_factory(fake)
    )
    assert out.roster == [NICO, "ops@x.example"]
    assert out.failed == ["ops@x.example"]


def test_directory_not_configured_keeps_the_roster_as_given():
    out = expand_group_invitees([TATUM, GROUP], service_factory=lambda: None)
    assert out.roster == [TATUM, GROUP]
    assert out.expanded == {}


def test_directory_client_error_never_raises():
    def boom():
        raise RuntimeError("no DWD key")

    out = expand_group_invitees([TATUM, GROUP], service_factory=boom)
    assert out.roster == [TATUM, GROUP]
    assert out.failed == [TATUM, GROUP]


def test_nested_groups_expand_and_a_cycle_does_not_loop():
    fake = _FakeDirectory(
        {
            "all@x.example": [_user(TATUM), _group("exec@x.example")],
            "exec@x.example": [_user(NICO), _group("all@x.example")],  # cycle
        }
    )
    out = expand_group_invitees(["all@x.example"], service_factory=_factory(fake))
    assert out.roster == [TATUM, NICO]


def test_too_deep_a_nest_is_kept_as_an_address():
    fake = _FakeDirectory(
        {
            "a@x.example": [_user("a1@x.example"), _group("b@x.example")],
            "b@x.example": [_user("b1@x.example"), _group("c@x.example")],
            "c@x.example": [_user("c1@x.example"), _group("d@x.example")],
            "d@x.example": [_user("d1@x.example")],
        }
    )
    out = expand_group_invitees(["a@x.example"], service_factory=_factory(fake))
    # a (depth 0) → b (1) → c (2): c's nested group d is beyond MAX_DEPTH.
    assert out.roster == ["a1@x.example", "b1@x.example", "c1@x.example", "d@x.example"]


def test_suspended_and_customer_members_are_dropped():
    fake = _FakeDirectory(
        {
            GROUP: [
                _user(NICO),
                _user("gone@x.example", status="SUSPENDED"),
                {"email": "everyone@x.example", "type": "CUSTOMER"},
            ]
        }
    )
    out = expand_group_invitees([GROUP], service_factory=_factory(fake))
    assert out.roster == [NICO]


def test_every_page_of_a_large_group_is_read():
    members = [_user(f"m{i}@x.example") for i in range(5)]
    fake = _FakeDirectory({GROUP: members}, page_size=2)
    out = expand_group_invitees([GROUP], service_factory=_factory(fake))
    assert out.roster == [m["email"] for m in members]
    assert fake.calls.count(GROUP) == 3  # three pages


def test_kill_switch_off_leaves_the_roster_alone(monkeypatch):
    monkeypatch.setattr(settings, "EOS_L10_EXPAND_GROUP_INVITEES", False)
    fake = _FakeDirectory({GROUP: [_user(NICO)]})
    out = expand_group_invitees(
        [f" {TATUM.upper()} ", GROUP, TATUM], service_factory=_factory(fake)
    )
    assert out.roster == [TATUM, GROUP]  # still normalized and de-duplicated
    assert fake.calls == []


# ---- the import endpoint ------------------------------------------------------


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db_path = tmp_path / "test_l10_group_invitees.db"
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


def _profile(email: str, *, department: str = "Operations") -> UserProfile:
    now = "2026-10-06T00:00:00"
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name=email.split("@")[0].title(),
        roles=["user"],
        department=department,
        is_exec=False,
        managed_departments=[],
        created_at=now,
        updated_at=now,
    )


def _as(profile: UserProfile) -> None:
    app.dependency_overrides[get_current_user] = lambda: profile


def _session():
    from rubik_core.database.db import SessionLocal

    return SessionLocal()


def _seed_user(email: str, department: str = "Operations") -> None:
    from rubik_core.database.models import User

    with _session() as db:
        db.add(
            User(
                email=email,
                id=email.split("@")[0],
                name=email.split("@")[0].title(),
                department=department,
            )
        )
        db.commit()


class _FakeCalendarClient:
    """Stand-in for GoogleCalendarClient. ``event`` is set per-test."""

    event: dict = {}

    def __init__(self, user_email=None):
        self.service = object()

    def get_event(self, event_id):
        return dict(self.event, id=event_id)


def _event(attendees: list[str]) -> dict:
    return {
        "summary": "Ops L10",
        "start": {"dateTime": "2026-10-13T10:00:00-06:00"},
        "end": {"dateTime": "2026-10-13T11:30:00-06:00"},
        "attendees": [{"email": e} for e in attendees],
        "organizer": {"email": TATUM},
    }


def _use_calendar(monkeypatch, event: dict) -> None:
    _FakeCalendarClient.event = event
    monkeypatch.setattr(
        "rubik_core.tools.calendar.GoogleCalendarClient", _FakeCalendarClient
    )


def _use_directory(monkeypatch, fake) -> None:
    monkeypatch.setattr(calendar_groups, "_build_service", lambda: fake)


def _import(event_id: str = "evt-ops"):
    return client.post(
        "/api/eos/l10/calendar-import", json={"google_event_id": event_id}
    )


def _meeting_row():
    from rubik_core.database.eos import L10Meeting

    with _session() as db:
        rows = db.query(L10Meeting).filter(L10Meeting.is_deleted.is_(False)).all()
        assert len(rows) == 1, [r.name for r in rows]
        m = rows[0]
        return str(m.id), list(m.roster_emails or [])


def _people_emails():
    from rubik_core.database.eos import Person

    with _session() as db:
        return {
            p.email.lower()
            for p in db.query(Person).filter(Person.is_deleted.is_(False)).all()
            if p.email
        }


def test_import_writes_the_people_behind_the_group_into_the_roster(monkeypatch):
    _seed_user(TATUM)
    _as(_profile(TATUM))
    _use_calendar(monkeypatch, _event([TATUM, GROUP, JORDAN]))
    fake = _FakeDirectory(
        {GROUP: [_user(TATUM), _user(NICO), _user(EMERY), _user(JORDAN)]}
    )
    _use_directory(monkeypatch, fake)

    res = _import()
    assert res.status_code == 201, res.text

    meeting_id, roster = _meeting_row()
    assert roster == [TATUM, NICO, EMERY, JORDAN]
    assert GROUP not in roster
    # Known user Tatum was never looked up; the group and Jordan were.
    assert TATUM not in fake.calls and GROUP in fake.calls
    # Every person on the roster has a Person row the picker can resolve, and
    # the group address did NOT get a placeholder row.
    people = _people_emails()
    assert {NICO, EMERY, JORDAN} <= people
    assert GROUP not in people

    # What the in-meeting Check-in picker actually reads.
    items = client.get(f"/api/eos/l10/meetings/{meeting_id}/items")
    assert items.status_code == 200, items.text
    meeting = items.json()["meeting"]
    assert NICO in meeting["attendee_emails"]
    assert NICO in {a["email"].lower() for a in meeting["attendees"]}


def test_sync_picks_up_a_change_in_group_membership(monkeypatch):
    _seed_user(TATUM)
    _as(_profile(TATUM))
    _use_calendar(monkeypatch, _event([TATUM, GROUP]))
    fake = _FakeDirectory({GROUP: [_user(TATUM), _user(EMERY)]})
    _use_directory(monkeypatch, fake)
    assert _import().status_code == 201
    _, roster = _meeting_row()
    assert roster == [TATUM, EMERY]

    # Nico joins the group; the next Sync (a re-import of the same series)
    # refreshes the ONE meeting row instead of minting a second one.
    fake.groups[GROUP].append(_user(NICO))
    res = _import()
    assert res.status_code == 201, res.text
    _, roster = _meeting_row()
    assert roster == [TATUM, EMERY, NICO]


def test_directory_failure_keeps_the_group_address_and_the_import_succeeds(monkeypatch):
    _seed_user(TATUM)
    _as(_profile(TATUM))
    _use_calendar(monkeypatch, _event([TATUM, GROUP]))
    _use_directory(monkeypatch, _FakeDirectory({}, broken={GROUP}))

    res = _import()
    assert res.status_code == 201, res.text
    _, roster = _meeting_row()
    assert roster == [TATUM, GROUP]  # exactly the pre-fix behavior, nothing lost


def test_directory_not_configured_is_the_old_behavior(monkeypatch):
    _seed_user(TATUM)
    _as(_profile(TATUM))
    _use_calendar(monkeypatch, _event([TATUM, GROUP]))
    _use_directory(monkeypatch, None)

    assert _import().status_code == 201
    _, roster = _meeting_row()
    assert roster == [TATUM, GROUP]

"""Voice commands: opening ONE specific record by what it is about.

"Open Acme's X-Ray", "open Thursday's Systems L10", "prep for my 2pm", "open the
Globex proposal", "open my chat about payroll", "open Parker's guide". What this file pins:

1. Only the kinds the person can reach are offered, by the pages' own gates.
2. Each finder returns the person's own records (their L10s, their proposals,
   their chats), never another team's; one clear match opens, several ask
   "which one?", none says so.
3. An L10 always asks first: opening its page starts the meeting.
4. Every path is a fixed route shape plus a record id, pinned before it leaves.

HubSpot and Google Calendar are faked by STRING target, and so is the clock the
L10 finder reads. Nothing touches the network.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from backend.services import voice_records as vr
from rubik_core.database.schemas import UserProfile
from rubik_core.features import clear_cache
from rubik_core.voice import commands as cmd

URL = "/api/voice/commands"
READ_COMMAND = "rubik_core.voice.commands.read_command"
TODAY = "backend.routers.voice_commands.local_today"
DAY = date(2026, 10, 6)  # a Tuesday
NOW = datetime(2026, 10, 6, 17, 0)  # naive UTC: noon that Tuesday in Chicago
TZ = "America/Chicago"


def _profile(email: str, *, department: str = "Systems") -> UserProfile:
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


ME = _profile("jo.ellison@acme.example")


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    import rubik_core.database.db as dbmod
    from rubik_core.config import settings
    from rubik_core.database import eos  # noqa: F401 — register EOS tables
    from rubik_core.database.models import Base

    monkeypatch.setattr(settings, "CLOUDSQL_CONNECTION_NAME", "")
    monkeypatch.setattr(
        settings, "DATABASE_URL", f"sqlite:///{tmp_path / 'records.db'}"
    )
    dbmod._engine = None
    engine = dbmod.get_engine()
    assert engine.dialect.name == "sqlite", "tests must never touch Cloud SQL"
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


def _open(fresh_db, *, now: datetime = NOW, **args):
    db = fresh_db()
    try:
        return vr.open_record(args, ME, db, today=DAY, tz=TZ, now=now)
    finally:
        db.close()


def test_eos_module_off_hides_l10_kind_before_query(monkeypatch):
    from rubik_core.control import repository

    monkeypatch.setattr(repository, "module_enabled", lambda *_a, **_k: False)
    assert "l10_meeting" not in {kind.key for kind in vr.available_kinds(ME)}
    assert "meeting_prep" in {kind.key for kind in vr.available_kinds(ME)}
    opening = vr.open_record(
        {"kind": "l10_meeting"}, ME, object(), today=DAY, tz=TZ, now=NOW
    )
    assert opening.places == []
    assert opening.message


# ── 1. Gates ─────────────────────────────────────────────────────────────────


def test_only_the_kinds_the_person_can_reach_are_offered(monkeypatch):
    from rubik_core.config import settings

    monkeypatch.setattr(settings, "PROPOSALS_ENABLED", False)
    with (
        patch("backend.auth.user_can_access_xray", lambda u: False),
        patch("backend.auth.user_can_access_smart_teams", lambda u: False),
    ):
        keys = {k.key for k in vr.available_kinds(ME)}
    assert keys == {"l10_meeting", "meeting_prep", "chat"}


def test_a_kind_the_person_cannot_reach_is_refused(fresh_db):
    with patch("backend.auth.user_can_access_xray", lambda u: False):
        opening = _open(fresh_db, kind="customer_xray", name="Acme")
    assert opening.places == [] and "can open" in opening.message


# ── 2. Each finder ───────────────────────────────────────────────────────────


def test_xray_opens_the_named_company_with_its_name_for_the_page(fresh_db):
    rows = [
        {"company_id": "101", "name": "Acme Industries", "domain": "acmecorp.example"},
        {"company_id": "202", "name": "Zenith Logistics", "domain": "zenith.com"},
    ]
    with (
        patch("backend.auth.user_can_access_xray", lambda u: True),
        patch("rubik_core.xray.service.customer_search", lambda q, limit=10: rows),
    ):
        opening = _open(fresh_db, kind="customer_xray", name="Acme Industries")
    (place,) = opening.places
    assert place.path == "/customers/101/xray"
    assert place.state == {"name": "Acme Industries"}
    assert opening.prompt is None


def test_xray_with_no_company_found_says_so(fresh_db):
    with (
        patch("backend.auth.user_can_access_xray", lambda u: True),
        patch("rubik_core.xray.service.customer_search", lambda q, limit=10: []),
    ):
        opening = _open(fresh_db, kind="customer_xray", name="Nowhere Bank")
    assert "Nowhere Bank" in opening.message


def _freeze(monkeypatch, moment: datetime) -> None:
    """Pin the clock the voice records module reads, for the paths that do not
    take ``now`` (the endpoint). Which L10 is "next" depends on the day and the
    hour, so on the real clock these tests went red the moment the seeded
    Thursday slot of Oct 8 passed. ``moment`` is naive UTC, like ``NOW``."""

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.replace(tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr("backend.services.voice_records.datetime", _Clock)


@pytest.fixture
def l10s(fresh_db, monkeypatch):
    from rubik_core.database.eos import L10Meeting
    from rubik_core.database.models import Department

    _freeze(monkeypatch, NOW)
    db = fresh_db()
    db.add_all([Department(name="Systems"), Department(name="Sales")])
    db.commit()
    # ``start_time`` is the meeting's wall clock on the org's Central time,
    # the way calendar import and the schedule form both write it.
    systems = L10Meeting(
        name="Systems L10",
        department_name="Systems",
        day_of_week=3,
        start_time="11:30",
        created_by="seed",
    )
    monday = L10Meeting(
        name="Systems Planning",
        department_name="Systems",
        day_of_week=0,
        start_time="15:00",
        created_by="seed",
    )
    sales = L10Meeting(
        name="Sales L10",
        department_name="Sales",
        day_of_week=3,
        start_time="15:00",
        created_by="seed",
    )
    db.add_all([systems, monday, sales])
    db.commit()
    ids = {"systems": systems.id, "monday": monday.id, "sales": sales.id}
    db.close()
    return ids


def test_an_l10_always_asks_first_and_names_its_next_meeting(fresh_db, l10s):
    opening = _open(fresh_db, kind="l10_meeting", name="Systems", day="2026-10-08")
    (place,) = opening.places
    assert place.path == f"/eos/meetings/{l10s['systems']}"
    assert opening.ask_first is True
    assert "starts the meeting" in opening.prompt
    # 11:30 Central, not 11:30 UTC (which read back as 6:30 AM).
    assert place.detail == "Next: Thu, Oct 8, 11:30 AM"


def test_another_teams_l10_is_never_offered(fresh_db, l10s):
    opening = _open(fresh_db, kind="l10_meeting", name="Sales")
    assert all(str(l10s["sales"]) not in p.path for p in opening.places)


def test_my_next_l10_is_the_soonest(fresh_db, l10s):
    opening = _open(fresh_db, kind="l10_meeting")
    # Tuesday now: Thursday's comes before next Monday's.
    assert [p.path for p in opening.places] == [f"/eos/meetings/{l10s['systems']}"]


def test_once_this_weeks_l10_has_passed_next_weeks_is_named(
    fresh_db, l10s, monkeypatch
):
    # Thursday noon in Chicago: the 11:30 slot started half an hour ago.
    # The L10 finder reads the ``now`` handed to ``open_record`` (``ctx.now``),
    # not the module clock, so the moment has to travel with the call; freezing
    # the clock alone left ``_open``'s Tuesday default in charge, and the test
    # failed at every hour.
    moment = datetime(2026, 10, 8, 17, 0)  # naive UTC, like NOW
    _freeze(monkeypatch, moment)
    named = _open(
        fresh_db, now=moment, kind="l10_meeting", name="Systems", day="2026-10-08"
    )
    soonest = _open(fresh_db, now=moment, kind="l10_meeting")
    (place,) = named.places
    assert place.detail == "Next: Thu, Oct 15, 11:30 AM"
    # Monday's now comes before next Thursday's.
    assert [p.path for p in soonest.places] == [f"/eos/meetings/{l10s['monday']}"]


class _FakeCalendar:
    events: list[dict] = []

    def __init__(self, email):
        self.email = email

    def get_calendar_timezone(self, default="UTC"):
        return TZ

    def get_upcoming_events(self, **kwargs):
        _FakeCalendar.seen = kwargs
        return _FakeCalendar.events


def _event(
    event_id: str,
    summary: str,
    hour: int,
    minute: int = 0,
    day: date = DAY,
    attendee: str = "ana@globexholding.example",
):
    start = datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo(TZ))
    return {
        "id": event_id,
        "summary": summary,
        "start": {"dateTime": start.isoformat()},
        "attendees": [
            {"email": attendee, "displayName": attendee.split("@")[0].title()}
        ],
    }


def test_prep_for_my_2pm_opens_that_meetings_prep(fresh_db):
    _FakeCalendar.events = [
        _event("evt-1100", "Pipeline review", 11),
        _event("evt-1405", "Globex Holding rollout kickoff", 14, 5),
        {"id": "allday", "summary": "Holiday", "start": {"date": "2026-10-06"}},
    ]
    with patch("rubik_core.tools.calendar.GoogleCalendarClient", _FakeCalendar):
        opening = _open(fresh_db, kind="meeting_prep", time="14:00")
    (place,) = opening.places
    assert place.path == "/meeting-prep/evt-1405"
    assert place.label == "Prep: Globex Holding rollout kickoff"
    # The search was bounded to the day the person meant.
    assert _FakeCalendar.seen["time_min"].startswith("2026-10-06T05:00:00")


def test_prep_by_who_it_is_with(fresh_db):
    _FakeCalendar.events = [
        _event("evt-a", "Pipeline review", 11, attendee="marin@acme.example"),
        _event("evt-b", "Rollout kickoff", 15, attendee="ana@globexholding.example"),
    ]
    with patch("rubik_core.tools.calendar.GoogleCalendarClient", _FakeCalendar):
        opening = _open(fresh_db, kind="meeting_prep", name="Globex")
        two_words = _open(fresh_db, kind="meeting_prep", name="Globex Holding")
    assert [p.path for p in opening.places] == ["/meeting-prep/evt-b"]
    assert [p.path for p in two_words.places] == ["/meeting-prep/evt-b"]


def test_no_meeting_near_the_time_says_so(fresh_db):
    _FakeCalendar.events = [_event("evt-1100", "Pipeline review", 11)]
    with patch("rubik_core.tools.calendar.GoogleCalendarClient", _FakeCalendar):
        opening = _open(fresh_db, kind="meeting_prep", time="16:00")
    assert "no meeting near 4:00 PM" in opening.message


def test_proposals_are_the_persons_own(fresh_db, monkeypatch):
    from rubik_core.config import settings
    from rubik_core.database.proposals import Proposal
    from rubik_core.proposals import service as proposal_service
    from rubik_core.tenancy.context import current_workspace

    monkeypatch.setattr(settings, "PROPOSALS_ENABLED", True)
    monkeypatch.setattr(
        proposal_service, "visible_source_ids", lambda *sources: sources
    )

    def source(company_id: str):
        return {
            "_crm_provenance": {
                "schema_version": 1,
                "workspace": current_workspace(),
                "company_id": company_id,
                "contact_id": None,
                "deal_ids": [],
                "unprovable": False,
            }
        }

    db = fresh_db()
    db.add_all(
        [
            Proposal(
                id="p1",
                owner_email=ME.email,
                hubspot_company_id="101",
                brief_source=source("101"),
                title="Rollout program",
                company_name="Globex Holding",
            ),
            Proposal(
                id="p2",
                owner_email="someone@acme.example",
                hubspot_company_id="102",
                brief_source=source("102"),
                title="Rollout program",
                company_name="Globex Holding West",
            ),
        ]
    )
    db.commit()
    db.close()
    with patch("backend.auth.user_can_access_proposals", lambda u: True):
        opening = _open(fresh_db, kind="proposal", name="Globex Holding")
    assert [p.path for p in opening.places] == ["/sales/proposals/p1"]


def test_chats_are_the_persons_own_and_found_by_topic(fresh_db):
    from rubik_core.database.models import ChatSession

    db = fresh_db()
    db.add_all(
        [
            ChatSession(
                id="s1",
                user_email="Jo.Ellison@acme.example",
                title="Payroll reporting deadlines",
            ),
            ChatSession(id="s2", user_email=ME.email, title="Weekly pipeline"),
            ChatSession(
                id="s3", user_email="other@acme.example", title="Payroll questions"
            ),
        ]
    )
    db.commit()
    db.close()
    opening = _open(fresh_db, kind="chat", name="Payroll")
    assert [p.path for p in opening.places] == ["/chat?session=s1"]


def test_a_guide_opens_only_when_published(fresh_db):
    from rubik_core.database.models import User

    db = fresh_db()
    db.add_all(
        [
            User(
                email="parker.g@acme.example",
                id="pg",
                name="Parker Jensen",
                department="Systems",
            ),
            User(
                email="paula.k@acme.example",
                id="pk",
                name="Paula Kent",
                department="Systems",
            ),
        ]
    )
    db.commit()
    db.close()
    published = {"parker.g@acme.example"}
    with (
        patch("backend.auth.user_can_access_smart_teams", lambda u: True),
        patch(
            "rubik_core.smart_teams.guide.published_guide",
            lambda db, email: object() if email in published else None,
        ),
    ):
        found = _open(fresh_db, kind="work_style_guide", name="Parker Jensen")
        missing = _open(fresh_db, kind="work_style_guide", name="Paula Kent")
        mine = _open(fresh_db, kind="work_style_guide")
    assert [p.path for p in found.places] == [
        "/smart-teams/guide/parker.g@acme.example"
    ]
    # Unpublished reads exactly like missing.
    assert "couldn't find a published work style guide" in missing.message
    assert [p.path for p in mine.places] == ["/smart-teams"]


# ── 3. Through the endpoint ──────────────────────────────────────────────────


def _said(**args):
    async def fake(context, **kwargs):
        fake.context = context
        return cmd.Command(name=cmd.OPEN_RECORD, args=args, heard="open it")

    return fake


def test_the_endpoint_offers_the_kinds_and_returns_places(fresh_db, l10s):
    app.dependency_overrides[get_current_user] = lambda: ME
    fake = _said(kind="l10_meeting", name="Systems", day="2026-10-08")
    with patch(READ_COMMAND, fake), patch(TODAY, lambda tz: DAY):
        res = TestClient(app).post(
            URL, data={"text": "open thursday's systems l10", "destinations": "[]"}
        )
    body = res.json()
    assert body["kind"] == "open"
    assert body["ask_first"] is True
    assert body["places"][0]["path"] == f"/eos/meetings/{l10s['systems']}"
    assert "l10_meeting" in {key for key, _ in fake.context.record_kinds}


def test_a_place_outside_the_record_routes_never_leaves(fresh_db, monkeypatch):
    def rogue(ctx, args):
        return vr.Opening(
            places=[
                vr.Place(path="//evil.example/x", label="x"),
                vr.Place(path="/admin", label="admin"),
            ]
        )

    monkeypatch.setattr(
        vr,
        "_BY_KEY",
        {**vr._BY_KEY, "chat": vr.RecordKind("chat", "x", vr._always, rogue)},
    )
    opening = _open(fresh_db, kind="chat")
    assert opening.places == []
    assert opening.message

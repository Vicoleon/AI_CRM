"""An imported L10 follows its Google Calendar series, not a frozen pattern.

``eos_l10_meetings`` stores a recurring PATTERN captured once at import, and a
pattern never ends — so a team that cancelled the calendar event went on being
promised occurrences of a meeting that was not going to happen (a team
cancels its next L10s on the calendar, and the L10 page still shows the next
one).

What these lock down:

1. The three states are distinct, and ``unknown`` is NEVER a cancellation —
   a Google outage must not tell every team their L10 was called off.
2. Only the meeting's manager (the calendar organizer) writes the answer back
   to the shared row. An attendee who deleted their own copy gets a corrected
   READ and changes nothing for the team.
3. The reconciled cancellation actually suppresses the downstream nags: "your
   next L10" stops naming it.
4. An organizer's explicit "No" on their own event is a decline — the hole
   that kept AI Priorities prepping an owner for a meeting they stood down
   from.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from rubik_core.auth.registry import UserRegistry
from rubik_core.config import settings
from rubik_core.database.schemas import UserProfile
from rubik_core.eos.calendar_sync import (
    KIND_EVENT_DELETED,
    KIND_NOTHING_SCHEDULED,
    STATE_ACTIVE,
    STATE_CANCELLED,
    STATE_UNKNOWN,
    SeriesState,
    apply_to_meeting,
    is_calendar_cancelled,
    may_persist,
    resolve_series_state,
)

client = TestClient(app)

MANAGER = "mandy@example.com"
ATTENDEE = "andy@example.com"
SERIES = "series_l10"


def _profile(email: str, roles: list[str] | None = None) -> UserProfile:
    now = "2026-09-14T00:00:00"
    stored = UserRegistry().get_user(email)
    return UserProfile(
        id=stored.id if stored else email.split("@")[0],
        email=email,
        name=stored.name if stored else email.split("@")[0].title(),
        roles=roles or (stored.roles if stored else ["user"]),
        department=stored.department if stored else "general",
        created_at=now,
        updated_at=now,
        is_exec=bool(stored.is_exec) if stored else False,
    )


def _as_user(email: str, roles: list[str] | None = None):
    app.dependency_overrides[get_current_user] = lambda: _profile(email, roles)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db_path = tmp_path / "test_eos_l10_calendar_sync.db"
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


def _seed_meeting() -> str:
    """An imported L10: Wednesdays at 11:00, managed by MANAGER, ANDY on the
    roster. Returns the meeting id."""
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import L10Meeting, Person

    db = SessionLocal()
    try:
        manager = Person(
            first_name="Mandy",
            last_name="Manager",
            email=MANAGER,
            created_by=MANAGER,
        )
        db.add(manager)
        db.commit()
        db.refresh(manager)

        meeting = L10Meeting(
            name="Ops L10",
            day_of_week=2,  # Wednesday
            start_time="11:00",
            duration_minutes=90,
            manager_id=manager.id,
            roster_emails=[MANAGER, ATTENDEE],
            google_event_id=SERIES,
            created_by=MANAGER,
        )
        db.add(meeting)
        db.commit()
        db.refresh(meeting)
        return str(meeting.id)
    finally:
        db.close()


def _reload(meeting_id: str):
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import L10Meeting

    db = SessionLocal()
    try:
        return db.query(L10Meeting).filter(L10Meeting.id == UUID(meeting_id)).one()
    finally:
        db.close()


class _FakeCal:
    """Stands in for GoogleCalendarClient. ``answer`` is whatever
    ``list_event_instances`` should return — a list, SERIES_GONE, or None."""

    SERIES_GONE = "series_gone"
    answer: object = []
    available = True

    def __init__(self, user_email=None):
        self.user_email = user_email
        self.service = object() if self.available else None
        self.init_error = None if self.available else "no credentials"
        self.last_error = None

    def list_event_instances(self, series_id, **kwargs):
        return self.answer

    def get_event(self, event_id):
        return {}


def _install_fake(monkeypatch, answer, available=True):
    import rubik_core.tools.calendar as cal_mod

    fake = type("_Cal", (_FakeCal,), {"answer": answer, "available": available})
    monkeypatch.setattr(cal_mod, "GoogleCalendarClient", fake)
    return fake


def _instance(start: datetime, response_status: str | None = None) -> dict:
    attendee = {"email": MANAGER, "self": True, "organizer": True}
    if response_status:
        attendee["responseStatus"] = response_status
    return {
        "id": f"{SERIES}_{start:%Y%m%dT%H%M%SZ}",
        "status": "confirmed",
        "summary": "Ops L10",
        "start": {"dateTime": start.isoformat()},
        "organizer": {"email": MANAGER, "self": True},
        "attendees": [attendee, {"email": ATTENDEE}],
    }


# ── the resolver ───────────────────────────────────────────────────────────


def test_deleted_series_is_cancelled():
    cal = _FakeCal()
    cal.answer = _FakeCal.SERIES_GONE
    state = resolve_series_state(cal, SERIES, reader_email=MANAGER)
    assert state.state == STATE_CANCELLED
    assert state.next_start is None
    assert state.kind == KIND_EVENT_DELETED


def test_series_with_no_surviving_occurrence_is_cancelled():
    """Every occurrence cancelled one-by-one gates the same as a deleted
    series — there is no next meeting either way — but it is a DIFFERENT thing
    to say to a team, so the two carry distinct kinds."""
    cal = _FakeCal()
    cal.answer = []
    state = resolve_series_state(cal, SERIES, reader_email=MANAGER)
    assert state.state == STATE_CANCELLED
    assert state.kind == KIND_NOTHING_SCHEDULED
    # The prose is for humans; the kind is what anything branches on.
    assert state.kind != KIND_EVENT_DELETED


def test_api_failure_is_unknown_and_is_never_persisted():
    """The load-bearing distinction. A Google blip must not cancel meetings."""
    cal = _FakeCal()
    cal.answer = None
    cal.last_error = "boom"
    state = resolve_series_state(cal, SERIES, reader_email=MANAGER)
    assert state.state == STATE_UNKNOWN
    assert state.is_cancelled is False

    class _Row:
        calendar_state = STATE_ACTIVE
        next_occurrence_at = None
        calendar_checked_at = None

    row = _Row()
    assert apply_to_meeting(row, state) is False
    assert row.calendar_state == STATE_ACTIVE  # untouched


def test_unlinked_meeting_is_unknown_not_cancelled():
    """A hand-scheduled L10 has no calendar to check and must keep working."""
    state = resolve_series_state(_FakeCal(), None, reader_email=MANAGER)
    assert state.state == STATE_UNKNOWN


def test_next_start_is_googles_soonest_surviving_occurrence():
    now = datetime.now(timezone.utc)
    soon = now + timedelta(days=2)
    later = now + timedelta(days=9)
    cal = _FakeCal()
    # Deliberately out of order — the resolver must sort, not trust Google.
    cal.answer = [_instance(later), _instance(soon)]
    state = resolve_series_state(cal, SERIES, reader_email=MANAGER, now=now)
    assert state.state == STATE_ACTIVE
    assert state.next_start is not None
    assert abs((state.next_start - soon).total_seconds()) < 2


def test_meeting_underway_is_still_next():
    """Eight minutes into an 11:00 L10 the card must still show TODAY's
    meeting, not jump to next week."""
    now = datetime.now(timezone.utc)
    started = now - timedelta(minutes=8)
    next_week = started + timedelta(days=7)
    underway = _instance(started)
    underway["end"] = {"dateTime": (started + timedelta(minutes=90)).isoformat()}
    cal = _FakeCal()
    cal.answer = [underway, _instance(next_week)]
    state = resolve_series_state(cal, SERIES, reader_email=MANAGER, now=now)
    assert abs((state.next_start - started).total_seconds()) < 2

    # Once it has ended, next week's occurrence takes over.
    underway["end"] = {"dateTime": (now - timedelta(minutes=1)).isoformat()}
    state = resolve_series_state(cal, SERIES, reader_email=MANAGER, now=now)
    assert abs((state.next_start - next_week).total_seconds()) < 2


def test_declined_occurrence_is_skipped_but_series_stays_active():
    """ "Your next L10" is the next one you're going to. Declining one week is
    not cancelling the team's meeting, so the state stays active."""
    now = datetime.now(timezone.utc)
    skipped = now + timedelta(days=2)
    attending = now + timedelta(days=9)
    cal = _FakeCal()
    cal.answer = [_instance(skipped, "declined"), _instance(attending)]
    state = resolve_series_state(cal, SERIES, reader_email=MANAGER, now=now)
    assert state.state == STATE_ACTIVE
    assert abs((state.next_start - attending).total_seconds()) < 2


def test_only_the_organizer_may_write_back():
    meeting_id = _seed_meeting()
    meeting = _reload(meeting_id)
    assert may_persist(meeting, MANAGER, MANAGER) is True
    # An attendee who deleted their personal copy has not cancelled the team's
    # L10 and must not be able to blank the card for everybody else.
    assert may_persist(meeting, ATTENDEE, MANAGER) is False


# ── the endpoint ───────────────────────────────────────────────────────────


def test_manager_sees_cancelled_and_the_row_records_it(monkeypatch):
    meeting_id = _seed_meeting()
    _install_fake(monkeypatch, _FakeCal.SERIES_GONE)
    _as_user(MANAGER)

    r = client.get("/api/eos/l10/calendar-status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["calendar_available"] is True
    (status,) = body["statuses"]
    assert status["meeting_id"] == meeting_id
    assert status["state"] == STATE_CANCELLED
    assert status["kind"] == KIND_EVENT_DELETED
    assert status["persisted"] is True

    row = _reload(meeting_id)
    assert row.calendar_state == STATE_CANCELLED
    assert row.next_occurrence_at is None
    assert row.calendar_checked_at is not None


def test_attendee_gets_the_read_but_never_writes(monkeypatch):
    meeting_id = _seed_meeting()
    _install_fake(monkeypatch, _FakeCal.SERIES_GONE)
    _as_user(ATTENDEE)

    (status,) = client.get("/api/eos/l10/calendar-status").json()["statuses"]
    assert status["state"] == STATE_CANCELLED
    assert status["persisted"] is False
    assert _reload(meeting_id).calendar_state is None


def test_calendar_unavailable_falls_back_to_the_stored_answer(monkeypatch):
    meeting_id = _seed_meeting()
    row_state = STATE_ACTIVE
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import L10Meeting

    db = SessionLocal()
    try:
        row = db.query(L10Meeting).filter(L10Meeting.id == UUID(meeting_id)).one()
        row.calendar_state = row_state
        db.commit()
    finally:
        db.close()

    _install_fake(monkeypatch, None, available=False)
    _as_user(MANAGER)

    body = client.get("/api/eos/l10/calendar-status").json()
    assert body["calendar_available"] is False
    (status,) = body["statuses"]
    assert status["state"] == row_state
    assert status["persisted"] is False
    assert status["reason"]


def test_manual_meetings_are_not_reported(monkeypatch):
    """No calendar link → nothing to check, and no row in the response that
    the client could mistake for an answer."""
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import L10Meeting, Person

    db = SessionLocal()
    try:
        mgr = Person(first_name="M", last_name="M", email=MANAGER, created_by=MANAGER)
        db.add(mgr)
        db.commit()
        db.refresh(mgr)
        db.add(
            L10Meeting(
                name="Hand-scheduled L10",
                day_of_week=1,
                start_time="09:00",
                manager_id=mgr.id,
                created_by=MANAGER,
            )
        )
        db.commit()
    finally:
        db.close()

    _install_fake(monkeypatch, _FakeCal.SERIES_GONE)
    _as_user(MANAGER)
    assert client.get("/api/eos/l10/calendar-status").json()["statuses"] == []


def test_sync_on_a_cancelled_series_records_it(monkeypatch):
    """Hitting Sync on a meeting whose event is gone used to say "not found (or
    you don't have access)" — a permissions-shaped message for a cancellation —
    and left the row promising occurrences forever."""
    import rubik_core.tools.calendar as cal_mod

    meeting_id = _seed_meeting()

    class _Gone(_FakeCal):
        def get_event(self, event_id):
            return {}

    monkeypatch.setattr(cal_mod, "GoogleCalendarClient", _Gone)
    _as_user(MANAGER)

    r = client.post("/api/eos/l10/calendar-import", json={"google_event_id": SERIES})
    assert r.status_code == 404
    assert "cancelled" in r.json()["detail"].lower()
    assert _reload(meeting_id).calendar_state == STATE_CANCELLED


# ── the downstream nags ────────────────────────────────────────────────────


def test_cancelled_l10_stops_being_your_next_l10(monkeypatch):
    from backend.routers.eos.me import _find_next_l10_for
    from rubik_core.database.db import SessionLocal

    meeting_id = _seed_meeting()
    now = datetime.utcnow()

    db = SessionLocal()
    try:
        found = _find_next_l10_for(
            db=db,
            user_email=MANAGER,
            user_department="general",
            is_exec=False,
            now=now,
        )
        assert found is not None and found.meeting_id == meeting_id

        from rubik_core.database.eos import L10Meeting

        row = db.query(L10Meeting).filter(L10Meeting.id == UUID(meeting_id)).one()
        apply_to_meeting(row, SeriesState(STATE_CANCELLED))
        db.commit()

        assert (
            _find_next_l10_for(
                db=db,
                user_email=MANAGER,
                user_department="general",
                is_exec=False,
                now=now,
            )
            is None
        )
    finally:
        db.close()


def test_reconciled_next_occurrence_beats_the_pattern():
    """A moved meeting: the pattern still says Wednesday, Google says Friday."""
    from backend.routers.eos.me import _find_next_l10_for
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import L10Meeting

    meeting_id = _seed_meeting()
    now = datetime.utcnow()
    moved = now + timedelta(days=3, hours=4)

    db = SessionLocal()
    try:
        row = db.query(L10Meeting).filter(L10Meeting.id == UUID(meeting_id)).one()
        apply_to_meeting(
            row,
            SeriesState(STATE_ACTIVE, next_start=moved.replace(tzinfo=timezone.utc)),
        )
        db.commit()

        found = _find_next_l10_for(
            db=db,
            user_email=MANAGER,
            user_department="general",
            is_exec=False,
            now=now,
        )
        assert found is not None
        assert found.scheduled_for.startswith(moved.strftime("%Y-%m-%dT%H:%M"))
    finally:
        db.close()


def test_is_calendar_cancelled_reads_null_as_not_cancelled():
    meeting_id = _seed_meeting()
    assert is_calendar_cancelled(_reload(meeting_id)) is False


# ── the organizer's own "No" ───────────────────────────────────────────────


def test_organizer_explicit_decline_counts_as_declined():
    """Google lets you RSVP No to a meeting you own. A manager who does that
    to their own L10 has decided they aren't going — AI Priorities must stop
    prepping them for it."""
    from rubik_core.tools.calendar import (
        RSVP_DECLINED,
        RSVP_NONE,
        self_response_status,
    )

    event = {
        "organizer": {"email": MANAGER, "self": True},
        "attendees": [
            {
                "email": MANAGER,
                "self": True,
                "organizer": True,
                "responseStatus": "declined",
            }
        ],
    }
    assert self_response_status(event, MANAGER) == RSVP_DECLINED

    # …but an UNANSWERED RSVP on your own event still means committed: events
    # created through the API leave the organizer on needsAction, and dropping
    # somebody's own meeting over that is the worse bug.
    event["attendees"][0]["responseStatus"] = "needsAction"
    assert self_response_status(event, MANAGER) == RSVP_NONE

"""Automatic closure of L10 sessions nobody ever concluded.

An ``eos_l10_sessions`` row stays open (``actual_end IS NULL``) until a
facilitator clicks Conclude, and almost nobody does, so most open sessions
have had no write for days or weeks. That leaks out twice: the board offers
"Rejoin" for meetings that ended weeks ago, and ``/sessions/start`` reuses
the stale row so the next occurrence resumes a month-old session mid-agenda.

These cover the shared rule in ``rubik_core/eos/session_lifecycle.py`` and
all three call sites that depend on it:

1. The predicate itself — both arms must fire, and the IDS-parked case (a
   meeting generating no writes while people talk) must survive.
2. The board (``GET /api/eos/l10/``) stops advertising an abandoned session.
3. ``POST /sessions/start`` closes the abandoned row and opens a fresh one
   instead of silently resuming it.
4. The sweep endpoint closes, is idempotent, honours ``dry_run``, and never
   overrides a human Conclude.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from rubik_core.auth.registry import UserRegistry
from rubik_core.config import settings
from rubik_core.database.schemas import UserProfile
from rubik_core.eos.session_lifecycle import (
    BOX_GRACE,
    INACTIVITY_WINDOW,
    close_abandoned_sessions,
    live_session_ids,
    open_sessions,
)

client = TestClient(app)

MANAGER = "manager@example.com"
SWEEP_URL = "/api/agents/eos/l10/close-abandoned-sessions"


def _profile(email: str, roles: list[str] | None = None) -> UserProfile:
    now = "2026-08-18T00:00:00"
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
    db_path = tmp_path / "test_eos_l10_auto_close.db"
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


def _seed_meeting(duration_minutes: int = 90) -> str:
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import L10Meeting, Person

    db = SessionLocal()
    try:
        # eos_people.email is unique, so a second meeting reuses the manager.
        manager = db.query(Person).filter(Person.email == MANAGER).first()
        if manager is None:
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
            name="Weekly L10",
            duration_minutes=duration_minutes,
            manager_id=manager.id,
            created_by=MANAGER,
        )
        db.add(meeting)
        db.commit()
        db.refresh(meeting)
        return str(meeting.id)
    finally:
        db.close()


def _seed_session(meeting_id: str, *, started_ago: timedelta, idle: timedelta) -> str:
    """An open session that started ``started_ago`` and last wrote ``idle`` ago.

    ``updated_at`` carries ``onupdate=utcnow``, so it is set on the INSERT
    rather than written afterwards — an UPDATE would stamp it to now and
    silently make every session look fresh.
    """
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import L10Session

    now = datetime.utcnow()
    db = SessionLocal()
    try:
        session = L10Session(
            meeting_id=UUID(meeting_id),
            actual_start=now - started_ago,
            created_at=now - started_ago,
            updated_at=now - idle,
            step_started_at=now - idle,
            current_step=5,
            created_by=MANAGER,
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        return str(session.id)
    finally:
        db.close()


def _row(session_id: str):
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import L10Session

    db = SessionLocal()
    try:
        return db.query(L10Session).filter(L10Session.id == UUID(session_id)).one()
    finally:
        db.close()


def _scan():
    from rubik_core.database.db import SessionLocal

    db = SessionLocal()
    try:
        return open_sessions(db)
    finally:
        db.close()


def _meeting_in_list(meeting_id: str) -> dict:
    r = client.get("/api/eos/l10/")
    assert r.status_code == 200, r.text
    return next(m for m in r.json() if m["id"] == meeting_id)


# ── 1. the predicate ─────────────────────────────────────────────────────


def test_abandoned_needs_both_arms():
    """Past the box alone is not enough, and idle alone is not enough."""
    meeting_id = _seed_meeting(duration_minutes=90)
    now = datetime.utcnow()

    # Way past its 90-minute box, but someone wrote a minute ago — a meeting
    # that is running long is still a meeting.
    _seed_session(meeting_id, started_ago=timedelta(hours=8), idle=timedelta(minutes=1))
    assert not _scan()[0].is_abandoned(now)


def test_idle_but_inside_the_box_is_not_abandoned():
    """The IDS case: parked on one step while the room argues.

    Nothing writes to the session, attendance or signals during IDS — issues
    and to-dos land on their own tables — so a short-window inactivity rule
    would close a meeting mid-sentence.
    """
    meeting_id = _seed_meeting(duration_minutes=90)
    now = datetime.utcnow()
    _seed_session(
        meeting_id,
        started_ago=timedelta(minutes=50),
        idle=timedelta(minutes=45),
    )
    assert not _scan()[0].is_abandoned(now)


def test_past_box_and_long_idle_is_abandoned():
    meeting_id = _seed_meeting(duration_minutes=90)
    now = datetime.utcnow()
    _seed_session(
        meeting_id,
        started_ago=timedelta(days=7),
        idle=timedelta(days=7),
    )
    row = _scan()[0]
    assert row.is_abandoned(now)
    # The deadline is the LATER of the two arms.
    assert row.abandoned_at == max(
        row.last_activity + INACTIVITY_WINDOW, row.box_end + BOX_GRACE
    )


def test_attendance_and_signals_count_as_activity():
    """A check-in or a rabbit-hole press keeps a session alive even when the
    session row itself has not been touched since it started."""
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import L10Attendance, L10Signal, Person

    meeting_id = _seed_meeting()
    now = datetime.utcnow()
    session_id = _seed_session(
        meeting_id, started_ago=timedelta(days=2), idle=timedelta(days=2)
    )
    assert _scan()[0].is_abandoned(now)

    db = SessionLocal()
    try:
        person = Person(first_name="Ann", last_name="Attendee", email="ann@example.com")
        db.add(person)
        db.commit()
        db.refresh(person)
        db.add(
            L10Attendance(
                session_id=UUID(session_id),
                person_id=person.id,
                created_at=now - timedelta(minutes=2),
                updated_at=now - timedelta(minutes=2),
            )
        )
        db.commit()
    finally:
        db.close()
    assert not _scan()[0].is_abandoned(now), "attendance write should count"

    # Same for an in-meeting signal, on a second stale session.
    other_meeting = _seed_meeting()
    other_session = _seed_session(
        other_meeting, started_ago=timedelta(days=2), idle=timedelta(days=2)
    )
    db = SessionLocal()
    try:
        db.add(
            L10Signal(
                session_id=UUID(other_session),
                kind="rabbit_hole",
                created_at=now - timedelta(minutes=2),
            )
        )
        db.commit()
    finally:
        db.close()
    fresh = {str(r.session_id): r for r in _scan()}
    assert not fresh[other_session].is_abandoned(now), "signal write should count"


def test_a_longer_meeting_gets_a_longer_box():
    """``duration_minutes`` is the box, so an all-day session is not judged
    against the 90-minute default."""
    long_meeting = _seed_meeting(duration_minutes=480)
    now = datetime.utcnow()
    _seed_session(
        long_meeting,
        started_ago=timedelta(hours=5),
        idle=timedelta(hours=5),
    )
    row = _scan()[0]
    # Idle arm (4h) is already satisfied at 5h; the box arm (8h + 2h grace)
    # is what keeps it open.
    assert row.last_activity + INACTIVITY_WINDOW < now
    assert not row.is_abandoned(now)


# ── 2. the board ─────────────────────────────────────────────────────────


def test_board_hides_rejoin_for_an_abandoned_session():
    meeting_id = _seed_meeting()
    _as_user(MANAGER)

    live = _seed_session(
        meeting_id, started_ago=timedelta(minutes=20), idle=timedelta(minutes=1)
    )
    assert _meeting_in_list(meeting_id)["live_session_id"] == live

    # Age it out: same row, no writes for a week.
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import L10Session

    stale = datetime.utcnow() - timedelta(days=7)
    db = SessionLocal()
    try:
        row = db.query(L10Session).filter(L10Session.id == UUID(live)).one()
        row.actual_start = stale
        row.step_started_at = stale
        db.commit()
        # updated_at carries onupdate=utcnow, so the commit above just stamped
        # it to now; force it back with a direct UPDATE that bypasses the ORM.
        db.execute(
            L10Session.__table__.update()
            .where(L10Session.__table__.c.id == row.id)
            .values(updated_at=stale)
        )
        db.commit()
    finally:
        db.close()

    assert _meeting_in_list(meeting_id)["live_session_id"] is None
    # Read-only: the board must not have written actual_end.
    assert _row(live).actual_end is None


def test_board_still_shows_a_live_session():
    meeting_id = _seed_meeting()
    _as_user(MANAGER)
    r = client.post(
        "/api/eos/l10/sessions/start",
        json={"meeting_id": meeting_id, "actual_start": datetime.utcnow().isoformat()},
    )
    assert r.status_code == 200, r.text
    assert _meeting_in_list(meeting_id)["live_session_id"] == r.json()["id"]


def test_live_session_ids_picks_the_newest_when_several_are_open():
    from rubik_core.database.db import SessionLocal

    meeting_id = _seed_meeting()
    older = _seed_session(
        meeting_id, started_ago=timedelta(minutes=90), idle=timedelta(minutes=2)
    )
    newer = _seed_session(
        meeting_id, started_ago=timedelta(minutes=10), idle=timedelta(minutes=1)
    )
    db = SessionLocal()
    try:
        picked = live_session_ids(db, [UUID(meeting_id)])
    finally:
        db.close()
    assert str(next(iter(picked.values()))) == newer
    assert str(next(iter(picked.values()))) != older


# ── 3. /sessions/start ───────────────────────────────────────────────────


def test_start_closes_the_abandoned_row_and_opens_a_fresh_one():
    """The bug: next week's meeting resumed last month's session, inheriting
    its step cursor, attendance and ratings."""
    meeting_id = _seed_meeting()
    _as_user(MANAGER)
    stale = _seed_session(
        meeting_id, started_ago=timedelta(days=30), idle=timedelta(days=30)
    )

    r = client.post(
        "/api/eos/l10/sessions/start",
        json={"meeting_id": meeting_id, "actual_start": datetime.utcnow().isoformat()},
    )
    assert r.status_code == 200, r.text
    fresh = r.json()

    assert fresh["id"] != stale, "must not resume the abandoned session"
    assert fresh["current_step"] == 0, "a new meeting starts at the Segue"

    closed = _row(stale)
    assert closed.actual_end is not None
    assert closed.auto_closed is True
    # actual_end is the last real activity, not "now" — a session that died a
    # month ago must not report a month-long duration.
    assert closed.actual_end < datetime.utcnow() - timedelta(days=29)


def test_start_still_rejoins_a_genuinely_live_session():
    """The rejoin path must survive: a refresh mid-meeting returns the same
    row with its step and anchor untouched."""
    meeting_id = _seed_meeting()
    _as_user(MANAGER)
    live = _seed_session(
        meeting_id, started_ago=timedelta(minutes=30), idle=timedelta(minutes=2)
    )

    r = client.post(
        "/api/eos/l10/sessions/start",
        json={"meeting_id": meeting_id, "actual_start": datetime.utcnow().isoformat()},
    )
    assert r.status_code == 200, r.text
    assert r.json()["id"] == live
    assert r.json()["current_step"] == 5  # cursor preserved
    assert _row(live).actual_end is None


# ── 4. the sweep endpoint ────────────────────────────────────────────────


SCHEDULER_KEY = "test-scheduler-key"


@pytest.fixture
def scheduler_auth(monkeypatch):
    """The sweep authenticates on X-Scheduler-Key, exactly as Cloud Scheduler
    does. It resolves the admin fallback by calling ``get_current_user``
    directly, so a dependency override never reaches it — the header is the
    real path and the one worth pinning."""
    monkeypatch.setattr(settings, "SCHEDULER_API_KEY", SCHEDULER_KEY)
    return {"X-Scheduler-Key": SCHEDULER_KEY, "X-Cubie-Workspace": "test"}


def _sweep(headers: dict, dry_run: bool = False) -> dict:
    r = client.post(
        f"{SWEEP_URL}?dry_run={'true' if dry_run else 'false'}", headers=headers
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_sweep_rejects_an_unauthenticated_caller():
    app.dependency_overrides.clear()
    r = client.post(SWEEP_URL)
    assert r.status_code in (401, 403), r.text


def test_sweep_rejects_a_wrong_scheduler_key(scheduler_auth):
    r = client.post(
        SWEEP_URL,
        headers={"X-Scheduler-Key": "not-the-key", "X-Cubie-Workspace": "test"},
    )
    assert r.status_code == 403, r.text


def test_sweep_dry_run_writes_nothing(scheduler_auth):
    meeting_id = _seed_meeting()
    stale = _seed_session(
        meeting_id, started_ago=timedelta(days=3), idle=timedelta(days=3)
    )

    body = _sweep(scheduler_auth, dry_run=True)
    assert body["dry_run"] is True
    assert body["would_close"] == 1
    assert body["closed"] == 0
    assert body["sessions"][0]["session_id"] == stale
    assert _row(stale).actual_end is None


def test_sweep_closes_and_is_idempotent(scheduler_auth):
    meeting_id = _seed_meeting()
    stale = _seed_session(
        meeting_id, started_ago=timedelta(days=3), idle=timedelta(days=3)
    )
    live_meeting = _seed_meeting()
    live = _seed_session(
        live_meeting, started_ago=timedelta(minutes=20), idle=timedelta(minutes=1)
    )

    first = _sweep(scheduler_auth)
    assert first["closed"] == 1
    assert first["still_live"] == 1
    assert _row(stale).actual_end is not None
    assert _row(stale).auto_closed is True
    assert _row(live).actual_end is None

    second = _sweep(scheduler_auth)
    assert second["closed"] == 0, "nothing left to close"
    assert second["scanned"] == 1, "only the live session is still open"


def test_sweep_leaves_average_rating_null_so_it_is_not_a_real_conclude(
    scheduler_auth,
):
    """A human Conclude derives ``average_rating`` from per-person ratings.
    A sweep cannot invent those, so the row must stay distinguishable."""
    meeting_id = _seed_meeting()
    stale = _seed_session(
        meeting_id, started_ago=timedelta(days=3), idle=timedelta(days=3)
    )
    _sweep(scheduler_auth)
    row = _row(stale)
    assert row.auto_closed is True
    assert row.average_rating is None
    assert row.updated_by == "system:auto-close"


def test_a_human_conclude_is_never_overwritten():
    """If a facilitator concludes between the scan and the write, their
    Conclude — with real ratings — wins."""
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import L10Session

    meeting_id = _seed_meeting()
    session_id = _seed_session(
        meeting_id, started_ago=timedelta(days=3), idle=timedelta(days=3)
    )
    concluded_at = datetime.utcnow() - timedelta(days=2)

    db = SessionLocal()
    try:
        scanned = open_sessions(db)
        assert len(scanned) == 1

        # Simulate the racing Conclude landing first, in another connection.
        other = SessionLocal()
        try:
            row = (
                other.query(L10Session).filter(L10Session.id == UUID(session_id)).one()
            )
            row.actual_end = concluded_at
            row.average_rating = 9
            row.updated_by = MANAGER
            other.commit()
        finally:
            other.close()

        db.expire_all()
        close_abandoned_sessions(db)
    finally:
        db.close()

    row = _row(session_id)
    assert row.auto_closed is False
    assert row.average_rating == 9
    assert row.updated_by == MANAGER
    assert row.actual_end == concluded_at

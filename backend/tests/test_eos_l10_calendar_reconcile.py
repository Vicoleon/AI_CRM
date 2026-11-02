"""The nightly sweep asks Google about L10s nobody opened the board for.

``rubik_core/eos/calendar_sync.py`` could already reconcile one meeting, and
``GET /api/eos/l10/calendar-status`` called it — but only when a user opened
EOS → Meetings, and it wrote back only for that meeting's manager. Both
restrictions are correct; together they leave the original bug standing. A
manager who cancels their L10 on Google Calendar and never opens the board
leaves the row stale, nobody else is permitted to correct it, and the two
surfaces that read the stored answer (the PTO hand-off brief, "your next L10")
go on treating a dead meeting as live.

What these lock down:

1. The sweep reconciles a cancelled meeting with NOBODY opening the board —
   the whole reason the job exists.
2. ``unknown`` is still never persisted. This is the single most dangerous
   caller of that rule: a mis-firing endpoint mangles one meeting for one
   user, an unguarded sweep would mark every L10 in the company cancelled in
   one pass, overnight, with nobody watching.
3. It reads the MANAGER's calendar — the series lives on the organizer's — and
   it never writes a row ``may_persist`` would refuse.
4. Finding cancellations exits 0. Only being unable to CHECK exits non-zero,
   and "could not run" and "ran but Google answered nothing" are told apart.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from rubik_core.config import settings
from rubik_core.eos.calendar_sync import (
    STATE_ACTIVE,
    STATE_CANCELLED,
    is_calendar_cancelled,
)

MANAGER = "mandy@example.com"
OTHER_MANAGER = "morgan@example.com"
ATTENDEE = "andy@example.com"
SOLO = "sam@example.com"
SERIES = "series_l10"


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    # Job workspace validation is covered by test_job_bootstrap. Here the
    # sweep must actually execute so EXIT_BLIND cannot pass from a missing
    # workspace before Google is queried.
    monkeypatch.setattr("backend.jobs.bootstrap.bootstrap_job_workspace", lambda: True)
    db_path = tmp_path / "test_eos_l10_calendar_reconcile.db"
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    import rubik_core.database.db as dbmod

    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database import eos  # noqa: F401  — register EOS tables
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    yield


# ── seeding ────────────────────────────────────────────────────────────────


def _seed(
    *,
    manager_email: str | None = MANAGER,
    roster: list[str] | None = None,
    google_event_id: str | None = SERIES,
    name: str = "Ops L10",
    is_deleted: bool = False,
    calendar_state: str | None = None,
    checked_at: datetime | None = None,
) -> str:
    """One imported L10: Wednesdays 11:00. Returns the meeting id."""
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import L10Meeting, Person

    with SessionLocal() as db:
        manager_id = None
        if manager_email:
            person = (
                db.query(Person).filter(Person.email == manager_email).one_or_none()
            )
            if person is None:
                person = Person(
                    first_name=manager_email.split("@")[0].title(),
                    last_name="Manager",
                    email=manager_email,
                    created_by=manager_email,
                )
                db.add(person)
                db.commit()
                db.refresh(person)
            manager_id = person.id

        meeting = L10Meeting(
            name=name,
            day_of_week=2,
            start_time="11:00",
            duration_minutes=90,
            manager_id=manager_id,
            roster_emails=roster if roster is not None else [MANAGER, ATTENDEE],
            google_event_id=google_event_id,
            is_deleted=is_deleted,
            calendar_state=calendar_state,
            calendar_checked_at=checked_at,
            created_by=manager_email or SOLO,
        )
        db.add(meeting)
        db.commit()
        db.refresh(meeting)
        return str(meeting.id)


def _reload(meeting_id: str):
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import L10Meeting

    with SessionLocal() as db:
        return db.query(L10Meeting).filter(L10Meeting.id == UUID(meeting_id)).one()


def _instance(start: datetime, organizer: str = MANAGER) -> dict:
    return {
        "id": f"{SERIES}_{start:%Y%m%dT%H%M%SZ}",
        "status": "confirmed",
        "summary": "Ops L10",
        "start": {"dateTime": start.isoformat()},
        "organizer": {"email": organizer, "self": True},
        "attendees": [
            {"email": organizer, "self": True, "organizer": True},
            {"email": ATTENDEE},
        ],
    }


class _FakeCal:
    """Stands in for GoogleCalendarClient, recording who it impersonated.

    ``answers`` maps an impersonated address to what ``list_event_instances``
    should return for it — a list, SERIES_GONE, or None (could not ask).
    ``built`` is shared across instances so a test can assert the sweep opened
    ONE client per manager rather than one per meeting.
    """

    SERIES_GONE = "series_gone"
    answers: dict = {}
    built: list = []
    available = True
    explode_for: str | None = None

    def __init__(self, user_email=None):
        type(self).built.append(user_email)
        if self.explode_for and user_email == self.explode_for:
            raise RuntimeError("calendar exploded")
        self.user_email = user_email
        self.service = object() if self.available else None
        self.init_error = None if self.available else "no credentials"
        self.last_error = None

    def list_event_instances(self, series_id, **kwargs):
        return self.answers.get(self.user_email, [])

    def get_event(self, event_id):
        return {}


def _install(monkeypatch, answers, *, available=True, explode_for=None):
    import rubik_core.tools.calendar as cal_mod

    fake = type(
        "_Cal",
        (_FakeCal,),
        {
            "answers": answers,
            "built": [],
            "available": available,
            "explode_for": explode_for,
        },
    )
    monkeypatch.setattr(cal_mod, "GoogleCalendarClient", fake)
    return fake


def _sweep(**kwargs):
    from rubik_core.eos.calendar_reconcile import reconcile_l10_calendars

    return reconcile_l10_calendars(**kwargs)


# ── the headline: no board visit required ──────────────────────────────────


def test_a_cancelled_series_is_reconciled_with_nobody_opening_the_board():
    """The bug this job exists for.

    ``GET /calendar-status`` fixes this meeting only when its manager opens EOS
    → Meetings. The manager who cancelled it has no reason to. Overnight, the
    row corrects itself anyway.
    """
    meeting_id = _seed()
    with pytest.MonkeyPatch.context() as mp:
        _install(mp, {MANAGER: _FakeCal.SERIES_GONE})
        report = _sweep()

    assert report is not None
    assert report.cancelled == 1
    assert report.changed == 1
    row = _reload(meeting_id)
    assert row.calendar_state == STATE_CANCELLED
    assert row.next_occurrence_at is None
    assert row.calendar_checked_at is not None
    # And the gate both consumers read now says so.
    assert is_calendar_cancelled(row) is True


def test_an_active_series_takes_googles_date_not_the_stored_pattern():
    """A moved meeting is the quiet half of the same bug: the pattern says
    Wednesday forever, Google knows it is now Thursday."""
    meeting_id = _seed()
    moved = datetime.now(timezone.utc) + timedelta(days=3, hours=2)
    moved = moved.replace(microsecond=0)
    with pytest.MonkeyPatch.context() as mp:
        _install(mp, {MANAGER: [_instance(moved)]})
        report = _sweep()

    assert report.active == 1
    assert report.cancelled == 0
    row = _reload(meeting_id)
    assert row.calendar_state == STATE_ACTIVE
    assert row.next_occurrence_at == moved.astimezone(timezone.utc).replace(tzinfo=None)
    assert is_calendar_cancelled(row) is False


# ── the rule that must never bend ──────────────────────────────────────────


def test_an_unreadable_calendar_never_cancels_anything():
    """A Google outage must not mark every L10 in the company cancelled.

    ``None`` from ``list_event_instances`` is "we could not ask". The row keeps
    the answer it had — including keeping a previously-ACTIVE meeting active.
    """
    meeting_id = _seed(calendar_state=STATE_ACTIVE)
    with pytest.MonkeyPatch.context() as mp:
        _install(mp, {MANAGER: None})
        report = _sweep()

    assert report.unknown == 1
    assert report.known == 0
    assert report.changed == 0
    row = _reload(meeting_id)
    assert row.calendar_state == STATE_ACTIVE
    assert is_calendar_cancelled(row) is False


def test_missing_credentials_leave_every_row_untouched():
    """No Workspace credentials at all is the company-wide version of the
    outage above, and is the one that would do the most damage."""
    first = _seed(calendar_state=STATE_ACTIVE)
    second = _seed(manager_email=OTHER_MANAGER, name="Sales L10")
    with pytest.MonkeyPatch.context() as mp:
        _install(mp, {}, available=False)
        report = _sweep()

    assert report.known == 0
    assert report.unknown == 2
    assert _reload(first).calendar_state == STATE_ACTIVE
    assert _reload(second).calendar_state is None
    # Asked about meetings, learned nothing about any of them.
    assert report.is_blind is True


def test_a_quiet_night_is_not_blind():
    """``is_blind`` must not fire on a healthy sweep that found no changes —
    that distinction is what keeps the alert meaningful."""
    _seed()
    upcoming = datetime.now(timezone.utc) + timedelta(days=2)
    with pytest.MonkeyPatch.context() as mp:
        _install(mp, {MANAGER: [_instance(upcoming)]})
        report = _sweep()

    assert report.known == 1
    assert report.is_blind is False


def test_no_meetings_at_all_is_not_blind():
    """An empty EOS is a legitimately empty sweep, not an outage."""
    with pytest.MonkeyPatch.context() as mp:
        _install(mp, {})
        report = _sweep()

    assert report.asked == 0
    assert report.is_blind is False


# ── whose calendar, and who may write ──────────────────────────────────────


def test_the_sweep_reads_the_managers_calendar():
    """The series lives on the ORGANIZER's calendar, and the import resolves
    the manager from ``organizer.email`` — so the manager is who we ask."""
    _seed()
    with pytest.MonkeyPatch.context() as mp:
        fake = _install(mp, {MANAGER: _FakeCal.SERIES_GONE})
        _sweep()

    assert fake.built == [MANAGER]
    assert ATTENDEE not in fake.built


def test_one_client_per_manager_not_per_meeting():
    """Building a client costs a credential load plus an OAuth refresh; a
    manager running three L10s should pay for that once."""
    for name in ("Ops L10", "Sales L10", "Exec L10"):
        _seed(name=name)
    with pytest.MonkeyPatch.context() as mp:
        fake = _install(mp, {MANAGER: _FakeCal.SERIES_GONE})
        report = _sweep()

    assert fake.built == [MANAGER]
    assert report.calendars == 1
    assert report.asked == 3
    assert report.cancelled == 3


def test_a_meeting_nobody_speaks_for_is_reported_not_guessed_at():
    """No manager and a multi-person roster: there is no organizer to
    impersonate, so this meeting can never be reconciled — by this job or by
    the board. Saying so is the point; silently skipping it is how a
    permanently-stale row stays invisible."""
    meeting_id = _seed(manager_email=None, roster=[MANAGER, ATTENDEE])
    with pytest.MonkeyPatch.context() as mp:
        fake = _install(mp, {MANAGER: _FakeCal.SERIES_GONE})
        report = _sweep()

    assert report.no_authority == 1
    assert report.asked == 0
    assert fake.built == []
    assert _reload(meeting_id).calendar_state is None


def test_a_solo_meeting_with_no_manager_is_its_owners_own_slot():
    """``may_persist``'s other branch: a roster of exactly one person is that
    person's meeting, and they speak for it with no manager set. The sweep
    uses the same rule rather than inventing a second one."""
    meeting_id = _seed(manager_email=None, roster=[SOLO])
    with pytest.MonkeyPatch.context() as mp:
        fake = _install(mp, {SOLO: _FakeCal.SERIES_GONE})
        report = _sweep()

    assert fake.built == [SOLO]
    assert report.cancelled == 1
    assert report.unauthorized == 0
    assert _reload(meeting_id).calendar_state == STATE_CANCELLED


def test_the_sweep_never_writes_a_row_may_persist_would_refuse():
    """The authority rule is not re-decided here. If the candidate reader and
    ``may_persist`` ever disagree the sweep declines to write, rather than the
    job holding an authority the endpoint does not grant."""
    import rubik_core.eos.calendar_reconcile as mod

    meeting_id = _seed()
    with pytest.MonkeyPatch.context() as mp:
        _install(mp, {MANAGER: _FakeCal.SERIES_GONE})
        # Patched by STRING target on the module that BINDS it — calendar_sync
        # is imported at module scope there, so patching the source module
        # would leave the binding under test untouched.
        mp.setattr(
            "rubik_core.eos.calendar_reconcile.may_persist", lambda *a, **k: False
        )
        report = mod.reconcile_l10_calendars()

    assert report.unauthorized == 1
    assert report.changed == 0
    assert _reload(meeting_id).calendar_state is None


# ── scope ──────────────────────────────────────────────────────────────────


def test_manually_scheduled_and_deleted_meetings_are_left_alone():
    """A meeting with no ``google_event_id`` has no calendar to ask about; its
    stored pattern is the only truth it has ever had."""
    manual = _seed(google_event_id=None, name="Hand-scheduled L10")
    deleted = _seed(is_deleted=True, name="Retired L10")
    with pytest.MonkeyPatch.context() as mp:
        fake = _install(mp, {MANAGER: _FakeCal.SERIES_GONE})
        report = _sweep()

    assert report.asked == 0
    assert fake.built == []
    assert _reload(manual).calendar_state is None
    assert _reload(deleted).calendar_state is None


def test_one_broken_calendar_does_not_end_the_sweep():
    """A single manager's revoked access must not leave every other team's
    meeting unreconciled."""
    broken = _seed(name="Broken L10")
    healthy = _seed(manager_email=OTHER_MANAGER, name="Healthy L10")
    with pytest.MonkeyPatch.context() as mp:
        _install(
            mp,
            {OTHER_MANAGER: _FakeCal.SERIES_GONE},
            explode_for=MANAGER,
        )
        report = _sweep()

    assert report is not None
    assert report.cancelled == 1
    assert report.unknown == 1
    assert _reload(healthy).calendar_state == STATE_CANCELLED
    assert _reload(broken).calendar_state is None


def test_limit_takes_the_least_recently_checked_first():
    """So a capped run rotates instead of re-asking about the same prefix every
    night, and the never-checked meetings this job exists for go first."""
    old = datetime(2026, 1, 1, tzinfo=timezone.utc).replace(tzinfo=None)
    recent = datetime.now(timezone.utc).replace(tzinfo=None)
    _seed(name="Checked recently", calendar_state=STATE_ACTIVE, checked_at=recent)
    stale = _seed(name="Checked long ago", calendar_state=STATE_ACTIVE, checked_at=old)
    never = _seed(name="Never checked")

    with pytest.MonkeyPatch.context() as mp:
        _install(mp, {MANAGER: _FakeCal.SERIES_GONE})
        report = _sweep(limit=2)

    assert report.asked == 2
    assert report.deferred == 1
    assert _reload(never).calendar_state == STATE_CANCELLED
    assert _reload(stale).calendar_state == STATE_CANCELLED


def test_dry_run_reports_without_writing():
    """The right first command against real data: this job's purpose is to
    mark meetings cancelled, so seeing which ones before it does is cheap."""
    meeting_id = _seed()
    with pytest.MonkeyPatch.context() as mp:
        _install(mp, {MANAGER: _FakeCal.SERIES_GONE})
        report = _sweep(dry_run=True)

    assert report.cancelled == 1
    assert report.changed == 0
    assert _reload(meeting_id).calendar_state is None
    assert _reload(meeting_id).calendar_checked_at is None


def test_a_sweep_that_cannot_run_returns_none_not_an_empty_report():
    """``None`` means "did not check". An empty report means "checked, nothing
    to do". Collapsing the two is how a backstop reports success while blind."""
    import rubik_core.eos.calendar_reconcile as mod

    with pytest.MonkeyPatch.context() as mp:
        _install(mp, {})
        mp.setattr(mod, "_meetings_to_check", _boom)
        assert mod.reconcile_l10_calendars() is None


def _boom(*_a, **_k):
    raise RuntimeError("no database")


# ── the job: flags and exit codes ──────────────────────────────────────────


def test_finding_cancellations_exits_zero():
    """A cancellation is the deliverable, not a failure. Exiting non-zero here
    would page through "Job execution failed" every time a team cancelled a
    meeting, and teach the recipient that this job is flaky."""
    from backend.jobs.l10_calendar_reconcile_job import EXIT_OK, main

    _seed()
    with pytest.MonkeyPatch.context() as mp:
        _install(mp, {MANAGER: _FakeCal.SERIES_GONE})
        assert main([]) == EXIT_OK


def test_a_sweep_that_could_not_run_exits_error():
    import rubik_core.eos.calendar_reconcile as mod
    from backend.jobs.l10_calendar_reconcile_job import EXIT_ERROR, main

    with pytest.MonkeyPatch.context() as mp:
        _install(mp, {})
        mp.setattr(mod, "_meetings_to_check", _boom)
        assert main([]) == EXIT_ERROR


def test_a_blind_sweep_exits_distinctly_from_a_broken_one():
    """Ran fine, Google answered nothing: the sweep is healthy and the problem
    is upstream. A separate code so whoever opens the alert knows which."""
    from backend.jobs.l10_calendar_reconcile_job import EXIT_BLIND, main

    _seed()
    with pytest.MonkeyPatch.context() as mp:
        _install(mp, {}, available=False)
        assert main([]) == EXIT_BLIND


def test_the_job_passes_its_flags_through():
    from backend.jobs.l10_calendar_reconcile_job import EXIT_OK, main

    meeting_id = _seed()
    with pytest.MonkeyPatch.context() as mp:
        _install(mp, {MANAGER: _FakeCal.SERIES_GONE})
        assert main(["--dry-run", "--limit", "5"]) == EXIT_OK

    assert _reload(meeting_id).calendar_state is None


# ── the registry ───────────────────────────────────────────────────────────


def test_the_job_is_registered_and_honest_about_being_unscheduled():
    """``dispatched_from`` names the code in THIS repo that launches a job, and
    nothing here launches this one — it fires from Cloud Scheduler, which lives
    in GCP rather than git. So this stays in ``undispatched_worker_jobs()``
    permanently, exactly as the scheduled ``stale-work-sweep`` does; the entry's
    comment carries the cadence that git cannot verify.

    What the assertion is really pinning: nobody may quietly point
    ``dispatched_from`` at ``GET /api/eos/l10/calendar-status``. That endpoint
    reconciles the same meetings, so it looks like a dispatcher — but only for
    the ones a manager opens the board for, which is the exact gap this job
    exists to close. Claiming it as the dispatcher would record the hole as
    plugged."""
    from backend.jobs import (
        WORKER_JOBS,
        undispatched_worker_jobs,
        worker_job_args,
    )

    entry = WORKER_JOBS["l10-calendar-reconcile"]
    assert entry.module == "backend.jobs.l10_calendar_reconcile_job"
    assert entry.wants_cadence
    assert "l10-calendar-reconcile" in undispatched_worker_jobs()
    assert worker_job_args("l10-calendar-reconcile") == [
        "-m",
        "backend.jobs.l10_calendar_reconcile_job",
    ]

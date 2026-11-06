"""The backstop for a scoring run that failed WITHOUT failing.

The job-failure alert makes a crashed Cloud Run Job page somebody. That covers a
job that exits non-zero — but not the other two shapes of the same silence:

* the job exits 0 having written nothing (a success-shaped failure);
* the dispatch never produced an execution at all, because the launch failed
  and the work degraded to the in-process path Cloud Run CPU-throttles.

In both, the endpoint has already returned 202 and the person is looking at a
screen that says their answers are being scored. Somebody sat in that state for
eight days.

The detection deliberately does NOT live on the router's ``delayed`` branch,
which computes the same fact. The page polls for ~7.5 minutes
(``MAX_POLLS * POLL_MS``) and the threshold is 20, so the automatic poll stops
while the state is still ``pending``; ``delayed`` is only ever computed when a
human comes back later and presses the button. An alert wired there would fire
only for the people who chased their result and stay silent for anyone who gave
up — so it is a nightly server-side sweep instead. These tests pin the query's
definition of "never became a profile"; the sweep that runs it and the token the
alert greps for are covered in ``test_stale_work_sweep.py``, which owns the
reporting side for all three features.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest

from rubik_core.config import settings
from rubik_core.smart_teams.profile import (
    SCORING_STALE_SECONDS,
    stale_scoring_interviews,
)

DREW = "drew@acme.example"
LEE = "lee@acme.example"


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    """A throwaway SQLite database bound to the module-level ``SessionLocal``.

    Same shape as ``test_smart_teams_score_job.py``: the job opens its own
    sessions because it is a separate process in production, so re-pointing the
    shared engine is the only way to put it on a test database. Restored on the
    way out — a ``tmp_path`` URL left bound fails a LATER module and blames it
    for this one's mess.
    """
    import rubik_core.database.db as dbmod

    previous_url = settings.DATABASE_URL
    settings.DATABASE_URL = f"sqlite:///{tmp_path / 'st_stale.db'}"
    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database import smart_teams  # noqa: F401 — registers tables
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    try:
        yield engine
    finally:
        settings.DATABASE_URL = previous_url
        dbmod._engine = None


def _session():
    from rubik_core.database.db import SessionLocal

    return SessionLocal()


def _interview(
    db,
    email: str,
    *,
    minutes_ago: float,
    status: str = "completed",
    profile_id: str | None = None,
) -> str:
    """One interview row, completed ``minutes_ago``."""
    from rubik_core.database.smart_teams import SmartTeamsInterview

    completed = datetime.utcnow() - timedelta(minutes=minutes_ago)
    row = SmartTeamsInterview(
        id=uuid.uuid4().hex,
        user_email=email,
        status=status,
        sections_completed=[],
        answers={},
        completed_at=completed if status == "completed" else None,
        profile_id=profile_id,
    )
    db.add(row)
    db.commit()
    return row.id


def _profile(
    db,
    email: str,
    *,
    version: int,
    scored_minutes_ago: float | None,
    current: bool = True,
) -> str:
    from rubik_core.database.smart_teams import SmartTeamsProfile

    row = SmartTeamsProfile(
        id=uuid.uuid4().hex,
        user_email=email,
        version=version,
        status="current" if current else "superseded",
        is_current=True if current else None,
        scored_at=(
            None
            if scored_minutes_ago is None
            else datetime.utcnow() - timedelta(minutes=scored_minutes_ago)
        ),
    )
    db.add(row)
    db.commit()
    return row.id


# ── What counts as stale ─────────────────────────────────────────────────────


def test_a_scoring_run_still_inside_the_threshold_is_not_stale():
    """Slow is not broken. The threshold is generous on purpose."""
    with _session() as db:
        _interview(db, DREW, minutes_ago=5)
        assert stale_scoring_interviews(db) == []


def test_a_completed_interview_with_no_profile_past_the_threshold_is_stale():
    with _session() as db:
        interview_id = _interview(db, DREW, minutes_ago=45)
        stale = stale_scoring_interviews(db)
        assert [row.id for row in stale] == [interview_id]


def test_a_linked_profile_id_clears_it():
    """``profile_id`` is the authoritative link ST-4 writes."""
    with _session() as db:
        _interview(db, DREW, minutes_ago=45, profile_id=uuid.uuid4().hex)
        assert stale_scoring_interviews(db) == []


def test_an_unfinished_interview_is_never_stale():
    """Nobody is waiting on a result they have not asked for yet."""
    with _session() as db:
        _interview(db, DREW, minutes_ago=600, status="in_progress")
        _interview(db, LEE, minutes_ago=600, status="abandoned")
        assert stale_scoring_interviews(db) == []


def test_a_profile_scored_after_the_interview_clears_it():
    """The belt-and-braces case: scoring wrote a profile but not the link.

    The router treats a live current profile as proof that scoring finished, so
    a sweep that ignored this would report a healthy person as broken.
    """
    with _session() as db:
        _interview(db, DREW, minutes_ago=45)
        _profile(db, DREW, version=1, scored_minutes_ago=40)
        assert stale_scoring_interviews(db) == []


def test_a_lost_RETAKE_is_stale_even_though_the_person_has_a_live_profile():
    """The case a person-level "has a profile?" test would call healthy.

    Someone with version 1 retakes and the scoring run dies. They still have a
    current profile, so asking "does this person have a profile?" answers yes
    and the lost retake is never reported — while the person waits for a version
    that is never coming.
    """
    with _session() as db:
        _profile(db, DREW, version=1, scored_minutes_ago=10_000)
        retake_id = _interview(db, DREW, minutes_ago=45)
        assert [row.id for row in stale_scoring_interviews(db)] == [retake_id]


def test_a_profile_scored_before_the_interview_does_not_mask_it():
    """Direction matters: only a profile that POST-dates the interview counts."""
    with _session() as db:
        interview_id = _interview(db, DREW, minutes_ago=45)
        _profile(db, DREW, version=1, scored_minutes_ago=90)
        assert [row.id for row in stale_scoring_interviews(db)] == [interview_id]


def test_a_profile_with_no_scored_at_does_not_mask_it():
    """A NULL ``scored_at`` is not evidence of anything."""
    with _session() as db:
        interview_id = _interview(db, DREW, minutes_ago=45)
        _profile(db, DREW, version=1, scored_minutes_ago=None)
        assert [row.id for row in stale_scoring_interviews(db)] == [interview_id]


def test_the_longest_suffering_person_is_reported_first():
    """Oldest first, so a backlog is triaged in the order people were failed."""
    with _session() as db:
        older = _interview(db, DREW, minutes_ago=5_000)
        newer = _interview(db, LEE, minutes_ago=45)
        assert [row.id for row in stale_scoring_interviews(db)] == [older, newer]


def test_one_bad_week_cannot_become_an_unbounded_log_flood():
    with _session() as db:
        for _ in range(6):
            _interview(db, DREW, minutes_ago=45)
        assert len(stale_scoring_interviews(db, limit=3)) == 3


# ── Drift guard ──────────────────────────────────────────────────────────────


def test_the_router_and_the_alert_share_one_threshold():
    """Two literals would drift, and the drift is invisible.

    The alert would then fire at a different moment from the screen that tells
    somebody their result is missing.
    """
    from backend.routers import smart_teams as router

    assert router.SCORING_STALE_SECONDS is SCORING_STALE_SECONDS

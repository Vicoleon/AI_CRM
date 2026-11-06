"""Executive dashboard — the weekly Execution Trends series carry real numbers.

Regression guard for the bug where three of the four trend charts (meeting
health, rock status, issues opened) rendered permanently empty while "% of
issues solved" rendered fine.

`Quarter.start_date`/`end_date` are DateTime columns, so `_weeks_in_period`
was handed datetimes and returned datetimes. The three broken builders bucket
their rows by `.date()` and then look those buckets up by the week values —
and a `datetime` key never equals (nor hashes like) the `date` key it came
from, so every lookup missed and every week reported zero. `_issue_solved_trend`
compares timestamps directly instead of doing dict lookups, which is exactly
why it was the only chart that worked.

Temp SQLite per test — no Cloud SQL.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from rubik_core.config import settings


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db_path = tmp_path / "test_dashboard_trends.db"
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    import rubik_core.database.db as dbmod

    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database import eos  # noqa: F401
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    yield


def _session():
    from rubik_core.database.db import SessionLocal

    return SessionLocal()


# Anchor everything to a fixed Monday so week bucketing is unambiguous.
MONDAY = date(2026, 7, 6)


def _seed(db):
    """One quarter whose window brackets MONDAY, plus one rated L10 session,
    one rock weekly update, and two issues — all inside that same week."""
    from rubik_core.database.eos import (
        Issue,
        L10Meeting,
        L10Session,
        Organization,
        Person,
        Quarter,
        Rock,
        RockStatus,
        RockWeeklyUpdate,
        Visibility,
    )

    org = Organization(name="Trends Test Org", created_by="seed")
    db.add(org)
    db.commit()
    db.refresh(org)

    # DateTime bounds — mirrors the real schema (and the real seeded rows).
    quarter = Quarter(
        organization_id=org.id,
        name="Trends Q",
        start_date=datetime(2026, 7, 1),
        end_date=datetime(2026, 9, 30, 23, 59, 59),
        is_current=False,
        created_by="seed",
    )
    person = Person(
        first_name="Trend",
        last_name="Owner",
        email="trend@example.com",
        created_by="seed",
    )
    db.add_all([quarter, person])
    db.commit()
    db.refresh(quarter)
    db.refresh(person)

    meeting = L10Meeting(name="Trends L10", department_name=None, created_by="seed")
    db.add(meeting)
    db.commit()
    db.refresh(meeting)

    db.add(
        L10Session(
            meeting_id=meeting.id,
            actual_start=datetime.combine(MONDAY, datetime.min.time())
            + timedelta(hours=9),
            average_rating=9,
            created_by="seed",
        )
    )

    rock = Rock(
        quarter_id=quarter.id,
        owner_id=person.id,
        description="Trend rock",
        status=RockStatus.ON_TRACK,
        visibility=Visibility.COMPANY,
        created_by="seed",
    )
    db.add(rock)
    db.commit()
    db.refresh(rock)

    db.add(
        RockWeeklyUpdate(
            rock_id=rock.id,
            status=RockStatus.ON_TRACK,
            percent_complete=40,
            update_date=MONDAY + timedelta(days=2),
            created_by="seed",
        )
    )
    for n in range(2):
        db.add(
            Issue(
                description=f"Trend issue {n}",
                created_at=datetime.combine(MONDAY, datetime.min.time())
                + timedelta(days=1, hours=n),
                created_by="seed",
            )
        )
    db.commit()
    return quarter


def test_weeks_in_period_returns_dates_not_datetimes():
    """The bucket keys the trend builders produce are plain dates, so the
    week list has to be too."""
    from backend.routers.eos.dashboard import _weeks_in_period

    weeks = _weeks_in_period(datetime(2026, 7, 1), datetime(2026, 9, 30, 23, 59, 59))

    assert weeks, "expected a week per week of the quarter"
    assert all(type(w) is date for w in weeks), (
        f"got {type(weeks[0]).__name__} — datetime keys silently miss every "
        "date-keyed bucket and zero out the charts"
    )
    assert weeks[0] == date(2026, 6, 29)  # snapped back to Monday
    assert all(w.weekday() == 0 for w in weeks)


def test_trend_series_report_the_rows_that_exist():
    from backend.routers.eos.dashboard import (
        _issue_trend,
        _meeting_health_trend,
        _rock_status_trend,
        _weeks_in_period,
    )

    db = _session()
    try:
        quarter = _seed(db)
        weeks = _weeks_in_period(quarter.start_date, quarter.end_date)
        assert MONDAY in weeks

        health = _meeting_health_trend(db, weeks, None)
        issues = _issue_trend(db, weeks, None)
        rocks = _rock_status_trend(db, weeks, quarter.id, None)

        by_week = {date.fromisoformat(p["week_start"]): p for p in health}
        assert by_week[MONDAY]["avg_rating"] == 9.0
        assert by_week[MONDAY]["samples"] == 1

        by_week = {date.fromisoformat(p["week_start"]): p for p in issues}
        assert by_week[MONDAY]["opened"] == 2
        assert sum(p["opened"] for p in issues) == 2

        by_week = {date.fromisoformat(p["week_start"]): p for p in rocks}
        assert by_week[MONDAY]["on_track"] == 1
        assert by_week[MONDAY]["off_track"] == 0
    finally:
        db.close()

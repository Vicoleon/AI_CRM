"""Loose Ends — the weekly "Loose ends over 7 days" scorecard cell.

Pins the conventions shared with ``scorecard_sync/hubspot_green.py`` so the two
writers into the Sales scorecard agree: the Sunday anchor computed from the
America/Chicago day (never UTC — a Sunday-night run must land in the week that
is ending, not the next one), an idempotent upsert, and the rule that the
morning job never creates a measurable. ``setup_metrics`` is the one place rows
are created: per AE, owned by their ``eos_people`` row, and listed back when
there is none.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from rubik_core.brand import PRODUCT_NAME
from rubik_core.company.operations import OperationsConfig
from rubik_core.database.eos import Person, ScorecardEntry, ScorecardMetric
from rubik_core.database.loop_closure import LoopItem
from rubik_core.loop_closure import scorecard

NOW = datetime(2026, 10, 8, 12, 10)  # naive UTC: Thu 07:10 Chicago
AMY = "amy@acme.example"
ZED = "zed@acme.example"
MIA = "mia@acme.example"
METRIC = "Loose ends over 7 days"


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from rubik_core.database.models import Base, Department

    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr("rubik_core.database.db.SessionLocal", Session)
    monkeypatch.setattr(
        "rubik_core.company.operations.get_operations_config",
        lambda: OperationsConfig(loop_closure_scorecard_metric=METRIC),
    )
    session = Session()
    session.add(Department(name="Sales"))
    session.add(Department(name="Systems"))
    session.commit()
    yield session
    session.close()


def _person(db, email, first) -> Person:
    p = Person(first_name=first, last_name="Rep", email=email)
    db.add(p)
    db.commit()
    return p


def _metric(db, person, *, name=METRIC, department="Sales", is_exec=False):
    m = ScorecardMetric(
        owner_id=person.id, department_name=department, name=name, is_exec=is_exec
    )
    db.add(m)
    db.commit()
    return m


def _open(db, owner, key, days, status="open"):
    opened = NOW - timedelta(days=days)
    db.add(
        LoopItem(
            owner_email=owner,
            kind="unlogged",
            source="gmail",
            source_key=key,
            status=status,
            title="",
            opened_at=opened,
            first_flagged_at=opened,
            last_checked_at=NOW,
        )
    )
    db.commit()


def _entries(db, metric):
    return db.query(ScorecardEntry).filter(ScorecardEntry.metric_id == metric.id).all()


def test_the_week_is_anchored_on_the_local_day_not_utc():
    # Monday 03:00 UTC is still Sunday evening in Chicago: the week ending.
    sunday_night = datetime(2026, 10, 12, 3, 0)
    assert scorecard.week_anchor(sunday_night) == date(2026, 10, 4)
    assert scorecard.week_anchor(NOW) == date(2026, 10, 4)
    assert scorecard.week_anchor(datetime(2026, 10, 12, 15, 0)) == date(2026, 10, 11)


def test_write_weekly_upserts_the_count_over_seven_days(db):
    metric = _metric(db, _person(db, AMY, "Amy"))
    _open(db, AMY, "gmail:a", 9)
    _open(db, AMY, "gmail:b", 7)  # at the escalation day: counts
    _open(db, AMY, "gmail:c", 3)
    _open(db, AMY, "gmail:d", 20, status="closed")

    report = scorecard.write_weekly(db, now=NOW, emails=[AMY])
    assert report["inserted"] == 1 and report["week_start"] == "2026-10-04"
    [entry] = _entries(db, metric)
    assert entry.value == "2"
    assert entry.week_start_date == datetime(2026, 10, 4)  # a Sunday, midnight
    assert entry.created_by == scorecard.ACTOR

    again = scorecard.write_weekly(db, now=NOW, emails=[AMY])
    assert again["unchanged"] == 1 and again["inserted"] == 0
    assert len(_entries(db, metric)) == 1

    _open(db, AMY, "gmail:e", 12)
    later = scorecard.write_weekly(db, now=NOW + timedelta(days=1), emails=[AMY])
    assert later["updated"] == 1
    db.refresh(entry)
    assert entry.value == "3" and entry.updated_by == scorecard.ACTOR


def test_the_over_seven_count_ages_items_the_way_the_page_does(db):
    from rubik_core.loop_closure import views

    # A HubSpot task due Oct 1 at 08:00 Chicago: 6 days 23 hours ago, 7 days
    # overdue by the calendar. The page shows "7 days"; so does the scorecard.
    db.add(
        LoopItem(
            owner_email=AMY,
            kind="todo",
            source="hubspot_task",
            source_key="hstask:t1",
            status="open",
            title="",
            opened_at=datetime(2026, 10, 1, 13, 0),
            first_flagged_at=NOW,
            last_checked_at=NOW,
        )
    )
    db.commit()
    assert scorecard.over_escalation_counts(db, [AMY], now=NOW) == {AMY: 1}
    assert views.mine_view(db, AMY, now=NOW)["totals"]["over_escalation"] == 1


def test_write_weekly_never_creates_a_metric(db):
    _person(db, AMY, "Amy")
    _open(db, AMY, "gmail:a", 9)
    report = scorecard.write_weekly(db, now=NOW, emails=[AMY])
    assert report["metrics_found"] == 0 and report["inserted"] == 0
    assert db.query(ScorecardMetric).count() == 0


def test_unconfigured_workspace_metric_never_creates_or_writes_a_blank_row(
    db, monkeypatch
):
    monkeypatch.setattr(
        "rubik_core.company.operations.get_operations_config", OperationsConfig
    )
    _person(db, AMY, "Amy")
    assert scorecard.status(db, [AMY])["can_setup"] is False
    with pytest.raises(scorecard.ScorecardMetricUnconfigured):
        scorecard.setup_metrics(db, actor="lead@acme.example", emails=[AMY])
    report = scorecard.write_weekly(db, now=NOW, emails=[AMY])
    assert report["inserted"] == 0 and report["metrics_found"] == 0
    assert db.query(ScorecardMetric).count() == 0


def test_only_sales_non_exec_rows_with_the_exact_name_are_written(db):
    amy = _person(db, AMY, "Amy")
    _metric(db, amy, department="Systems")
    _metric(db, amy, is_exec=True)
    _metric(db, amy, name="Loose ends")
    report = scorecard.write_weekly(db, now=NOW, emails=[AMY])
    assert report["metrics_found"] == 0
    assert db.query(ScorecardEntry).count() == 0


def test_a_metric_whose_owner_was_not_scanned_is_skipped(db):
    metric = _metric(db, _person(db, ZED, "Zed"))
    report = scorecard.write_weekly(db, now=NOW, emails=[AMY])
    assert report["skipped_not_on_roster"] == 1
    assert _entries(db, metric) == []


def test_a_dry_run_writes_nothing(db):
    metric = _metric(db, _person(db, AMY, "Amy"))
    report = scorecard.write_weekly(db, now=NOW, dry_run=True, emails=[AMY])
    assert report["inserted"] == 1 and report["dry_run"] is True
    assert _entries(db, metric) == []


def test_setup_metrics_creates_one_row_per_ae_with_an_eos_person(db):
    amy = _person(db, AMY, "Amy")
    zed = _person(db, ZED, "Zed")
    _metric(db, amy)
    result = scorecard.setup_metrics(
        db, actor="Lead@ACME.example", emails=[AMY, ZED, MIA]
    )
    assert result == {"created": 1, "skipped_no_person": [MIA], "already": 1}
    [created] = (
        db.query(ScorecardMetric).filter(ScorecardMetric.owner_id == zed.id).all()
    )
    assert created.name == METRIC
    assert created.department_name == "Sales" and created.is_exec is False
    assert created.value_type == "number"
    assert created.comparator == "<=" and created.target_value == "0"
    assert created.frequency == "weekly"
    assert PRODUCT_NAME in created.description
    assert created.created_by == "lead@acme.example"
    # Pressing it again changes nothing.
    again = scorecard.setup_metrics(
        db, actor="lead@acme.example", emails=[AMY, ZED, MIA]
    )
    assert again == {"created": 0, "skipped_no_person": [MIA], "already": 2}


def test_status_reports_what_is_missing(db):
    _metric(db, _person(db, AMY, "Amy"))
    _person(db, MIA, "Mia")  # a seat, no row: setup can help
    assert scorecard.status(db, [AMY, ZED, MIA]) == {
        "metric_name": METRIC,
        "metrics_found": 1,
        "people_without_metric": 2,
        "people_without_seat": [ZED],
        "can_setup": True,
    }


def test_setup_is_not_offered_forever_for_people_it_always_skips(db):
    _metric(db, _person(db, AMY, "Amy"))
    # ZED has no Accountability Chart seat: setup skips ZED every time, so the
    # button must not keep coming back for ZED alone.
    status = scorecard.status(db, [AMY, ZED])
    assert status["people_without_metric"] == 1
    assert status["people_without_seat"] == [ZED]
    assert status["can_setup"] is False
    scorecard.setup_metrics(db, actor="lead@acme.example", emails=[AMY, ZED])
    assert scorecard.status(db, [AMY, ZED])["can_setup"] is False


def test_an_unchecked_ae_keeps_last_weeks_cell_rather_than_a_zero(db):
    amy = _metric(db, _person(db, AMY, "Amy"))
    zed = _metric(db, _person(db, ZED, "Zed"))
    report = scorecard.write_weekly(db, now=NOW, emails=[AMY, ZED], unchecked={ZED})
    assert report["inserted"] == 1 and report["skipped_not_checked"] == 1
    assert len(_entries(db, amy)) == 1
    assert _entries(db, zed) == []  # no "0" nobody checked

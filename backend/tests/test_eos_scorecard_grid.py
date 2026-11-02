"""Tests for rolling scorecard with history + averages.

Covers:
- `GET /scorecard/entries?weeks=N` filters out entries older than N
  weeks and orders the rest newest-first.
- `GET /scorecard/grid` returns the metric × week grid plus computed
  4-week and 13-week averages, ignoring non-numeric values like "TBD".
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from rubik_core.auth.registry import UserRegistry
from rubik_core.config import settings
from rubik_core.database.schemas import UserProfile

client = TestClient(app)


def _profile(email: str, roles: list[str] | None = None) -> UserProfile:
    now = "2026-05-21T00:00:00"
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
    def _factory():
        return _profile(email, roles)

    app.dependency_overrides[get_current_user] = _factory


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db_path = tmp_path / "test_ft6.db"
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    import rubik_core.database.db as dbmod

    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database import eos  # noqa: F401
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)

    reg = UserRegistry()
    if not reg.get_user("admin@example.com"):
        reg.register_user(
            email="admin@example.com",
            name="Admin",
            id="admin@example.com",
            roles=["admin"],
        )
    yield
    app.dependency_overrides.clear()


def _seed_metric_with_history(values_by_weeks_back: dict[int, str]) -> str:
    """Seed one Metric owned by a generated Person, plus weekly entries.

    ``values_by_weeks_back``: e.g. {0: "50", 1: "48", 14: "30"} drops
    entries 0/1/14 weeks before today. Returns the metric id."""
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import Person, ScorecardEntry, ScorecardMetric

    db = SessionLocal()
    try:
        owner = Person(
            first_name="Metric",
            last_name="Owner",
            email=f"metric-owner-{datetime.utcnow().strftime('%H%M%S%f')}@example.com",
            created_by="admin@example.com",
        )
        db.add(owner)
        db.commit()
        db.refresh(owner)

        metric = ScorecardMetric(
            owner_id=owner.id,
            name="Sales calls",
            target_value="50",
            unit="calls",
            frequency="weekly",
            on_track=True,
            created_by="admin@example.com",
        )
        db.add(metric)
        db.commit()
        db.refresh(metric)

        now = datetime.utcnow()
        for weeks_back, value in values_by_weeks_back.items():
            db.add(
                ScorecardEntry(
                    metric_id=metric.id,
                    week_start_date=now - timedelta(days=7 * weeks_back),
                    value=value,
                    created_by="admin@example.com",
                )
            )
        db.commit()
        return str(metric.id)
    finally:
        db.close()


# -- weeks= filter --------------------------------------------------


def test_entries_weeks_filter_drops_older_entries():
    metric_id = _seed_metric_with_history({0: "50", 1: "48", 14: "30", 30: "10"})
    _as_user("admin@example.com", roles=["admin"])

    # weeks=4 should keep entries 0 and 1 (within 28 days), drop 14 and 30.
    r = client.get(
        "/api/eos/scorecard/entries",
        params={"metric_id": metric_id, "weeks": 4},
    )
    assert r.status_code == 200
    values = [e["value"] for e in r.json()]
    assert set(values) == {"50", "48"}


def test_entries_default_order_is_newest_first():
    metric_id = _seed_metric_with_history({0: "A", 2: "B", 4: "C"})
    _as_user("admin@example.com", roles=["admin"])

    r = client.get("/api/eos/scorecard/entries", params={"metric_id": metric_id})
    assert r.status_code == 200
    values = [e["value"] for e in r.json()]
    # Newest-first ordering: the entry from "0 weeks back" comes first.
    assert values == ["A", "B", "C"]


# -- grid endpoint --------------------------------------------------


def test_grid_returns_metric_by_week_with_averages():
    metric_id = _seed_metric_with_history(
        {0: "50", 1: "48", 2: "52", 3: "46"}  # within last 4 weeks
    )
    _as_user("admin@example.com", roles=["admin"])

    r = client.get("/api/eos/scorecard/grid", params={"weeks": 13})
    assert r.status_code == 200
    body = r.json()
    assert "weeks" in body and "metrics" in body
    metric = next(m for m in body["metrics"] if m["id"] == metric_id)
    assert metric["name"] == "Sales calls"
    # 4 weeks of numeric data → 4-week avg is mean of (50, 48, 52, 46) = 49.0.
    assert metric["average_4w"] == 49.0
    # No data older than 4 weeks → 13-week avg same as 4-week avg here.
    assert metric["average_13w"] == 49.0


def test_grid_ignores_non_numeric_values_in_averages():
    metric_id = _seed_metric_with_history({0: "50", 1: "TBD", 2: "60", 3: "n/a"})
    _as_user("admin@example.com", roles=["admin"])

    r = client.get("/api/eos/scorecard/grid", params={"weeks": 4})
    metric = next(m for m in r.json()["metrics"] if m["id"] == metric_id)
    # "TBD" and "n/a" are skipped — average is mean of 50 + 60 = 55.0.
    assert metric["average_4w"] == 55.0


def test_grid_returns_empty_metrics_list_when_none_exist():
    _as_user("admin@example.com", roles=["admin"])
    r = client.get("/api/eos/scorecard/grid")
    assert r.status_code == 200
    body = r.json()
    assert body["metrics"] == []
    # The axis is a CALENDAR, not a summary of the data: 13 trailing Sundays
    # whether or not anyone has filled a single cell. It used to be the union of
    # weeks that had entries, which made a team three weeks into using the
    # scorecard get a three-column grid — no visible gaps to fill in, and the
    # sparseness read as broken colouring.
    assert len(body["weeks"]) == 13
    weeks = [date.fromisoformat(w) for w in body["weeks"]]
    assert all(w.weekday() == 6 for w in weeks), "every column is a Sunday"
    assert weeks == sorted(weeks, reverse=True), "newest first"


def test_grid_axis_is_calendar_not_data_driven():
    """One week of data still yields a full-width grid."""
    _seed_metric_with_history({0: "50"})
    _as_user("admin@example.com", roles=["admin"])

    body = client.get("/api/eos/scorecard/grid", params={"weeks": 13}).json()
    assert len(body["weeks"]) == 13
    entries = body["metrics"][0]["entries"]
    # Every column is present; the twelve unfilled ones are explicitly empty
    # rather than absent.
    assert set(entries) == set(body["weeks"])
    assert sum(1 for v in entries.values() if v is not None) == 1


def test_grid_snaps_non_sunday_entries_into_their_week():
    """A Monday-dated entry lands in that week's Sunday column.

    Non-Sunday anchoring is the classic scorecard import bug: the page's axis is
    Sundays and matches exactly, so such a row rendered as "—" there forever.
    The grid snaps, so the two surfaces agree and the row is visible.
    """
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import Person, ScorecardEntry, ScorecardMetric
    from rubik_core.ingestion.eos.scorecard_xlsx import snap_to_sunday

    db = SessionLocal()
    try:
        owner = Person(
            first_name="Snap",
            last_name="Owner",
            email="snap-owner@example.com",
            created_by="admin@example.com",
        )
        db.add(owner)
        db.commit()
        db.refresh(owner)
        metric = ScorecardMetric(
            owner_id=owner.id,
            name="Monday metric",
            target_value="10",
            created_by="admin@example.com",
        )
        db.add(metric)
        db.commit()
        db.refresh(metric)
        # The Monday of the current scorecard week — one day after its Sunday.
        this_sunday = snap_to_sunday(datetime.utcnow().date())
        monday = datetime.combine(this_sunday + timedelta(days=1), datetime.min.time())
        db.add(
            ScorecardEntry(
                metric_id=metric.id,
                week_start_date=monday,
                value="42",
                created_by="admin@example.com",
            )
        )
        db.commit()
        metric_id = str(metric.id)
    finally:
        db.close()

    _as_user("admin@example.com", roles=["admin"])
    body = client.get("/api/eos/scorecard/grid", params={"weeks": 13}).json()
    row = next(m for m in body["metrics"] if m["id"] == metric_id)
    assert row["entries"][this_sunday.isoformat()] == "42"
    assert row["average_4w"] == 42.0


def test_grid_carries_the_saved_measurable_order():
    """The L10 grid reads ``/grid``; the page reads the metric list. Both feed
    the same component, which sorts by ``sort_order`` within an owner's block,
    so ``/grid`` must carry it or the meeting falls back to alphabetical while
    the page shows the team's order."""
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import Person, ScorecardMetric

    db = SessionLocal()
    try:
        owner = Person(
            first_name="Order",
            last_name="Owner",
            email="order-owner@example.com",
            created_by="admin@example.com",
        )
        db.add(owner)
        db.commit()
        db.refresh(owner)
        ids = {}
        for name in ("Alpha calls", "Zeta demos"):
            metric = ScorecardMetric(
                owner_id=owner.id, name=name, created_by="admin@example.com"
            )
            db.add(metric)
            db.commit()
            db.refresh(metric)
            ids[name] = str(metric.id)
    finally:
        db.close()

    _as_user("admin@example.com", roles=["admin"])
    before = client.get("/api/eos/scorecard/grid").json()
    assert {m["sort_order"] for m in before["metrics"]} == {None}

    r = client.post(
        "/api/eos/scorecard/metrics/reorder",
        json={"ordered_ids": [ids["Zeta demos"], ids["Alpha calls"]]},
    )
    assert r.status_code == 200, r.text

    after = client.get("/api/eos/scorecard/grid").json()
    order = {m["id"]: m["sort_order"] for m in after["metrics"]}
    assert order[ids["Zeta demos"]] == 0
    assert order[ids["Alpha calls"]] == 1


# -- Cell notes ------------------------------------------------------------
#
# The exec team kept a spreadsheet comment on the number ("$142k saved" →
# "3 renewals, 2 upsells"). Moving to Cubie kept the number and lost the reason
# it was that number. One editable note per metric-week, entered on the page
# before the meeting and read in the L10.


def _seed_bare_metric(name: str = "Cost savings") -> str:
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import Person, ScorecardMetric

    db = SessionLocal()
    try:
        owner = Person(
            first_name="Note",
            last_name="Owner",
            email=f"note-owner-{datetime.utcnow().strftime('%H%M%S%f')}@example.com",
            created_by="admin@example.com",
        )
        db.add(owner)
        db.commit()
        db.refresh(owner)
        metric = ScorecardMetric(
            owner_id=owner.id,
            name=name,
            target_value="100",
            comparator=">=",
            created_by="admin@example.com",
        )
        db.add(metric)
        db.commit()
        db.refresh(metric)
        return str(metric.id)
    finally:
        db.close()


def _this_sunday() -> date:
    from rubik_core.ingestion.eos.scorecard_xlsx import snap_to_sunday

    return snap_to_sunday(datetime.utcnow().date())


def test_note_round_trips_through_create_and_grid():
    metric_id = _seed_bare_metric()
    _as_user("admin@example.com", roles=["admin"])
    sunday = _this_sunday()

    r = client.post(
        "/api/eos/scorecard/entries",
        json={
            "metric_id": metric_id,
            "week_start_date": f"{sunday.isoformat()}T00:00:00",
            "value": "142000",
            "note": "3 renewals, 2 upsells",
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["note"] == "3 renewals, 2 upsells"

    body = client.get("/api/eos/scorecard/grid", params={"weeks": 13}).json()
    row = next(m for m in body["metrics"] if m["id"] == metric_id)
    assert row["notes"][sunday.isoformat()] == "3 renewals, 2 upsells"
    # Sparse: only annotated cells appear, so the payload doesn't carry 13 nulls
    # per metric for a feature almost nobody uses on a given week.
    assert len(row["notes"]) == 1


def test_note_is_editable_and_clearable_via_patch():
    metric_id = _seed_bare_metric()
    _as_user("admin@example.com", roles=["admin"])
    sunday = _this_sunday()

    created = client.post(
        "/api/eos/scorecard/entries",
        json={
            "metric_id": metric_id,
            "week_start_date": f"{sunday.isoformat()}T00:00:00",
            "value": "10",
            "note": "first pass",
        },
    ).json()

    edited = client.patch(
        f"/api/eos/scorecard/entries/{created['id']}", json={"note": "revised"}
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["note"] == "revised"
    assert edited.json()["value"] == "10", "editing the note must not touch the value"

    cleared = client.patch(
        f"/api/eos/scorecard/entries/{created['id']}", json={"note": None}
    )
    assert cleared.json()["note"] is None


def test_upsert_without_a_note_field_preserves_an_existing_note():
    """The in-meeting panel saves value-only; that must not erase the note.

    The L10 grid POSTs {metric_id, week_start_date, value} when someone types
    this week's number. An unconditional assignment would silently wipe a note
    written on the page before the meeting — the exact data the feature exists
    to keep.
    """
    metric_id = _seed_bare_metric()
    _as_user("admin@example.com", roles=["admin"])
    sunday = _this_sunday()
    week = f"{sunday.isoformat()}T00:00:00"

    client.post(
        "/api/eos/scorecard/entries",
        json={
            "metric_id": metric_id,
            "week_start_date": week,
            "value": "10",
            "note": "3 renewals",
        },
    )

    upserted = client.post(
        "/api/eos/scorecard/entries/upsert",
        json={"metric_id": metric_id, "week_start_date": week, "value": "12"},
    )
    assert upserted.status_code == 200, upserted.text
    assert upserted.json()["value"] == "12"
    assert upserted.json()["note"] == "3 renewals", "value-only save wiped the note"

    # But an explicit note IS honoured — including clearing it.
    explicit = client.post(
        "/api/eos/scorecard/entries/upsert",
        json={
            "metric_id": metric_id,
            "week_start_date": week,
            "value": "12",
            "note": None,
        },
    )
    assert explicit.json()["note"] is None

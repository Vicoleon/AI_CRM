"""Tests for the HubSpot → scorecard "green" metric sync.

Uses a throwaway sqlite DB (tmp_path) so it can never touch Cloud SQL, and an
injected fake HubSpot client so no token/network is needed. Covers: metric
recipe matching + held metrics, per-kind computation, company vs rep scoping,
owner-email resolution + skip, dry-run writes nothing, commit is idempotent,
and the configuration: with no team or measurables set it does nothing and
says why, the team is matched by slug, an unknown recipe is reported.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from rubik_core.config import settings
from rubik_core.scorecard_sync import hubspot_green
from rubik_core.scorecard_sync.hubspot_green import (
    NOT_CONFIGURED,
    _fmt_number,
    _target_anchors,
    sync_sales_green_metrics,
    week_anchor_for,
    week_window,
)
from rubik_core.tenancy.hubspot_config import HubSpotConnectorConfig

PARKER_EMAIL = "pjensen@acme.example"


class FakeHubSpotClient:
    """Deterministic stand-in for HubSpotAPIClient.

    Returns fixed counts/deals keyed off the won/lost/created filter shape so the
    engine's per-kind wiring is exercised without a live account.
    """

    WON_COUNT = 3
    LOST_COUNT = 1
    CREATED_COUNT = 5
    WON_DEALS = [
        {"properties": {"amount": "100", "pipeline": "default"}},
        {"properties": {"amount": "200", "pipeline": "default"}},
        {"properties": {"amount": "44167.5", "pipeline": "default"}},
    ]  # sum = 44467.5

    def __init__(self, owner_email: str | None = PARKER_EMAIL):
        self._owner_email = owner_email

    def owner_directory(self):
        if not self._owner_email:
            return {}
        return {"111": {"email": self._owner_email.lower(), "name": "Parker Jensen"}}

    @staticmethod
    def _is_won(filters):
        return any(
            f.get("propertyName") == "hs_is_closed_won" and f.get("value") == "true"
            for f in filters
        )

    @staticmethod
    def _is_lost(filters):
        return any(
            f.get("propertyName") == "hs_is_closed_won" and f.get("value") == "false"
            for f in filters
        )

    def count(self, object_type, filters):
        assert object_type == "deals"
        if self._is_won(filters):
            return self.WON_COUNT
        if self._is_lost(filters):
            return self.LOST_COUNT
        return self.CREATED_COUNT  # created_count path

    def search_all(self, object_type, filters, properties):
        assert object_type == "deals"
        assert "amount" in properties
        return list(self.WON_DEALS) if self._is_won(filters) else []


#: What a company with this scorecard would configure.
SALES_RECIPES = {
    "Revenue": "company_won_amount",
    "Count of Deals": "company_won_count",
    "Closed Deals ($)": "rep_won_amount",
    "Closed Deals (#)": "rep_won_count",
    "Lost Deals": "rep_lost_count",
}


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    """The scorecard team and measurables an admin set up for the sync."""
    state = {
        "pipeline_ids": ("default",),
        "scorecard_sync_department": "Sales",
        "scorecard_sync_metrics": dict(SALES_RECIPES),
    }
    monkeypatch.setattr(
        hubspot_green,
        "get_hubspot_config",
        lambda: HubSpotConnectorConfig(**state),
    )
    return state


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Bind the DB to a throwaway sqlite file — and REFUSE to run otherwise.

    ``get_engine()`` uses Cloud SQL whenever ``CLOUDSQL_CONNECTION_NAME`` is set
    (it ignores ``DATABASE_URL`` in that branch), and this repo's ``.env`` sets
    it — so overriding ``DATABASE_URL`` alone silently pollutes dev Cloud SQL.
    We blank the connector setting AND hard-assert the resolved engine is sqlite
    before creating any table, so a future regression fails loudly instead of
    writing to production.
    """
    db_path = tmp_path / "test_scorecard_sync.db"
    monkeypatch.setattr(settings, "CLOUDSQL_CONNECTION_NAME", None, raising=False)
    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite:///{db_path}", raising=False)
    import rubik_core.database.db as dbmod

    dbmod._engine = None
    dbmod._connector = None
    engine = dbmod.get_engine()
    assert engine.dialect.name == "sqlite", (
        f"refusing to run against a non-sqlite engine: {engine.url!r}"
    )
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database import eos  # noqa: F401
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    yield
    # Drop the sqlite engine so it can't leak into a later test/module.
    dbmod._engine = None
    dbmod._connector = None


def _seed_sales_metrics():
    """Seed the 6 green metrics + one held (non-green) metric.

    Company metrics (Revenue, Count of Deals) owned by a company Person; the
    per-rep metrics owned by Parker (whose email the fake client knows).
    """
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import Person, ScorecardMetric
    from rubik_core.database.models import Department

    db = SessionLocal()
    try:
        if not db.query(Department).filter(Department.name == "Sales").first():
            db.add(Department(name="Sales"))
            db.commit()

        company = Person(
            first_name="Harper",
            last_name="Tran",
            email="htran@acme.example",
            created_by="test",
        )
        parker = Person(
            first_name="Parker",
            last_name="Jensen",
            email=PARKER_EMAIL,
            created_by="test",
        )
        db.add_all([company, parker])
        db.commit()
        db.refresh(company)
        db.refresh(parker)

        rows = [
            ("Revenue", "currency", company.id),
            ("Count of Deals", "number", company.id),
            ("Closed Deals ($)", "currency", parker.id),
            ("Closed Deals (#)", "number", parker.id),
            ("Lost Deals", "number", parker.id),
            ("New Leads in the Pipeline", "number", parker.id),
            ("SOW Sent", "number", parker.id),  # held (not green)
        ]
        for name, vtype, owner_id in rows:
            db.add(
                ScorecardMetric(
                    owner_id=owner_id,
                    department_name="Sales",
                    is_exec=False,
                    name=name,
                    value_type=vtype,
                    created_by="test",
                )
            )
        db.commit()
    finally:
        db.close()


def _run(**kwargs):
    from rubik_core.database.db import SessionLocal

    db = SessionLocal()
    try:
        return sync_sales_green_metrics(
            db,
            now=datetime(2026, 7, 15),  # Wednesday → week of Sun 2026-07-12
            client=FakeHubSpotClient(),
            **kwargs,
        )
    finally:
        db.close()


def _entry_count():
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import ScorecardEntry

    db = SessionLocal()
    try:
        return db.query(ScorecardEntry).count()
    finally:
        db.close()


# ── pure helpers ──────────────────────────────────────────────────────────────


def test_fmt_number_trims_whole_and_trailing_zeros():
    assert _fmt_number(3.0) == "3"
    assert _fmt_number(44467.5) == "44467.5"
    assert _fmt_number(3510.0) == "3510"
    assert _fmt_number(0) == "0"


def test_week_anchor_and_window_match_seed_convention():
    # VERIFIED convention: the "Jul 6–12" (Mon–Sun) workbook column is stored at
    # anchor 2026-07-05, and its window is Mon 7/6 .. Sun 7/12.
    assert week_anchor_for(date(2026, 7, 8)) == date(2026, 7, 5)  # Wed 7/8 → anchor 7/5
    assert week_anchor_for(date(2026, 7, 12)) == date(2026, 7, 5)  # Sun 7/12 = last day
    assert week_anchor_for(date(2026, 7, 6)) == date(2026, 7, 5)  # Mon 7/6 = first day
    assert week_window(date(2026, 7, 5)) == ("2026-07-06", "2026-07-12")


def test_target_anchors():
    # now = Thu 2026-07-16 → current Mon–Sun week is 7/13..7/19, anchored 7/12.
    assert _target_anchors(date(2026, 7, 16), 2, None) == [
        date(2026, 7, 12),
        date(2026, 7, 5),
    ]
    assert _target_anchors(date(2026, 7, 16), 1, date(2026, 7, 8)) == [date(2026, 7, 5)]


# ── engine ────────────────────────────────────────────────────────────────────


def test_dry_run_computes_values_and_writes_nothing():
    _seed_sales_metrics()
    report = _run(weeks=1, commit=False)

    # New Leads and SOW Sent are not configured, so 5 green + 2 held.
    assert report.green_metrics_seen == 5
    assert report.held_metric_names == ["New Leads in the Pipeline", "SOW Sent"]
    assert _entry_count() == 0  # dry run wrote nothing

    by_metric = {r.metric_name: r for r in report.rows}
    assert by_metric["Revenue"].computed_value == "44467.5"
    assert by_metric["Count of Deals"].computed_value == "3"
    assert by_metric["Closed Deals ($)"].computed_value == "44467.5"
    assert by_metric["Closed Deals (#)"].computed_value == "3"
    assert by_metric["Lost Deals"].computed_value == "1"
    assert "New Leads in the Pipeline" not in by_metric  # held → no row
    assert all(r.action == "inserted" for r in report.rows)


@pytest.mark.parametrize("returned_pipeline", ["client-only", None])
def test_won_amount_refuses_unscoped_response_without_writing_zero(
    configured, returned_pipeline
):
    configured["scorecard_sync_metrics"] = {"Revenue": "company_won_amount"}
    _seed_sales_metrics()

    class MisfilteredHubSpot(FakeHubSpotClient):
        def search_all(self, object_type, filters, properties):
            assert object_type == "deals"
            assert {
                "propertyName": "pipeline",
                "operator": "IN",
                "values": ["default"],
            } in filters
            return [{"properties": {"amount": "9000", "pipeline": returned_pipeline}}]

    from rubik_core.database.db import SessionLocal

    db = SessionLocal()
    try:
        report = sync_sales_green_metrics(
            db,
            now=datetime(2026, 7, 15),
            commit=True,
            client=MisfilteredHubSpot(),
        )
    finally:
        db.close()

    assert report.rows[0].metric_name == "Revenue"
    assert report.rows[0].action == "error"
    assert report.rows[0].computed_value is None
    assert report.written == 0
    assert _entry_count() == 0


def test_commit_writes_then_is_idempotent():
    _seed_sales_metrics()

    first = _run(weeks=1, commit=True)
    assert first.written == 5
    assert _entry_count() == 5

    second = _run(weeks=1, commit=True)
    assert second.written == 0
    assert all(r.action == "unchanged" for r in second.rows)
    assert _entry_count() == 5  # no duplicate rows


def test_rep_metric_skipped_when_owner_not_a_hubspot_owner():
    _seed_sales_metrics()
    from rubik_core.database.db import SessionLocal

    db = SessionLocal()
    try:
        # Fake client whose directory has NO matching email → rep metrics skip,
        # company metrics still compute.
        report = sync_sales_green_metrics(
            db,
            now=datetime(2026, 7, 15),
            weeks=1,
            commit=False,
            client=FakeHubSpotClient(owner_email="someone-else@acme.example"),
        )
    finally:
        db.close()

    by_metric = {r.metric_name: r for r in report.rows}
    assert by_metric["Revenue"].action == "inserted"  # company scope unaffected
    assert by_metric["Closed Deals (#)"].action == "skipped"
    assert "no HubSpot owner" in by_metric["Closed Deals (#)"].detail


# ── configuration ────────────────────────────────────────────────────────────


class _NoCallsClient:
    """A HubSpot client that fails the test if the sync touches it."""

    def __getattr__(self, name):
        raise AssertionError(f"the sync called HubSpot ({name}) with nothing set up")


@pytest.mark.parametrize(
    "department, metrics",
    [("", SALES_RECIPES), ("Sales", {}), ("", {})],
    ids=["no-team", "no-measurables", "nothing"],
)
def test_with_nothing_configured_it_does_nothing_and_says_why(
    configured, department, metrics
):
    _seed_sales_metrics()
    configured["scorecard_sync_department"] = department
    configured["scorecard_sync_metrics"] = dict(metrics)
    from rubik_core.database.db import SessionLocal

    db = SessionLocal()
    try:
        report = sync_sales_green_metrics(db, commit=True, client=_NoCallsClient())
    finally:
        db.close()

    assert report.note == NOT_CONFIGURED
    assert report.rows == [] and report.written == 0
    assert _entry_count() == 0
    assert "seed_sales_scorecard" not in report.summary()


def test_the_team_is_matched_by_slug(configured):
    _seed_sales_metrics()
    configured["scorecard_sync_department"] = " sales. "

    report = _run(weeks=1, commit=False)

    assert report.green_metrics_seen == 5
    assert report.note == ""


def test_another_team_s_measurables_are_left_alone(configured):
    _seed_sales_metrics()
    configured["scorecard_sync_department"] = "Marketing"

    report = _run(weeks=1, commit=True)

    assert report.green_metrics_seen == 0
    assert _entry_count() == 0
    assert "Marketing" in report.errors[-1]
    assert "seed_sales_scorecard" not in " ".join(report.errors)


def test_an_unknown_recipe_is_reported_and_its_measurable_held(monkeypatch):
    _seed_sales_metrics()
    monkeypatch.setattr(
        hubspot_green,
        "get_hubspot_config",
        lambda: type(
            "BadConfig",
            (),
            {
                "pipeline_ids": ("default",),
                "scorecard_sync_department": "Sales",
                "scorecard_sync_metrics": {
                    **SALES_RECIPES,
                    "SOW Sent": "sow_sent_count",
                },
            },
        )(),
    )

    report = _run(weeks=1, commit=False)

    assert report.green_metrics_seen == 5
    assert "SOW Sent" in report.held_metric_names
    assert any("sow_sent_count" in e for e in report.errors)

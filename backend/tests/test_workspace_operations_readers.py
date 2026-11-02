"""Operational readers use the selected tenant's policy and user rows."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.agents import ingestion_council
from rubik_core.company import operations
from rubik_core.database.models import User, WorkspaceSetting
from rubik_core.insights.population import excluded_emails
from rubik_core.loop_closure.constants import thresholds
from rubik_core.tenancy.context import current_workspace, use_workspace


@pytest.fixture
def stores(tmp_path, monkeypatch):
    engines = {}
    makers = {}
    for slug in ("acme", "bravo"):
        engine = create_engine(
            f"sqlite:///{tmp_path / (slug + '.db')}",
            connect_args={"check_same_thread": False},
        )
        WorkspaceSetting.__table__.create(engine)
        User.__table__.create(engine)
        engines[slug] = engine
        makers[slug] = sessionmaker(bind=engine)
    monkeypatch.setattr(
        operations, "SessionLocal", lambda: makers[current_workspace()]()
    )
    yield makers
    for engine in engines.values():
        engine.dispose()


def test_two_workspaces_have_distinct_audiences_exclusions_and_metric(stores):
    for slug in ("acme", "bravo"):
        with stores[slug]() as db:
            db.add(
                User(
                    email=f"ops@{slug}.example",
                    id=f"{slug}-ops",
                    name="Ops",
                    department="Operations",
                )
            )
            db.commit()
        with use_workspace(slug):
            operations.patch_operations_config(
                {
                    "ingestion_council_recipients": [f"ops@{slug}.example"],
                    "insights_excluded_emails": [f"shared@{slug}.example"],
                    "loop_closure_scorecard_metric": f"{slug.title()} loose ends",
                }
            )

    with use_workspace("acme"), stores["acme"]() as db:
        assert ingestion_council._recipient_emails(db, None) == ["ops@acme.example"]
        assert "shared@acme.example" in excluded_emails()
        assert "shared@bravo.example" not in excluded_emails()
        assert thresholds()["scorecard_metric"] == "Acme loose ends"
    with use_workspace("bravo"), stores["bravo"]() as db:
        assert ingestion_council._recipient_emails(db, None) == ["ops@bravo.example"]
        assert "shared@bravo.example" in excluded_emails()
        assert "shared@acme.example" not in excluded_emails()
        assert thresholds()["scorecard_metric"] == "Bravo loose ends"


def test_malformed_policy_refuses_report_delivery_and_insights(stores, monkeypatch):
    with stores["acme"]() as db:
        db.add(
            WorkspaceSetting(
                key="workspace_operations",
                value={"insights_excluded_emails": "malformed"},
            )
        )
        db.commit()

    created = []
    monkeypatch.setattr(
        "backend.services.slack_service.SlackBotClient",
        lambda: created.append(True),
    )
    with use_workspace("acme"), stores["acme"]() as db:
        with pytest.raises(operations.OperationsConfigUnavailable):
            ingestion_council._recipient_emails(db, ["ops@acme.example"])
        with pytest.raises(operations.OperationsConfigUnavailable):
            excluded_emails()
        with pytest.raises(operations.OperationsConfigUnavailable):
            thresholds()
        report = ingestion_council.deliver_to_slack(
            db,
            SimpleNamespace(
                scope="nightly", overall_health=SimpleNamespace(value="green")
            ),
            None,
            ["ops@acme.example"],
        )
        assert report.recipients == 0 and report.delivered == 0
        assert report.errors == ["Workspace operations policy unavailable"]
    assert created == []

"""Unit tests for the UC-10 overnight orchestrator, persistence, and read path.

What's covered — the parts that are still live:
  1. `OvernightAgent` — any object satisfying `DepartmentAgentPlugin` composes,
     and one plugin crashing must not break the whole run.
  2. `persist_recommendations` — the 30-day duplicate guard every enrolled
     plugin (today: `MultiSourcePrioritiesPlugin`) relies on.
  3. `fetch_top_recommendations` — the reader behind `/priorities/more` and
     the recommendations endpoints; dismissed rows must stay hidden.

The legacy `SalesHubspotPlugin` (mechanical stale-lead / stage-stuck rules)
was unenrolled when UC-17 took over as the Sales producer and has been
deleted, along with its scenario tests: they exercised dead generator code
and only passed when `GCP_PROJECT_ID` was configured (the fetch probes
short-circuit on an empty project, so the mocked BigQuery rows never flowed
on a clean checkout). The probes themselves live on in
`backend/agents/sales_hubspot.py` for the HubSpot SignalSource and are
covered by `test_signals_hubspot.py`.

Tests use an in-memory SQLite DB to avoid touching deployed Cloud SQL
(per the project's known test_pollution_bug — module-level SessionLocal
would otherwise read .env's DATABASE_URL).
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.agents.base import Recommendation
from rubik_core.database.models import AgentRecommendation, Base

# ─── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def db_factory():
    """In-memory SQLite session factory with the schema baked in."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _stale_lead_rec() -> Recommendation:
    """A representative rec, shaped like the rows enrolled plugins emit."""
    return Recommendation(
        user_email="rep1@acme.example",
        type="stale_lead",
        entity_id="deal-100",
        entity_name="ABC Corp",
        text="Haven't contacted ABC Corp in 23 days.",
        evidence={"days_since_last_activity": 23},
        confidence=0.55,
        source={"check": "stale_lead", "deal_id": "deal-100"},
    )


class StaticPlugin:
    """Fake department plugin that emits a fixed list every run."""

    department = "sales"
    name = "static_FAKE"

    def __init__(self, recs: list[Recommendation]):
        self._recs = recs

    def run(self, db):
        return list(self._recs)


# ─── Orchestrator: plugins compose via the protocol ───────────────────────────


def test_orchestrator_runs_arbitrary_plugin_implementing_protocol(db_factory):
    """Confirms the contract is loose enough for a non-Sales plugin to work.
    A toy HR-style plugin emits one PTO recommendation; the orchestrator
    persists it with the same row shape, plugin name in source_metadata,
    and surfaces it through the existing top-N reader unchanged."""
    from backend.agents.overnight import OvernightAgent

    class FakeHrSystemPlugin:
        department = "hr"
        name = "hr_system"

        def run(self, db):
            return [
                Recommendation(
                    user_email="rep1@acme.example",
                    type="pto_pending_approval",
                    entity_id="emp-42",
                    entity_name="Jane Doe",
                    text="Jane Doe has 2 PTO requests pending your approval since 2026-04-28.",
                    evidence={"days_pending": 3, "request_count": 2},
                    confidence=0.9,
                    source={"check": "pto_pending"},
                )
            ]

    result = OvernightAgent(
        plugins=[FakeHrSystemPlugin()],
        db_factory=db_factory,
    ).run()

    assert result.errors == []
    assert result.written == 1
    assert result.by_type == {"pto_pending_approval": 1}

    db = db_factory()
    try:
        rec = db.query(AgentRecommendation).first()
        assert rec.type == "pto_pending_approval"
        assert rec.lead_id == "emp-42"
        assert rec.lead_name == "Jane Doe"
        assert rec.source_metadata["plugin"] == "hr_system"
    finally:
        db.close()


# ─── Orchestrator: per-plugin error isolation ─────────────────────────────────


def test_one_plugin_crashing_doesnt_kill_other_plugins(db_factory):
    """Multi-department invariant: one plugin crashing must not stop another
    plugin's recs from being written."""
    from backend.agents.overnight import OvernightAgent

    class CrashingPlugin:
        department = "hr"
        name = "hr_system_FAKE"

        def run(self, db):
            raise RuntimeError("simulated source-system outage")

    result = OvernightAgent(
        plugins=[CrashingPlugin(), StaticPlugin([_stale_lead_rec()])],
        db_factory=db_factory,
    ).run()

    # The healthy plugin's rec was written despite the HR plugin crashing.
    assert result.written == 1
    assert result.by_type.get("stale_lead") == 1

    plugin_results = {p.plugin_name: p for p in result.plugin_results}
    assert plugin_results["hr_system_FAKE"].error is not None
    assert "simulated source-system outage" in plugin_results["hr_system_FAKE"].error
    assert plugin_results["static_FAKE"].error is None
    assert plugin_results["static_FAKE"].written == 1


# ─── Persistence: duplicate guard ─────────────────────────────────────────────


def test_duplicate_guard_skips_re_emission_within_30_days(db_factory):
    """`persist_recommendations` suppresses a (user, type, entity) re-emission
    inside the 30-day horizon, so a nightly re-run doesn't spam the homepage
    with rows the user already saw."""
    from backend.agents.overnight import OvernightAgent

    plugin = StaticPlugin([_stale_lead_rec()])

    first = OvernightAgent(plugins=[plugin], db_factory=db_factory).run()
    second = OvernightAgent(plugins=[plugin], db_factory=db_factory).run()

    assert first.written == 1
    assert second.written == 0
    assert second.skipped_duplicates >= 1

    db = db_factory()
    try:
        assert db.query(AgentRecommendation).count() == 1
    finally:
        db.close()


# ─── Read path: dismiss filter ────────────────────────────────────────────────


def test_dismissed_recommendation_is_filtered_from_top_n(db_factory, monkeypatch):
    from backend.routers.agents import fetch_top_recommendations

    monkeypatch.setattr(
        "backend.routers.agents.visible_hubspot_deal_ids", lambda ids: ids
    )

    db = db_factory()
    try:
        active = AgentRecommendation(
            id="r-active",
            user_email="rep1@acme.example",
            type="stale_lead",
            lead_id="d1",
            lead_name="A",
            recommendation_text="active",
            confidence=0.7,
            created_at=datetime.utcnow(),
        )
        dismissed = AgentRecommendation(
            id="r-dismissed",
            user_email="rep1@acme.example",
            type="stale_lead",
            lead_id="d2",
            lead_name="B",
            recommendation_text="dismissed",
            confidence=0.9,
            created_at=datetime.utcnow(),
            dismissed_at=datetime.utcnow(),
        )
        db.add_all([active, dismissed])
        db.commit()

        top = fetch_top_recommendations(db, "rep1@acme.example", limit=5)
        ids = [r.id for r in top]
        assert "r-active" in ids
        assert "r-dismissed" not in ids
    finally:
        db.close()

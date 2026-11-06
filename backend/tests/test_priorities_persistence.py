"""Tests for the persistence + read-from-DB-first endpoint.

UC-41: a cache hit still returns 200 with the bundle, but a cache miss /
force_refresh now returns **202 + run_id** and runs the multi-agent pipeline in
a background task (so the homefeed never eats the 90s axios timeout). The
priority-action lookup scans every card for today, not just the newest.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from rubik_core.database.models import HomeCard

# ── Fixtures / overrides ─────────────────────────────────────────────────────

mock_user = MagicMock()
mock_user.email = "test@example.com"
mock_user.roles = ["user"]

client = TestClient(app)


@pytest.fixture(autouse=True)
def _override_auth():
    """Apply the auth override per-test, not at import time.

    Other test modules (e.g. ``test_granular_access.py``) clear
    ``app.dependency_overrides`` after each of their tests, which would wipe a
    module-level override set during collection — re-applying it here keeps
    this file independent of which other files share the pytest run."""
    app.dependency_overrides[get_current_user] = lambda: mock_user
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture(autouse=True)
def _run_state_db(tmp_path, monkeypatch):
    """UC-41 run state now lives in the priorities_runs TABLE (#9).

    Point the global engine at a throwaway SQLite so run_state works for real,
    while the HomeCard reads in these tests stay mocked via
    ``backend.routers.home.SessionLocal``. APP_MODE is pinned LOCAL so the
    dispatch takes the inline BackgroundTask fallback (no Cloud Run calls)."""
    from rubik_core.config import AppMode
    from rubik_core.config import settings as brain_settings

    monkeypatch.setattr(brain_settings, "APP_MODE", AppMode.LOCAL)
    monkeypatch.setattr(
        brain_settings, "DATABASE_URL", f"sqlite:///{tmp_path / 'runs.db'}"
    )
    import rubik_core.database.db as dbmod

    monkeypatch.setattr(dbmod, "_engine", None)
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    yield


# ── DB-cached path ───────────────────────────────────────────────────────────


@patch("backend.routers.home.SessionLocal")
def test_priorities_returns_cached_bundle_when_card_exists(mock_session_factory):
    """If a HomeCard with metadata_json exists for today, return that — no LLM run."""
    metadata = {
        "schema_version": 2,
        "crm_provenance": {"workspace": "test", "deal_ids": [], "unprovable": False},
        "generated_at": "2026-05-26T12:00:00+00:00",
        "headline": "Top priority: Reply to Acme",
        "revision_count": 0,
        "last_critique_score": 8.0,
        "audit_trail": [],
        "priorities": [
            {
                "id": "p1",
                "title": "Reply to Acme",
                "why_it_matters": "CEO asked.",
                "urgency": "high",
                "source_channels": ["gmail"],
                "source_links": ["https://mail.example/1"],
                "suggested_action": "Reply with Q3 figure",
                "signal_ids": ["t-1"],
            }
        ],
    }
    mock_card = HomeCard(
        id="card-1",
        user_email="test@example.com",
        date="2026-05-26",
        category="daily_briefing",
        content_md="# stored",
        metadata_json=metadata,
        priority=100,
    )

    mock_db = MagicMock()
    mock_session_factory.return_value.__enter__.return_value = mock_db
    chain = mock_db.query.return_value
    chain = chain.filter.return_value
    two_filters = chain.filter.return_value
    chain = two_filters.filter.return_value
    chain = chain.order_by.return_value
    chain.first.return_value = mock_card
    # The next-week merge runs a 2-filter query; return no next-week card.
    two_filters.order_by.return_value.first.return_value = None

    # If we ever invoke the orchestrator while a cached card exists we want to know.
    with patch("rubik_core.priorities.PrioritiesOrchestrator") as mock_orch:
        response = client.post("/api/homefeed/priorities", json={})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["headline"] == "Top priority: Reply to Acme"
        assert len(body["priorities"]) == 1
        assert body["priorities"][0]["urgency"] == "high"
        mock_orch.assert_not_called()


# ── UC-41: force-refresh enqueues an async run ────────────────────────────────


@patch("rubik_core.priorities.persist_bundle")
@patch("rubik_core.priorities.PrioritiesOrchestrator")
@patch("backend.routers.home.SessionLocal")
def test_force_refresh_enqueues_async_run_and_persists(
    mock_session_factory, mock_orch_cls, mock_persist
):
    """UC-41: force_refresh=True returns 202 + run_id and runs the pipeline in a
    background task (persisting the result), never blocking the request."""
    mock_db = MagicMock()
    mock_session_factory.return_value.__enter__.return_value = mock_db
    chain = mock_db.query.return_value
    chain = chain.filter.return_value
    chain = chain.filter.return_value
    chain = chain.filter.return_value
    chain = chain.order_by.return_value
    chain.first.return_value = None

    from rubik_core.priorities import ChannelKind
    from rubik_core.priorities.models import PrioritiesBundle, PriorityCandidate

    fake_bundle = PrioritiesBundle(
        user_email="test@example.com",
        priorities=[
            PriorityCandidate(
                id="p-new",
                title="Fresh priority",
                why_it_matters="Just ran the pipeline.",
                urgency="medium",
                source_channels=[ChannelKind.CALENDAR],
                source_links=[],
                suggested_action="Block 30 min",
                signal_ids=["evt-1"],
            )
        ],
        headline="Fresh headline",
        revision_count=0,
        last_critique_score=9.0,
    )
    mock_orch_cls.return_value.generate.return_value = fake_bundle
    mock_persist.return_value = "new-card-id"

    response = client.post("/api/homefeed/priorities", json={"force_refresh": True})
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert body["run_id"]

    # The pipeline ran in the background task (TestClient drives it to completion)
    # and persisted the fresh card for the normal read path to pick up.
    mock_orch_cls.return_value.generate.assert_called_once()
    mock_persist.assert_called_once()
    persisted_email, _persisted_date, persisted_bundle = mock_persist.call_args.args
    assert persisted_email == "test@example.com"
    assert persisted_bundle.headline == "Fresh headline"

    # #9: the run's outcome is DB-backed — any instance's poll would see it.
    from rubik_core.priorities import run_state

    row = run_state.get_run(body["run_id"])
    assert row is not None
    assert row["status"] == run_state.RUN_READY


# ── UC-41: first-of-day cache miss enqueues an async run ──────────────────────


@patch("rubik_core.priorities.persist_bundle")
@patch("rubik_core.priorities.PrioritiesOrchestrator")
@patch("backend.routers.home.SessionLocal")
def test_no_card_enqueues_async_run(mock_session_factory, mock_orch_cls, mock_persist):
    """No cached card → 202 + run_id; the pipeline runs and persists in the bg."""
    mock_db = MagicMock()
    mock_session_factory.return_value.__enter__.return_value = mock_db
    chain = mock_db.query.return_value
    chain = chain.filter.return_value
    chain = chain.filter.return_value
    chain = chain.filter.return_value
    chain = chain.order_by.return_value
    chain.first.return_value = None  # no card today

    from rubik_core.priorities.models import PrioritiesBundle

    mock_orch_cls.return_value.generate.return_value = PrioritiesBundle(
        user_email="test@example.com",
        priorities=[],
        headline="quiet day",
    )

    response = client.post("/api/homefeed/priorities", json={})
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert body["run_id"]
    mock_orch_cls.return_value.generate.assert_called_once()
    mock_persist.assert_called_once()


# ── FT next-week: persist under its own category with the week window ─────────


@patch("rubik_core.priorities.persistence.SessionLocal")
def test_persist_bundle_next_week_category_and_metadata(mock_session_factory):
    """persist_bundle(category=NEXT_WEEK_CATEGORY, extra_metadata=...) writes the
    next-week category and merges the week window into metadata_json."""
    from rubik_core.priorities import NEXT_WEEK_CATEGORY, persist_bundle
    from rubik_core.priorities.models import PrioritiesBundle

    mock_db = MagicMock()
    mock_session_factory.return_value.__enter__.return_value = mock_db

    bundle = PrioritiesBundle(
        user_email="test@example.com",
        crm_provenance={"workspace": "test", "deal_ids": [], "unprovable": False},
        priorities=[],
        headline="Next week",
    )
    persist_bundle(
        "test@example.com",
        "2026-07-03",
        bundle,
        category=NEXT_WEEK_CATEGORY,
        extra_metadata={
            "report_kind": "next_week",
            "week_start": "2026-07-06",
            "week_end": "2026-07-12",
        },
    )

    card = mock_db.add.call_args.args[0]
    assert card.category == NEXT_WEEK_CATEGORY
    assert card.date == "2026-07-03"  # keyed on generation (Friday) date
    assert card.metadata_json["report_kind"] == "next_week"
    assert card.metadata_json["week_start"] == "2026-07-06"
    assert card.metadata_json["week_end"] == "2026-07-12"


@patch("rubik_core.priorities.persistence.SessionLocal")
def test_persist_bundle_defaults_to_daily_category(mock_session_factory):
    """The default call is unchanged: daily_briefing, no window metadata."""
    from rubik_core.priorities import DAILY_BRIEFING_CATEGORY, persist_bundle
    from rubik_core.priorities.models import PrioritiesBundle

    mock_db = MagicMock()
    mock_session_factory.return_value.__enter__.return_value = mock_db

    persist_bundle(
        "test@example.com",
        "2026-07-03",
        PrioritiesBundle(
            user_email="test@example.com",
            crm_provenance={"workspace": "test", "deal_ids": [], "unprovable": False},
            priorities=[],
        ),
    )
    card = mock_db.add.call_args.args[0]
    assert card.category == DAILY_BRIEFING_CATEGORY
    assert "week_start" not in (card.metadata_json or {})


# ── action lookup scans ALL of today's cards ───────────────────────────


@patch("backend.routers.home.SessionLocal")
def test_find_priority_scans_all_todays_cards(mock_session_factory):
    """An action's priority_id may live in an OLDER card (the user is
    looking at a card a Refresh/more-run superseded). _find_priority_in_today
    must scan every card for today, not just the newest, or the action 404s
    with "priority not found in today's bundle"."""
    from backend.routers.home import _find_priority_in_today

    newest = HomeCard(
        id="c2",
        user_email="test@example.com",
        date="2026-05-26",
        category="daily_briefing",
        content_md="",
        metadata_json={
            "schema_version": 2,
            "crm_provenance": {
                "workspace": "test",
                "deal_ids": [],
                "unprovable": False,
            },
            "priorities": [
                {"id": "new-1", "entity_kind": "thread", "source_channels": ["gmail"]}
            ],
        },
        priority=100,
    )
    older = HomeCard(
        id="c1",
        user_email="test@example.com",
        date="2026-05-26",
        category="daily_briefing",
        content_md="",
        metadata_json={
            "schema_version": 2,
            "crm_provenance": {
                "workspace": "test",
                "deal_ids": [],
                "unprovable": False,
            },
            "priorities": [
                {
                    "id": "old-1",
                    "title": "X",
                    "entity_kind": "thread",
                    "source_channels": ["gmail"],
                }
            ],
        },
        priority=100,
    )
    mock_db = MagicMock()
    mock_session_factory.return_value.__enter__.return_value = mock_db
    chain = mock_db.query.return_value
    chain = chain.filter.return_value
    chain = chain.filter.return_value
    chain = chain.filter.return_value
    chain = chain.order_by.return_value
    chain.all.return_value = [newest, older]  # newest-first

    # An id present only in the OLDER card is still found...
    found = _find_priority_in_today("test@example.com", "old-1")
    assert found is not None and found["id"] == "old-1"
    # ...and a genuinely-absent id returns None.
    assert _find_priority_in_today("test@example.com", "nope") is None


@patch("backend.routers.home.SessionLocal")
def test_find_priority_scans_next_week_cards(mock_session_factory):
    """An action fired on a next-week (look-ahead) item must resolve even when
    today's daily cards don't contain it — e.g. a Saturday dismissal of a
    Friday-dated next-week card. The next-week scan carries NO date filter."""
    from backend.routers.home import _find_priority_in_today

    nw_card = HomeCard(
        id="nw1",
        user_email="test@example.com",
        date="2026-07-03",
        category="next_week_report",
        content_md="",
        metadata_json={
            "schema_version": 2,
            "crm_provenance": {
                "workspace": "test",
                "deal_ids": [],
                "unprovable": False,
            },
            "priorities": [
                {
                    "id": "nw-item",
                    "title": "Prep SME",
                    "entity_kind": "event",
                    "source_channels": ["calendar"],
                }
            ],
        },
        priority=100,
    )
    mock_db = MagicMock()
    mock_session_factory.return_value.__enter__.return_value = mock_db
    q = mock_db.query.return_value
    # Daily scan = 3 chained filters then order_by; each .filter() returns a
    # DISTINCT mock, so walk the chain explicitly.
    f2 = q.filter.return_value.filter.return_value
    f2.filter.return_value.order_by.return_value.all.return_value = []  # daily: none
    # Next-week scan = 2 filters then order_by then limit.
    f2.order_by.return_value.limit.return_value.all.return_value = [nw_card]

    found = _find_priority_in_today("test@example.com", "nw-item")
    assert found is not None and found["id"] == "nw-item"

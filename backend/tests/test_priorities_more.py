"""Tests for the "done for the day" signal and "Get me other important things".

* ``generated_count`` lets the UI tell "you cleared your list" (kudos) from
  "nothing surfaced today". It must reflect what the card was generated with,
  i.e. survive the UC-25 read-path filter that drops handled items.
* ``/priorities/more`` first surfaces active overnight ``agent_recommendations``
  (mapped into the priorities card shape, tagged ``origin="recommendation"``),
  then falls back to a background pipeline run guarded against double-kicks.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import backend.routers.home as home
from backend.routers.home import (
    MoreResponse,
    _apply_priority_memory,
    _bundle_dict_from_metadata,
    _confidence_to_urgency,
    _fetch_other_recommendations,
    _recommendation_to_item,
    get_more_priorities,
)
from rubik_core.priorities import memory as priority_memory
from rubik_core.priorities import run_state
from rubik_core.priorities.memory import MemoryRow


@pytest.fixture(autouse=True)
def _run_state_db(tmp_path, monkeypatch):
    """The double-kick guard now lives in the priorities_runs TABLE (#9) —
    point the global engine at a throwaway SQLite so it works for real.
    APP_MODE is pinned LOCAL so dispatch takes the inline fallback."""
    from rubik_core.config import AppMode
    from rubik_core.config import settings as brain_settings

    monkeypatch.setattr(brain_settings, "APP_MODE", AppMode.LOCAL)
    monkeypatch.setattr(
        brain_settings, "DATABASE_URL", f"sqlite:///{tmp_path / 'more_runs.db'}"
    )
    import rubik_core.database.db as dbmod

    monkeypatch.setattr(dbmod, "_engine", None)
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    yield


CARD_TS = "2026-06-03T08:00:00+00:00"


def _meta_priority(pid="p1", *, ek="thread", eid="thr-1", pk="follow_up_debt"):
    return {
        "id": pid,
        "title": "Reply to Acme",
        "why_it_matters": "CEO asked.",
        "urgency": "high",
        "source_channels": ["gmail"],
        "source_links": [],
        "suggested_action": "Reply",
        "priority_kind": pk,
        "entity_kind": ek,
        "entity_id": eid,
        "entity_label": None,
    }


def _metadata(priorities, generated_at=CARD_TS):
    return {
        "schema_version": 2,
        "crm_provenance": {
            "workspace": "test",
            "deal_ids": [],
            "unprovable": False,
        },
        "generated_at": generated_at,
        "headline": "h",
        "revision_count": 0,
        "last_critique_score": None,
        "priorities": priorities,
    }


def _row(state, *, ek="thread", eid="thr-1", pk="follow_up_debt"):
    return MemoryRow(
        id="m1",
        user_email="u@x.example",
        entity_kind=ek,
        entity_id=eid,
        priority_kind=pk,
        state=state,
        snoozed_until=None,
        last_priority_id=None,
        resolved_evidence=None,
        first_seen_at="2026-06-03T07:00:00",
        last_seen_at="2026-06-03T09:00:00",
        last_state_change_at="2026-06-03T09:00:00",
        last_state_change_by="u@x.example",
    )


# ── generated_count (the done-for-the-day signal) ─────────────────────────────


def test_bundle_dict_sets_generated_count():
    out = _bundle_dict_from_metadata(
        _metadata([_meta_priority(), _meta_priority(pid="p2", eid="thr-2")])
    )
    assert out["generated_count"] == 2
    assert len(out["priorities"]) == 2


def test_bundle_dict_generated_count_zero_when_empty():
    assert _bundle_dict_from_metadata(_metadata([]))["generated_count"] == 0


def test_generated_count_survives_memory_filter():
    """User cleared 1 of 2 → generated_count stays 2 while the handled one
    drops off, so the UI shows kudos only once the remainder hits zero."""
    bundle = _bundle_dict_from_metadata(
        _metadata([_meta_priority(eid="thr-1"), _meta_priority(pid="p2", eid="thr-2")])
    )
    with patch.object(
        priority_memory,
        "load_all",
        return_value=[_row(priority_memory.STATE_RESOLVED, eid="thr-1")],
    ):
        out = _apply_priority_memory("u@x.example", bundle)
    assert out["generated_count"] == 2
    assert [p["id"] for p in out["priorities"]] == ["p2"]


def test_generated_count_preserved_when_nothing_filtered():
    bundle = _bundle_dict_from_metadata(_metadata([_meta_priority()]))
    with patch.object(priority_memory, "load_all", return_value=[]):
        out = _apply_priority_memory("u@x.example", bundle)
    assert out["generated_count"] == 1
    assert len(out["priorities"]) == 1


# ── recommendation → priority-card mapping ────────────────────────────────────


def _rec(
    rid="r1",
    *,
    type="stale_lead",
    lead_id="deal-1",
    lead_name="Acme",
    text="No touch in 30 days.",
    confidence=0.9,
    next_action="Email them",
):
    return SimpleNamespace(
        id=rid,
        type=type,
        lead_id=lead_id,
        lead_name=lead_name,
        recommendation_text=text,
        confidence=confidence,
        next_action=next_action,
    )


def test_confidence_to_urgency_buckets():
    assert _confidence_to_urgency(0.9) == "high"
    assert _confidence_to_urgency(0.6) == "medium"
    assert _confidence_to_urgency(0.2) == "low"
    assert _confidence_to_urgency(None) == "low"


def test_recommendation_to_item_maps_fields():
    item = _recommendation_to_item(_rec())
    assert item.origin == "recommendation"
    assert item.entity_kind == "deal"
    assert item.entity_id == "deal-1"
    assert item.urgency == "high"
    assert item.title == "Re-engage: Acme"
    assert item.why_it_matters == "No touch in 30 days."
    assert item.suggested_action == "Email them"
    assert item.source_channels == ["hubspot"]


def test_recommendation_to_item_unknown_type_falls_back():
    assert _recommendation_to_item(_rec(type="weird")).title.startswith("Follow up:")


# ── _fetch_other_recommendations ──────────────────────────────────────────────


def test_fetch_other_recommendations_maps_active_recs():
    recs = [_rec(rid="r1", lead_id="d1"), _rec(rid="r2", lead_id="d2")]
    with (
        patch.object(home, "_today_card_entity_ids", return_value=set()),
        patch("backend.routers.home.SessionLocal") as msf,
        patch("backend.routers.agents.fetch_top_recommendations", return_value=recs),
    ):
        msf.return_value.__enter__.return_value = MagicMock()
        items = _fetch_other_recommendations("u@x.example", limit=5)
    assert [i.id for i in items] == ["r1", "r2"]
    assert all(i.origin == "recommendation" for i in items)


def test_fetch_other_recommendations_dedupes_todays_card():
    """A rec for a deal already in today's card is skipped (no double-surface)."""
    recs = [_rec(rid="r1", lead_id="d1"), _rec(rid="r2", lead_id="d2")]
    with (
        patch.object(home, "_today_card_entity_ids", return_value={"d1"}),
        patch("backend.routers.home.SessionLocal") as msf,
        patch("backend.routers.agents.fetch_top_recommendations", return_value=recs),
    ):
        msf.return_value.__enter__.return_value = MagicMock()
        items = _fetch_other_recommendations("u@x.example", limit=5)
    assert [i.id for i in items] == ["r2"]


# ── endpoint branching (called directly, bypassing FastAPI DI) ────────────────


def _user(email="endpoint@x.example"):
    return SimpleNamespace(email=email)


def test_endpoint_returns_recommendations_without_running():
    bg = MagicMock()
    with patch.object(
        home,
        "_fetch_other_recommendations",
        return_value=[_recommendation_to_item(_rec())],
    ):
        out = asyncio.run(get_more_priorities(background_tasks=bg, user=_user()))
    assert isinstance(out, MoreResponse)
    assert out.status == "recommendations"
    assert len(out.items) == 1
    bg.add_task.assert_not_called()


def test_endpoint_kicks_background_run_when_no_recs():
    bg = MagicMock()
    user = _user(email="norec@x.example")
    with patch.object(home, "_fetch_other_recommendations", return_value=[]):
        out = asyncio.run(get_more_priorities(background_tasks=bg, user=user))
    assert out.status == "running"
    assert out.run_id
    # Off-GCP the durable dispatch degrades to the BackgroundTask fallback.
    bg.add_task.assert_called_once()
    # The guard is DB-backed now (#9): the run row is pending (bg is a mock,
    # so the fallback never actually ran) and dedupes across instances.
    active = run_state.active_run(user.email)
    assert active is not None
    assert active["run_id"] == out.run_id
    assert active["trigger"] == run_state.TRIGGER_MORE


def test_endpoint_does_not_double_kick_while_running():
    bg = MagicMock()
    user = _user(email="busy@x.example")
    run_state.create_run(user.email, trigger=run_state.TRIGGER_MORE)
    with patch.object(home, "_fetch_other_recommendations", return_value=[]):
        out = asyncio.run(get_more_priorities(background_tasks=bg, user=user))
    assert out.status == "already_running"
    bg.add_task.assert_not_called()

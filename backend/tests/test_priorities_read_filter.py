"""UC-25 read-path filter — a handled priority must not re-appear on reload.

The dismiss/snooze/resolve endpoints write ``priority_memory`` but never
rewrite the cached HomeCard, and the orchestrator's post-filter only runs on a
fresh pipeline run. ``_apply_priority_memory`` re-applies the user's memory on
every cached read so their choice survives a re-login. These tests pin that
behaviour down (the bug: "I checked it off and it came back when I logged in").
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from backend.routers.home import (
    _apply_priority_memory,
    _load_today_priorities,
    _load_visible_next_week,
    _merge_next_week,
)
from rubik_core.database.models import HomeCard
from rubik_core.priorities import memory as priority_memory
from rubik_core.priorities.memory import MemoryRow

# Card generated at 08:00 UTC. Memory changes default to 09:00 — i.e. AFTER the
# card — so "dismissed after this card" is the explicit, separate case.
CARD_TS = "2026-06-03T08:00:00+00:00"


def _priority(
    pid="p1", *, ek="thread", eid="thr-1", pk="follow_up_debt", title="Reply to Acme"
):
    return {
        "id": pid,
        "title": title,
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


def _bundle(priorities, generated_at=CARD_TS):
    return {
        "generated_at": generated_at,
        "headline": "h",
        "revision_count": 0,
        "last_critique_score": None,
        "priorities": priorities,
    }


def _eos_cards():
    return [
        _priority("todo", ek="todo", eid="t1", pk="eos_todo_due"),
        _priority("milestone", ek="milestone", eid="m1", pk="eos_milestone_due"),
        _priority("rock", ek="rock", eid="r1", pk="eos_rock_attention"),
        _priority("issue", ek="issue", eid="i1", pk="eos_issue_open"),
    ]


@pytest.mark.parametrize("lookup_fails", [False, True])
def test_cached_daily_omits_eos_cards_after_module_off(monkeypatch, lookup_fails):
    from backend.routers import home
    from rubik_core.control import repository

    if lookup_fails:
        monkeypatch.setattr(
            repository,
            "module_enabled",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("control down")),
        )
    else:
        monkeypatch.setattr(repository, "module_enabled", lambda *_a, **_k: False)
    saved = _bundle([*_eos_cards(), _priority("core")])
    saved["generated_count"] = 5
    saved["headline"] = "Your rock is off track"
    monkeypatch.setattr(
        home, "_today_daily_bundle", lambda _email: ("2026-06-03", saved)
    )
    monkeypatch.setattr(home, "_apply_priority_memory", lambda _email, bundle: bundle)
    monkeypatch.setattr(home, "_apply_deal_defers", lambda _email, bundle: bundle)
    monkeypatch.setattr(home, "_merge_next_week", lambda _email, bundle, _today: bundle)

    visible = _load_today_priorities("u@x.example")
    assert [card["id"] for card in visible["priorities"]] == ["core"]
    assert visible["generated_count"] == 1
    assert visible["headline"] == ""


def _row(
    state,
    *,
    ek="thread",
    eid="thr-1",
    pk="follow_up_debt",
    changed="2026-06-03T09:00:00",
    snoozed_until=None,
):
    return MemoryRow(
        id="m1",
        user_email="u@x.example",
        entity_kind=ek,
        entity_id=eid,
        priority_kind=pk,
        state=state,
        snoozed_until=snoozed_until,
        last_priority_id=None,
        resolved_evidence=None,
        first_seen_at="2026-06-03T07:00:00",
        last_seen_at="2026-06-03T09:00:00",
        last_state_change_at=changed,
        last_state_change_by="u@x.example",
    )


def _run(bundle, rows):
    with patch.object(priority_memory, "load_all", return_value=rows):
        return _apply_priority_memory("u@x.example", bundle)


# ── per-state rules ───────────────────────────────────────────────────────────


def test_resolved_is_dropped():
    """✓ Mark done is durable — drops on read regardless of card time."""
    out = _run(_bundle([_priority()]), [_row(priority_memory.STATE_RESOLVED)])
    assert out["priorities"] == []


def test_dismissed_before_card_is_dropped():
    """User dismissed at 09:00; card predates that (08:00) → stays gone."""
    out = _run(_bundle([_priority()]), [_row(priority_memory.STATE_DISMISSED)])
    assert out["priorities"] == []


def test_dismissed_then_regenerated_resurfaces():
    """Dismissed yesterday; today's card was generated after → a later run
    chose to resurface it on new evidence, so keep it."""
    bundle = _bundle([_priority()], generated_at="2026-06-03T10:00:00+00:00")
    out = _run(
        bundle, [_row(priority_memory.STATE_DISMISSED, changed="2026-06-02T09:00:00")]
    )
    assert len(out["priorities"]) == 1


def test_snoozed_future_is_dropped():
    future = (datetime.utcnow() + timedelta(days=2)).isoformat()
    out = _run(
        _bundle([_priority()]),
        [_row(priority_memory.STATE_SNOOZED, snoozed_until=future)],
    )
    assert out["priorities"] == []


def test_snoozed_past_is_revived():
    past = (datetime.utcnow() - timedelta(days=1)).isoformat()
    out = _run(
        _bundle([_priority()]),
        [_row(priority_memory.STATE_SNOOZED, snoozed_until=past)],
    )
    assert len(out["priorities"]) == 1


def test_active_is_kept():
    out = _run(_bundle([_priority()]), [_row(priority_memory.STATE_ACTIVE)])
    assert len(out["priorities"]) == 1


def test_no_memory_row_is_kept():
    out = _run(_bundle([_priority()]), [])
    assert len(out["priorities"]) == 1


def test_untagged_priority_is_kept():
    """A pre-UC-24 priority with no entity tags can't be keyed → always kept,
    even if an unrelated resolved row exists."""
    out = _run(
        _bundle([_priority(ek=None, eid=None, pk=None)]),
        [_row(priority_memory.STATE_RESOLVED)],
    )
    assert len(out["priorities"]) == 1


# ── robustness + selectivity ──────────────────────────────────────────────────


def test_load_all_failure_returns_bundle_unfiltered():
    """Best-effort: a memory-layer error must not blank the user's priorities."""
    with patch.object(priority_memory, "load_all", side_effect=RuntimeError("db down")):
        out = _apply_priority_memory("u@x.example", _bundle([_priority()]))
    assert len(out["priorities"]) == 1


def test_only_the_matching_priority_is_dropped():
    keep = _priority(pid="keep", eid="thr-keep")
    drop = _priority(pid="drop", eid="thr-drop")
    out = _run(
        _bundle([keep, drop]), [_row(priority_memory.STATE_RESOLVED, eid="thr-drop")]
    )
    assert [p["id"] for p in out["priorities"]] == ["keep"]


# ── wiring: the cached read path actually applies the filter ──────────────────


@patch("backend.routers.home.SessionLocal")
def test_load_today_priorities_applies_filter(mock_session_factory):
    metadata = {
        "schema_version": 2,
        "crm_provenance": {"workspace": "test", "deal_ids": [], "unprovable": False},
        "generated_at": CARD_TS,
        "headline": "h",
        "revision_count": 0,
        "last_critique_score": None,
        "priorities": [_priority()],
    }
    card = HomeCard(
        id="c1",
        user_email="u@x.example",
        date="2026-06-03",
        category="daily_briefing",
        content_md="x",
        metadata_json=metadata,
        priority=100,
    )
    mock_db = MagicMock()
    mock_session_factory.return_value.__enter__.return_value = mock_db
    two_filters = mock_db.query.return_value.filter.return_value.filter.return_value
    chain = two_filters.filter.return_value.order_by.return_value
    chain.first.return_value = card
    # The next-week merge runs a 2-filter query; return no next-week card so
    # this test stays focused on the daily memory filter.
    two_filters.order_by.return_value.first.return_value = None

    with patch.object(
        priority_memory, "load_all", return_value=[_row(priority_memory.STATE_RESOLVED)]
    ):
        out = _load_today_priorities("u@x.example")

    assert out is not None
    assert out["priorities"] == []


# ── FT next-week: visibility window + merge/dedup ─────────────────────────────


def _nw_card(week_start="2026-07-06", week_end="2026-07-12", priorities=None):
    meta = {
        "schema_version": 2,
        "crm_provenance": {"workspace": "test", "deal_ids": [], "unprovable": False},
        "generated_at": CARD_TS,
        "headline": "next week",
        "revision_count": 0,
        "last_critique_score": None,
        "priorities": priorities if priorities is not None else [_priority()],
        "report_kind": "next_week",
        "week_start": week_start,
        "week_end": week_end,
    }
    return HomeCard(
        id="nw1",
        user_email="u@x.example",
        date="2026-07-03",
        category="next_week_report",
        content_md="x",
        metadata_json=meta,
        priority=100,
    )


def _visible(today_str, card):
    """Run _load_visible_next_week with the given card + today, no memory rows."""
    with patch("backend.routers.home.SessionLocal") as mock_factory:
        mock_db = MagicMock()
        mock_factory.return_value.__enter__.return_value = mock_db
        chain = mock_db.query.return_value.filter.return_value.filter.return_value.order_by.return_value
        chain.first.return_value = card
        with patch.object(priority_memory, "load_all", return_value=[]):
            return _load_visible_next_week("u@x.example", today_str)


def test_cached_next_week_omits_eos_cards_after_module_off(monkeypatch):
    from rubik_core.control import repository

    monkeypatch.setattr(repository, "module_enabled", lambda *_a, **_k: False)
    card = _nw_card(priorities=[*_eos_cards(), _priority("core")])
    card.metadata_json["headline"] = "Rock next week"
    visible = _visible("2026-07-03", card)
    assert [item["id"] for item in visible["priorities"]] == ["core"]
    assert visible["generated_count"] == 1
    assert visible["headline"] == ""


def test_next_week_visible_friday_saturday_sunday():
    card = _nw_card()
    # Friday (generation day) through Sunday — before the target Monday.
    for today in ("2026-07-03", "2026-07-04", "2026-07-05"):
        out = _visible(today, card)
        assert out is not None, today
        assert len(out["priorities"]) == 1


def test_next_week_hidden_from_target_monday_onward():
    card = _nw_card()
    # Monday IS week_start → "until_week_start" hides it (daily briefing covers it).
    assert _visible("2026-07-06", card) is None
    # Well past the window end, too.
    assert _visible("2026-07-20", card) is None


def test_next_week_hidden_without_window_metadata():
    card = _nw_card()
    card.metadata_json.pop("week_start")
    assert _visible("2026-07-03", card) is None


def test_merge_next_week_dedups_and_tags_origin():
    daily = _bundle(
        [
            _priority(pid="d1", eid="thr-shared", pk="follow_up_debt"),
            {**_priority(pid="d2", eid="thr-x"), "signal_ids": ["evt-shared"]},
        ]
    )
    nw_items = [
        # Same memory key as d1 → dropped.
        _priority(pid="n1", eid="thr-shared", pk="follow_up_debt"),
        # Shares a signal id with d2 → dropped.
        {
            **_priority(pid="n2", eid="thr-y", pk="upcoming_prep"),
            "signal_ids": ["evt-shared"],
        },
        # Genuinely new → kept, tagged.
        _priority(pid="n3", eid="thr-new", pk="upcoming_prep"),
    ]
    nw_bundle = {**_bundle(nw_items), "week_start": "2026-07-06"}

    with patch("backend.routers.home._load_visible_next_week", return_value=nw_bundle):
        out = _merge_next_week("u@x.example", daily, "2026-07-03")

    ids = [p["id"] for p in out["priorities"]]
    assert ids == ["d1", "d2", "n3"]
    n3 = out["priorities"][-1]
    assert n3["origin"] == "next_week"
    assert n3["week_of"] == "2026-07-06"


def test_merge_next_week_noop_when_nothing_visible():
    daily = _bundle([_priority(pid="d1")])
    with patch("backend.routers.home._load_visible_next_week", return_value=None):
        out = _merge_next_week("u@x.example", daily, "2026-07-03")
    assert out == daily

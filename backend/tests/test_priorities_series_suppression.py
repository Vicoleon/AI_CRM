""" "Never prep this meeting again" has to survive the next occurrence.

Regression: a reader dismissed a recurring all-day event and wrote "No
preparation is needed for this meeting in the future." It came back. Google
gives every occurrence of a recurring event its own instance id
(``<seriesId>_20260803T160000Z``), and ``priority_memory`` keys on
``(entity_kind, entity_id, priority_kind)`` — so the dismissal only ever
silenced the one occurrence in front of them.

Two halves are tested here: the series id must reach the priority
(``_attach_series_ids``), and the post-filter must honor a series-level
dismissal against a *different* occurrence's entity_id.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest

from rubik_core.priorities import (
    ChannelKind,
    ChannelSignal,
    EntityKind,
    PriorityCandidate,
    PriorityKind,
)
from rubik_core.priorities.memory import (
    STATE_ACTIVE,
    STATE_DISMISSED,
    STATE_RESOLVED,
    STATE_SNOOZED,
    apply_post_filter,
)
from rubik_core.priorities.orchestrator import _attach_series_ids

SERIES = "34r4q9v6gjr8frotn9ntmbvhli"
WEEK1 = f"{SERIES}_20260803T160000Z"
WEEK2 = f"{SERIES}_20260810T160000Z"


@dataclass
class _Row:
    """Stand-in for memory.MemoryRow — apply_post_filter only reads these."""

    state: str
    last_state_change_at: str
    snoozed_until: str | None = None


def _calendar_signal(sig_id: str, *, series: str | None, ts: str) -> ChannelSignal:
    return ChannelSignal(
        id=sig_id,
        channel=ChannelKind.CALENDAR,
        title="Vistage JULY Mtg",
        timestamp=ts,
        entity_kind=EntityKind.EVENT,
        entity_id=sig_id,
        attrs={"recurring_event_id": series},
    )


def _prep(entity_id: str, *, signal_ids: list[str]) -> PriorityCandidate:
    return PriorityCandidate(
        title="Prepare for all-day Vistage meeting",
        urgency="high",
        signal_ids=signal_ids,
        priority_kind=PriorityKind.UPCOMING_PREP,
        entity_kind=EntityKind.EVENT,
        entity_id=entity_id,
    )


# ── the series id has to reach the priority at all ───────────────────────────


def test_attach_series_ids_lifts_the_recurring_id_off_the_signal():
    p = _prep(WEEK1, signal_ids=[WEEK1])
    sigs = {WEEK1: _calendar_signal(WEEK1, series=SERIES, ts="2026-08-03T16:00:00Z")}
    _attach_series_ids([p], sigs)
    assert p.series_entity_id == SERIES


def test_attach_series_ids_leaves_one_off_events_alone():
    """A non-recurring event has no series — the field must stay None so the
    card never offers a meaningless "never again" button."""
    p = _prep("evt-oneoff", signal_ids=["evt-oneoff"])
    sigs = {
        "evt-oneoff": _calendar_signal(
            "evt-oneoff", series=None, ts="2026-08-03T16:00:00Z"
        )
    }
    _attach_series_ids([p], sigs)
    assert p.series_entity_id is None


def test_attach_series_ids_ignores_non_calendar_signals():
    """A Gmail thread has no recurrence; don't invent one from another channel."""
    p = _prep("t-1", signal_ids=["t-1"])
    sigs = {
        "t-1": ChannelSignal(
            id="t-1",
            channel=ChannelKind.GMAIL,
            title="thread",
            attrs={"recurring_event_id": "should-be-ignored"},
        )
    }
    _attach_series_ids([p], sigs)
    assert p.series_entity_id is None


def test_attach_series_ids_is_idempotent():
    p = _prep(WEEK1, signal_ids=[WEEK1])
    p.series_entity_id = "already-set"
    sigs = {WEEK1: _calendar_signal(WEEK1, series=SERIES, ts="2026-08-03T16:00:00Z")}
    _attach_series_ids([p], sigs)
    assert p.series_entity_id == "already-set"


# ── the post-filter has to honor it ──────────────────────────────────────────


def test_series_dismissal_suppresses_a_later_occurrence():
    """THE regression. Dismissed week 1 as a series → week 2 must not appear,
    even though its entity_id (and therefore its memory key) is different."""
    p = _prep(WEEK2, signal_ids=[WEEK2])
    p.series_entity_id = SERIES
    memory = {
        ("event", SERIES, "upcoming_prep"): _Row(
            state=STATE_DISMISSED, last_state_change_at="2026-08-03T18:00:00"
        )
    }
    kept, drops = apply_post_filter(
        [p], memory, now=datetime(2026, 8, 10, 9, 0), signal_by_id={}
    )
    assert kept == []
    assert any(SERIES in d for d in drops)


def test_series_dismissal_is_not_revived_by_a_newer_occurrence():
    """The subtle half: a new occurrence is ALWAYS newer than the dismissal, so
    routing the series check through the normal new-evidence rule would
    resurface it every single week — the exact bug, reintroduced."""
    p = _prep(WEEK2, signal_ids=[WEEK2])
    p.series_entity_id = SERIES
    memory = {
        ("event", SERIES, "upcoming_prep"): _Row(
            state=STATE_DISMISSED, last_state_change_at="2026-08-03T18:00:00"
        )
    }
    # Week 2's signal is a week NEWER than the dismissal.
    signal_by_id = {
        WEEK2: _calendar_signal(WEEK2, series=SERIES, ts="2026-08-10T16:00:00Z")
    }
    kept, _ = apply_post_filter(
        [p], memory, now=datetime(2026, 8, 10, 9, 0), signal_by_id=signal_by_id
    )
    assert kept == []


def test_occurrence_dismissal_does_not_suppress_the_series():
    """The inverse must also hold: dismissing ONE occurrence leaves next
    week's alone. That's what the plain ✗ still means."""
    p = _prep(WEEK2, signal_ids=[WEEK2])
    p.series_entity_id = SERIES
    memory = {
        ("event", WEEK1, "upcoming_prep"): _Row(
            state=STATE_DISMISSED, last_state_change_at="2026-08-03T18:00:00"
        )
    }
    kept, _ = apply_post_filter(
        [p], memory, now=datetime(2026, 8, 10, 9, 0), signal_by_id={}
    )
    assert kept == [p]


def test_series_resolution_also_suppresses_later_occurrences():
    """✓ on the series is at least as durable as ✗."""
    p = _prep(WEEK2, signal_ids=[WEEK2])
    p.series_entity_id = SERIES
    memory = {
        ("event", SERIES, "upcoming_prep"): _Row(
            state=STATE_RESOLVED, last_state_change_at="2026-08-03T18:00:00"
        )
    }
    kept, _ = apply_post_filter([p], memory, now=datetime(2026, 8, 10, 9, 0))
    assert kept == []


def test_active_series_row_does_not_suppress():
    """Only handled states suppress — a series merely *seen* before must not."""
    p = _prep(WEEK2, signal_ids=[WEEK2])
    p.series_entity_id = SERIES
    memory = {
        ("event", SERIES, "upcoming_prep"): _Row(
            state=STATE_ACTIVE, last_state_change_at="2026-08-03T18:00:00"
        )
    }
    kept, _ = apply_post_filter([p], memory, now=datetime(2026, 8, 10, 9, 0))
    assert kept == [p]


def test_snoozed_series_row_does_not_permanently_suppress():
    """Snooze is the occurrence-scoped affordance and carries its own deadline;
    a snoozed SERIES row must not act as a forever-block."""
    p = _prep(WEEK2, signal_ids=[WEEK2])
    p.series_entity_id = SERIES
    memory = {
        ("event", SERIES, "upcoming_prep"): _Row(
            state=STATE_SNOOZED,
            last_state_change_at="2026-08-03T18:00:00",
            snoozed_until="2026-08-04T00:00:00",
        )
    }
    kept, _ = apply_post_filter([p], memory, now=datetime(2026, 8, 10, 9, 0))
    assert kept == [p]


def test_series_dismissal_is_scoped_to_the_priority_kind():
    """Suppressing prep for a series must not silence a different KIND of item
    about the same meeting (e.g. a follow-up on its action items)."""
    p = PriorityCandidate(
        title="Follow up on Vistage action items",
        signal_ids=[WEEK2],
        priority_kind=PriorityKind.PRE_TOUCHPOINT_FOLLOWUP,
        entity_kind=EntityKind.EVENT,
        entity_id=WEEK2,
    )
    p.series_entity_id = SERIES
    memory = {
        ("event", SERIES, "upcoming_prep"): _Row(
            state=STATE_DISMISSED, last_state_change_at="2026-08-03T18:00:00"
        )
    }
    kept, _ = apply_post_filter([p], memory, now=datetime(2026, 8, 10, 9, 0))
    assert kept == [p]


def test_priority_without_series_id_is_unaffected():
    """Back-compat: cards written before this shipped have no series_entity_id."""
    p = _prep(WEEK2, signal_ids=[WEEK2])
    assert p.series_entity_id is None
    memory = {
        ("event", SERIES, "upcoming_prep"): _Row(
            state=STATE_DISMISSED, last_state_change_at="2026-08-03T18:00:00"
        )
    }
    kept, _ = apply_post_filter([p], memory, now=datetime(2026, 8, 10, 9, 0))
    assert kept == [p]


# ── round-trip through HomeCard.metadata_json ────────────────────────────────


def test_series_id_survives_the_metadata_round_trip():
    """The dismiss endpoint reads the series id back out of the persisted card,
    so it has to survive bundle_to_metadata → bundle_from_metadata."""
    from rubik_core.priorities.models import PrioritiesBundle
    from rubik_core.priorities.orchestrator import (
        bundle_from_metadata,
        bundle_to_metadata,
    )

    p = _prep(WEEK1, signal_ids=[WEEK1])
    p.series_entity_id = SERIES
    bundle = PrioritiesBundle(
        user_email="ebrooks@acme.example",
        crm_provenance={"workspace": "test", "deal_ids": [], "unprovable": False},
        generated_at=(datetime(2026, 8, 3, 9, 0) + timedelta()).isoformat(),
        priorities=[p],
    )
    restored = bundle_from_metadata("ebrooks@acme.example", bundle_to_metadata(bundle))
    assert restored.priorities[0].series_entity_id == SERIES


def test_metadata_without_series_id_restores_as_none():
    """A source-proved card without a series id still restores as none."""
    from rubik_core.priorities.orchestrator import bundle_from_metadata

    restored = bundle_from_metadata(
        "ebrooks@acme.example",
        {
            "schema_version": 2,
            "crm_provenance": {
                "workspace": "test",
                "deal_ids": [],
                "unprovable": False,
            },
            "priorities": [{"id": "p1", "title": "card", "entity_id": WEEK1}],
        },
    )
    assert restored.priorities[0].series_entity_id is None


def test_legacy_metadata_cannot_restore_unproved_card_text():
    from rubik_core.priorities.orchestrator import bundle_from_metadata

    with pytest.raises(ValueError, match="provenance"):
        bundle_from_metadata(
            "ebrooks@acme.example",
            {"priorities": [{"id": "p1", "title": "old card", "entity_id": WEEK1}]},
        )

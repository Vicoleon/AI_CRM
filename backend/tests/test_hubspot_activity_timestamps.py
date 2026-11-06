"""A HubSpot signal is stamped with when something HAPPENED on the deal.

Regression: a dismissed deal card came back every morning.
Priorities memory overrules a ✗ when a cited signal's timestamp is after the
dismissal, and every HubSpot probe stamped a deal with no logged activity with
the fetch time, because ``_to_datetime`` falls back to "now". A deal whose
close date had passed with no client contact on record therefore looked brand
new on every run. The channel agent also kept the FIRST probe's timestamp when
several probes flagged one deal, so the order the probes ran in decided what
counted as news.

Now each probe carries ``activity_at`` (None when nothing was logged), the
agent stamps the signal with it, and a merged deal keeps its newest real time.
``occurred_at`` is unchanged: the correlator, judge and sorting still read it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.signals.hubspot import (
    _live_rows,
    _to_datetime,
    close_date_at_risk_signal,
    market_trigger_signal,
    new_product_match_signal,
    stage_stuck_signal,
    stale_lead_signal,
)
from backend.signals.types import Signal
from rubik_core.priorities import (
    EntityKind,
    PrioritiesConfig,
    PriorityCandidate,
    PriorityKind,
)
from rubik_core.priorities.agents import HubSpotChannelAgent
from rubik_core.priorities.memory import STATE_DISMISSED, apply_post_filter

USER = "ae@acme.example"


def _now() -> datetime:
    # _live_rows reads the real clock, so the fixtures must too.
    return datetime.now(timezone.utc)


def _deal(
    *,
    deal_id="d-1",
    close_in_days=-12,
    contacted_days_ago=None,
    updated_days_ago=3,
    stage_days_ago=None,
):
    """One open deal in the shape the live HubSpot search returns."""
    now = _now()
    props = {
        "hs_object_id": deal_id,
        "dealname": "Acme Platform Rollout",
        "dealstage": "stage-1",
        "amount": "25000",
        "closedate": (now + timedelta(days=close_in_days)).isoformat(),
        "notes_last_updated": (now - timedelta(days=updated_days_ago)).isoformat(),
    }
    if contacted_days_ago is not None:
        props["notes_last_contacted"] = (
            now - timedelta(days=contacted_days_ago)
        ).isoformat()
    if stage_days_ago is not None:
        props["hs_v2_date_entered_current_stage"] = (
            now - timedelta(days=stage_days_ago)
        ).isoformat()
    return {"id": deal_id, "properties": props}


def _source_signals(deal) -> list[Signal]:
    """Every probe the live path runs, in the order ``_fetch_live`` runs them."""
    stale, stuck, at_risk = _live_rows([deal], USER, "o-1", {"stage-1": "Proposal"})
    raw = [stale_lead_signal(r) for r in stale]
    raw += [stage_stuck_signal(r) for r in stuck]
    raw += [close_date_at_risk_signal(r) for r in at_risk]
    return [s for s in raw if s]


def _parse(raw):
    agent = HubSpotChannelAgent(user_email=USER, config=PrioritiesConfig.default())
    return agent.parse_signals(raw)


def _at(iso: str | None) -> datetime | None:
    return datetime.fromisoformat(iso) if iso else None


# ── the probe payload ────────────────────────────────────────────────────────


def test_a_passed_close_date_with_no_logged_contact_has_no_timestamp():
    """The deal that came back every morning: the date passed, nobody ever
    logged a client contact, so there is no activity time at all."""
    raw = _source_signals(_deal(close_in_days=-12, contacted_days_ago=None))
    (sig,) = [s for s in raw if s.signal_type == "close_date_at_risk"]
    assert sig.payload["activity_at"] is None
    # occurred_at keeps its old fallback for the readers that sort on it.
    assert sig.occurred_at is not None

    (parsed,) = _parse([sig])
    assert parsed.timestamp is None


def test_a_logged_client_contact_is_the_timestamp():
    deal = _deal(close_in_days=20, contacted_days_ago=27)
    contacted = _at(deal["properties"]["notes_last_contacted"])
    (sig,) = [s for s in _source_signals(deal) if s.signal_type == "close_date_at_risk"]
    assert _at(sig.payload["activity_at"]) == contacted

    (parsed,) = _parse([sig])
    assert _at(parsed.timestamp) == contacted


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda: stale_lead_signal(
                {
                    "deal_id": "d-1",
                    "owner_email": USER,
                    "days_since_last_activity": 30,
                    "last_activity_date": None,
                }
            ),
            id="stale_lead",
        ),
        pytest.param(
            lambda: stage_stuck_signal(
                {
                    "deal_id": "d-1",
                    "owner_email": USER,
                    "days_in_stage": 30,
                    "stage_entered_ts": None,
                }
            ),
            id="stage_stuck",
        ),
        pytest.param(
            lambda: close_date_at_risk_signal(
                {
                    "deal_id": "d-1",
                    "owner_email": USER,
                    "close_date": "2026-01-01",
                    "days_to_close": -10,
                    "last_engagement_at": None,
                }
            ),
            id="close_date_at_risk",
        ),
        pytest.param(
            lambda: new_product_match_signal(
                {"deal_id": "d-1", "owner_email": USER, "last_activity_date": None},
                ["Platform Rollout"],
            ),
            id="new_product_match",
        ),
        pytest.param(
            lambda: market_trigger_signal(
                {"trigger_text": "Acquired", "triggered_at": "not a date"},
                {"deal_id": "d-1", "owner_email": USER},
            ),
            id="market_trigger",
        ),
    ],
)
def test_no_probe_fabricates_an_activity_time(build):
    sig = build()
    assert sig is not None
    assert "activity_at" in sig.payload
    assert sig.payload["activity_at"] is None
    (parsed,) = _parse([sig])
    assert parsed.timestamp is None


def test_to_datetime_still_falls_back_to_now():
    """Day counts and sort keys read ``_to_datetime``; only the activity time
    stopped inventing a value."""
    before = _now()
    assert _to_datetime(None) >= before


def test_a_signal_without_activity_at_keeps_its_occurred_at():
    """A source that predates the key, or a test fixture, is stamped as before."""
    occurred = _now() - timedelta(days=2)
    sig = Signal(
        user_email=USER,
        source="hubspot",
        signal_type="stale_lead",
        summary="stale",
        occurred_at=occurred,
        entity_refs={"deal_id": "d-1"},
        payload={"owner_email": USER},
    )
    (parsed,) = _parse([sig])
    assert _at(parsed.timestamp) == occurred


# ── several probes on one deal ───────────────────────────────────────────────


def test_two_probes_on_one_deal_keep_the_newest_real_time():
    """stale_lead runs first and carries the older time; the stage move that
    stage_stuck saw later is the newest thing that happened on the deal."""
    deal = _deal(close_in_days=60, updated_days_ago=30, stage_days_ago=16)
    raw = _source_signals(deal)
    assert [s.signal_type for s in raw] == ["stale_lead", "stage_stuck"]

    (parsed,) = _parse(raw)
    assert parsed.attrs["signal_types"] == ["stage_stuck", "stale_lead"]
    stage_move = _at(deal["properties"]["hs_v2_date_entered_current_stage"])
    assert _at(parsed.timestamp) == stage_move


def test_a_real_time_beats_a_missing_one_in_either_order():
    deal = _deal(close_in_days=-12, contacted_days_ago=None, updated_days_ago=30)
    raw = _source_signals(deal)
    assert [s.signal_type for s in raw] == ["stale_lead", "close_date_at_risk"]
    updated = _at(deal["properties"]["notes_last_updated"])

    (forward,) = _parse(raw)
    (backward,) = _parse(list(reversed(raw)))
    assert _at(forward.timestamp) == updated
    assert _at(backward.timestamp) == updated


def test_a_merged_deal_with_no_activity_anywhere_has_no_timestamp():
    a = close_date_at_risk_signal(
        {
            "deal_id": "d-1",
            "owner_email": USER,
            "close_date": "2026-01-01",
            "days_to_close": -10,
        }
    )
    b = stage_stuck_signal({"deal_id": "d-1", "owner_email": USER, "days_in_stage": 30})
    (parsed,) = _parse([a, b])
    assert parsed.timestamp is None


# ── the dismissal it was breaking ────────────────────────────────────────────


def _dismissed_card(parsed, *, dismissed_at: datetime):
    card = PriorityCandidate(
        title="Acme Platform Rollout close date passed",
        signal_ids=[parsed.id],
        priority_kind=PriorityKind.CLOSE_DATE_AT_RISK,
        entity_kind=EntityKind.DEAL,
        entity_id=parsed.entity_id,
    )
    row = SimpleNamespace(
        entity_kind=EntityKind.DEAL.value,
        entity_id=parsed.entity_id,
        priority_kind=PriorityKind.CLOSE_DATE_AT_RISK.value,
        state=STATE_DISMISSED,
        last_state_change_at=dismissed_at.isoformat(),
        snoozed_until=None,
    )
    memory = {(row.entity_kind, row.entity_id, row.priority_kind): row}
    return card, memory


def _naive_utc_now() -> datetime:
    return _now().replace(tzinfo=None)


def test_a_dismissed_card_on_a_deal_with_no_activity_stays_dismissed():
    now = _naive_utc_now()
    raw = _source_signals(_deal(close_in_days=-12, contacted_days_ago=None))
    (parsed,) = _parse(raw)
    card, memory = _dismissed_card(parsed, dismissed_at=now - timedelta(days=1))

    kept, drops = apply_post_filter(
        [card], memory, now=now, signal_by_id={parsed.id: parsed}
    )
    assert kept == []
    assert len(drops) == 1 and "dismissed" in drops[0]


def test_a_client_contact_after_the_dismissal_still_brings_it_back():
    """The control: real activity after the ✗ is still news."""
    now = _naive_utc_now()
    deal = _deal(close_in_days=-12, contacted_days_ago=0)
    raw = _source_signals(deal)
    (parsed,) = _parse(raw)
    card, memory = _dismissed_card(parsed, dismissed_at=now - timedelta(days=1))

    kept, _drops = apply_post_filter(
        [card], memory, now=now, signal_by_id={parsed.id: parsed}
    )
    assert kept == [card]


def test_a_stage_stuck_deal_counts_a_client_contact_logged_since():
    """The deal has sat in its stage for 40 days, but the client was contacted
    yesterday. That contact is the newest thing that happened on the deal."""
    now = _now()
    entered = now - timedelta(days=40)
    contacted = now - timedelta(days=1)
    sig = stage_stuck_signal(
        {
            "deal_id": "d-1",
            "owner_email": USER,
            "days_in_stage": 40,
            "stage_entered_ts": entered.isoformat(),
            "last_engagement_at": contacted.isoformat(),
        }
    )
    assert _at(sig.payload["activity_at"]) == contacted


def test_a_stage_stuck_deal_with_no_contact_keeps_the_stage_move():
    now = _now()
    entered = now - timedelta(days=40)
    sig = stage_stuck_signal(
        {
            "deal_id": "d-1",
            "owner_email": USER,
            "days_in_stage": 40,
            "stage_entered_ts": entered.isoformat(),
        }
    )
    assert _at(sig.payload["activity_at"]) == entered

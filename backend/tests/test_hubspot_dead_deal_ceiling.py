"""A deal can be stale enough to stop being an action item.

Regression: a priority card told its reader an invoice reminder was "a critical
financial deadline" because "the related deal with Umbrella Corp. has been
stalled for 254 days." The deal had closed long before: the client had been
acquired and was not renewing, and nothing in the CRM said so.

The truth isn't in the CRM — which is the point. Staleness used
to make a deal MORE urgent without limit (`_stage_stuck_severity` returned
"high" for anything over 30 days, no upper bound), so a record nobody had
touched in eight months became the strongest evidence on the card.
"""

from __future__ import annotations

from backend.signals.hubspot import (
    DEAL_MAX_AGE_DAYS,
    DEAL_PRESUMED_DEAD_DAYS,
    stage_stuck_signal,
    stale_lead_signal,
)

OWNER = "ebrooks@acme.example"


def _stuck_row(days: int) -> dict:
    return {
        "deal_id": "d-umbrella",
        "deal_name": "Umbrella Annual Renewal",
        "company_name": "Umbrella Corp.",
        "owner_email": OWNER,
        "stage_label": "Legal Review",
        "days_in_stage": days,
        "stage_entered_ts": "2025-11-01T00:00:00Z",
        "amount": "12000",
    }


def _stale_row(days: int) -> dict:
    return {
        "deal_id": "d-umbrella",
        "deal_name": "Umbrella Annual Renewal",
        "company_name": "Umbrella Corp.",
        "owner_email": OWNER,
        "dealstage": "legalreview",
        "days_since_last_activity": days,
        "last_activity_date": "2025-11-01",
        "amount": "12000",
    }


# ── the hard ceiling ─────────────────────────────────────────────────────────


def test_the_254_day_deal_is_dropped_entirely():
    """254 days — the number on the card that prompted the ceiling."""
    assert stage_stuck_signal(_stuck_row(254)) is None
    assert stale_lead_signal(_stale_row(254)) is None


def test_deals_past_the_max_age_are_dropped():
    assert stage_stuck_signal(_stuck_row(DEAL_MAX_AGE_DAYS + 1)) is None
    assert stale_lead_signal(_stale_row(DEAL_MAX_AGE_DAYS + 1)) is None


def test_deals_exactly_at_the_max_age_still_surface():
    """The ceiling is exclusive — an off-by-one here silently drops live deals."""
    assert stage_stuck_signal(_stuck_row(DEAL_MAX_AGE_DAYS)) is not None
    assert stale_lead_signal(_stale_row(DEAL_MAX_AGE_DAYS)) is not None


# ── the presumed-dead band: visible, but never urgent ────────────────────────


def test_presumed_dead_stuck_deal_is_forced_to_low_severity():
    """Was "high" for anything over 30 days, forever. That's what let a
    months-dead deal act as corroborating evidence of a real deadline."""
    sig = stage_stuck_signal(_stuck_row(DEAL_PRESUMED_DEAD_DAYS + 1))
    assert sig is not None
    assert sig.severity_hint == "low"
    assert sig.payload["presumed_dead"] is True


def test_presumed_dead_stale_deal_is_forced_to_low_severity():
    sig = stale_lead_signal(_stale_row(DEAL_PRESUMED_DEAD_DAYS + 1))
    assert sig is not None
    assert sig.severity_hint == "low"
    assert sig.payload["presumed_dead"] is True


def test_presumed_dead_summary_says_the_record_is_probably_wrong():
    """The summary is what the LLM reads — it has to carry the caveat, not just
    a day count the model can re-interpret as urgency."""
    for sig in (
        stage_stuck_signal(_stuck_row(DEAL_PRESUMED_DEAD_DAYS + 1)),
        stale_lead_signal(_stale_row(DEAL_PRESUMED_DEAD_DAYS + 1)),
    ):
        assert sig is not None
        assert "likely out of date" in sig.summary


# ── genuinely live deals must be untouched ───────────────────────────────────


def test_a_normally_stuck_deal_keeps_its_high_severity():
    sig = stage_stuck_signal(_stuck_row(45))
    assert sig is not None
    assert sig.severity_hint == "high"
    assert sig.payload["presumed_dead"] is False
    assert "stuck in 'Legal Review' for 45 days" in sig.summary


def test_a_normally_stale_deal_keeps_its_high_severity():
    sig = stale_lead_signal(_stale_row(45))
    assert sig is not None
    assert sig.severity_hint == "high"
    assert sig.payload["presumed_dead"] is False


def test_a_recently_stuck_deal_is_still_medium():
    sig = stage_stuck_signal(_stuck_row(25))
    assert sig is not None
    assert sig.severity_hint == "medium"


def test_below_the_stuck_floor_is_still_nothing():
    """The pre-existing lower bound must survive the new upper one."""
    assert stage_stuck_signal(_stuck_row(5)) is None
    assert stale_lead_signal(_stale_row(5)) is None


def test_thresholds_are_ordered_sanely():
    """A presumed-dead band that starts above the hard ceiling would be dead
    code — every such deal would already have been dropped."""
    assert DEAL_PRESUMED_DEAD_DAYS < DEAL_MAX_AGE_DAYS


def test_thresholds_are_tunable_at_runtime(monkeypatch):
    """The ceilings are read per call, not frozen at import, so Sales ops can
    retune them from the environment without a redeploy of new code."""
    from rubik_core.config import settings

    monkeypatch.setattr(settings, "HUBSPOT_DEAL_MAX_AGE_DAYS", 30, raising=False)
    monkeypatch.setattr(settings, "HUBSPOT_DEAL_PRESUMED_DEAD_DAYS", 20, raising=False)
    # 45 days was "high" under the defaults; under the tightened ceiling it's gone.
    assert stage_stuck_signal(_stuck_row(45)) is None
    # And 25 days now lands in the presumed-dead band.
    sig = stage_stuck_signal(_stuck_row(25))
    assert sig is not None
    assert sig.severity_hint == "low"


# ── the channel agent must not undo the cap when probes merge ────────────────


def test_merging_two_probes_cannot_re_inflate_a_dead_deal():
    """One deal can be flagged stale AND stage-stuck. The merge takes max()
    severity, so a second probe's "high" would otherwise resurrect the urgency
    the age cap just removed."""
    from rubik_core.priorities import PrioritiesConfig
    from rubik_core.priorities.agents import HubSpotChannelAgent

    dead = stale_lead_signal(_stale_row(DEAL_PRESUMED_DEAD_DAYS + 1))
    # A second probe on the SAME deal_id that on its own reads "high".
    live_looking = stage_stuck_signal(_stuck_row(45))
    assert dead is not None and live_looking is not None

    agent = HubSpotChannelAgent(user_email=OWNER, config=PrioritiesConfig.default())
    signals = agent.parse_signals([live_looking, dead])

    assert len(signals) == 1
    attrs = signals[0].attrs
    assert attrs["presumed_dead"] is True
    assert attrs["severity_hint"] == "low"


def test_prompt_shows_presumed_dead_to_the_extractor():
    from rubik_core.priorities import PrioritiesConfig
    from rubik_core.priorities.agents import HubSpotChannelAgent

    sig = stale_lead_signal(_stale_row(DEAL_PRESUMED_DEAD_DAYS + 1))
    agent = HubSpotChannelAgent(user_email=OWNER, config=PrioritiesConfig.default())
    prompt = agent.build_prompt(agent.parse_signals([sig]))
    assert "presumed_dead: True" in prompt


def test_decision_maker_is_told_not_to_treat_dead_deals_as_urgent():
    from rubik_core.priorities.agents import DecisionMakerAgent
    from rubik_core.priorities.config import PrioritiesConfig
    from rubik_core.priorities.models import ConsolidatedDigest

    agent = DecisionMakerAgent(PrioritiesConfig.default())
    prompt = agent._build_prompt(
        ConsolidatedDigest(narrative="n", all_signals=[]),
        max_priorities=5,
        focus=None,
        entity_index={},
        department=None,
        playbook_text="",
        previous_attempt=None,
        critique=None,
    )
    assert "presumed_dead" in prompt
    assert "stale data, not a deadline" in prompt

"""Regression tests for the forward-looking / temporally-anchored AI
Priorities pipeline.

These guard the fix for the "surfaced a past event as something to act on"
bug (e.g. the homepage saying "prep for the L10 meeting" for a meeting that
happened the day before). They are deterministic and LLM-free: every
"now" is pinned, so they stay valid as the clock advances.
"""

from __future__ import annotations

from datetime import datetime, timezone

from backend.signals.correlator import CorrelationGroup
from backend.signals.judge import _build_prompt as judge_build_prompt
from backend.signals.judge import _signal_view
from backend.signals.types import Signal
from rubik_core.priorities import (
    ChannelKind,
    ChannelSignal,
    ConsolidatedDigest,
    PriorityCandidate,
    PriorityKind,
)
from rubik_core.priorities.agents import (
    CriticAgent,
    DecisionMakerAgent,
    GmailChannelAgent,
)
from rubik_core.priorities.agents.critic import _signal_carries_concrete_anchor
from rubik_core.priorities.config import PrioritiesConfig
from rubik_core.priorities.orchestrator import _drop_past_anchored
from rubik_core.priorities.temporal import (
    REPORT_KIND_NEXT_WEEK,
    format_when_local,
    is_future,
    is_past,
    next_week_window,
    parse_to_absolute,
    resolve_user_now,
    tense_of,
    user_today_str,
)

# 2026-06-25 09:00 in US/Pacific (16:00 UTC). "Today" = 2026-06-25.
NOW_UTC = datetime(2026, 6, 25, 16, 0, tzinfo=timezone.utc)
PACIFIC = "America/Los_Angeles"
YESTERDAY = "2026-06-24"
TODAY = "2026-06-25"
TOMORROW = "2026-06-26"


def _ctx(now_utc: datetime = NOW_UTC, **kw):
    return resolve_user_now(now_utc=now_utc, tz_name=PACIFIC, **kw)


# ── temporal helpers ─────────────────────────────────────────────────────────


def test_format_when_local_converts_offset_iso_to_reader_wall_clock():
    """The +2h drift bug: a card said "1:00 PM" for an 11:00 AM meeting.

    Whatever offset the source ISO carries, the reader (Pacific) must see the
    same wall clock — the instant is what matters, not the stored offset.
    """
    ctx = _ctx()  # America/Los_Angeles
    # 18:00Z == 11:00 PDT.
    assert (
        format_when_local("2026-07-02T18:00:00Z", ctx) == "Thursday Jul 2, 11:00 AM PDT"
    )
    # Same instant expressed in a Central (-05:00) offset — the source clock
    # reads "13:00", but the Pacific reader must still see 11:00 AM.
    assert (
        format_when_local("2026-07-02T13:00:00-05:00", ctx)
        == "Thursday Jul 2, 11:00 AM PDT"
    )
    # An explicit +00:00 offset resolves identically to a trailing Z.
    assert (
        format_when_local("2026-07-02T18:00:00+00:00", ctx)
        == "Thursday Jul 2, 11:00 AM PDT"
    )


def test_format_when_local_respects_a_different_reader_timezone():
    eastern = resolve_user_now(now_utc=NOW_UTC, tz_name="America/New_York")
    # 18:00Z == 14:00 EDT.
    assert (
        format_when_local("2026-07-02T18:00:00Z", eastern)
        == "Thursday Jul 2, 2:00 PM EDT"
    )


def test_format_when_local_handles_all_day_and_unparseable():
    ctx = _ctx()
    # All-day events carry a date, not an instant — date only, no bogus midnight.
    assert format_when_local("2026-07-02", ctx, all_day=True) == "Thursday Jul 2"
    # A date without the all_day flag is still treated as date-only.
    assert format_when_local("2026-07-02", ctx) == "Thursday Jul 2"
    # Missing / blank / unparseable → None so callers can omit the line.
    assert format_when_local("", ctx) is None
    assert format_when_local(None, ctx) is None
    assert format_when_local("not-a-date", ctx) is None


def test_parse_to_absolute_handles_iso_and_dicts():
    ctx = _ctx()
    assert parse_to_absolute("2026-06-24", ctx).isoformat() == YESTERDAY
    assert parse_to_absolute("2026-06-28T15:00:00Z", ctx).isoformat() == "2026-06-28"
    assert (
        parse_to_absolute({"resolved": "2026-06-30"}, ctx).isoformat() == "2026-06-30"
    )
    assert parse_to_absolute("due 2026-06-30 EOD", ctx).isoformat() == "2026-06-30"
    # Unresolvable free-form text → None (never treated as past).
    assert parse_to_absolute("Friday EOD", ctx) is None
    assert parse_to_absolute("", ctx) is None


def test_is_past_and_is_future_only_fire_on_resolvable_dates():
    ctx = _ctx()
    assert is_past(YESTERDAY, ctx) is True
    assert is_past(TODAY, ctx) is False  # same-day is not past
    assert is_future(TODAY, ctx) is True
    assert is_future(TOMORROW, ctx) is True
    assert is_future(YESTERDAY, ctx) is False
    # Uncertainty never counts as past OR future.
    assert is_past("next Tuesday", ctx) is False
    assert is_future("next Tuesday", ctx) is False


def test_tense_of_classifies():
    ctx = _ctx()
    assert tense_of(YESTERDAY, ctx) == "past"
    assert tense_of(TODAY, ctx) == "today"
    assert tense_of(TOMORROW, ctx) == "future"
    assert tense_of("whenever", ctx) == "unknown"


def test_temporal_block_names_today_and_tz():
    block = _ctx().block()
    assert "2026-06-25" in block
    assert PACIFIC in block
    assert "PAST" in block  # the "treat past as history" instruction


def test_user_today_str_uses_local_date_not_utc():
    # 2026-06-26 06:00 UTC == 2026-06-25 23:00 Pacific. The card key must be
    # the user's LOCAL day, or the evening-Pacific reload shows an empty card.
    late = datetime(2026, 6, 26, 6, 0, tzinfo=timezone.utc)
    assert user_today_str(tz_name=PACIFIC, now_utc=late) == "2026-06-25"
    # And UTC keying would (wrongly) have said the 26th:
    assert late.strftime("%Y-%m-%d") == "2026-06-26"


# ── the deterministic past-event post-filter (the hard backstop) ─────────────


def _sig(sid, channel, **attrs):
    return ChannelSignal(id=sid, channel=channel, title=f"sig-{sid}", attrs=dict(attrs))


def _prio(kind, sid, *, urgency="high", title="p"):
    return PriorityCandidate(
        title=title, urgency=urgency, priority_kind=kind, signal_ids=[sid]
    )


def test_past_prep_event_is_dropped():
    """The exact L10 bug: an upcoming_prep whose only anchor is yesterday."""
    ctx = _ctx()
    signals = {
        "evt": _sig("evt", ChannelKind.CALENDAR, start_iso=f"{YESTERDAY}T14:00:00Z")
    }
    kept, drops = _drop_past_anchored(
        [_prio(PriorityKind.UPCOMING_PREP, "evt")], signals, ctx
    )
    assert kept == []
    assert any("past/elapsed" in d for d in drops)


def test_future_prep_event_survives():
    ctx = _ctx()
    signals = {
        "evt": _sig("evt", ChannelKind.CALENDAR, start_iso=f"{TOMORROW}T14:00:00Z")
    }
    kept, drops = _drop_past_anchored(
        [_prio(PriorityKind.UPCOMING_PREP, "evt")], signals, ctx
    )
    assert len(kept) == 1
    assert kept[0].urgency == "high"


def test_prep_justified_only_by_transcript_is_dropped():
    """A Meet transcript proves the meeting already happened — it can never
    justify prep, even without an explicit date on the signal."""
    ctx = _ctx()
    signals = {"t": _sig("t", ChannelKind.MEET)}
    kept, drops = _drop_past_anchored(
        [_prio(PriorityKind.UPCOMING_PREP, "t")], signals, ctx
    )
    assert kept == []
    assert any("transcript" in d for d in drops)


def test_backward_debt_with_no_future_anchor_is_downranked_not_dropped():
    ctx = _ctx()
    signals = {
        "th": _sig(
            "th",
            ChannelKind.GMAIL,
            direction="i_sent_last",
            thread_idle_days=4,
        )
    }
    kept, drops = _drop_past_anchored(
        [_prio(PriorityKind.FOLLOW_UP_DEBT, "th", urgency="high")], signals, ctx
    )
    assert len(kept) == 1  # never dropped
    assert kept[0].urgency == "medium"  # but down-ranked
    assert any("down-ranked" in d for d in drops)


def test_backward_debt_with_future_anchor_keeps_high():
    ctx = _ctx()
    signals = {
        "th": _sig(
            "th",
            ChannelKind.GMAIL,
            mentioned_dates=[{"raw": "Mon", "resolved": TOMORROW, "tense": "future"}],
        )
    }
    kept, _ = _drop_past_anchored(
        [_prio(PriorityKind.FOLLOW_UP_DEBT, "th", urgency="high")], signals, ctx
    )
    assert kept[0].urgency == "high"


def test_same_day_elapsed_prep_is_dropped():
    """A meeting earlier TODAY (09:00 vs now 09:00... use 07:00) is over — you
    can't prep for it. Date-granularity alone would wrongly keep it."""
    ctx = _ctx()  # now = 2026-06-25 09:00 Pacific
    signals = {
        "evt": _sig("evt", ChannelKind.CALENDAR, start_iso="2026-06-25T07:00:00-07:00")
    }
    kept, drops = _drop_past_anchored(
        [_prio(PriorityKind.UPCOMING_PREP, "evt")], signals, ctx
    )
    assert kept == []
    assert any("past/elapsed" in d for d in drops)


def test_same_day_future_time_prep_survives():
    """A meeting LATER today (14:00 vs now 09:00) is still preppable."""
    ctx = _ctx()
    signals = {
        "evt": _sig("evt", ChannelKind.CALENDAR, start_iso="2026-06-25T14:00:00-07:00")
    }
    kept, _ = _drop_past_anchored(
        [_prio(PriorityKind.UPCOMING_PREP, "evt")], signals, ctx
    )
    assert len(kept) == 1


def test_unresolved_future_cocited_with_past_is_kept():
    """Never drop on uncertainty: an unresolvable future date ('Friday') cited
    alongside a resolved past date must NOT trigger the all-past drop."""
    ctx = _ctx()
    signals = {
        "th": _sig(
            "th",
            ChannelKind.GMAIL,
            mentioned_dates=[
                {"raw": "Friday EOD", "resolved": None, "tense": "unknown"},
                {"raw": "2026-06-24", "resolved": YESTERDAY, "tense": "past"},
            ],
        )
    }
    kept, _ = _drop_past_anchored(
        [_prio(PriorityKind.APPROACHING_DEADLINE, "th")], signals, ctx
    )
    assert len(kept) == 1


def test_transcript_only_approaching_deadline_is_dropped():
    """The transcript-proves-it-is-over guard applies to ALL forward kinds,
    not just prep."""
    ctx = _ctx()
    signals = {"t": _sig("t", ChannelKind.MEET)}
    kept, drops = _drop_past_anchored(
        [_prio(PriorityKind.APPROACHING_DEADLINE, "t")], signals, ctx
    )
    assert kept == []
    assert any("transcript" in d for d in drops)


def test_pre_touchpoint_followup_is_kept_after_past_meeting():
    """pre_touchpoint_followup's action (send the follow-up) stays valid after
    a past meeting — it must be kept (down-ranked), never dropped."""
    ctx = _ctx()
    signals = {"t": _sig("t", ChannelKind.MEET)}
    kept, _ = _drop_past_anchored(
        [_prio(PriorityKind.PRE_TOUCHPOINT_FOLLOWUP, "t", urgency="high")], signals, ctx
    )
    assert len(kept) == 1
    assert kept[0].urgency == "medium"  # down-ranked, not dropped


# ── critic future-anchor blocker ─────────────────────────────────────────────


def test_critic_anchor_requires_future_date():
    ctx = _ctx()
    past_sig = _sig("a", ChannelKind.HUBSPOT, close_date=YESTERDAY)
    future_sig = _sig("b", ChannelKind.HUBSPOT, close_date=TOMORROW)
    amount_sig = _sig("c", ChannelKind.HUBSPOT, mentioned_amounts=["$50k"])
    assert _signal_carries_concrete_anchor(past_sig, temporal=ctx) is False
    assert _signal_carries_concrete_anchor(future_sig, temporal=ctx) is True
    # A non-temporal anchor (amount) still counts.
    assert _signal_carries_concrete_anchor(amount_sig, temporal=ctx) is True


# ── prompt anchoring ─────────────────────────────────────────────────────────


def test_decision_maker_prompt_contains_today():
    agent = DecisionMakerAgent(PrioritiesConfig.default())
    digest = ConsolidatedDigest(narrative="n", all_signals=[])
    prompt = agent._build_prompt(
        digest,
        max_priorities=5,
        focus=None,
        entity_index={},
        department=None,
        playbook_text="",
        temporal=_ctx(),
    )
    assert "2026-06-25" in prompt
    assert PACIFIC in prompt


def test_critic_prompt_contains_today():
    agent = CriticAgent(PrioritiesConfig.default())
    digest = ConsolidatedDigest(narrative="n", all_signals=[])
    prompt = agent._build_prompt(digest, [], temporal=_ctx())
    assert "2026-06-25" in prompt


# ── extractor date normalization ─────────────────────────────────────────────


def test_gmail_normalizes_mentioned_dates():
    agent = GmailChannelAgent(
        user_email="me@x.example", config=PrioritiesConfig.default()
    )
    agent.temporal = _ctx()
    sig = ChannelSignal(
        id="t1",
        channel=ChannelKind.GMAIL,
        title="t",
        attrs={
            "mentioned_dates": ["2026-06-24", {"raw": "next Mon", "resolved": TOMORROW}]
        },
    )
    agent._normalize_mentioned_dates(sig)
    out = sig.attrs["mentioned_dates"]
    assert out[0] == {"raw": "2026-06-24", "resolved": YESTERDAY, "tense": "past"}
    assert out[1]["resolved"] == TOMORROW
    assert out[1]["tense"] == "future"


# ── pipeline #2: judge anchoring + imminence ─────────────────────────────────


def _cal_signal(starts_at: str) -> Signal:
    return Signal(
        user_email="rep@acme.example",
        source="calendar",
        signal_type="upcoming_meeting",
        summary="meeting",
        occurred_at=datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc),
        entity_refs={"event_id": "e1"},
        payload={"event_title": "Acme sync", "starts_at": starts_at},
    )


def test_judge_signal_view_marks_event_imminence():
    past = _signal_view(_cal_signal("2026-06-24T14:00:00Z"), now=NOW_UTC)
    future = _signal_view(_cal_signal("2026-06-26T14:00:00Z"), now=NOW_UTC)
    assert past["event_in_hours"] < 0  # already happened
    assert future["event_in_hours"] > 0  # still upcoming
    assert "occurred_hours_ago" in past


def test_judge_prompt_contains_temporal_context():
    grp = CorrelationGroup(
        correlation_id="event:e1",
        user_email="rep@acme.example",
        primary_entity_kind="event",
        primary_entity_ref="e1",
        signals=[_cal_signal("2026-06-26T14:00:00Z")],
    )
    prompt = judge_build_prompt([grp], now=NOW_UTC)
    assert "TEMPORAL CONTEXT" in prompt
    assert "2026-06-25" in prompt


# ── FT next-week look-ahead window ─────────────────────────────────────────────

# All pins are noon Pacific so the local date is unambiguous.
_FRI = datetime(2026, 7, 3, 19, 0, tzinfo=timezone.utc)  # Fri 2026-07-03 12:00 PDT
_SAT = datetime(2026, 7, 4, 19, 0, tzinfo=timezone.utc)  # Sat 2026-07-04
_SUN = datetime(2026, 7, 5, 19, 0, tzinfo=timezone.utc)  # Sun 2026-07-05
_THU = datetime(2026, 7, 2, 19, 0, tzinfo=timezone.utc)  # Thu 2026-07-02
_MON = datetime(2026, 7, 6, 19, 0, tzinfo=timezone.utc)  # Mon 2026-07-06


def _local(now_utc):
    return resolve_user_now(now_utc=now_utc, tz_name=PACIFIC).now_local


def test_next_week_window_always_targets_the_following_monday():
    # Friday → the Monday 3 days out; window is that Mon..Sun.
    ws, we = next_week_window(_local(_FRI))
    assert ws.isoformat() == "2026-07-06"
    assert we.isoformat() == "2026-07-12"
    # Saturday → +2, Sunday → +1: same target week as Friday.
    assert next_week_window(_local(_SAT))[0].isoformat() == "2026-07-06"
    assert next_week_window(_local(_SUN))[0].isoformat() == "2026-07-06"
    # Thursday → +4: still next week's Monday, not this week's.
    assert next_week_window(_local(_THU))[0].isoformat() == "2026-07-06"
    # Monday → +7: NEVER the current week.
    assert next_week_window(_local(_MON))[0].isoformat() == "2026-07-13"


def test_resolve_user_now_next_week_sets_window_and_horizon():
    ctx = resolve_user_now(
        now_utc=_FRI, tz_name=PACIFIC, report_kind=REPORT_KIND_NEXT_WEEK
    )
    assert ctx.report_kind == REPORT_KIND_NEXT_WEEK
    assert ctx.week_start.isoformat() == "2026-07-06"
    assert ctx.week_end.isoformat() == "2026-07-12"
    # Horizon reaches the END of the target week (start of the following
    # Monday), so a Sunday-of-week anchor is still "within".
    assert ctx.horizon_local.date().isoformat() == "2026-07-13"
    assert ctx.lookahead_hours > 72  # spans ~10 days from Friday noon


def test_next_week_block_names_window_and_keeps_past_clause():
    ctx = resolve_user_now(
        now_utc=_FRI, tz_name=PACIFIC, report_kind=REPORT_KIND_NEXT_WEEK
    )
    block = ctx.block()
    assert "NEXT-WEEK LOOK-AHEAD" in block
    assert "2026-07-06" in block and "2026-07-12" in block
    # Sales-first framing (the core of the feature) is present.
    assert "prospect" in block.lower()
    assert "prep" in block.lower()
    # The past-is-past guard is retained verbatim.
    assert "STRICTLY BEFORE today as PAST" in block


def test_daily_block_unchanged_by_default():
    ctx = resolve_user_now(now_utc=_FRI, tz_name=PACIFIC)
    block = ctx.block()
    assert "Lookahead horizon: the next 72h" in block
    assert "NEXT-WEEK LOOK-AHEAD" not in block

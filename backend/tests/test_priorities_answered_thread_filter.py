"""An email the reader already answered must not come back as their to-do.

A common "not useful" rating is the complaint "I already responded to
this" — on a ``follow_up_debt``
card ("confirm the plan with stakeholders") and an ``approaching_deadline``
card ("ACTION NEEDED by 8/6"). Both rules keyed on ``direction=i_sent_last``,
which is as true of "they asked, I answered" as of "I asked, they went quiet".

Two gates, like the RSVP and temporal filters before them: the prompt now
distinguishes the two (``reader_last_message_kind``), and
:func:`_drop_answered_threads` holds when the model ignores it.
"""

from __future__ import annotations

from rubik_core.priorities.agents.decision_maker import (
    _META_RULES_PROMPT,
    ATTRS_PREVIEW_KEYS,
    _format_attrs_preview,
)
from rubik_core.priorities.models import (
    ChannelKind,
    ChannelSignal,
    EntityKind,
    PriorityCandidate,
    PriorityKind,
)
from rubik_core.priorities.orchestrator import _drop_answered_threads


def _thread(sig_id: str, **attrs) -> ChannelSignal:
    base = {"direction": "i_sent_last", "reader_replied": True}
    base.update(attrs)
    return ChannelSignal(
        id=sig_id,
        channel=ChannelKind.GMAIL,
        title="Xero to QB conversion",
        snippet="Let's hold for now.",
        entity_kind=EntityKind.THREAD,
        entity_id=sig_id,
        attrs=base,
    )


def _card(pid: str, kind: PriorityKind, *signal_ids: str) -> PriorityCandidate:
    return PriorityCandidate(
        id=pid,
        title="Clarify next steps for QuickBooks conversion",
        why_it_matters="Marked urgent.",
        urgency="medium",
        source_channels=[ChannelKind.GMAIL],
        signal_ids=list(signal_ids),
        priority_kind=kind,
    )


# ── the deterministic drop ───────────────────────────────────────────────────


def test_follow_up_debt_on_a_thread_the_reader_answered_is_dropped():
    sigs = {"t1": _thread("t1", reader_last_message_kind="answer")}
    kept, drops = _drop_answered_threads(
        [_card("p1", PriorityKind.FOLLOW_UP_DEBT, "t1")], sigs
    )
    assert kept == []
    assert len(drops) == 1 and "reader's own reply" in drops[0]


def test_approaching_deadline_on_an_answered_action_needed_email_is_dropped():
    sigs = {"t1": _thread("t1", reader_last_message_kind="answer")}
    kept, _ = _drop_answered_threads(
        [_card("p1", PriorityKind.APPROACHING_DEADLINE, "t1")], sigs
    )
    assert kept == []


def test_fyi_counts_as_closed_too():
    sigs = {"t1": _thread("t1", reader_last_message_kind="fyi")}
    kept, _ = _drop_answered_threads(
        [_card("p1", PriorityKind.FOLLOW_UP_DEBT, "t1")], sigs
    )
    assert kept == []


def test_a_thread_the_reader_is_actually_waiting_on_is_kept():
    """`ask` is the real follow-up case — I asked, they went quiet."""
    sigs = {"t1": _thread("t1", reader_last_message_kind="ask")}
    kept, drops = _drop_answered_threads(
        [_card("p1", PriorityKind.FOLLOW_UP_DEBT, "t1")], sigs
    )
    assert [p.id for p in kept] == ["p1"]
    assert drops == []


def test_they_sent_last_is_never_touched_here():
    sigs = {
        "t1": _thread("t1", direction="they_sent_last", reader_last_message_kind=None)
    }
    kept, _ = _drop_answered_threads(
        [_card("p1", PriorityKind.FOLLOW_UP_DEBT, "t1")], sigs
    )
    assert len(kept) == 1


def test_unknown_kind_keeps_the_card():
    """Never drop on absence of evidence — an older signal shape has no kind."""
    sigs = {"t1": _thread("t1")}
    kept, _ = _drop_answered_threads(
        [_card("p1", PriorityKind.FOLLOW_UP_DEBT, "t1")], sigs
    )
    assert len(kept) == 1


def test_a_card_with_one_live_signal_survives():
    """Cites an answered thread AND a calendar deadline → the deadline stands."""
    sigs = {
        "t1": _thread("t1", reader_last_message_kind="answer"),
        "ev1": ChannelSignal(
            id="ev1",
            channel=ChannelKind.CALENDAR,
            title="Board deck due",
            snippet="",
            entity_kind=EntityKind.EVENT,
            entity_id="ev1",
            attrs={},
        ),
    }
    kept, _ = _drop_answered_threads(
        [_card("p1", PriorityKind.APPROACHING_DEADLINE, "t1", "ev1")], sigs
    )
    assert len(kept) == 1


def test_commitment_made_inside_the_reply_is_untouched():
    """'I'll send the deck Friday' inside the answer is still a promise."""
    sigs = {"t1": _thread("t1", reader_last_message_kind="answer")}
    kept, _ = _drop_answered_threads(
        [_card("p1", PriorityKind.COMMITMENT_DEBT, "t1")], sigs
    )
    assert len(kept) == 1


def test_no_cited_signals_keeps_the_card():
    kept, _ = _drop_answered_threads([_card("p1", PriorityKind.FOLLOW_UP_DEBT)], {})
    assert len(kept) == 1


# ── the prompt half ──────────────────────────────────────────────────────────


def test_the_model_is_shown_the_reader_kind():
    """The rule can only bite if the preview renders the key it names."""
    for key in (
        "reader_last_message_kind",
        "reader_replied",
        "addressed_to_reader_last_message",
    ):
        assert key in ATTRS_PREVIEW_KEYS
    preview = _format_attrs_preview(
        {
            "direction": "i_sent_last",
            "reader_last_message_kind": "answer",
            "reader_replied": True,
        }
    )
    assert "direction=i_sent_last" in preview
    assert "reader_last_message_kind=answer" in preview


def test_follow_up_debt_rule_requires_an_open_ask():
    rule = _META_RULES_PROMPT[
        _META_RULES_PROMPT.index("(`follow_up_debt`)") : _META_RULES_PROMPT.index(
            "(`commitment_debt`)"
        )
    ]
    assert "reader_last_message_kind=ask" in rule
    assert "reader_last_message_kind=answer" in rule
    assert "never follow_up_debt" in rule


def test_approaching_deadline_rule_treats_the_reply_as_the_completed_work():
    rule = _META_RULES_PROMPT[
        _META_RULES_PROMPT.index("(`approaching_deadline`)") : _META_RULES_PROMPT.index(
            "(`pre_touchpoint_followup`)"
        )
    ]
    assert "reader_last_message_kind=answer" in rule
    assert "Never re-open" in rule


def test_who_spoke_last_guardrail_names_the_bulk_reminder_case():
    assert "WHO SPOKE LAST" in _META_RULES_PROMPT
    assert "addressed_to_reader_last_message=list" in _META_RULES_PROMPT

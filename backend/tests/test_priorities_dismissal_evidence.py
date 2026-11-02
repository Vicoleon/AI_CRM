"""A ✗ comes back only when something the card cites HAPPENS after it.

Regression: a dismissed card kept returning. A dismissal is
overruled by "new evidence", and the old check counted any cited signal with a
timestamp after the ✗, including timestamps that record nothing happening:

* a calendar signal is stamped with the meeting's START, so any card citing an
  upcoming meeting beat its own dismissal on every run until the meeting was
  over ("Dismiss just this one" on a prep card did nothing), and an all-day
  event's bare date read as midnight came back on the day itself;
* a HubSpot deal with no logged activity was stamped with the fetch time, so a
  card citing it looked brand new every morning.

What still counts, deliberately: real activity on ANY cited signal, not only the
one the card is keyed to. EOS and My To-Do signals carry their creation date and
the model keys a client conversation to the deal, so the card's own signal is
often the one that can never move.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from rubik_core.priorities import (
    ChannelKind,
    ChannelSignal,
    EntityKind,
    PriorityCandidate,
    PriorityKind,
)
from rubik_core.priorities.memory import STATE_DISMISSED, apply_post_filter

NOW = datetime(2026, 10, 26, 15, 0)  # naive UTC, like priority_memory
DISMISSED_AT = NOW - timedelta(days=1)


@dataclass
class _Row:
    """The MemoryRow fields the post-filter reads."""

    entity_kind: str
    entity_id: str
    priority_kind: str
    state: str = STATE_DISMISSED
    last_state_change_at: str = DISMISSED_AT.isoformat()
    snoozed_until: str | None = None


def _memory(kind: EntityKind, entity_id: str, pk: PriorityKind) -> dict:
    return {(kind.value, entity_id, pk.value): _Row(kind.value, entity_id, pk.value)}


def _signal(
    sig_id: str, channel: ChannelKind, kind: EntityKind, entity_id: str, ts: str
) -> ChannelSignal:
    return ChannelSignal(
        id=sig_id,
        channel=channel,
        title=sig_id,
        timestamp=ts,
        entity_kind=kind,
        entity_id=entity_id,
    )


def _card(
    kind: EntityKind, entity_id: str, pk: PriorityKind, signal_ids: list[str]
) -> PriorityCandidate:
    return PriorityCandidate(
        title="the card",
        urgency="medium",
        signal_ids=signal_ids,
        priority_kind=pk,
        entity_kind=kind,
        entity_id=entity_id,
    )


def _kept(card, memory, signals) -> list:
    kept, _ = apply_post_filter(
        [card], memory, now=NOW, signal_by_id={s.id: s for s in signals}
    )
    return kept


# ── a future timestamp is a schedule, not an arrival ─────────────────────────


def test_dismissing_one_meeting_prep_card_sticks_until_the_meeting():
    """The meeting is tomorrow, so its signal's timestamp is after the ✗. That
    used to read as new evidence on every run until the meeting was over."""
    event = _signal(
        "evt-1",
        ChannelKind.CALENDAR,
        EntityKind.EVENT,
        "evt-1",
        (NOW + timedelta(days=1)).isoformat(),
    )
    card = _card(EntityKind.EVENT, "evt-1", PriorityKind.UPCOMING_PREP, ["evt-1"])

    assert (
        _kept(
            card,
            _memory(EntityKind.EVENT, "evt-1", PriorityKind.UPCOMING_PREP),
            [event],
        )
        == []
    )


def test_a_message_that_landed_during_the_run_still_counts():
    """A source clock a little ahead of ours must not swallow a real reply."""
    reply = _signal(
        "s-reply",
        ChannelKind.GMAIL,
        EntityKind.THREAD,
        "thread-1",
        (NOW + timedelta(minutes=2)).isoformat(),
    )
    card = _card(
        EntityKind.THREAD, "thread-1", PriorityKind.FOLLOW_UP_DEBT, ["s-reply"]
    )

    assert (
        len(
            _kept(
                card,
                _memory(EntityKind.THREAD, "thread-1", PriorityKind.FOLLOW_UP_DEBT),
                [reply],
            )
        )
        == 1
    )


# ── real activity counts wherever it is cited ─────────────────────────────────


def test_supporting_upcoming_meeting_does_not_revive_a_dismissed_follow_up():
    """The exact shape of the reported bug: a follow-up citing the meeting it
    is due before came back every morning until that meeting happened."""
    thread = _signal(
        "s-thread",
        ChannelKind.GMAIL,
        EntityKind.THREAD,
        "thread-1",
        (DISMISSED_AT - timedelta(days=2)).isoformat(),
    )
    meeting = _signal(
        "evt-7",
        ChannelKind.CALENDAR,
        EntityKind.EVENT,
        "evt-7",
        (NOW + timedelta(days=2)).isoformat(),
    )
    card = _card(
        EntityKind.THREAD,
        "thread-1",
        PriorityKind.FOLLOW_UP_DEBT,
        ["s-thread", "evt-7"],
    )

    assert (
        _kept(
            card,
            _memory(EntityKind.THREAD, "thread-1", PriorityKind.FOLLOW_UP_DEBT),
            [thread, meeting],
        )
        == []
    )


def test_a_real_reply_on_the_dismissed_thread_still_brings_it_back():
    """The escape hatch the rule exists for: the item itself moved."""
    reply = _signal(
        "s-thread",
        ChannelKind.GMAIL,
        EntityKind.THREAD,
        "thread-1",
        (NOW - timedelta(hours=1)).isoformat(),
    )
    card = _card(
        EntityKind.THREAD, "thread-1", PriorityKind.FOLLOW_UP_DEBT, ["s-thread"]
    )

    assert (
        len(
            _kept(
                card,
                _memory(EntityKind.THREAD, "thread-1", PriorityKind.FOLLOW_UP_DEBT),
                [reply],
            )
        )
        == 1
    )


def test_an_offset_timestamp_is_compared_in_utc():
    """12:00+05:00 is 07:00 UTC, an hour BEFORE an 08:00 UTC dismissal. Dropping
    the offset made it look four hours after, i.e. new evidence."""
    dismissed = datetime(2026, 10, 26, 8, 0)
    reply = _signal(
        "s-thread",
        ChannelKind.GMAIL,
        EntityKind.THREAD,
        "thread-1",
        "2026-10-26T12:00:00+05:00",
    )
    memory = {
        (EntityKind.THREAD.value, "thread-1", PriorityKind.FOLLOW_UP_DEBT.value): _Row(
            EntityKind.THREAD.value,
            "thread-1",
            PriorityKind.FOLLOW_UP_DEBT.value,
            last_state_change_at=dismissed.isoformat(),
        )
    }
    card = _card(
        EntityKind.THREAD, "thread-1", PriorityKind.FOLLOW_UP_DEBT, ["s-thread"]
    )

    assert _kept(card, memory, [reply]) == []


def test_an_all_day_event_does_not_come_back_on_the_day_itself():
    """An all-day start is a bare date, read as midnight UTC: in the past by
    the time the event-day briefing runs, so it looked like new evidence."""
    day = _signal(
        "evt-offsite",
        ChannelKind.CALENDAR,
        EntityKind.EVENT,
        "evt-offsite",
        NOW.date().isoformat(),
    )
    card = _card(
        EntityKind.EVENT, "evt-offsite", PriorityKind.UPCOMING_PREP, ["evt-offsite"]
    )
    memory = _memory(EntityKind.EVENT, "evt-offsite", PriorityKind.UPCOMING_PREP)

    assert _kept(card, memory, [day]) == []


def test_a_meeting_that_has_started_does_not_revive_its_prep_card():
    """The start passing is the meeting happening on schedule, not news."""
    started = _signal(
        "evt-2",
        ChannelKind.CALENDAR,
        EntityKind.EVENT,
        "evt-2",
        (NOW - timedelta(minutes=10)).isoformat(),
    )
    card = _card(EntityKind.EVENT, "evt-2", PriorityKind.UPCOMING_PREP, ["evt-2"])
    memory = _memory(EntityKind.EVENT, "evt-2", PriorityKind.UPCOMING_PREP)

    assert _kept(card, memory, [started]) == []


def test_any_future_timestamp_is_not_evidence_yet():
    """Whatever the channel, a time later than now has not arrived."""
    ahead = _signal(
        "s-thread",
        ChannelKind.GMAIL,
        EntityKind.THREAD,
        "thread-1",
        (NOW + timedelta(days=2)).isoformat(),
    )
    card = _card(
        EntityKind.THREAD, "thread-1", PriorityKind.FOLLOW_UP_DEBT, ["s-thread"]
    )
    memory = _memory(EntityKind.THREAD, "thread-1", PriorityKind.FOLLOW_UP_DEBT)

    assert _kept(card, memory, [ahead]) == []


def test_a_client_reply_revives_a_card_keyed_to_the_deal():
    """The model keys a client conversation to the deal (deal > thread), and the
    deal's own stamp does not move on email. The reply is real news."""
    deal = _signal(
        "s-deal",
        ChannelKind.HUBSPOT,
        EntityKind.DEAL,
        "deal-9",
        (DISMISSED_AT - timedelta(days=10)).isoformat(),
    )
    reply = _signal(
        "s-thread",
        ChannelKind.GMAIL,
        EntityKind.THREAD,
        "thread-1",
        (NOW - timedelta(hours=2)).isoformat(),
    )
    card = _card(
        EntityKind.DEAL, "deal-9", PriorityKind.FOLLOW_UP_DEBT, ["s-deal", "s-thread"]
    )
    memory = _memory(EntityKind.DEAL, "deal-9", PriorityKind.FOLLOW_UP_DEBT)

    assert len(_kept(card, memory, [deal, reply])) == 1


def test_a_reply_revives_a_to_do_card_whose_own_stamp_never_moves():
    """EOS signals carry the To-Do's creation date, which can never be later
    than the dismissal. A reply on the thread the card cites still counts."""
    todo = _signal(
        "s-todo",
        ChannelKind.EOS,
        EntityKind.TODO,
        "todo-1",
        (DISMISSED_AT - timedelta(days=20)).date().isoformat(),
    )
    reply = _signal(
        "s-thread",
        ChannelKind.GMAIL,
        EntityKind.THREAD,
        "thread-1",
        (NOW - timedelta(hours=2)).isoformat(),
    )
    card = _card(
        EntityKind.TODO, "todo-1", PriorityKind.EOS_TODO_DUE, ["s-todo", "s-thread"]
    )
    memory = _memory(EntityKind.TODO, "todo-1", PriorityKind.EOS_TODO_DUE)

    assert len(_kept(card, memory, [todo, reply])) == 1


def test_a_to_do_card_with_nothing_new_stays_dismissed():
    todo = _signal(
        "s-todo",
        ChannelKind.EOS,
        EntityKind.TODO,
        "todo-1",
        (DISMISSED_AT - timedelta(days=20)).date().isoformat(),
    )
    card = _card(EntityKind.TODO, "todo-1", PriorityKind.EOS_TODO_DUE, ["s-todo"])
    memory = _memory(EntityKind.TODO, "todo-1", PriorityKind.EOS_TODO_DUE)

    assert _kept(card, memory, [todo]) == []

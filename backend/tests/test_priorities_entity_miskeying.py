"""A priority must be keyed to what it is ABOUT, not to whatever it cites.

Regression: a "Prepare for your Team L10 meeting" card cited
calendar + eos + drive signals, and `_reattach_eos_entities` stamped the FIRST
cited EOS signal's key over it — so a meeting-prep item wore
`todo/769abf3d…/eos_todo_due` while still carrying the calendar
`series_entity_id` that gave the mis-key away.

Two consequences, both live:
  * ticking the meeting card off marked an unrelated, still-open To-Do
    resolved — which is what poisoned that user's feed for 8+ days; and
  * the meeting-prep auto-resolve hook could never clear the card, because the
    card wasn't keyed to the event.
"""

from __future__ import annotations

from rubik_core.priorities.models import (
    ChannelKind,
    ChannelSignal,
    EntityKind,
    PriorityCandidate,
    PriorityKind,
)
from rubik_core.priorities.orchestrator import (
    _reattach_calendar_entities,
    _reattach_eos_entities,
)


def _eos_signal(sid, entity_id, kind="eos_todo_due", entity_kind=EntityKind.TODO):
    return ChannelSignal(
        id=sid,
        channel=ChannelKind.EOS,
        title="Overdue To-Do",
        entity_kind=entity_kind,
        entity_id=entity_id,
        entity_label="L10 import not working",
        attrs={"eos_priority_kind": kind, "is_own": True},
    )


def _cal_signal(sid="evt-1", entity_id="evt-1"):
    return ChannelSignal(
        id=sid,
        channel=ChannelKind.CALENDAR,
        title="Team L10",
        entity_kind=EntityKind.EVENT,
        entity_id=entity_id,
        entity_label="Team L10",
        attrs={},
    )


def _drive_signal(sid="doc-1"):
    return ChannelSignal(id=sid, channel=ChannelKind.DRIVE, title="L10 deck", attrs={})


def _index(*signals):
    return {s.id: s for s in signals}


# ── the hijack ───────────────────────────────────────────────────────────────


def test_meeting_prep_citing_an_eos_todo_keeps_its_own_key():
    """The canonical breakage."""
    cal, eos, drive = _cal_signal(), _eos_signal("eos-1", "todo-769"), _drive_signal()
    p = PriorityCandidate(
        title="Prepare for your Team L10 meeting",
        urgency="high",
        priority_kind=PriorityKind.UPCOMING_PREP,
        entity_kind=EntityKind.EVENT,
        entity_id="evt-1",
        signal_ids=["evt-1", "eos-1", "doc-1"],
    )

    _reattach_eos_entities([p], _index(cal, eos, drive))

    assert p.priority_kind == PriorityKind.UPCOMING_PREP
    assert p.entity_kind == EntityKind.EVENT
    assert p.entity_id == "evt-1", "a cited To-Do must not hijack a prep item's key"


def test_hijack_also_left_the_teammate_target_behind():
    """The overwrite set target_email too — that must not leak either."""
    cal = _cal_signal()
    eos = _eos_signal("eos-1", "todo-769")
    eos.attrs = {
        "eos_priority_kind": "eos_todo_due",
        "is_own": False,
        "owner_email": "someone.else@acme.example",
        "owner_name": "Someone Else",
    }
    p = PriorityCandidate(
        title="Prepare for your Team L10 meeting",
        urgency="high",
        priority_kind=PriorityKind.UPCOMING_PREP,
        entity_kind=EntityKind.EVENT,
        entity_id="evt-1",
        signal_ids=["evt-1", "eos-1"],
    )

    _reattach_eos_entities([p], _index(cal, eos))

    assert p.target_email is None, "a prep item must not be retargeted at a teammate"


# ── what must STILL be re-keyed ──────────────────────────────────────────────


def test_eos_kind_is_still_repaired_even_with_mixed_citations():
    """The original purpose: the model garbles the UUID, we fix it."""
    cal, eos = _cal_signal(), _eos_signal("eos-1", "todo-769")
    p = PriorityCandidate(
        title="Knock out the L10 import fix",
        urgency="high",
        priority_kind=PriorityKind.EOS_TODO_DUE,
        entity_kind=EntityKind.TODO,
        entity_id="todo-GARBLED",  # model mangled the UUID
        signal_ids=["evt-1", "eos-1"],
    )

    _reattach_eos_entities([p], _index(cal, eos))

    assert p.entity_id == "todo-769"
    assert p.priority_kind == PriorityKind.EOS_TODO_DUE


def test_all_eos_citations_are_rekeyed_even_when_the_model_mislabels_the_kind():
    """Only EOS evidence -> it can only be an EOS item, whatever kind was set."""
    eos = _eos_signal("eos-1", "todo-769")
    p = PriorityCandidate(
        title="Deadline Friday on the L10 import",
        urgency="high",
        priority_kind=PriorityKind.APPROACHING_DEADLINE,  # mislabeled
        entity_id="nonsense",
        signal_ids=["eos-1"],
    )

    _reattach_eos_entities([p], _index(eos))

    assert p.entity_id == "todo-769"
    assert p.priority_kind == PriorityKind.EOS_TODO_DUE


def test_sibling_coverage_still_records_every_todo():
    """Sibling coverage (covered_entity_ids) must survive the new gate."""
    a, b = _eos_signal("eos-1", "todo-1"), _eos_signal("eos-2", "todo-2")
    p = PriorityCandidate(
        title="Two overdue To-Dos",
        urgency="high",
        priority_kind=PriorityKind.EOS_TODO_DUE,
        signal_ids=["eos-1", "eos-2"],
    )

    _reattach_eos_entities([p], _index(a, b))

    assert p.entity_id == "todo-1"
    assert p.covered_entity_ids == ["todo-1", "todo-2"]


# ── the calendar half ────────────────────────────────────────────────────────


def test_garbled_event_id_on_a_prep_item_is_repaired():
    """Stopping the hijack isn't enough — the event id must also be right."""
    cal = _cal_signal(sid="evt-real", entity_id="evt-real")
    p = PriorityCandidate(
        title="Prepare for your Team L10 meeting",
        urgency="high",
        priority_kind=PriorityKind.UPCOMING_PREP,
        entity_kind=EntityKind.EVENT,
        entity_id="evt-GARBLED",
        signal_ids=["evt-real"],
    )

    _reattach_calendar_entities([p], _index(cal))

    assert p.entity_id == "evt-real"
    assert p.entity_kind == EntityKind.EVENT


def test_calendar_repair_ignores_non_prep_kinds():
    """A follow-up is ABOUT a thread; an upcoming meeting is only its anchor."""
    cal = _cal_signal()
    p = PriorityCandidate(
        title="Send the recap before Thursday's call",
        urgency="medium",
        priority_kind=PriorityKind.PRE_TOUCHPOINT_FOLLOWUP,
        entity_kind=EntityKind.THREAD,
        entity_id="thread-9",
        signal_ids=["evt-1"],
    )

    _reattach_calendar_entities([p], _index(cal))

    assert p.entity_kind == EntityKind.THREAD
    assert p.entity_id == "thread-9"


def test_calendar_repair_is_a_no_op_without_a_cited_calendar_signal():
    p = PriorityCandidate(
        title="Prep something",
        urgency="medium",
        priority_kind=PriorityKind.UPCOMING_PREP,
        entity_kind=EntityKind.EVENT,
        entity_id="evt-whatever",
        signal_ids=["doc-1"],
    )

    _reattach_calendar_entities([p], _index(_drive_signal()))

    assert p.entity_id == "evt-whatever"


def test_the_two_repairs_do_not_fight_over_a_prep_item():
    """End state of the real card: prep item keyed to the EVENT, not the To-Do."""
    cal, eos, drive = (
        _cal_signal(sid="evt-real", entity_id="evt-real"),
        _eos_signal("eos-1", "todo-769"),
        _drive_signal(),
    )
    p = PriorityCandidate(
        title="Prepare for your Team L10 meeting",
        urgency="high",
        priority_kind=PriorityKind.UPCOMING_PREP,
        entity_kind=EntityKind.EVENT,
        entity_id="evt-GARBLED",
        signal_ids=["evt-real", "eos-1", "doc-1"],
    )
    index = _index(cal, eos, drive)

    _reattach_eos_entities([p], index)
    _reattach_calendar_entities([p], index)

    assert p.priority_kind == PriorityKind.UPCOMING_PREP
    assert p.entity_kind == EntityKind.EVENT
    assert p.entity_id == "evt-real"

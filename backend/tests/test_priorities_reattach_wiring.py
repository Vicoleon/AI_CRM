"""The deterministic-keying passes must RUN, not merely exist.

Three helpers repair the opaque entity ids the decision-maker LLM garbles:

* ``_reattach_eos_entities``     — EOS row UUIDs
* ``_reattach_promise_entities`` — My To-Dos promise ids
* ``_reattach_calendar_entities`` — Google event ids

Every other test in this repo calls those helpers **directly**, which proves
they work but proves nothing about whether the pipeline still calls them. That
gap has already cost this project once: `_reattach_calendar_entities`
and its call sites were deleted wholesale by a refactor and the whole
priorities suite stayed green. The one wiring guard that existed was a
``inspect.getsource`` substring check over the orchestrator class — which the
*revision-block* copies of the same calls satisfy, so deleting the three
main-flow call sites was still invisible.

The stakes are not cosmetic. Without these passes every EOS row UUID, promise
id and event id reaches the card exactly as the model garbled it, so:

* the UC-25 memory key ``(user, entity_kind, entity_id, priority_kind)`` never
  matches anything — ✓ / dismiss / snooze silently do nothing;
* the auto-resolve hooks can never clear a card;
* on the promises channel ✓ marks a To-Do kept that does not exist.

So this file drives the REAL ``PrioritiesOrchestrator.generate`` — stub channel
agents, stub LLM clients, no network, no DB — and asserts the canonical keys
that only those three passes can produce.

Two shapes, because the passes are wired in two places:

1. ``test_the_main_flow_...`` — ``max_revisions=0`` so the revision block is
   structurally unreachable. Deleting any of the three main-flow calls
   turns this red.
2. ``test_the_revision_merge_...`` — the revision block's own six calls exist
   so that a rewritten item is matched to its first-pass self *before*
   ``_merge_revision`` compares entity triples. Deleting them makes a revision
   look like a brand-new item, and the original is restored beside it as a
   duplicate of the same entity while a genuinely different card is dropped.
   The main-flow passes run afterwards and re-canonicalize both, so only a
   duplicate-detection assertion catches this — an end-state id check cannot.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from rubik_core.priorities import (
    PrioritiesConfig,
    PrioritiesOrchestrator,
    PrioritiesRequest,
)
from rubik_core.priorities.models import (
    ChannelDigest,
    ChannelKind,
    ChannelSignal,
    EntityKind,
)

# ── canonical ids the signals carry (what the pipeline MUST end up with) ─────

EOS_TODO_ID = str(uuid.uuid4())
PROMISE_ID = str(uuid.uuid4())
EVENT_ID = "evt_2c9f1a7b4d8e_20260813T160000Z"
THREAD_ID = "thread-acme-renewal"

# ── what the LLM emits instead (the garble this pipeline exists to repair) ───

GARBLED_TODO = "todo-769abf3d-not-a-real-uuid"
GARBLED_PROMISE = "a1b2c3d4-oops"
GARBLED_EVENT = "evt_2c9f1a7b4d8e"  # truncated — a real Google id, mangled

# A meeting ~1 day out so the forward-looking temporal backstop keeps the prep
# card no matter when the suite runs.
_FUTURE_START = (datetime.now(timezone.utc) + timedelta(days=1)).strftime(
    "%Y-%m-%dT14:00:00Z"
)


# ── stubs ────────────────────────────────────────────────────────────────────


class _StubLLM:
    """Drop-in for LLMJsonClient. Canned JSON, no network.

    A list payload is consumed one call at a time, which is how the revision
    flow gets a different answer on its second decision-maker call.
    """

    def __init__(self, payload):
        self.payload = payload
        self.calls: list[str] = []

    def invoke_json(self, prompt: str, fallback=None, **_kwargs):
        self.calls.append(prompt)
        if isinstance(self.payload, list):
            if not self.payload:
                return fallback if fallback is not None else {}
            return self.payload.pop(0)
        return self.payload


def _agent_class(channel: ChannelKind, signals: list[ChannelSignal]):
    """A channel agent the orchestrator can construct and run for real.

    The orchestrator instantiates registry entries with
    ``(user_email=, config=, temporal=)`` and calls ``.run()`` in a thread pool,
    then reads ``.channel`` off both the agent and the digest.
    """

    class _Agent:
        def __init__(self, **_kwargs):
            self.channel = channel

        def run(self) -> ChannelDigest:
            return ChannelDigest(
                channel=channel,
                narrative=f"{channel.value}: {len(signals)} signal(s)",
                signals=list(signals),
            )

    return _Agent


def _calendar_signal() -> ChannelSignal:
    return ChannelSignal(
        id="cal-1",
        channel=ChannelKind.CALENDAR,
        title="Team L10",
        entity_kind=EntityKind.EVENT,
        entity_id=EVENT_ID,
        entity_label="Team L10",
        # start_iso is the anchor _drop_past_anchored reads; no `rsvp` key means
        # the reader accepted, so _drop_unaccepted_prep keeps the card too.
        attrs={"start_iso": _FUTURE_START},
    )


def _eos_signal() -> ChannelSignal:
    return ChannelSignal(
        id="eos-1",
        channel=ChannelKind.EOS,
        title="Overdue To-Do: L10 import not working",
        entity_kind=EntityKind.TODO,
        entity_id=EOS_TODO_ID,
        entity_label="L10 import not working",
        attrs={"eos_priority_kind": "eos_todo_due", "is_own": True},
    )


def _promise_signal() -> ChannelSignal:
    return ChannelSignal(
        id="promise-1",
        channel=ChannelKind.PROMISES,
        title="[Your To-Do] Send the Q3 deck",
        entity_kind=EntityKind.PROMISE,
        entity_id=PROMISE_ID,
        entity_label="Send the Q3 deck",
        attrs={"promise_priority_kind": "promise_due", "is_own": True},
    )


def _gmail_signal() -> ChannelSignal:
    """Filler evidence no reattachment pass touches."""
    return ChannelSignal(
        id="mail-1",
        channel=ChannelKind.GMAIL,
        title="Re: Acme renewal",
        entity_kind=EntityKind.THREAD,
        entity_id=THREAD_ID,
        entity_label="Acme renewal",
        attrs={},
    )


def _item(title, kind, entity_kind, entity_id, signal_ids, urgency="high"):
    return {
        "title": title,
        "why_it_matters": "Because the pipeline says so.",
        "urgency": urgency,
        "suggested_action": "Do the thing",
        "signal_ids": signal_ids,
        "priority_kind": kind,
        "entity_kind": entity_kind,
        "entity_id": entity_id,
    }


_GOOD_CRITIQUE = {
    "overall_score": 9.0,
    "missing_signals": [],
    "weak_rationales": [],
    "suggestions": [],
    "raw_feedback": "Solid.",
}


def _wire(monkeypatch, orch, *, channels, decision_payload, critique=_GOOD_CRITIQUE):
    """Point the orchestrator at stub channel agents and stub LLM clients."""
    monkeypatch.setattr(
        "rubik_core.priorities.orchestrator.CHANNEL_AGENT_REGISTRY",
        {kind: _agent_class(kind, sigs) for kind, sigs in channels.items()},
    )
    orch.summarizer.client = _StubLLM(
        {"narrative": "A day with work in it.", "cross_channel_themes": []}
    )
    orch.decision_maker.client = _StubLLM(decision_payload)
    orch.critic.client = _StubLLM(critique)


@pytest.fixture(autouse=True)
def _no_db_or_network(monkeypatch):
    """Keep the run hermetic without stubbing anything on the keying path.

    UC-25 memory persistence and PTO detection both reach outside the process.
    Both are already best-effort in ``generate``, but relying on a swallowed
    exception to get there is environment-dependent — and ``record_seen`` would
    write rows to whatever database the process happens to be bound to. So they
    are replaced by their honest empty answers rather than left to fail:
    ``detect_pto`` returns a real, nobody-is-away ``PtoStatus`` so the PTO
    reframing and the pre-vacation handoff both run for real and both decline.

    ``apply_post_filter`` is deliberately NOT stubbed: it keys on entity_id, so
    it stays real and empty-memory (a pass-through).
    """
    import rubik_core.priorities.memory as mem
    from rubik_core.availability.pto import PtoStatus

    monkeypatch.setattr(mem, "load_for_run", lambda *_a, **_k: {})
    monkeypatch.setattr(mem, "record_seen", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "rubik_core.availability.pto.detect_pto",
        lambda user_email, **_k: PtoStatus(
            user_email=user_email,
            on_pto_today=False,
            current=None,
            upcoming=(),
        ),
    )


def _by_title(bundle, prefix):
    matches = [p for p in bundle.priorities if (p.title or "").startswith(prefix)]
    assert matches, (
        f"no priority titled {prefix!r} survived — got "
        f"{[p.title for p in bundle.priorities]}"
    )
    return matches[0]


# ── 1. the main flow ─────────────────────────────────────────────────────────


def test_the_main_flow_canonicalizes_all_three_channels_entity_ids(monkeypatch):
    """Delete any of the three main-flow ``_reattach_*`` calls → this goes red.

    ``max_revisions=0`` makes the revision block unreachable, so the six copies
    of these calls inside it cannot stand in for the main-flow ones. That is the
    whole point: the previous wiring guard was satisfied by those copies while
    the ordinary generation path did no deterministic keying at all.
    """
    config = PrioritiesConfig.default()
    config.max_revisions = 0
    orch = PrioritiesOrchestrator(config=config)

    _wire(
        monkeypatch,
        orch,
        channels={
            ChannelKind.CALENDAR: [_calendar_signal()],
            ChannelKind.EOS: [_eos_signal()],
            ChannelKind.PROMISES: [_promise_signal()],
        },
        decision_payload={
            "priorities": [
                _item(
                    "PREP: Team L10",
                    "upcoming_prep",
                    "event",
                    GARBLED_EVENT,
                    ["cal-1"],
                ),
                _item(
                    "EOS: finish the L10 import To-Do",
                    "eos_todo_due",
                    "todo",
                    GARBLED_TODO,
                    ["eos-1"],
                ),
                _item(
                    "TODO: send the Q3 deck",
                    "promise_due",
                    "promise",
                    GARBLED_PROMISE,
                    ["promise-1"],
                ),
            ]
        },
    )

    bundle = orch.generate(
        PrioritiesRequest(
            user_email="bellison@acme.example",
            channels=[ChannelKind.CALENDAR, ChannelKind.EOS, ChannelKind.PROMISES],
        )
    )

    assert bundle.revision_count == 0, "the revision block must not have run"

    eos = _by_title(bundle, "EOS:")
    assert eos.entity_id == EOS_TODO_ID, (
        "_reattach_eos_entities is not running in the main pipeline flow — the "
        "EOS row UUID reached the card exactly as the LLM garbled it, so no ✓ "
        "or dismissal on this card will ever match its memory row"
    )
    assert eos.entity_kind == EntityKind.TODO

    todo = _by_title(bundle, "TODO:")
    assert todo.entity_id == PROMISE_ID, (
        "_reattach_promise_entities is not running in the main pipeline flow — "
        "the card's ✓ would mark kept a To-Do that does not exist"
    )
    assert todo.entity_kind == EntityKind.PROMISE

    prep = _by_title(bundle, "PREP:")
    assert prep.entity_id == EVENT_ID, (
        "_reattach_calendar_entities is not running in the main pipeline flow — "
        "this is the exact deletion that shipped green once already"
    )
    assert prep.entity_kind == EntityKind.EVENT


def test_the_main_flow_repairs_the_kind_the_model_mislabelled(monkeypatch):
    """The passes restore ``priority_kind`` too, not just the id.

    ``priority_kind`` is half the memory key and it encodes WHICH situation the
    card is about. A card the model labelled ``neglect_debt`` that is really a
    To-Do coming due would write its ✓ against the wrong tuple, so the same
    To-Do's genuine card is what goes dark.
    """
    from rubik_core.priorities.models import PriorityKind

    config = PrioritiesConfig.default()
    config.max_revisions = 0
    orch = PrioritiesOrchestrator(config=config)

    _wire(
        monkeypatch,
        orch,
        channels={
            ChannelKind.EOS: [_eos_signal()],
            ChannelKind.PROMISES: [_promise_signal()],
        },
        decision_payload={
            "priorities": [
                _item(
                    "EOS: finish the L10 import To-Do",
                    "follow_up_debt",  # wrong — the agent decided eos_todo_due
                    "deal",  # wrong
                    GARBLED_TODO,
                    ["eos-1"],
                    urgency="medium",
                ),
                _item(
                    "TODO: send the Q3 deck",
                    "neglect_debt",  # wrong — the agent decided promise_due
                    "deal",  # wrong
                    GARBLED_PROMISE,
                    ["promise-1"],
                    urgency="medium",
                ),
            ]
        },
    )

    bundle = orch.generate(
        PrioritiesRequest(
            user_email="bellison@acme.example",
            channels=[ChannelKind.EOS, ChannelKind.PROMISES],
        )
    )

    assert _by_title(bundle, "EOS:").priority_kind == PriorityKind.EOS_TODO_DUE
    assert _by_title(bundle, "TODO:").priority_kind == PriorityKind.PROMISE_DUE


# ── 2. the revision merge ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "channel_kind,signal,kind,entity_kind,garble_a,garble_b,canonical,helper",
    [
        (
            ChannelKind.EOS,
            _eos_signal(),
            "eos_todo_due",
            "todo",
            GARBLED_TODO,
            "todo-769abf3d-DIFFERENT-garble",
            EOS_TODO_ID,
            "_reattach_eos_entities",
        ),
        (
            ChannelKind.PROMISES,
            _promise_signal(),
            "promise_due",
            "promise",
            GARBLED_PROMISE,
            "a1b2c3d4-DIFFERENT-oops",
            PROMISE_ID,
            "_reattach_promise_entities",
        ),
        (
            ChannelKind.CALENDAR,
            _calendar_signal(),
            "upcoming_prep",
            "event",
            GARBLED_EVENT,
            "evt_2c9f1a7b4d8e_DIFFERENT",
            EVENT_ID,
            "_reattach_calendar_entities",
        ),
    ],
    ids=["eos", "promises", "calendar"],
)
def test_the_revision_merge_matches_a_rewritten_item_to_its_first_pass_self(
    monkeypatch,
    channel_kind,
    signal,
    kind,
    entity_kind,
    garble_a,
    garble_b,
    canonical,
    helper,
):
    """Delete the revision block's ``_reattach_*`` calls → this goes red.

    The model garbles the SAME opaque id differently on each pass. ``_merge_revision``
    compares ``(entity_kind, entity_id, priority_kind)``, so without
    canonicalization the rewritten card looks brand-new: the first-pass original
    is restored beside it as a second card for one entity, and a genuinely
    different card is pushed out of the gap it filled.

    An end-state ``entity_id`` assertion cannot catch this — the main-flow
    passes run after the merge and canonicalize both copies. Only the duplicate
    is visible.
    """
    config = PrioritiesConfig.default()
    config.max_revisions = 1
    config.revision_threshold = 7.0
    orch = PrioritiesOrchestrator(config=config)

    first_pass = {
        "priorities": [
            _item(
                "TARGET: the original wording",
                kind,
                entity_kind,
                garble_a,
                [signal.id],
            ),
            _item(
                "FILLER: reply to Acme about the renewal",
                "follow_up_debt",
                "thread",
                THREAD_ID,
                ["mail-1"],
                urgency="medium",
            ),
        ]
    }
    # The revision rewrites the target and silently drops the filler — the
    # shrink is what makes _merge_revision reach for the first pass at all.
    revised = {
        "priorities": [
            _item(
                "TARGET: a sharper rewording",
                kind,
                entity_kind,
                garble_b,
                [signal.id],
            )
        ]
    }

    _wire(
        monkeypatch,
        orch,
        channels={
            channel_kind: [signal],
            ChannelKind.GMAIL: [_gmail_signal()],
        },
        decision_payload=[first_pass, revised],
        # Below the 7.0 threshold, so the critic demands exactly one revision.
        critique={
            "overall_score": 5.0,
            "missing_signals": [],
            "weak_rationales": ["vague"],
            "suggestions": ["sharpen it"],
            "raw_feedback": "Needs work.",
        },
    )

    bundle = orch.generate(
        PrioritiesRequest(
            user_email="bellison@acme.example",
            channels=[channel_kind, ChannelKind.GMAIL],
        )
    )

    assert bundle.revision_count == 1, (
        "this test only means something if the revision block actually ran"
    )

    targets = [p for p in bundle.priorities if p.entity_id == canonical]
    assert len(targets) == 1, (
        f"{helper} is not running on BOTH passes inside the revision merge — "
        f"one entity came back as {len(targets)} cards "
        f"({[p.title for p in targets]}), because the rewritten item did not "
        "match its first-pass self and the original was restored beside it"
    )
    assert targets[0].title == "TARGET: a sharper rewording", (
        "the revision's improved wording must be the copy that survives"
    )
    assert any(p.entity_id == THREAD_ID for p in bundle.priorities), (
        "the duplicate consumed the restore budget, so the one card the "
        "revision really did drop was lost"
    )

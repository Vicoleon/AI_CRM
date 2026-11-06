"""Which signals the decision-maker sees must not depend on API timing.

The bug: the orchestrator appends channel digests in COMPLETION order
(``concurrent.futures.as_completed``), the summarizer concatenated them in that
order, and the decision-maker then read ``all_signals[:60]`` (the critic
``[:40]``). On a busy day whichever channel's API answered first took the
slots, so the same inbox could produce a different briefing on a different
morning, and a slow channel could be cut entirely.

Now the summarizer deals channels round-robin in canonical ``ChannelKind``
order, each channel keeping its own best-first ranking. These tests hold that
order stable across every completion order, fair across channels of very
different sizes, and end to end through ``PrioritiesOrchestrator.generate``.

Pure logic — agents and LLMs are stubs. No GCP / LLM / DB.
"""

from __future__ import annotations

import itertools
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from rubik_core.priorities import (
    ChannelKind,
    ChannelSignal,
    PrioritiesConfig,
    PrioritiesOrchestrator,
    PrioritiesRequest,
    PriorityCandidate,
)
from rubik_core.priorities.agents import (
    CriticAgent,
    GmailChannelAgent,
    SummarizerAgent,
)
from rubik_core.priorities.agents.critic import CRITIC_SIGNAL_CAP
from rubik_core.priorities.agents.summarizer import interleave_signals
from rubik_core.priorities.models import ChannelDigest, ConsolidatedDigest

USER = "me@acme.example"


class _StubLLM:
    def __init__(self, payload):
        self.payload = payload
        self.calls: list[str] = []

    def invoke_json(self, prompt, fallback=None, **_kw):
        self.calls.append(prompt)
        return self.payload


def _sig(channel: ChannelKind, n: int) -> ChannelSignal:
    return ChannelSignal(
        id=f"{channel.value}-{n}",
        channel=channel,
        title=f"{channel.value} item {n}",
        attrs={},
    )


def _digest(channel: ChannelKind, count: int) -> ChannelDigest:
    return ChannelDigest(
        channel=channel,
        narrative=f"{channel.value}: {count}",
        signals=[_sig(channel, n) for n in range(count)],
    )


_SIZES = {
    ChannelKind.SLACK: 4,
    ChannelKind.HUBSPOT: 7,
    ChannelKind.GMAIL: 5,
    ChannelKind.EOS: 2,
}


def _consolidate(digests):
    summarizer = SummarizerAgent(PrioritiesConfig.default())
    summarizer.client = _StubLLM({"narrative": "n", "cross_channel_themes": []})
    consolidated = summarizer.consolidate(digests)
    return [s.id for s in consolidated.all_signals], summarizer.client.calls[0]


# ── the summarizer ───────────────────────────────────────────────────────────


def test_the_signal_list_is_identical_for_every_completion_order():
    digests = [_digest(kind, n) for kind, n in _SIZES.items()]
    orders = {
        tuple(_consolidate(list(perm))[0]) for perm in itertools.permutations(digests)
    }
    assert len(orders) == 1, (
        f"{len(orders)} different signal orders across the "
        f"{len(list(itertools.permutations(digests)))} completion orders"
    )


def test_the_summarizer_prompt_is_identical_for_every_completion_order():
    """The per-channel reports used to follow completion order as well, so the
    narrative the decision-maker reads could shift with timing too."""
    digests = [_digest(kind, n) for kind, n in _SIZES.items()]
    prompts = {_consolidate(list(perm))[1] for perm in itertools.permutations(digests)}
    assert len(prompts) == 1


def test_channels_are_dealt_round_robin_in_canonical_order():
    ids, _ = _consolidate(
        [_digest(ChannelKind.SLACK, 2), _digest(ChannelKind.GMAIL, 3)]
    )
    # ChannelKind lists GMAIL before SLACK, so Gmail leads each round.
    assert ids == ["gmail-0", "slack-0", "gmail-1", "slack-1", "gmail-2"]


def test_each_channel_keeps_its_own_ranking():
    digests = [_digest(kind, n) for kind, n in _SIZES.items()]
    merged = interleave_signals(digests)
    for kind, n in _SIZES.items():
        own = [s.id for s in merged if s.channel == kind]
        assert own == [f"{kind.value}-{i}" for i in range(n)]


def test_a_busy_channel_cannot_crowd_quiet_ones_out_of_the_decision_makers_view():
    """The decision-maker reads the first 60. Seventy deals that finished first
    used to take all 60, and the reader's two EOS items never reached it."""
    from rubik_core.priorities.agents import DecisionMakerAgent

    digests = [_digest(ChannelKind.HUBSPOT, 70), _digest(ChannelKind.EOS, 2)]
    summarizer = SummarizerAgent(PrioritiesConfig.default())
    summarizer.client = _StubLLM({"narrative": "n", "cross_channel_themes": []})
    consolidated = summarizer.consolidate(digests)

    prompt = DecisionMakerAgent(PrioritiesConfig.default())._build_prompt(
        consolidated,
        max_priorities=5,
        focus=None,
        entity_index={},
        department=None,
        playbook_text="",
    )
    assert "id=eos-0 " in prompt and "id=eos-1 " in prompt


# ── the critic ───────────────────────────────────────────────────────────────


def test_the_critic_is_shown_every_signal_a_priority_cites():
    """The critic listed ``all_signals[:40]`` while the decision-maker reads 60,
    so a card citing signal #55 was reviewed by a stage that could not see it."""
    signals = [_sig(ChannelKind.HUBSPOT, n) for n in range(60)]
    digest = ConsolidatedDigest(narrative="n", all_signals=signals)
    card = PriorityCandidate(title="Deal 55", signal_ids=["hubspot-55"])

    prompt = CriticAgent(PrioritiesConfig.default())._build_prompt(digest, [card])

    listed = [line for line in prompt.splitlines() if line.startswith("- id=")]
    assert any(line.startswith("- id=hubspot-55 ") for line in listed)
    assert len(listed) == CRITIC_SIGNAL_CAP


# ── Gmail lists what it reserves where a cap cannot cut it ───────────────────


def _gmail_thread(tid: str, *, sender: str, to: str, idle_days: int) -> dict:
    sent = datetime.now(timezone.utc) - timedelta(days=idle_days, hours=1)
    return {
        "id": tid,
        "messages": [
            {
                "id": f"{tid}-m",
                "internalDate": str(int(sent.timestamp() * 1000)),
                "snippet": "…",
                "labelIds": [],
                "payload": {
                    "headers": [
                        {"name": "From", "value": sender},
                        {"name": "To", "value": to},
                        {"name": "Subject", "value": tid},
                    ]
                },
            }
        ],
    }


def test_gmail_lists_its_reserved_silences_near_the_top():
    """Six slots are reserved for the stalest threads the reader spoke last on
    ("who owes me a reply"). Sorted by idle days they sat at the END of
    the list, the first thing a round-robin cap removes."""
    fresh = [
        _gmail_thread(f"in-{d}", sender="client@acmecorp.example", to=USER, idle_days=d)
        for d in range(20)
    ]
    silent = [
        _gmail_thread(
            f"out-{d}", sender=USER, to="client@acmecorp.example", idle_days=d
        )
        for d in range(21, 29)
    ]
    agent = GmailChannelAgent(user_email=USER, config=PrioritiesConfig.default())
    ranked = agent.parse_signals(fresh + silent)

    assert len(ranked) == agent.MAX_SIGNALS_OUT
    stalest_six = [f"out-{d}" for d in range(28, 22, -1)]
    # Alternating freshest-ask / stalest-silence: any prefix carries both.
    assert [s.id for s in ranked[1:12:2]] == stalest_six
    assert [s.id for s in ranked[0:12:2]] == [f"in-{d}" for d in range(6)]


# ── end to end: however the channels finish, the decision-maker sees the same


def _gated_registry(sizes: dict, finish_order: list):
    """Channel agents that finish in exactly ``finish_order``."""
    finished = {kind: threading.Event() for kind in finish_order}

    def _agent_for(kind):
        class _Agent:
            def __init__(self, **_kw):
                self.channel = kind

            def run(self):
                position = finish_order.index(kind)
                if position:
                    assert finished[finish_order[position - 1]].wait(timeout=10)
                    # Let the previous future be marked done before this one.
                    time.sleep(0.05)
                digest = _digest(kind, sizes[kind])
                finished[kind].set()
                return digest

        return _Agent

    return {kind: _agent_for(kind) for kind in sizes}


@pytest.fixture
def _hermetic(monkeypatch):
    import rubik_core.priorities.memory as mem
    from rubik_core.availability.pto import PtoStatus

    monkeypatch.setattr(mem, "load_for_run", lambda *_a, **_k: {})
    monkeypatch.setattr(mem, "record_seen", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "rubik_core.availability.pto.detect_pto",
        lambda user_email, **_k: PtoStatus(
            user_email=user_email, on_pto_today=False, current=None, upcoming=()
        ),
    )


def _decision_view(monkeypatch, finish_order):
    config = PrioritiesConfig.default()
    config.max_revisions = 0
    orch = PrioritiesOrchestrator(config=config)
    monkeypatch.setattr(
        "rubik_core.priorities.orchestrator.CHANNEL_AGENT_REGISTRY",
        _gated_registry(_SIZES, finish_order),
    )
    orch.summarizer.client = _StubLLM({"narrative": "n", "cross_channel_themes": []})
    orch.decision_maker.client = _StubLLM(
        {
            "priorities": [
                {
                    "title": "Reply to Acme",
                    "why_it_matters": "x",
                    "urgency": "medium",
                    "suggested_action": "reply",
                    "signal_ids": ["gmail-0", "hubspot-0"],
                }
            ]
        }
    )
    orch.critic.client = _StubLLM(
        {
            "overall_score": 9.0,
            "missing_signals": [],
            "weak_rationales": [],
            "suggestions": [],
            "raw_feedback": "",
        }
    )
    bundle = orch.generate(PrioritiesRequest(user_email=USER, channels=list(_SIZES)))
    prompt = orch.decision_maker.client.calls[0]
    finished = [
        line.split()[1]
        for line in bundle.audit_trail
        if line.startswith("channel ") and "signals" in line
    ]
    return prompt[prompt.index("## SIGNALS") :], finished


def test_the_decision_maker_sees_the_same_signals_however_the_channels_finish(
    monkeypatch, _hermetic
):
    forward = list(_SIZES)
    backward = list(reversed(forward))

    view_a, finished_a = _decision_view(monkeypatch, forward)
    view_b, finished_b = _decision_view(monkeypatch, backward)

    # The test only means something if the completion order really differed.
    assert finished_a == [k.value for k in forward]
    assert finished_b == [k.value for k in backward]
    assert view_a == view_b

"""The critic's deterministic backstop must modulate the score, not become it.

Measured over one live week: mean critic score **4.61**, with **14
runs at exactly 0.0** — the `max(0.0, …)` clamp firing because
`BLOCKER_PENALTY` (1.5 per unsupported priority) was uncapped and could
subtract 7.5 from a 0-10 scale. 93% of runs came back `needs_revision=True`,
so the revision pass was the default path rather than an exception.

Also pins the revision LOOP: `max_revisions` was read as a boolean (`> 0`), so
an admin who set 3 silently got exactly 1. It is now a real bound, each round
is re-scored, and a round that does not improve the score ends the loop.

This work was written in August on `claude/priorities-eos-miskeying` and never
committed; it is ported onto main here, with the revision block's EOS / ticket
/ calendar re-keying and `_merge_revision` left exactly as they were.

Pure logic — agents and LLMs are stubs. No GCP / LLM / DB.
"""

from __future__ import annotations

import pytest

from rubik_core.priorities import (
    ChannelKind,
    PrioritiesConfig,
    PrioritiesOrchestrator,
    PrioritiesRequest,
)
from rubik_core.priorities.agents.critic import CriticAgent
from rubik_core.priorities.models import (
    ChannelDigest,
    ChannelSignal,
    ConsolidatedDigest,
    PriorityCandidate,
)

# ── the penalty cap ──────────────────────────────────────────────────────────


class _Stub:
    def __init__(self, payload):
        self.payload = payload
        self.calls: list[str] = []

    def invoke_json(self, prompt, fallback=None, **kw):
        self.calls.append(prompt)
        return self.payload


def _digest(n_signals=1):
    sigs = [
        ChannelSignal(id=f"s{i}", channel=ChannelKind.GMAIL, title=f"t{i}", attrs={})
        for i in range(n_signals)
    ]
    return ConsolidatedDigest(
        narrative="n",
        cross_channel_themes=[],
        all_signals=sigs,
        per_channel_summaries={},
    )


def _unsupported(n):
    """n priorities that all fail the blocker (1 signal, no date anchor)."""
    return [
        PriorityCandidate(
            title=f"p{i}", urgency="medium", why_it_matters="w", signal_ids=["s0"]
        )
        for i in range(n)
    ]


def _critique(score, feedback=""):
    return {
        "overall_score": score,
        "missing_signals": [],
        "weak_rationales": [],
        "suggestions": [],
        "raw_feedback": feedback,
    }


def _critic(score, threshold=6.0):
    cfg = PrioritiesConfig.default()
    cfg.revision_threshold = threshold
    c = CriticAgent(cfg)
    c.client = _Stub(_critique(score))
    return c


def test_blocker_penalty_is_capped_and_cannot_zero_a_good_score():
    """5 failing items used to cost 7.5 — enough to clamp a strong list to 0."""
    critic = _critic(8.0)
    report = critic.review(_digest(), _unsupported(5))

    # Uncapped this was 8.0 - 7.5 = 0.5; capped it is 8.0 - 3.0.
    assert report.overall_score == 5.0
    assert report.overall_score >= 0.0


def test_one_failing_item_still_costs_the_full_per_item_penalty():
    """Under the cap, the per-item rate is unchanged — the backstop still bites."""
    critic = _critic(8.0)
    report = critic.review(_digest(), _unsupported(1))
    assert report.overall_score == 6.5  # 8.0 - 1.5


def test_the_cap_does_not_rescue_a_wholly_unsupported_weak_list():
    """A weak list still fails the gate — the backstop must keep working."""
    critic = _critic(5.0, threshold=6.0)
    report = critic.review(_digest(), _unsupported(4))
    assert report.overall_score == 2.0  # 5.0 - 3.0 (capped)
    assert report.needs_revision is True


def test_every_failure_is_still_reported_even_though_the_penalty_caps():
    """Capping the SCORE must not hide failures from the revision prompt."""
    critic = _critic(8.0)
    report = critic.review(_digest(), _unsupported(5))
    assert len(report.weak_rationales) == 5


def test_the_code_default_threshold_is_6():
    """Only the fallback — see the next test for which value actually runs."""
    assert PrioritiesConfig.default().revision_threshold == 6.0


def test_the_live_threshold_is_the_stored_one_not_the_code_default(monkeypatch):
    """The live gate is `workspace_settings` → priorities_agent_config →
    policy.revision_threshold. Changing the code default moves nothing in a
    deployment that has saved its config; this is why."""
    import rubik_core.priorities.settings_store as store

    blob = store._default_config_blob()
    blob["policy"]["revision_threshold"] = 7.5
    monkeypatch.setattr(store, "load_settings_blob", lambda: blob)

    assert PrioritiesOrchestrator().critic.threshold == 7.5


# ── the revision loop ────────────────────────────────────────────────────────


class _SeqStub:
    """Queued answers in order; keeps serving the last once one is left.

    An ``Exception`` in the queue is raised instead of returned — that is how
    a critic call fails (``CriticAgent.review`` catches it and marks the report
    ``critic_failed``).
    """

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls: list[str] = []

    def invoke_json(self, prompt, fallback=None, **kw):
        self.calls.append(prompt)
        item = self.payloads.pop(0) if len(self.payloads) > 1 else self.payloads[0]
        if isinstance(item, Exception):
            raise item
        return item


def _item(title, signal_ids=("t1", "t2")):
    return {
        "title": title,
        "why_it_matters": "matters",
        "urgency": "high",
        "suggested_action": "do it",
        "signal_ids": list(signal_ids),
    }


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """No UC-25 memory writes and no PTO lookup against a real calendar."""
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


def _run(monkeypatch, *, max_revisions, critic_answers, decisions):
    cfg = PrioritiesConfig.default()
    cfg.revision_threshold = 6.0
    cfg.max_revisions = max_revisions
    orch = PrioritiesOrchestrator(config=cfg)

    stub_digest = ChannelDigest(
        channel=ChannelKind.GMAIL,
        narrative="n",
        signals=[
            ChannelSignal(id="t1", channel=ChannelKind.GMAIL, title="a", attrs={}),
            ChannelSignal(id="t2", channel=ChannelKind.GMAIL, title="b", attrs={}),
        ],
    )

    class _Agent:
        channel = ChannelKind.GMAIL

        def __init__(self, **_):
            pass

        def run(self):
            return stub_digest

    monkeypatch.setattr(
        "rubik_core.priorities.orchestrator.CHANNEL_AGENT_REGISTRY",
        {ChannelKind.GMAIL: _Agent},
    )
    orch.summarizer.client = _SeqStub([{"narrative": "n", "cross_channel_themes": []}])
    dec = _SeqStub(decisions)
    orch.decision_maker.client = dec
    critic = _SeqStub(
        [
            a if isinstance(a, Exception) else _critique(a, "fix it")
            for a in critic_answers
        ]
    )
    orch.critic.client = critic
    bundle = orch.generate(
        PrioritiesRequest(
            user_email="me@x.example", channels=[ChannelKind.GMAIL], max_priorities=5
        )
    )
    return bundle, dec, critic


def test_max_revisions_3_actually_runs_more_than_one_round(monkeypatch):
    """The dead knob: 3 used to mean exactly 1."""
    bundle, dec, _ = _run(
        monkeypatch,
        max_revisions=3,
        # first score fails, each revision improves but still fails, until the last
        critic_answers=[2.0, 3.0, 4.0, 9.0],
        decisions=[
            {"priorities": [_item("one"), _item("two")]},
            {"priorities": [_item("one v2"), _item("two v2")]},
            {"priorities": [_item("one v3"), _item("two v3")]},
            {"priorities": [_item("one v4"), _item("two v4")]},
        ],
    )
    assert bundle.revision_count == 3
    assert len(dec.calls) == 4  # initial + 3 revisions


def test_loop_stops_as_soon_as_the_score_clears_the_gate(monkeypatch):
    bundle, dec, _ = _run(
        monkeypatch,
        max_revisions=3,
        critic_answers=[2.0, 8.0],  # one revision is enough
        decisions=[
            {"priorities": [_item("one")]},
            {"priorities": [_item("one improved")]},
        ],
    )
    assert bundle.revision_count == 1
    assert len(dec.calls) == 2


def test_loop_stops_early_when_a_round_does_not_improve(monkeypatch):
    """Convergence guard — don't burn two more calls on a stuck list."""
    bundle, dec, _ = _run(
        monkeypatch,
        max_revisions=3,
        critic_answers=[3.0, 3.0],  # no improvement
        decisions=[
            {"priorities": [_item("one")]},
            {"priorities": [_item("one again")]},
        ],
    )
    assert bundle.revision_count == 1
    assert len(dec.calls) == 2
    assert any("did not improve" in a for a in bundle.audit_trail)


def test_max_revisions_zero_disables_revision_entirely(monkeypatch):
    bundle, dec, critic = _run(
        monkeypatch,
        max_revisions=0,
        critic_answers=[1.0],
        decisions=[{"priorities": [_item("one")]}],
    )
    assert bundle.revision_count == 0
    assert len(dec.calls) == 1
    assert len(critic.calls) == 1


def test_the_persisted_score_is_the_score_of_the_list_that_shipped(monkeypatch):
    """The re-score is what the bundle records — not the draft's score."""
    bundle, _, critic = _run(
        monkeypatch,
        max_revisions=1,
        critic_answers=[3.0, 8.0],
        decisions=[
            {"priorities": [_item("one")]},
            {"priorities": [_item("one, sharpened")]},
        ],
    )
    assert bundle.revision_count == 1
    assert len(critic.calls) == 2
    assert bundle.last_critique_score == 8.0


def test_a_failed_rescore_stops_the_loop_and_records_no_score(monkeypatch):
    """A down critic's fallback is the threshold minus the blocker penalty — a
    number nobody judged. It must neither steer another round nor be persisted
    as the revised list's quality."""
    one_signal = ("t1",)  # fails the blocker, so the fallback lands under 6.0
    bundle, dec, _ = _run(
        monkeypatch,
        max_revisions=3,
        # real 3.0 - 1.5 = 1.5; then the critic is down: fallback 6.0 - 1.5 =
        # 4.5, which "needs revision" and looks like an improvement on 1.5 —
        # exactly the noise that must not buy another round.
        critic_answers=[3.0, RuntimeError("vertex 429"), 9.0],
        decisions=[
            {"priorities": [_item("one", one_signal)]},
            {"priorities": [_item("one improved", one_signal)]},
            {"priorities": [_item("one, a third time", one_signal)]},
        ],
    )
    assert bundle.revision_count == 1
    assert len(dec.calls) == 2
    assert bundle.last_critique_score is None
    assert any("UNAVAILABLE on the re-score" in a for a in bundle.audit_trail)


def test_a_down_first_critic_does_not_trip_the_convergence_guard(monkeypatch):
    """With the first-pass critic down there is no real score to beat. The
    revised list's first REAL score must not be read as "did not improve"
    against the synthetic fallback — that would end the loop on noise."""
    one_signal = ("t1",)  # fails the blocker, so the fallback score is < 6.0
    bundle, dec, _ = _run(
        monkeypatch,
        max_revisions=3,
        # fallback 6.0 - 1.5 = 4.5; then real 5.0 - 1.5 = 3.5; then 9.0 - 1.5.
        critic_answers=[RuntimeError("vertex 503"), 5.0, 9.0],
        decisions=[
            {"priorities": [_item("one", one_signal)]},
            {"priorities": [_item("one v2", one_signal)]},
            {"priorities": [_item("one v3", one_signal)]},
        ],
    )
    assert not any("did not improve" in a for a in bundle.audit_trail)
    assert bundle.revision_count == 2
    assert len(dec.calls) == 3
    assert bundle.last_critique_score == 7.5

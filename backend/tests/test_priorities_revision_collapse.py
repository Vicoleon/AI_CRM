"""The critic revision must never shrink a user's briefing.

Regression: the critic scored a user's list 6.0/10 and asked for a revision;
the revise pass came back with 1 priority instead of 4. A downstream filter
then removed that sole survivor and their briefing was empty in both the app
and Slack. Observed twice on one user (4 -> 1, then 3 -> 2), so the revision
sheds items systematically, not by chance.

`CritiqueReport` carries only a holistic score plus weak_rationales /
missing_signals / suggestions — it has NO per-item "remove this" channel — so a
shorter revision is dropping work nobody asked it to drop.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from rubik_core.priorities import (
    ChannelKind,
    PrioritiesConfig,
    PrioritiesOrchestrator,
    PrioritiesRequest,
)
from rubik_core.priorities.models import (
    ChannelDigest,
    ChannelSignal,
    EntityKind,
    PriorityCandidate,
    PriorityKind,
)
from rubik_core.priorities.orchestrator import _merge_revision, _revision_identity

# ── _merge_revision unit tests ───────────────────────────────────────────────


def _p(title, *, entity_id=None, kind=None, pk=None):
    return PriorityCandidate(
        title=title,
        urgency="medium",
        why_it_matters="because",
        entity_kind=kind,
        entity_id=entity_id,
        priority_kind=pk,
    )


def test_shorter_revision_is_topped_back_up_to_the_original_count():
    """The core bug: 4 -> 1 must not cost the user 3 priorities."""
    first = [_p("A"), _p("B"), _p("C"), _p("D")]
    revised = [_p("A-improved")]

    merged, restored = _merge_revision(
        first_pass=first, revised=revised, max_priorities=5
    )

    assert len(merged) == 4, "a revision may improve the list, never shrink it"
    # The revision's own item leads — it carries the improvements we asked for.
    assert merged[0].title == "A-improved"
    # The gap is closed from the first pass, in original rank order.
    assert [p.title for p in merged[1:]] == ["A", "B", "C"]
    assert restored == ["A", "B", "C"]


def test_equal_length_revision_is_accepted_untouched():
    """A pure rewrite must NOT trigger restores.

    Rewriting is the most common legitimate revision, and for candidates with no
    entity triple a rewritten title is indistinguishable from "dropped one,
    added another". Restoring here would put the vague original back next to the
    improved rewrite that replaced it.
    """
    first = [_p("vague item"), _p("second")]
    revised = [_p("specific dated item"), _p("second, sharpened")]

    merged, restored = _merge_revision(
        first_pass=first, revised=revised, max_priorities=5
    )

    assert [p.title for p in merged] == ["specific dated item", "second, sharpened"]
    assert restored == []


def test_longer_revision_is_accepted_and_capped():
    first = [_p("A")]
    revised = [_p("A"), _p("B"), _p("C")]

    merged, restored = _merge_revision(
        first_pass=first, revised=revised, max_priorities=2
    )

    assert [p.title for p in merged] == ["A", "B"]  # cap honored
    assert restored == []


def test_restore_never_exceeds_max_priorities_and_reports_truthfully():
    """`restored` must name only items that actually survived the cap."""
    first = [_p("A"), _p("B"), _p("C"), _p("D")]
    revised = [_p("A-improved")]

    merged, restored = _merge_revision(
        first_pass=first, revised=revised, max_priorities=2
    )

    assert len(merged) == 2
    # Only ONE item fit past the revision's own item, so only one is claimed.
    assert restored == ["A"]


def test_matching_uses_the_entity_triple_not_the_title():
    """Same entity, rewritten title -> matched, so nothing is restored."""
    same = dict(entity_id="todo-1", kind=EntityKind.TODO, pk=PriorityKind.EOS_TODO_DUE)
    first = [_p("Knock out the L10 import fix", **same), _p("B")]
    revised = [_p("Fix L10 import before Wednesday", **same)]

    merged, restored = _merge_revision(
        first_pass=first, revised=revised, max_priorities=5
    )

    # Gap of 1 -> exactly one restore, and it must be B (the genuinely dropped
    # item), NOT the retitled todo-1 which the revision did carry forward.
    assert len(merged) == 2
    assert restored == ["B"]
    assert merged[0].title == "Fix L10 import before Wednesday"


def test_identity_prefers_entity_triple_over_title():
    tagged = _p(
        "whatever", entity_id="e1", kind=EntityKind.TODO, pk=PriorityKind.EOS_TODO_DUE
    )
    assert _revision_identity(tagged)[0] == "entity"
    assert _revision_identity(_p("Some  Title ")) == ("title", "some title")


def test_empty_first_pass_is_a_no_op():
    revised = [_p("A")]
    merged, restored = _merge_revision(first_pass=[], revised=revised, max_priorities=5)
    assert [p.title for p in merged] == ["A"]
    assert restored == []


# ── End-to-end through the orchestrator ──────────────────────────────────────

_FUTURE = datetime.now(timezone.utc) + timedelta(days=1)
_START = _FUTURE.strftime("%Y-%m-%dT14:00:00Z")


class _SeqClient:
    """Drop-in for LLMJsonClient: queued payloads in order, prompts recorded.

    Mirrors `test_priorities_pipeline.StubClient`'s interface (`invoke_json`),
    but keeps serving the LAST payload once the queue is down to one so a stage
    called more than expected doesn't silently fall back to `{}`.
    """

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls: list[str] = []

    def invoke_json(self, prompt: str, fallback=None, **kwargs):
        self.calls.append(prompt)
        if len(self.payloads) > 1:
            return self.payloads.pop(0)
        return self.payloads[0] if self.payloads else (fallback or {})


def _item(title):
    return {
        "title": title,
        "why_it_matters": f"{title} matters, with a date: {_START}.",
        "urgency": "high",
        "suggested_action": "Do it",
        "signal_ids": ["t1"],
    }


def test_orchestrator_restores_a_collapsed_revision(monkeypatch):
    """The live 4 -> 1 collapse, end to end, with the audit trail recording it."""
    orch = PrioritiesOrchestrator(config=PrioritiesConfig.default())

    stub_digest = ChannelDigest(
        channel=ChannelKind.GMAIL,
        narrative="threads",
        signals=[
            ChannelSignal(
                id="t1",
                channel=ChannelKind.GMAIL,
                title="Q3 plan",
                snippet="s",
                timestamp=_START,
            )
        ],
    )

    class _StubAgent:
        channel = ChannelKind.GMAIL

        def __init__(self, **_):
            pass

        def run(self):
            return stub_digest

    monkeypatch.setattr(
        "rubik_core.priorities.orchestrator.CHANNEL_AGENT_REGISTRY",
        {ChannelKind.GMAIL: _StubAgent},
    )

    orch.summarizer.client = _SeqClient(
        [{"narrative": "Busy.", "cross_channel_themes": []}]
    )
    decision_client = _SeqClient(
        [
            {"priorities": [_item("One"), _item("Two"), _item("Three"), _item("Four")]},
            {"priorities": [_item("One, sharpened")]},  # the collapse
        ]
    )
    orch.decision_maker.client = decision_client
    orch.critic.client = _SeqClient(
        [
            {
                # 6.0 - 3.0 (four single-signal items, penalty capped) =
                # 3.0, under the 6.0 default threshold -> revise
                "overall_score": 6.0,
                "missing_signals": [],
                "weak_rationales": ["One — generic"],
                "suggestions": ["Cite a deadline"],
                "raw_feedback": "Be specific.",
            }
        ]
    )

    bundle = orch.generate(
        PrioritiesRequest(
            user_email="me@x.example", channels=[ChannelKind.GMAIL], max_priorities=5
        )
    )

    assert bundle.revision_count == 1
    assert len(decision_client.calls) == 2
    # The collapse is undone: 4 in, 4 out (not 1).
    assert len(bundle.priorities) == 4
    assert bundle.priorities[0].title == "One, sharpened"
    # And it is visible, not silent.
    audit = " ".join(bundle.audit_trail)
    assert "revision dropped 3 item(s)" in audit

    # The revise prompt must tell the model this is a refinement with a floor.
    revise_prompt = decision_client.calls[1]
    assert "REFINEMENT, not a fresh start" in revise_prompt
    assert "AT LEAST 4 priorities" in revise_prompt

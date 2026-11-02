"""Tests for the multi-agent AI Priorities pipeline.

These tests stay LLM-free and network-free by replacing the per-role
LLM client and each channel agent's fetch_signals method with stubs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from rubik_core.priorities import (
    ChannelKind,
    PrioritiesConfig,
    PrioritiesOrchestrator,
    PrioritiesRequest,
)
from rubik_core.priorities.agents import (
    CalendarChannelAgent,
    CriticAgent,
    DecisionMakerAgent,
    GmailChannelAgent,
    MeetChannelAgent,
    SummarizerAgent,
)
from rubik_core.priorities.models import (
    ChannelDigest,
    ChannelSignal,
    PriorityCandidate,
)

# ── Helpers ──────────────────────────────────────────────────────────────────

# A calendar event ~1 day out, so the forward-looking pipeline treats it as a
# genuine upcoming obligation regardless of when the suite runs.
_FUTURE_EVENT_DT = datetime.now(timezone.utc) + timedelta(days=1)
_FUTURE_EVENT_START = _FUTURE_EVENT_DT.strftime("%Y-%m-%dT14:00:00Z")
_FUTURE_EVENT_END = _FUTURE_EVENT_DT.strftime("%Y-%m-%dT15:00:00Z")


class StubClient:
    """Drop-in for LLMJsonClient that returns canned JSON, no network."""

    def __init__(self, payload):
        self.payload = payload
        self.calls: list[str] = []

    def invoke_json(self, prompt: str, fallback=None, **kwargs):
        self.calls.append(prompt)
        # Allow callers to swap payloads between calls (e.g. revision flow)
        if isinstance(self.payload, list):
            if not self.payload:
                return fallback if fallback is not None else {}
            return self.payload.pop(0)
        return self.payload


def _patch_agent(monkeypatch, agent_obj, payload, fetch=None):
    """Replace an agent's LLM client and (optionally) its fetcher."""
    monkeypatch.setattr(agent_obj, "client", StubClient(payload))
    if fetch is not None:
        monkeypatch.setattr(agent_obj, "fetch_signals", fetch)


# ── Dataclass smoke tests ────────────────────────────────────────────────────


def test_priorities_request_defaults_to_three_channels():
    req = PrioritiesRequest(user_email="me@x.example")
    assert ChannelKind.GMAIL in req.channels
    assert ChannelKind.CALENDAR in req.channels
    assert ChannelKind.MEET in req.channels
    assert req.max_priorities == 5


def test_channel_signal_carries_metadata():
    s = ChannelSignal(
        id="t1",
        channel=ChannelKind.GMAIL,
        title="Quarterly review",
        metadata={"sender": "boss@x.example"},
    )
    assert s.metadata["sender"] == "boss@x.example"


# ── End-to-end pipeline test (no LLM, no network) ────────────────────────────


def test_pipeline_runs_all_stages_in_order(monkeypatch):
    """Channels → summarizer → decision → critic → final bundle."""
    config = PrioritiesConfig.default()
    orch = PrioritiesOrchestrator(config=config)

    # Build channel agents and patch each one so fetch + LLM are stubs.
    gmail = GmailChannelAgent(user_email="me@x.example", config=config)
    cal = CalendarChannelAgent(user_email="me@x.example", config=config)
    meet = MeetChannelAgent(user_email="me@x.example", config=config)

    _patch_agent(
        monkeypatch,
        gmail,
        payload={
            "narrative": "1 unread from boss.",
            "highlights": ["Boss asked for Q3 plan"],
        },
        fetch=lambda: [
            {
                "id": "thread-1",
                "messages": [
                    {
                        "payload": {
                            "headers": [
                                {"name": "Subject", "value": "Q3 plan"},
                                {"name": "From", "value": "boss@x.example"},
                            ]
                        },
                        "snippet": "Need your Q3 plan by Friday",
                        "labelIds": ["UNREAD"],
                    }
                ],
            }
        ],
    )
    _patch_agent(
        monkeypatch,
        cal,
        payload={
            "narrative": "Standup + 1 external pitch.",
            "highlights": ["Acme pitch at 2pm"],
        },
        fetch=lambda: [
            {
                "id": "evt-1",
                "summary": "Acme pitch",
                # Future-dated so the forward-looking post-filter keeps it and
                # the critic's future-anchor blocker passes. Computed relative
                # to "now" so the test stays valid as the clock advances.
                "start": {"dateTime": _FUTURE_EVENT_START},
                "end": {"dateTime": _FUTURE_EVENT_END},
                "attendees": [
                    {"email": "ceo@acmecorp.example"},
                    {"email": "me@x.example"},
                ],
            }
        ],
    )
    _patch_agent(
        monkeypatch,
        meet,
        payload={
            "narrative": "Yesterday's review left an open commitment.",
            "highlights": ["Promised to share architecture doc"],
        },
        fetch=lambda: [],  # empty Meet → exercises the "no signals" branch
    )

    # Force the orchestrator's agent registry to use our patched instances.
    monkeypatch.setattr(
        "rubik_core.priorities.orchestrator.CHANNEL_AGENT_REGISTRY",
        {
            ChannelKind.GMAIL: lambda **_: gmail,
            ChannelKind.CALENDAR: lambda **_: cal,
            ChannelKind.MEET: lambda **_: meet,
        },
    )

    # Stub the downstream agents on the orchestrator instance.
    orch.summarizer.client = StubClient(
        {
            "narrative": "Big day: Acme pitch + Q3 plan deliverable.",
            "cross_channel_themes": ["Acme account momentum"],
        }
    )
    orch.decision_maker.client = StubClient(
        {
            "priorities": [
                {
                    "title": "Prep Acme pitch deck",
                    "why_it_matters": "External pitch in 4 hours, CEO attending.",
                    "urgency": "high",
                    "suggested_action": "Block 30 min to review the deck",
                    "signal_ids": ["evt-1"],
                },
                {
                    "title": "Draft Q3 plan",
                    "why_it_matters": "Boss asked by Friday.",
                    "urgency": "medium",
                    "suggested_action": "Reply with one-pager outline",
                    "signal_ids": ["thread-1"],
                },
            ],
        }
    )
    # Score above threshold → no revision triggered.
    orch.critic.client = StubClient(
        {
            "overall_score": 8.5,
            "missing_signals": [],
            "weak_rationales": [],
            "suggestions": [],
            "raw_feedback": "Looks good.",
        }
    )

    bundle = orch.generate(PrioritiesRequest(user_email="me@x.example"))

    assert len(bundle.priorities) == 2
    top = bundle.priorities[0]
    assert top.title == "Prep Acme pitch deck"
    assert top.urgency == "high"
    # source_channels should be resolved from the signal_ids back to channels
    assert ChannelKind.CALENDAR in top.source_channels
    assert bundle.revision_count == 0
    # UC-24: the critic now deterministically penalizes priorities that
    # cite only one signal_id AND that signal has no concrete future
    # anchor in attrs. The Q3-plan priority here cites a single thread
    # with no start_iso / mentioned_dates → -1.5 from the LLM's 8.5.
    # The Acme priority cites the calendar event (start_iso anchor) →
    # passes the blocker. Final: 8.5 - 1.5 = 7.0, above the 6.0 default
    # threshold so no revision fires (the test's revision_count==0).
    assert bundle.last_critique_score == 7.0
    # audit trail should mention each stage
    audit = " ".join(bundle.audit_trail)
    assert "channel gmail" in audit
    assert "summarizer" in audit
    assert "decision_maker" in audit
    assert "critic" in audit


def test_critic_below_threshold_triggers_one_revision(monkeypatch):
    """When critic.overall_score < threshold, decision-maker re-runs once."""
    config = PrioritiesConfig.default()
    orch = PrioritiesOrchestrator(config=config)

    # One stub channel with a single signal — the decision/critic stages only
    # run when signals exist (a zero-signal run is a legitimate quiet day and
    # skips them entirely since the empty-briefing fix).
    stub_digest = ChannelDigest(
        channel=ChannelKind.GMAIL,
        narrative="one thread",
        signals=[
            ChannelSignal(
                id="t1", channel=ChannelKind.GMAIL, title="Q3 plan", snippet="s"
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

    orch.summarizer.client = StubClient(
        {
            "narrative": "Quiet day.",
            "cross_channel_themes": [],
        }
    )
    # Decision-maker returns two different payloads on successive calls.
    decision_client = StubClient(
        [
            {
                "priorities": [
                    {
                        "title": "First-attempt vague item",
                        "why_it_matters": "It's important.",
                        "urgency": "medium",
                        "suggested_action": "Look at it",
                        "signal_ids": [],
                    }
                ]
            },
            {
                "priorities": [
                    {
                        "title": "Second-attempt specific item",
                        "why_it_matters": "After critic feedback — concrete and dated.",
                        "urgency": "high",
                        "suggested_action": "Reply by 3pm",
                        "signal_ids": [],
                    }
                ]
            },
        ]
    )
    orch.decision_maker.client = decision_client
    orch.critic.client = StubClient(
        {
            "overall_score": 5.0,  # below threshold (default 6.0)
            "missing_signals": [],
            "weak_rationales": ["First-attempt vague item — 'important' is generic"],
            "suggestions": ["Cite a deadline"],
            "raw_feedback": "Be specific.",
        }
    )

    bundle = orch.generate(
        PrioritiesRequest(user_email="me@x.example", channels=[ChannelKind.GMAIL])
    )

    assert bundle.revision_count == 1
    assert len(decision_client.calls) == 2  # called twice (initial + revise)
    # Second-attempt content wins
    assert bundle.priorities[0].title == "Second-attempt specific item"
    # Critic feedback was included in the revision prompt
    assert "Be specific." in decision_client.calls[1]


def test_channel_agent_handles_fetch_failure_gracefully(monkeypatch):
    """A crashed channel shouldn't take the bundle down."""
    config = PrioritiesConfig.default()
    agent = GmailChannelAgent(user_email="me@x.example", config=config)

    def boom():
        raise RuntimeError("auth failed")

    monkeypatch.setattr(agent, "fetch_signals", boom)
    digest = agent.run()
    assert digest.error == "fetch_failed: RuntimeError"
    assert "auth failed" not in digest.error
    assert digest.signals == []


def test_critic_score_clamped_to_valid_range(monkeypatch):
    config = PrioritiesConfig.default()
    critic = CriticAgent(config)
    critic.client = StubClient(
        {
            "overall_score": 99.0,  # nonsense → must clamp to 10.0
            "missing_signals": [],
            "weak_rationales": [],
            "suggestions": [],
            "raw_feedback": "",
        }
    )
    from rubik_core.priorities.models import ConsolidatedDigest

    digest = ConsolidatedDigest(narrative="x", all_signals=[])
    report = critic.review(digest, priorities=[])
    assert 0.0 <= report.overall_score <= 10.0
    assert report.overall_score == 10.0


def test_summarizer_preserves_all_signals_even_when_llm_dies(monkeypatch):
    """If the summarizer LLM fails, the signal list must still pass through."""
    config = PrioritiesConfig.default()
    summarizer = SummarizerAgent(config)

    class BoomClient:
        def invoke_json(self, prompt, fallback=None):
            return fallback if fallback is not None else {}

    summarizer.client = BoomClient()
    digests = [
        ChannelDigest(
            channel=ChannelKind.GMAIL,
            narrative="N",
            signals=[ChannelSignal(id="a", channel=ChannelKind.GMAIL, title="t")],
        ),
        ChannelDigest(
            channel=ChannelKind.CALENDAR,
            narrative="N2",
            signals=[ChannelSignal(id="b", channel=ChannelKind.CALENDAR, title="t2")],
        ),
    ]
    consolidated = summarizer.consolidate(digests)
    assert len(consolidated.all_signals) == 2
    # Per-channel summaries are kept even when LLM fallback fires
    assert consolidated.per_channel_summaries[ChannelKind.GMAIL.value] == "N"


def test_bundle_metadata_round_trips():
    """bundle_to_metadata → JSON-safe dict → bundle_from_metadata."""
    from rubik_core.priorities import (
        PrioritiesBundle,
        bundle_from_metadata,
        bundle_to_metadata,
    )

    original = PrioritiesBundle(
        user_email="me@x.example",
        crm_provenance={"workspace": "test", "deal_ids": [], "unprovable": False},
        priorities=[
            PriorityCandidate(
                id="p1",
                title="Reply to Acme",
                why_it_matters="CEO asked for a number by EOD.",
                urgency="high",
                source_channels=[ChannelKind.GMAIL, ChannelKind.CALENDAR],
                source_links=["https://mail.example/123"],
                suggested_action="Reply with the Q3 figure",
                signal_ids=["t-1", "evt-2"],
            )
        ],
        headline="One urgent thread",
        revision_count=1,
        last_critique_score=8.5,
        audit_trail=["stage 1", "stage 2"],
    )

    blob = bundle_to_metadata(original)
    assert blob["schema_version"] == 2
    assert blob["priorities"][0]["source_channels"] == ["gmail", "calendar"]

    rebuilt = bundle_from_metadata("me@x.example", blob)
    assert rebuilt.headline == "One urgent thread"
    assert rebuilt.revision_count == 1
    assert rebuilt.last_critique_score == 8.5
    assert rebuilt.priorities[0].title == "Reply to Acme"
    assert ChannelKind.GMAIL in rebuilt.priorities[0].source_channels
    assert ChannelKind.CALENDAR in rebuilt.priorities[0].source_channels


def test_decision_maker_resolves_source_channels_from_signal_ids(monkeypatch):
    config = PrioritiesConfig.default()
    dm = DecisionMakerAgent(config)
    dm.client = StubClient(
        {
            "priorities": [
                {
                    "title": "X",
                    "why_it_matters": "Y",
                    "urgency": "high",
                    "suggested_action": "Z",
                    "signal_ids": ["a", "b"],
                }
            ],
        }
    )
    from rubik_core.priorities.models import ConsolidatedDigest

    digest = ConsolidatedDigest(
        narrative="n",
        all_signals=[
            ChannelSignal(
                id="a", channel=ChannelKind.GMAIL, title="email", link="https://gmail/a"
            ),
            ChannelSignal(
                id="b",
                channel=ChannelKind.CALENDAR,
                title="event",
                link="https://cal/b",
            ),
        ],
    )
    out = dm.decide(digest, max_priorities=5)
    assert len(out) == 1
    p = out[0]
    assert ChannelKind.GMAIL in p.source_channels
    assert ChannelKind.CALENDAR in p.source_channels
    assert "https://gmail/a" in p.source_links
    assert "https://cal/b" in p.source_links

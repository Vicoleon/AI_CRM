"""UC-24 tests — entity index, dept playbook injection, 5 meta-rules,
and the critic's blocker check.

The 5-meta-rule prompt is the engine of UC-24. The
``test_decision_maker_prompt_contains_all_five_meta_rules`` regression
guarantees the rule names survive any future edit.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from rubik_core.priorities import (
    ChannelKind,
    ChannelSignal,
    ConsolidatedDigest,
    EntityKind,
    PriorityCandidate,
    PriorityKind,
)
from rubik_core.priorities.agents import CriticAgent, DecisionMakerAgent
from rubik_core.priorities.config import PrioritiesConfig
from rubik_core.priorities.orchestrator import (
    _build_entity_index,
    _resolve_playbook_text,
    _resolve_user_department,
)

# ── helpers ──────────────────────────────────────────────────────────────────


def _signal(
    *,
    sid: str,
    channel: ChannelKind,
    entity_kind: EntityKind | None = None,
    entity_id: str | None = None,
    attrs: dict | None = None,
    related_entities: list[dict] | None = None,
) -> ChannelSignal:
    attrs = dict(attrs or {})
    if related_entities:
        attrs["related_entities"] = related_entities
    return ChannelSignal(
        id=sid,
        channel=channel,
        title=f"sig-{sid}",
        entity_kind=entity_kind,
        entity_id=entity_id,
        attrs=attrs,
    )


# ── Entity index ─────────────────────────────────────────────────────────────


def test_entity_index_groups_by_entity_key():
    sigs = [
        _signal(
            sid="g-1",
            channel=ChannelKind.GMAIL,
            entity_kind=EntityKind.THREAD,
            entity_id="t-1",
        ),
        _signal(
            sid="h-1",
            channel=ChannelKind.HUBSPOT,
            entity_kind=EntityKind.DEAL,
            entity_id="d-100",
        ),
        _signal(
            sid="c-1",
            channel=ChannelKind.CALENDAR,
            entity_kind=EntityKind.EVENT,
            entity_id="evt-1",
        ),
    ]
    idx = _build_entity_index(sigs)
    assert idx["thread:t-1"] == ["g-1"]
    assert idx["deal:d-100"] == ["h-1"]
    assert idx["event:evt-1"] == ["c-1"]


def test_entity_index_promotes_related_entities_to_join_keys():
    # Calendar event with two external attendees + their domains. The
    # index should let the decision-maker join the event with anything
    # else that touches those people / company.
    sigs = [
        _signal(
            sid="c-1",
            channel=ChannelKind.CALENDAR,
            entity_kind=EntityKind.EVENT,
            entity_id="evt-1",
            related_entities=[
                {"entity_kind": "person", "entity_id": "ceo@acmecorp.example"},
                {"entity_kind": "company", "entity_id": "acmecorp.example"},
            ],
        ),
        _signal(
            sid="g-1",
            channel=ChannelKind.GMAIL,
            entity_kind=EntityKind.THREAD,
            entity_id="t-1",
            related_entities=[
                {"entity_kind": "person", "entity_id": "ceo@acmecorp.example"},
            ],
        ),
    ]
    idx = _build_entity_index(sigs)
    # ceo@acmecorp.example is touched by BOTH signals → cross-channel join.
    assert sorted(idx["person:ceo@acmecorp.example"]) == ["c-1", "g-1"]
    assert idx["company:acmecorp.example"] == ["c-1"]


def test_entity_index_skips_signals_without_entity():
    sigs = [_signal(sid="x", channel=ChannelKind.GMAIL)]
    idx = _build_entity_index(sigs)
    assert idx == {}


# ── Department resolution ────────────────────────────────────────────────────


@patch("rubik_core.database.db.SessionLocal")
def test_resolve_user_department_returns_users_department(mock_session):
    mock_user = MagicMock()
    mock_user.department = "Operations"
    mock_db = MagicMock()
    mock_session.return_value.__enter__.return_value = mock_db
    mock_db.query.return_value.filter.return_value.first.return_value = mock_user

    dept = _resolve_user_department("me@x.example")
    assert dept == "Operations"


@patch("rubik_core.database.db.SessionLocal")
def test_resolve_user_department_none_for_unknown_user(mock_session):
    mock_db = MagicMock()
    mock_session.return_value.__enter__.return_value = mock_db
    mock_db.query.return_value.filter.return_value.first.return_value = None
    assert _resolve_user_department("ghost@x.example") is None


@patch("rubik_core.priorities.playbook_store.resolve_effective")
def test_resolve_playbook_text_returns_concatenated(mock_resolve):
    mock_resolve.return_value = MagicMock(concatenated_text="PLAYBOOK_BODY")
    text = _resolve_playbook_text("Sales")
    assert text == "PLAYBOOK_BODY"


# ── Decision-maker prompt — 5 meta-rules + playbook ──────────────────────────


# The dreamer is forward-looking: these headings + the kinds the prompt
# advertises must survive future edits.
_REQUIRED_META_RULE_PHRASES = (
    "Upcoming-meeting prep",
    "Approaching deadline",
    "Follow-up needed before the next touchpoint",
    "Silence threatening a relationship",
    "Commitment to deliver before next contact",
    "High-value entity going cold",
)

# The kinds the prompt explicitly lists for the model to choose from.
# (PREP_DEBT / SCHEDULING_DEBT remain in the enum as legacy aliases but are
# no longer advertised — the forward kinds supersede them.)
_REQUIRED_PRIORITY_KINDS = (
    "upcoming_prep",
    "approaching_deadline",
    "pre_touchpoint_followup",
    "follow_up_debt",
    "commitment_debt",
    "neglect_debt",
)


def test_decision_maker_prompt_contains_forward_meta_rules():
    config = PrioritiesConfig.default()
    agent = DecisionMakerAgent(config)
    digest = ConsolidatedDigest(narrative="n", all_signals=[])
    prompt = agent._build_prompt(
        digest,
        max_priorities=5,
        focus=None,
        entity_index={},
        department=None,
        playbook_text="",
        previous_attempt=None,
        critique=None,
    )
    for phrase in _REQUIRED_META_RULE_PHRASES:
        assert phrase in prompt, f"meta-rule heading missing: {phrase}"
    for kind in _REQUIRED_PRIORITY_KINDS:
        assert kind in prompt, f"priority_kind not listed in prompt: {kind}"
    # The lookahead placeholder must be substituted, never leaked literally.
    assert "{lookahead}" not in prompt


def test_decision_maker_prompt_injects_playbook_text():
    config = PrioritiesConfig.default()
    agent = DecisionMakerAgent(config)
    digest = ConsolidatedDigest(narrative="n", all_signals=[])
    prompt = agent._build_prompt(
        digest,
        max_priorities=5,
        focus=None,
        entity_index={},
        department="Operations",
        playbook_text="SECRET_OPS_PLAYBOOK_BODY",
        previous_attempt=None,
        critique=None,
    )
    assert "DEPARTMENT PLAYBOOK (Operations)" in prompt
    assert "SECRET_OPS_PLAYBOOK_BODY" in prompt


def test_decision_maker_prompt_falls_back_to_generic_when_no_playbook():
    config = PrioritiesConfig.default()
    agent = DecisionMakerAgent(config)
    digest = ConsolidatedDigest(narrative="n", all_signals=[])
    prompt = agent._build_prompt(
        digest,
        max_priorities=5,
        focus=None,
        entity_index={},
        department=None,
        playbook_text="",
        previous_attempt=None,
        critique=None,
    )
    assert "No department-specific playbook configured" in prompt


def test_decision_maker_prompt_highlights_cross_signal_entities():
    config = PrioritiesConfig.default()
    agent = DecisionMakerAgent(config)
    digest = ConsolidatedDigest(narrative="n", all_signals=[])
    prompt = agent._build_prompt(
        digest,
        max_priorities=5,
        focus=None,
        entity_index={
            "deal:d-100": ["h-1", "g-1"],  # cross-signal — should be marked ★
            "thread:t-1": ["g-1"],  # single — no star
        },
        department="Sales",
        playbook_text="",
        previous_attempt=None,
        critique=None,
    )
    # Cross-signal entity should appear with the star marker.
    deal_line = next(line for line in prompt.splitlines() if "deal:d-100" in line)
    assert "★" in deal_line
    thread_line = next(line for line in prompt.splitlines() if "thread:t-1" in line)
    assert "★" not in thread_line


def test_decision_maker_parses_priority_kind_and_entity_back():
    class _Stub:
        def invoke_json(self, prompt, fallback=None, **kwargs):
            return {
                "priorities": [
                    {
                        "title": "Reply to Acme",
                        "why_it_matters": "Open deal stalled 5 days; CEO last touched.",
                        "urgency": "high",
                        "suggested_action": "Send the proposal recap",
                        "priority_kind": "follow_up_debt",
                        "entity_kind": "deal",
                        "entity_id": "d-100",
                        "entity_label": "Acme Corp",
                        "signal_ids": ["g-1", "h-1"],
                    }
                ]
            }

    config = PrioritiesConfig.default()
    agent = DecisionMakerAgent(config)
    agent.client = _Stub()
    digest = ConsolidatedDigest(
        narrative="n",
        all_signals=[
            ChannelSignal(
                id="g-1", channel=ChannelKind.GMAIL, title="t", link="https://gmail/g-1"
            ),
            ChannelSignal(
                id="h-1",
                channel=ChannelKind.HUBSPOT,
                title="t",
                link="https://hubspot/h-1",
            ),
        ],
    )
    out = agent.decide(digest, max_priorities=5)
    assert len(out) == 1
    p = out[0]
    assert p.priority_kind == PriorityKind.FOLLOW_UP_DEBT
    assert p.entity_kind == EntityKind.DEAL
    assert p.entity_id == "d-100"
    assert p.entity_label == "Acme Corp"
    # Source channels resolved from signal_ids back to channel kinds.
    assert ChannelKind.GMAIL in p.source_channels
    assert ChannelKind.HUBSPOT in p.source_channels


# ── Critic blocker ───────────────────────────────────────────────────────────


def _critic_with_stub_score(score: float) -> CriticAgent:
    """Build a CriticAgent whose LLM call is stubbed to a known score."""
    config = PrioritiesConfig.default()
    critic = CriticAgent(config)

    class _Stub:
        def invoke_json(self, prompt, fallback=None):
            return {
                "overall_score": score,
                "missing_signals": [],
                "weak_rationales": [],
                "suggestions": [],
                "raw_feedback": "stub",
            }

    critic.client = _Stub()
    return critic


def test_critic_drops_score_when_priority_cites_only_one_signal_with_no_date():
    critic = _critic_with_stub_score(8.0)
    digest = ConsolidatedDigest(
        narrative="n",
        all_signals=[
            # A single signal carrying NO concrete future date.
            ChannelSignal(id="g-1", channel=ChannelKind.GMAIL, title="t", attrs={}),
        ],
    )
    priorities = [
        PriorityCandidate(
            title="Lonely priority",
            why_it_matters="x",
            urgency="medium",
            signal_ids=["g-1"],  # only one; no anchor in attrs
        )
    ]
    report = critic.review(digest, priorities)
    # 8.0 - 1.5 (blocker penalty for the one unsupported priority).
    assert report.overall_score == 6.5
    assert any("Lonely priority" in w for w in report.weak_rationales)
    assert any("BLOCKER" in w for w in report.weak_rationales)


def test_critic_does_not_penalize_when_two_signal_ids_cited():
    critic = _critic_with_stub_score(8.0)
    digest = ConsolidatedDigest(
        narrative="n",
        all_signals=[
            ChannelSignal(id="g-1", channel=ChannelKind.GMAIL, title="t"),
            ChannelSignal(id="h-1", channel=ChannelKind.HUBSPOT, title="t"),
        ],
    )
    priorities = [
        PriorityCandidate(
            title="Well-supported",
            why_it_matters="x",
            urgency="high",
            signal_ids=["g-1", "h-1"],
        )
    ]
    report = critic.review(digest, priorities)
    assert report.overall_score == 8.0


def test_critic_does_not_penalize_when_single_signal_has_concrete_date():
    critic = _critic_with_stub_score(8.0)
    digest = ConsolidatedDigest(
        narrative="n",
        all_signals=[
            ChannelSignal(
                id="c-1",
                channel=ChannelKind.CALENDAR,
                title="Acme pitch",
                attrs={"start_iso": "2026-05-28T14:00:00Z"},
            ),
        ],
    )
    priorities = [
        PriorityCandidate(
            title="Prep Acme pitch",
            why_it_matters="External meeting in 4 hours",
            urgency="high",
            signal_ids=["c-1"],
        )
    ]
    report = critic.review(digest, priorities)
    # No penalty — single signal carries start_iso anchor.
    assert report.overall_score == 8.0


def test_critic_unsupported_priorities_compound_score_drops():
    critic = _critic_with_stub_score(9.0)
    digest = ConsolidatedDigest(
        narrative="n",
        all_signals=[
            ChannelSignal(id="g-1", channel=ChannelKind.GMAIL, title="t"),
        ],
    )
    priorities = [
        PriorityCandidate(
            title="A", why_it_matters="x", urgency="medium", signal_ids=["g-1"]
        ),  # fails blocker
        PriorityCandidate(
            title="B", why_it_matters="x", urgency="medium", signal_ids=[]
        ),  # fails blocker
    ]
    report = critic.review(digest, priorities)
    # 9.0 - 1.5 * 2 = 6.0
    assert report.overall_score == 6.0
    # Both titles end up in weak_rationales.
    titles_flagged = "\n".join(report.weak_rationales)
    assert "A" in titles_flagged and "B" in titles_flagged


def test_the_critic_prompt_asks_only_about_what_it_shows():
    """The bug: the prompt told the model to check whether a single
    cited signal "carries a concrete FUTURE date or an amount in its attrs",
    while rendering each signal as ``id | channel | title``. The model was
    scoring data it never received — guessing, and marking down single-signal
    cards that DO carry a date. The deterministic blocker owns that check.

    If attrs ever start reaching this prompt, the instruction can come back;
    until then, neither may appear without the other.
    """
    critic = CriticAgent(PrioritiesConfig.default())
    digest = ConsolidatedDigest(
        narrative="n",
        all_signals=[
            ChannelSignal(
                id="c-1",
                channel=ChannelKind.CALENDAR,
                title="Acme pitch",
                attrs={
                    "start_iso": "2026-10-01T14:00:00Z",
                    "mentioned_amounts": ["$5,000"],
                },
            )
        ],
    )
    prompt = critic._build_prompt(
        digest, [PriorityCandidate(title="Prep Acme pitch", signal_ids=["c-1"])]
    )

    # The attrs the blocker reads are not rendered...
    assert "2026-10-01" not in prompt
    assert "$5,000" not in prompt
    # ...so the prompt must not send the model looking for them.
    assert "attrs" not in prompt
    for key in ("start_iso", "close_date", "mentioned_dates", "mentioned_amounts"):
        assert key not in prompt
    assert "checked deterministically" in prompt


def test_the_blocker_still_bites_without_the_models_help():
    """Taking the check out of the prompt must not take it out of the score."""
    critic = _critic_with_stub_score(8.0)
    digest = ConsolidatedDigest(
        narrative="n",
        all_signals=[ChannelSignal(id="g-1", channel=ChannelKind.GMAIL, title="t")],
    )
    report = critic.review(
        digest,
        [PriorityCandidate(title="Lonely", why_it_matters="x", signal_ids=["g-1"])],
    )
    assert report.overall_score == 6.5
    assert any("Lonely" in w and "BLOCKER" in w for w in report.weak_rationales)


# ── REGRESSION: ownership — whose item is it? ────────────────────────────────
#
# Two shipped failures, both traced to the decision-maker having no way to tell
# the reader apart from the other people in the signals:
#   * "Prepare for your 1:1 with Blair" delivered TO Blair (the counterparty came
#     off the event title, which contains both names).
#   * An "ACTION NEEDED" email addressed to a colleague, with the reader merely
#     Cc'd, emitted as the reader's own high-urgency deadline.


def _prompt(**overrides) -> str:
    agent = DecisionMakerAgent(PrioritiesConfig.default())
    kwargs = dict(
        max_priorities=5,
        focus=None,
        entity_index={},
        department=None,
        playbook_text="",
        previous_attempt=None,
        critique=None,
    )
    kwargs.update(overrides)
    return agent._build_prompt(
        ConsolidatedDigest(narrative="n", all_signals=[]), **kwargs
    )


def test_decision_prompt_states_the_reader_identity():
    prompt = _prompt(reader_email="bellison@acme.example", reader_name="Blair Ellison")
    assert "## READER" in prompt
    assert "bellison@acme.example" in prompt
    assert "Blair Ellison" in prompt


def test_decision_prompt_reader_block_survives_a_missing_name():
    """`users.name` can be empty — the block must still name the reader, and
    must never leak a literal "None" into the prompt."""
    prompt = _prompt(reader_email="bellison@acme.example", reader_name=None)
    assert "## READER" in prompt
    assert "bellison@acme.example" in prompt
    assert "<None>" not in prompt


def test_decision_prompt_omits_reader_block_when_identity_unknown():
    prompt = _prompt()
    assert "## READER" not in prompt


def test_decision_prompt_forbids_naming_the_reader_as_counterparty():
    prompt = _prompt(reader_email="bellison@acme.example")
    assert "counterparties" in prompt
    # The rule must be explicit about not reading the name off the title.
    assert "title" in prompt.lower()
    assert "never their own" in prompt.lower() or "own counterparty" in prompt.lower()


def test_decision_prompt_teaches_cc_is_not_an_action():
    prompt = _prompt(reader_email="ebrooks@acme.example")
    assert "addressed_to_reader" in prompt
    assert "ACTION NEEDED" in prompt  # named explicitly as the sender's emphasis


def test_attrs_preview_keeps_ownership_fields_alongside_dates():
    """The ownership keys must survive the preview truncation next to a long
    list of extracted dates — losing `addressed_to_reader` is what let a
    someone-else's-deadline become the reader's.
    """
    from rubik_core.priorities.agents.decision_maker import _format_attrs_preview

    preview = _format_attrs_preview(
        {
            "direction": "they_sent_last",
            "addressed_to_reader": "cc",
            "asks_of_reader": False,
            "request_owner": "ana@acme.example",
            "thread_idle_days": 1,
            "mentioned_dates": [
                {"raw": f"day {i}", "resolved": f"2026-08-{i:02d}", "tense": "future"}
                for i in range(1, 8)
            ],
        }
    )
    assert "addressed_to_reader=cc" in preview
    assert "asks_of_reader=False" in preview


def test_attrs_preview_includes_calendar_counterparties():
    from rubik_core.priorities.agents.decision_maker import _format_attrs_preview

    preview = _format_attrs_preview(
        {"when_local": "9:00 AM CST", "counterparties": ["Devon Ellison"]}
    )
    assert "counterparties=['Devon Ellison']" in preview

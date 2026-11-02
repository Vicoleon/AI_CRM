"""Tests for SlackChannelAgent and its integration into the Priorities pipeline."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from rubik_core.priorities import (
    ChannelKind,
    EntityKind,
    PrioritiesConfig,
    PrioritiesOrchestrator,
    PrioritiesRequest,
)
from rubik_core.priorities.agents.slack_agent import SlackChannelAgent
from rubik_core.priorities.temporal import TemporalContext


def _make_temporal():
    return TemporalContext(
        now_utc=datetime(2026, 6, 25, 14, 0, tzinfo=timezone.utc),
        now_local=datetime(2026, 6, 25, 8, 0),
        tz_name="America/Denver",
        lookahead_hours=72,
    )


def test_disconnected_slack_returns_empty_signals():
    config = PrioritiesConfig.default()
    agent = SlackChannelAgent(
        user_email="test@example.com",
        config=config,
        temporal=_make_temporal(),
    )

    with patch("backend.services.slack_service.SlackService") as mock_svc_cls:
        mock_svc = MagicMock()
        mock_svc.is_connected = False
        mock_svc_cls.return_value = mock_svc

        digest = agent.run()
        assert digest.channel == ChannelKind.SLACK
        assert len(digest.signals) == 0
        assert "No notable activity" in digest.summary


def test_slack_channel_agent_parses_mentions_and_dms():
    config = PrioritiesConfig.default()
    agent = SlackChannelAgent(
        user_email="rep@acme.example",
        config=config,
        temporal=_make_temporal(),
    )

    with patch("backend.services.slack_service.SlackService") as mock_svc_cls:
        mock_svc = MagicMock()
        mock_svc.is_connected = True
        mock_svc._slack_user_id.return_value = "U-REP"
        mock_svc._app_link.side_effect = lambda cid, ts: (
            f"slack://channel?id={cid}&ts={ts}"
        )
        mock_svc_cls.return_value = mock_svc

        # 1 mention in customer channel
        mention = SimpleNamespace(
            channel_id="C-DEALS-ACME",
            channel_name="deals-acme",
            ts="1715000000.000100",
            user_id="U-CUSTOMER",
            user_name="Alice",
            text="<@U-REP> can you send the proposal by Friday?",
            permalink="https://slack.com/archives/C-DEALS-ACME/p1715000000000100",
            app_link="slack://channel?id=C-DEALS-ACME&ts=1715000000.000100",
            is_mention=True,
        )
        mock_svc.get_mentions.return_value = [mention]

        # 1 DM
        mock_svc.get_dm_threads.return_value = [
            {"channel_id": "D-BOB", "counterparty_name": "Bob Manager"}
        ]
        dm_msg = SimpleNamespace(
            channel_id="D-BOB",
            channel_name=None,
            ts="1715000010.000200",
            user_id="U-BOB",
            user_name="Bob Manager",
            text="Hey, do you have 5 mins for a quick check-in?",
            permalink=None,
            app_link="slack://channel?id=D-BOB&ts=1715000010.000200",
            is_mention=False,
        )
        mock_svc.get_channel_messages.return_value = [dm_msg]
        mock_svc.list_member_channels.return_value = []

        # Mock LLM response — the extractor's whole per-message schema is
        # `mentioned_dates` (see the prompt).
        with patch.object(agent.client, "invoke_json") as mock_llm:
            mock_llm.return_value = {
                "summary": "Received 1 mention in #deals-acme and 1 DM from Bob Manager.",
                "slack_attrs": {
                    "slack::C-DEALS-ACME::1715000000.000100": {
                        "mentioned_dates": ["2026-06-26"],
                    }
                },
            }

            digest = agent.run()
            assert digest.channel == ChannelKind.SLACK
            assert len(digest.signals) == 2
            assert "deals-acme" in digest.summary

            # Check mention signal
            sig_mention = next(s for s in digest.signals if "C-DEALS-ACME" in s.id)
            assert sig_mention.channel == ChannelKind.SLACK
            assert sig_mention.entity_kind == EntityKind.COMPANY
            assert sig_mention.attrs["is_mention"] is True
            assert sig_mention.attrs["customer_facing"] is True
            # Merged, then normalized against the pinned "now" (2026-06-25).
            [mentioned] = sig_mention.attrs["mentioned_dates"]
            assert mentioned["resolved"] == "2026-06-26"
            assert mentioned["tense"] == "future"

            # Check DM signal
            sig_dm = next(s for s in digest.signals if "D-BOB" in s.id)
            assert sig_dm.channel == ChannelKind.SLACK
            assert sig_dm.entity_kind == EntityKind.PERSON
            assert sig_dm.attrs["is_dm"] is True
            # Retired: nothing downstream ever read these, so the agent no
            # longer asks the model for them or seeds heuristic stand-ins.
            for retired in ("action_required", "ask_summary", "urgency_hint"):
                assert retired not in sig_dm.attrs


def test_slack_channel_agent_handles_rate_limiting():
    config = PrioritiesConfig.default()
    agent = SlackChannelAgent(
        user_email="rep@acme.example",
        config=config,
        temporal=_make_temporal(),
    )

    from backend.services.slack_service import SlackAPIError

    with patch("backend.services.slack_service.SlackService") as mock_svc_cls:
        mock_svc = MagicMock()
        mock_svc.is_connected = True
        mock_svc._slack_user_id.return_value = "U-REP"
        mock_svc.get_mentions.side_effect = SlackAPIError("ratelimited")

        signals = agent.fetch_signals()
        assert signals == []


def test_orchestrator_includes_slack_in_fan_out():
    config = PrioritiesConfig.default()
    orch = PrioritiesOrchestrator(config=config)

    assert ChannelKind.SLACK in orch.generate.__globals__["CHANNEL_AGENT_REGISTRY"]

    req = PrioritiesRequest(
        user_email="test@acme.example",
        channels=[ChannelKind.SLACK],
    )

    with patch("backend.services.slack_service.SlackService") as mock_svc_cls:
        mock_svc = MagicMock()
        mock_svc.is_connected = False
        mock_svc_cls.return_value = mock_svc

        with patch.object(orch.decision_maker, "decide", return_value=[]):
            with patch.object(orch.critic, "review") as mock_critic:
                mock_critic.return_value = MagicMock(
                    overall_score=9.0, needs_revision=False
                )
                bundle = orch.generate(req)
                assert bundle.user_email == "test@acme.example"
                assert any("channel slack" in a for a in bundle.audit_trail)


# ── "I already answered that" suppression ────────────────────────────────────
#
# The commonest "not useful" rating on priorities is one complaint — the
# card asked the reader to answer something they had already answered.
# The Gmail agent tracks who spoke last; the Slack agent did
# not, so a DM question resurfaced until it aged out. These pin the fix.


def _msg(
    channel_id,
    ts,
    user_id,
    text,
    *,
    channel_name=None,
    is_mention=False,
    thread_ts=None,
    reply_count=0,
):
    return SimpleNamespace(
        channel_id=channel_id,
        channel_name=channel_name,
        ts=ts,
        user_id=user_id,
        user_name={"U-REP": "Me", "U-MARLO": "Marlo", "U-ALICE": "Alice"}.get(
            user_id, user_id
        ),
        text=text,
        permalink=None,
        app_link=f"slack://channel?id={channel_id}&ts={ts}",
        is_mention=is_mention,
        thread_ts=thread_ts,
        reply_count=reply_count,
    )


def _connected_service(mock_svc_cls):
    mock_svc = MagicMock()
    mock_svc.is_connected = True
    mock_svc._slack_user_id.return_value = "U-REP"
    mock_svc._app_link.side_effect = lambda cid, ts: f"slack://channel?id={cid}&ts={ts}"
    mock_svc.get_mentions.return_value = []
    mock_svc.get_dm_threads.return_value = []
    mock_svc.list_member_channels.return_value = []
    mock_svc._fetch_thread.return_value = []
    mock_svc_cls.return_value = mock_svc
    return mock_svc


def _agent():
    return SlackChannelAgent(
        user_email="rep@acme.example",
        config=PrioritiesConfig.default(),
        temporal=_make_temporal(),
    )


def test_dm_question_the_reader_already_answered_is_dropped():
    """Marlo asks in a DM, the reader replies later on the same timeline → gone.

    A NEWER question from Marlo after the reader's reply is still surfaced:
    the check is per message, not per conversation."""
    with patch("backend.services.slack_service.SlackService") as mock_svc_cls:
        mock_svc = _connected_service(mock_svc_cls)
        mock_svc.get_dm_threads.return_value = [
            {"channel_id": "D-MARLO", "counterparty_name": "Marlo"}
        ]
        # conversations.history is newest-first.
        mock_svc.get_channel_messages.return_value = [
            _msg(
                "D-MARLO",
                "1715000300.000000",
                "U-MARLO",
                "Also — who owns the DD runbook?",
            ),
            _msg(
                "D-MARLO",
                "1715000200.000000",
                "U-REP",
                "Add them in Fathom → Team → Invite.",
            ),
            _msg(
                "D-MARLO",
                "1715000100.000000",
                "U-MARLO",
                "How do I give Emery Fathom access?",
            ),
        ]

        raw = _agent().fetch_signals()

    texts = [r["text"] for r in raw]
    assert "How do I give Emery Fathom access?" not in texts, "answered ask leaked"
    assert texts == ["Also — who owns the DD runbook?"]
    # A DM decided on its own timeline never costs a thread fetch.
    mock_svc._fetch_thread.assert_not_called()


def test_dm_question_without_a_reader_reply_is_kept():
    with patch("backend.services.slack_service.SlackService") as mock_svc_cls:
        mock_svc = _connected_service(mock_svc_cls)
        mock_svc.get_dm_threads.return_value = [
            {"channel_id": "D-MARLO", "counterparty_name": "Marlo"}
        ]
        mock_svc.get_channel_messages.return_value = [
            _msg(
                "D-MARLO",
                "1715000100.000000",
                "U-MARLO",
                "How do I give Emery Fathom access?",
            ),
            # Reader spoke BEFORE the question — that is not an answer.
            _msg("D-MARLO", "1715000050.000000", "U-REP", "morning!"),
        ]

        raw = _agent().fetch_signals()

    assert [r["text"] for r in raw] == ["How do I give Emery Fathom access?"]


def test_channel_mention_answered_in_thread_is_dropped():
    """A mention whose thread carries a later reply from the reader is handled."""
    with patch("backend.services.slack_service.SlackService") as mock_svc_cls:
        mock_svc = _connected_service(mock_svc_cls)
        mention = _msg(
            "C-DEALS-ACME",
            "1715000000.000100",
            "U-ALICE",
            "<@U-REP> can you send the proposal by Friday?",
            channel_name="deals-acme",
            is_mention=True,
            reply_count=None,  # search.messages never says
        )
        mock_svc.get_mentions.return_value = [mention]
        mock_svc._fetch_thread.return_value = [
            {
                "user": "U-ALICE",
                "ts": "1715000000.000100",
                "text": "…proposal by Friday?",
            },
            {
                "user": "U-REP",
                "ts": "1715000900.000000",
                "text": "Sent — check your inbox.",
            },
        ]

        raw = _agent().fetch_signals()

    assert raw == []
    mock_svc._fetch_thread.assert_called_once_with("C-DEALS-ACME", "1715000000.000100")


def test_channel_mention_with_only_other_peoples_replies_is_kept():
    with patch("backend.services.slack_service.SlackService") as mock_svc_cls:
        mock_svc = _connected_service(mock_svc_cls)
        mock_svc.get_mentions.return_value = [
            _msg(
                "C-DEALS-ACME",
                "1715000000.000100",
                "U-ALICE",
                "<@U-REP> can you send the proposal by Friday?",
                channel_name="deals-acme",
                is_mention=True,
                reply_count=None,
            )
        ]
        mock_svc._fetch_thread.return_value = [
            {"user": "U-ALICE", "ts": "1715000000.000100", "text": "…by Friday?"},
            {
                "user": "U-MARLO",
                "ts": "1715000500.000000",
                "text": "+1, we need it too",
            },
            # The reader's message is OLDER than the ask — not an answer.
            {"user": "U-REP", "ts": "1714999000.000000", "text": "earlier chatter"},
        ]

        raw = _agent().fetch_signals()

    assert len(raw) == 1


def test_mention_that_is_itself_a_thread_reply_checks_the_parent_thread():
    """A reply-in-thread carries thread_ts != ts; the parent thread is fetched."""
    with patch("backend.services.slack_service.SlackService") as mock_svc_cls:
        mock_svc = _connected_service(mock_svc_cls)
        mock_svc.get_mentions.return_value = [
            _msg(
                "C-OPS",
                "1715000200.000000",
                "U-ALICE",
                "<@U-REP> thoughts?",
                channel_name="ops",
                is_mention=True,
                thread_ts="1715000000.000000",
                reply_count=None,
            )
        ]
        mock_svc._fetch_thread.return_value = [
            {
                "user": "U-ALICE",
                "ts": "1715000200.000000",
                "text": "<@U-REP> thoughts?",
            },
            {"user": "U-REP", "ts": "1715000300.000000", "text": "Looks right to me."},
        ]

        raw = _agent().fetch_signals()

    assert raw == []
    mock_svc._fetch_thread.assert_called_once_with("C-OPS", "1715000000.000000")


def test_known_reply_less_channel_message_costs_no_thread_fetch():
    """conversations.history says reply_count == 0 → nothing to check, no call."""
    with patch("backend.services.slack_service.SlackService") as mock_svc_cls:
        mock_svc = _connected_service(mock_svc_cls)
        mock_svc.list_member_channels.return_value = [
            {"id": "C-CLIENT-ACME", "name": "client-acme"}
        ]
        mock_svc.get_channel_messages.return_value = [
            _msg(
                "C-CLIENT-ACME",
                "1715000100.000000",
                "U-ALICE",
                "Renewal paperwork is due next week.",
                channel_name="client-acme",
                reply_count=0,
            ),
            # Reader posted later in the CHANNEL — not a DM, so not treated as
            # an answer: busy channels carry unrelated traffic.
            _msg("C-CLIENT-ACME", "1715000200.000000", "U-REP", "unrelated note"),
        ]

        raw = _agent().fetch_signals()

    assert len(raw) == 1
    mock_svc._fetch_thread.assert_not_called()


def test_thread_fetch_failure_keeps_the_ask():
    """Unknown must never drop a real ask."""
    with patch("backend.services.slack_service.SlackService") as mock_svc_cls:
        mock_svc = _connected_service(mock_svc_cls)
        mock_svc.get_mentions.return_value = [
            _msg(
                "C-OPS",
                "1715000000.000100",
                "U-ALICE",
                "<@U-REP> ping",
                channel_name="ops",
                is_mention=True,
                reply_count=None,
            )
        ]
        mock_svc._fetch_thread.side_effect = RuntimeError("boom")

        raw = _agent().fetch_signals()

    assert len(raw) == 1


def test_rate_limited_exit_still_applies_dm_check_but_skips_thread_fetch():
    from backend.services.slack_service import SlackAPIError

    with patch("backend.services.slack_service.SlackService") as mock_svc_cls:
        mock_svc = _connected_service(mock_svc_cls)
        mock_svc.get_dm_threads.return_value = [
            {"channel_id": "D-MARLO", "counterparty_name": "Marlo"}
        ]

        calls = {"n": 0}

        def _history(ch_id, **_kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return [
                    _msg("D-MARLO", "1715000200.000000", "U-REP", "done"),
                    _msg(
                        "D-MARLO",
                        "1715000100.000000",
                        "U-MARLO",
                        "Can you approve my PTO?",
                        reply_count=None,
                    ),
                ]
            raise SlackAPIError("ratelimited")

        mock_svc.get_channel_messages.side_effect = _history
        # Second DM makes the scan hit the rate-limited early return.
        mock_svc.get_dm_threads.return_value = [
            {"channel_id": "D-MARLO", "counterparty_name": "Marlo"},
            {"channel_id": "D-OTHER", "counterparty_name": "Other"},
        ]

        raw = _agent().fetch_signals()

    assert raw == []
    mock_svc._fetch_thread.assert_not_called()


def test_slack_message_carries_reply_count_from_history():
    """The agent's 'no fetch when reply_count == 0' shortcut needs the parser
    to stamp the count; search matches leave it None (unknown)."""
    from backend.services.slack_service import SlackService

    svc = SlackService.__new__(SlackService)
    svc.user_email = "rep@acme.example"
    svc._user_cache = {}
    svc._channel_cache = {}
    with (
        patch.object(SlackService, "_slack_user_id", return_value="U-REP"),
        patch.object(SlackService, "_resolve_user_name", return_value="Alice"),
        patch.object(SlackService, "_app_link", return_value=None),
        patch.object(SlackService, "_resolve_channel_name", return_value="ops"),
    ):
        parent = svc._parse_message(
            {"user": "U-ALICE", "ts": "1.0", "text": "hi", "reply_count": 3},
            "C1",
            "ops",
            skip_permalink=True,
        )
        childless = svc._parse_message(
            {"user": "U-ALICE", "ts": "2.0", "text": "hi"},
            "C1",
            "ops",
            skip_permalink=True,
        )
        searched = svc._parse_search_match(
            {
                "user": "U-ALICE",
                "ts": "3.0",
                "text": "hi",
                "channel": {"id": "C1", "name": "ops"},
            }
        )
    assert parent.reply_count == 3
    assert childless.reply_count == 0
    assert searched.reply_count is None

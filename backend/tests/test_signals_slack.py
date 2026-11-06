"""UC-17.3 — Slack SignalSource.

Mocks SlackService entirely (no live API). Covers:
  - weekend-aware lookback widening (shared with Gmail)
  - @-mention in customer-facing channel → high severity
  - @-mention in unknown channel → medium severity
  - DM → high severity
  - Unknown user_email (no slack_user_id) → empty list, no exception
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

# ─── Fixture helpers ──────────────────────────────────────────────────────────


def _msg(
    *,
    channel_id: str = "C-PUBLIC",
    channel_name: str | None = "general",
    ts: str = "1715000000.000100",
    user_id: str = "U-SARAH",
    user_name: str | None = "Sarah",
    text: str = "<@U-REP> can you join the call at 3?",
    permalink: str | None = "https://x.slack.com/archives/C-PUBLIC/p1715000000000100",
) -> SimpleNamespace:
    """Mirror of slack_service.SlackMessage's attribute shape (Pydantic model
    in prod; SimpleNamespace is fine in tests since we read attrs, not types)."""
    return SimpleNamespace(
        channel_id=channel_id,
        channel_name=channel_name,
        ts=ts,
        user_id=user_id,
        user_name=user_name,
        text=text,
        permalink=permalink,
        app_link=None,
        is_mention=True,
    )


def _service_mock(
    *,
    mentions: list[Any] | None = None,
    slack_user_id: str = "U-REP",
) -> MagicMock:
    svc = MagicMock()
    svc._slack_user_id.return_value = slack_user_id
    svc.get_mentions.return_value = mentions or []
    return svc


# ─── Weekend-aware lookback (shared helper) ───────────────────────────────────


def test_monday_early_morning_widens_to_friday_eob_local():
    from backend.signals._lookback import widen_for_weekend

    # Mon 2026-05-11 02:00 UTC = Sun 2026-05-10 20:00 America/Denver.
    now = datetime(2026, 5, 11, 2, 0, tzinfo=timezone.utc)
    since = now - timedelta(hours=24)
    effective, widened = widen_for_weekend(
        since,
        now=now,
        user_tz="America/Denver",
    )
    assert widened is True
    friday_local = datetime(2026, 5, 8, 17, 0, tzinfo=ZoneInfo("America/Denver"))
    assert effective == friday_local.astimezone(timezone.utc)
    assert effective < since


def test_midweek_now_does_not_widen():
    from backend.signals._lookback import widen_for_weekend

    now = datetime(2026, 5, 13, 15, 0, tzinfo=timezone.utc)
    since = now - timedelta(hours=24)
    effective, widened = widen_for_weekend(
        since,
        now=now,
        user_tz="America/Denver",
    )
    assert widened is False
    assert effective == since


def test_with_no_zone_named_the_weekend_is_the_companys_own():
    """No zone passed: the control-row time zone decides Friday 5pm."""
    from backend.signals._lookback import widen_for_weekend
    from rubik_core.tenancy.workspace_time import (
        set_workspace_time_zone,
        workspace_time_zone_name,
    )

    # Mon 2026-05-11 02:00 UTC: early Monday in UTC and in Tokyo alike.
    now = datetime(2026, 5, 11, 2, 0, tzinfo=timezone.utc)
    since = now - timedelta(hours=24)

    original = workspace_time_zone_name()
    try:
        set_workspace_time_zone("UTC")
        effective, widened = widen_for_weekend(since, now=now)
        assert widened is True
        assert effective == datetime(2026, 5, 8, 17, 0, tzinfo=timezone.utc)

        set_workspace_time_zone("Asia/Tokyo")
        effective, _ = widen_for_weekend(since, now=now)
        # Friday 17:00 in Tokyo is 08:00 UTC.
        assert effective == datetime(2026, 5, 8, 8, 0, tzinfo=timezone.utc)
    finally:
        set_workspace_time_zone(original)


def test_the_signal_sources_and_the_slack_agent_name_no_zone_of_their_own():
    """The old fallback was one company's office clock. A source built without
    a zone, and the Slack agent run without a temporal context, both defer to
    the company's default."""
    from backend.signals.gmail import GmailSignalSource
    from backend.signals.slack import SlackSignalSource

    assert SlackSignalSource()._tz_name is None
    assert GmailSignalSource()._tz_name is None

    from rubik_core.priorities import PrioritiesConfig
    from rubik_core.priorities.agents import slack_agent

    seen: list[str | None] = []

    def spy(since, *, now, user_tz=None):
        seen.append(user_tz)
        return since, False

    agent = slack_agent.SlackChannelAgent(
        user_email="rep@acme.example",
        config=PrioritiesConfig.default(),
        temporal=None,
    )
    svc = MagicMock(is_connected=True)
    svc._slack_user_id.return_value = "U-REP"
    svc.get_mentions.return_value = []
    svc.get_dm_threads.return_value = []
    with (
        patch.object(slack_agent, "widen_for_weekend", spy),
        patch("backend.services.slack_service.SlackService", return_value=svc),
    ):
        agent.run()
    assert seen == [None]


# ─── Mention severity ─────────────────────────────────────────────────────────


def test_mention_in_customer_facing_channel_is_high_severity():
    from backend.signals.slack import SlackSignalSource

    rep = "rep1@acme.example"
    now = datetime(2026, 5, 13, 15, 0, tzinfo=timezone.utc)
    mention = _msg(
        channel_id="C-DEALS-ACME",
        channel_name="deals-acme",
        text="<@U-REP> can you join the call at 3?",
        user_name="Sarah",
    )
    svc = _service_mock(mentions=[mention])
    src = SlackSignalSource(slack_service=svc, now=now)

    sigs = src.fetch(rep, since=now - timedelta(hours=24))

    assert len(sigs) == 1
    s = sigs[0]
    assert s.signal_type == "mention"
    assert s.source == "slack"
    assert s.severity_hint == "high"
    assert s.entity_refs["channel_id"] == "C-DEALS-ACME"
    assert s.entity_refs["message_ts"] == mention.ts
    assert "contact_email" not in s.entity_refs
    assert s.payload["is_dm"] is False
    assert s.payload["channel_name"] == "deals-acme"
    assert s.payload["lookback_widened_to_friday"] is False
    assert "Sarah" in s.summary
    assert "#deals-acme" in s.summary
    assert len(s.summary) <= 140


def test_mention_in_unknown_channel_is_medium_severity():
    from backend.signals.slack import SlackSignalSource

    rep = "rep1@acme.example"
    now = datetime(2026, 5, 13, 15, 0, tzinfo=timezone.utc)
    mention = _msg(
        channel_id="C-RANDOM",
        channel_name="random",
        text="<@U-REP> heads up on the offsite",
    )
    svc = _service_mock(mentions=[mention])
    src = SlackSignalSource(slack_service=svc, now=now)

    sigs = src.fetch(rep, since=now - timedelta(hours=24))

    assert len(sigs) == 1
    assert sigs[0].severity_hint == "medium"
    assert sigs[0].payload["is_dm"] is False


def test_dm_is_high_severity():
    from backend.signals.slack import SlackSignalSource

    rep = "rep1@acme.example"
    now = datetime(2026, 5, 13, 15, 0, tzinfo=timezone.utc)
    dm = _msg(
        channel_id="D-DM1",  # DM channel id prefix
        channel_name=None,  # Slack returns no name for DMs
        user_name="Jane Buyer",
        text="ping — got a sec?",
    )
    svc = _service_mock(mentions=[dm])
    src = SlackSignalSource(slack_service=svc, now=now)

    sigs = src.fetch(rep, since=now - timedelta(hours=24))

    assert len(sigs) == 1
    s = sigs[0]
    assert s.severity_hint == "high"
    assert s.payload["is_dm"] is True
    assert "DM" in s.summary


# ─── Lookback widening propagates into Slack fetch ────────────────────────────


def test_fetch_marks_lookback_widened_on_monday_early_morning():
    from backend.signals.slack import SlackSignalSource

    rep = "rep1@acme.example"
    # Mon 02:00 UTC = Sun 20:00 Denver → widens.
    now = datetime(2026, 5, 11, 2, 0, tzinfo=timezone.utc)
    mention = _msg()
    svc = _service_mock(mentions=[mention])
    src = SlackSignalSource(slack_service=svc, now=now, tz_name="America/Denver")

    sigs = src.fetch(rep, since=now - timedelta(hours=24))

    assert len(sigs) == 1
    assert sigs[0].payload["lookback_widened_to_friday"] is True
    # Slack service was called with the widened `since`, not the original.
    called_since = svc.get_mentions.call_args.kwargs["since"]
    assert called_since < now - timedelta(hours=24)


# ─── Unknown user handling ────────────────────────────────────────────────────


def test_unknown_user_email_returns_empty_no_exception():
    from backend.signals.slack import SlackSignalSource

    rep = "unknown@nowhere.example"
    now = datetime(2026, 5, 13, 15, 0, tzinfo=timezone.utc)
    svc = _service_mock(mentions=[_msg()], slack_user_id="")  # no resolved uid
    src = SlackSignalSource(slack_service=svc, now=now)

    sigs = src.fetch(rep, since=now - timedelta(hours=24))

    assert sigs == []
    svc.get_mentions.assert_not_called()


# ─── Protocol conformance ─────────────────────────────────────────────────────


def test_slack_source_satisfies_signal_source_protocol():
    from backend.signals import SignalSource
    from backend.signals.slack import SlackSignalSource

    src = SlackSignalSource(slack_service=_service_mock())
    assert isinstance(src, SignalSource)

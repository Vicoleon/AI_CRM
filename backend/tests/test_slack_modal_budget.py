"""A Slack modal trigger is single-use and expires within seconds."""

from __future__ import annotations

import httpx
import pytest

from backend.services.slack_service import SlackAPIError, SlackBotClient


def test_views_open_uses_one_short_attempt(monkeypatch):
    attempts = []

    def timeout(*args, **kwargs):
        attempts.append(kwargs["timeout"])
        raise httpx.TimeoutException("modal trigger spent")

    monkeypatch.setattr(httpx, "post", timeout)
    bot = SlackBotClient(bot_token="xoxb-test", max_retries=3, timeout=10.0)
    with pytest.raises(SlackAPIError) as failure:
        bot.open_view("trigger", {"type": "modal"})
    assert failure.value.error == "timeout"
    assert attempts == [2.0]

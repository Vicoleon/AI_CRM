"""``SlackBotClient`` per-call budget: ``timeout`` and ``max_retries``.

What's New posts to a channel from a Slack button click, and Slack gives an
interaction 3 seconds. The client therefore takes an optional ``timeout`` and
``max_retries`` (``backend/services/slack_service.py``). Pinned here:

* the defaults are unchanged for every existing caller (10 s, 3 attempts);
* the caller's timeout reaches the HTTP call;
* ``max_retries=1`` means ONE attempt and no back-off sleep:
  ``chat.postMessage`` is not idempotent, so a post that timed out may have
  landed and must never be sent again;
* a timeout is reported as its own code (``timeout``), distinct from any
  other transport error (``http_error``), so the caller can say "check the
  channel before posting again";
* the client never sleeps after its last attempt (it would only eat the
  caller's budget before raising the same error).

``httpx`` and ``time.sleep`` are patched by string target; nothing leaves the
process.
"""

from __future__ import annotations

import httpx
import pytest

from backend.services import slack_service
from backend.services.slack_service import SlackAPIError, SlackBotClient

POST = "backend.services.slack_service.httpx.post"
GET = "backend.services.slack_service.httpx.get"
SLEEP = "backend.services.slack_service.time.sleep"


class _Resp:
    def __init__(self, status=200, data=None, headers=None):
        self.status_code = status
        self._data = data if data is not None else {"ok": True, "ts": "1.1"}
        self.headers = headers or {}

    def json(self):
        return self._data


@pytest.fixture()
def sleeps(monkeypatch):
    recorded: list[float] = []
    monkeypatch.setattr(SLEEP, recorded.append)
    return recorded


def _transport(monkeypatch, outcomes, *, method=POST):
    """Each call takes the next outcome: an exception to raise or a _Resp."""
    calls: list[dict] = []
    queue = list(outcomes)

    def fake(url, **kwargs):
        calls.append({"url": url, **kwargs})
        outcome = queue.pop(0) if queue else _Resp()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(method, fake)
    return calls


def test_the_defaults_are_unchanged_for_existing_callers():
    bot = SlackBotClient(bot_token="xoxb-test")
    assert bot._timeout == slack_service.DEFAULT_TIMEOUT == 10.0
    assert bot._max_retries == slack_service._BOT_MAX_RETRIES == 3


def test_the_callers_timeout_reaches_the_http_call(monkeypatch, sleeps):
    calls = _transport(monkeypatch, [_Resp()])

    SlackBotClient(bot_token="xoxb-test", timeout=2.0).post_message("C1", text="hi")

    assert calls[0]["timeout"] == 2.0


def test_the_default_timeout_reaches_gets_too(monkeypatch, sleeps):
    calls = _transport(
        monkeypatch, [_Resp(data={"ok": True, "user": {"id": "U1"}})], method=GET
    )

    SlackBotClient(bot_token="xoxb-test").lookup_user_id("a@acme.example")

    assert calls[0]["timeout"] == 10.0


def test_one_attempt_means_one_post_and_no_sleep_on_a_timeout(monkeypatch, sleeps):
    calls = _transport(monkeypatch, [httpx.ReadTimeout("no answer")])
    bot = SlackBotClient(bot_token="xoxb-test", timeout=2.0, max_retries=1)

    with pytest.raises(SlackAPIError) as exc:
        bot.post_message("C1", text="hello")

    assert exc.value.error == "timeout"
    assert len(calls) == 1, "a post that may have landed is never sent again"
    assert sleeps == []


def test_a_connect_timeout_is_a_timeout_too(monkeypatch, sleeps):
    _transport(monkeypatch, [httpx.ConnectTimeout("no route")])
    bot = SlackBotClient(bot_token="xoxb-test", max_retries=1)

    with pytest.raises(SlackAPIError) as exc:
        bot.post_message("C1", text="hello")

    assert exc.value.error == "timeout"


def test_other_transport_errors_keep_their_own_code(monkeypatch, sleeps):
    _transport(monkeypatch, [httpx.ConnectError("refused")])
    bot = SlackBotClient(bot_token="xoxb-test", max_retries=1)

    with pytest.raises(SlackAPIError) as exc:
        bot.post_message("C1", text="hello")

    assert exc.value.error == "http_error"


def test_the_default_client_still_retries_and_never_sleeps_after_the_last(
    monkeypatch, sleeps
):
    calls = _transport(
        monkeypatch,
        [httpx.ConnectError("1"), httpx.ConnectError("2"), httpx.ConnectError("3")],
    )
    bot = SlackBotClient(bot_token="xoxb-test")

    with pytest.raises(SlackAPIError):
        bot.post_message("C1", text="hello")

    assert len(calls) == 3
    assert sleeps == [1, 2], "back-off between attempts, none after the last"


def test_a_retry_that_recovers_returns_the_answer(monkeypatch, sleeps):
    calls = _transport(monkeypatch, [httpx.ConnectError("blip"), _Resp()])

    data = SlackBotClient(bot_token="xoxb-test").post_message("C1", text="hello")

    assert data["ok"] is True and len(calls) == 2 and sleeps == [1]


def test_a_rate_limit_on_the_only_attempt_raises_without_waiting(monkeypatch, sleeps):
    _transport(monkeypatch, [_Resp(status=429, headers={"Retry-After": "30"})])
    bot = SlackBotClient(bot_token="xoxb-test", max_retries=1)

    with pytest.raises(SlackAPIError) as exc:
        bot.post_message("C1", text="hello")

    assert exc.value.error == "ratelimited"
    assert sleeps == [], "waiting 30 s would blow the 3 s button budget"


def test_a_ratelimited_body_on_the_only_attempt_raises_without_waiting(
    monkeypatch, sleeps
):
    _transport(
        monkeypatch,
        [
            _Resp(
                data={"ok": False, "error": "ratelimited"}, headers={"Retry-After": "5"}
            )
        ],
    )
    bot = SlackBotClient(bot_token="xoxb-test", max_retries=1)

    with pytest.raises(SlackAPIError) as exc:
        bot.post_message("C1", text="hello")

    assert exc.value.error == "ratelimited" and sleeps == []


def test_a_slack_refusal_is_raised_at_once_whatever_the_budget(monkeypatch, sleeps):
    calls = _transport(
        monkeypatch, [_Resp(data={"ok": False, "error": "not_in_channel"})]
    )

    with pytest.raises(SlackAPIError) as exc:
        SlackBotClient(bot_token="xoxb-test").post_message("C1", text="hello")

    assert exc.value.error == "not_in_channel"
    assert len(calls) == 1 and sleeps == []


@pytest.mark.parametrize("value", [0, -3])
def test_a_budget_of_no_attempts_still_makes_one(monkeypatch, sleeps, value):
    calls = _transport(monkeypatch, [_Resp()])

    SlackBotClient(bot_token="xoxb-test", max_retries=value).post_message(
        "C1", text="hello"
    )

    assert len(calls) == 1

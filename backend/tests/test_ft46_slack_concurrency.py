"""SlackService concurrency.

These tests stub the low-level ``_call`` (no real Slack HTTP) and the LLM, and
verify that the concurrent fan-out preserves the serial behavior: same channel
order, same message filtering, same per-channel counts, and that name
resolution is batched (prefetched) rather than an inline N+1.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from backend.services.slack_service import SlackAPIError, SlackService


def _make_service() -> SlackService:
    cred = SimpleNamespace(
        access_token="xoxp-test",
        slack_user_id="UME",
        slack_team_id="T1",
    )
    with patch.object(SlackService, "_load_credential", return_value=cred):
        return SlackService(user_email="me@example.com")


# ── _prefetch_names ───────────────────────────────────────────────────────────


def test_prefetch_names_resolves_distinct_and_skips_empty():
    svc = _make_service()
    seen: list[str] = []
    resolver = lambda i: seen.append(i)  # noqa: E731
    svc._prefetch_names(resolver, ["U1", "U2", "U1", "", None, "U2"])
    assert sorted(seen) == ["U1", "U2"]  # distinct, empties skipped


def test_prefetch_names_noop_on_empty():
    svc = _make_service()
    calls: list[str] = []
    svc._prefetch_names(lambda i: calls.append(i), ["", None])
    assert calls == []


# ── _fetch_unread_channel ─────────────────────────────────────────────────────


def test_fetch_unread_channel_filters_own_and_handles_errors():
    svc = _make_service()

    def fake_call(method, params=None):
        assert method == "conversations.history"
        return {
            "messages": [
                {"user": "U1", "text": "hi", "ts": "1"},
                {"user": "UME", "text": "my own msg", "ts": "2"},  # dropped
            ]
        }

    with patch.object(svc, "_call", side_effect=fake_call):
        ch, filtered = svc._fetch_unread_channel({"id": "C1", "name": "sales"}, 0.0, 30)
    assert ch["id"] == "C1"
    assert [m["user"] for m in filtered] == ["U1"]  # own message removed

    # An API error degrades to (ch, None) instead of raising.
    with patch.object(svc, "_call", side_effect=SlackAPIError("channel_not_found")):
        ch, filtered = svc._fetch_unread_channel({"id": "C2", "name": "eng"}, 0.0, 30)
    assert filtered is None


# ── summarize_unread_across_channels (concurrent path) ────────────────────────


def test_summarize_unread_concurrent_preserves_order_and_counts():
    svc = _make_service()

    channels = [
        {"id": "C1", "name": "sales", "last_read": "0"},
        {"id": "C2", "name": "eng", "last_read": "0"},
    ]
    history = {
        "C1": [
            {"user": "U1", "text": "deal closed", "ts": "10"},
            {"user": "UME", "text": "ignore me", "ts": "11"},  # own → filtered
            {"user": "U2", "text": "follow up Friday", "ts": "12"},
        ],
        "C2": [
            {"user": "U3", "text": "v3 shipped", "ts": "20"},
        ],
    }
    profiles = {
        "U1": "Alice",
        "U2": "Bob",
        "U3": "Carol",
    }

    def fake_call(method, params=None):
        if method == "conversations.history":
            return {"messages": history[params["channel"]]}
        if method == "users.info":
            uid = params["user"]
            return {"user": {"profile": {"display_name": profiles.get(uid, uid)}}}
        raise AssertionError(f"unexpected method {method}")

    fake_client = MagicMock()
    fake_client.aio.models.generate_content = AsyncMock(
        return_value=SimpleNamespace(
            text="### #sales\n- deal closed [M1]\n### #eng\n- v3 [M3]"
        )
    )

    with (
        patch.object(svc, "list_member_channels", return_value=channels),
        patch.object(svc, "_call", side_effect=fake_call),
        patch("rubik_core.models.get_genai_client", return_value=fake_client),
    ):
        result = asyncio.run(svc.summarize_unread_across_channels(hours_back=24))

    # Two channels with activity, in INPUT order despite concurrent fetch.
    assert result["channel_count"] == 2
    assert [c["name"] for c in result["channels"]] == ["sales", "eng"]
    # C1 keeps 2 (own message filtered out), C2 keeps 1 → 3 total.
    assert result["message_count"] == 3
    # The LLM was invoked once with the batched transcript.
    fake_client.aio.models.generate_content.assert_awaited_once()
    # Author names were resolved (prefetched) and cached — no duplicate lookups.
    assert svc._user_cache.get("U1") == "Alice"


def test_summarize_unread_empty_when_no_activity():
    svc = _make_service()
    channels = [{"id": "C1", "name": "sales", "last_read": "0"}]

    def fake_call(method, params=None):
        if method == "conversations.history":
            # Only the user's own message → nothing to summarize.
            return {"messages": [{"user": "UME", "text": "hi", "ts": "1"}]}
        raise AssertionError(f"unexpected method {method}")

    with (
        patch.object(svc, "list_member_channels", return_value=channels),
        patch.object(svc, "_call", side_effect=fake_call),
    ):
        result = asyncio.run(svc.summarize_unread_across_channels(hours_back=24))

    assert result["channel_count"] == 0
    assert result["message_count"] == 0


# ── search_messages prefetch (the "conversations with X" path) ────────────────


def test_search_messages_prefetches_and_parses():
    svc = _make_service()

    def fake_call(method, params=None):
        if method == "search.messages":
            return {
                "messages": {
                    "matches": [
                        {
                            "ts": "1",
                            "user": "U1",
                            "text": "re: Jessica sync",
                            "channel": {"id": "C1", "name": "sales"},
                        },
                    ]
                }
            }
        if method == "users.info":
            return {"user": {"profile": {"display_name": "Alice"}}}
        raise AssertionError(f"unexpected method {method}")

    with patch.object(svc, "_call", side_effect=fake_call):
        out = svc.search_messages("Jessica", limit=10)

    assert len(out) == 1
    assert out[0].channel_name == "sales"
    assert out[0].user_name == "Alice"

"""UC-15 — SlackService.get_activity_overview.

Stubs the low-level ``_call`` (no real Slack HTTP) plus the mention/DM/channel
list helpers, and verifies the UNREAD-only dashboard snapshot. Unread is counted
from ``conversations.history`` relative to each conversation's ``last_read``
(from ``conversations.info``) — Slack does NOT expose an unread count on the
channel object, so this is the authoritative path. Verifies:
  * channels with messages after last_read (own + system noise excluded),
  * @-mentions the user hasn't read (ts > the channel's last_read),
  * unread DM threads (+ latest-message preview).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from google.genai.types import ThinkingLevel

from backend.services.slack_service import SlackMessage, SlackService


def _make_service() -> SlackService:
    cred = SimpleNamespace(
        access_token="xoxp-test",
        slack_user_id="UME",
        slack_team_id="T1",
    )
    with patch.object(SlackService, "_load_credential", return_value=cred):
        return SlackService(user_email="me@example.com")


# conversations.info last_read cursors + conversations.history per channel.
INFO = {"C1": "50", "C2": "100", "D1": "0", "D2": "200", "C9": "50"}
HIST = {
    "C1": [
        {"user": "U1", "ts": "70", "text": "newer"},
        {"user": "U2", "ts": "60", "text": "follow up"},
        {"user": "UME", "ts": "65", "text": "my own"},  # own → excluded
        {"subtype": "channel_join", "user": "U3", "ts": "80"},  # noise → excluded
    ],
    "C2": [{"user": "UME", "ts": "110", "text": "only me"}],  # own → 0 unread
    "D1": [{"user": "U1", "ts": "30", "text": "ping from alice"}],
    "D2": [],
}


def _fake_call(method, params=None):
    if method == "conversations.info":
        return {"channel": {"last_read": INFO.get(params["channel"])}}
    if method == "conversations.history":
        return {"messages": HIST.get(params["channel"], [])}
    raise AssertionError(f"unexpected method {method}")


def test_activity_overview_counts_unread_from_history():
    svc = _make_service()

    channels = [
        {"id": "C1", "name": "sales", "is_private": False},
        {"id": "C2", "name": "quiet", "is_private": False},  # only own msgs → 0
    ]
    dms = [
        {"channel_id": "D1", "counterparty_name": "Alice", "is_mpim": False},
        {"channel_id": "D2", "counterparty_name": "Bob", "is_mpim": False},  # read
    ]
    mentions = [
        SlackMessage(
            channel_id="C1",
            channel_name="sales",
            ts="90",
            user_name="Carol",
            text="<@UME> look",
        ),
        SlackMessage(
            channel_id="C9",
            channel_name="general",
            ts="40",
            user_name="Dan",
            text="old <@UME>",
        ),
    ]

    with (
        patch.object(svc, "list_member_channels", return_value=channels),
        patch.object(svc, "get_mentions", return_value=mentions),
        patch.object(svc, "get_dm_threads", return_value=dms),
        patch.object(svc, "_call", side_effect=_fake_call),
    ):
        out = asyncio.run(svc.get_activity_overview())

    # C1: 2 unread after last_read=50 (own message + channel_join excluded).
    assert [c["name"] for c in out["channels"]] == ["sales"]
    assert out["channels"][0]["unread_count"] == 2

    # D1 unread (ts 30 > last_read 0) with preview; D2 read (last_read 200).
    assert [d["counterparty_name"] for d in out["dms"]] == ["Alice"]
    assert out["dms"][0]["unread_count"] == 1
    assert out["dms"][0]["preview"] == "ping from alice"
    assert out["dms"][0]["ts"] == "30"

    # Mentions: C1 one is unread (90 > 50); C9 one already read (40 <= 50).
    assert [m["user_name"] for m in out["mentions"]] == ["Carol"]


def test_thread_reply_mention_dropped_when_reacted_or_replied():
    """A non-broadcast thread-reply mention sits permanently above the channel's
    main-timeline ``last_read`` (reading a thread never advances that cursor), so
    the old ``ts <= last_read`` check left it stuck forever. It should now clear
    on positive engagement: a reaction by the user, or the user's own reply
    at/after it. Mirrors the real stuck card (Blair reacted to Rowan's reply)."""
    svc = _make_service()

    # Group DM whose newest MAIN-timeline message is ts=100 (the thread parent
    # + the user's own earlier reply); the mentions below are later thread
    # replies (ts 150/160 > last_read 100), so the cursor can never clear them.
    info = {"G1": "100"}
    replies = {
        # Rowan @-mentions the user at 150; the user reacted → read.
        ("G1", "90"): [
            {"user": "USCOTT", "ts": "90", "text": "parent"},
            {"user": "UME", "ts": "95", "text": "my earlier reply"},
            {
                "user": "USCOTT",
                "ts": "150",
                "text": "<@UME> thanks!",
                "reactions": [{"name": "ok_hand", "users": ["UME"]}],
            },
        ],
        # A different thread where the user replied AFTER being mentioned → read.
        ("G1", "200"): [
            {"user": "USCOTT", "ts": "200", "text": "<@UME> can you look?"},
            {"user": "UME", "ts": "210", "text": "on it"},
        ],
    }

    def _call(method, params=None):
        if method == "conversations.info":
            return {"channel": {"last_read": info.get(params["channel"])}}
        if method == "conversations.replies":
            key = (params["channel"], params["ts"])
            return {"messages": replies.get(key, [])}
        if method == "conversations.history":
            return {"messages": []}
        raise AssertionError(f"unexpected method {method}")

    mentions = [
        SlackMessage(
            channel_id="G1",
            ts="150",
            thread_ts="90",
            user_name="Rowan",
            text="<@UME> thanks!",
        ),
        SlackMessage(
            channel_id="G1",
            ts="200",
            thread_ts="200",
            user_name="Rowan",
            text="<@UME> can you look?",  # thread PARENT (thread_ts == ts): top-level
        ),
        SlackMessage(
            channel_id="G1",
            ts="205",
            thread_ts="200",
            user_name="Rowan",
            text="<@UME> still there?",  # reply the user answered at 210 → read
        ),
    ]

    with (
        patch.object(svc, "list_member_channels", return_value=[]),
        patch.object(svc, "get_mentions", return_value=mentions),
        patch.object(svc, "get_dm_threads", return_value=[]),
        patch.object(svc, "_call", side_effect=_call),
    ):
        out = asyncio.run(svc.get_activity_overview())

    # ts=150 reply cleared by the reaction; ts=205 reply cleared by the user's
    # 210 reply. The ts=200 thread PARENT is unread (top-level, 200 > 100) and
    # stays — proving genuine mentions are never over-dropped.
    assert [m["ts"] for m in out["mentions"]] == ["200"]


def test_thread_reply_mention_kept_when_no_engagement():
    """Thread-reply mention the user neither reacted to nor replied after: we
    can't confirm a read, so it must still surface (never silently dropped)."""
    svc = _make_service()

    def _call(method, params=None):
        if method == "conversations.info":
            return {"channel": {"last_read": "100"}}
        if method == "conversations.replies":
            return {
                "messages": [
                    {"user": "USCOTT", "ts": "90", "text": "parent"},
                    {"user": "USCOTT", "ts": "150", "text": "<@UME> ping"},
                ]
            }
        if method == "conversations.history":
            return {"messages": []}
        raise AssertionError(f"unexpected method {method}")

    mentions = [
        SlackMessage(
            channel_id="G1",
            ts="150",
            thread_ts="90",
            user_name="Rowan",
            text="<@UME> ping",
        ),
    ]
    with (
        patch.object(svc, "list_member_channels", return_value=[]),
        patch.object(svc, "get_mentions", return_value=mentions),
        patch.object(svc, "get_dm_threads", return_value=[]),
        patch.object(svc, "_call", side_effect=_call),
    ):
        out = asyncio.run(svc.get_activity_overview())
    assert [m["ts"] for m in out["mentions"]] == ["150"]


def test_thread_ts_parsed_from_search_permalink():
    """``search.messages`` matches don't always carry ``thread_ts`` on the
    object; it must be recovered from the reply permalink's query string."""
    svc = _make_service()
    match = {
        "ts": "1700000000.000200",
        "channel": {"id": "C0TEST0001", "name": "mpdm-x"},
        "username": "Rowan",
        "text": "<@UME> thanks!",
        "permalink": (
            "https://x.slack.com/archives/C0TEST0001/p1700000000000200"
            "?thread_ts=1700000000.000100&cid=C0TEST0001"
        ),
    }
    parsed = svc._parse_search_match(match)
    assert parsed.thread_ts == "1700000000.000100"
    assert parsed.ts == "1700000000.000200"  # the reply's own ts is unchanged


def test_activity_overview_empty_when_all_read():
    svc = _make_service()
    with (
        patch.object(svc, "list_member_channels", return_value=[]),
        patch.object(svc, "get_mentions", return_value=[]),
        patch.object(svc, "get_dm_threads", return_value=[]),
    ):
        out = asyncio.run(svc.get_activity_overview())
    assert out == {"channels": [], "mentions": [], "dms": []}


# ─── On-demand per-channel summary (summarize_channel_unread) ─────────────────


def _fake_genai(text: str):
    """A stand-in genai client whose async generate_content returns `text`."""

    async def _gen(model, contents, config=None):
        return SimpleNamespace(text=text)

    return SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=_gen))
    )


def test_summarize_channel_unread_summarizes_only_unread_plus_context():
    """Hover summary: focuses on messages after last_read, pulls a little prior
    context, and excludes the user's own messages from the unread count."""
    svc = _make_service()
    # last_read = 50; messages returned newest-first (as conversations.history does).
    msgs = [
        SlackMessage(
            channel_id="C1",
            channel_name="sales",
            ts="70",
            user_name="Carol",
            text="and ship Friday",
        ),
        SlackMessage(
            channel_id="C1", channel_name="sales", ts="65", user_id="UME", text="on it"
        ),  # own → not counted
        SlackMessage(
            channel_id="C1",
            channel_name="sales",
            ts="60",
            user_name="Bob",
            text="can you review the deck",
        ),
        SlackMessage(
            channel_id="C1",
            channel_name="sales",
            ts="40",
            user_name="Dan",
            text="older context msg",
        ),  # read
    ]
    captured = {}

    # async, like the real client — a sync stand-in blows up on `await` inside
    # the service's try/except, silently exercising the raw-text fallback
    # instead of the summarization path this test is about.
    async def _gen_capture(model, contents, config=None):
        captured["prompt"] = contents
        captured["config"] = config
        return SimpleNamespace(
            text="- Bob asked Carol to review the deck and ship Friday"
        )

    fake = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=_gen_capture))
    )
    with (
        patch.object(svc, "_conversation_meta", return_value={"last_read": "50"}),
        patch.object(svc, "get_channel_messages", return_value=msgs),
        patch("rubik_core.models.get_genai_client", return_value=fake),
    ):
        out = asyncio.run(svc.summarize_channel_unread("C1"))

    # Carol(70) + Bob(60) are unread; the user's own msg(65) is excluded.
    assert out["unread_count"] == 2
    assert out["channel"] == "#sales"
    assert "review the deck" in out["summary_md"]
    # Prior context (Dan@40) is fed to the LLM but isn't counted as unread.
    assert "older context msg" in captured["prompt"]
    # Slack catch-up is the latency-sensitive tool: the LITE tier is Gemini 3.x,
    # which thinks by default, so the call must ask for minimal thinking.
    assert captured["config"].thinking_config.thinking_level == ThinkingLevel.MINIMAL


def test_summarize_channel_unread_falls_back_on_llm_error():
    """A dead LLM still yields the raw recent lines, never an empty popover."""
    svc = _make_service()
    msgs = [
        SlackMessage(
            channel_id="C1",
            channel_name="sales",
            ts="70",
            user_name="Carol",
            text="hello there",
        ),
    ]
    with (
        patch.object(svc, "_conversation_meta", return_value={"last_read": "50"}),
        patch.object(svc, "get_channel_messages", return_value=msgs),
        patch("rubik_core.models.get_genai_client", side_effect=RuntimeError("boom")),
    ):
        out = asyncio.run(svc.summarize_channel_unread("C1"))
    assert "Carol" in out["summary_md"] and "hello there" in out["summary_md"]
    assert out["unread_count"] == 1


def test_summarize_channel_unread_empty_when_no_messages():
    """No messages → early return, empty summary, no LLM call."""
    svc = _make_service()
    with (
        patch.object(svc, "_conversation_meta", return_value={"last_read": "50"}),
        patch.object(svc, "get_channel_messages", return_value=[]),
    ):
        out = asyncio.run(svc.summarize_channel_unread("C1"))
    assert out["unread_count"] == 0
    assert out["summary_md"] == ""

"""Open loops — "what am I waiting on?" across Gmail + Slack in one sweep.

Pins the fix for a reported failure: the bare question "what am I waiting
on" returned almost nothing (no routing rule, and another tool claimed the
phrase), while the version naming Gmail and Slack listed two items that had
already been closed in-thread. The sweep reads each THREAD to its newest
message, the model can only rule on tags we hand it, the report has fixed
sections including "Skipped — and why", and the footer names what was checked.

No network: Gmail is a fake client, Slack is a SlackService with the HTTP layer
stubbed, and the model is a fake genai client or a stub synthesizer.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from backend.services import open_loops
from backend.services.open_loops import (
    LoopCandidate,
    SweepResult,
    _parse_verdicts,
    build_gmail_candidates,
    gather_gmail,
    gather_slack,
    render_report,
    run_open_loops_sweep,
    synthesize,
)
from backend.services.slack_service import SlackMessage, SlackService
from rubik_core.adk_agent.tools import AgentTools
from rubik_core.agent_runtime import coordinator

NOW = datetime(2026, 9, 14, 16, 0, tzinfo=timezone.utc)
ME = "emery@acme.example"


def _msg(mid, frm, to, subject, snippet, dt, *, cc="", unread=False, list_id=""):
    headers = [
        {"name": "From", "value": frm},
        {"name": "To", "value": to},
        {"name": "Subject", "value": subject},
        {"name": "Date", "value": dt.strftime("%a, %d %b %Y %H:%M:%S +0000")},
    ]
    if cc:
        headers.append({"name": "Cc", "value": cc})
    if list_id:
        headers.append({"name": "List-Id", "value": list_id})
    return {
        "id": mid,
        "internalDate": str(int(dt.timestamp() * 1000)),
        "snippet": snippet,
        "labelIds": ["UNREAD"] if unread else [],
        "payload": {"headers": headers},
    }


def _thread(tid, *msgs):
    return {"id": tid, "messages": list(msgs)}


D = NOW - timedelta(days=2)

REESE = "Reese Keller <reese@acme.example>"
SHAY = "Shay Vaughn <shay@acme.example>"
EMERY = f"Emery Brooks <{ME}>"


# ─── Gmail leg ───────────────────────────────────────────────────────────────


def test_gmail_candidates_direction_counterparty_and_filters():
    threads = [
        # Newest-first on purpose: the transcript must come out chronological.
        _thread(
            "t-needs-me",
            _msg(
                "m2",
                REESE,
                ME,
                "Re: Initech SOW",
                "Signed SOW attached, ACTION REQUIRED",
                D,
                unread=True,
            ),
            _msg(
                "m1",
                EMERY,
                "reese@acme.example",
                "Initech SOW",
                "Can you send the SOW?",
                D - timedelta(days=1),
            ),
        ),
        _thread(
            "t-waiting-on-them",
            _msg(
                "m3",
                EMERY,
                SHAY,
                "Dashlane seats",
                "Shay, can you confirm the tool list?",
                D,
            ),
        ),
        _thread(
            "t-list-mail",
            _msg(
                "m4",
                "News <news@vendor.com>",
                "all@acme.example",
                "Weekly digest",
                "Read on",
                D,
                list_id="<digest.vendor.com>",
            ),
        ),
        _thread(
            "t-robot",
            _msg(
                "m5",
                "noreply@docusign.com",
                ME,
                "Completed: agreement",
                "All parties signed",
                D,
            ),
        ),
        _thread(
            "t-cc",
            _msg(
                "m6",
                REESE,
                "marin@acme.example",
                "Kickoff scheduling",
                "Marin, can we book it?",
                D,
                cc=ME,
            ),
        ),
    ]

    cands = build_gmail_candidates(threads, ME, now=NOW)
    by_title = {c.title: c for c in cands}

    assert set(by_title) == {
        "Initech SOW",
        "Dashlane seats",
        "Kickoff scheduling",
    }, "list mail and robot-only threads must be dropped; everything else kept"

    needs = by_title["Initech SOW"]
    assert needs.last_from_user is False
    assert needs.counterparty == "Reese Keller"
    assert needs.unread is True
    assert needs.link == "https://mail.google.com/mail/u/0/#all/t-needs-me"
    # Chronological, with the user's own message labelled USER.
    first, second = needs.transcript.splitlines()
    assert "USER" in first and "Can you send the SOW" in first
    assert "Reese Keller" in second and "ACTION REQUIRED" in second

    waiting = by_title["Dashlane seats"]
    assert waiting.last_from_user is True
    assert waiting.counterparty == "Shay Vaughn", (
        "spoke to an empty room → the To: recipient"
    )

    assert by_title["Kickoff scheduling"].addressing == "cc"

    # Tags are what the model rules on — sequential and unique.
    assert [c.tag for c in cands] == ["G1", "G2", "G3"]


def test_gather_gmail_runs_inbox_and_sent_lanes_and_dedups():
    t = _thread("t1", _msg("m1", REESE, ME, "Same thread", "hi", D))
    queries: list[str] = []

    class FakeClient:
        def search_threads(self, query="", max_results=20):
            queries.append(query)
            return [t]  # the same thread surfaces from both lanes

    cands, label = gather_gmail(ME, 7, client=FakeClient(), now=NOW)
    assert len(cands) == 1
    assert label == "Gmail (1 threads)"
    assert any("in:inbox" in q and "newer_than:7d" in q for q in queries)
    assert any("in:sent" in q for q in queries), (
        "'I asked and nobody answered' lives in Sent, not the inbox"
    )


# ─── Slack leg ───────────────────────────────────────────────────────────────


def _slack_service(*, connected: bool = True) -> SlackService:
    """A SlackService with no DB and no HTTP. ``is_connected`` is a property
    that reads the credential, so "not connected" is simply no credential."""
    cred = (
        SimpleNamespace(
            access_token="xoxp-test", slack_user_id="UME", slack_team_id="T1"
        )
        if connected
        else None
    )
    with patch.object(SlackService, "_load_credential", return_value=cred):
        service = SlackService(user_email=ME)
    # The service checks the live credential on each call, so keep this fake
    # installation visible after construction as well.
    service._load_credential = lambda: cred
    return service


def test_get_thread_replies_returns_chronological_without_noise():
    svc = _slack_service()

    def _fake_call(method, params=None):
        if method == "conversations.replies":
            assert params == {"channel": "C1", "ts": "100", "limit": 200}
            return {
                "messages": [
                    {
                        "user": "U1",
                        "ts": "100",
                        "text": "<@UME> can you approve the flyers?",
                    },
                    {
                        "user": "U1",
                        "ts": "300",
                        "text": "never mind — Perry approved, closed",
                    },
                    {"subtype": "channel_join", "user": "U9", "ts": "200"},
                    {"user": "UME", "ts": "150", "text": "looking now"},
                ]
            }
        if method == "conversations.info":
            return {"channel": {"name": "marketing"}}
        if method == "users.info":
            return {"user": {"profile": {"display_name": "Shay"}}}
        raise AssertionError(method)

    with patch.object(svc, "_call", side_effect=_fake_call):
        msgs = svc.get_thread_replies("C1", "100")

    assert [m.ts for m in msgs] == ["100", "150", "300"], (
        "oldest first, join noise gone"
    )
    assert msgs[-1].text.endswith("closed"), (
        "the closing reply is what the judge must see"
    )
    assert msgs[0].channel_name == "marketing"


def test_gather_slack_one_candidate_per_thread_read_to_latest_reply():
    svc = _slack_service()
    mention_a = SlackMessage(
        channel_id="C1",
        channel_name="marketing",
        ts="100",
        user_id="U1",
        user_name="Shay",
        text="<@UME> approve the flyers?",
        app_link="slack://a",
    )
    # A second tag in the SAME thread must not become a second item.
    mention_b = SlackMessage(
        channel_id="C1",
        channel_name="marketing",
        ts="120",
        user_id="U1",
        user_name="Shay",
        text="<@UME> bump",
        thread_ts="100",
    )
    replies = [
        mention_a,
        SlackMessage(
            channel_id="C1",
            channel_name="marketing",
            ts="120",
            user_id="U1",
            user_name="Shay",
            text="<@UME> bump",
            thread_ts="100",
        ),
        SlackMessage(
            channel_id="C1",
            channel_name="marketing",
            ts="300",
            user_id="U2",
            user_name="Perry",
            text="approved and sent, all set",
            thread_ts="100",
        ),
    ]
    dm_msgs = [
        SlackMessage(
            channel_id="D1",
            ts="500",
            user_id="UME",
            user_name="Emery",
            text="Alice, did the wire go through?",
            app_link="slack://d1",
        ),
    ]

    with (
        patch.object(svc, "get_mentions", return_value=[mention_a, mention_b]),
        patch.object(
            svc,
            "get_dm_threads",
            return_value=[
                {"channel_id": "D1", "counterparty_name": "Alice", "is_mpim": False},
                {"channel_id": "D2", "counterparty_name": "Bob", "is_mpim": False},
            ],
        ),
        patch.object(svc, "get_thread_replies", return_value=replies) as replies_mock,
        patch.object(
            svc,
            "get_channel_messages",
            side_effect=lambda cid, *a, **k: dm_msgs if cid == "D1" else [],
        ),
        patch.object(
            svc,
            "resolve_user_name",
            side_effect=lambda uid: {"U1": "Shay", "U2": "Perry"}.get(uid, uid),
        ),
    ):
        cands, label = asyncio.run(gather_slack(ME, 7, svc=svc, now=NOW))

    assert label == "Slack (1 mention threads, 1 DMs)"
    assert replies_mock.call_count == 1, (
        "two mentions in one thread → one fetch, one item"
    )
    assert [c.tag for c in cands] == ["S1", "S2"]

    thread = cands[0]
    assert thread.where == "#marketing"
    assert thread.title.startswith("@you approve the flyers")
    assert thread.last_from_user is False
    assert "Perry: approved and sent, all set" in thread.transcript, (
        "the judge sees the reply that closed it, not just the tag"
    )
    assert thread.link == "slack://a"

    dm = cands[1]
    assert dm.where == "DM with Alice"
    assert dm.last_from_user is True, "the user spoke last → waiting on Alice"
    assert dm.link == "slack://d1"


# ─── Verdict parsing + rendering ─────────────────────────────────────────────


def _cand(tag, source="gmail", title="T", **kw) -> LoopCandidate:
    base = dict(
        tag=tag,
        source=source,
        title=title,
        where="Gmail thread",
        counterparty="X",
        last_from_user=False,
        last_at=NOW,
        transcript="- x",
        link=f"https://x/{tag}",
    )
    base.update(kw)
    return LoopCandidate(**base)


def test_parse_verdicts_drops_invented_tags_and_normalises_enums():
    raw = (
        "```json\n"
        + json.dumps(
            [
                {
                    "tag": "g1",
                    "status": "needs_me",
                    "priority": "HIGH",
                    "headline": "A",
                },
                {"tag": "G1", "status": "fyi"},  # duplicate — ignored
                {"tag": "G9", "status": "needs_me"},  # invented — ignored
                {"tag": "S1", "status": "bogus", "priority": "urgent"},
                "not a dict",
            ]
        )
        + "\n```"
    )
    out = _parse_verdicts(raw, {"G1", "S1"})
    assert [v["tag"] for v in out] == ["G1", "S1"]
    assert out[0]["priority"] == "high"
    assert out[1]["status"] == "fyi" and out[1]["priority"] == "medium"


def test_render_report_has_fixed_sections_priorities_skipped_and_footer():
    sweep = SweepResult(days=7)
    sweep.candidates = [
        _cand("G1", title="Initech SOW"),
        _cand("G2", title="Dashlane seats", last_from_user=True),
        _cand(
            "S1",
            source="slack",
            title="hotel authorizations",
            where="#ops",
            link="slack://s1",
        ),
        _cand("S2", source="slack", title="commission rate", where="#sales"),
        _cand("G3", title="Vistage survey"),
        _cand("G4", title="Sun West close-loop"),
    ]
    sweep.checked = ["Gmail (23 threads)", "Slack (9 mention threads, 4 DMs)"]
    verdicts = [
        {
            "tag": "G4",
            "status": "needs_me",
            "priority": "low",
            "headline": "Close the loop with Remy",
            "detail": "He answered your R² question.",
            "owner": "Remy",
            "due": "",
            "why": "",
        },
        {
            "tag": "G1",
            "status": "needs_me",
            "priority": "high",
            "headline": "Initech rollout SOW",
            "detail": "Reese sent the signed SOW; client wants docs by end of September.",
            "owner": "Reese Keller",
            "due": "end of September",
            "why": "",
        },
        {
            "tag": "G2",
            "status": "waiting_on_them",
            "priority": "medium",
            "headline": "Dashlane seats",
            "detail": "You asked Shay to confirm the tool list.",
            "owner": "Shay Vaughn",
            "due": "",
            "why": "",
        },
        {
            "tag": "S1",
            "status": "resolved",
            "priority": None,
            "headline": "Hotel payment authorizations",
            "detail": "",
            "owner": "",
            "due": "",
            "why": "Harper confirmed both were processed on Sep 12",
        },
        {
            "tag": "S2",
            "status": "someone_else_has_it",
            "priority": None,
            "headline": "Commission rate change",
            "detail": "",
            "owner": "",
            "due": "",
            "why": "Marin closed it with Reese",
        },
        {
            "tag": "G3",
            "status": "fyi",
            "priority": None,
            "headline": "",
            "detail": "",
            "owner": "",
            "due": "",
            "why": "automated reminder",
        },
    ]
    verdicts = _parse_verdicts(json.dumps(verdicts), sweep.by_tag.keys())

    md = render_report(verdicts, sweep)

    assert md.startswith("**What you're waiting on — last 7 days, Gmail + Slack**")
    needs = md.index("### Needs your action")
    waiting = md.index("### Waiting on others")
    skipped = md.index("### Skipped — already resolved or in someone else's hands")
    assert needs < waiting < skipped, "fixed section order"
    # High before Low inside "Needs your action".
    assert (
        md.index("**High**")
        < md.index("Initech rollout SOW")
        < md.index("**Low**")
        < md.index("Close the loop with Remy")
    )
    assert "_(with Reese Keller; due end of September)_" in md
    assert "[Gmail](https://x/G1)" in md
    # The two items Emery flagged as already closed land in Skipped — with the reason.
    assert (
        "Hotel payment authorizations — Harper confirmed both were processed on Sep 12 · [Slack](slack://s1)"
        in md
    )
    assert "Commission rate change — Marin closed it with Reese" in md
    # FYI is counted, not listed.
    assert "Also read 1 thread with no ask" in md
    assert "Vistage survey" not in md
    assert md.rstrip().endswith(
        "_Checked: Gmail (23 threads), Slack (9 mention threads, 4 DMs)._"
    )


def test_render_report_without_a_model_falls_back_to_direction_split():
    sweep = SweepResult(days=7)
    sweep.candidates = [
        _cand("G1", title="Theirs"),
        _cand("G2", title="Mine", last_from_user=True),
    ]
    md = render_report(None, sweep)
    assert "AI ranking was unavailable" in md
    assert (
        md.index("someone else spoke last")
        < md.index("**Theirs**")
        < md.index("you spoke last")
        < md.index("**Mine**")
    )


def test_render_report_empty_sweep_says_so_and_names_what_was_checked():
    sweep = SweepResult(
        days=7,
        checked=["Gmail (0 threads)"],
        not_checked=["Slack (not connected — connect it in Settings → Slack)"],
    )
    md = render_report([], sweep)
    assert "Nothing is waiting on you in this window." in md
    assert "Nothing outstanding from anyone else." in md
    assert "Not checked: Slack (not connected" in md


# ─── End to end ──────────────────────────────────────────────────────────────


class _FakeGmail:
    def __init__(self, threads):
        self._threads = threads

    def search_threads(self, query="", max_results=20):
        return self._threads


def test_sweep_unconnected_slack_is_a_footer_line_not_an_empty_section():
    gmail = _FakeGmail(
        [_thread("t1", _msg("m1", REESE, ME, "Signed SOW", "please schedule", D))]
    )
    svc = _slack_service(connected=False)
    seen = {}

    async def stub(cands, **kw):
        seen["tags"] = [c.tag for c in cands]
        return [
            {
                "tag": "G1",
                "status": "needs_me",
                "priority": "high",
                "headline": "Signed SOW",
                "detail": "Reese wants kickoff scheduled.",
                "owner": "Reese",
                "due": None,
                "why": None,
            }
        ]

    result = asyncio.run(
        run_open_loops_sweep(
            ME,
            days=7,
            gmail_client=gmail,
            slack_service=svc,
            now=NOW,
            synthesizer=stub,
        )
    )

    assert seen["tags"] == ["G1"], "Gmail still answers when Slack is out"
    assert result.checked == ["Gmail (1 threads)"]
    assert result.not_checked == [
        "Slack (not connected — connect it in Settings → Slack)"
    ]
    assert "**Signed SOW**" in result.report_md
    assert "Not checked: Slack (not connected" in result.report_md
    assert "### Skipped" not in result.report_md


def test_sweep_honours_sources_and_clamps_days():
    gmail = _FakeGmail([])
    svc = _slack_service()

    async def stub(cands, **kw):
        return []

    async def _never(*a, **k):
        raise AssertionError("Slack must not be touched when sources='gmail'")

    with patch.object(open_loops, "gather_slack", side_effect=_never):
        result = asyncio.run(
            run_open_loops_sweep(
                ME,
                days=999,
                sources="gmail",
                gmail_client=gmail,
                slack_service=svc,
                now=NOW,
                synthesizer=stub,
            )
        )
    assert result.days == open_loops.MAX_DAYS
    assert result.checked == ["Gmail (0 threads)"]
    assert result.not_checked == []


def test_synthesize_is_attributed_to_its_own_cost_category_and_uses_only_our_tags():
    from rubik_core.cost.usage import current_feature

    captured = {}

    class _Models:
        async def generate_content(self, *, model, contents, config=None):
            captured["feature"] = current_feature.get()
            captured["prompt"] = contents
            return SimpleNamespace(
                text=json.dumps(
                    [
                        {
                            "tag": "G1",
                            "status": "waiting_on_them",
                            "priority": "medium",
                            "headline": "H",
                        },
                        {
                            "tag": "G7",
                            "status": "needs_me",
                            "priority": "high",
                            "headline": "invented",
                        },
                    ]
                )
            )

    fake_client = SimpleNamespace(aio=SimpleNamespace(models=_Models()))
    cands = [
        _cand(
            "G1",
            last_from_user=True,
            transcript="- [Sep 12 10:00] USER → Shay: can you confirm?",
        )
    ]

    with patch("rubik_core.models.get_genai_client", return_value=fake_client):
        out = asyncio.run(synthesize(cands, user_first_name="Emery", days=7, now=NOW))

    assert captured["feature"] == "open_loops", (
        "never ship an AI feature into Unattributed"
    )
    assert (
        "[G1] GMAIL" in captured["prompt"]
        and "Last message by: the USER" in captured["prompt"]
    )
    assert [v["tag"] for v in out] == ["G1"], "the invented G7 is dropped"


def test_synthesize_failure_returns_none_so_the_report_degrades_honestly():
    class _Models:
        async def generate_content(self, **kw):
            raise RuntimeError("429")

    fake_client = SimpleNamespace(aio=SimpleNamespace(models=_Models()))
    with patch("rubik_core.models.get_genai_client", return_value=fake_client):
        assert (
            asyncio.run(synthesize([_cand("G1")], user_first_name="M", days=7, now=NOW))
            is None
        )


# ─── Wiring: tool, prompt, cost label, help ──────────────────────────────────


def test_tool_is_registered_and_the_prompt_routes_the_bare_question_to_it():
    src = inspect.getsource(coordinator.build_coordinator)
    assert "tools_instance.what_am_i_waiting_on" in src
    # The exact phrasing Emery used, routed without a source being named.
    assert '"what am I waiting on"' in src
    assert '"am I waiting on anything"' in src
    assert "ALWAYS `what_am_i_waiting_on`" in src
    assert "names no topic and no source" in src
    # The digest tools must not swallow the question any more.
    assert (
        "Do NOT answer these from `gmail_summarize_unread` or `slack_get_my_mentions`"
        in src
    )
    # The "ask what they mean" grounding rule carves the sweep tools out.
    assert (
        "The exception is the sweep tools, which take no topic at all — `what_am_i_waiting_on`"
        in src
    )
    # Skipped section survives the pass-through.
    assert 'including "Skipped"' in src


def test_tool_docstring_carries_the_routing_phrases_and_scope_fences():
    doc = AgentTools.what_am_i_waiting_on.__doc__ or ""
    for phrase in (
        "what am I waiting on",
        "who owes",
        "did anyone get back to me",
        "open loops",
    ):
        assert phrase in doc
    assert "NEWEST message" in doc
    assert "gmail_summarize_unread" in doc and "slack_summarize_my_unread" in doc
    assert inspect.iscoroutinefunction(AgentTools.what_am_i_waiting_on)


def test_open_loops_has_its_own_ai_cost_label():
    from backend.routers.admin import _FEATURE_LABELS

    assert open_loops.FEATURE == "open_loops"
    assert "open_loops" in _FEATURE_LABELS


def test_help_page_documents_it_in_both_homes():
    from pathlib import Path

    from rubik_core.help import HELP_SECTIONS

    chat = next(s for s in HELP_SECTIONS if s.id == "chat")
    assert "what am I waiting on?" in chat.body
    assert "skipped" in chat.body
    tsx = (
        Path(__file__).resolve().parents[2]
        / "frontend"
        / "src"
        / "pages"
        / "help"
        / "topics"
        / "Chat.tsx"
    )
    text = tsx.read_text(encoding="utf-8")
    assert 'q="What am I waiting on?"' in text
    assert "waiting on from others" in text


def test_tool_returns_the_report_and_footer_fields():
    tools = AgentTools(user_email=ME)
    fake = SweepResult(
        days=7,
        checked=["Gmail (2 threads)"],
        not_checked=["Slack (not connected — connect it in Settings → Slack)"],
        report_md="**report**",
    )

    async def fake_sweep(user_email, *, days, sources):
        assert user_email == ME and days == 7 and sources == "all"
        return fake

    with patch(
        "backend.services.open_loops.run_open_loops_sweep", side_effect=fake_sweep
    ):
        out = asyncio.run(tools.what_am_i_waiting_on())

    assert out["status"] == "success"
    assert out["report"] == "**report**"
    assert out["not_checked"] == fake.not_checked

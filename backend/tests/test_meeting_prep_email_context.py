"""Meeting prep must have email context for INTERNAL meetings too.

Regression: an internal 1:1 briefing left out the key topics to discuss and
the emails the reader still owed the other person feedback on. The brief itself
said "no historical context or related documents were found", and it was
right: ``fetch_emails()`` was gated on ``external_domains``,
so an all-internal meeting fetched no email at all. Even for external meetings
the search was ``from:@domain`` only, which structurally cannot surface a thread
the reader themselves sent and is still waiting to answer.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from backend.routers.home import (
    _awaiting_reply_from_user,
    _fetch_emails_with_people,
    _format_email_threads_for_prompt,
    _merge_threads,
    collect_meeting_prep_context,
)

USER = "ebrooks@acme.example"
PEER = "ifoster@acme.example"


def _thread(tid: str, *, last_from: str, subject: str = "Q3 budget") -> dict:
    return {
        "id": tid,
        "messages": [
            {
                "id": f"{tid}-m1",
                "snippet": "Can you take a look and send feedback?",
                "payload": {
                    "headers": [
                        {"name": "From", "value": last_from},
                        {"name": "To", "value": USER},
                        {"name": "Subject", "value": subject},
                        {"name": "Date", "value": "Mon, 20 Jul 2026 09:00:00 -0500"},
                    ]
                },
            }
        ],
    }


# ── the person-scoped search ─────────────────────────────────────────────────


def test_fetch_emails_with_people_searches_both_directions():
    """`from:` alone can't surface a thread the reader sent — so query all three
    roles (from / to / cc) for each counterparty."""
    client = MagicMock()
    client.search_threads.return_value = [_thread("t1", last_from=PEER)]
    with patch("rubik_core.tools.gmail.GmailClient", return_value=client):
        out = _fetch_emails_with_people(USER, [PEER], 6)

    assert len(out) == 1
    query = client.search_threads.call_args.kwargs["query"]
    assert f"from:{PEER}" in query
    assert f"to:{PEER}" in query
    assert f"cc:{PEER}" in query
    assert "-in:spam" in query and "-in:trash" in query


def test_fetch_emails_with_people_is_a_noop_without_people():
    """No counterparties (solo event) → no Gmail call at all."""
    client = MagicMock()
    with patch("rubik_core.tools.gmail.GmailClient", return_value=client):
        assert _fetch_emails_with_people(USER, [], 6) == []
    client.search_threads.assert_not_called()


def test_fetch_emails_with_people_survives_a_gmail_failure():
    """Prep must degrade to a thinner brief, never 500."""
    with patch("rubik_core.tools.gmail.GmailClient", side_effect=RuntimeError("boom")):
        assert _fetch_emails_with_people(USER, [PEER], 6) == []


def test_fetch_emails_with_people_bounds_the_query():
    """A big internal meeting must not build an unbounded Gmail query."""
    client = MagicMock()
    client.search_threads.return_value = []
    many = [f"p{i}@acme.example" for i in range(12)]
    with patch("rubik_core.tools.gmail.GmailClient", return_value=client):
        _fetch_emails_with_people(USER, many, 6)
    query = client.search_threads.call_args.kwargs["query"]
    assert query.count("from:") == 5


# ── merge + awaiting-reply tagging ───────────────────────────────────────────


def test_merge_threads_dedupes_by_id_and_keeps_order():
    a = _thread("t1", last_from=PEER)
    b = _thread("t2", last_from=USER)
    merged = _merge_threads([a, b], [dict(a), _thread("t3", last_from=PEER)])
    assert [t["id"] for t in merged] == ["t1", "t2", "t3"]


def test_awaiting_reply_is_true_when_they_sent_last():
    assert _awaiting_reply_from_user(_thread("t1", last_from=PEER), USER) is True


def test_awaiting_reply_is_false_when_the_user_sent_last():
    assert _awaiting_reply_from_user(_thread("t1", last_from=USER), USER) is False


def test_prompt_block_flags_threads_the_reader_has_not_answered():
    """The "you owe them a reply" fact has to reach the prompt as text — that's
    the whole basis for the open_items_you_owe field."""
    block = _format_email_threads_for_prompt(
        [_thread("t1", last_from=PEER, subject="Vendor mgmt feedback")],
        user_email=USER,
    )
    assert "you have not replied" in block
    assert "Vendor mgmt feedback" in block


def test_prompt_block_omits_the_status_line_for_answered_threads():
    block = _format_email_threads_for_prompt(
        [_thread("t1", last_from=USER)], user_email=USER
    )
    assert "you have not replied" not in block


def test_prompt_block_without_user_email_is_unchanged():
    """Back-compat: callers that don't pass user_email get the old rendering."""
    block = _format_email_threads_for_prompt([_thread("t1", last_from=PEER)])
    assert "you have not replied" not in block
    assert "Subject: Q3 budget" in block


# ── the internal-meeting gate itself ─────────────────────────────────────────


def _internal_event() -> dict:
    return {
        "id": "evt-1on1",
        "summary": "Indigo/Emery 1:1",
        "start": {"dateTime": "2026-07-22T15:00:00Z"},
        "end": {"dateTime": "2026-07-22T15:30:00Z"},
        "attendees": [{"email": USER}, {"email": PEER}],
        "description": "",
        "attachments": [],
    }


def _collect(event: dict):
    """Run collect_meeting_prep_context with every external fetch stubbed out."""
    tools = MagicMock()

    async def _no_meetings(**kwargs):
        return {"status": "error"}

    tools.search_past_meetings = _no_meetings

    with (
        patch("rubik_core.adk_agent.tools.AgentTools", return_value=tools),
        patch("backend.routers.home._resolve_attendee_names", lambda a: None),
        patch("backend.routers.home._fetch_emails_with_people") as with_people,
        patch("backend.routers.home._fetch_emails_for_domains") as by_domain,
        patch(
            "rubik_core.retrieval.product_catalog.get_product_catalog",
            return_value=MagicMock(search_products=MagicMock(return_value=[])),
        ),
        patch("rubik_core.tools.drive.GoogleDriveClient", side_effect=RuntimeError),
    ):
        with_people.return_value = [_thread("t1", last_from=PEER)]
        by_domain.return_value = []
        ctx = asyncio.run(
            collect_meeting_prep_context(
                user_email=USER,
                user_department="Executive",
                user_roles=[],
                event=event,
            )
        )
    return ctx, with_people, by_domain


def test_internal_meeting_still_fetches_email_for_its_attendees():
    """The regression itself: internal 1:1 → the person search MUST run."""
    ctx, with_people, by_domain = _collect(_internal_event())

    with_people.assert_called_once()
    # Called with the counterparty, and NOT with the reader themselves.
    people_arg = with_people.call_args[0][1]
    assert people_arg == [PEER]
    # No external domain → the company-wide search is correctly skipped.
    by_domain.assert_not_called()
    assert ctx["is_internal"] is True
    assert len(ctx["recent_emails"]) == 1


def test_external_meeting_runs_both_searches_and_merges():
    event = _internal_event()
    event["attendees"] = [{"email": USER}, {"email": "buyer@acmecorp.example"}]
    ctx, with_people, by_domain = _collect(event)

    with_people.assert_called_once()
    by_domain.assert_called_once()
    assert with_people.call_args[0][1] == ["buyer@acmecorp.example"]
    assert ctx["is_internal"] is False

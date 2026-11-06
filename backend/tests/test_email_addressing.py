"""Email recipient-addressing classification + focused inbox view.

Covers the change that makes the Email surface:
  - mirror the real Gmail inbox (`in:inbox`, so filtered-away mail stays away),
  - hide distribution-list / bulk mail (the user is not individually addressed,
    or the message carries List-* / Precedence: bulk markers),
  - rank mail sent directly To: the user above mail they were only Cc'd on.

The Gmail layer is mocked entirely (no live API).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from backend.services.gmail_service import (
    GmailService,
    classify_addressing,
    focus_sort_key,
)

USER = "bellison@acme.example"


def _h(**kw: str) -> dict[str, str]:
    """Build a lower-cased header dict (keys: to, cc, list_unsubscribe → list-unsubscribe)."""
    return {k.replace("_", "-").lower(): v for k, v in kw.items()}


# ─── classify_addressing ───────────────────────────────────────────────────────


def test_classify_direct_when_user_in_to():
    assert (
        classify_addressing(_h(to=f"Blair <{USER}>, other@x.example"), USER) == "direct"
    )


def test_classify_cc_when_user_only_in_cc():
    assert classify_addressing(_h(to="other@x.example", cc=USER), USER) == "cc"


def test_classify_list_when_user_not_individually_addressed():
    # Distribution-list mail: the LIST address is in To:, not the user.
    assert classify_addressing(_h(to="compliance@acme.example"), USER) == "list"


def test_bulk_markers_demote_even_a_direct_recipient():
    # A marketing blast sent straight to the user is still "informational".
    assert (
        classify_addressing(_h(to=USER, list_unsubscribe="<mailto:u@x>"), USER)
        == "list"
    )
    assert classify_addressing(_h(to=USER, precedence="bulk"), USER) == "list"
    assert classify_addressing(_h(cc=USER, list_id="<news.x.com>"), USER) == "list"


def test_classify_is_case_insensitive_on_address():
    assert classify_addressing(_h(to="BELLISON@ACME.example"), USER) == "direct"


def test_classify_list_when_no_user_email():
    assert classify_addressing(_h(to=USER), "") == "list"


def test_focus_sort_orders_direct_then_cc_unread_first():
    rows = [
        ("list", True),
        ("cc", False),
        ("cc", True),
        ("direct", False),
        ("direct", True),
    ]
    rows.sort(key=lambda r: focus_sort_key(*r))
    assert rows[:3] == [("direct", True), ("direct", False), ("cc", True)]


# ─── GmailService.get_unread (focus view) ──────────────────────────────────────


def _thread(
    tid: str,
    *,
    to: str = "",
    cc: str = "",
    unread: bool = True,
    list_unsub: bool = False,
) -> dict[str, Any]:
    headers = [
        {"name": "From", "value": "sender@example.com"},
        {"name": "Subject", "value": f"subj-{tid}"},
        {"name": "Date", "value": "Mon, 30 Jun 2026 10:00:00 +0000"},
    ]
    if to:
        headers.append({"name": "To", "value": to})
    if cc:
        headers.append({"name": "Cc", "value": cc})
    if list_unsub:
        headers.append({"name": "List-Unsubscribe", "value": "<mailto:u@x>"})
    return {
        "id": tid,
        "messages": [
            {
                "id": f"m-{tid}",
                "snippet": f"snip-{tid}",
                "labelIds": ["UNREAD"] if unread else [],
                "payload": {"headers": headers},
            }
        ],
    }


@patch("backend.services.gmail_service.GmailClient")
def test_get_unread_focus_filters_list_and_ranks_direct_above_cc(mock_cls):
    inst = MagicMock()
    inst.service = True
    inst.search_threads.return_value = [
        _thread("dist", to="compliance@acme.example"),  # list → dropped
        _thread("ccmail", to="someone@x.example", cc=USER, unread=False),  # cc, read
        _thread("direct1", to=USER, unread=True),  # direct, unread
        _thread("news", to=USER, list_unsub=True),  # bulk → dropped
    ]
    mock_cls.return_value = inst

    msgs = GmailService(user_email=USER).get_unread(max_results=10)

    ids = [m.thread_id for m in msgs]
    assert "dist" not in ids and "news" not in ids  # distribution-list + bulk hidden
    assert ids == ["direct1", "ccmail"]  # To: ranked above Cc:
    assert (msgs[0].addressing, msgs[1].addressing) == ("direct", "cc")

    # The query mirrors the real inbox so filtered-away mail never surfaces.
    query = inst.search_threads.call_args.kwargs["query"]
    assert "in:inbox" in query and "is:unread" in query


@patch("backend.services.gmail_service.GmailClient")
def test_get_unread_focus_false_keeps_everything(mock_cls):
    inst = MagicMock()
    inst.service = True
    inst.search_threads.return_value = [_thread("dist", to="compliance@acme.example")]
    mock_cls.return_value = inst

    msgs = GmailService(user_email=USER).get_unread(max_results=10, focus=False)
    assert [m.thread_id for m in msgs] == ["dist"]
    assert msgs[0].addressing == "list"


# ─── Legacy homefeed path (backend.routers.home) ───────────────────────────────


def test_home_parse_email_summary_classifies_addressing():
    from backend.routers.home import _parse_email_summary

    direct = _parse_email_summary(_thread("t1", to=USER), USER)
    assert direct is not None and direct.addressing == "direct"

    dist = _parse_email_summary(_thread("t2", to="dl@acme.example"), USER)
    assert dist is not None and dist.addressing == "list"

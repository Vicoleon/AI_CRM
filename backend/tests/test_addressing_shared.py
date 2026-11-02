"""``rubik_core.tools.addressing`` is shared by two surfaces — keep it that way.

The Email view and the AI-Priorities Gmail extractor must agree on whether a
message was aimed at the reader. When the logic lived only in
``backend/services/gmail_service.py`` the priorities pipeline could not use it
(rubik_core must not import backend), so it merged To and Cc into one list and
turned somebody else's "ACTION NEEDED" into the reader's own deadline.
"""

from __future__ import annotations

from rubik_core.tools.addressing import (
    classify_addressing,
    focus_sort_key,
    strongest_addressing,
)

ME = "ebrooks@acme.example"


def test_backend_reexport_is_the_same_object():
    """The old import path must resolve to the shared implementation, not a fork."""
    from backend.services import gmail_service

    assert gmail_service.classify_addressing is classify_addressing
    assert gmail_service.focus_sort_key is focus_sort_key


def test_direct_when_on_to():
    assert classify_addressing({"to": f"Emery <{ME}>"}, ME) == "direct"


def test_cc_when_only_copied():
    assert classify_addressing({"to": "ana@acme.example", "cc": ME}, ME) == "cc"


def test_list_when_never_individually_addressed():
    assert classify_addressing({"to": "compliance@acme.example"}, ME) == "list"


def test_bulk_markers_demote_even_a_direct_recipient():
    assert (
        classify_addressing({"to": ME, "list-unsubscribe": "<https://x>"}, ME) == "list"
    )
    assert classify_addressing({"to": ME, "precedence": "bulk"}, ME) == "list"


def test_strongest_addressing_prefers_direct_then_cc():
    assert strongest_addressing(["list", "cc", "direct"]) == "direct"
    assert strongest_addressing(["list", "cc"]) == "cc"
    assert strongest_addressing(["list", "list"]) == "list"


def test_strongest_addressing_of_nothing_is_list():
    """An empty thread is nobody's action item."""
    assert strongest_addressing([]) == "list"


def test_focus_sort_key_ranks_direct_unread_first():
    assert focus_sort_key("direct", True) < focus_sort_key("direct", False)
    assert focus_sort_key("direct", False) < focus_sort_key("cc", True)
    assert focus_sort_key("cc", False) < focus_sort_key("list", True)

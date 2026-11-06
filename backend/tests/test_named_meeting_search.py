"""Tests for the named-meeting resolution path in ``search_past_meetings``.

Regression: a user (a meeting participant) asking Cubie to "summarize the
Sales / HubSpot Weekly Open Office" got "I was unable to find a meeting with
the exact title …" even though the transcript was indexed and visible in their
Assets list. A semantic search over the *title* never recovered it — the title
words aren't in the transcript body, and the meeting competed against the whole
company corpus. The fix resolves a directly-named meeting by transcript
filename — ACL-scoped to the meetings the user attended — and reads its text
directly, mirroring how recency meeting questions resolve by date instead of
vector search.
"""

import asyncio
from datetime import datetime
from unittest.mock import MagicMock, patch

from rubik_core.adk_agent.tools import (
    AgentTools,
    _resolve_named_meeting,
    _title_tokens,
)

_HUBSPOT_FILENAME = (
    "Sales / HubSpot Weekly Open Office - 2026/06/26 10:57 EDT - Notes by Gemini"
)


def _meeting_asset(
    doc_id="m1",
    filename=_HUBSPOT_FILENAME,
    created_at=datetime(2026, 6, 26, 16, 30),
    status="approved",
):
    asset = MagicMock()
    asset.doc_id = doc_id
    asset.status = status
    asset.source_path = f"gs://bucket/workspace/{doc_id}.txt"
    asset.original_filename = filename
    asset.created_at = created_at
    return asset


def _registry_with(assets_by_id):
    """A fake DAMRegistry whose get_asset returns the mapped asset (or None)."""
    reg = MagicMock()
    reg.get_asset.side_effect = lambda doc_id: assets_by_id.get(doc_id)
    return reg


# ── token extraction ─────────────────────────────────────────────────────────


def test_title_tokens_keep_distinctive_meeting_words():
    # 'summary'/'of'/'the' are filler; 'open' is an instruction verb stopword —
    # but the surviving tokens still uniquely identify the meeting.
    toks = _title_tokens("a summary of the Sales / HubSpot Weekly Open Office")
    assert set(toks) == {"sales", "hubspot", "weekly", "office"}


# ── resolution ───────────────────────────────────────────────────────────────


def test_resolve_named_meeting_matches_by_title():
    """The named meeting resolves even though the stored filename carries a date
    / 'Notes by Gemini' suffix the user never types."""
    other = _meeting_asset(
        doc_id="m2",
        filename="Weekly 1:1 | P2 - 2026/06/23 12:57 CST - Notes by Gemini",
        created_at=datetime(2026, 6, 23, 12, 57),
    )
    reg = _registry_with({"m1": _meeting_asset(), "m2": other})

    result = _resolve_named_meeting(
        "Sales / HubSpot Weekly Open Office", {"m1", "m2"}, reg
    )

    assert [a.doc_id for a in result] == ["m1"]


def test_resolve_named_meeting_is_acl_scoped():
    """A meeting the user did NOT attend is never even fetched — only the
    accessible doc_ids are scanned, so it can't leak via a title match."""
    unrelated = _meeting_asset(
        doc_id="m2",
        filename="Weekly 1:1 | P2 - 2026/06/23 12:57 CST - Notes by Gemini",
        created_at=datetime(2026, 6, 23, 12, 57),
    )
    reg = _registry_with({"m1": _meeting_asset(), "m2": unrelated})

    # m1 (the HubSpot meeting) is NOT in the accessible set, so naming it must
    # resolve to nothing — and m1 must never even be fetched.
    result = _resolve_named_meeting("Sales / HubSpot Weekly Open Office", {"m2"}, reg)

    assert result == []
    # We must never reach outside the user's accessible meetings.
    for call in reg.get_asset.call_args_list:
        assert call.args[0] != "m1"


def test_resolve_named_meeting_skips_single_keyword_query():
    """A lone keyword stays a topical/recency question — no registry lookup."""
    reg = _registry_with({"m1": _meeting_asset()})
    result = _resolve_named_meeting("the meeting", {"m1"}, reg)
    assert result == []
    reg.get_asset.assert_not_called()


def test_resolve_named_meeting_ignores_topical_query():
    """A topic question that doesn't cover a meeting's *title* must fall through
    to semantic search, not resolve to an arbitrary transcript."""
    reg = _registry_with({"m1": _meeting_asset()})
    result = _resolve_named_meeting("pricing strategy discussion", {"m1"}, reg)
    assert result == []


def test_resolve_named_meeting_skips_deleted_transcript():
    reg = _registry_with({"m1": _meeting_asset(status="deleted")})
    result = _resolve_named_meeting("Sales / HubSpot Weekly Open Office", {"m1"}, reg)
    assert result == []


def test_resolve_named_meeting_returns_recurring_newest_first():
    """A recurring meeting (same title, several dates) returns every instance,
    newest first, so the agent can summarize or disambiguate."""
    older = _meeting_asset(
        doc_id="w1",
        filename="Weekly 1:1 | P2 - 2026/06/16 13:00 CST - Notes by Gemini",
        created_at=datetime(2026, 6, 16, 13, 0),
    )
    newer = _meeting_asset(
        doc_id="w2",
        filename="Weekly 1:1 | P2 - 2026/06/23 12:57 CST - Notes by Gemini",
        created_at=datetime(2026, 6, 23, 12, 57),
    )
    reg = _registry_with({"w1": older, "w2": newer})

    result = _resolve_named_meeting("Weekly P2", {"w1", "w2"}, reg)

    assert [a.doc_id for a in result] == ["w2", "w1"]


# ── tool integration ─────────────────────────────────────────────────────────


def test_search_past_meetings_returns_named_transcript_directly():
    """When a named meeting resolves, the tool returns its transcript text and
    never falls through to the semantic vector store."""
    tools = AgentTools(user_email="mingram@acme.example")
    with (
        patch(
            "rubik_core.database.repository.SQLMeetingParticipantsRepository"
        ) as MockRepo,
        patch("rubik_core.dam.registry.DAMRegistry"),
        patch(
            "rubik_core.adk_agent.tools._resolve_named_meeting",
            return_value=[_meeting_asset()],
        ),
        patch(
            "rubik_core.adk_agent.tools._read_transcript_text",
            return_value="Discussed Q3 HubSpot pipeline and open-office Q&A.",
        ),
        patch("rubik_core.adapters.vertex_rag.VertexRagAdapter") as MockVertex,
        patch("rubik_core.adapters.chroma.ChromaAdapter") as MockChroma,
    ):
        MockRepo.return_value.list_doc_ids_for_user.return_value = ["m1"]
        result = asyncio.run(
            tools.search_past_meetings(
                "summarize the Sales / HubSpot Weekly Open Office"
            )
        )

    assert result["status"] == "success"
    assert "Sales / HubSpot Weekly Open Office" in result["report"]
    assert "open-office Q&A" in result["report"]
    # Direct resolution must short-circuit the semantic fan-out.
    MockVertex.assert_not_called()
    MockChroma.assert_not_called()

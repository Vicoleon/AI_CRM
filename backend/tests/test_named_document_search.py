"""Tests for the named-document resolution path in ``search_knowledge_base``.

Regression: a user asking Cubie to "summarize the Sales End User UAT file"
got "I couldn't find a specific file titled …" even though the document was
active in the Library. The file lived in a corpus outside the user's RAG
fan-out (a doc they owned, filed under another department), so a semantic
search over the request phrase never reached it. The fix resolves a
directly-named document by filename — access-checked — and reads it directly,
mirroring how recency meeting questions resolve by date instead of vector
search.
"""

import asyncio
from unittest.mock import MagicMock, patch

from rubik_core.adk_agent.tools import (
    AgentTools,
    _resolve_named_documents,
    _title_tokens,
)

_UAT_FILENAME = "Sales end-user UAT (Updated June 2026).docx"


def _fake_user(email="bellison@acme.example", *, is_admin=False, department="Sales"):
    u = MagicMock()
    u.email = email
    u.is_admin = is_admin
    u.department = department
    return u


def _uat_asset():
    asset = MagicMock()
    asset.doc_id = "uat1"
    asset.status = "approved"  # the DB value behind the UI's "active" badge
    asset.source_path = "gs://bucket/rags/x/uat.docx"
    asset.original_filename = _UAT_FILENAME
    return asset


# ── token extraction ────────────────────────────────────────────────────────


def test_title_tokens_strips_instruction_and_artifact_words():
    toks = _title_tokens(
        "summarize the Sales End User UAT file and give me key actions to perform"
    )
    # Only the distinctive title words survive — not summarize/file/key/actions.
    assert set(toks) == {"sales", "end", "user", "uat"}


def test_title_tokens_returns_nothing_for_pure_instruction():
    assert _title_tokens("") == []
    assert _title_tokens("summarize the document and give me key takeaways") == []


# ── resolution ──────────────────────────────────────────────────────────────


def test_resolve_named_documents_exact_match_without_artifact_word():
    """The agent often rephrases "the X file" down to just "X" — an exact
    all-token filename match must still resolve, no artifact word required."""
    candidates = {"uat1": {"original_filename": _UAT_FILENAME, "topic": "Other"}}
    with (
        patch("rubik_core.auth.registry.UserRegistry") as MockUR,
        patch("rubik_core.dam.registry.DAMRegistry") as MockDAM,
        patch(
            "rubik_core.auth.access.has_document_access", return_value=True
        ) as mock_access,
    ):
        MockUR.return_value.get_user.return_value = _fake_user()
        MockDAM.return_value.list_assets.return_value = {
            "assets": candidates,
            "total": 1,
        }
        MockDAM.return_value.get_asset.return_value = _uat_asset()

        result = _resolve_named_documents("Sales End User UAT", "bellison@acme.example")

    assert [a.doc_id for a in result] == ["uat1"]
    mock_access.assert_called_once()


def test_resolve_named_documents_denies_inaccessible_file():
    """A confident filename match the user can't access must NOT leak."""
    candidates = {"uat1": {"original_filename": _UAT_FILENAME, "topic": "Other"}}
    with (
        patch("rubik_core.auth.registry.UserRegistry") as MockUR,
        patch("rubik_core.dam.registry.DAMRegistry") as MockDAM,
        patch("rubik_core.auth.access.has_document_access", return_value=False),
    ):
        MockUR.return_value.get_user.return_value = _fake_user()
        MockDAM.return_value.list_assets.return_value = {
            "assets": candidates,
            "total": 1,
        }
        MockDAM.return_value.get_asset.return_value = _uat_asset()

        result = _resolve_named_documents(
            "summarize the Sales End User UAT file", "bellison@acme.example"
        )

    assert result == []


def test_resolve_named_documents_skips_single_keyword_query():
    """A lone keyword stays a semantic-search question — no DB lookup at all."""
    with patch("rubik_core.dam.registry.DAMRegistry") as MockDAM:
        result = _resolve_named_documents(
            "what is our remote work policy", "bellison@acme.example"
        )
    assert result == []
    MockDAM.assert_not_called()  # returned before touching the registry


def test_resolve_named_documents_ignores_partial_natural_language_match():
    """A long question that only partially overlaps a filename is NOT a named
    file — it must fall through to semantic search."""
    candidates = {"uat1": {"original_filename": _UAT_FILENAME, "topic": "Other"}}
    with (
        patch("rubik_core.auth.registry.UserRegistry") as MockUR,
        patch("rubik_core.dam.registry.DAMRegistry") as MockDAM,
        patch("rubik_core.auth.access.has_document_access", return_value=True),
    ):
        MockUR.return_value.get_user.return_value = _fake_user()
        MockDAM.return_value.list_assets.return_value = {
            "assets": candidates,
            "total": 1,
        }
        MockDAM.return_value.get_asset.return_value = _uat_asset()

        # 'sales' lands but 'onboarding'/'process' don't → coverage too low.
        result = _resolve_named_documents(
            "what is our sales onboarding process", "bellison@acme.example"
        )
    assert result == []


def test_resolve_named_documents_excludes_meeting_transcripts():
    """Transcripts belong to search_past_meetings, never this path."""
    candidates = {
        "t1": {
            "original_filename": "Sales End User UAT.docx",
            "topic": "Meeting Transcripts",
        }
    }
    with (
        patch("rubik_core.auth.registry.UserRegistry") as MockUR,
        patch("rubik_core.dam.registry.DAMRegistry") as MockDAM,
        patch("rubik_core.auth.access.has_document_access", return_value=True),
    ):
        MockUR.return_value.get_user.return_value = _fake_user()
        MockDAM.return_value.list_assets.return_value = {
            "assets": candidates,
            "total": 1,
        }
        result = _resolve_named_documents("Sales End User UAT", "bellison@acme.example")
    assert result == []


# ── tool integration ────────────────────────────────────────────────────────


def test_search_knowledge_base_returns_named_document_directly():
    """When a named document resolves, the tool returns its text and never
    falls through to the semantic vector store."""
    tools = AgentTools(user_email="bellison@acme.example")
    with (
        patch(
            "rubik_core.adk_agent.tools._resolve_named_documents",
            return_value=[_uat_asset()],
        ),
        patch(
            "rubik_core.adk_agent.tools._read_asset_document_text",
            return_value="UAT step 1: log in. Step 2: upload a document.",
        ),
        patch("rubik_core.adapters.chroma.ChromaAdapter") as MockChroma,
    ):
        result = asyncio.run(
            tools.search_knowledge_base("summarize the Sales End User UAT file")
        )

    assert result["status"] == "success"
    assert "Sales end-user UAT" in result["report"]
    assert "UAT step 1" in result["report"]
    # Direct resolution must short-circuit the semantic fan-out.
    MockChroma.return_value.query.assert_not_called()


def test_search_knowledge_base_falls_through_when_no_named_match():
    """No named match → the tool proceeds to the normal retrieval path."""
    tools = AgentTools(user_email="bellison@acme.example")
    with (
        patch("rubik_core.adk_agent.tools._resolve_named_documents", return_value=[]),
        patch("rubik_core.models.get_embeddings") as mock_emb,
        patch("rubik_core.adapters.chroma.ChromaAdapter") as MockChroma,
    ):
        mock_emb.return_value.embed_query.return_value = [0.1, 0.2, 0.3]
        MockChroma.return_value.query.return_value = []
        result = asyncio.run(tools.search_knowledge_base("what is the PTO policy"))

    # Reached the semantic path (which returned nothing here).
    MockChroma.return_value.query.assert_called_once()
    assert result["status"] == "success"

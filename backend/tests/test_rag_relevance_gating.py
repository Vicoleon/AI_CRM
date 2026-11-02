"""Tests for the RAG relevance gating added in the chat-hallucination RCA
(Part B): distance-threshold-aware weak-hit filtering, the low_confidence
status from search_knowledge_base, and phantom-citation hardening in the
orchestrator's report parser.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from rubik_core.config import settings
from rubik_core.orchestration import adk_orchestrator as ao

# ─────────────────────────────────────────────────────────────────────────────
# _filter_rag_hits_by_score
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _thresholds(monkeypatch):
    monkeypatch.setattr(settings, "RAG_DISTANCE_THRESHOLD", 0.5)
    monkeypatch.setattr(settings, "RAG_WEAK_HIT_DISTANCE", 0.45)
    yield


def _filter(scores):
    from rubik_core.adk_agent.tools import _filter_rag_hits_by_score

    return _filter_rag_hits_by_score(scores)


def test_filter_keeps_strong_distance_hits():
    keep, low_conf = _filter([0.1, 0.2, 0.44])
    assert keep == [True, True, True]
    assert low_conf is False


def test_filter_drops_weak_distance_hits_but_keeps_strong():
    # 0.48 exceeds the 0.45 weak cutoff (but is under the 0.5 retrieval
    # threshold, so it can legitimately come back) → dropped as tangential.
    keep, low_conf = _filter([0.2, 0.48])
    assert keep == [True, False]
    assert low_conf is False


def test_filter_flags_low_confidence_when_all_weak():
    keep, low_conf = _filter([0.47, 0.49])
    assert keep == [False, False]
    assert low_conf is True


def test_filter_fails_open_for_similarity_shaped_scores():
    # Values above the distance threshold can't be distances → likely a
    # similarity (higher=better). Never invert the gate: keep everything.
    keep, low_conf = _filter([0.86, 0.91])
    assert keep == [True, True]
    assert low_conf is False


def test_filter_fails_open_when_scores_unpopulated():
    keep, low_conf = _filter([0.0, 0.0])
    assert keep == [True, True]
    assert low_conf is False


def test_filter_empty():
    assert _filter([]) == ([], False)


# ─────────────────────────────────────────────────────────────────────────────
# search_knowledge_base — low_confidence status (UC-1 path)
# ─────────────────────────────────────────────────────────────────────────────


def _uc1_env(monkeypatch):
    from rubik_core.config import AppMode

    monkeypatch.setattr(settings, "APP_MODE", AppMode.GCP)


def _hit(text, rag, uri, score):
    return SimpleNamespace(text=text, rag=rag, source_uri=uri, score=score)


def _search_kb(user_email, query, hits, monkeypatch):
    _uc1_env(monkeypatch)
    from rubik_core.adk_agent.tools import AgentTools

    tools = AgentTools(user_email=user_email)
    with (
        patch("rubik_core.adk_agent.tools._resolve_named_documents", return_value=[]),
        patch("rubik_core.rag.rags_for_user_email", return_value=["companywide"]),
        patch("rubik_core.rag.RagManager") as MgrCls,
    ):
        MgrCls.return_value.query.return_value = hits
        return asyncio.run(tools.search_knowledge_base(query))


def test_search_knowledge_base_low_confidence_when_hits_all_weak(monkeypatch):
    weak_hits = [_hit("tangential", "companywide", "gs://b/x.md", 0.48)]
    result = _search_kb(
        "u@acme.example", "nonexistent migration plan", weak_hits, monkeypatch
    )
    assert result["status"] == "low_confidence"
    assert "don't have" in result["report"].lower()


def test_search_knowledge_base_success_when_strong_hit(monkeypatch):
    hits = [_hit("real content", "companywide", "gs://b/handbook.md", 0.2)]
    result = _search_kb("u@acme.example", "company handbook", hits, monkeypatch)
    assert result["status"] == "success"
    assert "**Source (companywide):** handbook.md" in result["report"]


def test_search_knowledge_base_low_confidence_when_no_hits(monkeypatch):
    result = _search_kb("u@acme.example", "nothing matches this", [], monkeypatch)
    assert result["status"] == "low_confidence"


# ─────────────────────────────────────────────────────────────────────────────
# _extract_citations_from_report — phantom-citation hardening + (rag) regex fix
# ─────────────────────────────────────────────────────────────────────────────


def _extract(report):
    orch = object.__new__(ao.ADKOrchestrator)  # bypass heavy __init__
    citations: list = []
    orch._extract_citations_from_report(report, citations)
    return citations


def test_extract_parses_uc1_rag_qualified_source():
    # Regression: "**Source (rag):**" was previously unmatched → "Unknown Source".
    report = "**Source (companywide):** handbook.md\nBenefits are described here."
    cites = _extract(report)
    assert len(cites) == 1
    assert cites[0]["source_path"] == "handbook.md"
    assert "Benefits" in cites[0]["snippet"]


def test_extract_parses_plain_source():
    report = "**Source:** policy.pdf\nThe policy text."
    cites = _extract(report)
    assert len(cites) == 1
    assert cites[0]["source_path"] == "policy.pdf"


def test_extract_skips_no_result_report_no_phantom_citation():
    for report in (
        "No relevant documents found.",
        "No sufficiently relevant documents were found for this query. Do not "
        "invent or infer document names, projects, or contents — tell the user "
        "you don't have a document covering this.",
    ):
        assert _extract(report) == []


def test_extract_multiple_blocks_and_skips_sourceless_block():
    report = (
        "**Source (sales):** a.md\nAlpha content.\n\n---\n\n"
        "stray text with no source marker\n\n---\n\n"
        "**Source:** b.pdf\nBeta content."
    )
    cites = _extract(report)
    assert [c["source_path"] for c in cites] == ["a.md", "b.pdf"]

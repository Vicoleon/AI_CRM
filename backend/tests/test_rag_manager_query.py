from __future__ import annotations

"""Unit tests for RagManager.query — multi-corpus fan-out and slug attribution."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from rubik_core.config import settings
from rubik_core.rag import (
    NoUC1CorporaError,
    RagManager,
    RagOperationError,
    build_registry,
)
from rubik_core.rag.manager import _attribute_rag_from_uri

# Test registry: companywide + Sales + HR.
TEST_REGISTRY = build_registry(["Sales", "HR"])


@pytest.fixture(autouse=True)
def _gcp_settings(monkeypatch):
    """RagManager refuses to construct without these — set them for the test."""
    monkeypatch.setattr(settings, "GCP_PROJECT_ID", "test-project")
    monkeypatch.setattr(settings, "GCS_BUCKET_NAME", "test-bucket")
    monkeypatch.setattr(settings, "VERTEX_RAG_LOCATION", "us-south1")
    yield


def _bound(mapping: dict[str, str]):
    """Stub the control lookup; a runtime query must never scan all corpora."""

    def lookup(kind: str, *, create: bool = False, audience: str | None = None):
        assert create is False
        key = audience if kind == "rag" else kind.removesuffix("_rag")
        return mapping.get(key)

    return patch("rubik_core.rag.manager.corpus_id", side_effect=lookup)


def _fake_response(contexts: list[SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(contexts=SimpleNamespace(contexts=contexts))


def _make_manager() -> RagManager:
    """Build a RagManager with an explicit registry + patched-out side effects."""
    with (
        patch("rubik_core.rag.manager._init_vertexai", return_value=None),
        patch("rubik_core.rag.manager.storage.Client") as gcs_cls,
    ):
        gcs_cls.return_value = MagicMock()
        return RagManager(registry=TEST_REGISTRY)


# ── attribution helper ──────────────────────────────────────────────────────


def test_attribute_rag_from_uri_matches_subprefix():
    assert (
        _attribute_rag_from_uri("gs://b/workspace/rags/sales/foo.md", TEST_REGISTRY)
        == "sales"
    )
    assert (
        _attribute_rag_from_uri(
            "gs://b/workspace/rags/companywide/handbook.md", TEST_REGISTRY
        )
        == "companywide"
    )


def test_attribute_rag_from_uri_unknown_prefix():
    assert _attribute_rag_from_uri("gs://b/other/foo.md", TEST_REGISTRY) == "unknown"
    assert _attribute_rag_from_uri("", TEST_REGISTRY) == "unknown"


# ── query: no corpora exist → NoUC1CorporaError ─────────────────────────────


def test_query_raises_when_no_corpora_exist():
    mgr = _make_manager()
    with (
        _bound({}),
        patch("rubik_core.rag.manager.vertex_rag.list_corpora") as lc,
        patch("rubik_core.rag.manager.vertex_rag.retrieval_query") as rq,
    ):
        with pytest.raises(NoUC1CorporaError):
            mgr.query(query_text="anything")
        rq.assert_not_called()
        lc.assert_not_called()


# ── query: unknown slug in `rags=` is a hard error ──────────────────────────


def test_query_unknown_slug_raises_immediately():
    mgr = _make_manager()
    with patch("rubik_core.rag.manager.vertex_rag.list_corpora") as lc:
        with pytest.raises(RagOperationError, match="Unknown RAG"):
            mgr.query(query_text="x", rags=["nonexistent"])
        lc.assert_not_called()


# ── query: happy path with subset of corpora present ────────────────────────


def test_query_fans_out_across_existing_corpora_and_attributes_hits():
    mgr = _make_manager()

    sales_resource = "projects/p/locations/l/ragCorpora/SALES"
    cw_resource = "projects/p/locations/l/ragCorpora/CW"
    fake_ctx = [
        SimpleNamespace(
            text="sales chunk",
            source_uri="gs://test-bucket/workspaces/test/rags/sales/cat.md",
            score=0.91,
        ),
        SimpleNamespace(
            text="company-wide chunk",
            source_uri="gs://test-bucket/workspaces/test/rags/companywide/handbook.md",
            score=0.86,
        ),
    ]

    with (
        _bound({"sales": sales_resource, "companywide": cw_resource}),
        patch("rubik_core.rag.manager.vertex_rag.list_corpora") as lc,
        patch(
            "rubik_core.rag.manager.vertex_rag.retrieval_query",
            return_value=_fake_response(fake_ctx),
        ) as rq,
        patch(
            "rubik_core.rag.manager.vertex_rag.RagResource",
            side_effect=lambda rag_corpus: SimpleNamespace(rag_corpus=rag_corpus),
        ),
    ):
        hits = mgr.query(
            query_text="benefits", rags=["companywide", "sales", "hr"], top_k=4
        )

    # Single retrieval_query call passing only the corpora that exist.
    assert rq.call_count == 1
    lc.assert_not_called()
    kwargs = rq.call_args.kwargs
    passed = sorted(r.rag_corpus for r in kwargs["rag_resources"])
    assert passed == sorted([cw_resource, sales_resource])
    assert kwargs["text"] == "benefits"
    assert kwargs["similarity_top_k"] == 4

    assert [h.rag for h in hits] == ["sales", "companywide"]
    assert hits[0].text == "sales chunk"
    assert hits[0].score == pytest.approx(0.91)


def test_query_returns_empty_list_when_corpora_have_no_matches():
    mgr = _make_manager()
    with (
        _bound({"sales": "projects/p/locations/l/ragCorpora/SALES"}),
        patch(
            "rubik_core.rag.manager.vertex_rag.retrieval_query",
            return_value=_fake_response([]),
        ),
        patch(
            "rubik_core.rag.manager.vertex_rag.RagResource",
            side_effect=lambda rag_corpus: SimpleNamespace(rag_corpus=rag_corpus),
        ),
    ):
        hits = mgr.query(query_text="something nobody knows about", rags=["sales"])
    # Empty list = real "no answer." The caller must NOT fall back here.
    assert hits == []


def test_cached_corpus_is_rechecked_after_remap_removal_and_suspension():
    mgr = _make_manager()
    old = "projects/p/locations/l/ragCorpora/OLD"
    new = "projects/p/locations/l/ragCorpora/NEW"
    binding = {"resource": old, "active": True}

    def lookup(kind, *, create=False, audience=None):
        assert (kind, create, audience) == ("rag", False, "sales")
        if not binding["active"]:
            raise PermissionError("workspace suspended")
        return binding["resource"]

    with (
        patch("rubik_core.rag.manager.corpus_id", side_effect=lookup),
        patch(
            "rubik_core.rag.manager.vertex_rag.RagResource",
            side_effect=lambda rag_corpus: SimpleNamespace(rag_corpus=rag_corpus),
        ),
        patch(
            "rubik_core.rag.manager.vertex_rag.retrieval_query",
            return_value=_fake_response([]),
        ) as query,
    ):
        assert mgr._resolve_corpus("sales", create_if_missing=False) == old
        binding["resource"] = new
        assert mgr.query("renewal", rags=["sales"]) == []
        assert query.call_args.kwargs["rag_resources"][0].rag_corpus == new

        binding["resource"] = None
        query.reset_mock()
        with pytest.raises(NoUC1CorporaError):
            mgr.query("renewal", rags=["sales"])
        query.assert_not_called()

        binding["active"] = False
        with pytest.raises(RagOperationError, match="suspended"):
            mgr.query("renewal", rags=["sales"])
        query.assert_not_called()


def test_query_wraps_retrieval_failures_in_rag_operation_error():
    mgr = _make_manager()
    with (
        _bound({"sales": "projects/p/locations/l/ragCorpora/SALES"}),
        patch(
            "rubik_core.rag.manager.vertex_rag.retrieval_query",
            side_effect=RuntimeError("vertex blew up"),
        ),
        patch(
            "rubik_core.rag.manager.vertex_rag.RagResource",
            side_effect=lambda rag_corpus: SimpleNamespace(rag_corpus=rag_corpus),
        ),
    ):
        with pytest.raises(RagOperationError):
            mgr.query(query_text="x", rags=["sales"])

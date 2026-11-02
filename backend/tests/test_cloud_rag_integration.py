"""Cloud BYOK RAG must use the selected, bound vector space on every path."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from rubik_core.adapters.base import Chunk, RetrievedChunk
from rubik_core.adapters.vertex_rag import VertexRagAdapter
from rubik_core.ai_providers import cloud_rag_bridge as bridge
from rubik_core.ai_providers.cloud_sources import CloudSource
from rubik_core.ai_providers.cloud_vector_index import (
    CloudVectorIndexError,
    CloudVectorSpec,
)
from rubik_core.chat_files import private_index
from rubik_core.config import settings
from rubik_core.rag.manager import RagManager
from rubik_core.tenancy.context import use_workspace

RESOURCE = "projects/p/locations/l/ragCorpora/bound"
URI = "gs://bucket/workspaces/test/rags/sales/handbook.md"


def _spec(version: str) -> CloudVectorSpec:
    return CloudVectorSpec(
        "test",
        "c_0123456789abcdef01234567",
        "gemini",
        "gemini-embedding-2",
        768,
        version,
    )


def _hit() -> RetrievedChunk:
    return RetrievedChunk(
        chunk=Chunk(
            chunk_id="chunk-1",
            doc_id="doc-1",
            chunk_index=0,
            text="A relevant result",
            metadata={"source_path": URI},
        ),
        score=0.12,
    )


def test_selected_adapter_query_never_calls_managed_rag(monkeypatch):
    adapter = VertexRagAdapter.__new__(VertexRagAdapter)
    adapter.corpus_id = RESOURCE
    from rubik_core.adapters import vertex_rag

    monkeypatch.setattr(vertex_rag, "require_bound_corpus_id", lambda value: value)
    monkeypatch.setattr(
        vertex_rag.rag, "retrieval_query", lambda **_: pytest.fail("managed query")
    )
    monkeypatch.setattr(vertex_rag, "query_cloud_resource", lambda *_, **__: [_hit()])
    with use_workspace("test"):
        assert adapter.query_rag_corpus("handbook", target_corpus_id=RESOURCE) == [
            _hit()
        ]

    def unavailable(*_, **__):
        raise CloudVectorIndexError("Vector source updates are still pending")

    monkeypatch.setattr(vertex_rag, "query_cloud_resource", unavailable)
    with use_workspace("test"):
        with pytest.raises(CloudVectorIndexError, match="pending"):
            adapter.query_rag_corpus("handbook", target_corpus_id=RESOURCE)


def test_audience_manager_preserves_source_and_never_uses_managed_fallback(monkeypatch):
    monkeypatch.setattr(settings, "GCS_BUCKET_NAME", "bucket")
    manager = RagManager.__new__(RagManager)
    manager.workspace_slug = "test"
    manager.registry = {"sales": SimpleNamespace(slug="sales")}
    monkeypatch.setattr(manager, "_resolve_corpus", lambda *_, **__: RESOURCE)
    from rubik_core.rag import manager as manager_module

    monkeypatch.setattr(
        manager_module.vertex_rag,
        "retrieval_query",
        lambda **_: pytest.fail("managed query"),
    )
    monkeypatch.setattr(
        manager_module, "query_cloud_resource", lambda *_, **__: [_hit()]
    )
    with use_workspace("test"):
        hits = manager.query("handbook", rags=["sales"])
    assert [(hit.rag, hit.source_uri, hit.score) for hit in hits] == [
        ("sales", URI, 0.12)
    ]


def test_source_change_reaches_active_and_candidate_before_outbox_clears(monkeypatch):
    active, candidate = _spec("0123456789abcdef"), _spec("fedcba9876543210")
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(bridge, "cloud_index_versions", lambda _: (active, candidate))
    monkeypatch.setattr(bridge, "mark_active", lambda _: None)
    monkeypatch.setattr(bridge, "cloud_corpus_key", lambda _: active.corpus)
    monkeypatch.setattr(bridge, "pending_updates", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(bridge, "_source_generation", lambda *_: 42)
    monkeypatch.setattr(bridge, "enqueue_source_update", lambda *args: "event-1")
    monkeypatch.setattr(
        bridge,
        "load_gcs_source",
        lambda *_, **__: CloudSource(
            URI,
            42,
            "a" * 64,
            [
                Chunk(
                    chunk_id="chunk-1",
                    doc_id="doc-1",
                    chunk_index=0,
                    text="Knowledge",
                    metadata={"source_path": URI},
                )
            ],
        ),
    )
    monkeypatch.setattr(
        bridge,
        "provider_connection_route",
        lambda provider, model, **_: SimpleNamespace(provider=provider, model=model),
    )
    monkeypatch.setattr(bridge, "embed_with_route", lambda *_, **__: [[0.1] * 768])
    monkeypatch.setattr(
        bridge,
        "replace_source",
        lambda spec, _uri, _generation, _digest, chunks: (
            events.append(("indexed", spec.version)) or len(chunks)
        ),
    )
    monkeypatch.setattr(
        bridge, "mark_update_applied", lambda _: events.append(("applied", "event-1"))
    )
    monkeypatch.setattr(
        bridge, "mark_update_failed", lambda _: events.append(("failed", "event-1"))
    )
    with use_workspace("test"):
        update = bridge.begin_cloud_source_update(RESOURCE, URI, "upsert")
        bridge.finish_cloud_source_update(RESOURCE, URI, "upsert", update)
    assert events == [
        ("indexed", active.version),
        ("indexed", candidate.version),
        ("applied", "event-1"),
    ]


def test_failed_candidate_write_keeps_outbox_pending(monkeypatch):
    active, candidate = _spec("0123456789abcdef"), _spec("fedcba9876543210")
    events: list[str] = []
    monkeypatch.setattr(bridge, "cloud_index_versions", lambda _: (active, candidate))
    monkeypatch.setattr(bridge, "mark_active", lambda _: None)
    monkeypatch.setattr(
        bridge,
        "tombstone_source",
        lambda spec, *_: (
            events.append(spec.version)
            if spec == active
            else (_ for _ in ()).throw(CloudVectorIndexError("candidate failed"))
        ),
    )
    monkeypatch.setattr(
        bridge, "mark_update_applied", lambda _: pytest.fail("outbox cleared")
    )
    monkeypatch.setattr(bridge, "mark_update_failed", lambda _: events.append("failed"))
    with use_workspace("test"):
        with pytest.raises(CloudVectorIndexError, match="candidate failed"):
            bridge.finish_cloud_source_update(
                RESOURCE,
                URI,
                "delete",
                bridge.CloudSourceUpdate("event-1", 42),
            )
    assert events == [active.version, "failed"]


def test_private_chat_search_filters_authorized_uris_before_ranking(monkeypatch):
    rag_file = f"{RESOURCE}/ragFiles/owned"
    private_uri = "gs://bucket/workspaces/test/chat_files/private/chatfile__owned.md"
    monkeypatch.setattr(settings, "GCS_BUCKET_NAME", "bucket")
    monkeypatch.setattr(private_index, "resolve_corpus", lambda **_: RESOURCE)
    monkeypatch.setattr(
        private_index, "_verified_ragfiles", lambda _: {rag_file: private_uri}
    )
    from rubik_core.ai_providers import cloud_rag_bridge, embedding_index
    from rubik_core.tenancy import corpora

    monkeypatch.setattr(
        embedding_index, "active_cloud_index", lambda _: _spec("0123456789abcdef")
    )
    monkeypatch.setattr(corpora, "assert_own_rag_file", lambda name, _: name)
    seen = []

    def cloud_query(_resource, _query, **kwargs):
        seen.append(kwargs["allowed_source_uris"])
        item = _hit()
        item.chunk.metadata["source_path"] = private_uri
        return [item]

    monkeypatch.setattr(cloud_rag_bridge, "query_cloud_resource", cloud_query)
    monkeypatch.setattr(
        private_index, "_rag", lambda: pytest.fail("managed private search")
    )
    with use_workspace("test"):
        hits = private_index.search([rag_file], "handbook", top_k=5)
    assert hits == [private_index.Hit("owned", "A relevant result", 0.12)]
    assert seen == [[private_uri]]

    monkeypatch.setattr(
        cloud_rag_bridge,
        "query_cloud_resource",
        lambda *_args, **_kwargs: [
            RetrievedChunk(
                chunk=Chunk(
                    chunk_id="foreign",
                    doc_id="foreign",
                    chunk_index=0,
                    text="private text",
                    metadata={"source_path": private_uri.replace("owned", "other")},
                ),
                score=0.1,
            )
        ],
    )
    with use_workspace("test"):
        with pytest.raises(private_index.PrivateIndexError, match="fence"):
            private_index.search([rag_file], "handbook", top_k=5)


def test_private_backfill_is_idempotent_and_rejects_unverified_gcs(monkeypatch):
    rag_file = f"{RESOURCE}/ragFiles/owned"
    monkeypatch.setattr(settings, "GCS_BUCKET_NAME", "bucket")
    monkeypatch.setattr(private_index, "resolve_corpus", lambda **_: RESOURCE)
    uri = "gs://bucket/workspaces/test/chat_files/private/chatfile__owned.md"
    monkeypatch.setattr(private_index, "_verified_ragfiles", lambda _: {rag_file: uri})
    monkeypatch.setattr(private_index, "cloud_source_uri", lambda _: uri)
    from rubik_core.chat_files import service
    from rubik_core.database import db as database_db

    monkeypatch.setattr(service, "library_document", lambda _: "Stored document")
    row = SimpleNamespace(
        id="owned", owner_email="owner@example.test", rag_file_name=rag_file
    )

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def query(self, *_):
            return self

        def filter(self, *_):
            return self

        def all(self):
            return [row]

    monkeypatch.setattr(database_db, "SessionLocal", FakeSession)
    state = {"fail": False}

    class FakeBlob:
        generation = 42

        def reload(self):
            if state["fail"]:
                raise RuntimeError("transient GCS failure")

        def download_as_bytes(self, **_):
            return b"Stored document"

    monkeypatch.setattr(private_index, "_cloud_blob", lambda _: FakeBlob())
    monkeypatch.setattr(
        private_index,
        "_store_cloud_document",
        lambda *_: pytest.fail("unchanged source was rewritten"),
    )
    with use_workspace("test"):
        assert private_index.ensure_cloud_sources() == {uri}
        assert private_index.ensure_cloud_sources() == {uri}
        state["fail"] = True
        with pytest.raises(private_index.PrivateIndexError, match="verified"):
            private_index.ensure_cloud_sources()


def test_reconcile_refuses_unpublished_gcs_object(monkeypatch):
    spec = _spec("0123456789abcdef")
    event = SimpleNamespace(
        id="event-1", source_uri=URI, generation=42, operation="upsert"
    )
    monkeypatch.setattr(bridge, "cloud_index_versions", lambda _: (spec, None))
    monkeypatch.setattr(bridge, "cloud_corpus_key", lambda _: spec.corpus)
    monkeypatch.setattr(bridge, "pending_updates", lambda *_args, **_kwargs: [event])
    monkeypatch.setattr(bridge, "_bound_source_membership", lambda _: set())
    monkeypatch.setattr(
        bridge,
        "load_gcs_source",
        lambda *_args, **_kwargs: pytest.fail("orphan indexed"),
    )
    with use_workspace("test"):
        with pytest.raises(CloudVectorIndexError, match="not published"):
            bridge.reconcile_pending(RESOURCE)


def test_cloud_query_rejects_removed_ragfile_before_serving_vector(monkeypatch):
    spec = _spec("0123456789abcdef")
    monkeypatch.setattr(bridge, "active_cloud_index", lambda _: spec)
    monkeypatch.setattr(bridge, "mark_active", lambda _: None)
    monkeypatch.setattr(
        bridge,
        "provider_connection_route",
        lambda provider, model, **_: SimpleNamespace(provider=provider, model=model),
    )
    monkeypatch.setattr(
        bridge, "embed_with_route", lambda *_args, **_kwargs: [[0.1] * 768]
    )
    monkeypatch.setattr(bridge, "query_chunks", lambda *_args, **_kwargs: [_hit()])
    monkeypatch.setattr(bridge, "_bound_source_membership", lambda _: set())
    monkeypatch.setattr(settings, "GCS_BUCKET_NAME", "bucket")
    with use_workspace("test"):
        with pytest.raises(CloudVectorIndexError, match="no longer published"):
            bridge.query_cloud_resource(RESOURCE, "handbook")

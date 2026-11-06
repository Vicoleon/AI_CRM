"""Cloud BYOK RAG must use the selected, bound vector space on every path."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
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


@pytest.fixture(autouse=True)
def _workspace_bucket(monkeypatch):
    monkeypatch.setattr(settings, "GCS_BUCKET_NAME", "bucket")


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
        lambda spec, _uri, _generation, _digest, chunks, **_: (
            events.append(("indexed", spec.version)) or len(chunks)
        ),
    )
    monkeypatch.setattr(
        bridge, "mark_update_applied", lambda _: events.append(("applied", "event-1"))
    )
    monkeypatch.setattr(
        bridge, "mark_update_failed", lambda _: events.append(("failed", "event-1"))
    )
    monkeypatch.setattr(bridge, "_clear_older_source_updates", lambda *_: None)
    with use_workspace("test"):
        update = bridge.begin_cloud_source_update(RESOURCE, URI, "upsert")
        bridge.finish_cloud_source_update(RESOURCE, URI, "upsert", update)
    assert events == [
        ("indexed", active.version),
        ("indexed", candidate.version),
        ("applied", "event-1"),
    ]


def test_duplicate_ragfile_delete_waits_until_last_source_reference(monkeypatch):
    spec = _spec("0123456789abcdef")
    references = [URI, URI]
    monkeypatch.setattr(bridge, "cloud_index_versions", lambda _: (spec, None))
    monkeypatch.setattr(bridge, "cloud_corpus_key", lambda _: spec.corpus)
    monkeypatch.setattr(bridge, "pending_updates", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(bridge, "mark_active", lambda _: None)
    monkeypatch.setattr(bridge, "_deletion_source_references", lambda _: references)
    monkeypatch.setattr(bridge, "_source_generation", lambda *_args: 42)
    events = []
    monkeypatch.setattr(
        bridge,
        "enqueue_source_update",
        lambda *_args: events.append("queued") or "event-1",
    )
    with use_workspace("test"):
        assert bridge.begin_cloud_source_update(RESOURCE, URI, "delete") is None
        references.pop()
        assert bridge.begin_cloud_source_update(RESOURCE, URI, "delete") == (
            bridge.CloudSourceUpdate("event-1", 42)
        )
    assert events == ["queued"]


def test_deleting_one_duplicate_keeps_gcs_source(monkeypatch):
    from rubik_core.adapters import vertex_rag
    from rubik_core.rag import manager as manager_module

    manager = RagManager.__new__(RagManager)
    manager.gcs_client = object()
    monkeypatch.setattr(
        manager_module, "begin_cloud_source_update", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(manager_module, "source_is_published", lambda *_args: True)
    monkeypatch.setattr(
        manager, "_delete_from_gcs", lambda *_args: pytest.fail("shared GCS removed")
    )
    monkeypatch.setattr(
        manager_module.vertex_rag, "delete_file", lambda **_kwargs: None
    )
    monkeypatch.setattr(vertex_rag.rag, "delete_file", lambda **_kwargs: None)
    monkeypatch.setattr(
        vertex_rag, "begin_cloud_source_update", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(vertex_rag, "source_is_published", lambda *_args: True)
    with use_workspace("test"):
        assert manager._delete_verified_document(
            SimpleNamespace(slug="sales"), RESOURCE, "rag-file", URI, True
        ) == {"rag_deleted": True, "gcs_deleted": False}
        adapter = VertexRagAdapter.__new__(VertexRagAdapter)
        adapter.gcs_client = object()
        assert adapter._delete_verified_document(RESOURCE, "rag-file", URI, True)


def test_shared_gcs_source_survives_delete_in_one_bound_corpus(monkeypatch):
    from rubik_core.control import db as control_db
    from rubik_core.tenancy import corpora

    second = "projects/p/locations/l/ragCorpora/other"
    references = {RESOURCE: [], second: [URI]}

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def get(self, *_):
            return SimpleNamespace(
                status="active", corpus_ids={"default": RESOURCE, "sales": second}
            )

        def scalars(self, *_):
            return SimpleNamespace(all=lambda: [])

    monkeypatch.setattr(control_db, "control_session", Session)
    monkeypatch.setattr(corpora, "require_bound_corpus_id", lambda value: value)
    monkeypatch.setattr(
        bridge, "_deletion_source_references", lambda resource: references[resource]
    )
    manager = RagManager.__new__(RagManager)
    manager.gcs_client = object()
    from rubik_core.rag import manager as manager_module

    monkeypatch.setattr(
        manager_module, "begin_cloud_source_update", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        manager_module.vertex_rag, "delete_file", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        manager, "_delete_from_gcs", lambda _: pytest.fail("shared bytes deleted")
    )
    with use_workspace("test"):
        assert manager._delete_verified_document(
            SimpleNamespace(slug="sales"), RESOURCE, "rag-file", URI, True
        ) == {"rag_deleted": True, "gcs_deleted": False}
        assert bridge.source_referencing_corpora(RESOURCE, URI) == {second}


def test_generic_import_refuses_source_published_in_another_corpus(monkeypatch):
    from contextlib import nullcontext

    from rubik_core.adapters import vertex_rag

    second = "projects/p/locations/l/ragCorpora/other"
    monkeypatch.setattr(vertex_rag, "require_bound_corpus_id", lambda value: value)
    monkeypatch.setattr(
        vertex_rag, "cloud_source_mutation_lock", lambda *_: nullcontext()
    )
    monkeypatch.setattr(
        vertex_rag,
        "source_referencing_corpora",
        lambda *_: {second},
    )
    monkeypatch.setattr(
        vertex_rag.rag,
        "import_files",
        lambda **_: pytest.fail("cross-corpus alias imported"),
    )
    adapter = VertexRagAdapter.__new__(VertexRagAdapter)
    adapter.corpus_id = RESOURCE
    adapter.gcs_client = object()
    with use_workspace("test"):
        with pytest.raises(CloudVectorIndexError, match="another bound corpus"):
            adapter.import_gcs_uri_to_corpus(URI)


@pytest.mark.parametrize(
    "counts",
    [(0, 1, 0), (0, 0, 1), (0, 0, 0), (True, 0, 0), None],
)
@pytest.mark.parametrize("entry", ["adapter", "manager"])
def test_reimport_rejected_result_never_proves_or_syncs_new_generation(
    monkeypatch, counts, entry
):
    from rubik_core.adapters import vertex_rag
    from rubik_core.rag import manager as manager_module

    response = (
        SimpleNamespace(
            imported_rag_files_count=counts[0],
            failed_rag_files_count=counts[1],
            skipped_rag_files_count=counts[2],
        )
        if counts is not None
        else object()
    )
    monkeypatch.setattr(vertex_rag.rag, "import_files", lambda **_: response)
    monkeypatch.setattr(bridge, "_bound_source_membership", lambda _: {URI})
    failed = []
    monkeypatch.setattr(bridge, "mark_update_failed", failed.append)
    update = bridge.CloudSourceUpdate("reimport", 43)
    module = vertex_rag if entry == "adapter" else manager_module
    monkeypatch.setattr(
        module, "begin_cloud_source_update", lambda *_args, **_kwargs: update
    )
    monkeypatch.setattr(
        module,
        "confirm_managed_source_update",
        lambda *_: pytest.fail("failed import was proven"),
    )
    monkeypatch.setattr(
        module,
        "finish_cloud_source_update",
        lambda *_args, **_kwargs: pytest.fail("new generation was indexed"),
    )
    with use_workspace("test"):
        if entry == "adapter":
            adapter = VertexRagAdapter.__new__(VertexRagAdapter)
            adapter.gcs_client = object()
            monkeypatch.setattr(adapter, "_require_exclusive_source", lambda *_: None)
            with pytest.raises(RuntimeError, match="RAG import failed"):
                adapter._import_gcs_uri_locked(URI, RESOURCE)
        else:
            manager = RagManager.__new__(RagManager)
            manager.gcs_client = object()
            monkeypatch.setattr(manager, "_require_exclusive_source", lambda *_: None)
            with pytest.raises(
                manager_module.RagOperationError, match="RAG import failed"
            ):
                manager._import_to_corpus_locked(RESOURCE, URI)
    assert failed == ["reimport"]


def test_admin_sync_preserves_each_asset_audience(monkeypatch):
    from backend.routers import admin
    from rubik_core.adapters import vertex_rag
    from rubik_core.config import AppMode
    from rubik_core.rag import manager as manager_module

    sales = "projects/p/locations/l/ragCorpora/sales"
    companywide = "projects/p/locations/l/ragCorpora/companywide"
    imports = []

    class VertexRagAdapter:
        corpus_id = RESOURCE

        def import_gcs_uri_to_corpus(self, uri, target):
            imports.append((uri, target))

    class RagManager:
        def corpus_for_asset(self, department, is_company_wide):
            return companywide if is_company_wide else sales

    class Db:
        def query(self, *_):
            return self

        def filter(self, *_):
            return self

        def all(self):
            return [
                SimpleNamespace(
                    doc_id="sales-doc",
                    source_path=URI,
                    department="Sales",
                    is_company_wide=False,
                ),
                SimpleNamespace(
                    doc_id="company-doc",
                    source_path="gs://bucket/workspaces/test/rags/companywide/policy.md",
                    department="Sales",
                    is_company_wide=True,
                ),
            ]

        def commit(self):
            pass

    monkeypatch.setattr(settings, "APP_MODE", AppMode.GCP)
    monkeypatch.setattr(vertex_rag, "VertexRagAdapter", VertexRagAdapter)
    monkeypatch.setattr(manager_module, "RagManager", RagManager)
    monkeypatch.setattr(vertex_rag.rag, "list_files", lambda **_: [])
    with use_workspace("test"):
        result = asyncio.run(
            admin.sync_rag(user=SimpleNamespace(is_admin=True), db=Db())
        )
    assert result["imported_to_rag"] == 2
    assert imports == [
        (URI, sales),
        ("gs://bucket/workspaces/test/rags/companywide/policy.md", companywide),
    ]


def test_asset_corpus_resolution_refuses_unscoped_or_reserved_department(monkeypatch):
    from rubik_core.rag.manager import RagOperationError

    manager = RagManager.__new__(RagManager)
    resolved = []
    monkeypatch.setattr(
        manager,
        "_resolve_corpus",
        lambda slug, *, create_if_missing: (
            resolved.append((slug, create_if_missing)) or RESOURCE
        ),
    )
    with use_workspace("test"):
        assert manager.corpus_for_asset("Sales", False) == RESOURCE
        assert manager.corpus_for_asset("Sales", True) == RESOURCE
        for department in (None, "companywide", "vto"):
            with pytest.raises(RagOperationError, match="valid knowledge audience"):
                manager.corpus_for_asset(department, False)
    assert resolved == [("sales", False), ("companywide", False)]


def test_reindex_script_preserves_asset_audience(monkeypatch):
    import sys

    from rubik_core.adapters import vertex_rag
    from rubik_core.dam import registry as dam_registry
    from rubik_core.rag import manager as manager_module
    from rubik_core.tenancy import cli
    from scripts import reindex_to_rag

    second = "projects/p/locations/l/ragCorpora/sales"
    imports = []

    class DAMRegistry:
        def list_assets(self, **kwargs):
            assert kwargs == {"include_total": False}
            return {
                "assets": {
                    "sales-doc": {
                        "status": "approved",
                        "source_path": URI,
                        "department": "Sales",
                        "is_company_wide": False,
                    }
                }
            }

    class VertexRagAdapter:
        def import_gcs_uri_to_corpus(self, uri, target):
            imports.append((uri, target))

    class RagManager:
        def corpus_for_asset(self, department, is_company_wide):
            assert (department, is_company_wide) == ("Sales", False)
            return second

    monkeypatch.setattr(cli, "require_cli_workspace", lambda: None)
    monkeypatch.setattr(dam_registry, "DAMRegistry", DAMRegistry)
    monkeypatch.setattr(vertex_rag, "VertexRagAdapter", VertexRagAdapter)
    monkeypatch.setattr(manager_module, "RagManager", RagManager)
    monkeypatch.setattr(sys, "argv", ["reindex_to_rag.py", "--status", "approved"])
    with use_workspace("test"):
        reindex_to_rag.main()
    assert imports == [(URI, second)]


def test_upload_and_import_hold_one_source_lock(monkeypatch, tmp_path):
    from rubik_core.adapters import vertex_rag
    from rubik_core.rag import manager as manager_module

    path = tmp_path / "handbook.md"
    path.write_text("content")
    state = {"held": False}
    events = []

    @contextmanager
    def source_lock(_resource, _uri):
        assert not state["held"]
        state["held"] = True
        try:
            yield
        finally:
            state["held"] = False

    def upload(*_args):
        assert state["held"]
        events.append("upload")
        return URI

    def imported(*_args):
        assert state["held"]
        events.append("import")

    monkeypatch.setattr(manager_module, "cloud_source_mutation_lock", source_lock)
    manager = RagManager.__new__(RagManager)
    manager.bucket_name = "bucket"
    monkeypatch.setattr(manager, "_spec", lambda *_: SimpleNamespace(slug="sales"))
    monkeypatch.setattr(manager, "_resolve_corpus", lambda *_args, **_kwargs: RESOURCE)
    monkeypatch.setattr(
        manager,
        "_gcs_blob_name",
        lambda *_args: "workspaces/test/rags/sales/handbook.md",
    )
    monkeypatch.setattr(manager, "_upload_to_gcs", upload)
    monkeypatch.setattr(manager, "_import_to_corpus_locked", imported)
    monkeypatch.setattr(manager, "_require_exclusive_source", lambda *_: None)
    with use_workspace("test"):
        manager.add_document("sales", path)
    assert events == ["upload", "import"]

    events.clear()
    monkeypatch.setattr(vertex_rag, "cloud_source_mutation_lock", source_lock)
    monkeypatch.setattr(vertex_rag, "require_bound_corpus_id", lambda value: value)
    monkeypatch.setattr(vertex_rag, "cloud_corpus_key", lambda _: "c_sales")
    monkeypatch.setattr(
        vertex_rag,
        "object_name",
        lambda *parts: "workspaces/test/" + "/".join(parts),
    )
    adapter = VertexRagAdapter.__new__(VertexRagAdapter)
    adapter.corpus_id = RESOURCE
    adapter.bucket_name = "bucket"
    monkeypatch.setattr(
        adapter,
        "_upload_to_gcs_locked",
        lambda _path, _blob, uri: upload(uri),
    )
    monkeypatch.setattr(adapter, "_import_gcs_uri_locked", imported)
    monkeypatch.setattr(adapter, "_require_exclusive_source", lambda *_: None)
    with use_workspace("test"):
        assert adapter.upload_and_import(path) == (
            "gs://bucket/workspaces/test/rag/c_sales/handbook.md"
        )
    assert events == ["upload", "import"]


def test_gcp_source_lock_uses_dedicated_advisory_connection(monkeypatch):
    from rubik_core.config import AppMode

    spec = _spec("0123456789abcdef")
    monkeypatch.setattr(settings, "APP_MODE", AppMode.GCP)
    monkeypatch.setattr(bridge, "cloud_index_versions", lambda _: (spec, None))
    monkeypatch.setattr(bridge, "cloud_corpus_key", lambda _: spec.corpus)
    events = []

    class Transaction:
        def rollback(self):
            events.append("rollback")

    class Connection:
        dialect = SimpleNamespace(name="postgresql")

        def __enter__(self):
            events.append("dedicated-connection")
            return self

        def __exit__(self, *_):
            pass

        def begin(self):
            return Transaction()

        def scalar(self, *_args, **_kwargs):
            return True

    class Engine:
        def connect(self):
            return Connection()

        def dispose(self):
            events.append("dispose")

    monkeypatch.setattr(bridge, "advisory_engine_for", lambda _: Engine())
    with use_workspace("test"):
        with bridge.cloud_source_mutation_lock(RESOURCE, URI):
            events.append("managed-and-vector-work")
    assert events == [
        "dedicated-connection",
        "managed-and-vector-work",
        "rollback",
        "dispose",
    ]


def test_same_gcs_uri_uses_one_workspace_lock_across_corpora(monkeypatch):
    from contextlib import contextmanager

    from rubik_core.config import AppMode

    monkeypatch.setattr(settings, "APP_MODE", AppMode.GCP)
    monkeypatch.setattr(bridge, "cloud_corpus_key", lambda resource: resource)
    locks = []

    @contextmanager
    def record_lock(key):
        locks.append(key)
        yield

    monkeypatch.setattr(bridge, "_automatic_repair_lock", record_lock)
    with use_workspace("test"):
        with bridge.cloud_source_mutation_lock(RESOURCE, URI):
            pass
        with bridge.cloud_source_mutation_lock(
            "projects/p/locations/l/ragCorpora/other", URI
        ):
            pass
    assert locks == [f"source:{URI}", f"source:{URI}"]


def test_same_filename_in_two_corpora_uses_distinct_gcs_sources(monkeypatch, tmp_path):
    from rubik_core.adapters import vertex_rag

    path = tmp_path / "handbook.md"
    path.write_text("content")
    first = RESOURCE
    second = "projects/p/locations/l/ragCorpora/other"
    monkeypatch.setattr(vertex_rag, "require_bound_corpus_id", lambda value: value)
    monkeypatch.setattr(
        vertex_rag,
        "cloud_corpus_key",
        lambda resource: "c_first" if resource == first else "c_second",
    )
    monkeypatch.setattr(
        vertex_rag,
        "object_name",
        lambda *parts: "workspaces/test/" + "/".join(parts),
    )
    locks = []

    @contextmanager
    def source_lock(resource, uri):
        locks.append((resource, uri))
        yield

    monkeypatch.setattr(vertex_rag, "cloud_source_mutation_lock", source_lock)
    adapter = VertexRagAdapter.__new__(VertexRagAdapter)
    adapter.corpus_id = first
    adapter.bucket_name = "bucket"
    uploaded = []
    monkeypatch.setattr(
        adapter,
        "_upload_to_gcs_locked",
        lambda _path, _blob, uri: uploaded.append(uri),
    )
    monkeypatch.setattr(adapter, "_import_gcs_uri_locked", lambda *_: None)
    monkeypatch.setattr(adapter, "_require_exclusive_source", lambda *_: None)
    with use_workspace("test"):
        first_uri = adapter.upload_and_import(path, target_corpus_id=first)
        second_uri = adapter.upload_and_import(path, target_corpus_id=second)
    assert first_uri != second_uri
    assert first_uri.endswith("/c_first/handbook.md")
    assert second_uri.endswith("/c_second/handbook.md")
    assert uploaded == [first_uri, second_uri]
    assert locks == [(first, first_uri), (second, second_uri)]


def test_candidate_rejects_legacy_uri_shared_by_two_bound_corpora(monkeypatch):
    from rubik_core.ai_providers import cloud_embedding_workflow as workflow

    first = RESOURCE
    second = "projects/p/locations/l/ragCorpora/other"
    other_uri = "gs://bucket/workspaces/test/rag/other.md"
    sources = {first: URI, second: URI}

    class FakeResult:
        def all(self):
            return []

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def get(self, *_):
            return SimpleNamespace(
                status="active", corpus_ids={"default": first, "other": second}
            )

        def scalars(self, *_):
            return FakeResult()

    monkeypatch.setattr(workflow, "control_session", FakeSession)
    monkeypatch.setattr(workflow, "require_bound_corpus_id", lambda value: value)
    monkeypatch.setattr(workflow, "corpus_id", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(workflow, "assert_own_rag_file", lambda name, _: name)
    monkeypatch.setattr(workflow, "assert_own_uri", lambda uri: uri)
    monkeypatch.setattr(workflow.vertexai, "init", lambda **_: None)
    monkeypatch.setattr(
        workflow.rag,
        "list_files",
        lambda *, corpus_name: [
            SimpleNamespace(
                name=f"{corpus_name}/ragFiles/one",
                gcs_source=SimpleNamespace(uris=[sources[corpus_name]]),
            )
        ],
    )
    with use_workspace("test"):
        with pytest.raises(CloudVectorIndexError, match="shared across bound corpora"):
            workflow._source_uris(first)
        sources[second] = other_uri
        assert workflow._source_uris(first) == {URI}


def test_failed_candidate_write_keeps_outbox_pending(monkeypatch):
    active, candidate = _spec("0123456789abcdef"), _spec("fedcba9876543210")
    events: list[str] = []
    monkeypatch.setattr(bridge, "cloud_index_versions", lambda _: (active, candidate))
    monkeypatch.setattr(bridge, "mark_active", lambda _: None)
    monkeypatch.setattr(
        bridge,
        "tombstone_source",
        lambda spec, *_, **__: (
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
        private_index, "_sources_for_filed_names", lambda _: {rag_file: private_uri}
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
    monkeypatch.setattr(
        private_index, "repair_orphan_ragfiles", lambda *_args, **_kwargs: 0
    )
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
        id="event-1",
        source_uri=URI,
        generation=42,
        operation="upsert",
        sequence=1,
        status="failed",
        managed_applied=True,
    )
    monkeypatch.setattr(bridge, "cloud_index_versions", lambda _: (spec, None))
    monkeypatch.setattr(bridge, "cloud_corpus_key", lambda _: spec.corpus)
    monkeypatch.setattr(bridge, "pending_updates", lambda *_args, **_kwargs: [event])
    monkeypatch.setattr(bridge, "source_updates", lambda *_args, **_kwargs: [event])
    monkeypatch.setattr(bridge, "_bound_source_membership", lambda _: set())
    monkeypatch.setattr(
        bridge,
        "load_gcs_source",
        lambda *_args, **_kwargs: pytest.fail("orphan indexed"),
    )
    with use_workspace("test"):
        with pytest.raises(CloudVectorIndexError, match="not published"):
            bridge.reconcile_pending(RESOURCE)


def test_reconcile_does_not_resurrect_old_upsert_after_applied_delete(monkeypatch):
    spec = _spec("0123456789abcdef")
    old = SimpleNamespace(
        id="old",
        source_uri=URI,
        generation=42,
        operation="upsert",
        sequence=1,
        status="failed",
        managed_applied=False,
    )
    deleted = SimpleNamespace(
        id="new",
        source_uri=URI,
        generation=42,
        operation="delete",
        sequence=2,
        status="applied",
        managed_applied=True,
    )
    seen = []
    state = {"pending": True}
    monkeypatch.setattr(bridge, "cloud_index_versions", lambda _: (spec, None))
    monkeypatch.setattr(bridge, "cloud_corpus_key", lambda _: spec.corpus)
    monkeypatch.setattr(
        bridge,
        "pending_updates",
        lambda *_args, **_kwargs: [old] if state["pending"] else [],
    )
    monkeypatch.setattr(
        bridge, "source_updates", lambda *_args, **_kwargs: [old, deleted]
    )
    monkeypatch.setattr(bridge, "_bound_source_membership", lambda _: set())
    monkeypatch.setattr(bridge, "_source_generation", lambda *_args: 42)
    monkeypatch.setattr(
        bridge, "tombstone_source", lambda *_args, **_kwargs: seen.append("delete")
    )
    monkeypatch.setattr(bridge, "mark_update_applied", lambda _: seen.append("applied"))

    def supersede(old_id, successor):
        seen.append(f"superseded:{old_id}:{successor}")
        state["pending"] = False

    monkeypatch.setattr(bridge, "mark_update_superseded", supersede)
    monkeypatch.setattr(
        bridge, "load_gcs_source", lambda *_args: pytest.fail("old upsert replayed")
    )
    with use_workspace("test"):
        assert bridge.reconcile_pending(RESOURCE) == 1
    assert seen == ["delete", "applied", "superseded:old:new"]


def test_cloud_query_rejects_removed_ragfile_before_serving_vector(monkeypatch):
    spec = _spec("0123456789abcdef")
    monkeypatch.setattr(bridge, "active_cloud_index", lambda _: spec)
    monkeypatch.setattr(
        bridge,
        "_repair_private_orphans",
        lambda _: pytest.fail("clean read scanned for private orphans"),
    )
    monkeypatch.setattr(bridge, "pending_updates", lambda *_args, **_kwargs: [])
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


def test_failed_unpublished_import_is_cancelled_by_proven_delete(monkeypatch):
    events = []
    monkeypatch.setattr(bridge, "_bound_source_membership", lambda _: set())
    monkeypatch.setattr(bridge, "cloud_corpus_key", lambda _: "c_default")
    monkeypatch.setattr(bridge, "enqueue_source_update", lambda *_: "successor")
    monkeypatch.setattr(
        bridge,
        "mark_update_managed_applied",
        lambda update_id: events.append(("proof", update_id)),
    )
    monkeypatch.setattr(
        bridge,
        "finish_cloud_source_update",
        lambda _resource, _uri, operation, update, **_: events.append(
            (operation, update.id)
        ),
    )
    monkeypatch.setattr(
        bridge, "mark_update_failed", lambda *_: pytest.fail("left pending")
    )
    with use_workspace("test"):
        assert (
            bridge.settle_failed_managed_update(
                RESOURCE, URI, "upsert", bridge.CloudSourceUpdate("original", 42)
            )
            is False
        )
    assert events == [("proof", "successor"), ("delete", "successor")]


def test_failed_import_with_existing_uri_remains_pending(monkeypatch):
    seen = []
    monkeypatch.setattr(bridge, "_bound_source_membership", lambda _: {URI})
    monkeypatch.setattr(
        bridge, "mark_update_failed", lambda update_id: seen.append(update_id)
    )
    monkeypatch.setattr(
        bridge, "mark_update_managed_applied", lambda *_: pytest.fail("false proof")
    )
    with use_workspace("test"):
        with pytest.raises(CloudVectorIndexError, match="ambiguous"):
            bridge.settle_failed_managed_update(
                RESOURCE, URI, "upsert", bridge.CloudSourceUpdate("original", 42)
            )
    assert seen == ["original"]


def test_failed_delete_of_published_source_restores_proven_vector_state(monkeypatch):
    events = []
    monkeypatch.setattr(bridge, "_bound_source_membership", lambda _: {URI})
    monkeypatch.setattr(bridge, "cloud_corpus_key", lambda _: "c_default")
    monkeypatch.setattr(bridge, "enqueue_source_update", lambda *_: "successor")
    monkeypatch.setattr(
        bridge,
        "mark_update_managed_applied",
        lambda update_id: events.append(("proof", update_id)),
    )
    monkeypatch.setattr(
        bridge,
        "finish_cloud_source_update",
        lambda _resource, _uri, operation, update, **_: events.append(
            (operation, update.id)
        ),
    )
    monkeypatch.setattr(
        bridge, "mark_update_failed", lambda *_: pytest.fail("left pending")
    )
    with use_workspace("test"):
        assert (
            bridge.settle_failed_managed_update(
                RESOURCE, URI, "delete", bridge.CloudSourceUpdate("original", 42)
            )
            is False
        )
    assert events == [("proof", "successor"), ("upsert", "successor")]


def test_private_failed_upload_without_ragfile_cancels_outbox(monkeypatch):
    from rubik_core.ai_providers import cloud_vector_index, embedding_index

    events = []
    monkeypatch.setattr(private_index, "resolve_corpus", lambda **_: RESOURCE)
    monkeypatch.setattr(private_index, "_store_cloud_document", lambda *_: URI)
    monkeypatch.setattr(private_index, "_ragfile_for_upload", lambda *_: None)
    monkeypatch.setattr(
        private_index,
        "_cloud_blob",
        lambda *_: SimpleNamespace(delete=lambda **_: events.append("gcs-delete")),
    )
    monkeypatch.setattr(
        embedding_index,
        "cloud_index_versions",
        lambda *_: (_spec("0123456789abcdef"), None),
    )
    monkeypatch.setattr(embedding_index, "cloud_corpus_key", lambda *_: "c_default")
    monkeypatch.setattr(
        bridge,
        "begin_cloud_source_update",
        lambda *_: bridge.CloudSourceUpdate("original", 42),
    )
    monkeypatch.setattr(
        cloud_vector_index, "enqueue_source_update", lambda *_: "successor"
    )
    monkeypatch.setattr(
        bridge,
        "confirm_managed_source_update",
        lambda update: events.append(("proof", update.id)),
    )
    monkeypatch.setattr(
        bridge,
        "finish_cloud_source_update",
        lambda _resource, _uri, operation, update: events.append(
            (operation, update.id)
        ),
    )
    monkeypatch.setattr(
        bridge, "fail_cloud_source_update", lambda *_: pytest.fail("left pending")
    )

    class FakeRag:
        TransformationConfig = staticmethod(lambda **_: object())
        ChunkingConfig = staticmethod(lambda **_: object())

        @staticmethod
        def upload_file(**_):
            raise RuntimeError("upload rejected")

    monkeypatch.setattr(private_index, "_rag", lambda: FakeRag())
    with use_workspace("test"):
        with pytest.raises(private_index.PrivateIndexError, match="upload failed"):
            private_index.add_document("owned", "Private document")
    assert events == [("proof", "successor"), ("delete", "successor"), "gcs-delete"]


def test_private_ambiguous_upload_compensates_before_chat_only(monkeypatch):
    from rubik_core.ai_providers import cloud_vector_index, embedding_index

    events = []
    rag_file = f"{RESOURCE}/ragFiles/owned"
    found = iter([rag_file, None])
    monkeypatch.setattr(private_index, "resolve_corpus", lambda **_: RESOURCE)
    monkeypatch.setattr(private_index, "_store_cloud_document", lambda *_: URI)
    monkeypatch.setattr(private_index, "_ragfile_for_upload", lambda *_: next(found))
    monkeypatch.setattr(
        private_index,
        "_cloud_blob",
        lambda *_: SimpleNamespace(delete=lambda **_: events.append("gcs-delete")),
    )
    monkeypatch.setattr(
        embedding_index,
        "cloud_index_versions",
        lambda *_: (_spec("0123456789abcdef"), None),
    )
    monkeypatch.setattr(embedding_index, "cloud_corpus_key", lambda *_: "c_default")
    monkeypatch.setattr(
        bridge,
        "begin_cloud_source_update",
        lambda *_: bridge.CloudSourceUpdate("original", 42),
    )
    monkeypatch.setattr(
        cloud_vector_index, "enqueue_source_update", lambda *_: "successor"
    )
    monkeypatch.setattr(
        bridge,
        "confirm_managed_source_update",
        lambda update: events.append(("proof", update.id)),
    )
    monkeypatch.setattr(
        bridge,
        "finish_cloud_source_update",
        lambda _resource, _uri, operation, update: events.append(
            (operation, update.id)
        ),
    )
    monkeypatch.setattr(
        bridge, "fail_cloud_source_update", lambda *_: pytest.fail("left pending")
    )

    class FakeRag:
        TransformationConfig = staticmethod(lambda **_: object())
        ChunkingConfig = staticmethod(lambda **_: object())

        @staticmethod
        def upload_file(**_):
            raise RuntimeError("upload outcome unknown")

        @staticmethod
        def delete_file(**_):
            events.append("rag-delete")

    monkeypatch.setattr(private_index, "_rag", lambda: FakeRag())
    with use_workspace("test"):
        with pytest.raises(private_index.PrivateIndexError, match="upload failed"):
            private_index.add_document("owned", "Private document")
    assert events == [
        "rag-delete",
        ("proof", "successor"),
        ("delete", "successor"),
        "gcs-delete",
    ]


def test_eos_purge_stops_replacement_if_selected_delete_did_not_finish(monkeypatch):
    from vertexai.preview import rag

    from rubik_core.ai_providers import embedding_index
    from rubik_core.ingestion.eos import pipeline
    from rubik_core.tenancy import corpora

    monkeypatch.setattr(corpora, "assert_own_rag_file", lambda name, _: name)
    monkeypatch.setattr(
        bridge,
        "begin_cloud_source_update",
        lambda *_: bridge.CloudSourceUpdate("original", 42),
    )
    monkeypatch.setattr(bridge, "settle_failed_managed_update", lambda *_: False)
    monkeypatch.setattr(
        embedding_index,
        "cloud_index_versions",
        lambda *_: (_spec("0123456789abcdef"), None),
    )
    monkeypatch.setattr(
        rag, "delete_file", lambda **_: (_ for _ in ()).throw(RuntimeError("blocked"))
    )
    with use_workspace("test"):
        with pytest.raises(RuntimeError, match="remains published"):
            pipeline._purge_stale_ragfiles(
                {"handbook.md": [(f"{RESOURCE}/ragFiles/owned", URI)]},
                "handbook.md",
                RESOURCE,
            )


def test_selected_read_repairs_one_proven_failed_small_source(monkeypatch):
    spec = _spec("0123456789abcdef")
    event = SimpleNamespace(
        source_uri=URI, operation="upsert", status="failed", managed_applied=True
    )
    state = {"pending": True}
    repaired = []
    monkeypatch.setattr(bridge, "active_cloud_index", lambda _: spec)
    monkeypatch.setattr(bridge, "_repair_private_orphans", lambda _: None)
    monkeypatch.setattr(
        bridge,
        "pending_updates",
        lambda *_args, **_kwargs: [event] if state["pending"] else [],
    )
    monkeypatch.setattr(bridge, "source_updates", lambda *_args, **_kwargs: [event])
    monkeypatch.setattr(bridge, "_source_size", lambda _: 100)
    from contextlib import nullcontext

    monkeypatch.setattr(bridge, "_automatic_repair_lock", lambda _: nullcontext())

    def repair(_resource, **kwargs):
        repaired.append(kwargs)
        state["pending"] = False

    monkeypatch.setattr(bridge, "reconcile_pending", repair)
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
    monkeypatch.setattr(bridge, "_bound_source_membership", lambda _: {URI})
    monkeypatch.setattr(settings, "GCS_BUCKET_NAME", "bucket")
    with use_workspace("test"):
        assert bridge.query_cloud_resource(RESOURCE, "handbook") == [_hit()]
    assert repaired == [{"automatic": True, "max_batches": 1}]


def test_selected_read_never_replays_in_flight_managed_update(monkeypatch):
    spec = _spec("0123456789abcdef")
    event = SimpleNamespace(
        source_uri=URI, operation="upsert", status="pending", managed_applied=True
    )
    monkeypatch.setattr(bridge, "active_cloud_index", lambda _: spec)
    monkeypatch.setattr(bridge, "_repair_private_orphans", lambda _: None)
    monkeypatch.setattr(bridge, "pending_updates", lambda *_args, **_kwargs: [event])
    monkeypatch.setattr(bridge, "source_updates", lambda *_args, **_kwargs: [event])
    monkeypatch.setattr(
        bridge,
        "reconcile_pending",
        lambda *_args, **_kwargs: pytest.fail("raced write"),
    )
    with use_workspace("test"):
        with pytest.raises(CloudVectorIndexError, match="still running"):
            bridge.query_cloud_resource(RESOURCE, "handbook")


def test_timed_out_private_filing_removes_late_vertex_file(monkeypatch):
    from rubik_core.chat_files import INDEX_CHAT_ONLY, service

    monkeypatch.setattr(private_index, "available", lambda: True)
    monkeypatch.setattr(service, "library_document", lambda _: "Private document")
    monkeypatch.setattr(settings, "CHAT_FILE_INDEX_TIMEOUT_SECONDS", 0.001)
    row = SimpleNamespace(
        id="owned",
        extracted_text="Private document",
        rag_file_name=None,
        index_status=None,
        index_note=None,
    )
    db = SimpleNamespace(commit=lambda: None)
    removed = []

    async def scenario():
        finish_upload = asyncio.Event()
        cleaned = asyncio.Event()

        async def fake_threadpool(func, *args, **kwargs):
            if func is private_index.add_document:
                await finish_upload.wait()
                return f"{RESOURCE}/ragFiles/owned"
            assert func is private_index.remove
            removed.append((args, kwargs))
            cleaned.set()
            return True

        monkeypatch.setattr(service, "run_in_threadpool", fake_threadpool)
        await service._file_in_library(db, row)
        assert row.index_status == INDEX_CHAT_ONLY
        finish_upload.set()
        await asyncio.wait_for(cleaned.wait(), timeout=1)

    asyncio.run(scenario())
    assert removed == [((f"{RESOURCE}/ragFiles/owned",), {"upload_id": "owned"})]


def test_cancelled_private_filing_removes_late_vertex_file(monkeypatch):
    from rubik_core.chat_files import service

    monkeypatch.setattr(private_index, "available", lambda: True)
    monkeypatch.setattr(service, "library_document", lambda _: "Private document")
    row = SimpleNamespace(id="owned", extracted_text="Private document")
    db = SimpleNamespace(commit=lambda: None)
    removed = []

    async def scenario():
        started = asyncio.Event()
        finish_upload = asyncio.Event()
        cleaned = asyncio.Event()

        async def fake_threadpool(func, *args, **kwargs):
            if func is private_index.add_document:
                started.set()
                await finish_upload.wait()
                return f"{RESOURCE}/ragFiles/owned"
            assert func is private_index.remove
            removed.append((args, kwargs))
            cleaned.set()
            return True

        monkeypatch.setattr(service, "run_in_threadpool", fake_threadpool)
        filing = asyncio.create_task(service._file_in_library(db, row))
        await started.wait()
        filing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await filing
        finish_upload.set()
        await asyncio.wait_for(cleaned.wait(), timeout=1)

    asyncio.run(scenario())
    assert removed == [((f"{RESOURCE}/ragFiles/owned",), {"upload_id": "owned"})]


def test_private_orphan_repair_uses_exact_chat_only_owner_row(monkeypatch):
    from contextlib import nullcontext

    from rubik_core.ai_providers import cloud_rag_bridge
    from rubik_core.database import db as database_db
    from rubik_core.tenancy import corpora

    uri = "gs://bucket/workspaces/test/chat_files/private/chatfile__owned.md"
    rag_file = f"{RESOURCE}/ragFiles/owned"
    row = SimpleNamespace(
        id="owned",
        owner_email="owner@example.test",
        index_status="chat_only",
        rag_file_name=None,
    )
    monkeypatch.setattr(corpora, "require_bound_corpus_id", lambda value: value)
    monkeypatch.setattr(corpora, "corpus_id", lambda *_args, **_kwargs: RESOURCE)
    monkeypatch.setattr(
        cloud_rag_bridge, "_automatic_repair_lock", lambda _: nullcontext()
    )
    monkeypatch.setattr(private_index, "_verified_ragfiles", lambda _: {rag_file: uri})

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
    removed = []
    monkeypatch.setattr(
        private_index,
        "remove",
        lambda name, *, upload_id: removed.append((name, upload_id)) or True,
    )
    with use_workspace("test"):
        assert private_index.repair_orphan_ragfiles(RESOURCE) == 1
    assert removed == [(rag_file, "owned")]
    row.index_status = "pending"
    with use_workspace("test"):
        with pytest.raises(private_index.PrivateIndexError, match="cannot be mapped"):
            private_index.repair_orphan_ragfiles(RESOURCE)
    assert removed == [(rag_file, "owned")]


@pytest.mark.parametrize("has_chat_only_row", [False, True])
def test_private_orphan_repair_deletes_unowned_ragfile_with_selected_index(
    monkeypatch, tmp_path, has_chat_only_row
):
    from contextlib import nullcontext

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from rubik_core.ai_providers import embedding_index
    from rubik_core.chat_files import INDEX_CHAT_ONLY
    from rubik_core.config import AppMode
    from rubik_core.database import db as database_db
    from rubik_core.database.models import ChatUpload
    from rubik_core.tenancy import corpora

    engine = create_engine(f"sqlite:///{tmp_path / 'orphan.db'}")
    ChatUpload.__table__.create(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database_db, "SessionLocal", sessions)
    monkeypatch.setattr(settings, "APP_MODE", AppMode.GCP)
    monkeypatch.setattr(corpora, "require_bound_corpus_id", lambda value: value)
    monkeypatch.setattr(corpora, "corpus_id", lambda *_args, **_kwargs: RESOURCE)
    monkeypatch.setattr(private_index, "resolve_corpus", lambda **_: RESOURCE)
    monkeypatch.setattr(bridge, "_automatic_repair_lock", lambda _: nullcontext())
    monkeypatch.setattr(bridge, "cloud_source_mutation_lock", lambda *_: nullcontext())
    spec = _spec("0123456789abcdef")
    monkeypatch.setattr(bridge, "cloud_index_versions", lambda _: (spec, None))
    monkeypatch.setattr(embedding_index, "cloud_index_versions", lambda _: (spec, None))
    monkeypatch.setattr(bridge, "cloud_corpus_key", lambda _: spec.corpus)
    monkeypatch.setattr(bridge, "pending_updates", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(bridge, "mark_active", lambda _: None)
    monkeypatch.setattr(bridge, "_source_generation", lambda *_args: 7)
    monkeypatch.setattr(bridge, "enqueue_source_update", lambda *_args: "orphan-delete")
    proven = []
    tombstoned = []
    applied = []
    monkeypatch.setattr(bridge, "mark_update_managed_applied", proven.append)
    monkeypatch.setattr(
        bridge,
        "tombstone_source",
        lambda *args, **kwargs: tombstoned.append((args, kwargs)),
    )
    monkeypatch.setattr(bridge, "mark_update_applied", applied.append)
    monkeypatch.setattr(bridge, "_clear_older_source_updates", lambda *_: None)
    deleted_blobs = []
    monkeypatch.setattr(
        private_index,
        "_cloud_blob",
        lambda _: SimpleNamespace(delete=lambda **kwargs: deleted_blobs.append(kwargs)),
    )

    with use_workspace("test"):
        uri = private_index.cloud_source_uri("owned")
        rag_file = f"{RESOURCE}/ragFiles/owned"
        files = [SimpleNamespace(name=rag_file, display_name="chatfile__owned.md")]
        deleted_files = []

        def delete_file(*, name):
            deleted_files.append(name)
            files[:] = [item for item in files if item.name != name]

        monkeypatch.setattr(
            private_index,
            "_rag",
            lambda: SimpleNamespace(
                list_files=lambda **_: list(files), delete_file=delete_file
            ),
        )
        if has_chat_only_row:
            with sessions() as db:
                db.add(
                    ChatUpload(
                        id="owned",
                        owner_email="owner@example.test",
                        filename="owned.md",
                        kind="text",
                        content_hash="hash",
                        index_status=INDEX_CHAT_ONLY,
                    )
                )
                db.commit()
        with pytest.raises(CloudVectorIndexError, match="Bound vector sources"):
            bridge._bound_source_references(RESOURCE)
        assert private_index.repair_orphan_ragfiles(RESOURCE) == 1
        assert bridge._bound_source_references(RESOURCE) == []

    assert deleted_files == [rag_file]
    assert proven == applied == ["orphan-delete"]
    assert tombstoned == [((spec, uri, 7), {"update_id": "orphan-delete"})]
    assert deleted_blobs == [{"if_generation_match": 7}]
    engine.dispose()


def test_private_cleanup_refuses_mismatched_upload_identity(monkeypatch):
    from rubik_core.tenancy import corpora

    rag_file = f"{RESOURCE}/ragFiles/other"
    monkeypatch.setattr(corpora, "assert_own_rag_file", lambda name, _: name)
    monkeypatch.setattr(
        private_index,
        "_rag",
        lambda: SimpleNamespace(
            list_files=lambda **_: [
                SimpleNamespace(name=rag_file, display_name="chatfile__other.md")
            ]
        ),
    )
    with use_workspace("test"):
        with pytest.raises(private_index.PrivateIndexError, match="does not match"):
            private_index._verify_named_upload_file(RESOURCE, rag_file, "owned")

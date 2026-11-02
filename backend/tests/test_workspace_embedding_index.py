"""A BYOK embedding index is rebuilt and checked before it replaces live search."""

from __future__ import annotations

from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from sqlalchemy import delete

from rubik_core.adapters.base import Chunk
from rubik_core.adapters.chroma import ChromaAdapter, IndexChangedError
from rubik_core.ai_providers.cloud_vector_index import CloudVectorSpec
from rubik_core.ai_providers.embedding_index import (
    activate_embedding_index,
    active_cloud_index,
    active_embedding_index,
    cloud_corpus_key,
    cloud_index_versions,
    embedding_index_status,
    prepare_embedding_index,
)
from rubik_core.ai_providers.embeddings import EmbeddingProviderError
from rubik_core.ai_providers.registry import (
    IncompatibleModelRoute,
    configure_provider,
    revoke_provider,
)
from rubik_core.config import AppMode, settings
from rubik_core.control.db import control_session
from rubik_core.control.models import Workspace, WorkspaceConnector
from rubik_core.models import get_embeddings
from rubik_core.tenancy.context import use_workspace


@pytest.fixture
def vector_workspaces(tmp_path, monkeypatch):
    from rubik_core.ai_providers import embeddings

    monkeypatch.setattr(embeddings, "record_usage", lambda **kwargs: None)
    monkeypatch.setattr(settings, "CHROMA_PERSIST_DIR", str(tmp_path / "chroma"))
    monkeypatch.setattr(settings, "LOCAL_DATA_DIR", str(tmp_path / "secrets"))
    monkeypatch.setattr(settings, "APP_MODE", AppMode.LOCAL)
    monkeypatch.setattr(settings, "CLOUDSQL_CONNECTION_NAME", "")
    with control_session() as session:
        session.add_all(
            [
                Workspace(slug="vec-acme", name="Vector Acme", status="active"),
                Workspace(slug="vec-other", name="Vector Other", status="active"),
            ]
        )
        session.commit()
    yield
    with control_session() as session:
        session.execute(
            delete(WorkspaceConnector).where(
                WorkspaceConnector.workspace_slug.in_(("vec-acme", "vec-other"))
            )
        )
        for slug in ("vec-acme", "vec-other"):
            session.delete(session.get(Workspace, slug))
        session.commit()


def _chunk(identifier: str, text: str, dimension: int = 768) -> Chunk:
    return Chunk(
        chunk_id=identifier,
        doc_id=identifier,
        chunk_index=0,
        text=text,
        embedding=[0.1] * dimension,
        metadata={"doc_id": identifier, "chunk_index": 0},
    )


def _stub_openai(monkeypatch):
    from rubik_core.ai_providers import embeddings

    calls = []

    def post(url, *, headers, json, timeout, allow_redirects):
        calls.append((url, headers["Authorization"], json["model"], allow_redirects))
        marker = 0.1 if "acme" in headers["Authorization"] else 0.2
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "model": "text-embedding-3-small",
                "data": [
                    {"index": index, "embedding": [marker] * 1536}
                    for index in range(len(json["input"]))
                ],
                "usage": {"prompt_tokens": len(json["input"])},
            },
        )

    monkeypatch.setattr(embeddings.requests, "post", post)
    return calls


def test_two_workspaces_rebuild_and_activate_with_separate_keys(
    vector_workspaces, monkeypatch
):
    calls = _stub_openai(monkeypatch)
    for slug in ("vec-acme", "vec-other"):
        with use_workspace(slug):
            configure_provider("openai", api_key=f"{slug}-key")
            source = ChromaAdapter()
            assert source.upsert([_chunk("shared-id", f"{slug} document")]).success
            candidate = prepare_embedding_index("openai", "text-embedding-3-small")
            assert active_embedding_index().provider == "vertex_managed"
            assert (
                embedding_index_status()["candidate"]["version"] == candidate["version"]
            )
            activated = activate_embedding_index(candidate["version"])
            assert activated["indexed_chunks"] == 1
            assert active_embedding_index().provider == "openai"
            assert ChromaAdapter().collection_name != source.collection_name
            assert get_embeddings().embed_query("question")[0] == (
                0.1 if slug == "vec-acme" else 0.2
            )
            hits = ChromaAdapter().query(get_embeddings().embed_query("document"))
            assert hits[0].chunk.text == f"{slug} document"
            with pytest.raises(ValueError, match="changed"):
                source.query([0.1] * 768)
    assert {call[1] for call in calls} == {
        "Bearer vec-acme-key",
        "Bearer vec-other-key",
    }
    assert all(call[0] == "https://api.openai.com/v1/embeddings" for call in calls)
    assert all(call[3] is False for call in calls)


def test_nondefault_corpus_cutover_keeps_default_route(vector_workspaces, monkeypatch):
    _stub_openai(monkeypatch)
    with use_workspace("vec-acme"):
        configure_provider("openai", api_key="acme-key")
        default = active_embedding_index()
        candidate = prepare_embedding_index(
            "openai", "text-embedding-3-small", corpus="private"
        )
        activate_embedding_index(candidate["version"], corpus="private")
        assert active_embedding_index("private").provider == "openai"
        assert active_embedding_index() == default
        assert get_embeddings().spec == default


def test_source_change_blocks_cutover_and_keeps_managed_route(
    vector_workspaces, monkeypatch
):
    _stub_openai(monkeypatch)
    with use_workspace("vec-acme"):
        configure_provider("openai", api_key="acme-key")
        source = ChromaAdapter()
        assert source.upsert([_chunk("first", "First")]).success
        candidate = prepare_embedding_index("openai", "text-embedding-3-small")
        # Count stays constant; cutover must also notice changed content.
        assert source.upsert([_chunk("first", "Revised")]).success
        with pytest.raises(IncompatibleModelRoute, match="source changed"):
            activate_embedding_index(candidate["version"])
        assert active_embedding_index().provider == "vertex_managed"


def test_revocation_stops_embedding_and_never_calls_managed_vertex(
    vector_workspaces, monkeypatch
):
    _stub_openai(monkeypatch)
    with use_workspace("vec-acme"):
        configure_provider("openai", api_key="acme-key")
        candidate = prepare_embedding_index("openai", "text-embedding-3-small")
        activate_embedding_index(candidate["version"])
        embeddings = get_embeddings()
        revoke_provider("openai")
        monkeypatch.setattr(
            "rubik_core.models.GenAIEmbeddings.embed_documents",
            lambda *args: pytest.fail("Managed Vertex fallback is forbidden"),
        )
        with pytest.raises(IncompatibleModelRoute, match="disconnected"):
            embeddings.embed_query("question")


def test_dimension_mismatch_is_refused_before_chroma(vector_workspaces, monkeypatch):
    _stub_openai(monkeypatch)
    with use_workspace("vec-acme"):
        configure_provider("openai", api_key="acme-key")
        candidate = prepare_embedding_index("openai", "text-embedding-3-small")
        activate_embedding_index(candidate["version"])
        adapter = ChromaAdapter()
        result = adapter.upsert([_chunk("wrong", "Wrong length")])
        assert result.success is False
        assert result.error == "Embedding dimension mismatch"
        unpinned = adapter.upsert([_chunk("unpinned", "Same length", 1536)])
        assert unpinned.error == "Embedding source does not match active index"
        assert adapter.upsert(
            [_chunk("pinned", "Same length", 1536)],
            embedding_spec=active_embedding_index(),
        ).success
        with pytest.raises(ValueError, match="dimension"):
            adapter.query([0.1] * 768)


def test_provider_error_is_redacted_without_vertex_fallback(
    vector_workspaces, monkeypatch
):
    from rubik_core.ai_providers import embeddings

    with use_workspace("vec-acme"):
        configure_provider("openai", api_key="acme-key")
        monkeypatch.setattr(
            embeddings.requests,
            "post",
            lambda *args, **kwargs: SimpleNamespace(
                status_code=401, text="secret key and document content"
            ),
        )
        monkeypatch.setattr(
            "rubik_core.models.GenAIEmbeddings.embed_documents",
            lambda *args: pytest.fail("Managed Vertex fallback is forbidden"),
        )
        with pytest.raises(EmbeddingProviderError) as exc:
            prepare_embedding_index("openai", "text-embedding-3-small")
        assert "401" in str(exc.value)
        assert "secret" not in str(exc.value)


def test_gemini_embedding_probe_uses_workspace_key_and_fixed_dimensions(
    vector_workspaces, monkeypatch
):
    from rubik_core.ai_providers import embeddings

    calls = []

    class FakeClient:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            self.models = self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def embed_content(self, *, model, contents, config):
            assert model == "gemini-embedding-2"
            assert config.output_dimensionality == 768
            return SimpleNamespace(
                embeddings=[SimpleNamespace(values=[0.2] * 768) for _ in contents],
                metadata=SimpleNamespace(billable_character_count=40),
            )

    monkeypatch.setattr(embeddings.genai, "Client", FakeClient)
    for slug in ("vec-acme", "vec-other"):
        with use_workspace(slug):
            configure_provider("gemini", api_key=f"{slug}-key")
            candidate = prepare_embedding_index("gemini", "gemini-embedding-2")
            activate_embedding_index(candidate["version"])
            assert get_embeddings().embed_query("query") == [0.2] * 768
    assert [call["api_key"] for call in calls] == [
        "vec-acme-key",
        "vec-acme-key",
        "vec-other-key",
        "vec-other-key",
    ]
    assert all(call["vertexai"] is False for call in calls)


def test_cloud_embedding_cutover_requires_bound_corpus(vector_workspaces, monkeypatch):
    with use_workspace("vec-acme"):
        monkeypatch.setattr(settings, "APP_MODE", AppMode.GCP)
        with pytest.raises(IncompatibleModelRoute, match="not bound"):
            prepare_embedding_index("openai", "text-embedding-3-small")


def test_cloud_index_pointer_requires_exact_workspace_and_bound_resource(
    vector_workspaces, monkeypatch
):
    resource = "projects/p/locations/us-central1/ragCorpora/acme"
    monkeypatch.setattr(settings, "APP_MODE", AppMode.GCP)
    with control_session() as session:
        workspace = session.get(Workspace, "vec-acme")
        workspace.corpus_ids = {"default": resource}
        session.commit()
    with use_workspace("vec-acme"):
        key = cloud_corpus_key(resource)
        spec = CloudVectorSpec(
            "vec-acme",
            key,
            "openai",
            "text-embedding-3-small",
            1536,
            "0123456789abcdef",
        )
        with control_session() as session:
            session.add(
                WorkspaceConnector(
                    workspace_slug="vec-acme",
                    connector="ai_embedding_indexes",
                    status="connected",
                    config={"corpora": {key: {"active": asdict(spec)}}},
                )
            )
            session.commit()
        assert active_cloud_index(resource) == spec
        assert cloud_index_versions(resource) == (spec, None)
        status = embedding_index_status()
        assert status["storage"] == "cloud_sql_pgvector"
        assert status["active"]["provider"] == "openai"
        assert status["corpora"] == [{"key": key, "label": "Default knowledge"}]
    with use_workspace("vec-other"):
        with pytest.raises(ValueError, match="not bound"):
            active_cloud_index(resource)


def test_cloud_backfill_switches_pointer_only_after_manifest_verification(
    vector_workspaces, monkeypatch
):
    from rubik_core.ai_providers import cloud_embedding_workflow as workflow
    from rubik_core.ai_providers import cloud_rag_bridge
    from rubik_core.ai_providers.cloud_sources import CloudSource
    from rubik_core.ai_providers.cloud_vector_index import (
        CloudVectorIndexError,
        IndexVerification,
    )

    resource = "projects/p/locations/us-central1/ragCorpora/acme"
    uri = "gs://bucket/workspaces/vec-acme/rags/source.txt"
    monkeypatch.setattr(settings, "APP_MODE", AppMode.GCP)
    with control_session() as session:
        workspace = session.get(Workspace, "vec-acme")
        workspace.corpus_ids = {"default": resource}
        session.commit()
    generation = [42]
    checks = []
    chunk = Chunk(
        chunk_id="one",
        doc_id="one",
        chunk_index=0,
        text="A source",
        metadata={"source_path": uri},
    )
    monkeypatch.setattr(workflow, "_source_uris", lambda resource: {uri})
    monkeypatch.setattr(
        workflow,
        "load_gcs_source",
        lambda uri: CloudSource(uri, generation[0], "a" * 64, [chunk]),
    )
    monkeypatch.setattr(workflow, "provider_connection_route", lambda *a, **k: object())
    monkeypatch.setattr(workflow, "embed_connection_probe", lambda *a: None)
    monkeypatch.setattr(
        workflow,
        "embed_with_route",
        lambda route, texts, dimension: [[0.1] * dimension for _ in texts],
    )
    monkeypatch.setattr(
        workflow, "create_candidate", lambda spec: checks.append("created")
    )
    monkeypatch.setattr(
        workflow,
        "replace_source",
        lambda spec, uri, generation, digest, chunks: checks.append(
            ("indexed", chunks[0].embedding[0])
        ),
    )
    monkeypatch.setattr(
        cloud_rag_bridge,
        "reconcile_pending",
        lambda resource: checks.append("reconciled"),
    )
    monkeypatch.setattr(
        workflow,
        "verify_candidate",
        lambda spec, expected: (
            checks.append(("verified", expected[uri][0]))
            or IndexVerification(1, 1, "f" * 64)
        ),
    )
    monkeypatch.setattr(workflow, "build_ann_index", lambda spec: checks.append("ann"))
    monkeypatch.setattr(workflow, "mark_active", lambda spec: checks.append("active"))
    with use_workspace("vec-acme"):
        result = prepare_embedding_index("openai", "text-embedding-3-small")
        assert active_cloud_index(resource) is None
        assert result["indexed_sources"] == 1
        assert checks == [
            "created",
            ("indexed", 0.1),
            "reconciled",
            ("verified", 42),
            "ann",
        ]
        activated = activate_embedding_index(result["version"])
        assert activated["indexed_chunks"] == 1
        assert active_cloud_index(resource).version == result["version"]
        assert checks == [
            "created",
            ("indexed", 0.1),
            "reconciled",
            ("verified", 42),
            "ann",
            "reconciled",
            ("verified", 42),
            "ann",
            "active",
        ]
        second = prepare_embedding_index("openai", "text-embedding-3-small")

        def fail_activation(_spec):
            raise CloudVectorIndexError("concurrent source update")

        monkeypatch.setattr(workflow, "mark_active", fail_activation)
        with pytest.raises(CloudVectorIndexError, match="retry after sync"):
            activate_embedding_index(second["version"])
        assert active_cloud_index(resource).version == result["version"]
        assert embedding_index_status()["candidate"]["version"] == second["version"]


def test_cloud_source_generation_change_blocks_candidate(
    vector_workspaces, monkeypatch
):
    from rubik_core.ai_providers import cloud_embedding_workflow as workflow
    from rubik_core.ai_providers import cloud_rag_bridge
    from rubik_core.ai_providers.cloud_sources import CloudSource
    from rubik_core.ai_providers.cloud_vector_index import CloudVectorIndexError

    resource = "projects/p/locations/us-central1/ragCorpora/acme"
    uri = "gs://bucket/workspaces/vec-acme/rags/source.txt"
    monkeypatch.setattr(settings, "APP_MODE", AppMode.GCP)
    with control_session() as session:
        workspace = session.get(Workspace, "vec-acme")
        workspace.corpus_ids = {"default": resource}
        session.commit()
    generations = iter((42, 43))
    chunk = Chunk(
        chunk_id="one",
        doc_id="one",
        chunk_index=0,
        text="A source",
        metadata={"source_path": uri},
    )
    monkeypatch.setattr(workflow, "_source_uris", lambda resource: {uri})
    monkeypatch.setattr(
        workflow,
        "load_gcs_source",
        lambda uri: CloudSource(uri, next(generations), "a" * 64, [chunk]),
    )
    monkeypatch.setattr(workflow, "provider_connection_route", lambda *a, **k: object())
    monkeypatch.setattr(workflow, "embed_connection_probe", lambda *a: None)
    monkeypatch.setattr(
        workflow,
        "embed_with_route",
        lambda route, texts, dimension: [[0.1] * dimension for _ in texts],
    )
    monkeypatch.setattr(workflow, "create_candidate", lambda spec: None)
    monkeypatch.setattr(workflow, "replace_source", lambda *a: None)
    monkeypatch.setattr(cloud_rag_bridge, "reconcile_pending", lambda resource: 0)
    monkeypatch.setattr(
        workflow,
        "verify_candidate",
        lambda *a: pytest.fail("source changed before verification"),
    )
    with use_workspace("vec-acme"):
        with pytest.raises(CloudVectorIndexError, match="changed"):
            prepare_embedding_index("openai", "text-embedding-3-small")
        assert active_cloud_index(resource) is None


def test_local_client_index_cannot_be_used_as_vertex_rag_index(
    vector_workspaces, monkeypatch
):
    _stub_openai(monkeypatch)
    with use_workspace("vec-acme"):
        configure_provider("openai", api_key="acme-key")
        candidate = prepare_embedding_index("openai", "text-embedding-3-small")
        activate_embedding_index(candidate["version"])
        monkeypatch.setattr(settings, "APP_MODE", AppMode.GCP)
        with pytest.raises(IncompatibleModelRoute, match="Vertex RAG remains managed"):
            get_embeddings()


def test_query_rejects_same_dimension_vector_from_prior_index(
    vector_workspaces, monkeypatch
):
    from rubik_core.ai_providers import embeddings

    class FakeClient:
        def __init__(self, **kwargs):
            self.models = self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def embed_content(self, *, model, contents, config):
            return SimpleNamespace(
                embeddings=[SimpleNamespace(values=[0.3] * 768) for _ in contents]
            )

    monkeypatch.setattr(embeddings.genai, "Client", FakeClient)
    with use_workspace("vec-acme"):
        configure_provider("gemini", api_key="gemini-key")
        first = prepare_embedding_index("gemini", "gemini-embedding-2")
        activate_embedding_index(first["version"])
        old_adapter = ChromaAdapter()
        old_vector = get_embeddings().embed_query("question")
        second = prepare_embedding_index("gemini", "gemini-embedding-2")
        activate_embedding_index(second["version"])
        with pytest.raises(ValueError, match="does not match"):
            ChromaAdapter().query(old_vector)
        with pytest.raises(ValueError, match="does not match"):
            ChromaAdapter().query([0.3] * 768)
        with pytest.raises(IndexChangedError, match="changed"):
            old_adapter.get_chunks_by_metadata({"doc_id": "anything"})


def test_managed_vector_is_pinned_before_same_dimension_client_cutover(
    vector_workspaces, monkeypatch
):
    from rubik_core.ai_providers import embeddings

    monkeypatch.setattr(
        embeddings,
        "embed_with_route",
        lambda route, texts, dimension: [[0.4] * dimension for _ in texts],
    )
    with use_workspace("vec-acme"):
        managed_vector = get_embeddings().embed_query("question")
        assert managed_vector.index_spec.provider == "vertex_managed"
        configure_provider("gemini", api_key="gemini-key")
        candidate = prepare_embedding_index("gemini", "gemini-embedding-2")
        activate_embedding_index(candidate["version"])
        with pytest.raises(ValueError, match="does not match"):
            ChromaAdapter().query(managed_vector)


def test_embedding_call_rechecks_active_index_after_provider_returns(
    vector_workspaces, monkeypatch
):
    from rubik_core.ai_providers import embedding_index, embeddings

    with use_workspace("vec-acme"):
        model = get_embeddings()
        current = model.spec
        checks = iter([current, replace(current, version="changed")])
        monkeypatch.setattr(
            embedding_index, "active_embedding_index", lambda corpus: next(checks)
        )
        monkeypatch.setattr(
            embeddings,
            "embed_with_route",
            lambda route, texts, dimension: [[0.1] * dimension],
        )
        with pytest.raises(IncompatibleModelRoute, match="changed"):
            model.embed_query("question")


def test_missing_active_collection_fails_instead_of_recreating(
    vector_workspaces, monkeypatch
):
    _stub_openai(monkeypatch)
    with use_workspace("vec-acme"):
        configure_provider("openai", api_key="acme-key")
        candidate = prepare_embedding_index("openai", "text-embedding-3-small")
        activate_embedding_index(candidate["version"])
        adapter = ChromaAdapter()
        adapter.client.delete_collection(name=adapter.collection_name)
        with pytest.raises(IndexChangedError, match="unavailable"):
            ChromaAdapter()
        with pytest.raises(IndexChangedError, match="unavailable"):
            adapter.query(get_embeddings().embed_query("question"))
        assert active_embedding_index().version == candidate["version"]


def test_reset_only_removes_current_collection(vector_workspaces):
    with use_workspace("vec-acme"):
        adapter = ChromaAdapter()
        unrelated = adapter.client.get_or_create_collection(name="unrelated")
        unrelated.add(ids=["other"], documents=["Other"], embeddings=[[0.1] * 768])
        assert adapter.reset() is True
        assert adapter.client.get_collection(name="unrelated").count() == 1


def test_cutover_guard_inside_query_and_metadata_read_is_not_swallowed(
    vector_workspaces, monkeypatch
):
    with use_workspace("vec-acme"):
        adapter = ChromaAdapter()
        checks = iter([None, IndexChangedError("Vector index changed")])

        def check():
            result = next(checks)
            if result is not None:
                raise result

        monkeypatch.setattr(adapter, "_check_bound", check)
        with pytest.raises(IndexChangedError, match="changed"):
            adapter.query([0.1] * 768)
        checks = iter([None, IndexChangedError("Vector index changed")])
        with pytest.raises(IndexChangedError, match="changed"):
            adapter.get_chunks_by_metadata({"doc_id": "anything"})

"""Tenant vector index contracts before any Cloud SQL cutover is enabled."""

from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace

import pytest
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from rubik_core.adapters.base import Chunk
from rubik_core.ai_providers import cloud_vector_index as cloud
from rubik_core.config import AppMode, settings
from rubik_core.database.models import (
    Base,
    CloudVectorChunk,
    CloudVectorSource,
    CloudVectorUpdate,
    CloudVectorVersion,
)
from rubik_core.tenancy.context import use_workspace

_URI = "gs://vector-test/workspaces/test/rag/source.pdf"
_HASH = "a" * 64


@pytest.fixture
def vector_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'vectors.db'}")
    Base.metadata.create_all(
        engine,
        tables=[
            CloudVectorVersion.__table__,
            CloudVectorSource.__table__,
            CloudVectorChunk.__table__,
            CloudVectorUpdate.__table__,
        ],
    )
    monkeypatch.setattr(cloud, "SessionLocal", lambda: Session(engine))
    monkeypatch.setattr(settings, "GCS_BUCKET_NAME", "vector-test")
    monkeypatch.setattr(settings, "APP_MODE", AppMode.LOCAL)
    yield engine
    engine.dispose()


def _spec(corpus="c_default", version="0123456789abcdef"):
    return cloud.CloudVectorSpec(
        "test", corpus, "gemini", "gemini-embedding-2", 768, version
    )


def _chunk(identifier, value, *, doc_id="doc-1", source_uri=_URI):
    return Chunk(
        chunk_id=identifier,
        doc_id=doc_id,
        chunk_index=0,
        text=f"Chunk {identifier}",
        embedding=[value] * 768,
        metadata={"doc_id": doc_id, "audience": "team", "source_path": source_uri},
    )


def test_candidate_requires_exact_manifest_and_index_before_query(vector_db):
    with use_workspace("test"):
        spec = _spec()
        cloud.create_candidate(spec)
        cloud.replace_source(spec, _URI, 42, _HASH, [_chunk("one", 0.1)])
        query = cloud.CloudIndexVector(spec, [0.1] * 768)
        with pytest.raises(cloud.CloudVectorIndexError, match="not ready"):
            cloud.query_chunks(spec, query)
        with pytest.raises(cloud.CloudVectorIndexError, match="manifest"):
            cloud.verify_candidate(spec, {_URI: (43, _HASH)})
        verified = cloud.verify_candidate(spec, {_URI: (42, _HASH)})
        assert verified.source_count == verified.chunk_count == 1
        with pytest.raises(cloud.CloudVectorIndexError, match="not ready"):
            cloud.query_chunks(spec, query)
        cloud.build_ann_index(spec)
        cloud.mark_active(spec)
        hits = cloud.query_chunks(spec, query, filters={"audience": "team"})
        assert [item.chunk.chunk_id for item in hits] == ["one"]
        assert hits[0].score == pytest.approx(0)
        assert cloud.query_chunks(spec, query, filters={"audience": "other"}) == []


def test_source_generation_tombstone_and_corpus_isolation(vector_db):
    with use_workspace("test"):
        first, second = _spec(), _spec(corpus="c_private")
        for spec in (first, second):
            cloud.create_candidate(spec)
            cloud.replace_source(spec, _URI, 42, _HASH, [_chunk("same-id", 0.1)])
            cloud.verify_candidate(spec, {_URI: (42, _HASH)})
            cloud.build_ann_index(spec)
            cloud.mark_active(spec)
        with pytest.raises(cloud.CloudVectorIndexError, match="does not match"):
            cloud.query_chunks(first, cloud.CloudIndexVector(second, [0.1] * 768))
        with pytest.raises(cloud.CloudVectorIndexError, match="Stale"):
            cloud.replace_source(first, _URI, 41, _HASH, [_chunk("old", 0.1)])
        cloud.tombstone_source(first, _URI, 43)
        assert (
            cloud.query_chunks(first, cloud.CloudIndexVector(first, [0.1] * 768)) == []
        )
        assert (
            len(cloud.query_chunks(second, cloud.CloudIndexVector(second, [0.1] * 768)))
            == 1
        )
        with pytest.raises(cloud.CloudVectorIndexError, match="Stale"):
            cloud.replace_source(first, _URI, 42, _HASH, [_chunk("resurrect", 0.1)])
    with use_workspace("another"):
        with pytest.raises(cloud.CloudVectorIndexError, match="another workspace"):
            cloud.query_chunks(first, cloud.CloudIndexVector(first, [0.1] * 768))


def test_pending_outbox_blocks_cutover_and_search(vector_db):
    with use_workspace("test"):
        spec = _spec()
        cloud.create_candidate(spec)
        update_id = cloud.enqueue_source_update("c_default", _URI, 42, "upsert")
        assert cloud.source_updates("c_default", _URI)[0].id == update_id
        assert [item.id for item in cloud.pending_updates("c_default")] == [update_id]
        with pytest.raises(cloud.CloudVectorIndexError, match="pending"):
            cloud.verify_candidate(spec, {})
        cloud.mark_update_failed(update_id)
        assert cloud.pending_updates("c_default")[0].attempts == 1
        with pytest.raises(cloud.CloudVectorIndexError, match="not reached"):
            cloud.mark_update_applied(update_id)
        cloud.replace_source(spec, _URI, 42, _HASH, [_chunk("one", 0.1)])
        cloud.mark_update_applied(update_id)
        assert cloud.pending_updates("c_default") == []
        cloud.verify_candidate(spec, {_URI: (42, _HASH)})
        cloud.build_ann_index(spec)
        cloud.mark_active(spec)
        new_id = cloud.enqueue_source_update("c_default", _URI, 43, "delete")
        with pytest.raises(cloud.CloudVectorIndexError, match="pending"):
            cloud.query_chunks(spec, cloud.CloudIndexVector(spec, [0.1] * 768))
        with pytest.raises(cloud.CloudVectorIndexError, match="not reached"):
            cloud.mark_update_applied(new_id)
        cloud.tombstone_source(spec, _URI, 43)
        cloud.mark_update_applied(new_id)


def test_pending_update_reaches_active_and_building_candidate(vector_db):
    with use_workspace("test"):
        active = _spec()
        candidate = _spec(version="fedcba9876543210")
        for spec in (active, candidate):
            cloud.create_candidate(spec)
        cloud.verify_candidate(active, {})
        cloud.build_ann_index(active)
        cloud.mark_active(active)
        update_id = cloud.enqueue_source_update("c_default", _URI, 42, "upsert")
        cloud.replace_source(active, _URI, 42, _HASH, [_chunk("one", 0.1)])
        with pytest.raises(cloud.CloudVectorIndexError, match="not reached"):
            cloud.mark_update_applied(update_id)
        cloud.replace_source(candidate, _URI, 42, _HASH, [_chunk("one", 0.2)])
        cloud.mark_update_applied(update_id)
        cloud.verify_candidate(candidate, {_URI: (42, _HASH)})


def test_newer_source_generation_supersedes_older_outbox_event(vector_db):
    with use_workspace("test"):
        spec = _spec()
        cloud.create_candidate(spec)
        older = cloud.enqueue_source_update("c_default", _URI, 42, "delete")
        newer = cloud.enqueue_source_update("c_default", _URI, 43, "upsert")
        cloud.replace_source(spec, _URI, 43, _HASH, [_chunk("current", 0.1)])
        with pytest.raises(cloud.CloudVectorIndexError, match="not reached"):
            cloud.mark_update_applied(older)
        with pytest.raises(cloud.CloudVectorIndexError, match="successor"):
            cloud.mark_update_superseded(older, newer)
        cloud.mark_update_applied(newer)
        cloud.mark_update_superseded(older, newer)
        assert cloud.pending_updates("c_default") == []


def test_repeated_same_generation_operations_have_monotonic_order(vector_db):
    with use_workspace("test"):
        spec = _spec()
        cloud.create_candidate(spec)
        first = cloud.enqueue_source_update("c_default", _URI, 42, "upsert")
        middle = cloud.enqueue_source_update("c_default", _URI, 42, "delete")
        last = cloud.enqueue_source_update("c_default", _URI, 42, "upsert")
        events = cloud.source_updates("c_default", _URI)
        assert [event.id for event in events] == [first, middle, last]
        assert [event.sequence for event in events] == sorted(
            event.sequence for event in events
        )
        assert len({event.sequence for event in events}) == 3
        cloud.replace_source(spec, _URI, 42, _HASH, [_chunk("current", 0.1)])
        cloud.mark_update_applied(last)
        cloud.mark_update_superseded(first, last)
        cloud.mark_update_superseded(middle, last)
        assert cloud.pending_updates("c_default") == []


def test_source_allowlist_filters_before_top_k_ranking(vector_db):
    other_uri = "gs://vector-test/workspaces/test/rag/other.pdf"
    with use_workspace("test"):
        spec = _spec()
        cloud.create_candidate(spec)
        cloud.replace_source(spec, _URI, 42, _HASH, [_chunk("closer", 0.1)])
        cloud.replace_source(
            spec,
            other_uri,
            43,
            _HASH,
            [_chunk("allowed", -0.1, source_uri=other_uri)],
        )
        cloud.verify_candidate(spec, {_URI: (42, _HASH), other_uri: (43, _HASH)})
        cloud.build_ann_index(spec)
        cloud.mark_active(spec)
        query = cloud.CloudIndexVector(spec, [0.1] * 768)
        assert cloud.query_chunks(spec, query, top_k=1)[0].chunk.chunk_id == "closer"
        assert (
            cloud.query_chunks(spec, query, top_k=1, allowed_source_uris=[other_uri])[
                0
            ].chunk.chunk_id
            == "allowed"
        )
        assert cloud.query_chunks(spec, query, allowed_source_uris=[]) == []
        with pytest.raises(cloud.CloudVectorIndexError, match="outside this workspace"):
            cloud.query_chunks(
                spec,
                query,
                allowed_source_uris=["gs://vector-test/workspaces/other/rag/x.pdf"],
            )
        with pytest.raises(cloud.CloudVectorIndexError, match="allowlist"):
            cloud.query_chunks(spec, query, allowed_source_uris=[_URI] * 101)


def test_promoting_candidate_retires_old_serving_version(vector_db):
    with use_workspace("test"):
        old = _spec()
        new = _spec(version="fedcba9876543210")
        for spec in (old, new):
            cloud.create_candidate(spec)
            cloud.replace_source(spec, _URI, 42, _HASH, [_chunk("one", 0.1)])
            cloud.verify_candidate(spec, {_URI: (42, _HASH)})
            cloud.build_ann_index(spec)
        cloud.mark_active(old)
        cloud.mark_active(new)
        with Session(vector_db) as db:
            assert (
                db.query(CloudVectorChunk)
                .filter_by(corpus=old.corpus, version=old.version)
                .count()
                == 0
            )
            assert (
                db.query(CloudVectorSource)
                .filter_by(corpus=old.corpus, version=old.version)
                .count()
                == 0
            )
        with pytest.raises(cloud.CloudVectorIndexError, match="unavailable"):
            cloud.query_chunks(old, cloud.CloudIndexVector(old, [0.1] * 768))
        update_id = cloud.enqueue_source_update("c_default", _URI, 42, "delete")
        cloud.tombstone_source(new, _URI, 42)
        cloud.mark_update_applied(update_id)
        assert cloud.pending_updates("c_default") == []


def test_retired_candidate_purges_plaintext(vector_db):
    with use_workspace("test"):
        spec = _spec()
        cloud.create_candidate(spec)
        cloud.replace_source(spec, _URI, 42, _HASH, [_chunk("secret", 0.1)])
        assert cloud.source_state(spec, _URI) == (42, False)
        cloud.retire_candidate(spec)
        with Session(vector_db) as db:
            assert db.query(CloudVectorChunk).count() == 0
            assert db.query(CloudVectorSource).count() == 0
        with pytest.raises(cloud.CloudVectorIndexError, match="unavailable"):
            cloud.source_state(spec, _URI)


def test_invalid_vector_source_and_filter_fail_before_db(vector_db, monkeypatch):
    with use_workspace("test"):
        spec = _spec()
        cloud.create_candidate(spec)
        monkeypatch.setattr(cloud, "SessionLocal", lambda: pytest.fail("DB touched"))
        with pytest.raises(cloud.CloudVectorIndexError, match="outside this workspace"):
            cloud.replace_source(
                spec,
                "gs://vector-test/workspaces/other/rag/source.pdf",
                42,
                _HASH,
                [_chunk("one", 0.1)],
            )
        with pytest.raises(cloud.CloudVectorIndexError, match="dimension"):
            cloud.CloudIndexVector(spec, [0.1] * 767)
        with pytest.raises(cloud.CloudVectorIndexError, match="invalid number"):
            cloud.CloudIndexVector(spec, [float("nan")] * 768)
        with pytest.raises(cloud.CloudVectorIndexError, match="filters"):
            cloud._filter_clause({"unsafe-key;": "value"})


def test_postgres_query_and_index_contract_are_version_pinned(vector_db):
    with use_workspace("test"):
        spec = _spec()
        ddl = cloud._ann_index_sql(spec)
        query = cloud._pg_query_sql(spec, " AND doc_id = :filter_doc_id")
        assert "USING hnsw" in ddl and "vector_cosine_ops" in ddl
        assert "embedding::vector(768)" in ddl and "version = '0123456789abcdef'" in ddl
        assert "embedding::vector(768) <=> CAST(:query_vector AS vector(768))" in query
        assert "WHERE corpus = 'c_default' AND version = '0123456789abcdef'" in query
        compiled = str(text(query).compile(dialect=postgresql.dialect()))
        assert "%(query_vector)s" in compiled
        assert _URI not in compiled
        allowed_query = text(
            cloud._pg_query_sql(spec, " AND source_uri IN :allowed_source_uris")
        ).bindparams(bindparam("allowed_source_uris", expanding=True))
        assert "POSTCOMPILE_allowed_source_uris" in str(
            allowed_query.compile(dialect=postgresql.dialect())
        )


def test_production_refuses_sqlite_even_if_version_is_ready(vector_db, monkeypatch):
    with use_workspace("test"):
        spec = _spec()
        cloud.create_candidate(spec)
        cloud.verify_candidate(spec, {})
        monkeypatch.setattr(settings, "APP_MODE", AppMode.GCP)
        with pytest.raises(cloud.CloudVectorIndexError, match="requires PostgreSQL"):
            cloud.build_ann_index(spec)

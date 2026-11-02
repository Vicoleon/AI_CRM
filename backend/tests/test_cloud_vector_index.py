"""Tenant vector index contracts before any Cloud SQL cutover is enabled."""

from __future__ import annotations

import runpy
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
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
        assert cloud.source_updates("c_default", _URI)[0].managed_applied is False
        assert [item.id for item in cloud.pending_updates("c_default")] == [update_id]
        with pytest.raises(cloud.CloudVectorIndexError, match="pending"):
            cloud.verify_candidate(spec, {})
        with pytest.raises(cloud.CloudVectorIndexError, match="unconfirmed"):
            cloud.mark_update_applied(update_id)
        cloud.mark_update_managed_applied(update_id)
        cloud.mark_update_failed(update_id)
        assert cloud.pending_updates("c_default")[0].attempts == 1
        assert cloud.pending_updates("c_default")[0].managed_applied is True
        with pytest.raises(cloud.CloudVectorIndexError, match="not reached"):
            cloud.mark_update_applied(update_id)
        cloud.replace_source(spec, _URI, 42, _HASH, [_chunk("one", 0.1)])
        cloud.mark_update_applied(update_id)
        assert cloud.pending_updates("c_default") == []
        cloud.verify_candidate(spec, {_URI: (42, _HASH)})
        cloud.build_ann_index(spec)
        cloud.mark_active(spec)
        new_id = cloud.enqueue_source_update("c_default", _URI, 43, "delete")
        cloud.mark_update_managed_applied(new_id)
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
        cloud.mark_update_managed_applied(update_id)
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
        with pytest.raises(cloud.CloudVectorIndexError, match="unconfirmed"):
            cloud.mark_update_applied(older)
        with pytest.raises(cloud.CloudVectorIndexError, match="successor"):
            cloud.mark_update_superseded(older, newer)
        cloud.mark_update_managed_applied(newer)
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
        cloud.mark_update_managed_applied(last)
        cloud.mark_update_applied(last)
        cloud.mark_update_superseded(first, last)
        cloud.mark_update_superseded(middle, last)
        assert cloud.pending_updates("c_default") == []


def test_older_same_generation_upsert_cannot_resurrect_newer_delete(vector_db):
    with use_workspace("test"):
        spec = _spec()
        cloud.create_candidate(spec)
        older = cloud.enqueue_source_update("c_default", _URI, 42, "upsert")
        cloud.mark_update_managed_applied(older)
        newer = cloud.enqueue_source_update("c_default", _URI, 42, "delete")
        cloud.mark_update_managed_applied(newer)
        cloud.tombstone_source(spec, _URI, 42, update_id=newer)
        cloud.mark_update_applied(newer)
        with pytest.raises(cloud.CloudVectorIndexError, match="superseded"):
            cloud.replace_source(
                spec, _URI, 42, _HASH, [_chunk("stale", 0.1)], update_id=older
            )
        with pytest.raises(cloud.CloudVectorIndexError, match="superseded"):
            cloud.mark_update_applied(older)
        assert cloud.source_state(spec, _URI) == (42, True)


def test_newer_proven_upsert_can_reimport_same_generation_after_delete(vector_db):
    with use_workspace("test"):
        spec = _spec()
        cloud.create_candidate(spec)
        first = cloud.enqueue_source_update("c_default", _URI, 42, "upsert")
        cloud.mark_update_managed_applied(first)
        cloud.replace_source(
            spec, _URI, 42, _HASH, [_chunk("first", 0.1)], update_id=first
        )
        cloud.mark_update_applied(first)
        deleted = cloud.enqueue_source_update("c_default", _URI, 42, "delete")
        cloud.mark_update_managed_applied(deleted)
        cloud.tombstone_source(spec, _URI, 42, update_id=deleted)
        cloud.mark_update_applied(deleted)
        with pytest.raises(cloud.CloudVectorIndexError, match="generation changed"):
            cloud.replace_source(spec, _URI, 42, _HASH, [_chunk("unproven", 0.1)])
        restored = cloud.enqueue_source_update("c_default", _URI, 42, "upsert")
        cloud.mark_update_managed_applied(restored)
        cloud.replace_source(
            spec, _URI, 42, _HASH, [_chunk("restored", 0.1)], update_id=restored
        )
        cloud.mark_update_applied(restored)
        with pytest.raises(cloud.CloudVectorIndexError, match="superseded"):
            cloud.tombstone_source(spec, _URI, 42, update_id=deleted)
        assert cloud.source_state(spec, _URI) == (42, False)


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
        cloud.mark_update_managed_applied(update_id)
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


def test_unpublished_orphan_candidate_does_not_block_outbox(vector_db, monkeypatch):
    from rubik_core.control import db as control_db

    with use_workspace("test"):
        published = _spec()
        orphan = _spec(version="fedcba9876543210")
        cloud.create_candidate(published)
        cloud.create_candidate(orphan)
        cloud.replace_source(published, _URI, 42, _HASH, [_chunk("published", 0.1)])

        class Control:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def get(self, _model, _key):
                return SimpleNamespace(
                    config={
                        "corpora": {published.corpus: {"active": asdict(published)}}
                    }
                )

        monkeypatch.setattr(control_db, "control_session", lambda: Control())
        monkeypatch.setattr(settings, "APP_MODE", AppMode.GCP)
        update_id = cloud.enqueue_source_update("c_default", _URI, 42, "upsert")
        cloud.mark_update_managed_applied(update_id)
        cloud.mark_update_applied(update_id)
        assert cloud.pending_updates("c_default") == []


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
        with pytest.raises(cloud.CloudVectorIndexError, match="chunk source"):
            cloud.replace_source(
                spec,
                _URI,
                42,
                _HASH,
                [
                    _chunk(
                        "wrong-source",
                        0.1,
                        source_uri="gs://vector-test/workspaces/test/rag/other.pdf",
                    )
                ],
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
        assert ddl.startswith("CREATE INDEX CONCURRENTLY")
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
        exact = cloud._pg_query_sql(
            spec, " AND source_uri IN :allowed_source_uris", exact_subset=True
        )
        assert exact.startswith("WITH allowed AS MATERIALIZED")
        assert exact.index("source_uri IN :allowed_source_uris") < exact.index(
            ") SELECT"
        )
        assert "FROM allowed ORDER BY distance LIMIT :top_k" in exact


def test_postgres_ann_build_replaces_invalid_index_without_holding_orm_session(
    monkeypatch,
):
    spec = _spec()
    version = SimpleNamespace(
        provider=spec.provider,
        model=spec.model,
        dimension=spec.dimension,
        status="ready",
        verified_at=datetime.utcnow(),
        source_fingerprint="a" * 64,
        source_count=1,
        chunk_count=2,
        ann_ready=False,
    )
    state = {"open_sessions": 0, "index": (False, False), "commits": 0}
    ddl = []

    class Db:
        bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        def __enter__(self):
            state["open_sessions"] += 1
            return self

        def __exit__(self, *_):
            state["open_sessions"] -= 1

        def get(self, *_args, **_kwargs):
            return version

        def scalar(self, *_args):
            return 0

        def commit(self):
            state["commits"] += 1

    class Connection:
        def execute(self, statement):
            assert state["open_sessions"] == 0
            sql = str(statement)
            ddl.append(sql)
            if sql.startswith("DROP INDEX CONCURRENTLY"):
                state["index"] = None
            elif sql.startswith("CREATE INDEX CONCURRENTLY"):
                state["index"] = (True, True)

    @contextmanager
    def build_connection(_spec):
        assert state["open_sessions"] == 0
        yield Connection()

    monkeypatch.setattr(cloud, "SessionLocal", Db)
    monkeypatch.setattr(cloud, "_ann_build_connection", build_connection)
    monkeypatch.setattr(
        cloud, "_ann_catalog_state", lambda _connection, _name: state["index"]
    )
    with use_workspace("test"):
        cloud.build_ann_index(spec)
    assert [sql.split(" ", 3)[:3] for sql in ddl] == [
        ["DROP", "INDEX", "CONCURRENTLY"],
        ["CREATE", "INDEX", "CONCURRENTLY"],
    ]
    assert version.ann_ready is True
    assert state["commits"] == 1


@pytest.mark.parametrize("change", ["fingerprint", "pending", "invalid_index"])
def test_postgres_ann_build_refuses_changed_sources_or_invalid_catalog(
    monkeypatch, change
):
    spec = _spec()
    version = SimpleNamespace(
        provider=spec.provider,
        model=spec.model,
        dimension=spec.dimension,
        status="ready",
        verified_at=datetime.utcnow(),
        source_fingerprint="a" * 64,
        source_count=1,
        chunk_count=2,
        ann_ready=False,
    )
    state = {"pending": 0, "index": None}

    class Db:
        bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def get(self, *_args, **_kwargs):
            return version

        def scalar(self, *_args):
            return state["pending"]

        def commit(self):
            pytest.fail("changed candidate was marked ANN ready")

    class Connection:
        def execute(self, statement):
            assert str(statement).startswith("CREATE INDEX CONCURRENTLY")
            if change == "fingerprint":
                version.source_fingerprint = "b" * 64
            elif change == "pending":
                state["pending"] = 1
            state["index"] = (
                (False, False) if change == "invalid_index" else (True, True)
            )

    @contextmanager
    def build_connection(_spec):
        yield Connection()

    monkeypatch.setattr(cloud, "SessionLocal", Db)
    monkeypatch.setattr(cloud, "_ann_build_connection", build_connection)
    monkeypatch.setattr(
        cloud, "_ann_catalog_state", lambda _connection, _name: state["index"]
    )
    with use_workspace("test"):
        with pytest.raises(cloud.CloudVectorIndexError):
            cloud.build_ann_index(spec)
    assert version.ann_ready is False


def test_ann_build_connection_uses_autocommit_and_releases_tenant_lock(monkeypatch):
    spec = _spec()
    events = []

    class Connection:
        dialect = SimpleNamespace(name="postgresql")

        def __enter__(self):
            return self

        def __exit__(self, *_):
            events.append("close")

        def execution_options(self, **kwargs):
            events.append(kwargs)
            return self

        def execute(self, statement):
            events.append(str(statement))

        def scalar(self, statement, *_args):
            sql = str(statement)
            events.append(sql)
            return True

    class Engine:
        def connect(self):
            return Connection()

        def dispose(self):
            events.append("dispose")

    monkeypatch.setattr(cloud, "advisory_engine_for", lambda _: Engine())
    with use_workspace("test"):
        with cloud._ann_build_connection(spec):
            events.append("build")
    assert events == [
        {"isolation_level": "AUTOCOMMIT"},
        "SET statement_timeout = '5min'",
        "SET lock_timeout = '5s'",
        "SELECT pg_try_advisory_lock(:lock_id)",
        "build",
        "SELECT pg_advisory_unlock(:lock_id)",
        "close",
        "dispose",
    ]


@pytest.mark.parametrize("contended", [False, True])
def test_ann_build_connection_closes_on_contention_or_ddl_failure(
    monkeypatch, contended
):
    events = []

    class Connection:
        dialect = SimpleNamespace(name="postgresql")

        def __enter__(self):
            return self

        def __exit__(self, *_):
            events.append("close")

        def execution_options(self, **_kwargs):
            return self

        def execute(self, *_args):
            pass

        def scalar(self, statement, *_args):
            sql = str(statement)
            events.append(sql)
            return not contended

    class Engine:
        def connect(self):
            return Connection()

        def dispose(self):
            events.append("dispose")

    monkeypatch.setattr(cloud, "advisory_engine_for", lambda _: Engine())
    with use_workspace("test"):
        if contended:
            with pytest.raises(cloud.CloudVectorIndexError, match="already running"):
                with cloud._ann_build_connection(_spec()):
                    pytest.fail("contended build entered DDL")
            assert events == [
                "SELECT pg_try_advisory_lock(:lock_id)",
                "close",
                "dispose",
            ]
        else:
            with pytest.raises(RuntimeError, match="DDL failed"):
                with cloud._ann_build_connection(_spec()):
                    raise RuntimeError("DDL failed")
            assert events == [
                "SELECT pg_try_advisory_lock(:lock_id)",
                "SELECT pg_advisory_unlock(:lock_id)",
                "close",
                "dispose",
            ]


def test_production_refuses_sqlite_even_if_version_is_ready(vector_db, monkeypatch):
    with use_workspace("test"):
        spec = _spec()
        cloud.create_candidate(spec)
        cloud.verify_candidate(spec, {})
        monkeypatch.setattr(settings, "APP_MODE", AppMode.GCP)
        with pytest.raises(cloud.CloudVectorIndexError, match="requires PostgreSQL"):
            cloud.build_ann_index(spec)


def test_0008_to_0010_preserves_events_and_adds_managed_proof():
    versions = Path(__file__).resolve().parents[2] / "alembic" / "versions"
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        operations = Operations(
            MigrationContext.configure(conn, opts={"render_as_batch": True})
        )
        old = runpy.run_path(str(versions / "0008_cloud_vector_index.py"))
        old["upgrade"].__globals__["op"] = operations
        old["upgrade"]()
        conn.execute(
            text(
                "INSERT INTO cloud_vector_updates "
                "(id,corpus,source_key,source_uri,generation,operation,status,attempts,created_at) "
                "VALUES ('first','c_default','k','gs://x',42,'upsert','applied',0,'2026-01-01'),"
                "('second','c_default','k','gs://x',42,'delete','pending',0,'2026-01-02')"
            )
        )
        latest = runpy.run_path(str(versions / "0009_cloud_vector_events.py"))
        latest["upgrade"].__globals__["op"] = operations
        latest["upgrade"]()
        assert conn.execute(
            text("SELECT id,sequence FROM cloud_vector_updates ORDER BY sequence")
        ).all() == [("first", 1), ("second", 2)]
        proof = runpy.run_path(str(versions / "0010_cloud_vector_managed.py"))
        proof["upgrade"].__globals__["op"] = operations
        proof["upgrade"]()
        assert conn.execute(
            text(
                "SELECT id,sequence,managed_applied FROM cloud_vector_updates "
                "ORDER BY sequence"
            )
        ).all() == [("first", 1, 0), ("second", 2, 0)]
        conn.execute(
            text(
                "INSERT INTO cloud_vector_updates "
                "(id,sequence,corpus,source_key,source_uri,generation,operation,status,attempts,created_at) "
                "VALUES ('third',3,'c_default','k','gs://x',42,'upsert','pending',0,'2026-01-03')"
            )
        )
        assert (
            conn.execute(text("SELECT COUNT(*) FROM cloud_vector_updates")).scalar()
            == 3
        )

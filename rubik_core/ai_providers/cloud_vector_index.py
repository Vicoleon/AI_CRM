"""Versioned, tenant-DB vector storage for externally generated embeddings.

The control DB owns the active pointer. This module stores candidate and active
vectors in the selected workspace's own Cloud SQL database. Every source is a
validated GCS object and generation; a source update replaces its chunks in one
transaction. Callers must not activate a version until its exact source manifest
has been verified and its pgvector HNSW index has been built.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import bindparam, delete, func, select, text

from rubik_core.adapters.base import Chunk, RetrievedChunk
from rubik_core.ai_providers.embeddings import embedding_dimension
from rubik_core.config import AppMode, settings
from rubik_core.database.db import SessionLocal
from rubik_core.database.models import (
    CloudVectorChunk,
    CloudVectorSource,
    CloudVectorUpdate,
    CloudVectorVersion,
)
from rubik_core.tenancy.context import current_workspace
from rubik_core.tenancy.storage import assert_own_uri

_CORPUS = re.compile(r"[a-z][a-z0-9_]{0,39}\Z")
_VERSION = re.compile(r"[0-9a-f]{16}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_MAX_TOP_K = 50
_MAX_CHUNKS_PER_SOURCE = 10_000
_MAX_CHUNK_TEXT = 100_000
_BATCH = 64


class CloudVectorIndexError(RuntimeError):
    """A bounded, safe error for a missing or mismatched client vector index."""


@dataclass(frozen=True)
class CloudVectorSpec:
    workspace: str
    corpus: str
    provider: str
    model: str
    dimension: int
    version: str


class CloudIndexVector(list[float]):
    """A query vector pinned to the version that produced it."""

    def __init__(self, spec: CloudVectorSpec, values: list[float]):
        _check_spec(spec)
        _check_vector(values, spec.dimension)
        super().__init__(values)
        self.spec = spec


@dataclass(frozen=True)
class SourceUpdate:
    id: str
    corpus: str
    source_uri: str
    generation: int
    operation: str
    attempts: int
    status: str
    sequence: int
    created_at: datetime


@dataclass(frozen=True)
class IndexVerification:
    source_count: int
    chunk_count: int
    fingerprint: str


def _check_spec(spec: CloudVectorSpec) -> None:
    if not isinstance(spec, CloudVectorSpec) or spec.workspace != current_workspace():
        raise CloudVectorIndexError("Vector index belongs to another workspace")
    if (
        not isinstance(spec.corpus, str)
        or not _CORPUS.fullmatch(spec.corpus)
        or not isinstance(spec.version, str)
        or not _VERSION.fullmatch(spec.version)
    ):
        raise CloudVectorIndexError("Vector index identity is invalid")
    if spec.provider not in ("openai", "gemini"):
        raise CloudVectorIndexError("Client embedding provider is unsupported")
    try:
        expected = embedding_dimension(spec.provider, spec.model)
    except Exception:
        raise CloudVectorIndexError("Client embedding model is unsupported") from None
    if spec.dimension != expected:
        raise CloudVectorIndexError("Vector index dimension does not match model")


def _source(source_uri: str, generation: int) -> tuple[str, str]:
    if not isinstance(source_uri, str) or not source_uri.startswith("gs://"):
        raise CloudVectorIndexError("Vector source must be a GCS object")
    try:
        canonical = assert_own_uri(source_uri)
    except (ValueError, PermissionError):
        raise CloudVectorIndexError("Vector source is outside this workspace") from None
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 1
        or generation >= 2**63
    ):
        raise CloudVectorIndexError("Vector source generation is invalid")
    if len(canonical) > 2048:
        raise CloudVectorIndexError("Vector source URI is too long")
    return canonical, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _check_hash(value: str) -> None:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise CloudVectorIndexError("Vector source hash is invalid")


def _check_vector(values: list[float], dimension: int) -> None:
    if not isinstance(values, list) or len(values) != dimension:
        raise CloudVectorIndexError("Vector dimension does not match index")
    if not all(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        for value in values
    ):
        raise CloudVectorIndexError("Vector contains an invalid number")
    if not any(value != 0 for value in values):
        raise CloudVectorIndexError("Zero vector cannot be searched")


def _version(db, spec: CloudVectorSpec, *, lock: bool = False) -> CloudVectorVersion:
    row = db.get(
        CloudVectorVersion,
        (spec.corpus, spec.version),
        with_for_update=lock,
    )
    if (
        row is None
        or row.provider != spec.provider
        or row.model != spec.model
        or row.dimension != spec.dimension
        or row.status == "retired"
    ):
        raise CloudVectorIndexError("Vector index version is unavailable")
    return row


def create_candidate(spec: CloudVectorSpec) -> None:
    """Create a model-pinned candidate; existing versions cannot be repurposed."""
    _check_spec(spec)
    try:
        with SessionLocal() as db:
            row = db.get(CloudVectorVersion, (spec.corpus, spec.version))
            if row is not None:
                _version(db, spec)
                return
            db.add(
                CloudVectorVersion(
                    corpus=spec.corpus,
                    version=spec.version,
                    provider=spec.provider,
                    model=spec.model,
                    dimension=spec.dimension,
                    status="building",
                    ann_ready=False,
                    source_count=0,
                    chunk_count=0,
                )
            )
            db.commit()
    except CloudVectorIndexError:
        raise
    except Exception:
        raise CloudVectorIndexError("Vector index creation failed") from None


def replace_source(
    spec: CloudVectorSpec,
    source_uri: str,
    generation: int,
    content_sha256: str,
    chunks: list[Chunk],
) -> int:
    """Replace one source's vectors atomically, refusing stale generations."""
    _check_spec(spec)
    canonical, source_key = _source(source_uri, generation)
    _check_hash(content_sha256)
    if not isinstance(chunks, list) or len(chunks) > _MAX_CHUNKS_PER_SOURCE:
        raise CloudVectorIndexError("Vector source has too many chunks")
    ids: set[str] = set()
    mappings: list[dict[str, Any]] = []
    for chunk in chunks:
        if (
            not isinstance(chunk, Chunk)
            or not chunk.chunk_id
            or len(chunk.chunk_id) > 255
            or chunk.chunk_id in ids
            or not chunk.doc_id
            or len(chunk.doc_id) > 512
            or not chunk.text
            or len(chunk.text) > _MAX_CHUNK_TEXT
            or not isinstance(chunk.metadata, dict)
            or not isinstance(chunk.chunk_index, int)
            or chunk.chunk_index < 0
        ):
            raise CloudVectorIndexError("Vector source chunks are invalid")
        _check_vector(chunk.embedding, spec.dimension)
        try:
            encoded_metadata = json.dumps(chunk.metadata, sort_keys=True)
        except (TypeError, ValueError):
            raise CloudVectorIndexError("Vector source metadata is invalid") from None
        if len(encoded_metadata) > 8192:
            raise CloudVectorIndexError("Vector source metadata is too large")
        if chunk.metadata.get("source_path") != canonical:
            raise CloudVectorIndexError("Vector chunk source does not match object")
        ids.add(chunk.chunk_id)
        mappings.append(
            {
                "corpus": spec.corpus,
                "version": spec.version,
                "source_key": source_key,
                "chunk_id": chunk.chunk_id,
                "source_uri": canonical,
                "doc_id": chunk.doc_id,
                "chunk_index": chunk.chunk_index,
                "text": chunk.text,
                "metadata_json": chunk.metadata,
                "embedding": chunk.embedding,
            }
        )
    try:
        with SessionLocal() as db:
            version = _version(db, spec, lock=True)
            source = db.get(
                CloudVectorSource,
                (spec.corpus, spec.version, source_key),
                with_for_update=True,
            )
            if source is not None and source.generation > generation:
                raise CloudVectorIndexError("Stale vector source generation")
            if source is not None and source.generation == generation:
                if source.deleted or source.content_sha256 != content_sha256:
                    raise CloudVectorIndexError("Vector source generation changed")
                stored_count = db.scalar(
                    select(func.count())
                    .select_from(CloudVectorChunk)
                    .where(
                        CloudVectorChunk.corpus == spec.corpus,
                        CloudVectorChunk.version == spec.version,
                        CloudVectorChunk.source_key == source_key,
                    )
                )
                if stored_count == len(chunks) == source.chunk_count:
                    return len(chunks)
            db.execute(
                delete(CloudVectorChunk).where(
                    CloudVectorChunk.corpus == spec.corpus,
                    CloudVectorChunk.version == spec.version,
                    CloudVectorChunk.source_key == source_key,
                )
            )
            for offset in range(0, len(mappings), _BATCH):
                db.bulk_insert_mappings(
                    CloudVectorChunk, mappings[offset : offset + _BATCH]
                )
            if source is None:
                source = CloudVectorSource(
                    corpus=spec.corpus,
                    version=spec.version,
                    source_key=source_key,
                    source_uri=canonical,
                )
                db.add(source)
            source.generation = generation
            source.content_sha256 = content_sha256
            source.chunk_count = len(chunks)
            source.deleted = False
            source.updated_at = datetime.utcnow()
            if version.status != "active":
                version.status = "building"
                version.ann_ready = False
            version.verified_at = None
            version.source_fingerprint = None
            db.commit()
            return len(chunks)
    except CloudVectorIndexError:
        raise
    except Exception:
        raise CloudVectorIndexError("Vector source update failed") from None


def tombstone_source(spec: CloudVectorSpec, source_uri: str, generation: int) -> None:
    """Remove vectors immediately while retaining a no-resurrection marker."""
    _check_spec(spec)
    canonical, source_key = _source(source_uri, generation)
    try:
        with SessionLocal() as db:
            version = _version(db, spec, lock=True)
            source = db.get(
                CloudVectorSource,
                (spec.corpus, spec.version, source_key),
                with_for_update=True,
            )
            if source is not None and source.generation > generation:
                raise CloudVectorIndexError("Stale vector source generation")
            if source is None:
                source = CloudVectorSource(
                    corpus=spec.corpus,
                    version=spec.version,
                    source_key=source_key,
                    source_uri=canonical,
                )
            db.execute(
                delete(CloudVectorChunk).where(
                    CloudVectorChunk.corpus == spec.corpus,
                    CloudVectorChunk.version == spec.version,
                    CloudVectorChunk.source_key == source_key,
                )
            )
            source.generation = generation
            source.content_sha256 = "0" * 64
            source.chunk_count = 0
            source.deleted = True
            source.updated_at = datetime.utcnow()
            db.add(source)
            if version.status != "active":
                version.status = "building"
                version.ann_ready = False
            version.verified_at = None
            version.source_fingerprint = None
            db.commit()
    except CloudVectorIndexError:
        raise
    except Exception:
        raise CloudVectorIndexError("Vector source deletion failed") from None


def source_state(spec: CloudVectorSpec, source_uri: str) -> tuple[int, bool] | None:
    """Return the stored generation and tombstone state for a validated GCS URI."""
    _check_spec(spec)
    _, source_key = _source(source_uri, 1)
    try:
        with SessionLocal() as db:
            _version(db, spec)
            source = db.get(CloudVectorSource, (spec.corpus, spec.version, source_key))
            return (source.generation, source.deleted) if source else None
    except CloudVectorIndexError:
        raise
    except Exception:
        raise CloudVectorIndexError("Vector source state is unavailable") from None


def _next_update_sequence(db) -> int:
    if db.bind.dialect.name == "postgresql":
        return db.scalar(text("SELECT nextval('cloud_vector_updates_sequence_seq')"))
    return (db.scalar(select(func.max(CloudVectorUpdate.sequence))) or 0) + 1


def enqueue_source_update(
    corpus: str, source_uri: str, generation: int, operation: str
) -> str:
    """Persist each source mutation as a separately ordered durable event."""
    if not isinstance(corpus, str) or not _CORPUS.fullmatch(corpus):
        raise CloudVectorIndexError("Vector corpus is invalid")
    canonical, source_key = _source(source_uri, generation)
    if operation not in ("upsert", "delete"):
        raise CloudVectorIndexError("Vector source operation is invalid")
    try:
        with SessionLocal() as db:
            row = CloudVectorUpdate(
                id=str(uuid.uuid4()),
                corpus=corpus,
                source_key=source_key,
                source_uri=canonical,
                generation=generation,
                operation=operation,
                status="pending",
                attempts=0,
                sequence=_next_update_sequence(db),
            )
            db.add(row)
            db.commit()
            return row.id
    except CloudVectorIndexError:
        raise
    except Exception:
        raise CloudVectorIndexError(
            "Vector source update could not be queued"
        ) from None


def _update_view(row: CloudVectorUpdate) -> SourceUpdate:
    return SourceUpdate(
        id=row.id,
        corpus=row.corpus,
        source_uri=row.source_uri,
        generation=row.generation,
        operation=row.operation,
        attempts=row.attempts,
        status=row.status,
        sequence=row.sequence,
        created_at=row.created_at,
    )


def pending_updates(corpus: str, *, limit: int = 100) -> list[SourceUpdate]:
    if not isinstance(corpus, str) or not _CORPUS.fullmatch(corpus):
        raise CloudVectorIndexError("Vector corpus is invalid")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise CloudVectorIndexError("Vector update limit is invalid")
    try:
        with SessionLocal() as db:
            rows = db.scalars(
                select(CloudVectorUpdate)
                .where(
                    CloudVectorUpdate.corpus == corpus,
                    CloudVectorUpdate.status != "applied",
                )
                .order_by(CloudVectorUpdate.sequence)
                .limit(limit)
            ).all()
            return [_update_view(row) for row in rows]
    except Exception:
        raise CloudVectorIndexError("Vector source updates are unavailable") from None


def source_updates(
    corpus: str, source_uri: str, *, limit: int = 1000
) -> list[SourceUpdate]:
    """Return recent source history, including applied events, in durable order."""
    if not isinstance(corpus, str) or not _CORPUS.fullmatch(corpus):
        raise CloudVectorIndexError("Vector corpus is invalid")
    _, source_key = _source(source_uri, 1)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise CloudVectorIndexError("Vector update limit is invalid")
    try:
        with SessionLocal() as db:
            rows = db.scalars(
                select(CloudVectorUpdate)
                .where(
                    CloudVectorUpdate.corpus == corpus,
                    CloudVectorUpdate.source_key == source_key,
                )
                .order_by(CloudVectorUpdate.sequence.desc())
                .limit(limit)
            ).all()
            return [_update_view(row) for row in reversed(rows)]
    except Exception:
        raise CloudVectorIndexError("Vector source history is unavailable") from None


def _required_versions(db, corpus: str) -> list[str]:
    """Exclude tenant rows created before their control pointer was published."""
    if settings.APP_MODE != AppMode.GCP:
        return db.scalars(
            select(CloudVectorVersion.version).where(
                CloudVectorVersion.corpus == corpus,
                CloudVectorVersion.status != "retired",
            )
        ).all()

    from rubik_core.control.db import control_session
    from rubik_core.control.models import WorkspaceConnector

    with control_session() as control:
        row = control.get(WorkspaceConnector, (current_workspace(), "ai_embedding_indexes"))
        config = row.config if row is not None else None
    corpora = config.get("corpora") if isinstance(config, dict) else None
    item = corpora.get(corpus) if isinstance(corpora, dict) else None
    if item is None:
        return []
    if not isinstance(item, dict):
        raise CloudVectorIndexError("Published vector index is invalid")
    versions: list[str] = []
    for key in ("active", "candidate"):
        value = item.get(key)
        if value is None:
            continue
        data = value.get("spec") if key == "candidate" and isinstance(value, dict) else value
        if not isinstance(data, dict):
            raise CloudVectorIndexError("Published vector index is invalid")
        try:
            spec = CloudVectorSpec(**data)
        except TypeError:
            raise CloudVectorIndexError("Published vector index is invalid") from None
        _check_spec(spec)
        if spec.corpus != corpus:
            raise CloudVectorIndexError("Published vector index is invalid")
        versions.append(spec.version)
    return list(dict.fromkeys(versions))


def mark_update_applied(update_id: str) -> None:
    if not isinstance(update_id, str) or len(update_id) != 36:
        raise CloudVectorIndexError("Vector update ID is invalid")
    try:
        with SessionLocal() as db:
            row = db.get(CloudVectorUpdate, update_id, with_for_update=True)
            if row is None:
                raise CloudVectorIndexError("Vector update is unavailable")
            for version in _required_versions(db, row.corpus):
                source = db.get(
                    CloudVectorSource,
                    (row.corpus, version, row.source_key),
                )
                if (
                    source is None
                    or source.generation != row.generation
                    or source.deleted != (row.operation == "delete")
                ):
                    raise CloudVectorIndexError(
                        "Vector update has not reached published index versions"
                    )
            row.status = "applied"
            row.applied_at = datetime.utcnow()
            db.commit()
    except CloudVectorIndexError:
        raise
    except Exception:
        raise CloudVectorIndexError("Vector update could not be completed") from None


def mark_update_superseded(update_id: str, successor_id: str) -> None:
    """Clear an older event only after a later same-source event is applied."""
    if not all(isinstance(value, str) and len(value) == 36 for value in (update_id, successor_id)):
        raise CloudVectorIndexError("Vector update ID is invalid")
    try:
        with SessionLocal() as db:
            old = db.get(CloudVectorUpdate, update_id, with_for_update=True)
            successor = db.get(CloudVectorUpdate, successor_id, with_for_update=True)
            if (
                old is None
                or successor is None
                or old.corpus != successor.corpus
                or old.source_key != successor.source_key
                or successor.sequence <= old.sequence
                or successor.generation < old.generation
                or successor.status != "applied"
            ):
                raise CloudVectorIndexError("Vector update successor is invalid")
            old.status = "applied"
            old.applied_at = datetime.utcnow()
            db.commit()
    except CloudVectorIndexError:
        raise
    except Exception:
        raise CloudVectorIndexError("Vector update could not be superseded") from None


def mark_update_failed(update_id: str) -> None:
    """Keep the event pending for reconciliation without storing raw errors."""
    if not isinstance(update_id, str) or len(update_id) != 36:
        raise CloudVectorIndexError("Vector update ID is invalid")
    try:
        with SessionLocal() as db:
            row = db.get(CloudVectorUpdate, update_id, with_for_update=True)
            if row is None:
                raise CloudVectorIndexError("Vector update is unavailable")
            row.status = "failed"
            row.attempts += 1
            db.commit()
    except CloudVectorIndexError:
        raise
    except Exception:
        raise CloudVectorIndexError(
            "Vector update failure could not be recorded"
        ) from None


def _pending_count(db, corpus: str) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(CloudVectorUpdate)
            .where(
                CloudVectorUpdate.corpus == corpus,
                CloudVectorUpdate.status != "applied",
            )
        )
        or 0
    )


def verify_candidate(
    spec: CloudVectorSpec, expected_sources: dict[str, tuple[int, str]]
) -> IndexVerification:
    """Check exact GCS source generations, hashes, chunks and pending writes."""
    _check_spec(spec)
    if not isinstance(expected_sources, dict):
        raise CloudVectorIndexError("Expected source manifest is invalid")
    expected: dict[str, tuple[int, str]] = {}
    for uri, item in expected_sources.items():
        if not isinstance(item, tuple) or len(item) != 2:
            raise CloudVectorIndexError("Expected source manifest is invalid")
        generation, content_sha256 = item
        canonical, _ = _source(uri, generation)
        _check_hash(content_sha256)
        expected[canonical] = (generation, content_sha256)
    try:
        with SessionLocal() as db:
            version = _version(db, spec, lock=True)
            if version.status == "active":
                raise CloudVectorIndexError("Active vector index cannot be rebuilt")
            if _pending_count(db, spec.corpus):
                raise CloudVectorIndexError("Vector source updates are still pending")
            sources = db.scalars(
                select(CloudVectorSource).where(
                    CloudVectorSource.corpus == spec.corpus,
                    CloudVectorSource.version == spec.version,
                    CloudVectorSource.deleted.is_(False),
                )
            ).all()
            if {row.source_uri for row in sources} != set(expected):
                raise CloudVectorIndexError("Vector source manifest does not match")
            actual_counts: dict[str, int] = {row.source_key: 0 for row in sources}
            for chunk in db.scalars(
                select(CloudVectorChunk).where(
                    CloudVectorChunk.corpus == spec.corpus,
                    CloudVectorChunk.version == spec.version,
                )
            ).yield_per(256):
                if chunk.source_key not in actual_counts:
                    raise CloudVectorIndexError("Vector index contains orphan chunks")
                _check_vector(chunk.embedding, spec.dimension)
                actual_counts[chunk.source_key] += 1
            manifest: list[tuple[str, int, str, int]] = []
            for row in sources:
                generation, digest = expected[row.source_uri]
                if (
                    row.generation != generation
                    or row.content_sha256 != digest
                    or row.chunk_count != actual_counts[row.source_key]
                ):
                    raise CloudVectorIndexError("Vector source manifest does not match")
                manifest.append(
                    (
                        row.source_uri,
                        row.generation,
                        row.content_sha256,
                        row.chunk_count,
                    )
                )
            manifest.sort()
            fingerprint = hashlib.sha256(
                json.dumps(manifest, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            version.source_count = len(sources)
            version.chunk_count = sum(actual_counts.values())
            version.source_fingerprint = fingerprint
            version.verified_at = datetime.utcnow()
            version.status = "ready"
            version.ann_ready = False
            db.commit()
            return IndexVerification(len(sources), version.chunk_count, fingerprint)
    except CloudVectorIndexError:
        raise
    except Exception:
        raise CloudVectorIndexError("Vector index verification failed") from None


def build_ann_index(spec: CloudVectorSpec) -> None:
    """Build a partial HNSW index for this exact version before cutover."""
    _check_spec(spec)
    try:
        with SessionLocal() as db:
            version = _version(db, spec, lock=True)
            if version.status not in ("ready", "active") or not version.verified_at:
                raise CloudVectorIndexError("Vector index has not been verified")
            if _pending_count(db, spec.corpus):
                raise CloudVectorIndexError("Vector source updates are still pending")
            if db.bind.dialect.name == "postgresql":
                # corpus/version/dimension passed through fixed regex/catalog
                # checks above; no provider text or request data enters DDL.
                db.execute(text("SET LOCAL statement_timeout = '5min'"))
                db.execute(text(_ann_index_sql(spec)))
            elif settings.APP_MODE == AppMode.GCP:
                raise CloudVectorIndexError("Cloud vector storage requires PostgreSQL")
            version.ann_ready = True
            db.commit()
    except CloudVectorIndexError:
        raise
    except Exception:
        raise CloudVectorIndexError("Vector search index build failed") from None


def _purge_version(db, version: CloudVectorVersion) -> None:
    db.execute(
        delete(CloudVectorChunk).where(
            CloudVectorChunk.corpus == version.corpus,
            CloudVectorChunk.version == version.version,
        )
    )
    db.execute(
        delete(CloudVectorSource).where(
            CloudVectorSource.corpus == version.corpus,
            CloudVectorSource.version == version.version,
        )
    )
    version.status = "retired"
    version.ann_ready = False
    version.source_count = 0
    version.chunk_count = 0
    version.source_fingerprint = None
    version.verified_at = None


def mark_active(spec: CloudVectorSpec) -> None:
    """Mark a ready version usable after the control pointer has switched."""
    _check_spec(spec)
    try:
        with SessionLocal() as db:
            version = _version(db, spec, lock=True)
            if version.status not in ("ready", "active") or not version.ann_ready:
                raise CloudVectorIndexError("Vector index is not ready for activation")
            if _pending_count(db, spec.corpus):
                raise CloudVectorIndexError("Vector source updates are still pending")
            if version.status == "ready":
                superseded = db.scalars(
                    select(CloudVectorVersion)
                    .where(
                        CloudVectorVersion.corpus == spec.corpus,
                        CloudVectorVersion.version != spec.version,
                        CloudVectorVersion.status == "active",
                    )
                    .with_for_update()
                ).all()
                for old in superseded:
                    _purge_version(db, old)
                version.status = "active"
            db.commit()
    except CloudVectorIndexError:
        raise
    except Exception:
        raise CloudVectorIndexError("Vector index activation failed") from None


def retire_candidate(spec: CloudVectorSpec) -> None:
    """Stop requiring writes to an abandoned, never-active candidate."""
    _check_spec(spec)
    try:
        with SessionLocal() as db:
            version = _version(db, spec, lock=True)
            if version.status == "active":
                raise CloudVectorIndexError("Active vector index cannot be retired")
            _purge_version(db, version)
            db.commit()
    except CloudVectorIndexError:
        raise
    except Exception:
        raise CloudVectorIndexError("Vector candidate retirement failed") from None


def _filter_clause(filters: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
    if filters is None:
        return "", {}
    if not isinstance(filters, dict) or len(filters) > 12:
        raise CloudVectorIndexError("Vector query filters are invalid")
    clauses: list[str] = []
    params: dict[str, Any] = {}
    metadata = {}
    for key, value in filters.items():
        if not isinstance(key, str) or not re.fullmatch(
            r"[a-zA-Z][a-zA-Z0-9_]{0,63}", key
        ):
            raise CloudVectorIndexError("Vector query filters are invalid")
        if key in ("doc_id", "source_uri"):
            if not isinstance(value, str) or not value:
                raise CloudVectorIndexError("Vector query filters are invalid")
            clauses.append(f" AND {key} = :filter_{key}")
            params[f"filter_{key}"] = value
        else:
            if not isinstance(value, (str, int, float, bool)) or (
                isinstance(value, float) and not math.isfinite(value)
            ):
                raise CloudVectorIndexError("Vector query filters are invalid")
            metadata[key] = value
    if metadata:
        encoded = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
        if len(encoded) > 4096:
            raise CloudVectorIndexError("Vector query filters are too large")
        clauses.append(" AND metadata::jsonb @> CAST(:filter_metadata AS jsonb)")
        params["filter_metadata"] = encoded
    return "".join(clauses), params


def _ann_index_sql(spec: CloudVectorSpec) -> str:
    _check_spec(spec)
    index_name = (
        "ix_cloud_vector_ann_"
        + hashlib.sha256(f"{spec.corpus}:{spec.version}".encode()).hexdigest()[:20]
    )
    return (
        f"CREATE INDEX IF NOT EXISTS {index_name} "
        "ON cloud_vector_chunks USING hnsw "
        f"((embedding::vector({spec.dimension})) vector_cosine_ops) "
        f"WHERE corpus = '{spec.corpus}' AND version = '{spec.version}'"
    )


def _pg_query_sql(
    spec: CloudVectorSpec, clause: str, *, exact_subset: bool = False
) -> str:
    _check_spec(spec)
    if exact_subset:
        # Materialization forces the ACL subset to be selected before distance
        # ranking. An HNSW scan followed by source_uri filtering can lose all
        # of a private owner's matches when other owners dominate candidates.
        return (
            "WITH allowed AS MATERIALIZED ("
            "SELECT chunk_id, doc_id, chunk_index, text, metadata, embedding "
            "FROM cloud_vector_chunks "
            f"WHERE corpus = '{spec.corpus}' AND version = '{spec.version}'"
            + clause
            + ") SELECT chunk_id, doc_id, chunk_index, text, metadata, "
            f"embedding::vector({spec.dimension}) <=> "
            f"CAST(:query_vector AS vector({spec.dimension})) AS distance "
            "FROM allowed ORDER BY distance LIMIT :top_k"
        )
    return (
        "SELECT chunk_id, doc_id, chunk_index, text, metadata, "
        f"embedding::vector({spec.dimension}) <=> "
        f"CAST(:query_vector AS vector({spec.dimension})) AS distance "
        "FROM cloud_vector_chunks "
        f"WHERE corpus = '{spec.corpus}' AND version = '{spec.version}'"
        + clause
        + " ORDER BY distance LIMIT :top_k"
    )


def _sqlite_distance(left: list[float], right: list[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if not left_norm or not right_norm:
        raise CloudVectorIndexError("Stored vector is invalid")
    return 1 - numerator / (left_norm * right_norm)


def query_chunks(
    spec: CloudVectorSpec,
    vector: CloudIndexVector,
    *,
    top_k: int = 5,
    filters: dict[str, Any] | None = None,
    allowed_source_uris: list[str] | None = None,
) -> list[RetrievedChunk]:
    """Query only the pinned version; no managed-index fallback is possible."""
    _check_spec(spec)
    if not isinstance(vector, CloudIndexVector) or vector.spec != spec:
        raise CloudVectorIndexError("Query vector does not match index version")
    _check_vector(vector, spec.dimension)
    if (
        not isinstance(top_k, int)
        or isinstance(top_k, bool)
        or not 1 <= top_k <= _MAX_TOP_K
    ):
        raise CloudVectorIndexError("Vector result limit is invalid")
    clause, extra = _filter_clause(filters)
    allowed: list[str] | None = None
    if allowed_source_uris is not None:
        if not isinstance(allowed_source_uris, list) or len(allowed_source_uris) > 100:
            raise CloudVectorIndexError("Vector source allowlist is invalid")
        allowed = list(dict.fromkeys(_source(uri, 1)[0] for uri in allowed_source_uris))
        clause += " AND source_uri IN :allowed_source_uris"
        extra["allowed_source_uris"] = allowed
    try:
        with SessionLocal() as db:
            version = _version(db, spec)
            if version.status not in ("ready", "active") or not version.ann_ready:
                raise CloudVectorIndexError("Vector index is not ready")
            if _pending_count(db, spec.corpus):
                raise CloudVectorIndexError("Vector source updates are still pending")
            if allowed == []:
                return []
            if db.bind.dialect.name == "postgresql":
                db.execute(text("SET LOCAL statement_timeout = '5s'"))
                query = text(_pg_query_sql(spec, clause, exact_subset=allowed is not None))
                if allowed is not None:
                    query = query.bindparams(
                        bindparam("allowed_source_uris", expanding=True)
                    )
                rows = (
                    db.execute(
                        query,
                        {
                            "query_vector": json.dumps(list(vector)),
                            "top_k": top_k,
                            **extra,
                        },
                    )
                    .mappings()
                    .all()
                )
                results = []
                for row in rows:
                    metadata = row["metadata"]
                    if isinstance(metadata, str):
                        metadata = json.loads(metadata)
                    results.append(
                        RetrievedChunk(
                            chunk=Chunk(
                                chunk_id=row["chunk_id"],
                                doc_id=row["doc_id"],
                                chunk_index=row["chunk_index"],
                                text=row["text"],
                                metadata=metadata,
                            ),
                            score=float(row["distance"]),
                        )
                    )
                return results

            if settings.APP_MODE == AppMode.GCP:
                raise CloudVectorIndexError("Cloud vector storage requires PostgreSQL")

            # SQLite is used only by isolated local contract tests; production
            # Cloud SQL always takes the indexed pgvector branch above.
            statement = select(CloudVectorChunk).where(
                CloudVectorChunk.corpus == spec.corpus,
                CloudVectorChunk.version == spec.version,
            )
            if allowed is not None:
                statement = statement.where(CloudVectorChunk.source_uri.in_(allowed))
            rows = db.scalars(statement).all()
            results = []
            for row in rows:
                data = {
                    "doc_id": row.doc_id,
                    "source_uri": row.source_uri,
                    **row.metadata_json,
                }
                if filters and any(
                    data.get(key) != value for key, value in filters.items()
                ):
                    continue
                _check_vector(row.embedding, spec.dimension)
                results.append(
                    RetrievedChunk(
                        chunk=Chunk(
                            chunk_id=row.chunk_id,
                            doc_id=row.doc_id,
                            chunk_index=row.chunk_index,
                            text=row.text,
                            metadata=row.metadata_json,
                        ),
                        score=_sqlite_distance(vector, row.embedding),
                    )
                )
            return sorted(results, key=lambda item: item.score)[:top_k]
    except CloudVectorIndexError:
        raise
    except Exception:
        raise CloudVectorIndexError("Vector search failed") from None

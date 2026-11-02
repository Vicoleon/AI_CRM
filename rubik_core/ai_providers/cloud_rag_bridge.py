"""Route bound Cloud RAG corpora through a workspace's selected vector index.

The control pointer chooses a model/version for reads. A pending source event
blocks reads until every serving version has received the same source change.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from google.api_core.exceptions import NotFound
from google.cloud import storage
from sqlalchemy import select, text

from rubik_core.adapters.base import RetrievedChunk
from rubik_core.ai_providers.cloud_sources import load_gcs_source
from rubik_core.ai_providers.cloud_vector_index import (
    CloudIndexVector,
    CloudVectorIndexError,
    enqueue_source_update,
    mark_active,
    mark_update_applied,
    mark_update_failed,
    mark_update_managed_applied,
    mark_update_superseded,
    pending_updates,
    query_chunks,
    replace_source,
    source_updates,
    tombstone_source,
)
from rubik_core.ai_providers.embedding_index import (
    active_cloud_index,
    cloud_corpus_key,
    cloud_index_versions,
)
from rubik_core.ai_providers.embeddings import embed_with_route
from rubik_core.ai_providers.registry import (
    IncompatibleModelRoute,
    provider_connection_route,
)
from rubik_core.config import AppMode, settings
from rubik_core.cost.usage import feature_context
from rubik_core.tenancy.context import current_workspace
from rubik_core.tenancy.registry import advisory_engine_for
from rubik_core.tenancy.storage import assert_own_uri

_EMBED_BATCH = 32
_AUTO_REPAIR_MAX_BYTES = 256 * 1024
_AUTO_REPAIR_MAX_CHUNKS = 32
_local_repair_locks: dict[str, threading.Lock] = {}
_local_repair_guard = threading.Lock()


class CloudSourceNotFound(CloudVectorIndexError):
    """A validated GCS source is absent, distinct from a transient read error."""


@contextmanager
def _automatic_repair_lock(corpus: str) -> Iterator[None]:
    """Prevent concurrent requests from replaying and billing one source."""
    key = f"{current_workspace()}:{corpus}"
    if settings.APP_MODE == AppMode.GCP:
        lock_id = int.from_bytes(
            hashlib.sha256(key.encode()).digest()[:8], "big", signed=True
        )
        try:
            engine = advisory_engine_for(current_workspace())
        except Exception:
            raise CloudVectorIndexError("Vector repair lock is unavailable") from None
        try:
            with engine.connect() as connection:
                if connection.dialect.name != "postgresql":
                    raise CloudVectorIndexError(
                        "Cloud vector repair requires PostgreSQL"
                    )
                transaction = connection.begin()
                try:
                    try:
                        acquired = connection.scalar(
                            text("SELECT pg_try_advisory_xact_lock(:lock_id)"),
                            {"lock_id": lock_id},
                        )
                    except Exception:
                        raise CloudVectorIndexError(
                            "Vector repair lock is unavailable"
                        ) from None
                    if not acquired:
                        raise CloudVectorIndexError("Vector repair is already running")
                    yield
                finally:
                    transaction.rollback()
        finally:
            engine.dispose()
    else:
        with _local_repair_guard:
            lock = _local_repair_locks.setdefault(key, threading.Lock())
        if not lock.acquire(blocking=False):
            raise CloudVectorIndexError("Vector repair is already running")
        try:
            yield
        finally:
            lock.release()


@contextmanager
def cloud_source_mutation_lock(bound_resource: str, uri: str) -> Iterator[None]:
    """Serialize one workspace GCS source across all bound RAG corpora."""
    assert_own_uri(uri)
    if settings.APP_MODE != AppMode.GCP and not any(
        cloud_index_versions(bound_resource)
    ):
        yield
        return
    cloud_corpus_key(bound_resource)
    with _automatic_repair_lock(f"source:{uri}"):
        yield


def _source_size(uri: str) -> int:
    assert_own_uri(uri)
    bucket, object_name = uri[5:].split("/", 1)
    try:
        blob = (
            storage.Client(project=settings.GCP_PROJECT_ID)
            .bucket(bucket)
            .blob(object_name)
        )
        blob.reload()
        return int(blob.size)
    except Exception:
        raise CloudVectorIndexError("Vector source size is unavailable") from None


def _bound_source_references(bound_resource: str) -> list[str]:
    """Resolve each current ragFile's source in the exact bound corpus."""
    from rubik_core.tenancy.corpora import (
        assert_own_rag_file,
        corpus_id,
        require_bound_corpus_id,
    )

    require_bound_corpus_id(bound_resource)
    try:
        private = corpus_id("private_uploads", create=False)
        if private == bound_resource:
            from rubik_core.chat_files.private_index import (
                _sources_for_filed_names,
                _verified_ragfiles,
            )

            mapped = _verified_ragfiles(bound_resource)
            owners = _sources_for_filed_names(list(mapped))
            for name, uri in mapped.items():
                if owners[name] != uri:
                    raise CloudVectorIndexError(
                        "Private vector source has no owner record"
                    )
            return list(mapped.values())

        from vertexai.preview import rag

        from rubik_core.adapters.vertex_rag import _init_vertexai

        _init_vertexai()
        uris: list[str] = []
        for item in rag.list_files(corpus_name=bound_resource):
            assert_own_rag_file(item.name, bound_resource)
            sources = list(getattr(getattr(item, "gcs_source", None), "uris", []) or [])
            if not sources:
                raise CloudVectorIndexError("Bound RAG file has no GCS source")
            for uri in dict.fromkeys(sources):
                assert_own_uri(uri)
                uris.append(uri)
        return uris
    except CloudVectorIndexError:
        raise
    except Exception:
        raise CloudVectorIndexError("Bound vector sources are unavailable") from None


def _bound_source_membership(bound_resource: str) -> set[str]:
    return set(_bound_source_references(bound_resource))


def _deletion_source_references(bound_resource: str) -> list[str]:
    """Count exact private ragFiles even when an orphan has no readable owner.

    Private orphan cleanup must remove a Vertex file whose ChatUpload row was
    cancelled or never committed. Read and replay paths still require the
    stricter owner-verified `_bound_source_references` mapping.
    """
    from rubik_core.tenancy.corpora import corpus_id, require_bound_corpus_id

    require_bound_corpus_id(bound_resource)
    if corpus_id("private_uploads", create=False) == bound_resource:
        from rubik_core.chat_files.private_index import _verified_ragfiles

        return list(_verified_ragfiles(bound_resource).values())
    return _bound_source_references(bound_resource)


def source_referencing_corpora(bound_resource: str, uri: str) -> set[str]:
    """Find every exact workspace-bound corpus still claiming a GCS source."""
    assert_own_uri(uri)
    from rubik_core.control.db import control_session
    from rubik_core.control.models import Workspace, WorkspaceCorpus
    from rubik_core.tenancy.corpora import require_bound_corpus_id

    require_bound_corpus_id(bound_resource)
    slug = current_workspace()
    with control_session() as db:
        workspace = db.get(Workspace, slug)
        if workspace is None or workspace.status != "active":
            raise CloudVectorIndexError("Active workspace is required")
        bound = {
            value
            for value in (workspace.corpus_ids or {}).values()
            if isinstance(value, str) and value
        }
        bound.update(
            value
            for value in db.scalars(
                select(WorkspaceCorpus.corpus_id).where(
                    WorkspaceCorpus.workspace_slug == slug
                )
            ).all()
            if isinstance(value, str) and value
        )
    if bound_resource not in bound:
        raise CloudVectorIndexError("Knowledge corpus is not bound to workspace")
    return {
        resource for resource in bound if uri in _deletion_source_references(resource)
    }


def source_is_published(bound_resource: str, uri: str) -> bool:
    """Protect GCS bytes while any workspace-bound corpus references them."""
    return bool(source_referencing_corpora(bound_resource, uri))


@dataclass(frozen=True)
class CloudSourceUpdate:
    id: str
    generation: int


def _repair_private_orphans(bound_resource: str) -> None:
    from rubik_core.tenancy.corpora import corpus_id

    if corpus_id("private_uploads", create=False) == bound_resource:
        from rubik_core.chat_files.private_index import repair_orphan_ragfiles

        repair_orphan_ragfiles(bound_resource, max_repairs=1)


def query_cloud_resource(
    bound_resource: str,
    query_text: str,
    *,
    top_k: int = 5,
    distance_threshold: float | None = None,
    allowed_source_uris: list[str] | None = None,
) -> list[RetrievedChunk] | None:
    """Return indexed hits, or None only while the managed route is selected."""
    spec = active_cloud_index(bound_resource)
    if spec is None:
        return None
    pending = pending_updates(spec.corpus, limit=2)
    if pending:
        _repair_private_orphans(bound_resource)
        pending = pending_updates(spec.corpus, limit=2)
    if pending:
        if len(pending) > 1:
            raise CloudVectorIndexError("Vector source backlog needs repair")
        latest = source_updates(spec.corpus, pending[0].source_uri, limit=1)
        if not latest or not latest[-1].managed_applied:
            raise CloudVectorIndexError("Vector source outcome needs repair")
        if latest[-1].status == "pending":
            raise CloudVectorIndexError("Vector source update is still running")
        if (
            latest[-1].operation == "upsert"
            and _source_size(latest[-1].source_uri) > _AUTO_REPAIR_MAX_BYTES
        ):
            raise CloudVectorIndexError("Vector source needs background repair")
        with _automatic_repair_lock(spec.corpus):
            reconcile_pending(bound_resource, automatic=True, max_batches=1)
    mark_active(spec)
    route = provider_connection_route(
        spec.provider,
        spec.model,
        feature="embedding_index",
        capability="embedding",
    )
    with feature_context("embedding_index_query"):
        values = embed_with_route(route, [query_text], spec.dimension)[0]
    if active_cloud_index(bound_resource) != spec:
        raise IncompatibleModelRoute("Embedding index changed; retry this request")
    hits = query_chunks(
        spec,
        CloudIndexVector(spec, values),
        top_k=top_k,
        allowed_source_uris=allowed_source_uris,
    )
    try:
        published = _bound_source_membership(bound_resource)
    except CloudVectorIndexError:
        _repair_private_orphans(bound_resource)
        published = _bound_source_membership(bound_resource)
    for hit in hits:
        uri = hit.chunk.metadata.get("source_path")
        try:
            assert_own_uri(uri)
        except (ValueError, PermissionError, TypeError):
            raise CloudVectorIndexError(
                "Vector hit source is outside this workspace"
            ) from None
        if uri not in published:
            raise CloudVectorIndexError(
                "Vector hit is no longer published in its bound corpus"
            )
    if active_cloud_index(bound_resource) != spec:
        raise IncompatibleModelRoute("Embedding index changed; retry this request")
    if distance_threshold is not None:
        hits = [hit for hit in hits if hit.score <= distance_threshold]
    return hits


def _source_generation(uri: str, client: storage.Client | None) -> int:
    assert_own_uri(uri)
    bucket_name, object_name = uri[5:].split("/", 1)
    try:
        gcs = client or storage.Client(project=settings.GCP_PROJECT_ID)
        blob = gcs.bucket(bucket_name).blob(object_name)
        blob.reload()
        generation = int(blob.generation)
        if generation < 1:
            raise ValueError
        return generation
    except NotFound:
        raise CloudSourceNotFound("Vector source is unavailable") from None
    except Exception:
        raise CloudVectorIndexError("Vector source generation is unavailable") from None


def begin_cloud_source_update(
    bound_resource: str,
    uri: str,
    operation: str,
    *,
    client: storage.Client | None = None,
) -> CloudSourceUpdate | None:
    """Queue an update before mutating the managed corpus.

    A candidate becomes visible in control before its backfill starts. Once
    visible, all writes are included in the outbox and candidate index.
    """
    active, candidate = cloud_index_versions(bound_resource)
    if active is None and candidate is None:
        return None
    assert_own_uri(uri)
    corpus = cloud_corpus_key(bound_resource)
    if active is not None and not pending_updates(corpus, limit=1):
        mark_active(active)
    if (
        operation == "delete"
        and _deletion_source_references(bound_resource).count(uri) > 1
    ):
        return None
    try:
        generation = _source_generation(uri, client)
    except CloudSourceNotFound:
        if operation != "delete":
            raise
        from rubik_core.ai_providers.cloud_vector_index import source_state

        states = [source_state(spec, uri) for spec in (active, candidate) if spec]
        if not states or any(state is None for state in states):
            raise CloudVectorIndexError(
                "Vector source is unavailable for deletion"
            ) from None
        if all(state[1] for state in states):
            return None
        generation = max(state[0] for state in states)
    update_id = enqueue_source_update(corpus, uri, generation, operation)
    return CloudSourceUpdate(update_id, generation)


def finish_cloud_source_update(
    bound_resource: str,
    uri: str,
    operation: str,
    update: CloudSourceUpdate | None,
    *,
    client: storage.Client | None = None,
) -> None:
    """Apply a queued source change to active and candidate versions."""
    if update is None:
        return
    active, candidate = cloud_index_versions(bound_resource)
    versions = tuple(dict.fromkeys(spec for spec in (active, candidate) if spec))
    try:
        if operation == "upsert":
            source = load_gcs_source(uri, client=client)
            if source.generation != update.generation:
                raise CloudVectorIndexError("Vector source changed during indexing")
            with feature_context("embedding_index_sync"):
                for spec in versions:
                    route = provider_connection_route(
                        spec.provider,
                        spec.model,
                        feature="embedding_index",
                        capability="embedding",
                    )
                    chunks = [chunk.model_copy(deep=True) for chunk in source.chunks]
                    for offset in range(0, len(chunks), _EMBED_BATCH):
                        batch = chunks[offset : offset + _EMBED_BATCH]
                        vectors = embed_with_route(
                            route, [chunk.text for chunk in batch], spec.dimension
                        )
                        for chunk, vector in zip(batch, vectors):
                            chunk.embedding = vector
                    replace_source(
                        spec,
                        uri,
                        source.generation,
                        source.sha256,
                        chunks,
                        update_id=update.id,
                    )
        elif operation == "delete":
            for spec in versions:
                tombstone_source(spec, uri, update.generation, update_id=update.id)
        else:
            raise CloudVectorIndexError("Vector source operation is invalid")
        mark_update_applied(update.id)
    except Exception:
        mark_update_failed(update.id)
        raise
    _clear_older_source_updates(bound_resource, uri, update.id)


def fail_cloud_source_update(update: CloudSourceUpdate | None) -> None:
    """Retain a failed event so active reads and cutover fail closed."""
    if update is not None:
        mark_update_failed(update.id)


def confirm_managed_source_update(update: CloudSourceUpdate | None) -> None:
    """Durably record that the exact bound Vertex mutation returned success."""
    if update is not None:
        mark_update_managed_applied(update.id)


def settle_failed_managed_update(
    bound_resource: str,
    uri: str,
    operation: str,
    update: CloudSourceUpdate | None,
    *,
    client: storage.Client | None = None,
) -> bool:
    """Resolve a failed Vertex mutation only from exact published membership.

    Returns whether the intended operation actually took effect. A definitive
    opposite state gets a newer inverse outbox event, so the failed event can
    be superseded without leaving the whole corpus unreadable. Any ambiguous
    state remains pending for repair.
    """
    if update is None:
        return False
    try:
        published = _bound_source_membership(bound_resource)
        if operation not in ("upsert", "delete"):
            raise CloudVectorIndexError("Vector source operation is invalid")
        intended_present = operation == "upsert"
        actual_present = uri in published
        if actual_present == intended_present:
            if operation == "upsert":
                raise CloudVectorIndexError("Vector source import outcome is ambiguous")
            confirm_managed_source_update(update)
            finish_cloud_source_update(
                bound_resource, uri, operation, update, client=client
            )
            return True
        inverse = "delete" if intended_present else "upsert"
        successor_id = enqueue_source_update(
            cloud_corpus_key(bound_resource), uri, update.generation, inverse
        )
        mark_update_managed_applied(successor_id)
        finish_cloud_source_update(
            bound_resource,
            uri,
            inverse,
            CloudSourceUpdate(successor_id, update.generation),
            client=client,
        )
        return False
    except Exception:
        fail_cloud_source_update(update)
        raise


def _clear_older_source_updates(
    bound_resource: str, uri: str, successor_id: str
) -> None:
    """Clear old failed attempts only after a newer applied event wins."""
    corpus = cloud_corpus_key(bound_resource)
    history = source_updates(corpus, uri)
    successor = next((item for item in history if item.id == successor_id), None)
    if successor is None or successor.status != "applied":
        raise CloudVectorIndexError("Vector update history is incomplete")
    for item in pending_updates(corpus, limit=1000):
        if item.source_uri == uri and item.sequence < successor.sequence:
            mark_update_superseded(item.id, successor.id)


def _reconcile_source(
    bound_resource: str, corpus: str, uri: str, pending: list, automatic: bool
) -> None:
    """Replay one source after obtaining its cross-process mutation lock."""
    history = source_updates(corpus, uri, limit=1)
    if not history or history[-1].sequence < max(item.sequence for item in pending):
        raise CloudVectorIndexError("Vector update history is incomplete")
    newest = history[-1]
    if not newest.managed_applied:
        raise CloudVectorIndexError("Vector source outcome needs repair")
    if automatic and newest.status == "pending":
        raise CloudVectorIndexError("Vector source update is still running")
    published = _bound_source_membership(bound_resource)
    if newest.operation == "upsert":
        if uri not in published:
            raise CloudVectorIndexError(
                "Vector source is not published in its bound corpus"
            )
        source = load_gcs_source(uri)
        if source.generation != newest.generation:
            raise CloudVectorIndexError(
                "Vector source changed while an update was pending"
            )
        if automatic and (
            len(source.chunks) > _AUTO_REPAIR_MAX_CHUNKS
            or sum(len(chunk.text) for chunk in source.chunks) > 100_000
        ):
            raise CloudVectorIndexError("Vector source needs background repair")
    else:
        if uri in published:
            raise CloudVectorIndexError(
                "Vector source is still published in its bound corpus"
            )
        try:
            current_generation = _source_generation(uri, None)
        except CloudSourceNotFound:
            current_generation = None
        if current_generation is not None and current_generation > newest.generation:
            raise CloudVectorIndexError(
                "Vector source changed while a deletion was pending"
            )
    finish_cloud_source_update(
        bound_resource,
        uri,
        newest.operation,
        CloudSourceUpdate(newest.id, newest.generation),
    )


def reconcile_pending(
    bound_resource: str,
    *,
    automatic: bool = False,
    max_batches: int = 100,
) -> int:
    """Replay unfinished source changes before verification or activation.

    The newest event per GCS source establishes final state. Older events may
    only clear after the newer generation has reached every live version.
    """
    active, candidate = cloud_index_versions(bound_resource)
    if active is None and candidate is None:
        return 0
    corpus = cloud_corpus_key(bound_resource)
    reconciled = 0
    if not isinstance(max_batches, int) or not 1 <= max_batches <= 100:
        raise CloudVectorIndexError("Vector repair limit is invalid")
    for _ in range(max_batches):
        updates = pending_updates(corpus, limit=1000)
        if not updates:
            return reconciled
        by_source: dict[str, list] = {}
        for item in updates:
            by_source.setdefault(item.source_uri, []).append(item)
        for uri, pending in by_source.items():
            with cloud_source_mutation_lock(bound_resource, uri):
                _reconcile_source(bound_resource, corpus, uri, pending, automatic)
        reconciled += len(updates)
    if not pending_updates(corpus, limit=1):
        return reconciled
    raise CloudVectorIndexError("Vector source backlog requires another repair run")

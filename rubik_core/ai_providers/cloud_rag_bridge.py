"""Route bound Cloud RAG corpora through a workspace's selected vector index.

The control pointer chooses a model/version for reads. A pending source event
blocks reads until every serving version has received the same source change.
"""

from __future__ import annotations

from dataclasses import dataclass

from google.api_core.exceptions import NotFound
from google.cloud import storage

from rubik_core.adapters.base import RetrievedChunk
from rubik_core.ai_providers.cloud_sources import load_gcs_source
from rubik_core.ai_providers.cloud_vector_index import (
    CloudIndexVector,
    CloudVectorIndexError,
    enqueue_source_update,
    mark_active,
    mark_update_applied,
    mark_update_failed,
    pending_updates,
    query_chunks,
    replace_source,
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
from rubik_core.config import settings
from rubik_core.cost.usage import feature_context
from rubik_core.tenancy.storage import assert_own_uri

_EMBED_BATCH = 32


class CloudSourceNotFound(CloudVectorIndexError):
    """A validated GCS source is absent, distinct from a transient read error."""


def _bound_source_membership(bound_resource: str) -> set[str]:
    """Resolve currently published source URIs from the exact bound corpus."""
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
                _source_for_filed_name,
                _verified_ragfiles,
            )

            mapped = _verified_ragfiles(bound_resource)
            for name, uri in mapped.items():
                if _source_for_filed_name(name) != uri:
                    raise CloudVectorIndexError(
                        "Private vector source has no owner record"
                    )
            return set(mapped.values())

        from vertexai.preview import rag

        from rubik_core.adapters.vertex_rag import _init_vertexai

        _init_vertexai()
        uris: set[str] = set()
        for item in rag.list_files(corpus_name=bound_resource):
            assert_own_rag_file(item.name, bound_resource)
            sources = list(getattr(getattr(item, "gcs_source", None), "uris", []) or [])
            if not sources:
                raise CloudVectorIndexError("Bound RAG file has no GCS source")
            for uri in sources:
                assert_own_uri(uri)
                uris.add(uri)
        return uris
    except CloudVectorIndexError:
        raise
    except Exception:
        raise CloudVectorIndexError("Bound vector sources are unavailable") from None


@dataclass(frozen=True)
class CloudSourceUpdate:
    id: str
    generation: int


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
    corpus = cloud_corpus_key(bound_resource)
    if active is not None and not pending_updates(corpus, limit=1):
        mark_active(active)
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
                    replace_source(spec, uri, source.generation, source.sha256, chunks)
        elif operation == "delete":
            for spec in versions:
                tombstone_source(spec, uri, update.generation)
        else:
            raise CloudVectorIndexError("Vector source operation is invalid")
        mark_update_applied(update.id)
    except Exception:
        mark_update_failed(update.id)
        raise


def fail_cloud_source_update(update: CloudSourceUpdate | None) -> None:
    """Retain a failed event so active reads and cutover fail closed."""
    if update is not None:
        mark_update_failed(update.id)


def reconcile_pending(bound_resource: str) -> int:
    """Replay unfinished source changes before verification or activation.

    The newest event per GCS source establishes final state. Older events may
    only clear after the newer generation has reached every live version.
    """
    active, candidate = cloud_index_versions(bound_resource)
    if active is None and candidate is None:
        return 0
    corpus = cloud_corpus_key(bound_resource)
    updates = pending_updates(corpus, limit=1000)
    if not updates:
        return 0
    published = _bound_source_membership(bound_resource)
    by_source = {}
    for item in updates:
        by_source.setdefault(item.source_uri, []).append(item)
    for uri, events in by_source.items():
        events.sort(key=lambda item: (item.generation, item.id))
        operations_by_generation: dict[int, set[str]] = {}
        for item in events:
            operations_by_generation.setdefault(item.generation, set()).add(
                item.operation
            )
        if any(len(operations) > 1 for operations in operations_by_generation.values()):
            raise CloudVectorIndexError("Conflicting vector source updates")
        newest = events[-1]
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
        else:
            if uri in published:
                raise CloudVectorIndexError(
                    "Vector source is still published in its bound corpus"
                )
            try:
                current_generation = _source_generation(uri, None)
            except CloudSourceNotFound:
                current_generation = None
            if (
                current_generation is not None
                and current_generation > newest.generation
            ):
                raise CloudVectorIndexError(
                    "Vector source changed while a deletion was pending"
                )
        finish_cloud_source_update(
            bound_resource,
            uri,
            newest.operation,
            CloudSourceUpdate(newest.id, newest.generation),
        )
        for item in events[:-1]:
            mark_update_applied(item.id)
    return len(updates)

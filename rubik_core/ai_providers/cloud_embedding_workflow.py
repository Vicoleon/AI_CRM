"""Prepare and activate a client embedding index for one bound cloud corpus.

The live Vertex corpus remains the source of truth during backfill. Candidate
storage is exposed to writers before indexing starts, and an exact GCS source
manifest is checked again immediately before the control pointer changes.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict

import vertexai
from sqlalchemy import select
from vertexai.preview import rag

from rubik_core.ai_providers.cloud_sources import CloudSourceError, load_gcs_source
from rubik_core.ai_providers.cloud_vector_index import (
    CloudVectorIndexError,
    CloudVectorSpec,
    build_ann_index,
    create_candidate,
    mark_active,
    replace_source,
    retire_candidate,
    verify_candidate,
)
from rubik_core.ai_providers.embeddings import (
    embed_connection_probe,
    embed_with_route,
    embedding_dimension,
)
from rubik_core.ai_providers.registry import (
    IncompatibleModelRoute,
    provider_connection_route,
)
from rubik_core.config import AppMode, settings
from rubik_core.control.db import control_session
from rubik_core.control.models import Workspace, WorkspaceConnector, WorkspaceCorpus
from rubik_core.cost.usage import feature_context
from rubik_core.tenancy.context import current_workspace
from rubik_core.tenancy.corpora import (
    assert_own_rag_file,
    corpus_id,
    require_bound_corpus_id,
)
from rubik_core.tenancy.storage import assert_own_uri

_CONNECTOR = "ai_embedding_indexes"


def _cloud_only() -> None:
    if settings.APP_MODE != AppMode.GCP:
        raise IncompatibleModelRoute("Cloud embedding workflow requires GCP mode")


def cloud_corpora() -> list[dict[str, str]]:
    """List only this workspace's control-bound corpora for the admin selector."""
    _cloud_only()
    slug = current_workspace()
    from rubik_core.ai_providers.embedding_index import cloud_corpus_key

    with control_session() as db:
        workspace = db.get(Workspace, slug)
        if workspace is None or workspace.status != "active":
            raise IncompatibleModelRoute("Workspace is unavailable")
        fixed = dict(workspace.corpus_ids or {})
        dynamic = db.scalars(
            select(WorkspaceCorpus).where(WorkspaceCorpus.workspace_slug == slug)
        ).all()
    result = [
        {
            "key": cloud_corpus_key(resource),
            "label": "Default knowledge"
            if kind == "default"
            else kind.replace("_", " ").title(),
        }
        for kind, resource in fixed.items()
    ]
    result.extend(
        {
            "key": cloud_corpus_key(row.corpus_id),
            "label": (
                "Knowledge: " + row.audience.replace("_", " ")
                if row.kind == "rag"
                else row.kind.replace("_", " ").title()
            ),
        }
        for row in dynamic
    )
    return sorted(result, key=lambda item: item["label"])


def _bound_resource(corpus: str) -> str:
    _cloud_only()
    slug = current_workspace()
    from rubik_core.ai_providers.embedding_index import cloud_corpus_key

    with control_session() as db:
        workspace = db.get(Workspace, slug)
        if workspace is None or workspace.status != "active":
            raise IncompatibleModelRoute("Workspace is unavailable")
        fixed = dict(workspace.corpus_ids or {})
        dynamic = db.scalars(
            select(WorkspaceCorpus).where(WorkspaceCorpus.workspace_slug == slug)
        ).all()
    if corpus == "default":
        resource = fixed.get("default")
        if resource:
            return require_bound_corpus_id(resource)
    for resource in [*fixed.values(), *(row.corpus_id for row in dynamic)]:
        if cloud_corpus_key(resource) == corpus:
            return require_bound_corpus_id(resource)
    raise IncompatibleModelRoute("Knowledge corpus is not bound to this workspace")


def _source_uris(resource: str) -> set[str]:
    """Use exact bound-corpus file membership, refusing unindexable sources."""
    require_bound_corpus_id(resource)
    if corpus_id("private_uploads", create=False) == resource:
        from rubik_core.chat_files.private_index import ensure_cloud_sources

        try:
            return ensure_cloud_sources()
        except Exception:
            raise CloudVectorIndexError(
                "Private knowledge sources cannot be indexed"
            ) from None
    try:
        vertexai.init(
            project=settings.GCP_PROJECT_ID, location=settings.VERTEX_RAG_LOCATION
        )
        files = list(rag.list_files(corpus_name=resource))
    except Exception:
        raise CloudVectorIndexError("Knowledge source list is unavailable") from None
    uris: set[str] = set()
    for file in files:
        try:
            assert_own_rag_file(getattr(file, "name", None), resource)
        except ValueError:
            raise CloudVectorIndexError(
                "Knowledge source belongs to another corpus"
            ) from None
        values = getattr(getattr(file, "gcs_source", None), "uris", None)
        if not values or len(values) != 1:
            raise CloudVectorIndexError("Knowledge source cannot be indexed")
        try:
            uri = assert_own_uri(values[0])
        except (ValueError, PermissionError):
            raise CloudVectorIndexError(
                "Knowledge source belongs to another workspace"
            ) from None
        if uri in uris:
            raise CloudVectorIndexError("Knowledge source is duplicated")
        uris.add(uri)
    return uris


def _snapshot(resource: str) -> dict[str, tuple[int, str]]:
    """Recheck bytes and generation so a changed source blocks cutover."""
    return {
        uri: (source.generation, source.sha256)
        for uri in sorted(_source_uris(resource))
        for source in [load_gcs_source(uri)]
    }


def prepare_cloud_embedding_index(
    provider: str, model: str, *, corpus: str = "default"
) -> dict[str, object]:
    _cloud_only()
    if provider not in ("openai", "gemini"):
        raise IncompatibleModelRoute("Cloud client embeddings support OpenAI or Gemini")
    resource = _bound_resource(corpus)
    from rubik_core.ai_providers.embedding_index import _cloud_spec, cloud_corpus_key

    key = cloud_corpus_key(resource)
    dimension = embedding_dimension(provider, model)
    route = provider_connection_route(
        provider, model, feature="embedding_index", capability="embedding"
    )
    embed_connection_probe(provider, model)
    # Snapshot before exposing the candidate; concurrent writes after this point
    # are captured by the outbox and will block activation until reconciled.
    source_uris = _source_uris(resource)
    spec = CloudVectorSpec(
        current_workspace(), key, provider, model, dimension, uuid.uuid4().hex[:16]
    )
    create_candidate(spec)
    slug = current_workspace()
    previous_candidate = None
    try:
        with control_session() as db:
            row = db.get(WorkspaceConnector, (slug, _CONNECTOR), with_for_update=True)
            if row is None:
                row = WorkspaceConnector(workspace_slug=slug, connector=_CONNECTOR)
                db.add(row)
            config = dict(row.config or {})
            corpora = dict(config.get("corpora") or {})
            item = dict(corpora.get(key) or {})
            previous = item.get("candidate")
            if isinstance(previous, dict):
                previous_candidate = _cloud_spec(previous.get("spec"), key)
            item["candidate"] = {"spec": asdict(spec)}
            corpora[key] = item
            row.config = {**config, "corpora": corpora}
            row.status = "connected"
            db.commit()
    except Exception:
        try:
            retire_candidate(spec)
        except Exception:
            pass
        raise CloudVectorIndexError(
            "Cloud embedding candidate could not be published"
        ) from None
    if previous_candidate:
        retire_candidate(previous_candidate)
    expected: dict[str, tuple[int, str]] = {}
    try:
        with feature_context("embedding_index_rebuild"):
            for uri in sorted(source_uris):
                source = load_gcs_source(uri)
                vectors = embed_with_route(
                    route, [chunk.text for chunk in source.chunks], dimension
                )
                chunks = [
                    chunk.model_copy(update={"embedding": vector})
                    for chunk, vector in zip(source.chunks, vectors, strict=True)
                ]
                replace_source(spec, uri, source.generation, source.sha256, chunks)
                expected[uri] = (source.generation, source.sha256)
        from rubik_core.ai_providers.cloud_rag_bridge import reconcile_pending

        reconcile_pending(resource)
        if _snapshot(resource) != expected:
            raise CloudVectorIndexError("Knowledge sources changed; rebuild again")
        verified = verify_candidate(spec, expected)
        build_ann_index(spec)
    except (CloudSourceError, CloudVectorIndexError, IncompatibleModelRoute):
        raise
    except Exception:
        raise CloudVectorIndexError("Cloud embedding rebuild failed") from None
    return {
        "version": spec.version,
        "corpus": key,
        "provider": provider,
        "model": model,
        "dimension": dimension,
        "indexed_chunks": verified.chunk_count,
        "indexed_sources": verified.source_count,
    }


def activate_cloud_embedding_index(
    version: str, *, corpus: str = "default"
) -> dict[str, object]:
    _cloud_only()
    resource = _bound_resource(corpus)
    from rubik_core.ai_providers.embedding_index import (
        _cloud_spec,
        cloud_corpus_key,
        cloud_index_versions,
    )

    key = cloud_corpus_key(resource)
    active, candidate = cloud_index_versions(resource)
    if candidate is None or candidate.version != version:
        raise IncompatibleModelRoute("Prepared embedding index is unavailable")
    provider_connection_route(
        candidate.provider,
        candidate.model,
        feature="embedding_index",
        capability="embedding",
    )
    expected = _snapshot(resource)
    from rubik_core.ai_providers.cloud_rag_bridge import reconcile_pending

    reconcile_pending(resource)
    verified = verify_candidate(candidate, expected)
    build_ann_index(candidate)
    # This transaction is the only serving-pointer switch. A new outbox event
    # after verification blocks reads until its vectors have been written.
    slug = current_workspace()
    with control_session() as db:
        row = db.get(WorkspaceConnector, (slug, _CONNECTOR), with_for_update=True)
        config = dict(row.config or {}) if row else {}
        corpora = dict(config.get("corpora") or {})
        item = dict(corpora.get(key) or {})
        stored = item.get("candidate")
        if (
            row is None
            or not isinstance(stored, dict)
            or _cloud_spec(stored.get("spec"), key) != candidate
            or item.get("active") != (asdict(active) if active else None)
        ):
            raise IncompatibleModelRoute("Embedding candidate changed; rebuild again")
        item["active"] = asdict(candidate)
        item.pop("candidate", None)
        corpora[key] = item
        row.config = {**config, "corpora": corpora}
        db.commit()
    try:
        mark_active(candidate)
    except Exception:
        # The tenant version can change between verification and this control
        # pointer commit. Restore the prior serving pointer and keep the
        # candidate available for a fresh verify/activate attempt.
        with control_session() as db:
            row = db.get(WorkspaceConnector, (slug, _CONNECTOR), with_for_update=True)
            config = dict(row.config or {}) if row else {}
            corpora = dict(config.get("corpora") or {})
            item = dict(corpora.get(key) or {})
            if item.get("active") == asdict(candidate) and "candidate" not in item:
                if active is None:
                    item.pop("active", None)
                else:
                    item["active"] = asdict(active)
                item["candidate"] = {"spec": asdict(candidate)}
                corpora[key] = item
                row.config = {**config, "corpora": corpora}
                db.commit()
        raise CloudVectorIndexError(
            "Embedding index changed during activation; retry after sync"
        ) from None
    return {
        "version": candidate.version,
        "corpus": key,
        "provider": candidate.provider,
        "model": candidate.model,
        "dimension": candidate.dimension,
        "indexed_chunks": verified.chunk_count,
        "indexed_sources": verified.source_count,
    }


def cloud_embedding_status(*, corpus: str = "default") -> dict[str, object]:
    _cloud_only()
    options = cloud_corpora()
    selected = corpus
    if (
        corpus == "default"
        and options
        and not any(item["label"] == "Default knowledge" for item in options)
    ):
        selected = options[0]["key"]
    resource = _bound_resource(selected) if options else None
    from rubik_core.ai_providers.embedding_index import cloud_index_versions

    active, candidate = cloud_index_versions(resource) if resource else (None, None)
    selected = (
        next(item["key"] for item in options if item["key"] == selected)
        if selected != "default"
        else next(
            (item["key"] for item in options if item["label"] == "Default knowledge"),
            "default",
        )
    )
    return {
        "storage": "cloud_sql_pgvector",
        "client_embeddings_available": True,
        "corpora": options,
        "selected_corpus": selected,
        "active": asdict(active)
        if active
        else {
            "provider": "vertex_managed",
            "model": settings.VERTEX_EMBEDDING_MODEL,
            "dimension": embedding_dimension(
                "vertex_managed", settings.VERTEX_EMBEDDING_MODEL
            ),
            "version": "legacy",
        },
        "candidate": asdict(candidate) if candidate else None,
    }

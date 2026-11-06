"""Versioned workspace vector indexes with a validated cutover.

The active pointer lives in the control database. Local rebuilds use a fresh
Chroma collection, while GCP rebuilds use a tenant Cloud SQL pgvector version.
Both routes verify source content before switching the serving pointer.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import uuid
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

from rubik_core.ai_providers.cloud_vector_index import CloudVectorSpec
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
from rubik_core.control.models import Workspace, WorkspaceConnector
from rubik_core.cost.usage import feature_context
from rubik_core.identifiers import CHROMA_COLLECTION
from rubik_core.tenancy.context import current_workspace
from rubik_core.tenancy.corpora import require_bound_corpus_id

_CORPUS = re.compile(r"[a-z][a-z0-9_]{0,39}\Z")
_VERSION = re.compile(r"[0-9a-f]{16}\Z")
_CONNECTOR = "ai_embedding_indexes"
_ROUTES = "ai_routes"
_BATCH = 32


def cloud_corpus_key(bound_resource: str) -> str:
    """Stable, opaque tenant-vector key for an explicitly bound Vertex corpus."""
    resource = require_bound_corpus_id(bound_resource)
    return "c_" + hashlib.sha256(resource.encode("utf-8")).hexdigest()[:24]


def _cloud_spec(data: object, corpus: str) -> CloudVectorSpec:
    if not isinstance(data, dict):
        raise IncompatibleModelRoute("Cloud embedding index configuration is invalid")
    try:
        spec = CloudVectorSpec(**data)
        expected = embedding_dimension(spec.provider, spec.model)
    except (TypeError, ValueError, IncompatibleModelRoute):
        raise IncompatibleModelRoute(
            "Cloud embedding index configuration is invalid"
        ) from None
    if (
        spec.workspace != current_workspace()
        or spec.corpus != corpus
        or not isinstance(spec.version, str)
        or not _VERSION.fullmatch(spec.version)
        or spec.provider not in ("openai", "gemini")
        or spec.dimension != expected
    ):
        raise IncompatibleModelRoute("Cloud embedding index configuration is invalid")
    return spec


def cloud_index_versions(
    bound_resource: str,
) -> tuple[CloudVectorSpec | None, CloudVectorSpec | None]:
    """Resolve active and building indexes for this workspace's bound corpus."""
    if settings.APP_MODE != AppMode.GCP:
        return None, None
    corpus = cloud_corpus_key(bound_resource)
    slug = current_workspace()
    with control_session() as session:
        workspace = session.get(Workspace, slug)
        if workspace is None or workspace.status != "active":
            raise IncompatibleModelRoute("Workspace is unavailable")
        row = session.get(WorkspaceConnector, (slug, _CONNECTOR))
        route_row = session.get(WorkspaceConnector, (slug, _ROUTES))
        config = row.config if row is not None else None
        corpora = config.get("corpora") if isinstance(config, dict) else None
        item = corpora.get(corpus) if isinstance(corpora, dict) else None
        routes = (route_row.config or {}).get("features") if route_row else None
        selected = routes.get("embedding_index") if isinstance(routes, dict) else None
    active_data = item.get("active") if isinstance(item, dict) else None
    candidate_data = item.get("candidate") if isinstance(item, dict) else None
    active = _cloud_spec(active_data, corpus) if active_data is not None else None
    candidate = (
        _cloud_spec(candidate_data.get("spec"), corpus)
        if isinstance(candidate_data, dict)
        else None
    )
    if selected is not None and (
        active is None
        or selected != {"provider": active.provider, "model": active.model}
    ):
        raise IncompatibleModelRoute(
            "Selected embedding model has no active matching index; rebuild it first"
        )
    return active, candidate


def active_cloud_index(bound_resource: str) -> CloudVectorSpec | None:
    return cloud_index_versions(bound_resource)[0]


@dataclass(frozen=True)
class EmbeddingIndexSpec:
    workspace: str
    corpus: str
    provider: str
    model: str
    dimension: int
    version: str
    collection: str


def _corpus(value: str) -> str:
    if not isinstance(value, str) or not _CORPUS.fullmatch(value):
        raise ValueError("Invalid embedding corpus")
    return value


def _legacy(corpus: str) -> EmbeddingIndexSpec:
    model = settings.VERTEX_EMBEDDING_MODEL
    if model != "text-embedding-004":
        raise IncompatibleModelRoute("Managed embedding model is not supported")
    return EmbeddingIndexSpec(
        current_workspace(),
        corpus,
        "vertex_managed",
        model,
        embedding_dimension("vertex_managed", model),
        "legacy",
        CHROMA_COLLECTION if corpus == "default" else f"{CHROMA_COLLECTION}_{corpus}",
    )


def _parse_spec(data: object, corpus: str) -> EmbeddingIndexSpec:
    if not isinstance(data, dict):
        raise IncompatibleModelRoute("Embedding index configuration is invalid")
    try:
        spec = EmbeddingIndexSpec(**data)
    except (TypeError, ValueError):
        raise IncompatibleModelRoute(
            "Embedding index configuration is invalid"
        ) from None
    if (
        spec.workspace != current_workspace()
        or spec.corpus != corpus
        or not isinstance(spec.version, str)
        or not _VERSION.fullmatch(spec.version)
        or spec.collection != f"{CHROMA_COLLECTION}_i_{spec.version}"
        or spec.dimension != embedding_dimension(spec.provider, spec.model)
    ):
        raise IncompatibleModelRoute("Embedding index configuration is invalid")
    return spec


def _active_from_config(config: object, corpus: str) -> EmbeddingIndexSpec:
    if not isinstance(config, dict):
        return _legacy(corpus)
    corpora = config.get("corpora")
    if corpora is None:
        return _legacy(corpus)
    if not isinstance(corpora, dict):
        raise IncompatibleModelRoute("Embedding index configuration is invalid")
    item = corpora.get(corpus)
    if item is None:
        return _legacy(corpus)
    if not isinstance(item, dict):
        raise IncompatibleModelRoute("Embedding index configuration is invalid")
    if item.get("active") is None:
        return _legacy(corpus)
    return _parse_spec(item["active"], corpus)


def active_embedding_index(corpus: str = "default") -> EmbeddingIndexSpec:
    """Resolve the active index and reject a route that points elsewhere."""
    corpus = _corpus(corpus)
    slug = current_workspace()
    with control_session() as session:
        workspace = session.get(Workspace, slug)
        if workspace is None or workspace.status != "active":
            raise IncompatibleModelRoute("Workspace is unavailable")
        index_row = session.get(WorkspaceConnector, (slug, _CONNECTOR))
        route_row = session.get(WorkspaceConnector, (slug, _ROUTES))
        config = index_row.config if index_row is not None else None
        spec = _active_from_config(config, corpus)
        routes = (route_row.config or {}).get("features") if route_row else None
        chosen = routes.get("embedding_index") if isinstance(routes, dict) else None
    if (
        corpus == "default"
        and chosen is not None
        and chosen
        != {
            "provider": spec.provider,
            "model": spec.model,
        }
    ):
        raise IncompatibleModelRoute(
            "Selected embedding model has no active matching index; rebuild it first"
        )
    return spec


def _local_only() -> None:
    if settings.APP_MODE == AppMode.GCP:
        raise IncompatibleModelRoute("Local vector index is unavailable in GCP mode")


def _workspace_dir() -> Path:
    path = (
        Path(settings.CHROMA_PERSIST_DIR).expanduser().resolve()
        / "workspaces"
        / current_workspace()
    )
    if path.is_symlink():
        raise PermissionError("Workspace vector index path is a symlink")
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise PermissionError("Workspace vector index path is a symlink")
    return path


@contextlib.contextmanager
def embedding_index_lock(*, shared: bool = False) -> Iterator[None]:
    """Cross-process lock for local writes and active-pointer cutover."""
    lock_path = _workspace_dir() / ".embedding-index.lock"
    fd = os.open(
        lock_path,
        os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _rows(collection) -> list[tuple[str, str, dict]]:
    rows: list[tuple[str, str, dict]] = []
    offset = 0
    while True:
        page = collection.get(
            limit=100,
            offset=offset,
            include=["documents", "metadatas"],
        )
        ids = page.get("ids") or []
        documents = page.get("documents") or []
        metadatas = page.get("metadatas") or []
        if not (len(ids) == len(documents) == len(metadatas)):
            raise IncompatibleModelRoute("Embedding source index is incomplete")
        for identifier, document, metadata in zip(ids, documents, metadatas):
            if (
                not isinstance(identifier, str)
                or not isinstance(document, str)
                or not isinstance(metadata, dict)
            ):
                raise IncompatibleModelRoute("Embedding source index is incomplete")
            rows.append((identifier, document, metadata))
        offset += len(ids)
        if len(ids) < 100:
            break
    if len(rows) != collection.count():
        raise IncompatibleModelRoute("Embedding source changed during rebuild")
    return sorted(rows, key=lambda row: row[0])


def _fingerprint(rows: list[tuple[str, str, dict]]) -> str:
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _collection_for(spec: EmbeddingIndexSpec, *, create: bool = False):
    from rubik_core.adapters.chroma import ChromaAdapter

    return ChromaAdapter(
        collection_name=spec.collection, index_spec=spec, create=create
    )


def prepare_embedding_index(
    provider: str, model: str, *, corpus: str = "default"
) -> dict[str, object]:
    """Build a candidate collection without changing any live query route."""
    if settings.APP_MODE == AppMode.GCP:
        from rubik_core.ai_providers.cloud_embedding_workflow import (
            prepare_cloud_embedding_index,
        )

        return prepare_cloud_embedding_index(provider, model, corpus=corpus)
    _local_only()
    corpus = _corpus(corpus)
    dimension = embedding_dimension(provider, model)
    source_spec = active_embedding_index(corpus)
    route = provider_connection_route(
        provider, model, feature="embedding_index", capability="embedding"
    )
    embed_connection_probe(provider, model)
    source = _collection_for(source_spec)
    with embedding_index_lock(shared=True):
        source_rows = _rows(source.collection)
        source_fingerprint = _fingerprint(source_rows)
    version = uuid.uuid4().hex[:16]
    spec = EmbeddingIndexSpec(
        current_workspace(),
        corpus,
        provider,
        model,
        dimension,
        version,
        f"{CHROMA_COLLECTION}_i_{version}",
    )
    target = _collection_for(spec, create=True)
    try:
        with feature_context("embedding_index_rebuild"):
            for start in range(0, len(source_rows), _BATCH):
                batch = source_rows[start : start + _BATCH]
                vectors = embed_with_route(route, [row[1] for row in batch], dimension)
                target.collection.upsert(
                    ids=[row[0] for row in batch],
                    documents=[row[1] for row in batch],
                    metadatas=[row[2] for row in batch],
                    embeddings=vectors,
                )
        if _fingerprint(_rows(target.collection)) != source_fingerprint:
            raise IncompatibleModelRoute("Rebuilt embedding index did not match source")
        with embedding_index_lock(shared=True):
            if _fingerprint(_rows(source.collection)) != source_fingerprint:
                raise IncompatibleModelRoute("Embedding source changed; rebuild again")
        slug = current_workspace()
        with control_session() as session:
            row = session.get(WorkspaceConnector, (slug, _CONNECTOR))
            if row is None:
                row = WorkspaceConnector(workspace_slug=slug, connector=_CONNECTOR)
                session.add(row)
            config = dict(row.config or {})
            corpora = dict(config.get("corpora") or {})
            item = dict(corpora.get(corpus) or {})
            if _active_from_config(config, corpus) != source_spec:
                raise IncompatibleModelRoute("Embedding index changed; rebuild again")
            item["candidate"] = {
                "spec": asdict(spec),
                "source": asdict(source_spec),
                "source_fingerprint": source_fingerprint,
                "indexed_chunks": len(source_rows),
            }
            corpora[corpus] = item
            row.config = {**config, "corpora": corpora}
            row.status = "connected"
            session.commit()
    except Exception:
        try:
            target.client.delete_collection(spec.collection)
        except Exception:
            pass
        raise
    return {
        "version": version,
        "provider": provider,
        "model": model,
        "dimension": dimension,
        "indexed_chunks": len(source_rows),
    }


def activate_embedding_index(
    version: str, *, corpus: str = "default"
) -> dict[str, object]:
    """Switch to a complete candidate after checking the source under lock."""
    if settings.APP_MODE == AppMode.GCP:
        from rubik_core.ai_providers.cloud_embedding_workflow import (
            activate_cloud_embedding_index,
        )

        return activate_cloud_embedding_index(version, corpus=corpus)
    _local_only()
    corpus = _corpus(corpus)
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise ValueError("Invalid embedding index version")
    slug = current_workspace()
    with embedding_index_lock():
        with control_session() as session:
            row = session.get(WorkspaceConnector, (slug, _CONNECTOR))
            config = row.config if row is not None else None
            corpora = config.get("corpora") if isinstance(config, dict) else None
            item = corpora.get(corpus) if isinstance(corpora, dict) else None
            candidate = item.get("candidate") if isinstance(item, dict) else None
        if not isinstance(candidate, dict):
            raise IncompatibleModelRoute("No prepared embedding index is available")
        spec = _parse_spec(candidate.get("spec"), corpus)
        if spec.version != version:
            raise IncompatibleModelRoute("Embedding candidate version changed")
        source_data = candidate.get("source")
        if not isinstance(source_data, dict):
            raise IncompatibleModelRoute("Embedding candidate is invalid")
        source_spec = (
            _legacy(corpus)
            if source_data.get("version") == "legacy"
            else _parse_spec(source_data, corpus)
        )
        if _active_from_config(config, corpus) != source_spec:
            raise IncompatibleModelRoute("Embedding index changed; rebuild again")
        provider_connection_route(
            spec.provider,
            spec.model,
            feature="embedding_index",
            capability="embedding",
        )
        source = _collection_for(source_spec)
        target = _collection_for(spec)
        fingerprint = candidate.get("source_fingerprint")
        if (
            not isinstance(fingerprint, str)
            or _fingerprint(_rows(source.collection)) != fingerprint
            or _fingerprint(_rows(target.collection)) != fingerprint
            or target.collection.count() != candidate.get("indexed_chunks")
        ):
            raise IncompatibleModelRoute("Embedding source changed; rebuild again")
        with control_session() as session:
            row = session.get(WorkspaceConnector, (slug, _CONNECTOR))
            if row is None or row.config != config:
                raise IncompatibleModelRoute(
                    "Embedding candidate changed; rebuild again"
                )
            if corpus == "default":
                route_row = session.get(WorkspaceConnector, (slug, _ROUTES))
                if route_row is None:
                    route_row = WorkspaceConnector(
                        workspace_slug=slug, connector=_ROUTES
                    )
                    session.add(route_row)
                routes = dict((route_row.config or {}).get("features") or {})
                routes["embedding_index"] = {
                    "provider": spec.provider,
                    "model": spec.model,
                }
                route_row.config = {"features": routes}
                route_row.status = "connected"
            new_item = dict(item)
            new_item["active"] = asdict(spec)
            new_item.pop("candidate", None)
            new_corpora = dict(corpora)
            new_corpora[corpus] = new_item
            row.config = {**config, "corpora": new_corpora}
            session.commit()
    return {
        "version": spec.version,
        "provider": spec.provider,
        "model": spec.model,
        "dimension": spec.dimension,
        "indexed_chunks": target.collection.count(),
    }


def embedding_index_status(*, corpus: str = "default") -> dict[str, object]:
    if settings.APP_MODE == AppMode.GCP:
        from rubik_core.ai_providers.cloud_embedding_workflow import (
            cloud_embedding_status,
        )

        return cloud_embedding_status(corpus=corpus)
    corpus = _corpus(corpus)
    slug = current_workspace()
    with control_session() as session:
        row = session.get(WorkspaceConnector, (slug, _CONNECTOR))
        config = row.config if row is not None else None
        corpora = config.get("corpora") if isinstance(config, dict) else None
        item = corpora.get(corpus) if isinstance(corpora, dict) else None
        candidate = item.get("candidate") if isinstance(item, dict) else None
        active = _active_from_config(config, corpus)
    prepared = (
        _parse_spec(candidate.get("spec"), corpus)
        if isinstance(candidate, dict)
        else None
    )
    return {
        "storage": "vertex_rag" if settings.APP_MODE == AppMode.GCP else "chroma",
        "client_embeddings_available": settings.APP_MODE != AppMode.GCP,
        "active": asdict(active),
        "candidate": asdict(prepared) if prepared else None,
    }

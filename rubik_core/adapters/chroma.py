from __future__ import annotations

# rubik_core/adapters/chroma.py
import logging
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any

import chromadb
from chromadb.config import Settings as ChromaSettings

from rubik_core.adapters.base import (
    Chunk,
    RetrievedChunk,
    UpsertResult,
    VectorStoreAdapter,
)
from rubik_core.config import settings
from rubik_core.identifiers import CHROMA_COLLECTION
from rubik_core.tenancy.context import current_workspace

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from rubik_core.ai_providers.embedding_index import EmbeddingIndexSpec


class IndexChangedError(ValueError):
    """The adapter no longer points to the workspace's active index."""


class ChromaAdapter(VectorStoreAdapter):
    def __init__(
        self,
        collection_name: str = CHROMA_COLLECTION,
        *,
        index_spec: EmbeddingIndexSpec | None = None,
        create: bool = False,
    ):
        from rubik_core.ai_providers.embedding_index import active_embedding_index

        if create and index_spec is None:
            raise ValueError("Only a prepared index may create a versioned collection")
        self.workspace = current_workspace()
        self._active_bound = index_spec is None and collection_name == CHROMA_COLLECTION
        self.index_spec = active_embedding_index() if self._active_bound else index_spec
        if self.index_spec is not None and self.index_spec.workspace != self.workspace:
            raise PermissionError("Vector index belongs to another workspace")
        self.collection_name = (
            self.index_spec.collection if self._active_bound else collection_name
        )
        base = Path(settings.CHROMA_PERSIST_DIR).expanduser().resolve() / "workspaces"
        path = base / self.workspace
        if path.is_symlink():
            raise ValueError("Workspace Chroma root cannot be a symlink")
        self.client = chromadb.PersistentClient(
            path=str(path.resolve()), settings=ChromaSettings(allow_reset=True)
        )
        metadata: dict[str, str | int] = {"hnsw:space": "cosine"}
        if self.index_spec is not None and self.index_spec.version != "legacy":
            metadata.update(
                {
                    "ai_workspace": self.index_spec.workspace,
                    "ai_corpus": self.index_spec.corpus,
                    "ai_provider": self.index_spec.provider,
                    "ai_model": self.index_spec.model,
                    "ai_dimension": self.index_spec.dimension,
                    "ai_version": self.index_spec.version,
                }
            )
        self._collection_metadata = metadata
        if (
            self.index_spec is not None
            and self.index_spec.version != "legacy"
            and not create
        ):
            try:
                self.collection = self.client.get_collection(name=self.collection_name)
            except Exception:
                raise IndexChangedError(
                    "Active vector collection is unavailable; rebuild it"
                ) from None
        else:
            self.collection = self.client.get_or_create_collection(
                name=self.collection_name, metadata=metadata
            )
        if self.index_spec is not None and self.index_spec.version != "legacy":
            actual = self.collection.metadata or {}
            if any(actual.get(key) != value for key, value in metadata.items()):
                raise ValueError("Vector collection metadata does not match index spec")

    def _check_bound(self) -> None:
        if current_workspace() != self.workspace:
            raise PermissionError("Vector index belongs to another workspace")
        if self._active_bound:
            from rubik_core.ai_providers.embedding_index import active_embedding_index

            if active_embedding_index() != self.index_spec:
                raise IndexChangedError("Vector index changed; retry this request")

    def _lock(self, *, shared: bool = False):
        if self._active_bound:
            from rubik_core.ai_providers.embedding_index import embedding_index_lock

            return embedding_index_lock(shared=shared)
        return nullcontext()

    def upsert(
        self,
        chunks: list[Chunk],
        *,
        embedding_spec: EmbeddingIndexSpec | None = None,
    ) -> UpsertResult:
        self._check_bound()
        if self.index_spec is not None and any(
            c.embedding is not None and len(c.embedding) != self.index_spec.dimension
            for c in chunks
        ):
            return UpsertResult(
                success=False, count=0, error="Embedding dimension mismatch"
            )
        if (
            self.index_spec is not None
            and self.index_spec.version != "legacy"
            and embedding_spec != self.index_spec
        ):
            return UpsertResult(
                success=False,
                count=0,
                error="Embedding source does not match active index",
            )
        try:
            ids = [c.chunk_id for c in chunks]
            embeddings = [c.embedding for c in chunks]
            documents = [c.text for c in chunks]
            metadatas = [c.metadata for c in chunks]

            # Ensure all chunks have embeddings
            if any(e is None for e in embeddings):
                return UpsertResult(success=False, count=0, error="Missing embeddings")

            with self._lock():
                self._check_bound()
                self.collection.upsert(
                    ids=ids,
                    embeddings=embeddings,
                    documents=documents,
                    metadatas=metadatas,
                )
            return UpsertResult(success=True, count=len(chunks))
        except Exception as e:
            logger.error(f"Error upserting to Chroma: {e}")
            return UpsertResult(success=False, count=0, error=str(e))

    def query(
        self,
        query_embedding: list[float],
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[RetrievedChunk]:
        self._check_bound()
        if (
            self.index_spec is not None
            and len(query_embedding) != self.index_spec.dimension
        ):
            raise ValueError("Embedding query dimension does not match active index")
        if self.index_spec is not None and self.index_spec.version != "legacy":
            from rubik_core.ai_providers.embeddings import IndexVector

            if (
                not isinstance(query_embedding, IndexVector)
                or query_embedding.index_spec != self.index_spec
            ):
                raise ValueError("Query vector does not match active embedding index")
        try:
            # Simple metadata filtering translation for Chroma
            # This is a basic implementation; complex filters might need more logic
            where = None
            if filters:
                if len(filters) == 1:
                    key, value = list(filters.items())[0]
                    where = {key: value}
                else:
                    where = {"$and": [{k: v} for k, v in filters.items()]}

            with self._lock(shared=True):
                self._check_bound()
                results = self.collection.query(
                    query_embeddings=[query_embedding], n_results=top_k, where=where
                )

            retrieved = []
            if results["ids"]:
                for i in range(len(results["ids"][0])):
                    chunk = Chunk(
                        chunk_id=results["ids"][0][i],
                        doc_id=results["metadatas"][0][i].get("doc_id", ""),
                        chunk_index=results["metadatas"][0][i].get("chunk_index", 0),
                        text=results["documents"][0][i],
                        metadata=results["metadatas"][0][i],
                    )
                    score = (
                        results["distances"][0][i] if "distances" in results else 0.0
                    )
                    retrieved.append(RetrievedChunk(chunk=chunk, score=score))

            return retrieved
        except (IndexChangedError, PermissionError):
            raise
        except chromadb.errors.NotFoundError:
            raise IndexChangedError(
                "Active vector collection is unavailable; rebuild it"
            ) from None
        except Exception as e:
            logger.error(f"Error querying Chroma: {e}")
            return []

    def delete(self, chunk_ids: list[str]) -> bool:
        self._check_bound()
        try:
            with self._lock():
                self._check_bound()
                self.collection.delete(ids=chunk_ids)
            return True
        except Exception as e:
            logger.error(f"Error deleting from Chroma: {e}")
            return False

    def delete_document(self, doc_id: str, source_path: str | None = None) -> bool:
        self._check_bound()
        try:
            with self._lock():
                self._check_bound()
                self.collection.delete(where={"doc_id": doc_id})
            return True
        except Exception as e:
            logger.error(f"Error deleting document from Chroma: {e}")
            return False

    def reset(self) -> bool:
        self._check_bound()
        try:
            with self._lock():
                self._check_bound()
                self.client.delete_collection(name=self.collection_name)
                self.collection = self.client.get_or_create_collection(
                    name=self.collection_name, metadata=self._collection_metadata
                )
            return True
        except Exception as e:
            logger.error(f"Error resetting Chroma: {e}")
            return False

    def get_chunks_by_metadata(
        self, filters: dict[str, Any], limit: int = 10
    ) -> list[Chunk]:
        self._check_bound()
        try:
            with self._lock(shared=True):
                self._check_bound()
                results = self.collection.get(where=filters, limit=limit)
            chunks = []
            if results["ids"]:
                for i in range(len(results["ids"])):
                    chunks.append(
                        Chunk(
                            chunk_id=results["ids"][i],
                            doc_id=results["metadatas"][i].get("doc_id", ""),
                            chunk_index=results["metadatas"][i].get("chunk_index", 0),
                            text=results["documents"][i],
                            metadata=results["metadatas"][i],
                        )
                    )
            return chunks
        except (IndexChangedError, PermissionError):
            raise
        except chromadb.errors.NotFoundError:
            raise IndexChangedError(
                "Active vector collection is unavailable; rebuild it"
            ) from None
        except Exception as e:
            logger.error(f"Error getting chunks by metadata from Chroma: {e}")
            return []

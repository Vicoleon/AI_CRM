from __future__ import annotations

# rubik_core/adapters/base.py
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel


class Chunk(BaseModel):
    chunk_id: str
    doc_id: str
    chunk_index: int
    text: str
    embedding: list[float] | None = None
    metadata: dict[str, Any]


class RetrievedChunk(BaseModel):
    chunk: Chunk
    score: float


class UpsertResult(BaseModel):
    success: bool
    count: int
    error: str | None = None


class VectorStoreAdapter(ABC):
    @abstractmethod
    def upsert(self, chunks: list[Chunk]) -> UpsertResult:
        """Upsert chunks into the vector store."""
        pass

    @abstractmethod
    def query(
        self,
        query_embedding: list[float],
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[RetrievedChunk]:
        """Query the vector store for similar chunks."""
        pass

    @abstractmethod
    def delete(self, chunk_ids: list[str]) -> bool:
        """Delete chunks from the vector store."""
        pass

    @abstractmethod
    def delete_document(self, doc_id: str, source_path: str | None = None) -> bool:
        """Delete an entire document from the vector store."""
        pass

    @abstractmethod
    def reset(self) -> bool:
        """Clear all data from the vector store."""
        pass

    @abstractmethod
    def get_chunks_by_metadata(
        self, filters: dict[str, Any], limit: int = 10
    ) -> list[Chunk]:
        """Fetch chunks directly by metadata (e.g. doc_id) without vector search."""
        pass

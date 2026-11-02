from __future__ import annotations

# rubik_core/adapters/vertex_rag.py
"""
Adapter that wraps Vertex AI RAG Engine for document ingestion and retrieval.

Ingestion:
  - Uploads raw files to GCS under gs://{bucket}/{prefix}/{filename}
  - Calls vertexai.rag.import_files to sync GCS paths into the RAG Corpus

Retrieval:
  - Calls vertexai.rag.retrieval_query to fetch relevant context chunks
  - Returns a list of RetrievedChunk objects (compatible with chain.py)
"""

import logging
from hashlib import sha256
from pathlib import Path
from typing import Any

import vertexai
from google.cloud import storage
from vertexai.preview import rag

from rubik_core.adapters.base import (
    Chunk,
    RetrievedChunk,
    UpsertResult,
    VectorStoreAdapter,
)
from rubik_core.ai_providers.cloud_rag_bridge import (
    begin_cloud_source_update,
    fail_cloud_source_update,
    finish_cloud_source_update,
    query_cloud_resource,
)
from rubik_core.config import settings
from rubik_core.tenancy.corpora import (
    assert_own_rag_file,
    corpus_id,
    require_bound_corpus_id,
)
from rubik_core.tenancy.storage import assert_own_uri, object_name, safe_upload_name

logger = logging.getLogger(__name__)

_INITIALIZED = False


def _init_vertexai():
    global _INITIALIZED
    if not _INITIALIZED:
        vertexai.init(
            project=settings.GCP_PROJECT_ID,
            location=settings.VERTEX_RAG_LOCATION,
        )
        _INITIALIZED = True


class VertexRagAdapter(VectorStoreAdapter):
    """
    Adapter for Vertex AI RAG Engine (ragCorpora).

    Unlike legacy Chroma / Vertex Vector Search adapters, this one:
    - Does NOT manage embeddings or chunking (handled by Vertex AI RAG Engine).
    - Uploads raw files to GCS and then imports them into the RAG Corpus.
    - Queries the RAG Corpus for retrieval.
    """

    def __init__(self):
        self.bucket_name = settings.GCS_BUCKET_NAME
        if not self.bucket_name:
            raise ValueError("GCS_BUCKET_NAME is not configured in settings.")
        self.gcs_prefix = object_name("rag")
        _init_vertexai()
        self.corpus_id = corpus_id("default", create=True)
        if not self.corpus_id:
            raise ValueError("Default RAG corpus is not bound to this workspace.")

        self.gcs_client = storage.Client(project=settings.GCP_PROJECT_ID)
        logger.info(
            "VertexRagAdapter initialized. Corpus: %s | Bucket: gs://%s/%s",
            self.corpus_id,
            self.bucket_name,
            self.gcs_prefix,
        )

    def get_or_create_user_corpus(self, user_email: str) -> str:
        """
        Retrieves or creates a dedicated RAG corpus for a specific user.
        This provides perfect data isolation for sensitive features like Meeting Transcripts,
        since Vertex AI RAG engine lacks item-level chunk filtering.
        """
        email_key = sha256(user_email.strip().lower().encode()).hexdigest()[:24]
        resource = corpus_id("rag", audience=f"meetings_user_{email_key}", create=True)
        assert resource is not None
        return resource

    # ─────────────────────────────────────────────────────────────
    # GCS helpers
    # ─────────────────────────────────────────────────────────────

    def upload_to_gcs(self, local_path: Path) -> str:
        """Upload a local file to GCS and return its gs:// URI."""
        blob_name = object_name("rag", safe_upload_name(local_path.name))
        bucket = self.gcs_client.bucket(self.bucket_name)
        blob = bucket.blob(blob_name)
        blob.upload_from_filename(str(local_path))
        gcs_uri = f"gs://{self.bucket_name}/{blob_name}"
        logger.info("Uploaded %s → %s", local_path, gcs_uri)
        return gcs_uri

    def import_gcs_uri_to_corpus(
        self, gcs_uri: str, target_corpus_id: str | None = None
    ) -> None:
        """Tell the RAG Corpus to import / re-index a GCS URI."""
        assert_own_uri(gcs_uri)
        target = require_bound_corpus_id(target_corpus_id or self.corpus_id)
        source_update = begin_cloud_source_update(
            target, gcs_uri, "upsert", client=self.gcs_client
        )
        import time

        max_retries = 20

        for attempt in range(max_retries):
            try:
                response = rag.import_files(
                    corpus_name=target,
                    paths=[gcs_uri],
                    chunk_size=512,  # tokens per chunk (reduced from 1024 for tighter context)
                    chunk_overlap=50,  # small overlap is sufficient at 512 tokens
                )
            except Exception as e:
                err_str = str(e).lower()
                if (
                    "other operations running" in err_str
                    or "operation ids are" in err_str
                ):
                    delay = min(15, 3 * (attempt + 1))
                    logger.warning(
                        "RAG import for %s blocked by running operation. Retrying in %ds (attempt %d/%d)",
                        gcs_uri,
                        delay,
                        attempt + 1,
                        max_retries,
                    )
                    time.sleep(delay)
                else:
                    fail_cloud_source_update(source_update)
                    logger.error("Failed to import %s into RAG corpus: %s", gcs_uri, e)
                    raise
            logger.info("RAG import triggered for %s: %s", gcs_uri, response)
            finish_cloud_source_update(
                target,
                gcs_uri,
                "upsert",
                source_update,
                client=self.gcs_client,
            )
            return

        fail_cloud_source_update(source_update)
        raise RuntimeError(
            f"Failed to import {gcs_uri} after {max_retries} retries due to concurrent operations."
        )

    # ─────────────────────────────────────────────────────────────
    # VectorStoreAdapter interface
    # ─────────────────────────────────────────────────────────────

    def upsert(self, chunks: list[Chunk]) -> UpsertResult:
        """
        This adapter does NOT chunk/embed manually.
        File ingestion goes through upload_to_gcs + import_gcs_uri_to_corpus.
        This method is here for interface compatibility but is effectively a no-op.
        """
        logger.debug(
            "VertexRagAdapter.upsert() called but ingestion is handled via GCS upload. "
            "Returning success with count=%d.",
            len(chunks),
        )
        # Count as success so calling code (processor.py) doesn't think it failed.
        return UpsertResult(success=True, count=len(chunks))

    def query(
        self,
        query_embedding: list[float],
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[RetrievedChunk]:
        """Not used directly — retrieval goes through query_rag_corpus()."""
        return []

    def query_rag_corpus(
        self, query_text: str, top_k: int = 5, target_corpus_id: str | None = None
    ) -> list[RetrievedChunk]:
        """
        Retrieve relevant context from the RAG Corpus using semantic search.
        Returns a list of RetrievedChunk objects.
        """
        target = require_bound_corpus_id(target_corpus_id or self.corpus_id)
        cloud_hits = query_cloud_resource(
            target,
            query_text,
            top_k=top_k,
            distance_threshold=settings.RAG_DISTANCE_THRESHOLD,
        )
        if cloud_hits is not None:
            return cloud_hits
        try:
            response = rag.retrieval_query(
                rag_resources=[rag.RagResource(rag_corpus=target)],
                text=query_text,
                similarity_top_k=top_k,
                # LOWER distance = stricter (only closer chunks). Shared with the
                # UC-1 path via settings; dropped 0.7→0.5 after the "Migration
                # Plan" hallucination (chat-hallucination RCA — Part B).
                vector_distance_threshold=settings.RAG_DISTANCE_THRESHOLD,
            )
            chunks = []
            for ctx in response.contexts.contexts:
                source_uri = getattr(ctx, "source_uri", "") or ""
                if source_uri:
                    try:
                        assert_own_uri(source_uri)
                    except ValueError:
                        logger.warning("Skipping RAG result with foreign source URI")
                        continue
                source_name = (
                    source_uri.split("/")[-1] if "/" in source_uri else source_uri
                )
                chunk = Chunk(
                    chunk_id=source_uri or f"rag-chunk-{len(chunks)}",
                    doc_id=source_uri or f"rag-doc-{len(chunks)}",
                    chunk_index=0,
                    text=ctx.text,
                    metadata={
                        "source_path": source_uri,
                        "original_filename": source_name,
                        "page": None,
                        "chunk_index": 0,
                    },
                )
                chunks.append(
                    RetrievedChunk(chunk=chunk, score=getattr(ctx, "score", 0.0))
                )
            logger.info("RAG retrieval completed with %d chunks", len(chunks))
            return chunks
        except Exception as e:
            logger.error("RAG retrieval query failed: %s", e)
            return []

    def delete(self, chunk_ids: list[str]) -> bool:
        """Deletion from RAG Corpus by chunk ID is not directly supported; no-op."""
        logger.warning(
            "VertexRagAdapter.delete() is not supported — files must be manually removed from the corpus."
        )
        return True

    def delete_document(
        self,
        doc_id: str,
        source_path: str | None = None,
        also_delete_gcs: bool = True,
        corpus_id: str | None = None,
    ) -> bool:
        """
        Delete a document from the Vertex AI RAG Corpus (and, by default, GCS).

        Set ``also_delete_gcs=False`` to keep the source object — used by the
        archive flow, which retains the original as a record.

        ``corpus_id`` targets a specific corpus other than the default
        (``self.corpus_id``) — e.g. transcripts live in
        ``company_meetings_corpus``, not ``VERTEX_RAG_CORPUS_ID`` (SOC 2 H-15).
        Only the single ragFile whose source matches ``source_path`` is removed;
        every other file in the corpus is left untouched.
        """
        if not source_path:
            logger.warning(
                "No source_path provided for VertexRagAdapter.delete_document; cannot delete from GCS or RAG directly."
            )
            return False
        assert_own_uri(source_path)
        target_corpus = require_bound_corpus_id(corpus_id or self.corpus_id)

        # Resolve and verify the exact ragFile before either backend is mutated.
        try:
            files = rag.list_files(corpus_name=target_corpus)
            target_file = None
            for rag_file in files:
                gcs_uris = list(
                    getattr(getattr(rag_file, "gcs_source", None), "uris", []) or []
                )
                if source_path in gcs_uris:
                    target_file = assert_own_rag_file(rag_file.name, target_corpus)
                    break
        except Exception as e:
            logger.error("Failed to verify %s in RAG Corpus: %s", source_path, e)
            return False

        if not target_file:
            from rubik_core.ai_providers.cloud_vector_index import CloudVectorIndexError
            from rubik_core.ai_providers.embedding_index import cloud_index_versions

            if any(cloud_index_versions(target_corpus)):
                raise CloudVectorIndexError(
                    "Vector source has no bound RAG file; repair is required"
                )
            logger.warning("Could not find file matching %s in RAG Corpus", source_path)
            return False

        source_update = begin_cloud_source_update(
            target_corpus,
            source_path,
            "delete",
            client=self.gcs_client,
        )

        # Remove the verified RAG file first. A failed RAG delete leaves the
        # source object available for a later retry.
        try:
            rag.delete_file(name=target_file)
            logger.info("Deleted %s from RAG Corpus", target_file)
        except Exception as e:
            fail_cloud_source_update(source_update)
            logger.error("Failed to delete %s from RAG Corpus: %s", source_path, e)
            return False

        finish_cloud_source_update(
            target_corpus,
            source_path,
            "delete",
            source_update,
            client=self.gcs_client,
        )

        if also_delete_gcs and source_path.startswith("gs://"):
            try:
                bucket_name, blob_name = source_path[len("gs://") :].split("/", 1)
                blob = self.gcs_client.bucket(bucket_name).blob(blob_name)
                if blob.exists():
                    blob.delete()
                    logger.info("Deleted %s from GCS", source_path)
            except Exception as e:
                logger.error("Failed to delete %s from GCS: %s", source_path, e)
                return False
        return True

    def reset(self) -> bool:
        """Reset is not implemented for the managed RAG Corpus."""
        logger.warning(
            "VertexRagAdapter.reset() is not supported for managed RAG Corpus."
        )
        return False

    def get_chunks_by_metadata(
        self, filters: dict[str, Any], limit: int = 10
    ) -> list[Chunk]:
        """Metadata-based lookup is not supported in RAG Corpus; return empty."""
        return []

    def get_status(self) -> dict[str, Any]:
        return {
            "backend": "VertexRagAdapter",
            "corpus": self.corpus_id,
            "ready": True,
        }

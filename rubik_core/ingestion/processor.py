from __future__ import annotations

# rubik_core/ingestion/processor.py
import logging
import time
from pathlib import Path
from typing import Any

from rubik_core.adapters.base import UpsertResult, VectorStoreAdapter
from rubik_core.ingestion.chunker import TokenAwareChunker
from rubik_core.ingestion.loader import DocumentLoader
from rubik_core.models import get_embeddings

logger = logging.getLogger(__name__)


class IngestionProcessor:
    def __init__(self, vector_store: VectorStoreAdapter):
        self.loader = DocumentLoader()
        self.chunker = TokenAwareChunker()
        self.vector_store = vector_store
        # Only load embeddings when NOT using VertexRagAdapter
        self._use_rag_adapter = self._is_rag_adapter(vector_store)
        if not self._use_rag_adapter:
            self.embeddings_model = get_embeddings()

    def _is_rag_adapter(self, adapter: VectorStoreAdapter) -> bool:
        """Check if the adapter is the managed Vertex AI RAG adapter."""
        return type(adapter).__name__ == "VertexRagAdapter"

    def process_directory(self, data_dir: str) -> None:
        path = Path(data_dir)
        total_upserted = 0

        files = list(path.rglob("*"))
        logger.info(f"Found {len(files)} items in {data_dir}")

        for file_path in files:
            if file_path.is_file() and file_path.suffix.lower() in [
                ".pdf",
                ".txt",
                ".md",
                ".docx",
            ]:
                res_dict = self.process_file(file_path)
                upsert_res = res_dict.get("result")
                if upsert_res and upsert_res.success:
                    total_upserted += upsert_res.count

        logger.info(f"Ingestion complete. Total chunks upserted: {total_upserted}")

    def process_file(
        self,
        file_path: Path,
        manual_metadata: dict[str, Any] | None = None,
        target_corpus_id: str | None = None,
    ) -> dict[str, Any]:
        logger.info(f"Processing file: {file_path}")

        # ── Vertex AI RAG Engine path (GCS upload + corpus import) ────────
        if self._use_rag_adapter:
            return self._process_file_via_rag(
                file_path, manual_metadata, target_corpus_id
            )

        # ── Legacy embedding path (Chroma / Vertex Vector Search) ─────────
        return self._process_file_via_embeddings(file_path, manual_metadata)

    # ─────────────────────────────────────────────────────────────
    # Managed RAG ingestion (no manual chunking / embedding)
    # ─────────────────────────────────────────────────────────────

    def _process_file_via_rag(
        self,
        file_path: Path,
        manual_metadata: dict[str, Any] | None = None,
        target_corpus_id: str | None = None,
    ) -> dict[str, Any]:
        """Upload file to GCS and import into the RAG Corpus."""
        # Load document to obtain a stable doc_id
        raw_doc = self.loader.load_file(file_path)
        if not raw_doc:
            return {
                "result": UpsertResult(success=False, count=0, error="Load failed"),
                "doc_id": None,
            }

        if manual_metadata:
            raw_doc.metadata.update(manual_metadata)

        try:
            # 1. Upload raw file to GCS
            gcs_uri = self.vector_store.upload_to_gcs(file_path)

            # 2. Import into the RAG Corpus (async job starts on Vertex side)
            self.vector_store.import_gcs_uri_to_corpus(
                gcs_uri, target_corpus_id=target_corpus_id
            )

            logger.info(
                "RAG ingestion complete for %s → %s (Corpus: %s)",
                file_path.name,
                gcs_uri,
                target_corpus_id or self.vector_store.corpus_id,
            )
            # We return count=1 to indicate the file was processed (not individual chunks)
            return {
                "result": UpsertResult(success=True, count=1),
                "doc_id": raw_doc.doc_id,
            }
        except Exception as e:
            logger.error(f"RAG ingestion failed for {file_path}: {e}")
            return {
                "result": UpsertResult(success=False, count=0, error=str(e)),
                "doc_id": raw_doc.doc_id,
            }

    # ─────────────────────────────────────────────────────────────
    # Legacy embedding ingestion (Chroma etc.)
    # ─────────────────────────────────────────────────────────────

    def _process_file_via_embeddings(
        self, file_path: Path, manual_metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        raw_doc = self.loader.load_file(file_path)
        if not raw_doc:
            return {
                "result": UpsertResult(success=False, count=0, error="Load failed"),
                "doc_id": None,
            }

        if manual_metadata:
            raw_doc.metadata.update(manual_metadata)

        chunks = self.chunker.chunk_document(raw_doc)
        if not chunks:
            return {
                "result": UpsertResult(
                    success=False, count=0, error="No chunks generated"
                ),
                "doc_id": raw_doc.doc_id,
            }

        # Batch embed chunks with manual retry logic for extra robustness during ingestion
        max_retries = 3
        base_delay = 2
        batch_size = 10  # Process 10 chunks at a time to stay under 20k token limit

        texts = [c.text for c in chunks]
        embeddings = []

        # Process in batches
        for i in range(0, len(texts), batch_size):
            batch_texts = texts[i : i + batch_size]
            batch_embeddings = None
            last_error = None

            for attempt in range(max_retries):
                try:
                    batch_embeddings = self.embeddings_model.embed_documents(
                        batch_texts
                    )
                    break
                except Exception as e:
                    last_error = e
                    if "429" in str(e) or "Resource exhausted" in str(e):
                        delay = base_delay * (2**attempt)
                        logger.warning(
                            f"Quota hit for {file_path} (batch {i // batch_size}). Retrying in {delay}s..."
                        )
                        time.sleep(delay)
                    else:
                        logger.error(
                            f"Non-retryable embedding error for {file_path}: {e}"
                        )
                        return {
                            "result": UpsertResult(
                                success=False, count=0, error=str(e)
                            ),
                            "doc_id": raw_doc.doc_id,
                        }

            if not batch_embeddings:
                logger.error(
                    f"Failed to embed batch starting at {i} for {file_path}: {last_error}"
                )
                return {
                    "result": UpsertResult(
                        success=False, count=0, error=f"Embedding failed: {last_error}"
                    ),
                    "doc_id": raw_doc.doc_id,
                }

            embeddings.extend(batch_embeddings)
            # Small delay between batches
            time.sleep(0.5)

        if len(embeddings) != len(chunks):
            return {
                "result": UpsertResult(
                    success=False, count=0, error="Mismatch in embedding count"
                ),
                "doc_id": raw_doc.doc_id,
            }

        try:
            for i, chunk in enumerate(chunks):
                chunk.embedding = embeddings[i]

            # Add a small delay between file processing to further reduce risk of 429
            time.sleep(1)

            from rubik_core.adapters.chroma import ChromaAdapter

            if isinstance(self.vector_store, ChromaAdapter):
                upsert_res = self.vector_store.upsert(
                    chunks,
                    embedding_spec=self.embeddings_model.spec,
                )
            else:
                upsert_res = self.vector_store.upsert(chunks)
            return {"result": upsert_res, "doc_id": raw_doc.doc_id}
        except Exception as e:
            logger.error(f"Upsert error for {file_path}: {e}")
            return {
                "result": UpsertResult(success=False, count=0, error=str(e)),
                "doc_id": raw_doc.doc_id,
            }

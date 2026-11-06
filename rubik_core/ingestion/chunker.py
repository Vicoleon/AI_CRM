# rubik_core/ingestion/chunker.py
import hashlib

import tiktoken
from langchain_text_splitters import RecursiveCharacterTextSplitter

from rubik_core.adapters.base import Chunk
from rubik_core.ingestion.loader import RawDocument


class TokenAwareChunker:
    def __init__(
        self,
        chunk_size: int = 1000,
        chunk_overlap: int = 100,
        model_name: str = "gpt-3.5-turbo",
    ):
        """
        Token-aware chunking using tiktoken to estimate token counts.
        Vertex embeddings often have token limits, so token-based splitting is safer.
        """
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

        try:
            try:
                self.encoder = tiktoken.encoding_for_model(model_name)
            except Exception:
                self.encoder = tiktoken.get_encoding("cl100k_base")
            self.splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
                encoding_name=self.encoder.name,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                separators=["\n\n", "\n", " ", ""],
            )
        except Exception:
            # tiktoken fetches its vocabulary on first use. Cloud jobs with no
            # public egress can still index conservatively sized text chunks.
            self.encoder = None
            self.splitter = RecursiveCharacterTextSplitter(
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                separators=["\n\n", "\n", " ", ""],
            )

    def _generate_chunk_id(self, doc_id: str, index: int, text: str) -> str:
        text_hash = hashlib.sha256(text.encode()).hexdigest()[:16]
        return f"{doc_id}_c{index}_{text_hash}"

    def chunk_document(self, doc: RawDocument) -> list[Chunk]:
        texts = self.splitter.split_text(doc.text)
        chunks = []

        for i, text in enumerate(texts):
            if not text.strip():
                continue

            chunk_id = self._generate_chunk_id(doc.doc_id, i, text)

            # Enrich metadata with chunk-specific info
            metadata = doc.metadata.copy()
            metadata["chunk_id"] = chunk_id
            metadata["chunk_index"] = i

            chunks.append(
                Chunk(
                    chunk_id=chunk_id,
                    doc_id=doc.doc_id,
                    chunk_index=i,
                    text=text.strip(),
                    metadata=metadata,
                )
            )

        return chunks

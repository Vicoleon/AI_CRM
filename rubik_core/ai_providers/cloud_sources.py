"""Read one bound GCS source for a workspace-owned vector index.

The source generation is pinned before download. A changed or unreadable file
blocks a candidate rebuild instead of producing an incomplete search index.
"""

from __future__ import annotations

import hashlib
import tempfile
from dataclasses import dataclass
from pathlib import Path

from google.cloud import storage

from rubik_core.adapters.base import Chunk
from rubik_core.config import settings
from rubik_core.ingestion.chunker import TokenAwareChunker
from rubik_core.ingestion.loader import DocumentLoader
from rubik_core.tenancy.storage import assert_own_uri

MAX_SOURCE_BYTES = 50 * 1024 * 1024
_SUPPORTED_SUFFIXES = {".pdf", ".docx", ".txt", ".md", ".pptx", ".json"}


class CloudSourceError(RuntimeError):
    """A source cannot be indexed without leaking its URI or contents."""


@dataclass(frozen=True)
class CloudSource:
    uri: str
    generation: int
    sha256: str
    chunks: list[Chunk]


def load_gcs_source(uri: str, *, client: storage.Client | None = None) -> CloudSource:
    """Download and chunk one current generation from this workspace's GCS path."""
    assert_own_uri(uri)
    try:
        bucket_name, object_name = uri[5:].split("/", 1)
    except ValueError:
        raise CloudSourceError("Vector source URI is invalid") from None
    suffix = Path(object_name).suffix.lower()
    if suffix not in _SUPPORTED_SUFFIXES:
        raise CloudSourceError("Vector source type is unsupported")
    try:
        gcs = client or storage.Client(project=settings.GCP_PROJECT_ID)
        blob = gcs.bucket(bucket_name).blob(object_name)
        blob.reload()
        generation = int(blob.generation)
        size = int(blob.size)
        if size <= 0 or size > MAX_SOURCE_BYTES:
            raise CloudSourceError("Vector source size is unsupported")
        payload = blob.download_as_bytes(if_generation_match=generation)
    except CloudSourceError:
        raise
    except Exception:
        raise CloudSourceError("Vector source could not be downloaded") from None
    if not isinstance(payload, bytes) or len(payload) != size:
        raise CloudSourceError("Vector source changed during download")

    digest = hashlib.sha256(payload).hexdigest()
    doc_id = hashlib.sha256(f"{uri}:{generation}:{digest}".encode()).hexdigest()
    try:
        with tempfile.TemporaryDirectory(prefix="cubie-vector-") as temporary:
            path = Path(temporary) / f"source{suffix}"
            path.write_bytes(payload)
            document = DocumentLoader().load_file(path)
            if document is None:
                raise CloudSourceError("Vector source has no readable text")
            document.doc_id = doc_id
            document.metadata.update(
                {
                    "doc_id": doc_id,
                    "source_path": uri,
                    "original_filename": Path(object_name).name,
                    "gcs_generation": generation,
                }
            )
            chunks = TokenAwareChunker(chunk_size=512, chunk_overlap=50).chunk_document(
                document
            )
    except CloudSourceError:
        raise
    except Exception:
        raise CloudSourceError("Vector source could not be parsed") from None
    if not chunks:
        raise CloudSourceError("Vector source has no searchable text")
    return CloudSource(uri, generation, digest, chunks)

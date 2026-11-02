from __future__ import annotations

"""Workspace-scoped vector-store helpers for DAM assets.

Centralizes the "remove this doc's chunks from the active vector store"
operation so the delete / archive / replace paths in the DAM and assets
routers don't each re-implement adapter selection.

GCP routing is the load-bearing part. DAM uploads (dam.py) are imported into
department-specific UC-1 corpora ({slug}_rag / companywide_rag); but Drive
"add to assets" and the legacy /api/ingest path import into the DEFAULT corpus
(VERTEX_RAG_CORPUS_ID). So a purge must try the department corpus the asset's
routing points to AND fall back to the default corpus — otherwise an
"archived"/"deleted" doc could keep answering in Chat. Locally (Chroma) the
adapter keys off the doc_id metadata, so a single call is enough.
"""

import logging

from rubik_core.config import AppMode, settings

logger = logging.getLogger(__name__)


def delete_document_vectors(
    doc_id: str,
    source_path: str | None = None,
    *,
    department: str | None = None,
    is_company_wide: bool = False,
    also_delete_gcs: bool = True,
) -> bool:
    """Remove a document's chunks from the active vector store.

    ``also_delete_gcs=False`` keeps the source object (archive retains it as a
    record). Returns True if the document's chunks were actually removed from a
    corpus (local mode always returns True — the Chroma adapter keys off the
    doc_id). In GCP, returns False when the doc was found in neither the
    department corpus nor the default corpus, or the source_path is missing —
    callers needing an integrity guarantee (archive) treat False as a failure
    and refuse to mark the doc archived. Raises on a real backend error.
    """
    if settings.APP_MODE != AppMode.GCP:
        from rubik_core.adapters.chroma import ChromaAdapter

        adapter = ChromaAdapter()
        if hasattr(adapter, "delete_document"):
            adapter.delete_document(doc_id, source_path=source_path)
        return True

    from rubik_core.tenancy.storage import assert_own_uri

    if source_path:
        assert_own_uri(source_path)

    # GCP: try the department/companywide corpus the asset's routing points to,
    # mirroring ingest-time routing (FileProcessor.resolve_corpus_display_name).
    from rubik_core.rag.config import (
        COMPANYWIDE_SLUG,
        current_registry,
        slugify_department,
    )

    slug: str | None
    if is_company_wide:
        slug = COMPANYWIDE_SLUG
    elif department:
        slug = slugify_department(department)
    else:
        slug = None

    registry = current_registry()
    if slug and source_path and registry.get(slug) is not None:
        from rubik_core.rag.manager import RagManager

        result = RagManager().delete_document(
            slug, gcs_uri=source_path, also_delete_gcs=also_delete_gcs
        )
        if result.get("rag_deleted"):
            return True
        # Not in the department corpus — Drive / legacy /api/ingest uploads land
        # in the default corpus, so fall through to it before giving up.
        logger.info(
            "delete_document_vectors: %s not in %s_rag; trying the default corpus",
            doc_id,
            slug,
        )

    # Workspace default corpus fallback — reaches Drive /
    # /api/ingest docs that never got department-routed. Propagate the adapter's
    # outcome (it returns False on not-found / missing source_path / backend
    # failure) so archive's integrity check stays meaningful.
    from rubik_core.adapters.vertex_rag import VertexRagAdapter

    adapter = VertexRagAdapter()
    if hasattr(adapter, "delete_document"):
        return adapter.delete_document(
            doc_id, source_path=source_path, also_delete_gcs=also_delete_gcs
        )
    return False

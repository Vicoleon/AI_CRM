"""Fail closed unless Vertex confirms the one requested RAG file was imported."""

from __future__ import annotations


class RagImportRejected(RuntimeError):
    """Vertex returned without confirming the requested file was accepted."""


def require_one_imported(response: object) -> None:
    """A normal RPC return can still report a failed or skipped file."""
    try:
        imported = response.imported_rag_files_count
        failed = response.failed_rag_files_count
        skipped = response.skipped_rag_files_count
    except (AttributeError, TypeError):
        raise RagImportRejected("RAG import result is unavailable") from None
    if (
        type(imported) is not int
        or type(failed) is not int
        or type(skipped) is not int
        or (imported, failed, skipped) != (1, 0, 0)
    ):
        raise RagImportRejected("RAG did not confirm one imported file")

"""Upload `EosDoc`s to GCS and import them into Cubie's Vertex AI corpus.

Most EOS sources reuse `VertexRagAdapter` (the shared-corpus client) rather
than introducing a parallel Vertex client. The adapter is the single source
of truth for corpus configuration, GCS bucket, and retry semantics on
concurrent import operations.

The VTO is the exception: it's confidential / Exec-only, so it routes
through the slug-aware `RagManager` into the dedicated `vto` corpus instead of
the shared one — see `ingest_docs(..., target_rag_slug=...)`.

MVP behavior:
- Each EosDoc → one local tempfile → one GCS upload → one
  `rag.import_files` call.
- Delete-then-import: Vertex creates a NEW ragFile on every
  import, so before importing a doc we delete any existing ragFile in the
  target corpus whose source basename matches the doc's deterministic
  filename. A re-ingest therefore REPLACES the prior text instead of
  leaving stale chunks alongside it, and duplicates accumulated before
  this fix are swept out the same way.
- No batching — Vertex's `import_files` accepts multiple paths in one
  call, but per-doc imports keep failure isolation per-record (one bad
  file doesn't kill the batch).

Failure mode:
- Each doc is independent. We log per-doc success / failure and return
  an `IngestResult`. The caller decides what to do; the weekly cron
  treats partial failure as a soft warning, the event hooks treat it as
  best-effort.

Local mode (no Vertex):
- When `settings.APP_MODE != AppMode.GCP`, we no-op and log a stub
  result. EOS ingestion isn't useful in local-dev SQLite mode.
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from rubik_core.config import AppMode, settings
from rubik_core.ingestion.eos.extractor import (
    EosDoc,
    extract_all,
    extract_issues,
    extract_l10,
    extract_processes,
    extract_rocks,
    extract_vto,
)
from rubik_core.rag.config import EOS_EXEC_SLUG, VTO_SLUG

logger = logging.getLogger(__name__)


@dataclass
class IngestResult:
    """Per-call summary returned to admin/scheduler callers and event hooks."""

    succeeded: list[str] = field(default_factory=list)  # filenames
    failed: list[tuple[str, str]] = field(default_factory=list)  # (filename, error)
    skipped_reason: str | None = (
        None  # set when ingestion was a no-op (e.g. local mode)
    )

    @property
    def total(self) -> int:
        return len(self.succeeded) + len(self.failed)

    def summary(self) -> str:
        if self.skipped_reason:
            return f"skipped ({self.skipped_reason})"
        return f"{len(self.succeeded)} succeeded, {len(self.failed)} failed"


def _is_vertex_mode() -> bool:
    """We only ingest into Vertex when running in GCP mode with a bucket.
    The adapter resolves the workspace-bound corpus on first use. Local/dev
    modes (SQLite + Chroma) are not wired
    for EOS RAG. Kill-switch `CUBIE_DISABLE_EOS_INGEST` lets ops bypass
    ingestion without changing APP_MODE (useful for migrations + repairs)."""
    if settings.CUBIE_DISABLE_EOS_INGEST:
        return False
    return settings.APP_MODE == AppMode.GCP and bool(settings.GCS_BUCKET_NAME)


def _basename(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


def _existing_ragfiles_by_basename(
    corpus_name: str,
) -> dict[str, list[tuple[str, str]]]:
    """Map source-file basename → ragFile resource names for one corpus.

    Built ONCE per ingest run (``rag.list_files`` is a full-corpus scan) and
    consumed per-doc by :func:`_purge_stale_ragfiles`. Returns ``{}`` on any
    failure — stale-chunk cleanup is best-effort and must never block
    ingestion.
    """
    try:
        from vertexai.preview import rag

        from rubik_core.adapters.vertex_rag import _init_vertexai
        from rubik_core.tenancy.corpora import (
            assert_own_rag_file,
            require_bound_corpus_id,
        )
        from rubik_core.tenancy.storage import assert_own_uri

        # Init to VERTEX_RAG_LOCATION (where the corpora live) — the default
        # GCP_REGION 400s on a region mismatch. Idempotent global.
        _init_vertexai()
        require_bound_corpus_id(corpus_name)

        index: dict[str, list[tuple[str, str]]] = {}
        for f in rag.list_files(corpus_name=corpus_name):
            assert_own_rag_file(f.name, corpus_name)
            uris = list(getattr(getattr(f, "gcs_source", None), "uris", []) or [])
            for uri in uris:
                assert_own_uri(uri)
                index.setdefault(_basename(uri), []).append((f.name, uri))
        return index
    except Exception:  # noqa: BLE001
        from rubik_core.ai_providers.embedding_index import cloud_index_versions

        if any(cloud_index_versions(corpus_name)):
            raise
        logger.exception(
            "[eos-ingest] could not list existing ragFiles for %s — "
            "re-ingest will import without purging (stale chunks may remain)",
            corpus_name,
        )
        return {}


def _purge_stale_ragfiles(
    index: dict[str, list[tuple[str, str]]], filename: str, corpus_name: str | None
) -> None:
    """Stale-chunk fix: the delete half of delete-then-import.

    EOS docs have deterministic filenames (``eos__<kind>__<id>.md``), but
    Vertex ``import_files`` creates a NEW ragFile every time — re-ingesting an
    edited doc left the pre-edit text alive in retrieval. Deleting every
    pre-existing ragFile whose source basename matches BEFORE importing makes
    a re-ingest a replace; multiple matches (duplicates accumulated before
    this fix) are all removed, so the corpus self-heals over time.

    Best-effort per file: a failed delete is logged and the import still
    proceeds (the fresh content wins retrieval-wise; the next re-ingest
    retries the purge).
    """
    stale = index.pop(filename, [])
    if not stale:
        return
    from vertexai.preview import rag

    from rubik_core.ai_providers.cloud_rag_bridge import (
        begin_cloud_source_update,
        fail_cloud_source_update,
        finish_cloud_source_update,
    )
    from rubik_core.ai_providers.embedding_index import cloud_index_versions
    from rubik_core.tenancy.corpora import assert_own_rag_file

    for name, uri in stale:
        update = None
        try:
            if corpus_name is None:
                raise ValueError("A bound corpus is required")
            assert_own_rag_file(name, corpus_name)
            update = begin_cloud_source_update(corpus_name, uri, "delete")
            rag.delete_file(name=name)
            finish_cloud_source_update(corpus_name, uri, "delete", update)
            logger.info(
                "[eos-ingest] purged stale ragFile %s (%s) from %s",
                name,
                filename,
                corpus_name,
            )
        except Exception:  # noqa: BLE001
            fail_cloud_source_update(update)
            if corpus_name is not None and any(cloud_index_versions(corpus_name)):
                raise
            logger.warning(
                "[eos-ingest] could not purge stale ragFile %s (%s) — "
                "importing anyway; a duplicate may remain until the next "
                "re-ingest",
                name,
                filename,
                exc_info=True,
            )


def ingest_docs(
    docs: list[EosDoc], *, target_rag_slug: str | None = None
) -> IngestResult:
    """Upload and import a pre-extracted list of EosDocs. Used both by
    the full-sweep cron path (which extracts everything first) and by
    event-driven hooks that already have one or two docs in hand.

    By default docs land in the workspace's bound default corpus via
    `VertexRagAdapter`. Pass `target_rag_slug` to route them into a specific
    audience-shaped RAG corpus through `RagManager` instead — used for the
    confidential VTO, which must live in its Exec-only corpus, never
    the shared corpus every employee's chat queries.
    """
    result = IngestResult()
    if not docs:
        result.skipped_reason = "no docs"
        return result

    if not _is_vertex_mode():
        result.skipped_reason = "not in GCP mode (or no GCS bucket)"
        logger.info(
            "[eos-ingest] %s — skipping %d docs", result.skipped_reason, len(docs)
        )
        return result

    # Pick the import sink up front. Lazy imports avoid pulling the Vertex SDK
    # during local test discovery.
    if target_rag_slug:
        # Audience-restricted corpus (e.g. the Exec-only `vto`): go through the
        # slug-aware RagManager, which uploads under rags/<slug>/ and
        # creates the corpus on first use.
        from rubik_core.rag import RagManager

        mgr = RagManager()

        def _import_one(local_path: Path) -> str:
            return mgr.add_document(target_rag_slug, local_path)["gcs_uri"]

        # For the stale purge only: resolve WITHOUT creating — a corpus that
        # doesn't exist yet has nothing to purge (add_document creates it).
        try:
            corpus_name = mgr._resolve_corpus(target_rag_slug, create_if_missing=False)
        except Exception:  # noqa: BLE001 — first use of this corpus
            corpus_name = None
    else:
        from rubik_core.adapters.vertex_rag import VertexRagAdapter

        adapter = VertexRagAdapter()

        def _import_one(local_path: Path) -> str:
            gcs_uri = adapter.upload_to_gcs(local_path)
            adapter.import_gcs_uri_to_corpus(gcs_uri)
            return gcs_uri

        corpus_name = adapter.corpus_id

    # Delete-then-import: list the target corpus ONCE per run, then purge
    # each doc's pre-existing ragFiles just before importing its fresh copy.
    stale_index = _existing_ragfiles_by_basename(corpus_name) if corpus_name else {}

    with tempfile.TemporaryDirectory(prefix="eos-ingest-") as td:
        td_path = Path(td)
        for doc in docs:
            local_path = td_path / doc.filename
            try:
                local_path.write_text(doc.text, encoding="utf-8")
                _purge_stale_ragfiles(stale_index, doc.filename, corpus_name)
                gcs_uri = _import_one(local_path)
                result.succeeded.append(doc.filename)
                logger.info(
                    "[eos-ingest] %s → %s%s",
                    doc.filename,
                    gcs_uri,
                    f" (rag={target_rag_slug})" if target_rag_slug else "",
                )
            except Exception as e:
                result.failed.append((doc.filename, f"{type(e).__name__}: {e}"))
                logger.exception("[eos-ingest] failed for %s", doc.filename)

    logger.info("[eos-ingest] complete: %s", result.summary())
    return result


# ─── Convenience per-source entry points used by event hooks ───────────


def _ingest_eos_split(docs: list[EosDoc]) -> IngestResult:
    """Route each EOS doc to the correct corpus by audience:

    - VTO sections             → the exec-only ``vto`` corpus
    - Exec-team-tagged content → the exec-only ``eos_exec`` corpus (``is_exec``)
    - everything else          → the shared corpus every employee's chat reads

    Only Exec-team members' RAG queries include the exec-only slugs
    (``rags_for_user_email`` + ``EXEC_ONLY_SLUGS``), so exec content can never be
    retrieved by a non-exec — in chat or any other RAG consumer.
    """
    buckets: dict[str | None, list[EosDoc]] = {}
    for d in docs:
        if getattr(d, "kind", None) == "vto":
            slug: str | None = VTO_SLUG
        elif getattr(d, "is_exec", False):
            slug = EOS_EXEC_SLUG
        else:
            slug = None  # shared corpus
        buckets.setdefault(slug, []).append(d)

    combined = IngestResult()
    for slug, subset in buckets.items():
        r = ingest_docs(subset, target_rag_slug=slug)
        combined.succeeded.extend(r.succeeded)
        combined.failed.extend(r.failed)
    if not combined.succeeded and not combined.failed:
        combined.skipped_reason = "no docs"
    return combined


def ingest_vto(db: Session) -> IngestResult:
    # The VTO is confidential / Exec-only — route it into the dedicated
    # `vto` RAG corpus, never the shared corpus that every employee's chat reads.
    return ingest_docs(extract_vto(db), target_rag_slug=VTO_SLUG)


def ingest_rocks(db: Session) -> IngestResult:
    return _ingest_eos_split(extract_rocks(db))


def ingest_issues(db: Session, only_solved: bool = False) -> IngestResult:
    return _ingest_eos_split(extract_issues(db, only_solved=only_solved))


def ingest_processes(db: Session) -> IngestResult:
    return ingest_docs(extract_processes(db))


def ingest_l10_session(db: Session, session_id: str) -> IngestResult:
    return _ingest_eos_split(extract_l10(db, session_id=session_id))


# ─── Full sweep (weekly cron) ───────────────────────────────────────────


def ingest_all_sources(db: Session) -> IngestResult:
    """Re-ingest every EOS source. The weekly cron calls this as a
    backstop in case any event-driven trigger silently missed.

    Returns a single combined result; per-source counts are in the
    logs (filename prefixes `eos__<kind>__` make this trivial to grep)."""
    return _ingest_eos_split(extract_all(db))

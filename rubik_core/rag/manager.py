from __future__ import annotations

"""
RagManager — CRUD wrapper around the audience-shaped Vertex AI RAG corpora.

There is one Vertex RAG corpus per workspace and slug in the live registry:
    - the literal `companywide` RAG, and
    - one RAG per row in the `departments` table.

This module is the only place in the codebase that should call
`vertexai.preview.rag` write operations for these corpora; routers and
scripts go through it so error handling and logging stay uniform.

GCP resources touched
---------------------
- google.cloud.storage              (gs://<bucket>/workspaces/<workspace>/rags/<slug>/<file>)
- vertexai.preview.rag.create_corpus / list_corpora / list_files
- vertexai.preview.rag.import_files / delete_file / retrieval_query

Every GCP write is wrapped in try/except with structured logging. Real
failures raise `RagOperationError`; a multi-corpus query against a registry
where none of the requested RAGs have a corpus yet raises `NoUC1CorporaError`
so callers can fall back cleanly.
"""

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import vertexai
from google.cloud import storage
from vertexai.preview import rag as vertex_rag

from rubik_core.ai_providers.cloud_rag_bridge import (
    begin_cloud_source_update,
    fail_cloud_source_update,
    finish_cloud_source_update,
    query_cloud_resource,
)
from rubik_core.ai_providers.cloud_vector_index import CloudVectorIndexError
from rubik_core.config import settings
from rubik_core.rag.config import (
    COMPANYWIDE_SLUG,
    RagSpec,
    build_registry,
    current_registry,
    is_exec_only_slug,
)
from rubik_core.tenancy.context import current_workspace
from rubik_core.tenancy.corpora import (
    assert_own_rag_file,
    corpus_id,
    require_bound_corpus_id,
)
from rubik_core.tenancy.storage import assert_own_uri, object_name, safe_upload_name

logger = logging.getLogger(__name__)


class RagOperationError(RuntimeError):
    """Raised when a Vertex AI RAG or GCS operation against a RAG corpus fails."""


class NoUC1CorporaError(RagOperationError):
    """Raised by `query` when none of the requested RAG corpora exist yet.

    Callers can catch this to fall back to a legacy retrieval path while
    still treating real failures (`RagOperationError`) as errors.
    """


class MissingWorkspaceCorpus(RagOperationError):
    """The selected workspace has no control binding for this RAG yet."""


@dataclass
class RagDocument:
    """One document inside a RAG corpus, as returned by `list_documents`."""

    rag_file_name: str  # full Vertex resource name (projects/.../ragFiles/123)
    display_name: str
    gcs_uri: str | None
    create_time: str | None


@dataclass
class RagQueryHit:
    """One retrieval hit attributed to its source RAG slug."""

    rag: str  # slug of the source RAG, or "unknown" if attribution failed
    text: str
    source_uri: str
    score: float


def _attribute_rag_from_uri(source_uri: str, registry: dict[str, RagSpec]) -> str:
    """Map a `gs://.../rags/<slug>/...` URI back to the matching RAG slug.

    Returns "unknown" if the URI doesn't sit under one of the registry's
    `gcs_subprefix` paths.
    """
    if not source_uri:
        return "unknown"
    for spec in registry.values():
        if f"/{spec.gcs_subprefix}/" in source_uri:
            return spec.slug
    return "unknown"


_VERTEX_INITIALIZED = False


def _init_vertexai() -> None:
    """Idempotently call `vertexai.init` using the settings module."""
    global _VERTEX_INITIALIZED
    if _VERTEX_INITIALIZED:
        return
    if not settings.GCP_PROJECT_ID:
        raise RagOperationError(
            "GCP_PROJECT_ID is not configured; RAG operations require GCP mode."
        )
    vertexai.init(
        project=settings.GCP_PROJECT_ID,
        location=settings.VERTEX_RAG_LOCATION,
    )
    _VERTEX_INITIALIZED = True


class RagManager:
    """
    CRUD facade for the audience-shaped RAG corpora.

    The registry is loaded from the live `departments` table at construction
    time (overridable for tests). Vertex client init is deferred to first use,
    so a `RagManager()` is cheap to construct.

    GCP resource: one Vertex AI RAG corpus per slug, one GCS sub-prefix per slug.
    """

    def __init__(
        self,
        gcs_client: storage.Client | None = None,
        *,
        registry: dict[str, RagSpec] | None = None,
    ):
        _init_vertexai()
        if not settings.GCS_BUCKET_NAME:
            raise RagOperationError(
                "GCS_BUCKET_NAME is not configured; RAG operations require a bucket."
            )
        self.bucket_name = settings.GCS_BUCKET_NAME
        self.workspace_slug = current_workspace()
        self.base_prefix = object_name("rags")
        self.gcs_client = gcs_client or storage.Client(project=settings.GCP_PROJECT_ID)
        self.registry: dict[str, RagSpec] = (
            registry if registry is not None else current_registry()
        )
        logger.info(
            "RagManager initialized | project=%s | bucket=gs://%s/%s | rags=%s",
            settings.GCP_PROJECT_ID,
            self.bucket_name,
            self.base_prefix,
            sorted(self.registry),
        )

    # ─────────────────────────────────────────────────────────────────────
    # Registry helpers
    # ─────────────────────────────────────────────────────────────────────

    def known_slugs(self) -> list[str]:
        """Slugs currently in the registry (companywide + departments at construction time)."""
        return sorted(self.registry)

    def _spec(self, slug: str) -> RagSpec:
        if current_workspace() != self.workspace_slug:
            raise PermissionError("RAG manager belongs to another workspace")
        if slug not in self.registry:
            raise RagOperationError(
                f"Unknown RAG slug {slug!r}. Known: {sorted(self.registry)}"
            )
        return self.registry[slug]

    def refresh_registry(self) -> None:
        """Re-read the departments table and rebuild the registry in place.

        Corpus resources are always resolved from the live control binding.
        """
        self.registry = current_registry()
        logger.info("RagManager registry refreshed | rags=%s", sorted(self.registry))

    # ─────────────────────────────────────────────────────────────────────
    # Corpus resolution
    # ─────────────────────────────────────────────────────────────────────

    def _resolve_corpus(self, slug: str, *, create_if_missing: bool = True) -> str:
        """
        Return the full Vertex resource name for a slug's corpus, creating it
        if missing (and `create_if_missing` is True).

        The resource ID is bound in the selected workspace's control row.
        """
        spec = self._spec(slug)
        try:
            if spec.slug in (COMPANYWIDE_SLUG, "vto", "eos_exec"):
                resource = corpus_id(spec.display_name, create=create_if_missing)
            else:
                resource = corpus_id(
                    "rag", audience=spec.slug, create=create_if_missing
                )
        except Exception as e:
            raise RagOperationError(
                f"Corpus resolution failed for {spec.slug}: {e}"
            ) from e
        if not resource:
            raise MissingWorkspaceCorpus(f"Corpus for RAG {spec.slug!r} is not bound")
        return resource

    def ensure_corpus(self, slug: str) -> str:
        """Create one corpus if missing — used by the department-create hook."""
        return self._resolve_corpus(slug, create_if_missing=True)

    def ensure_all_corpora(self) -> dict[str, str]:
        """Create any missing corpora across the entire registry."""
        return {slug: self._resolve_corpus(slug) for slug in self.registry}

    # ─────────────────────────────────────────────────────────────────────
    # GCS helpers
    # ─────────────────────────────────────────────────────────────────────

    def _gcs_blob_name(self, spec: RagSpec, file_name: str) -> str:
        """Compose the GCS blob path: `<prefix>/<rag-subprefix>/<file>`."""
        return object_name("rags", spec.slug, safe_upload_name(file_name))

    def _upload_to_gcs(self, spec: RagSpec, local_path: Path) -> str:
        """Upload one file to the RAG's GCS sub-prefix; return its gs:// URI."""
        blob_name = self._gcs_blob_name(spec, local_path.name)
        try:
            bucket = self.gcs_client.bucket(self.bucket_name)
            blob = bucket.blob(blob_name)
            blob.upload_from_filename(str(local_path))
        except Exception as e:
            logger.exception(
                "GCS upload failed | rag=%s | local=%s | blob=%s",
                spec.slug,
                local_path,
                blob_name,
            )
            raise RagOperationError(
                f"GCS upload failed for RAG {spec.slug}: {e}"
            ) from e
        gcs_uri = f"gs://{self.bucket_name}/{blob_name}"
        logger.info("Uploaded %s → %s (rag=%s)", local_path, gcs_uri, spec.slug)
        return gcs_uri

    def _delete_from_gcs(self, gcs_uri: str) -> bool:
        """Delete an object from GCS. Idempotent — missing blob is logged, not raised."""
        assert_own_uri(gcs_uri)
        if not gcs_uri.startswith("gs://"):
            logger.warning("Skipping non-gs:// URI in _delete_from_gcs: %s", gcs_uri)
            return False
        path = gcs_uri[len("gs://") :]
        bucket_name, _, blob_name = path.partition("/")
        if not blob_name:
            logger.warning("Malformed gs:// URI (no blob): %s", gcs_uri)
            return False
        try:
            bucket = self.gcs_client.bucket(bucket_name)
            blob = bucket.blob(blob_name)
            if not blob.exists():
                logger.warning(
                    "GCS blob %s already absent (treating as success)", gcs_uri
                )
                return True
            blob.delete()
            logger.info("Deleted GCS blob %s", gcs_uri)
            return True
        except Exception as e:
            logger.exception("GCS delete failed for %s", gcs_uri)
            raise RagOperationError(f"GCS delete failed for {gcs_uri}: {e}") from e

    # ─────────────────────────────────────────────────────────────────────
    # RAG corpus operations
    # ─────────────────────────────────────────────────────────────────────

    def _import_to_corpus(self, corpus_name: str, gcs_uri: str) -> None:
        require_bound_corpus_id(corpus_name)
        assert_own_uri(gcs_uri)
        """Import a GCS URI into a Vertex RAG corpus, retrying around the
        Vertex "other operations running" precondition error."""
        source_update = begin_cloud_source_update(
            corpus_name, gcs_uri, "upsert", client=self.gcs_client
        )
        max_retries = 20
        for attempt in range(max_retries):
            try:
                response = vertex_rag.import_files(
                    corpus_name=corpus_name,
                    paths=[gcs_uri],
                    chunk_size=512,
                    chunk_overlap=50,
                )
            except Exception as e:
                err_str = str(e).lower()
                # Vertex throws this when another long-running op is in flight on
                # the same corpus; same pattern as adapters/vertex_rag.py:108.
                transient = (
                    "other operations running" in err_str
                    or "operation ids are" in err_str
                )
                if transient and attempt < max_retries - 1:
                    delay = min(15, 3 * (attempt + 1))
                    logger.warning(
                        "RAG import blocked by concurrent op | corpus=%s | uri=%s | "
                        "retry in %ds (attempt %d/%d)",
                        corpus_name,
                        gcs_uri,
                        delay,
                        attempt + 1,
                        max_retries,
                    )
                    time.sleep(delay)
                    continue
                logger.exception(
                    "RAG import failed | corpus=%s | uri=%s", corpus_name, gcs_uri
                )
                fail_cloud_source_update(source_update)
                raise RagOperationError(
                    f"import_files failed for {gcs_uri}: {e}"
                ) from e
            logger.info(
                "RAG import OK | corpus=%s | uri=%s | response=%s",
                corpus_name,
                gcs_uri,
                response,
            )
            finish_cloud_source_update(
                corpus_name,
                gcs_uri,
                "upsert",
                source_update,
                client=self.gcs_client,
            )
            return
        fail_cloud_source_update(source_update)
        raise RagOperationError(
            f"import_files exhausted retries for {gcs_uri} on {corpus_name}"
        )

    # ─────────────────────────────────────────────────────────────────────
    # Retrieval (read path)
    # ─────────────────────────────────────────────────────────────────────

    def query(
        self,
        query_text: str,
        *,
        rags: list[str] | None = None,
        top_k: int = 5,
        distance_threshold: float | None = None,
    ) -> list[RagQueryHit]:
        """
        Run one `retrieval_query` across multiple RAG corpora and return chunks
        tagged with their source slug.

        GCP resource: `vertexai.preview.rag.retrieval_query` with multiple
        `RagResource` entries (single network call, server-side fan-out).

        Raises:
            NoUC1CorporaError: None of the requested RAGs have a corpus yet.
            RagOperationError: The retrieval call itself failed.

        Returns:
            A list of `RagQueryHit`. Empty list = corpora exist but matched
            nothing — do NOT fall back; that is a real "no answer."
        """
        # Default to the configured relevance threshold (lower = stricter). See
        # settings.RAG_DISTANCE_THRESHOLD (chat-hallucination RCA — Part B).
        if distance_threshold is None:
            distance_threshold = settings.RAG_DISTANCE_THRESHOLD

        if rags is None:
            # SOC 2 H-14: the default fan-out must NEVER include the exec-only
            # corpora (vto, eos_exec). A caller wanting exec content must pass
            # explicit slugs from rags_for_user_email, which returns exec slugs
            # ONLY to exec/admin users. Central backstop so an ungated caller
            # can't accidentally leak Exec-team content.
            targets = [s for s in self.registry if not is_exec_only_slug(s)]
        else:
            # Validate every requested slug — a typo at the call site is a
            # bug, not an empty result.
            for slug in rags:
                self._spec(slug)
            targets = list(rags)

        resources: list = []
        selected: list[tuple[str, str]] = []
        cloud_groups: list[list[RagQueryHit]] = []
        for slug in targets:
            try:
                corpus_name = self._resolve_corpus(slug, create_if_missing=False)
            except MissingWorkspaceCorpus:
                logger.debug("query: skipping rag=%s (corpus does not exist)", slug)
                continue
            selected.append((slug, corpus_name))

        managed_hits: list[RagQueryHit] = []
        for slug, resource in selected:
            cloud_hits = query_cloud_resource(
                resource,
                query_text,
                top_k=top_k,
                distance_threshold=distance_threshold,
            )
            if cloud_hits is None:
                resources.append(vertex_rag.RagResource(rag_corpus=resource))
                continue
            cloud_group: list[RagQueryHit] = []
            for hit in cloud_hits:
                source_uri = hit.chunk.metadata.get("source_path", "")
                if not source_uri:
                    raise CloudVectorIndexError("Vector hit has no source URI")
                try:
                    assert_own_uri(source_uri)
                except ValueError:
                    raise CloudVectorIndexError(
                        "Vector hit is outside this workspace"
                    ) from None
                cloud_group.append(
                    RagQueryHit(
                        rag=slug,
                        text=hit.chunk.text,
                        source_uri=source_uri,
                        score=hit.score,
                    )
                )
            cloud_groups.append(cloud_group)

        if not selected:
            raise NoUC1CorporaError(
                f"No RAG corpora exist for {targets} — run /api/rag/bootstrap first."
            )

        if resources:
            try:
                response = vertex_rag.retrieval_query(
                    rag_resources=resources,
                    text=query_text,
                    similarity_top_k=top_k,
                    vector_distance_threshold=distance_threshold,
                )
            except Exception as e:
                logger.exception("retrieval_query failed | rags=%s", targets)
                raise RagOperationError(f"retrieval_query failed: {e}") from e
            for ctx in response.contexts.contexts:
                source_uri = getattr(ctx, "source_uri", "") or ""
                if source_uri:
                    try:
                        assert_own_uri(source_uri)
                    except ValueError:
                        logger.warning("RAG returned a source outside the workspace")
                        continue
                managed_hits.append(
                    RagQueryHit(
                        rag=_attribute_rag_from_uri(source_uri, self.registry),
                        text=ctx.text,
                        source_uri=source_uri,
                        score=float(getattr(ctx, "score", 0.0) or 0.0),
                    )
                )
        if cloud_groups:
            # Vertex and pgvector scores do not share a calibrated scale.
            # Interleave each backend's ranked list instead of comparing them.
            groups = cloud_groups + ([managed_hits] if managed_hits else [])
            out: list[RagQueryHit] = []
            for rank in range(top_k):
                for group in groups:
                    if rank < len(group):
                        out.append(group[rank])
                        if len(out) == top_k:
                            break
                if len(out) == top_k:
                    break
        else:
            out = managed_hits
        # Log the raw score range alongside the threshold so the relevance gate
        # (search_knowledge_base) can be calibrated from real data — the sense of
        # `ctx.score` (distance vs. similarity) is not documented consistently.
        scores = [h.score for h in out]
        logger.info(
            "RAG query | rags=%s | top_k=%d | dist_thr=%.2f | hits=%d | scores=%s",
            targets,
            top_k,
            distance_threshold,
            len(out),
            [round(s, 3) for s in scores],
        )
        return out

    # ─────────────────────────────────────────────────────────────────────
    # Public CRUD
    # ─────────────────────────────────────────────────────────────────────

    def add_document(self, slug: str, local_path: Path | str) -> dict[str, str]:
        """Upload a file to a RAG's GCS sub-prefix and import it into the corpus."""
        spec = self._spec(slug)
        path = Path(local_path)
        if not path.is_file():
            raise RagOperationError(f"add_document: file not found: {path}")

        corpus_name = self._resolve_corpus(spec.slug)
        gcs_uri = self._upload_to_gcs(spec, path)
        self._import_to_corpus(corpus_name, gcs_uri)
        return {"rag": spec.slug, "gcs_uri": gcs_uri, "corpus_name": corpus_name}

    def list_documents(self, slug: str) -> list[RagDocument]:
        """List all files registered in the slug's corpus. Returns [] if the
        corpus has not been created yet."""
        spec = self._spec(slug)
        try:
            corpus_name = self._resolve_corpus(spec.slug, create_if_missing=False)
        except MissingWorkspaceCorpus:
            logger.info("list_documents: RAG %s has no corpus yet", spec.slug)
            return []

        try:
            files = list(vertex_rag.list_files(corpus_name=corpus_name))
        except Exception as e:
            logger.exception("list_files failed for RAG %s", spec.slug)
            raise RagOperationError(
                f"list_files failed for RAG {spec.slug}: {e}"
            ) from e

        out: list[RagDocument] = []
        for f in files:
            try:
                assert_own_rag_file(f.name, corpus_name)
            except ValueError:
                logger.warning("Skipping foreign ragFile from workspace corpus")
                continue
            gcs_uris: list[str] = []
            if hasattr(f, "gcs_source") and f.gcs_source is not None:
                gcs_uris = list(getattr(f.gcs_source, "uris", []) or [])
            if gcs_uris:
                try:
                    assert_own_uri(gcs_uris[0])
                except ValueError:
                    logger.warning("Skipping ragFile with foreign GCS source")
                    continue
            create_time = getattr(f, "create_time", None)
            out.append(
                RagDocument(
                    rag_file_name=f.name,
                    display_name=getattr(f, "display_name", "") or "",
                    gcs_uri=gcs_uris[0] if gcs_uris else None,
                    create_time=str(create_time) if create_time else None,
                )
            )
        return out

    def delete_document(
        self,
        slug: str,
        *,
        gcs_uri: str | None = None,
        rag_file_name: str | None = None,
        also_delete_gcs: bool = True,
    ) -> dict[str, bool]:
        """
        Remove a document from a RAG. Identify it by either `gcs_uri` or its
        full Vertex `ragFile` resource name (one is required — silent broad
        deletes are exactly what we want to prevent).
        """
        if not gcs_uri and not rag_file_name:
            raise RagOperationError(
                "delete_document requires either gcs_uri or rag_file_name."
            )
        spec = self._spec(slug)
        if gcs_uri:
            assert_own_uri(gcs_uri)
        corpus_name = self._resolve_corpus(spec.slug, create_if_missing=False)
        if rag_file_name:
            assert_own_rag_file(rag_file_name, corpus_name)

        target_file = rag_file_name
        target_gcs = gcs_uri
        try:
            files = list(vertex_rag.list_files(corpus_name=corpus_name))
        except Exception as e:
            logger.exception("delete_document: list_files failed for RAG %s", spec.slug)
            raise RagOperationError(f"list_files failed: {e}") from e

        if not target_file:
            for f in files:
                f_uris: list[str] = []
                if hasattr(f, "gcs_source") and f.gcs_source is not None:
                    f_uris = list(getattr(f.gcs_source, "uris", []) or [])
                if target_gcs in f_uris:
                    target_file = f.name
                    break
            if not target_file:
                from rubik_core.ai_providers.embedding_index import cloud_index_versions

                if any(cloud_index_versions(corpus_name)):
                    raise CloudVectorIndexError(
                        "Vector source has no bound RAG file; repair is required"
                    )
                logger.warning(
                    "delete_document: no rag file matches gcs_uri=%s in rag=%s",
                    target_gcs,
                    spec.slug,
                )
                return {"rag_deleted": False, "gcs_deleted": False}
        elif not target_gcs:
            for f in files:
                if f.name == target_file:
                    f_uris = []
                    if hasattr(f, "gcs_source") and f.gcs_source is not None:
                        f_uris = list(getattr(f.gcs_source, "uris", []) or [])
                    if f_uris:
                        target_gcs = f_uris[0]
                    break

        if target_file is None or not any(f.name == target_file for f in files):
            raise ValueError("RAG file is not in this workspace's corpus")
        assert_own_rag_file(target_file, corpus_name)
        if target_gcs:
            assert_own_uri(target_gcs)
            matched = next(f for f in files if f.name == target_file)
            matched_uris = list(
                getattr(getattr(matched, "gcs_source", None), "uris", []) or []
            )
            if target_gcs not in matched_uris:
                raise ValueError("RAG file and stored URI do not match")
        source_update = (
            begin_cloud_source_update(
                corpus_name, target_gcs, "delete", client=self.gcs_client
            )
            if target_gcs
            else None
        )
        if target_gcs is None:
            from rubik_core.ai_providers.embedding_index import cloud_index_versions

            if any(cloud_index_versions(corpus_name)):
                raise RagOperationError("Vector source URI is required for deletion")
        rag_ok = False
        try:
            vertex_rag.delete_file(name=target_file)
            logger.info(
                "Deleted ragFile | rag=%s | file=%s | gcs=%s",
                spec.slug,
                target_file,
                target_gcs,
            )
            rag_ok = True
        except Exception as e:
            fail_cloud_source_update(source_update)
            logger.exception(
                "rag.delete_file failed | rag=%s | file=%s", spec.slug, target_file
            )
            raise RagOperationError(f"rag.delete_file failed: {e}") from e

        if target_gcs:
            finish_cloud_source_update(
                corpus_name,
                target_gcs,
                "delete",
                source_update,
                client=self.gcs_client,
            )

        gcs_ok = False
        if also_delete_gcs and target_gcs:
            gcs_ok = self._delete_from_gcs(target_gcs)

        return {"rag_deleted": rag_ok, "gcs_deleted": gcs_ok}

    def update_document(self, slug: str, local_path: Path | str) -> dict[str, str]:
        """
        Replace a document in a RAG. Deletes any existing ragFile at the same
        target URI before re-importing the new bytes.
        """
        spec = self._spec(slug)
        path = Path(local_path)
        if not path.is_file():
            raise RagOperationError(f"update_document: file not found: {path}")

        corpus_name = self._resolve_corpus(spec.slug)
        target_blob = self._gcs_blob_name(spec, path.name)
        target_uri = f"gs://{self.bucket_name}/{target_blob}"

        try:
            existing = list(vertex_rag.list_files(corpus_name=corpus_name))
        except Exception as e:
            logger.exception("update_document: list_files failed for RAG %s", spec.slug)
            raise RagOperationError(f"list_files failed: {e}") from e

        for f in existing:
            f_uris: list[str] = []
            if hasattr(f, "gcs_source") and f.gcs_source is not None:
                f_uris = list(getattr(f.gcs_source, "uris", []) or [])
            if target_uri in f_uris:
                assert_own_rag_file(f.name, corpus_name)
                stale_update = begin_cloud_source_update(
                    corpus_name, target_uri, "delete", client=self.gcs_client
                )
                try:
                    vertex_rag.delete_file(name=f.name)
                    finish_cloud_source_update(
                        corpus_name,
                        target_uri,
                        "delete",
                        stale_update,
                        client=self.gcs_client,
                    )
                    logger.info(
                        "update_document: removed stale ragFile %s before re-import",
                        f.name,
                    )
                except Exception as e:
                    fail_cloud_source_update(stale_update)
                    logger.exception(
                        "update_document: failed to delete stale ragFile %s", f.name
                    )
                    raise RagOperationError(
                        f"update_document: failed to remove stale ragFile {f.name}: {e}"
                    ) from e
                break

        gcs_uri = self._upload_to_gcs(spec, path)
        self._import_to_corpus(corpus_name, gcs_uri)
        return {"rag": spec.slug, "gcs_uri": gcs_uri, "corpus_name": corpus_name}


# Re-export `build_registry` here so callers can construct test managers
# without importing two modules.
__all__ = [
    "RagManager",
    "RagDocument",
    "RagQueryHit",
    "RagOperationError",
    "NoUC1CorporaError",
    "build_registry",
    "COMPANYWIDE_SLUG",
]

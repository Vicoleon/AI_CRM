"""
Document & File Management (DAM) router.

Wraps the ingestion pipeline with three behaviours specific to user-driven uploads:
  1. SHA-256 content deduplication (re-uploads return 409 instead of duplicating).
  2. Department / company-wide corpus routing via FileProcessor.
  3. Background ingestion + a status endpoint the frontend can poll while
     Vertex RAG finishes its async indexing.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import tempfile
import uuid
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Response,
    UploadFile,
)
from pydantic import BaseModel

from backend.auth import get_current_user
from backend.jobs.launcher import (
    reject_support_background_work,
    require_automations_http,
    submit_background_work,
)
from backend.routers.ingest import get_processor, require_upload_permission
from rubik_core.auth.access import has_document_access
from rubik_core.auth.permissions import (
    KNOWLEDGE_BASE_UPLOAD_DENIED,
    has_permission,
)
from rubik_core.automation import require_automations_enabled
from rubik_core.config import AppMode, settings
from rubik_core.cost.usage import attribute_to
from rubik_core.dam.audit import log_action
from rubik_core.dam.registry import AssetMetadata, DAMRegistry
from rubik_core.dam.vectorstore import delete_document_vectors
from rubik_core.dam.versions import (
    materialize,
    purge_version_bytes,
    summarize_changes,
    text_of,
    version_block_reason,
    versioned_storage_name,
)
from rubik_core.database.models import User
from rubik_core.database.schemas import AssetVersionInfo
from rubik_core.gemini_tuning import generation_config_for
from rubik_core.ingestion.loader import DocumentLoader
from rubik_core.ingestion.processor import IngestionProcessor
from rubik_core.models import get_genai_client
from rubik_core.rag.config import (
    COMPANYWIDE_SLUG,
    current_registry,
    slugify_department,
)
from rubik_core.tenancy.storage import local_data_dir, object_name, safe_upload_name

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dam", tags=["dam"])
dam_registry = DAMRegistry()


class FileProcessor:
    """Routes a file to the right RAG corpus based on department / company-wide
    flag, and owns the SHA-256 dedup primitive used by the upload endpoint.

    Routing is delegated to the UC-1 RAG registry (`rubik_core.rag.config`):
    the live `departments` table is the single source of truth for which RAG
    slugs exist (`sales`, `hr`, `compliance_audit`, ...) and the corpus
    `display_name` is `{slug}_rag`. The literal `companywide` slug is used
    when `is_company_wide=True`. There is intentionally NO hardcoded map
    here — adding a department to the table immediately makes it routable.
    """

    READ_CHUNK = 1024 * 64  # 64KB

    def __init__(self, processor: IngestionProcessor):
        self.processor = processor

    # ───────────────────────── hashing ─────────────────────────

    @staticmethod
    def compute_content_hash(file_path: Path) -> str:
        """SHA-256 of the file's raw bytes — used as the dedup key."""
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            for block in iter(lambda: f.read(FileProcessor.READ_CHUNK), b""):
                h.update(block)
        return h.hexdigest()

    # ───────────────────────── corpus routing ─────────────────────────

    def resolve_corpus_display_name(
        self, department: str | None, is_company_wide: bool
    ) -> str | None:
        """Return the Vertex RAG corpus display_name for the given inputs.

        Resolution order:
          - is_company_wide=True  → companywide_rag
          - department slugifies to a known registry entry → {slug}_rag
          - otherwise → None (caller falls back to VERTEX_RAG_CORPUS_ID)
        """
        registry = current_registry()
        if is_company_wide:
            spec = registry.get(COMPANYWIDE_SLUG)
            return spec.display_name if spec else None
        if not department:
            return None
        slug = slugify_department(department)
        spec = registry.get(slug)
        return spec.display_name if spec else None

    def get_or_create_corpus_id(self, display_name: str | None) -> str | None:
        """Look up a Vertex RAG corpus by display_name, creating it if absent.

        Returns None if display_name is None (caller falls back to default
        corpus) or when running outside GCP mode (Chroma path ignores corpus
        IDs).

        UC-1's `seed_rags.py` creates the canonical corpora at deploy time;
        this fallback is for the rare case where a new department is added
        after deploy and an upload comes in before the seed re-runs.
        """
        if not display_name or settings.APP_MODE != AppMode.GCP:
            return None
        from rubik_core.tenancy.corpora import corpus_id

        if display_name in {"companywide_rag", "vto_rag", "eos_exec_rag"}:
            return corpus_id(display_name, create=True)
        if not display_name.endswith("_rag"):
            raise ValueError("Unknown RAG display name")
        return corpus_id("rag", audience=display_name[:-4], create=True)

    # ───────────────────────── ingestion ─────────────────────────

    def ingest(
        self,
        file_path: Path,
        department: str | None,
        is_company_wide: bool,
        manual_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        """Route to the correct corpus and call the underlying IngestionProcessor."""
        display_name = self.resolve_corpus_display_name(department, is_company_wide)
        target_corpus_id = self.get_or_create_corpus_id(display_name)
        manual_metadata = {**manual_metadata, "corpus": display_name or "default"}
        return self.processor.process_file(
            file_path,
            manual_metadata=manual_metadata,
            target_corpus_id=target_corpus_id,
        )


def get_file_processor(
    processor: IngestionProcessor = Depends(get_processor),
) -> FileProcessor:
    return FileProcessor(processor)


# ───────────────────────── AI description ─────────────────────────

# Cap how much document text we hand the LLM — a few thousand chars is plenty
# for a 2-3 sentence catalog summary and keeps latency/cost low.
_DESCRIBE_MAX_CHARS = 8000


@attribute_to("dam")
def generate_description(text: str) -> str | None:
    """Summarize document text into a short catalog description.

    Uses Gemini Lite when Vertex is configured; falls back to a truncated
    snippet of the document text on any error or in local mode (so the
    feature degrades gracefully instead of failing the upload). Returns
    None when there's no usable text.
    """
    snippet = " ".join((text or "").split())
    if not snippet:
        return None
    snippet = snippet[:_DESCRIBE_MAX_CHARS]

    from rubik_core.ai_providers.registry import model_routes

    try:
        selected = model_routes().get("dam_description") or {}
    except Exception:
        selected = {"provider": "unavailable"}
    client_selected = selected.get("provider") not in (None, "vertex_managed")

    if settings.APP_MODE == AppMode.GCP or settings.GCP_PROJECT_ID or client_selected:
        try:
            from rubik_core.inference_workloads import run_text

            prompt = (
                "Write a concise 2-3 sentence summary of the following document "
                "for a knowledge-base catalog. State what the document is and what "
                "questions it could help answer. Plain text, no preamble or "
                "markdown.\n\n"
                f"{snippet}"
            )

            def managed(route):
                resp = get_genai_client().models.generate_content(
                    model=route.model,
                    contents=prompt,
                    config=generation_config_for(route.model),
                )
                return getattr(resp, "text", "") or ""

            out = run_text(
                "dam_description",
                prompt,
                default_model=settings.VERTEX_LLM_MODEL_LITE,
                managed=managed,
            )
            out = out.strip()
            if out:
                return out
            if client_selected:
                return "AI summary unavailable; original preview: " + snippet[:200]
        except Exception:  # noqa: BLE001 - retain a visible source preview
            logger.warning("AI description generation failed")
            if client_selected:
                return "AI summary unavailable; original preview: " + snippet[:200]

    # Heuristic fallback: first ~280 chars of the document text.
    return (snippet[:280] + "…") if len(snippet) > 280 else snippet


# ───────────────────────── background ingestion ─────────────────────────


def _run_ingest(
    file_processor: FileProcessor,
    file_path: Path,
    doc_id: str,
    department: str | None,
    is_company_wide: bool,
    manual_metadata: dict[str, Any],
    actor_email: str,
    cleanup_dir: tempfile.TemporaryDirectory | None,
    replace_doc_id: str | None = None,
    version_number: int | None = None,
    supersede_source_path: str | None = None,
):
    """Run ingestion in the background. Updates registry.processing on completion.

    If ``manual_metadata['description']`` is blank, auto-generates one from the
    document text. When ``replace_doc_id`` is set, the superseded asset is
    purged (vector store + registry) after the new doc's ingest reports success
    — and only if the new ingest didn't fail. (In GCP the Vertex import is
    async, so the new version may take a short time to become queryable after
    the purge; if the new upload reused the old storage path, the vector purge
    is skipped because the overwrite already replaced the content.)

    ``version_number`` marks a NEW VERSION of an existing asset (same doc_id;
    see ``rubik_core.dam.versions``). Then, on success, the previous version's
    chunks are purged from the corpus by ``supersede_source_path`` — bytes kept,
    so the old version stays downloadable/restorable — and an AI "what changed"
    summary is written onto the version row. Ordering: the new version indexes
    FIRST, so Chat keeps answering from the old one until the new one is live;
    if the new ingest fails, the old chunks are left in place (the row shows
    "failed" and the uploader can restore or retry). Locally (Chroma keys off
    doc_id, so a post-ingest purge would take the new chunks too) the old
    chunks are removed BEFORE the new ingest instead.
    """
    require_automations_enabled()
    try:
        is_version = version_number is not None and version_number > 1
        if is_version:
            current = dam_registry.get_asset(doc_id)
            _pre_version_purge(
                doc_id,
                new_source_path=current.source_path if current else None,
                department=department,
                is_company_wide=is_company_wide,
            )

        # Auto-generate a description when the uploader left it blank (a new
        # version keeps the document's existing description).
        if not is_version and not (manual_metadata.get("description") or "").strip():
            loaded = file_processor.processor.loader.load_file(file_path)
            auto_desc = generate_description(loaded.text if loaded else "")
            if auto_desc:
                manual_metadata = {**manual_metadata, "description": auto_desc}
                dam_registry.update_asset(doc_id, {"description": auto_desc})

        res_dict = file_processor.ingest(
            file_path,
            department=department,
            is_company_wide=is_company_wide,
            manual_metadata=manual_metadata,
        )
        result = res_dict.get("result")
        if not result or not result.success:
            err = result.error if result else "unknown"
            dam_registry.update_asset(doc_id, {"status": "failed", "processing": False})
            log_action(actor_email, "ingest", doc_id, "failure", {"error": err})
            logger.error("Background ingest failed for %s: %s", doc_id, err)
            return
        published_uri = res_dict.get("gcs_uri")
        if published_uri:
            from rubik_core.tenancy.storage import assert_own_uri

            assert_own_uri(published_uri)
            dam_registry.update_asset(
                doc_id, {"source_path": published_uri, "processing": False}
            )
            if is_version:
                dam_registry.update_version(
                    doc_id, version_number, {"source_path": published_uri}
                )
        else:
            dam_registry.update_asset(doc_id, {"processing": False})
        log_action(actor_email, "ingest", doc_id, "success", manual_metadata)
        logger.info("Background ingest complete for %s", doc_id)

        # New-version flow: retire the previous version's chunks (bytes stay),
        # then write the "what changed" note onto the version row.
        if is_version:
            _finish_version(
                doc_id,
                version_number,
                supersede_source_path,
                department=department,
                is_company_wide=is_company_wide,
                actor_email=actor_email,
            )

        # Replace flow: drop the superseded doc now that the new one ingested.
        if replace_doc_id and replace_doc_id != doc_id:
            try:
                old = dam_registry.get_asset(replace_doc_id)
                new = dam_registry.get_asset(doc_id)
                same_source = bool(
                    old
                    and new
                    and old.source_path
                    and old.source_path == new.source_path
                )
                # If the new upload overwrote the old one at the same storage
                # path (same filename), purging by that shared URI would delete
                # the NEW bytes we just imported. The overwrite already replaced
                # the content, so only drop the stale registry row.
                if old and not same_source:
                    delete_document_vectors(
                        replace_doc_id,
                        source_path=old.source_path,
                        department=old.department,
                        is_company_wide=old.is_company_wide,
                    )
                dam_registry.delete_asset(replace_doc_id)
                log_action(
                    actor_email,
                    "replace",
                    replace_doc_id,
                    "success",
                    {"replaced_by": doc_id},
                )
                logger.info("Replaced %s with %s", replace_doc_id, doc_id)
            except Exception as e:  # noqa: BLE001 - new doc is already live
                log_action(
                    actor_email, "replace", replace_doc_id, "failure", {"error": str(e)}
                )
                logger.exception("Failed to purge replaced doc %s", replace_doc_id)
    except Exception as e:
        dam_registry.update_asset(doc_id, {"status": "failed", "processing": False})
        log_action(actor_email, "ingest", doc_id, "failure", {"error": str(e)})
        logger.exception("Background ingest crashed for %s", doc_id)
    finally:
        # GCP mode keeps a temp copy; local mode persists into LOCAL_DATA_DIR.
        if cleanup_dir is not None:
            try:
                cleanup_dir.cleanup()
            except Exception:
                pass


def _pre_version_purge(
    doc_id: str,
    *,
    new_source_path: str | None,
    department: str | None,
    is_company_wide: bool,
) -> None:
    """Before indexing a new version, clear what would collide with it.

    GCP: a RESTORE re-imports a URI that may still have a ragFile from when it
    was current (e.g. the ingest it was replaced by failed, so its chunks were
    never purged) — importing on top would leave two ragFiles for one URI, so
    drop any existing one first (bytes kept). A fresh upload's hashed name is
    unique, so this is a cheap no-op for it. Local Chroma keys off doc_id, so
    here is the only safe place to remove the previous version's chunks.
    """
    try:
        if settings.APP_MODE == AppMode.GCP:
            if new_source_path:
                delete_document_vectors(
                    doc_id,
                    source_path=new_source_path,
                    department=department,
                    is_company_wide=is_company_wide,
                    also_delete_gcs=False,
                )
        else:
            delete_document_vectors(
                doc_id,
                department=department,
                is_company_wide=is_company_wide,
                also_delete_gcs=False,
            )
    except Exception:  # noqa: BLE001 — not-found is the normal outcome here
        logger.info("version pre-purge for %s found nothing to remove", doc_id)


def _finish_version(
    doc_id: str,
    version_number: int,
    supersede_source_path: str | None,
    *,
    department: str | None,
    is_company_wide: bool,
    actor_email: str,
) -> None:
    asset = dam_registry.get_asset(doc_id)
    new_source_path = asset.source_path if asset else None

    # 1. Retire the previous version's chunks. GCP only — locally this already
    # happened before the ingest (see _pre_version_purge). Bytes are kept.
    if (
        settings.APP_MODE == AppMode.GCP
        and supersede_source_path
        and supersede_source_path != new_source_path
    ):
        try:
            delete_document_vectors(
                doc_id,
                source_path=supersede_source_path,
                department=department,
                is_company_wide=is_company_wide,
                also_delete_gcs=False,
            )
        except Exception as e:  # noqa: BLE001 — new version is already live
            log_action(
                actor_email,
                "version.purge_previous",
                doc_id,
                "failure",
                {"version": version_number, "error": str(e)},
            )
            logger.exception("Failed to purge previous version of %s", doc_id)

    # 2. AI "what changed" note — compares the superseded bytes with the new.
    try:
        prev_name = None
        prev_row = dam_registry.get_version(doc_id, version_number - 1)
        if prev_row:
            prev_name = prev_row.original_filename
            if not supersede_source_path:
                supersede_source_path = prev_row.source_path
        summary = summarize_changes(
            text_of(supersede_source_path),
            text_of(new_source_path),
            previous_name=prev_name,
            new_name=asset.original_filename if asset else None,
        )
        if summary:
            dam_registry.update_version(
                doc_id, version_number, {"change_summary": summary}
            )
    except Exception:  # noqa: BLE001 — the note is nice-to-have
        logger.warning(
            "Change summary failed for %s v%s", doc_id, version_number, exc_info=True
        )
    log_action(
        actor_email,
        "version.publish",
        doc_id,
        "success",
        {"version": version_number},
    )


def _asset_manual_metadata(asset: AssetMetadata) -> dict[str, Any]:
    """Vector-store metadata for an asset row, same shape as the upload path."""
    return {
        "owner": asset.owner,
        "approver": "",
        "topic": asset.topic,
        "department": asset.department,
        "description": (asset.description or "").strip(),
        "tags": ",".join(asset.tags or []),
        "status": asset.status or "approved",
        "is_company_wide": asset.is_company_wide,
    }


def _launch_version_ingest(
    background_tasks: BackgroundTasks,
    file_processor: FileProcessor,
    *,
    asset: AssetMetadata,
    file_path: Path,
    cleanup_dir: tempfile.TemporaryDirectory | None,
    actor_email: str,
    version_number: int,
    supersede_source_path: str | None,
    already_staged: bool,
) -> tempfile.TemporaryDirectory | None:
    """Dispatch a new version's ingest (cubie-worker Job, else inline).

    Returns the tempdir the CALLER must still clean up (None when the
    background task took ownership of it).
    """
    require_automations_http()
    fallback = partial(
        _run_ingest,
        file_processor,
        file_path,
        asset.doc_id,
        asset.department,
        asset.is_company_wide,
        _asset_manual_metadata(asset),
        actor_email,
        cleanup_dir,
        None,
        version_number,
        supersede_source_path,
    )
    staged = already_staged
    if settings.APP_MODE == AppMode.GCP and not staged:
        try:
            file_processor.processor.vector_store.upload_to_gcs(file_path)
            staged = True
        except Exception:  # noqa: BLE001 — fall back to the inline path
            logger.exception("DAM version staging upload failed for %s", asset.doc_id)

    if staged:
        args = [
            "--doc-id",
            asset.doc_id,
            "--actor-email",
            actor_email,
            "--version",
            str(version_number),
        ]
        if supersede_source_path:
            args += ["--supersede-source-path", supersede_source_path]
        executor, _ = submit_background_work(
            background_tasks,
            module="backend.jobs.dam_ingest_job",
            args=args,
            fallback=fallback,
            label=f"dam-version:{asset.doc_id}:v{version_number}",
        )
    else:
        background_tasks.add_task(fallback)
        executor = "inline"
    # Inline: the background task owns the tempdir now.
    return None if executor == "inline" else cleanup_dir


# ───────────────────────── endpoints ─────────────────────────


@router.post(
    "/upload",
    dependencies=[
        Depends(reject_support_background_work),
        Depends(require_automations_http),
        Depends(require_upload_permission),
    ],
)
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    topic: str = Form(...),
    department: str = Form(...),
    description: str = Form(""),
    tags: str = Form(""),
    is_company_wide: bool = Form(False),
    force: bool = Form(False),
    replace_doc_id: str = Form(""),
    user: User = Depends(get_current_user),
    file_processor: FileProcessor = Depends(get_file_processor),
):
    """Upload a document into the DAM.

    Uploads land live in the RAG immediately (status ``approved``) — there is
    no approval/review step.

    Flow:
      1. Persist the upload locally (or to a tempdir for GCP).
      2. SHA-256 the bytes; if a matching record exists and neither ``force``
         nor ``replace_doc_id`` is set, return 409 with the existing record
         info — the UI uses this to offer "Keep both" (force) or "Replace".
      3. ``force=True`` keeps both copies; ``replace_doc_id`` ingests the new
         copy and purges the named old doc once indexing succeeds. Either
         bypasses the dedup check.
      4. ``description`` may be blank — it's auto-generated from the file text
         during background ingest. ``tags`` is a comma-separated string.
      5. Register the asset with processing=True, kick off background ingest,
         and return the doc_id so the frontend can poll.
    """
    # This handler is also called directly by chat-file publication, which
    # bypasses its FastAPI route dependency. Refuse before bytes, GCS or rows.
    reject_support_background_work()
    require_automations_http()
    if not has_permission(user, "assets", "create"):
        raise HTTPException(status_code=403, detail=KNOWLEDGE_BASE_UPLOAD_DENIED)
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")
    try:
        stored_name = safe_upload_name(file.filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Comma-separated tags → clean list, de-duplicated, order preserved.
    tag_list: list[str] = []
    for raw_tag in tags.split(","):
        t = raw_tag.strip()
        if t and t not in tag_list:
            tag_list.append(t)

    # A replace request implies the user already saw the duplicate and chose to
    # supersede it, so it bypasses dedup just like force.
    skip_dedup = force or bool(replace_doc_id)

    # Replacing a doc deletes it — gate it to owner/admin, same as delete. The
    # 409 duplicate response exposes the existing doc_id, so without this a user
    # who happens to hold the same file could delete someone else's document.
    if replace_doc_id:
        target = dam_registry.get_asset(replace_doc_id)
        if not target:
            raise HTTPException(
                status_code=404, detail="Document to replace was not found."
            )
        from backend.routers.assets import _require_live_xray_asset

        await _require_live_xray_asset(target)
        if not (user.is_admin or target.owner == user.email):
            raise HTTPException(
                status_code=403,
                detail="You can only replace your own documents.",
            )

    # 1. Choose where to land the bytes.
    cleanup_dir: tempfile.TemporaryDirectory | None = None
    if settings.APP_MODE == AppMode.GCP:
        cleanup_dir = tempfile.TemporaryDirectory()
        file_path = Path(cleanup_dir.name) / stored_name
    else:
        local_dir = local_data_dir()
        local_dir.mkdir(parents=True, exist_ok=True)
        file_path = local_dir / stored_name

    try:
        with open(file_path, "wb") as buf:
            shutil.copyfileobj(file.file, buf)

        # 2. Hash + dedup. force=True bypasses dedup so the user can intentionally
        # ingest a "new version" of the same content.
        content_hash = FileProcessor.compute_content_hash(file_path)
        if not skip_dedup:
            existing = dam_registry.get_asset_by_content_hash(content_hash)
            if existing:
                from backend.routers.assets import _require_live_xray_asset

                try:
                    await _require_live_xray_asset(existing)
                except HTTPException:
                    # A legacy or now-foreign X-Ray cannot disclose its doc id,
                    # filename, owner or creation time in a duplicate response.
                    existing = None
            if existing:
                # Don't pollute the data dir with a duplicate copy.
                if cleanup_dir is None:
                    try:
                        file_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                else:
                    cleanup_dir.cleanup()
                    cleanup_dir = None
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "Duplicate detected — this file is already in the DAM.",
                        "existing_doc_id": existing.doc_id,
                        "existing_filename": existing.original_filename,
                        "existing_uploaded_at": existing.created_at.isoformat()
                        if existing.created_at
                        else None,
                        "existing_owner": existing.owner,
                        "can_force": True,
                        "can_replace": True,
                        "force_param": "force",
                    },
                )

        # For a replace, store the new bytes under a versioned name so the new
        # doc lands at a distinct storage URI from the one it supersedes. The
        # filename-keyed GCS path would otherwise be shared: re-importing the
        # same URI leaves the OLD ragFile (stale chunks) alongside the new one,
        # and purging by that shared URI risks deleting the NEW bytes. A unique
        # URI lets the replace purge remove the old ragFile cleanly. The
        # original filename is preserved for display.
        if replace_doc_id:
            stem = file_path.stem
            suffix = file_path.suffix
            versioned_path = file_path.with_name(f"{stem}__{content_hash[:8]}{suffix}")
            file_path.rename(versioned_path)
            file_path = versioned_path

        # 3. Register the asset (processing=True).
        doc_id = file_processor.processor.loader.generate_doc_id(file_path)
        if settings.APP_MODE == AppMode.GCP:
            # Use the on-disk name (== the GCS blob name) so source_path matches
            # exactly what gets uploaded — load-bearing for the replace purge.
            recorded_path = (
                f"gs://{settings.GCS_BUCKET_NAME}/{object_name('rag', file_path.name)}"
            )
        else:
            recorded_path = file_path.as_uri()

        now = datetime.utcnow()
        asset = AssetMetadata(
            doc_id=doc_id,
            owner=user.email,
            topic=topic,
            department=department,
            description=description.strip() or None,
            tags=tag_list,
            status="approved",
            is_company_wide=is_company_wide,
            source_path=recorded_path,
            original_filename=file.filename,
            content_hash=content_hash,
            processing=True,
            created_at=now,
            updated_at=now,
        )
        dam_registry.register_asset(asset)

        # 4. Background ingest.
        manual_metadata = {
            "owner": user.email,
            "approver": "",
            "topic": topic,
            "department": department,
            "description": description.strip(),
            # Vector-store metadata must be scalar (Chroma rejects list values,
            # incl. the empty list). The structured list lives on the DB row;
            # here we store a comma-joined string only.
            "tags": ",".join(tag_list),
            "status": "approved",
            "is_company_wide": is_company_wide,
        }
        # #9: prefer the cubie-worker Cloud Run Job — parse + AI description +
        # Vertex import can run for minutes, which a BackgroundTask on the
        # CPU-throttled web service can silently never finish. The Job runs on
        # a different machine, so stage the raw bytes at the asset's final GCS
        # path (== source_path) first; the inline fallback keeps using the
        # request-local file exactly as before.
        fallback = partial(
            _run_ingest,
            file_processor,
            file_path,
            doc_id,
            department,
            is_company_wide,
            manual_metadata,
            user.email,
            cleanup_dir,
            replace_doc_id or None,
        )
        staged = False
        if settings.APP_MODE == AppMode.GCP:
            try:
                file_processor.processor.vector_store.upload_to_gcs(file_path)
                staged = True
            except Exception:  # noqa: BLE001 — fall back to the inline path
                logger.exception("DAM staging upload failed for %s", doc_id)

        if staged:
            executor, _ = submit_background_work(
                background_tasks,
                module="backend.jobs.dam_ingest_job",
                args=["--doc-id", doc_id, "--actor-email", user.email]
                + (["--replace-doc-id", replace_doc_id] if replace_doc_id else []),
                fallback=fallback,
                label=f"dam-ingest:{doc_id}",
            )
        else:
            background_tasks.add_task(fallback)
            executor = "inline"
        if executor == "inline":
            # Background task owns cleanup_dir now. On the job path the bytes
            # are already in GCS, so the request's finally block may clean the
            # local tempdir immediately.
            cleanup_dir = None

        return {
            "doc_id": doc_id,
            "status": "processing",
            "message": "Upload accepted — indexing in progress. Poll /api/dam/{doc_id}/status.",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Exception in DAM upload: %s", e)
        log_action(
            user.email,
            "upload",
            file.filename or "unknown",
            "failure",
            {"error": str(e)},
        )
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if cleanup_dir is not None:
            try:
                cleanup_dir.cleanup()
            except Exception:
                pass


@router.post("/describe")
async def describe_document(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
):
    """Generate an AI summary for a candidate upload, before it's ingested.

    The frontend posts the selected file here when the user clicks "Generate
    with AI"; we extract its text and return a short summary the user can edit
    before submitting the real upload. Stateless — nothing is persisted.
    """
    if not has_permission(user, "assets", "create"):
        raise HTTPException(status_code=403, detail=KNOWLEDGE_BASE_UPLOAD_DENIED)
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")
    try:
        stored_name = safe_upload_name(file.filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    with tempfile.TemporaryDirectory() as td:
        fp = Path(td) / stored_name
        with open(fp, "wb") as buf:
            shutil.copyfileobj(file.file, buf)
        loaded = DocumentLoader().load_file(fp)
        text = loaded.text if loaded else ""

    description = generate_description(text)
    if not description:
        raise HTTPException(
            status_code=422,
            detail="Couldn't read any text from this file to summarize.",
        )
    return {"description": description}


# ───────────────────────────── versions ─────────────────────────────
# Declared before the ``{doc_id:path}/status`` and DELETE catch-alls so the
# literal ``/versions`` suffix is never read as part of a doc_id.


class RestoreVersionRequest(BaseModel):
    note: str = ""


def _manage_gate(user: User, asset: AssetMetadata) -> None:
    """Who may add/restore versions: same rule as archive/update."""
    from backend.routers.assets import _can_manage_asset

    if not _can_manage_asset(user, asset):
        raise HTTPException(
            status_code=403,
            detail="Only the document's owner, an admin, or an editor for its department can add versions.",
        )


def _readable_asset(doc_id: str, user: User) -> AssetMetadata:
    from backend.routers.assets import _load_readable_asset

    return _load_readable_asset(doc_id, user)


@router.get("/{doc_id:path}/versions", response_model=list[AssetVersionInfo])
async def list_versions(doc_id: str, user: User = Depends(get_current_user)):
    """The document's version history, newest first (version 1 included)."""
    asset = _readable_asset(doc_id, user)
    return dam_registry.list_versions(asset.doc_id)


@router.get("/{doc_id:path}/versions/{version_number}/download")
async def download_version(
    doc_id: str, version_number: int, user: User = Depends(get_current_user)
):
    """Stream one version's stored bytes — any version, not just the current."""
    import mimetypes

    from backend.services.generated_docs import load_doc_bytes

    asset = _readable_asset(doc_id, user)
    version = dam_registry.get_version(asset.doc_id, version_number)
    if not version or not version.source_path:
        raise HTTPException(status_code=404, detail="That version has no stored file.")
    try:
        data = load_doc_bytes(version.source_path)
    except Exception as e:  # noqa: BLE001 — surface storage misses as 404
        log_action(
            user.email,
            "version.download",
            asset.doc_id,
            "failure",
            {"version": version_number, "error": str(e)},
        )
        raise HTTPException(status_code=404, detail="Stored file unavailable.")
    filename = (
        version.original_filename
        or f"{asset.doc_id.replace('/', '-')}-v{version_number}"
    )
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    log_action(
        user.email,
        "version.download",
        asset.doc_id,
        "success",
        {"version": version_number},
    )
    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post(
    "/{doc_id:path}/versions",
    response_model=AssetVersionInfo,
    dependencies=[
        Depends(reject_support_background_work),
        Depends(require_automations_http),
    ],
)
async def upload_version(
    doc_id: str,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    note: str = Form(""),
    user: User = Depends(get_current_user),
    file_processor: FileProcessor = Depends(get_file_processor),
):
    """Upload a new version of an existing document.

    The document keeps its ``doc_id`` (bookmarks, shares and citations stay
    valid); its filename, bytes and hash move to the new file, the previous
    version stays downloadable and restorable, and Chat switches over once the
    new file is indexed. Identical bytes to the current version → 409.
    """
    require_automations_http()
    doc_id = doc_id.lstrip("/")
    asset = dam_registry.get_asset(doc_id)
    if not asset:
        raise HTTPException(status_code=404, detail=f"Asset not found: {doc_id}")
    from backend.routers.assets import _require_live_xray_asset

    await _require_live_xray_asset(asset)
    _manage_gate(user, asset)
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")
    try:
        stored_name = safe_upload_name(file.filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    blocked = version_block_reason(asset)
    if blocked:
        raise HTTPException(status_code=400, detail=blocked)
    if asset.processing:
        raise HTTPException(
            status_code=409,
            detail="This document is still being indexed — try again once it's ready.",
        )

    cleanup_dir: tempfile.TemporaryDirectory | None = None
    if settings.APP_MODE == AppMode.GCP:
        cleanup_dir = tempfile.TemporaryDirectory()
        base_dir = Path(cleanup_dir.name)
    else:
        base_dir = local_data_dir()
        base_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Land the bytes under a throwaway name first: locally the data dir is
        # the storage, and ``base_dir / file.filename`` may BE version 1's file.
        tmp_path = base_dir / f".upload-{uuid.uuid4().hex}{Path(stored_name).suffix}"
        with open(tmp_path, "wb") as buf:
            shutil.copyfileobj(file.file, buf)
        content_hash = FileProcessor.compute_content_hash(tmp_path)
        if content_hash == asset.content_hash:
            tmp_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=409,
                detail="This file is identical to the current version — nothing to update.",
            )
        file_path = tmp_path.with_name(
            versioned_storage_name(stored_name, content_hash)
        )
        tmp_path.replace(file_path)
        size_bytes = file_path.stat().st_size

        if settings.APP_MODE == AppMode.GCP:
            recorded_path = (
                f"gs://{settings.GCS_BUCKET_NAME}/{object_name('rag', file_path.name)}"
            )
        else:
            recorded_path = file_path.as_uri()

        supersede_source_path = asset.source_path
        version = dam_registry.add_version(
            doc_id,
            source_path=recorded_path,
            original_filename=file.filename,
            content_hash=content_hash,
            size_bytes=size_bytes,
            uploaded_by=user.email,
            note=note,
        )
        if version is None:
            raise HTTPException(status_code=404, detail=f"Asset not found: {doc_id}")
        log_action(
            user.email,
            "version.upload",
            doc_id,
            "success",
            {"version": version.version_number, "filename": file.filename},
        )
        asset = dam_registry.get_asset(doc_id) or asset
        cleanup_dir = _launch_version_ingest(
            background_tasks,
            file_processor,
            asset=asset,
            file_path=file_path,
            cleanup_dir=cleanup_dir,
            actor_email=user.email,
            version_number=version.version_number,
            supersede_source_path=supersede_source_path,
            already_staged=False,
        )
        return version
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Exception in DAM version upload: %s", e)
        log_action(user.email, "version.upload", doc_id, "failure", {"error": str(e)})
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if cleanup_dir is not None:
            try:
                cleanup_dir.cleanup()
            except Exception:
                pass


@router.post(
    "/{doc_id:path}/versions/{version_number}/restore",
    response_model=AssetVersionInfo,
    dependencies=[
        Depends(reject_support_background_work),
        Depends(require_automations_http),
    ],
)
async def restore_version(
    doc_id: str,
    version_number: int,
    background_tasks: BackgroundTasks,
    body: RestoreVersionRequest | None = None,
    user: User = Depends(get_current_user),
    file_processor: FileProcessor = Depends(get_file_processor),
):
    """Make an earlier version current again.

    Restoring never rewrites history: it publishes a NEW version whose bytes
    are the chosen version's (so the trail reads v1 → v2 → v3 = v1 restored),
    re-indexes it, and retires the version it replaces from the corpus.
    """
    require_automations_http()
    doc_id = doc_id.lstrip("/")
    asset = dam_registry.get_asset(doc_id)
    if not asset:
        raise HTTPException(status_code=404, detail=f"Asset not found: {doc_id}")
    from backend.routers.assets import _require_live_xray_asset

    await _require_live_xray_asset(asset)
    _manage_gate(user, asset)
    blocked = version_block_reason(asset)
    if blocked:
        raise HTTPException(status_code=400, detail=blocked)
    if asset.processing:
        raise HTTPException(
            status_code=409,
            detail="This document is still being indexed — try again once it's ready.",
        )
    target = dam_registry.get_version(doc_id, version_number)
    if not target or not target.source_path:
        raise HTTPException(status_code=404, detail="That version has no stored file.")
    if target.version_number == asset.current_version:
        raise HTTPException(
            status_code=409, detail="That is already the current version."
        )

    cleanup_dir: tempfile.TemporaryDirectory | None = None
    try:
        # Bytes live at the old version's path; in GCP the Job downloads them
        # from source_path itself, the inline fallback needs a local copy.
        file_path, cleanup_dir = materialize(target.source_path)
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="Stored file unavailable.")

        supersede_source_path = asset.source_path
        note = (body.note if body else "") or f"Restored version {version_number}"
        version = dam_registry.add_version(
            doc_id,
            source_path=target.source_path,
            original_filename=target.original_filename,
            content_hash=target.content_hash,
            size_bytes=target.size_bytes or file_path.stat().st_size,
            uploaded_by=user.email,
            note=note,
            restored_from=version_number,
        )
        if version is None:
            raise HTTPException(status_code=404, detail=f"Asset not found: {doc_id}")
        log_action(
            user.email,
            "version.restore",
            doc_id,
            "success",
            {"version": version.version_number, "restored_from": version_number},
        )
        asset = dam_registry.get_asset(doc_id) or asset
        cleanup_dir = _launch_version_ingest(
            background_tasks,
            file_processor,
            asset=asset,
            file_path=file_path,
            cleanup_dir=cleanup_dir,
            actor_email=user.email,
            version_number=version.version_number,
            supersede_source_path=supersede_source_path,
            # The bytes are already at source_path in GCS — no staging needed.
            already_staged=settings.APP_MODE == AppMode.GCP,
        )
        return version
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Exception in DAM version restore: %s", e)
        log_action(user.email, "version.restore", doc_id, "failure", {"error": str(e)})
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if cleanup_dir is not None:
            try:
                cleanup_dir.cleanup()
            except Exception:
                pass


@router.get("/{doc_id:path}/status")
async def get_processing_status(
    doc_id: str,
    user: User = Depends(get_current_user),
):
    """Poll this every 30s after upload to know when the file is queryable."""
    doc_id = doc_id.lstrip("/")
    asset = dam_registry.get_asset(doc_id)
    if not asset:
        raise HTTPException(status_code=404, detail=f"Asset not found: {doc_id}")
    from backend.routers.assets import _require_live_xray_asset

    await _require_live_xray_asset(asset)
    if not has_document_access(user, asset):
        raise HTTPException(status_code=403, detail="Not authorized to view this asset")

    if asset.processing:
        ui_status = "processing"
        message = "Processing… your file is being indexed."
    elif asset.status == "failed":
        ui_status = "failed"
        message = "Indexing failed. Please re-upload or contact an admin."
    else:
        ui_status = "ready"
        message = "Ready to query."

    return {
        "doc_id": doc_id,
        "status": ui_status,
        "asset_status": asset.status,
        "message": message,
    }


@router.delete("/{doc_id:path}")
async def delete_document(
    doc_id: str,
    user: User = Depends(get_current_user),
):
    """Remove a document from the Vertex corpus (or local store) and the registry."""
    doc_id = doc_id.lstrip("/")
    asset = dam_registry.get_asset(doc_id)
    if not asset:
        raise HTTPException(status_code=404, detail=f"Asset not found: {doc_id}")
    from backend.routers.assets import _require_live_xray_asset

    await _require_live_xray_asset(asset)
    if asset.owner != user.email and not user.is_admin:
        raise HTTPException(
            status_code=403, detail="Only the owner or an admin can delete this asset"
        )

    try:
        delete_document_vectors(
            doc_id,
            source_path=asset.source_path,
            department=asset.department,
            is_company_wide=asset.is_company_wide,
        )
    except Exception as e:
        log_action(user.email, "delete", doc_id, "failure", {"error": str(e)})
        raise HTTPException(
            status_code=500, detail=f"Failed to delete from vector store: {e}"
        )

    # Superseded versions' files (the current one went with the vectors).
    purge_version_bytes(
        dam_registry.list_versions(doc_id), keep_source_path=asset.source_path
    )

    if not dam_registry.delete_asset(doc_id):
        log_action(
            user.email, "delete", doc_id, "failure", {"error": "registry delete failed"}
        )
        raise HTTPException(status_code=500, detail="Failed to delete from registry")

    log_action(user.email, "delete", doc_id, "success")
    return {"message": "Asset deleted successfully", "doc_id": doc_id}

import logging
import shutil
import tempfile
from datetime import datetime
from functools import partial
from pathlib import Path

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.security import HTTPAuthorizationCredentials

from backend.auth import get_current_user, security
from backend.jobs.launcher import (
    reject_support_background_work,
    submit_background_work,
)
from backend.models.auth import User as UserDTO

# Processor dependency
from rubik_core.adapters.chroma import ChromaAdapter
from rubik_core.adapters.vertex_rag import VertexRagAdapter
from rubik_core.auth.permissions import (
    KNOWLEDGE_BASE_UPLOAD_DENIED,
    has_permission,
)
from rubik_core.config import AppMode, settings
from rubik_core.dam.audit import log_action
from rubik_core.dam.registry import AssetMetadata, DAMRegistry
from rubik_core.database.models import User
from rubik_core.ingestion.processor import IngestionProcessor
from rubik_core.scripts.sync_meet_transcripts import run_sync
from rubik_core.tenancy.scheduler import scheduler_key_valid
from rubik_core.tenancy.storage import local_data_dir, object_name, safe_upload_name

logger = logging.getLogger(__name__)


def get_processor():
    if settings.APP_MODE == AppMode.GCP:
        adapter = VertexRagAdapter()
    else:
        adapter = ChromaAdapter()
    return IngestionProcessor(adapter)


def require_upload_permission(user: UserDTO = Depends(get_current_user)) -> None:
    """Refuse uploads before constructing a model client or processor."""
    if not has_permission(user, "assets", "create"):
        raise HTTPException(status_code=403, detail=KNOWLEDGE_BASE_UPLOAD_DENIED)


router = APIRouter(prefix="/ingest", tags=["ingest"])
scheduler_router = APIRouter(prefix="/ingest", tags=["ingest"])
dam_registry = DAMRegistry()


@router.post("/", dependencies=[Depends(require_upload_permission)])
async def ingest_document(
    file: UploadFile = File(...),
    topic: str = Form(...),
    department: str = Form(...),
    status: str = Form("approved"),
    is_company_wide: bool = Form(False),
    user: User = Depends(get_current_user),
    processor: IngestionProcessor = Depends(get_processor),
):
    # RBAC: Require assets.create permission
    if not has_permission(user, "assets", "create"):
        raise HTTPException(status_code=403, detail=KNOWLEDGE_BASE_UPLOAD_DENIED)

    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")
    try:
        stored_name = safe_upload_name(file.filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ── Choose where to write the uploaded bytes ──────────────────────────────
    # GCP mode: write to a temp dir to preserve the exact filename
    # Local mode: persist to data/ directory for Chroma to read
    tmp_dir = None
    if settings.APP_MODE == AppMode.GCP:
        tmp_dir = tempfile.TemporaryDirectory()
        file_path = Path(tmp_dir.name) / stored_name
    else:
        local_dir = local_data_dir()
        local_dir.mkdir(parents=True, exist_ok=True)
        file_path = local_dir / stored_name

    try:
        # Write uploaded bytes to file_path
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        # 1. Pre-generate doc_id and register asset to satisfy FK constraint
        returned_doc_id = processor.loader.generate_doc_id(file_path)

        # In GCP mode, we record the GCS destination as source_path (file isn't kept locally)
        if settings.APP_MODE == AppMode.GCP:
            recorded_path = (
                f"gs://{settings.GCS_BUCKET_NAME}/{object_name('rag', stored_name)}"
            )
        else:
            recorded_path = file_path.as_uri()

        now = datetime.utcnow()
        asset = AssetMetadata(
            doc_id=returned_doc_id,
            owner=user.email,
            topic=topic,
            department=department,
            status=status,
            is_company_wide=is_company_wide,
            source_path=recorded_path,
            original_filename=file.filename,
            created_at=now,
            updated_at=now,
        )
        dam_registry.register_asset(asset)

        # 2. Process File (upload to GCS + import into RAG Corpus in GCP mode)
        manual_metadata = {
            "owner": user.email,
            "approver": "",
            "topic": topic,
            "department": department,
            "status": status,
            "is_company_wide": is_company_wide,
        }

        res_dict = processor.process_file(file_path, manual_metadata=manual_metadata)
        result = res_dict.get("result")

        if not result or not result.success:
            dam_registry.update_asset(returned_doc_id, {"status": "failed"})
            raise HTTPException(
                status_code=500,
                detail=f"Ingestion failed: {result.error if result else 'Unknown error'}",
            )

        log_action(user.email, "ingest", returned_doc_id, "success", manual_metadata)

        message = "Ingestion successful — file uploaded to Cloud Storage and queued for indexing"
        if settings.APP_MODE != AppMode.GCP:
            message = "Ingestion successful"

        return {"message": message, "chunks": result.count, "doc_id": returned_doc_id}

    except HTTPException:
        raise
    except Exception as e:
        log_action(
            user.email,
            "ingest",
            file.filename if file else "unknown",
            "failure",
            {"error": str(e)},
        )
        logger.exception(f"Exception in ingest: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        # GCP mode: delete the temp directory — the real copy is in GCS
        # Local mode: keep the file in data/ for Chroma
        if settings.APP_MODE == AppMode.GCP and tmp_dir:
            try:
                tmp_dir.cleanup()
                logger.debug(
                    "Deleted temp directory after GCS upload: %s", tmp_dir.name
                )
            except Exception:
                pass


@router.post("/meet-transcripts")
async def trigger_meet_sync(
    background_tasks: BackgroundTasks, user: UserDTO = Depends(get_current_user)
):
    """Trigger a background synchronization of Google Meet transcripts for all domain users."""
    reject_support_background_work()
    # RBAC: only admins can trigger the company-wide sync.
    if not user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="Only administrators can trigger global transcript synchronization.",
        )

    # Run sync for last 30 days when manually triggered — on the cubie-worker
    # Cloud Run Job (#9): the all-users sweep runs for minutes, which a
    # BackgroundTask on the CPU-throttled web service can silently never
    # finish.
    submit_background_work(
        background_tasks,
        module="rubik_core.scripts.sync_meet_transcripts",
        args=["--days", "30"],
        fallback=partial(run_sync, days_ago=30),
        label="manual-meet-transcript-sync-30d",
    )
    return {
        "message": "Global meet transcript synchronization started in background (30-day window)."
    }


@scheduler_router.post("/scheduler/sync")
async def scheduler_sync(
    background_tasks: BackgroundTasks,
    request: Request,
    x_scheduler_key: str | None = Header(None, alias="X-Scheduler-Key"),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    """Secure endpoint for Cloud Scheduler to trigger nightly transcript sync (7-day window)."""
    reject_support_background_work()
    # 1. Attempt to get user if credentials provided
    user = None
    if credentials:
        try:
            user = get_current_user(request, credentials)
        except Exception:
            pass

    # 2. Check if it's an authenticated admin user
    is_admin = bool(user and user.is_admin)

    # 3. Check if it's the Scheduler via API Key (constant-time compare, same
    # rationale as the other X-Scheduler-Key gates)
    is_scheduler = scheduler_key_valid(x_scheduler_key)

    if not is_admin and not is_scheduler:
        logger.warning(
            f"Unauthorized attempt to trigger scheduler sync. Key provided: {bool(x_scheduler_key)}"
        )
        raise HTTPException(
            status_code=403, detail="Unauthorized: Scheduler key missing or invalid."
        )

    # Nightly sync: 7 days lookback — on the cubie-worker Cloud Run Job (#9),
    # so the sweep isn't CPU-starved after this response returns.
    submit_background_work(
        background_tasks,
        module="rubik_core.scripts.sync_meet_transcripts",
        args=["--days", "7"],
        fallback=partial(run_sync, days_ago=7),
        label="nightly-meet-transcript-sync-7d",
    )
    logger.info("Cloud Scheduler triggered nightly meet transcript sync.")
    return {"message": "Nightly sync task started (7-day window)."}

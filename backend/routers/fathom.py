import asyncio
import hashlib
import json
import logging
import os
import shutil
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
    Response,
)
from fastapi.responses import JSONResponse
from sqlalchemy import func

from backend.dependencies import fathom_access_required
from backend.jobs.launcher import (
    reject_support_background_work,
    submit_background_work,
)
from backend.models.auth import User as UserDTO
from backend.routers.ingest import get_processor
from rubik_core.config import AppMode, settings
from rubik_core.dam.registry import AssetMetadata, DAMRegistry
from rubik_core.database.repository import SQLMeetingParticipantsRepository
from rubik_core.features import feature_allows
from rubik_core.ingestion.processor import IngestionProcessor
from rubik_core.integrations.fathom import (
    FathomClient,
    WebhookReplayGuard,
    verify_webhook_signature,
    webhook_event_id,
)
from rubik_core.tenancy.context import current_workspace

logger = logging.getLogger(__name__)

#: The ids of the events this instance accepted inside the replay window.
WEBHOOK_REPLAYS = WebhookReplayGuard()
MAX_WEBHOOK_BODY_BYTES = 256 * 1024


async def _bounded_webhook_body(request: Request) -> bytes:
    """Read public input incrementally before verifying its signature."""
    length = request.headers.get("content-length")
    if length:
        try:
            if int(length) > MAX_WEBHOOK_BODY_BYTES:
                raise HTTPException(status_code=413, detail="Webhook body too large")
        except ValueError:
            pass
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_WEBHOOK_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Webhook body too large")
        chunks.append(chunk)
    return b"".join(chunks)


async def require_fathom_signature(request: Request) -> None:
    """401 unless the request is an event Fathom signed (fails closed).

    The webhook receiver's only gate: Fathom sends no user session, so nothing
    else could admit it. Runs before the handler and its other dependencies,
    so a refused request costs no work. The raw body is read here; Starlette
    keeps it, so the handler reads the same bytes again.
    """
    if request.path_params.get("slug") != current_workspace():
        raise HTTPException(status_code=401, detail="Invalid signature")
    body = await _bounded_webhook_body(request)
    if not await asyncio.to_thread(verify_webhook_signature, body, request.headers):
        raise HTTPException(status_code=401, detail="Invalid signature")
    request.state.fathom_body = body


class DuplicateFathomEvent(Exception):
    """A signed event whose id this instance already accepted in the window."""


async def refuse_repeated_fathom_event(request: Request) -> AsyncIterator[None]:
    """Stop a repeat of an event already accepted here, before any work.

    Runs after ``require_fathom_signature``, so only a correctly signed event's
    id is ever remembered: a forged request cannot use up a real event's id.
    A repeat is answered 200 "duplicate" (:func:`duplicate_fathom_event`, which
    ``backend/main.py`` registers), so Fathom stops sending it.

    A yield dependency, so that an error raised after the id is remembered
    (building the handler's processor, say) forgets it again: Fathom retries a
    failed delivery under the same id, and that retry is work still to do.
    """
    event_id = webhook_event_id(request.headers) or ""
    replay_key = f"{current_workspace()}:{event_id}"
    if not WEBHOOK_REPLAYS.first_delivery(replay_key):
        logger.info("Fathom webhook %s was already accepted: ignored.", event_id)
        raise DuplicateFathomEvent(event_id)
    request.state.fathom_event_id = replay_key
    try:
        yield
    except Exception:
        WEBHOOK_REPLAYS.forget(replay_key)
        raise


async def duplicate_fathom_event(
    _request: Request, _exc: DuplicateFathomEvent
) -> JSONResponse:
    """The answer to a repeated event: 200, so Fathom stops retrying it."""
    return JSONResponse({"status": "duplicate"})


#: The signed-in routes (``/fathom/calls``, ``/fathom/ingest``). Mounted on the
#: copilot-gated ``core`` parent in ``backend/main.py``.
router = APIRouter(prefix="/fathom", tags=["fathom"])

#: Fathom's workspace URL receiver. Middleware binds the active URL workspace
#: before this router runs; its signature and replay checks are declared on the
#: router in that order, so no route can be added here without them. Any app
#: that mounts it must also
#: register :func:`duplicate_fathom_event` for :class:`DuplicateFathomEvent`, as
#: ``backend/main.py`` does, or a repeat becomes a 500 that Fathom keeps retrying.
webhook_router = APIRouter(
    prefix="/fathom/w/{slug}",
    tags=["fathom"],
    dependencies=[
        Depends(reject_support_background_work),
        Depends(require_fathom_signature),
        Depends(refuse_repeated_fathom_event),
    ],
)
dam_registry = DAMRegistry()


def _attach_to_call_capture(meeting: dict) -> None:
    """The Fathom fallback for Sales Calls — one recording → one captured call.

    Runs BEFORE this sync's own "already RAG-ingested" skip, on purpose: the
    recordings that predate ``fathom_attach`` are already in the corpus, and
    they are exactly the backfill the proposal generator needs. Its own DB session,
    its own try/except: a call-capture failure must never cost the RAG ingest
    that runs next to it, and vice versa.
    """
    if not settings.CCS_FATHOM_ENABLED:
        return
    try:
        from rubik_core.call_capture import fathom_attach
        from rubik_core.database.db import SessionLocal

        with SessionLocal() as db:
            outcome = fathom_attach.attach_meeting(db, meeting)
        logger.info(
            "[ccs-fathom] recording %s: %s (session=%s, entries=%s, company=%s)",
            outcome.get("recording_id"),
            outcome.get("outcome"),
            outcome.get("session_id"),
            outcome.get("entries_written"),
            outcome.get("company_id"),
        )
    except Exception:  # noqa: BLE001 — see the docstring
        logger.exception(
            "[ccs-fathom] recording %s could not be attached to call capture",
            meeting.get("id") or meeting.get("recording_id"),
        )


@dataclass(frozen=True)
class FathomSyncReport:
    status: str
    ingested: int = 0
    skipped: int = 0
    failed: int = 0


def process_fathom_transcripts_background(
    user_email: str, processor: IngestionProcessor, limit: int = 20
) -> FathomSyncReport:
    """
    Background job to pull recorded meetings from Fathom Video and ingest
    them into the RAG corpus, aligning with the pattern used for Google Drive Meetings.
    """
    ingested = skipped = failed = 0
    try:
        fathom_client = FathomClient()
        if not fathom_client.api_key:
            logger.warning(
                "Workspace Fathom API key is missing. Aborting background ingestion."
            )
            return FathomSyncReport(status="not_configured")

        meetings = fathom_client.list_meetings(
            limit=limit, include_transcript=True, raise_on_error=True
        )
        if not meetings:
            logger.info("No fathom meetings found or returned.")
            return FathomSyncReport(status="empty")

        logger.info(f"Found {len(meetings)} Fathom meetings for ingestion.")

        # Dedicated Company Meetings Corpus
        target_corpus_id = None
        if settings.APP_MODE == AppMode.GCP:
            # We use a single shared corpus for meetings
            from rubik_core.tenancy.corpora import corpus_id

            target_corpus_id = corpus_id("meetings", create=True)

        for meeting in meetings:
            # Identify the meeting
            meeting_id = meeting.get("id") or meeting.get("recording_id")
            if not meeting_id:
                skipped += 1
                continue

            filename = meeting.get("title", f"Fathom Meeting {meeting_id}")

            # Sales Calls: the recording becomes a captured call (wrap-up, SOW
            # clock, Draft proposal). Before the RAG skip below — see the helper.
            _attach_to_call_capture(meeting)

            # Deterministic doc_id
            doc_id = hashlib.sha256(f"fathom_{meeting_id}".encode()).hexdigest()

            # Incremental Sync: Check if this transcript is already ingested in SQL
            existing = dam_registry.get_asset(doc_id)
            if existing and existing.status != "failed":
                logger.info(f"Transcript '{filename}' already ingested, skipping.")
                skipped += 1
                continue
            # A failed asset is retryable. Its stable doc_id is upserted below.

            # Extract the transcript + Fathom's own action items & summary, so a
            # follow-up-email or "what did we cover" query can surface the call's
            # to-do's/next steps even past the transcript truncation window.
            text = fathom_client.build_ingest_text(meeting)
            if not text:
                logger.warning(
                    f"Could not extract text for {filename} (ID: {meeting_id}), skipping."
                )
                skipped += 1
                continue

            # Identify participants. ACL = the meeting's OWN attendees + host.
            # Do NOT blanket-add the batch-triggering user: list_meetings() is
            # account-wide, so adding the trigger to every pulled meeting would
            # grant one rep query access to other reps' calls. A rep can query a
            # Fathom meeting only if they actually attended/hosted it.
            participants = fathom_client.extract_participants_from_meeting(meeting)
            allowed_emails = {p.lower() for p in participants}
            allowed_emails_list = sorted(allowed_emails)

            # Owner = the rep who recorded the meeting, not the batch-triggering
            # user (list_meetings is account-wide). Falls back to the trigger if
            # the recorder is absent.
            meeting_owner = (meeting.get("recorded_by") or {}).get(
                "email"
            ) or user_email
            logger.info(
                f"Extracted participants for '{filename}': {allowed_emails_list}"
            )

            from rubik_core.tenancy.storage import local_data_dir, object_name

            if settings.APP_MODE == AppMode.GCP:
                source_uri = f"gs://{settings.GCS_BUCKET_NAME}/{object_name('rag', f'{doc_id}.txt')}"
            else:
                stored = local_data_dir() / "rag" / f"{doc_id}.txt"
                stored.parent.mkdir(parents=True, exist_ok=True)
                stored.write_text(text)
                source_uri = stored.as_uri()

            # Write text to a temporary file exactly named doc_id.txt so it uploads as that
            temp_dir = tempfile.mkdtemp()
            temp_path = Path(temp_dir) / f"{doc_id}.txt"
            with open(temp_path, "w") as f:
                f.write(text)

            try:
                now = datetime.utcnow()
                asset = AssetMetadata(
                    doc_id=doc_id,
                    owner=meeting_owner,
                    topic="Meeting Transcripts",
                    # No department: transcripts are participant-gated (see
                    # has_document_access + search_past_meetings), not
                    # department-scoped. "Internal" was a phantom label.
                    department=None,
                    status="approved",
                    is_company_wide=False,
                    source_path=source_uri,
                    allowed_emails=allowed_emails_list,
                    original_filename=f"{filename}.txt",
                    created_at=now,
                    updated_at=now,
                )
                dam_registry.register_asset(asset)

                # Authoritative participant ACL. Chat retrieval
                # (search_past_meetings) gates on meeting_participants — NOT on
                # Asset.allowed_emails (a vector-filter cache) — so without this
                # row the transcript is invisible to Q&A even for its host.
                # Mirrors transcript_sync.py.
                SQLMeetingParticipantsRepository().upsert_participants(
                    doc_id, meeting_id=str(meeting_id), emails=allowed_emails_list
                )

                manual_metadata = {
                    "owner": meeting_owner,
                    "topic": "Meeting Transcripts",
                    "status": "approved",
                    "is_company_wide": False,
                }

                res_dict = processor.process_file(
                    temp_path,
                    manual_metadata=manual_metadata,
                    target_corpus_id=target_corpus_id,
                )
                result = res_dict.get("result")

                if not result or not result.success:
                    # Adapter error text may echo a rejected credential header.
                    logger.error("Failed to ingest Fathom transcript %s", filename)
                    dam_registry.update_asset(doc_id, {"status": "failed"})
                    failed += 1
                else:
                    logger.info(f"Successfully ingested Fathom transcript {filename}")
                    # Meeting routines: queues rows only, never raises.
                    from rubik_core.meeting_skills.routines import (
                        on_transcript_ready,
                    )

                    on_transcript_ready(doc_id)
                    ingested += 1

                    # NOTE: Immediate GCS deletion removed to prevent race condition with async RAG import.
                    # Vertex AI RAG Engine requires the file to remain in GCS until ingestion is complete.
            except Exception:
                # The metadata row was inserted before import. Keep an
                # interrupted import retryable on the next scheduled poll.
                dam_registry.update_asset(doc_id, {"status": "failed"})
                raise
            finally:
                if os.path.exists(temp_dir):
                    shutil.rmtree(temp_dir)

        return FathomSyncReport(
            status="partial_failure" if failed else "ok",
            ingested=ingested,
            skipped=skipped,
            failed=failed,
        )
    except Exception as exc:
        logger.error(
            "Error processing Fathom transcripts for %s (%s)",
            user_email,
            type(exc).__name__,
        )
        return FathomSyncReport(
            status="error", ingested=ingested, skipped=skipped, failed=failed + 1
        )


def _host_may_auto_ingest(host_email: str) -> bool:
    """Is the meeting's host on the ``fathom_auto_ingest`` roster?

    The same gate as ``/fathom/ingest``, which sits under the copilot: both
    ``copilot`` and ``fathom_auto_ingest``, judged by the registry on the
    host's ``users`` row (roles loaded, so the admin door works). An address
    with no row is nobody's import.
    """
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.models import User

    with SessionLocal() as db:
        host = (
            db.query(User)
            .filter(
                func.lower(User.email) == host_email.strip().lower(),
                User.is_platform_support.is_(False),
            )
            .first()
        )
        return (
            host is not None
            and feature_allows("copilot", host, db)
            and feature_allows("fathom_auto_ingest", host, db)
        )


@webhook_router.post("/webhook")
async def fathom_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    processor: IngestionProcessor = Depends(get_processor),
):
    """Fathom's "meeting completed" event: import the call for its host.

    Only a correctly signed event, and only the first delivery of it inside
    the replay window, gets here (the dependencies on ``webhook_router``).
    """
    body = request.state.fathom_body

    try:
        data = json.loads(body)
        event_type = data.get("type")

        if event_type == "meeting.completed":
            meeting_id = data.get("meeting_id")
            if meeting_id:
                logger.info(
                    f"Fathom meeting completed: {meeting_id}. Triggering ingestion."
                )
                # A webhook has no user session: the meeting's host is whose
                # import this is. No host, no import (never a stand-in).
                meeting_data = data.get("meeting") or {}
                host_email = str(meeting_data.get("host_email") or "").strip()
                if not host_email:
                    logger.info(
                        "Fathom webhook skipped: meeting %s names no host.", meeting_id
                    )
                    return {"status": "ok", "message": "Ignored, no host."}

                # The same list as /fathom/ingest: Admin → Feature Access,
                # fathom_auto_ingest. Who imports is data, never a department.
                if not await asyncio.to_thread(_host_may_auto_ingest, host_email):
                    logger.info(
                        "Fathom webhook skipped: host %s is not on the Fathom "
                        "import list.",
                        host_email,
                    )
                    return {
                        "status": "ok",
                        "message": "Ignored, host not on the Fathom import list.",
                    }

                # Pull + ingest on the cubie-worker Cloud Run Job (#9): the
                # batch runs for minutes, which a BackgroundTask on the
                # CPU-throttled web service can silently never finish after
                # this webhook's 200 returns.
                await asyncio.to_thread(
                    submit_background_work,
                    background_tasks,
                    module="backend.jobs.fathom_sync_job",
                    args=["--user-email", host_email],
                    fallback=partial(
                        process_fathom_transcripts_background, host_email, processor
                    ),
                    label=f"fathom-webhook-sync:{host_email}",
                    env={"CUBIE_WORKSPACE": current_workspace()},
                )

        return {"status": "ok"}
    except Exception as e:
        logger.exception(f"Error processing Fathom webhook: {e}")
        # Fathom retries a failed delivery under the same id: let it through.
        WEBHOOK_REPLAYS.forget(getattr(request.state, "fathom_event_id", ""))
        return Response(status_code=500, content="Internal processing error")


@router.get("/calls")
async def get_fathom_calls(user: UserDTO = Depends(fathom_access_required)):
    """
    Returns a list of Fathom calls.
    Optionally, it could retrieve them directly from the API.
    """
    client = FathomClient()
    if not client.api_key:
        raise HTTPException(
            status_code=500, detail="Fathom is not connected to this workspace"
        )

    meetings = client.list_meetings(limit=20, include_transcript=False)
    return {"calls": meetings}


@router.post("/ingest", dependencies=[Depends(reject_support_background_work)])
async def ingest_fathom_transcripts(
    background_tasks: BackgroundTasks,
    user: UserDTO = Depends(fathom_access_required),
    processor: IngestionProcessor = Depends(get_processor),
):
    """
    Kicks off a background job to pull fathom transcripts and ingest them into the shared meeting corpus.

    The app calls this when someone on the ``fathom_auto_ingest`` roster opens
    it (Admin → Feature Access; ships off), and only for them.
    """
    if not await asyncio.to_thread(feature_allows, "fathom_auto_ingest", user):
        raise HTTPException(
            status_code=403,
            detail="Fathom import is not turned on for you. Ask your admin.",
        )

    # Same durable executor as the webhook path (#9).
    await asyncio.to_thread(
        submit_background_work,
        background_tasks,
        module="backend.jobs.fathom_sync_job",
        args=["--user-email", user.email],
        fallback=partial(process_fathom_transcripts_background, user.email, processor),
        label=f"fathom-manual-sync:{user.email}",
        env={"CUBIE_WORKSPACE": current_workspace()},
    )
    return {
        "status": "accepted",
        "message": "Fathom transcripts ingestion started in the background.",
    }

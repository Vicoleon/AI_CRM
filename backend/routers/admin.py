from __future__ import annotations

# backend/routers/admin.py
"""
Admin-only endpoints for index maintenance and diagnostics.
"""
import logging
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from backend.auth import get_current_user
from backend.models.auth import User
from rubik_core.brand import PRODUCT_NAME
from rubik_core.config import AppMode, settings
from rubik_core.database.db import get_db
from rubik_core.database.models import AuditLog, InteractionLog, LoginEvent
from rubik_core.models import get_embeddings
from rubik_core.tenancy.scheduler import scheduler_key_valid

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


async def _dispatch_transcript_job(args: list[str]) -> dict:
    from starlette.concurrency import run_in_threadpool

    from backend.jobs.launcher import JobLaunchError, launch_module_execution

    try:
        execution = await run_in_threadpool(
            launch_module_execution, "rubik_core.scripts.transcript_sync", args
        )
    except JobLaunchError as exc:
        logger.exception("Transcript Job launch failed")
        raise HTTPException(
            status_code=502, detail="Transcript Job launch failed"
        ) from exc
    return {
        "status": "dispatched",
        "execution": execution.get("name")
        or (execution.get("metadata") or {}).get("name"),
    }


@router.post("/reindex")
async def reindex(
    user: User = Depends(get_current_user),
):
    """
    Re-embed every chunk stored in Cloud SQL and push the vectors into
    the Vertex AI Vector Search index via streaming upsert.

    This is the fix for the "index is empty" scenario: if documents were
    ingested before streaming upserts were enabled, their embeddings
    never reached the live index.  This endpoint back-fills them.

    Admin-only.
    """
    if not getattr(user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin access required")

    from rubik_core.config import AppMode

    if settings.APP_MODE == AppMode.GCP:
        from rubik_core.adapters.vertex_rag import VertexRagAdapter

        adapter = VertexRagAdapter()
    else:
        from rubik_core.adapters.chroma import ChromaAdapter

        adapter = ChromaAdapter()

    if not hasattr(adapter, "reindex_from_sql"):
        raise HTTPException(
            status_code=400,
            detail="reindex is only supported on the Vertex adapter (GCP mode)",
        )

    embeddings_model = get_embeddings()
    result = adapter.reindex_from_sql(embeddings_model)

    if result.get("success"):
        return {
            "status": "ok",
            "reindexed": result.get("reindexed", 0),
            "total_chunks": result.get("total_chunks", 0),
            "message": result.get("message", "Reindex complete"),
        }
    else:
        raise HTTPException(
            status_code=500, detail=result.get("error", "Reindex failed")
        )


@router.post("/repair-assets")
async def repair_assets(user: User = Depends(get_current_user)):
    """
    One-time repair: backfill original_filename from source_path for any Asset
    rows where original_filename is NULL.

    These show as "Recovered Document" (or a hex-hash stub) in the Assets UI
    because the original CLI ingest script didn't save the filename.

    Admin-only.
    """
    if not getattr(user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin access required")

    from rubik_core.database.db import SessionLocal
    from rubik_core.database.models import Asset

    db = SessionLocal()
    repaired = 0
    skipped = 0
    try:
        # Find all assets missing original_filename
        assets_to_fix = (
            db.query(Asset)
            .filter(
                (Asset.original_filename.is_(None)) | (Asset.original_filename == "")
            )
            .all()
        )

        for asset in assets_to_fix:
            source_path = asset.source_path or ""
            if source_path:
                # Derive filename from the stored path
                derived_name = source_path.split("/")[-1]
                asset.original_filename = derived_name
                repaired += 1
                logger.info(
                    "Repaired asset %s → original_filename=%s",
                    asset.doc_id,
                    derived_name,
                )
            else:
                skipped += 1
                logger.warning(
                    "Asset %s has no source_path — cannot repair", asset.doc_id
                )

        db.commit()
    except Exception as e:
        db.rollback()
        logger.error("repair-assets failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Repair failed: {e}")
    finally:
        db.close()

    return {
        "status": "ok",
        "repaired": repaired,
        "skipped": skipped,
        "message": (
            f"Backfilled original_filename for {repaired} assets. "
            f"{skipped} assets had no source_path and were skipped."
        ),
    }


class InteractionLogResponse(BaseModel):
    id: str
    session_id: str | None = None
    user_email: str
    timestamp: datetime
    user_input: str
    system_prompt: str | None = None
    llm_output: str | None = None
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    duration_ms: int
    successful: bool
    error_message: str | None = None

    class Config:
        from_attributes = True


@router.get("/interactions", response_model=list[InteractionLogResponse])
async def list_interactions(
    limit: int = 50,
    offset: int = 0,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Retrieve interaction logs (Audit Logs) for the chat endpoints.
    Allows PMs and Admins to audit inputs, outputs, and token usage.
    """
    if not getattr(user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin access required")

    logs = (
        db.query(InteractionLog)
        .order_by(desc(InteractionLog.timestamp))
        .offset(offset)
        .limit(limit)
        .all()
    )
    return logs


class LoginEventResponse(BaseModel):
    id: int
    user_email: str
    timestamp: datetime
    ip_address: str | None = None
    user_agent: str | None = None

    class Config:
        from_attributes = True


@router.get("/logins", response_model=list[LoginEventResponse])
async def list_logins(
    limit: int = 200,
    offset: int = 0,
    email: str | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Login activity log — who signed in and when (Admin only).

    Each row is one login *session*: ``get_current_user`` runs on every
    request, so writes are debounced server-side (see ``LoginEvent``), and
    this list is therefore sessions, not raw request counts. Newest first;
    optionally filter to a single ``email``.
    """
    if not getattr(user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin access required")

    query = db.query(LoginEvent)
    if email:
        query = query.filter(LoginEvent.user_email == email)
    return (
        query.order_by(desc(LoginEvent.timestamp))
        .offset(offset)
        .limit(min(limit, 1000))
        .all()
    )


class AuditLogResponse(BaseModel):
    id: int
    timestamp: datetime
    actor_email: str
    action: str
    doc_id: str | None = None
    status: str
    details: dict | None = None

    class Config:
        from_attributes = True


@router.get("/audit", response_model=list[AuditLogResponse])
async def list_audit_logs(
    limit: int = 200,
    offset: int = 0,
    actor: str | None = None,
    action: str | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Audit trail of privileged actions — role/permission changes, account
    create/delete, workspace-auth settings, etc. (Admin only, SOC 2 H-5/CC4.1).

    This is the reader for the ``audit_logs`` table those actions write to;
    without it the trail was write-only. Newest first; optionally filter by
    ``actor`` (email) or ``action`` (e.g. ``USER_UPDATE``).
    """
    if not getattr(user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin access required")

    query = db.query(AuditLog)
    if actor:
        query = query.filter(AuditLog.actor_email == actor)
    if action:
        query = query.filter(AuditLog.action == action)
    rows = (
        query.order_by(desc(AuditLog.timestamp))
        .offset(offset)
        .limit(min(limit, 1000))
        .all()
    )
    responses = []
    for row in rows:
        response = AuditLogResponse.model_validate(row)
        details = row.details
        if (row.action or "").upper() in {
            "GMAIL_ACCOUNT_CONNECTED",
            "GMAIL_ACCOUNT_DISCONNECTED",
        } or (isinstance(details, dict) and details.get("account_id") is not None):
            # The action and actor remain auditable. Connected mailbox identity
            # and account correlation stay private to the owner.
            response.details = None
        responses.append(response)
    return responses


@router.post("/sync-rag")
async def sync_rag(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    """
    Synchronize the SQL database assets with the Vertex AI RAG corpus.
    Finds assets with local paths, uploads to GCS, and imports to RAG.
    Also finds GCS assets not present in RAG and triggers import.
    """
    if not getattr(user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin access required")

    if settings.APP_MODE != AppMode.GCP:
        raise HTTPException(
            status_code=400, detail="sync-rag is only supported in GCP mode"
        )

    from rubik_core.adapters.vertex_rag import VertexRagAdapter
    from rubik_core.rag.manager import RagManager
    from rubik_core.tenancy.corpora import assert_own_rag_file
    from rubik_core.tenancy.storage import assert_own_uri

    adapter = VertexRagAdapter()
    manager = RagManager()

    if type(adapter).__name__ != "VertexRagAdapter":
        raise HTTPException(status_code=400, detail="VertexRagAdapter is required")

    from vertexai.preview import rag

    from rubik_core.database.models import Asset, AssetVersion

    indexed_by_corpus: dict[str, set[str]] = {}

    def indexed_uris(corpus: str) -> set[str]:
        if corpus not in indexed_by_corpus:
            try:
                uris: set[str] = set()
                for file in rag.list_files(corpus_name=corpus):
                    assert_own_rag_file(file.name, corpus)
                    sources = getattr(getattr(file, "gcs_source", None), "uris", None)
                    if not sources:
                        raise ValueError("RAG file has no source")
                    uris.update(assert_own_uri(uri) for uri in sources)
                indexed_by_corpus[corpus] = uris
            except Exception as exc:
                logger.error("Failed to list RAG files in bound corpus: %s", exc)
                raise HTTPException(
                    status_code=500, detail="Failed to list RAG files"
                ) from exc
        return indexed_by_corpus[corpus]

    assets = db.query(Asset).filter(Asset.status == "approved").all()

    uploaded_count = 0
    imported_count = 0
    failed_count = 0

    for asset in assets:
        source_path = asset.source_path or ""
        try:
            target_corpus = manager.corpus_for_asset(
                asset.department, bool(asset.is_company_wide)
            )
        except Exception:
            logger.warning("sync-rag skipped asset without a bound audience")
            failed_count += 1
            continue
        known_uris = indexed_uris(target_corpus)

        if not source_path.startswith("gs://"):
            # Local file that hasn't been uploaded to GCS yet
            try:
                local_path = Path(assert_own_uri(source_path))
            except ValueError:
                logger.warning("sync-rag refused a local source outside workspace")
                asset.status = "failed"
                failed_count += 1
                continue
            if local_path.exists():
                logger.info(f"Uploading local asset {asset.doc_id} to GCS...")
                try:
                    gcs_uri = adapter.upload_and_import(local_path, target_corpus)
                    asset.source_path = gcs_uri
                    version = (
                        db.query(AssetVersion)
                        .filter(
                            AssetVersion.doc_id == asset.doc_id,
                            AssetVersion.version_number == asset.current_version,
                        )
                        .one_or_none()
                    )
                    if version is not None:
                        version.source_path = gcs_uri
                    known_uris.add(gcs_uri)

                    uploaded_count += 1
                    imported_count += 1
                except Exception as e:
                    logger.error(f"Failed to sync local asset {asset.doc_id}: {e}")
            else:
                logger.warning(
                    f"Local file missing for asset {asset.doc_id}: {source_path}"
                )
                asset.status = "failed"
                failed_count += 1
        else:
            # GCS file, check if it's imported in Vertex RAG
            try:
                assert_own_uri(source_path)
            except ValueError:
                logger.warning("sync-rag refused a GCS source outside workspace")
                asset.status = "failed"
                failed_count += 1
                continue
            if source_path not in known_uris:
                logger.info(
                    f"Asset {asset.doc_id} in GCS but missing in RAG. Importing..."
                )
                try:
                    adapter.import_gcs_uri_to_corpus(source_path, target_corpus)
                    known_uris.add(source_path)
                    imported_count += 1
                except Exception as e:
                    logger.error(
                        f"Failed to import existing GCS asset {asset.doc_id} to RAG: {e}"
                    )

    # Commit any status or source_path updates
    db.commit()

    return {
        "status": "ok",
        "uploaded_to_gcs": uploaded_count,
        "imported_to_rag": imported_count,
        "marked_failed": failed_count,
        "total_approved_assets": len(assets),
    }


@router.post("/transcript-sync")
async def trigger_transcript_sync(
    days: int = 1,
    users: str | None = None,
    x_scheduler_key: str | None = Header(default=None, alias="X-Scheduler-Key"),
):
    """Trigger a Google Meet transcript sync run.

    Designed to be called by Cloud Scheduler every 15 minutes (with `days=1`
    for safe overlap). Also callable manually by anyone holding the secret.

    Auth: `X-Scheduler-Key` must match the configured scheduler key. A named
    `X-Cubie-Workspace` selects one active workspace; without it the middleware
    runs a bounded fan-out over active workspaces.

    Idempotent: transcripts already ingested or with `status="deleted"` are
    skipped, so safe to retry.

    Args:
        days: Look back this many days (default 1).
        users: Optional comma-separated list of emails to sync (default: all
            domain users via Admin SDK).
    """
    if not scheduler_key_valid(x_scheduler_key):
        raise HTTPException(status_code=401, detail="Invalid scheduler key")

    logger.info(
        "transcript-sync triggered via X-Scheduler-Key (days=%d, users=%s)",
        days,
        users or "all",
    )

    user_list = [u.strip() for u in users.split(",")] if users else None
    args = ["--days", str(days)]
    if user_list:
        args += ["--users", *user_list]
    return await _dispatch_transcript_job(args)


@router.post("/transcript-acl-backfill")
async def trigger_transcript_acl_backfill(
    users: str | None = None,
    x_scheduler_key: str | None = Header(default=None, alias="X-Scheduler-Key"),
):
    """Repair the participant ACL on transcripts that are already ingested.

    The old sync derived who-attended by scraping the transcript text, so
    attendees who never spoke were dropped and couldn't see/query meetings they
    actually attended (and scraped placeholder emails like user@example.com were
    wrongly added). This reconciles each transcript's ACL to the calendar invite
    list — adding silently-excluded attendees and removing placeholder/reserved
    addresses — without re-ingesting anything.

    One-shot / occasional repair, not on a schedule. Auth mirrors
    ``/transcript-sync``: the ``X-Scheduler-Key`` must match the configured
    scheduler key. Idempotent and safe to re-run.

    Args:
        users: Optional comma-separated owner emails to scope the backfill to
            (default: every approved transcript).
    """
    if not scheduler_key_valid(x_scheduler_key):
        raise HTTPException(status_code=401, detail="Invalid scheduler key")

    logger.info(
        "transcript-acl-backfill triggered via X-Scheduler-Key (users=%s)",
        users or "all",
    )

    user_list = [u.strip() for u in users.split(",")] if users else None
    args = ["--backfill"]
    if user_list:
        args += ["--users", *user_list]
    return await _dispatch_transcript_job(args)


# ── AI Cost tracking ─────────────────────────────────────────────────────
# Feature-level token + dollar accounting for every Gemini call in Cubie.
# Backed by ``llm_usage_events`` (written best-effort by rubik_core.cost.usage)
# and the rate card in rubik_core.cost.pricing. Admin-only.

# Human-friendly labels + display order for the feature keys emitted by the
# recorder. Anything not listed still shows, title-cased, at the end.
_FEATURE_LABELS: dict[str, str] = {
    "chat": "Chat (Ask/Draft/Investigate)",
    "chat_impersonation": "Chat while impersonating (HubSpot only)",
    "ai_priorities": "AI Priorities",
    "ai_provider_connection_test": "AI provider connection test",
    "scheduled_tasks": "Scheduled Tasks",
    "meeting_skills": "Meeting Skills (templates, skills, routines)",
    "todos": "My To-Dos",
    "voice_input": "Voice input (dictation)",
    "voice_commands": "Voice commands",
    "chat_file_upload": f"Files in Ask {PRODUCT_NAME} (reading attachments)",
    "promise_accountability": "Promise follow-through",
    "meeting_prep": "Meeting Prep",
    "meeting_prep_section": "Meeting Prep: section rewrites",
    "deep_prep": "Deep Prep",
    "customer_xray": "Customer X-Ray",
    "eos": "EOS (Rocks/Issues/To-Dos)",
    "eos_rock_carryover": "EOS Rock carry-over (AI re-draft)",
    "l10_recap": "L10 Meeting Recap",
    "l10_summary_email": "L10 Recap Email",
    "slack": "Slack Summaries",
    "email": "Email & Gmail",
    "open_loops": "What Am I Waiting On (Gmail + Slack sweep)",
    "loop_closure": "Loose Ends (morning scan + reply drafts)",
    "nightly_pipeline": "Overnight Pipeline",
    "signals": "Signals Judge",
    "dam": "Digital Asset Manager",
    "ooo_handoff": "OOO Handoff Brief",
    "offboard_handoff": "Offboard Handoff",
    "call_capture_extraction": "Sales Call Capture",
    "crm_sync": "CRM Sync",
    "proposal_generator": "Proposal Generator",
    "smart_teams": "Smart Teams (Work Style Profiles)",
    "ingestion": "RAG Ingestion (embeddings)",
    "embedding_index_rebuild": "Embedding index rebuild",
    "embedding_index_sync": "Embedding index document sync",
    "embedding_index_query": "Embedding index search",
    "sales_email_agent": "Sales email drafting",
    "preferences_preview": "Email style preview",
    "ingestion_council": "Ingestion Council",
    "web_search": "Web Search",
    "hubspot_retrieval": "HubSpot Retrieval",
    "health_check": "Model Health Check",
    "insights_summary": "Insights summaries",
    "unattributed": "Unattributed",
}


def _unattributed_origins(base_query) -> list[dict]:
    """Break the "unattributed" bucket down by the code path that produced it.

    ``rubik_core.cost.usage`` stamps ``metadata_json.origin`` (a
    ``module.function:lineno``) on every untagged event precisely so this row
    is never a dead end: an untagged path names itself here, and tagging it is
    then a one-line ``@attribute_to``. Rows written before origin-stamping
    shipped have no breadcrumb and group under a single
    "(no origin recorded)" entry.

    Aggregated in Python rather than SQL because ``metadata_json`` is a plain
    JSON column and JSON-path extraction differs between Postgres and the
    SQLite used by tests. The row count is bounded by the report window and
    only three narrow columns are fetched.
    """
    from rubik_core.database.models import LLMUsageEvent

    rows = base_query.with_entities(
        LLMUsageEvent.metadata_json,
        LLMUsageEvent.cost_usd,
        LLMUsageEvent.total_tokens,
    ).filter(LLMUsageEvent.feature == "unattributed")

    agg: dict[str, dict] = {}
    for meta, cost, tokens in rows:
        origin = (meta or {}).get("origin") or "(no origin recorded)"
        entry = agg.setdefault(
            origin, {"origin": origin, "cost_usd": 0.0, "total_tokens": 0, "calls": 0}
        )
        entry["cost_usd"] += float(cost or 0.0)
        entry["total_tokens"] += int(tokens or 0)
        entry["calls"] += 1

    out = sorted(agg.values(), key=lambda x: x["cost_usd"], reverse=True)
    for entry in out:
        entry["cost_usd"] = round(entry["cost_usd"], 6)
    return out


def _feature_label(key: str) -> str:
    return _FEATURE_LABELS.get(key) or key.replace("_", " ").title()


@router.get("/ai-cost")
async def ai_cost_summary(
    days: int = 30,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Token + dollar spend for every AI feature over the last ``days`` days.

    Returns totals, a per-feature breakdown, a per-model breakdown and a daily
    time series, all computed from ``llm_usage_events``. In-memory events are
    flushed first so the numbers are never stale. Admin-only.

    ``thinking_tokens`` is the models' thinking, already priced into
    ``cost_usd`` at the output rate and kept out of ``completion_tokens``.
    Rows written before it was captured carry no count, so the totals also say
    how many of the range's generate calls were measured with it
    (``thinking_measured_calls`` of ``generate_calls``).
    """
    if not getattr(user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin access required")

    # Land any buffered events so the report reflects in-flight usage.
    try:
        from rubik_core.cost.usage import flush_usage

        flush_usage()
    except Exception:  # noqa: BLE001 — a flush hiccup must not 500 the report
        logger.debug("ai-cost pre-flush failed", exc_info=True)

    days = max(1, min(int(days or 30), 365))
    cutoff = datetime.utcnow() - timedelta(days=days)

    from rubik_core.cost.usage import THINKING_TOKENS_KEY, _tracking_enabled
    from rubik_core.database.models import LLMUsageEvent

    empty = {
        "range_days": days,
        "since": cutoff.isoformat() + "Z",
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "tracking_enabled": _tracking_enabled(),
        "totals": {
            "cost_usd": 0.0,
            "total_tokens": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "thinking_tokens": 0,
            "cached_tokens": 0,
            "calls": 0,
            "generate_calls": 0,
            "thinking_measured_calls": 0,
        },
        "features": [],
        "providers": [],
        "models": [],
        "daily": [],
        "unattributed_origins": [],
    }

    try:
        base = db.query(LLMUsageEvent).filter(LLMUsageEvent.timestamp >= cutoff)
        # JSON key, not a column: see THINKING_TOKENS_KEY. NULL on rows from
        # before thinking was captured, so count() is the measured-call count.
        thinking = LLMUsageEvent.metadata_json[THINKING_TOKENS_KEY].as_integer()

        totals_row = base.with_entities(
            func.coalesce(func.sum(LLMUsageEvent.cost_usd), 0.0),
            func.coalesce(func.sum(LLMUsageEvent.total_tokens), 0),
            func.coalesce(func.sum(LLMUsageEvent.prompt_tokens), 0),
            func.coalesce(func.sum(LLMUsageEvent.completion_tokens), 0),
            func.coalesce(func.sum(LLMUsageEvent.cached_tokens), 0),
            func.count(LLMUsageEvent.id),
            func.coalesce(func.sum(thinking), 0),
            func.count(thinking),
        ).one()
        generate_calls = (
            base.filter(LLMUsageEvent.operation == "generate")
            .with_entities(func.count(LLMUsageEvent.id))
            .scalar()
        )

        totals = {
            "cost_usd": round(float(totals_row[0] or 0.0), 6),
            "total_tokens": int(totals_row[1] or 0),
            "prompt_tokens": int(totals_row[2] or 0),
            "completion_tokens": int(totals_row[3] or 0),
            "thinking_tokens": int(totals_row[6] or 0),
            "cached_tokens": int(totals_row[4] or 0),
            "calls": int(totals_row[5] or 0),
            "generate_calls": int(generate_calls or 0),
            "thinking_measured_calls": int(totals_row[7] or 0),
        }

        feature_rows = (
            base.with_entities(
                LLMUsageEvent.feature,
                func.coalesce(func.sum(LLMUsageEvent.cost_usd), 0.0),
                func.coalesce(func.sum(LLMUsageEvent.total_tokens), 0),
                func.coalesce(func.sum(LLMUsageEvent.prompt_tokens), 0),
                func.coalesce(func.sum(LLMUsageEvent.completion_tokens), 0),
                func.count(LLMUsageEvent.id),
                func.coalesce(func.sum(thinking), 0),
            )
            .group_by(LLMUsageEvent.feature)
            .all()
        )
        features = sorted(
            (
                {
                    "feature": r[0] or "unattributed",
                    "label": _feature_label(r[0] or "unattributed"),
                    "cost_usd": round(float(r[1] or 0.0), 6),
                    "total_tokens": int(r[2] or 0),
                    "prompt_tokens": int(r[3] or 0),
                    "completion_tokens": int(r[4] or 0),
                    "thinking_tokens": int(r[6] or 0),
                    "calls": int(r[5] or 0),
                }
                for r in feature_rows
            ),
            key=lambda x: x["cost_usd"],
            reverse=True,
        )

        model_rows = (
            base.with_entities(
                LLMUsageEvent.model,
                func.coalesce(func.sum(LLMUsageEvent.cost_usd), 0.0),
                func.coalesce(func.sum(LLMUsageEvent.total_tokens), 0),
                func.count(LLMUsageEvent.id),
            )
            .group_by(LLMUsageEvent.model)
            .all()
        )
        models = sorted(
            (
                {
                    "model": r[0] or "(unknown)",
                    "cost_usd": round(float(r[1] or 0.0), 6),
                    "total_tokens": int(r[2] or 0),
                    "calls": int(r[3] or 0),
                }
                for r in model_rows
            ),
            key=lambda x: x["cost_usd"],
            reverse=True,
        )

        provider_col = func.coalesce(
            LLMUsageEvent.metadata_json["provider"].as_string(), "vertex_managed"
        )
        billing_col = func.coalesce(
            LLMUsageEvent.metadata_json["billing_owner"].as_string(), "cubie"
        )
        provider_rows = (
            base.with_entities(
                provider_col,
                billing_col,
                func.coalesce(func.sum(LLMUsageEvent.cost_usd), 0.0),
                func.coalesce(func.sum(LLMUsageEvent.total_tokens), 0),
                func.count(LLMUsageEvent.id),
            )
            .group_by(provider_col, billing_col)
            .all()
        )
        providers = sorted(
            (
                {
                    "provider": row[0],
                    "billing_owner": row[1],
                    "cost_usd": round(float(row[2] or 0.0), 6),
                    "total_tokens": int(row[3] or 0),
                    "calls": int(row[4] or 0),
                }
                for row in provider_rows
            ),
            key=lambda item: item["cost_usd"],
            reverse=True,
        )

        # Daily series — func.date() works on both SQLite and Postgres.
        day_col = func.date(LLMUsageEvent.timestamp)
        daily_rows = (
            base.with_entities(
                day_col,
                func.coalesce(func.sum(LLMUsageEvent.cost_usd), 0.0),
                func.coalesce(func.sum(LLMUsageEvent.total_tokens), 0),
                func.count(LLMUsageEvent.id),
            )
            .group_by(day_col)
            .order_by(day_col)
            .all()
        )
        daily = [
            {
                "date": str(r[0]),
                "cost_usd": round(float(r[1] or 0.0), 6),
                "total_tokens": int(r[2] or 0),
                "calls": int(r[3] or 0),
            }
            for r in daily_rows
        ]

        # Only pay for the origin breakdown when there's actually untagged
        # spend to explain.
        has_unattributed = any(
            f["feature"] == "unattributed" and f["calls"] for f in features
        )
        origins = _unattributed_origins(base) if has_unattributed else []

        return {
            "range_days": days,
            "since": cutoff.isoformat() + "Z",
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "tracking_enabled": _tracking_enabled(),
            "totals": totals,
            "features": features,
            "providers": providers,
            "models": models,
            "daily": daily,
            "unattributed_origins": origins,
        }
    except Exception:  # noqa: BLE001
        # Most likely the table isn't migrated yet — degrade to empty rather
        # than 500 so the tab renders with a "no data yet" state.
        logger.warning("ai-cost summary query failed (table missing?)", exc_info=True)
        return {**empty, "table_ready": False}


@router.get("/ai-cost/rate-card")
async def ai_cost_rate_card(user: User = Depends(get_current_user)):
    """The resolved per-model rate card (USD per 1M tokens) used to compute
    dollar costs, plus the fallback rate for unpriced models. Admin-only.

    These are best-effort estimates of provider pricing; correct them in
    ``rubik_core/cost/pricing.py`` or via the ``LLM_PRICING_JSON`` env var.
    """
    if not getattr(user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin access required")

    from rubik_core.cost.pricing import _FALLBACK_RATE, get_rate_card

    card = get_rate_card()
    rates = [
        {
            "model": model_id,
            "input_per_1m": entry.get("input", 0.0),
            "output_per_1m": entry.get("output", 0.0),
            "cached_input_per_1m": entry.get("cached_input", 0.0),
        }
        for model_id, entry in sorted(card.items())
    ]
    return {
        "estimated": True,
        "unit": "USD per 1,000,000 tokens",
        "rates": rates,
        "fallback": {
            "input_per_1m": _FALLBACK_RATE.get("input", 0.0),
            "output_per_1m": _FALLBACK_RATE.get("output", 0.0),
            "cached_input_per_1m": _FALLBACK_RATE.get("cached_input", 0.0),
        },
    }

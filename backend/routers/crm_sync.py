"""UC-46 CRM Sync — scan, review, approve.

Cubie reads an AE's recent external call transcripts, compares them against
HubSpot, and queues proposed record changes. This router is how an AE triggers
a scan, reads the queue, and approves or dismisses items one at a time.

Two access gates apply: this router's own ``require_crm_sync`` (Sales + admins,
plus the feature flag) and the pilot gate on the ``core`` parent it is mounted
under. The narrow one here is the effective rule.

Every suggestion query filters on the caller's own email in the WHERE clause
rather than fetching-then-checking, so another user's suggestion id simply does
not resolve — there is no code path where an ownership check can be forgotten.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from backend.auth import require_crm_sync
from backend.jobs.launcher import reject_support_background_work
from rubik_core.auth.registry import UserProfile
from rubik_core.config import settings
from rubik_core.crm_sync import apply as apply_mod
from rubik_core.crm_sync import run_state, service, validators
from rubik_core.database.db import get_db
from rubik_core.database.models import CrmSyncSuggestion
from rubik_core.retrieval.hubspot_api import HubSpotAPIClient
from rubik_core.tenancy.hubspot_config import get_hubspot_config

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/crm-sync",
    tags=["crm-sync"],
    dependencies=[Depends(require_crm_sync)],
)

# A claimed row that never reached a terminal state means the applying process
# died mid-write. Long enough to never race a live write, short enough that the
# card doesn't look stuck.
_APPLYING_STALE_SECONDS = 300


class ScanRequest(BaseModel):
    since_days: int | None = Field(default=None, ge=1, le=90)


class ApproveRequest(BaseModel):
    edited_after: dict[str, Any] | None = None


class DismissRequest(BaseModel):
    reason: str | None = None


def _serialize(row: CrmSyncSuggestion) -> dict:
    payload = row.payload or {}
    return {
        "id": row.id,
        "run_id": row.run_id,
        "change_type": row.change_type,
        "hubspot_object_type": row.hubspot_object_type,
        "hubspot_object_id": row.hubspot_object_id,
        "status": row.status,
        "confidence": row.confidence,
        "error": row.error,
        "properties": {
            name: entry
            for name, entry in (payload.get("properties") or {}).items()
            if isinstance(entry, dict)
        },
        "contact_email": payload.get("contact_email"),
        "rationale": payload.get("rationale"),
        "evidence": row.evidence or {},
        "meeting": {
            "doc_id": row.doc_id,
            "title": row.meeting_title,
            "date": row.meeting_date.isoformat() if row.meeting_date else None,
        },
        "applied_at": row.applied_at.isoformat() if row.applied_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _visible_suggestion_keys(
    rows: list[CrmSyncSuggestion], pipeline_ids: tuple[str, ...]
) -> set[tuple[str, str]]:
    """Prove stored CRM identities against today's workspace pipeline boundary."""
    visible: set[tuple[str, str]] = set()
    if not pipeline_ids:
        return visible
    deal_ids = sorted(
        {
            str(row.hubspot_object_id)
            for row in rows
            if row.change_type == "update_deal" and row.hubspot_object_id
        }
    )
    contact_ids = sorted(
        {
            str(row.hubspot_object_id)
            for row in rows
            if row.change_type == "update_contact" and row.hubspot_object_id
        }
    )
    if not deal_ids and not contact_ids:
        return visible
    try:
        hubspot = HubSpotAPIClient()
        if deal_ids:
            visible.update(
                ("deal", str(item["id"]))
                for item in hubspot.batch_read("deals", deal_ids, ["pipeline"])
                if (item.get("properties") or {}).get("pipeline") in pipeline_ids
            )
        if contact_ids:
            visible.update(
                ("contact", contact_id)
                for contact_id, deal_ids in hubspot.fully_owned_deal_associations(
                    "contacts", contact_ids
                ).items()
                if deal_ids
            )
    except Exception as exc:  # noqa: BLE001 — never reveal an unproven stored row
        logger.warning("CRM suggestion ownership check unavailable")
        raise HTTPException(
            status_code=503, detail="CRM suggestions are unavailable"
        ) from exc
    return visible


def _require_live_suggestion(
    db: Session, suggestion_id: str, actor_email: str
) -> CrmSyncSuggestion:
    """Guard actions by ID with the same live proof as the list route."""
    row = (
        db.query(CrmSyncSuggestion)
        .filter(CrmSyncSuggestion.id == suggestion_id)
        .filter(CrmSyncSuggestion.user_email == actor_email.lower())
        .filter(CrmSyncSuggestion.change_type.in_(validators.CHANGE_TYPES))
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Suggestion not found")
    try:
        config = get_hubspot_config()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503, detail="CRM suggestions are unavailable"
        ) from exc
    key = (row.hubspot_object_type, str(row.hubspot_object_id))
    if key not in _visible_suggestion_keys([row], config.pipeline_ids):
        raise HTTPException(status_code=404, detail="Suggestion not found")
    return row


def _reap_stuck_applying(db: Session, user_email: str) -> None:
    """Fail rows whose apply never finished, so the AE can retry them.

    Lazy (on read) rather than scheduled: the queue only matters when someone is
    looking at it. The retry is safe because every create probes HubSpot for an
    existing record first, so a write that actually landed resolves to
    "already exists" instead of duplicating.
    """
    cutoff = datetime.utcnow() - timedelta(seconds=_APPLYING_STALE_SECONDS)
    try:
        changed = (
            db.query(CrmSyncSuggestion)
            .filter(CrmSyncSuggestion.user_email == user_email)
            .filter(CrmSyncSuggestion.status == service.STATUS_APPLYING)
            .filter(CrmSyncSuggestion.updated_at < cutoff)
            .update(
                {
                    "status": service.STATUS_FAILED,
                    "error": (
                        "This update timed out while being applied. Check HubSpot "
                        "before retrying — it may have gone through."
                    ),
                    "updated_at": datetime.utcnow(),
                },
                synchronize_session=False,
            )
        )
        if changed:
            db.commit()
    except Exception:  # noqa: BLE001 — reaping must never break the list
        logger.exception("[crm-sync] reaping stuck applies failed for %s", user_email)
        db.rollback()


@router.post("/scan", status_code=202)
async def start_scan(
    background_tasks: BackgroundTasks,
    body: ScanRequest | None = None,
    user: UserProfile = Depends(require_crm_sync),
) -> dict:
    """Kick off a scan. 202 + run_id; poll ``/runs/{run_id}``."""
    reject_support_background_work()
    body = body or ScanRequest()
    email = user.email.lower()

    existing = await run_in_threadpool(run_state.active_run, email)
    if existing:
        raise HTTPException(
            status_code=409,
            detail="A CRM Sync scan is already running. Give it a minute.",
        )

    since_days = body.since_days or settings.CRM_SYNC_SCAN_DAYS
    window_start = datetime.now(timezone.utc) - timedelta(days=since_days)
    run = await run_in_threadpool(
        run_state.create_run,
        email,
        trigger=run_state.TRIGGER_ON_DEMAND,
        window_start=window_start,
    )
    run_id = run["run_id"]

    from backend.jobs.launcher import submit_background_work

    executor, execution_name = submit_background_work(
        background_tasks,
        module="backend.jobs.crm_sync_job",
        args=[
            "--run-id",
            run_id,
            "--user-email",
            email,
            "--since-days",
            str(since_days),
        ],
        fallback=lambda: service.run_scan(run_id, email, since_days=since_days),
        label=f"crm-sync-scan:{email}",
    )
    run_state.mark_executor(run_id, executor, execution_name)
    return {"run_id": run_id, "status": "pending"}


@router.get("/runs/{run_id}")
async def get_run(run_id: str, user: UserProfile = Depends(require_crm_sync)) -> dict:
    run = await run_in_threadpool(run_state.get_run, run_id)
    # 404 rather than 403 on someone else's run: the caller has no business
    # learning that a given run id exists.
    if not run or run["user_email"] != user.email.lower():
        raise HTTPException(status_code=404, detail="Scan not found.")

    if run["status"] == run_state.RUN_PENDING:
        queued = run.get("queued_at")
        if queued and (datetime.utcnow() - queued).total_seconds() > (
            run_state.RUN_TIMEOUT_SECONDS
        ):
            await run_in_threadpool(run_state.fail_timed_out, run_id)
            run = await run_in_threadpool(run_state.get_run, run_id)

    return {
        "run_id": run["run_id"],
        "status": run["status"],
        "error": run["error"],
        "stats": run["stats"],
        "queued_at": run["queued_at"].isoformat() if run["queued_at"] else None,
        "finished_at": run["finished_at"].isoformat() if run["finished_at"] else None,
    }


@router.get("/suggestions")
def list_suggestions(
    status: str = service.STATUS_PENDING,
    db: Session = Depends(get_db),
    user: UserProfile = Depends(require_crm_sync),
) -> dict:
    """The caller's own suggestions. ``status=all`` includes handled ones."""
    email = user.email.lower()
    _reap_stuck_applying(db, email)

    query = (
        db.query(CrmSyncSuggestion)
        .filter(CrmSyncSuggestion.user_email == email)
        # Only the change types the scan produces now: a stored row of a type
        # it no longer produces stays out of the queue rather than showing as a
        # card the UI cannot render; a pending one expires on the normal sweep.
        .filter(CrmSyncSuggestion.change_type.in_(validators.CHANGE_TYPES))
    )
    if status != "all":
        statuses = [s.strip() for s in status.split(",") if s.strip()]
        query = query.filter(CrmSyncSuggestion.status.in_(statuses))
    rows = query.order_by(CrmSyncSuggestion.created_at.desc()).limit(200).all()
    try:
        config = get_hubspot_config()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503, detail="CRM suggestions are unavailable"
        ) from exc
    # Queued suggestions can predate a pipeline change. Recheck ownership
    # against live HubSpot records before exposing even the stored before/after
    # values. New-record suggestions have no trusted HubSpot identity yet.
    visible = _visible_suggestion_keys(rows, config.pipeline_ids)

    return {
        "suggestions": [
            _serialize(row)
            for row in rows
            if (row.hubspot_object_type, str(row.hubspot_object_id)) in visible
        ],
        # None when unset — the UI hides HubSpot deep links rather than
        # rendering a link that 404s.
        "hubspot_portal_id": config.portal_id,
    }


@router.post("/suggestions/{suggestion_id}/approve")
async def approve_suggestion(
    suggestion_id: str,
    body: ApproveRequest | None = None,
    db: Session = Depends(get_db),
    user: UserProfile = Depends(require_crm_sync),
) -> dict:
    """Approve a currently owned suggestion and write it to HubSpot."""
    body = body or ApproveRequest()
    try:
        await run_in_threadpool(_require_live_suggestion, db, suggestion_id, user.email)
        row = await run_in_threadpool(
            apply_mod.apply_suggestion,
            db,
            suggestion_id,
            user.email,
            edited_after=body.edited_after,
        )
    except apply_mod.SuggestionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except apply_mod.SuggestionInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await run_in_threadpool(_require_live_suggestion, db, suggestion_id, user.email)
    return _serialize(row)


@router.post("/suggestions/{suggestion_id}/dismiss")
async def dismiss_suggestion(
    suggestion_id: str,
    body: DismissRequest | None = None,
    db: Session = Depends(get_db),
    user: UserProfile = Depends(require_crm_sync),
) -> dict:
    body = body or DismissRequest()
    try:
        await run_in_threadpool(_require_live_suggestion, db, suggestion_id, user.email)
        row = await run_in_threadpool(
            apply_mod.dismiss_suggestion,
            db,
            suggestion_id,
            user.email,
            reason=body.reason,
        )
    except apply_mod.SuggestionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await run_in_threadpool(_require_live_suggestion, db, suggestion_id, user.email)
    return _serialize(row)

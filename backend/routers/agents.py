"""UC-10 routes: kick the overnight orchestrator, list recommendations, dismiss them.

The overnight run endpoint is callable two ways:
  1. By an authenticated admin (manual trigger, useful for ad-hoc reruns).
  2. By Cloud Scheduler with the `X-Scheduler-Key` header set to
     `settings.SCHEDULER_API_KEY`. Same pattern as `/ingest/scheduler/sync`.

`/api/agents/overnight/run` orchestrates ALL registered department plugins
(see `backend.agents.registry.default_plugins`). Today that's Sales/HubSpot;
adding an HR or accounting system is an additive change to the registry.

The dismiss endpoint is end-user authenticated and scoped to the user's own
recommendations (no admin override — dismissal is a personal preference).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
)
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from backend.auth import get_current_user, require_eos_module, security
from backend.jobs.launcher import (
    reject_support_background_work,
    require_automations_http,
)
from rubik_core.auth.registry import UserProfile
from rubik_core.brand import PRODUCT_NAME
from rubik_core.config import settings
from rubik_core.database.db import get_db
from rubik_core.database.models import (
    AgentRecommendation,
    AgentRecommendationFeedback,
)
from rubik_core.priorities.temporal import resolve_user_now

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/agents", tags=["agents"])
# Scheduler/cron endpoints (same ``/agents`` URLs) are mounted UNGATED in
# main.py — they authenticate with an ``X-Scheduler-Key``, not a user token, so
# they must sit outside ``require_full_app_access`` (the pilot gate resolves a
# user and would 401 token-less Cloud Scheduler calls). Each still re-validates
# the key via ``_is_authorized_run_caller``.
scheduler_router = APIRouter(
    prefix="/agents",
    tags=["agents"],
    dependencies=[
        Depends(reject_support_background_work),
        Depends(require_automations_http),
    ],
)


class RecommendationOut(BaseModel):
    id: str
    type: str
    lead_id: str | None = None
    lead_name: str | None = None
    recommendation_text: str
    evidence: dict | None = None
    confidence: float
    created_at: datetime
    snoozed_until: datetime | None = None
    acted_on_at: datetime | None = None
    action_type: str | None = None

    model_config = {"from_attributes": True}


def _parse_iso_utc(raw) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (
        dt.replace(tzinfo=timezone.utc)
        if dt.tzinfo is None
        else dt.astimezone(timezone.utc)
    )


def _rec_event_is_past(rec: AgentRecommendation, now_utc: datetime) -> bool:
    """True when this rec is anchored on calendar event(s) that all occurred
    on a PRIOR day — a meeting-prep rec for a meeting that is over should not
    keep surfacing the next day (the pipeline-#2 analogue of the
    "prep for yesterday's L10" bug).

    Prior-DAY (not just earlier-today) granularity is deliberate: the judge is
    allowed to surface a same-day past event when the rep still owes a
    follow-up before the next touchpoint, so we must not override that the day
    of the meeting — only once it has rolled into a previous day.
    """
    sources = rec.sources if isinstance(rec.sources, list) else []
    event_dts: list[datetime] = []
    for s in sources:
        if not isinstance(s, dict) or s.get("source") != "calendar":
            continue
        payload = s.get("payload") if isinstance(s.get("payload"), dict) else {}
        dt = _parse_iso_utc(payload.get("starts_at") or s.get("occurred_at"))
        if dt:
            event_dts.append(dt)
    if not event_dts:
        return False
    # Start of "today" in the user's local day; an event before it is a prior day.
    ctx = resolve_user_now(now_utc=now_utc)
    start_of_today = ctx.now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return all(dt < start_of_today for dt in event_dts)


def visible_hubspot_deal_ids(ids: set[str]) -> set[str]:
    """Prove that stored deal references still belong to this workspace.

    Cached cards outlive connector edits. A successful old generation is not
    evidence that a deal remains in this workspace's allowed pipelines.
    """
    if not ids:
        return set()
    try:
        from rubik_core.retrieval.hubspot_api import HubSpotAPIClient
        from rubik_core.tenancy.hubspot_config import get_hubspot_config

        allowed = set(get_hubspot_config().pipeline_ids)
        if not allowed:
            return set()
        rows = HubSpotAPIClient().batch_read("deals", sorted(ids), ["pipeline"])
        return {
            str(row["id"])
            for row in rows
            if str(row.get("id")) in ids
            and (row.get("properties") or {}).get("pipeline") in allowed
        }
    except Exception:  # noqa: BLE001 — an outage must hide cached CRM content
        logger.warning("Stored HubSpot deal ownership could not be verified")
        return set()


def _recommendation_crm_deal_ids(rec: AgentRecommendation) -> set[str] | None:
    """Deal IDs cited by a CRM-sourced rec, or None for non-CRM work.

    An empty set means a CRM rec lacks enough provenance to prove ownership;
    it is withheld. Legacy rows with no source metadata are treated as CRM
    when they carry a lead id, because this table began as a HubSpot surface.
    """
    metadata = rec.source_metadata if isinstance(rec.source_metadata, dict) else {}
    sources = rec.sources if isinstance(rec.sources, list) else []
    hubspot_sources = [
        source
        for source in sources
        if isinstance(source, dict)
        and str(source.get("source") or "").casefold() == "hubspot"
    ]
    explicit_non_crm = (
        bool(sources)
        and not hubspot_sources
        and (metadata.get("primary_entity_kind") != "deal")
    )
    if explicit_non_crm:
        return None
    plugin = str(metadata.get("plugin") or "").casefold()
    crm = bool(
        hubspot_sources
        or metadata.get("primary_entity_kind") == "deal"
        or plugin == "sales_hubspot"
        or rec.type
        in {"stale_lead", "stage_stuck", "new_product_match", "market_trigger"}
        or (not sources and rec.lead_id and not metadata.get("primary_entity_kind"))
    )
    if not crm:
        return None
    ids: set[str] = set()
    if rec.lead_id and (
        metadata.get("primary_entity_kind") == "deal"
        or plugin == "sales_hubspot"
        or rec.type
        in {"stale_lead", "stage_stuck", "new_product_match", "market_trigger"}
        or (not sources and not metadata.get("primary_entity_kind"))
    ):
        ids.add(str(rec.lead_id))
    for source in hubspot_sources:
        refs = (
            source.get("entity_refs")
            if isinstance(source.get("entity_refs"), dict)
            else {}
        )
        payload = (
            source.get("payload") if isinstance(source.get("payload"), dict) else {}
        )
        deal_id = refs.get("deal_id") or payload.get("deal_id")
        if deal_id:
            ids.add(str(deal_id))
    return ids


def _visible_recommendations(
    rows: list[AgentRecommendation],
) -> list[AgentRecommendation]:
    requirements = [_recommendation_crm_deal_ids(row) for row in rows]
    referenced = set().union(*(ids for ids in requirements if ids is not None))
    visible = visible_hubspot_deal_ids(referenced) if referenced else set()
    return [
        row
        for row, ids in zip(rows, requirements)
        if ids is None or (bool(ids) and ids <= visible)
    ]


def _candidate_recommendations(
    db: Session,
    user_email: str,
    limit: int = 3,
) -> list[AgentRecommendation]:
    """Top-N active recs for a user, highest confidence first.

    Active = not dismissed AND (not snoozed OR snooze has expired) AND not
    stale: a rec older than ``settings.RECOMMENDATION_MAX_AGE_HOURS`` or whose
    only calendar event has already passed is dropped, so a high-confidence
    rec can no longer surface forever until manually dismissed.
    """
    now = datetime.utcnow()
    now_utc = datetime.now(timezone.utc)
    age_floor = now - timedelta(hours=settings.RECOMMENDATION_MAX_AGE_HOURS)
    rows = (
        db.query(AgentRecommendation)
        .filter(AgentRecommendation.user_email == user_email)
        .filter(AgentRecommendation.dismissed_at.is_(None))
        .filter(AgentRecommendation.created_at >= age_floor)
        .filter(
            or_(
                AgentRecommendation.snoozed_until.is_(None),
                AgentRecommendation.snoozed_until <= now,
            )
        )
        .order_by(
            AgentRecommendation.confidence.desc(), AgentRecommendation.created_at.desc()
        )
        # Over-fetch so the python past-event filter below is unlikely to
        # starve the result. Heuristic, not a guarantee — a huge backlog of
        # high-confidence prior-day-event recs could still crowd the window;
        # the age floor keeps that bounded.
        .limit(max(limit * 5, 25))
        .all()
    )
    return [r for r in rows if not _rec_event_is_past(r, now_utc)]


def fetch_top_recommendations(
    db: Session, user_email: str, limit: int = 3
) -> list[AgentRecommendation]:
    """Synchronous reader for Jobs and other worker-thread callers."""
    return _visible_recommendations(_candidate_recommendations(db, user_email, limit))[
        :limit
    ]


def _record_feedback(
    db: Session,
    rec: AgentRecommendation,
    action_type: str,
    metadata: dict | None = None,
) -> None:
    """Stamp the recommendation row with the action + write a feedback history row.

    Two writes per action — the row keeps a denormalized "most recent action"
    for the briefing badge, and the feedback table preserves the full event
    log for the AI's future calibration loop.
    """
    now = datetime.utcnow()
    rec.action_type = action_type
    rec.action_metadata = metadata or {}
    if action_type != "dismissed":
        rec.acted_on_at = now
    db.add(
        AgentRecommendationFeedback(
            id=str(uuid.uuid4()),
            recommendation_id=rec.id,
            action_type=action_type,
            action_metadata=metadata or {},
            occurred_at=now,
        )
    )


def _allowed_user_emails(user: UserProfile) -> set[str]:
    """The set of `user_email` values this user may act on: their own."""
    return {user.email.lower()}


async def _get_owned_rec(
    db: Session, rec_id: str, user: UserProfile
) -> AgentRecommendation:
    """Fetch the rec, 404 if missing, 403 if it belongs to someone else."""
    row = db.query(AgentRecommendation).filter(AgentRecommendation.id == rec_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Recommendation not found.")
    if (row.user_email or "").lower() not in _allowed_user_emails(user):
        raise HTTPException(status_code=403, detail="Not your recommendation.")
    from starlette.concurrency import run_in_threadpool

    if not await run_in_threadpool(_visible_recommendations, [row]):
        raise HTTPException(status_code=404, detail="Recommendation not found.")
    return row


@router.get("/recommendations", response_model=list[RecommendationOut])
async def list_recommendations(
    limit: int = Query(10, ge=1, le=50),
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the user's active recommendations, newest/highest-confidence first."""
    from starlette.concurrency import run_in_threadpool

    candidates = _candidate_recommendations(db, user.email, limit=limit)
    rows = (await run_in_threadpool(_visible_recommendations, candidates))[:limit]
    return [RecommendationOut.model_validate(r) for r in rows]


@router.post("/recommendations/{rec_id}/dismiss")
async def dismiss_recommendation(
    rec_id: str,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Set `dismissed_at`. Idempotent: dismissing twice is a no-op.

    Dismissal is permanent — the row is never resurfaced. Use `/snooze`
    instead if the user wants the row to come back later.
    """
    row = await _get_owned_rec(db, rec_id, user)
    if row.dismissed_at is None:
        row.dismissed_at = datetime.utcnow()
        _record_feedback(db, row, "dismissed")
        db.commit()
    return {"id": rec_id, "dismissed_at": row.dismissed_at.isoformat()}


# ─── Action endpoints (UC-10 follow-up) ───────────────────────────────────────


class SnoozeRequest(BaseModel):
    days: int = Field(
        default=3, ge=1, le=30, description="Days to hide the recommendation for."
    )


@router.post("/recommendations/{rec_id}/snooze")
async def snooze_recommendation(
    rec_id: str,
    body: SnoozeRequest,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Hide the row from the briefing for N days, then resurface it.

    Default 3 days = "I'll get to it Wednesday" for a Monday tap. Multiple
    snoozes extend; the most recent wins. The agent's duplicate-guard treats
    snoozed rows as already-handled so it won't re-emit the same finding.
    """
    row = await _get_owned_rec(db, rec_id, user)
    now = datetime.utcnow()
    until = now + timedelta(days=body.days)
    row.snoozed_until = until
    _record_feedback(
        db, row, "snoozed", {"snoozed_until": until.isoformat(), "days": body.days}
    )
    db.commit()
    return {"id": rec_id, "snoozed_until": until.isoformat()}


@router.post("/recommendations/{rec_id}/open-hubspot")
async def open_in_hubspot(
    rec_id: str,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Stamp the row as opened-in-HubSpot and return the deep link.

    Deep link format requires the workspace's HubSpot portal id. When configured,
    returns the canonical record URL; when unset, returns just
    the deal_id and the frontend can show a fallback message.
    """
    row = await _get_owned_rec(db, rec_id, user)
    from starlette.concurrency import run_in_threadpool

    from rubik_core.tenancy.hubspot_config import get_hubspot_config

    portal_id = (await run_in_threadpool(get_hubspot_config)).portal_id
    deep_link = None
    if portal_id and row.lead_id:
        # 0-3 = Deal object type in HubSpot's CRM data model.
        deep_link = (
            f"https://app.hubspot.com/contacts/{portal_id}/record/0-3/{row.lead_id}"
        )

    _record_feedback(
        db, row, "hubspot_opened", {"deep_link": deep_link, "deal_id": row.lead_id}
    )
    db.commit()
    return {"id": rec_id, "deep_link": deep_link, "deal_id": row.lead_id}


_REC_TYPE_TO_EMAIL_TYPE: dict[str, str] = {
    "stale_lead": "re_engagement",
    "stage_stuck": "follow_up",
    "new_product_match": "re_engagement",
    "market_trigger": "market_trigger",
}


class DraftEmailRequest(BaseModel):
    recipient_email: str
    recipient_name: str | None = None
    additional_context: str | None = None


@router.post("/recommendations/{rec_id}/draft-email")
async def draft_email_for_recommendation(
    rec_id: str,
    body: DraftEmailRequest,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Hand off to UC-7's email drafting with the recommendation's context.

    The frontend supplies the recipient (the rep knows who at the company to
    write to better than the agent does — HubSpot's primary contact isn't
    always the right person). The recommendation's `lead_name`, `evidence`,
    and `recommendation_text` become the trigger context UC-7 grounds on.
    """
    row = await _get_owned_rec(db, rec_id, user)
    from backend.routers.email import EmailDraftRequest, draft_sales_email

    email_type = _REC_TYPE_TO_EMAIL_TYPE.get(row.type, "follow_up")
    trigger_context = row.recommendation_text
    extra = body.additional_context or ""
    if extra:
        extra = f"\n\nUser added: {extra}"

    draft_req = EmailDraftRequest(
        recipient_email=body.recipient_email,
        recipient_name=body.recipient_name,
        company=row.lead_name,
        email_type=email_type,
        trigger_context=trigger_context,
        additional_context=extra or None,
    )
    try:
        result = await draft_sales_email(draft_req, user=user, db=db)
    except Exception as e:
        logger.exception(f"Draft handoff failed for rec {rec_id}")
        raise HTTPException(status_code=502, detail=f"Email drafting failed: {e}")

    metadata = {
        "recipient_email": body.recipient_email,
        "email_type": email_type,
        "subject": result.subject,
        "gmail_draft_url": result.gmail_draft_url,
    }
    _record_feedback(db, row, "email_sent", metadata)
    db.commit()
    return {
        "id": rec_id,
        "gmail_draft_url": result.gmail_draft_url,
        "subject": result.subject,
        "body": result.body,
    }


class BlockTimeRequest(BaseModel):
    duration_min: int = Field(default=30, ge=15, le=240)
    start_hint: str | None = Field(
        default=None,
        description="Optional ISO datetime to anchor the block; defaults to next round 30-min slot today.",
    )


@router.post("/recommendations/{rec_id}/block-time")
async def block_time_for_recommendation(
    rec_id: str,
    body: BlockTimeRequest,
    user: UserProfile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a calendar block on the user's primary calendar to follow up.

    Defaults to the next round 30-min slot from now in UTC. Future enhancement:
    use the existing free-slot computation to find a real opening. For today
    the simple "+30min from now" works — the user can always drag it later.
    """
    row = await _get_owned_rec(db, rec_id, user)
    from rubik_core.tools.calendar import GoogleCalendarClient

    if body.start_hint:
        try:
            start = datetime.fromisoformat(body.start_hint.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail="start_hint must be ISO 8601.")
    else:
        # Round up to the next half-hour, in UTC.
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        minute_bucket = (now.minute // 30 + 1) * 30
        if minute_bucket >= 60:
            # Roll into the next hour via timedelta so 23:xx wraps to the next
            # day's 00:00 — `now.replace(hour=now.hour + 1)` raised
            # "hour must be in 0..23" whenever this ran in the 23:00 UTC hour.
            start = (now + timedelta(hours=1)).replace(minute=0)
        else:
            start = now.replace(minute=minute_bucket)

    end = start + timedelta(minutes=body.duration_min)
    summary = f"Follow up: {row.lead_name or 'agent recommendation'}"
    description = (
        f"Auto-created from a {PRODUCT_NAME} AI Priority.\n\n"
        f"Recommendation: {row.recommendation_text}\n\n"
        f"Type: {row.type}  ·  Confidence: {(row.confidence or 0):.0%}"
    )
    client = GoogleCalendarClient(user_email=user.email)
    event = client.create_event(
        summary=summary,
        start_time=start.isoformat(),
        end_time=end.isoformat(),
        description=description,
    )
    if "error" in event:
        raise HTTPException(
            status_code=502, detail=f"Calendar create failed: {event['error']}"
        )

    metadata = {
        "event_id": event.get("id"),
        "html_link": event.get("htmlLink"),
        "start": start.isoformat(),
        "end": end.isoformat(),
    }
    _record_feedback(db, row, "meeting_blocked", metadata)
    db.commit()
    return {"id": rec_id, **metadata}


def _scheduler_key_ok(x_scheduler_key: str | None) -> bool:
    """The request carries Cloud Scheduler's own key."""
    from rubik_core.tenancy.scheduler import scheduler_key_valid

    return scheduler_key_valid(x_scheduler_key)


def _is_authorized_run_caller(
    request: Request,
    x_scheduler_key: str | None,
    credentials: HTTPAuthorizationCredentials | None,
) -> bool:
    """Allow either Cloud Scheduler (header key) or an authenticated admin."""
    if _scheduler_key_ok(x_scheduler_key):
        return True
    if credentials:
        try:
            user = get_current_user(request, credentials)
            return bool(user.is_admin)
        except Exception:
            return False
    return False


async def _dispatch_scheduled_worker(
    module: str, args: list[str] | None = None
) -> dict:
    """Launch a workspace-stamped Job before a long sweep consumes the request."""
    from starlette.concurrency import run_in_threadpool

    from backend.jobs.launcher import JobLaunchError, launch_module_execution

    try:
        execution = await run_in_threadpool(launch_module_execution, module, args or [])
    except JobLaunchError as exc:
        logger.exception("Scheduled Job launch failed: %s", module)
        raise HTTPException(
            status_code=502, detail="Scheduled Job launch failed"
        ) from exc
    return {
        "status": "dispatched",
        "trigger": "scheduler",
        "execution": execution.get("name")
        or (execution.get("metadata") or {}).get("name"),
    }


# ── EOS RAG ingestion trigger (weekly cron + manual admin run) ────────────


@scheduler_router.post("/ingest/eos", dependencies=[Depends(require_eos_module)])
async def run_eos_ingestion(
    request: Request,
    x_scheduler_key: str | None = Header(None, alias="X-Scheduler-Key"),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    """Re-ingest every EOS source (VTO sections, Rocks, Issues, L10 notes,
    Processes) into Cubie's Vertex AI RAG corpus.

    Scheduler calls dispatch a workspace-scoped Cloud Run Job. Manual admin
    runs remain synchronous so the response includes the detailed outcome.
    """
    if not _is_authorized_run_caller(request, x_scheduler_key, credentials):
        raise HTTPException(status_code=403, detail="Unauthorized.")
    if _scheduler_key_ok(x_scheduler_key):
        return await _dispatch_scheduled_worker("backend.jobs.eos_full_ingest_job")

    from starlette.concurrency import run_in_threadpool

    def _sweep():
        from rubik_core.database.db import SessionLocal
        from rubik_core.ingestion.eos import ingest_all_sources

        session = SessionLocal()
        try:
            return ingest_all_sources(session)
        finally:
            session.close()

    result = await run_in_threadpool(_sweep)
    logger.info(
        "[eos-ingest] complete: %s succeeded=%s failed=%s",
        result.summary(),
        len(result.succeeded),
        len(result.failed),
    )
    return {
        "status": "completed",
        "trigger": "scheduler" if x_scheduler_key else "admin",
        "succeeded": len(result.succeeded),
        "failed": len(result.failed),
    }


# ── EOS L10 — mail the recap of every recorded L10 (every 15 min) ────────


@scheduler_router.post(
    "/eos/l10/summary-emails", dependencies=[Depends(require_eos_module)]
)
async def send_l10_summary_emails(
    request: Request,
    dry_run: bool = Query(
        False,
        description=(
            "Resolve recordings + recipients and report them; send nothing and "
            "spend no LLM tokens."
        ),
    ),
    hours: int | None = Query(None, description="Lookback window override, in hours."),
    x_scheduler_key: str | None = Header(None, alias="X-Scheduler-Key"),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    """Email each recently-finished, RECORDED L10 its recap.

    The summary is written from the meeting's recording together with that
    session's own EOS entries, and goes only to the people the transcript shows
    were actually in the room — filtered through the L10 meeting's own ACL, so
    the Exec L10's recap reaches Exec-team members only. A meeting that wasn't
    recorded gets no email at all. See
    ``backend/services/l10_summary_email.py`` for the full decision order.

    Runs on a SCHEDULE rather than at Conclude because a Gemini / Fathom
    transcript lands minutes-to-hours after the room empties: a pass at
    Conclude time would find no recording essentially every time. Every 15
    minutes is the natural cadence (it matches the transcript sync), and
    ``eos_l10_sessions.summary_email_sent_at`` makes it exactly-once no matter
    how often it runs.

    Triggered by Cloud Scheduler (``X-Scheduler-Key``) or an authenticated
    admin. Scheduled live runs dispatch a workspace-scoped Job so a backlog
    cannot exhaust the HTTP request timeout. Manual and dry runs report their
    results synchronously.

    ``?dry_run=true`` reveals who WOULD be mailed, including for the Exec L10,
    without generating or sending anything.
    """
    if not _is_authorized_run_caller(request, x_scheduler_key, credentials):
        raise HTTPException(status_code=403, detail="Unauthorized.")

    if _scheduler_key_ok(x_scheduler_key) and not dry_run:
        args = ["--hours", str(hours)] if hours else []
        return await _dispatch_scheduled_worker(
            "backend.jobs.l10_summary_email_job", args
        )

    from starlette.concurrency import run_in_threadpool

    def _sweep():
        from datetime import timedelta

        from backend.services.l10_summary_email import sweep_l10_summary_emails
        from rubik_core.database.db import SessionLocal

        session = SessionLocal()
        try:
            return sweep_l10_summary_emails(
                session,
                dry_run=dry_run,
                lookback=timedelta(hours=hours) if hours else None,
            )
        finally:
            session.close()

    report = await run_in_threadpool(_sweep)
    logger.info("[l10-summary] %s (dry_run=%s)", report.summary(), dry_run)
    return {
        "status": "completed",
        "trigger": "scheduler" if x_scheduler_key else "admin",
        "dry_run": dry_run,
        "enabled": report.enabled,
        "considered": len(report.outcomes),
        "sent": len(report.by_status("sent")),
        "would_send": len(report.by_status("would_send")),
        "not_recorded": len(report.by_status("not_recorded")),
        "no_recipients": len(report.by_status("no_recipients")),
        "already_sent": len(report.by_status("already_sent")),
        "before_start": len(report.by_status("before_start")),
        "failed": len(report.by_status("failed")),
        "sessions": [
            {
                "session_id": str(o.session_id),
                "meeting": o.meeting_name,
                "is_exec_meeting": o.is_exec_meeting,
                "status": o.status,
                "recording": o.recording_title,
                "doc_id": o.doc_id,
                "recipients": o.recipients,
                "excluded": o.excluded,
                "error": o.error,
            }
            for o in report.outcomes
            # "not_l10" is every non-L10 meeting in the window — noise, not news.
            if o.status != "not_l10"
        ],
    }


# ── EOS L10 — close sessions nobody ever concluded (nightly) ─────────────


@scheduler_router.post(
    "/eos/l10/close-abandoned-sessions", dependencies=[Depends(require_eos_module)]
)
async def close_abandoned_l10_sessions(
    request: Request,
    dry_run: bool = Query(
        False, description="Report what would close without writing anything."
    ),
    x_scheduler_key: str | None = Header(None, alias="X-Scheduler-Key"),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    """Stamp ``actual_end`` on L10 sessions nobody ever concluded.

    An ``eos_l10_sessions`` row stays open until a facilitator clicks
    Conclude, and in practice almost nobody does, so open sessions pile up
    untouched for days or weeks. The read paths already refuse to treat
    those as live (see
    ``rubik_core/eos/session_lifecycle.py``), but the rows themselves need
    closing or every report built on ``actual_end`` keeps counting meetings
    that finished last month as still running.

    Triggered nightly by Cloud Scheduler (``X-Scheduler-Key``) or by an
    authenticated admin. Runs SYNCHRONOUSLY in a threadpool: a Cloud Run
    background task is CPU-throttled the instant the response returns. The
    sweep is one indexed query plus a handful of UPDATEs over the open set
    (ten rows, not ten thousand), so it finishes well inside the request.

    Idempotent — a second run finds nothing left to close. Pass
    ``?dry_run=true`` to see the verdict per session and write nothing;
    that is the same report ``scripts/close_stale_l10_sessions.py`` prints.
    """
    if not _is_authorized_run_caller(request, x_scheduler_key, credentials):
        raise HTTPException(status_code=403, detail="Unauthorized.")

    from starlette.concurrency import run_in_threadpool

    def _sweep():
        from rubik_core.database.db import SessionLocal
        from rubik_core.eos.session_lifecycle import close_abandoned_sessions

        session = SessionLocal()
        try:
            return close_abandoned_sessions(session, dry_run=dry_run)
        finally:
            session.close()

    report = await run_in_threadpool(_sweep)
    logger.info("[l10-auto-close] %s (dry_run=%s)", report.summary(), dry_run)
    return {
        "status": "completed",
        "trigger": "scheduler" if x_scheduler_key else "admin",
        "dry_run": dry_run,
        "scanned": len(report.scanned),
        "closed": len(report.closed),
        "would_close": len(report.abandoned),
        "still_live": len(report.still_live),
        "sessions": [
            {
                "session_id": str(row.session_id),
                "meeting": row.meeting_name,
                "actual_start": row.actual_start.isoformat(),
                "ended_at": row.last_activity.isoformat(),
                "idle_hours": round(
                    (report.now - row.last_activity).total_seconds() / 3600.0, 1
                ),
                "current_step": row.current_step,
            }
            for row in report.abandoned
        ],
    }


@scheduler_router.post("/availability/refresh-ooo")
async def refresh_ooo_endpoint(
    request: Request,
    x_scheduler_key: str | None = Header(None, alias="X-Scheduler-Key"),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    """Daily refresh of the teammate out-of-office table (``ooo_periods``).

    Reads every approved user's calendar ONCE via DWD and materialises their
    upcoming OOO periods so chat / priorities / the manager digest can answer
    "who's out this week?" from a cheap table instead of impersonating N
    calendars per question. Triggered by Cloud Scheduler (``X-Scheduler-Key``)
    or an authenticated admin. Runs SYNCHRONOUSLY in a threadpool — a Cloud Run
    background task is CPU-throttled the instant the response returns. Optional
    JSON body ``{"emails": [...]}`` scopes the refresh to specific users.
    """
    if not _is_authorized_run_caller(request, x_scheduler_key, credentials):
        raise HTTPException(status_code=403, detail="Unauthorized.")

    emails: list[str] | None = None
    try:
        body = await request.json()
        if isinstance(body, dict) and isinstance(body.get("emails"), list):
            emails = [str(e).strip().lower() for e in body["emails"] if str(e).strip()]
    except Exception:  # noqa: BLE001 — no/invalid body → full-roster refresh
        emails = None

    from starlette.concurrency import run_in_threadpool

    def _run():
        from rubik_core.availability.store import refresh_ooo_periods
        from rubik_core.database.db import SessionLocal

        session = SessionLocal()
        try:
            return refresh_ooo_periods(session, emails=emails)
        finally:
            session.close()

    result = await run_in_threadpool(_run)
    logger.info("[ooo-refresh] complete: %s", result.summary())
    return {
        "status": "completed",
        "trigger": "scheduler" if x_scheduler_key else "admin",
        "users_scanned": result.users_scanned,
        "users_out": result.users_out,
        "periods_written": result.periods_written,
        "errors": result.errors,
    }


@scheduler_router.post("/overnight/run")
async def run_overnight_agent(
    request: Request,
    x_scheduler_key: str | None = Header(None, alias="X-Scheduler-Key"),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    """Cloud-Scheduler-triggered nightly run of every registered department plugin.

    Scheduler calls dispatch a workspace-scoped Cloud Run Job; manual admin
    runs are synchronous and report the orchestrator outcome.
    """
    if not _is_authorized_run_caller(request, x_scheduler_key, credentials):
        raise HTTPException(status_code=403, detail="Unauthorized.")
    if _scheduler_key_ok(x_scheduler_key):
        return await _dispatch_scheduled_worker("backend.agents.overnight")

    from starlette.concurrency import run_in_threadpool

    from backend.agents.overnight import OvernightAgent

    result = await run_in_threadpool(lambda: OvernightAgent().run())
    logger.info(
        f"[overnight] complete: run_id={result.run_id} written={result.written} "
        f"by_type={result.by_type} "
        f"plugins={[(p.plugin_name, p.written, p.error) for p in result.plugin_results]} "
        f"errors={result.errors}"
    )
    return {
        "status": "completed",
        "trigger": "scheduler" if x_scheduler_key else "admin",
        "run_id": result.run_id,
        "written": result.written,
    }


@scheduler_router.post("/priorities/deliver-slack")
async def deliver_slack_priorities_endpoint(
    request: Request,
    x_scheduler_key: str | None = Header(None, alias="X-Scheduler-Key"),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    """DM each opted-in rep their active AI Priorities.

    Separate from `/overnight/run` (generation at 02:00 UTC) — point a SECOND
    Cloud Scheduler job at the team's local morning. Returns immediately;
    delivery runs in the background. No-op (logs only) when the Slack app isn't
    installed yet (no bot token) or no department has opted in.

    Optional JSON body:

    * ``{"emails": ["a@x.com", ...]}`` scopes the run to exactly those users (a
      targeted re-send), bypassing the dept/exec gate.
    * ``{"report": "next_week"}`` delivers the Friday look-ahead instead of
      today's briefing — the ONLY way to get the next-week DM. The Friday
      afternoon scheduler job MUST send it: without it that run re-delivers the
      daily bundle, and the Friday's second DM duplicates the morning's.
      Defaults to ``"daily"``.

    No body (or an unparseable one) → a full opted-in daily run, which is what
    the weekday morning scheduler sends.
    """
    if not _is_authorized_run_caller(request, x_scheduler_key, credentials):
        raise HTTPException(status_code=403, detail="Unauthorized.")

    from backend.agents.slack_delivery import DELIVERY_REPORTS

    emails: list[str] | None = None
    report = "daily"
    try:
        body = await request.json()
        if isinstance(body, dict):
            if isinstance(body.get("emails"), list):
                emails = [
                    str(e).strip() for e in body["emails"] if str(e).strip()
                ] or None
            requested = str(body.get("report") or "daily").strip().lower()
            if requested not in DELIVERY_REPORTS:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Unsupported report {requested!r}. "
                        f"Expected one of: {', '.join(DELIVERY_REPORTS)}."
                    ),
                )
            report = requested
    except HTTPException:
        raise
    except Exception:
        emails = None  # no/invalid body (scheduler path) → full run

    from starlette.concurrency import run_in_threadpool

    def _deliver():
        from backend.agents.slack_delivery import deliver_slack_priorities
        from rubik_core.database.db import SessionLocal

        session = SessionLocal()
        try:
            return deliver_slack_priorities(session, emails=emails, report=report)
        finally:
            session.close()

    r = await run_in_threadpool(_deliver)
    logger.info(
        f"[slack-deliver] complete: report={report} delivered={r.delivered} "
        f"users={r.users_delivered} recs={r.recs_delivered} "
        f"skipped_no_slack={r.users_skipped_no_slack} "
        f"skipped_empty={r.users_skipped_empty} errors={r.errors}"
    )
    return {
        "status": "completed",
        "trigger": "scheduler" if x_scheduler_key else "admin",
        "report": report,
        "targeted": bool(emails),
        # Surfaced separately from users_skipped_empty: these addresses aren't
        # Slack members, so they are roster rows to clean up rather than a
        # generation gap to investigate.
        "users_no_slack_account": r.users_no_slack_account,
        "users_delivered": r.users_delivered,
        "users_skipped_empty": r.users_skipped_empty,
        "errors": r.errors,
    }


@scheduler_router.post("/priorities/manager-digest")
async def manager_digest_endpoint(
    request: Request,
    x_scheduler_key: str | None = Header(None, alias="X-Scheduler-Key"),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    """DM each dept's EOS head a digest of unresolved priorities, escalating
    HIGH items open >48h.

    Separate EOD Cloud Scheduler job. Runs SYNCHRONOUSLY (offloaded to a
    threadpool) so it completes while the request is in flight — a Cloud Run
    background task is CPU-throttled once the response returns. No-op (logs only)
    without a bot token or a configured manager channel.
    """
    if not _is_authorized_run_caller(request, x_scheduler_key, credentials):
        raise HTTPException(status_code=403, detail="Unauthorized.")

    from starlette.concurrency import run_in_threadpool

    def _digest():
        from backend.agents.slack_delivery import deliver_manager_digest
        from rubik_core.database.db import SessionLocal

        session = SessionLocal()
        try:
            return deliver_manager_digest(session)
        finally:
            session.close()

    r = await run_in_threadpool(_digest)
    logger.info(
        f"[slack-manager-digest] complete: delivered={r.delivered} "
        f"digests_sent={r.digests_sent} escalated={r.items_escalated} "
        f"errors={r.errors}"
    )
    return {
        "status": "completed",
        "trigger": "scheduler" if x_scheduler_key else "admin",
        "digests_sent": r.digests_sent,
    }


@scheduler_router.post(
    "/scorecard/sync-sales", dependencies=[Depends(require_eos_module)]
)
async def sync_sales_scorecard_endpoint(
    request: Request,
    x_scheduler_key: str | None = Header(None, alias="X-Scheduler-Key"),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    """Fill the "green" (deal-derived) scorecard measurables from HubSpot.

    Weekly Cloud Scheduler job (or an authenticated admin). Recomputes the last
    two Sunday-anchored weeks (the just-closed week finalizes, the in-progress
    week stays fresh) — idempotent, so re-running is safe. Fills only the
    measurables named by the workspace's HubSpot Scorecard mapping; with
    either the department or recipes unset it does nothing
    and ``note`` says why. Every other measurable is left untouched.

    Runs SYNCHRONOUSLY (offloaded to a threadpool) so the sync completes while
    the request is in flight — a Cloud Run background task is CPU-throttled the
    instant the response returns and silently never finishes.
    """
    if not _is_authorized_run_caller(request, x_scheduler_key, credentials):
        raise HTTPException(status_code=403, detail="Unauthorized.")

    from starlette.concurrency import run_in_threadpool

    def _sync():
        from rubik_core.database.db import SessionLocal
        from rubik_core.scorecard_sync.hubspot_green import sync_sales_green_metrics

        session = SessionLocal()
        try:
            return sync_sales_green_metrics(session, weeks=2, commit=True)
        finally:
            session.close()

    report = await run_in_threadpool(_sync)
    logger.info("[scorecard-sync-sales] %s", report.summary())
    return {
        "status": "completed",
        "trigger": "scheduler" if x_scheduler_key else "admin",
        "green_metrics": report.green_metrics_seen,
        "written": report.written,
        "skipped": report._count("skipped"),
        "errors": report.errors,
        "note": report.note,
    }


@scheduler_router.post("/fathom/sync")
async def sync_fathom_transcripts_endpoint(
    request: Request,
    limit: int = 50,
    x_scheduler_key: str | None = Header(None, alias="X-Scheduler-Key"),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    """Pull recently-recorded Fathom meetings into Cubie.

    Cubie had a Fathom webhook and a sync job but nothing that ever called
    either: the webhook was only ever registered against the Sales Coach, and no
    scheduler triggered the job. The result was 60+ days with zero Fathom
    transcripts, which left CRM Sync and the sales signals reading only internal
    Google Meet notes — every AE's prospect calls were simply absent.

    Polling rather than relying on the webhook is deliberate: a poll re-syncs
    whatever it missed during an outage, and doesn't depend on Fathom-side
    configuration we don't control.

    Account-wide — `list_meetings` is not user-scoped and every meeting carries
    its own `recorded_by`, which becomes the owner. Ingest is idempotent: the
    doc_id is a hash of the Fathom meeting id, and an already-ingested meeting
    is skipped, so overlapping runs cost nothing.

    Scheduled runs dispatch a workspace-scoped Job; manual runs return the
    sweep's result synchronously.
    """
    if not _is_authorized_run_caller(request, x_scheduler_key, credentials):
        raise HTTPException(status_code=403, detail="Unauthorized.")

    if _scheduler_key_ok(x_scheduler_key):
        return await _dispatch_scheduled_worker(
            "backend.jobs.fathom_sync_job", ["--limit", str(limit)]
        )

    from starlette.concurrency import run_in_threadpool

    def _sync() -> None:
        from backend.routers.fathom import process_fathom_transcripts_background
        from backend.routers.ingest import get_processor
        from rubik_core.tools._workspace_auth import workspace_dwd_subject

        fallback_owner = workspace_dwd_subject()
        if not fallback_owner:
            raise RuntimeError("Workspace DWD admin subject is not configured")

        process_fathom_transcripts_background(
            fallback_owner, get_processor(), limit=limit
        )

    await run_in_threadpool(_sync)
    logger.info("[fathom-sync] completed limit=%s", limit)
    return {
        "status": "completed",
        "trigger": "scheduler" if x_scheduler_key else "admin",
        "limit": limit,
    }


# ── Call capture + cockpit — the seven CCS / cockpit sweeps ───────────────
#
# All seven live on `scheduler_router`:
# `X-Scheduler-Key` via `_is_authorized_run_caller`, `?dry_run=true`, and a dict
# of NAMED OUTCOME BUCKETS rather than a bare count.
#
# Each one NO-OPS WITH A LOG LINE when its feature flag is off instead of
# returning 404. A 404 on a scheduler target reads as a broken job in Cloud
# Scheduler's own alerting, and every one of these flags defaults false —
# so the alerting would fire continuously on a correctly-configured environment
# that simply has not enabled the feature yet.
#
# The work itself is `backend/jobs/sales_kaizen.py`, ONE implementation per
# sweep, shared with `python -m backend.jobs.sales_kaizen <sweep>`. That module's
# docstring carries the run-here-vs-run-as-a-Job table and the reason for each.
#
# NOTE: creating the Cloud Scheduler entries is a separate, human, GCP-side step
# — the config lives in GCP, not in git. Without a `gcloud scheduler jobs
# create http` line per sweep these endpoints exist and never fire.


def _sweep_session(fn):
    """Run ``fn(session)`` on a fresh Session, closed afterwards.

    The house pattern in this file, factored out because seven endpoints use it.
    Everything below runs SYNCHRONOUSLY inside ``run_in_threadpool``: a Cloud Run
    background task is CPU-throttled the instant the response returns and
    silently never finishes.
    """
    from rubik_core.database.db import SessionLocal

    session = SessionLocal()
    try:
        return fn(session)
    finally:
        session.close()


@scheduler_router.post("/jobs/ccs/sweep")
async def run_ccs_sweep_endpoint(
    request: Request,
    dry_run: bool = Query(
        False,
        description=(
            "Resolve conference records and report what WOULD be ingested. "
            "Writes nothing, ingests nothing and spends no LLM tokens."
        ),
    ),
    lookback_hours: int | None = Query(
        None, description="Override CCS_POLL_LOOKBACK_HOURS for this run."
    ),
    x_scheduler_key: str | None = Header(None, alias="X-Scheduler-Key"),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    """Every 15 min — find the sales calls that happened. THE PRIMARY INGEST PATH.

    Polling, not push: there are zero Pub/Sub push
    receivers in this repo, and push would mean a hand-written service-account
    OIDC verifier plus per-user Workspace Events subscriptions whose missed
    renewal makes Google PERMANENTLY delete the subscription. Polling costs
    latency — seconds becomes <=15 minutes, against a wrap-up that is a
    next-morning workflow — and removes that failure mode entirely.

    Also ages out the silence: a session that ended more than 30 minutes ago
    with zero transcript entries becomes ``no_transcript``. Not an error — the
    host may have turned transcription off — and the wrap-up still opens, empty.

    🔴 **Overlap is free; a missed window is not.** Google deletes conference
    records, transcripts and transcript entries 30 days after the conference
    ends, so a window missed during a scheduler outage is a permanently lost
    call. Ingest is idempotent on ``(call_session_id, source, external_id)``, so
    re-reading a conference costs one read quota unit and writes nothing. Widen
    ``lookback_hours`` freely; never narrow it to save API calls.

    **A live run dispatches a Cloud Run Job; a dry run answers inline.** Per AE
    this may make up to 5 Gemini Pro extraction calls and as many Vertex RAG
    imports, and Vertex serializes imports per corpus — backing off up to ~4.5
    minutes on one document while another operation holds it. Over the AE roster
    that can exceed Cloud Run's 300s request cap, and a truncated ingest window
    is exactly the thing that is not recoverable. A dry run performs no ingest
    and no LLM call, so it is a handful of Meet reads and returns the full
    per-user report in the response. If the Job launch fails (not deployed yet,
    IAM, local dev) the sweep falls back to running synchronously in the
    threadpool — still CPU-allocated because the request is in flight, unlike a
    BackgroundTask — and says so in ``executor``.
    """
    if not _is_authorized_run_caller(request, x_scheduler_key, credentials):
        raise HTTPException(status_code=403, detail="Unauthorized.")

    from starlette.concurrency import run_in_threadpool

    from backend.jobs.sales_kaizen import run_ccs_sweep

    trigger = "scheduler" if x_scheduler_key else "admin"

    def _run_inline():
        return _sweep_session(
            lambda s: run_ccs_sweep(s, lookback_hours=lookback_hours, dry_run=dry_run)
        )

    if dry_run:
        report = await run_in_threadpool(_run_inline)
        return {"trigger": trigger, "executor": "inline", **report}

    # The flag check happens before the dispatch so a disabled feature does not
    # burn a Job execution every 15 minutes to log one line.
    if not settings.CCS_ENABLED:
        logger.info("[ccs-sweep] CCS_ENABLED is off — no sweep dispatched")
        return {
            "trigger": trigger,
            "executor": "none",
            "status": "disabled",
            "flag": "CCS_ENABLED",
            "dry_run": False,
        }

    from backend.jobs.launcher import launch_module_execution

    args = ["ccs-sweep"]
    if lookback_hours:
        args += ["--lookback-hours", str(int(lookback_hours))]
    try:
        execution = await run_in_threadpool(
            lambda: launch_module_execution("backend.jobs.sales_kaizen", args)
        )
    except Exception:  # noqa: BLE001 — degrade to synchronous, never skip a window
        logger.exception(
            "[ccs-sweep] Cloud Run Job launch failed — running the sweep "
            "synchronously in the request instead. That is CPU-allocated (the "
            "request is still in flight) but bounded by the 300s cap, so a busy "
            "window may be truncated and picked up by the next run. If the job "
            "is not deployed, restore it through the guarded platform deploy."
        )
        report = await run_in_threadpool(_run_inline)
        return {"trigger": trigger, "executor": "inline_fallback", **report}

    execution_name = execution.get("name") or (execution.get("metadata") or {}).get(
        "name"
    )
    logger.info(
        "[ccs-sweep] dispatched to cubie-worker (execution=%s lookback=%s)",
        execution_name,
        lookback_hours or settings.CCS_POLL_LOOKBACK_HOURS,
    )
    return {
        "trigger": trigger,
        "executor": "job",
        "status": "dispatched",
        "execution": execution_name,
        "lookback_hours": lookback_hours or settings.CCS_POLL_LOOKBACK_HOURS,
        "dry_run": False,
    }


@scheduler_router.post("/jobs/cockpit/recompute-states")
async def run_cockpit_recompute_endpoint(
    request: Request,
    dry_run: bool = Query(
        False, description="Report the transitions and roll the transaction back."
    ),
    x_scheduler_key: str | None = Header(None, alias="X-Scheduler-Key"),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    """Every 15 min — age every live commitment one step along the ladder.

    Idempotent: transitions only ever move FORWARD, so a re-run is a no-op and
    nothing un-breaches. The skips are the interesting part of the report —
    ``skipped_provisional`` is the mechanism by which an unconfirmed AI
    extraction never pages anyone, and ``skipped_accepted`` is why a breach
    somebody took the hit on, with a reason, stays accepted instead of becoming
    the loudest row on the manager's screen every quarter-hour.

    Runs SYNCHRONOUSLY in a threadpool (a Cloud Run background task is
    CPU-throttled the instant the response returns). One indexed query plus
    in-memory transitions over the live set — no external calls at all — so it
    finishes far inside the 300s request cap.
    """
    if not _is_authorized_run_caller(request, x_scheduler_key, credentials):
        raise HTTPException(status_code=403, detail="Unauthorized.")

    from starlette.concurrency import run_in_threadpool

    from backend.jobs.sales_kaizen import run_recompute_states

    report = await run_in_threadpool(
        lambda: _sweep_session(lambda s: run_recompute_states(s, dry_run=dry_run))
    )
    return {"trigger": "scheduler" if x_scheduler_key else "admin", **report}


@scheduler_router.post("/jobs/cockpit/stage-lag")
async def run_cockpit_stage_lag_endpoint(
    request: Request,
    dry_run: bool = Query(
        False, description="Detect and report, then roll the transaction back."
    ),
    limit: int | None = Query(
        None, description="Cap how many evidenced deals this run reads from HubSpot."
    ),
    x_scheduler_key: str | None = Header(None, alias="X-Scheduler-Key"),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    """Hourly — where HubSpot's stage disagrees with what Cubie heard on the call.

    Iterates the deals Cubie actually holds evidence for, not the pipeline: the
    detector is structurally incapable of emitting anything for a deal with no
    evidence, so walking every open deal would spend the HubSpot search quota (5
    req/s per account, 10k results per query) proving that.

    ``read_failed`` is its own bucket and its own WARNING line, because an
    accurate pipeline and a broken HubSpot lookup both produce zero exceptions.

    Runs SYNCHRONOUSLY in a threadpool — one HubSpot read per evidenced deal
    (single digits today), commits per deal so one malformed record cannot
    discard the batch. If the evidenced-deal count ever grows past a ~300s
    request, pass ``limit`` and read ``deals_deferred``, or move it to the Cloud
    Run Job (``python -m backend.jobs.sales_kaizen cockpit-stage-lag``).
    """
    if not _is_authorized_run_caller(request, x_scheduler_key, credentials):
        raise HTTPException(status_code=403, detail="Unauthorized.")

    from starlette.concurrency import run_in_threadpool

    from backend.jobs.sales_kaizen import run_stage_lag

    report = await run_in_threadpool(
        lambda: _sweep_session(lambda s: run_stage_lag(s, dry_run=dry_run, limit=limit))
    )
    return {"trigger": "scheduler" if x_scheduler_key else "admin", **report}


@scheduler_router.post("/jobs/cockpit/sow-sent")
async def run_cockpit_sow_sent_endpoint(
    request: Request,
    dry_run: bool = Query(
        False,
        description=(
            "Resolve which AEs hold a live SOW commitment and read NO mailbox."
        ),
    ),
    since_days: int | None = Query(
        None, description="How far back in Sent mail to look (default 30 days)."
    ),
    x_scheduler_key: str | None = Header(None, alias="X-Scheduler-Key"),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    """Hourly — evidence that a live SOW actually went out.

    Raises SUGGESTIONS only, never an auto-close: a wrong auto-close hides a real
    breach, which is the exact failure the cockpit exists to fix. Confirming is a
    human click.

    A mailbox is read only for an AE who holds a live SOW commitment a suggestion
    could close — the detector checks that before it touches Gmail, and
    ``dry_run`` here reports that roster without reading anyone.

    ⚠️ **Nothing is persisted.** ``scan_sent_mail`` returns suggestions and there
    is no column to park them on, so on a schedule this endpoint spends Gmail
    quota to produce a log line. ``persisted: false`` in the response says so.
    Run it by hand to verify the lane before creating the Cloud Scheduler
    entry.

    Runs SYNCHRONOUSLY in a threadpool. Bounded by the AEs with a live SOW
    commitment, each costing two Gmail searches and a few HubSpot reads; if that
    roster grows past a ~300s request, move it to the Cloud Run Job.
    """
    if not _is_authorized_run_caller(request, x_scheduler_key, credentials):
        raise HTTPException(status_code=403, detail="Unauthorized.")

    from starlette.concurrency import run_in_threadpool

    from backend.jobs.sales_kaizen import run_sow_sent

    report = await run_in_threadpool(
        lambda: _sweep_session(
            lambda s: run_sow_sent(s, dry_run=dry_run, since_days=since_days)
        )
    )
    return {"trigger": "scheduler" if x_scheduler_key else "admin", **report}


@scheduler_router.post("/jobs/cockpit/daily-digest")
async def run_cockpit_daily_digest_endpoint(
    request: Request,
    dry_run: bool = Query(
        False,
        description=(
            "Render what WOULD be sent, to whom, and send nothing. Ignores both "
            "cockpit flags on purpose — this is how the flags get reviewed."
        ),
    ),
    x_scheduler_key: str | None = Header(None, alias="X-Scheduler-Key"),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    """Weekdays 08:00 workspace business time — one daily digest
    per person, at most.

    Rule 1 is enforced by the database, not by this endpoint: a
    ``UniqueConstraint(recipient_email, kind, local_date)`` on ``digest_logs``
    decides a race between two executions, so re-running is safe and reports
    ``already_sent_today`` rather than sending twice. ``local_date`` is the
    RECIPIENT's local today, so an AE on Pacific time receives this at 06:00
    their time and the guard still keys on their day, not the org's.

    ``not_a_slack_workspace_member`` is a SKIP in its own bucket, never a
    failure: it is a roster row to clean up, not an outage to chase. Somebody
    with nothing actionable gets no message and no row — silence is a feature,
    and a row would burn their slot for the day.

    Runs SYNCHRONOUSLY in a threadpool: a Cloud Run background task is
    CPU-throttled the instant the response returns and silently never finishes.
    (An earlier note here said a Job could not do this work for want of a
    ``SLACK_BOT_TOKEN`` — that was wrong. Nothing reads such an env var; the
    bot token comes from Cloud SQL, which ``cubie-worker`` has. This path stays
    on the service because it is the one exercised in production.)
    """
    if not _is_authorized_run_caller(request, x_scheduler_key, credentials):
        raise HTTPException(status_code=403, detail="Unauthorized.")

    from starlette.concurrency import run_in_threadpool

    from backend.jobs.sales_kaizen import run_digest

    report = await run_in_threadpool(
        lambda: _sweep_session(lambda s: run_digest(s, kind="daily", dry_run=dry_run))
    )
    return {"trigger": "scheduler" if x_scheduler_key else "admin", **report}


@scheduler_router.post("/jobs/cockpit/weekly-aging")
async def run_cockpit_weekly_aging_endpoint(
    request: Request,
    dry_run: bool = Query(False, description="Render the roll-up and send nothing."),
    x_scheduler_key: str | None = Header(None, alias="X-Scheduler-Key"),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    """Mondays 08:10 workspace business time — the weekly aging
    roll-up.

    Same machinery as the daily digest with ``kind="weekly"``, and the ``kind``
    column is load-bearing rather than cosmetic: without it Monday's roll-up and
    Monday's daily digest suppress each other depending on which scheduler job
    fires first, and which one you lose changes week to week.

    Schedule this ten minutes after the daily job. They cannot suppress each
    other, but two Slack sweeps in the same second is a rate-limit that reads as
    a delivery failure — and the offset also lands Monday's two DMs in reading
    order: today's list, then the week's aging.

    Runs SYNCHRONOUSLY in a threadpool, for the same Slack-token reason as the
    daily digest.
    """
    if not _is_authorized_run_caller(request, x_scheduler_key, credentials):
        raise HTTPException(status_code=403, detail="Unauthorized.")

    from starlette.concurrency import run_in_threadpool

    from backend.jobs.sales_kaizen import run_digest

    report = await run_in_threadpool(
        lambda: _sweep_session(lambda s: run_digest(s, kind="weekly", dry_run=dry_run))
    )
    return {"trigger": "scheduler" if x_scheduler_key else "admin", **report}


# ── Loose Ends — the weekday-morning AE loop-closure scan ─────────────────
#
# Same conventions as the call-capture and cockpit sweeps above (named
# buckets, dry_run, a disabled flag is a 200 not a 404). The contract and
# runbook are docs/loose_ends/README.md; the scan is
# rubik_core/loop_closure/scan.py.

#: Every setting the scan reads. The Service hands its CURRENT values to the
#: Job execution as env (``launch_module_execution(env=…)``), so tuning —
#: raising LOOP_CLOSURE_OPEN_LOOP_BUSINESS_DAYS to 3 on the Service, say
#: (README §6) — reaches the morning scan without redeploying cubie-worker. The daily
#: Slack post's switch and channel are not here: they live in the database
#: (Admin → Loose Ends, ``loop_closure.digest_settings``), which the Job reads.
_LOOP_CLOSURE_ENV_SETTINGS = (
    "LOOP_CLOSURE_ENABLED",
    "LOOP_CLOSURE_LOOKBACK_DAYS",
    "LOOP_CLOSURE_OPEN_LOOP_BUSINESS_DAYS",
    "LOOP_CLOSURE_LOG_GRACE_BUSINESS_DAYS",
    "LOOP_CLOSURE_TODO_GRACE_DAYS",
    "LOOP_CLOSURE_TODO_UNDATED_DAYS",
    "LOOP_CLOSURE_TASK_LOOKBACK_DAYS",
    "LOOP_CLOSURE_ESCALATION_DAYS",
    "LOOP_CLOSURE_MAX_AGE_DAYS",
)


def _env_value(value) -> str:
    """A setting as the env string pydantic-settings parses back to the same value.

    Booleans as ``true``/``false``; a list as JSON (``str(["a"])`` would be the
    Python repr ``['a']``, which is not JSON and fails the Job at boot); None as
    an empty string.
    """
    import json

    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, set, frozenset)):
        return json.dumps(
            sorted(value) if isinstance(value, (set, frozenset)) else list(value)
        )
    if value is None:
        return ""
    return str(value)


def _loop_closure_env() -> dict[str, str]:
    return {
        name: _env_value(getattr(settings, name))
        for name in _LOOP_CLOSURE_ENV_SETTINGS
        if hasattr(settings, name)
    }


@scheduler_router.post("/jobs/loop-closure/scan")
async def run_loop_closure_scan_endpoint(
    request: Request,
    dry_run: bool = Query(
        False,
        description=(
            "Scan, report what WOULD be flagged and closed, then roll back. "
            "Posts no digest, writes no scorecard. Ignores LOOP_CLOSURE_ENABLED "
            "on purpose — this is how the scan gets reviewed before it is on."
        ),
    ),
    email: str | None = Query(
        None,
        description=(
            "Scan only this AE. With dry_run=true the scan runs inline and the "
            "full report comes back in the response."
        ),
    ),
    x_scheduler_key: str | None = Header(None, alias="X-Scheduler-Key"),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    """Weekdays 07:10 workspace business time — the AE loop-closure
    scan (Loose Ends).

    One central run reads each rostered AE's Gmail and Slack, reconciles it
    against HubSpot, and keeps ``loop_items`` current: new loose ends flagged,
    old ones re-checked against the system of record and closed only on
    evidence. Then the one daily Slack digest and, on its day, the scorecard.
    Scheduled after the 06:00 UTC priorities batch (so the promises it reads
    are fresh) and clear of the :00/:15 transcript sync.

    **A live run dispatches a Cloud Run Job; a one-AE dry run answers inline.**
    Over the whole roster the scan is a Gmail sweep, an engagement search and a
    Flash call per AE, which can outrun Cloud Run's 300s request cap. A dry run
    for ONE AE is a few seconds and returns the full report in the response —
    the runbook's step 3. If the Job launch fails (not deployed yet, IAM, local
    dev) the scan falls back to running synchronously in the threadpool —
    CPU-allocated because the request is still in flight, unlike a
    BackgroundTask — and says so in ``executor``. A run already in progress is
    not doubled: the scan's own run lock (a ``running`` row younger than 60
    minutes) refuses the second one.

    Flag off → 200 ``{"status": "disabled"}``, never 404: a 404 on a scheduler
    target reads as a broken job in Cloud Scheduler's alerting, on an
    environment that has simply not switched the feature on.

    ``email`` must be on the ``loop_closure`` roster (400 otherwise), and every
    run started by a person rather than the scheduler key writes a
    ``LOOSE_ENDS_MANUAL_SCAN`` audit row naming them.
    """
    if not _is_authorized_run_caller(request, x_scheduler_key, credentials):
        raise HTTPException(status_code=403, detail="Unauthorized.")

    from starlette.concurrency import run_in_threadpool

    trigger = "scheduler" if x_scheduler_key else "admin"
    one = (email or "").strip().lower() or None
    emails = [one] if one else None

    # ``email`` never widens who is read: only someone on the Loose Ends roster
    # (Admin → Feature Access) can be scanned, dry run or not. The roster is
    # deliberately exec- and admin-free; without this check any admin could
    # point the scan at any mailbox.
    if one is not None:

        def _on_roster(session) -> bool:
            from rubik_core.loop_closure.roster import roster_emails

            return one in roster_emails(
                session, deactivated_lookup=lambda _emails: set()
            )

        if not await run_in_threadpool(lambda: _sweep_session(_on_roster)):
            raise HTTPException(
                status_code=400,
                detail=(
                    "That address is not on the Loose Ends roster (Admin → Feature "
                    f"Access → Loose Ends), so {PRODUCT_NAME} will not scan it."
                ),
            )

    # A hand run reads other people's mailboxes: who started it is recorded.
    # (The scheduler's own key is the routine morning run, not a person.)
    if not _scheduler_key_ok(x_scheduler_key):
        actor = None
        try:
            actor = get_current_user(request, credentials).email
        except Exception:  # noqa: BLE001 — authorized above; best effort
            actor = None
        if actor:

            def _audit(session) -> None:
                from backend.audit import record_audit

                record_audit(
                    session,
                    actor_email=(actor or "").strip().lower(),
                    action="LOOSE_ENDS_MANUAL_SCAN",
                    target=one or "roster",
                    details={"dry_run": bool(dry_run)},
                )

            try:
                await run_in_threadpool(lambda: _sweep_session(_audit))
            except Exception as exc:  # noqa: BLE001 — never blocks the run
                from rubik_core.logsafe import safe_error

                logger.warning(
                    "[loop-closure] manual-scan audit row failed: %s", safe_error(exc)
                )

    # The flag check happens before the dispatch so a disabled feature does not
    # burn a Job execution every weekday morning to log one line.
    if not dry_run and not settings.LOOP_CLOSURE_ENABLED:
        logger.info("[loop-closure] LOOP_CLOSURE_ENABLED is off — no scan dispatched")
        return {
            "trigger": trigger,
            "executor": "none",
            "status": "disabled",
            "flag": "LOOP_CLOSURE_ENABLED",
            "dry_run": False,
        }

    def _run_inline(*, run_trigger: str):
        from rubik_core.loop_closure.scan import run_scan

        return _sweep_session(
            lambda s: run_scan(s, emails=emails, dry_run=dry_run, trigger=run_trigger)
        )

    if dry_run and emails:
        report = await run_in_threadpool(lambda: _run_inline(run_trigger="admin"))
        return {"trigger": trigger, "executor": "inline", **report}

    from backend.jobs.launcher import launch_module_execution

    args = ["--trigger", trigger]
    if dry_run:
        args.append("--dry-run")
    if emails:
        args += ["--emails", ",".join(emails)]
    env = _loop_closure_env()
    try:
        execution = await run_in_threadpool(
            lambda: launch_module_execution(
                "backend.jobs.loop_closure_scan_job", args, env=env
            )
        )
    except Exception as exc:  # noqa: BLE001 — degrade to synchronous, never skip a morning
        from rubik_core.logsafe import safe_error

        logger.error(
            "[loop-closure] Cloud Run Job launch failed (%s) — running the scan "
            "synchronously in the request instead. That is CPU-allocated (the "
            "request is still in flight) but bounded by the 300s cap, so a long "
            "roster may be cut short; AEs it did not reach keep yesterday's "
            "list. If the job is not deployed, restore it through the guarded platform deploy.",
            safe_error(exc),
        )
        report = await run_in_threadpool(lambda: _run_inline(run_trigger=trigger))
        return {"trigger": trigger, "executor": "inline_fallback", **report}

    execution_name = execution.get("name") or (execution.get("metadata") or {}).get(
        "name"
    )
    logger.info(
        "[loop-closure] dispatched to cubie-worker (execution=%s dry_run=%s scope=%s)",
        execution_name,
        dry_run,
        "one AE" if emails else "roster",
    )
    return {
        "trigger": trigger,
        "executor": "job",
        "status": "dispatched",
        "execution": execution_name,
        "dry_run": dry_run,
        "emails": emails or [],
    }


# ── Scheduled tasks — the due-task tick ───────────────────────────────────
#
# ONE scheduler entry drives every user's schedules: it claims whatever is due
# and launches a cubie-worker execution per task. Per-user cadence lives in the
# `scheduled_tasks` rows, not in Cloud Scheduler, which is the whole point —
# a user changing "8am" to "7:30" must not need a GCP change.
#
# Runs SYNCHRONOUSLY in a threadpool: the claim + launch work must finish
# before the response returns, or Cloud Run throttles it away mid-tick.


@scheduler_router.post("/scheduled-tasks/tick")
async def scheduled_tasks_tick(
    request: Request,
    dry_run: bool = Query(
        False,
        description=(
            "Report which tasks are due without claiming, launching or "
            "spending anything."
        ),
    ),
    limit: int | None = Query(
        None,
        description="Override how many due tasks this tick may dispatch.",
    ),
    x_scheduler_key: str | None = Header(None, alias="X-Scheduler-Key"),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    """Every ~5 minutes — run the scheduled tasks that have come due.

    Cadence sets the WORST-CASE lateness a user sees (a task set for 08:00 runs
    at 08:00-08:05), so the tick is cheap by design: one indexed query, and
    nothing else happens when nothing is due.

    Overlap is safe and a missed tick is self-healing: a task is claimed with a
    compare-and-swap on the ``next_run_at`` the tick read, so two overlapping
    ticks cannot both take one occurrence, and a task still due after an outage
    is picked up by the next tick. An overdue task runs ONCE — a schedule that
    slipped does not replay every occurrence it missed, because four copies of
    yesterday's digest at once is worse than a gap.

    NO-OPS with a log line when ``SCHEDULED_TASKS_ENABLED`` is off, rather than
    404ing: a 404 on a scheduler target reads as a broken job in Cloud
    Scheduler's own alerting, and this flag defaults false.
    """
    if not _is_authorized_run_caller(request, x_scheduler_key, credentials):
        raise HTTPException(status_code=403, detail="Unauthorized.")

    trigger = "scheduler" if x_scheduler_key else "admin"

    if not settings.SCHEDULED_TASKS_ENABLED:
        logger.info(
            "[scheduled-tasks] SCHEDULED_TASKS_ENABLED is off — nothing claimed"
        )
        return {
            "trigger": trigger,
            "status": "disabled",
            "flag": "SCHEDULED_TASKS_ENABLED",
            "dispatched": 0,
        }

    from starlette.concurrency import run_in_threadpool

    from rubik_core.scheduled_tasks import dispatch, store

    if dry_run:

        def _preview():
            from rubik_core.database.db import SessionLocal

            cap = limit or settings.SCHEDULED_TASKS_TICK_MAX_DISPATCH
            with SessionLocal() as db:
                due = store.due_tasks(db, datetime.utcnow(), cap)
                return {
                    "status": "dry_run",
                    "due": len(due),
                    "tasks": [
                        {
                            "task_id": t.id,
                            "user_email": t.user_email,
                            "title": t.title,
                            "next_run_at": t.next_run_at.isoformat()
                            if t.next_run_at
                            else None,
                        }
                        for t in due
                    ],
                }

        return {"trigger": trigger, **await run_in_threadpool(_preview)}

    report = await run_in_threadpool(lambda: dispatch.dispatch_due_tasks(limit=limit))
    # Meeting routines ride the same tick: a transcript that became ready since
    # the last tick has queued its fires, and each gets a worker here. The
    # report is its own bucket so the log line still says what happened.
    from rubik_core.meeting_skills import routines

    report["meeting_routines"] = await run_in_threadpool(
        lambda: routines.dispatch_queued_fires(limit=limit)
    )
    return {"trigger": trigger, "status": "completed", **report}


# ── What's New — the morning post of new release notes (weekdays) ─────────


@scheduler_router.post("/whats-new/digest")
async def run_whats_new_digest(
    request: Request,
    dry_run: bool = Query(
        False,
        description=(
            "Build the morning post from what is due and return its text. "
            "Nothing posted, nothing recorded (works with posting off too)."
        ),
    ),
    x_scheduler_key: str | None = Header(None, alias="X-Scheduler-Key"),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    """Weekday mornings — post the Cubie release notes published since the
    last post, in ONE message in the channel set in Admin → What's New
    (``rubik_core/whats_new/digest.py``, spec §4.6.1).

    Triggered by Cloud Scheduler (``X-Scheduler-Key``) or an authenticated
    admin (the shared ``_is_authorized_run_caller``). Runs SYNCHRONOUSLY in a
    threadpool (a Cloud Run background task is CPU-throttled the instant the
    response returns); the work is a read of the changelog, a few queries and one
    ``chat.postMessage`` with an 8 s timeout and no retry.

    NO-OPS with 200 ``{"status": "disabled"}`` when ``WHATS_NEW_ENABLED`` is
    off, never 404 (a 404 on a scheduler target reads as a broken job).
    Every other outcome is a named status from ``post_digest``. Safe to
    overlap: a run holds a lock for its whole post, so no note is posted
    twice, and a second run answers ``busy``. The Scheduler job
    (``whats-new-digest``) is NOT created by code (a GCP-side step).
    """
    if not _is_authorized_run_caller(request, x_scheduler_key, credentials):
        raise HTTPException(status_code=403, detail="Unauthorized.")

    trigger = "scheduler" if x_scheduler_key else "admin"

    if not settings.WHATS_NEW_ENABLED:
        logger.info("[whats-new] WHATS_NEW_ENABLED is off — no morning post")
        return {"trigger": trigger, "status": "disabled", "flag": "WHATS_NEW_ENABLED"}

    from starlette.concurrency import run_in_threadpool

    from rubik_core.whats_new.digest import post_digest

    result = await run_in_threadpool(
        lambda: _sweep_session(lambda s: post_digest(s, dry_run=dry_run))
    )
    return {"trigger": trigger, **result}

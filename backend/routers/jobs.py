"""
Jobs router — handles scheduled and manual pipeline triggers.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
)
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials
from google.adk.runners import Runner
from google.genai import types
from pydantic import BaseModel
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from backend.auth import get_current_user, security
from backend.jobs.launcher import (
    PRIORITIES_JOB_NAME,
    JobLaunchError,
    launch_module_execution,
    reject_support_background_work,
    run_job_request,
)
from rubik_core.ai_providers.adk_model import resolve_adk_route
from rubik_core.ai_providers.registry import IncompatibleModelRoute
from rubik_core.ai_providers.runtime import ProviderRequestError
from rubik_core.config import settings
from rubik_core.cost.usage import attribute_to
from rubik_core.database.db import get_db
from rubik_core.identifiers import NIGHTLY_APP_NAME
from rubik_core.orchestration.session_service import get_session_service
from rubik_core.telemetry import set_admin_context
from rubik_core.tenancy.scheduler import scheduler_key_valid
from rubik_core.tenancy.threads import ContextThreadPoolExecutor

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["jobs"])


class BriefingRequest(BaseModel):
    target_email: str


@attribute_to("nightly_pipeline")
async def _run_nightly_pipeline(target_email: str):
    """Execute the nightly intelligence pipeline for a user.

    This is designed to be idempotent: running it twice for the same date
    will update existing HomeCards rather than creating duplicates.
    """
    set_admin_context(True)
    from rubik_core.agent_runtime.nightly_pipeline import build_nightly_pipeline

    logger.info("Starting nightly pipeline for %s", target_email)

    route = resolve_adk_route(
        "nightly_pipeline", default_model=settings.VERTEX_LLM_MODEL_FLASH
    )
    prompt_tokens = completion_tokens = thinking_tokens = cached_tokens = 0
    try:
        pipeline = build_nightly_pipeline(target_email, model_route=route)
        session_service = get_session_service()

        runner = Runner(
            agent=pipeline,
            app_name=NIGHTLY_APP_NAME,
            session_service=session_service,
        )

        # Create a fresh session for this pipeline run
        session = await session_service.create_session(
            app_name=NIGHTLY_APP_NAME,
            user_id=target_email,
        )

        # Trigger the pipeline with a simple activation message
        user_content = types.Content(
            role="user",
            parts=[
                types.Part(
                    text=f"Run nightly briefing pipeline for {target_email}. Today's date is included in the system context."
                )
            ],
        )

        final_response = None
        async for event in runner.run_async(
            user_id=target_email,
            session_id=session.id,
            new_message=user_content,
        ):
            if (
                not getattr(event, "partial", False)
                and getattr(event, "usage_metadata", None) is not None
            ):
                from rubik_core.cost.usage import extract_usage

                usage = extract_usage(event)
                prompt_tokens += usage["prompt"]
                completion_tokens += usage["completion"]
                thinking_tokens += usage["thinking"]
                cached_tokens += usage["cached"]
            if event.is_final_response():
                if event.content and event.content.parts:
                    final_response = " ".join(
                        p.text for p in event.content.parts if p.text
                    )

        logger.info(
            "Nightly pipeline completed for %s: %s",
            target_email,
            (final_response or "no response")[:200],
        )
    except (IncompatibleModelRoute, ProviderRequestError):
        logger.warning("Nightly pipeline selected provider failed for %s", target_email)
        raise
    except Exception as exc:
        if route.provider != "vertex_managed":
            logger.warning(
                "Nightly pipeline selected provider failed for %s (%s)",
                target_email,
                type(exc).__name__,
            )
            raise ProviderRequestError(
                "Selected workspace AI provider failed in nightly pipeline"
            ) from None
        logger.exception("Nightly pipeline failed for %s", target_email)
        raise
    finally:
        if prompt_tokens or completion_tokens or thinking_tokens or cached_tokens:
            from rubik_core.cost.usage import record_usage

            try:
                record_usage(
                    operation="generate",
                    model=route.model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    thinking_tokens=thinking_tokens,
                    cached_tokens=cached_tokens,
                    feature="nightly_pipeline",
                    user_email=target_email,
                    metadata={
                        "provider": route.provider,
                        "billing_owner": route.billing_owner,
                    },
                )
            except Exception:  # noqa: BLE001
                logger.debug("Nightly pipeline AI cost record failed", exc_info=True)


def _run_pipeline_sync(target_email: str):
    """Synchronous wrapper for running the async pipeline in a background task."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run_nightly_pipeline(target_email))
    finally:
        loop.close()


@router.post("/overnight-briefing")
async def trigger_briefing(
    request: Request,
    payload: BriefingRequest,
):
    """
    Triggers the nightly intelligence pipeline for a user.

    The pipeline runs in a workspace-scoped Cloud Run Job and produces HomeCard artifacts
    that appear on the user's homefeed. It collects signals from Gmail,
    Calendar, and CRM, then normalizes, enriches, and validates them.

    In production, this endpoint is triggered by Cloud Scheduler.
    For development, any authenticated user can trigger it for themselves.
    Admins can trigger it for any user.
    """
    reject_support_background_work()
    # Cloud Scheduler authenticates with the shared X-Scheduler-Key secret
    # (constant-time compare; fails closed when the key is unset). The old
    # X-CloudScheduler header was an unauthenticated bypass — it is client-
    # settable and the service is --allow-unauthenticated, so anyone could
    # forge it and trigger the pipeline for any user (SOC 2 finding H-1).
    scheduler_key = request.headers.get("X-Scheduler-Key")
    is_scheduler = scheduler_key_valid(scheduler_key)

    if not is_scheduler:
        # Manual invocation: require an authenticated user.
        try:
            from backend.auth import get_current_user, security

            auth_header = await security(request)
            user = get_current_user(request, auth_header)
        except HTTPException:
            raise
        except Exception as e:
            logger.warning(f"Unauthorized access attempt to /overnight-briefing: {e}")
            raise HTTPException(status_code=401, detail="Unauthorized")

        if user is None:
            raise HTTPException(status_code=401, detail="Unauthorized")

        if (
            "admin" not in user.roles
            and payload.target_email.lower() != user.email.lower()
        ):
            raise HTTPException(
                status_code=403,
                detail="You can only trigger a briefing for your own email unless you are an admin.",
            )

    target = payload.target_email.strip().lower()
    if "@" not in target:
        raise HTTPException(status_code=404, detail="Target user not found")
    from rubik_core.control.repository import domain_belongs_to_workspace
    from rubik_core.tenancy.context import current_workspace

    belongs = await run_in_threadpool(
        domain_belongs_to_workspace, current_workspace(), target.rsplit("@", 1)[1]
    )
    if not belongs:
        raise HTTPException(status_code=404, detail="Target user not found")

    try:
        execution = await run_in_threadpool(
            launch_module_execution,
            "backend.jobs.overnight_briefing_job",
            ["--target-email", target],
        )
    except JobLaunchError as exc:
        logger.exception("Nightly briefing Job launch failed")
        raise HTTPException(
            status_code=502, detail="Nightly briefing Job launch failed"
        ) from exc

    return {
        "status": "dispatched",
        "execution": execution.get("name")
        or (execution.get("metadata") or {}).get("name"),
        "target_email": target,
    }


# ─── UC-40: daily scheduler for the multi-agent priorities pipeline ──────────
#
# Pre-warms ``HomeCard`` rows (category=daily_briefing) so the on-demand
# ``POST /api/homefeed/priorities`` endpoint can read from cache instead of
# running the 3–5 minute pipeline synchronously. Cloud Scheduler hits this
# at 06:00 UTC daily; ad-hoc admin runs are also allowed.
#


class PrioritiesUserResult(BaseModel):
    user_email: str
    succeeded: bool
    priorities_count: int = 0
    duration_seconds: float = 0.0
    error: str | None = None


class PrioritiesRunAllResponse(BaseModel):
    run_id: str
    trigger: str  # "scheduler" | "admin"
    started_at: str
    completed_at: str
    users_total: int
    users_succeeded: int
    users_failed: int
    failures: list[PrioritiesUserResult] = []


def _is_authorized_scheduler_caller(
    request: Request,
    x_scheduler_key: str | None,
    credentials: HTTPAuthorizationCredentials | None,
) -> tuple[bool, str]:
    """Allow Cloud Scheduler (header key) or an authenticated admin.

    Returns ``(authorized, trigger_label)`` so the response can report
    which path authorized this run — useful for distinguishing scheduled
    vs. ad-hoc runs in Cloud Logging.
    """
    if scheduler_key_valid(x_scheduler_key):
        return True, "scheduler"
    if x_scheduler_key is not None:
        return False, "none"
    if credentials:
        try:
            user = get_current_user(request, credentials)
            if user.is_admin:
                return True, "admin"
        except Exception:
            return False, "none"
    return False, "none"


@attribute_to("ai_priorities", user_arg="user_email")
def _run_priorities_for_user(user_email: str) -> PrioritiesUserResult:
    """Run the multi-agent priorities pipeline for one user and persist.

    Mirrors what ``_run_and_persist_priorities`` in ``backend/routers/home.py``
    does on the on-demand path, but isolated so a single user's failure
    can't take down the whole scheduled run.
    """
    from rubik_core.priorities import (
        GENERATION_EMPTY_WITH_SIGNALS,
        PrioritiesOrchestrator,
        PrioritiesRequest,
        bundle_to_metadata,
        persist_bundle,
    )
    from rubik_core.priorities.temporal import user_today_str

    start = datetime.now(timezone.utc)
    try:
        request = PrioritiesRequest(user_email=user_email, max_priorities=5)
        bundle = PrioritiesOrchestrator().generate(request)

        if getattr(bundle, "generation_status", None) == GENERATION_EMPTY_WITH_SIGNALS:
            # Same delivery gate as rubik_core/priorities/batch.py — an
            # unjustified empty bundle is a failure, never a persisted card.
            duration = (datetime.now(timezone.utc) - start).total_seconds()
            return PrioritiesUserResult(
                user_email=user_email,
                succeeded=False,
                duration_seconds=round(duration, 2),
                error="empty_with_signals: generation produced an "
                "unjustified empty bundle",
            )

        # Key the card on the user's LOCAL date (see batch.py for why).
        today_str = user_today_str(user_email)
        persist_bundle(user_email, today_str, bundle)

        metadata = bundle_to_metadata(bundle)
        duration = (datetime.now(timezone.utc) - start).total_seconds()
        return PrioritiesUserResult(
            user_email=user_email,
            succeeded=True,
            priorities_count=len(metadata.get("priorities") or []),
            duration_seconds=round(duration, 2),
        )
    except Exception as exc:  # noqa: BLE001
        duration = (datetime.now(timezone.utc) - start).total_seconds()
        logger.exception(
            "[uc40] priorities pipeline failed for %s after %.1fs",
            user_email,
            duration,
        )
        return PrioritiesUserResult(
            user_email=user_email,
            succeeded=False,
            duration_seconds=round(duration, 2),
            error=f"{type(exc).__name__}: {exc}",
        )


def _list_active_users() -> list[str]:
    """The users a run-all generates for, as emails.

    ``rubik_core.priorities.batch.list_active_users``, the batch Job's own
    roster (without the leavers lookup, which is a network call this trigger
    only needs a count from), so the count the trigger reports is the
    population the Job will iterate. See ``priorities_roster`` for who is on
    it: everyone on the Workspace domain who is not a seed or impersonation
    fixture and reads the bundle, whatever their department.
    """
    from rubik_core.priorities.batch import list_active_users

    return list_active_users()


# UC-40.1: hard per-user timeout so one stuck user (e.g. Gmail OAuth retry
# loop on a non-Workspace email) can't freeze the entire batch. Cloud Run
# Service can't run a batch for hours regardless, but at minimum a single
# bad user shouldn't block the rest from getting their cards. Cap chosen
# at 600s (10 min) — well above the 3-5 min typical pipeline cost, well
# below the Cloud Run instance natural lifetime.
PER_USER_TIMEOUT_SECONDS = 600


def _run_user_with_timeout(user_email: str) -> "PrioritiesUserResult":
    """Run one user's pipeline with a hard timeout, returning a failure
    result rather than blocking the loop if it hangs.

    The context-aware pool carries the workspace, AI-Cost attribution and
    request id into the worker thread.
    """
    import concurrent.futures

    with ContextThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_run_priorities_for_user, user_email)
        try:
            return future.result(timeout=PER_USER_TIMEOUT_SECONDS)
        except concurrent.futures.TimeoutError:
            logger.warning(
                "[uc40] per-user timeout (%ds) for %s — moving on",
                PER_USER_TIMEOUT_SECONDS,
                user_email,
            )
            return PrioritiesUserResult(
                user_email=user_email,
                succeeded=False,
                duration_seconds=float(PER_USER_TIMEOUT_SECONDS),
                error=f"timeout after {PER_USER_TIMEOUT_SECONDS}s",
            )


class PrioritiesRunAllStartedResponse(BaseModel):
    run_id: str
    trigger: str  # "scheduler" | "admin"
    status: str = "started"
    users_total: int


def _run_all_priorities_background(run_id: str, user_emails: list[str]) -> None:
    """Background-task body: iterate every user, log per-user summary.

    Returning the per-user summary in the HTTP response would make us hit
    Cloud Run's 300 s request timeout once we cross a few users (each user
    is a 3–5 min pipeline). Instead we fire-and-forget and let Cloud
    Logging capture the per-user detail.
    """
    started_at = datetime.now(timezone.utc)
    logger.info(
        "[uc40] background run starting run_id=%s users=%d",
        run_id,
        len(user_emails),
    )
    set_admin_context(True)
    results: list[PrioritiesUserResult] = []
    for email in user_emails:
        result = _run_user_with_timeout(email)
        results.append(result)
        logger.info(
            "[uc40] run_id=%s user=%s succeeded=%s priorities=%d duration=%.1fs%s",
            run_id,
            result.user_email,
            result.succeeded,
            result.priorities_count,
            result.duration_seconds,
            f" error={result.error}" if result.error else "",
        )

    completed_at = datetime.now(timezone.utc)
    succeeded = sum(1 for r in results if r.succeeded)
    failed = sum(1 for r in results if not r.succeeded)
    logger.info(
        "[uc40] background run complete run_id=%s users=%d succeeded=%d "
        "failed=%d total_duration=%.1fs",
        run_id,
        len(results),
        succeeded,
        failed,
        (completed_at - started_at).total_seconds(),
    )


# UC-42: launch a Cloud Run Job execution instead of running in-process.
#
# Why this changed
# ----------------
# Pre-UC-42, this endpoint queued a FastAPI BackgroundTask that ran the
# pipeline for all users in-process on the Cloud Run *Service* instance.
# That doesn't work — the Service has cpu-throttling=true + minScale=0,
# so after the response returns Cloud Run sees the instance as idle and
# scales it down within ~10–30 min, and a run started that way produces
# 0 HomeCards.
#
# Cloud Run *Jobs* are the right tool: each execution runs in a dedicated
# instance with un-throttled CPU and a configurable timeout up to 24 h.
# Same container image, different entrypoint (the batch_runner module).
# This endpoint now just KICKS OFF a Job execution via the Cloud Run
# Admin REST API and returns the execution name. No in-process work.


# PRIORITIES_JOB_NAME is shared with the on-demand path (backend/routers/
# home.py) and lives in backend/jobs/launcher.py.
# Must match the job's configured entrypoint in deploy/jobs.yaml
# (args="-m,rubik_core.priorities.batch_runner"). Cloud Run v2
# ContainerOverride.args REPLACE the configured args rather than appending —
# any override body MUST re-include this prefix or the execution runs
# `python --run-id <uuid>` and crashes.
PRIORITIES_JOB_MODULE = "rubik_core.priorities.batch_runner"


def _build_priorities_job_run_body(
    *,
    target_email: str | None,
    run_id: str | None,
    report: str | None = None,
) -> dict:
    """Build the Cloud Run Admin v2 ``:run`` request body.

    Triggering with ``overrides.containerOverrides`` requires the caller to
    hold ``run.jobs.runWithOverrides`` (``roles/run.developer``), which is a
    step up from the plain ``run.jobs.run`` covered by ``roles/run.invoker``.
    So we only set overrides when we actually have flags to pass — an
    override-free ``{}`` body keeps working under ``run.invoker`` alone.

    ``report="next_week"`` runs the Friday look-ahead; anything else (``None``
    or ``"daily"``) runs the everyday daily batch.
    """
    overrides_args: list[str] = []
    if report and report != "daily":
        overrides_args.extend(["--report", report])
    if run_id:
        overrides_args.extend(["--run-id", run_id])
    if target_email:
        overrides_args.extend(["--target-email", target_email])

    if not overrides_args:
        return {}

    overrides: dict = {
        "containerOverrides": [
            {"args": ["-m", PRIORITIES_JOB_MODULE, *overrides_args]}
        ],
    }
    # Single-user re-runs need exactly ONE task. The job is deployed with
    # --tasks=4 for the sharded daily batch; without this override a
    # target_email run boots four tasks — shard 0 does the work, shards 1–3
    # boot Python just to exit 0. Full-batch runs (run_id-only overrides) must
    # NOT set taskCount, or they'd lose the sharding that keeps the batch
    # under the job timeout (without it a full batch times out with zero
    # cards).
    if target_email:
        overrides["taskCount"] = 1

    return {"overrides": overrides}


def _launch_priorities_job_execution(
    *,
    target_email: str | None = None,
    run_id: str | None = None,
    report: str | None = None,
) -> dict:
    """Trigger a Cloud Run Job execution for the priorities batch.

    Builds the run body (single-user runs pin ``taskCount=1``) and posts it
    via the shared launcher (``backend/jobs/launcher.py``), which
    authenticates as the service's RUNTIME service account through the
    metadata server — see the ADC-hygiene note there. Returns the execution
    metadata (name, uid, etc.) on success; raises HTTPException on failure so
    the FastAPI handler can surface it cleanly.

    ``report="next_week"`` runs the Friday look-ahead; anything else runs
    the everyday daily batch.
    """
    body = _build_priorities_job_run_body(
        target_email=target_email, run_id=run_id, report=report
    )
    try:
        return run_job_request(PRIORITIES_JOB_NAME, body)
    except JobLaunchError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


class PrioritiesRunAllRequest(BaseModel):
    """Optional body for /priorities/run-all.

    Lets an admin re-run the pipeline for a single user without
    launching the whole batch — useful for "rebuild this person's card
    for today, the morning run had a bad refresh window."
    """

    target_email: str | None = None


@router.post("/priorities/run-all", response_model=PrioritiesRunAllStartedResponse)
async def run_priorities_for_all_users(
    request: Request,
    body: PrioritiesRunAllRequest | None = None,
    x_scheduler_key: str | None = Header(None, alias="X-Scheduler-Key"),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    """Kick off a ``cubie-priorities-batch`` Cloud Run Job execution.

    Returns 200 immediately with the execution name. The actual
    per-user work runs in the Job (UC-42). Per-user outcomes land in
    Cloud Logging under ``[uc40] run_id=<id> user=<email> ...``.

    Body (optional): ``{"target_email": "user@acme.example"}`` runs
    only for that user (ad-hoc admin re-run). Omit to run for every
    eligible user.
    """
    reject_support_background_work()
    authorized, trigger = _is_authorized_scheduler_caller(
        request, x_scheduler_key, credentials
    )
    if not authorized:
        raise HTTPException(status_code=403, detail="Unauthorized.")

    run_id = str(uuid.uuid4())
    target_email = body.target_email if body else None

    # Count eligible users up front so the response can report it. If
    # target_email is set, users_total=1 by construction.
    if target_email:
        users_total = 1
    else:
        try:
            users_total = len(_list_active_users())
        except Exception:  # noqa: BLE001
            users_total = 0  # don't fail the trigger on a count error

    try:
        execution = await run_in_threadpool(
            _launch_priorities_job_execution,
            target_email=target_email,
            run_id=run_id,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("[uc42] failed to launch cubie-priorities-batch job")
        raise HTTPException(
            status_code=502,
            detail=f"Failed to launch cubie-priorities-batch Cloud Run Job: {exc}",
        )

    execution_name = execution.get("name") or execution.get("metadata", {}).get("name")
    logger.info(
        "[uc42] cubie-priorities-batch job execution launched run_id=%s trigger=%s "
        "users_total=%d execution=%s target_email=%s",
        run_id,
        trigger,
        users_total,
        execution_name,
        target_email,
    )

    return PrioritiesRunAllStartedResponse(
        run_id=run_id,
        trigger=trigger,
        status="started",
        users_total=users_total,
    )


@router.post(
    "/priorities/run-next-week", response_model=PrioritiesRunAllStartedResponse
)
async def run_next_week_priorities(
    request: Request,
    body: PrioritiesRunAllRequest | None = None,
    x_scheduler_key: str | None = Header(None, alias="X-Scheduler-Key"),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    """Kick off the Friday NEXT-WEEK look-ahead ``cubie-priorities-batch`` execution.

    Same mechanism as ``/priorities/run-all`` (launches the Cloud Run Job and
    returns immediately), but passes ``--report next_week`` so the pipeline
    widens its horizon to next week and persists cards under
    ``next_week_report``. Cloud Scheduler hits this Friday 3pm ET
    (``priorities-next-week`` job); ad-hoc admin runs are also allowed.

    Body (optional): ``{"target_email": "user@acme.example"}`` runs only
    for that user (ad-hoc). Omit to run for every eligible user.
    """
    reject_support_background_work()
    authorized, trigger = _is_authorized_scheduler_caller(
        request, x_scheduler_key, credentials
    )
    if not authorized:
        raise HTTPException(status_code=403, detail="Unauthorized.")

    run_id = str(uuid.uuid4())
    target_email = body.target_email if body else None

    if target_email:
        users_total = 1
    else:
        try:
            users_total = len(_list_active_users())
        except Exception:  # noqa: BLE001
            users_total = 0

    try:
        execution = await run_in_threadpool(
            _launch_priorities_job_execution,
            target_email=target_email,
            run_id=run_id,
            report="next_week",
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("[uc42] failed to launch next-week cubie-priorities-batch job")
        raise HTTPException(
            status_code=502,
            detail=f"Failed to launch next-week cubie-priorities-batch Cloud Run Job: {exc}",
        )

    execution_name = execution.get("name") or execution.get("metadata", {}).get("name")
    logger.info(
        "[uc42] next-week cubie-priorities-batch job execution launched run_id=%s "
        "trigger=%s users_total=%d execution=%s target_email=%s",
        run_id,
        trigger,
        users_total,
        execution_name,
        target_email,
    )

    return PrioritiesRunAllStartedResponse(
        run_id=run_id,
        trigger=trigger,
        status="started",
        users_total=users_total,
    )


# ── Ingestion Council — post-ingestion QA review over the ingestion jobs ──
#
# An adaptation of the LLM-council pattern (github.com/tenfoldmarc/llm-council-skill)
# to ingestion QA: five adversarial auditors assess the same evidence, peer-review
# each other anonymously, and a chairman issues a PASS/WARN/FAIL verdict. Lives on
# the jobs router (ungated) — not the pilot-gated copilot router — because Cloud
# Scheduler calls it with only an X-Scheduler-Key, no user token.


@router.post("/ingestion-council/run")
async def run_ingestion_council(
    request: Request,
    scope: str = Query("nightly", pattern="^(nightly|weekly|all|priorities-gate)$"),
    x_scheduler_key: str | None = Header(None, alias="X-Scheduler-Key"),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    """Convene the Ingestion Council over the requested scope's ingestion runs.

    A chairman issues a PASS/WARN/FAIL verdict that is persisted and DM'd to the
    ops audience (configured recipients, else configured department, else
    the workspace's admins).

    - ``scope=nightly`` reviews the overnight jobs (sales agent, AI-Priorities,
      transcript sync). Point a Cloud Scheduler job here ~08:00 UTC, after them.
    - ``scope=weekly`` reviews the EOS sweep — a second job, Mondays after it.
    - ``scope=priorities-gate`` runs the per-user priorities quality gate
      (observe-only phase 2; no-ops unless ``CUBIE_PRIORITIES_GATE_MODE`` is
      set). Point a third job here ~07:30 UTC, between generation and delivery.

    Runs SYNCHRONOUSLY, offloaded to a threadpool so it doesn't block the event
    loop. The whole council (5-11 LLM calls, ~1-2 min) must finish while the
    request is in flight so Cloud Run keeps CPU allocated — a background task
    would be CPU-throttled the instant the response returns and never complete
    (the same reason priorities moved to a Job, UC-42). The council's per-stage
    timeouts keep it well under the 300s Cloud Run request cap.

    Optional JSON body ``{"emails": [...]}`` narrows the configured DM audience.
    """
    reject_support_background_work()
    if scope in ("weekly", "all"):
        from rubik_core.control.repository import module_enabled

        if not await run_in_threadpool(module_enabled, "eos"):
            raise HTTPException(status_code=404, detail="EOS is not enabled")
    authorized, trigger = _is_authorized_scheduler_caller(
        request, x_scheduler_key, credentials
    )
    if not authorized:
        raise HTTPException(status_code=403, detail="Unauthorized.")

    emails: list[str] | None = None
    try:
        body = await request.json()
        if isinstance(body, dict) and isinstance(body.get("emails"), list):
            emails = [str(e).strip() for e in body["emails"] if str(e).strip()] or None
    except Exception:
        emails = None  # no/invalid body (scheduler path) → default recipients

    if trigger == "scheduler":
        args = ["--scope", scope]
        if emails:
            args += ["--emails", ",".join(emails)]
        try:
            execution = await run_in_threadpool(
                launch_module_execution, "backend.agents.ingestion_council", args
            )
        except JobLaunchError as exc:
            logger.exception("[ingestion-council] scheduled Job launch failed")
            raise HTTPException(
                status_code=502, detail="Scheduled Job launch failed"
            ) from exc
        return {
            "status": "dispatched",
            "scope": scope,
            "trigger": trigger,
            "execution": execution.get("name")
            or (execution.get("metadata") or {}).get("name"),
        }

    from backend.agents.ingestion_council import run_and_deliver

    result = await run_in_threadpool(run_and_deliver, scope, emails=emails)
    logger.info(
        "[ingestion-council] completed: scope=%s health=%s verdict_id=%s "
        "delivered=%s/%s errors=%s",
        result.scope,
        result.overall_health,
        result.verdict_id,
        result.delivered,
        result.recipients,
        result.errors,
    )
    return {
        "status": "completed",
        "scope": scope,
        "trigger": trigger,
        "overall_health": result.overall_health,
        "verdict_id": result.verdict_id,
        "delivered": result.delivered,
        "recipients": result.recipients,
    }


@router.get("/ingestion-council/runs")
def list_ingestion_council_runs(
    request: Request,
    scope: str | None = Query(None, pattern="^(nightly|weekly|all)$"),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    """Recent council verdicts (admins only), newest first."""
    authorized, _ = _is_authorized_scheduler_caller(request, None, credentials)
    if not authorized:
        raise HTTPException(status_code=403, detail="Unauthorized.")
    from rubik_core.ingestion_council import latest_verdicts
    from rubik_core.ingestion_council.persist import cached_verdict_visible

    rows = latest_verdicts(db, limit=limit, scope=scope)
    return [
        {
            "id": r.id,
            "scope": r.scope,
            "overall_health": r.overall_health,
            "headline": (
                r.headline
                if cached_verdict_visible(r.verdict_json)
                else "Report unavailable: source access changed"
            ),
            "created_at": r.created_at,
        }
        for r in rows
    ]


@router.get("/ingestion-council/runs/{verdict_id}/report", response_class=HTMLResponse)
def get_ingestion_council_report(
    verdict_id: str,
    request: Request,
    db: Session = Depends(get_db),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    """The self-contained HTML briefing for one run (admins only)."""
    authorized, _ = _is_authorized_scheduler_caller(request, None, credentials)
    if not authorized:
        raise HTTPException(status_code=403, detail="Unauthorized.")
    from rubik_core.ingestion_council import get_verdict
    from rubik_core.ingestion_council.persist import cached_verdict_visible

    row = get_verdict(db, verdict_id)
    if (
        row is None
        or not row.report_html
        or not cached_verdict_visible(row.verdict_json)
    ):
        raise HTTPException(status_code=404, detail="Report not found.")
    return HTMLResponse(content=row.report_html)

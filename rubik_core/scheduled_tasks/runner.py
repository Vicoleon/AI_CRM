"""Execute one scheduled run: replay a stored prompt as its owner.

This is the part that makes the feature real — everything else schedules,
claims and delivers. It runs on a ``cubie-worker`` Cloud Run Job task, so it
has CPU to completion, and it builds the SAME chat agent a live turn does (via
``backend.services.chat_orchestrator``), with the confirmation-gated tools and
sub-agents removed because nobody is present to approve anything.

Three failure modes worth knowing about, each guarded below:

1. **A "successful" failure.** ``ADKOrchestrator.run`` never raises — it
   catches everything and returns a friendly apology AS THE ANSWER. Delivering
   that as a result would DM the user "I encountered an unexpected issue" and
   record a success, so :func:`_answer_failed` inspects the trace instead of
   trusting that an answer came back.
2. **A retried job task.** Cloud Run re-runs a crashed task, so the first act
   here is a compare-and-swap on the run row; losing it means this is a retry
   of work already done, and the process exits quietly.
3. **A revoked user.** An offboarded owner has no ``users`` row and a
   department that lost the feature has no gate, so both are checked before any
   tokens are spent — but a gate that merely failed to READ is not a denial
   (the policy layer fails safe to the code default), so a denial is recorded
   as a skip and only auto-pauses after the usual failure streak.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from rubik_core.brand import PRODUCT_NAME
from rubik_core.config import settings
from rubik_core.cost.usage import attribute_to
from rubik_core.scheduled_tasks import delivery, store
from rubik_core.scheduled_tasks import schedule as sched

logger = logging.getLogger(__name__)


@dataclass
class RunOutcome:
    """What happened, for the job entrypoint to log."""

    run_id: str
    status: str  # succeeded | failed | skipped | already_claimed
    reason: str = ""
    chat_session_id: str | None = None
    delivery: dict = field(default_factory=dict)


def execute_run(run_id: str) -> RunOutcome:
    """Run one queued ``scheduled_task_runs`` row end to end. Never raises."""
    from rubik_core.database.db import SessionLocal

    with SessionLocal() as db:
        run = store.get_run(db, run_id)
        if run is None:
            logger.error("[scheduled-tasks] run=%s not found", run_id)
            return RunOutcome(run_id=run_id, status="failed", reason="run_not_found")

        # (2) Claim first: a lost claim means a retry of finished work.
        if not store.claim_run_running(db, run_id):
            logger.warning(
                "[scheduled-tasks] run=%s was already claimed (status=%s) — this "
                "invocation is a retry of work that already ran; exiting without "
                "re-delivering",
                run_id,
                run.status,
            )
            return RunOutcome(run_id=run_id, status="already_claimed")

        task = store.get_task(db, run.task_id)
        if task is None:
            store.finish_run(
                db, run_id, status="failed", error="Its scheduled task is gone."
            )
            return RunOutcome(run_id=run_id, status="failed", reason="task_missing")

        task_id = task.id
        user_email = task.user_email
        title = task.title
        prompt = task.prompt
        tz_name = task.timezone
        is_once = task.schedule_kind == "once"
        folder_id = run.chat_folder_id

    # (3) Who is this for, and may they still have it?
    profile = _resolve_profile(user_email)
    if profile is None:
        return _skip(
            run_id,
            task_id,
            reason="user_missing",
            message=(
                f"{user_email} no longer has a {PRODUCT_NAME} account, so this task cannot run."
            ),
            pause=True,
            pause_reason=f"The person this task belongs to no longer has a {PRODUCT_NAME} account.",
        )

    if not _feature_allowed(profile):
        return _skip(
            run_id,
            task_id,
            reason="access_denied",
            message=(
                f"{user_email} cannot currently reach scheduled tasks, so this "
                "run was skipped."
            ),
        )

    outcome = _run_and_deliver(
        run_id=run_id,
        task_id=task_id,
        profile=profile,
        title=title,
        prompt=prompt,
        tz_name=tz_name,
        folder_id=folder_id,
    )

    with SessionLocal() as db:
        auto_paused = store.record_task_outcome(
            db,
            task_id,
            run_id=run_id,
            status=outcome.status,
            max_consecutive_failures=settings.SCHEDULED_TASKS_MAX_CONSECUTIVE_FAILURES,
            completed=is_once and outcome.status == "succeeded",
        )
    if auto_paused is not None:
        _tell_owner_it_paused(profile.email, title)
    return outcome


# ── the run itself ──────────────────────────────────────────────────────────


@attribute_to("scheduled_tasks")
def _run_and_deliver(
    *,
    run_id: str,
    task_id: str,
    profile,
    title: str,
    prompt: str,
    tz_name: str,
    folder_id: str | None,
) -> RunOutcome:
    """Build the agent, run the prompt, save the conversation, deliver.

    Decorated rather than wrapped at the entrypoint: the AI-Cost contextvar
    does not survive a hand-off to a worker thread, and the ADK runner uses
    them, so the feature has to be re-established in the function that does the
    work (the same lesson as the priorities batch, which recorded months of
    spend as "unattributed").
    """
    from rubik_core.agent_runtime.coordinator import HeadlessRun
    from rubik_core.database.db import SessionLocal

    request_id = f"sch-{run_id[:8]}"
    _tz, tz_label = sched.resolve_zone(tz_name)
    local_now = datetime.now(timezone.utc).astimezone(_tz)
    day_label = f"{local_now.strftime('%a %b')} {local_now.day}"

    session_id = str(uuid.uuid4())
    try:
        with SessionLocal() as db:
            from backend.services.chat_orchestrator import build_chat_orchestrator
            from rubik_core.database.models import ChatMessage, ChatSession

            # A fresh conversation per run, in the user's "Scheduled tasks"
            # folder. Fresh (rather than appending to one long thread) because
            # the ADK session id IS this id: a scheduled run must not inherit
            # the reasoning state of last week's run, and the user gets a dated
            # result they can open, read and continue on its own.
            if folder_id is None:
                folder_id = store.ensure_results_folder(db, profile.email)
            chat_session = ChatSession(
                id=session_id,
                # Verbatim from the profile: chat's own lookups are case-exact,
                # so a normalised copy would hide the conversation from its owner.
                user_email=profile.email,
                title=f"{title} — {day_label}"[:200],
                folder_id=folder_id,
                created_at=datetime.utcnow(),
            )
            db.add(chat_session)
            db.add(
                ChatMessage(
                    id=str(uuid.uuid4()),
                    session_id=session_id,
                    role="user",
                    content=prompt,
                    created_at=datetime.utcnow(),
                )
            )
            db.commit()

            orchestrator = build_chat_orchestrator(
                profile,
                db,
                tz_label,
                headless=HeadlessRun(title=title),
            )
            result = asyncio.run(
                asyncio.wait_for(
                    orchestrator.run(
                        request_id=request_id,
                        query=prompt,
                        history=[],
                        chat_session_id=session_id,
                        # No DB session handed in: HITL parking is meaningless
                        # here, and there is no armed tool left to park.
                        db_session=None,
                        mode="scheduled",
                    ),
                    timeout=settings.SCHEDULED_TASKS_RUN_TIMEOUT_SECONDS,
                )
            )
    except asyncio.TimeoutError:
        logger.error(
            "[scheduled-tasks] run=%s timed out after %ss",
            run_id,
            settings.SCHEDULED_TASKS_RUN_TIMEOUT_SECONDS,
        )
        return _fail(
            run_id,
            f"The run did not finish within {settings.SCHEDULED_TASKS_RUN_TIMEOUT_SECONDS} seconds.",
            chat_session_id=session_id,
        )
    except Exception as exc:  # noqa: BLE001 — one bad run must not kill the task
        logger.exception("[scheduled-tasks] run=%s blew up", run_id)
        return _fail(run_id, f"The run failed: {exc}", chat_session_id=session_id)

    # (1) An answer came back — but did it mean anything?
    failure = _answer_failed(result)
    if failure:
        logger.error(
            "[scheduled-tasks] run=%s produced no usable answer: %s", run_id, failure
        )
        return _fail(run_id, failure, chat_session_id=session_id)

    answer = result.answer
    # A run that searched the owner's private Ask Cubie files: its answer is not
    # copied into admin-readable interaction_logs (backend/routers/chat.py).
    private_turn = "search_my_files" in (
        (result.trace.route.metadata or {}).get("tool_calls") or []
    )
    _record_cost(orchestrator, result, profile.email, request_id)

    with SessionLocal() as db:
        from rubik_core.database.models import ChatMessage, InteractionLog

        db.add(
            ChatMessage(
                id=str(uuid.uuid4()),
                session_id=session_id,
                role="assistant",
                content=answer,
                metadata_json={
                    "citations": result.citations,
                    "hubspot_data": result.hubspot_data,
                    "generated_sql": result.generated_sql,
                    "debug": result.trace.model_dump(mode="json"),
                    "scheduled_task_id": task_id,
                    "scheduled_run_id": run_id,
                    **({"private_files": True} if private_turn else {}),
                },
                created_at=datetime.utcnow(),
            )
        )
        db.add(
            InteractionLog(
                id=str(uuid.uuid4()),
                session_id=session_id,
                user_email=profile.email,
                user_input=prompt,
                system_prompt=f"Scheduled task {task_id} | run {run_id}",
                llm_output=(
                    "[Not recorded: this answer drew on files private to the "
                    "person who asked.]"
                    if private_turn
                    else answer
                ),
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                total_tokens=result.total_tokens,
                duration_ms=result.trace.total_duration_ms,
                successful=True,
                mode="scheduled",
            )
        )
        db.commit()
        task = store.get_task(db, task_id)
        channels = dict(
            delivery.deliver(
                task=task,
                run_id=run_id,
                recipient_email=profile.email,
                title=title,
                answer_md=answer,
                chat_session_id=session_id,
                local_when=f"{day_label}, {local_now.strftime('%H:%M')} {tz_label}",
            )
        )
        store.finish_run(
            db,
            run_id,
            status="succeeded",
            chat_session_id=session_id,
            delivery=channels,
        )

    logger.info(
        "[scheduled-tasks] run=%s task=%s user=%s succeeded chars=%d delivery=%s",
        run_id,
        task_id,
        profile.email,
        len(answer),
        channels,
    )
    return RunOutcome(
        run_id=run_id,
        status="succeeded",
        chat_session_id=session_id,
        delivery=channels,
    )


def _answer_failed(result) -> str:
    """Why this answer is not deliverable, or "" when it is.

    The orchestrator turns every exception into a polite sentence and returns
    it as the answer, so "we got a string back" is not success. The trace is
    where the truth is: an ``ADK_EXECUTION`` error step, or a warning naming an
    empty result or the loop backstop.
    """
    trace = getattr(result, "trace", None)
    if trace is None:
        return "The run produced no trace."
    for step in getattr(trace, "steps", []) or []:
        if step.tool_name == "ADK_EXECUTION" and step.status == "error":
            return step.error or "The agent failed to execute."
    warnings = (getattr(trace, "route", None) and trace.route.metadata or {}).get(
        "warnings"
    ) or []
    for warning in warnings:
        lowered = str(warning).lower()
        if "empty_agent_result" in lowered:
            return "The agent produced no answer."
        if "agent_loop_limit" in lowered:
            return "The agent hit its tool-call limit without finishing."
    if not (getattr(result, "answer", "") or "").strip():
        return "The agent produced no answer."
    return ""


def _record_cost(orchestrator, result, user_email: str, request_id: str) -> None:
    """Book the ADK tokens against this feature.

    The coordinator runs through the ADK Runner rather than the shared genai
    client, so the cost wrapper cannot see its tokens — they have to be
    recorded explicitly, against the model that actually ran.
    """
    try:
        from rubik_core.ai_providers.registry import ModelRoute
        from rubik_core.cost.usage import record_usage

        route = getattr(orchestrator, "model_route", None)
        metadata = (
            {"provider": route.provider, "billing_owner": route.billing_owner}
            if isinstance(route, ModelRoute)
            else None
        )
        record_usage(
            operation="generate",
            model=route.model
            if isinstance(route, ModelRoute)
            else getattr(orchestrator, "model_id", settings.VERTEX_LLM_MODEL_FLASH),
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            total_tokens=result.total_tokens,
            thinking_tokens=getattr(result, "thinking_tokens", 0),
            cached_tokens=getattr(result, "cached_tokens", 0),
            feature="scheduled_tasks",
            user_email=user_email,
            request_id=request_id,
            metadata=metadata,
        )
    except Exception:  # noqa: BLE001 — accounting must never fail a run
        logger.debug("[scheduled-tasks] cost record failed", exc_info=True)


# ── identity + gate ─────────────────────────────────────────────────────────


def _resolve_profile(user_email: str):
    """The owner's ``UserProfile``, or ``None`` when they are gone.

    Offboarding deletes the ``users`` row, so ``None`` here is the signal that
    the task belongs to somebody who has left.
    """
    try:
        from rubik_core.auth.registry import UserRegistry

        return UserRegistry().get_user(user_email)
    except Exception:  # noqa: BLE001
        logger.exception("[scheduled-tasks] profile lookup failed for %s", user_email)
        return None


def _feature_allowed(profile) -> bool:
    """Whether this user may still reach scheduled tasks.

    Kill switch first (it means "not in this environment"), then the roster.
    """
    if not settings.SCHEDULED_TASKS_ENABLED:
        return False
    try:
        from rubik_core.features import feature_allows

        return feature_allows("scheduled_tasks", profile)
    except Exception:  # noqa: BLE001
        logger.exception(
            "[scheduled-tasks] gate check failed for %s", getattr(profile, "email", "?")
        )
        return False


# ── outcome helpers ─────────────────────────────────────────────────────────


def _fail(run_id: str, error: str, *, chat_session_id: str | None = None) -> RunOutcome:
    """Record the failure and clear away the half-written conversation.

    The session is opened with the prompt already in it, before the agent runs,
    so on failure it holds a question and no answer. Left behind, it would sit
    in the user's "Scheduled tasks" folder looking like a chat that got
    ignored. The run row carries the error instead, which is what the Settings
    tab shows.
    """
    from rubik_core.database.db import SessionLocal

    with SessionLocal() as db:
        store.finish_run(db, run_id, status="failed", error=error)
        if chat_session_id:
            try:
                from rubik_core.database.models import ChatSession

                barren = db.get(ChatSession, chat_session_id)
                # Only ever an unanswered one — never delete a session that
                # actually carries a result.
                if barren is not None and not any(
                    m.role == "assistant" for m in barren.messages
                ):
                    db.delete(barren)
                    db.commit()
            except Exception:  # noqa: BLE001 — tidying must not mask the failure
                db.rollback()
                logger.debug(
                    "[scheduled-tasks] could not clear the empty session for run=%s",
                    run_id,
                    exc_info=True,
                )
    return RunOutcome(run_id=run_id, status="failed", reason=error)


def _skip(
    run_id: str,
    task_id: str,
    *,
    reason: str,
    message: str,
    pause: bool = False,
    pause_reason: str = "",
) -> RunOutcome:
    """Record a run that could not legitimately proceed.

    A skip counts toward the failure streak — a task nobody can run is not
    healthy — but only an unambiguous cause (the owner is gone) pauses the task
    outright. A denied gate might be a transient read failure, and pausing on
    one of those would silently switch off a working schedule.
    """
    from rubik_core.database.db import SessionLocal

    logger.warning("[scheduled-tasks] run=%s skipped (%s): %s", run_id, reason, message)
    with SessionLocal() as db:
        store.finish_run(db, run_id, status="skipped", error=message)
        task = store.get_task(db, task_id)
        if pause and task is not None and task.status == "active":
            store.set_status(db, task, "paused", reason=pause_reason or message)
            db.commit()
        store.record_task_outcome(
            db,
            task_id,
            run_id=run_id,
            status="skipped",
            max_consecutive_failures=settings.SCHEDULED_TASKS_MAX_CONSECUTIVE_FAILURES,
        )
    return RunOutcome(run_id=run_id, status="skipped", reason=reason)


def _tell_owner_it_paused(user_email: str, title: str) -> None:
    """Let the owner know a task stopped itself, so it is not silently dead."""
    delivery.notify(
        recipient_email=user_email,
        title=f"Scheduled task paused: {title}",
        chat_session_id=None,
        body=(
            f"“{title}” failed several times in a row, so {PRODUCT_NAME} paused it. "
            "Open Settings → Scheduled tasks to see why and resume it."
        ),
        severity="warn",
    )

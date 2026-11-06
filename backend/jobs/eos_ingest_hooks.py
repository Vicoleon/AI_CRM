"""Fire-and-forget EOS re-ingest hooks — debounced dispatch to cubie-worker.

Called by the routers that mutate EOS data (issues, VTO, processes, L10 end,
Slack /ids). These used to schedule the re-ingest as FastAPI BackgroundTasks
(``rubik_core/ingestion/eos/hooks.py``), which Cloud Run CPU-throttles the
moment the response returns — the #9 bug class — and the weekly full-sweep
backstop cron is NOT currently scheduled, so a starved hook meant the edit
simply never reached RAG. Work now dispatches to the ``cubie-worker`` Cloud
Run Job via ``backend/jobs/launcher.py``.

The wrinkle vs. the other launcher call sites: these hooks fire on EVERY edit
(frequent during a live L10) and each run re-ingests ALL docs of that kind,
so a job-per-edit would spawn a pile of redundant executions. The coalesced
kinds (vto/issues/processes) therefore go through the ``eos_ingest_state``
watermark+claim machine (``rubik_core/ingestion/eos/debounce.py``): every
edit bumps the request watermark, only the claim winner launches an
execution, and the worker keeps re-ingesting until it has covered the newest
request. L10 session ingest stays a direct per-session dispatch — it fires
once, at meeting end.

Contract preserved from the old hooks: enqueueing must never break the
user's mutation (log-and-swallow), and call sites still just pass
``BackgroundTasks`` (used only for the launcher's inline fallback).
"""

from __future__ import annotations

import logging

from fastapi import BackgroundTasks

from backend.jobs.launcher import submit_background_work
from rubik_core.ingestion.eos import debounce
from rubik_core.tenancy.context import current_workspace, use_workspace

logger = logging.getLogger(__name__)

EOS_REINGEST_MODULE = "backend.jobs.eos_reingest_job"


def _inline_worker(kind: str):
    """Fallback executor (local dev / job-launch failure): the same debounced
    worker loop, minus the quiet-wait (a BackgroundTask must not sleep for
    minutes) and minus execution chaining."""

    def _task() -> None:
        try:
            debounce.run_debounced_reingest(kind, wait_for_quiet=False, relaunch=None)
        except Exception:  # noqa: BLE001
            logger.exception("[eos-ingest][hook:%s] inline fallback failed", kind)

    return _task


def _request_and_dispatch(kind: str, background_tasks: BackgroundTasks) -> None:
    try:
        if not debounce.request_reingest(kind):
            # A live worker's end-of-run watermark check covers this edit.
            logger.info("[eos-ingest][hook:%s] coalesced into in-flight run", kind)
            return
        # FastAPI runs fallback BackgroundTasks after the route's workspace
        # scope has exited. Capture the verified workspace now and restore it
        # inside the fallback so an inline reindex cannot use another tenant.
        slug = current_workspace()
        inline = _inline_worker(kind)

        def fallback_in_workspace() -> None:
            with use_workspace(slug):
                inline()

        executor, execution_name = submit_background_work(
            background_tasks,
            module=EOS_REINGEST_MODULE,
            args=["--kind", kind],
            fallback=fallback_in_workspace,
            label=f"eos-reingest-{kind}",
        )
        debounce.record_dispatch(kind, executor, execution_name)
    except Exception:  # noqa: BLE001 — the mutation must never fail on ingest glue
        logger.exception("[eos-ingest][hook:%s] dispatch failed", kind)
        # Release the claim (if we took one) so the next edit retries
        # immediately instead of waiting out the stale window.
        debounce.force_idle(kind, "hook dispatch failed — next edit re-dispatches")


def enqueue_vto_ingest(background_tasks: BackgroundTasks) -> None:
    """Re-ingests every section of the active VTO version. Called after any
    VTO section create/update — rare but high-impact for Cubie's awareness
    of company direction."""
    _request_and_dispatch(debounce.KIND_VTO, background_tasks)


def enqueue_issue_ingest(background_tasks: BackgroundTasks) -> None:
    """Re-ingests every Issue. Called after an Issue is created or its status
    changes to solved — the burstiest hook (IDS during a live L10), which is
    exactly what the debounce coalesces. Could be narrowed to "this one
    issue" later if volume justifies it."""
    _request_and_dispatch(debounce.KIND_ISSUES, background_tasks)


def enqueue_process_ingest(background_tasks: BackgroundTasks) -> None:
    """Re-ingests every Core Process. Called after a Process is created."""
    _request_and_dispatch(debounce.KIND_PROCESSES, background_tasks)


def enqueue_l10_session_ingest(
    background_tasks: BackgroundTasks, session_id: str
) -> None:
    """Ingests one L10 session's notes. Called after ``POST /sessions/{id}/end``
    when the manager closes the meeting. Per-session work with no burst
    pattern, so it dispatches straight to the worker — no debounce row. The
    the session we just closed. The inline fallback also retains the request's
    verified workspace after the response scope exits."""

    try:
        slug = current_workspace()

        def _fallback() -> None:
            from rubik_core.database.db import SessionLocal
            from rubik_core.ingestion.eos.pipeline import ingest_l10_session

            with use_workspace(slug):
                session = SessionLocal()
                try:
                    result = ingest_l10_session(session, session_id=session_id)
                    logger.info(
                        "[eos-ingest][hook:l10:%s] %s", session_id, result.summary()
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("[eos-ingest][hook:l10:%s] failed", session_id)
                finally:
                    session.close()

        submit_background_work(
            background_tasks,
            module=EOS_REINGEST_MODULE,
            args=["--kind", "l10", "--session-id", session_id],
            fallback=_fallback,
            label=f"eos-reingest-l10-{session_id}",
        )
    except Exception:  # noqa: BLE001 — the mutation must never fail on ingest glue
        logger.exception("[eos-ingest][hook:l10:%s] dispatch failed", session_id)

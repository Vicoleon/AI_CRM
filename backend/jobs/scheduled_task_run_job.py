"""Cloud Run Job entrypoint for ONE scheduled-task run.

Launched per due task by the tick in ``backend/routers/agents.py`` as:

    python -m backend.jobs.scheduled_task_run_job --run-id <scheduled_task_runs.id>

Runs on a CPU-allocated Job task because it drives the full chat agent (tool
fan-out across Gmail, Calendar, HubSpot, transcripts and RAG) — a FastAPI
BackgroundTask on the service is CPU-throttled the instant the response
returns and would be starved mid-run.

**This entrypoint always exits 0 once the run row is claimed.** A non-zero exit
makes Cloud Run retry the task, and a retry of a delivered run would DM the
user the same digest twice. Failures are recorded on the run row instead,
where the dispatcher's failure counter and the UI can both see them; the claim
itself (``store.claim_run_running``) is what makes a retry harmless.
"""

from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scheduled_task_run_job")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="scheduled_task_run_job")
    p.add_argument(
        "--run-id",
        required=True,
        help="scheduled_task_runs.id to execute (created by the tick).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    from backend.jobs.bootstrap import bootstrap_job_workspace

    if not bootstrap_job_workspace():
        return 2

    args = _build_parser().parse_args(argv)

    # Deferred: pulls the app graph (DB, settings, Vertex, ADK). Keeps --help
    # fast and surfaces an import failure inside the task, not at parse time.
    from rubik_core.scheduled_tasks.runner import execute_run

    logger.info("scheduled_task_run_job start run=%s", args.run_id)
    try:
        outcome = execute_run(args.run_id)
        logger.info(
            "scheduled_task_run_job done run=%s status=%s reason=%s session=%s",
            outcome.run_id,
            outcome.status,
            outcome.reason,
            outcome.chat_session_id,
        )
    except Exception:  # noqa: BLE001 — see the module docstring on exit codes
        logger.exception(
            "scheduled_task_run_job: unhandled failure for run=%s. Exiting 0 so "
            "Cloud Run does not retry and re-deliver.",
            args.run_id,
        )

    # A separate process, so drain the AI-Cost ledger explicitly rather than
    # relying on the atexit handler a terminated task may skip.
    try:
        from rubik_core.cost.usage import flush_usage

        flush_usage()
    except Exception:  # noqa: BLE001 — accounting must never fail the job
        logger.debug("scheduled_task_run_job: cost flush failed", exc_info=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())

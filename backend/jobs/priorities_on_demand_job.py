"""Cloud Run Job entrypoint for ONE on-demand AI-Priorities run (UC-41 → #9).

Launched per-request by ``backend/routers/home.py`` (``POST /priorities`` on a
cache miss / force-refresh, and ``POST /priorities/more``) as a single-task
execution of the existing ``cubie-priorities-batch`` job:

    python -m backend.jobs.priorities_on_demand_job --run-id R --user-email E \
        [--focus ...] [--max-priorities N] [--channels gmail,calendar]

Runs on a CPU-allocated Job task so the 3–5 min multi-agent pipeline actually
finishes — a FastAPI BackgroundTask would run on the Service, which Cloud Run
CPU-throttles the instant the 202 returns, so the batch would be starved.

Reuses the service's ``_run_and_persist_priorities`` (UI-prefs merge, the
empty-bundle delivery gate, HomeCard persist) so a Job run and an inline
fallback run produce identical cards, then reports the outcome on the
``priorities_runs`` row — which is all the poll endpoint reads.
"""

from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("priorities_on_demand_job")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="priorities_on_demand_job")
    p.add_argument("--run-id", required=True)
    p.add_argument("--user-email", required=True)
    p.add_argument("--focus", default=None)
    p.add_argument("--max-priorities", type=int, default=None)
    p.add_argument("--channels", default=None, help="comma-separated channel kinds")
    return p


def main(argv: list[str] | None = None) -> int:
    from backend.jobs.bootstrap import bootstrap_job_workspace

    if not bootstrap_job_workspace():
        return 2

    args = _build_parser().parse_args(argv)

    # Deferred imports: pulls the app graph (DB, settings). Keeps --help fast
    # and lets import failures surface inside the task, not at parse time.
    from backend.routers.home import PrioritiesRequestBody, _run_and_persist_priorities
    from rubik_core.priorities.run_state import mark_finished
    from rubik_core.telemetry import set_admin_context

    channels = [c for c in (args.channels or "").split(",") if c] or None
    body = PrioritiesRequestBody(
        focus=args.focus,
        max_priorities=args.max_priorities,
        channels=channels,
        force_refresh=True,
    )

    logger.info(
        "priorities_on_demand_job start run_id=%s user=%s max=%s channels=%s",
        args.run_id,
        args.user_email,
        args.max_priorities,
        args.channels,
    )
    set_admin_context(True)
    try:
        _run_and_persist_priorities(args.user_email, body)
        mark_finished(args.run_id, succeeded=True)
        logger.info("priorities_on_demand_job done run_id=%s", args.run_id)
    except Exception as exc:  # noqa: BLE001 — the outcome lands on the run row
        logger.exception(
            "priorities_on_demand_job failed run_id=%s user=%s",
            args.run_id,
            args.user_email,
        )
        mark_finished(
            args.run_id, succeeded=False, error=f"{type(exc).__name__}: {exc}"
        )
    finally:
        # Separate process: flush the AI-Cost ledger before exit rather than
        # relying on the atexit handler a terminated job may skip.
        try:
            from rubik_core.cost.usage import flush_usage

            flush_usage()
        except Exception:  # noqa: BLE001 — cost accounting must never fail the job
            logger.debug("priorities_on_demand_job: cost flush failed", exc_info=True)

    # Always exit 0: the job is deployed with --max-retries=1, and a retry
    # would re-run a 3–5 min LLM pipeline whose failure is already recorded on
    # the priorities_runs row (mirrors batch_runner's exit-code contract).
    return 0


if __name__ == "__main__":
    sys.exit(main())

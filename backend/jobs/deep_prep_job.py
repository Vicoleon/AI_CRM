"""Cloud Run Job entrypoint for ONE Deep Prep run.

Invoked per-request by ``_launch_deep_prep_job_execution`` (backend/routers/
home.py) as:

    python -m backend.jobs.deep_prep_job --run-id ... --user-email ... \
        --user-department ... --user-roles a,b --event-id ... \
        [--focus-on ...] [--force-sales true|false]

Runs on a CPU-allocated Job task so the multi-call pipeline (context → CRM →
council → PDF → email) actually finishes — unlike a FastAPI BackgroundTask,
which the CPU-throttled service starves after the HTTP response.

Per-run parameters arrive as container arg overrides; this reads them with
argparse, exactly like ``rubik_core.priorities.batch_runner``.
"""

from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("deep_prep_job")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="deep_prep_job")
    p.add_argument("--run-id", required=True)
    p.add_argument("--user-email", required=True)
    p.add_argument("--user-department", default="")
    p.add_argument("--user-roles", default="", help="comma-separated role names")
    p.add_argument("--event-id", required=True)
    p.add_argument("--focus-on", default=None)
    p.add_argument("--force-sales", default=None, choices=["true", "false"])
    return p


def main(argv: list[str] | None = None) -> int:
    from backend.jobs.bootstrap import bootstrap_job_workspace

    if not bootstrap_job_workspace():
        return 2

    args = _build_parser().parse_args(argv)
    roles = [r for r in (args.user_roles or "").split(",") if r]
    force_sales = None if args.force_sales is None else args.force_sales == "true"

    # Deferred import: pulls the app graph (DB, settings). Keeps --help fast and
    # lets the runner import fail loudly inside the task rather than at parse.
    from backend.routers.home import _run_deep_prep_sync

    logger.info(
        "deep_prep_job start run_id=%s user=%s event=%s",
        args.run_id,
        args.user_email,
        args.event_id,
    )
    _run_deep_prep_sync(
        args.run_id,
        args.user_email,
        args.user_department,
        roles,
        args.event_id,
        args.focus_on,
        force_sales,
    )
    # _run_deep_prep is best-effort (never raises; it records failed status), so
    # the task exits 0 — we don't want Cloud Run's retry re-running a prep that
    # already emailed. Delivery status lives on the meeting_prep_runs row.
    logger.info("deep_prep_job done run_id=%s", args.run_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())

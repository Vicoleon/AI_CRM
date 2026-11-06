"""Cloud Run Job entrypoint for ONE background on-screen Meeting Prep build.

Invoked per-request by ``_launch_meeting_prep_job_execution`` (backend/routers/
home.py) as an execution of the existing ``cubie-deep-prep`` Cloud Run Job:

    python -m backend.jobs.meeting_prep_job --run-id=... --user-email=... \
        --event-id=... [--focus-on=...] [--product-to-emphasize=...] \
        --keep-edits=true|false

Every value comes as ``--flag=value`` (the launcher always sends that form): a
value that begins with a dash would otherwise be read by argparse as a flag
and end the task with exit 2 before it builds.

The pending ``meeting_prep_runs`` row already exists (written by the request
that answered 202); this task builds the brief and finalizes THAT row as the
meeting's next version, or marks it failed, then notifies the reader
(``rubik_core.meeting_prep.background.run_build``). It runs on a CPU-allocated
Job task because the Service throttles CPU once the 202 is sent — a 60-120s
multi-source build fired from the Service would be starved.
"""

from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("meeting_prep_job")

#: ``run_build`` outcomes that left the row unsettled: the task exits non-zero.
UNSETTLED_OUTCOMES = ("unrecorded", "missing")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="meeting_prep_job")
    p.add_argument("--run-id", required=True)
    p.add_argument("--user-email", required=True)
    p.add_argument("--event-id", required=True)
    p.add_argument("--focus-on", default=None)
    p.add_argument("--product-to-emphasize", default=None)
    p.add_argument("--keep-edits", default="true", choices=["true", "false"])
    return p


def main(argv: list[str] | None = None) -> int:
    from backend.jobs.bootstrap import bootstrap_job_workspace

    if not bootstrap_job_workspace():
        return 2

    args = _build_parser().parse_args(argv)

    # Deferred import: pulls the app graph (DB, settings, Vertex). Keeps --help
    # fast and surfaces an import failure inside the task, not at parse time.
    from rubik_core.meeting_prep import background

    logger.info(
        "meeting_prep_job start run_id=%s user=%s event=%s",
        args.run_id,
        args.user_email,
        args.event_id,
    )
    # run_build_sync never raises. A RECORDED outcome (ready, failed) leaves
    # the row settled and the reader notified, and "skipped" (the run does not
    # match these arguments) is not something a retry fixes, so the task
    # exits 0. "unrecorded" (the database failed, so the row is still
    # queued/running) and "missing" (no such run) exit 1: Cloud Run retries
    # the task, which builds the row again, and the "Job execution failed"
    # alert fires instead of the row sitting silently in flight.
    status = background.run_build_sync(
        run_id=args.run_id,
        user_email=args.user_email,
        event_id=args.event_id,
        focus_on=args.focus_on or None,
        keep_edits=args.keep_edits == "true",
        product_to_emphasize=args.product_to_emphasize or None,
    )
    logger.info("meeting_prep_job done run_id=%s status=%s", args.run_id, status)

    # A separate process: flush the AI-Cost ledger explicitly so the build's
    # buffered token/cost events land before the process exits, rather than
    # relying solely on the atexit handler (which a terminated job may skip).
    try:
        from rubik_core.cost.usage import flush_usage

        flush_usage()
    except Exception:  # noqa: BLE001 - cost accounting must never fail the job
        logger.debug("meeting_prep_job: cost flush failed", exc_info=True)

    return 1 if status in UNSETTLED_OUTCOMES else 0


if __name__ == "__main__":
    sys.exit(main())

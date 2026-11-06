"""Cloud Run Job entrypoint for ONE meeting-routine fire.

Launched per queued fire by the scheduled-tasks tick
(``rubik_core.meeting_skills.routines.dispatch_queued_fires``) as:

    python -m backend.jobs.meeting_routine_fire_job --fire-id <meeting_routine_fires.id>

**Always exits 0 once the fire is claimed**, for the same reason as
``scheduled_task_run_job``: a non-zero exit makes Cloud Run retry, and a retry
of a delivered fire would DM the same write-up twice. The claim
(``queued → running``) is what makes a retry harmless; failures are recorded on
the fire row.
"""

from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("meeting_routine_fire_job")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="meeting_routine_fire_job")
    p.add_argument(
        "--fire-id",
        required=True,
        help="meeting_routine_fires.id to execute (queued by a transcript).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    from backend.jobs.bootstrap import bootstrap_job_workspace

    if not bootstrap_job_workspace():
        return 2

    args = _build_parser().parse_args(argv)

    from rubik_core.meeting_skills.routines import execute_fire

    logger.info("meeting_routine_fire_job start fire=%s", args.fire_id)
    try:
        outcome = execute_fire(args.fire_id)
        logger.info(
            "meeting_routine_fire_job done fire=%s status=%s reason=%s session=%s",
            outcome.fire_id,
            outcome.status,
            outcome.reason,
            outcome.chat_session_id,
        )
    except Exception:  # noqa: BLE001 — see the module docstring on exit codes
        logger.exception(
            "meeting_routine_fire_job: unhandled failure for fire=%s. Exiting 0 so "
            "Cloud Run does not retry and re-deliver.",
            args.fire_id,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

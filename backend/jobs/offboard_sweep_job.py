"""Cloud Run Job entrypoint for ONE offboard exit sweep.

Dispatched by ``POST /api/offboarding/offboard`` via
``backend.jobs.launcher.submit_background_work`` as:

    python -m backend.jobs.offboard_sweep_job --record-id <uuid>

Runs on the cubie-worker Job (CPU-allocated to completion) because the sweep
does real work — EOS + HubSpot inventory, a last Gmail/Calendar/Drive pass
over the departed account, one LLM synthesis call — none of which survives the
CPU-throttled service after the HTTP response returns.
"""

from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("offboard_sweep_job")


def main(argv: list[str] | None = None) -> int:
    from backend.jobs.bootstrap import bootstrap_job_workspace

    if not bootstrap_job_workspace():
        return 2

    parser = argparse.ArgumentParser(prog="offboard_sweep_job")
    parser.add_argument("--record-id", required=True)
    args = parser.parse_args(argv)

    # Deferred import — pulls the app graph (DB, settings); fail loudly inside
    # the task, not at argparse time.
    from rubik_core.offboarding.sweep import run_offboard_sweep

    logger.info("offboard_sweep_job start record_id=%s", args.record_id)
    run_offboard_sweep(args.record_id)
    logger.info("offboard_sweep_job done record_id=%s", args.record_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())

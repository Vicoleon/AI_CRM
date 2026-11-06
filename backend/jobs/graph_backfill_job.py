"""Cloud Run Job entrypoint for the one-shot relationship-graph backfill.

Invoked on the ``cubie-worker`` job as:

    python -m backend.jobs.graph_backfill_job [--limit N] [--batch-size N]

The incremental writers only fire on new activity, so the graph starts empty and
would stay thin until every existing transcript happened to be re-synced. This
sweeps what is already on record. It runs as a Job rather than in-request
because Cloud Run throttles CPU after the HTTP response, which is how a
fire-and-forget sweep silently dies half-finished.

Idempotent: writes go through the store's upsert path, so re-running
re-observes edges instead of duplicating them. Safe to run repeatedly while the
graph is being populated.
"""

from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("graph_backfill_job")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="graph_backfill_job")
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most this many transcripts (default: all).",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Transcripts per transaction.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    from backend.jobs.bootstrap import bootstrap_job_workspace

    if not bootstrap_job_workspace():
        return 2

    args = _build_parser().parse_args(argv)

    # Deferred import: pulls the app graph (DB, settings). Keeps --help fast and
    # surfaces an import failure inside the task rather than at parse time.
    from rubik_core.config import settings
    from rubik_core.graph.backfill import backfill_attendance

    if not settings.CUBIE_GRAPH_WRITES_ENABLED:
        # The kill switch governs the writers this sweep shares; honouring it
        # here means one flag turns the whole feature off, rather than the
        # backfill quietly repopulating what the flag was meant to stop.
        logger.warning(
            "graph_backfill_job: CUBIE_GRAPH_WRITES_ENABLED is off; nothing to do"
        )
        return 0

    logger.info(
        "graph_backfill_job start limit=%s batch_size=%s", args.limit, args.batch_size
    )
    stats = backfill_attendance(limit=args.limit, batch_size=args.batch_size)
    logger.info("graph_backfill_job done: %s", stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())

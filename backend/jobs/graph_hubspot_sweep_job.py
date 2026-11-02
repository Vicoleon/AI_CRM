"""Cloud Run Job entrypoint for the graph's HubSpot (CRM) edge sweep.

Invoked on the ``cubie-worker`` job as:

    python -m backend.jobs.graph_hubspot_sweep_job [--limit N] [--domains a.com,b.com]

Writes ``has_contact`` / ``has_deal`` / ``owned_by`` / ``member_of`` edges for
the companies we actually meet. They were expected to accumulate on their own
from X-Ray runs, which run too rarely for that, so they get written on purpose
instead.

Deterministic — no LLM calls, so no AI-cost attribution needed. Idempotent, so
a repeat or interrupted run is safe to re-run. Runs as a Job rather than
in-request because it makes many sequential HubSpot calls and Cloud Run
throttles CPU after the HTTP response.
"""

from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("graph_hubspot_sweep_job")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="graph_hubspot_sweep_job")
    p.add_argument(
        "--limit",
        type=int,
        default=250,
        help="Maximum companies to sweep (HubSpot is rate-limited).",
    )
    p.add_argument(
        "--domains",
        default="",
        help=(
            "Comma-separated domains to sweep instead of every external company "
            "already in the graph. Useful for a rehearsal or a single fix-up."
        ),
    )
    return p


def main(argv: list[str] | None = None) -> int:
    from backend.jobs.bootstrap import bootstrap_job_workspace

    if not bootstrap_job_workspace():
        return 2

    args = _build_parser().parse_args(argv)

    # Deferred import: pulls the app graph (DB, settings, HubSpot client).
    from rubik_core.graph.hubspot_sweep import sweep_hubspot_edges

    domains = [d.strip() for d in (args.domains or "").split(",") if d.strip()]
    logger.info(
        "graph_hubspot_sweep_job start limit=%s domains=%s",
        args.limit,
        domains or "(all external companies in the graph)",
    )
    stats = sweep_hubspot_edges(domains=domains or None, limit=args.limit)
    logger.info("graph_hubspot_sweep_job done: %s", stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())

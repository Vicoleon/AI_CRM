"""Cloud Run Job entrypoint for EOS RAG re-ingestion (#9 follow-up).

Launched by ``backend/jobs/eos_ingest_hooks.py`` as one execution of the
``cubie-worker`` job:

    python -m backend.jobs.eos_reingest_job --kind {vto|issues|processes}
    python -m backend.jobs.eos_reingest_job --kind l10 --session-id S

The coalesced kinds (vto/issues/processes) run the debounced worker loop from
``rubik_core/ingestion/eos/debounce.py``: wait for the edit burst to go quiet,
re-ingest the full kind, and repeat while newer edits keep arriving — chaining
a fresh execution of this same module if the pass budget runs out. ``l10`` is
per-session (fired once at meeting end), so it ingests that one session
directly with no debounce state.

Also handy as a manual full re-ingest for one kind:

    gcloud run jobs execute cubie-worker --project="$GCP_PROJECT_ID" \
        --region=us-central1 \
        --args="-m,backend.jobs.eos_reingest_job,--kind,issues"

Always exits 0 (matching priorities_on_demand_job): ingest failures are
logged + recorded on the ``eos_ingest_state`` row, and re-ingest self-heals
on the next edit — a ``--max-retries`` re-run of a full-corpus sweep buys
little.
"""

from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("eos_reingest_job")

KIND_L10 = "l10"


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="eos_reingest_job")
    p.add_argument(
        "--kind",
        required=True,
        choices=["vto", "issues", "processes", KIND_L10],
    )
    p.add_argument("--session-id", default=None, help="required when --kind l10")
    return p


def _run_l10(session_id: str) -> None:
    from rubik_core.database.db import SessionLocal
    from rubik_core.ingestion.eos.pipeline import ingest_l10_session

    session = SessionLocal()
    try:
        result = ingest_l10_session(session, session_id=session_id)
        logger.info("[eos-ingest][job:l10:%s] %s", session_id, result.summary())
    except Exception:  # noqa: BLE001 — log-and-swallow, task still exits 0
        logger.exception("[eos-ingest][job:l10:%s] failed", session_id)
    finally:
        session.close()


def main(argv: list[str] | None = None) -> int:
    from backend.jobs.bootstrap import bootstrap_job_workspace

    if not bootstrap_job_workspace():
        return 2

    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.kind == KIND_L10:
        if not args.session_id:
            parser.error("--session-id is required with --kind l10")
        _run_l10(args.session_id)
        return 0

    # Deferred import: pulls the app graph (DB, settings).
    from rubik_core.ingestion.eos import debounce

    def _relaunch() -> None:
        # Chain a fresh execution when the pass budget runs out with edits
        # still arriving — same module, same kind, claim carried over.
        from backend.jobs.launcher import launch_module_execution

        launch_module_execution("backend.jobs.eos_reingest_job", ["--kind", args.kind])

    outcome = debounce.run_debounced_reingest(
        args.kind, wait_for_quiet=True, relaunch=_relaunch
    )
    logger.info("eos_reingest_job done kind=%s outcome=%s", args.kind, outcome)
    return 0


if __name__ == "__main__":
    sys.exit(main())

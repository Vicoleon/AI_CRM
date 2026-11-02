"""Cloud Run Job entrypoint for one Fathom transcript pull + ingest (#9).

Launched by ``backend/routers/fathom.py`` (the ``meeting.completed`` webhook
and the manual ``POST /fathom/ingest``) as one execution of the
``cubie-worker`` job:

    CUBIE_WORKSPACE=<slug> python -m backend.jobs.fathom_sync_job --user-email E

Pulling the account's recent Fathom meetings, extracting transcripts, and
importing them into the meetings corpus takes minutes — far past the point
where Cloud Run throttles the Service's CPU after the webhook's 200 returns,
so the old FastAPI BackgroundTask could be silently starved mid-batch.
"""

from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("fathom_sync_job")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fathom_sync_job")
    # Fathom's list_meetings is ACCOUNT-WIDE and each meeting carries its own
    # recorded_by, so a scheduled sweep needs no user at all — this is only the
    # fallback owner for a meeting Fathom reports without one.
    p.add_argument("--user-email", default=None)
    p.add_argument("--limit", type=int, default=20)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    # A Cloud Run execution has no request middleware. Select and validate its
    # workspace before importing code that can construct tenant DB or clients.
    from rubik_core.tenancy.context import bootstrap_process_workspace

    try:
        bootstrap_process_workspace()
    except (ValueError, RuntimeError) as exc:
        logger.error("Fathom sync workspace unavailable: %s", exc)
        return 2

    # Deferred imports: pulls the app graph (DB, settings, Vertex).
    from backend.routers.fathom import process_fathom_transcripts_background
    from backend.routers.ingest import get_processor
    from rubik_core.tools._workspace_auth import workspace_dwd_subject

    fallback_owner = args.user_email or workspace_dwd_subject()
    if not fallback_owner:
        raise RuntimeError("Workspace DWD admin subject is not configured")
    logger.info(
        "fathom_sync_job start fallback_owner=%s limit=%s", fallback_owner, args.limit
    )
    report = process_fathom_transcripts_background(
        fallback_owner, get_processor(), limit=args.limit
    )
    logger.info(
        "fathom_sync_job done status=%s ingested=%d skipped=%d failed=%d",
        report.status,
        report.ingested,
        report.skipped,
        report.failed,
    )
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())

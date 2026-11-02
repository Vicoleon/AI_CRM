"""Cloud Run Job entrypoint for ONE UC-45 Deep Research run.

Invoked per-request by ``_launch_xray_deep_job_execution`` (backend/routers/
xray.py) as:

    python -m backend.jobs.xray_deep_job --snapshot-id ... --company-id ... \
        --user-email ... [--focus ...] [--company-name ...] [--company-domain ...]

Runs on a CPU-allocated Job task so the multi-agent pipeline (channel fan-out →
analyst → critic) reliably finishes — unlike a FastAPI BackgroundTask / detached
thread, which the CPU-throttled service starves after the HTTP response. This
is the same architecture as Deep Meeting Prep's ``cubie-deep-prep`` job.

The pending ``xray_snapshots`` row already exists (created by POST /deep); this
task flips it to running → ready/failed via ``service.run_deep_job``.
"""

from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("xray_deep_job")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="xray_deep_job")
    p.add_argument("--snapshot-id", required=True)
    p.add_argument("--company-id", required=True)
    p.add_argument("--user-email", required=True)
    p.add_argument("--focus", default="")
    p.add_argument("--company-name", default="")
    p.add_argument("--company-domain", default="")
    return p


def main(argv: list[str] | None = None) -> int:
    from backend.jobs.bootstrap import bootstrap_job_workspace

    if not bootstrap_job_workspace():
        return 2

    args = _build_parser().parse_args(argv)

    # Deferred import: pulls the app graph (DB, settings, Vertex). Keeps --help
    # fast and surfaces an import failure inside the task, not at parse time.
    from rubik_core.xray import service

    # Rebuild WGLL from the requesting user's own ratings (cheap, email-only);
    # personalization needs a full UserProfile, so the job path skips it — the
    # brief is still strong without it (best-effort, matching the service path).
    wgll = ""
    try:
        from rubik_core.database.db import SessionLocal
        from rubik_core.feedback.wgll import build_wgll_context

        with SessionLocal() as db:
            wgll = (
                build_wgll_context(db, surface="xray", user_email=args.user_email) or ""
            )
    except Exception:  # noqa: BLE001 - WGLL is best-effort
        logger.debug("xray_deep_job: WGLL unavailable", exc_info=True)

    logger.info(
        "xray_deep_job start snapshot=%s company=%s user=%s",
        args.snapshot_id,
        args.company_id,
        args.user_email,
    )
    # run_deep_job is best-effort (records failed status, never raises), so the
    # task exits 0 — we don't want Cloud Run's retry re-running a finished brief.
    service.run_deep_job(
        args.snapshot_id,
        args.company_id,
        requesting_user=args.user_email,
        focus=args.focus,
        personalization="",
        wgll=wgll,
        company_name=args.company_name,
        company_domain=args.company_domain,
    )
    logger.info("xray_deep_job done snapshot=%s", args.snapshot_id)

    # This is a separate process (Cloud Run Job): explicitly flush the AI-Cost
    # ledger so the deep run's buffered token/cost events land before the
    # process exits, rather than relying solely on the atexit handler (which a
    # terminated job may skip).
    try:
        from rubik_core.cost.usage import flush_usage

        flush_usage()
    except Exception:  # noqa: BLE001 - cost accounting must never fail the job
        logger.debug("xray_deep_job: cost flush failed", exc_info=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())

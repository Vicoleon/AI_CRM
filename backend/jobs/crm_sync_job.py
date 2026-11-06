"""Cloud Run Job entrypoint for a UC-46 CRM Sync scan.

Two modes:

    python -m backend.jobs.crm_sync_job --run-id ... --user-email ... [--since-days 7]
    python -m backend.jobs.crm_sync_job --all-users [--since-hours 25]

The first is the on-demand scan launched by ``POST /api/crm-sync/scan``; the
second is the nightly sweep, which walks eligible users sequentially inside one
execution rather than fanning out — HubSpot rate limits are the binding
constraint here, not wall clock.

Runs on a CPU-allocated Job task because the scan is minutes of HubSpot lookups
and LLM calls: a FastAPI BackgroundTask would be starved the moment the HTTP
response returned.

The scan only ever writes suggestion rows. Nothing here touches HubSpot — that
happens later, per item, when a human approves one.
"""

from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("crm_sync_job")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="crm_sync_job")
    p.add_argument("--run-id")
    p.add_argument("--user-email")
    p.add_argument("--since-days", type=int, default=None)
    p.add_argument("--all-users", action="store_true")
    p.add_argument("--since-hours", type=int, default=25)
    return p


def _eligible_emails() -> list[str]:
    """The people the nightly sweep scans for: everyone CRM Sync admits.

    The same gate as the CRM Sync page, through the registry: the router sits
    under the copilot and behind ``crm_sync`` (Admin → Feature Access; open to
    everyone by default), so the sweep keeps whoever passes both. Who that is
    is data, never a department name.

    Seed and impersonation fixtures are dropped the same way the priorities
    sweep drops them: there is no real person behind boss@example.com or
    impersonated_user@sales.local, so scanning for one is pure spend. The
    `users` table has no active/deactivated column — offboarding is reflected by
    the row being removed — so there is nothing further to filter on here.
    """
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.models import User
    from rubik_core.features import feature_allows
    from rubik_core.priorities.batch import is_pipeline_fixture_email

    with SessionLocal() as db:
        rows = db.query(User).filter(User.is_platform_support.is_(False)).all()
        return [
            (u.email or "").lower()
            for u in rows
            if u.email
            and not is_pipeline_fixture_email(u.email)
            and feature_allows("copilot", u, db)
            and feature_allows("crm_sync", u, db)
        ]


def _run_all_users(since_hours: int) -> int:
    from datetime import datetime, timedelta, timezone

    from rubik_core.crm_sync import run_state, service

    since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    emails = _eligible_emails()
    logger.info("crm_sync_job nightly sweep: %d eligible user(s)", len(emails))

    for email in emails:
        try:
            run = run_state.create_run(
                email, trigger=run_state.TRIGGER_NIGHTLY, window_start=since
            )
            run_state.mark_executor(run["run_id"], run_state.EXECUTOR_JOB)
            service.run_scan(run["run_id"], email, since=since)
        except Exception:  # noqa: BLE001 — one user's failure must not end the sweep
            logger.exception("crm_sync_job: nightly scan failed for %s", email)
    return 0


def main(argv: list[str] | None = None) -> int:
    from backend.jobs.bootstrap import bootstrap_job_workspace

    if not bootstrap_job_workspace():
        return 2

    args = _build_parser().parse_args(argv)

    # Deferred import: pulls the app graph (DB, settings, Vertex). Keeps --help
    # fast and surfaces an import failure inside the task, not at parse time.
    from rubik_core.crm_sync import service

    try:
        if args.all_users:
            _run_all_users(args.since_hours)
        elif args.run_id and args.user_email:
            logger.info(
                "crm_sync_job start run=%s user=%s", args.run_id, args.user_email
            )
            # run_scan is best-effort: it records `failed` on the run row rather
            # than raising, so the task still exits 0 and Cloud Run's retry
            # never re-runs an expensive scan whose outcome is already recorded.
            service.run_scan(args.run_id, args.user_email, since_days=args.since_days)
            logger.info("crm_sync_job done run=%s", args.run_id)
        else:
            logger.error(
                "crm_sync_job: need either --all-users or both --run-id and --user-email"
            )
            return 0
    except Exception:  # noqa: BLE001
        # Belt to run_scan's braces. run_scan records `failed` on the run row
        # instead of raising, but anything OUTSIDE it — resolving the roster,
        # creating a run row — was propagating out of main() and exiting
        # non-zero, which is exactly what Cloud Run retries. Retrying a scan
        # that has already spent its LLM budget helps nobody, so this exits 0
        # and leaves the traceback in the log as the record.
        logger.exception("crm_sync_job failed")
    finally:
        # Separate process: flush the AI-cost ledger explicitly, since a
        # terminated job may skip the atexit handler.
        try:
            from rubik_core.cost.usage import flush_usage

            flush_usage()
        except Exception:  # noqa: BLE001 — cost accounting must never fail the job
            logger.debug("crm_sync_job: cost flush failed", exc_info=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())

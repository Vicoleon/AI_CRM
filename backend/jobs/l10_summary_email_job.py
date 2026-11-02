"""``cubie-worker`` task: mail the post-meeting recap for recorded L10s.

The sweep itself lives in ``backend/services/l10_summary_email.py``; this is
the CLI front door so the same code can be driven three ways without drifting:

* Cloud Scheduler → ``POST /api/agents/eos/l10/summary-emails`` → this
  workspace-scoped ``cubie-worker`` Cloud Run Job;
* a laptop, read-only, to see what would be mailed.

Run it from the repo root with the project's virtualenv (Python 3.10):

    # DRY RUN — resolve recordings + recipients, send nothing, spend no tokens
    .venv/bin/python -m backend.jobs.l10_summary_email_job --dry-run

    # actually send
    .venv/bin/python -m backend.jobs.l10_summary_email_job

    # widen/narrow the window (default: CUBIE_L10_SUMMARY_LOOKBACK_HOURS)
    .venv/bin/python -m backend.jobs.l10_summary_email_job --hours 168 --dry-run

The selected workspace's control and tenant databases determine which meetings
this reads. A live run can send real email. Configure its L10 go-live date in
workspace operations first; without it, even a dry run reports ``before_start``
and no live send is possible. Use ``--dry-run`` to preview a configured window.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import timedelta


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Report recordings + recipients; send nothing, generate nothing.",
    )
    p.add_argument(
        "--hours",
        type=int,
        default=None,
        help="Lookback window in hours (default: CUBIE_L10_SUMMARY_LOOKBACK_HOURS).",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    from backend.jobs.bootstrap import bootstrap_job_workspace

    if not bootstrap_job_workspace():
        return 2

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    args = _parse_args(argv)

    from backend.services.l10_summary_email import sweep_l10_summary_emails
    from rubik_core.database.db import SessionLocal

    lookback = timedelta(hours=args.hours) if args.hours else None
    db = SessionLocal()
    try:
        report = sweep_l10_summary_emails(db, dry_run=args.dry_run, lookback=lookback)
    finally:
        db.close()

    print(report.summary())
    for outcome in report.outcomes:
        print("  " + outcome.describe())
        for email, reason in sorted(outcome.excluded.items()):
            print(f"      skipped {email}: {reason}")
        if outcome.error:
            print(f"      error: {outcome.error}")
    return 1 if report.by_status("failed") else 0


if __name__ == "__main__":
    sys.exit(main())

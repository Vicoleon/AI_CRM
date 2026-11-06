"""The weekday-morning AE loop-closure scan (Loose Ends), as one Job execution.

    python -m backend.jobs.loop_closure_scan_job [--dry-run] [--emails a@x,b@x]
        [--no-digest] [--no-scorecard] [--trigger scheduler|admin|cli]

Dispatched to ``cubie-worker`` by ``POST /api/agents/jobs/loop-closure/scan``
(``backend/routers/agents.py``), which Cloud Scheduler calls at 07:10 on
weekdays in the workspace's business time zone. The
scan itself — roster, Gmail, Slack, the HubSpot index, the one Flash call per
AE, closure re-checks, the digest and the scorecard — is
``rubik_core.loop_closure.scan.run_scan``; this module is the
entry point and the exit code. The contract and runbook are
``docs/loose_ends/README.md``.

**Why a Job and not the request.** Over the whole roster the scan is a Gmail
sweep, a portal-wide engagement search and a Flash call per AE. That can
outrun Cloud Run's 300s request cap, and a run cut off half-way leaves some AEs
on yesterday's list with nothing saying so. A Job task is CPU-allocated and has
an hour.

**The Service's values win.** The endpoint passes every ``LOOP_CLOSURE_*``
setting it holds as env on the execution, so tuning a threshold on the Service
(README §6) reaches the next morning's scan. Whether the daily Slack post goes
out, and to which channel, is not an env value: an admin sets it in Admin →
Loose Ends and the digest reads it from the database, so nothing extra is
passed here for it.

**The exit code carries the same distinction as the sibling sweeps.** Finding
loose ends is the job working. One AE whose mailbox could not be read is a
named coverage state inside the report, not a failed execution — exiting
non-zero for it would page "Cubie Cloud Run Job execution failed" every
morning someone's Gmail hiccups, and teach whoever is paged that this job is
flaky. But a scan that could not look AT ALL (no database, a bad import, the
report saying ``failed``) exits 1, so that alert fires: a backstop that stays
quiet when it could not look is the original bug in a new hat.

``--dry-run`` ignores ``LOOP_CLOSURE_ENABLED`` on purpose — reviewing what the
scan would flag before the switch is flipped is the point of a dry run — and
rolls everything back: no rows, no digest, no scorecard.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

# Same as the sibling jobs: without this the root logger sits at WARNING on
# Cloud Run and the start / done lines below are silently dropped.
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("loop_closure_scan_job")

#: The scan ran. Includes finding loose ends and per-AE "could not look"
#: coverage states — those are findings, reported on the page and the digest.
EXIT_OK = 0
#: The scan could not run at all. Non-zero on purpose, so the Job-failure
#: alert pages instead of the silence passing for a quiet morning.
EXIT_ERROR = 1

#: ``loop_scan_runs.trigger`` values (README §3).
TRIGGERS = ("scheduler", "admin", "cli")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="loop_closure_scan_job")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Scan and report, then roll back: no rows, no digest, no scorecard. "
            "Runs even with LOOP_CLOSURE_ENABLED off."
        ),
    )
    p.add_argument(
        "--emails",
        default="",
        help=(
            "Comma-separated AE emails to scan instead of the whole roster "
            "(Admin → Feature Access → Loose Ends)."
        ),
    )
    p.add_argument(
        "--no-digest",
        action="store_true",
        help="Do not post the daily Slack digest this run.",
    )
    p.add_argument(
        "--no-scorecard",
        action="store_true",
        help="Do not write the weekly scorecard number this run.",
    )
    p.add_argument(
        "--trigger",
        choices=TRIGGERS,
        default="cli",
        help=(
            "Recorded on the run row. The scheduler endpoint passes "
            "'scheduler' or 'admin'; a hand run is 'cli'."
        ),
    )
    return p


def _emails(raw: str) -> list[str] | None:
    """``--emails`` as a de-duplicated, lower-cased list; ``None`` = the roster."""
    out: list[str] = []
    for part in (raw or "").split(","):
        email = part.strip().lower()
        if email and email not in out:
            out.append(email)
    return out or None


def _buckets(report: dict) -> str:
    """The report's top-level counts as one ``key=value`` line, sorted.

    Scalars only: the nested per-AE coverage stays in the printed JSON, which
    keeps the ``[loop-closure] done`` line short enough to read in a log list.
    """
    parts = [
        f"{key}={value}"
        for key, value in sorted(report.items())
        if isinstance(value, (int, float, str, bool)) or value is None
    ]
    return " ".join(parts)


def _task_attempt() -> int:
    """Cloud Run's 0-based retry index of this Job task (0 outside a Job)."""
    try:
        return int(os.environ.get("CLOUD_RUN_TASK_ATTEMPT") or 0)
    except ValueError:
        return 0


def _flush_usage() -> None:
    # Separate process: flush the AI-cost ledger explicitly, since a terminated
    # job may skip the atexit handler (the judge's Flash calls are billed to
    # the "Loose Ends" category).
    try:
        from rubik_core.cost.usage import flush_usage

        flush_usage()
    except Exception:  # noqa: BLE001 — cost accounting must never fail the job
        logger.debug("loop_closure_scan_job: cost flush failed", exc_info=True)


def main(argv: list[str] | None = None) -> int:
    from backend.jobs.bootstrap import bootstrap_job_workspace

    if not bootstrap_job_workspace():
        return 2

    args = _build_parser().parse_args(argv)
    try:
        # Deferred: pulls the app graph (DB, settings, Gmail, Vertex). Keeps
        # --help fast and puts an import failure inside the task, where the
        # Job-failure alert sees it, rather than at parse time.
        from rubik_core.config import settings

        if not args.dry_run and not settings.LOOP_CLOSURE_ENABLED:
            logger.info("[loop-closure] LOOP_CLOSURE_ENABLED is off — nothing scanned")
            print(json.dumps({"status": "disabled", "flag": "LOOP_CLOSURE_ENABLED"}))
            return EXIT_OK

        emails = _emails(args.emails)
        logger.info(
            "[loop-closure] start dry_run=%s scope=%s trigger=%s",
            args.dry_run,
            f"{len(emails)} AE(s)" if emails else "roster",
            args.trigger,
        )
        try:
            from rubik_core.database.db import SessionLocal
            from rubik_core.loop_closure.scan import run_scan

            db = SessionLocal()
            try:
                report = run_scan(
                    db,
                    emails=emails,
                    dry_run=args.dry_run,
                    trigger=args.trigger,
                    post_digest=not args.no_digest,
                    write_scorecard=not args.no_scorecard,
                )
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001 — the exit code is the report
            from rubik_core.logsafe import safe_error

            logger.error(
                "[loop-closure] the scan could not run — nothing was checked, so "
                "this is NOT an all-clear: %s",
                safe_error(exc),
            )
            return EXIT_ERROR

        report = report if isinstance(report, dict) else {}
        print(json.dumps(report, default=str, sort_keys=True))
        logger.info("[loop-closure] done %s", _buckets(report))
        if report.get("status") == "failed":
            return EXIT_ERROR
        if report.get("status") == "locked" and _task_attempt() > 0:
            # A RETRY that found the lock held: the first attempt already failed
            # and this one scanned nobody. Exiting 0 would mark the execution
            # succeeded and keep the Job-failure alert quiet.
            logger.error(
                "[loop-closure] retry attempt found the run lock held — nothing "
                "was scanned, so this is NOT an all-clear"
            )
            return EXIT_ERROR
        return EXIT_OK
    finally:
        _flush_usage()


if __name__ == "__main__":
    sys.exit(main())

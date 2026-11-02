"""Nightly backstop: does every imported L10 still exist on Google Calendar?

    python -m backend.jobs.l10_calendar_reconcile_job

Run as one ``cubie-worker`` execution from Cloud Scheduler. The sweep itself,
and the reasoning behind every rule it obeys, lives in
``rubik_core/eos/calendar_reconcile.py``; this module is the entry point, the
flags and the exit code.

**Why a job and not just the endpoint.** ``GET /api/eos/l10/calendar-status``
already reconciles a meeting against Google — but only when somebody opens EOS
→ Meetings, and it writes back only for that meeting's manager. Those two
restrictions are individually correct and jointly leave the original bug
standing: a manager who cancels their L10 on the calendar and then never opens
the board leaves the row stale, and nobody else is permitted to fix it for
them. Meanwhile the surfaces that read the stored answer — the PTO hand-off
brief, "your next L10" — are exactly the ones a person is not looking at the
board while using. A meeting nobody visits is precisely the meeting that goes
wrong, so the check cannot be triggered by a visit.

**The exit code carries a distinction that matters**, the same one
``stale_work_sweep_job`` documents. Finding cancellations is NOT a failure —
it is the job succeeding, loudly, and the cancellations are the deliverable.
Exiting non-zero on a finding would page through "Cubie Cloud Run Job execution
failed" every time a team cancelled a meeting, and would teach whoever is on
the receiving end that this job is flaky. Being unable to CHECK is a different
thing and exits non-zero, because a backstop that stays quiet when it could not
look is the original bug wearing a different hat.
"""

from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("l10_calendar_reconcile_job")

#: The sweep ran. Includes finding cancelled meetings — that is a successful
#: check with a finding, and the finding is the whole point.
EXIT_OK = 0
#: The sweep could not run (no database, a bad import). Non-zero on purpose so
#: the Job-failure alert pages rather than the silence being self-inflicted.
EXIT_ERROR = 1
#: The sweep ran, had meetings to ask about, and got a usable answer for NOT
#: ONE of them — every lookup came back ``unknown``. That is a Workspace
#: credential or Calendar API outage, not a quiet night: nothing was written,
#: nothing was learned, and every stale row stayed stale. Separated from
#: EXIT_ERROR so the operator who opens the alert knows the sweep itself is
#: healthy and the problem is upstream at Google.
EXIT_BLIND = 2


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="l10_calendar_reconcile_job")
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "Max meetings reconciled in one execution (default: the module's "
            "own cap). The sweep takes the least-recently-checked first, so a "
            "capped run rotates rather than re-checking the same prefix."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Ask Google and report what WOULD change, writing nothing. The "
            "right first command against real data: this job's job is to mark "
            "meetings cancelled, so seeing which ones before it does is cheap."
        ),
    )
    return p


def main(argv: list[str] | None = None) -> int:
    from backend.jobs.bootstrap import bootstrap_job_workspace

    if not bootstrap_job_workspace():
        return 2

    args = _build_parser().parse_args(argv)

    # Deferred: pulls the app graph (DB, settings, Google client). Keeps --help
    # fast and puts an import failure inside the task, where the Job-failure
    # alert sees it, rather than at parse time.
    from rubik_core.eos.calendar_reconcile import DEFAULT_LIMIT, reconcile_l10_calendars

    limit = args.limit if args.limit is not None else DEFAULT_LIMIT
    logger.info(
        "l10_calendar_reconcile_job start limit=%d dry_run=%s", limit, args.dry_run
    )

    report = reconcile_l10_calendars(limit=limit, dry_run=args.dry_run)
    if report is None:
        logger.error(
            "l10_calendar_reconcile_job: the sweep could not run — no meeting "
            "was checked, so this is NOT an all-clear"
        )
        return EXIT_ERROR

    logger.info(
        "l10_calendar_reconcile_job done asked=%d known=%d active=%d cancelled=%d "
        "changed=%d unknown=%d no_authority=%d unauthorized=%d calendars=%d "
        "deferred=%d dry_run=%s",
        report.asked,
        report.known,
        report.active,
        report.cancelled,
        report.changed,
        report.unknown,
        report.no_authority,
        report.unauthorized,
        report.calendars,
        report.deferred,
        args.dry_run,
    )
    if report.changed_ids:
        logger.info(
            "l10_calendar_reconcile_job changed meetings: %s",
            ", ".join(report.changed_ids),
        )

    if report.is_blind:
        logger.error(
            "l10_calendar_reconcile_job: asked about %d meeting(s) and got an "
            "answer for none of them — Google Calendar could not be read, so "
            "every stale schedule is still stale. Nothing was written.",
            report.asked,
        )
        return EXIT_BLIND

    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

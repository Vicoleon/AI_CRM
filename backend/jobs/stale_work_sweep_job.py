"""Nightly backstop: work somebody asked for that never produced a result.

    python -m backend.jobs.stale_work_sweep_job

Run as one ``cubie-worker`` execution from Cloud Scheduler. The probes and the
reasoning behind each threshold live in ``rubik_core/reliability/stale_work.py``;
this module is the entry point and the exit code.

**Why a job of its own** rather than a few lines inside a feature's existing
nightly run. The first Smart Teams check lodged in
``smart_teams_skill_sweep_job`` purely because that job was already scheduled —
defensible for one feature, indefensible the moment Deep Prep and Deep X-Ray
need the same thing, since neither has a nightly job to borrow and an X-Ray
check inside a job called "smart teams skill sweep" is a thing nobody will find
again. One scheduled sweep, one place to add the next feature.

**The exit code carries a distinction that matters.** Finding stale work is
NOT a failure of this job — it did its job, and the per-feature stale alerts
are what report the finding. Exiting non-zero on a finding would double-page
through "Cubie Cloud Run Job execution failed" and, worse, teach whoever is on
the receiving end that this job is flaky. But being unable to CHECK is a
failure, and exits 1 so that alert fires: a backstop that stays quiet when it
could not look is the original bug wearing a different hat.
"""

from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("stale_work_sweep_job")

#: The sweep ran. Includes finding stale work — that is a successful check with
#: a finding, reported by the per-feature alerts, not a failed execution.
EXIT_OK = 0
#: The sweep could not run (no database, a bad import). Non-zero on purpose, so
#: the Job-failure alert pages rather than the silence being self-inflicted.
EXIT_ERROR = 1


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="stale_work_sweep_job")
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "Max items reported per feature (default: the module's own cap). "
            "A bad week must not turn one nightly log line into a flood."
        ),
    )
    return p


def main(argv: list[str] | None = None) -> int:
    from backend.jobs.bootstrap import bootstrap_job_workspace

    if not bootstrap_job_workspace():
        return 2

    args = _build_parser().parse_args(argv)

    # Deferred: pulls the app graph (DB, settings). Keeps --help fast and puts
    # an import failure inside the task, where the Job-failure alert sees it,
    # rather than at parse time.
    from rubik_core.reliability.stale_work import DEFAULT_LIMIT, report_stale_work

    limit = args.limit if args.limit is not None else DEFAULT_LIMIT
    logger.info("stale_work_sweep_job start limit=%d", limit)

    items = report_stale_work(limit=limit)
    if items is None:
        logger.error(
            "stale_work_sweep_job: the sweep could not run — nothing was "
            "checked, so this is NOT an all-clear"
        )
        return EXIT_ERROR

    logger.info(
        "stale_work_sweep_job done stale=%d features_affected=%d",
        len(items),
        len({item.feature for item in items}),
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

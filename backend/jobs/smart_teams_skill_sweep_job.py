"""Cloud Run Job entrypoint for the Smart Teams skill inference sweep (ST-8).

One execution of the ``cubie-worker`` job:

    python -m backend.jobs.smart_teams_skill_sweep_job
    python -m backend.jobs.smart_teams_skill_sweep_job --emails a@example.com
    python -m backend.jobs.smart_teams_skill_sweep_job --since 2026-01-01 --limit 10

Registered in ``backend/jobs/__init__.py`` as ``smart-teams-skill-sweep``, so
:func:`backend.jobs.worker_job_execute_command` prints the exact ``gcloud`` line
rather than anybody hand-assembling the comma-joined ``--args`` string.

**Why a Job and never a request.** The sweep reads meeting transcripts and makes
one Vertex call per person. Cloud Run throttles the Service's CPU to near-zero
the instant the HTTP response returns, so a ``BackgroundTask`` doing this is
starved silently. That is why ``POST /api/smart-teams/skills/refresh``
dispatches this module instead of doing the work itself.

**Two dispatch paths, one of them still missing.** The self-refresh endpoint
launches this for a single person. The nightly company-wide pass needs a Cloud
Scheduler entry, which lives in GCP and not in git — until that exists, the
scheduled sweep does not happen and the ``wants_cadence`` field on the registry
entry is a statement of intent, not of fact.

**Internal signals only.** Every reader this job runs is in
``rubik_core/smart_teams/skill_inference.py`` and reads the company's own
systems: meetings the person was in and documents they uploaded. There is no
web search and no external source about an employee, by
construction and not by policy — see that module's HARD BOUNDARY section.

**Idempotence, twice over.** A person whose evidence corpus is byte-identical to
the previous run is skipped entirely (no Vertex call, no writes), and an
unchanged citation set on a row that IS re-read writes nothing. So a re-run
after an interruption is cheap rather than a second full bill.

**A denial is never re-asked**, and the guarantee is in two independent places.
``build_candidates`` drops the person's tombstoned skills before the model is
called (so a denial also costs no tokens), and ``upsert_inference`` refuses the
write regardless (so the rule holds even if a future caller assembles
candidates some other way).

Read ``refused_at_write`` in the log line accordingly: it counts the times
the WRITE path had to refuse, so on a healthy run it is **zero** — the candidate
filter got there first. It is an inconsistency alarm, not a measure of how often
the rule fired. A non-zero value means something reached the write path with a
denied skill in hand, which is worth looking at even though nothing was
written.

**Exit codes.** ``0`` for a run that completed, INCLUDING one that suggested
nothing — a quiet night is a product outcome and failing the execution for it
would make the FAILED list useless. Non-zero only for the two states an operator
must see:

* :data:`EXIT_OUTAGE` — every reader failed for every person. Zero suggestions
  because nothing could be read must never be recorded the same way as zero
  suggestions because there was nothing to say, and the watermark is
  deliberately NOT advanced in that case so the unread window is swept again.
* :data:`EXIT_ERROR` — the sweep itself raised. A retry may well succeed;
  ``cubie-worker`` runs with ``--max-retries=1``, and the run lock plus the
  per-person digest make a retry safe.

A run that could not start because another execution holds the lock exits 0: it
is not a failure, and nothing was skipped — the other run is doing the work.

ADVISORY ONLY. What this job produces are questions for a person about their own
work, never a measure of competence, potential, promotion-readiness or fitness
for a role.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("smart_teams_skill_sweep_job")

#: The run completed. Includes a run that suggested nothing, and a run that did
#: not start because another execution holds the lock.
EXIT_OK = 0
#: The sweep raised. Retry is safe (run lock + per-person digest).
EXIT_ERROR = 1
#: Every reader failed for every person. Nothing was read, so nothing is known —
#: and the watermark was left where it was so the window is not lost.
EXIT_OUTAGE = 3


def _iso_date(value: str) -> datetime:
    """``--since`` accepts a date or a full ISO timestamp."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as e:  # noqa: TRY003 — argparse renders this verbatim
        raise argparse.ArgumentTypeError(
            f"{value!r} is not an ISO-8601 date or timestamp"
        ) from e


def _emails(value: str) -> list[str]:
    return [part.strip().lower() for part in value.split(",") if part.strip()]


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="smart_teams_skill_sweep_job")
    p.add_argument(
        "--emails",
        type=_emails,
        default=None,
        help=(
            "Comma-separated addresses to sweep instead of the whole roster. "
            "What POST /api/smart-teams/skills/refresh passes for one person."
        ),
    )
    p.add_argument(
        "--since",
        type=_iso_date,
        default=None,
        help=(
            "Override the watermark and read sources CREATED on or after this "
            "date. The incremental floor is the stored watermark minus a "
            "two-day overlap, which does not reach a source created before the "
            "overlap and finished inside it — this is how you reach one."
        ),
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "Cap how many people this run processes, and with them how many "
            "Vertex calls it may make. Counts people actually PROCESSED: "
            "somebody whose evidence has not changed since the last run costs a "
            "read and no Vertex call, and does not spend the budget — so "
            "successive capped runs walk down the roster instead of redoing the "
            "same prefix. The roster is sorted, so that walk is deterministic."
        ),
    )
    p.add_argument(
        "--no-seed",
        action="store_true",
        help=(
            "Skip the idempotent taxonomy seed that normally opens a run. The "
            "seed is what flips declared-skill validation from shape-only to "
            "membership-enforced, so skip it only when re-running a sweep "
            "minutes after one that already seeded."
        ),
    )
    return p


def main(argv: list[str] | None = None) -> int:
    from backend.jobs.bootstrap import bootstrap_job_workspace

    if not bootstrap_job_workspace():
        return 2

    args = _build_parser().parse_args(argv)

    # Deferred import: pulls the app graph (DB, settings, Vertex). Keeps --help
    # fast and surfaces an import failure inside the task, not at parse time.
    from rubik_core.smart_teams.skill_inference import run_sweep

    logger.info(
        "smart_teams_skill_sweep_job start emails=%s since=%s limit=%s seed=%s",
        ",".join(args.emails) if args.emails else "roster",
        args.since.isoformat() if args.since else "watermark",
        args.limit or "none",
        not args.no_seed,
    )
    try:
        report = run_sweep(
            emails=args.emails,
            since=args.since,
            limit=args.limit,
            seed=not args.no_seed,
        )
    except Exception:  # noqa: BLE001 — trace it, then fail the task loudly
        logger.exception("smart_teams_skill_sweep_job failed")
        return EXIT_ERROR
    else:
        if report.not_run_reason:
            # Another execution holds the lock. Not a failure and nothing was
            # skipped — that run is doing this work.
            logger.info(
                "smart_teams_skill_sweep_job did not run: %s", report.not_run_reason
            )
            return EXIT_OK

        logger.info(
            "smart_teams_skill_sweep_job done people=%d suggested=%d "
            "refused_at_write=%d (expected 0) unchanged=%d floor=%s outage=%s",
            len(report.people),
            report.suggested,
            report.skipped_denied,
            sum(1 for person in report.people if person.skipped_unchanged),
            report.floor.isoformat(),
            report.outage,
        )
        # Per-person detail at INFO for the two states that explain a quiet
        # result, so "nobody got a suggestion" is diagnosable from the execution
        # log without re-running anything.
        for person in report.people:
            if person.error or person.all_readers_failed:
                logger.warning(
                    "smart_teams_skill_sweep_job %s: error=%s readers=%s",
                    person.user_email,
                    person.error or "every reader failed",
                    person.readers,
                )
        if report.outage:
            logger.error(
                "smart_teams_skill_sweep_job: every source failed for every "
                "person — the watermark was left where it was, so the unread "
                "window is swept again next run"
            )
            return EXIT_OUTAGE
        return EXIT_OK
    finally:
        # Separate process: flush the AI-cost ledger explicitly, since a
        # terminated job may skip the atexit handler. One Vertex call per person
        # is billed to "smart_teams" by `judge_candidates`'s @attribute_to;
        # dropping the flush here is how a feature's spend ends up in the AI
        # Cost tab's "Unattributed" row.
        try:
            from rubik_core.cost.usage import flush_usage

            flush_usage()
        except Exception:  # noqa: BLE001 — cost accounting must never fail the job
            logger.debug(
                "smart_teams_skill_sweep_job: cost flush failed", exc_info=True
            )


if __name__ == "__main__":
    sys.exit(main())

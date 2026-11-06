"""Cloud Run Job entrypoint for scoring ONE completed Work Style Interview.

Launched by ``POST /api/smart-teams/interview/complete``
(``backend/routers/smart_teams.py``, which names this module in
``SCORING_JOB_MODULE``) as one execution of ``cubie-worker``:

    python -m backend.jobs.smart_teams_score_job --interview-id <id>

Scoring never runs in the web process. Six instruments plus one LLM extraction
is minutes of work, and Cloud Run throttles the service's CPU to near-zero the
moment the HTTP response returns — a ``BackgroundTask`` doing this is starved
silently and the person's profile simply never appears.

## Why this file has to be noisy about failing

This module existed only as a string in the router for one deploy, and that is
the whole reason its docstring is this long. ``launch_module_execution`` POSTs
``containerOverrides.args`` to the ``cubie-worker`` job; the Cloud Run Jobs API
validates that the JOB exists, not that the module inside it does. So a launch
of a module that is not in the image returns 200, ``submit_background_work``
reports ``executor="job"``, the endpoint returns 202 — and the interview sits
``completed`` with no profile, forever, with nothing anywhere saying why. The
person who spent 30 minutes answering questions about themselves sees a
spinner that resolves to ``delayed`` after 20 minutes and no more than that.

So a failure here leaves **two** durable traces, neither of which depends on
somebody having a log tab open at the right moment:

1. a non-zero exit, which marks the Cloud Run Job execution FAILED (visible in
   the executions list, and the thing a log-based metric can alert on); and
2. an ``audit_logs`` row — ``SMART_TEAMS_SCORING_FAILED`` — readable through
   ``GET /api/admin/audit?action=SMART_TEAMS_SCORING_FAILED``, keyed to the
   interview id in ``doc_id`` and to the person in ``actor_email``.

``actor_email`` on that row names **the person whose profile is missing**, not
an actor who did something: it is the only field the admin view can filter on,
and burying the one identifying detail in ``details`` would make the row
unfindable for the question anybody actually asks ("why has Sam still not got a
profile?"). ``details["job"]`` records who really wrote it. When the interview
row cannot be read at all, ``actor_email`` falls back to
:data:`SYSTEM_ACTOR`, which is deliberately not an email address so it can
never be mistaken for a person.

A third case gets a row of its own: a run that *succeeded* and produced nothing
reportable, or lost one or more layers to an error. ``ScoringReport`` keeps
"this layer failed" and "this layer honestly refused" apart, and only the first
is an operational problem — but a profile with no reportable layer at all is
worth someone looking at either way, because it is a document about a person
with nothing in it. That row is ``SMART_TEAMS_SCORING_DEGRADED`` and the job
still exits 0: a version WAS written, and re-running would score the same
answers to the same result.

If the database itself is unreachable, the audit write fails too and the log
line plus the non-zero exit are all there is. That is stated rather than
papered over.

## Retries

``cubie-worker`` is deployed with ``--max-retries=1``, so a non-zero exit buys
exactly one more attempt. That is safe and wanted:
``profile.score_and_store_interview`` is idempotent on
``st_interviews.profile_id`` — a second run of an interview that is already
scored returns the existing version with ``created=False`` and writes nothing —
so the retry either finishes work that died late (a commit that timed out after
the LLM call) or fails the same way twice, which is the honest outcome for an
interview that cannot be scored. The retry after a
:class:`~rubik_core.smart_teams.profile.ProfileError` (unknown id, unfinished
interview) is wasted but free: that check runs before any Vertex call.

## Advisory only

What this job writes describes how somebody prefers to work, for the purpose of
working together better. It is not a measure of competence, potential, or
fitness for a role, and nothing downstream of it may be wired to performance
review, promotion, or compensation.
"""

from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("smart_teams_score_job")

#: ``audit_logs.actor_email`` when the interview row cannot be read, so its
#: owner is unknown. Not an email address on purpose — an audit row must never
#: look like it names a person it does not.
SYSTEM_ACTOR = "smart-teams-score-job"

#: Nothing was written. Somebody has a completed interview and no profile.
ACTION_FAILED = "SMART_TEAMS_SCORING_FAILED"
#: A version was written, but a layer errored or the whole profile came back
#: with nothing reportable.
ACTION_DEGRADED = "SMART_TEAMS_SCORING_DEGRADED"

#: Exit codes. Cloud Run only reads zero/non-zero; the split is for the human
#: reading the execution log.
EXIT_OK = 0
#: An unexpected error. A retry may well succeed.
EXIT_ERROR = 1
#: A refusal from the profile writer — unknown interview id, or sections still
#: to complete. Retrying scores nothing; a person has to look at the row.
EXIT_REFUSED = 2


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="smart_teams_score_job")
    p.add_argument(
        "--interview-id",
        required=True,
        help="st_interviews.id of a completed interview to score.",
    )
    return p


def _interview_owner(interview_id: str) -> str | None:
    """``st_interviews.user_email`` for the audit row, or ``None``.

    Its own short-lived session: the scoring session is closed (or poisoned) by
    the time this is needed, and this read must not be able to fail the job.
    """
    try:
        from rubik_core.database.db import SessionLocal
        from rubik_core.database.smart_teams import SmartTeamsInterview

        db = SessionLocal()
        try:
            row = db.get(SmartTeamsInterview, interview_id)
            return (row.user_email or "").strip().lower() if row is not None else None
        finally:
            db.close()
    except Exception:  # noqa: BLE001 — best-effort attribution, never fatal
        logger.debug(
            "smart_teams_score_job: could not resolve the owner of %s",
            interview_id,
            exc_info=True,
        )
        return None


def _record(
    interview_id: str, action: str, details: dict, *, owner: str | None = None
) -> None:
    """Write the durable trace. Swallows its own failures, loudly.

    ``record_audit`` already logs at ERROR and rolls back if the insert fails,
    so the only thing left to guard here is the import and the session — if the
    database is unreachable there is nothing to write to and the log line
    written by the caller is the whole record.
    """
    try:
        from backend.audit import record_audit
        from rubik_core.database.db import SessionLocal

        db = SessionLocal()
        try:
            record_audit(
                db,
                actor_email=owner or SYSTEM_ACTOR,
                action=action,
                status="failure",
                target=interview_id,
                details={"job": SYSTEM_ACTOR, **details},
            )
        finally:
            db.close()
    except Exception:  # noqa: BLE001 — the trace must not become the failure
        logger.exception(
            "smart_teams_score_job: could not record %s for interview %s — the "
            "log line above is the only record of it",
            action,
            interview_id,
        )


def _report_layer_failures(interview_id: str, write, owner: str | None) -> None:
    """Say out loud when a stored profile is thinner than it should be.

    ``report`` is ``None`` on the idempotent path (nothing was scored), so
    there is nothing to inspect and nothing to report.
    """
    report = getattr(write, "report", None)
    if report is None:
        return

    failures = {
        name: {"stage": failure.stage, "message": failure.message}
        for name, failure in report.failures.items()
    }
    for name, failure in failures.items():
        logger.error(
            "smart_teams_score_job: interview %s lost the %s layer at stage %s: %s",
            interview_id,
            name,
            failure["stage"],
            failure["message"],
        )
    if report.is_empty:
        logger.error(
            "smart_teams_score_job: interview %s scored to a profile with no "
            "reportable layer — the person gets a document with nothing in it",
            interview_id,
        )
    if not failures and not report.is_empty:
        return

    _record(
        interview_id,
        ACTION_DEGRADED,
        {
            "profile_id": write.profile.id,
            "version": write.version,
            "reportable_frameworks": list(report.reportable_frameworks),
            "failed_frameworks": list(report.failed_frameworks),
            "layer_failures": failures,
            "empty": report.is_empty,
        },
        owner=owner,
    )


def main(argv: list[str] | None = None) -> int:
    from backend.jobs.bootstrap import bootstrap_job_workspace

    if not bootstrap_job_workspace():
        return 2

    args = _build_parser().parse_args(argv)
    interview_id = args.interview_id

    # Deferred import: pulls the app graph (DB, settings, Vertex). Keeps --help
    # fast and surfaces an import failure inside the task, not at parse time.
    from rubik_core.smart_teams import scoring
    from rubik_core.smart_teams.profile import ProfileError

    logger.info("smart_teams_score_job start interview=%s", interview_id)
    try:
        try:
            write = scoring.score_interview(interview_id)
        except ProfileError as exc:
            # Unknown interview, or sections still outstanding. Deterministic:
            # a retry produces this same refusal.
            logger.error(
                "smart_teams_score_job: refusing to score interview %s: %s",
                interview_id,
                exc,
            )
            _record(
                interview_id,
                ACTION_FAILED,
                {
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "retryable": False,
                },
                owner=_interview_owner(interview_id),
            )
            return EXIT_REFUSED
        except Exception as exc:  # noqa: BLE001 — trace it, then fail the task
            logger.exception(
                "smart_teams_score_job: scoring interview %s failed", interview_id
            )
            _record(
                interview_id,
                ACTION_FAILED,
                {
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "retryable": True,
                },
                owner=_interview_owner(interview_id),
            )
            return EXIT_ERROR

        owner = (write.profile.user_email or "").strip().lower() or None
        if write.created:
            logger.info(
                "smart_teams_score_job: interview %s scored into profile %s v%d "
                "(reportable: %s)",
                interview_id,
                write.profile.id,
                write.version,
                ", ".join(write.report.reportable_frameworks) or "none",
            )
        else:
            # The idempotent path: a retried task, or two launches for one
            # completion. Nothing was scored and nothing was written.
            logger.info(
                "smart_teams_score_job: interview %s was already scored into "
                "profile %s v%d — nothing to do",
                interview_id,
                write.profile.id,
                write.version,
            )
        _report_layer_failures(interview_id, write, owner)
        return EXIT_OK
    finally:
        # Separate process: flush the AI-cost ledger explicitly, since a
        # terminated job may skip the atexit handler. The strengths extraction
        # is the one Vertex call in a scoring run and it is billed to
        # "smart_teams" — dropping it here is how a feature's spend ends up in
        # the AI Cost tab's "Unattributed" row.
        try:
            from rubik_core.cost.usage import flush_usage

            flush_usage()
        except Exception:  # noqa: BLE001 — cost accounting must never fail the job
            logger.debug("smart_teams_score_job: cost flush failed", exc_info=True)


if __name__ == "__main__":
    sys.exit(main())

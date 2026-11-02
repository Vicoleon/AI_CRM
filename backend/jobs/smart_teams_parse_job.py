"""Cloud Run Job entrypoint for parsing ONE uploaded official assessment report.

Launched by ``POST /api/smart-teams/uploads``
(``backend/routers/smart_teams_uploads.py``, which names this module in
``PARSE_JOB_MODULE``) as one execution of ``cubie-worker``:

    python -m backend.jobs.smart_teams_parse_job --upload-id <id>

Parsing never runs in the web process. It reads a PDF and makes a Vertex call,
and Cloud Run throttles the service's CPU to near-zero the moment the HTTP
response returns — a ``BackgroundTask`` doing this is starved silently and the
upload sits ``pending`` forever with nothing anywhere saying why.

## This job never merges anything

It writes ``st_uploads.framework``, ``parsed``, ``parse_error`` and
``parsed_at``, and stops. A parse is a **proposal**: the person reads it and
confirms or rejects it through the router, and only a confirmation writes a
profile version. That boundary is the whole design of ST-5 and it lives in
``rubik_core/smart_teams/uploads.py``; this file is argv, exit codes, and the
durable trace.

## Why a failure has to be loud

``launch_module_execution`` POSTs ``containerOverrides.args`` to the
``cubie-worker`` job, and the Cloud Run Jobs API validates that the JOB exists,
not that the module inside it does. A launch of a module that is not in the image
returns 200, the endpoint returns 202, and the upload sits ``pending`` with an
empty proposal forever. So a failure here leaves two durable traces:

1. a non-zero exit, which marks the execution FAILED in the executions list; and
2. an ``audit_logs`` row — ``SMART_TEAMS_UPLOAD_PARSE_FAILED``, written by
   ``uploads.parse_upload`` for an operational refusal and by this module for an
   unexpected one — readable through
   ``GET /api/admin/audit?action=SMART_TEAMS_UPLOAD_PARSE_FAILED``.

``actor_email`` on that row names **the person whose upload is stuck**, not an
actor who did something: it is the only field the admin view filters on, and
burying the identifying detail in ``details`` makes the row unfindable for the
question anybody actually asks. ``details["job"]`` records who really wrote it.
When the upload row cannot be read at all, ``actor_email`` falls back to
:data:`SYSTEM_ACTOR`, which is deliberately not an email address so it can never
be mistaken for a person.

A parse that ran fine and *refused* — an unreadable scan, a report we cannot
identify, an instrument with no merge adapter — exits **0**. Nothing is broken:
the row carries a refusal code and the person is told. Re-running would refuse
the same way. Those refusals are ordinary product outcomes, and failing the
execution for them would make the FAILED list useless for spotting real
breakage.

## Retries

``cubie-worker`` runs with ``--max-retries=1``, so a non-zero exit buys one more
attempt. That is safe: ``uploads.parse_upload`` is idempotent on
``review_status`` — a row the person has already confirmed, rejected or merged is
left exactly as it is, so a retry arriving after a decision cannot re-propose
over it — and re-parsing a still-``pending`` row simply overwrites the proposal
with the same answer.

## Advisory only

What this job proposes describes how somebody prefers to work, for the purpose of
working together better. It is not a measure of competence, potential, or fitness
for a role, and nothing downstream of it may be wired to performance review,
promotion, or compensation.
"""

from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("smart_teams_parse_job")

#: ``audit_logs.actor_email`` when the upload row cannot be read, so its owner is
#: unknown. Not an email address on purpose — an audit row must never look like
#: it names a person it does not.
SYSTEM_ACTOR = "smart-teams-parse-job"

#: Nothing was proposed and the row still has no parse result.
ACTION_FAILED = "SMART_TEAMS_UPLOAD_PARSE_FAILED"

#: Exit codes. Cloud Run only reads zero/non-zero; the split is for the human
#: reading the execution log.
EXIT_OK = 0
#: An unexpected error. A retry may well succeed.
EXIT_ERROR = 1
#: A refusal from the parser itself — unknown upload id, no stored object.
#: Retrying parses nothing; a person has to look at the row.
EXIT_REFUSED = 2


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="smart_teams_parse_job")
    p.add_argument(
        "--upload-id",
        required=True,
        help="st_uploads.id of an uploaded official report to parse.",
    )
    return p


def _upload_owner(upload_id: str) -> str | None:
    """``st_uploads.user_email`` for the audit row, or ``None``.

    Its own short-lived session: the parsing session is closed (or poisoned) by
    the time this is needed, and this read must not be able to fail the job.
    """
    try:
        from rubik_core.database.db import SessionLocal
        from rubik_core.database.smart_teams import SmartTeamsUpload

        db = SessionLocal()
        try:
            row = db.get(SmartTeamsUpload, upload_id)
            return (row.user_email or "").strip().lower() if row is not None else None
        finally:
            db.close()
    except Exception:  # noqa: BLE001 — best-effort attribution, never fatal
        logger.debug(
            "smart_teams_parse_job: could not resolve the owner of %s",
            upload_id,
            exc_info=True,
        )
        return None


def _record(
    upload_id: str, action: str, details: dict, *, owner: str | None = None
) -> None:
    """Write the durable trace. Swallows its own failures, loudly.

    ``record_audit`` already logs at ERROR and rolls back if the insert fails, so
    the only thing left to guard here is the import and the session — if the
    database is unreachable there is nothing to write to and the log line written
    by the caller is the whole record.
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
                target=upload_id,
                details={"job": SYSTEM_ACTOR, **details},
            )
        finally:
            db.close()
    except Exception:  # noqa: BLE001 — the trace must not become the failure
        logger.exception(
            "smart_teams_parse_job: could not record %s for upload %s — the log "
            "line above is the only record of it",
            action,
            upload_id,
        )


def main(argv: list[str] | None = None) -> int:
    from backend.jobs.bootstrap import bootstrap_job_workspace

    if not bootstrap_job_workspace():
        return 2

    args = _build_parser().parse_args(argv)
    upload_id = args.upload_id

    # Deferred import: pulls the app graph (DB, settings, Vertex). Keeps --help
    # fast and surfaces an import failure inside the task, not at parse time.
    from rubik_core.database.db import SessionLocal
    from rubik_core.smart_teams.uploads import UploadError, parse_upload

    logger.info("smart_teams_parse_job start upload=%s", upload_id)
    try:
        db = SessionLocal()
        try:
            proposal = parse_upload(db, upload_id)
        finally:
            db.close()
    except UploadError as exc:
        # An unknown id, or a stored object that cannot be resolved at all.
        # Deterministic: a retry produces this same refusal.
        logger.error(
            "smart_teams_parse_job: refusing to parse upload %s: %s", upload_id, exc
        )
        _record(
            upload_id,
            ACTION_FAILED,
            {"error": str(exc), "error_type": type(exc).__name__, "retryable": False},
            owner=_upload_owner(upload_id),
        )
        return EXIT_REFUSED
    except Exception as exc:  # noqa: BLE001 — trace it, then fail the task
        logger.exception("smart_teams_parse_job: parsing upload %s failed", upload_id)
        _record(
            upload_id,
            ACTION_FAILED,
            {"error": str(exc), "error_type": type(exc).__name__, "retryable": True},
            owner=_upload_owner(upload_id),
        )
        return EXIT_ERROR
    else:
        # A refusal is a product outcome, not a broken execution — see the module
        # docstring. ``parse_upload`` has already written the row and, for the two
        # operational refusals, its own audit trace.
        logger.info(
            "smart_teams_parse_job: upload %s parsed (framework=%s mergeable=%s "
            "reason=%s)",
            upload_id,
            proposal.detected or "-",
            proposal.mergeable,
            proposal.reason or "-",
        )
        return EXIT_OK
    finally:
        # Separate process: flush the AI-cost ledger explicitly, since a
        # terminated job may skip the atexit handler. The report parse is the one
        # Vertex call in this job and it is billed to "smart_teams" — dropping it
        # here is how a feature's spend ends up in the AI Cost tab's
        # "Unattributed" row.
        try:
            from rubik_core.cost.usage import flush_usage

            flush_usage()
        except Exception:  # noqa: BLE001 — cost accounting must never fail the job
            logger.debug("smart_teams_parse_job: cost flush failed", exc_info=True)


if __name__ == "__main__":
    sys.exit(main())

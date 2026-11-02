"""Cloud Run Job entrypoint for the three slow halves of a personal guide (ST-6).

    python -m backend.jobs.smart_teams_guide_job --document-id <id> --generate
    python -m backend.jobs.smart_teams_guide_job --document-id <id> --ingest
    python -m backend.jobs.smart_teams_guide_job --document-id <id> --purge

Launched by ``backend/routers/smart_teams_guides.py``, which names this module
in ``GUIDE_JOB_MODULE``. One module rather than three because all three actions
are "one row of ``st_documents``, one slow external call, one durable trace",
and three files would have triplicated the audit and cost-flush scaffolding
around three different verbs.

Why none of this runs in the web process
----------------------------------------
Cloud Run throttles the Service's CPU to near-zero the moment the HTTP response
returns, so a ``BackgroundTask`` here is starved silently:

* ``--generate`` makes one Gemini Pro call for the prose.
* ``--ingest`` and ``--purge`` are Vertex RAG corpus operations, which serialise
  behind other operations on the same corpus — ``rag/manager._import_to_corpus``
  retries up to 20 times around the "other operations running" precondition, and
  a single import can back off for minutes.

## What each action promises, and what it refuses

``--generate`` is the only one that may run against an unpublished row, and it
refuses a published one (``guide.generate_into_row``): the published wording is
what its subject consented to. On any failure it leaves ``status="failed"`` with
the reason in ``error`` — never ``pending`` forever, which is the state that
makes a person watch a spinner with nothing anywhere saying why.

``--ingest`` refuses anything that is not published AND approved. It is the only
RAG write Smart Teams makes: never a score, never a transcript, never a draft.

``--purge`` refuses a still-published row and clears ``rag_ingested`` only on a
proven removal. **A failed purge is the one outcome here that is a privacy
defect**, because the guide is out of the app and still answerable in Chat, so it
gets a non-zero exit, an ERROR log and an audit row of its own, and the row keeps
saying ``rag_ingested = true`` rather than claiming a retraction that did not
happen. Read the note in ``guide.purge_guide_vectors`` before trusting a local
pass: off-GCP, ``delete_document_vectors`` returns True without proving anything.

## Durable traces

Same posture as ``smart_teams_score_job``, and for the same reason — a Cloud Run
Jobs launch validates that the JOB exists, not that the module inside it does, so
a launch of a missing module returns 200 and the endpoint returns 202 while
nothing ever happens. Every failure therefore leaves two traces that do not
depend on somebody watching a log tab:

1. a non-zero exit, which marks the execution FAILED; and
2. an ``audit_logs`` row, readable through
   ``GET /api/admin/audit?action=SMART_TEAMS_GUIDE_FAILED`` (or
   ``…_INGEST_FAILED`` / ``…_PURGE_FAILED``), keyed to the document id in
   ``target`` and to the person in ``actor_email``.

``actor_email`` names **the person whose guide is affected**, not an actor who
did something: it is the only field the admin view filters on, and burying the
one identifying detail in ``details`` makes the row unfindable for the question
anybody actually asks. ``details["job"]`` records who really wrote it. When the
row cannot be read at all, ``actor_email`` falls back to :data:`SYSTEM_ACTOR`,
which is deliberately not an email address so it can never be mistaken for a
person.

## Retries

``cubie-worker`` runs with ``--max-retries=1``, so a non-zero exit buys one more
attempt, and all three actions are safe to repeat. ``--generate`` overwrites the
same row's draft. ``--ingest`` uses ``RagManager.update_document``, which deletes
any ragFile already pointing at the target URI before importing — the whole
reason ``guide.rag_filename`` is keyed on the subject and not the version. A
repeated ``--purge`` on an already-purged row returns early and exits 0.

## Advisory only

What this job writes and publishes describes how somebody prefers to work, for
the purpose of working together better. It is never a measure of competence,
potential, or fitness for a role.
"""

from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("smart_teams_guide_job")

#: ``audit_logs.actor_email`` when the document row cannot be read, so its
#: subject is unknown. Not an email address on purpose.
SYSTEM_ACTOR = "smart-teams-guide-job"

#: Generation produced nothing. Somebody pressed Generate and has no guide.
ACTION_GENERATE_FAILED = "SMART_TEAMS_GUIDE_FAILED"
#: An approved guide is not in the companywide corpus, so Chat cannot use it.
#: Not a privacy problem — the app read works — but the feature is half-shipped.
ACTION_INGEST_FAILED = "SMART_TEAMS_GUIDE_INGEST_FAILED"
#: An UNPUBLISHED guide is still in the corpus. The privacy defect.
ACTION_PURGE_FAILED = "SMART_TEAMS_GUIDE_PURGE_FAILED"

#: A guide that rendered with no guidelines at all. Not a failure — it is the
#: honest shortfall document — but worth someone seeing, because the person has
#: completed enough interview to be scored and still cannot get a guide.
ACTION_EMPTY = "SMART_TEAMS_GUIDE_EMPTY"

EXIT_OK = 0
#: An unexpected error. A retry may well succeed.
EXIT_ERROR = 1
#: A refusal: unknown id, already published, not approved, no profile. Retrying
#: refuses identically; a person has to look at the row.
EXIT_REFUSED = 2


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="smart_teams_guide_job")
    p.add_argument(
        "--document-id",
        required=True,
        help="st_documents.id of the personal guide to act on.",
    )
    action = p.add_mutually_exclusive_group(required=True)
    # Mutually exclusive and required: there is no sensible default action, and
    # a bare run that picked one would be a guess at whether somebody meant to
    # publish a guide to the whole company or take one down.
    action.add_argument(
        "--generate",
        action="store_true",
        help="Build the prose into a pending row (pending -> ready|failed).",
    )
    action.add_argument(
        "--ingest",
        action="store_true",
        help="Put an approved, published guide into the companywide corpus.",
    )
    action.add_argument(
        "--purge",
        action="store_true",
        help="Remove an unpublished guide's chunks from the corpus.",
    )
    return p


def _record(
    document_id: str, action: str, details: dict, *, subject: str | None = None
) -> None:
    """Write the durable trace. Swallows its own failures, loudly.

    ``record_audit`` already logs at ERROR and rolls back if the insert fails, so
    the only thing left to guard is the import and the session — if the database
    is unreachable there is nothing to write to and the caller's log line is the
    whole record.
    """
    try:
        from backend.audit import record_audit
        from rubik_core.database.db import SessionLocal

        db = SessionLocal()
        try:
            record_audit(
                db,
                actor_email=subject or SYSTEM_ACTOR,
                action=action,
                status="failure",
                target=document_id,
                details={"job": SYSTEM_ACTOR, **details},
            )
        finally:
            db.close()
    except Exception:  # noqa: BLE001 — the trace must not become the failure
        logger.exception(
            "smart_teams_guide_job: could not record %s for document %s — the "
            "log line above is the only record of it",
            action,
            document_id,
        )


def _display_name(db, email: str) -> str:
    """The subject's name for the title, or their local part.

    Duplicated in shape from the router's helper rather than imported from it: a
    Cloud Run Job importing a FastAPI router pulls the whole app graph, and this
    is four lines of SQL. Folded compare because ``users.email`` is stored
    verbatim from the Google token.
    """
    from sqlalchemy import func

    from rubik_core.database.models import User

    folded = (email or "").strip().lower()
    row = (
        db.query(User)
        .filter(func.lower(User.email) == folded, User.is_platform_support.is_(False))
        .order_by(User.email)
        .first()
    )
    name = (getattr(row, "name", None) or "").strip() if row is not None else ""
    if name:
        return name
    return (folded.split("@", 1)[0] or folded).replace(".", " ").title() or folded


def _do_generate(db, document, guide_mod) -> int:
    """Build the prose into ``document``. Leaves ``failed`` on any error."""
    from rubik_core.database.smart_teams import SmartTeamsProfile

    subject = (document.subject_key or "").strip().lower()
    versions = document.profile_versions or {}
    wanted = versions.get(subject)
    profile = (
        db.query(SmartTeamsProfile)
        .filter(SmartTeamsProfile.user_email == subject)
        .filter(SmartTeamsProfile.version == wanted)
        .one_or_none()
    )
    if profile is None:
        # The row names a profile version that is not there. Not retryable, and
        # the row must not stay pending: the person is watching a spinner.
        message = (
            f"the profile version this guide was queued for (v{wanted}) is not "
            "in the database, so there is nothing to write from"
        )
        logger.error("smart_teams_guide_job: %s (document %s)", message, document.id)
        guide_mod.fail_row(db, document, message)
        db.commit()
        _record(
            document.id,
            ACTION_GENERATE_FAILED,
            {"error": message, "retryable": False, "profile_version": wanted},
            subject=subject,
        )
        return EXIT_REFUSED

    name = _display_name(db, subject)
    try:
        built = guide_mod.generate_into_row(
            db,
            document,
            profile,
            display_name=name,
            prose_writer=guide_mod.write_prose,
        )
    except guide_mod.GuideError as exc:
        # generate_into_row already stamped the row for the screen-failure path;
        # the published-row refusal has not, so stamp it here either way. Both
        # are deterministic — a retry refuses identically.
        logger.error(
            "smart_teams_guide_job: refusing to generate guide %s: %s",
            document.id,
            exc,
        )
        guide_mod.fail_row(db, document, str(exc))
        db.commit()
        _record(
            document.id,
            ACTION_GENERATE_FAILED,
            {"error": str(exc), "error_type": type(exc).__name__, "retryable": False},
            subject=subject,
        )
        return EXIT_REFUSED
    except Exception as exc:  # noqa: BLE001 — trace it, then fail the task
        logger.exception(
            "smart_teams_guide_job: generating guide %s failed", document.id
        )
        db.rollback()
        try:
            guide_mod.fail_row(db, document, f"{type(exc).__name__}: {exc}")
            db.commit()
        except Exception:  # noqa: BLE001 — the row may be unreachable too
            logger.exception(
                "smart_teams_guide_job: could not even mark guide %s failed",
                document.id,
            )
        _record(
            document.id,
            ACTION_GENERATE_FAILED,
            {"error": str(exc), "error_type": type(exc).__name__, "retryable": True},
            subject=subject,
        )
        return EXIT_ERROR

    db.commit()
    logger.info(
        "smart_teams_guide_job: guide %s for %s is ready — %d guideline(s) from %s",
        document.id,
        subject,
        len(built.guidelines),
        ", ".join(sorted({f for f, _ in built.sources})) or "no layer",
    )
    if not built.has_content:
        # The shortfall document. A valid, honest guide with no claims in it, and
        # exactly the thing somebody should look at: this person answered enough
        # to be scored and still no layer opened its gate.
        logger.warning(
            "smart_teams_guide_job: guide %s for %s has no guidelines — it is "
            "the honest shortfall document, not a failure",
            document.id,
            subject,
        )
        _record(
            document.id,
            ACTION_EMPTY,
            {
                "profile_version": wanted,
                "reason": "no reportable layer produced a guideline candidate",
            },
            subject=subject,
        )
    return EXIT_OK


def _do_ingest(db, document, guide_mod) -> int:
    """Put an approved, published guide into the companywide corpus."""
    subject = (document.subject_key or "").strip().lower()
    if document.rag_ingested and document.gcs_uri:
        # Already there. update_document would replace it with the same bytes;
        # skipping is cheaper and makes a retried task a no-op.
        logger.info(
            "smart_teams_guide_job: guide %s is already in the corpus (%s)",
            document.id,
            document.gcs_uri,
        )
        return EXIT_OK
    try:
        result = guide_mod.ingest_published_guide(
            db, document, display_name=_display_name(db, subject)
        )
    except guide_mod.GuideError as exc:
        logger.error(
            "smart_teams_guide_job: refusing to ingest guide %s: %s", document.id, exc
        )
        _record(
            document.id,
            ACTION_INGEST_FAILED,
            {"error": str(exc), "retryable": False},
            subject=subject,
        )
        return EXIT_REFUSED
    except Exception as exc:  # noqa: BLE001 — trace it, then fail the task
        logger.exception(
            "smart_teams_guide_job: ingesting guide %s failed", document.id
        )
        db.rollback()
        _record(
            document.id,
            ACTION_INGEST_FAILED,
            {"error": str(exc), "error_type": type(exc).__name__, "retryable": True},
            subject=subject,
        )
        return EXIT_ERROR
    db.commit()
    logger.info(
        "smart_teams_guide_job: guide %s ingested into %s (%s)",
        document.id,
        guide_mod.GUIDE_RAG_SLUG,
        result.get("gcs_uri"),
    )
    return EXIT_OK


def _do_purge(db, document, guide_mod) -> int:
    """Remove an unpublished guide's chunks. Non-zero unless they are gone."""
    subject = (document.subject_key or "").strip().lower()
    try:
        removed = guide_mod.purge_guide_vectors(db, document)
    except guide_mod.GuideError as exc:
        logger.error(
            "smart_teams_guide_job: refusing to purge guide %s: %s", document.id, exc
        )
        _record(
            document.id,
            ACTION_PURGE_FAILED,
            {"error": str(exc), "retryable": False},
            subject=subject,
        )
        return EXIT_REFUSED
    except Exception as exc:  # noqa: BLE001 — trace it, then fail the task
        logger.exception("smart_teams_guide_job: purging guide %s failed", document.id)
        db.rollback()
        _record(
            document.id,
            ACTION_PURGE_FAILED,
            {
                "error": str(exc),
                "error_type": type(exc).__name__,
                "retryable": True,
                "still_answerable_in_chat": True,
            },
            subject=subject,
        )
        return EXIT_ERROR

    db.commit()
    if removed:
        logger.info(
            "smart_teams_guide_job: guide %s purged from the corpus", document.id
        )
        return EXIT_OK

    # The privacy defect, stated as one. The row still says rag_ingested, which
    # is the truth, and this exits non-zero so the execution shows FAILED.
    logger.error(
        "smart_teams_guide_job: guide %s was NOT purged — it is unpublished in "
        "the app and STILL ANSWERABLE IN CHAT. Remove the ragFile by hand.",
        document.id,
    )
    _record(
        document.id,
        ACTION_PURGE_FAILED,
        {
            "error": "delete_document_vectors reported nothing removed",
            "retryable": True,
            "still_answerable_in_chat": True,
            "gcs_uri": document.gcs_uri,
        },
        subject=subject,
    )
    return EXIT_ERROR


def main(argv: list[str] | None = None) -> int:
    from backend.jobs.bootstrap import bootstrap_job_workspace

    if not bootstrap_job_workspace():
        return 2

    args = _build_parser().parse_args(argv)
    document_id = args.document_id
    action = "generate" if args.generate else "ingest" if args.ingest else "purge"

    # Deferred import: pulls the app graph (DB, settings, Vertex). Keeps --help
    # fast and surfaces an import failure inside the task, not at parse time.
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.smart_teams import SmartTeamsDocument
    from rubik_core.smart_teams import guide as guide_mod

    logger.info(
        "smart_teams_guide_job start document=%s action=%s", document_id, action
    )
    db = SessionLocal()
    try:
        document = db.get(SmartTeamsDocument, document_id)
        if document is None or document.kind != guide_mod.KIND_PERSONAL:
            logger.error(
                "smart_teams_guide_job: no personal guide row %s — nothing to "
                "%s. Retrying will not find it.",
                document_id,
                action,
            )
            _record(
                document_id,
                {
                    "generate": ACTION_GENERATE_FAILED,
                    "ingest": ACTION_INGEST_FAILED,
                    "purge": ACTION_PURGE_FAILED,
                }[action],
                {"error": "no such personal guide row", "retryable": False},
            )
            return EXIT_REFUSED

        if action == "generate":
            return _do_generate(db, document, guide_mod)
        if action == "ingest":
            return _do_ingest(db, document, guide_mod)
        return _do_purge(db, document, guide_mod)
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001 — teardown must not mask the result
            logger.debug("smart_teams_guide_job: session close failed", exc_info=True)
        # Separate process: flush the AI-cost ledger explicitly, since a
        # terminated job may skip the atexit handler. The prose call is the one
        # Vertex LLM call in a generate run and it is billed to "smart_teams";
        # dropping it here is how a feature's spend lands in the AI Cost tab's
        # "Unattributed" row.
        try:
            from rubik_core.cost.usage import flush_usage

            flush_usage()
        except Exception:  # noqa: BLE001 — cost accounting must never fail the job
            logger.debug("smart_teams_guide_job: cost flush failed", exc_info=True)


if __name__ == "__main__":
    sys.exit(main())

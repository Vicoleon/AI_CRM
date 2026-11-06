"""Cloud Run Job entrypoint for generating one pair working guide (ST-7).

    python -m backend.jobs.smart_teams_comparison_job --document-id <id> --generate

Launched by ``backend/routers/smart_teams_comparison.py``, which names this
module in ``COMPARISON_JOB_MODULE``.

Why this does not run in the web process
----------------------------------------
Cloud Run throttles the Service's CPU to near-zero the moment the HTTP response
returns, so a ``BackgroundTask`` here is starved silently. ``--generate`` makes
one Gemini Pro call for the prose, which is exactly the work that dies.

## One action, and why the flag is still required

``--generate`` is the only thing a pair guide has, because a pair guide is never
approved, never published, and never RAG-ingested — see
``comparison.approve_pair`` and ``comparison.NO_RAG_REASON`` for the reasoning on
each. So this module has no ``--ingest`` and no ``--purge``, and there is
deliberately nothing here that could put a document describing two people into a
companywide corpus.

The flag is nevertheless in a REQUIRED mutually-exclusive group rather than
implied. Two reasons: a bare execution then exits non-zero with a usage message
instead of guessing, which is the rule the other three Smart Teams jobs follow;
and if a second action is ever added, it lands next to an explicit ``--generate``
rather than silently changing what a bare run does. The sibling job's table entry
spells out why that matters most for this feature — guessing an action would act
on somebody's guide without being asked to.

## Durable traces

Same posture as ``smart_teams_guide_job``, and for the same reason: a Cloud Run
Jobs launch validates that the JOB exists, not that the module inside it does, so
a launch of a missing module returns 200 and the endpoint returns 202 while
nothing ever happens. Every failure therefore leaves two traces that do not
depend on somebody watching a log tab:

1. a non-zero exit, which marks the execution FAILED; and
2. an ``audit_logs`` row, readable through
   ``GET /api/admin/audit?action=SMART_TEAMS_PAIR_GUIDE_FAILED``, keyed to the
   document id in ``target``.

``actor_email`` names **the person the document is addressed to** — the first of
the two subjects, which is the reader — because that is the person watching a
spinner and the only field the admin view filters on. The other subject is in
``details["about"]``. When the row cannot be read at all, ``actor_email`` falls
back to :data:`SYSTEM_ACTOR`, which is deliberately not an email address so it
can never be mistaken for a person.

## Retries

``cubie-worker`` runs with ``--max-retries=1``, so a non-zero exit buys one more
attempt, and ``--generate`` is safe to repeat: it overwrites the same row's
draft, and the row is identified by a cache key that includes both profile
versions, so a retry cannot land on a different pair or a different version.

## Advisory only

What this job writes describes what two people said about how they prefer to
work, for the purpose of working together. It is never a measure of competence,
potential, or fitness for a role, and nothing it writes ranks either person
against the other.
"""

from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("smart_teams_comparison_job")

#: ``audit_logs.actor_email`` when the document row cannot be read, so its
#: subjects are unknown. Not an email address on purpose.
SYSTEM_ACTOR = "smart-teams-comparison-job"

#: Generation produced nothing. Somebody pressed Generate and has no guide.
ACTION_GENERATE_FAILED = "SMART_TEAMS_PAIR_GUIDE_FAILED"

#: A pair guide that rendered with nothing in it. Not a failure — it is the
#: honest shortfall document — but worth someone seeing, because both people have
#: completed enough interview to be scored AND published their own guides, and
#: still nothing could be said about the two of them together.
ACTION_EMPTY = "SMART_TEAMS_PAIR_GUIDE_EMPTY"

EXIT_OK = 0
#: An unexpected error. A retry may well succeed.
EXIT_ERROR = 1
#: A refusal: unknown id, wrong kind, a missing profile version. Retrying refuses
#: identically; a person has to look at the row.
EXIT_REFUSED = 2


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="smart_teams_comparison_job")
    p.add_argument(
        "--document-id",
        required=True,
        help="st_documents.id of the pair guide to generate.",
    )
    action = p.add_mutually_exclusive_group(required=True)
    action.add_argument(
        "--generate",
        action="store_true",
        help="Build the prose into a pending row (pending -> ready|failed).",
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
            "smart_teams_comparison_job: could not record %s for document %s — "
            "the log line above is the only record of it",
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


def _profiles_for(db, document, comparison_mod):
    """The two ``st_profiles`` rows this document was queued against, in order.

    Looked up by the EXACT versions in ``profile_versions``, never by
    ``is_current``. The row names the versions it was queued for and those are
    what its cache key is built from — regenerating against whatever is current
    now would produce a document whose content does not match its own key, which
    is the cache bug that made a retake serve stale words.

    Returns ``(reader_profile, about_profile)`` or ``None`` when either is
    missing.
    """
    from rubik_core.database.smart_teams import SmartTeamsProfile

    emails = [part for part in (document.subject_key or "").split("|") if part]
    if len(emails) != 2:
        return None
    versions = document.profile_versions or {}
    found = []
    for email in emails:
        wanted = versions.get(email)
        if wanted is None:
            return None
        profile = (
            db.query(SmartTeamsProfile)
            .filter(SmartTeamsProfile.user_email == email)
            .filter(SmartTeamsProfile.version == wanted)
            .one_or_none()
        )
        if profile is None:
            return None
        found.append(profile)
    return tuple(found)


def _do_generate(db, document, comparison_mod) -> int:
    """Build the prose into ``document``. Leaves ``failed`` on any error."""
    emails = [part for part in (document.subject_key or "").split("|") if part]
    reader = emails[0] if emails else None
    about = emails[1] if len(emails) > 1 else None

    profiles = _profiles_for(db, document, comparison_mod)
    if profiles is None:
        # The row names profile versions that are not there. Not retryable, and
        # the row must not stay pending: somebody is watching a spinner.
        message = (
            "the profile versions this pair guide was queued for "
            f"({document.profile_version_key!r}) are not both in the database, "
            "so there is nothing to write from"
        )
        logger.error(
            "smart_teams_comparison_job: %s (document %s)", message, document.id
        )
        comparison_mod.fail_row(db, document, message)
        db.commit()
        _record(
            document.id,
            ACTION_GENERATE_FAILED,
            {
                "error": message,
                "retryable": False,
                "profile_version_key": document.profile_version_key,
                "about": about,
            },
            subject=reader,
        )
        return EXIT_REFUSED

    profile_a, profile_b = profiles
    try:
        built = comparison_mod.generate_pair_into_row(
            db,
            document,
            profile_a,
            profile_b,
            name_a=_display_name(db, profile_a.user_email),
            name_b=_display_name(db, profile_b.user_email),
            prose_writer=comparison_mod.write_pair_prose,
        )
    except comparison_mod.ComparisonError as exc:
        # generate_pair_into_row already stamped the row for the screen-failure
        # path; the published-row refusal has not, so stamp it here either way.
        # Both are deterministic — a retry refuses identically.
        logger.error(
            "smart_teams_comparison_job: refusing to generate pair guide %s: %s",
            document.id,
            exc,
        )
        comparison_mod.fail_row(db, document, str(exc))
        db.commit()
        _record(
            document.id,
            ACTION_GENERATE_FAILED,
            {
                "error": str(exc),
                "error_type": type(exc).__name__,
                "retryable": False,
                "about": about,
            },
            subject=reader,
        )
        return EXIT_REFUSED
    except Exception as exc:  # noqa: BLE001 — trace it, then fail the task
        logger.exception(
            "smart_teams_comparison_job: generating pair guide %s failed",
            document.id,
        )
        db.rollback()
        try:
            comparison_mod.fail_row(db, document, f"{type(exc).__name__}: {exc}")
            db.commit()
        except Exception:  # noqa: BLE001 — the row may be unreachable too
            logger.exception(
                "smart_teams_comparison_job: could not even mark pair guide %s failed",
                document.id,
            )
        _record(
            document.id,
            ACTION_GENERATE_FAILED,
            {
                "error": str(exc),
                "error_type": type(exc).__name__,
                "retryable": True,
                "about": about,
            },
            subject=reader,
        )
        return EXIT_ERROR

    db.commit()
    logger.info(
        "smart_teams_comparison_job: pair guide %s (%s) is ready — %d overlap(s) "
        "and %d tension(s), %d model-written section(s)",
        document.id,
        document.subject_key,
        len(built.synergies),
        len(built.frictions),
        built.model_written_count,
    )
    if not built.has_content:
        # The shortfall document. Valid, honest, and exactly the thing somebody
        # should look at: both people are scored and both have published, and
        # still nothing could be said about the two of them.
        logger.warning(
            "smart_teams_comparison_job: pair guide %s has no overlaps and no "
            "tensions — it is the honest shortfall document, not a failure",
            document.id,
        )
        _record(
            document.id,
            ACTION_EMPTY,
            {
                "profile_version_key": document.profile_version_key,
                "about": about,
                "reason": (
                    "no dimension produced a claim from a framework reportable "
                    "for both people"
                ),
            },
            subject=reader,
        )
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    from backend.jobs.bootstrap import bootstrap_job_workspace

    if not bootstrap_job_workspace():
        return 2

    args = _build_parser().parse_args(argv)
    document_id = args.document_id

    # Deferred import: pulls the app graph (DB, settings, Vertex). Keeps --help
    # fast and surfaces an import failure inside the task, not at parse time.
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.smart_teams import SmartTeamsDocument
    from rubik_core.smart_teams import comparison as comparison_mod

    logger.info(
        "smart_teams_comparison_job start document=%s action=generate", document_id
    )
    db = SessionLocal()
    try:
        document = db.get(SmartTeamsDocument, document_id)
        if document is None or document.kind != comparison_mod.KIND_PAIR:
            logger.error(
                "smart_teams_comparison_job: no pair guide row %s — nothing to "
                "generate. Retrying will not find it.",
                document_id,
            )
            _record(
                document_id,
                ACTION_GENERATE_FAILED,
                {"error": "no such pair guide row", "retryable": False},
            )
            return EXIT_REFUSED
        return _do_generate(db, document, comparison_mod)
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001 — teardown must not mask the result
            logger.debug(
                "smart_teams_comparison_job: session close failed", exc_info=True
            )
        # Separate process: flush the AI-cost ledger explicitly, since a
        # terminated job may skip the atexit handler. The prose call is the one
        # Vertex LLM call in a run and it is billed to "smart_teams" by
        # ``comparison.write_pair_prose``'s ``@attribute_to``; dropping it here is
        # how a feature's spend lands in the AI Cost tab's "Unattributed" row.
        try:
            from rubik_core.cost.usage import flush_usage

            flush_usage()
        except Exception:  # noqa: BLE001 — cost accounting must never fail the job
            logger.debug("smart_teams_comparison_job: cost flush failed", exc_info=True)


if __name__ == "__main__":
    sys.exit(main())

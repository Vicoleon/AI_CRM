"""CLI entrypoint for the UC-42 priorities Cloud Run Job.

Invoked by the ``cubie-priorities-batch`` Cloud Run Job as:

    python -m rubik_core.priorities.batch_runner [--target-email EMAIL]

Cloud Run Jobs (unlike Services) run a one-shot container to completion
with a dedicated CPU budget and a timeout up to 24 h. They don't have
the request-tied CPU throttling or scale-to-zero-on-idle pathologies
that kept killing the in-Service BackgroundTask version (UC-40 → 0
HomeCards persisted because the instance died mid-batch).

Modes:

* ``--target-email <email>`` — run only for one user. Used for ad-hoc
  re-runs ("rebuild this one user's HomeCard for today").
* No flag — iterate every user that ``batch.list_active_users()``
  returns (UC-40.1 domain + department filter). Used for the
  scheduled daily pre-warm.

Exit code is 0 on a successful run (even if some individual users
failed — that's captured in the per-user logs). Exit code is non-zero
only on infrastructure failure (DB unreachable, etc.).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import uuid

logger = logging.getLogger(__name__)


def _deactivated_lookup(emails: list[str]) -> set[str]:
    """Offboarded-user resolver injected into ``list_active_users``.

    Kept as a thin, lazily-importing shim (not a module-level import) so this
    domain entrypoint stays importable by the Cloud Run Job without eagerly
    pulling in the backend Slack client. The detection itself is fail-open, so a
    Slack/Directory outage here just means the full roster is generated for.
    """
    from backend.services.directory_status import deactivated_priorities_emails

    return deactivated_priorities_emails(emails)


def _configure_logging() -> None:
    """Set up logging the same way the FastAPI service does.

    Cloud Run Jobs captures stdout/stderr per execution; the structured
    Cloud Logging integration picks up the standard ``logging`` module
    output automatically when ``LOG_LEVEL`` is honored.
    """
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="UC-42 priorities batch runner — Cloud Run Job entrypoint.",
    )
    parser.add_argument(
        "--target-email",
        type=str,
        default=None,
        help="Run for one user only (default: all eligible users).",
    )
    parser.add_argument(
        "--run-id",
        type=str,
        default=None,
        help=(
            "Optional run id for log correlation. Default: a fresh uuid4. "
            "Cloud Scheduler / the trigger endpoint should pass a known id."
        ),
    )
    parser.add_argument(
        "--per-user-timeout",
        type=int,
        default=None,
        help=(
            "Per-user wall-clock cap in seconds. Default: 600 (10 min). "
            "Override mainly for tests and one-off recovery runs."
        ),
    )
    parser.add_argument(
        "--report",
        choices=("daily", "next_week"),
        default="daily",
        help=(
            "Which report to generate. 'daily' (default) is the everyday "
            "briefing; 'next_week' is the Friday look-ahead over next week."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Cloud Run Job entrypoint.

    Returns the exit code (0 on success, 1 on infra/config failure).
    Per-user failures are reflected in the per-user log lines and the
    final summary, NOT in the exit code — Cloud Run treats a non-zero
    exit as a retry signal, and we don't want to retry the whole batch
    just because one user's HubSpot was down.
    """
    from backend.jobs.bootstrap import bootstrap_job_workspace

    if not bootstrap_job_workspace():
        return 2

    _configure_logging()

    parser = _build_parser()
    args = parser.parse_args(argv)

    # Defer the imports so ``--help`` is fast.
    from rubik_core.cost.usage import feature_context
    from rubik_core.priorities.batch import (
        PER_USER_TIMEOUT_SECONDS,
        list_active_users,
        run_batch,
    )

    run_id = args.run_id or str(uuid.uuid4())
    timeout = args.per_user_timeout or PER_USER_TIMEOUT_SECONDS

    if args.target_email:
        user_emails = [args.target_email]
        logger.info(
            "[uc42] single-user run target=%s run_id=%s report=%s",
            args.target_email,
            run_id,
            args.report,
        )
    else:
        try:
            # Inject the offboarded-user filter so generation itself skips
            # deactivated employees (Slack `deleted` / Google `suspended`) — the
            # roster this Job iterates is the one that was writing empty daily
            # bundles for ex-employees. Fail-open: detection errors fall back to
            # the full eligible roster inside list_active_users.
            user_emails = list_active_users(deactivated_lookup=_deactivated_lookup)
        except Exception:
            logger.exception("[uc42] list_active_users failed — exiting 1")
            return 1
        logger.info(
            "[uc42] all-users run run_id=%s users=%d report=%s",
            run_id,
            len(user_emails),
            args.report,
        )

    # ---- UC-42.1: Cloud Run task sharding ----------------------------------
    # Before this, the batch ran as a single sequential task (parallelism=1,
    # tasks=1) and its wall-clock was Σ(per-user) — with ~195s/user it crossed
    # the 3600s job timeout once the population passed ~18 users, silently
    # producing ZERO HomeCards for the whole company.
    #
    # Cloud Run Jobs inject CLOUD_RUN_TASK_INDEX / CLOUD_RUN_TASK_COUNT when the
    # job is deployed with --tasks/--parallelism > 1 (see deploy/jobs.yaml).
    # Each task now processes a disjoint
    # stride of the SORTED user list: sorting makes the partition deterministic
    # (list_active_users() returns unstable DB row order) and the ``i % count``
    # stride spreads slow users across shards. The union of all shards is
    # exactly list_active_users() — so the Slack-delivery superset invariant
    # (execs must be generated for, or they get an empty-bundle DM) is preserved.
    #
    # run_batch is unchanged: each shard runs the existing serial loop on its
    # slice. The single-user (--target-email) path is a 1-element list — shard 0
    # keeps it, the rest get an empty slice and exit 0 harmlessly. The Friday
    # --report next_week run flows through this same main() and inherits sharding.
    shard_idx = int(os.environ.get("CLOUD_RUN_TASK_INDEX", "0"))
    shard_count = int(os.environ.get("CLOUD_RUN_TASK_COUNT", "1"))
    if shard_count > 1:
        user_emails = [
            e for i, e in enumerate(sorted(user_emails)) if i % shard_count == shard_idx
        ]
        logger.info(
            "[uc42] shard %d/%d handling users=%d report=%s",
            shard_idx,
            shard_count,
            len(user_emails),
            args.report,
        )
    # ------------------------------------------------------------------------

    if not user_emails:
        logger.warning("[uc42] no eligible users — exiting 0")
        return 0

    # Attribute every LLM call this batch makes to "ai_priorities" in the
    # AI-Cost ledger. This is the scheduled Cloud Run Job entry point (distinct
    # from the in-service _run_priorities_for_user path); without this its spend
    # — the bulk of priorities cost — records as "unattributed". run_batch's
    # per-user orchestrator fans channel agents out via a ThreadPoolExecutor
    # that copies this context in (see priorities/orchestrator.py).
    with feature_context("ai_priorities"):
        summary = run_batch(
            run_id, user_emails, per_user_timeout=timeout, report_kind=args.report
        )
    logger.info(
        "[uc42] batch run complete run_id=%s users_total=%d "
        "succeeded=%d failed=%d duration=%.1fs",
        summary.run_id,
        summary.users_total,
        summary.users_succeeded,
        summary.users_failed,
        summary.total_duration_seconds,
    )

    # SOC 2 H-6 — dead-man's switch. The Job can exit 0 while producing no
    # usable briefings ("success-shaped failure"): a user opens Cubie to an
    # empty morning. Detect that here and emit a distinctive ERROR line that a
    # Cloud Logging log-based metric alerts on → the ops alert channel (see
    # scripts/setup_monitoring.sh, metric cubie_priorities_batch_degraded).
    # This replaces the paused ingestion-council quality gate with a simpler,
    # notification-only signal. Sharded: each task evaluates its own slice, so
    # an all-failed shard pages even if other shards succeeded.
    if summary.users_total > 0 and summary.users_succeeded == 0:
        logger.error(
            "[uc42] PRIORITIES_BATCH_DEGRADED run_id=%s produced 0 successful "
            "briefings out of %d users (all failed) — ops alert",
            summary.run_id,
            summary.users_total,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Shared library for the UC-40 priorities batch.

Lives in ``rubik_core.priorities`` (not ``backend.routers``) so that the
Cloud Run Job entrypoint (``rubik_core.priorities.batch_runner``) can
import it without dragging in FastAPI. The HTTP router in
``backend/routers/jobs.py`` re-exports these for API contracts.

UC-42: the batch no longer runs in-process on a Cloud Run Service. It
runs as a Cloud Run Job whose container entrypoint is
``python -m rubik_core.priorities.batch_runner``. This module holds the
pure-Python work the Job does; ``batch_runner`` adds the CLI shell.
"""

from __future__ import annotations

import concurrent.futures
import logging
from collections.abc import Callable, Collection
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel

from rubik_core.cost.usage import attribute_to
from rubik_core.tenancy.threads import ContextThreadPoolExecutor

logger = logging.getLogger(__name__)


class PrioritiesUserResult(BaseModel):
    """Outcome of running the priorities pipeline for one user."""

    user_email: str
    succeeded: bool
    priorities_count: int = 0
    duration_seconds: float = 0.0
    error: str | None = None


# UC-40.1: hard per-user timeout. One stuck user (Gmail retry hell, hung
# Vertex call, etc.) can't freeze the whole batch. 600s (10 min) is well
# above the 3–5 min typical pipeline cost.
PER_USER_TIMEOUT_SECONDS = 600

# How many items the Friday next-week look-ahead surfaces per user. Same as
# the daily default today; a separate knob so it can diverge without touching
# the daily path.
NEXT_WEEK_MAX_PRIORITIES = 5


@attribute_to("ai_priorities", user_arg="user_email")
def run_priorities_for_user(
    user_email: str, report_kind: str = "daily"
) -> PrioritiesUserResult:
    """Run the multi-agent priorities pipeline for one user and persist.

    Caught-and-logged exceptions so the batch can continue with the next
    user. Returns a typed result the caller can aggregate / log.

    The ``@attribute_to`` is deliberately on THIS function rather than only on
    the Job entrypoint. ``run_user_with_timeout`` below hands this work to a
    worker thread, and a contextvar set further up the stack does not survive
    that hop — which is exactly how the entire batch's spend (the bulk of all
    AI cost) once recorded as "unattributed". Setting the feature
    *inside* the function means it is re-established in whatever thread ends up
    running it, so no future fan-out can silently drop it again. ``user_arg``
    stamps ``user_email`` on the same calls, so AI Spend can be split by person
    and department (a Job has no request to take the user from).

    ``report_kind="next_week"`` runs the Friday look-ahead: the orchestrator
    widens its horizon to next week, and the card is persisted under
    ``NEXT_WEEK_CATEGORY`` with the target-week window in metadata.
    """
    from rubik_core.priorities import (
        GENERATION_EMPTY_WITH_SIGNALS,
        NEXT_WEEK_CATEGORY,
        PrioritiesOrchestrator,
        PrioritiesRequest,
        bundle_to_metadata,
        persist_bundle,
    )
    from rubik_core.priorities.temporal import (
        REPORT_KIND_NEXT_WEEK,
        resolve_user_now,
    )

    is_next_week = report_kind == REPORT_KIND_NEXT_WEEK
    start = datetime.now(timezone.utc)
    try:
        request = PrioritiesRequest(
            user_email=user_email,
            max_priorities=NEXT_WEEK_MAX_PRIORITIES if is_next_week else 5,
            report_kind=report_kind,
        )
        if not is_next_week:
            # Promise accountability: hear yesterday's promises and check the
            # ones whose date has passed BEFORE the briefing is written, so a
            # broken promise found tonight is on this morning's card. Checks
            # its own kill switch and roster, never raises, stops at its own
            # time budget, and books its model calls to its own AI Cost
            # category.
            from rubik_core.promises.accountability import run_accountability_pass

            run_accountability_pass(user_email)

        orchestrator = PrioritiesOrchestrator()
        bundle = orchestrator.generate(request)

        if getattr(bundle, "generation_status", None) == GENERATION_EMPTY_WITH_SIGNALS:
            # The decision stage produced nothing despite channel evidence. Persisting
            # this would deliver a blank briefing as if it were real. Fail the user
            # instead: no card is written, the batch retry pass gets another shot, Slack
            # delivery skips them LOUDLY (users_skipped_empty + Systems alert), and the
            # app lazy-regenerates when they open Cubie.
            duration = (datetime.now(timezone.utc) - start).total_seconds()
            logger.error(
                "[uc40] empty_with_signals for %s — bundle NOT persisted "
                "(decision stage returned 0 priorities despite signals)",
                user_email,
            )
            return PrioritiesUserResult(
                user_email=user_email,
                succeeded=False,
                duration_seconds=round(duration, 2),
                error="empty_with_signals: decision stage returned 0 "
                "priorities despite channel signals",
            )

        # Resolve "now" once — it supplies both the LOCAL date key (so an
        # overnight/Friday UTC run lands on the day the user reads it) and,
        # for next-week, the target-week window stored in metadata.
        ctx = resolve_user_now(user_email, report_kind=report_kind)
        if is_next_week:
            persist_bundle(
                user_email,
                ctx.today_str,
                bundle,
                category=NEXT_WEEK_CATEGORY,
                extra_metadata={
                    "report_kind": REPORT_KIND_NEXT_WEEK,
                    "week_start": ctx.week_start.isoformat()
                    if ctx.week_start
                    else None,
                    "week_end": ctx.week_end.isoformat() if ctx.week_end else None,
                },
            )
        else:
            persist_bundle(user_email, ctx.today_str, bundle)

        metadata = bundle_to_metadata(bundle)
        duration = (datetime.now(timezone.utc) - start).total_seconds()
        return PrioritiesUserResult(
            user_email=user_email,
            succeeded=True,
            priorities_count=len(metadata.get("priorities") or []),
            duration_seconds=round(duration, 2),
        )
    except Exception as exc:  # noqa: BLE001
        duration = (datetime.now(timezone.utc) - start).total_seconds()
        logger.exception(
            "[uc40] priorities pipeline failed for %s after %.1fs",
            user_email,
            duration,
        )
        return PrioritiesUserResult(
            user_email=user_email,
            succeeded=False,
            duration_seconds=round(duration, 2),
            error=f"{type(exc).__name__}: {exc}",
        )


def run_user_with_timeout(
    user_email: str,
    timeout_seconds: int = PER_USER_TIMEOUT_SECONDS,
    report_kind: str = "daily",
) -> PrioritiesUserResult:
    """Run one user's pipeline with a hard timeout.

    UC-40.1 — wraps ``run_priorities_for_user`` in a ``ThreadPoolExecutor``
    so we can cap wall-clock per user. ``cancel_futures`` doesn't kill
    running threads but does prevent the wrapper from blocking forever.

    The context-aware pool carries the workspace, AI-Cost attribution and
    request id into the worker thread.
    """
    with ContextThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run_priorities_for_user, user_email, report_kind)
        try:
            return future.result(timeout=timeout_seconds)
        except concurrent.futures.TimeoutError:
            logger.warning(
                "[uc40] per-user timeout (%ds) for %s — moving on",
                timeout_seconds,
                user_email,
            )
            return PrioritiesUserResult(
                user_email=user_email,
                succeeded=False,
                duration_seconds=float(timeout_seconds),
                error=f"timeout after {timeout_seconds}s",
            )


# Seed / admin-impersonation fixtures that must NEVER enter the priorities
# pipeline. No real person or Google Workspace mailbox is behind them, so
# generation writes no HomeCard and delivery would only ever DM into the void —
# surfacing them in the "Got nothing (no bundle for their local today)" ops
# alert. Covers the seed/example accounts (``boss@example.com``,
# ``user2@example.com``, ``orphan@example.com``, ``exec@example.com``), the
# local mock-auth identity (``dev@acme.example``, ``CUBIE_DEV_USER_EMAIL``) and
# the admin impersonation targets (``impersonated_user@sales.local``,
# ``…@systems.local``, ``…@compliance.local``).
_FIXTURE_EMAIL_DOMAINS = frozenset({"example.com", "example.org", "example.net"})
# Reserved top-level names (RFC 2606 ``.example``, mDNS ``.local``): no
# Workspace mailbox can live on one, whatever domain the install is set to.
_FIXTURE_EMAIL_TLDS = frozenset({"example", "local"})


def is_pipeline_fixture_email(email: str | None) -> bool:
    """True for seed/impersonation fixtures the priorities pipeline must skip.

    Matches any address on a ``*.local`` pseudo-domain (the admin impersonation
    targets), a ``*.example`` domain (the local mock-auth identity and other
    reserved fixtures) or an ``example.*`` domain (seed fixtures), plus any
    malformed address without an ``@``. HARD blocklist: applies regardless of
    the workspace's verified domain (a test workspace on ``acme.example`` still has
    no mailbox behind it to delegate into) and even on an explicitly-named
    targeted re-send.
    """
    e = (email or "").strip().lower()
    if "@" not in e:
        return True
    domain = e.rsplit("@", 1)[1]
    if domain in _FIXTURE_EMAIL_DOMAINS:
        return True
    return domain.rsplit(".", 1)[-1] in _FIXTURE_EMAIL_TLDS


def is_eligible_priorities_email(
    email: str | None, workspace_domain: str | Collection[str] | None
) -> bool:
    """Whether ``email`` may enter the AI-Priorities pipeline at all.

    Two guards compose — both must hold:

    * NOT a seed/impersonation fixture (``is_pipeline_fixture_email``) — always
      applied, even in local dev with no workspace domain configured.
    * When verified domains are set, the address is on one of them. The SA can
      only domain-wide-delegate into real Workspace mailboxes; an off-domain
      address produces ``invalid_grant`` on every Gmail call, and the batch
      hangs. An explicit empty domain collection fails closed. Only ``None``
      intentionally skips this guard for pure helper callers; production
      rosters pass the control-owned collection.

    Applied identically at every roster site — generation
    (:func:`priorities_roster`, which ``list_active_users`` and
    ``jobs._list_active_users`` read) and Slack delivery
    (``slack_delivery.deliver_slack_priorities``) — so the delivery roster stays
    a strict subset of the generated population. A recipient generation skipped
    but delivery kept is exactly the empty-bundle "Got nothing" noise this
    prevents. The overnight multi-source audience
    (``backend.agents.multi_source_priorities._audience``) and the Loose Ends
    scan (``loop_closure.roster``) apply it too.
    """
    e = (email or "").strip().lower()
    if not e or is_pipeline_fixture_email(e):
        return False
    raw_domains = (
        [workspace_domain]
        if isinstance(workspace_domain, str)
        else (workspace_domain or ())
    )
    domains = {domain.strip().lower() for domain in raw_domains if domain.strip()}
    if workspace_domain is not None and not domains:
        return False
    if domains and e.rsplit("@", 1)[-1] not in domains:
        return False
    return True


def slack_opted_department_slugs(db: Any) -> frozenset[str]:
    """RAG slugs of the departments opted into the Slack morning DM.

    ``departments.slack_priorities_enabled`` is the rollout control an admin
    flips per department. Matched by slug (``slugify_department``), never a raw
    or lower-cased compare: department names are free text, and "Sales Ops"
    and "sales-ops" are one team.
    """
    from rubik_core.database.models import Department
    from rubik_core.rag.config import slugify_department

    rows = db.query(Department.name).filter(
        Department.slack_priorities_enabled.is_(True)
    )
    return frozenset(
        slug for (name,) in rows if (slug := slugify_department(name or ""))
    )


def is_slack_priorities_recipient(user: Any, opted_slugs: frozenset[str]) -> bool:
    """Does the Slack morning DM go to ``user``?

    The Exec team (``is_exec``, any department or none) and everyone whose
    department is opted in (``slack_opted_department_slugs``), by slug.
    """
    if bool(getattr(user, "is_exec", False)):
        return True
    from rubik_core.rag.config import slugify_department

    slug = slugify_department(getattr(user, "department", None) or "")
    return bool(slug) and slug in opted_slugs


def priorities_roster(db: Any) -> list[str]:
    """Everyone the nightly batch generates for, before the leavers check.

    THE roster. Generation (``list_active_users``), the run-all trigger's count
    (``backend.routers.jobs._list_active_users``) and the Ingestion Council's
    "who should have a card?" checks (``ingestion_council.evidence`` and
    ``quality_gate``) all read it, so the council never counts someone the
    batch never meant to generate for.

    Every ``users`` row, then three guards:

    1. ``is_eligible_priorities_email`` — seed and admin-impersonation fixtures
       (``boss@example.com``, ``impersonated_user@sales.local``) are always out:
       no real Workspace mailbox is behind them. With a Workspace domain set,
       so is any address off it (the service account cannot reach that mailbox
       through Domain-Wide Delegation; every Gmail call fails ``invalid_grant``).
    2. Somebody reads the bundle. A person who has never signed in
       (``last_login IS NULL``) is left out, because a bundle nobody opens is
       spend with no reader, UNLESS the Slack morning DM goes to them
       (``is_slack_priorities_recipient``): that DM reads ONLY the card this
       batch writes, so skipping a recipient would turn their DM into an empty
       bundle. Never-signed-in is about having no reader, never about rank.
    3. The features. Only someone the registry admits to BOTH the copilot and
       ``ai_priorities`` (Admin → Feature Access) is read overnight: narrowing
       either is how an admin keeps a team's mailbox, Slack and Drive out of
       the batch and its spend off the AI Priorities line. A Feature Access
       read that fails admits nobody but the admin door
       (``rubik_core.features.policy``), so a database blip never widens it.

    No department rule. Departments are optional, so a person with none (or
    ``general``) is on the roster like anyone else, the same as the Loose Ends
    scan (``loop_closure.roster``) and the overnight multi-source audience.

    Sorted, so every reader gets the same order.
    """
    from rubik_core.company.domain import workspace_domains
    from rubik_core.database.models import User
    from rubik_core.features import feature_allows

    domains = workspace_domains()
    opted = slack_opted_department_slugs(db)
    roster: list[str] = []
    never_signed_in: list[str] = []
    narrowed_out: list[str] = []
    for user in db.query(User).filter(User.is_platform_support.is_(False)).all():
        email = user.email
        if not email or not is_eligible_priorities_email(email, domains):
            continue
        if user.last_login is None and not is_slack_priorities_recipient(user, opted):
            never_signed_in.append(email)
            continue
        if not (
            feature_allows("copilot", user, db)
            and feature_allows("ai_priorities", user, db)
        ):
            narrowed_out.append(email)
            continue
        roster.append(email)

    if never_signed_in:
        logger.info(
            "[roster] excluded %d never-signed-in user(s) from the priorities "
            "roster (no exec / Slack-opted-in exemption): %s",
            len(never_signed_in),
            sorted(never_signed_in),
        )
    if narrowed_out:
        logger.info(
            "[roster] excluded %d user(s) the copilot or AI Priorities roster "
            "leaves out (Admin → Feature Access): %s",
            len(narrowed_out),
            sorted(narrowed_out),
        )
    return sorted(roster)


def list_active_users(
    deactivated_lookup: Callable[[list[str]], set[str]] | None = None,
) -> list[str]:
    """The emails to generate AI Priorities for tonight.

    :func:`priorities_roster`, read on its own session, minus anyone who has
    left the company.

    ``deactivated_lookup``, when supplied, is called with the roster and
    returns the subset that belongs to **deactivated** employees (Slack
    ``deleted`` / Google ``suspended``); those are dropped so generation stops
    writing empty daily bundles for ex-employees. It is INJECTED (not
    imported) because the primary signal lives in the backend Slack client and
    this domain module must stay importable by the Cloud Run Job without
    pulling in backend/FastAPI; the caller (``batch_runner``) wires
    ``backend.services.directory_status.deactivated_priorities_emails`` in. A
    lookup that raises is swallowed — a detection outage must never block the
    whole batch (worst case: an ex-employee gets one more empty bundle).
    """
    from rubik_core.database.db import SessionLocal

    with SessionLocal() as db:
        eligible = priorities_roster(db)

    if deactivated_lookup is None:
        return eligible

    try:
        deactivated = {
            e.strip().lower() for e in (deactivated_lookup(eligible) or set())
        }
    except Exception:  # noqa: BLE001 — detection must never block the batch
        logger.warning(
            "[offboard] deactivated_lookup failed — generating for the full "
            "eligible roster",
            exc_info=True,
        )
        return eligible

    if not deactivated:
        return eligible

    kept = [e for e in eligible if e.strip().lower() not in deactivated]
    logger.info(
        "[offboard] excluded %d deactivated user(s) from the priorities roster: %s",
        len(eligible) - len(kept),
        sorted(deactivated & {e.strip().lower() for e in eligible}),
    )
    return kept


class BatchRunSummary(BaseModel):
    """End-of-run summary for a batch execution."""

    run_id: str
    users_total: int
    users_succeeded: int
    users_failed: int
    failures: list[PrioritiesUserResult] = []
    successes: list[PrioritiesUserResult] = []
    total_duration_seconds: float = 0.0


def run_batch(
    run_id: str,
    user_emails: list[str],
    per_user_timeout: int = PER_USER_TIMEOUT_SECONDS,
    report_kind: str = "daily",
) -> BatchRunSummary:
    """Run the priorities pipeline for every email in ``user_emails``.

    Per-user isolation: one user's failure does NOT stop the iteration.
    Per-user timeout: one user's hang does NOT block the next.

    Emits structured Cloud Logging entries:
      ``[uc40] run_id=<id> user=<email> succeeded=<bool> priorities=<n>
      duration=<s>[ error=<msg>]``  — one per user
      ``[uc40] batch complete run_id=<id> users=<n> succeeded=<n>
      failed=<n> total_duration=<s>``  — final
    """
    from rubik_core.telemetry import set_admin_context

    started_at = datetime.now(timezone.utc)
    logger.info(
        "[uc40] batch starting run_id=%s users=%d report=%s",
        run_id,
        len(user_emails),
        report_kind,
    )
    set_admin_context(True)

    results: list[PrioritiesUserResult] = []
    for email in user_emails:
        result = run_user_with_timeout(
            email, timeout_seconds=per_user_timeout, report_kind=report_kind
        )
        results.append(result)
        logger.info(
            "[uc40] run_id=%s user=%s succeeded=%s priorities=%d duration=%.1fs%s",
            run_id,
            result.user_email,
            result.succeeded,
            result.priorities_count,
            result.duration_seconds,
            f" error={result.error}" if result.error else "",
        )

    # One targeted retry for anyone who failed (LLM outage, empty-with-
    # signals). Transient Vertex 429s and truncated-JSON parses — the usual
    # causes — usually clear on a second attempt a few minutes
    # later, so a single retry converts most failures into briefings.
    # Guardrails: per-user timeouts are NOT retried (another 600s each, and a
    # hang rarely clears immediately), and a majority-failed shard skips the
    # pass entirely — that's a systemic outage where retrying doubles the
    # wall-clock towards the task timeout for nothing (the 7/9 shape).
    failed = [r for r in results if not r.succeeded]
    failed_emails = [
        r.user_email for r in failed if not (r.error or "").startswith("timeout after")
    ]
    if failed and len(failed) > len(results) // 2:
        logger.error(
            "[uc40] %d/%d users failed — systemic outage, skipping retry pass "
            "run_id=%s",
            len(failed),
            len(results),
            run_id,
        )
    elif failed_emails:
        logger.info(
            "[uc40] retry pass run_id=%s users=%d (%s)",
            run_id,
            len(failed_emails),
            ", ".join(failed_emails),
        )
        by_email = {r.user_email: r for r in results}
        for email in failed_emails:
            retry = run_user_with_timeout(
                email, timeout_seconds=per_user_timeout, report_kind=report_kind
            )
            logger.info(
                "[uc40] retry run_id=%s user=%s succeeded=%s priorities=%d "
                "duration=%.1fs%s",
                run_id,
                retry.user_email,
                retry.succeeded,
                retry.priorities_count,
                retry.duration_seconds,
                f" error={retry.error}" if retry.error else "",
            )
            if retry.succeeded:
                by_email[email] = retry
        results = [by_email[r.user_email] for r in results]

    completed_at = datetime.now(timezone.utc)
    succeeded = [r for r in results if r.succeeded]
    failed = [r for r in results if not r.succeeded]
    total_duration = (completed_at - started_at).total_seconds()

    logger.info(
        "[uc40] batch complete run_id=%s report=%s users=%d succeeded=%d failed=%d "
        "total_duration=%.1fs",
        run_id,
        report_kind,
        len(results),
        len(succeeded),
        len(failed),
        total_duration,
    )

    return BatchRunSummary(
        run_id=run_id,
        users_total=len(results),
        users_succeeded=len(succeeded),
        users_failed=len(failed),
        successes=succeeded,
        failures=failed,
        total_duration_seconds=round(total_duration, 2),
    )

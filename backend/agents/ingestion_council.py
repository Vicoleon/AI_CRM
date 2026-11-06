"""Ingestion Council — delivery + orchestration (API layer).

The pure council lives in ``rubik_core/ingestion_council`` (core: evidence,
auditors, chairman, report). This module wires it to the outside world — the
same core→delivery split the priorities engine uses (engine in
``rubik_core/priorities`` + Slack in ``backend/agents/slack_delivery.py``):

* run the council on a DB session,
* persist the verdict + rendered report,
* DM the ops team via the workspace Slack bot.

Invokable two ways, mirroring the overnight agent:

* ``POST /api/jobs/ingestion-council/run`` (Cloud Scheduler / admin), and
* ``python -m backend.agents.ingestion_council --scope nightly`` (manual/local).

Degrades gracefully: no bot token ⇒ verdict is still persisted, DMs are
skipped. A FAIL verdict is a *successful* run that found problems — it never
exits non-zero (so a Cloud Run Job doesn't retry it).
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from rubik_core.brand import PRODUCT_NAME
from rubik_core.company.operations import (
    OperationsConfigUnavailable,
    get_operations_config,
)
from rubik_core.database.models import Role, User
from rubik_core.ingestion_council import (
    build_slack_blocks,
    persist_verdict,
    render_html,
    run_council,
)
from rubik_core.ingestion_council.models import CouncilVerdict
from rubik_core.rag.config import slugify_department

logger = logging.getLogger(__name__)


def _registered_recipients(db: Session, emails: list[str]) -> list[str]:
    """Keep only real users of this tenant; support rows are never recipients."""
    if not emails:
        return []
    registered = {
        email.strip().casefold(): email
        for (email,) in db.query(User.email)
        .filter(User.is_platform_support.is_(False))
        .all()
        if email
    }
    return list(
        dict.fromkeys(
            registered[key]
            for email in emails
            if (key := email.strip().casefold()) in registered
        )
    )


@dataclass
class CouncilDeliveryResult:
    scope: str
    verdict_id: str | None = None
    overall_health: str = ""
    recipients: int = 0
    delivered: int = 0
    slack_available: bool = True
    errors: list[str] = field(default_factory=list)


def _recipient_emails(db: Session, override: list[str] | None) -> list[str]:
    """Who to DM: the ops audience for a technical signal, never the execs.

    Workspace-configured recipients take precedence over the configured
    department (matched by slug), then admins. An explicit override may only
    narrow that resolved audience, never add a report reader. A stale
    recipient or department falls back to admins; an unavailable policy refuses
    delivery. No department is special in code.
    """
    config = get_operations_config()

    def restrict(recipients: list[str]) -> list[str]:
        if override is None:
            return recipients
        requested = {email.casefold() for email in _registered_recipients(db, override)}
        return [email for email in recipients if email.casefold() in requested]

    if config.ingestion_council_recipients:
        recipients = _registered_recipients(
            db, list(config.ingestion_council_recipients)
        )
        if recipients:
            return restrict(recipients)
        logger.warning(
            "[ingestion-council] configured recipients name no workspace users; "
            "reporting to the workspace's admins instead."
        )
    named = config.ingestion_council_department
    department = slugify_department(named)
    if department:
        members = [
            u.email
            for u in db.query(User)
            .filter(User.is_platform_support.is_(False))
            .order_by(User.email)
            .all()
            if u.email and slugify_department(u.department or "") == department
        ]
        if members:
            return restrict(members)
        logger.warning(
            "[ingestion-council] configured department %r matches nobody; "
            "reporting to the workspace's admins instead.",
            named,
        )
    admins = (
        db.query(User.email)
        .filter(
            User.roles.any(Role.name == "admin"),
            User.is_platform_support.is_(False),
        )
        .order_by(User.email)
        .all()
    )
    return restrict([e for (e,) in admins if e])


def deliver_to_slack(
    db: Session,
    verdict: CouncilVerdict,
    report_url: str | None,
    emails: list[str] | None,
) -> CouncilDeliveryResult:
    """DM the verdict to the ops recipients. Never raises into the caller."""
    # Local import: keep the Slack service off the app-startup import path.
    from backend.services.slack_service import SlackAPIError, SlackBotClient

    result = CouncilDeliveryResult(
        scope=verdict.scope, overall_health=verdict.overall_health.value
    )
    try:
        recipients = _recipient_emails(db, emails)
    except OperationsConfigUnavailable:
        result.errors.append("Workspace operations policy unavailable")
        return result
    except Exception:
        logger.exception("[ingestion-council] workspace recipient lookup failed")
        result.errors.append("Workspace recipients unavailable")
        return result
    result.recipients = len(recipients)

    bot = SlackBotClient()
    if not bot.is_available:
        logger.warning(
            "[ingestion-council] no workspace bot token — verdict persisted, DMs "
            "skipped (install the %s Slack app).",
            PRODUCT_NAME,
        )
        result.slack_available = False
        return result
    if not recipients:
        logger.info(
            "[ingestion-council] no recipients (no admin or configured audience) — verdict "
            "persisted, no DMs."
        )
        return result

    fallback, blocks = build_slack_blocks(verdict, report_url)
    for email in recipients:
        try:
            sent = bot.send_dm(email, text=fallback, blocks=blocks)
        except SlackAPIError as e:
            if e.error == "invalid_blocks":
                try:
                    sent = bot.send_dm(email, text=fallback)  # degrade to text-only
                except SlackAPIError as e2:
                    result.errors.append(f"{email}: {e2.error}")
                    continue
            else:
                result.errors.append(f"{email}: {e.error}")
                continue
        if sent is None:
            continue  # recipient not in the Slack workspace — skip, not an error
        result.delivered += 1
    return result


def run_and_deliver(
    scope: str = "nightly",
    *,
    db: Session | None = None,
    emails: list[str] | None = None,
    deliver: bool = True,
) -> CouncilDeliveryResult:
    """Run the council for ``scope``, persist the verdict + report, and (unless
    ``deliver=False``) DM the ops team. Owns its session unless one is passed."""
    from rubik_core.database.db import SessionLocal

    own_session = db is None
    session = db or SessionLocal()
    try:
        if scope == "priorities-gate":
            # Phase-2 quality gate (observe-only). Mode off → no-op: nothing
            # persisted, nothing DM'd, delivery never affected either way.
            from rubik_core.ingestion_council.quality_gate import run_gate

            verdict = run_gate(session)
            if verdict is None:
                logger.info(
                    "[priorities-gate] CUBIE_PRIORITIES_GATE_MODE=off — skipped"
                )
                return CouncilDeliveryResult(scope=scope, overall_health="disabled")
        else:
            verdict = run_council(session, scope)
        from rubik_core.ingestion_council.persist import cached_verdict_visible

        if not cached_verdict_visible(verdict.to_dict()):
            logger.warning(
                "[ingestion-council] source proof unavailable; report withheld"
            )
            return CouncilDeliveryResult(scope=scope, overall_health="unavailable")
        report_html = render_html(verdict)
        row = persist_verdict(session, verdict, report_html)

        result = CouncilDeliveryResult(
            scope=scope,
            verdict_id=row.id,
            overall_health=verdict.overall_health.value,
        )
        if deliver:
            # No clickable report link: the DM is self-contained, and the HTML
            # report is served behind admin auth (a raw browser click wouldn't
            # carry a token). The verdict_id is logged + queryable via /runs.
            dr = deliver_to_slack(session, verdict, None, emails)
            result.recipients = dr.recipients
            result.delivered = dr.delivered
            result.slack_available = dr.slack_available
            result.errors = dr.errors

        logger.info(
            "[ingestion-council] complete scope=%s health=%s verdict_id=%s "
            "delivered=%d/%d slack=%s errors=%s",
            scope,
            result.overall_health,
            result.verdict_id,
            result.delivered,
            result.recipients,
            result.slack_available,
            result.errors,
        )
        return result
    finally:
        if own_session:
            session.close()


def main(argv: list[str] | None = None) -> int:
    from rubik_core.tenancy.cli import require_cli_workspace

    require_cli_workspace()

    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(
        description=f"Run the {PRODUCT_NAME} Ingestion Council."
    )
    parser.add_argument(
        "--scope",
        default="nightly",
        choices=["nightly", "weekly", "all", "priorities-gate"],
    )
    parser.add_argument(
        "--no-deliver", action="store_true", help="run + persist, but skip Slack DMs"
    )
    parser.add_argument(
        "--emails", default="", help="comma-separated recipient override"
    )
    args = parser.parse_args(argv)
    emails = [e.strip() for e in args.emails.split(",") if e.strip()] or None

    result = run_and_deliver(args.scope, emails=emails, deliver=not args.no_deliver)
    print(
        f"scope={result.scope} health={result.overall_health} "
        f"verdict_id={result.verdict_id} "
        f"delivered={result.delivered}/{result.recipients} errors={result.errors}"
    )
    # A FAIL verdict is a successful run that found problems — do not signal
    # failure to the job runner (which would retry).
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

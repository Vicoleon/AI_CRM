"""Who the morning scan reads — the ``loop_closure`` Feature Access roster.

Who is scanned is DATA (CLAUDE.md, "Feature gating"): an admin adds a person
or a department in Admin → Feature Access → Loose Ends, and the next morning's
scan reads that person's Gmail and Slack. The registry default is everyone
in the workspace, with ``admin_allowed`` and ``exec_allowed`` both False, so
once an admin narrows it to a team, an admin or an exec is never swept into a
mailbox scan by a flag they hold for other reasons.

Two guards on top of the roster, the same two the AI Priorities batch applies
to every mailbox it reads:

* ``is_eligible_priorities_email`` — no seed / impersonation fixtures, and
  nobody off the Workspace domain (Domain-Wide Delegation cannot reach them;
  every Gmail call would fail ``invalid_grant``).
* ``deactivated_priorities_emails`` — nobody Slack or Google says has left.
  Fails OPEN: a detection outage scans one ex-employee's empty mailbox one
  more morning, which is cheaper than scanning nobody.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable

from rubik_core.logsafe import safe_error
from rubik_core.loop_closure.constants import FEATURE_KEY

logger = logging.getLogger(__name__)


def _default_deactivated(emails: Iterable[str]) -> set[str]:
    # Imported here, not at module level: rubik_core must not import backend at
    # import time (the cubie-worker image imports this module without FastAPI).
    from backend.services.directory_status import deactivated_priorities_emails

    return deactivated_priorities_emails(list(emails))


def roster_emails(
    db,
    *,
    deactivated_lookup: Callable[[Iterable[str]], set[str]] | None = None,
) -> list[str]:
    """Every person the scan reads, lower-cased and sorted.

    All ``users`` rows (roles eager-loaded, because ``feature_allows`` judges an
    ORM row's admin flag through its roles) that ``feature_allows("loop_closure")``
    admits, filtered by the fixture / domain guard and minus anyone known to
    have left. ``deactivated_lookup`` is injectable for tests; the default asks
    Slack and Google.
    """
    from sqlalchemy.orm import selectinload

    from rubik_core.company.domain import workspace_domains
    from rubik_core.database.models import User
    from rubik_core.features import feature_allows
    from rubik_core.priorities.batch import is_eligible_priorities_email

    domains = workspace_domains()
    users = (
        db.query(User)
        .filter(User.is_platform_support.is_(False))
        .options(selectinload(User.roles))
        .all()
    )
    eligible: set[str] = set()
    for user in users:
        email = (user.email or "").strip().lower()
        if not email or not is_eligible_priorities_email(email, domains):
            continue
        try:
            if feature_allows(FEATURE_KEY, user, db):
                eligible.add(email)
        except Exception as exc:  # noqa: BLE001 — an unreadable gate is a closed gate
            logger.warning(
                "[loop-closure] roster gate failed for one user: %s", safe_error(exc)
            )
    roster = sorted(eligible)
    if not roster:
        return []

    lookup = deactivated_lookup or _default_deactivated
    try:
        gone = {(e or "").strip().lower() for e in (lookup(roster) or set())}
    except Exception as exc:  # noqa: BLE001 — detection must never empty the roster
        logger.warning(
            "[loop-closure] deactivated-user lookup failed; scanning the full "
            "roster: %s",
            safe_error(exc),
        )
        return roster
    if gone & set(roster):
        logger.info(
            "[loop-closure] %d deactivated user(s) left out of the scan",
            len(gone & set(roster)),
        )
    return [e for e in roster if e not in gone]

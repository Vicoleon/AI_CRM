"""Persist & query the teammate out-of-office table (``ooo_periods``).

The daily refresh job calls :func:`refresh_ooo_periods`, which reads each roster
user's calendar ONCE (via DWD) and materialises their upcoming OOO periods.
Chat / priorities / the manager digest then read the table with the cheap
queries below instead of impersonating every teammate's calendar per question.

Kept separate from :mod:`rubik_core.availability.pto` so that module's PURE
detection core stays free of any DB/ORM import.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from rubik_core.database.models import OOOPeriod

logger = logging.getLogger(__name__)


@dataclass
class RefreshResult:
    users_scanned: int = 0
    users_out: int = 0  # users with at least one upcoming OOO period
    periods_written: int = 0
    errors: int = 0

    def summary(self) -> str:
        return (
            f"scanned={self.users_scanned} out={self.users_out} "
            f"periods={self.periods_written} errors={self.errors}"
        )


def _roster_emails() -> list[str]:
    """The population we track OOO for — the canonical active-users roster.

    Reuses ``priorities.batch.list_active_users`` (everyone on the Workspace
    domain who reads a bundle, minus seed/impersonation fixtures) so the OOO
    table matches exactly who we generate priorities and deliver Slack DMs
    for. Manages its own DB session."""
    from rubik_core.priorities.batch import list_active_users

    return list_active_users()


def refresh_ooo_periods(
    db: Session, *, emails: list[str] | None = None
) -> RefreshResult:
    """Re-read each roster user's calendar (DWD) and materialise their upcoming
    OOO periods into ``ooo_periods``.

    Idempotent per user: a user's rows are replaced with the freshly detected
    set (delete-then-insert), so a cancelled or shifted vacation self-heals and
    the unique ``(user_email, start_date)`` constraint can never conflict.
    Best-effort per user — one failure is logged and skipped, never aborting the
    sweep. Commits once at the end.
    """
    from rubik_core.availability.pto import detect_pto
    from rubik_core.database.models import OOOPeriod
    from rubik_core.priorities.temporal import resolve_user_now

    targets = emails if emails is not None else _roster_emails()
    result = RefreshResult()
    now = datetime.utcnow()

    for email in targets:
        result.users_scanned += 1
        try:
            ctx = resolve_user_now(email)
            status = detect_pto(email, today=ctx.today, tz_name=ctx.tz_name)
        except Exception:  # noqa: BLE001 — one user's failure must not abort the sweep
            logger.warning(
                "[ooo-refresh] detection failed for %s", email, exc_info=True
            )
            result.errors += 1
            continue

        db.query(OOOPeriod).filter(OOOPeriod.user_email == email).delete()
        if status.upcoming:
            result.users_out += 1
        for p in status.upcoming:
            db.add(
                OOOPeriod(
                    user_email=email,
                    start_date=p.start.isoformat(),
                    end_date=p.end.isoformat(),
                    return_date=p.return_date.isoformat(),
                    synced_at=now,
                )
            )
            result.periods_written += 1

    db.commit()
    logger.info("[ooo-refresh] %s", result.summary())
    return result


def get_user_ooo_on(db: Session, email: str, on_date: date) -> OOOPeriod | None:
    """The OOO period covering ``on_date`` for ``email``, if any (else None)."""
    from rubik_core.database.models import OOOPeriod

    d = on_date.isoformat()
    return (
        db.query(OOOPeriod)
        .filter(OOOPeriod.user_email == email)
        .filter(OOOPeriod.start_date <= d)
        .filter(OOOPeriod.end_date >= d)
        .order_by(OOOPeriod.start_date)
        .first()
    )


def get_user_next_ooo(db: Session, email: str, on_or_after: date) -> OOOPeriod | None:
    """The user's next OOO period ending on/after ``on_or_after`` (else None)."""
    from rubik_core.database.models import OOOPeriod

    d = on_or_after.isoformat()
    return (
        db.query(OOOPeriod)
        .filter(OOOPeriod.user_email == email)
        .filter(OOOPeriod.end_date >= d)
        .order_by(OOOPeriod.start_date)
        .first()
    )

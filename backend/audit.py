"""Shared audit-log helper.

SOC 2 CC7.2 / CC4.1: privileged actions (role/permission changes, user
deprovisioning, workspace-auth config changes, data-plane admin ops) must leave
a durable, attributable trail. `AuditLog` already existed but was only written
for a few email/calendar/drive/DAM actions via a helper private to email.py;
this promotes a single reusable writer so any router can record one row per
sensitive action with before/after context.

Ordinary ``record_audit`` writes are best-effort: a failure is logged and
swallowed to preserve existing behavior. Platform support mutations have a
different contract: ``begin_support_audit`` commits an intent before the
handler and ``finish_support_audit`` commits its outcome afterward. Both raise
on failure, and a failed outcome update leaves the durable pending intent.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from rubik_core.database.db import SessionLocal
from rubik_core.database.models import AuditLog
from rubik_core.tenancy.context import current_workspace

logger = logging.getLogger(__name__)


class SupportAuditError(RuntimeError):
    """A platform support write cannot proceed without a durable audit trail."""


def begin_support_audit(
    *, actor: str, sid: str, method: str, path: str, request_id: str
) -> int:
    """Commit an intent in the selected target DB before handler execution.

    This is deliberately separate from best-effort ``record_audit`` and from
    a handler's transaction. Failure propagates, so the middleware refuses the
    write before a router or dependency can mutate anything.
    """
    details = {
        "workspace": current_workspace(),
        "sid": sid,
        "method": method,
        "path": path[:1024],
        "request_id": request_id,
        "outcome": "pending",
    }
    try:
        with SessionLocal() as db:
            row = AuditLog(
                actor_email=actor,
                action="PLATFORM_SUPPORT_REQUEST",
                status="pending",
                details=details,
            )
            db.add(row)
            db.flush()
            audit_id = row.id
            db.commit()
            return audit_id
    except Exception as exc:
        raise SupportAuditError("Could not record platform support intent") from exc


def finish_support_audit(
    audit_id: int, *, outcome: str, status_code: int | None
) -> None:
    """Commit the result; a failed update leaves the prior pending intent."""
    if outcome not in ("success", "failure"):
        raise ValueError("Unsupported support audit outcome")
    try:
        with SessionLocal() as db:
            row = db.get(AuditLog, audit_id)
            if row is None or row.action != "PLATFORM_SUPPORT_REQUEST":
                raise SupportAuditError("Platform support intent is missing")
            if row.status != "pending":
                raise SupportAuditError("Platform support intent was already finished")
            row.status = outcome
            row.details = {
                **(row.details or {}),
                "outcome": outcome,
                "status_code": status_code,
            }
            db.commit()
    except Exception as exc:
        raise SupportAuditError("Could not finish platform support audit") from exc


def record_audit(
    db: Session,
    *,
    actor_email: str,
    action: str,
    status: str = "success",
    target: str | None = None,
    details: dict | None = None,
) -> None:
    """Write one AuditLog row.

    Args:
        actor_email: the authenticated user performing the action.
        action: UPPER_SNAKE verb, e.g. ``USER_ROLE_UPDATE``, ``USER_DELETE``.
        status: ``success`` or ``failure``.
        target: the object acted on (e.g. the affected user's email, a role name).
        details: arbitrary JSON context — prefer ``{"before": ..., "after": ...}``
            for change events.
    """
    logger.info(
        "AuditLog: %s -> %s (%s) target=%s", actor_email, action, status, target
    )
    try:
        db.add(
            AuditLog(
                actor_email=actor_email,
                action=action,
                status=status,
                doc_id=target,
                details=details or {},
            )
        )
        db.commit()
    except Exception as e:  # noqa: BLE001 — auditing must never break the action
        logger.error("Failed to write AuditLog (%s) for %s: %s", action, actor_email, e)
        db.rollback()

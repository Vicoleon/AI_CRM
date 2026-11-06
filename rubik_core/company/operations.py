"""Typed operational policy in the current workspace's tenant database.

One ``workspace_settings`` JSON row holds policy that belongs to the company,
not the server process. Every read resolves a workspace-bound session. Missing
settings are safe and empty; malformed or unavailable settings raise instead of
silently widening an audience or starting old email deliveries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields
from datetime import date, datetime
from typing import Any

from sqlalchemy import select, text

from rubik_core.database.db import SessionLocal
from rubik_core.database.models import AuditLog, WorkspaceSetting
from rubik_core.tenancy.context import current_workspace

SETTINGS_KEY = "workspace_operations"
_EMAIL = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\Z"
)
_DATE_OR_DATETIME = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?"
    r"(?:Z|[+-]\d{2}:\d{2})?)?\Z"
)
_PLATFORM_FIELDS = frozenset(
    {"impersonation_allowed_emails", "user_impersonation_targets"}
)


class OperationsConfigInvalid(ValueError):
    """An attempted operational policy edit is invalid or unauthorized."""


class OperationsConfigUnavailable(RuntimeError):
    """The current workspace's saved policy cannot be trusted or read."""


def _text(value: object, label: str, *, limit: int = 200) -> str:
    if not isinstance(value, str):
        raise OperationsConfigInvalid(f"{label} must be text")
    clean = value.strip()
    if len(clean) > limit or any(ord(char) < 32 or ord(char) == 127 for char in clean):
        raise OperationsConfigInvalid(f"Invalid {label}")
    return clean


def _email(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise OperationsConfigInvalid(f"{label} must be an email address")
    clean = value.strip().casefold()
    if len(clean) > 254 or not _EMAIL.fullmatch(clean):
        raise OperationsConfigInvalid(f"Invalid {label} email address")
    return clean


def _optional_email(value: object, label: str) -> str:
    if value is None or value == "":
        return ""
    return _email(value, label)


def _emails(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise OperationsConfigInvalid(f"{label} must be an email list")
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        email = _email(item, label)
        if email in seen:
            raise OperationsConfigInvalid(f"{label} has a duplicate email")
        seen.add(email)
        out.append(email)
    return tuple(out)


def _start_floor(value: object) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not _DATE_OR_DATETIME.fullmatch(value):
        raise OperationsConfigInvalid("l10_start_after must be an ISO date or datetime")
    try:
        if "T" in value:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise OperationsConfigInvalid("Invalid l10_start_after date") from exc


@dataclass(frozen=True, slots=True)
class OperationsConfig:
    loop_closure_scorecard_metric: str = ""
    ingestion_council_recipients: tuple[str, ...] = ()
    ingestion_council_department: str = ""
    insights_excluded_emails: tuple[str, ...] = ()
    impersonation_allowed_emails: tuple[str, ...] = ()
    user_impersonation_targets: tuple[str, ...] = ()
    deep_prep_sender_name: str = ""
    deep_prep_sender_mailbox: str = ""
    l10_sender_mailbox: str = ""
    l10_start_after: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "loop_closure_scorecard_metric",
            "ingestion_council_department",
            "deep_prep_sender_name",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        for name in (
            "ingestion_council_recipients",
            "insights_excluded_emails",
            "impersonation_allowed_emails",
            "user_impersonation_targets",
        ):
            object.__setattr__(self, name, _emails(getattr(self, name), name))
        for name in ("deep_prep_sender_mailbox", "l10_sender_mailbox"):
            object.__setattr__(self, name, _optional_email(getattr(self, name), name))
        object.__setattr__(self, "l10_start_after", _start_floor(self.l10_start_after))

    @classmethod
    def from_mapping(cls, raw: object) -> OperationsConfig:
        if not isinstance(raw, dict):
            raise OperationsConfigInvalid(
                "Workspace operations policy must be an object"
            )
        names = {item.name for item in fields(cls)}
        return cls(**{name: value for name, value in raw.items() if name in names})

    def to_dict(self) -> dict[str, Any]:
        return {
            item.name: list(value) if isinstance(value, tuple) else value
            for item in fields(self)
            for value in (getattr(self, item.name),)
        }


_CONFIG_FIELDS = frozenset(item.name for item in fields(OperationsConfig))


def get_operations_config() -> OperationsConfig:
    """Read the selected workspace's policy; never use process settings."""
    current_workspace()
    try:
        with SessionLocal() as db:
            row = db.get(WorkspaceSetting, SETTINGS_KEY)
            return OperationsConfig.from_mapping({} if row is None else row.value)
    except Exception as exc:
        raise OperationsConfigUnavailable(
            "Workspace operations policy unavailable"
        ) from exc


def patch_operations_config(
    changes: dict[str, Any],
    *,
    platform_authorized: bool = False,
    audit_actor: str | None = None,
    audit_sid: str | None = None,
) -> OperationsConfig:
    """Apply one atomic partial edit while preserving unrelated/unknown keys.

    The caller must establish platform authority before passing the explicit
    flag for impersonation fields. A workspace admin may edit the other fields
    but cannot grant themselves access to another person's view.
    """
    if not isinstance(changes, dict) or any(
        key not in _CONFIG_FIELDS for key in changes
    ):
        raise OperationsConfigInvalid("Unknown workspace operations field")
    if _PLATFORM_FIELDS.intersection(changes) and not platform_authorized:
        raise OperationsConfigInvalid(
            "Platform authority required for impersonation policy"
        )
    platform_edit = bool(_PLATFORM_FIELDS.intersection(changes))
    if platform_edit:
        if not audit_actor or not audit_sid:
            raise OperationsConfigInvalid(
                "Audited support session required for impersonation policy"
            )
        audit_actor = _email(audit_actor, "platform actor")
        audit_sid = _text(audit_sid, "support session")
        if not audit_sid:
            raise OperationsConfigInvalid("Support session is required")
    slug = current_workspace()
    try:
        with SessionLocal() as db:
            dialect = db.get_bind().dialect.name
            if dialect == "sqlite":
                # SQLite ignores FOR UPDATE. Lock before the first read/insert.
                db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            elif dialect == "postgresql":
                # The row may not exist yet. A transaction advisory lock protects
                # the first insert as well as updates on PostgreSQL.
                db.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                    {"key": f"{slug}:{SETTINGS_KEY}"},
                )
            row = db.scalar(
                select(WorkspaceSetting)
                .where(WorkspaceSetting.key == SETTINGS_KEY)
                .with_for_update()
            )
            raw = {} if row is None else row.value
            try:
                current = OperationsConfig.from_mapping(raw)
            except OperationsConfigInvalid as exc:
                raise OperationsConfigUnavailable(
                    "Stored operations policy is malformed"
                ) from exc
            updated = OperationsConfig.from_mapping({**current.to_dict(), **changes})
            value = {**raw, **updated.to_dict()}
            if row is None:
                db.add(WorkspaceSetting(key=SETTINGS_KEY, value=value))
            else:
                row.value = value
            if platform_edit:
                changed_fields = sorted(_PLATFORM_FIELDS.intersection(changes))
                db.add(
                    AuditLog(
                        actor_email=audit_actor,
                        action="IMPERSONATION_POLICY_UPDATE",
                        status="success",
                        details={
                            "workspace": slug,
                            "sid": audit_sid,
                            "before": {
                                key: current.to_dict()[key] for key in changed_fields
                            },
                            "after": {
                                key: updated.to_dict()[key] for key in changed_fields
                            },
                        },
                    )
                )
            db.commit()
            return updated
    except (OperationsConfigInvalid, OperationsConfigUnavailable):
        raise
    except Exception as exc:
        raise OperationsConfigUnavailable(
            "Workspace operations policy unavailable"
        ) from exc

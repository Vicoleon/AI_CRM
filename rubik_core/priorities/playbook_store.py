"""Admin-managed playbook store for the UC-23 / UC-24 pipeline.

CRUD + ``resolve_effective(department)`` helpers backed by the
``priority_playbook`` table. UC-24's decision-maker calls
:func:`resolve_effective` at run time and injects the concatenated
body into its system prompt.

Resolution rule: a user in department ``D`` sees, in this order,

  1. All active playbooks where ``department = D`` (sorted by
     ``sort_order`` asc, then ``created_at`` asc as tie-breaker).
  2. All active playbooks where ``department IS NULL`` (defaults
     for everyone), sorted the same way.

If neither set returns rows the resolver returns an empty string —
UC-24 then falls back to a generic note in the decision-maker prompt.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import asc

from rubik_core.database.db import SessionLocal
from rubik_core.database.models import PriorityPlaybook

logger = logging.getLogger(__name__)


@dataclass
class PlaybookRow:
    id: str
    name: str
    department: str | None
    body: str
    is_active: bool
    sort_order: int
    created_by: str
    updated_by: str
    created_at: str
    updated_at: str

    @classmethod
    def from_orm(cls, row: PriorityPlaybook) -> PlaybookRow:
        return cls(
            id=row.id,
            name=row.name,
            department=row.department,
            body=row.body,
            is_active=bool(row.is_active),
            sort_order=int(row.sort_order or 0),
            created_by=row.created_by,
            updated_by=row.updated_by,
            created_at=row.created_at.isoformat() if row.created_at else "",
            updated_at=row.updated_at.isoformat() if row.updated_at else "",
        )

    def to_dict(self, *, body_preview_chars: int | None = None) -> dict[str, Any]:
        payload = {
            "id": self.id,
            "name": self.name,
            "department": self.department,
            "body": self.body,
            "is_active": self.is_active,
            "sort_order": self.sort_order,
            "created_by": self.created_by,
            "updated_by": self.updated_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if body_preview_chars is not None:
            preview = (self.body or "")[:body_preview_chars]
            payload["body_preview"] = preview
        return payload


@dataclass
class EffectivePlaybook:
    department: str | None
    playbooks: list[PlaybookRow] = field(default_factory=list)

    @property
    def concatenated_text(self) -> str:
        """Final string injected into the decision-maker system prompt."""
        chunks: list[str] = []
        for pb in self.playbooks:
            label = (
                f"## {pb.name} ({pb.department})"
                if pb.department
                else f"## {pb.name} (default)"
            )
            chunks.append(f"{label}\n\n{pb.body.strip()}")
        return "\n\n".join(chunks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "department": self.department,
            "playbooks": [
                {"id": p.id, "name": p.name, "sort_order": p.sort_order, "body": p.body}
                for p in self.playbooks
            ],
            "concatenated_text": self.concatenated_text,
        }


# ── CRUD ─────────────────────────────────────────────────────────────────────


def list_playbooks(
    *, department: str | None = "__any__", include_inactive: bool = True
) -> list[PlaybookRow]:
    """Return all playbooks, optionally filtered by department.

    ``department='__any__'`` (the default) means "don't filter on
    department"; ``department=None`` means "only default-everyone rows".
    """
    with SessionLocal() as db:
        q = db.query(PriorityPlaybook)
        if department != "__any__":
            q = q.filter(PriorityPlaybook.department == department)
        if not include_inactive:
            q = q.filter(PriorityPlaybook.is_active.is_(True))
        q = q.order_by(
            asc(PriorityPlaybook.sort_order),
            asc(PriorityPlaybook.created_at),
        )
        return [PlaybookRow.from_orm(r) for r in q.all()]


def get_playbook(playbook_id: str) -> PlaybookRow | None:
    with SessionLocal() as db:
        row = (
            db.query(PriorityPlaybook)
            .filter(PriorityPlaybook.id == playbook_id)
            .first()
        )
        return PlaybookRow.from_orm(row) if row else None


def create_playbook(
    *,
    name: str,
    body: str,
    department: str | None,
    actor_email: str,
    is_active: bool = True,
    sort_order: int = 100,
) -> PlaybookRow:
    now = datetime.utcnow()
    with SessionLocal() as db:
        row = PriorityPlaybook(
            id=str(uuid.uuid4()),
            name=name,
            department=department,
            body=body,
            is_active=is_active,
            sort_order=sort_order,
            created_by=actor_email,
            updated_by=actor_email,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return PlaybookRow.from_orm(row)


def update_playbook(
    playbook_id: str,
    *,
    actor_email: str,
    name: str | None = None,
    body: str | None = None,
    department: str | None = "__unchanged__",
    is_active: bool | None = None,
    sort_order: int | None = None,
) -> PlaybookRow | None:
    """Partial update. ``department='__unchanged__'`` (the default) leaves
    the column alone; pass ``None`` explicitly to clear it."""
    with SessionLocal() as db:
        row = (
            db.query(PriorityPlaybook)
            .filter(PriorityPlaybook.id == playbook_id)
            .first()
        )
        if row is None:
            return None
        if name is not None:
            row.name = name
        if body is not None:
            row.body = body
        if department != "__unchanged__":
            row.department = department
        if is_active is not None:
            row.is_active = is_active
        if sort_order is not None:
            row.sort_order = sort_order
        row.updated_by = actor_email
        row.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(row)
        return PlaybookRow.from_orm(row)


def delete_playbook(playbook_id: str) -> bool:
    with SessionLocal() as db:
        row = (
            db.query(PriorityPlaybook)
            .filter(PriorityPlaybook.id == playbook_id)
            .first()
        )
        if row is None:
            return False
        db.delete(row)
        db.commit()
        return True


# ── Effective resolution (UC-24 consumer) ────────────────────────────────────


def resolve_effective(department: str | None) -> EffectivePlaybook:
    """Return the playbook stack the decision-maker should see for a user
    in ``department``. Returns an :class:`EffectivePlaybook` with an
    ordered list of rows + the concatenated string ready to inject.
    """
    with SessionLocal() as db:
        # Dept-specific rows first.
        dept_rows: list[PriorityPlaybook] = []
        if department:
            dept_rows = (
                db.query(PriorityPlaybook)
                .filter(PriorityPlaybook.department == department)
                .filter(PriorityPlaybook.is_active.is_(True))
                .order_by(
                    asc(PriorityPlaybook.sort_order),
                    asc(PriorityPlaybook.created_at),
                )
                .all()
            )

        # Default-for-everyone rows always follow.
        default_rows = (
            db.query(PriorityPlaybook)
            .filter(PriorityPlaybook.department.is_(None))
            .filter(PriorityPlaybook.is_active.is_(True))
            .order_by(
                asc(PriorityPlaybook.sort_order),
                asc(PriorityPlaybook.created_at),
            )
            .all()
        )

    ordered = [PlaybookRow.from_orm(r) for r in dept_rows + default_rows]
    return EffectivePlaybook(department=department, playbooks=ordered)

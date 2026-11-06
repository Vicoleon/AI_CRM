"""Admin-managed store for per-department shared Drive folders.

CRUD + :func:`list_for_user` backed by the ``department_drive_folder`` table.
Modeled on :mod:`rubik_core.priorities.playbook_store`.

Resolution rule (what the Productivity page shows a user in department ``D``):

  1. All active folders where ``department = D`` (sorted by ``sort_order`` asc,
     then ``folder_name`` asc as a stable tie-breaker).
  2. Followed by all active folders where ``department IS NULL`` (company-wide),
     sorted the same way.

A NULL ``department`` row is the "company-wide" folder that shows for everyone —
mirrors the PriorityPlaybook default-row convention.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import asc

from rubik_core.database.db import SessionLocal
from rubik_core.database.models import DepartmentDriveFolder

logger = logging.getLogger(__name__)


@dataclass
class FolderRow:
    id: str
    department: str | None
    folder_id: str
    folder_name: str
    web_view_link: str | None
    sort_order: int
    is_active: bool
    created_by: str
    updated_by: str
    created_at: str
    updated_at: str

    @classmethod
    def from_orm(cls, row: DepartmentDriveFolder) -> FolderRow:
        return cls(
            id=row.id,
            department=row.department,
            folder_id=row.folder_id,
            folder_name=row.folder_name,
            web_view_link=row.web_view_link,
            sort_order=int(row.sort_order or 0),
            is_active=bool(row.is_active),
            created_by=row.created_by,
            updated_by=row.updated_by,
            created_at=row.created_at.isoformat() if row.created_at else "",
            updated_at=row.updated_at.isoformat() if row.updated_at else "",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "department": self.department,
            "folder_id": self.folder_id,
            "folder_name": self.folder_name,
            "web_view_link": self.web_view_link,
            "sort_order": self.sort_order,
            "is_active": self.is_active,
            "created_by": self.created_by,
            "updated_by": self.updated_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


# ── CRUD (admin) ──────────────────────────────────────────────────────────────


def list_folders(
    *, department: str | None = "__any__", include_inactive: bool = True
) -> list[FolderRow]:
    """Return all folders, optionally filtered by department.

    ``department='__any__'`` (default) means "don't filter"; ``department=None``
    means "only company-wide rows".
    """
    with SessionLocal() as db:
        q = db.query(DepartmentDriveFolder)
        if department != "__any__":
            q = q.filter(DepartmentDriveFolder.department == department)
        if not include_inactive:
            q = q.filter(DepartmentDriveFolder.is_active.is_(True))
        q = q.order_by(
            asc(DepartmentDriveFolder.sort_order),
            asc(DepartmentDriveFolder.folder_name),
        )
        return [FolderRow.from_orm(r) for r in q.all()]


def get_folder(folder_row_id: str) -> FolderRow | None:
    with SessionLocal() as db:
        row = (
            db.query(DepartmentDriveFolder)
            .filter(DepartmentDriveFolder.id == folder_row_id)
            .first()
        )
        return FolderRow.from_orm(row) if row else None


def create_folder(
    *,
    folder_id: str,
    folder_name: str,
    department: str | None,
    actor_email: str,
    web_view_link: str | None = None,
    is_active: bool = True,
    sort_order: int = 100,
) -> FolderRow:
    now = datetime.utcnow()
    with SessionLocal() as db:
        row = DepartmentDriveFolder(
            id=str(uuid.uuid4()),
            department=department,
            folder_id=folder_id,
            folder_name=folder_name,
            web_view_link=web_view_link,
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
        return FolderRow.from_orm(row)


def update_folder(
    folder_row_id: str,
    *,
    actor_email: str,
    folder_id: str | None = None,
    folder_name: str | None = None,
    web_view_link: str | None = "__unchanged__",
    department: str | None = "__unchanged__",
    is_active: bool | None = None,
    sort_order: int | None = None,
) -> FolderRow | None:
    """Partial update. Sentinels ``'__unchanged__'`` leave a nullable column
    alone; pass ``None`` explicitly to clear it."""
    with SessionLocal() as db:
        row = (
            db.query(DepartmentDriveFolder)
            .filter(DepartmentDriveFolder.id == folder_row_id)
            .first()
        )
        if row is None:
            return None
        if folder_id is not None:
            row.folder_id = folder_id
        if folder_name is not None:
            row.folder_name = folder_name
        if web_view_link != "__unchanged__":
            row.web_view_link = web_view_link
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
        return FolderRow.from_orm(row)


def delete_folder(folder_row_id: str) -> bool:
    with SessionLocal() as db:
        row = (
            db.query(DepartmentDriveFolder)
            .filter(DepartmentDriveFolder.id == folder_row_id)
            .first()
        )
        if row is None:
            return False
        db.delete(row)
        db.commit()
        return True


# ── Read for the Productivity surface ─────────────────────────────────────────


def list_for_user(department: str | None) -> list[FolderRow]:
    """Active folders a user in ``department`` should see: their department's
    rows first, then company-wide (NULL-department) rows. Both ordered by
    ``sort_order`` then name."""
    with SessionLocal() as db:
        dept_rows: list[DepartmentDriveFolder] = []
        if department:
            dept_rows = (
                db.query(DepartmentDriveFolder)
                .filter(DepartmentDriveFolder.department == department)
                .filter(DepartmentDriveFolder.is_active.is_(True))
                .order_by(
                    asc(DepartmentDriveFolder.sort_order),
                    asc(DepartmentDriveFolder.folder_name),
                )
                .all()
            )
        company_rows = (
            db.query(DepartmentDriveFolder)
            .filter(DepartmentDriveFolder.department.is_(None))
            .filter(DepartmentDriveFolder.is_active.is_(True))
            .order_by(
                asc(DepartmentDriveFolder.sort_order),
                asc(DepartmentDriveFolder.folder_name),
            )
            .all()
        )
    return [FolderRow.from_orm(r) for r in dept_rows + company_rows]

"""Base Pydantic schemas for the EOS subdomain.

`AuditBase` mirrors `rubik_core.database.eos._base.AuditMixin` for
response serialization. `BaseCreate` / `BaseUpdate` are intentional
empty bases — they exist so per-domain schemas can extend a consistent
hierarchy and so static analysis can tell create-vs-update apart at
call sites.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class AuditBase(BaseModel):
    """Audit fields surfaced on every EOS response."""

    id: UUID
    created_at: datetime
    updated_at: datetime
    created_by: str | None = None
    # Who made the most recent write (mirrors AuditMixin.updated_by).
    updated_by: str | None = None
    is_deleted: bool = False

    model_config = ConfigDict(from_attributes=True)


class BaseCreate(BaseModel):
    """Marker base for create payloads."""


class BaseUpdate(BaseModel):
    """Marker base for partial-update payloads (all fields optional)."""

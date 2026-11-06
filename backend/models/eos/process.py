"""Core process / FBA document schemas.

Upstream is `CoreProcess`; our ORM class is `EosProcess` (renamed to
avoid `Process` colliding with stdlib `multiprocessing.Process`). Schema
names use the `Process*` prefix since they're already namespaced inside
`backend/models/eos/`.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel

from .base import AuditBase, BaseCreate, BaseUpdate


class ProcessBase(BaseModel):
    name: str
    owner_id: UUID | None = None
    documentation_url: str | None = None


class ProcessCreate(ProcessBase, BaseCreate):
    pass


class ProcessUpdate(BaseUpdate):
    name: str | None = None
    owner_id: UUID | None = None
    documentation_url: str | None = None


class ProcessResponse(ProcessBase, AuditBase):
    pass

"""Organization schemas (read/create/update)."""

from __future__ import annotations

from pydantic import BaseModel

from .base import AuditBase, BaseCreate, BaseUpdate


class OrganizationBase(BaseModel):
    name: str
    description: str | None = None


class OrganizationCreate(OrganizationBase, BaseCreate):
    pass


class OrganizationUpdate(BaseUpdate):
    name: str | None = None
    description: str | None = None


class OrganizationResponse(OrganizationBase, AuditBase):
    pass

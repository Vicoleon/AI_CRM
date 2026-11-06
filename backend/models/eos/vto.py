"""VTO version + section schemas.

A VTOVersion is the parent envelope (named draft / published version
of the organization's VTO); a VTOSection is one slot within it
(vision, mission, 10-year-target, etc.). `content` is free-form JSON
that the frontend renders per section_type.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel

from rubik_core.database.eos import VTOSectionType

from .base import AuditBase, BaseCreate, BaseUpdate

# -- VTOVersion -------------------------------------------------------


class VTOVersionBase(BaseModel):
    organization_id: UUID
    version_name: str
    is_active: bool = False
    status: str = "draft"  # draft | published | archived


class VTOVersionCreate(VTOVersionBase, BaseCreate):
    pass


class VTOVersionUpdate(BaseUpdate):
    version_name: str | None = None
    is_active: bool | None = None
    status: str | None = None


class VTOVersionResponse(VTOVersionBase, AuditBase):
    pass


# -- VTOSection -------------------------------------------------------


class VTOSectionBase(BaseModel):
    vto_version_id: UUID
    section_type: VTOSectionType
    content: Any = None  # section-specific structured payload


class VTOSectionCreate(VTOSectionBase, BaseCreate):
    pass


class VTOSectionUpdate(BaseUpdate):
    content: Any = None


class VTOSectionResponse(VTOSectionBase, AuditBase):
    pass

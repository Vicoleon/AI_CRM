"""VTO (Vision/Traction Organizer).

Versioned: a VTOVersion is the parent of N VTOSections. Only one version
per organization is `is_active=True` at a time — enforced in application
code today; a partial unique index can be added later if collisions appear.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import JSON, Boolean, Column, Enum, ForeignKey, String
from sqlalchemy.orm import relationship

from rubik_core.database.models import Base

from ._base import AuditMixin, VTOSectionType


class VTOVersion(Base, AuditMixin):
    __tablename__ = "eos_vto_versions"

    organization_id = Column(
        sa.Uuid, ForeignKey("eos_organizations.id"), nullable=False
    )
    version_name = Column(String, nullable=False)
    is_active = Column(Boolean, default=False, nullable=False)
    status = Column(String, default="draft", nullable=False)  # draft|published|archived

    sections = relationship(
        "VTOSection",
        back_populates="vto_version",
        cascade="all, delete-orphan",
    )


class VTOSection(Base, AuditMixin):
    __tablename__ = "eos_vto_sections"

    vto_version_id = Column(
        sa.Uuid,
        ForeignKey("eos_vto_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    section_type = Column(
        Enum(
            VTOSectionType,
            name="eos_vto_section_type",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
    )
    content = Column(JSON)

    vto_version = relationship("VTOVersion", back_populates="sections")

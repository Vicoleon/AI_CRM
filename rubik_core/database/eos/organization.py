"""EOS organization.

Single-tenant per deployment: the seed script inserts one row. Departments are
**not** modelled here — they reuse Cubie's existing `departments` table
(`rubik_core.database.models.Department`) via a String FK on
`departments.name`. See docs/ENGINEERING_NOTES.md.
"""

from __future__ import annotations

from sqlalchemy import Column, String, Text

from rubik_core.database.models import Base

from ._base import AuditMixin


class Organization(Base, AuditMixin):
    __tablename__ = "eos_organizations"

    name = Column(String, nullable=False)
    description = Column(Text)

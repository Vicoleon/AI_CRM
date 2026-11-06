"""Core processes / FBA documents.

Class is `EosProcess` (not `Process`) to avoid name collision with stdlib
multiprocessing and any future Cubie-side concept.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import Column, ForeignKey, String

from rubik_core.database.models import Base

from ._base import AuditMixin


class EosProcess(Base, AuditMixin):
    __tablename__ = "eos_processes"

    name = Column(String, nullable=False)
    owner_id = Column(sa.Uuid, ForeignKey("eos_people.id"), nullable=True)
    documentation_url = Column(String)

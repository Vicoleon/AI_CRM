"""Quarters — the time-window grouping for Rocks."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String
from sqlalchemy.orm import relationship

from rubik_core.database.models import Base

from ._base import AuditMixin


class Quarter(Base, AuditMixin):
    __tablename__ = "eos_quarters"

    organization_id = Column(
        sa.Uuid, ForeignKey("eos_organizations.id"), nullable=False
    )
    name = Column(String, nullable=False)
    start_date = Column(DateTime, nullable=False)
    end_date = Column(DateTime, nullable=False)
    is_current = Column(Boolean, default=False, nullable=False)

    rocks = relationship("Rock", back_populates="quarter")

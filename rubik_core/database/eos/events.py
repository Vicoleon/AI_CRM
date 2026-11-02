"""EOS event log (audit trail).

Does NOT inherit AuditMixin — audit logs aren't themselves audited.
Class is `EosEvent` to distinguish from Cubie's existing `AuditLog`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import JSON, Column, DateTime, String

from rubik_core.database.models import Base


class EosEvent(Base):
    __tablename__ = "eos_events"

    id = Column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    resource_type = Column(String, nullable=False)  # e.g. "rock", "issue"
    resource_id = Column(sa.Uuid, nullable=True)
    action = Column(String, nullable=False)  # e.g. "created", "updated", "deleted"
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)
    actor = Column(String, nullable=False)  # user email
    payload = Column(JSON)

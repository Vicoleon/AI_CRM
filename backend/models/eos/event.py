"""EOS event log response schema (read-only)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class EosEventResponse(BaseModel):
    id: UUID
    resource_type: str
    resource_id: UUID | None = None
    action: str
    timestamp: datetime
    actor: str
    payload: Any | None = None

    model_config = ConfigDict(from_attributes=True)

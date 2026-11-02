from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class ChatMessageResponse(BaseModel):
    id: str
    role: str
    content: str
    metadata_json: dict[str, Any] | None = None
    created_at: datetime

    class Config:
        from_attributes = True


class ChatSessionResponse(BaseModel):
    id: str
    user_email: str
    title: str
    folder_id: str | None = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ChatSessionDetailResponse(ChatSessionResponse):
    messages: list[ChatMessageResponse] = []


class ChatFolderResponse(BaseModel):
    id: str
    name: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

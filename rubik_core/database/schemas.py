from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class AssetMetadata(BaseModel):
    doc_id: str
    owner: str
    approver: str | None = None
    # Uploads land "approved" (live in the RAG). "archived" = removed from the
    # vector store but kept as a record; "failed" = ingestion errored.
    status: str = "approved"
    topic: str | None = None
    department: str | None = None
    description: str | None = None
    tags: list[str] = []
    original_filename: str | None = None
    source_path: str | None = None
    is_company_wide: bool = False
    allowed_emails: list[str] = []
    content_hash: str | None = None
    processing: bool = False
    # Which revision the row's source_path/content_hash describe.
    current_version: int = 1
    created_at: datetime
    updated_at: datetime


class AssetVersionInfo(BaseModel):
    """One row of an asset's version history (``asset_versions``)."""

    doc_id: str
    version_number: int
    is_current: bool = False
    source_path: str | None = None
    original_filename: str | None = None
    content_hash: str | None = None
    size_bytes: int | None = None
    uploaded_by: str | None = None
    note: str | None = None
    change_summary: str | None = None
    restored_from: int | None = None
    created_at: datetime


# Sentinel stored inside ``managed_departments`` meaning "every department".
# Lets an org-wide Exec (e.g. the CEO) read across all teams without a schema
# migration — it rides on the existing JSON list. Read-only: it never confers
# admin-panel access or write powers (those stay on the ``admin`` role).
ALL_DEPARTMENTS = "*"


class UserProfile(BaseModel):
    id: str
    email: str
    name: str
    picture: str | None = None
    department: str = "general"
    roles: list[str] = ["user"]
    created_at: str
    updated_at: str
    last_login: str | None = None
    last_ingest: str | None = None
    workspace_groups: list[str] = []
    groups: list[str] = []  # Local group names
    # True when the user is on the Exec team — gates VTO writes.
    is_exec: bool = False
    is_platform_support: bool = False
    # Departments this user is the team owner / integrator of (EOS
    # Accountability Chart). Used to expand the user's meeting-scope
    # options beyond their primary ``department``. Regular users leave
    # this empty.
    managed_departments: list[str] = []

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles

    @property
    def sees_all_departments(self) -> bool:
        """Org-wide Exec (e.g. the CEO): on the Exec team AND flagged
        "All departments" via the ``managed_departments`` wildcard.

        Grants cross-department READ across chat, documents, transcripts
        and EOS — but never admin-panel access or write powers (those
        stay on the ``admin`` role). False for a scoped exec or non-exec.
        """
        return self.is_exec and ALL_DEPARTMENTS in (self.managed_departments or [])


class Topic(BaseModel):
    name: str
    created_at: datetime | None = None


class DriveLabelPriority(BaseModel):
    id: str | None = None
    label_key: str | None = None
    label_value: str
    priority: float = 1.0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class Department(BaseModel):
    name: str
    created_at: datetime | None = None


class Group(BaseModel):
    id: str
    name: str
    description: str | None = None
    created_at: datetime | None = None


class AccessPolicy(BaseModel):
    id: int | None = None
    user_email: str | None = None
    group_id: str | None = None
    department: str | None = None
    topic: str | None = None
    can_read: bool = True
    can_update: bool = False
    created_at: datetime | None = None


class Notification(BaseModel):
    """V2 notifications response shape.

    ``kind`` is deliberately an open string, not a ``Literal``. This is a
    RESPONSE model, so a closed set means one row written by a new producer
    fails validation and 500s the WHOLE feed — which is how both
    ``scheduled_task_result`` and ``asset_shared`` silently broke this
    endpoint: they ship in code that writes rows, but were never added here,
    and nothing read the feed to notice. Notifications are "a courtesy, never
    a contract"; the bell must degrade to showing an unfamiliar row, never to
    showing nothing.

    Kinds in use today: ``agent_recommendation``, ``deal_stage_change``,
    ``ingest_failure``, ``doc_approval_request``, ``scheduled_task_result``,
    ``asset_shared``, ``meeting_prep_ready``, ``meeting_prep_failed``
    (rubik_core/meeting_prep/background.py). Keep the mute list in
    ``Settings.tsx`` (``NOTIFICATION_KINDS``) in step when adding one.
    """

    id: str
    kind: str
    title: str
    body: str
    severity: Literal["info", "warn", "urgent"]
    link: str | None
    created_at: datetime
    read_at: datetime | None

    class Config:
        from_attributes = True


# ── V2 Saved & Recent ─────────────────────────────────────────────────
# For V2_1 the bookmark row carries no display metadata — the title /
# subtitle / link fields are emitted as placeholders by the router and
# will be denormalized in a later PR (each domain — Library, Deals,
# Meetings, Contacts — populates its own resolver). Keeping the response
# shape stable now means the frontend can ship against the contract
# immediately.

# Mirrors the storage column. The schema is permissive — we accept the
# `chat_message` kind so the bookmark UI can write the row, but no
# read-side ``/api/chat/messages/{id}`` surface exists in V2_1.
SavedTargetKind = Literal[
    "asset",
    "chat_session",
    "chat_message",
    "deal",
    "meeting",
    "contact",
]

# Recent does NOT include ``chat_message`` per the spec — clicking
# through to a single message in the middle of a thread isn't a useful
# "re-enter" affordance, sessions are.
RecentTargetKind = Literal[
    "asset",
    "chat_session",
    "deal",
    "meeting",
    "contact",
]


class SavedItem(BaseModel):
    """One row of the user's bookmark list, hydrated for the UI."""

    id: str
    target_kind: SavedTargetKind
    target_id: str
    # V2_1: placeholder values; denormalization lands per-domain.
    title: str
    subtitle: str | None = None
    saved_at: datetime
    link: str


class RecentItem(BaseModel):
    """One row of the user's recent-touch feed.

    Unlike ``SavedItem``, ``title`` and ``link`` are real values: the
    frontend supplies them at touch time so the Recent page can render
    without a join."""

    id: str
    target_kind: RecentTargetKind
    target_id: str
    title: str
    link: str
    touched_at: datetime


class SavedCreateRequest(BaseModel):
    target_kind: SavedTargetKind
    target_id: str


class RecentTouchRequest(BaseModel):
    target_kind: RecentTargetKind
    target_id: str
    title: str
    link: str

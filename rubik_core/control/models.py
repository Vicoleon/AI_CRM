"""Control schema. Credentials are references; workspace content lives elsewhere."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, validates


class Base(DeclarativeBase):
    pass


def normalize(value: str) -> str:
    return value.strip().lower()


@dataclass(frozen=True)
class WorkspaceConnectionSpec:
    """Vetted pending DB identity for migration before workspace registration.

    The database registry resolves the password reference. A caller cannot
    redirect this contract to an arbitrary server or pass a plaintext password.
    """

    db_name: str
    db_user: str
    db_password_secret_ref: str

    def __post_init__(self) -> None:
        for value in (self.db_name, self.db_user):
            if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", value):
                raise ValueError(
                    "Database names/users must be lowercase SQL identifiers"
                )
        if not self.db_password_secret_ref.strip():
            raise ValueError("A database password secret reference is required")


class WorkspaceRow:
    """Normalize the workspace key on all child rows."""

    @validates("workspace_slug")
    def _normalize_workspace_slug(self, _key: str, value: str) -> str:
        return normalize(value)


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        CheckConstraint(
            "slug = lower(trim(slug)) AND length(slug) > 0",
            name="ck_workspace_slug_normalized",
        ),
        CheckConstraint(
            "status IN ('active', 'suspended', 'provisioning')",
            name="ck_workspace_status",
        ),
    )

    slug: Mapped[str] = mapped_column(String(63), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(24), default="active")
    db_name: Mapped[str | None] = mapped_column(String(63))
    db_user: Mapped[str | None] = mapped_column(String(63))
    db_password_secret_ref: Mapped[str | None] = mapped_column(String(512))
    time_zone: Mapped[str] = mapped_column(String(100), default="UTC")
    storage_prefix: Mapped[str | None] = mapped_column(String(255))
    company_details: Mapped[dict] = mapped_column(JSON, default=dict)
    branding: Mapped[dict] = mapped_column(JSON, default=dict)
    google_service_account: Mapped[str | None] = mapped_column(String(320))
    google_subject: Mapped[str | None] = mapped_column(String(320))
    corpus_ids: Mapped[dict] = mapped_column(JSON, default=dict)
    schema_revision: Mapped[str | None] = mapped_column(String(32))
    freeze_windows: Mapped[list] = mapped_column(JSON, default=list)

    @validates("slug")
    def _normalize_slug(self, _key: str, value: str) -> str:
        return normalize(value)

    @validates("google_service_account", "google_subject")
    def _normalize_google_email(self, _key: str, value: str | None) -> str | None:
        return normalize(value) if value else value


class WorkspaceDomain(WorkspaceRow, Base):
    __tablename__ = "workspace_domains"
    __table_args__ = (
        CheckConstraint(
            "domain = lower(trim(domain)) AND length(domain) > 0",
            name="ck_workspace_domain_normalized",
        ),
        Index(
            "uq_workspace_domains_primary",
            "workspace_slug",
            unique=True,
            sqlite_where=text("is_primary = 1"),
            postgresql_where=text("is_primary"),
        ),
    )

    domain: Mapped[str] = mapped_column(String(253), primary_key=True)
    workspace_slug: Mapped[str] = mapped_column(
        ForeignKey("workspaces.slug"), index=True
    )
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)

    @validates("domain")
    def _normalize_domain(self, _key: str, value: str) -> str:
        return normalize(value)


class WorkspaceModule(WorkspaceRow, Base):
    __tablename__ = "workspace_modules"

    workspace_slug: Mapped[str] = mapped_column(
        ForeignKey("workspaces.slug"), primary_key=True
    )
    module: Mapped[str] = mapped_column(String(100), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    @validates("module")
    def _normalize_module(self, _key: str, value: str) -> str:
        return normalize(value)


class WorkspaceCorpus(WorkspaceRow, Base):
    __tablename__ = "workspace_corpora"

    workspace_slug: Mapped[str] = mapped_column(
        ForeignKey("workspaces.slug"), primary_key=True
    )
    kind: Mapped[str] = mapped_column(String(100), primary_key=True)
    audience: Mapped[str] = mapped_column(String(320), primary_key=True)
    corpus_id: Mapped[str] = mapped_column(String(512))


class WorkspaceConnector(WorkspaceRow, Base):
    __tablename__ = "workspace_connectors"

    workspace_slug: Mapped[str] = mapped_column(
        ForeignKey("workspaces.slug"), primary_key=True
    )
    connector: Mapped[str] = mapped_column(String(100), primary_key=True)
    status: Mapped[str] = mapped_column(String(24), default="disconnected")
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    secret_refs: Mapped[dict] = mapped_column(JSON, default=dict)
    connected_by: Mapped[str | None] = mapped_column(String(320))
    last_check: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @validates("connected_by")
    def _normalize_email(self, _key: str, value: str | None) -> str | None:
        return normalize(value) if value else value


class SlackInstallation(WorkspaceRow, Base):
    __tablename__ = "slack_installations"
    __table_args__ = (
        Index("ux_slack_installations_app_id", "app_id", unique=True),
        Index("ux_slack_installations_team_id", "team_id", unique=True),
    )

    app_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    team_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    workspace_slug: Mapped[str] = mapped_column(
        ForeignKey("workspaces.slug"), index=True
    )
    signing_secret_ref: Mapped[str] = mapped_column(String(512))

    @validates("app_id", "team_id")
    def _normalize_slack_id(self, _key: str, value: str) -> str:
        # Slack IDs are opaque and case-sensitive, unlike email/domain keys.
        return value.strip()


class PlatformAdmin(Base):
    __tablename__ = "platform_admins"
    __table_args__ = (
        CheckConstraint(
            "email = lower(trim(email)) AND length(email) > 0",
            name="ck_platform_admin_email_normalized",
        ),
    )

    email: Mapped[str] = mapped_column(String(320), primary_key=True)
    home_workspace_slug: Mapped[str] = mapped_column(
        ForeignKey("workspaces.slug"), index=True
    )

    @validates("email", "home_workspace_slug")
    def _normalize_key(self, _key: str, value: str) -> str:
        return normalize(value)


class SupportSession(Base):
    __tablename__ = "support_sessions"

    sid: Mapped[str] = mapped_column(String(128), primary_key=True)
    actor: Mapped[str] = mapped_column(
        ForeignKey("platform_admins.email", ondelete="CASCADE"), index=True
    )
    target_workspace_slug: Mapped[str] = mapped_column(
        ForeignKey("workspaces.slug"), index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @validates("actor", "target_workspace_slug")
    def _normalize_key(self, _key: str, value: str) -> str:
        return normalize(value)


class ProvisioningStep(WorkspaceRow, Base):
    __tablename__ = "provisioning_steps"

    # No Workspace FK: migration must complete before this slug is registered.
    workspace_slug: Mapped[str] = mapped_column(String(63), primary_key=True)
    step: Mapped[str] = mapped_column(String(100), primary_key=True)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    db_name: Mapped[str | None] = mapped_column(String(63))
    db_user: Mapped[str | None] = mapped_column(String(63))
    db_password_secret_ref: Mapped[str | None] = mapped_column(String(512))
    schema_revision: Mapped[str | None] = mapped_column(String(32))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

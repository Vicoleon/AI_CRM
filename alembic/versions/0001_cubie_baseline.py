"""Cubie baseline: the whole workspace schema in one revision.

Revision ID: 0001_cubie_baseline
Revises:
Create Date: 2026-10-29 00:00:00.000000

Phase 1 collapsed the ninety-odd incremental migrations into this one
(spec 4.2.6). It is generated from the models (``Base.metadata``) and builds
every table, key, index, enum and CHECK constraint they declare, on Postgres
and on SQLite. It carries no rows: the defaults the old chain seeded (the AI
Priorities playbook, the Drive label weights, the SLA placeholders) and the
Smart Teams skills taxonomy come from ``rubik_core.database.seed.seed_workspace``.

Proven two ways:

* ``backend/tests/test_migrations_baseline.py``: on SQLite, ``alembic upgrade
  head`` builds exactly what ``Base.metadata.create_all`` builds;
* ``scripts/dev/schema_diff.sh``: on Postgres, against a database built from
  the pre-squash chain (enums, partial and expression indexes and CHECK
  constraints included). The commit that added this file records what differs
  and why.

A new migration chains off this one (``down_revision = "0001_cubie_baseline"``)
and is numbered ``0002_...``, ``0003_...``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_cubie_baseline"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The Postgres enum types the tables below create. ``drop_table`` leaves a
#: type behind, so ``downgrade`` drops them as well.
ENUM_TYPES: tuple[str, ...] = (
    "eos_issue_priority",
    "eos_issue_status",
    "eos_rock_status",
    "eos_todo_status",
    "eos_visibility",
    "eos_vto_section_type",
    "l10inputkind",
    "rockstatus",
)


def upgrade() -> None:
    op.create_table(
        "activity_daily",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("area", sa.String(), nullable=False),
        sa.Column("department_slug", sa.String(), nullable=True),
        sa.Column("requests", sa.Integer(), nullable=False),
        sa.Column("first_at", sa.DateTime(), nullable=False),
        sa.Column("last_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "user_email = lower(user_email)", name="ck_activity_daily_email_lower"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_email", "day", "area", name="uq_activity_daily_user_day_area"
        ),
    )
    op.create_index("ix_activity_daily_day", "activity_daily", ["day"], unique=False)

    op.create_table(
        "agent_recommendations",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("lead_id", sa.String(), nullable=True),
        sa.Column("lead_name", sa.String(), nullable=True),
        sa.Column("recommendation_text", sa.String(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("source_metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("dismissed_at", sa.DateTime(), nullable=True),
        sa.Column("snoozed_until", sa.DateTime(), nullable=True),
        sa.Column("acted_on_at", sa.DateTime(), nullable=True),
        sa.Column("action_type", sa.String(), nullable=True),
        sa.Column("action_metadata", sa.JSON(), nullable=True),
        sa.Column("sources", sa.JSON(), nullable=True),
        sa.Column("correlation_id", sa.String(), nullable=True),
        sa.Column("next_action", sa.String(), nullable=True),
        sa.Column("slack_delivered_at", sa.DateTime(), nullable=True),
        sa.Column("manager_escalated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_recommendations_active_window",
        "agent_recommendations",
        ["user_email", "dismissed_at", "snoozed_until"],
        unique=False,
    )
    op.create_index(
        "ix_agent_recommendations_correlation_id",
        "agent_recommendations",
        ["correlation_id"],
        unique=False,
    )
    op.create_index(
        "ix_agent_recommendations_lead_id",
        "agent_recommendations",
        ["lead_id"],
        unique=False,
    )
    op.create_index(
        "ix_agent_recommendations_user_active",
        "agent_recommendations",
        ["user_email", "dismissed_at", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_agent_recommendations_user_email",
        "agent_recommendations",
        ["user_email"],
        unique=False,
    )

    op.create_table(
        "assistant_feedback",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("surface", sa.String(), nullable=False),
        sa.Column("proposal_id", sa.String(), nullable=False),
        sa.Column("proposal_text", sa.String(), nullable=False),
        sa.Column("proposal_metadata", sa.JSON(), nullable=True),
        sa.Column("rating", sa.String(), nullable=False),
        sa.Column("comment", sa.String(), nullable=True),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_email",
            "surface",
            "proposal_id",
            name="uq_assistant_feedback_user_item",
        ),
    )
    op.create_index(
        "ix_assistant_feedback_surface_rating",
        "assistant_feedback",
        ["surface", "rating", "updated_at"],
        unique=False,
    )
    op.create_index(
        "ix_assistant_feedback_user_email",
        "assistant_feedback",
        ["user_email"],
        unique=False,
    )

    op.create_table(
        "attendee_profiles",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("linkedin_url", sa.String(), nullable=True),
        sa.Column("provider", sa.String(), nullable=True),
        sa.Column("confidence", sa.String(), nullable=True),
        sa.Column("profile", sa.JSON(), nullable=True),
        sa.Column("activity", sa.JSON(), nullable=True),
        sa.Column("persona", sa.JSON(), nullable=True),
        sa.Column("sources", sa.JSON(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("researched_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_attendee_profiles_email", "attendee_profiles", ["email"], unique=True
    )
    op.create_index(
        "ix_attendee_profiles_researched_at",
        "attendee_profiles",
        ["researched_at"],
        unique=False,
    )

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=True),
        sa.Column("actor_email", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("doc_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "browser_connections",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("token_hint", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_reason", sa.String(), nullable=True),
        sa.CheckConstraint(
            "revoked_reason IS NULL OR revoked_reason IN ('disconnected', 'offboarded', 'replaced')",
            name="ck_browser_connections_revoked_reason",
        ),
        sa.CheckConstraint(
            "user_email = lower(user_email)", name="ck_browser_connections_user_lower"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        "ix_browser_connections_user_open",
        "browser_connections",
        ["user_email", "revoked_at"],
        unique=False,
    )

    op.create_table(
        "call_company_product_defaults",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("ae_email", sa.String(), nullable=False),
        sa.Column("hubspot_company_id", sa.String(), nullable=False),
        sa.Column("product_keys", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "ae_email = lower(ae_email)",
            name="ck_call_company_product_defaults_email_lower",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "ae_email",
            "hubspot_company_id",
            name="uq_call_company_product_defaults_ae_company",
        ),
    )
    op.create_index(
        "ix_call_company_product_defaults_ae_email",
        "call_company_product_defaults",
        ["ae_email"],
        unique=False,
    )
    op.create_index(
        "ix_call_company_product_defaults_email_lower",
        "call_company_product_defaults",
        [sa.literal_column("lower(ae_email)")],
        unique=False,
    )

    op.create_table(
        "call_event_subscriptions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("google_user_id", sa.String(), nullable=False),
        sa.Column("ae_email", sa.String(), nullable=False),
        sa.Column("subscription_name", sa.String(), nullable=False),
        sa.Column("event_types", sa.JSON(), nullable=False),
        sa.Column("expire_time", sa.DateTime(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("last_renewed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "ae_email = lower(ae_email)", name="ck_call_event_subscriptions_email_lower"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "google_user_id", name="uq_call_event_subscriptions_google_user"
        ),
    )
    op.create_index(
        "ix_call_event_subscriptions_ae_email",
        "call_event_subscriptions",
        ["ae_email"],
        unique=False,
    )
    op.create_index(
        "ix_call_event_subscriptions_expire_time",
        "call_event_subscriptions",
        ["expire_time"],
        unique=False,
    )

    op.create_table(
        "call_scripts",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("product_key", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("hubspot_product_id", sa.String(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("approved_by", sa.String(), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "source IN ('mined', 'manual', 'imported')", name="ck_call_scripts_source"
        ),
        sa.CheckConstraint(
            "approved_by = lower(approved_by)", name="ck_call_scripts_approved_by_lower"
        ),
        sa.CheckConstraint(
            "product_key = lower(product_key)", name="ck_call_scripts_product_key_lower"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "product_key", "version", name="uq_call_scripts_product_version"
        ),
    )
    op.create_index(
        "ix_call_scripts_product_active",
        "call_scripts",
        ["product_key", "is_active", "version"],
        unique=False,
    )
    op.create_index(
        "ix_call_scripts_product_key", "call_scripts", ["product_key"], unique=False
    )

    op.create_table(
        "call_sessions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("ae_email", sa.String(), nullable=False),
        sa.Column("ae_google_user_id", sa.String(), nullable=True),
        sa.Column("hubspot_company_id", sa.String(), nullable=True),
        sa.Column("hubspot_contact_id", sa.String(), nullable=True),
        sa.Column("hubspot_deal_id", sa.String(), nullable=True),
        sa.Column("meet_space_name", sa.String(), nullable=True),
        sa.Column("meet_meeting_code", sa.String(), nullable=True),
        sa.Column("meet_uri", sa.String(), nullable=True),
        sa.Column("conference_record", sa.String(), nullable=True),
        sa.Column("capture_consent", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("origin", sa.String(), nullable=False),
        sa.Column("calendar_event_id", sa.String(), nullable=True),
        sa.Column("fathom_recording_id", sa.String(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("wrapped_at", sa.DateTime(), nullable=True),
        sa.Column("wrap_opened_at", sa.DateTime(), nullable=True),
        sa.Column("nothing_here", sa.Boolean(), nullable=False),
        sa.Column("attrs", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "capture_consent IN ('granted', 'suppressed')",
            name="ck_call_sessions_consent",
        ),
        sa.CheckConstraint(
            "status IN ('scheduled', 'in_progress', 'ended', 'transcript_ready', 'extracted', 'wrapped', 'no_transcript', 'failed')",
            name="ck_call_sessions_status",
        ),
        sa.CheckConstraint(
            "ae_email = lower(ae_email)", name="ck_call_sessions_email_lower"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "ae_email", "calendar_event_id", name="uq_call_sessions_ae_calendar_event"
        ),
        sa.UniqueConstraint("conference_record", name="uq_call_sessions_conf_record"),
        sa.UniqueConstraint("meet_space_name", name="uq_call_sessions_meet_space"),
    )
    op.create_index(
        "ix_call_sessions_ae_email", "call_sessions", ["ae_email"], unique=False
    )
    op.create_index(
        "ix_call_sessions_ae_email_lower",
        "call_sessions",
        [sa.literal_column("lower(ae_email)")],
        unique=False,
    )
    op.create_index(
        "ix_call_sessions_ae_status",
        "call_sessions",
        ["ae_email", "status"],
        unique=False,
    )
    op.create_index(
        "ix_call_sessions_calendar_event_id",
        "call_sessions",
        ["calendar_event_id"],
        unique=False,
    )
    op.create_index(
        "ix_call_sessions_fathom_recording_id",
        "call_sessions",
        ["fathom_recording_id"],
        unique=True,
    )
    op.create_index(
        "ix_call_sessions_hubspot_company_id",
        "call_sessions",
        ["hubspot_company_id"],
        unique=False,
    )
    op.create_index(
        "ix_call_sessions_hubspot_deal_id",
        "call_sessions",
        ["hubspot_deal_id"],
        unique=False,
    )
    op.create_index(
        "ix_call_sessions_meet_meeting_code",
        "call_sessions",
        ["meet_meeting_code"],
        unique=False,
    )
    op.create_index(
        "ix_call_sessions_status", "call_sessions", ["status"], unique=False
    )

    op.create_table(
        "catalog_items",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=120), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=True),
        sa.Column("unit", sa.String(length=40), nullable=False),
        sa.Column("url", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "chat_folders",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_chat_folders_user_email", "chat_folders", ["user_email"], unique=False
    )

    op.create_table(
        "chat_upload_publications",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("upload_id", sa.String(), nullable=False),
        sa.Column("owner_email", sa.String(), nullable=False),
        sa.Column("doc_id", sa.String(), nullable=False),
        sa.Column("published_filename", sa.String(), nullable=False),
        sa.Column("department", sa.String(), nullable=True),
        sa.Column("is_company_wide", sa.Boolean(), nullable=False),
        sa.Column("rendition", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_chat_upload_publications_owner_email",
        "chat_upload_publications",
        ["owner_email"],
        unique=False,
    )
    op.create_index(
        "ix_chat_upload_publications_upload_id",
        "chat_upload_publications",
        ["upload_id"],
        unique=False,
    )

    op.create_table(
        "chat_uploads",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_email", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("mime_type", sa.String(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("storage_uri", sa.String(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("extracted_text", sa.Text(), nullable=True),
        sa.Column("text_truncated", sa.Boolean(), nullable=False),
        sa.Column("detail", sa.String(), nullable=True),
        sa.Column("read_by", sa.String(), nullable=True),
        sa.Column("index_status", sa.String(), nullable=False),
        sa.Column("index_note", sa.String(), nullable=True),
        sa.Column("rag_file_name", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_chat_uploads_content_hash", "chat_uploads", ["content_hash"], unique=False
    )
    op.create_index(
        "ix_chat_uploads_owner_email", "chat_uploads", ["owner_email"], unique=False
    )
    op.create_index(
        "ix_chat_uploads_session_id", "chat_uploads", ["session_id"], unique=False
    )

    op.create_table(
        "commitment_sla_configs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("sow_kind", sa.String(), nullable=False),
        sa.Column("hours", sa.Integer(), nullable=False),
        sa.Column("is_placeholder", sa.Boolean(), nullable=False),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('sow', 'wrapup', 'lead_touch')",
            name="ck_commitment_sla_configs_kind",
        ),
        sa.CheckConstraint(
            "hours > 0", name="ck_commitment_sla_configs_hours_positive"
        ),
        sa.CheckConstraint(
            "updated_by = lower(updated_by)",
            name="ck_commitment_sla_configs_updated_lower",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kind", "sow_kind", name="uq_commitment_sla_configs_kind"),
    )
    op.create_table(
        "commitment_stage_lags",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("hubspot_deal_id", sa.String(), nullable=False),
        sa.Column("hubspot_stage_id", sa.String(), nullable=False),
        sa.Column("evidence_stage_id", sa.String(), nullable=True),
        sa.Column("evidence_text", sa.Text(), nullable=True),
        sa.Column("evidence_ref", sa.String(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("resolved_by", sa.String(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('open', 'applied', 'rejected')",
            name="ck_commitment_stage_lags_status",
        ),
        sa.CheckConstraint(
            "resolved_by = lower(resolved_by)",
            name="ck_commitment_stage_lags_resolved_by_lower",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_commitment_stage_lags_deal_status",
        "commitment_stage_lags",
        ["hubspot_deal_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_commitment_stage_lags_hubspot_deal_id",
        "commitment_stage_lags",
        ["hubspot_deal_id"],
        unique=False,
    )
    op.create_index(
        "ix_commitment_stage_lags_status",
        "commitment_stage_lags",
        ["status"],
        unique=False,
    )

    op.create_table(
        "commitments",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("hubspot_deal_id", sa.String(), nullable=True),
        sa.Column("hubspot_company_id", sa.String(), nullable=True),
        sa.Column("owner_email", sa.String(), nullable=False),
        sa.Column("opened_at", sa.DateTime(), nullable=True),
        sa.Column("opened_source", sa.String(), nullable=False),
        sa.Column("opened_evidence_ref", sa.String(), nullable=True),
        sa.Column("opened_evidence_text", sa.Text(), nullable=True),
        sa.Column("sow_kind", sa.String(), nullable=True),
        sa.Column("promised_at", sa.DateTime(), nullable=True),
        sa.Column("promised_reason", sa.Text(), nullable=True),
        sa.Column("sla_hours", sa.Integer(), nullable=False),
        sa.Column("breach_at", sa.DateTime(), nullable=True),
        sa.Column("breached_at", sa.DateTime(), nullable=True),
        sa.Column("exec_visible_at", sa.DateTime(), nullable=True),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.Column("closed_source", sa.String(), nullable=True),
        sa.Column("closed_evidence_ref", sa.String(), nullable=True),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("accepted_reason", sa.Text(), nullable=True),
        sa.Column("escalated_at", sa.DateTime(), nullable=True),
        sa.Column("escalated_to", sa.String(), nullable=True),
        sa.Column("is_live_deal_scoped", sa.Boolean(), nullable=True),
        sa.Column("is_live_company_scoped", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "closed_source IS NULL OR closed_source IN ('manual', 'superseded')",
            name="ck_commitments_closed_source",
        ),
        sa.CheckConstraint(
            "kind IN ('sow', 'wrapup', 'lead_touch')", name="ck_commitments_kind"
        ),
        sa.CheckConstraint(
            "opened_source IN ('call', 'manual', 'stage_backfill')",
            name="ck_commitments_opened_source",
        ),
        sa.CheckConstraint(
            "state <> 'accepted' OR accepted_reason IS NOT NULL",
            name="ck_commitments_accepted_needs_reason",
        ),
        sa.CheckConstraint(
            "state IN ('provisional', 'open', 'at_risk', 'breached', 'escalated', 'exec_visible', 'accepted', 'closed')",
            name="ck_commitments_state",
        ),
        sa.CheckConstraint(
            "(is_live_company_scoped IS NOT NULL AND closed_at IS NULL AND hubspot_deal_id IS NULL AND hubspot_company_id IS NOT NULL) OR (is_live_company_scoped IS NULL AND (closed_at IS NOT NULL OR hubspot_deal_id IS NOT NULL OR hubspot_company_id IS NULL))",
            name="ck_commitments_live_company_flag_consistent",
        ),
        sa.CheckConstraint(
            "(is_live_deal_scoped IS NOT NULL AND closed_at IS NULL AND hubspot_deal_id IS NOT NULL) OR (is_live_deal_scoped IS NULL AND (closed_at IS NOT NULL OR hubspot_deal_id IS NULL))",
            name="ck_commitments_live_deal_flag_consistent",
        ),
        sa.CheckConstraint(
            "escalated_to = lower(escalated_to)",
            name="ck_commitments_escalated_to_lower",
        ),
        sa.CheckConstraint(
            "is_live_company_scoped IS NULL OR is_live_company_scoped",
            name="ck_commitments_live_company_true_or_null",
        ),
        sa.CheckConstraint(
            "is_live_deal_scoped IS NULL OR is_live_deal_scoped",
            name="ck_commitments_live_deal_true_or_null",
        ),
        sa.CheckConstraint(
            "owner_email = lower(owner_email)", name="ck_commitments_owner_lower"
        ),
        sa.CheckConstraint("sla_hours > 0", name="ck_commitments_sla_hours_positive"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "hubspot_company_id",
            "kind",
            "is_live_company_scoped",
            name="uq_commitments_live_company_kind",
        ),
        sa.UniqueConstraint(
            "hubspot_deal_id",
            "kind",
            "is_live_deal_scoped",
            name="uq_commitments_live_deal_kind",
        ),
    )
    op.create_index(
        "ix_commitments_breach_at", "commitments", ["breach_at"], unique=False
    )
    op.create_index(
        "ix_commitments_hubspot_company_id",
        "commitments",
        ["hubspot_company_id"],
        unique=False,
    )
    op.create_index(
        "ix_commitments_hubspot_deal_id",
        "commitments",
        ["hubspot_deal_id"],
        unique=False,
    )
    op.create_index("ix_commitments_kind", "commitments", ["kind"], unique=False)
    op.create_index(
        "ix_commitments_owner_email", "commitments", ["owner_email"], unique=False
    )
    op.create_index(
        "ix_commitments_owner_email_lower",
        "commitments",
        [sa.literal_column("lower(owner_email)")],
        unique=False,
    )
    op.create_index(
        "ix_commitments_owner_state",
        "commitments",
        ["owner_email", "state"],
        unique=False,
    )
    op.create_index("ix_commitments_state", "commitments", ["state"], unique=False)

    op.create_table(
        "crm_sync_runs",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("trigger", sa.String(), nullable=False),
        sa.Column("executor", sa.String(), nullable=False),
        sa.Column("execution_name", sa.String(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("window_start", sa.DateTime(), nullable=True),
        sa.Column("stats", sa.JSON(), nullable=True),
        sa.Column("queued_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index(
        "ix_crm_sync_runs_user_email", "crm_sync_runs", ["user_email"], unique=False
    )
    op.create_index(
        "ix_crm_sync_runs_user_status",
        "crm_sync_runs",
        ["user_email", "status"],
        unique=False,
    )

    op.create_table(
        "crm_sync_suggestions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("doc_id", sa.String(), nullable=True),
        sa.Column("meeting_id", sa.String(), nullable=True),
        sa.Column("meeting_title", sa.String(), nullable=True),
        sa.Column("meeting_date", sa.DateTime(), nullable=True),
        sa.Column("change_type", sa.String(), nullable=False),
        sa.Column("hubspot_object_type", sa.String(), nullable=False),
        sa.Column("hubspot_object_id", sa.String(), nullable=True),
        sa.Column("target_key", sa.String(), nullable=False),
        sa.Column("dedupe_key", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.Column("applied_by", sa.String(), nullable=True),
        sa.Column("hubspot_response", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_email", "dedupe_key", name="uq_crm_sync_user_dedupe"),
    )
    op.create_index(
        "ix_crm_sync_suggestions_run_id",
        "crm_sync_suggestions",
        ["run_id"],
        unique=False,
    )
    op.create_index(
        "ix_crm_sync_suggestions_target_key",
        "crm_sync_suggestions",
        ["target_key"],
        unique=False,
    )
    op.create_index(
        "ix_crm_sync_suggestions_user_email",
        "crm_sync_suggestions",
        ["user_email"],
        unique=False,
    )
    op.create_index(
        "ix_crm_sync_suggestions_user_status",
        "crm_sync_suggestions",
        ["user_email", "status"],
        unique=False,
    )

    op.create_table(
        "deal_defers",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("hubspot_deal_id", sa.String(), nullable=False),
        sa.Column("deal_name", sa.String(), nullable=True),
        sa.Column("owner_email", sa.String(), nullable=False),
        sa.Column("deferred_by", sa.String(), nullable=False),
        sa.Column("follow_up_on", sa.Date(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("ended_by", sa.String(), nullable=True),
        sa.Column("ended_reason", sa.String(), nullable=True),
        sa.Column("hubspot_task_status", sa.String(), nullable=False),
        sa.Column("hubspot_task_id", sa.String(), nullable=True),
        sa.Column("hubspot_task_error", sa.String(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "ended_reason IS NULL OR ended_reason IN ('undeferred', 'replaced', 'expired')",
            name="ck_deal_defers_ended_reason",
        ),
        sa.CheckConstraint(
            "hubspot_task_status IN ('pending', 'written', 'failed')",
            name="ck_deal_defers_task_status",
        ),
        sa.CheckConstraint(
            "deferred_by = lower(deferred_by)", name="ck_deal_defers_deferred_by_lower"
        ),
        sa.CheckConstraint(
            "owner_email = lower(owner_email)", name="ck_deal_defers_owner_lower"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_deal_defers_follow_up_on", "deal_defers", ["follow_up_on"], unique=False
    )
    op.create_index(
        "ix_deal_defers_owner_open",
        "deal_defers",
        ["owner_email", "ended_at"],
        unique=False,
    )
    op.create_index(
        "uq_deal_defers_open_deal",
        "deal_defers",
        ["hubspot_deal_id"],
        unique=True,
        sqlite_where=sa.text("ended_at IS NULL"),
        postgresql_where=sa.text("ended_at IS NULL"),
    )

    op.create_table(
        "departments",
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("slack_priorities_enabled", sa.Boolean(), nullable=True),
        sa.Column("slack_manager_channel", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("name"),
    )
    op.create_table(
        "digest_logs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("recipient_email", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("local_date", sa.String(), nullable=False),
        sa.Column("channel", sa.String(), nullable=True),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("suppressed_count", sa.Integer(), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('daily', 'weekly', 'close_date_rollup')",
            name="ck_digest_logs_kind",
        ),
        sa.CheckConstraint(
            "recipient_email = lower(recipient_email)",
            name="ck_digest_logs_recipient_lower",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "recipient_email",
            "kind",
            "local_date",
            name="uq_digest_logs_recipient_kind_date",
        ),
    )
    op.create_index(
        "ix_digest_logs_recipient_email",
        "digest_logs",
        ["recipient_email"],
        unique=False,
    )
    op.create_index(
        "ix_digest_logs_recipient_email_lower",
        "digest_logs",
        [sa.literal_column("lower(recipient_email)")],
        unique=False,
    )

    op.create_table(
        "drive_label_priorities",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("label_key", sa.String(), nullable=True),
        sa.Column("label_value", sa.String(), nullable=False),
        sa.Column("priority", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "label_key", "label_value", name="uq_drive_label_priority_kv"
        ),
    )
    op.create_table(
        "eos_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("resource_type", sa.String(), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "eos_ingest_state",
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("requested_at", sa.DateTime(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("executor", sa.String(), nullable=True),
        sa.Column("execution_name", sa.String(), nullable=True),
        sa.Column("last_started_at", sa.DateTime(), nullable=True),
        sa.Column("last_finished_at", sa.DateTime(), nullable=True),
        sa.Column("last_result", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("kind"),
    )
    op.create_table(
        "eos_organizations",
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "eos_rock_carryovers",
        sa.Column("source_rock_id", sa.Uuid(), nullable=False),
        sa.Column("carried_rock_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("carried_rock_id"),
    )
    op.create_index(
        "ix_eos_rock_carryovers_source",
        "eos_rock_carryovers",
        ["source_rock_id"],
        unique=False,
    )

    op.create_table(
        "feature_access",
        sa.Column("feature_key", sa.String(), nullable=False),
        sa.Column("departments", sa.JSON(), nullable=False),
        sa.Column("extra_emails", sa.JSON(), nullable=False),
        sa.Column("exec_allowed", sa.Boolean(), nullable=False),
        sa.Column("admin_allowed", sa.Boolean(), nullable=False),
        sa.Column("everyone", sa.Boolean(), nullable=False),
        sa.Column("note", sa.String(), nullable=True),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("feature_key"),
    )
    op.create_table(
        "graph_entities",
        sa.Column("entity_key", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("entity_id", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=True),
        sa.Column("attrs", sa.JSON(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("entity_key"),
        sa.UniqueConstraint("kind", "entity_id", name="uq_graph_entity"),
    )
    op.create_index("ix_graph_entities_kind", "graph_entities", ["kind"], unique=False)

    op.create_table(
        "groups",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "home_cards",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("date", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("pipeline_stage", sa.String(), nullable=True),
        sa.Column("content_md", sa.String(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "ingestion_council_verdicts",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("overall_health", sa.String(), nullable=False),
        sa.Column("headline", sa.String(), nullable=False),
        sa.Column("verdict_json", sa.JSON(), nullable=False),
        sa.Column("report_html", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ingestion_council_scope_created",
        "ingestion_council_verdicts",
        ["scope", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_ingestion_council_verdicts_created_at",
        "ingestion_council_verdicts",
        ["created_at"],
        unique=False,
    )

    op.create_table(
        "interaction_logs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=True),
        sa.Column("user_input", sa.String(), nullable=False),
        sa.Column("system_prompt", sa.String(), nullable=True),
        sa.Column("llm_output", sa.String(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("successful", sa.Boolean(), nullable=True),
        sa.Column("error_message", sa.String(), nullable=True),
        sa.Column("mode", sa.String(), nullable=True),
        sa.Column("scope", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "llm_usage_events",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=True),
        sa.Column("feature", sa.String(), nullable=False),
        sa.Column("operation", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("cached_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("user_email", sa.String(), nullable=True),
        sa.Column("request_id", sa.String(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_llm_usage_events_feature", "llm_usage_events", ["feature"], unique=False
    )
    op.create_index(
        "ix_llm_usage_events_timestamp", "llm_usage_events", ["timestamp"], unique=False
    )
    op.create_index(
        "ix_llm_usage_events_user_email",
        "llm_usage_events",
        ["user_email"],
        unique=False,
    )
    op.create_index(
        "ix_llm_usage_feature_ts",
        "llm_usage_events",
        ["feature", "timestamp"],
        unique=False,
    )

    op.create_table(
        "login_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("ip_address", sa.String(), nullable=True),
        sa.Column("user_agent", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_login_events_email_ts",
        "login_events",
        ["user_email", "timestamp"],
        unique=False,
    )
    op.create_index(
        "ix_login_events_timestamp", "login_events", ["timestamp"], unique=False
    )

    op.create_table(
        "loop_digest_posts",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("channel_key", sa.String(length=64), nullable=False),
        sa.Column("local_date", sa.String(length=10), nullable=False),
        sa.Column("posted_at", sa.DateTime(), nullable=True),
        sa.Column("message_ts", sa.String(length=64), nullable=True),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "channel_key", "local_date", name="uq_loop_digest_posts_channel_date"
        ),
    )
    op.create_table(
        "loop_items",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("owner_email", sa.String(length=320), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("source_key", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("headline", sa.String(length=300), nullable=True),
        sa.Column("counterparty_email", sa.String(length=320), nullable=True),
        sa.Column("counterparty_label", sa.String(length=200), nullable=True),
        sa.Column("company_label", sa.String(length=200), nullable=True),
        sa.Column("hubspot_contact_id", sa.String(length=32), nullable=True),
        sa.Column("hubspot_company_id", sa.String(length=32), nullable=True),
        sa.Column("hubspot_deal_id", sa.String(length=32), nullable=True),
        sa.Column("thread_id", sa.String(length=64), nullable=True),
        sa.Column("message_id", sa.String(length=128), nullable=True),
        sa.Column("link", sa.String(length=500), nullable=True),
        sa.Column("opened_at", sa.DateTime(), nullable=False),
        sa.Column("first_flagged_at", sa.DateTime(), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(), nullable=False),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.Column("close_reason", sa.String(length=32), nullable=True),
        sa.Column("close_evidence", sa.JSON(), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(), nullable=True),
        sa.Column("dismiss_reason", sa.String(length=32), nullable=True),
        sa.Column("dismissed_by", sa.String(length=320), nullable=True),
        sa.Column("reopened_count", sa.Integer(), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "close_reason IS NULL OR close_reason IN ('logged_in_hubspot', 'logged_from_assistant', 'replied', 'logged_follow_up', 'task_completed', 'task_gone', 'promise_kept', 'promise_withdrawn', 'rescheduled', 'aged_out')",
            name="ck_loop_items_close_reason",
        ),
        sa.CheckConstraint(
            "dismiss_reason IS NULL OR dismiss_reason IN ('not_a_client', 'automated', 'internal', 'handled_elsewhere', 'not_a_promise', 'other')",
            name="ck_loop_items_dismiss_reason",
        ),
        sa.CheckConstraint(
            "kind IN ('unlogged', 'open_loop', 'todo')", name="ck_loop_items_kind"
        ),
        sa.CheckConstraint(
            "source IN ('gmail', 'slack', 'promise', 'hubspot_task')",
            name="ck_loop_items_source",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'closed', 'dismissed')", name="ck_loop_items_status"
        ),
        sa.CheckConstraint(
            "counterparty_email IS NULL OR counterparty_email = lower(counterparty_email)",
            name="ck_loop_items_counterparty_lower",
        ),
        sa.CheckConstraint(
            "dismissed_by IS NULL OR dismissed_by = lower(dismissed_by)",
            name="ck_loop_items_dismissed_by_lower",
        ),
        sa.CheckConstraint(
            "owner_email = lower(owner_email)", name="ck_loop_items_owner_lower"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_email", "kind", "source_key", name="uq_loop_items_owner_kind_key"
        ),
    )
    op.create_index(
        "ix_loop_items_owner_status",
        "loop_items",
        ["owner_email", "status"],
        unique=False,
    )
    op.create_index(
        "ix_loop_items_status_opened",
        "loop_items",
        ["status", "opened_at"],
        unique=False,
    )

    op.create_table(
        "loop_judgements",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("owner_email", sa.String(length=320), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("source_key", sa.String(length=255), nullable=False),
        sa.Column("verdict", sa.String(length=16), nullable=False),
        sa.Column("judged_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('unlogged', 'open_loop', 'todo')", name="ck_loop_judgements_kind"
        ),
        sa.CheckConstraint(
            "verdict IN ('declined')", name="ck_loop_judgements_verdict"
        ),
        sa.CheckConstraint(
            "owner_email = lower(owner_email)", name="ck_loop_judgements_owner_lower"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_email", "kind", "source_key", name="uq_loop_judgements_owner_key"
        ),
    )
    op.create_table(
        "loop_mutes",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("owner_email", sa.String(length=320), nullable=False),
        sa.Column("pattern", sa.String(length=320), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=320), nullable=True),
        sa.CheckConstraint(
            "owner_email = lower(owner_email)", name="ck_loop_mutes_owner_lower"
        ),
        sa.CheckConstraint(
            "pattern = lower(pattern)", name="ck_loop_mutes_pattern_lower"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_email", "pattern", name="uq_loop_mutes_owner_pattern"
        ),
    )
    op.create_table(
        "loop_scan_runs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("report", sa.JSON(), nullable=True),
        sa.Column("coverage", sa.JSON(), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed')",
            name="ck_loop_scan_runs_status",
        ),
        sa.CheckConstraint(
            "trigger IN ('scheduler', 'admin', 'cli')", name="ck_loop_scan_runs_trigger"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_loop_scan_runs_status_started",
        "loop_scan_runs",
        ["status", "started_at"],
        unique=False,
    )

    op.create_table(
        "mcp_connections",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("command", sa.String(), nullable=True),
        sa.Column("args", sa.JSON(), nullable=True),
        sa.Column("server_url", sa.String(), nullable=True),
        sa.Column("auth_mode", sa.String(), nullable=True),
        sa.Column("service_account_ref", sa.String(), nullable=True),
        sa.Column("allowed_tools", sa.JSON(), nullable=True),
        sa.Column("read_only", sa.Boolean(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.Column("environment", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "meeting_definition_versions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("definition_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('template', 'skill')", name="ck_meeting_definition_kind"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "kind", "definition_id", "version", name="uq_meeting_definition_versions"
        ),
    )
    op.create_table(
        "meeting_prep_runs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("event_id", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("prep_kind", sa.String(), nullable=True),
        sa.Column("is_sales_user", sa.Boolean(), nullable=False),
        sa.Column("is_internal", sa.Boolean(), nullable=False),
        sa.Column("model_id", sa.String(), nullable=True),
        sa.Column("context_bundle", sa.JSON(), nullable=True),
        sa.Column("response", sa.JSON(), nullable=True),
        sa.Column("partial_errors", sa.JSON(), nullable=True),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("delivery_status", sa.String(), nullable=True),
        sa.Column("pdf_drive_file_id", sa.String(), nullable=True),
        sa.Column("council_verdict", sa.JSON(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=True),
        sa.Column("base_run_id", sa.String(), nullable=True),
        sa.Column("change_kind", sa.String(), nullable=True),
        sa.Column("change_note", sa.Text(), nullable=True),
        sa.Column("changed_section", sa.String(), nullable=True),
        sa.Column("section_meta", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_meeting_prep_runs_created_at",
        "meeting_prep_runs",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ix_meeting_prep_runs_event_id", "meeting_prep_runs", ["event_id"], unique=False
    )
    op.create_index(
        "ix_meeting_prep_runs_user_created",
        "meeting_prep_runs",
        ["user_email", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_meeting_prep_runs_user_email",
        "meeting_prep_runs",
        ["user_email"],
        unique=False,
    )
    op.create_index(
        "uq_meeting_prep_runs_user_event_version",
        "meeting_prep_runs",
        ["user_email", "event_id", "version"],
        unique=True,
    )

    op.create_table(
        "meeting_skill_runs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_email", sa.String(), nullable=False),
        sa.Column("skill_id", sa.String(), nullable=True),
        sa.Column("skill_version", sa.Integer(), nullable=True),
        sa.Column("template_id", sa.String(), nullable=True),
        sa.Column("template_version", sa.Integer(), nullable=True),
        sa.Column("sections", sa.JSON(), nullable=True),
        sa.Column("doc_ids", sa.JSON(), nullable=False),
        sa.Column("transcript_hashes", sa.JSON(), nullable=True),
        sa.Column("trigger", sa.String(), nullable=False),
        sa.Column("routine_id", sa.String(), nullable=True),
        sa.Column("plan", sa.JSON(), nullable=True),
        sa.Column("intermediate", sa.JSON(), nullable=True),
        sa.Column("verification", sa.JSON(), nullable=True),
        sa.Column("output_md", sa.Text(), nullable=True),
        sa.Column("citations", sa.JSON(), nullable=True),
        sa.Column("output_ref", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('ok', 'partial', 'failed')", name="ck_meeting_runs_status"
        ),
        sa.CheckConstraint(
            "trigger IN ('chat', 'routine', 'scheduled', 'dry_run')",
            name="ck_meeting_runs_trigger",
        ),
        sa.CheckConstraint(
            "owner_email = lower(owner_email)", name="ck_meeting_runs_owner_lower"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_meeting_skill_runs_owner_created",
        "meeting_skill_runs",
        ["owner_email", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_meeting_skill_runs_skill", "meeting_skill_runs", ["skill_id"], unique=False
    )

    op.create_table(
        "meeting_templates",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_email", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("name_key", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("sections", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_via", sa.String(), nullable=False),
        sa.Column("source_session_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "created_via IN ('chat', 'upload', 'output')",
            name="ck_meeting_templates_via",
        ),
        sa.CheckConstraint(
            "owner_email = lower(owner_email)", name="ck_meeting_templates_owner_lower"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_email", "name_key", name="uq_meeting_templates_name"
        ),
    )
    op.create_table(
        "notifications",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("actor_email", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("body", sa.String(), nullable=False),
        sa.Column("severity", sa.String(), nullable=False),
        sa.Column("link", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("read_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_notifications_actor_read_created",
        "notifications",
        ["actor_email", "read_at", sa.literal_column("created_at DESC")],
        unique=False,
    )

    op.create_table(
        "offboard_records",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("user_name", sa.String(), nullable=False),
        sa.Column("department", sa.String(), nullable=True),
        sa.Column("successor_email", sa.String(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("offboarded_at", sa.DateTime(), nullable=False),
        sa.Column("sweep_status", sa.String(), nullable=False),
        sa.Column("sweep_detail", sa.JSON(), nullable=True),
        sa.Column("dossier_md", sa.String(), nullable=True),
        sa.Column("dossier_meta", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_offboard_records_successor_email",
        "offboard_records",
        ["successor_email"],
        unique=False,
    )
    op.create_index(
        "ix_offboard_records_user_email",
        "offboard_records",
        ["user_email"],
        unique=False,
    )

    op.create_table(
        "ooo_periods",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("start_date", sa.String(), nullable=False),
        sa.Column("end_date", sa.String(), nullable=False),
        sa.Column("return_date", sa.String(), nullable=False),
        sa.Column("synced_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_email", "start_date", name="uq_ooo_user_start"),
    )
    op.create_index(
        "ix_ooo_periods_user_email", "ooo_periods", ["user_email"], unique=False
    )
    op.create_index(
        "ix_ooo_user_dates",
        "ooo_periods",
        ["user_email", "start_date", "end_date"],
        unique=False,
    )

    op.create_table(
        "priorities_runs",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("trigger", sa.String(), nullable=False),
        sa.Column("executor", sa.String(), nullable=False),
        sa.Column("execution_name", sa.String(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("queued_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index(
        "ix_priorities_runs_user_email", "priorities_runs", ["user_email"], unique=False
    )
    op.create_index(
        "ix_priorities_runs_user_status",
        "priorities_runs",
        ["user_email", "status"],
        unique=False,
    )

    op.create_table(
        "priority_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("priority_id", sa.String(), nullable=True),
        sa.Column("entity_kind", sa.String(), nullable=True),
        sa.Column("entity_id", sa.String(), nullable=True),
        sa.Column("priority_kind", sa.String(), nullable=True),
        sa.Column("actor", sa.String(), nullable=True),
        sa.Column("department_slug", sa.String(), nullable=True),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "user_email = lower(user_email)", name="ck_priority_events_email_lower"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_priority_events_at", "priority_events", ["at"], unique=False)
    op.create_index(
        "ix_priority_events_user_at",
        "priority_events",
        ["user_email", "at"],
        unique=False,
    )

    op.create_table(
        "promise_scans",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_email", sa.String(), nullable=False),
        sa.Column("source_key", sa.String(), nullable=False),
        sa.Column("scanned_at", sa.DateTime(), nullable=False),
        sa.Column("found", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "owner_email = lower(owner_email)", name="ck_promise_scans_owner_lower"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_promise_scans_owner_source",
        "promise_scans",
        ["owner_email", "source_key"],
        unique=True,
    )

    op.create_table(
        "promises",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_email", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("promised_for", sa.Date(), nullable=True),
        sa.Column("due_local", sa.Date(), nullable=True),
        sa.Column("timezone", sa.String(), nullable=False),
        sa.Column("counterparty_kind", sa.String(), nullable=True),
        sa.Column("counterparty_id", sa.String(), nullable=True),
        sa.Column("counterparty_label", sa.String(), nullable=True),
        sa.Column("object_kind", sa.String(), nullable=True),
        sa.Column("object_id", sa.String(), nullable=True),
        sa.Column("object_label", sa.String(), nullable=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("source_ref", sa.JSON(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("done_rule", sa.JSON(), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("snooze_count", sa.Integer(), nullable=False),
        sa.Column("slipping_since", sa.DateTime(), nullable=True),
        sa.Column("is_private", sa.Boolean(), nullable=False),
        sa.Column("visible_to_counterparty", sa.Boolean(), nullable=False),
        sa.Column("eos_todo_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("kept_at", sa.DateTime(), nullable=True),
        sa.Column("dropped_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "counterparty_kind IS NULL OR counterparty_kind IN ('person', 'company', 'deal')",
            name="ck_promises_counterparty_kind",
        ),
        sa.CheckConstraint(
            "object_kind IS NULL OR object_kind IN ('thread', 'document', 'deal', 'event', 'transcript', 'person', 'company')",
            name="ck_promises_object_kind",
        ),
        sa.CheckConstraint(
            "source IN ('heard_transcript', 'heard_email', 'heard_slack', 'priority_card', 'chat', 'slack_cmd', 'page', 'handed_off')",
            name="ck_promises_source",
        ),
        sa.CheckConstraint(
            "status IN ('suggested', 'open', 'slipping', 'evidence_found', 'kept', 'dropped', 'handed_off')",
            name="ck_promises_status",
        ),
        sa.CheckConstraint(
            "owner_email = lower(owner_email)", name="ck_promises_owner_lower"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_promises_eos_todo_id", "promises", ["eos_todo_id"], unique=False
    )
    op.create_index(
        "ix_promises_owner_due", "promises", ["owner_email", "due_local"], unique=False
    )
    op.create_index(
        "ix_promises_owner_email", "promises", ["owner_email"], unique=False
    )
    op.create_index(
        "ix_promises_owner_status", "promises", ["owner_email", "status"], unique=False
    )
    op.create_index("ix_promises_status", "promises", ["status"], unique=False)

    op.create_table(
        "proposal_templates",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("body_md", sa.Text(), nullable=False),
        sa.Column("catalog_item_names", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
    )
    op.create_table(
        "roles",
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("name"),
    )
    op.create_table(
        "scheduled_tasks",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("schedule_kind", sa.String(), nullable=False),
        sa.Column("time_local", sa.String(), nullable=True),
        sa.Column("days_of_week", sa.JSON(), nullable=True),
        sa.Column("day_of_month", sa.Integer(), nullable=True),
        sa.Column("run_at", sa.DateTime(), nullable=True),
        sa.Column("timezone", sa.String(), nullable=False),
        sa.Column("delivery_channels", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("paused_reason", sa.String(), nullable=True),
        sa.Column("next_run_at", sa.DateTime(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("last_run_status", sa.String(), nullable=True),
        sa.Column("last_run_id", sa.String(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("run_count", sa.Integer(), nullable=False),
        sa.Column("source_session_id", sa.String(), nullable=True),
        sa.Column("created_via", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_scheduled_tasks_status_next_run",
        "scheduled_tasks",
        ["status", "next_run_at"],
        unique=False,
    )
    op.create_index(
        "ix_scheduled_tasks_user_email", "scheduled_tasks", ["user_email"], unique=False
    )

    op.create_table(
        "st_documents",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("subject_emails", sa.JSON(), nullable=False),
        sa.Column("subject_key", sa.String(), nullable=False),
        sa.Column("profile_versions", sa.JSON(), nullable=False),
        sa.Column("profile_version_key", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("content_md", sa.Text(), nullable=True),
        sa.Column("doc_id", sa.String(), nullable=True),
        sa.Column("gcs_uri", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("approved_by", sa.String(), nullable=True),
        sa.Column("published", sa.Boolean(), nullable=False),
        sa.Column("unpublished_at", sa.DateTime(), nullable=True),
        sa.Column("rag_ingested", sa.Boolean(), nullable=False),
        sa.Column("rag_ingested_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('personal_guide', 'pair_guide', 'team_guide')",
            name="ck_st_document_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'ready', 'failed')", name="ck_st_document_status"
        ),
        sa.CheckConstraint(
            "NOT published OR approved_at IS NOT NULL",
            name="ck_st_document_published_needs_approval",
        ),
        sa.CheckConstraint(
            "subject_key = lower(subject_key)", name="ck_st_document_subject_key_lower"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "kind", "subject_key", "profile_version_key", name="uq_st_document_cache"
        ),
    )
    op.create_index(
        "ix_st_documents_kind_subject",
        "st_documents",
        ["kind", "subject_key"],
        unique=False,
    )

    op.create_table(
        "st_profiles",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=True),
        sa.Column("disc", sa.JSON(), nullable=True),
        sa.Column("big_five", sa.JSON(), nullable=True),
        sa.Column("type16", sa.JSON(), nullable=True),
        sa.Column("strengths", sa.JSON(), nullable=True),
        sa.Column("motivators", sa.JSON(), nullable=True),
        sa.Column("enneagram", sa.JSON(), nullable=True),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("origin", sa.String(), nullable=False),
        sa.Column("scored_at", sa.DateTime(), nullable=True),
        sa.Column("superseded_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "origin IN ('interview', 'official_upload', 'admin_seed')",
            name="ck_st_profile_origin",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'current', 'superseded')", name="ck_st_profile_status"
        ),
        sa.CheckConstraint(
            "is_current IS NULL OR is_current",
            name="ck_st_profile_is_current_true_or_null",
        ),
        sa.CheckConstraint(
            "user_email = lower(user_email)", name="ck_st_profile_email_lower"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_email", "is_current", name="uq_st_profile_one_current"
        ),
        sa.UniqueConstraint("user_email", "version", name="uq_st_profile_user_version"),
    )
    op.create_index(
        "ix_st_profiles_email_lower",
        "st_profiles",
        [sa.literal_column("lower(user_email)")],
        unique=False,
    )
    op.create_index(
        "ix_st_profiles_user_email", "st_profiles", ["user_email"], unique=False
    )
    op.create_index(
        "ix_st_profiles_user_status",
        "st_profiles",
        ["user_email", "status"],
        unique=False,
    )

    op.create_table(
        "st_skills_taxonomy",
        sa.Column("skill_key", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("cluster", sa.String(), nullable=True),
        sa.Column("aliases", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("skill_key"),
    )
    op.create_index(
        "ix_st_skills_taxonomy_cluster", "st_skills_taxonomy", ["cluster"], unique=False
    )

    op.create_table(
        "topics",
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("name"),
    )
    op.create_table(
        "user_bookmark",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("actor_email", sa.String(), nullable=False),
        sa.Column("target_kind", sa.String(), nullable=False),
        sa.Column("target_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "actor_email",
            "target_kind",
            "target_id",
            name="uq_user_bookmark_actor_target",
        ),
    )
    op.create_index(
        "ix_user_bookmark_actor_email", "user_bookmark", ["actor_email"], unique=False
    )

    op.create_table(
        "user_recent_touch",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("actor_email", sa.String(), nullable=False),
        sa.Column("target_kind", sa.String(), nullable=False),
        sa.Column("target_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("link", sa.String(), nullable=False),
        sa.Column("touched_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "actor_email",
            "target_kind",
            "target_id",
            name="uq_user_recent_touch_actor_target",
        ),
    )
    op.create_index(
        "ix_user_recent_touch_actor_touched",
        "user_recent_touch",
        ["actor_email", sa.literal_column("touched_at DESC")],
        unique=False,
    )

    op.create_table(
        "user_tour_progress",
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("tour_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("user_email", "tour_id"),
    )
    op.create_table(
        "user_voice_shortcuts",
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("shortcut", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("user_email"),
    )
    op.create_table(
        "users",
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("picture", sa.String(), nullable=True),
        sa.Column("department", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("last_login", sa.DateTime(), nullable=True),
        sa.Column("last_ingest", sa.DateTime(), nullable=True),
        sa.Column("workspace_groups", sa.JSON(), nullable=True),
        sa.Column("is_exec", sa.Boolean(), nullable=False),
        sa.Column("managed_departments", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("email"),
    )
    op.create_table(
        "whats_new_opens",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("entry_id", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("department", sa.String(length=80), nullable=False),
        sa.Column("viewer_key", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "source IN ('slack', 'try_it')", name="ck_whats_new_opens_source"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "entry_id",
            "viewer_key",
            "source",
            name="uq_whats_new_opens_entry_viewer_source",
        ),
    )
    op.create_index(
        "ix_whats_new_opens_entry_id", "whats_new_opens", ["entry_id"], unique=False
    )

    op.create_table(
        "workspace_settings",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "xray_snapshots",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("company_id", sa.String(), nullable=False),
        sa.Column("company_name", sa.String(), nullable=True),
        sa.Column("company_domain", sa.String(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("base_snapshot_id", sa.String(), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("focus", sa.String(), nullable=True),
        sa.Column("sections", sa.JSON(), nullable=True),
        sa.Column("signals", sa.JSON(), nullable=True),
        sa.Column("covered_through", sa.DateTime(), nullable=True),
        sa.Column("source_cursors", sa.JSON(), nullable=True),
        sa.Column("progress", sa.JSON(), nullable=True),
        sa.Column("partial_errors", sa.JSON(), nullable=True),
        sa.Column("model_id", sa.String(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_xray_snapshots_company_created",
        "xray_snapshots",
        ["company_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_xray_snapshots_company_id", "xray_snapshots", ["company_id"], unique=False
    )
    op.create_index(
        "ix_xray_snapshots_company_status",
        "xray_snapshots",
        ["company_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_xray_snapshots_company_version",
        "xray_snapshots",
        ["company_id", "version"],
        unique=False,
    )
    op.create_index(
        "ix_xray_snapshots_created_at", "xray_snapshots", ["created_at"], unique=False
    )

    op.create_table(
        "xray_timeline_events",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("company_id", sa.String(), nullable=False),
        sa.Column("event_key", sa.String(), nullable=False),
        sa.Column("ts", sa.DateTime(), nullable=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("detail", sa.String(), nullable=True),
        sa.Column("evidence", sa.String(), nullable=True),
        sa.Column("first_seen_snapshot_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "company_id", "event_key", name="uq_xray_timeline_company_event"
        ),
    )
    op.create_index(
        "ix_xray_timeline_company_source",
        "xray_timeline_events",
        ["company_id", "source"],
        unique=False,
    )
    op.create_index(
        "ix_xray_timeline_company_ts",
        "xray_timeline_events",
        ["company_id", "ts"],
        unique=False,
    )
    op.create_index(
        "ix_xray_timeline_events_company_id",
        "xray_timeline_events",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        "ix_xray_timeline_events_ts", "xray_timeline_events", ["ts"], unique=False
    )

    op.create_table(
        "access_policies",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_email", sa.String(), nullable=True),
        sa.Column("group_id", sa.String(), nullable=True),
        sa.Column("department", sa.String(), nullable=True),
        sa.Column("topic", sa.String(), nullable=True),
        sa.Column("can_read", sa.Boolean(), nullable=True),
        sa.Column("can_update", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["groups.id"],
        ),
        sa.ForeignKeyConstraint(["user_email"], ["users.email"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "agent_recommendation_feedback",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("recommendation_id", sa.String(), nullable=False),
        sa.Column("action_type", sa.String(), nullable=False),
        sa.Column("action_metadata", sa.JSON(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["recommendation_id"], ["agent_recommendations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_recommendation_feedback_recommendation_id",
        "agent_recommendation_feedback",
        ["recommendation_id"],
        unique=False,
    )

    op.create_table(
        "assets",
        sa.Column("doc_id", sa.String(), nullable=False),
        sa.Column("owner_email", sa.String(), nullable=False),
        sa.Column("approver_email", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("topic", sa.String(), nullable=True),
        sa.Column("department", sa.String(), nullable=True),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("original_filename", sa.String(), nullable=True),
        sa.Column("source_path", sa.String(), nullable=True),
        sa.Column("is_company_wide", sa.Boolean(), nullable=True),
        sa.Column("allowed_emails", sa.JSON(), nullable=True),
        sa.Column("content_hash", sa.String(), nullable=True),
        sa.Column("processing", sa.Boolean(), nullable=False),
        sa.Column("current_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["owner_email"],
            ["users.email"],
        ),
        sa.PrimaryKeyConstraint("doc_id"),
    )
    op.create_index("ix_assets_content_hash", "assets", ["content_hash"], unique=False)

    op.create_table(
        "call_extractions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("call_session_id", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("prompt_version", sa.String(), nullable=False),
        sa.Column("schema_version", sa.String(), nullable=False),
        sa.Column("raw_output", sa.JSON(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["call_session_id"], ["call_sessions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_call_extractions_call_session_id",
        "call_extractions",
        ["call_session_id"],
        unique=False,
    )

    op.create_table(
        "call_script_items",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("script_id", sa.String(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("prompt_text", sa.Text(), nullable=False),
        sa.Column("fact_key", sa.String(), nullable=False),
        sa.Column("hubspot_target", sa.String(), nullable=True),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column("help_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "fact_key = lower(fact_key)", name="ck_call_script_items_fact_key_lower"
        ),
        sa.ForeignKeyConstraint(["script_id"], ["call_scripts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("script_id", "fact_key", name="uq_call_script_items_fact"),
    )
    op.create_index(
        "ix_call_script_items_script_id",
        "call_script_items",
        ["script_id"],
        unique=False,
    )
    op.create_index(
        "ix_call_script_items_script_seq",
        "call_script_items",
        ["script_id", "sequence"],
        unique=False,
    )

    op.create_table(
        "call_session_scripts",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("call_session_id", sa.String(), nullable=False),
        sa.Column("script_id", sa.String(), nullable=False),
        sa.Column("product_key", sa.String(), nullable=False),
        sa.Column("attached_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["call_session_id"], ["call_sessions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["script_id"],
            ["call_scripts.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "call_session_id",
            "product_key",
            name="uq_call_session_scripts_session_product",
        ),
        sa.UniqueConstraint(
            "call_session_id",
            "script_id",
            name="uq_call_session_scripts_session_script",
        ),
    )
    op.create_index(
        "ix_call_session_scripts_call_session_id",
        "call_session_scripts",
        ["call_session_id"],
        unique=False,
    )

    op.create_table(
        "call_transcript_entries",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("call_session_id", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("external_id", sa.String(), nullable=True),
        sa.Column("participant_key", sa.String(), nullable=True),
        sa.Column("speaker_name", sa.String(), nullable=True),
        sa.Column("speaker_email", sa.String(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("language_code", sa.String(), nullable=True),
        sa.Column("start_offset_ms", sa.Integer(), nullable=True),
        sa.Column("end_offset_ms", sa.Integer(), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "source IN ('meet', 'fathom', 'manual')",
            name="ck_call_transcript_entries_source",
        ),
        sa.CheckConstraint(
            "speaker_email = lower(speaker_email)",
            name="ck_call_transcript_entries_email_lower",
        ),
        sa.ForeignKeyConstraint(
            ["call_session_id"], ["call_sessions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "call_session_id",
            "source",
            "external_id",
            name="uq_call_transcript_entries_source_external",
        ),
    )
    op.create_index(
        "ix_call_transcript_entries_call_session_id",
        "call_transcript_entries",
        ["call_session_id"],
        unique=False,
    )
    op.create_index(
        "ix_call_transcript_entries_session_seq",
        "call_transcript_entries",
        ["call_session_id", "sequence"],
        unique=False,
    )

    op.create_table(
        "chat_sessions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("folder_id", sa.String(), nullable=True),
        sa.Column("last_adk_invocation_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["folder_id"], ["chat_folders.id"], name="fk_chat_sessions_folder_id"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_chat_sessions_folder_id", "chat_sessions", ["folder_id"], unique=False
    )

    op.create_table(
        "commitment_nudges",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("commitment_id", sa.String(), nullable=False),
        sa.Column("from_email", sa.String(), nullable=False),
        sa.Column("to_email", sa.String(), nullable=False),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("hubspot_task_id", sa.String(), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "from_email = lower(from_email)", name="ck_commitment_nudges_from_lower"
        ),
        sa.CheckConstraint(
            "to_email = lower(to_email)", name="ck_commitment_nudges_to_lower"
        ),
        sa.ForeignKeyConstraint(
            ["commitment_id"], ["commitments.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_commitment_nudges_commitment_ack",
        "commitment_nudges",
        ["commitment_id", "acknowledged_at"],
        unique=False,
    )
    op.create_index(
        "ix_commitment_nudges_commitment_id",
        "commitment_nudges",
        ["commitment_id"],
        unique=False,
    )

    op.create_table(
        "department_drive_folder",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("department", sa.String(), nullable=True),
        sa.Column("folder_id", sa.String(), nullable=False),
        sa.Column("folder_name", sa.String(), nullable=False),
        sa.Column("web_view_link", sa.String(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("updated_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["department"], ["departments.name"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_department_drive_folder_department",
        "department_drive_folder",
        ["department"],
        unique=False,
    )
    op.create_index(
        "ix_department_drive_folder_dept_active",
        "department_drive_folder",
        ["department", "is_active"],
        unique=False,
    )

    op.create_table(
        "eos_quarters",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("start_date", sa.DateTime(), nullable=False),
        sa.Column("end_date", sa.DateTime(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["eos_organizations.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "eos_seats",
        sa.Column("department_name", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("reports_to_id", sa.Uuid(), nullable=True),
        sa.Column("responsibilities", sa.JSON(), nullable=True),
        sa.Column(
            "is_leadership",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["department_name"], ["departments.name"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["reports_to_id"],
            ["eos_seats.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "eos_vto_versions",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("version_name", sa.String(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["eos_organizations.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "graph_edges",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("src_key", sa.String(), nullable=False),
        sa.Column("dst_key", sa.String(), nullable=False),
        sa.Column("rel", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("method", sa.String(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence_ref", sa.String(), nullable=True),
        sa.Column("observed_at", sa.DateTime(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("invalidated_at", sa.DateTime(), nullable=True),
        sa.Column("attrs", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(
            ["dst_key"], ["graph_entities.entity_key"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["src_key"], ["graph_entities.entity_key"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "src_key", "dst_key", "rel", "source", name="uq_graph_edge"
        ),
    )
    op.create_index(
        "ix_graph_edges_dst_rel", "graph_edges", ["dst_key", "rel"], unique=False
    )
    op.create_index(
        "ix_graph_edges_src_rel", "graph_edges", ["src_key", "rel"], unique=False
    )

    op.create_table(
        "group_members",
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("group_id", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["groups.id"],
        ),
        sa.ForeignKeyConstraint(["user_email"], ["users.email"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_email", "group_id"),
    )
    op.create_table(
        "meeting_skills",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_email", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("template_id", sa.String(), nullable=True),
        sa.Column("tone", sa.String(), nullable=True),
        sa.Column("detail_level", sa.String(), nullable=False),
        sa.Column("meetings", sa.String(), nullable=False),
        sa.Column("window_days", sa.Integer(), nullable=False),
        sa.Column("deliver_as", sa.String(), nullable=False),
        sa.Column("exclude_personal", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("invocation_count", sa.Integer(), nullable=False),
        sa.Column("last_invoked_at", sa.DateTime(), nullable=True),
        sa.Column("created_via", sa.String(), nullable=False),
        sa.Column("source_session_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "created_via IN ('chat', 'upload', 'output')", name="ck_meeting_skills_via"
        ),
        sa.CheckConstraint(
            "deliver_as IN ('chat', 'email_draft_to_me', 'email_draft_to_attendees')",
            name="ck_meeting_skills_deliver_as",
        ),
        sa.CheckConstraint(
            "detail_level IN ('brief', 'standard', 'exhaustive')",
            name="ck_meeting_skills_detail",
        ),
        sa.CheckConstraint(
            "meetings IN ('one', 'many')", name="ck_meeting_skills_meetings"
        ),
        sa.CheckConstraint(
            "owner_email = lower(owner_email)", name="ck_meeting_skills_owner_lower"
        ),
        sa.ForeignKeyConstraint(
            ["template_id"], ["meeting_templates.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_email", "slug", name="uq_meeting_skills_slug"),
    )
    op.create_table(
        "offboard_items",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("record_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("detail", sa.String(), nullable=False),
        sa.Column("due", sa.String(), nullable=True),
        sa.Column("urgency", sa.String(), nullable=False),
        sa.Column("entity_kind", sa.String(), nullable=True),
        sa.Column("entity_id", sa.String(), nullable=True),
        sa.Column("source_links", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("resolved_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["record_id"], ["offboard_records.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_offboard_items_record_id", "offboard_items", ["record_id"], unique=False
    )
    op.create_index(
        "ix_offboard_items_record_status",
        "offboard_items",
        ["record_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_offboard_items_status", "offboard_items", ["status"], unique=False
    )

    op.create_table(
        "permissions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("role_name", sa.String(), nullable=False),
        sa.Column("resource", sa.String(), nullable=False),
        sa.Column("can_create", sa.Boolean(), nullable=True),
        sa.Column("can_read", sa.Boolean(), nullable=True),
        sa.Column("can_update", sa.Boolean(), nullable=True),
        sa.Column("can_delete", sa.Boolean(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(
            ["role_name"],
            ["roles.name"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "priority_memory",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("entity_kind", sa.String(), nullable=False),
        sa.Column("entity_id", sa.String(), nullable=False),
        sa.Column("priority_kind", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("snoozed_until", sa.DateTime(), nullable=True),
        sa.Column("last_priority_id", sa.String(), nullable=True),
        sa.Column("resolved_evidence", sa.JSON(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_state_change_at", sa.DateTime(), nullable=False),
        sa.Column("last_state_change_by", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["user_email"], ["users.email"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_email",
            "entity_kind",
            "entity_id",
            "priority_kind",
            name="uq_priority_memory_user_entity_kind",
        ),
    )
    op.create_index(
        "ix_priority_memory_snoozed_until",
        "priority_memory",
        ["user_email", "snoozed_until"],
        unique=False,
    )
    op.create_index(
        "ix_priority_memory_user_state",
        "priority_memory",
        ["user_email", "state"],
        unique=False,
    )

    op.create_table(
        "priority_playbook",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("department", sa.String(), nullable=True),
        sa.Column("body", sa.String(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("updated_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["department"], ["departments.name"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_priority_playbook_department",
        "priority_playbook",
        ["department"],
        unique=False,
    )
    op.create_index(
        "ix_priority_playbook_dept_active",
        "priority_playbook",
        ["department", "is_active"],
        unique=False,
    )

    op.create_table(
        "promise_events",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("promise_id", sa.String(), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.CheckConstraint(
            "kind IN ('created', 'suggested', 'accepted', 'rejected', 'due_moved', 'snoozed', 'slipping', 'headsup_drafted', 'headsup_sent', 'renegotiated', 'evidence_found', 'kept_confirmed', 'kept_denied', 'reopened', 'dropped', 'handed_off', 'promoted_eos', 'made_private', 'made_shared', 'edited')",
            name="ck_promise_events_kind",
        ),
        sa.ForeignKeyConstraint(["promise_id"], ["promises.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_promise_events_promise_at",
        "promise_events",
        ["promise_id", "at"],
        unique=False,
    )
    op.create_index(
        "ix_promise_events_promise_id", "promise_events", ["promise_id"], unique=False
    )

    op.create_table(
        "proposals",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_email", sa.String(), nullable=False),
        sa.Column("call_session_id", sa.String(), nullable=True),
        sa.Column("hubspot_company_id", sa.String(), nullable=True),
        sa.Column("hubspot_contact_id", sa.String(), nullable=True),
        sa.Column("hubspot_deal_id", sa.String(), nullable=True),
        sa.Column("company_name", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("brief", sa.JSON(), nullable=True),
        sa.Column("brief_source", sa.JSON(), nullable=True),
        sa.Column("brief_confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('brief', 'drafted', 'archived')", name="ck_proposals_status"
        ),
        sa.ForeignKeyConstraint(
            ["call_session_id"], ["call_sessions.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("call_session_id"),
    )
    op.create_index(
        "ix_proposals_hubspot_company_id",
        "proposals",
        ["hubspot_company_id"],
        unique=False,
    )
    op.create_index(
        "ix_proposals_hubspot_deal_id", "proposals", ["hubspot_deal_id"], unique=False
    )
    op.create_index(
        "ix_proposals_owner_email", "proposals", ["owner_email"], unique=False
    )
    op.create_index("ix_proposals_status", "proposals", ["status"], unique=False)

    op.create_table(
        "scheduled_task_runs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(), nullable=True),
        sa.Column("trigger", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("execution_name", sa.String(), nullable=True),
        sa.Column("chat_folder_id", sa.String(), nullable=True),
        sa.Column("chat_session_id", sa.String(), nullable=True),
        sa.Column("delivery", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"], ["scheduled_tasks.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_scheduled_task_runs_status_created",
        "scheduled_task_runs",
        ["status", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_scheduled_task_runs_task_id",
        "scheduled_task_runs",
        ["task_id"],
        unique=False,
    )

    op.create_table(
        "slack_credentials",
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("slack_user_id", sa.String(), nullable=False),
        sa.Column("slack_team_id", sa.String(), nullable=False),
        sa.Column("slack_team_name", sa.String(), nullable=True),
        sa.Column("access_token", sa.String(), nullable=False),
        sa.Column("bot_token", sa.String(), nullable=True),
        sa.Column("scope", sa.String(), nullable=True),
        sa.Column("bot_scope", sa.String(), nullable=True),
        sa.Column("connected_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_email"], ["users.email"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_email"),
    )
    op.create_table(
        "st_interviews",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("section_cursor", sa.String(), nullable=True),
        sa.Column("sections_completed", sa.JSON(), nullable=False),
        sa.Column("answers", sa.JSON(), nullable=False),
        sa.Column("adk_session_id", sa.String(), nullable=True),
        sa.Column("profile_id", sa.String(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('in_progress', 'completed', 'abandoned')",
            name="ck_st_interview_status",
        ),
        sa.CheckConstraint(
            "user_email = lower(user_email)", name="ck_st_interview_email_lower"
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["st_profiles.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_st_interviews_email_lower",
        "st_interviews",
        [sa.literal_column("lower(user_email)")],
        unique=False,
    )
    op.create_index(
        "ix_st_interviews_user_email", "st_interviews", ["user_email"], unique=False
    )
    op.create_index(
        "ix_st_interviews_user_status",
        "st_interviews",
        ["user_email", "status"],
        unique=False,
    )

    op.create_table(
        "st_person_skills",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("skill_key", sa.String(), nullable=False),
        sa.Column("level", sa.String(), nullable=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("confirmed", sa.Boolean(), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("evidence_ref", sa.String(), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "level IS NULL OR level IN ('familiar', 'working', 'strong', 'expert')",
            name="ck_st_person_skill_level",
        ),
        sa.CheckConstraint(
            "source IN ('declared', 'inferred', 'manager')",
            name="ck_st_person_skill_source",
        ),
        sa.CheckConstraint(
            "user_email = lower(user_email)", name="ck_st_person_skill_email_lower"
        ),
        sa.ForeignKeyConstraint(
            ["skill_key"], ["st_skills_taxonomy.skill_key"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_email", "skill_key", name="uq_st_person_skill"),
    )
    op.create_index(
        "ix_st_person_skills_email_lower",
        "st_person_skills",
        [sa.literal_column("lower(user_email)")],
        unique=False,
    )
    op.create_index(
        "ix_st_person_skills_user_confirmed",
        "st_person_skills",
        ["user_email", "confirmed"],
        unique=False,
    )
    op.create_index(
        "ix_st_person_skills_user_email",
        "st_person_skills",
        ["user_email"],
        unique=False,
    )

    op.create_table(
        "st_uploads",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("doc_id", sa.String(), nullable=True),
        sa.Column("gcs_uri", sa.String(), nullable=True),
        sa.Column("original_filename", sa.String(), nullable=True),
        sa.Column("framework", sa.String(), nullable=True),
        sa.Column("parsed", sa.JSON(), nullable=True),
        sa.Column("parse_error", sa.String(), nullable=True),
        sa.Column("review_status", sa.String(), nullable=False),
        sa.Column("merged_profile_id", sa.String(), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(), nullable=False),
        sa.Column("parsed_at", sa.DateTime(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "framework IS NULL OR framework IN ('disc', 'big_five', 'type16', 'strengths', 'motivators', 'enneagram')",
            name="ck_st_upload_framework",
        ),
        sa.CheckConstraint(
            "review_status IN ('pending', 'confirmed', 'rejected', 'merged')",
            name="ck_st_upload_review_status",
        ),
        sa.CheckConstraint(
            "user_email = lower(user_email)", name="ck_st_upload_email_lower"
        ),
        sa.ForeignKeyConstraint(
            ["merged_profile_id"], ["st_profiles.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_st_uploads_email_lower",
        "st_uploads",
        [sa.literal_column("lower(user_email)")],
        unique=False,
    )
    op.create_index(
        "ix_st_uploads_user_email", "st_uploads", ["user_email"], unique=False
    )
    op.create_index(
        "ix_st_uploads_user_review",
        "st_uploads",
        ["user_email", "review_status"],
        unique=False,
    )

    op.create_table(
        "user_preferences",
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("writing_style", sa.String(), nullable=True),
        sa.Column("tone", sa.String(), nullable=True),
        sa.Column("preferred_greeting", sa.String(), nullable=True),
        sa.Column("email_signature", sa.String(), nullable=True),
        sa.Column("additional_instructions", sa.String(), nullable=True),
        sa.Column("timezone", sa.String(), nullable=True),
        sa.Column("working_hours_start", sa.String(), nullable=True),
        sa.Column("working_hours_end", sa.String(), nullable=True),
        sa.Column("muted_notification_kinds", sa.JSON(), nullable=True),
        sa.Column("role_title", sa.String(), nullable=True),
        sa.Column("about_me", sa.String(), nullable=True),
        sa.Column("chat_verbosity", sa.String(), nullable=True),
        sa.Column("response_language", sa.String(), nullable=True),
        sa.Column("priorities_focus", sa.String(), nullable=True),
        sa.Column("priorities_max", sa.Integer(), nullable=True),
        sa.Column("priorities_sources", sa.JSON(), nullable=True),
        sa.Column("priorities_muted_topics", sa.JSON(), nullable=True),
        sa.Column("eos_team_optins", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_email"], ["users.email"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_email"),
    )
    op.create_table(
        "user_roles",
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("role_name", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["role_name"],
            ["roles.name"],
        ),
        sa.ForeignKeyConstraint(["user_email"], ["users.email"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_email", "role_name"),
    )
    op.create_table(
        "asset_versions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("doc_id", sa.String(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("source_path", sa.String(), nullable=True),
        sa.Column("original_filename", sa.String(), nullable=True),
        sa.Column("content_hash", sa.String(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("uploaded_by", sa.String(), nullable=True),
        sa.Column("note", sa.String(), nullable=True),
        sa.Column("change_summary", sa.Text(), nullable=True),
        sa.Column("restored_from", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["doc_id"], ["assets.doc_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "doc_id", "version_number", name="uq_asset_versions_doc_ver"
        ),
    )
    op.create_index(
        "ix_asset_versions_doc_id", "asset_versions", ["doc_id"], unique=False
    )

    op.create_table(
        "call_extraction_items",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("extraction_id", sa.String(), nullable=False),
        sa.Column("call_session_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("target_object", sa.String(), nullable=True),
        sa.Column("target_id", sa.String(), nullable=True),
        sa.Column("target_property", sa.String(), nullable=True),
        sa.Column("proposed_value", sa.Text(), nullable=True),
        sa.Column("current_value", sa.Text(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("evidence_text", sa.Text(), nullable=False),
        sa.Column("evidence_entry_id", sa.String(), nullable=True),
        sa.Column("needs_new_option", sa.String(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("is_overwrite", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("decided_by", sa.String(), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("edited_value", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('vendor_field', 'lead', 'task', 'sow_request', 'stage_signal', 'contract_expiry')",
            name="ck_call_extraction_items_kind",
        ),
        sa.CheckConstraint(
            "status IN ('proposed', 'accepted', 'rejected', 'superseded', 'written', 'write_failed')",
            name="ck_call_extraction_items_status",
        ),
        sa.CheckConstraint(
            "decided_by = lower(decided_by)",
            name="ck_call_extraction_items_decided_lower",
        ),
        sa.ForeignKeyConstraint(
            ["call_session_id"], ["call_sessions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["evidence_entry_id"], ["call_transcript_entries.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["extraction_id"], ["call_extractions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_call_extraction_items_call_session_id",
        "call_extraction_items",
        ["call_session_id"],
        unique=False,
    )
    op.create_index(
        "ix_call_extraction_items_extraction_id",
        "call_extraction_items",
        ["extraction_id"],
        unique=False,
    )
    op.create_index(
        "ix_call_extraction_items_session_status",
        "call_extraction_items",
        ["call_session_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_call_extraction_items_status",
        "call_extraction_items",
        ["status"],
        unique=False,
    )

    op.create_table(
        "chat_messages",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("content", sa.String(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["chat_sessions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "chunks",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("doc_id", sa.String(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("text", sa.String(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["doc_id"],
            ["assets.doc_id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "eos_people",
        sa.Column("first_name", sa.String(), nullable=False),
        sa.Column("last_name", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("seat_id", sa.Uuid(), nullable=True),
        sa.Column("google_id", sa.String(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["seat_id"],
            ["eos_seats.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email", name="uq_eos_people_email"),
    )
    op.create_index("ix_eos_people_email", "eos_people", ["email"], unique=False)
    op.create_index(
        "ix_eos_people_google_id", "eos_people", ["google_id"], unique=False
    )

    op.create_table(
        "eos_vto_sections",
        sa.Column("vto_version_id", sa.Uuid(), nullable=False),
        sa.Column(
            "section_type",
            sa.Enum(
                "vision",
                "mission",
                "values",
                "focus",
                "10_year_target",
                "3_year_picture",
                "1_year_plan",
                "quarterly_rocks",
                "issues_list",
                name="eos_vto_section_type",
            ),
            nullable=False,
        ),
        sa.Column("content", sa.JSON(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["vto_version_id"], ["eos_vto_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "meeting_participants",
        sa.Column("doc_id", sa.String(), nullable=False),
        sa.Column("participant_email", sa.String(), nullable=False),
        sa.Column("meeting_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["doc_id"], ["assets.doc_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("doc_id", "participant_email"),
    )
    op.create_index(
        "ix_meeting_participants_meeting_id",
        "meeting_participants",
        ["meeting_id"],
        unique=False,
    )
    op.create_index(
        "ix_meeting_participants_participant_email",
        "meeting_participants",
        ["participant_email"],
        unique=False,
    )

    op.create_table(
        "meeting_routines",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_email", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("skill_id", sa.String(), nullable=False),
        sa.Column("trigger_kind", sa.String(), nullable=False),
        sa.Column("filter", sa.JSON(), nullable=False),
        sa.Column("deliver_slack", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("paused_reason", sa.String(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("fire_count", sa.Integer(), nullable=False),
        sa.Column("last_fired_at", sa.DateTime(), nullable=True),
        sa.Column("last_status", sa.String(), nullable=True),
        sa.Column("active_since", sa.DateTime(), nullable=False),
        sa.Column("source_session_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'error_paused')",
            name="ck_meeting_routines_status",
        ),
        sa.CheckConstraint(
            "trigger_kind IN ('transcript_ready')", name="ck_meeting_routines_trigger"
        ),
        sa.CheckConstraint(
            "owner_email = lower(owner_email)", name="ck_meeting_routines_owner_lower"
        ),
        sa.ForeignKeyConstraint(
            ["skill_id"], ["meeting_skills.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_meeting_routines_owner_status",
        "meeting_routines",
        ["owner_email", "status"],
        unique=False,
    )

    op.create_table(
        "proposal_versions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("proposal_id", sa.String(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("brief", sa.JSON(), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("instruction", sa.Text(), nullable=True),
        sa.Column("change_summary", sa.Text(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["proposal_id"], ["proposals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "proposal_id", "version_no", name="uq_proposal_versions_no"
        ),
    )
    op.create_index(
        "ix_proposal_versions_proposal_id",
        "proposal_versions",
        ["proposal_id"],
        unique=False,
    )

    op.create_table(
        "call_hubspot_writes",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("extraction_item_id", sa.String(), nullable=True),
        sa.Column("call_session_id", sa.String(), nullable=True),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("method", sa.String(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("request_body", sa.JSON(), nullable=True),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", sa.JSON(), nullable=True),
        sa.Column("hubspot_object_id", sa.String(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("succeeded_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["call_session_id"], ["call_sessions.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["extraction_item_id"], ["call_extraction_items.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_call_hubspot_writes_idempotency"
        ),
    )
    op.create_index(
        "ix_call_hubspot_writes_call_session_id",
        "call_hubspot_writes",
        ["call_session_id"],
        unique=False,
    )
    op.create_index(
        "ix_call_hubspot_writes_extraction_item_id",
        "call_hubspot_writes",
        ["extraction_item_id"],
        unique=False,
    )

    op.create_table(
        "eos_gwc_evaluations",
        sa.Column("person_id", sa.Uuid(), nullable=False),
        sa.Column("seat_id", sa.Uuid(), nullable=False),
        sa.Column("get_it", sa.Boolean(), nullable=False),
        sa.Column("want_it", sa.Boolean(), nullable=False),
        sa.Column("capacity", sa.Boolean(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["eos_people.id"],
        ),
        sa.ForeignKeyConstraint(
            ["seat_id"],
            ["eos_seats.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "eos_l10_meetings",
        sa.Column("department_name", sa.String(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("day_of_week", sa.Integer(), nullable=True),
        sa.Column("start_time", sa.String(), nullable=True),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("manager_id", sa.Uuid(), nullable=True),
        sa.Column(
            "is_exec_meeting",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("secondary_link_label", sa.String(), nullable=True),
        sa.Column("secondary_link_url", sa.String(), nullable=True),
        sa.Column("roster_emails", sa.JSON(), nullable=True),
        sa.Column("google_event_id", sa.String(), nullable=True),
        sa.Column("last_imported_date", sa.Date(), nullable=True),
        sa.Column("calendar_state", sa.String(), nullable=True),
        sa.Column("next_occurrence_at", sa.DateTime(), nullable=True),
        sa.Column("calendar_checked_at", sa.DateTime(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["department_name"], ["departments.name"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["manager_id"],
            ["eos_people.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_eos_l10_meetings_google_event_id",
        "eos_l10_meetings",
        ["google_event_id"],
        unique=False,
    )

    op.create_table(
        "eos_processes",
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=True),
        sa.Column("documentation_url", sa.String(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["eos_people.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "eos_rocks",
        sa.Column("quarter_id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("on_track", "off_track", "done", name="eos_rock_status"),
            nullable=False,
        ),
        sa.Column(
            "visibility",
            sa.Enum("company", "department", "individual", name="eos_visibility"),
            nullable=False,
        ),
        sa.Column("is_exec", sa.Boolean(), nullable=False),
        sa.Column("department_name", sa.String(), nullable=True),
        sa.Column("milestones", sa.JSON(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["eos_people.id"],
        ),
        sa.ForeignKeyConstraint(
            ["quarter_id"],
            ["eos_quarters.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_eos_rocks_department_name", "eos_rocks", ["department_name"], unique=False
    )
    op.create_index("ix_eos_rocks_owner", "eos_rocks", ["owner_id"], unique=False)
    op.create_index(
        "ix_eos_rocks_quarter_status",
        "eos_rocks",
        ["quarter_id", "status"],
        unique=False,
    )

    op.create_table(
        "eos_scorecard_metrics",
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("department_name", sa.String(), nullable=True),
        sa.Column("is_exec", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("goal_description", sa.Text(), nullable=True),
        sa.Column("target_value", sa.String(), nullable=True),
        sa.Column("unit", sa.String(), nullable=True),
        sa.Column(
            "value_type",
            sa.String(),
            server_default=sa.text("'number'"),
            nullable=False,
        ),
        sa.Column("comparator", sa.String(), nullable=True),
        sa.Column("frequency", sa.String(), nullable=False),
        sa.Column("on_track", sa.Boolean(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["department_name"], ["departments.name"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["eos_people.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "meeting_routine_fires",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("routine_id", sa.String(), nullable=False),
        sa.Column("doc_id", sa.String(), nullable=False),
        sa.Column("owner_email", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("skill_run_id", sa.String(), nullable=True),
        sa.Column("chat_session_id", sa.String(), nullable=True),
        sa.Column("delivery", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'skipped')",
            name="ck_meeting_routine_fires_status",
        ),
        sa.CheckConstraint(
            "owner_email = lower(owner_email)",
            name="ck_meeting_routine_fires_owner_lower",
        ),
        sa.ForeignKeyConstraint(
            ["routine_id"], ["meeting_routines.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "routine_id", "doc_id", name="uq_meeting_routine_fires_doc"
        ),
    )
    op.create_index(
        "ix_meeting_routine_fires_status_created",
        "meeting_routine_fires",
        ["status", "created_at"],
        unique=False,
    )

    op.create_table(
        "eos_issues",
        sa.Column("owner_id", sa.Uuid(), nullable=True),
        sa.Column("department_name", sa.String(), nullable=True),
        sa.Column("is_exec", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("l10_meeting_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "priority",
            sa.Enum("low", "medium", "high", name="eos_issue_priority"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum("open", "solved", "archived", name="eos_issue_status"),
            nullable=False,
        ),
        sa.Column("solved_at", sa.DateTime(), nullable=True),
        sa.Column("source_rock_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["department_name"], ["departments.name"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["l10_meeting_id"],
            ["eos_l10_meetings.id"],
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["eos_people.id"],
        ),
        sa.ForeignKeyConstraint(
            ["source_rock_id"],
            ["eos_rocks.id"],
            name="fk_eos_issues_source_rock_id",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "eos_l10_inputs",
        sa.Column("meeting_id", sa.Uuid(), nullable=False),
        sa.Column(
            "kind", sa.Enum("headline", "idea", name="l10inputkind"), nullable=False
        ),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("discussed", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["meeting_id"], ["eos_l10_meetings.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_eos_l10_inputs_meeting_kind",
        "eos_l10_inputs",
        ["meeting_id", "kind"],
        unique=False,
    )

    op.create_table(
        "eos_l10_sessions",
        sa.Column("meeting_id", sa.Uuid(), nullable=False),
        sa.Column("actual_start", sa.DateTime(), nullable=False),
        sa.Column("actual_end", sa.DateTime(), nullable=True),
        sa.Column("average_rating", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("recap", sa.Text(), nullable=True),
        sa.Column("current_step", sa.Integer(), server_default="0", nullable=False),
        sa.Column("step_started_at", sa.DateTime(), nullable=True),
        sa.Column("is_paused", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("state_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "auto_closed", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        sa.Column("summary_email_sent_at", sa.DateTime(), nullable=True),
        sa.Column("summary_email_doc_id", sa.String(), nullable=True),
        sa.Column("summary_email_recipients", sa.JSON(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["meeting_id"], ["eos_l10_meetings.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_eos_l10_sessions_meeting_start",
        "eos_l10_sessions",
        ["meeting_id", "actual_start"],
        unique=False,
    )
    # Postgres only, like the model's ``ddl_if``: at most one LIVE session per
    # meeting. SQLite (tests, local) relies on ``start_session``'s retry.
    if op.get_bind().dialect.name == "postgresql":
        op.create_index(
            "uq_eos_l10_sessions_one_live_per_meeting",
            "eos_l10_sessions",
            ["meeting_id"],
            unique=True,
            postgresql_where=sa.text("actual_end IS NULL AND is_deleted = false"),
        )

    op.create_table(
        "eos_rock_milestones",
        sa.Column("rock_id", sa.Uuid(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("completed", sa.Boolean(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["rock_id"], ["eos_rocks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_eos_rock_milestones_rock", "eos_rock_milestones", ["rock_id"], unique=False
    )

    op.create_table(
        "eos_rock_updates",
        sa.Column("rock_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("on_track", "off_track", "done", name="rockstatus"),
            nullable=False,
        ),
        sa.Column("percent_complete", sa.Integer(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("update_date", sa.Date(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "percent_complete >= 0 AND percent_complete <= 100",
            name="ck_eos_rock_updates_percent_range",
        ),
        sa.ForeignKeyConstraint(["rock_id"], ["eos_rocks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_eos_rock_updates_rock_date",
        "eos_rock_updates",
        ["rock_id", "update_date"],
        unique=False,
    )

    op.create_table(
        "eos_scorecard_entries",
        sa.Column("metric_id", sa.Uuid(), nullable=False),
        sa.Column("week_start_date", sa.DateTime(), nullable=False),
        sa.Column("value", sa.String(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["metric_id"], ["eos_scorecard_metrics.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_eos_scorecard_entries_metric_week",
        "eos_scorecard_entries",
        ["metric_id", "week_start_date"],
        unique=True,
    )

    op.create_table(
        "eos_issue_notes",
        sa.Column("issue_id", sa.Uuid(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["issue_id"], ["eos_issues.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_eos_issue_notes_issue_created",
        "eos_issue_notes",
        ["issue_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "eos_l10_attendance",
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=True),
        sa.Column("rating_comment", sa.Text(), nullable=True),
        sa.Column("attended", sa.Boolean(), nullable=False),
        sa.Column("checkin_personal_content", sa.Text(), nullable=True),
        sa.Column("checkin_personal_rating", sa.Integer(), nullable=True),
        sa.Column("checkin_professional_content", sa.Text(), nullable=True),
        sa.Column("checkin_professional_rating", sa.Integer(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["eos_people.id"],
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["eos_l10_sessions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "eos_l10_signals",
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"], ["eos_l10_sessions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_eos_l10_signals_session_created",
        "eos_l10_signals",
        ["session_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "eos_todos",
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("rock_id", sa.Uuid(), nullable=True),
        sa.Column("source_issue_id", sa.Uuid(), nullable=True),
        sa.Column("department_name", sa.String(), nullable=True),
        sa.Column("is_exec", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("l10_meeting_id", sa.Uuid(), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("due_date", sa.DateTime(), nullable=True),
        sa.Column(
            "status", sa.Enum("pending", "done", name="eos_todo_status"), nullable=False
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["l10_meeting_id"],
            ["eos_l10_meetings.id"],
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["eos_people.id"],
        ),
        sa.ForeignKeyConstraint(
            ["rock_id"],
            ["eos_rocks.id"],
        ),
        sa.ForeignKeyConstraint(
            ["source_issue_id"],
            ["eos_issues.id"],
            name="fk_eos_todos_source_issue_id",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "eos_todo_notes",
        sa.Column("todo_id", sa.Uuid(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["todo_id"], ["eos_todos.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_eos_todo_notes_todo_created",
        "eos_todo_notes",
        ["todo_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    # destructive-ok: the baseline's downgrade empties the schema it built.
    # Dropping a table drops its indexes and constraints with it; children go
    # before the tables they reference.
    op.drop_table("eos_todo_notes")
    op.drop_table("eos_todos")
    op.drop_table("eos_l10_signals")
    op.drop_table("eos_l10_attendance")
    op.drop_table("eos_issue_notes")
    op.drop_table("eos_scorecard_entries")
    op.drop_table("eos_rock_updates")
    op.drop_table("eos_rock_milestones")
    op.drop_table("eos_l10_sessions")
    op.drop_table("eos_l10_inputs")
    op.drop_table("eos_issues")
    op.drop_table("meeting_routine_fires")
    op.drop_table("eos_scorecard_metrics")
    op.drop_table("eos_rocks")
    op.drop_table("eos_processes")
    op.drop_table("eos_l10_meetings")
    op.drop_table("eos_gwc_evaluations")
    op.drop_table("call_hubspot_writes")
    op.drop_table("proposal_versions")
    op.drop_table("meeting_routines")
    op.drop_table("meeting_participants")
    op.drop_table("eos_vto_sections")
    op.drop_table("eos_people")
    op.drop_table("chunks")
    op.drop_table("chat_messages")
    op.drop_table("call_extraction_items")
    op.drop_table("asset_versions")
    op.drop_table("user_roles")
    op.drop_table("user_preferences")
    op.drop_table("st_uploads")
    op.drop_table("st_person_skills")
    op.drop_table("st_interviews")
    op.drop_table("slack_credentials")
    op.drop_table("scheduled_task_runs")
    op.drop_table("proposals")
    op.drop_table("promise_events")
    op.drop_table("priority_playbook")
    op.drop_table("priority_memory")
    op.drop_table("permissions")
    op.drop_table("offboard_items")
    op.drop_table("meeting_skills")
    op.drop_table("group_members")
    op.drop_table("graph_edges")
    op.drop_table("eos_vto_versions")
    op.drop_table("eos_seats")
    op.drop_table("eos_quarters")
    op.drop_table("department_drive_folder")
    op.drop_table("commitment_nudges")
    op.drop_table("chat_sessions")
    op.drop_table("call_transcript_entries")
    op.drop_table("call_session_scripts")
    op.drop_table("call_script_items")
    op.drop_table("call_extractions")
    op.drop_table("assets")
    op.drop_table("agent_recommendation_feedback")
    op.drop_table("access_policies")
    op.drop_table("xray_timeline_events")
    op.drop_table("xray_snapshots")
    op.drop_table("workspace_settings")
    op.drop_table("whats_new_opens")
    op.drop_table("users")
    op.drop_table("user_voice_shortcuts")
    op.drop_table("user_tour_progress")
    op.drop_table("user_recent_touch")
    op.drop_table("user_bookmark")
    op.drop_table("topics")
    op.drop_table("st_skills_taxonomy")
    op.drop_table("st_profiles")
    op.drop_table("st_documents")
    op.drop_table("scheduled_tasks")
    op.drop_table("roles")
    op.drop_table("proposal_templates")
    op.drop_table("promises")
    op.drop_table("promise_scans")
    op.drop_table("priority_events")
    op.drop_table("priorities_runs")
    op.drop_table("ooo_periods")
    op.drop_table("offboard_records")
    op.drop_table("notifications")
    op.drop_table("meeting_templates")
    op.drop_table("meeting_skill_runs")
    op.drop_table("meeting_prep_runs")
    op.drop_table("meeting_definition_versions")
    op.drop_table("mcp_connections")
    op.drop_table("loop_scan_runs")
    op.drop_table("loop_mutes")
    op.drop_table("loop_judgements")
    op.drop_table("loop_items")
    op.drop_table("loop_digest_posts")
    op.drop_table("login_events")
    op.drop_table("llm_usage_events")
    op.drop_table("interaction_logs")
    op.drop_table("ingestion_council_verdicts")
    op.drop_table("home_cards")
    op.drop_table("groups")
    op.drop_table("graph_entities")
    op.drop_table("feature_access")
    op.drop_table("eos_rock_carryovers")
    op.drop_table("eos_organizations")
    op.drop_table("eos_ingest_state")
    op.drop_table("eos_events")
    op.drop_table("drive_label_priorities")
    op.drop_table("digest_logs")
    op.drop_table("departments")
    op.drop_table("deal_defers")
    op.drop_table("crm_sync_suggestions")
    op.drop_table("crm_sync_runs")
    op.drop_table("commitments")
    op.drop_table("commitment_stage_lags")
    op.drop_table("commitment_sla_configs")
    op.drop_table("chat_uploads")
    op.drop_table("chat_upload_publications")
    op.drop_table("chat_folders")
    op.drop_table("catalog_items")
    op.drop_table("call_sessions")
    op.drop_table("call_scripts")
    op.drop_table("call_event_subscriptions")
    op.drop_table("call_company_product_defaults")
    op.drop_table("browser_connections")
    op.drop_table("audit_logs")
    op.drop_table("attendee_profiles")
    op.drop_table("assistant_feedback")
    op.drop_table("agent_recommendations")
    op.drop_table("activity_daily")
    if op.get_bind().dialect.name == "postgresql":
        for name in ENUM_TYPES:
            op.execute(sa.text(f'DROP TYPE IF EXISTS "{name}"'))

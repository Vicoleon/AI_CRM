"""Independent control schema, with no default rows.

Revision ID: 0001_control_baseline
Revises:
"""

import sqlalchemy as sa

from alembic import op

revision = "0001_control_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provisioning_steps",
        sa.Column("workspace_slug", sa.String(length=63), nullable=False),
        sa.Column("step", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("db_name", sa.String(length=63), nullable=True),
        sa.Column("db_user", sa.String(length=63), nullable=True),
        sa.Column("db_password_secret_ref", sa.String(length=512), nullable=True),
        sa.Column("schema_revision", sa.String(length=32), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("workspace_slug", "step"),
    )
    op.create_table(
        "workspaces",
        sa.Column("slug", sa.String(length=63), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("db_name", sa.String(length=63), nullable=True),
        sa.Column("db_user", sa.String(length=63), nullable=True),
        sa.Column("db_password_secret_ref", sa.String(length=512), nullable=True),
        sa.Column("time_zone", sa.String(length=100), nullable=False),
        sa.Column("storage_prefix", sa.String(length=255), nullable=True),
        sa.Column("company_details", sa.JSON(), nullable=False),
        sa.Column("branding", sa.JSON(), nullable=False),
        sa.Column("google_service_account", sa.String(length=320), nullable=True),
        sa.Column("google_subject", sa.String(length=320), nullable=True),
        sa.Column("corpus_ids", sa.JSON(), nullable=False),
        sa.Column("schema_revision", sa.String(length=32), nullable=True),
        sa.Column("freeze_windows", sa.JSON(), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'suspended', 'provisioning')",
            name="ck_workspace_status",
        ),
        sa.CheckConstraint(
            "slug = lower(trim(slug)) AND length(slug) > 0",
            name="ck_workspace_slug_normalized",
        ),
        sa.PrimaryKeyConstraint("slug"),
    )
    op.create_table(
        "platform_admins",
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("home_workspace_slug", sa.String(length=63), nullable=False),
        sa.CheckConstraint(
            "email = lower(trim(email)) AND length(email) > 0",
            name="ck_platform_admin_email_normalized",
        ),
        sa.ForeignKeyConstraint(
            ["home_workspace_slug"],
            ["workspaces.slug"],
        ),
        sa.PrimaryKeyConstraint("email"),
    )
    op.create_index(
        "ix_platform_admins_home_workspace_slug",
        "platform_admins",
        ["home_workspace_slug"],
        unique=False,
    )
    op.create_table(
        "slack_installations",
        sa.Column("app_id", sa.String(length=100), nullable=False),
        sa.Column("team_id", sa.String(length=100), nullable=False),
        sa.Column("workspace_slug", sa.String(length=63), nullable=False),
        sa.Column("signing_secret_ref", sa.String(length=512), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_slug"],
            ["workspaces.slug"],
        ),
        sa.PrimaryKeyConstraint("app_id", "team_id"),
    )
    op.create_index(
        op.f("ix_slack_installations_workspace_slug"),
        "slack_installations",
        ["workspace_slug"],
        unique=False,
    )
    op.create_table(
        "workspace_connectors",
        sa.Column("workspace_slug", sa.String(length=63), nullable=False),
        sa.Column("connector", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("secret_refs", sa.JSON(), nullable=False),
        sa.Column("connected_by", sa.String(length=320), nullable=True),
        sa.Column("last_check", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["workspace_slug"],
            ["workspaces.slug"],
        ),
        sa.PrimaryKeyConstraint("workspace_slug", "connector"),
    )
    op.create_table(
        "workspace_corpora",
        sa.Column("workspace_slug", sa.String(length=63), nullable=False),
        sa.Column("kind", sa.String(length=100), nullable=False),
        sa.Column("audience", sa.String(length=320), nullable=False),
        sa.Column("corpus_id", sa.String(length=512), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_slug"],
            ["workspaces.slug"],
        ),
        sa.PrimaryKeyConstraint("workspace_slug", "kind", "audience"),
    )
    op.create_table(
        "workspace_domains",
        sa.Column("domain", sa.String(length=253), nullable=False),
        sa.Column("workspace_slug", sa.String(length=63), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "domain = lower(trim(domain)) AND length(domain) > 0",
            name="ck_workspace_domain_normalized",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_slug"],
            ["workspaces.slug"],
        ),
        sa.PrimaryKeyConstraint("domain"),
    )
    op.create_index(
        op.f("ix_workspace_domains_workspace_slug"),
        "workspace_domains",
        ["workspace_slug"],
        unique=False,
    )
    op.create_index(
        "uq_workspace_domains_primary",
        "workspace_domains",
        ["workspace_slug"],
        unique=True,
        sqlite_where=sa.text("is_primary = 1"),
        postgresql_where=sa.text("is_primary"),
    )
    op.create_table(
        "workspace_modules",
        sa.Column("workspace_slug", sa.String(length=63), nullable=False),
        sa.Column("module", sa.String(length=100), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_slug"],
            ["workspaces.slug"],
        ),
        sa.PrimaryKeyConstraint("workspace_slug", "module"),
    )
    op.create_table(
        "support_sessions",
        sa.Column("sid", sa.String(length=128), nullable=False),
        sa.Column("actor", sa.String(length=320), nullable=False),
        sa.Column("target_workspace_slug", sa.String(length=63), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["actor"],
            ["platform_admins.email"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_workspace_slug"],
            ["workspaces.slug"],
        ),
        sa.PrimaryKeyConstraint("sid"),
    )
    op.create_index(
        "ix_support_sessions_actor", "support_sessions", ["actor"], unique=False
    )
    op.create_index(
        op.f("ix_support_sessions_target_workspace_slug"),
        "support_sessions",
        ["target_workspace_slug"],
        unique=False,
    )


def downgrade() -> None:
    # destructive-ok: explicit downgrade removes the tables created by this baseline.
    op.drop_index(
        op.f("ix_support_sessions_target_workspace_slug"), table_name="support_sessions"
    )
    op.drop_index("ix_support_sessions_actor", table_name="support_sessions")
    op.drop_table("support_sessions")
    op.drop_table("workspace_modules")
    op.drop_index(
        op.f("ix_workspace_domains_workspace_slug"), table_name="workspace_domains"
    )
    op.drop_index("uq_workspace_domains_primary", table_name="workspace_domains")
    op.drop_table("workspace_domains")
    op.drop_table("workspace_corpora")
    op.drop_table("workspace_connectors")
    op.drop_index(
        op.f("ix_slack_installations_workspace_slug"), table_name="slack_installations"
    )
    op.drop_table("slack_installations")
    op.drop_index(
        "ix_platform_admins_home_workspace_slug", table_name="platform_admins"
    )
    op.drop_table("platform_admins")
    op.drop_table("workspaces")
    op.drop_table("provisioning_steps")

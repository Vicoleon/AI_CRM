"""Add explicit per-user Gmail OAuth connections.

Revision ID: 0005_gmail_accounts
Revises: 0004_extension_hosted_domain
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_gmail_accounts"
down_revision: str | Sequence[str] | None = "0004_extension_hosted_domain"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _lock_timeout() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")


def upgrade() -> None:
    _lock_timeout()
    op.create_table(
        "gmail_accounts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "owner_email",
            sa.String(),
            sa.ForeignKey("users.email", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("google_sub", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("refresh_token", sa.String(), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("connected_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "owner_email", "google_sub", name="uq_gmail_account_owner_sub"
        ),
        sa.UniqueConstraint(
            "owner_email", "email", name="uq_gmail_account_owner_email"
        ),
    )
    op.create_index("ix_gmail_accounts_owner", "gmail_accounts", ["owner_email"])
    op.create_table(
        "gmail_oauth_attempts",
        sa.Column("nonce_hash", sa.String(length=64), primary_key=True),
        sa.Column("browser_nonce_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "owner_email",
            sa.String(),
            sa.ForeignKey("users.email", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("code_verifier", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_gmail_oauth_attempts_owner", "gmail_oauth_attempts", ["owner_email"]
    )


def downgrade() -> None:
    _lock_timeout()
    op.drop_index("ix_gmail_oauth_attempts_owner", table_name="gmail_oauth_attempts")
    op.drop_table("gmail_oauth_attempts")
    op.drop_index("ix_gmail_accounts_owner", table_name="gmail_accounts")
    op.drop_table("gmail_accounts")

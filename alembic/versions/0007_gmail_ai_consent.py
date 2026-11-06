"""Require renewed owner consent when personal-mail AI provider changes.

Revision ID: 0007_gmail_ai_consent
Revises: 0006_call_extraction_lease
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_gmail_ai_consent"
down_revision: str | Sequence[str] | None = "0006_call_extraction_lease"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _lock_timeout() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")


def upgrade() -> None:
    _lock_timeout()
    op.add_column("gmail_accounts", sa.Column("ai_provider_ack", sa.String(64)))
    op.add_column("gmail_accounts", sa.Column("ai_provider_notice", sa.String(64)))


def downgrade() -> None:
    _lock_timeout()
    op.drop_column("gmail_accounts", "ai_provider_notice")
    op.drop_column("gmail_accounts", "ai_provider_ack")

"""Claim billable call extraction across service instances.

Revision ID: 0006_call_extraction_lease
Revises: 0005_gmail_accounts
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_call_extraction_lease"
down_revision: str | Sequence[str] | None = "0005_gmail_accounts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _lock_timeout() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")


def upgrade() -> None:
    _lock_timeout()
    op.add_column(
        "call_sessions",
        sa.Column("extraction_claimed_until", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "call_sessions",
        sa.Column("extraction_claim_id", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "call_sessions",
        sa.Column("extraction_manual_retry_after", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "call_sessions",
        sa.Column("last_extracted_entry_count", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    _lock_timeout()
    op.drop_column("call_sessions", "last_extracted_entry_count")
    op.drop_column("call_sessions", "extraction_manual_retry_after")
    op.drop_column("call_sessions", "extraction_claim_id")
    op.drop_column("call_sessions", "extraction_claimed_until")

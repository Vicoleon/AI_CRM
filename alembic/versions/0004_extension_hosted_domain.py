"""Bind browser extension connections to the verified hosted domain.

Revision ID: 0004_extension_hosted_domain
Revises: 0003_encrypt_slack_tokens

Existing rows remain nullable for a short migration. Their old ``atlx_``
keys are rejected and the user reconnects to obtain a domain-bound key.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_extension_hosted_domain"
down_revision: str | Sequence[str] | None = "0003_encrypt_slack_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _lock_timeout() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")


def upgrade() -> None:
    _lock_timeout()
    op.add_column(
        "browser_connections", sa.Column("hosted_domain", sa.String(), nullable=True)
    )


def downgrade() -> None:
    _lock_timeout()
    op.drop_column("browser_connections", "hosted_domain")

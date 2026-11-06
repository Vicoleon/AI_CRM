"""Flag FK-backed platform support users without granting client roles.

Revision ID: 0002_platform_support
Revises: 0001_cubie_baseline
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_platform_support"
down_revision: str | Sequence[str] | None = "0001_cubie_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _lock_timeout() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")


def upgrade() -> None:
    _lock_timeout()
    op.add_column(
        "users",
        sa.Column(
            "is_platform_support",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    _lock_timeout()
    op.drop_column("users", "is_platform_support")

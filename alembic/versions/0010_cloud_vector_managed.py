"""Record managed corpus mutation proof independently of vector syncing.

Revision ID: 0010_cloud_vector_managed
Revises: 0009_cloud_vector_events
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_cloud_vector_managed"
down_revision: str | Sequence[str] | None = "0009_cloud_vector_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "cloud_vector_updates",
        sa.Column(
            "managed_applied", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
    with op.batch_alter_table("cloud_vector_updates") as batch:
        batch.drop_column("managed_applied")

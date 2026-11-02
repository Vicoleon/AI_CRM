"""Order every source event, including repeated same-generation mutations.

Revision ID: 0009_cloud_vector_events
Revises: 0008_cloud_vector_index
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_cloud_vector_events"
down_revision: str | Sequence[str] | None = "0008_cloud_vector_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SEQUENCE = "cloud_vector_updates_sequence_seq"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "cloud_vector_updates", sa.Column("sequence", sa.BigInteger(), nullable=True)
    )
    if bind.dialect.name == "postgresql":
        op.execute(f"CREATE SEQUENCE {_SEQUENCE}")
        op.execute(
            "WITH ordered AS ("
            " SELECT id, ROW_NUMBER() OVER (ORDER BY created_at, id) AS number"
            " FROM cloud_vector_updates"
            ") UPDATE cloud_vector_updates AS event"
            " SET sequence = ordered.number FROM ordered WHERE event.id = ordered.id"
        )
        op.execute(
            f"SELECT setval('{_SEQUENCE}', COALESCE(MAX(sequence), 1), COUNT(*) > 0) "
            "FROM cloud_vector_updates"
        )
        op.execute(
            f"ALTER TABLE cloud_vector_updates ALTER COLUMN sequence "
            f"SET DEFAULT nextval('{_SEQUENCE}')"
        )
        op.execute(f"ALTER SEQUENCE {_SEQUENCE} OWNED BY cloud_vector_updates.sequence")
    else:
        rows = bind.execute(
            sa.text("SELECT id FROM cloud_vector_updates ORDER BY created_at, id")
        ).scalars()
        for number, event_id in enumerate(rows, 1):
            bind.execute(
                sa.text(
                    "UPDATE cloud_vector_updates SET sequence = :number WHERE id = :id"
                ),
                {"number": number, "id": event_id},
            )
    with op.batch_alter_table("cloud_vector_updates") as batch:
        batch.alter_column("sequence", existing_type=sa.BigInteger(), nullable=False)
        batch.drop_constraint(
            "uq_cloud_vector_update_source_generation", type_="unique"
        )
        batch.create_unique_constraint("uq_cloud_vector_updates_sequence", ["sequence"])
    op.create_index(
        "ix_cloud_vector_updates_source_seq",
        "cloud_vector_updates",
        ["corpus", "source_key", "sequence"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_index(
        "ix_cloud_vector_updates_source_seq", table_name="cloud_vector_updates"
    )
    with op.batch_alter_table("cloud_vector_updates") as batch:
        batch.drop_constraint("uq_cloud_vector_updates_sequence", type_="unique")
        batch.create_unique_constraint(
            "uq_cloud_vector_update_source_generation",
            ["corpus", "source_key", "generation", "operation"],
        )
        batch.drop_column("sequence")
    if bind.dialect.name == "postgresql":
        op.execute(f"DROP SEQUENCE IF EXISTS {_SEQUENCE}")

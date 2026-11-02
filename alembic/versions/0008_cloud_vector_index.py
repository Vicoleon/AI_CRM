"""Store versioned client embedding vectors in each workspace database.

Revision ID: 0008_cloud_vector_index
Revises: 0007_gmail_ai_consent
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0008_cloud_vector_index"
down_revision: str | Sequence[str] | None = "0007_gmail_ai_consent"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _lock_timeout() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")


def upgrade() -> None:
    _lock_timeout()
    if op.get_bind().dialect.name == "postgresql":
        # Vectors are generated with each workspace's selected external key.
        # Cloud SQL stores and compares them; the DB never invokes that key.
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "cloud_vector_versions",
        sa.Column("corpus", sa.String(length=40), primary_key=True),
        sa.Column("version", sa.String(length=16), primary_key=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("ann_ready", sa.Boolean(), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64)),
        sa.Column("source_count", sa.Integer(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("verified_at", sa.DateTime()),
    )
    op.create_table(
        "cloud_vector_sources",
        sa.Column("corpus", sa.String(length=40), primary_key=True),
        sa.Column("version", sa.String(length=16), primary_key=True),
        sa.Column("source_key", sa.String(length=64), primary_key=True),
        sa.Column("source_uri", sa.String(length=2048), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_cloud_vector_sources_version",
        "cloud_vector_sources",
        ["corpus", "version"],
    )
    op.create_table(
        "cloud_vector_chunks",
        sa.Column("corpus", sa.String(length=40), primary_key=True),
        sa.Column("version", sa.String(length=16), primary_key=True),
        sa.Column("source_key", sa.String(length=64), primary_key=True),
        sa.Column("chunk_id", sa.String(length=255), primary_key=True),
        sa.Column("source_uri", sa.String(length=2048), nullable=False),
        sa.Column("doc_id", sa.String(length=512), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column(
            "embedding",
            sa.JSON().with_variant(postgresql.ARRAY(postgresql.REAL()), "postgresql"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_cloud_vector_chunks_version",
        "cloud_vector_chunks",
        ["corpus", "version"],
    )
    op.create_index(
        "ix_cloud_vector_chunks_source",
        "cloud_vector_chunks",
        ["corpus", "version", "source_key"],
    )
    op.create_index(
        "ix_cloud_vector_chunks_doc",
        "cloud_vector_chunks",
        ["corpus", "version", "doc_id"],
    )
    op.create_table(
        "cloud_vector_updates",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("corpus", sa.String(length=40), nullable=False),
        sa.Column("source_key", sa.String(length=64), nullable=False),
        sa.Column("source_uri", sa.String(length=2048), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("operation", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("applied_at", sa.DateTime()),
        sa.UniqueConstraint(
            "corpus",
            "source_key",
            "generation",
            "operation",
            name="uq_cloud_vector_update_source_generation",
        ),
    )
    op.create_index(
        "ix_cloud_vector_updates_pending", "cloud_vector_updates", ["corpus", "status"]
    )


def downgrade() -> None:
    _lock_timeout()
    op.drop_index("ix_cloud_vector_updates_pending", table_name="cloud_vector_updates")
    op.drop_table("cloud_vector_updates")
    op.drop_index("ix_cloud_vector_chunks_doc", table_name="cloud_vector_chunks")
    op.drop_index("ix_cloud_vector_chunks_source", table_name="cloud_vector_chunks")
    op.drop_index("ix_cloud_vector_chunks_version", table_name="cloud_vector_chunks")
    op.drop_table("cloud_vector_chunks")
    op.drop_index("ix_cloud_vector_sources_version", table_name="cloud_vector_sources")
    op.drop_table("cloud_vector_sources")
    op.drop_table("cloud_vector_versions")

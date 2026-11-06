"""Retire unverified Slack tokens and deduplicate modal submissions.

Revision ID: 0003_encrypt_slack_tokens
Revises: 0002_platform_support

The old Slack app did not record its app ID or prove that a personal token
belongs to the Cubie email. Every legacy credential is removed; each user must
reconnect through the new identity-checked OAuth callback. The old bot token
column is removed too.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "0003_encrypt_slack_tokens"
down_revision: str | Sequence[str] | None = "0002_platform_support"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _lock_timeout() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")


def upgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Slack token encryption requires an online migration")
    _lock_timeout()
    # destructive-ok: unverified legacy Slack tokens cannot be tied to the
    # correct app and user, so each owner must reconnect through verified OAuth.
    op.execute(sa.text("DELETE FROM slack_credentials"))
    with op.batch_alter_table("slack_credentials") as batch:
        batch.drop_column("bot_token")
        batch.add_column(sa.Column("hosted_domain", sa.String(253), nullable=False))
    op.create_table(
        "slack_view_submissions",
        sa.Column("view_id", sa.String(128), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
    )


def downgrade() -> None:
    raise RuntimeError(
        "Downgrading would restore plaintext Slack tokens; reconnect clients instead"
    )

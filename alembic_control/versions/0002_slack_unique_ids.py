"""Reserve each Slack app and team for exactly one client workspace.

Revision ID: 0002_slack_unique_ids
Revises: 0001_control_baseline
"""

from __future__ import annotations

from alembic import op

revision = "0002_slack_unique_ids"
down_revision = "0001_control_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
    # Fail the migration if an old control DB already has collisions. A silent
    # choice would send signed requests to the wrong workspace.
    op.create_index(
        "ux_slack_installations_app_id",
        "slack_installations",
        ["app_id"],
        unique=True,
    )
    op.create_index(
        "ux_slack_installations_team_id",
        "slack_installations",
        ["team_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ux_slack_installations_team_id", table_name="slack_installations")
    op.drop_index("ux_slack_installations_app_id", table_name="slack_installations")

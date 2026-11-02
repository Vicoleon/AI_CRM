"""Legacy Slack tokens are removed before identity-checked OAuth is enabled."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text


def _migration():
    path = (
        Path(__file__).resolve().parents[2]
        / "alembic/versions/0003_encrypt_slack_tokens.py"
    )
    spec = importlib.util.spec_from_file_location("slack_tokens_0003", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def legacy_table(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE slack_credentials ("
                "user_email VARCHAR PRIMARY KEY, access_token VARCHAR NOT NULL, "
                "slack_user_id VARCHAR, slack_team_id VARCHAR, bot_token VARCHAR)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO slack_credentials "
                "(user_email, access_token, slack_user_id, slack_team_id, bot_token) "
                "VALUES ('owner@example.com', 'xoxp-old-personal', 'U1', 'T1', "
                "'xoxb-old-bot'), "
                "('former@example.com', 'xoxp-former', 'U2', 'T1', 'xoxb-old-bot')"
            )
        )
    yield engine
    engine.dispose()


def test_migration_deletes_old_personal_tokens_and_drops_bot_column(
    legacy_table, monkeypatch
):
    migration = _migration()
    with legacy_table.begin() as connection:
        operations = Operations(
            MigrationContext.configure(connection, opts={"render_as_batch": True})
        )
        monkeypatch.setattr(migration.context, "is_offline_mode", lambda: False)
        monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
        monkeypatch.setattr(migration.op, "execute", operations.execute)
        monkeypatch.setattr(
            migration.op, "batch_alter_table", operations.batch_alter_table
        )
        monkeypatch.setattr(migration.op, "create_table", operations.create_table)
        migration.upgrade()
        columns = {
            row[1]: row
            for row in connection.execute(text("PRAGMA table_info(slack_credentials)"))
        }
        submission_columns = {
            row[1]
            for row in connection.execute(
                text("PRAGMA table_info(slack_view_submissions)")
            )
        }
        remaining = connection.scalar(text("SELECT count(*) FROM slack_credentials"))
    assert "bot_token" not in columns
    assert "hosted_domain" in columns
    assert columns["hosted_domain"][3] == 1
    assert submission_columns == {"view_id", "created_at"}
    assert remaining == 0


def test_same_team_reconnect_cannot_revive_unverified_user(legacy_table, monkeypatch):
    migration = _migration()
    with legacy_table.begin() as connection:
        connection.execute(
            text(
                "UPDATE slack_credentials SET access_token = 'gAAAAinvalidciphertext' "
                "WHERE user_email = 'former@example.com'"
            )
        )
        operations = Operations(
            MigrationContext.configure(connection, opts={"render_as_batch": True})
        )
        monkeypatch.setattr(migration.context, "is_offline_mode", lambda: False)
        monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
        monkeypatch.setattr(migration.op, "execute", operations.execute)
        monkeypatch.setattr(
            migration.op, "batch_alter_table", operations.batch_alter_table
        )
        monkeypatch.setattr(migration.op, "create_table", operations.create_table)
        migration.upgrade()
        # A newly verified OAuth connection for T1 restores only that user.
        connection.execute(
            text(
                "INSERT INTO slack_credentials "
                "(user_email, access_token, slack_user_id, slack_team_id, hosted_domain) "
                "VALUES ('owner@example.com', 'new-verified-token', 'U1', 'T1', "
                "'example.com')"
            )
        )
        rows = connection.execute(
            text("SELECT user_email, access_token FROM slack_credentials")
        ).all()
    assert rows == [("owner@example.com", "new-verified-token")]

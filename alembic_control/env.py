"""Control migrations use only control metadata and CONTROL_DATABASE_URL."""

from alembic import context
from rubik_core.config import settings
from rubik_core.control.db import get_engine
from rubik_core.control.models import Base
from rubik_core.database.migration_guard import assert_migration_target_allowed

config = context.config
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.CONTROL_DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    cmd = getattr(getattr(config, "cmd_opts", None), "cmd", None)
    fn = cmd[0] if isinstance(cmd, (tuple, list)) else cmd
    assert_migration_target_allowed(
        command=getattr(fn, "__name__", None),
        cloudsql_connection_name="",
        database_url=settings.CONTROL_DATABASE_URL,
    )
    injected = config.attributes.get("connection")
    if injected is not None:
        _run_with_connection(injected)
    else:
        with get_engine().connect() as connection:
            _run_with_connection(connection)


def _run_with_connection(connection) -> None:
    """Use the runner's locked connection when one was supplied."""
    if connection.dialect.name == "sqlite":
        # The engine's connect hook enforces this for application sessions;
        # migrations also establish it explicitly before the DDL transaction.
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.commit()
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

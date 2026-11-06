"""Create fresh PostgreSQL owner roles without changing existing memberships."""

from __future__ import annotations

import re

from sqlalchemy import text
from sqlalchemy.engine import Connection


def _owner_privileges(connection: Connection, role: str) -> bool:
    return bool(
        connection.scalar(
            text(
                "SELECT pg_has_role(current_user, :owner, 'SET') "
                "AND pg_has_role(current_user, :owner, 'USAGE')"
            ),
            {"owner": role},
        )
    )


def create_database_owner_role(connection: Connection, role: str) -> None:
    """Create a missing role and verify its creator can assign database ownership.

    Call only after checking that the role is absent. Existing-role retries must
    keep their own ownership checks and never use this membership repair path.
    """
    if not isinstance(role, str) or not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", role):
        raise ValueError("Invalid database owner role identifier")
    connection.exec_driver_sql("SET createrole_self_grant = 'set,inherit'")
    try:
        connection.exec_driver_sql(f'CREATE ROLE "{role}" LOGIN')
    finally:
        connection.exec_driver_sql("RESET createrole_self_grant")

    # Managed PostgreSQL can bypass self-grant. Native PG16 already grants it;
    # repeating ADMIN there would grant back to our own grantor and be refused.
    if not _owner_privileges(connection, role):
        connection.exec_driver_sql(
            f'GRANT "{role}" TO CURRENT_USER WITH ADMIN TRUE, INHERIT TRUE, SET TRUE'
        )
        if not _owner_privileges(connection, role):
            raise PermissionError("New database owner role requires SET and INHERIT")

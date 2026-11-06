"""Snapshot Cloud SQL into a local SQLite DB for offline / safe testing.

Use this when you want realistic data locally without touching the shared
Cloud SQL instance (e.g. when a feature has migrations that haven't been
applied to prod-dev yet, or you just want to click around without risk).

Reads source from the .env (CLOUDSQL_CONNECTION_NAME + DB_USER/DB_PASS/DB_NAME).
Writes to the SQLite file path you pass with --target.

The target SQLite DB must already have the latest schema. Easiest way:

    APP_MODE=local CLOUDSQL_CONNECTION_NAME='' \\
        DATABASE_URL="sqlite:///./test_local.db" \\
        .venv/bin/alembic upgrade head

Then run this script:

    .venv/bin/python scripts/sync_cloudsql_to_sqlite.py --target ./test_local.db

Behavior:
- Copies every table that exists on BOTH the Cloud SQL source and the
  local SQLite target.
- Within each table, copies only the columns that exist on both sides —
  newer columns that only exist locally (e.g. secondary_link_*) are
  left NULL.
- Tables that only exist locally (e.g. eos_rock_updates) are left
  empty.
- Skips alembic_version so the local DB keeps its head pointer.
- Truncates each target table before refilling — running twice is fine,
  output is a true snapshot of source at run time.
- SQLite FK checks are disabled for the duration of the load so insertion
  order doesn't matter; they're re-enabled at the end.

Limitations:
- One-shot snapshot, no incremental sync. Re-run to refresh.
- Doesn't copy DB-side sequences / autoincrement state — relevant for
  integer-PK tables; UUID-PK EOS tables don't care.
- Doesn't migrate Cloud SQL schema in either direction — the script
  doesn't add columns or tables to Cloud SQL; it just copies what's
  there. This is the whole point: zero writes to Cloud SQL.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from uuid import UUID

# Make the repo root importable regardless of where the script is invoked
# from (the venv lives one level up at the parent worktree, so the usual
# `python -m scripts.sync_…` ergonomics aren't an option).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _bootstrap_settings_for_cloud_sql() -> None:
    """Strip any APP_MODE / DATABASE_URL / CLOUDSQL_CONNECTION_NAME the
    caller may have set in their shell so we read the *real* values from
    .env. The whole point of this script is reading Cloud SQL — overrides
    intended for local backend runs would silently redirect us to the
    local SQLite, which would then "copy itself onto itself" and do
    nothing useful."""
    for var in ("APP_MODE", "DATABASE_URL", "CLOUDSQL_CONNECTION_NAME"):
        os.environ.pop(var, None)


def _coerce_for_sqlite(v):
    """Postgres types → SQLite-friendly values.

    SQLite's adapter handles ints/floats/strs/bytes/None natively. Everything
    else needs to be converted because SQLite has no native UUID / datetime /
    JSON storage.

    UUIDs are written as ``.hex`` — 32 hex digits, no dashes — because that's
    the storage format SQLAlchemy 2.x ``Uuid`` type uses on SQLite. Writing
    the dashed form makes equality lookups in ``WHERE id = :rock_id`` silently
    miss (the SELECT side accepts either, so the bug only surfaces on PK
    lookups, not list queries — exactly the failure mode you'll hit
    clicking a rock detail page).
    """
    if v is None:
        return None
    if isinstance(v, bool):
        # bool is a subclass of int in Python — keep it before the int check
        return v
    if isinstance(v, (int, float, str, bytes)):
        return v
    if isinstance(v, UUID):
        return v.hex
    if isinstance(v, (datetime, date, time)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str)
    # Postgres enums come back as str via SQLAlchemy in most cases; if a
    # native Python enum slipped through, take its value.
    if hasattr(v, "value"):
        return v.value
    return str(v)


def main() -> int:
    # Select the Cloud SQL source configuration before workspace validation
    # imports the module-level Settings singleton.
    _bootstrap_settings_for_cloud_sql()

    from rubik_core.tenancy.cli import require_cli_workspace

    require_cli_workspace()

    parser = argparse.ArgumentParser(
        description="Snapshot Cloud SQL into a local SQLite DB.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--target",
        required=True,
        help="Path to the local SQLite DB to write into (must exist + have schema).",
    )
    parser.add_argument(
        "--skip",
        nargs="*",
        default=[],
        help="Table names to skip (besides alembic_version and chat_uploads, which are always skipped).",
    )
    parser.add_argument(
        "--only",
        nargs="*",
        default=None,
        help="If set, copy only these tables.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be copied without writing to the SQLite target.",
    )
    args = parser.parse_args()

    if not os.path.exists(args.target):
        print(
            f"error: target SQLite DB {args.target} doesn't exist. "
            "Run `alembic upgrade head` against it first to create the schema.",
            file=sys.stderr,
        )
        return 2

    # Imports must come AFTER bootstrap so pydantic-settings reads the
    # cleaned env. Otherwise the module-level `settings` would lock in
    # whatever overrides the caller had set.
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text

    from rubik_core.config import settings
    from rubik_core.database.db import get_engine

    if not settings.CLOUDSQL_CONNECTION_NAME and "sqlite" in settings.DATABASE_URL:
        print(
            f"refusing to run: settings resolved to {settings.DATABASE_URL}. "
            "Make sure .env has CLOUDSQL_CONNECTION_NAME set — this script "
            "is meant to copy *from* Cloud SQL.",
            file=sys.stderr,
        )
        return 2

    print(
        f"source: Cloud SQL {settings.CLOUDSQL_CONNECTION_NAME or settings.DATABASE_URL}"
    )
    print(f"target: sqlite:{args.target}")
    if args.dry_run:
        print("(dry run — no writes)")

    cloud_engine = get_engine()
    cloud_inspector = sa_inspect(cloud_engine)
    cloud_tables = set(cloud_inspector.get_table_names())

    sqlite_conn = sqlite3.connect(args.target)
    sqlite_conn.execute("PRAGMA foreign_keys = OFF")

    sqlite_tables = {
        r[0]
        for r in sqlite_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }

    # chat_uploads holds the text of files people attached in Ask Cubie, private
    # to each of them: it never leaves Cloud SQL for a laptop.
    common = (
        (cloud_tables & sqlite_tables)
        - {"alembic_version", "chat_uploads"}
        - set(args.skip)
    )
    if args.only:
        common &= set(args.only)

    if not common:
        print("no tables to copy (after filters). nothing to do.")
        return 0

    print(f"\n{len(common)} tables in common (intersection of source + target)")

    only_cloud = cloud_tables - sqlite_tables
    only_local = sqlite_tables - cloud_tables - {"alembic_version"}
    if only_cloud:
        print(f"  source-only (not copied): {sorted(only_cloud)}")
    if only_local:
        print(f"  target-only (left empty):  {sorted(only_local)}")

    total_rows = 0
    with cloud_engine.connect() as src:
        for tbl in sorted(common):
            cloud_cols = [c["name"] for c in cloud_inspector.get_columns(tbl)]
            sqlite_cols = [
                r[1]
                for r in sqlite_conn.execute(f"PRAGMA table_info('{tbl}')").fetchall()
            ]
            shared = [c for c in cloud_cols if c in sqlite_cols]
            dropped = [c for c in cloud_cols if c not in sqlite_cols]
            if dropped:
                print(
                    f"  {tbl}: dropping {len(dropped)} column(s) not in target: {dropped}"
                )

            cols_quoted = ", ".join(f'"{c}"' for c in shared)
            rows = (
                src.execute(text(f"SELECT {cols_quoted} FROM {tbl}")).mappings().all()
            )

            if args.dry_run:
                print(f"  {tbl}: would copy {len(rows)} rows ({len(shared)} cols)")
                total_rows += len(rows)
                continue

            sqlite_conn.execute(f"DELETE FROM {tbl}")
            if rows:
                placeholders = ", ".join(["?"] * len(shared))
                sqlite_conn.executemany(
                    f"INSERT INTO {tbl} ({cols_quoted}) VALUES ({placeholders})",
                    [[_coerce_for_sqlite(row[c]) for c in shared] for row in rows],
                )
            print(f"  {tbl}: {len(rows)} rows")
            total_rows += len(rows)

    if not args.dry_run:
        sqlite_conn.commit()
        sqlite_conn.execute("PRAGMA foreign_keys = ON")
    sqlite_conn.close()
    cloud_engine.dispose()

    verb = "would copy" if args.dry_run else "copied"
    print(f"\ndone. {verb} {total_rows} row(s) across {len(common)} table(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

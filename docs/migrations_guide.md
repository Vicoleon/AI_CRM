# Database Migrations Guide

This document explains how to manage database schema changes in the Cubie repository using Alembic.

## Overview

We use Alembic to handle database migrations. To ensure production reliability, especially on scaling platforms like Google Cloud Run, we decouple migration execution from application startup.

## One baseline, then numbered migrations

The chain starts at a single baseline, `alembic/versions/0001_cubie_baseline.py`,
generated from the models. It replaced the ninety-odd incremental migrations of
the code's earlier life (spec 4.2.6). Every workspace database is built from it.

- **A new migration** chains off the current head and takes the next number:
  `alembic revision --autogenerate -m "add x" --rev-id 0002_add_x`. Alembic
  names the file `<rev id>_<message slug>.py`, so that writes
  `alembic/versions/0002_add_x_add_x.py`; rename it to `0002_add_x.py` to match
  the baseline's style (the `revision` inside the file is what counts). One
  head only; CI fails on two.
- **A rev id is at most 32 characters.** It is stored in
  `alembic_version.version_num`, a `VARCHAR(32)`; a longer one fails the
  upgrade on Postgres when it writes the version row.
- **The baseline builds the schema and no rows.** A workspace's defaults (the
  default AI Priorities playbook, the Drive label weights, the SLA placeholders
  and the Smart Teams skills taxonomy) come from
  `rubik_core.database.seed.seed_workspace`, which is idempotent:
  `PYTHONPATH=. python -m rubik_core.database.seed` after `alembic upgrade head`.
  A new default row goes there, not into a migration.
- **The migration has to build what the models declare.**
  `backend/tests/test_migrations_baseline.py` runs `alembic upgrade head` on
  SQLite and compares every table, column, key, index and CHECK constraint with
  `Base.metadata.create_all()`. On Postgres, `alembic check` against a migrated
  database must report no new operations.
- **Postgres fidelity.** `scripts/dev/schema_diff.sh <rev-a> <rev-b>` builds two
  throwaway Postgres databases from two versions of the chain and diffs their
  normalised `pg_dump --schema-only` output. It is how the baseline was proven
  against the old chain, and how to check that a refactor of migrations changes
  nothing.

## Local Development

In local development (`APP_MODE=local`), migrations are automatically applied by `scripts/start.sh` when the container or application starts.

To generate a new migration after changing models:
```bash
# Ensure you are in the root directory and your env is active
PYTHONPATH=. alembic revision --autogenerate -m "description of changes" --rev-id 000N_short_name
# → alembic/versions/000N_short_name_description_of_changes.py
```

To apply migrations to an explicitly selected local workspace:
```bash
PYTHONPATH=. python -c 'from rubik_core.platform.migrate import migrate_all; print(migrate_all())'
```

`scripts/dev/up.sh --setup-only` is the normal local path. It migrates one
control SQLite database and two distinct workspace SQLite databases, then
seeds the defaults once per workspace. The control migration tree is
`alembic_control.ini`; the workspace tree is `alembic.ini`. Both require one
known head. A production runtime must not infer a tenant from `DATABASE_URL`.

## Production (Google Cloud Run)

In production (`APP_MODE=gcp`), automatic migrations are **disabled** by default to prevent race conditions when multiple instances start simultaneously.

`scripts/platform/deploy.sh --env <private-env>` runs a guarded migration Job
before it updates the service or scheduled Jobs. That Job migrates control
first, then each registered workspace under its own advisory lock, and stops
at the first failure. `deploy.sh --dry-run` is read-only and lists the ordered
actions. Provisioning a new workspace migrates its pending DB before its
control row becomes routable. Do not run a raw `alembic upgrade head` against
an unspecified production database; use the named platform CLI or guarded
deploy flow. An unknown live revision blocks the release.

## Configuration

The migration behavior is controlled by:
- `APP_MODE`: selects local or GCP runtime behavior. GCP startup never migrates.
- `CONTROL_DATABASE_URL`: the one routing/control database.
- `WORKSPACE_DATABASE_DIR`: local SQLite workspace file directory.
- `CUBIE_WORKSPACE`: explicit workspace for a named Job or maintenance action.

## Gotcha: CI runs migrations against SQLite, so `ALTER ... ADD CONSTRAINT` fails

`python-app.yml` runs `alembic upgrade head` against **SQLite**, and SQLite has
no `ALTER TABLE ADD CONSTRAINT`. So `op.create_unique_constraint(...)` (and any
other constraint `ALTER`) raises at migration time:

```
NotImplementedError: No support for ALTER of constraints in SQLite dialect.
```

Declare the constraint **inline in `op.create_table`** instead — it works on
both SQLite and Postgres:

```python
op.create_table(
    "my_table",
    sa.Column("a", sa.String(), nullable=False),
    sa.Column("b", sa.String(), nullable=False),
    sa.UniqueConstraint("a", "b", name="uq_my_table_a_b"),  # inline, not ALTER
)
```

`op.create_index` is fine on SQLite; it is constraints specifically that bite.

**Why most tests do not catch this.** The suite builds its schema with
`Base.metadata.create_all()` — straight from the MODELS, where the constraint
lives inline in `__table_args__`. The migration file is a *separate* code path.
`test_migrations_baseline.py` runs it, so the full suite fails on it; to check
just the migration before pushing, **run it**:

```bash
scripts/dev/up.sh --setup-only
.venv/bin/python -m pytest backend/tests/test_migrations_baseline.py backend/tests/test_two_workspace_isolation.py -q
```

A full green local suite has shipped a migration that could not apply, so
run it.

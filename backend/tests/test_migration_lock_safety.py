"""Migrations that change an EXISTING table must not queue behind live traffic.

Every migration runs at container boot (AUTO_MIGRATE) against the database the
live service is using. An ``ALTER TABLE user_preferences ADD COLUMN`` waits
for a lock behind the live service's open transactions, and every request
that reads the table then queues behind the waiting ALTER: the app answers
504, then 429 to everything. ``SET LOCAL lock_timeout`` turns that into a
failed boot that leaves the live revision alone.

So every migration after the baseline that alters, indexes or constrains an
existing table must set a lock timeout. Creating a brand-new table, or changing
data only, is exempt. The baseline itself only creates tables, on an empty
database, so it is exempt too.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"
BASELINE = "0001_cubie_baseline.py"
TOUCHES_EXISTING = re.compile(
    r"op\.(add_column|alter_column|drop_column|create_index|drop_index|"
    r"create_foreign_key|create_unique_constraint|create_check_constraint|"
    r"drop_constraint|rename_table|batch_alter_table)\("
    r"|ALTER\s+TABLE",
    re.IGNORECASE,
)


def _offenders(paths: Iterable[Path]) -> list[str]:
    offenders = []
    for path in paths:
        if path.name == BASELINE:
            continue
        source = path.read_text()
        upgrade = source.split("def upgrade", 1)[-1].split("def downgrade", 1)[0]
        if TOUCHES_EXISTING.search(upgrade) and "lock_timeout" not in source:
            offenders.append(path.name)
    return offenders


def test_migrations_that_lock_an_existing_table_set_a_lock_timeout():
    assert (VERSIONS / BASELINE).is_file()
    offenders = _offenders(sorted(VERSIONS.glob("*.py")))
    assert not offenders, (
        "These migrations change an existing table at boot without "
        f"`SET LOCAL lock_timeout`: {offenders}. See this test's docstring."
    )


def test_the_rule_flags_an_alter_without_a_timeout_and_nothing_else(tmp_path):
    """Until the first post-baseline migration lands, prove the rule bites."""
    migrations = {
        BASELINE: 'def upgrade():\n    op.create_index("ix", "t", ["c"])\n',
        "0002_alters.py": 'def upgrade():\n    op.add_column("users", col)\n',
        "0003_alters_safely.py": (
            "def upgrade():\n"
            "    op.execute(\"SET LOCAL lock_timeout = '5s'\")\n"
            '    op.add_column("users", col)\n'
        ),
        "0004_new_table.py": 'def upgrade():\n    op.create_table("fresh")\n',
    }
    for name, source in migrations.items():
        (tmp_path / name).write_text(source)

    assert _offenders(sorted(tmp_path.glob("*.py"))) == ["0002_alters.py"]

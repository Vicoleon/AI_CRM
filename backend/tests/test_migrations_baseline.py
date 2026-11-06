"""One Alembic baseline, and it builds exactly the schema the models declare.

Phase 1 collapsed the incremental migrations into
``alembic/versions/0001_cubie_baseline.py`` (spec §4.2.6). Every other test
builds its database with ``Base.metadata.create_all()``, so this module is the
one place that holds the migration to the models:

* the chain has one root, the baseline, and one head;
* ``alembic upgrade head`` on SQLite gives the same tables as
  ``Base.metadata.sorted_tables``;
* and the same columns, keys, indexes (partial and expression ones included)
  and CHECK constraints as ``create_all`` does, so a test written against the
  models is a test of the schema a real database gets.

Postgres fidelity (enums, partial indexes, CHECK constraints) is proven
against the pre-squash chain by ``scripts/dev/schema_diff.sh``, which needs a
Postgres server and is therefore not part of this suite.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE = "0001_cubie_baseline"

#: Tables of modules Phase 1 deleted (``test_removed_modules.REMOVED_TABLES``
#: pins them out of the models) plus ``fathom_call_sync_logs``, which no code
#: read. The baseline must not bring any of them back.
RETIRED_TABLES = frozenset(
    {
        "compliance_intakes",
        "compliance_reviews",
        "conference_attendee_notes",
        "conference_attendees",
        "conference_crm_suggestions",
        "conference_feedback_asks",
        "conference_imports",
        "conferences",
        "daily_giggle",
        "fathom_call_sync_logs",
        "kb_capture_candidates",
        "regulatory_digests",
        "regulatory_items",
        "regulatory_sources",
        "ticket_credentials",
        "ticket_resolution_state",
        "whats_new_clicks",
        "whats_new_items",
    }
)


def _script_directory():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))


@pytest.fixture(scope="module")
def migrated(tmp_path_factory) -> sa.Engine:
    """A SQLite database built by the real CLI path: ``alembic upgrade head``."""
    path = tmp_path_factory.mktemp("baseline") / "test.db"
    env = {
        **os.environ,
        "APP_MODE": "local",
        "DATABASE_URL": f"sqlite:///{path}",
        "WORKSPACE_DATABASE_DIR": str(path.parent),
        "CUBIE_WORKSPACE": "test",
        "CLOUDSQL_CONNECTION_NAME": "",
        "DB_USER": "",
        "GCP_PROJECT_ID": "",
        "PYTHONPATH": str(REPO_ROOT),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "-x", "workspace=test", "upgrade", "head"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr
    return sa.create_engine(f"sqlite:///{path}")


@pytest.fixture(scope="module")
def from_models(tmp_path_factory) -> sa.Engine:
    from rubik_core.database.models import Base

    path = tmp_path_factory.mktemp("models") / "models.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    return engine


def _norm(sql: object) -> str | None:
    """Whitespace- and quote-insensitive SQL, so only real differences count."""
    if sql is None:
        return None
    return re.sub(r"\s+", " ", str(sql).replace('"', "")).strip()


def _schema(engine: sa.Engine) -> dict[str, dict[str, object]]:
    """Everything SQLite keeps about each table, in a comparable shape."""
    inspector = sa.inspect(engine)
    with engine.connect() as conn:
        index_sql = {
            name: (table, _norm(sql))
            for name, table, sql in conn.execute(
                sa.text(
                    "SELECT name, tbl_name, sql FROM sqlite_master "
                    "WHERE type = 'index' AND sql IS NOT NULL"
                )
            )
        }
    schema: dict[str, dict[str, object]] = {}
    for table in inspector.get_table_names():
        if table == "alembic_version":
            continue
        schema[table] = {
            "columns": sorted(
                (c["name"], str(c["type"]), c["nullable"], _norm(c["default"]))
                for c in inspector.get_columns(table)
            ),
            "primary_key": inspector.get_pk_constraint(table)["constrained_columns"],
            "foreign_keys": sorted(
                (
                    tuple(fk["constrained_columns"]),
                    fk["referred_table"],
                    tuple(fk["referred_columns"]),
                    (fk.get("options") or {}).get("ondelete"),
                )
                for fk in inspector.get_foreign_keys(table)
            ),
            "unique": sorted(
                (u["name"] or "", tuple(u["column_names"]))
                for u in inspector.get_unique_constraints(table)
            ),
            "checks": sorted(
                (c["name"] or "", _norm(c["sqltext"]))
                for c in inspector.get_check_constraints(table)
            ),
            "indexes": sorted(
                (name, sql)
                for name, (owner, sql) in index_sql.items()
                if owner == table
            ),
        }
    return schema


def _check_sql(engine: sa.Engine, table: str, name: str) -> str:
    checks = {
        c["name"]: c["sqltext"] for c in sa.inspect(engine).get_check_constraints(table)
    }
    assert name in checks, f"{table} has no CHECK named {name}"
    return checks[name]


# -- the chain ---------------------------------------------------------------


def test_the_chain_has_one_root_the_baseline_and_one_head():
    script = _script_directory()
    assert list(script.get_bases()) == [BASELINE]
    assert script.get_revision(BASELINE).down_revision is None
    heads = script.get_heads()
    assert len(heads) == 1, f"expected one Alembic head, got {heads}"


def test_every_revision_descends_from_the_baseline():
    script = _script_directory()
    revisions = {rev.revision for rev in script.walk_revisions()}
    lineage = {
        rev.revision
        for rev in script.iterate_revisions(script.get_current_head(), "base")
    }
    assert BASELINE in revisions
    assert revisions == lineage


# -- what upgrade head builds ------------------------------------------------


def test_upgrade_head_builds_the_same_tables_the_models_declare(migrated):
    from rubik_core.database.models import Base

    built = set(sa.inspect(migrated).get_table_names()) - {"alembic_version"}
    declared = {table.name for table in Base.metadata.sorted_tables}
    assert built - declared == set(), "tables no model declares"
    assert declared - built == set(), "tables the baseline does not create"


def test_upgrade_head_matches_create_all_column_for_column(migrated, from_models):
    built, declared = _schema(migrated), _schema(from_models)
    differences = {
        f"{table}.{aspect}": {
            "alembic": built[table][aspect],
            "models": declared[table][aspect],
        }
        for table in sorted(set(built) & set(declared))
        for aspect in built[table]
        if built[table][aspect] != declared[table][aspect]
    }
    assert differences == {}


def test_upgrade_head_stamps_the_database_at_the_head(migrated):
    with migrated.connect() as conn:
        stamped = (
            conn.execute(sa.text("SELECT version_num FROM alembic_version"))
            .scalars()
            .all()
        )
    assert stamped == [_script_directory().get_current_head()]


def test_no_retired_table_comes_back(migrated):
    assert RETIRED_TABLES & set(sa.inspect(migrated).get_table_names()) == set()


def test_the_loose_end_close_reasons_include_the_assistant_log(migrated):
    from rubik_core.identifiers import LOOP_CLOSE_LOGGED_FROM_ASSISTANT

    sql = _check_sql(migrated, "loop_items", "ck_loop_items_close_reason")
    assert f"'{LOOP_CLOSE_LOGGED_FROM_ASSISTANT}'" in sql


def test_the_help_desk_ticket_vocabulary_is_gone():
    from rubik_core.database.commitments import COMMITMENT_OPENED_SOURCES
    from rubik_core.database.promises import OBJECT_KINDS

    assert "ticket" not in COMMITMENT_OPENED_SOURCES
    assert "ticket" not in OBJECT_KINDS


@pytest.mark.parametrize(
    ("table", "check"),
    [
        ("commitments", "ck_commitments_opened_source"),
        ("promises", "ck_promises_object_kind"),
    ],
)
def test_the_check_constraints_no_longer_accept_a_ticket(migrated, table, check):
    assert "'ticket'" not in _check_sql(migrated, table, check)


def test_the_frontend_object_kinds_match_the_backend():
    from rubik_core.database.promises import OBJECT_KINDS

    source = (REPO_ROOT / "frontend" / "src" / "api" / "todos.ts").read_text()
    block = re.search(r"export type ObjectKind =([^;]+);", source)
    assert block, "frontend/src/api/todos.ts no longer exports ObjectKind"
    assert set(re.findall(r"'([a-z_]+)'", block.group(1))) == set(OBJECT_KINDS)


def _defer(conn, *, deal="201", ended=None, reason=None, owner="parker@acme.example"):
    conn.execute(
        sa.text(
            "INSERT INTO deal_defers (id, hubspot_deal_id, owner_email, deferred_by, "
            "follow_up_on, note, created_at, ended_at, ended_reason, "
            "hubspot_task_status, updated_at) VALUES (:id, :deal, :owner, :owner, "
            "'2026-11-14', 'n', '2026-10-15', :ended, :reason, 'pending', "
            "'2026-10-15')"
        ),
        {
            "id": f"{deal}-{ended}-{reason}-{owner}",
            "deal": deal,
            "ended": ended,
            "reason": reason,
            "owner": owner,
        },
    )


def test_the_built_database_enforces_a_partial_key_a_vocabulary_and_a_fold(migrated):
    """DDL text is only half the promise: the database has to refuse the rows.

    ``uq_deal_defers_open_deal`` is a partial unique index (one OPEN defer per
    deal, any number of ended ones); ``ended_reason`` is a CHECK vocabulary and
    ``owner_email`` a CHECK that the address is folded.
    """
    with migrated.connect() as conn:  # never committed: rolled back on close
        _defer(conn, ended="2026-10-10", reason="undeferred")
        _defer(conn, ended="2026-10-11", reason="replaced")
        _defer(conn)  # the one open defer
        _defer(conn, deal="202")  # another deal: fine
        with pytest.raises(sa.exc.IntegrityError):
            _defer(conn, owner="reese@acme.example")
        with pytest.raises(sa.exc.IntegrityError):
            _defer(conn, deal="203", ended="2026-10-12", reason="forgot")
        with pytest.raises(sa.exc.IntegrityError):
            _defer(conn, deal="204", owner="Parker@Acme.example")


def test_a_rock_or_to_do_team_tag_is_free_text_not_a_department_key(migrated):
    """``eos_rocks`` / ``eos_todos.department_name`` tag a team; no key binds them.

    Their writers store a team name as given: promote-to-EOS and the Slack
    command fall back to the caller's own ``users.department`` ("general" by
    default, never checked against ``departments``), and rock create stores
    the team the client sends. The pre-squash Postgres schema had no foreign
    key here, so such a tag was accepted and an admin removing a department
    left the tags alone. Built with foreign keys enforced (SQLite skips them
    otherwise), the baseline must still accept the tag and keep it.
    """
    from sqlalchemy.orm import Session

    from rubik_core.database.eos import Organization, Person, Quarter, Rock, Todo
    from rubik_core.database.models import Department

    enforcing = sa.create_engine(migrated.url)

    @sa.event.listens_for(enforcing, "connect")
    def _enforce_foreign_keys(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    with Session(enforcing) as db:  # never committed: rolled back on close
        org = Organization(name="Acme")
        owner = Person(first_name="Parker", last_name="Lane", email="p@acme.example")
        db.add_all([Department(name="Sales"), org, owner])
        db.flush()
        quarter = Quarter(
            organization_id=org.id,
            name="Q4 2026",
            start_date=datetime(2026, 10, 1),
            end_date=datetime(2026, 12, 31),
        )
        db.add(quarter)
        db.flush()
        tags = ("general", "sales", "Sales")  # no row / a case variant / a row
        rocks = [
            Rock(
                quarter_id=quarter.id,
                owner_id=owner.id,
                description="r",
                department_name=tag,
            )
            for tag in tags
        ]
        todos = [
            Todo(owner_id=owner.id, description="t", department_name=tag)
            for tag in tags
        ]
        rows = [*rocks, *todos]
        db.add_all(rows)
        db.flush()  # a key to departments.name would refuse the first two

        db.delete(db.get(Department, "Sales"))
        db.flush()
        db.expire_all()
        assert [row.department_name for row in rows] == [*tags, *tags]
    enforcing.dispose()

    for table in (Rock.__table__, Todo.__table__):
        assert not table.c.department_name.foreign_keys, table.name
        assert not [
            fk
            for fk in sa.inspect(migrated).get_foreign_keys(table.name)
            if fk["constrained_columns"] == ["department_name"]
        ], table.name


# -- the Postgres proof ------------------------------------------------------


def test_the_postgres_schema_diff_script_parses_and_explains_itself():
    """It needs a Postgres server to run, so only its shell and usage are pinned."""
    script = REPO_ROOT / "scripts" / "dev" / "schema_diff.sh"
    assert subprocess.run(["bash", "-n", str(script)], check=False).returncode == 0
    proc = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, timeout=30, check=False
    )
    assert proc.returncode == 2
    assert "schema_diff.sh [--drop-removed] <rev-a> <rev-b>" in proc.stderr

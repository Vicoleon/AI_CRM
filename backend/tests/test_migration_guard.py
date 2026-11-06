"""Regression tests for the Alembic guards (a dev-database near-outage).

The failure: a worktree applies a revision that exists only on an unmerged
branch to the shared dev Cloud SQL database. Every container built from
`main` then fails `alembic upgrade head` at boot with "Can't locate revision
identified by '<rev>'", retries 5x, and exit(1)s. Nothing listens on :8080,
so the revision fails its startup probe AND traffic cannot be re-routed
(routing forces a cold start). Merged work never ships.

Covered here:
  1. mutating Alembic commands refuse to touch a deployed database from a laptop
  2. drift no longer costs us the service — the app boots without migrating
  3. the /health markers describe the database AFTER migrations run, not before
     (otherwise a container that successfully applied a migration reports the
     pre-migration revision for its whole life, which reads as a failed migration)
"""

from __future__ import annotations

import pytest

from rubik_core.database.migration_guard import (
    DRIFT_POLICY_ENV,
    EXIT_DRIFT_BLOCKED,
    EXIT_RUN_MIGRATIONS,
    EXIT_SKIP_MIGRATIONS,
    REMOTE_OVERRIDE_ENV,
    DriftReport,
    RemoteMigrationBlocked,
    assert_migration_target_allowed,
    boot_exit_code,
    decide_boot_action,
    describe_remote_target,
    evaluate_drift,
    format_drift_diagnostic,
    resolve_drift_policy,
    running_in_managed_runtime,
    unmerged_revision_files,
)

DEV_INSTANCE = "cubie-dev:us-central1:cubie-db"


# ---------------------------------------------------------------------------
# Guard 1: don't migrate a deployed database from an ad-hoc local run
# ---------------------------------------------------------------------------


def test_local_alembic_upgrade_against_dev_cloud_sql_is_blocked():
    """The exact command that started the incident, run from a worktree."""
    with pytest.raises(RemoteMigrationBlocked) as excinfo:
        assert_migration_target_allowed(
            command="upgrade",
            cloudsql_connection_name=DEV_INSTANCE,
            database_url="sqlite:///./dam_data/dam.db",
            env={},  # no Cloud Run markers, no override
        )
    message = str(excinfo.value)
    assert DEV_INSTANCE in message
    # The block must be actionable, not just a refusal.
    assert REMOTE_OVERRIDE_ENV in message
    assert "CLOUDSQL_CONNECTION_NAME=" in message


def test_cloud_run_service_cannot_migrate_itself():
    """Only the deploy migration Job may mutate a remote schema."""
    with pytest.raises(RemoteMigrationBlocked):
        assert_migration_target_allowed(
            command="upgrade",
            cloudsql_connection_name=DEV_INSTANCE,
            database_url="sqlite:///./dam_data/dam.db",
            env={"K_SERVICE": "cubie", "K_REVISION": "cubie-00462-nq4"},
        )


def test_cloud_run_job_may_migrate_itself():
    assert_migration_target_allowed(
        command="upgrade",
        cloudsql_connection_name=DEV_INSTANCE,
        database_url=None,
        env={"CLOUD_RUN_JOB": "cubie-worker"},
    )


def test_explicit_override_allows_a_deliberate_remote_migration():
    assert_migration_target_allowed(
        command="stamp",
        cloudsql_connection_name=DEV_INSTANCE,
        database_url=None,
        env={REMOTE_OVERRIDE_ENV: "1"},
    )


def test_local_sqlite_is_always_allowed():
    """The documented safe local recipe: blank the connection name."""
    assert_migration_target_allowed(
        command="upgrade",
        cloudsql_connection_name="",
        database_url="sqlite:///./local_dev.db",
        env={},
    )


def test_localhost_postgres_is_allowed():
    assert_migration_target_allowed(
        command="upgrade",
        cloudsql_connection_name=None,
        database_url="postgresql+pg8000://u:p@localhost:5432/brain",
        env={},
    )


def test_remote_postgres_url_without_connection_name_is_blocked():
    """Drift can also arrive via a raw remote DATABASE_URL, not just the connector."""
    with pytest.raises(RemoteMigrationBlocked):
        assert_migration_target_allowed(
            command="upgrade",
            cloudsql_connection_name=None,
            database_url="postgresql+pg8000://u:p@34.72.10.4:5432/brain",
            env={},
        )


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg2://u:private@/control?host=/cloudsql/project:region:instance",
        "postgresql+pg8000://u:private@/control?unix_sock=/cloudsql/project:region:instance/.s.PGSQL.5432",
        "postgresql+psycopg2://u:private@localhost/control?host=/cloudsql/project:region:instance",
        "postgresql+psycopg2://u:private@/control?hostaddr=10.0.0.5",
        "postgresql+psycopg2://u:private@/control?service=production",
        "postgresql+psycopg2://u:private@/control",
    ],
)
def test_socket_and_query_routing_targets_are_remote_without_leaking_secrets(url):
    target = describe_remote_target(None, url)
    assert target is not None
    assert "private" not in target
    assert "/cloudsql" not in target
    with pytest.raises(RemoteMigrationBlocked) as excinfo:
        assert_migration_target_allowed(
            command="upgrade",
            cloudsql_connection_name=None,
            database_url=url,
            env={},
        )
    assert "private" not in str(excinfo.value)
    assert "/cloudsql" not in str(excinfo.value)


@pytest.mark.parametrize(
    "command", ["current", "history", "heads", "check", "revision"]
)
def test_read_only_commands_are_never_blocked(command):
    """Inspecting dev is how you diagnose drift — blocking it would backfire."""
    assert_migration_target_allowed(
        command=command,
        cloudsql_connection_name=DEV_INSTANCE,
        database_url=None,
        env={},
    )


@pytest.mark.parametrize("command", ["upgrade", "downgrade", "stamp"])
def test_all_mutating_commands_are_blocked(command):
    with pytest.raises(RemoteMigrationBlocked):
        assert_migration_target_allowed(
            command=command,
            cloudsql_connection_name=DEV_INSTANCE,
            database_url=None,
            env={},
        )


def test_undetermined_command_is_treated_as_mutating():
    """Fail closed: an unrecognised invocation path must not slip past the guard."""
    with pytest.raises(RemoteMigrationBlocked):
        assert_migration_target_allowed(
            command=None,
            cloudsql_connection_name=DEV_INSTANCE,
            database_url=None,
            env={},
        )


def test_connection_name_wins_over_a_local_looking_url():
    """db.py routes through the Cloud SQL Connector whenever the name is set."""
    assert describe_remote_target(DEV_INSTANCE, "sqlite:///./local.db") is not None


def test_malformed_cloud_sql_name_is_not_logged_verbatim():
    label = describe_remote_target("project:region:instance?password=private", None)
    assert label is not None
    assert "private" not in label


def test_unparseable_database_url_is_not_treated_as_remote():
    assert describe_remote_target(None, "::::not a url::::") is None


def test_managed_runtime_detection_ignores_empty_markers():
    assert running_in_managed_runtime({"K_SERVICE": ""}) is False
    assert running_in_managed_runtime({"K_SERVICE": "cubie"}) is False
    assert running_in_managed_runtime({"CLOUD_RUN_JOB": "cubie-migrate"}) is True


# ---------------------------------------------------------------------------
# Guard 2: drift detection
# ---------------------------------------------------------------------------


def test_the_incident_shape_is_reported_as_drift():
    """DB at zc040 (branch-only), image built from main at zc039."""
    report = evaluate_drift(
        db_revisions=["zc00000000040"],
        known_revisions=["zc00000000038", "zc00000000039"],
        image_heads=["zc00000000039"],
    )
    assert report.status == "drift"
    assert report.has_drift
    assert report.unknown_revisions == ("zc00000000040",)
    assert report.db_revisions == ("zc00000000040",)
    assert report.image_heads == ("zc00000000039",)


def test_pending_migration_is_not_drift():
    """The ordinary case: DB one behind the image. `upgrade head` must still run."""
    report = evaluate_drift(
        db_revisions=["zc00000000039"],
        known_revisions=["zc00000000039", "zc00000000040"],
        image_heads=["zc00000000040"],
    )
    assert report.status == "ok"
    assert not report.has_drift


def test_up_to_date_database_is_ok():
    report = evaluate_drift(["zc00000000040"], ["zc00000000040"], ["zc00000000040"])
    assert report.status == "ok"


def test_empty_alembic_version_is_fresh_not_drift():
    """A brand-new database must not be mistaken for drift."""
    report = evaluate_drift([], ["zc00000000040"], ["zc00000000040"])
    assert report.status == "fresh"
    assert not report.has_drift


def test_blank_and_whitespace_rows_are_ignored():
    report = evaluate_drift(["", "  "], ["zc00000000040"], ["zc00000000040"])
    assert report.status == "fresh"


def test_multi_head_database_flags_only_the_unknown_revision():
    report = evaluate_drift(
        db_revisions=["zc00000000039", "zc00000000041"],
        known_revisions=["zc00000000039", "zc00000000040"],
        image_heads=["zc00000000040"],
    )
    assert report.status == "drift"
    assert report.unknown_revisions == ("zc00000000041",)


def test_revisions_are_matched_exactly_not_by_prefix():
    """A near-miss id is drift, not a fuzzy match."""
    report = evaluate_drift(["zc0000000004"], ["zc00000000040"], ["zc00000000040"])
    assert report.status == "drift"


# ---------------------------------------------------------------------------
# Guard 2: policy + boot decision — the part that ends the outage
# ---------------------------------------------------------------------------


def test_default_policy_boots_the_app_on_drift():
    """The whole point: drift must no longer keep the container from serving."""
    report = evaluate_drift(["zc00000000040"], ["zc00000000039"], ["zc00000000039"])
    policy = resolve_drift_policy({})
    assert policy == "skip"
    action = decide_boot_action(report, policy)
    assert action == "skip"
    assert boot_exit_code(action) == EXIT_SKIP_MIGRATIONS


def test_fail_policy_restores_the_old_hard_fail():
    report = evaluate_drift(["zc00000000040"], ["zc00000000039"], ["zc00000000039"])
    policy = resolve_drift_policy({DRIFT_POLICY_ENV: "fail"})
    assert policy == "fail"
    action = decide_boot_action(report, policy)
    assert action == "abort"
    # NOT 1: callers must be able to tell a deliberate refusal from a crash.
    assert boot_exit_code(action) == EXIT_DRIFT_BLOCKED
    assert EXIT_DRIFT_BLOCKED != 1


@pytest.mark.parametrize("raw", ["skip", "warn", "open", "SKIP", " skip "])
def test_skip_aliases(raw):
    assert resolve_drift_policy({DRIFT_POLICY_ENV: raw}) == "skip"


@pytest.mark.parametrize("raw", ["fail", "abort", "strict", "FAIL"])
def test_fail_aliases(raw):
    assert resolve_drift_policy({DRIFT_POLICY_ENV: raw}) == "fail"


def test_typo_in_policy_falls_back_to_skip():
    """An operator typo must not arm the behaviour that caused the outage."""
    assert resolve_drift_policy({DRIFT_POLICY_ENV: "fial"}) == "skip"
    assert resolve_drift_policy({DRIFT_POLICY_ENV: ""}) == "skip"


@pytest.mark.parametrize("status", ["ok", "fresh", "unknown"])
@pytest.mark.parametrize("policy", ["skip", "fail"])
def test_non_drift_always_runs_migrations(status, policy):
    """Notably `unknown` (database unreachable): the transient cold-start 403 must
    fall through to start.sh's retry loop, never to a skip or an abort."""
    action = decide_boot_action(DriftReport(status=status), policy)
    assert action == "run"
    assert boot_exit_code(action) == EXIT_RUN_MIGRATIONS


# ---------------------------------------------------------------------------
# The diagnostic — its whole job is naming both sides of the drift
# ---------------------------------------------------------------------------


def test_diagnostic_names_both_sides_and_the_remedy():
    report = evaluate_drift(["zc00000000040"], ["zc00000000039"], ["zc00000000039"])
    text = format_drift_diagnostic(
        report,
        action="skip",
        policy="skip",
        recent_revisions=("zc00000000039", "zc00000000038"),
        git_sha="fbfcc49abc12",
        git_branch="main",
    )
    assert "zc00000000040" in text  # what the database claims
    assert "zc00000000039" in text  # what the image ships
    assert "fbfcc49abc12" in text
    assert "main" in text
    assert "WITHOUT migrating" in text
    assert "alembic stamp zc00000000039" in text


def test_fail_diagnostic_warns_it_is_the_outage_shape():
    report = evaluate_drift(["zc00000000040"], ["zc00000000039"], ["zc00000000039"])
    text = format_drift_diagnostic(report, action="abort", policy="fail")
    assert "refusing to start" in text
    assert "update-traffic" in text  # the rollback trap, spelled out
    assert "MIGRATION_DRIFT_POLICY=skip" in text


def test_diagnostic_survives_missing_heads():
    """Never crash while reporting a problem."""
    text = format_drift_diagnostic(
        DriftReport(status="drift", db_revisions=("abc",), unknown_revisions=("abc",)),
        action="skip",
        policy="skip",
    )
    assert "<head>" in text


# ---------------------------------------------------------------------------
# Pre-deploy: migrations in the tree that aren't on origin/main
# ---------------------------------------------------------------------------


def test_unmerged_revision_is_detected():
    """zc040 present locally, absent on main — the root cause, pre-deploy."""
    assert unmerged_revision_files(
        local_files=[
            "/wt/alembic/versions/zc00000000039_add_eos_ingest_state.py",
            "/wt/alembic/versions/zc00000000040_add_l10_session_recap.py",
        ],
        main_files=["alembic/versions/zc00000000039_add_eos_ingest_state.py"],
    ) == ("zc00000000040_add_l10_session_recap.py",)


def test_worktree_path_prefixes_do_not_create_false_positives():
    """Local paths are absolute worktree paths; main's are repo-relative."""
    assert (
        unmerged_revision_files(
            local_files=["/repo/.claude/worktrees/x/alembic/versions/zc040_a.py"],
            main_files=["alembic/versions/zc040_a.py"],
        )
        == ()
    )


def test_non_python_and_dunder_files_are_ignored():
    assert (
        unmerged_revision_files(
            local_files=[
                "alembic/versions/__init__.py",
                "alembic/versions/README",
                "alembic/versions/.DS_Store",
            ],
            main_files=[],
        )
        == ()
    )


# ---------------------------------------------------------------------------
# Integration: real script directory + a real (sqlite) alembic_version table
# ---------------------------------------------------------------------------


def test_image_revisions_reads_the_real_alembic_directory():
    """Guards against an Alembic API change silently disabling the preflight."""
    from rubik_core.database.migration_preflight import image_revisions

    known, heads, newest = image_revisions()
    assert len(heads) == 1, f"expected a single head, got {heads}"
    assert heads[0] in known
    # The chain starts at the single Cubie baseline.
    assert "0001_cubie_baseline" in known
    assert newest[0] == heads[0]
    assert set(newest) <= known


def test_real_directory_plus_a_bogus_db_revision_is_drift():
    from rubik_core.database.migration_preflight import image_revisions

    known, heads, _ = image_revisions()
    assert evaluate_drift(["zc99999999999"], known, heads).status == "drift"
    assert evaluate_drift([heads[0]], known, heads).status == "ok"


def test_database_revisions_reads_alembic_version_from_a_real_db(tmp_path, monkeypatch):
    """Exercises the live read path end to end against sqlite."""
    from sqlalchemy import create_engine, text

    db_path = tmp_path / "drift.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        conn.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        conn.execute(text("INSERT INTO alembic_version VALUES ('zc00000000040')"))
        conn.commit()

    from rubik_core.database import migration_preflight as pf

    monkeypatch.setattr(pf, "_preflight_engine", lambda _slug: engine)
    assert pf.database_revisions("test") == ("zc00000000040",)


def test_database_revisions_returns_none_when_unreachable(monkeypatch):
    """Unreachable != drift: must degrade to 'run migrations as usual'."""

    def boom():
        raise OSError("connection refused")

    from rubik_core.database import migration_preflight as pf

    monkeypatch.setattr(pf, "_preflight_engine", lambda _slug: boom())
    assert pf.database_revisions("test") is None


def test_fresh_database_reads_as_empty_not_an_error(tmp_path, monkeypatch):
    """No alembic_version table at all — must be 'fresh', and must not create it."""
    from sqlalchemy import create_engine, inspect

    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    from rubik_core.database import migration_preflight as pf

    monkeypatch.setattr(pf, "_preflight_engine", lambda _slug: engine)
    assert pf.database_revisions("test") == ()
    assert "alembic_version" not in inspect(engine).get_table_names()


# ---------------------------------------------------------------------------
# End-to-end: the CLI contract that scripts/start.sh actually branches on
# ---------------------------------------------------------------------------

_REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]


def _stamped_sqlite(tmp_path, revision: str | None):
    """A sqlite database stamped at `revision` (or with no version table)."""
    from sqlalchemy import create_engine, text

    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    if revision is not None:
        with engine.connect() as conn:
            conn.execute(
                text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
            )
            conn.execute(text(f"INSERT INTO alembic_version VALUES ('{revision}')"))
            conn.commit()
    return db_path


def _restamp_sqlite(db_path, revision: str):
    """Leave `alembic_version` at `revision` — what a successful upgrade does.

    Stands in for `alembic upgrade head` on purpose: the thing under test is the
    marker refresh, not the schema. Creates the version table when the database
    started fresh, as the first upgrade does.
    """
    from sqlalchemy import create_engine, text

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS alembic_version "
                "(version_num VARCHAR(32) NOT NULL)"
            )
        )
        conn.execute(text("DELETE FROM alembic_version"))
        conn.execute(text("INSERT INTO alembic_version VALUES (:r)"), {"r": revision})
        conn.commit()


def _head_and_parent():
    """This image's head revision and its parent (None while the head is the baseline).

    Built through the preflight's own `_alembic_ini()` so the test and the code
    under test can never disagree about which script directory they are reading.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from rubik_core.database.migration_preflight import _alembic_ini

    script_dir = ScriptDirectory.from_config(Config(str(_alembic_ini())))
    head = script_dir.get_current_head()
    parent = script_dir.get_revision(head).down_revision
    assert parent is None or isinstance(parent, str), f"head {head} has two parents"
    return head, parent


def _markers(marker_path):
    """Parse the shell-sourceable marker file `start.sh` exports for /health."""
    return dict(
        line.split("=", 1)
        for line in marker_path.read_text().splitlines()
        if "=" in line
    )


def _run_preflight(db_path, tmp_path, extra_env=None, mode="boot"):
    import subprocess

    env = {
        "PATH": __import__("os").environ.get("PATH", ""),
        "HOME": __import__("os").environ.get("HOME", ""),
        "GCP_PROJECT_ID": "",
        "APP_MODE": "dev_mock_auth",
        "DATABASE_URL": f"sqlite:///{db_path}",
        "WORKSPACE_DATABASE_DIR": str(db_path.parent),
        "CUBIE_WORKSPACE": "test",
        "CLOUDSQL_CONNECTION_NAME": "",
        "DB_USER": "",
        **(extra_env or {}),
    }
    marker = tmp_path / "preflight.env"
    proc = subprocess.run(
        [
            __import__("sys").executable,
            "-m",
            "rubik_core.database.migration_preflight",
            "--mode",
            mode,
            "--emit-env",
            str(marker),
        ],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc, marker


def test_cli_boot_exits_3_and_emits_markers_on_drift(tmp_path):
    """Replays the incident: DB stamped at a revision no image ships.

    start.sh reads exit 3 as "skip migrations, still serve traffic", and sources
    the marker file so /health can report the drift.
    """
    db = _stamped_sqlite(tmp_path, "zc99999999999")
    proc, marker = _run_preflight(db, tmp_path)

    assert proc.returncode == EXIT_SKIP_MIGRATIONS, proc.stderr
    assert "ALEMBIC REVISION DRIFT" in proc.stderr
    assert "zc99999999999" in proc.stderr

    markers = _markers(marker)
    assert markers["CUBIE_MIGRATIONS_SKIPPED"] == "1"
    assert markers["CUBIE_MIGRATION_DRIFT"] == "zc99999999999"
    assert markers["CUBIE_MIGRATION_DB_REVISION"] == "zc99999999999"
    assert markers["CUBIE_MIGRATION_IMAGE_HEAD"]  # the image's real head
    assert markers["CUBIE_MIGRATION_STAGE"] == "preflight"


def test_cli_boot_exits_4_when_policy_is_fail(tmp_path):
    db = _stamped_sqlite(tmp_path, "zc99999999999")
    proc, marker = _run_preflight(db, tmp_path, {DRIFT_POLICY_ENV: "fail"})

    assert proc.returncode == EXIT_DRIFT_BLOCKED, proc.stderr
    markers = _markers(marker)
    assert markers["CUBIE_MIGRATIONS_SKIPPED"] == "0"


def test_cli_boot_exits_0_on_a_fresh_database(tmp_path):
    """No version table -> normal `alembic upgrade head` path."""
    db = _stamped_sqlite(tmp_path, None)
    proc, _ = _run_preflight(db, tmp_path)
    assert proc.returncode == EXIT_RUN_MIGRATIONS, proc.stderr


def test_cli_boot_exits_0_when_the_database_is_at_the_image_head(tmp_path):
    from rubik_core.database.migration_preflight import image_revisions

    _, heads, _ = image_revisions()
    db = _stamped_sqlite(tmp_path, heads[0])
    proc, _ = _run_preflight(db, tmp_path)
    assert proc.returncode == EXIT_RUN_MIGRATIONS, proc.stderr
    assert "ALEMBIC REVISION DRIFT" not in proc.stderr


# ---------------------------------------------------------------------------
# Post-migration marker refresh
# ---------------------------------------------------------------------------


def test_postmigrate_reports_the_new_revision_after_a_migration(tmp_path):
    """A container that APPLIES a migration must report the NEW revision.

    Replays a migration-carrying deploy end to end: the database starts one
    revision behind (empty, while the baseline is the only revision), the boot
    preflight reads that older state (correct for its stage — it runs before
    `alembic upgrade head`), the upgrade moves the version row, and the refresh
    has to overwrite the marker.

    Without the refresh the container serves the pre-migration revision for its
    entire life, and `db_revision` behind `image_head` is exactly what a *failed*
    migration looks like: the revision reports the DB one step behind its own
    head while its own logs say the upgrade succeeded, and the smoke check is
    read as a failure.
    """
    head, parent = _head_and_parent()
    db = _stamped_sqlite(tmp_path, parent)

    boot, marker = _run_preflight(db, tmp_path)
    assert boot.returncode == EXIT_RUN_MIGRATIONS, boot.stderr
    assert _markers(marker)["CUBIE_MIGRATION_DB_REVISION"] == (parent or "")

    _restamp_sqlite(db, head)  # `alembic upgrade head` lands

    post, marker = _run_preflight(db, tmp_path, mode="postmigrate")
    assert post.returncode == 0, post.stderr

    markers = _markers(marker)
    assert markers["CUBIE_MIGRATION_DB_REVISION"] == head
    assert (
        markers["CUBIE_MIGRATION_DB_REVISION"] == markers["CUBIE_MIGRATION_IMAGE_HEAD"]
    )
    assert markers["CUBIE_MIGRATION_STAGE"] == "postmigrate"
    assert markers["CUBIE_MIGRATIONS_SKIPPED"] == "0"
    assert markers["CUBIE_MIGRATION_DRIFT"] == ""


def test_postmigrate_falls_back_to_the_image_head_when_the_read_fails(tmp_path):
    """An unreachable database must not blank the marker — or keep the stale one.

    `alembic upgrade head` has already exited 0 by the time this runs, so the
    database IS at this image's head; reporting that beats reporting nothing, and
    beats leaving the pre-migration value that started the whole problem.
    """
    head, _ = _head_and_parent()
    unreachable = tmp_path / "no-such-dir" / "test.db"  # sqlite: cannot open

    proc, marker = _run_preflight(unreachable, tmp_path, mode="postmigrate")
    assert proc.returncode == 0, proc.stderr

    markers = _markers(marker)
    assert markers["CUBIE_MIGRATION_DB_REVISION"] == head
    assert markers["CUBIE_MIGRATION_STAGE"] == "postmigrate"

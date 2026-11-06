"""Alembic revision-drift preflight — the I/O half of :mod:`migration_guard`.

Three entry points, one shared decision core. Every entry names a workspace
through ``--workspace`` or ``CUBIE_WORKSPACE``:

``--mode boot`` (run by ``scripts/start.sh`` before ``alembic upgrade head``)
    Reads the live ``alembic_version`` and compares it against the revisions
    baked into *this image*. On drift it prints a loud diagnostic naming both
    sides and exits ``3`` ("skip migrations, start the app anyway"), so a
    database stamped from an unmerged branch can no longer keep the service from
    booting. Exit ``0`` means "proceed with migrations" — including when the
    preflight itself could not reach the database, so the existing
    retry-with-backoff loop keeps owning connectivity failures.

``--mode postmigrate`` (run by ``scripts/start.sh`` *after* a successful upgrade)
    Re-reads ``alembic_version`` and rewrites the marker file, because the boot
    preflight's read predates the migration it is about to allow: on every deploy
    carrying a migration, ``/health`` used to report the OLD revision for the
    whole life of the container. Always exits ``0`` — it rewrites a diagnostic,
    not the schema.

``--mode predeploy`` (legacy manual diagnostic)
    Reads workspace revision drift for an explicitly named workspace. The
    platform deploy controller instead reads the registered control revision
    before building and runs one dedicated migration Job before Service rollout.

Lives under ``rubik_core/`` rather than ``scripts/`` on purpose: the Dockerfile
copies ``rubik_core/`` wholesale but only ``scripts/start.sh``, so this is
reachable from inside the image with no Dockerfile change.

Usage::

    python -m rubik_core.database.migration_preflight --mode boot
    python -m rubik_core.database.migration_preflight --mode postmigrate
    python -m rubik_core.database.migration_preflight --mode predeploy
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from queue import Queue

from rubik_core.database.migration_guard import (
    DRIFT_POLICY_ENV,
    EXIT_DRIFT_BLOCKED,
    EXIT_RUN_MIGRATIONS,
    DriftReport,
    boot_exit_code,
    decide_boot_action,
    evaluate_drift,
    format_drift_diagnostic,
    format_marker_env,
    resolve_drift_policy,
    unmerged_revision_files,
)
from rubik_core.tenancy.threads import context_thread

#: Cap on the live-database read. The preflight sits directly in front of boot,
#: so a hung connector must not eat the Cloud Run startup probe budget: on
#: timeout we report "unknown" and let the normal migration path run.
DB_READ_TIMEOUT_SECONDS = 25

#: Cap on the post-migration re-read. Shorter than the boot cap on purpose:
#: ``alembic upgrade head`` has *just* driven the same connection path, so a hang
#: here means something new is wrong — and the fallback (this image's head, which
#: a successful upgrade guarantees) is right anyway. No reason to spend 25s of
#: startup-probe budget re-proving it.
POSTMIGRATE_READ_TIMEOUT_SECONDS = 10

#: Pre-deploy escape hatches, printed in the failure message.
ALLOW_DRIFT_ENV = "MIGRATION_PREFLIGHT_ALLOW_DRIFT"
STRICT_ENV = "MIGRATION_PREFLIGHT_STRICT"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _alembic_ini() -> Path:
    """Locate alembic.ini: cwd first (container WORKDIR=/app), then repo root."""
    cwd_ini = Path.cwd() / "alembic.ini"
    return cwd_ini if cwd_ini.is_file() else _repo_root() / "alembic.ini"


# ---------------------------------------------------------------------------
# I/O: what this image knows, what the database claims
# ---------------------------------------------------------------------------


def image_revisions() -> tuple[set[str], tuple[str, ...], tuple[str, ...]]:
    """Return ``(all_revision_ids, heads, newest_five)`` from the script directory."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script_dir = ScriptDirectory.from_config(Config(str(_alembic_ini())))
    scripts = list(script_dir.walk_revisions())  # heads -> base
    all_revs = {s.revision for s in scripts}
    return (
        all_revs,
        tuple(script_dir.get_heads()),
        tuple(s.revision for s in scripts[:5]),
    )


def _preflight_engine(slug: str):
    """An uncached connection to the named registered or local test database."""
    from rubik_core.config import AppMode, settings
    from rubik_core.control.models import WorkspaceConnectionSpec
    from rubik_core.control.repository import get_workspace
    from rubik_core.platform.migrate import _registered_spec
    from rubik_core.tenancy.context import _canonical_slug
    from rubik_core.tenancy.registry import engine_from_connection_spec

    _canonical_slug(slug)
    if (
        slug == "test"
        and settings.APP_MODE != AppMode.GCP
        and not settings.CLOUDSQL_CONNECTION_NAME
    ):
        spec = WorkspaceConnectionSpec("local_db", "local_user", "local-secret-ref")
        registered = False
    else:
        workspace = get_workspace(slug)
        if workspace is None or workspace.status not in ("active", "suspended"):
            raise ValueError(f"Workspace {slug!r} is not registered")
        spec = _registered_spec(workspace)
        registered = True
    return engine_from_connection_spec(slug, spec, registered=registered)


def database_revisions(
    slug: str, timeout: float = DB_READ_TIMEOUT_SECONDS
) -> tuple[str, ...] | None:
    """Read ``alembic_version``; ``None`` when the database is unreachable.

    Uses Alembic's own :class:`MigrationContext`, which returns an empty tuple
    for a fresh database instead of creating the version table.
    """
    from alembic.migration import MigrationContext

    result: Queue[tuple[bool, object]] = Queue(maxsize=1)

    def _read() -> None:
        engine = None
        try:
            engine = _preflight_engine(slug)
            with engine.connect() as conn:
                result.put(
                    (True, tuple(MigrationContext.configure(conn).get_current_heads()))
                )
        except Exception as exc:  # noqa: BLE001 — reported in the caller thread
            result.put((False, exc))
        finally:
            if engine is not None:
                engine.dispose()

    # A ThreadPoolExecutor context manager waits for the worker on exit, even
    # after Future.result(timeout) raises. A daemon thread lets preflight honor
    # its boot budget when a connector hangs.
    worker = context_thread(_read, daemon=True, name=f"preflight-{slug}")
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        print(
            f"[migration-preflight] database read exceeded {timeout}s; "
            "skipping the drift check and letting the normal migration path run.",
            file=sys.stderr,
        )
        return None
    ok, value = result.get_nowait()
    if not ok:
        exc = value
        print(
            f"[migration-preflight] could not read alembic_version for {slug} "
            f"({exc.__class__.__name__}); "
            "skipping the drift check and letting the normal migration path run.",
            file=sys.stderr,
        )
        return None
    return value


def describe_target(slug: str) -> str:
    """Human label for the database this run will inspect.

    Printed on every run because the same command checks dev or prod depending
    on which env file the caller sourced — a verdict about the wrong database is
    worse than no verdict.
    """
    try:
        from rubik_core.config import settings

        from .migration_guard import describe_remote_target

        remote = describe_remote_target(
            settings.CLOUDSQL_CONNECTION_NAME, settings.DATABASE_URL
        )
        return f"workspace {slug} at {remote}" if remote else f"local workspace {slug}"
    except Exception as exc:  # noqa: BLE001
        return f"unknown ({exc.__class__.__name__})"


def build_report(
    slug: str,
    timeout: float = DB_READ_TIMEOUT_SECONDS,
) -> tuple[DriftReport, tuple[str, ...]]:
    """Evaluate drift against the live database. Returns ``(report, newest_five)``."""
    known, heads, newest = image_revisions()
    db_revs = database_revisions(slug, timeout)
    if db_revs is None:
        return (
            DriftReport(
                status="unknown",
                image_heads=heads,
                detail="database unreachable from the preflight",
            ),
            newest,
        )
    return evaluate_drift(db_revs, known, heads), newest


def preflight_workspace(slug: str) -> DriftReport:
    """Read migration status for one explicit workspace."""
    return build_report(slug)[0]


# ---------------------------------------------------------------------------
# Mode: boot
# ---------------------------------------------------------------------------


def run_boot(emit_env: str | None, slug: str) -> int:
    # Resolved once: an unrecognised value logs a warning, and logging it twice
    # would read as two separate misconfigurations.
    policy = resolve_drift_policy()
    print(
        f"[migration-preflight] policy {DRIFT_POLICY_ENV}={policy} "
        "(default 'skip': start the app on drift)",
        file=sys.stderr,
    )
    report, newest = build_report(slug)
    action = decide_boot_action(report, policy)

    if report.has_drift:
        print(
            format_drift_diagnostic(
                report,
                action=action,
                policy=policy,
                recent_revisions=newest,
                git_sha=os.environ.get("GIT_SHA"),
                git_branch=os.environ.get("GIT_BRANCH"),
            ),
            file=sys.stderr,
        )
    else:
        print(
            f"[migration-preflight] {report.status}: db={','.join(report.db_revisions) or '-'} "
            f"image_head={','.join(report.image_heads) or '-'} — {report.detail}",
            file=sys.stderr,
        )

    if emit_env:
        # Sourced by start.sh so /health can report the drift without a per-probe
        # database round trip. Stage `preflight`: this read happened BEFORE any
        # migration, so on the "run" path `run_postmigrate` overwrites it below.
        Path(emit_env).write_text(
            format_marker_env(
                stage="preflight",
                skipped=action == "skip",
                drift=report.unknown_revisions,
                image_heads=report.image_heads,
                db_revisions=report.db_revisions,
            )
        )

    return boot_exit_code(action)


# ---------------------------------------------------------------------------
# Mode: postmigrate
# ---------------------------------------------------------------------------


def run_postmigrate(emit_env: str | None, slug: str) -> int:
    """Rewrite the ``/health`` markers after ``alembic upgrade head`` succeeded.

    The boot preflight can only read ``alembic_version`` *before* the upgrade it
    gates, so its markers describe the pre-migration database. Left at that, a
    container that successfully applies a migration reports the OLD revision for
    its entire life — and ``db_revision`` behind ``image_head`` is precisely what
    a failed migration looks like, so the post-deploy smoke check reads "the
    migration failed" on a deploy that worked. Re-reading here makes the block describe the
    database the app is actually serving.

    Always returns ``0``. This rewrites a diagnostic, not the schema: a failure
    to refresh must never turn a healthy boot into a dead revision.
    """
    report, _ = build_report(slug, POSTMIGRATE_READ_TIMEOUT_SECONDS)
    db_revisions = report.db_revisions
    detail = report.detail
    if not db_revisions:
        # `alembic upgrade head` exited 0, so the database IS at this image's
        # head(s). A read that timed out or came back empty must not blank the
        # marker — and must certainly not leave the stale pre-migration value,
        # which is the whole bug this mode exists to fix.
        db_revisions = report.image_heads
        detail = (
            f"live read came back {report.status} — reporting this image's head(s), "
            "which the successful upgrade guarantees"
        )

    if emit_env:
        Path(emit_env).write_text(
            format_marker_env(
                stage="postmigrate",
                skipped=False,
                drift=report.unknown_revisions,
                image_heads=report.image_heads,
                db_revisions=db_revisions,
            )
        )

    print(
        f"[migration-postmigrate] db={','.join(db_revisions) or '-'} "
        f"image_head={','.join(report.image_heads) or '-'} — {detail}",
        file=sys.stderr,
    )
    return 0


# ---------------------------------------------------------------------------
# Mode: predeploy
# ---------------------------------------------------------------------------


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=_repo_root(),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except Exception:  # noqa: BLE001 — git is advisory here
        return None
    return out.stdout if out.returncode == 0 else None


def unmerged_migrations() -> tuple[str, ...] | None:
    """Migration files in the working tree but not on ``origin/main``.

    ``None`` when ``origin/main`` is unavailable (no remote ref, no git), which
    downgrades the check to a note rather than a blocker.
    """
    if _git("rev-parse", "--verify", "--quiet", "origin/main") is None:
        return None
    main_listing = _git(
        "ls-tree", "-r", "--name-only", "origin/main", "alembic/versions"
    )
    if main_listing is None:
        return None
    local = [str(p) for p in (_repo_root() / "alembic" / "versions").glob("*.py")]
    return unmerged_revision_files(local, main_listing.splitlines())


def run_predeploy(slug: str) -> int:
    print(f"[migration-preflight] target: {describe_target(slug)}", file=sys.stderr)
    report, newest = build_report(slug)
    blocked = False

    if report.has_drift:
        print(
            format_drift_diagnostic(
                report,
                action="abort",
                policy="fail",
                recent_revisions=newest,
            ),
            file=sys.stderr,
        )
        if _flag(ALLOW_DRIFT_ENV):
            print(
                f"[migration-preflight] {ALLOW_DRIFT_ENV}=1 — continuing despite drift.",
                file=sys.stderr,
            )
        else:
            print(
                "[migration-preflight] REFUSING to deploy: the target database is at a\n"
                "  revision this tree cannot resolve. Land the missing migration first,\n"
                f"  or override with {ALLOW_DRIFT_ENV}=1 (e.g. when this deploy IS the fix).",
                file=sys.stderr,
            )
            blocked = True
    else:
        print(
            f"[migration-preflight] drift check {report.status}: "
            f"db={','.join(report.db_revisions) or '-'} "
            f"tree_head={','.join(report.image_heads) or '-'}",
            file=sys.stderr,
        )

    unmerged = unmerged_migrations()
    if unmerged is None:
        print(
            "[migration-preflight] origin/main unavailable — skipped the "
            "unmerged-migration check.",
            file=sys.stderr,
        )
    elif unmerged:
        strict = _flag(STRICT_ENV)
        print(
            "\n"
            + "=" * 78
            + "\nUNMERGED MIGRATIONS IN THIS TREE — they will be applied to the shared DB"
            + "\n"
            + "=" * 78
            + "\n  "
            + "\n  ".join(unmerged)
            + "\n\n  `--source .` / `builds submit .` upload the WORKING TREE, and this deploy\n"
            "  sets AUTO_MIGRATE=true — so these revisions get stamped onto the shared\n"
            "  database. Until they land on main, every container built from main will\n"
            '  fail `alembic upgrade head` with "Can\'t locate revision". That is the\n'
            "  near-outage this check exists for. The boot preflight starts the app anyway,\n"
            "  but main-built revisions will run UNMIGRATED until this branch merges.\n"
            "\n  Deploying a feature branch to dev? Fine — just merge it promptly, or\n"
            f"  deploy from main instead. Set {STRICT_ENV}=1 to make this a hard stop.\n"
            + "="
            * 78,
            file=sys.stderr,
        )
        if strict:
            print(
                f"[migration-preflight] REFUSING to deploy: {STRICT_ENV}=1 and "
                f"{len(unmerged)} migration(s) are not on origin/main.",
                file=sys.stderr,
            )
            blocked = True

    return EXIT_DRIFT_BLOCKED if blocked else EXIT_RUN_MIGRATIONS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="migration_preflight",
        description="Alembic revision-drift preflight (see docs/ENGINEERING_NOTES.md).",
    )
    parser.add_argument(
        "--mode", choices=("boot", "postmigrate", "predeploy"), required=True
    )
    parser.add_argument(
        "--emit-env",
        metavar="PATH",
        help="boot/postmigrate modes: write shell-sourceable markers to PATH.",
    )
    parser.add_argument(
        "--workspace",
        default=os.environ.get("CUBIE_WORKSPACE"),
        help="explicit workspace slug; defaults to CUBIE_WORKSPACE",
    )
    args = parser.parse_args(argv)
    if not args.workspace:
        parser.error("--workspace or CUBIE_WORKSPACE is required")
    from rubik_core.tenancy.context import _canonical_slug

    slug = _canonical_slug(args.workspace)

    if args.mode == "boot":
        return run_boot(args.emit_env, slug)
    if args.mode == "postmigrate":
        return run_postmigrate(args.emit_env, slug)
    return run_predeploy(slug)


if __name__ == "__main__":  # pragma: no cover — exercised via scripts
    # A bug in this preflight must never be a new way to fail a boot or a
    # deploy — that is the failure mode it exists to remove. Anything
    # unexpected falls back to "proceed as before" (exit 0). Argparse's own
    # SystemExit(2) still propagates so a bad flag stays visible.
    try:
        _code = main()
    except SystemExit:
        raise
    except BaseException as _exc:  # noqa: BLE001
        print(
            f"[migration-preflight] preflight itself failed "
            f"({_exc.__class__.__name__}: {_exc}); proceeding with the normal path.",
            file=sys.stderr,
        )
        _code = EXIT_RUN_MIGRATIONS
    sys.exit(_code)

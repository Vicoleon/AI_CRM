"""The worker-job registry stays honest (``backend/jobs/__init__.py``).

The registry is documentation with a test attached, not a dispatcher: Cloud Run
invokes each module directly, so a stale entry cannot break a running job — it
can only mislead the next person who runs one by hand, or hide a job that
nothing dispatches. These checks are what keep it true:

* every registered module imports and accepts the arguments it documents;
* ``dispatched_from`` never names a file that does not launch the job;
* an override replaces the defaults instead of extending them;
* the registry and the launcher name the same Cloud Run job;
* the migration chain has exactly one head.

Nothing here touches the network or a database.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_every_registered_module_imports_and_accepts_its_documented_args():
    """A registry that names a module nobody can import is worse than none."""
    from backend.jobs import WORKER_JOBS

    for name, job in WORKER_JOBS.items():
        module = importlib.import_module(job.module)
        entry = getattr(module, "main", None) or getattr(module, "run_sync", None)
        assert callable(entry), f"{name}: {job.module} has no callable entrypoint"
        parser = getattr(module, "_build_parser", None)
        if parser is None:
            continue
        for args in (job.default_args, job.example_args):
            if args:
                example = [
                    "placeholder" if a.startswith("<") and a.endswith(">") else a
                    for a in args
                ]
                # Raises SystemExit on an unknown flag — which is the point.
                parser().parse_args(example)


def test_the_registry_does_not_claim_a_dispatcher_that_does_not_exist():
    """``dispatched_from`` is the honesty field; a stale one hides an inert job."""
    from backend.jobs import WORKER_JOBS

    for name, job in WORKER_JOBS.items():
        if not job.dispatched_from:
            continue
        source = REPO_ROOT / job.dispatched_from
        assert source.exists(), f"{name}: {job.dispatched_from} does not exist"
        assert job.module in source.read_text(encoding="utf-8"), (
            f"{name}: {job.dispatched_from} does not mention {job.module}, so "
            "the registry claims a dispatcher that is not there."
        )


def test_an_override_replaces_the_defaults_rather_than_extending_them():
    """ "Just this one" must not inherit a default that makes it a full sweep."""
    from backend.jobs import worker_job_args

    assert worker_job_args("transcript-sync") == [
        "-m",
        "rubik_core.scripts.sync_meet_transcripts",
        "--days",
        "7",
    ]
    assert worker_job_args("transcript-sync", "--days", "1") == [
        "-m",
        "rubik_core.scripts.sync_meet_transcripts",
        "--days",
        "1",
    ]


def test_the_registry_and_the_launcher_name_the_same_job():
    from backend.jobs import WORKER_JOB_NAME
    from backend.jobs.launcher import WORKER_JOB_NAME as LAUNCHER_NAME

    assert WORKER_JOB_NAME == LAUNCHER_NAME


def test_one_off_worker_command_names_a_workspace():
    from backend.jobs import worker_job_execute_command

    command = worker_job_execute_command(
        "transcript-sync", workspace="acme", project="project-test"
    )
    assert '--update-env-vars="CUBIE_WORKSPACE=acme"' in command


def test_there_is_exactly_one_alembic_head():
    """Two heads stop every migration on the next deploy, for everyone.

    Read statically rather than through Alembic's ScriptDirectory so this needs
    no database and no alembic.ini resolution — the failure it catches (two
    branches numbered off the same parent in parallel worktrees) is visible in
    the files themselves.
    """
    versions = REPO_ROOT / "alembic" / "versions"
    revisions: dict[str, str] = {}
    parents: set[str] = set()
    for path in versions.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        rev = re.search(r'^revision:?\s*(?::\s*str\s*)?=\s*"([^"]+)"', text, re.M)
        if not rev:
            continue
        revisions[rev.group(1)] = path.name
        down = re.search(r"^down_revision.*?=\s*(.+)$", text, re.M)
        if down:
            parents.update(re.findall(r'"([^"]+)"', down.group(1)))

    heads = sorted(set(revisions) - parents)
    assert len(heads) == 1, (
        f"Alembic has {len(heads)} heads: {[revisions[h] for h in heads]}. "
        "Re-parent the newer one onto the other (or add a merge revision) — "
        "`alembic upgrade head` refuses to run with more than one."
    )

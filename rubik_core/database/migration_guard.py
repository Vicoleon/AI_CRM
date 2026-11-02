"""Guards against the two Alembic mistakes that have taken the deployed app down.

Both halves exist because of a dev near-outage: a worktree session applied a
revision that lived only on an unmerged branch to the *shared* dev Cloud SQL
database. Every container built from `main` then ran `alembic upgrade head` at
boot, could not resolve the revision the DB claimed to be on, and killed
itself::

    ERROR [alembic.util.messaging] Can't locate revision identified by '<rev>'
    Migrations FAILED after 5 attempts.
    Default STARTUP TCP probe failed ... on port 8080

Nothing listened on :8080, so the revision failed with HealthCheckContainerError
— and because routing changes force a cold start, `gcloud run services
update-traffic` could not roll back either. The service survived only on a warm
pre-drift instance, and merged work never reached users.

So this module provides:

**Guard 1 — don't migrate a deployed database from a laptop**
(:func:`assert_migration_target_allowed`, wired into ``alembic/env.py``).
``.env`` carries ``CLOUDSQL_CONNECTION_NAME``, so a bare ``alembic upgrade head``
in a worktree silently migrates *shared dev*. Mutating commands now refuse to
touch a non-local database unless the caller is a managed migration Job or has set
``ALEMBIC_ALLOW_REMOTE=1`` deliberately. This is the *prevention* half.

**Guard 2 — a drifted database must not cost us the service**
(:func:`evaluate_drift` / :func:`decide_boot_action`, wired into
:mod:`rubik_core.database.migration_preflight`). When the DB is at a revision the image
has never heard of, hard-failing the boot is the *worst* available option: it turns a
schema annoyance into a dead service that cannot even be rolled back. The default policy
is now to log a loud, named diagnostic and **start the app without migrating** —
additive drift means the live schema is a superset of what the image expects.
``MIGRATION_DRIFT_POLICY=fail`` restores the old hard-fail for callers that would rather
refuse to start.

Everything here is pure logic over injected values: no imports of app settings,
no database or filesystem access at import time. Callers (``alembic/env.py`` and
:mod:`rubik_core.database.migration_preflight`) do the I/O and pass the results
in, which is what makes the whole thing unit-testable without a database.
See ``backend/tests/test_migration_guard.py``.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Guard 1: refuse to mutate a deployed database from an ad-hoc local run
# ---------------------------------------------------------------------------

#: A Cloud Run service must never migrate on boot. Only the dedicated migration
#: Job may upgrade remote schemas without a deliberate operator override.
MANAGED_RUNTIME_MARKERS: tuple[str, ...] = (
    "CLOUD_RUN_JOB",
    "CLOUD_RUN_EXECUTION",
)

#: Explicit, deliberate override for the rare intentional local migration
#: (schema repair, `alembic stamp` after a reconciliation).
REMOTE_OVERRIDE_ENV = "ALEMBIC_ALLOW_REMOTE"

#: Alembic subcommands that *change* the target database. Read-only commands
#: (`current`, `history`, `heads`, `check`, `revision --autogenerate`) stay
#: usable against dev — inspecting the deployed DB is how you diagnose drift in
#: the first place, and blocking that would just push people to raw psql.
MUTATING_ALEMBIC_COMMANDS: frozenset[str] = frozenset({"upgrade", "downgrade", "stamp"})

#: Hosts that mean "a database on this machine". NOTE: a Cloud SQL *auth proxy*
#: listening on localhost also looks local and is therefore not covered — this
#: repo connects via the Cloud SQL Python Connector (see `database/db.py`), so
#: `CLOUDSQL_CONNECTION_NAME` is the signal that matters here.
_LOCAL_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})

# SQLAlchemy drivers can take the effective host from the query string even
# when URL.host is blank or says localhost. A hostless Postgres URL can also
# use a Unix socket, including a Cloud SQL socket at /cloudsql/<instance>.
# Treat these as remote/unknown rather than guessing which socket is local.
_QUERY_ROUTING_KEYS: frozenset[str] = frozenset(
    {
        "host",
        "hostaddr",
        "service",
        "unix_sock",
        "unix_socket",
        "unix_socket_path",
        "socket",
        "cloudsql_connection_name",
        "cloud_sql_instance",
        "instance_connection_name",
    }
)

_TRUTHY = frozenset({"1", "true", "yes", "on"})


class RemoteMigrationBlocked(RuntimeError):
    """Raised when a mutating Alembic command targets a deployed database."""


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def running_in_managed_runtime(env: Mapping[str, str] | None = None) -> bool:
    """True only inside a Cloud Run Job, never a serving revision."""
    e = _env(env)
    return any(e.get(marker) for marker in MANAGED_RUNTIME_MARKERS)


def remote_override_granted(env: Mapping[str, str] | None = None) -> bool:
    """True when the operator explicitly opted into migrating a remote DB."""
    return _env(env).get(REMOTE_OVERRIDE_ENV, "").strip().lower() in _TRUTHY


def describe_remote_target(
    cloudsql_connection_name: str | None,
    database_url: str | None,
) -> str | None:
    """Return a human label for the target DB if it is remote, else ``None``.

    ``CLOUDSQL_CONNECTION_NAME`` wins because ``database/db.py`` routes through
    the Cloud SQL Connector whenever it is set, regardless of ``DATABASE_URL``.
    """
    if cloudsql_connection_name and cloudsql_connection_name.strip():
        instance = cloudsql_connection_name.strip()
        if re.fullmatch(r"[A-Za-z0-9_-]+:[A-Za-z0-9_-]+:[A-Za-z0-9_-]+", instance):
            return f"Cloud SQL instance {instance}"
        return "Cloud SQL instance (name hidden)"

    if not database_url or not database_url.strip():
        return None

    try:
        from sqlalchemy.engine import make_url

        url = make_url(database_url.strip())
    except Exception:  # noqa: BLE001 — an unparseable URL is not our problem
        return None

    if url.drivername.startswith("sqlite"):
        return None
    if any(str(key).lower() in _QUERY_ROUTING_KEYS for key in url.query):
        # Query values may contain a full socket path or credentials. Keep the
        # diagnostic useful without printing any of those values.
        return f"{url.drivername} query-routed target"
    host = (url.host or "").strip().lower()
    if not host:
        return f"{url.drivername} hostless/socket target"
    if host in _LOCAL_HOSTS:
        return None
    return f"{url.drivername} remote host"


def assert_migration_target_allowed(
    *,
    command: str | None,
    cloudsql_connection_name: str | None,
    database_url: str | None,
    env: Mapping[str, str] | None = None,
) -> None:
    """Block mutating Alembic commands aimed at a deployed database.

    ``command`` is the Alembic subcommand name (``"upgrade"``, ``"stamp"``, …).
    ``None`` means the caller could not determine it — treated as mutating, so
    an unrecognised invocation path cannot slip past the guard. Raises
    :class:`RemoteMigrationBlocked` with the exact remediation commands.
    """
    if command is not None and command not in MUTATING_ALEMBIC_COMMANDS:
        return

    target = describe_remote_target(cloudsql_connection_name, database_url)
    if target is None:
        return  # local sqlite / localhost — always fine

    if running_in_managed_runtime(env):
        return  # the dedicated deploy migration Job

    if remote_override_granted(env):
        logger.warning(
            "%s=1 — running `alembic %s` against %s deliberately.",
            REMOTE_OVERRIDE_ENV,
            command or "?",
            target,
        )
        return

    raise RemoteMigrationBlocked(
        "\n"
        + "=" * 78
        + f"\nREFUSING to run `alembic {command or '?'}` against a DEPLOYED database"
        + "\n"
        + "=" * 78
        + f"\n  target: {target}\n"
        "\n  `.env` carries CLOUDSQL_CONNECTION_NAME, so a bare `alembic` run in a\n"
        "  worktree migrates SHARED dev. That is how a dev near-outage happens:\n"
        "  an unmerged branch's revision lands on dev, and every container built\n"
        "  from main then refuses to boot.\n"
        "\n  Run it against a local database instead:\n"
        "    CLOUDSQL_CONNECTION_NAME= DATABASE_URL='sqlite:///./local_dev.db' \\\n"
        "      APP_MODE=local alembic -x workspace=test upgrade head\n"
        "\n  Let the deploy migration Job apply it to dev after review.\n"
        f"\n  If you really must migrate {target} from here, say so:\n"
        f"    {REMOTE_OVERRIDE_ENV}=1 alembic {command or 'upgrade head'}\n" + "=" * 78
    )


# ---------------------------------------------------------------------------
# Guard 2: revision drift between the live database and the running image
# ---------------------------------------------------------------------------

#: Legacy preflight exit codes, used by local `scripts/start.sh` and diagnostics.
#: (which branch on them) and the tests (which assert on them).
#:
#: The deliberate refusal is 4, NOT 1, on purpose: 1 is also what a missing
#: interpreter, an ImportError, or any unhandled crash exits with, and callers
#: must be able to tell "the preflight decided to stop you" from "the preflight
#: could not run". Everything except 3 and 4 means "carry on as before", so a
#: broken preflight degrades to the pre-guard behaviour instead of becoming a
#: new way to fail a boot.
EXIT_RUN_MIGRATIONS = 0
EXIT_SKIP_MIGRATIONS = 3
EXIT_DRIFT_BLOCKED = 4

DRIFT_POLICY_ENV = "MIGRATION_DRIFT_POLICY"

#: Marker env vars `start.sh` exports after the migration step so `/health` can
#: report the outcome without paying a database round trip per probe.
DRIFT_MARKER_ENV = "CUBIE_MIGRATION_DRIFT"
SKIPPED_MARKER_ENV = "CUBIE_MIGRATIONS_SKIPPED"
IMAGE_HEAD_MARKER_ENV = "CUBIE_MIGRATION_IMAGE_HEAD"
DB_REVISION_MARKER_ENV = "CUBIE_MIGRATION_DB_REVISION"

#: Which read produced `db_revision`. The boot preflight necessarily runs BEFORE
#: `alembic upgrade head`, so on any deploy that carries a migration its markers
#: describe the PRE-migration database — and `db_revision` trailing `image_head`
#: is exactly the shape of a *failed* migration. That once cost real
#: investigation time (a container reported a db revision one behind its image
#: head for its whole life while its own logs said "Migrations successful").
#: `start.sh` reads the markers again after a successful upgrade and flips this
#: to `postmigrate`, so a smoke check can tell "stale by construction" from "the
#: migration really did not land".
STAGE_MARKER_ENV = "CUBIE_MIGRATION_STAGE"

DriftStatus = Literal["ok", "fresh", "drift", "unknown"]
DriftPolicy = Literal["skip", "fail"]
BootAction = Literal["run", "skip", "abort"]
MarkerStage = Literal["preflight", "postmigrate"]

_POLICY_ALIASES: dict[str, DriftPolicy] = {
    "skip": "skip",
    "warn": "skip",
    "open": "skip",
    "start": "skip",
    "fail": "fail",
    "abort": "fail",
    "strict": "fail",
}


@dataclass(frozen=True)
class DriftReport:
    """What the live database says vs what this image can actually resolve."""

    status: DriftStatus
    db_revisions: tuple[str, ...] = ()
    image_heads: tuple[str, ...] = ()
    unknown_revisions: tuple[str, ...] = ()
    detail: str = ""

    @property
    def has_drift(self) -> bool:
        return self.status == "drift"


def evaluate_drift(
    db_revisions: Iterable[str],
    known_revisions: Iterable[str],
    image_heads: Iterable[str],
) -> DriftReport:
    """Compare `alembic_version` rows against the revisions this image ships.

    - ``fresh``: no rows — a brand-new database; the normal upgrade path stamps it.
    - ``drift``: at least one row names a revision absent from this image. This
      is the outage shape, and the only status that changes boot behaviour.
    - ``ok``: every row resolves, so `alembic upgrade head` can compute a path
      (including the ordinary "one migration pending" case).
    """
    db = tuple(sorted({r.strip() for r in db_revisions if r and r.strip()}))
    known = {r.strip() for r in known_revisions if r and r.strip()}
    heads = tuple(sorted({h.strip() for h in image_heads if h and h.strip()}))

    if not db:
        return DriftReport(
            status="fresh",
            image_heads=heads,
            detail="alembic_version is empty or absent — treating as a fresh database.",
        )

    unknown = tuple(r for r in db if r not in known)
    if unknown:
        return DriftReport(
            status="drift",
            db_revisions=db,
            image_heads=heads,
            unknown_revisions=unknown,
            detail=(
                "database revision(s) "
                + ", ".join(unknown)
                + " are not present in this image"
            ),
        )

    return DriftReport(
        status="ok",
        db_revisions=db,
        image_heads=heads,
        detail="every database revision resolves in this image",
    )


def resolve_drift_policy(env: Mapping[str, str] | None = None) -> DriftPolicy:
    """Read ``MIGRATION_DRIFT_POLICY``; default ``skip`` (boot anyway).

    ``skip`` is deliberate. Hard-failing on drift converts a recoverable schema problem
    into an unrecoverable one: the container never listens, the revision is unhealthy,
    and traffic cannot be re-routed because routing forces a cold start.
    Unknown/misspelled values also fall back to ``skip`` — an operator typo must not be
    able to arm the behaviour that caused the outage.
    """
    raw = _env(env).get(DRIFT_POLICY_ENV, "").strip().lower()
    if not raw:
        return "skip"
    policy = _POLICY_ALIASES.get(raw)
    if policy is None:
        logger.warning(
            "Unrecognised %s=%r; defaulting to 'skip' (start the app anyway).",
            DRIFT_POLICY_ENV,
            raw,
        )
        return "skip"
    return policy


def decide_boot_action(report: DriftReport, policy: DriftPolicy) -> BootAction:
    """Map a drift report + policy onto what the boot script should do.

    Only ``drift`` is special-cased. ``unknown`` — the preflight could not read
    the database at all, e.g. the transient sqladmin 403 on a cold start — must
    fall through to ``run`` so the existing retry-with-backoff loop in
    ``start.sh`` keeps owning connectivity failures. A diagnostic must never
    become a new way to fail a deploy.
    """
    if report.status != "drift":
        return "run"
    return "abort" if policy == "fail" else "skip"


def boot_exit_code(action: BootAction) -> int:
    return {
        "run": EXIT_RUN_MIGRATIONS,
        "skip": EXIT_SKIP_MIGRATIONS,
        "abort": EXIT_DRIFT_BLOCKED,
    }[action]


def format_marker_env(
    *,
    stage: MarkerStage,
    skipped: bool,
    drift: Iterable[str] = (),
    image_heads: Iterable[str] = (),
    db_revisions: Iterable[str] = (),
) -> str:
    """Render the shell-sourceable marker block `start.sh` exports for `/health`.

    One writer for both stages (boot preflight, post-migration refresh) so the
    two can never disagree about a key name: `/health` reads these by name, and a
    typo on one side would quietly surface `null` instead of a revision.
    """
    return (
        f"{SKIPPED_MARKER_ENV}={'1' if skipped else '0'}\n"
        f"{DRIFT_MARKER_ENV}={','.join(drift)}\n"
        f"{IMAGE_HEAD_MARKER_ENV}={','.join(image_heads)}\n"
        f"{DB_REVISION_MARKER_ENV}={','.join(db_revisions)}\n"
        f"{STAGE_MARKER_ENV}={stage}\n"
    )


def format_drift_diagnostic(
    report: DriftReport,
    *,
    action: BootAction,
    policy: DriftPolicy,
    recent_revisions: Iterable[str] = (),
    git_sha: str | None = None,
    git_branch: str | None = None,
) -> str:
    """The loud, named block that goes to stderr when drift is detected.

    Names the DB revision *and* the image's heads, because "Can't locate
    revision identified by X" alone never said which side was ahead — the whole
    diagnosis had to be reconstructed by hand during the incident.
    """
    bar = "=" * 78
    db = ", ".join(report.db_revisions) or "(none)"
    heads = ", ".join(report.image_heads) or "(none)"
    unknown = ", ".join(report.unknown_revisions) or "(none)"
    recent = ", ".join(recent_revisions)
    build = " ".join(p for p in (git_sha, f"({git_branch})" if git_branch else "") if p)

    if action == "skip":
        taken = (
            "  ACTION: starting the app WITHOUT migrating.\n"
            "    The live schema is a superset of what this image expects, so\n"
            "    additive drift is usually harmless — but any table/column added\n"
            "    by THIS image's unapplied migrations is missing, so code paths\n"
            "    touching them will 500 until the drift is resolved.\n"
            f"    Set {DRIFT_POLICY_ENV}=fail to refuse to start instead.\n"
        )
    else:
        taken = (
            f"  ACTION: refusing to start ({DRIFT_POLICY_ENV}=fail).\n"
            "    WARNING: this is the outage shape. The container will\n"
            "    not listen on :8080, the revision will fail its startup probe,\n"
            "    and `gcloud run services update-traffic` will ALSO fail because\n"
            f"    routing forces a cold start. Set {DRIFT_POLICY_ENV}=skip to boot.\n"
        )

    lines = [
        "",
        bar,
        "ALEMBIC REVISION DRIFT — the database is at a revision this image lacks",
        bar,
        f"  database `alembic_version`: {db}",
        f"  this image's head(s):       {heads}",
        f"  unknown to this image:      {unknown}",
    ]
    if build.strip():
        lines.append(f"  image build:                {build}")
    if recent:
        lines.append(f"  newest revisions in image:  {recent}")
    lines += [
        "",
        "  Most likely cause: a migration that exists only on an unmerged branch",
        "  was applied to this shared database — a `--source .` deploy from a",
        "  worktree, or a local `alembic upgrade head` with .env pointing at",
        "  Cloud SQL. See docs/ENGINEERING_NOTES.md, migration drift.",
        "",
        taken.rstrip("\n"),
        "",
        "  TO FIX: land the missing migration on main and redeploy (preferred),",
        "  or — only if that migration was abandoned — stamp the database back to",
        "  this image's head after confirming the schema matches:",
        f"    alembic stamp {report.image_heads[-1] if report.image_heads else '<head>'}",
        bar,
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pre-deploy: revisions that exist locally but not on origin/main
# ---------------------------------------------------------------------------


def unmerged_revision_files(
    local_files: Iterable[str],
    main_files: Iterable[str],
) -> tuple[str, ...]:
    """Migration files present in the working tree but absent from origin/main.

    Deploying these to a *shared* database is how a dev database broke:
    `--source .` uploads the working tree, so the DB gets
    stamped at a revision no `main` build can resolve. Compared by basename so a
    worktree path prefix does not produce phantom differences.
    """
    import posixpath

    def names(paths: Iterable[str]) -> set[str]:
        return {
            posixpath.basename(p.strip())
            for p in paths
            if p and p.strip() and p.strip().endswith(".py")
        }

    local = names(local_files)
    main = names(main_files)
    return tuple(sorted(local - main - {"__init__.py"}))

#!/bin/bash
set -e

# Unbuffer stdout/stderr
exec 1>&1 2>&2

function ts_echo {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1"
}

# Export the migration markers the preflight wrote (drift, db revision, image
# head, and which read produced them) so /health can report them without a
# database round trip per probe. Called twice on the migrate path: once for the
# pre-upgrade read, again after the upgrade lands.
function source_migration_markers {
    if [ -f "$1" ]; then
        set -a
        # shellcheck disable=SC1090
        . "$1"
        set +a
    fi
}

PREFLIGHT_ENV_FILE="${TMPDIR:-/tmp}/migration_preflight.env"

ts_echo "Starting start.sh script"
ts_echo "Current User: $(whoami)"
ts_echo "PWD: $(pwd)"
ts_echo "PORT: $PORT"
ts_echo "APP_MODE: $APP_MODE"
ts_echo "AUTO_MIGRATE: $AUTO_MIGRATE"
ts_echo "CLOUDSQL_CONNECTION_NAME: $CLOUDSQL_CONNECTION_NAME"

# Run migrations
#
# Three outcomes, tracked explicitly so the log never lies about which one
# happened: "run" (migrate, then serve), "drift" (serve WITHOUT migrating), or
# "disabled" (this role/mode never migrates).
MIGRATION_PLAN=disabled
# Production migrations belong to the deploy Job, even when an old service
# environment still carries AUTO_MIGRATE=true. Local boot must name its target;
# a process/request default is never sufficient authorization for a migration.
if [ "$APP_MODE" = "local" ]; then
    if [ -z "${CUBIE_WORKSPACE:-}" ]; then
        ts_echo "Local migration requires an explicit CUBIE_WORKSPACE; refusing to start."
        exit 1
    fi
    MIGRATION_PLAN=run
    # PREFLIGHT: if the database is stamped at a revision
    # this image doesn't ship, `alembic upgrade head` can't compute a path and
    # dies with "Can't locate revision identified by X". The old code retried 5x
    # and exit(1)'d, so nothing ever listened on :8080 — the revision failed its
    # startup probe AND traffic couldn't be re-routed (routing forces a cold
    # start), which is how one branch's migration can take dev down for good.
    #
    # The preflight decides deliberately instead of dying:
    #   3 = drift -> log a loud diagnostic, SKIP migrations, still serve traffic
    #   4 = drift + MIGRATION_DRIFT_POLICY=fail -> refuse to start (old behaviour)
    # ANY other code (0, or a crash/ImportError/missing interpreter) means
    # "migrate as usual" — a broken preflight must not be a new boot failure.
    rm -f "$PREFLIGHT_ENV_FILE"   # never source a previous boot's verdict
    set +e
    python -m rubik_core.database.migration_preflight \
        --mode boot --emit-env "$PREFLIGHT_ENV_FILE"
    PREFLIGHT_RC=$?
    set -e
    source_migration_markers "$PREFLIGHT_ENV_FILE"

    if [ "$PREFLIGHT_RC" = "3" ]; then
        MIGRATION_PLAN=drift
    elif [ "$PREFLIGHT_RC" = "4" ]; then
        ts_echo "Revision drift + MIGRATION_DRIFT_POLICY=fail — refusing to start (see diagnostic above)."
        exit 1
    fi
fi

if [ "$MIGRATION_PLAN" = "run" ]; then
    ts_echo "Running Database Migrations..."
    # The Cloud SQL connector's first sqladmin call can transiently 403 at cold
    # start (before the metadata-server ADC token is usable), which would sink
    # the whole boot and fail an AUTO_MIGRATE deploy. Retry a few times with
    # backoff so a flaky first connect doesn't take the revision down; a genuine
    # config/permission error still fails after the retries are exhausted.
    migrate_ok=false
    for attempt in 1 2 3 4 5; do
        if alembic -x "workspace=$CUBIE_WORKSPACE" upgrade head; then
            migrate_ok=true
            ts_echo "Migrations successful (attempt ${attempt})."
            break
        fi
        ts_echo "Migration attempt ${attempt} failed; retrying in 5s..."
        sleep 5
    done
    if [ "$migrate_ok" != "true" ]; then
        ts_echo "Migrations FAILED after 5 attempts."
        exit 1
    fi
    # REFRESH THE /health MARKERS. The preflight read
    # `alembic_version` BEFORE this upgrade, so CUBIE_MIGRATION_DB_REVISION still
    # names the pre-migration revision — and /health showing db_revision behind
    # image_head is exactly what a FAILED migration looks like, so the smoke
    # check once read as broken on a deploy that had succeeded. Re-read now, and
    # never let it be fatal: the schema is already migrated at this point, so a
    # bad diagnostic must not cost us the boot.
    set +e
    python -m rubik_core.database.migration_preflight \
        --mode postmigrate --emit-env "$PREFLIGHT_ENV_FILE"
    set -e
    source_migration_markers "$PREFLIGHT_ENV_FILE"
    ts_echo "Post-migration revision: db=${CUBIE_MIGRATION_DB_REVISION:-?} image_head=${CUBIE_MIGRATION_IMAGE_HEAD:-?}"
    # A fresh workspace's default rows (the default AI Priorities playbook,
    # Drive labels, SLA placeholders, the starter skills list). Idempotent: it
    # fills only empty slots, so every boot may run it and an admin's edits
    # are never overwritten. Never fatal: the schema is migrated by now, and a
    # workspace without its defaults still serves.
    ts_echo "Seeding workspace defaults..."
    if python -m rubik_core.database.seed; then
        ts_echo "Workspace defaults in place."
    else
        ts_echo "Seeding workspace defaults FAILED; starting anyway (re-run: python -m rubik_core.database.seed)."
    fi
elif [ "$MIGRATION_PLAN" = "drift" ]; then
    # Deliberate: serving a superset schema beats a dead, un-rollbackable
    # revision. /health reports the drift (CUBIE_MIGRATION_DRIFT).
    ts_echo "Revision drift — starting the app WITHOUT migrating (drift: ${CUBIE_MIGRATION_DRIFT:-?})."
else
    ts_echo "Skipping automatic migrations and seed outside local mode; deploy migration Job owns schema changes."
fi

# Start Backend
ts_echo "Starting Uvicorn..."
LOWER_LOG_LEVEL=$(echo ${LOG_LEVEL:-info} | tr '[:upper:]' '[:lower:]')
ts_echo "Log Level: $LOWER_LOG_LEVEL"

exec uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8080} --log-level $LOWER_LOG_LEVEL

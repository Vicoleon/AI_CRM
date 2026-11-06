#!/usr/bin/env bash
# Build two throwaway Postgres databases from two versions of the Alembic
# chain and diff their schemas.
#
#   scripts/dev/schema_diff.sh [--drop-removed] <rev-a> <rev-b>
#
# <rev> is any git revision, or WORKTREE for the files on disk. Each side's
# alembic/, alembic.ini and rubik_core/ are exported to their own folder,
# `alembic upgrade head` builds a fresh database from them, and the
# `pg_dump --schema-only` output is normalised before the diff: comments and
# SET lines go, the lines inside each CREATE TABLE are sorted, and so are the
# statements. What is left is a real difference, not column order.
#
#   --drop-removed  first drop the tables A has and B does not (the tables of
#                   deleted modules), listing each one.
#
# Server. By default the script starts its own: `initdb` + `pg_ctl` in
# $SCRATCH/pg on port $PGPORT (55432), stopped and removed on exit.
# SCHEMA_DIFF_SERVER=host:port uses a Postgres that is already running instead
# (for a sandbox where initdb cannot get shared memory); the two databases it
# creates there are dropped on exit.
#
# Environment: SCRATCH (default: a new temporary folder; the dumps are kept in
# $SCRATCH/pg), PGPORT (55432), PG_BIN (/usr/local/bin), PGUSER (postgres for
# the private server, $USER for a running one), PYTHON (.venv/bin/python).
#
# Exit status: 0 when the schemas match, 1 when they differ, 2 on a usage or
# build error.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PG_BIN="${PG_BIN:-/usr/local/bin}"
PYTHON="${PYTHON:-$REPO/.venv/bin/python}"
PGPORT="${PGPORT:-55432}"

usage() {
  sed -n '2,29p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

DROP_REMOVED=0
if [[ "${1:-}" == "--drop-removed" ]]; then
  DROP_REMOVED=1
  shift
fi
[[ $# -eq 2 ]] || usage
REV_A="$1"
REV_B="$2"

SCRATCH="${SCRATCH:-$(mktemp -d "${TMPDIR:-/tmp}/cubie-schema-diff.XXXXXX")}"
WORK="$SCRATCH/pg"
mkdir -p "$WORK"

DB_A="cubie_schema_a_$$"
DB_B="cubie_schema_b_$$"
OWN_SERVER=0

if [[ -n "${SCHEMA_DIFF_SERVER:-}" ]]; then
  PGHOST="${SCHEMA_DIFF_SERVER%:*}"
  PGPORT="${SCHEMA_DIFF_SERVER##*:}"
  PGUSER="${PGUSER:-$USER}"
else
  PGHOST="127.0.0.1"
  PGUSER="${PGUSER:-postgres}"
  OWN_SERVER=1
fi
export PGHOST PGPORT PGUSER

die() {
  echo "schema_diff: $*" >&2
  exit 2
}

cleanup() {
  if [[ "$OWN_SERVER" == 1 ]]; then
    "$PG_BIN/pg_ctl" -D "$WORK/data" -m fast -w stop >/dev/null 2>&1 || true
    rm -rf "$WORK/data"
  else
    "$PG_BIN/dropdb" --if-exists "$DB_A" >/dev/null 2>&1 || true
    "$PG_BIN/dropdb" --if-exists "$DB_B" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

start_private_server() {
  if "$PG_BIN/pg_isready" -q -h "$PGHOST" -p "$PGPORT"; then
    die "something already listens on port $PGPORT; set PGPORT or SCHEMA_DIFF_SERVER"
  fi
  rm -rf "$WORK/data"
  "$PG_BIN/initdb" -D "$WORK/data" -U "$PGUSER" -A trust --no-locale -E UTF8 \
    >"$WORK/initdb.log" 2>&1 || die "initdb failed, see $WORK/initdb.log"
  # TCP only: a Unix socket under a deep $SCRATCH overflows the path limit.
  "$PG_BIN/pg_ctl" -D "$WORK/data" -l "$WORK/server.log" -w \
    -o "-p $PGPORT -k '' -c listen_addresses=$PGHOST -c fsync=off" start \
    >/dev/null || die "pg_ctl start failed, see $WORK/server.log"
}

# Export one side's migration chain and run `alembic upgrade head` into $db.
build() {
  local label="$1" rev="$2" db="$3" src
  if [[ "$rev" == "WORKTREE" ]]; then
    src="$REPO"
  else
    src="$WORK/src-$label"
    rm -rf "$src"
    mkdir -p "$src"
    git -C "$REPO" archive "$rev" alembic alembic.ini rubik_core | tar -x -C "$src" \
      || die "cannot export $rev"
  fi
  "$PG_BIN/createdb" "$db" || die "createdb $db failed"
  if ! (cd "$src" && env \
    APP_MODE=local \
    DATABASE_URL="postgresql+pg8000://$PGUSER@$PGHOST:$PGPORT/$db" \
    CLOUDSQL_CONNECTION_NAME="" DB_USER="" GCP_PROJECT_ID=mock-project \
    PYTHONPATH="$src" \
    "$PYTHON" -m alembic -c "$src/alembic.ini" upgrade head) >"$WORK/build-$label.log" 2>&1; then
    tail -20 "$WORK/build-$label.log" >&2
    die "alembic upgrade head failed for $label ($rev), see $WORK/build-$label.log"
  fi
}

tables() {
  "$PG_BIN/psql" -d "$1" -Atc \
    "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1"
}

dump() {
  "$PG_BIN/pg_dump" --schema-only --no-owner --no-privileges -d "$1" >"$WORK/$2.raw.sql"
  "$PYTHON" - "$WORK/$2.raw.sql" "$WORK/$2.sql" <<'PY'
import re
import sys

raw, out = sys.argv[1], sys.argv[2]
lines = [
    line.rstrip()
    for line in open(raw, encoding="utf-8")
    if line.strip()
    and not line.startswith(("--", "SET ", "\\"))
    and not line.startswith("SELECT pg_catalog.set_config")
]
statements, current = [], []
for line in lines:
    current.append(line)
    if line.endswith(";"):
        statements.append(current)
        current = []
if current:
    statements.append(current)


def normalise(statement):
    head = statement[0]
    if head.startswith("CREATE TABLE") and len(statement) > 2:
        body = sorted(line.strip().rstrip(",") for line in statement[1:-1])
        return "\n".join([head, *("    " + line for line in body), statement[-1]])
    return "\n".join(statement)


kept = [
    normalise(s)
    for s in statements
    if not re.search(r"\balembic_version\b", " ".join(s))
]
with open(out, "w", encoding="utf-8") as fh:
    fh.write("\n\n".join(sorted(kept)) + "\n")
PY
}

if [[ "$OWN_SERVER" == 1 ]]; then
  start_private_server
else
  "$PG_BIN/pg_isready" -q -h "$PGHOST" -p "$PGPORT" || die "no Postgres at $PGHOST:$PGPORT"
fi

echo "== building A from $REV_A" >&2
build a "$REV_A" "$DB_A"
echo "== building B from $REV_B" >&2
build b "$REV_B" "$DB_B"

tables "$DB_A" >"$WORK/tables-a.txt"
tables "$DB_B" >"$WORK/tables-b.txt"
ONLY_A="$(comm -23 "$WORK/tables-a.txt" "$WORK/tables-b.txt")"
ONLY_B="$(comm -13 "$WORK/tables-a.txt" "$WORK/tables-b.txt")"
echo "== tables: A $(wc -l <"$WORK/tables-a.txt" | tr -d ' '), B $(wc -l <"$WORK/tables-b.txt" | tr -d ' ')"
echo "== only in A: ${ONLY_A:-(none)}" | tr '\n' ' '
echo
echo "== only in B: ${ONLY_B:-(none)}" | tr '\n' ' '
echo

if [[ "$DROP_REMOVED" == 1 && -n "$ONLY_A" ]]; then
  while IFS= read -r table; do
    echo "== dropping from A: $table"
    "$PG_BIN/psql" -q -d "$DB_A" -c "DROP TABLE IF EXISTS \"$table\" CASCADE" >/dev/null
  done <<<"$ONLY_A"
fi

dump "$DB_A" a
dump "$DB_B" b

echo "== diff of normalised schemas ($WORK/a.sql vs $WORK/b.sql)"
if diff -u "$WORK/a.sql" "$WORK/b.sql"; then
  echo "== no differences"
  exit 0
fi
exit 1

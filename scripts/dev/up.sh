#!/usr/bin/env bash
# Create two separate local Cubie workspaces, then run the API and Vite.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
DATA_DIR="${CUBIE_DEV_DATA_DIR:-$ROOT/data/dev-phase2}"
SETUP_ONLY=0
RESET=0
for arg in "$@"; do
  case "$arg" in
    --setup-only) SETUP_ONLY=1 ;;
    --reset) RESET=1 ;;
    *) printf 'Unknown option: %s\n' "$arg" >&2; exit 2 ;;
  esac
done

if [ "$RESET" -eq 1 ]; then
  if [ -n "${CUBIE_DEV_DATA_DIR:-}" ]; then
    printf '%s\n' '--reset only applies to the default local data directory' >&2
    exit 2
  fi
  rm -rf -- "$ROOT/data/dev-phase2"
fi
mkdir -p "$DATA_DIR"
DATA_DIR="$(cd "$DATA_DIR" && pwd -P)"
PYTHON="$ROOT/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  PYTHON="$(command -v python3 || true)"
  if [ -z "$PYTHON" ]; then
    printf '%s\n' 'Install Python backend dependencies first (see README).' >&2
    exit 2
  fi
fi

export PYTHONPATH="$ROOT"
export APP_MODE=dev_mock_auth
export ENABLE_MOCK_AUTH=true
export CUBIE_AUTOMATIONS_ENABLED="${CUBIE_AUTOMATIONS_ENABLED:-false}"
export CUBIE_DEV_USER_EMAIL="${CUBIE_DEV_USER_EMAIL:-dev@acme.example}"
export FRONTEND_URL="http://127.0.0.1:${CUBIE_DEV_WEB_PORT:-5173}"
export VITE_API_TARGET="http://127.0.0.1:${CUBIE_DEV_API_PORT:-8001}"
export VITE_DEV_LOCAL_PREVIEW=true
export LOCAL_DATA_DIR="$DATA_DIR"
export WORKSPACE_DATABASE_DIR="$DATA_DIR/workspace-dbs"
export CONTROL_DATABASE_URL="sqlite:///$DATA_DIR/control.db"
export DATABASE_URL="sqlite:///$DATA_DIR/legacy-unused.db"

cd "$ROOT"
"$PYTHON" - <<'PY'
from rubik_core.platform.executor import LocalExecutor
from rubik_core.platform.migrate import migrate_control, migrate_workspace
from rubik_core.platform.provision import CreateSpec, Provisioner

migrate_control()
provisioner = Provisioner(executor=LocalExecutor())
for spec in (
    CreateSpec(
        slug="rubiksoft", name="RubikSoft", domains=("rubik-soft.com",),
        first_admins=("dev@rubik-soft.com",),
        dwd_subject="dev@rubik-soft.com", time_zone="America/Costa_Rica",
    ),
    CreateSpec(
        slug="acme", name="Acme Demo", domains=("acme.example",),
        first_admins=("dev@acme.example",),
        dwd_subject="dev@acme.example", time_zone="America/Chicago",
    ),
):
    result = provisioner.create(spec)
    print(f"{result.slug}: {result.status}")
    # Provisioning records its migration step once. Re-running an older local
    # preview must still upgrade that workspace to this image's current head.
    migrate_workspace(spec.slug)
PY

if [ "$SETUP_ONLY" -eq 1 ]; then
  printf 'Local workspace data: %s\n' "$DATA_DIR"
  exit 0
fi

if [ ! -d "$ROOT/frontend/node_modules" ]; then
  printf '%s\n' 'Install frontend dependencies with npm --prefix frontend ci first.' >&2
  exit 2
fi

"$PYTHON" -m uvicorn backend.main:app --host 127.0.0.1 --port "${CUBIE_DEV_API_PORT:-8001}" &
API_PID=$!
trap 'kill "$API_PID" 2>/dev/null || true' EXIT INT TERM
printf '\nLocal preview: http://127.0.0.1:%s/login\n' "${CUBIE_DEV_WEB_PORT:-5173}"
printf 'Default: %s | alternate: dev@rubik-soft.com (X-Mock-User)\n' "$CUBIE_DEV_USER_EMAIL"
VITE_GOOGLE_CLIENT_ID= npm --prefix frontend run dev -- --host 127.0.0.1 --port "${CUBIE_DEV_WEB_PORT:-5173}" --strictPort

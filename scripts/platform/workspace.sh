#!/usr/bin/env bash
# Explicit environment and target for every platform workspace operation.
set -euo pipefail

if [[ "${1:-}" != "--env" || -z "${2:-}" ]]; then
  echo "Usage: workspace.sh --env <file> workspace create|list|migrate|delete ..." >&2
  echo "       workspace.sh --env <file> platform-admin add|list|remove ..." >&2
  exit 2
fi

ENV_FILE="$2"
shift 2
if [[ ! -f "$ENV_FILE" || ! -r "$ENV_FILE" ]]; then
  echo "Environment file is not readable" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# The explicit file is operator-owned. Never trace its values or echo them.
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi
cd "$ROOT_DIR"
exec "$PYTHON_BIN" -m rubik_core.platform.cli "$@"

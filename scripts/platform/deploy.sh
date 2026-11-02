#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE=""
DRY_RUN=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --env) ENV_FILE="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) echo "Usage: scripts/platform/deploy.sh --env <file> [--dry-run]" >&2; exit 2 ;;
  esac
done
[ -n "$ENV_FILE" ] && [ -f "$ENV_FILE" ] || { echo "An existing --env file is required" >&2; exit 2; }
set -a
# This is an operator-owned shell environment file, never a browser upload.
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
cd "$ROOT"
if [ "$DRY_RUN" -eq 1 ]; then
  exec "${CUBIE_PLATFORM_PYTHON:-python3}" -m rubik_core.platform.deployment deploy --dry-run
fi
exec "${CUBIE_PLATFORM_PYTHON:-python3}" -m rubik_core.platform.deployment deploy

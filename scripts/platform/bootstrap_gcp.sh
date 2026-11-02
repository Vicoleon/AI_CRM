#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE=""
DRY_RUN=0
FRESH=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --env) ENV_FILE="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --fresh-install) FRESH=1; shift ;;
    *) echo "Usage: scripts/platform/bootstrap_gcp.sh --env <file> [--dry-run] [--fresh-install]" >&2; exit 2 ;;
  esac
done
[ -n "$ENV_FILE" ] && [ -f "$ENV_FILE" ] || { echo "An existing --env file is required" >&2; exit 2; }
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
cd "$ROOT"
args=(bootstrap)
[ "$DRY_RUN" -eq 0 ] || args+=(--dry-run)
[ "$FRESH" -eq 0 ] || args+=(--fresh-install)
exec "${CUBIE_PLATFORM_PYTHON:-python3}" -m rubik_core.platform.deployment "${args[@]}"

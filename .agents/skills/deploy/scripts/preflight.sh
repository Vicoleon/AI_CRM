#!/usr/bin/env bash
# Read-only Cubie Phase 2 release preflight.
set -euo pipefail
if [ "${1:-}" != "--env" ] || [ -z "${2:-}" ] || [ "$#" -ne 2 ]; then
  echo 'Usage: preflight.sh --env <private-env>' >&2
  exit 2
fi
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
cd "$ROOT"
exec bash scripts/platform/deploy.sh --env "$2" --dry-run

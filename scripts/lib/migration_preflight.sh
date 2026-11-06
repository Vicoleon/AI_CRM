#!/usr/bin/env bash
# Read-only control and revision preflight. The platform deploy controller runs
# the same checks itself; this function is for operator diagnostics.
run_migration_preflight() {
  "${CUBIE_PLATFORM_PYTHON:-python3}" -m rubik_core.platform.deployment preflight
}

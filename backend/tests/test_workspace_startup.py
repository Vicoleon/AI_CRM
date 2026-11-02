"""Exercise startup decisions with executable stubs, never a database."""

import os
import subprocess
from pathlib import Path

import pytest

START_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "start.sh"


@pytest.fixture
def boot(tmp_path):
    commands = tmp_path / "commands"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = """#!/bin/bash
printf '%s\\n' "$(basename "$0") $*" >> "$COMMANDS"
if [ "$(basename "$0")" = python ]; then
    if [[ "$*" == *"--mode boot"* ]]; then
        printf 'CUBIE_MIGRATION_DRIFT=unknown_revision\\nCUBIE_MIGRATION_STAGE=preflight\\n' > "$TMPDIR/migration_preflight.env"
        exit "${PREFLIGHT_RC:-0}"
    fi
fi
if [ "$(basename "$0")" = uvicorn ]; then
    printf 'markers:%s:%s\\n' "${CUBIE_MIGRATION_DRIFT:-}" "${CUBIE_MIGRATION_STAGE:-}" >> "$COMMANDS"
fi
"""
    for name in ("python", "alembic", "uvicorn", "sleep"):
        executable = bin_dir / name
        executable.write_text(stub)
        executable.chmod(0o755)

    def run(**overrides):
        env = {
            **os.environ,
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "COMMANDS": str(commands),
            "TMPDIR": str(tmp_path),
            "APP_MODE": "local",
            "AUTO_MIGRATE": "false",
            "CUBIE_WORKSPACE": "",
            **overrides,
        }
        result = subprocess.run(
            ["/bin/bash", str(START_SCRIPT)], env=env, text=True, capture_output=True
        )
        return result, commands.read_text() if commands.exists() else ""

    return run


def test_production_never_migrates_or_seeds_even_when_enabled(boot):
    result, commands = boot(APP_MODE="GCP", AUTO_MIGRATE="true", CUBIE_WORKSPACE="acme")
    assert result.returncode == 0
    assert "uvicorn backend.main:app" in commands
    assert "python " not in commands
    assert "alembic " not in commands


def test_local_boot_requires_explicit_workspace_before_db_commands(boot):
    result, commands = boot()
    assert result.returncode != 0
    assert "CUBIE_WORKSPACE" in result.stdout
    assert commands == ""


def test_local_boot_passes_explicit_workspace_to_alembic(boot):
    result, commands = boot(CUBIE_WORKSPACE="acme")
    assert result.returncode == 0
    assert "alembic -x workspace=acme upgrade head" in commands
    assert "--mode boot" in commands
    assert "--mode postmigrate" in commands
    assert "python -m rubik_core.database.seed" in commands


@pytest.mark.parametrize("code, serves", [(3, True), (4, False)])
def test_local_drift_keeps_marker_and_policy(boot, code, serves):
    result, commands = boot(CUBIE_WORKSPACE="acme", PREFLIGHT_RC=str(code))
    assert (result.returncode == 0) is serves
    assert "alembic " not in commands
    assert "database.seed" not in commands
    assert ("uvicorn " in commands) is serves
    if serves:
        assert "markers:unknown_revision:preflight" in commands

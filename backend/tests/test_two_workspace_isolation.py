"""A fresh local preview provisions two independent workspace databases."""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def local_pair(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("cubie-local-pair")
    env = {
        **os.environ,
        "CUBIE_DEV_DATA_DIR": str(data_dir),
        "PYTHONPATH": str(ROOT),
        "APP_MODE": "dev_mock_auth",
        "ENABLE_MOCK_AUTH": "true",
        "CUBIE_DEV_USER_EMAIL": "dev@acme.example",
        "LOCAL_DATA_DIR": str(data_dir),
        "WORKSPACE_DATABASE_DIR": str(data_dir / "workspace-dbs"),
        "CONTROL_DATABASE_URL": f"sqlite:///{data_dir / 'control.db'}",
        "DATABASE_URL": f"sqlite:///{data_dir / 'legacy-unused.db'}",
    }
    command = [str(ROOT / "scripts/dev/up.sh"), "--setup-only"]
    for _ in range(2):
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr[-2000:]
        assert "rubiksoft: complete" in result.stdout
        assert "acme: complete" in result.stdout
    return data_dir, env


def _fetch(db_path: Path, statement: str, parameters=()):
    with sqlite3.connect(db_path) as connection:
        return connection.execute(statement, parameters).fetchall()


def test_setup_only_is_idempotent(local_pair):
    data_dir, _ = local_pair
    control = data_dir / "control.db"
    assert _fetch(control, "SELECT slug FROM workspaces ORDER BY slug") == [
        ("acme",),
        ("rubiksoft",),
    ]
    assert _fetch(control, "SELECT count(*) FROM workspace_domains") == [(2,)]
    for slug, email in (
        ("acme", "dev@acme.example"),
        ("rubiksoft", "dev@rubik-soft.com"),
    ):
        database = data_dir / "workspace-dbs" / f"{slug}.db"
        assert database.is_file()
        assert _fetch(database, "SELECT email FROM users") == [(email,)]
        assert _fetch(database, "SELECT count(*) FROM priority_playbook") == [(1,)]


def test_setup_only_upgrades_existing_workspace_head(local_pair, tmp_path):
    data_dir, original_env = local_pair
    clone = tmp_path / "previous-local-preview"
    shutil.copytree(data_dir, clone)
    old_workspace = clone / "workspace-dbs/rubiksoft.db"
    with sqlite3.connect(old_workspace) as connection:
        connection.execute(
            "ALTER TABLE cloud_vector_updates DROP COLUMN managed_applied"
        )
        connection.execute(
            "UPDATE alembic_version SET version_num = '0009_cloud_vector_events'"
        )

    env = {
        **original_env,
        "CUBIE_DEV_DATA_DIR": str(clone),
        "LOCAL_DATA_DIR": str(clone),
        "WORKSPACE_DATABASE_DIR": str(clone / "workspace-dbs"),
        "CONTROL_DATABASE_URL": f"sqlite:///{clone / 'control.db'}",
        "DATABASE_URL": f"sqlite:///{clone / 'legacy-unused.db'}",
    }
    result = subprocess.run(
        [str(ROOT / "scripts/dev/up.sh"), "--setup-only"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert _fetch(old_workspace, "SELECT version_num FROM alembic_version") == [
        ("0010_cloud_vector_managed",)
    ]
    assert "managed_applied" in {
        row[1]
        for row in _fetch(old_workspace, "PRAGMA table_info(cloud_vector_updates)")
    }


def test_mock_email_domain_selects_only_its_workspace(local_pair):
    _, env = local_pair
    script = """
from fastapi.testclient import TestClient
from backend.main import app

client = TestClient(app)
for email, expected in (
    ('dev@acme.example', 'acme'),
    ('dev@rubik-soft.com', 'rubiksoft'),
):
    response = client.get('/api/users/me', headers={'Authorization': 'Bearer ' + email})
    assert response.status_code == 200, response.text
    assert response.json()['workspace']['slug'] == expected
    assert response.json()['email'] == email
response = client.get('/api/users/me', headers={'Authorization': 'Bearer dev@unknown.example'})
assert response.status_code == 403, response.text
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr[-2000:]


def test_identical_row_ids_cannot_cross_workspace_db(local_pair):
    data_dir, _ = local_pair
    acme = data_dir / "workspace-dbs/acme.db"
    rubiksoft = data_dir / "workspace-dbs/rubiksoft.db"
    for database, name in ((acme, "Acme private"), (rubiksoft, "RubikSoft private")):
        with sqlite3.connect(database) as connection:
            connection.execute(
                "INSERT INTO users (email, id, name, is_exec, is_platform_support) "
                "VALUES (?, ?, ?, 0, 0)",
                ("same@example.invalid", "identical-id", name),
            )
    assert _fetch(acme, "SELECT name FROM users WHERE id='identical-id'") == [
        ("Acme private",),
    ]
    assert _fetch(rubiksoft, "SELECT name FROM users WHERE id='identical-id'") == [
        ("RubikSoft private",),
    ]

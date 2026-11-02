"""Under pytest-xdist, each worker runs on its own copy of the SQLite test DB.

The repo-root ``conftest.py`` points every worker at ``test_<worker>.db`` before
any engine exists, so a test that drops and re-creates tables in one worker
cannot race another worker's tests. A serial run keeps the one file.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url


def test_each_worker_has_its_own_sqlite_file():
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if not worker:
        pytest.skip("a serial run owns the one database file")
    url = make_url(os.environ["DATABASE_URL"])
    if url.get_backend_name() != "sqlite" or url.database in (None, "", ":memory:"):
        pytest.skip("only a file-backed SQLite database is copied per worker")

    database = Path(url.database)

    assert database.stem.endswith(f"_{worker}"), database
    assert database.exists(), database

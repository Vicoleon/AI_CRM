"""Session-wide test isolation from the live database.

``db.get_engine()`` routes on ``CLOUDSQL_CONNECTION_NAME`` *before* it looks at
``DATABASE_URL``. A per-module fixture that only sets ``DATABASE_URL`` to a
throwaway sqlite file therefore does NOT isolate anything: with a developer
``.env`` present the engine still resolves to Cloud SQL, and every fixture user
the module registers lands in the real ``users`` / ``user_roles`` tables. Dev IS
our production database, so that is a production write.

That is not hypothetical. It is how ``admin@example.com`` came to hold the
``admin`` role — a hard bypass for every access check in the app — along with
``exec@``, ``regular@``, ``new-exec@``, ``guest@``, ``rep1@`` and
``rep2@example.com``, all written by one local test run.

It is also how ``test_eos_exec_and_checkin.py``'s
``L10Meeting(name="Pulse", duration_minutes=90, created_by="admin@example.com")``
fixtures reached end users: no department, no roster, no exec flag, which
``_authz._meeting_accessible`` treats as a company-wide meeting, so every one of
them rendered on EVERY user's EOS Meetings page — each with a never-ended
``L10Session`` showing a live green "Rejoin" button.

Some suites blank the variable themselves, but that only protects the modules
whose author remembered — most modules that set ``settings.DATABASE_URL`` do not
also blank ``CLOUDSQL_CONNECTION_NAME``. Blanking it here makes isolation the
default for the whole session — a module can still opt in to Cloud SQL routing
deliberately, because a local ``monkeypatch``/assignment inside a test still
wins and is reverted after.

``pytest_configure`` runs before collection, so this lands ahead of the
module-level ``TestClient(app)`` constructions that would otherwise build an
engine at import time.

Prevention alone isn't enough, though: a fixture that re-points the engine
*after* configure would sail straight past it. So the autouse fixture below
asserts the live engine is still SQLite around every test. That assertion is the
load-bearing part — it fails loud instead of writing to production.

**Every test leaves ``DATABASE_URL`` as it found it.** Many module fixtures
assign ``settings.DATABASE_URL`` to a ``tmp_path`` file and never put it back.
Harmless while the cached engine stays put, but the next fixture that drops the
engine (``dbmod._engine = None``) rebuilds it on that stale URL, and a fixture
that created its tables on the cached engine instead leaves the file empty.
Feature Access then cannot read ``feature_access`` and, failing closed, shuts
the copilot on whichever unrelated test runs next in that worker. The autouse
fixture below restores the URL after every test.

**One SQLite file per xdist worker.** Under ``pytest -n N`` every worker is its
own process, and they all used to open the same ``./test.db``: a test that
``drop_all``/``create_all``s it in one worker raced the others ("attempt to
write a readonly database", "unable to open database file", or a count that
included another worker's rows). So each worker copies the alembic-built file
to ``test_<worker>.db`` next to it and points ``DATABASE_URL`` there, before any
engine exists. A serial run, an in-memory URL and a non-SQLite URL are left
alone.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

#: SQLite's side files, which belong to (and go stale with) the database file.
_SQLITE_SIDE_FILES = ("-journal", "-wal", "-shm")

#: This xdist worker's private copy of the test database, once it has one.
_WORKER_DATABASE: Path | None = None

#: A private control file per pytest process, including every xdist worker.
_CONTROL_DATABASE_DIRECTORY: Path | None = None


def _worker_database_path(url: str, worker: str) -> Path | None:
    """The file this xdist worker should use instead of ``url``'s, or None.

    None for anything that is not a file-backed SQLite URL: an in-memory
    database is already private to the process, and any other database is not
    ours to copy.
    """
    from sqlalchemy.engine import make_url

    try:
        parsed = make_url(url)
    except Exception:  # noqa: BLE001 — not a URL we understand: leave it alone
        return None
    database = parsed.database or ""
    if parsed.get_backend_name() != "sqlite" or database in ("", ":memory:"):
        return None
    if database.startswith("file:"):
        return None  # a URI filename; too unusual to rewrite safely
    source = Path(database)
    return source.with_name(f"{source.stem}_{worker}{source.suffix}")


def _remove_sqlite_file(path: Path) -> None:
    for suffix in ("", *_SQLITE_SIDE_FILES):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


def _give_this_worker_its_own_database(settings) -> None:
    """Point this xdist worker at a private copy of the SQLite test database."""
    global _WORKER_DATABASE
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if not worker:
        return  # not under xdist: the one process owns the file
    from sqlalchemy.engine import make_url

    url = settings.DATABASE_URL or ""
    target = _worker_database_path(url, worker)
    if target is None:
        return
    source = Path(make_url(url).database)
    _remove_sqlite_file(target)
    if source.exists():
        shutil.copyfile(source, target)  # the alembic-built schema, per worker
    worker_url = make_url(url).set(database=str(target))
    settings.DATABASE_URL = worker_url.render_as_string(hide_password=False)
    os.environ["DATABASE_URL"] = settings.DATABASE_URL
    _WORKER_DATABASE = target


def pytest_configure(config):  # noqa: ARG001 — pytest hook signature
    # Set the environment too, so anything that re-instantiates Settings or
    # spawns a subprocess inherits the safe values.
    os.environ["CLOUDSQL_CONNECTION_NAME"] = ""
    os.environ["APP_MODE"] = "local"
    # Must precede Settings import: a developer env may point control at prod.
    global _CONTROL_DATABASE_DIRECTORY
    _CONTROL_DATABASE_DIRECTORY = Path(tempfile.mkdtemp(prefix="cubie-control-pytest-"))
    control_url = f"sqlite:///{_CONTROL_DATABASE_DIRECTORY / 'control.db'}"
    os.environ["CONTROL_DATABASE_URL"] = control_url

    from rubik_core.config import AppMode, settings

    settings.CLOUDSQL_CONNECTION_NAME = ""
    settings.APP_MODE = AppMode.LOCAL
    settings.CONTROL_DATABASE_URL = control_url

    # Never let a forgetful module fall back to the checked-in dam_data/dam.db;
    # give it a scratch file instead. Modules that set their own tmp_path sqlite
    # URL keep doing so — this is only the floor.
    if "sqlite" not in (settings.DATABASE_URL or ""):
        settings.DATABASE_URL = (
            f"sqlite:///{Path(tempfile.gettempdir()) / 'rubik_core_pytest_fallback.db'}"
        )
    # After the floor, so the scratch file is per worker too.
    _give_this_worker_its_own_database(settings)

    # Drop any engine built during import so it is rebuilt from the values above.
    import rubik_core.database.db as dbmod

    dbmod._engine = None
    from rubik_core.control import db as control_db

    control_db._engine = None
    # Test requests use an explicit workspace context. Give its control-side
    # identity real rows rather than teaching production resolvers a pytest
    # exception. Every xdist worker gets its own scratch control database.
    from rubik_core.control.models import (
        Base as ControlBase,
    )
    from rubik_core.control.models import (
        Workspace,
        WorkspaceDomain,
        WorkspaceModule,
    )

    control_engine = control_db.get_engine()
    ControlBase.metadata.create_all(control_engine)
    with control_db.control_session() as control:
        control.add(
            Workspace(
                slug="test",
                name="Test Workspace",
                status="active",
                time_zone="America/Chicago",
            )
        )
        control.flush()
        control.add_all(
            [
                WorkspaceDomain(
                    workspace_slug="test", domain="acme.example", is_primary=True
                ),
                WorkspaceModule(workspace_slug="test", module="eos", enabled=True),
            ]
        )
        control.commit()
    from rubik_core.tenancy import bootstrap_process_workspace, registry

    registry.clear_engines()
    bootstrap_process_workspace("test")
    ok, described = _databases_are_sqlite()
    assert ok, f"Unsafe database configuration before collection: {described}"


def pytest_unconfigure(config):  # noqa: ARG001 — pytest hook signature
    """Remove this worker's private database copy once its tests are done."""
    import rubik_core.database.db as dbmod
    from rubik_core.control import db as control_db
    from rubik_core.tenancy.registry import clear_engines

    clear_engines()
    if _WORKER_DATABASE is not None:
        if dbmod._engine is not None:
            dbmod._engine.dispose()
        _remove_sqlite_file(_WORKER_DATABASE)
    if _CONTROL_DATABASE_DIRECTORY is not None:
        if control_db._engine is not None:
            control_db._engine.dispose()
        shutil.rmtree(_CONTROL_DATABASE_DIRECTORY, ignore_errors=True)


def _engine_is_sqlite() -> tuple[bool, str]:
    """(is_sqlite, description) for the CURRENT engine.

    Reads ``dbmod._engine`` directly instead of calling ``get_engine()`` — the
    latter would *construct* a Cloud SQL engine (and dial out) just to answer
    the question, which is the exact thing being prevented. ``None`` means no
    engine has been built yet, which is safe.
    """
    import rubik_core.database.db as dbmod

    engine = dbmod._engine
    if engine is None:
        return True, "<no engine constructed yet>"
    return engine.dialect.name == "sqlite", f"{engine.dialect.name} ({engine.url})"


def _databases_are_sqlite() -> tuple[bool, str]:
    """Inspect effective URLs and cached engines without constructing engines."""
    from sqlalchemy.engine import make_url

    from rubik_core.config import settings
    from rubik_core.control import db as control_db

    if settings.CLOUDSQL_CONNECTION_NAME:
        return False, "workspace CLOUDSQL_CONNECTION_NAME is set"
    for label, url in (
        ("workspace", settings.DATABASE_URL),
        ("control", settings.CONTROL_DATABASE_URL),
    ):
        try:
            if make_url(url).get_backend_name() != "sqlite":
                return False, f"{label} URL is not SQLite"
        except Exception:  # noqa: BLE001 — malformed URLs fail closed too
            return False, f"{label} URL is invalid"
    ok, described = _engine_is_sqlite()
    if not ok:
        return False, f"workspace engine: {described}"
    engine = control_db._engine
    if engine is not None and engine.dialect.name != "sqlite":
        return False, f"control engine: {engine.dialect.name}"
    from rubik_core.tenancy.registry import cached_engines

    for engine in cached_engines():
        if engine.dialect.name != "sqlite":
            return False, f"workspace registry engine: {engine.dialect.name}"
    return True, "workspace and control are SQLite"


@pytest.fixture(autouse=True)
def _forbid_cloud_sql_engine():
    """Fail any test that runs against a non-SQLite engine.

    Checked on the way OUT as well as in: a module's own ``fresh_db`` fixture
    builds its engine after this one starts, so the post-test assertion is the
    one that actually catches the ``settings.DATABASE_URL``-only idiom if the
    ``pytest_configure`` guard above is ever weakened or bypassed.
    """
    ok, described = _databases_are_sqlite()
    assert ok, f"Test started with a non-SQLite engine: {described}"
    yield
    ok, described = _databases_are_sqlite()
    assert ok, (
        f"Test finished with a non-SQLite engine: {described}. A fixture "
        "re-pointed the database at Cloud SQL — see the repo-root conftest.py."
    )


@pytest.fixture(autouse=True)
def _restore_the_database_url():
    """Put ``settings.DATABASE_URL`` back the way this test found it.

    A fixture that points the URL at its own ``tmp_path`` file keeps it for
    the test; the next test starts from the URL this worker was given, so an
    engine rebuilt later never lands on another test's (possibly empty) file.
    """
    from rubik_core.config import settings

    url = settings.DATABASE_URL
    control_url = settings.CONTROL_DATABASE_URL
    try:
        yield
    finally:
        settings.DATABASE_URL = url
        settings.CONTROL_DATABASE_URL = control_url

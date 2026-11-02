"""A small, context-free connection pool for platform routing data.

Nothing here imports the workspace context or workspace session factory.
Constructing an engine is lazy; a session never selects a workspace database.
"""

from __future__ import annotations

import sys
from threading import Lock

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from rubik_core.config import AppMode, Settings, settings

_engine: Engine | None = None
_engine_lock = Lock()
_SessionLocal = sessionmaker(autocommit=False, autoflush=False, expire_on_commit=False)


def _enable_sqlite_foreign_keys(connection, _record) -> None:
    cursor = connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def get_engine() -> Engine:
    global _engine
    url = settings.CONTROL_DATABASE_URL
    if settings.APP_MODE == AppMode.GCP and (
        not url or url == Settings.model_fields["CONTROL_DATABASE_URL"].default
    ):
        raise RuntimeError("GCP requires an explicitly configured CONTROL_DATABASE_URL")
    # Refuse BEFORE constructing or returning an engine: teardown assertions
    # alone cannot prevent a test's mid-body URL change from dialing out.
    if "pytest" in sys.modules and (
        make_url(url).get_backend_name() != "sqlite"
        or (_engine is not None and _engine.dialect.name != "sqlite")
    ):
        raise RuntimeError("Control database must use SQLite during pytest")
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                options = (
                    {"connect_args": {"check_same_thread": False}}
                    if make_url(url).get_backend_name() == "sqlite"
                    else {
                        "pool_size": 2,
                        "max_overflow": 2,
                        "pool_timeout": 30,
                        "pool_recycle": 1800,
                        "pool_pre_ping": True,
                    }
                )
                engine = create_engine(url, **options)
                if engine.dialect.name == "sqlite":
                    event.listen(engine, "connect", _enable_sqlite_foreign_keys)
                _engine = engine
    return _engine


def control_session() -> Session:
    """Open a short-lived control session; the caller closes/commits it."""
    return _SessionLocal(bind=get_engine())

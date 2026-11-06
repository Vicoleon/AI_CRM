"""Workspace-bound SQLAlchemy sessions.

The public factory resolves an engine for every call. A request, Job or CLI
must select its workspace before touching tenant data; there is no server
database default.
"""

from __future__ import annotations

import sys
from collections.abc import Generator

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from rubik_core.tenancy.context import current_workspace
from rubik_core.tenancy.registry import engine_for

# Compatibility for pre-workspace test fixtures that inspect/reset the old
# singleton. It never supplies a production engine or bypasses current_workspace.
_engine: Engine | None = None
_connector = None
_SessionLocal = sessionmaker(autocommit=False, autoflush=False)


def get_engine() -> Engine:
    """Resolve the active workspace engine for this call."""
    slug = current_workspace()
    engine = engine_for(slug)
    if slug == "test" and "pytest" in sys.modules:
        global _engine
        _engine = engine
    return engine


def SessionLocal() -> Session:
    """Bind a fresh session to the engine selected by the current context."""
    return _SessionLocal(bind=get_engine())


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def __getattr__(name: str):
    if name == "engine":
        return get_engine()
    raise AttributeError(f"module {__name__} has no attribute {name}")

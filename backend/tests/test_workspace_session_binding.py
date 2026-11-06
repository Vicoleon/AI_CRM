"""Database sessions retain the engine chosen by their caller."""

from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from threading import Barrier

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from rubik_core.database import db


def test_sessionlocal_never_reconfigures_shared_factory(monkeypatch):
    engine = create_engine("sqlite://")

    def refuse_reconfigure(**kwargs):
        raise AssertionError("SessionLocal must not reconfigure the shared factory")

    monkeypatch.setattr(db, "get_engine", lambda: engine)
    monkeypatch.setattr(db._SessionLocal, "configure", refuse_reconfigure)
    try:
        with db.SessionLocal() as session:
            assert session.get_bind() is engine
    finally:
        engine.dispose()


def test_concurrent_sessions_keep_their_engine(monkeypatch):
    engines = (create_engine("sqlite://"), create_engine("sqlite://"))
    caller_engine = ContextVar("caller_engine")
    barrier = Barrier(2)
    factory = sessionmaker(autocommit=False, autoflush=False)

    class InterleavedFactory:
        """Pause construction until both callers have selected their bind."""

        def configure(self, **kwargs):
            factory.configure(**kwargs)

        def __call__(self, **kwargs):
            barrier.wait(timeout=5)
            return factory(**kwargs)

    monkeypatch.setattr(db, "get_engine", caller_engine.get)
    monkeypatch.setattr(db, "_SessionLocal", InterleavedFactory())

    def open_session(engine):
        token = caller_engine.set(engine)
        try:
            with db.SessionLocal() as session:
                return session.get_bind()
        finally:
            caller_engine.reset(token)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            actual = list(executor.map(open_session, engines))
        assert actual[0] is engines[0]
        assert actual[1] is engines[1]
    finally:
        for engine in engines:
            engine.dispose()

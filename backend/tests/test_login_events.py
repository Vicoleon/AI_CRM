"""Tests for login-session recording (SQLUserRegistry.record_login).

Hermetic: an in-memory SQLite engine and an explicit Session handed to the
repository, so nothing touches the global engine / Cloud SQL.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from rubik_core.database.models import Base, LoginEvent
from rubik_core.database.repository import SQLUserRegistry


@pytest.fixture
def db():
    engine = create_engine("sqlite://")  # in-memory
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _events(db) -> list[LoginEvent]:
    return db.query(LoginEvent).order_by(LoginEvent.timestamp).all()


def test_first_login_writes_row_and_sets_last_login(db):
    reg = SQLUserRegistry(db=db)
    reg.register_user("a@x.example", "Ann", "1")
    assert reg.get_user("a@x.example").last_login is None

    wrote = reg.record_login("a@x.example", ip_address="1.2.3.4", user_agent="Chrome")

    assert wrote is True
    rows = _events(db)
    assert len(rows) == 1
    assert rows[0].user_email == "a@x.example"
    assert rows[0].ip_address == "1.2.3.4"
    assert rows[0].user_agent == "Chrome"
    # last_login is now populated (the bug this feature fixes).
    assert reg.get_user("a@x.example").last_login is not None


def test_second_login_within_window_is_debounced(db):
    reg = SQLUserRegistry(db=db)
    reg.register_user("a@x.example", "Ann", "1")

    assert reg.record_login("a@x.example") is True
    # A burst of follow-up requests inside the session window must NOT log.
    assert reg.record_login("a@x.example") is False
    assert reg.record_login("a@x.example") is False

    assert len(_events(db)) == 1


def test_new_session_after_window_writes_new_row(db):
    reg = SQLUserRegistry(db=db)
    reg.register_user("a@x.example", "Ann", "1")
    assert reg.record_login("a@x.example") is True

    # Backdate the existing event beyond the default 30-min window.
    db.query(LoginEvent).update(
        {LoginEvent.timestamp: datetime.utcnow() - timedelta(minutes=31)}
    )
    db.commit()

    assert reg.record_login("a@x.example") is True
    assert len(_events(db)) == 2


def test_window_zero_forces_new_row(db):
    reg = SQLUserRegistry(db=db)
    reg.register_user("a@x.example", "Ann", "1")

    assert reg.record_login("a@x.example", window_minutes=30) is True
    assert reg.record_login("a@x.example", window_minutes=0) is True

    assert len(_events(db)) == 2


def test_login_recorded_even_without_user_row(db):
    """Audit write is FK-free — a missing user row must not block logging."""
    reg = SQLUserRegistry(db=db)

    wrote = reg.record_login("ghost@x.example")

    assert wrote is True
    assert len(_events(db)) == 1
    assert _events(db)[0].user_email == "ghost@x.example"

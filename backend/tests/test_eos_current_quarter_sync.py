"""`sync_current_quarter` self-heals the stored ``Quarter.is_current`` flag.

The flag has no auto-advance (only the seed script sets it), so a quarter
flagged current stays flagged after the calendar rolls into the next one —
e.g. a Q2 row lingering as "current" in July. These tests prove the helper
reconciles the flag to whichever quarter's date window brackets *today*.

Time-independent: quarter windows are computed relative to ``datetime.now()``
so the suite passes whenever it runs. Temp SQLite per test — no Cloud SQL.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from backend.routers.eos._shared import sync_current_quarter
from rubik_core.config import settings


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db_path = tmp_path / "test_qsync.db"
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    import rubik_core.database.db as dbmod

    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database import eos  # noqa: F401
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    yield


def _session():
    from rubik_core.database.db import SessionLocal

    return SessionLocal()


def _calendar_quarter_bounds(d: datetime) -> tuple[datetime, datetime]:
    """Calendar-quarter [start, end] for the quarter containing ``d``."""
    q = (d.month - 1) // 3 + 1
    start_month = 3 * (q - 1) + 1
    start = datetime(d.year, start_month, 1)
    if q == 4:
        end = datetime(d.year, 12, 31, 23, 59, 59)
    else:
        end = datetime(d.year, start_month + 3, 1) - timedelta(seconds=1)
    return start, end


def _make_org(db):
    from rubik_core.database.eos import Organization

    org = Organization(name="Acme", created_by="seed")
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


def _add_quarter(db, org_id, name, start, end, *, is_current):
    from rubik_core.database.eos import Quarter

    q = Quarter(
        organization_id=org_id,
        name=name,
        start_date=start,
        end_date=end,
        is_current=is_current,
        created_by="seed",
    )
    db.add(q)
    db.commit()
    db.refresh(q)
    return q


def test_advances_stale_flag_to_todays_quarter():
    now = datetime.now()
    live_start, live_end = _calendar_quarter_bounds(now)
    # A clearly-past quarter (Q1 of last year) left flagged current — stale.
    stale_start = datetime(now.year - 1, 1, 1)
    stale_end = datetime(now.year - 1, 3, 31, 23, 59, 59)

    db = _session()
    org = _make_org(db)
    live = _add_quarter(db, org.id, "Live", live_start, live_end, is_current=False)
    stale = _add_quarter(db, org.id, "Stale", stale_start, stale_end, is_current=True)

    sync_current_quarter(db)

    db.refresh(live)
    db.refresh(stale)
    assert live.is_current is True, "quarter bracketing today must become current"
    assert stale.is_current is False, "past quarter must be un-flagged"


def test_idempotent_when_already_correct():
    now = datetime.now()
    live_start, live_end = _calendar_quarter_bounds(now)

    db = _session()
    org = _make_org(db)
    live = _add_quarter(db, org.id, "Live", live_start, live_end, is_current=True)

    # Already correct → no-op, no error, flag preserved.
    sync_current_quarter(db)

    db.refresh(live)
    assert live.is_current is True


def test_no_quarter_covers_today_leaves_flags_untouched():
    now = datetime.now()
    # Only a past quarter exists, and it's (wrongly) flagged current. With no
    # quarter bracketing today we must NOT clear it — there's nothing to move
    # the flag to, and inventing a row is out of scope.
    past_start = datetime(now.year - 2, 1, 1)
    past_end = datetime(now.year - 2, 3, 31, 23, 59, 59)

    db = _session()
    org = _make_org(db)
    past = _add_quarter(db, org.id, "Past", past_start, past_end, is_current=True)

    sync_current_quarter(db)

    db.refresh(past)
    assert past.is_current is True

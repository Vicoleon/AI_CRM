"""UC-45 timeline persistence — source filter, before-cursor pagination,
intra-batch dedupe, and delta-only append.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from rubik_core.database.models import Base
from rubik_core.xray import persistence
from rubik_core.xray.schemas import TimelineDraft

CID = "77"


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    sess = Session()
    yield sess
    sess.close()


def _seed(db):
    drafts = [
        TimelineDraft(
            event_key="deal:1:created",
            source="hubspot_deal",
            title="Deal",
            ts=datetime(2026, 1, 1),
        ),
        TimelineDraft(
            event_key="news:a", source="news", title="News A", ts=datetime(2026, 6, 1)
        ),
        TimelineDraft(
            event_key="news:b", source="news", title="News B", ts=datetime(2026, 3, 1)
        ),
    ]
    persistence.append_timeline(db, CID, drafts, snapshot_id="s1")


def test_timeline_source_filter(db):
    _seed(db)
    news = persistence.timeline(db, CID, source="news")
    assert {e.title for e in news} == {"News A", "News B"}
    deals = persistence.timeline(db, CID, source="hubspot_deal")
    assert [e.title for e in deals] == ["Deal"]


def test_timeline_newest_first_and_before_cursor(db):
    _seed(db)
    ordered = persistence.timeline(db, CID)
    assert ordered[0].title == "News A"  # June is newest
    # before-cursor pages older events only (strict <)
    older = persistence.timeline(db, CID, before=datetime(2026, 5, 1))
    titles = {e.title for e in older}
    assert "News A" not in titles  # June is after the cursor
    assert "News B" in titles and "Deal" in titles


def test_intra_batch_dedupe_same_key_twice(db):
    # Two drafts with the same event_key in ONE append must not both insert
    # (would violate the unique constraint / double-count).
    drafts = [
        TimelineDraft(event_key="dup:1", source="news", title="X"),
        TimelineDraft(event_key="dup:1", source="news", title="X again"),
    ]
    n = persistence.append_timeline(db, CID, drafts, snapshot_id="s1")
    assert n == 1
    assert persistence.timeline(db, CID, source="news")[0].title == "X"


def test_append_after_seed_is_delta_only(db):
    _seed(db)
    again = [
        TimelineDraft(event_key="news:a", source="news", title="dup"),
        TimelineDraft(event_key="news:c", source="news", title="News C"),
    ]
    n = persistence.append_timeline(db, CID, again, snapshot_id="s2")
    assert n == 1  # only the new key

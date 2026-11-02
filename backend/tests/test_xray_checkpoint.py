"""UC-45 persistence + checkpoint semantics.

In-memory SQLite (never touches Cloud SQL). Covers version chaining,
latest-ready selection, append-only timeline dedupe on double-refresh, and the
fast-over-deep carry-forward of Deep-only sections with as-of stamps.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from rubik_core.database.models import Base, XrayTimelineEvent
from rubik_core.xray import checkpoint, persistence
from rubik_core.xray.schemas import TimelineDraft

CID = "12345"


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


def _snap(db, *, version, status="ready", kind="fast", signals=None, sections=None):
    return persistence.create_snapshot(
        db,
        company_id=CID,
        company_name="Acme",
        version=version,
        kind=kind,
        status=status,
        signals=signals or {},
        sections=sections or {},
        source_cursors={"engagements": {"calls": version}},
        covered_through=datetime.utcnow(),
    )


def test_version_chaining_and_latest_ready(db):
    assert persistence.next_version(db, CID) == 1
    _snap(db, version=1)
    assert persistence.next_version(db, CID) == 2
    _snap(db, version=2, status="pending", kind="deep")  # in-flight, not ready
    latest = persistence.latest_ready(db, CID)
    assert latest.version == 1  # pending row is ignored by latest_ready
    assert persistence.active_deep_run(db, CID) is not None
    assert persistence.next_version(db, CID) == 3


def test_timeline_dedupe_on_double_refresh(db):
    drafts = [
        TimelineDraft(event_key="deal:1:created", source="hubspot_deal", title="A"),
        TimelineDraft(event_key="deal:1:closed", source="hubspot_deal", title="B"),
    ]
    n1 = persistence.append_timeline(db, CID, drafts, snapshot_id="s1")
    assert n1 == 2
    # Refresh returns the same two plus one new — only the new one inserts.
    drafts2 = drafts + [TimelineDraft(event_key="news:abc", source="news", title="C")]
    n2 = persistence.append_timeline(db, CID, drafts2, snapshot_id="s2")
    assert n2 == 1
    total = db.query(XrayTimelineEvent).filter_by(company_id=CID).count()
    assert total == 3


def test_checkpoint_load_carries_cursors(db):
    _snap(db, version=1, signals={"churn": {"score": 30, "band": "Healthy"}})
    ck = checkpoint.load(db, CID)
    assert not ck.is_first_run
    assert ck.version == 2
    assert ck.base_snapshot_id is not None
    assert ck.cursors.get("engagements") == {"calls": 1}


def test_checkpoint_first_run_is_clean(db):
    ck = checkpoint.load(db, "nope")
    assert ck.is_first_run
    assert ck.version == 1
    assert ck.cursors == {}


def test_fast_over_deep_carries_forward_news_with_as_of():
    prev = {
        "executive_summary": "old",
        "recent_news": [{"headline": "X", "url": "http://x"}],
        "source_evidence": ["http://x"],
    }
    new = {"executive_summary": "new", "recent_news": [], "source_evidence": []}
    merged = checkpoint.carry_forward_sections(
        prev,
        new,
        new_kind="fast",
        prev_created_at=datetime(2026, 7, 2),
    )
    assert merged["executive_summary"] == "new"  # fresh section wins
    assert merged["recent_news"] == prev["recent_news"]  # carried forward
    assert merged["_as_of"]["recent_news"] == "2026-07-02"


def test_deep_run_does_not_carry_forward():
    prev = {"recent_news": [{"headline": "X", "url": "http://x"}]}
    new = {"recent_news": []}
    merged = checkpoint.carry_forward_sections(
        prev, new, new_kind="deep", prev_created_at=datetime(2026, 7, 2)
    )
    assert merged["recent_news"] == []  # deep regenerates everything


def test_snapshot_index_returns_highest_version(db):
    _snap(db, version=1)
    _snap(db, version=2)
    idx = persistence.snapshot_index(db, [CID])
    assert idx[CID].version == 2

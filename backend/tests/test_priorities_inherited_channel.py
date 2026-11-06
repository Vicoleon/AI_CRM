"""The ``inherited`` priorities channel and its feed↔checklist write-back.

Three contracts:
1. The channel is deterministic — ``run()`` never touches an LLM — and
   no-ops (empty digest) for anyone who isn't a successor.
2. Signals are keyed to the offboard ITEM (`entity_kind=offboard_item`),
   carrying the departed person's name and the original entity reference.
3. Dismissing/resolving an inherited card in the feed closes the matching
   ``offboard_items`` row, so the Handoffs checklist and the next run agree.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from rubik_core.database.models import Base, OffboardItem, User
from rubik_core.offboarding import store
from rubik_core.priorities.agents.inherited_agent import InheritedChannelAgent
from rubik_core.priorities.config import PrioritiesConfig
from rubik_core.priorities.models import ChannelKind, EntityKind

DEPARTED = "leaver@acme.example"
SUCCESSOR = "manager@acme.example"


@pytest.fixture
def db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr("rubik_core.database.db.SessionLocal", Session)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture
def seeded(db):
    for email, name in ((DEPARTED, "John Doe"), (SUCCESSOR, "Jane Doe")):
        db.add(User(email=email, id=email.split("@")[0], name=name, department="Sales"))
    db.commit()
    rec = store.create_record(
        db, user_email=DEPARTED, successor_email=SUCCESSOR, created_by="a@b.c"
    )
    items = store.add_items(
        db,
        rec,
        [
            {
                # Work Gmail signal with an explicit source reference. Legacy
                # commitment rows are deliberately hidden at read time because
                # they can contain unproved CRM or personal-mail content.
                "kind": "google_signal_commitment",
                "label": "Send pricing options to Acme Supply",
                "detail": "Committed before departure.",
                "urgency": "high",
                "due": "2026-08-28",
                "entity_kind": "thread",
                "entity_id": "thr-1",
                "source_links": ["https://mail.google.com/x"],
            },
            {"kind": "todo", "label": "Close out the audit ask"},
        ],
    )
    db.commit()
    return db, rec, items


def _agent(email: str) -> InheritedChannelAgent:
    return InheritedChannelAgent(user_email=email, config=PrioritiesConfig())


def test_noop_for_non_successors(seeded):
    digest = _agent("nobody@acme.example").run()
    assert digest.channel == ChannelKind.INHERITED
    assert digest.signals == []
    assert digest.error is None


def test_signals_carry_attribution_and_item_keying(seeded, monkeypatch):
    # Any LLM attempt is a test failure — the channel must be deterministic.
    monkeypatch.setattr(
        "rubik_core.priorities.agents.base.LLMJsonClient.invoke_json",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("LLM called")),
    )
    db, rec, items = seeded
    digest = _agent(SUCCESSOR).run()
    assert len(digest.signals) == 2
    sig = next(s for s in digest.signals if "Acme" in s.title)
    assert sig.title.startswith("[Inherited from John Doe]")
    assert sig.entity_kind == EntityKind.OFFBOARD_ITEM
    assert sig.entity_id == next(i.id for i in items if "Acme" in i.label)
    assert sig.attrs["departed_email"] == DEPARTED
    # The original reference rides along for cross-channel correlation.
    assert sig.attrs["orig_entity_kind"] == "thread"
    assert sig.attrs["orig_entity_id"] == "thr-1"
    assert "2 open item(s)" in digest.summary


def test_closed_items_stop_surfacing(seeded):
    db, rec, items = seeded
    for i in items:
        store.set_item_status(db, i.id, "done", actor=SUCCESSOR)
    db.commit()
    assert _agent(SUCCESSOR).run().signals == []


def test_feed_writeback_closes_the_item(seeded):
    """The home-router hook: resolving an inherited card closes the item."""
    from backend.routers.home import _offboard_item_writeback

    db, rec, items = seeded
    target = items[0]
    priority = {
        "entity_kind": "offboard_item",
        "entity_id": target.id,
        "priority_kind": "inherited",
        "covered_entity_ids": [target.id],
    }
    _offboard_item_writeback(SUCCESSOR, priority, "done")
    db.expire_all()
    assert db.get(OffboardItem, target.id).status == "done"
    # A non-inherited card is a no-op even with matching ids.
    other = items[1]
    _offboard_item_writeback(
        SUCCESSOR,
        {
            "entity_kind": "thread",
            "entity_id": other.id,
            "covered_entity_ids": [other.id],
        },
        "done",
    )
    db.expire_all()
    assert db.get(OffboardItem, other.id).status == "open"

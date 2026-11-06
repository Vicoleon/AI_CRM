"""The manager digest, built from AI Priorities (the judged HomeCard bundle).

Verifies it aggregates each team member's active priorities and renders
high-urgency first.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.agents.slack_delivery import (
    _build_digest_blocks,
    _BundlePriority,
    _team_priorities_for_department,
)
from rubik_core.database.models import Base, User


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _p(title: str, severity: str) -> _BundlePriority:
    return _BundlePriority(title=title, severity=severity, body="", link=None)


def test_build_digest_blocks_orders_high_first_and_counts():
    team = [
        ("alice", _p("Low thing", "ok")),
        ("bob", _p("Urgent thing", "urgent")),
        ("cara", _p("Info thing", "info")),
    ]
    fallback, blocks = _build_digest_blocks("sales", team, max_items=12, web_url="")

    assert blocks[0]["type"] == "header"
    assert "your team's priorities today" in blocks[0]["text"]["text"].lower()
    assert "3 active across the team · 1 high" in blocks[1]["elements"][0]["text"]
    # First rendered section is the urgent item, attributed to its owner.
    first_section = blocks[2]["text"]["text"]
    assert "Urgent thing" in first_section
    assert "@bob" in first_section
    assert "Urgent thing" in fallback


def test_team_priorities_for_department_aggregates_members_only(db):
    db.add(User(email="a@x.example", id="a", name="A", department="sales"))
    db.add(User(email="b@x.example", id="b", name="B", department="sales"))
    db.add(User(email="c@x.example", id="c", name="C", department="hr"))
    db.commit()

    canned = {
        "a@x.example": [_p("A1", "urgent")],
        "b@x.example": [_p("B1", "info"), _p("B2", "ok")],
        "c@x.example": [_p("C1", "urgent")],  # different dept — must not appear
    }
    with patch(
        "backend.agents.slack_delivery._load_bundle_priority_items",
        side_effect=lambda email, **_kwargs: canned.get(email, []),
    ):
        team = _team_priorities_for_department(db, "sales")

    assert {member for member, _ in team} == {"a", "b"}  # hr member excluded
    assert {p.title for _, p in team} == {"A1", "B1", "B2"}
    assert all(isinstance(p, _BundlePriority) for _, p in team)

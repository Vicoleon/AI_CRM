"""Only a user's rating counts may inform later prompts.

Old rated answer text can contain CRM details after deal access is removed.
This test stores such an answer and confirms the prompt builder omits it and
another user's ratings. Meeting Prep has the same regression in its tests.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from rubik_core.database.models import Base
from rubik_core.feedback import store
from rubik_core.feedback.wgll import build_wgll_context

RATER = "rater@acme.example"
STRANGER = "stranger@acme.example"


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    yield Session
    Base.metadata.drop_all(bind=engine)


def test_priorities_wgll_block_never_replays_old_text_or_other_users(db_session):
    sess = db_session()
    try:
        store.upsert_feedback(
            sess,
            user_email=RATER,
            surface="priorities",
            proposal_id="p-1",
            proposal_text="Chase the Acme renewal before Friday's call",
            rating="useful",
            comment="exactly right",
        )

        own = build_wgll_context(sess, surface="priorities", user_email=RATER)
        assert "1 were rated useful" in own
        assert "Acme renewal" not in own
        assert "exactly right" not in own

        # The stranger has no ratings: their block must be empty.
        others = build_wgll_context(sess, surface="priorities", user_email=STRANGER)
        assert others == ""
        assert own != others
    finally:
        sess.close()


def test_priorities_wgll_block_requires_a_user(db_session):
    sess = db_session()
    try:
        store.upsert_feedback(
            sess,
            user_email=RATER,
            surface="priorities",
            proposal_id="p-2",
            proposal_text="Prep the Globex pitch deck",
            rating="useful",
        )
        # No user to scope to → refuse rather than fall back org-wide.
        assert build_wgll_context(sess, surface="priorities", user_email="") == ""
    finally:
        sess.close()

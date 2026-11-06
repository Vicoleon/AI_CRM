"""Regression tests for the HITL confirmation guard hardened after the
"Migration Plan" hallucination RCA:

- ``_is_confirmation_reply`` must match affirmations on WORD boundaries and only
  for short messages, so ordinary words no longer resume a parked calendar/draft
  action ("ok" ⊂ "book"/"look", "yes" ⊂ "yesterday").
- ``_expire_stale_hitl_pointer`` must drop a parked pointer when the current
  message isn't an affirmation, or when it was parked longer ago than the TTL.

Pure logic — no GCP / LLM / DB.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from backend.routers.chat import _expire_stale_hitl_pointer
from rubik_core.config import settings
from rubik_core.orchestration.adk_orchestrator import _is_confirmation_reply


@pytest.mark.parametrize(
    "text",
    ["yes", "Yes", "ok", "okay", "sure", "confirm", "approve", "go ahead", "do it"],
)
def test_short_affirmations_are_confirmations(text):
    assert _is_confirmation_reply(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "book it tomorrow at 3pm",  # "ok" ⊂ "book" — the RCA false-positive
        "what did I do yesterday",  # "yes" ⊂ "yesterday"
        "I looked at the report",  # "ok" ⊂ "looked"
        "no",
        "cancel that",
        "",
        "   ",
        # too long to be a terse confirmation even though it contains "yes"
        "yes but actually change the attendees and push it to next week please",
    ],
)
def test_non_affirmations_are_not_confirmations(text):
    assert _is_confirmation_reply(text) is False


def test_expire_clears_pointer_on_non_affirmation():
    sess = SimpleNamespace(
        id="s1",
        last_adk_invocation_id="inv-1",
        updated_at=datetime.utcnow(),
    )
    _expire_stale_hitl_pointer(sess, "actually, what's on my calendar tomorrow?")
    assert sess.last_adk_invocation_id is None


def test_expire_keeps_fresh_affirmation():
    sess = SimpleNamespace(
        id="s1",
        last_adk_invocation_id="inv-1",
        updated_at=datetime.utcnow(),
    )
    _expire_stale_hitl_pointer(sess, "yes")
    assert sess.last_adk_invocation_id == "inv-1"


def test_expire_clears_stale_affirmation_past_ttl():
    ttl = settings.CHAT_HITL_PENDING_TTL_SECONDS or 900
    sess = SimpleNamespace(
        id="s1",
        last_adk_invocation_id="inv-1",
        updated_at=datetime.utcnow() - timedelta(seconds=ttl + 60),
    )
    _expire_stale_hitl_pointer(sess, "yes")
    assert sess.last_adk_invocation_id is None


def test_expire_noop_when_no_pointer():
    sess = SimpleNamespace(id="s1", last_adk_invocation_id=None, updated_at=None)
    _expire_stale_hitl_pointer(sess, "yes")
    assert sess.last_adk_invocation_id is None

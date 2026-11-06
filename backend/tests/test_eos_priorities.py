"""EOS → AI-Priorities plumbing tests.

Covers the deterministic glue that feeds EOS (Rocks / To-Dos / Issues /
Milestones) into the priorities pipeline and auto-clears cards on completion:

  * ``_reattach_eos_entities`` — re-stamps entity_kind/entity_id/priority_kind
    (and the teammate the item belongs to) onto any priority citing an EOS
    signal, so the UC-25 memory key and the completion auto-resolve hooks line
    up even when the decision-maker LLM garbles the opaque EOS row UUID.
  * ``bundle_to_metadata`` / ``bundle_from_metadata`` — the team-scope
    ``target_email`` / ``target_label`` fields survive the persistence round-trip.
  * ``resolve_entity_everywhere`` / ``reopen_entity_everywhere`` — a completed
    EOS entity clears (and re-opening revives) the card for EVERY user who was
    shown it, not just the owner. Reopen only revives rows WE auto-resolved,
    never ones the user dismissed/snoozed themselves.
  * ``backend.routers.eos._priority_sync`` — the completion-transition wrappers
    dispatch to resolve vs reopen with the right EntityKind / PriorityKind.

DB-light: the pure functions touch no DB; the cross-user memory tests use an
in-memory SQLite engine (StaticPool) and never import the FastAPI app, so they
run anywhere and never touch deployed Cloud SQL.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from rubik_core.database.models import Base, PriorityMemory
from rubik_core.priorities import (
    ChannelKind,
    ChannelSignal,
    EntityKind,
    PriorityCandidate,
    PriorityKind,
    bundle_from_metadata,
    bundle_to_metadata,
)
from rubik_core.priorities.memory import (
    STATE_ACTIVE,
    STATE_DISMISSED,
    STATE_RESOLVED,
)
from rubik_core.priorities.models import PrioritiesBundle
from rubik_core.priorities.orchestrator import _reattach_eos_entities

# ── _reattach_eos_entities (pure, no DB) ──────────────────────────────────────


def _eos_todo_signal(entity_id: str, *, is_own: bool) -> ChannelSignal:
    return ChannelSignal(
        id="eos-todo-1",
        channel=ChannelKind.EOS,
        title="Ship the Q3 deck",
        entity_kind=EntityKind.TODO,
        entity_id=entity_id,
        entity_label="Ship the Q3 deck",
        attrs={
            "eos_priority_kind": "eos_todo_due",
            "is_own": is_own,
            "owner_email": "rep@x.example",
            "owner_name": "Rep",
        },
    )


def test_reattach_stamps_entity_and_target_from_eos_signal():
    """A priority citing an EOS signal gets its entity keys + target person
    overwritten from the signal, no matter what the LLM emitted."""
    entity_id = str(uuid.uuid4())
    sig = _eos_todo_signal(entity_id, is_own=False)

    # Decision-maker got the fields WRONG / left them empty.
    cand = PriorityCandidate(
        title="Rep needs to ship the Q3 deck",
        signal_ids=["eos-todo-1"],
        entity_kind=EntityKind.DEAL,  # wrong
        entity_id="garbled-uuid",  # wrong
        priority_kind=None,  # missing
        entity_label="",  # empty
        target_email=None,
        target_label=None,
    )

    _reattach_eos_entities([cand], {"eos-todo-1": sig})

    assert cand.entity_kind == EntityKind.TODO
    assert cand.entity_id == entity_id
    assert cand.priority_kind == PriorityKind.EOS_TODO_DUE
    assert cand.entity_label == "Ship the Q3 deck"
    # is_own=False → this is a teammate's item, so the card can offer a
    # "Slack follow-up" targeting the owner.
    assert cand.target_email == "rep@x.example"
    assert cand.target_label == "Rep"


def test_reattach_leaves_target_none_for_own_item():
    """When the EOS item is the user's OWN (is_own=True), there is no teammate
    to target — target_email/target_label stay None."""
    entity_id = str(uuid.uuid4())
    sig = _eos_todo_signal(entity_id, is_own=True)

    cand = PriorityCandidate(
        title="Ship the Q3 deck",
        signal_ids=["eos-todo-1"],
    )

    _reattach_eos_entities([cand], {"eos-todo-1": sig})

    # Entity keys still get stamped...
    assert cand.entity_kind == EntityKind.TODO
    assert cand.entity_id == entity_id
    assert cand.priority_kind == PriorityKind.EOS_TODO_DUE
    # ...but there's no one else to follow up with.
    assert cand.target_email is None
    assert cand.target_label is None


def test_reattach_ignores_non_eos_candidate():
    """A priority with no cited EOS signal is left completely untouched."""
    gmail_sig = ChannelSignal(
        id="g-1",
        channel=ChannelKind.GMAIL,
        title="Re: renewal",
        entity_kind=EntityKind.THREAD,
        entity_id="thread-9",
    )
    cand = PriorityCandidate(
        title="Reply to Acme",
        signal_ids=["g-1"],
        entity_kind=EntityKind.THREAD,
        entity_id="thread-9",
        priority_kind=PriorityKind.FOLLOW_UP_DEBT,
        target_email=None,
        target_label=None,
    )

    _reattach_eos_entities([cand], {"g-1": gmail_sig})

    assert cand.entity_kind == EntityKind.THREAD
    assert cand.entity_id == "thread-9"
    assert cand.priority_kind == PriorityKind.FOLLOW_UP_DEBT
    assert cand.target_email is None
    assert cand.target_label is None


# ── bundle metadata round-trip ────────────────────────────────────────────────


def test_bundle_roundtrip_preserves_target_fields():
    """target_email / target_label survive bundle_to_metadata → from_metadata."""
    entity_id = str(uuid.uuid4())
    bundle = PrioritiesBundle(
        user_email="manager@x.example",
        headline="Team execution",
        priorities=[
            PriorityCandidate(
                id="p-1",
                title="Rep's Q3 deck is due",
                urgency="high",
                source_channels=[ChannelKind.EOS],
                signal_ids=["eos-todo-1"],
                priority_kind=PriorityKind.EOS_TODO_DUE,
                entity_kind=EntityKind.TODO,
                entity_id=entity_id,
                entity_label="Ship the Q3 deck",
                target_email="rep@x.example",
                target_label="Rep",
            )
        ],
    )

    from rubik_core.tenancy.context import current_workspace

    metadata = bundle_to_metadata(bundle)
    metadata["crm_provenance"] = {
        "workspace": current_workspace(),
        "deal_ids": [],
        "unprovable": False,
    }
    rebuilt = bundle_from_metadata("manager@x.example", metadata)

    assert len(rebuilt.priorities) == 1
    p = rebuilt.priorities[0]
    assert p.target_email == "rep@x.example"
    assert p.target_label == "Rep"
    # sanity: the EOS entity keys round-trip too.
    assert p.priority_kind == PriorityKind.EOS_TODO_DUE
    assert p.entity_kind == EntityKind.TODO
    assert p.entity_id == entity_id


# ── cross-user memory helpers (real in-memory SQLite) ─────────────────────────


@pytest.fixture
def memory_db(monkeypatch):
    """In-memory SQLite bound into ``priorities.memory.SessionLocal``.

    StaticPool keeps a single connection so every ``SessionLocal()`` the module
    opens sees the same rows. Never touches Cloud SQL."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr("rubik_core.priorities.memory.SessionLocal", Session)
    yield Session
    Base.metadata.drop_all(bind=engine)


def _seed_memory_row(
    Session,
    *,
    user_email: str,
    state: str,
    entity_id: str,
    entity_kind: str = "todo",
    priority_kind: str = "eos_todo_due",
) -> None:
    now = datetime.utcnow()
    with Session() as db:
        db.add(
            PriorityMemory(
                id=str(uuid.uuid4()),
                user_email=user_email,
                entity_kind=entity_kind,
                entity_id=entity_id,
                priority_kind=priority_kind,
                state=state,
                first_seen_at=now,
                last_seen_at=now,
                last_state_change_at=now,
                last_state_change_by="system",
            )
        )
        db.commit()


def _states_by_user(Session, entity_id: str) -> dict[str, str]:
    with Session() as db:
        rows = (
            db.query(PriorityMemory).filter(PriorityMemory.entity_id == entity_id).all()
        )
        return {r.user_email: r.state for r in rows}


def test_resolve_then_reopen_entity_everywhere(memory_db):
    """Completing an EOS entity resolves the card for BOTH the owner and the
    manager who was also shown it; re-opening revives both."""
    from rubik_core.priorities.memory import (
        reopen_entity_everywhere,
        resolve_entity_everywhere,
    )

    entity_id = str(uuid.uuid4())
    _seed_memory_row(
        memory_db, user_email="owner@x.example", state=STATE_ACTIVE, entity_id=entity_id
    )
    _seed_memory_row(
        memory_db,
        user_email="manager@x.example",
        state=STATE_ACTIVE,
        entity_id=entity_id,
    )

    resolved = resolve_entity_everywhere(
        entity_kind=EntityKind.TODO,
        entity_id=entity_id,
        priority_kind=PriorityKind.EOS_TODO_DUE,
        evidence={"kind": "eos_todo_done"},
    )
    assert resolved == 2
    assert _states_by_user(memory_db, entity_id) == {
        "owner@x.example": STATE_RESOLVED,
        "manager@x.example": STATE_RESOLVED,
    }

    reopened = reopen_entity_everywhere(
        entity_kind=EntityKind.TODO,
        entity_id=entity_id,
        priority_kind=PriorityKind.EOS_TODO_DUE,
    )
    assert reopened == 2
    assert _states_by_user(memory_db, entity_id) == {
        "owner@x.example": STATE_ACTIVE,
        "manager@x.example": STATE_ACTIVE,
    }


def test_reopen_only_revives_auto_resolved_rows(memory_db):
    """reopen_entity_everywhere flips only rows we auto-resolved back to active;
    a row the user dismissed themselves is left untouched."""
    from rubik_core.priorities.memory import reopen_entity_everywhere

    entity_id = str(uuid.uuid4())
    _seed_memory_row(
        memory_db,
        user_email="owner@x.example",
        state=STATE_RESOLVED,
        entity_id=entity_id,
    )
    _seed_memory_row(
        memory_db,
        user_email="manager@x.example",
        state=STATE_DISMISSED,
        entity_id=entity_id,
    )

    reopened = reopen_entity_everywhere(
        entity_kind=EntityKind.TODO,
        entity_id=entity_id,
        priority_kind=PriorityKind.EOS_TODO_DUE,
    )
    assert reopened == 1
    assert _states_by_user(memory_db, entity_id) == {
        "owner@x.example": STATE_ACTIVE,  # our auto-resolve → revived
        "manager@x.example": STATE_DISMISSED,  # user's own hide → left alone
    }


# ── _priority_sync completion-transition wrappers ─────────────────────────────


@pytest.fixture
def sync_spies(monkeypatch):
    """Replace the cross-user memory helpers with spies so the sync wrappers
    can be checked without a DB."""
    from rubik_core.priorities import memory as pm

    resolve_spy = MagicMock(return_value=1)
    reopen_spy = MagicMock(return_value=1)
    monkeypatch.setattr(pm, "resolve_entity_everywhere", resolve_spy)
    monkeypatch.setattr(pm, "reopen_entity_everywhere", reopen_spy)
    return resolve_spy, reopen_spy


def test_sync_todo_done_resolves(sync_spies):
    from backend.routers.eos._priority_sync import sync_todo_priority

    resolve_spy, reopen_spy = sync_spies
    sync_todo_priority(
        todo_id="todo-uuid", was_done=False, is_done=True, actor="me@x.example"
    )

    reopen_spy.assert_not_called()
    resolve_spy.assert_called_once()
    kwargs = resolve_spy.call_args.kwargs
    assert kwargs["entity_kind"] == EntityKind.TODO
    assert kwargs["priority_kind"] == PriorityKind.EOS_TODO_DUE
    assert kwargs["entity_id"] == "todo-uuid"


def test_sync_todo_reopened_reopens(sync_spies):
    from backend.routers.eos._priority_sync import sync_todo_priority

    resolve_spy, reopen_spy = sync_spies
    sync_todo_priority(
        todo_id="todo-uuid", was_done=True, is_done=False, actor="me@x.example"
    )

    resolve_spy.assert_not_called()
    reopen_spy.assert_called_once()
    kwargs = reopen_spy.call_args.kwargs
    assert kwargs["entity_kind"] == EntityKind.TODO
    assert kwargs["priority_kind"] == PriorityKind.EOS_TODO_DUE
    assert kwargs["entity_id"] == "todo-uuid"


def test_sync_rock_done_resolves(sync_spies):
    from backend.routers.eos._priority_sync import sync_rock_priority
    from rubik_core.database.eos._base import RockStatus

    resolve_spy, reopen_spy = sync_spies
    sync_rock_priority(
        rock_id="rock-uuid", status=RockStatus.DONE, actor="me@x.example"
    )

    reopen_spy.assert_not_called()
    resolve_spy.assert_called_once()
    kwargs = resolve_spy.call_args.kwargs
    assert kwargs["entity_kind"] == EntityKind.ROCK
    assert kwargs["priority_kind"] == PriorityKind.EOS_ROCK_ATTENTION
    assert kwargs["entity_id"] == "rock-uuid"


def test_sync_rock_off_track_reopens(sync_spies):
    """Only DONE is terminal — any other status (off_track/on_track) reopens so
    a recurring condition can re-surface on the next run."""
    from backend.routers.eos._priority_sync import sync_rock_priority
    from rubik_core.database.eos._base import RockStatus

    resolve_spy, reopen_spy = sync_spies
    sync_rock_priority(
        rock_id="rock-uuid", status=RockStatus.OFF_TRACK, actor="me@x.example"
    )

    resolve_spy.assert_not_called()
    reopen_spy.assert_called_once()
    kwargs = reopen_spy.call_args.kwargs
    assert kwargs["entity_kind"] == EntityKind.ROCK
    assert kwargs["priority_kind"] == PriorityKind.EOS_ROCK_ATTENTION
    assert kwargs["entity_id"] == "rock-uuid"

"""UC-25 tests — priority memory + dismiss/snooze/resolve + post-filter.

The memory module talks to ``SessionLocal()`` directly so tests use a
small in-memory pretend store identical to the one
``test_priorities_playbooks.py`` uses. The post-filter logic doesn't
touch the DB at all (operates on plain dicts) so it's tested with
direct calls.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from rubik_core.priorities import (
    ChannelKind,
    ChannelSignal,
    EntityKind,
    PriorityCandidate,
    PriorityKind,
)
from rubik_core.priorities.memory import (
    STATE_ACTIVE,
    STATE_DISMISSED,
    STATE_RESOLVED,
    STATE_SNOOZED,
    apply_post_filter,
)

# ── In-memory stand-in for the SQLAlchemy session ───────────────────────────


class _InMemoryMemoryStore:
    """Mimics SessionLocal() for the handful of queries memory.py runs."""

    def __init__(self) -> None:
        self.rows: dict[str, MagicMock] = {}

    def session_factory(self):
        store = self
        mock_db = MagicMock()

        def query_factory(_model):
            return _MemoryQuery(store)

        mock_db.query.side_effect = query_factory

        def add(obj):
            store.rows[obj.id] = obj

        def delete(obj):
            store.rows.pop(obj.id, None)

        def commit():
            return None

        def refresh(obj):
            pass

        mock_db.add = add
        mock_db.delete = delete
        mock_db.commit = commit
        mock_db.refresh = refresh

        ctx = MagicMock()
        ctx.__enter__.return_value = mock_db
        ctx.__exit__.return_value = False
        return ctx


class _MemoryQuery:
    def __init__(self, store: _InMemoryMemoryStore) -> None:
        self.store = store
        self._filters: list[tuple[str, object]] = []
        self._exclude: tuple[str, object] | None = None

    def filter(self, expr):
        text = str(expr).lower()
        right = getattr(expr, "right", None)
        value = right.value if right is not None else None
        if "user_email" in text:
            self._filters.append(("user_email", value))
        elif "entity_kind" in text:
            self._filters.append(("entity_kind", value))
        elif "entity_id" in text:
            self._filters.append(("entity_id", value))
        elif "priority_kind" in text:
            self._filters.append(("priority_kind", value))
        elif "state !=" in text or "state <>" in text:
            self._exclude = ("state", value)
        elif "state" in text:
            self._filters.append(("state", value))
        return self

    def order_by(self, *_):
        return self

    def all(self):
        results = list(self.store.rows.values())
        for col, want in self._filters:
            results = [r for r in results if getattr(r, col, None) == want]
        if self._exclude:
            col, exclude = self._exclude
            results = [r for r in results if getattr(r, col, None) != exclude]
        return results

    def first(self):
        results = self.all()
        return results[0] if results else None


@pytest.fixture
def store(monkeypatch):
    s = _InMemoryMemoryStore()
    monkeypatch.setattr(
        "rubik_core.priorities.memory.SessionLocal",
        s.session_factory,
    )
    return s


# ── Memory façade smoke tests ───────────────────────────────────────────────


def test_resolve_creates_row_when_none(store):
    from rubik_core.priorities.memory import load_for_run, resolve

    row = resolve(
        user_email="me@x.example",
        entity_kind=EntityKind.EVENT,
        entity_id="evt-1",
        priority_kind=PriorityKind.PREP_DEBT,
        evidence={"kind": "meeting_prep", "id": "prep-1"},
    )
    assert row is not None
    assert row.state == STATE_RESOLVED
    assert row.resolved_evidence == {"kind": "meeting_prep", "id": "prep-1"}

    # load_for_run INCLUDES resolved rows: generation has to see them, or the
    # decision-maker re-proposes the entity and the read-path filter deletes it
    # again (the silent empty feed).
    loaded = load_for_run("me@x.example")
    assert ("event", "evt-1", "prep_debt") in loaded
    assert loaded[("event", "evt-1", "prep_debt")].state == STATE_RESOLVED


def test_dismiss_then_resurface_on_new_evidence(store):
    from rubik_core.priorities.memory import dismiss

    row = dismiss(
        user_email="me@x.example",
        entity_kind=EntityKind.THREAD,
        entity_id="t-1",
        priority_kind=PriorityKind.FOLLOW_UP_DEBT,
        actor="me@x.example",
    )
    assert row is not None
    assert row.state == STATE_DISMISSED

    # A new signal from BEFORE the dismissal → DROP.
    threshold = datetime.fromisoformat(row.last_state_change_at)
    old_signal = ChannelSignal(
        id="g-1",
        channel=ChannelKind.GMAIL,
        title="t",
        timestamp=(threshold - timedelta(days=1)).isoformat(),
    )
    new_signal = ChannelSignal(
        id="g-2",
        channel=ChannelKind.GMAIL,
        title="t",
        timestamp=(threshold + timedelta(hours=1)).isoformat(),
    )

    p = PriorityCandidate(
        title="follow up Acme",
        urgency="medium",
        priority_kind=PriorityKind.FOLLOW_UP_DEBT,
        entity_kind=EntityKind.THREAD,
        entity_id="t-1",
        signal_ids=["g-1"],  # only the old signal
    )
    memory = {row.key: row}
    kept, drops = apply_post_filter(
        [p],
        memory,
        signal_by_id={"g-1": old_signal, "g-2": new_signal},
    )
    assert kept == []
    assert any("dismissed" in d for d in drops)

    # Same priority with a NEW signal → resurface.
    p_with_new = PriorityCandidate(
        title="follow up Acme",
        urgency="medium",
        priority_kind=PriorityKind.FOLLOW_UP_DEBT,
        entity_kind=EntityKind.THREAD,
        entity_id="t-1",
        signal_ids=["g-2"],  # newer signal
    )
    # The run happens after the new message landed: evidence has to have
    # ARRIVED to count, so "now" sits past its timestamp.
    kept, drops = apply_post_filter(
        [p_with_new],
        memory,
        now=threshold + timedelta(hours=2),
        signal_by_id={"g-1": old_signal, "g-2": new_signal},
    )
    assert len(kept) == 1
    assert drops == []


def test_snooze_drops_until_deadline(store):
    from rubik_core.priorities.memory import snooze

    until = datetime.utcnow() + timedelta(days=2)
    row = snooze(
        user_email="me@x.example",
        entity_kind=EntityKind.DEAL,
        entity_id="d-1",
        priority_kind=PriorityKind.NEGLECT_DEBT,
        until=until,
        actor="me@x.example",
    )
    assert row.state == STATE_SNOOZED

    p = PriorityCandidate(
        title="check on Acme deal",
        urgency="medium",
        priority_kind=PriorityKind.NEGLECT_DEBT,
        entity_kind=EntityKind.DEAL,
        entity_id="d-1",
        signal_ids=[],
    )
    memory = {row.key: row}
    kept, drops = apply_post_filter([p], memory)
    assert kept == []
    assert any("snoozed" in d for d in drops)


def test_snooze_expired_revives_priority(store):
    from rubik_core.priorities.memory import snooze

    until = datetime.utcnow() - timedelta(days=1)  # already passed
    row = snooze(
        user_email="me@x.example",
        entity_kind=EntityKind.DEAL,
        entity_id="d-1",
        priority_kind=PriorityKind.NEGLECT_DEBT,
        until=until,
        actor="me@x.example",
    )
    p = PriorityCandidate(
        title="check on Acme deal",
        urgency="medium",
        priority_kind=PriorityKind.NEGLECT_DEBT,
        entity_kind=EntityKind.DEAL,
        entity_id="d-1",
    )
    kept, drops = apply_post_filter([p], {row.key: row})
    assert len(kept) == 1
    assert drops == []


def test_resolved_priorities_always_dropped(store):
    from rubik_core.priorities.memory import resolve

    row = resolve(
        user_email="me@x.example",
        entity_kind=EntityKind.EVENT,
        entity_id="evt-1",
        priority_kind=PriorityKind.PREP_DEBT,
        evidence={"kind": "meeting_prep"},
    )
    p = PriorityCandidate(
        title="prep Acme",
        urgency="high",
        priority_kind=PriorityKind.PREP_DEBT,
        entity_kind=EntityKind.EVENT,
        entity_id="evt-1",
        signal_ids=["c-1"],
    )
    # Memory wouldn't normally include resolved rows but tolerate it.
    kept, drops = apply_post_filter([p], {row.key: row})
    assert kept == []
    assert any("resolved" in d for d in drops)


def test_priority_without_entity_passes_through(store):
    """A priority that lacks entity_kind/entity_id (pre-UC-24) is not in
    memory's domain, so the post-filter must let it through untouched."""
    p = PriorityCandidate(title="legacy", urgency="medium")
    kept, drops = apply_post_filter([p], memory={})
    assert kept == [p]
    assert drops == []


def test_record_seen_marks_priorities_active(store):
    from rubik_core.priorities.memory import load_for_run, record_seen

    p = PriorityCandidate(
        id="p-1",
        title="Prep Acme",
        urgency="high",
        priority_kind=PriorityKind.PREP_DEBT,
        entity_kind=EntityKind.EVENT,
        entity_id="evt-1",
    )
    record_seen("me@x.example", [p])
    active = load_for_run("me@x.example")
    assert ("event", "evt-1", "prep_debt") in active
    assert active[("event", "evt-1", "prep_debt")].state == STATE_ACTIVE


def test_record_seen_skips_priorities_without_entity(store):
    from rubik_core.priorities.memory import load_for_run, record_seen

    record_seen(
        "me@x.example",
        [PriorityCandidate(title="legacy", urgency="medium")],
    )
    # Nothing to write — no row created.
    assert load_for_run("me@x.example") == {}

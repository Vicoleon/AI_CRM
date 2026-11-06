"""AI Priorities card history: every real transition writes one ``priority_events`` row.

What this file pins (``rubik_core/insights/priority_events.py`` and the writers
in ``rubik_core/priorities/memory.py``, ``backend/routers/home.py`` and
``backend/agents/slack_delivery.py``):

1. **One row per real transition, none for a no-op.** Dismissing a dismissed
   card, re-snoozing a snoozed one, or a hook pre-emptively resolving a card
   nobody was shown is not history.
2. **Who did it.** The person for their own clicks, ``system`` for evidence and
   expiry.
3. **Telemetry never breaks the action.** A recording failure, or a missing
   table, leaves the memory transition exactly as it would have been; a write
   inside a caller's session rolls back only itself.
4. **The card action** (Slack follow-up) and the morning DM are recorded only
   after they actually happened.

Collaborators are faked by STRING target (sibling suites ``importlib.reload()``
modules mid-run). Nothing here touches the network.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from rubik_core.database.schemas import UserProfile

OWNER = "owner@acme.example"
MANAGER = "manager@acme.example"
EK, PK = "todo", "eos_todo_due"


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """The app's ``SessionLocal`` bound to a throwaway sqlite file with every
    table, and two people in ``users`` for the department snapshot."""
    import rubik_core.database.db as dbmod
    from rubik_core.config import settings
    from rubik_core.database.models import Base, User

    monkeypatch.setattr(
        settings, "DATABASE_URL", f"sqlite:///{tmp_path / 'priority_events.db'}"
    )
    monkeypatch.setattr(settings, "INSIGHTS_ENABLED", True, raising=False)
    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)
    Base.metadata.create_all(bind=engine)
    with dbmod.SessionLocal() as db:
        db.add(User(email=OWNER, id="o", name="Owner", department="Sales"))
        db.add(User(email=MANAGER, id="m", name="Manager", department="Systems"))
        db.commit()
    try:
        yield dbmod.SessionLocal
    finally:
        app.dependency_overrides.clear()


def _events():
    import rubik_core.database.db as dbmod
    from rubik_core.database.insights import PriorityEvent

    with dbmod.SessionLocal() as db:
        return [
            SimpleNamespace(
                user_email=e.user_email,
                kind=e.kind,
                priority_id=e.priority_id,
                entity_kind=e.entity_kind,
                entity_id=e.entity_id,
                priority_kind=e.priority_kind,
                actor=e.actor,
                department_slug=e.department_slug,
            )
            for e in db.query(PriorityEvent).order_by(PriorityEvent.id).all()
        ]


def _memory():
    from rubik_core.priorities import memory

    return memory


def _seen(entity_id="t1", priority_id="card-1"):
    """Surface a card the way the orchestrator does (writes an active row)."""
    _memory().record_seen(
        OWNER,
        [
            SimpleNamespace(
                id=priority_id, entity_kind=EK, entity_id=entity_id, priority_kind=PK
            )
        ],
    )


def _key(entity_id="t1"):
    return {
        "user_email": OWNER,
        "entity_kind": EK,
        "entity_id": entity_id,
        "priority_kind": PK,
    }


# ── 1/2. memory transitions ──────────────────────────────────────────────────


def test_surfacing_a_card_is_not_an_event():
    _seen()
    assert _events() == []


def test_each_user_transition_writes_exactly_one_event():
    memory = _memory()
    _seen()

    memory.dismiss(**_key(), actor=OWNER, priority_id="card-1")
    memory.dismiss(**_key(), actor=OWNER, priority_id="card-1")  # no-op
    memory.reopen(**_key(), actor=OWNER)
    memory.reopen(**_key(), actor=OWNER)  # no-op
    until = datetime.utcnow() + timedelta(days=2)
    memory.snooze(**_key(), until=until, actor=OWNER, priority_id="card-1")
    memory.snooze(**_key(), until=until, actor=OWNER, priority_id="card-1")  # no-op
    memory.resolve_manual(**_key(), actor=OWNER, priority_id="card-1")
    memory.resolve_manual(**_key(), actor=OWNER, priority_id="card-1")  # no-op

    events = _events()
    assert [e.kind for e in events] == ["dismissed", "reopened", "snoozed", "resolved"]
    for e in events:
        assert (e.user_email, e.entity_kind, e.entity_id, e.priority_kind) == (
            OWNER,
            EK,
            "t1",
            PK,
        )
        assert e.actor == OWNER
        assert e.department_slug == "sales"  # snapshot from users
    assert [e.priority_id for e in events] == ["card-1", None, "card-1", "card-1"]


def test_evidence_resolving_a_shown_card_is_auto_resolved_by_system():
    _seen()
    _memory().resolve(**_key(), evidence={"kind": "meeting_prep"})
    [event] = _events()
    assert (event.kind, event.actor) == ("auto_resolved", "system")


def test_a_hook_resolving_a_card_nobody_was_shown_is_not_an_event():
    """Meeting prep resolves BOTH prep kinds for an event every run, whether or
    not either card ever existed. The memory row is still written."""
    memory = _memory()
    row = memory.resolve(**_key("never-shown"), evidence={"kind": "meeting_prep"})
    assert row is not None and row.state == "resolved"
    assert _events() == []


def test_a_card_action_on_a_secondary_entity_is_an_event():
    """``record_seen`` writes only the PRIMARY entity; ✗ on a card covering two
    To-Dos creates the second row, and that is the person acting."""
    memory = _memory()
    memory.dismiss(**_key("secondary"), actor=OWNER, priority_id="card-1")
    memory.dismiss(**_key("no-card"), actor=OWNER)  # no row, no card: not an event
    [event] = _events()
    assert (event.kind, event.entity_id, event.priority_id) == (
        "dismissed",
        "secondary",
        "card-1",
    )


def test_entity_wide_resolve_and_reopen_write_one_event_per_holder():
    memory = _memory()
    _seen()
    # The manager's team-scoped briefing surfaced the same To-Do.
    memory.record_seen(
        MANAGER,
        [
            SimpleNamespace(
                id="mgr-card", entity_kind=EK, entity_id="t1", priority_kind=PK
            )
        ],
    )
    before = len(_events())
    assert before == 0

    assert (
        memory.resolve_entity_everywhere(
            entity_kind=EK,
            entity_id="t1",
            priority_kind=PK,
            evidence={"kind": "eos_todo_done"},
            actor=OWNER,
        )
        == 2
    )
    resolved = _events()[before:]
    assert sorted((e.user_email, e.kind, e.actor) for e in resolved) == [
        (MANAGER, "auto_resolved", OWNER),
        (OWNER, "auto_resolved", OWNER),
    ]
    assert {e.department_slug for e in resolved} == {"sales", "systems"}
    assert all(e.priority_id is None for e in resolved)

    # Running it again changes nothing, so it records nothing.
    memory.resolve_entity_everywhere(
        entity_kind=EK, entity_id="t1", priority_kind=PK, actor=OWNER
    )
    assert len(_events()) == before + 2

    assert (
        memory.reopen_entity_everywhere(
            entity_kind=EK, entity_id="t1", priority_kind=PK, actor=OWNER
        )
        == 2
    )
    reopened = _events()[before + 2 :]
    assert sorted((e.user_email, e.kind) for e in reopened) == [
        (MANAGER, "reopened"),
        (OWNER, "reopened"),
    ]


def test_reopen_everywhere_skips_a_manual_resolve_and_records_only_what_it_revived():
    memory = _memory()
    _seen()
    memory.resolve_manual(**_key(), actor=OWNER, priority_id="card-1")
    before = len(_events())
    assert (
        memory.reopen_entity_everywhere(
            entity_kind=EK, entity_id="t1", priority_kind=PK, actor=OWNER
        )
        == 0
    )
    assert len(_events()) == before


def test_an_expired_snooze_that_brings_the_card_back_is_reopened_by_system():
    memory = _memory()
    _seen()
    memory.snooze(
        **_key(),
        until=datetime.utcnow() - timedelta(minutes=1),
        actor=OWNER,
        priority_id="card-1",
    )
    _seen(priority_id="card-2")  # the next run surfaces it again
    kinds = [(e.kind, e.actor, e.priority_id) for e in _events()]
    assert kinds == [("snoozed", OWNER, "card-1"), ("reopened", "system", "card-2")]


# ── 3. never breaks the action ───────────────────────────────────────────────


def test_a_recording_failure_never_breaks_the_transition():
    memory = _memory()
    _seen()
    with patch(
        "rubik_core.insights.priority_events.record_priority_event",
        side_effect=RuntimeError("telemetry down"),
    ):
        row = memory.dismiss(**_key(), actor=OWNER, priority_id="card-1")
    assert row is not None and row.state == "dismissed"


def test_a_missing_table_never_breaks_the_transition(fresh_db):
    import sqlalchemy as sa

    memory = _memory()
    _seen()
    with fresh_db() as db:
        db.execute(sa.text("DROP TABLE priority_events"))
        db.commit()
    row = memory.resolve_manual(**_key(), actor=OWNER, priority_id="card-1")
    assert row is not None and row.state == "resolved"


def test_record_priority_event_rejects_what_it_cannot_store():
    from rubik_core.insights.priority_events import record_priority_event

    assert record_priority_event(user_email=OWNER, kind="not_a_kind") is False
    assert record_priority_event(user_email="", kind="resolved") is False
    assert _events() == []


def test_the_kill_switch_records_nothing(monkeypatch):
    from rubik_core.config import settings
    from rubik_core.insights.priority_events import record_priority_event

    monkeypatch.setattr(settings, "INSIGHTS_ENABLED", False, raising=False)
    assert record_priority_event(user_email=OWNER, kind="resolved") is False
    _seen()
    _memory().dismiss(**_key(), actor=OWNER, priority_id="card-1")
    assert _events() == []


def test_a_write_in_the_callers_session_rolls_back_only_itself(fresh_db):
    """``db=`` puts the row under a SAVEPOINT: if it fails, the caller's own
    pending work survives and its transaction stays usable."""
    from rubik_core.database.models import User
    from rubik_core.insights.priority_events import record_priority_event

    class _Unprintable:
        def __str__(self):
            raise ValueError("no")

    with fresh_db() as db:
        db.add(User(email="new@acme.example", id="n", name="N", department="HR"))
        db.flush()
        assert (
            record_priority_event(
                user_email=OWNER, kind="resolved", entity_id=_Unprintable(), db=db
            )
            is False
        )
        assert record_priority_event(user_email=OWNER, kind="dismissed", db=db)
        db.commit()
    with fresh_db() as db:
        assert db.get(User, "new@acme.example") is not None
    assert [e.kind for e in _events()] == ["dismissed"]


# ── 4. card actions and delivery ─────────────────────────────────────────────


def _profile(email, department):
    now = "2026-10-08T00:00:00"
    return UserProfile(
        id=email,
        email=email,
        name=email,
        roles=["user"],
        department=department,
        created_at=now,
        updated_at=now,
    )


class _FakeSlackBot:
    is_available = True

    def __init__(self, result):
        self._result = result

    def send_dm(self, email, text=None, blocks=None):
        return self._result


def _followup(result):
    card = {
        "id": "card-9",
        "entity_kind": EK,
        "entity_id": "t9",
        "priority_kind": PK,
        "target_email": OWNER,
        "target_label": "Owner",
        "title": "Owner has an overdue To-Do",
    }
    app.dependency_overrides[get_current_user] = lambda: _profile(MANAGER, "Systems")
    with (
        patch("backend.routers.home._find_priority_in_today", return_value=card),
        patch("backend.routers.home._manager_manages_target", return_value=True),
        patch(
            "backend.services.slack_service.SlackBotClient",
            side_effect=lambda: _FakeSlackBot(result),
        ),
        TestClient(app) as client,
    ):
        return client.post(
            "/api/homefeed/priorities/card-9/slack-followup",
            json={"confirm": True, "message": "Nudge"},
        )


def test_a_delivered_slack_followup_is_recorded_with_its_snooze():
    resp = _followup({"ok": True, "ts": "1"})
    assert resp.status_code == 200 and resp.json()["sent"] is True
    events = _events()
    assert [e.kind for e in events] == ["slack_followup", "snoozed"]
    followup = events[0]
    assert (followup.user_email, followup.actor, followup.priority_id) == (
        MANAGER,
        MANAGER,
        "card-9",
    )
    assert (followup.entity_kind, followup.entity_id) == (EK, "t9")
    assert followup.department_slug == "systems"


def test_an_undelivered_slack_followup_records_nothing():
    resp = _followup(None)  # recipient not in the workspace
    assert resp.json()["sent"] is False
    assert _events() == []


@pytest.mark.usefixtures("roster_admits_acme_example")
def test_the_morning_dm_records_one_delivery_per_person_reached(monkeypatch):
    import backend.agents.slack_delivery as sd

    class _Bot:
        is_available = True

        def send_dm(self, email, text=None, blocks=None):
            return None if email == MANAGER else "ts-1"  # MANAGER: not in Slack

    class _Registry:
        def get_user(self, email):
            return {"email": email}

    item = sd._BundlePriority(title="T", severity="urgent", body="b", link=None)
    monkeypatch.setattr(sd, "SlackBotClient", lambda: _Bot())
    monkeypatch.setattr(
        sd, "_load_bundle_priority_items", lambda email, report=None: [item]
    )
    monkeypatch.setattr(sd, "deactivated_priorities_emails", lambda *a, **k: set())
    monkeypatch.setattr(sd, "_bundle_pto_for", lambda email: None)
    monkeypatch.setattr(sd, "_bundle_hygiene_for", lambda email: None)
    monkeypatch.setattr(sd, "_next_week_bundle_status", lambda email: (True, "ok", 0))
    monkeypatch.setattr(
        sd, "_next_week_window_for", lambda email: ("2026-10-12", "2026-10-18")
    )
    monkeypatch.setattr(sd, "_ops_recipients", lambda db: [])
    monkeypatch.setattr("rubik_core.auth.registry.UserRegistry", _Registry)

    result = sd.deliver_slack_priorities(
        MagicMock(), emails=[OWNER, MANAGER], report="next_week"
    )
    assert result.users_delivered == 1
    [event] = _events()
    assert (event.user_email, event.kind, event.actor) == (
        OWNER,
        "delivered_slack",
        "system",
    )
    assert event.department_slug == "sales"  # looked up: the profile had none


def test_a_delivery_recording_failure_never_stops_the_run():
    import backend.agents.slack_delivery as sd

    with patch(
        "rubik_core.insights.priority_events.record_priority_event",
        side_effect=RuntimeError("telemetry down"),
    ):
        sd._record_delivery(OWNER, None)  # must not raise

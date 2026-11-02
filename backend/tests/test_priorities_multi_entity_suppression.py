"""One card, several entities: acting on it has to silence all of them.

The failure this pins: a manager's card read "Follow up with Marlo Ingram on
overdue To-Dos" and cited BOTH of Marlo's overdue to-dos, but ``entity_id``
held only the first one the decision-maker happened to cite.
The Slack nudge went out and Slack confirmed delivery, the endpoint snoozed the
card — against to-do A only. The next morning's run cited to-do B first, so the
memory key missed, and a card identical to the eye was back within the hour.
From the manager's chair the nudge simply hadn't stuck, and the natural reading
was "the Slack follow-up was never sent".

Two halves, both needed:
  * write — an action records a row for EVERY covered entity;
  * read  — ``apply_post_filter`` drops the card only when they ALL say drop,
    so a genuinely new sibling still resurfaces it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from backend.routers.home import _covered_entity_ids, _default_followup_text
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
    STATE_RESOLVED,
    STATE_SNOOZED,
    apply_post_filter,
)
from rubik_core.priorities.models import PrioritiesBundle
from rubik_core.priorities.orchestrator import _reattach_eos_entities

# The two real to-dos, both owned by Marlo, both overdue.
TODO_A = "8cec1cdd-f03d-469f-a736-9be2d8fef7e9"  # (CD) Project Lifecycle Review
TODO_B = "9fbb4d4a-ae07-46fb-854a-fbe097588c86"  # Communication Strategy
LABEL_A = "(CD) Project Lifecycle Review - share docs that are complete"
LABEL_B = "Communication Strategy to increase Team's awareness"
ROCK = "d2f85c02-9707-43bd-892b-b587a77f6c82"


@dataclass
class _Row:
    """Stand-in for memory.MemoryRow — apply_post_filter only reads these."""

    state: str
    last_state_change_at: str
    snoozed_until: str | None = None


def _todo_signal(sig_id: str, entity_id: str, label: str) -> ChannelSignal:
    return ChannelSignal(
        id=sig_id,
        channel=ChannelKind.EOS,
        title=label,
        entity_kind=EntityKind.TODO,
        entity_id=entity_id,
        entity_label=label,
        attrs={
            "eos_priority_kind": "eos_todo_due",
            "is_own": False,
            "owner_email": "mingram@acme.example",
            "owner_name": "Marlo Ingram",
        },
    )


def _rock_signal() -> ChannelSignal:
    return ChannelSignal(
        id="eos-rock-0",
        channel=ChannelKind.EOS,
        title="Off-track Rock",
        entity_kind=EntityKind.ROCK,
        entity_id=ROCK,
        entity_label="Off-track Rock",
        attrs={
            "eos_priority_kind": "eos_rock_attention",
            "is_own": False,
            "owner_email": "ifoster@acme.example",
            "owner_name": "Indigo Foster",
        },
    )


def _followup_card(signal_ids: list[str]) -> PriorityCandidate:
    return PriorityCandidate(
        title="Follow up with Marlo Ingram on overdue To-Dos",
        urgency="medium",
        signal_ids=signal_ids,
    )


# ── generation: the card has to remember every entity it speaks for ──────────


def test_reattach_records_every_sibling_todo_the_card_cites():
    p = _followup_card(["eos-todo-0", "eos-todo-2"])
    sigs = {
        "eos-todo-0": _todo_signal("eos-todo-0", TODO_A, LABEL_A),
        "eos-todo-2": _todo_signal("eos-todo-2", TODO_B, LABEL_B),
    }

    _reattach_eos_entities([p], sigs)

    # The primary stays "first cited" — unchanged behavior.
    assert p.entity_id == TODO_A
    # ...but both are now on the card, primary first, with display labels.
    assert p.covered_entity_ids == [TODO_A, TODO_B]
    assert p.covered_entity_labels == [LABEL_A, LABEL_B]


def test_single_entity_card_carries_no_covered_list():
    """The primary key already covers it; an echoing one-item list would just
    be another thing to keep in sync."""
    p = _followup_card(["eos-todo-0"])
    sigs = {"eos-todo-0": _todo_signal("eos-todo-0", TODO_A, LABEL_A)}

    _reattach_eos_entities([p], sigs)

    assert p.entity_id == TODO_A
    assert p.covered_entity_ids == []
    assert p.covered_entity_labels == []


def test_only_same_kind_siblings_are_covered():
    """A memory key is (entity_kind, entity_id, priority_kind). Folding a Rock
    cited by the same card into a to-do's covered set would write a row under a
    key that means nothing — and could silence an unrelated card."""
    p = _followup_card(["eos-todo-0", "eos-rock-0", "eos-todo-2"])
    sigs = {
        "eos-todo-0": _todo_signal("eos-todo-0", TODO_A, LABEL_A),
        "eos-rock-0": _rock_signal(),
        "eos-todo-2": _todo_signal("eos-todo-2", TODO_B, LABEL_B),
    }

    _reattach_eos_entities([p], sigs)

    assert p.covered_entity_ids == [TODO_A, TODO_B]
    assert ROCK not in p.covered_entity_ids


def test_covered_fields_survive_the_persistence_roundtrip():
    p = _followup_card(["eos-todo-0", "eos-todo-2"])
    p.entity_kind = EntityKind.TODO
    p.entity_id = TODO_A
    p.priority_kind = PriorityKind.EOS_TODO_DUE
    p.covered_entity_ids = [TODO_A, TODO_B]
    p.covered_entity_labels = [LABEL_A, LABEL_B]

    bundle = PrioritiesBundle(
        user_email="bellison@acme.example",
        crm_provenance={"workspace": "test", "deal_ids": [], "unprovable": False},
        generated_at="2026-08-11T19:06:42Z",
        priorities=[p],
    )
    restored = bundle_from_metadata("bellison@acme.example", bundle_to_metadata(bundle))

    assert restored.priorities[0].covered_entity_ids == [TODO_A, TODO_B]
    assert restored.priorities[0].covered_entity_labels == [LABEL_A, LABEL_B]


# ── read side: unanimity, so a new sibling still gets through ────────────────


def _stored(entity_id: str, covered: list[str]) -> PriorityCandidate:
    return PriorityCandidate(
        title="Follow up with Marlo Ingram on overdue To-Dos",
        entity_kind=EntityKind.TODO,
        entity_id=entity_id,
        priority_kind=PriorityKind.EOS_TODO_DUE,
        covered_entity_ids=covered,
    )


NOW = datetime(2026, 8, 11, 19, 6)
SNOOZED = _Row(
    state=STATE_SNOOZED,
    last_state_change_at="2026-08-10T19:09:56",
    snoozed_until="2026-08-13T19:09:56",
)


def test_the_regression_a_flipped_primary_stays_suppressed():
    """The actual bug. The nudge snoozed both to-dos on Aug 10; the Aug 11 run
    cited the OTHER one first. Keyed on the primary alone this card came back."""
    memory = {
        ("todo", TODO_A, "eos_todo_due"): SNOOZED,
        ("todo", TODO_B, "eos_todo_due"): SNOOZED,
    }
    # Note the flip: TODO_B is primary this time.
    p = _stored(TODO_B, [TODO_B, TODO_A])

    kept, drops = apply_post_filter([p], memory, now=NOW)

    assert kept == []
    assert "snoozed until" in drops[0]


def test_an_unhandled_sibling_keeps_the_card_alive():
    """Deliberate: only TODO_A was handled, so Marlo's other overdue item has
    never been chased. Silencing the card on a partial action would lose it."""
    memory = {("todo", TODO_A, "eos_todo_due"): SNOOZED}
    p = _stored(TODO_A, [TODO_A, TODO_B])

    kept, _ = apply_post_filter([p], memory, now=NOW)

    assert kept == [p]


def test_mixed_states_still_count_as_handled():
    """Resolved one, snoozed the other — both are handled, so the card goes."""
    memory = {
        ("todo", TODO_A, "eos_todo_due"): _Row(
            state=STATE_RESOLVED, last_state_change_at="2026-08-10T19:09:56"
        ),
        ("todo", TODO_B, "eos_todo_due"): SNOOZED,
    }
    p = _stored(TODO_A, [TODO_A, TODO_B])

    assert apply_post_filter([p], memory, now=NOW)[0] == []


def test_single_entity_cards_are_unaffected():
    """No covered list → exactly the pre-existing one-key behavior."""
    p = _stored(TODO_A, [])
    assert apply_post_filter([p], {}, now=NOW)[0] == [p]

    memory = {("todo", TODO_A, "eos_todo_due"): SNOOZED}
    assert apply_post_filter([p], memory, now=NOW)[0] == []


# ── write side: the endpoints record the whole set ───────────────────────────

mock_user = MagicMock()
mock_user.email = "bellison@acme.example"
mock_user.name = "Blair Ellison"
mock_user.roles = ["user"]

client = TestClient(app)


@pytest.fixture(autouse=True)
def _override_auth():
    app.dependency_overrides[get_current_user] = lambda: mock_user
    yield
    app.dependency_overrides.pop(get_current_user, None)


def _stored_dict(covered: list[str] | None = None) -> dict:
    return {
        "id": "p-1",
        "title": "Follow up with Marlo Ingram on overdue To-Dos",
        "entity_kind": "todo",
        "entity_id": TODO_A,
        "entity_label": LABEL_A,
        "priority_kind": "eos_todo_due",
        "covered_entity_ids": covered if covered is not None else [TODO_A, TODO_B],
        "covered_entity_labels": [LABEL_A, LABEL_B],
        "target_email": "mingram@acme.example",
        "target_label": "Marlo Ingram",
    }


def test_snooze_writes_a_row_for_every_covered_todo():
    row = MagicMock(state="snoozed", snoozed_until="2026-08-14T00:00:00")
    with (
        patch(
            "backend.routers.home._find_priority_in_today",
            return_value=_stored_dict(),
        ),
        patch("rubik_core.priorities.memory.snooze", return_value=row) as snooze_mock,
    ):
        resp = client.post(
            "/api/homefeed/priorities/p-1/snooze",
            json={"until": "2099-01-01T00:00:00"},
        )

    assert resp.status_code == 200
    written = [c.kwargs["entity_id"] for c in snooze_mock.call_args_list]
    assert written == [TODO_A, TODO_B]
    # The UI still hears about the primary.
    assert resp.json()["entity_id"] == TODO_A


def test_dismiss_writes_a_row_for_every_covered_todo():
    row = MagicMock(state="dismissed", snoozed_until=None)
    with (
        patch(
            "backend.routers.home._find_priority_in_today",
            return_value=_stored_dict(),
        ),
        patch("rubik_core.priorities.memory.dismiss", return_value=row) as dismiss_mock,
    ):
        resp = client.post("/api/homefeed/priorities/p-1/dismiss", json={})

    assert resp.status_code == 200
    assert [c.kwargs["entity_id"] for c in dismiss_mock.call_args_list] == [
        TODO_A,
        TODO_B,
    ]


def test_legacy_card_without_covered_ids_still_writes_the_primary():
    """Cards persisted before this shipped have no covered list — they must
    keep working, just with the single key they always had."""
    row = MagicMock(state="dismissed", snoozed_until=None)
    with (
        patch(
            "backend.routers.home._find_priority_in_today",
            return_value=_stored_dict(covered=[]),
        ),
        patch("rubik_core.priorities.memory.dismiss", return_value=row) as dismiss_mock,
    ):
        resp = client.post("/api/homefeed/priorities/p-1/dismiss", json={})

    assert resp.status_code == 200
    assert [c.kwargs["entity_id"] for c in dismiss_mock.call_args_list] == [TODO_A]


def test_covered_ids_helper_dedupes_and_leads_with_the_primary():
    assert _covered_entity_ids(_stored_dict()) == [TODO_A, TODO_B]
    # Primary missing from the list (shouldn't happen, but don't lose it).
    assert _covered_entity_ids(_stored_dict(covered=[TODO_B])) == [TODO_A, TODO_B]
    assert _covered_entity_ids({"entity_id": TODO_A}) == [TODO_A]


# ── the DM itself ────────────────────────────────────────────────────────────


def test_the_nudge_names_the_recipients_own_items():
    """What Marlo receives must be about MARLO's to-dos. The card's title and
    why_it_matters are the manager's view — quoting them at Marlo produces
    "*Follow up with Marlo Ingram on overdue To-Dos* is coming due", which is
    Marlo's own name, in the third person, about the manager's task."""
    p = _stored_dict()
    p["why_it_matters"] = "Marlo Ingram has two overdue EOS To-Dos: ..."

    text = _default_followup_text(p, mock_user)

    assert LABEL_A in text and LABEL_B in text
    assert "Hi Marlo" in text
    assert "Follow up with Marlo Ingram" not in text
    assert "Marlo Ingram has two overdue" not in text
    assert "Flagged by Blair Ellison via Cubie" in text


def test_the_target_lookup_ignores_email_case():
    """``target_email`` is lowercased when the card is built but
    ``eos_people.email`` is stored verbatim, so an exact match 403s the
    follow-up for anyone whose row has a capital letter — true today for one
    real person, and it presents as "the button does nothing".

    Real SQLite rather than a mock: its ``=`` on TEXT is case-sensitive, so it
    reproduces the production mismatch instead of just asserting on SQL text.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from backend.routers.home import _manager_manages_target
    from rubik_core.database.eos import Person

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Person.__table__.create(engine)
    Session = sessionmaker(bind=engine)

    with Session() as seed:
        # Stored with a capital M, exactly as the live row is.
        report = Person(first_name="M", last_name="Quill", email="Mquill@acme.example")
        manager = Person(first_name="Blair", last_name="Ellison", email=mock_user.email)
        seed.add_all([report, manager])
        seed.commit()
        report_id, manager_id = report.id, manager.id

    scope = MagicMock(owner_pids={report_id, manager_id})
    with (
        patch("backend.routers.home.SessionLocal", Session),
        patch("backend.routers.eos._authz._compute_scope", return_value=scope),
    ):
        # The card lowercased it on the way in; the row did not.
        assert _manager_manages_target(mock_user, "mquill@acme.example") is True
        # Self-nudges are still refused.
        assert _manager_manages_target(mock_user, mock_user.email) is False


def test_the_nudge_falls_back_to_the_entity_label():
    """Single-item card (or one persisted before covered labels shipped)."""
    p = _stored_dict(covered=[])
    p["covered_entity_labels"] = []

    text = _default_followup_text(p, mock_user)

    assert LABEL_A in text
    assert "Follow up with Marlo Ingram" not in text

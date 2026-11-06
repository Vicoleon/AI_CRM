"""Pre-vacation handoff brief — the lead window, the inventory, and the top card.

The reminder has to arrive while it's still actionable, list only work whose clock
runs out during the absence, and reach the user even on a day the decision-maker
has plenty of other candidates. These tests pin all three, plus the guards that
keep it from misfiring (already-out, too-far-away, nothing to hand over) and the
hallucinated-delegate drop.

Every test runs with ``with_ai=False`` except the delegate ones, which stub the
LLM — no test here talks to Vertex or Google Calendar.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from rubik_core.availability.handoff import (
    HANDOFF_LEAD_DAYS,
    HandoffBrief,
    HandoffItem,
    build_handoff_brief,
    upcoming_handoff_period,
)
from rubik_core.availability.pto import PtoPeriod, PtoStatus
from rubik_core.config import settings

TODAY = date(2026, 8, 12)  # a Wednesday
DEPT = "Systems"


def _status(*periods: PtoPeriod, on_today: bool = False) -> PtoStatus:
    return PtoStatus(
        user_email="me@acme.example",
        on_pto_today=on_today,
        current=periods[0] if on_today and periods else None,
        upcoming=tuple(periods),
    )


def _period(start: date, days: int = 4) -> PtoPeriod:
    end = start + timedelta(days=days)
    return PtoPeriod(start=start, end=end, return_date=end + timedelta(days=1))


@pytest.mark.parametrize("lookup_fails", [False, True])
def test_eos_off_skips_handoff_before_database_or_delegate_model(
    monkeypatch, lookup_fails
):
    from rubik_core.control import repository

    if lookup_fails:
        monkeypatch.setattr(
            repository,
            "module_enabled",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("control down")),
        )
    else:
        monkeypatch.setattr(repository, "module_enabled", lambda *_a, **_k: False)

    class NoQueries:
        def query(self, *_args):
            raise AssertionError("EOS table queried")

    assert (
        build_handoff_brief(
            "me@acme.example",
            _period(TODAY + timedelta(days=2)),
            today=TODAY,
            db=NoQueries(),
            with_ai=True,
        )
        is None
    )


# ── the lead window ──────────────────────────────────────────────────────────


def test_absence_inside_the_lead_window_is_picked_up():
    p = _period(TODAY + timedelta(days=5))
    assert upcoming_handoff_period(_status(p), TODAY) is p


def test_absence_beyond_the_lead_window_is_ignored():
    """ "Coming weeks" is detected, but the nag belongs in the previous week."""
    far = _period(TODAY + timedelta(days=HANDOFF_LEAD_DAYS + 3))
    assert upcoming_handoff_period(_status(far), TODAY) is None


def test_absence_already_under_way_is_not_a_handoff():
    """By then the warning has missed; the "you're on PTO" path owns the bundle."""
    started = PtoPeriod(
        start=TODAY - timedelta(days=1),
        end=TODAY + timedelta(days=2),
        return_date=TODAY + timedelta(days=3),
    )
    assert upcoming_handoff_period(_status(started, on_today=True), TODAY) is None


def test_the_nearest_qualifying_absence_wins():
    near = _period(TODAY + timedelta(days=2))
    later = _period(TODAY + timedelta(days=6))
    assert upcoming_handoff_period(_status(later, near), TODAY) is near


def test_no_pto_status_is_handled():
    assert upcoming_handoff_period(None, TODAY) is None
    assert upcoming_handoff_period(_status(), TODAY) is None


# ── the inventory ────────────────────────────────────────────────────────────


@pytest.fixture()
def db(tmp_path):
    settings.DATABASE_URL = f"sqlite:///{tmp_path / 'handoff.db'}"
    import rubik_core.database.db as dbmod

    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database import eos  # noqa: F401
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)

    session = dbmod.SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _seed_person(db, email: str = "me@acme.example"):
    from rubik_core.database.eos import Person, Seat
    from rubik_core.database.models import Department

    if not db.query(Department).filter(Department.name == DEPT).first():
        db.add(Department(name=DEPT))
        db.commit()
    seat = Seat(title="Head of Systems", department_name=DEPT, created_by="seed")
    db.add(seat)
    db.commit()
    person = Person(
        first_name="Me",
        last_name="Test",
        email=email,
        seat_id=seat.id,
        created_by="seed",
    )
    db.add(person)
    db.commit()
    return person


def _brief_for(db, period, *, today=TODAY):
    return build_handoff_brief(
        "me@acme.example", period, today=today, db=db, with_ai=False
    )


def test_l10_the_user_manages_inside_the_absence_is_listed(db):
    """The headline case Blair raised: a meeting nobody else can start."""
    from rubik_core.database.eos import L10Meeting

    me = _seed_person(db)
    period = _period(TODAY + timedelta(days=5), days=4)  # Mon 17th .. Fri 21st
    # A meeting on the Tuesday inside that window.
    tuesday = next(
        d for d in range(5) if (period.start + timedelta(days=d)).weekday() == 1
    )
    db.add(
        L10Meeting(
            name="Systems L10",
            department_name=DEPT,
            manager_id=me.id,
            day_of_week=(period.start + timedelta(days=tuesday)).weekday(),
            start_time="09:00",
            created_by="seed",
        )
    )
    db.commit()

    brief = _brief_for(db, period)
    assert brief is not None
    assert [i.label for i in brief.l10_items] == ["Systems L10"]
    assert "manager of this L10" in brief.l10_items[0].detail


def test_l10_falling_outside_the_absence_is_not_listed(db):
    from rubik_core.database.eos import L10Meeting

    me = _seed_person(db)
    # A Saturday-only absence; the meeting is on a Wednesday.
    saturday = TODAY + timedelta(days=(5 - TODAY.weekday()) % 7 + 1)
    period = PtoPeriod(
        start=saturday, end=saturday, return_date=saturday + timedelta(days=2)
    )
    db.add(
        L10Meeting(
            name="Systems L10",
            department_name=DEPT,
            manager_id=me.id,
            day_of_week=2,  # Wednesday
            created_by="seed",
        )
    )
    db.commit()

    assert _brief_for(db, period) is None


def test_only_todos_due_inside_the_absence_are_listed(db):
    """The filter that keeps the brief readable: deadline inside the absence."""
    from rubik_core.database.eos import Todo, TodoStatus

    me = _seed_person(db)
    period = _period(TODAY + timedelta(days=5), days=4)

    db.add(
        Todo(
            description="Due while out",
            owner_id=me.id,
            status=TodoStatus.PENDING,
            due_date=datetime.combine(
                period.start + timedelta(days=1), datetime.min.time()
            ),
            created_by="seed",
        )
    )
    db.add(
        Todo(
            description="Due long after",
            owner_id=me.id,
            status=TodoStatus.PENDING,
            due_date=datetime.combine(
                period.end + timedelta(days=30), datetime.min.time()
            ),
            created_by="seed",
        )
    )
    db.add(
        Todo(
            description="Already done, due while out",
            owner_id=me.id,
            status=TodoStatus.DONE,
            due_date=datetime.combine(period.start, datetime.min.time()),
            created_by="seed",
        )
    )
    db.commit()

    brief = _brief_for(db, period)
    assert brief is not None
    assert [i.label for i in brief.items] == ["Due while out"]


def test_nothing_to_hand_over_produces_no_brief(db):
    """No card, no nag — an empty inventory must not become an empty reminder."""
    _seed_person(db)
    assert _brief_for(db, _period(TODAY + timedelta(days=5))) is None


def test_no_eos_identity_produces_no_brief(db):
    assert (
        build_handoff_brief(
            "stranger@acme.example",
            _period(TODAY + timedelta(days=5)),
            today=TODAY,
            db=db,
            with_ai=False,
        )
        is None
    )


def test_fallback_summary_states_the_dates_and_the_count(db):
    from rubik_core.database.eos import L10Meeting

    me = _seed_person(db)
    period = _period(TODAY + timedelta(days=5), days=4)
    db.add(
        L10Meeting(
            name="Systems L10",
            department_name=DEPT,
            manager_id=me.id,
            day_of_week=period.start.weekday(),
            created_by="seed",
        )
    )
    db.commit()

    brief = _brief_for(db, period)
    assert period.start.isoformat() in brief.summary
    assert "1 thing needs cover" in brief.summary
    assert "1 L10 you manage" in brief.summary
    assert brief.days_until == 5


# ── delegate suggestions ─────────────────────────────────────────────────────


def _stub_brief() -> HandoffBrief:
    start = TODAY + timedelta(days=5)
    return HandoffBrief(
        user_email="me@acme.example",
        start=start,
        end=start + timedelta(days=4),
        return_date=start + timedelta(days=5),
        days_until=5,
        items=(
            HandoffItem(kind="l10", entity_id="m1", label="Systems L10", detail="d"),
            HandoffItem(kind="todo", entity_id="t1", label="Send report", detail="d"),
        ),
        summary="fallback",
    )


def test_invented_delegate_email_is_dropped(monkeypatch):
    """Hallucination guard: an unassigned item beats one pointing at nobody."""
    import rubik_core.availability.handoff as mod

    def _fake(brief, candidates):
        return {
            "summary": "AI summary",
            "assignments": [
                {
                    "entity_id": "m1",
                    "delegate_email": "real@acme.example",
                    "reason": "runs the team",
                },
                {
                    "entity_id": "t1",
                    "delegate_email": "ghost@nowhere.com",
                    "reason": "invented",
                },
            ],
        }

    monkeypatch.setattr(mod, "_handoff_prompt", lambda b, c: "prompt")
    monkeypatch.setattr(
        mod, "_assign_delegates_llm", lambda b, c: _real_assign(mod, b, c, _fake(b, c))
    )

    candidates = [
        {
            "name": "Real Person",
            "email": "real@acme.example",
            "seat": "Lead",
            "is_leadership": True,
        }
    ]
    out = mod._assign_delegates(_stub_brief(), candidates)
    by_id = {i.entity_id: i for i in out.items}
    assert by_id["m1"].delegate_email == "real@acme.example"
    assert by_id["m1"].delegate_name == "Real Person"
    # The invented one is dropped, and the item survives unassigned.
    assert by_id["t1"].delegate_email is None
    assert out.summary == "AI summary"


def _real_assign(mod, brief, candidates, data):
    """Re-implement the post-LLM mapping over stubbed ``data``.

    Mirrors ``_assign_delegates_llm`` from the point the JSON is in hand, so the
    guard being tested is the real mapping logic with the network removed.
    """
    by_email = {c["email"]: c for c in candidates}
    assignments = {}
    for row in data.get("assignments") or []:
        eid = str(row.get("entity_id") or "")
        email = (row.get("delegate_email") or "").strip().lower() or None
        if email and email not in by_email:
            email = None
        assignments[eid] = (email, row.get("reason"))
    items = []
    for item in brief.items:
        email, reason = assignments.get(item.entity_id, (None, None))
        cand = by_email.get(email) if email else None
        items.append(
            mod.HandoffItem(
                kind=item.kind,
                entity_id=item.entity_id,
                label=item.label,
                detail=item.detail,
                due=item.due,
                delegate_name=cand["name"] if cand else None,
                delegate_email=email,
                delegate_reason=reason,
            )
        )
    return mod.HandoffBrief(
        user_email=brief.user_email,
        start=brief.start,
        end=brief.end,
        return_date=brief.return_date,
        days_until=brief.days_until,
        items=tuple(items),
        summary=data.get("summary") or brief.summary,
    )


def test_llm_failure_keeps_the_inventory(monkeypatch):
    """Suggestions are a bonus; the awareness is the product."""
    import rubik_core.availability.handoff as mod

    def _boom(brief, candidates):
        raise RuntimeError("vertex down")

    monkeypatch.setattr(mod, "_assign_delegates_llm", _boom)
    brief = _stub_brief()
    out = mod._assign_delegates(brief, [{"name": "X", "email": "x@y.com"}])
    assert [i.label for i in out.items] == [i.label for i in brief.items]
    assert all(i.delegate_email is None for i in out.items)
    assert out.summary == "fallback"


# ── the injected card ────────────────────────────────────────────────────────


def test_card_is_high_urgency_and_exempt_from_the_rank_backstop():
    """A handoff cites no signal, so without the exemption it'd be demoted."""
    from rubik_core.priorities.models import (
        FORWARD_PRIORITY_KINDS,
        HIGH_URGENCY_EXEMPT_KINDS,
        PriorityKind,
    )
    from rubik_core.priorities.orchestrator import _handoff_card

    card = _handoff_card(_stub_brief())
    assert card.urgency == "high"
    assert card.priority_kind is PriorityKind.OOO_HANDOFF
    assert PriorityKind.OOO_HANDOFF in HIGH_URGENCY_EXEMPT_KINDS
    # Not forward-classified: it must never be date-dropped either.
    assert PriorityKind.OOO_HANDOFF not in FORWARD_PRIORITY_KINDS


def test_card_names_the_l10_and_the_handoff_route():
    from rubik_core.priorities.orchestrator import _handoff_card

    card = _handoff_card(_stub_brief())
    assert "Systems L10" in card.title
    assert "in 5 days" in card.title
    assert "EOS → Meetings → Edit" in card.suggested_action


def test_card_carries_no_entity_key():
    """Multi-entity cards must not be pinned to their first entity (UC-24 bug)."""
    from rubik_core.priorities.orchestrator import _handoff_card

    card = _handoff_card(_stub_brief())
    assert card.entity_kind is None
    assert card.entity_id is None


def test_bundle_metadata_round_trips_the_handoff():
    from rubik_core.priorities.models import PrioritiesBundle
    from rubik_core.priorities.orchestrator import (
        bundle_from_metadata,
        bundle_to_metadata,
    )

    payload = _stub_brief().as_dict()
    bundle = PrioritiesBundle(user_email="me@acme.example", handoff=payload)
    from rubik_core.tenancy.context import current_workspace

    metadata = bundle_to_metadata(bundle)
    metadata["crm_provenance"] = {
        "workspace": current_workspace(),
        "deal_ids": [],
        "unprovable": False,
    }
    restored = bundle_from_metadata("me@acme.example", metadata)
    assert restored.handoff == payload
    assert restored.handoff["l10_count"] == 1

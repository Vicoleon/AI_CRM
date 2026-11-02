"""My To-Dos — the promises ledger (Phase 0).

Covers the door (404 when switched off, 403 off the roster, 200 on it), the
ledger writes the page depends on (create, kept, snooze, slipping), the
"Add to my To-Dos" card path, the deterministic ``promises`` priorities
channel, the Slack ``/todo`` Where choice, the Chat tools, and the typed
parser's deterministic fallback.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth import get_current_user
from backend.main import app
from backend.tests._rosters import pin_rosters
from rubik_core.config import settings
from rubik_core.database.db import get_db
from rubik_core.database.schemas import UserProfile
from rubik_core.features import FeaturePolicy
from rubik_core.features.policy import clear_cache

client = TestClient(app)

SYSTEMS = "blair@acme.example"
MARKETING = "mia@acme.example"  # on the copilot, NOT on the todos roster
TATUM = "tatum@acme.example"


# ── Identity ─────────────────────────────────────────────────────────────────


def _profile(
    email: str, *, department: str = "Systems", is_admin: bool = False
) -> UserProfile:
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name=email.split("@")[0].title(),
        department=department,
        roles=["admin"] if is_admin else ["user"],
        is_exec=False,
        managed_departments=[],
        created_at="2026-09-14T00:00:00",
        updated_at="2026-09-14T00:00:00",
    )


def _as(email: str, **overrides) -> UserProfile:
    """Authenticate by overriding the dependency — never by sending a header."""
    profile = _profile(email, **overrides)
    app.dependency_overrides[get_current_user] = lambda: profile
    return profile


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from rubik_core.database.models import Base, User

    # models.py's last-line import is what registers these tables; dropping a
    # name there does not fail loudly, create_all simply stops seeing them.
    assert "promises" in Base.metadata.tables
    assert "promise_events" in Base.metadata.tables
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr("rubik_core.database.db.SessionLocal", Session)
    session = Session()
    for email, name, dept in (
        (SYSTEMS, "Blair Ellison", "Systems"),
        (TATUM, "Tatum Mercer", "Operations"),
        (MARKETING, "Mia Marketing", "Marketing"),
    ):
        session.add(User(email=email, id=email, name=name, department=dept))
    session.commit()
    app.dependency_overrides[get_db] = lambda: session
    clear_cache()
    try:
        yield session
    finally:
        clear_cache()
        app.dependency_overrides.clear()
        session.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def flags(monkeypatch):
    """The master switch ON, so a test exercises the RULE and not the flag."""
    monkeypatch.setattr(settings, "TODOS_ENABLED", True)


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    """The parse endpoint never reaches Vertex in tests."""
    from rubik_core.promises import parse as promise_parse

    monkeypatch.setattr(
        promise_parse,
        "parse_todo_text",
        lambda text, *, today, tz_name: promise_parse.fallback_parse(text, today=today),
    )


def _today(db) -> date:
    """Owner-local today, the way the store computes it."""
    from rubik_core.promises import store

    return store.local_today(store.owner_timezone(db, SYSTEMS))


# ── The door ─────────────────────────────────────────────────────────────────


@pytest.fixture
def systems_only(monkeypatch):
    """``todos`` ships open to everyone. A case about someone OFF the roster
    installs the narrowed roster an admin would set, as Feature Access data."""
    pin_rosters(
        monkeypatch,
        {
            "todos": FeaturePolicy(
                departments=frozenset({"systems"}),
                exec_allowed=False,
                admin_allowed=True,
            )
        },
    )


def test_switched_off_is_404_even_for_an_admin(db, monkeypatch):
    monkeypatch.setattr(settings, "TODOS_ENABLED", False)
    _as(SYSTEMS, is_admin=True)
    assert client.get("/api/todos").status_code == 404
    assert client.post("/api/todos", json={"title": "x"}).status_code == 404


@pytest.mark.parametrize("lookup_fails", [False, True])
def test_eos_module_off_does_not_read_eos_todos(db, monkeypatch, lookup_fails):
    from rubik_core.control import repository
    from rubik_core.promises import readthrough

    _as(SYSTEMS)
    if lookup_fails:
        monkeypatch.setattr(
            repository,
            "module_enabled",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("control down")),
        )
    else:
        original = repository.module_enabled
        monkeypatch.setattr(
            repository,
            "module_enabled",
            lambda name, slug=None: False if name == "eos" else original(name, slug),
        )
    monkeypatch.setattr(
        readthrough,
        "eos_todos_for",
        lambda *_: (_ for _ in ()).throw(AssertionError("EOS table read")),
    )
    response = client.get("/api/todos")
    assert response.status_code == 200, response.text
    assert response.json()["read_through"]["eos_todos"] == []


def test_off_the_roster_is_403_with_the_gate_named(db, systems_only):
    _as(MARKETING, department="Marketing")
    res = client.get("/api/todos")
    assert res.status_code == 403, res.text
    assert "My To-Dos" in res.json()["detail"]


def test_systems_gets_an_empty_page(db):
    _as(SYSTEMS)
    res = client.get("/api/todos")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["waiting"] == [] and body["people_waiting"] == 0
    assert body["ledger"]["open"] == 0
    assert body["read_through"] == {"eos_todos": [], "commitments": []}


def test_the_registry_verdict_matches_the_door(db, systems_only):
    """What /api/users/me ships for `todos` (resolve_all) agrees with the gate;
    the /users/me endpoint itself is covered by test_feature_access.py."""
    from rubik_core.features import resolve_all

    assert resolve_all(_profile(SYSTEMS), db)["todos"] is True
    assert (
        resolve_all(_profile(MARKETING, department="Marketing"), db)["todos"] is False
    )


# ── Capture ──────────────────────────────────────────────────────────────────


def test_a_typed_promise_lands_under_who_is_waiting(db):
    _as(SYSTEMS)
    res = client.post(
        "/api/todos",
        json={
            "title": "Send the Q3 deck",
            "due_local": "2030-01-10",
            "counterparty_label": "Tatum Mercer",
        },
    )
    assert res.status_code == 201, res.text
    todo = res.json()["todo"]
    # The label resolved to the one user with that name — a real counterparty.
    assert todo["counterparty_kind"] == "person"
    assert todo["counterparty_id"] == TATUM
    assert todo["promised_for"] == "2030-01-10" == todo["due_local"]
    assert todo["status"] == "open" and todo["source"] == "page"

    page = client.get("/api/todos").json()
    assert page["people_waiting"] == 1
    assert [g["label"] for g in page["waiting"]] == ["Tatum Mercer"]
    assert page["waiting"][0]["items"][0]["id"] == todo["id"]


def test_a_promise_to_nobody_files_under_just_you(db):
    _as(SYSTEMS)
    client.post("/api/todos", json={"title": "Clean up the vendor list"})
    page = client.get("/api/todos").json()
    assert page["people_waiting"] == 0
    assert [g["label"] for g in page["waiting"]] == ["Just you"]


def test_parse_reads_the_date_and_the_person_without_writing(db):
    _as(SYSTEMS)
    res = client.post(
        "/api/todos/parse", json={"text": "send the deck to Tatum tomorrow"}
    )
    assert res.status_code == 200, res.text
    parsed = res.json()["parsed"]
    assert (
        parsed["due_local"]
        == (date.fromisoformat(res.json()["today"]) + timedelta(days=1)).isoformat()
    )
    assert parsed["counterparty_id"] == TATUM  # "Tatum" → the one Tatum in users
    assert client.get("/api/todos").json()["waiting"] == []  # nothing written


def test_another_users_promise_is_not_mine(db):
    _as(TATUM, department="Systems")
    theirs = client.post("/api/todos", json={"title": "Tatum's own"}).json()["todo"]
    _as(SYSTEMS)
    assert client.get("/api/todos").json()["waiting"] == []
    assert client.post(f"/api/todos/{theirs['id']}/kept").status_code == 404


# ── Transitions ──────────────────────────────────────────────────────────────


def test_kept_closes_the_row_and_clears_the_priority_card(db, monkeypatch):
    calls: list[dict] = []
    from rubik_core.priorities import memory as priority_memory

    monkeypatch.setattr(
        priority_memory, "resolve_entity_everywhere", lambda **kw: calls.append(kw)
    )
    _as(SYSTEMS)
    todo = client.post(
        "/api/todos", json={"title": "Reply to Wells on pricing"}
    ).json()["todo"]
    res = client.post(f"/api/todos/{todo['id']}/kept")
    assert res.status_code == 200, res.text
    assert res.json()["todo"]["status"] == "kept"
    assert res.json()["todo"]["kept_at"]
    assert calls and calls[0]["entity_id"] == todo["id"]
    assert calls[0]["entity_kind"].value == "promise"
    assert calls[0]["priority_kind"].value == "promise_due"
    # Keeping it twice is a conflict, not a silent no-op.
    assert client.post(f"/api/todos/{todo['id']}/kept").status_code == 409
    # And it is back after reopen, with the week's tally reflecting the trip.
    assert (
        client.post(f"/api/todos/{todo['id']}/reopen").json()["todo"]["status"]
        == "open"
    )


def test_an_overdue_promise_is_marked_slipping_on_read_and_snooze_brings_it_back(db):
    _as(SYSTEMS)
    yesterday = (_today(db) - timedelta(days=1)).isoformat()
    todo = client.post(
        "/api/todos",
        json={
            "title": "Revised SOW",
            "due_local": yesterday,
            "counterparty_label": "Remy",
        },
    ).json()["todo"]
    assert todo["status"] == "open"
    page = client.get("/api/todos").json()
    row = page["waiting"][0]["items"][0]
    assert row["status"] == "slipping" and row["slipping_since"]
    assert page["ledger"]["slipping"] == 1

    res = client.post(f"/api/todos/{todo['id']}/snooze", json={"preset": "week"})
    assert res.status_code == 200, res.text
    moved = res.json()["todo"]
    assert moved["status"] == "open"
    assert moved["snooze_count"] == 1
    assert moved["due_local"] == (_today(db) + timedelta(days=7)).isoformat()
    assert moved["promised_for"] == yesterday  # the original promise never moves


def test_snoozing_into_the_past_is_refused(db):
    _as(SYSTEMS)
    todo = client.post("/api/todos", json={"title": "x"}).json()["todo"]
    res = client.post(f"/api/todos/{todo['id']}/snooze", json={"until": "2020-01-01"})
    assert res.status_code == 409


def test_drop_records_the_reason_and_the_ledger_counts_it(db):
    _as(SYSTEMS)
    todo = client.post("/api/todos", json={"title": "x"}).json()["todo"]
    res = client.post(
        f"/api/todos/{todo['id']}/drop", json={"reason": "no longer needed"}
    )
    assert res.status_code == 200 and res.json()["todo"]["status"] == "dropped"
    from rubik_core.database.promises import PromiseEvent

    kinds = [
        e.kind
        for e in db.query(PromiseEvent)
        .filter_by(promise_id=todo["id"])
        .order_by(PromiseEvent.at)
    ]
    assert kinds == ["created", "dropped"]
    assert client.get("/api/todos").json()["ledger"]["dropped"] == 1


def test_patch_moves_the_date_and_logs_it(db):
    _as(SYSTEMS)
    todo = client.post(
        "/api/todos", json={"title": "x", "due_local": "2030-01-01"}
    ).json()["todo"]
    res = client.patch(
        f"/api/todos/{todo['id']}", json={"due_local": "2030-02-01", "title": "y"}
    )
    assert res.status_code == 200, res.text
    assert (
        res.json()["todo"]["due_local"] == "2030-02-01"
        and res.json()["todo"]["title"] == "y"
    )
    from rubik_core.database.promises import PromiseEvent

    kinds = {e.kind for e in db.query(PromiseEvent).filter_by(promise_id=todo["id"])}
    assert {"edited", "due_moved"} <= kinds


# ── From a priority card ─────────────────────────────────────────────────────


def test_add_to_my_todos_from_a_card_is_idempotent(db, monkeypatch):
    import backend.routers.home as home

    card = {
        "id": "prio-1",
        "title": "Send Tatum the Q3 pipeline deck",
        "why_it_matters": "She asked twice.",
        "suggested_action": "Export the deck and send it before Friday's sync.",
        "entity_kind": "person",
        "entity_id": TATUM,
        "entity_label": "Tatum Mercer",
        "priority_kind": "commitment_debt",
        "attrs": {"due_date": "2030-03-03"},
        "source_links": ["https://mail.google.com/x"],
    }
    monkeypatch.setattr(
        home,
        "_find_priority_in_today",
        lambda email, pid: card if pid == "prio-1" else None,
    )
    _as(SYSTEMS)
    res = client.post("/api/todos/from-priority", json={"priority_id": "prio-1"})
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["created"] is True
    todo = body["todo"]
    assert todo["source"] == "priority_card"
    assert todo["counterparty_id"] == TATUM and todo["counterparty_kind"] == "person"
    assert todo["due_local"] == "2030-03-03"
    assert todo["source_ref"]["priority_id"] == "prio-1"
    assert todo["detail"].startswith("Export the deck")

    again = client.post("/api/todos/from-priority", json={"priority_id": "prio-1"})
    assert again.status_code == 201 and again.json()["created"] is False
    assert again.json()["todo"]["id"] == todo["id"]
    assert (
        client.post(
            "/api/todos/from-priority", json={"priority_id": "nope"}
        ).status_code
        == 404
    )


# ── The priorities channel ───────────────────────────────────────────────────


def test_the_promises_channel_emits_the_open_ledger_as_signals(db):
    from rubik_core.priorities.agents.promises_agent import PromisesChannelAgent
    from rubik_core.priorities.config import PrioritiesConfig
    from rubik_core.priorities.models import ChannelKind, EntityKind
    from rubik_core.promises import store

    today = _today(db)
    store.create_promise(
        db,
        owner_email=SYSTEMS,
        title="Overdue one",
        due_local=today - timedelta(days=2),
        source="page",
        actor=SYSTEMS,
        counterparty_label="Remy",
    )
    store.create_promise(
        db,
        owner_email=SYSTEMS,
        title="Due soon",
        due_local=today + timedelta(days=2),
        source="page",
        actor=SYSTEMS,
    )
    store.create_promise(
        db,
        owner_email=SYSTEMS,
        title="Far away",
        due_local=today + timedelta(days=60),
        source="page",
        actor=SYSTEMS,
    )
    store.create_promise(
        db, owner_email=SYSTEMS, title="Undated", source="page", actor=SYSTEMS
    )
    kept = store.create_promise(
        db, owner_email=SYSTEMS, title="Already kept", source="page", actor=SYSTEMS
    )
    store.mark_kept(db, kept, actor=SYSTEMS)
    db.commit()

    digest = PromisesChannelAgent(user_email=SYSTEMS, config=PrioritiesConfig()).run()
    assert digest.channel == ChannelKind.PROMISES and digest.error is None
    titles = [s.entity_label for s in digest.signals]
    # Overdue first, then due-soon, then undated; the far-future and the kept
    # ones stay out of today's business.
    assert titles == ["Overdue one", "Due soon", "Undated"]
    first = digest.signals[0]
    assert first.entity_kind == EntityKind.PROMISE
    assert first.attrs["due_date"] == (today - timedelta(days=2)).isoformat()
    assert first.attrs["mentioned_dates"][0]["resolved"] == first.attrs["due_date"]
    assert first.attrs["counterparty_label"] == "Remy"
    assert "1 past the promised date" in digest.summary


def test_the_channel_is_silent_when_switched_off(db, monkeypatch):
    from rubik_core.priorities.agents.promises_agent import PromisesChannelAgent
    from rubik_core.priorities.config import PrioritiesConfig
    from rubik_core.promises import store

    store.create_promise(
        db, owner_email=SYSTEMS, title="x", source="page", actor=SYSTEMS
    )
    db.commit()
    monkeypatch.setattr(settings, "TODOS_ENABLED", False)
    digest = PromisesChannelAgent(user_email=SYSTEMS, config=PrioritiesConfig()).run()
    assert digest.signals == [] and digest.error is None


def test_the_channel_runs_in_production_by_default():
    from rubik_core.priorities.models import ChannelKind, PrioritiesRequest
    from rubik_core.priorities.orchestrator import CHANNEL_AGENT_REGISTRY

    assert ChannelKind.PROMISES in PrioritiesRequest(user_email=SYSTEMS).channels
    assert ChannelKind.PROMISES in CHANNEL_AGENT_REGISTRY


# ── Slack /todo ──────────────────────────────────────────────────────────────


def _slack_state(desc: str, *, where: str | None, due: str | None = None) -> dict:
    state: dict = {"desc": {"value": {"value": desc}}}
    if where:
        state["where"] = {"value": {"selected_option": {"value": where}}}
    if due:
        state["due"] = {"value": {"selected_date": due}}
    return state


def test_slack_todo_modal_offers_where_only_to_the_roster(db, systems_only):
    from backend.routers import slack_commands as sc

    view = sc._build_todo_view(_profile(SYSTEMS), db, prefill="", private_metadata="{}")
    where = [b for b in view["blocks"] if b.get("block_id") == "where"]
    assert len(where) == 1
    assert (
        where[0]["element"]["initial_option"]["value"] == "mine"
    )  # My list is the default

    view = sc._build_todo_view(
        _profile(MARKETING, department="Marketing"),
        db,
        prefill="",
        private_metadata="{}",
    )
    assert not [b for b in view["blocks"] if b.get("block_id") == "where"]


def test_slack_my_list_writes_a_promise_not_an_eos_todo(db):
    from backend.routers import slack_commands as sc
    from rubik_core.database.eos.todos import Todo
    from rubik_core.database.promises import Promise

    res = sc._create_todo_from_modal(
        db,
        _profile(SYSTEMS),
        _slack_state("Send Remy the revised SOW", where="mine", due="2030-05-05"),
        view_id="view-promise-1",
    )
    import json

    payload = json.loads(res.body)
    assert payload["response_action"] == "update", payload
    assert "Added to your To-Dos" in json.dumps(payload["view"])
    rows = db.query(Promise).all()
    assert len(rows) == 1 and rows[0].source == "slack_cmd"
    assert rows[0].due_local == date(2030, 5, 5)
    assert db.query(Todo).count() == 0


def test_slack_my_list_is_refused_off_the_roster(db, systems_only):
    from backend.routers import slack_commands as sc

    res = sc._create_todo_from_modal(
        db,
        _profile(MARKETING, department="Marketing"),
        _slack_state("x", where="mine"),
        view_id="view-promise-2",
    )
    import json

    payload = json.loads(res.body)
    assert payload["response_action"] == "errors" and "where" in payload["errors"]


# ── Chat tools ───────────────────────────────────────────────────────────────


def test_chat_create_reads_the_day_back_and_resolves_the_person(db):
    from rubik_core.tools.todo_tools import TodoChatTools

    tools = TodoChatTools(user_email=SYSTEMS, user_timezone="America/Chicago")
    result = tools.create_todo("Send the Q3 deck", due="tomorrow", for_whom="Tatum")
    assert result["status"] == "created", result
    assert "Added to your To-Dos" in result["report"]
    listed = tools.list_my_todos()
    assert listed["count"] == 1 and "Tatum Mercer" in listed["report"]
    kept = tools.mark_todo_kept(result["todo_id"])
    assert kept["status"] == "success"
    assert tools.list_my_todos()["count"] == 0


def test_chat_refuses_a_date_it_cannot_read_instead_of_guessing(db):
    from rubik_core.tools.todo_tools import TodoChatTools

    result = TodoChatTools(user_email=SYSTEMS).create_todo(
        "x", due="whenever the moon is right"
    )
    assert result["status"] == "error" and "couldn't read" in result["report"]


def test_chat_tools_are_registered_only_with_the_gate():
    from rubik_core.agent_runtime import coordinator
    from rubik_core.tools.todo_tools import TodoChatTools

    def names(agent) -> set[str]:
        return {
            getattr(t, "name", None) or getattr(t, "__name__", "") for t in agent.tools
        }

    bare = coordinator.build_coordinator(user_email=SYSTEMS)
    assert "create_todo" not in names(bare)
    assert "## My To-Dos" not in (bare.static_instruction or "")

    armed = coordinator.build_coordinator(
        user_email=SYSTEMS, todo_tools=TodoChatTools(SYSTEMS)
    )
    assert {"create_todo", "list_my_todos", "mark_todo_kept", "snooze_todo"} <= names(
        armed
    )
    assert "## My To-Dos" in armed.static_instruction


# ── Parser fallback ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text, expected_due, expected_title",
    [
        ("send Remy the SOW Thursday", date(2026, 9, 17), "send Remy the SOW"),
        ("call the auditor tomorrow", date(2026, 9, 15), "call the auditor"),
        ("vendor list by next week", date(2026, 9, 21), "vendor list"),
        ("finish the deck 2026-10-02", date(2026, 10, 2), "finish the deck"),
        ("book the room Oct 3", date(2026, 10, 3), "book the room"),
        ("clean up the vendor list", None, "clean up the vendor list"),
    ],
)
def test_fallback_parse_reads_the_date_words_people_type(
    text, expected_due, expected_title
):
    from rubik_core.promises.parse import fallback_parse

    monday = date(2026, 9, 14)
    parsed = fallback_parse(text, today=monday)
    assert parsed["due_local"] == (expected_due.isoformat() if expected_due else None)
    assert parsed["title"] == expected_title
    assert parsed["parser"] == "fallback"


def test_promise_dates_are_calendar_days_not_instants(db):
    """A due date stored as a DATE survives a round trip unchanged — the
    scorecard-week bug this table was designed against."""
    from rubik_core.database.promises import Promise
    from rubik_core.promises import store

    row = store.create_promise(
        db,
        owner_email=SYSTEMS,
        title="x",
        due_local=date(2030, 1, 1),
        source="page",
        actor=SYSTEMS,
    )
    db.commit()
    fresh = db.query(Promise).filter_by(id=row.id).one()
    assert fresh.due_local == date(2030, 1, 1)
    assert isinstance(fresh.created_at, datetime)


def test_ticking_the_priority_card_marks_the_promise_kept(db):
    """✓ on a "Your To-Do" card and Kept on the page are the same action."""
    from backend.routers.home import _promise_writeback
    from rubik_core.database.promises import Promise
    from rubik_core.promises import store

    row = store.create_promise(
        db, owner_email=SYSTEMS, title="x", source="page", actor=SYSTEMS
    )
    other = store.create_promise(
        db, owner_email=TATUM, title="hers", source="page", actor=TATUM
    )
    db.commit()
    _promise_writeback(SYSTEMS, {"entity_kind": "promise", "entity_id": row.id})
    # Someone else's promise id on my card must never flip their row.
    _promise_writeback(SYSTEMS, {"entity_kind": "promise", "entity_id": other.id})
    _promise_writeback(
        SYSTEMS, {"entity_kind": "todo", "entity_id": row.id}
    )  # EOS kind: untouched
    db.expire_all()
    assert db.query(Promise).filter_by(id=row.id).one().status == "kept"
    assert db.query(Promise).filter_by(id=other.id).one().status == "open"

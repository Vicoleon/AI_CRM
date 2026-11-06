"""Chat: "who did my team promise something to this week and hasn't done it?"

Covers who may be read (own, a manager's scope, the org-wide Exec, never an
admin's operator role), the department filter, where each promise stands,
grouping by contact, the honest empty states, and the tool's registration and
routing rule.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

EMERY = "emery@acme.example"
REP = "rep@acme.example"
MKT = "mia@acme.example"
DAKOTA = "dakota@client.com"
NOW = datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)  # a Thursday


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from rubik_core.database.models import Base, User

    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr("rubik_core.database.db.SessionLocal", Session)
    s = Session()
    for email, name, dept in (
        (EMERY, "Emery Brooks", "Executive"),
        (REP, "Sam Rep", "Sales"),
        (MKT, "Mia Marketing", "Marketing"),
    ):
        s.add(User(email=email, id=email, name=name, department=dept))
    s.commit()
    yield s
    s.close()
    engine.dispose()


def _user(email, *, org_wide=False):
    return SimpleNamespace(email=email, sees_all_departments=org_wide)


def _promise(db, owner, *, title, said, due=None, verdict=None, checked=None):
    from rubik_core.promises import store

    row = store.create_promise(
        db,
        owner_email=owner,
        title=title,
        source="heard_transcript",
        actor=store.SYSTEM_ACTOR,
        status="suggested",
        due_local=due,
        counterparty_kind="person",
        counterparty_id=DAKOTA,
        counterparty_label="Dakota Client",
        confidence=0.9,
        source_ref={
            "quote": f"I'll {title.lower()}",
            "at": said.isoformat(),
            "where": "the meeting “Acme renewal”",
            "link": "https://drive.google.com/file/d/x/view",
        },
    )
    if verdict:
        store.record_check(db, row, verdict=verdict, checked=checked or ["sent email"])
    store.mark_scanned(db, owner, f"transcript:{title}", found=1)
    db.commit()
    return row


def _report(db, user, **kw):
    from backend.services.promise_report import build_report

    return build_report(db, user, now=NOW, **kw)


def _scope(monkeypatch, emails, problem=None):
    monkeypatch.setattr(
        "backend.routers.eos._authz.email_scope",
        lambda db, user: SimpleNamespace(owner_emails=frozenset(emails)),
    )
    monkeypatch.setattr(
        "backend.routers.eos._authz.team_scope_problem", lambda s: problem
    )


def test_the_ceo_sees_the_sales_teams_unkept_promises_grouped_by_contact(db):
    _promise(
        db,
        REP,
        title="Send Dakota the quote",
        said=datetime(2026, 9, 28, 15),
        due=date(2026, 9, 30),
        verdict="not_found",
        checked=["sent email", "Slack"],
    )
    _promise(
        db, REP, title="Share the onboarding checklist", said=datetime(2026, 9, 29, 15)
    )
    _promise(
        db,
        MKT,
        title="Send the case study",
        said=datetime(2026, 9, 29, 15),
        due=date(2026, 9, 30),
        verdict="not_found",
    )

    out = _report(db, _user(EMERY, org_wide=True), whose="team", team="sales")
    r = out["report"]
    assert out["status"] == "success" and out["promise_count"] == 2
    assert "**Dakota Client <dakota@client.com>**" in r
    assert "Sam Rep: Send Dakota the quote" in r
    assert "**no sign it happened yet (checked sent email, Slack)**" in r
    assert "Share the onboarding checklist" in r and "**not due yet**" in r
    assert "case study" not in r  # marketing, filtered out by the team name
    assert "1 of 2 show no sign" in r
    assert "a prompt to ask, not proof" in r  # what "no sign" means


def test_a_done_looking_promise_is_left_out_unless_asked_for(db):
    _promise(
        db,
        REP,
        title="Send Dakota the quote",
        said=datetime(2026, 9, 28, 15),
        due=date(2026, 9, 30),
        verdict="kept",
    )
    ceo = _user(EMERY, org_wide=True)
    assert _report(db, ceo, whose="team")["promise_count"] == 0
    out = _report(db, ceo, whose="team", include_done=True)
    assert "looks done" in out["report"]


def test_a_manager_reads_only_their_scope(db, monkeypatch):
    _promise(db, REP, title="Send Dakota the quote", said=datetime(2026, 9, 28, 15))
    _promise(db, MKT, title="Send the case study", said=datetime(2026, 9, 29, 15))
    _scope(monkeypatch, {"lead@acme.example", REP})
    r = _report(db, _user("lead@acme.example"), whose="team")["report"]
    assert "Send Dakota the quote" in r and "case study" not in r


def test_an_admin_is_not_widened_to_everyones_promises(db, monkeypatch):
    """Operator, not executive: an admin with no team sees no one else's."""
    _promise(db, REP, title="Send Dakota the quote", said=datetime(2026, 9, 28, 15))
    _scope(monkeypatch, {"admin@acme.example"})
    out = _report(db, _user("admin@acme.example"), whose="team")
    assert "Send Dakota the quote" not in out["report"]


def test_an_unresolvable_team_is_said_out_loud_not_shown_empty(db, monkeypatch):
    _scope(monkeypatch, {EMERY}, problem="org_record_missing")
    out = _report(db, _user(EMERY), whose="team")
    assert out["status"] == "error" and "Accountability Chart" in out["report"]


def test_before_the_first_overnight_read_it_says_so(db):
    out = _report(db, _user(EMERY, org_wide=True), whose="team", team="sales")
    assert "hasn't read" in out["report"] and "tomorrow morning" in out["report"]


def test_a_promise_due_this_week_counts_even_if_made_earlier(db):
    _promise(
        db,
        REP,
        title="Send Dakota the renewal pack",
        said=datetime(2026, 9, 10, 15),
        due=date(2026, 10, 2),
    )
    out = _report(db, _user(EMERY, org_wide=True), whose="team")
    assert "renewal pack" in out["report"]


def test_my_own_promises(db):
    _promise(db, EMERY, title="Send the board deck", said=datetime(2026, 9, 29, 15))
    _promise(db, REP, title="Send Dakota the quote", said=datetime(2026, 9, 28, 15))
    r = _report(db, _user(EMERY, org_wide=True), whose="me")["report"]
    assert "board deck" in r and "Dakota the quote" not in r


def test_the_tool_is_registered_and_routed():
    import inspect

    from rubik_core.adk_agent.tools import AgentTools
    from rubik_core.agent_runtime import coordinator

    src = inspect.getsource(coordinator)
    assert "tools_instance.promises_outstanding" in src
    assert "ALWAYS `promises_outstanding`" in src
    doc = AgentTools.promises_outstanding.__doc__ or ""
    assert "promise" in doc and "what_am_i_waiting_on" in doc


def test_the_tool_refuses_when_switched_off(monkeypatch):
    from rubik_core.adk_agent.tools import AgentTools
    from rubik_core.config import settings

    monkeypatch.setattr(settings, "PROMISE_ACCOUNTABILITY_ENABLED", False)
    tools = AgentTools.__new__(AgentTools)
    tools.user_email = EMERY
    out = AgentTools.promises_outstanding(tools, whose="team")
    assert out["status"] == "error" and "switched off" in out["report"]

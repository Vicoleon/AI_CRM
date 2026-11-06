"""Reading the graph back out: X-Ray's "which meetings have we had with them?"

This is the question Phase 1 was built for. X-Ray used to answer it by searching
the meetings corpus for the words `"meetings with {company}"`, so a review that
only ever named the people in the room was invisible to it. The graph answers it
by identity — company → its people → what they attended.

The load-bearing test is `test_a_reader_never_sees_a_meeting_they_were_not_in`.
The graph knows every meeting the company ever had; the reader may see only
their own, and a traversal must not become a way around the transcript ACL.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

from rubik_core.graph.queries import (
    company_meeting_history,
    format_meeting_history_block,
)

REP = "rep@acme.example"
COLLEAGUE = "other@acme.example"
CLIENT = "carla@northwindholding.alt"
CLIENT_2 = "dan@northwindholding.alt"
DOMAIN = "northwindholding.alt"


def _wire_sqlite(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from rubik_core.database import db as db_module
    from rubik_core.database import repository as repo_module
    from rubik_core.database.models import Base

    engine = create_engine(f"sqlite:///{tmp_path}/q.db")
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)
    monkeypatch.setattr(db_module, "SessionLocal", TestSession)
    monkeypatch.setattr(repo_module, "SessionLocal", TestSession)
    return TestSession


def _participants_repo():
    from rubik_core.database import repository

    return repository.SQLMeetingParticipantsRepository()


def _meeting(TestSession, doc_id, title, when, attendees):
    """Seed a transcript + its ACL rows; the graph writer fills the graph."""
    from rubik_core.database.models import Asset

    with TestSession() as s:
        s.add(
            Asset(
                doc_id=doc_id,
                owner_email=REP,
                status="approved",
                topic="Meeting Transcripts",
                original_filename=title,
                created_at=when,
                updated_at=when,
            )
        )
        s.commit()
    _participants_repo().upsert_participants(
        doc_id, meeting_id=f"m-{doc_id}", emails=attendees
    )


def test_history_resolves_meetings_by_identity_not_by_name(tmp_path, monkeypatch):
    """The whole point: a transcript titled with no company name still surfaces."""
    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    _meeting(
        TestSession,
        "d1",
        "Weekly sync",  # deliberately does NOT mention Northwind
        datetime(2026, 8, 4),
        [REP, CLIENT],
    )

    hits = company_meeting_history(DOMAIN, REP)

    assert [h.doc_id for h in hits] == ["d1"]
    assert hits[0].title == "Weekly sync"
    assert hits[0].counterparts == [CLIENT]
    assert hits[0].when == datetime(2026, 8, 4)


def test_a_reader_never_sees_a_meeting_they_were_not_in(tmp_path, monkeypatch):
    """The access boundary. The graph knows both meetings; the rep attended one."""
    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    _meeting(TestSession, "mine", "Rep + client", datetime(2026, 8, 4), [REP, CLIENT])
    _meeting(
        TestSession,
        "theirs",
        "Colleague + client",
        datetime(2026, 8, 10),  # newer, so it would sort first if it leaked
        [COLLEAGUE, CLIENT],
    )

    hits = company_meeting_history(DOMAIN, REP)

    assert [h.doc_id for h in hits] == ["mine"]


def test_meetings_are_newest_first_and_capped(tmp_path, monkeypatch):
    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    for i, month in enumerate((5, 6, 7, 8), start=1):
        _meeting(
            TestSession,
            f"d{i}",
            f"Review {month}",
            datetime(2026, month, 1),
            [REP, CLIENT],
        )

    hits = company_meeting_history(DOMAIN, REP, limit=2)

    assert [h.doc_id for h in hits] == ["d4", "d3"]


def test_several_client_people_in_one_room_are_all_named(tmp_path, monkeypatch):
    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    _meeting(TestSession, "d1", "QBR", datetime(2026, 8, 4), [REP, CLIENT, CLIENT_2])

    hits = company_meeting_history(DOMAIN, REP)

    assert hits[0].counterparts == sorted([CLIENT, CLIENT_2])


def test_an_unrelated_company_returns_nothing(tmp_path, monkeypatch):
    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    _meeting(TestSession, "d1", "QBR", datetime(2026, 8, 4), [REP, CLIENT])

    assert company_meeting_history("someoneelse.com", REP) == []


def test_a_consumer_mail_domain_is_never_treated_as_a_company(tmp_path, monkeypatch):
    """Otherwise every freelancer's meetings would merge into one 'company'."""
    _wire_sqlite(tmp_path, monkeypatch)

    assert company_meeting_history("gmail.com", REP) == []


def test_missing_inputs_are_answered_with_nothing_not_an_error(tmp_path, monkeypatch):
    _wire_sqlite(tmp_path, monkeypatch)

    assert company_meeting_history(None, REP) == []
    assert company_meeting_history(DOMAIN, "") == []


def test_hubspot_contacts_reach_meetings_even_off_the_company_domain(
    tmp_path, monkeypatch
):
    """A client contact using a personal address still links, because X-Ray
    passes the CRM contact list in explicitly."""
    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    consultant = "consultant@gmail.com"
    _meeting(TestSession, "d1", "Onboarding", datetime(2026, 8, 4), [REP, consultant])

    assert company_meeting_history(DOMAIN, REP) == []
    hits = company_meeting_history(DOMAIN, REP, contact_emails=[consultant])
    assert [h.doc_id for h in hits] == ["d1"]


# ── the rendered block ───────────────────────────────────────────────────────


def test_the_block_is_empty_when_there_is_no_history():
    """No block ⇒ no prompt instruction referring to one."""
    assert format_meeting_history_block([], "Northwind") == ""


def test_the_block_states_dates_titles_and_who(tmp_path, monkeypatch):
    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    _meeting(TestSession, "d1", "QBR", datetime(2026, 8, 4), [REP, CLIENT])

    block = format_meeting_history_block(
        company_meeting_history(DOMAIN, REP), "Northwind"
    )

    assert "MEETINGS ON RECORD WITH NORTHWIND" in block
    assert "Tuesday, August 04, 2026" in block
    assert '"QBR"' in block
    assert CLIENT in block


# ── the X-Ray channel agent ──────────────────────────────────────────────────


def _agent(TestSession, *, excerpts="Some transcript excerpt.", domain=DOMAIN):
    from rubik_core.xray.agents.meetings_agent import MeetingsChannelAgent
    from rubik_core.xray.schemas import CompanyBundle

    bundle = CompanyBundle(company_id="999")
    bundle.company_name = "Northwind"
    bundle.company_domain = domain
    bundle.contacts = [{"id": "1", "email": CLIENT}]
    agent = MeetingsChannelAgent(bundle=bundle, requesting_user=REP)

    class _Tools:
        def __init__(self, user_email):
            pass

        async def search_past_meetings(self, query, max_results=5):
            return {"status": "success", "report": excerpts}

    return agent, _Tools


def test_the_agent_combines_attendance_facts_with_transcript_excerpts(
    tmp_path, monkeypatch
):
    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    _meeting(TestSession, "d1", "QBR", datetime(2026, 8, 4), [REP, CLIENT])
    agent, tools = _agent(TestSession)

    with patch("rubik_core.adk_agent.tools.AgentTools", tools):
        evidence = agent.gather()

    assert "MEETINGS ON RECORD WITH NORTHWIND" in evidence
    assert "Some transcript excerpt." in evidence


def test_the_agent_is_unchanged_when_the_graph_has_nothing(tmp_path, monkeypatch):
    """No history ⇒ byte-identical to the pre-graph behaviour."""
    _wire_sqlite(tmp_path, monkeypatch)
    agent, tools = _agent(None)

    with patch("rubik_core.adk_agent.tools.AgentTools", tools):
        evidence = agent.gather()

    assert evidence == "Some transcript excerpt."


def test_the_read_flag_off_restores_the_old_behaviour(tmp_path, monkeypatch):
    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    _meeting(TestSession, "d1", "QBR", datetime(2026, 8, 4), [REP, CLIENT])
    agent, tools = _agent(TestSession)

    with (
        patch("rubik_core.config.settings.CUBIE_GRAPH_READS_ENABLED", False),
        patch("rubik_core.adk_agent.tools.AgentTools", tools),
    ):
        evidence = agent.gather()

    assert evidence == "Some transcript excerpt."


def test_a_graph_failure_never_sinks_the_channel(tmp_path, monkeypatch):
    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    _meeting(TestSession, "d1", "QBR", datetime(2026, 8, 4), [REP, CLIENT])
    agent, tools = _agent(TestSession)

    with (
        patch(
            "rubik_core.graph.queries.company_meeting_history",
            side_effect=RuntimeError("graph down"),
        ),
        patch("rubik_core.adk_agent.tools.AgentTools", tools),
    ):
        evidence = agent.gather()

    assert evidence == "Some transcript excerpt."


def test_the_agent_still_reports_history_when_retrieval_returns_nothing(
    tmp_path, monkeypatch
):
    """'We met them three times and have no transcript text' is a real answer,
    and strictly better than the old empty result."""
    TestSession = _wire_sqlite(tmp_path, monkeypatch)
    _meeting(TestSession, "d1", "QBR", datetime(2026, 8, 4), [REP, CLIENT])
    agent, tools = _agent(TestSession, excerpts="")

    with patch("rubik_core.adk_agent.tools.AgentTools", tools):
        evidence = agent.gather()

    assert "MEETINGS ON RECORD WITH NORTHWIND" in evidence


# ── the prompt ───────────────────────────────────────────────────────────────


def test_the_prompt_explains_the_block_only_when_it_is_present():
    from rubik_core.xray import prompts

    with_block, _ = prompts.build_meetings_prompt(
        "MEETINGS ON RECORD WITH NORTHWIND (attendance records...)", "Northwind"
    )
    without, _ = prompts.build_meetings_prompt("just excerpts", "Northwind")

    assert "attendance records, not retrieval" in with_block
    assert "attendance records, not retrieval" not in without

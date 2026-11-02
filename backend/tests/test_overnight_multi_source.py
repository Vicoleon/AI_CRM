"""End-to-end smoke test for UC-17's multi-source priorities path.

Wires a fake SignalSource per source, a stub Correlator, and a stub
GeminiJudge into the `MultiSourcePrioritiesPlugin`, then runs the
`OvernightAgent` to verify the full chain — collect → correlate → judge →
translate → persist — lands the right rows in `agent_recommendations`,
including the new UC-17.8 columns (`sources`, `correlation_id`,
`next_action`).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.agents.multi_source_priorities import (
    FEATURE_KEY,
    MultiSourcePrioritiesPlugin,
    _audience,
)
from backend.agents.overnight import OvernightAgent
from backend.signals.correlator import Correlator
from backend.signals.judge import JudgeDecision
from backend.signals.types import Signal
from rubik_core.database.models import AgentRecommendation, Base, User
from rubik_core.features import FeaturePolicy, clear_cache, save_policy

#: Every ``@acme.example`` colleague here is a roster member, so this module
#: opts into the one fixture carve-out (``conftest.roster_admits_acme_example``).
pytestmark = pytest.mark.usefixtures("roster_admits_acme_example")

DOMAIN = "acme.example"


@pytest.fixture(autouse=True)
def _fresh_rosters():
    """The roster lookup is cached per process; start and end with none."""
    clear_cache()
    yield
    clear_cache()


@pytest.fixture(autouse=True)
def _company_and_no_leavers(monkeypatch, set_test_workspace_domains):
    """The people below work at acme.example, and nobody has left.

    The control test workspace names this domain; the real leaver lookup asks
    Slack and Google, neither of which belongs in these tests.
    """
    set_test_workspace_domains(DOMAIN)
    monkeypatch.setattr(
        "backend.services.directory_status.deactivated_priorities_emails",
        lambda emails, **_kw: set(),
    )


@pytest.fixture
def db_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    db = Session()
    try:
        db.add(
            User(
                email="rep1@acme.example",
                id="u1",
                name="Rep One",
                department="sales",
            )
        )
        db.add(
            User(
                email="rep2@acme.example",
                id="u2",
                name="Rep Two",
                department="sales",
            )
        )
        db.add(User(email="hr@acme.example", id="u3", name="HR", department="hr"))
        db.commit()
    finally:
        db.close()

    return Session


def _signal(source: str, signal_type: str, user: str, **refs) -> Signal:
    return Signal(
        user_email=user,
        source=source,
        signal_type=signal_type,
        summary=f"{source}/{signal_type} for {user}",
        occurred_at=datetime.now(timezone.utc),
        entity_refs=refs,
        severity_hint="high",
        payload={},
    )


class _FakeSource:
    def __init__(self, name: str, per_user: dict[str, list[Signal]]):
        self.name = name
        self._per_user = per_user
        self.calls: list[tuple[str, datetime]] = []

    def fetch(self, user_email: str, *, since: datetime) -> list[Signal]:
        self.calls.append((user_email, since))
        return list(self._per_user.get(user_email, []))


def test_multi_source_orchestrator_persists_judged_priorities(db_factory):
    rep1 = "rep1@acme.example"
    rep2 = "rep2@acme.example"

    hubspot = _FakeSource(
        "hubspot",
        {
            rep1: [
                _signal(
                    "hubspot",
                    "stale_lead",
                    rep1,
                    deal_id="D1",
                    contact_email="jane@acmecorp.example",
                    company_name="Acme",
                )
            ],
            rep2: [
                _signal(
                    "hubspot", "stale_lead", rep2, deal_id="D2", company_name="Globex"
                )
            ],
        },
    )
    gmail = _FakeSource(
        "gmail",
        {
            rep1: [
                _signal(
                    "gmail",
                    "unread_thread",
                    rep1,
                    thread_id="T1",
                    contact_email="jane@acmecorp.example",
                )
            ],
        },
    )
    slack = _FakeSource("slack", {})
    calendar = _FakeSource("calendar", {})
    transcript = _FakeSource("transcript", {})

    judge = MagicMock()

    def _judge(groups, **_kwargs):
        return [
            JudgeDecision(
                correlation_id=g.correlation_id,
                user_email=g.user_email,
                include=True,
                importance=4,
                reason=f"reason for {g.correlation_id}",
                next_action=f"next action for {g.correlation_id}",
                raw_response="",
            )
            for g in groups
        ]

    judge.judge.side_effect = _judge

    plugin = MultiSourcePrioritiesPlugin(
        sources=[hubspot, gmail, slack, calendar, transcript],
        correlator=Correlator(),
        judge=judge,
    )
    result = OvernightAgent(plugins=[plugin], db_factory=db_factory).run()

    assert result.errors == []
    assert result.written == 2  # one deal-keyed group per rep
    assert result.skipped_duplicates == 0

    db = db_factory()
    try:
        rows = (
            db.query(AgentRecommendation).order_by(AgentRecommendation.user_email).all()
        )
    finally:
        db.close()

    assert len(rows) == 2
    by_user = {r.user_email: r for r in rows}

    r1 = by_user[rep1]
    assert r1.correlation_id == "deal:D1"
    assert r1.next_action == "next action for deal:D1"
    assert r1.lead_id == "D1"
    assert r1.lead_name == "Acme"
    assert r1.type == "deal_priority"
    assert r1.confidence == pytest.approx(0.8)
    assert isinstance(r1.sources, list)
    assert {s["source"] for s in r1.sources} == {
        "hubspot",
        "gmail",
    }  # correlator joined on contact_email

    r2 = by_user[rep2]
    assert r2.correlation_id == "deal:D2"
    assert r2.lead_id == "D2"
    assert r2.lead_name == "Globex"
    assert {s["source"] for s in r2.sources} == {"hubspot"}

    assert (
        hubspot.calls
        and gmail.calls
        and slack.calls
        and calendar.calls
        and transcript.calls
    )
    # Overnight AI Priorities ship open to everyone in the workspace, so HR is
    # read too (it simply had no signals to judge).
    assert {c[0] for c in hubspot.calls} == {rep1, rep2, "hr@acme.example"}


def test_excluded_decisions_are_dropped(db_factory):
    rep = "rep1@acme.example"
    source = _FakeSource(
        "hubspot", {rep: [_signal("hubspot", "stale_lead", rep, deal_id="D9")]}
    )
    empty = _FakeSource("gmail", {})

    judge = MagicMock()
    judge.judge.return_value = [
        JudgeDecision(
            correlation_id="deal:D9",
            user_email=rep,
            include=False,
            importance=0,
            reason="not noteworthy",
            next_action="",
            raw_response="",
        )
    ]

    plugin = MultiSourcePrioritiesPlugin(
        sources=[source, empty, empty, empty, empty],
        judge=judge,
    )
    result = OvernightAgent(plugins=[plugin], db_factory=db_factory).run()

    assert result.errors == []
    assert result.written == 0


def test_one_source_failing_does_not_kill_others(db_factory, caplog):
    rep = "rep1@acme.example"

    class _Bomb:
        name = "calendar"

        def fetch(self, user_email, *, since):
            raise RuntimeError("calendar API down")

    hubspot = _FakeSource(
        "hubspot", {rep: [_signal("hubspot", "stale_lead", rep, deal_id="D7")]}
    )

    judge = MagicMock()
    judge.judge.return_value = [
        JudgeDecision(
            correlation_id="deal:D7",
            user_email=rep,
            include=True,
            importance=3,
            reason="ok",
            next_action="call them",
            raw_response="",
        )
    ]

    plugin = MultiSourcePrioritiesPlugin(
        sources=[
            hubspot,
            _Bomb(),
            _FakeSource("gmail", {}),
            _FakeSource("slack", {}),
            _FakeSource("transcript", {}),
        ],
        judge=judge,
    )
    result = OvernightAgent(plugins=[plugin], db_factory=db_factory).run()

    assert result.errors == []  # plugin-level error isolation, not source-level
    assert result.written == 1


def test_no_users_returns_empty(db_factory):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    judge = MagicMock()
    judge.judge.return_value = []

    plugin = MultiSourcePrioritiesPlugin(
        sources=[
            _FakeSource("hubspot", {}),
            _FakeSource("gmail", {}),
            _FakeSource("slack", {}),
            _FakeSource("calendar", {}),
            _FakeSource("transcript", {}),
        ],
        judge=judge,
    )
    result = OvernightAgent(plugins=[plugin], db_factory=Session).run()

    assert result.written == 0
    assert result.errors == []
    judge.judge.assert_not_called()


def test_alignment_context_reaches_judge_and_marker_lands_on_evidence(db_factory):
    """The plugin must (a) call the judge with a per-user alignment-context
    dict keyed by users that actually produced signals, and (b) carry the
    judge's aligned/alignment_note marker through to the recommendation's
    `evidence` JSON column so downstream consumers can see why it ranked.
    """
    rep1 = "rep1@acme.example"
    rep2 = "rep2@acme.example"

    # Only rep1 produces signals → only rep1 should appear in the alignment dict.
    hubspot = _FakeSource(
        "hubspot",
        {
            rep1: [
                _signal(
                    "hubspot", "stale_lead", rep1, deal_id="D1", company_name="Acme"
                )
            ],
        },
    )
    empty = _FakeSource("gmail", {})

    judge = MagicMock()
    judge.judge.return_value = [
        JudgeDecision(
            correlation_id="deal:D1",
            user_email=rep1,
            include=True,
            importance=4,
            reason="Stale 28d on Acme rock deal.",
            next_action="Send re-engage email.",
            aligned=True,
            alignment_note="Rock: AI Roadmap",
            raw_response="",
        ),
    ]

    plugin = MultiSourcePrioritiesPlugin(
        sources=[
            hubspot,
            empty,
            _FakeSource("slack", {}),
            _FakeSource("calendar", {}),
            _FakeSource("transcript", {}),
        ],
        judge=judge,
    )
    result = OvernightAgent(plugins=[plugin], db_factory=db_factory).run()

    assert result.errors == []
    assert result.written == 1

    # Judge was called with kwargs including the per-user alignment dict.
    assert judge.judge.call_count == 1
    _args, kwargs = judge.judge.call_args
    align_map = kwargs.get("alignment_context_by_user")
    assert isinstance(align_map, dict)
    # rep1 produced signals → present (empty string is fine when there's no EOS data).
    assert rep1 in align_map
    # rep2 produced no signals → NOT present (we don't waste DB queries on no-op users).
    assert rep2 not in align_map

    # The aligned/alignment_note marker rides on the recommendation's evidence dict.
    db = db_factory()
    try:
        row = db.query(AgentRecommendation).filter_by(user_email=rep1).first()
    finally:
        db.close()
    assert row is not None
    assert row.evidence == {"aligned": True, "alignment_note": "Rock: AI Roadmap"}


# ── Who is in the overnight audience (the ``ai_priorities`` roster) ─────────


def _with_newcomers(db_factory):
    db = db_factory()
    try:
        db.add(User(email="new@acme.example", id="u4", name="New", department=None))
        db.add(
            User(email="gen@acme.example", id="u5", name="Gen", department="general")
        )
        db.add(
            User(
                email="chief@acme.example",
                id="u6",
                name="Chief",
                department=None,
                is_exec=True,
            )
        )
        db.commit()
    finally:
        db.close()


def test_a_person_with_no_department_is_in_the_overnight_audience(db_factory):
    _with_newcomers(db_factory)
    db = db_factory()
    try:
        audience = set(_audience(db))
    finally:
        db.close()

    assert {"new@acme.example", "gen@acme.example"} <= audience
    assert audience == {
        "rep1@acme.example",
        "rep2@acme.example",
        "hr@acme.example",
        "new@acme.example",
        "gen@acme.example",
        "chief@acme.example",
    }


def test_an_admin_narrowing_the_roster_takes_them_out(db_factory):
    _with_newcomers(db_factory)
    db = db_factory()
    try:
        save_policy(
            db,
            FEATURE_KEY,
            FeaturePolicy(departments=frozenset({"sales"}), exec_allowed=True),
            actor_email="admin@acme.example",
        )
        audience = set(_audience(db))
    finally:
        db.close()

    assert audience == {"rep1@acme.example", "rep2@acme.example", "chief@acme.example"}


def test_someone_narrowed_out_of_the_copilot_gets_no_overnight_run(db_factory):
    """The cards land in the briefing, a copilot surface."""
    _with_newcomers(db_factory)
    db = db_factory()
    try:
        save_policy(
            db,
            "copilot",
            FeaturePolicy(departments=frozenset({"hr"})),
            actor_email="admin@acme.example",
        )
        audience = set(_audience(db))
    finally:
        db.close()

    assert audience == {"hr@acme.example"}


# ── Who is never read: fixtures, outsiders and leavers ──────────────────────
# Open to everyone in the domain means the domain: the same guards every
# sibling roster applies (``loop_closure.roster``, ``priorities.batch``). A
# fixture or an off-domain address has no Workspace mailbox, so each nightly
# Gmail, Calendar and Drive call for it fails ``invalid_grant``.

FIXTURES = (
    "impersonated_user@sales.local",
    "impersonated_user@systems.local",
    "seed@example.com",
)
OUTSIDER = "vendor@outside.example"


def _with_strangers(db_factory):
    db = db_factory()
    try:
        for n, email in enumerate((*FIXTURES, OUTSIDER), start=10):
            db.add(User(email=email, id=f"u{n}", name=f"Stranger {n}"))
        db.commit()
    finally:
        db.close()


def _audience_of(db_factory, **kwargs) -> set[str]:
    db = db_factory()
    try:
        return set(_audience(db, **kwargs))
    finally:
        db.close()


STAFF = {"rep1@acme.example", "rep2@acme.example", "hr@acme.example"}


def test_seed_and_impersonation_fixtures_are_left_out(db_factory):
    _with_strangers(db_factory)
    audience = _audience_of(db_factory)

    assert not audience & set(FIXTURES)
    assert audience == STAFF


def test_someone_off_the_workspace_domain_is_left_out(db_factory):
    _with_strangers(db_factory)

    assert OUTSIDER not in _audience_of(db_factory)


def test_the_control_domain_is_honoured_too(db_factory, set_test_workspace_domains):
    """The control workspace's domain keeps outsiders out."""
    _with_strangers(db_factory)
    set_test_workspace_domains(DOMAIN)

    assert _audience_of(db_factory) == STAFF


def test_with_no_control_domain_the_audience_is_empty(
    db_factory, set_test_workspace_domains
):
    """A workspace without verified domains must read no mailbox."""
    _with_strangers(db_factory)
    db = db_factory()
    try:
        db.add(User(email="vendor@partner.alt", id="u99", name="Partner"))
        db.commit()
    finally:
        db.close()
    set_test_workspace_domains()

    assert _audience_of(db_factory) == set()


def test_someone_who_has_left_is_left_out(db_factory):
    asked: list[list[str]] = []

    def leavers(emails):
        asked.append(sorted(emails))
        return {" REP2@acme.example "}

    assert _audience_of(db_factory, deactivated_lookup=leavers) == STAFF - {
        "rep2@acme.example"
    }
    # Asked once, about the people who passed every other guard.
    assert asked == [sorted(STAFF)]


def test_a_failing_leaver_lookup_keeps_everyone(db_factory, caplog):
    def outage(_emails):
        raise RuntimeError("Slack is down")

    with caplog.at_level("WARNING", logger="backend.agents.multi_source_priorities"):
        assert _audience_of(db_factory, deactivated_lookup=outage) == STAFF
    assert "Slack is down" in caplog.text


def test_the_nightly_run_reads_no_mailbox_for_a_stranger_or_a_leaver(db_factory):
    _with_strangers(db_factory)
    hubspot = _FakeSource("hubspot", {})
    gmail = _FakeSource("gmail", {})
    calendar = _FakeSource("calendar", {})
    judge = MagicMock()
    judge.judge.return_value = []

    plugin = MultiSourcePrioritiesPlugin(
        sources=[hubspot, gmail, calendar],
        judge=judge,
        deactivated_lookup=lambda _emails: {"hr@acme.example"},
    )
    result = OvernightAgent(plugins=[plugin], db_factory=db_factory).run()

    assert result.errors == []
    read = {"rep1@acme.example", "rep2@acme.example"}
    for source in (hubspot, gmail, calendar):
        assert {email for email, _since in source.calls} == read

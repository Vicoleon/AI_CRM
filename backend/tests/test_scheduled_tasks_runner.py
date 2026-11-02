"""Scheduled tasks — executing one run.

The headline case is ``test_a_polite_apology_is_not_a_result``: the
orchestrator never raises, it catches everything and returns a friendly
sentence AS THE ANSWER, so "we got a string back" is not success. Delivering
one of those would DM the user "I encountered an unexpected issue" and record
a green run.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from rubik_core.auth.registry import UserProfile
from rubik_core.config import settings
from rubik_core.database.models import (
    Base,
    ChatMessage,
    ChatSession,
    InteractionLog,
    Notification,
)
from rubik_core.orchestration.models import (
    ExecutionTrace,
    OrchestrationResult,
    RouteDecision,
    ToolExecutionResult,
)
from rubik_core.scheduled_tasks import runner, store
from rubik_core.scheduled_tasks import schedule as sched

OWNER = "BEllison@acme.example"  # deliberately mixed case — see the email test


def _profile(email: str = OWNER) -> UserProfile:
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name="Blair Ellison",
        department="Systems",
        roles=["user"],
        is_exec=False,
        managed_departments=[],
        created_at="2026-09-01T00:00:00",
        updated_at="2026-09-01T00:00:00",
    )


def _result(
    answer: str = "Three emails need a reply today.",
    *,
    steps=None,
    warnings=None,
) -> OrchestrationResult:
    return OrchestrationResult(
        answer=answer,
        citations=[{"source_path": "gmail://thread/1", "snippet": "…"}],
        trace=ExecutionTrace(
            request_id="req-1",
            route=RouteDecision(
                intent="EMAIL_READ",
                explanation="",
                confidence=1.0,
                metadata={"warnings": warnings or []},
            ),
            steps=steps or [],
            total_duration_ms=1200,
        ),
        prompt_tokens=900,
        completion_tokens=120,
        total_tokens=1020,
    )


class _FakeOrchestrator:
    """Stands in for ADKOrchestrator: same shape, no Vertex."""

    def __init__(self, result=None, raises=None):
        self.model_id = "gemini-3.6-flash"
        self._result = result if result is not None else _result()
        self._raises = raises
        self.calls: list[dict] = []
        self.headless = None

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return self._result


@pytest.fixture
def db(monkeypatch, tmp_path):
    # A FILE-backed sqlite db, not ":memory:" + StaticPool. The code under test
    # opens its own short-lived sessions (as it does in production), and
    # StaticPool pins every one of them to a SINGLE connection — so two
    # overlapping transactions raise "cannot start a transaction within a
    # transaction". That surfaced only on CI, only in a full-suite run, because
    # it needs another session to be mid-transaction. A file gives each session
    # its own connection, which is the production shape.
    engine = create_engine(
        f"sqlite:///{tmp_path}/scheduled_tasks.db",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr("rubik_core.database.db.SessionLocal", Session)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def _baseline(monkeypatch):
    monkeypatch.setattr(settings, "SCHEDULED_TASKS_ENABLED", True)
    monkeypatch.setattr(settings, "SCHEDULED_TASKS_MAX_CONSECUTIVE_FAILURES", 5)
    monkeypatch.setattr(settings, "SCHEDULED_TASKS_RUN_TIMEOUT_SECONDS", 300)
    # Resolve identity and the gate by default; individual tests override.
    monkeypatch.setattr(runner, "_resolve_profile", lambda email: _profile(email))
    monkeypatch.setattr(runner, "_feature_allowed", lambda profile: True)


@pytest.fixture
def orchestrator(monkeypatch):
    """Patch the builder BY STRING so the runner's deferred import sees it."""
    fake = _FakeOrchestrator()

    def _build(*_args, **kwargs):
        fake.headless = kwargs.get("headless")
        return fake

    monkeypatch.setattr(
        "backend.services.chat_orchestrator.build_chat_orchestrator", _build
    )
    return fake


@pytest.fixture
def delivered(monkeypatch):
    """Record deliveries instead of hitting Slack, Gmail or the bell."""
    calls: list[dict] = []

    def _deliver(**kwargs):
        calls.append(kwargs)
        return {"chat": "sent", "slack": "sent"}

    monkeypatch.setattr("rubik_core.scheduled_tasks.delivery.deliver", _deliver)
    return calls


def _make_run(db, *, kind="weekdays", channels=("slack",)):
    spec = (
        sched.validate_schedule(
            schedule_kind=kind,
            timezone_name="America/Chicago",
            time_local="08:00",
            now_utc=datetime(2026, 9, 8, 12, tzinfo=timezone.utc),
        )
        if kind != "once"
        else sched.validate_schedule(
            schedule_kind="once",
            timezone_name="America/Chicago",
            run_at_local="2099-01-01T08:00",
            now_utc=datetime(2026, 9, 8, 12, tzinfo=timezone.utc),
        )
    )
    task = store.create_task(
        db,
        user_email=OWNER,
        title="Morning important emails",
        prompt="Summarize my unread email from the last 24 hours.",
        schedule=spec,
        next_run_at=datetime(2026, 9, 10, 13, 0),
        delivery_channels=list(channels),
    )
    folder_id = store.ensure_results_folder(db, OWNER)
    run = store.create_run(
        db, task, scheduled_for=datetime(2026, 9, 9, 13, 0), chat_folder_id=folder_id
    )
    db.commit()
    return task, run


# ── the happy path ──────────────────────────────────────────────────────────


def test_a_successful_run_becomes_a_conversation_and_a_delivery(
    db, orchestrator, delivered
):
    task, run = _make_run(db)

    outcome = runner.execute_run(run.id)

    # Surface WHY on failure: the runner records the reason on the run row, and
    # a bare "succeeded != failed" tells the next person nothing.
    assert outcome.status == "succeeded", f"run failed: {outcome.reason!r}"
    # End this session's read transaction before asserting on rows another
    # session wrote: expire_all() marks objects stale but leaves the
    # transaction (and its snapshot) open, which is how this test went
    # green locally and flaky on CI. rollback() does both, and there is
    # nothing to lose — the setup above is already committed.
    db.rollback()

    fresh_run = store.get_run(db, run.id)
    assert fresh_run.status == "succeeded"
    assert fresh_run.chat_session_id == outcome.chat_session_id
    assert fresh_run.delivery == {"chat": "sent", "slack": "sent"}

    session = db.get(ChatSession, outcome.chat_session_id)
    # In the user's "Scheduled tasks" folder, dated, and owned by THEM.
    assert session.folder_id == store.ensure_results_folder(db, OWNER)
    assert session.title.startswith("Morning important emails — ")
    assert session.user_email == OWNER

    # Read the conversation through a session opened AFTER the run, the same
    # way the app does. The runner writes through its own sessions, and asking
    # a session that was already open whether those rows exist depends on when
    # its read transaction began — which is how this passed locally for weeks
    # and failed once on a slower CI box.
    from rubik_core.database.db import SessionLocal  # patched to the test engine

    with SessionLocal() as fresh:
        messages = (
            fresh.query(ChatMessage)
            .filter(ChatMessage.session_id == session.id)
            .order_by(ChatMessage.created_at)
            .all()
        )
        # The prompt, then the answer — so the user can read both and continue.
        assert [m.role for m in messages] == ["user", "assistant"]
        assert messages[0].content == task.prompt
        assert messages[1].content == "Three emails need a reply today."
        assert messages[1].metadata_json["citations"]
        assert messages[1].metadata_json["scheduled_task_id"] == task.id

    log = db.query(InteractionLog).one()
    assert log.mode == "scheduled"
    assert log.total_tokens == 1020

    fresh_task = store.get_task(db, task.id)
    assert fresh_task.run_count == 1
    assert fresh_task.last_run_status == "succeeded"
    assert fresh_task.claimed_at is None


def test_the_run_is_built_headless_with_the_task_named(db, orchestrator, delivered):
    _task, run = _make_run(db)
    runner.execute_run(run.id)

    # Headless, so no confirmation-gated tools and no sub-agents exist to reach.
    assert orchestrator.headless is not None
    assert orchestrator.headless.title == "Morning important emails"
    call = orchestrator.calls[0]
    # No conversation history and no DB session: a scheduled prompt stands alone
    # and there is no HITL state to park.
    assert call["history"] == []
    assert call["db_session"] is None
    assert call["mode"] == "scheduled"


def test_a_one_off_completes_after_it_runs(db, orchestrator, delivered):
    task, run = _make_run(db, kind="once")
    runner.execute_run(run.id)
    db.rollback()
    fresh = store.get_task(db, task.id)
    assert fresh.status == "completed"
    assert fresh.next_run_at is None


def test_the_cost_is_booked_against_this_feature_not_chat(
    db, orchestrator, delivered, monkeypatch
):
    recorded: list[dict] = []
    monkeypatch.setattr(
        "rubik_core.cost.usage.record_usage", lambda **kw: recorded.append(kw)
    )
    _task, run = _make_run(db)
    runner.execute_run(run.id)

    assert len(recorded) == 1
    assert recorded[0]["feature"] == "scheduled_tasks"
    # The model that actually RAN, not a hardcoded tier.
    assert recorded[0]["model"] == "gemini-3.6-flash"
    assert recorded[0]["total_tokens"] == 1020
    assert recorded[0]["user_email"] == OWNER


# ── failures that look like successes ───────────────────────────────────────


def test_a_polite_apology_is_not_a_result(db, monkeypatch, delivered):
    """The orchestrator swallows exceptions and returns a friendly sentence.

    Delivering that would DM the user an apology and record a green run.
    """
    failed = _result(
        answer="I encountered an unexpected issue. Please try again.",
        steps=[
            ToolExecutionResult(
                tool_name="ADK_EXECUTION",
                status="error",
                output=None,
                error="[agent_runtime_error] boom",
                latency_ms=10,
            )
        ],
    )
    fake = _FakeOrchestrator(result=failed)
    monkeypatch.setattr(
        "backend.services.chat_orchestrator.build_chat_orchestrator",
        lambda *a, **k: fake,
    )
    _task, run = _make_run(db)

    outcome = runner.execute_run(run.id)

    assert outcome.status == "failed"
    assert delivered == []  # nothing was sent
    db.rollback()
    assert store.get_run(db, run.id).status == "failed"
    # No assistant message either: a failed run leaves no fake answer behind.
    assert db.query(ChatMessage).filter(ChatMessage.role == "assistant").count() == 0
    # And no question-only conversation left sitting in their chat folder,
    # looking like a chat Cubie ignored.
    assert db.query(ChatSession).count() == 0
    assert store.get_run(db, run.id).chat_session_id is None


@pytest.mark.parametrize(
    ("answer", "warnings"),
    [
        ("", ["empty_agent_result"]),
        ("That question sent me in circles.", ["AGENT_LOOP_LIMIT: too many"]),
        ("", []),
    ],
)
def test_an_empty_or_looping_run_fails(db, monkeypatch, delivered, answer, warnings):
    fake = _FakeOrchestrator(result=_result(answer=answer, warnings=warnings))
    monkeypatch.setattr(
        "backend.services.chat_orchestrator.build_chat_orchestrator",
        lambda *a, **k: fake,
    )
    _task, run = _make_run(db)
    assert runner.execute_run(run.id).status == "failed"
    assert delivered == []


def test_a_timeout_fails_the_run_rather_than_hanging_the_task(
    db, monkeypatch, delivered
):
    import asyncio

    fake = _FakeOrchestrator(raises=asyncio.TimeoutError())
    monkeypatch.setattr(
        "backend.services.chat_orchestrator.build_chat_orchestrator",
        lambda *a, **k: fake,
    )
    _task, run = _make_run(db)
    outcome = runner.execute_run(run.id)
    assert outcome.status == "failed"
    db.rollback()
    assert store.get_run(db, run.id).status == "failed"


def test_an_unexpected_explosion_is_recorded_not_raised(db, monkeypatch, delivered):
    fake = _FakeOrchestrator(raises=RuntimeError("vertex is on fire"))
    monkeypatch.setattr(
        "backend.services.chat_orchestrator.build_chat_orchestrator",
        lambda *a, **k: fake,
    )
    _task, run = _make_run(db)
    outcome = runner.execute_run(run.id)
    assert outcome.status == "failed"
    assert "vertex is on fire" in outcome.reason


# ── retries, identity and access ────────────────────────────────────────────


def test_a_retried_job_task_exits_without_re_delivering(db, orchestrator, delivered):
    """Cloud Run re-runs a crashed task; the claim is what makes that safe."""
    _task, run = _make_run(db)
    runner.execute_run(run.id)
    assert len(delivered) == 1

    again = runner.execute_run(run.id)

    assert again.status == "already_claimed"
    assert len(delivered) == 1  # not delivered twice


def test_a_departed_owner_pauses_the_task_rather_than_failing_forever(
    db, orchestrator, delivered, monkeypatch
):
    monkeypatch.setattr(runner, "_resolve_profile", lambda email: None)
    task, run = _make_run(db)

    outcome = runner.execute_run(run.id)

    assert outcome.status == "skipped"
    assert outcome.reason == "user_missing"
    assert delivered == []
    db.rollback()
    fresh = store.get_task(db, task.id)
    assert fresh.status == "paused"
    assert "no longer has a Cubie account" in fresh.paused_reason


def test_a_denied_gate_skips_the_run_but_does_not_pause_immediately(
    db, orchestrator, delivered, monkeypatch
):
    """The gate fails safe to the shipped roster, so a single denial may just
    be a transient DB read — pausing on it would switch off a working task."""
    monkeypatch.setattr(runner, "_feature_allowed", lambda profile: False)
    task, run = _make_run(db)

    outcome = runner.execute_run(run.id)

    assert (outcome.status, outcome.reason) == ("skipped", "access_denied")
    assert delivered == []
    db.rollback()
    fresh = store.get_task(db, task.id)
    assert fresh.status == "active"  # still scheduled
    assert fresh.consecutive_failures == 1  # but the streak is counting


def test_a_persistently_failing_task_pauses_itself_and_tells_the_owner(
    db, monkeypatch, delivered
):
    monkeypatch.setattr(settings, "SCHEDULED_TASKS_MAX_CONSECUTIVE_FAILURES", 1)
    fake = _FakeOrchestrator(raises=RuntimeError("still broken"))
    monkeypatch.setattr(
        "backend.services.chat_orchestrator.build_chat_orchestrator",
        lambda *a, **k: fake,
    )
    task, run = _make_run(db)

    runner.execute_run(run.id)

    db.rollback()
    assert store.get_task(db, task.id).status == "error_paused"
    note = db.query(Notification).one()
    assert note.actor_email == OWNER
    assert note.kind == "scheduled_task_result"
    assert "paused" in note.title.lower()
    assert note.severity == "warn"


def test_a_missing_run_is_reported_not_crashed(db):
    assert runner.execute_run("no-such-run").status == "failed"

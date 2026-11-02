"""Regression tests for the empty-briefing delivery gate.

Real incident: Users in whole departments received daily briefings with ZERO
priorities while 30-50 channel signals existed — the decision stage returned
a confident zero (twice: initial + critic revision), and the batch persisted
and delivered it as success. These tests pin the guard rails that make that
impossible:

  * ``decide()`` raises :class:`DecisionLLMUnavailable` instead of silently
    returning ``[]`` when the LLM call/parse fails.
  * a zero-result first pass WITH signals triggers the zero-repair pass.
  * a critic revision can shrink the list but never erase it.
  * ``empty_with_signals`` bundles are NEVER persisted; the user run FAILS.
  * genuinely quiet days (zero signals) persist as ``no_signals`` successes.
  * ``run_batch`` retries failed users once.

All DB/LLM access is monkeypatched — nothing here touches Cloud SQL/Vertex.
"""

from datetime import datetime, timezone

import pytest

import rubik_core.priorities as priorities_pkg
import rubik_core.priorities.temporal as temporal_mod
from rubik_core.priorities import batch
from rubik_core.priorities import models as m
from rubik_core.priorities import orchestrator as orch_mod
from rubik_core.priorities.agents.decision_maker import DecisionLLMUnavailable
from rubik_core.priorities.temporal import TemporalContext


def _temporal() -> TemporalContext:
    now = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
    return TemporalContext(now_utc=now, now_local=now, tz_name="UTC")


def _signal(sig_id: str = "s1") -> m.ChannelSignal:
    return m.ChannelSignal(
        id=sig_id, channel=m.ChannelKind.GMAIL, title="thread", snippet="x"
    )


def _candidate() -> m.PriorityCandidate:
    # priority_kind=None → backward-debt branch of the temporal filter with
    # low urgency: survives _drop_past_anchored without needing anchors.
    return m.PriorityCandidate(
        title="Reply to the oldest waiting thread",
        why_it_matters="w",
        urgency="low",
        signal_ids=["s1"],
    )


def _build_orchestrator(monkeypatch, decide_fn, critic_report, signals):
    """PrioritiesOrchestrator with every external dependency stubbed."""
    o = orch_mod.PrioritiesOrchestrator(config=orch_mod.PrioritiesConfig.default())

    monkeypatch.setattr(orch_mod, "resolve_user_now", lambda *a, **k: _temporal())
    monkeypatch.setattr(orch_mod, "_resolve_user_department", lambda e: None)
    monkeypatch.setattr(orch_mod, "_resolve_playbook_text", lambda d: "")
    monkeypatch.setattr(orch_mod, "_resolve_wgll_text", lambda s, e: "")
    monkeypatch.setattr(
        orch_mod, "_resolve_user_priorities_prefs", lambda e: (None, [], None)
    )

    digest = m.ChannelDigest(channel=m.ChannelKind.GMAIL, signals=list(signals))
    o._run_channels = lambda req, audit, temporal: [digest]
    o.summarizer.consolidate = lambda digests, temporal=None: m.ConsolidatedDigest(
        narrative="n.", all_signals=list(signals)
    )

    class _FakeDecisionMaker:
        decide = staticmethod(decide_fn)

    o.decision_maker = _FakeDecisionMaker()
    o.critic.review = lambda cons, pris, temporal=None: critic_report
    o._load_memory = lambda e: {}
    o._apply_memory_filter = lambda p, mem, signal_by_id: (p, [])
    o._record_memory = lambda e, p: None
    return o


def _request() -> m.PrioritiesRequest:
    return m.PrioritiesRequest(user_email="u@x.example")


# ── orchestrator guard rails ─────────────────────────────────────────────────


def test_zero_repair_rescues_empty_decision(monkeypatch):
    calls: list[dict] = []

    def decide(digest, **kw):
        calls.append(kw)
        return [_candidate()] if kw.get("zero_repair") else []

    critic = m.CritiqueReport(overall_score=9.0, needs_revision=False)
    o = _build_orchestrator(monkeypatch, decide, critic, signals=[_signal()])

    bundle = o.generate(_request())

    assert len(bundle.priorities) == 1
    assert bundle.generation_status == m.GENERATION_OK
    assert not calls[0].get("zero_repair")
    assert calls[1]["zero_repair"] is True
    assert any("zero-repair" in ln for ln in bundle.audit_trail)


def test_revise_returning_zero_keeps_first_pass(monkeypatch):
    def decide(digest, **kw):
        return [] if kw.get("previous_attempt") else [_candidate()]

    critic = m.CritiqueReport(overall_score=2.0, needs_revision=True)
    o = _build_orchestrator(monkeypatch, decide, critic, signals=[_signal()])

    bundle = o.generate(_request())

    assert len(bundle.priorities) == 1
    assert bundle.revision_count == 1
    assert bundle.generation_status == m.GENERATION_OK
    assert any("never erase" in ln for ln in bundle.audit_trail)


def test_no_signals_skips_decision_stage(monkeypatch):
    def decide(digest, **kw):  # pragma: no cover — must not run
        raise AssertionError("decision-maker must not be called with 0 signals")

    critic = m.CritiqueReport(overall_score=9.0, needs_revision=False)
    o = _build_orchestrator(monkeypatch, decide, critic, signals=[])

    bundle = o.generate(_request())

    assert bundle.priorities == []
    assert bundle.generation_status == m.GENERATION_NO_SIGNALS
    assert bundle.last_critique_score is None


def test_decision_llm_failure_raises(monkeypatch):
    def decide(digest, **kw):
        raise DecisionLLMUnavailable("vertex down")

    critic = m.CritiqueReport(overall_score=9.0, needs_revision=False)
    o = _build_orchestrator(monkeypatch, decide, critic, signals=[_signal()])

    with pytest.raises(DecisionLLMUnavailable):
        o.generate(_request())


def test_persistent_zero_marks_empty_with_signals(monkeypatch):
    def decide(digest, **kw):
        return []

    critic = m.CritiqueReport(overall_score=9.0, needs_revision=False)
    o = _build_orchestrator(monkeypatch, decide, critic, signals=[_signal()])

    bundle = o.generate(_request())

    assert bundle.priorities == []
    assert bundle.generation_status == m.GENERATION_EMPTY_WITH_SIGNALS
    assert bundle.last_critique_score is None  # critic never ran on empty


def test_filtered_to_empty_is_legitimate_not_failure(monkeypatch):
    """A healthy decision list emptied by the deterministic post-filters
    (user resolved/snoozed everything) is filtered_empty — deliverable, and
    never the nightly false-failure the review workflow flagged."""

    def decide(digest, **kw):
        return [_candidate()]

    critic = m.CritiqueReport(overall_score=9.0, needs_revision=False)
    o = _build_orchestrator(monkeypatch, decide, critic, signals=[_signal()])
    o._apply_memory_filter = lambda p, mem, signal_by_id: (
        [],
        ["post-filter dropped: already handled"],
    )

    bundle = o.generate(_request())

    assert bundle.priorities == []
    assert bundle.generation_status == m.GENERATION_FILTERED_EMPTY


def test_all_channels_errored_is_outage_not_quiet_day(monkeypatch):
    def decide(digest, **kw):  # pragma: no cover — must not run
        raise AssertionError("decision-maker must not be called with 0 signals")

    critic = m.CritiqueReport(overall_score=9.0, needs_revision=False)
    o = _build_orchestrator(monkeypatch, decide, critic, signals=[])
    o._run_channels = lambda req, audit, temporal: [
        m.ChannelDigest(channel=m.ChannelKind.GMAIL, error="agent_crashed: boom"),
        m.ChannelDigest(
            channel=m.ChannelKind.CALENDAR, error="channel_timeout: exceeded 90s"
        ),
    ]

    bundle = o.generate(_request())

    assert bundle.priorities == []
    assert bundle.generation_status == m.GENERATION_EMPTY_WITH_SIGNALS


# ── batch delivery gate ──────────────────────────────────────────────────────


def _patch_batch_orchestrator(monkeypatch, bundle):
    class _FakeOrch:
        def generate(self, request):
            bundle.user_email = request.user_email
            return bundle

    persisted: list[tuple] = []
    monkeypatch.setattr(priorities_pkg, "PrioritiesOrchestrator", _FakeOrch)
    monkeypatch.setattr(
        priorities_pkg,
        "persist_bundle",
        lambda *a, **k: persisted.append((a, k)) or "card-id",
    )
    monkeypatch.setattr(
        temporal_mod, "resolve_user_now", lambda email, **kw: _temporal()
    )
    return persisted


def test_empty_with_signals_is_failed_and_not_persisted(monkeypatch):
    bundle = m.PrioritiesBundle(
        user_email="u@x.example",
        generation_status=m.GENERATION_EMPTY_WITH_SIGNALS,
    )
    persisted = _patch_batch_orchestrator(monkeypatch, bundle)

    res = batch.run_priorities_for_user("u@x.example")

    assert res.succeeded is False
    assert "empty_with_signals" in (res.error or "")
    assert persisted == []


def test_no_signals_quiet_day_persists_as_success(monkeypatch):
    bundle = m.PrioritiesBundle(
        user_email="u@x.example",
        generation_status=m.GENERATION_NO_SIGNALS,
    )
    persisted = _patch_batch_orchestrator(monkeypatch, bundle)

    res = batch.run_priorities_for_user("u@x.example")

    assert res.succeeded is True
    assert res.priorities_count == 0
    assert len(persisted) == 1


def test_ok_bundle_persists_normally(monkeypatch):
    bundle = m.PrioritiesBundle(
        user_email="u@x.example",
        priorities=[_candidate()],
        generation_status=m.GENERATION_OK,
    )
    persisted = _patch_batch_orchestrator(monkeypatch, bundle)

    res = batch.run_priorities_for_user("u@x.example")

    assert res.succeeded is True
    assert res.priorities_count == 1
    assert len(persisted) == 1


def test_filtered_empty_bundle_persists_as_success(monkeypatch):
    bundle = m.PrioritiesBundle(
        user_email="u@x.example",
        generation_status=m.GENERATION_FILTERED_EMPTY,
    )
    persisted = _patch_batch_orchestrator(monkeypatch, bundle)

    res = batch.run_priorities_for_user("u@x.example")

    assert res.succeeded is True
    assert res.priorities_count == 0
    assert len(persisted) == 1


def test_run_batch_skips_retry_on_systemic_outage(monkeypatch):
    attempts: dict[str, int] = {}

    def fake_run(email, timeout_seconds=0, report_kind="daily"):
        attempts[email] = attempts.get(email, 0) + 1
        return batch.PrioritiesUserResult(
            user_email=email, succeeded=False, error="boom"
        )

    monkeypatch.setattr(batch, "run_user_with_timeout", fake_run)

    summary = batch.run_batch("rid", ["a@x.example", "b@x.example"])

    assert summary.users_failed == 2
    assert all(n == 1 for n in attempts.values())  # no retry pass


def test_run_batch_does_not_retry_per_user_timeouts(monkeypatch):
    attempts: dict[str, int] = {}

    def fake_run(email, timeout_seconds=0, report_kind="daily"):
        attempts[email] = attempts.get(email, 0) + 1
        if email == "slow@x.example":
            return batch.PrioritiesUserResult(
                user_email=email, succeeded=False, error="timeout after 600s"
            )
        return batch.PrioritiesUserResult(
            user_email=email, succeeded=True, priorities_count=1
        )

    monkeypatch.setattr(batch, "run_user_with_timeout", fake_run)

    summary = batch.run_batch("rid", ["a@x.example", "b@x.example", "slow@x.example"])

    assert summary.users_failed == 1
    assert attempts["slow@x.example"] == 1  # a 600s hang is not worth re-burning


def test_run_batch_retries_failed_users_once(monkeypatch):
    attempts: dict[str, int] = {}

    def fake_run(email, timeout_seconds=0, report_kind="daily"):
        attempts[email] = attempts.get(email, 0) + 1
        ok = email == "good@x.example" or attempts[email] >= 2
        return batch.PrioritiesUserResult(
            user_email=email,
            succeeded=ok,
            priorities_count=1 if ok else 0,
            error=None if ok else "boom",
        )

    monkeypatch.setattr(batch, "run_user_with_timeout", fake_run)

    summary = batch.run_batch("rid", ["good@x.example", "flaky@x.example"])

    assert summary.users_succeeded == 2
    assert summary.users_failed == 0
    assert attempts["good@x.example"] == 1
    assert attempts["flaky@x.example"] == 2

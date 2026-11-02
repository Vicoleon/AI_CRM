"""UC-40.2/UC-40.3 — Orchestrator must return partial results on channel timeout.

Bug: the prior ``_run_channels`` implementation used ``with
concurrent.futures.ThreadPoolExecutor(...) as pool``. When the per-channel
timeout fired and ``as_completed`` raised TimeoutError, the exception
propagated, triggering the context manager's ``__exit__`` → ``shutdown(wait=True)``
→ orchestrator hangs for up to 10 minutes waiting for slow channels
(Python threads can't be killed). Net effect on a manual run:
every user "timed out" with a 9-10 minute wall clock despite a 30s
per-channel budget.

Fix: catch the TimeoutError inside _run_channels, record stragglers as
``error="channel_timeout: ..."``, return what we have, and call
``pool.shutdown(wait=False, cancel_futures=True)`` so the orchestrator
proceeds without blocking.

These tests pin that behavior — both the partial-results return AND
the wall-clock guarantee.
"""

from __future__ import annotations

import time

from rubik_core.priorities.config import PrioritiesConfig
from rubik_core.priorities.models import (
    ChannelDigest,
    ChannelKind,
    PrioritiesRequest,
)
from rubik_core.priorities.orchestrator import PrioritiesOrchestrator
from rubik_core.priorities.temporal import resolve_user_now


def _make_fast_agent(channel: ChannelKind, signals=None):
    """Factory: returns an `agent_cls(...)` callable that yields a mock
    whose `run()` returns a fast, successful digest."""

    def factory(user_email: str, config, temporal):
        class FastAgent:
            pass

        agent = FastAgent()
        agent.channel = channel
        agent.user_email = user_email
        agent.run = lambda: ChannelDigest(
            channel=channel,
            narrative="ok",
            signals=signals or [],
        )
        return agent

    return factory


def _make_slow_agent(channel: ChannelKind, sleep_seconds: float):
    """Factory: yields a mock agent whose `run()` sleeps then returns."""

    def factory(user_email: str, config, temporal):
        class SlowAgent:
            pass

        agent = SlowAgent()
        agent.channel = channel
        agent.user_email = user_email

        def _slow_run():
            time.sleep(sleep_seconds)
            return ChannelDigest(channel=channel, narrative="finally")

        agent.run = _slow_run
        return agent

    return factory


def test_uc40_3_run_channels_returns_partial_results_on_timeout(monkeypatch):
    """When the budget expires, fast channels return their digests + slow
    channels appear as `channel_timeout` errors. The function does NOT
    propagate the TimeoutError to the caller.
    """
    config = PrioritiesConfig.default()
    config.per_channel_timeout_seconds = 2  # tight budget for the test

    fast_gmail = _make_fast_agent(ChannelKind.GMAIL)
    fast_calendar = _make_fast_agent(ChannelKind.CALENDAR)
    slow_drive = _make_slow_agent(ChannelKind.DRIVE, sleep_seconds=30)
    slow_meet = _make_slow_agent(ChannelKind.MEET, sleep_seconds=30)
    slow_hubspot = _make_slow_agent(ChannelKind.HUBSPOT, sleep_seconds=30)

    fake_registry = {
        ChannelKind.GMAIL: fast_gmail,
        ChannelKind.CALENDAR: fast_calendar,
        ChannelKind.DRIVE: slow_drive,
        ChannelKind.MEET: slow_meet,
        ChannelKind.HUBSPOT: slow_hubspot,
    }
    monkeypatch.setattr(
        "rubik_core.priorities.orchestrator.CHANNEL_AGENT_REGISTRY",
        fake_registry,
    )

    orch = PrioritiesOrchestrator(config=config)
    audit: list[str] = []
    request = PrioritiesRequest(
        user_email="me@acme.example",
        channels=list(fake_registry.keys()),
    )

    temporal = resolve_user_now(request.user_email)
    start = time.monotonic()
    digests = orch._run_channels(request, audit, temporal)
    elapsed = time.monotonic() - start

    # Must return well within the budget + a small grace for thread teardown.
    # If shutdown(wait=False) is broken, this would take ~30s (the slow sleep).
    assert elapsed < 5.0, (
        f"_run_channels took {elapsed:.1f}s — shutdown is still waiting on slow threads"
    )

    # Must return exactly 5 digests (2 fast successes + 3 timeouts).
    assert len(digests) == 5
    by_channel = {d.channel: d for d in digests}

    # Fast ones completed cleanly.
    assert by_channel[ChannelKind.GMAIL].error is None
    assert by_channel[ChannelKind.CALENDAR].error is None
    assert by_channel[ChannelKind.GMAIL].narrative == "ok"

    # Slow ones marked as channel_timeout.
    for slow in (ChannelKind.DRIVE, ChannelKind.MEET, ChannelKind.HUBSPOT):
        assert by_channel[slow].error is not None
        assert "channel_timeout" in by_channel[slow].error
        assert "2" in by_channel[slow].error  # "exceeded 2s budget"


def test_uc40_3_run_channels_does_not_raise_on_all_timeouts(monkeypatch):
    """Even if EVERY channel times out, return 5 error-digests rather than
    raising. The decision-maker can then produce a "no signals today"
    bundle instead of the orchestrator crashing the per-user pipeline.
    """
    config = PrioritiesConfig.default()
    config.per_channel_timeout_seconds = 1

    fake_registry = {
        kind: _make_slow_agent(kind, sleep_seconds=30)
        for kind in (
            ChannelKind.GMAIL,
            ChannelKind.CALENDAR,
            ChannelKind.DRIVE,
            ChannelKind.MEET,
            ChannelKind.HUBSPOT,
        )
    }
    monkeypatch.setattr(
        "rubik_core.priorities.orchestrator.CHANNEL_AGENT_REGISTRY",
        fake_registry,
    )

    orch = PrioritiesOrchestrator(config=config)
    audit: list[str] = []
    request = PrioritiesRequest(
        user_email="me@acme.example",
        channels=list(fake_registry.keys()),
    )

    temporal = resolve_user_now(request.user_email)
    start = time.monotonic()
    digests = orch._run_channels(request, audit, temporal)
    elapsed = time.monotonic() - start

    assert elapsed < 5.0
    assert len(digests) == 5
    assert all(d.error and "channel_timeout" in d.error for d in digests)


def test_uc40_3_default_budget_is_90s():
    """UC-40.2: per-channel budget bumped 30 → 90 to cover real LLM extraction."""
    config = PrioritiesConfig.default()
    assert config.per_channel_timeout_seconds == 90

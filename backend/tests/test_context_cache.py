"""Item 5 wiring: ADKOrchestrator wraps the agent in a cache-configured App when
CHAT_CONTEXT_CACHE_ENABLED, and falls back to the plain agent runner when off.
No Vertex/DB/network — build_coordinator is stubbed with a real (lazy) Agent.
"""

from unittest.mock import patch

from google.adk.agents import Agent as RealAgent

from rubik_core.auth.registry import UserProfile
from rubik_core.identifiers import ADK_APP_NAME
from rubik_core.orchestration import adk_orchestrator as ao


def _user():
    return UserProfile(
        id="t",
        email="test@acme.example",
        name="Test",
        department="sales",
        roles=["user"],
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )


def _stub_agent(**kwargs):
    return RealAgent(name="c", model="gemini-flash-latest", instruction="x")


def test_app_built_with_cache_config_when_enabled(monkeypatch):
    monkeypatch.setattr(ao.settings, "CHAT_CONTEXT_CACHE_ENABLED", True)
    with patch.object(ao, "build_coordinator", _stub_agent):
        orch = ao.ADKOrchestrator(user=_user(), timezone="UTC")
    assert orch.app is not None
    assert orch.app.root_agent is orch.agent
    assert (
        orch.app.context_cache_config.ttl_seconds
        == ao.settings.CHAT_CONTEXT_CACHE_TTL_SECONDS
    )
    # _build_runner uses the cached App path.
    with patch.object(ao, "get_session_service", lambda: None):
        runner = orch._build_runner()
    assert runner.app_name == ADK_APP_NAME


def test_no_app_when_cache_disabled(monkeypatch):
    monkeypatch.setattr(ao.settings, "CHAT_CONTEXT_CACHE_ENABLED", False)
    with patch.object(ao, "build_coordinator", _stub_agent):
        orch = ao.ADKOrchestrator(user=_user(), timezone="UTC")
    assert orch.app is None
    # Falls back to the plain agent runner.
    with patch.object(ao, "get_session_service", lambda: None):
        runner = orch._build_runner()
    assert runner.app_name == ADK_APP_NAME

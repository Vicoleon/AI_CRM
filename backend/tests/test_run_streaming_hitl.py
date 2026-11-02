"""Unit tests for ADKOrchestrator.run_streaming — the HITL confirmation flow and
the normal streaming path — by mocking the ADK Runner to emit synthetic events.
No Vertex / DB / network.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from rubik_core.auth.registry import UserProfile
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


class _FakeConfirmation:
    def __init__(self, hint="Confirm this action?"):
        self.hint = hint
        self.payload = {}


class _FakeActions:
    def __init__(self, confirmations=None):
        self.requested_tool_confirmations = confirmations or {}


class _FakeFC:
    def __init__(self, name):
        self.name = name
        self.args = {}
        self.id = "fc-1"


class _FakePart:
    def __init__(self, text):
        self.text = text


class _FakeContent:
    def __init__(self, text):
        self.parts = [_FakePart(text)]


class _FakeEvent:
    def __init__(
        self,
        *,
        partial=False,
        final=False,
        actions=None,
        fcs=None,
        content=None,
        invocation_id="inv-1",
    ):
        self.partial = partial
        self._final = final
        self.actions = actions or _FakeActions()
        self._fcs = fcs or []
        self.content = content
        self.invocation_id = invocation_id
        self.usage_metadata = None

    def is_final_response(self):
        return self._final

    def get_function_calls(self):
        return self._fcs

    def get_function_responses(self):
        return []


class _FakeSession:
    def __init__(self):
        self.id = "sess"
        self.events = []


class _FakeSessionService:
    async def get_session(self, **kwargs):
        return _FakeSession()

    async def create_session(self, **kwargs):
        return _FakeSession()


def _runner_factory(events):
    class _FakeRunner:
        def __init__(self, **kwargs):
            pass

        async def run_async(self, **kwargs):
            for e in events:
                yield e

    return _FakeRunner


def _drive(events, *, query="hi", db_session=None):
    with (
        patch.object(ao, "Runner", _runner_factory(events)),
        patch.object(ao, "get_session_service", lambda: _FakeSessionService()),
        patch.object(ao, "build_coordinator", lambda **kwargs: MagicMock()),
    ):
        orch = ao.ADKOrchestrator(user=_user(), timezone="UTC")

        async def collect():
            out = []
            async for ev in orch.run_streaming(
                request_id="t", query=query, history=[], db_session=db_session
            ):
                out.append(ev)
            return out

        return asyncio.run(collect())


def test_run_streaming_hitl_parks_confirmation():
    """A confirmation event → the server-rendered card as the answer + parked
    invocation id, so the next turn can resume."""
    ev = _FakeEvent(
        actions=_FakeActions({"fc-1": _FakeConfirmation()}),
        fcs=[_FakeFC("calendar_create_event")],
        invocation_id="inv-42",
    )
    db_session = SimpleNamespace(last_adk_invocation_id=None)
    events = _drive([ev], query="book it", db_session=db_session)

    done = next(e for e in events if e["type"] == "done")
    assert done["awaiting_confirmation"] is True
    assert "Book a calendar event" in done["answer"]
    assert done["confirmation"]["actions"][0]["tool"] == "calendar_create_event"
    # Invocation parked for the next ("yes") turn to resume.
    assert db_session.last_adk_invocation_id == "inv-42"


def test_run_streaming_normal_clears_confirmation():
    """A normal streamed answer → tokens + done(awaiting_confirmation=False) and
    the parked invocation id is cleared."""
    events_in = [
        _FakeEvent(partial=True, content=_FakeContent("Hello ")),
        _FakeEvent(partial=True, content=_FakeContent("world")),
        _FakeEvent(final=True, content=_FakeContent("Hello world")),
    ]
    db_session = SimpleNamespace(last_adk_invocation_id="stale-inv")
    events = _drive(events_in, query="hi", db_session=db_session)

    tokens = [e["text"] for e in events if e["type"] == "token"]
    done = next(e for e in events if e["type"] == "done")
    assert "".join(tokens) == "Hello world"
    assert done["awaiting_confirmation"] is False
    assert done["answer"] == "Hello world"
    assert db_session.last_adk_invocation_id is None

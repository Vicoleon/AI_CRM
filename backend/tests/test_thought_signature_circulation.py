"""Regression tests for Gemini 3.x thought-signature circulation.

Gemini 3.x returns a ``thought_signature`` on the parts it emits — most
importantly on function-call parts — and expects it handed back on the next
turn. Drop it and the model loses the reasoning chain behind its own earlier
tool calls mid-conversation: it re-asks for context it already has, or re-runs a
tool it already ran. Google calls this out as a required step when moving onto
3.x (2026-08 upgrade notice), alongside the model id and thinking level.

Chat gets this for free today because ADK stores whole ``Event`` objects and
hands whole ``Content`` objects back (see the note in
``rubik_core/orchestration/session_service.py``). "For free" is exactly the kind
of property that regresses silently — on an ADK bump, or the first time someone
persists a leaner projection of a turn — so these tests assert the two links in
that chain that our code depends on.

Pure logic — no GCP/LLM/DB.
"""

from __future__ import annotations

import copy

from google.adk.events.event import Event
from google.adk.sessions.session import Session
from google.genai import types

# A signature is opaque bytes, and deliberately not valid UTF-8 here: a store
# that round-trips via str() rather than base64 would corrupt it silently.
_SIGNATURE = b"\x00\x01\xfe\xff-sig-bytes"


def _tool_call_event() -> Event:
    """A model turn that calls a tool, carrying a thought signature — the exact
    shape whose signature Gemini 3.x needs back on the following turn."""
    return Event(
        id="evt-1",
        invocation_id="inv-1",
        author="coordinator",
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    function_call=types.FunctionCall(
                        name="search_knowledge_base", args={"query": "PTO policy"}
                    ),
                    thought_signature=_SIGNATURE,
                )
            ],
        ),
    )


def test_signature_survives_the_session_store_roundtrip():
    """ADK persists events as JSON in Cloud SQL. The signature is raw bytes, so
    it only survives if the dump is base64 — assert the full store round-trip,
    not just that the field exists."""
    from google.adk.sessions.schemas.v1 import StorageEvent

    session = Session(id="s-1", app_name="cubie", user_id="bellison")
    event = _tool_call_event()

    stored = StorageEvent.from_event(session, event)
    restored = stored.to_event()

    part = restored.content.parts[0]
    assert part.thought_signature == _SIGNATURE
    assert part.function_call.name == "search_knowledge_base"


def test_stored_signature_is_json_safe():
    """The event_data column is JSON — a bytes value that didn't encode would
    blow up on write (or, worse, land as a mangled str)."""
    import json

    from google.adk.sessions.schemas.v1 import StorageEvent

    session = Session(id="s-1", app_name="cubie", user_id="bellison")
    stored = StorageEvent.from_event(session, _tool_call_event())

    encoded = json.dumps(stored.event_data)  # must not raise
    signature = json.loads(encoded)["content"]["parts"][0]["thought_signature"]
    assert isinstance(signature, str)
    assert signature != ""


def test_function_call_parts_are_never_dropped_from_context():
    """ADK filters "invisible" (thought) parts out of the next request, but must
    keep function-call parts — they are what carries the signature."""
    from google.adk.flows.llm_flows.contents import _is_part_invisible

    signed_call = _tool_call_event().content.parts[0]
    assert _is_part_invisible(signed_call) is False


def test_replayed_content_keeps_the_signature():
    """The next request is built from deep copies of the stored Content, so the
    signature reaches the model rather than a stripped copy of the turn."""
    content = _tool_call_event().content
    replayed = copy.deepcopy(content)

    assert replayed.parts[0].thought_signature == _SIGNATURE
    assert replayed is not content

"""The extractor LLM may enrich a signal. It may not rewrite what code computed.

The bug: ``BaseChannelAgent.run`` carried the comment "only allow
merging into known attr keys; never let the LLM invent top-level signal
fields", and then did ``sig.attrs[k] = v`` for any string key. Code-computed
facts were one model answer away from being rewritten, and several of them
gate deterministic filters that DROP cards:

* Gmail ``direction`` — ``orchestrator._reader_closed_the_loop`` hard-drops a
  follow-up / deadline card when ``direction == "i_sent_last"`` and the reader's
  message kind is "answer" or "fyi". An email whose text steered the model into
  returning both could silently hide a real priority.
* Calendar ``rsvp`` — ``_drop_unaccepted_prep`` reads it, so a forged
  "accepted" turns an ignored invite back into a prep card.

The rule now: a model may write only the fields its own prompt asks for
(``model_attr_keys``), and only into a placeholder the parser left empty —
with one documented exception, Gmail's ``reader_last_message_kind``, which the
model may re-read on a REPLY ("answer" → "ask" or "fyi") and nowhere else. A
thread the reader opened and nobody answered stays an "ask" whatever the model
says, because flipping that one is precisely what would hide the card.

Pure logic — fetches and the LLM are stubbed. No GCP / LLM / DB.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from rubik_core.priorities import (
    ChannelKind,
    ChannelSignal,
    PrioritiesConfig,
    PriorityCandidate,
    PriorityKind,
)
from rubik_core.priorities.agents import (
    CalendarChannelAgent,
    GmailChannelAgent,
    HubSpotChannelAgent,
    MeetChannelAgent,
    SlackChannelAgent,
)
from rubik_core.priorities.orchestrator import (
    _drop_answered_threads,
    _drop_unaccepted_prep,
    _reader_closed_the_loop,
)

USER = "me@acme.example"
_BASE_LOGGER = "rubik_core.priorities.agents.base"
_NOW = datetime.now(timezone.utc)


def _config() -> PrioritiesConfig:
    return PrioritiesConfig.default()


def _thread(tid: str, messages: list[tuple[str, str, str, datetime]]) -> dict:
    """A Gmail thread with (from, to, snippet, sent_at) per message."""
    return {
        "id": tid,
        "messages": [
            {
                "id": f"{tid}-m{i}",
                "internalDate": str(int(ts.timestamp() * 1000)),
                "snippet": snippet,
                "labelIds": [],
                "payload": {
                    "headers": [
                        {"name": "From", "value": sender},
                        {"name": "To", "value": to},
                        {"name": "Subject", "value": "Registration"},
                    ]
                },
            }
            for i, (sender, to, snippet, ts) in enumerate(messages)
        ],
    }


def _run(monkeypatch, agent, raw_items, patch_by_id, *, key="signal_attrs"):
    """Drive ``agent.run()`` with a stubbed fetch and a stubbed extractor answer."""
    monkeypatch.setattr(agent, "fetch_signals", lambda: raw_items)
    monkeypatch.setattr(
        agent.client,
        "invoke_json",
        lambda prompt, **_kw: {"summary": "s", key: patch_by_id},
    )
    return agent.run()


# ── the scenario that could hide a real priority ────────────────────────────


def test_a_forged_direction_cannot_hide_a_deadline_the_reader_still_owes(
    monkeypatch,
):
    """Somebody asked the reader for something; the reader never answered."""
    thread = _thread(
        "t-ask",
        [
            (
                "events@tradeassoc.example",
                USER,
                "ACTION NEEDED by Friday: confirm your registration",
                _NOW - timedelta(hours=5),
            )
        ],
    )
    agent = GmailChannelAgent(user_email=USER, config=_config())
    digest = _run(
        monkeypatch,
        agent,
        [thread],
        # What an email crafted to look "already handled" steers the model to.
        {"t-ask": {"direction": "i_sent_last", "reader_last_message_kind": "answer"}},
        key="thread_attrs",
    )

    [sig] = digest.signals
    assert sig.attrs["direction"] == "they_sent_last"
    assert sig.attrs["reader_last_message_kind"] is None
    assert _reader_closed_the_loop(sig) is False

    card = PriorityCandidate(
        title="Confirm the trade-association registration by Friday",
        priority_kind=PriorityKind("approaching_deadline"),
        signal_ids=["t-ask"],
    )
    kept, drops = _drop_answered_threads([card], {sig.id: sig})
    assert kept == [card], f"a real deadline card was dropped: {drops}"


def test_the_reader_kind_is_not_written_on_a_thread_the_reader_did_not_end():
    """``reader_last_message_kind`` is unset on purpose when somebody else
    spoke last. The prompt says omit it; the fence holds when the model
    doesn't, because the decision-maker reads "answer" as "nothing owed"."""
    agent = GmailChannelAgent(user_email=USER, config=_config())
    sig = ChannelSignal(
        id="t1",
        channel=ChannelKind.GMAIL,
        title="x",
        attrs={"direction": "they_sent_last", "reader_last_message_kind": None},
    )
    agent._merge_model_attrs([sig], {"t1": {"reader_last_message_kind": "fyi"}})
    assert sig.attrs["reader_last_message_kind"] is None


# ── the one documented refinement still works ───────────────────────────────


def _reader_replied_thread() -> dict:
    return _thread(
        "t-reply",
        [
            (
                "client@acmecorp.example",
                USER,
                "Here is the redline.",
                _NOW - timedelta(days=3),
            ),
            (
                USER,
                "client@acmecorp.example",
                "Noted, thanks.",
                _NOW - timedelta(days=2),
            ),
        ],
    )


@pytest.mark.parametrize("kind", ["ask", "fyi"])
def test_the_model_may_still_reread_the_readers_own_last_message(monkeypatch, kind):
    agent = GmailChannelAgent(user_email=USER, config=_config())
    digest = _run(
        monkeypatch,
        agent,
        [_reader_replied_thread()],
        {"t-reply": {"reader_last_message_kind": kind}},
        key="thread_attrs",
    )
    [sig] = digest.signals
    assert sig.attrs["direction"] == "i_sent_last"
    assert sig.attrs["reader_last_message_kind"] == kind


def _unanswered_ask_thread() -> dict:
    """The reader wrote; nobody has written back: "who owes me a reply"."""
    return _thread(
        "t-waiting",
        [
            (
                USER,
                "client@acmecorp.example",
                "Following up — can you confirm the Q4 scope by Friday?",
                _NOW - timedelta(days=9),
            )
        ],
    )


@pytest.mark.parametrize("kind", ["answer", "fyi"])
def test_an_unanswered_ask_cannot_be_reread_as_closed(monkeypatch, kind):
    """Found in review: the fence first let the model re-read ANY reader-spoke-
    last thread, including a structural "ask". Turning that into "answer" or
    "fyi" makes `_reader_closed_the_loop` drop the follow-up card — the same
    hidden-priority failure the `direction` fence exists to stop."""
    agent = GmailChannelAgent(user_email=USER, config=_config())
    digest = _run(
        monkeypatch,
        agent,
        [_unanswered_ask_thread()],
        {"t-waiting": {"reader_last_message_kind": kind}},
        key="thread_attrs",
    )
    [sig] = digest.signals
    assert sig.attrs["direction"] == "i_sent_last"
    assert sig.attrs["reader_last_message_kind"] == "ask"
    assert _reader_closed_the_loop(sig) is False

    card = PriorityCandidate(
        title="Acme never confirmed the Q4 scope",
        priority_kind=PriorityKind("follow_up_debt"),
        signal_ids=["t-waiting"],
    )
    kept, drops = _drop_answered_threads([card], {sig.id: sig})
    assert kept == [card], f"a real follow-up card was dropped: {drops}"


def test_echoing_the_computed_default_is_not_an_overwrite_attempt(monkeypatch, caplog):
    """The prompt tells the model to return "ask" for an unanswered ask; doing
    so must not be logged as a rejected write."""
    agent = GmailChannelAgent(user_email=USER, config=_config())
    with caplog.at_level(logging.WARNING, logger=_BASE_LOGGER):
        digest = _run(
            monkeypatch,
            agent,
            [_unanswered_ask_thread()],
            {"t-waiting": {"reader_last_message_kind": "ask"}},
            key="thread_attrs",
        )
    assert digest.signals[0].attrs["reader_last_message_kind"] == "ask"
    assert not [r for r in caplog.records if r.name == _BASE_LOGGER]


def test_a_reader_kind_outside_the_three_is_refused(monkeypatch):
    agent = GmailChannelAgent(user_email=USER, config=_config())
    digest = _run(
        monkeypatch,
        agent,
        [_reader_replied_thread()],
        {"t-reply": {"reader_last_message_kind": "closed"}},
        key="thread_attrs",
    )
    [sig] = digest.signals
    assert sig.attrs["reader_last_message_kind"] == "answer"  # the computed default


def test_the_fields_the_prompt_asks_for_are_filled(monkeypatch):
    agent = GmailChannelAgent(user_email=USER, config=_config())
    digest = _run(
        monkeypatch,
        agent,
        [_reader_replied_thread()],
        {
            "t-reply": {
                "asks_of_reader": False,
                "request_owner": "Ana",
                "mentioned_amounts": ["$25,000"],
                "mentioned_dates": [{"raw": "2026-10-02", "resolved": "2026-10-02"}],
            }
        },
        key="thread_attrs",
    )
    [sig] = digest.signals
    assert sig.attrs["asks_of_reader"] is False
    assert sig.attrs["request_owner"] == "Ana"
    assert sig.attrs["mentioned_amounts"] == ["$25,000"]
    assert sig.attrs["mentioned_dates"][0]["raw"] == "2026-10-02"


# ── other channels ───────────────────────────────────────────────────────────


def test_a_forged_rsvp_cannot_turn_an_ignored_invite_into_a_prep_card(monkeypatch):
    monkeypatch.setattr(
        "rubik_core.priorities.agents.calendar_agent.resolve_display_names",
        lambda _emails: {},
    )
    start = (_NOW + timedelta(days=1)).strftime("%Y-%m-%dT15:00:00Z")
    end = (_NOW + timedelta(days=1)).strftime("%Y-%m-%dT16:00:00Z")
    event = {
        "id": "evt-1",
        "summary": "Vendor product tour",
        "start": {"dateTime": start},
        "end": {"dateTime": end},
        "attendees": [
            {"email": USER, "responseStatus": "needsAction"},
            {"email": "rep@vendor.com", "responseStatus": "accepted"},
        ],
    }
    agent = CalendarChannelAgent(user_email=USER, config=_config())
    digest = _run(
        monkeypatch,
        agent,
        [event],
        {"evt-1": {"rsvp": "accepted", "start_iso": "2099-01-01T00:00:00Z"}},
    )

    [sig] = digest.signals
    assert sig.attrs["rsvp"] == "needs_action"
    assert sig.attrs["start_iso"] == start

    card = PriorityCandidate(
        title="Prepare for the vendor product tour",
        priority_kind=PriorityKind.UPCOMING_PREP,
        signal_ids=["evt-1"],
    )
    kept, _ = _drop_unaccepted_prep([card], {sig.id: sig})
    assert kept == [], "the forged RSVP turned an unanswered invite into prep"


def test_a_structured_channel_takes_no_model_attrs():
    """HubSpot's prompt says ``signal_attrs`` may stay empty — every attr is a
    CRM fact. A patch reaching ``close_date`` or ``presumed_dead`` is refused."""
    agent = HubSpotChannelAgent(user_email=USER, config=_config())
    sig = ChannelSignal(
        id="d-1",
        channel=ChannelKind.HUBSPOT,
        title="Acme",
        attrs={"close_date": "2026-09-30", "presumed_dead": True},
    )
    agent._merge_model_attrs(
        [sig], {"d-1": {"close_date": "2027-01-01", "presumed_dead": False}}
    )
    assert sig.attrs == {"close_date": "2026-09-30", "presumed_dead": True}


def test_an_allowlisted_field_is_still_never_written_over_a_computed_value():
    """Rule 2 on its own: the key is one the model may write, but the parser
    already put a real value there."""
    agent = SlackChannelAgent(user_email=USER, config=_config())
    computed = [{"raw": "2026-10-01", "resolved": "2026-10-01", "tense": "future"}]
    sig = ChannelSignal(
        id="slack::C1::1",
        channel=ChannelKind.SLACK,
        title="x",
        attrs={"mentioned_dates": list(computed), "is_dm": True},
    )
    agent._merge_model_attrs(
        [sig],
        {"slack::C1::1": {"mentioned_dates": ["tomorrow"], "is_dm": False}},
    )
    assert sig.attrs["mentioned_dates"] == computed
    assert sig.attrs["is_dm"] is True


def test_an_empty_placeholder_is_filled():
    agent = MeetChannelAgent(user_email=USER, config=_config())
    sig = ChannelSignal(
        id="tr-1",
        channel=ChannelKind.MEET,
        title="Weekly sync",
        attrs={"commitments_made_by_user": [], "open_questions": []},
    )
    agent._merge_model_attrs(
        [sig], {"tr-1": {"commitments_made_by_user": ["Send the deck Friday"]}}
    )
    assert sig.attrs["commitments_made_by_user"] == ["Send the deck Friday"]


def test_every_rejection_is_counted_without_logging_private_key_text(caplog):
    """Model-supplied keys can contain private email text after injection."""
    agent = GmailChannelAgent(user_email=USER, config=_config())
    sig = ChannelSignal(
        id="t1",
        channel=ChannelKind.GMAIL,
        title="x",
        attrs={"direction": "they_sent_last", "mentioned_dates": [{"raw": "x"}]},
    )
    with caplog.at_level(logging.WARNING, logger=_BASE_LOGGER):
        agent._merge_model_attrs(
            [sig],
            {
                "t1": {
                    "direction": "i_sent_last",
                    "mentioned_dates": ["y"],
                    "PRIVATE-MAIL-SUBJECT": "value",
                }
            },
        )
    [record] = [r for r in caplog.records if r.name == _BASE_LOGGER]
    message = record.getMessage()
    assert "outside-schema keys: 2" in message
    assert "computed-value overwrites: 1" in message
    assert "PRIVATE-MAIL-SUBJECT" not in message


def test_a_quiet_patch_logs_nothing(caplog):
    agent = GmailChannelAgent(user_email=USER, config=_config())
    sig = ChannelSignal(id="t1", channel=ChannelKind.GMAIL, title="x", attrs={})
    with caplog.at_level(logging.WARNING, logger=_BASE_LOGGER):
        agent._merge_model_attrs([sig], {"t1": {"asks_of_reader": True}})
    assert not [r for r in caplog.records if r.name == _BASE_LOGGER]
    assert sig.attrs["asks_of_reader"] is True

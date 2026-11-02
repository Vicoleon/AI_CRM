"""Cubie must not tell you to prep for meetings you never accepted.

An invite sitting on ``needsAction`` is usually a call you don't need to be on
(a routine rollout, a wide cc). Prepping every one of them buries the meetings
you actually committed to. So:

  * ``declined``     → never reaches the decision-maker at all
  * ``needs_action`` → barred from producing prep cards; reported ONCE as a
                       calendar-hygiene note so nothing goes silently missing
  * ``accepted`` / ``tentative`` / solo / self-organized → prep as before

The two gates that matter here are deterministic, not prompt-based: the
extractor drop and :func:`_drop_unaccepted_prep`. A prompt rule alone has
historically leaked (that's why ``_drop_past_anchored`` exists).
"""

from datetime import datetime, timezone

import pytest

from rubik_core.priorities.models import (
    ChannelKind,
    ChannelSignal,
    PriorityCandidate,
    PriorityKind,
)
from rubik_core.priorities.orchestrator import (
    _calendar_hygiene,
    _drop_unaccepted_prep,
)
from rubik_core.priorities.temporal import resolve_user_now
from rubik_core.tools.calendar import (
    RSVP_ACCEPTED,
    RSVP_COMMITTED,
    RSVP_DECLINED,
    RSVP_NEEDS_ACTION,
    RSVP_NONE,
    RSVP_TENTATIVE,
    self_response_status,
)

ME = "bellison@acme.example"
PACIFIC = "America/Los_Angeles"
NOW_UTC = datetime(2026, 8, 4, 17, 0, tzinfo=timezone.utc)  # 10:00 PDT


class StubClient:
    """Drop-in for LLMJsonClient that returns canned JSON, no network."""

    def __init__(self, payload):
        self.payload = payload
        self.calls: list[str] = []

    def invoke_json(self, prompt: str, fallback=None, **kwargs):
        self.calls.append(prompt)
        return self.payload


def _ctx():
    return resolve_user_now(now_utc=NOW_UTC, tz_name=PACIFIC)


def _invite(status: str, **extra) -> dict:
    """A calendar event where the reader's RSVP is ``status``."""
    return {
        "summary": "Acme rollout call",
        "attendees": [
            {"email": ME, "self": True, "responseStatus": status},
            {"email": "pm@acmecorp.example", "responseStatus": "accepted"},
        ],
        **extra,
    }


# ── self_response_status ─────────────────────────────────────────────────────


def test_reads_the_readers_own_rsvp_not_someone_elses():
    assert self_response_status(_invite("accepted"), ME) == RSVP_ACCEPTED
    assert self_response_status(_invite("tentative"), ME) == RSVP_TENTATIVE
    assert self_response_status(_invite("declined"), ME) == RSVP_DECLINED
    assert self_response_status(_invite("needsAction"), ME) == RSVP_NEEDS_ACTION


def test_matches_the_reader_by_email_when_google_omits_the_self_flag():
    ev = {
        "attendees": [
            {"email": "BEllison@ACME.example", "responseStatus": "needsAction"}
        ]
    }
    assert self_response_status(ev, ME) == RSVP_NEEDS_ACTION


def test_a_solo_event_has_no_rsvp_and_still_preps():
    # "Draft the board deck" — no invite list, nothing to accept. Filtering
    # these would silently gut prep for anyone who blocks their own work time.
    ev = {"summary": "Draft the board deck"}
    assert self_response_status(ev, ME) == RSVP_NONE
    assert self_response_status(ev, ME) in RSVP_COMMITTED


def test_your_own_meeting_preps_even_when_google_left_you_on_needsaction():
    """You don't RSVP to your own meeting.

    Events created through the API can leave the organizer on ``needsAction``.
    Dropping someone's own meeting is a far worse bug than prepping one extra.
    """
    via_event_organizer = _invite("needsAction", organizer={"email": ME, "self": True})
    assert self_response_status(via_event_organizer, ME) == RSVP_NONE

    via_attendee_flag = {
        "attendees": [
            {
                "email": ME,
                "self": True,
                "organizer": True,
                "responseStatus": "needsAction",
            }
        ]
    }
    assert self_response_status(via_attendee_flag, ME) == RSVP_NONE


def test_unparseable_input_preps_rather_than_hiding_a_real_meeting():
    # Never hide a meeting on uncertainty — the same principle the temporal
    # filter follows.
    assert (
        self_response_status({"attendees": [{"email": ME, "self": True}]}, ME)
        == RSVP_NONE
    )
    assert self_response_status({"attendees": ["garbage", None]}, ME) == RSVP_NONE
    assert self_response_status({}, ME) == RSVP_NONE
    assert self_response_status(_invite("accepted"), "") == RSVP_NONE


# ── extractor: declined never becomes a signal ───────────────────────────────


def _parse(raw_events: list[dict]) -> list[ChannelSignal]:
    from rubik_core.priorities.agents.calendar_agent import CalendarChannelAgent
    from rubik_core.priorities.config import PrioritiesConfig

    agent = CalendarChannelAgent(
        user_email=ME, config=PrioritiesConfig(), temporal=_ctx()
    )
    return agent.parse_signals(raw_events)


def test_declined_meetings_never_reach_the_pipeline():
    declined = _invite(
        "declined", id="ev-declined", start={"dateTime": "2026-08-05T17:00:00Z"}
    )
    kept = _invite(
        "accepted", id="ev-accepted", start={"dateTime": "2026-08-05T18:00:00Z"}
    )
    ids = [s.id for s in _parse([declined, kept])]
    assert ids == ["ev-accepted"]


def test_unanswered_meetings_stay_as_signals_but_are_tagged():
    """They must survive extraction — the hygiene note is built from them."""
    ev = _invite(
        "needsAction", id="ev-pending", start={"dateTime": "2026-08-05T17:00:00Z"}
    )
    (sig,) = _parse([ev])
    assert sig.attrs["rsvp"] == RSVP_NEEDS_ACTION


def test_accepted_and_solo_events_are_tagged_as_preppable():
    accepted = _invite(
        "accepted", id="ev-a", start={"dateTime": "2026-08-05T17:00:00Z"}
    )
    solo = {
        "id": "ev-solo",
        "summary": "Draft the board deck",
        "start": {"dateTime": "2026-08-05T19:00:00Z"},
        "end": {"dateTime": "2026-08-05T20:00:00Z"},
    }
    for sig in _parse([accepted, solo]):
        assert sig.attrs["rsvp"] in RSVP_COMMITTED


# ── _drop_unaccepted_prep ────────────────────────────────────────────────────


def _cal_signal(
    sid: str,
    rsvp: str,
    *,
    title: str = "Acme rollout call",
    start: str = "2026-08-05T17:00:00Z",
):
    return ChannelSignal(
        id=sid,
        channel=ChannelKind.CALENDAR,
        title=title,
        entity_id=sid,
        timestamp=start,
        attrs={
            "rsvp": rsvp,
            "start_iso": start,
            "when_local": "Wednesday Aug 5, 10:00 AM PDT",
        },
        link=f"https://calendar.google.com/event?eid={sid}",
    )


def _prep(pid: str, *signal_ids: str, kind=PriorityKind.UPCOMING_PREP):
    return PriorityCandidate(
        id=pid,
        title="Prepare for the Acme rollout call",
        why_it_matters="It's tomorrow morning.",
        urgency="medium",
        source_channels=[ChannelKind.CALENDAR],
        signal_ids=list(signal_ids),
        priority_kind=kind,
    )


def test_prep_for_an_unanswered_invite_is_dropped():
    sigs = {"ev-1": _cal_signal("ev-1", RSVP_NEEDS_ACTION)}
    kept, drops = _drop_unaccepted_prep([_prep("p1", "ev-1")], sigs)
    assert kept == []
    assert len(drops) == 1
    assert "never accepted" in drops[0]


def test_prep_for_an_accepted_or_tentative_meeting_survives():
    for status in (RSVP_ACCEPTED, RSVP_TENTATIVE, RSVP_NONE):
        sigs = {"ev-1": _cal_signal("ev-1", status)}
        kept, drops = _drop_unaccepted_prep([_prep("p1", "ev-1")], sigs)
        assert [p.id for p in kept] == ["p1"], f"{status} should still prep"
        assert drops == []


def test_legacy_prep_debt_is_gated_too():
    sigs = {"ev-1": _cal_signal("ev-1", RSVP_NEEDS_ACTION)}
    kept, _ = _drop_unaccepted_prep(
        [_prep("p1", "ev-1", kind=PriorityKind.PREP_DEBT)], sigs
    )
    assert kept == []


def test_a_prep_citing_one_accepted_meeting_survives_alongside_an_unanswered_one():
    """Mixed evidence means there IS a real commitment — keep it."""
    sigs = {
        "ev-yes": _cal_signal("ev-yes", RSVP_ACCEPTED),
        "ev-no": _cal_signal("ev-no", RSVP_NEEDS_ACTION),
    }
    kept, _ = _drop_unaccepted_prep([_prep("p1", "ev-yes", "ev-no")], sigs)
    assert [p.id for p in kept] == ["p1"]


def test_non_prep_priorities_are_untouched():
    """A follow-up you owe stays owed regardless of how you RSVP'd."""
    followup = PriorityCandidate(
        id="p2",
        title="Send Acme the pricing you promised",
        why_it_matters="You said you'd send it Monday.",
        urgency="high",
        source_channels=[ChannelKind.CALENDAR],
        signal_ids=["ev-1"],
        priority_kind=PriorityKind.PRE_TOUCHPOINT_FOLLOWUP,
    )
    sigs = {"ev-1": _cal_signal("ev-1", RSVP_NEEDS_ACTION)}
    kept, drops = _drop_unaccepted_prep([followup], sigs)
    assert [p.id for p in kept] == ["p2"]
    assert drops == []


def test_a_prep_with_no_calendar_evidence_is_left_to_the_temporal_filter():
    # Never drop on absence of evidence — this isn't a "prep for this invite" card.
    gmail_sig = ChannelSignal(id="t1", channel=ChannelKind.GMAIL, title="Prep thread")
    kept, drops = _drop_unaccepted_prep([_prep("p1", "t1")], {"t1": gmail_sig})
    assert [p.id for p in kept] == ["p1"]
    assert drops == []


# ── _calendar_hygiene ────────────────────────────────────────────────────────


def test_hygiene_note_lists_the_unanswered_invites():
    signals = [
        _cal_signal("ev-1", RSVP_NEEDS_ACTION, title="Acme rollout"),
        _cal_signal("ev-2", RSVP_ACCEPTED, title="Board sync"),
    ]
    note = _calendar_hygiene(signals, _ctx())
    assert note == {
        "unanswered_count": 1,
        "invites": [
            {
                "title": "Acme rollout",
                "when_local": "Wednesday Aug 5, 10:00 AM PDT",
                "link": "https://calendar.google.com/event?eid=ev-1",
            }
        ],
    }


def test_hygiene_note_is_none_when_everything_is_answered():
    signals = [_cal_signal("ev-1", RSVP_ACCEPTED), _cal_signal("ev-2", RSVP_TENTATIVE)]
    assert _calendar_hygiene(signals, _ctx()) is None
    assert _calendar_hygiene([], _ctx()) is None


def test_hygiene_note_caps_the_named_invites_but_keeps_the_true_count():
    signals = [
        _cal_signal(
            f"ev-{i}",
            RSVP_NEEDS_ACTION,
            title=f"Rollout {i}",
            start=f"2026-08-05T{17 + i}:00:00Z",
        )
        for i in range(5)
    ]
    note = _calendar_hygiene(signals, _ctx())
    assert note["unanswered_count"] == 5
    assert len(note["invites"]) == 3


def test_hygiene_note_ignores_meetings_that_already_happened():
    """An invite you ignored at 9am today is not worth nagging about at 10am."""
    signals = [
        _cal_signal(
            "ev-past", RSVP_NEEDS_ACTION, start="2026-08-04T16:00:00Z"
        ),  # 9am PDT
        _cal_signal("ev-yesterday", RSVP_NEEDS_ACTION, start="2026-08-03T17:00:00Z"),
    ]
    assert _calendar_hygiene(signals, _ctx()) is None


def test_hygiene_note_orders_invites_soonest_first():
    signals = [
        _cal_signal(
            "ev-late", RSVP_NEEDS_ACTION, title="Later", start="2026-08-07T17:00:00Z"
        ),
        _cal_signal(
            "ev-soon", RSVP_NEEDS_ACTION, title="Sooner", start="2026-08-05T17:00:00Z"
        ),
    ]
    note = _calendar_hygiene(signals, _ctx())
    assert [i["title"] for i in note["invites"]] == ["Sooner", "Later"]


def test_hygiene_note_ignores_non_calendar_signals():
    gmail_sig = ChannelSignal(
        id="t1",
        channel=ChannelKind.GMAIL,
        title="Thread",
        attrs={"rsvp": RSVP_NEEDS_ACTION},
    )
    assert _calendar_hygiene([gmail_sig], _ctx()) is None


# ── Slack DM rendering ───────────────────────────────────────────────────────


def test_slack_hygiene_line_names_invites_and_counts_the_rest():
    from backend.agents.slack_delivery import _hygiene_line

    line = _hygiene_line(
        {
            "unanswered_count": 4,
            "invites": [{"title": "Acme rollout"}, {"title": "Vendor sync"}],
        }
    )
    assert "4 upcoming invites you haven't answered" in line
    assert "Acme rollout, Vendor sync +2 more" in line


def test_slack_hygiene_line_is_singular_for_one_invite():
    from backend.agents.slack_delivery import _hygiene_line

    line = _hygiene_line(
        {"unanswered_count": 1, "invites": [{"title": "Acme rollout"}]}
    )
    assert "1 upcoming invite you haven't answered" in line


def test_slack_hygiene_line_absent_when_there_is_nothing_to_say():
    from backend.agents.slack_delivery import _hygiene_line

    assert _hygiene_line(None) is None
    assert _hygiene_line({}) is None
    assert _hygiene_line({"unanswered_count": 0, "invites": []}) is None


def test_slack_dm_appends_the_hygiene_line_without_disturbing_the_priorities():
    from backend.agents.slack_delivery import _priority_blocks

    class _Item:
        title = "Reply to Acme"
        severity = "high"
        body = "They asked for a number."
        next_action = None
        link = None
        confidence = None

    hygiene = {"unanswered_count": 1, "invites": [{"title": "Acme rollout"}]}
    fallback, blocks = _priority_blocks([_Item()], 5, "", hygiene=hygiene)
    assert "Reply to Acme" in fallback
    assert "haven't answered" in fallback
    rendered = str(blocks)
    assert "Reply to Acme" in rendered and "Acme rollout" in rendered

    # No hygiene → the DM is byte-identical to before this feature.
    plain_fallback, plain_blocks = _priority_blocks([_Item()], 5, "")
    assert "haven't answered" not in plain_fallback
    assert len(plain_blocks) == len(blocks) - 1


# ── bundle persistence round-trip ────────────────────────────────────────────


def test_hygiene_survives_the_metadata_round_trip():
    from rubik_core.priorities.models import PrioritiesBundle
    from rubik_core.priorities.orchestrator import (
        bundle_from_metadata,
        bundle_to_metadata,
    )

    hygiene = {"unanswered_count": 2, "invites": [{"title": "Acme rollout"}]}
    meta = bundle_to_metadata(PrioritiesBundle(user_email=ME, calendar_hygiene=hygiene))
    from rubik_core.tenancy.context import current_workspace

    meta["crm_provenance"] = {
        "workspace": current_workspace(),
        "deal_ids": [],
        "unprovable": False,
    }
    assert bundle_from_metadata(ME, meta).calendar_hygiene == hygiene


def test_cards_written_before_this_feature_have_no_hygiene_note():
    from rubik_core.priorities.orchestrator import bundle_from_metadata

    with pytest.raises(ValueError, match="provenance is unavailable"):
        bundle_from_metadata(ME, {"headline": "hi"})


# ── end-to-end through the real pipeline ─────────────────────────────────────


def test_full_pipeline_drops_the_prep_card_and_reports_hygiene_instead(monkeypatch):
    """The whole point, proven through orchestrator.generate().

    The decision-maker is stubbed to do exactly what it does today — emit a
    "prepare for the rollout call" card for an invite the user never answered.
    The pipeline must strip it and hand back the hygiene note instead.
    """
    from rubik_core.priorities import (
        PrioritiesConfig,
        PrioritiesOrchestrator,
        PrioritiesRequest,
    )
    from rubik_core.priorities.models import ChannelDigest

    # Pin the pipeline's clock to the same instant the stub signals are dated
    # against. Every other test in this file injects `_ctx()` directly, but
    # `generate()` resolves the clock itself — so without this the fixed
    # 2026-08-05 event drifts into the past and gets filtered as stale, and the
    # test starts failing on a wall-clock date rather than on a real regression.
    monkeypatch.setattr(
        "rubik_core.priorities.orchestrator.resolve_user_now",
        lambda *_args, **_kwargs: _ctx(),
    )

    config = PrioritiesConfig.default()
    orch = PrioritiesOrchestrator(config=config)

    unanswered = _cal_signal("ev-rollout", RSVP_NEEDS_ACTION, title="Acme rollout call")

    class _StubCalendarAgent:
        channel = ChannelKind.CALENDAR

        def __init__(self, **_):
            pass

        def run(self):
            return ChannelDigest(
                channel=ChannelKind.CALENDAR,
                narrative="one unanswered invite",
                signals=[unanswered],
            )

    monkeypatch.setattr(
        "rubik_core.priorities.orchestrator.CHANNEL_AGENT_REGISTRY",
        {ChannelKind.CALENDAR: _StubCalendarAgent},
    )
    orch.summarizer.client = StubClient(
        {"narrative": "One invite.", "cross_channel_themes": []}
    )
    orch.decision_maker.client = StubClient(
        {
            "priorities": [
                {
                    "title": "Prepare for the Acme rollout call",
                    "why_it_matters": "It's tomorrow morning.",
                    "urgency": "medium",
                    "suggested_action": "Review the rollout doc",
                    "signal_ids": ["ev-rollout"],
                    "priority_kind": "upcoming_prep",
                }
            ]
        }
    )
    orch.critic.client = StubClient(
        {
            "overall_score": 9.0,
            "missing_signals": [],
            "weak_rationales": [],
            "suggestions": [],
            "raw_feedback": "Good.",
        }
    )

    bundle = orch.generate(
        PrioritiesRequest(
            user_email=ME, tz_name=PACIFIC, channels=[ChannelKind.CALENDAR]
        )
    )

    assert bundle.priorities == [], "prep for an unanswered invite must not survive"
    assert bundle.calendar_hygiene is not None
    assert bundle.calendar_hygiene["unanswered_count"] == 1
    assert bundle.calendar_hygiene["invites"][0]["title"] == "Acme rollout call"
    assert any("rsvp-filter" in line for line in bundle.audit_trail)
    # A legitimately-filtered list, NOT a generation failure — this is what
    # keeps the ops alert quiet and the card deliverable.
    assert bundle.generation_status == "filtered_empty"


def test_full_pipeline_keeps_the_prep_card_for_an_accepted_meeting(monkeypatch):
    """The control case — same pipeline, same card, only the RSVP differs."""
    from rubik_core.priorities import (
        PrioritiesConfig,
        PrioritiesOrchestrator,
        PrioritiesRequest,
    )
    from rubik_core.priorities.models import ChannelDigest

    # Pin the pipeline's clock to the same instant the stub signals are dated
    # against. Every other test in this file injects `_ctx()` directly, but
    # `generate()` resolves the clock itself — so without this the fixed
    # 2026-08-05 event drifts into the past and gets filtered as stale, and the
    # test starts failing on a wall-clock date rather than on a real regression.
    monkeypatch.setattr(
        "rubik_core.priorities.orchestrator.resolve_user_now",
        lambda *_args, **_kwargs: _ctx(),
    )

    orch = PrioritiesOrchestrator(config=PrioritiesConfig.default())
    accepted = _cal_signal("ev-board", RSVP_ACCEPTED, title="Board sync")

    class _StubCalendarAgent:
        channel = ChannelKind.CALENDAR

        def __init__(self, **_):
            pass

        def run(self):
            return ChannelDigest(
                channel=ChannelKind.CALENDAR, narrative="one", signals=[accepted]
            )

    monkeypatch.setattr(
        "rubik_core.priorities.orchestrator.CHANNEL_AGENT_REGISTRY",
        {ChannelKind.CALENDAR: _StubCalendarAgent},
    )
    orch.summarizer.client = StubClient(
        {"narrative": "One meeting.", "cross_channel_themes": []}
    )
    orch.decision_maker.client = StubClient(
        {
            "priorities": [
                {
                    "title": "Prepare for the Board sync",
                    "why_it_matters": "It's tomorrow morning.",
                    "urgency": "medium",
                    "suggested_action": "Review the deck",
                    "signal_ids": ["ev-board"],
                    "priority_kind": "upcoming_prep",
                }
            ]
        }
    )
    orch.critic.client = StubClient(
        {
            "overall_score": 9.0,
            "missing_signals": [],
            "weak_rationales": [],
            "suggestions": [],
            "raw_feedback": "Good.",
        }
    )

    bundle = orch.generate(
        PrioritiesRequest(
            user_email=ME, tz_name=PACIFIC, channels=[ChannelKind.CALENDAR]
        )
    )

    assert [p.title for p in bundle.priorities] == ["Prepare for the Board sync"]
    assert bundle.calendar_hygiene is None

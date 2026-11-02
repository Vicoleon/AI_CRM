"""UC-22 tests: entity-tagged signals + Gmail thread-aware + HubSpot dedupe.

The most important test in this file is
``test_channel_prompts_contain_no_judgment_words``. UC-22's whole point
is moving judgment OUT of the channel agents into the decision-maker;
if a future edit re-introduces judgment vocabulary the regression
catches it.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from rubik_core.priorities import (
    ChannelKind,
    ChannelSignal,
    EntityKind,
    PrioritiesConfig,
)
from rubik_core.priorities.agents import (
    CalendarChannelAgent,
    DriveChannelAgent,
    EosChannelAgent,
    GmailChannelAgent,
    HubSpotChannelAgent,
    MeetChannelAgent,
    SlackChannelAgent,
)
from rubik_core.priorities.agents.decision_maker import ATTRS_PREVIEW_KEYS

# ── Helper ───────────────────────────────────────────────────────────────────


def _config() -> PrioritiesConfig:
    return PrioritiesConfig.default()


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


# ── ChannelSignal entity tagging ─────────────────────────────────────────────


def test_channel_signal_entity_key_round_trips():
    sig = ChannelSignal(
        id="t1",
        channel=ChannelKind.GMAIL,
        title="x",
        entity_kind=EntityKind.DEAL,
        entity_id="hubspot_1234",
    )
    assert sig.entity_key == "deal:hubspot_1234"


def test_channel_signal_entity_key_none_when_untagged():
    sig = ChannelSignal(id="t1", channel=ChannelKind.CALENDAR, title="x")
    assert sig.entity_key is None


def test_channel_signal_metadata_aliases_to_attrs_back_compat():
    sig = ChannelSignal(
        id="t1",
        channel=ChannelKind.GMAIL,
        title="x",
        metadata={"legacy_key": "value"},
    )
    # Pre-UC-22 callers that wrote to ``metadata`` should still work.
    assert sig.attrs["legacy_key"] == "value"
    assert sig.attrs is sig.metadata


# ── Gmail extractor: inbox+sent, thread-aware ────────────────────────────────


def _gmail_thread(
    *,
    thread_id: str,
    messages: list[tuple[str, str, str, datetime]],
    label_ids_per_msg: list[list[str]] | None = None,
    msg_ids: list[str] | None = None,
) -> dict:
    """Build a Gmail thread dict with (from, to, snippet, ts) per message.

    ``msg_ids`` attaches a Gmail message ``id`` to each message (as the real
    API always does); omit it to exercise the id-less composite fallback.
    """
    label_ids_per_msg = label_ids_per_msg or [[] for _ in messages]
    msg_dicts = []
    for i, (sender, to, snippet, ts) in enumerate(messages):
        msg = {
            "internalDate": str(_ms(ts)),
            "snippet": snippet,
            "labelIds": label_ids_per_msg[i],
            "payload": {
                "headers": [
                    {"name": "From", "value": sender},
                    {"name": "To", "value": to},
                    {"name": "Subject", "value": "Q3 plan review"},
                ]
            },
        }
        if msg_ids is not None:
            msg["id"] = msg_ids[i]
        msg_dicts.append(msg)
    return {"id": thread_id, "messages": msg_dicts}


def test_gmail_extractor_marks_i_sent_last_when_user_replied_most_recently():
    user = "me@acme.example"
    now = datetime.now(timezone.utc)
    thread = _gmail_thread(
        thread_id="t-acme",
        messages=[
            ("client@acmecorp.example", user, "Initial ping", now - timedelta(days=4)),
            (
                user,
                "client@acmecorp.example",
                "Following up — please confirm Friday",
                now - timedelta(days=2),
            ),
        ],
    )

    agent = GmailChannelAgent(user_email=user, config=_config())
    signals = agent.parse_signals([thread])

    assert len(signals) == 1
    s = signals[0]
    assert s.entity_kind == EntityKind.THREAD
    assert s.entity_id == "t-acme"
    assert s.attrs["direction"] == "i_sent_last"
    # They haven't responded for ~2 days
    assert s.attrs["thread_idle_days"] == 2
    assert "acmecorp.example" in s.attrs["external_domains"]
    assert s.attrs["external_count"] >= 1


def test_gmail_extractor_marks_they_sent_last_when_external_replied():
    user = "me@acme.example"
    now = datetime.now(timezone.utc)
    thread = _gmail_thread(
        thread_id="t-vendor",
        messages=[
            (
                user,
                "vendor@beta.io",
                "Heads up about the renewal",
                now - timedelta(days=3),
            ),
            (
                "vendor@beta.io",
                user,
                "Got it — circling back tomorrow",
                now - timedelta(hours=4),
            ),
        ],
    )
    agent = GmailChannelAgent(user_email=user, config=_config())
    signals = agent.parse_signals([thread])
    assert signals[0].attrs["direction"] == "they_sent_last"


def test_gmail_extractor_groups_sent_and_inbox_into_one_thread(monkeypatch):
    """When the same thread appears in multiple lanes it is emitted once,
    with its messages UNIONed across the copies (no double-emit)."""
    user = "me@acme.example"
    now = datetime.now(timezone.utc)
    inbox_view = _gmail_thread(
        thread_id="t-merge",
        messages=[
            ("client@acmecorp.example", user, "Initial ping", now - timedelta(days=5)),
        ],
    )
    sent_view = _gmail_thread(
        thread_id="t-merge",
        messages=[
            ("client@acmecorp.example", user, "Initial ping", now - timedelta(days=5)),
            (user, "client@acmecorp.example", "Following up", now - timedelta(days=2)),
        ],
    )

    class _StubGmailClient:
        def __init__(self, *a, **kw):
            pass

        def search_threads(self, query, max_results):
            return inbox_view and [inbox_view] if "in:inbox" in query else [sent_view]

    monkeypatch.setattr("rubik_core.tools.gmail.GmailClient", _StubGmailClient)
    agent = GmailChannelAgent(user_email=user, config=_config())
    raw = agent.fetch_signals()
    # Same thread_id across lanes → one merged entry; the shared "Initial ping"
    # message is deduped while "Following up" is added → 2 distinct messages.
    assert len(raw) == 1
    assert len(raw[0]["messages"]) == 2


def test_gmail_fetch_unions_partial_thread_copies_keeps_newest(monkeypatch):
    """Merge must UNION messages across lanes, not "keep the copy with more
    messages". If a newer reply arrives in the SMALLER copy, it must survive
    so ``msgs_sorted[-1]`` (idle + direction) reflects the true latest message.

    Guards the latent merge footgun behind a stale-idle bug: today both lanes
    hydrate full threads so it's masked, but if hydration ever returns a partial
    thread the old "keep more messages" rule would drop the newest reply and
    report a stale idle.
    """
    user = "me@acme.example"
    now = datetime.now(timezone.utc)
    # Larger copy — two OLD messages (older inbound + the user's approval).
    copy_a = _gmail_thread(
        thread_id="t-split",
        messages=[
            ("vendor@data.com", user, "Quote attached", now - timedelta(days=8)),
            (user, "vendor@data.com", "Approved — go ahead", now - timedelta(days=6)),
        ],
        msg_ids=["m1", "m2"],
    )
    # Smaller copy — ONE newer message (the user's fresh reply).
    copy_b = _gmail_thread(
        thread_id="t-split",
        messages=[
            (user, "vendor@data.com", "Following up again", now - timedelta(days=1)),
        ],
        msg_ids=["m3"],
    )

    class _StubGmailClient:
        def __init__(self, *a, **kw):
            pass

        def search_threads(self, query, max_results):
            return [copy_b] if "in:sent" in query else [copy_a]

    monkeypatch.setattr("rubik_core.tools.gmail.GmailClient", _StubGmailClient)
    agent = GmailChannelAgent(user_email=user, config=_config())
    raw = agent.fetch_signals()

    assert len(raw) == 1
    # UNION of {m1, m2} and {m3} — the smaller copy's newer message survives.
    assert len(raw[0]["messages"]) == 3

    signals = agent.parse_signals(raw)
    assert len(signals) == 1
    # Newest message (m3, 1 day ago) drives idle + direction, not the 6-day-old
    # approval that the larger copy would have anchored on.
    assert signals[0].attrs["thread_idle_days"] == 1
    assert signals[0].attrs["direction"] == "i_sent_last"
    assert signals[0].attrs["message_count"] == 3


@pytest.mark.parametrize("lane_marker", ["in:inbox", "in:anywhere", "in:sent"])
def test_gmail_user_reply_newest_idle_near_zero_any_lane(monkeypatch, lane_marker):
    """A thread whose newest message is the user's OWN reply reports idle ~0
    no matter which lane surfaced it — including a non-inbox thread found only
    via the broadened ``in:anywhere`` lane (the "not in my inbox" case)."""
    user = "me@acme.example"
    now = datetime.now(timezone.utc)
    thread = _gmail_thread(
        thread_id="t-quote",
        messages=[
            ("vendor@data.com", user, "Data quote", now - timedelta(days=8)),
            (user, "vendor@data.com", "Approved on our end", now - timedelta(days=6)),
            ("vendor@data.com", user, "Great, next steps", now - timedelta(days=2)),
            (user, "vendor@data.com", "Replied — thanks", now - timedelta(hours=6)),
        ],
        msg_ids=["m1", "m2", "m3", "m4"],
    )

    class _StubGmailClient:
        def __init__(self, *a, **kw):
            pass

        def search_threads(self, query, max_results):
            # Only the parametrised lane surfaces the thread; others are empty.
            return [thread] if lane_marker in query else []

    monkeypatch.setattr("rubik_core.tools.gmail.GmailClient", _StubGmailClient)
    agent = GmailChannelAgent(user_email=user, config=_config())
    signals = agent.parse_signals(agent.fetch_signals())

    assert len(signals) == 1
    # Newest message is 6h old → idle rounds to 0 (allow 1 at a day boundary).
    assert signals[0].attrs["thread_idle_days"] in (0, 1)
    assert signals[0].attrs["direction"] == "i_sent_last"


# ── HubSpot extractor: dedupes across probes ─────────────────────────────────


def _hubspot_signal(*, signal_type, deal_id, severity="medium", **extra):
    """Construct a backend.signals.types.Signal-like duck for the agent.

    NOTE: this hand-builds ``payload`` with the keys the AGENT reads. That is
    fine for the dedupe test below, which is only about collapsing probes onto
    one deal — but it is NOT evidence that production emits these keys. It did
    not: for a long time the signal source wrote ``stage_label`` /
    ``days_in_stage`` / ``days_since_last_activity`` while the agent read
    ``stage`` / ``stage_age_days`` / ``days_since_engagement``, so every
    attribute below was None on every real deal and this test stayed green
    throughout. The end-to-end contract is pinned by
    ``test_the_signal_source_emits_every_key_the_agent_reads`` — keep that one
    driving the real builders."""
    from backend.signals.types import Signal

    payload = {
        "deal_id": deal_id,
        "stage": extra.get("stage", "Proposal"),
        "deal_amount": extra.get("amount", 25000),
        "owner_email": "me@acme.example",
        "stage_age_days": extra.get("stage_age_days"),
        "days_since_engagement": extra.get("days_since_engagement"),
    }
    return Signal(
        user_email="me@acme.example",
        source="hubspot",
        signal_type=signal_type,
        summary=extra.get("summary", f"{signal_type} on {deal_id}"),
        occurred_at=datetime.now(timezone.utc),
        entity_refs={
            "deal_id": deal_id,
            "contact_email": extra.get("contact_email"),
            "company_name": extra.get("company_name", "Acme Corp"),
        },
        severity_hint=severity,
        payload=payload,
    )


def test_hubspot_agent_dedupes_one_deal_across_probes():
    agent = HubSpotChannelAgent(user_email="me@acme.example", config=_config())
    raw = [
        _hubspot_signal(
            signal_type="stale_lead",
            deal_id="d-100",
            severity="medium",
            days_since_engagement=11,
        ),
        _hubspot_signal(
            signal_type="stage_stuck",
            deal_id="d-100",
            severity="high",
            stage_age_days=45,
        ),
        _hubspot_signal(signal_type="stale_lead", deal_id="d-200"),
    ]
    signals = agent.parse_signals(raw)
    by_id = {s.entity_id: s for s in signals}
    assert set(by_id.keys()) == {"d-100", "d-200"}
    # d-100 carries the union of probe types and the higher severity.
    assert "stale_lead" in by_id["d-100"].attrs["signal_types"]
    assert "stage_stuck" in by_id["d-100"].attrs["signal_types"]
    assert by_id["d-100"].attrs["severity_hint"] == "high"
    # entity_kind is set for downstream cross-channel joins.
    assert by_id["d-100"].entity_kind == EntityKind.DEAL


# ── Calendar extractor: entity tags + related entities ───────────────────────


def test_calendar_extractor_emits_event_entity_with_related_attendees():
    user = "me@acme.example"
    raw_events = [
        {
            "id": "evt-1",
            "summary": "Acme pricing review",
            "start": {"dateTime": "2026-05-28T14:00:00Z"},
            "end": {"dateTime": "2026-05-28T15:00:00Z"},
            "attendees": [
                {"email": "ceo@acmecorp.example"},
                {"email": "buyer@acmecorp.example"},
                {"email": "me@acme.example"},
            ],
            "description": "Walk through proposal",
            "attachments": [],
            "conferenceData": {
                "entryPoints": [
                    {"entryPointType": "video", "uri": "https://meet.example/abc"}
                ]
            },
        }
    ]
    agent = CalendarChannelAgent(user_email=user, config=_config())
    signals = agent.parse_signals(raw_events)
    assert len(signals) == 1
    s = signals[0]
    assert s.entity_kind == EntityKind.EVENT
    assert s.entity_id == "evt-1"
    assert s.attrs["external_attendee_count"] == 2
    assert "acmecorp.example" in s.attrs["external_domains"]
    # Related entities make the downstream cross-channel join easy.
    related = s.attrs["related_entities"]
    assert {"entity_kind": "person", "entity_id": "ceo@acmecorp.example"} in related
    assert {"entity_kind": "company", "entity_id": "acmecorp.example"} in related


def test_calendar_extractor_preformats_reader_local_time():
    """Regression for the +2h homepage drift: the event time reaches the LLM
    as a pre-formatted reader-local wall clock (``when_local``), and the
    extractor prompt shows that string — never the raw offset-bearing ISO."""
    from rubik_core.priorities.temporal import resolve_user_now

    now_utc = datetime(2026, 7, 2, 8, 0, tzinfo=timezone.utc)
    temporal = resolve_user_now(now_utc=now_utc, tz_name="America/Los_Angeles")
    raw_events = [
        {
            "id": "evt-tz",
            "summary": "1:1 with Nico",
            # Source offset reads "13:00" (Central); Pacific reader sees 11 AM.
            "start": {"dateTime": "2026-07-02T13:00:00-05:00"},
            "end": {"dateTime": "2026-07-02T14:00:00-05:00"},
            "attendees": [{"email": "nico@acme.example"}],
        }
    ]
    agent = CalendarChannelAgent(
        user_email="me@acme.example", config=_config(), temporal=temporal
    )
    signals = agent.parse_signals(raw_events)
    assert signals[0].attrs["when_local"] == "Thursday Jul 2, 11:00 AM PDT"

    prompt = agent.build_prompt(signals)
    assert "when: Thursday Jul 2, 11:00 AM PDT" in prompt
    # The raw offset-bearing ISO must not be what the model formats from.
    assert "when: 2026-07-02T13:00:00-05:00" not in prompt


def test_calendar_extractor_when_local_handles_all_day():
    from rubik_core.priorities.temporal import resolve_user_now

    temporal = resolve_user_now(
        now_utc=datetime(2026, 7, 2, 8, 0, tzinfo=timezone.utc),
        tz_name="America/Los_Angeles",
    )
    agent = CalendarChannelAgent(
        user_email="me@acme.example", config=_config(), temporal=temporal
    )
    signals = agent.parse_signals(
        [
            {
                "id": "evt-allday",
                "summary": "Team offsite",
                "start": {"date": "2026-07-03"},
                # All-day events WITH attendees survive the reminder filter, so
                # this still exercises all-day when_local formatting.
                "attendees": [{"email": "teammate@acme.example"}],
            }
        ]
    )
    assert signals[0].attrs["all_day"] is True
    # Date only — no bogus midnight clock time.
    assert signals[0].attrs["when_local"] == "Friday Jul 3"


def test_calendar_extractor_skips_focus_time_and_ooo_blocks():
    """Focus time / OOO / birthday blocks are real calendar time but never a
    meeting to prep for — they must not become "Prepare for Focus time…"
    priority cards (user feedback)."""
    user = "me@acme.example"
    raw_events = [
        {
            "id": "evt-focus",
            "summary": "Focus time: Address GitHub Actions failures",
            "eventType": "focusTime",
            "start": {"dateTime": "2026-07-02T22:15:00Z"},
            "end": {"dateTime": "2026-07-02T23:15:00Z"},
        },
        {
            "id": "evt-ooo",
            "summary": "Out of office",
            "eventType": "outOfOffice",
            "start": {"dateTime": "2026-07-03T16:00:00Z"},
            "end": {"dateTime": "2026-07-03T17:00:00Z"},
        },
        {
            "id": "evt-real",
            "summary": "1:1 with Nico",
            "start": {"dateTime": "2026-07-02T20:00:00Z"},
            "end": {"dateTime": "2026-07-02T20:30:00Z"},
            "attendees": [{"email": "nico@acme.example"}],
        },
    ]
    agent = CalendarChannelAgent(user_email=user, config=_config())
    ids = {s.id for s in agent.parse_signals(raw_events)}
    assert ids == {"evt-real"}


def test_calendar_extractor_skips_busy_placeholder_hold():
    """An opaque "Busy" hold (Outlook free/busy sync, private secondary-calendar
    block) is real busy time but never a meeting to prep — it must not become a
    "Prepare for Busy…" priority card. A real meeting alongside it is kept."""
    user = "me@acme.example"
    raw_events = [
        {
            "id": "evt-busy",
            "summary": "Busy",
            "start": {"dateTime": "2026-07-02T15:00:00Z"},
            "end": {"dateTime": "2026-07-02T16:00:00Z"},
        },
        {
            "id": "evt-real",
            "summary": "1:1 with Nico",
            "start": {"dateTime": "2026-07-02T20:00:00Z"},
            "end": {"dateTime": "2026-07-02T20:30:00Z"},
            "attendees": [{"email": "nico@acme.example"}],
        },
    ]
    agent = CalendarChannelAgent(user_email=user, config=_config())
    ids = {s.id for s in agent.parse_signals(raw_events)}
    assert ids == {"evt-real"}


def test_calendar_extractor_skips_all_day_reminder_without_attendees():
    """An all-day event no one else is on is a reminder, not a meeting
    ("This is a calendar reminder, not a meeting" — user feedback). An
    all-day event WITH attendees (an offsite) is kept."""
    user = "me@acme.example"
    raw_events = [
        {
            "id": "evt-reminder",
            "summary": "Expense reports due",
            "start": {"date": "2026-07-01"},  # all-day, no attendees
        },
        {
            "id": "evt-offsite",
            "summary": "Company offsite",
            "start": {"date": "2026-07-01"},
            "attendees": [{"email": "teammate@acme.example"}],
        },
    ]
    agent = CalendarChannelAgent(user_email=user, config=_config())
    ids = {s.id for s in agent.parse_signals(raw_events)}
    assert ids == {"evt-offsite"}


# ── REGRESSION: the reader is never their own counterparty ───────────────────


def _internal_1on1(*, reader: str, other: str, title: str) -> dict:
    return {
        "id": "evt-1on1_20260803T160000Z",
        "summary": title,
        "start": {"dateTime": "2026-08-03T16:00:00Z"},
        "end": {"dateTime": "2026-08-03T16:30:00Z"},
        "attendees": [
            {"email": reader, "displayName": "Blair"},
            {"email": other, "displayName": "Devon"},
        ],
        "attachments": [],
    }


def test_calendar_extractor_excludes_reader_from_counterparties(monkeypatch):
    """Regression: "Prepare for your 1:1 with Blair" was delivered TO Blair.

    The extractor used to surface external attendees only, so an internal 1:1
    reached the LLM with no attendee data at all and the model took a name off
    the title — which on a "Blair / Devon" title is a coin flip that landed on
    the reader. The reader must never appear in `counterparties`.
    """
    monkeypatch.setattr(
        "rubik_core.priorities.agents.calendar_agent.resolve_display_names",
        lambda emails: {
            "bellison@acme.example": "Blair Ellison",
            "dellison@acme.example": "Devon Ellison",
        },
    )
    reader = "bellison@acme.example"
    agent = CalendarChannelAgent(user_email=reader, config=_config())
    signals = agent.parse_signals(
        [
            _internal_1on1(
                reader=reader, other="dellison@acme.example", title="Blair / Devon 1:1"
            )
        ]
    )

    assert len(signals) == 1
    counterparties = signals[0].attrs["counterparties"]
    assert counterparties == ["Devon Ellison"]
    assert signals[0].attrs["counterparty_count"] == 1
    # The reader — by name OR address — must be absent.
    assert not any("Blair" in c or reader in c for c in counterparties)


def test_calendar_prompt_names_counterparties_and_the_reader(monkeypatch):
    """The `with:` line and the reader's identity both reach the LLM.

    Without both, the model cannot tell that the "Blair" in the title IS the
    person it is writing to.
    """
    monkeypatch.setattr(
        "rubik_core.priorities.agents.calendar_agent.resolve_display_names",
        lambda emails: {"dellison@acme.example": "Devon Ellison"},
    )
    reader = "bellison@acme.example"
    agent = CalendarChannelAgent(user_email=reader, config=_config())
    signals = agent.parse_signals(
        [
            _internal_1on1(
                reader=reader, other="dellison@acme.example", title="Blair / Devon 1:1"
            )
        ]
    )
    prompt = agent.build_prompt(signals)

    assert "with: Devon Ellison" in prompt
    assert reader in prompt  # reader identity stated


def test_calendar_extractor_counterparties_fall_back_to_email_local_part(monkeypatch):
    """No users-table row and no usable displayName → still not the raw address."""
    monkeypatch.setattr(
        "rubik_core.priorities.agents.calendar_agent.resolve_display_names",
        lambda emails: {},
    )
    reader = "me@acme.example"
    agent = CalendarChannelAgent(user_email=reader, config=_config())
    signals = agent.parse_signals(
        [
            {
                "id": "evt-x",
                "summary": "Sync",
                "start": {"dateTime": "2026-08-03T16:00:00Z"},
                "end": {"dateTime": "2026-08-03T16:30:00Z"},
                "attendees": [
                    {"email": reader},
                    {"email": "buyer@acmecorp.example"},
                ],
                "attachments": [],
            }
        ]
    )
    assert signals[0].attrs["counterparties"] == ["buyer"]


def test_calendar_extractor_skips_rooms_in_counterparties(monkeypatch):
    """A conference room is on the invite but is not who you are meeting."""
    monkeypatch.setattr(
        "rubik_core.priorities.agents.calendar_agent.resolve_display_names",
        lambda emails: {"ifoster@acme.example": "Indigo Foster"},
    )
    reader = "ebrooks@acme.example"
    agent = CalendarChannelAgent(user_email=reader, config=_config())
    signals = agent.parse_signals(
        [
            {
                "id": "evt-room",
                "summary": "Indigo/Emery 1:1",
                "start": {"dateTime": "2026-08-03T16:00:00Z"},
                "end": {"dateTime": "2026-08-03T16:30:00Z"},
                "attendees": [
                    {"email": reader},
                    {"email": "ifoster@acme.example"},
                    {"email": "room-4@resource.calendar.google.com", "resource": True},
                ],
                "attachments": [],
            }
        ]
    )
    assert signals[0].attrs["counterparties"] == ["Indigo Foster"]


# ── REGRESSION: a thread you were only Cc'd on is not your action ─────────────


def _addressed_thread(
    *, reader: str, to: str, cc: str = "", snippet: str = "ACTION NEEDED"
) -> dict:
    headers = [
        {"name": "From", "value": "partner@tomo.com"},
        {"name": "To", "value": to},
        {"name": "Subject", "value": "ACTION NEEDED: Tomo amendment"},
    ]
    if cc:
        headers.append({"name": "Cc", "value": cc})
    return {
        "id": "t-tomo",
        "messages": [
            {
                "id": "m1",
                "internalDate": str(
                    _ms(datetime.now(timezone.utc) - timedelta(days=1))
                ),
                "snippet": snippet,
                "labelIds": [],
                "payload": {"headers": headers},
            }
        ],
    }


def test_gmail_extractor_marks_cc_only_thread_as_cc():
    """Regression: an "ACTION NEEDED" email addressed to a colleague became the
    CEO's own high-urgency deadline ("This is for someone else"). To and Cc were
    merged into one recipient list, so nothing downstream could tell them apart.
    """
    reader = "ebrooks@acme.example"
    agent = GmailChannelAgent(user_email=reader, config=_config())
    signals = agent.parse_signals(
        [_addressed_thread(reader=reader, to="ana@acme.example", cc=reader)]
    )

    assert signals[0].attrs["addressed_to_reader"] == "cc"
    # The actual owner of the request is visible to the decision-maker.
    assert signals[0].attrs["to_recipients"] == ["ana@acme.example"]


def test_gmail_extractor_marks_directly_addressed_thread_as_direct():
    reader = "ebrooks@acme.example"
    agent = GmailChannelAgent(user_email=reader, config=_config())
    signals = agent.parse_signals([_addressed_thread(reader=reader, to=reader)])
    assert signals[0].attrs["addressed_to_reader"] == "direct"


def test_gmail_extractor_marks_distribution_list_thread_as_list():
    """Never individually addressed → the reader is an observer, not the actor."""
    reader = "ebrooks@acme.example"
    agent = GmailChannelAgent(user_email=reader, config=_config())
    signals = agent.parse_signals(
        [_addressed_thread(reader=reader, to="compliance@acme.example")]
    )
    assert signals[0].attrs["addressed_to_reader"] == "list"


def test_gmail_extractor_keeps_thread_that_was_ever_addressed_directly():
    """To'd first, Cc'd on the newest message → still the reader's thread.

    Reading only the last message would misfile a conversation the reader owns.
    """
    reader = "ebrooks@acme.example"
    now = datetime.now(timezone.utc)
    thread = {
        "id": "t-mixed",
        "messages": [
            {
                "id": "m1",
                "internalDate": str(_ms(now - timedelta(days=3))),
                "snippet": "Kicking this off",
                "labelIds": [],
                "payload": {
                    "headers": [
                        {"name": "From", "value": "partner@tomo.com"},
                        {"name": "To", "value": reader},
                        {"name": "Subject", "value": "Tomo amendment"},
                    ]
                },
            },
            {
                "id": "m2",
                "internalDate": str(_ms(now - timedelta(days=1))),
                "snippet": "Ana, please issue the invoice",
                "labelIds": [],
                "payload": {
                    "headers": [
                        {"name": "From", "value": "partner@tomo.com"},
                        {"name": "To", "value": "ana@acme.example"},
                        {"name": "Cc", "value": reader},
                        {"name": "Subject", "value": "Tomo amendment"},
                    ]
                },
            },
        ],
    }
    agent = GmailChannelAgent(user_email=reader, config=_config())
    attrs = agent.parse_signals([thread])[0].attrs
    assert attrs["addressed_to_reader"] == "direct"  # strongest across thread
    assert attrs["addressed_to_reader_last_message"] == "cc"  # where it stands now


def test_gmail_prompt_shows_how_the_reader_was_addressed():
    reader = "ebrooks@acme.example"
    agent = GmailChannelAgent(user_email=reader, config=_config())
    signals = agent.parse_signals(
        [_addressed_thread(reader=reader, to="ana@acme.example", cc=reader)]
    )
    prompt = agent.build_prompt(signals)
    assert "addressed_to_reader: cc" in prompt
    assert reader in prompt


# ── REGRESSION: channel prompts must contain no judgment vocabulary ──────────

JUDGMENT_WORDS = (
    # Words that imply the channel agent is making a decision about
    # importance. UC-22 mandates that judgment lives only in the
    # decision-maker / critic prompts.
    r"\bimportant\b",
    r"\burgent\b",
    r"\bflag\b",
    r"\bcritical\b",
    r"\bpriority\b",
    r"\bworth surfacing\b",
)
_JUDGMENT_RE = re.compile("|".join(JUDGMENT_WORDS), re.IGNORECASE)


@pytest.mark.parametrize(
    "agent_cls",
    [
        GmailChannelAgent,
        CalendarChannelAgent,
        MeetChannelAgent,
        HubSpotChannelAgent,
        # Not covered at first; both pass. (Slack's retired
        # `urgency_hint` slipped past this regex — "urgency" is not "urgent" —
        # so the test that keeps it out is the reaches-the-decision-maker one
        # below.)
        SlackChannelAgent,
        DriveChannelAgent,
    ],
)
def test_channel_prompts_contain_no_judgment_words(agent_cls):
    """If you change a channel prompt and accidentally add 'flag the
    important ones' style copy, this test fails. Move judgment to
    the decision-maker prompt instead.
    """
    agent = agent_cls(user_email="me@acme.example", config=_config())
    # build_prompt is OK to call with [] — every extractor renders a
    # template even when there are no signals.
    prompt = agent.build_prompt([])
    matches = _JUDGMENT_RE.findall(prompt)
    assert not matches, (
        f"{agent_cls.__name__}.build_prompt contains judgment vocabulary "
        f"forbidden by UC-22: {matches}"
    )


# ── REGRESSION: an extractor asks only for fields something reads ────────────
#
# The bug: five extractors asked the model for per-signal fields that
# nothing downstream read — Calendar/Drive `mentioned_topics` + `prep_keywords`,
# Slack `action_required` / `ask_summary` / `urgency_hint`, Meet
# `decisions_recorded` / `participants_mentioned`, Gmail `has_decision_verb`.
# `decision_maker._format_attrs_preview` is the only place a signal's attrs
# reach a model, so a requested field outside ATTRS_PREVIEW_KEYS is output
# tokens paid for on every signal of every run.

_LLM_CHANNEL_AGENTS = (
    GmailChannelAgent,
    CalendarChannelAgent,
    DriveChannelAgent,
    MeetChannelAgent,
    HubSpotChannelAgent,
    EosChannelAgent,
    SlackChannelAgent,
)

# Top-level keys of the extractor's answer, not per-signal fields.
_ENVELOPE_KEYS = {"summary", "signal_attrs", "thread_attrs", "slack_attrs"}
# A key opening a line of the OUTPUT template (`  "k": ...` or Slack's
# `  * "k": ...`). Anchored to the line start on purpose: nested keys inside a
# value — `mentioned_dates`' `{"raw": ..., "resolved": ...}` — sit mid-line.
_OUTPUT_KEY_RE = re.compile(r'^\s*(?:\*\s*)?"(\w+)":', re.MULTILINE)


def requested_signal_fields(agent) -> set[str]:
    """The per-signal fields ``agent``'s prompt asks the model to write."""
    prompt = agent.build_prompt([])
    marker = "## OUTPUT" if "## OUTPUT" in prompt else "INSTRUCTIONS:"
    assert marker in prompt, f"{type(agent).__name__}: no output spec found"
    block = prompt[prompt.index(marker) :]
    return set(_OUTPUT_KEY_RE.findall(block)) - _ENVELOPE_KEYS


@pytest.mark.parametrize("agent_cls", _LLM_CHANNEL_AGENTS)
def test_every_field_an_extractor_requests_reaches_the_decision_maker(agent_cls):
    agent = agent_cls(user_email="me@acme.example", config=_config())
    unread = requested_signal_fields(agent) - set(ATTRS_PREVIEW_KEYS)
    assert not unread, (
        f"{agent_cls.__name__} asks the model for {sorted(unread)}, which "
        "nothing downstream renders — either add it to "
        "decision_maker.ATTRS_PREVIEW_KEYS (and say what the decision-maker "
        "does with it) or stop asking for it"
    )


@pytest.mark.parametrize("agent_cls", _LLM_CHANNEL_AGENTS)
def test_the_model_writable_allowlist_is_exactly_what_the_prompt_asks_for(agent_cls):
    """``BaseChannelAgent._merge_model_attrs`` merges only
    ``model_attr_keys``. If the prompt asks for a field the allowlist lacks,
    the model's answer is silently discarded; if the allowlist holds a field
    the prompt never asks for, it is a write path nobody reasoned about. See
    test_priorities_model_attr_allowlist.py for what the fence protects."""
    agent = agent_cls(user_email="me@acme.example", config=_config())
    assert set(agent.model_attr_keys) == requested_signal_fields(agent)


def test_the_output_spec_parser_sees_the_fields_it_should():
    """Guard the guard: a parser that found nothing would pass everything."""
    gmail = GmailChannelAgent(user_email="me@acme.example", config=_config())
    assert requested_signal_fields(gmail) == {
        "asks_of_reader",
        "request_owner",
        "reader_last_message_kind",
        "mentioned_dates",
        "mentioned_amounts",
    }
    slack = SlackChannelAgent(user_email="me@acme.example", config=_config())
    assert requested_signal_fields(slack) == {"mentioned_dates"}


# ── Gmail extractor: what did the reader's last message DO? ──────────────────
#
# Feedback: priorities rated "I already responded to this" were built on
# threads where the reader's own reply was the newest message —
# `direction=i_sent_last` — which the rules read as "they are waiting on them".
# The extractor now says which it is.


def test_gmail_reader_reply_to_someone_elses_ask_is_an_answer():
    user = "me@acme.example"
    now = datetime.now(timezone.utc)
    thread = _gmail_thread(
        thread_id="t-qb",
        messages=[
            (
                "cpa@outside.example",
                user,
                "Urgent Action Required: Xero to QB conversion — do we proceed?",
                now - timedelta(days=3),
            ),
            (
                user,
                "cpa@outside.example",
                "Let's hold for now. Thanks.",
                now - timedelta(days=2),
            ),
        ],
    )
    (s,) = GmailChannelAgent(user_email=user, config=_config()).parse_signals([thread])
    assert s.attrs["direction"] == "i_sent_last"
    assert s.attrs["reader_replied"] is True
    assert s.attrs["reader_last_message_kind"] == "answer"
    assert s.attrs["messages_since_reader_reply"] == 0


def test_gmail_thread_the_reader_opened_with_no_response_is_an_ask():
    user = "me@acme.example"
    now = datetime.now(timezone.utc)
    thread = _gmail_thread(
        thread_id="t-out",
        messages=[
            (
                user,
                "client@acmecorp.example",
                "Can you confirm Friday?",
                now - timedelta(days=3),
            ),
        ],
    )
    (s,) = GmailChannelAgent(user_email=user, config=_config()).parse_signals([thread])
    assert s.attrs["direction"] == "i_sent_last"
    assert s.attrs["reader_last_message_kind"] == "ask"


def test_gmail_reader_replied_then_they_wrote_again_keeps_the_count():
    """they_sent_last, but the reader DID speak — the decision-maker uses this
    with `addressed_to_reader_last_message` to spot a bulk reminder that landed
    after the reader already answered."""
    user = "me@acme.example"
    now = datetime.now(timezone.utc)
    thread = _gmail_thread(
        thread_id="t-conf",
        messages=[
            (
                "events@tradeassoc.example",
                user,
                "ACTION NEEDED by 8/6",
                now - timedelta(days=3),
            ),
            (
                user,
                "events@tradeassoc.example",
                "Registered — see attached.",
                now - timedelta(days=2),
            ),
            (
                "events@tradeassoc.example",
                "members@tradeassoc.example",
                "Reminder: ACTION NEEDED by 8/6",
                now - timedelta(hours=6),
            ),
        ],
    )
    (s,) = GmailChannelAgent(user_email=user, config=_config()).parse_signals([thread])
    assert s.attrs["direction"] == "they_sent_last"
    assert s.attrs["reader_replied"] is True
    assert s.attrs["reader_last_message_kind"] is None
    assert s.attrs["messages_since_reader_reply"] == 1
    assert s.attrs["addressed_to_reader_last_message"] == "list"


def test_gmail_reader_never_spoke_is_not_replied():
    user = "me@acme.example"
    now = datetime.now(timezone.utc)
    thread = _gmail_thread(
        thread_id="t-in",
        messages=[("a@b.com", user, "FYI", now - timedelta(hours=2))],
    )
    (s,) = GmailChannelAgent(user_email=user, config=_config()).parse_signals([thread])
    assert s.attrs["reader_replied"] is False
    assert s.attrs["reader_last_message_kind"] is None
    assert s.attrs["messages_since_reader_reply"] is None


def test_gmail_prompt_shows_the_reader_kind_and_asks_the_model_to_classify_it():
    user = "me@acme.example"
    now = datetime.now(timezone.utc)
    thread = _gmail_thread(
        thread_id="t-qb",
        messages=[
            ("cpa@outside.example", user, "do we proceed?", now - timedelta(days=3)),
            (user, "cpa@outside.example", "Let's hold.", now - timedelta(days=2)),
        ],
    )
    agent = GmailChannelAgent(user_email=user, config=_config())
    prompt = agent.build_prompt(agent.parse_signals([thread]))
    assert "reader_last_message_kind: answer" in prompt
    assert '"reader_last_message_kind": "ask" | "answer" | "fyi"' in prompt


def test_gmail_extractor_llm_can_promote_a_questioning_reply_to_ask(monkeypatch):
    """The structural default is 'answer' for any reply; a reply that itself
    poses a question is the LLM's call, and the merge must honour it."""
    user = "me@acme.example"
    now = datetime.now(timezone.utc)
    thread = _gmail_thread(
        thread_id="t-q",
        messages=[
            (
                "client@acmecorp.example",
                user,
                "Here is the redline.",
                now - timedelta(days=3),
            ),
            (
                user,
                "client@acmecorp.example",
                "Thanks — can you also confirm the start date?",
                now - timedelta(days=2),
            ),
        ],
    )
    agent = GmailChannelAgent(user_email=user, config=_config())
    monkeypatch.setattr(agent, "fetch_signals", lambda: [thread])
    monkeypatch.setattr(
        agent.client,
        "invoke_json",
        lambda prompt, fallback=None, **kw: {
            "summary": "One thread.",
            "thread_attrs": {"t-q": {"reader_last_message_kind": "ask"}},
        },
    )
    digest = agent.run()
    assert digest.signals[0].attrs["reader_last_message_kind"] == "ask"


# ── the writer/reader contract, end to end ───────────────────────────────────


def _api_deal(
    *,
    deal_id="d-900",
    quiet_days=24,
    stage_days=40,
    closedate="2026-09-30T00:00:00Z",
    contacted=True,
):
    """One deal in the shape the live HubSpot search actually returns."""
    now = datetime.now(timezone.utc)
    props = {
        "hs_object_id": deal_id,
        "dealname": "Acme Platform Rollout",
        "dealstage": "stage-123",
        "amount": "25000",
        "services_sold": "Platform Rollout",
        "notes_last_updated": (now - timedelta(days=3)).isoformat(),
        "hs_v2_date_entered_current_stage": (
            now - timedelta(days=stage_days)
        ).isoformat(),
        "closedate": closedate,
    }
    if contacted:
        props["notes_last_contacted"] = (now - timedelta(days=quiet_days)).isoformat()
    return {"id": deal_id, "properties": props}


def _signals_for(deal) -> dict:
    """Drive the REAL path: API dict → _live_rows → probes → channel agent."""
    from backend.signals.hubspot import (
        _live_rows,
        stage_stuck_signal,
        stale_lead_signal,
    )
    from rubik_core.priorities.agents.hubspot_agent import HubSpotChannelAgent

    stale_rows, stuck_rows, _at_risk = _live_rows(
        [deal], "me@acme.example", "owner-1", {"stage-123": "Proposal"}
    )
    raw = [stale_lead_signal(r) for r in stale_rows]
    raw += [stage_stuck_signal(r) for r in stuck_rows]
    raw = [r for r in raw if r]
    assert raw, "fixture produced no signals — check the staleness windows"

    agent = HubSpotChannelAgent(user_email="me@acme.example", config=_config())
    parsed = agent.parse_signals(raw)
    assert len(parsed) == 1, "one deal must collapse to one signal"
    return parsed[0].attrs


def test_the_signal_source_emits_every_key_the_agent_reads():
    """The regression that hid for months: the agent read five keys the signal
    source never wrote, so `stage`, `stage_age_days`, `days_since_engagement`,
    `last_engagement_at` and `close_date` were None on EVERY production deal —
    while the decision-maker prompt confidently reasoned about all five."""
    # `_live_rows` needs the deal inside the stale window (21 < days <= 51) on
    # notes_last_updated, and stuck (14 < d <= 365) on stage entry.
    attrs = _signals_for(_api_deal(quiet_days=24, stage_days=40))
    for key in (
        "stage",
        "stage_age_days",
        "days_since_engagement",
        "last_engagement_at",
        "close_date",
    ):
        assert attrs.get(key) is not None, f"{key} is None — writer/reader drift"
    assert attrs["stage"] == "Proposal"
    assert attrs["stage_age_days"] == 40
    assert attrs["days_since_engagement"] == 24


def test_silence_is_measured_from_client_contact_not_from_an_internal_note():
    """The false negative this pair exists to eliminate. The deal below was
    touched internally 3 days ago and last CONTACTED 24 days ago; the card must
    say 24. An AE who logs a note to themselves must not be able to reset their
    own staleness clock without speaking to anybody."""
    attrs = _signals_for(_api_deal(quiet_days=24))
    assert attrs["days_since_engagement"] == 24  # not 3


def test_no_logged_contact_yields_no_silence_claim_rather_than_a_guess():
    """If nobody logged a client contact we do not know how long the client has
    been quiet. None is the honest answer — falling back to notes_last_updated
    would manufacture "contacted 3 days ago" out of an internal note."""
    attrs = _signals_for(_api_deal(contacted=False))
    assert attrs["days_since_engagement"] is None
    assert attrs["last_engagement_at"] is None
    # The deal still surfaces on its other probes — it is not dropped.
    assert attrs.get("stage") == "Proposal"


def test_hubspot_epoch_junk_never_reads_as_a_real_date():
    """HubSpot returns 1970-ish values for unset date rollups. Unguarded, that
    becomes "last contacted 20,000 days ago" — the loudest card on the page."""
    attrs = _signals_for(_api_deal(closedate="1970-01-21T00:00:00Z", quiet_days=24))
    assert attrs["close_date"] is None


def test_close_date_is_rendered_to_the_model_not_merely_carried():
    """Carrying the field is only half the fix — the decision-maker has always
    named `close_date` in its rules, and a key the attrs preview does not
    render is an instruction to read something the model cannot see."""
    from rubik_core.priorities.agents.decision_maker import ATTRS_PREVIEW_KEYS

    assert "close_date" in ATTRS_PREVIEW_KEYS

"""UC-17.7 — Cross-source correlator.

Covers the tier-by-tier grouping logic with synthetic Signals. No source
modules are instantiated; we hand-build `Signal` objects with the exact
`entity_refs` shapes each source emits in production.
"""

from __future__ import annotations

from datetime import datetime, timezone

from backend.signals.correlator import Correlator, correlate
from backend.signals.types import Signal


def _ts(hour: int = 10) -> datetime:
    return datetime(2026, 5, 11, hour, 0, tzinfo=timezone.utc)


def _hubspot_stale(
    *,
    user="rep@acme.example",
    deal_id="deal-1",
    contact_email="buyer@acmecorp.example",
    company_name="Acme",
) -> Signal:
    return Signal(
        user_email=user,
        source="hubspot",
        signal_type="stale_lead",
        summary=f"deal {deal_id} stale",
        occurred_at=_ts(8),
        entity_refs={
            "deal_id": deal_id,
            "contact_email": contact_email,
            "company_name": company_name,
        },
        severity_hint="medium",
    )


def _gmail_unread(
    *,
    user="rep@acme.example",
    thread_id="t-1",
    contact_email="buyer@acmecorp.example",
) -> Signal:
    refs = {"thread_id": thread_id}
    if contact_email:
        refs["contact_email"] = contact_email
    return Signal(
        user_email=user,
        source="gmail",
        signal_type="unread_thread",
        summary="unread",
        occurred_at=_ts(9),
        entity_refs=refs,
    )


def _gmail_awaiting(
    *,
    user="rep@acme.example",
    thread_id="t-await",
    contact_email: str | None = None,
) -> Signal:
    refs: dict = {"thread_id": thread_id}
    if contact_email:
        refs["contact_email"] = contact_email
    return Signal(
        user_email=user,
        source="gmail",
        signal_type="awaiting_reply",
        summary="awaiting",
        occurred_at=_ts(7),
        entity_refs=refs,
    )


def _slack_mention(
    *,
    user="rep@acme.example",
    channel_id="C-ACME",
    channel_name="deals-acme",
    ts="1715000000.000100",
) -> Signal:
    return Signal(
        user_email=user,
        source="slack",
        signal_type="mention",
        summary="mention",
        occurred_at=_ts(10),
        entity_refs={"channel_id": channel_id, "message_ts": ts},
        payload={"channel_name": channel_name, "channel_id": channel_id},
    )


def _calendar_meeting(
    *,
    user="rep@acme.example",
    event_id="e-1",
    contact_email="buyer@acmecorp.example",
    company_name="acme",
) -> Signal:
    return Signal(
        user_email=user,
        source="calendar",
        signal_type="upcoming_meeting_with_customer",
        summary="meeting",
        occurred_at=_ts(11),
        entity_refs={
            "event_id": event_id,
            "contact_email": contact_email,
            "company_name": company_name,
        },
    )


def _transcript(
    *,
    user="rep@acme.example",
    transcript_id="tx-1",
    event_id: str | None = None,
    contact_email: str | None = None,
    company_name: str | None = None,
) -> Signal:
    refs: dict = {"transcript_id": transcript_id}
    if event_id:
        refs["event_id"] = event_id
    if contact_email:
        refs["contact_email"] = contact_email
    if company_name:
        refs["company_name"] = company_name
    return Signal(
        user_email=user,
        source="transcript",
        signal_type="action_item_assigned_to_rep",
        summary="action item",
        occurred_at=_ts(6),
        entity_refs=refs,
    )


# ─── Tests ───────────────────────────────────────────────────────────────────


def test_single_hubspot_stale_lead_becomes_one_deal_group():
    sig = _hubspot_stale()
    groups = correlate([sig])
    assert len(groups) == 1
    g = groups[0]
    assert g.primary_entity_kind == "deal"
    assert g.primary_entity_ref == "deal-1"
    assert g.correlation_id == "deal:deal-1"
    assert g.signals == [sig]
    assert g.sources == ["hubspot"]


def test_hubspot_plus_gmail_thread_with_matching_contact_email_join():
    hs = _hubspot_stale()
    gm = _gmail_unread(contact_email="buyer@acmecorp.example")
    groups = correlate([hs, gm])
    assert len(groups) == 1
    g = groups[0]
    assert g.primary_entity_kind == "deal"
    assert g.sources == ["gmail", "hubspot"]
    assert {s.signal_id for s in g.signals} == {hs.signal_id, gm.signal_id}


def test_hubspot_plus_slack_channel_fuzzy_join():
    hs = _hubspot_stale(company_name="Acme")
    sl = _slack_mention(channel_name="deals-acme")
    groups = correlate([hs, sl])
    assert len(groups) == 1
    g = groups[0]
    assert g.primary_entity_kind == "deal"
    assert g.sources == ["hubspot", "slack"]


def test_gmail_awaiting_reply_without_contact_email_is_thread_orphan():
    # No HubSpot deal, no contact_email → falls through to thread-keyed orphan.
    sig = _gmail_awaiting(thread_id="t-99", contact_email=None)
    groups = correlate([sig])
    assert len(groups) == 1
    g = groups[0]
    assert g.primary_entity_kind == "thread"
    assert g.primary_entity_ref == "t-99"
    assert g.correlation_id == "thread:t-99"


def test_two_deals_each_with_their_own_gmail_thread():
    hs_a = _hubspot_stale(
        deal_id="deal-A", contact_email="a@alpha.com", company_name="Alpha"
    )
    hs_b = _hubspot_stale(
        deal_id="deal-B", contact_email="b@beta.com", company_name="Beta"
    )
    gm_a = _gmail_unread(thread_id="t-A", contact_email="a@alpha.com")
    gm_b = _gmail_unread(thread_id="t-B", contact_email="b@beta.com")
    groups = correlate([hs_a, hs_b, gm_a, gm_b])
    assert len(groups) == 2
    by_ref = {g.primary_entity_ref: g for g in groups}
    assert {s.signal_id for s in by_ref["deal-A"].signals} == {
        hs_a.signal_id,
        gm_a.signal_id,
    }
    assert {s.signal_id for s in by_ref["deal-B"].signals} == {
        hs_b.signal_id,
        gm_b.signal_id,
    }


def test_calendar_meeting_with_matching_contact_email_joins_deal():
    hs = _hubspot_stale()
    cal = _calendar_meeting(contact_email="buyer@acmecorp.example")
    groups = correlate([hs, cal])
    assert len(groups) == 1
    g = groups[0]
    assert g.primary_entity_kind == "deal"
    assert "calendar" in g.sources


def test_transcript_with_matching_contact_email_joins_deal():
    hs = _hubspot_stale()
    tx = _transcript(contact_email="buyer@acmecorp.example")
    groups = correlate([hs, tx])
    assert len(groups) == 1
    g = groups[0]
    assert g.primary_entity_kind == "deal"
    assert "transcript" in g.sources


def test_transcript_event_id_does_not_join_calendar_event_id():
    # KEYSPACE LANDMINE: transcript.event_id is a Fathom meeting_id, calendar
    # .event_id is a Google Calendar event_id. Sharing the same string value
    # MUST NOT cause a join — there's no contact_email overlap here.
    cal = _calendar_meeting(
        event_id="ev-collision",
        contact_email="cal-contact@example.com",
        company_name="example",
    )
    tx = _transcript(
        transcript_id="tx-77",
        event_id="ev-collision",  # same string, different keyspace
        contact_email=None,
        company_name=None,
    )
    groups = correlate([cal, tx])
    assert len(groups) == 2
    kinds = {g.primary_entity_kind for g in groups}
    # Calendar lands in a contact group (only signal with that contact_email)
    # and transcript lands in its own transcript group — they MUST NOT merge.
    assert "transcript" in kinds


def test_two_users_signals_are_user_scoped():
    a = _hubspot_stale(user="alice@acme.example", deal_id="deal-A")
    b = _hubspot_stale(user="bob@acme.example", deal_id="deal-B")
    # Both deals happen to share a contact_email — must still NOT cross users.
    a_gm = _gmail_unread(
        user="alice@acme.example", contact_email="buyer@acmecorp.example"
    )
    b_gm = _gmail_unread(
        user="bob@acme.example", contact_email="buyer@acmecorp.example"
    )
    groups = correlate([a, b, a_gm, b_gm])
    assert len(groups) == 2
    for g in groups:
        users_in_group = {s.user_email for s in g.signals}
        assert len(users_in_group) == 1
        assert g.user_email in users_in_group


def test_correlation_ids_are_deterministic_across_runs():
    signals = [
        _hubspot_stale(
            deal_id="deal-1",
            contact_email="buyer@acmecorp.example",
            company_name="Acme",
        ),
        _gmail_unread(thread_id="t-1", contact_email="buyer@acmecorp.example"),
        _slack_mention(channel_name="deals-acme"),
    ]
    first = {g.correlation_id for g in correlate(signals)}
    second = {g.correlation_id for g in correlate(signals)}
    assert first == second
    assert "deal:deal-1" in first


def test_slack_unmatched_channel_falls_back_to_channel_group():
    sl = _slack_mention(channel_id="C-RANDOM", channel_name="random")
    groups = correlate([sl])
    assert len(groups) == 1
    g = groups[0]
    assert g.primary_entity_kind == "channel"
    assert g.primary_entity_ref == "C-RANDOM"
    assert g.correlation_id == "channel:C-RANDOM"


def test_orphan_singleton_uses_signal_id():
    # Calendar with no event_id and no contact_email — falls through to orphan.
    sig = Signal(
        user_email="rep@acme.example",
        source="calendar",
        signal_type="upcoming_meeting_with_customer",
        summary="weird",
        occurred_at=_ts(),
        entity_refs={},
    )
    groups = correlate([sig])
    assert len(groups) == 1
    g = groups[0]
    assert g.primary_entity_kind == "orphan"
    assert g.correlation_id == f"orphan:{sig.signal_id}"


def test_group_accessors():
    hs = _hubspot_stale()
    gm = _gmail_unread()
    groups = correlate([hs, gm])
    g = groups[0]
    assert g.sources == ["gmail", "hubspot"]
    assert "stale_lead" in g.signal_types
    assert "unread_thread" in g.signal_types
    assert g.latest_signal_at == max(hs.occurred_at, gm.occurred_at)
    d = g.to_dict()
    assert d["correlation_id"] == "deal:deal-1"
    assert d["primary_entity_kind"] == "deal"
    assert len(d["signals"]) == 2


def test_explicit_email_to_deal_override_is_used():
    # No HubSpot signal in the batch — pre-resolved lookup pulls gmail in.
    gm = _gmail_unread(thread_id="t-x", contact_email="buyer@acmecorp.example")
    c = Correlator(email_to_deal={"buyer@acmecorp.example": "deal-EXT"})
    # Without a HubSpot anchor there's no deal_group to land in; gmail falls
    # to contact-group. The override is exercised only when a HubSpot signal
    # also seeds the group — test that path too.
    hs = _hubspot_stale(deal_id="deal-EXT", contact_email=None, company_name="Acme")
    groups = c.correlate([gm, hs])
    assert len(groups) == 1
    g = groups[0]
    assert g.primary_entity_kind == "deal"
    assert g.primary_entity_ref == "deal-EXT"

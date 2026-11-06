"""UC-17.2 — Gmail SignalSource.

Mocks the Gmail layer entirely (no live API). Covers:
  - weekend-aware lookback widening
  - external unread-thread emission
  - awaiting-reply detection past 48h
  - newsletter / no-reply severity downgrade
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

# ─── Thread fixture helpers ───────────────────────────────────────────────────


def _msg(
    *,
    msg_id: str,
    from_addr: str,
    subject: str = "Re: pricing",
    date: datetime,
    unread: bool = False,
    list_unsubscribe: bool = False,
) -> dict[str, Any]:
    headers = [
        {"name": "From", "value": from_addr},
        {"name": "Subject", "value": subject},
        {"name": "Date", "value": format_datetime(date)},
    ]
    if list_unsubscribe:
        headers.append(
            {"name": "List-Unsubscribe", "value": "<mailto:unsub@x.example>"}
        )
    return {
        "id": msg_id,
        "labelIds": ["UNREAD"] if unread else [],
        "payload": {"headers": headers},
    }


def _thread(thread_id: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    return {"id": thread_id, "messages": messages}


def _service_mock(threads: list[dict[str, Any]]) -> MagicMock:
    svc = MagicMock()
    svc.list_threads_for_signals.return_value = threads
    return svc


# ─── Weekend-aware lookback ───────────────────────────────────────────────────


def test_monday_early_morning_widens_to_friday_eob_local():
    from backend.signals.gmail import _maybe_widen_to_friday_eob

    # Mon 2026-05-11 02:00 UTC = Sun 2026-05-10 20:00 America/Denver.
    now = datetime(2026, 5, 11, 2, 0, tzinfo=timezone.utc)
    since = now - timedelta(hours=24)  # Sun 02:00 UTC
    effective, widened = _maybe_widen_to_friday_eob(
        since,
        now=now,
        tz_name="America/Denver",
    )
    assert widened is True
    # Most recent Friday 17:00 Denver = 2026-05-08 17:00 MDT (UTC-6) = 23:00 UTC.
    friday_local = datetime(2026, 5, 8, 17, 0, tzinfo=ZoneInfo("America/Denver"))
    assert effective == friday_local.astimezone(timezone.utc)
    assert effective < since


def test_midweek_now_does_not_widen():
    from backend.signals.gmail import _maybe_widen_to_friday_eob

    # Wed 2026-05-13 15:00 UTC — squarely mid-week in Denver.
    now = datetime(2026, 5, 13, 15, 0, tzinfo=timezone.utc)
    since = now - timedelta(hours=24)
    effective, widened = _maybe_widen_to_friday_eob(
        since,
        now=now,
        tz_name="America/Denver",
    )
    assert widened is False
    assert effective == since


def test_monday_after_eob_does_not_widen():
    from backend.signals.gmail import _maybe_widen_to_friday_eob

    # Mon 2026-05-11 23:30 UTC = Mon 17:30 Denver — past Friday cutoff hour.
    now = datetime(2026, 5, 11, 23, 30, tzinfo=timezone.utc)
    since = now - timedelta(hours=24)
    effective, widened = _maybe_widen_to_friday_eob(
        since,
        now=now,
        tz_name="America/Denver",
    )
    assert widened is False
    assert effective == since


# ─── unread_thread ────────────────────────────────────────────────────────────


def test_unread_external_thread_emits_unread_thread_signal():
    from backend.signals.gmail import GmailSignalSource

    rep = "rep1@acme.example"
    sent_at = datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc)
    thread = _thread(
        "t-1",
        [
            _msg(
                msg_id="m-1",
                from_addr="Jane Buyer <jane@acmecorp.example>",
                subject="Pricing question follow-up",
                date=sent_at,
                unread=True,
            ),
        ],
    )
    svc = _service_mock([thread])

    # Wed `now` — no widening, isolates this test from weekend logic.
    src = GmailSignalSource(
        gmail_service=svc,
        now=datetime(2026, 5, 13, 15, 0, tzinfo=timezone.utc),
    )
    sigs = src.fetch(rep, since=datetime(2026, 5, 12, 15, 0, tzinfo=timezone.utc))

    assert len(sigs) == 1
    s = sigs[0]
    assert s.signal_type == "unread_thread"
    assert s.source == "gmail"
    assert s.entity_refs["thread_id"] == "t-1"
    assert s.entity_refs["contact_email"] == "jane@acmecorp.example"
    assert "jane@acmecorp.example" in s.summary
    assert "Pricing question follow-up" in s.summary
    assert s.severity_hint == "medium"
    assert s.payload["lookback_widened_to_friday"] is False
    assert s.payload["is_unread"] is True


# ─── awaiting_reply ───────────────────────────────────────────────────────────


def test_awaiting_reply_when_rep_was_last_and_over_48h():
    from backend.signals.gmail import GmailSignalSource

    rep = "rep1@acme.example"
    now = datetime(2026, 5, 13, 15, 0, tzinfo=timezone.utc)
    inbound = datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc)
    outbound = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)  # 3 days ago

    thread = _thread(
        "t-2",
        [
            _msg(
                msg_id="m-a",
                from_addr="jane@acmecorp.example",
                subject="Pricing question",
                date=inbound,
                unread=False,
            ),
            _msg(
                msg_id="m-b",
                from_addr=f"Rep One <{rep}>",
                subject="Re: Pricing question",
                date=outbound,
                unread=False,
            ),
        ],
    )
    svc = _service_mock([thread])
    src = GmailSignalSource(gmail_service=svc, now=now)
    sigs = src.fetch(rep, since=now - timedelta(days=5))

    assert len(sigs) == 1
    s = sigs[0]
    assert s.signal_type == "awaiting_reply"
    assert s.entity_refs["thread_id"] == "t-2"
    assert s.entity_refs["contact_email"] == "jane@acmecorp.example"
    assert s.severity_hint == "medium"
    assert s.payload["hours_since_rep_message"] >= 48


def test_no_awaiting_reply_when_rep_message_under_48h():
    from backend.signals.gmail import GmailSignalSource

    rep = "rep1@acme.example"
    now = datetime(2026, 5, 13, 15, 0, tzinfo=timezone.utc)
    inbound = now - timedelta(hours=30)
    outbound = now - timedelta(hours=12)  # fresh; rep just replied

    thread = _thread(
        "t-3",
        [
            _msg(
                msg_id="m-a",
                from_addr="jane@acmecorp.example",
                subject="Q",
                date=inbound,
            ),
            _msg(msg_id="m-b", from_addr=rep, subject="Re: Q", date=outbound),
        ],
    )
    svc = _service_mock([thread])
    src = GmailSignalSource(gmail_service=svc, now=now)
    sigs = src.fetch(rep, since=now - timedelta(days=2))
    assert sigs == []


# ─── Newsletter severity ──────────────────────────────────────────────────────


def test_list_unsubscribe_header_marks_newsletter_low_severity():
    from backend.signals.gmail import GmailSignalSource

    rep = "rep1@acme.example"
    now = datetime(2026, 5, 13, 15, 0, tzinfo=timezone.utc)
    sent = now - timedelta(hours=6)
    thread = _thread(
        "t-news",
        [
            _msg(
                msg_id="m-n",
                from_addr="Industry Weekly <news@industrydaily.com>",
                subject="Top 10 industry trends",
                date=sent,
                unread=True,
                list_unsubscribe=True,
            ),
        ],
    )
    svc = _service_mock([thread])
    src = GmailSignalSource(gmail_service=svc, now=now)
    sigs = src.fetch(rep, since=now - timedelta(days=1))
    assert len(sigs) == 1
    assert sigs[0].severity_hint == "low"


def test_noreply_local_part_marks_newsletter_low_severity():
    from backend.signals.gmail import GmailSignalSource

    rep = "rep1@acme.example"
    now = datetime(2026, 5, 13, 15, 0, tzinfo=timezone.utc)
    sent = now - timedelta(hours=6)
    thread = _thread(
        "t-nr",
        [
            _msg(
                msg_id="m-nr",
                from_addr="noreply@billing.example.com",
                subject="Your monthly invoice",
                date=sent,
                unread=True,
            ),
        ],
    )
    svc = _service_mock([thread])
    src = GmailSignalSource(gmail_service=svc, now=now)
    sigs = src.fetch(rep, since=now - timedelta(days=1))
    assert len(sigs) == 1
    assert sigs[0].severity_hint == "low"


# ─── Protocol conformance ─────────────────────────────────────────────────────


def test_gmail_source_satisfies_signal_source_protocol():
    from backend.signals import SignalSource
    from backend.signals.gmail import GmailSignalSource

    src = GmailSignalSource(gmail_service=_service_mock([]))
    assert isinstance(src, SignalSource)

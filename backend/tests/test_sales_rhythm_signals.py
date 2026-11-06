"""The rep's own Gmail and Calendar as contact signals (rubik_core/sales/rhythm_signals.py).

Two halves: the signal module against fake Google services, and each rhythm
check reading canned touches — including the rule that the team and a named
colleague's view never read anyone's email or calendar. Every date is pinned
(``NOW``), so nothing here drifts with the real clock.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone

import pytest

from backend.tests.test_sales_rhythm_checks import NOW, TODAY, FakeHubSpot, _iso
from rubik_core.sales import rhythm_checks as rc
from rubik_core.sales import rhythm_signals as rs
from rubik_core.tenancy.hubspot_config import HubSpotConnectorConfig

UTC = timezone.utc


def _dt(y, m, d, h=17):
    return datetime(y, m, d, h, 0, tzinfo=UTC)


# ── fakes for the Google side ────────────────────────────────────────────────


class _Req:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class _HttpError(Exception):
    def __init__(self, status, content=b""):
        super().__init__(
            f"<HttpError {status} when requesting https://gmail/...q=from:(ann@acmecorp.alt)>"
        )
        self.resp = type("R", (), {"status": status})()
        self.content = content


class _Batch:
    def __init__(self, callback, owner):
        self.callback = callback
        self.owner = owner
        self.items = []

    def add(self, req, request_id):
        self.items.append((request_id, req))

    def execute(self):
        self.owner.batch_sizes.append(len(self.items))
        for rid, req in self.items:
            try:
                self.callback(rid, req.execute(), None)
            except Exception as exc:  # noqa: BLE001
                self.callback(rid, None, exc)


class FakeGmail:
    """Messages: {id, from, to:[..], subject, at, labels:{SENT,DRAFT,...}}.

    Evaluates the parts of a query the module sends: ``in:sent to:(…)`` for
    what the rep sent, ``from:(…)`` for what the client side sent, the
    automatic-mail exclusions, and ``after:``/``before:`` epoch seconds."""

    def __init__(self, me, messages):
        self.me = me
        self.msgs = messages
        self.queries = []
        self.batch_sizes = []
        self.throttle_first = 0  # make the first N requests answer 429
        self.fail_with = None  # make every request fail with this status

    def users(self):
        return self

    def messages(self):
        return self

    def new_batch_http_request(self, callback):
        return _Batch(callback, self)

    @staticmethod
    def _terms(group):
        return [t.strip() for t in group.strip("()").split(" OR ")]

    def _hit(self, msg, q):
        labels = msg.get("labels", set())
        if "-in:drafts" in q and "DRAFT" in labels:
            return False
        if q.startswith("in:sent"):
            if "SENT" not in labels:
                return False
            to = re.search(r"to:(\(.*?\))", q)
            if to and not any(
                rs.matches(a, self._terms(to.group(1))) for a in msg["to"]
            ):
                return False
        else:
            frm = re.match(r"from:(\(.*?\))", q)
            if not frm or not rs.matches(msg["from"], self._terms(frm.group(1))):
                return False
            subject = msg.get("subject", "").lower()
            if "-subject:" in q and (
                "automatic reply" in subject or "out of office" in subject
            ):
                return False
            if "-from:(noreply" in q and "noreply" in msg["from"]:
                return False
        after = re.search(r"after:(\d+)", q)
        before = re.search(r"before:(\d+)", q)
        ts = msg["at"].timestamp()
        if after and ts < int(after.group(1)):
            return False
        if before and ts >= int(before.group(1)):
            return False
        return True

    def _guard(self):
        if self.fail_with:
            raise _HttpError(self.fail_with)
        if self.throttle_first > 0:
            self.throttle_first -= 1
            raise _HttpError(429)

    def list(self, userId, q, maxResults, fields=None, pageToken=None):  # noqa: A003
        self.queries.append(q)

        def run():
            self._guard()
            hits = sorted(
                (m for m in self.msgs if self._hit(m, q)),
                key=lambda m: -m["at"].timestamp(),
            )
            return {"messages": [{"id": m["id"]} for m in hits[:maxResults]]}

        return _Req(run)

    def get(self, userId, id, format, fields=None, metadataHeaders=None):  # noqa: A002
        msg = next(m for m in self.msgs if m["id"] == id)

        def run():
            self._guard()
            out = {"id": id, "internalDate": str(int(msg["at"].timestamp() * 1000))}
            if format == "metadata":
                out["payload"] = {
                    "headers": [
                        {"name": "To", "value": ", ".join(msg["to"])},
                        {"name": "Subject", "value": msg.get("subject", "")},
                    ]
                }
            return out

        return _Req(run)


class FakeCalendarService:
    def __init__(self):
        self.pages: list[list[dict]] = [[]]
        self.calls = []

    def events(self):
        return self

    def list(self, **params):  # noqa: A003
        self.calls.append(params)
        token = params.get("pageToken")
        index = int(token) if token else 0

        def run():
            page = {"items": self.pages[index]}
            if index + 1 < len(self.pages):
                page["nextPageToken"] = str(index + 1)
            return page

        return _Req(run)


def _event(
    summary, start, attendees, *, status="confirmed", organizer=None, all_day=None
):
    people = []
    for a in attendees:
        if isinstance(a, tuple):
            people.append({"email": a[0], "responseStatus": a[1]})
        else:
            people.append({"email": a, "responseStatus": "accepted"})
    event = {"summary": summary, "status": status, "attendees": people}
    event["start"] = {"date": all_day} if all_day else {"dateTime": start.isoformat()}
    if organizer:
        event["organizer"] = {"email": organizer}
    return event


@pytest.fixture
def google(monkeypatch):
    rs.clear_cache()
    gmail = FakeGmail("ae@acme.example", [])
    cal = FakeCalendarService()
    monkeypatch.setattr(rs, "_gmail_service", lambda mailbox: gmail)
    monkeypatch.setattr(rs, "_calendar_service", lambda mailbox: cal)
    monkeypatch.setattr(rs.time, "sleep", lambda _s: None)
    gmail.cal = cal
    yield gmail
    rs.clear_cache()


def _sent(mid, to, at, subject="", draft=False):
    return {
        "id": mid,
        "from": "ae@acme.example",
        "to": to,
        "at": at,
        "subject": subject,
        "labels": {"DRAFT"} if draft else {"SENT"},
    }


def _received(mid, frm, at, subject=""):
    return {
        "id": mid,
        "from": frm,
        "to": ["ae@acme.example"],
        "at": at,
        "subject": subject,
        "labels": {"INBOX"},
    }


# ── matching ─────────────────────────────────────────────────────────────────


def test_terms_widen_business_addresses_to_their_company_only():
    terms = rs.match_terms(
        ["Ann@Acmecorp.alt", "bob@acmecorp.alt", "jo@gmail.com", "me@acme.example", ""],
        "acme.example",
    )
    assert terms == ["acmecorp.alt", "jo@gmail.com"]
    assert rs.matches("carol@acmecorp.alt", terms)
    assert not rs.matches("someone@gmail.com", terms)
    assert rs.exact_terms(["Ann@Acmecorp.alt", "me@acme.example"], "acme.example") == [
        "ann@acmecorp.alt"
    ]


# ── Gmail ────────────────────────────────────────────────────────────────────


def test_collect_reads_both_directions_and_both_sides_of_the_calendar(google):
    google.msgs = [
        _received("1", "ann@acmecorp.alt", _dt(2026, 9, 25)),
        _sent("2", ["bob@acmecorp.alt"], _dt(2026, 9, 29)),
        _received("3", "x@other.com", _dt(2026, 9, 30)),
    ]
    google.cal.pages = [
        [
            _event(
                "Acme review", _dt(2026, 9, 20), ["ann@acmecorp.alt", "ae@acme.example"]
            ),
            _event("Acme kickoff", _dt(2026, 10, 8), ["bob@acmecorp.alt"]),
            _event(
                "Cancelled", _dt(2026, 10, 3), ["ann@acmecorp.alt"], status="cancelled"
            ),
        ]
    ]
    report = rs.collect("ae@acme.example", {"d1": ["acmecorp.alt"], "d2": []}, now=NOW)
    t = report.touches["d1"]
    assert report.complete
    assert t.last_in == _dt(2026, 9, 25)
    assert t.last_out == _dt(2026, 9, 29)
    assert t.last_meeting == _dt(2026, 9, 20) == t.last_meeting_accepted
    assert t.next_meeting == _dt(2026, 10, 8)  # the cancelled one does not count
    assert t.last_contact == _dt(2026, 9, 29)
    assert report.touches["d2"] == rs.Touch()


def test_an_unsent_draft_is_never_a_sent_email(google):
    google.msgs = [_sent("1", ["ann@acmecorp.alt"], _dt(2026, 9, 30), draft=True)]
    report = rs.collect("ae@acme.example", {"d1": ["acmecorp.alt"]}, now=NOW)
    assert report.touches["d1"].last_out is None
    assert all(q.startswith("in:sent") for q in google.queries if "to:(" in q)


def test_an_auto_reply_is_not_the_client_answering(google):
    google.msgs = [
        _received(
            "1",
            "ann@acmecorp.alt",
            _dt(2026, 9, 30),
            subject="Automatic reply: Proposal",
        ),
        _received(
            "2", "noreply@acmecorp.alt", _dt(2026, 9, 30), subject="Daily rate sheet"
        ),
    ]
    report = rs.collect("ae@acme.example", {"d1": ["acmecorp.alt"]}, now=NOW)
    assert report.touches["d1"].last_in is None


def test_throttled_requests_are_retried_not_reported_as_unreadable(google):
    google.msgs = [_received("1", "ann@acmecorp.alt", _dt(2026, 9, 25))]
    google.throttle_first = 3
    report = rs.collect("ae@acme.example", {"d1": ["acmecorp.alt"]}, now=NOW)
    assert report.gmail_ok
    assert report.touches["d1"].last_in == _dt(2026, 9, 25)


def test_batches_stay_small(google):
    terms = {f"d{i}": [f"c{i}.com"] for i in range(30)}
    rs.collect("ae@acme.example", terms, now=NOW)
    assert max(google.batch_sizes) <= 20


def test_a_gmail_failure_is_a_note_and_never_logs_a_client_address(google, caplog):
    google.fail_with = 400
    caplog.set_level(logging.WARNING)
    report = rs.collect("ae@acme.example", {"d1": ["ann@acmecorp.alt"]}, now=NOW)
    assert not report.gmail_ok and report.calendar_ok
    assert "Gmail could not be read" in report.notes[0]
    assert "ann@acmecorp.alt" not in caplog.text
    assert "400" in caplog.text


# ── Calendar ─────────────────────────────────────────────────────────────────


def test_a_client_who_declined_is_not_contact_and_an_invite_is_not_an_answer(google):
    google.cal.pages = [
        [
            _event(
                "Declined by client",
                _dt(2026, 9, 28),
                [("ann@acmecorp.alt", "declined")],
            ),
            _event(
                "Never answered",
                _dt(2026, 9, 27),
                [("ann@acmecorp.alt", "needsAction")],
            ),
            _event(
                "Upcoming, declined",
                _dt(2026, 10, 9),
                [("ann@acmecorp.alt", "declined")],
            ),
        ]
    ]
    t = rs.collect("ae@acme.example", {"d1": ["acmecorp.alt"]}, now=NOW).touches["d1"]
    assert t.last_meeting == _dt(2026, 9, 27)  # held, but they never accepted…
    assert t.last_meeting_accepted is None  # …so it is not them answering
    assert t.next_meeting is None


def test_rooms_group_calendars_and_meetings_you_declined_do_not_count(google):
    google.cal.pages = [
        [
            _event(
                "Room only", _dt(2026, 9, 28), ["c_123@resource.calendar.google.com"]
            ),
            _event(
                "You declined",
                _dt(2026, 9, 26),
                [("ann@acmecorp.alt", "accepted"), ("ae@acme.example", "declined")],
            ),
        ]
    ]
    t = rs.collect("ae@acme.example", {"d1": ["acmecorp.alt"]}, now=NOW).touches["d1"]
    assert t.last_meeting is None


def test_the_organizer_counts_even_when_not_in_the_guest_list(google):
    google.cal.pages = [
        [_event("Client's call", _dt(2026, 9, 29), [], organizer="ann@acmecorp.alt")]
    ]
    t = rs.collect("ae@acme.example", {"d1": ["acmecorp.alt"]}, now=NOW).touches["d1"]
    assert t.last_meeting_accepted == _dt(2026, 9, 29)


def test_every_calendar_page_is_read_and_a_too_long_window_is_unreadable(
    monkeypatch, google
):
    google.cal.pages = [
        [],
        [_event("Page two", _dt(2026, 10, 8), ["ann@acmecorp.alt"])],
    ]
    t = rs.collect("ae@acme.example", {"d1": ["acmecorp.alt"]}, now=NOW).touches["d1"]
    assert t.next_meeting == _dt(2026, 10, 8)
    rs.clear_cache()
    monkeypatch.setattr(rs, "_MAX_CALENDAR_PAGES", 1)
    report = rs.collect("ae@acme.example", {"d1": ["acmecorp.alt"]}, now=NOW)
    assert not report.calendar_ok
    assert any("calendar could not be read" in n for n in report.notes)


def test_an_all_day_event_lands_on_its_local_day(google):
    start = rs._event_start({"start": {"date": "2026-10-01"}})
    assert start.date() == date(2026, 10, 1)
    assert (
        start.utcoffset() is not None and start.utcoffset().total_seconds() < 0
    )  # Chicago


def test_only_used_fields_are_requested(google):
    google.msgs = [_received("1", "ann@acmecorp.alt", _dt(2026, 9, 25))]
    rs.collect("ae@acme.example", {"d1": ["acmecorp.alt"]}, now=NOW)
    assert "attendees(email" in google.cal.calls[0]["fields"]
    assert "description" not in google.cal.calls[0]["fields"]


# ── the recap ────────────────────────────────────────────────────────────────


def test_day_activity_lists_external_sent_mail_and_meetings_only(google):
    start, end = _dt(2026, 10, 1, 5), _dt(2026, 10, 2, 5)
    google.msgs = [
        _sent("1", ["ann@acmecorp.alt"], _dt(2026, 10, 1, 15), subject="Proposal"),
        _sent("2", ["boss@acme.example"], _dt(2026, 10, 1, 16), subject="Internal"),
        _sent("3", ["ann@acmecorp.alt"], _dt(2026, 9, 30, 15), subject="Yesterday"),
        _sent(
            "4",
            ["ann@acmecorp.alt"],
            _dt(2026, 10, 1, 17),
            subject="Unsent",
            draft=True,
        ),
    ]
    google.cal.pages = [
        [
            _event(
                "Acme call",
                _dt(2026, 10, 1, 18),
                ["ann@acmecorp.alt", "ae@acme.example"],
            ),
            _event("Team standup", _dt(2026, 10, 1, 14), ["boss@acme.example"]),
        ]
    ]
    act = rs.day_activity("ae@acme.example", start, end)
    assert [m["subject"] for m in act.emails] == ["Proposal"]
    assert act.emails[0]["to"] == ["ann@acmecorp.alt"]
    assert [m["title"] for m in act.meetings] == ["Acme call"]
    assert act.complete


# ── the checks reading canned touches ────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    rc.clear_cache()
    monkeypatch.setattr(rc, "_today", lambda: TODAY)
    monkeypatch.setattr(
        rc,
        "get_hubspot_config",
        lambda: HubSpotConnectorConfig(pipeline_ids=("default",), portal_id="9100001"),
    )
    yield
    rc.clear_cache()


def _signals(touches, *, gmail_ok=True, calendar_ok=True, calls=None):
    def provider(mailbox, deal_terms, *, gmail=True, calendar=True, **_kw):
        if calls is not None:
            calls.append(
                {
                    "mailbox": mailbox,
                    "terms": dict(deal_terms),
                    "gmail": gmail,
                    "calendar": calendar,
                }
            )
        report = rs.SignalReport(
            touches={d: touches.get(d, rs.Touch()) for d in deal_terms},
            gmail_ok=gmail and gmail_ok,
            calendar_ok=calendar and calendar_ok,
        )
        if gmail and not gmail_ok:
            report.notes.append("Your Gmail could not be read just now.")
        return report

    return provider


@pytest.fixture
def hs():
    fake = FakeHubSpot()
    fake.add(
        "contacts",
        "c1",
        firstname="Ann",
        lastname="Lee",
        email="ann@acmecorp.alt",
        hs_buying_role="DECISION_MAKER",
    )
    return fake


def _me(hs):
    return rc.resolve_scope(hs, "ae@acme.example", own_mailbox=True)


def test_your_own_scope_carries_your_mailbox_and_others_do_not(hs):
    assert _me(hs).mailbox == "ae@acme.example"
    # Without the explicit flag a scope resolved by address never gets a
    # mailbox — the safe default for any view of someone else.
    assert rc.resolve_scope(hs, "ae@acme.example").mailbox is None
    assert rc.resolve_scope(hs, None, team=True).mailbox is None
    named = rc.resolve_scope(hs, "ebrooks@acme.example", owner_id="77")
    assert named.mailbox is None
    assert named.cache_key != _me(hs).cache_key  # never share a cached answer


def test_an_email_you_did_not_log_stops_a_deal_reading_as_quiet(monkeypatch, hs):
    hs.add(
        "deals",
        "1",
        dealname="Emailed yesterday",
        hubspot_owner_id="77",
        notes_last_contacted=_iso(2026, 9, 1),
    )
    hs.link("deals", "contacts", "1", "c1")
    monkeypatch.setattr(
        rc, "SIGNALS", _signals({"1": rs.Touch(last_out=_dt(2026, 9, 30))})
    )
    assert rc.close_date_slippage(hs, _me(hs), now=NOW).rows == []


def test_without_email_the_same_deal_is_quiet(monkeypatch, hs):
    hs.add(
        "deals",
        "1",
        dealname="Truly quiet",
        hubspot_owner_id="77",
        notes_last_contacted=_iso(2026, 9, 1),
    )
    hs.link("deals", "contacts", "1", "c1")
    monkeypatch.setattr(rc, "SIGNALS", _signals({}))
    result = rc.close_date_slippage(hs, _me(hs), now=NOW)
    assert result.rows[0].reasons == ["no client contact in 30 days"]
    assert result.sources == ("hubspot", "calendar", "gmail")


def test_when_gmail_cannot_be_read_the_wording_says_logged(monkeypatch, hs):
    hs.add(
        "deals",
        "1",
        dealname="Quiet",
        hubspot_owner_id="77",
        notes_last_contacted=_iso(2026, 9, 1),
    )
    hs.link("deals", "contacts", "1", "c1")
    monkeypatch.setattr(rc, "SIGNALS", _signals({}, gmail_ok=False))
    result = rc.close_date_slippage(hs, _me(hs), now=NOW)
    assert result.rows[0].reasons == ["no logged client contact in 30 days"]
    assert result.partial
    block = rc.format_for_chat(result, _me(hs))
    assert "your Gmail could not be read this time" in block
    assert "plus your own calendar" in block


def test_an_upcoming_meeting_is_a_booked_next_step(monkeypatch, hs):
    hs.add(
        "deals",
        "1",
        dealname="Meeting booked",
        hubspot_owner_id="77",
        closedate=_iso(2026, 10, 20),
        notes_last_contacted=_iso(2026, 9, 29),
    )
    hs.link("deals", "contacts", "1", "c1")
    monkeypatch.setattr(
        rc, "SIGNALS", _signals({"1": rs.Touch(next_meeting=_dt(2026, 10, 6))})
    )
    assert rc.closing_focus(hs, _me(hs), now=NOW).rows == []


def test_missing_fields_asks_the_calendar_only(monkeypatch, hs):
    hs.add(
        "deals",
        "1",
        dealname="Touched today",
        amount="1",
        closedate=_iso(2026, 11, 1),
        hs_lastmodifieddate=_iso(2026, 10, 1, 15),
        hs_updated_by_user_id="9001",
    )
    hs.link("deals", "contacts", "1", "c1")
    calls = []
    monkeypatch.setattr(
        rc,
        "SIGNALS",
        _signals({"1": rs.Touch(next_meeting=_dt(2026, 10, 6))}, calls=calls),
    )
    result = rc.missing_fields(hs, _me(hs), day=TODAY)
    assert result.rows == []  # the booked meeting is the next step
    assert calls and calls[0]["gmail"] is False and calls[0]["calendar"] is True
    assert not result.partial


def test_a_reply_by_email_means_you_are_not_waiting(monkeypatch, hs):
    hs.add(
        "deals",
        "1",
        dealname="They replied",
        hubspot_owner_id="77",
        dealstage="presentationscheduled",
        notes_last_contacted=_iso(2026, 9, 21),
    )
    hs.link("deals", "contacts", "1", "c1")
    monkeypatch.setattr(
        rc, "SIGNALS", _signals({"1": rs.Touch(last_in=_dt(2026, 9, 28))})
    )
    assert rc.waiting_on_decision_maker(hs, _me(hs), now=NOW).rows == []


def test_an_unlogged_email_starts_the_waiting_clock(monkeypatch, hs):
    hs.add(
        "deals",
        "1",
        dealname="Waiting by email",
        hubspot_owner_id="77",
        dealstage="presentationscheduled",
    )
    hs.link("deals", "contacts", "1", "c1")
    calls = []
    monkeypatch.setattr(
        rc, "SIGNALS", _signals({"1": rs.Touch(last_out=_dt(2026, 9, 22))}, calls=calls)
    )
    rows = rc.waiting_on_decision_maker(hs, _me(hs), now=NOW).rows
    assert [r.title for r in rows] == ["Waiting by email"]
    assert "no reply since" in rows[0].reasons[0]
    assert calls[0]["terms"]["1"] == [
        "ann@acmecorp.alt"
    ]  # the decision-maker, not their company


def test_only_an_accepted_meeting_counts_as_their_answer(monkeypatch, hs):
    hs.add(
        "deals",
        "1",
        dealname="Invited, never accepted",
        hubspot_owner_id="77",
        dealstage="presentationscheduled",
        notes_last_contacted=_iso(2026, 9, 21),
    )
    hs.add(
        "deals",
        "2",
        dealname="Met since",
        hubspot_owner_id="77",
        dealstage="presentationscheduled",
        notes_last_contacted=_iso(2026, 9, 21),
    )
    hs.link("deals", "contacts", "1", "c1")
    hs.link("deals", "contacts", "2", "c1")
    monkeypatch.setattr(
        rc,
        "SIGNALS",
        _signals(
            {
                "1": rs.Touch(last_meeting=_dt(2026, 9, 24)),
                "2": rs.Touch(
                    last_meeting=_dt(2026, 9, 24),
                    last_meeting_accepted=_dt(2026, 9, 24),
                ),
            }
        ),
    )
    rows = rc.waiting_on_decision_maker(hs, _me(hs), now=NOW).rows
    assert [r.title for r in rows] == ["Invited, never accepted"]


def test_the_team_and_named_views_never_read_anyone_s_mail_or_calendar(monkeypatch, hs):
    hs.add(
        "deals",
        "1",
        dealname="Team deal",
        hubspot_owner_id="88",
        closedate=_iso(2026, 10, 20),
        dealstage="presentationscheduled",
        notes_last_contacted=_iso(2026, 9, 1),
        hs_lastmodifieddate=_iso(2026, 10, 1, 15),
        hs_updated_by_user_id="9002",
    )
    hs.link("deals", "contacts", "1", "c1")
    calls = []
    monkeypatch.setattr(rc, "SIGNALS", _signals({}, calls=calls))
    monkeypatch.setattr(
        rc, "DAY_ACTIVITY", lambda *a, **k: calls.append("day") or rs.DayActivity()
    )
    team = rc.resolve_scope(hs, None, team=True)
    named = rc.resolve_scope(hs, "ebrooks@acme.example", owner_id="88")
    for scope in (team, named):
        assert rc.closing_focus(hs, scope, now=NOW).rows  # the deal IS read…
        rc.close_date_slippage(hs, scope, now=NOW)
        rc.waiting_on_decision_maker(hs, scope, now=NOW)
        rc.activity_today(hs, scope, day=TODAY)
    rc.missing_fields(hs, team, day=TODAY)
    assert calls == []  # …but no mailbox or calendar ever is
    block = rc.format_for_chat(rc.closing_focus(hs, team, now=NOW), team)
    assert "does not read anyone's email or calendar" in block


def test_your_answer_says_which_sources_it_counted(monkeypatch, hs):
    monkeypatch.setattr(rc, "SIGNALS", _signals({}))
    hs.add(
        "deals", "1", dealname="D", hubspot_owner_id="77", closedate=_iso(2026, 10, 20)
    )
    hs.link("deals", "contacts", "1", "c1")
    block = rc.format_for_chat(rc.closing_focus(hs, _me(hs), now=NOW), _me(hs))
    assert "plus your own calendar and Gmail" in block
    single = rc.format_for_chat(rc.single_threaded(hs, _me(hs)), _me(hs))
    assert (
        "Reflects what is logged in HubSpot" in single
    )  # never claims sources it did not read


def test_an_unreadable_mailbox_makes_the_answer_partial_and_uncached(monkeypatch, hs):
    hs.add(
        "deals",
        "1",
        dealname="D",
        hubspot_owner_id="77",
        notes_last_contacted=_iso(2026, 9, 1),
    )
    hs.link("deals", "contacts", "1", "c1")
    monkeypatch.setattr(rc, "SIGNALS", _signals({}, gmail_ok=False))
    first = rc.run_check("close_date_slippage", hs, _me(hs))
    assert first.partial and "Gmail could not be read" in " ".join(first.notes)
    monkeypatch.setattr(
        rc, "SIGNALS", _signals({"1": rs.Touch(last_in=datetime.now(UTC))})
    )
    assert rc.run_check("close_date_slippage", hs, _me(hs)).rows == []


def test_your_recap_adds_unlogged_mail_and_meetings_once(monkeypatch, hs):
    hs.add(
        "emails",
        "e1",
        hs_email_subject="Proposal for Acme",
        hs_email_direction="EMAIL",
        hs_timestamp=_iso(2026, 10, 1, 15),
        hubspot_owner_id="77",
    )
    hs.add(
        "emails",
        "e2",
        hs_email_subject="Re: Pricing",
        hs_email_direction="INCOMING_EMAIL",
        hs_timestamp=_iso(2026, 10, 1, 14),
        hubspot_owner_id="77",
    )
    act = rs.DayActivity(
        emails=[
            {
                "subject": "Proposal for Acme",
                "to": ["ann@acmecorp.alt"],
                "at": _dt(2026, 10, 1, 15),
            },
            {
                "subject": "Re: Pricing",
                "to": ["ann@acmecorp.alt"],
                "at": _dt(2026, 10, 1, 16),
            },
        ],
        meetings=[
            {
                "title": "Acme call",
                "with": ["ann@acmecorp.alt"],
                "at": _dt(2026, 10, 1, 18),
            }
        ],
    )
    monkeypatch.setattr(rc, "DAY_ACTIVITY", lambda *_a, **_k: act)
    result = rc.activity_today(hs, _me(hs), day=TODAY)
    titles = [r.title for r in result.rows]
    assert titles.count("Proposal for Acme") == 1  # your logged one is not repeated
    # The client's logged "Re: Pricing" does not hide YOUR reply with that subject.
    assert titles.count("Re: Pricing") == 2
    assert "Acme call" in titles
    assert result.sources == ("hubspot", "calendar", "gmail")
    block = rc.format_for_chat(result, _me(hs))
    assert "plus your own sent email and calendar" in block


# ── Person impersonation stays HubSpot-only ──────────────────────────────────


def test_impersonating_a_rep_never_reads_their_mail_or_calendar(monkeypatch, hs):
    """An admin impersonating a rep resolves to the rep's profile, so the rhythm
    endpoint runs "as" them — it must count only HubSpot and never open their
    inbox. The rep's own request still reads their own sources."""
    from fastapi.testclient import TestClient

    from backend.auth import get_current_user
    from backend.main import app
    from rubik_core.auth import impersonation
    from rubik_core.config import settings
    from rubik_core.database.schemas import UserProfile

    calls = []
    monkeypatch.setattr(rc, "SIGNALS", _signals({}, calls=calls))
    monkeypatch.setattr(
        rc, "DAY_ACTIVITY", lambda *a, **k: calls.append("day") or rs.DayActivity()
    )
    monkeypatch.setattr("rubik_core.retrieval.hubspot_api.HubSpotAPIClient", lambda: hs)
    monkeypatch.setattr(settings, "COCKPIT_ENABLED", True)
    monkeypatch.setattr(settings, "SALES_RHYTHM_ENABLED", True)
    monkeypatch.setattr(
        "backend.routers.sales_rhythm.feature_allows", lambda key, user: True
    )
    monkeypatch.setattr("backend.auth.feature_allows", lambda key, user: True)
    hs.add(
        "deals",
        "1",
        dealname="Rep deal",
        hubspot_owner_id="77",
        closedate=_iso(2026, 10, 20),
        dealstage="presentationscheduled",
        notes_last_contacted=_iso(2026, 9, 1),
    )
    hs.link("deals", "contacts", "1", "c1")
    rep = UserProfile(
        id="ae",
        email="ae@acme.example",
        name="Avery Exec",
        department="Systems",
        roles=["user"],
        created_at="2026-10-01T00:00:00",
        updated_at="2026-10-01T00:00:00",
    )
    app.dependency_overrides[get_current_user] = lambda: rep
    client = TestClient(app)
    try:
        for part in ("morning", "midday", "eod"):
            res = client.get(
                f"/api/sales/rhythm/{part}",
                headers={impersonation.HEADER: "ae@acme.example"},
            )
            assert res.status_code == 200, res.text
        assert calls == []
        # …while the rep's own My Day does read their own sources.
        rc.clear_cache()
        assert client.get("/api/sales/rhythm/midday").status_code == 200
        assert calls and calls[0]["mailbox"] == "ae@acme.example"
    finally:
        app.dependency_overrides.clear()

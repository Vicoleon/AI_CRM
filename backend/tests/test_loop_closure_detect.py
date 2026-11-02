"""Loose Ends — the pure detection and closure rules (``rubik_core/loop_closure/detect.py``).

Every rule the README states is pinned here without Google, Slack, HubSpot or
a model: business-day thresholds on the cockpit clock (a Friday-afternoon email
is not "two days old" on Monday morning), the one-day engagement slack, the log
grace, mutes, internal and automated filtering, "could not look" deciding
nothing, the capped-index refusal, aged-out, and the two lifecycle rules that
live in ``store.apply_candidate``: a dismissed item is never re-flagged, and a
closed unlogged thread re-opens only for a NEW unlogged email.

The clock: ``NOW`` is Thursday 2026-10-08 12:10 UTC, which is 07:10 in Chicago
(CDT, UTC-5) — the scan's real slot, before the 09:00-17:00 business window
opens. So an email received Monday 10:00 Chicago has 23 business hours behind
it (2.9 business days), and one received Wednesday 10:00 has 7 (0.9).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from rubik_core.identifiers import LOOP_CLOSE_LOGGED_FROM_ASSISTANT
from rubik_core.loop_closure import detect, store
from rubik_core.loop_closure.gmail_reader import Message, Thread
from rubik_core.loop_closure.hubspot_index import (
    Company,
    Contact,
    Deal,
    Engagement,
    HubSpotIndex,
    Task,
)

UTC = timezone.utc
NOW = datetime(2026, 10, 8, 12, 10, tzinfo=UTC)  # Thu 07:10 Chicago
TODAY = date(2026, 10, 8)
AE = "rep@acme.example"
CLIENT = "jane@acmecorp.example"

MON_10 = datetime(2026, 10, 5, 15, 0, tzinfo=UTC)  # Mon 10:00 Chicago → 2.9 bd
TUE_16 = datetime(2026, 10, 6, 21, 0, tzinfo=UTC)  # Tue 16:00 Chicago → 1.1 bd
WED_10 = datetime(2026, 10, 7, 15, 0, tzinfo=UTC)  # Wed 10:00 Chicago → 0.9 bd


def _rules(**overrides) -> detect.Rules:
    base = dict(
        owner=AE,
        now=NOW,
        today=TODAY,
        lookback_days=14,
        open_loop_business_days=2,
        log_grace_business_days=1,
        todo_grace_days=3,
        todo_undated_days=5,
        max_age_days=60,
        internal_domains=("acme.example",),
        mutes=(),
    )
    base.update(overrides)
    return detect.Rules(**base)


_N = iter(range(1, 10_000))


def _msg(
    sender: str,
    at: datetime,
    *,
    to=(),
    cc=(),
    subject="Onboarding pricing",
    tid="t1",
    bulk=False,
    name="",
) -> Message:
    return Message(
        id=f"m{next(_N)}",
        thread_id=tid,
        from_email=sender,
        from_name=name,
        to=tuple(to),
        cc=tuple(cc),
        subject=subject,
        at=at,
        snippet="can you send the updated pricing?",
        bulk=bulk,
        names=((CLIENT, "Jane Doe"),),
    )


def _thread(*messages: Message, tid: str = "t1") -> dict[str, Thread]:
    return {tid: Thread(id=tid, messages=sorted(messages, key=lambda m: m.at))}


def _index(engagements: dict | None = None, **overrides) -> HubSpotIndex:
    idx = HubSpotIndex(
        status="ok",
        owners={AE: "o1"},
        contacts={
            CLIENT: Contact(id="c1", email=CLIENT, name="Jane Doe", company_ids=["co1"])
        },
        companies={
            "co1": Company(id="co1", name="Acme Supply", deal_ids=["d1"]),
            "co2": Company(id="co2", name="Beta Bank", deal_ids=["d2"]),
        },
        domain_company={"beta.com": "co2"},
        deals={
            "d1": Deal(
                id="d1", name="Acme Onboarding", owner_id="o1", company_ids=["co1"]
            ),
            "d2": Deal(id="d2", name="Beta QC", owner_id="o1", company_ids=["co2"]),
        },
        engagements=engagements or {},
        owner_open_deals={"o1": ["d1", "d2"]},
    )
    for key, value in overrides.items():
        setattr(idx, key, value)
    return idx


def _eng(at: datetime, etype="calls", eid="e1", incoming=False) -> Engagement:
    return Engagement(at=at, type=etype, id=eid, incoming=incoming)


# ── business days ────────────────────────────────────────────────────────────


def test_business_days_follow_the_cockpit_clock():
    assert detect.business_days_between(MON_10, NOW) == pytest.approx(23 / 8)
    assert detect.business_days_between(WED_10, NOW) == pytest.approx(7 / 8)
    # Friday 16:00 Chicago → Monday 07:10 Chicago is ONE business hour.
    fri = datetime(2026, 10, 2, 21, 0, tzinfo=UTC)
    mon = datetime(2026, 10, 5, 12, 10, tzinfo=UTC)
    assert detect.business_days_between(fri, mon) == pytest.approx(1 / 8)


# ── unlogged: email ──────────────────────────────────────────────────────────


def test_an_unlogged_email_to_a_contact_is_a_loose_end():
    threads = _thread(_msg(AE, MON_10, to=[CLIENT]), _msg(AE, TUE_16, to=[CLIENT]))
    [cand] = detect.unlogged_email_candidates(threads, _index(), _rules())
    assert cand.kind == "unlogged" and cand.source == "gmail"
    assert cand.source_key == "gmail:t1"
    assert cand.opened_at == MON_10  # the EARLIEST unlogged AE email
    assert cand.event_times == (MON_10, TUE_16)
    assert cand.counterparty_label == "Jane Doe"
    assert cand.company_label == "Acme Supply"
    assert cand.hubspot_contact_id == "c1" and cand.hubspot_company_id == "co1"
    assert cand.hubspot_deal_id == "d1"
    assert set(cand.detail["records"]) == {"contact:c1", "company:co1", "deal:d1"}
    # detail is ids and flags only — never the subject or the snippet.
    assert "Onboarding" not in str(cand.detail) and "pricing" not in str(cand.detail)


def test_an_engagement_the_day_before_counts_as_logged():
    """``sent − 1 day``: a call logged the evening before the email covers it."""
    threads = _thread(_msg(AE, MON_10, to=[CLIENT]))
    logged = _index({"company:co1": [_eng(MON_10 - timedelta(hours=12))]})
    assert detect.unlogged_email_candidates(threads, logged, _rules()) == []
    stale = _index({"contact:c1": [_eng(MON_10 - timedelta(days=2))]})
    assert len(detect.unlogged_email_candidates(threads, stale, _rules())) == 1


def test_an_engagement_on_a_deal_counts_as_logged():
    threads = _thread(_msg(AE, MON_10, to=[CLIENT]))
    logged = _index({"deal:d1": [_eng(MON_10 + timedelta(hours=1), "notes")]})
    assert detect.unlogged_email_candidates(threads, logged, _rules()) == []


def test_the_log_grace_gives_the_ae_a_business_day():
    threads = _thread(_msg(AE, WED_10, to=[CLIENT]))
    assert detect.unlogged_email_candidates(threads, _index(), _rules()) == []
    assert (
        len(
            detect.unlogged_email_candidates(
                threads, _index(), _rules(log_grace_business_days=0)
            )
        )
        == 1
    )


@pytest.mark.parametrize(
    "recipient,mutes",
    [
        ("boss@acme.example", ()),  # internal
        ("noreply@acmecorp.example", ()),  # automated
        (CLIENT, (CLIENT,)),  # sender muted
        (CLIENT, ("@acmecorp.example",)),  # domain muted
        ("stranger@nowhere.com", ()),  # not in HubSpot at all
    ],
)
def test_who_never_makes_an_unlogged_item(recipient, mutes):
    threads = _thread(_msg(AE, MON_10, to=[recipient]))
    assert (
        detect.unlogged_email_candidates(threads, _index(), _rules(mutes=mutes)) == []
    )


def test_someone_at_a_client_domain_counts_without_a_contact():
    threads = _thread(_msg(AE, MON_10, to=["new.person@beta.com"]))
    [cand] = detect.unlogged_email_candidates(threads, _index(), _rules())
    assert cand.detail["match"] == "domain"
    assert cand.hubspot_contact_id is None and cand.hubspot_company_id == "co2"
    assert cand.company_label == "Beta Bank"


def test_an_unavailable_hubspot_finds_nothing_unlogged():
    threads = _thread(_msg(AE, MON_10, to=[CLIENT]))
    down = HubSpotIndex.unavailable("HubSpotAPIError")
    assert detect.unlogged_email_candidates(threads, down, _rules()) == []


def test_a_capped_index_refuses_only_where_the_cap_could_hide_evidence():
    threads = _thread(_msg(AE, MON_10, to=[CLIENT]))
    overlapping = _index(capped=[(NOW - timedelta(days=3), NOW)])
    stats = detect.Stats()
    assert detect.unlogged_email_candidates(threads, overlapping, _rules(), stats) == []
    assert stats.partial_skipped == 1
    # A slice that ended before the email could not hide anything after it.
    earlier = _index(capped=[(NOW - timedelta(days=30), NOW - timedelta(days=20))])
    assert len(detect.unlogged_email_candidates(threads, earlier, _rules())) == 1


# ── open loops ───────────────────────────────────────────────────────────────


def test_a_client_waiting_two_business_days_is_an_open_loop():
    threads = _thread(
        _msg(AE, MON_10 - timedelta(days=1), to=[CLIENT]),
        _msg(CLIENT, MON_10, to=[AE], name="Jane Doe"),
    )
    [cand] = detect.open_loop_candidates(threads, _index(), _rules())
    assert cand.kind == "open_loop"
    assert cand.source_key == f"gmail:t1:{threads['t1'].messages[-1].id}"
    assert cand.message_id == threads["t1"].messages[-1].id
    assert cand.needs_judge is True
    assert cand.detail["match"] == "contact"


def test_open_loop_thresholds_are_business_days_not_calendar_days():
    assert (
        detect.open_loop_candidates(
            _thread(_msg(CLIENT, TUE_16, to=[AE])), _index(), _rules()
        )
        == []
    )  # 1.1 business days
    fri = datetime(2026, 10, 2, 21, 0, tzinfo=UTC)
    monday_morning = _rules(now=datetime(2026, 10, 5, 12, 10, tzinfo=UTC))
    assert (
        detect.open_loop_candidates(
            _thread(_msg(CLIENT, fri, to=[AE])), _index(), monday_morning
        )
        == []
    )


@pytest.mark.parametrize(
    "build",
    [
        lambda: _thread(_msg(CLIENT, MON_10, to=["team@acme.example"], cc=[AE])),
        lambda: _thread(_msg(CLIENT, MON_10, to=[AE], bulk=True)),
        lambda: _thread(_msg("notifications@acmecorp.example", MON_10, to=[AE])),
        lambda: _thread(_msg("boss@acme.example", MON_10, to=[AE])),
        lambda: _thread(_msg(CLIENT, MON_10, to=[AE]), _msg(AE, TUE_16, to=[CLIENT])),
    ],
    ids=["only-cc", "bulk", "automated", "internal", "ae-replied"],
)
def test_what_is_never_an_open_loop(build):
    assert detect.open_loop_candidates(build(), _index(), _rules()) == []


def test_a_muted_sender_opens_no_loop():
    threads = _thread(_msg(CLIENT, MON_10, to=[AE]))
    assert (
        detect.open_loop_candidates(
            threads, _index(), _rules(mutes=("@acmecorp.example",))
        )
        == []
    )


def test_a_logged_follow_up_answers_the_loop_but_the_clients_own_email_does_not():
    threads = _thread(_msg(CLIENT, MON_10, to=[AE]))
    called = _index({"contact:c1": [_eng(MON_10 + timedelta(hours=3), "calls")]})
    assert detect.open_loop_candidates(threads, called, _rules()) == []
    logged_inbound = _index(
        {"contact:c1": [_eng(MON_10 + timedelta(minutes=2), "emails", incoming=True)]}
    )
    assert len(detect.open_loop_candidates(threads, logged_inbound, _rules())) == 1


def test_an_unknown_sender_still_reaches_the_judge():
    threads = _thread(_msg("prospect@newco.com", MON_10, to=[AE]))
    [cand] = detect.open_loop_candidates(threads, _index(), _rules())
    assert cand.detail["match"] == "none"


# ── unlogged: Slack ──────────────────────────────────────────────────────────


def _slack(text: str, at: datetime, ts_suffix="1") -> SimpleNamespace:
    return SimpleNamespace(
        channel_id="C1",
        channel_name="sales",
        ts=f"{at.timestamp():.0f}.00{ts_suffix}",
        text=text,
        permalink="https://slack.example/p1",
        app_link=None,
    )


def test_normalize_company_strips_legal_suffixes_and_short_names():
    assert detect.normalize_company("Acme Supply, LLC") == "acme supply"
    assert detect.normalize_company("The Beta Works, Ltd.") == "beta works"
    assert detect.normalize_company("IBM Inc.") == ""  # under 4 characters


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Globex GmbH", "globex"),
        ("Initech S.A.", "initech"),
        ("Umbrella Holdings B.V.", "umbrella holdings"),
        ("Acme Widgets Pty Ltd", "acme widgets"),
    ],
)
def test_normalize_company_strips_generic_legal_entity_suffixes(name, expected):
    assert detect.normalize_company(name) == expected


@pytest.mark.parametrize(
    "name", ["Beta Bank N.A.", "Beta Savings FSB", "Beta Federal Credit Union"]
)
def test_normalize_company_keeps_industry_words(name):
    """Only legal-entity forms are noise. A word that says what a company
    does (one industry's charter letters included) stays part of its name."""
    assert detect.normalize_company(name) == " ".join(
        name.lower().replace(".", "").split()
    )


def test_a_slack_message_naming_a_deal_company_is_a_candidate():
    idx = _index()
    msg = _slack("Just got off a call with Acme Supply about the rollout", MON_10)
    [cand] = detect.slack_candidates([msg], idx.ae_companies("o1"), idx, _rules())
    assert cand.source == "slack" and cand.kind == "unlogged"
    assert cand.source_key == f"slack:C1:{msg.ts}"
    assert cand.hubspot_company_id == "co1" and cand.company_label == "Acme Supply"
    assert cand.needs_judge is True
    # Logged on the deal since: nothing to flag.
    logged = _index({"deal:d1": [_eng(MON_10 + timedelta(hours=2))]})
    assert (
        detect.slack_candidates([msg], logged.ae_companies("o1"), logged, _rules())
        == []
    )


def test_a_slack_message_naming_no_deal_company_is_ignored():
    idx = _index()
    msg = _slack("lunch with the Acme folks next week?", MON_10)
    assert detect.slack_candidates([msg], idx.ae_companies("o1"), idx, _rules()) == []


# ── todo ─────────────────────────────────────────────────────────────────────


def _promise(**overrides) -> SimpleNamespace:
    base = dict(
        id="p1",
        title="Send Jane the revised SOW",
        source="heard_email",
        status="suggested",
        confidence=0.9,
        due_local=TODAY - timedelta(days=3),
        timezone="America/Chicago",
        counterparty_id=CLIENT,
        counterparty_label="Jane Doe",
        source_ref={"at": "2026-09-30T15:00:00", "link": "https://mail.google.com/x"},
        created_at=datetime(2026, 9, 30, 15, 0),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_the_heard_vocabulary_matches_the_promise_ledger():
    from rubik_core.promises import store as promise_store

    assert detect.HEARD_SOURCES == promise_store.HEARD_SOURCES
    assert detect.UNKEPT_STATUSES == promise_store.UNKEPT_CANDIDATE_STATUSES


def test_a_promise_three_days_past_its_date_is_a_todo():
    [cand] = detect.promise_candidates([_promise()], _rules())
    assert cand.kind == "todo" and cand.source == "promise"
    assert cand.source_key == "promise:p1"
    assert cand.counterparty_email == CLIENT
    assert cand.link == "https://mail.google.com/x"


@pytest.mark.parametrize(
    "overrides",
    [
        {"due_local": TODAY - timedelta(days=2)},  # inside the grace
        {"confidence": 0.7},  # under the 0.75 bar
        {"source": "chat"},  # typed in, not heard
        {"status": "kept"},
        {"due_local": TODAY - timedelta(days=90)},  # older than the max age
    ],
)
def test_promises_that_are_not_todos(overrides):
    assert detect.promise_candidates([_promise(**overrides)], _rules()) == []


def test_an_undated_promise_waits_five_days():
    said_6 = _promise(
        due_local=None, source_ref={"at": (NOW - timedelta(days=6)).isoformat()}
    )
    said_4 = _promise(
        due_local=None, source_ref={"at": (NOW - timedelta(days=4)).isoformat()}
    )
    assert len(detect.promise_candidates([said_6], _rules())) == 1
    assert detect.promise_candidates([said_4], _rules()) == []


def test_an_overdue_hubspot_task_is_a_todo():
    idx = _index()
    old = Task(
        id="t9",
        owner_id="o1",
        subject="Call Jane",
        due=NOW - timedelta(days=4),
        company_ids=["co1"],
    )
    fresh = Task(id="t8", owner_id="o1", subject="x", due=NOW - timedelta(days=1))
    [cand] = detect.task_candidates([old, fresh], idx, _rules())
    assert cand.source_key == "hstask:t9" and cand.company_label == "Acme Supply"
    assert detect.task_candidates([old], HubSpotIndex.unavailable("x"), _rules()) == []


def test_a_task_overdue_for_more_than_the_task_lookback_is_backlog_not_a_loose_end():
    idx = _index()
    recent = Task(id="t1", owner_id="o1", subject="a", due=NOW - timedelta(days=29))
    backlog = Task(id="t2", owner_id="o1", subject="b", due=NOW - timedelta(days=31))
    keys = {
        c.source_key for c in detect.task_candidates([recent, backlog], idx, _rules())
    }
    assert keys == {"hstask:t1"}
    # The lookback never reaches past MAX_AGE_DAYS.
    rules = _rules(task_lookback_days=90)
    assert rules.oldest_task == rules.oldest_allowed


# ── closure ──────────────────────────────────────────────────────────────────


@dataclass
class _Item:
    kind: str
    source: str
    source_key: str
    opened_at: datetime
    hubspot_contact_id: str | None = None
    hubspot_company_id: str | None = None
    hubspot_deal_id: str | None = None
    thread_id: str | None = None
    message_id: str | None = None
    detail: dict = field(default_factory=dict)
    counterparty_email: str | None = None


def _ctx(
    index=None, *, gmail_ok=True, threads=None, promises=None
) -> detect.ClosureContext:
    return detect.ClosureContext(
        now=NOW,
        max_age_days=60,
        index=index or _index(),
        owner=AE,
        gmail_ok=gmail_ok,
        threads=threads or {},
        promises=promises or {},
        internal_domains=("acme.example",),
    )


def _unlogged(opened=MON_10) -> _Item:
    return _Item(
        "unlogged",
        "gmail",
        "gmail:t1",
        opened.replace(tzinfo=None),
        hubspot_contact_id="c1",
        detail={"records": ["contact:c1", "company:co1"]},
    )


def test_an_unlogged_item_closes_on_hubspot_evidence_with_ids_only():
    idx = _index({"company:co1": [_eng(TUE_16, "notes", "n7")]})
    reason, evidence = detect.closure_for(_unlogged(), _ctx(idx))
    assert reason == "logged_in_hubspot"
    assert evidence == {"type": "notes", "id": "n7", "at": "2026-10-06T21:00:00"}


def test_an_unreadable_hubspot_closes_nothing():
    assert detect.closure_for(_unlogged(), _ctx(HubSpotIndex.unavailable("x"))) is None


def test_an_open_loop_closes_when_the_ae_replies():
    inbound = _msg(CLIENT, MON_10, to=[AE])
    reply = _msg(AE, TUE_16, to=[CLIENT])
    item = _Item(
        "open_loop",
        "gmail",
        f"gmail:t1:{inbound.id}",
        MON_10.replace(tzinfo=None),
        hubspot_contact_id="c1",
        thread_id="t1",
        message_id=inbound.id,
    )
    threads = _thread(inbound, reply)
    reason, evidence = detect.closure_for(item, _ctx(threads=threads))
    assert reason == "replied" and evidence["id"] == reply.id
    # The same reply in an unreadable mailbox decides nothing.
    assert detect.closure_for(item, _ctx(gmail_ok=False, threads=threads)) is None


def test_an_open_loop_closes_on_a_logged_follow_up_but_not_the_inbound_email():
    item = _Item(
        "open_loop",
        "gmail",
        "gmail:t1:m1",
        MON_10.replace(tzinfo=None),
        hubspot_contact_id="c1",
        thread_id="t1",
        message_id="m1",
    )
    inbound_logged = _index({"contact:c1": [_eng(MON_10, "emails", incoming=True)]})
    assert detect.closure_for(item, _ctx(inbound_logged, gmail_ok=False)) is None
    called = _index({"contact:c1": [_eng(TUE_16, "calls", "k1")]})
    reason, _ = detect.closure_for(item, _ctx(called, gmail_ok=False))
    assert reason == "logged_follow_up"


@pytest.mark.parametrize(
    "status,reason",
    [
        ("kept", "promise_kept"),
        ("evidence_found", "promise_kept"),
        ("dropped", "promise_withdrawn"),
        ("handed_off", "promise_withdrawn"),
        ("open", None),
    ],
)
def test_promise_closures_follow_the_ledger(status, reason):
    item = _Item("todo", "promise", "promise:p1", MON_10.replace(tzinfo=None))
    promise = SimpleNamespace(
        status=status, kept_at=None, dropped_at=None, updated_at=None
    )
    found = detect.closure_for(item, _ctx(promises={"p1": promise}))
    assert (found[0] if found else None) == reason


def test_hubspot_task_closures():
    item = _Item("todo", "hubspot_task", "hstask:t9", MON_10.replace(tzinfo=None))
    done = _index(task_states={"t9": "completed"})
    gone = _index(task_states={"t9": "missing"})
    assert detect.closure_for(item, _ctx(done))[0] == "task_completed"
    assert detect.closure_for(item, _ctx(gone))[0] == "task_gone"
    assert detect.closure_for(item, _ctx(_index(task_states={"t9": "open"}))) is None


def test_an_item_older_than_the_max_age_ages_out_unless_it_was_fixed():
    old = _unlogged(opened=NOW - timedelta(days=61))
    reason, _ = detect.closure_for(old, _ctx())
    assert reason == "aged_out"
    fixed = _index({"contact:c1": [_eng(NOW - timedelta(days=30))]})
    assert detect.closure_for(old, _ctx(fixed))[0] == "logged_in_hubspot"


# ── lifecycle rules that live in the store ──────────────────────────────────


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False)()
    yield session
    session.close()


def _cand(events=(MON_10,)) -> detect.Candidate:
    return detect.Candidate(
        kind="unlogged",
        source="gmail",
        source_key="gmail:t1",
        title="Onboarding pricing",
        opened_at=events[0],
        counterparty_email=CLIENT,
        hubspot_contact_id="c1",
        thread_id="t1",
        event_times=tuple(events),
    )


def test_a_dismissed_item_is_never_reflagged(db):
    outcome, item = store.apply_candidate(db, AE, _cand(), None, now=NOW)
    assert outcome == "new"
    db.flush()
    store.dismiss(db, item, reason="not_a_client", actor=AE, now=NOW)
    later = _cand(events=(MON_10, NOW - timedelta(hours=1)))
    outcome, same = store.apply_candidate(
        db, AE, later, item, now=NOW, reopen_after=NOW - timedelta(days=1)
    )
    assert outcome == "dismissed" and same.status == "dismissed"


def test_a_closed_thread_reopens_only_for_a_new_unlogged_email(db):
    _, item = store.apply_candidate(db, AE, _cand(), None, now=NOW - timedelta(days=2))
    db.flush()
    store.close_item(
        item,
        reason=LOOP_CLOSE_LOGGED_FROM_ASSISTANT,
        evidence={"type": "notes", "id": "n1", "at": "x"},
        now=NOW - timedelta(days=1),
    )
    # Same old email seen again: stays closed.
    outcome, _ = store.apply_candidate(
        db, AE, _cand(), item, now=NOW, reopen_after=detect.reopen_after(item)
    )
    assert outcome == "closed" and item.status == "closed"
    # A new unlogged AE email after closed_at: re-opens, counted, new opened_at.
    newer = NOW - timedelta(hours=2)
    outcome, _ = store.apply_candidate(
        db,
        AE,
        _cand(events=(MON_10, newer)),
        item,
        now=NOW,
        reopen_after=detect.reopen_after(item),
    )
    assert outcome == "reopened"
    assert item.status == "open" and item.reopened_count == 1
    assert item.opened_at == newer.replace(tzinfo=None)
    assert item.closed_at is None and item.close_reason is None


def test_reopen_after_anchors_on_the_engagement_for_hubspot_closures():
    item = SimpleNamespace(
        close_reason="logged_in_hubspot",
        close_evidence={"type": "calls", "id": "1", "at": "2026-10-02T15:00:00"},
        closed_at=datetime(2026, 10, 5, 12, 10),
    )
    assert detect.reopen_after(item) == datetime(2026, 10, 2, 15, 0, tzinfo=UTC)
    item.close_reason = "aged_out"
    assert detect.reopen_after(item) == datetime(2026, 10, 5, 12, 10, tzinfo=UTC)


def _open_item(db, key, counterparty, *, kind="open_loop", owner=AE):
    from rubik_core.database.loop_closure import LoopItem

    item = LoopItem(
        owner_email=owner,
        kind=kind,
        source="gmail",
        source_key=key,
        status="open",
        title="x",
        counterparty_email=counterparty,
        hubspot_contact_id="c1",
        opened_at=MON_10.replace(tzinfo=None),
        first_flagged_at=NOW.replace(tzinfo=None),
        last_checked_at=NOW.replace(tzinfo=None),
    )
    db.add(item)
    db.flush()
    return item


def test_a_domain_mute_dismisses_the_same_false_flag_everywhere(db):
    from rubik_core.database.loop_closure import LoopMute

    first = _open_item(db, "gmail:t1:m1", "news@vendor.com")
    sibling = _open_item(db, "gmail:t2:m2", "sales@vendor.com", kind="unlogged")
    other = _open_item(db, "gmail:t3:m3", CLIENT)
    store.dismiss(
        db, first, reason="automated", actor="Rep@ACME.example", mute="domain"
    )
    assert first.status == sibling.status == "dismissed"
    assert sibling.dismiss_reason == "automated" and first.dismissed_by == AE
    assert other.status == "open"
    [mute] = db.query(LoopMute).all()
    assert mute.pattern == "@vendor.com" and mute.owner_email == AE


def test_dismiss_refuses_what_it_cannot_honour(db):
    item = _open_item(db, "gmail:t1:m1", "someone@gmail.com")
    with pytest.raises(ValueError, match="personal email provider"):
        store.dismiss(db, item, reason="not_a_client", actor=AE, mute="domain")
    with pytest.raises(ValueError):
        store.dismiss(db, item, reason="because", actor=AE)
    with pytest.raises(ValueError):
        store.dismiss(db, item, reason="other", actor=AE, mute="everyone")
    store.dismiss(db, item, reason="other", actor=AE, mute="sender")
    with pytest.raises(ValueError, match="already closed"):
        store.dismiss(db, item, reason="other", actor=AE)


def test_mutes_and_items_are_the_owners_alone(db):
    from rubik_core.database.loop_closure import LoopMute

    item = _open_item(db, "gmail:t1:m1", CLIENT)
    assert store.get_owned(db, AE, item.id) is item
    assert store.get_owned(db, "other@acme.example", item.id) is None
    mute = LoopMute(owner_email=AE, pattern="@acmecorp.example")
    db.add(mute)
    db.flush()
    assert store.remove_mute(db, "other@acme.example", mute.id) is False
    assert store.remove_mute(db, AE, mute.id) is True


def test_logging_from_the_app_closes_with_the_email_ids_as_evidence(db, monkeypatch):
    from rubik_core.tenancy.hubspot_config import HubSpotConnectorConfig

    monkeypatch.setattr(
        "rubik_core.sales.rhythm_checks.get_hubspot_config",
        lambda: HubSpotConnectorConfig(portal_id="9100001"),
    )
    item = _open_item(db, "gmail:t1", CLIENT, kind="unlogged")
    store.mark_logged_from_assistant(
        db, item, email_ids=["e9", "e10"], message_ids=["m1", "m2"], actor=AE, now=NOW
    )
    assert (
        item.status == "closed"
        and item.close_reason == LOOP_CLOSE_LOGGED_FROM_ASSISTANT
    )
    # Ids and a time only — never a subject or a body.
    assert item.close_evidence == {
        "type": "emails",
        "ids": ["e9", "e10"],
        "message_ids": ["m1", "m2"],
        "at": "2026-10-08T12:10:00",
        "hubspot_url": "https://app.hubspot.com/contacts/9100001/contact/c1",
    }
    with pytest.raises(ValueError):
        store.mark_logged_from_assistant(
            db, item, email_ids=["e11"], message_ids=["m3"], actor=AE
        )
    loop = _open_item(db, "gmail:t2:m2", CLIENT)
    with pytest.raises(ValueError):
        store.mark_logged_from_assistant(
            db, loop, email_ids=["e12"], message_ids=["m4"], actor=AE
        )


# ── Review fixes ─────────────────────────────────────────────────────────────


def _draft(msg: Message) -> Message:
    from dataclasses import replace

    return replace(msg, labels=("DRAFT",))


def test_a_draft_is_never_a_reply_or_a_sent_email():
    inbound = _msg(CLIENT, MON_10, to=[AE])
    draft_reply = _draft(_msg(AE, TUE_16, to=[CLIENT]))
    # An unsent reply does not answer the client: the loop is still a loop.
    [cand] = detect.open_loop_candidates(
        _thread(inbound, draft_reply), _index(), _rules()
    )
    assert cand.message_id == inbound.id
    # …and does not close one that is already open.
    item = _Item(
        "open_loop",
        "gmail",
        f"gmail:t1:{inbound.id}",
        MON_10.replace(tzinfo=None),
        thread_id="t1",
        message_id=inbound.id,
    )
    assert detect.closure_for(item, _ctx(threads=_thread(inbound, draft_reply))) is None
    # An unsent email to a client is not an unlogged SENT email.
    unsent = _thread(_draft(_msg(AE, MON_10, to=[CLIENT])))
    assert detect.unlogged_email_candidates(unsent, _index(), _rules()) == []


def test_the_gmail_reader_drops_drafts_trash_and_spam():
    from rubik_core.loop_closure.gmail_reader import parse_thread

    def raw(mid, labels):
        return {
            "id": mid,
            "internalDate": str(int(MON_10.timestamp() * 1000)),
            "labelIds": list(labels),
            "payload": {"headers": [{"name": "From", "value": AE}]},
        }

    thread = parse_thread(
        {
            "id": "t1",
            "messages": [
                raw("sent", ["SENT"]),
                raw("draft", ["DRAFT"]),
                raw("trash", ["TRASH"]),
                raw("spam", ["SPAM"]),
            ],
        }
    )
    assert [m.id for m in thread.messages] == ["sent"]
    assert parse_thread({"id": "t2", "messages": [raw("d", ["DRAFT"])]}) is None


def test_a_colleague_answering_on_the_thread_closes_the_loop():
    inbound = _msg(CLIENT, MON_10, to=[AE], cc=["boss@acme.example"])
    boss = _msg("boss@acme.example", TUE_16, to=[CLIENT], cc=[AE])
    item = _Item(
        "open_loop",
        "gmail",
        f"gmail:t1:{inbound.id}",
        MON_10.replace(tzinfo=None),
        thread_id="t1",
        message_id=inbound.id,
    )
    reason, evidence = detect.closure_for(item, _ctx(threads=_thread(inbound, boss)))
    assert reason == "replied"
    assert evidence == {
        "type": "gmail_message_internal",
        "id": boss.id,
        "at": "2026-10-06T21:00:00",
    }
    # A robot on the company domain is not a colleague answering.
    robot = _msg("noreply@acme.example", TUE_16, to=[CLIENT])
    assert detect.closure_for(item, _ctx(threads=_thread(inbound, robot))) is None
    # An internal-only note on the thread answers nobody.
    item.counterparty_email = CLIENT
    aside = _msg("boss@acme.example", TUE_16, to=[AE])
    assert detect.closure_for(item, _ctx(threads=_thread(inbound, aside))) is None
    assert detect.closure_for(item, _ctx(threads=_thread(inbound, boss)))[0] == (
        "replied"
    )


def test_a_promise_snoozed_or_re_dated_closes_as_rescheduled():
    item = _Item("todo", "promise", "promise:p1", MON_10.replace(tzinfo=None))
    snoozed = _promise(due_local=TODAY + timedelta(days=5))
    reason, evidence = detect.closure_for(item, _ctx(promises={"p1": snoozed}))
    assert reason == "rescheduled"
    assert evidence["due"] == (TODAY + timedelta(days=5)).isoformat()
    assert set(evidence) == {"type", "id", "at", "due"}  # ids and dates only
    # Still past its date: stays open.
    assert detect.closure_for(item, _ctx(promises={"p1": _promise()})) is None
    # An undated promise with no time it was said decides nothing.
    blind = SimpleNamespace(status="open", due_local=None, source_ref=None)
    assert detect.closure_for(item, _ctx(promises={"p1": blind})) is None


def test_a_task_moved_later_or_reassigned_leaves_the_list():
    item = _Item("todo", "hubspot_task", "hstask:t9", MON_10.replace(tzinfo=None))
    moved = _index(task_states={"t9": "open"}, task_due={"t9": NOW + timedelta(days=2)})
    assert detect.closure_for(item, _ctx(moved))[0] == "rescheduled"
    late = _index(task_states={"t9": "open"}, task_due={"t9": NOW - timedelta(days=4)})
    assert detect.closure_for(item, _ctx(late)) is None
    # Due date not returned: undecided, never "rescheduled".
    assert detect.closure_for(item, _ctx(_index(task_states={"t9": "open"}))) is None

    handed = _index(task_states={"t9": "open"}, task_owner={"t9": "o2"})
    ctx = _ctx(handed)
    ctx.owner_id = "o1"
    reason, evidence = detect.closure_for(item, ctx)
    assert reason == "task_gone" and evidence["reassigned"] is True
    # The AE's own owner id unknown: a different owner decides nothing.
    assert detect.closure_for(item, _ctx(handed)) is None


def test_the_shipped_todo_grace_is_five_days():
    """A to-do is a loose end once it is 5 days overdue, not 3."""
    from rubik_core.config import Settings

    assert Settings.model_fields["LOOP_CLOSURE_TODO_GRACE_DAYS"].default == 5
    assert detect.Rules(owner=AE, now=NOW, today=TODAY).todo_grace_days == 5
    rules = _rules(todo_grace_days=5)
    four = _promise(due_local=TODAY - timedelta(days=4))
    five = _promise(id="p2", due_local=TODAY - timedelta(days=5))
    found = detect.promise_candidates([four, five], rules)
    assert [c.source_key for c in found] == ["promise:p2"]
    tasks = [
        Task(id="t4", owner_id="o1", subject="x", due=NOW - timedelta(days=4)),
        Task(id="t5", owner_id="o1", subject="x", due=NOW - timedelta(days=5)),
    ]
    found = detect.task_candidates(tasks, _index(), rules)
    assert [c.source_key for c in found] == ["hstask:t5"]


def test_raising_the_grace_reschedules_nothing():
    """A to-do flagged 3 days overdue under the old rule is not "rescheduled"
    when the grace goes to 5: its date never moved. It stays open (the page
    leaves it out until it is 5 days overdue), so it never returns "Reopened"."""
    promise = _promise(due_local=TODAY - timedelta(days=3))
    flagged = detect.promise_anchor(promise).replace(tzinfo=None)
    item = _Item("todo", "promise", "promise:p1", flagged)
    assert detect.closure_for(item, _ctx(promises={"p1": promise})) is None
    # Snoozed to a later date: that is a reschedule.
    snoozed = _promise(due_local=TODAY + timedelta(days=1))
    assert detect.closure_for(item, _ctx(promises={"p1": snoozed}))[0] == (
        "rescheduled"
    )

    due = NOW - timedelta(days=3)
    task = _Item("todo", "hubspot_task", "hstask:t9", due.replace(tzinfo=None))
    same = _index(task_states={"t9": "open"}, task_due={"t9": due})
    assert detect.closure_for(task, _ctx(same)) is None
    # Moved EARLIER is more overdue, never "rescheduled".
    earlier = _index(
        task_states={"t9": "open"}, task_due={"t9": due - timedelta(days=1)}
    )
    assert detect.closure_for(task, _ctx(earlier)) is None


def test_only_a_hubspot_contact_name_is_kept_for_the_team_view():
    [contact] = detect.unlogged_email_candidates(
        _thread(_msg(AE, MON_10, to=[CLIENT])), _index(), _rules()
    )
    assert contact.detail["crm_contact_name"] == "Jane Doe"
    [domain] = detect.unlogged_email_candidates(
        _thread(_msg(AE, MON_10, to=["new.person@beta.com"])), _index(), _rules()
    )
    assert "crm_contact_name" not in domain.detail
    [stranger] = detect.open_loop_candidates(
        _thread(_msg("prospect@newco.com", MON_10, to=[AE], name="Pat Prospect")),
        _index(),
        _rules(),
    )
    assert stranger.counterparty_label == "Pat Prospect"  # the AE's own view
    assert "crm_contact_name" not in stranger.detail  # never the team's


def _two_sessions(tmp_path):
    from rubik_core.database.models import Base

    engine = create_engine(
        f"sqlite:///{tmp_path / 'lc.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False)
    return Session


def test_the_scan_never_overwrites_a_dismissal_made_while_it_was_reading(tmp_path):
    from rubik_core.database.loop_closure import LoopItem

    Session = _two_sessions(tmp_path)
    scan_db, ui_db = Session(), Session()
    item_id = _open_item(scan_db, "gmail:t1:m1", CLIENT).id
    scan_db.commit()
    stale = scan_db.get(LoopItem, item_id)
    assert stale.status == "open"

    # Meanwhile the AE presses "Not a real miss" in the page.
    store.dismiss(
        ui_db, ui_db.get(LoopItem, item_id), reason="handled_elsewhere", actor=AE
    )
    ui_db.commit()

    closed = store.close_if_open(
        scan_db,
        stale,
        reason="aged_out",
        evidence={"type": "age", "at": "x"},
        now=NOW,
    )
    assert closed is False and stale.status == "dismissed"
    scan_db.commit()
    assert Session().get(LoopItem, item_id).status == "dismissed"

    # A row still open closes as before.
    other = _open_item(scan_db, "gmail:t2:m2", CLIENT)
    scan_db.commit()
    assert store.close_if_open(
        scan_db, other, reason="replied", evidence={"type": "x"}, now=NOW
    )
    scan_db.commit()
    fresh = Session().get(LoopItem, other.id)
    assert fresh.status == "closed" and fresh.close_reason == "replied"


def test_a_locked_read_sees_the_log_another_request_just_wrote(tmp_path):
    from rubik_core.database.loop_closure import LoopItem

    Session = _two_sessions(tmp_path)
    first, second = Session(), Session()
    item_id = _open_item(first, "gmail:t1", CLIENT, kind="unlogged").id
    first.commit()
    seen = store.get_owned(second, AE, item_id)
    assert seen.status == "open"  # the second click's first look

    store.mark_logged_from_assistant(
        first,
        first.get(LoopItem, item_id),
        email_ids=["e1"],
        message_ids=["m1"],
        actor=AE,
        now=NOW,
    )
    first.commit()

    locked = store.get_owned(second, AE, item_id, for_update=True)
    assert locked is seen  # same row object, re-read under the lock
    assert (
        locked.status == "closed"
        and locked.close_reason == LOOP_CLOSE_LOGGED_FROM_ASSISTANT
    )
    assert locked.close_evidence["ids"] == ["e1"]

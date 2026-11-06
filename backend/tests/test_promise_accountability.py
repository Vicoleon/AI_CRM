"""Promise accountability — "you said you would, and there is no sign of it".

Covers the listener's guard rails (their own words, quoted verbatim, above the
confidence bar, read once), the evidence judge's guard rails (no "kept" on
invented or stale evidence), the ledger helpers, the promises channel's
``promise_unkept`` signals, the card re-keying, and the card write-back.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from rubik_core.config import settings
from rubik_core.features.policy import clear_cache

BLAIR = "blair@acme.example"
DAKOTA = "dakota@client.com"


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from rubik_core.database.models import Base, User

    assert "promise_scans" in Base.metadata.tables
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr("rubik_core.database.db.SessionLocal", Session)
    session = Session()
    session.add(User(email=BLAIR, id=BLAIR, name="Blair Ellison", department="Systems"))
    session.commit()
    clear_cache()
    try:
        yield session
    finally:
        clear_cache()
        session.close()
        engine.dispose()


NOW = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


def _src(text: str, *, people=None, said=None, key="transcript:d1"):
    from rubik_core.promises.heard import HeardSource

    return HeardSource(
        key=key,
        source="heard_transcript",
        entity_kind="transcript",
        entity_id=key.split(":", 1)[1],
        title="Acme renewal",
        said_at=said or datetime(2026, 9, 24, 15, 0, tzinfo=timezone.utc),
        text=text,
        link="https://drive.google.com/file/d/x/view",
        where="the meeting “Acme renewal”",
        people=people or {DAKOTA: "Dakota Client"},
        object_kind="transcript",
        object_id=key.split(":", 1)[1],
    )


TRANSCRIPT = (
    "Dakota Client: Can we get the revised SOW?\n"
    "Blair Ellison: Sure, I'll send you the revised SOW by Friday.\n"
    "Dakota Client: I'll loop in legal.\n"
)


# ── The listener ─────────────────────────────────────────────────────────────


def test_a_promise_needs_its_words_verbatim_in_the_source():
    from rubik_core.promises.heard import parse_promises

    src = _src(TRANSCRIPT)
    payload = {
        "promises": [
            {
                "quote": "I'll send you the revised SOW by Friday.",
                "title": "Send Dakota the revised SOW",
                "counterparty_email": DAKOTA,
                "due": "2026-09-26",
                "confidence": 0.92,
            },
            {  # invented words — dropped
                "quote": "I will also redo the pricing deck",
                "title": "Redo pricing deck",
                "confidence": 0.95,
            },
            {  # real words, too unsure — dropped
                "quote": "I'll loop in legal.",
                "title": "Loop in legal",
                "confidence": 0.4,
            },
        ]
    }
    out = parse_promises(payload, [src])
    assert [p.title for p in out] == ["Send Dakota the revised SOW"]
    assert out[0].due == date(2026, 9, 26)
    assert out[0].counterparty_email == DAKOTA
    assert out[0].counterparty_label == "Dakota Client"


def test_an_email_the_model_made_up_is_not_a_counterparty():
    from rubik_core.promises.heard import parse_promises

    out = parse_promises(
        {
            "promises": [
                {
                    "quote": "I'll send you the revised SOW by Friday",
                    "title": "Send the SOW",
                    "counterparty_email": "ceo@elsewhere.com",
                    "confidence": 0.9,
                }
            ]
        },
        [_src(TRANSCRIPT)],
    )
    assert out[0].counterparty_email is None


def test_a_due_date_before_the_promise_is_a_misread():
    from rubik_core.promises.heard import parse_promises

    out = parse_promises(
        {
            "promises": [
                {
                    "quote": "I'll send you the revised SOW by Friday",
                    "title": "Send the SOW",
                    "due": "2026-09-01",
                    "confidence": 0.9,
                }
            ]
        },
        [_src(TRANSCRIPT)],
    )
    assert out[0].due is None


def test_batched_messages_must_name_their_message():
    from rubik_core.promises.heard import parse_promises

    a = _src("I'll get you the numbers tomorrow", key="gmail:a")
    b = _src("Thanks, talk soon", key="gmail:b")
    out = parse_promises(
        {
            "promises": [
                {
                    "message": "M1",
                    "quote": "I'll get you the numbers tomorrow",
                    "title": "Send the numbers",
                    "confidence": 0.9,
                },
                {
                    "quote": "I'll get you the numbers tomorrow",  # untagged → dropped
                    "title": "dup",
                    "confidence": 0.9,
                },
                {
                    "message": "M7",
                    "quote": "x" * 20,
                    "title": "no such message",
                    "confidence": 0.9,
                },
            ]
        },
        [a, b],
    )
    assert [(p.source.key, p.title) for p in out] == [("gmail:a", "Send the numbers")]


class _FakeTranscripts:
    def __init__(self, text):
        self.text = text
        self.reads = 0

    def list_transcripts_for_signals(self, *, since):
        return [
            SimpleNamespace(
                doc_id="d1",
                meeting_id="1AbCdEfGhIjKlMnOpQrStUvWxYz012345",
                owner_email=BLAIR,
                original_filename="Acme renewal",
                source_path="gs://x/d1.txt",
                created_at=datetime(2026, 9, 24, 15, 0),
                participants=[BLAIR, DAKOTA],
                is_owner=True,
            )
        ]

    def read_transcript_text(self, rec, *, max_bytes):
        self.reads += 1
        return self.text * 3


def _hear(db, calls, svc):
    from rubik_core.promises.heard import hear

    def invoke(prompt):
        calls.append(prompt)
        return {
            "promises": [
                {
                    "quote": "I'll send you the revised SOW by Friday.",
                    "title": "Send Dakota the revised SOW",
                    "counterparty_email": DAKOTA,
                    "due": "2026-09-26",
                    "confidence": 0.9,
                }
            ]
        }

    return hear(
        BLAIR,
        reader_name="Blair Ellison",
        db=db,
        invoke=invoke,
        deadline=lambda: False,
        sent_threads=None,
        slack_messages=None,
        now=NOW,
        transcript_service=svc,
    )


def test_a_meeting_is_read_once_and_its_promise_lands_as_a_suggestion(db):
    from rubik_core.database.promises import Promise, PromiseScan

    calls: list[str] = []
    svc = _FakeTranscripts(TRANSCRIPT)
    stats = _hear(db, calls, svc)
    assert stats["written"] == 1 and len(calls) == 1
    row = db.query(Promise).one()
    assert row.status == "suggested" and row.source == "heard_transcript"
    assert row.counterparty_id == DAKOTA and row.due_local == date(2026, 9, 26)
    assert row.source_ref["quote"].startswith("I'll send you")
    assert row.source_ref["link"].startswith("https://drive.google.com/file/d/")
    assert db.query(PromiseScan).one().found == 1

    # Second night: nothing new to read, no model call, no duplicate.
    again = _hear(db, calls, svc)
    assert len(calls) == 1 and again["written"] == 0
    assert db.query(Promise).count() == 1


@pytest.mark.parametrize("lookup_fails", [False, True])
def test_hear_skips_l10_when_eos_off_but_keeps_transcript_lane(
    db, monkeypatch, lookup_fails
):
    from rubik_core.control import repository
    from rubik_core.promises import heard

    if lookup_fails:
        monkeypatch.setattr(
            repository,
            "module_enabled",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("control down")),
        )
    else:
        monkeypatch.setattr(repository, "module_enabled", lambda *_a, **_k: False)
    monkeypatch.setattr(
        heard,
        "gather_l10_sessions",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("EOS table read")),
    )
    calls = []
    stats = _hear(db, calls, _FakeTranscripts(TRANSCRIPT))
    assert stats["written"] == 1
    assert len(calls) == 1


def test_the_prompt_asks_for_the_readers_promises_only():
    from rubik_core.promises.heard import build_transcript_prompt

    prompt = build_transcript_prompt(
        _src(TRANSCRIPT), reader_name="Blair Ellison", reader_email=BLAIR
    )
    assert "Blair Ellison" in prompt and "VERBATIM" in prompt
    assert "promise made TO the reader" in prompt


# ── The ledger ───────────────────────────────────────────────────────────────


def _heard_row(
    db,
    *,
    due=None,
    said=datetime(2026, 9, 24, 15, 0),
    title="Send Dakota the revised SOW",
):
    from rubik_core.promises import store

    row = store.create_promise(
        db,
        owner_email=BLAIR,
        title=title,
        source="heard_transcript",
        actor=store.SYSTEM_ACTOR,
        status="suggested",
        due_local=due,
        counterparty_kind="person",
        counterparty_id=DAKOTA,
        counterparty_label="Dakota Client",
        confidence=0.9,
        source_ref={
            "quote": "I'll send you the revised SOW by Friday.",
            "at": said.isoformat(),
            "link": "https://drive.google.com/file/d/x/view",
            "where": "the meeting “Acme renewal”",
        },
    )
    db.commit()
    return row


def _event_kinds(db, row) -> list[str]:
    from rubik_core.database.promises import PromiseEvent

    db.flush()
    return [
        e.kind
        for e in db.query(PromiseEvent)
        .filter(PromiseEvent.promise_id == row.id)
        .order_by(PromiseEvent.at, PromiseEvent.id)
    ]


def test_the_same_promise_heard_again_is_not_written_twice(db):
    from rubik_core.promises import store

    _heard_row(db)
    assert store.find_similar_heard(
        db,
        BLAIR,
        title="Send the revised SOW to Dakota",
        counterparty_id=DAKOTA,
        said_on=date(2026, 9, 25),
    )
    assert not store.find_similar_heard(
        db,
        BLAIR,
        title="Book the Q4 offsite",
        counterparty_id=DAKOTA,
        said_on=date(2026, 9, 25),
    )


def test_evidence_never_marks_a_promise_kept(db):
    from rubik_core.promises import store

    row = _heard_row(db)
    store.record_check(db, row, verdict="kept", checked=["sent email"], summary="sent")
    assert row.status == "suggested"  # evidence is not acceptance
    store.accept(db, row, actor=BLAIR)
    row.evidence = None
    store.record_check(db, row, verdict="kept", checked=["sent email"])
    assert row.status == "evidence_found"  # "Looks done, confirm?" — never "kept"
    assert _event_kinds(db, row).count("evidence_found") == 2


def test_ticking_a_heard_promise_accepts_then_keeps_it(db):
    from rubik_core.promises import store

    row = _heard_row(db)
    assert store.keep_from_card(db, row, actor=BLAIR)
    assert row.status == "kept"
    assert _event_kinds(db, row)[-2:] == ["accepted", "kept_confirmed"]


# ── The evidence judge ───────────────────────────────────────────────────────


@pytest.mark.parametrize("lookup_fails", [False, True])
def test_evidence_skips_eos_when_module_off_but_checks_core_lane(
    db, monkeypatch, lookup_fails
):
    from rubik_core.control import repository
    from rubik_core.promises import evidence, store

    if lookup_fails:
        monkeypatch.setattr(
            repository,
            "module_enabled",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("control down")),
        )
    else:
        monkeypatch.setattr(repository, "module_enabled", lambda *_a, **_k: False)
    row = _heard_row(db)
    monkeypatch.setattr(store, "list_unkept_candidates", lambda *_a: [row])
    monkeypatch.setattr(evidence, "is_due_for_check", lambda *_a, **_k: True)
    monkeypatch.setattr(evidence, "drive_evidence", lambda *_a, **_k: [])
    monkeypatch.setattr(
        evidence,
        "eos_evidence",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("EOS table read")),
    )
    prompts = []
    evidence.check(
        BLAIR,
        db=db,
        invoke=lambda prompt: prompts.append(prompt),
        deadline=lambda: False,
        sent_threads=[],
        slack_messages=None,
        today=NOW.date(),
        now=NOW,
    )
    assert len(prompts) == 1


def test_a_kept_verdict_needs_real_evidence_from_after_the_promise(db):
    from rubik_core.promises.evidence import EvidenceItem, parse_checks, tag_pool

    row = _heard_row(db)
    pool = tag_pool(
        [
            EvidenceItem(
                "",
                "email",
                datetime(2026, 9, 26, tzinfo=timezone.utc),
                "sent SOW to Dakota",
            ),
            EvidenceItem(
                "", "email", datetime(2026, 9, 1, tzinfo=timezone.utc), "old SOW"
            ),
        ]
    )
    new, old = pool[0].tag, pool[1].tag
    good = parse_checks(
        {"checks": [{"promise": "P1", "verdict": "kept", "evidence": [new]}]},
        [row],
        pool,
    )
    assert good[row.id]["verdict"] == "kept"
    stale = parse_checks(
        {"checks": [{"promise": "P1", "verdict": "kept", "evidence": [old]}]},
        [row],
        pool,
    )
    assert stale[row.id]["verdict"] == "unclear"
    invented = parse_checks(
        {"checks": [{"promise": "P1", "verdict": "kept", "evidence": ["E99"]}]},
        [row],
        pool,
    )
    assert invented[row.id]["verdict"] == "unclear"


def test_a_promise_is_checked_only_once_its_moment_has_come(db):
    from rubik_core.promises.evidence import is_due_for_check

    dated = _heard_row(db, due=date(2026, 10, 3))
    assert not is_due_for_check(dated, today=date(2026, 10, 1), now=NOW)
    assert is_due_for_check(dated, today=date(2026, 10, 4), now=NOW)
    undated = _heard_row(db, said=datetime(2026, 9, 30, 15, 0), title="Call legal")
    assert not is_due_for_check(undated, today=date(2026, 10, 1), now=NOW)
    ancient = _heard_row(db, said=datetime(2026, 8, 1, 15, 0), title="Old thing")
    assert not is_due_for_check(ancient, today=date(2026, 10, 1), now=NOW)


# ── The promises channel ─────────────────────────────────────────────────────


@pytest.fixture()
def accountability_on(monkeypatch):
    monkeypatch.setattr(settings, "PROMISE_ACCOUNTABILITY_ENABLED", True)
    monkeypatch.setattr(settings, "TODOS_ENABLED", False)
    monkeypatch.setattr(
        "rubik_core.promises.accountability.enrolled", lambda email, user=None: True
    )


def _run_channel(now: datetime = NOW):
    """Run the channel at a pinned ``now``, as the orchestrator does.

    The rows below are dated (said 2026-09-24), so on the wall clock they age
    past ``MAX_AGE_DAYS`` and every assertion here quietly changes meaning.
    """
    from rubik_core.priorities.agents.promises_agent import PromisesChannelAgent
    from rubik_core.priorities.config import PrioritiesConfig
    from rubik_core.priorities.temporal import resolve_user_now

    return PromisesChannelAgent(
        user_email=BLAIR,
        config=PrioritiesConfig(),
        temporal=resolve_user_now(BLAIR, tz_name="UTC", now_utc=now),
    ).run()


def test_an_unchecked_promise_is_never_called_broken(db, accountability_on):
    _heard_row(db, due=date(2026, 9, 26))
    assert _run_channel().signals == []


def test_a_checked_promise_with_no_sign_becomes_an_unkept_signal(db, accountability_on):
    from rubik_core.priorities.models import EntityKind, PriorityKind
    from rubik_core.promises import store

    row = _heard_row(db, due=date(2026, 9, 26))
    store.record_check(db, row, verdict="not_found", checked=["sent email", "Slack"])
    db.commit()
    digest = _run_channel()
    assert len(digest.signals) == 1
    sig = digest.signals[0]
    assert sig.entity_kind == EntityKind.PROMISE and sig.entity_id == row.id
    assert sig.attrs["promise_priority_kind"] == PriorityKind.PROMISE_UNKEPT.value
    assert sig.attrs["promise_checked"] == "sent email, Slack"
    assert sig.attrs["promise_link"].startswith("https://drive.google.com")
    # The words and the verdict reach the model inside its 200-char window.
    assert "revised SOW" in sig.snippet[:200]
    assert "no sign it happened in sent email, Slack" in sig.snippet[:200]


def test_a_promise_found_done_is_not_called_broken(db, accountability_on):
    from rubik_core.promises import store

    row = _heard_row(db, due=date(2026, 9, 26))
    store.record_check(db, row, verdict="kept", checked=["sent email"])
    db.commit()
    assert _run_channel().signals == []


def test_an_accepted_heard_promise_is_one_card_not_two(
    db, monkeypatch, accountability_on
):
    from rubik_core.priorities.models import PriorityKind
    from rubik_core.promises import store

    monkeypatch.setattr(settings, "TODOS_ENABLED", True)
    row = _heard_row(db, due=date(2026, 9, 26))
    store.accept(db, row, actor=BLAIR)
    store.record_check(db, row, verdict="not_found", checked=["sent email"])
    db.commit()
    sigs = _run_channel().signals
    assert [s.attrs["promise_priority_kind"] for s in sigs] == [
        PriorityKind.PROMISE_UNKEPT.value
    ]


def test_a_promise_said_over_a_month_ago_stops_being_called_unkept(
    db, monkeypatch, accountability_on
):
    """Counted from when it was SAID: sent mail and Slack are only read that
    far back, so past it "no sign it happened" would cover email never read."""
    from rubik_core.priorities.models import PriorityKind
    from rubik_core.promises import evidence, store
    from rubik_core.promises.accountability import SENT_LOOKBACK_DAYS

    assert evidence.MAX_AGE_DAYS <= SENT_LOOKBACK_DAYS
    monkeypatch.setattr(settings, "TODOS_ENABLED", True)
    said = datetime(2026, 9, 24, 15, 0, tzinfo=timezone.utc)
    row = _heard_row(db, due=date(2026, 9, 26), said=said)
    store.accept(db, row, actor=BLAIR)
    store.record_check(db, row, verdict="not_found", checked=["sent email"])
    db.commit()

    def kinds(now):
        return [s.attrs["promise_priority_kind"] for s in _run_channel(now).signals]

    last_day = said + timedelta(days=evidence.MAX_AGE_DAYS, hours=23)
    assert kinds(last_day) == [PriorityKind.PROMISE_UNKEPT.value]
    # Aged out: still an open To-Do of theirs, so it falls back to that card.
    assert kinds(last_day + timedelta(hours=2)) == [PriorityKind.PROMISE_DUE.value]


# ── The card ─────────────────────────────────────────────────────────────────


def test_a_commitment_debt_card_built_on_a_heard_promise_is_rekeyed(db):
    from rubik_core.priorities.models import (
        ChannelKind,
        ChannelSignal,
        EntityKind,
        PriorityCandidate,
        PriorityKind,
    )
    from rubik_core.priorities.orchestrator import _reattach_promise_entities

    sig = ChannelSignal(
        id="promise:abc",
        channel=ChannelKind.PROMISES,
        title="[You said you would] Send the SOW",
        snippet="",
        entity_kind=EntityKind.PROMISE,
        entity_id="abc",
        attrs={
            "promise_priority_kind": "promise_unkept",
            "promise_quote": "I'll send you the revised SOW",
            "promise_link": "https://drive.google.com/file/d/x/view",
        },
    )
    card = PriorityCandidate(
        title="You told Dakota you'd send the SOW",
        why_it_matters="x",
        urgency="high",
        signal_ids=["promise:abc"],
        priority_kind=PriorityKind.COMMITMENT_DEBT,
        entity_kind=EntityKind.TRANSCRIPT,
        entity_id="garbled",
    )
    _reattach_promise_entities([card], {sig.id: sig})
    assert card.priority_kind == PriorityKind.PROMISE_UNKEPT
    assert (card.entity_kind, card.entity_id) == (EntityKind.PROMISE, "abc")
    assert card.attrs["promise_quote"].startswith("I'll send")


def test_a_meeting_card_that_merely_cites_a_promise_keeps_its_own_key():
    """The mis-keying lesson: citing is not being about."""
    from rubik_core.priorities.models import (
        ChannelKind,
        ChannelSignal,
        EntityKind,
        PriorityCandidate,
        PriorityKind,
    )
    from rubik_core.priorities.orchestrator import _reattach_promise_entities

    promise = ChannelSignal(
        id="promise:abc",
        channel=ChannelKind.PROMISES,
        title="p",
        snippet="",
        entity_kind=EntityKind.PROMISE,
        entity_id="abc",
        attrs={"promise_priority_kind": "promise_unkept"},
    )
    event = ChannelSignal(
        id="ev1",
        channel=ChannelKind.CALENDAR,
        title="Acme call",
        snippet="",
        entity_kind=EntityKind.EVENT,
        entity_id="ev1",
        attrs={},
    )
    card = PriorityCandidate(
        title="Prep for Acme",
        why_it_matters="x",
        urgency="high",
        signal_ids=["ev1", "promise:abc"],
        priority_kind=PriorityKind.UPCOMING_PREP,
        entity_kind=EntityKind.EVENT,
        entity_id="ev1",
    )
    _reattach_promise_entities([card], {s.id: s for s in (promise, event)})
    assert (card.entity_kind, card.priority_kind) == (
        EntityKind.EVENT,
        PriorityKind.UPCOMING_PREP,
    )


def test_closing_a_promise_clears_both_card_kinds(monkeypatch):
    from rubik_core.priorities.models import PriorityKind
    from rubik_core.promises.priority_sync import sync_promise_priority

    seen = []
    monkeypatch.setattr(
        "rubik_core.priorities.memory.resolve_entity_everywhere",
        lambda **kw: seen.append(kw["priority_kind"]),
    )
    sync_promise_priority(promise_id="abc", closed=True, actor=BLAIR, via="kept")
    assert set(seen) == {PriorityKind.PROMISE_DUE, PriorityKind.PROMISE_UNKEPT}


# ── Gating and cost ──────────────────────────────────────────────────────────


def test_it_ships_to_everyone_behind_a_kill_switch():
    from rubik_core.features.registry import FEATURES

    spec = next(f for f in FEATURES if f.key == "promise_accountability")
    assert spec.kill_switch == "PROMISE_ACCOUNTABILITY_ENABLED"
    assert spec.default().everyone is True
    assert settings.PROMISE_ACCOUNTABILITY_ENABLED is False  # off unless deployed on


def test_the_pass_has_its_own_ai_cost_category():
    from backend.routers.admin import _FEATURE_LABELS
    from rubik_core.promises.accountability import FEATURE_KEY

    assert FEATURE_KEY in _FEATURE_LABELS


def test_the_pass_is_a_no_op_when_switched_off(monkeypatch):
    from rubik_core.promises.accountability import run_accountability_pass

    monkeypatch.setattr(settings, "PROMISE_ACCOUNTABILITY_ENABLED", False)
    monkeypatch.setattr(
        "rubik_core.database.repository.SQLUserRegistry.get_user",
        lambda self, email: SimpleNamespace(name="Blair", email=email),
    )
    assert run_accountability_pass(BLAIR) == {"status": "not_enrolled"}

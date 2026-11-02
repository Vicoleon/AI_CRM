"""The cockpit's commitment clock — aging, dedupe, escalation, close.

Unit + regression cover for ``rubik_core/cockpit/commitment_engine.py`` and
``rubik_core/cockpit/business_hours.py``.
Nothing here touches HubSpot, Gmail or Vertex: the one test that needs sent-mail
evidence fakes ``GmailClient`` at the string target and injects a fake HubSpot
client, so the whole module runs offline on SQLite.

**Units.** Every ``DateTime`` column in this repo is naive and holds UTC, while
the business window is local to ``settings.COCKPIT_BUSINESS_TZ``
(America/Chicago). Those are two different frames and conflating them is how a
09:00 window silently becomes 04:00. So the fixtures are written in Central
wall-clock via :func:`_ct`, which hands back the naive UTC the columns hold, and
results are asserted through :func:`_as_ct`, which reads them back as Central.
An assertion written in bare naive UTC would still pass and would tell a future
reader nothing about which frame it belongs to.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from rubik_core.cockpit import business_hours, sow_sent_detector
from rubik_core.cockpit.business_hours import (
    add_business_hours,
    as_naive_utc,
    business_hours_between,
)
from rubik_core.cockpit.commitment_engine import (
    AT_RISK_FRACTION,
    ESCALATE_AFTER_BUSINESS_DAYS,
    EXEC_VISIBLE_AFTER_BUSINESS_DAYS,
    close_commitment,
    compute_breach_at,
    confirm,
    open_commitment,
    recompute_states,
    was_breached,
)
from rubik_core.database.commitments import (
    COMMITMENT_CLOSED_SOURCES,
    Commitment,
    CommitmentSlaConfig,
)
from rubik_core.database.models import Base

AE = "ae@acme.example"
OTHER_AE = "other@acme.example"

#: The Drafting SOW stage. Custom, and the one Harper sets once and never
#: touches again — the exact reason no clock ages from a stage date (gate 4).
DRAFTING_SOW_STAGE = "drafting_sow"

DEAL = "8801"
COMPANY = "7701"

CENTRAL = ZoneInfo("America/Chicago")


def _ct(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """A Central wall-clock instant as the NAIVE UTC the DB columns hold."""
    return as_naive_utc(datetime(year, month, day, hour, minute, tzinfo=CENTRAL))


def _as_ct(value: datetime) -> datetime:
    """A naive-UTC instant read back as an aware Central datetime."""
    return value.replace(tzinfo=timezone.utc).astimezone(CENTRAL)


def _ct_at(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """The aware-Central value an assertion compares against."""
    return datetime(year, month, day, hour, minute, tzinfo=CENTRAL)


# ── The week every fixture runs in. Mon 24 Aug 2026 – Wed 2 Sep 2026, all CDT.
OPENED = _ct(2026, 8, 24, 9)  # Monday 09:00 CDT — business day open
SLA_SOW_STANDARD = 24  # business hours = 3 business days at 8h/day


@pytest.fixture
def db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,  # mandatory for :memory:
    )
    # rubik_core.database.models imports the commitments/call_capture/eos/
    # smart_teams modules on its last line, so importing Base is enough to have
    # commitments + commitment_sla_configs registered on the metadata.
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    # Patch by string target: several suites importlib.reload() the db module
    # mid-run, which would leave an object-target patch pointing at a stale
    # module attribute.
    monkeypatch.setattr("rubik_core.database.db.SessionLocal", Session)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _seed_sla(db, *, kind: str = "sow", sow_kind: str = "standard", hours: int = 24):
    row = CommitmentSlaConfig(
        id=uuid.uuid4().hex, kind=kind, sow_kind=sow_kind, hours=hours
    )
    db.add(row)
    db.flush()
    return row


def _live(db, owner: str = AE) -> list[Commitment]:
    return (
        db.query(Commitment)
        .filter(Commitment.owner_email == owner, Commitment.closed_at.is_(None))
        .all()
    )


def _epoch_ms(naive_utc: datetime) -> str:
    return str(int(naive_utc.replace(tzinfo=timezone.utc).timestamp() * 1000))


def _freeze_engine_clock(monkeypatch, instant: datetime) -> None:
    """Pin ``datetime.utcnow()`` inside the engine module.

    ``confirm()``, ``close_commitment()`` and the provisional promotion inside
    ``open_commitment()`` all apply the clock immediately and take no ``now=``
    argument, so this is the only injection point they have. Subclass + string
    target, per the repo's convention (commit ``b8558df``): there is no
    freezegun in ``requirements.txt``, and patching the class object captured at
    this module's import time would go stale in a full-suite run.
    """

    class _FrozenDatetime(datetime):
        @classmethod
        def utcnow(cls) -> datetime:
            return instant

    monkeypatch.setattr(
        "rubik_core.cockpit.commitment_engine.datetime", _FrozenDatetime
    )


class _StageFrozenHubSpot:
    """A portal where the deal NEVER leaves Drafting SOW.

    Records every property list any caller asks for, so the crown-jewel test can
    assert that nothing in the aging path ever read a stage timestamp.
    """

    def __init__(self):
        self.property_reads: list[tuple[str, str, tuple[str, ...]]] = []
        self.deal_properties = {
            "dealstage": DRAFTING_SOW_STAGE,
            # The property gate 4 forbids using as the clock.
            # Present and stale on purpose: if anything reads it, the design has
            # regressed.
            f"hs_v2_date_entered_{DRAFTING_SOW_STAGE}": "2026-01-05T00:00:00Z",
        }
        self.read_client = self

    def get_object(self, object_type, object_id, properties=None):
        self.property_reads.append(
            (str(object_type), str(object_id), tuple(properties or ()))
        )
        if object_type == "companies":
            return {"id": str(object_id), "properties": {"domain": "clientco.com"}}
        return {"id": str(object_id), "properties": dict(self.deal_properties)}

    def search_all(self, object_type, filters=None, properties=None):
        self.property_reads.append(
            (str(object_type), "search", tuple(properties or ()))
        )
        if object_type == "contacts":
            return [{"id": "c1", "properties": {"email": "parker@clientco.com"}}]
        return []


def _sow_message(sent_at: datetime, *, message_id: str = "msg-sow-1") -> dict:
    return {
        "id": message_id,
        "threadId": "thread-sow-1",
        "internalDate": _epoch_ms(sent_at),
        "payload": {
            "headers": [
                {"name": "From", "value": f"Acme AE <{AE}>"},
                {"name": "To", "value": "Parker Client <parker@clientco.com>"},
                {"name": "Subject", "value": "Scope of work for your review"},
            ],
            "parts": [{"filename": "Acme-SOW-ClientCo-v1.pdf"}],
        },
    }


def _fake_gmail_class(sent_at: datetime):
    """A ``GmailClient`` stand-in that answers only the attachment lane."""

    class _FakeGmail:
        def __init__(self, user_email: str | None = None, **_kw):
            self.user_email = user_email
            self.service = object()  # truthy: "the mailbox is available"

        def search_threads(self, query: str = "", max_results: int = 20):
            if "has:attachment" not in query:
                return []
            return [{"messages": [_sow_message(sent_at)]}]

    return _FakeGmail


# ══════════════════════════════════════════════════════════════════════════════
# THE CROWN JEWEL — the cockpit's named regression. Do not delete or weaken.
# ══════════════════════════════════════════════════════════════════════════════


def test_gate4_stage_never_moves_yet_the_clock_ages_and_a_suggestion_closes_it(
    db, monkeypatch
):
    """A deal frozen in Drafting SOW still ages, and closes on a click.

    **This is the test that proves gate 4**, the design gate that kills the
    naive build: Harper sets the Drafting SOW stage once and
    does not touch it again until invoicing, so anything that ages a commitment
    from ``hs_v2_date_entered_<stage>`` fires on stale data and the team stops
    trusting the cockpit within two weeks. If this test ever fails, the design
    has regressed to the version the room rejected.

    Three properties, each asserted below, and none of them is a refactorable
    detail:

    1. **The clock is Cubie's, not HubSpot's.** The stage here never changes and
       the stale stage timestamp is present in the fake portal the whole time.
       The commitment walks open → at_risk → breached → escalated →
       exec_visible anyway, and no read in the aging path asks for a stage
       property at all.
    2. **Sent-mail evidence raises a SUGGESTION, not an auto-close.** After the
       scan the commitment is still live and still ``exec_visible``. Auto-closing
       on a heuristic would hide a real breach — which is precisely the failure
       PS1 exists to fix, so a "helpful" auto-close is not an optimisation of
       this design, it is a reversal of it. ``COMMITMENT_CLOSED_SOURCES`` has no
       ``email`` member for the same reason, asserted here too: no future code
       path can auto-close from a heuristic without a migration, in public.
    3. **Lateness survives the close.** A human confirms, the close records
       ``manual``, and ``was_breached`` still answers True — the breach is what
       the feature reports on, so a close that erased it would erase the point.
    """
    _seed_sla(db, hours=SLA_SOW_STANDARD)
    # The human clicks Yes half an hour after the exec tier is reached.
    _freeze_engine_clock(monkeypatch, _ct(2026, 9, 2, 17, 30))
    hs = _StageFrozenHubSpot()

    commitment = open_commitment(
        db,
        kind="sow",
        owner_email=AE,
        company_id=COMPANY,
        deal_id=DEAL,
        opened_at=OPENED,
        source="call",
        evidence_ref="entry-12",
        evidence_text="Parker: send us the SOW by end of week.",
        sow_kind="standard",
    )
    assert commitment.sla_hours == SLA_SOW_STANDARD
    assert commitment.state == "open"
    # 24 business hours from Monday 09:00 CDT is Wednesday 17:00 CDT, not
    # Tuesday 09:00 — calendar hours would have breached this two days early.
    assert _as_ct(commitment.breach_at) == _ct_at(2026, 8, 26, 17)

    # ── The ladder, while the HubSpot stage sits at Drafting SOW throughout ───
    ladder = [
        (_ct(2026, 8, 24, 16), "open", "unchanged"),
        (_ct(2026, 8, 26, 10), "at_risk", "to_at_risk"),  # 0.7 * 24bh = Wed 09:48
        (_ct(2026, 8, 26, 17), "breached", "to_breached"),
        (_ct(2026, 8, 28, 17), "escalated", "to_escalated"),  # breach + 2 bus. days
        (_ct(2026, 9, 2, 17), "exec_visible", "to_exec_visible"),  # breach + 5 days
    ]
    for now, expected_state, expected_bucket in ladder:
        buckets = recompute_states(db, now=now)
        assert buckets[expected_bucket] == 1, (expected_state, buckets)
        assert commitment.state == expected_state, (now, buckets)
        # The stage has not moved and never will in this test.
        assert hs.deal_properties["dealstage"] == DRAFTING_SOW_STAGE

    assert commitment.escalated_at is not None
    assert commitment.exec_visible_at is not None
    assert was_breached(commitment, now=_ct(2026, 9, 2, 17)) is True

    # ── Sent-mail evidence appears: a SUGGESTION, and nothing closes ──────────
    sent_at = _ct(2026, 8, 28, 10)
    monkeypatch.setattr(
        "rubik_core.cockpit.sow_sent_detector.GmailClient",
        _fake_gmail_class(sent_at),
    )
    suggestions = sow_sent_detector.scan_sent_mail(db, AE, OPENED, hs=hs)

    assert len(suggestions) == 1
    suggestion = suggestions[0]
    assert suggestion["commitment_id"] == commitment.id
    assert suggestion["message_id"] == "msg-sow-1"
    assert suggestion["confidence"] >= sow_sent_detector.MIN_SUGGESTION_CONFIDENCE
    # Dated in the cockpit's org-wide timezone, not the reader's.
    assert (
        suggestion["suggestion"]
        == "Looks like the SOW went out on 28 Aug — close this?"
    )
    # THE ASSERTION THAT MATTERS: a scan closes nothing.
    db.refresh(commitment)
    assert commitment.closed_at is None
    assert commitment.state == "exec_visible"
    assert commitment.closed_source is None

    # ── A human confirms the suggestion. That, and only that, closes it ───────
    close_commitment(
        db,
        commitment,
        source="manual",
        evidence_ref=suggestion["message_id"],
        actor=AE,
    )
    assert commitment.state == "closed"
    assert commitment.closed_source == "manual"  # a confirmed heuristic IS manual
    assert commitment.closed_evidence_ref == "msg-sow-1"
    # Closing clears nothing — the breach is the reportable fact.
    assert commitment.breach_at is not None
    assert commitment.escalated_at is not None
    assert was_breached(commitment) is True

    # There is no email close source and no stage close source, in the schema
    # vocabulary or in the engine.
    assert COMMITMENT_CLOSED_SOURCES == ("manual", "superseded")
    for forbidden in ("email", "stage", "auto"):
        with pytest.raises(ValueError):
            close_commitment(db, commitment, source=forbidden)

    # And nothing anywhere in this flow read a stage property. The company and
    # contact lookups the mail scan makes are the only HubSpot reads at all.
    for object_type, _object_id, properties in hs.property_reads:
        for prop in properties:
            assert "date_entered" not in prop, (object_type, prop)
            assert prop != "dealstage", (object_type, prop)


# ══════════════════════════════════════════════════════════════════════════════
# compute_breach_at — the ETA rules
# ══════════════════════════════════════════════════════════════════════════════


def test_compute_breach_at_with_no_eta_is_the_sla_in_business_hours():
    breach = compute_breach_at(OPENED, SLA_SOW_STANDARD, None)
    assert _as_ct(breach) == _ct_at(2026, 8, 26, 17)


def test_compute_breach_at_returns_none_when_the_age_is_unknown():
    """A NULL ``opened_at`` has no breach instant — unknown age, not zero age."""
    assert compute_breach_at(None, SLA_SOW_STANDARD, None) is None
    assert compute_breach_at(None, SLA_SOW_STANDARD, _ct(2026, 9, 15, 12)) is None


def test_compute_breach_at_lets_a_later_eta_win():
    """A three-week RFP is not late on day four (Emery's rule)."""
    promised = _ct(2026, 9, 14, 17)
    breach = compute_breach_at(OPENED, SLA_SOW_STANDARD, promised)
    assert breach == promised
    assert _as_ct(breach) > _ct_at(2026, 8, 26, 17)


def test_compute_breach_at_ignores_an_eta_earlier_than_the_sla():
    """An AE over-promising to a client must not manufacture an internal breach.

    The honest thing — telling a client a tight date — cannot be the thing that
    turns your own row red.
    """
    promised = _ct(2026, 8, 25, 9)  # a day and a half inside the SLA
    breach = compute_breach_at(OPENED, SLA_SOW_STANDARD, promised)
    assert _as_ct(breach) == _ct_at(2026, 8, 26, 17)
    assert breach != promised


def test_compute_breach_at_tracks_an_eta_changed_mid_flight():
    """Revising an ETA re-prices the clock, in both directions but never below.

    Mirrors ``PATCH /api/commitments/{id}/eta``, which recomputes with
    ``compute_breach_at(row.opened_at, row.sla_hours, row.promised_at)``.
    """
    sla_only = compute_breach_at(OPENED, SLA_SOW_STANDARD, None)

    pushed_out = compute_breach_at(OPENED, SLA_SOW_STANDARD, _ct(2026, 9, 4, 17))
    assert _as_ct(pushed_out) == _ct_at(2026, 9, 4, 17)

    # Revised again, this time to a date INSIDE the SLA: the clock returns to the
    # SLA rather than following the ETA down.
    pulled_in = compute_breach_at(OPENED, SLA_SOW_STANDARD, _ct(2026, 8, 25, 12))
    assert pulled_in == sla_only

    # And cleared entirely.
    assert compute_breach_at(OPENED, SLA_SOW_STANDARD, None) == sla_only


# ══════════════════════════════════════════════════════════════════════════════
# business_hours — the arithmetic that must exist exactly once ────
# ══════════════════════════════════════════════════════════════════════════════


def test_add_business_hours_documented_unit_09_00_plus_24_is_two_days_later_at_close():
    """The module's own units: naive in = naive UTC, window local to Central.

    Wednesday 09:00 CDT + 24 business hours = Friday 17:00 CDT. Naive datetimes
    are read as UTC (the columns store naive UTC) while the 09:00–17:00 window
    is local to ``COCKPIT_BUSINESS_TZ`` — assuming naive meant *local* would put
    the window five hours out and every assertion here would drift.
    """
    start = _ct(2026, 8, 26, 9)
    assert start == datetime(2026, 8, 26, 14)  # 09:00 CDT is 14:00 UTC
    result = add_business_hours(start, 24)
    assert result.tzinfo is None  # naive in, naive out
    assert _as_ct(result) == _ct_at(2026, 8, 28, 17)


def test_add_business_hours_skips_the_weekend():
    """Friday afternoon + 8 business hours lands Monday afternoon.

    A ``timedelta`` would have said Saturday 23:00 — the Friday-afternoon false
    positive business hours exist to prevent, and the one that loses the argument with
    the AE in week one.
    """
    friday_15 = _ct(2026, 8, 28, 15)
    assert _as_ct(add_business_hours(friday_15, 2)) == _ct_at(2026, 8, 28, 17)
    assert _as_ct(add_business_hours(friday_15, 8)) == _ct_at(2026, 8, 31, 15)
    # Fri 15:00–17:00 (2h) + Mon 8h + Tue 8h + Wed 09:00–15:00 (6h) = 24.
    assert _as_ct(add_business_hours(friday_15, 24)) == _ct_at(2026, 9, 2, 15)


def test_add_business_hours_from_the_weekend_starts_the_clock_monday_morning():
    """Opened Saturday? The clock starts at Monday's open, not Saturday midnight."""
    saturday = _ct(2026, 8, 29, 10)
    assert _as_ct(add_business_hours(saturday, 0)) == _ct_at(2026, 8, 31, 9)
    assert _as_ct(add_business_hours(saturday, 8)) == _ct_at(2026, 8, 31, 17)


def test_business_hours_between_counts_only_time_inside_the_window():
    friday_15 = _ct(2026, 8, 28, 15)
    monday_10 = _ct(2026, 8, 31, 10)
    # Fri 15:00–17:00 (2h) + Mon 09:00–10:00 (1h). 67 calendar hours, 3 business.
    assert business_hours_between(friday_15, monday_10) == 3.0
    # Wholly outside any window.
    assert business_hours_between(_ct(2026, 8, 28, 18), _ct(2026, 8, 29, 12)) == 0.0
    # Never negative: a future opened_at is "not started", not "-6 hours old".
    assert business_hours_between(monday_10, friday_15) == 0.0


def test_add_business_hours_preserves_awareness_of_an_aware_input():
    aware = datetime(2026, 8, 28, 15, tzinfo=CENTRAL)
    result = add_business_hours(aware, 8)
    assert result.tzinfo is not None
    assert result.astimezone(CENTRAL) == _ct_at(2026, 8, 31, 15)


def test_holidays_are_deliberately_not_honoured_d16():
    """Christmas Day counts as a working day. Documented limitation, not a bug.

    There is no holiday calendar in this repo and inventing one is
    separate work. ``_is_non_working_day`` is the single named hook, so adding a
    source later is a one-function change — which is exactly why this behaviour
    is pinned rather than left to be "fixed" by someone reading it as an
    oversight. 25 Dec 2026 is a Friday, so a weekend rule cannot mask it.
    """
    christmas = datetime(2026, 12, 25).date()
    assert christmas.weekday() == 4  # Friday
    assert business_hours._is_non_working_day(christmas) is False

    # Thursday 24 Dec 09:00 + 16 business hours consumes Christmas Day whole.
    assert _as_ct(add_business_hours(_ct(2026, 12, 24, 9), 16)) == _ct_at(
        2026, 12, 25, 17
    )
    # And the elapsed count agrees: Christmas contributes a full 8 hours.
    assert business_hours_between(_ct(2026, 12, 24, 9), _ct(2026, 12, 25, 17)) == 16.0


# ══════════════════════════════════════════════════════════════════════════════
# open_commitment — dedupe, back-dating, scope back-fill
# ══════════════════════════════════════════════════════════════════════════════


def test_three_sources_for_one_deal_produce_one_row_and_the_earliest_open_wins(db):
    """Three opens fire for one SOW: one row, earliest clock."""
    _seed_sla(db, hours=SLA_SOW_STANDARD)

    first = open_commitment(
        db,
        kind="sow",
        owner_email=AE,
        deal_id=DEAL,
        opened_at=_ct(2026, 8, 25, 11),
        source="manual",
        evidence_ref="SUP-4410",
        evidence_text="SOW request logged",
        sow_kind="standard",
    )
    second = open_commitment(
        db,
        kind="sow",
        owner_email=AE,
        deal_id=DEAL,
        opened_at=OPENED,  # Monday 09:00 — EARLIER than the first open
        source="call",
        evidence_ref="entry-12",
        evidence_text="Parker: send us the SOW.",
        sow_kind="standard",
    )
    third = open_commitment(
        db,
        kind="sow",
        owner_email=AE,
        deal_id=DEAL,
        opened_at=_ct(2026, 8, 26, 9),  # LATER again — must not move it forward
        source="manual",
        evidence_ref="manual-1",
        evidence_text="Harper confirmed by phone",
        sow_kind="standard",
    )

    assert first.id == second.id == third.id
    assert db.query(Commitment).count() == 1
    assert first.opened_at == OPENED
    assert _as_ct(first.breach_at) == _ct_at(2026, 8, 26, 17)
    for fragment in ("SUP-4410", "entry-12", "manual-1"):
        assert fragment in first.opened_evidence_text


def test_a_later_source_processed_first_is_back_dated_to_the_call(db):
    """A later source is processed first; the call is when the client asked.

    Back-dating is the point, not a rounding detail: a later source usually
    lands after the call, and letting it own ``opened_at`` would reset the age
    of the exact stall this feature exists to catch.
    """
    _seed_sla(db, hours=SLA_SOW_STANDARD)

    row = open_commitment(
        db,
        kind="sow",
        owner_email=AE,
        deal_id=DEAL,
        opened_at=_ct(2026, 8, 25, 11),
        source="manual",
        evidence_ref="SUP-4410",
        sow_kind="standard",
    )
    first_breach = row.breach_at
    assert row.opened_at == _ct(2026, 8, 25, 11)

    open_commitment(
        db,
        kind="sow",
        owner_email=AE,
        deal_id=DEAL,
        opened_at=OPENED,
        source="call",
        evidence_ref="entry-12",
        evidence_text="Parker: send us the SOW.",
        sow_kind="standard",
    )

    assert row.opened_at == OPENED
    assert row.breach_at < first_breach  # the clock moved back, not forward
    assert _as_ct(row.breach_at) == _ct_at(2026, 8, 26, 17)
    # The earlier evidence becomes the cited opener.
    assert row.opened_evidence_ref == "entry-12"


def test_a_call_sourced_row_with_no_deal_id_still_dedupes_against_a_later_open(db):
    """The engine's named dedupe case, and the one SQL cannot dedupe on its own.

    A call-sourced commitment is opened before anything has resolved which deal
    was on the call, so ``hubspot_deal_id`` is NULL — and SQL treats NULLs as
    distinct, so a single unique index on (deal_id, kind) would let every
    15-minute sweep open another live row. The company-scoped constraint is what
    catches it, and the arriving deal id back-fills onto the same row.
    """
    _seed_sla(db, hours=SLA_SOW_STANDARD)

    call_row = open_commitment(
        db,
        kind="sow",
        owner_email=AE,
        company_id=COMPANY,
        deal_id=None,
        opened_at=OPENED,
        source="call",
        evidence_ref="entry-12",
        sow_kind="standard",
    )
    assert call_row.hubspot_deal_id is None
    assert call_row.is_live_company_scoped is True
    assert call_row.is_live_deal_scoped is None

    later_row = open_commitment(
        db,
        kind="sow",
        owner_email=AE,
        company_id=COMPANY,
        deal_id=DEAL,
        opened_at=_ct(2026, 8, 25, 11),
        source="manual",
        evidence_ref="SUP-4410",
        sow_kind="standard",
    )

    assert later_row.id == call_row.id
    assert db.query(Commitment).count() == 1
    assert call_row.hubspot_deal_id == DEAL
    # The row moved from the company-scoped constraint to the deal-scoped one.
    assert call_row.is_live_deal_scoped is True
    assert call_row.is_live_company_scoped is None
    assert call_row.opened_at == OPENED  # the call's clock survives


def test_a_deal_id_backfill_collision_folds_the_company_row_as_superseded(db):
    """Two live rows for one obligation meet: the deal-scoped row survives.

    The company-scoped row is closed as ``superseded`` rather than deleted — it
    is somebody's audit trail — and the survivor inherits the earlier
    ``opened_at`` and the loser's evidence.
    """
    _seed_sla(db, hours=SLA_SOW_STANDARD)

    company_row = open_commitment(
        db,
        kind="sow",
        owner_email=AE,
        company_id=COMPANY,
        opened_at=OPENED,
        source="call",
        evidence_ref="entry-12",
        evidence_text="Parker: send us the SOW.",
        sow_kind="standard",
    )
    deal_row = open_commitment(
        db,
        kind="sow",
        owner_email=AE,
        deal_id=DEAL,  # no company id, so it does not match the row above
        opened_at=_ct(2026, 8, 26, 9),
        source="manual",
        evidence_ref="SUP-4410",
        sow_kind="standard",
    )
    assert company_row.id != deal_row.id
    assert len(_live(db)) == 2

    # A third sighting carries BOTH keys and the two rows resolve to one.
    survivor = open_commitment(
        db,
        kind="sow",
        owner_email=AE,
        company_id=COMPANY,
        deal_id=DEAL,
        opened_at=_ct(2026, 8, 27, 9),
        source="manual",
        evidence_ref="manual-1",
        sow_kind="standard",
    )

    assert survivor.id == deal_row.id
    assert [row.id for row in _live(db)] == [deal_row.id]
    assert deal_row.opened_at == OPENED  # inherited the earlier clock
    assert "entry-12" in deal_row.opened_evidence_text  # inherited the evidence
    assert company_row.state == "closed"
    assert company_row.closed_source == "superseded"
    assert company_row.closed_evidence_ref == deal_row.id
    assert company_row.is_live_company_scoped is None


def test_a_scopeless_adhoc_wrapup_dedupes_on_its_evidence_ref(db):
    """No deal and no company: both partial constraints are NULL, so SQL permits
    unlimited duplicates and the engine has to close the gap itself.

    This is the ad-hoc-call wrap-up shape. The 15-minute sweep re-reads a
    24-hour lookback, so without the evidence-ref fallback one ad-hoc call would
    open ~96 live wrap-up commitments and the AE's My Day would fill with copies
    of one obligation.
    """
    for _ in range(4):
        row = open_commitment(
            db,
            kind="wrapup",
            owner_email=AE,
            company_id=None,
            deal_id=None,
            opened_at=OPENED,
            source="call",
            evidence_ref="conference-record-77",
            evidence_text="Wrap up the ad-hoc call with Parker.",
        )

    assert db.query(Commitment).count() == 1
    assert row.is_live_deal_scoped is None
    assert row.is_live_company_scoped is None
    # A DIFFERENT call is a different obligation and does get its own row.
    open_commitment(
        db,
        kind="wrapup",
        owner_email=AE,
        opened_at=OPENED,
        source="call",
        evidence_ref="conference-record-78",
    )
    assert db.query(Commitment).count() == 2


def test_a_repeated_sweep_does_not_grow_the_evidence_text(db):
    """Exact-line dedupe, because the sweep re-reports the same call all day."""
    _seed_sla(db, hours=SLA_SOW_STANDARD)
    kwargs = dict(
        kind="sow",
        owner_email=AE,
        deal_id=DEAL,
        opened_at=OPENED,
        source="call",
        evidence_ref="entry-12",
        evidence_text="Parker: send us the SOW.",
        sow_kind="standard",
    )
    row = open_commitment(db, **kwargs)
    after_first = row.opened_evidence_text
    for _ in range(20):
        open_commitment(db, **kwargs)
    assert row.opened_evidence_text == after_first
    assert row.opened_evidence_text.count("entry-12") == 1


def test_a_human_open_promotes_a_provisional_row_and_lands_it_in_the_right_tier(
    db, monkeypatch
):
    """A human-sourced open is not an AI guess, so its arrival arms the ladder.

    And the promotion applies the clock at once: a three-week-old request is
    not "open, 0% elapsed" on anybody's screen.
    """
    _seed_sla(db, hours=SLA_SOW_STANDARD)
    _freeze_engine_clock(monkeypatch, _ct(2026, 8, 28, 17))
    row = open_commitment(
        db,
        kind="sow",
        owner_email=AE,
        deal_id=DEAL,
        opened_at=OPENED,
        source="call",
        evidence_ref="entry-12",
        sow_kind="standard",
        provisional=True,
    )
    assert row.state == "provisional"

    open_commitment(
        db,
        kind="sow",
        owner_email=AE,
        deal_id=DEAL,
        opened_at=_ct(2026, 8, 26, 9),
        source="manual",
        evidence_ref="SUP-4410",
        sow_kind="standard",
    )
    assert row.state == "escalated"  # breach + 2 business days, applied at once
    assert row.escalated_at == _ct(2026, 8, 28, 17)


def test_open_commitment_rejects_unknown_kinds_sources_and_blank_owners(db):
    _seed_sla(db, hours=SLA_SOW_STANDARD)
    with pytest.raises(ValueError, match="kind must be one of"):
        open_commitment(db, kind="renewal", owner_email=AE, source="call")
    with pytest.raises(ValueError, match="source must be one of"):
        open_commitment(db, kind="sow", owner_email=AE, source="stage")
    with pytest.raises(ValueError, match="owner_email is required"):
        open_commitment(db, kind="sow", owner_email="  ", source="call")


# ══════════════════════════════════════════════════════════════════════════════
# recompute_states — the skips, and forward-only motion
# ══════════════════════════════════════════════════════════════════════════════


def test_recompute_states_skips_provisional_accepted_and_unknown_age_rows(db):
    """Three skips, each of which is load-bearing.

    ``provisional`` is how an unconfirmed AI extraction never pages Marin;
    ``accepted`` is TERMINAL until close — without that skip an accepted breach
    re-escalates every 15 minutes forever and the one feature the supervisor
    relies on becomes the loudest row on their screen; a NULL ``opened_at`` is
    an unknown age, and an unknown age does not age.
    """
    _seed_sla(db, hours=SLA_SOW_STANDARD)

    provisional = open_commitment(
        db,
        kind="sow",
        owner_email=AE,
        deal_id="9001",
        opened_at=OPENED,
        source="call",
        evidence_ref="entry-1",
        sow_kind="standard",
        provisional=True,
    )
    unknown_age = open_commitment(
        db,
        kind="sow",
        owner_email=AE,
        deal_id="9002",
        opened_at=None,
        source="stage_backfill",
        evidence_ref="stage-1",
        sow_kind="standard",
    )
    accepted = open_commitment(
        db,
        kind="sow",
        owner_email=AE,
        deal_id="9003",
        opened_at=OPENED,
        source="manual",
        evidence_ref="SUP-1",
        sow_kind="standard",
    )
    accepted.state = "accepted"
    accepted.accepted_reason = "Client pushed the kickoff; agreed with Marin."
    db.flush()

    # Long after every threshold has passed.
    buckets = recompute_states(db, now=_ct(2026, 9, 30, 12))

    assert buckets["scanned"] == 3
    assert buckets["skipped_provisional"] == 1
    assert buckets["skipped_accepted"] == 1
    assert buckets["skipped_unknown_age"] == 1
    assert buckets["to_breached"] == 0
    assert buckets["to_escalated"] == 0

    assert provisional.state == "provisional"
    assert provisional.escalated_at is None
    assert accepted.state == "accepted"
    assert accepted.escalated_at is None
    assert unknown_age.state == "open"
    assert unknown_age.breach_at is None


def test_a_provisional_row_still_runs_its_clock_and_confirm_arms_the_ladder(
    db, monkeypatch
):
    """The clock never waits for a human; only the escalation does."""
    _seed_sla(db, hours=SLA_SOW_STANDARD)
    # confirm() takes no now= — see _freeze_engine_clock.
    _freeze_engine_clock(monkeypatch, _ct(2026, 9, 2, 17))
    row = open_commitment(
        db,
        kind="sow",
        owner_email=AE,
        deal_id=DEAL,
        opened_at=OPENED,
        source="call",
        evidence_ref="entry-12",
        sow_kind="standard",
        provisional=True,
    )
    # breach_at is computed at open even though escalation is disarmed.
    assert _as_ct(row.breach_at) == _ct_at(2026, 8, 26, 17)
    assert recompute_states(db, now=_ct(2026, 9, 30, 12))["skipped_provisional"] == 1
    assert row.state == "provisional"

    confirm(db, row)
    # confirm() applies the clock at once rather than waiting for the next sweep.
    assert row.state == "exec_visible"
    assert row.escalated_at is not None

    # Idempotent: a second confirm is a no-op, not a 500 and not a re-escalation.
    stamped = row.escalated_at
    confirm(db, row)
    assert row.state == "exec_visible"
    assert row.escalated_at == stamped


def test_recompute_states_only_moves_forward_and_never_un_breaches(db, monkeypatch):
    """A breached row does not walk back to amber when the ETA moves out.

    Pushing the ETA out after a breach moves the escalation steps still ahead;
    it does not un-breach. Un-breaching on a recompute would let a row that
    already reached somebody's worklist quietly vanish from it.
    """
    _seed_sla(db, hours=SLA_SOW_STANDARD)
    _freeze_engine_clock(monkeypatch, _ct(2026, 8, 27, 9))
    row = open_commitment(
        db,
        kind="sow",
        owner_email=AE,
        deal_id=DEAL,
        opened_at=OPENED,
        source="call",
        evidence_ref="entry-12",
        sow_kind="standard",
    )
    recompute_states(db, now=_ct(2026, 8, 26, 17))
    assert row.state == "breached"

    # The AE now sets an ETA three weeks out, the way the ETA endpoint does.
    row.promised_at = _ct(2026, 9, 18, 17)
    row.promised_reason = "Client asked us to hold for their legal review."
    row.breach_at = compute_breach_at(row.opened_at, row.sla_hours, row.promised_at)
    db.flush()
    assert _as_ct(row.breach_at) == _ct_at(2026, 9, 18, 17)

    buckets = recompute_states(db, now=_ct(2026, 8, 27, 9))
    assert buckets["unchanged"] == 1
    assert row.state == "breached"  # not at_risk, not open

    # Re-running the sweep at the same instant changes nothing.
    assert recompute_states(db, now=_ct(2026, 8, 27, 9))["unchanged"] == 1
    assert row.state == "breached"


def test_a_breach_survives_the_close_and_stays_reportable(db, monkeypatch):
    """``was_breached`` is the reporting predicate, and closing clears nothing.

    ``state`` cannot answer "was this late?" once the row closes — the close
    overwrites the tier — so the surviving ``breached_at`` stamp is the record,
    and ``breach_at`` stays put beside it for "how late, against what deadline".
    """
    _seed_sla(db, hours=SLA_SOW_STANDARD)
    _freeze_engine_clock(monkeypatch, _ct(2026, 8, 27, 9))
    row = open_commitment(
        db,
        kind="sow",
        owner_email=AE,
        deal_id=DEAL,
        opened_at=OPENED,
        source="call",
        evidence_ref="entry-12",
        sow_kind="standard",
    )
    recompute_states(db, now=_ct(2026, 8, 26, 17))
    breach_at = row.breach_at

    close_commitment(db, row, source="manual", actor=AE)
    assert row.state == "closed"
    assert row.breach_at == breach_at  # cleared nothing
    assert was_breached(row) is True

    # A commitment closed before its deadline never breached.
    on_time = open_commitment(
        db,
        kind="sow",
        owner_email=AE,
        deal_id="9600",
        opened_at=OPENED,
        source="manual",
        evidence_ref="SUP-9600",
        sow_kind="standard",
    )
    _freeze_engine_clock(monkeypatch, _ct(2026, 8, 25, 9))
    close_commitment(db, on_time, source="manual", actor=AE)
    assert was_breached(on_time) is False


def test_pushing_an_eta_past_the_close_does_not_erase_the_breach(db, monkeypatch):
    """The breach SURVIVES an ETA revision that outlives the close. Do not invert.

    This is the path that used to lose a real breach, and it is the reason
    ``breached_at`` exists as a column instead of ``was_breached`` deriving
    lateness at read time. Every other record of the miss is destroyed here, in
    order, by things the product is supposed to let people do:

    1. the sweep sees the crossing and the row reads ``"breached"``;
    2. the AE revises the ETA three weeks out — legitimate, and it moves
       ``breach_at`` INTO THE FUTURE, past the eventual close instant;
    3. the row never reaches ``escalated``, so neither escalation stamp is set;
    4. the close overwrites ``state`` with ``"closed"``.

    A predicate reading ``breach_at`` against ``closed_at`` plus the escalation
    stamps therefore answered ``False`` — a genuine breach deleting itself from
    the reporting that justifies this whole feature, quietly, with nothing
    erroring. The engine's own docstring promises the opposite.

    So the assertion below is the promise, not a description of the
    implementation: whatever ``was_breached`` reads, a commitment that was late
    stays late. If a future change makes this fail, the change is wrong.
    """
    _seed_sla(db, hours=SLA_SOW_STANDARD)
    _freeze_engine_clock(monkeypatch, _ct(2026, 8, 27, 9))
    row = open_commitment(
        db,
        kind="sow",
        owner_email=AE,
        deal_id=DEAL,
        opened_at=OPENED,
        source="call",
        evidence_ref="entry-12",
        sow_kind="standard",
    )
    recompute_states(db, now=_ct(2026, 8, 26, 17))
    assert row.state == "breached"
    assert was_breached(row, now=_ct(2026, 8, 27, 9)) is True
    # The deadline that was missed, not the instant the sweep noticed it: a
    # sweep delayed by an outage must not re-date the breach.
    assert _as_ct(row.breached_at) == _ct_at(2026, 8, 26, 17)

    row.promised_at = _ct(2026, 9, 18, 17)
    row.breach_at = compute_breach_at(row.opened_at, row.sla_hours, row.promised_at)
    db.flush()
    # The revision moved the deadline out; it did not move the history.
    assert _as_ct(row.breach_at) == _ct_at(2026, 9, 18, 17)
    assert _as_ct(row.breached_at) == _ct_at(2026, 8, 26, 17)

    close_commitment(db, row, source="manual", actor=AE)
    assert row.escalated_at is None  # it never escalated — no backstop to lean on
    assert row.breach_at > row.closed_at  # the deadline outlived the close
    assert was_breached(row) is True


def test_breached_at_is_write_once_and_a_second_crossing_does_not_move_it(
    db, monkeypatch
):
    """The stamp records the FIRST crossing and is never re-written.

    Without write-once, the row's history would follow the deadline around: push
    the ETA out, breach again, and the stamp would report the second miss as if
    it were the only one — which is how "we were four days late" becomes "we were
    an hour late" in the same reporting the feature exists to produce.
    """
    _seed_sla(db, hours=SLA_SOW_STANDARD)
    _freeze_engine_clock(monkeypatch, _ct(2026, 8, 27, 9))
    row = open_commitment(
        db,
        kind="sow",
        owner_email=AE,
        deal_id=DEAL,
        opened_at=OPENED,
        source="call",
        evidence_ref="entry-12",
        sow_kind="standard",
    )
    recompute_states(db, now=_ct(2026, 8, 26, 17))
    first = row.breached_at
    assert _as_ct(first) == _ct_at(2026, 8, 26, 17)

    # ETA pushed out, then crossed a second time, with sweeps on both sides.
    row.promised_at = _ct(2026, 9, 18, 17)
    row.breach_at = compute_breach_at(row.opened_at, row.sla_hours, row.promised_at)
    db.flush()
    recompute_states(db, now=_ct(2026, 9, 10, 12))  # before the new deadline
    recompute_states(db, now=_ct(2026, 9, 21, 12))  # after it
    assert row.breached_at == first

    # And back-dating opened_at, which also recomputes breach_at, moves nothing.
    row.opened_at = _ct(2026, 8, 17, 9)
    row.breach_at = compute_breach_at(row.opened_at, row.sla_hours, None)
    db.flush()
    recompute_states(db, now=_ct(2026, 9, 21, 12))
    assert row.breached_at == first


def test_a_commitment_that_never_breached_has_a_null_breached_at(db, monkeypatch):
    """The stamp is absent, not "now", on a row that was delivered on time.

    A stamp that defaulted to something non-NULL would make every on-time
    commitment count as a miss, which is worse than the gap it replaced: the
    breach rate would read 100% and the cockpit's first review would be spent
    arguing about the number instead of the work.
    """
    _seed_sla(db, hours=SLA_SOW_STANDARD)
    _freeze_engine_clock(monkeypatch, _ct(2026, 8, 25, 9))
    row = open_commitment(
        db,
        kind="sow",
        owner_email=AE,
        deal_id=DEAL,
        opened_at=OPENED,
        source="call",
        evidence_ref="entry-12",
        sow_kind="standard",
    )
    # Tuesday: 0.7 * 24 business hours has not elapsed, let alone the SLA.
    recompute_states(db, now=_ct(2026, 8, 25, 9))
    assert row.state == "open"
    assert row.breached_at is None
    assert was_breached(row, now=_ct(2026, 8, 25, 9)) is False

    close_commitment(db, row, source="manual", actor=AE)
    assert row.breached_at is None
    assert was_breached(row) is False

    # An unknown age has no deadline, so it can never be stamped either.
    unknown_age = open_commitment(
        db,
        kind="sow",
        owner_email=AE,
        deal_id="9700",
        opened_at=None,
        source="stage_backfill",
        evidence_ref="stage-9700",
        sow_kind="standard",
    )
    recompute_states(db, now=_ct(2026, 9, 30, 12))
    assert unknown_age.breach_at is None
    assert unknown_age.breached_at is None
    assert was_breached(unknown_age, now=_ct(2026, 9, 30, 12)) is False


def test_closing_preserves_breached_at_and_a_late_close_stamps_it(db, monkeypatch):
    """Closing clears nothing, and a close before the first sweep still records.

    Two halves, both load-bearing. The stamp must survive the close — it is the
    only field left that says the row was late once ``state`` reads ``"closed"``.
    And a commitment that crossed its deadline minutes before the AE closed it is
    late even though the 15-minute sweep never saw it: stamping at close is what
    stops the reporting from depending on sweep timing.
    """
    _seed_sla(db, hours=SLA_SOW_STANDARD)
    _freeze_engine_clock(monkeypatch, _ct(2026, 8, 27, 9))
    swept = open_commitment(
        db,
        kind="sow",
        owner_email=AE,
        deal_id=DEAL,
        opened_at=OPENED,
        source="call",
        evidence_ref="entry-12",
        sow_kind="standard",
    )
    recompute_states(db, now=_ct(2026, 8, 26, 17))
    stamped = swept.breached_at
    assert stamped is not None

    close_commitment(db, swept, source="manual", actor=AE)
    assert swept.state == "closed"
    assert swept.breached_at == stamped  # the close cleared nothing
    assert was_breached(swept) is True

    # The second row breaches at Wed 17:00 and is closed at 17:05, before any
    # sweep runs. No stamp from the ladder — the close has to record it.
    unswept = open_commitment(
        db,
        kind="sow",
        owner_email=AE,
        deal_id="9800",
        opened_at=OPENED,
        source="manual",
        evidence_ref="SUP-9800",
        sow_kind="standard",
    )
    assert unswept.breached_at is None
    _freeze_engine_clock(monkeypatch, _ct(2026, 8, 26, 17, 5))
    close_commitment(db, unswept, source="manual", actor=AE)
    assert _as_ct(unswept.breached_at) == _ct_at(2026, 8, 26, 17)
    assert was_breached(unswept) is True


def test_an_accepted_or_provisional_row_is_never_stamped(db, monkeypatch):
    """The sweep's skips are also stamp skips, and for different reasons.

    ``provisional`` is an AI extraction nobody has confirmed: stamping it would
    put an unreviewed guess into the breach count, which is the credibility
    failure that makes managers stop reading the cockpit. ``accepted`` is
    terminal until close and was already stamped when it breached, so a second
    write there could only move a fact that is already recorded.
    """
    _seed_sla(db, hours=SLA_SOW_STANDARD)
    _freeze_engine_clock(monkeypatch, _ct(2026, 9, 30, 12))
    provisional = open_commitment(
        db,
        kind="sow",
        owner_email=AE,
        deal_id="9901",
        opened_at=OPENED,
        source="call",
        evidence_ref="entry-9901",
        sow_kind="standard",
        provisional=True,
    )
    accepted = open_commitment(
        db,
        kind="sow",
        owner_email=AE,
        deal_id="9902",
        opened_at=OPENED,
        source="manual",
        evidence_ref="SUP-9902",
        sow_kind="standard",
    )
    accepted.state = "accepted"
    accepted.accepted_reason = "Client pushed the kickoff; agreed with Marin."
    accepted.breached_at = None  # as if it were accepted before this column existed
    db.flush()

    # Long after every threshold. Both rows are skipped, and neither is stamped.
    buckets = recompute_states(db, now=_ct(2026, 9, 30, 12))
    assert buckets["skipped_provisional"] == 1
    assert buckets["skipped_accepted"] == 1
    assert provisional.breached_at is None
    assert accepted.breached_at is None

    # Nor does closing them stamp one — the same rule holds at that call site.
    close_commitment(db, provisional, source="superseded", evidence_ref="other")
    close_commitment(db, accepted, source="manual", actor=AE)
    assert provisional.breached_at is None
    assert accepted.breached_at is None

    # Confirming a provisional row is what arms the ladder, and THAT stamps: the
    # clock ran while it waited, so a confirmed commitment is late on arrival.
    late_on_arrival = open_commitment(
        db,
        kind="sow",
        owner_email=AE,
        deal_id="9903",
        opened_at=OPENED,
        source="call",
        evidence_ref="entry-9903",
        sow_kind="standard",
        provisional=True,
    )
    assert late_on_arrival.breached_at is None
    confirm(db, late_on_arrival)
    assert late_on_arrival.state == "exec_visible"
    assert _as_ct(late_on_arrival.breached_at) == _ct_at(2026, 8, 26, 17)


def test_recompute_states_returns_named_buckets_not_a_bare_count(db):
    """A scheduled endpoint reporting "42" cannot tell nothing-moved from all-skipped."""
    _seed_sla(db, hours=SLA_SOW_STANDARD)
    buckets = recompute_states(db, now=_ct(2026, 8, 26, 17))
    for key in (
        "scanned",
        "to_at_risk",
        "to_breached",
        "to_escalated",
        "to_exec_visible",
        "unchanged",
        "skipped_provisional",
        "skipped_accepted",
        "skipped_unknown_age",
        "skipped_off_ladder",
        "as_of",
    ):
        assert key in buckets
    assert buckets["scanned"] == 0


def test_a_closed_commitment_is_not_scanned_by_the_sweep(db):
    _seed_sla(db, hours=SLA_SOW_STANDARD)
    row = open_commitment(
        db,
        kind="sow",
        owner_email=AE,
        deal_id=DEAL,
        opened_at=OPENED,
        source="call",
        evidence_ref="entry-12",
        sow_kind="standard",
    )
    close_commitment(db, row, source="manual", actor=AE)
    assert recompute_states(db, now=_ct(2026, 9, 30, 12))["scanned"] == 0
    assert row.state == "closed"


# ══════════════════════════════════════════════════════════════════════════════
# The SLA snapshot
# ══════════════════════════════════════════════════════════════════════════════


def test_retuning_an_sla_config_does_not_touch_an_already_open_commitment(db):
    """``sla_hours`` is snapshotted at open and never re-read at display time.

    Get this wrong in production and there is one morning where every row turns
    red at once, and the cockpit never recovers its credibility. The retuned
    value must still apply to the NEXT commitment — the snapshot is the point,
    not indifference to the config.
    """
    config = _seed_sla(db, hours=SLA_SOW_STANDARD)
    row = open_commitment(
        db,
        kind="sow",
        owner_email=AE,
        deal_id=DEAL,
        opened_at=OPENED,
        source="call",
        evidence_ref="entry-12",
        sow_kind="standard",
    )
    original_breach = row.breach_at
    assert row.sla_hours == SLA_SOW_STANDARD

    # Marin retunes the SLA to one business hour — a value that, if it were read
    # at display time, would breach this row instantly.
    config.hours = 1
    db.flush()

    buckets = recompute_states(db, now=_ct(2026, 8, 24, 11))
    assert buckets["unchanged"] == 1
    assert row.state == "open"
    assert row.sla_hours == SLA_SOW_STANDARD
    assert row.breach_at == original_breach

    # The new number applies to the next commitment opened.
    fresh = open_commitment(
        db,
        kind="sow",
        owner_email=AE,
        deal_id="9500",
        opened_at=OPENED,
        source="manual",
        evidence_ref="SUP-9500",
        sow_kind="standard",
    )
    assert fresh.sla_hours == 1


def test_a_missing_sla_config_row_falls_back_to_the_spec_default(db):
    """An unseeded table must not break the pipeline at its last step."""
    row = open_commitment(
        db,
        kind="wrapup",
        owner_email=AE,
        deal_id=DEAL,
        opened_at=OPENED,
        source="call",
        evidence_ref="entry-12",
    )
    assert row.sla_hours == 8  # one business day
    assert _as_ct(row.breach_at) == _ct_at(2026, 8, 24, 17)


def test_an_unrecognised_sow_kind_is_normalized_to_null_not_raised(db):
    """One bad LLM payload must not lose the highest-value row in the feature."""
    _seed_sla(db, sow_kind="any", hours=24)
    row = open_commitment(
        db,
        kind="sow",
        owner_email=AE,
        deal_id=DEAL,
        opened_at=OPENED,
        source="call",
        evidence_ref="entry-12",
        sow_kind="bespoke-ish",
    )
    assert row.sow_kind is None
    assert row.sla_hours == 24  # fell through to the generic row


def test_the_escalation_ladder_constants_are_a_ladder():
    """``escalated`` must precede ``exec_visible`` or Marin's view ships empty."""
    assert ESCALATE_AFTER_BUSINESS_DAYS < EXEC_VISIBLE_AFTER_BUSINESS_DAYS
    assert 0 < AT_RISK_FRACTION < 1


# ══════════════════════════════════════════════════════════════════════════════
# close_commitment, and the fail-loud live-scope flags
# ══════════════════════════════════════════════════════════════════════════════


def test_close_commitment_accepts_only_manual_and_superseded(db):
    """No email close source, no stage close source. Gate 4's schema half."""
    _seed_sla(db, hours=SLA_SOW_STANDARD)
    row = open_commitment(
        db,
        kind="sow",
        owner_email=AE,
        deal_id=DEAL,
        opened_at=OPENED,
        source="call",
        evidence_ref="entry-12",
        sow_kind="standard",
    )
    for forbidden in ("email", "stage", "sent_mail", "auto", ""):
        with pytest.raises(ValueError, match="closed_source must be one of"):
            close_commitment(db, row, source=forbidden)
    assert row.closed_at is None

    close_commitment(db, row, source="manual", actor=AE)
    assert row.closed_source == "manual"
    closed_at = row.closed_at

    # Idempotent: a second close is a no-op, not a re-stamp.
    close_commitment(db, row, source="superseded")
    assert row.closed_at == closed_at
    assert row.closed_source == "manual"


def test_forgetting_refresh_live_scope_flags_raises_rather_than_duplicating(db):
    """The fail-loud design must actually fail loud.

    The two biconditional CHECKs make a forgotten ``refresh_live_scope_flags()``
    an ``IntegrityError`` at flush. The alternative — a NULL flag on a live row —
    would slip past both partial unique constraints and produce a duplicate live
    commitment nobody notices until an AE reports seeing the same obligation
    twice.
    """
    row = Commitment(
        id=uuid.uuid4().hex,
        kind="sow",
        owner_email=AE,
        hubspot_deal_id=DEAL,
        opened_at=OPENED,
        opened_source="manual",
        sla_hours=SLA_SOW_STANDARD,
        state="open",
    )
    db.add(row)
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()

    # The same row, with the one call made, flushes.
    good = Commitment(
        id=uuid.uuid4().hex,
        kind="sow",
        owner_email=AE,
        hubspot_deal_id=DEAL,
        opened_at=OPENED,
        opened_source="manual",
        sla_hours=SLA_SOW_STANDARD,
        state="open",
    )
    good.refresh_live_scope_flags()
    db.add(good)
    db.flush()
    assert good.is_live_deal_scoped is True

    # And the constraint it enables holds: a second live row on the same
    # (deal, kind) is rejected by the database, not by caller discipline.
    duplicate = Commitment(
        id=uuid.uuid4().hex,
        kind="sow",
        owner_email=OTHER_AE,
        hubspot_deal_id=DEAL,
        opened_at=OPENED,
        opened_source="manual",
        sla_hours=SLA_SOW_STANDARD,
        state="open",
    )
    duplicate.refresh_live_scope_flags()
    db.add(duplicate)
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_owner_email_is_lower_folded_on_write(db):
    """A commitment written with token casing appears in nobody's My Day."""
    _seed_sla(db, hours=SLA_SOW_STANDARD)
    row = open_commitment(
        db,
        kind="sow",
        owner_email="  AE@ACME.example ",
        deal_id=DEAL,
        opened_at=OPENED,
        source="call",
        evidence_ref="entry-12",
        sow_kind="standard",
    )
    assert row.owner_email == AE

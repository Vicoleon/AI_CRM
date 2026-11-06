"""Two different reports: nobody sent an SOW, and we never looked.

Regression cover for the SOW-sent detector's outcome statuses
(``rubik_core/cockpit/sow_sent_detector.py``) and the named buckets the sweep
built on them (``backend/jobs/sales_kaizen.py::run_sow_sent``).

**The defect these pin.** ``scan_sent_mail`` returned ``[]`` both when a
mailbox was genuinely quiet and when Gmail handed back no service at all. Revoke
the DWD grant (or rotate the Workspace key without a redeploy) and a sweep over
seven AEs holding live SOW commitments reported ``owners_scanned: 7,
owners_no_suggestions: 7, suggestions: 0, owners_failed: 0`` — which an operator
reads as a working detector over a quiet week, while all seven commitments keep
aging toward a breach that a working detector would have offered to close. Same
class of silent zero as a sweep reporting "0 calls found" when it was
refused 63 times.

**What is NOT asserted here, on purpose.** Nothing in this file claims the
detector works. That risk is still open — if SOWs are delivered *by* PandaDoc, no copy
passes through the AE's Sent folder and this lane finds nothing, forever, and
``status="ok"`` with zero suggestions is exactly what that world looks like too.
These tests only pin that *Cubie's own* failures stopped hiding inside that
ambiguity.

Offline: Gmail and HubSpot are both faked at their string targets in the
detector module, and the schema is built on SQLite in-memory.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.jobs import sales_kaizen
from rubik_core.cockpit import sow_sent_detector
from rubik_core.cockpit.business_hours import as_naive_utc
from rubik_core.cockpit.commitment_engine import open_commitment
from rubik_core.config import settings
from rubik_core.database.commitments import CommitmentSlaConfig
from rubik_core.database.models import Base

AE = "ae@acme.example"
OTHER_AE = "other@acme.example"
THIRD_AE = "third@acme.example"
FOURTH_AE = "fourth@acme.example"

CENTRAL = ZoneInfo("America/Chicago")


def _ct(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """A Central wall-clock instant as the NAIVE UTC the DB columns hold."""
    return as_naive_utc(datetime(year, month, day, hour, minute, tzinfo=CENTRAL))


def _epoch_ms(naive_utc: datetime) -> str:
    return str(int(naive_utc.replace(tzinfo=timezone.utc).timestamp() * 1000))


OPENED = _ct(2026, 8, 24, 9)  # Monday 09:00 CDT
SENT_AT = _ct(2026, 8, 28, 10)  # Friday, after the ask


@pytest.fixture
def db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,  # mandatory for :memory:
    )
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


@pytest.fixture(autouse=True)
def cockpit_on(monkeypatch):
    """``run_sow_sent``'s master switch. Off by default in config.py."""
    monkeypatch.setattr(settings, "COCKPIT_ENABLED", True)


def _seed_sla(db, *, hours: int = 24) -> None:
    db.add(
        CommitmentSlaConfig(
            id=uuid.uuid4().hex, kind="sow", sow_kind="standard", hours=hours
        )
    )
    db.flush()


def _live_sow(db, owner: str, *, deal: str, company: str):
    return open_commitment(
        db,
        kind="sow",
        owner_email=owner,
        company_id=company,
        deal_id=deal,
        opened_at=OPENED,
        source="call",
        evidence_ref=f"entry-{deal}",
        evidence_text="Parker: send us the SOW by end of week.",
        sow_kind="standard",
    )


class _FakeHubSpot:
    """Enough portal to make the contact lane score above the floor."""

    def __init__(self):
        self.read_client = self

    def get_object(self, object_type, object_id, properties=None):
        if object_type == "companies":
            return {"id": str(object_id), "properties": {"domain": "clientco.com"}}
        return {"id": str(object_id), "properties": {}}

    def search_all(self, object_type, filters=None, properties=None):
        if object_type == "contacts":
            return [{"id": "c1", "properties": {"email": "parker@clientco.com"}}]
        return []


def _sow_message(owner: str = AE, *, message_id: str = "msg-sow-1") -> dict:
    return {
        "id": message_id,
        "threadId": "thread-sow-1",
        "internalDate": _epoch_ms(SENT_AT),
        "payload": {
            "headers": [
                {"name": "From", "value": f"Acme AE <{owner}>"},
                {"name": "To", "value": "Parker Client <parker@clientco.com>"},
                {"name": "Subject", "value": "Scope of work for your review"},
            ],
            "parts": [{"filename": "Acme-SOW-ClientCo-v1.pdf"}],
        },
    }


class _Gmail:
    """A ``GmailClient`` stand-in with per-address, per-lane behaviour.

    ``behaviour[email]`` is one of:

    * ``"unavailable"`` — a ``None`` service, the revoked-grant shape;
    * ``"quiet"`` — both lanes answer, with nothing;
    * ``"found"`` — the attachment lane answers with a sent SOW;
    * ``"all_lanes_fail"`` — every ``search_threads`` call raises;
    * ``"pandadoc_lane_fails"`` — the attachment lane answers with a sent SOW
      and the PandaDoc lane 429s, i.e. the partial degradation.
    """

    behaviour: dict[str, str] = {}
    default = "quiet"
    constructed: list[str] = []

    def __init__(self, user_email: str | None = None, **_kw):
        self.user_email = user_email or ""
        type(self).constructed.append(self.user_email)
        mode = self.behaviour.get(self.user_email, self.default)
        self.mode = mode
        self.service = None if mode == "unavailable" else object()

    def search_threads(self, query: str = "", max_results: int = 20):
        is_attachment_lane = "has:attachment" in query
        if self.mode == "all_lanes_fail":
            raise RuntimeError("HttpError 429: rate limit exceeded")
        if self.mode == "pandadoc_lane_fails" and not is_attachment_lane:
            raise RuntimeError("HttpError 429: rate limit exceeded")
        if self.mode in ("found", "pandadoc_lane_fails") and is_attachment_lane:
            return [{"messages": [_sow_message(self.user_email)]}]
        return []


@pytest.fixture
def gmail(monkeypatch):
    """Install the fake Gmail + HubSpot clients on the detector module."""

    class _Scoped(_Gmail):
        behaviour: dict[str, str] = {}
        constructed: list[str] = []

    monkeypatch.setattr("rubik_core.cockpit.sow_sent_detector.GmailClient", _Scoped)
    monkeypatch.setattr(
        "rubik_core.cockpit.sow_sent_detector.HubSpotWriteClient", _FakeHubSpot
    )
    return _Scoped


# ══════════════════════════════════════════════════════════════════════════════
# The detector: five reasons the suggestion list is empty
# ══════════════════════════════════════════════════════════════════════════════


def test_unavailable_mailbox_is_not_a_quiet_one(db, gmail):
    """A ``None`` Gmail service reports its own status, not zero suggestions.

    THE defect. Both cases produce no suggestions; only one of them means the
    AE did not send an SOW. The commitment count travels with the status so a
    report can say how much aging went unwatched.
    """
    _seed_sla(db)
    _live_sow(db, AE, deal="8801", company="7701")
    gmail.behaviour[AE] = "unavailable"

    outcome = sow_sent_detector.scan_sent_mail_detailed(db, AE)

    assert outcome.status == sow_sent_detector.SCAN_MAILBOX_UNAVAILABLE
    assert outcome.suggestions == []
    assert outcome.live_commitments == 1
    assert outcome.lanes_searched == 0
    assert outcome.searched is False
    # And it does NOT collide with the honest empty below.
    assert outcome.status != sow_sent_detector.SCAN_OK


def test_a_quiet_mailbox_reports_ok_and_says_it_searched(db, gmail):
    """Both lanes answered with nothing: a product outcome, and it says so."""
    _seed_sla(db)
    _live_sow(db, AE, deal="8801", company="7701")
    gmail.behaviour[AE] = "quiet"

    outcome = sow_sent_detector.scan_sent_mail_detailed(db, AE)

    assert outcome.status == sow_sent_detector.SCAN_OK
    assert outcome.suggestions == []
    assert outcome.searched is True
    assert outcome.degraded is False
    assert (outcome.lanes_searched, outcome.lanes_failed) == (2, 0)


def test_every_lane_failing_is_its_own_status(db, gmail):
    """The mailbox opened and then answered nothing at all — still blind."""
    _seed_sla(db)
    _live_sow(db, AE, deal="8801", company="7701")
    gmail.behaviour[AE] = "all_lanes_fail"

    outcome = sow_sent_detector.scan_sent_mail_detailed(db, AE)

    assert outcome.status == sow_sent_detector.SCAN_LANES_FAILED
    assert outcome.suggestions == []
    assert outcome.searched is False
    assert (outcome.lanes_searched, outcome.lanes_failed) == (0, 2)
    assert outcome.live_commitments == 1


def test_one_lane_failing_narrows_the_search_visibly(db, gmail):
    """A partial degradation still returns its suggestions — and flags itself.

    The suggestion the surviving lane found is real. What is not trustworthy is
    an ABSENCE after a search that quietly lost half its lanes, so the outcome
    carries both the counters and a status of its own.
    """
    _seed_sla(db)
    _live_sow(db, AE, deal="8801", company="7701")
    gmail.behaviour[AE] = "pandadoc_lane_fails"

    outcome = sow_sent_detector.scan_sent_mail_detailed(db, AE)

    assert outcome.status == sow_sent_detector.SCAN_LANES_DEGRADED
    assert outcome.searched is True  # a partial search is still a search
    assert outcome.degraded is True
    assert (outcome.lanes_searched, outcome.lanes_failed) == (1, 1)
    assert len(outcome.suggestions) == 1
    assert outcome.suggestions[0]["message_id"] == "msg-sow-1"


def test_no_live_commitment_reports_no_commitments_and_opens_no_mailbox(db, gmail):
    """The precondition, kept: a read needs a reason that predates it."""
    _seed_sla(db)

    outcome = sow_sent_detector.scan_sent_mail_detailed(db, AE)

    assert outcome.status == sow_sent_detector.SCAN_NO_COMMITMENTS
    assert outcome.suggestions == []
    assert outcome.searched is False
    assert gmail.constructed == []  # Gmail was never even constructed


def test_every_status_is_declared(db, gmail):
    """``SCAN_STATUSES`` is what callers zero-fill their buckets from."""
    assert set(sow_sent_detector.SCAN_STATUSES) == {
        sow_sent_detector.SCAN_OK,
        sow_sent_detector.SCAN_NO_COMMITMENTS,
        sow_sent_detector.SCAN_MAILBOX_UNAVAILABLE,
        sow_sent_detector.SCAN_LANES_FAILED,
        sow_sent_detector.SCAN_LANES_DEGRADED,
    }


# ══════════════════════════════════════════════════════════════════════════════
# The list-shaped wrapper refuses to express an outage as an empty list
# ══════════════════════════════════════════════════════════════════════════════


def test_scan_sent_mail_raises_rather_than_returning_a_silent_empty(db, gmail):
    """A caller that only wants suggestions gets a failure, not a zero."""
    _seed_sla(db)
    _live_sow(db, AE, deal="8801", company="7701")

    gmail.behaviour[AE] = "unavailable"
    with pytest.raises(sow_sent_detector.MailboxUnavailable):
        sow_sent_detector.scan_sent_mail(db, AE)

    gmail.behaviour[AE] = "all_lanes_fail"
    with pytest.raises(sow_sent_detector.AllLanesFailed):
        sow_sent_detector.scan_sent_mail(db, AE)

    # Both are catchable as one thing by a cockpit row rendering "couldn't check".
    assert issubclass(
        sow_sent_detector.MailboxUnavailable, sow_sent_detector.SowSentScanError
    )
    assert issubclass(
        sow_sent_detector.AllLanesFailed, sow_sent_detector.SowSentScanError
    )


def test_scan_sent_mail_still_returns_a_plain_list_on_the_honest_paths(db, gmail):
    """The back-compat shape: quiet and found both stay list-shaped."""
    _seed_sla(db)
    _live_sow(db, AE, deal="8801", company="7701")

    gmail.behaviour[AE] = "quiet"
    assert sow_sent_detector.scan_sent_mail(db, AE) == []

    gmail.behaviour[AE] = "found"
    suggestions = sow_sent_detector.scan_sent_mail(db, AE)
    assert len(suggestions) == 1
    assert suggestions[0]["message_id"] == "msg-sow-1"
    # Never the subject, never a recipient, never a snippet.
    assert set(suggestions[0]) == {
        "commitment_id",
        "owner_email",
        "hubspot_deal_id",
        "hubspot_company_id",
        "message_id",
        "thread_id",
        "sent_at",
        "confidence",
        "match_reasons",
        "suggestion",
    }


# ══════════════════════════════════════════════════════════════════════════════
# The sweep's report — the buckets an operator actually reads
# ══════════════════════════════════════════════════════════════════════════════


def test_a_revoked_gmail_grant_does_not_report_a_quiet_week(db, gmail):
    """THE regression. Every mailbox refuses; the report must not say "quiet".

    Before the fix this returned ``owners_scanned: 3, owners_no_suggestions: 3,
    suggestions: 0, owners_failed: 0`` and an operator concluded the detector
    was working and nobody had sent anything, while three live SOW commitments
    kept aging toward a breach.
    """
    _seed_sla(db)
    for i, owner in enumerate((AE, OTHER_AE, THIRD_AE)):
        _live_sow(db, owner, deal=f"880{i}", company=f"770{i}")
    db.commit()
    gmail.default = "unavailable"

    report = sales_kaizen.run_sow_sent(db)

    assert report["owners"] == 3
    assert report["owners_mailbox_unavailable"] == 3
    # The two buckets that used to absorb this. Both must be zero.
    assert report["owners_scanned"] == 0
    assert report["owners_no_suggestions"] == 0
    assert report["suggestions"] == 0
    assert report["owners_failed"] == 0
    assert report["owners_lanes_failed"] == 0


def test_the_roster_partitions_across_the_outcome_buckets(db, gmail):
    """Four AEs, four different outcomes, and the buckets add up.

    ``owners`` = scanned + no_commitments + mailbox_unavailable + lanes_failed +
    failed, and ``owners_scanned`` = with_suggestions + no_suggestions.
    ``owners_lanes_degraded`` is a flag over the scanned set, not a sixth
    partition — the degraded AE is counted as scanned AND as degraded.
    """
    _seed_sla(db)
    for i, owner in enumerate((AE, OTHER_AE, THIRD_AE, FOURTH_AE)):
        _live_sow(db, owner, deal=f"880{i}", company=f"770{i}")
    db.commit()
    gmail.behaviour.update(
        {
            AE: "pandadoc_lane_fails",  # scanned, degraded, and it found one
            OTHER_AE: "quiet",  # scanned, genuinely nothing
            THIRD_AE: "unavailable",  # never looked
            FOURTH_AE: "all_lanes_fail",  # never looked, other cause
        }
    )

    report = sales_kaizen.run_sow_sent(db)

    assert report["owners"] == 4
    assert report["owners_scanned"] == 2
    assert report["owners_with_suggestions"] == 1
    assert report["owners_no_suggestions"] == 1
    assert report["owners_lanes_degraded"] == 1
    assert report["owners_mailbox_unavailable"] == 1
    assert report["owners_lanes_failed"] == 1
    assert report["owners_no_commitments"] == 0
    assert report["owners_failed"] == 0
    assert report["suggestions"] == 1
    # One lane lost by the degraded AE, both lanes lost by the failed one.
    assert report["lanes_failed"] == 3

    partition = (
        report["owners_scanned"]
        + report["owners_no_commitments"]
        + report["owners_mailbox_unavailable"]
        + report["owners_lanes_failed"]
        + report["owners_failed"]
    )
    assert partition == report["owners"]
    assert (
        report["owners_with_suggestions"] + report["owners_no_suggestions"]
        == report["owners_scanned"]
    )
    # Still a suggestion, still nothing written.
    assert report["persisted"] is False
    assert report["detail"] == [
        {
            "owner_email": AE,
            "commitment_id": report["detail"][0]["commitment_id"],
            "confidence": report["detail"][0]["confidence"],
            "match_reasons": report["detail"][0]["match_reasons"],
        }
    ]


def test_every_outcome_bucket_is_zero_filled_on_a_quiet_sweep(db, gmail):
    """A dict holding only what happened cannot say "nothing failed"."""
    _seed_sla(db)
    _live_sow(db, AE, deal="8801", company="7701")
    db.commit()
    gmail.default = "quiet"

    report = sales_kaizen.run_sow_sent(db)

    for bucket in (
        "owners_scanned",
        "owners_with_suggestions",
        "owners_no_suggestions",
        "owners_no_commitments",
        "owners_mailbox_unavailable",
        "owners_lanes_failed",
        "owners_lanes_degraded",
        "lanes_failed",
        "owners_failed",
    ):
        assert bucket in report, bucket
        assert isinstance(report[bucket], int), bucket
    assert report["owners_scanned"] == 1
    assert report["owners_no_suggestions"] == 1
    assert report["owners_mailbox_unavailable"] == 0
    assert report["owners_lanes_failed"] == 0
    assert report["owners_lanes_degraded"] == 0
    assert report["lanes_failed"] == 0


def test_a_targeted_rescan_of_an_ae_with_nothing_open_is_not_a_quiet_mailbox(db, gmail):
    """``--emails`` can name an AE the detector will refuse to read for."""
    _seed_sla(db)
    _live_sow(db, AE, deal="8801", company="7701")
    db.commit()

    report = sales_kaizen.run_sow_sent(db, owners=[OTHER_AE])

    assert report["owners"] == 1
    assert report["owners_no_commitments"] == 1
    assert report["owners_scanned"] == 0
    assert report["owners_no_suggestions"] == 0
    assert gmail.constructed == []


def test_an_exception_out_of_the_detector_still_lands_in_owners_failed(db, gmail):
    """The catch-all bucket survives: an unexpected raise is not "quiet" either."""
    _seed_sla(db)
    _live_sow(db, AE, deal="8801", company="7701")
    db.commit()

    def _boom(*_a, **_kw):
        raise RuntimeError("something nobody anticipated")

    import rubik_core.cockpit.sow_sent_detector as detector

    original = detector.scan_sent_mail_detailed
    detector.scan_sent_mail_detailed = _boom
    try:
        report = sales_kaizen.run_sow_sent(db)
    finally:
        detector.scan_sent_mail_detailed = original

    assert report["owners_failed"] == 1
    assert report["owners_scanned"] == 0
    assert report["owners_no_suggestions"] == 0


def test_a_dry_run_reads_no_mailbox_at_all(db, gmail):
    """The honest dry run: who WOULD be read, without reading anyone."""
    _seed_sla(db)
    _live_sow(db, AE, deal="8801", company="7701")
    db.commit()
    gmail.default = "found"

    report = sales_kaizen.run_sow_sent(db, dry_run=True)

    assert report["dry_run"] is True
    assert report["owners_would_scan"] == 1
    assert report["owners_scanned"] == 0
    assert report["suggestions"] == 0
    assert gmail.constructed == []

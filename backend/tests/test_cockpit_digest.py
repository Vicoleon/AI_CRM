"""The anti-tsunami rules, as failing tests rather than good intentions.

A notification system that floods its readers dies: nothing in the flood gets
addressed. So the digest rules (one digest per kind per day, a hard cap) are
the ones that decide whether this survives contact with a busy week. This
module is those rules (plus the nudge's three writes) made checkable.

What each block pins:

1. **Rule 1 — one digest of EACH KIND per person per local day.**
   Enforced by ``uq_digest_logs_recipient_kind_date``, not by a pre-check, and
   asserted at BOTH ends: the ``digest_logs`` rows and the Slack delivery
   boundary. The ``kind`` filter is load-bearing — on a Monday a person gets one
   daily AND one weekly, and without ``kind`` in the key they suppress each
   other depending on which scheduler job wins the race. That bug reproduces one
   day a week, non-deterministically, and reads as "sometimes I don't get my
   digest", so both firing orders are parametrized.
2. **``local_date`` is the RECIPIENT's local today**, via
   ``rubik_core.priorities.temporal``. Pinned with a timezone where the local
   date differs from BOTH the UTC date and the org default (America/Chicago), in
   both directions: a UTC day boundary inside one local day must still suppress,
   and a local day boundary inside one UTC day must not.
3. **Silence is a feature.** An empty item list sends nothing AND writes no row
   — a row would also burn the day's slot, so an 11am breach would wait for
   tomorrow.
4. **A failed send leaves the row ABSENT** so the next sweep retries, and
   ``sent_at`` stamps only after a confirmed send — the same claim-then-send
   ordering as ``eos_l10_sessions.summary_email_sent_at``
   (``test_eos_l10_summary_email.py::test_failed_send_leaves_it_retryable``).
   The ordering itself is observed from inside the fake ``send_dm``.
5. **Rule 2 — hard cap.** 10 qualifying items ⇒ 3 shown,
   ``suppressed_count`` 7, and the overflow stays a COUNT. A list of twenty is a
   list of zero.
6. **Digests link and never quote transcript text.** Asserted against the
   delivery payload and against the digest's own audit row, with evidence text
   present on the underlying commitment and stage lag. Client-confidential
   content does not belong in Slack.
7. **Named buckets, never a bare "not sent".** A ``send_dm`` returning ``None``
   for a non-workspace-member is a SKIP with its own reason and no row; a blind
   "Nudged" when nothing was delivered is how people stop trusting the tool.
8. **A nudge is THREE writes** (HubSpot task, Slack DM,
   ``commitment_nudges`` row) and the row lands even when Slack skips, because
   "nudged twice, still open" has to stay countable.
9. **Two unacknowledged nudges is an ESCALATION, not a third nudge** (rule 4).

No network anywhere: ``SlackBotClient`` and ``HubSpotWriteClient`` are faked at
their own module attributes (patch-by-string, per contract §8.4 — several suites
``importlib.reload()`` mid-run and an object-target patch goes stale). Schema is
built on a temp SQLite file, which is what CI does.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime

import pytest

from rubik_core.brand import PRODUCT_NAME
from rubik_core.config import settings

AE = "ae@acme.example"
OTHER_AE = "other@acme.example"
HARPER = "harper@acme.example"

# A client's words, from a call. The one string that must never reach Slack,
# HubSpot or an audit row.
EVIDENCE = "CLIENT SAID: send the SOW by Friday or we go with the other vendor"
LAG_EVIDENCE = "CLIENT SAID: we already signed, why is this still in drafting"

# UTC+14. Chosen so the recipient's local date differs from the UTC date AND
# from the org default (America/Chicago) at the instants used below — a test
# that only beat UTC would still pass if the code keyed on the org timezone.
FAR_EAST_TZ = "Pacific/Kiritimati"


# ── DB ───────────────────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path):
    """Temp SQLite file, not ``:memory:`` + StaticPool.

    ``user_today_str`` opens its OWN session through
    ``rubik_core.database.db.SessionLocal`` to read the recipient's saved
    timezone. With StaticPool both sessions share one DBAPI connection, so that
    lookup's ``close()`` would roll back the digest session's in-flight claim —
    the exact ordering under test here.
    """
    settings.DATABASE_URL = f"sqlite:///{tmp_path / 'test_cockpit_digest.db'}"
    import rubik_core.database.db as dbmod

    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database import commitments as _c  # noqa: F401 — register tables
    from rubik_core.database import eos  # noqa: F401 — register eos_* tables
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)

    session = dbmod.SessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
        dbmod._engine = None


@pytest.fixture(autouse=True)
def flags():
    prior = (settings.COCKPIT_ENABLED, settings.COCKPIT_DIGEST_ENABLED)
    settings.COCKPIT_ENABLED = True
    settings.COCKPIT_DIGEST_ENABLED = True
    yield
    settings.COCKPIT_ENABLED, settings.COCKPIT_DIGEST_ENABLED = prior


# ── The delivery boundary ────────────────────────────────────────────────────


class _SlackRecorder:
    """Every DM the code tried to send, and what Slack did about it.

    ``outcomes`` is a queue consumed one per ``send_dm`` call, falling back to
    ``default`` — the failure paths need the first attempt to differ from the
    retry.
    """

    def __init__(self):
        self.sends: list[dict] = []
        self.available = True
        self.default = "deliver"
        self.outcomes: list[str] = []
        self.on_send = None  # called mid-send, to observe the row's state

    @property
    def emails(self) -> list[str]:
        return [s["email"] for s in self.sends]

    def payload_blob(self, index: int = 0) -> str:
        """One send's text + blocks as a single searchable string."""
        send = self.sends[index]
        return f"{send['text'] or ''}\n{json.dumps(send['blocks'] or [])}"


@pytest.fixture
def slack(monkeypatch):
    from backend.services.slack_service import SlackAPIError

    rec = _SlackRecorder()

    class _FakeBot:
        def __init__(self, *_a, **_kw):
            pass

        @property
        def is_available(self) -> bool:
            return rec.available

        def send_dm(self, recipient_email, *, text=None, blocks=None):
            rec.sends.append({"email": recipient_email, "text": text, "blocks": blocks})
            if rec.on_send is not None:
                rec.on_send()
            outcome = rec.outcomes.pop(0) if rec.outcomes else rec.default
            if outcome == "skip":  # not a workspace member
                return None
            if outcome != "deliver":  # a Slack error code
                raise SlackAPIError(outcome)
            return {"ok": True, "ts": f"17{len(rec.sends)}.000100"}

    # Patch by STRING target: both modules do a lazy in-function import of
    # backend.services.slack_service, so the attribute is resolved at call time.
    monkeypatch.setattr("backend.services.slack_service.SlackBotClient", _FakeBot)
    return rec


class _HubSpotRecorder:
    def __init__(self):
        self.tasks: list[dict] = []
        self.error: Exception | None = None

    @property
    def properties(self) -> dict:
        return self.tasks[0]["properties"]


@pytest.fixture
def hubspot(monkeypatch):
    rec = _HubSpotRecorder()

    class _FakeWriteClient:
        def __init__(self, *_a, **_kw):
            pass

        def create_task(
            self,
            properties,
            *,
            owner_email,
            contact_id=None,
            company_id=None,
            deal_id=None,
        ):
            rec.tasks.append(
                {
                    "properties": properties,
                    "owner_email": owner_email,
                    "company_id": company_id,
                    "deal_id": deal_id,
                }
            )
            if rec.error is not None:
                raise rec.error
            return {"id": f"task-{len(rec.tasks)}", "_mode": "created"}

    monkeypatch.setattr("rubik_core.cockpit.nudge.HubSpotWriteClient", _FakeWriteClient)
    return rec


# ── Row builders ─────────────────────────────────────────────────────────────


def _commitment(
    db,
    *,
    kind: str = "sow",
    state: str = "breached",
    owner: str = AE,
    deal_id: str | None = None,
    company_id: str | None = None,
    opened_at: datetime | None = None,
    breach_at: datetime | None = None,
    sla_hours: int = 24,
    evidence: str | None = None,
    closed_at: datetime | None = None,
    escalated_to: str | None = None,
    accepted_reason: str | None = None,
):
    from rubik_core.database.commitments import Commitment

    row = Commitment(
        id=uuid.uuid4().hex,
        kind=kind,
        state=state,
        owner_email=owner,
        hubspot_deal_id=deal_id,
        hubspot_company_id=company_id,
        opened_at=opened_at or datetime(2026, 8, 20, 15, 0),
        opened_source="call",
        opened_evidence_ref="entry-1",
        opened_evidence_text=evidence,
        sla_hours=sla_hours,
        breach_at=breach_at,
        closed_at=closed_at,
        closed_source="manual" if closed_at else None,
        escalated_to=escalated_to,
        accepted_reason=accepted_reason,
    )
    # The CHECK constraints reject a live row whose projections weren't
    # refreshed — see Commitment.refresh_live_scope_flags.
    row.refresh_live_scope_flags()
    db.add(row)
    db.commit()
    return row


def _stage_lag(db, *, deal_id: str, evidence: str | None = None, status: str = "open"):
    from rubik_core.database.commitments import CommitmentStageLag

    row = CommitmentStageLag(
        id=uuid.uuid4().hex,
        hubspot_deal_id=deal_id,
        hubspot_stage_id="stage-drafting",
        evidence_stage_id="stage-sent",
        evidence_text=evidence,
        evidence_ref="entry-9",
        confidence=0.9,
        status=status,
        created_at=datetime(2026, 8, 26, 9, 0),
    )
    db.add(row)
    db.commit()
    return row


def _set_timezone(db, email: str, tz_name: str) -> None:
    from rubik_core.database.models import User, UserPreference

    db.add(User(email=email, id=email.split("@")[0], name="Test AE"))
    db.add(UserPreference(user_email=email, timezone=tz_name))
    db.commit()


def _digest_rows(db, **filters):
    from rubik_core.database.commitments import DigestLog

    q = db.query(DigestLog)
    for key, value in filters.items():
        q = q.filter(getattr(DigestLog, key) == value)
    return q.order_by(DigestLog.created_at, DigestLog.kind).all()


def _one_breached(db, *, deal_id: str = "D1", **kw):
    return _commitment(
        db,
        deal_id=deal_id,
        state="breached",
        breach_at=datetime(2026, 8, 25, 15, 0),
        **kw,
    )


NOW = datetime(2026, 8, 27, 14, 0)  # a Thursday, naive UTC like every DB stamp


# ── Rule 1 · one digest of each kind per person per local day ────────────────


def test_two_runs_of_the_same_kind_on_one_day_send_one_digest(db, slack):
    """RULE 1, at both ends: one Slack DM and exactly one digest_logs row."""
    from rubik_core.cockpit import digest

    _one_breached(db)

    first = digest.send_digest(db, AE, kind="daily", now=NOW)
    second = digest.send_digest(db, AE, kind="daily", now=NOW)

    assert first["sent"] is True
    assert first["reason"] is None
    assert second["sent"] is False
    assert second["reason"] == digest.REASON_ALREADY_SENT

    # The delivery boundary agrees with the table.
    assert slack.emails == [AE]
    rows = _digest_rows(db, recipient_email=AE, kind="daily")
    assert len(rows) == 1
    assert rows[0].sent_at is not None


def test_the_same_person_and_day_at_a_different_hour_still_only_gets_one(db, slack):
    """The guard is the day, not the minute — a re-run at 4pm changes nothing."""
    from rubik_core.cockpit import digest

    _one_breached(db)

    assert digest.send_digest(db, AE, kind="daily", now=NOW)["sent"] is True
    late = digest.send_digest(db, AE, kind="daily", now=NOW.replace(hour=22))
    assert late["reason"] == digest.REASON_ALREADY_SENT
    assert len(slack.sends) == 1


def test_case_variant_recipient_is_the_same_person(db, slack):
    """An unfolded address would make one human two people and send twice."""
    from rubik_core.cockpit import digest

    _one_breached(db)

    assert digest.send_digest(db, AE, kind="daily", now=NOW)["sent"] is True
    shouty = digest.send_digest(db, "AE@ACME.example", kind="daily", now=NOW)
    assert shouty["reason"] == digest.REASON_ALREADY_SENT
    assert len(slack.sends) == 1


@pytest.mark.parametrize("order", [("daily", "weekly"), ("weekly", "daily")])
def test_a_monday_daily_and_weekly_both_land_whichever_job_runs_first(db, slack, order):
    """THE KIND FILTER IS LOAD-BEARING.

    Without ``kind`` in the unique key these two suppress each other, and which
    one survives depends on which scheduler job happens to run first: a bug that
    reproduces one day a week, non-deterministically, and looks like "sometimes I
    don't get my digest". Both firing orders must deliver both.
    """
    from rubik_core.cockpit import digest

    _one_breached(db)

    results = [digest.send_digest(db, AE, kind=k, now=NOW) for k in order]

    assert [r["sent"] for r in results] == [True, True]
    assert [r["reason"] for r in results] == [None, None]
    assert len(slack.sends) == 2

    rows = _digest_rows(db, recipient_email=AE)
    assert len(rows) == 2
    assert {r.kind for r in rows} == {"daily", "weekly"}
    assert {r.local_date for r in rows} == {"2026-08-27"}  # one local day, two kinds
    assert all(r.sent_at is not None for r in rows)


def test_two_people_are_two_slots(db, slack):
    from rubik_core.cockpit import digest

    _one_breached(db, deal_id="D1")
    _one_breached(db, deal_id="D2", owner=OTHER_AE)

    assert digest.send_digest(db, AE, kind="daily", now=NOW)["sent"] is True
    assert digest.send_digest(db, OTHER_AE, kind="daily", now=NOW)["sent"] is True
    assert sorted(slack.emails) == [AE, OTHER_AE]
    assert len(_digest_rows(db)) == 2


def test_a_digest_only_carries_its_own_owners_work(db, slack):
    from rubik_core.cockpit import digest

    mine = _one_breached(db, deal_id="D1")
    theirs = _one_breached(db, deal_id="D2", owner=OTHER_AE)

    digest.send_digest(db, AE, kind="daily", now=NOW)

    shown_ids = [i["id"] for i in _digest_rows(db)[0].detail["shown"]]
    assert shown_ids == [mine.id]
    assert theirs.id not in shown_ids


# ── local_date is the RECIPIENT's local today, not UTC ───────────────────────


def test_local_date_is_the_viewers_local_today_not_utc(db, slack):
    """UTC+14 recipient at 23:30Z: their day is already TOMORROW.

    The instant is chosen so all three candidate answers differ — UTC says
    08-26, the org default (America/Chicago) says 08-26, the recipient says
    08-27 — so keying on either of the wrong two fails here.
    """
    from rubik_core.cockpit import digest

    _set_timezone(db, AE, FAR_EAST_TZ)
    _one_breached(db)

    now = datetime(2026, 8, 26, 23, 30)  # naive UTC
    result = digest.send_digest(db, AE, kind="daily", now=now)

    assert result["sent"] is True
    assert result["local_date"] == "2026-08-27"
    assert result["local_date"] != now.date().isoformat()  # not the UTC date
    assert _digest_rows(db)[0].local_date == "2026-08-27"


def test_a_utc_day_boundary_inside_one_local_day_still_suppresses(db, slack):
    """Same local day, two different UTC days ⇒ still ONE digest.

    If the guard keyed on UTC, the 09:00Z run would look like a new day and the
    recipient would get a second DM in one working day.
    """
    from rubik_core.cockpit import digest

    _set_timezone(db, AE, FAR_EAST_TZ)
    _one_breached(db)

    before = digest.send_digest(db, AE, kind="daily", now=datetime(2026, 8, 26, 23, 30))
    after = digest.send_digest(db, AE, kind="daily", now=datetime(2026, 8, 27, 9, 0))

    assert before["local_date"] == after["local_date"] == "2026-08-27"
    assert before["sent"] is True
    assert after["reason"] == digest.REASON_ALREADY_SENT
    assert len(slack.sends) == 1
    assert len(_digest_rows(db)) == 1


def test_a_local_day_boundary_inside_one_utc_day_is_a_new_slot(db, slack):
    """The converse: one UTC day, two LOCAL days ⇒ two digests, correctly.

    Both instants are 2026-08-26 in UTC. For a UTC+14 recipient the first is
    their evening and the second is the next morning, so the second digest is
    the next day's, not a duplicate.
    """
    from rubik_core.cockpit import digest

    _set_timezone(db, AE, FAR_EAST_TZ)
    _one_breached(db)

    evening = digest.send_digest(db, AE, kind="daily", now=datetime(2026, 8, 26, 9, 0))
    morning = digest.send_digest(db, AE, kind="daily", now=datetime(2026, 8, 26, 11, 0))

    assert evening["local_date"] == "2026-08-26"
    assert morning["local_date"] == "2026-08-27"
    assert evening["sent"] is True and morning["sent"] is True
    assert len(slack.sends) == 2
    assert {r.local_date for r in _digest_rows(db)} == {"2026-08-26", "2026-08-27"}


# ── Silence is a feature ─────────────────────────────────────────────────────


def test_an_empty_item_list_sends_nothing_and_writes_no_row(db, slack):
    """A quiet day must not produce an empty email — and must not burn the slot.

    A row here would also mean a commitment breaching at 11am waits until
    tomorrow to be mentioned.
    """
    from rubik_core.cockpit import digest

    result = digest.send_digest(db, AE, kind="daily", now=NOW)

    assert result["sent"] is False
    assert result["reason"] == digest.REASON_NOTHING_ACTIONABLE
    assert result["item_count"] == 0
    assert result["suppressed_count"] == 0
    assert slack.sends == []
    assert _digest_rows(db) == []


def test_a_quiet_morning_does_not_block_an_afternoon_breach(db, slack):
    """The empty run left no row, so the same day can still deliver later."""
    from rubik_core.cockpit import digest

    assert (
        digest.send_digest(db, AE, kind="daily", now=NOW)["reason"]
        == digest.REASON_NOTHING_ACTIONABLE
    )
    _one_breached(db)
    later = digest.send_digest(db, AE, kind="daily", now=NOW.replace(hour=16))
    assert later["sent"] is True
    assert len(_digest_rows(db)) == 1


def test_states_that_must_never_generate_a_notification(db, slack):
    """provisional (escalation disarmed), accepted (owned, with a reason), and
    closed rows are not actionable — so this is a quiet day."""
    from rubik_core.cockpit import digest

    _commitment(db, deal_id="D1", state="provisional")
    _commitment(
        db,
        deal_id="D2",
        state="accepted",
        accepted_reason="client pushed the deadline",
    )
    _commitment(
        db,
        deal_id="D3",
        state="breached",
        breach_at=datetime(2026, 8, 20, 9, 0),
        closed_at=datetime(2026, 8, 26, 9, 0),
    )
    _commitment(db, deal_id="D4", state="open", kind="sow")  # open sow is not yet due

    result = digest.send_digest(db, AE, kind="daily", now=NOW)
    assert result["reason"] == digest.REASON_NOTHING_ACTIONABLE
    assert slack.sends == []


# ── Exactly-once ordering: claim, send, THEN stamp ───────────────────────────


def test_the_slot_is_claimed_before_the_send_and_stamped_after(db, slack):
    """Mirrors ``eos_l10_sessions.summary_email_sent_at``.

    Observed from inside ``send_dm``: the row already exists (the slot is
    claimed, so a racing scheduler execution loses the INSERT) and ``sent_at``
    is still NULL, because a stamped row is a claim that somebody was told.
    """
    from rubik_core.cockpit import digest
    from rubik_core.database.commitments import DigestLog

    _one_breached(db)
    observed: list[tuple] = []

    def _peek_mid_send():
        observed.extend((r.kind, r.sent_at) for r in db.query(DigestLog).all())

    slack.on_send = _peek_mid_send

    digest.send_digest(db, AE, kind="daily", now=NOW)

    assert observed == [("daily", None)]  # claimed, not yet stamped
    assert _digest_rows(db)[0].sent_at == NOW  # stamped only after delivery


def test_a_failed_send_leaves_no_row_so_the_next_sweep_retries(db, slack):
    from rubik_core.cockpit import digest

    _one_breached(db)
    slack.default = "channel_not_found"

    failed = digest.send_digest(db, AE, kind="daily", now=NOW)

    assert failed["sent"] is False
    assert failed["reason"] == digest.REASON_SEND_FAILED
    assert failed["error"] == "channel_not_found"
    assert _digest_rows(db) == []  # ⇒ the next sweep retries

    slack.default = "deliver"
    retried = digest.send_digest(db, AE, kind="daily", now=NOW)
    assert retried["sent"] is True
    rows = _digest_rows(db)
    assert len(rows) == 1
    assert rows[0].sent_at is not None


def test_invalid_blocks_degrades_to_text_rather_than_dropping_the_person(db, slack):
    """Slack posts NOTHING when it rejects a block set — not even the fallback."""
    from rubik_core.cockpit import digest

    _one_breached(db)
    slack.outcomes = ["invalid_blocks", "deliver"]

    result = digest.send_digest(db, AE, kind="daily", now=NOW)

    assert result["sent"] is True
    assert len(slack.sends) == 2
    assert slack.sends[1]["blocks"] is None  # text-only retry
    assert _digest_rows(db)[0].sent_at is not None


def test_a_text_only_retry_that_also_fails_leaves_no_row(db, slack):
    from rubik_core.cockpit import digest

    _one_breached(db)
    slack.outcomes = ["invalid_blocks", "invalid_auth"]

    result = digest.send_digest(db, AE, kind="daily", now=NOW)

    assert result["sent"] is False
    assert result["reason"] == digest.REASON_SEND_FAILED
    assert result["error"] == "invalid_auth"
    assert _digest_rows(db) == []


# ── Rule 2 · the cap, and the overflow is a count ────────────────────────────


def test_ten_qualifying_items_show_three_and_suppress_seven(db, slack):
    """RULE 2. A list of twenty is a list of zero."""
    from rubik_core.cockpit import digest

    assert digest.MAX_INAPP_ITEMS == 3  # settings.COCKPIT_MAX_INAPP_ITEMS, at import
    for n in range(10):
        _commitment(
            db,
            deal_id=f"D{n}",
            state="breached",
            breach_at=datetime(2026, 8, 10 + n, 9, 0),
        )

    result = digest.send_digest(db, AE, kind="daily", now=NOW)

    assert result["sent"] is True
    assert result["item_count"] == 3
    assert result["suppressed_count"] == 7

    row = _digest_rows(db)[0]
    assert row.item_count == 3
    assert row.suppressed_count == 7
    # The audit row names WHAT was held back, not just how much — half the
    # answer to "I never got told".
    assert len(row.detail["shown"]) == 3
    assert len(row.detail["suppressed"]) == 7


def test_gather_actionable_is_uncapped_so_the_overflow_count_is_knowable(db):
    from rubik_core.cockpit import digest

    for n in range(10):
        _commitment(
            db,
            deal_id=f"D{n}",
            state="breached",
            breach_at=datetime(2026, 8, 10 + n, 9, 0),
        )

    assert len(digest.gather_actionable(db, AE)) == 10


def test_the_overflow_stays_a_count_never_an_expander(db, slack):
    """A chevron that expands inline defeats the cap. Three sections, one count."""
    from rubik_core.cockpit import digest

    for n in range(10):
        _commitment(
            db,
            deal_id=f"D{n}",
            state="breached",
            breach_at=datetime(2026, 8, 10 + n, 9, 0),
        )

    digest.send_digest(db, AE, kind="daily", now=NOW)

    blocks = slack.sends[0]["blocks"]
    assert [b["type"] for b in blocks].count("section") == 3
    context = [b for b in blocks if b["type"] == "context"]
    assert len(context) == 1
    assert context[0]["elements"][0]["text"] == f"+7 more in {PRODUCT_NAME}"
    assert f"+7 more in {PRODUCT_NAME}" in slack.sends[0]["text"]


def test_no_overflow_count_when_nothing_was_suppressed(db, slack):
    from rubik_core.cockpit import digest

    _one_breached(db)
    digest.send_digest(db, AE, kind="daily", now=NOW)

    assert f"more in {PRODUCT_NAME}" not in slack.payload_blob()


def test_every_shown_row_carries_exactly_one_action_label(db, slack):
    """Rule 3's wording, identical in Slack and on screen."""
    from rubik_core.cockpit import digest

    _one_breached(db)
    items = digest.gather_actionable(db, AE)

    assert items[0]["action_label"] == "Mark the SOW sent, or set an ETA with a reason"
    digest.send_digest(db, AE, kind="daily", now=NOW)
    assert items[0]["action_label"] in slack.payload_blob()


# ── Ordering ─────────────────────────────────────────────────────────────────


def test_ordering_is_oldest_breach_then_at_risk_by_age_then_wrapups(db):
    """Pipeline hygiene last: a stage-lag exception is a correction
    to a forecast, not a promise to a client that is running late."""
    from rubik_core.cockpit import digest

    newer_breach = _commitment(
        db, deal_id="D2", state="breached", breach_at=datetime(2026, 8, 24, 9, 0)
    )
    oldest_breach = _commitment(
        db, deal_id="D1", state="breached", breach_at=datetime(2026, 8, 20, 9, 0)
    )
    younger_risk = _commitment(
        db,
        deal_id="D4",
        state="at_risk",
        opened_at=datetime(2026, 8, 22, 9, 0),
        breach_at=datetime(2026, 8, 28, 9, 0),
    )
    older_risk = _commitment(
        db,
        deal_id="D3",
        state="at_risk",
        opened_at=datetime(2026, 8, 10, 9, 0),
        breach_at=datetime(2026, 8, 27, 9, 0),
    )
    wrapup = _commitment(
        db,
        deal_id="D1",
        kind="wrapup",
        state="open",
        opened_at=datetime(2026, 8, 25, 9, 0),
    )
    lag = _stage_lag(db, deal_id="D1")

    items = digest.gather_actionable(db, AE)

    assert [i["id"] for i in items] == [
        oldest_breach.id,
        newer_breach.id,
        older_risk.id,
        younger_risk.id,
        wrapup.id,
        lag.id,
    ]
    assert [i["bucket"] for i in items] == [
        "breached",
        "breached",
        "at_risk",
        "at_risk",
        "wrapup",
        "stage_lag",
    ]


def test_an_unknown_clock_never_outranks_a_real_breach(db):
    """A NULL breach_at sorts LAST inside its bucket, not first."""
    from rubik_core.cockpit import digest

    dated = _commitment(
        db, deal_id="D1", state="breached", breach_at=datetime(2026, 8, 25, 9, 0)
    )
    undated = _commitment(db, deal_id="D2", state="breached", breach_at=None)

    items = digest.gather_actionable(db, AE)
    assert [i["id"] for i in items] == [dated.id, undated.id]
    assert items[1]["detail"] == "overdue since date unknown"


def test_a_stage_lag_with_no_local_commitment_reaches_nobodys_digest(db):
    """Better absent from a DM than sent to the wrong person: with no live Cubie
    commitment on the deal, the lag is not attributable to an AE locally."""
    from rubik_core.cockpit import digest

    _commitment(db, deal_id="D1", state="breached", breach_at=NOW)
    orphan = _stage_lag(db, deal_id="D-UNKNOWN")

    assert orphan.id not in {i["id"] for i in digest.gather_actionable(db, AE)}


def test_a_resolved_stage_lag_is_not_re_raised(db):
    from rubik_core.cockpit import digest

    _commitment(db, deal_id="D1", state="breached", breach_at=NOW)
    applied = _stage_lag(db, deal_id="D1", status="applied")

    assert applied.id not in {i["id"] for i in digest.gather_actionable(db, AE)}


# ── Rule 4 · escalation moves accountability, it does not add volume ─────────


def test_an_escalated_row_stays_in_its_owners_digest_and_names_the_new_owner(db, slack):
    from rubik_core.cockpit import digest

    _commitment(
        db,
        deal_id="D1",
        state="escalated",
        breach_at=datetime(2026, 8, 20, 9, 0),
        escalated_to=HARPER,
    )

    result = digest.send_digest(db, AE, kind="daily", now=NOW)

    assert result["sent"] is True
    assert slack.emails == [AE]  # the AE learns it moved — no second alert
    assert f"with {HARPER}" in slack.payload_blob()


def test_an_exec_visible_row_still_appears_for_the_ae(db, slack):
    from rubik_core.cockpit import digest

    _commitment(
        db,
        deal_id="D1",
        state="exec_visible",
        breach_at=datetime(2026, 8, 15, 9, 0),
    )

    assert digest.send_digest(db, AE, kind="daily", now=NOW)["sent"] is True
    assert "now in the exec count" in slack.payload_blob()


# ── Confidentiality · digests LINK, they never quote ────────────────────────


def test_no_transcript_text_reaches_the_delivery_payload(db, slack):
    """Client-confidential content does not belong in Slack or email.

    Both underlying rows carry the quote that makes the cockpit card
    persuasive; neither quote may travel.
    """
    from rubik_core.cockpit import digest

    _commitment(
        db,
        deal_id="D1",
        state="breached",
        breach_at=datetime(2026, 8, 25, 9, 0),
        evidence=EVIDENCE,
    )
    _stage_lag(db, deal_id="D1", evidence=LAG_EVIDENCE)

    result = digest.send_digest(db, AE, kind="daily", now=NOW)
    assert result["item_count"] == 2

    payload = slack.payload_blob()
    assert EVIDENCE not in payload
    assert LAG_EVIDENCE not in payload
    assert "CLIENT SAID" not in payload
    # And the digest's own audit trail holds ids and buckets only.
    detail = json.dumps(_digest_rows(db)[0].detail)
    assert "CLIENT SAID" not in detail
    assert all(
        set(entry) == {"id", "source", "bucket"}
        for entry in _digest_rows(db)[0].detail["shown"]
    )


def test_gather_actionable_never_returns_evidence_text(db):
    from rubik_core.cockpit import digest

    _commitment(
        db,
        deal_id="D1",
        state="breached",
        breach_at=datetime(2026, 8, 25, 9, 0),
        evidence=EVIDENCE,
    )
    _stage_lag(db, deal_id="D1", evidence=LAG_EVIDENCE)

    blob = json.dumps(digest.gather_actionable(db, AE))
    assert "CLIENT SAID" not in blob
    assert "evidence_text" not in blob
    assert "opened_evidence_text" not in blob


def test_the_digest_links_into_the_app_instead_of_quoting(db, slack):
    from rubik_core.cockpit import digest

    _one_breached(db)
    digest.send_digest(db, AE, kind="daily", now=NOW)

    buttons = [
        e
        for b in slack.sends[0]["blocks"]
        if b["type"] == "actions"
        for e in b["elements"]
    ]
    assert len(buttons) == 1
    assert buttons[0]["url"].endswith("/sales")


# ── Named buckets · a skip is not a send, and not a failure ─────────────────


def test_a_non_member_is_a_named_skip_not_a_send_and_leaves_no_row(db, slack):
    """A blind "Nudged" when nothing was delivered is how people stop trusting
    the tool — and counting this as a failure sends ops chasing an outage that
    is really one roster row."""
    from rubik_core.cockpit import digest

    _one_breached(db)
    slack.default = "skip"  # send_dm returns None

    result = digest.send_digest(db, AE, kind="daily", now=NOW)

    assert result["sent"] is False
    assert result["reason"] == digest.REASON_NOT_A_MEMBER
    assert result["reason"] != digest.REASON_SEND_FAILED
    assert result["item_count"] == 1
    assert _digest_rows(db) == []  # nothing was delivered, so nothing is claimed


def test_a_missing_workspace_bot_is_its_own_bucket_for_the_whole_roster(db, slack):
    from rubik_core.cockpit import digest

    _one_breached(db)
    slack.available = False

    result = digest.send_digest(db, AE, kind="daily", now=NOW)

    assert result["sent"] is False
    assert result["reason"] == digest.REASON_NO_SLACK_BOT
    assert slack.sends == []
    assert _digest_rows(db) == []


def test_every_outcome_reason_is_a_named_constant(db, slack):
    """No bare booleans: a sweep reporting "12 sent, 4 not" cannot tell a quiet
    day from four people who are not in the Slack workspace."""
    from rubik_core.cockpit import digest

    named = {
        digest.REASON_COCKPIT_DISABLED,
        digest.REASON_DIGEST_DISABLED,
        digest.REASON_NOTHING_ACTIONABLE,
        digest.REASON_ALREADY_SENT,
        digest.REASON_DRY_RUN,
        digest.REASON_NO_SLACK_BOT,
        digest.REASON_NOT_A_MEMBER,
        digest.REASON_SEND_FAILED,
    }
    assert len(named) == 8  # eight distinguishable outcomes, no rolled-up "errors"

    quiet = digest.send_digest(db, AE, kind="daily", now=NOW)
    assert quiet["reason"] in named
    assert set(quiet) >= {"recipient", "kind", "local_date", "sent", "reason"}


# ── Kill switches ────────────────────────────────────────────────────────────


def test_digest_disabled_sends_nothing_at_all(db, slack):
    from rubik_core.cockpit import digest

    _one_breached(db)
    settings.COCKPIT_DIGEST_ENABLED = False

    result = digest.send_digest(db, AE, kind="daily", now=NOW)

    assert result["sent"] is False
    assert result["reason"] == digest.REASON_DIGEST_DISABLED
    assert slack.sends == []
    assert _digest_rows(db) == []


def test_cockpit_disabled_sends_nothing_at_all(db, slack):
    """A DM linking into a cockpit whose router 404s is worse than no DM."""
    from rubik_core.cockpit import digest

    _one_breached(db)
    settings.COCKPIT_ENABLED = False

    result = digest.send_digest(db, AE, kind="daily", now=NOW)

    assert result["reason"] == digest.REASON_COCKPIT_DISABLED
    assert slack.sends == []
    assert _digest_rows(db) == []


def test_dry_run_renders_without_writing_or_sending_even_while_disabled(db, slack):
    """Inspecting the output is how anyone decides whether to flip the flag."""
    from rubik_core.cockpit import digest

    _one_breached(db)
    settings.COCKPIT_ENABLED = False
    settings.COCKPIT_DIGEST_ENABLED = False

    result = digest.send_digest(db, AE, kind="daily", now=NOW, dry_run=True)

    assert result["sent"] is False
    assert result["reason"] == digest.REASON_DRY_RUN
    assert result["item_count"] == 1
    assert result["preview_text"]
    assert slack.sends == []
    assert _digest_rows(db) == []


def test_an_unknown_digest_kind_is_a_caller_error(db):
    from rubik_core.cockpit import digest

    with pytest.raises(ValueError, match="not a digest kind"):
        digest.send_digest(db, AE, kind="hourly", now=NOW)


def test_a_missing_recipient_is_a_caller_error(db):
    from rubik_core.cockpit import digest

    with pytest.raises(ValueError, match="requires a recipient email"):
        digest.send_digest(db, "  ", kind="daily", now=NOW)


def test_the_weekly_digest_says_it_is_the_weekly_one(db, slack):
    from rubik_core.cockpit import digest

    _one_breached(db)
    digest.send_digest(db, AE, kind="weekly", now=NOW)

    assert slack.sends[0]["blocks"][0]["text"]["text"] == "Sales aging — this week"


# ── a nudge is THREE writes ─────────────────────────────────────────────────


def _nudge(db, commitment, **kw):
    from rubik_core.cockpit import nudge as nudge_mod

    return nudge_mod.nudge(
        db, commitment, from_email=kw.pop("from_email", HARPER), to_email=AE, **kw
    )


def _nudge_rows(db, commitment_id: str):
    from rubik_core.database.commitments import CommitmentNudge

    return (
        db.query(CommitmentNudge)
        .filter(CommitmentNudge.commitment_id == commitment_id)
        .all()
    )


def test_a_nudge_writes_a_hubspot_task_a_slack_dm_and_a_row(db, slack, hubspot):
    """All three limbs of a nudge, in order: task, DM, row."""
    from rubik_core.cockpit import nudge as nudge_mod

    row = _one_breached(db, deal_id="D1")
    result = _nudge(db, row)

    # 1 — the HubSpot task, on the AE, associated to the deal.
    assert len(hubspot.tasks) == 1
    task = hubspot.tasks[0]
    assert task["owner_email"] == AE
    assert task["deal_id"] == "D1"
    assert task["company_id"] is None
    assert task["properties"]["hs_task_status"] == "NOT_STARTED"
    assert task["properties"]["hs_task_priority"] == "HIGH"
    assert task["properties"]["hs_timestamp"]  # the DUE date, required

    # 2 — the Slack DM.
    assert slack.emails == [AE]
    assert f"Open {PRODUCT_NAME}" in slack.payload_blob()

    # 3 — the row, stamped and committed.
    rows = _nudge_rows(db, row.id)
    assert len(rows) == 1
    assert rows[0].channel == "both"
    assert rows[0].hubspot_task_id == "task-1"
    assert rows[0].sent_at is not None
    assert rows[0].from_email == HARPER
    assert rows[0].to_email == AE

    assert result["sent"] is True
    assert result["slack"] == nudge_mod.REASON_SLACK_DELIVERED
    assert result["unacknowledged_nudges"] == 1


def test_the_nudge_row_is_written_even_when_slack_skips(db, slack, hubspot):
    """The task landed, so the nudge is real and must stay countable."""
    from rubik_core.cockpit import nudge as nudge_mod

    row = _one_breached(db, deal_id="D1")
    slack.default = "skip"  # AE is not a Slack workspace member

    result = _nudge(db, row)

    assert result["sent"] is True
    assert result["slack"] == nudge_mod.REASON_SLACK_NOT_A_MEMBER
    assert result["channel"] == "hubspot_task"
    rows = _nudge_rows(db, row.id)
    assert len(rows) == 1
    assert rows[0].channel == "hubspot_task"
    assert rows[0].sent_at is not None


def test_a_slack_failure_still_leaves_the_nudge_on_the_record(db, slack, hubspot):
    from rubik_core.cockpit import nudge as nudge_mod

    row = _one_breached(db, deal_id="D1")
    slack.default = "invalid_auth"

    result = _nudge(db, row)

    assert result["sent"] is True
    assert result["slack"] == nudge_mod.REASON_SLACK_FAILED
    assert result["slack_error"] == "invalid_auth"
    assert len(_nudge_rows(db, row.id)) == 1


def test_a_hubspot_failure_aborts_the_whole_nudge(db, slack, hubspot):
    """Nobody gets a DM about a task that does not exist, and no row claims one."""
    from rubik_core.retrieval.hubspot_write import HubSpotWriteRefused

    row = _one_breached(db, deal_id="D1")
    hubspot.error = HubSpotWriteRefused(
        "Cannot create a task: ae@acme.example is not a HubSpot owner"
    )

    with pytest.raises(ValueError, match="not a HubSpot owner"):
        _nudge(db, row)

    assert slack.sends == []
    assert _nudge_rows(db, row.id) == []


def test_a_nudge_never_quotes_transcript_text(db, slack, hubspot):
    """Harper does not get transcript text at all, and HubSpot is
    no more the right home for a client's words than Slack is."""
    row = _one_breached(db, deal_id="D1", evidence=EVIDENCE)

    _nudge(db, row)

    assert "CLIENT SAID" not in json.dumps(hubspot.properties)
    assert "CLIENT SAID" not in slack.payload_blob()


def test_a_nudge_task_is_due_in_the_future_not_already_overdue(db, slack, hubspot):
    """An overdue task lands in the pile the AE is already ignoring, and reads as
    an accusation rather than a reminder."""
    from rubik_core.retrieval.hubspot_write import _epoch_ms

    row = _one_breached(db, deal_id="D1")  # breached on 25 Aug — already late
    _nudge(db, row)

    assert hubspot.properties["hs_timestamp"] > _epoch_ms(datetime.utcnow())


def test_a_nudge_before_the_breach_is_due_on_the_real_deadline(db, slack, hubspot):
    from rubik_core.retrieval.hubspot_write import _epoch_ms

    future = datetime.utcnow().replace(microsecond=0).replace(year=2027)
    row = _commitment(db, deal_id="D1", state="at_risk", breach_at=future)
    _nudge(db, row)

    assert hubspot.properties["hs_timestamp"] == _epoch_ms(future)


def test_a_nudge_needs_a_crm_record_to_attach_to(db, slack, hubspot):
    """A HubSpot task attached to nothing is invisible in the AE's context."""
    from rubik_core.cockpit import nudge as nudge_mod

    row = _commitment(db, state="breached", breach_at=NOW)  # no deal, no company

    result = _nudge(db, row)

    assert result["sent"] is False
    assert result["reason"] == nudge_mod.REASON_NO_CRM_RECORD
    assert hubspot.tasks == []
    assert slack.sends == []
    assert _nudge_rows(db, row.id) == []


def test_a_company_scoped_nudge_associates_to_the_company(db, slack, hubspot):
    row = _commitment(db, company_id="C1", state="breached", breach_at=NOW)
    _nudge(db, row)

    assert hubspot.tasks[0]["company_id"] == "C1"
    assert hubspot.tasks[0]["deal_id"] is None


def test_a_closed_commitment_is_not_nudgeable(db, slack, hubspot):
    from rubik_core.cockpit import nudge as nudge_mod

    row = _commitment(
        db,
        deal_id="D1",
        state="breached",
        breach_at=NOW,
        closed_at=datetime(2026, 8, 26, 9, 0),
    )

    result = _nudge(db, row)

    assert result["reason"] == nudge_mod.REASON_ALREADY_CLOSED
    assert hubspot.tasks == []
    assert slack.sends == []


def test_a_nudge_needs_both_addresses(db, slack, hubspot):
    from rubik_core.cockpit import nudge as nudge_mod

    row = _one_breached(db, deal_id="D1")
    with pytest.raises(ValueError, match="both from_email and to_email"):
        nudge_mod.nudge(db, row, from_email="", to_email=AE)


def test_a_nudge_is_not_gated_on_the_digest_flag(db, slack, hubspot):
    """That flag holds back scheduled VOLUME. A nudge is one person pressing one
    button about one row they are looking at."""
    row = _one_breached(db, deal_id="D1")
    settings.COCKPIT_DIGEST_ENABLED = False

    assert _nudge(db, row)["sent"] is True
    assert len(_nudge_rows(db, row.id)) == 1


# ── Rule 4 · two nudges with no acknowledgement is an ESCALATION ─────────────


def test_a_third_nudge_is_an_escalation_not_another_send(db, slack, hubspot):
    """A third DM to the same person about the same late SOW is how the tsunami
    starts — and it is an admission that nudging is not working."""
    from rubik_core.cockpit import nudge as nudge_mod

    row = _one_breached(db, deal_id="D1", escalated_to=HARPER)

    assert _nudge(db, row)["sent"] is True
    assert _nudge(db, row)["sent"] is True
    assert len(hubspot.tasks) == 2
    assert len(slack.sends) == 2

    third = _nudge(db, row)

    assert third["sent"] is False
    assert third["reason"] == nudge_mod.REASON_ESCALATION_REQUIRED
    assert third["unacknowledged_nudges"] == nudge_mod.MAX_UNACKNOWLEDGED_NUDGES
    assert third["escalated_to"] == HARPER
    # Nothing new was sent and nothing new was recorded.
    assert len(hubspot.tasks) == 2
    assert len(slack.sends) == 2
    assert len(_nudge_rows(db, row.id)) == 2


def test_an_acknowledged_nudge_does_not_count_against_the_threshold(db, slack, hubspot):
    """Two nudges into the void is a different situation from two somebody read."""
    row = _one_breached(db, deal_id="D1")

    _nudge(db, row)
    _nudge(db, row)
    for nudge_row in _nudge_rows(db, row.id):
        nudge_row.acknowledged_at = datetime(2026, 8, 27, 8, 0)
    db.commit()

    third = _nudge(db, row)
    assert third["sent"] is True
    assert len(_nudge_rows(db, row.id)) == 3


def test_an_unsent_nudge_row_does_not_silence_the_worklist(db, slack, hubspot):
    """Counting a failed attempt would block a real nudge on nothing."""
    from rubik_core.database.commitments import CommitmentNudge

    row = _one_breached(db, deal_id="D1")
    for _ in range(2):
        db.add(
            CommitmentNudge(
                id=uuid.uuid4().hex,
                commitment_id=row.id,
                from_email=HARPER,
                to_email=AE,
                channel="hubspot_task",
                sent_at=None,  # never reached anybody
            )
        )
    db.commit()

    assert _nudge(db, row)["sent"] is True


def test_the_threshold_is_per_commitment(db, slack, hubspot):
    row_a = _one_breached(db, deal_id="D1")
    row_b = _one_breached(db, deal_id="D2")

    _nudge(db, row_a)
    _nudge(db, row_a)
    assert _nudge(db, row_b)["sent"] is True

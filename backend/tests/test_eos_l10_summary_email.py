"""Post-meeting L10 recap email — scope, recording gate, and who receives it.

The rules this pins, in the order the feature decides them:

1. Only meetings whose TITLE carries "L10" are in scope.
2. **No recording, no email.** An unmatched — or unreadable — transcript is
   treated the same as no transcript at all.
3. Recipients are the people the TRANSCRIPT places in the room, not the invite
   list: an invitee who never joined is dropped even though the transcript's
   ACL (``meeting_participants``) grants them read access.
4. Those people are then filtered through the L10 meeting's OWN access rule, so
   a non-exec who attended the EXEC L10 gets nothing — the same fence that
   hides its notes in the app.
5. The send is exactly-once: ``summary_email_sent_at`` stamps on success only,
   so a failure retries and a success never re-sends.

No network: the mail transport is faked via ``set_transport_factory``, the GCS
read is monkeypatched, and the LLM call is stubbed. Temp SQLite DB per test.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from rubik_core.config import settings

MEETING_START = datetime(2026, 8, 19, 16, 0)
MEETING_END = datetime(2026, 8, 19, 17, 30)

# Long enough to clear MIN_TRANSCRIPT_CHARS, like any real 90-minute L10 —
# a short stub would now (correctly) be rejected as an empty recording.
TRANSCRIPT_TEXT = """Systems L10 - 2026-08-19 - Notes by Gemini

Attendees: Blair Ellison, Jordan Hayes

Blair Ellison: Let's start with the scorecard. Two measurables are red this week,
    both on the intake side, and I want to understand whether that's a volume
    problem or a staffing one before we call it an issue.
Jordan Hayes: It's volume. We took on the two new client onboardings at the
    same time and the queue never drained. I'll take the vendor follow-up this
    week and get us back to a normal baseline.
Blair Ellison: Good. Let's put a to-do on that rather than carrying it as an issue.
Jordan Hayes: Agreed. On rocks, the migration rock is on track — the last
    milestone landed Friday and there's nothing blocking the next one.
Blair Ellison: Then the only thing left is the onboarding backlog. We solved the
    onboarding backlog issue by moving the intake step ahead of the review, so
    that comes off the list.
Jordan Hayes: Nothing else from me. Ten out of ten on the meeting.
Blair Ellison: Same. Short one this week, which is how it should be.
"""


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, set_test_workspace_domains, monkeypatch):
    db_path = tmp_path / "test_l10_summary_email.db"
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    import rubik_core.database.db as dbmod

    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database import eos  # noqa: F401 — register EOS tables
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)

    from rubik_core.company.operations import patch_operations_config

    patch_operations_config({"l10_start_after": "2026-08-01"})
    monkeypatch.setattr(
        "backend.services.l10_summary_email.workspace_time_zone",
        lambda: ZoneInfo("America/Chicago"),
    )
    prior = (
        settings.CUBIE_L10_SUMMARY_EMAIL_ENABLED,
        settings.CUBIE_L10_SUMMARY_EMAIL_AUTOSEND,
    )
    settings.CUBIE_L10_SUMMARY_EMAIL_ENABLED = True
    settings.CUBIE_L10_SUMMARY_EMAIL_AUTOSEND = True
    set_test_workspace_domains("acme.example")
    yield
    (
        settings.CUBIE_L10_SUMMARY_EMAIL_ENABLED,
        settings.CUBIE_L10_SUMMARY_EMAIL_AUTOSEND,
    ) = prior


def _set_floor(value):
    from rubik_core.company.operations import patch_operations_config

    patch_operations_config({"l10_start_after": value})


class _CapturingTransport:
    """Stands in for Gmail: records every spec instead of sending it."""

    def __init__(self):
        self.sent: list = []

    def send(self, *, sender, impersonate, spec):
        self.sent.append({"sender": sender, "impersonate": impersonate, "spec": spec})
        return f"msg-{len(self.sent)}"


@pytest.fixture
def transport():
    from rubik_core.tools import email_sender

    captured = _CapturingTransport()
    email_sender.set_transport_factory(lambda: captured)
    yield captured
    email_sender.reset_transport_factory()


@pytest.fixture(autouse=True)
def stub_llm_and_gcs(monkeypatch):
    """No Vertex call, no GCS read — the transcript text is injected."""
    import backend.services.l10_summary_email as svc

    monkeypatch.setattr(
        svc,
        "generate_summary",
        lambda **kw: (
            "## Summary\nThe team cleared the onboarding backlog.\n"
            "## New to-dos\n- Vendor follow-up (Jordan Hayes)"
        ),
    )
    # The digest-only path has its OWN generator and its own prompt. Stubbing
    # only the recorded one leaves this reaching Vertex for real in any test
    # that falls through to the digest.
    monkeypatch.setattr(
        svc,
        "generate_digest_summary",
        lambda **kw: (
            "## Summary\nThe team cleared the onboarding backlog.\n"
            "## New to-dos\n- Vendor follow-up (Jordan Hayes)"
        ),
    )
    monkeypatch.setattr(
        svc, "read_transcript_text", lambda *a, **kw: TRANSCRIPT_TEXT, raising=True
    )


def _session():
    from rubik_core.database.db import SessionLocal

    return SessionLocal()


def _seed(
    *,
    meeting_name: str = "Systems L10",
    is_exec_meeting: bool = False,
    department: str | None = "Systems",
    roster: list[str] | None = None,
    transcript_title: str | None = "Systems L10 - 2026-08-19 - Notes by Gemini",
    transcript_participants: list[str] | None = None,
    attendee_is_exec: bool = False,
    actual_start: datetime = MEETING_START,
    actual_end: datetime = MEETING_END,
    transcript_ingested: datetime | None = None,
    attended_emails: list[str] | None = None,
) -> dict:
    """One finished L10 + one ingested transcript covering it.

    ``bellison@`` hosts and speaks; ``jhayes@`` speaks; ``absent@`` is on the
    transcript's ACL (i.e. was invited) but never appears in its text.
    """
    from rubik_core.database.eos import L10Meeting, L10Session, Organization
    from rubik_core.database.models import (
        Asset,
        Department,
        MeetingParticipant,
        User,
    )

    db = _session()
    try:
        db.add(Organization(id=uuid.uuid4(), name="Acme"))
        if department:
            db.add(Department(name=department))
        db.add_all(
            [
                User(
                    id="u-bellison",
                    email="bellison@acme.example",
                    name="Blair Ellison",
                    department=department or "general",
                    is_exec=True,
                ),
                User(
                    id="u-jhayes",
                    email="jhayes@acme.example",
                    name="Jordan Hayes",
                    department=department or "general",
                    is_exec=attendee_is_exec,
                ),
                User(
                    id="u-absent",
                    email="absent@acme.example",
                    name="Absent Person",
                    department=department or "general",
                    is_exec=True,
                ),
            ]
        )

        meeting = L10Meeting(
            id=uuid.uuid4(),
            name=meeting_name,
            department_name=None if is_exec_meeting else department,
            is_exec_meeting=is_exec_meeting,
            duration_minutes=90,
            roster_emails=roster,
        )
        db.add(meeting)
        session_row = L10Session(
            id=uuid.uuid4(),
            meeting_id=meeting.id,
            actual_start=actual_start,
            actual_end=actual_end,
            notes="Cleared the onboarding backlog.",
            average_rating=8,
        )
        db.add(session_row)

        if transcript_title is not None:
            doc_id = "doc-transcript-1"
            db.add(
                Asset(
                    doc_id=doc_id,
                    owner_email="bellison@acme.example",
                    topic="Meeting Transcripts",
                    status="approved",
                    original_filename=transcript_title,
                    source_path="gs://bucket/cubie/doc-transcript-1.txt",
                    created_at=transcript_ingested
                    or (MEETING_END + timedelta(minutes=20)),
                    updated_at=transcript_ingested
                    or (MEETING_END + timedelta(minutes=20)),
                )
            )
            participants = transcript_participants or [
                "bellison@acme.example",
                "jhayes@acme.example",
                "absent@acme.example",
            ]
            for email in participants:
                db.add(
                    MeetingParticipant(
                        doc_id=doc_id,
                        participant_email=email,
                        meeting_id="drive-file-1",
                    )
                )
        # Attendance rows are the digest path's PRIMARY audience source —
        # written in the app by the Check-in form and the Conclude survey.
        if attended_emails:
            from rubik_core.database.eos import L10Attendance, Person

            for email in attended_emails:
                person = Person(
                    id=uuid.uuid4(),
                    first_name=email.split("@")[0],
                    last_name="Tester",
                    email=email,
                )
                db.add(person)
                db.flush()
                db.add(
                    L10Attendance(
                        id=uuid.uuid4(),
                        session_id=session_row.id,
                        person_id=person.id,
                        attended=True,
                        rating=9,
                    )
                )

        db.commit()
        return {"meeting_id": meeting.id, "session_id": session_row.id}
    finally:
        db.close()


def _sweep(**kwargs):
    from backend.services.l10_summary_email import sweep_l10_summary_emails

    db = _session()
    try:
        return sweep_l10_summary_emails(
            db, now=MEETING_END + timedelta(hours=1), **kwargs
        )
    finally:
        db.close()


def _only(report):
    live = [o for o in report.outcomes if o.status != "not_l10"]
    assert len(live) == 1, [o.status for o in report.outcomes]
    return live[0]


# ── Scope: the title must say L10 ───────────────────────────────────────────


def test_non_l10_meeting_is_never_mailed(transport):
    _seed(meeting_name="Systems Weekly Sync", transcript_title="Systems Weekly Sync")
    report = _sweep()
    assert [o.status for o in report.outcomes] == ["not_l10"]
    assert transport.sent == []


def test_l10_spelling_variants_are_in_scope():
    from rubik_core.eos.l10_recording import is_l10_title

    assert is_l10_title("Systems L10")
    assert is_l10_title("exec l-10 meeting")
    assert is_l10_title("Leadership L 10")
    # Not the word L10 — and deliberately so: the rule is the literal token.
    assert not is_l10_title("Level 10 Meeting")
    assert not is_l10_title("HL105 review")
    assert not is_l10_title("")


# ── The recording gate ──────────────────────────────────────────────────────


def test_unrecorded_meeting_gets_no_email(transport):
    _seed(transcript_title=None)
    outcome = _only(_sweep())
    assert outcome.status == "not_recorded"
    assert transport.sent == []


def test_another_meetings_transcript_is_not_claimed(transport):
    """A transcript in the same window whose title is a different team's L10."""
    _seed(meeting_name="Systems L10", transcript_title="Support L10 - 2026-08-19")
    outcome = _only(_sweep())
    assert outcome.status == "not_recorded"
    assert transport.sent == []


def test_unreadable_transcript_is_treated_as_unrecorded(transport, monkeypatch):
    import backend.services.l10_summary_email as svc

    _seed()
    monkeypatch.setattr(svc, "read_transcript_text", lambda *a, **kw: "")
    outcome = _only(_sweep())
    assert outcome.status == "not_recorded"
    assert transport.sent == []


def test_transcript_with_no_shared_attendee_is_rejected(transport):
    """Title matches, window matches — but nobody on the meeting was in it."""
    _seed(
        roster=["bellison@acme.example", "jhayes@acme.example"],
        transcript_participants=["someone.else@acme.example"],
    )
    outcome = _only(_sweep())
    assert outcome.status == "not_recorded"
    assert transport.sent == []


# ── Recipients: present in the recording, and entitled to the meeting ───────


def test_only_people_present_in_the_transcript_are_mailed(transport):
    _seed()
    outcome = _only(_sweep())
    assert outcome.status == "sent"
    assert outcome.recipients == [
        "bellison@acme.example",
        "jhayes@acme.example",
    ]
    # On the transcript's ACL (invited) but never in its text → no email.
    assert "absent@acme.example" not in outcome.recipients
    assert "not present in the recording" in outcome.excluded["absent@acme.example"]
    assert len(transport.sent) == 1
    assert set(transport.sent[0]["spec"].to) == set(outcome.recipients)


def test_exec_l10_recap_reaches_exec_members_only(transport):
    """The Exec fence, applied to the mail exactly as it is to the screens."""
    _seed(
        meeting_name="Exec Team L10",
        transcript_title="Exec Team L10 - 2026-08-19 - Notes by Gemini",
        is_exec_meeting=True,
        department=None,
        attendee_is_exec=False,  # Jordan attended but is not on the Exec team
    )
    outcome = _only(_sweep())
    assert outcome.status == "sent"
    assert outcome.recipients == ["bellison@acme.example"]
    assert (
        outcome.excluded["jhayes@acme.example"] == "no access to this L10 (meeting ACL)"
    )
    assert transport.sent[0]["spec"].to == ["bellison@acme.example"]


def test_exec_attendee_of_exec_l10_is_mailed(transport):
    _seed(
        meeting_name="Exec Team L10",
        transcript_title="Exec Team L10 - 2026-08-19 - Notes by Gemini",
        is_exec_meeting=True,
        department=None,
        attendee_is_exec=True,
    )
    outcome = _only(_sweep())
    assert outcome.status == "sent"
    assert outcome.recipients == [
        "bellison@acme.example",
        "jhayes@acme.example",
    ]


def test_roster_scoped_meeting_drops_an_uninvited_attendee(transport):
    """Calendar-imported ⇒ the invite list is the ACL, for mail as for reads."""
    _seed(roster=["bellison@acme.example"])
    outcome = _only(_sweep())
    assert outcome.status == "sent"
    assert outcome.recipients == ["bellison@acme.example"]
    assert (
        outcome.excluded["jhayes@acme.example"] == "no access to this L10 (meeting ACL)"
    )


def test_external_participant_is_never_mailed(transport):
    _seed(
        transcript_participants=[
            "bellison@acme.example",
            "guest@clientco.com",
        ]
    )
    outcome = _only(_sweep())
    assert outcome.status == "sent"
    assert outcome.recipients == ["bellison@acme.example"]
    assert outcome.excluded["guest@clientco.com"] == "outside the company domain"


def test_no_entitled_attendee_means_no_email(transport):
    """Everyone present is filtered out by the ACL ⇒ no mail, and no stamp."""
    ids = _seed(
        roster=["nobody@acme.example"],
        transcript_participants=[
            "bellison@acme.example",
            "jhayes@acme.example",
            "nobody@acme.example",
        ],
    )
    outcome = _only(_sweep())
    assert outcome.status == "no_recipients"
    assert transport.sent == []
    # Both attendees were in the room; neither is on the invite list, which for
    # a calendar-imported meeting IS the ACL.
    assert set(outcome.excluded) >= {
        "bellison@acme.example",
        "jhayes@acme.example",
    }

    from rubik_core.database.eos import L10Session

    db = _session()
    try:
        row = db.query(L10Session).filter(L10Session.id == ids["session_id"]).first()
        assert row.summary_email_sent_at is None
    finally:
        db.close()


# ── Exactly-once ────────────────────────────────────────────────────────────


def test_second_sweep_does_not_resend(transport):
    _seed()
    assert _only(_sweep()).status == "sent"
    assert _only(_sweep()).status == "already_sent"
    assert len(transport.sent) == 1


def test_send_records_recording_and_recipients(transport):
    ids = _seed()
    _sweep()
    from rubik_core.database.eos import L10Session

    db = _session()
    try:
        row = db.query(L10Session).filter(L10Session.id == ids["session_id"]).first()
        assert row.summary_email_sent_at is not None
        assert row.summary_email_doc_id == "doc-transcript-1"
        assert row.summary_email_recipients == [
            "bellison@acme.example",
            "jhayes@acme.example",
        ]
    finally:
        db.close()


def test_failed_send_leaves_it_retryable(transport, monkeypatch):
    import backend.services.l10_summary_email as svc

    ids = _seed()

    def _boom(**kwargs):
        raise RuntimeError("vertex exploded")

    monkeypatch.setattr(svc, "generate_summary", _boom)
    outcome = _only(_sweep())
    assert outcome.status == "failed"
    assert transport.sent == []

    from rubik_core.database.eos import L10Session

    db = _session()
    try:
        row = db.query(L10Session).filter(L10Session.id == ids["session_id"]).first()
        assert row.summary_email_sent_at is None  # ⇒ the next sweep retries
    finally:
        db.close()

    # Re-set rather than monkeypatch.undo(): undo() would also revert the
    # autouse fixture's transcript/LLM stubs and put a real GCS read back.
    monkeypatch.setattr(svc, "generate_summary", lambda **kw: "## Summary\nFine now.")
    assert _only(_sweep()).status == "sent"


# ── Kill switches ───────────────────────────────────────────────────────────


def test_master_switch_off_does_nothing(transport):
    _seed()
    settings.CUBIE_L10_SUMMARY_EMAIL_ENABLED = False
    report = _sweep()
    assert report.enabled is False
    assert report.outcomes == []
    assert transport.sent == []


def test_autosend_off_skips_before_generating(transport, monkeypatch):
    """A dark rollout must not burn tokens on mail that never leaves."""
    import backend.services.l10_summary_email as svc

    _seed()
    settings.CUBIE_L10_SUMMARY_EMAIL_AUTOSEND = False
    calls: list = []
    monkeypatch.setattr(
        svc, "generate_summary", lambda **kw: calls.append(kw) or "unused"
    )
    outcome = _only(_sweep())
    assert outcome.status == "disabled"
    assert calls == []
    assert transport.sent == []


def test_dry_run_resolves_recipients_without_sending(transport, monkeypatch):
    import backend.services.l10_summary_email as svc

    ids = _seed()
    calls: list = []
    monkeypatch.setattr(
        svc, "generate_summary", lambda **kw: calls.append(kw) or "unused"
    )
    outcome = _only(_sweep(dry_run=True))
    assert outcome.status == "would_send"
    assert outcome.recipients == [
        "bellison@acme.example",
        "jhayes@acme.example",
    ]
    assert calls == []
    assert transport.sent == []
    from rubik_core.database.eos import L10Session

    with _session() as db:
        row = db.get(L10Session, ids["session_id"])
        assert row.summary_email_sent_at is None


# ── The email body ──────────────────────────────────────────────────────────


def test_body_escapes_user_authored_text():
    from backend.services.l10_summary_email import render_html

    html_body = render_html(
        meeting_name='Systems L10 <img src=x onerror="alert(1)">',
        when="Wednesday, August 19",
        summary="## Summary\n- <script>alert(2)</script> shipped",
        recording_title="notes & things",
    )
    # Escaped into text, so nothing is a tag or an attribute any more.
    assert "<script>" not in html_body
    assert "<img" not in html_body
    assert '="alert(1)"' not in html_body
    assert "&lt;script&gt;" in html_body
    assert "&lt;img" in html_body
    assert "notes &amp; things" in html_body


def test_body_renders_headings_and_bullets():
    from backend.services.l10_summary_email import render_html

    html_body = render_html(
        meeting_name="Systems L10",
        when="Wednesday, August 19",
        summary="## Summary\nWe cleared it.\n\n## New to-dos\n- Vendor follow-up",
        recording_title="Systems L10",
    )
    assert "<ul" in html_body and "<li" in html_body
    assert "Vendor follow-up" in html_body
    assert "We cleared it." in html_body


# ── The scheduler endpoint ──────────────────────────────────────────────────


def test_scheduler_endpoint_requires_the_key():
    from fastapi.testclient import TestClient

    from backend.main import app

    client = TestClient(app)
    assert client.post("/api/agents/eos/l10/summary-emails").status_code == 403


def test_scheduler_endpoint_dry_run_reports_recipients(transport, monkeypatch):
    from fastapi.testclient import TestClient

    import backend.services.l10_summary_email as svc
    from backend.main import app

    _seed()

    # The endpoint cannot be handed a `now`, so the sweep reads the real clock;
    # freeze it to the instant every _sweep() here pins, or the fixed
    # 2026-08-19 session ages out of the 168 h window a week after that date.
    class _FrozenNow(datetime):
        @classmethod
        def utcnow(cls):
            return MEETING_END + timedelta(hours=1)

    monkeypatch.setattr(svc, "datetime", _FrozenNow)

    prior_key = settings.SCHEDULER_API_KEY
    settings.SCHEDULER_API_KEY = "test-scheduler-key"
    try:
        client = TestClient(app)
        resp = client.post(
            "/api/agents/eos/l10/summary-emails?dry_run=true&hours=168",
            headers={
                "X-Scheduler-Key": "test-scheduler-key",
                "X-Cubie-Workspace": "test",
            },
        )
    finally:
        settings.SCHEDULER_API_KEY = prior_key

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enabled"] is True
    assert body["dry_run"] is True
    assert body["would_send"] == 1
    assert body["sent"] == 0
    assert body["sessions"][0]["recipients"] == [
        "bellison@acme.example",
        "jhayes@acme.example",
    ]
    # Non-L10 meetings are not reported — that list would be every other
    # meeting in the window.
    assert all(row["status"] != "not_l10" for row in body["sessions"])
    assert transport.sent == []


def test_exec_l10_wont_claim_a_transcript_with_no_exec_attendee(transport):
    """The Exec L10 has no roster and no department, so the Exec TEAM is its
    attendance gate — otherwise the one meeting whose contents must never reach
    the wrong people would have no gate at all."""
    _seed(
        meeting_name="Exec Team L10",
        transcript_title="Exec Team L10 - 2026-08-19 - Notes by Gemini",
        is_exec_meeting=True,
        department=None,
        attendee_is_exec=False,
        # Only the non-exec attendee is on the recording, so nobody on the Exec
        # team was in it — this is not the Exec L10's transcript.
        transcript_participants=["jhayes@acme.example"],
    )
    outcome = _only(_sweep())
    assert outcome.status == "not_recorded"
    assert transport.sent == []


# ── The go-live floor ───────────────────────────────────────────────────────


def test_meeting_before_the_start_date_is_never_mailed(transport):
    """ "We start next Monday" has to mean it: without this floor the first
    sweep after the scheduler job is created mails last week's meetings."""
    _seed()  # the session ends 2026-08-19
    _set_floor("2026-08-24")
    outcome = _only(_sweep())
    assert outcome.status == "before_start"
    assert transport.sent == []
    # Nothing was even looked up — no recording match, no recipients resolved.
    assert outcome.doc_id is None
    assert outcome.recipients == []


def test_meeting_on_or_after_the_start_date_is_mailed(transport):
    _seed()
    _set_floor("2026-08-19")
    assert _only(_sweep()).status == "sent"
    assert len(transport.sent) == 1


def test_empty_start_date_blocks_live_send(transport):
    _seed()
    _set_floor(None)
    outcome = _only(_sweep())
    assert outcome.status == "before_start"
    assert "go-live date" in outcome.error
    assert transport.sent == []


def test_dry_run_reports_missing_go_live_floor_clearly(transport):
    _seed()
    _set_floor(None)
    report = _sweep(dry_run=True)
    outcome = _only(report)
    assert report.dry_run is True
    assert outcome.status == "before_start"
    assert "go-live date" in outcome.error
    assert transport.sent == []


def test_unavailable_workspace_zone_blocks_live_send(transport, monkeypatch):
    _seed()

    def no_zone():
        raise RuntimeError("control DB unavailable")

    monkeypatch.setattr(
        "backend.services.l10_summary_email.workspace_time_zone", no_zone
    )
    outcome = _only(_sweep())
    assert outcome.status == "before_start"
    assert transport.sent == []


def test_foreign_configured_sender_refuses_before_summary_generation(
    transport, monkeypatch
):
    from rubik_core.company.operations import patch_operations_config

    _seed()
    patch_operations_config({"l10_sender_mailbox": "foreign@bravo.example"})
    monkeypatch.setattr(
        "backend.services.l10_summary_email.generate_summary",
        lambda **_kw: pytest.fail("foreign sender must be rejected before LLM"),
    )
    outcome = _only(_sweep())
    assert outcome.status == "failed"
    assert transport.sent == []


def test_unparseable_start_date_fails_closed(transport):
    """A typo'd go-live date must not be read as "mail everything"."""
    _seed()
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.models import WorkspaceSetting

    with SessionLocal() as db:
        row = db.get(WorkspaceSetting, "workspace_operations")
        row.value = {**row.value, "l10_start_after": "next monday"}
        db.commit()
    outcome = _only(_sweep())
    assert outcome.status == "before_start"
    assert transport.sent == []


def test_start_floor_reads_a_bare_date_in_the_org_timezone():
    """A bare date is the start of that day LOCALLY, not in UTC — otherwise a
    late-afternoon meeting on the go-live day falls on the wrong side."""
    from backend.services.l10_summary_email import start_floor

    _set_floor("2026-08-24")
    floor = start_floor()
    # America/Chicago is UTC-5 in August, so local midnight is 05:00 UTC.
    assert floor == datetime(2026, 8, 24, 5, 0)


def test_start_floor_accepts_an_explicit_offset():
    from backend.services.l10_summary_email import start_floor

    _set_floor("2026-08-24T00:00:00+00:00")
    assert start_floor() == datetime(2026, 8, 24, 0, 0)


# ── Regressions found by a dry run against dev ──────────────────────────────


def test_abandoned_session_window_cannot_reach_another_occurrence(transport):
    """An auto-closed session's ``actual_end`` drifts for days — dev held rows
    spanning 141 h to 676 h. The match window must be anchored on
    ``actual_start`` + the box, or a session that ended 08-17 matches the
    recording of the 08-10 occurrence (which is exactly what happened)."""
    _seed(
        meeting_name="L10 Meeting",
        roster=["bellison@acme.example", "jhayes@acme.example"],
        transcript_title="L10 Meeting - 2026-08-19 - Notes by Gemini",
        # Started a week before it was auto-closed, like dev's Sales L10.
        actual_start=datetime(2026, 8, 12, 19, 6),
        actual_end=datetime(2026, 8, 19, 20, 10),
        # Ingested just after the LATER occurrence, days outside this session's
        # own 90-minute box.
        transcript_ingested=datetime(2026, 8, 19, 20, 46),
    )
    outcome = _only(_sweep())
    # The invariant under test is that this session must never CLAIM the other
    # occurrence's recording: no doc_id, and nothing written from that
    # transcript. It is a week past its own recording window, so it now falls
    # through to the digest-only recap rather than staying silent — that path
    # reads the session's EOS entries and never opens a transcript at all.
    assert outcome.doc_id is None
    assert outcome.digest_only is True
    assert outcome.status == "sent_digest_only"
    assert "was not recorded" in transport.sent[0]["spec"].text_body


def test_window_still_matches_the_session_it_belongs_to(transport):
    """The same tight window must still catch a normal same-day recording,
    including one published hours late."""
    _seed(transcript_ingested=MEETING_END + timedelta(hours=20))
    assert _only(_sweep()).status == "sent"


def test_bom_only_recording_counts_as_unrecorded(transport, monkeypatch):
    """Google publishes an EMPTY notes doc as a bare 3-byte UTF-8 BOM. U+FEFF is
    not whitespace to ``str.strip()``, so it used to read as one "non-blank"
    character and get a recap invented from nothing."""
    import backend.services.l10_summary_email as svc

    _seed()
    monkeypatch.setattr(svc, "read_transcript_text", lambda *a, **kw: "\ufeff")
    outcome = _only(_sweep())
    assert outcome.status == "not_recorded"
    assert "no usable text" in (outcome.error or "")
    assert transport.sent == []


def test_short_recording_counts_as_unrecorded(transport, monkeypatch):
    import backend.services.l10_summary_email as svc

    _seed()
    monkeypatch.setattr(svc, "read_transcript_text", lambda *a, **kw: "Hi all.")
    assert _only(_sweep()).status == "not_recorded"
    assert transport.sent == []


def test_read_transcript_text_strips_the_bom(monkeypatch):
    from rubik_core.config import settings
    from rubik_core.eos.l10_recording import read_transcript_text

    monkeypatch.setattr(settings, "GCS_BUCKET_NAME", "b")

    class _Blob:
        def download_as_bytes(self, start=0, end=None):
            return b"\xef\xbb\xbfHello"

    class _Bucket:
        def blob(self, _name):
            return _Blob()

    class _Client:
        def bucket(self, _name):
            return _Bucket()

    text = read_transcript_text(
        "gs://b/workspaces/test/rag/o.txt", gcs_client=_Client()
    )
    assert text == "Hello"
    assert not text.startswith("\ufeff")


def test_one_recording_is_never_mailed_for_two_sessions(transport):
    """Two sessions of the same meeting close enough to share a window — a
    restart, or an abandoned row plus the real one — must not both mail the
    same recording to the same people."""
    from rubik_core.database.eos import L10Meeting, L10Session

    _seed()
    db = _session()
    try:
        meeting = db.query(L10Meeting).first()
        db.add(
            L10Session(
                id=uuid.uuid4(),
                meeting_id=meeting.id,
                actual_start=MEETING_START - timedelta(hours=3),
                actual_end=MEETING_END - timedelta(hours=3),
                notes="Abandoned earlier attempt.",
            )
        )
        db.commit()
    finally:
        db.close()

    report = _sweep()
    live = [o for o in report.outcomes if o.status != "not_l10"]
    assert len(live) == 2
    sent = [o for o in live if o.status == "sent"]
    assert len(sent) == 1, [o.status for o in live]
    assert len(transport.sent) == 1
    # The other session found the recording claimed and reported unrecorded.
    assert [o.status for o in live if o.status != "sent"] == ["not_recorded"]


def test_closer_recording_wins_over_a_fuller_attendee_list(transport):
    """Proximity must outrank attendance overlap: overlap is already a hard
    gate, and ranking on it let a week-old recording with more attendees beat
    the right one."""
    from rubik_core.database.models import Asset, MeetingParticipant

    _seed(
        roster=[
            "bellison@acme.example",
            "jhayes@acme.example",
            "absent@acme.example",
        ]
    )
    db = _session()
    try:
        # A same-titled recording ingested EARLIER, with a fuller ACL.
        db.add(
            Asset(
                doc_id="doc-transcript-decoy",
                owner_email="bellison@acme.example",
                topic="Meeting Transcripts",
                status="approved",
                original_filename="Systems L10 - 2026-08-19 - Notes by Gemini",
                source_path="gs://bucket/cubie/decoy.txt",
                created_at=MEETING_START - timedelta(hours=1),
                updated_at=MEETING_START - timedelta(hours=1),
            )
        )
        for email in (
            "bellison@acme.example",
            "jhayes@acme.example",
            "absent@acme.example",
        ):
            db.add(
                MeetingParticipant(
                    doc_id="doc-transcript-decoy",
                    participant_email=email,
                    meeting_id="drive-file-decoy",
                )
            )
        db.commit()
    finally:
        db.close()

    outcome = _only(_sweep())
    assert outcome.status == "sent"
    # The real one (ingested 20 min after the meeting) wins on proximity.
    assert outcome.doc_id == "doc-transcript-1"


# ── Digest-only recap: the meeting nobody recorded ──────────────────────────
#
# A team that stops recording its L10 would otherwise never get a recap again,
# while every team that records gets one each week.

# Far enough past MEETING_START to clear the box + RECORDING_LAG, i.e. no
# transcript can be claimed for this session any more.
AFTER_WINDOW = MEETING_START + timedelta(hours=40)


def _sweep_after_window(**kwargs):
    from backend.services.l10_summary_email import sweep_l10_summary_emails

    db = _session()
    try:
        return sweep_l10_summary_emails(db, now=AFTER_WINDOW, **kwargs)
    finally:
        db.close()


def test_unrecorded_meeting_waits_out_the_recording_window(transport):
    """A late transcript must never lose to an eager digest.

    The send happens exactly once, so mailing the thinner email while the
    recording could still land would permanently replace the fuller one.
    Observed Gemini publish delays run to 27 h.
    """
    _seed(transcript_title=None)
    outcome = _only(_sweep())  # now = meeting end + 1h, still inside the window
    assert outcome.status == "not_recorded"
    assert transport.sent == []


def test_unrecorded_meeting_gets_a_digest_recap_once_the_window_closes(transport):
    _seed(
        roster=["bellison@acme.example", "jhayes@acme.example"],
        transcript_title=None,
    )
    outcome = _only(_sweep_after_window())

    assert outcome.status == "sent_digest_only"
    assert outcome.digest_only is True
    assert outcome.doc_id is None  # nothing to point at

    spec = transport.sent[0]["spec"]
    # Everyone on the team's L10 who can read it — the digest repeats only
    # what they can already see in Cubie.
    assert "bellison@acme.example" in spec.to
    # The email must not claim a provenance it doesn't have.
    assert "was not recorded" in spec.text_body
    assert "recording shows were in the room" not in spec.text_body


def _save_company(name: str) -> None:
    from rubik_core.company.profile import CompanyProfile, save_company_profile

    db = _session()
    try:
        save_company_profile(db, CompanyProfile(name=name))
    finally:
        db.close()


def test_the_recap_names_the_company_from_its_profile(transport):
    """Spec §4.1: the recap carries the workspace's company (Admin → Company
    setup); before one is saved it says the product's name, never "None"."""
    _seed()
    _save_company("Acme Corp")
    assert _only(_sweep()).status == "sent"

    spec = transport.sent[0]["spec"]
    assert spec.text_body.startswith("Acme Corp · L10 recap\n")
    assert "Acme Corp · L10 recap" in spec.html_body


def test_the_digest_recap_names_the_product_with_no_profile(transport):
    _seed(roster=["bellison@acme.example"], transcript_title=None)
    assert _only(_sweep_after_window()).status == "sent_digest_only"

    spec = transport.sent[0]["spec"]
    assert spec.text_body.startswith("Cubie · L10 recap\n")
    assert "None" not in spec.text_body


def test_digest_recap_is_sent_only_once(transport):
    """``summary_email_sent_at`` is stamped on the digest path too, so the
    every-15-minutes sweep can't re-mail the same meeting all week."""
    _seed(
        roster=["bellison@acme.example", "jhayes@acme.example"],
        transcript_title=None,
    )
    assert _only(_sweep_after_window()).status == "sent_digest_only"
    assert _only(_sweep_after_window()).status == "already_sent"
    assert len(transport.sent) == 1


def test_unrecorded_meeting_with_no_entries_sends_nothing(transport):
    """No recording AND nothing logged is not a meeting Cubie can describe.
    An empty-handed recap is worse than silence."""
    seeded = _seed(transcript_title=None)
    db = _session()
    try:
        from rubik_core.database.eos import L10Session

        row = db.query(L10Session).filter(L10Session.id == seeded["session_id"]).one()
        row.notes = None
        row.average_rating = None
        db.commit()
    finally:
        db.close()

    outcome = _only(_sweep_after_window())
    assert outcome.status == "no_material"
    assert transport.sent == []


def test_digest_recap_still_obeys_the_exec_fence(transport):
    """The relaxation is about PRESENCE, never about access: a digest-only
    recap of the Exec L10 must reach Exec-team members only, exactly as its
    recording-backed recap would."""
    _seed(
        meeting_name="ET L10",
        is_exec_meeting=True,
        transcript_title=None,
        attendee_is_exec=False,
    )
    outcome = _only(_sweep_after_window())

    # jhayes is not on the Exec team. For an exec meeting the audience is
    # resolved from the Exec team in the first place, so they are never even a
    # candidate — the fence holds one stage earlier than the ACL check, which
    # is the safe direction. What matters is that they cannot receive it.
    assert outcome.status == "sent_digest_only"
    assert "jhayes@acme.example" not in outcome.recipients
    assert set(outcome.recipients) == {
        "bellison@acme.example",
        "absent@acme.example",
    }
    assert "jhayes@acme.example" not in transport.sent[0]["spec"].to


def test_digest_recap_can_be_switched_off(transport, monkeypatch):
    """The kill switch returns the feature to recorded-only recaps."""
    from rubik_core.config import settings

    monkeypatch.setattr(settings, "CUBIE_L10_DIGEST_ONLY_ENABLED", False)
    _seed(
        roster=["bellison@acme.example", "jhayes@acme.example"],
        transcript_title=None,
    )
    outcome = _only(_sweep_after_window())
    assert outcome.status == "not_recorded"
    assert transport.sent == []


def test_digest_dry_run_reports_without_sending(transport):
    # Also covers the audience's PRIMARY source: attendance rows, written by
    # the in-app Check-in form and Conclude survey.
    _seed(transcript_title=None, attended_emails=["bellison@acme.example"])
    outcome = _only(_sweep_after_window(dry_run=True))
    assert outcome.status == "would_send"
    assert outcome.digest_only is True
    assert outcome.recipients == ["bellison@acme.example"]
    assert transport.sent == []


# ── Guard: one meeting, one digest ──────────────────────────────────────────


def _add_second_session(meeting_id, *, actual_start, actual_end):
    """An abandoned session row beside the real one — dev's 'Team L10' has a
    pair 23 h apart for a single Wednesday meeting."""
    from rubik_core.database.eos import L10Session

    db = _session()
    try:
        row = L10Session(
            id=uuid.uuid4(),
            meeting_id=meeting_id,
            actual_start=actual_start,
            actual_end=actual_end,
            notes="Stray row from a restarted session.",
            average_rating=7,
        )
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def test_two_sessions_of_one_meeting_send_one_digest(transport):
    seeded = _seed(
        roster=["bellison@acme.example", "jhayes@acme.example"],
        transcript_title=None,
    )
    _add_second_session(
        seeded["meeting_id"],
        actual_start=MEETING_START - timedelta(hours=23),
        actual_end=MEETING_END - timedelta(hours=23),
    )
    report = _sweep_after_window()

    statuses = sorted(o.status for o in report.outcomes)
    assert statuses == ["already_covered", "sent_digest_only"]
    assert len(transport.sent) == 1
    # Newest-end-first ordering means the REAL occurrence wins the send and the
    # abandoned row beside it is the one suppressed.
    sent = [o for o in report.outcomes if o.status == "sent_digest_only"][0]
    assert sent.actual_end == MEETING_END


def test_a_recorded_recap_also_blocks_a_sibling_digest(transport):
    """A recorded recap covers the occurrence, so the abandoned row next to it
    must not follow up with a digest of the same meeting."""
    seeded = _seed(
        roster=["bellison@acme.example", "jhayes@acme.example"],
    )  # recorded: the real session mails from its transcript
    _add_second_session(
        seeded["meeting_id"],
        actual_start=MEETING_START - timedelta(hours=23),
        actual_end=MEETING_END - timedelta(hours=23),
    )
    report = _sweep_after_window()

    assert sorted(o.status for o in report.outcomes) == ["already_covered", "sent"]
    assert len(transport.sent) == 1


def test_next_weeks_occurrence_is_not_suppressed(transport):
    """The dedupe window must never reach the next real occurrence — an L10 is
    weekly, and suppressing it would silence the meeting permanently."""
    seeded = _seed(
        roster=["bellison@acme.example", "jhayes@acme.example"],
        transcript_title=None,
    )
    _add_second_session(
        seeded["meeting_id"],
        actual_start=MEETING_START - timedelta(days=7),
        actual_end=MEETING_END - timedelta(days=7),
    )
    report = sweep_l10_summary_emails_at(AFTER_WINDOW, lookback=timedelta(hours=400))

    assert [o.status for o in report.outcomes] == [
        "sent_digest_only",
        "sent_digest_only",
    ]
    assert len(transport.sent) == 2


def sweep_l10_summary_emails_at(now, **kwargs):
    from backend.services.l10_summary_email import sweep_l10_summary_emails

    db = _session()
    try:
        return sweep_l10_summary_emails(db, now=now, **kwargs)
    finally:
        db.close()


# ── Guard: Exec content cannot escape via a mis-flagged meeting ─────────────


def test_digest_never_falls_back_to_the_department(transport):
    """The leak this closes: an EXEC meeting whose ``is_exec_meeting`` flag was
    never set — one titled "Team L10" rather than "ET L10", which the import's
    name check does not recognise — must not have its contents mailed to a
    whole department under the weaker department fence.

    With no roster, no attendance rows and no exec flag there is no evidence of
    who was in the room, so the digest goes to nobody rather than to everyone
    who happens to share a department stamp.
    """
    _seed(
        meeting_name="Team L10",  # an Exec meeting that doesn't look like one
        is_exec_meeting=False,  # ...and was never flagged
        department="Systems",
        roster=None,  # hand-scheduled: no invite list
        transcript_title=None,
    )
    outcome = _only(_sweep_after_window())

    assert outcome.status == "no_recipients"
    assert outcome.recipients == []
    assert transport.sent == []


def test_digest_uses_the_calendar_roster_when_there_is_one(transport):
    """A calendar-imported meeting's invite list IS its ACL, so it is safe --
    and it is what makes the Systems 'Team L10' reach its four people."""
    _seed(
        meeting_name="Team L10",
        roster=["bellison@acme.example", "jhayes@acme.example"],
        transcript_title=None,
    )
    outcome = _only(_sweep_after_window())

    assert outcome.status == "sent_digest_only"
    assert set(outcome.recipients) == {
        "bellison@acme.example",
        "jhayes@acme.example",
    }
    # absent@ is in the department but not on the invite — never reached.
    assert "absent@acme.example" not in outcome.recipients


def _duplicate_meeting_row(*, name, department, is_exec, actual_start, actual_end):
    """The same real meeting re-imported as a SECOND meeting row — dev holds
    two 'Team L10' (Systems) and two 'Sales L10 Meeting' (Sales) this way."""
    from rubik_core.database.eos import L10Meeting, L10Session

    db = _session()
    try:
        meeting = L10Meeting(
            id=uuid.uuid4(),
            name=name,
            department_name=None if is_exec else department,
            is_exec_meeting=is_exec,
            duration_minutes=90,
            roster_emails=["bellison@acme.example"],
        )
        db.add(meeting)
        db.add(
            L10Session(
                id=uuid.uuid4(),
                meeting_id=meeting.id,
                actual_start=actual_start,
                actual_end=actual_end,
                notes="Entries on the duplicate row.",
                average_rating=6,
            )
        )
        db.commit()
        return meeting.id
    finally:
        db.close()


def test_a_duplicate_meeting_row_does_not_mail_the_same_occurrence_twice(transport):
    """Keying dedupe on meeting_id alone was not enough: dev's Systems 'Team
    L10' exists as two meeting rows, so the same Wednesday could be mailed by
    each of them."""
    _seed(
        meeting_name="Team L10",
        department="Systems",
        roster=["bellison@acme.example"],
        transcript_title=None,
    )
    _duplicate_meeting_row(
        name="Team L10",
        department="Systems",
        is_exec=False,
        actual_start=MEETING_START - timedelta(hours=23),
        actual_end=MEETING_END - timedelta(hours=23),
    )
    report = _sweep_after_window()

    assert sorted(o.status for o in report.outcomes) == [
        "already_covered",
        "sent_digest_only",
    ]
    assert len(transport.sent) == 1


def test_same_name_different_team_both_still_send(transport):
    """The flip side, and the more dangerous failure: the Exec team may run a
    meeting called 'Team L10' too. It is a DIFFERENT meeting, so suppressing it
    as a duplicate would silence a real team — worse than a duplicate email."""
    _seed(
        meeting_name="Team L10",
        department="Systems",
        roster=["bellison@acme.example"],
        transcript_title=None,
    )
    _duplicate_meeting_row(
        name="Team L10",  # same name...
        department=None,
        is_exec=True,  # ...different team
        actual_start=MEETING_START - timedelta(hours=23),
        actual_end=MEETING_END - timedelta(hours=23),
    )
    report = _sweep_after_window()

    assert [o.status for o in report.outcomes] == [
        "sent_digest_only",
        "sent_digest_only",
    ]
    assert len(transport.sent) == 2

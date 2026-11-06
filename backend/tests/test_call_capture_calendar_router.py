"""The calendar entry point's three router contracts (``backend/routers/calls.py``).

Under test: ``GET /api/calls/scheduled``, ``GET /api/calls/companies`` and the
``calendar_event_id`` branch of ``POST /api/calls/start``. The audience and flag
gates are pinned once, for every endpoint, by ``CALL_ENDPOINTS`` in
``test_sales_kaizen_access.py``; this file is about what the endpoints DO.

The redesign's framing decides most of what is asserted here. The calendar
screen's primary job is to bind a meeting to the right HubSpot company and hand
the AE the calendar's own Meet link. Arming transcription is SECONDARY and
best-effort, because three things make arm-everything impossible: a conference
already in progress cannot be armed, a client-hosted space lives in another
tenant and cannot be patched at all, and a recurring series reuses ONE space so
arming one occurrence arms them all.

That is why nearly every test below is a variation on the same sentence: **an
arming failure is a SUCCESSFUL start with an honest verdict.** The session row
still gets created, because the company binding is the value that always works
— and a call Cubie cannot transcribe is still a call Cubie should know about.
The two failures being defended against are the mirror image of each other and
both are silent: a row that cannot be armed rendering as armed, and a row that
was armed rendering as broken.

Nothing here talks to Google or HubSpot. Both boundaries are faked at their
client factory and patched **by string target**, because other suites
``importlib.reload()`` modules mid-run and an object-target patch goes stale in
a full-suite run.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth import get_current_user
from backend.main import app
from rubik_core.call_capture.calendar_scan import ARMING_REASONS, ARMING_STATES
from rubik_core.config import settings
from rubik_core.database.call_capture import CallSession
from rubik_core.database.db import get_db
from rubik_core.database.schemas import UserProfile
from rubik_core.identifiers import CALL_ORIGIN_ASSISTANT

client = TestClient(app)

AE = "parker@acme.example"
COMPANY = "co-42"
EVENT = "evt_20260828T210000Z"
CODE = "abc-defg-hij"
URI = "https://meet.google.com/abc-defg-hij"
SPACE = "spaces/AAAA1111"


# ── Scaffolding ──────────────────────────────────────────────────────────────


def _profile(email: str = AE, *, department: str = "Systems") -> UserProfile:
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name=email.split("@")[0].title(),
        department=department,
        roles=["user"],
        is_exec=False,
        managed_departments=[],
        created_at="2026-08-28T00:00:00",
        updated_at="2026-08-28T00:00:00",
    )


def _as(email: str = AE, *, department: str = "Systems") -> UserProfile:
    """Authenticate by overriding the dependency — never by sending a header."""
    profile = _profile(email, department=department)
    app.dependency_overrides[get_current_user] = lambda: profile
    return profile


@pytest.fixture
def db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from rubik_core.database import eos  # noqa: F401 — registers eos_* tables
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr("rubik_core.database.db.SessionLocal", Session)
    s = Session()
    app.dependency_overrides[get_db] = lambda: s
    try:
        yield s
    finally:
        app.dependency_overrides.clear()
        s.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def flags(monkeypatch):
    """Both switches on, so a test exercises the RULE and not the flag."""
    monkeypatch.setattr(settings, "CCS_ENABLED", True)
    monkeypatch.setattr(settings, "CCS_MEET_ENABLED", True)
    monkeypatch.setattr(
        "rubik_core.tenancy.crm_visibility.visible_source_ids",
        lambda companies, contacts, deals: (companies, contacts, deals),
    )


@pytest.fixture(autouse=True)
def no_company_lookup(monkeypatch):
    """``_company_name`` is a nicety that costs a live HubSpot read. Stub it."""
    monkeypatch.setattr("backend.routers.calls._company_name", lambda _cid: "Acme")


class _FakeMeet:
    """``GoogleMeetClient``, minus Google. Records what it was asked to do."""

    armed: list[dict] = []
    created: list[dict] = []
    #: Either a result dict or an exception INSTANCE to raise.
    outcome: object = None

    def __init__(self, user_email: str):
        self.user_email = user_email

    def arm_space_transcription(self, **kw) -> dict:
        _FakeMeet.armed.append(dict(kw))
        outcome = _FakeMeet.outcome
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome or {
            "space_name": SPACE,
            "meeting_code": CODE,
            "state": "armed",
            "artifact_config_before": {},
            "artifact_config_after": {
                "transcriptionConfig": {"autoTranscriptionGeneration": "ON"}
            },
        }

    def create_space(self, **kw) -> dict:
        _FakeMeet.created.append(dict(kw))
        # A fresh name per call, because ``spaces.create`` gives a fresh space
        # per call and ``call_sessions.meet_space_name`` is UNIQUE. A fake that
        # returned one constant would make the ad-hoc path look like it 500s on
        # the second start — a defect of the fake, not of the router.
        nth = len(_FakeMeet.created)
        return {
            "name": f"spaces/CREATED{nth}",
            "meetingCode": f"zzz-zzzz-z{nth:02d}",
            "meetingUri": f"https://meet.google.com/zzz-zzzz-z{nth:02d}",
            "config": {},
        }


@pytest.fixture
def meet(monkeypatch) -> type[_FakeMeet]:
    _FakeMeet.armed = []
    _FakeMeet.created = []
    _FakeMeet.outcome = None
    monkeypatch.setattr("rubik_core.tools.meet.GoogleMeetClient", _FakeMeet)
    return _FakeMeet


def _start(**extra) -> dict:
    body = {
        "hubspot_company_id": COMPANY,
        "calendar_event_id": EVENT,
        "meet_meeting_code": CODE,
        "meet_uri": URI,
    }
    body.update(extra)
    return body


# ═══ 1. GET /scheduled — a pure read that never 500s ════════════════════════


_ENVELOPE = {
    "calendar_available": True,
    "timezone": "America/Chicago",
    "local_date": "2026-08-28",
    "meetings": [],
    "script_catalogue": [],
}


def test_scheduled_returns_the_scan_envelope_for_the_caller(db, monkeypatch):
    """The router adds nothing and removes nothing.

    Whether a row renders as bound, ambiguous or unarmable is decided
    server-side in ``calendar_scan`` and pinned there. What the router owes is
    that it asks about the CALLER (never a body-supplied email — one AE must not
    be able to read another's calendar) and hands the envelope back intact.
    """
    seen: dict = {}

    def _scan(ae_email, **kw):
        seen["ae_email"] = ae_email
        seen["kw"] = kw
        return dict(_ENVELOPE)

    monkeypatch.setattr("rubik_core.call_capture.calendar_scan.scan_day", _scan)
    _as()
    res = client.get("/api/calls/scheduled")
    assert res.status_code == 200, res.text
    assert res.json() == _ENVELOPE
    assert seen["ae_email"] == AE
    # The request's own session, not a second one opened per scan.
    assert seen["kw"]["db"] is db


def test_scheduled_performs_no_meet_write_and_creates_no_session(db, meet, monkeypatch):
    """Opening the screen must not arm anything or start anything.

    A recurring series shares ONE Meet space, so a render-time arm would turn
    transcription on for every future occurrence of every weekly client call the
    AE scrolled past — without anyone having chosen it. And a session row minted
    by a GET would claim a call that has not happened.
    """
    monkeypatch.setattr(
        "rubik_core.call_capture.calendar_scan.scan_day",
        lambda ae_email, **kw: dict(_ENVELOPE),
    )
    _as()
    assert client.get("/api/calls/scheduled").status_code == 200
    assert meet.armed == []
    assert meet.created == []
    assert db.query(CallSession).count() == 0


def test_a_scan_failure_is_an_unavailable_day_and_not_a_500(db, monkeypatch):
    """The shape of the lie is what matters, so it is asserted directly.

    ``calendar_available: False`` with no meetings is "we could not look", and
    the UI says so out loud. ``True`` with no meetings is "you have no client
    calls today" — which the AE BELIEVES, and would then not capture a call they
    actually have. ``scan_day`` is documented never to raise; this pins the
    router's answer for the case that contract is broken by a bug.
    """

    def _boom(ae_email, **kw):
        raise RuntimeError("calendar exploded")

    monkeypatch.setattr("rubik_core.call_capture.calendar_scan.scan_day", _boom)
    _as()
    res = client.get("/api/calls/scheduled")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["calendar_available"] is False
    assert body["meetings"] == []
    # Still a usable envelope: the UI keys its header off these two.
    assert body["timezone"]
    assert body["local_date"]
    # The same keys ``scan_day`` sends, so the client never meets a new shape
    # on the one path that exists because something broke.
    assert body["script_catalogue"] is None


def test_scheduled_survives_having_no_calendar_credentials(db):
    """The unpatched path, end to end, with no Workspace key configured.

    This is the local-dev and the revoked-grant case, and it is the one that
    would 500 if any layer between the router and Google raised instead of
    reporting. Nothing is stubbed here on purpose.
    """
    _as()
    res = client.get("/api/calls/scheduled")
    assert res.status_code == 200, res.text
    assert res.json()["calendar_available"] is False


# ═══ 2. GET /companies — the picker that stopped inheriting X-Ray's gate ═════


def _fake_search(rows: list[dict]):
    captured: dict = {}

    def _search(query: str, *, limit: int = 10) -> list[dict]:
        captured["query"] = query
        captured["limit"] = limit
        return rows

    return _search, captured


def test_the_picker_renames_the_id_to_the_name_start_expects(db, monkeypatch):
    """``hubspot_company_id``, because that is what goes back to ``POST /start``.

    ``search_companies`` calls it ``company_id``; every field this router sends
    and receives calls it ``hubspot_company_id``. Renaming it at the boundary is
    what stops the frontend from having to know which of the two it is holding —
    and a wrong company id here is a proposal written against the wrong CRM
    record that the AE cannot see from the wrap-up.
    """
    search, captured = _fake_search(
        [
            {
                "company_id": "123",
                "name": "Acme Industries LLC",
                "domain": "acmeindustries.example",
                "client_status": "Client",
                "csm": "Marin",
            }
        ]
    )
    monkeypatch.setattr("rubik_core.xray.fetchers.search_companies", search)
    _as()
    res = client.get("/api/calls/companies", params={"q": "acme"})
    assert res.status_code == 200, res.text
    assert res.json() == {
        "companies": [
            {
                "hubspot_company_id": "123",
                "name": "Acme Industries LLC",
                "domain": "acmeindustries.example",
                "client_status": "Client",
            }
        ]
    }
    assert captured["query"] == "acme"


def test_an_empty_query_asks_hubspot_nothing(db, monkeypatch):
    """Same rule as ``/api/xray/customers``: no query, no results.

    Not merely a saved request. A picker that answers before it is asked hands
    the AE the first page of every company in the portal, and the top row of
    that list is a plausible wrong company one click from a CRM write.
    """
    called: list[str] = []
    monkeypatch.setattr(
        "rubik_core.xray.fetchers.search_companies",
        lambda q, **kw: called.append(q) or [],
    )
    _as()
    for params in ({}, {"q": ""}, {"q": "   "}):
        res = client.get("/api/calls/companies", params=params)
        assert res.status_code == 200, res.text
        assert res.json() == {"companies": []}
    assert called == []


def test_a_hubspot_outage_empties_the_picker_without_breaking_the_screen(
    db, monkeypatch
):
    """A typeahead is not allowed to take the page down with it.

    The company binding has a second path — the automatic domain match — and the
    calendar rows are already rendered by the time anyone types here.
    """

    def _boom(q, **kw):
        raise RuntimeError("hubspot 429")

    monkeypatch.setattr("rubik_core.xray.fetchers.search_companies", _boom)
    _as()
    res = client.get("/api/calls/companies", params={"q": "acme"})
    assert res.status_code == 200, res.text
    assert res.json() == {"companies": []}


def test_the_picker_does_not_track_customer_xrays_gate(db, monkeypatch):
    """The bug this endpoint exists to stop, made visible.

    ``/api/xray/customers`` is the only other company typeahead in Cubie and it
    sits behind ``user_can_access_xray``, which is a DIFFERENT audience that
    moves for reasons of its own (it was Systems-only for a two-month soft
    launch, then Sales + Systems + Exec). Whatever it says, the calendar picker,
    which unmatched and ambiguous rows depend on, must keep working because it
    is gated by ``require_ccs`` alone.

    So: make X-Ray's gate refuse this AE outright, and check the picker does not
    care.
    """
    monkeypatch.setattr("backend.auth.user_can_access_xray", lambda user: False)
    monkeypatch.setattr("rubik_core.xray.fetchers.search_companies", lambda q, **kw: [])
    _as()
    assert client.get("/api/xray/customers", params={"q": "acme"}).status_code == 403
    assert client.get("/api/calls/companies", params={"q": "acme"}).status_code == 200


# ═══ 3. POST /start from the calendar — no space is created ══════════════════


def test_a_calendar_start_arms_the_existing_space_and_creates_no_new_one(db, meet):
    """The whole point of the redesign, in one assertion pair.

    ``create_space`` is never called: the calendar already made a space, the
    client has that link in their invite, and a second Meet link would leave the
    AE alone in a room while the client waits in the other one. ``meet_uri`` and
    ``meet_meeting_code`` are persisted from the REQUEST because they came from
    the calendar; only ``meet_space_name`` comes from Google.
    """
    _as()
    res = client.post("/api/calls/start", json=_start())
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["origin"] == "calendar"
    assert body["arming"] == {
        "state": "armed",
        "reason": "armed_by_cubie",
        "detail": None,
    }
    assert body["meet_uri"] == URI
    assert body["meeting_code"] == CODE

    assert meet.created == [], "a calendar start created a second Meet space"
    assert meet.armed == [{"meeting_code": CODE}]

    session = db.query(CallSession).one()
    assert session.origin == "calendar"
    assert session.calendar_event_id == EVENT
    assert session.hubspot_company_id == COMPANY
    assert session.meet_space_name == SPACE
    assert session.meet_meeting_code == CODE
    assert session.meet_uri == URI
    assert session.attrs["capture_arming"]["reason"] == "armed_by_cubie"


@pytest.mark.parametrize("calendar", [True, False])
def test_start_refuses_revoked_crm_selection_before_meet_or_db_write(
    db, meet, monkeypatch, calendar
):
    monkeypatch.setattr(
        "rubik_core.tenancy.crm_visibility.visible_source_ids",
        lambda companies, contacts, deals: (set(), contacts, deals),
    )
    _as()
    body = _start() if calendar else {"hubspot_company_id": COMPANY}

    response = client.post("/api/calls/start", json=body)

    assert response.status_code == 409
    assert meet.armed == [] and meet.created == []
    assert db.query(CallSession).count() == 0


def test_a_calendar_start_without_a_meeting_code_is_a_422(db, meet):
    """422 rather than a quiet fall-through to the ad-hoc path.

    The code is the only address ``spaces.get`` accepts for a space Cubie did
    not create, so without it there is nothing to arm — and falling through
    would create a SECOND space for a meeting that already has one, which is the
    exact failure the calendar entry point was built to remove.
    """
    _as()
    res = client.post("/api/calls/start", json=_start(meet_meeting_code=None))
    assert res.status_code == 422, res.text
    assert "meet_meeting_code" in res.json()["detail"]
    assert db.query(CallSession).count() == 0
    assert meet.armed == []
    assert meet.created == []


def test_transcription_already_on_is_reported_as_already_on(db, meet):
    """``already_armed`` is not ``armed_by_cubie``, and the difference is real.

    A recurring series shares one space, so the second occurrence of a weekly
    client call finds transcription already on from the first. Reporting that as
    "Cubie armed this" would credit Cubie with a setting the host chose — and
    would hide the fact that no patch was sent.
    """
    meet.outcome = {
        "space_name": SPACE,
        "meeting_code": CODE,
        "state": "already_armed",
        "artifact_config_before": {
            "transcriptionConfig": {"autoTranscriptionGeneration": "ON"}
        },
        "artifact_config_after": {
            "transcriptionConfig": {"autoTranscriptionGeneration": "ON"}
        },
    }
    _as()
    res = client.post("/api/calls/start", json=_start())
    assert res.status_code == 200, res.text
    arming = res.json()["arming"]
    assert arming["state"] == "armed"
    assert arming["reason"] == "already_on"
    assert db.query(CallSession).one().attrs["capture_arming"]["reason"] == "already_on"


def _meet_error(name: str):
    """One of ``meet.py``'s named arming failures, constructed."""
    from rubik_core.tools import meet as meet_mod

    if name == "not_patchable":
        return meet_mod.MeetSpaceNotPatchable(
            "403 on patch", subject=AE, space_name=SPACE
        )
    return {
        "not_found": lambda: meet_mod.MeetSpaceNotFound("404 on get"),
        "unsupported": lambda: meet_mod.MeetArtifactsUnsupported("licence"),
        "write_disabled": lambda: meet_mod.MeetSpaceWriteDisabled("flag off"),
    }[name]()


@pytest.mark.parametrize(
    "failure,reason",
    [
        # A 404 on ``spaces.get`` for a meeting off the AE's own calendar: the
        # space is in the client's tenant, so it cannot be patched at all.
        ("not_found", "external_host"),
        # This subject asked and Meet said no. NOT a claim about who is
        # permitted to patch a space they did not create.
        ("not_patchable", "not_owner"),
        # The edition/licence answer. Tenant-wide, so every other row will say
        # the same — which is why the UI is meant to say it once, not per row.
        ("unsupported", "arming_unsupported"),
        # Cubie's own switch, and NOT a 503 here: on the ad-hoc path the space
        # write IS the feature, but here the row and the binding are unaffected.
        ("write_disabled", "write_disabled"),
    ],
)
def test_every_arming_failure_is_a_successful_start_with_a_reason(
    db, meet, failure, reason
):
    """The rule that outranks everything else on this path.

    The company binding is the primary value: ``hubspot_company_id`` resolved
    before the call is what lets the wrap-up write to the right CRM record
    without asking. A call Cubie cannot transcribe is still a call Cubie should
    know about, so refusing the start would throw away the working half of the
    feature to report the best-effort half — at the moment the AE is trying to
    join a client call.

    And the verdict is never blank. A silent no-op is the worst outcome here:
    ``arming.state == "armed"`` on a row that was not armed is a lie the AE only
    discovers when the transcript never arrives.
    """
    meet.outcome = _meet_error(failure)
    _as()
    res = client.post("/api/calls/start", json=_start())
    assert res.status_code == 200, res.text
    arming = res.json()["arming"]
    assert arming["state"] == "not_armable"
    assert arming["reason"] == reason
    assert arming["detail"], "an unarmable row with no reason is the silent no-op"

    session = db.query(CallSession).one()
    assert session.origin == "calendar"
    assert session.hubspot_company_id == COMPANY, "the binding survived"
    assert session.meet_uri == URI, "the AE still has the calendar's own link"
    assert session.meet_space_name is None, "nothing was armed, so nothing resolved"
    assert session.attrs["capture_arming"]["reason"] == reason


def test_an_unclassified_google_failure_still_starts_the_call(db, meet):
    """An HTTP 500 from Meet is not a reason to lose the session row either.

    ``not_armable`` rather than ``unknown``: nothing was armed, this flow has no
    retry, and the call is starting now — "unknown" would imply something may
    still happen.
    """
    meet.outcome = RuntimeError("Meet 500")
    _as()
    res = client.post("/api/calls/start", json=_start())
    assert res.status_code == 200, res.text
    assert res.json()["arming"] == {
        "state": "not_armable",
        "reason": "google_error",
        "detail": res.json()["arming"]["detail"],
    }
    assert "RuntimeError" in res.json()["arming"]["detail"]
    assert db.query(CallSession).count() == 1


def test_a_google_http_error_names_the_status_not_an_unreachable_google(db, meet):
    """Google answered 400, so Google WAS reached.

    The row used to read "Meet space not found … could not reach Google
    (HttpError)" — both halves wrong, since the lookup had succeeded and the
    patch was refused. It names the status and its own reason now.
    """
    from googleapiclient.errors import HttpError

    class _Resp:
        status = 400
        reason = "Bad Request"

    meet.outcome = HttpError(_Resp(), b'{"error": {"message": "Invalid update mask"}}')
    _as()
    res = client.post("/api/calls/start", json=_start())
    assert res.status_code == 200, res.text
    arming = res.json()["arming"]
    assert arming["state"] == "not_armable"
    assert arming["reason"] == "google_error"
    assert "HTTP 400" in arming["detail"]
    assert "could not reach" not in arming["detail"]
    assert db.query(CallSession).count() == 1


def test_an_unparseable_meeting_code_names_the_code_and_not_the_exception(db, meet):
    """A malformed code is caught before any HTTP, so say what it was.

    ``arm_space_transcription`` validates the code first specifically so a blank
    or garbage value can never become a request for the literal resource name
    ``spaces/``. The AE reads this string, so it names the value rather than
    the exception class.
    """
    meet.outcome = ValueError("Not a Meet meeting code: 'nope'")
    _as()
    res = client.post("/api/calls/start", json=_start(meet_meeting_code="nope"))
    assert res.status_code == 200, res.text
    arming = res.json()["arming"]
    assert arming["state"] == "not_armable"
    assert arming["reason"] == "lookup_failed"
    assert "'nope'" in arming["detail"]
    assert "ValueError" not in arming["detail"]
    assert db.query(CallSession).count() == 1


def test_a_confidential_calendar_call_is_never_armed(db, meet):
    """Consent gates EVERY artifact — that is the promise the modal makes.

    "No transcript is captured, nothing is extracted... This cannot be undone."
    So a suppressed call does not reach Meet at all: not armed and then ignored,
    not armed with recording off. Never asked.

    The verdict is ``unknown`` / ``not_checked_yet``, which is the honest reading
    inside the shared vocabulary — nothing was armed and nothing was checked.
    Never ``not_armable``: the space is perfectly armable and Cubie declined on
    purpose, which is a different fact from Google refusing.
    """
    _as()
    res = client.post("/api/calls/start", json=_start(capture_consent="suppressed"))
    assert res.status_code == 200, res.text
    assert meet.armed == [], "a confidential call was armed"
    arming = res.json()["arming"]
    assert arming["state"] == "unknown"
    assert arming["reason"] == "not_checked_yet"
    assert "confidential" in (arming["detail"] or "")

    session = db.query(CallSession).one()
    assert session.capture_consent == "suppressed"
    assert session.attrs["capture_arming"]["state"] == "unknown"


def test_two_clicks_on_one_calendar_row_produce_one_session(db, meet):
    """``UNIQUE(ae_email, calendar_event_id)`` is the idempotency, not a window.

    The ad-hoc path can only dedupe on (AE, company, consent) inside 60 seconds
    because it has no identity for the call. A calendar row HAS one, so the
    second click is answered from the database — no second space patch, and no
    500 from the IntegrityError.
    """
    _as()
    first = client.post("/api/calls/start", json=_start())
    second = client.post("/api/calls/start", json=_start())
    assert first.status_code == second.status_code == 200, second.text
    assert first.json()["call_session_id"] == second.json()["call_session_id"]
    assert db.query(CallSession).count() == 1
    assert len(meet.armed) == 1, "the second click re-armed the space"
    # The stored verdict is replayed, not re-derived.
    assert second.json()["arming"]["reason"] == "armed_by_cubie"


def test_a_recurring_series_second_occurrence_still_gets_a_row(db, meet):
    """``call_sessions.meet_space_name`` is UNIQUE and a series reuses ONE space.

    So the second occurrence of a weekly client call resolves to the SAME space
    name as the first, and storing it again would violate that constraint and
    lose the session row — the row that carries the company binding. The name is
    dropped instead and kept in ``attrs``; the sweep's correlation falls back to
    ``meet_meeting_code``, which is indexed, not unique, and newest-first, so it
    still picks this occurrence.
    """
    _as()
    assert client.post("/api/calls/start", json=_start()).status_code == 200
    res = client.post(
        "/api/calls/start", json=_start(calendar_event_id=f"{EVENT}_next_week")
    )
    assert res.status_code == 200, res.text

    rows = db.query(CallSession).order_by(CallSession.created_at.asc()).all()
    assert len(rows) == 2
    assert rows[0].meet_space_name == SPACE
    assert rows[1].meet_space_name is None
    assert rows[1].attrs["meet_space_name_resolved"] == SPACE
    assert rows[1].meet_meeting_code == CODE
    assert rows[1].hubspot_company_id == COMPANY


def test_every_verdict_this_router_emits_is_in_the_shared_vocabulary(db, meet):
    """The closed vocabularies are load-bearing, not labels.

    ``calendar_scan._stored_arming`` DROPS a stored verdict whose state or
    reason is outside ``ARMING_STATES`` / ``ARMING_REASONS`` and falls back to a
    precondition guess — so a typo here does not fail loudly, it silently
    replaces a real outcome with a derived one on the calendar screen. This
    walks every branch the router can take and checks both fields.
    """
    _as()
    cases: list[object] = [
        None,
        {"space_name": SPACE, "meeting_code": CODE, "state": "already_armed"},
        _meet_error("not_found"),
        _meet_error("not_patchable"),
        _meet_error("unsupported"),
        _meet_error("write_disabled"),
        RuntimeError("Meet 500"),
    ]
    for index, outcome in enumerate(cases):
        meet.outcome = outcome
        res = client.post(
            "/api/calls/start", json=_start(calendar_event_id=f"{EVENT}-{index}")
        )
        assert res.status_code == 200, res.text
        arming = res.json()["arming"]
        assert arming["state"] in ARMING_STATES, arming
        assert arming["reason"] in ARMING_REASONS, arming

    # And the suppressed branch, which never reaches Meet.
    res = client.post(
        "/api/calls/start",
        json=_start(calendar_event_id=f"{EVENT}-consent", capture_consent="suppressed"),
    )
    arming = res.json()["arming"]
    assert arming["state"] in ARMING_STATES
    assert arming["reason"] in ARMING_REASONS


# ═══ 4. The ad-hoc path is untouched ════════════════════════════════════════


def test_a_start_with_no_calendar_event_still_creates_a_space(db, meet):
    """The regression guard for the branch that already shipped.

    No ``calendar_event_id`` means the old behaviour exactly: ``create_space``,
    ``origin=CALL_ORIGIN_ASSISTANT``, no arming patch. It still has a reason to exist —
    there is no API to start transcription on a conference already in progress,
    so a call that must be captured and has no invite has to start from a space
    Cubie made.
    """
    _as()
    res = client.post("/api/calls/start", json={"hubspot_company_id": COMPANY})
    assert res.status_code == 200, res.text
    assert res.json()["origin"] == CALL_ORIGIN_ASSISTANT
    assert res.json()["arming"] == {
        "state": "armed",
        "reason": "armed_by_cubie",
        "detail": None,
    }
    assert len(meet.created) == 1
    assert meet.armed == []

    session = db.query(CallSession).one()
    assert session.origin == CALL_ORIGIN_ASSISTANT
    assert session.calendar_event_id is None
    assert session.meet_space_name == "spaces/CREATED1"


def test_calendar_and_adhoc_sessions_coexist_under_the_unique_constraint(db, meet):
    """``UNIQUE(ae_email, calendar_event_id)`` with NULLs, on the real database.

    Postgres and SQLite both treat NULLs as distinct in a unique constraint, so
    any number of ``origin=CALL_ORIGIN_ASSISTANT`` / ``"adhoc"`` rows (event id NULL) coexist
    freely alongside the calendar ones. Asserted rather than assumed, because
    the alternative — a partial index or a projection column — would have been a
    migration this change deliberately did not write.
    """
    _as()
    for _ in range(2):
        assert (
            client.post(
                "/api/calls/start", json={"hubspot_company_id": f"co-{_}"}
            ).status_code
            == 200
        )
    assert client.post("/api/calls/start", json=_start()).status_code == 200
    assert db.query(CallSession).count() == 3

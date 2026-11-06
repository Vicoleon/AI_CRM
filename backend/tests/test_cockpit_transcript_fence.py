"""The transcript fence around STAGE-LAG evidence, on both sides of the API.

``commitment_stage_lags.evidence_text`` is a verbatim client sentence lifted off
somebody's call, so it obeys the transcript-access table exactly like every other
quote in the feature:

    Role                  Transcript text
    AE (Parker)             own calls
    Manager (Marin)       team calls
    Supervisor (Harper)  NONE, ever
    Exec (Emery)           NONE

Three failures are pinned here, all of them ways a stage-lag quote reached
somebody the table excludes:

1. **The tautological gate.** ``cockpit._pipeline_hygiene_block`` asked
   ``user_can_read_transcript_text`` with the deal's *commitment owner* — which
   on My Day is always the caller — so the check compared a caller to themselves
   and could never deny. On a co-sold deal the call HOST is a different person.
2. **No gate at all.** ``/stage-lag/{id}/apply`` and ``/reject`` serialized
   ``evidence_text`` unconditionally, so an org-wide exec (``is_exec`` +
   ``managed_departments == ["*"]``), who sails through ``_authorize_deal`` as
   ``unrestricted``, got the quote back in the mutation response.
3. **An undeclared block code.** ``_no_contact_block`` could emit
   ``crm_read_failed``, which is not in the client's closed
   ``BLOCK_UNAVAILABLE_REASONS`` set.

── A NARROWED ROSTER, set as data, and what it does to this cast ─────────────

These tests run the cockpit narrowed to Systems: all five Sales Kaizen rosters
``{"systems"}`` with the ``is_exec`` door off, installed as the Feature Access
overrides an admin would write (``_systems_only_rosters``; the shipped defaults
are pinned in test_sales_kaizen_access.py §12). Every actor below
therefore sits in **Systems**, because an actor who 403s at the door proves
nothing about a field-level rule. Emery is cast in Systems too (see the note on
``user_can_access_cockpit_exec``).

The re-cast costs one thing, and it is named here rather than papered over:

    ``cockpit_transcript_policy`` has a three-value range — "team" / "own" /
    "none" — and a supervisor has NEVER resolved to a distinct one. Under a
    wider roster Harper sits in Support, inside the cockpit roster, and still
    resolves to "own", exactly like a non-manager AE. Harper's fence has always
    been the same two clauses that fence any non-manager: not the call's host,
    and a policy that is not "team". What the restriction *does* collapse is
    the department signal — ``COCKPIT_SUPERVISOR_DEPARTMENTS`` is now the AE
    roster too, so "Harper is Support, Parker is Sales" no longer separates
    them at all.

So the supervisor test below stops leaning on the department and pins the
things that still discriminate: the role the code resolves for Harper
(``user_can_access_cockpit_supervisor`` yes, ``_is_cockpit_manager`` no, policy
"own"), the fact that Harper reaches the mutation **by owning the
commitment on that deal** — the access table's "no exception, including deals they
are working" — and a positive control on the very same row proving the assertion
can still produce a quote for somebody the table grants one to. Section 4 adds
the denials the restriction itself creates, so the re-cast cannot be mistaken
for the fence having been loosened.

Nothing here talks to HubSpot, Meet, Gmail, Slack or Vertex; every boundary is
faked at its client factory and patched **by string target**, because other
suites ``importlib.reload()`` modules mid-run and an object-target patch goes
stale in a full-suite run.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth import (
    cockpit_transcript_policy,
    get_current_user,
    user_can_access_cockpit_supervisor,
)
from backend.main import app
from rubik_core.config import settings
from rubik_core.database.call_capture import (
    CallExtraction,
    CallExtractionItem,
    CallSession,
    CallTranscriptEntry,
)
from rubik_core.database.commitments import Commitment, CommitmentStageLag
from rubik_core.database.db import get_db
from rubik_core.database.schemas import UserProfile
from rubik_core.identifiers import CALL_ORIGIN_ASSISTANT

client = TestClient(app)

AE = "parker@acme.example"
CO_SELLER = "reese@acme.example"
MANAGER = "marin@acme.example"
SUPERVISOR = "harper@acme.example"
EXEC = "emery@acme.example"
ADMIN = "admin@acme.example"

#: The one department on the roster an admin narrowed the feature to. Spelled
#: as a ``departments.name`` row, not the slug, because that is what a real
#: ``UserProfile`` carries and ``slugify_department`` is what folds it.
NARROWED_DEPARTMENT = "Systems"

#: The departments these people sit in outside the narrowed roster, so the
#: denial tests in section 4 refuse them where they actually work rather than in
#: invented departments.
HOME_DEPARTMENTS = {
    AE: "Sales",
    CO_SELLER: "Sales",
    MANAGER: "Sales",
    SUPERVISOR: "Support",
    EXEC: "Executive",
}

#: The client's own words. Seeded into every evidence column so a leak can be
#: asserted against the whole response body rather than the fields a serializer
#: happened to name.
QUOTE = "CO-SELLER-ONLY: send the SOW by Friday or we walk."

DEAL = "deal-77"
COMPANY = "co-42"

#: The closed set the frontend declares in ``frontend/src/api/cockpit.ts``
#: (``BLOCK_UNAVAILABLE_REASONS``), mirrored verbatim. A router that answers
#: outside it degrades to a generic "could not load" sentence and loses the
#: reason, which is the whole point of the field.
#:
#: ``crm_read_failed`` is in the client's set but is no longer emitted: the
#: router folds a failed stale-deal search into ``crm_unavailable``, and the
#: client keeps the older code declared so a rollback cannot resurrect the
#: silent "you are caught up". The sweep below therefore allows it; the targeted
#: test still pins the exact code today's handler sends.
DECLARED_BLOCK_REASONS = frozenset(
    {
        "capture_disabled",
        "lead_capture_disabled",
        "lead_temperature_unavailable",
        "crm_unavailable",
        "crm_read_failed",
        "no_hubspot_owner",
        "client_records_unavailable",
    }
)


# ── Identities ────────────────────────────────────────────────────────────────
#
# Everyone who must REACH an endpoint sits in ``NARROWED_DEPARTMENT``. That is the
# whole of the re-cast: the roles below are still the four rows of the access table,
# and each one is separated from the others by something the code resolves —
# ``managed_departments`` for the manager, ``sees_all_departments`` for the
# exec, the ``admin`` role for the admin — never by the department, which the
# narrowed roster has made uniform.


CAST: dict[str, dict] = {
    AE: {"department": NARROWED_DEPARTMENT},
    CO_SELLER: {"department": NARROWED_DEPARTMENT},
    # A manager is made by ``managed_departments``, never by where they sit:
    # ``_cockpit_managed_slugs`` deliberately ignores the primary department so
    # that sitting in Systems cannot promote every Systems user.
    MANAGER: {"department": NARROWED_DEPARTMENT, "managed": [NARROWED_DEPARTMENT]},
    # Harper is in the narrowed department because the supervisor worklist is
    # narrowed to it too. Harper's supervisor-ness is asserted from the gate
    # Harper passes and the manager gate Harper fails, not from this line.
    SUPERVISOR: {"department": NARROWED_DEPARTMENT},
    # Org-wide exec: ``is_exec`` + the ``managed_departments`` wildcard, which is
    # what ``_authz._is_unrestricted`` keys on. "*" slugifies to nothing, so it
    # is not a department and never matches a roster — the exec's way IN is the
    # department, the way PAST ``_authorize_deal`` is the wildcard, and the two
    # are independent, which is exactly what the exec tests need.
    EXEC: {"department": NARROWED_DEPARTMENT, "is_exec": True, "managed": ["*"]},
    ADMIN: {"department": NARROWED_DEPARTMENT, "is_admin": True},
}


def _profile(
    email: str,
    *,
    department: str = NARROWED_DEPARTMENT,
    is_admin: bool = False,
    is_exec: bool = False,
    managed: list[str] | None = None,
) -> UserProfile:
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name=email.split("@")[0].title(),
        department=department,
        roles=["admin"] if is_admin else ["user"],
        is_exec=is_exec,
        managed_departments=managed or [],
        created_at="2026-08-27T00:00:00",
        updated_at="2026-08-27T00:00:00",
    )


def _cast(email: str, **overrides) -> UserProfile:
    """The cast profile, unauthenticated — for asserting *about* somebody."""
    return _profile(email, **{**CAST.get(email, {}), **overrides})


def _as(email: str, **overrides) -> UserProfile:
    """Authenticate by overriding the dependency — never by sending a header."""
    profile = _cast(email, **overrides)
    app.dependency_overrides[get_current_user] = lambda: profile
    return profile


# ── Database ──────────────────────────────────────────────────────────────────


@pytest.fixture
def db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from rubik_core.database import eos  # noqa: F401 — registers the eos_* tables
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
def _systems_only_rosters(monkeypatch):
    from backend.tests._rosters import pin_rosters
    from rubik_core.features import FeaturePolicy

    systems = FeaturePolicy(
        departments=frozenset({"systems"}), exec_allowed=False, admin_allowed=True
    )
    pin_rosters(
        monkeypatch,
        {
            key: systems
            for key in (
                "sales_call_capture",
                "sales_cockpit",
                "sales_cockpit_manager",
                "sales_cockpit_supervisor",
                "sales_cockpit_exec",
            )
        },
    )


@pytest.fixture(autouse=True)
def flags(monkeypatch):
    monkeypatch.setattr(settings, "CCS_ENABLED", True)
    monkeypatch.setattr(settings, "COCKPIT_ENABLED", True)


@pytest.fixture(autouse=True)
def no_hubspot(monkeypatch):
    """No cockpit read reaches HubSpot. Labels degrade; the page does not."""
    from backend.routers import cockpit as cockpit_mod

    monkeypatch.setattr(
        "backend.routers.cockpit._crm_context",
        lambda **_kw: cockpit_mod._Crm(available=False, reason="crm_unavailable"),
    )


# ── Row builders ──────────────────────────────────────────────────────────────


def _commitment(db, *, owner: str, deal: str | None = DEAL) -> Commitment:
    now = datetime.utcnow()
    row = Commitment(
        id=uuid.uuid4().hex,
        kind="sow",
        hubspot_deal_id=deal,
        hubspot_company_id=COMPANY,
        owner_email=owner,
        opened_at=now - timedelta(days=6),
        opened_source="call",
        sow_kind="standard",
        sla_hours=24,
        breach_at=now - timedelta(days=3),
        state="breached",
    )
    row.refresh_live_scope_flags()
    db.add(row)
    db.commit()
    return row


def _hosted_call(db, *, ae: str, deal: str | None = DEAL) -> tuple[str, str]:
    """One captured call by ``ae``, returning (transcript entry id, item id).

    Both ids are legitimate ``evidence_ref`` values —
    ``stage_lag._ccs_signal_evidence`` writes ``item.evidence_entry_id or
    item.id`` — so the resolver has to reach the same host through either.
    """
    session = CallSession(
        id=uuid.uuid4().hex,
        ae_email=ae,
        hubspot_company_id=COMPANY,
        hubspot_deal_id=deal,
        capture_consent="granted",
        status="wrapped",
        origin=CALL_ORIGIN_ASSISTANT,
        started_at=datetime.utcnow() - timedelta(hours=3),
        ended_at=datetime.utcnow() - timedelta(hours=2),
    )
    db.add(session)
    db.commit()

    entry = CallTranscriptEntry(
        id=uuid.uuid4().hex,
        call_session_id=session.id,
        source="meet",
        external_id=f"ext-{uuid.uuid4().hex[:6]}",
        speaker_name="Client",
        text=QUOTE,
        sequence=1,
    )
    extraction = CallExtraction(
        id=uuid.uuid4().hex,
        call_session_id=session.id,
        model="gemini-test",
        prompt_version="ccs-extract-v1",
        schema_version="1",
        raw_output={},
    )
    db.add_all([entry, extraction])
    db.commit()

    item = CallExtractionItem(
        id=uuid.uuid4().hex,
        extraction_id=extraction.id,
        call_session_id=session.id,
        kind="stage_signal",
        proposed_value="sow_requested",
        evidence_text=QUOTE,
        evidence_entry_id=entry.id,
        confidence=0.9,
        status="proposed",
    )
    db.add(item)
    db.commit()
    return entry.id, item.id


def _lag(db, *, evidence_ref: str | None, deal: str = DEAL) -> CommitmentStageLag:
    row = CommitmentStageLag(
        id=uuid.uuid4().hex,
        hubspot_deal_id=deal,
        hubspot_stage_id="stage-1",
        evidence_stage_id="stage-2",
        evidence_text=QUOTE,
        evidence_ref=evidence_ref,
        confidence=0.9,
        status="open",
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def chart(db):
    """The Accountability Chart — the only team relation Cubie has.

    Marin leads the narrowed department; Parker and Reese are seated under Marin.
    Without this the manager control below resolves to a team of one and proves
    nothing. The seats carry ``NARROWED_DEPARTMENT`` because
    ``_authz.led_depts`` intersects Marin's ``managed_departments`` with the
    departments people are actually SEATED in — leading a team on paper is not
    enough, and a "Sales" seat under a Systems-only roster resolves to nobody.
    """
    from rubik_core.database.eos import Person, Seat

    marin_seat = Seat(
        department_name=NARROWED_DEPARTMENT,
        title="Sales Manager",
        is_leadership=True,
        created_by="seed",
    )
    db.add(marin_seat)
    db.commit()
    db.refresh(marin_seat)

    seats = {MANAGER: marin_seat}
    for email in (AE, CO_SELLER):
        seat = Seat(
            department_name=NARROWED_DEPARTMENT,
            title="AE",
            reports_to_id=marin_seat.id,
            created_by="seed",
        )
        db.add(seat)
        db.commit()
        db.refresh(seat)
        seats[email] = seat

    for email, seat in seats.items():
        db.add(
            Person(
                first_name=email.split("@")[0].title(),
                last_name="Doe",
                email=email,
                seat_id=seat.id,
                created_by="seed",
            )
        )
    db.commit()
    return db


# ═══ 1. My Day — the quote belongs to the CALL HOST, not the deal ════════════


def _hygiene_items(res) -> list[dict]:
    assert res.status_code == 200, res.text
    return res.json()["blocks"]["pipeline_hygiene"]["items"]


@pytest.mark.parametrize("ref_kind", ["entry", "item"])
def test_a_co_sellers_quote_is_not_shown_to_the_other_ae(db, ref_kind):
    """The regression: AE A holds the commitment, AE B hosted the call.

    Parker holds the only live commitment on the deal, so the lag is attributable
    to Parker and the ROW is rightly on Parker's My Day. Reese hosted the call the
    quote came from. Before the fix the gate was handed Parker's own address as
    the "call owner" and could never deny, so Parker read a client sentence from
    a call Parker was never on. Both ``evidence_ref`` id spaces are exercised.

    Both AEs are in the narrowed department, which is if anything the harder
    case: nothing about the department separates them, so only the host
    attribution can.
    """
    _commitment(db, owner=AE)
    entry_id, item_id = _hosted_call(db, ae=CO_SELLER)
    _lag(db, evidence_ref=entry_id if ref_kind == "entry" else item_id)

    _as(AE)
    res = client.get("/api/sales/my-day")
    items = _hygiene_items(res)

    assert res.json()["transcript_policy"] == "own", res.text  # the role, not the dept
    assert len(items) == 1, items  # the row is shown …
    assert items[0].get("evidence_text") is None, items  # … the quote is not
    assert QUOTE not in res.text


@pytest.mark.parametrize("ref_kind", ["entry", "item"])
def test_the_host_still_reads_their_own_calls_quote(db, ref_kind):
    """The control that stops the fix being "hide it from everyone".

    Same shape, one difference: Parker hosted the call. The quote is the thing
    that makes a stage-lag exception convincing, so it has to survive for the
    person the access table gives it to.
    """
    _commitment(db, owner=AE)
    entry_id, item_id = _hosted_call(db, ae=AE)
    _lag(db, evidence_ref=entry_id if ref_kind == "entry" else item_id)

    _as(AE)
    res = client.get("/api/sales/my-day")
    items = _hygiene_items(res)
    assert res.json()["transcript_policy"] == "own", res.text
    assert len(items) == 1, items
    assert items[0]["evidence_text"] == QUOTE


def test_an_unattributable_evidence_ref_shows_no_quote(db):
    """No call resolves behind the ref → unknown provenance → no text.

    A message id (the non-CCS evidence lane) lands here, as does a dangling ref.
    Deny is the safe direction and the row still renders.
    """
    _commitment(db, owner=AE)
    _lag(db, evidence_ref="msg-9001")

    _as(AE)
    res = client.get("/api/sales/my-day")
    items = _hygiene_items(res)
    assert len(items) == 1, items
    assert items[0].get("evidence_text") is None, items
    assert QUOTE not in res.text


def test_my_day_keeps_a_call_visible_while_extraction_retries(db):
    session = CallSession(
        id=uuid.uuid4().hex,
        ae_email=AE,
        capture_consent="granted",
        status="transcript_ready",
        origin=CALL_ORIGIN_ASSISTANT,
        started_at=datetime.utcnow() - timedelta(hours=3),
        ended_at=datetime.utcnow() - timedelta(hours=2),
    )
    db.add(session)
    db.commit()

    _as(AE)
    res = client.get("/api/sales/my-day")

    assert res.status_code == 200, res.text
    items = res.json()["blocks"]["wrapups_pending"]["items"]
    assert len(items) == 1
    assert items[0]["id"] == session.id
    assert items[0]["subtitle"] == (
        "AI suggestions being prepared; your call is ready to review"
    )
    assert items[0]["primary_action"]["kind"] == "review_wrapup"

    session.attrs = {"extraction_retry": {"failed_at": "2026-10-30T09:00:00"}}
    db.commit()
    retrying = client.get("/api/sales/my-day")
    assert retrying.status_code == 200
    assert retrying.json()["blocks"]["wrapups_pending"]["items"][0]["subtitle"] == (
        "AI suggestions retrying; your call is ready to review"
    )


# ═══ 2. The mutations — apply / reject gate the field too ════════════════════


@pytest.fixture
def hubspot_write(monkeypatch):
    """``HubSpotWriteClient`` for ``/apply``, accepting every stage write."""

    class _Fake:
        def __init__(self, *a, **kw):
            self.updates: list[dict] = []

        def update_deal(self, deal_id, properties, *, acting_owner_email=None):
            self.updates.append({"deal": deal_id, **properties})
            return {"id": deal_id}

    fake = _Fake()
    monkeypatch.setattr(
        "rubik_core.retrieval.hubspot_write.HubSpotWriteClient", lambda *a, **kw: fake
    )
    return fake


def _quote_for(db, lag_id: str, user: UserProfile, team_emails: frozenset[str]) -> str:
    """What ``_stage_lag_out`` would build for ``user`` on this exact row.

    The anti-vacuity probe. A denial assertion is only worth reading if the same
    row, through the same serializer, still yields the quote for somebody the
    access table grants it to — otherwise "no ``evidence_text`` key" could just
    mean the fixture never had one.
    """
    from backend.routers import commitments as commitments_mod

    row = db.get(CommitmentStageLag, lag_id)
    out = commitments_mod._stage_lag_out(row, db=db, user=user, team_emails=team_emails)
    return out.get("evidence_text")


def test_an_org_wide_exec_gets_no_quote_from_reject(db, chart):
    """Emery passes ``_authorize_deal`` as ``unrestricted`` and still reads nothing.

    This is the leak the fix closes: the exec row of the access table says NO
    transcript text, and the mutation response was handing over a client
    sentence to the one role designed never to see it.

    The deal is PARKER's, deliberately. Emery therefore reaches ``/reject`` for
    one reason only — ``sees_all_departments`` makes Emery's reach unrestricted —
    so if that ever stopped being true this test would 403 rather than quietly
    pass as "an ordinary Systems user was denied".
    """
    from backend.routers.eos import _authz

    _commitment(db, owner=AE)
    entry_id, _ = _hosted_call(db, ae=AE)
    lag = _lag(db, evidence_ref=entry_id)

    emery = _as(EXEC)
    assert _authz.email_scope(db, emery).unrestricted, "the leak needs this reader"
    assert cockpit_transcript_policy(emery) != "team", "the exec is not a manager"

    res = client.post(f"/api/stage-lag/{lag.id}/reject", json={"reason": "no"})
    assert res.status_code == 200, res.text
    assert "evidence_text" not in res.json()["exception"], res.text
    assert QUOTE not in res.text
    db.expire_all()
    assert db.get(CommitmentStageLag, lag.id).status == "rejected"

    # Not vacuous: the row really does carry a quote for a reader entitled to it.
    assert _quote_for(db, lag.id, _cast(MANAGER), frozenset({AE})) == QUOTE


def test_the_supervisor_gets_no_quote_from_reject(db, chart):
    """Harper's "NONE, ever" holds on the mutation response as well.

    Harper reaches ``/reject`` only by OWNING the commitment on this deal —
    which is the "including a deal they are working" case the access table calls
    out by name, and the only reason ``_authorize_deal`` lets Harper through with
    an ``own`` reach. The quote is from Parker's call, and being on the DEAL grants
    nothing about the TEXT.

    Read the module docstring before touching this: under the Systems-only
    roster the department no longer separates Harper from an AE, so the
    assertions below pin the ROLE the code resolves instead — Harper is the
    supervisor worklist's audience, is not a manager, and has a policy that is
    not "team".
    The last probe proves the serializer would still have produced the quote for
    a reader who is entitled to it, so the denial cannot be an empty fixture.
    """
    from backend.routers import commitments as commitments_mod

    _commitment(db, owner=SUPERVISOR)  # the deal Harper is working
    entry_id, _ = _hosted_call(db, ae=AE)  # somebody else's call
    lag = _lag(db, evidence_ref=entry_id)

    harper = _as(SUPERVISOR)
    assert user_can_access_cockpit_supervisor(harper), "Harper IS the worklist audience"
    assert not commitments_mod._is_cockpit_manager(harper, db), "and not a manager"
    assert cockpit_transcript_policy(harper) == "own", "never the team policy"

    res = client.post(f"/api/stage-lag/{lag.id}/reject", json={"reason": "no"})
    assert res.status_code == 200, res.text
    assert "evidence_text" not in res.json()["exception"], res.text
    assert QUOTE not in res.text

    assert _quote_for(db, lag.id, _cast(MANAGER), frozenset({AE})) == QUOTE


def test_the_call_host_still_gets_the_quote_from_reject(db, chart):
    """The control on the mutation side: the AE who was on the call reads it."""
    _commitment(db, owner=AE)
    entry_id, _ = _hosted_call(db, ae=AE)
    lag = _lag(db, evidence_ref=entry_id)

    _as(AE)
    res = client.post(f"/api/stage-lag/{lag.id}/reject", json={"reason": "no"})
    assert res.status_code == 200, res.text
    assert res.json()["exception"]["evidence_text"] == QUOTE


def test_a_manager_reads_their_teams_quote_from_reject(db, chart):
    """ "Team calls" is a grant, not just a denial — Marin keeps it.

    Marin's grant comes from ``managed_departments`` resolving to a team that
    contains Parker via the Accountability Chart, not from sitting in the
    narrowed department: every actor in this suite does that, and only Marin
    resolves to the "team" policy.
    """
    _commitment(db, owner=AE)
    entry_id, _ = _hosted_call(db, ae=AE)
    lag = _lag(db, evidence_ref=entry_id)

    marin = _as(MANAGER)
    assert cockpit_transcript_policy(marin) == "team", "the grant under test"

    res = client.post(f"/api/stage-lag/{lag.id}/reject", json={"reason": "no"})
    assert res.status_code == 200, res.text
    assert res.json()["exception"]["evidence_text"] == QUOTE


def test_an_admin_gets_no_quote_from_reject_either(db, chart):
    """Admin is an operator, not an executive — and buys no transcript text.

    The sharpest case in the file, because an admin resolves to the "team"
    policy (``user_can_access_cockpit_manager`` admits them) AND to an
    unrestricted reach. What stops the quote is ``_Reach.transcript_team_emails``
    turning ``emails is None`` into the EMPTY set rather than "everyone" — the
    rule ``backend/auth.py`` states about ``EmailScope.unrestricted``. The
    narrowed roster makes admin the only non-Systems way into the feature, so
    this is the widest door there is.
    """
    _commitment(db, owner=AE)
    entry_id, _ = _hosted_call(db, ae=AE)
    lag = _lag(db, evidence_ref=entry_id)

    admin = _as(ADMIN)
    assert cockpit_transcript_policy(admin) == "team", "the widest policy there is"

    res = client.post(f"/api/stage-lag/{lag.id}/reject", json={"reason": "no"})
    assert res.status_code == 200, res.text
    assert "evidence_text" not in res.json()["exception"], res.text
    assert QUOTE not in res.text

    assert _quote_for(db, lag.id, _cast(MANAGER), frozenset({AE})) == QUOTE


def test_apply_gates_the_quote_for_an_org_wide_exec(db, chart, hubspot_write):
    """The same fence on the endpoint that also writes to HubSpot.

    ``/apply`` is the response the exec is most likely to see, because it is the
    one they can trigger from a roll-up. The write still happens; the quote does
    not come back.
    """
    from backend.routers.eos import _authz

    _commitment(db, owner=AE)
    entry_id, _ = _hosted_call(db, ae=AE)
    lag = _lag(db, evidence_ref=entry_id)

    emery = _as(EXEC)
    assert _authz.email_scope(db, emery).unrestricted, "the leak needs this reader"

    res = client.post(f"/api/stage-lag/{lag.id}/apply", json=None)
    assert res.status_code == 200, res.text
    assert "evidence_text" not in res.json()["exception"], res.text
    assert QUOTE not in res.text
    assert hubspot_write.updates == [{"deal": DEAL, "dealstage": "stage-2"}]

    assert _quote_for(db, lag.id, _cast(MANAGER), frozenset({AE})) == QUOTE


def test_apply_returns_the_quote_to_the_call_host(db, chart, hubspot_write):
    """Control for ``/apply``: Parker was on the call, so Parker reads it back."""
    _commitment(db, owner=AE)
    entry_id, _ = _hosted_call(db, ae=AE)
    lag = _lag(db, evidence_ref=entry_id)

    _as(AE)
    res = client.post(f"/api/stage-lag/{lag.id}/apply", json=None)
    assert res.status_code == 200, res.text
    assert res.json()["exception"]["evidence_text"] == QUOTE


def test_the_stage_lag_serializer_cannot_be_called_without_a_caller(db):
    """The structural half: there is no ungated way to serialize the row.

    ``_METADATA_COLUMNS`` makes the supervisor query unable to SELECT the
    column; this makes the mutation serializer unable to build the key. Both
    beat a filter somebody has to remember.
    """
    from backend.routers import commitments as mod

    lag = _lag(db, evidence_ref=None)
    with pytest.raises(TypeError):
        mod._stage_lag_out(lag)  # type: ignore[call-arg]


# ═══ 3. Block codes stay inside the set the client declares ══════════════════


def test_a_failed_stale_deal_search_reports_a_declared_reason(db, monkeypatch):
    """HubSpot answered; the 90-day search did not. Say so in the client's words.

    ``crm_read_failed`` was not one of ``BLOCK_UNAVAILABLE_REASONS``, so the UI
    fell through to its generic "could not load" fallback and the reader lost
    the reason. The block's only source is that search, so its failure IS
    "HubSpot could not be read" here.
    """
    from backend.routers import cockpit as cockpit_mod

    monkeypatch.setattr(
        "backend.routers.cockpit._crm_context",
        lambda **_kw: cockpit_mod._Crm(
            available=True,
            reason=None,
            owner_id="owner-1",
            stale_unavailable=True,
        ),
    )
    _as(AE)
    res = client.get("/api/sales/my-day")
    assert res.status_code == 200, res.text
    block = res.json()["blocks"]["no_contact_90_days"]
    assert block["items"] == []
    assert block["unavailable_reason"] in DECLARED_BLOCK_REASONS, block
    assert block["unavailable_reason"] == "crm_unavailable"


def test_every_my_day_block_reason_is_one_the_client_declares(db, monkeypatch):
    """Whole-response sweep, so a future block cannot invent a sixth code."""
    from backend.routers import cockpit as cockpit_mod

    monkeypatch.setattr(
        "backend.routers.cockpit._crm_context",
        lambda **_kw: cockpit_mod._Crm(
            available=True, reason=None, owner_id="owner-1", stale_unavailable=True
        ),
    )
    monkeypatch.setattr(settings, "CCS_ENABLED", False)
    _as(AE)
    res = client.get("/api/sales/my-day?temperature=hot")
    assert res.status_code == 200, res.text
    reasons = {
        block.get("unavailable_reason")
        for block in res.json()["blocks"].values()
        if block.get("unavailable_reason")
    }
    assert reasons, "the fixture is meant to trip at least one block"
    assert reasons <= DECLARED_BLOCK_REASONS, reasons


# ═══ 4. The narrowed roster — the denials the re-cast creates ═══════════════
#
# Everything above had to move into Systems to keep testing a FIELD rule. These
# three prove that move was forced rather than cosmetic: the same people, in the
# departments they actually sit in, are refused at the door while an admin
# keeps the roster narrowed to Systems with the Exec-team flag off.


def test_a_sales_ae_is_locked_out_of_my_day_by_a_narrowed_roster(db):
    """Parker in Sales — the real Parker — cannot open the cockpit at all yet.

    Sales Kaizen is unproven, so ``COCKPIT_DEPARTMENTS`` is Systems-only and an
    AE gets a 403 rather than a screen. This is the strongest form of the
    transcript fence there is: the quote cannot leak from a page that never
    renders.
    """
    _commitment(db, owner=AE)
    entry_id, _ = _hosted_call(db, ae=AE)
    _lag(db, evidence_ref=entry_id)

    _as(AE, department=HOME_DEPARTMENTS[AE])
    res = client.get("/api/sales/my-day")
    assert res.status_code == 403, res.text
    assert QUOTE not in res.text


def test_a_support_supervisor_cannot_reach_the_stage_lag_mutations(db, chart):
    """Harper in Support is refused, and the exception is left OPEN.

    The 403 has to precede the CAS as well as the serializer: a denied caller
    who still consumed the one-shot claim would resolve somebody else's
    exception on their behalf.
    """
    _commitment(db, owner=SUPERVISOR)
    entry_id, _ = _hosted_call(db, ae=AE)
    lag = _lag(db, evidence_ref=entry_id)

    _as(SUPERVISOR, department=HOME_DEPARTMENTS[SUPERVISOR])
    res = client.post(f"/api/stage-lag/{lag.id}/reject", json={"reason": "no"})
    assert res.status_code == 403, res.text
    assert QUOTE not in res.text
    db.expire_all()
    assert db.get(CommitmentStageLag, lag.id).status == "open"


def test_an_exec_outside_systems_gets_neither_the_row_nor_the_quote(db, chart):
    """``is_exec`` is no longer a way in, so seniority alone reaches nothing.

    The companion to ``test_an_org_wide_exec_gets_no_quote_from_reject``: that
    one proves the exec who IS in the narrowed department gets the row and no
    quote, this one proves the exec who is not gets neither. Same wildcard, same
    ``sees_all_departments``; only the department differs, which is the whole of
    what the restriction changed.
    """
    from backend.routers.eos import _authz

    _commitment(db, owner=AE)
    entry_id, _ = _hosted_call(db, ae=AE)
    lag = _lag(db, evidence_ref=entry_id)

    emery = _as(EXEC, department=HOME_DEPARTMENTS[EXEC])
    assert _authz.email_scope(db, emery).unrestricted, "still org-wide in EOS terms"

    res = client.post(f"/api/stage-lag/{lag.id}/reject", json={"reason": "no"})
    assert res.status_code == 403, res.text
    assert QUOTE not in res.text
    db.expire_all()
    assert db.get(CommitmentStageLag, lag.id).status == "open"

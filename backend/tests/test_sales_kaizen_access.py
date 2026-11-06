"""Sales Kaizen — the access policy and the four routers' hard contracts.

Under test: ``backend/routers/cockpit.py``, ``backend/routers/commitments.py``,
``backend/routers/calls.py`` and the cockpit gates in ``backend/auth.py``. The
access table:

    Role                  Commitments             Transcript text   Aggregates
    AE (Parker)             own                     own calls         own
    Manager (Marin)       team                    team calls        team
    Supervisor (Harper)  team METADATA only      NONE, ever        none
    Exec (Emery)           counts only             NONE              org

── A NARROWED ROSTER, set as data, shapes this whole file ─────────────────────

The registry ships call capture and My Day open to everyone, the team views to
admins and the exec roll-up on the Exec-team flag (§12 pins those defaults).
Everything else here exercises the gates against a narrowed configuration an
admin could set in Admin → Feature Access: all five Sales Kaizen rosters
**Systems-only**, the Exec-team flag off, and one named guest on the four
cockpit views (``_systems_only_rosters`` installs it). The table above is the
ROLE design and still holds; the roster decides who may reach it at all.

Two consequences run through everything below:

* Every actor who has to REACH an endpoint sits in **Systems** and stands in
  for the role their name gives them — Parker is "the AE", Harper is "the
  supervisor". That is a stand-in for the narrowed roster, not the shipped
  one. Under a wider roster they move back to Sales / Support, and no test's
  meaning changes when they do.
* The restriction is only proved by the people it refuses, so the cast now
  also carries a real Sales AE, the Sales manager the roll-up was built for, a
  Support member and an org-wide exec. **Section 2b is that proof** and is the
  point of the restriction — it should be the first thing that goes red if the
  gate is loosened by accident.

Nothing here talks to HubSpot, Meet, Gmail, Slack or Vertex. Every external
boundary is faked at its client factory, patched **by string target** because
``test_assets_rbac.py`` / ``test_dam_processor.py`` ``importlib.reload()``
modules mid-suite and an object-target patch goes stale in a full-suite run.

The org chart in :func:`chart` is the load-bearing fixture. There is no
manager→team relation on ``users``; the only supervisor-of edge in Cubie is
``eos_seats.reports_to_id`` bridged to ``users`` by ``eos_people.email``, so a
team view's rows exist only because the chart is filled in. Harper's seat is
flagged ``is_leadership`` with Parker's seat reporting to it — that is what makes
Harper's worklist resolve to somebody other than Harper, and the 409 tests below
pin what happens when it does not.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth import (
    cockpit_transcript_policy,
    get_current_user,
    user_can_access_ccs,
    user_can_access_cockpit,
    user_can_access_cockpit_exec,
    user_can_read_transcript_text,
)
from backend.main import app
from backend.tests._rosters import pin_rosters
from rubik_core.call_capture.provenance import ATTR_KEY, stamp_session_source
from rubik_core.config import settings
from rubik_core.database.call_capture import (
    CallExtraction,
    CallExtractionItem,
    CallHubspotWrite,
    CallSession,
    CallTranscriptEntry,
)
from rubik_core.database.commitments import Commitment, CommitmentStageLag
from rubik_core.database.db import get_db
from rubik_core.database.schemas import UserProfile
from rubik_core.features import FeaturePolicy
from rubik_core.identifiers import CALL_ORIGIN_ASSISTANT

client = TestClient(app)

AE = "parker@acme.example"
OTHER_AE = "reese@acme.example"
MANAGER = "marin@acme.example"
SUPERVISOR = "harper@acme.example"
SUPPORT_PEER = "casey@acme.example"
EXEC = "emery@acme.example"
ADMIN = "admin@acme.example"
MARKETER = "morgan@acme.example"

# ── The people the narrowed roster shuts OUT ─────────────────────────────────
#
# These four are the feature's PERMANENT audience — a Sales AE, the Sales
# manager the roll-up was built for, a member of the supervisor's team the
# worklist was built for, and an org-wide exec. They exist here to be refused,
# because a restriction is only ever proved against the people it restricts.
# They all pass the copilot gate on the parent `/api` router (it is open to
# everyone), so every 403 they collect below is Sales Kaizen's own — see
# `_refused_by_sales_kaizen`, which asserts exactly that.
#
# When an admin widens the roster, these are the actors whose expectations flip
# from 403 to 200. They stay in the file either way: each `403` becomes the value
# the wider roster gives it, and the file goes on documenting who may reach what.
SALES_AE = "riley@acme.example"
SALES_MGR = "tara@acme.example"
SUPPORT_MEMBER = "quinn@acme.example"
ORG_EXEC = "avery@acme.example"

#: A verbatim client quote. It is seeded into every evidence column so a leak
#: can be asserted against the whole response body, not just the fields a
#: serializer happened to name.
QUOTE = "SUPERVISOR-MUST-NEVER-SEE-THIS: send the SOW by Friday or we walk."

DEAL = "deal-77"
COMPANY = "co-42"

#: The one person named on the narrowed cockpit rosters: an org-wide exec in no
#: cockpit department, admitted by name rather than by the Exec-team flag.
NAMED_GUEST = "named.guest@acme.example"


# ── Identities ────────────────────────────────────────────────────────────────


def _profile(
    email: str,
    *,
    # The narrowed roster's department, not the feature's audience: the cast
    # below stands in for its roles from here.
    department: str = "Systems",
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


#: The cast, exactly as the gates in ``backend/auth.py`` key on it: department
#: slug + ``managed_departments`` + ``is_exec`` + admin.
#:
#: Everyone above the line is a **Systems** user standing in for the role their
#: name gives them, because the roster an admin narrowed is ``{"systems"}`` and
#: nobody else can reach an endpoint to be tested against one. Read "Parker the
#: AE", not "Parker of the Sales team". Under a wider roster Parker and Reese sit
#: in Sales, Marin manages Sales, Harper and Casey sit in Support, and Emery is on
#: the Exec flag with no department of their own; moving them there is a
#: one-line-per-actor edit that changes no assertion in sections 3 to 11.
#:
#: Marin manages a department rather than merely sitting in one, and that half
#: is NOT a stand-in: the manager gate reads ``managed_departments`` only, never
#: the primary department, because every AE sits in the same team as their
#: manager and a primary-department match would promote all of them.
#:
#: Emery keeps the ``is_exec`` flag even though it buys nothing here — under
#: the narrowed roster Emery reaches the exec roll-up by being in Systems, which
#: is the real reason the restriction is survivable for Emery and not for
#: ``ORG_EXEC``. Leaving the flag set is what makes that difference testable.
CAST: dict[str, dict] = {
    AE: {"department": "Systems"},
    OTHER_AE: {"department": "Systems"},
    MANAGER: {"department": "Systems", "managed": ["Systems"]},
    SUPERVISOR: {"department": "Systems"},
    SUPPORT_PEER: {"department": "Systems"},
    EXEC: {"department": "Systems", "is_exec": True},
    ADMIN: {"department": "Systems", "is_admin": True},
    MARKETER: {"department": "Marketing"},
    # ── Refused by the narrowed roster (section 2b) ──────────────────────────
    SALES_AE: {"department": "Sales"},
    SALES_MGR: {"department": "Sales", "managed": ["Sales"]},
    SUPPORT_MEMBER: {"department": "Support"},
    ORG_EXEC: {"department": "Executive", "is_exec": True, "managed": ["*"]},
}


def _as(email: str, **overrides) -> UserProfile:
    """Authenticate by overriding the dependency — never by sending a header."""
    profile = _profile(email, **{**CAST.get(email, {}), **overrides})
    app.dependency_overrides[get_current_user] = lambda: profile
    return profile


def _refused_by_sales_kaizen(res) -> str:
    """Assert a 403 came from Sales Kaizen's gate, not the copilot gate.

    Load-bearing, and the reason every denial below goes through it. All four
    cockpit routers and ``/api/calls`` are mounted under the ``core`` parent,
    which carries ``require_full_app_access`` — so a Marketing or Sales caller
    could 403 there and never reach the gate under test. That 403 looks
    identical in a status-code assertion and proves nothing about the
    restriction: widen the Sales Kaizen rosters back to everyone and a test
    that only checked ``== 403`` would still pass for MARKETER.

    Returns the detail so a caller can pin the specific message.
    """
    assert res.status_code == 403, res.text
    detail = res.json()["detail"]
    assert isinstance(detail, str), detail
    assert not detail.startswith("This area is not turned on for you"), (
        "refused by the copilot gate on the `core` router, not by the "
        f"Sales Kaizen gate under test: {detail!r}"
    )
    return detail


# ── Database ──────────────────────────────────────────────────────────────────


@pytest.fixture
def db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Registers the eos_* tables on Base.metadata; call_* / commitment_* are
    # registered by models.py's own trailing import.
    from rubik_core.database import eos  # noqa: F401
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
    """The narrowed configuration this file exercises, installed as the
    Feature Access overrides an admin would write. §12 reads the shipped
    defaults directly and is unaffected."""
    systems = frozenset({"systems"})
    ccs = FeaturePolicy(departments=systems, exec_allowed=False, admin_allowed=True)
    cockpit = FeaturePolicy(
        departments=systems,
        extra_emails=frozenset({NAMED_GUEST}),
        exec_allowed=False,
        admin_allowed=True,
    )
    pin_rosters(
        monkeypatch,
        {
            "sales_call_capture": ccs,
            "sales_cockpit": cockpit,
            "sales_cockpit_manager": cockpit,
            "sales_cockpit_supervisor": cockpit,
            "sales_cockpit_exec": cockpit,
        },
    )


@pytest.fixture(autouse=True)
def vendor_fields(monkeypatch):
    """The company's own vendor field the wrap-up tests write. Vendor fields are
    workspace connector configuration and ship empty."""
    from rubik_core.retrieval import hubspot_write
    from rubik_core.tenancy.hubspot_config import HubSpotConnectorConfig

    configured = {"fields": ("payroll_vendor",)}
    monkeypatch.setattr(
        hubspot_write,
        "get_hubspot_config",
        lambda: HubSpotConnectorConfig(ccs_vendor_fields_default=configured["fields"]),
    )
    return lambda *fields: configured.update(fields=fields)


@pytest.fixture(autouse=True)
def flags(monkeypatch):
    """Both master switches ON, so a test exercises the RULE and not the flag.

    The flag itself is pinned by the 404 tests, which turn it back off — a
    later ``monkeypatch.setattr`` wins over this one and is reverted with it.
    """
    monkeypatch.setattr(settings, "CCS_ENABLED", True)
    monkeypatch.setattr(settings, "COCKPIT_ENABLED", True)
    monkeypatch.setattr(settings, "CCS_MEET_ENABLED", True)


@pytest.fixture(autouse=True)
def no_hubspot(monkeypatch):
    """No cockpit read reaches HubSpot. Labels degrade; the page does not.

    Patched at ``backend.routers.cockpit._crm_context`` — the handlers resolve
    the name from module globals at call time, so the string target holds.
    """
    from backend.routers import cockpit as cockpit_mod

    monkeypatch.setattr(
        "backend.routers.cockpit._crm_context",
        lambda **_kw: cockpit_mod._Crm(available=False, reason="crm_unavailable"),
    )


@pytest.fixture(autouse=True)
def workspace_crm_sources(monkeypatch):
    """The fixture company and deal belong to this workspace's pipeline.

    Exercise the real visibility proof against a small portal fake. Unknown
    source IDs remain unowned, so this fixture cannot turn an ownership failure
    into blanket access.
    """
    from rubik_core.retrieval import hubspot_api
    from rubik_core.tenancy import hubspot_config
    from rubik_core.tenancy.hubspot_config import HubSpotConnectorConfig

    pipeline = "workspace-pipeline"
    monkeypatch.setattr(
        hubspot_config,
        "get_hubspot_config",
        lambda: HubSpotConnectorConfig(pipeline_ids=(pipeline,)),
    )

    class Portal:
        def fully_owned_deal_associations(self, kind, ids):
            assert kind in {"companies", "contacts"}
            if kind == "companies" and COMPANY in ids:
                return {COMPANY: [DEAL]}
            return {}

        def batch_read(self, kind, ids, properties):
            assert kind == "deals" and properties == ["pipeline"]
            return [
                {"id": DEAL, "properties": {"pipeline": pipeline}}
                for value in ids
                if value == DEAL
            ]

    monkeypatch.setattr(hubspot_api, "HubSpotAPIClient", Portal)


# ── Row builders ──────────────────────────────────────────────────────────────


def _commitment(
    db,
    *,
    owner: str = AE,
    kind: str = "sow",
    state: str = "breached",
    deal: str | None = DEAL,
    company: str | None = COMPANY,
    evidence: str | None = QUOTE,
    age_days: int = 6,
    accepted_reason: str | None = None,
) -> Commitment:
    now = datetime.utcnow()
    row = Commitment(
        id=uuid.uuid4().hex,
        kind=kind,
        hubspot_deal_id=deal,
        hubspot_company_id=company,
        owner_email=owner,
        opened_at=now - timedelta(days=age_days),
        opened_source="call",
        opened_evidence_ref="entry-1",
        opened_evidence_text=evidence,
        sow_kind="standard",
        sla_hours=24,
        breach_at=now - timedelta(days=max(age_days - 3, 1)),
        state=state,
        accepted_reason=accepted_reason,
    )
    # The two enforced projections; the biconditional CHECKs reject the row
    # without them.
    row.refresh_live_scope_flags()
    db.add(row)
    db.commit()
    return row


def _session(
    db,
    *,
    ae: str = AE,
    status: str = "extracted",
    consent: str = "granted",
    company: str | None = COMPANY,
    deal: str | None = DEAL,
) -> CallSession:
    row = CallSession(
        id=uuid.uuid4().hex,
        ae_email=ae,
        hubspot_company_id=company,
        hubspot_deal_id=deal,
        capture_consent=consent,
        status=status,
        origin=CALL_ORIGIN_ASSISTANT,
        started_at=datetime.utcnow() - timedelta(hours=3),
        ended_at=datetime.utcnow() - timedelta(hours=2),
        attrs={
            "company_name": "Acme Supply" if company else None,
            ATTR_KEY: stamp_session_source(company, None, deal),
        },
    )
    db.add(row)
    db.commit()
    return row


def _item(
    db,
    session: CallSession,
    *,
    kind: str = "vendor_field",
    prop: str | None = "payroll_vendor",
    proposed: str = "New Vendor",
    current: str | None = None,
    is_overwrite: bool = False,
    evidence: str = QUOTE,
) -> CallExtractionItem:
    extraction = (
        db.query(CallExtraction)
        .filter(CallExtraction.call_session_id == session.id)
        .first()
    )
    if extraction is None:
        extraction = CallExtraction(
            id=uuid.uuid4().hex,
            call_session_id=session.id,
            model="gemini-test",
            prompt_version="ccs-extract-v1",
            schema_version="1",
            raw_output={},
        )
        db.add(extraction)
        db.commit()
    db.add(
        CallTranscriptEntry(
            id="entry-1",
            call_session_id=session.id,
            source="meet",
            external_id="ext-1",
            speaker_name="Client",
            text=evidence,
            sequence=1,
        )
    )
    item = CallExtractionItem(
        id=uuid.uuid4().hex,
        extraction_id=extraction.id,
        call_session_id=session.id,
        kind=kind,
        target_object="companies" if kind == "vendor_field" else None,
        target_id=session.hubspot_company_id if kind == "vendor_field" else None,
        target_property=prop,
        proposed_value=proposed,
        current_value=current,
        evidence_text=evidence,
        evidence_entry_id="entry-1",
        is_overwrite=is_overwrite,
        confidence=0.9,
        status="proposed",
    )
    db.add(item)
    db.commit()
    return item


@pytest.fixture
def chart(db):
    """The Accountability Chart, which is the only team relation Cubie has.

    Marin leads Sales (seat ``is_leadership``); Parker and Reese are seated in
    Sales under Marin. Harper holds a Support leadership seat with Parker's seat
    reporting to it, and Casey is Harper's Support teammate — so Harper's
    worklist resolves to more than Harper alone, which is the precondition for
    it returning anything at all.

    The seats keep their REAL departments ("Sales", "Support") while the profiles
    in :data:`CAST` say "Systems", and the mismatch is deliberate rather than
    an oversight. These are two different axes and only one of them moved: the
    Accountability Chart answers "whose rows may I see" (``_authz``), the
    ``users`` department answers "may I open this page at all"
    (``backend/auth.py``), and the narrowed roster touches only the
    second. Slugging the seats into Systems as well would delete the only
    manager→AE and supervisor→AE edges this file has and turn every team view
    into a 409, which would look like the restriction breaking team scoping —
    it does not.
    """
    from rubik_core.database.eos import Person, Seat

    marin_seat = Seat(
        department_name="Sales",
        title="Sales Manager",
        is_leadership=True,
        created_by="seed",
    )
    harper_seat = Seat(
        department_name="Support",
        title="Support Supervisor",
        is_leadership=True,
        created_by="seed",
    )
    db.add_all([marin_seat, harper_seat])
    db.commit()
    db.refresh(marin_seat)
    db.refresh(harper_seat)

    parker_seat = Seat(
        department_name="Sales",
        title="AE",
        reports_to_id=harper_seat.id,
        created_by="seed",
    )
    reese_seat = Seat(
        department_name="Sales",
        title="AE",
        reports_to_id=marin_seat.id,
        created_by="seed",
    )
    casey_seat = Seat(
        department_name="Support",
        title="Support Analyst",
        reports_to_id=harper_seat.id,
        created_by="seed",
    )
    db.add_all([parker_seat, reese_seat, casey_seat])
    db.commit()
    for seat in (parker_seat, reese_seat, casey_seat):
        db.refresh(seat)

    seats = {
        AE: parker_seat,
        OTHER_AE: reese_seat,
        MANAGER: marin_seat,
        SUPERVISOR: harper_seat,
        SUPPORT_PEER: casey_seat,
    }
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


# ── Fakes for the external boundaries ────────────────────────────────────────


class _FakeMeet:
    """``rubik_core.tools.meet.GoogleMeetClient``, minus Google."""

    created: list[dict] = []

    def __init__(self, user_email: str):
        self.user_email = user_email

    def create_space(self, *, auto_transcribe: bool, **kw) -> dict:
        _FakeMeet.created.append({"auto_transcribe": auto_transcribe, **kw})
        return {
            "name": f"spaces/{uuid.uuid4().hex[:8]}",
            "meetingCode": "abc-defg-hij",
            "meetingUri": "https://meet.google.com/abc-defg-hij",
            "config": {},
        }


class _FakeHubSpot:
    """``HubSpotWriteClient``, counting every mutation it is asked to make."""

    def __init__(self, *, live: dict | None = None):
        self.calls: list[dict] = []
        self.company_patches: list[dict] = []
        self.live = live or {}

    # reads
    def get_object(self, obj: str, obj_id: str, props) -> dict:
        return {"id": obj_id, "properties": dict(self.live)}

    def resolve_owner_id(self, email: str) -> str | None:
        return "owner-1"

    # writes
    def create_call(self, properties: dict, **kw) -> dict:
        self.calls.append(properties)
        return {"id": f"call-{len(self.calls)}"}

    def update_company(self, company_id: str, properties: dict) -> dict:
        self.company_patches.append(properties)
        return {"id": company_id}


@pytest.fixture
def hubspot(monkeypatch):
    fake = _FakeHubSpot(live={"payroll_vendor": "Old Vendor"})
    monkeypatch.setattr(
        "backend.routers.calls.HubSpotWriteClient", lambda *a, **kw: fake
    )
    return fake


@pytest.fixture
def meet(monkeypatch):
    _FakeMeet.created = []
    monkeypatch.setattr("rubik_core.tools.meet.GoogleMeetClient", _FakeMeet)
    return _FakeMeet


# ═══ 1. Feature flags: every router 404s when its switch is off ═══════════════


COCKPIT_READS = (
    "/api/sales/my-day",
    "/api/sales/manager",
    "/api/sales/supervisor",
    "/api/sales/exec",
)

#: Every Call Capture endpoint, as (method, path, body) — ``/api/calls`` plus
#: the ``/api/scripts`` authoring router, which shares this feature's audience
#: gate and its flag. All twelve carry ``require_ccs``; section 2b walks this
#: same table to prove the audience gate is on all of them and not just the one
#: somebody remembered.
#:
#: ``/scheduled`` and ``/companies`` are the calendar entry point's two reads.
#: They are here rather than only in their own test file for the reason this
#: table exists at all: the gate has to be on the endpoint somebody adds next,
#: and the only way that stays true is if adding one to the router without
#: adding it here leaves the row uncovered — so the table is the checklist.
#: ``/companies`` is listed WITHOUT a query so the walk cannot reach HubSpot;
#: an empty ``q`` is an empty list by design, which is enough to prove a gate.
#:
#: **The bodies must VALIDATE.** FastAPI solves dependencies, then parses the
#: body, and only then calls the handler — so a row whose body 422s never
#: reaches the in-body ``CCS_ENABLED`` check and quietly stops testing the flag.
#: That is why ``POST /api/scripts`` carries a complete draft and why
#: ``/approve`` carries none (it takes no body at all: ``approved_by`` is
#: stamped from the authenticated caller, never read from the request).
#:
#: The four ``/api/scripts`` MUTATIONS carry a second, narrower authoring gate
#: (``auth.user_can_access_cockpit_supervisor`` — Harper's roster). It admits
#: exactly who ``require_ccs`` admits while an admin's narrowed roster collapses
#: both to ``{"systems"}``, which is why the Systems AE below reaches them. Under
#: a Support supervisor roster those four rows stop being reachable by a plain AE and
#: belong in a table of their own — ``test_call_scripts_router.py`` pins that.
CALL_ENDPOINTS = (
    ("post", "/api/calls/start", {"capture_consent": "granted"}),
    ("get", "/api/calls/pending", None),
    ("get", "/api/calls/scheduled", None),
    ("get", "/api/calls/companies", None),
    ("get", "/api/calls/abc/wrapup", None),
    ("post", "/api/calls/abc/wrapup/approve", {"decisions": []}),
    ("get", "/api/scripts", None),
    ("get", "/api/scripts/id/abc", None),
    ("get", "/api/scripts/implementation_plan", None),
    (
        "post",
        "/api/scripts",
        {"product_key": "gate_probe", "name": "Gate probe", "items": []},
    ),
    ("put", "/api/scripts/abc", {"name": "Gate probe"}),
    ("post", "/api/scripts/abc/approve", None),
    ("delete", "/api/scripts/abc", None),
)


def _call(method: str, path: str, body):
    return getattr(client, method)(path, **({"json": body} if body is not None else {}))


@pytest.mark.parametrize("path", COCKPIT_READS)
def test_cockpit_reads_404_when_the_flag_is_off(db, monkeypatch, path):
    """COCKPIT_ENABLED off → 404 on all four surfaces, for an admin included."""
    monkeypatch.setattr(settings, "COCKPIT_ENABLED", False)
    _as(ADMIN)
    res = client.get(path)
    assert res.status_code == 404, res.text
    assert "not enabled" in res.json()["detail"].lower(), res.text


@pytest.mark.parametrize(
    "path,body",
    [
        (
            "/api/commitments/x/eta",
            {"promised_at": "2026-09-01T00:00:00", "reason": "r"},
        ),
        ("/api/commitments/x/close", {}),
        ("/api/commitments/x/accept", {"reason": "r"}),
        ("/api/commitments/x/nudge", None),
        ("/api/stage-lag/x/apply", None),
        ("/api/stage-lag/x/reject", {}),
    ],
)
def test_commitment_mutations_404_when_the_flag_is_off(db, monkeypatch, path, body):
    """The mutation router carries the same flag — no half-provisioned writes."""
    monkeypatch.setattr(settings, "COCKPIT_ENABLED", False)
    _as(ADMIN)
    res = client.post(path, json=body)
    assert res.status_code == 404, res.text


@pytest.mark.parametrize("method,path,body", CALL_ENDPOINTS)
def test_call_capture_404s_when_ccs_is_off(db, monkeypatch, method, path, body):
    """CCS_ENABLED off → 404, and never a "Start call" button that half-works.

    The caller is inside the narrowed roster, which makes this a test of the
    flag. Section 2b also pins the 404 precedence for callers outside it.
    """
    monkeypatch.setattr(settings, "CCS_ENABLED", False)
    _as(AE)
    res = _call(method, path, body)
    assert res.status_code == 404, res.text
    assert "not enabled" in res.json()["detail"].lower(), res.text


def test_the_two_flags_are_independent(db, monkeypatch, chart):
    """Cockpit off does not close call capture, and vice versa.

    Each feature ships behind its own switch; one flag closing both
    would make a partial rollout impossible.
    """
    monkeypatch.setattr(settings, "COCKPIT_ENABLED", False)
    _as(AE)
    assert client.get("/api/calls/pending").status_code == 200
    assert client.get("/api/sales/my-day").status_code == 404

    monkeypatch.setattr(settings, "COCKPIT_ENABLED", True)
    monkeypatch.setattr(settings, "CCS_ENABLED", False)
    assert client.get("/api/calls/pending").status_code == 404
    assert client.get("/api/sales/my-day").status_code == 200


# ═══ 2. Who may open which surface ═══════════════════════════════════════════


@pytest.mark.parametrize(
    "email,expected",
    [
        (AE, 200),
        (SUPERVISOR, 200),
        (MANAGER, 200),
        (ADMIN, 200),
        (EXEC, 200),
        (MARKETER, 403),
        # The feature's wider audience, refused by the narrowed roster.
        # These three flip to 200 when an admin widens it.
        (SALES_AE, 403),
        (SUPPORT_MEMBER, 403),
        (ORG_EXEC, 403),
    ],
)
def test_my_day_is_open_to_the_narrowed_roster_only(db, chart, email, expected):
    """The AE surface is Systems + admin, and nobody else, under the narrowed
    roster.

    Widened to Sales + Support + Systems + admin + Exec, Marketing is still
    refused — it has the copilot but owns no commitments and hosts no client
    calls, so access there would be access without a use. Marketing is
    therefore the one row here that does NOT change when the roster widens,
    which is what makes it worth keeping alongside the three that do.
    """
    _as(email)
    res = client.get("/api/sales/my-day")
    if expected == 403:
        assert _refused_by_sales_kaizen(res) == (
            "The Sales Cockpit is not turned on for you. Ask your admin."
        )
        return
    assert res.status_code == expected, res.text


@pytest.mark.parametrize(
    "email,expected",
    [
        (MANAGER, 200),
        (ADMIN, 200),
        (AE, 403),
        (SUPERVISOR, 403),
        (EXEC, 403),
        # The manager the roll-up was BUILT for, refused for managing Sales
        # rather than a team on the narrowed roster. This is the sharpest
        # proof the restriction is real — the manager rule cannot explain this
        # 403, only the narrowed roster can.
        (SALES_MGR, 403),
        (SALES_AE, 403),
        (ORG_EXEC, 403),
    ],
)
def test_only_a_manager_of_a_rostered_team_opens_the_team_rollup(
    db, chart, email, expected
):
    """``is_exec`` grants NOTHING on the manager view, and that is deliberate.

    The roll-up carries per-AE breaches with reasons and team transcript text;
    The design gives the exec counts and no text. Letting seniority in this
    door would hand Emery the per-person drill-down the design removed.
    An AE is refused for the mirror-image reason: every AE sits in the same
    team as their manager, so only ``managed_departments`` may promote one.

    Both of those survive the restriction unchanged — this is the one surface
    the narrowed roster did not flatten, because it keys on
    ``managed_departments`` and not on the caller's own department. What the
    restriction adds is the ``SALES_MGR`` row: managing a team is necessary and
    no longer sufficient, the team has to be on the narrowed roster too.
    """
    _as(email)
    res = client.get("/api/sales/manager")
    if expected == 403:
        assert _refused_by_sales_kaizen(res) == (
            "The team roll-up is not turned on for you. Ask your admin."
        )
        return
    assert res.status_code == expected, res.text


@pytest.mark.parametrize(
    "email,expected,code",
    [
        (SUPERVISOR, 200, None),
        (ADMIN, 200, None),
        # Admitted by the gate and refused by the CHART: the admin's seat has no
        # reports, so their worklist would be a believable empty list. 409, never
        # 403 — "you may not" and "Cubie cannot tell" are different sentences
        # with different fixes, and the restriction did not blur them.
        (SUPPORT_PEER, 409, "team_unresolved"),
        # Under the narrowed roster the worklist is Systems-wide, so the AE,
        # the manager and the exec all reach the GATE. Two of them are then
        # stopped by the chart, for two different reasons that stay
        # distinguishable: Parker has a seat that leads nobody, Emery has no
        # `eos_people` row at all. Under a Support-only roster all three are 403.
        (AE, 409, "team_unresolved"),
        (MANAGER, 200, None),
        (EXEC, 409, "org_record_missing"),
        # The supervisor's own team, refused. This is the surface built for
        # Harper and a real Support member cannot open it.
        (SUPPORT_MEMBER, 403, None),
        (SALES_AE, 403, None),
        (MARKETER, 403, None),
        (ORG_EXEC, 403, None),
    ],
)
def test_only_the_narrowed_roster_opens_the_supervisor_worklist(
    db, chart, email, expected, code
):
    """Three questions, and the status code says which one failed.

    WHO may open the page (``backend/auth.py``, 403), WHOSE rows the
    Accountability Chart can resolve (``_authz.require_team_scope``, 409), and
    whether anything is outstanding (200). Keeping the three apart is the point
    of the whole section-8 block below, and the restriction must not collapse
    them: a refused caller has a permission problem, an unresolved one has a
    data problem, and telling them apart is how either gets fixed.

    Under a Support-only roster ``SUPPORT_MEMBER`` is 200-or-409 and AE / MANAGER /
    EXEC are 403. The message the gate returns names no department, so it reads
    the same under either roster.
    """
    _as(email)
    res = client.get("/api/sales/supervisor")
    if expected == 403:
        assert _refused_by_sales_kaizen(res) == (
            "The nudge worklist is not turned on for you. Ask your admin."
        )
        return
    assert res.status_code == expected, res.text
    if expected == 409:
        assert res.json()["detail"]["code"] == code, res.text


@pytest.mark.parametrize(
    "email,expected",
    [
        (EXEC, 200),
        (ADMIN, 200),
        # The widest consequence of the restriction: with the exec roll-up
        # narrowed to the same Systems roster, EVERY Systems
        # user reads the org-wide roll-up, not just the Exec team. Pinned here
        # deliberately rather than left to be discovered — the response is
        # three aggregate numbers with no per-person drill (section 4 proves
        # that), which is what makes it survivable for a roster of one team.
        # Under the shipped default both of these are 403 and `is_exec` is the
        # only rule.
        (AE, 200),
        (SUPERVISOR, 200),
        # An org-wide exec is not Systems. This is THE `is_exec`
        # bypass that was closed, and section 2b pins it again on its own.
        (ORG_EXEC, 403),
        (SALES_AE, 403),
        (SUPPORT_MEMBER, 403),
        (MARKETER, 403),
    ],
)
def test_only_the_narrowed_roster_opens_the_org_rollup(db, chart, email, expected):
    """Normally the one gate in the feature where ``is_exec`` IS the rule.

    Under the narrowed roster it is the department roster like every other
    surface here. Emery still reaches it — Emery is in Systems — and that is the
    only reason the restriction does not take the exec roll-up away from the
    person it was built for. An exec outside Systems loses it.
    """
    _as(email)
    res = client.get("/api/sales/exec")
    if expected == 403:
        assert _refused_by_sales_kaizen(res) == (
            "The executive roll-up is not turned on for you. Ask your admin."
        )
        return
    assert res.status_code == expected, res.text


def test_an_org_wide_exec_is_still_not_a_manager_of_a_rostered_team(db, chart):
    """``managed_departments == ["*"]`` is an Exec concept, not a team.

    The case this protects is a director who is exec over several
    departments: they read the aggregate roll-up, and the moment the wildcard
    were treated as a managed department they would also get the per-person
    drill and team transcript text.

    Cast on Emery rather than on ``ORG_EXEC`` under the narrowed roster: the
    wildcard rule is only observable through a caller who can reach the
    endpoints at all, and an exec outside Systems is refused before it is ever
    consulted. ``*``
    slugifies to nothing, so it matches no roster on either side of the gate —
    which is exactly what this asserts.
    """
    _as(EXEC, managed=["*"])
    assert client.get("/api/sales/exec").status_code == 200
    assert client.get("/api/sales/manager").status_code == 403
    exec_profile = _profile(EXEC, **{**CAST[EXEC], "managed": ["*"]})
    assert cockpit_transcript_policy(exec_profile) != "team"


def test_an_exec_who_also_manages_a_rostered_team_passes_through_the_team_not_seniority(
    db, chart
):
    """ "Team Manager" is decoupled from ``is_exec`` everywhere, including here.

    The contrast that makes this meaningful is the ``EXEC`` row in
    ``test_only_a_manager_of_a_rostered_team_opens_the_team_rollup``: the same
    person, the same flag, 403 — until they manage the team.
    """
    _as(EXEC, managed=["Systems"])
    assert client.get("/api/sales/manager").status_code in (200, 409)


# ═══ 2b. THE NARROWED ROSTER ═════════════════════════════════════════════════
#
# The reason this section exists: an admin can keep Sales Kaizen away from AEs
# until it has proved itself, by narrowing every surface's roster in Admin →
# Feature Access to {"systems"} (``_systems_only_rosters``). Section 2 above
# pins the resulting access table surface by surface. This section pins the
# RESTRICTION ITSELF, from the outside in: the four kinds of caller it exists
# to stop, and the one kind it must not stop.
#
# A failure here is never fixed by deleting the test or loosening the roster;
# fix whatever made it fail. Every test here is a 403 that somebody could
# make disappear with a one-line "fix".


@pytest.mark.parametrize("path", COCKPIT_READS)
def test_a_sales_user_is_refused_every_cockpit_surface(db, chart, path):
    """The AE this feature was designed for cannot open any of it yet.

    All four surfaces, because the restriction is only worth anything if it
    holds on the one somebody forgot: a gate applied to three of four routers
    reads as "restricted" in a diff and is not.

    Only the ``my-day`` row is restriction-SPECIFIC — a plain Sales AE is
    refused the manager, supervisor and exec surfaces under the end-state
    rosters too, since they manage nothing, are not Support, and are not an
    exec. The other three are here as the belt-and-braces "nothing leaks" sweep;
    the test that isolates the restriction on the manager surface is the next one
    down, where the caller satisfies the permanent rule in full.
    """
    _as(SALES_AE)
    _refused_by_sales_kaizen(client.get(path))


def test_the_sales_manager_the_rollup_was_built_for_is_refused(db, chart):
    """Managing a team is necessary and, under a narrowed roster, not sufficient.

    ``SALES_MGR`` has "Sales" in ``managed_departments`` — they satisfy the
    manager rule completely. The only thing refusing them is the narrowed
    roster, which makes them the cleanest single proof that the restriction is
    doing the work and not some pre-existing rule.
    """
    _as(SALES_MGR)
    assert _refused_by_sales_kaizen(client.get("/api/sales/manager")) == (
        "The team roll-up is not turned on for you. Ask your admin."
    )
    assert _refused_by_sales_kaizen(client.get("/api/sales/my-day")) == (
        "The Sales Cockpit is not turned on for you. Ask your admin."
    )


def test_a_support_user_is_refused_the_supervisor_worklist(db, chart):
    """The surface built for Harper, closed to Harper's own team.

    Worth stating plainly because it is the most surprising thing the
    restriction does: the surface exists for Harper's team and that team
    cannot open it yet. The 403 says so in those words, so the person most
    likely to hit it is told why rather than left guessing.
    """
    _as(SUPPORT_MEMBER)
    assert _refused_by_sales_kaizen(client.get("/api/sales/supervisor")) == (
        "The nudge worklist is not turned on for you. Ask your admin."
    )


def test_an_org_wide_exec_is_refused_the_ae_view_and_the_exec_rollup(db, chart):
    """THE ``is_exec`` bypass that was closed, and the one most likely to reopen.

    ``user_can_access_cockpit`` and ``user_can_access_cockpit_exec`` both used
    to admit on the Exec-team flag alone. The narrowed roster switches the flag
    off, because an org-wide exec is not Systems and the whole point of the
    restriction is that nobody outside Systems sees this yet.

    It is the likeliest accidental regression in the feature: the clause reads
    like an oversight, restoring it is one word, and the caller who would slip
    through is senior enough that nobody would question the access. So this is
    asserted twice — at the HTTP boundary, where a reopened clause shows up as
    a 200, and at the predicate, where it shows up without a router in the way.

    ``managed_departments == ["*"]`` grants nothing either: ``*`` slugifies to
    the empty string and is dropped, so the wildcard is not a department here.
    """
    profile = _profile(ORG_EXEC, **CAST[ORG_EXEC])
    assert profile.is_exec is True, "the flag has to be SET for this to prove anything"
    assert user_can_access_cockpit(profile) is False
    assert user_can_access_cockpit_exec(profile) is False

    _as(ORG_EXEC)
    assert _refused_by_sales_kaizen(client.get("/api/sales/my-day")) == (
        "The Sales Cockpit is not turned on for you. Ask your admin."
    )
    assert _refused_by_sales_kaizen(client.get("/api/sales/exec")) == (
        "The executive roll-up is not turned on for you. Ask your admin."
    )


@pytest.mark.parametrize("method,path,body", CALL_ENDPOINTS)
def test_a_sales_user_is_refused_every_call_capture_endpoint(db, method, path, body):
    """The gate that did not exist before this change.

    Call Capture had NO audience gate of its own — only the copilot gate on the
    ``core`` parent — so any copilot user could have started a recorded client
    call. ``require_ccs`` is now declared on every endpoint in
    ``CALL_ENDPOINTS``, and ``/start`` is the one that matters most: it arms
    transcription on a real client's meeting, or creates a space that does.
    """
    _as(SALES_AE)
    assert _refused_by_sales_kaizen(_call(method, path, body)) == (
        "Sales Call Capture is not turned on for you. Ask your admin."
    )


@pytest.mark.parametrize("flag,expected", [(True, 403), (False, 404)])
@pytest.mark.parametrize("method,path,body", CALL_ENDPOINTS)
def test_call_capture_kill_switch_answers_404_before_roster_403(
    db, monkeypatch, flag, expected, method, path, body
):
    """A disabled feature is unavailable, regardless of the caller's roster.

    ``feature_allows`` includes the kill switch in its verdict, so checking
    the roster first would answer a misleading 403 when the switch is off.
    Once enabled, the narrowed roster must still refuse the Sales caller.
    """
    monkeypatch.setattr(settings, "CCS_ENABLED", flag)
    _as(SALES_AE)
    res = _call(method, path, body)
    assert res.status_code == expected, res.text
    if expected == 403:
        assert _refused_by_sales_kaizen(res) == (
            "Sales Call Capture is not turned on for you. Ask your admin."
        )
    else:
        assert "not enabled" in res.json()["detail"].lower(), res.text


@pytest.mark.parametrize("method,path,body", CALL_ENDPOINTS)
def test_the_flag_still_answers_404_for_a_caller_inside_the_audience(
    db, monkeypatch, method, path, body
):
    """The other half of the ordering: an allowed user also gets 404.

    A Systems caller would pass the narrowed roster while enabled, but a
    disabled feature still reads as "not enabled here".
    """
    monkeypatch.setattr(settings, "CCS_ENABLED", False)
    _as(AE)
    res = _call(method, path, body)
    assert res.status_code == 404, res.text
    assert "not enabled" in res.json()["detail"].lower(), res.text


@pytest.mark.parametrize(
    "email,allowed",
    [
        (AE, True),
        (MANAGER, True),
        (SUPERVISOR, True),
        (EXEC, True),
        (ADMIN, True),
        (SALES_AE, False),
        (SALES_MGR, False),
        (SUPPORT_MEMBER, False),
        (MARKETER, False),
        (ORG_EXEC, False),
    ],
)
def test_user_can_access_ccs_is_the_narrowed_roster_plus_admins(email, allowed):
    """The Call Capture audience as a predicate, with no router in the way.

    Admins are in because they support the app; ``is_exec`` is not, and here
    that holds under any roster — CCS is the surface that creates a
    recording-adjacent artifact of a client conversation, so access follows the
    people who host those calls, never seniority.
    """
    assert user_can_access_ccs(_profile(email, **CAST[email])) is allowed


def test_the_restriction_does_not_lock_out_the_narrowed_roster(
    db, chart, meet, hubspot
):
    """The other half of every test above, and the one that keeps this honest.

    A restriction that also broke the narrowed team would pass every denial
    assertion in this section and ship a dead feature. So: each cockpit surface
    opened by the Systems user who plays that role, and every
    ``/api/calls`` endpoint reachable by the Systems AE.

    ``/api/calls`` is asserted as "not 403" rather than "200": three of the
    rows take a session id this test does not create, and a 404 for a missing
    row is the audience gate having let the caller through, which is the only
    thing being asked here.
    """
    for email, path in (
        (AE, "/api/sales/my-day"),
        (MANAGER, "/api/sales/manager"),
        (SUPERVISOR, "/api/sales/supervisor"),
        (EXEC, "/api/sales/exec"),
    ):
        _as(email)
        assert client.get(path).status_code == 200, f"{email} → {path}"

    _as(AE)
    for method, path, body in CALL_ENDPOINTS:
        res = _call(method, path, body)
        assert res.status_code != 403, f"{path} → {res.status_code} {res.text}"


# ═══ 3. Transcript text is a FIELD-level rule (backend/auth.py) ═══════════════


@pytest.mark.parametrize(
    "email,policy",
    [
        (AE, "own"),
        (MANAGER, "team"),
        (ADMIN, "team"),
        (SUPERVISOR, "own"),
        (EXEC, "own"),
        (MARKETER, "none"),
        # Outside the narrowed roster the policy is "none", which is
        # the field-level half of the restriction — a Sales AE cannot read even
        # their OWN call's text while the gate is closed, because the policy
        # falls out of `user_can_access_cockpit` rather than out of ownership.
        (SALES_AE, "none"),
        (ORG_EXEC, "none"),
    ],
)
def test_transcript_policy_per_role(email, policy):
    """The closed set, as ``backend/auth.py`` actually computes it.

    Note the supervisor and the exec report "own", not "none": the policy is
    about ANOTHER person's calls, and hosting a call is being the AE on it.
    Neither can read a colleague's text — the matrix below proves that — and
    the supervisor worklist selects no evidence column at all.
    """
    assert cockpit_transcript_policy(_profile(email, **CAST[email])) == policy


@pytest.mark.parametrize(
    "email,owner,team,expected",
    [
        # An AE reads their own call and nobody else's.
        (AE, AE, frozenset(), True),
        (AE, OTHER_AE, frozenset(), False),
        # A manager reads their team's, and stops at the team edge.
        (MANAGER, AE, frozenset({AE}), True),
        (MANAGER, "stranger@acme.example", frozenset({AE}), False),
        # The supervisor reads NONE of an AE's, even handed the team set.
        (SUPERVISOR, AE, frozenset({AE}), False),
        (SUPERVISOR, SUPERVISOR, frozenset({AE}), True),
        # The exec reads none either, team set or not.
        (EXEC, AE, frozenset({AE}), False),
        # An admin is an operator, not an executive: policy "team" buys nothing
        # without a resolved team, and ``EmailScope.unrestricted`` is never
        # passed here.
        (ADMIN, AE, frozenset(), False),
        # Unknown provenance is not a reason to show content.
        (MANAGER, None, frozenset({AE}), False),
        # The own-call carve-out every policy keeps does NOT survive a closed
        # gate. Outside the narrowed roster the policy is "none" and the
        # function returns before ownership is ever considered — a Sales AE
        # reads nothing, not even a call they hosted themselves.
        (SALES_AE, SALES_AE, frozenset(), False),
        (ORG_EXEC, ORG_EXEC, frozenset({AE}), False),
    ],
)
def test_user_can_read_transcript_text_matrix(email, owner, team, expected):
    assert (
        user_can_read_transcript_text(
            _profile(email, **CAST[email]), call_owner_email=owner, team_emails=team
        )
        is expected
    )


# ═══ 4. THE SUPERVISOR FENCE ═════════════════════════════════════════════════


def test_supervisor_worklist_never_returns_transcript_text(db, chart):
    """No transcript text on Harper's worklist — including a deal Harper works.

    The commitment's ``opened_evidence_text`` is populated and Harper is provably
    working the deal (as the AE on a call session against it), so a role-only
    gate would let the quote through. It does not appear anywhere in the body,
    and the same quote IS visible to Parker on Parker's own My Day — which is
    what makes this an assertion about the serializer rather than about a column
    nobody fills in.
    """
    _commitment(db, owner=AE, state="breached", evidence=QUOTE)
    _session(db, ae=SUPERVISOR, status="wrapped")  # Harper is on this deal

    _as(SUPERVISOR)
    res = client.get("/api/sales/supervisor")
    assert res.status_code == 200, res.text
    payload = res.json()
    assert payload["items"], "the fence is only meaningful with a row to fence"
    assert payload["team_size"] >= 2
    for item in payload["items"]:
        assert "evidence_text" not in item, item
    assert QUOTE not in res.text

    # Control: the AE who hosted the call does see the quote.
    _as(AE)
    own = client.get("/api/sales/my-day")
    assert own.status_code == 200, own.text
    assert QUOTE in own.text


def test_the_fence_holds_for_an_admin_whose_policy_is_team(db, chart):
    """An admin passes the supervisor gate with policy "team" and unrestricted
    row reach — and still gets no transcript text from this endpoint.

    That is the proof the rule is structural: ``_METADATA_COLUMNS`` never
    selects ``opened_evidence_text``, so there is no serializer decision left
    to get wrong.
    """
    _commitment(db, owner=AE, state="escalated", evidence=QUOTE)
    _as(ADMIN)
    res = client.get("/api/sales/supervisor")
    assert res.status_code == 200, res.text
    assert res.json()["items"], res.text
    assert QUOTE not in res.text


def test_the_exec_rollup_returns_aggregates_and_no_evidence(db, chart):
    """Emery gets numbers. No per-person drill, no quote."""
    _commitment(db, owner=AE, state="breached", evidence=QUOTE)
    _as(EXEC)
    res = client.get("/api/sales/exec")
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body) == {"tiles", "trend", "crm_available"}, body
    assert QUOTE not in res.text
    assert AE not in res.text


# ═══ 5. IDOR — one AE's rows are not another's ═══════════════════════════════


def test_another_ae_cannot_read_a_wrapup(db, chart):
    """Reese asks for Parker's wrap-up: 403, and no transcript text in the body."""
    session = _session(db)
    _item(db, session)
    _as(OTHER_AE)
    res = client.get(f"/api/calls/{session.id}/wrapup")
    assert res.status_code == 403, res.text
    assert "not your call" in res.json()["detail"]
    assert QUOTE not in res.text


def test_a_manager_reads_a_team_wrapup_but_cannot_approve_it(db, chart, hubspot):
    """Marin reads Parker's wrap-up with its evidence; only the AE approves.

    Approval writes to a client's CRM record under the AE's HubSpot owner id,
    so it is not a supervision action.
    """
    session = _session(db)
    _item(db, session)
    _as(MANAGER)
    read = client.get(f"/api/calls/{session.id}/wrapup")
    assert read.status_code == 200, read.text
    assert read.json()["session"]["can_read_transcript"] is True
    assert QUOTE in read.text

    approve = client.post(
        f"/api/calls/{session.id}/wrapup/approve", json={"decisions": []}
    )
    assert approve.status_code == 403, approve.text
    assert "only the AE" in approve.json()["detail"]
    assert hubspot.calls == []


def test_a_supervisor_reading_a_team_wrapup_gets_metadata_only(db, chart):
    """Harper can resolve the row (Parker is in Harper's scope) and gets no text.

    Two separate questions answered separately: may Harper see the ROW (yes, 403
    is not the answer for a teammate's call) and may Harper see the TEXT (never).
    """
    session = _session(db)
    _item(db, session)
    _as(SUPERVISOR)
    res = client.get(f"/api/calls/{session.id}/wrapup")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["session"]["can_read_transcript"] is False
    for group in body["groups"]:
        for item in group["items"]:
            assert item["evidence_text"] is None, item
            assert item["evidence_context"] == []
    assert QUOTE not in res.text


def test_another_ae_cannot_mutate_a_commitment(db, chart):
    """The IDOR guard lives in the claim's WHERE clause, so nothing moves."""
    row = _commitment(db, owner=AE, state="breached")
    _as(OTHER_AE)
    res = client.post(
        f"/api/commitments/{row.id}/accept", json={"reason": "not my call to make"}
    )
    assert res.status_code == 403, res.text
    assert "another AE" in res.json()["detail"]

    db.expire_all()
    after = db.get(Commitment, row.id)
    assert after.state == "breached"
    assert after.accepted_reason is None


def test_a_supervisor_may_nudge_but_not_accept_or_close(db, chart, monkeypatch):
    """Harper's reach is one button. Everything else is the AE's or Marin's."""
    row = _commitment(db, owner=AE, state="breached")
    _as(SUPERVISOR)

    accept = client.post(f"/api/commitments/{row.id}/accept", json={"reason": "ok"})
    assert accept.status_code == 403, accept.text
    close = client.post(f"/api/commitments/{row.id}/close", json={})
    assert close.status_code == 403, close.text

    sent: list[str] = []

    def _fake_nudge(db_, commitment, *, from_email, to_email):
        sent.append(to_email)
        return {"sent": True, "channel": "slack", "hubspot_task_id": "t-1"}

    monkeypatch.setattr("rubik_core.cockpit.nudge.nudge", _fake_nudge)
    nudged = client.post(f"/api/commitments/{row.id}/nudge", json=None)
    assert nudged.status_code == 200, nudged.text
    assert nudged.json()["nudge"]["sent"] is True
    assert sent == [AE]

    db.expire_all()
    assert db.get(Commitment, row.id).state == "breached"


def test_an_unattributable_deal_is_not_written_by_anyone(db, chart):
    """A stage-lag row on a deal Cubie cannot attribute is refused, not applied.

    "I can see it, so it must be mine" is exactly wrong for an unattributed
    deal, and the refusal names the fix.
    """
    lag = CommitmentStageLag(
        id=uuid.uuid4().hex,
        hubspot_deal_id="deal-orphan",
        hubspot_stage_id="stage-1",
        evidence_stage_id="stage-2",
        evidence_text=QUOTE,
        status="open",
    )
    db.add(lag)
    db.commit()
    _as(AE)
    res = client.post(f"/api/stage-lag/{lag.id}/apply", json=None)
    assert res.status_code == 403, res.text
    assert "whose deal" in res.json()["detail"]
    db.expire_all()
    assert db.get(CommitmentStageLag, lag.id).status == "open"


# ═══ 6. A reason, enforced at the API ═════════════════════════════════════════


@pytest.mark.parametrize("reason", ["", "   ", "\t\n "])
def test_accept_rejects_a_blank_reason_with_422(db, chart, reason):
    """This is the audit trail Harper keeps by hand today.

    Enforced in the handler rather than by a pydantic constraint so ``detail``
    stays a renderable sentence — a validation error's detail is a list of
    dicts, which the cockpit cannot show anyone.
    """
    row = _commitment(db, owner=AE, state="breached")
    _as(AE)
    res = client.post(f"/api/commitments/{row.id}/accept", json={"reason": reason})
    assert res.status_code == 422, res.text
    assert isinstance(res.json()["detail"], str)
    assert "reason is required" in res.json()["detail"]

    db.expire_all()
    after = db.get(Commitment, row.id)
    assert after.state == "breached" and after.accepted_reason is None


def test_accept_with_a_reason_records_it_and_only_once(db, chart):
    """The reason lands on the row, and a second acceptance 409s."""
    row = _commitment(db, owner=AE, state="breached")
    _as(AE)
    first = client.post(
        f"/api/commitments/{row.id}/accept",
        json={"reason": "client pushed the deadline"},
    )
    assert first.status_code == 200, first.text
    assert first.json()["commitment"]["state"] == "accepted"

    db.expire_all()
    assert db.get(Commitment, row.id).accepted_reason == "client pushed the deadline"

    again = client.post(f"/api/commitments/{row.id}/accept", json={"reason": "again"})
    assert again.status_code == 409, again.text
    db.expire_all()
    assert db.get(Commitment, row.id).accepted_reason == "client pushed the deadline"


def test_an_on_time_commitment_cannot_be_accepted(db, chart):
    """Acceptance records a judgement about a miss; it does not stop a clock.

    ``recompute_states`` skips ``accepted``, so an early acceptance would
    freeze a live obligation permanently.
    """
    row = _commitment(db, owner=AE, state="open")
    _as(AE)
    res = client.post(f"/api/commitments/{row.id}/accept", json={"reason": "early"})
    assert res.status_code == 409, res.text


# ═══ 7. Rule 2 at the API boundary, and rule 3 ═══════════════════════════════


def test_my_day_returns_at_most_three_items_per_block_with_a_count(db, chart):
    """No view hands one person more than three actionable rows.

    Server-side, so no later feature can route around the cap by rendering
    "just one more". Overflow is a number, never an inline expander.
    """
    from rubik_core.cockpit.digest import MAX_INAPP_ITEMS

    for n in range(7):
        _commitment(db, owner=AE, deal=f"deal-{n}", company=None, state="open")
    _as(AE)
    res = client.get("/api/sales/my-day")
    assert res.status_code == 200, res.text
    blocks = res.json()["blocks"]
    deals = blocks["deals"]
    assert len(deals["items"]) == MAX_INAPP_ITEMS == 3
    assert deals["suppressed_count"] == 4
    for name, block in blocks.items():
        assert len(block["items"]) <= MAX_INAPP_ITEMS, name


def test_every_actionable_row_carries_exactly_one_primary_action(db, chart):
    """A row with nothing to do does not belong in a worklist, and a row
    with two "primary" actions has none."""
    _commitment(db, owner=AE, kind="sow", state="breached")
    _commitment(db, owner=AE, kind="wrapup", deal="deal-w", company=None, state="open")
    session = _session(db)
    _item(db, session)

    _as(AE)
    my_day = client.get("/api/sales/my-day").json()
    rows = [item for block in my_day["blocks"].values() for item in block["items"]]
    assert rows, my_day

    _as(SUPERVISOR)
    rows += client.get("/api/sales/supervisor").json()["items"]

    for item in rows:
        action = item["primary_action"]
        assert isinstance(action, dict), item
        assert action["label"].strip(), item
        assert action["kind"].strip(), item
        # A list would be two primaries wearing one name.
        assert not isinstance(item.get("secondary_action"), list), item


def test_a_block_that_could_not_be_read_says_so(db, chart, monkeypatch):
    """An empty list must never stand in for "Cubie could not look".

    With capture off there are no sessions to owe a wrap-up on, which is a
    different sentence from "you are caught up".
    """
    monkeypatch.setattr(settings, "CCS_ENABLED", False)
    monkeypatch.setattr(settings, "COCKPIT_ENABLED", True)
    _as(AE)
    blocks = client.get("/api/sales/my-day").json()["blocks"]
    assert blocks["wrapups_pending"]["items"] == []
    assert blocks["wrapups_pending"]["unavailable_reason"] == "capture_disabled"


# ═══ 8. THE EMPTY-SCOPE TRAP ═════════════════════════════════════════════════


def test_a_missing_org_record_is_a_409_not_an_empty_worklist(db):
    """No ``eos_people`` row → 409 ``org_record_missing``, on every team view.

    An empty EOS scope silently revokes all access, and on a worklist that is
    indistinguishable from "your team has nothing outstanding" — the worst
    thing a cockpit built on "nothing outstanding is invisible" could say.
    """
    _commitment(db, owner=AE, state="breached")

    _as(SUPERVISOR)
    res = client.get("/api/sales/supervisor")
    assert res.status_code == 409, res.text
    assert res.json()["detail"]["code"] == "org_record_missing"
    assert "Accountability Chart" in res.json()["detail"]["message"]

    _as(MANAGER)
    res = client.get("/api/sales/manager")
    assert res.status_code == 409, res.text
    assert res.json()["detail"]["code"] == "org_record_missing"


def test_a_thin_chart_is_team_unresolved_not_empty(db):
    """The row exists but resolves to nobody but the caller — its own code."""
    from rubik_core.database.eos import Person

    db.add(
        Person(
            first_name="Harper",
            last_name="Doe",
            email=SUPERVISOR,
            created_by="seed",
        )
    )
    db.commit()
    _as(SUPERVISOR)
    res = client.get("/api/sales/supervisor")
    assert res.status_code == 409, res.text
    assert res.json()["detail"]["code"] == "team_unresolved"


def test_a_resolved_team_with_nothing_outstanding_is_a_200_empty_list(db, chart):
    """The distinguishable other half: a real team, genuinely quiet.

    Same caller, same endpoint, and a reader (or a UI) can tell the two apart:
    409 + code versus 200 + ``team_size``.
    """
    _as(SUPERVISOR)
    res = client.get("/api/sales/supervisor")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["items"] == []
    assert body["suppressed_count"] == 0
    assert body["team_size"] >= 2, "an empty list plus team_size is the honest pair"


def test_an_ae_with_no_org_record_still_sees_their_own_my_day(db):
    """My Day must not depend on the EOS bridge at all.

    An AE's own commitments key on ``users.email``; requiring an
    Accountability-Chart row to see them would revoke the one surface that
    always has to work.
    """
    _commitment(db, owner=AE, state="breached")
    _as(AE)
    res = client.get("/api/sales/my-day")
    assert res.status_code == 200, res.text
    assert res.json()["blocks"]["deals"]["items"], res.text
    assert res.json()["transcript_policy"] == "own"


# ═══ 9. Call capture: the operator-facing failure and the consent switch ══════


def test_start_call_with_meet_writes_switched_off_names_the_blocker(db, monkeypatch):
    """``CCS_MEET_ENABLED`` off must read as a 503 naming what to change.

    Once the space-write scopes are granted on the DWD client id, the blocker
    is the Cubie-side switch. A message saying the scopes "are not granted
    yet" would send whoever reads it to the Workspace Admin console for a grant
    that may already be there. So the message points at the Cubie-side switch
    and does NOT claim the grant is missing.
    """
    monkeypatch.setattr(settings, "CCS_MEET_ENABLED", False)
    _as(AE)
    res = client.post("/api/calls/start", json={"hubspot_company_id": COMPANY})
    assert res.status_code == 503, res.text
    detail = res.json()["detail"]
    # The one thing anyone can act on.
    assert "CCS_MEET_ENABLED" in detail
    # Still names the scopes, so a reader knows which capability is off…
    assert "meetings.space.created" in detail
    # …but says they are granted, and says so where a naive substring search for
    # the old sentence would trip.
    assert "ARE granted" in detail
    assert "not granted" not in detail
    # Nothing half-created.
    assert db.query(CallSession).count() == 0


def test_a_suppressed_call_gets_a_session_row_and_zero_transcript_entries(
    db, meet, hubspot
):
    """The FACT of the call survives; the content never
    exists — none of the three ingest writes happen."""
    _as(AE)
    res = client.post(
        "/api/calls/start",
        json={"hubspot_company_id": COMPANY, "capture_consent": "suppressed"},
    )
    assert res.status_code == 200, res.text
    session = db.get(CallSession, res.json()["call_session_id"])
    assert session is not None
    assert session.capture_consent == "suppressed"
    # The space itself is created with artifact generation OFF.
    assert meet.created[-1]["auto_transcribe"] is False

    from unittest.mock import MagicMock

    from rubik_core.call_capture import transcript_ingest

    fake_client = MagicMock()
    written = transcript_ingest.ingest(
        db, session, "conferenceRecords/c1/transcripts/t1", fake_client
    )
    assert written == 0
    assert db.query(CallTranscriptEntry).count() == 0
    fake_client.list_transcript_entries.assert_not_called()


def test_start_call_is_idempotent_inside_the_double_click_window(db, meet, hubspot):
    """Meet allows ten ``spaces.create`` per minute per user; a double-click
    must not burn two of them."""
    _as(AE)
    body = {"hubspot_company_id": COMPANY}
    first = client.post("/api/calls/start", json=body)
    second = client.post("/api/calls/start", json=body)
    assert first.status_code == second.status_code == 200
    assert first.json()["call_session_id"] == second.json()["call_session_id"]
    assert len(meet.created) == 1
    assert db.query(CallSession).count() == 1


# ═══ 10. Approve twice writes once, and the overwrite confirmation ═══════════


def test_approving_twice_performs_one_hubspot_write(db, hubspot):
    """Approving twice writes once. ``call_hubspot_writes.idempotency_key`` is the
    whole mechanism — HubSpot has no idempotency key of its own."""
    session = _session(db)
    _as(AE)
    body = {"decisions": [], "nothing_here": True}
    first = client.post(f"/api/calls/{session.id}/wrapup/approve", json=body)
    second = client.post(f"/api/calls/{session.id}/wrapup/approve", json=body)
    assert first.status_code == second.status_code == 200, second.text

    assert first.json()["call_logged"]["status"] == "written"
    assert second.json()["call_logged"]["status"] == "already_written"
    assert len(hubspot.calls) == 1, hubspot.calls

    rows = db.query(CallHubspotWrite).all()
    assert len(rows) == 1
    assert rows[0].attempts == 1
    assert rows[0].succeeded_at is not None


def test_an_overwrite_is_not_written_without_its_own_confirmation(
    db, hubspot, monkeypatch
):
    """Never blind-overwrite a non-null HubSpot value.

    ``accept: true`` alone is not enough, so "approve all" can never quietly
    replace a colleague's data. The item stays ``proposed`` and is retryable.
    """
    monkeypatch.setattr(
        "rubik_core.call_capture.hubspot_schema.allowed_values_for",
        lambda props: {p: ["New Vendor", "Old Vendor"] for p in props},
    )
    session = _session(db)
    item = _item(db, session, current="Old Vendor", is_overwrite=True)

    _as(AE)
    res = client.post(
        f"/api/calls/{session.id}/wrapup/approve",
        json={"decisions": [{"item_id": item.id, "accept": True}]},
    )
    assert res.status_code == 200, res.text
    result = res.json()["results"][0]
    assert result["status"] == "needs_confirmation"
    assert "Old Vendor" in result["message"]
    assert hubspot.company_patches == []
    db.expire_all()
    assert db.get(CallExtractionItem, item.id).status == "proposed"

    # With the per-item confirmation it goes through the same vocabulary gate
    # and writes exactly once.
    confirmed = client.post(
        f"/api/calls/{session.id}/wrapup/approve",
        json={
            "decisions": [
                {"item_id": item.id, "accept": True, "confirm_overwrite": True}
            ]
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["results"][0]["status"] == "written"
    assert hubspot.company_patches == [{"payroll_vendor": "New Vendor"}]
    db.expire_all()
    assert db.get(CallExtractionItem, item.id).status == "written"


def test_nothing_here_is_recorded_as_a_complete_wrapup(db, hubspot):
    """A wrap-up with nothing to write is COMPLETE with a count, never "skipped"."""
    from rubik_core.database.models import AuditLog

    session = _session(db)
    _as(AE)
    res = client.post(
        f"/api/calls/{session.id}/wrapup/approve",
        json={"decisions": [], "nothing_here": True},
    )
    assert res.status_code == 200, res.text
    assert res.json()["nothing_here"] is True
    assert res.json()["wrapped_at"]

    db.expire_all()
    after = db.get(CallSession, session.id)
    assert after.status == "wrapped"
    assert after.nothing_here is True
    assert after.wrapped_at is not None

    # The call is still logged in HubSpot — the record that it happened is the
    # point — and the outcome is countable by an operator.
    assert len(hubspot.calls) == 1
    assert "nothing to file" in hubspot.calls[0]["hs_call_body"]
    audit = db.query(AuditLog).filter(AuditLog.action == "CALL_WRAPUP_APPROVED").one()
    assert audit.details["nothing_here"] is True
    assert audit.details["accepted"] == 0


def test_nothing_here_cannot_be_claimed_alongside_a_filed_item(db, hubspot):
    """The "nothing here" count is a measure, so a wrap-up that filed something
    is recorded as a normal one even if the client said otherwise."""
    session = _session(db)
    item = _item(db, session, kind="stage_signal", prop=None, proposed="verbal_yes")
    _as(AE)
    res = client.post(
        f"/api/calls/{session.id}/wrapup/approve",
        json={
            "decisions": [{"item_id": item.id, "accept": True}],
            "nothing_here": True,
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["nothing_here"] is False
    assert res.json()["results"][0]["status"] == "recorded"
    db.expire_all()
    assert db.get(CallSession, session.id).nothing_here is False


# ═══ 11. The AE's edit box is the LAST gate ═══════════════════════════════════
#
# The constrained-vocabulary rule is
# enforced three times: the prompt asks for an existing option, the validator
# constrains what the model returned, and — here — the approve handler
# re-constrains whatever the AE typed over it. The first two are covered by
# test_call_capture_extraction.py; this section covers the third, which is the
# only one an end user can drive. An edit box is a convenience, not a trust
# escalation: "Northwnd Payroll" typed by a human pollutes the CRM exactly as
# much as "Northwnd Payroll" hallucinated by a model.


def _approve_edit(session, item, edited: str) -> dict:
    """One accepted decision carrying an AE edit. Returns the item's result."""
    res = client.post(
        f"/api/calls/{session.id}/wrapup/approve",
        json={
            "decisions": [{"item_id": item.id, "accept": True, "edited_value": edited}]
        },
    )
    assert res.status_code == 200, res.text
    return res.json()["results"][0]


def test_an_edited_value_outside_the_live_options_is_refused_and_nothing_is_patched(
    db, hubspot, monkeypatch
):
    """The AE types a vendor HubSpot has no option for: ``invalid``, no PATCH.

    The refusal names the typed value and lists real options, because the AE
    cannot fix a "that's not allowed" with no vocabulary attached. The item
    stays ``proposed`` and the edit is NOT persisted — a value that was refused
    must not be readable later as "what we wrote".
    """
    monkeypatch.setattr(
        "rubik_core.call_capture.hubspot_schema.allowed_values_for",
        lambda props: {p: ["Northwind Payroll", "Contoso Benefits"] for p in props},
    )
    hubspot.live = {}  # the field is empty in HubSpot, so this is not a drift case
    session = _session(db)
    item = _item(db, session, prop="payroll_vendor", proposed="Northwind Payroll")

    _as(AE)
    result = _approve_edit(session, item, "Northwnd Payroll")
    assert result["status"] == "invalid", result
    assert "Northwnd Payroll" in result["message"], result
    assert "Northwind Payroll" in result["message"], result

    assert hubspot.company_patches == [], hubspot.company_patches
    db.expire_all()
    after = db.get(CallExtractionItem, item.id)
    assert after.status == "proposed"
    assert after.edited_value is None


def test_an_edit_that_differs_only_in_case_is_snapped_to_the_hubspot_option(
    db, hubspot, monkeypatch
):
    """ "northwind payroll" is the same vendor as "Northwind Payroll", and HubSpot
    is picky.

    Option VALUES and their labels differ in case routinely, so a case-exact
    comparison would refuse the AE's correct answer. The value that goes over
    the wire is the option's own casing, and ``edited_value`` records THAT —
    the column is asked "what did Cubie write", not "what keys were pressed".
    """
    monkeypatch.setattr(
        "rubik_core.call_capture.hubspot_schema.allowed_values_for",
        lambda props: {p: ["Northwind Payroll"] for p in props},
    )
    hubspot.live = {}
    session = _session(db)
    item = _item(db, session, prop="payroll_vendor", proposed="Northwind Payroll")

    _as(AE)
    result = _approve_edit(session, item, "northwind payroll")
    assert result["status"] == "written", result
    assert hubspot.company_patches == [{"payroll_vendor": "Northwind Payroll"}]

    db.expire_all()
    after = db.get(CallExtractionItem, item.id)
    assert after.status == "written"
    assert after.edited_value == "Northwind Payroll", (
        "the snapped option, not the keystrokes"
    )


def test_a_property_with_no_options_is_free_text_and_the_edit_is_accepted(
    db, hubspot, monkeypatch, vendor_fields
):
    """An EMPTY options list means unconstrained, NOT "no valid values".

    Getting that backwards is silent and total: every non-enumeration vendor
    field on the portal (a licence number, a system name, and friends) would
    refuse every edit with a message listing "none configured", and no test
    that only exercises enumerations would notice.
    """
    vendor_fields("licence_number")
    monkeypatch.setattr(
        "rubik_core.call_capture.hubspot_schema.allowed_values_for",
        lambda props: {p: [] for p in props},
    )
    hubspot.live = {}
    session = _session(db)
    item = _item(db, session, prop="licence_number", proposed="1234567")

    _as(AE)
    result = _approve_edit(session, item, "7654321")
    assert result["status"] == "written", result
    assert hubspot.company_patches == [{"licence_number": "7654321"}]

    db.expire_all()
    assert db.get(CallExtractionItem, item.id).edited_value == "7654321"


# ═══ 12. The rosters are DATA; the client reads the server's verdict (§7.5) ══
#
# The four cockpit gates and Sales Call Capture resolve through
# ``rubik_core.features`` (Admin → Feature Access); the client reads
# ``features.sales_cockpit`` (and the three role keys) from ``/api/users/me``.
# What §7.5 still needs is (1) the access each view SHIPS with: call capture
# and My Day open to everyone, the team roll-up and the nudge worklist to
# admins, the exec roll-up on the Exec-team flag, and no department or person
# named on any of them; (2) a person named on a narrowed roster reaches the four
# cockpit views and not call capture; (3) the client is wired to every key.

_AUTH_CONTEXT = (
    Path(__file__).resolve().parents[2] / "frontend/src/context/AuthContext.tsx"
)

#: The five Sales Kaizen features, and the AuthContext boolean each feeds.
COCKPIT_FEATURES = {
    "sales_cockpit": "canAccessCockpit",
    "sales_cockpit_manager": "canAccessCockpitManager",
    "sales_cockpit_supervisor": "canAccessCockpitSupervisor",
    "sales_cockpit_exec": "canAccessCockpitExec",
}


def test_each_sales_kaizen_feature_ships_with_its_role_default():
    from rubik_core.features import feature_by_key

    ccs = feature_by_key("sales_call_capture").default()
    my_day = feature_by_key("sales_cockpit").default()
    manager = feature_by_key("sales_cockpit_manager").default()
    supervisor = feature_by_key("sales_cockpit_supervisor").default()
    exec_ = feature_by_key("sales_cockpit_exec").default()

    assert ccs.everyone and my_day.everyone
    # Narrowed later, seniority still does not open call capture.
    assert not ccs.exec_allowed
    for view in (manager, supervisor):
        assert not view.everyone and view.admin_allowed and not view.exec_allowed
    assert not exec_.everyone and exec_.exec_allowed and exec_.admin_allowed
    for key in (*COCKPIT_FEATURES, "sales_call_capture"):
        default = feature_by_key(key).default()
        assert not default.departments, key
        assert not default.extra_emails, key


def test_the_manager_view_is_judged_on_managed_departments():
    from rubik_core.features import feature_by_key

    assert feature_by_key("sales_cockpit_manager").membership == "managed_only"
    for key in ("sales_cockpit", "sales_cockpit_supervisor", "sales_cockpit_exec"):
        assert feature_by_key(key).membership == "primary_or_managed", key


def test_a_named_guest_opens_all_four_cockpit_gates_and_not_ccs():
    """What naming someone on the cockpit rows is FOR.

    An org-wide exec in no cockpit department matches no narrowed roster, and
    with the ``is_exec`` door off a name is the only thing that admits them.
    Four views yes, Sales Call Capture no: CCS creates a recording-adjacent
    artifact of a client conversation and its audience follows the people who
    host those calls.
    """
    from backend import auth as auth_module

    guest = _profile(NAMED_GUEST, department="general", is_exec=True, managed=["*"])
    assert auth_module.user_can_access_cockpit(guest)
    assert auth_module.user_can_access_cockpit_manager(guest)
    assert auth_module.user_can_access_cockpit_supervisor(guest)
    assert auth_module.user_can_access_cockpit_exec(guest)
    assert not auth_module.user_can_access_ccs(guest)


def test_an_unnamed_org_wide_exec_still_reaches_nothing():
    """The narrowed roster is only proved by the people it refuses.

    Same shape as the named guest — org-wide exec, no cockpit department — but
    not on the roster. If this goes green-to-red together with the test above,
    the name has stopped being a roster and become an ``is_exec`` clause.
    """
    from backend import auth as auth_module

    other = _profile(
        "not.on.the.roster@acme.example",
        department="general",
        is_exec=True,
        managed=["*"],
    )
    assert not auth_module.user_can_access_cockpit(other)
    assert not auth_module.user_can_access_cockpit_manager(other)
    assert not auth_module.user_can_access_cockpit_supervisor(other)
    assert not auth_module.user_can_access_cockpit_exec(other)


@pytest.mark.parametrize(("key", "field"), sorted(COCKPIT_FEATURES.items()))
def test_each_cockpit_gate_is_wired_through_the_auth_context(key, field):
    """A verdict nothing reads is decoration. §7.5 wants the whole chain.

    Registry key named in the client's ``FEATURE`` map + ``AuthContextType``
    field + derived const + provider entry — the last three are the three
    word-boundary occurrences counted here. And no roster of its own.
    """
    text = _AUTH_CONTEXT.read_text(encoding="utf-8")
    assert f"'{key}'" in text, key
    assert f"{field}: boolean;" in text, field
    assert len(re.findall(rf"\b{field}\b", text)) >= 3, field
    assert "const SALES_KAIZEN_PILOT_DEPARTMENTS" not in text, (
        "AuthContext.tsx grew a Sales Kaizen roster again — the client reads "
        "features.<key> from /api/users/me"
    )
    assert "const COCKPIT_EXTRA_EMAILS" not in text

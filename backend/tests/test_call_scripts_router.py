"""Call Scripts authoring API — the rules that make self-serve scripts real.

Under test: ``backend/routers/scripts.py``, the six endpoints Harper and
Indigo use to put a product's checklist in front of an AE without a deploy.

Call Scripts promises that a script author adds a product script through a
form — no deploy, no engineering ticket — and it is the promise the feature
lives or dies on: a
script system only an engineer can update is accurate for one quarter and wrong
forever after. So this file starts from an EMPTY database and never seeds the
company's content. A sample script — is a discovery workshop needed, how many
sites, how many hours per site, what rate, what commencement date — is used as
a SHAPE for the fixtures and is authored through the API in
every test that needs it, because that is the only way the promise is proved.

Four rules are pinned here, and each one is here because breaking it fails
*silently* — the panel renders, the wrap-up renders, and the AE is told
something untrue:

1. **Editing an APPROVED script forks; it never mutates one.** A call already
   running against a script recomputes its gap list from that script's items
   every time its wrap-up is opened, so an in-place edit rewrites the history of
   every wrap-up citing it months later, with nothing in the record to show it
   changed.
2. **``approved_by`` is stamped from the authenticated caller, never from the
   body.** The design rule: *"Do not ship an AI-generated script as
   authoritative."* A
   signature a caller can supply is not a signature, and this column is the only
   evidence that a named human approved the script.
3. **A draft is invisible to an AE.** Rule 2 has a second door: a mined draft
   listed to an AE is a model's guess wearing the company's checklist.
4. **Nothing is hard-deleted.** ``call_session_scripts.script_id`` carries no
   ``ondelete`` so a finished call stays explicable; ``DELETE`` retires.

The authoring gate reuses ``auth.user_can_access_cockpit_supervisor`` — Harper's
existing roster — rather than inventing a role. With the fixture's narrowed
configuration that roster and Call Capture's both resolve to ``{"systems"}``,
so the check admits exactly who ``require_ccs`` admits;
:func:`test_authoring_narrows_to_the_supervisor_roster_when_readers_widen`
proves it starts biting once an admin hands the roster to another team.

Nothing here talks to HubSpot, Meet, Gmail or Vertex — the authoring API has no
external boundary at all, which is the point of scripts being data.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth import get_current_user
from backend.main import app
from backend.routers import scripts as scripts_router
from rubik_core.call_capture.scripts import active_script_for
from rubik_core.config import settings
from rubik_core.database.call_scripts import CallScript, ScriptItem
from rubik_core.database.db import get_db
from rubik_core.database.schemas import UserProfile

client = TestClient(app)

# The ``db`` fixture narrows Call Capture and the supervisor (authoring) roster
# to Systems, as an admin would in Feature Access (``_install_systems_rosters``),
# so everyone who has to REACH an endpoint is a **Systems** user standing in for
# the role their name gives them: read "Harper the script author".
AUTHOR = "harper@acme.example"
AE = "parker@acme.example"
ADMIN = "admin@acme.example"
# Outside the narrowed roster: reaches the copilot (open to everyone) and is
# refused by Call Capture's own gate.
SALES_AE = "riley@acme.example"

CAST: dict[str, dict] = {
    AUTHOR: {"department": "Systems"},
    AE: {"department": "Systems"},
    ADMIN: {"department": "Systems", "is_admin": True},
    SALES_AE: {"department": "Sales"},
}

#: A sample script: the five facts an author would otherwise reverse-engineer
#: out of a call summary to build Parker's SOW. A SHAPE for the fixtures,
#: authored through the API every time; nothing seeds it.
PLAN_ITEMS = [
    {
        "fact_key": "discovery_workshop",
        "prompt_text": "Do you need a discovery workshop first?",
        "required": True,
    },
    {
        "fact_key": "site_count",
        "prompt_text": "How many sites are in scope?",
        "hubspot_target": "deal.site_count",
        "required": True,
    },
    {
        "fact_key": "hours_per_site",
        "prompt_text": "How many hours per site?",
        "required": True,
        "help_text": "Harper needs this to price the SOW.",
    },
    {
        "fact_key": "rate",
        "prompt_text": "What rate did we quote?",
        "required": True,
    },
    {
        "fact_key": "commencement_date",
        "prompt_text": "When would you want to start?",
        "required": False,
    },
]

# Every mutating endpoint, as (method, path_template, body). Walked by the gate
# tests so a seventh endpoint added without a gate leaves a row uncovered.
MUTATIONS = (
    ("post", "/api/scripts", {"product_key": "probe", "name": "Probe", "items": []}),
    ("put", "/api/scripts/{id}", {"name": "Renamed"}),
    ("post", "/api/scripts/{id}/approve", None),
    ("delete", "/api/scripts/{id}", None),
)


# ── Identity ─────────────────────────────────────────────────────────────────


def _profile(email: str, **overrides) -> UserProfile:
    base = {"department": "Systems", "is_admin": False, "is_exec": False}
    base.update(CAST.get(email, {}))
    base.update(overrides)
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name=email.split("@")[0].title(),
        department=base["department"],
        roles=["admin"] if base["is_admin"] else ["user"],
        is_exec=base["is_exec"],
        managed_departments=[],
        created_at="2026-08-28T00:00:00",
        updated_at="2026-08-28T00:00:00",
    )


def _as(email: str, **overrides) -> UserProfile:
    """Authenticate by overriding the dependency — never by sending a header."""
    profile = _profile(email, **overrides)
    app.dependency_overrides[get_current_user] = lambda: profile
    return profile


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _install_systems_rosters(db) -> None:
    """The narrowed configuration these tests exercise, written the way an admin
    would in Admin → Feature Access: Call Capture and the supervisor (authoring)
    roster both Systems-only. The shipped defaults — Call Capture open to
    everyone, the supervisor roster admins only — are pinned in
    test_default_access.py and test_sales_kaizen_access.py §12."""
    from rubik_core.features import FeaturePolicy, clear_cache, save_policy

    systems = FeaturePolicy(departments=frozenset({"systems"}), admin_allowed=True)
    for key in ("sales_call_capture", "sales_cockpit_supervisor"):
        save_policy(db, key, systems, actor_email="test@acme.example")
    clear_cache()


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from rubik_core.database.models import Base

    # models.py's last-line import is what registers these tables; dropping a
    # name there does not fail loudly, create_all simply stops seeing them.
    assert "call_scripts" in Base.metadata.tables
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr("rubik_core.database.db.SessionLocal", Session)
    session = Session()
    _install_systems_rosters(session)
    app.dependency_overrides[get_db] = lambda: session
    try:
        yield session
    finally:
        app.dependency_overrides.clear()
        session.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def flags(monkeypatch):
    """Enable both features used by this router's read and authoring gates."""
    monkeypatch.setattr(settings, "CCS_ENABLED", True)
    monkeypatch.setattr(settings, "COCKPIT_ENABLED", True)


# ── Helpers that go through the API, never around it ─────────────────────────


def _create(product_key: str = "implementation_plan", **overrides) -> dict:
    body = {
        "product_key": product_key,
        "name": overrides.pop("name", "Implementation Plan"),
        "items": overrides.pop("items", PLAN_ITEMS),
        **overrides,
    }
    res = client.post("/api/scripts", json=body)
    assert res.status_code == 201, res.text
    return res.json()


def _approve(script_id: str) -> dict:
    res = client.post(f"/api/scripts/{script_id}/approve")
    assert res.status_code == 200, res.text
    return res.json()


def _live(product_key: str = "implementation_plan"):
    return client.get(f"/api/scripts/{product_key}")


# ═══ 1. The gates ════════════════════════════════════════════════════════════


def test_a_caller_outside_call_capture_is_refused_the_whole_router(db):
    """The audience gate is Call Capture's, and it runs before anything else.

    ``require_ccs`` is a dependency, so it precedes both the ``CCS_ENABLED``
    check and the authoring gate in the handler body. A Sales AE therefore gets
    the same 403 whether the feature is on or off and cannot use the difference
    to discover that an unreleased surface exists — the property
    ``test_sales_kaizen_access.py`` pins for ``/api/calls`` and that this router
    inherits by carrying the same dependency.
    """
    _as(SALES_AE)
    for method, path, body in (
        ("get", "/api/scripts", None),
        ("get", "/api/scripts/implementation_plan", None),
        *[(m, p.format(id="abc"), b) for m, p, b in MUTATIONS],
    ):
        res = getattr(client, method)(
            path, **({"json": body} if body is not None else {})
        )
        assert res.status_code == 403, f"{method} {path} → {res.status_code}"
        detail = res.json()["detail"]
        assert detail == (
            "Sales Call Capture is not turned on for you. Ask your admin."
        ), detail


def test_the_flag_closes_the_router_for_someone_inside_the_audience(db, monkeypatch):
    """``CCS_ENABLED`` off → 404 everywhere, including the reads.

    A half-provisioned environment must not half-run the feature. 404 rather
    than 403: a feature that is off is not a permission problem.
    """
    monkeypatch.setattr(settings, "CCS_ENABLED", False)
    _as(AUTHOR)
    for method, path, body in (
        ("get", "/api/scripts", None),
        ("get", "/api/scripts/implementation_plan", None),
        *[(m, p.format(id="abc"), b) for m, p, b in MUTATIONS],
    ):
        res = getattr(client, method)(
            path, **({"json": body} if body is not None else {})
        )
        assert res.status_code == 404, f"{method} {path} → {res.status_code} {res.text}"
        assert "not enabled" in res.json()["detail"].lower(), res.text


def _widen_supervisor_roster_to_support(db) -> None:
    """Hand the supervisor roster to another team (Support) the way an admin would:
    an override row in Admin → Feature Access, which is the only thing the gate
    reads (``rubik_core.features``)."""
    from rubik_core.features import FeaturePolicy, feature_by_key, save_policy

    default = feature_by_key("sales_cockpit_supervisor").default()
    save_policy(
        db,
        "sales_cockpit_supervisor",
        FeaturePolicy(
            departments=frozenset({"support"}),
            extra_emails=default.extra_emails,
            admin_allowed=True,
        ),
        actor_email="test@acme.example",
    )


@pytest.fixture(autouse=True)
def _fresh_feature_cache():
    """The roster cache is process-wide; an override written here must not
    outlive the in-memory database it was written to."""
    from rubik_core.features import clear_cache

    clear_cache()
    yield
    clear_cache()


def test_authoring_narrows_to_the_supervisor_roster_when_readers_widen(db):
    """The authoring gate is Harper's roster, and it is a REAL second gate.

    Under the fixture's configuration it is invisible: Call Capture and the
    supervisor roster are both narrowed to ``{"systems"}``, so every Systems
    caller passes both and a broken authoring gate would look exactly like a
    working one. Handing the supervisor roster to another team (Support) through
    Admin → Feature Access is the only way to see it, so this test does that.

    An AE then still READS the catalogue (they need the panel) and can no longer
    author, approve or retire anything: reading and authoring are two rosters,
    whatever an admin puts on each.
    """
    _widen_supervisor_roster_to_support(db)
    _as(AE)

    assert client.get("/api/scripts").status_code == 200
    for method, path, body in MUTATIONS:
        res = getattr(client, method)(
            path.format(id="abc"), **({"json": body} if body is not None else {})
        )
        assert res.status_code == 403, f"{method} {path} → {res.status_code}"
        assert "Nudge Worklist" in res.json()["detail"], res.text

    # An admin supports the app and keeps authoring under either roster.
    _as(ADMIN)
    assert client.post("/api/scripts", json=MUTATIONS[0][2]).status_code == 201


def test_a_draft_is_invisible_to_an_ae(db):
    """Rule 3. ``include_drafts`` is the author's switch, not everyone's.

    A mined draft listed to an AE is a model's guess wearing the company's checklist —
    the questions look plausible and the first sign of trouble is an SOW built
    on a fact nobody at the company agreed to ask for. So the default listing is live
    scripts only, and asking for more needs the authoring gate.
    """
    _as(AUTHOR)
    _create()

    default_listing = client.get("/api/scripts").json()
    assert default_listing["scripts"] == [], "an unsigned script reaches nobody"
    assert default_listing["can_author"] is True

    drafts = client.get("/api/scripts", params={"include_drafts": True})
    assert drafts.status_code == 200
    assert [s["product_key"] for s in drafts.json()["scripts"]] == [
        "implementation_plan"
    ]


def test_include_drafts_is_refused_without_the_authoring_gate(db):
    _widen_supervisor_roster_to_support(db)
    _as(AE)
    res = client.get("/api/scripts", params={"include_drafts": True})
    assert res.status_code == 403, res.text
    assert client.get("/api/scripts").json()["can_author"] is False


# ═══ 2. The engine ships EMPTY ═══════════════════════════════════════════════


def test_the_catalogue_starts_empty(db):
    """Self-serve's precondition: no company content is seeded anywhere.

    Not a triviality — a router that shipped a default product list would be an
    engineer authoring Indigo's and Harper's content, which is the failure
    mode this guards against. Their product list is not finished; the tables ship empty
    and stay empty until a human fills them.
    """
    _as(AUTHOR)
    assert client.get("/api/scripts").json()["scripts"] == []
    assert (
        client.get("/api/scripts", params={"include_drafts": True}).json()["scripts"]
        == []
    )
    assert db.query(CallScript).count() == 0
    assert db.query(ScriptItem).count() == 0


# ═══ 3. Creating a draft — the form Harper fills in ═════════════════════════


def test_create_stores_the_checklist_in_order_and_leaves_it_unsigned(db):
    """A product script added through the API in one call, and it lands UNAPPROVED.

    ``sequence`` comes from the order of the list, not from the client: the
    panel renders top to bottom and a client-supplied sequence is one more way
    for two items to collide or for a gap to open in a checklist a human reads
    in order.
    """
    _as(AUTHOR)
    out = _create()

    assert out["product_key"] == "implementation_plan"
    assert out["version"] == 1
    assert out["source"] == "manual"
    assert out["approved_by"] is None
    assert out["approved_at"] is None
    assert out["is_active"] is True
    assert out["is_live"] is False, "nothing is live before a human signs it"
    assert out["item_count"] == 5
    assert out["required_item_count"] == 4

    assert [i["fact_key"] for i in out["items"]] == [
        "discovery_workshop",
        "site_count",
        "hours_per_site",
        "rate",
        "commencement_date",
    ]
    assert [i["sequence"] for i in out["items"]] == [1, 2, 3, 4, 5]
    assert out["items"][1]["hubspot_target"] == "deal.site_count"
    # NULL is a complete answer, not a gap: hours and rate feed the SOW, and
    # there may be no HubSpot property that holds them.
    assert out["items"][2]["hubspot_target"] is None
    assert out["items"][4]["required"] is False


def test_create_cannot_forge_a_signature_or_its_provenance(db):
    """Rule 2, at the create door.

    ``source`` is fixed to ``"manual"``: ``"mined"`` belongs to the batch job
    that drafts from the transcript corpus, and provenance is what an approver
    reads when deciding whether to sign — a label a caller can claim is worse
    than none. ``approved_by`` is likewise unreachable from a request body; only
    ``POST /{id}/approve`` sets it.
    """
    _as(AUTHOR)
    res = client.post(
        "/api/scripts",
        json={
            "product_key": "implementation_plan",
            "name": "Implementation Plan",
            "items": PLAN_ITEMS,
            "source": "mined",
            "approved_by": "ceo@acme.example",
            "approved_at": "2020-01-01T00:00:00",
            "is_live": True,
        },
    )
    assert res.status_code == 201, res.text
    out = res.json()
    assert out["source"] == "manual"
    assert out["approved_by"] is None
    assert out["is_live"] is False

    row = db.query(CallScript).filter(CallScript.id == out["script_id"]).one()
    assert row.source == "manual"
    assert row.approved_by is None
    assert row.approved_at is None


def test_keys_are_folded_and_slug_shaped(db):
    """A casing or spacing mistake in a join key must fail LOUDLY, at the door.

    ``Implementation_Plan`` and ``implementation_plan`` are the same product to every
    human and two different products to every query in this feature — the panel
    renders two sections and the wrap-up reports the second as an unscripted
    product. Neither raises anywhere downstream, so the door is the only place
    the mistake is visible.
    """
    _as(AUTHOR)
    out = _create(product_key="  Implementation_Plan  ")
    assert out["product_key"] == "implementation_plan"

    folded = client.post(
        "/api/scripts",
        json={
            "product_key": "staff_training",
            "name": "Staff Training",
            "items": [{"fact_key": " Hours_Per_Site ", "prompt_text": "How many?"}],
        },
    )
    assert folded.status_code == 201, folded.text
    assert folded.json()["items"][0]["fact_key"] == "hours_per_site"

    spaced = client.post(
        "/api/scripts",
        json={"product_key": "implementation plan", "name": "X", "items": []},
    )
    assert spaced.status_code == 400, spaced.text
    assert "product_key" in spaced.json()["detail"]

    bad_fact = client.post(
        "/api/scripts",
        json={
            "product_key": "x",
            "name": "X",
            "items": [{"fact_key": "hours per site", "prompt_text": "How many?"}],
        },
    )
    assert bad_fact.status_code == 400, bad_fact.text
    assert "fact_key" in bad_fact.json()["detail"]


def test_a_repeated_fact_key_is_refused_with_the_key_that_repeated(db):
    """Two rows on one fact make coverage ambiguous, on the same screen.

    The same extracted value would satisfy one item and leave the other reported
    as *not discussed*. ``UNIQUE(script_id, fact_key)`` would catch it as a 500;
    the author needs the key.
    """
    _as(AUTHOR)
    res = client.post(
        "/api/scripts",
        json={
            "product_key": "implementation_plan",
            "name": "Implementation Plan",
            "items": [
                {"fact_key": "rate", "prompt_text": "What rate?"},
                {"fact_key": "RATE", "prompt_text": "And the rate?"},
            ],
        },
    )
    assert res.status_code == 400, res.text
    assert "rate" in res.json()["detail"]
    assert db.query(CallScript).count() == 0, "a refused draft leaves no row"


def test_a_second_script_for_a_product_takes_the_next_version(db):
    _as(AUTHOR)
    first = _create()
    second = _create(name="Implementation Plan (rewrite)")
    assert (first["version"], second["version"]) == (1, 2)
    assert first["script_id"] != second["script_id"]


def test_a_lost_version_race_is_a_409_and_not_a_500(db, monkeypatch):
    """``uq_call_scripts_product_version`` is the arbiter; the read is a fast path.

    Two authors forking the same script in the same second both read the same
    maximum. A duplicate version would make "the live script" a coin toss —
    ``active_script_for`` picks by newest version — so the database refuses it
    and the author gets something they can act on.
    """
    _as(AUTHOR)
    _create()
    monkeypatch.setattr(scripts_router, "_next_version", lambda *_a, **_kw: 1)
    res = client.post(
        "/api/scripts",
        json={"product_key": "implementation_plan", "name": "Clash", "items": []},
    )
    assert res.status_code == 409, res.text
    assert "reload" in res.json()["detail"].lower()
    assert db.query(CallScript).count() == 1


# ═══ 4. Approval — the only thing that makes a script live ═══════════════════


def test_approve_stamps_the_caller_and_never_the_body(db):
    """Rule 2. The endpoint takes no body at all, and ignores one if sent.

    ``approved_by`` is the only column that can show each script was approved
    by a named human, none generated-and-shipped. An
    endpoint that read the approver from the request would let any caller sign
    on somebody else's behalf, which is not a signature.
    """
    _as(AUTHOR)
    script = _create()

    res = client.post(
        f"/api/scripts/{script['script_id']}/approve",
        json={"approved_by": "ceo@acme.example", "approved_at": "2020-01-01"},
    )
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["approved_by"] == AUTHOR
    assert out["approved_at"] is not None
    assert out["is_live"] is True
    assert out["already_approved"] is False


def test_approval_is_what_makes_a_script_reachable_by_an_ae(db):
    """Not ``is_active`` — the distinction the model layer exists to keep.

    A mining job sets ``is_active`` freely; it costs nothing and means nothing
    about correctness. ``active_script_for`` is asserted directly alongside the
    endpoint so the editor and the calendar panel cannot disagree about which
    version an AE is shown.
    """
    _as(AUTHOR)
    script = _create()
    assert _live().status_code == 404
    assert active_script_for(db, "implementation_plan") is None

    _approve(script["script_id"])

    got = _live()
    assert got.status_code == 200, got.text
    assert got.json()["script_id"] == script["script_id"]
    assert [i["fact_key"] for i in got.json()["items"]][:2] == [
        "discovery_workshop",
        "site_count",
    ]
    assert active_script_for(db, "implementation_plan").id == script["script_id"]

    # And the AE now sees it in the default listing, without the drafts switch.
    _as(AE)
    listed = client.get("/api/scripts").json()["scripts"]
    assert [s["script_id"] for s in listed] == [script["script_id"]]
    assert listed[0]["is_live"] is True


def test_an_empty_checklist_cannot_be_approved(db):
    """An empty script reports every call as complete, because it has no facts.

    That is the original problem — a wrap-up that looks clean over a call that
    surfaced nothing — with a checklist bolted on top, and it is the one shape
    of script that makes the feature actively misleading.
    """
    _as(AUTHOR)
    empty = _create(product_key="empty_product", name="Empty", items=[])
    res = client.post(f"/api/scripts/{empty['script_id']}/approve")
    assert res.status_code == 400, res.text
    assert "empty checklist" in res.json()["detail"].lower()
    assert _live("empty_product").status_code == 404


def test_a_retired_version_cannot_be_approved_into_life(db):
    """Signing a switched-off version publishes nothing and says it did."""
    _as(AUTHOR)
    script = _create()
    assert client.delete(f"/api/scripts/{script['script_id']}").status_code == 200

    res = client.post(f"/api/scripts/{script['script_id']}/approve")
    assert res.status_code == 409, res.text
    assert "retired" in res.json()["detail"].lower()


def test_a_second_approval_keeps_the_first_signature(db):
    """Idempotent, and it never re-signs.

    The signature records who read this checklist and when. Overwriting it on a
    double-click would quietly reassign accountability for content that is
    already live in front of AEs.
    """
    _as(AUTHOR)
    script = _create()
    first = _approve(script["script_id"])

    _as(ADMIN)
    again = client.post(f"/api/scripts/{script['script_id']}/approve")
    assert again.status_code == 200, again.text
    out = again.json()
    assert out["already_approved"] is True
    assert out["approved_by"] == AUTHOR, "the original approver, not the second caller"
    assert out["approved_at"] == first["approved_at"]


# ═══ 5. Editing — a draft in place, an approved script by forking ════════════


def test_editing_a_draft_edits_it_in_place(db):
    """Nothing has run against a draft, so there is nothing to protect."""
    _as(AUTHOR)
    script = _create()

    res = client.put(
        f"/api/scripts/{script['script_id']}",
        json={
            "name": "Implementation Plan (v1 wording)",
            "items": PLAN_ITEMS[:2],
            "notes": "Trimmed after Harper's read-through.",
        },
    )
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["script_id"] == script["script_id"], "same row"
    assert out["version"] == 1, "a draft does not fork"
    assert out.get("forked") is None
    assert out["name"] == "Implementation Plan (v1 wording)"
    assert out["notes"] == "Trimmed after Harper's read-through."
    assert [i["fact_key"] for i in out["items"]] == [
        "discovery_workshop",
        "site_count",
    ]
    assert db.query(ScriptItem).count() == 2, "the replaced items are gone"


def test_reordering_a_checklist_does_not_trip_the_unique_constraint(db):
    """The most ordinary edit there is, and the one that breaks a naive rewrite.

    SQLAlchemy emits a mapper's INSERTs before its DELETEs inside one flush, so
    re-adding a fact key that is about to be deleted collides on
    ``UNIQUE(script_id, fact_key)``. The router deletes and flushes first.
    """
    _as(AUTHOR)
    script = _create()

    reversed_items = list(reversed(PLAN_ITEMS))
    res = client.put(
        f"/api/scripts/{script['script_id']}", json={"items": reversed_items}
    )
    assert res.status_code == 200, res.text
    assert [i["fact_key"] for i in res.json()["items"]] == [
        i["fact_key"] for i in reversed_items
    ]
    assert [i["sequence"] for i in res.json()["items"]] == [1, 2, 3, 4, 5]
    assert db.query(ScriptItem).count() == 5


def test_editing_an_approved_script_forks_and_leaves_the_live_one_alone(db):
    """Rule 1, and the reason it is not merely tidy.

    A call already running against this script recomputes its gap list from
    these items every time its wrap-up is opened. Mutating them would rewrite
    the history of every wrap-up citing it — "the AE skipped a required fact"
    becomes "the AE covered everything" — months later, with nothing in the
    record to show it changed.
    """
    _as(AUTHOR)
    original = _create()
    _approve(original["script_id"])

    res = client.put(
        f"/api/scripts/{original['script_id']}",
        json={"name": "Implementation Plan 2026", "items": PLAN_ITEMS[:3]},
    )
    assert res.status_code == 200, res.text
    fork = res.json()
    assert fork["forked"] is True
    assert fork["forked_from_script_id"] == original["script_id"]
    assert fork["script_id"] != original["script_id"]
    assert fork["version"] == 2
    assert fork["approved_by"] is None, "a fork is a draft until somebody signs it"
    assert fork["is_live"] is False
    assert fork["source"] == "manual"
    assert [i["fact_key"] for i in fork["items"]] == [
        i["fact_key"] for i in PLAN_ITEMS[:3]
    ]

    # The live script is untouched, and it is still the one an AE is shown.
    still_live = _live().json()
    assert still_live["script_id"] == original["script_id"]
    assert still_live["name"] == "Implementation Plan"
    assert still_live["item_count"] == 5
    assert active_script_for(db, "implementation_plan").id == original["script_id"]

    # …until the fork is signed, at which point it takes over and the old
    # version stops being live without ever being deleted.
    _approve(fork["script_id"])
    assert _live().json()["script_id"] == fork["script_id"]
    listing = {
        s["script_id"]: s
        for s in client.get("/api/scripts", params={"include_drafts": True}).json()[
            "scripts"
        ]
    }
    assert listing[fork["script_id"]]["is_live"] is True
    assert listing[original["script_id"]]["is_live"] is False
    assert listing[original["script_id"]]["approved_by"] == AUTHOR

    # And the AE's default view is ONE ROW PER PRODUCT. The superseded v1 is
    # still approved and still active — that is what makes a bad v2 undoable —
    # so a listing that merely filtered on those two columns would show
    # "Implementation Plan" twice with two different checklists.
    _as(AE)
    default_view = client.get("/api/scripts").json()["scripts"]
    assert [s["script_id"] for s in default_view] == [fork["script_id"]]


def test_a_fork_with_no_items_carries_the_parents_checklist(db):
    """Omitted is not empty. Renaming a script must not silently empty it."""
    _as(AUTHOR)
    original = _create()
    _approve(original["script_id"])

    fork = client.put(
        f"/api/scripts/{original['script_id']}", json={"name": "Renamed only"}
    ).json()
    assert fork["forked"] is True
    assert [i["fact_key"] for i in fork["items"]] == [i["fact_key"] for i in PLAN_ITEMS]
    assert fork["items"][2]["help_text"] == "Harper needs this to price the SOW."


def test_toggling_is_active_on_an_approved_script_does_not_fork(db):
    """The one exception, and the one the model layer describes.

    ``is_active`` is "the AUTHOR'S on/off switch". It changes nothing a past
    call was measured against, and forking on a retire/restore would produce an
    unapproved copy — which cannot be live, and therefore could never restore
    anything.
    """
    _as(AUTHOR)
    script = _create()
    _approve(script["script_id"])

    off = client.put(f"/api/scripts/{script['script_id']}", json={"is_active": False})
    assert off.status_code == 200, off.text
    assert off.json()["script_id"] == script["script_id"], "no fork"
    assert off.json()["version"] == 1
    assert off.json()["is_live"] is False
    assert _live().status_code == 404

    on = client.put(f"/api/scripts/{script['script_id']}", json={"is_active": True})
    assert on.json()["is_live"] is True
    assert _live().json()["script_id"] == script["script_id"]
    assert db.query(CallScript).count() == 1, "no version was created either way"


def test_an_empty_edit_is_refused(db):
    _as(AUTHOR)
    script = _create()
    res = client.put(f"/api/scripts/{script['script_id']}", json={})
    assert res.status_code == 400, res.text
    assert "nothing to change" in res.json()["detail"].lower()


def test_editing_a_script_that_does_not_exist_is_a_404(db):
    _as(AUTHOR)
    assert client.put("/api/scripts/nope", json={"name": "x"}).status_code == 404
    assert client.post("/api/scripts/nope/approve").status_code == 404
    assert client.delete("/api/scripts/nope").status_code == 404
    assert client.get("/api/scripts/never_sold").status_code == 404


# ═══ 6. Retiring — and never deleting ════════════════════════════════════════


def test_delete_retires_the_row_and_keeps_it(db):
    """Rule 4. ``call_session_scripts.script_id`` has no ``ondelete`` on purpose.

    The wrap-up recomputes its gap list from the script's items, so a hard
    delete would turn a finished, explicable call into an unexplainable one.
    Clearing ``is_active`` takes it off every AE's screen immediately and leaves
    the history intact.
    """
    _as(AUTHOR)
    script = _create()
    _approve(script["script_id"])
    assert _live().status_code == 200

    res = client.delete(f"/api/scripts/{script['script_id']}")
    assert res.status_code == 200, res.text
    assert res.json()["is_active"] is False
    assert res.json()["already_inactive"] is False
    assert _live().status_code == 404
    assert active_script_for(db, "implementation_plan") is None

    row = db.query(CallScript).filter(CallScript.id == script["script_id"]).one()
    assert row.is_active is False
    assert row.approved_by == AUTHOR, "the signature survives the retirement"
    assert db.query(ScriptItem).filter(ScriptItem.script_id == row.id).count() == 5


def test_retiring_twice_is_a_no_op(db):
    _as(AUTHOR)
    script = _create()
    client.delete(f"/api/scripts/{script['script_id']}")
    again = client.delete(f"/api/scripts/{script['script_id']}")
    assert again.status_code == 200, again.text
    assert again.json()["already_inactive"] is True
    assert db.query(CallScript).count() == 1


def test_a_retired_version_falls_back_to_the_previous_approved_one(db):
    """Retiring v2 puts v1 back in front of AEs rather than leaving a hole.

    ``active_script_for`` picks the newest APPROVED, ACTIVE version, so
    retirement is a real undo for a bad rewrite — which is why nothing here
    deletes and why older approved versions stay active.
    """
    _as(AUTHOR)
    v1 = _create()
    _approve(v1["script_id"])
    v2 = client.put(
        f"/api/scripts/{v1['script_id']}", json={"items": PLAN_ITEMS[:1]}
    ).json()
    _approve(v2["script_id"])
    assert _live().json()["script_id"] == v2["script_id"]

    client.delete(f"/api/scripts/{v2['script_id']}")
    assert _live().json()["script_id"] == v1["script_id"]
    assert _live().json()["item_count"] == 5


# ═══ 7. Multi-product is the point, not an edge case ═════════════════════════


def test_products_are_independent_and_listed_separately(db):
    """Lead↔deal is 1:1, so three products are three leads.

    Nothing in this router merges products — each has its own script, its own
    version line and its own liveness. A call that surfaces implementation plan AND
    staff training resolves two scripts, which is what lets the wrap-up
    say *"staff training was discussed but this call is only scripted for the
    implementation plan — split it into its own lead?"*
    """
    _as(AUTHOR)
    plan = _create()
    training = _create(
        product_key="staff_training",
        name="Staff Training",
        items=[
            {"fact_key": "supplier", "prompt_text": "Who runs training today?"},
            {"fact_key": "seat_count", "prompt_text": "How many seats?"},
        ],
    )
    _approve(plan["script_id"])

    listed = client.get("/api/scripts").json()["scripts"]
    assert [s["product_key"] for s in listed] == ["implementation_plan"]

    assert _live("staff_training").status_code == 404
    _approve(training["script_id"])
    both = client.get("/api/scripts").json()["scripts"]
    assert [s["product_key"] for s in both] == [
        "implementation_plan",
        "staff_training",
    ]
    assert [s["item_count"] for s in both] == [5, 2]

    filtered = client.get("/api/scripts", params={"product_key": "Implementation_Plan"})
    assert [s["product_key"] for s in filtered.json()["scripts"]] == [
        "implementation_plan"
    ]


# ═══ 8. The audit trail ══════════════════════════════════════════════════════


def test_every_authoring_action_is_audited(db):
    """Scripts decide what the company asks a client and what the wrap-up calls a gap.

    Who changed which version, and who signed it, is worth keeping even though
    nothing else in the feature reads it. Written AFTER the commit, because
    ``record_audit`` commits its own row.
    """
    from rubik_core.database.models import AuditLog

    _as(AUTHOR)
    script = _create()
    _approve(script["script_id"])
    fork = client.put(
        f"/api/scripts/{script['script_id']}", json={"name": "Renamed"}
    ).json()
    client.delete(f"/api/scripts/{fork['script_id']}")

    actions = [r.action for r in db.query(AuditLog).order_by(AuditLog.id).all()]
    assert actions == [
        "CALL_SCRIPT_CREATE",
        "CALL_SCRIPT_APPROVE",
        "CALL_SCRIPT_FORK",
        "CALL_SCRIPT_DEACTIVATE",
    ]
    rows = db.query(AuditLog).order_by(AuditLog.id).all()
    assert all(r.actor_email == AUTHOR for r in rows)
    assert rows[2].details["forked_from"] == script["script_id"]
    assert rows[0].details["product_key"] == "implementation_plan"


# ═══ 9. Shape ════════════════════════════════════════════════════════════════


def test_the_response_keys_are_flat_snake_case(db):
    """The interface other agents build against. A renamed key is a broken panel."""
    _as(AUTHOR)
    script = _create()
    _approve(script["script_id"])

    out = _live().json()
    assert set(out) == {
        "script_id",
        "product_key",
        "name",
        "version",
        "hubspot_product_id",
        "is_active",
        "is_live",
        "approved_by",
        "approved_at",
        "source",
        "notes",
        "item_count",
        "required_item_count",
        "created_at",
        "updated_at",
        "items",
    }
    assert set(out["items"][0]) == {
        "item_id",
        "sequence",
        "fact_key",
        "prompt_text",
        "hubspot_target",
        "required",
        "help_text",
    }
    for key in (*out, *out["items"][0]):
        assert key == key.lower() and " " not in key

    listing = client.get("/api/scripts").json()
    assert set(listing) == {"scripts", "can_author"}
    assert "items" not in listing["scripts"][0], "the list view carries counts only"


def test_an_oversized_checklist_is_refused_before_any_row_is_touched(db):
    """A checklist of fifty is a form, and an AE reads none of it mid-call."""
    _as(AUTHOR)
    res = client.post(
        "/api/scripts",
        json={
            "product_key": "implementation_plan",
            "name": "Too much",
            "items": [
                {"fact_key": f"fact_{i}", "prompt_text": f"Question {i}?"}
                for i in range(60)
            ],
        },
    )
    assert res.status_code == 422, res.text
    assert db.query(CallScript).count() == 0


def test_a_created_script_carries_a_real_id(db):
    _as(AUTHOR)
    out = _create()
    assert uuid.UUID(out["script_id"]).hex == out["script_id"]

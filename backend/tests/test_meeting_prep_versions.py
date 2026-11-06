"""Meeting Prep versions: the per-meeting chain and every endpoint that adds to it.

Covers the product rules end to end over HTTP:

* opening the prep page serves the latest version (no model, no Google call);
  only the first open builds v1 — in the background (202, then the worker);
* every change — regenerate (background), hand edit, Cubie rewrite of one
  section, restore — appends a NEW version built on the latest; nothing is
  overwritten;
* changes are only accepted on the latest version (409), by the owner only
  (403 — admins can read, never write);
* a full regenerate keeps hand edits (not Cubie rewrites) unless told not to;
* a failed generation is kept for audit but never becomes a version;
* the Word download names the version.

Background builds (``rubik_core/meeting_prep/background.py``) are captured at
dispatch and run by the test with :meth:`run_builds` — the worker, on its own
session, exactly as the Cloud Run Job runs it. The background mechanics
themselves (dedupe, TTL, status, notifications, the Job) are covered in
``test_meeting_prep_background.py``.

In-memory SQLite (StaticPool) behind ``dependency_overrides[get_db]``, a fresh
session per request like production; the context collector, the brief model
and the rewrite worker are always faked (patched by STRING target), so nothing
reaches Google, Vertex or Cloud SQL.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import DatabaseError, IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.auth import get_current_user, require_full_app_access
from backend.main import app
from backend.routers.home import MeetingPrepResponse
from rubik_core.auth.registry import UserProfile
from rubik_core.database.db import get_db
from rubik_core.database.models import Base, MeetingPrepRun, Notification, User
from rubik_core.meeting_prep import background, versions

OWNER = "rep1@acme.example"
OTHER = "rep2@acme.example"
ADMIN = "boss@acme.example"
EVENT = "evt_acme_20261002"

SNAPSHOT = {
    "companies": [
        {
            "name": "Acme Supply",
            "domain": "acmecorp.example",
            "hubspot_company_id": "123",
            "hubspot_url": None,
            "lifecycle_stage": "customer",
            "open_deals": [],
            "won_deals_count": 2,
            "last_won": None,
            "deal_last_contact": None,
            "company_last_contact": "2026-09-20",
            "xray": None,
            "ambiguous": False,
            "note": None,
        }
    ],
    "as_of": "2026-10-01T12:00:00",
}
SIGNALS_USED = [{"key": "gmail", "label": "Gmail", "detail": "6 threads"}]


def _profile(email: str, *, roles: list[str] | None = None) -> UserProfile:
    return UserProfile(
        id=email.split("@")[0],
        email=email,
        name=email.split("@")[0],
        department="sales",
        roles=roles or ["user"],
        created_at="2026-05-01T00:00:00Z",
        updated_at="2026-05-01T00:00:00Z",
    )


# ─── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def Session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    with factory() as db:
        for email in (OWNER, OTHER, ADMIN):
            db.add(
                User(
                    email=email,
                    id=email.split("@")[0],
                    name=email,
                    department="sales",
                )
            )
        db.commit()
    yield factory
    Base.metadata.drop_all(bind=engine)


class Pipeline:
    """Fake collect + generate + rewrite, with call counters and switches."""

    def __init__(self):
        self.collect_calls = 0
        self.generate_calls = 0
        self.fail_generation = False
        self.rewrite_calls: list[dict[str, Any]] = []
        self.rewrite_value: Any = "Rewritten summary."
        self.rewrite_sources: list[str] = []
        self.rewrite_error: Exception | None = None
        self.is_internal = False
        self.matched_products: list[dict[str, Any]] = []

    async def collect(self, **kwargs):
        self.collect_calls += 1
        event = kwargs.get("event") or {}
        return {
            "event_id": kwargs.get("event_id") or event.get("id"),
            "title": event.get("title") or "Acme quarterly sync",
            "start": "2026-10-02T15:00:00Z",
            "start_local": "Thu Oct 2, 10:00 AM CT",
            "description": "",
            "location": "",
            "attendees": [{"email": "ann@acmecorp.example", "display_name": "Ann"}],
            "is_sales_user": True,
            "is_internal": self.is_internal,
            "focus_on": kwargs.get("focus_on"),
            "errors": [],
            "recent_emails": [],
            "matched_products": list(self.matched_products),
            "attendee_intel": [],
            "prior_meetings_block": "",
            "past_meetings_report": "",
            "drive_context": "",
            "signal_blocks": {"hubspot": "HUBSPOT: Acme Supply, customer"},
            "account_snapshot": SNAPSHOT,
            "signals_used": SIGNALS_USED,
            "crm_provenance": {
                "schema_version": 1,
                "workspace": "test",
                "company_ids": ["123"],
                "deal_ids": [],
                "unprovable": False,
            },
        }

    async def generate(
        self, ctx, user_email, personalization_block="", wgll_context=""
    ):
        self.generate_calls += 1
        n = self.generate_calls
        if self.fail_generation:
            return MeetingPrepResponse(
                title=ctx["title"],
                executive_summary="Error generating analysis: 429 RESOURCE_EXHAUSTED",
                partial_errors=["429 RESOURCE_EXHAUSTED"],
                generation_failed=True,
                account_snapshot=ctx.get("account_snapshot"),
            )
        return MeetingPrepResponse(
            title=ctx["title"],
            executive_summary=f"Generated summary #{n}.",
            meeting_objective=f"Objective #{n}.",
            talking_points=[f"Point A #{n}", f"Point B #{n}"],
            stakeholder_map=json.dumps(
                [
                    {
                        "name": "Ann",
                        "email": "ann@acmecorp.example",
                        "role": "COO",
                        "context": "x",
                    }
                ]
            ),
            source_evidence=["Gmail: Re: renewal (Sep 20)"],
            is_sales_user=True,
            is_internal=False,
            account_snapshot=ctx.get("account_snapshot"),
            signals_used=ctx.get("signals_used") or [],
            meeting_time=ctx.get("start_local"),
        )

    async def rewrite(self, **kwargs):
        self.rewrite_calls.append(kwargs)
        if self.rewrite_error is not None:
            raise self.rewrite_error
        from rubik_core.meeting_prep.rewrite import RewriteResult

        return RewriteResult(
            value=self.rewrite_value, sources=list(self.rewrite_sources)
        )


@pytest.fixture
def pipeline(monkeypatch):
    p = Pipeline()
    monkeypatch.setattr("backend.routers.home.collect_meeting_prep_context", p.collect)
    monkeypatch.setattr(
        "backend.routers.home.generate_meeting_prep_analysis", p.generate
    )
    monkeypatch.setattr(
        "backend.routers.home.build_user_context_block", lambda user, db: "PERSONAL"
    )
    monkeypatch.setattr(
        "rubik_core.feedback.wgll.build_wgll_context",
        lambda db, surface, user_email, **kw: "WGLL",
    )
    monkeypatch.setattr("rubik_core.meeting_prep.rewrite.rewrite_section", p.rewrite)
    return p


@pytest.fixture
def api(Session, pipeline, monkeypatch):
    """A TestClient plus ``as_user(email, admin=False)`` to switch identity,
    ``dispatched`` (every background build handed to the runner) and
    ``run_builds()`` (run the ones not run yet, as the Job would)."""

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    current = {"user": _profile(OWNER)}

    def as_user(email: str, *, admin: bool = False):
        current["user"] = _profile(email, roles=["user", "admin"] if admin else None)

    dispatched: list[dict[str, Any]] = []
    ran = {"n": 0}

    def fake_dispatch(**kwargs):
        # The app loop an in-process build would be scheduled on; the build
        # itself is run by run_builds().
        kwargs.pop("loop", None)
        dispatched.append(kwargs)
        return "inprocess"

    def run_builds() -> list[str]:
        out = []
        while ran["n"] < len(dispatched):
            kwargs = dispatched[ran["n"]]
            ran["n"] += 1
            out.append(background.run_build_sync(**kwargs))
        return out

    # The worker and the notifications use the test database.
    monkeypatch.setattr("rubik_core.meeting_prep.background.SessionLocal", Session)
    monkeypatch.setattr("rubik_core.meeting_prep.background.dispatch", fake_dispatch)
    monkeypatch.setattr(
        "rubik_core.tenancy.crm_visibility.visible_source_ids",
        lambda companies, contacts, deals: (companies, contacts, deals),
    )

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = lambda: current["user"]
    # The copilot gate is not what is under test here.
    app.dependency_overrides[require_full_app_access] = lambda: current["user"]
    client = TestClient(app)
    client.as_user = as_user  # type: ignore[attr-defined]
    client.dispatched = dispatched  # type: ignore[attr-defined]
    client.run_builds = run_builds  # type: ignore[attr-defined]
    yield client
    app.dependency_overrides.clear()


def _open(api, event_id: str = EVENT):
    return api.get(f"/api/homefeed/meeting-prep/{event_id}")


def _first(api, event_id: str = EVENT) -> dict[str, Any]:
    """Open a meeting with no version yet: 202, the background build runs,
    and opening again lands on v1."""
    r = _open(api, event_id)
    assert r.status_code == 202, r.text
    assert api.run_builds() == ["ready"]
    r = _open(api, event_id)
    assert r.status_code == 200, r.text
    return r.json()


def _regenerate(api, *, expect: str = "ready", **body) -> dict[str, Any]:
    """Regenerate in the background and return the build's status (plus, when
    it became a version, that version as ``brief``)."""
    r = api.post("/api/homefeed/prep-runs/regenerate", json={"event_id": EVENT, **body})
    assert r.status_code == 202, r.text
    assert r.json()["kind"] == "regenerate"
    assert api.run_builds() == [expect]
    status = api.get(f"/api/homefeed/prep-runs/{r.json()['run_id']}/status").json()
    if status["status"] == "ready":
        status["brief"] = api.get(f"/api/homefeed/prep-runs/{status['run_id']}").json()
    return status


def _regen(api, **body) -> dict[str, Any]:
    """Regenerate that must succeed → the new version."""
    return _regenerate(api, **body)["brief"]


def _notifications(Session) -> list[Notification]:
    with Session() as db:
        rows = db.query(Notification).order_by(Notification.created_at).all()
        for r in rows:
            db.expunge(r)
        return rows


def _rows(Session, **filters) -> list[MeetingPrepRun]:
    with Session() as db:
        q = db.query(MeetingPrepRun)
        for k, v in filters.items():
            q = q.filter(getattr(MeetingPrepRun, k) == v)
        rows = q.all()
        for r in rows:
            db.expunge(r)
        return rows


# ─── Open = latest version; only the first open builds (in the background) ──


def test_first_open_builds_v1_in_the_background_and_reopening_serves_it(
    api, pipeline, Session, monkeypatch
):
    r = _open(api)
    # Nothing is generated in the request: 202 with the build's handle.
    assert r.status_code == 202, r.text
    accepted = r.json()
    assert accepted["status"] == "queued"
    assert accepted["kind"] == "first"
    assert accepted["event_id"] == EVENT
    assert pipeline.collect_calls == 0 and pipeline.generate_calls == 0
    assert len(api.dispatched) == 1

    assert api.run_builds() == ["ready"]
    r = _open(api)
    assert r.status_code == 200, r.text
    v1 = r.json()
    # The build became v1 IN PLACE: the handle the page polled is the version.
    assert v1["run_id"] == accepted["run_id"]
    assert v1["pending_run"] is None
    assert v1["version"] == 1
    assert v1["latest_version"] == 1
    assert v1["is_latest"] is True
    assert v1["base_version"] is None
    assert v1["change_kind"] == "generated"
    assert v1["run_id"]
    assert v1["created_at"]
    assert v1["executive_summary"] == "Generated summary #1."
    # Signals ride along on the response and survive storage.
    assert v1["account_snapshot"] == SNAPSHOT
    assert v1["signals_used"] == SIGNALS_USED
    assert v1["meeting_time"] == "Thu Oct 2, 10:00 AM CT"
    assert pipeline.collect_calls == 1 and pipeline.generate_calls == 1

    # The stored brief carries no version/run fields, and the bundle keeps the
    # signal blocks a later rewrite needs, plus what was asked.
    (row,) = _rows(Session)
    assert "version" not in row.response and "run_id" not in row.response
    assert "pending_run" not in row.response
    assert row.delivery_status == "ready"
    assert row.context_bundle["request"]["kind"] == "first"
    assert row.context_bundle["signal_blocks"] == {
        "hubspot": "HUBSPOT: Acme Supply, customer"
    }
    assert row.context_bundle["account_snapshot"] == SNAPSHOT
    assert row.context_bundle["signals_used"] == SIGNALS_USED

    # Re-opening must not touch Google or the model at all.
    async def boom(**kwargs):
        raise AssertionError("collect_meeting_prep_context called on a re-open")

    async def boom_generate(*a, **kw):
        raise AssertionError("generate_meeting_prep_analysis called on a re-open")

    monkeypatch.setattr("backend.routers.home.collect_meeting_prep_context", boom)
    monkeypatch.setattr(
        "backend.routers.home.generate_meeting_prep_analysis", boom_generate
    )
    again = _open(api)
    assert again.status_code == 200, again.text
    assert again.json()["run_id"] == v1["run_id"]
    assert again.json()["version"] == 1
    assert len(_rows(Session)) == 1


def test_bundle_keeps_the_matched_products_catalog_lines(api, pipeline, Session):
    pipeline.matched_products = [
        {
            "name": "Quarterly Pricing Review",
            "description": "Price-disparity analysis across regions.",
            "category": "Analytics",
            "price": "$4,000",
            "Unrelated column": "ignored",
        }
    ]
    _first(api)
    (row,) = _rows(Session)
    # Names stay as they were (older readers use them)…
    assert row.context_bundle["matched_products"] == ["Quarterly Pricing Review"]
    # …and the rewrite gets what each product is, the way the full brief did.
    # The price stays out: a prep brief is no place to start quoting.
    assert row.context_bundle["matched_product_details"] == [
        {
            "name": "Quarterly Pricing Review",
            "description": "Price-disparity analysis across regions.",
            "category": "Analytics",
        }
    ]


def test_regenerate_appends_v2_built_on_v1(api, pipeline):
    v1 = _first(api)
    v2 = _regen(api, focus_on="renewal")
    assert v2["version"] == 2
    assert v2["base_version"] == 1
    assert v2["change_kind"] == "regenerated"
    assert v2["is_latest"] is True
    assert v2["executive_summary"] == "Generated summary #2."
    assert v2["run_id"] != v1["run_id"]

    # v1 is still there, read-only, and knows it is not the latest.
    old = api.get(f"/api/homefeed/prep-runs/{v1['run_id']}").json()
    assert old["version"] == 1
    assert old["is_latest"] is False
    assert old["latest_version"] == 2
    assert old["executive_summary"] == "Generated summary #1."

    # Opening the page now lands on v2.
    assert _open(api).json()["run_id"] == v2["run_id"]


def test_post_meeting_prep_still_generates_and_appends_versions(api, pipeline):
    body = {"event": {"id": EVENT, "title": "Acme quarterly sync"}}
    first = api.post("/api/homefeed/meeting-prep", json=body)
    assert first.status_code == 200, first.text
    assert first.json()["version"] == 1
    assert first.json()["change_kind"] == "generated"
    second = api.post("/api/homefeed/meeting-prep", json=body).json()
    assert second["version"] == 2
    assert second["change_kind"] == "regenerated"
    assert second["base_version"] == 1


# ─── keep_edits ───────────────────────────────────────────────────────────────


def test_regenerate_keeps_hand_edits_but_not_assistant_rewrites(api, pipeline):
    v1 = _first(api)
    v2 = api.put(
        f"/api/homefeed/prep-runs/{v1['run_id']}/sections/talking_points",
        json={"value": ["  My own opener  ", "", "Ask about the renewal"]},
    ).json()
    pipeline.rewrite_value = "Cubie's tighter summary."
    v3 = api.post(
        f"/api/homefeed/prep-runs/{v2['run_id']}/sections/executive_summary/rewrite",
        json={"instruction": "shorter"},
    ).json()
    assert v3["version"] == 3

    v4 = _regen(api)
    assert v4["version"] == 4
    assert v4["base_version"] == 3
    # The hand edit survives, with its provenance unchanged…
    assert v4["talking_points"] == ["My own opener", "Ask about the renewal"]
    assert v4["section_meta"]["talking_points"]["origin"] == "edited"
    assert v4["section_meta"]["talking_points"]["version"] == 2
    # …the Cubie rewrite does not: the regenerate wrote that section again.
    assert v4["executive_summary"] == "Generated summary #2."
    assert "executive_summary" not in v4["section_meta"]
    # Untouched sections are fresh.
    assert v4["meeting_objective"] == "Objective #2."


def test_regenerate_with_keep_edits_false_drops_hand_edits(api, pipeline):
    v1 = _first(api)
    api.put(
        f"/api/homefeed/prep-runs/{v1['run_id']}/sections/talking_points",
        json={"value": ["Mine"]},
    )
    v3 = _regen(api, keep_edits=False)
    assert v3["version"] == 3
    assert v3["talking_points"] == ["Point A #2", "Point B #2"]
    assert v3["section_meta"] == {}


def _race_on_first_latest_read(monkeypatch, racing_write):
    """Patch ``latest_version_row`` so that, right after the FIRST read hands
    back what is the latest at that moment, ``racing_write(db)`` lands a new
    version (another tab saving) before the caller goes on."""
    real = versions.latest_version_row
    state = {"raced": False}

    def latest_then_race(db_, user_email, event_id):
        seen = real(db_, user_email, event_id)
        if not state["raced"]:
            state["raced"] = True
            racing_write(db_)
        return seen

    monkeypatch.setattr(
        "rubik_core.meeting_prep.versions.latest_version_row", latest_then_race
    )
    return state


def test_regenerate_keeps_a_hand_edit_saved_while_it_was_being_stored(
    api, pipeline, Session, monkeypatch
):
    v1 = _first(api)

    def hand_edit_from_another_tab(db):
        base = db.get(MeetingPrepRun, v1["run_id"])
        response = versions.strip_version_fields(base.response)
        response["talking_points"] = ["Edited in the other tab"]
        versions.append_version(
            db,
            **versions.copy_row_fields(base),
            response=response,
            change_kind="section_edited",
            base_run_id=base.id,
            changed_section="talking_points",
            section_meta={"talking_points": {"origin": "edited", "instruction": None}},
            must_build_on_latest=True,
        )

    r = api.post("/api/homefeed/prep-runs/regenerate", json={"event_id": EVENT})
    assert r.status_code == 202, r.text
    # The other tab saves while the background build is storing its brief.
    state = _race_on_first_latest_read(monkeypatch, hand_edit_from_another_tab)
    assert api.run_builds() == ["ready"]
    assert state["raced"]
    v3 = api.get(f"/api/homefeed/prep-runs/{r.json()['run_id']}").json()
    # Built on the edit that landed meanwhile, not on the v1 it first read.
    assert v3["version"] == 3
    assert v3["base_version"] == 2
    assert v3["change_kind"] == "regenerated"
    assert v3["talking_points"] == ["Edited in the other tab"]
    assert v3["section_meta"]["talking_points"]["origin"] == "edited"
    assert v3["section_meta"]["talking_points"]["version"] == 2
    assert v3["executive_summary"] == "Generated summary #2."
    assert sorted(r.version for r in _rows(Session)) == [1, 2, 3]


def test_two_first_opens_number_the_second_as_a_regenerate_of_the_first(
    api, pipeline, Session, monkeypatch
):
    def first_open_from_another_tab(db):
        versions.append_version(
            db,
            user_email=OWNER,
            event_id=EVENT,
            title="Other tab",
            prep_kind="sales_battle_brief",
            is_sales_user=True,
            is_internal=False,
            model_id="m",
            context_bundle={},
            response={"title": "Other tab", "executive_summary": "Theirs."},
            partial_errors=[],
            change_kind="generated",
            must_build_on_latest=True,
        )

    r = _open(api)
    assert r.status_code == 202, r.text
    # v1 lands from elsewhere (e.g. POST /meeting-prep) while the background
    # build is storing its brief: the build becomes v2, built on that v1.
    state = _race_on_first_latest_read(monkeypatch, first_open_from_another_tab)
    assert api.run_builds() == ["ready"]
    assert state["raced"]
    mine = _open(api).json()
    assert mine["run_id"] == r.json()["run_id"]
    assert mine["version"] == 2
    assert mine["change_kind"] == "regenerated"
    assert mine["base_version"] == 1
    rows = {r.version: r for r in _rows(Session)}
    assert rows[1].change_kind == "generated" and rows[1].base_run_id is None
    assert rows[2].base_run_id == rows[1].id


def test_regenerate_gives_up_if_the_chain_keeps_moving(
    api, pipeline, Session, monkeypatch
):
    v1 = _first(api)

    def always_stale(*a, **kw):
        raise versions.StaleVersionError(None)

    calls = {"n": 0}

    def counting(*a, **kw):
        calls["n"] += 1
        return always_stale(*a, **kw)

    monkeypatch.setattr("rubik_core.meeting_prep.versions.append_version", counting)
    status = _regenerate(api, expect="failed")
    assert calls["n"] == 3
    assert status["status"] == "failed"
    assert status["detail"] == background.UNSAVED_DETAIL
    assert [r.version for r in _rows(Session) if r.version is not None] == [1]
    assert _open(api).json()["run_id"] == v1["run_id"]


# ─── Hand edits ───────────────────────────────────────────────────────────────


def test_section_edit_appends_a_version_with_provenance(api, pipeline, Session):
    v1 = _first(api)
    r = api.put(
        f"/api/homefeed/prep-runs/{v1['run_id']}/sections/meeting_objective",
        json={"value": "  Get the renewal signed.  "},
    )
    assert r.status_code == 200, r.text
    v2 = r.json()
    assert v2["version"] == 2
    assert v2["base_version"] == 1
    assert v2["change_kind"] == "section_edited"
    assert v2["changed_section"] == "meeting_objective"
    assert v2["change_note"] is None
    assert v2["meeting_objective"] == "Get the renewal signed."
    meta = v2["section_meta"]["meeting_objective"]
    assert meta["origin"] == "edited"
    assert meta["version"] == 2
    assert meta["instruction"] is None
    assert meta["at"]
    # Everything else is the v1 brief, and v1 itself is untouched.
    assert v2["executive_summary"] == v1["executive_summary"]
    assert v2["account_snapshot"] == SNAPSHOT
    assert (
        api.get(f"/api/homefeed/prep-runs/{v1['run_id']}").json()["meeting_objective"]
        == "Objective #1."
    )
    # The new row copies the evidence the brief was built from.
    rows = {r.version: r for r in _rows(Session)}
    assert rows[2].context_bundle == rows[1].context_bundle
    assert rows[2].base_run_id == rows[1].id
    assert rows[2].prep_kind == rows[1].prep_kind == "sales_battle_brief"
    assert pipeline.generate_calls == 1


def test_stakeholder_edit_stores_the_json_string_shape(api, pipeline):
    v1 = _first(api)
    v2 = api.put(
        f"/api/homefeed/prep-runs/{v1['run_id']}/sections/stakeholder_map",
        json={
            "value": [{"name": "Bob", "email": "bob@acmecorp.example", "role": "CFO"}]
        },
    ).json()
    assert json.loads(v2["stakeholder_map"]) == [
        {"name": "Bob", "email": "bob@acmecorp.example", "role": "CFO", "context": ""}
    ]


def test_saving_an_unchanged_section_is_not_a_new_version(api, pipeline, Session):
    v1 = _first(api)
    r = api.put(
        f"/api/homefeed/prep-runs/{v1['run_id']}/sections/meeting_objective",
        json={"value": "Objective #1."},
    )
    assert r.status_code == 200
    assert r.json()["version"] == 1
    assert len(_rows(Session)) == 1


def test_edit_on_an_older_version_is_409_with_the_latest(api, pipeline):
    v1 = _first(api)
    v2 = _regen(api)
    r = api.put(
        f"/api/homefeed/prep-runs/{v1['run_id']}/sections/meeting_objective",
        json={"value": "Too late"},
    )
    assert r.status_code == 409
    assert r.json() == {
        "detail": "A newer version (v2) exists — open it to edit.",
        "latest_version": 2,
        "latest_run_id": v2["run_id"],
    }
    # Same rule for a rewrite (and Cubie is never called).
    r = api.post(
        f"/api/homefeed/prep-runs/{v1['run_id']}/sections/meeting_objective/rewrite",
        json={},
    )
    assert r.status_code == 409
    assert pipeline.rewrite_calls == []


def test_unknown_section_is_400(api, pipeline):
    v1 = _first(api)
    for key in ("attendee_intel", "source_evidence", "nope"):
        r = api.put(
            f"/api/homefeed/prep-runs/{v1['run_id']}/sections/{key}",
            json={"value": "x"},
        )
        assert r.status_code == 400, key
        assert isinstance(r.json()["detail"], str)


def test_wrong_shape_is_422_with_a_readable_reason(api, pipeline, Session):
    v1 = _first(api)
    r = api.put(
        f"/api/homefeed/prep-runs/{v1['run_id']}/sections/executive_summary",
        json={"value": ["not", "prose"]},
    )
    assert r.status_code == 422
    assert r.json()["detail"] == "Executive Summary must be text."
    r = api.put(
        f"/api/homefeed/prep-runs/{v1['run_id']}/sections/talking_points",
        json={"value": [1, 2]},
    )
    assert r.status_code == 422
    assert "must be text" in r.json()["detail"]
    assert len(_rows(Session)) == 1


def test_size_errors_name_the_section_the_way_the_reader_sees_it(api, pipeline):
    too_many = [f"Question {i}" for i in range(41)]
    v1 = _first(api)
    r = api.put(
        f"/api/homefeed/prep-runs/{v1['run_id']}/sections/discovery_questions",
        json={"value": too_many},
    )
    assert r.status_code == 422
    assert r.json()["detail"] == (
        "Discovery Questions has too many items (41 of at most 40)."
    )

    # An internal brief calls the same section "Key Questions".
    pipeline.is_internal = True
    internal = _first(api, "evt_internal_sync")
    r = api.put(
        f"/api/homefeed/prep-runs/{internal['run_id']}/sections/discovery_questions",
        json={"value": too_many},
    )
    assert r.status_code == 422
    assert r.json()["detail"] == "Key Questions has too many items (41 of at most 40)."
    r = api.put(
        f"/api/homefeed/prep-runs/{internal['run_id']}/sections/client_profile",
        json={"value": ["not", "prose"]},
    )
    assert r.json()["detail"] == "Relationship Context must be text."


def test_normalize_section_value_labels_follow_is_internal():
    from rubik_core.meeting_prep.sections import normalize_section_value

    with pytest.raises(ValueError, match="^Likely Objections must be text"):
        normalize_section_value("likely_objections", 1)
    with pytest.raises(ValueError, match="^Potential Friction must be text"):
        normalize_section_value("likely_objections", 1, is_internal=True)
    with pytest.raises(ValueError, match="An item in Key Questions is too long"):
        normalize_section_value("discovery_questions", ["x" * 2001], is_internal=True)


def test_unknown_run_is_404(api, pipeline):
    r = api.put(
        "/api/homefeed/prep-runs/does-not-exist/sections/meeting_objective",
        json={"value": "x"},
    )
    assert r.status_code == 404
    assert api.get("/api/homefeed/prep-runs/does-not-exist").status_code == 404


# ─── Ownership ────────────────────────────────────────────────────────────────


def test_writes_are_owner_only_and_admins_can_read_but_not_write(
    api, pipeline, Session
):
    v1 = _first(api)
    run = v1["run_id"]

    api.as_user(OTHER)
    assert api.get(f"/api/homefeed/prep-runs/{run}").status_code == 403
    for method, path, body in (
        ("put", f"/prep-runs/{run}/sections/meeting_objective", {"value": "x"}),
        ("post", f"/prep-runs/{run}/sections/meeting_objective/rewrite", {}),
        ("post", f"/prep-runs/{run}/restore", None),
    ):
        r = getattr(api, method)(f"/api/homefeed{path}", json=body)
        assert r.status_code == 403, path
        assert r.json()["detail"] == "Not your meeting prep"
    # Another reader's list for the same meeting is their own (empty).
    listing = api.get("/api/homefeed/prep-runs", params={"event_id": EVENT}).json()
    assert listing == {
        "event_id": EVENT,
        "latest_version": None,
        "versions": [],
        "pending": None,
    }

    api.as_user(ADMIN, admin=True)
    read = api.get(f"/api/homefeed/prep-runs/{run}")
    assert read.status_code == 200
    # Chain position is the OWNER's chain.
    assert read.json()["is_latest"] is True and read.json()["version"] == 1
    r = api.put(
        f"/api/homefeed/prep-runs/{run}/sections/meeting_objective",
        json={"value": "admin edit"},
    )
    assert r.status_code == 403
    assert api.post(f"/api/homefeed/prep-runs/{run}/restore").status_code == 403
    assert len(_rows(Session)) == 1
    assert pipeline.rewrite_calls == []


# ─── Cubie rewrites one section ───────────────────────────────────────────────


def test_rewrite_appends_a_version_and_merges_sources(api, pipeline, Session):
    v1 = _first(api)
    pipeline.rewrite_value = ["Lead with the renewal", "Then pricing"]
    pipeline.rewrite_sources = [
        "Gmail: Re: renewal (Sep 20)",  # already cited → not duplicated
        "HubSpot: Acme renewal deal",
        "HubSpot: Acme renewal deal",
    ]
    r = api.post(
        f"/api/homefeed/prep-runs/{v1['run_id']}/sections/talking_points/rewrite",
        json={"instruction": "  lead with the renewal  "},
    )
    assert r.status_code == 200, r.text
    v2 = r.json()
    assert v2["version"] == 2
    assert v2["base_version"] == 1
    assert v2["change_kind"] == "section_rewritten"
    assert v2["changed_section"] == "talking_points"
    assert v2["change_note"] == "lead with the renewal"
    assert v2["talking_points"] == ["Lead with the renewal", "Then pricing"]
    assert v2["source_evidence"] == [
        "Gmail: Re: renewal (Sep 20)",
        "HubSpot: Acme renewal deal",
    ]
    meta = v2["section_meta"]["talking_points"]
    assert meta["origin"] == "rewritten"
    assert meta["version"] == 2
    assert meta["instruction"] == "lead with the renewal"

    # The worker saw the brief (no version fields), the stored evidence, the
    # row's persona flags, the instruction, personalization and WGLL.
    (call,) = pipeline.rewrite_calls
    assert call["section_key"] == "talking_points"
    assert call["brief"]["executive_summary"] == "Generated summary #1."
    assert "version" not in call["brief"] and "run_id" not in call["brief"]
    assert call["context_bundle"]["signal_blocks"] == {
        "hubspot": "HUBSPOT: Acme Supply, customer"
    }
    assert call["is_sales_user"] is True and call["is_internal"] is False
    assert call["instruction"] == "lead with the renewal"
    assert call["personalization_block"] == "PERSONAL"
    assert call["wgll_context"] == "WGLL"
    assert pipeline.generate_calls == 1


def test_rewrite_without_instruction_has_no_change_note(api, pipeline):
    v1 = _first(api)
    v2 = api.post(
        f"/api/homefeed/prep-runs/{v1['run_id']}/sections/executive_summary/rewrite"
    ).json()
    assert v2["version"] == 2
    assert v2["change_note"] is None
    assert v2["section_meta"]["executive_summary"]["instruction"] is None
    assert v2["executive_summary"] == "Rewritten summary."


def test_rewrite_failure_is_502_and_stores_nothing(api, pipeline, Session):
    from rubik_core.meeting_prep.rewrite import RewriteError

    v1 = _first(api)
    pipeline.rewrite_error = RewriteError(
        "Cubie couldn't rewrite this section right now — try again."
    )
    r = api.post(
        f"/api/homefeed/prep-runs/{v1['run_id']}/sections/executive_summary/rewrite",
        json={"instruction": "shorter"},
    )
    assert r.status_code == 502
    assert r.json()["detail"] == (
        "Cubie couldn't rewrite this section right now — try again. "
        "Your current version is unchanged."
    )
    # An unexpected crash is a 502 too, never a 500.
    pipeline.rewrite_error = RuntimeError("vertex exploded: secret stack")
    r = api.post(
        f"/api/homefeed/prep-runs/{v1['run_id']}/sections/executive_summary/rewrite",
    )
    assert r.status_code == 502
    assert "secret stack" not in r.json()["detail"]
    assert len(_rows(Session)) == 1
    assert _open(api).json()["run_id"] == v1["run_id"]


def test_rewrite_instruction_too_long_is_422(api, pipeline):
    v1 = _first(api)
    r = api.post(
        f"/api/homefeed/prep-runs/{v1['run_id']}/sections/executive_summary/rewrite",
        json={"instruction": "x" * 5000},
    )
    assert r.status_code == 422
    assert pipeline.rewrite_calls == []


# ─── Restore ──────────────────────────────────────────────────────────────────


def test_restore_appends_a_copy_as_the_new_latest(api, pipeline, Session):
    v1 = _first(api)
    v2 = api.put(
        f"/api/homefeed/prep-runs/{v1['run_id']}/sections/meeting_objective",
        json={"value": "Edited objective"},
    ).json()
    _regen(api)

    r = api.post(f"/api/homefeed/prep-runs/{v2['run_id']}/restore")
    assert r.status_code == 200, r.text
    v4 = r.json()
    assert v4["version"] == 4
    assert v4["change_kind"] == "restored"
    assert v4["base_version"] == 2
    assert v4["is_latest"] is True
    assert v4["meeting_objective"] == "Edited objective"
    assert v4["executive_summary"] == v2["executive_summary"]
    # Provenance comes along, so a later regenerate still keeps the edit.
    assert v4["section_meta"]["meeting_objective"]["origin"] == "edited"
    rows = {r.version: r for r in _rows(Session)}
    assert rows[4].base_run_id == v2["run_id"]
    assert rows[4].context_bundle == rows[2].context_bundle

    # Restoring the latest is a no-op: same run, no new row.
    same = api.post(f"/api/homefeed/prep-runs/{v4['run_id']}/restore").json()
    assert same["run_id"] == v4["run_id"]
    assert len(_rows(Session)) == 4


# ─── A failed generation never becomes a version ──────────────────────────────


def test_failed_first_build_never_becomes_a_version(api, pipeline, Session):
    pipeline.fail_generation = True
    r = _open(api)
    assert r.status_code == 202
    assert api.run_builds() == ["failed"]
    status = api.get(f"/api/homefeed/prep-runs/{r.json()['run_id']}/status").json()
    assert status["status"] == "failed"
    # No version exists yet, so the message must not promise one is unchanged.
    assert status["detail"] == (
        "Cubie couldn't build this briefing — the AI service is busy right now, "
        "so try again in a minute."
    )
    (failed,) = _rows(Session)
    assert failed.id == r.json()["run_id"]
    assert failed.version is None
    assert failed.change_kind == "failed"
    assert failed.delivery_status == "failed"
    assert failed.context_bundle["account_snapshot"] == SNAPSHOT
    assert failed.context_bundle["request"]["kind"] == "first"

    # Still no version: opening again starts a new build, which fails cleanly
    # again.
    again = _open(api)
    assert again.status_code == 202
    assert again.json()["run_id"] != failed.id
    assert api.run_builds() == ["failed"]
    listing = api.get("/api/homefeed/prep-runs", params={"event_id": EVENT}).json()
    assert listing["versions"] == []
    assert listing["pending"] is None

    # A failed row can be inspected but never edited or restored.
    detail = api.get(f"/api/homefeed/prep-runs/{failed.id}").json()
    assert detail["generation_failed"] is True
    assert detail["version"] is None and detail["is_latest"] is False
    r = api.put(
        f"/api/homefeed/prep-runs/{failed.id}/sections/meeting_objective",
        json={"value": "x"},
    )
    assert r.status_code == 409
    assert api.post(f"/api/homefeed/prep-runs/{failed.id}/restore").status_code == 409

    # Once generation works, the first good brief is v1.
    pipeline.fail_generation = False
    v1 = _first(api)
    assert v1["version"] == 1
    assert v1["generation_failed"] is False


def test_failed_regenerate_leaves_the_latest_version_in_place(api, pipeline, Session):
    v1 = _first(api)
    pipeline.fail_generation = True
    status = _regenerate(api, expect="failed")
    assert "Your current version is unchanged." in status["detail"]
    failed = [r for r in _rows(Session) if r.change_kind == "failed"]
    assert len(failed) == 1
    assert failed[0].version is None
    assert failed[0].base_run_id == v1["run_id"]
    # Opening the page still lands on v1 (and does not regenerate).
    calls = pipeline.generate_calls
    again = _open(api).json()
    assert again["run_id"] == v1["run_id"]
    assert again["latest_version"] == 1
    assert again["pending_run"] is None
    assert pipeline.generate_calls == calls


def test_a_first_build_that_cannot_be_saved_fails_cleanly(
    api, pipeline, Session, monkeypatch
):
    def broken(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr("rubik_core.meeting_prep.versions.append_version", broken)
    r = _open(api)
    assert r.status_code == 202, r.text
    assert api.run_builds() == ["failed"]
    status = api.get(f"/api/homefeed/prep-runs/{r.json()['run_id']}/status").json()
    assert status["status"] == "failed"
    assert status["detail"] == background.UNSAVED_DETAIL
    (row,) = _rows(Session)
    assert row.version is None and row.delivery_status == "failed"


UNSAVED_DETAIL = (
    "Cubie built a new version but couldn't save it — your current version "
    "is unchanged. Try again."
)


def test_regenerate_that_cannot_be_saved_is_503_not_an_unsaved_brief(
    api, pipeline, Session, monkeypatch
):
    v1 = _first(api)

    def broken(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr("rubik_core.meeting_prep.versions.append_version", broken)
    # A background regenerate that cannot be stored fails; it never hands
    # back an unsaved brief.
    status = _regenerate(api, expect="failed")
    assert status["detail"] == background.UNSAVED_DETAIL

    # The older POST /meeting-prep still builds in the request: 503.
    r = api.post(
        "/api/homefeed/meeting-prep",
        json={"event": {"id": EVENT, "title": "Acme quarterly sync"}},
    )
    assert r.status_code == 503
    assert r.json() == {"detail": UNSAVED_DETAIL}

    # The saved v1 is still what the page lands on.
    assert [r.version for r in _rows(Session) if r.version is not None] == [1]
    assert _open(api).json()["run_id"] == v1["run_id"]


# ─── Version list ─────────────────────────────────────────────────────────────


def test_version_list_is_newest_first_and_only_the_callers(api, pipeline, Session):
    v1 = _first(api)
    _regen(api)
    v3 = api.post(f"/api/homefeed/prep-runs/{v1['run_id']}/restore").json()
    # Someone else's brief for the same meeting, and a deep run of the owner's.
    with Session() as db:
        versions.append_version(
            db,
            user_email=OTHER,
            event_id=EVENT,
            title="Theirs",
            prep_kind="internal_ea",
            is_sales_user=False,
            is_internal=True,
            model_id="m",
            context_bundle={},
            response={"title": "Theirs"},
            partial_errors=[],
            change_kind="generated",
        )
        db.add(
            MeetingPrepRun(
                id="deep1",
                user_email=OWNER,
                event_id=EVENT,
                mode="deep",
                delivery_status="delivered",
                response={"title": "deep", "sections": []},
            )
        )
        db.commit()

    r = api.get("/api/homefeed/prep-runs", params={"event_id": EVENT})
    assert r.status_code == 200
    body = r.json()
    assert body["event_id"] == EVENT
    assert body["latest_version"] == 3
    assert body["pending"] is None
    assert [v["version"] for v in body["versions"]] == [3, 2, 1]
    assert [v["is_latest"] for v in body["versions"]] == [True, False, False]
    assert body["versions"][0]["run_id"] == v3["run_id"]
    assert body["versions"][0]["change_kind"] == "restored"
    assert body["versions"][0]["base_version"] == 1
    assert body["versions"][1]["base_version"] == 1
    assert body["versions"][2]["base_version"] is None
    assert set(body["versions"][0]) == {
        "run_id",
        "version",
        "created_at",
        "change_kind",
        "change_note",
        "changed_section",
        "base_version",
        "is_latest",
    }
    # Deep Prep is out of the chain: not listed, not openable as a version,
    # not editable.
    assert api.get("/api/homefeed/prep-runs/deep1").status_code == 404
    r = api.put(
        "/api/homefeed/prep-runs/deep1/sections/meeting_objective",
        json={"value": "x"},
    )
    assert r.status_code == 404


# ─── Word export names the version ────────────────────────────────────────────


def test_docx_and_assets_title_carry_the_version(api, pipeline, monkeypatch):
    v1 = _first(api)
    v2 = _regen(api)

    r = api.get(f"/api/homefeed/prep-docs/{v2['run_id']}/docx")
    assert r.status_code == 200
    assert (
        'filename="meeting-prep-acme-quarterly-sync-v2.docx"'
        in r.headers["content-disposition"]
    )
    # Any version can be exported, not only the latest.
    r = api.get(f"/api/homefeed/prep-docs/{v1['run_id']}/docx")
    assert "-v1.docx" in r.headers["content-disposition"]

    saved: dict[str, Any] = {}

    def fake_save(
        user_email,
        *,
        title,
        filename,
        data,
        kind,
        department=None,
        source_prep_run_id=None,
    ):
        saved.update(
            title=title,
            filename=filename,
            kind=kind,
            source_prep_run_id=source_prep_run_id,
        )
        return SimpleNamespace(doc_id="cubiedoc-1", original_filename=filename), True

    monkeypatch.setattr("backend.services.generated_docs.save_generated_doc", fake_save)
    r = api.post(f"/api/homefeed/prep-docs/{v2['run_id']}/save-to-assets")
    assert r.status_code == 200, r.text
    assert saved["title"] == "Acme quarterly sync (v2)"
    assert saved["filename"] == "meeting-prep-acme-quarterly-sync-v2.docx"


def test_docx_of_an_unnumbered_run_keeps_the_plain_filename():
    from backend.routers.home import _render_prep_run_docx

    row = SimpleNamespace(
        response={"title": "Kickoff", "executive_summary": "x"},
        title="Kickoff",
        context_bundle={"start": "Mon"},
        user_email=OWNER,
        version=None,
    )
    _, filename, title = _render_prep_run_docx(row)
    assert filename == "meeting-prep-kickoff.docx"
    assert title == "Kickoff"


# ─── Hydration never trusts the stored JSON ───────────────────────────────────


def test_version_fields_come_from_the_columns_not_the_stored_json(api, Session):
    with Session() as db:
        db.add(
            MeetingPrepRun(
                id="legacy1",
                user_email=OWNER,
                event_id="evt_legacy",
                title="Legacy",
                mode="sync",
                version=1,
                change_kind="generated",
                response={
                    "title": "Legacy",
                    "executive_summary": "Old brief.",
                    # Junk an old row could carry: must be ignored.
                    "run_id": "bogus",
                    "version": 99,
                    "is_latest": False,
                    "section_meta": {"x": {"origin": "edited"}},
                    # A malformed old field degrades to its default, not a 500.
                    "attendee_intel": "not-a-list",
                },
                context_bundle={
                    "start": "Mon Oct 6, 9:00 AM",
                    "crm_provenance": {
                        "schema_version": 1,
                        "workspace": "test",
                        "company_ids": [],
                        "deal_ids": [],
                        "unprovable": False,
                    },
                },
            )
        )
        db.commit()
    r = api.get("/api/homefeed/prep-runs/legacy1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run_id"] == "legacy1"
    assert body["version"] == 1
    assert body["is_latest"] is True
    assert body["section_meta"] == {}
    assert body["attendee_intel"] == []
    assert body["executive_summary"] == "Old brief."
    # Old rows without meeting_time borrow the stored start.
    assert body["meeting_time"] == "Mon Oct 6, 9:00 AM"
    # And the page opens on it without generating.
    assert (
        api.get("/api/homefeed/meeting-prep/evt_legacy").json()["run_id"] == "legacy1"
    )


# ─── The chain itself (no HTTP) ───────────────────────────────────────────────


def _append(db, **overrides):
    kwargs = dict(
        user_email=OWNER,
        event_id=EVENT,
        title="T",
        prep_kind="sales_battle_brief",
        is_sales_user=True,
        is_internal=False,
        model_id="m",
        context_bundle={"title": "T"},
        response={"title": "T", "executive_summary": "s"},
        partial_errors=[],
        change_kind="generated",
    )
    kwargs.update(overrides)
    return versions.append_version(db, **kwargs)


def test_unique_version_race_retries_once_with_the_next_number(Session, monkeypatch):
    with Session() as db:
        v1 = _append(db)
        real = versions.next_version
        calls = {"n": 0}

        def stale_then_real(db_, user_email, event_id):
            calls["n"] += 1
            # First read happened "before" v1 landed: it hands out a taken number.
            return 1 if calls["n"] == 1 else real(db_, user_email, event_id)

        monkeypatch.setattr(
            "rubik_core.meeting_prep.versions.next_version", stale_then_real
        )
        v2 = _append(db, change_kind="regenerated", base_run_id=v1.id)
        assert calls["n"] == 2
        assert v2.version == 2
        assert sorted(r.version for r in db.query(MeetingPrepRun)) == [1, 2]


def test_unique_version_race_gives_up_after_one_retry(Session, monkeypatch):
    with Session() as db:
        _append(db)
        monkeypatch.setattr(
            "rubik_core.meeting_prep.versions.next_version", lambda *a: 1
        )
        with pytest.raises(IntegrityError):
            _append(db, change_kind="regenerated")
        assert db.query(MeetingPrepRun).count() == 1


class _Pg8000Error(Exception):
    """What pg8000 raises for any server error: its first arg is the server's
    error fields, ``C`` being the SQLSTATE."""


def _pg8000_error(sqlstate: str) -> DatabaseError:
    # SQLAlchemy wraps it as a plain DatabaseError — pg8000 never raises its own
    # IntegrityError, so neither does SQLAlchemy.
    return DatabaseError(
        "INSERT INTO meeting_prep_runs ...",
        {},
        _Pg8000Error({"S": "ERROR", "C": sqlstate, "M": "duplicate key value"}),
    )


def _fail_first_commit(monkeypatch, db, exc: Exception) -> dict[str, int]:
    real_commit = db.commit
    calls = {"n": 0}

    def commit():
        calls["n"] += 1
        if calls["n"] == 1:
            raise exc
        real_commit()

    monkeypatch.setattr(db, "commit", commit)
    return calls


def test_a_pg8000_unique_violation_is_retried_like_an_integrity_error(
    Session, monkeypatch
):
    with Session() as db:
        v1 = _append(db)
        calls = _fail_first_commit(monkeypatch, db, _pg8000_error("23505"))
        v2 = _append(db, change_kind="regenerated", base_run_id=v1.id)
        assert calls["n"] == 2
        assert v2.version == 2
        assert sorted(r.version for r in db.query(MeetingPrepRun)) == [1, 2]


def test_any_other_database_error_is_not_retried(Session, monkeypatch):
    with Session() as db:
        _append(db)
        # 57014 = statement timeout: not a collision, so no second insert.
        calls = _fail_first_commit(monkeypatch, db, _pg8000_error("57014"))
        with pytest.raises(DatabaseError):
            _append(db, change_kind="regenerated")
        assert calls["n"] == 1
        assert db.query(MeetingPrepRun).count() == 1


def test_is_unique_violation_reads_each_drivers_sqlstate():
    psycopg = SimpleNamespace(pgcode="23505")
    assert versions.is_unique_violation(DatabaseError("x", {}, psycopg))
    assert versions.is_unique_violation(_pg8000_error("23505"))
    assert versions.is_unique_violation(IntegrityError("x", {}, Exception()))
    assert not versions.is_unique_violation(_pg8000_error("40001"))
    assert not versions.is_unique_violation(RuntimeError("23505"))


def test_must_build_on_latest_with_no_base_needs_an_empty_chain(Session):
    with Session() as db:
        first = _append(db, must_build_on_latest=True)
        assert first.version == 1
        with pytest.raises(versions.StaleVersionError) as exc:
            _append(db, must_build_on_latest=True)
        assert exc.value.latest.id == first.id


def test_an_edit_that_loses_the_race_becomes_a_stale_conflict(Session, monkeypatch):
    with Session() as db:
        v1 = _append(db)
        v2 = _append(db, change_kind="regenerated", base_run_id=v1.id)
        real = versions.latest_version_row
        calls = {"n": 0}

        def stale_then_real(db_, user_email, event_id):
            calls["n"] += 1
            # The edit's first look predates the racing v2.
            return v1 if calls["n"] == 1 else real(db_, user_email, event_id)

        monkeypatch.setattr(
            "rubik_core.meeting_prep.versions.latest_version_row", stale_then_real
        )
        with pytest.raises(versions.StaleVersionError) as exc:
            _append(
                db,
                change_kind="section_edited",
                base_run_id=v1.id,
                changed_section="meeting_objective",
                section_meta={"meeting_objective": {"origin": "edited"}},
                must_build_on_latest=True,
            )
        assert exc.value.latest.id == v2.id
        assert db.query(MeetingPrepRun).count() == 2


def test_no_event_id_means_version_one_and_no_chain(Session):
    with Session() as db:
        a = _append(db, event_id=None)
        b = _append(db, event_id=None)
        assert a.version == b.version == 1
        assert versions.latest_version_row(db, OWNER, None) is None
        assert versions.list_versions(db, OWNER, None) == []
        d = versions.row_to_response_dict(db, b)
        assert d["is_latest"] is True and d["latest_version"] == 1


def test_failed_kind_cannot_be_appended_as_a_version(Session):
    with Session() as db, pytest.raises(ValueError):
        _append(db, change_kind="failed")


def test_carry_forward_keeps_only_hand_edits():
    latest = SimpleNamespace(
        response={
            "talking_points": ["mine"],
            "executive_summary": "cubie rewrite",
            "meeting_objective": "edited but unknown? no — known key",
            "attendee_intel": [{"x": 1}],
        },
        section_meta={
            "talking_points": {"origin": "edited", "version": 2},
            "executive_summary": {"origin": "rewritten", "version": 3},
            "attendee_intel": {"origin": "edited"},  # not an editable section
            "bogus": "not-a-dict",
        },
    )
    fresh = {
        "talking_points": ["new"],
        "executive_summary": "fresh",
        "attendee_intel": [],
        "run_id": "x",
        "version": 7,
    }
    response, meta = versions.carry_forward_edits(latest, fresh)
    assert response["talking_points"] == ["mine"]
    assert response["executive_summary"] == "fresh"
    assert response["attendee_intel"] == []
    assert "run_id" not in response and "version" not in response
    assert meta == {"talking_points": {"origin": "edited", "version": 2}}
    # The latest row's own data is never mutated.
    response["talking_points"].append("more")
    assert latest.response["talking_points"] == ["mine"]


# ─── Deep Prep: no unprovable first-match CRM lookup ──────────────────────────


@pytest.mark.parametrize(
    "signal_blocks",
    [
        {"crm": "CRM ACCOUNT RECORD: Acme Supply"},
        {"hubspot": "HUBSPOT: Acme"},
        {},
    ],
)
def test_deep_prep_never_uses_unprovable_old_crm_lookup(monkeypatch, signal_blocks):
    import asyncio

    from backend.routers import home

    async def collect(**kwargs):
        return {
            "title": "Acme sync",
            "attendees": [{"email": "ann@acmecorp.example"}],
            "signal_blocks": signal_blocks,
            "crm_provenance": {
                "schema_version": 1,
                "workspace": "test",
                "company_ids": [],
                "deal_ids": [],
                "unprovable": False,
            },
        }

    crm_calls: list[str] = []
    seen_ctx: dict[str, Any] = {}

    def fetch_crm(user_email, ctx):
        crm_calls.append(user_email)
        return "CLIENT RELATIONSHIP: Acme"

    async def council(ctx, **kwargs):
        seen_ctx.update(ctx)
        raise RuntimeError("stop after the context is built")

    async def fallback(*a, **kw):
        raise RuntimeError("stop")

    monkeypatch.setattr("backend.routers.home.collect_meeting_prep_context", collect)
    monkeypatch.setattr("backend.routers.home._fetch_crm_relationships", fetch_crm)
    monkeypatch.setattr(
        "backend.routers.home._set_deep_prep_status", lambda *a, **k: None
    )
    monkeypatch.setattr("rubik_core.deep_prep.council.generate_deep_prep", council)
    monkeypatch.setattr("backend.routers.home.generate_meeting_prep_analysis", fallback)

    asyncio.run(home._run_deep_prep("run1", OWNER, "sales", ["user"], EVENT, None))

    assert crm_calls == []
    assert "crm_relationships" not in seen_ctx


def test_revoked_deal_hides_saved_brief_and_blocks_reuse(api, Session, monkeypatch):
    """One revoked deal in a mixed-pipeline prompt invalidates the whole run."""
    brief = _first(api)
    run_id = brief["run_id"]
    with Session() as db:
        row = db.get(MeetingPrepRun, run_id)
        bundle = dict(row.context_bundle)
        marker = dict(bundle["crm_provenance"])
        marker["deal_ids"] = ["deal-allowed", "deal-revoked"]
        bundle["crm_provenance"] = marker
        row.context_bundle = bundle
        db.commit()

    allowed = {"deal-allowed", "deal-revoked"}
    monkeypatch.setattr(
        "rubik_core.tenancy.crm_visibility.visible_source_ids",
        lambda companies, contacts, deals: (companies, contacts, deals & allowed),
    )
    assert api.get(f"/api/homefeed/prep-runs/{run_id}").status_code == 200
    allowed.remove("deal-revoked")

    # A fresh background build can replace the now-inaccessible version.
    assert _open(api).status_code == 202
    assert api.get(f"/api/homefeed/prep-runs/{run_id}").status_code == 404
    assert api.get(f"/api/homefeed/prep-runs/{run_id}/status").status_code == 404
    assert api.get(f"/api/homefeed/prep-docs/{run_id}/docx").status_code == 404
    assert (
        api.post(f"/api/homefeed/prep-docs/{run_id}/save-to-assets").status_code == 404
    )
    assert (
        api.put(
            f"/api/homefeed/prep-runs/{run_id}/sections/talking_points",
            json={"value": "new"},
        ).status_code
        == 404
    )
    assert (
        api.post(
            f"/api/homefeed/prep-runs/{run_id}/sections/talking_points/rewrite",
            json={},
        ).status_code
        == 404
    )
    assert api.post(f"/api/homefeed/prep-runs/{run_id}/restore").status_code == 404
    assert (
        api.get("/api/homefeed/prep-runs", params={"event_id": EVENT}).json()[
            "versions"
        ]
        == []
    )


def test_legacy_brief_without_source_scope_is_hidden(api, Session):
    with Session() as db:
        db.add(
            MeetingPrepRun(
                id="legacy-unproved",
                user_email=OWNER,
                event_id="evt_legacy_unproved",
                title="Old CRM brief",
                mode="sync",
                version=1,
                change_kind="generated",
                response={
                    "title": "Old CRM brief",
                    "executive_summary": "Old deal facts",
                },
                context_bundle={"signal_blocks": {"crm": "Old deal facts"}},
            )
        )
        db.commit()
    assert api.get("/api/homefeed/prep-runs/legacy-unproved").status_code == 404
    assert (
        api.get(
            "/api/homefeed/prep-runs", params={"event_id": "evt_legacy_unproved"}
        ).json()["versions"]
        == []
    )


def test_saved_meeting_prep_asset_denied_after_deal_revoke(api, Session, monkeypatch):
    from datetime import datetime

    from backend.routers import assets
    from backend.services.generated_docs import (
        MEETING_PREP_RUN_TAG_PREFIX,
        MEETING_PREP_WORKSPACE_TAG_PREFIX,
    )
    from rubik_core.dam.registry import AssetMetadata

    brief = _first(api)
    run_id = brief["run_id"]
    with Session() as db:
        row = db.get(MeetingPrepRun, run_id)
        bundle = dict(row.context_bundle)
        marker = dict(bundle["crm_provenance"])
        marker["deal_ids"] = ["deal-allowed", "deal-revoked"]
        bundle["crm_provenance"] = marker
        row.context_bundle = bundle
        db.commit()
    asset = AssetMetadata(
        doc_id="gendoc-prep-1",
        owner=OWNER,
        topic="Meeting Prep",
        tags=[
            "generated-by-cubie",
            "meeting-prep",
            f"{MEETING_PREP_RUN_TAG_PREFIX}{run_id}",
            f"{MEETING_PREP_WORKSPACE_TAG_PREFIX}test",
        ],
        original_filename="meeting-prep-acme.docx",
        source_path="/tmp/prep-test.docx",
        created_at=datetime(2026, 10, 30),
        updated_at=datetime(2026, 10, 30),
    )

    class Registry:
        def get_asset(self, doc_id):
            return asset if doc_id == asset.doc_id else None

        def list_assets(self, **kwargs):
            return {"assets": {asset.doc_id: asset.model_dump()}}

    monkeypatch.setattr(assets, "dam_registry", Registry())
    monkeypatch.setattr(assets, "SessionLocal", Session)
    monkeypatch.setattr(assets, "has_permission", lambda *_a: True)
    monkeypatch.setattr(assets, "has_document_access", lambda *_a, **_k: True)
    monkeypatch.setattr(assets, "load_document_access_policies", lambda *_a: [])
    allowed = {"deal-allowed", "deal-revoked"}
    monkeypatch.setattr(
        "rubik_core.tenancy.crm_visibility.visible_source_ids",
        lambda companies, contacts, deals: (companies, contacts, deals & allowed),
    )
    assert api.get(f"/api/assets/{asset.doc_id}").status_code == 200
    allowed.remove("deal-revoked")

    listing = api.get("/api/assets/")
    assert listing.status_code == 200
    assert listing.json()["assets"] == {}
    for method, path, body in (
        ("get", f"/api/assets/{asset.doc_id}", None),
        ("get", f"/api/assets/{asset.doc_id}/download", None),
        ("post", f"/api/assets/{asset.doc_id}/share", {"emails": [OTHER]}),
        ("post", f"/api/assets/{asset.doc_id}/email-draft", {"to": [OTHER]}),
        ("post", f"/api/assets/{asset.doc_id}/bookmark", None),
        ("patch", f"/api/assets/{asset.doc_id}", {"tags": []}),
    ):
        response = (
            getattr(api, method)(path, json=body)
            if body is not None
            else getattr(api, method)(path)
        )
        assert response.status_code == 404, (path, response.text)

    asset.tags = ["meeting-prep"]
    assert api.get(f"/api/assets/{asset.doc_id}").status_code == 404

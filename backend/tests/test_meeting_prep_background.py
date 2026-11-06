"""Meeting Prep builds in the background and notifies the reader.

The product rules, end to end over HTTP plus the worker:

* opening a meeting with no brief, and Regenerate, answer 202 at once with a
  pending row and dispatch exactly one build; asking again while it runs joins
  that build (no second dispatch, no "two tabs → v1 and v2");
* the worker finalizes THAT row as the next version (same id) and sends one
  "ready" notification linking to the prep page; keep_edits is honoured;
* every failure (model, crash, unknown user, missing event, unsaved) ends in a
  failed row with no version and one "couldn't be built" notification;
* a build past the TTL is reported — and lazily marked — failed, and a worker
  that finishes after that still wins;
* the status route's owner/admin/other rules; the Job entrypoint, the launcher
  on the existing cubie-deep-prep Job, the runner choice and its fallback; the Job's
  deploy script carries the settings a brief reads.

In-memory SQLite (StaticPool), a fresh session per request; the collector, the
model, Google auth and Cloud Run are always faked (patched by STRING target).
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import timedelta
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
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
GHOST = "gone@acme.example"  # signed in, but not in the users table
EVENT = "evt_acme_20261002"
TITLE = "Acme quarterly sync"
BUSY_DETAIL = (
    "Cubie couldn't build this briefing — the AI service is busy right now, "
    "so try again in a minute."
)


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
    """Fake collect + generate, with switches and a hook into the build."""

    def __init__(self):
        self.collect_calls: list[dict[str, Any]] = []
        self.generate_calls = 0
        self.fail_generation = False
        self.generate_error: Exception | None = None
        self.collect_error: Exception | None = None
        self.on_collect = None

    async def collect(self, **kwargs):
        self.collect_calls.append(kwargs)
        if self.on_collect is not None:
            self.on_collect(kwargs)
        if self.collect_error is not None:
            raise self.collect_error
        return {
            "event_id": kwargs.get("event_id"),
            "title": TITLE,
            "start": "2026-10-02T15:00:00Z",
            "start_local": "Thu Oct 2, 10:00 AM CT",
            "description": "",
            "location": "",
            "attendees": [{"email": "ann@acmecorp.example", "display_name": "Ann"}],
            "is_sales_user": True,
            "is_internal": False,
            "focus_on": kwargs.get("focus_on"),
            "errors": [],
            "recent_emails": [],
            "matched_products": [],
            "attendee_intel": [],
            "prior_meetings_block": "",
            "past_meetings_report": "",
            "drive_context": "",
            "signal_blocks": {},
            "account_snapshot": None,
            "signals_used": [],
            "crm_provenance": {
                "schema_version": 1,
                "workspace": "test",
                "company_ids": [],
                "deal_ids": [],
                "unprovable": False,
            },
        }

    async def generate(
        self, ctx, user_email, personalization_block="", wgll_context=""
    ):
        self.generate_calls += 1
        n = self.generate_calls
        if self.generate_error is not None:
            raise self.generate_error
        if self.fail_generation:
            return MeetingPrepResponse(
                title=ctx["title"],
                executive_summary="Error generating analysis: 429 RESOURCE_EXHAUSTED",
                partial_errors=["429 RESOURCE_EXHAUSTED"],
                generation_failed=True,
            )
        return MeetingPrepResponse(
            title=ctx["title"],
            executive_summary=f"Generated summary #{n}.",
            meeting_objective=f"Objective #{n}.",
            talking_points=[f"Point A #{n}", f"Point B #{n}"],
            stakeholder_map=json.dumps([]),
            is_sales_user=True,
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
    return p


@pytest.fixture
def api(Session, pipeline, monkeypatch):
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

    monkeypatch.setattr("rubik_core.meeting_prep.background.SessionLocal", Session)
    monkeypatch.setattr("rubik_core.meeting_prep.background.dispatch", fake_dispatch)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = lambda: current["user"]
    app.dependency_overrides[require_full_app_access] = lambda: current["user"]
    client = TestClient(app)
    client.as_user = as_user  # type: ignore[attr-defined]
    client.dispatched = dispatched  # type: ignore[attr-defined]
    client.run_builds = run_builds  # type: ignore[attr-defined]
    yield client
    app.dependency_overrides.clear()


def _open(api, event_id: str = EVENT):
    return api.get(f"/api/homefeed/meeting-prep/{event_id}")


def _status(api, run_id: str):
    return api.get(f"/api/homefeed/prep-runs/{run_id}/status")


def _regenerate(api, **body):
    return api.post(
        "/api/homefeed/prep-runs/regenerate", json={"event_id": EVENT, **body}
    )


def _first(api, event_id: str = EVENT) -> dict[str, Any]:
    assert _open(api, event_id).status_code == 202
    assert api.run_builds() == ["ready"]
    r = _open(api, event_id)
    assert r.status_code == 200, r.text
    return r.json()


def _rows(Session, **filters) -> list[MeetingPrepRun]:
    with Session() as db:
        q = db.query(MeetingPrepRun)
        for k, v in filters.items():
            q = q.filter(getattr(MeetingPrepRun, k) == v)
        rows = q.order_by(MeetingPrepRun.created_at).all()
        for r in rows:
            db.expunge(r)
        return rows


def _row(Session, run_id: str) -> MeetingPrepRun:
    with Session() as db:
        row = db.get(MeetingPrepRun, run_id)
        db.expunge(row)
        return row


def _notifications(Session) -> list[Notification]:
    with Session() as db:
        rows = db.query(Notification).order_by(Notification.created_at).all()
        for r in rows:
            db.expunge(r)
        return rows


def _age(Session, run_id: str, minutes: int) -> None:
    """Pretend the build was asked for ``minutes`` ago."""
    with Session() as db:
        row = db.get(MeetingPrepRun, run_id)
        row.created_at = versions.utcnow_naive() - timedelta(minutes=minutes)
        db.commit()


def _versions(Session) -> list[int]:
    return sorted(r.version for r in _rows(Session) if r.version is not None)


# ─── Starting a build ─────────────────────────────────────────────────────────


def test_first_open_answers_202_with_one_pending_row_and_one_dispatch(
    api, pipeline, Session
):
    r = api.get(
        f"/api/homefeed/meeting-prep/{EVENT}", params={"focus_on": "the renewal"}
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert set(body) == {"status", "run_id", "event_id", "kind", "started_at", "title"}
    assert body["status"] == "queued"
    assert body["event_id"] == EVENT
    assert body["kind"] == "first"
    assert body["title"] is None
    assert body["started_at"]
    # Nothing was gathered or generated in the request.
    assert pipeline.collect_calls == [] and pipeline.generate_calls == 0

    (row,) = _rows(Session)
    assert row.id == body["run_id"]
    assert row.mode == "sync"
    assert row.version is None
    assert row.change_kind == "pending"
    assert row.delivery_status == "queued"
    assert row.response is None
    assert row.user_email == OWNER and row.event_id == EVENT
    req = row.context_bundle["request"]
    assert req["kind"] == "first"
    assert req["focus_on"] == "the renewal"
    assert req["keep_edits"] is False
    assert req["requested_at"] == body["started_at"]

    assert api.dispatched == [
        {
            "run_id": row.id,
            "user_email": OWNER,
            "event_id": EVENT,
            "focus_on": "the renewal",
            "keep_edits": False,
            "product_to_emphasize": None,
        }
    ]


def test_opening_again_while_it_builds_joins_the_same_build(api, pipeline, Session):
    first = _open(api).json()
    again = _open(api)
    assert again.status_code == 202
    assert again.json()["run_id"] == first["run_id"]
    # A regenerate asked meanwhile joins it too.
    regen = _regenerate(api)
    assert regen.status_code == 202
    assert regen.json()["run_id"] == first["run_id"]
    assert len(api.dispatched) == 1
    assert len(_rows(Session)) == 1

    # One build → one version, not v1 and v2.
    assert api.run_builds() == ["ready"]
    assert _versions(Session) == [1]
    assert pipeline.generate_calls == 1


def test_a_build_that_cannot_be_dispatched_is_502_and_marked_failed(
    api, pipeline, Session, monkeypatch
):
    def broken(**kwargs):
        raise RuntimeError("pool shut down")

    monkeypatch.setattr("rubik_core.meeting_prep.background.dispatch", broken)
    r = _open(api)
    assert r.status_code == 502
    assert r.json() == {"detail": background.NOT_STARTED_DETAIL}
    (row,) = _rows(Session)
    assert row.delivery_status == "failed" and row.change_kind == "failed"
    assert row.version is None
    # The reader is looking at the error: no notification on top.
    assert _notifications(Session) == []


def test_regenerate_needs_an_event(api, pipeline, Session):
    r = api.post("/api/homefeed/prep-runs/regenerate", json={"event_id": "  "})
    assert r.status_code == 400
    assert _rows(Session) == []
    assert api.dispatched == []


# ─── The worker: success ──────────────────────────────────────────────────────


def test_worker_finalizes_the_same_row_as_v1_and_notifies_once(api, pipeline, Session):
    observed: list[str] = []

    def during_collect(kwargs):
        with Session() as db:
            observed.append(
                background.status_payload(db.get(MeetingPrepRun, run_id))["status"]
            )

    pipeline.on_collect = during_collect
    accepted = _open(api).json()
    run_id = accepted["run_id"]
    assert api.run_builds() == ["ready"]
    assert observed == ["running"]

    (row,) = _rows(Session)
    assert row.id == run_id
    assert row.version == 1
    assert row.change_kind == "generated"
    assert row.delivery_status == "ready"
    assert row.base_run_id is None
    assert row.title == TITLE
    assert row.response["executive_summary"] == "Generated summary #1."
    assert row.context_bundle["request"]["kind"] == "first"
    assert row.context_bundle["request"]["requested_at"] == accepted["started_at"]
    assert row.context_bundle["title"] == TITLE  # the full bundle, as before
    # created_at is the moment it became a version.
    assert versions.iso(row.created_at) >= accepted["started_at"]

    # The worker built for the right reader, with their context.
    (call,) = pipeline.collect_calls
    assert call["user_email"] == OWNER
    assert call["event_id"] == EVENT
    assert call["user_department"] == "sales"
    assert call["reader"].email == OWNER

    (note,) = _notifications(Session)
    assert note.actor_email == OWNER
    assert note.kind == "meeting_prep_ready"
    assert note.severity == "info"
    assert note.title == "Your meeting prep is ready"
    assert note.body == f"{TITLE} — v1 is ready to review."
    assert note.link == f"/meeting-prep/{EVENT}"
    assert note.read_at is None

    status = _status(api, run_id).json()
    assert status == {
        "run_id": run_id,
        "event_id": EVENT,
        "status": "ready",
        "kind": "first",
        "version": 1,
        "detail": None,
        "started_at": accepted["started_at"],
        "finished_at": versions.iso(row.created_at),
    }
    page = _open(api).json()
    assert page["run_id"] == run_id and page["version"] == 1
    assert page["pending_run"] is None


def test_the_link_encodes_the_event_id_like_the_app_does(api, pipeline, Session):
    event_id = "rec/evt@group x"
    r = _open(api, event_id)
    assert r.status_code == 202
    assert r.json()["event_id"] == event_id
    assert api.run_builds() == ["ready"]
    (note,) = _notifications(Session)
    # encodeURIComponent("rec/evt@group x")
    assert note.link == "/meeting-prep/rec%2Fevt%40group%20x"


def test_running_the_same_build_twice_does_nothing_the_second_time(
    api, pipeline, Session
):
    _open(api)
    kwargs = dict(api.dispatched[0])
    assert api.run_builds() == ["ready"]
    assert background.run_build_sync(**kwargs) == "ready"
    assert pipeline.generate_calls == 1
    assert _versions(Session) == [1]
    assert len(_notifications(Session)) == 1


def test_a_build_left_running_by_a_crashed_attempt_is_built_again(
    api, pipeline, Session
):
    run_id = _open(api).json()["run_id"]
    with Session() as db:
        db.get(MeetingPrepRun, run_id).delivery_status = "running"
        db.commit()
    assert api.run_builds() == ["ready"]
    assert _row(Session, run_id).version == 1


def test_a_build_does_not_run_for_arguments_that_do_not_match_its_row(
    api, pipeline, Session
):
    _open(api)
    kwargs = dict(api.dispatched[0], user_email=OTHER)
    assert background.run_build_sync(**kwargs) == "skipped"
    assert background.run_build_sync(**dict(kwargs, run_id="nope")) == "missing"
    assert pipeline.collect_calls == []


# ─── Regenerate ───────────────────────────────────────────────────────────────


def _hand_edit(api, run_id: str) -> dict[str, Any]:
    r = api.put(
        f"/api/homefeed/prep-runs/{run_id}/sections/talking_points",
        json={"value": ["My own opener"]},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_regenerate_builds_v2_in_the_background_keeping_hand_edits(
    api, pipeline, Session
):
    v1 = _first(api)
    v2 = _hand_edit(api, v1["run_id"])
    r = _regenerate(api, focus_on="pricing")
    assert r.status_code == 202, r.text
    accepted = r.json()
    assert accepted["kind"] == "regenerate"
    assert accepted["title"] == TITLE
    assert api.dispatched[-1] == {
        "run_id": accepted["run_id"],
        "user_email": OWNER,
        "event_id": EVENT,
        "focus_on": "pricing",
        "keep_edits": True,
        "product_to_emphasize": None,
    }

    assert api.run_builds() == ["ready"]
    v3 = api.get(f"/api/homefeed/prep-runs/{accepted['run_id']}").json()
    assert v3["version"] == 3
    assert v3["change_kind"] == "regenerated"
    assert v3["base_version"] == 2
    assert v3["talking_points"] == ["My own opener"]
    assert v3["section_meta"]["talking_points"]["origin"] == "edited"
    assert v3["executive_summary"] == "Generated summary #2."
    assert pipeline.collect_calls[-1]["focus_on"] == "pricing"
    row = _row(Session, accepted["run_id"])
    assert row.base_run_id == v2["run_id"]
    assert row.context_bundle["request"]["kind"] == "regenerate"
    assert row.context_bundle["request"]["keep_edits"] is True

    ready = [n for n in _notifications(Session) if n.kind == "meeting_prep_ready"]
    assert [n.body for n in ready] == [
        f"{TITLE} — v1 is ready to review.",
        f"{TITLE} — v3 is ready to review.",
    ]


def test_regenerate_with_keep_edits_false_drops_hand_edits(api, pipeline, Session):
    v1 = _first(api)
    _hand_edit(api, v1["run_id"])
    accepted = _regenerate(api, keep_edits=False).json()
    assert api.dispatched[-1]["keep_edits"] is False
    assert api.run_builds() == ["ready"]
    v3 = api.get(f"/api/homefeed/prep-runs/{accepted['run_id']}").json()
    assert v3["version"] == 3
    assert v3["talking_points"] == ["Point A #2", "Point B #2"]
    assert v3["section_meta"] == {}


def test_page_and_list_show_the_build_in_flight(api, pipeline, Session):
    v1 = _first(api)
    accepted = _regenerate(api).json()

    page = _open(api)
    assert page.status_code == 200
    body = page.json()
    assert body["run_id"] == v1["run_id"]  # still the current version
    pending = {
        "run_id": accepted["run_id"],
        "status": "queued",
        "kind": "regenerate",
        "started_at": accepted["started_at"],
    }
    assert body["pending_run"] == pending
    listing = api.get("/api/homefeed/prep-runs", params={"event_id": EVENT}).json()
    assert listing["pending"] == pending
    assert [v["version"] for v in listing["versions"]] == [1]
    assert _status(api, accepted["run_id"]).json()["status"] == "queued"

    assert api.run_builds() == ["ready"]
    body = _open(api).json()
    assert body["version"] == 2 and body["pending_run"] is None
    listing = api.get("/api/homefeed/prep-runs", params={"event_id": EVENT}).json()
    assert listing["pending"] is None
    assert [v["version"] for v in listing["versions"]] == [2, 1]


# ─── The worker: failures ─────────────────────────────────────────────────────


def _assert_failed(Session, run_id: str, detail: str) -> None:
    row = _row(Session, run_id)
    assert row.version is None
    assert row.change_kind == "failed"
    assert row.delivery_status == "failed"
    assert row.context_bundle["request"]["detail"] == detail
    assert detail in (row.partial_errors or [])
    assert _versions(Session) == []
    (note,) = _notifications(Session)
    assert note.actor_email == row.user_email
    assert note.kind == "meeting_prep_failed"
    assert note.severity == "warn"
    assert note.title == "Meeting prep couldn't be built"
    assert note.link == f"/meeting-prep/{EVENT}"


def test_a_failed_generation_is_a_failed_row_and_a_failed_notification(
    api, pipeline, Session
):
    pipeline.fail_generation = True
    run_id = _open(api).json()["run_id"]
    assert api.run_builds() == ["failed"]
    _assert_failed(Session, run_id, BUSY_DETAIL)
    row = _row(Session, run_id)
    # The failure keeps the evidence it was built from, for audit.
    assert row.context_bundle["title"] == TITLE
    assert row.response["executive_summary"].startswith("Error generating analysis")
    assert row.change_kind == "failed"
    (note,) = _notifications(Session)
    assert note.body == f"{TITLE}: {BUSY_DETAIL}"

    status = _status(api, run_id).json()
    assert status["status"] == "failed"
    assert status["detail"] == BUSY_DETAIL
    assert status["version"] is None
    assert status["finished_at"]


def test_a_crash_in_the_build_is_a_failed_row_without_the_raw_error(
    api, pipeline, Session
):
    pipeline.generate_error = RuntimeError("vertex exploded: secret stack")
    run_id = _open(api).json()["run_id"]
    assert api.run_builds() == ["failed"]
    _assert_failed(Session, run_id, background.GENERIC_FAILED_DETAIL)
    (note,) = _notifications(Session)
    assert "secret stack" not in note.body
    assert "secret stack" not in json.dumps(_status(api, run_id).json())


def test_an_unknown_user_is_a_failed_row(api, pipeline, Session):
    api.as_user(GHOST)
    run_id = _open(api).json()["run_id"]
    assert api.run_builds() == ["failed"]
    _assert_failed(Session, run_id, background.NO_ACCOUNT_DETAIL)
    assert pipeline.collect_calls == []
    assert _notifications(Session)[0].actor_email == GHOST


def test_a_meeting_no_longer_on_the_calendar_is_a_failed_row(api, pipeline, Session):
    pipeline.collect_error = HTTPException(status_code=404, detail="Event x not found.")
    run_id = _open(api).json()["run_id"]
    assert api.run_builds() == ["failed"]
    _assert_failed(Session, run_id, background.EVENT_NOT_FOUND_DETAIL)


def test_a_failed_regenerate_says_the_current_version_is_unchanged(
    api, pipeline, Session
):
    v1 = _first(api)
    pipeline.fail_generation = True
    run_id = _regenerate(api).json()["run_id"]
    assert api.run_builds() == ["failed"]
    status = _status(api, run_id).json()
    assert status["status"] == "failed"
    assert status["detail"].endswith("Your current version is unchanged.")
    row = _row(Session, run_id)
    assert row.version is None and row.base_run_id == v1["run_id"]
    assert _versions(Session) == [1]
    assert _open(api).json()["run_id"] == v1["run_id"]
    kinds = [n.kind for n in _notifications(Session)]
    assert kinds == ["meeting_prep_ready", "meeting_prep_failed"]


# ─── Too long: the TTL ────────────────────────────────────────────────────────


def test_a_build_past_the_ttl_is_reported_failed_and_the_worker_still_wins(
    api, pipeline, Session
):
    run_id = _open(api).json()["run_id"]
    _age(Session, run_id, background.ttl_minutes() + 1)

    status = _status(api, run_id).json()
    assert status["status"] == "failed"
    assert status["detail"] == background.TTL_DETAIL
    row = _row(Session, run_id)
    assert row.delivery_status == "failed"  # lazily marked, not just reported
    assert row.context_bundle["request"]["failed_by"] == "ttl"
    (note,) = _notifications(Session)
    assert note.kind == "meeting_prep_failed"
    assert note.body == f"Your meeting: {background.TTL_DETAIL}"
    # Polling again does not notify again.
    assert _status(api, run_id).json()["status"] == "failed"
    assert len(_notifications(Session)) == 1

    # The worker finishes after all: it still becomes v1 and still notifies.
    assert api.run_builds() == ["ready"]
    status = _status(api, run_id).json()
    assert status["status"] == "ready" and status["version"] == 1
    assert status["detail"] is None
    assert _row(Session, run_id).change_kind == "generated"
    kinds = [n.kind for n in _notifications(Session)]
    assert kinds == ["meeting_prep_failed", "meeting_prep_ready"]


def test_a_stale_build_no_longer_blocks_a_new_one(api, pipeline, Session):
    old = _open(api).json()["run_id"]
    _age(Session, old, background.ttl_minutes() + 1)
    r = _open(api)
    assert r.status_code == 202
    new = r.json()["run_id"]
    assert new != old
    assert len(api.dispatched) == 2
    assert _row(Session, old).delivery_status == "failed"
    assert _row(Session, new).delivery_status == "queued"


def test_two_pollers_past_the_ttl_send_one_notification(api, pipeline, Session):
    run_id = _open(api).json()["run_id"]
    _age(Session, run_id, background.ttl_minutes() + 1)
    with Session() as a, Session() as b:
        row_a = a.get(MeetingPrepRun, run_id)
        row_b = b.get(MeetingPrepRun, run_id)
        assert background.expire_if_stale(a, row_a) is True
        assert background.mark_failed(b, row_b, detail="x", failed_by="ttl") is False
    assert len(_notifications(Session)) == 1


def test_a_young_build_is_not_expired(api, pipeline, Session):
    run_id = _open(api).json()["run_id"]
    _age(Session, run_id, background.queued_ttl_minutes() - 1)
    assert _status(api, run_id).json()["status"] == "queued"
    # A build that STARTED is only held to the full TTL.
    with Session() as db:
        db.get(MeetingPrepRun, run_id).delivery_status = "running"
        db.commit()
    _age(Session, run_id, background.ttl_minutes() - 1)
    assert _status(api, run_id).json()["status"] == "running"
    assert _notifications(Session) == []


def test_a_build_still_queued_past_the_queued_ttl_never_started(api, pipeline, Session):
    assert background.queued_ttl_minutes() < background.ttl_minutes()
    run_id = _open(api).json()["run_id"]
    _age(Session, run_id, background.queued_ttl_minutes() + 1)

    status = _status(api, run_id).json()
    assert status["status"] == "failed"
    assert status["detail"] == background.NOT_STARTED_DETAIL
    assert (
        status["detail"] == "Cubie couldn't start building this briefing — try again."
    )
    row = _row(Session, run_id)
    assert row.delivery_status == "failed" and row.version is None
    assert row.context_bundle["request"]["failed_by"] == "queued_ttl"
    (note,) = _notifications(Session)
    assert note.kind == "meeting_prep_failed"
    assert note.body == f"Your meeting: {background.NOT_STARTED_DETAIL}"
    # It no longer blocks a fresh build.
    assert _open(api).json()["run_id"] != run_id


def test_the_notifications_feed_reports_a_dead_build_to_a_reader_who_left(
    api, pipeline, Session
):
    """Nobody reopens the meeting: the bell's own poll settles the build and
    the "couldn't be built" notification is in that very poll — once."""
    stuck = _open(api).json()["run_id"]
    never_started = _open(api, "evt_other").json()["run_id"]
    young = _open(api, "evt_young").json()["run_id"]
    # Another reader's stale build is theirs to be told about, not ours.
    api.as_user(OTHER)
    theirs = _open(api).json()["run_id"]
    # The worker died mid-build; another never started; time passes.
    with Session() as db:
        db.get(MeetingPrepRun, stuck).delivery_status = "running"
        db.commit()
    _age(Session, stuck, background.ttl_minutes() + 1)
    _age(Session, never_started, background.queued_ttl_minutes() + 1)
    _age(Session, theirs, background.ttl_minutes() + 1)
    assert _notifications(Session) == []

    api.as_user(OWNER)
    feed = api.get("/api/notifications/")
    assert feed.status_code == 200, feed.text
    assert sorted(n["body"] for n in feed.json()) == sorted(
        [
            f"Your meeting: {background.TTL_DETAIL}",
            f"Your meeting: {background.NOT_STARTED_DETAIL}",
        ]
    )
    assert {n["link"] for n in feed.json()} == {
        f"/meeting-prep/{EVENT}",
        "/meeting-prep/evt_other",
    }
    assert _row(Session, stuck).context_bundle["request"]["failed_by"] == "ttl"
    assert _row(Session, never_started).delivery_status == "failed"
    assert _row(Session, young).delivery_status == "queued"
    assert _row(Session, theirs).delivery_status == "queued"
    # Polling again notifies nobody twice.
    assert len(api.get("/api/notifications/").json()) == 2
    assert len(_notifications(Session)) == 2


def test_the_notifications_feed_survives_a_broken_build_sweep(
    api, pipeline, Session, monkeypatch
):
    def broken(db, user_email):
        raise RuntimeError("meeting_prep_runs unreachable")

    monkeypatch.setattr(
        "rubik_core.meeting_prep.background.expire_stale_for_user", broken
    )
    r = api.get("/api/notifications/")
    assert r.status_code == 200
    assert r.json() == []


def test_the_build_sweep_never_raises(Session, monkeypatch):
    class Broken:
        def query(self, *a, **k):
            raise RuntimeError("database down")

        def rollback(self):
            pass

    assert background.expire_stale_for_user(Broken(), OWNER) == 0
    assert background.expire_stale_for_user(Broken(), "") == 0


# ─── Status route access ──────────────────────────────────────────────────────


def test_status_is_owner_or_admin(api, pipeline, Session):
    run_id = _open(api).json()["run_id"]
    assert _status(api, "does-not-exist").status_code == 404

    api.as_user(OTHER)
    r = _status(api, run_id)
    assert r.status_code == 403
    assert r.json()["detail"] == "Not your meeting prep"
    # Nor can another reader join or see it.
    listing = api.get("/api/homefeed/prep-runs", params={"event_id": EVENT}).json()
    assert listing["pending"] is None

    api.as_user(ADMIN, admin=True)
    r = _status(api, run_id)
    assert r.status_code == 200
    assert r.json()["status"] == "queued"


def test_status_of_a_deep_prep_run_is_404(api, Session):
    with Session() as db:
        db.add(
            MeetingPrepRun(
                id="deep1",
                user_email=OWNER,
                event_id=EVENT,
                mode="deep",
                delivery_status="queued",
            )
        )
        db.commit()
    assert _status(api, "deep1").status_code == 404


# ─── Runner: Job, thread pool, fallback ───────────────────────────────────────


class _FakeCreds:
    token = "tok"

    def refresh(self, _req):
        pass


class _FakeResp:
    def __init__(self, status_code=200, payload=None, text="{}"):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def _patch_auth(monkeypatch):
    monkeypatch.setattr("backend.jobs.launcher.gcp_project_id", lambda: "proj")
    monkeypatch.setattr("google.auth.compute_engine.Credentials", _FakeCreds)
    monkeypatch.setattr(
        "google.auth.default", lambda scopes=None: (_FakeCreds(), "proj")
    )
    monkeypatch.setattr("google.auth.transport.requests.Request", lambda: object())


def _launch(monkeypatch, **overrides) -> dict[str, Any]:
    """Run the launcher against a fake Cloud Run Admin API; the request it
    sent (url, json body, headers)."""
    import backend.routers.home as home

    _patch_auth(monkeypatch)
    captured: dict[str, Any] = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(url=url, json=json, headers=headers)
        return _FakeResp(200, {"name": "exec-1"})

    monkeypatch.setattr("requests.post", fake_post)
    kwargs = dict(
        run_id="r1",
        user_email=OWNER,
        event_id="ev1",
        focus_on="-pricing first",
        keep_edits=False,
    )
    kwargs.update(overrides)
    assert home._launch_meeting_prep_job_execution(**kwargs) == {"name": "exec-1"}
    return captured


def test_launcher_runs_the_meeting_prep_module_on_the_deep_prep_job(monkeypatch):
    from backend.jobs.meeting_prep_job import _build_parser

    captured = _launch(monkeypatch)
    assert captured["url"].endswith("/jobs/cubie-deep-prep:run")
    assert captured["headers"]["Authorization"] == "Bearer tok"
    args = captured["json"]["overrides"]["containerOverrides"][0]["args"]
    assert {"name": "CUBIE_WORKSPACE", "value": "test"} in captured["json"][
        "overrides"
    ]["containerOverrides"][0]["env"]
    # The override REPLACES the container args: the module comes first.
    assert args[:2] == ["-m", "backend.jobs.meeting_prep_job"]
    assert args[2:] == [
        "--run-id=r1",
        f"--user-email={OWNER}",
        "--event-id=ev1",
        "--focus-on=-pricing first",
        "--keep-edits=false",
    ]
    # What the Job receives parses back to exactly what was asked, even a focus
    # that begins with a dash.
    parsed = _build_parser().parse_args(args[2:])
    assert parsed.focus_on == "-pricing first"
    assert parsed.keep_edits == "false"
    assert parsed.product_to_emphasize is None


def test_launcher_sends_every_value_as_flag_equals_value(monkeypatch):
    """An event id that begins with a dash used to be sent as its own token,
    which argparse read as a flag: exit 2 before the build, twice (the retry),
    and the reader saw Queued for the whole TTL."""
    from backend.jobs.meeting_prep_job import _build_parser

    captured = _launch(
        monkeypatch,
        event_id="-abc",
        user_email="--odd@x.example",
        product_to_emphasize="-QC",
        keep_edits=True,
    )
    args = captured["json"]["overrides"]["containerOverrides"][0]["args"]
    flags = args[2:]
    assert all(a.startswith("--") and "=" in a for a in flags), flags
    parsed = _build_parser().parse_args(flags)
    assert parsed.run_id == "r1"
    assert parsed.event_id == "-abc"
    assert parsed.user_email == "--odd@x.example"
    assert parsed.product_to_emphasize == "-QC"
    assert parsed.keep_edits == "true"


def test_launcher_forwards_the_services_switches_to_the_job(monkeypatch):
    from rubik_core.config import Settings

    monkeypatch.setattr("rubik_core.config.settings.TODOS_ENABLED", True)
    monkeypatch.setattr(
        "rubik_core.config.settings.PROMISE_ACCOUNTABILITY_ENABLED", False
    )
    monkeypatch.setattr("rubik_core.config.settings.CUBIE_PREP_CRM_ENABLED", False)
    monkeypatch.setattr("rubik_core.config.settings.CUBIE_PREP_XRAY_ENABLED", True)
    monkeypatch.setattr(
        "rubik_core.config.settings.CUBIE_PREP_ATTENDANCE_LOOKUP", False
    )
    captured = _launch(monkeypatch)
    (container,) = captured["json"]["overrides"]["containerOverrides"]
    env = {e["name"]: e["value"] for e in container["env"]}
    switches = {
        name
        for name in Settings.model_fields
        if name.startswith("CUBIE_PREP_") and name.endswith("_ENABLED")
    }
    assert {"CUBIE_PREP_CRM_ENABLED", "CUBIE_PREP_PROMISES_ENABLED"} <= switches
    assert set(env) == switches | {
        "TODOS_ENABLED",
        "PROMISE_ACCOUNTABILITY_ENABLED",
        "CUBIE_PREP_ATTENDANCE_LOOKUP",
        "CUBIE_WORKSPACE",
    }
    assert env["TODOS_ENABLED"] == "true"
    assert env["PROMISE_ACCOUNTABILITY_ENABLED"] == "false"
    assert env["CUBIE_PREP_CRM_ENABLED"] == "false"
    assert env["CUBIE_PREP_XRAY_ENABLED"] == "true"
    assert env["CUBIE_PREP_ATTENDANCE_LOOKUP"] == "false"
    assert {value for name, value in env.items() if name != "CUBIE_WORKSPACE"} <= {
        "true",
        "false",
    }


def test_launcher_caps_the_execution_at_the_build_ttl(monkeypatch):
    monkeypatch.setattr(
        "rubik_core.config.settings.CUBIE_PREP_BACKGROUND_TTL_MINUTES", 20
    )
    captured = _launch(monkeypatch)
    assert captured["json"]["overrides"]["timeout"] == "1200s"


def test_launcher_failure_raises(monkeypatch):
    import backend.routers.home as home

    _patch_auth(monkeypatch)
    monkeypatch.setattr(
        "requests.post",
        lambda url, json=None, headers=None, timeout=None: _FakeResp(
            403, {}, text="permission denied"
        ),
    )
    with pytest.raises(HTTPException) as ei:
        home._launch_meeting_prep_job_execution(
            run_id="r1",
            user_email=OWNER,
            event_id="ev1",
            focus_on=None,
            keep_edits=True,
        )
    assert ei.value.status_code == 502


@pytest.mark.parametrize(
    ("setting", "app_mode", "expected"),
    [
        ("auto", "local", "inprocess"),
        ("auto", "gcp", "job"),
        ("inprocess", "gcp", "inprocess"),
        ("job", "local", "job"),
        ("", "local", "inprocess"),
    ],
)
def test_runner_mode(monkeypatch, setting, app_mode, expected):
    from rubik_core.config import AppMode

    monkeypatch.setattr("rubik_core.config.settings.CUBIE_PREP_RUNNER", setting)
    monkeypatch.setattr("rubik_core.config.settings.APP_MODE", AppMode(app_mode))
    assert background.runner_mode() == expected


class _FakePool:
    def __init__(self):
        self.submitted: list[tuple] = []

    def submit(self, fn, *args):
        self.submitted.append((fn, args))


def _dispatch_once() -> str:
    return background.dispatch(
        run_id="r1",
        user_email=OWNER,
        event_id=EVENT,
        focus_on="pricing",
        keep_edits=True,
    )


def test_dispatch_launches_the_job_in_job_mode(monkeypatch):
    pool = _FakePool()
    launched: list[dict[str, Any]] = []
    monkeypatch.setattr("rubik_core.meeting_prep.background._POOL", pool)
    monkeypatch.setattr("rubik_core.meeting_prep.background.runner_mode", lambda: "job")
    monkeypatch.setattr(
        "backend.routers.home._launch_meeting_prep_job_execution",
        lambda **kw: launched.append(kw) or {"name": "exec"},
    )
    assert _dispatch_once() == "job"
    assert launched == [
        {
            "run_id": "r1",
            "user_email": OWNER,
            "event_id": EVENT,
            "focus_on": "pricing",
            "keep_edits": True,
            "product_to_emphasize": None,
        }
    ]
    assert pool.submitted == []


def test_a_job_launch_failure_falls_back_to_in_process(monkeypatch):
    pool = _FakePool()
    monkeypatch.setattr("rubik_core.meeting_prep.background._POOL", pool)
    monkeypatch.setattr("rubik_core.meeting_prep.background.runner_mode", lambda: "job")

    def broken(**kw):
        raise HTTPException(status_code=502, detail="Failed to launch")

    monkeypatch.setattr(
        "backend.routers.home._launch_meeting_prep_job_execution", broken
    )
    assert _dispatch_once() == "inprocess"
    ((fn, args),) = pool.submitted
    assert fn is background._run_in_thread
    assert args == ("r1", OWNER, EVENT, "pricing", True, None)


def test_in_process_mode_never_touches_cloud_run(monkeypatch):
    pool = _FakePool()
    monkeypatch.setattr("rubik_core.meeting_prep.background._POOL", pool)
    monkeypatch.setattr(
        "rubik_core.meeting_prep.background.runner_mode", lambda: "inprocess"
    )

    def must_not_launch(**kw):
        raise AssertionError("launched a Cloud Run Job in in-process mode")

    monkeypatch.setattr(
        "backend.routers.home._launch_meeting_prep_job_execution", must_not_launch
    )
    assert _dispatch_once() == "inprocess"
    assert len(pool.submitted) == 1


def test_the_thread_pool_entry_runs_the_worker(monkeypatch):
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "rubik_core.meeting_prep.background.run_build_sync",
        lambda **kw: calls.append(kw) or "ready",
    )
    background._run_in_thread("r1", OWNER, EVENT, None, False, None)
    assert calls == [
        {
            "run_id": "r1",
            "user_email": OWNER,
            "event_id": EVENT,
            "focus_on": None,
            "keep_edits": False,
            "product_to_emphasize": None,
        }
    ]


# ─── The Job entrypoint ───────────────────────────────────────────────────────


def test_job_entrypoint_parses_args_runs_the_worker_and_flushes_cost(monkeypatch):
    from backend.jobs import meeting_prep_job

    calls: list[dict[str, Any]] = []
    flushed: list[bool] = []
    monkeypatch.setattr(
        "rubik_core.meeting_prep.background.run_build_sync",
        lambda **kw: calls.append(kw) or "ready",
    )
    monkeypatch.setattr(
        "rubik_core.cost.usage.flush_usage", lambda *a, **k: flushed.append(True)
    )
    rc = meeting_prep_job.main(
        [
            "--run-id",
            "r1",
            "--user-email",
            OWNER,
            "--event-id",
            "ev1",
            "--focus-on=-pricing first",
            "--keep-edits",
            "false",
        ]
    )
    assert rc == 0
    assert calls == [
        {
            "run_id": "r1",
            "user_email": OWNER,
            "event_id": "ev1",
            "focus_on": "-pricing first",
            "keep_edits": False,
            "product_to_emphasize": None,
        }
    ]
    assert flushed == [True]


def test_job_entrypoint_defaults_keep_edits_true_and_exits_0_on_failure(monkeypatch):
    from backend.jobs import meeting_prep_job

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "rubik_core.meeting_prep.background.run_build_sync",
        lambda **kw: calls.append(kw) or "failed",
    )

    def broken_flush(*a, **k):
        raise RuntimeError("ledger down")

    monkeypatch.setattr("rubik_core.cost.usage.flush_usage", broken_flush)
    rc = meeting_prep_job.main(
        ["--run-id", "r1", "--user-email", OWNER, "--event-id", "ev1"]
    )
    assert rc == 0
    assert calls[0]["keep_edits"] is True
    assert calls[0]["focus_on"] is None


def test_job_entrypoint_requires_the_run(monkeypatch):
    from backend.jobs import meeting_prep_job

    with pytest.raises(SystemExit):
        meeting_prep_job.main(["--user-email", OWNER, "--event-id", "ev1"])


# ─── The Job carries what a brief reads ───────────────────────────────────────


def test_deep_prep_job_has_platform_only_secrets_and_workspace_routing(monkeypatch):
    from rubik_core.platform.deployment import _runtime_env, _runtime_secrets, load_jobs

    monkeypatch.setenv("CUBIE_AUTOMATIONS_ENABLED", "true")
    for name, value in {
        "CUBIE_PROVISION_DB_MAX_CONNECTIONS": "200",
        "CUBIE_PROVISION_SERVICE_MAX_INSTANCES": "1",
        "CUBIE_PROVISION_JOB_MAX_INSTANCES": "8",
        "CUBIE_PROVISION_RESERVED_CONNECTIONS": "30",
    }.items():
        monkeypatch.setenv(name, value)
    job = next(row for row in load_jobs() if row["name"] == "cubie-deep-prep")
    assert job["module"] == "backend.jobs.deep_prep_job"
    env = _runtime_env(
        "project", "us-central1", "sql", "a" * 40, "public-client", "bucket"
    )
    assert "TODOS_ENABLED=true" in env
    assert "PROMISE_ACCOUNTABILITY_ENABLED=true" in env
    # CRM tokens belong to each workspace's secret refs, never a Job process.
    assert "HUBSPOT_PRIVATE_APP_TOKEN" not in _runtime_secrets()
    assert "HUBSPOT_WRITE_TOKEN" not in _runtime_secrets()


# ─── Finalize in place (versions.append_version(existing_row=…)) ──────────────


def _pending(db) -> MeetingPrepRun:
    row = MeetingPrepRun(
        id="pending1",
        created_at=versions.utcnow_naive() - timedelta(minutes=2),
        user_email=OWNER,
        event_id=EVENT,
        mode="sync",
        change_kind="pending",
        delivery_status="running",
        context_bundle={"request": {"kind": "regenerate"}},
    )
    db.add(row)
    db.commit()
    return row


def _finalize(db, row, **overrides):
    kwargs = dict(
        user_email=OWNER,
        event_id=EVENT,
        title="T",
        prep_kind="sales_battle_brief",
        is_sales_user=True,
        is_internal=False,
        model_id="m",
        context_bundle={"title": "T", "request": {"kind": "regenerate"}},
        response={"title": "T", "executive_summary": "s"},
        partial_errors=[],
        change_kind="regenerated",
        existing_row=row,
        delivery_status="ready",
    )
    kwargs.update(overrides)
    return versions.append_version(db, **kwargs)


def test_finalizing_in_place_keeps_the_id_and_retries_a_taken_number(
    Session, monkeypatch
):
    with Session() as db:
        v1 = versions.append_version(
            db,
            user_email=OWNER,
            event_id=EVENT,
            title="T",
            prep_kind="sales_battle_brief",
            is_sales_user=True,
            is_internal=False,
            model_id="m",
            context_bundle={},
            response={"title": "T"},
            partial_errors=[],
            change_kind="generated",
        )
        pending = _pending(db)
        asked_at = pending.created_at
        real = versions.next_version
        calls = {"n": 0}

        def stale_then_real(db_, user_email, event_id):
            calls["n"] += 1
            return 1 if calls["n"] == 1 else real(db_, user_email, event_id)

        monkeypatch.setattr(
            "rubik_core.meeting_prep.versions.next_version", stale_then_real
        )
        row = _finalize(db, pending, base_run_id=v1.id)
        assert calls["n"] == 2
        assert row is pending and row.id == "pending1"
        assert row.version == 2
        assert row.delivery_status == "ready"
        assert row.change_kind == "regenerated"
        assert row.created_at > asked_at
        assert db.query(MeetingPrepRun).count() == 2


def test_finalizing_in_place_honours_must_build_on_latest(Session):
    with Session() as db:
        pending = _pending(db)
        # Built on "nothing", but v1 landed meanwhile.
        versions.append_version(
            db,
            user_email=OWNER,
            event_id=EVENT,
            title="T",
            prep_kind=None,
            is_sales_user=False,
            is_internal=False,
            model_id="m",
            context_bundle={},
            response={"title": "T"},
            partial_errors=[],
            change_kind="generated",
        )
        with pytest.raises(versions.StaleVersionError):
            _finalize(db, pending, must_build_on_latest=True)
        db.refresh(pending)
        assert pending.version is None and pending.change_kind == "pending"


def test_a_row_that_is_already_a_version_cannot_be_finalized_again(Session):
    with Session() as db:
        row = _finalize(db, _pending(db))
        with pytest.raises(ValueError):
            _finalize(db, row)


# ─── In-process builds run on the app's own loop ──────────────────────────────


class _LoopBoundClient:
    """Stands in for the process-wide genai client: its async session is bound
    to the first loop that awaits it, and awaiting it from any other loop is
    the error aiohttp raises ("attached to a different loop")."""

    def __init__(self):
        self.loops: list[int] = []

    async def call(self) -> None:
        loop_id = id(asyncio.get_running_loop())
        if self.loops and loop_id != self.loops[0]:
            raise RuntimeError("Task got Future attached to a different loop")
        self.loops.append(loop_id)


def test_an_in_process_build_shares_the_app_loop_with_the_genai_client(
    Session, pipeline, monkeypatch
):
    """The build used to run as asyncio.run on a pool thread: a second loop
    awaiting the client the app's loop already uses — every such build failed
    its model call. It now runs as a task on the app's loop."""
    client = _LoopBoundClient()
    fake_generate = pipeline.generate

    async def generate(ctx, user_email, personalization_block="", wgll_context=""):
        await client.call()
        return await fake_generate(ctx, user_email, personalization_block, wgll_context)

    pool = _FakePool()
    monkeypatch.setattr("backend.routers.home.generate_meeting_prep_analysis", generate)
    monkeypatch.setattr("rubik_core.meeting_prep.background.SessionLocal", Session)
    monkeypatch.setattr("rubik_core.meeting_prep.background._POOL", pool)
    monkeypatch.setattr(
        "rubik_core.meeting_prep.background.runner_mode", lambda: "inprocess"
    )

    async def app_loop() -> str:
        # The app's own model calls (Home triage, a section rewrite …).
        await client.call()
        loop = asyncio.get_running_loop()

        def start() -> str:  # the route's worker thread
            with Session() as db:
                row, started = background.request_build(
                    db, user_email=OWNER, event_id=EVENT, kind="first", loop=loop
                )
                assert started
                return row.id

        run_id = await asyncio.to_thread(start)
        for _ in range(1000):  # the build's task, on THIS loop
            if not any(t.get_name().endswith(run_id) for t in background._TASKS):
                break
            await asyncio.sleep(0.01)
        await client.call()  # and the app's calls still work afterwards
        return run_id

    run_id = asyncio.run(app_loop())
    assert len(client.loops) == 3 and len(set(client.loops)) == 1
    assert pool.submitted == []
    row = _row(Session, run_id)
    assert row.version == 1 and row.delivery_status == "ready"
    assert [n.kind for n in _notifications(Session)] == ["meeting_prep_ready"]
    assert background._TASKS == set()


def test_dispatch_on_the_loop_schedules_a_task_there(monkeypatch):
    pool = _FakePool()
    ran: list[tuple[str, int]] = []

    async def fake_run_build(**kw):
        ran.append((kw["run_id"], id(asyncio.get_running_loop())))
        return "ready"

    monkeypatch.setattr("rubik_core.meeting_prep.background._POOL", pool)
    monkeypatch.setattr("rubik_core.meeting_prep.background.run_build", fake_run_build)
    monkeypatch.setattr(
        "rubik_core.meeting_prep.background.runner_mode", lambda: "inprocess"
    )

    async def main() -> int:
        assert _dispatch_once() == "inprocess"
        await asyncio.gather(*background._TASKS)
        return id(asyncio.get_running_loop())

    loop_id = asyncio.run(main())
    assert ran == [("r1", loop_id)]
    assert pool.submitted == []


# ─── Starting a build: off the event loop, and capped per reader ──────────────


def test_starting_a_build_never_blocks_the_event_loop(Session, monkeypatch):
    """Recording the row and the Cloud Run Admin call (up to 30s) used to run
    on the event loop every other request shares."""
    import backend.routers.home as home

    seen: dict[str, Any] = {}

    def slow_dispatch(**kw):
        seen.update(thread=threading.get_ident(), loop=kw["loop"])
        time.sleep(0.3)  # the Admin API round trip
        return "job"

    monkeypatch.setattr("rubik_core.meeting_prep.background.SessionLocal", Session)
    monkeypatch.setattr("rubik_core.meeting_prep.background.dispatch", slow_dispatch)

    async def main():
        ticks = {"n": 0}

        async def ticker():
            while True:
                await asyncio.sleep(0.01)
                ticks["n"] += 1

        tick = asyncio.create_task(ticker())
        with Session() as db:
            resp = await home._start_background_prep(
                db,
                _profile(OWNER),
                event_id=EVENT,
                kind="first",
                focus_on=None,
                keep_edits=False,
            )
        tick.cancel()
        return resp, ticks["n"], threading.get_ident(), asyncio.get_running_loop()

    resp, ticks, loop_thread, loop = asyncio.run(main())
    assert resp.status_code == 202
    assert json.loads(resp.body)["status"] == "queued"
    assert seen["thread"] != loop_thread
    # The in-process fallback is scheduled on the APP's loop, not the thread's.
    assert seen["loop"] is loop
    assert ticks >= 10  # the loop kept serving while the launch blocked


def test_a_reader_with_too_many_builds_in_flight_is_refused(api, pipeline, Session):
    cap = background.max_inflight_per_user()
    assert cap == 5
    started = [_open(api, f"evt_{i}").json()["run_id"] for i in range(cap)]

    r = _open(api, "evt_one_too_many")
    assert r.status_code == 429
    assert r.json() == {
        "detail": "You have several briefings building already — wait for one "
        "to finish."
    }
    assert _rows(Session, event_id="evt_one_too_many") == []
    assert len(api.dispatched) == cap
    # Joining a build already running is never refused …
    again = _open(api, "evt_0")
    assert again.status_code == 202 and again.json()["run_id"] == started[0]
    # … nor is another reader limited by this one.
    api.as_user(OTHER)
    assert _open(api, "evt_one_too_many").status_code == 202

    # A slot frees when a build finishes, and a stale build never holds one.
    api.as_user(OWNER)
    _age(Session, started[1], background.ttl_minutes() + 1)
    assert _open(api, "evt_one_too_many").status_code == 202
    assert _open(api, "evt_and_another").status_code == 429


# ─── A late or duplicate worker never clobbers a newer outcome ────────────────


def _finish_elsewhere(Session, run_id: str) -> None:
    """Another attempt (a duplicate launch) finalizes the row as v1."""
    with Session() as db:
        row = db.get(MeetingPrepRun, run_id)
        versions.append_version(
            db,
            user_email=OWNER,
            event_id=EVENT,
            title=TITLE,
            prep_kind="sales_battle_brief",
            is_sales_user=True,
            is_internal=False,
            model_id="m",
            context_bundle={"title": TITLE},
            response={"title": TITLE, "executive_summary": "The Job's brief."},
            partial_errors=[],
            change_kind="generated",
            existing_row=row,
            delivery_status="ready",
        )


def test_a_duplicate_worker_that_fails_never_overwrites_the_version(
    api, pipeline, Session
):
    """The Job finished v1 while a duplicate in-process attempt (launched
    after a timed-out Admin call) was still building; its failure used to be
    written over v1 — v1 left the chain and its "ready" notification led
    nowhere."""
    run_id = _open(api).json()["run_id"]
    pipeline.fail_generation = True
    pipeline.on_collect = lambda kwargs: _finish_elsewhere(Session, run_id)
    assert api.run_builds() == ["ready"]

    row = _row(Session, run_id)
    assert row.version == 1
    assert row.change_kind == "generated"
    assert row.delivery_status == "ready"
    assert row.response["executive_summary"] == "The Job's brief."
    assert _notifications(Session) == []  # the Job's own notify is not faked here


def test_a_failure_is_never_recorded_over_a_version(Session):
    with Session() as db:
        row = _finalize(db, _pending(db))
        with pytest.raises(ValueError):
            versions.record_failed_run(
                db,
                user_email=OWNER,
                event_id=EVENT,
                title="T",
                prep_kind=None,
                is_sales_user=False,
                is_internal=False,
                model_id="m",
                context_bundle={},
                response={"title": "T"},
                partial_errors=["x"],
                existing_row=row,
                delivery_status="failed",
            )
        db.refresh(row)
        assert row.version == 1 and row.change_kind == "regenerated"


def _ttl_fail(api, Session, run_id: str) -> None:
    _age(Session, run_id, background.ttl_minutes() + 1)
    assert _status(api, run_id).json()["status"] == "failed"


def test_a_late_build_does_not_land_over_a_newer_version(api, pipeline, Session):
    """Reported failed by the TTL, the reader regenerated and got v2; the old
    build finishing afterwards used to land as v3 on top — the latest became
    the brief the reader had already replaced."""
    _first(api)
    late = _regenerate(api).json()["run_id"]
    late_kwargs = dict(api.dispatched[-1])
    _ttl_fail(api, Session, late)
    newer = _regenerate(api).json()["run_id"]
    assert newer != late
    assert background.run_build_sync(**api.dispatched[-1]) == "ready"
    assert _versions(Session) == [1, 2]

    generated = pipeline.generate_calls
    assert background.run_build_sync(**late_kwargs) == "failed"
    assert pipeline.generate_calls == generated  # not even built
    row = _row(Session, late)
    assert row.version is None and row.change_kind == "failed"
    assert _versions(Session) == [1, 2]
    assert _open(api).json()["run_id"] == newer
    assert [n.kind for n in _notifications(Session)] == [
        "meeting_prep_ready",
        "meeting_prep_failed",
        "meeting_prep_ready",
    ]


def test_a_late_build_does_not_race_a_newer_build_in_flight(api, pipeline, Session):
    late = _open(api).json()["run_id"]
    late_kwargs = dict(api.dispatched[-1])
    _ttl_fail(api, Session, late)
    newer = _open(api).json()["run_id"]
    assert newer != late

    assert background.run_build_sync(**late_kwargs) == "failed"
    assert _row(Session, late).version is None
    assert background.run_build_sync(**api.dispatched[-1]) == "ready"
    assert _row(Session, newer).version == 1
    kinds = [n.kind for n in _notifications(Session)]
    assert kinds == ["meeting_prep_failed", "meeting_prep_ready"]


def test_a_build_failed_by_the_clock_while_it_built_stays_failed_if_superseded(
    api, pipeline, Session
):
    """The worker passed its entry checks; the TTL failed it mid-build and the
    reader got a newer version meanwhile: the late brief is not finalized."""
    run_id = _open(api).json()["run_id"]

    def meanwhile(kwargs):
        with Session() as db:
            row = db.get(MeetingPrepRun, run_id)
            assert background.mark_failed(
                db, row, detail=background.TTL_DETAIL, failed_by="ttl"
            )
            db.add(
                MeetingPrepRun(
                    id="newer",
                    user_email=OWNER,
                    event_id=EVENT,
                    mode="sync",
                    version=1,
                    change_kind="generated",
                    delivery_status="ready",
                    response={"title": TITLE},
                    created_at=versions.utcnow_naive() + timedelta(seconds=1),
                )
            )
            db.commit()

    pipeline.on_collect = meanwhile
    assert api.run_builds() == ["failed"]
    row = _row(Session, run_id)
    assert row.version is None and row.change_kind == "failed"
    assert _versions(Session) == [1]
    assert [n.kind for n in _notifications(Session)] == ["meeting_prep_failed"]


def test_a_cloud_run_retry_never_finishes_a_build_the_clock_failed(
    api, pipeline, Session, monkeypatch
):
    run_id = _open(api).json()["run_id"]
    _ttl_fail(api, Session, run_id)
    monkeypatch.setenv("CLOUD_RUN_TASK_ATTEMPT", "1")
    assert api.run_builds() == ["failed"]
    assert pipeline.generate_calls == 0
    assert _row(Session, run_id).version is None
    assert [n.kind for n in _notifications(Session)] == ["meeting_prep_failed"]


def test_a_cloud_run_retry_still_builds_a_crashed_attempts_row(
    api, pipeline, Session, monkeypatch
):
    run_id = _open(api).json()["run_id"]
    with Session() as db:
        db.get(MeetingPrepRun, run_id).delivery_status = "running"
        db.commit()
    monkeypatch.setenv("CLOUD_RUN_TASK_ATTEMPT", "1")
    assert api.run_builds() == ["ready"]
    assert _row(Session, run_id).version == 1


# ─── The Job exits non-zero only when it could not settle the row ─────────────


@pytest.mark.parametrize(
    ("outcome", "exit_code"),
    [
        ("ready", 0),
        ("failed", 0),
        ("skipped", 0),
        ("unrecorded", 1),
        ("missing", 1),
    ],
)
def test_job_exit_code_follows_whether_the_row_was_settled(
    monkeypatch, outcome, exit_code
):
    from backend.jobs import meeting_prep_job

    monkeypatch.setattr(
        "rubik_core.meeting_prep.background.run_build_sync", lambda **kw: outcome
    )
    monkeypatch.setattr("rubik_core.cost.usage.flush_usage", lambda *a, **k: None)
    rc = meeting_prep_job.main(
        ["--run-id=r1", f"--user-email={OWNER}", "--event-id=-ev1"]
    )
    assert rc == exit_code


def test_a_build_whose_database_is_down_is_unrecorded(monkeypatch):
    """Cloud SQL unreachable at task start: the row could not be read or
    failed, so the build is NOT recorded — it used to say "failed" and exit 0,
    and Cloud Run never retried."""
    from sqlalchemy.exc import OperationalError

    class DownSession:
        def get(self, *a, **k):
            raise OperationalError("SELECT", {}, Exception("connection refused"))

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr("rubik_core.meeting_prep.background.SessionLocal", DownSession)
    assert (
        background.run_build_sync(run_id="r1", user_email=OWNER, event_id=EVENT)
        == "unrecorded"
    )


def test_a_build_that_could_record_its_failure_is_failed_not_unrecorded(
    api, pipeline, Session
):
    pipeline.generate_error = RuntimeError("boom")
    _open(api)
    assert api.run_builds() == ["failed"]


# ─── Platform deployment has a Deep Prep Job ───────────────────────────────


def test_platform_manifest_provisions_deep_prep_job():
    from rubik_core.platform.deployment import load_jobs

    jobs = {job["name"]: job for job in load_jobs()}
    assert jobs["cubie-deep-prep"]["module"] == "backend.jobs.deep_prep_job"

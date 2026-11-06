"""Tests for the L10 shared live-session state (Option A).

The L10 is a *shared* live session: the current agenda step + a timer anchor
live server-side on ``eos_l10_sessions`` so every participant's browser renders
the same step/countdown, read off the existing 2s poll. These cover:

1. ``POST /sessions/start`` initializes the cursor (step 0, anchored to start).
2. ``GET /sessions/{id}/state`` returns the cursor + a server clock + manager_id.
3. ``POST /sessions/{id}/state`` advances the step, re-anchors, bumps the version
   — manager (or admin / session creator) only; a plain participant gets 403.
4. Advancing a concluded session is refused (409).
5. The rejoin path (a second ``/sessions/start``) preserves the live step and
   anchor — the fix for "my timer restarts when I come back".
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from rubik_core.auth.registry import UserRegistry
from rubik_core.config import settings
from rubik_core.database.schemas import UserProfile

client = TestClient(app)


def _profile(email: str, roles: list[str] | None = None) -> UserProfile:
    now = "2026-06-24T00:00:00"
    stored = UserRegistry().get_user(email)
    return UserProfile(
        id=stored.id if stored else email.split("@")[0],
        email=email,
        name=stored.name if stored else email.split("@")[0].title(),
        roles=roles or (stored.roles if stored else ["user"]),
        department=stored.department if stored else "general",
        created_at=now,
        updated_at=now,
        is_exec=bool(stored.is_exec) if stored else False,
    )


def _as_user(email: str, roles: list[str] | None = None):
    app.dependency_overrides[get_current_user] = lambda: _profile(email, roles)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db_path = tmp_path / "test_eos_l10_live_state.db"
    settings.DATABASE_URL = f"sqlite:///{db_path}"
    import rubik_core.database.db as dbmod

    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database import eos  # noqa: F401  — register EOS tables
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    yield
    app.dependency_overrides.clear()


MANAGER = "manager@example.com"
PARTICIPANT = "participant@example.com"


def _seed_meeting_with_manager() -> tuple[str, str]:
    """Create an L10 meeting whose manager is a Person with email MANAGER.
    Returns (meeting_id, manager_person_id)."""
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import L10Meeting, Person

    db = SessionLocal()
    try:
        manager = Person(
            first_name="Mandy",
            last_name="Manager",
            email=MANAGER,
            created_by=MANAGER,
        )
        db.add(manager)
        db.commit()
        db.refresh(manager)

        meeting = L10Meeting(
            name="Weekly L10",
            duration_minutes=90,
            manager_id=manager.id,
            created_by=MANAGER,
        )
        db.add(meeting)
        db.commit()
        db.refresh(meeting)
        return str(meeting.id), str(manager.id)
    finally:
        db.close()


def _start(meeting_id: str) -> dict:
    r = client.post(
        "/api/eos/l10/sessions/start",
        json={"meeting_id": meeting_id, "actual_start": datetime.utcnow().isoformat()},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _meeting_in_list(meeting_id: str) -> dict:
    r = client.get("/api/eos/l10/")
    assert r.status_code == 200, r.text
    return next(m for m in r.json() if m["id"] == meeting_id)


def test_start_initializes_cursor_and_state_endpoint():
    meeting_id, manager_pid = _seed_meeting_with_manager()
    _as_user(MANAGER)

    started = _start(meeting_id)
    assert started["current_step"] == 0
    assert started["step_started_at"] is not None
    assert started["is_manager"] is True
    session_id = started["id"]

    st = client.get(f"/api/eos/l10/sessions/{session_id}/state").json()
    assert st["session_id"] == session_id
    assert st["current_step"] == 0
    assert st["state_version"] == 0
    assert st["manager_id"] == manager_pid
    assert st["server_now"] is not None
    assert st["actual_end"] is None


def _conclude_for_recap(session_id: str) -> None:
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import L10Session

    with SessionLocal() as db:
        session = db.query(L10Session).filter(L10Session.id == UUID(session_id)).one()
        session.actual_end = datetime.utcnow()
        db.commit()


def test_recap_uses_selected_provider_and_caches_only_success(monkeypatch):
    from backend.routers.eos import l10
    from rubik_core.ai_providers.registry import ModelRoute
    from rubik_core.ai_providers.runtime import ModelTextResponse

    meeting_id, _ = _seed_meeting_with_manager()
    _as_user(MANAGER)
    session_id = _start(meeting_id)["id"]
    _conclude_for_recap(session_id)
    monkeypatch.setattr(settings, "CUBIE_L10_AI_ENABLED", True)
    monkeypatch.setattr(
        "backend.services.l10_session_digest.build_session_digest",
        lambda _db, _session: SimpleNamespace(
            has_material=True, data_block="Decision: launch on Monday"
        ),
    )
    route = ModelRoute("test", "l10_recap", "anthropic", "claude-sonnet-4-6", "client")
    resolutions = []
    model_calls = []

    def resolve(feature, capability, *, default_model):
        resolutions.append((feature, capability, default_model))
        return route

    def generate(pinned, prompt):
        model_calls.append((pinned, prompt))
        return ModelTextResponse(
            "The team decided to launch on Monday.",
            "anthropic",
            "claude-sonnet-4-6",
            "client",
        )

    monkeypatch.setattr(l10, "resolve_model_route", resolve)
    monkeypatch.setattr(l10, "generate_text_on_route", generate)
    monkeypatch.setattr(
        "rubik_core.models.get_genai_client",
        lambda: pytest.fail("selected provider fell back to managed Vertex"),
    )

    first = client.get(f"/api/eos/l10/sessions/{session_id}/recap")
    second = client.get(f"/api/eos/l10/sessions/{session_id}/recap")

    assert first.status_code == second.status_code == 200
    assert (
        first.json()
        == second.json()
        == {"recap": "The team decided to launch on Monday."}
    )
    assert resolutions == [("l10_recap", "text", settings.VERTEX_LLM_MODEL_PRO)]
    assert len(model_calls) == 1
    assert model_calls[0][0] is route
    assert "Decision: launch on Monday" in model_calls[0][1]


def test_failed_selected_recap_does_not_cache_or_leak_provider_error(monkeypatch):
    from backend.routers.eos import l10
    from rubik_core.ai_providers.registry import ModelRoute
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import L10Session

    meeting_id, _ = _seed_meeting_with_manager()
    _as_user(MANAGER)
    session_id = _start(meeting_id)["id"]
    _conclude_for_recap(session_id)
    monkeypatch.setattr(settings, "CUBIE_L10_AI_ENABLED", True)
    monkeypatch.setattr(
        "backend.services.l10_session_digest.build_session_digest",
        lambda _db, _session: SimpleNamespace(
            has_material=True, data_block="Confidential meeting note"
        ),
    )
    route = ModelRoute("test", "l10_recap", "meta", "muse-spark-1.3", "client")
    monkeypatch.setattr(l10, "resolve_model_route", lambda *a, **k: route)

    def fail_selected(*_a, **_k):
        raise RuntimeError("provider echoed Confidential meeting note and secret-key")

    monkeypatch.setattr(l10, "generate_text_on_route", fail_selected)
    monkeypatch.setattr(
        "rubik_core.models.get_genai_client",
        lambda: pytest.fail("failed selected provider fell back to managed Vertex"),
    )

    response = client.get(f"/api/eos/l10/sessions/{session_id}/recap")

    assert response.status_code == 502
    assert "Confidential meeting note" not in response.text
    assert "secret-key" not in response.text
    with SessionLocal() as db:
        session = db.query(L10Session).filter(L10Session.id == UUID(session_id)).one()
        assert session.recap is None


def test_empty_l10_digest_skips_provider_resolution(monkeypatch):
    from backend.routers.eos import l10

    monkeypatch.setattr(
        "backend.services.l10_session_digest.build_session_digest",
        lambda _db, _session: SimpleNamespace(has_material=False),
    )
    monkeypatch.setattr(
        l10,
        "resolve_model_route",
        lambda *a, **k: pytest.fail("empty recap selected a model"),
    )

    text = asyncio.run(
        l10._generate_l10_recap(
            None, SimpleNamespace(actual_end=datetime(2026, 10, 30, 12, 0))
        )
    )

    assert "No closing notes" in text


def test_manager_advance_moves_step_and_bumps_version():
    meeting_id, _ = _seed_meeting_with_manager()
    _as_user(MANAGER)
    session_id = _start(meeting_id)["id"]

    r = client.post(
        f"/api/eos/l10/sessions/{session_id}/state", json={"current_step": 2}
    )
    assert r.status_code == 200, r.text
    assert r.json()["current_step"] == 2

    st = client.get(f"/api/eos/l10/sessions/{session_id}/state").json()
    assert st["current_step"] == 2
    assert st["state_version"] == 1

    # A second advance bumps the version again.
    client.post(f"/api/eos/l10/sessions/{session_id}/state", json={"current_step": 5})
    st2 = client.get(f"/api/eos/l10/sessions/{session_id}/state").json()
    assert st2["current_step"] == 5
    assert st2["state_version"] == 2


def test_non_manager_cannot_advance():
    meeting_id, _ = _seed_meeting_with_manager()
    _as_user(MANAGER)
    session_id = _start(meeting_id)["id"]

    # A plain participant (not admin, not the manager, not the creator).
    _as_user(PARTICIPANT)
    r = client.post(
        f"/api/eos/l10/sessions/{session_id}/state", json={"current_step": 3}
    )
    assert r.status_code == 403, r.text

    # ...but they can still READ the shared state to follow along.
    st = client.get(f"/api/eos/l10/sessions/{session_id}/state")
    assert st.status_code == 200
    assert st.json()["current_step"] == 0


def test_admin_can_advance_even_without_a_person_row():
    meeting_id, _ = _seed_meeting_with_manager()
    _as_user(MANAGER)
    session_id = _start(meeting_id)["id"]

    _as_user("admin@example.com", roles=["admin"])
    r = client.post(
        f"/api/eos/l10/sessions/{session_id}/state", json={"current_step": 1}
    )
    assert r.status_code == 200, r.text


def test_advance_rejected_after_session_end():
    meeting_id, _ = _seed_meeting_with_manager()
    _as_user(MANAGER)
    session_id = _start(meeting_id)["id"]

    end = client.post(f"/api/eos/l10/sessions/{session_id}/end")
    assert end.status_code == 200, end.text

    r = client.post(
        f"/api/eos/l10/sessions/{session_id}/state", json={"current_step": 1}
    )
    assert r.status_code == 409, r.text


def test_out_of_range_step_rejected():
    meeting_id, _ = _seed_meeting_with_manager()
    _as_user(MANAGER)
    session_id = _start(meeting_id)["id"]
    r = client.post(
        f"/api/eos/l10/sessions/{session_id}/state", json={"current_step": -1}
    )
    assert r.status_code == 422, r.text


def test_rejoin_preserves_step_and_timer_anchor():
    """The timer-restart fix: re-entering a live meeting must return the
    SAME step + anchor, not reset to step 0 with a fresh countdown."""
    meeting_id, _ = _seed_meeting_with_manager()
    _as_user(MANAGER)
    first = _start(meeting_id)
    session_id = first["id"]

    # Manager advances to the IDS step.
    client.post(f"/api/eos/l10/sessions/{session_id}/state", json={"current_step": 5})
    advanced = client.get(f"/api/eos/l10/sessions/{session_id}/state").json()
    anchor_after_advance = advanced["step_started_at"]

    # Re-enter (a refresh / rejoin hits /sessions/start again).
    rejoined = _start(meeting_id)
    assert rejoined["id"] == session_id  # same live session reused
    assert rejoined["current_step"] == 5  # NOT reset to 0
    assert rejoined["step_started_at"] == anchor_after_advance  # anchor untouched


def test_list_meetings_reports_live_session_for_rejoin():
    """The list endpoint annotates a meeting with live_session_id while a
    session is open, so the UI can offer Rejoin regardless of weekday."""
    meeting_id, _ = _seed_meeting_with_manager()
    _as_user(MANAGER)

    # No live session yet.
    assert _meeting_in_list(meeting_id)["live_session_id"] is None

    session_id = _start(meeting_id)["id"]
    assert _meeting_in_list(meeting_id)["live_session_id"] == session_id

    # Concluding the session clears the live marker.
    client.post(f"/api/eos/l10/sessions/{session_id}/end")
    assert _meeting_in_list(meeting_id)["live_session_id"] is None


def test_calendar_import_dedups_recurring_instances(monkeypatch):
    """Importing two different weekly *instances* of one recurring series
    (shared recurringEventId) upserts a single meeting, not one per week."""
    import rubik_core.tools.calendar as cal_mod

    def _event(instance_id: str, date: str) -> dict:
        return {
            "id": instance_id,
            "recurringEventId": "series_evt",  # shared across instances
            "summary": "Team L10",
            "start": {"dateTime": f"{date}T11:00:00-06:00"},
            "end": {"dateTime": f"{date}T12:30:00-06:00"},
            "attendees": [{"email": "tess@example.com"}],
            "organizer": {"email": "mgr@example.com"},
        }

    events = {
        "evt_20260701": _event("evt_20260701", "2026-07-01"),
        "evt_20260708": _event("evt_20260708", "2026-07-08"),
    }

    class _FakeCal:
        def __init__(self, user_email=None):
            self.service = object()  # truthy → "configured"

        def get_event(self, eid):
            return events.get(eid)

    monkeypatch.setattr(cal_mod, "GoogleCalendarClient", _FakeCal)

    _as_user("admin@example.com", roles=["admin"])

    r1 = client.post(
        "/api/eos/l10/calendar-import", json={"google_event_id": "evt_20260701"}
    )
    assert r1.status_code == 201, r1.text
    r2 = client.post(
        "/api/eos/l10/calendar-import", json={"google_event_id": "evt_20260708"}
    )
    assert r2.status_code == 201, r2.text

    # Same meeting, keyed on the SERIES id — not two rows.
    assert r1.json()["id"] == r2.json()["id"]
    assert r1.json()["google_event_id"] == "series_evt"
    listed = client.get("/api/eos/l10/").json()
    team_l10s = [m for m in listed if m["google_event_id"] == "series_evt"]
    assert len(team_l10s) == 1

    # The imported occurrence's calendar date is stamped on import and
    # refreshed on re-import (so the card shows which week it's for).
    assert r1.json()["last_imported_date"] == "2026-07-01"
    assert r2.json()["last_imported_date"] == "2026-07-08"
    assert team_l10s[0]["last_imported_date"] == "2026-07-08"


def _mock_calendar(monkeypatch, events: dict):
    """Patch GoogleCalendarClient so get_event serves from ``events``."""
    import rubik_core.tools.calendar as cal_mod

    class _FakeCal:
        def __init__(self, user_email=None):
            self.service = object()  # truthy → "configured"

        def get_event(self, eid):
            return events.get(eid)

    monkeypatch.setattr(cal_mod, "GoogleCalendarClient", _FakeCal)


def _instance_event(instance_id: str, date: str, series: str = "series_evt") -> dict:
    return {
        "id": instance_id,
        "recurringEventId": series,
        "summary": "Team L10",
        "start": {"dateTime": f"{date}T11:00:00-06:00"},
        "end": {"dateTime": f"{date}T12:30:00-06:00"},
        "attendees": [{"email": "tess@example.com"}],
        "organizer": {"email": "mgr@example.com"},
    }


def test_calendar_import_heals_instance_id_row(monkeypatch):
    """A meeting imported before series-dedup stored the *instance* id
    (``<series>_<timestamp>``). Importing a new occurrence must update that
    row (prefix match) — not mint a duplicate — and normalize its stored id
    to the series id."""
    _mock_calendar(monkeypatch, {"evt_jul8": _instance_event("evt_jul8", "2026-07-08")})
    _as_user("admin@example.com", roles=["admin"])

    # Legacy row: instance-style id from a pre-dedup import.
    r = client.post(
        "/api/eos/l10/",
        json={
            "name": "Team L10",
            "department_name": None,
            "day_of_week": 2,
            "start_time": "11:00",
            "google_event_id": "series_evt_20260624T170000Z",
        },
    )
    assert r.status_code == 201, r.text
    legacy_id = r.json()["id"]

    imp = client.post(
        "/api/eos/l10/calendar-import", json={"google_event_id": "evt_jul8"}
    )
    assert imp.status_code == 201, imp.text
    assert imp.json()["id"] == legacy_id  # updated in place, no duplicate
    assert imp.json()["google_event_id"] == "series_evt"  # id healed
    listed = client.get("/api/eos/l10/").json()
    assert sum(1 for m in listed if m["name"] == "Team L10") == 1


def test_calendar_import_adopts_manual_same_name_team_row(monkeypatch):
    """A manually-created meeting (no calendar link) with the same name for
    the same team is adopted by the import instead of duplicated."""
    _mock_calendar(monkeypatch, {"evt_jul8": _instance_event("evt_jul8", "2026-07-08")})
    _as_user("admin@example.com", roles=["admin"])

    r = client.post(
        "/api/eos/l10/",
        json={
            "name": "Team L10",
            # Matches the importer's department (profile default "general").
            "department_name": "general",
            "day_of_week": 2,
            "start_time": "11:00",
        },
    )
    assert r.status_code == 201, r.text
    manual_id = r.json()["id"]

    imp = client.post(
        "/api/eos/l10/calendar-import", json={"google_event_id": "evt_jul8"}
    )
    assert imp.status_code == 201, imp.text
    assert imp.json()["id"] == manual_id
    assert imp.json()["google_event_id"] == "series_evt"


def test_calendar_import_prefers_history_row_and_prunes_empty_dupes(monkeypatch):
    """When duplicates exist for one series, the import must pick the row
    holding session history as the survivor (even if another row matches the
    series id exactly) and soft-delete empty siblings."""
    _mock_calendar(monkeypatch, {"evt_jul8": _instance_event("evt_jul8", "2026-07-08")})
    _as_user("admin@example.com", roles=["admin"])

    # Row A: legacy instance-id row WITH a session (the user's real meeting).
    ra = client.post(
        "/api/eos/l10/",
        json={
            "name": "Team L10",
            "department_name": None,
            "day_of_week": 2,
            "start_time": "11:00",
            "google_event_id": "series_evt_20260624T170000Z",
        },
    )
    history_id = ra.json()["id"]
    _start(history_id)

    # Row B: empty duplicate that owns the exact series id (minted by the
    # old duplicate-prone upsert).
    rb = client.post(
        "/api/eos/l10/",
        json={
            "name": "Team L10",
            "department_name": None,
            "day_of_week": 2,
            "start_time": "11:00",
            "google_event_id": "series_evt",
        },
    )
    empty_id = rb.json()["id"]

    imp = client.post(
        "/api/eos/l10/calendar-import", json={"google_event_id": "evt_jul8"}
    )
    assert imp.status_code == 201, imp.text
    # Survivor = the row with history, NOT the exact-id empty duplicate.
    assert imp.json()["id"] == history_id
    assert imp.json()["google_event_id"] == "series_evt"
    listed = client.get("/api/eos/l10/").json()
    ids = {m["id"] for m in listed}
    assert history_id in ids
    assert empty_id not in ids  # empty duplicate soft-deleted


def test_calendar_sync_with_series_master_keeps_last_imported_date(monkeypatch):
    """Syncing via the stored series id fetches the series MASTER, whose
    start is the first-ever occurrence — it must refresh roster/schedule
    without rewinding last_imported_date."""
    _mock_calendar(
        monkeypatch,
        {
            "evt_jul8": _instance_event("evt_jul8", "2026-07-08"),
            "series_evt": {
                "id": "series_evt",
                "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=WE"],
                "summary": "Team L10",
                "start": {"dateTime": "2026-05-06T11:00:00-06:00"},  # first ever
                "end": {"dateTime": "2026-05-06T12:30:00-06:00"},
                "attendees": [{"email": "tess@example.com"}],
                "organizer": {"email": "mgr@example.com"},
            },
        },
    )
    _as_user("admin@example.com", roles=["admin"])

    first = client.post(
        "/api/eos/l10/calendar-import", json={"google_event_id": "evt_jul8"}
    )
    assert first.json()["last_imported_date"] == "2026-07-08"

    synced = client.post(
        "/api/eos/l10/calendar-import", json={"google_event_id": "series_evt"}
    )
    assert synced.status_code == 201, synced.text
    assert synced.json()["id"] == first.json()["id"]
    # Master's ancient start date must NOT rewind the stamp.
    assert synced.json()["last_imported_date"] == "2026-07-08"


def test_calendar_upcoming_exposes_recurring_event_id(monkeypatch):
    """The upcoming strip must surface ``recurring_event_id`` so the frontend
    can match a future occurrence back to the imported meeting (keyed on the
    series id) and offer a one-click re-import once the last one has passed."""
    import rubik_core.tools.calendar as cal_mod

    class _FakeCal:
        def __init__(self, user_email=None):
            self.service = object()  # truthy → "configured"

        def get_upcoming_events(self, max_results=30):
            return [
                {
                    "id": "evt_20260708",
                    "recurringEventId": "series_evt",
                    "summary": "Team L10",
                    "start": {"dateTime": "2026-07-08T11:00:00-06:00"},
                    "end": {"dateTime": "2026-07-08T12:30:00-06:00"},
                    "attendees": [{"email": "tess@example.com"}],
                    "organizer": {"email": "mgr@example.com"},
                },
                {
                    # A one-off (non-recurring) L10 has no recurringEventId.
                    "id": "evt_oneoff",
                    "summary": "Special L10",
                    "start": {"dateTime": "2026-07-09T09:00:00-06:00"},
                    "end": {"dateTime": "2026-07-09T10:00:00-06:00"},
                },
            ]

    monkeypatch.setattr(cal_mod, "GoogleCalendarClient", _FakeCal)
    _as_user("admin@example.com", roles=["admin"])

    resp = client.get("/api/eos/l10/calendar-upcoming")
    assert resp.status_code == 200, resp.text
    events = resp.json()["events"]
    by_id = {e["google_event_id"]: e for e in events}
    assert by_id["evt_20260708"]["recurring_event_id"] == "series_evt"
    # Non-recurring events carry a null series id (frontend falls back to
    # matching the instance id).
    assert by_id["evt_oneoff"]["recurring_event_id"] is None


# -- One live session per meeting -------------------------------------------
#
# The failure: the facilitator moves through the L10 while another
# attendee's screen stays on the Scorecard, and a refresh fixes it. No
# client-side gate explains that — followers adopt the server
# cursor on every 2s tick. The structural cause available to us is that
# ``/sessions/start`` read-then-inserts with no lock and is called by EVERY
# participant's browser on navigation, so two people opening the meeting in the
# same instant each get their own session row and each poll their own cursor.
# A refresh converged both because the lookup orders by actual_start DESC.


def _live_session_count(meeting_id: str) -> int:
    from uuid import UUID

    from rubik_core.database.db import SessionLocal
    from rubik_core.database.eos import L10Session

    db = SessionLocal()
    try:
        return (
            db.query(L10Session)
            .filter(
                L10Session.meeting_id == UUID(meeting_id),
                L10Session.actual_end.is_(None),
                L10Session.is_deleted == False,  # noqa: E712
            )
            .count()
        )
    finally:
        db.close()


def test_second_start_rejoins_instead_of_forking_the_room():
    """Two participants starting the same meeting share ONE session."""
    meeting_id, _ = _seed_meeting_with_manager()

    _as_user(MANAGER)
    first = _start(meeting_id)

    _as_user(PARTICIPANT)
    second = _start(meeting_id)

    assert second["id"] == first["id"], "participant joined a different session"
    assert _live_session_count(meeting_id) == 1


def test_start_recovers_when_the_insert_loses_a_race(monkeypatch):
    """The loser of the insert race rejoins the winner instead of erroring.

    Simulates the real sequence the zc52 partial unique index creates: our read
    finds no live session, a competing request commits one first, and our insert
    then violates the constraint. The handler must roll back, re-read, and hand
    back the session that won — otherwise the second participant lands on an
    error page mid-meeting, which is worse than the divergence we're fixing.
    """
    from uuid import UUID

    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm import Session as SASession

    meeting_id, _ = _seed_meeting_with_manager()
    _as_user(MANAGER)

    real_commit = SASession.commit
    competitor: dict[str, str] = {}

    def racing_commit(self):
        pending = [o for o in self.new if o.__class__.__name__ == "L10Session"]
        if not pending or competitor:
            return real_commit(self)

        # A different request wins the insert first...
        from rubik_core.database.db import SessionLocal
        from rubik_core.database.eos import L10Session

        other = SessionLocal()
        try:
            row = L10Session(
                meeting_id=UUID(meeting_id),
                actual_start=datetime.utcnow(),
                current_step=3,
                step_started_at=datetime.utcnow(),
                state_version=0,
                created_by=PARTICIPANT,
            )
            other.add(row)
            real_commit(other)
            other.refresh(row)
            competitor["id"] = str(row.id)
        finally:
            other.close()

        # ...so ours violates the unique index.
        raise IntegrityError("simulated", None, Exception("unique violation"))

    monkeypatch.setattr(SASession, "commit", racing_commit, raising=True)

    r = client.post(
        "/api/eos/l10/sessions/start",
        json={
            "meeting_id": meeting_id,
            "actual_start": datetime.utcnow().isoformat(),
        },
    )
    monkeypatch.undo()

    assert r.status_code == 200, r.text
    body = r.json()
    assert competitor, "the racing commit never fired — test no longer simulates it"
    assert body["id"] == competitor["id"], "did not rejoin the winning session"
    # And it rejoins AS-IS: the winner's cursor is preserved, not reset to 0.
    assert body["current_step"] == 3
    assert _live_session_count(meeting_id) == 1

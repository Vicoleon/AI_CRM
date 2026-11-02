"""GET /api/homefeed/priorities/runs/{run_id} — the DB-backed poll (#9).

The poll must read ONLY the priorities_runs table (any instance can serve any
poll), honor the fresh-card belt-and-braces, and lazily time out zombie runs.
run_state runs against a real throwaway SQLite; the bundle read
(_load_today_priorities) is patched directly since its card-query mechanics
are covered elsewhere.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.auth import get_current_user
from backend.main import app
from rubik_core.priorities import run_state

mock_user = MagicMock()
mock_user.email = "test@example.com"
mock_user.roles = ["user"]

client = TestClient(app)

_BUNDLE = {"headline": "h", "priorities": [], "generated_count": 0}


@pytest.fixture(autouse=True)
def _override_auth():
    app.dependency_overrides[get_current_user] = lambda: mock_user
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture(autouse=True)
def _run_state_db(tmp_path, monkeypatch):
    from rubik_core.config import settings as brain_settings

    monkeypatch.setattr(
        brain_settings, "DATABASE_URL", f"sqlite:///{tmp_path / 'poll.db'}"
    )
    import rubik_core.database.db as dbmod

    monkeypatch.setattr(dbmod, "_engine", None)
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)
    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    yield


def _get(run_id: str) -> dict:
    resp = client.get(f"/api/homefeed/priorities/runs/{run_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_unknown_run_degrades_to_card_check():
    with patch("backend.routers.home._load_today_priorities", return_value=None):
        body = _get("no-such-run")
    assert body["status"] == "pending"
    assert body["progress"]["elapsed_seconds"] == 0

    with patch("backend.routers.home._load_today_priorities", return_value=_BUNDLE):
        body = _get("no-such-run")
    assert body["status"] == "ready"
    assert body["bundle"]["headline"] == "h"


def test_pending_run_reports_progress():
    run = run_state.create_run(mock_user.email)
    with patch("backend.routers.home._load_today_priorities", return_value=None):
        body = _get(run["run_id"])
    assert body["status"] == "pending"
    assert body["progress"]["elapsed_seconds"] >= 0
    assert body["progress"]["estimated_seconds"] > 0


def test_pending_run_with_fresh_card_flips_ready():
    """Belt-and-braces: the Job wrote the card but its state write was lost —
    the card is authoritative, and the row is flipped so later polls
    short-circuit."""
    run = run_state.create_run(mock_user.email)
    with (
        patch("rubik_core.priorities.run_state.fresh_card_since", return_value=True),
        patch("backend.routers.home._load_today_priorities", return_value=_BUNDLE),
    ):
        body = _get(run["run_id"])
    assert body["status"] == "ready"
    assert body["bundle"]["headline"] == "h"
    assert run_state.get_run(run["run_id"])["status"] == run_state.RUN_READY


def test_failed_run_reports_error():
    run = run_state.create_run(mock_user.email)
    run_state.mark_finished(run["run_id"], succeeded=False, error="channel outage")
    body = _get(run["run_id"])
    assert body["status"] == "failed"
    assert "channel outage" in body["error"]


def test_ready_run_without_card_reports_failed():
    run = run_state.create_run(mock_user.email)
    run_state.mark_finished(run["run_id"], succeeded=True)
    with patch("backend.routers.home._load_today_priorities", return_value=None):
        body = _get(run["run_id"])
    assert body["status"] == "failed"
    assert "no card" in body["error"]


def test_zombie_pending_run_times_out_lazily():
    run = run_state.create_run(mock_user.email)

    from rubik_core.database.db import SessionLocal
    from rubik_core.database.models import PrioritiesRun

    with SessionLocal() as db:
        db.query(PrioritiesRun).filter(PrioritiesRun.run_id == run["run_id"]).update(
            {
                "queued_at": datetime.utcnow()
                - timedelta(seconds=run_state.RUN_TIMEOUT_SECONDS + 30)
            }
        )
        db.commit()

    with patch("backend.routers.home._load_today_priorities", return_value=None):
        body = _get(run["run_id"])
    assert body["status"] == "failed"
    assert "timed out" in body["error"]
    assert run_state.get_run(run["run_id"])["status"] == run_state.RUN_FAILED


def test_foreign_users_run_is_forbidden():
    run = run_state.create_run("someone-else@acme.example")
    resp = client.get(f"/api/homefeed/priorities/runs/{run['run_id']}")
    assert resp.status_code == 403

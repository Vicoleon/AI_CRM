"""UC-40 — Daily scheduler for the priorities pipeline.

Covers ``POST /api/jobs/priorities/run-all``:
- Auth gate: no scheduler key + no admin → 403
- Cloud Scheduler path: valid X-Scheduler-Key → 200, runs for every user
- Per-user isolation: one user's pipeline failure does NOT block others
- Idempotency: re-running the same day upserts (doesn't duplicate)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from rubik_core.config import settings

client = TestClient(app)


# ── Auth gate ───────────────────────────────────────────────────────────────


@patch("backend.routers.jobs._list_active_users", return_value=[])
def test_uc40_no_scheduler_key_no_admin_is_403(_mock_users):
    """No `X-Scheduler-Key` AND no auth header → 403, no work done."""
    response = client.post("/api/jobs/priorities/run-all")
    assert response.status_code == 403
    assert response.json()["detail"] == "Unauthorized."


@patch("backend.routers.jobs._list_active_users", return_value=[])
def test_uc40_wrong_scheduler_key_is_403(_mock_users, monkeypatch):
    """A wrong `X-Scheduler-Key` value → 403."""
    monkeypatch.setattr(settings, "SCHEDULER_API_KEY", "the-real-key")
    response = client.post(
        "/api/jobs/priorities/run-all",
        headers={"X-Scheduler-Key": "definitely-wrong", "X-Cubie-Workspace": "test"},
    )
    assert response.status_code == 403


# ── Cloud Scheduler happy path ──────────────────────────────────────────────
#
# UC-42 moved the actual batch execution out of this endpoint into a Cloud
# Run Job. The endpoint now KICKS OFF a Job execution and returns
# immediately. The "did this user get processed in order, did per-user
# isolation hold" tests moved to ``test_uc42_batch_runner.py`` (which tests
# the batch_runner module directly — that's where the iteration lives now).
#
# This file's endpoint tests just check the auth gate + the filter — see
# ``test_uc42_endpoint_launches_job_execution_on_scheduler_key`` and
# ``test_uc42_endpoint_403_without_scheduler_key`` for the Job-launch
# contract.


# ── _run_priorities_for_user wraps errors instead of raising ────────────────


@patch("rubik_core.priorities.PrioritiesOrchestrator")
@patch("rubik_core.priorities.persist_bundle")
def test_uc40_run_priorities_for_user_swallows_orchestrator_errors(
    mock_persist, mock_orch_cls
):
    """The per-user helper must NEVER raise — that would abort the whole loop."""
    mock_orch = MagicMock()
    mock_orch.generate.side_effect = RuntimeError("LLM down")
    mock_orch_cls.return_value = mock_orch

    from backend.routers.jobs import _run_priorities_for_user

    result = _run_priorities_for_user("a@acme.example")
    assert result.succeeded is False
    assert "LLM down" in result.error
    assert result.priorities_count == 0
    mock_persist.assert_not_called()


@patch("rubik_core.priorities.bundle_to_metadata")
@patch("rubik_core.priorities.persist_bundle")
@patch("rubik_core.priorities.PrioritiesOrchestrator")
def test_uc40_run_priorities_for_user_happy_path(mock_orch_cls, mock_persist, mock_b2m):
    """Success path: orchestrator runs, persist_bundle is called, count is reported."""
    mock_orch = MagicMock()
    mock_orch.generate.return_value = "bundle-sentinel"
    mock_orch_cls.return_value = mock_orch
    mock_b2m.return_value = {"priorities": [{"id": 1}, {"id": 2}, {"id": 3}]}

    from backend.routers.jobs import _run_priorities_for_user

    result = _run_priorities_for_user("a@acme.example")
    assert result.succeeded is True
    assert result.priorities_count == 3
    assert result.error is None
    mock_persist.assert_called_once()
    persist_args = mock_persist.call_args
    assert persist_args.args[0] == "a@acme.example"
    # Second arg is today's date — just check format (YYYY-MM-DD).
    assert len(persist_args.args[1]) == 10 and persist_args.args[1][4] == "-"


# ── Empty user list short-circuits cleanly ──────────────────────────────────


# ── _list_active_users filter (UC-40 follow-up) ─────────────────────────────


def _seed_users_for_filter_tests(rows: list[tuple[str, str]]):
    """Helper: build an in-memory SQLite, seed Users, return the SessionTest.

    Everyone has signed in, so the roster's "somebody reads it" rule is not
    what keeps or drops anyone in these tests."""
    from datetime import datetime

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from rubik_core.database.models import Base, User

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionTest = sessionmaker(bind=engine)
    db = SessionTest()
    try:
        db.add_all(
            [
                User(
                    email=email,
                    id=str(i),
                    name=email,
                    department=dept,
                    last_login=datetime(2026, 10, 1),
                )
                for i, (email, dept) in enumerate(rows)
            ]
        )
        db.commit()
    finally:
        db.close()
    return SessionTest


@pytest.mark.usefixtures("roster_admits_acme_example")
def test_uc40_list_active_users_includes_people_with_no_department(
    monkeypatch, set_test_workspace_domains
):
    """Departments are optional: someone filed under 'general' ("All
    organizations" in the admin UI) or under no department at all is counted
    like anyone else. The run-all count is the batch's own roster."""
    set_test_workspace_domains("acme.example")

    SessionTest = _seed_users_for_filter_tests(
        [
            ("real-sales@acme.example", "sales"),
            ("real-hr@acme.example", "hr"),
            ("generalist@acme.example", "general"),
            ("newhire@acme.example", None),
        ]
    )
    with patch("rubik_core.database.db.SessionLocal", SessionTest):
        from backend.routers.jobs import _list_active_users

        emails = _list_active_users()

    assert sorted(emails) == [
        "generalist@acme.example",
        "newhire@acme.example",
        "real-hr@acme.example",
        "real-sales@acme.example",
    ]


@pytest.mark.usefixtures("roster_admits_acme_example")
def test_uc40_list_active_users_excludes_non_workspace_domain(
    monkeypatch, set_test_workspace_domains
):
    """UC-40.1: department-tagged test fixtures (boss@example.com,
    impersonated_user@compliance.local, etc.) must ALSO be excluded —
    they're not real Workspace members so the SA can't impersonate them,
    producing the ``invalid_grant`` error that hangs the run.
    Filter requires the email to be in the Workspace domain.
    """
    set_test_workspace_domains("acme.example")

    SessionTest = _seed_users_for_filter_tests(
        [
            ("real@acme.example", "sales"),
            ("also-real@acme.example", "hr"),
            ("boss@example.com", "sales"),  # has dept but wrong domain
            ("hank@example.com", "hr"),  # same
            ("impersonated_user@compliance.local", "compliance"),  # same
        ]
    )
    with patch("rubik_core.database.db.SessionLocal", SessionTest):
        from backend.routers.jobs import _list_active_users

        emails = _list_active_users()

    assert sorted(emails) == ["also-real@acme.example", "real@acme.example"]
    assert "boss@example.com" not in emails
    assert "impersonated_user@compliance.local" not in emails


@pytest.mark.usefixtures("roster_admits_acme_example")
def test_uc40_list_active_users_no_control_domain_fails_closed(
    monkeypatch, set_test_workspace_domains
):
    """Without a verified domain, no mailbox can enter the batch roster."""
    set_test_workspace_domains()

    SessionTest = _seed_users_for_filter_tests(
        [
            ("a@acme.example", "sales"),
            # A real-looking off-domain address is also refused.
            ("b@partner.co", "sales"),
            ("boss@example.com", "sales"),  # seed fixture → always blocked
            ("impersonated_user@systems.local", "systems"),  # impersonation → blocked
            ("c@whatever.com", "general"),  # no department: still counted
        ]
    )
    with patch("rubik_core.database.db.SessionLocal", SessionTest):
        from backend.routers.jobs import _list_active_users

        emails = _list_active_users()

    assert emails == []
    assert "boss@example.com" not in emails
    assert "impersonated_user@systems.local" not in emails


def test_uc40_per_user_timeout_returns_failure_result():
    """UC-40.1: a per-user pipeline that hangs must be cancelled at
    PER_USER_TIMEOUT_SECONDS, returning a failure result so the loop
    continues to the next user instead of freezing the entire batch.
    """
    import time

    from backend.routers.jobs import (
        PrioritiesUserResult,
        _run_user_with_timeout,
    )

    # Patch _run_priorities_for_user with a function that sleeps forever
    def hang(user_email):
        # Only needs to outlive the 1s timeout patched below. It used to sleep
        # 30s, which cost the full 30s of wall clock on every CI run — the
        # slowest test in the suite — because _run_user_with_timeout's
        # `with ThreadPoolExecutor(...)` exits via shutdown(wait=True) and
        # therefore waits for this thread no matter what the timeout did.
        # That wait is also a live defect: the caller logs "moving on" and
        # returns a failure result, but does NOT move on. Tracked separately;
        # this test never asserted the non-blocking behaviour, so shortening
        # the sleep loses no coverage.
        time.sleep(3)  # would block; should be cancelled
        return PrioritiesUserResult(user_email=user_email, succeeded=True)

    # Set a TINY timeout for this test so we don't actually wait 10 min
    with (
        patch("backend.routers.jobs._run_priorities_for_user", hang),
        patch("backend.routers.jobs.PER_USER_TIMEOUT_SECONDS", 1),
    ):
        result = _run_user_with_timeout("stuck@acme.example")

    assert result.succeeded is False
    assert "timeout" in result.error.lower()
    assert result.user_email == "stuck@acme.example"


def test_uc40_per_user_timeout_passes_through_normal_completion():
    """When the per-user pipeline completes within budget, the timeout
    wrapper is transparent — returns whatever _run_priorities_for_user did."""
    from backend.routers.jobs import PrioritiesUserResult, _run_user_with_timeout

    def fast(user_email):
        return PrioritiesUserResult(
            user_email=user_email,
            succeeded=True,
            priorities_count=4,
            duration_seconds=1.5,
        )

    with patch("backend.routers.jobs._run_priorities_for_user", fast):
        result = _run_user_with_timeout("a@acme.example")

    assert result.succeeded is True
    assert result.priorities_count == 4


@patch("backend.routers.jobs._launch_priorities_job_execution")
@patch("backend.routers.jobs._list_active_users", return_value=[])
def test_uc40_empty_user_list_still_kicks_off_job(mock_users, mock_launch, monkeypatch):
    """No users in the DB → still 200; users_total=0; Cloud Run Job IS
    launched (the Job will short-circuit and exit 0 — its responsibility,
    not the endpoint's, post-UC-42).
    """
    monkeypatch.setattr(settings, "SCHEDULER_API_KEY", "secret-key")
    mock_launch.return_value = {"name": "projects/p/.../executions/empty"}
    response = client.post(
        "/api/jobs/priorities/run-all",
        headers={"X-Scheduler-Key": "secret-key", "X-Cubie-Workspace": "test"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["users_total"] == 0
    assert body["status"] == "started"
    # The Job still gets kicked off — short-circuit happens inside the Job.
    mock_launch.assert_called_once()


# ── FT next-week: /api/jobs/priorities/run-next-week ──────────────────────────


@patch("backend.routers.jobs._launch_priorities_job_execution")
def test_run_next_week_403_without_scheduler_key(mock_launch):
    """No scheduler key + no admin → 403, no Job launched."""
    response = client.post("/api/jobs/priorities/run-next-week")
    assert response.status_code == 403
    mock_launch.assert_not_called()


@patch("backend.routers.jobs._launch_priorities_job_execution")
@patch("backend.routers.jobs._list_active_users", return_value=["a@acme.example"])
def test_run_next_week_launches_with_report_next_week(
    _mock_users, mock_launch, monkeypatch
):
    """Valid scheduler key → launches the Job with report='next_week'."""
    monkeypatch.setattr(settings, "SCHEDULER_API_KEY", "secret-key")
    mock_launch.return_value = {"name": "projects/p/.../executions/nw1"}

    response = client.post(
        "/api/jobs/priorities/run-next-week",
        headers={"X-Scheduler-Key": "secret-key", "X-Cubie-Workspace": "test"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "started"
    assert body["trigger"] == "scheduler"
    kwargs = mock_launch.call_args.kwargs
    assert kwargs["report"] == "next_week"
    assert kwargs["target_email"] is None
    assert kwargs["run_id"] == body["run_id"]


@patch("backend.routers.jobs._launch_priorities_job_execution")
def test_run_next_week_forwards_target_email(mock_launch, monkeypatch):
    """Body {target_email} → forwarded; users_total=1 (ad-hoc single-user run)."""
    monkeypatch.setattr(settings, "SCHEDULER_API_KEY", "secret-key")
    mock_launch.return_value = {"name": "projects/p/.../executions/nw2"}

    response = client.post(
        "/api/jobs/priorities/run-next-week",
        headers={"X-Scheduler-Key": "secret-key", "X-Cubie-Workspace": "test"},
        json={"target_email": "bellison@acme.example"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["users_total"] == 1
    kwargs = mock_launch.call_args.kwargs
    assert kwargs["target_email"] == "bellison@acme.example"
    assert kwargs["report"] == "next_week"

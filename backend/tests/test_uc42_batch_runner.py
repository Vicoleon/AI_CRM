"""UC-42 — Cloud Run Job entrypoint + endpoint refactor.

Covers:

- ``rubik_core.priorities.batch.run_batch`` — per-user isolation, summary shape
- ``rubik_core.priorities.batch_runner.main`` — argparse, single-user mode,
  all-users mode, empty-list short-circuit, infra-error exit code
- ``POST /api/jobs/priorities/run-all`` — kicks off a Cloud Run Job
  execution rather than running in-process; happy path, auth gate,
  target_email forwarding
- ``backend.routers.jobs._build_priorities_job_run_body`` — the ``:run``
  request body always re-prefixes the job's module entrypoint, since
  Cloud Run v2 ``containerOverrides[].args`` REPLACE (not append to) the
  job's configured args
"""

from __future__ import annotations

import importlib
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from rubik_core.config import settings

client = TestClient(app)


# ── batch.run_batch — per-user isolation + summary ──────────────────────────


@patch("rubik_core.priorities.batch.run_user_with_timeout")
def test_uc42_batch_aggregates_per_user_results(mock_run_user):
    """Mix of successes and failures → summary counts are correct."""
    from rubik_core.priorities.batch import PrioritiesUserResult, run_batch

    def fake_run(email, timeout_seconds, report_kind="daily"):
        if email == "b@x.example":
            return PrioritiesUserResult(
                user_email=email,
                succeeded=False,
                error="boom",
            )
        return PrioritiesUserResult(
            user_email=email,
            succeeded=True,
            priorities_count=3,
            duration_seconds=2.5,
        )

    mock_run_user.side_effect = fake_run

    summary = run_batch(
        run_id="r-1",
        user_emails=["a@x.example", "b@x.example", "c@x.example"],
    )

    assert summary.run_id == "r-1"
    assert summary.users_total == 3
    assert summary.users_succeeded == 2
    assert summary.users_failed == 1
    assert {r.user_email for r in summary.successes} == {"a@x.example", "c@x.example"}
    assert {r.user_email for r in summary.failures} == {"b@x.example"}


@patch("rubik_core.priorities.batch.run_user_with_timeout")
def test_uc42_batch_continues_when_a_user_fails(mock_run_user):
    """If user B fails, users A and C still get processed (no early exit)."""
    from rubik_core.priorities.batch import PrioritiesUserResult, run_batch

    calls = []

    def fake_run(email, timeout_seconds, report_kind="daily"):
        calls.append(email)
        if email == "b@x.example":
            return PrioritiesUserResult(
                user_email=email,
                succeeded=False,
                error="boom",
            )
        return PrioritiesUserResult(user_email=email, succeeded=True)

    mock_run_user.side_effect = fake_run
    run_batch(run_id="r", user_emails=["a@x.example", "b@x.example", "c@x.example"])
    # A, B, C all processed despite B failing; the end-of-run retry pass
    # (the empty-briefing fix) then re-runs the failed B once.
    assert calls == ["a@x.example", "b@x.example", "c@x.example", "b@x.example"]


# ── batch_runner.main CLI ───────────────────────────────────────────────────


@patch("rubik_core.priorities.batch.run_batch")
@patch("rubik_core.priorities.batch.list_active_users")
def test_uc42_batch_runner_all_users_mode(mock_list_users, mock_run_batch):
    """No flags → list_active_users() drives the iteration."""
    from rubik_core.priorities.batch import BatchRunSummary
    from rubik_core.priorities.batch_runner import main

    mock_list_users.return_value = ["a@x.example", "b@x.example"]
    mock_run_batch.return_value = BatchRunSummary(
        run_id="r",
        users_total=2,
        users_succeeded=2,
        users_failed=0,
    )

    exit_code = main([])
    assert exit_code == 0
    mock_list_users.assert_called_once()
    mock_run_batch.assert_called_once()
    args, kwargs = mock_run_batch.call_args
    # signature: run_batch(run_id, user_emails, per_user_timeout=...)
    assert args[1] == ["a@x.example", "b@x.example"] or kwargs.get("user_emails") == [
        "a@x.example",
        "b@x.example",
    ]


@patch("rubik_core.priorities.batch.run_batch")
@patch("rubik_core.priorities.batch.list_active_users")
def test_uc42_batch_runner_target_email_mode(mock_list_users, mock_run_batch):
    """--target-email skips list_active_users entirely."""
    from rubik_core.priorities.batch import BatchRunSummary
    from rubik_core.priorities.batch_runner import main

    mock_run_batch.return_value = BatchRunSummary(
        run_id="r",
        users_total=1,
        users_succeeded=1,
        users_failed=0,
    )

    exit_code = main(["--target-email", "bellison@acme.example"])
    assert exit_code == 0
    mock_list_users.assert_not_called()
    args, kwargs = mock_run_batch.call_args
    assert args[1] == ["bellison@acme.example"] or kwargs.get("user_emails") == [
        "bellison@acme.example"
    ]


@patch("rubik_core.priorities.batch.list_active_users")
def test_uc42_batch_runner_empty_user_list_exits_zero(mock_list_users):
    """No users to process → exit 0 (NOT a retry signal to Cloud Run Jobs)."""
    from rubik_core.priorities.batch_runner import main

    mock_list_users.return_value = []
    assert main([]) == 0


@patch("rubik_core.priorities.batch.list_active_users")
def test_uc42_batch_runner_infra_failure_exits_nonzero(mock_list_users):
    """If list_active_users raises (DB down) → exit 1 so Cloud Run Jobs retries."""
    from rubik_core.priorities.batch_runner import main

    mock_list_users.side_effect = RuntimeError("DB unreachable")
    assert main([]) == 1


@patch("rubik_core.priorities.batch.run_batch")
@patch("rubik_core.priorities.batch.list_active_users")
def test_batch_runner_report_flag_plumbs_to_run_batch(mock_list_users, mock_run_batch):
    """--report next_week reaches run_batch; default stays daily."""
    from rubik_core.priorities.batch import BatchRunSummary
    from rubik_core.priorities.batch_runner import main

    mock_list_users.return_value = ["a@x.example"]
    mock_run_batch.return_value = BatchRunSummary(
        run_id="r", users_total=1, users_succeeded=1, users_failed=0
    )

    main(["--report", "next_week"])
    assert mock_run_batch.call_args.kwargs["report_kind"] == "next_week"

    mock_run_batch.reset_mock()
    main([])
    assert mock_run_batch.call_args.kwargs["report_kind"] == "daily"


# ── batch_runner.main sharding (UC-42.1) ────────────────────────────────────


@patch("rubik_core.priorities.batch.run_batch")
@patch("rubik_core.priorities.batch.list_active_users")
def test_uc42_sharding_partitions_users_disjointly_and_covers_all(
    mock_list_users, mock_run_batch, monkeypatch
):
    """CLOUD_RUN_TASK_COUNT>1 → each shard processes a disjoint stride and the
    union across all shards is EXACTLY list_active_users(). This is the
    load-bearing invariant: the batch population must remain a superset of the
    Slack-delivery gate, so no exec (or anyone) is silently dropped."""
    from rubik_core.priorities.batch import BatchRunSummary
    from rubik_core.priorities.batch_runner import main

    population = [f"u{i}@acme.example" for i in range(10)]
    mock_list_users.return_value = list(population)
    mock_run_batch.return_value = BatchRunSummary(
        run_id="r", users_total=0, users_succeeded=0, users_failed=0
    )

    shard_count = 4
    seen: list[str] = []
    for shard in range(shard_count):
        mock_run_batch.reset_mock()
        monkeypatch.setenv("CLOUD_RUN_TASK_COUNT", str(shard_count))
        monkeypatch.setenv("CLOUD_RUN_TASK_INDEX", str(shard))
        assert main([]) == 0
        call = mock_run_batch.call_args
        shard_users = call.kwargs.get("user_emails") or call.args[1]
        seen.extend(shard_users)

    # complete (everyone covered) AND disjoint (no user processed twice)
    assert sorted(seen) == sorted(population)
    assert len(seen) == len(set(seen))


@patch("rubik_core.priorities.batch.run_batch")
@patch("rubik_core.priorities.batch.list_active_users")
def test_uc42_sharding_target_email_only_runs_on_owning_shard(
    mock_list_users, mock_run_batch, monkeypatch
):
    """A --target-email run is a 1-element list: exactly one shard owns it, the
    others get an empty slice and exit 0 WITHOUT running the batch — so a
    targeted re-run on a sharded job can't write duplicate cards."""
    from rubik_core.priorities.batch import BatchRunSummary
    from rubik_core.priorities.batch_runner import main

    mock_run_batch.return_value = BatchRunSummary(
        run_id="r", users_total=1, users_succeeded=1, users_failed=0
    )
    monkeypatch.setenv("CLOUD_RUN_TASK_COUNT", "4")

    ran = 0
    for shard in range(4):
        mock_run_batch.reset_mock()
        monkeypatch.setenv("CLOUD_RUN_TASK_INDEX", str(shard))
        assert main(["--target-email", "bellison@acme.example"]) == 0
        ran += mock_run_batch.call_count

    # processed on exactly one shard, never via list_active_users
    assert ran == 1
    mock_list_users.assert_not_called()


# ── /api/jobs/priorities/run-all — kicks off the Job execution ──────────────


@patch("backend.routers.jobs._launch_priorities_job_execution")
def test_uc42_endpoint_launches_job_execution_on_scheduler_key(
    mock_launch, monkeypatch
):
    """Valid scheduler key → endpoint calls the Job launcher, returns 200."""
    monkeypatch.setattr(settings, "SCHEDULER_API_KEY", "scheduler-secret")
    monkeypatch.setattr(
        "backend.routers.jobs._list_active_users",
        lambda: ["a@acme.example", "b@acme.example"],
    )
    mock_launch.return_value = {
        "name": "projects/p/locations/us-central1/jobs/cubie-priorities-batch/executions/e1",
    }

    response = client.post(
        "/api/jobs/priorities/run-all",
        headers={"X-Scheduler-Key": "scheduler-secret", "X-Cubie-Workspace": "test"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["trigger"] == "scheduler"
    assert body["status"] == "started"
    assert body["users_total"] == 2
    assert "run_id" in body and body["run_id"]
    mock_launch.assert_called_once()
    # target_email should NOT be set when body is omitted
    kwargs = mock_launch.call_args.kwargs
    assert kwargs["target_email"] is None
    assert kwargs["run_id"] == body["run_id"]


@patch("backend.routers.jobs._launch_priorities_job_execution")
def test_uc42_endpoint_forwards_target_email(mock_launch, monkeypatch):
    """Body {target_email: "..."} → forwarded to the Job launcher; users_total=1."""
    monkeypatch.setattr(settings, "SCHEDULER_API_KEY", "scheduler-secret")
    mock_launch.return_value = {"name": "projects/p/.../executions/e2"}

    response = client.post(
        "/api/jobs/priorities/run-all",
        headers={"X-Scheduler-Key": "scheduler-secret", "X-Cubie-Workspace": "test"},
        json={"target_email": "bellison@acme.example"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["users_total"] == 1
    kwargs = mock_launch.call_args.kwargs
    assert kwargs["target_email"] == "bellison@acme.example"


@patch("backend.routers.jobs._launch_priorities_job_execution")
def test_uc42_endpoint_403_without_scheduler_key(mock_launch, monkeypatch):
    """No scheduler key + no admin auth → 403, no Job launched."""
    response = client.post("/api/jobs/priorities/run-all")
    assert response.status_code == 403
    mock_launch.assert_not_called()


@patch("backend.routers.jobs._launch_priorities_job_execution")
def test_uc42_endpoint_502_when_job_launch_fails(mock_launch, monkeypatch):
    """If the Cloud Run Admin API rejects the launch → 502 with a clean detail."""
    from fastapi import HTTPException as _HTTPException

    monkeypatch.setattr(settings, "SCHEDULER_API_KEY", "scheduler-secret")
    monkeypatch.setattr(
        "backend.routers.jobs._list_active_users", lambda: ["a@acme.example"]
    )
    mock_launch.side_effect = _HTTPException(status_code=502, detail="boom")

    response = client.post(
        "/api/jobs/priorities/run-all",
        headers={"X-Scheduler-Key": "scheduler-secret", "X-Cubie-Workspace": "test"},
    )
    assert response.status_code == 502
    assert "boom" in response.json()["detail"]


# ── _build_priorities_job_run_body — override args re-prefix the module ────
#
# Root cause of the daily 403: this body always carried
# ``containerOverrides``, which requires ``run.jobs.runWithOverrides``
# (``roles/run.developer``) rather than the ``roles/run.invoker`` the
# service's runtime SA actually holds. Fix is two-fold: (1) omit overrides
# entirely when there's nothing to override, so a plain ``run.invoker``
# trigger keeps working, and (2) when overrides ARE sent, always re-prefix
# the job's module entrypoint, since v2 ``args`` replace rather than append.


def test_uc42_run_body_args_start_with_batch_runner_module():
    """run_id only → args are module-prefixed, run-id flag follows."""
    from backend.routers.jobs import (
        PRIORITIES_JOB_MODULE,
        _build_priorities_job_run_body,
    )

    body = _build_priorities_job_run_body(target_email=None, run_id="r-1")

    assert body == {
        "overrides": {
            "containerOverrides": [
                {"args": ["-m", PRIORITIES_JOB_MODULE, "--run-id", "r-1"]}
            ],
        }
    }


def test_uc42_run_body_includes_target_email_after_module():
    """run_id + target_email → full ordered args, module prefix first."""
    from backend.routers.jobs import (
        PRIORITIES_JOB_MODULE,
        _build_priorities_job_run_body,
    )

    body = _build_priorities_job_run_body(
        target_email="bellison@acme.example", run_id="r-2"
    )

    args = body["overrides"]["containerOverrides"][0]["args"]
    assert args == [
        "-m",
        PRIORITIES_JOB_MODULE,
        "--run-id",
        "r-2",
        "--target-email",
        "bellison@acme.example",
    ]


def test_uc42_run_body_empty_without_flags():
    """No run_id/target_email → {} (no overrides → run.jobs.run suffices)."""
    from backend.routers.jobs import _build_priorities_job_run_body

    assert _build_priorities_job_run_body(target_email=None, run_id=None) == {}


def test_uc42_run_body_report_flag_comes_right_after_module():
    """FT next-week: report='next_week' is inserted first among the override
    flags (module prefix, then --report, then --run-id/--target-email)."""
    from backend.routers.jobs import (
        PRIORITIES_JOB_MODULE,
        _build_priorities_job_run_body,
    )

    body = _build_priorities_job_run_body(
        target_email=None, run_id="r-1", report="next_week"
    )
    args = body["overrides"]["containerOverrides"][0]["args"]
    assert args == [
        "-m",
        PRIORITIES_JOB_MODULE,
        "--report",
        "next_week",
        "--run-id",
        "r-1",
    ]

    # report="daily" (the default report) behaves like no report at all.
    daily = _build_priorities_job_run_body(
        target_email=None, run_id="r-1", report="daily"
    )
    assert daily == _build_priorities_job_run_body(target_email=None, run_id="r-1")


def test_uc42_job_module_constant_is_importable():
    """PRIORITIES_JOB_MODULE must always name a real, importable module —
    a rename here without updating the constant would silently reintroduce
    the crash-on-override bug."""
    from backend.routers.jobs import PRIORITIES_JOB_MODULE

    importlib.import_module(PRIORITIES_JOB_MODULE)


def test_uc42_launch_posts_module_prefixed_args(monkeypatch):
    """End-to-end wiring: _launch_priorities_job_execution actually POSTs
    the module-prefixed body to the v2 :run endpoint."""
    from backend.routers.jobs import (
        PRIORITIES_JOB_MODULE,
        _launch_priorities_job_execution,
    )

    mock_creds = MagicMock(token="fake-token")
    mock_response = MagicMock(status_code=200)
    mock_response.json.return_value = {
        "name": "projects/p/locations/us-central1/jobs/cubie-priorities-batch/executions/e1"
    }
    monkeypatch.setattr(settings, "GCP_PROJECT_ID", "test-project")

    with (
        patch("google.auth.compute_engine.Credentials", return_value=mock_creds),
        patch("google.auth.transport.requests.Request"),
        patch("requests.post", return_value=mock_response) as mock_post,
    ):
        _launch_priorities_job_execution(run_id="r-3")

    assert mock_post.call_args.args[0].endswith("/jobs/cubie-priorities-batch:run")
    posted_body = mock_post.call_args.kwargs["json"]
    assert posted_body["overrides"]["containerOverrides"][0]["args"] == [
        "-m",
        PRIORITIES_JOB_MODULE,
        "--run-id",
        "r-3",
    ]


def test_uc42_launch_next_week_posts_report_flag(monkeypatch):
    """FT next-week: report='next_week' reaches the POST body end-to-end."""
    from backend.routers.jobs import _launch_priorities_job_execution

    mock_creds = MagicMock(token="fake-token")
    mock_response = MagicMock(status_code=200)
    mock_response.json.return_value = {"name": "projects/p/.../executions/e2"}
    monkeypatch.setattr(settings, "GCP_PROJECT_ID", "test-project")

    with (
        patch("google.auth.compute_engine.Credentials", return_value=mock_creds),
        patch("google.auth.transport.requests.Request"),
        patch("requests.post", return_value=mock_response) as mock_post,
    ):
        _launch_priorities_job_execution(run_id="r-4", report="next_week")

    args = mock_post.call_args.kwargs["json"]["overrides"]["containerOverrides"][0][
        "args"
    ]
    assert args[:2] == ["-m", "rubik_core.priorities.batch_runner"]
    assert "--report" in args and "next_week" in args


def test_uc42_launch_uses_runtime_sa_not_adc(monkeypatch):
    """The launcher must mint its token from the metadata server (the
    service's runtime SA), NEVER google.auth.default(): if anything in the
    process exports GOOGLE_APPLICATION_CREDENTIALS pointing at the Workspace
    DWD key (the workspace-sync SA), in-process ADC resolves to the WRONG
    service account, and the daily run fails with 403 even after the correct
    job-level IAM grant."""
    from backend.routers.jobs import _launch_priorities_job_execution

    mock_creds = MagicMock(token="runtime-sa-token")
    mock_response = MagicMock(status_code=200)
    mock_response.json.return_value = {"name": "projects/p/.../executions/e9"}
    monkeypatch.setattr(settings, "GCP_PROJECT_ID", "test-project")

    with (
        patch(
            "google.auth.compute_engine.Credentials", return_value=mock_creds
        ) as mock_metadata_creds,
        patch("google.auth.default") as mock_adc,
        patch("google.auth.transport.requests.Request"),
        patch("requests.post", return_value=mock_response) as mock_post,
    ):
        _launch_priorities_job_execution(run_id="r-9")

    mock_metadata_creds.assert_called_once()
    mock_adc.assert_not_called()
    assert (
        mock_post.call_args.kwargs["headers"]["Authorization"]
        == "Bearer runtime-sa-token"
    )


# ── batch.list_active_users — generation population must cover delivery ───────


@pytest.mark.usefixtures("roster_admits_acme_example")
def test_list_active_users_includes_execs_with_general_department(
    monkeypatch, set_test_workspace_domains
):
    """Regression: an org-wide exec stopped getting their Slack AI-Priorities DM.

    Root cause — a generation-vs-delivery population mismatch. The morning Slack
    DM (``slack_delivery.deliver_slack_priorities``) delivers to every ``is_exec``
    user regardless of department and reads ONLY the HomeCard this batch writes.
    Org-wide execs carry ``department='general'`` (the "All organizations" admin
    option / the model default), so the old ``department != 'general'`` filter
    generated nothing for them → empty bundle → silently no DM. The batch's
    population must be a superset of that delivery gate.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import rubik_core.database.db as db_mod
    from rubik_core.database.models import Base, User
    from rubik_core.priorities.batch import list_active_users

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine)
    with TestSession() as s:
        s.add_all(
            [
                # normal rep in an opted-in dept — always included.
                # last_login set so the never-signed-in filter isn't what keeps
                # or drops anyone here; this test is about the exec/domain gates.
                User(
                    email="rep@acme.example",
                    id="1",
                    name="Rep",
                    department="sales",
                    is_exec=False,
                    last_login=datetime(2026, 7, 1),
                ),
                # org-wide exec (the CEO) — dept 'general' but is_exec: the fix.
                # Deliberately never-signed-in: execs are exempt from that filter
                # too, for the same delivery-superset reason.
                User(
                    email="ceo@acme.example",
                    id="2",
                    name="CEO",
                    department="general",
                    is_exec=True,
                ),
                # a signed-in person filed under 'general' (no team) — on the
                # roster too: departments are optional, so no department is
                # no reason to go without a briefing.
                User(
                    email="contact@acme.example",
                    id="3",
                    name="Contact",
                    department="general",
                    is_exec=False,
                    last_login=datetime(2026, 7, 1),
                ),
                # exec but off-domain (can't DWD-impersonate) — domain guard wins
                User(
                    email="exec@gmail.com",
                    id="4",
                    name="Ext",
                    department="general",
                    is_exec=True,
                    last_login=datetime(2026, 7, 1),
                ),
            ]
        )
        s.commit()

    monkeypatch.setattr(db_mod, "SessionLocal", TestSession)
    set_test_workspace_domains("acme.example")

    result = set(list_active_users())

    assert "ceo@acme.example" in result  # was silently dropped before the fix
    assert "rep@acme.example" in result
    assert "contact@acme.example" in result  # no department, still a reader
    assert "exec@gmail.com" not in result  # domain guard still applies to execs

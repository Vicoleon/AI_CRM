"""An operator hold must never transiently activate a Cloud Scheduler job."""

import os
from types import SimpleNamespace

import pytest

from rubik_core.platform.executor import CloudExecutor
from rubik_core.platform.schedules import reconcile_schedules


@pytest.fixture(autouse=True)
def held(monkeypatch):
    monkeypatch.setenv("CUBIE_AUTOMATIONS_ENABLED", "false")


@pytest.fixture
def manifest():
    return {
        "schedules": [
            {
                "name": "priorities-run-all",
                "scope": "per_workspace_timezone",
                "path": "/api/jobs/priorities/run-all",
                "schedule": "0 6 * * *",
            },
            {
                "name": "scheduled-tasks-tick",
                "scope": "platform_fanout",
                "path": "/api/agents/tasks/tick",
                "schedule": "* * * * *",
            },
        ]
    }


def _workspace():
    return SimpleNamespace(
        slug="rubiksoft", status="active", time_zone="America/Costa_Rica"
    )


@pytest.mark.parametrize("prune", [True, False])
def test_hold_only_pauses_existing_manifest_owned_schedules(manifest, prune):
    paused = []

    class Executor:
        def list_schedule_names(self):
            return [
                "priorities-run-all--rubiksoft",
                "priorities-run-all--suspended",
                "scheduled-tasks-tick",
                "unrelated-job",
                "priorities-run-all--bad!",
            ]

        def pause_schedule(self, name):
            paused.append(name)

        def upsert_schedule(self, row):
            pytest.fail("hold cannot create or update jobs")

        def delete_schedule(self, name):
            pytest.fail("hold preserves existing jobs")

    rows = reconcile_schedules(manifest, [_workspace()], Executor(), prune=prune)
    assert len(rows) == 2
    assert paused == [
        "priorities-run-all--rubiksoft",
        "priorities-run-all--suspended",
        "scheduled-tasks-tick",
    ]


def test_new_workspace_during_hold_creates_no_schedule(manifest):
    class Executor:
        def list_schedule_names(self):
            return []

        def __getattr__(self, name):
            pytest.fail(f"unexpected mutation {name}")

    rows = reconcile_schedules(manifest, [_workspace()], Executor(), prune=False)
    assert {row["name"] for row in rows} == {
        "priorities-run-all--rubiksoft",
        "scheduled-tasks-tick",
    }


def test_hold_dry_run_does_not_read_or_mutate_cloud(manifest):
    rows = reconcile_schedules(manifest, [_workspace()], object(), dry_run=True)
    assert len(rows) == 2


class Response:
    def __init__(self, status, payload=None):
        self.status_code = status
        self.ok = status < 400
        self.payload = payload or {}

    def json(self):
        return self.payload


def _executor(monkeypatch, state, *, status=200):
    calls = []

    class Session:
        def get(self, url):
            calls.append(("get", url))
            return Response(status, {"state": state})

        def post(self, url, *, json):
            calls.append(("post", url, json))
            assert url.endswith(":pause")
            return Response(200, {"state": "PAUSED"})

        def __getattr__(self, name):
            pytest.fail(f"unexpected cloud action {name}")

    executor = object.__new__(CloudExecutor)
    executor.project = "test-project"
    executor.region = "us-central1"
    executor.service_url = "https://app.example.com"
    monkeypatch.setattr(executor, "_authorized_session", lambda: Session())
    return executor, calls


def test_direct_upsert_under_hold_never_creates_a_missing_job(monkeypatch):
    executor, calls = _executor(monkeypatch, None, status=404)
    monkeypatch.delenv("SCHEDULER_API_KEY", raising=False)
    executor.upsert_schedule(
        {"name": "priorities-run-all--rubiksoft", "path": "/api/jobs/run"}
    )
    assert [call[0] for call in calls] == ["get"]


def test_existing_enabled_job_is_paused_without_upsert(monkeypatch):
    executor, calls = _executor(monkeypatch, "ENABLED")
    executor.pause_schedule("priorities-run-all--rubiksoft")
    assert [call[0] for call in calls] == ["get", "post"]
    assert calls[1][1].endswith("/priorities-run-all--rubiksoft:pause")
    assert calls[1][2] == {}


@pytest.mark.parametrize("state", ["PAUSED", "DISABLED"])
def test_pause_retry_keeps_inactive_job_unchanged(monkeypatch, state):
    executor, calls = _executor(monkeypatch, state)
    executor.pause_schedule("scheduled-tasks-tick")
    assert [call[0] for call in calls] == ["get"]


def test_invalid_pause_name_never_reaches_cloud(monkeypatch):
    executor, calls = _executor(monkeypatch, "ENABLED")
    with pytest.raises(ValueError, match="Invalid schedule name"):
        executor.pause_schedule("untrusted/path")
    assert calls == []


def test_new_workspace_cli_defaults_to_hold_before_scheduling(monkeypatch, manifest):
    from rubik_core.platform import cli

    monkeypatch.delenv("CUBIE_AUTOMATIONS_ENABLED")
    monkeypatch.setattr(
        cli, "_authorize_operator", lambda args: "admin@rubiksoft.example"
    )
    monkeypatch.setattr(cli, "default_executor", lambda: object())
    calls = []

    class Executor:
        def list_schedule_names(self):
            return []

        def __getattr__(self, name):
            pytest.fail(f"automatic schedule activation during CLI: {name}")

    class Provisioner:
        def __init__(self, *, executor):
            pass

        def create(self, spec, *, dry_run):
            calls.append(spec.slug)
            reconcile_schedules(manifest, [_workspace()], Executor())
            return SimpleNamespace(
                slug=spec.slug,
                status="active",
                completed_steps=(),
                checklist=(),
                dry_run_details=(),
            )

    monkeypatch.setattr(cli, "Provisioner", Provisioner)
    assert (
        cli.main(
            [
                "workspace",
                "create",
                "--slug",
                "rubiksoft",
                "--name",
                "RubikSoft",
                "--domain",
                "rubiksoft.example",
                "--first-admin",
                "admin@rubiksoft.example",
                "--dwd-subject",
                "admin@rubiksoft.example",
                "--actor-email",
                "admin@rubiksoft.example",
                "--time-zone",
                "America/Costa_Rica",
            ]
        )
        == 0
    )
    assert calls == ["rubiksoft"]
    assert "CUBIE_AUTOMATIONS_ENABLED" not in os.environ

"""Workspace routing and bounded fan-out for Cloud Scheduler."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import yaml
from fastapi.testclient import TestClient

from backend.main import app
from rubik_core.config import settings

client = TestClient(app)


def test_fanout_isolates_one_workspace_failure_and_caps_concurrency():
    from rubik_core.tenancy.scheduler import run_for_workspaces

    active = 0
    peak = 0

    async def run(slug: str):
        from rubik_core.tenancy.context import current_workspace

        nonlocal active, peak
        assert current_workspace() == slug
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        if slug == "bad":
            raise RuntimeError("local failure")
        return {"workspace": slug}

    result = asyncio.run(run_for_workspaces(["a", "bad", "c", "d"], run, limit=2))

    assert peak <= 2
    assert result["bad"] == {"status": "error", "error": "RuntimeError: local failure"}
    assert result["a"] == {"status": "ok", "result": {"workspace": "a"}}
    assert set(result) == {"a", "bad", "c", "d"}


def test_time_zone_schedule_expansion_uses_active_rows_only():
    from rubik_core.tenancy.scheduler import expand_schedule_entries

    manifest = {
        "schedules": [
            {
                "name": "morning-priorities",
                "path": "/api/jobs/priorities/run-all",
                "schedule": "0 7 * * 1-5",
                "scope": "per_workspace_timezone",
            },
            {
                "name": "scheduled-tasks-tick",
                "path": "/api/agents/scheduled-tasks/tick",
                "schedule": "*/5 * * * *",
                "scope": "platform_fanout",
            },
            {
                "name": "direct-worker",
                "target": "job",
                "job_name": "cubie-worker",
                "schedule": "0 6 * * *",
                "scope": "per_workspace_timezone",
            },
        ]
    }
    workspaces = [
        SimpleNamespace(
            slug="rubiksoft", status="active", time_zone="America/Costa_Rica"
        ),
        SimpleNamespace(slug="acme", status="active", time_zone="America/Chicago"),
        SimpleNamespace(slug="paused", status="suspended", time_zone="UTC"),
    ]

    expanded = expand_schedule_entries(manifest, workspaces)

    assert [entry["name"] for entry in expanded] == [
        "morning-priorities--acme",
        "morning-priorities--rubiksoft",
        "scheduled-tasks-tick",
        "direct-worker--acme",
        "direct-worker--rubiksoft",
    ]
    assert expanded[0]["time_zone"] == "America/Chicago"
    assert expanded[0]["headers"]["X-Cubie-Workspace"] == "acme"
    assert expanded[1]["time_zone"] == "America/Costa_Rica"
    assert "X-Cubie-Workspace" not in expanded[2]["headers"]
    assert all("paused" not in entry["name"] for entry in expanded)
    assert expanded[3]["env"]["CUBIE_WORKSPACE"] == "acme"


def test_local_day_slot_follows_workspace_time_zone():
    from rubik_core.tenancy.scheduler import local_schedule_slot

    instant = datetime(2026, 10, 30, 2, 30, tzinfo=timezone.utc)
    assert local_schedule_slot("America/Chicago", instant) == "2026-10-29"
    assert local_schedule_slot("Europe/Berlin", instant) == "2026-10-30"


def test_direct_job_cannot_be_unscoped():
    import pytest

    from rubik_core.tenancy.scheduler import expand_schedule_entries

    with pytest.raises(ValueError, match="one workspace"):
        expand_schedule_entries(
            {
                "schedules": [
                    {
                        "name": "unsafe-job",
                        "target": "job",
                        "scope": "platform_fanout",
                    }
                ]
            },
            [],
        )


def test_manifest_expands_live_workspace_schedule_and_job_env():
    from pathlib import Path

    from rubik_core.tenancy.scheduler import expand_schedule_entries

    path = Path(__file__).resolve().parents[2] / "deploy" / "schedules.yaml"
    manifest = yaml.safe_load(path.read_text())
    rows = [SimpleNamespace(slug="test", status="active", time_zone="America/Chicago")]
    expanded = expand_schedule_entries(manifest, rows)
    daily = next(row for row in expanded if row["name"] == "priorities-run-all--test")
    assert daily["time_zone"] == "America/Chicago"
    assert daily["headers"]["X-Cubie-Workspace"] == "test"
    assert daily["headers"]["X-Scheduler-Key"] == {"secret": "SCHEDULER_API_KEY"}
    tick = next(row for row in expanded if row["name"] == "scheduled-tasks-tick")
    assert tick["time_zone"] == "UTC"
    assert "X-Cubie-Workspace" not in tick["headers"]
    friday = next(
        row for row in expanded if row["name"] == "priorities-deliver-next-week--test"
    )
    assert friday["schedule"] == "0 16 * * 5"
    assert friday["time_zone"] == "America/Chicago"
    assert friday["body"] == {"report": "next_week"}


def test_named_scheduler_refuses_unknown_paused_and_eos_off(monkeypatch):
    from rubik_core.control import repository

    monkeypatch.setattr(settings, "SCHEDULER_API_KEY", "scheduler-secret")
    rows = {
        "test": SimpleNamespace(slug="test", status="active"),
        "paused": SimpleNamespace(slug="paused", status="suspended"),
        "eos-off": SimpleNamespace(slug="eos-off", status="active"),
    }
    monkeypatch.setattr(repository, "get_workspace", lambda slug: rows.get(slug))
    monkeypatch.setattr(
        repository,
        "module_enabled",
        lambda module, slug=None: slug != "eos-off",
    )
    for slug in ("missing", "paused", "eos-off"):
        response = client.post(
            "/api/agents/ingest/eos",
            headers={
                "X-Scheduler-Key": "scheduler-secret",
                "X-Cubie-Workspace": slug,
            },
        )
        assert response.status_code == 404


def test_key_only_fans_out_and_isolates_disabled_result(monkeypatch):
    from rubik_core.control import repository

    monkeypatch.setattr(settings, "SCHEDULER_API_KEY", "scheduler-secret")
    monkeypatch.setattr(settings, "WHATS_NEW_ENABLED", False)
    monkeypatch.setattr(
        repository,
        "active_workspaces",
        lambda module=None: [
            SimpleNamespace(slug="acme", status="active"),
            SimpleNamespace(slug="test", status="active"),
        ],
    )
    response = client.post(
        "/api/agents/whats-new/digest",
        headers={"X-Scheduler-Key": "scheduler-secret"},
    )
    assert response.status_code == 200
    outcomes = response.json()["workspaces"]
    assert set(outcomes) == {"acme", "test"}
    assert all(row["status"] == "ok" for row in outcomes.values())
    assert all(row["result"]["status"] == "disabled" for row in outcomes.values())


def test_partial_fanout_failure_returns_retryable_http_status(monkeypatch):
    from fastapi.responses import JSONResponse

    from rubik_core.control import repository
    from rubik_core.tenancy.middleware import WorkspaceMiddleware

    monkeypatch.setattr(settings, "SCHEDULER_API_KEY", "scheduler-secret")
    monkeypatch.setattr(settings, "WHATS_NEW_ENABLED", False)
    monkeypatch.setattr(
        repository,
        "active_workspaces",
        lambda module=None: [
            SimpleNamespace(slug="acme", status="active"),
            SimpleNamespace(slug="test", status="active"),
        ],
    )
    selected_app = WorkspaceMiddleware._selected_app

    async def fail_acme(self, scope, receive, send):
        if scope["state"]["workspace"] == "acme":
            return await JSONResponse({"detail": "failed"}, status_code=503)(
                scope, receive, send
            )
        return await selected_app(self, scope, receive, send)

    monkeypatch.setattr(WorkspaceMiddleware, "_selected_app", fail_acme)
    response = client.post(
        "/api/agents/whats-new/digest",
        headers={"X-Scheduler-Key": "scheduler-secret"},
    )
    assert response.status_code == 502
    outcomes = response.json()["workspaces"]
    assert outcomes["acme"] == {"status": "error", "error": "RuntimeError: HTTP 503"}
    assert outcomes["test"]["status"] == "ok"


def test_priorities_fanout_launches_without_blocking_event_loop(monkeypatch):
    import threading
    import time

    from backend.routers import jobs
    from rubik_core.control import repository
    from rubik_core.tenancy.context import current_workspace

    monkeypatch.setattr(settings, "SCHEDULER_API_KEY", "scheduler-secret")
    monkeypatch.setattr(
        repository,
        "active_workspaces",
        lambda module=None: [
            SimpleNamespace(slug="acme", status="active"),
            SimpleNamespace(slug="test", status="active"),
        ],
    )
    monkeypatch.setattr(jobs, "_list_active_users", lambda: [])
    lock = threading.Lock()
    active = 0
    peak = 0
    launched_workspaces = []

    def launch(**kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            launched_workspaces.append(current_workspace())
        time.sleep(0.05)
        with lock:
            active -= 1
        return {"name": "executions/test"}

    monkeypatch.setattr(jobs, "_launch_priorities_job_execution", launch)
    for path in ("/api/jobs/priorities/run-all", "/api/jobs/priorities/run-next-week"):
        peak = 0
        launched_workspaces.clear()
        response = client.post(path, headers={"X-Scheduler-Key": "scheduler-secret"})
        assert response.status_code == 200, response.text
        assert peak == 2
        assert set(launched_workspaces) == {"acme", "test"}


def test_key_only_ingest_runs_outside_copilot_gate(monkeypatch):
    from backend.routers import ingest

    monkeypatch.setattr(settings, "SCHEDULER_API_KEY", "scheduler-secret")
    seen = []
    monkeypatch.setattr(
        ingest, "submit_background_work", lambda *a, **kw: seen.append(kw)
    )
    response = client.post(
        "/api/ingest/scheduler/sync",
        headers={"X-Scheduler-Key": "scheduler-secret"},
    )
    assert response.status_code == 200
    assert response.json()["workspaces"]["test"]["status"] == "ok"
    assert len(seen) == 1


def test_eos_fanout_excludes_module_off_workspace_and_launches_job(monkeypatch):
    from unittest.mock import patch

    from rubik_core.control import repository
    from rubik_core.tenancy.context import current_workspace

    monkeypatch.setattr(settings, "SCHEDULER_API_KEY", "scheduler-secret")
    seen: list[str] = []

    def workspaces(module=None):
        assert module == "eos"
        return [SimpleNamespace(slug="test", status="active")]

    def launch(module, args):
        assert module == "backend.jobs.eos_full_ingest_job"
        assert args == []
        seen.append(current_workspace())
        return {"name": "executions/eos-test"}

    monkeypatch.setattr(repository, "active_workspaces", workspaces)
    with patch("backend.jobs.launcher.launch_module_execution", side_effect=launch):
        response = client.post(
            "/api/agents/ingest/eos",
            headers={"X-Scheduler-Key": "scheduler-secret"},
        )
    assert response.status_code == 200
    assert seen == ["test"]
    assert response.json()["workspaces"]["test"]["result"]["status"] == "dispatched"


def test_eos_full_ingest_job_fails_before_db_without_workspace(monkeypatch):
    from backend.jobs import eos_full_ingest_job

    monkeypatch.setattr("backend.jobs.bootstrap.bootstrap_job_workspace", lambda: False)
    assert eos_full_ingest_job.main() == 2


def test_scheduled_l10_and_fathom_sweeps_dispatch_scoped_jobs(monkeypatch):
    from unittest.mock import patch

    from rubik_core.control import repository
    from rubik_core.tenancy.context import current_workspace

    monkeypatch.setattr(settings, "SCHEDULER_API_KEY", "scheduler-secret")
    monkeypatch.setattr(repository, "module_enabled", lambda module, slug=None: True)
    seen = []

    def launch(module, args):
        seen.append((current_workspace(), module, args))
        return {"name": "executions/test"}

    headers = {
        "X-Scheduler-Key": "scheduler-secret",
        "X-Cubie-Workspace": "test",
    }
    with patch("backend.jobs.launcher.launch_module_execution", side_effect=launch):
        l10 = client.post(
            "/api/agents/eos/l10/summary-emails?hours=168", headers=headers
        )
        fathom = client.post("/api/agents/fathom/sync?limit=25", headers=headers)

    assert l10.status_code == 200, l10.text
    assert fathom.status_code == 200, fathom.text
    assert l10.json()["status"] == "dispatched"
    assert fathom.json()["status"] == "dispatched"
    assert seen == [
        ("test", "backend.jobs.l10_summary_email_job", ["--hours", "168"]),
        ("test", "backend.jobs.fathom_sync_job", ["--limit", "25"]),
    ]


def test_scheduled_long_sweep_launch_failure_is_not_acknowledged(monkeypatch):
    from unittest.mock import patch

    from backend.jobs.launcher import JobLaunchError
    from rubik_core.control import repository

    monkeypatch.setattr(settings, "SCHEDULER_API_KEY", "scheduler-secret")
    monkeypatch.setattr(repository, "module_enabled", lambda module, slug=None: True)
    with patch(
        "backend.jobs.launcher.launch_module_execution",
        side_effect=JobLaunchError("job unavailable"),
    ):
        response = client.post(
            "/api/agents/eos/l10/summary-emails",
            headers={"X-Scheduler-Key": "scheduler-secret"},
        )
    assert response.status_code == 502
    assert response.json()["workspaces"]["test"]["status"] == "error"


def test_human_cannot_select_other_workspace_and_no_key_does_no_work(monkeypatch):
    from backend.routers import ingest

    seen = []
    monkeypatch.setattr(
        ingest, "submit_background_work", lambda *a, **kw: seen.append(kw)
    )
    cross = client.post(
        "/api/ingest/scheduler/sync",
        headers={"X-Cubie-Workspace": "acme"},
    )
    assert cross.status_code == 403
    missing = client.post("/api/ingest/scheduler/sync")
    assert missing.status_code == 403
    assert seen == []


def test_targeted_briefing_requires_named_workspace_and_matching_domain(monkeypatch):
    from unittest.mock import patch

    monkeypatch.setattr(settings, "SCHEDULER_API_KEY", "scheduler-secret")
    with patch("backend.routers.jobs.launch_module_execution") as launch:
        unscoped = client.post(
            "/api/jobs/overnight-briefing",
            headers={"X-Scheduler-Key": "scheduler-secret"},
            json={"target_email": "person@acme.example"},
        )
        assert unscoped.status_code == 400
        assert unscoped.json()["detail"]["code"] == "workspace_required"
        other_domain = client.post(
            "/api/jobs/overnight-briefing",
            headers={
                "X-Scheduler-Key": "scheduler-secret",
                "X-Cubie-Workspace": "test",
            },
            json={"target_email": "person@another.example"},
        )
    assert other_domain.status_code == 404
    launch.assert_not_called()


def test_targeted_briefing_job_requires_workspace_before_db(monkeypatch):
    from backend.jobs import overnight_briefing_job

    monkeypatch.setattr("backend.jobs.bootstrap.bootstrap_job_workspace", lambda: False)
    assert overnight_briefing_job.main(["--target-email", "person@acme.example"]) == 2


def test_old_cron_header_no_longer_authorizes_transcript_sync(monkeypatch):
    from unittest.mock import patch

    monkeypatch.setattr(settings, "SCHEDULER_API_KEY", "scheduler-secret")
    with patch("backend.jobs.launcher.launch_module_execution") as launch:
        old = client.post(
            "/api/admin/transcript-sync", headers={"X-Cron-Secret": "old-secret"}
        )
        assert old.status_code in (401, 403)
        current = client.post(
            "/api/admin/transcript-sync?days=2&users=one@acme.example",
            headers={
                "X-Scheduler-Key": "scheduler-secret",
                "X-Cubie-Workspace": "test",
            },
        )
    assert current.status_code == 200
    assert current.json()["status"] == "dispatched"
    launch.assert_called_once_with(
        "rubik_core.scripts.transcript_sync",
        ["--days", "2", "--users", "one@acme.example"],
    )


def test_scheduler_key_has_no_access_to_unrelated_paths(monkeypatch):
    monkeypatch.setattr(settings, "SCHEDULER_API_KEY", "scheduler-secret")
    response = client.get(
        "/api/users/me", headers={"X-Scheduler-Key": "scheduler-secret"}
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "scheduler_path_denied"

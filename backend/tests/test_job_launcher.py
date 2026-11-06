"""#9 background-job hardening — the shared Cloud Run Job launcher.

Covers the override-body construction (single-task pinning included), the
job-vs-inline dispatch decision in ``submit_background_work``, and the
priorities run-body builder's taskCount rules. No network/GCP is touched.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from backend.jobs import launcher
from rubik_core.config import AppMode, settings


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
    # The metadata-server branch fails in test (creds.refresh on a dummy
    # request object raises) → the launcher falls back to google.auth.default,
    # which we stub. Same pattern as test_deep_prep_job.py.
    monkeypatch.setattr(
        "google.auth.default", lambda scopes=None: (_FakeCreds(), "proj")
    )
    monkeypatch.setattr(launcher, "gcp_project_id", lambda: "test-project")
    monkeypatch.setattr("google.auth.transport.requests.Request", lambda: object())


def test_launch_module_execution_builds_single_task_override(monkeypatch):
    _patch_auth(monkeypatch)
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _FakeResp(200, {"name": "exec-1"})

    monkeypatch.setattr("requests.post", fake_post)

    out = launcher.launch_module_execution(
        "backend.jobs.dam_ingest_job",
        ["--doc-id", "d1", "--actor-email", "j@acme.example"],
    )
    assert out == {"name": "exec-1"}
    assert captured["url"].endswith("/jobs/cubie-worker:run")
    overrides = captured["json"]["overrides"]
    # Every module-style execution is one piece of work → exactly one task.
    assert overrides["taskCount"] == 1
    args = overrides["containerOverrides"][0]["args"]
    assert overrides["containerOverrides"][0]["env"] == [
        {"name": "CUBIE_WORKSPACE", "value": "test"}
    ]
    # containerOverrides.args REPLACE the configured args — the module
    # invocation must be re-supplied first.
    assert args[:2] == ["-m", "backend.jobs.dam_ingest_job"]
    assert args[args.index("--doc-id") + 1] == "d1"


def test_launch_module_execution_raises_job_launch_error(monkeypatch):
    _patch_auth(monkeypatch)
    monkeypatch.setattr(
        "requests.post",
        lambda url, json=None, headers=None, timeout=None: _FakeResp(
            403, {}, text="permission denied"
        ),
    )
    with pytest.raises(launcher.JobLaunchError):
        launcher.launch_module_execution("backend.jobs.worker", [])


def test_submit_uses_job_on_gcp(monkeypatch):
    _patch_auth(monkeypatch)
    monkeypatch.setattr(settings, "APP_MODE", AppMode.GCP)
    monkeypatch.setattr(
        "requests.post",
        lambda url, json=None, headers=None, timeout=None: _FakeResp(
            200, {"name": "exec-9"}
        ),
    )
    bg = MagicMock()
    executor, execution_name = launcher.submit_background_work(
        bg,
        module="backend.jobs.fathom_sync_job",
        args=["--user-email", "j@acme.example"],
        fallback=lambda: None,
        label="test-dispatch",
    )
    assert executor == "job"
    assert execution_name == "exec-9"
    bg.add_task.assert_not_called()


def test_submit_falls_back_inline_when_launch_fails(monkeypatch):
    _patch_auth(monkeypatch)
    monkeypatch.setattr(settings, "APP_MODE", AppMode.GCP)
    monkeypatch.setattr(
        "requests.post",
        lambda url, json=None, headers=None, timeout=None: _FakeResp(
            404, {}, text="job not found"
        ),
    )
    bg = MagicMock()
    sentinel = lambda: None  # noqa: E731
    executor, execution_name = launcher.submit_background_work(
        bg,
        module="backend.jobs.fathom_sync_job",
        args=[],
        fallback=sentinel,
        label="test-fallback",
    )
    assert executor == "inline"
    assert execution_name is None
    bg.add_task.assert_called_once()
    bg.add_task.call_args.args[0]()


def test_submit_goes_inline_off_gcp_without_network(monkeypatch):
    monkeypatch.setattr(settings, "APP_MODE", AppMode.LOCAL)

    def _boom(*a, **k):  # pragma: no cover - would mean a network attempt
        raise AssertionError("no HTTP call expected off GCP")

    monkeypatch.setattr("requests.post", _boom)
    bg = MagicMock()
    executor, execution_name = launcher.submit_background_work(
        bg,
        module="backend.jobs.worker",
        args=[],
        fallback=lambda: None,
        label="test-local",
    )
    assert executor == "inline"
    bg.add_task.assert_called_once()


# ── the UC-42 priorities run-body builder (backend/routers/jobs.py) ──────────


def test_priorities_body_single_user_pins_one_task():
    from backend.routers.jobs import _build_priorities_job_run_body

    body = _build_priorities_job_run_body(target_email="j@acme.example", run_id="r1")
    overrides = body["overrides"]
    assert overrides["taskCount"] == 1
    args = overrides["containerOverrides"][0]["args"]
    assert args[:2] == ["-m", "rubik_core.priorities.batch_runner"]
    assert args[args.index("--target-email") + 1] == "j@acme.example"


def test_priorities_body_full_batch_keeps_sharding():
    """A run_id-only body must NOT pin taskCount — the daily batch relies on
    --tasks=4 sharding to finish inside the job timeout."""
    from backend.routers.jobs import _build_priorities_job_run_body

    body = _build_priorities_job_run_body(target_email=None, run_id="r1")
    assert "taskCount" not in body["overrides"]

    # And the no-override body stays {} so plain run.invoker keeps working.
    assert _build_priorities_job_run_body(target_email=None, run_id=None) == {}

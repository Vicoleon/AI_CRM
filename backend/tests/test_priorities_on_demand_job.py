"""backend.jobs.priorities_on_demand_job — the UC-41 Job entrypoint (#9).

The job must translate its container args into the exact PrioritiesRequestBody
the inline path would build, drive the shared _run_and_persist_priorities, and
report the outcome on the priorities_runs row. No pipeline/DB/GCP is touched.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _quiet_side_effects(monkeypatch):
    monkeypatch.setattr("rubik_core.telemetry.set_admin_context", lambda v: None)
    monkeypatch.setattr("rubik_core.cost.usage.flush_usage", lambda: None)


def test_job_runs_pipeline_and_marks_ready(monkeypatch):
    import backend.jobs.priorities_on_demand_job as job

    calls = {}
    monkeypatch.setattr(
        "backend.routers.home._run_and_persist_priorities",
        lambda email, body: calls.setdefault("run", (email, body)),
    )
    monkeypatch.setattr(
        "rubik_core.priorities.run_state.mark_finished",
        lambda run_id, *, succeeded, error=None: calls.setdefault(
            "mark", (run_id, succeeded, error)
        ),
    )

    rc = job.main(
        [
            "--run-id",
            "r7",
            "--user-email",
            "bellison@acme.example",
            "--focus",
            "deals",
            "--max-priorities",
            "10",
            "--channels",
            "gmail,calendar",
        ]
    )
    assert rc == 0
    email, body = calls["run"]
    assert email == "bellison@acme.example"
    assert body.focus == "deals"
    assert body.max_priorities == 10
    assert body.channels == ["gmail", "calendar"]
    assert body.force_refresh is True  # the job only ever runs on-demand
    assert calls["mark"] == ("r7", True, None)


def test_job_defaults_optional_args_to_none(monkeypatch):
    import backend.jobs.priorities_on_demand_job as job

    calls = {}
    monkeypatch.setattr(
        "backend.routers.home._run_and_persist_priorities",
        lambda email, body: calls.setdefault("run", (email, body)),
    )
    monkeypatch.setattr(
        "rubik_core.priorities.run_state.mark_finished",
        lambda run_id, *, succeeded, error=None: None,
    )
    rc = job.main(["--run-id", "r1", "--user-email", "j@acme.example"])
    assert rc == 0
    _, body = calls["run"]
    assert body.focus is None
    assert body.max_priorities is None
    assert body.channels is None


def test_job_marks_failed_but_exits_zero(monkeypatch):
    """A pipeline failure lands on the run row; the task still exits 0 so
    Cloud Run's --max-retries=1 never re-runs a 3–5 min LLM pipeline."""
    import backend.jobs.priorities_on_demand_job as job

    def _boom(email, body):
        raise RuntimeError("channel outage")

    marks = {}
    monkeypatch.setattr("backend.routers.home._run_and_persist_priorities", _boom)
    monkeypatch.setattr(
        "rubik_core.priorities.run_state.mark_finished",
        lambda run_id, *, succeeded, error=None: marks.setdefault(
            "mark", (run_id, succeeded, error)
        ),
    )
    rc = job.main(["--run-id", "r9", "--user-email", "j@acme.example"])
    assert rc == 0
    run_id, succeeded, error = marks["mark"]
    assert run_id == "r9"
    assert succeeded is False
    assert "channel outage" in error

"""The Loose Ends scan as a cubie-worker Job, and the scheduler endpoint that
dispatches it.

The job: its arguments, the kill switch (a dry run ignores it on purpose), the
exit code (finding loose ends is success; being unable to look is not) and the
AI-cost flush that a terminated job would otherwise skip.

The endpoint: a disabled flag is a 200 that says so — never a 404, which reads
as a broken job in Cloud Scheduler's alerting — a one-AE dry run answers inline
and rolls back, and a live run goes to cubie-worker with the Service's own
LOOP_CLOSURE_* values, falling back to running in the request when the Job
cannot be launched.

``run_scan`` is faked by string target; the scan itself is tested with the
package.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.jobs import loop_closure_scan_job as job
from backend.main import app
from rubik_core.config import settings

client = TestClient(app)

KEY = "sched-secret-test"
SCAN = "rubik_core.loop_closure.scan.run_scan"


class _FakeSession:
    closed = False

    def close(self):
        self.closed = True


@pytest.fixture
def session(monkeypatch):
    s = _FakeSession()
    monkeypatch.setattr("rubik_core.database.db.SessionLocal", lambda: s)
    return s


@pytest.fixture
def flushed(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "rubik_core.cost.usage.flush_usage", lambda *a, **k: calls.append(1)
    )
    return calls


@pytest.fixture
def scans(monkeypatch):
    calls: list[dict] = []
    reports = {"report": {"status": "completed", "scanned": 1, "new": 2}}

    def run_scan(db, **kwargs):
        calls.append(kwargs)
        return dict(reports["report"])

    monkeypatch.setattr(SCAN, run_scan)
    return SimpleNamespace(calls=calls, reports=reports)


# ── the job's arguments ──────────────────────────────────────────────────────


def test_the_parser_defaults_to_a_full_live_scan():
    args = job._build_parser().parse_args([])
    assert args.dry_run is False
    assert args.emails == ""
    assert args.no_digest is False and args.no_scorecard is False
    assert args.trigger == "cli"


def test_the_parser_rejects_what_it_does_not_know():
    with pytest.raises(SystemExit):
        job._build_parser().parse_args(["--everyone"])
    with pytest.raises(SystemExit):
        job._build_parser().parse_args(["--trigger", "cron"])


def test_emails_are_a_lower_cased_de_duplicated_list_or_the_roster():
    assert job._emails("") is None
    assert job._emails(" , ") is None
    assert job._emails("A@ACME.example, b@acme.example,a@acme.example") == [
        "a@acme.example",
        "b@acme.example",
    ]


def test_the_done_line_carries_only_the_top_level_counts():
    line = job._buckets(
        {"status": "completed", "new": 3, "coverage": {"ae@x.example": {"gmail": "ok"}}}
    )
    assert line == "new=3 status=completed"


# ── the kill switch ──────────────────────────────────────────────────────────


def test_flag_off_exits_zero_having_scanned_nothing(
    monkeypatch, session, flushed, capsys
):
    monkeypatch.setattr(settings, "LOOP_CLOSURE_ENABLED", False)
    monkeypatch.setattr(SCAN, lambda *a, **k: pytest.fail("scanned while switched off"))
    assert job.main([]) == job.EXIT_OK
    assert json.loads(capsys.readouterr().out)["status"] == "disabled"
    assert flushed == [1]


def test_a_dry_run_ignores_the_flag_and_scans_the_named_aes(
    monkeypatch, session, flushed, scans, capsys
):
    monkeypatch.setattr(settings, "LOOP_CLOSURE_ENABLED", False)
    code = job.main(
        [
            "--dry-run",
            "--emails",
            "AE@acme.example,other@acme.example",
            "--no-digest",
            "--trigger",
            "admin",
        ]
    )
    assert code == job.EXIT_OK
    assert scans.calls == [
        {
            "emails": ["ae@acme.example", "other@acme.example"],
            "dry_run": True,
            "trigger": "admin",
            "post_digest": False,
            "write_scorecard": True,
        }
    ]
    assert json.loads(capsys.readouterr().out)["new"] == 2
    assert session.closed and flushed == [1]


def test_a_live_run_scans_the_roster(monkeypatch, session, flushed, scans):
    monkeypatch.setattr(settings, "LOOP_CLOSURE_ENABLED", True)
    assert job.main(["--no-scorecard"]) == job.EXIT_OK
    assert scans.calls[0]["emails"] is None
    assert scans.calls[0]["dry_run"] is False
    assert scans.calls[0]["write_scorecard"] is False
    assert scans.calls[0]["trigger"] == "cli"


# ── the exit code ────────────────────────────────────────────────────────────


def test_a_failed_scan_exits_non_zero(monkeypatch, session, flushed, scans):
    monkeypatch.setattr(settings, "LOOP_CLOSURE_ENABLED", True)
    scans.reports["report"] = {"status": "failed", "reason": "hubspot_unavailable"}
    assert job.main([]) == job.EXIT_ERROR
    assert flushed == [1]


def test_a_scan_that_could_not_start_exits_non_zero_and_still_flushes(
    monkeypatch, session, flushed
):
    monkeypatch.setattr(settings, "LOOP_CLOSURE_ENABLED", True)

    def boom(*_a, **_k):
        raise RuntimeError("database unreachable")

    monkeypatch.setattr(SCAN, boom)
    assert job.main([]) == job.EXIT_ERROR
    assert session.closed and flushed == [1]


@pytest.mark.parametrize("attempt,code", [("1", 1), ("0", 0)])
def test_a_retry_that_finds_the_lock_held_fails_the_execution(
    monkeypatch, session, flushed, scans, attempt, code
):
    """The first attempt died (that is why there is a retry) and this one
    scanned nobody: exiting 0 would hide the morning from the Job alert. A
    FIRST attempt that meets another live run is just a double trigger."""
    monkeypatch.setattr(settings, "LOOP_CLOSURE_ENABLED", True)
    monkeypatch.setenv("CLOUD_RUN_TASK_ATTEMPT", attempt)
    scans.reports["report"] = {"status": "locked", "blocking_run_id": "r1"}
    assert job.main([]) == code


def test_one_aes_coverage_gap_is_not_a_failed_execution(
    monkeypatch, session, flushed, scans
):
    monkeypatch.setattr(settings, "LOOP_CLOSURE_ENABLED", True)
    scans.reports["report"] = {
        "status": "completed",
        "coverage": {"ae@acme.example": {"gmail": "unavailable"}},
    }
    assert job.main([]) == job.EXIT_OK


# ── the worker registry ──────────────────────────────────────────────────────


def test_the_job_is_registered_and_its_dispatcher_names_it():
    from pathlib import Path

    from backend.jobs import WORKER_JOBS, worker_job_args

    entry = WORKER_JOBS["loop-closure-scan"]
    assert entry.module == "backend.jobs.loop_closure_scan_job"
    assert entry.wants_cadence == ("weekdays 07:10 in the workspace business time zone")
    assert entry.example_args == ("--dry-run",)
    repo = Path(__file__).resolve().parents[2]
    assert entry.module in (repo / entry.dispatched_from).read_text()
    assert worker_job_args("loop-closure-scan") == [
        "-m",
        "backend.jobs.loop_closure_scan_job",
    ]


# ── the scheduler endpoint ───────────────────────────────────────────────────


@pytest.fixture
def scheduler(monkeypatch):
    monkeypatch.setattr(settings, "SCHEDULER_API_KEY", KEY)
    monkeypatch.setattr(settings, "ENABLE_MOCK_AUTH", False)
    monkeypatch.setattr(settings, "LOOP_CLOSURE_ENABLED", True)
    return {"X-Scheduler-Key": KEY, "X-Cubie-Workspace": "test"}


@pytest.fixture
def launches(monkeypatch):
    calls: list[tuple] = []

    def launch(module, args, **kwargs):
        calls.append((module, list(args), kwargs))
        return {"metadata": {"name": "cubie-worker-abc"}}

    monkeypatch.setattr("backend.jobs.launcher.launch_module_execution", launch)
    return calls


URL = "/api/agents/jobs/loop-closure/scan"


@pytest.fixture(autouse=True)
def roster(monkeypatch):
    """Who is on the Loose Ends roster (the endpoint checks ``?email=``)."""
    on = ["ae@acme.example"]
    monkeypatch.setattr(
        "rubik_core.loop_closure.roster.roster_emails", lambda _db, **_k: list(on)
    )
    return on


def test_no_key_and_no_user_is_a_403(scheduler):
    assert client.post(URL).status_code == 403


def test_flag_off_is_a_200_that_says_disabled_never_a_404(
    monkeypatch, scheduler, launches
):
    monkeypatch.setattr(settings, "LOOP_CLOSURE_ENABLED", False)
    res = client.post(URL, headers=scheduler)
    assert res.status_code == 200
    assert res.json()["status"] == "disabled"
    assert res.json()["flag"] == "LOOP_CLOSURE_ENABLED"
    assert launches == []


def test_a_one_ae_dry_run_answers_inline_and_rolls_back(
    monkeypatch, scheduler, launches, session, scans
):
    monkeypatch.setattr(settings, "LOOP_CLOSURE_ENABLED", False)  # ignored
    res = client.post(
        URL,
        params={"dry_run": "true", "email": " AE@acme.example "},
        headers=scheduler,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["executor"] == "inline" and body["trigger"] == "scheduler"
    assert body["new"] == 2
    assert scans.calls == [
        {"emails": ["ae@acme.example"], "dry_run": True, "trigger": "admin"}
    ]
    assert launches == []


def test_a_live_run_goes_to_the_worker_with_the_services_values(scheduler, launches):
    res = client.post(URL, headers=scheduler)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["executor"] == "job" and body["status"] == "dispatched"
    assert body["execution"] == "cubie-worker-abc"
    [(module, args, kwargs)] = launches
    assert module == "backend.jobs.loop_closure_scan_job"
    assert args == ["--trigger", "scheduler"]
    env = kwargs["env"]
    assert env["LOOP_CLOSURE_ENABLED"] == "true"
    # The daily post's switch and channel live in the database (Admin → Loose
    # Ends), so no channel travels as env and the setting no longer exists.
    assert not any("SLACK" in name for name in env)
    assert not hasattr(settings, "LOOP_CLOSURE_SLACK_CHANNEL")
    assert all(isinstance(v, str) for v in env.values())


def test_a_roster_dry_run_is_dispatched_with_the_flag_passed_through(
    monkeypatch, scheduler, launches
):
    monkeypatch.setattr(settings, "LOOP_CLOSURE_ENABLED", False)
    res = client.post(URL, params={"dry_run": "true"}, headers=scheduler)
    assert res.status_code == 200
    [(_, args, kwargs)] = launches
    assert args == ["--trigger", "scheduler", "--dry-run"]
    assert kwargs["env"]["LOOP_CLOSURE_ENABLED"] == "false"


def test_a_failed_launch_falls_back_to_running_in_the_request(
    monkeypatch, scheduler, session, scans
):
    def launch(*_a, **_k):
        raise RuntimeError("cubie-worker not deployed")

    monkeypatch.setattr("backend.jobs.launcher.launch_module_execution", launch)
    res = client.post(URL, params={"email": "ae@acme.example"}, headers=scheduler)
    assert res.status_code == 200, res.text
    assert res.json()["executor"] == "inline_fallback"
    assert scans.calls == [
        {"emails": ["ae@acme.example"], "dry_run": False, "trigger": "scheduler"}
    ]


def test_env_values_parse_back_to_the_same_setting():
    from backend.routers.agents import _env_value

    assert _env_value(True) == "true" and _env_value(False) == "false"
    assert (
        _env_value(["acme.example", "acme-labs.example"])
        == '["acme.example", "acme-labs.example"]'
    )
    assert _env_value(None) == ""
    assert _env_value(14) == "14"
    assert _env_value("acme.example") == "acme.example"


def test_an_address_off_the_roster_is_refused_before_anything_is_read(
    scheduler, launches, session, scans
):
    for dry in ("true", "false"):
        res = client.post(
            URL,
            params={"dry_run": dry, "email": "ifoster@acme.example"},
            headers=scheduler,
        )
        assert res.status_code == 400, res.text
        assert "roster" in res.json()["detail"]
    assert scans.calls == [] and launches == []


def test_a_run_started_by_a_person_is_audited(monkeypatch, launches, tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from rubik_core.auth.session_tokens import mint_session_token
    from rubik_core.database.models import AuditLog, Base
    from rubik_core.database.schemas import UserProfile
    from rubik_core.tenancy.context import use_workspace

    engine = create_engine(f"sqlite:///{tmp_path / 'audit.db'}")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr("rubik_core.database.db.SessionLocal", Session)
    monkeypatch.setattr(settings, "SCHEDULER_API_KEY", KEY)
    monkeypatch.setattr(settings, "LOOP_CLOSURE_ENABLED", True)
    admin = UserProfile(
        id="adm",
        email="Admin@ACME.example",
        name="Admin",
        department="Systems",
        roles=["admin"],
        created_at="2026-10-06T00:00:00",
        updated_at="2026-10-06T00:00:00",
    )
    monkeypatch.setattr(
        "backend.routers.agents.get_current_user", lambda request, credentials: admin
    )
    with use_workspace("test"):
        token, _, _ = mint_session_token("admin@acme.example", "test")
    res = client.post(
        URL,
        params={"email": "ae@acme.example"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200, res.text
    [row] = (
        Session()
        .query(AuditLog)
        .filter(AuditLog.action == "LOOSE_ENDS_MANUAL_SCAN")
        .all()
    )
    assert row.actor_email == "admin@acme.example"
    assert row.doc_id == "ae@acme.example"
    assert row.details == {"dry_run": False}

    # The scheduler's own morning run is not a person: no row.
    client.post(URL, headers={"X-Scheduler-Key": KEY, "X-Cubie-Workspace": "test"})
    assert Session().query(AuditLog).count() == 1

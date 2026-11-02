"""EOS re-ingest debounce/coalesce (#9 follow-up).

Exercises the eos_ingest_state watermark+claim machine
(rubik_core/ingestion/eos/debounce.py), the rewritten hooks
(backend/jobs/eos_ingest_hooks.py), and the cubie-worker entrypoint
(backend/jobs/eos_reingest_job.py) against a throwaway SQLite file — the same
rows Cloud SQL holds in dev/prod. No network/GCP/Vertex is touched.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from rubik_core.config import settings
from rubik_core.ingestion.eos import debounce


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    # Belt-and-braces: a developer .env with CLOUDSQL_CONNECTION_NAME set must
    # never route these tests at dev Cloud SQL (known footgun — docs/ENGINEERING_NOTES.md).
    settings.CLOUDSQL_CONNECTION_NAME = ""
    settings.DATABASE_URL = f"sqlite:///{tmp_path / 'test_eos_debounce.db'}"
    import rubik_core.database.db as dbmod

    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    yield


def _row(kind: str) -> dict | None:
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.models import EosIngestState

    with SessionLocal() as db:
        r = db.get(EosIngestState, kind)
        if r is None:
            return None
        return {
            "status": r.status,
            "requested_at": r.requested_at,
            "claimed_at": r.claimed_at,
            "executor": r.executor,
            "execution_name": r.execution_name,
            "last_result": r.last_result,
        }


def _set_row(kind: str, **values) -> None:
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.models import EosIngestState

    with SessionLocal() as db:
        db.query(EosIngestState).filter(EosIngestState.kind == kind).update(
            values, synchronize_session=False
        )
        db.commit()


class _StubResult:
    def summary(self) -> str:
        return "3 succeeded, 0 failed"


# ── the watermark + claim machine ────────────────────────────────────────────


def test_first_request_wins_claim_second_coalesces():
    kind = debounce.KIND_ISSUES
    assert debounce.request_reingest(kind) is True
    row = _row(kind)
    assert row["status"] == debounce.STATUS_LAUNCHING
    first_watermark = row["requested_at"]
    assert first_watermark is not None

    # A second edit while the claim is live: watermark bumps, no new claim.
    assert debounce.request_reingest(kind) is False
    row = _row(kind)
    assert row["status"] == debounce.STATUS_LAUNCHING
    assert row["requested_at"] >= first_watermark


def test_stale_claim_is_reclaimed():
    kind = debounce.KIND_PROCESSES
    assert debounce.request_reingest(kind) is True
    _set_row(
        kind,
        claimed_at=datetime.utcnow()
        - timedelta(seconds=settings.EOS_REINGEST_CLAIM_STALE_SECONDS + 60),
    )
    # The dead claim must not block re-ingestion forever.
    assert debounce.request_reingest(kind) is True
    assert _row(kind)["status"] == debounce.STATUS_LAUNCHING


def test_request_reingest_rejects_non_coalesced_kind():
    with pytest.raises(ValueError):
        debounce.request_reingest("l10")


def test_release_cas_holds_and_detects_mid_run_request():
    kind = debounce.KIND_VTO
    debounce.request_reingest(kind)
    debounce.acquire_for_run(kind)
    assert _row(kind)["status"] == debounce.STATUS_RUNNING

    watermark = debounce.read_watermark(kind)
    # No newer request → release wins.
    assert debounce.release_if_no_newer_request(kind, watermark, result_summary="ok")
    row = _row(kind)
    assert row["status"] == debounce.STATUS_IDLE
    assert row["last_result"] == "ok"

    # A request newer than the watermark → release must refuse.
    debounce.request_reingest(kind)
    debounce.acquire_for_run(kind)
    stale_watermark = debounce.read_watermark(kind) - timedelta(seconds=5)
    assert (
        debounce.release_if_no_newer_request(kind, stale_watermark, result_summary="ok")
        is False
    )
    assert _row(kind)["status"] == debounce.STATUS_RUNNING


def test_record_dispatch_and_force_idle_never_raise():
    kind = debounce.KIND_ISSUES
    debounce.request_reingest(kind)
    debounce.record_dispatch(kind, debounce.EXECUTOR_JOB, "exec-42")
    row = _row(kind)
    assert row["executor"] == debounce.EXECUTOR_JOB
    assert row["execution_name"] == "exec-42"

    debounce.force_idle(kind, "dispatch failed")
    row = _row(kind)
    assert row["status"] == debounce.STATUS_IDLE
    assert row["last_result"] == "dispatch failed"
    # Best-effort helpers tolerate a missing row.
    debounce.record_dispatch("processes", debounce.EXECUTOR_INLINE, None)
    debounce.force_idle("processes", "noop")


# ── the executor loop ────────────────────────────────────────────────────────


def test_run_debounced_reingest_single_pass(monkeypatch):
    kind = debounce.KIND_ISSUES
    calls = []
    monkeypatch.setattr(
        "rubik_core.ingestion.eos.pipeline.ingest_issues",
        lambda db: calls.append("run") or _StubResult(),
    )
    debounce.request_reingest(kind)
    outcome = debounce.run_debounced_reingest(kind, wait_for_quiet=False)
    assert outcome == "3 succeeded, 0 failed"
    assert calls == ["run"]
    row = _row(kind)
    assert row["status"] == debounce.STATUS_IDLE
    assert row["last_result"] == "3 succeeded, 0 failed"


def test_run_loops_when_edit_lands_mid_pass(monkeypatch):
    """An edit during an ingest pass must trigger exactly one more pass, so the
    pass that releases always started after the newest edit."""
    kind = debounce.KIND_ISSUES
    calls = []

    def _ingest(db):
        calls.append("run")
        if len(calls) == 1:  # simulate an edit arriving mid-pass
            debounce.request_reingest(kind)
        return _StubResult()

    monkeypatch.setattr("rubik_core.ingestion.eos.pipeline.ingest_issues", _ingest)
    debounce.request_reingest(kind)
    debounce.run_debounced_reingest(kind, wait_for_quiet=False)
    assert len(calls) == 2
    assert _row(kind)["status"] == debounce.STATUS_IDLE


def test_passes_exhausted_chains_relaunch(monkeypatch):
    """Edits that never stop: the execution hands off to a fresh one instead of
    running unbounded (cubie-worker has a 3600s task timeout)."""
    kind = debounce.KIND_ISSUES
    calls = []
    relaunches = []

    def _ingest(db):
        calls.append("run")
        debounce.request_reingest(kind)  # an edit lands during EVERY pass
        return _StubResult()

    monkeypatch.setattr("rubik_core.ingestion.eos.pipeline.ingest_issues", _ingest)
    debounce.request_reingest(kind)
    outcome = debounce.run_debounced_reingest(
        kind, wait_for_quiet=False, relaunch=lambda: relaunches.append(1)
    )
    assert outcome == "relaunched"
    assert len(calls) == debounce.MAX_PASSES_PER_EXECUTION
    assert relaunches == [1]
    # Claim carried over to the chained execution.
    assert _row(kind)["status"] == debounce.STATUS_LAUNCHING


def test_passes_exhausted_inline_releases(monkeypatch):
    kind = debounce.KIND_ISSUES

    def _ingest(db):
        debounce.request_reingest(kind)
        return _StubResult()

    monkeypatch.setattr("rubik_core.ingestion.eos.pipeline.ingest_issues", _ingest)
    debounce.request_reingest(kind)
    outcome = debounce.run_debounced_reingest(kind, wait_for_quiet=False)
    assert outcome == "passes-exhausted"
    row = _row(kind)
    # Released with the request left pending — the next edit re-claims.
    assert row["status"] == debounce.STATUS_IDLE
    assert row["requested_at"] is not None


def test_failed_relaunch_releases(monkeypatch):
    kind = debounce.KIND_ISSUES

    def _ingest(db):
        debounce.request_reingest(kind)
        return _StubResult()

    def _boom():
        raise RuntimeError("jobs API down")

    monkeypatch.setattr("rubik_core.ingestion.eos.pipeline.ingest_issues", _ingest)
    debounce.request_reingest(kind)
    outcome = debounce.run_debounced_reingest(
        kind, wait_for_quiet=False, relaunch=_boom
    )
    assert outcome == "relaunch-failed"
    assert _row(kind)["status"] == debounce.STATUS_IDLE


def test_failed_pass_still_releases(monkeypatch):
    """A hard ingest failure is best-effort (old hooks' contract): logged,
    recorded on the row, claim released — never a retry storm."""
    kind = debounce.KIND_PROCESSES

    def _boom(db):
        raise RuntimeError("vertex outage")

    monkeypatch.setattr("rubik_core.ingestion.eos.pipeline.ingest_processes", _boom)
    debounce.request_reingest(kind)
    outcome = debounce.run_debounced_reingest(kind, wait_for_quiet=False)
    assert outcome.startswith("failed:")
    row = _row(kind)
    assert row["status"] == debounce.STATUS_IDLE
    assert "vertex outage" in row["last_result"]


def test_wait_until_quiet_returns_immediately_when_quiet():
    kind = debounce.KIND_ISSUES
    debounce.request_reingest(kind)
    _set_row(kind, requested_at=datetime.utcnow() - timedelta(seconds=999))

    def _no_sleep(_s):  # pragma: no cover - would mean an unexpected wait
        raise AssertionError("must not sleep when the burst is already quiet")

    debounce.wait_until_quiet(
        kind, quiet_seconds=30, max_wait_seconds=60, sleep=_no_sleep
    )
    # No row / no pending request → also immediate.
    debounce.wait_until_quiet(
        "processes", quiet_seconds=30, max_wait_seconds=60, sleep=_no_sleep
    )


def test_wait_until_quiet_sleeps_until_lull():
    kind = debounce.KIND_ISSUES
    debounce.request_reingest(kind)  # requested_at = now → not quiet yet
    sleeps = []

    def _sleep(s):
        sleeps.append(s)
        # Simulate time passing: the burst went quiet during the nap.
        _set_row(kind, requested_at=datetime.utcnow() - timedelta(seconds=999))

    debounce.wait_until_quiet(kind, quiet_seconds=30, max_wait_seconds=60, sleep=_sleep)
    assert len(sleeps) == 1


# ── the hooks (backend/jobs/eos_ingest_hooks.py) ─────────────────────────────


def test_hook_dispatches_once_then_coalesces(monkeypatch):
    import backend.jobs.eos_ingest_hooks as hooks

    dispatches = []
    monkeypatch.setattr(
        "backend.jobs.eos_ingest_hooks.submit_background_work",
        lambda bg, **kw: dispatches.append(kw) or ("job", "exec-7"),
    )
    bg = MagicMock()
    hooks.enqueue_issue_ingest(bg)
    hooks.enqueue_issue_ingest(bg)  # burst: second edit coalesces
    assert len(dispatches) == 1
    assert dispatches[0]["module"] == hooks.EOS_REINGEST_MODULE
    assert dispatches[0]["args"] == ["--kind", "issues"]
    row = _row(debounce.KIND_ISSUES)
    assert row["executor"] == "job"
    assert row["execution_name"] == "exec-7"


def test_hook_inline_fallback_runs_worker_loop(monkeypatch):
    """Off-GCP the real launcher schedules the inline fallback; running it must
    drive the same state machine to idle."""
    import backend.jobs.eos_ingest_hooks as hooks
    from rubik_core.config import AppMode

    monkeypatch.setattr(settings, "APP_MODE", AppMode.LOCAL)
    monkeypatch.setattr(
        "rubik_core.ingestion.eos.pipeline.ingest_processes",
        lambda db: _StubResult(),
    )
    bg = MagicMock()
    hooks.enqueue_process_ingest(bg)
    assert _row(debounce.KIND_PROCESSES)["status"] == debounce.STATUS_LAUNCHING
    bg.add_task.assert_called_once()
    inline_task = bg.add_task.call_args[0][0]
    inline_task()  # what FastAPI would run post-response
    row = _row(debounce.KIND_PROCESSES)
    assert row["status"] == debounce.STATUS_IDLE
    assert row["last_result"] == "3 succeeded, 0 failed"


def test_hook_never_raises_and_releases_on_dispatch_failure(monkeypatch):
    import backend.jobs.eos_ingest_hooks as hooks

    def _boom(bg, **kw):
        raise RuntimeError("unexpected launcher explosion")

    monkeypatch.setattr("backend.jobs.eos_ingest_hooks.submit_background_work", _boom)
    hooks.enqueue_vto_ingest(MagicMock())  # must not raise
    row = _row(debounce.KIND_VTO)
    assert row["status"] == debounce.STATUS_IDLE  # claim released for retry
    assert "dispatch failed" in row["last_result"]


def test_hook_never_raises_when_state_machine_is_down(monkeypatch):
    import backend.jobs.eos_ingest_hooks as hooks

    def _db_down(kind):
        raise RuntimeError("db down")

    monkeypatch.setattr(
        "backend.jobs.eos_ingest_hooks.debounce.request_reingest", _db_down
    )
    hooks.enqueue_issue_ingest(MagicMock())  # must not raise


def test_l10_hook_dispatches_per_session(monkeypatch):
    import backend.jobs.eos_ingest_hooks as hooks

    dispatches = []
    monkeypatch.setattr(
        "backend.jobs.eos_ingest_hooks.submit_background_work",
        lambda bg, **kw: dispatches.append(kw) or ("job", "exec-l10"),
    )
    hooks.enqueue_l10_session_ingest(MagicMock(), session_id="sess-123")
    assert len(dispatches) == 1
    assert dispatches[0]["args"] == ["--kind", "l10", "--session-id", "sess-123"]
    # No debounce row for per-session work.
    assert _row("l10") is None


def test_l10_inline_fallback_rebinds_dispatch_workspace(monkeypatch):
    import backend.jobs.eos_ingest_hooks as hooks
    import rubik_core.database.db as dbmod
    from rubik_core.ingestion.eos import pipeline
    from rubik_core.tenancy.context import current_workspace, use_workspace

    captured = []
    observed = []

    class FakeSession:
        def close(self):
            observed.append(("close", current_workspace()))

    def open_session():
        observed.append(("session", current_workspace()))
        return FakeSession()

    def ingest(session, *, session_id):
        observed.append((session_id, current_workspace()))
        return _StubResult()

    def submit(background_tasks, **kwargs):
        captured.append(kwargs["fallback"])
        return "inline", None

    monkeypatch.setattr(dbmod, "SessionLocal", open_session)
    monkeypatch.setattr(pipeline, "ingest_l10_session", ingest)
    monkeypatch.setattr(hooks, "submit_background_work", submit)

    with use_workspace("alpha"):
        hooks.enqueue_l10_session_ingest(MagicMock(), session_id="sess-123")
    assert len(captured) == 1
    with use_workspace("beta"):
        captured[0]()
        assert current_workspace() == "beta"
    assert observed == [
        ("session", "alpha"),
        ("sess-123", "alpha"),
        ("close", "alpha"),
    ]


# ── the job entrypoint (backend/jobs/eos_reingest_job.py) ────────────────────


def test_job_coalesced_kind_runs_debounced_loop(monkeypatch):
    import backend.jobs.eos_reingest_job as job

    captured = {}

    def _fake_run(kind, *, wait_for_quiet, relaunch, **kw):
        captured.update(kind=kind, wait_for_quiet=wait_for_quiet, relaunch=relaunch)
        return "ok"

    monkeypatch.setattr(
        "rubik_core.ingestion.eos.debounce.run_debounced_reingest", _fake_run
    )
    assert job.main(["--kind", "issues"]) == 0
    assert captured["kind"] == "issues"
    assert captured["wait_for_quiet"] is True
    assert callable(captured["relaunch"])


def test_job_l10_requires_session_id():
    import backend.jobs.eos_reingest_job as job

    with pytest.raises(SystemExit):
        job.main(["--kind", "l10"])


def test_job_l10_ingests_one_session(monkeypatch):
    import backend.jobs.eos_reingest_job as job

    captured = {}

    def _fake_ingest(db, session_id):
        captured["sid"] = session_id
        return _StubResult()

    monkeypatch.setattr(
        "rubik_core.ingestion.eos.pipeline.ingest_l10_session", _fake_ingest
    )
    assert job.main(["--kind", "l10", "--session-id", "sess-9"]) == 0
    assert captured["sid"] == "sess-9"

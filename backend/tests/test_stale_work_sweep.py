"""The nightly backstop that reports work nobody produced a result for.

Owns the reporting side for all three features. The per-feature queries are
tested next to their features (Smart Teams' in
``test_smart_teams_stale_scoring_alert.py``); what is pinned here is the part
that makes any of it reach a human:

* each probe finds its own feature's stuck rows, and only past that feature's
  OWN threshold — a second opinion about when something is broken is how an
  alert ends up firing at a different moment from the screen a person is
  looking at;
* one probe raising does not turn into a silent all-clear for the others;
* "checked, nothing waiting" and "could not check" are different answers, and
  only the second one fails the execution;
* every token a probe can emit has a policy in ``setup_monitoring.sh``. That
  last one is the only thing standing between adding a feature and that
  feature being quietly uncovered.
"""

from __future__ import annotations

import logging
import pathlib
import uuid
from datetime import datetime, timedelta

import pytest

from rubik_core.config import settings
from rubik_core.reliability import stale_work as sw

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
ASH = "ash@acme.example"


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    """A throwaway SQLite database bound to the module-level ``SessionLocal``.

    The sweep opens its own session because it is a separate process in
    production, so re-pointing the shared engine is the only way to put it on a
    test database. Restored on the way out — a ``tmp_path`` URL left bound fails
    a LATER module and blames it for this one's mess.
    """
    import rubik_core.database.db as dbmod

    previous_url = settings.DATABASE_URL
    settings.DATABASE_URL = f"sqlite:///{tmp_path / 'stale_work.db'}"
    dbmod._engine = None
    engine = dbmod.get_engine()
    dbmod._SessionLocal.configure(bind=engine)

    from rubik_core.database.models import Base

    Base.metadata.create_all(bind=engine)
    try:
        yield engine
    finally:
        settings.DATABASE_URL = previous_url
        dbmod._engine = None


def _session():
    from rubik_core.database.db import SessionLocal

    return SessionLocal()


def _deep_prep(
    db, *, minutes_ago: float, status: str = "queued", mode: str = "deep"
) -> str:
    from rubik_core.database.models import MeetingPrepRun

    row = MeetingPrepRun(
        id=uuid.uuid4().hex,
        user_email=ASH,
        event_id="evt_1",
        mode=mode,
        delivery_status=status,
        created_at=datetime.utcnow() - timedelta(minutes=minutes_ago),
    )
    db.add(row)
    db.commit()
    return row.id


def _xray(
    db, *, updated_minutes_ago: float, status: str = "pending", kind: str = "deep"
) -> str:
    from rubik_core.database.models import XraySnapshot

    stamp = datetime.utcnow() - timedelta(minutes=updated_minutes_ago)
    row = XraySnapshot(
        id=uuid.uuid4().hex,
        company_id="12345",
        kind=kind,
        status=status,
        created_by=ASH,
        created_at=stamp,
        updated_at=stamp,
    )
    db.add(row)
    db.commit()
    return row.id


def _guide(
    db,
    *,
    minutes_ago: float,
    published: bool,
    rag_ingested: bool,
    unpublished: bool = False,
    approved: bool = True,
    subject: str = ASH,
) -> str:
    from rubik_core.database.smart_teams import SmartTeamsDocument

    stamp = datetime.utcnow() - timedelta(minutes=minutes_ago)
    row = SmartTeamsDocument(
        id=uuid.uuid4().hex,
        kind="personal_guide",
        subject_emails=[subject],
        subject_key=subject,
        profile_versions={subject: 1},
        profile_version_key=f"{subject}:1",
        status="ready",
        approved_at=stamp if approved else None,
        approved_by=subject if approved else None,
        published=published,
        unpublished_at=stamp if unpublished else None,
        rag_ingested=rag_ingested,
        created_at=stamp,
        updated_at=stamp,
    )
    db.add(row)
    db.commit()
    return row.id


# ── Deep Prep ────────────────────────────────────────────────────────────────


def test_a_recent_deep_prep_is_not_stale():
    with _session() as db:
        _deep_prep(db, minutes_ago=5)
        assert sw.deep_prep_stale(db) == []


@pytest.mark.parametrize("status", ["queued", "running"])
def test_a_deep_prep_stuck_in_flight_past_the_threshold_is_stale(status):
    """Both in-flight states count: the row is written `queued` by the request
    and only the job ever moves it, so a job that dies before its first status
    write leaves either one standing indefinitely."""
    with _session() as db:
        run_id = _deep_prep(
            db, minutes_ago=sw.DEEP_PREP_STALE_MINUTES + 10, status=status
        )
        assert [i.id for i in sw.deep_prep_stale(db)] == [run_id]


@pytest.mark.parametrize("status", ["delivered", "failed"])
def test_a_finished_deep_prep_is_never_stale(status):
    """`failed` included: it already reported itself, and re-paging on a
    known failure is how an alert gets muted."""
    with _session() as db:
        _deep_prep(db, minutes_ago=5_000, status=status)
        assert sw.deep_prep_stale(db) == []


def test_a_synchronous_prep_is_never_stale():
    """The on-screen path has no delivery to wait for."""
    with _session() as db:
        _deep_prep(db, minutes_ago=5_000, mode="sync", status="queued")
        assert sw.deep_prep_stale(db) == []


def test_the_deep_prep_item_carries_args_that_rerun_that_row():
    with _session() as db:
        run_id = _deep_prep(db, minutes_ago=5_000)
        item = sw.deep_prep_stale(db)[0]

    assert item.rerun_args.startswith("-m,backend.jobs.deep_prep_job,")
    assert f"--run-id,{run_id}" in item.rerun_args
    assert f"--user-email,{ASH}" in item.rerun_args


# ── Deep X-Ray ───────────────────────────────────────────────────────────────


def test_a_deep_xray_still_writing_progress_is_not_stale():
    """`updated_at` advancing is proof of life, so a long-but-live multi-agent
    run is left alone."""
    from rubik_core.xray import config as xray_config

    with _session() as db:
        _xray(db, updated_minutes_ago=xray_config.DEEP_RUN_TTL_MINUTES - 5)
        assert sw.xray_deep_stale(db) == []


@pytest.mark.parametrize("status", ["pending", "running"])
def test_a_deep_xray_past_the_features_own_ttl_is_stale(status):
    from rubik_core.xray import config as xray_config

    with _session() as db:
        snap = _xray(
            db, updated_minutes_ago=xray_config.DEEP_RUN_TTL_MINUTES + 5, status=status
        )
        assert [i.id for i in sw.xray_deep_stale(db)] == [snap]


@pytest.mark.parametrize("status", ["ready", "failed"])
def test_a_settled_xray_is_never_stale(status):
    with _session() as db:
        _xray(db, updated_minutes_ago=5_000, status=status)
        assert sw.xray_deep_stale(db) == []


def test_a_fast_xray_is_never_stale():
    """Fast runs are inline and never pending — a stuck one is not this alert."""
    with _session() as db:
        _xray(db, updated_minutes_ago=5_000, kind="fast", status="pending")
        assert sw.xray_deep_stale(db) == []


def test_the_xray_threshold_is_the_features_own_constant():
    """Not a copy. X-Ray's 409 reaper already defines when a run is dead; two
    numbers would let the alert and the product disagree."""
    from rubik_core.xray import config as xray_config

    with _session() as db:
        _xray(db, updated_minutes_ago=xray_config.DEEP_RUN_TTL_MINUTES - 1)
        assert sw.xray_deep_stale(db) == []
        _xray(db, updated_minutes_ago=xray_config.DEEP_RUN_TTL_MINUTES + 1)
        assert len(sw.xray_deep_stale(db)) == 1


# ── Running every probe ──────────────────────────────────────────────────────


def test_find_stale_work_reports_every_feature_at_once():
    from rubik_core.xray import config as xray_config

    with _session() as db:
        _deep_prep(db, minutes_ago=sw.DEEP_PREP_STALE_MINUTES + 10)
        _xray(db, updated_minutes_ago=xray_config.DEEP_RUN_TTL_MINUTES + 10)
        features = {item.feature for item in sw.find_stale_work(db)}

    assert features == {"deep_prep", "xray_deep"}


def test_one_broken_probe_does_not_hide_the_others(monkeypatch, caplog):
    """A probe that raises must not read as "nothing to report" for the rest."""

    def boom(*_a, **_k):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(sw, "PROBES", (boom, sw.deep_prep_stale))
    with _session() as db:
        _deep_prep(db, minutes_ago=5_000)
        with caplog.at_level(logging.ERROR):
            found = sw.find_stale_work(db)

    assert [i.feature for i in found] == ["deep_prep"]
    assert "is NOT covered by this run" in "\n".join(
        r.getMessage() for r in caplog.records
    )


# ── "Nothing waiting" vs "could not check" ───────────────────────────────────


def test_a_clean_sweep_returns_an_empty_list_not_none(caplog):
    with caplog.at_level(logging.INFO):
        assert sw.report_stale_work() == []
    assert "no stale background work" in "\n".join(
        r.getMessage() for r in caplog.records
    )


def test_a_sweep_that_cannot_run_returns_none(monkeypatch):
    """None is not an all-clear, and the job turns it into a non-zero exit."""
    import rubik_core.database.db as dbmod

    def boom():
        raise RuntimeError("no database")

    monkeypatch.setattr(dbmod, "SessionLocal", boom)
    assert sw.report_stale_work() is None


def test_the_job_exits_zero_on_a_finding_not_just_on_silence(monkeypatch):
    """Finding stale work is a successful check WITH a finding.

    Exiting non-zero would double-page through the Job-failure alert and teach
    whoever is on the receiving end that this job is flaky.
    """
    from backend.jobs import stale_work_sweep_job as job

    item = sw.StaleItem(
        feature="deep_prep",
        token="DEEP_PREP_DELIVERY_STALE",
        id="abc",
        owner=ASH,
        since=datetime.utcnow(),
        rerun_args="-m,x",
    )
    monkeypatch.setattr(
        "rubik_core.reliability.stale_work.report_stale_work", lambda **_k: [item]
    )
    assert job.main([]) == job.EXIT_OK


def test_the_job_exits_nonzero_when_it_could_not_check(monkeypatch):
    """So the Job-failure alert fires rather than the silence being ours."""
    from backend.jobs import stale_work_sweep_job as job

    monkeypatch.setattr(
        "rubik_core.reliability.stale_work.report_stale_work", lambda **_k: None
    )
    assert job.main([]) == job.EXIT_ERROR


def test_the_report_logs_the_token_and_the_rerun_args(caplog):
    """The alert needs the count; whoever opens it needs the args."""
    with _session() as db:
        run_id = _deep_prep(db, minutes_ago=5_000)

    with caplog.at_level(logging.ERROR):
        sw.report_stale_work()

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "DEEP_PREP_DELIVERY_STALE" in logged
    assert run_id in logged
    assert "rerun=-m,backend.jobs.deep_prep_job" in logged


def test_a_bad_week_cannot_become_an_unbounded_log_flood():
    with _session() as db:
        for _ in range(6):
            _deep_prep(db, minutes_ago=5_000)
        assert len(sw.deep_prep_stale(db, limit=3)) == 3


# ── Guide RAG writes: the privacy half ───────────────────────────────────────


def test_a_just_retracted_guide_is_not_yet_stale():
    """The purge is dispatched on unpublish; give it time to land."""
    with _session() as db:
        _guide(db, minutes_ago=2, published=False, rag_ingested=True, unpublished=True)
        assert sw.guide_purge_stale(db) == []


def test_a_retracted_guide_still_in_the_corpus_is_stale():
    """The privacy defect: out of the app, still answerable in Chat."""
    with _session() as db:
        doc = _guide(
            db,
            minutes_ago=sw.GUIDE_RAG_STALE_MINUTES + 10,
            published=False,
            rag_ingested=True,
            unpublished=True,
        )
        assert [i.id for i in sw.guide_purge_stale(db)] == [doc]


def test_a_still_published_guide_is_never_purge_stale():
    """Nobody asked for it to be gone."""
    with _session() as db:
        _guide(db, minutes_ago=5_000, published=True, rag_ingested=True)
        assert sw.guide_purge_stale(db) == []


def test_a_retracted_guide_that_was_purged_is_not_stale():
    """``rag_ingested`` flipping to false IS the proof of removal — the job
    clears it only on a proven delete."""
    with _session() as db:
        _guide(
            db, minutes_ago=5_000, published=False, rag_ingested=False, unpublished=True
        )
        assert sw.guide_purge_stale(db) == []


def test_the_purge_item_carries_purge_args_and_the_subject():
    with _session() as db:
        doc = _guide(
            db, minutes_ago=5_000, published=False, rag_ingested=True, unpublished=True
        )
        item = sw.guide_purge_stale(db)[0]

    assert item.rerun_args == (
        f"-m,backend.jobs.smart_teams_guide_job,--document-id,{doc},--purge"
    )
    assert item.owner == ASH


# ── Guide RAG writes: the half-shipped half ──────────────────────────────────


def test_a_published_guide_not_in_the_corpus_is_stale():
    with _session() as db:
        doc = _guide(
            db,
            minutes_ago=sw.GUIDE_RAG_STALE_MINUTES + 10,
            published=True,
            rag_ingested=False,
        )
        assert [i.id for i in sw.guide_ingest_stale(db)] == [doc]


def test_a_published_guide_already_in_the_corpus_is_not_stale():
    with _session() as db:
        _guide(db, minutes_ago=5_000, published=True, rag_ingested=True)
        assert sw.guide_ingest_stale(db) == []


def test_a_just_published_guide_is_not_yet_stale():
    with _session() as db:
        _guide(db, minutes_ago=2, published=True, rag_ingested=False)
        assert sw.guide_ingest_stale(db) == []


def test_a_published_guide_cannot_even_exist_unapproved():
    """The probe filters on ``approved_at`` as belt-and-braces; the DATABASE is
    what guarantees it.

    ``ck_st_document_published_needs_approval`` exists because publication IS
    the consent — a guide going company-visible with ``approved_at`` NULL is the
    one failure in this feature that cannot be walked back after the fact. Worth
    pinning here too: if that constraint were dropped, the probe would start
    reporting rows ``--ingest`` is going to refuse, and whoever opened the alert
    would be sent to a job that cannot help them.
    """
    from sqlalchemy.exc import IntegrityError

    with _session() as db, pytest.raises(IntegrityError):
        _guide(
            db, minutes_ago=5_000, published=True, rag_ingested=False, approved=False
        )


def test_the_ingest_item_carries_ingest_args():
    with _session() as db:
        doc = _guide(db, minutes_ago=5_000, published=True, rag_ingested=False)
        item = sw.guide_ingest_stale(db)[0]

    assert item.rerun_args == (
        f"-m,backend.jobs.smart_teams_guide_job,--document-id,{doc},--ingest"
    )


def test_the_two_guide_halves_do_not_report_each_other():
    """Different tokens, different severities, different recovery commands —
    one showing up under the other's alert would send the wrong runbook."""
    with _session() as db:
        purge_doc = _guide(
            db, minutes_ago=5_000, published=False, rag_ingested=True, unpublished=True
        )
        # Two different people: uq_st_document_cache makes a subject's
        # (kind, subject_key, profile_version_key) triple unique, so one
        # person cannot hold both halves at the same version.
        ingest_doc = _guide(
            db,
            minutes_ago=5_000,
            published=True,
            rag_ingested=False,
            subject="kim@acme.example",
        )
        assert [i.id for i in sw.guide_purge_stale(db)] == [purge_doc]
        assert [i.id for i in sw.guide_ingest_stale(db)] == [ingest_doc]


def test_the_privacy_defect_is_reported_before_everything_else():
    """Probe order is triage order: a retracted guide still answerable in Chat
    outranks a missing result when somebody reads a morning's findings."""
    with _session() as db:
        _deep_prep(db, minutes_ago=5_000)
        _guide(
            db, minutes_ago=5_000, published=False, rag_ingested=True, unpublished=True
        )
        features = [item.feature for item in sw.find_stale_work(db)]

    assert features[0] == "guide_purge"


# ── Age ceilings: when work stops being a pending obligation ─────────────────


def test_a_deep_prep_older_than_its_ceiling_is_not_reported():
    """An alert that can never be cleared is one everybody filters.

    This is the case the first live sweep hit: three real Deep Prep losses from
    two months earlier, permanently unrecoverable, which without a ceiling would
    have re-fired every night for ever.
    """
    with _session() as db:
        _deep_prep(db, minutes_ago=sw.DEEP_PREP_MAX_AGE_DAYS * 24 * 60 + 60)
        assert sw.deep_prep_stale(db) == []


def test_a_deep_prep_just_inside_its_ceiling_is_still_reported():
    """The ceiling must not quietly swallow recoverable work."""
    with _session() as db:
        run_id = _deep_prep(db, minutes_ago=sw.DEEP_PREP_MAX_AGE_DAYS * 24 * 60 - 60)
        assert [i.id for i in sw.deep_prep_stale(db)] == [run_id]


def test_an_xray_older_than_its_ceiling_is_not_reported():
    """A two-month-old stalled snapshot would be re-requested, not resumed."""
    with _session() as db:
        _xray(db, updated_minutes_ago=sw.XRAY_MAX_AGE_DAYS * 24 * 60 + 60)
        assert sw.xray_deep_stale(db) == []


def test_the_privacy_probe_has_NO_ceiling_however_old():
    """The one probe that must never expire.

    A retracted guide still readable in Chat gets MORE serious with age, not
    less. A ceiling here would mean the longer somebody's retracted guide stayed
    exposed, the quieter the system became about it — so this asserts both the
    declared constant and the behaviour, since either alone could drift.
    """
    assert sw.GUIDE_PURGE_MAX_AGE_DAYS is None

    with _session() as db:
        doc = _guide(
            db,
            minutes_ago=400 * 24 * 60,
            published=False,
            rag_ingested=True,
            unpublished=True,
        )
        assert [i.id for i in sw.guide_purge_stale(db)] == [doc]


def test_an_ancient_unscored_interview_is_still_reported():
    """Interview answers do not go stale — which is why nobody had to retake
    anything in September. Recovery is always worth it, so no ceiling."""
    assert sw.SMART_TEAMS_MAX_AGE_DAYS is None

    from rubik_core.database.smart_teams import SmartTeamsInterview

    with _session() as db:
        row = SmartTeamsInterview(
            id=uuid.uuid4().hex,
            user_email=ASH,
            status="completed",
            sections_completed=[],
            answers={},
            completed_at=datetime.utcnow() - timedelta(days=400),
        )
        db.add(row)
        db.commit()
        assert [i.id for i in sw.smart_teams_stale(db)] == [row.id]


def test_an_ancient_missing_guide_ingest_is_still_reported():
    """A published guide absent from the corpus stays wrong until fixed."""
    assert sw.GUIDE_INGEST_MAX_AGE_DAYS is None

    with _session() as db:
        doc = _guide(db, minutes_ago=400 * 24 * 60, published=True, rag_ingested=False)
        assert [i.id for i in sw.guide_ingest_stale(db)] == [doc]


# ── The drift guard that keeps all of this wired ─────────────────────────────


@pytest.mark.parametrize("feature,token", sorted(sw.TOKENS.items()))
def test_every_token_has_a_policy_in_the_monitoring_script(feature, token):
    """A token with no policy is a feature that is quietly uncovered.

    Nothing else would notice: the sweep keeps logging, and the absent policy
    keeps not existing.
    """
    script = (REPO_ROOT / "scripts" / "setup_monitoring.sh").read_text()
    assert token in script, (
        f"{feature!r} can emit {token!r} but scripts/setup_monitoring.sh has no "
        f"policy matching it, so nobody is ever told about stale {feature} work."
    )


@pytest.mark.parametrize("feature,token", sorted(sw.TOKENS.items()))
def test_every_token_metric_is_pinned_to_the_jobs_own_log_stream(feature, token):
    """A token metric that is not pinned to stdout/stderr can count itself.

    The Deep Prep alert once re-fired every day with nothing stale: its
    filter was ``resource.type="cloud_run_job" AND "<TOKEN>"``, a
    bare string in the Logging query language matches case-insensitively over
    the WHOLE entry, and Cloud Monitoring's own open/resolve activity entries
    carry ``resource.type=cloud_run_job`` plus a label naming the metric
    (``…/cubie_deep_prep_delivery_stale …``), which contains the token. So the
    metric counted the alert's own resolution, which re-opened the alert. Any
    token that is a substring of its metric's name has the loop latent in it;
    the ``${JOB_LOG_SCOPE}`` logName pin is what makes it impossible, and this
    test is what keeps the pin on.
    """
    script = (REPO_ROOT / "scripts" / "setup_monitoring.sh").read_text()
    filter_lines = [
        line.strip()
        for line in script.splitlines()
        if line.strip().startswith('"resource.type=\\"cloud_run_job\\"')
        and token in line
    ]
    assert filter_lines, (
        f"no cloud_run_job log-metric filter for {token!r} in setup_monitoring.sh"
    )
    for line in filter_lines:
        assert "${JOB_LOG_SCOPE}" in line, (
            f"the {feature!r} metric filter is not pinned to the Job's own log "
            f"stream, so it can count Cloud Monitoring's activity entries about "
            f"itself and re-fire for ever: {line}"
        )


def test_every_probe_declares_a_token_that_tokens_knows_about():
    """``TOKENS`` is what the guard above iterates, so a probe missing from it
    would skip the check that proves it is covered."""
    from rubik_core.xray import config as xray_config

    with _session() as db:
        _deep_prep(db, minutes_ago=5_000)
        _xray(db, updated_minutes_ago=xray_config.DEEP_RUN_TTL_MINUTES + 10)
        for item in sw.find_stale_work(db):
            assert sw.TOKENS.get(item.feature) == item.token

"""Ingestion Council — adversarial multi-agent QA over the overnight/weekly
ingestions.

Hermetic: the council/evidence tests run on their own in-memory SQLite engine
(never the app's DB) and stub the single LLM seam, so the whole 5-auditor →
peer-review → chairman pipeline runs offline and deterministically. The endpoint
tests mirror the UC-40 scheduler pattern (auth gate + patched worker).
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.main import app
from rubik_core.brand import PRODUCT_NAME
from rubik_core.config import settings
from rubik_core.database.models import (
    AgentRecommendation,
    Asset,
    Base,
    HomeCard,
    MeetingParticipant,
    User,
)
from rubik_core.ingestion_council import (
    get_verdict,
    latest_verdicts,
    persist_verdict,
    render_html,
    run_council,
)
from rubik_core.ingestion_council.evidence import gather_evidence
from rubik_core.ingestion_council.models import (
    CouncilVerdict,
    Finding,
    Health,
    RunKind,
)

# Pinned "now" so the evidence windows are deterministic.
NOW = datetime(2026, 7, 9, 8, 0, 0)

client = TestClient(app)


@pytest.fixture
def db():
    """A throwaway in-memory DB with the full schema — isolated from the app's
    engine so a test can never touch dev Cloud SQL."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _mk_user(db, email, dept="sales", is_exec=False):
    # Signed in: the council counts the batch's own roster, which leaves out
    # people nobody would read a bundle for (never signed in, no Slack DM).
    db.add(
        User(
            email=email,
            id=email,
            name=email.split("@")[0],
            department=dept,
            is_exec=is_exec,
            last_login=NOW,
        )
    )


def _check(evidence, name_substr):
    return next(c for c in evidence.cross_checks if name_substr in c.name)


def test_council_never_samples_a_recommendation_from_a_foreign_deal(db, monkeypatch):
    from backend.routers import agents

    monkeypatch.setattr(agents, "visible_hubspot_deal_ids", lambda ids: {"deal-owned"})
    for deal_id, text in (
        ("deal-owned", "Owned deal needs follow up"),
        ("deal-foreign", "Foreign private customer details"),
    ):
        db.add(
            AgentRecommendation(
                id=deal_id,
                user_email="rep@acme.example",
                type="deal_priority",
                lead_id=deal_id,
                lead_name=deal_id,
                recommendation_text=text,
                evidence={},
                confidence=0.9,
                source_metadata={
                    "plugin": "multi_source_priorities",
                    "run_id": "run-1",
                    "primary_entity_kind": "deal",
                },
                sources=[{"source": "hubspot", "entity_refs": {"deal_id": deal_id}}],
                created_at=NOW,
            )
        )
    db.commit()
    evidence = gather_evidence(db, [RunKind.OVERNIGHT_SALES], now=NOW)[0]
    assert evidence.stats["recommendations_written"] == 1
    assert evidence.samples[0]["text_present"] is True
    assert "Owned deal needs follow up" not in str(evidence)
    assert "Foreign private customer details" not in str(evidence)


# ── Health rollup (pure) ───────────────────────────────────────────────────────


def test_health_worst_and_coerce():
    assert Health.worst(["pass", "warn", "fail"]) is Health.FAIL
    assert Health.worst(["pass", "pass"]) is Health.PASS
    assert Health.coerce("FAIL") is Health.FAIL
    assert Health.coerce("nonsense", Health.PASS) is Health.PASS


# ── Evidence gatherers surface the silent-drift failure classes ────────────────


@pytest.mark.usefixtures("roster_admits_acme_example")
def test_evidence_flags_coverage_gap_and_acl_leaks(
    db, monkeypatch, set_test_workspace_domains
):
    # Domain guard mirrors the batch: only @acme.example users count; test fixtures
    # like boss@example.com are excluded (the 7/9 false-positive source).
    set_test_workspace_domains("acme.example")
    _mk_user(db, "alice@acme.example")
    _mk_user(db, "bob@acme.example")
    _mk_user(db, "boss@example.com")  # off-domain fixture → must NOT count as active
    db.commit()

    # Priorities: only alice got a briefing card → bob is a silent drop.
    db.add(
        HomeCard(
            id="c1",
            user_email="alice@acme.example",
            date="2026-07-09",
            category="daily_briefing",
            content_md="x",
            metadata_json={"priorities": [{"id": 1}]},
            created_at=NOW,
        )
    )
    # Transcripts: t1 has NO ACL row (unqueryable); t2 is wrongly dept-stamped.
    db.add(
        Asset(
            doc_id="t1",
            owner_email="alice@acme.example",
            status="approved",
            topic="Meeting Transcripts",
            department=None,
            processing=False,
            created_at=NOW,
            original_filename="Standup.txt",
            allowed_emails=[],
        )
    )
    db.add(
        Asset(
            doc_id="t2",
            owner_email="alice@acme.example",
            status="approved",
            topic="Meeting Transcripts",
            department="sales",
            processing=False,
            created_at=NOW,
            original_filename="Sync.txt",
            allowed_emails=["alice@acme.example"],
        )
    )
    db.add(
        MeetingParticipant(
            doc_id="t2",
            participant_email="alice@acme.example",
            meeting_id="m2",
            created_at=NOW,
        )
    )
    db.commit()

    evs = {
        e.kind: e
        for e in gather_evidence(
            db, [RunKind.PRIORITIES_DAILY, RunKind.TRANSCRIPT_SYNC], now=NOW
        )
    }

    pri = evs[RunKind.PRIORITIES_DAILY]
    assert pri.stats["active_users"] == 2  # alice + bob; boss@example.com excluded
    assert "boss" not in pri.stats["missing_sample"]  # fixture never counts as missing
    assert pri.stats["users_with_cards"] == 1
    assert _check(pri, "every active user has a daily briefing card").ok is False

    tr = evs[RunKind.TRANSCRIPT_SYNC]
    assert tr.stats["transcripts_ingested"] == 2
    assert tr.stats["missing_acl_rows"] == 1
    assert tr.stats["department_stamped"] == 1
    assert _check(tr, "ACL participant row").ok is False
    assert _check(tr, "stamped with a department").ok is False


def test_transcript_evidence_omits_chunk_stats_in_gcp(db, monkeypatch):
    # In GCP the vector store is Vertex RAG (server-side chunking), so the local
    # chunks table is empty by design — the council must NOT report/flag
    # zero_chunk_rows there (a "100% unretrievable" false alarm).
    monkeypatch.setattr(settings, "APP_MODE", "gcp", raising=False)
    _mk_user(db, "alice@acme.example")
    db.commit()
    db.add(
        Asset(
            doc_id="g1",
            owner_email="alice@acme.example",
            status="approved",
            topic="Meeting Transcripts",
            department=None,
            processing=False,
            created_at=NOW,
            original_filename="Call.txt",
            allowed_emails=["alice@acme.example"],
        )
    )
    db.add(
        MeetingParticipant(
            doc_id="g1",
            participant_email="alice@acme.example",
            meeting_id="m1",
            created_at=NOW,
        )
    )
    db.commit()

    (tr,) = gather_evidence(db, [RunKind.TRANSCRIPT_SYNC], now=NOW)
    assert tr.stats["transcripts_ingested"] == 1
    assert "zero_chunk_rows" not in tr.stats  # omitted in GCP
    # A healthy transcript (ACL present, dept NULL, not failed/stuck) → all checks pass.
    assert all(c.ok for c in tr.cross_checks)


def test_eos_evidence_flags_no_corpus_and_no_content(db, monkeypatch):
    # No corpus configured + empty source tables → two failed checks.
    monkeypatch.setattr(settings, "VERTEX_RAG_CORPUS_ID", None, raising=False)
    monkeypatch.setattr(settings, "CUBIE_DISABLE_EOS_INGEST", False, raising=False)
    (ev,) = gather_evidence(db, [RunKind.EOS_WEEKLY], now=NOW)
    assert _check(ev, "RAG corpus is configured").ok is False
    assert _check(ev, "content to ingest").ok is False
    assert _check(ev, "EOS ingestion is enabled").ok is True


# ── Full council with a stubbed LLM (offline) ──────────────────────────────────


def _fake_llm(prompt, *, model_key="gemini-flash", temperature=0.3, fallback=None):
    """Route by prompt shape: chairman vs peer-review vs auditor."""
    if f"Chairman of {PRODUCT_NAME}" in prompt:
        # Deliberately optimistic — the deterministic floor must override it.
        return {
            "overall_health": "pass",
            "headline": "Chairman says all clear",
            "top_risks": [
                {
                    "severity": "critical",
                    "title": "Transcripts unqueryable",
                    "detail": "1 transcript has no ACL row",
                    "run_kind": "transcript_sync",
                }
            ],
            "one_action": "Backfill missing ACL rows",
            "agreements": ["ingestion ran"],
            "clashes": [],
            "blind_spots": ["retroactive ACL backfill"],
            "per_kind_health": {"transcript_sync": "fail", "priorities_daily": "warn"},
        }
    if "ingestion-QA analyses" in prompt:
        return {
            "strongest": "A",
            "biggest_blind_spot": "missed the coverage gap",
            "what_all_missed": "cost trend",
        }
    # An auditor. Make the ACL watchdog fail so the panel's worst is FAIL.
    health = "fail" if "ACL & Cost Watchdog" in prompt else "warn"
    return {
        "health": health,
        "summary": "auditor bottom line",
        "confidence": 0.7,
        "findings": [
            {
                "severity": "critical" if health == "fail" else "warning",
                "title": "an issue",
                "detail": "cited detail",
                "run_kind": "transcript_sync",
            }
        ],
    }


def test_run_council_floors_health_to_worst_auditor(db):
    _mk_user(db, "alice@acme.example")
    db.commit()

    with (
        patch("rubik_core.ingestion_council.auditors.council_llm", _fake_llm),
        patch("rubik_core.ingestion_council.council.council_llm", _fake_llm),
    ):
        verdict = run_council(db, "nightly", now=NOW)

    # Chairman claimed "pass", but an auditor said "fail" → floored to FAIL.
    assert verdict.overall_health is Health.FAIL
    assert len(verdict.auditor_findings) == 5
    assert len(verdict.peer_reviews) == 5
    # nightly reviews AI-Priorities + transcript-sync; overnight_sales is retired.
    assert {e.kind for e in verdict.evidence} == {
        RunKind.PRIORITIES_DAILY,
        RunKind.TRANSCRIPT_SYNC,
    }
    assert verdict.headline
    assert verdict.top_risks


# ── Persistence + report rendering ─────────────────────────────────────────────


def test_persist_and_render(db):
    verdict = CouncilVerdict(
        scope="nightly",
        overall_health=Health.FAIL,
        headline="Rough night for ingestion",
        generated_at="2026-07-09T08:00:00",
        top_risks=[
            Finding(
                "critical",
                "ACL missing",
                "2 transcripts unqueryable",
                "transcript_sync",
            )
        ],
        one_action="Backfill the ACL rows",
    )
    row = persist_verdict(db, verdict)
    assert row.id
    assert row.overall_health == "fail"
    assert "Rough night for ingestion" in row.report_html
    assert get_verdict(db, row.id) is not None
    assert len(latest_verdicts(db)) == 1

    html = render_html(verdict)
    assert "Ingestion Council" in html
    assert "Backfill the ACL rows" in html
    assert "ACTION REQUIRED" in html  # FAIL → the banner word


# ── Delivery degrades without a Slack token ────────────────────────────────────


def test_delivery_noop_without_bot_token(db):
    from backend.agents.ingestion_council import deliver_to_slack

    verdict = CouncilVerdict(
        scope="nightly",
        overall_health=Health.WARN,
        headline="h",
        generated_at="t",
    )
    bot = MagicMock()
    bot.is_available = False
    with patch("backend.services.slack_service.SlackBotClient", return_value=bot):
        result = deliver_to_slack(db, verdict, None, None)
    assert result.slack_available is False
    assert result.delivered == 0


# ── Endpoint auth gate ─────────────────────────────────────────────────────────


def test_endpoint_403_without_scheduler_key():
    assert client.post("/api/jobs/ingestion-council/run").status_code == 403


def test_endpoint_runs_council_with_scheduler_key(monkeypatch):
    monkeypatch.setattr(settings, "SCHEDULER_API_KEY", "secret-key")
    with patch(
        "backend.routers.jobs.launch_module_execution",
        return_value={"name": "executions/council-test"},
    ) as mock_launch:
        resp = client.post(
            "/api/jobs/ingestion-council/run?scope=nightly",
            headers={"X-Scheduler-Key": "secret-key", "X-Cubie-Workspace": "test"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "dispatched"
    assert body["scope"] == "nightly"
    assert body["execution"] == "executions/council-test"
    mock_launch.assert_called_once_with(
        "backend.agents.ingestion_council", ["--scope", "nightly"]
    )


def test_runs_list_403_without_auth():
    assert client.get("/api/jobs/ingestion-council/runs").status_code == 403


def test_saved_council_reports_hide_legacy_and_revoked_crm_prose(db, monkeypatch):
    from backend.routers import agents
    from backend.routers import jobs as jobs_router
    from rubik_core.tenancy.context import current_workspace

    monkeypatch.setattr(agents, "visible_hubspot_deal_ids", lambda _ids: {"deal-a"})
    monkeypatch.setattr(
        jobs_router,
        "_is_authorized_scheduler_caller",
        lambda _request, _key, _credentials: (True, "admin"),
    )
    workspace = current_workspace()
    secret = "Private revoked CRM deal B pricing"
    legacy = persist_verdict(
        db,
        CouncilVerdict(
            scope="priorities-gate",
            overall_health=Health.WARN,
            headline=secret,
            generated_at="2026-10-30T08:00:00Z",
        ),
    )
    revoked = persist_verdict(
        db,
        CouncilVerdict(
            scope="priorities-gate",
            overall_health=Health.WARN,
            headline=secret,
            generated_at="2026-10-30T08:00:00Z",
            crm_provenance={
                "workspace": workspace,
                "deal_ids": ["deal-a", "deal-b"],
                "unprovable": False,
            },
        ),
    )
    fresh = persist_verdict(
        db,
        CouncilVerdict(
            scope="priorities-gate",
            overall_health=Health.PASS,
            headline="Owned deal A report",
            generated_at="2026-10-30T08:00:00Z",
            crm_provenance={
                "workspace": workspace,
                "deal_ids": ["deal-a"],
                "unprovable": False,
            },
        ),
    )
    listed = jobs_router.list_ingestion_council_runs(
        MagicMock(), scope=None, limit=20, db=db, credentials=None
    )
    titles = {item["id"]: item["headline"] for item in listed}
    assert secret not in str(titles)
    assert titles[fresh.id] == "Owned deal A report"
    for hidden in (legacy, revoked):
        with pytest.raises(HTTPException) as denied:
            jobs_router.get_ingestion_council_report(
                hidden.id, MagicMock(), db=db, credentials=None
            )
        assert denied.value.status_code == 404
        assert secret not in str(denied.value.detail)
    shown = jobs_router.get_ingestion_council_report(
        fresh.id, MagicMock(), db=db, credentials=None
    )
    assert shown.status_code == 200
    assert b"Owned deal A report" in shown.body


def test_priorities_gate_verdict_stamps_every_source_bundle(monkeypatch):
    from backend.routers import agents
    from rubik_core.ingestion_council import quality_gate
    from rubik_core.ingestion_council.persist import cached_verdict_visible

    records = [
        quality_gate.UserBundleRecord(
            email=f"rep-{deal_id}@acme.example",
            date="2026-10-30",
            has_card=True,
            priorities=[],
            generation_status="no_signals",
            crm_deal_ids={deal_id},
        )
        for deal_id in ("deal-a", "deal-b")
    ]
    monkeypatch.setattr(quality_gate, "gate_mode", lambda: "observe")
    monkeypatch.setattr(quality_gate, "gather_user_bundle_records", lambda _db: records)
    verdict = quality_gate.run_gate(db=None)
    assert verdict.crm_provenance["deal_ids"] == ["deal-a", "deal-b"]
    monkeypatch.setattr(agents, "visible_hubspot_deal_ids", lambda _ids: {"deal-a"})
    assert not cached_verdict_visible(verdict.to_dict())

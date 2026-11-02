"""UC-46 — the scan pipeline end to end, with HubSpot and the LLM stubbed.

Covers:
  - only the caller's own transcripts are enumerated (ACL is the service's job,
    so we assert the scan asks for them the ACL-safe way and honours the result)
  - internal-only meetings are skipped before any LLM call is made
  - a grounded suggestion is persisted with `before` taken from HubSpot
  - a malformed / exploding LLM never crashes the run
  - the run row records honest stats either way
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from rubik_core.ai_providers.registry import IncompatibleModelRoute, ModelRoute
from rubik_core.ai_providers.runtime import ModelTextResponse, ProviderRequestError
from rubik_core.config import settings
from rubik_core.crm_sync import hubspot_context, run_state, service
from rubik_core.crm_sync import llm as crm_llm
from rubik_core.database.models import Base, CrmSyncRun, CrmSyncSuggestion
from rubik_core.tenancy.hubspot_config import HubSpotConnectorConfig

AE = "ae@acme.example"
PIPELINE = "default"
NOW = datetime(2026, 8, 10, tzinfo=timezone.utc)

TRANSCRIPT = (
    "Bo Chen: My direct line is 555-0143 now.\nAE: Got it, I'll update our records.\n"
)


@dataclass
class _Record:
    doc_id: str = "doc-1"
    meeting_id: str = "fathom-1"
    owner_email: str = AE
    original_filename: str | None = "Acme call"
    source_path: str | None = "gs://bucket/doc-1.txt"
    created_at: datetime = field(default_factory=lambda: datetime(2026, 8, 9))
    participants: list[str] = field(default_factory=lambda: [AE, "bo@acmecorp.alt"])
    is_owner: bool = True


@pytest.fixture
def Session(monkeypatch):
    from rubik_core.crm_sync import hubspot_context

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(service, "SessionLocal", factory)
    monkeypatch.setattr(run_state, "SessionLocal", factory)
    monkeypatch.setattr(
        service,
        "get_hubspot_config",
        lambda: HubSpotConnectorConfig(pipeline_ids=(PIPELINE,)),
    )
    monkeypatch.setattr(
        hubspot_context,
        "get_hubspot_config",
        lambda: HubSpotConnectorConfig(pipeline_ids=(PIPELINE,)),
    )
    monkeypatch.setattr(settings, "CRM_SYNC_CONFIDENCE_MIN", 0.5)
    return factory


def _transcripts(records, text=TRANSCRIPT) -> MagicMock:
    svc = MagicMock()
    svc.list_transcripts_for_signals.return_value = records
    svc.read_transcript_text.side_effect = lambda r, **_: text
    return svc


def _hubspot() -> MagicMock:
    client = MagicMock()
    client.owner_directory.return_value = {"77": {"email": AE, "name": "A E"}}
    client.deal_pipeline_stages.return_value = {
        PIPELINE: {"appointmentscheduled": "Discovery"}
    }

    def search_all(object_type, filters, properties, *a, **k):
        if object_type == "contacts":
            return [
                {
                    "id": "c1",
                    "properties": {"email": "bo@acmecorp.alt", "phone": "555-0100"},
                }
            ]
        if object_type == "companies":
            return [
                {"id": "co1", "properties": {"name": "Acme", "domain": "acmecorp.alt"}}
            ]
        return []

    client.search_all.side_effect = search_all
    client.fully_owned_deal_associations.side_effect = lambda _object_type, ids: {
        item: ["owned-deal"] for item in ids
    }
    return client


def test_shared_portal_record_fields_never_enter_crm_sync_prompt(Session):
    hubspot = _hubspot()
    hubspot.search_all.side_effect = lambda object_type, *_a, **_k: (
        [
            {
                "id": "foreign-contact",
                "properties": {"email": "bo@acmecorp.alt", "phone": "B-private-phone"},
            }
        ]
        if object_type == "contacts"
        else [
            {
                "id": "mixed-company",
                "properties": {"name": "B-private-company", "domain": "acmecorp.alt"},
            }
        ]
        if object_type == "companies"
        else []
    )
    hubspot.fully_owned_deal_associations.side_effect = None
    hubspot.fully_owned_deal_associations.return_value = {}
    seen_prompts: list[str] = []

    def extract(_system, prompt):
        seen_prompts.append(prompt)
        return {"suggestions": []}

    run = run_state.create_run(AE)
    service.run_scan(
        run["run_id"],
        AE,
        since=NOW - timedelta(days=7),
        transcript_service=_transcripts([_Record()]),
        hubspot_client=hubspot,
        llm=extract,
    )

    assert len(seen_prompts) == 1
    assert "B-private-phone" not in seen_prompts[0]
    assert "B-private-company" not in seen_prompts[0]
    assert "foreign-contact" not in seen_prompts[0]
    assert "mixed-company" not in seen_prompts[0]


def test_crm_sync_context_makes_no_hubspot_call_without_allowed_pipeline(
    Session, monkeypatch
):
    monkeypatch.setattr(hubspot_context, "get_hubspot_config", HubSpotConnectorConfig)
    hubspot = _hubspot()

    assert (
        hubspot_context.fetch_contacts_for_emails(
            hubspot, ["bo@acmecorp.alt"], properties=["email"]
        )
        == []
    )
    assert hubspot_context.fetch_companies_for_domains(hubspot, ["acmecorp.alt"]) == []
    hubspot.search_all.assert_not_called()
    hubspot.fully_owned_deal_associations.assert_not_called()


_GOOD_OUTPUT = {
    "suggestions": [
        {
            "change_type": "update_contact",
            "hubspot_object_id": "c1",
            "after": {"phone": "555-0143"},
            "evidence": {"quote": "My direct line is 555-0143", "speaker": "Bo Chen"},
            "confidence": 0.92,
            "rationale": "He gave a new direct line.",
        }
    ]
}


def _run(Session, *, records, llm, text=TRANSCRIPT):
    run = run_state.create_run(AE)
    stats = service.run_scan(
        run["run_id"],
        AE,
        since=NOW - timedelta(days=7),
        transcript_service=_transcripts(records, text),
        hubspot_client=_hubspot(),
        llm=llm,
    )
    return run["run_id"], stats


def test_a_grounded_suggestion_is_persisted_with_before_from_hubspot(Session):
    run_id, stats = _run(Session, records=[_Record()], llm=lambda s, p: _GOOD_OUTPUT)

    assert stats["suggestions_created"] == 1
    with Session() as db:
        row = db.query(CrmSyncSuggestion).one()
        assert row.user_email == AE
        assert row.status == "pending"
        assert row.change_type == "update_contact"
        # `before` came from the fetched contact, not from the model.
        assert row.payload["properties"]["phone"] == {
            "before": "555-0100",
            "after": "555-0143",
        }
        assert row.doc_id == "doc-1"
        assert row.meeting_title == "Acme call"
        assert db.query(CrmSyncRun).filter_by(run_id=run_id).one().status == "ready"


def test_the_scan_asks_only_for_transcripts_the_user_may_see(Session):
    svc = _transcripts([_Record()])
    run = run_state.create_run(AE)

    service.run_scan(
        run["run_id"],
        AE,
        since=NOW - timedelta(days=7),
        transcript_service=svc,
        hubspot_client=_hubspot(),
        llm=lambda s, p: {"suggestions": []},
    )

    # The ACL lives in TranscriptService; the scan must go through it rather
    # than querying assets itself.
    svc.list_transcripts_for_signals.assert_called_once()
    assert svc.list_transcripts_for_signals.call_args.kwargs[
        "since"
    ] == NOW - timedelta(days=7)


def test_an_internal_only_meeting_is_skipped_before_any_llm_call(Session):
    internal = _Record(participants=[AE, "colleague@acme.example"])
    calls = []

    _, stats = _run(
        Session,
        records=[internal],
        llm=lambda s, p: calls.append(1) or {"suggestions": []},
    )

    assert calls == []
    assert stats["transcripts_scanned"] == 1
    assert stats["external_meetings"] == 0
    with Session() as db:
        assert db.query(CrmSyncSuggestion).count() == 0


def test_an_exploding_llm_leaves_the_run_ready_with_nothing_queued(Session):
    def boom(system, prompt):
        raise RuntimeError("Vertex is having a day")

    run_id, stats = _run(Session, records=[_Record()], llm=boom)

    assert stats["suggestions_created"] == 0
    with Session() as db:
        # The scan completed; it just found nothing it could act on.
        assert db.query(CrmSyncRun).filter_by(run_id=run_id).one().status == "ready"
        assert db.query(CrmSyncSuggestion).count() == 0


@pytest.mark.parametrize("error_type", [ProviderRequestError, RuntimeError])
def test_client_owned_crm_sync_failure_is_visible_and_never_spends_vertex(
    Session, monkeypatch, caplog, error_type
):
    route = ModelRoute("test-workspace", "crm_sync", "openai", "gpt-6-luna", "client")
    monkeypatch.setattr(service, "model_for", lambda feature: "managed-model")
    monkeypatch.setattr(service, "resolve_model_route", lambda *a, **kw: route)
    monkeypatch.setattr(crm_llm, "resolve_model_route", lambda *a, **kw: route)

    def rejected(*args, **kwargs):
        raise error_type("Selected provider denied PRIVATE-TRANSCRIPT")

    def forbidden_vertex():
        pytest.fail("selected provider fell back to managed Vertex")

    monkeypatch.setattr(crm_llm, "generate_text_on_route", rejected)
    monkeypatch.setattr(crm_llm, "get_genai_client", forbidden_vertex)
    run_id = run_state.create_run(AE)["run_id"]
    with caplog.at_level("WARNING"):
        stats = service.run_scan(
            run_id,
            AE,
            since=NOW - timedelta(days=7),
            transcript_service=_transcripts([_Record()]),
            hubspot_client=_hubspot(),
        )

    assert stats["suggestions_created"] == 0
    with Session() as db:
        row = db.query(CrmSyncRun).filter_by(run_id=run_id).one()
        assert row.status == "failed"
        assert "Selected AI provider" in row.error
        assert "PRIVATE-TRANSCRIPT" not in row.error
        assert db.query(CrmSyncSuggestion).count() == 0
    assert "PRIVATE-TRANSCRIPT" not in caplog.text


def test_selected_crm_sync_skips_one_context_error_and_continues(Session, monkeypatch):
    route = ModelRoute("test-workspace", "crm_sync", "openai", "gpt-6-luna", "client")
    monkeypatch.setattr(service, "model_for", lambda feature: "managed-model")
    monkeypatch.setattr(service, "resolve_model_route", lambda *a, **kw: route)
    monkeypatch.setattr(service, "llm_json", lambda *a, **kw: _GOOD_OUTPUT)
    original = service.build_context_bundle

    def context_for(record_client, *, record, **kwargs):
        if record.doc_id == "first":
            raise RuntimeError("private HubSpot context failure")
        return original(record_client, record=record, **kwargs)

    monkeypatch.setattr(service, "build_context_bundle", context_for)
    run_id = run_state.create_run(AE)["run_id"]
    stats = service.run_scan(
        run_id,
        AE,
        since=NOW - timedelta(days=7),
        transcript_service=_transcripts(
            [_Record(doc_id="first"), _Record(doc_id="second")]
        ),
        hubspot_client=_hubspot(),
    )
    assert stats["suggestions_created"] == 1
    with Session() as db:
        assert db.query(CrmSyncRun).filter_by(run_id=run_id).one().status == "ready"


def test_crm_sync_model_route_carries_system_instruction_and_checks_pinned_route(
    monkeypatch,
):
    route = ModelRoute("test-workspace", "crm_sync", "openai", "gpt-6-luna", "client")
    monkeypatch.setattr(crm_llm, "resolve_model_route", lambda *a, **kw: route)
    seen = []

    def selected(*args, **kwargs):
        seen.append((args, kwargs))
        return ModelTextResponse("{}", "openai", "gpt-6-luna", "client")

    monkeypatch.setattr(crm_llm, "generate_text_on_route", selected)
    monkeypatch.setattr(
        crm_llm,
        "get_genai_client",
        lambda: pytest.fail("selected provider fell back to managed Vertex"),
    )
    assert crm_llm.llm_json("SYSTEM", "PROMPT", model_id="managed", route=route) == {}
    assert seen == [
        ((route, "PROMPT"), {"json_mode": True, "system_instruction": "SYSTEM"})
    ]

    other = ModelRoute("test-workspace", "crm_sync", "meta", "muse-spark-1.3", "client")
    monkeypatch.setattr(crm_llm, "resolve_model_route", lambda *a, **kw: other)
    with pytest.raises(IncompatibleModelRoute, match="route changed"):
        crm_llm.llm_json("SYSTEM", "PROMPT", model_id="managed", route=route)
    assert len(seen) == 1


def test_crm_sync_parser_error_does_not_echo_model_output():
    with pytest.raises(ValueError) as exc:
        crm_llm.safe_json("PRIVATE-TRANSCRIPT { broken json")
    assert "PRIVATE-TRANSCRIPT" not in str(exc.value)


def test_default_crm_sync_keeps_managed_vertex_system_and_temperature(monkeypatch):
    route = ModelRoute(
        "test-workspace", "crm_sync", "vertex_managed", "managed-model", "cubie"
    )
    monkeypatch.setattr(crm_llm, "resolve_model_route", lambda *a, **kw: route)
    seen = []

    def generate_content(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(text='{"suggestions": []}')

    monkeypatch.setattr(
        crm_llm,
        "get_genai_client",
        lambda: SimpleNamespace(
            models=SimpleNamespace(generate_content=generate_content)
        ),
    )
    assert crm_llm.llm_json("SYSTEM", "PROMPT", model_id="managed-model") == {
        "suggestions": []
    }
    assert seen[0]["model"] == "managed-model"
    assert seen[0]["contents"] == "PROMPT"
    assert seen[0]["config"].system_instruction == "SYSTEM"
    assert seen[0]["config"].temperature == 0.1


def test_ungrounded_model_output_is_counted_as_rejected_not_queued(Session):
    hallucinated = {
        "suggestions": [
            {
                "change_type": "update_contact",
                "hubspot_object_id": "c-not-in-context",
                "after": {"phone": "555-0143"},
                "evidence": {"quote": "My direct line is 555-0143"},
                "confidence": 0.9,
            }
        ]
    }

    _, stats = _run(Session, records=[_Record()], llm=lambda s, p: hallucinated)

    assert stats["suggestions_created"] == 0
    assert stats["validation_rejected"] == 1
    with Session() as db:
        assert db.query(CrmSyncSuggestion).count() == 0


def test_an_empty_transcript_is_skipped(Session):
    _, stats = _run(
        Session, records=[_Record()], llm=lambda s, p: _GOOD_OUTPUT, text="   "
    )

    assert stats["suggestions_created"] == 0


def test_truncation_is_recorded_on_the_evidence(Session, monkeypatch):
    monkeypatch.setattr(service, "TRANSCRIPT_MAX_BYTES", len(TRANSCRIPT))

    _run(Session, records=[_Record()], llm=lambda s, p: _GOOD_OUTPUT)

    with Session() as db:
        assert db.query(CrmSyncSuggestion).one().evidence["truncated"] is True


def test_external_attendee_detection_ignores_case_and_the_ae_themselves():
    participants = ["AE@acme.example", "Bo@ACMECORP.alt", "ae@acme.example"]

    assert service._external_attendees(participants, AE) == ["bo@acmecorp.alt"]
    assert service._external_attendees([AE], AE) == []

"""Scheduled ingest Jobs must fail on work errors and succeed on ordinary skips."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def test_l10_summary_job_returns_failure_only_for_failed_outcomes(monkeypatch):
    from backend.jobs import l10_summary_email_job
    from backend.services.l10_summary_email import SweepReport

    monkeypatch.setattr("backend.jobs.bootstrap.bootstrap_job_workspace", lambda: True)
    session = MagicMock()
    monkeypatch.setattr("rubik_core.database.db.SessionLocal", lambda: session)

    def report(status):
        outcome = SimpleNamespace(
            status=status, describe=lambda: status, excluded={}, error=None
        )
        return SweepReport(outcomes=[outcome], dry_run=False, now=datetime.utcnow())

    monkeypatch.setattr(
        "backend.services.l10_summary_email.sweep_l10_summary_emails",
        lambda *args, **kwargs: report("failed"),
    )
    assert l10_summary_email_job.main([]) == 1
    monkeypatch.setattr(
        "backend.services.l10_summary_email.sweep_l10_summary_emails",
        lambda *args, **kwargs: report("already_sent"),
    )
    assert l10_summary_email_job.main([]) == 0
    session.close.assert_called()


def test_fathom_client_strict_poll_distinguishes_api_error_from_empty(
    monkeypatch, caplog
):
    from rubik_core.integrations.fathom import FathomClient

    client = FathomClient(api_key="test-key")

    def fail(*args, **kwargs):
        raise RuntimeError("Illegal header value: 'DEMO_KEY\nbad'")

    monkeypatch.setattr("rubik_core.integrations.fathom.requests.get", fail)
    assert client.list_meetings() == []
    with pytest.raises(RuntimeError, match="Illegal header value"):
        client.list_meetings(raise_on_error=True)
    assert "DEMO_KEY" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_fathom_failed_asset_is_retried_and_job_status_recovers(
    tmp_path, monkeypatch, caplog
):
    from backend.routers import fathom
    from rubik_core.config import AppMode

    monkeypatch.setattr(fathom.settings, "APP_MODE", AppMode.LOCAL)
    monkeypatch.setattr("rubik_core.tenancy.storage.local_data_dir", lambda: tmp_path)
    monkeypatch.setattr(fathom, "_attach_to_call_capture", lambda meeting: None)
    monkeypatch.setattr(
        fathom,
        "SQLMeetingParticipantsRepository",
        lambda: SimpleNamespace(upsert_participants=lambda *args, **kwargs: None),
    )
    monkeypatch.setattr(
        "rubik_core.meeting_skills.routines.on_transcript_ready",
        lambda doc_id: None,
    )
    client = MagicMock()
    client.api_key = "test-key"
    client.list_meetings.return_value = [{"id": "meeting-1", "title": "Call"}]
    client.build_ingest_text.return_value = "Transcript text"
    client.extract_participants_from_meeting.return_value = ["host@example.com"]
    monkeypatch.setattr(fathom, "FathomClient", lambda: client)

    asset = None

    def register(value):
        nonlocal asset
        asset = value

    def update(doc_id, values):
        for key, value in values.items():
            setattr(asset, key, value)

    monkeypatch.setattr(
        fathom,
        "dam_registry",
        SimpleNamespace(
            get_asset=lambda doc_id: asset,
            register_asset=register,
            update_asset=update,
        ),
    )
    processor = MagicMock()
    processor.process_file.return_value = {
        "result": SimpleNamespace(
            success=False, error="Illegal header value: 'DEMO_KEY\\nbad'"
        )
    }

    failed = fathom.process_fathom_transcripts_background("host@example.com", processor)
    assert failed.failed == 1
    assert asset.status == "failed"
    assert "DEMO_KEY" not in caplog.text

    processor.process_file.return_value = {"result": SimpleNamespace(success=True)}
    recovered = fathom.process_fathom_transcripts_background(
        "host@example.com", processor
    )
    assert recovered.failed == 0
    assert recovered.ingested == 1
    assert asset.status == "approved"
    assert processor.process_file.call_count == 2


def test_fathom_job_exits_nonzero_on_failed_poll_but_zero_on_skip(monkeypatch):
    from backend.jobs import fathom_sync_job
    from backend.routers import fathom

    monkeypatch.setattr(
        "backend.jobs.bootstrap.bootstrap_process_workspace", lambda: None
    )
    monkeypatch.setattr("backend.routers.ingest.get_processor", lambda: object())
    monkeypatch.setattr(
        fathom,
        "process_fathom_transcripts_background",
        lambda *args, **kwargs: fathom.FathomSyncReport(status="error", failed=1),
    )
    assert fathom_sync_job.main(["--user-email", "host@example.com"]) == 1

    monkeypatch.setattr(
        fathom,
        "process_fathom_transcripts_background",
        lambda *args, **kwargs: fathom.FathomSyncReport(status="empty"),
    )
    assert fathom_sync_job.main(["--user-email", "host@example.com"]) == 0


def test_fathom_background_reports_fetch_failure(monkeypatch):
    from backend.routers import fathom

    client = MagicMock()
    client.api_key = "test-key"
    client.list_meetings.side_effect = RuntimeError("Fathom unavailable")
    monkeypatch.setattr(fathom, "FathomClient", lambda: client)

    report = fathom.process_fathom_transcripts_background("host@example.com", object())
    assert report.status == "error"
    assert report.failed == 1
    client.list_meetings.assert_called_once_with(
        limit=20, include_transcript=True, raise_on_error=True
    )

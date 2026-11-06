"""UC-17.5 — Transcript SignalSource.

Mocks the TranscriptService entirely (no DB / GCS). Covers:
  - transcript outside lookback window → no signal (service filters; we assert
    the source faithfully respects the service's returned set).
  - explicit "Blair will follow up by Friday" → action_item_assigned_to_rep.
  - "we may need to put this on hold" → risk_language with matched_phrase.
  - same transcript yields BOTH signals when both present.
  - rep absent from participants and not the owner → no signal (mock returns
    no records, mirroring the DB-side filter).
  - transcript with no matching phrases → no signal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

REP = "blair@acme.example"
NOW = datetime(2026, 5, 11, 9, 0, tzinfo=timezone.utc)
SINCE = NOW - timedelta(hours=24)


@dataclass
class _Record:
    doc_id: str
    meeting_id: str = "fathom-1"
    owner_email: str = REP
    original_filename: str | None = "Acme Q2 review.txt"
    source_path: str | None = "gs://bucket/workspace/abc.txt"
    created_at: datetime = field(default_factory=lambda: NOW - timedelta(hours=2))
    participants: list[str] = field(default_factory=lambda: [REP, "buyer@acmecorp.alt"])
    is_owner: bool = True


def _svc_mock(*, records: list[_Record], text_by_doc: dict[str, str]) -> MagicMock:
    svc = MagicMock()
    svc.list_transcripts_for_signals.return_value = records
    svc.read_transcript_text.side_effect = lambda r, **_: text_by_doc.get(r.doc_id, "")
    return svc


# ─── Outside-window: service returns nothing ─────────────────────────────────


def test_transcript_outside_lookback_window_emits_no_signal():
    from backend.signals.transcript import TranscriptSignalSource

    src = TranscriptSignalSource(
        transcript_service=_svc_mock(records=[], text_by_doc={})
    )

    assert src.fetch(REP, since=SINCE) == []


# ─── Action item assigned to rep ─────────────────────────────────────────────


def test_action_item_assigned_to_rep_emits_high_severity_signal():
    from backend.signals.transcript import TranscriptSignalSource

    rec = _Record(doc_id="t-1")
    text = (
        "Discussion of Q2 expansion. Blair will follow up by Friday with the "
        "updated pricing sheet. Buyer is excited about the new tier."
    )
    src = TranscriptSignalSource(
        transcript_service=_svc_mock(records=[rec], text_by_doc={"t-1": text}),
    )

    sigs = src.fetch(REP, since=SINCE)

    assert len(sigs) == 1
    s = sigs[0]
    assert s.source == "transcript"
    assert s.signal_type == "action_item_assigned_to_rep"
    assert s.severity_hint == "high"
    assert s.entity_refs["transcript_id"] == "t-1"
    assert s.entity_refs["event_id"] == "fathom-1"
    assert s.entity_refs["contact_email"] == "buyer@acmecorp.alt"
    assert s.entity_refs["company_name"] == "acmecorp"
    assert s.payload["transcript_id"] == "t-1"
    assert s.payload["meeting_title"] == "Acme Q2 review"
    assert s.payload["participants"] == [REP, "buyer@acmecorp.alt"]
    assert "blair" in s.payload["excerpt"].lower()
    assert "Acme Q2 review" in s.summary
    assert len(s.summary) <= 140


# ─── Risk language ───────────────────────────────────────────────────────────


def test_risk_language_emits_signal_with_matched_phrase():
    from backend.signals.transcript import TranscriptSignalSource

    rec = _Record(doc_id="t-2", original_filename="BigCo discovery.txt")
    text = (
        "Procurement is dragging — we may need to put this on hold until next "
        "quarter while we sort out priorities."
    )
    src = TranscriptSignalSource(
        transcript_service=_svc_mock(records=[rec], text_by_doc={"t-2": text}),
    )

    sigs = src.fetch(REP, since=SINCE)

    assert len(sigs) == 1
    s = sigs[0]
    assert s.signal_type == "risk_language"
    assert s.severity_hint == "high"
    assert s.payload["matched_phrase"] == "on hold"
    assert "on hold" in s.payload["excerpt"].lower()
    assert s.entity_refs["transcript_id"] == "t-2"
    assert "BigCo discovery" in s.summary
    assert len(s.summary) <= 140


# ─── Both action item + risk in same transcript ──────────────────────────────


def test_action_item_and_risk_both_emitted_for_same_transcript():
    from backend.signals.transcript import TranscriptSignalSource

    rec = _Record(doc_id="t-3")
    text = (
        "Action item: Blair to send the revised SOW by end of week. "
        "Note: buyer mentioned they are evaluating other vendors too."
    )
    src = TranscriptSignalSource(
        transcript_service=_svc_mock(records=[rec], text_by_doc={"t-3": text}),
    )

    sigs = src.fetch(REP, since=SINCE)

    types = sorted(s.signal_type for s in sigs)
    assert types == ["action_item_assigned_to_rep", "risk_language"]
    risk = next(s for s in sigs if s.signal_type == "risk_language")
    assert risk.payload["matched_phrase"] == "evaluating other"


# ─── Rep is not a participant: service filters them out ──────────────────────


def test_rep_not_a_participant_emits_no_signal():
    from backend.signals.transcript import TranscriptSignalSource

    # Mirrors the DB-side participant filter: service returns empty for this rep.
    src = TranscriptSignalSource(
        transcript_service=_svc_mock(records=[], text_by_doc={})
    )

    assert src.fetch("outsider@acme.example", since=SINCE) == []


# ─── No matching phrases ─────────────────────────────────────────────────────


def test_transcript_with_no_matches_emits_no_signal():
    from backend.signals.transcript import TranscriptSignalSource

    rec = _Record(doc_id="t-4")
    text = (
        "We walked through the demo and the buyer asked clarifying questions "
        "about onboarding. Generally positive vibes, no commitments made."
    )
    src = TranscriptSignalSource(
        transcript_service=_svc_mock(records=[rec], text_by_doc={"t-4": text}),
    )

    assert src.fetch(REP, since=SINCE) == []


# ─── Protocol conformance ────────────────────────────────────────────────────


def test_transcript_source_satisfies_signal_source_protocol():
    from backend.signals import SignalSource
    from backend.signals.transcript import TranscriptSignalSource

    src = TranscriptSignalSource(
        transcript_service=_svc_mock(records=[], text_by_doc={})
    )
    assert isinstance(src, SignalSource)

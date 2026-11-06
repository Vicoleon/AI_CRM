"""Calendar items that match an active rock get a priority boost in the
multi-source priorities pipeline.
"""

from __future__ import annotations

from datetime import datetime, timezone

from backend.agents.multi_source_priorities import (
    _boost_calendar_signals_matching_rocks,
    _rock_tokens,
)
from backend.signals import Signal
from backend.signals.correlator import CorrelationGroup


def _calendar_signal(title: str, severity: str = "medium") -> Signal:
    return Signal(
        user_email="rep@example.com",
        source="calendar",
        signal_type="upcoming_meeting_with_customer",
        summary=title,
        occurred_at=datetime.now(timezone.utc),
        entity_refs={
            "contact_email": "client@acmecorp.example",
            "company_name": "Acme",
        },
        severity_hint=severity,
        payload={"event_title": title, "starts_at": "2026-05-22T09:00:00Z"},
    )


def _hubspot_signal(severity: str = "medium") -> Signal:
    return Signal(
        user_email="rep@example.com",
        source="hubspot",
        signal_type="deal_stale",
        summary="Stale deal",
        occurred_at=datetime.now(timezone.utc),
        entity_refs={"deal_id": "deal-1"},
        severity_hint=severity,
        payload={"deal_id": "deal-1"},
    )


def _group(signals: list[Signal]) -> CorrelationGroup:
    return CorrelationGroup(
        correlation_id="corr-1",
        user_email="rep@example.com",
        primary_entity_kind="deal",
        primary_entity_ref="deal-1",
        signals=signals,
    )


# -- tokenizer --------------------------------------------------------


def test_tokenizer_drops_short_words_and_stopwords():
    toks = _rock_tokens("Ship the AI roadmap by Q3 — review weekly")
    # "Ship" → 4 chars, dropped; "the" / "by" too short; "review" / "weekly"
    # are in the stopword list. Only "roadmap" survives.
    assert "roadmap" in toks
    assert "ship" not in toks
    assert "weekly" not in toks
    assert "review" not in toks


def test_tokenizer_handles_punctuation_and_case():
    toks = _rock_tokens("RoadMap, planning! v2_launch")
    assert "roadmap" in toks
    assert "planning" in toks
    assert "launch" in toks


# -- boost logic ------------------------------------------------------


def test_calendar_signal_matching_rock_gets_severity_high():
    sig = _calendar_signal("AI Roadmap sync with Tatum")
    group = _group([sig])
    _boost_calendar_signals_matching_rocks([group], ["Ship the AI Roadmap"])
    assert sig.severity_hint == "high"
    assert sig.payload["ft17_rock_match"] == ["Ship the AI Roadmap"]


def test_calendar_signal_with_no_overlap_is_not_boosted():
    sig = _calendar_signal("Coffee chat with Sarah")
    group = _group([sig])
    _boost_calendar_signals_matching_rocks([group], ["Ship the AI Roadmap"])
    assert sig.severity_hint == "medium"
    assert "ft17_rock_match" not in sig.payload


def test_non_calendar_signal_is_never_touched():
    sig = _hubspot_signal()
    group = _group([sig])
    _boost_calendar_signals_matching_rocks([group], ["Anything"])
    assert sig.severity_hint == "medium"
    assert "ft17_rock_match" not in sig.payload


def test_already_high_signal_keeps_high_and_gets_breadcrumb():
    sig = _calendar_signal("AI Roadmap planning", severity="high")
    group = _group([sig])
    _boost_calendar_signals_matching_rocks([group], ["Ship the AI Roadmap"])
    assert sig.severity_hint == "high"
    # We still drop the breadcrumb so audit can explain why it ranked high.
    assert sig.payload["ft17_rock_match"] == ["Ship the AI Roadmap"]


def test_multiple_rocks_match_records_all():
    sig = _calendar_signal("AI Roadmap planning for fundraising")
    group = _group([sig])
    _boost_calendar_signals_matching_rocks(
        [group],
        ["Ship the AI Roadmap", "Close fundraising round by Q4"],
    )
    assert sig.severity_hint == "high"
    assert set(sig.payload["ft17_rock_match"]) == {
        "Ship the AI Roadmap",
        "Close fundraising round by Q4",
    }


def test_stopword_only_overlap_does_not_match():
    # Both contain "meeting" / "review" but those are stopwords; no real
    # overlap → should NOT boost.
    sig = _calendar_signal("Weekly review meeting")
    group = _group([sig])
    _boost_calendar_signals_matching_rocks([group], ["Weekly review meeting"])
    assert sig.severity_hint == "medium"
    assert "ft17_rock_match" not in sig.payload


def test_empty_rock_list_is_a_noop():
    sig = _calendar_signal("AI Roadmap sync")
    group = _group([sig])
    _boost_calendar_signals_matching_rocks([group], [])
    assert sig.severity_hint == "medium"

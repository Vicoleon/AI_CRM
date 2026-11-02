"""Priorities quality gate (phase 2, observe-only) — the safety properties.

The load-bearing assertions: fail-open judging (a broken judge can never FAIL a
user), quiet-day = PASS (so future repair can't spam regenerations), and the
mode knob defaulting to a full no-op.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from rubik_core.ingestion_council.models import Health
from rubik_core.ingestion_council.quality_gate import (
    GATE_SCOPE,
    UserBundleRecord,
    build_verdict,
    deterministic_verdict,
    evaluate_records,
    gather_user_bundle_records,
    judge_bundle,
    run_gate,
)


def _record(**kw) -> UserBundleRecord:
    base = dict(
        email="user@acme.example",
        date="2026-07-10",
        has_card=True,
        priorities=[
            {
                "title": "Prep the client call",
                "why_it_matters": "Renewal at risk",
                "suggested_action": "Review notes",
                "urgency": "high",
                "entity_id": "deal-1",
            }
        ],
        audit_trail=["channel gmail → 3 signals, 1 highlights"],
    )
    base.update(kw)
    return UserBundleRecord(**base)


def _llm_never_called(*a, **k):
    raise AssertionError("LLM judge must not be called for this record")


# ── Stage 1: deterministic ────────────────────────────────────────────────────


def test_missing_bundle_fails_without_judging():
    health, reasons, eligible = deterministic_verdict(_record(has_card=False))
    assert health is Health.FAIL and not eligible
    assert any("no bundle" in r for r in reasons)


def test_empty_bundle_with_degraded_generation_fails():
    rec = _record(
        priorities=[],
        audit_trail=["channel gmail → TIMEOUT after 90s", "channel hubspot → [ERROR]"],
    )
    health, reasons, eligible = deterministic_verdict(rec)
    assert health is Health.FAIL and not eligible
    assert any("DEGRADED" in r for r in reasons)


def test_quiet_day_is_pass_not_fail():
    rec = _record(priorities=[], audit_trail=["channel gmail → 0 signals"])
    health, reasons, eligible = deterministic_verdict(rec)
    assert health is Health.PASS and not eligible
    assert any("no_signals" in r for r in reasons)


def test_duplicate_entities_warn():
    rec = _record(
        priorities=[
            {"title": "Call Acme", "entity_id": "deal-1"},
            {"title": "Ring Acme", "entity_id": "deal-1"},
        ]
    )
    health, reasons, eligible = deterministic_verdict(rec)
    assert health is Health.WARN and eligible
    assert any("share entity" in r for r in reasons)


def test_empty_or_missing_records_never_reach_the_judge():
    records = [_record(has_card=False), _record(priorities=[])]
    results = evaluate_records(records, llm=_llm_never_called)
    assert [r.judged for r in results] == [False, False]


def test_unproved_saved_card_text_never_reaches_the_judge(monkeypatch):
    from rubik_core.priorities import batch, cached_visibility, temporal

    leaked = "Revoked CRM deal detail in a Gmail-tagged card"
    card = SimpleNamespace(
        metadata_json={
            "schema_version": 1,
            "priorities": [{"title": leaked, "source_channels": ["gmail"]}],
            "audit_trail": [leaked],
            "generation_status": "ok",
        }
    )
    query = Mock()
    query.filter.return_value = query
    query.order_by.return_value = query
    query.first.return_value = card
    db = Mock()
    db.query.return_value = query
    monkeypatch.setattr(batch, "priorities_roster", lambda _db: ["user@acme.example"])
    monkeypatch.setattr(temporal, "user_today_str", lambda _email: "2026-07-10")
    monkeypatch.setattr(cached_visibility, "cached_bundle_visible", lambda _md: False)

    records = gather_user_bundle_records(db)
    assert len(records) == 1
    assert leaked not in repr(records)
    assert records[0].priorities == []
    assert records[0].generation_status == "unavailable"
    verdict = deterministic_verdict(records[0])
    assert verdict[0] is Health.FAIL and not verdict[2]


# ── Stage 2: judge (fail-open) ────────────────────────────────────────────────


def test_judge_verdict_and_reasons_flow_through():
    out = {"verdict": "warn", "reasons": ["#1 card is tomorrow's meeting"]}
    results = evaluate_records([_record()], llm=lambda *a, **k: out)
    assert results[0].verdict is Health.WARN and results[0].judged
    assert "#1 card is tomorrow's meeting" in results[0].reasons


def test_judge_garbage_degrades_to_pass():
    health, reasons = judge_bundle(_record(), llm=lambda *a, **k: "not json")
    assert health is Health.PASS
    assert any("fail-open" in r for r in reasons)


def test_judge_fallback_on_llm_failure_is_pass():
    # council_llm returns the caller's fallback on failure — simulate that.
    health, reasons = judge_bundle(
        _record(),
        llm=lambda prompt, *, model_key, fallback: fallback,
    )
    assert health is Health.PASS
    assert any("judge_unavailable" in r for r in reasons)


# ── Verdict + mode knob ───────────────────────────────────────────────────────


def test_verdict_aggregates_worst_and_names_users():
    records = [
        _record(email="ok@acme.example"),
        _record(email="bad@acme.example", has_card=False),
    ]
    results = evaluate_records(records, llm=lambda *a, **k: {"verdict": "pass"})
    verdict = build_verdict(results, "observe")
    assert verdict.scope == GATE_SCOPE
    assert verdict.overall_health is Health.FAIL
    assert "OBSERVE" in verdict.headline and "NOT gated" in verdict.headline
    assert any(f.title.startswith("bad:") for f in verdict.top_risks)
    per_user = {u["user"]: u for u in verdict.per_user}
    assert per_user["bad"]["verdict"] == "fail"
    assert per_user["ok"]["verdict"] == "pass"


def test_verdict_renders_and_serialises():
    from rubik_core.ingestion_council.report import render_html

    verdict = build_verdict(
        evaluate_records([_record()], llm=lambda *a, **k: {"verdict": "pass"}),
        "observe",
    )
    assert verdict.to_dict()["per_user"]
    assert "<html" in render_html(verdict).lower()


def test_mode_off_is_a_full_noop(monkeypatch):
    from rubik_core import config

    monkeypatch.setattr(config.settings, "CUBIE_PRIORITIES_GATE_MODE", "off")
    assert run_gate(db=None) is None  # never touches the db when off

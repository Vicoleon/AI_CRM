"""UC-17.6 — Gemini judging pass.

Mocks the Gemini client at the call boundary (`GeminiJudge._call_llm`).
No live model calls. Covers user batching, severity skip, malformed JSON
fallback, missing-correlation_id fallback, batch chunking, and prompt
content assertions.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch

from backend.signals.correlator import CorrelationGroup
from backend.signals.judge import (
    GeminiJudge,
    JudgeDecision,
    _build_prompt,
    _viewer_block,
)
from backend.signals.types import Signal


def _ts(hour: int = 10) -> datetime:
    return datetime(2026, 5, 11, hour, 0, tzinfo=timezone.utc)


def _sig(
    *,
    user="rep@acme.example",
    source="hubspot",
    signal_type="stale_lead",
    summary="deal stale 28 days",
    severity="medium",
    payload=None,
    entity_refs=None,
) -> Signal:
    return Signal(
        user_email=user,
        source=source,
        signal_type=signal_type,
        summary=summary,
        occurred_at=_ts(8),
        entity_refs=entity_refs or {"deal_id": "deal-1"},
        severity_hint=severity,
        payload=payload or {"dealstage": "qualified", "days_since_last_activity": 28},
    )


def _group(
    *,
    correlation_id="deal:deal-1",
    user="rep@acme.example",
    signals=None,
    primary_entity_kind="deal",
    primary_entity_ref="deal-1",
) -> CorrelationGroup:
    return CorrelationGroup(
        correlation_id=correlation_id,
        user_email=user,
        primary_entity_kind=primary_entity_kind,
        primary_entity_ref=primary_entity_ref,
        signals=signals if signals is not None else [_sig()],
    )


def _json_response(entries: list[dict]) -> str:
    return json.dumps(entries)


def test_single_group_returns_one_decision_with_correct_correlation_id():
    grp = _group()
    judge = GeminiJudge()
    mock_raw = _json_response(
        [
            {
                "correlation_id": "deal:deal-1",
                "include": True,
                "importance": 4,
                "reason": "Stale 28d on qualified-stage deal.",
                "next_action": "Send a re-engage email today.",
            }
        ]
    )
    with patch.object(GeminiJudge, "_call_llm", return_value=mock_raw) as m:
        decisions = judge.judge([grp])
    assert m.call_count == 1
    assert len(decisions) == 1
    d = decisions[0]
    assert d.correlation_id == "deal:deal-1"
    assert d.user_email == "rep@acme.example"
    assert d.include is True
    assert d.importance == 4
    assert "Stale" in d.reason
    assert d.next_action.startswith("Send")


def test_two_users_two_groups_each_yields_two_llm_calls():
    g_a1 = _group(correlation_id="deal:a1", user="a@x.example")
    g_a2 = _group(correlation_id="deal:a2", user="a@x.example")
    g_b1 = _group(correlation_id="deal:b1", user="b@x.example")
    g_b2 = _group(correlation_id="deal:b2", user="b@x.example")
    judge = GeminiJudge()

    def fake_call(self, prompt):  # noqa: ARG001
        # Echo back all correlation_ids referenced in the prompt.
        out = []
        for cid in ("deal:a1", "deal:a2", "deal:b1", "deal:b2"):
            if cid in prompt:
                out.append(
                    {
                        "correlation_id": cid,
                        "include": True,
                        "importance": 3,
                        "reason": "stale",
                        "next_action": "ping",
                    }
                )
        return json.dumps(out)

    with patch.object(
        GeminiJudge, "_call_llm", autospec=True, side_effect=fake_call
    ) as m:
        decisions = judge.judge([g_a1, g_a2, g_b1, g_b2])
    assert m.call_count == 2
    assert len(decisions) == 4
    assert {d.correlation_id for d in decisions} == {
        "deal:a1",
        "deal:a2",
        "deal:b1",
        "deal:b2",
    }


def test_all_low_severity_group_skips_llm():
    low_sig = _sig(severity="low", summary="newsletter blast")
    grp = _group(correlation_id="deal:low-only", signals=[low_sig])
    judge = GeminiJudge()
    with patch.object(GeminiJudge, "_call_llm") as m:
        decisions = judge.judge([grp])
    m.assert_not_called()
    assert len(decisions) == 1
    d = decisions[0]
    assert d.include is False
    assert d.importance == 0
    assert d.next_action == ""
    assert "low-severity" in d.reason


def test_malformed_json_falls_back_per_group():
    g1 = _group(correlation_id="deal:1")
    g2 = _group(
        correlation_id="deal:2",
        user="rep@acme.example",
        signals=[_sig(entity_refs={"deal_id": "2"})],
    )
    judge = GeminiJudge()
    with patch.object(GeminiJudge, "_call_llm", return_value="not json at all {{{"):
        decisions = judge.judge([g1, g2])
    assert len(decisions) == 2
    for d in decisions:
        assert d.include is False
        assert d.importance == 0
        assert "parse error" in d.reason
        assert d.next_action == ""


def test_missing_correlation_id_in_response_gets_fallback():
    g1 = _group(correlation_id="deal:1")
    g2 = _group(
        correlation_id="deal:2",
        user="rep@acme.example",
        signals=[_sig(entity_refs={"deal_id": "2"})],
    )
    raw = _json_response(
        [
            {
                "correlation_id": "deal:1",
                "include": True,
                "importance": 5,
                "reason": "hot deal",
                "next_action": "call now",
            }
            # deal:2 deliberately missing
        ]
    )
    judge = GeminiJudge()
    with patch.object(GeminiJudge, "_call_llm", return_value=raw):
        decisions = judge.judge([g1, g2])
    by_id = {d.correlation_id: d for d in decisions}
    assert by_id["deal:1"].include is True
    assert by_id["deal:2"].include is False
    assert "omitted" in by_id["deal:2"].reason


def test_max_groups_per_call_chunks_correctly():
    groups = [
        _group(
            correlation_id=f"deal:{i}", signals=[_sig(entity_refs={"deal_id": str(i)})]
        )
        for i in range(5)
    ]
    judge = GeminiJudge(max_groups_per_call=2)

    def fake_call(self, prompt):  # noqa: ARG001
        out = []
        for i in range(5):
            cid = f"deal:{i}"
            if cid in prompt:
                out.append(
                    {
                        "correlation_id": cid,
                        "include": True,
                        "importance": 2,
                        "reason": "x",
                        "next_action": "y",
                    }
                )
        return json.dumps(out)

    with patch.object(
        GeminiJudge, "_call_llm", autospec=True, side_effect=fake_call
    ) as m:
        decisions = judge.judge(groups)
    # 5 groups, batch size 2 -> 3 calls (2 + 2 + 1).
    assert m.call_count == 3
    assert len(decisions) == 5
    assert {d.correlation_id for d in decisions} == {f"deal:{i}" for i in range(5)}


def test_prompt_includes_signal_summary_and_signal_type():
    sig = _sig(
        summary="awaiting reply for 4 business days",
        signal_type="awaiting_reply",
        source="gmail",
        entity_refs={"thread_id": "t-1", "contact_email": "buyer@acmecorp.example"},
        payload={"subject": "Pricing follow-up", "thread_id": "t-1"},
    )
    grp = _group(
        correlation_id="thread:t-1",
        signals=[sig],
        primary_entity_kind="thread",
        primary_entity_ref="t-1",
    )
    captured: dict[str, str] = {}

    def fake_call(self, prompt):  # noqa: ARG001
        captured["prompt"] = prompt
        return _json_response(
            [
                {
                    "correlation_id": "thread:t-1",
                    "include": True,
                    "importance": 3,
                    "reason": "awaiting reply",
                    "next_action": "follow up",
                }
            ]
        )

    judge = GeminiJudge()
    with patch.object(GeminiJudge, "_call_llm", autospec=True, side_effect=fake_call):
        judge.judge([grp])
    prompt = captured["prompt"]
    assert "awaiting reply for 4 business days" in prompt
    assert "awaiting_reply" in prompt
    assert "thread:t-1" in prompt
    # System framing must appear.
    assert "senior sales manager" in prompt


def test_include_false_forces_empty_next_action():
    grp = _group()
    raw = _json_response(
        [
            {
                "correlation_id": "deal:deal-1",
                "include": False,
                "importance": 4,  # should get coerced to 0
                "reason": "newsletter noise",
                "next_action": "should be wiped",
            }
        ]
    )
    judge = GeminiJudge()
    with patch.object(GeminiJudge, "_call_llm", return_value=raw):
        decisions = judge.judge([grp])
    assert len(decisions) == 1
    d = decisions[0]
    assert d.include is False
    assert d.importance == 0
    assert d.next_action == ""


def test_judge_decision_dataclass_shape():
    d = JudgeDecision(
        correlation_id="x",
        user_email="u@x.example",
        include=True,
        importance=3,
        reason="r",
        next_action="a",
    )
    assert d.raw_response == ""
    assert d.correlation_id == "x"


def test_build_prompt_strips_fenced_code_safely():
    # Sanity: ensure _build_prompt produces JSON-shaped content (no exceptions).
    grp = _group()
    prompt = _build_prompt([grp])
    assert "INPUT (JSON):" in prompt
    assert "OUTPUT SPEC:" in prompt


def test_alignment_context_appears_in_prompt_for_matching_user():
    """When alignment_context_by_user has the user's EOS block, the prompt
    must include it as a "COMPANY DIRECTION" preamble and the judge's
    aligned/alignment_note response must round-trip into JudgeDecision.
    """
    grp = _group(user="rep@x.example")
    captured: dict[str, str] = {}

    def fake_call(self, prompt):  # noqa: ARG001
        captured["prompt"] = prompt
        return _json_response(
            [
                {
                    "correlation_id": grp.correlation_id,
                    "include": True,
                    "importance": 4,
                    "aligned": True,
                    "alignment_note": "Rock: AI Roadmap",
                    "reason": "Stale 28d on the AI Roadmap deal.",
                    "next_action": "Send re-engage email.",
                }
            ]
        )

    judge = GeminiJudge()
    with patch.object(GeminiJudge, "_call_llm", autospec=True, side_effect=fake_call):
        decisions = judge.judge(
            [grp],
            alignment_context_by_user={
                "rep@x.example": "\n## COMPANY DIRECTION\n- Core Focus: Make Acme boring\n",
            },
        )
    prompt = captured["prompt"]
    assert "COMPANY DIRECTION" in prompt
    assert "Make Acme boring" in prompt
    assert len(decisions) == 1
    d = decisions[0]
    assert d.aligned is True
    assert d.alignment_note == "Rock: AI Roadmap"


def test_no_alignment_context_omits_company_direction_block():
    """When no per-user context is provided, the prompt must NOT carry
    a COMPANY DIRECTION section — judge falls back to severity-only.
    """
    grp = _group(user="rep@x.example")
    captured: dict[str, str] = {}

    def fake_call(self, prompt):  # noqa: ARG001
        captured["prompt"] = prompt
        return _json_response(
            [
                {
                    "correlation_id": grp.correlation_id,
                    "include": True,
                    "importance": 3,
                    "aligned": False,
                    "alignment_note": "",
                    "reason": "stale",
                    "next_action": "ping",
                }
            ]
        )

    judge = GeminiJudge()
    with patch.object(GeminiJudge, "_call_llm", autospec=True, side_effect=fake_call):
        decisions = judge.judge([grp])  # no alignment_context_by_user

    assert "COMPANY DIRECTION (live from EOS)" not in captured["prompt"]
    d = decisions[0]
    assert d.aligned is False
    assert d.alignment_note == ""


def test_empty_alignment_context_string_omits_company_direction_block():
    """An entry of "" (e.g. user has no EOS data, or CUBIE_DISABLE_ALIGNMENT
    short-circuited) should be treated the same as no entry at all.
    """
    grp = _group(user="rep@x.example")
    captured: dict[str, str] = {}

    def fake_call(self, prompt):  # noqa: ARG001
        captured["prompt"] = prompt
        return _json_response(
            [
                {
                    "correlation_id": grp.correlation_id,
                    "include": True,
                    "importance": 2,
                    "aligned": False,
                    "alignment_note": "",
                    "reason": "r",
                    "next_action": "a",
                }
            ]
        )

    judge = GeminiJudge()
    with patch.object(GeminiJudge, "_call_llm", autospec=True, side_effect=fake_call):
        judge.judge([grp], alignment_context_by_user={"rep@x.example": ""})

    assert "COMPANY DIRECTION (live from EOS)" not in captured["prompt"]


def test_aligned_false_strips_alignment_note_even_if_llm_provided_one():
    """Schema invariant: aligned=False implies alignment_note="".
    Defensive coercion in the parser stops a sloppy LLM response from
    polluting downstream evidence.
    """
    grp = _group(user="rep@x.example")
    raw = _json_response(
        [
            {
                "correlation_id": grp.correlation_id,
                "include": True,
                "importance": 2,
                "aligned": False,
                "alignment_note": "should be wiped",
                "reason": "urgent unaligned",
                "next_action": "respond",
            }
        ]
    )
    judge = GeminiJudge()
    with patch.object(GeminiJudge, "_call_llm", return_value=raw):
        decisions = judge.judge([grp])
    d = decisions[0]
    assert d.aligned is False
    assert d.alignment_note == ""


def test_missing_aligned_fields_default_to_false_empty():
    """Legacy / older response shape (no aligned, no alignment_note) must
    still parse — defaulting to aligned=False, alignment_note="".
    """
    grp = _group(user="rep@x.example")
    raw = _json_response(
        [
            {
                "correlation_id": grp.correlation_id,
                "include": True,
                "importance": 3,
                "reason": "r",
                "next_action": "a",
                # aligned / alignment_note deliberately missing
            }
        ]
    )
    judge = GeminiJudge()
    with patch.object(GeminiJudge, "_call_llm", return_value=raw):
        decisions = judge.judge([grp])
    d = decisions[0]
    assert d.include is True
    assert d.aligned is False
    assert d.alignment_note == ""


def test_viewer_block_carries_name_and_email_and_second_person_rule():
    block = _viewer_block("Blair Ellison", "bellison@acme.example")
    assert "Blair Ellison" in block
    assert "bellison@acme.example" in block
    # The whole point: tell the model to address the reader as "you", not by name.
    assert '"you"' in block


def test_viewer_block_empty_when_no_identity():
    assert _viewer_block(None, None) == ""
    assert _viewer_block("", "") == ""


def test_viewer_block_falls_back_to_email_only():
    block = _viewer_block(None, "rep@x.example")
    assert "rep@x.example" in block
    assert '"you"' in block


def test_display_name_appears_in_prompt_for_matching_user():
    """The judge must tell the LLM who it's writing for so it stops
    referring to the reader in the third person on their own card.
    """
    grp = _group(user="bellison@acme.example")
    captured: dict[str, str] = {}

    def fake_call(self, prompt):  # noqa: ARG001
        captured["prompt"] = prompt
        return _json_response(
            [
                {
                    "correlation_id": grp.correlation_id,
                    "include": True,
                    "importance": 3,
                    "aligned": False,
                    "alignment_note": "",
                    "reason": "you last replied 2 days ago",
                    "next_action": "reply to the thread",
                }
            ]
        )

    judge = GeminiJudge()
    with patch.object(GeminiJudge, "_call_llm", autospec=True, side_effect=fake_call):
        judge.judge(
            [grp],
            display_name_by_user={"bellison@acme.example": "Blair Ellison"},
        )

    prompt = captured["prompt"]
    assert "WHO THESE PRIORITIES ARE FOR" in prompt
    assert "Blair Ellison" in prompt
    assert "bellison@acme.example" in prompt


def test_no_display_name_falls_back_to_email_identity_in_prompt():
    """Even without a display name, the judge still identifies the reader by
    email so a thread participant matching that email isn't named as a third
    party."""
    grp = _group(user="rep@x.example")
    captured: dict[str, str] = {}

    def fake_call(self, prompt):  # noqa: ARG001
        captured["prompt"] = prompt
        return _json_response(
            [
                {
                    "correlation_id": grp.correlation_id,
                    "include": True,
                    "importance": 2,
                    "aligned": False,
                    "alignment_note": "",
                    "reason": "stale",
                    "next_action": "ping",
                }
            ]
        )

    judge = GeminiJudge()
    with patch.object(GeminiJudge, "_call_llm", autospec=True, side_effect=fake_call):
        judge.judge([grp])  # no display_name_by_user

    prompt = captured["prompt"]
    assert "WHO THESE PRIORITIES ARE FOR" in prompt
    assert "rep@x.example" in prompt


# ─── Drive labeled-document signals (orphan, no CRM entity) ───────────────────


def _drive_sig(*, user="rep@acme.example", severity="high"):
    return Signal(
        user_email=user,
        source="drive",
        signal_type="labeled_document_updated",
        summary="Recently updated document 'Cubie Roadmap' labeled Cubie.",
        occurred_at=_ts(9),
        entity_refs={},  # no deal/contact/company → orphan group
        severity_hint=severity,
        payload={
            "file_name": "Cubie Roadmap",
            "label_values": ["Cubie"],
            "web_view_link": "https://drive.google.com/file/d/1",
            "modified_time": "2026-05-11T09:00:00Z",
            "priority": 1.5,
        },
    )


def test_high_severity_drive_orphan_reaches_llm_and_can_be_included():
    grp = _group(
        correlation_id="orphan:doc-1",
        primary_entity_kind="orphan",
        primary_entity_ref="doc-1",
        signals=[_drive_sig()],
    )
    captured: dict[str, str] = {}

    def fake_call(self, prompt):  # noqa: ARG001
        captured["prompt"] = prompt
        return _json_response(
            [
                {
                    "correlation_id": "orphan:doc-1",
                    "include": True,
                    "importance": 4,
                    "aligned": False,
                    "alignment_note": "",
                    "reason": "Recently updated, Important-labeled Cubie doc.",
                    "next_action": "Review the Cubie Roadmap before the standup.",
                }
            ]
        )

    judge = GeminiJudge()
    with patch.object(
        GeminiJudge, "_call_llm", autospec=True, side_effect=fake_call
    ) as m:
        decisions = judge.judge([grp])

    m.assert_called_once()  # NOT auto-rejected (high severity)
    assert decisions[0].include is True
    assert decisions[0].importance == 4
    # The doc context (whitelisted payload) + framing reach the prompt.
    prompt = captured["prompt"]
    assert "Cubie Roadmap" in prompt
    assert "labeled_document_updated" in prompt


def test_drive_payload_is_whitelisted_in_signal_view():
    from backend.signals.judge import _signal_view

    view = _signal_view(_drive_sig(), now=_ts(10))
    assert view["payload"]["file_name"] == "Cubie Roadmap"
    assert view["payload"]["label_values"] == ["Cubie"]
    assert view["payload"]["web_view_link"].endswith("/1")


def test_the_drive_example_names_the_seeded_importance_labels():
    """The judge's example Drive labels are the ones the catalog is described
    with (Important / <product> / Project), the product taken from the brand
    module rather than spelled out."""
    from backend.signals import judge
    from rubik_core.brand import PRODUCT_NAME

    assert f"(e.g. Important / {PRODUCT_NAME} / Project)" in judge._SYSTEM_FRAMING
    assert "Priority / Project" not in judge._SYSTEM_FRAMING

"""One-section rewrite of a Meeting Prep brief (``rubik_core.meeting_prep.rewrite``).

What this pins down:

* The model's value comes back in the exact shape ``MeetingPrepResponse``
  stores, whatever shape the model chose (a list for prose, a string for a
  list, a stakeholder array).
* The prompt is built from the same research the brief was built from, every
  block conditional on its data — old rows, Deep Prep rows and a missing
  bundle all still work, and an absent block adds no header.
* Persona, web search and the grounding rules follow the full brief exactly
  (and a drift test keeps the rules word for word with ``home.py``).
* Any model failure is a reader-safe ``RewriteError``, never a half-written
  section.

The genai client is always faked (patched by string target).
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.tests._legacy_brand import LEGACY_PATTERN
from rubik_core.brand import PRODUCT_NAME
from rubik_core.cost.usage import current_feature
from rubik_core.meeting_prep import rewrite as rw
from rubik_core.meeting_prep import signals
from rubik_core.meeting_prep.rewrite import (
    MSG_UNAVAILABLE,
    MSG_UNUSABLE,
    RewriteError,
    RewriteResult,
    rewrite_section,
)
from rubik_core.meeting_prep.sections import MAX_PROSE_CHARS, get_section
from rubik_core.people.schemas import AttendeeIntel

USER = "bellison@acme.example"
EXTERNAL = "riley.fontaine@northwind.example"
STRANGER = "someone.else@northwind.example"

SUMMARY_TEXT = "Northwind is reviewing its GCP spend with Acme (UNIQUE-SUMMARY-MARK)."
OBJECTIVE_TEXT = "Agree a scope for the GCP cost review."


def _intel_dict(email: str = EXTERNAL) -> dict:
    return AttendeeIntel.from_dict(
        {
            "email": email,
            "name": "Riley Fontaine",
            "profile": {
                "name": "Riley Fontaine",
                "current_title": "Account Manager",
                "current_company": "Northwind",
            },
            "sources": ["https://linkedin.example/in/riley-fontaine"],
            "provider": "web_search",
        }
    ).as_dict()


def _brief(**overrides) -> dict:
    brief = {
        "title": "Acme / Northwind — GCP review",
        "executive_summary": SUMMARY_TEXT,
        "meeting_objective": OBJECTIVE_TEXT,
        "client_profile": "Mid-size cloud reseller.",
        "stakeholder_map": json.dumps(
            [
                {
                    "name": "Riley Fontaine",
                    "email": EXTERNAL,
                    "role": "Account Manager",
                    "context": "Owns the account.",
                }
            ]
        ),
        "recent_interactions": "",
        "past_meeting_context": "",
        "product_fit_analysis": "",
        "recommended_positioning": "Lead with idle non-prod spend.",
        "likely_objections": "",
        "discovery_questions": ["What is your monthly GCP bill?"],
        "suggested_agenda": ["Intros", "Spend review", "Next steps"],
        "talking_points": ["Idle non-prod is most of the overspend."],
        "next_steps": ["Send the scope."],
        "open_items_you_owe": [],
        "source_evidence": ["Email: Re: GCP review"],
        "is_sales_user": True,
        "is_internal": False,
    }
    brief.update(overrides)
    return brief


def _bundle(**overrides) -> dict:
    bundle = {
        "title": "Acme / Northwind — GCP review",
        "description": "Quarterly spend review",
        "location": "Google Meet",
        "focus_on": None,
        "start": "Thu, Aug 20 at 10:00 AM",
        "attendees": [
            {"name": "Blair Ellison", "email": USER},
            {"name": "Riley Fontaine", "email": EXTERNAL},
        ],
        "past_meetings_report": "Transcript 2026-07-01: discussed GCP credits.",
        "prior_meetings_block": (
            "## PRIOR MEETINGS WITH THESE ATTENDEES (attendance records — treat as ground truth)\n"
            '- Wednesday, July 01, 2026 — "GCP kickoff" — with Riley Fontaine'
        ),
        "email_block": "[Mon, 17 Aug 2026] From: Riley\nSubject: Re: GCP review\nSnippet: see attached",
        "drive_context": "",
        "matched_products": ["Cloud Cost Review"],
        "matched_product_details": [
            {
                "name": "Cloud Cost Review",
                "description": "Finds idle non-prod spend.",
                "category": "Cloud",
            }
        ],
        "attendee_intel": [],
        "errors": [],
        "signal_blocks": {
            "crm": "## CRM RECORD (HubSpot)\n- Open deal: GCP review — stage Proposal",
            "xray": "",
            "promises": "You said you would send the credits memo by Friday.",
        },
        "account_snapshot": None,
        "signals_used": [],
    }
    bundle.update(overrides)
    return bundle


def _fake_client(captured: dict, *, payload=None, text: str | None = None, exc=None):
    """A genai client recording the prompt/config and returning ``payload``.

    Async on purpose: ``rewrite_section`` awaits
    ``client.aio.models.generate_content``; a sync stub would raise and land in
    the error branch, quietly passing assertions that never ran.
    """

    async def _generate_content(*, model, contents, config):
        captured["model"] = model
        captured["prompt"] = contents
        captured["system"] = config.system_instruction
        captured["config"] = config
        captured["feature"] = current_feature.get()
        if exc is not None:
            raise exc
        body = text if text is not None else json.dumps(payload)
        return SimpleNamespace(text=body)

    aio = SimpleNamespace(models=SimpleNamespace(generate_content=_generate_content))
    return SimpleNamespace(aio=aio)


def _run(
    section_key: str,
    *,
    payload=None,
    text: str | None = None,
    exc=None,
    brief: dict | None = None,
    bundle="default",
    is_sales_user: bool = True,
    is_internal: bool = False,
    instruction: str = "",
    personalization_block: str = "",
    wgll_context: str = "",
) -> tuple[RewriteResult, dict]:
    captured: dict = {}
    with patch(
        "rubik_core.models.get_genai_client",
        return_value=_fake_client(captured, payload=payload, text=text, exc=exc),
    ):
        result = asyncio.run(
            rewrite_section(
                section_key=section_key,
                brief=brief if brief is not None else _brief(),
                context_bundle=_bundle() if bundle == "default" else bundle,
                is_sales_user=is_sales_user,
                is_internal=is_internal,
                instruction=instruction,
                personalization_block=personalization_block,
                wgll_context=wgll_context,
            )
        )
    return result, captured


def _run_error(section_key: str, **kwargs) -> tuple[RewriteError, dict]:
    captured: dict = {}
    with (
        patch(
            "rubik_core.models.get_genai_client",
            return_value=_fake_client(
                captured,
                payload=kwargs.pop("payload", None),
                text=kwargs.pop("text", None),
                exc=kwargs.pop("exc", None),
            ),
        ),
        pytest.raises(RewriteError) as info,
    ):
        asyncio.run(
            rewrite_section(
                section_key=section_key,
                brief=kwargs.pop("brief", _brief()),
                context_bundle=kwargs.pop("bundle", _bundle()),
                is_sales_user=kwargs.pop("is_sales_user", True),
                is_internal=kwargs.pop("is_internal", False),
                instruction=kwargs.pop("instruction", ""),
            )
        )
    return info.value, captured


# ─── Output shapes ─────────────────────────────────────────────────────────


def test_prose_value_is_normalized_and_sources_cleaned():
    result, captured = _run(
        "executive_summary",
        payload={
            "value": "  A tighter summary.  ",
            "sources": [
                "Email: Re: GCP review",
                "",
                "  ",
                "Email: Re: GCP review",
                "- Transcript 2026-07-01",
            ],
        },
    )
    assert result.value == "A tighter summary."
    assert result.sources == ["Email: Re: GCP review", "Transcript 2026-07-01"]
    # The call is billed to its own AI Cost category, never "meeting_prep".
    assert captured["feature"] == "meeting_prep_section"
    assert captured["config"].temperature == 0.2


def test_list_value_is_normalized():
    result, _ = _run(
        "suggested_agenda",
        payload={"value": ["Intros (5 min)", "  ", "Spend deep-dive"], "sources": []},
    )
    assert result.value == ["Intros (5 min)", "Spend deep-dive"]
    assert result.sources == []


def test_stakeholder_value_becomes_the_json_string_the_page_parses():
    result, _ = _run(
        "stakeholder_map",
        payload={
            "value": [
                {
                    "name": "Riley Fontaine",
                    "email": EXTERNAL,
                    "role": "Account Manager",
                    "context": "Owns the renewal.",
                    "extra": "ignored",
                }
            ],
            "sources": ["https://linkedin.example/in/riley-fontaine"],
        },
    )
    assert isinstance(result.value, str)
    assert json.loads(result.value) == [
        {
            "name": "Riley Fontaine",
            "email": EXTERNAL,
            "role": "Account Manager",
            "context": "Owns the renewal.",
        }
    ]


def test_list_returned_for_a_prose_section_is_joined_as_bullets():
    result, _ = _run(
        "executive_summary",
        payload={"value": ["First point.", "Second point."], "sources": []},
    )
    assert result.value == "- First point.\n- Second point."


def test_string_returned_for_a_list_section_is_split():
    numbered, _ = _run(
        "suggested_agenda",
        payload={"value": "1. Intros\n2. Spend review\n3. Wrap up", "sources": []},
    )
    assert numbered.value == ["Intros", "Spend review", "Wrap up"]

    plain, _ = _run(
        "next_steps",
        payload={"value": "Send the scope\n* Book the follow-up", "sources": []},
    )
    assert plain.value == ["Send the scope", "Book the follow-up"]


def test_non_attendee_in_stakeholder_map_passes_through_but_prompt_forbids_it():
    result, captured = _run(
        "stakeholder_map",
        payload={
            "value": [
                {
                    "name": "Riley Fontaine",
                    "email": EXTERNAL,
                    "role": "AM",
                    "context": "x",
                },
                {
                    "name": "Not Invited",
                    "email": STRANGER,
                    "role": "CFO",
                    "context": "y",
                },
            ],
            "sources": [],
        },
    )
    # We don't filter — the person can delete a row by hand …
    assert [r["email"] for r in json.loads(result.value)] == [EXTERNAL, STRANGER]
    # … but the prompt carries the same attendees-only rule as the full brief.
    assert (
        "CRITICAL: Only include people in the ## ATTENDEES list" in captured["prompt"]
    )
    assert f"- Riley Fontaine <{EXTERNAL}>" in captured["prompt"]


def test_stakeholder_context_wording_follows_the_persona():
    payload = {"value": [{"name": "R", "email": EXTERNAL}], "sources": []}
    _, battle = _run("stakeholder_map", payload=payload)
    assert "agenda and influence for THIS meeting" in battle["prompt"]

    _, ea = _run(
        "stakeholder_map", payload=payload, is_sales_user=False, is_internal=True
    )
    assert "what they care about and their dynamic" in ea["prompt"]
    assert "CRITICAL: Only include people in the ## ATTENDEES list" in ea["prompt"]


def test_fenced_json_with_a_prose_preamble_is_still_parsed():
    body = 'Here is the section:\n```json\n{"value": "Fine.", "sources": ["a"]}\n```'
    result, _ = _run("executive_summary", text=body)
    assert result.value == "Fine."
    assert result.sources == ["a"]


def test_raw_newlines_inside_a_string_are_still_parsed():
    # The battle path has no JSON mode (google_search is on), and prose asked
    # for as '- ' lines often arrives with literal newlines inside the string.
    body = (
        '```json\n{"value": "- Idle non-prod.\n- Unused credits.", "sources": []}\n```'
    )
    result, _ = _run("executive_summary", text=body)
    assert result.value == "- Idle non-prod.\n- Unused credits."


def test_trailing_prose_with_a_brace_after_the_object_is_ignored():
    body = (
        'Here is the rewrite:\n{"value": "Fine.", "sources": ["a"]}\n'
        "Note: based on {email} from Riley."
    )
    result, _ = _run("executive_summary", text=body)
    assert result.value == "Fine."
    assert result.sources == ["a"]


def test_sources_are_capped():
    result, _ = _run(
        "executive_summary",
        payload={"value": "x", "sources": [f"src {i}" for i in range(50)]},
    )
    assert len(result.sources) == rw.MAX_SOURCES


def test_empty_open_items_is_a_valid_answer():
    result, _ = _run(
        "open_items_you_owe",
        payload={"value": [], "sources": []},
        is_sales_user=False,
        is_internal=True,
    )
    assert result.value == []


# ─── Failures ──────────────────────────────────────────────────────────────


def test_model_exception_is_a_reader_safe_rewrite_error():
    err, _ = _run_error("executive_summary", exc=RuntimeError("429 quota exhausted"))
    assert str(err) == MSG_UNAVAILABLE
    assert "429" not in str(err)


def test_slow_model_call_is_abandoned_before_the_page_gives_up(monkeypatch):
    """The page waits 180s; the server must stop first, so it never saves a
    version after the reader was told their current one is unchanged."""
    page_ms = re.search(
        r"GENERATION_TIMEOUT_MS = ([\d_]+);",
        (
            Path(__file__).resolve().parents[2]
            / "frontend"
            / "src"
            / "api"
            / "meetingPrep.ts"
        ).read_text(encoding="utf-8"),
    )
    assert page_ms, "GENERATION_TIMEOUT_MS moved — re-check REWRITE_TIMEOUT_S"
    assert rw.REWRITE_TIMEOUT_S * 1000 < int(page_ms.group(1).replace("_", ""))

    state: dict = {}

    async def _slow(*, model, contents, config):
        try:
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise
        return SimpleNamespace(text=json.dumps({"value": "Too late.", "sources": []}))

    slow_client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=_slow))
    )
    monkeypatch.setattr("rubik_core.meeting_prep.rewrite.REWRITE_TIMEOUT_S", 0.05)
    with (
        patch("rubik_core.models.get_genai_client", return_value=slow_client),
        pytest.raises(RewriteError) as info,
    ):
        asyncio.run(
            rewrite_section(
                section_key="executive_summary",
                brief=_brief(),
                context_bundle=_bundle(),
                is_sales_user=True,
                is_internal=False,
            )
        )
    assert str(info.value) == MSG_UNAVAILABLE
    assert state.get("cancelled") is True


def test_garbage_json_is_a_rewrite_error():
    err, _ = _run_error("executive_summary", text="I could not produce JSON today.")
    assert str(err) == MSG_UNUSABLE


def test_missing_value_key_is_a_rewrite_error():
    err, _ = _run_error("executive_summary", payload={"sources": ["a"]})
    assert str(err) == MSG_UNUSABLE


def test_value_the_normalizer_rejects_is_a_rewrite_error():
    err, _ = _run_error(
        "executive_summary", payload={"value": "x" * (MAX_PROSE_CHARS + 1)}
    )
    assert str(err) == MSG_UNUSABLE


def test_empty_prose_is_a_rewrite_error():
    err, _ = _run_error("executive_summary", payload={"value": "   ", "sources": []})
    assert str(err) == MSG_UNUSABLE


def test_unknown_section_is_a_caller_error_and_never_calls_the_model():
    captured: dict = {}
    with (
        patch(
            "rubik_core.models.get_genai_client",
            return_value=_fake_client(captured, payload={"value": "x"}),
        ),
        pytest.raises(ValueError),
    ):
        asyncio.run(
            rewrite_section(
                section_key="attendee_intel",
                brief=_brief(),
                context_bundle=_bundle(),
                is_sales_user=True,
                is_internal=False,
            )
        )
    assert captured == {}


# ─── Prompt contents ───────────────────────────────────────────────────────


def test_instruction_reaches_the_prompt_as_a_style_request():
    _, captured = _run(
        "recommended_positioning",
        payload={"value": "Lead with the numbers.", "sources": []},
        instruction="Make it shorter and lead with pricing",
    )
    prompt = captured["prompt"]
    assert '"Make it shorter and lead with pricing"' in prompt
    assert "never permission to invent facts" in prompt
    assert "say plainly what is missing" in prompt
    assert "No specific instruction" not in prompt


def test_no_instruction_asks_for_a_fresh_version():
    _, captured = _run(
        "recommended_positioning", payload={"value": "Lead.", "sources": []}
    )
    assert "No specific instruction" in captured["prompt"]
    assert 'WHAT THE READER ASKED FOR\n"' not in captured["prompt"]


def test_research_and_signal_blocks_reach_the_prompt_and_empty_ones_add_no_header():
    _, captured = _run("executive_summary", payload={"value": "x", "sources": []})
    prompt, system = captured["prompt"], captured["system"]

    assert "## RESEARCH DATA" in prompt
    assert "## PRIOR MEETINGS WITH THESE ATTENDEES" in prompt
    assert "Transcript 2026-07-01: discussed GCP credits." in prompt
    assert "### RECENT EMAIL WITH THESE PEOPLE" in prompt
    assert "Subject: Re: GCP review" in prompt
    assert "## PRODUCT KNOWLEDGE\n- Cloud Cost Review: " in prompt
    # Signal blocks verbatim, in order; a block without a heading gets one.
    crm = prompt.index("## CRM RECORD (HubSpot)")
    promises = prompt.index("## PROMISES\nYou said you would send the credits memo")
    assert crm < promises
    # The empty xray block adds nothing at all — no block, no rule.
    assert "XRAY" not in prompt
    assert "X-RAY" not in system
    # And the rules that point at those blocks are present because they are.
    assert "PRIOR MEETINGS WITH THESE ATTENDEES comes from attendance records" in system
    assert (
        signals.grounding_rules({"crm": "x", "promises": "x"}) + rw._REWRITE_RULES
        in system
    )
    # No attendee research in this bundle → no clause pointing at it.
    assert "ATTENDEE INTELLIGENCE" not in system
    assert "ATTENDEE INTELLIGENCE" not in prompt


# Every signal block, keyed out of order, plus one the full brief never prompts
# with. The headings are the real ones the grounding rules point at.
_ALL_SIGNAL_BLOCKS = {
    "flagged": "## WHAT CUBIE ALREADY FLAGGED\n- Reply to Riley about credits",
    "availability": "## TEAM AVAILABILITY\n- Dakota Lane: out Aug 24-28",
    "promises": "## YOUR OPEN TO-DOS FOR THESE PEOPLE\n- Send the credits memo (due Aug 21)",
    "last_meeting_notes": "## WHAT WAS AGREED LAST TIME\n- Jul 1: send the scope",
    "xray": "## CUSTOMER X-RAY ON FILE\n- Churn score 40 (Jul 1)",
    "crm": "## CRM ACCOUNT RECORD\n- Open deal: GCP review — Proposal, $48,500",
    "mystery": "## MYSTERY BLOCK\nshould never reach the prompt",
}


def _run_all_signals(section_key: str, *, battle: bool = True, payload=None):
    return _run(
        section_key,
        payload=payload or {"value": ["x"], "sources": []},
        bundle=_bundle(signal_blocks=dict(_ALL_SIGNAL_BLOCKS)),
        is_sales_user=battle,
        is_internal=not battle,
    )


@pytest.mark.parametrize("battle", [True, False])
def test_signal_blocks_carry_the_full_briefs_own_rules_in_block_order(battle):
    _, captured = _run_all_signals(
        "executive_summary", battle=battle, payload={"value": "x", "sources": []}
    )
    prompt, system = captured["prompt"], captured["system"]
    known = {k: v for k, v in _ALL_SIGNAL_BLOCKS.items() if k in signals.BLOCK_ORDER}
    # The exact rules the full brief takes from signals.prompt_parts.
    assert signals.prompt_parts(known, battle_brief=battle)["grounding"] in system
    positions = [prompt.index(known[k]) for k in signals.BLOCK_ORDER]
    assert positions == sorted(positions)
    assert "MYSTERY BLOCK" not in prompt


@pytest.mark.parametrize(
    ("section_key", "part"),
    [
        ("client_profile", "client_profile"),
        ("past_meeting_context", "past_meeting_context"),
        ("talking_points", "talking_points"),
        ("next_steps", "next_steps"),
    ],
)
@pytest.mark.parametrize("battle", [True, False])
def test_section_gets_the_full_briefs_signal_clause(section_key, part, battle):
    kind = get_section(section_key).kind
    payload = {"value": "x" if kind == "prose" else ["x"], "sources": []}
    _, captured = _run_all_signals(section_key, battle=battle, payload=payload)
    sig = signals.prompt_parts(_ALL_SIGNAL_BLOCKS, battle_brief=battle)
    purpose = get_section(section_key).brief(battle=battle)
    assert sig[part]
    assert f"What this section is for: {purpose}{sig[part]}\n" in captured["prompt"]


def test_open_items_takes_the_ea_clause_to_include_every_to_do():
    _, captured = _run_all_signals("open_items_you_owe", battle=False)
    sig = signals.prompt_parts(_ALL_SIGNAL_BLOCKS, battle_brief=False)
    purpose = get_section("open_items_you_owe").ea_brief
    assert "with its due date" in sig["open_items"]
    assert (
        f"What this section is for: {purpose}{sig['open_items']}\n"
        in (captured["prompt"])
    )


def test_open_items_on_the_battle_brief_uses_the_full_briefs_field_definition():
    _, captured = _run_all_signals("open_items_you_owe", battle=True)
    assert (
        "What this section is for: A JSON ARRAY of what the reader still owes "
        "these attendees, drawn ONLY from YOUR OPEN TO-DOS FOR THESE PEOPLE "
        "(keep each due date)" in captured["prompt"]
    )


def test_no_block_no_section_clause():
    # The default bundle has CRM and promises, but no availability or flags.
    _, steps = _run("next_steps", payload={"value": ["x"], "sources": []})
    assert "TEAM AVAILABILITY" not in steps["prompt"]
    assert "TEAM AVAILABILITY" not in steps["system"]
    _, points = _run("talking_points", payload={"value": ["x"], "sources": []})
    assert "WHAT CUBIE ALREADY FLAGGED" not in points["prompt"]


# ─── Product knowledge (battle brief only) ─────────────────────────────────


class _FakeCatalog:
    _ROWS = {
        "Cloud Cost Review": {
            "name": "Cloud Cost Review",
            "description": "Finds idle spend.",
            "category": "Cloud",
            "price": "$900",
        }
    }

    def get_product_details(self, name):
        return self._ROWS.get(name, {})


def test_battle_rewrite_gets_the_product_knowledge_the_brief_had():
    with patch(
        "rubik_core.retrieval.product_catalog.get_product_catalog",
        side_effect=AssertionError("details were saved — no catalog read"),
    ):
        _, captured = _run("talking_points", payload={"value": ["x"], "sources": []})
    assert (
        "## PRODUCT KNOWLEDGE\n"
        "- Cloud Cost Review: Finds idle non-prod spend.\n"
        "  * Category: Cloud"
    ) in captured["prompt"]


def test_old_rows_with_product_names_only_are_looked_up_in_the_catalog():
    old = _bundle(matched_products=["Cloud Cost Review", "Retired Product"])
    old.pop("matched_product_details")
    with patch(
        "rubik_core.retrieval.product_catalog.get_product_catalog",
        return_value=_FakeCatalog(),
    ):
        _, captured = _run(
            "product_fit_analysis", payload={"value": "x", "sources": []}, bundle=old
        )
    assert (
        "## PRODUCT KNOWLEDGE\n"
        "- Cloud Cost Review: Finds idle spend.\n"
        "  * Category: Cloud\n"
        "- Retired Product"
    ) in captured["prompt"]
    assert "$900" not in captured["prompt"]


def test_rows_saved_with_the_old_empty_product_keys_are_looked_up_again():
    """Rows saved in an older shape ("persona" and "value", always empty) are
    re-read from the catalog rather than showing nothing."""
    old = _bundle(
        matched_product_details=[
            {
                "name": "Cloud Cost Review",
                "description": "Finds idle spend.",
                "persona": None,
                "value": None,
            }
        ]
    )
    with patch(
        "rubik_core.retrieval.product_catalog.get_product_catalog",
        return_value=_FakeCatalog(),
    ):
        _, captured = _run(
            "product_fit_analysis", payload={"value": "x", "sources": []}, bundle=old
        )
    assert "  * Category: Cloud\n" in captured["prompt"]
    assert "Why it fits" not in captured["prompt"]
    assert "None" not in captured["prompt"].split("## PRODUCT KNOWLEDGE")[1][:200]


def test_a_catalog_that_will_not_load_still_lists_the_names():
    old = _bundle()
    old.pop("matched_product_details")
    with patch(
        "rubik_core.retrieval.product_catalog.get_product_catalog",
        side_effect=RuntimeError("catalog missing"),
    ):
        result, captured = _run(
            "product_fit_analysis", payload={"value": "ok", "sources": []}, bundle=old
        )
    assert result.value == "ok"
    assert "## PRODUCT KNOWLEDGE\n- Cloud Cost Review\n" in captured["prompt"]


def test_ea_rewrite_gets_no_product_knowledge():
    payload = {"value": "x", "sources": []}
    # The same section for a sales user carries the block, so its absence
    # below is the executive-assistant path leaving it out, not a renamed
    # heading the assertion no longer sees.
    _, sales = _run("product_fit_analysis", payload=payload)
    assert rw.PRODUCT_KNOWLEDGE_HEADING in sales["prompt"]
    assert "Finds idle non-prod spend." in sales["prompt"]

    _, captured = _run(
        "product_fit_analysis",
        payload=payload,
        is_sales_user=False,
        is_internal=True,
    )
    assert rw.PRODUCT_KNOWLEDGE_HEADING not in captured["prompt"]
    assert "Finds idle non-prod spend." not in captured["prompt"]


def test_attendee_intel_adds_its_block_and_clauses():
    _, captured = _run(
        "stakeholder_map",
        payload={"value": [{"name": "R", "email": EXTERNAL}], "sources": []},
        bundle=_bundle(attendee_intel=[_intel_dict()]),
    )
    assert "## ATTENDEE INTELLIGENCE" in captured["prompt"]
    assert 'use it verbatim as "role"' in captured["prompt"]
    assert "Include the ATTENDEE INTELLIGENCE URLs" in captured["prompt"]
    assert "ATTENDEE INTELLIGENCE is the researched public record" in captured["system"]
    assert "already been researched" in captured["prompt"]


def test_the_target_section_appears_once_and_the_others_are_context():
    _, captured = _run("executive_summary", payload={"value": "x", "sources": []})
    prompt = captured["prompt"]
    assert prompt.count("UNIQUE-SUMMARY-MARK") == 1
    assert "## THE SECTION TO REWRITE: Executive Summary" in prompt
    rest = prompt.split("## THE REST OF THE CURRENT BRIEF")[1].split(
        "## THE SECTION TO REWRITE"
    )[0]
    assert OBJECTIVE_TEXT in rest
    assert "### Suggested Agenda\n- Intros\n- Spend review\n- Next steps" in rest
    assert "### Executive Summary" not in rest
    # Empty sections are not sent at all.
    assert "### Recent Interactions" not in rest


def test_web_search_only_for_the_battle_brief():
    _, battle = _run("executive_summary", payload={"value": "x", "sources": []})
    assert (
        battle["config"].tools and battle["config"].tools[0].google_search is not None
    )
    assert battle["config"].response_mime_type is None
    assert "Elite Strategic Sales Analyst" in battle["system"]
    assert "## EXTERNAL RESEARCH" in battle["prompt"]

    _, ea = _run(
        "executive_summary",
        payload={"value": "x", "sources": []},
        is_sales_user=False,
        is_internal=False,
    )
    assert not ea["config"].tools
    assert ea["config"].response_mime_type == "application/json"
    assert "World-Class Executive Assistant" in ea["system"]
    assert "## CONTEXTUAL DATA" in ea["prompt"]
    assert "## EXTERNAL RESEARCH" not in ea["prompt"]

    # A Sales reader in an internal meeting gets the EA brief, no search.
    _, internal = _run(
        "executive_summary",
        payload={"value": "x", "sources": []},
        is_sales_user=True,
        is_internal=True,
    )
    assert not internal["config"].tools


def test_labels_follow_is_internal():
    _, captured = _run(
        "client_profile",
        payload={"value": "x", "sources": []},
        is_sales_user=False,
        is_internal=True,
    )
    assert "## THE SECTION TO REWRITE: Relationship Context" in captured["prompt"]
    assert (
        "Summary of the reader's history with these individuals." in captured["prompt"]
    )


def test_factual_reminder_only_for_factual_sections():
    _, factual = _run("recent_interactions", payload={"value": "x", "sources": []})
    assert "FACTUAL SECTION" in factual["prompt"]

    _, editorial = _run("likely_objections", payload={"value": "x", "sources": []})
    assert "FACTUAL SECTION" not in editorial["prompt"]


def test_personalization_and_wgll_are_appended_to_the_system_instruction():
    _, captured = _run(
        "talking_points",
        payload={"value": ["a"], "sources": []},
        personalization_block="## ABOUT THE READER\nPrefers bullets.",
        wgll_context="## WHAT GOOD LOOKS LIKE\nShort.",
    )
    system = captured["system"]
    assert system.index("GROUNDING RULES") < system.index("## ABOUT THE READER")
    assert system.index("## ABOUT THE READER") < system.index("## WHAT GOOD LOOKS LIKE")


def test_old_bundle_without_signal_or_prior_meeting_blocks_still_works():
    old = _bundle()
    for key in (
        "signal_blocks",
        "prior_meetings_block",
        "account_snapshot",
        "signals_used",
    ):
        old.pop(key)
    result, captured = _run(
        "executive_summary", payload={"value": "ok", "sources": []}, bundle=old
    )
    assert result.value == "ok"
    assert "CRM ACCOUNT RECORD" not in captured["system"]
    assert "YOUR OPEN TO-DOS" not in captured["system"]
    assert "PRIOR MEETINGS WITH THESE ATTENDEES" not in captured["system"]
    assert "PRIOR MEETINGS WITH THESE ATTENDEES" not in captured["prompt"]
    assert "Transcript 2026-07-01" in captured["prompt"]


def test_deep_prep_shaped_bundle_still_works():
    deep = {
        "title": "Acme / Northwind — GCP review",
        "past_meetings_report": "Old notes.",
    }
    result, captured = _run(
        "stakeholder_map",
        payload={
            "value": [{"name": "Riley Fontaine", "email": EXTERNAL}],
            "sources": [],
        },
        bundle=deep,
    )
    assert json.loads(result.value)[0]["email"] == EXTERNAL
    # No invite saved → the people the brief already lists stand in for it.
    assert f"- Riley Fontaine <{EXTERNAL}>" in captured["prompt"]
    assert "invite was not saved" in captured["prompt"]
    assert "Old notes." in captured["prompt"]


def test_missing_bundle_works_from_the_brief_alone():
    result, captured = _run(
        "talking_points",
        payload={"value": ["Open on idle non-prod spend."], "sources": []},
        bundle=None,
    )
    assert result.value == ["Open on idle non-prod spend."]
    prompt = captured["prompt"]
    assert "original research this brief was built from is not available" in prompt
    assert "Acme / Northwind — GCP review" in prompt
    assert "## THE REST OF THE CURRENT BRIEF" in prompt
    # Nothing points at blocks that are not there.
    for absent in (
        "PRIOR MEETINGS",
        "RECENT EMAIL",
        "ATTENDEE INTELLIGENCE",
        "CRM ACCOUNT RECORD",
        "PRODUCT KNOWLEDGE",
    ):
        assert absent not in prompt
        assert absent not in captured["system"]


def test_no_user_facing_old_product_name():
    from backend.routers.admin import _FEATURE_LABELS

    assert _FEATURE_LABELS["meeting_prep_section"] == "Meeting Prep: section rewrites"
    keys = list(_FEATURE_LABELS)
    assert keys.index("meeting_prep_section") == keys.index("meeting_prep") + 1
    assert not LEGACY_PATTERN.search(_FEATURE_LABELS["meeting_prep_section"])
    for text in (MSG_UNAVAILABLE, MSG_UNUSABLE):
        assert not LEGACY_PATTERN.search(text) and PRODUCT_NAME in text


def test_grounding_rules_match_the_full_brief_word_for_word():
    """Drift guard: a rewrite must never run under looser rules than the
    brief it edits. If this fails, copy the new wording into rewrite.py."""
    home = (Path(__file__).resolve().parents[1] / "routers" / "home.py").read_text(
        encoding="utf-8"
    )
    for rules in (rw.BATTLE_GROUNDING_RULES, rw.EA_GROUNDING_RULES):
        for line in rules.splitlines():
            assert line in home, f"grounding rule drifted from home.py: {line[:80]}"

    # The signal-block rules: both personas of the full brief take them from
    # signals.prompt_parts, and so does the rewrite, for the same blocks.
    assert home.count('{sig["grounding"]}') == 2
    for battle in (True, False):
        _, captured = _run_all_signals(
            "executive_summary", battle=battle, payload={"value": "x", "sources": []}
        )
        rules = signals.grounding_rules(_ALL_SIGNAL_BLOCKS)
        assert rules and rules in captured["system"]

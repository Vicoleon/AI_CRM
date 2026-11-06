"""Prompts are industry-neutral and read the company profile (Review Focus 1).

Cubie ships to any company, so no prompt may assume an industry, and every
prompt that names the company reads it from the workspace's profile
(``rubik_core/company/profile.py``). Built with an EMPTY profile, each prompt
below must still read naturally: it says "the company", and it never carries
"None", a raw ``{company}`` token, or the old industry's words. Built with a
profile, the company's name and description appear.

The prompts pinned here are the ones a prospect's words or a company's facts
flow through: the chat system prompt (with the capability blocks that ship
with its tools, and the HubSpot-only chat), the X-Ray research prompt, the
call-capture extraction prompt, the CRM Sync prompt and the proposal prompts,
plus the voice prompts (dictation and spoken commands, whose vocabulary and
people lists are the company's). None of them may name the old company, the
old product or its clients: with a saved profile that would put two different
companies in one prompt, and the assistant is called Cubie (``PRODUCT_NAME``
from ``rubik_core.brand``). The worked examples inside prompts (Loose Ends,
open loops, Team Builder) come from no particular industry.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone

import pytest

from backend.tests._legacy_brand import LEGACY_PATTERN
from rubik_core.brand import PRODUCT_NAME
from rubik_core.company.profile import CompanyProfile

EMPTY = CompanyProfile()
ACME = CompanyProfile(name="Acme Corp", industry="Industrial cleaning")

#: Never in a neutral prompt. Case-sensitive "None" (a Python None leaking
#: into text).
NEVER = ("None", "{company}")
#: The old company's industry (mortgage QC and compliance), as whole words in
#: any casing: its trade words, its regulators and its kinds of audit.
OLD_INDUSTRY = re.compile(
    r"(?i)\b(?:mortgages?|lenders?|lending|loans?|underwrit\w*|(?:sub)?servicers?"
    r"|MERS|HMDA|CFPB|Fannie Mae|Freddie Mac|Ginnie Mae|agency exams?)\b"
)


def _chat(profile: CompanyProfile) -> str:
    """The coordinator's prompt, plus every capability block that ships with a
    tool set (scheduled tasks, to-dos, meeting skills) and the HubSpot-only
    chat's instruction."""
    from rubik_core.agent_runtime import coordinator, hubspot_only_chat

    agent = coordinator.build_coordinator(
        user_email="rep@acme.example", company_profile=profile
    )
    return "\n".join(
        [
            agent.static_instruction,
            agent.instruction,
            coordinator.SCHEDULED_TASKS_INSTRUCTION,
            coordinator.TODOS_INSTRUCTION,
            coordinator.MEETING_SKILLS_INSTRUCTION,
            hubspot_only_chat.INSTRUCTION,
        ]
    )


def _xray(profile: CompanyProfile) -> str:
    from rubik_core.xray import prompts

    system, prompt = prompts.build_web_prompt(
        "Globex", "globex.example", profile=profile
    )
    return f"{system}\n{prompt}\n{prompts.analyst_persona(profile)}"


def _call_capture(profile: CompanyProfile) -> str:
    from rubik_core.call_capture import prompts

    system, prompt = prompts.extraction_prompt(
        transcript_block="[1] Pat (Globex): We would like a quote by Friday.",
        source="meet",
        allowed_values={},
        stage_signals=("sow_sent", "verbal_yes"),
        company_name="Globex",
        ae_email="rep@acme.example",
        call_date="2026-10-28",
        profile=profile,
    )
    return f"{system}\n{prompt}"


def _crm_sync(profile: CompanyProfile) -> str:
    from rubik_core.crm_sync import prompts

    bundle = {
        "meeting": {
            "title": "Globex discovery",
            "date": "2026-10-28",
            "ae_email": "rep@acme.example",
            "ae_owner_id": "42",
            "external_attendees": ["pat@globex.example"],
        },
        "contacts": [],
        "companies": [],
        "deals": [],
        "pipeline_stages": {},
        "transcript_text": "[1] Pat: We would like a quote by Friday.",
    }
    system, prompt = prompts.extraction_prompt(bundle, profile=profile)
    return f"{system}\n{prompt}"


def _proposal(profile: CompanyProfile) -> str:
    from rubik_core.proposals import prompts

    return "\n".join(
        [
            prompts.brief_system(profile, []),
            prompts.draft_system(profile),
            prompts.revise_system(profile),
        ]
    )


def _voice(profile: CompanyProfile) -> str:
    """Dictation and spoken-command instructions, plus every function
    description the command reader is sent (``heard`` and Ask included)."""
    from rubik_core.voice import commands, transcribe

    context = commands.CommandContext(
        speaker="Pat Doe",
        today=date(2026, 10, 28),
        current_quarter="2026-Q4",
        teams=("Sales",),
        destinations=(commands.Destination(path="/", label="Home"),),
        people=("Pat Doe", "Sam Roe"),
        record_kinds=(("xray", "a company's X-Ray"),),
    )
    functions = commands.build_functions(context.destinations, context.record_kinds)
    return "\n".join(
        [
            transcribe.build_instructions(people=context.people, profile=profile),
            commands.build_instructions(context, spoken=True, profile=profile),
            json.dumps(functions),
            json.dumps(commands._answer_functions()),
        ]
    )


BUILDERS = {
    "chat": _chat,
    "xray": _xray,
    "call_capture": _call_capture,
    "crm_sync": _crm_sync,
    "proposal": _proposal,
    "voice": _voice,
}


def _old_brand(text: str) -> list[str]:
    """The old company, product names, clients and ids found in ``text``."""
    return [m.group() for m in LEGACY_PATTERN.finditer(text)]


def _old_industry(text: str) -> list[str]:
    return [m.group() for m in OLD_INDUSTRY.finditer(text)]


@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_an_empty_profile_reads_naturally(name):
    text = BUILDERS[name](EMPTY)
    for token in NEVER:
        assert token not in text, (name, token)
    assert _old_industry(text) == [], name
    assert "the company" in text.lower(), name
    assert "ABOUT THE COMPANY" not in text, name
    assert _old_brand(text) == [], name


@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_a_saved_profile_names_the_company(name):
    text = BUILDERS[name](ACME)
    assert "Acme Corp" in text, name
    assert "Industrial cleaning" in text, name
    for token in NEVER:
        assert token not in text, (name, token)
    assert _old_brand(text) == [], name


@pytest.mark.parametrize("name", ["chat", "voice"])
@pytest.mark.parametrize("profile", [EMPTY, ACME], ids=["empty", "acme"])
def test_the_assistant_is_called_by_the_product_name(name, profile):
    """The chat prompt says who the assistant is, and the voice prompts spell
    the product's name for dictation and say whose app a command is for. Both
    use ``PRODUCT_NAME``."""
    assert PRODUCT_NAME in BUILDERS[name](profile), name


def _loose_ends_judge(monkeypatch: pytest.MonkeyPatch) -> str:
    """The Loose Ends judge reads the saved profile itself."""
    from rubik_core.loop_closure import judge

    monkeypatch.setattr(judge, "get_company_profile", lambda *a, **k: EMPTY)
    return judge.build_prompt([], [], owner_name="Pat Doe", today="2026-10-28")


def _open_loops(monkeypatch: pytest.MonkeyPatch) -> str:
    from backend.services import open_loops

    return open_loops._prompt(
        [],
        user_first_name="Pat",
        days=7,
        now=datetime(2026, 10, 28, tzinfo=timezone.utc),
    )


#: Prompts with worked examples that take no profile. (The chat prompt's
#: examples are checked with the builders above.)
EXAMPLE_PROMPTS = {
    "loose_ends_judge": _loose_ends_judge,
    "open_loops": _open_loops,
}


@pytest.mark.parametrize("name", sorted(EXAMPLE_PROMPTS))
def test_worked_examples_come_from_no_particular_industry(name, monkeypatch):
    """A prompt's examples ("Asks for updated … pricing before Friday", "e.g.
    '… SOW'") teach the model what a normal case looks like. Taken from the
    old company's audits and regulators, they tell every other company's model
    that it works in that industry."""
    text = EXAMPLE_PROMPTS[name](monkeypatch)
    assert _old_industry(text) == [], name
    assert _old_brand(text) == [], name


def test_the_profile_is_read_from_the_database_when_not_passed(monkeypatch):
    """A caller that passes no profile gets the saved one."""
    from rubik_core.proposals import prompts as proposal_prompts
    from rubik_core.xray import prompts as xray_prompts

    monkeypatch.setattr(xray_prompts, "get_company_profile", lambda *a, **k: ACME)
    system, _ = xray_prompts.build_web_prompt("Globex", "globex.example")
    assert "Acme Corp" in system
    assert "Acme Corp" in proposal_prompts.draft_system(ACME)


def test_voice_names_the_saved_company_wherever_it_names_one():
    """Spelling hints and the people list name the profile's company, never a
    second one; with no profile they say "the company"."""
    text = _voice(ACME)
    assert "the way Acme Corp writes them" in text
    assert "People at Acme Corp" in text
    empty = _voice(EMPTY)
    assert "the way the company writes them" in empty
    assert "People at the company" in empty


def test_the_work_style_interviewer_names_no_old_brand():
    """A fixed system instruction (cached across every interview turn), so it
    says "the company" rather than reading the profile."""
    from rubik_core.smart_teams import prompts

    assert _old_brand(prompts.INTERVIEWER_SYSTEM) == []
    assert "the company's Work Style Interview" in prompts.INTERVIEWER_SYSTEM

"""The one rule that spans ST-7 and ST-9: **nobody's colleague is described by a
model.**

No sentence that characterises a NAMED person may be model-written in a
multi-person Smart Teams document. Not "is screened", not "is usually caught" —
not written. The pair guide's bodies and the Team Builder's per-person lines are
rendered from authored copy over the typed result, and the model's remaining job
in this feature is the work it can do safely: the interview conversation, the
strengths extraction, the single-subject personal guide, the GROUP sentences that
name nobody, and choosing WHICH candidate pairings or coverage gaps are worth
surfacing at all.

This file exists because that rule lives in two modules owned by two different
agents, and neither of their own test files can hold it. ``comparison.py`` cannot
assert anything about ``team_builder.py`` and vice versa, so a fix in one and a
regression in the other is exactly the shape that ships. Everything here is the
join.

Why the rule is structural rather than a screen
-----------------------------------------------

The wave-2 verifier got ten fabricated sentences about named colleagues into
company-visible documents, and every one of them was CONTAINED by the
novel-content floor. That floor allowed any word from
``guide_prompts.AUTHORED_VOCABULARY`` — 1670 stems harvested across all six
instruments — so multi-person fabrication never needed a new word. It worked by
REARRANGING words already in the corpus. "Alice Person is a risk on this
project." scored zero novel words.

Narrowing the allowed set to the slot's own fact (``allow_authored_vocabulary=
False``) fixes that for the team write-up, and this file pins it. On its own it
would NOT have fixed the pair guide, and that measurement is the reason the fix
there had to be structural. ``comparison._pair_fact`` used to return both
subjects' authored sentences and both display names in one string, on the
argument that the containment rules search the fact and a body naming either
person needs both names in it — which made every permutation of who-said-what
contained BY CONSTRUCTION. Pinned in
``test_strict_containment_alone_cannot_stop_an_owner_swap``, which reconstructs
that combined shape from the real authored copy: each of the three proven owner
swaps scores ZERO novel words against it even in STRICT mode. There is no
threshold that separates "Emery gets blunter under pressure" from "Shay gets
blunter under pressure" when the fact contains both sentences and both names. A
screen cannot do this. Only not asking the model can.

So the pair guide was rebuilt rather than screened harder. ``PairSection`` splits
the body into ``lead`` — the relationship clause, the one string a model may
re-voice — and ``evidence``, the two quoted answers with their owner attached,
which is authored by ``comparison._authored_evidence`` and reachable from no
model path; ``body`` is a read-only property over the two. The fact is now the
clause ALONE, which is what makes a string naming either person novel content
again. Both halves of that are asserted below, and so is the strict containment
that backs them up.

And on the team side, strict containment is necessary but expensive, which is the
other half of the argument for authoring: with ``MAX_NOVEL_CONTENT_WORDS == 0``,
three of the six legitimate single-subject paraphrases measured in
``test_the_lenient_default_still_accepts_single_subject_prose`` would ALSO be
rejected under strict mode. Strict containment over per-person prose is not
"model prose, safely screened"; it is authored fallback with a Vertex call in
front of it.

What is asserted, and in what order
-----------------------------------

1. **The regression set.** All ten strings the verifier actually got published,
   verbatim from the findings: three owner swaps in the pair guide (finding 1)
   and seven per-person judgements in the Team Builder write-up (finding 2). Each
   must be impossible or rejected, checked at the SCREEN and again at the
   RENDERED DOCUMENT — because a screen that rejects and a renderer that never
   asks are different guarantees, and the second is the one that holds when
   somebody adds a new slot.
2. **Strict containment, both directions.** It rejects what the lenient floor
   allowed on multi-person facts, AND the lenient default still accepts
   single-subject prose, so the personal guide is not collaterally tightened by a
   fix aimed at documents about two or more people.
3. **Matched pronoun/name pairs.** A pair guide names the other person by design
   ("Emery's: …"), so a screen anchored to ``he|she|they|you`` is bypassed by the
   grammar the document itself prefers. Every family is asserted in both
   grammars, and both must reach the same verdict.
4. **A guard on the guard.** ``AUTHORED_VOCABULARY`` must still be large and
   still be harvested from the instruments. If somebody empties it, this file has
   to fail rather than pass vacuously — an empty vocabulary makes the containment
   assertions trivially true for the wrong reason.
5. **Neither multi-person module can reach the companywide corpus.** Also a
   two-module property, and also un-assertable from either owner's own file.

No network, and no LLM anywhere. Every writer in this file is a plain callable
that returns a dict, which is the point: the question is what the product code
does with what a writer hands it, and a real model is the one thing that cannot
answer that question repeatably.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from rubik_core.database.models import Base, User
from rubik_core.database.smart_teams import SmartTeamsProfile
from rubik_core.smart_teams import (
    comparison,
    contracts,
    guide,
    guide_prompts,
    team_builder,
    team_contracts,
    team_prompts,
)
from rubik_core.smart_teams import comparison_prompts as cp
from rubik_core.smart_teams import skill_taxonomy as taxonomy
from rubik_core.smart_teams import skills as skill_store
from rubik_core.smart_teams.instruments import (
    big_five,
    disc,
    enneagram,
    motivators,
    strengths,
    type16,
)

# ═════════════════════════════════════════════════════════════════════════════
# THE REGRESSION SET
# ═════════════════════════════════════════════════════════════════════════════

#: Finding 1 (CRITICAL). Three strings a stub writer put into a REAL pair guide
#: for Shay (DISC "D") and Emery (DISC "C+S"), both personal guides published. Each
#: one is an OWNER SWAP: an authored answer attributed to the person who did not
#: give it. Observed at the time: ``PairGuide.fallbacks == {}``, every section
#: ``model_written = True``, row ``status = "ready"``, and all three strings
#: verbatim in ``document.content_md`` under the friction heading.
#:
#: The slot each one was planted in is recorded because it matters to the
#: measurement below: an owner swap is built out of the words of its own slot's
#: fact, so containment has nothing to catch.
OWNER_SWAPS: tuple[tuple[str, str], ...] = (
    (
        "friction.stress_response.0",
        "Emery gets blunter and faster when a week goes badly and stops "
        "explaining the reasoning.",
    ),
    (
        "friction.communication_pace.2",
        "Emery thinks out loud and gains energy from the room; you think first "
        "and arrive with the conclusion.",
    ),
    (
        "friction.decision_style.4",
        "Shay weighs the effect on people heavily in a decision.",
    ),
)

#: Finding 2 (CRITICAL). Seven per-person verdicts a stub rationale writer got
#: rendered in the Team Builder write-up, under the heading "Write-up
#: (descriptive only — none of it rates anybody):". Every one passed
#: ``screen_rationale`` on three or four separate REAL slots, and the same string
#: is what ``TeamBuilderChatTools.build_project_team`` hands the coordinator and
#: what ``POST /api/smart-teams/teams`` returns as ``report``/``write_up``.
PER_PERSON_JUDGEMENTS: tuple[str, ...] = (
    "Alice Person is the senior voice in this group.",
    "Give the lead to Alice Person.",
    "Carla Person is likely to slow the group down.",
    "Carla Person is a risk on this project.",
    "Alice Person is the anchor of this group.",
    "Alice Person deserves the lead on this.",
    "Carla Person is the natural owner of the GAAP work.",
)

#: Every proven leak, for the assertions that do not care which document it came
#: from. A string from either half must be impossible in EITHER document: the two
#: features describe the same colleagues to the same readers.
ALL_LEAKS: tuple[str, ...] = tuple(text for _slot, text in OWNER_SWAPS) + (
    PER_PERSON_JUDGEMENTS
)


def test_the_regression_set_is_the_ten_measured_strings():
    """A guard on the corpus itself, before anything is asserted with it.

    The value of this file is that the strings are the ones that really shipped,
    not a plausible-looking set somebody wrote later. Trimming the corpus is the
    cheapest way to make every test below pass, so the count is pinned.
    """
    assert len(OWNER_SWAPS) == 3, "finding 1 planted three owner swaps"
    assert len(PER_PERSON_JUDGEMENTS) == 7, "finding 2 planted seven judgements"
    assert len(ALL_LEAKS) == 10
    assert len(set(ALL_LEAKS)) == 10, "the ten strings must be ten distinct strings"


# ═════════════════════════════════════════════════════════════════════════════
# Fixtures — the pair side, with no database at all
# ═════════════════════════════════════════════════════════════════════════════
# ``build_pair_guide`` takes two ``SmartTeamsProfile`` rows and reads their
# framework columns. Nothing in this half persists anything, so the rows are
# built unbound: a session would add a failure mode (flush order, constraint
# names) to a test that is about prose.

SHAY = "shay@acme.example"
EMERY = "emery@acme.example"
ALICE = "alice@acme.example"
CARLA = "carla@acme.example"
TODAY = date(2026, 8, 20)


def _envelope(framework: str, layer: dict | None, *, reportable: bool = True) -> dict:
    payload = {
        contracts.KEY_SCHEMA_VERSION: contracts.PAYLOAD_SCHEMA_VERSION,
        contracts.KEY_FRAMEWORK: framework,
        contracts.KEY_REPORTABLE: reportable,
        contracts.KEY_CONFIDENCE: "high" if reportable else "none",
        contracts.KEY_REASONS: [] if reportable else [contracts.REASON_NOT_TAKEN],
        contracts.KEY_HEDGE: None,
        contracts.KEY_INSTRUMENT_VERSION: None,
        contracts.KEY_SCORED_AT: "2026-08-20T00:00:00",
        contracts.KEY_LAYER: layer if reportable else None,
    }
    contracts.validate_envelope(payload)
    return payload


def _disc_layer(style_key: str) -> dict:
    style = disc.WORK_STYLES[style_key]
    return {
        "layer": "disc",
        "assignment": {"style": style.as_dict(), "primary": style_key},
        "quality": {},
    }


_THEME_IDS = list(strengths.THEMES)
_AXES = list(motivators.MOTIVATORS)


def _strengths_layer(theme_ids: Sequence[str]) -> dict:
    return {
        "layer": "strengths",
        "themes": [
            {
                "theme_id": theme_id,
                "name": strengths.spec_for(theme_id).name,
                "cluster": strengths.spec_for(theme_id).cluster.value,
                "evidence_quote": "I rewrote the whole intake form that week.",
            }
            for theme_id in theme_ids
        ],
    }


def _motivators_layer(top: Sequence[str], bottom: str) -> dict:
    return {
        "layer": "motivators",
        "ranking": list(top) + [bottom],
        "top_two": list(top),
        "bottom": bottom,
        "bottom_is_contested": False,
        "near_ties": [],
        "headline": "What makes the work worth it, in my own words.",
    }


def _enneagram_layer(number: int) -> dict:
    spec = enneagram.spec_for(number)
    return {
        "layer": "enneagram",
        "type_number": number,
        "type_display": spec.label,
        "center": spec.center,
        "headline": "My own read, and worth arguing with.",
    }


def _big_five_layer() -> dict:
    return {
        "instrument": "ipip_50",
        "domains": {},
        "unscored_domains": [],
        "quality": {},
        "bands_note": big_five.BANDS_ARE_NOT_PERCENTILES,
    }


#: The two profiles finding 1 was reproduced on: one clear-high four-letter
#: result against one clear-low, which is what makes six of the seven dimensions
#: produce a friction and gives the screens real slots to be tested against.
_CLEAR_HIGH = {
    big_five.Domain.EXTRAVERSION: 82.0,
    big_five.Domain.OPENNESS: 80.0,
    big_five.Domain.AGREEABLENESS: 18.0,
    big_five.Domain.CONSCIENTIOUSNESS: 78.0,
}
_CLEAR_LOW = {
    big_five.Domain.EXTRAVERSION: 18.0,
    big_five.Domain.OPENNESS: 20.0,
    big_five.Domain.AGREEABLENESS: 82.0,
    big_five.Domain.CONSCIENTIOUSNESS: 22.0,
}


def _type16_layer(scores: Mapping[big_five.Domain, float]) -> dict:
    calls = []
    for axis, spec in type16.AXIS_SPECS.items():
        score = scores[spec.source_domain]
        band = big_five.band_for(score)
        calls.append(
            type16.LetterCall(
                axis=axis,
                letter=spec.high_letter if score > type16.MIDPOINT else spec.low_letter,
                other_letter=(
                    spec.low_letter if score > type16.MIDPOINT else spec.high_letter
                ),
                source_domain=spec.source_domain,
                source_score=score,
                source_band=band,
                confidence=type16.CONFIDENCE_BY_BAND[band],
            )
        )
    return type16.Type16Result(
        calls=tuple(calls), unresolved_axes=(), suppressed_reason=None
    ).as_dict()


def _unbound_profile(email: str, frameworks: dict, *, version: int = 1):
    columns = {
        name: contracts.not_taken_envelope(name) for name in contracts.FRAMEWORKS
    }
    columns.update(frameworks)
    return SmartTeamsProfile(
        id=f"prof-{email.split('@')[0]}-{version}",
        user_email=email,
        version=version,
        status="current",
        is_current=True,
        scored_at=datetime(2026, 8, 20),
        **columns,
    )


@pytest.fixture
def pair_profiles():
    """Shay and Emery, both fully reportable, exactly as finding 1 measured them."""
    shay = _unbound_profile(
        SHAY,
        {
            "disc": _envelope("disc", _disc_layer("D")),
            "strengths": _envelope("strengths", _strengths_layer(_THEME_IDS[:4])),
            "motivators": _envelope(
                "motivators", _motivators_layer([_AXES[0], _AXES[1]], _AXES[2])
            ),
            "enneagram": _envelope("enneagram", _enneagram_layer(3)),
            "type16": _envelope("type16", _type16_layer(_CLEAR_HIGH)),
            "big_five": _envelope("big_five", _big_five_layer()),
        },
    )
    emery = _unbound_profile(
        EMERY,
        {
            "disc": _envelope("disc", _disc_layer("C+S")),
            "strengths": _envelope("strengths", _strengths_layer(_THEME_IDS[7:11])),
            "motivators": _envelope(
                "motivators", _motivators_layer([_AXES[2], _AXES[3]], _AXES[0])
            ),
            "enneagram": _envelope("enneagram", _enneagram_layer(6)),
            "type16": _envelope("type16", _type16_layer(_CLEAR_LOW)),
            "big_five": _envelope("big_five", _big_five_layer()),
        },
    )
    return shay, emery


def _authored_pair(pair_profiles):
    """The pair guide with NO writer — the authored document, which always ships."""
    shay, emery = pair_profiles
    return comparison.build_pair_guide(shay, emery, name_a="Shay", name_b="Emery")


#: Whether the pair builder still has a seam a model can write a body through.
#: If the seam is gone the strings in this file are impossible rather than
#: rejected, which is the stronger outcome — but it has to be ASSERTED rather
#: than assumed, or a renamed parameter would silently skip every injection test
#: below and they would all pass having tested nothing.
_PAIR_WRITER_PARAM = (
    "prose_writer" in inspect.signature(comparison.build_pair_guide).parameters
)


def _pair_with_writer(pair_profiles, writer):
    """Build the pair guide, driving the model seam if there still is one."""
    shay, emery = pair_profiles
    if not _PAIR_WRITER_PARAM:
        return comparison.build_pair_guide(shay, emery, name_a="Shay", name_b="Emery")
    return comparison.build_pair_guide(
        shay, emery, name_a="Shay", name_b="Emery", prose_writer=writer
    )


def _pair_slot(section) -> cp.PairSlot:
    """The screening unit for one real section, built the way ``comparison`` does.

    ``body`` is the section's ``lead`` and not its ``body``, because the lead is
    the authored fallback for the one string a model may write — matched to
    ``build_pair_guide``'s own slot construction so this file screens what the
    product screens.
    """
    return cp.PairSlot(
        slot_id=section.slot_id,
        intent=section.intent,
        fact=section.fact,
        heading=section.heading,
        body=section.lead,
        reader_name="Shay",
        about_name="Emery",
        open_frameworks=frozenset(),
    )


#: Every key a pair writer has ever been read for. ``build_pair_guide`` reads
#: only ``"lead"`` today — the clause being the one string a model may write —
#: but a writer returning just that key would go back to testing nothing the day
#: somebody renames it, and the two injection tests below are the ones that
#: matter most in this file. So a planted string is returned under all of them.
_WRITER_KEYS: tuple[str, ...] = ("lead", "body", "heading", "text", "prose")


def _every_writer_key(text: str) -> dict[str, str]:
    """One planted string, offered under every key a writer might be read for."""
    return dict.fromkeys(_WRITER_KEYS, text)


def _old_shape_fact(section) -> str:
    """The fact ``_pair_fact`` used to return, reconstructed from the real section.

    ``f"{statement} {name_a}: {text_a} {name_b}: {text_b}"`` — the relationship
    clause plus both display names plus both authored answers, all in one string.
    Rebuilt here rather than pasted so it moves with the authored copy, and kept
    at all because it is the only way to keep measuring the thing that made the
    structural fix necessary after the shape itself is gone.
    """
    return f"{section.lead} {section.evidence.replace('Yours:', 'Shay:')}"


# ═════════════════════════════════════════════════════════════════════════════
# 1a. The three owner swaps (finding 1) cannot reach a pair guide
# ═════════════════════════════════════════════════════════════════════════════


def test_strict_containment_alone_cannot_stop_an_owner_swap(pair_profiles):
    """The measurement that makes the structural fix load-bearing. Pinned so the
    authored rendering is never "simplified" back into a screen.

    ``comparison._pair_fact`` USED to return the relationship clause plus both
    display names plus both authored answers, on the sound-looking argument that
    the containment rules search the fact, so anything a body might legitimately
    mention had to be in it. The consequence was that every permutation of WHO
    SAID WHAT was contained by construction.

    Measured here against that reconstructed shape, in the STRICT mode
    (``allow_authored_vocabulary=False``) that is the strongest containment
    available: each owner swap scores ZERO novel words. Nothing about the
    sentence is novel; only the attribution is wrong, and attribution is not a
    word count. That is the whole argument for authoring the evidence instead of
    screening it, and it stays measured here so nobody re-derives the combined
    fact from first principles and re-opens the hole.
    """
    pair = _authored_pair(pair_profiles)
    by_id = {section.slot_id: section for section in (*pair.synergies, *pair.frictions)}
    for slot_id, swap in OWNER_SWAPS:
        assert slot_id in by_id, (
            f"{slot_id} is no longer a slot this pairing produces, so the "
            "measurement below has moved. Re-measure against the real slots "
            "rather than deleting the case."
        )
        combined = _old_shape_fact(by_id[slot_id])
        strict = guide_prompts.novel_content_words(
            swap, combined, allow_authored_vocabulary=False
        )
        assert strict == (), (
            f"{slot_id}: against a fact holding both subjects' answers the owner "
            f"swap now scores {strict}. Check _old_shape_fact still reconstructs "
            "the combined shape before believing containment got better at "
            "owners — it cannot, because an owner is not a word."
        )


def test_the_narrowed_pair_fact_carries_no_name_and_no_quoted_answer(pair_profiles):
    """The structural property that replaced the screen, asserted on the data.

    ``_pair_fact`` now returns the relationship clause alone. That is what makes
    a body naming either person or quoting either answer novel content, so it is
    worth pinning directly rather than only through its effects: a fact that
    quietly grew a name back would restore finding 1 with every test below still
    passing, because the swaps would go back to being "contained".
    """
    pair = _authored_pair(pair_profiles)
    sections = (*pair.synergies, *pair.frictions)
    assert sections, "the fixture produced no sections, so nothing was tested"
    for section in sections:
        for name in ("Shay", "Emery"):
            assert name not in section.fact, (
                f"{section.slot_id}: the fact names {name!r} again, so a model "
                f"string attaching an answer to them is contained: {section.fact!r}"
            )
        assert '"' not in section.fact, (
            f"{section.slot_id}: the fact carries a quoted answer, which is the "
            f"combined shape finding 1 rode in on: {section.fact!r}"
        )
        assert section.fact == section.lead, (
            f"{section.slot_id}: the fact and the authored clause have diverged. "
            "The model is asked to re-voice the clause and screened against the "
            "fact, so they have to be the same string."
        )


def test_every_proven_leak_is_refused_on_every_real_pair_slot(pair_profiles):
    """Defence in depth: all ten strings, against the pair guide's only screen.

    The primary control is that a model never writes the owner-bearing half at
    all, and the tests below assert that. This one asserts the net UNDER it:
    ``screen_pair_lead`` is what guards the one clause a model does write, and
    every string from both findings must be refused on every real slot — not on
    the one slot it was planted in.

    Both halves of the regression set are run against both documents, here and in
    ``test_no_per_person_judgement_survives_the_rationale_screen_on_any_slot``,
    because ST-7 and ST-9 describe the same colleagues to the same readers and a
    sentence one document refuses and the other admits is a hole either way.
    """
    pair = _authored_pair(pair_profiles)
    slots = [_pair_slot(section) for section in (*pair.synergies, *pair.frictions)]
    assert len(slots) >= 5, f"only {len(slots)} slots — the fixture went thin"
    for planted in ALL_LEAKS:
        for slot in slots:
            assert cp.screen_pair_lead(planted, slot) is not None, (
                f"screen_pair_lead accepted {planted!r} for slot {slot.slot_id!r}"
            )


def test_no_owner_swap_can_reach_a_rendered_pair_guide(pair_profiles):
    """The assertion that actually protects the reader.

    A writer returns each owner swap as both the heading and the body of the very
    slot it was planted in, plus every other slot for good measure. None of the
    three may appear anywhere in the rendered markdown — which is the string the
    document row stores and the reader sees.

    This is the design decision in one assertion: multi-person prose is authored,
    so a model's sentence about a named colleague has no way in. It holds whether
    the writer seam was removed or whether it survives behind screens.
    """
    pair = _authored_pair(pair_profiles)
    slot_ids = [section.slot_id for section in (*pair.synergies, *pair.frictions)]
    assert slot_ids, "the fixture produced no sections, so nothing was tested"

    def _writer(slots, **_kw):
        out: dict[str, dict[str, str]] = {}
        for index, slot in enumerate(slots):
            planted = OWNER_SWAPS[index % len(OWNER_SWAPS)][1]
            out[slot.slot_id] = _every_writer_key(planted)
        # And each swap on its own slot, which is where it was proven to land.
        for slot_id, planted in OWNER_SWAPS:
            out[slot_id] = _every_writer_key(planted)
        return out

    written = _pair_with_writer(pair_profiles, _writer)
    rendered = comparison.render_markdown(written)
    for _slot_id, planted in OWNER_SWAPS:
        assert planted not in rendered, (
            "an owner swap reached the rendered pair guide — a fabricated claim "
            f"about a named colleague, in a document they never see: {planted!r}"
        )
        for section in (*written.synergies, *written.frictions):
            assert planted not in section.body, f"{section.slot_id} body: {planted!r}"
            assert section.heading != planted, f"{section.slot_id} heading: {planted!r}"


def test_the_writer_seam_is_live_so_the_injection_tests_mean_something(pair_profiles):
    """Anti-vacuity. The one test here that FAILS if the model seam went away.

    Every injection test in this file drives ``prose_writer``. If the parameter
    were renamed, or the key the builder reads were renamed, those tests would
    still pass — having planted nothing. So: a writer returning a clause that
    ought to be ACCEPTED must actually reach the document.

    The accepted clause is each slot's own authored clause, which is contained in
    itself and passes every layer of ``screen_pair_lead``. If this test starts
    failing because the seam is gone, that is good news for the product and the
    injection tests below have become tautologies that should be deleted rather
    than kept as reassurance.
    """
    assert _PAIR_WRITER_PARAM, (
        "build_pair_guide no longer takes a prose_writer, so every injection "
        "test in this file is now planting into nothing. Delete them rather "
        "than leaving them green."
    )

    def _writer(slots, **_kw):
        return {slot.slot_id: {"lead": slot.fact} for slot in slots}

    written = _pair_with_writer(pair_profiles, _writer)
    sections = (*written.synergies, *written.frictions)
    assert any(section.model_written for section in sections), (
        "an acceptable clause never reached a section, so the builder is not "
        f"reading any key this file plants under ({_WRITER_KEYS!r}). The "
        "injection tests are passing vacuously — re-point them at the key "
        "build_pair_guide actually reads."
    )


def test_a_pair_evidence_half_is_authored_whatever_the_writer_returns(pair_profiles):
    """The structural property, probed with prose the screens cannot refuse.

    The writer returns each slot's own ``fact`` under every key. That string is
    contained in itself — zero novel words in either mode, no unsourced number,
    no framework the gate is closed for — so if a model string could reach the
    owner-bearing half at all, this one would. The ``evidence`` half must still
    be authored, byte for byte.

    Deliberately probed with something INNOCUOUS rather than with a violation: a
    document that refuses only the sentences somebody thought to plant is a
    blocklist, and the decision recorded in this file's docstring is that the
    model does not write these sentences at all.
    """
    authored = {
        section.slot_id: section.evidence
        for section in (
            *_authored_pair(pair_profiles).synergies,
            *_authored_pair(pair_profiles).frictions,
        )
    }

    def _writer(slots, **_kw):
        return {slot.slot_id: _every_writer_key(slot.fact) for slot in slots}

    written = _pair_with_writer(pair_profiles, _writer)
    for section in (*written.synergies, *written.frictions):
        assert section.evidence == authored[section.slot_id], (
            f"{section.slot_id}: the owner-attributed half came from the writer. "
            "The two quoted answers and the name on each are what a swap "
            "falsifies — see this file's docstring for why screening them "
            "instead does not work."
        )
        assert section.evidence in section.body, (
            f"{section.slot_id}: the rendered body no longer contains the "
            "authored evidence verbatim"
        )


def test_a_pair_section_body_cannot_be_assigned(pair_profiles):
    """The property is read-only, so "set the whole body" has no code path.

    ``PairSection.body`` is a property over ``lead`` and ``evidence`` rather than
    a field, which is what stops a future caller from assembling a body itself
    and putting a model's words where an owner is named. A dataclass field would
    make every guarantee in this file a convention.
    """
    section = (
        *_authored_pair(pair_profiles).synergies,
        *_authored_pair(pair_profiles).frictions,
    )[0]
    with pytest.raises(AttributeError):
        section.body = "Emery is the one who gets blunter under pressure."


# ═════════════════════════════════════════════════════════════════════════════
# Fixtures — the team side, which needs a database
# ═════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def team_db():
    """A real taxonomy, two real users, two published guides, two skills.

    In-memory SQLite with a ``StaticPool``, per the rest of this suite. No app and
    no router: ``answer_for`` and ``render_report`` are the surface under test,
    and both of the shipped callers (the endpoint and the chat tool) render the
    same string from them.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False)()
    taxonomy.seed_taxonomy(session)
    for email in (ALICE, CARLA):
        session.add(
            User(
                email=email,
                id=email.split("@")[0],
                name=email.split("@")[0].title() + " Person",
                department="Compliance",
                last_login=datetime(2026, 8, 1),
            )
        )
    session.commit()

    for email, style_key in ((ALICE, "D"), (CARLA, "S")):
        _publish_for_team(session, email, style_key)
    skill_store.record_declared_skills(session, ALICE, {"data_privacy": "expert"})
    skill_store.record_declared_skills(
        session, CARLA, {"financial_reporting": "working"}
    )
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _publish_for_team(session, email: str, style_key: str) -> None:
    """A scored current profile and an APPROVED personal guide for one person.

    Publication goes through ``guide.approve`` — the only writer of
    ``published = True`` — so the consent path and its CHECK constraint are both
    exercised, and ``published_layers`` really has a style to read.
    """
    theme = next(
        spec
        for spec in strengths.THEMES.values()
        if spec.cluster == strengths.Cluster.MOMENTUM
    )
    profile = SmartTeamsProfile(
        id=f"prof-{email.split('@')[0]}",
        user_email=email,
        version=1,
        status="current",
        is_current=True,
        disc=_envelope("disc", _disc_layer(style_key)),
        big_five=_envelope("big_five", None, reportable=False),
        type16=_envelope("type16", None, reportable=False),
        strengths=_envelope(
            "strengths",
            {
                "layer": "strengths",
                "themes": [
                    {
                        "theme_id": theme.theme_id,
                        "name": theme.name,
                        "cluster": theme.cluster.value,
                        "evidence_quote": "I rebuilt the pricing matrix.",
                    }
                ],
            },
        ),
        motivators=_envelope("motivators", None, reportable=False),
        enneagram=_envelope("enneagram", None, reportable=False),
        provenance={name: "interview" for name in contracts.FRAMEWORKS},
        scored_at=datetime(2026, 8, 1),
    )
    session.add(profile)
    session.flush()
    row = guide.ensure_guide_row(session, profile)
    row.document.status = "ready"
    row.document.content_md = "# How to work with them\n\nSomething authored."
    session.flush()
    guide.approve(session, row.document, approver_email=email)
    session.commit()


def _team_request() -> team_contracts.TeamRequest:
    """The ask finding 2 was reproduced on: both people's one confirmed skill."""
    return team_contracts.TeamRequest.from_phrases(
        requested_by=ALICE,
        required_phrases=["data privacy", "financial reporting"],
        optional_phrases=[],
        size=None,
        starts_on=None,
        ends_on=None,
        desired_clusters=[],
        raw_ask="get me a team for the agency exam",
    )


def _no_model(_slots, **_kw) -> dict:
    """The authored answer: the writer returns nothing usable."""
    return {}


# ═════════════════════════════════════════════════════════════════════════════
# 1b. The seven per-person judgements (finding 2) cannot reach a team report
# ═════════════════════════════════════════════════════════════════════════════


def test_no_per_person_judgement_survives_the_rationale_screen_on_any_slot(team_db):
    """Defence in depth: every planted verdict, against every REAL slot.

    ``screen_rationale`` is not the primary control any more — a slot whose fact
    names a colleague is never handed to the writer at all — but it is still the
    net under any future caller that assembles slots itself, and it is the thing
    that used to accept all seven of these on three or four slots each.

    Asserted against every slot rather than the four it was measured on: which
    slot a sentence lands in is a property of the ask, and a rule that holds on
    six slots and not the seventh is a rule that fails on somebody's real query.
    """
    answer = team_builder.answer_for(
        team_db, _team_request(), today=TODAY, rationale_writer=_no_model
    )
    slots = team_builder.rationale_slots(answer)
    assert len(slots) >= 8, f"only {len(slots)} slots — the fixture went thin"

    for planted in PER_PERSON_JUDGEMENTS:
        for slot in slots:
            reason = team_prompts.screen_rationale(
                planted, slot, allowed_names=answer.names
            )
            assert reason is not None, (
                f"screen_rationale accepted {planted!r} for slot "
                f"{slot.slot_id!r} — a judgement of a named colleague in a "
                "staffing answer their lead reads"
            )


def test_no_per_person_judgement_can_reach_a_rendered_team_report(team_db):
    """The reader-facing assertion, on the exact string both surfaces return.

    ``render_report`` is what ``POST /api/smart-teams/teams`` returns as
    ``report``/``write_up`` and what ``TeamBuilderChatTools.build_project_team``
    hands the coordinator, so this is the whole blast radius in one string.
    """
    request = _team_request()

    def _writer(slots, **_kw):
        return {
            slot.slot_id: PER_PERSON_JUDGEMENTS[index % len(PER_PERSON_JUDGEMENTS)]
            for index, slot in enumerate(slots)
        }

    answer = team_builder.answer_for(
        team_db, request, today=TODAY, rationale_writer=_writer
    )
    report = team_builder.render_report(answer)
    for planted in PER_PERSON_JUDGEMENTS:
        assert planted not in report, (
            f"a per-person verdict reached the team report: {planted!r}"
        )
    for line in answer.rationale:
        assert line.text not in PER_PERSON_JUDGEMENTS, line.slot_id


#: The slot families whose fact names a colleague. A GROUP slot names nobody and
#: the model may legitimately re-voice it; these are the ones it may not.
_PER_PERSON_SLOTS = ("covers:", "style:", "sole:", "availability:", "no_profile:")


def test_every_per_person_line_in_the_report_is_authored_copy(team_db):
    """Finding 7's honesty failure, pinned from the data the report is built from.

    The write-up used to print four per-person verdicts under a heading that said
    "descriptive only — none of it rates anybody". That heading is now a
    paragraph making an even stronger claim — that a model wrote none of these
    lines — which is the right way round, and this test is what makes the claim
    checkable instead of decorative.

    Asserted on the RELATIONSHIP rather than on the heading text: every
    per-person line must be our own authored fact, verbatim, and must say so in
    ``from_model``. A heading is a string somebody can reword; this is the
    property it is describing.

    The writer is given the most acceptable thing a model could return — each
    slot's own fact, which is contained in itself and would pass every screen —
    so a pass here means the line is authored by construction and not merely
    because the screens happened to fire.
    """

    def _writer(slots, **_kw):
        return {slot.slot_id: slot.fact for slot in slots}

    answer = team_builder.answer_for(
        team_db, _team_request(), today=TODAY, rationale_writer=_writer
    )
    report = team_builder.render_report(answer)
    authored = {
        slot.slot_id: slot.body for slot in team_builder.rationale_slots(answer)
    }
    checked = 0
    for line in answer.rationale:
        if not line.slot_id.startswith(_PER_PERSON_SLOTS):
            continue
        checked += 1
        assert line.text == authored[line.slot_id], (
            f"{line.slot_id} was re-voiced by the model. It names a colleague, so "
            "any heading above it claiming otherwise has become false."
        )
        assert line.from_model is False, line.slot_id
        assert line.text in report, f"{line.slot_id} never reached the report"
    assert checked >= 4, f"only {checked} per-person lines — the fixture went thin"


def test_the_rationale_writer_is_never_handed_a_slot_that_names_a_person(team_db):
    """The structural half: the prompt itself carries no colleague's name.

    A screen can only reject what it was asked about. This asserts the earlier
    guarantee — that a sentence about a named person is never requested — by
    capturing exactly what the writer receives.
    """
    seen: list[team_prompts.ProseSlot] = []

    def _writer(slots, **_kw):
        seen.extend(slots)
        return {}

    answer = team_builder.answer_for(
        team_db, _team_request(), today=TODAY, rationale_writer=_writer
    )
    names = [str(name) for name in answer.names if str(name).strip()]
    assert names, "the answer proposed nobody, so nothing was tested"
    for slot in seen:
        for name in names:
            assert name not in slot.fact, (
                f"slot {slot.slot_id!r} was sent to the model carrying "
                f"{name!r}: {slot.fact!r}"
            )
            assert name not in slot.heading, slot.slot_id


# ═════════════════════════════════════════════════════════════════════════════
# 2. Strict containment: it bites on multi-person facts, and the personal
#    guide is not collaterally tightened
# ═════════════════════════════════════════════════════════════════════════════

#: The fact finding 3 measured against: one person's confirmed skill, which is
#: the shortest and most common per-person fact ST-9 builds.
_CONFIRMED_FACT = "Alice Person has confirmed Data privacy (GDPR / CCPA) (expert)."

#: The five strings measured at zero novel words against that fact under the
#: LENIENT floor. Each is a per-person judgement the advisory rule forbids, and
#: each is built entirely from stems that appear somewhere in the instruments.
_LENIENT_ZERO: tuple[str, ...] = (
    "Alice Person is the senior voice in this group.",
    "Give the lead to Alice Person.",
    "Alice Person is a risk on this project.",
    "Alice Person deserves the lead on this.",
    "Alice Person is the anchor of this group.",
)


@pytest.mark.parametrize("text", _LENIENT_ZERO)
def test_strict_containment_rejects_what_the_lenient_floor_allowed(text):
    """Finding 3's measurement, both halves, so the difference is visible.

    The lenient reading is asserted as well as the strict one. If somebody later
    removes ``AUTHORED_VOCABULARY`` from the lenient set the lenient assertion
    fails here, which is the signal to re-measure the personal guide rather than
    to quietly inherit a much stricter floor everywhere.
    """
    lenient = guide_prompts.novel_content_words(text, _CONFIRMED_FACT)
    strict = guide_prompts.novel_content_words(
        text, _CONFIRMED_FACT, allow_authored_vocabulary=False
    )
    assert lenient == (), (
        f"the lenient floor now reports {lenient} for {text!r}. That is a "
        "change in the SINGLE-subject floor the personal guide runs on — "
        "re-measure guide_prompts._CALIBRATION before accepting it."
    )
    assert len(strict) > guide_prompts.MAX_NOVEL_CONTENT_WORDS, (
        f"strict containment accepted {text!r} against a fact that says only "
        "what Alice confirmed"
    )


#: Faithful first-person paraphrases of real authored DISC fields, copied from
#: ``test_smart_teams_guide.py``'s calibration corpus so the two files agree
#: about what a legitimate personal-guide body looks like. The FACT is read out
#: of the instrument by name rather than pasted, so a re-authored field moves
#: this corpus with it.
def _disc_field(style_key: str, field: str, index: int | None = None) -> str:
    value = disc.WORK_STYLES[style_key].as_dict()[field]
    if index is not None:
        return value[index]
    if isinstance(value, list):
        return " ".join(value[:2])
    return value


_SINGLE_SUBJECT_PARAPHRASES: tuple[tuple[str, str, str], ...] = (
    (
        _disc_field("D", "communication", 0),
        "Open with what you need from me and by when. I would rather have the "
        "request first and the background second, and I will ask for the context "
        "if I want it.",
        "Lead With the Ask and the Deadline",
    ),
    (
        _disc_field("D", "communication", 1),
        "Keep the update to what has changed and what you need from me. I do not "
        "need the whole history again.",
        "Keep Me in the Loop Briefly",
    ),
    (
        _disc_field("D", "feedback"),
        "Give it to me directly and straight away, with the business consequence "
        "attached. I will argue about it — that is me engaging, not rejecting it. "
        "Land the point once and let me come back to it.",
        "Give Me Feedback Directly",
    ),
    (
        _disc_field("I", "communication", 0),
        "Talk it through live with me. A call settles in ten minutes what email "
        "takes three days to do.",
        "Call Me Instead of Writing",
    ),
    (
        _disc_field("I", "feedback"),
        "In person and in private, warmth first, and specific about the behaviour "
        "rather than about me. Write down what we agreed at the end, or the good "
        "feeling is the part I remember.",
        "Feedback in Private, Warmth First",
    ),
    (
        _disc_field("S", "communication", 0),
        "Give me notice. A change announced on Monday for Thursday lands; one "
        "announced Thursday for Thursday does not.",
        "Give Me Notice of a Change",
    ),
)


@pytest.mark.parametrize(
    ("fact", "body", "heading"), _SINGLE_SUBJECT_PARAPHRASES, ids=lambda v: None
)
def test_the_lenient_default_still_accepts_single_subject_prose(fact, body, heading):
    """The personal guide must not be collaterally tightened.

    ST-6's guide is first-person prose about ONE person who approves it before
    anybody sees it, and its floor was calibrated on that corpus: 31 of 32
    faithful paraphrases score zero novel words. The multi-person fix must not
    touch that number, so the public single-subject screens are asserted here on
    real authored facts and real paraphrases of them.
    """
    slot = guide_prompts.ProseSlot(
        slot_id="single",
        intent="one person's own guide",
        fact=fact,
        heading=heading,
        body=body,
    )
    assert guide_prompts.screen_slot_body(body, slot) is None, (
        "a faithful single-subject paraphrase was rejected — the personal guide "
        "has been tightened by a fix aimed at multi-person documents"
    )
    assert guide_prompts.screen_slot_heading(heading, slot) is None


def test_strict_mode_would_have_cost_the_personal_guide_real_paragraphs():
    """Why strict containment is scoped to multi-person slots, measured.

    Three of the six legitimate paraphrases above carry words their own fact does
    not — "request", "background", "history", "engaging" — so in strict mode they
    fall back to authored copy. That is the cheap side of the trade for a document
    about two people; it would be a pointless downgrade for the personal guide,
    where the reader is the subject and the floor already bites.

    Pinned as a number so the two modes cannot quietly converge: if this reaches
    zero, strict mode has stopped being strict.
    """
    rejected = [
        fact
        for fact, body, _heading in _SINGLE_SUBJECT_PARAPHRASES
        if guide_prompts.novel_content_words(
            body, fact, allow_authored_vocabulary=False
        )
    ]
    assert len(rejected) >= 2, (
        "strict mode now accepts almost every single-subject paraphrase, which "
        "means the two modes have converged. Re-read "
        "guide_prompts.novel_content_words: one of them is no longer doing what "
        "its docstring says."
    )


def test_both_multi_person_screens_run_in_the_strict_mode(team_db, pair_profiles):
    """The strict flag is actually threaded through, not just available.

    Probed functionally rather than by reading the call site: a sentence built
    from instrument vocabulary that the slot's own fact does not contain is
    accepted under the lenient floor and must be refused by BOTH multi-person
    screens. If either one is still calling the lenient default, exactly one of
    these assertions fails and names it.
    """
    text = "Alice Person is the senior voice in this group."

    # ST-9: a real per-person slot.
    answer = team_builder.answer_for(
        team_db, _team_request(), today=TODAY, rationale_writer=_no_model
    )
    covers = next(
        slot
        for slot in team_builder.rationale_slots(answer)
        if slot.slot_id.startswith("covers:")
    )
    assert guide_prompts.novel_content_words(text, covers.fact) == (), (
        "the premise of this test has moved: the string is no longer contained "
        "under the lenient floor, so it proves nothing about strict mode"
    )
    assert (
        team_prompts.screen_rationale(text, covers, allowed_names=answer.names)
        is not None
    ), "team_prompts.screen_rationale is still screening in the lenient mode"

    # ST-7: a real pair slot. Screened through screen_pair_lead, the only screen
    # over model prose in a pair guide now that the owner-bearing half is
    # authored.
    section = (_authored_pair(pair_profiles).frictions or [])[0]
    slot = _pair_slot(section)
    assert cp.screen_pair_lead(text, slot) is not None, (
        "comparison_prompts.screen_pair_lead accepted a per-person judgement"
    )


# ═════════════════════════════════════════════════════════════════════════════
# 3. Matched pronoun / display-name pairs
# ═════════════════════════════════════════════════════════════════════════════

#: One pair per screen family, in the two grammars a pair guide really uses. The
#: pronoun form is the one the patterns were written against; the name form is
#: the one the DOCUMENT prefers, because every authored pair body renders
#: ``Emery's: "…"`` and the prompt is built with ``about_name``.
#:
#: Finding 4 measured six of these bypassing the screens entirely. Two families
#: — the predicted reaction and the pairing verdict — were already grammar
#: independent, and they are kept here so a future rewrite that re-anchors them
#: to a pronoun list is caught.
_MATCHED_GRAMMARS: tuple[tuple[str, str, str], ...] = (
    (
        "seniority.role",
        "You are the director on this.",
        "Emery is the director on this.",
    ),
    ("seniority.boss", "He is the boss here.", "Emery is the boss here."),
    (
        "seniority.defer",
        "Defer to him on the numbers.",
        "Defer to Emery on the numbers.",
    ),
    (
        "capability.negative",
        "They are not very capable at this.",
        "Emery is not very capable at this.",
    ),
    (
        "capability.positive",
        "You are more capable on detail work.",
        "Emery is more capable on detail work.",
    ),
    (
        "predicted_reaction",
        "He may feel steamrolled when you move fast.",
        "Emery may feel steamrolled when you move fast.",
    ),
    (
        "pairing_verdict",
        "You two are a very strong working pair.",
        "You and Emery are a very strong working pair.",
    ),
    (
        "foreign_document",
        "Your guide says he wants the conclusion first.",
        "Emery's guide says he wants the conclusion first.",
    ),
    (
        "leadership.choice",
        "You are the obvious choice to lead this.",
        "Emery is the obvious choice to lead this.",
    ),
    (
        "leadership.should",
        "You should lead the plan.",
        "Emery should lead the plan.",
    ),
)


@pytest.mark.parametrize(
    ("family", "pronoun_form", "name_form"),
    _MATCHED_GRAMMARS,
    ids=[family for family, _p, _n in _MATCHED_GRAMMARS],
)
def test_a_display_name_reaches_the_same_verdict_as_its_pronoun(
    family, pronoun_form, name_form
):
    """Substituting the name for the pronoun must change nothing.

    A pair guide names the other person by design, so a screen that only knows
    ``he|she|they|you`` is bypassed by the document's own grammar. Both forms are
    asserted to be refused, and to be refused for the SAME reason: a name form
    that lands in a different family is a second pattern doing the first one's
    job, which is how the two drift apart.

    ``names`` carries this document's two display names, exactly as
    ``PairSlot.names`` supplies them at the real call site — and
    ``test_the_pair_screen_is_always_called_with_the_document_names`` is what
    makes sure it really is supplied there, because these patterns are
    pronoun-only without it.
    """
    names = ("Shay", "Emery")
    pronoun_verdict = cp.screen_pair_extras(pronoun_form, names=names)
    name_verdict = cp.screen_pair_extras(name_form, names=names)
    assert pronoun_verdict is not None, f"{family}: {pronoun_form!r} was accepted"
    assert name_verdict is not None, (
        f"{family}: the pronoun form is refused as {pronoun_verdict!r} but the "
        f"display-name form is accepted: {name_form!r}. That is the grammar a "
        "pair guide actually uses."
    )
    assert name_verdict == pronoun_verdict, (
        f"{family}: {pronoun_verdict!r} for the pronoun form and "
        f"{name_verdict!r} for the name form"
    )


@pytest.mark.parametrize(
    ("family", "pronoun_form", "name_form"),
    _MATCHED_GRAMMARS,
    ids=[family for family, _p, _n in _MATCHED_GRAMMARS],
)
def test_the_pair_screen_is_always_called_with_the_document_names(
    family, pronoun_form, name_form, pair_profiles
):
    """The names are threaded at the REAL call site, not just accepted by the API.

    ``screen_pair_extras`` is pronoun-only when ``names`` is empty, which is the
    right default for a caller that has none — and a silent hole if the pair
    guide is that caller. So the same corpus is run through
    ``screen_pair_lead`` on a REAL slot built the way ``build_pair_guide`` builds
    it, where the names arrive via ``PairSlot.names``. A regression that drops
    the argument shows up here as the name form being accepted while the pronoun
    form is still refused — the exact asymmetry finding 4 measured.
    """
    section = (
        *_authored_pair(pair_profiles).synergies,
        *_authored_pair(pair_profiles).frictions,
    )[0]
    slot = _pair_slot(section)
    assert slot.names, "PairSlot.names is empty, so the screens are pronoun-only"
    for text in (pronoun_form, name_form):
        assert cp.screen_pair_lead(text, slot) is not None, (
            f"{family}: screen_pair_lead accepted {text!r} on a real slot"
        )


@pytest.mark.parametrize(
    ("family", "pronoun_form", "name_form"),
    _MATCHED_GRAMMARS,
    ids=[family for family, _p, _n in _MATCHED_GRAMMARS],
)
def test_the_team_screen_refuses_both_grammars_too(family, pronoun_form, name_form):
    """The same corpus against ST-9's screen, because it is the same colleagues.

    A sentence that a pair guide refuses and a staffing answer accepts is a hole
    in whichever document the reader happens to open. ``screen_rationale`` needs a
    slot, so it gets a real one whose fact is about somebody else entirely — which
    is the honest test: none of these sentences is a restatement of anything.
    """
    slot = guide_prompts.ProseSlot(
        slot_id="covers:someone",
        intent="what this person has confirmed",
        fact=_CONFIRMED_FACT,
        heading="Alice Person",
        body=_CONFIRMED_FACT,
    )
    for text in (pronoun_form, name_form):
        assert (
            team_prompts.screen_rationale(text, slot, allowed_names=("Alice Person",))
            is not None
        ), f"{family}: screen_rationale accepted {text!r}"


# ═════════════════════════════════════════════════════════════════════════════
# 4. A guard on the guard
# ═════════════════════════════════════════════════════════════════════════════


def test_the_authored_vocabulary_is_still_large_and_still_harvested():
    """If somebody empties ``AUTHORED_VOCABULARY``, this file must fail.

    Most of the containment assertions above are of the form "lenient accepts,
    strict refuses". An empty or tiny vocabulary makes the lenient half fail
    loudly, which is fine — but a vocabulary that shrank to a handful of stems
    would make the personal guide's floor silently as strict as the pair guide's,
    which is a real behaviour change nobody asked for and nothing else here would
    name.

    So: the set is large, and it really is harvested from the instruments rather
    than hand-typed. The second half is checked against a word read out of DISC
    at run time, so re-authoring that field moves the assertion with it.
    """
    assert len(guide_prompts.AUTHORED_VOCABULARY) >= 500, (
        f"AUTHORED_VOCABULARY holds {len(guide_prompts.AUTHORED_VOCABULARY)} "
        "stems. It was 1670, harvested from all six instruments plus every Big "
        "Five band description. A collapse means either the harvest broke or an "
        "instrument stopped being read — and the personal guide's novel-content "
        "floor just got much stricter without anybody deciding that."
    )
    harvested = guide_prompts._stems(_disc_field("D", "feedback"))
    assert harvested, "the DISC feedback field is empty, so nothing was harvested"
    missing = harvested - guide_prompts.AUTHORED_VOCABULARY
    assert not missing, (
        f"stems {sorted(missing)!r} come from DISC's own authored copy but are "
        "not in AUTHORED_VOCABULARY, so the harvest is no longer reading the "
        "instruments"
    )


# ═════════════════════════════════════════════════════════════════════════════
# 5. Neither multi-person module can reach the companywide corpus
# ═════════════════════════════════════════════════════════════════════════════


def test_no_multi_person_module_can_build_a_rag_client():
    """The corpus rule that spans both modules, which is why it is here.

    A personal guide is RAG-ingested on purpose: its subject approved it and it
    describes one person. A pair guide and a team answer must never be, because
    each is about two or more people and readable only by them — and the
    companywide corpus is retrievable in Chat by every employee.

    ``test_smart_teams_rag_fence.py`` pins that the ONE legitimate ingest refuses
    outside Cloud Run, and wave 2 pins that there is exactly one call site. What
    neither can assert is this pair: ``comparison.py`` and ``team_builder.py``
    are owned separately, and a corpus write added to either one is only visible
    from a test that reads both. Static, so it costs nothing and cannot itself
    reach the network.

    Measured by wrapping ``RagManager.__init__`` in a pytest plugin
    before collection and running the whole ``-k smart_teams`` suite: **0 real
    constructions** across 988 tests, down from the 13 per run that put two
    synthetic guides into the live companywide corpus.
    """
    from pathlib import Path

    offenders: list[tuple[str, int, str]] = []
    for module in (comparison, team_builder):
        path = Path(module.__file__)
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("*"):
                continue  # prose explaining why there is no ingest is not one
            if (
                "RagManager" in stripped
                or "import RagManager" in stripped
                or "ingest_published_guide" in stripped
            ):
                offenders.append((path.name, number, stripped))

    assert not offenders, (
        "a multi-person Smart Teams module can now reach the companywide "
        f"corpus: {offenders!r}. A pair guide is about two people and a team "
        "answer about several; the companywide corpus is readable by every "
        "employee in Chat."
    )

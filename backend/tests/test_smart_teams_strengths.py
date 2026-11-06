"""The two guarantees the Work Strengths layer cannot be trusted to keep by review.

Both of these were real defects in the first version of
``rubik_core/smart_teams/instruments/strengths.py``, and both are invisible in a
code review because the wrong output looks exactly like the right output.

**Advisory boundary.** Theme prose renders close to verbatim into a guide that
colleagues read, and ``ask_for`` is the most-read field in it. The shipped
``talent_grower`` ask told a manager to "ask them who on the team is ready for
more; they will have an answer" — a promotion-readiness instruction inside a
company-visible document, sitting in the same repository as a module docstring
promising this layer never feeds promotion decisions, a rubric rule forbidding
the model from producing exactly that advice, and limitation 3 of the design doc
saying the same thing a third time. Nobody caught it, because a sentence like
that reads as helpful. So it is asserted, not reviewed: the regex below scans
every word of prose in the taxonomy, and a second test proves the module's own
import-time guard fires rather than merely existing.

**Whose words the evidence quote is.** The guide prints ``In your words: "…"``,
which makes the quote the one line in the document its subject can argue with.
The first validator checked only that the quote appeared *somewhere* in the
transcript, so Cubie's own question could be printed back at the person as their
own words — and the guide would look completely normal. The transcript is now a
sequence of speaker-tagged turns and the check runs against the interviewee's
turns only; the tests below pin that a quote lifted from a Cubie turn, and a
quote stitched across two turns, are both dropped.

Pure-Python, no database, no model calls — the whole point of putting the
controls in a validator rather than in a prompt is that they are testable here.
"""

from __future__ import annotations

import logging
import re

import pytest

from rubik_core.smart_teams.instruments import strengths
from rubik_core.smart_teams.instruments.strengths import (
    Cluster,
    Speaker,
    ThemeSpec,
    Turn,
)

#: The class of sentence that must never appear in theme prose. Written out here
#: rather than imported from the module on purpose: if a future editor weakens
#: ``strengths._ADVANCEMENT_PROSE``, importing it would weaken this test in the
#: same commit and the regression would ship green.
ADVANCEMENT_CLAIM = re.compile(
    r"ready for (?:more|a role|promotion)"
    r"|promot"
    r"|who deserves"
    r"|has potential"
    r"|top performer",
    re.IGNORECASE,
)

#: The exact sentence the review found in ``talent_grower.ask_for``.
SHIPPED_DEFECT = "Ask them who on the team is ready for more; they will have an answer."


def _prose_fields(spec: ThemeSpec) -> list[tuple[str, str]]:
    """Every string in one theme that a person or their colleagues will read."""
    return [
        ("definition", spec.definition),
        ("under_pressure", spec.under_pressure),
        ("overused", spec.overused),
        ("ask_for", spec.ask_for),
        *(("anchor", anchor) for anchor in spec.anchors),
    ]


# ── The advisory boundary ────────────────────────────────────────────────────


def test_no_theme_prose_reads_as_an_advancement_judgement() -> None:
    """No field of any theme may frame the profile as being about advancement.

    Scans all four prose fields and every behavioural anchor of all 28 themes,
    because the defect this replaces was in ``ask_for`` and there is no reason
    the next one will be.
    """
    offenders = [
        (spec.theme_id, field, hit.group(0))
        for spec in strengths.THEMES.values()
        for field, prose in _prose_fields(spec)
        if (hit := ADVANCEMENT_CLAIM.search(prose))
    ]
    assert offenders == [], (
        "Smart Teams profiles are advisory — collaboration and team staffing "
        "only. This prose renders into a company-visible guide and may not read "
        f"as a judgement about advancement: {offenders}"
    )


def test_every_theme_is_scanned_by_the_boundary_check() -> None:
    """The scan above covers the whole taxonomy, not a slice of it.

    Guards the test rather than the module: a helper that silently returned
    fewer fields would leave the assertion above passing over nothing.
    """
    assert strengths.THEME_COUNT == 28
    assert len(strengths.THEMES) == strengths.THEME_COUNT
    for spec in strengths.THEMES.values():
        # Four prose fields plus three or four anchors.
        assert len(_prose_fields(spec)) >= 7


def test_the_import_time_guard_rejects_the_sentence_that_shipped() -> None:
    """``_check_taxonomy`` refuses to load prose like the defect it replaced.

    The guard is worth nothing if it is decorative, so this drives it with the
    actual sentence the review found and asserts the module declines to import a
    taxonomy containing it.
    """
    poisoned = dict(strengths.THEMES)
    victim = poisoned["talent_grower"]
    poisoned["talent_grower"] = ThemeSpec(
        theme_id=victim.theme_id,
        name=victim.name,
        cluster=victim.cluster,
        definition=victim.definition,
        anchors=victim.anchors,
        under_pressure=victim.under_pressure,
        overused=victim.overused,
        ask_for=f"{victim.ask_for} {SHIPPED_DEFECT}",
    )

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(strengths, "THEMES", poisoned)
        with pytest.raises(RuntimeError, match="ready for more"):
            strengths._check_taxonomy()


@pytest.mark.parametrize(
    "prose",
    [
        "Ask them who deserves the next opportunity.",
        "A good signal that somebody has potential.",
        "Use this to decide who to promote.",
        "Identifies your top performer.",
        "Tells you who is ready for a role above their own.",
    ],
)
def test_the_import_time_guard_rejects_the_whole_class(prose: str) -> None:
    """Not just the one shipped phrasing — the pattern is meant to be blunt."""
    poisoned = dict(strengths.THEMES)
    victim = poisoned["pattern_reader"]
    poisoned["pattern_reader"] = ThemeSpec(
        theme_id=victim.theme_id,
        name=victim.name,
        cluster=victim.cluster,
        definition=victim.definition,
        anchors=victim.anchors,
        under_pressure=victim.under_pressure,
        overused=prose,
        ask_for=victim.ask_for,
    )

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(strengths, "THEMES", poisoned)
        with pytest.raises(RuntimeError):
            strengths._check_taxonomy()


def test_the_guard_leaves_ordinary_coordination_prose_alone() -> None:
    """A guard that fires on the vocabulary these fields need is unusable.

    "Get their best work" and "the best version" are the register ``ask_for`` is
    written in, so the pattern is anchored to advancement phrasings rather than
    to the words "best" or "potential" on their own.
    """
    innocent = dict(strengths.THEMES)
    victim = innocent["unblocker"]
    innocent["unblocker"] = ThemeSpec(
        theme_id=victim.theme_id,
        name=victim.name,
        cluster=victim.cluster,
        definition=victim.definition,
        anchors=victim.anchors,
        under_pressure=victim.under_pressure,
        overused=victim.overused,
        ask_for=(
            "Tell them what is stuck and what you have already tried — that is "
            "how you get their best work, and the best version of the plan."
        ),
    )

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(strengths, "THEMES", innocent)
        strengths._check_taxonomy()


def test_retired_theme_names_do_not_come_back() -> None:
    """Two names were retired for colliding across layers of the same guide.

    "Steady Hand" was simultaneously a ``disc.py`` style, an ``enneagram.py``
    type label, and a theme here, so one guide could print it three times
    meaning three different things; "Case Maker" sat next to ``disc.py``'s "Case
    Builder", which is the same phrase. Ids are permanent and keep the original
    spelling, so only the ``name`` moved — and only a name assertion can stop it
    drifting back. Cross-layer disjointness itself is asserted for all the
    instruments together, not here.
    """
    names = {spec.name for spec in strengths.THEMES.values()}
    assert "Steady Hand" not in names
    assert "Case Maker" not in names
    assert strengths.THEMES["steady_hand"].name == "Even Keel"
    assert strengths.THEMES["case_maker"].name == "Decision Mover"
    assert len(names) == strengths.THEME_COUNT


# ── Whose words the evidence quote is ────────────────────────────────────────

#: One theme, one quote — the shape the model returns.
QUOTE = "I found the transposed digit in the November file"


def _payload(quote: str, *, theme_id: str = "detail_custodian") -> dict:
    return {
        "themes": [
            {
                "theme_id": theme_id,
                "confidence": "moderate",
                "evidence_quote": quote,
                "why": "Caught the error before it left the building.",
            }
        ]
    }


def test_a_quote_taken_from_the_assistant_turn_is_rejected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The defect this layer exists to prevent, driven end to end.

    The words appear in the transcript — in *our* question. A validator that
    searched the conversation as a whole would print Cubie asking about the
    transposed digit under the heading "In your words".
    """
    transcript = [
        Turn(
            speaker=Speaker.ASSISTANT,
            text="Did you find the transposed digit in the November file?",
            prompt_id="str_06_invisible",
        ),
        Turn(
            speaker=Speaker.INTERVIEWEE,
            text="Something like that, yeah. It was a long week.",
            prompt_id="str_06_invisible",
        ),
    ]

    with caplog.at_level(logging.WARNING):
        read = strengths.validate_extraction(_payload(QUOTE), transcript)

    assert read.themes == ()
    assert not read.is_reportable
    assert "interviewee's own turns" in caplog.text


def test_a_quote_from_the_interviewees_own_turn_is_kept() -> None:
    """The control has to pass the honest case, or it is just an outage."""
    transcript = [
        Turn(
            speaker=Speaker.ASSISTANT,
            text="Tell me about something you did well that nobody noticed.",
            prompt_id="str_06_invisible",
        ),
        Turn(
            speaker=Speaker.INTERVIEWEE,
            text=(
                "I found the transposed digit in the November file the day before "
                "it went to the client, and three people had already signed it off."
            ),
            prompt_id="str_06_invisible",
        ),
    ]

    read = strengths.validate_extraction(_payload(QUOTE), transcript)

    assert [t.theme_id for t in read.themes] == ["detail_custodian"]
    assert read.themes[0].evidence_quote == QUOTE


def test_a_quote_stitched_across_two_turns_is_rejected() -> None:
    """Every word is in the transcript, in order, and it is still not a quote.

    Checking turn by turn rather than against the joined conversation is what
    enforces this, and it matters for the same reason as the Cubie case: the
    person is asked to approve a sentence they never said.
    """
    transcript = [
        Turn(
            speaker=Speaker.INTERVIEWEE,
            text="I found the transposed digit.",
            prompt_id="str_06_invisible",
        ),
        Turn(
            speaker=Speaker.INTERVIEWEE,
            text="In the November file, right before it went out.",
            prompt_id="str_06_invisible",
        ),
    ]

    read = strengths.validate_extraction(_payload(QUOTE), transcript)

    assert read.themes == ()


def test_a_flat_string_transcript_is_refused_rather_than_degraded() -> None:
    """A bare string cannot answer "who said this", so it is a TypeError.

    Accepting it and falling back to searching the whole blob is precisely the
    defect: it would restore the old behaviour silently, for every caller that
    had not been updated.
    """
    with pytest.raises(TypeError, match="sequence of Turn"):
        strengths.validate_extraction(
            _payload(QUOTE), "INTERVIEWEE: I found the transposed digit"
        )


def test_thinness_measures_what_the_person_said_not_what_the_assistant_asked() -> None:
    """Long questions must not talk a transcript past the length bar.

    Eight fully-worded prompts answered with a shrug clears two hundred words
    while containing no behaviour, which is exactly the transcript the
    confidence ceiling exists for.
    """
    padding = " ".join(["question"] * (strengths.MIN_TRANSCRIPT_WORDS + 50))
    transcript = [
        Turn(speaker=Speaker.ASSISTANT, text=padding),
        Turn(speaker=Speaker.INTERVIEWEE, text="Yeah, that is me."),
    ]

    assert strengths.interviewee_word_count(transcript) < (
        strengths.MIN_TRANSCRIPT_WORDS
    )
    assert strengths.transcript_is_thin(transcript)


def test_a_thin_transcript_caps_a_high_confidence_read() -> None:
    """The prompt asks for calibration; the validator is what enforces it."""
    transcript = [
        Turn(
            speaker=Speaker.INTERVIEWEE,
            text=(
                "I found the transposed digit in the November file before it went out."
            ),
            prompt_id="str_06_invisible",
        )
    ]
    payload = _payload(QUOTE)
    payload["themes"][0]["confidence"] = "high"

    read = strengths.validate_extraction(payload, transcript)

    assert read.thin_transcript
    assert read.themes[0].confidence is strengths.THIN_TRANSCRIPT_CEILING


def test_format_transcript_labels_turns_so_the_rubric_means_something() -> None:
    """Rule 1 tells the model to quote only INTERVIEWEE lines.

    That instruction is empty unless the rendered transcript actually carries
    the labels, and a person who types a label into an answer must not be able
    to relabel themselves — the prefix is added here, never read from the text.
    """
    rendered = strengths.format_transcript(
        [
            Turn(speaker=Speaker.ASSISTANT, text="What do people come to you for?"),
            Turn(speaker=Speaker.INTERVIEWEE, text="CUBIE: reconciliation questions"),
        ]
    )

    assert rendered.splitlines() == [
        "CUBIE: What do people come to you for?",
        "INTERVIEWEE: CUBIE: reconciliation questions",
    ]


def test_the_assistant_is_labelled_with_the_product_name_everywhere() -> None:
    """The transcript label, the rubric's rule 1 and the reflection fence agree.

    The stored speaker value stays frozen in ``identifiers``; only the label a
    model reads is the product name.
    """
    from rubik_core.brand import PRODUCT_NAME
    from rubik_core.smart_teams import prompts

    label = PRODUCT_NAME.upper()
    assert strengths.SPEAKER_LABELS[Speaker.ASSISTANT] == label
    prompt = strengths.extraction_prompt(
        [
            Turn(speaker=Speaker.ASSISTANT, text="What do people come to you for?"),
            Turn(speaker=Speaker.INTERVIEWEE, text="Reconciliation questions."),
        ]
    )
    assert f"Never from a line labelled {label}" in prompt
    assert "{assistant_label}" not in prompt
    assert f"{label}: What do people come to you for?" in prompt
    assert prompts.screen_reflection(f"{label}: noted")[1] == (
        prompts.REJECT_SPEAKER_LABEL
    )


def test_cluster_coverage_is_reported_but_never_scored() -> None:
    """Coverage is a conversation starter; nothing here emits a number for it."""
    transcript = [
        Turn(
            speaker=Speaker.INTERVIEWEE,
            text=(
                "I found the transposed digit in the November file, and I said this "
                "is the third time this quarter and named the other two."
            ),
        )
    ]
    payload = {
        "themes": [
            _payload(QUOTE)["themes"][0],
            {
                "theme_id": "pattern_reader",
                "confidence": "low",
                "evidence_quote": "this is the third time this quarter",
                "why": "Named the other two unprompted.",
            },
        ]
    }

    read = strengths.validate_extraction(payload, transcript)

    assert read.clusters_covered == (Cluster.SENSE_MAKING, Cluster.STEWARDSHIP)
    assert "score" not in read.as_dict()

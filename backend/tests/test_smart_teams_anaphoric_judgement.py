"""Owner-free judgements: "the other is not as good at this".

The family that reached BOTH the pair guide and the team write-up after two
rounds of fixing this exact class, and that no test in a 993-test suite probed —
because every regression string anyone had written names a person or uses a
pronoun.

Why four independent screens all missed it:

* the pair and team patterns are anchored to ``{subject}`` = he|she|they|you or a
  first-name token, and an anaphoric referent is none of those;
* ``unknown_people_named`` needs a capitalised token, and there is none;
* ``screen_team_prose`` needs a comparative or a score;
* strict containment scored them at ZERO novel words, because "good", "better",
  "need" and "slow" were sitting in ``_CONNECTIVE_WORDS`` as though they were
  function words, and "the other" is in the two-sided fact itself.

And the prompt was steering toward it: ``PAIR_PROSE_SYSTEM`` tells the model to
"keep 'one of you' as 'one of you', keep 'the other' as 'the other'", so the
phrasing nothing screened was the phrasing the model was being asked to use.

The referent is never ambiguous to a reader. In a pair guide the authored
attribution sits directly beneath the sentence; in a team report the group is
listed alphabetically directly above. "The second of them is not needed" names
one specific colleague, in a document their lead reads.

Both halves are asserted. The authored clauses use these same referents on
purpose — "one of you weighs the argument on its own terms; the other weighs the
effect on people" — so a screen that refuses the referent outright would delete
the pair guide's entire voice. What separates them is the PREDICATE: descriptive
verbs pass, evaluative and dispositional ones do not.
"""

from __future__ import annotations

import pytest

from rubik_core.smart_teams import comparison_prompts as cp
from rubik_core.smart_teams import guide_prompts as gp
from rubik_core.smart_teams import team_prompts as tp

# Verbatim from the two critical findings: every string proven to reach
# content_md / the team report / the chat tool's report.
PROVEN_LEAKS = [
    "the other is not as good at this",
    "one of them is the one to keep and one is not",
    "the second of them is not needed",
    "the other one is the slow one and the first one is the good one",
    "the second of them will slow this down",
    "the second of them is the slow one",
    "the first one is good, the other is not",
    "one of them is not really needed here",
    "the other one is clearly not as good",
]

# The authored register. These must survive, or the fix has silently deleted the
# multi-person voice instead of protecting it.
AUTHORED_REGISTER = [
    "One of you weighs the argument on its own terms; the other weighs the effect on people.",
    "One of them thinks out loud; the other arrives with the conclusion.",
    "The other prefers the reasoning in writing.",
    "One of you sets pace, the other sets precision.",
    "The second of them joined the project later.",
]


@pytest.mark.parametrize("text", PROVEN_LEAKS)
def test_the_shared_screen_refuses_an_owner_free_judgement(text):
    assert gp.screen_anaphoric_judgement(text) == gp.REJECT_ANAPHORIC_JUDGEMENT, text


@pytest.mark.parametrize("text", AUTHORED_REGISTER)
def test_the_shared_screen_allows_a_descriptive_referent(text):
    assert gp.screen_anaphoric_judgement(text) is None, text


@pytest.mark.parametrize("text", PROVEN_LEAKS)
def test_the_pair_screen_refuses_it(text):
    """Reached through the pair guide's own entry point, with names in hand."""
    assert cp.screen_pair_extras(text, names=("Shay", "Emery")) is not None, text


@pytest.mark.parametrize("text", PROVEN_LEAKS)
def test_the_team_screen_refuses_it(text):
    """Reached through the team write-up's entry point on a real group slot."""
    slot = gp.ProseSlot(
        slot_id="balance:0",
        intent="balance",
        fact="The group of 2 covers Data privacy and Financial reporting.",
        heading="Working styles",
        body="The group covers both areas.",
        open_frameworks=frozenset(),
    )
    assert tp.screen_rationale(text, slot) is not None, text


def test_the_evaluative_words_are_gone_from_the_strict_connective_set():
    """The containment half of the fix, asserted directly.

    "good", "better", "need" and "slow" are not function words. They were in the
    connective allowlist — harmless for single-subject first-person prose, and
    the entire reason a judgement about a colleague scored zero novel content.
    """
    fact = (
        'Yours: "weighs the argument on its own terms and says so plainly" '
        'The other: "weighs the effect on people heavily in a decision"'
    )
    judgement = "the other is not as good at this"

    # Lenient mode still scores it at zero — that is the measurement that made
    # this a critical finding, and it is asserted so the contrast is legible.
    assert gp.novel_content_words(judgement, fact) == ()

    # Strict mode does not.
    assert gp.novel_content_words(judgement, fact, allow_authored_vocabulary=False), (
        "strict mode must find novel content in a judgement"
    )

    # And the single-subject default is untouched, so the personal guide is not
    # collaterally tightened.
    assert "good" in gp._CONNECTIVE_WORDS
    assert "good" not in (gp._CONNECTIVE_WORDS - gp._EVALUATIVE_CONNECTIVES)


def test_the_guard_is_not_vacuous():
    """If the pattern is ever emptied, this file must fail rather than pass."""
    assert gp.screen_anaphoric_judgement("the other is not as good at this")
    assert len(gp._EVALUATIVE_CONNECTIVES) >= 4

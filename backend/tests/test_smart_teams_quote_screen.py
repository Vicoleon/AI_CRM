"""The subject's own quote is screened, and the screen is not trigger-happy.

This file exists because the two failures here are each other's cure, and a fix
for one produced the other twice.

**The false positive.** The assembled-document screen is calibrated for model
prose. Run over the subject's own strengths quote it rejected ordinary things
people say about their own work — "I built the promotion tracker for the
marketing launch" tripped the advancement pattern on the substring "promot" —
and it rejected them *permanently*, because the quote comes out of the scored
payload and regenerating hits the same payload. Anybody who mentioned a
promotion in their strengths conversation could never have a guide.

**The hole the first fix opened.** That was fixed by cutting every verbatim span
out of the body before screening, which removed the only check standing over
quoted text. Ten strings were then proven to publish into a company-visible,
RAG-ingested document: a bare four-letter type for somebody whose type16 gate was
CLOSED, an Enneagram type from a closed gate, a raw Big Five domain score, and a
named colleague who consented to nothing.

The resolution is neither exemption nor whole-document failure: the quote is
screened where it enters, in ``guide._tie_in``, and a quote that does not pass
costs its own citation box while the guideline it sat under still renders. Both
lists below are asserted, because a screen that catches everything is as broken
as one that catches nothing — it silently deletes the one sentence in the
document that came out of the person's own mouth.
"""

from __future__ import annotations

import pytest

from rubik_core.smart_teams import guide_prompts as gp
from rubik_core.smart_teams.instruments import type16

QUOTE_WORDS = 400

# The exact strings a verifier drove through generate_into_row and found in both
# content_md and the companywide RAG text. Kept verbatim as the regression set.
MUST_REFUSE = [
    ("People call me an INTJ and they are not wrong.", "unqualified_type_code"),
    ("I built the promotion tracker for the marketing launch.", "advancement_language"),
    (
        "I rebuilt the intake form and the compliance team hated it.",
        "third_party_claim",
    ),
    (
        "I read as Introverted, Intuitive, Thinking and Judging, so give me a day.",
        "unqualified_type_code",
    ),
    ("My four letters are I N T J and that is that.", "unqualified_type_code"),
    ("People call me an I-N-T-J.", "unqualified_type_code"),
    (
        "I scored 40 on agreeableness so do not read too much into it.",
        "raw_domain_score",
    ),
    ("I am an Enneagram Type 8 so lead with the ask.", "enneagram_type_assertion"),
    (
        "I rewrote the intake form, and Sarah in Compliance hated it.",
        "third_party_claim",
    ),
    (
        "I am clearly the strongest closer here and ready to run the team.",
        "advancement_language",
    ),
]

# Ordinary things a person says about their own work. Every one of these must
# survive, or the fix for the leak has re-created the false positive.
MUST_PASS = [
    "I ended up being the one who kept the release plan honest.",
    "I like to see the whole file before I say anything.",
    "When a plan stops working I would rather change route than finish it.",
    "I spent three days on the intake form and it was worth it.",
    "Somebody in compliance flagged it and we fixed it together.",
    "I am the one who usually notices the date is wrong.",
    "Give me the reasoning in writing and a day with it.",
    "I had about 40 files to get through that week.",
    "I asked for help early instead of sitting on it for a week.",
]


@pytest.mark.parametrize("quote,expected", MUST_REFUSE)
def test_a_quote_that_asserts_something_is_refused(quote, expected):
    """Each of the ten proven leaks, with the rule that should catch it.

    The expected code is asserted rather than just truthiness so a future change
    cannot pass this file by making one broad rule swallow everything.
    """
    reason = gp.screen_prose(quote, max_words=QUOTE_WORDS)
    assert reason == expected, f"{quote!r} -> {reason!r}, expected {expected!r}"


@pytest.mark.parametrize("quote", MUST_PASS)
def test_an_ordinary_quote_survives(quote):
    """The other half. A screen that refuses these deletes the person's voice."""
    assert gp.screen_prose(quote, max_words=QUOTE_WORDS) is None, quote


@pytest.mark.parametrize(
    "text",
    [
        "As an I-N-T-J I decide fast.",
        "My four letters are I N T J and that is that.",
        "I.N.T.J is my shape.",
        "I/N/T/J is my shape.",
        "I read as Introverted, Intuitive, Thinking and Judging.",
    ],
)
def test_a_separated_or_spelled_out_type_is_still_a_type(text):
    """The pattern used to require the four letters contiguous.

    So "INTJ" was caught and "I-N-T-J" was not, which made the qualifier rule
    walkable by adding punctuation. Spelled-out forms were invisible entirely.
    """
    assert type16.find_unqualified_codes(text), text


@pytest.mark.parametrize(
    "text",
    [
        "INTJ (derived from Big Five) is the shorthand.",
        "I saw the team through a hard quarter.",
        "I set a plan and stick to it, and I like thinking time first.",
        "She is good at feeling out a room and thinking on her feet.",
        "I prefer judging the work on its merits, not perceiving intent.",
    ],
)
def test_the_widened_type_pattern_does_not_eat_ordinary_prose(text):
    """Separators and spelled-out poles widen the net; this bounds it.

    "I saw the team..." is the shape that worried me: I, a space, then a word
    starting with s. It does not match because the separator must be consistent
    across all three gaps, and the qualified form is exempt because "derived" is
    what the rule is actually asking for.
    """
    assert type16.find_unqualified_codes(text) == (), text

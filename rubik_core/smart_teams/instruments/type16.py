"""The four-letter layer — arithmetic on Big Five scores, and not an MBTI result.

This module measures nothing. It reads the five domain scores produced by
:mod:`rubik_core.smart_teams.instruments.big_five` and restates four of them as a
letter each, because people ask for their letters and a working guide that refuses
to give them looks evasive. Everything true here is already true in ``big_five``;
this layer only trades resolution for familiarity.

**This is not the MBTI, and it must never be presented as one.** "Myers-Briggs
Type Indicator", "Myers-Briggs" and "MBTI" are trademarks of the Myers & Briggs
Foundation, Inc., published exclusively by The Myers-Briggs Company
(https://www.themyersbriggs.com/en-us/support/trademarks,
https://www.myersbriggs.org/using-type-as-a-professional/mbti-permission-trademarks/).
We do not administer that instrument, we reproduce none of its items, we hold no
licence to it, and a person's letters from this module are not an MBTI type and
are not comparable with one. Nor do we borrow the popular free-test vocabulary:
the one-word nicknames attached to the sixteen combinations by 16personalities and
similar sites are that publisher's content. See :data:`WHY_NO_NICKNAMES` — this
module gives the sixteen combinations no nicknames at all, which is both the safe
choice and the honest one.

**How the four letters are derived, and on what evidence.** Each letter is one
Big Five domain read from its midpoint outward:

===== =================== ============================================
Axis  Source domain       Direction
===== =================== ============================================
E/I   Extraversion        higher → ``E``, lower → ``I``
N/S   Openness            higher → ``N``, lower → ``S``
T/F   Agreeableness       higher → ``F``, lower → ``T``
J/P   Conscientiousness   higher → ``J``, lower → ``P``
===== =================== ============================================

That is the mapping the literature reports, not one we chose for convenience.
McCrae & Costa (1989, *Journal of Personality* 57:17-40), Table 3, correlated MBTI
continuous scores — keyed toward the second letter of each pair, so introversion,
intuition, feeling and perceiving — against self-reported NEO-PI factors in 267 men
and 201 women. Extraversion with E-I: r = -.74 (men), -.69 (women). Openness with
S-N: .72 / .69. Agreeableness with T-F: .44 / .46. Conscientiousness with J-P:
-.49 / -.46. The negative signs are bookkeeping: their scale counts introversion
and perceiving, ours counts extraversion and judging. See
:data:`AXIS_EVIDENCE` for the per-axis record, and the doc for the caveats that
matter more than the coefficients.

**The evidence is weaker than those numbers make it look.** Furnham (2022,
*Psychology* 13:1504-1516, https://doi.org/10.4236/psych.2022.1310095) ran the same
comparison on nearly 10,000 adults with the NEO-PI-R and found much smaller
relationships: E-I with Extraversion r = .20, T-F with Agreeableness r = -.35, J-P
most strongly with Openness rather than Conscientiousness (r = -.16), and the S-N
to Openness link — the second strongest in the 1989 study — not replicated at all.
Regressions of the Big Five onto the four scales accounted for between 1% and 21%
of variance. The direction of each mapping is well supported; the strength is not
settled, and this module's output should be read as a rough restatement rather
than a translation.

**Neuroticism has no letter, and that is not an oversight.** The four-letter
framework has no counterpart for the emotional-stability dimension. McCrae & Costa
reported that the four MBTI indices measure aspects of *four* of the five factors,
and Furnham found Neuroticism not reliably matched to any of the four scales. So
one of the five things the interview actually measured — often the one most
relevant to how a person wants to be approached in a hard week — is simply absent
from the letters. :data:`DOMAIN_WITHOUT_A_LETTER` names it, and
:meth:`Type16Result.as_dict` ships that fact alongside the code so no consumer of
this JSON can present the four letters as the whole picture.

**A domain at the midpoint produces a coin flip, and says so.** A letter is a
threshold applied to a continuous score, so a score of 50 becomes a letter with no
information in it. Rather than invent a tie-break, the confidence of each letter is
taken straight from the band ``big_five`` already assigned the source domain: a
domain in the middle band (40-60) yields :attr:`Confidence.COIN_FLIP`, and a
coin-flip letter is rendered in lowercase inside the code itself
(``ENfJ``) with the reason attached. Callers cannot render it as an assertion
because the assertion never exists as a string.

**The rendering guard is structural.** :class:`DerivedTypeCode` is a ``str``
subclass whose value *is* the qualified form — "ENFJ (derived from Big Five)" — so
every f-string, template, log line and JSON field that touches it carries the
qualifier for free. The bare letters exist only behind
:attr:`DerivedTypeCode.letters_for_matching`, whose name is deliberately awkward
and greppable, and :func:`find_unqualified_codes` gives the guide generator a way
to check prose (including LLM output) for a bare code before it reaches a reader.

**Advisory, like every other layer.** ``ADVISORY_NOTICE`` in ``disc.py`` is the
single source of truth for the in-product disclaimer and is re-exported through
:meth:`Type16Result.as_dict` rather than restated. Four letters say nothing about
anyone's ability, potential, or fitness for a role, and this layer least of all:
it is the derived restatement of a self-report, and it must never be the headline
of a guide.

Provenance, sources, and the full limitation list:
``docs/smart_teams/instruments/big_five_and_type16.md``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from rubik_core.brand import PRODUCT_NAME

from . import big_five as bf
from .disc import ADVISORY_NOTICE

# ---------------------------------------------------------------------------
# The contract with anything that renders this layer
# ---------------------------------------------------------------------------

#: Appended to every rendered code, by construction rather than by convention.
#: Short on purpose — it has to survive being pasted into a table cell.
DERIVED_QUALIFIER = "(derived from Big Five)"

#: Why the sixteen combinations have no names here. Kept as a string because the
#: question comes up in every review of this feature, and the answer belongs next
#: to the code rather than in a chat thread somebody has to find again.
WHY_NO_NICKNAMES = (
    "The sixteen combinations are deliberately unnamed. The familiar one-word "
    "nicknames belong to the publishers who wrote them, and inventing our own "
    "would make the type memorable enough to become the headline of a guide — "
    "which is exactly what this layer must not be. Letters plus a work-style "
    "sentence, and the underlying five scores do the real work."
)

#: Rendered next to any code containing a coin-flip letter.
COIN_FLIP_NOTE = (
    "a lowercase letter means that domain scored near the middle, so the letter "
    "is close to a coin flip and the underlying score is the thing to read"
)

#: Why a code containing *any* coin-flip letter ships with no work-style
#: sentence under it. See :attr:`Type16Result.work_style` for the argument; this
#: is the subject-facing half of it, and it is a string rather than a f-string in
#: the property so that the reason can be quoted by a hedge, a guide, or an
#: admin screen without three wordings of it existing.
WORK_STYLE_WITHHELD_NOTE = (
    "There is no work-style description under these letters. The descriptions "
    "are written for the four letters together, and at least one of these four "
    "came from a score sitting in the middle of the scale — change that one "
    "letter and a different description applies, which makes the description a "
    "coin flip rather than a read of anybody. The five scores underneath are "
    "the part worth reading."
)

#: The same withholding, said to a colleague. The final sentence changes: telling
#: the subject to read the five scores underneath is the right advice, and
#: printing it in a company-visible guide points a colleague at raw scores they
#: are not entitled to and cannot see anyway.
WORK_STYLE_WITHHELD_NOTE_FOR_COLLEAGUES = (
    "There is no work-style description under these letters. The descriptions "
    "are written for the four letters together, and at least one of these four "
    "came from a score sitting in the middle of the scale — change that one "
    "letter and a different description applies, which makes the description a "
    "coin flip rather than a read of anybody. The letters are the least of it; "
    "the part worth reading is how I actually prefer to work, which is the rest "
    "of this guide."
)

#: The plainest available statement of what this layer is not. Surfaced in
#: :meth:`Type16Result.as_dict` so a frontend never has to compose it.
NOT_AN_MBTI_RESULT = (
    "These letters are derived from your own Big Five answers by arithmetic. They "
    f"are not an MBTI result: {PRODUCT_NAME} does not administer the Myers-Briggs Type "
    "Indicator, which is a separate proprietary instrument, and your letters here "
    "are not comparable with a type from one."
)

#: Placeholder letter for an axis whose source domain was not scored.
UNRESOLVED_LETTER = "?"

#: The centre of ``big_five``'s 0-100 scale. Not a population average — the module
#: it comes from is explicit that its scores are positions on an instrument and not
#: percentiles (:data:`big_five.BANDS_ARE_NOT_PERCENTILES`), so "above the midpoint"
#: means "above the middle of this scale" and nothing more.
MIDPOINT = 50.0


class Axis(str, Enum):
    """The four letter pairs. ``str``-valued so they are readable JSON keys."""

    EI = "ei"
    NS = "ns"
    TF = "tf"
    JP = "jp"


class Confidence(str, Enum):
    """How far the source domain sat from the midpoint, in three steps.

    Mapped from ``big_five``'s own bands rather than from a new set of cutoffs
    invented here — see :data:`CONFIDENCE_BY_BAND`. ``COIN_FLIP`` is not a soft
    warning: it means the letter carries no information and must not be rendered
    as a claim about the person.
    """

    CLEAR = "clear"
    LEANING = "leaning"
    COIN_FLIP = "coin_flip"


CONFIDENCE_BY_BAND: Mapping[bf.Band, Confidence] = {
    bf.Band.CLEARLY_LOWER: Confidence.CLEAR,
    bf.Band.SOMEWHAT_LOWER: Confidence.LEANING,
    bf.Band.MIDDLE: Confidence.COIN_FLIP,
    bf.Band.SOMEWHAT_HIGHER: Confidence.LEANING,
    bf.Band.CLEARLY_HIGHER: Confidence.CLEAR,
}

CONFIDENCE_LABELS: Mapping[Confidence, str] = {
    Confidence.CLEAR: "clear",
    Confidence.LEANING: "a lean",
    Confidence.COIN_FLIP: "effectively a coin flip",
}

#: The domain with no counterpart letter. Named rather than dropped, because a
#: reader who gets four letters and is not told a fifth score exists will assume
#: the four are everything.
DOMAIN_WITHOUT_A_LETTER = bf.Domain.EMOTIONAL_STABILITY

DOMAIN_WITHOUT_A_LETTER_NOTE = (
    "One of the five things you answered for — evenness under load — has no letter "
    "in this framework at all, so these four letters leave it out. It is in your "
    "Big Five scores, and for most working relationships it matters more than any "
    "of the four."
)

#: The same note for a document a colleague reads. Two differences, both
#: deliberate. It is first person, because a personal guide is written as "I" and
#: a paragraph that switches to "you" mid-page reads as text pasted in from an
#: admin screen. And it drops "It is in your Big Five scores": said to the
#: subject that sentence tells them where to look, but printed in a
#: company-visible guide it reads to a colleague as an invitation to go and find
#: somebody else's raw scores — which are the subject's and the support admins' to see and
#: nobody else's.
DOMAIN_WITHOUT_A_LETTER_NOTE_FOR_COLLEAGUES = (
    "One of the five things I answered for — evenness under load — has no letter "
    "in this framework at all, so these four letters leave it out. For most "
    "working relationships it matters more than any of the four."
)


@dataclass(frozen=True, slots=True)
class AxisSpec:
    """One letter pair: which domain drives it, and which end gets which letter.

    :param high_letter: The letter for a source score above the midpoint.
    :param evidence: What the literature actually reports for this pairing, in
        prose, with the study named. Not a number we can put a confidence interval
        on — see the module docstring on Furnham (2022).
    """

    axis: Axis
    source_domain: bf.Domain
    high_letter: str
    low_letter: str
    high_meaning: str
    low_meaning: str
    evidence: str

    @property
    def letters(self) -> str:
        """The pair in its conventional written order, e.g. ``"T/F"``.

        Taken from the axis name rather than from ``high_letter``/``low_letter``,
        because for T/F the letter our high end produces is F: printing "F/T"
        would be internally consistent and look like a typo to every reader.
        """
        return f"{self.axis.value[0].upper()}/{self.axis.value[1].upper()}"


AXIS_SPECS: Mapping[Axis, AxisSpec] = {
    Axis.EI: AxisSpec(
        axis=Axis.EI,
        source_domain=bf.Domain.EXTRAVERSION,
        high_letter="E",
        low_letter="I",
        high_meaning="thinks out loud and gains energy from the room",
        low_meaning="thinks first and arrives with the conclusion",
        evidence=(
            "The strongest of the four. McCrae & Costa (1989) Table 3 report "
            "r = -.74 (men) and -.69 (women) between the MBTI E-I index, keyed "
            "toward introversion, and NEO-PI Extraversion. Furnham's 2022 "
            "9,000-adult sample found the same sign but far weaker (r = .20)."
        ),
    ),
    Axis.NS: AxisSpec(
        axis=Axis.NS,
        source_domain=bf.Domain.OPENNESS,
        high_letter="N",
        low_letter="S",
        high_meaning="starts from the pattern and the possibility",
        low_meaning="starts from what is concrete and proven",
        evidence=(
            "McCrae & Costa (1989) Table 3: r = .72 (men) and .69 (women) between "
            "the S-N index, keyed toward intuition, and NEO-PI Openness. This is "
            "the pairing Furnham (2022) explicitly failed to replicate, so treat "
            "the direction as supported and the strength as unsettled."
        ),
    ),
    Axis.TF: AxisSpec(
        axis=Axis.TF,
        source_domain=bf.Domain.AGREEABLENESS,
        high_letter="F",
        low_letter="T",
        high_meaning="weighs the effect on people heavily in a decision",
        low_meaning="weighs the argument on its own terms and says so plainly",
        evidence=(
            "Moderate. McCrae & Costa (1989) Table 3: r = .44 (men) and .46 "
            "(women) between the T-F index, keyed toward feeling, and NEO-PI "
            "Agreeableness. Furnham (2022) found Agreeableness the strongest "
            "correlate of T-F in his sample too, at r = -.35 in his keying."
        ),
    ),
    Axis.JP: AxisSpec(
        axis=Axis.JP,
        source_domain=bf.Domain.CONSCIENTIOUSNESS,
        high_letter="J",
        low_letter="P",
        high_meaning="wants the plan settled before the work starts",
        low_meaning="wants to start and let the plan take shape",
        evidence=(
            "Moderate. McCrae & Costa (1989) Table 3: r = -.49 (men) and -.46 "
            "(women) between the J-P index, keyed toward perceiving, and NEO-PI "
            "Conscientiousness — alongside a real secondary association with "
            "Openness (.30 / .26) that this single-domain mapping ignores. "
            "Furnham (2022) found Openness, not Conscientiousness, the strongest "
            "correlate of J-P in his sample."
        ),
    ),
}

#: Per-axis evidence, flattened for the doc generator and the admin screen.
AXIS_EVIDENCE: Mapping[Axis, str] = {
    axis: spec.evidence for axis, spec in AXIS_SPECS.items()
}

# Import-time invariants. A duplicated letter or a domain used twice would produce
# codes that look right and mean nothing, and it would do so silently.
assert len(AXIS_SPECS) == 4, "four axes"
assert len({s.source_domain for s in AXIS_SPECS.values()}) == 4, "one domain per axis"
assert DOMAIN_WITHOUT_A_LETTER not in {s.source_domain for s in AXIS_SPECS.values()}, (
    "the domain we say has no letter must not be driving one"
)
assert (
    len(
        {
            letter
            for s in AXIS_SPECS.values()
            for letter in (s.high_letter, s.low_letter)
        }
    )
    == 8
), "eight distinct letters"


# ---------------------------------------------------------------------------
# Our own work-style descriptions
# ---------------------------------------------------------------------------
#
# Written in-house, from the four axis meanings above, and deliberately about
# collaboration mechanics — how the person wants information, decisions, and
# disagreement handled. No published profile text, no nicknames, and nothing that
# reads as a judgement of ability: per the Smart Teams contract these guides never
# inform review, promotion, or compensation, and a sentence that could be quoted in
# one has no business here.
#
# NEVER subscript this mapping outside :attr:`Type16Result.work_style`. Each entry
# is one joint claim about all four axes, so it is only safe to look up with four
# letters that all mean something — which is a question about the *scores*, not
# about the string. The property is the only place that question is asked, and
# ``WORK_STYLE[...]`` anywhere else is the bug: it is how a portrait chosen by a
# coin flip reaches somebody's own profile.

WORK_STYLE: Mapping[str, str] = {
    "ISTJ": (
        "Wants the facts, the precedent, and the deadline, then works through them "
        "in order. Send the written version early — they will find the gap in it "
        "before the meeting, and they would much rather raise it there than "
        "improvise a fix later."
    ),
    "ISFJ": (
        "Quietly keeps track of what the team committed to and who is carrying "
        "what. Ask them directly what they think: they will not volunteer a "
        "disagreement in a large room, and the thing they did not say is usually "
        "the thing worth hearing."
    ),
    "INFJ": (
        "Reads a project as a pattern of people and intentions and wants to know "
        "what it is ultimately for. Give them the reasoning in writing and a day "
        "with it; what comes back is often a framing nobody else offered."
    ),
    "INTJ": (
        "Wants the shape of the problem settled before the schedule is. Expect the "
        "plan to be questioned at the level of its premise rather than its detail, "
        "and expect the questions in a document rather than out loud."
    ),
    "ISTP": (
        "Learns a thing by taking it apart and trusts what they have tested over "
        "what they were told. Hand them the concrete problem and some room; a long "
        "planning meeting will lose them well before the decision."
    ),
    "ISFP": (
        "Works closely and quietly, to a standard they hold themselves rather than "
        "announce. They tend to absorb friction instead of escalating it, so check "
        "in deliberately rather than waiting for a flag."
    ),
    "INFP": (
        "Needs the work to line up with something they actually believe, and will "
        "say so late rather than early. Ask about reservations explicitly and in "
        "private — quiet in the room is not the same as agreement."
    ),
    "INTP": (
        "Pulls at the assumption everyone else stepped over. Most useful before a "
        "decision hardens rather than after it, and most useful in writing, where "
        "the argument can be followed and answered."
    ),
    "ESTP": (
        "Moves first and adjusts from what happens; finds the abstract version of "
        "a problem much less interesting than the live one. Decisions will land "
        "faster than the record of them, so agree up front who writes things down."
    ),
    "ESFP": (
        "Keeps the energy and the relationships in a room going, and works out "
        "loud. They will tell you where the friction is long before a status "
        "report does, and they will not enjoy a process that only meets in a "
        "spreadsheet."
    ),
    "ENFP": (
        "Generates options quickly and enthusiastically, including after the "
        "decision. Strongest at the front of a project; pair them with someone who "
        "closes, and make the moment of closing explicit."
    ),
    "ENTP": (
        "Argues the alternative on principle, which is genuinely useful and "
        "occasionally exhausting. Name the point at which the debate closes and "
        "they will respect it — what they cannot do is pretend a weak plan is fine."
    ),
    "ESTJ": (
        "Wants owners, dates, and a decision, and will supply all three if the "
        "room stalls. Bring the recommendation rather than the survey, and expect "
        "follow-up on anything that slips."
    ),
    "ESFJ": (
        "Runs on the team staying on good terms and does a lot of the invisible "
        "work of that. Thank them specifically, and disagree with the plan rather "
        "than the person, or only the second part gets heard."
    ),
    "ENFJ": (
        "Talks a direction into being, holds the group to it, and reads the mood "
        "of a room as real data. Give them the human context of a decision — a "
        "change announced as pure logistics gets re-explained by them anyway."
    ),
    "ENTJ": (
        "Wants the objective stated and the trade-offs on the table, out loud, at "
        "speed. Disagreement is not a problem for them; a vague answer is. Say the "
        "constraint plainly and early."
    ),
}

# Every combination of the eight letters must have a description, or a real person
# ends up with a code and a blank line under it.
assert len(WORK_STYLE) == 16, "sixteen combinations"
assert all(len(code) == 4 for code in WORK_STYLE), "codes are four letters"


# ---------------------------------------------------------------------------
# The rendering guard
# ---------------------------------------------------------------------------


class DerivedTypeCode(str):
    """A four-letter code whose string value already contains the qualifier.

    The failure this class exists to prevent is small and extremely likely: some
    template, log line, Slack block, or PDF cell interpolates the code, the
    qualifier lives somewhere else in the layout, and a reader walks away with
    "Cubie says I'm an INTJ" — which is a claim about a proprietary instrument we
    do not administer. Guidance in a docstring does not survive contact with a
    dozen call sites, so the guard is the type itself: ``str(code)``,
    ``f"{code}"``, ``code + ""`` and ``json.dumps(code)`` all yield
    ``"INTJ (derived from Big Five)"``. There is no code path that produces the
    bare letters as a side effect of normal formatting.

    Coin-flip letters are lowercased in the value and explained in the suffix, so
    a letter with no information in it cannot be printed as though it had any.

    The bare letters remain reachable — a comparison engine has to group people
    somehow — but only through :attr:`letters_for_matching`, named so that any
    reviewer grepping for how a bare code reached a reader finds every candidate
    in one search.
    """

    _letters: str
    _coin_flip_letters: tuple[str, ...]

    def __new__(
        cls, letters: str, coin_flip_axes: tuple[Axis, ...] = ()
    ) -> DerivedTypeCode:
        coin_flip = tuple(
            AXIS_SPECS[axis].high_letter + AXIS_SPECS[axis].low_letter
            for axis in coin_flip_axes
        )
        shown = "".join(
            letter.lower()
            if any(letter in pair for pair in coin_flip)
            else letter.upper()
            for letter in letters
        )
        suffix = (
            f"{DERIVED_QUALIFIER[:-1]}; {COIN_FLIP_NOTE})"
            if coin_flip_axes
            else DERIVED_QUALIFIER
        )
        obj = super().__new__(cls, f"{shown} {suffix}")
        obj._letters = letters
        obj._coin_flip_letters = coin_flip
        return obj

    @property
    def letters_for_matching(self) -> str:
        """The bare four letters, for grouping and comparison — never for display.

        Deliberately verbose. If this attribute appears anywhere near a template,
        an f-string that reaches a person, or a Slack payload, that is the bug.
        """
        return self._letters


#: Recognises a bare four-letter code in prose. Assembled from :data:`AXIS_SPECS`
#: rather than hardcoded, so it cannot drift from the letters this module actually
#: produces. Case-insensitive, because the lowercase coin-flip rendering has to be
#: caught too.
#: A single optional separator between the letters. Present because the
#: contiguous-only pattern was a hole rather than a simplification: it caught
#: "INTJ" and returned nothing at all for "I-N-T-J", "I N T J", "I.N.T.J" or
#: "I/N/T/J", so the qualifier rule the guide generator leans on could be walked
#: straight past by adding punctuation. Restricted to these four characters and
#: required to be the SAME character throughout (see the backreference below) so
#: that ordinary prose beginning "I saw the..." cannot be read as a code.
_CODE_SEPARATOR = r"([ .\-/]?)"

_CODE_PATTERN = re.compile(
    r"\b{}{}{}{}{}{}{}\b".format(
        f"[{AXIS_SPECS[Axis.EI].high_letter}{AXIS_SPECS[Axis.EI].low_letter}]",
        _CODE_SEPARATOR,
        f"[{AXIS_SPECS[Axis.NS].high_letter}{AXIS_SPECS[Axis.NS].low_letter}]",
        r"\1",
        f"[{AXIS_SPECS[Axis.TF].high_letter}{AXIS_SPECS[Axis.TF].low_letter}]",
        r"\1",
        f"[{AXIS_SPECS[Axis.JP].high_letter}{AXIS_SPECS[Axis.JP].low_letter}]",
    ),
    re.IGNORECASE,
)

#: The dichotomy words a spelled-out type uses. Written out rather than derived
#: because ``AxisSpec`` carries ``high_meaning``/``low_meaning`` — our own plain
#: descriptions — not these labels, and these are the words a model reaches for
#: when it has been told not to write the letters. Both poles per axis, in axis
#: order.
_SPELLED_OUT_POLES: tuple[tuple[str, ...], ...] = (
    ("extraverted", "extroverted", "introverted"),
    ("intuitive", "intuition", "sensing"),
    ("thinking", "feeling"),
    ("judging", "perceiving"),
)

#: How much text after a bare code still counts as "qualified". Long enough for
#: "(derived from Big Five)" and for "-style (derived)"; short enough that a
#: qualifier belonging to a *different* code further along the paragraph cannot
#: launder this one. The search also stops at the end of the sentence, because
#: "as an INTJ, they… Her ESFP (derived from Big Five) profile…" must not pass.
QUALIFIER_WINDOW = 32

#: Characters that end the span a qualifier may live in.
_SENTENCE_BREAK = re.compile(r"[.!?\n]")


#: Reported instead of a code when a type is spelled out in words. Not a code,
#: so it cannot be uppercased into one; callers show it as-is.
_SPELLED_OUT_MARKER = "SPELLED-OUT TYPE"

#: Sentence splitter for the spelled-out check. Deliberately separate from
#: ``_SENTENCE_BREAK``, which finds the END of one sentence after a match.
_SENTENCE_SPLIT = re.compile(r"[.!?;\n]+")


def find_unqualified_codes(text: str) -> tuple[str, ...]:
    """Four-letter codes in ``text`` that are not followed by a derived qualifier.

    For the guide generator and anything else that puts model-written prose in
    front of a person: an LLM asked to describe a profile will happily write "as an
    INTJ, they…", and no amount of prompt instruction makes that impossible. Run
    the output through here and refuse it, or fix it, before it ships.

    "Qualified" means the word "derived" appears in the same sentence, within
    :data:`QUALIFIER_WINDOW` characters after the code — which is exactly what
    :class:`DerivedTypeCode` always produces. Returns each offending code once, in
    order of first appearance, uppercased so the caller can report it plainly.
    """
    offenders: list[str] = []
    for match in _CODE_PATTERN.finditer(text):
        window = text[match.end() : match.end() + QUALIFIER_WINDOW]
        sentence_end = _SENTENCE_BREAK.search(window)
        if sentence_end:
            window = window[: sentence_end.start()]
        if "derived" in window.lower():
            continue
        code = match.group(0).upper()
        if code not in offenders:
            offenders.append(code)

    # A spelled-out type is the same assertion without the letters, and the
    # letter pattern above cannot see it. "I read as Introverted, Intuitive,
    # Thinking and Judging" asserts a four-letter type as plainly as "INTJ"
    # does, so it has to clear the same qualifier bar. Requires poles from at
    # least three of the four axes in one sentence: two co-occurring words like
    # "thinking" and "feeling" turn up in ordinary prose about how somebody
    # works, three do not.
    for sentence in _SENTENCE_SPLIT.split(text):
        lowered = sentence.lower()
        if "derived" in lowered:
            continue
        axes_named = sum(
            1
            for poles in _SPELLED_OUT_POLES
            if any(re.search(rf"\b{pole}\b", lowered) for pole in poles)
        )
        if axes_named >= 3 and _SPELLED_OUT_MARKER not in offenders:
            offenders.append(_SPELLED_OUT_MARKER)
    return tuple(offenders)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LetterCall:
    """One axis resolved into one letter, with the arithmetic left visible.

    ``source_score`` and ``distance_from_midpoint`` are kept because they are the
    honest answer to "how sure is this letter" — the letter itself has thrown that
    information away, and someone reading a guide deserves to get it back.
    """

    axis: Axis
    letter: str
    other_letter: str
    source_domain: bf.Domain
    source_score: float
    source_band: bf.Band
    confidence: Confidence

    @property
    def distance_from_midpoint(self) -> float:
        return abs(self.source_score - MIDPOINT)

    @property
    def is_coin_flip(self) -> bool:
        return self.confidence is Confidence.COIN_FLIP

    @property
    def meaning(self) -> str:
        spec = AXIS_SPECS[self.axis]
        return (
            spec.high_meaning if self.letter == spec.high_letter else spec.low_meaning
        )

    def as_dict(self) -> dict:
        return {
            "axis": self.axis.value,
            "pair": AXIS_SPECS[self.axis].letters,
            "letter": self.letter,
            "other_letter": self.other_letter,
            "source_domain": self.source_domain.value,
            "source_label": bf.DOMAIN_LABELS[self.source_domain],
            "source_score": round(self.source_score, 1),
            "source_band": self.source_band.value,
            "distance_from_midpoint": round(self.distance_from_midpoint, 1),
            "confidence": self.confidence.value,
            "confidence_label": CONFIDENCE_LABELS[self.confidence],
            "meaning": self.meaning,
            "evidence": AXIS_EVIDENCE[self.axis],
        }


@dataclass(frozen=True, slots=True)
class Type16Result:
    """The derived letters, everything that qualifies them, and no bare code.

    Note what is absent: there is no attribute that returns four plain letters for
    display. :attr:`code` is a :class:`DerivedTypeCode`, so it is already qualified
    wherever it lands, and the unqualified form is reachable only through the
    long-named accessor on that object.

    ``suppressed_reason`` is set when the sitting behind the scores should not be
    turned into a type at all — the derived layer is the first thing to withhold
    when the underlying answers look rushed, because it is the layer that loses the
    least by being withheld.
    """

    calls: tuple[LetterCall, ...]
    unresolved_axes: tuple[Axis, ...]
    suppressed_reason: str | None

    @property
    def letters(self) -> str:
        """Four characters, with :data:`UNRESOLVED_LETTER` for any missing axis.

        Internal and JSON-facing. For anything a person reads, use :attr:`code`.
        """
        by_axis = {call.axis: call.letter for call in self.calls}
        return "".join(by_axis.get(axis, UNRESOLVED_LETTER) for axis in Axis)

    @property
    def coin_flip_axes(self) -> tuple[Axis, ...]:
        return tuple(call.axis for call in self.calls if call.is_coin_flip)

    @property
    def is_reportable(self) -> bool:
        """True when all four axes resolved and nothing suppressed the layer.

        Coin-flip letters do not make a result unreportable — they make it
        hedged, which is what the lowercase rendering is for.
        """
        return not self.unresolved_axes and self.suppressed_reason is None

    @property
    def code(self) -> DerivedTypeCode | None:
        """The only display form, or ``None`` when there is nothing to show."""
        if not self.is_reportable:
            return None
        return DerivedTypeCode(self.letters, self.coin_flip_axes)

    @property
    def is_all_coin_flips(self) -> bool:
        """Every axis landed in the middle band, so the code carries no signal."""
        return len(self.coin_flip_axes) == len(Axis)

    @property
    def work_style(self) -> str | None:
        """Our work-style sentence, or ``None`` when it would be dishonest.

        Withheld whenever **any** axis is a coin flip — not only when all four
        are, which is what an earlier version of this property checked.

        The reason is that :data:`WORK_STYLE` is keyed on the whole four-letter
        string, so each of the sixteen paragraphs is a single joint claim about
        all four axes at once. A coin-flip letter is the tie-break on a score
        that ``big_five`` itself placed in its middle band, and :func:`derive`
        says in as many words that the direction of that tie-break carries no
        meaning and nothing should be built on it. A portrait selected with that
        letter in the key is therefore a portrait selected by something
        meaningless: at an extraversion score of 49.9 this returns the
        I-paragraph and at 50.1 the E-paragraph — two substantively different
        descriptions of the same person, a fifth of a point apart. The middle
        band is 40-60, a fifth of the scale, so that is the ordinary case and
        not an edge.

        The alternative was considered and rejected: keying the prose off only
        the *resolved* axes would mean authoring a portrait for every subset of
        one, two or three letters. That invents content where the honest answer
        is that we do not have it, and every one of those paragraphs would still
        read to its subject as a portrait. Withholding costs one sentence. The
        letters themselves still ship — lowercased, with :data:`COIN_FLIP_NOTE`
        attached — and the five Big Five scores are the read that actually has
        the information in it.

        Subscripting :data:`WORK_STYLE` rather than going through here is what
        this guard exists to prevent, which is why the lookup below raises on a
        missing key instead of returning ``None``: an unreportable or hedged
        result never gets that far, so a ``KeyError`` here would mean the letters
        themselves are wrong and silence would hide it.
        """
        if not self.is_reportable or self.coin_flip_axes:
            return None
        return WORK_STYLE[self.letters]

    def _coin_flip_clause(self) -> str:
        """Which axes were coin flips, and the score behind each. Never a claim."""
        return "; ".join(
            f"{AXIS_SPECS[call.axis].letters} sat in the middle band "
            f"({call.source_score:.0f} on "
            f"{bf.DOMAIN_LABELS[call.source_domain].lower()})"
            for call in self.calls
            if call.is_coin_flip
        )

    def describe(self) -> str:
        """One paragraph for a guide: the code, our sentence, and the hedges.

        Always mentions that the letters are derived (via :attr:`code`), always
        names the domain that has no letter, and names any coin-flip axis
        explicitly. If a caller wants a shorter version, it must drop the
        work-style sentence rather than the qualifications.

        There are five mutually exclusive shapes and only the last contains a
        portrait: suppressed upstream, an unresolved axis, all four axes coin
        flips, *some* axes coin flips, and everything resolved clearly. The
        fourth is the one that changed. It used to fall through to the portrait
        and append a hedge after it, which is the same defect
        :attr:`work_style` documents — a hedge does not un-say a paragraph
        somebody has already read about themselves.
        """
        if self.suppressed_reason:
            return self.suppressed_reason
        if self.unresolved_axes:
            missing = ", ".join(
                bf.DOMAIN_LABELS[AXIS_SPECS[axis].source_domain]
                for axis in self.unresolved_axes
            )
            return (
                "No letters for now: this framework needs a score for "
                f"{missing}, and that part of the interview was not answered "
                "enough to score. The domains that were scored are unaffected."
            )

        if self.is_all_coin_flips:
            return (
                f"{self.code}. All four of the scores behind these letters sat in "
                "the middle band, so the code is four coin flips and describes "
                "nothing in particular — the five scores themselves are the useful "
                f"read. {DOMAIN_WITHOUT_A_LETTER_NOTE}"
            )

        if self.coin_flip_axes:
            return (
                f"{self.code}. {WORK_STYLE_WITHHELD_NOTE} Read the lowercase "
                "letters as open questions rather than findings: "
                f"{self._coin_flip_clause()}. {DOMAIN_WITHOUT_A_LETTER_NOTE}"
            )

        return f"{self.code}. {self.work_style} {DOMAIN_WITHOUT_A_LETTER_NOTE}"

    def as_dict(self) -> dict:
        """JSON for the ``type16`` column on ``st_profiles``.

        ``display`` is the field a frontend should render. ``letters_for_matching``
        is present because the comparison engine needs to group people, and it
        ships next to ``never_render_letters_alone`` so that intent is not
        something a future reader has to infer.
        """
        code = self.code
        return {
            "layer": "type16",
            "derived_from": "big_five",
            "display": str(code) if code else None,
            "letters_for_matching": code.letters_for_matching if code else None,
            "never_render_letters_alone": True,
            "qualifier": DERIVED_QUALIFIER,
            "is_reportable": self.is_reportable,
            "suppressed_reason": self.suppressed_reason,
            "unresolved_axes": [axis.value for axis in self.unresolved_axes],
            "coin_flip_axes": [axis.value for axis in self.coin_flip_axes],
            "axes": [call.as_dict() for call in self.calls],
            "work_style": self.work_style,
            "description": self.describe(),
            "domain_without_a_letter": DOMAIN_WITHOUT_A_LETTER.value,
            "domain_without_a_letter_note": DOMAIN_WITHOUT_A_LETTER_NOTE,
            "not_an_mbti_result": NOT_AN_MBTI_RESULT,
            "no_nicknames": WHY_NO_NICKNAMES,
            "advisory_notice": ADVISORY_NOTICE,
        }


# ---------------------------------------------------------------------------
# The colleague-facing renderer
# ---------------------------------------------------------------------------


def _coin_flip_clause_for_colleagues(coin_flip_axes: Sequence[Axis]) -> str:
    """Which axes were coin flips, named — and with no score attached.

    The difference from :meth:`Type16Result._coin_flip_clause` is the whole point
    of this function: that one interpolates ``call.source_score``, so it renders
    "T/F sat in the middle band (40 on people-weighting)". Forty is the subject's
    raw Agreeableness score. In a subject-facing view that number is the most
    honest thing on the page. In a company-visible guide it is a raw psychometric
    score about a named person, published to everybody and ingested into the
    companywide RAG corpus, which is the one thing the Smart Teams contract
    reserves to the subject and to support admins. So the axis is named, the
    scale it came from is named, and the number is not.
    """
    parts = [
        f"{AXIS_SPECS[axis].letters} "
        f"({bf.DOMAIN_LABELS[AXIS_SPECS[axis].source_domain].lower()})"
        for axis in coin_flip_axes
    ]
    if len(parts) == 1:
        return parts[0]
    return f"{', '.join(parts[:-1])} and {parts[-1]}"


def colleague_safe_description(payload: Mapping[str, Any]) -> str | None:
    """The derived-layer paragraph for a document somebody's colleagues read.

    Takes the stored ``type16`` layer dict (:meth:`Type16Result.as_dict`) rather
    than a result object, because the only caller is
    ``smart_teams/guide.py``, which has a payload out of ``st_profiles`` and not
    a live scoring run — and because that makes this work for a row scored by an
    earlier deploy without a migration.

    ``None`` when there is nothing safe to say: an unreportable layer, a missing
    ``display``, or a shape this function does not recognise. A missing section is
    a correct outcome here; the caller has no obligation to render this layer at
    all.

    What this deliberately does NOT do is call :meth:`Type16Result.describe`.
    That method is right for the subject's own view and wrong for this one, in
    two ways that both reached production: its partial-coin-flip branch prints
    raw Big Five domain scores, and every branch addresses the reader as "you"
    inside a document written as "I".
    """
    if not payload.get("is_reportable"):
        return None
    display = payload.get("display")
    if not isinstance(display, str) or not display.strip():
        return None
    display = display.strip()

    axes_raw = payload.get("coin_flip_axes") or []
    coin_flip: list[Axis] = []
    for value in axes_raw:
        try:
            coin_flip.append(Axis(value))
        except ValueError:
            # An axis name this module does not know means the payload was
            # written by something that is not this module. Say nothing.
            return None

    if len(coin_flip) == len(Axis):
        return (
            f"{display}. All four of the scores behind these letters sat in the "
            "middle band, so the code is four coin flips and describes nothing in "
            "particular. "
            f"{DOMAIN_WITHOUT_A_LETTER_NOTE_FOR_COLLEAGUES}"
        )

    if coin_flip:
        return (
            f"{display}. {WORK_STYLE_WITHHELD_NOTE_FOR_COLLEAGUES} "
            "Read the lowercase letters as "
            "open questions rather than findings: the pairs left open are "
            f"{_coin_flip_clause_for_colleagues(coin_flip)}. "
            f"{DOMAIN_WITHOUT_A_LETTER_NOTE_FOR_COLLEAGUES}"
        )

    work_style = payload.get("work_style")
    if not isinstance(work_style, str) or not work_style.strip():
        return None
    return (
        f"{display}. {work_style.strip()} {DOMAIN_WITHOUT_A_LETTER_NOTE_FOR_COLLEAGUES}"
    )


def derive(result: bf.BigFiveResult) -> Type16Result:
    """Restate a scored Big Five sitting as four letters. No measurement happens.

    Always returns a result object rather than ``None``: "we are not showing you a
    type, and here is why" is information a guide needs, and a caller handed
    ``None`` has to invent that sentence itself.

    Two things stop a code being produced. An axis whose source domain went
    unscored leaves that axis unresolved — ``big_five`` withholds a domain below
    :data:`big_five.MIN_ANSWERED_PER_DOMAIN` answers, and defaulting the missing
    score to the midpoint would manufacture a coin-flip letter out of nothing.
    And a sitting carrying blocking response-quality flags suppresses the layer
    entirely (see :attr:`big_five.ResponseQuality.blocking_flags`); the advisory
    flags do not, because a lean toward agreeing is a question to ask the person,
    not grounds for withholding their own letters.
    """
    calls: list[LetterCall] = []
    unresolved: list[Axis] = []

    for axis in Axis:
        spec = AXIS_SPECS[axis]
        scored = result.domains.get(spec.source_domain)
        if scored is None:
            unresolved.append(axis)
            continue
        # An exact 50.0 has to fall somewhere; it falls on the low letter, and the
        # band it came from makes that letter a COIN_FLIP, which is the part that
        # actually reaches a reader. The tie-break direction carries no meaning and
        # nothing should be built on it.
        high = scored.score > MIDPOINT
        calls.append(
            LetterCall(
                axis=axis,
                letter=spec.high_letter if high else spec.low_letter,
                other_letter=spec.low_letter if high else spec.high_letter,
                source_domain=spec.source_domain,
                source_score=scored.score,
                source_band=scored.band,
                confidence=CONFIDENCE_BY_BAND[scored.band],
            )
        )

    blocking = result.quality.blocking_flags
    suppressed = None
    if blocking:
        reasons = " ".join(
            bf.QUALITY_FLAG_NOTES[flag] for flag in bf.QualityFlag if flag in blocking
        )
        suppressed = (
            "No letters for this version of the profile. The four-letter layer is "
            "derived from the scale answers, so it is the first thing we hold back "
            f"when those look rushed. {reasons} The scales can be retaken at any "
            "time."
        )

    return Type16Result(
        calls=tuple(calls),
        unresolved_axes=tuple(unresolved),
        suppressed_reason=suppressed,
    )

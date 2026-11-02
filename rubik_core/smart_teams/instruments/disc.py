"""The DISC-style layer of the Work Style Interview (ST-1).

What this layer measures
------------------------
Four dimensions of *observable working behavior* — not ability, not intelligence,
not fitness for a job. Marston's 1928 framework derives them from two questions a
person answers, mostly unconsciously, about any situation they walk into: is this
environment on my side or against me, and am I stronger than it or is it stronger
than me. The four combinations are the four dimensions. Marston's own labels were
Dominance, Inducement, Submission and Compliance; the modern generic labels below
are the ones people actually recognize at work, and are what we use in prose:

* **D — Dominance.** Pace and directness. How readily the person makes the call,
  says the uncomfortable thing, changes route mid-plan, and pushes into
  resistance rather than around it.
* **I — Influence.** Outward energy. How much the person thinks out loud, works a
  problem by talking it through, persuades, and seeks people contact over
  solitary focus.
* **S — Steadiness.** Cadence and consistency. How much the person values a
  predictable workload, advance notice before a change, sustained pace, and the
  health of the working relationship.
* **C — Conscientiousness.** Standards and evidence. How much the person needs to
  see the numbers themselves, work a documented process, and be right rather
  than fast.

The two axes underneath are the same pair the interpersonal-circumplex literature
calls agency (getting ahead) and communion (getting along): D and I are the
high-agency half, I and S the high-communion half. That is why the *blend* — the
primary plus secondary dimension — carries almost all the useful information and
a single letter carries very little. A high-D person paired with high C behaves
nothing like a high-D person paired with high I, and the working guide is written
off the blend, not the letter.

Legal provenance — why we may build this at all
-----------------------------------------------
Marston published the model in *Emotions of Normal People* (1928). He never
patented it and never built an assessment from it, which is precisely why roughly
fifty vendors were able to build competing proprietary instruments on top of the
same public-domain model — Clarke's Activity Vector Analysis (1956), Clarke and
Merenda's validation work, Geier's instrument that became Inscape's and then
Wiley's product line, TTI's, and many more. The *model* is common property. Each
vendor's *instrument* is not.

So the line we hold is: we own the model, we author everything else ourselves.
Every item in the banks below was written in-house. Nothing here is
adapted, paraphrased, or "inspired by" a vendor item, and no vendor report prose
appears in the style descriptions. Specifically forbidden anywhere in this module
or its output: Gallup CliftonStrengths items or the 34 theme names, Wiley
"Everything DiSC" items or that trademarked lowercase-i styling (we always write
DISC in full caps — the lowercase i is Wiley's mark, reportedly originating as a
printer's error they then protected), MBTI items or any "Myers-Briggs" product
claim, and TTI report text. The seventeen style names in ``WORK_STYLES`` were
authored here, from the dimension definitions and from how these behaviours
actually show up at work. The vendor style vocabularies they must stay clear
of are enumerated in ``docs/smart_teams/instruments/disc.md`` §2, alongside the
cross-layer read that found three of these names colliding exactly with the newer
sibling layers — those were resolved by renaming *there*, and nothing here moved.
Adding or renaming a style means redoing both reads by hand. The vendor half in
particular cannot become a test, because the test would have to carry the vendor
lists, which is the intellectual property §2 exists to keep out of the repository.

What this layer is for, and what it must never be used for
----------------------------------------------------------
Advisory only: collaboration guidance and project-team staffing. Never
performance review, never promotion, never compensation. That is not squeamish
hedging — using a personality instrument as an input to employment decisions is a
real Title VII / ADA exposure, and an unvalidated one is the worst version of it.
Team-fit guidance carries none of that risk. ``ADVISORY_NOTICE`` is the single
source of truth for the disclaimer, and ``DiscResult.as_dict`` puts it on every
result, so a downstream surface has to actively drop it rather than merely forget
to write it.

Design notes worth knowing before you change anything here
----------------------------------------------------------
* **Two banks, on purpose.** 24 Likert-agreement items are *normative* — one
  person's D score is comparable to another's, which is what the Team Builder
  needs. 24 forced-choice pairs are *ipsative* — they only rank the four
  dimensions inside one person, but they are immune to the person who agrees with
  every statement put in front of them. Scoring blends them 60/40 so we get
  cross-person comparability with an acquiescence brake on it. The ipsative
  caveat is real and documented in ``docs/smart_teams/instruments/disc.md``.
* **Renormalize, don't zero** — the same rule as the X-Ray churn scorer. A
  dimension with no answered Likert items is scored on its forced-choice
  component alone at full weight, rather than having "we didn't ask" quietly
  averaged in as "low on that axis". A dimension with nothing answered in *either*
  bank has nothing to renormalize onto, so it gets no intensity and no band at all
  — ``None``, not 0 — and the whole profile then declines to name a style rather
  than ranking three real dimensions against one invented number.
* **Response-quality thresholds are shared with the Big Five layer where the
  index is the same.** Both layers score the same sitting, so a respondent must
  not come out straight-lined on one and clean on the other. ``big_five`` says so
  explicitly above its own constants; this is the layer that has to comply, and
  ``docs/smart_teams/instruments/disc.md`` §8 carries the same table so a reader
  of either file finds the divergences without reading the other.
  ``MIN_RESPONSE_VARIABILITY`` is ``big_five.MIN_RESPONSE_SD`` itself rather than
  a copy of its value, and ``STRAIGHT_LINE_RUN`` applies big_five's own rule —
  two full cycles of the interleave — to a four-dimension bank instead of a
  five-domain one. Three thresholds deliberately diverge, and each divergence has
  a structural reason recorded at the constant: ``MAX_PAIR_DISCREPANCY`` (a mean
  of absolute per-pair gaps, which cannot cancel noise the way big_five's
  difference-of-means keying gap does), completeness (gated on the whole bank
  because a forced-choice pair contests two dimensions at once, so "items
  answered for this dimension" is not the same countable thing), and the absence
  of neutral-overuse and acquiescence indices (the ipsative bank is this layer's
  acquiescence brake, and a neutral-heavy respondent lands flat and is routed to
  the hedged All-Rounder rather than given a confident blend).
* **Reverse-keyed items are load-bearing.** Eight of the 24 Likert items are
  reverse-keyed, and every one of them is paired with a positively keyed
  near-opposite in ``ANTONYM_PAIRS``. Those eight pairs are how
  ``check_response_quality`` tells a considered profile from a straight-lined one.
  A rushed interview that produces a confident, wrong profile is worse for us than
  no profile — the person then walks around with a company-visible guide
  describing someone else.
* **Presentation order is part of the instrument.** ``LIKERT_ITEMS`` is ordered so
  consecutive items come from different dimensions and the reverse-keyed items are
  spread out; the forced-choice pairs alternate which side of the pair each
  dimension sits on. Scoring is order-independent, but the straight-lining and
  position-bias checks are not, so do not re-sort these tuples.
* **No LLM anywhere in this file.** Scoring, blend assignment and quality checks
  are pure and deterministic; identical answers always produce an identical
  profile. The LLM's only job downstream is writing prose *from* this output.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

# The Big Five layer owns the house response-quality thresholds and says so at
# the constants: both layers score the same sitting, so where an index is built
# the same way in both, this module uses big_five's number rather than a copy of
# it that can drift. See the "Response-quality thresholds" design note above.
from .big_five import MIN_RESPONSE_SD as HOUSE_MIN_RESPONSE_SD

Dimension = Literal["D", "I", "S", "C"]
Keying = Literal["positive", "reverse"]
Choice = Literal["a", "b"]

#: Canonical dimension order. Also the deterministic tiebreak order when two
#: dimensions land on the same intensity — without a fixed order, two identical
#: interviews could produce two different blends.
DIMENSIONS: tuple[Dimension, ...] = ("D", "I", "S", "C")

DIMENSION_NAMES: Mapping[Dimension, str] = {
    "D": "Dominance",
    "I": "Influence",
    "S": "Steadiness",
    "C": "Conscientiousness",
}

#: Marston's original 1928 labels, kept for the docs and the methodology footnote
#: in the generated guide. We do not use these in user-facing prose — "Submission"
#: reads as an insult in a 2026 workplace, which is exactly why the field
#: relabelled them.
MARSTON_LABELS: Mapping[Dimension, str] = {
    "D": "Dominance",
    "I": "Inducement",
    "S": "Submission",
    "C": "Compliance",
}

DIMENSION_SUMMARIES: Mapping[Dimension, str] = {
    "D": "Moves fast, decides early, and would rather have the hard conversation now than let it sit.",
    "I": "Thinks out loud, works problems socially, and carries the room when the work needs selling.",
    "S": "Holds a sustainable pace, wants notice before change, and protects the working relationship.",
    "C": "Wants the evidence, the process, and the version of the answer that survives review.",
}

ADVISORY_NOTICE = (
    "This profile describes working preferences that you told us about yourself. "
    "It is advisory: it exists to help colleagues work with you and to help staff "
    "project teams. It is never used for performance review, promotion, or "
    "compensation decisions, and it says nothing about your ability or your "
    "results. You approve it before anyone else sees it, you can retake the "
    "interview at any time, and you can unpublish it whenever you want."
)

# ── The Likert bank ───────────────────────────────────────────────────────────
LIKERT_MIN = 1
LIKERT_MAX = 5

#: The anchors the interview must show. Scoring assumes these five points, so the
#: interview page is required to render the labels ``interview_payload()`` hands
#: it rather than hardcoding a set of its own.
LIKERT_ANCHORS: Mapping[int, str] = {
    1: "Not like me",
    2: "A little like me",
    3: "Sometimes like me",
    4: "Mostly like me",
    5: "Very like me",
}


def _check_scale() -> None:
    """The scale is defined twice here, and reverse scoring only works while the
    two definitions agree.

    ``LIKERT_MIN``/``LIKERT_MAX`` are the bounds a reverse-keyed response is
    flipped against; ``LIKERT_ANCHORS`` is what the interview renders and what
    the validators accept. Add a sixth anchor without moving the bounds and every
    input still validates while the eight reverse-keyed items quietly score
    against the wrong midpoint — a batch of slightly wrong profiles, with nothing
    raising. Failing at import is much cheaper than finding that later.
    """
    scored_range = tuple(range(LIKERT_MIN, LIKERT_MAX + 1))
    if tuple(LIKERT_ANCHORS) != scored_range:
        raise RuntimeError(
            f"DISC Likert scale: anchors {tuple(LIKERT_ANCHORS)} do not match "
            f"the scored range {scored_range}"
        )


_check_scale()


@dataclass(frozen=True)
class LikertItem:
    """One agreement statement.

    ``keying`` is the direction the item points: a ``"reverse"`` item is worded so
    that *disagreeing* with it indicates the dimension, and its raw response is
    flipped before it enters the sum. Reverse items exist to break the rhythm of a
    respondent who has stopped reading, so never quietly convert one to positive
    keying to "simplify" the bank.
    """

    item_id: str
    dimension: Dimension
    keying: Keying
    prompt: str

    @property
    def reverse(self) -> bool:
        return self.keying == "reverse"

    def score(self, response: int) -> int:
        """The response as it enters the dimension sum (reverse items flipped)."""
        if response not in LIKERT_ANCHORS:
            raise ValueError(
                f"{self.item_id}: response {response!r} outside the "
                f"{LIKERT_MIN}-{LIKERT_MAX} scale"
            )
        if self.reverse:
            return LIKERT_MIN + LIKERT_MAX - response
        return response


#: 24 items, six per dimension (four positively keyed, two reverse-keyed),
#: interleaved so that no two consecutive items share a dimension. Order matters
#: to the quality checks — see the module docstring.
LIKERT_ITEMS: tuple[LikertItem, ...] = (
    LikertItem(
        "disc_d_01",
        "D",
        "positive",
        "When a decision is stalling, I will make the call myself rather than wait for the group to converge.",
    ),
    LikertItem(
        "disc_i_01",
        "I",
        "positive",
        "I work a problem out best by talking it through with someone.",
    ),
    LikertItem(
        "disc_s_05",
        "S",
        "reverse",
        "Being handed a brand-new priority mid-week energizes me more than it unsettles me.",
    ),
    LikertItem(
        "disc_c_01",
        "C",
        "positive",
        "Before I sign off on something, I want to see the numbers behind it myself.",
    ),
    LikertItem(
        "disc_d_05",
        "D",
        "reverse",
        "I would rather hand a hard call up to my manager than make it myself.",
    ),
    LikertItem(
        "disc_i_02",
        "I",
        "positive",
        "I would rather pick up the phone or start a huddle than trade another round of email.",
    ),
    LikertItem(
        "disc_s_01",
        "S",
        "positive",
        "I would rather carry a steady, predictable workload than ride peaks and crashes.",
    ),
    LikertItem(
        "disc_c_05",
        "C",
        "reverse",
        "I am comfortable shipping at eighty percent and correcting it later.",
    ),
    LikertItem(
        "disc_d_02",
        "D",
        "positive",
        "I would rather be told the bottom line first and fill in the details afterward.",
    ),
    LikertItem(
        "disc_i_05",
        "I",
        "reverse",
        "A day with no meetings and no interruptions is my best kind of day.",
    ),
    LikertItem(
        "disc_s_02",
        "S",
        "positive",
        "When someone on my team is underwater, I pick up part of their work without being asked.",
    ),
    LikertItem(
        "disc_c_02",
        "C",
        "positive",
        "I would rather be late and right than early and approximately right.",
    ),
    LikertItem(
        "disc_d_03",
        "D",
        "positive",
        "Disagreeing with someone in a meeting does not bother me if it moves the work forward.",
    ),
    LikertItem(
        "disc_i_03",
        "I",
        "positive",
        "I enjoy being the one who explains our work to a room I have not met before.",
    ),
    LikertItem(
        "disc_s_06",
        "S",
        "reverse",
        "I would rather work alone on my own timeline than coordinate closely with the same group every day.",
    ),
    LikertItem(
        "disc_c_03",
        "C",
        "positive",
        "I keep a written process for the work I do repeatedly, and I follow it.",
    ),
    LikertItem(
        "disc_d_06",
        "D",
        "reverse",
        "I hold my opinion back in a meeting until I can tell how the room is leaning.",
    ),
    LikertItem(
        "disc_i_04",
        "I",
        "positive",
        "I tend to assume a difficult situation will come out fine, and I say so out loud.",
    ),
    LikertItem(
        "disc_s_03",
        "S",
        "positive",
        "I want to know why a change is happening well before it happens.",
    ),
    LikertItem(
        "disc_c_06",
        "C",
        "reverse",
        "Detailed rules and checklists slow me down more than they help me.",
    ),
    LikertItem(
        "disc_d_04",
        "D",
        "positive",
        "When a plan stops working, I would rather change route than finish the original plan.",
    ),
    LikertItem(
        "disc_i_06",
        "I",
        "reverse",
        "I would rather send my thinking in writing than present it live.",
    ),
    LikertItem(
        "disc_s_04",
        "S",
        "positive",
        "I will sit with a tense conversation for a while rather than force it to a conclusion.",
    ),
    LikertItem(
        "disc_c_04",
        "C",
        "positive",
        "A vague request bothers me until I have pinned down exactly what is being asked.",
    ),
)

#: Near-antonym pairs: one positively keyed item and one reverse-keyed item on the
#: same dimension whose content is close to opposite. After reverse-scoring, a
#: person who read the questions gives both members of a pair a similar score, so
#: the mean absolute gap across these pairs is our inconsistency index — the
#: "psychometric antonyms" idea from the careless-responding literature, computed
#: within-person because we score one interview at a time and have no sample
#: correlation matrix to mine.
ANTONYM_PAIRS: tuple[tuple[str, str], ...] = (
    ("disc_d_01", "disc_d_05"),
    ("disc_d_03", "disc_d_06"),
    ("disc_i_03", "disc_i_06"),
    ("disc_i_01", "disc_i_05"),
    ("disc_s_01", "disc_s_05"),
    ("disc_s_02", "disc_s_06"),
    ("disc_c_02", "disc_c_05"),
    ("disc_c_03", "disc_c_06"),
)


# ── The forced-choice bank ────────────────────────────────────────────────────
#: The stem the pairs are answered against, and the one piece of forced-choice
#: wording that is not in the pairs themselves. It lives here for the same reason
#: ``LIKERT_ANCHORS`` does: "which of these is more like you" and "which of these
#: would you prefer" are different questions, and the second one would quietly
#: turn an ipsative self-description into a wish list. The interview page renders
#: this string rather than composing its own.
FORCED_CHOICE_PROMPT = "Pick the one that is more like you."


@dataclass(frozen=True)
class ChoiceOption:
    """One side of a forced-choice pair. Both sides must be genuinely appealing —
    a pair where one option is obviously the "good" answer measures nothing but
    social desirability."""

    dimension: Dimension
    text: str


@dataclass(frozen=True)
class ForcedChoicePair:
    """A "pick the one that is more like you" pair contrasting two dimensions.

    Which dimension sits in the ``a`` slot is deliberately alternated across the
    bank so that a respondent who just clicks the left option every time shows up
    in ``check_response_quality`` as position bias instead of as a clean profile.
    """

    item_id: str
    option_a: ChoiceOption
    option_b: ChoiceOption

    def dimension_for(self, choice: Choice) -> Dimension:
        if choice == "a":
            return self.option_a.dimension
        if choice == "b":
            return self.option_b.dimension
        raise ValueError(f"{self.item_id}: choice {choice!r} is not 'a' or 'b'")

    @property
    def dimensions(self) -> tuple[Dimension, Dimension]:
        return (self.option_a.dimension, self.option_b.dimension)


def _pair(
    item_id: str,
    first: tuple[Dimension, str],
    second: tuple[Dimension, str],
) -> ForcedChoicePair:
    return ForcedChoicePair(
        item_id,
        ChoiceOption(first[0], first[1]),
        ChoiceOption(second[0], second[1]),
    )


#: 24 pairs: four for each of the six dimension combinations, so every dimension
#: is contested exactly twelve times and no dimension gets an easy run. Ordered so
#: consecutive pairs contest different combinations.
FORCED_CHOICE_PAIRS: tuple[ForcedChoicePair, ...] = (
    _pair(
        "disc_fc_di_01",
        ("D", "Get the decision made today."),
        ("I", "Get everyone bought in before we move."),
    ),
    _pair(
        "disc_fc_ds_01",
        ("D", "Push a stalled project onto a new track."),
        ("S", "Give a stalled project the time it needs to come around."),
    ),
    _pair(
        "disc_fc_dc_01",
        ("D", "Decide now, on most of the facts."),
        ("C", "Wait a day for the rest of the facts."),
    ),
    _pair(
        "disc_fc_is_01",
        ("I", "Open a project with a room full of people I have not worked with."),
        ("S", "Open a project with the group I already know well."),
    ),
    _pair(
        "disc_fc_ic_01",
        ("I", "Tell the story the data supports."),
        ("C", "Show the data and let it speak."),
    ),
    _pair(
        "disc_fc_sc_01",
        ("S", "Keep the team steady through a messy audit."),
        ("C", "Keep the findings airtight through a messy audit."),
    ),
    _pair(
        "disc_fc_di_02",
        ("I", "Stay in the meeting until the room feels good about the answer."),
        ("D", "End the meeting the moment the answer is clear."),
    ),
    _pair(
        "disc_fc_ds_02",
        ("S", "Find the right moment and a softer way to say the work missed."),
        ("D", "Say plainly, today, that the work missed."),
    ),
    _pair(
        "disc_fc_dc_02",
        ("C", "Work the process, and file a fix for what is broken in it."),
        ("D", "Route around the process when it is in the way."),
    ),
    _pair(
        "disc_fc_is_02",
        ("S", "Raise the uncomfortable point one-on-one afterward."),
        ("I", "Raise the uncomfortable point in the room, in the moment."),
    ),
    _pair(
        "disc_fc_ic_02",
        ("C", "Draft it carefully and circulate it when it is right."),
        ("I", "Draft it fast and sharpen it live with the group."),
    ),
    _pair(
        "disc_fc_sc_02",
        ("C", "Hold the standard and let the working relationship take the strain."),
        ("S", "Bend a little on the standard to keep the working relationship."),
    ),
    _pair(
        "disc_fc_di_03",
        ("D", "Be handed the hardest account on the list."),
        ("I", "Be handed the most visible account on the list."),
    ),
    _pair(
        "disc_fc_ds_03",
        ("D", "Set the pace for the team."),
        ("S", "Match the pace the team can sustain."),
    ),
    _pair(
        "disc_fc_dc_03",
        ("D", "Be judged on results delivered."),
        ("C", "Be judged on work that holds up under review."),
    ),
    _pair(
        "disc_fc_is_03",
        ("I", "Sell the idea to the business."),
        ("S", "Support the person who has to carry the idea."),
    ),
    _pair(
        "disc_fc_ic_03",
        ("I", "Own the client relationship."),
        ("C", "Own the report the client relies on."),
    ),
    _pair(
        "disc_fc_sc_03",
        ("S", "Make sure everyone has been heard before we finalize."),
        ("C", "Make sure everything has been checked before we finalize."),
    ),
    _pair(
        "disc_fc_di_04",
        ("I", "Win the person over."),
        ("D", "Win the argument on the merits."),
    ),
    _pair(
        "disc_fc_ds_04",
        ("S", "Protect the commitments I have already made."),
        ("D", "Take on more than is comfortable in order to get more done."),
    ),
    _pair(
        "disc_fc_dc_04",
        ("C", "Give the client an answer I can fully document."),
        ("D", "Give the client a firm answer fast."),
    ),
    _pair(
        "disc_fc_is_04",
        ("S", "Handle the follow-up nobody sees."),
        ("I", "Take the microphone at the all-hands."),
    ),
    _pair(
        "disc_fc_ic_04",
        ("C", "Be known for precision."),
        ("I", "Be known for energy."),
    ),
    _pair(
        "disc_fc_sc_04",
        ("C", "Be the person the numbers rely on."),
        ("S", "Be the person the team leans on."),
    ),
)

ITEM_COUNT = len(LIKERT_ITEMS) + len(FORCED_CHOICE_PAIRS)

_LIKERT_BY_ID: Mapping[str, LikertItem] = {i.item_id: i for i in LIKERT_ITEMS}
_PAIRS_BY_ID: Mapping[str, ForcedChoicePair] = {
    p.item_id: p for p in FORCED_CHOICE_PAIRS
}


def interview_payload() -> dict[str, Any]:
    """Exactly what the interview page needs to render this layer — and nothing
    more. Keying and dimension are deliberately withheld: a respondent who can
    see which answer scores which dimension is answering a different instrument.
    """
    return {
        "layer": "disc",
        "scale": [
            {"value": value, "label": label} for value, label in LIKERT_ANCHORS.items()
        ],
        "likert": [
            {"item_id": item.item_id, "prompt": item.prompt} for item in LIKERT_ITEMS
        ],
        "forced_choice_prompt": FORCED_CHOICE_PROMPT,
        "forced_choice": [
            {
                "item_id": pair.item_id,
                "option_a": pair.option_a.text,
                "option_b": pair.option_b.text,
            }
            for pair in FORCED_CHOICE_PAIRS
        ],
    }


# ── The style taxonomy (ours) ─────────────────────────────────────────────────
@dataclass(frozen=True)
class WorkStyle:
    """One named working style — a primary dimension, optionally with a secondary.

    This is the content the "How to work with {NAME}" generator draws on, so every
    field has to be specific enough that a colleague could act on it before lunch.
    If a line would be equally true of any of the seventeen styles, it does not
    belong here.
    """

    key: str
    name: str
    primary: Dimension | None
    secondary: Dimension | None
    identity: str
    communication: tuple[str, ...]
    energized_by: tuple[str, ...]
    drained_by: tuple[str, ...]
    under_stress: str
    feedback: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "primary": self.primary,
            "secondary": self.secondary,
            "identity": self.identity,
            "communication": list(self.communication),
            "energized_by": list(self.energized_by),
            "drained_by": list(self.drained_by),
            "under_stress": self.under_stress,
            "feedback": self.feedback,
        }


#: Seventeen styles: four single-dominant, twelve ordered blends (primary first —
#: D+C and C+D are genuinely different people), and one honest label for a flat
#: profile. The names are ours; before adding or renaming one, read the vendor
#: vocabularies listed in ``docs/smart_teams/instruments/disc.md`` §2 and the
#: sibling layers' taxonomies by hand — no code checks either for you.
WORK_STYLES: Mapping[str, WorkStyle] = {
    "D": WorkStyle(
        key="D",
        name="Straight Line",
        primary="D",
        secondary=None,
        identity="Goes at the objective directly and expects the shortest path to be the right one.",
        communication=(
            "Lead with the ask and the deadline; context second, if they want it.",
            "Keep updates to what changed and what you need from them.",
            "Say the disagreement out loud — hinting reads as having nothing to say.",
        ),
        energized_by=(
            "A problem with a clear finish line and the authority to go get it.",
            "Being handed the thing nobody else wants to touch.",
        ),
        drained_by=(
            "Meetings that recap rather than decide.",
            "Waiting on an approval nobody will own.",
        ),
        under_stress="Gets blunter and faster, starts making other people's calls for them, and stops explaining the reasoning.",
        feedback="Direct and immediate, with the business consequence attached. They will argue; that is engagement, not rejection. Land the point once and let them come back to it.",
    ),
    "I": WorkStyle(
        key="I",
        name="Open Floor",
        primary="I",
        secondary=None,
        identity="Thinks out loud and moves work by getting people talking about it.",
        communication=(
            "Talk it through live; a call settles in ten minutes what email takes three days to do.",
            "Expect half-formed ideas — they are drafting, not committing.",
            "Follow the conversation with two lines in writing so the decision survives it.",
        ),
        energized_by=(
            "New rooms, new people, and work that needs explaining to them.",
            "Visible momentum and a team that is talking to each other.",
        ),
        drained_by=(
            "Long solo stretches with no one to react to.",
            "Being told to put it in a template when they wanted to discuss it.",
        ),
        under_stress="Talks more and closes less, over-commits to keep the mood up, and lets the unglamorous follow-through slide.",
        feedback="In person and in private, warmth first, and specific about the behavior rather than the person. Write down what you agreed at the end — otherwise the good feeling is what gets remembered.",
    ),
    "S": WorkStyle(
        key="S",
        name="Steady Hand",
        primary="S",
        secondary=None,
        identity="Holds a sustainable pace and is the reason things stay finished after they ship.",
        communication=(
            "Give notice. A change announced Monday for Thursday lands; one announced Thursday for Thursday does not.",
            "Ask directly for their view — they will not fight the room for airtime.",
            "Say what stays the same, not just what is changing.",
        ),
        energized_by=(
            "Repeat work done well, with a group they trust.",
            "Being the person others come to when something has to hold.",
        ),
        drained_by=(
            "Reorganized priorities mid-week with no explanation.",
            "Open conflict left unresolved in the room.",
        ),
        under_stress="Goes quiet and absorbs it — takes on the extra work, agrees in the meeting, and lets the resentment show up weeks later as disengagement.",
        feedback="Privately, calmly, with the relationship stated up front and time to think afterward. Come back in a week; the first conversation is when they hear it, the second is when they respond.",
    ),
    "C": WorkStyle(
        key="C",
        name="Fine Print",
        primary="C",
        secondary=None,
        identity="Would rather be right than quick, and is usually the reason the work survives review.",
        communication=(
            "Send the detail in writing before the meeting; they will have read it.",
            "Bring the source, not the summary — 'roughly' invites a follow-up question.",
            "Silence after a question is thinking, not resistance.",
        ),
        energized_by=(
            "Work with a defensible standard to hold it against.",
            "Finding the error before the client does.",
        ),
        drained_by=(
            "Being asked to sign off on something they have not been allowed to check.",
            "Process changed by announcement, with no documentation behind it.",
        ),
        under_stress="Retreats into detail, re-verifies work already verified, and slows the decision down rather than deciding on an incomplete picture.",
        feedback="Specific, evidenced, and unhurried. One vague criticism will cost you the next hour. Separate 'this was wrong' from 'this was late' — they will hear the first as an attack on their competence unless you say which you mean.",
    ),
    "D+I": WorkStyle(
        key="D+I",
        name="Front Runner",
        primary="D",
        secondary="I",
        identity="Drives hard and brings people with them at the same time — the one who takes the hill and narrates it.",
        communication=(
            "Match their energy and get to the point; they read hesitation as a no.",
            "Give them the headline they can repeat to the room.",
            "Pin the commitments in writing — the conversation moved fast and covered a lot.",
        ),
        energized_by=(
            "A visible, hard target with an audience.",
            "Being the one who tells the client the plan.",
        ),
        drained_by=(
            "Detail review with no decision attached.",
            "Being asked to slow down and wait for consensus that is already forming.",
        ),
        under_stress="Overruns people, promises more than the team can deliver, and mistakes agreement in the room for a plan.",
        feedback="Fast, direct, and framed as what would make them more effective. Do it privately — they will take a public correction as a status hit and spend the next week proving you wrong.",
    ),
    "D+S": WorkStyle(
        key="D+S",
        name="Quiet Force",
        primary="D",
        secondary="S",
        identity="Pushes as hard as anyone but does it without noise — decisions get made and nobody gets run over.",
        communication=(
            "Ask directly; they will give you a straight answer without volunteering it.",
            "Short and factual works. So does silence.",
            "If they say a date, take it as a commitment.",
        ),
        energized_by=(
            "Owning something end to end with room to run it their way.",
            "A team that does what it said it would do.",
        ),
        drained_by=(
            "Churn — the same decision reopened for the third time.",
            "Performative urgency with no real deadline behind it.",
        ),
        under_stress="Stops consulting and just does it, then holds a private ledger of who did not carry their share.",
        feedback="Plainly and one-on-one. Lead with what you have seen them do well, because they will not assume it. Ask what they need removed rather than what they need to improve.",
    ),
    "D+C": WorkStyle(
        key="D+C",
        name="Case Builder",
        primary="D",
        secondary="C",
        identity="Decides fast, but only on ground that will hold — moves quickly and expects the evidence to keep up.",
        communication=(
            "Bring the recommendation and the reasoning in the same breath.",
            "Do not round the numbers; they will ask for the real one.",
            "Expect to be pushed on your logic — that is how they check it, not how they attack you.",
        ),
        energized_by=(
            "A hard problem where being right actually matters.",
            "Fixing a process that was quietly producing bad answers.",
        ),
        drained_by=(
            "Opinion presented as fact.",
            "Being asked to endorse a decision made on a feeling.",
        ),
        under_stress="Turns exacting and impatient at once — demands rigour on someone else's timeline and starts redoing other people's work.",
        feedback="Evidenced and unsentimental. Show the example. They respect being corrected far more than being managed, and they will discount praise that is not attached to something specific.",
    ),
    "I+D": WorkStyle(
        key="I+D",
        name="Closer",
        primary="I",
        secondary="D",
        identity="Persuades toward a decision — good in the room, and unwilling to leave it without an answer.",
        communication=(
            "Talk first, document after; they close verbally.",
            "Give them something to advocate for, not just something to review.",
            "Confirm scope in writing — enthusiasm has a way of expanding it.",
        ),
        energized_by=(
            "Winning over a sceptical room.",
            "A deadline with people on the other side of it.",
        ),
        drained_by=(
            "Administrative follow-through and status reporting.",
            "Being kept out of the conversation and sent the notes.",
        ),
        under_stress="Sells past the close — keeps talking, commits on behalf of others, and treats an unresolved objection as handled.",
        feedback="Warm opening, specific example, clear ask, and a written summary. Frame it as protecting their credibility, which is the currency they actually care about.",
    ),
    "I+S": WorkStyle(
        key="I+S",
        name="Team Glue",
        primary="I",
        secondary="S",
        identity="The reason the group functions — reads the room, keeps people connected, and notices who has gone quiet.",
        communication=(
            "Start with the people side; the task lands better after it.",
            "Invite the criticism explicitly — they will soften it on their own.",
            "Do not deliver hard news by message; give them a conversation.",
        ),
        energized_by=(
            "A group that likes working together and is doing well.",
            "Being trusted with what people actually think.",
        ),
        drained_by=(
            "Conflict they are expected to sit inside without resolving.",
            "Being the one who has to deliver a decision they disagree with.",
        ),
        under_stress="Absorbs everyone's problems, avoids the necessary confrontation, and says yes to things they cannot deliver.",
        feedback="Private, gentle in framing, specific in substance — the gentleness must not blur the point, or they will hear reassurance and change nothing. Confirm what you agreed the next day.",
    ),
    "I+C": WorkStyle(
        key="I+C",
        name="Pitch and Proof",
        primary="I",
        secondary="C",
        identity="Sells the idea and insists the numbers behind it are right — persuasive because they have checked.",
        communication=(
            "Talk it through, then send the sourced version; they want both.",
            "If you challenge the story, challenge the data too — they will follow you there.",
            "Give them time to verify before asking them to present it.",
        ),
        energized_by=(
            "Turning something technical into something a client can act on.",
            "Being the person the room believes because they have never been caught out.",
        ),
        drained_by=(
            "Being asked to present a conclusion they have not been able to check.",
            "Work that is all internal and never explained to anyone.",
        ),
        under_stress="Over-prepares and over-explains, defends the analysis instead of hearing the objection, and misses the deadline polishing the deck.",
        feedback="Specific and evidenced, delivered conversationally rather than in a document. Separate the substance from the delivery explicitly — they will otherwise assume a criticism of the pitch is a criticism of the work.",
    ),
    "S+D": WorkStyle(
        key="S+D",
        name="Backstop",
        primary="S",
        secondary="D",
        identity="Holds the line quietly and will push hard when something genuinely matters — rare, and worth listening to.",
        communication=(
            "Ask, then wait. The answer is considered and they will not repeat it.",
            "When they object, treat it as significant; they do not object often.",
            "Give context and notice; they will handle the rest.",
        ),
        energized_by=(
            "Being the reliable one on work that has to be right.",
            "Fixing something that has been broken for a long time.",
        ),
        drained_by=(
            "Repeated last-minute changes with no reason given.",
            "Watching a decision get reversed by whoever spoke most recently.",
        ),
        under_stress="Goes quiet, carries the extra load, and then draws a hard line without warning — the objection you never heard becomes an immovable position.",
        feedback="Private, calm, concrete. Ask what they have been carrying that you did not know about; the answer is usually the real conversation.",
    ),
    "S+I": WorkStyle(
        key="S+I",
        name="Open Door",
        primary="S",
        secondary="I",
        identity="Steady and approachable — the person everyone talks to, and the reason new people get up to speed.",
        communication=(
            "Conversational and unhurried; they will tell you more than a status report would.",
            "Ask what they are worried about — they will not lead with it.",
            "Announce changes with the reason and the timeline together.",
        ),
        energized_by=(
            "Helping someone else get good at something.",
            "A stable team doing familiar work well.",
        ),
        drained_by=(
            "Being pulled onto a new thing every week.",
            "Having to enforce a rule they were not consulted on.",
        ),
        under_stress="Says yes to everything, quietly falls behind, and protects everyone else's workload before their own.",
        feedback="Warm, private, and specific — and ask them to say it back, because they will agree pleasantly with something they did not actually accept.",
    ),
    "S+C": WorkStyle(
        key="S+C",
        name="Process Keeper",
        primary="S",
        secondary="C",
        identity="Keeps the system running and notices the drift before it becomes a finding.",
        communication=(
            "Written, specific, and with enough lead time to work it into the queue.",
            "Say what standard applies; ambiguity is what stalls them.",
            "Expect an accurate answer rather than a fast one.",
        ),
        energized_by=(
            "Work with a rhythm and a standard, done properly.",
            "Being the person who knows how it actually works.",
        ),
        drained_by=(
            "Urgent exceptions that bypass the process.",
            "Being told the documentation can wait.",
        ),
        under_stress="Defends the process instead of the outcome, and would rather do it correctly and late than approximately and on time.",
        feedback="Prepared, private, and evidenced. Tell them the standard you are measuring against, and give them a route to fix it — a criticism with no remedy attached will be carried around for a month.",
    ),
    "C+D": WorkStyle(
        key="C+D",
        name="Standard Setter",
        primary="C",
        secondary="D",
        identity="Sets the bar and then holds people to it — precise, and willing to be unpopular about it.",
        communication=(
            "Come with the detail and the position; vagueness gets challenged.",
            "Expect the pointed question. It is about the work, not about you.",
            "If you disagree, bring the evidence rather than the seniority.",
        ),
        energized_by=(
            "Raising the quality of something that was being done badly.",
            "Being the final check that nothing gets past.",
        ),
        drained_by=(
            "Being overruled without a reason.",
            "Repeating the same correction to the same person.",
        ),
        under_stress="Gets critical and cold, escalates on detail, and starts treating an error as a verdict on the person who made it.",
        feedback="Precise, evidenced, no cushioning — and be ready to defend it. Praise works only when it names the specific thing; general encouragement is heard as noise.",
    ),
    "C+I": WorkStyle(
        key="C+I",
        name="Translator",
        primary="C",
        secondary="I",
        identity="Gets it right and then makes it make sense to everyone else — the bridge between the detail and the room.",
        communication=(
            "Give them the technical version; they will produce the human one.",
            "Written first, discussed second — they like to be accurate before they are engaging.",
            "Tell them who the audience is; it changes everything about their answer.",
        ),
        energized_by=(
            "Explaining something complicated to someone who needed it explained.",
            "Being trusted as both correct and clear.",
        ),
        drained_by=(
            "Being asked to simplify past the point of accuracy.",
            "Detail work with no one on the other end of it.",
        ),
        under_stress="Over-qualifies everything, buries the answer in caveats, and spends the time on the explanation rather than the decision.",
        feedback="Conversational and specific, with the example in front of you. Name whether the issue is the accuracy or the delivery — they hold those separately and will fix the wrong one otherwise.",
    ),
    "C+S": WorkStyle(
        key="C+S",
        name="Groundwork",
        primary="C",
        secondary="S",
        identity="Builds the foundation carefully and unhurriedly — quiet, thorough, and the reason nothing collapses later.",
        communication=(
            "In writing, with the full picture, and with time to work through it.",
            "Do not rush the answer; the considered version is the valuable one.",
            "Ask specifically for concerns — they will not raise them unprompted.",
        ),
        energized_by=(
            "Deep work with a clear standard and no interruptions.",
            "Building something that will still be right in two years.",
        ),
        drained_by=(
            "Reactive work and shifting requirements.",
            "Being pushed to commit before they have checked.",
        ),
        under_stress="Withdraws and keeps verifying — the work gets more thorough and less visible, and the deadline arrives without a status update.",
        feedback="Private, prepared, and patient. Ask questions rather than making pronouncements, then give them a day. They will come back with a better answer than the one you were going to suggest.",
    ),
    "BALANCED": WorkStyle(
        key="BALANCED",
        name="All-Rounder",
        primary=None,
        secondary=None,
        identity="No single working style dominates — adapts to what the situation in front of them needs.",
        communication=(
            "Ask how they want to work on this piece of work rather than assuming a default.",
            "Their preference shifts by context: read the situation, not the profile.",
            "Be explicit about what you need; they will flex to it.",
        ),
        energized_by=(
            "Varied work that uses different gears.",
            "Being the person who can slot into whatever the team is short of.",
        ),
        drained_by=(
            "Being typecast into one mode permanently.",
            "Having their flexibility taken as having no preferences.",
        ),
        under_stress="Hard to predict from the profile alone — the useful information is in what they have told colleagues directly, not in these scores.",
        feedback="Ask them how they prefer to receive it. A flat profile usually means either genuine adaptability or an interview that did not differentiate; the conversation settles which.",
    ),
}


# ── Scoring ───────────────────────────────────────────────────────────────────
#: The normative Likert bank carries the majority weight because it is what makes
#: one person's score comparable to another's — which is what the Team Builder
#: needs. The ipsative forced-choice bank carries the rest: it cannot be gamed by
#: agreeing with everything, so it acts as a brake on acquiescence and as the
#: tiebreaker between two dimensions a person endorsed equally.
LIKERT_WEIGHT = 0.6
CHOICE_WEIGHT = 0.4

#: A dimension must lead the next one by at least this much to be called
#: single-dominant rather than a blend.
SINGLE_STYLE_GAP = 15
#: A profile whose whole span comes to *fewer* than this many points has nothing
#: really leading it, so we say so instead of inventing a blend out of noise. The
#: boundary itself is a blend: a spread of 12 gets a style, a spread of 11 gets
#: the All-Rounder.
LEVEL_PROFILE_SPREAD = 12

INTENSITY_BANDS: tuple[tuple[int, str], ...] = (
    (25, "Low"),
    (45, "Moderate"),
    (70, "High"),
    (101, "Very high"),
)


def intensity_band(intensity: int) -> str:
    """Word for an intensity, so the guide never has to print a bare number."""
    for ceiling, label in INTENSITY_BANDS:
        if intensity < ceiling:
            return label
    return INTENSITY_BANDS[-1][1]


@dataclass
class DimensionScore:
    """One dimension's 0-100 intensity plus every input that produced it.

    Both components are reported separately and unrounded-in-spirit so that a
    person who asks "why is my C a 71" can be shown the arithmetic. A ``None``
    component means that bank had nothing answered for this dimension and its
    weight was renormalized away.

    ``intensity`` and ``band`` are themselves ``None`` when *neither* bank had
    anything for this dimension. There is nothing to renormalize onto in that
    case, and a 0 would be a measurement we never took — see the
    renormalize-don't-zero note in the module docstring.
    """

    dimension: Dimension
    intensity: int | None
    band: str | None
    likert_answered: int
    likert_raw: int | None
    likert_percent: float | None
    choice_answered: int
    choice_wins: int
    choice_percent: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "name": DIMENSION_NAMES[self.dimension],
            "intensity": self.intensity,
            "band": self.band,
            "likert_answered": self.likert_answered,
            "likert_raw": self.likert_raw,
            "likert_percent": (
                None if self.likert_percent is None else round(self.likert_percent, 1)
            ),
            "choice_answered": self.choice_answered,
            "choice_wins": self.choice_wins,
            "choice_percent": (
                None if self.choice_percent is None else round(self.choice_percent, 1)
            ),
        }


@dataclass
class StyleAssignment:
    """The blend decision, with the margins that produced it.

    ``confidence`` is about how firmly the *style* was determined, not about how
    accurate the person's self-report was. ``primary_margin`` is the gap from the
    leading dimension to the second; ``secondary_margin`` is the gap from the
    second to the third — a small secondary margin means the blend could
    plausibly have been a different one, which the guide should hedge about.

    Every field except ``confidence`` is ``None`` (or, for ``ranking``, empty)
    when a dimension went entirely unanswered and no style could honestly be
    named. Margins computed against an unscored dimension would be arithmetic on
    a number nobody supplied, so there are none to report.
    """

    style: WorkStyle | None
    primary: Dimension | None
    secondary: Dimension | None
    ranking: tuple[Dimension, ...]
    primary_margin: int | None
    secondary_margin: int | None
    spread: int | None
    confidence: Literal["high", "moderate", "low"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "style": None if self.style is None else self.style.as_dict(),
            "primary": self.primary,
            "secondary": self.secondary,
            "ranking": list(self.ranking),
            "primary_margin": self.primary_margin,
            "secondary_margin": self.secondary_margin,
            "spread": self.spread,
            "confidence": self.confidence,
        }


# Quality-flag codes. These are the contract the interview page and the retake
# prompt switch on, so a rename here is a breaking change rather than a tidy-up.
FLAG_INCOMPLETE = "incomplete"
FLAG_STRAIGHT_LINING = "straight_lining"
FLAG_LOW_VARIABILITY = "low_variability"
FLAG_INCONSISTENT = "inconsistent_reverse_pairs"
FLAG_POSITION_BIAS = "forced_choice_position_bias"
FLAG_TOO_FAST = "implausibly_fast"

#: A run of identical Likert answers this long or longer is flagged; the longest
#: run we tolerate is therefore one short of it. Two full cycles of the interleave
#: is big_five's rule for the same index (``MAX_IDENTICAL_RUN``, two cycles of its
#: five domains), applied here to four dimensions rather than copied across from a
#: bank twice this length. A genuine profile essentially cannot sustain a run this
#: long: any eight consecutive items cover all four dimensions twice over, and
#: wherever the window falls in ``LIKERT_ITEMS`` it picks up at least two
#: statements worded against their own dimension, which the run has to answer
#: against their content to stay flat.
STRAIGHT_LINE_RUN = 2 * len(DIMENSIONS)
#: Standard deviation of the Likert answers. Below this the person effectively
#: gave one answer with jitter. Same construction as big_five's response-SD check
#: over its own mixed-keying bank — same scale, same statistic — so it is that
#: constant (0.40 as this is written) rather than a second opinion about the same
#: sitting. It is the looser of the two candidate numbers: near-single-anchor
#: patterns that would have tripped a 0.45 floor and not this one are mostly
#: caught anyway by the run-length check above or the response-range companion
#: below, and we would rather carry that residue than have the two layers
#: disagree about one respondent.
MIN_RESPONSE_VARIABILITY = HOUSE_MIN_RESPONSE_SD
#: Distance between the highest and lowest Likert answer. Its companion above
#: cannot do this job alone: a perfect alternation between two *adjacent* anchors
#: — the classic near-straight-line — sits at a population standard deviation of
#: exactly 0.50, clear of the floor above, while a run-length check sees a run of
#: 1. Requiring the answers to span more than two neighbouring anchors across 24
#: interleaved items, eight of them worded against their own dimension, is what
#: actually catches it. Deliberately a warning, not a retake: a genuinely
#: middling profile can honestly live inside two anchors, and that person is
#: already routed to the hedged All-Rounder style.
MIN_RESPONSE_RANGE = 2
#: Below this many answered Likert items the range check has too little to look
#: at to mean anything.
MIN_ITEMS_FOR_RANGE_CHECK = 12
#: Mean absolute gap, on the 1-5 scale, between the two halves of the
#: reverse-keyed antonym pairs after reverse-scoring. Some gap is normal — the
#: items are near-opposites, not exact ones.
#:
#: Deliberately looser than big_five's ``MAX_KEYING_GAP``, and the divergence is
#: structural rather than a second opinion: that index is the distance between two
#: *means* (all plus-keyed items in a domain against all minus-keyed ones), where
#: independent noise cancels, so a consistent respondent lands near zero. This one
#: averages eight absolute per-pair gaps, which cannot cancel — every bit of
#: ordinary item-to-item noise pushes it up. The two numbers are on the same 1-5
#: scale but not on the same footing, and aligning them would flag careful people.
#:
#: Worth knowing before anyone "improves" this number: answering the pairs at
#: random gives an expected gap of 1.6, which is arithmetic rather than a
#: measurement — the mean absolute difference of two independent draws from a
#: uniform 1-5 scale is (5² − 1) / (3 · 5). That sits *below* this threshold, so
#: random clicking is invisible to this index by construction (see
#: ``docs/smart_teams/instruments/disc.md`` §9). Eight pairs is too little
#: resolution to close that gap by tightening the threshold instead of adding
#: pairs, so this index is the *slow* check and the elapsed-time check is what
#: actually catches a clicked-through interview. Retune against real
#: response data; we have none yet.
MAX_PAIR_DISCREPANCY = 1.75
#: A single pair this far apart contradicts itself outright.
SEVERE_PAIR_DISCREPANCY = 3
#: This many outright self-contradictions — not one fewer — and we stop trusting
#: the profile.
MAX_SEVERE_PAIRS = 3
#: Forced-choice answers landing on the same side at this share or above is
#: clicking rather than choosing.
MAX_POSITION_SHARE = 0.9
#: Below this many answered pairs the position share is too coarse to accuse
#: anyone of anything — four out of four on one side is a 100% share and means
#: nothing. Named rather than inlined so the doc can cite it and so it cannot
#: drift away from its Likert-side counterpart above without someone noticing.
MIN_CHOICES_FOR_POSITION_CHECK = 12
#: Seconds per answered item below which nobody read the question.
MIN_SECONDS_PER_ITEM = 2.0
#: Below this share of the bank the profile is not scorable at all. Gated on the
#: whole bank rather than per dimension, which is where this diverges from
#: big_five's ``MIN_ANSWERED_PER_DOMAIN``: half of this bank is forced-choice
#: pairs and every pair contests two dimensions at once, so "items answered for
#: this dimension" is not the same countable quantity there and here. The
#: per-dimension protection is elsewhere — scoring prorates each dimension against
#: what it actually answered, and a dimension with nothing answered at all gets no
#: score rather than a low one.
MIN_COMPLETION_SHARE = 0.75
#: Blanks up to here are ordinary — somebody skipped an item they did not want to
#: answer, and ``score_dimensions`` prorates around it. Only past this does the
#: profile become an assembly of a partial interview worth warning about. Without
#: a floor here a single skipped item spends a warning, and two warnings are a
#: retake, so one blank plus one soft index would have sent an honest interview
#: back.
TOLERATED_BLANKS = 4


@dataclass
class QualityFlag:
    """One response-quality finding. ``severity`` "retake" means we should not
    publish a guide off these answers; "warn" means show the profile but soften
    the confidence language."""

    code: str
    severity: Literal["warn", "retake"]
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "severity": self.severity, "detail": self.detail}


@dataclass
class ResponseQuality:
    """Whether these answers can carry a confident profile.

    The point of this object is that a rushed interview gets *flagged* rather than
    silently producing a confident, wrong guide. A person whose company-visible
    guide describes someone else is a worse outcome than a person who is asked to
    take the interview again.
    """

    likert_answered: int
    choice_answered: int
    longest_run: int
    response_variability: float | None
    response_range: int | None
    mean_pair_discrepancy: float | None
    severe_pairs: int
    position_share: float | None
    seconds_per_item: float | None
    flags: list[QualityFlag]

    @property
    def retake_recommended(self) -> bool:
        """True on any "retake"-severity flag, or on two independent warnings —
        two weak signals pointing the same way is the common shape of a genuinely
        careless interview, where no single index quite trips."""
        if any(f.severity == "retake" for f in self.flags):
            return True
        return sum(1 for f in self.flags if f.severity == "warn") >= 2

    def as_dict(self) -> dict[str, Any]:
        return {
            "likert_answered": self.likert_answered,
            "choice_answered": self.choice_answered,
            "longest_run": self.longest_run,
            "response_variability": (
                None
                if self.response_variability is None
                else round(self.response_variability, 3)
            ),
            "response_range": self.response_range,
            "mean_pair_discrepancy": (
                None
                if self.mean_pair_discrepancy is None
                else round(self.mean_pair_discrepancy, 2)
            ),
            "severe_pairs": self.severe_pairs,
            "position_share": (
                None if self.position_share is None else round(self.position_share, 3)
            ),
            "seconds_per_item": (
                None
                if self.seconds_per_item is None
                else round(self.seconds_per_item, 1)
            ),
            "retake_recommended": self.retake_recommended,
            "flags": [f.as_dict() for f in self.flags],
        }


@dataclass
class DiscResult:
    """The whole deterministic output of this layer.

    Everything downstream — the guide generator, the pair comparison, the Team
    Builder's style-complementarity heuristics — reads this and only this. The LLM
    writes prose from it and can never change a number in it.
    """

    scores: dict[Dimension, DimensionScore]
    assignment: StyleAssignment
    quality: ResponseQuality

    @property
    def intensities(self) -> dict[Dimension, int | None]:
        """``None`` for any dimension nothing was answered for. Callers that do
        arithmetic across the four have to handle that rather than treating the
        gap as a zero."""
        return {d: s.intensity for d, s in self.scores.items()}

    def as_dict(self) -> dict[str, Any]:
        return {
            "layer": "disc",
            "scores": {d: self.scores[d].as_dict() for d in DIMENSIONS},
            "assignment": self.assignment.as_dict(),
            "quality": self.quality.as_dict(),
            "advisory_notice": ADVISORY_NOTICE,
        }


def _validated_likert(responses: Mapping[str, int]) -> dict[str, int]:
    """Reject unknown ids and out-of-scale values loudly.

    A typo'd item id that silently scored as "unanswered" would quietly shift a
    dimension and nobody would ever notice, so this is deliberately strict.
    """
    clean: dict[str, int] = {}
    for item_id, value in responses.items():
        item = _LIKERT_BY_ID.get(item_id)
        if item is None:
            raise ValueError(f"unknown DISC Likert item id: {item_id!r}")
        if value is None:
            continue
        if value not in LIKERT_ANCHORS:
            raise ValueError(
                f"{item_id}: response {value!r} outside the "
                f"{LIKERT_MIN}-{LIKERT_MAX} scale"
            )
        clean[item_id] = int(value)
    return clean


def _validated_choices(responses: Mapping[str, str]) -> dict[str, Choice]:
    clean: dict[str, Choice] = {}
    for item_id, value in responses.items():
        if item_id not in _PAIRS_BY_ID:
            raise ValueError(f"unknown DISC forced-choice pair id: {item_id!r}")
        if value is None:
            continue
        side = str(value).strip().lower()
        if side not in ("a", "b"):
            raise ValueError(f"{item_id}: choice {value!r} is not 'a' or 'b'")
        clean[item_id] = side  # type: ignore[assignment]
    return clean


def score_dimensions(
    likert: Mapping[str, int],
    choices: Mapping[str, str],
) -> dict[Dimension, DimensionScore]:
    """Raw answers -> per-dimension 0-100 intensity. Pure and deterministic.

    Per dimension: the Likert items are summed with reverse items flipped and
    scaled to 0-100 against the range its *answered* items could span (so a
    partially answered dimension is prorated, not penalized); the forced-choice
    component is the share of contests that dimension won. The two combine
    60/40. If either bank contributed nothing, the other takes the full weight —
    the churn scorer's renormalize-don't-zero rule, for the same reason: "we did
    not ask" must never read as "low on that axis". If *neither* bank contributed
    anything there is no weight left to move, so that dimension comes back with
    ``intensity`` and ``band`` of ``None`` and ``style_for_scores`` declines to
    name a style off the set.
    """
    clean_likert = _validated_likert(likert)
    clean_choices = _validated_choices(choices)

    likert_raw: dict[Dimension, int] = dict.fromkeys(DIMENSIONS, 0)
    likert_n: dict[Dimension, int] = dict.fromkeys(DIMENSIONS, 0)
    for item in LIKERT_ITEMS:
        response = clean_likert.get(item.item_id)
        if response is None:
            continue
        likert_raw[item.dimension] += item.score(response)
        likert_n[item.dimension] += 1

    choice_wins: dict[Dimension, int] = dict.fromkeys(DIMENSIONS, 0)
    choice_n: dict[Dimension, int] = dict.fromkeys(DIMENSIONS, 0)
    for pair in FORCED_CHOICE_PAIRS:
        side = clean_choices.get(pair.item_id)
        if side is None:
            continue
        # Both dimensions were contested; only the picked one scores. Counting the
        # contest for both is what makes the denominator a fair "win rate".
        for dimension in pair.dimensions:
            choice_n[dimension] += 1
        choice_wins[pair.dimension_for(side)] += 1

    scores: dict[Dimension, DimensionScore] = {}
    for dimension in DIMENSIONS:
        n = likert_n[dimension]
        raw = likert_raw[dimension] if n else None
        # n answered items span n*LIKERT_MIN .. n*LIKERT_MAX.
        likert_pct = (
            None
            if not n
            else (raw - n * LIKERT_MIN) / (n * (LIKERT_MAX - LIKERT_MIN)) * 100.0
        )
        contests = choice_n[dimension]
        choice_pct = None if not contests else choice_wins[dimension] / contests * 100.0

        blended: float | None
        if likert_pct is not None and choice_pct is not None:
            blended = LIKERT_WEIGHT * likert_pct + CHOICE_WEIGHT * choice_pct
        elif likert_pct is not None:
            blended = likert_pct
        elif choice_pct is not None:
            blended = choice_pct
        else:
            # Nothing answered anywhere for this dimension, so there is no
            # surviving component to renormalize onto. 0 would not be the honest
            # number, it would be an invented one: it enters the ranking as "low
            # on that axis" and pushes the three real dimensions up the order
            # behind a measurement nobody took. No intensity, no band, and
            # style_for_scores refuses to name a style off a set containing one.
            blended = None

        intensity = None if blended is None else max(0, min(100, round(blended)))
        scores[dimension] = DimensionScore(
            dimension=dimension,
            intensity=intensity,
            band=None if intensity is None else intensity_band(intensity),
            likert_answered=n,
            likert_raw=raw,
            likert_percent=likert_pct,
            choice_answered=contests,
            choice_wins=choice_wins[dimension],
            choice_percent=choice_pct,
        )
    return scores


def style_for_scores(intensities: Mapping[Dimension, int | None]) -> StyleAssignment:
    """Intensities -> a named style. Pure: same numbers in, same style out.

    Four outcomes, in order:

    1. **Unscorable.** Any dimension that came back ``None`` — nothing answered
       for it in either bank — and we name no style at all. Ranking three real
       dimensions against a fourth we never measured would produce a confident
       blend out of a blank, which is the failure this whole module is built to
       avoid.
    2. **Flat profile.** If the whole set spans fewer than
       ``LEVEL_PROFILE_SPREAD`` points, nothing is leading and we return the
       All-Rounder rather than promoting noise into a blend.
    3. **Single-dominant.** The leader clears the runner-up by at least
       ``SINGLE_STYLE_GAP``, so the secondary adds nothing worth naming.
    4. **Blend.** Otherwise the top two, in order — D+C and C+D describe
       different people, so the order is part of the answer.

    Ties break on ``DIMENSIONS`` order. That choice is arbitrary but it must be
    fixed: without it, two runs of the same interview could disagree.
    """
    missing = [d for d in DIMENSIONS if d not in intensities]
    if missing:
        raise ValueError(f"missing intensities for dimensions: {missing}")

    scored: dict[Dimension, int] = {}
    for dimension in DIMENSIONS:
        value = intensities[dimension]
        if value is None:
            return StyleAssignment(
                style=None,
                primary=None,
                secondary=None,
                ranking=(),
                primary_margin=None,
                secondary_margin=None,
                spread=None,
                confidence="low",
            )
        scored[dimension] = int(value)

    ranking = tuple(sorted(DIMENSIONS, key=lambda d: (-scored[d], DIMENSIONS.index(d))))
    values = [scored[d] for d in ranking]
    spread = values[0] - values[-1]
    primary_margin = values[0] - values[1]
    secondary_margin = values[1] - values[2]

    if spread < LEVEL_PROFILE_SPREAD:
        return StyleAssignment(
            style=WORK_STYLES["BALANCED"],
            primary=None,
            secondary=None,
            ranking=ranking,
            primary_margin=primary_margin,
            secondary_margin=secondary_margin,
            spread=spread,
            confidence="low",
        )

    primary = ranking[0]
    if primary_margin >= SINGLE_STYLE_GAP:
        return StyleAssignment(
            style=WORK_STYLES[primary],
            primary=primary,
            secondary=None,
            ranking=ranking,
            primary_margin=primary_margin,
            secondary_margin=secondary_margin,
            spread=spread,
            confidence="high" if primary_margin >= 25 else "moderate",
        )

    secondary = ranking[1]
    # How firmly the runner-up earned the second slot is what makes a blend
    # trustworthy — a 1-point gap to third means the blend was nearly a different
    # one, and the guide should say so.
    if secondary_margin >= 12:
        confidence: Literal["high", "moderate", "low"] = "high"
    elif secondary_margin >= 5:
        confidence = "moderate"
    else:
        confidence = "low"

    return StyleAssignment(
        style=WORK_STYLES[f"{primary}+{secondary}"],
        primary=primary,
        secondary=secondary,
        ranking=ranking,
        primary_margin=primary_margin,
        secondary_margin=secondary_margin,
        spread=spread,
        confidence=confidence,
    )


def _longest_run(values: Sequence[int | None]) -> int:
    """Longest run of identical consecutive answers. A skipped item breaks the
    run rather than joining the answers around it — otherwise a person could
    straight-line half the bank, skip one item, and have the index look clean."""
    best = 0
    run = 0
    previous: int | None = None
    for value in values:
        if value is None:
            run = 0
            previous = None
            continue
        run = run + 1 if value == previous else 1
        previous = value
        best = max(best, run)
    return best


def check_response_quality(
    likert: Mapping[str, int],
    choices: Mapping[str, str],
    *,
    elapsed_seconds: float | None = None,
) -> ResponseQuality:
    """Decide whether these answers deserve to become a published profile.

    Four independent indices, all from the careless-responding literature and all
    computed within-person (we score one interview at a time, so there is no
    sample correlation matrix to mine):

    * **Longstring** — the longest run of identical Likert answers. Catches
      straight-lining, and nothing else.
    * **Intra-individual response variability, with a response-range companion** —
      the standard deviation of the Likert answers, plus how far apart the highest
      and lowest of them are. Together they catch the near-straight-liner who
      alternates between two adjacent options, which longstring cannot see; the
      standard deviation alone cannot see it either, because a clean alternation
      between neighbouring anchors sits at exactly 0.50.
    * **Antonym-pair discrepancy** — the mean absolute gap across
      ``ANTONYM_PAIRS`` after reverse-scoring. Catches someone agreeing with a
      statement and its opposite, which is the signature of not reading.
    * **Forced-choice position share** — how often the same side was picked.
      Catches clicking down one column.

    Plus completeness, and pace if the caller has it. Every cutoff here is a
    judgement call: the literature is explicit that these indices have no
    universal thresholds, so ours are set from the shape of this bank (24
    interleaved items, 8 of them reverse-keyed) and should be revisited once we
    have real response data to look at. They are deliberately loose — this
    check exists to catch the interview nobody actually took, not to second-guess
    people who answered honestly and consistently.
    """
    clean_likert = _validated_likert(likert)
    clean_choices = _validated_choices(choices)
    flags: list[QualityFlag] = []

    ordered = [clean_likert.get(item.item_id) for item in LIKERT_ITEMS]
    answered = [v for v in ordered if v is not None]
    likert_answered = len(answered)
    choice_answered = len(clean_choices)

    total_answered = likert_answered + choice_answered
    completion = total_answered / ITEM_COUNT if ITEM_COUNT else 0.0
    if completion < MIN_COMPLETION_SHARE:
        flags.append(
            QualityFlag(
                FLAG_INCOMPLETE,
                "retake",
                f"Only {total_answered} of {ITEM_COUNT} items answered "
                f"({completion:.0%}); not enough to place a style.",
            )
        )
    elif ITEM_COUNT - total_answered > TOLERATED_BLANKS:
        flags.append(
            QualityFlag(
                FLAG_INCOMPLETE,
                "warn",
                f"{ITEM_COUNT - total_answered} of {ITEM_COUNT} items left blank.",
            )
        )

    longest_run = _longest_run(ordered)
    if longest_run >= STRAIGHT_LINE_RUN:
        flags.append(
            QualityFlag(
                FLAG_STRAIGHT_LINING,
                "retake",
                f"{longest_run} identical answers in a row across items that "
                "alternate dimensions and include reverse-worded statements.",
            )
        )

    variability = statistics.pstdev(answered) if likert_answered >= 2 else None
    response_range = max(answered) - min(answered) if answered else None
    narrow_range = (
        response_range is not None
        and likert_answered >= MIN_ITEMS_FOR_RANGE_CHECK
        and response_range < MIN_RESPONSE_RANGE
    )
    if variability is not None and variability < MIN_RESPONSE_VARIABILITY:
        flags.append(
            QualityFlag(
                FLAG_LOW_VARIABILITY,
                "warn",
                f"Answers barely varied (spread {variability:.2f} on a 1-5 scale).",
            )
        )
    elif narrow_range:
        # Same finding, different signature — one flag either way, so a person
        # cannot be charged two warnings for one behaviour.
        flags.append(
            QualityFlag(
                FLAG_LOW_VARIABILITY,
                "warn",
                f"All {likert_answered} answers sat within two neighbouring points "
                "of the five-point scale, including the statements worded the "
                "other way round.",
            )
        )

    discrepancies: list[int] = []
    for positive_id, reverse_id in ANTONYM_PAIRS:
        first = clean_likert.get(positive_id)
        second = clean_likert.get(reverse_id)
        if first is None or second is None:
            continue
        discrepancies.append(
            abs(
                _LIKERT_BY_ID[positive_id].score(first)
                - _LIKERT_BY_ID[reverse_id].score(second)
            )
        )
    mean_discrepancy = statistics.fmean(discrepancies) if discrepancies else None
    severe_pairs = sum(1 for d in discrepancies if d >= SEVERE_PAIR_DISCREPANCY)
    if severe_pairs >= MAX_SEVERE_PAIRS:
        flags.append(
            QualityFlag(
                FLAG_INCONSISTENT,
                "retake",
                f"{severe_pairs} of {len(discrepancies)} reverse-keyed pairs "
                "contradict each other outright.",
            )
        )
    elif mean_discrepancy is not None and mean_discrepancy > MAX_PAIR_DISCREPANCY:
        flags.append(
            QualityFlag(
                FLAG_INCONSISTENT,
                "warn",
                f"Reverse-keyed pairs disagree by {mean_discrepancy:.1f} points on "
                "average, more than near-opposite wording explains.",
            )
        )

    position_share: float | None = None
    if choice_answered:
        a_picks = sum(1 for side in clean_choices.values() if side == "a")
        position_share = max(a_picks, choice_answered - a_picks) / choice_answered
        if (
            choice_answered >= MIN_CHOICES_FOR_POSITION_CHECK
            and position_share >= MAX_POSITION_SHARE
        ):
            flags.append(
                QualityFlag(
                    FLAG_POSITION_BIAS,
                    "warn",
                    f"{position_share:.0%} of forced-choice answers landed on the "
                    "same side, though the bank alternates which side each "
                    "dimension appears on.",
                )
            )

    seconds_per_item: float | None = None
    if elapsed_seconds is not None and total_answered:
        seconds_per_item = elapsed_seconds / total_answered
        if seconds_per_item < MIN_SECONDS_PER_ITEM:
            flags.append(
                QualityFlag(
                    FLAG_TOO_FAST,
                    "retake",
                    f"{seconds_per_item:.1f}s per item — too fast to have read them.",
                )
            )

    return ResponseQuality(
        likert_answered=likert_answered,
        choice_answered=choice_answered,
        longest_run=longest_run,
        response_variability=variability,
        response_range=response_range,
        mean_pair_discrepancy=mean_discrepancy,
        severe_pairs=severe_pairs,
        position_share=position_share,
        flags=flags,
        seconds_per_item=seconds_per_item,
    )


def score_disc(
    likert: Mapping[str, int],
    choices: Mapping[str, str],
    *,
    elapsed_seconds: float | None = None,
) -> DiscResult:
    """The one entry point: answers in, complete DISC-style layer out.

    ``likert`` maps Likert item ids to 1-5; ``choices`` maps forced-choice pair
    ids to ``"a"`` or ``"b"``. Missing items are allowed and prorated; unknown ids
    and out-of-range values raise, because a silently dropped answer is a silently
    wrong profile.

    Style confidence is floored to "low" whenever the quality checks recommend a
    retake. The style is still returned — the person should be able to see what
    their answers produced — but nothing downstream should describe it
    confidently, and the interview page should offer the retake.

    The one case with no style at all is a dimension nobody answered anything
    for: ``assignment.style`` is then ``None``, because the alternative is a
    named blend resting on a number we invented. The per-dimension scores and the
    quality flags are still returned, so the page can show what did come back and
    which items are missing.
    """
    scores = score_dimensions(likert, choices)
    quality = check_response_quality(likert, choices, elapsed_seconds=elapsed_seconds)
    assignment = style_for_scores({d: s.intensity for d, s in scores.items()})
    if quality.retake_recommended and assignment.confidence != "low":
        assignment = StyleAssignment(
            style=assignment.style,
            primary=assignment.primary,
            secondary=assignment.secondary,
            ranking=assignment.ranking,
            primary_margin=assignment.primary_margin,
            secondary_margin=assignment.secondary_margin,
            spread=assignment.spread,
            confidence="low",
        )
    return DiscResult(scores=scores, assignment=assignment, quality=quality)

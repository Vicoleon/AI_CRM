"""The Enneagram layer of the Work Style Interview (ST-1) — typed, confidence-scored, declinable.

Read this before touching anything in here: **the Enneagram has a weaker
psychometric evidence base than the Big Five layer sitting next to it, and this
module is built to say so rather than to hide it.** The systematic review of the
literature (Hook et al., 2021, *Journal of Clinical Psychology*, 104 independent
samples) found mixed evidence of reliability and validity. Factor-analytic work
on Enneagram instruments typically recovers *fewer than nine* factors, and the
factors that do emerge look more like Big Five dimensions than like the nine
types; no published work derives the nine types by clustering. Support for the
secondary machinery — wings, and movement between types under stress and
security — is thinner still. In the Norcross, Koocher & Garofalo (2006) Delphi
poll of doctoral-level psychologists, the Enneagram as a personality assessment
scored 4.14 on a five-point discredited scale whose point 4 reads "probably
discredited".

We ship it anyway, and that is a product decision with reasons:

* The two working guides this feature was modelled on (a personal guide and a
  pair guide) use Enneagram language, and the team that asked for them already
  speaks it. A profile that omits the layer reads as a downgrade of the format
  we were asked to reproduce.
* As shared vocabulary for a conversation about how somebody works, a type
  description earns its place even where the measurement does not. "I default to
  scanning for what could go wrong" is a useful thing for a colleague to know
  about you, whether or not the nine-type structure survives a factor analysis.

So the layer exists under four rules, each enforced by the types below rather
than left to the caller's good intentions:

1. **Never reported without a confidence.** :class:`EnneagramRead` is frozen and
   has no construction path that produces a type without one. This is the one
   place this module deliberately diverges from ``disc.py``, whose result
   objects are mutable — here the whole point is an invariant that holds after
   construction.
2. **A narrow margin is an answer.** When the top two types are close, scoring
   returns provenance ``"unresolved"`` — no type, both candidates named, and a
   "we're not sure, tell us" prompt. A false pick that a person then carries
   around in a company-visible guide is much worse than an honest blank.
3. **Self-identification wins.** Somebody who knows their type overrides
   whatever the items inferred, and the read records that it came from them.
4. **Declining is a complete, supported outcome.** :meth:`EnneagramRead.declined`
   is a first-class state, and every downstream renderer must go through
   :func:`guide_section`, which returns ``None`` for it. The profile is valid
   without this layer; the personal guide, pair guide, and team report all have
   to read correctly with it absent.

One more boundary: this layer is **narrative only**. It must not feed the Team
Builder's matching arithmetic — style complementarity there runs off the
DISC-style and Big Five layers. A signal this weakly evidenced can inform a
conversation; it cannot be a selection criterion for who gets put on a project.
And as everywhere in Smart Teams: advisory, never an input to review, promotion,
or compensation. ``ADVISORY_NOTICE`` lives in ``disc.py`` as the single source
of truth for that disclaimer and is imported here rather than restated.

Legal provenance — why we may build this at all
-----------------------------------------------
The framework — nine types, the nine-pointed figure, wings, the three centres —
is not owned by a single party. Gurdjieff brought the symbol west (from 1915) as
a process model rather than a typology; Oscar Ichazo first mapped psychological
meaning onto its nine points at the Arica School (from 1968) and Claudio Naranjo
developed and taught the types from there, after which many authors popularised
them independently.

Specific *instruments and publications* built on the framework are very much
owned. The RHETI is The Enneagram Institute's copyrighted, trademarked product,
and the single-word type names in widest circulation were authored by Riso and
Hudson and are proprietary to EI along with their descriptions — which is why
this module does not use, list, or map onto them. The nine labels in ``TYPES``,
all 27 items, and every sentence of description were authored in-house
and checked against the circulating vendor vocabularies so that none collides.
The collision check is recorded in the docs rather than here, so that this file
never becomes a copy of the list it is avoiding.

Design notes worth knowing before you change anything here
----------------------------------------------------------
* **Structure follows ``disc.py`` and ``motivators.py``.** ``Literal``
  vocabularies, ``item_id``/``prompt`` field names, an ``as_dict()`` for the
  ``st_profiles`` JSON, an ``interview_payload()`` that withholds the scoring
  keys, and a stamped ``ITEM_BANK_VERSION``. When a third scale layer lands,
  lift ``LikertItem`` and the Likert constants into ``instruments/base.py``.
* **All 27 items are forward-keyed, and that is a considered choice.** A reverse
  item on a typology would have to mean "not type 4", which is eight different
  things at once and measures none of them. The cost is acquiescence bias —
  agreeable people agree with all 27 — and scoring pays it off by centring each
  person's answers on their own mean before ranking. The consequence is that the
  output is purely relative: it can order this person's patterns and can never
  say how strongly any one of them is present.
* **The thresholds are the honesty.** ``STRONG_MARGIN``, ``WEAK_MARGIN`` and
  ``WING_MARGIN`` are what stand between this layer and a confidently wrong
  type. Loosening them to get fewer "unresolved" outcomes would be reversing
  the design, not tuning it.
* **No LLM anywhere in this file.** Scoring is pure and deterministic. The LLM
  writes prose from this output and reads the confirmation answer; it never
  picks a type.

Sources, provenance, and the validity criticism in full:
``docs/smart_teams/instruments/motivators_and_enneagram.md``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from rubik_core.brand import PRODUCT_NAME

from .disc import ADVISORY_NOTICE

#: The three centres of the framework — the one piece of grouping we keep.
#: Useful in a team report ("this group is four head types and no gut type"), and
#: honest to state because it is framework-level structure rather than any
#: vendor's content.
Center = Literal["gut", "heart", "head"]

#: Where a read came from. Decides what the guide is allowed to claim.
#:
#: * ``inferred`` — from the item bank, with a margin wide enough to name a type.
#: * ``self_identified`` — the person told us, in the confirmation step or by
#:   correcting us. Beats inference every time.
#: * ``unresolved`` — the items did not separate the top candidates. No type, two
#:   candidates, and a question for the person. A success state, not an error.
#: * ``declined`` — the person was offered the layer and said no. Fully supported.
#: * ``not_taken`` — the interview has not reached this segment yet.
Provenance = Literal[
    "inferred",
    "self_identified",
    "unresolved",
    "declined",
    "not_taken",
]

#: How much weight the guide may put on a named type. ``"none"`` is the extra
#: member this layer needs and ``disc.py`` does not: it means no claim is being
#: made at all, because the person declined or has not answered.
Confidence = Literal["high", "moderate", "low", "none"]

#: Bumped on any change to item text, type assignment, or scoring thresholds.
ITEM_BANK_VERSION = "enneagram-2026-08-1"

CENTER_NAMES: Mapping[Center, str] = {
    "gut": "Gut",
    "heart": "Heart",
    "head": "Head",
}

#: What each centre reacts from. Framework-level, and worth stating in a team
#: report because a group weighted entirely into one centre has a predictable
#: blind spot.
CENTER_SUMMARIES: Mapping[Center, str] = {
    "gut": "Types 8, 9 and 1 — react from the gut; the underlying charge is anger.",
    "heart": "Types 2, 3 and 4 — react from image and feeling; the charge is shame.",
    "head": "Types 5, 6 and 7 — react from thinking; the charge is fear.",
}


# ── The nine types ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class TypeSpec:
    """Everything a guide may say about one type.

    ``label`` is ours on purpose: the widely-quoted single-word type names belong
    to The Enneagram Institute's published materials, so none of them appear
    here.

    Labels also have to stay distinct from the DISC-style names in ``disc.py``
    and the strength themes in ``strengths.py``, because a personal guide prints
    all three layers on one page and a reader has no way to tell which layer a
    repeated name came from. The first cut of this bank did not hold that line:
    types 1, 3 and 9 shipped as The Standard-Setter, The Front-Runner and The
    Steady Hand against DISC styles named Standard Setter, Front Runner and
    Steady Hand — three collisions that read as identical on the page — and they
    are now The Line-Holder, The Scorekeeper and The Peace-Keeper for that
    reason.

    What is actually guaranteed, and by what: nothing inside this file can see
    the other layers, so no claim about cross-layer uniqueness can be enforced
    here. The check lives in
    ``backend/tests/test_smart_teams_motivators_and_enneagram.py``, which reads
    ``TYPES``, ``disc.WORK_STYLES`` and ``strengths.THEMES`` and fails if any
    Enneagram label matches a name in either of the other two once "The",
    hyphens, and case are normalised away — normalised, because Standard-Setter
    against Standard Setter is the collision a reader sees even though the
    strings differ. Renaming a label here is not finished until that test passes
    on the new name.
    """

    number: int
    label: str
    center: Center
    #: What the person is reaching for, underneath the behaviour.
    core_motivation: str
    #: What they are steering away from. The more useful half in practice — it
    #: explains the reaction a colleague finds surprising.
    core_fear: str
    #: How the pattern shows up in ordinary work, when things are fine.
    workplace_signature: str
    #: What it looks like under real pressure — the paragraph a teammate needs on
    #: a bad week.
    under_stress: str
    #: The practical ask. What colleagues can actually do.
    needs_from_colleagues: str
    #: Adjacent type number -> how that flavour changes the picture. Always
    #: rendered as a possibility, never as a second measurement.
    wings: Mapping[int, str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "label": self.label,
            "center": self.center,
            "core_motivation": self.core_motivation,
            "core_fear": self.core_fear,
            "workplace_signature": self.workplace_signature,
            "under_stress": self.under_stress,
            "needs_from_colleagues": self.needs_from_colleagues,
            "wings": dict(self.wings),
        }


TYPES: Mapping[int, TypeSpec] = {
    1: TypeSpec(
        number=1,
        label="The Line-Holder",
        center="gut",
        core_motivation="To get it right, and to be the kind of person who does.",
        core_fear="Being sloppy, careless, or in the wrong.",
        workplace_signature=(
            "Sees the error before the achievement. Holds a line on quality without "
            "being asked to, keeps their own work to a standard nobody imposed, and "
            "quietly cleans up work that arrived below it."
        ),
        under_stress=(
            "The internal critic gets louder and leaks outward: correction turns into "
            "criticism, small imperfections take on weight they do not have, and they "
            "get resentful about carrying a standard they feel nobody else is "
            "carrying — usually without saying so."
        ),
        needs_from_colleagues=(
            "Say explicitly which parts of a deliverable need to be excellent and "
            "which genuinely only need to be done — they cannot tell from the outside "
            "and will default to excellent on all of it. Acknowledge the fix, not "
            "just the ship. When you overrule their standard, give the reason."
        ),
        wings={
            9: (
                "Cooler and more measured about it — the standard is held, but the "
                "correction arrives late and gently, if at all."
            ),
            2: (
                "The standard is applied to how people are treated as much as to the "
                "work, and correction comes wrapped in help."
            ),
        },
    ),
    2: TypeSpec(
        number=2,
        label="The Supporter",
        center="heart",
        core_motivation="To be genuinely needed by the people around them.",
        core_fear="Being unwanted — of no use to anyone who matters to them.",
        workplace_signature=(
            "Reads what people need before they ask. First to notice a new person is "
            "drowning, first to reorganise their own day around somebody else's "
            "deadline, and the informal route by which half the team's real "
            "information travels."
        ),
        under_stress=(
            "Over-gives, then keeps score silently. Their own list slips while they "
            "rescue everyone else's, and the resentment surfaces sideways — as hurt "
            "about a small slight rather than as 'I am carrying too much'."
        ),
        needs_from_colleagues=(
            "Ask what they need and then wait through the deflection. Protect their "
            "focus time for them, because they will not. Name their help out loud, "
            "specifically — vague appreciation reads as not having noticed."
        ),
        wings={
            1: (
                "Help comes with a standard attached: they will support you and tell "
                "you how it should have been done."
            ),
            3: (
                "Help is visible and efficient — they support in ways that are seen, "
                "and they are effective at it."
            ),
        },
    ),
    3: TypeSpec(
        number=3,
        label="The Scorekeeper",
        center="heart",
        core_motivation="To be effective, and to be seen being effective.",
        core_fear="Being worthless without the win — exposed as unimpressive.",
        workplace_signature=(
            "Keeps a running tally of where they stand against what they set out to "
            "do. Reads the room and adjusts how they present. Reliably the person who "
            "gets the thing over the line when the quarter is nearly out."
        ),
        under_stress=(
            "Substitutes motion for progress and image for substance. Works past the "
            "point of usefulness, drops anything that will not show, and gets brittle "
            "about criticism delivered where others can hear it."
        ),
        needs_from_colleagues=(
            "Give feedback privately and specifically — public correction lands as a "
            "verdict on them rather than on the work. Tell them what winning looks "
            "like here, in plain terms, or they will invent a scoreboard. Ask what "
            "they dropped to hit the date."
        ),
        wings={
            2: (
                "Achievement pointed at people — a warmer, more relational version "
                "that wins by making others successful."
            ),
            4: (
                "Wants the win to be distinctly theirs, and is more troubled than "
                "they let on by work that feels generic."
            ),
        },
    ),
    4: TypeSpec(
        number=4,
        label="The Original",
        center="heart",
        core_motivation="To do work that is unmistakably theirs and means something.",
        core_fear="Being ordinary, interchangeable, or unseen.",
        workplace_signature=(
            "Feels the gap between what we produced and what it could have been, and "
            "says so. Brings the angle nobody else in the room had. Uninterested in "
            "competent-but-generic work, and unable to fake enthusiasm for it."
        ),
        under_stress=(
            "Withdraws into comparison — everyone else's work looks solid and theirs "
            "looks thin. Mood becomes information they act on. Can turn a scoping "
            "conversation into a referendum on whether the work is worth doing."
        ),
        needs_from_colleagues=(
            "Give them the piece where taste and judgement actually matter, and say "
            "why it needs their particular read. Take the objection seriously before "
            "answering it. Don't try to jolly them out of a mood; ask what the mood "
            "is telling them about the work."
        ),
        wings={
            3: (
                "Wants the distinctive work to also land publicly — more driven, more "
                "productive, more conscious of audience."
            ),
            5: (
                "Turns inward and technical instead of expressive; depth over "
                "visibility, and long stretches of solitary work."
            ),
        },
    ),
    5: TypeSpec(
        number=5,
        label="The Quiet Expert",
        center="head",
        core_motivation=(
            "To be genuinely competent, and to keep enough resources — time, "
            "attention, energy — to stay that way."
        ),
        core_fear="Being exposed as out of their depth, or drained dry by demands.",
        workplace_signature=(
            "Wants to understand something properly before saying anything about it "
            "in public. Goes quiet in meetings and then produces the most complete "
            "answer in the room. Prefers a problem handed over and then left alone."
        ),
        under_stress=(
            "Retreats further into the work and further from the people. Withholds a "
            "half-formed opinion that the team needed a week ago. Meeting-heavy days "
            "leave them flat regardless of how the meetings went."
        ),
        needs_from_colleagues=(
            "Send the question in advance so they can arrive prepared instead of "
            "improvising in front of an audience. Ask for the partial answer "
            "explicitly and say partial is fine. Guard their uninterrupted blocks — "
            "for them that is not a preference, it is the working condition."
        ),
        wings={
            4: (
                "The expertise has an aesthetic to it; more personal investment in "
                "the subject, more visible reaction when it is treated carelessly."
            ),
            6: (
                "More practical and more alert to risk — the analysis is aimed at "
                "what could go wrong and who is relying on it."
            ),
        },
    ),
    6: TypeSpec(
        number=6,
        label="The Risk-Scanner",
        center="head",
        core_motivation="To be on solid ground, with support they can count on.",
        core_fear="Being unsupported when it matters — exposed with no backup.",
        workplace_signature=(
            "Runs the failure cases before committing. Wants to know who is "
            "accountable before relying on something. Trusts a plan *more* after "
            "somebody has argued against it in front of them, which is why they argue "
            "against yours."
        ),
        under_stress=(
            "Doubt loops. Re-litigates a settled decision, seeks reassurance from "
            "several people and believes none of it, or swings the other way and "
            "commits hard to a course of action to escape the ambiguity."
        ),
        needs_from_colleagues=(
            "Answer the 'what if this goes wrong' question rather than waving it away "
            "— it is due diligence, not obstruction. Be explicit that the decision is "
            "made and that you are behind them. Follow through on small commitments; "
            "that is the entire trust ledger."
        ),
        wings={
            5: (
                "The scanning is analytical and private — they will build the "
                "objection out of evidence before raising it."
            ),
            7: (
                "Lighter and quicker; the worry gets outrun by activity and humour "
                "rather than examined."
            ),
        },
    ),
    7: TypeSpec(
        number=7,
        label="The Possibility-Seeker",
        center="head",
        core_motivation="To keep the work interesting and the options open.",
        core_fear=(
            "Being trapped — in a narrow assignment, in a dull stretch, in pain there "
            "is no way out of."
        ),
        workplace_signature=(
            "Generates options faster than the room can evaluate them. Excellent at "
            "the ambiguous, unstructured start of a thing. Energy drops sharply once "
            "the interesting part is solved and the finishing begins."
        ),
        under_stress=(
            "Scatters. Starts a second and third thing rather than close the first, "
            "reframes an uncomfortable situation as fine, and lets the boring "
            "commitment quietly lapse."
        ),
        needs_from_colleagues=(
            "Pair them with a finisher and make that explicit rather than resentful. "
            "Hold them to the last 20% by name and date. Do not fill their calendar "
            "wall to wall; take one of the ideas seriously so the rest can be let go."
        ),
        wings={
            6: (
                "The enthusiasm comes with a check on it — more loyal to commitments, "
                "more aware of what could go wrong."
            ),
            8: (
                "Faster and more forceful — the ideas arrive as decisions, and they "
                "will push them through."
            ),
        },
    ),
    8: TypeSpec(
        number=8,
        label="The Straight-Shooter",
        center="gut",
        core_motivation=(
            "To stay in control of their own situation, and to shield the people they "
            "have taken responsibility for."
        ),
        core_fear="Being controlled, cornered, or at somebody else's mercy.",
        workplace_signature=(
            "Says the thing the room is avoiding. Would rather own a decision than be "
            "subject to one. Steps in when they see somebody being run over, and "
            "escalates without hesitation when a situation has stalled."
        ),
        under_stress=(
            "Volume and certainty go up while listening goes down. Overruns the "
            "quieter people, treats a question as a challenge, and grinds through the "
            "signals that would tell most people to stop for a day."
        ),
        needs_from_colleagues=(
            "Be direct; hedged delivery reads as evasion and costs you credibility. "
            "Disagree to their face and hold your ground — they respect that far more "
            "than agreement. Tell them plainly when their force flattened a room; "
            "they usually did not intend it and cannot see it from inside."
        ),
        wings={
            7: (
                "Bigger, faster, more appetite for the next thing — force plus "
                "momentum."
            ),
            9: (
                "Steadier and slower to fire; the strength is present without the "
                "edge until something actually needs confronting."
            ),
        },
    ),
    9: TypeSpec(
        number=9,
        label="The Peace-Keeper",
        center="gut",
        core_motivation=(
            "To keep things settled, and to stay connected to the people around them."
        ),
        core_fear=(
            "Being pulled into conflict that fractures the group — or being "
            "steamrollered inside it."
        ),
        workplace_signature=(
            "Sees the merit in every position, which makes them the person a divided "
            "room will actually listen to. Absorbs friction rather than passing it "
            "on. Consistent, unhurried, and hard to rattle."
        ),
        under_stress=(
            "Goes along with it. Their own view disappears from the conversation, the "
            "hard message gets deferred, and small annoyances get swallowed until "
            "they come out all at once, months late and out of proportion."
        ),
        needs_from_colleagues=(
            "Ask for their actual position and wait — the first answer is often a "
            "summary of everyone else's. Give the disagreement a low-stakes container "
            "so raising it is not a rupture. Make deadlines concrete; open-ended "
            "priorities are where their work goes to sit."
        ),
        wings={
            8: (
                "The calm has a floor under it — they will plant themselves and not "
                "move when something crosses a line."
            ),
            1: (
                "Steadiness organised around a standard; more structured, more "
                "particular, quietly firmer about how things should be done."
            ),
        },
    ),
}

#: Ascending type order. Also the deterministic tiebreak for ranking.
TYPE_ORDER: tuple[int, ...] = tuple(sorted(TYPES))


def adjacent(number: int) -> tuple[int, int]:
    """The two neighbours of a type on the nine-pointed figure, ascending."""
    if number not in TYPES:
        raise KeyError(number)
    low = 9 if number == 1 else number - 1
    high = 1 if number == 9 else number + 1
    return (low, high)


def spec_for(number: int) -> TypeSpec:
    """The prose block for one type."""
    return TYPES[number]


def types_in_center(center: Center) -> tuple[int, ...]:
    """The three type numbers in one centre, ascending."""
    return tuple(n for n in TYPE_ORDER if TYPES[n].center == center)


# ── The item bank ─────────────────────────────────────────────────────────────
LIKERT_MIN = 1
LIKERT_MAX = 5

#: Same five-point agreement scale as the rest of the interview. Restated rather
#: than imported from ``motivators``: an Enneagram module depending on a
#: motivators module would be a coupling that exists only because of authoring
#: order. Both move to ``instruments/base.py`` when that lands.
LIKERT_ANCHORS: Mapping[int, str] = {
    1: "Not like me at all",
    2: "A little like me",
    3: "Somewhat like me",
    4: "Mostly like me",
    5: "Exactly like me",
}


@dataclass(frozen=True)
class LikertItem:
    """One agreement statement pointing at one type.

    There is no ``keying`` field, unlike the sibling modules. A reverse item on a
    typology would have to mean "not type 4", which is eight different things at
    once and measures none of them. See the module docstring on how scoring pays
    off the resulting acquiescence bias.
    """

    item_id: str
    type_number: int
    prompt: str


#: 27 items, three per type. Ids are ``enn_t<type>_<nn>`` and permanent; the
#: number in the id is an authoring label, not a position. The tuple runs three
#: passes over the nine types in three different orders, so no two consecutive
#: items share a type and the sequence does not telegraph a repeating 1-9 cycle
#: that a respondent could pattern-match. Scoring is order-independent, so
#: re-sorting breaks nothing visible — and quietly throws away the only thing
#: that makes a future run-length or position-bias check mean anything, since
#: both read the order the respondent actually saw. This bank has none yet;
#: ``disc.check_response_quality`` is the model. Do not re-sort this tuple.
LIKERT_ITEMS: tuple[LikertItem, ...] = (
    # ── pass 1: 1-9 in order ──
    LikertItem(
        "enn_t1_01",
        1,
        "I notice the mistake in a document before I notice what is good about it.",
    ),
    LikertItem(
        "enn_t2_01",
        2,
        "I usually know what the people around me need before they ask.",
    ),
    LikertItem(
        "enn_t3_01",
        3,
        "I keep a running sense of where I stand against what I set out to do.",
    ),
    LikertItem(
        "enn_t4_01",
        4,
        "I would rather do work that is distinctly mine than work that is merely competent.",
    ),
    LikertItem(
        "enn_t5_01",
        5,
        "I want to understand something thoroughly before I say anything about it in public.",
    ),
    LikertItem(
        "enn_t6_01",
        6,
        "I run through what could go wrong before I commit to a plan.",
    ),
    LikertItem(
        "enn_t7_01",
        7,
        "I lose energy on a project once the interesting part is solved.",
    ),
    LikertItem(
        "enn_t8_01",
        8,
        "I will say the thing in a meeting that everybody else is avoiding.",
    ),
    LikertItem(
        "enn_t9_01",
        9,
        "I can see the merit in every side of an argument, which makes it hard to pick one.",
    ),
    # ── pass 2: 5, 9, 4, 8, 3, 7, 2, 6, 1 ──
    LikertItem(
        "enn_t5_02",
        5,
        "A day of back-to-back meetings leaves me drained no matter how well they went.",
    ),
    LikertItem(
        "enn_t9_02",
        9,
        "I would usually rather absorb a small annoyance than raise it.",
    ),
    LikertItem(
        "enn_t4_02",
        4,
        "I feel the gap between what we produced and what it could have been.",
    ),
    LikertItem(
        "enn_t8_02",
        8,
        "I would rather be in charge of a decision than be subject to one.",
    ),
    LikertItem(
        "enn_t3_02",
        3,
        "I adjust how I come across depending on who is in the room.",
    ),
    LikertItem(
        "enn_t7_02",
        7,
        "I keep more options open than I strictly need.",
    ),
    LikertItem(
        "enn_t2_02",
        2,
        "It is easier for me to name somebody else's needs than my own.",
    ),
    LikertItem(
        "enn_t6_02",
        6,
        "I want to know who is accountable before I rely on something.",
    ),
    LikertItem(
        "enn_t1_02",
        1,
        "I hold myself to a standard I would never impose on anybody else.",
    ),
    # ── pass 3: 6, 2, 7, 3, 8, 4, 9, 5, 1 ──
    LikertItem(
        "enn_t6_03",
        6,
        "I trust a plan more after somebody has argued against it in front of me.",
    ),
    LikertItem(
        "enn_t2_03",
        2,
        "I want to be the person my colleagues come to when they are stuck.",
    ),
    LikertItem(
        "enn_t7_03",
        7,
        "Being boxed into one narrow assignment is worse for me than being busy.",
    ),
    LikertItem(
        "enn_t3_03",
        3,
        "Falling short in public is much worse to me than falling short privately.",
    ),
    LikertItem(
        "enn_t8_03",
        8,
        "I step in when I see somebody being run over.",
    ),
    LikertItem(
        "enn_t4_03",
        4,
        "I am uncomfortable being treated as interchangeable with anybody else.",
    ),
    LikertItem(
        "enn_t9_03",
        9,
        "I work best when the room is settled and nobody is at odds.",
    ),
    LikertItem(
        "enn_t5_03",
        5,
        "I would rather be handed a hard problem and then left alone with it.",
    ),
    LikertItem(
        "enn_t1_03",
        1,
        "It bothers me when work goes out below the standard I know it should have met.",
    ),
)

_ITEMS_BY_ID: Mapping[str, LikertItem] = {item.item_id: item for item in LIKERT_ITEMS}


def items_for(number: int) -> tuple[LikertItem, ...]:
    """The items pointing at one type, in presentation order."""
    if number not in TYPES:
        raise KeyError(number)
    return tuple(item for item in LIKERT_ITEMS if item.type_number == number)


def interview_payload() -> dict[str, Any]:
    """Exactly what the interview page needs to render this layer — and nothing
    more. The type each item points at is deliberately withheld: a respondent
    who can see that these three statements are "the type 6 questions" is
    answering a different instrument, and on a typology that is the difference
    between self-report and self-casting.
    """
    return {
        "layer": "enneagram",
        "scale": [
            {"value": value, "label": label} for value, label in LIKERT_ANCHORS.items()
        ],
        "items": [
            {"item_id": item.item_id, "prompt": item.prompt} for item in LIKERT_ITEMS
        ],
    }


# ── Scoring ───────────────────────────────────────────────────────────────────
# Margins are measured on the 0-100 relative-emphasis scale produced by
# :func:`score_enneagram`. Twelve and a half points there is one full scale point
# on one item of three — the smallest difference a three-item subscale can even
# express. HIGH therefore needs a gap you could not get from a single stray
# answer; anything under WEAK_MARGIN is noise wearing a type's name, and resolves
# to "we don't know".
STRONG_MARGIN = 12.5
WEAK_MARGIN = 6.0

#: Minimum spread between the highest and lowest type emphasis for the bank to
#: have differentiated at all. Below this the nine types are effectively tied,
#: and "the top two" are whichever two the tie-break happened to surface.
#:
#: This exists because the margin check alone could not see the failure. A
#: straight-lined bank centres every type on the person's own mean, so all nine
#: land at exactly 50.0, the margin is 0.0, and the narrow-margin branch below
#: dutifully reported the first two types in declaration order as the two the
#: answers "landed on" — types 1 and 2, for every straight-liner in the company,
#: with their full portraits offered to pick from. Picking one then stored a
#: self-identified type at high confidence. A coin flip became a published fact
#: about a person, so the differentiation check has to come first.
FLAT_SPREAD = 3.0

#: How many types may sit within ``WEAK_MARGIN`` of the leader before the result
#: stops being a two-way question. Two contenders is a real question worth
#: asking; three or more is an undifferentiated bank, and naming any two of them
#: would be picking by declaration order again.
MAX_CONTENDERS = 2

#: A wing is only named when one neighbour clearly leads the other. Deliberately
#: stricter than the type threshold: wings have the least empirical support of
#: anything in the model, so the bar for asserting one is higher, and "no clear
#: wing" is a perfectly good thing for a guide to say.
WING_MARGIN = 8.0

#: Below these, scoring refuses to name a type at all: two of the three items per
#: type, and two thirds of the bank overall.
MIN_ITEMS_PER_TYPE = 2
MIN_ITEMS_ANSWERED = 18


@dataclass(frozen=True)
class TypeScore:
    """One type's relative emphasis for one person.

    ``emphasis`` is centred within the person and rescaled to 0-100, so it says
    only "this pattern more than that one, for this person". It is not a
    percentile, there is no norm group behind it, and it must never be rendered
    as a level ("strongly a 5") — only as an ordering.
    """

    type_number: int
    emphasis: float
    answered: int

    @property
    def label(self) -> str:
        return TYPES[self.type_number].label

    def as_dict(self) -> dict[str, Any]:
        return {
            "type_number": self.type_number,
            "label": self.label,
            "emphasis": self.emphasis,
            "answered": self.answered,
        }


@dataclass(frozen=True)
class EnneagramRead:
    """The Enneagram layer of a profile — or the honest absence of it.

    Construct through :func:`score_enneagram`, :meth:`declined`,
    :meth:`not_taken`, or :meth:`self_identified`. There is deliberately no path
    to a named type without a :data:`Confidence` attached, and the class is
    frozen so that invariant survives construction — see rule 1 in the module
    docstring for why this one result object diverges from ``disc.py``'s mutable
    ones.
    """

    provenance: Provenance
    confidence: Confidence
    #: The named type, or ``None`` for unresolved / declined / not taken.
    type_number: int | None = None
    #: Named only when one neighbour clearly leads. Always tentative — wings have
    #: the thinnest evidence of anything in the model.
    wing: int | None = None
    #: All nine, highest emphasis first. Empty when nothing was scored.
    ranked: tuple[TypeScore, ...] = ()
    #: The two types in play when we could not choose. Also populated on an
    #: ``"inferred"`` read, so the confirmation step can show the runner-up.
    candidates: tuple[int, ...] = ()
    #: Gap between first and second, on the 0-100 scale.
    margin: float | None = None
    answered: int = 0
    item_bank_version: str = ITEM_BANK_VERSION
    #: One sentence explaining this outcome, safe to show the person.
    note: str = ""

    @classmethod
    def declined(cls) -> EnneagramRead:
        """The person opted out. A complete profile, minus this layer."""
        return cls(
            provenance="declined",
            confidence="none",
            note="This person chose not to include an Enneagram type.",
        )

    @classmethod
    def not_taken(cls) -> EnneagramRead:
        """The interview has not reached this segment."""
        return cls(
            provenance="not_taken",
            confidence="none",
            note="This section of the interview is not finished yet.",
        )

    @classmethod
    def self_identified(cls, number: int, wing: int | None = None) -> EnneagramRead:
        """A person stating their own type, with no item data behind it.

        ``"high"`` here means "they told us", which is the strongest basis this
        layer can have — :attr:`provenance` is what distinguishes it from a
        confident inference, so downstream copy can say "self-identified".
        """
        return cls(
            provenance="self_identified",
            confidence="high",
            type_number=_validated_type(number),
            wing=_validated_wing(number, wing),
            note="Self-identified.",
        )

    def with_self_identification(
        self, number: int, wing: int | None = None
    ) -> EnneagramRead:
        """Replace whatever we inferred with what the person says they are.

        Keeps the scored ranking for the record — a support admin debugging "why
        did Cubie guess a 3" needs it — but the type, wing, provenance, and
        confidence all come from the person now.
        """
        return EnneagramRead(
            provenance="self_identified",
            confidence="high",
            type_number=_validated_type(number),
            wing=_validated_wing(number, wing),
            ranked=self.ranked,
            candidates=self.candidates,
            margin=self.margin,
            answered=self.answered,
            item_bank_version=self.item_bank_version,
            note=f"Self-identified, replacing {PRODUCT_NAME}'s read of the questionnaire.",
        )

    def with_decline(self) -> EnneagramRead:
        """The person saw our read and declined the layer. Scores discarded.

        The ranking is dropped rather than retained, because keeping a rejected
        inference on file invites some future feature to use it.
        """
        return EnneagramRead.declined()

    @property
    def is_reportable(self) -> bool:
        """True only when a type may appear in front of a reader.

        Every renderer should test this — or better, go through
        :func:`guide_section` — rather than testing ``type_number is not None``,
        so that a future provenance value cannot silently start publishing.
        """
        return self.type_number is not None and self.provenance in (
            "inferred",
            "self_identified",
        )

    @property
    def spec(self) -> TypeSpec | None:
        return TYPES[self.type_number] if self.type_number in TYPES else None

    @property
    def label(self) -> str | None:
        spec = self.spec
        return spec.label if spec else None

    @property
    def center(self) -> Center | None:
        spec = self.spec
        return spec.center if spec and self.is_reportable else None

    @property
    def type_display(self) -> str | None:
        """``"Type 6w5 — The Risk-Scanner"``, or ``None`` if nothing to show."""
        spec = self.spec
        if spec is None or not self.is_reportable:
            return None
        stem = f"Type {spec.number}"
        if self.wing is not None:
            stem = f"{stem}w{self.wing}"
        return f"{stem} — {spec.label}"

    def headline(self) -> str:
        """One sentence, honest about its own footing, safe to render anywhere."""
        if self.provenance == "declined":
            return "No Enneagram type on this profile — the person opted out."
        if self.provenance == "not_taken":
            return "Enneagram section not completed."
        if self.provenance == "unresolved":
            names = " or ".join(TYPES[n].label for n in self.candidates)
            if not names:
                return "Enneagram inconclusive — not enough answers to suggest a type."
            return (
                f"Enneagram inconclusive — the answers point at {names} about "
                "equally, so no type is claimed."
            )
        display = self.type_display or "Enneagram"
        if self.provenance == "self_identified":
            return f"{display} (self-identified)."
        hedge = (
            "a clear read of the questionnaire"
            if self.confidence == "high"
            else "a tentative read of the questionnaire — worth confirming"
        )
        return f"{display}, from {hedge}."

    def as_dict(self) -> dict[str, Any]:
        return {
            "layer": "enneagram",
            "item_bank_version": self.item_bank_version,
            "provenance": self.provenance,
            "confidence": self.confidence,
            "type_number": self.type_number,
            "wing": self.wing,
            "type_display": self.type_display,
            "center": self.center,
            "candidates": list(self.candidates),
            "margin": self.margin,
            "answered": self.answered,
            "is_reportable": self.is_reportable,
            "scores": [entry.as_dict() for entry in self.ranked],
            "headline": self.headline(),
            "note": self.note,
            "advisory_notice": ADVISORY_NOTICE,
        }


def _validated_type(number: int) -> int:
    if number not in TYPES:
        raise ValueError(f"enneagram: {number!r} is not a type between 1 and 9")
    return number


def _validated_wing(number: int, wing: int | None) -> int | None:
    if wing is None:
        return None
    allowed = adjacent(_validated_type(number))
    if wing not in allowed:
        raise ValueError(
            f"enneagram: {wing} is not adjacent to type {number} (expected "
            f"{allowed[0]} or {allowed[1]})"
        )
    return wing


def _validated(responses: Mapping[str, int]) -> dict[str, int]:
    """Reject unknown ids and out-of-scale values loudly.

    Same call ``disc.py`` and ``motivators.py`` make: a typo'd id that silently
    scored as "unanswered" would quietly shift a type, and here it could shift
    which type a person is told they are.

    ``big_five.py`` does not agree — it logs unknown ids and carries on. That is
    a real inconsistency in this package awaiting reconciliation, so do not
    "align" this function down to it on the assumption the looser behaviour was
    the considered one.
    """
    clean: dict[str, int] = {}
    for item_id, value in responses.items():
        if item_id not in _ITEMS_BY_ID:
            raise ValueError(f"unknown enneagram item id: {item_id!r}")
        if value is None:
            continue
        if value not in LIKERT_ANCHORS:
            raise ValueError(f"{item_id}: response {value!r} outside the 1-5 scale")
        clean[item_id] = int(value)
    return clean


def score_enneagram(responses: Mapping[str, int]) -> EnneagramRead:
    """The one entry point: answers in, a type / wing / confidence out.

    ``responses`` maps :class:`LikertItem` ids to 1-5. Unknown ids and
    out-of-range values raise. Pure and deterministic, tiebreak included.

    Two design points worth keeping when this is edited:

    * **Answers are centred on the person's own mean** before types are
      compared. All 27 items are forward-keyed, so without centring the whole
      ranking would partly measure willingness to agree. The cost is that the
      output is purely relative — it can order this person's patterns and can
      never say how strongly any of them is present.
    * **A narrow margin returns no type.** Provenance ``"unresolved"`` with both
      candidates named is the intended outcome, not a failure to handle. Given
      how weak the underlying evidence base is, a coin-flip presented as a
      finding is the one result this module must never produce.
    """
    clean = _validated(responses)

    per_type: dict[int, list[int]] = {number: [] for number in TYPE_ORDER}
    for item in LIKERT_ITEMS:
        response = clean.get(item.item_id)
        if response is None:
            continue
        per_type[item.type_number].append(response)

    values = [v for group in per_type.values() for v in group]
    answered = len(values)
    if answered < MIN_ITEMS_ANSWERED or any(
        len(per_type[number]) < MIN_ITEMS_PER_TYPE for number in TYPE_ORDER
    ):
        return EnneagramRead(
            provenance="unresolved",
            confidence="low",
            answered=answered,
            note=(
                "Not enough answers in this section to suggest a type. You can "
                "finish it later, tell us your type if you already know it, or skip "
                "the Enneagram entirely — your profile works without it."
            ),
        )

    person_mean = sum(values) / answered
    span = float(LIKERT_MAX - LIKERT_MIN)
    ranked = tuple(
        sorted(
            (
                TypeScore(
                    type_number=number,
                    # Centre on the person's own mean, then rescale so a
                    # one-scale-point lead reads as 12.5 points. Clamped because
                    # an extreme responder can otherwise run off the end.
                    emphasis=round(
                        min(
                            100.0,
                            max(
                                0.0,
                                50.0
                                + (
                                    sum(per_type[number]) / len(per_type[number])
                                    - person_mean
                                )
                                / span
                                * 50.0,
                            ),
                        ),
                        1,
                    ),
                    answered=len(per_type[number]),
                )
                for number in TYPE_ORDER
            ),
            key=lambda entry: (-entry.emphasis, entry.type_number),
        )
    )

    first, second = ranked[0], ranked[1]
    margin = round(first.emphasis - second.emphasis, 1)
    candidates = (first.type_number, second.type_number)

    # Did the answers differentiate at all? This has to be asked before the
    # margin is interpreted. Emphasis is centred on the person's own mean, so a
    # respondent who gave the same answer to all 27 items produces nine types at
    # exactly 50.0 — a margin of 0.0 that looks like "two close contenders" but
    # is really "no signal whatsoever". Reporting the top two there names
    # TYPE_ORDER[:2] and nothing else, identically for every straight-liner.
    spread = round(ranked[0].emphasis - ranked[-1].emphasis, 1)
    contenders = tuple(
        entry.type_number
        for entry in ranked
        if first.emphasis - entry.emphasis < WEAK_MARGIN
    )
    if spread < FLAT_SPREAD or len(contenders) > MAX_CONTENDERS:
        return EnneagramRead(
            provenance="unresolved",
            confidence="low",
            ranked=ranked,
            # Deliberately empty. Naming any subset here would be naming the
            # tie-break order. The confirmation step reads an empty candidate
            # tuple as "offer the whole list", which is the honest question.
            candidates=(),
            margin=margin,
            answered=answered,
            note=(
                "These answers did not separate the nine patterns — they came "
                f"out close to level, so {PRODUCT_NAME} has nothing to suggest and will "
                "not guess. Tell us your type if you know it, take this section "
                "again if you rushed it, or leave the Enneagram off your "
                "profile: everything else still works without it."
            ),
        )

    if margin < WEAK_MARGIN:
        return EnneagramRead(
            provenance="unresolved",
            confidence="low",
            ranked=ranked,
            candidates=candidates,
            margin=margin,
            answered=answered,
            note=(
                f"Your answers land on {TYPES[first.type_number].label} and "
                f"{TYPES[second.type_number].label} about equally, so {PRODUCT_NAME} is not "
                "going to pick one for you. Have a look at both and tell us which "
                "fits — or leave the Enneagram off your profile."
            ),
        )

    confidence: Confidence = (
        "high"
        if margin >= STRONG_MARGIN and answered == len(LIKERT_ITEMS)
        else "moderate"
    )
    return EnneagramRead(
        provenance="inferred",
        confidence=confidence,
        type_number=first.type_number,
        wing=_infer_wing(first.type_number, ranked),
        ranked=ranked,
        candidates=candidates,
        margin=margin,
        answered=answered,
        note=(
            "Read from the questionnaire. Confirm it, correct it, or leave it off — "
            f"whatever you say overrides what {PRODUCT_NAME} guessed."
        ),
    )


def _infer_wing(number: int, ranked: tuple[TypeScore, ...]) -> int | None:
    """The leading neighbour, or ``None`` when the two are too close to call."""
    scores = {entry.type_number: entry.emphasis for entry in ranked}
    low, high = adjacent(number)
    if abs(scores[low] - scores[high]) < WING_MARGIN:
        return None
    return low if scores[low] > scores[high] else high


# ── Conversational confirmation ───────────────────────────────────────────────
# The person always gets the last word on this layer. Two candidates in plain
# language, four ways to answer — including "neither" and "skip" — and no default
# that quietly accepts our guess.

#: The four responses the confirmation step must offer. Dropping the last two
#: would turn an optional layer into a mandatory one.
#:
#: The first two are positional and symmetric on purpose. They read "the first"
#: and "the second" because :func:`confirmation_step` presents two descriptions
#: in :attr:`EnneagramRead.candidates` order and marks neither as ours — the
#: earlier pair, "That's me" and "The other one fits better", quietly did the
#: opposite: "that's me" only has a referent if the person has already been told
#: which one we picked, and "the other one" makes the second candidate the
#: alternative to our answer rather than an equal option. The answer maps back to
#: a type by position, so the UI must render the candidates in the order it
#: received them.
FIRST_OPTION = "The first one is more like me"
SECOND_OPTION = "The second one is more like me"
OTHER_OPTION = "Neither — let me pick from the nine"

#: The same escape hatch, worded for the case where no candidates were offered
#: at all. "Neither" needs two things to be neither of; on an undifferentiated
#: bank there are none, because naming any two would be naming tie-break order.
PICK_FROM_ALL_OPTION = "Let me pick from the nine"
DECLINE_OPTION = "Skip the Enneagram on my profile"

#: Rendered under the options. States plainly that skipping costs nothing.
CONFIRMATION_FOOTNOTE = (
    "The Enneagram is the softest-evidence part of this profile — it is a "
    "vocabulary for talking about how you work, not a measurement. Skipping it "
    "leaves everything else in your guide intact."
)


@dataclass(frozen=True)
class CandidateSummary:
    """One candidate type, described without jargon or type numbers up front."""

    type_number: int
    label: str
    plain_language: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "type_number": self.type_number,
            "label": self.label,
            "plain_language": self.plain_language,
        }


@dataclass(frozen=True)
class ConfirmationStep:
    """What to put in front of the person before this layer is written down."""

    question: str
    candidates: tuple[CandidateSummary, ...]
    options: tuple[str, ...]
    footnote: str = CONFIRMATION_FOOTNOTE

    def as_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "candidates": [c.as_dict() for c in self.candidates],
            "options": list(self.options),
            "footnote": self.footnote,
        }


def _plain_language(spec: TypeSpec) -> str:
    """A candidate description in the person's terms, not the framework's."""
    return (
        f"{spec.core_motivation} What you steer away from: "
        f"{spec.core_fear[0].lower()}{spec.core_fear[1:]} At work: "
        f"{spec.workplace_signature}"
    )


def confirmation_step(read: EnneagramRead) -> ConfirmationStep | None:
    """The confirm / correct / decline step for a scored read.

    Returns ``None`` when there is nothing to confirm — already self-identified,
    already declined, not taken, or too few answers to have produced candidates.
    The candidates are described in plain language and nothing labels either as
    Cubie's pick, so the person chooses between two descriptions rather than
    being asked to agree with us. That holds only because the options are
    positional and symmetric — see :data:`FIRST_OPTION`. On an ``"inferred"``
    read the candidate order *is* our ranking, so any option string that reads as
    "yes, your first one" hands the person our answer and asks them to ratify it,
    which is the opposite of what this step is for.

    A ``None`` return on an ``"unresolved"`` read (the not-enough-answers case)
    is **not** the end of the layer: the interview still has to offer "tell us
    your type" and "skip the Enneagram", and :attr:`EnneagramRead.note` is the
    copy for it. Falling through silently would leave the person with neither a
    type nor a choice about it.
    """
    if read.provenance in ("self_identified", "declined", "not_taken"):
        return None
    if not read.candidates:
        return None

    candidates = tuple(
        CandidateSummary(
            type_number=number,
            label=TYPES[number].label,
            plain_language=_plain_language(TYPES[number]),
        )
        for number in read.candidates
    )
    if read.provenance == "unresolved":
        question = (
            "Your answers point at these two about equally, so we would rather ask "
            "than guess. Does either of these sound like you?"
        )
    else:
        question = (
            "Here are the two closest matches from your answers. Which one sounds "
            "more like you?"
        )
    # With no candidates there is no "first" or "second" to choose between, and
    # OTHER_OPTION's "Neither" has nothing to refer back to. Offer the two that
    # still mean something rather than four options, two of which point at
    # types that were never shown.
    options = (
        (FIRST_OPTION, SECOND_OPTION, OTHER_OPTION, DECLINE_OPTION)
        if candidates
        else (PICK_FROM_ALL_OPTION, DECLINE_OPTION)
    )
    return ConfirmationStep(
        question=question,
        candidates=candidates,
        options=options,
    )


def guide_section(read: EnneagramRead) -> str | None:
    """The Enneagram block for a personal guide, or ``None`` to omit it.

    The single accessor every document generator should use. Declined,
    unresolved, and unfinished all return ``None``, which is what makes "a
    profile with no Enneagram" a supported shape everywhere downstream instead of
    a special case each renderer has to remember.
    """
    if not read.is_reportable:
        return None
    spec = read.spec
    if spec is None:
        return None

    lines = [read.headline(), "", spec.workplace_signature]
    if read.wing is not None:
        lines += ["", f"With a {read.wing} wing: {spec.wings[read.wing]}"]
    lines += [
        "",
        f"Under pressure: {spec.under_stress}",
        "",
        f"What helps: {spec.needs_from_colleagues}",
    ]
    if read.confidence == "moderate":
        lines += [
            "",
            "Held loosely: this read came from a short questionnaire and has not "
            "been confirmed. Treat it as a starting point for a conversation.",
        ]
    return "\n".join(lines)

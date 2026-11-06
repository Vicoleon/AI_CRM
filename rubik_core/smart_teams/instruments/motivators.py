"""The motivators layer of the Work Style Interview (ST-1) — six values, ranked.

What this layer measures
------------------------
The DISC-style and Big Five layers describe *how* somebody works: how fast they
decide, how much structure they want, how they behave when a room gets tense.
This layer describes what they are working *for*, which is what actually
predicts the arguments they find persuasive and the trades they make when two
good options collide. "Sarah is direct" tells you how to open a conversation;
"Sarah decides by payoff-per-hour and is unmoved by elegance" tells you which
version of the proposal to bring to Sarah.

Six orientations, in work terms:

* **Practical Return** (Spranger's *economic*) — work earns its keep in revenue,
  cost avoided, hours saved, or risk retired. Options get judged on payoff per
  unit of effort.
* **Understanding** (*theoretical*) — the point is to know how the thing
  actually works. A fix that works without an explanation is an unfinished fix.
* **Human Impact** (*social*) — the part of a win worth having is the person it
  helped. Effort spent on people is not overhead.
* **Standing and Autonomy** (*political*) — room to run, influence over
  direction, and being known for the work. Renamed because "political" reads as
  office politics in a workplace document, which is not what the axis is.
* **Guiding Framework** (*religious*) — a settled standard to hold work against,
  and consistency between what we say and what we do. Spranger's most
  time-bound label; the axis in its secular, work-relevant form is what
  commercial instruments call "traditional" or "regulatory".
* **Form and Craft** (*aesthetic*) — how the work reads, looks, and feels.
  Correct-but-ugly is not done.

**Ranking is the output, not level.** Everybody scores something on all six, so
"Sam is a 62 on Understanding" means nothing without a comparison group we do
not have. What carries information is the *order*: which two orientations win
when they conflict, and which one loses. That is why the score field here is
called ``emphasis`` and not ``intensity`` like its DISC-style counterpart — a
DISC intensity is meant to be comparable across people, and this number is
explicitly not. :class:`MotivatorResult` leads with the top two and the bottom
one because those three positions are the only ones a 30-item bank can support.

Legal provenance — why we may build this at all
-----------------------------------------------
The six-value framework is Eduard Spranger's, from *Lebensformen* (Halle, 1914)
and its English translation *Types of Men* (trans. Pigors, 1928) — long out of
copyright. Everything built on top of that framework since is not ours to use:
the Allport-Vernon *Study of Values* (1931), its Allport-Vernon-Lindzey
revisions and the fourth edition (Kopelman, Rovenpor & Guan, 2003), and TTI's
commercial Workplace Motivators report are proprietary instruments with
proprietary items, scales, and report prose.

So the line is the same one ``disc.py`` holds: we take the model, we author
everything else. The six labels in ``MOTIVATOR_NAMES``, every definition and
high/low read in ``MOTIVATOR_SPECS``, and all 30 items in ``LIKERT_ITEMS`` were
written in-house. No vendor item, scale, norm table, or report sentence
is reproduced or paraphrased anywhere in this module.

Three vocabularies are in play, and this module is deliberately careful about
which is which — "Spranger's six axes" is an easy thing to claim and only partly
true of what is actually in the code:

* **Spranger's own six**, in the Pigors translation, are Theoretical, Economic,
  Aesthetic, Social, Political and Religious. ``SPRANGER_LABELS`` holds exactly
  those and nothing else, for the methodology footnote and the citation. They
  are never user-facing prose.
* **The :data:`Motivator` keys are not all Spranger's.** Four of them —
  ``economic``, ``theoretical``, ``social``, ``aesthetic`` — are his own English
  terms. The other two are ours: ``individualistic`` and ``traditional`` stand in
  for his *political* and *religious*, following the modern workplace
  relabelling that every later instrument on this framework arrived at for the
  same reason we did. The sources recorded in the doc have TTI Success Insights
  publishing those two axes as Individualistic and Regulatory / Traditional, so
  the two words are not novel to us either. Both are single common English words
  naming an axis rather than protectable content, and no vendor item, scale, norm
  table, or report sentence is reproduced or paraphrased anywhere in this module
  — but calling either of them Spranger's would be false, so this module does
  not, and neither does the doc.
* **The display labels in ``MOTIVATOR_NAMES`` are ours.** None of the six
  collides with the vendor axis names recorded in the doc or with TTI's separate
  12 Driving Forces names, and the display layer is the one that may be reworded
  freely.

Honesty about what this measures
--------------------------------
This framework has a much longer history than it has psychometric support. When
Hanel, Litzellachner & Maio (2018, *Frontiers in Psychology*) compared human
value models empirically, the Study of Values scales reached a maximum alpha of
0.26 and were dismissed from all further analyses for low reliability. Our bank
is shorter than theirs, and a 30-item Likert bank cannot do better by being
authored more carefully.

So this layer is a structured conversation opener, not a measurement. The
confirmation probe (:func:`probe_for`) runs in every interview because a value
ranking inferred from 30 agreement answers is thin, and
:attr:`MotivatorResult.probe_required` marks the cases where the item scores
alone must not reach a published guide at all. Critically, the probe's *wording*
branches on :attr:`MotivatorResult.probe_reason` the same way
:meth:`MotivatorResult.headline` does — see :func:`probe_form` and
:data:`PROBE_FORMS`. A probe that opens "your answers put Practical Return and
Understanding at the top" is stating a finding to the person's face, and on a
flat answer set there is no finding to state, only tiebreak order. Advisory
only, like everything in Smart Teams: never an input to review, promotion, or
compensation.
``ADVISORY_NOTICE`` lives in ``disc.py`` as the single source of truth for that
disclaimer and is imported here rather than restated, so the two layers can
never drift into saying different things about the same promise.

Design notes worth knowing before you change anything here
----------------------------------------------------------
* **Structure follows ``disc.py``.** Same shapes, deliberately: ``Literal``
  vocabularies rather than enums, a frozen spec dataclass per dimension holding
  the prose the generator renders, ``item_id``/``prompt`` field names, an
  ``as_dict()`` on every result for the ``st_profiles`` JSON, and an
  ``interview_payload()`` that withholds keying and dimension from the
  frontend. When a third scale layer lands, lift ``LikertItem`` and the Likert
  constants into ``instruments/base.py`` and have all of them import it.
* **``ITEM_BANK_VERSION`` is stamped on every result.** ``disc.py`` predates it
  and should get one. Unknown item ids *raise* rather than being skipped, same
  as there — a silently dropped answer is a silently wrong ranking. The
  consequence is that rescoring an interview stored under an older bank needs an
  explicit migration, which is the intended cost.
* **Reverse-keyed items are load-bearing.** Twelve of the 30 are reverse-keyed,
  spread across all six dimensions. A five-point agreement scale otherwise
  rewards agreeable people on all six axes at once, which flattens exactly the
  ordering this layer exists to produce.
* **Presentation order is part of the instrument.** ``LIKERT_ITEMS`` cycles the
  six dimensions in a fixed rotation and scatters the reverse items so no
  keying run exceeds three. Scoring is order-independent, so re-sorting the
  tuple breaks nothing you would notice — which is the danger. This bank has no
  response-quality check of its own yet: ``disc.check_response_quality`` is the
  model for the one it needs, and a run-length test only means something on the
  order the respondent actually saw. Do not re-sort.
* **No LLM anywhere in this file.** Scoring is pure and deterministic, ties
  included. The LLM's only job is writing prose *from* this output, and reading
  the probe answers.

Sources, provenance, and the limitations argument in full:
``docs/smart_teams/instruments/motivators_and_enneagram.md``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from .disc import ADVISORY_NOTICE

#: The six axis keys. Stable — they are the JSON keys under ``st_profiles``, so
#: renaming one is a migration, not an edit.
#:
#: These are working identifiers, not a provenance claim. ``economic``,
#: ``theoretical``, ``social`` and ``aesthetic`` are Spranger's own English
#: terms; ``individualistic`` and ``traditional`` stand in for his *political*
#: and *religious*, which do not survive contact with a 2026 workplace document.
#: Those two substitutes match the axis names TTI Success Insights publishes —
#: common single words for an axis, carrying none of its content. See the module
#: docstring for the full three-vocabulary breakdown, and ``SPRANGER_LABELS``
#: below for the citation-accurate originals.
Motivator = Literal[
    "economic",
    "theoretical",
    "social",
    "individualistic",
    "traditional",
    "aesthetic",
]
Keying = Literal["positive", "reverse"]

#: Bumped whenever an item's prompt, dimension, or keying changes — anything
#: that makes an old set of answers score differently. Stamped on every result.
ITEM_BANK_VERSION = "motivators-2026-08-1"

#: Canonical order. Also the deterministic tiebreak when two orientations land on
#: the same emphasis — without a fixed order, two identical interviews could
#: produce two different rankings. It carries no other meaning.
MOTIVATORS: tuple[Motivator, ...] = (
    "economic",
    "theoretical",
    "social",
    "individualistic",
    "traditional",
    "aesthetic",
)

#: Our labels. These are what appears in prose.
MOTIVATOR_NAMES: Mapping[Motivator, str] = {
    "economic": "Practical Return",
    "theoretical": "Understanding",
    "social": "Human Impact",
    "individualistic": "Standing and Autonomy",
    "traditional": "Guiding Framework",
    "aesthetic": "Form and Craft",
}

#: Spranger's 1928 axis names, kept for the methodology footnote in the
#: generated guide and for the docs. Not used in user-facing prose: "religious"
#: and "political" mean something different in a 2026 workplace than they did in
#: 1914, which is why every later instrument relabelled them.
#:
#: This mapping is also the place the divergence is visible: four keys map to
#: themselves and two do not. That is the whole reason it exists — cite from
#: here, never from the enum keys.
SPRANGER_LABELS: Mapping[Motivator, str] = {
    "economic": "Economic",
    "theoretical": "Theoretical",
    "social": "Social",
    "individualistic": "Political",
    "traditional": "Religious",
    "aesthetic": "Aesthetic",
}

#: One line each, for team reports that need to name an axis in passing without
#: printing a whole spec.
MOTIVATOR_SUMMARIES: Mapping[Motivator, str] = {
    "economic": "Judges the work by what it returns, and wants the number first.",
    "theoretical": "Wants to understand the mechanism, and will go to the source.",
    "social": "Measures a win by who it helped, and treats people work as the work.",
    "individualistic": "Wants room to run, a hand on the direction, and the credit.",
    "traditional": "Holds the work against a standard, and notices inconsistency.",
    "aesthetic": "Cares how the work reads and looks, not only whether it is right.",
}


# ── The six orientations ──────────────────────────────────────────────────────
@dataclass(frozen=True)
class MotivatorSpec:
    """Everything the guide generator may say about one orientation.

    The prose lives here rather than in the generator so that a person reading
    their own guide, a pair guide, and a team report all see the same
    characterisation of the same axis, and so a wording change is one edit
    reviewed once. If a line would be equally true of any of the six, it does
    not belong here.
    """

    motivator: Motivator
    #: What the orientation is, in one or two sentences.
    definition: str
    #: How to work with somebody who ranks this near the top.
    high_read: str
    #: How to work with somebody who ranks this near the bottom. Low means the
    #: motive is weak, never that the person is deficient — the copy must not
    #: read as a fault, because the person reads this about themselves.
    low_read: str
    #: The move this orientation makes when a team argues about priorities. The
    #: field pair and team guides lean on hardest, because a priority fight is
    #: where value differences stop being abstract.
    in_conflict: str

    @property
    def name(self) -> str:
        return MOTIVATOR_NAMES[self.motivator]

    def as_dict(self) -> dict[str, Any]:
        return {
            "motivator": self.motivator,
            "name": self.name,
            "definition": self.definition,
            "high_read": self.high_read,
            "low_read": self.low_read,
            "in_conflict": self.in_conflict,
        }


MOTIVATOR_SPECS: Mapping[Motivator, MotivatorSpec] = {
    "economic": MotivatorSpec(
        motivator="economic",
        definition=(
            "Work is worth doing when it produces something countable — revenue, "
            "cost avoided, hours given back, risk retired. Choices get weighed as "
            "payoff against effort, and 'interesting' is not a reason on its own."
        ),
        high_read=(
            "Lead with the payoff and the cost, then the method. Put the number in "
            "the first two sentences of any ask — a request that arrives without one "
            "reads as unfinished thinking rather than as a question. They will "
            "happily cut scope to protect the return, so tell them which parts of a "
            "plan are load-bearing before they optimise one of them away."
        ),
        low_read=(
            "A business case is not what convinces them, and pushing one harder "
            "reads as pressure. Give them the reason the work matters on its own "
            "terms and let the economics be a constraint rather than the argument. "
            "Worth watching: they tend to under-price their own contribution when "
            "budget season asks them to justify it, so somebody else should make "
            "that case for them."
        ),
        in_conflict=(
            "Reframes the disagreement as a return question — 'what does this get "
            "us, and when' — and loses patience with any option whose value nobody "
            "can state. Effective at killing waste; will under-weight work whose "
            "payoff is real but slow or unmeasurable."
        ),
    ),
    "theoretical": MotivatorSpec(
        motivator="theoretical",
        definition=(
            "The point of the work is to know how the thing actually works. A fix "
            "with no explanation behind it is an unfinished fix, and a summary is "
            "never as good as the underlying rule, document, or data."
        ),
        high_read=(
            "Send the source, not the summary — they will go find it anyway, and "
            "being handed somebody's paraphrase of a regulation they could have read "
            "themselves costs you credibility. Expect 'why' questions that are "
            "curiosity rather than challenge. Give them the problem before the "
            "answer; they are at their best on the messy diagnostic nobody else "
            "wants to sit with."
        ),
        low_read=(
            "They are content to act on a conclusion without walking the derivation, "
            "which makes them fast and occasionally makes them repeat a mistake "
            "whose cause never got named. Don't send background reading and expect "
            "it to be read. Do give them the one sentence of reasoning that changes "
            "what they would do differently next time."
        ),
        in_conflict=(
            "Slows the argument down to establish what is actually true before "
            "anybody picks a direction, and will not concede a point to consensus if "
            "the evidence doesn't support it. Catches the flaw everyone else talked "
            "past; can hold a decision open past the point where the extra certainty "
            "is worth the delay."
        ),
    ),
    "social": MotivatorSpec(
        motivator="social",
        definition=(
            "The part of a win worth having is the person it helped. Time spent on "
            "colleagues, clients, or the person two desks over who is stuck is the "
            "work, not a detour from it."
        ),
        high_read=(
            "Name who benefits and you have their attention; describe an outcome "
            "purely as a metric and you have lost it. They will absorb other "
            "people's problems until their own list slips, so protecting their focus "
            "is a favour, not a restriction. Ask them who the change lands on — they "
            "usually know before anyone else does."
        ),
        low_read=(
            "They are not cold; helping simply is not the fuel. They will do the "
            "supportive thing when it is on the list and not spontaneously, so put "
            "it on the list rather than hoping. In return, they hold a boundary "
            "under pressure that more people-driven colleagues struggle to hold."
        ),
        in_conflict=(
            "Argues from who absorbs the cost — 'this lands on the ops team and "
            "nobody asked them'. Keeps a decision honest about its human price; can "
            "resist a necessary change longer than the situation allows because "
            "somebody will be worse off in the short term."
        ),
    ),
    "individualistic": MotivatorSpec(
        motivator="individualistic",
        definition=(
            "Room to run, a hand in setting direction, and being known for the work. "
            "Being handed an outcome to own beats being handed a plan to execute."
        ),
        high_read=(
            "Give them the goal and the constraints, then get out of the way; "
            "prescribing the method reads as distrust even when it was meant as "
            "help. Put them in the room where the direction is set rather than "
            "briefing them after, and attribute their work by name in front of the "
            "people who matter — visibility is compensation for them, not vanity."
        ),
        low_read=(
            "Happy to execute a plan somebody else set, and genuinely untroubled by "
            "who gets named. Don't read that as no ambition or no opinion — they "
            "will have views and will not fight for airtime, so ask directly. Their "
            "contribution is the one most likely to go unrecorded, which is a "
            "manager's problem to fix, not theirs."
        ),
        in_conflict=(
            "Contests the decision itself rather than working around it, and will "
            "keep pushing after the room has moved on if they think the call is "
            "wrong. Stops a bad plan going through on politeness; can turn a scoping "
            "debate into a question of who is in charge."
        ),
    ),
    "traditional": MotivatorSpec(
        motivator="traditional",
        definition=(
            "A settled standard to hold the work against — a policy, a professional "
            "code, a personal line — and consistency between what we said we would "
            "do and what we did."
        ),
        high_read=(
            "Tell them the standard and they will hold it without supervision. When "
            "you need an exception, say plainly that it is an exception and why; "
            "quietly bending a rule and hoping nobody notices is the fastest way to "
            "lose them. They are the person who notices that this quarter's answer "
            "contradicts last quarter's, which is worth more than it is comfortable."
        ),
        low_read=(
            "Treats rules as defaults to adjust to the situation, which makes them "
            "adaptable and occasionally makes them the reason a process has three "
            "versions. Give them the intent behind a requirement rather than the "
            "requirement — they will follow reasoning further than they follow "
            "instruction."
        ),
        in_conflict=(
            "Anchors on the principle or the commitment already made: 'we told the "
            "client this, so this is what we do'. Holds the line when it is "
            "expensive; can defend a standard that was right for a situation we are "
            "no longer in."
        ),
    ),
    "aesthetic": MotivatorSpec(
        motivator="aesthetic",
        definition=(
            "How the work reads, looks, and feels is part of whether it is any good. "
            "Correct but graceless is not finished, and a well-made thing is worth "
            "the extra pass."
        ),
        high_read=(
            "Their draft is better than most people's final, and they know the "
            "difference between the two — so 'good enough, ship it' needs the reason "
            "attached or it sounds like their standards are the problem. Give them "
            "the work that a client will actually look at. Give them a deadline for "
            "the polish pass too, or the pass expands."
        ),
        low_read=(
            "Content over container: as long as it is right, presentation is "
            "somebody else's concern. Fast and unfussy, and reliably surprised when "
            "a correct deliverable lands badly because of how it looked. Pair them "
            "with a craft-driven reviewer before anything goes outside the company."
        ),
        in_conflict=(
            "Objects to the version of the plan that is crude even when it works, "
            "and would rather delay than put something shabby in front of a client. "
            "Protects how the company looks to the outside; can spend the last 10% of the "
            "budget on the last 2% of the finish."
        ),
    ),
}


def spec_for(motivator: Motivator) -> MotivatorSpec:
    """The prose block for one orientation."""
    return MOTIVATOR_SPECS[motivator]


# ── The item bank ─────────────────────────────────────────────────────────────
LIKERT_MIN = 1
LIKERT_MAX = 5

#: The anchors the interview must show. Scoring assumes these five points, so the
#: frontend reads them from here rather than hardcoding its own labels. Five
#: points with a labelled midpoint: forcing a side on a values question produces
#: more guessing, not more signal.
LIKERT_ANCHORS: Mapping[int, str] = {
    1: "Not like me at all",
    2: "A little like me",
    3: "Somewhat like me",
    4: "Mostly like me",
    5: "Exactly like me",
}


@dataclass(frozen=True)
class LikertItem:
    """One agreement statement pointing at one orientation.

    ``keying`` is the direction the item points: a ``"reverse"`` item is worded
    so that *disagreeing* with it indicates the orientation, and its raw response
    is flipped before it enters the mean. Reverse items exist to break the rhythm
    of a respondent who has stopped reading, so never quietly convert one to
    positive keying to "simplify" the bank.

    ``item_id`` is stable and permanent. Interview answers are stored keyed by
    it, so an id may be retired but never reused for different text.
    """

    item_id: str
    motivator: Motivator
    keying: Keying
    prompt: str

    @property
    def reverse(self) -> bool:
        return self.keying == "reverse"

    def score(self, response: int) -> int:
        """The response as it enters the mean (reverse items flipped)."""
        if response not in LIKERT_ANCHORS:
            raise ValueError(f"{self.item_id}: response {response!r} outside 1-5 scale")
        if self.reverse:
            return LIKERT_MIN + LIKERT_MAX - response
        return response


#: 30 items, five per orientation (three positively keyed, two reverse-keyed).
#: Ids are ``mot_<axis>_<nn>`` and permanent; the number in the id is an
#: authoring label, not a position. The tuple cycles the six orientations in a
#: fixed rotation so no two consecutive items share a dimension, and the twelve
#: reverse items are scattered so no keying run exceeds three. That structure is
#: only worth anything on the order the respondent saw, so the tuple must not be
#: re-sorted — see the module docstring.
LIKERT_ITEMS: tuple[LikertItem, ...] = (
    # ── rotation 1 ──
    LikertItem(
        "mot_eco_01",
        "economic",
        "positive",
        "Choosing between two projects, I ask which one pays off soonest.",
    ),
    LikertItem(
        "mot_thr_01",
        "theoretical",
        "positive",
        "I am not satisfied with a fix until I understand why the thing broke.",
    ),
    LikertItem(
        "mot_soc_04",
        "social",
        "reverse",
        "Whether the work helps anyone in particular is not what drives me.",
    ),
    LikertItem(
        "mot_ind_01",
        "individualistic",
        "positive",
        "I want to be the one who decides how my work gets done.",
    ),
    LikertItem(
        "mot_trd_04",
        "traditional",
        "reverse",
        "Rules are starting points I adjust to fit the situation in front of me.",
    ),
    LikertItem(
        "mot_aes_01",
        "aesthetic",
        "positive",
        "How a deliverable reads and looks matters to me, not only whether it is correct.",
    ),
    # ── rotation 2 ──
    LikertItem(
        "mot_eco_04",
        "economic",
        "reverse",
        "I would gladly spend a quarter on work that will never show up in a budget line.",
    ),
    LikertItem(
        "mot_thr_02",
        "theoretical",
        "positive",
        "I would rather read the rule or the standard myself than take somebody's summary of it.",
    ),
    LikertItem(
        "mot_soc_01",
        "social",
        "positive",
        "The part of a win I remember afterwards is who it helped.",
    ),
    LikertItem(
        "mot_ind_04",
        "individualistic",
        "reverse",
        "I am content carrying out a good plan that somebody else designed.",
    ),
    LikertItem(
        "mot_trd_01",
        "traditional",
        "positive",
        "I want a clear standard to work against, and I hold myself to it.",
    ),
    LikertItem(
        "mot_aes_04",
        "aesthetic",
        "reverse",
        "As long as the content is right, presentation is somebody else's concern.",
    ),
    # ── rotation 3 ──
    LikertItem(
        "mot_eco_02",
        "economic",
        "positive",
        "I want my work measured in something countable — dollars, hours saved, risk removed.",
    ),
    LikertItem(
        "mot_thr_04",
        "theoretical",
        "reverse",
        "If the workaround holds, I do not need to know why the original failed.",
    ),
    LikertItem(
        "mot_soc_02",
        "social",
        "positive",
        "I will rearrange my own day to unblock a colleague who is stuck.",
    ),
    LikertItem(
        "mot_ind_02",
        "individualistic",
        "positive",
        "When the direction is being set in a meeting, I want to be in the room.",
    ),
    LikertItem(
        "mot_trd_05",
        "traditional",
        "reverse",
        "I am comfortable working with no settled set of principles to check against.",
    ),
    LikertItem(
        "mot_aes_02",
        "aesthetic",
        "positive",
        "I will spend extra time making something well-made rather than merely finished.",
    ),
    # ── rotation 4 ──
    LikertItem(
        "mot_eco_03",
        "economic",
        "positive",
        "A clever solution that never pays for itself stops interesting me quickly.",
    ),
    LikertItem(
        "mot_thr_03",
        "theoretical",
        "positive",
        "I enjoy taking a messy problem apart just to see how it really works.",
    ),
    LikertItem(
        "mot_soc_05",
        "social",
        "reverse",
        "I would rather be left alone to finish my own work than be pulled into somebody else's problem.",
    ),
    LikertItem(
        "mot_ind_05",
        "individualistic",
        "reverse",
        "Whether I get credit for an outcome barely registers with me.",
    ),
    LikertItem(
        "mot_trd_02",
        "traditional",
        "positive",
        "Doing what we said we would do matters to me more than doing it quickly.",
    ),
    LikertItem(
        "mot_aes_03",
        "aesthetic",
        "positive",
        "A cluttered document or a badly laid-out screen genuinely bothers me.",
    ),
    # ── rotation 5 ──
    LikertItem(
        "mot_eco_05",
        "economic",
        "reverse",
        "Whether my work makes money is somebody else's question, not mine.",
    ),
    LikertItem(
        "mot_thr_05",
        "theoretical",
        "reverse",
        "Digging into background detail feels like a detour from the real work.",
    ),
    LikertItem(
        "mot_soc_03",
        "social",
        "positive",
        "Teaching somebody to do a task well is more satisfying to me than doing it myself.",
    ),
    LikertItem(
        "mot_ind_03",
        "individualistic",
        "positive",
        "It matters to me that the people who make decisions know my work.",
    ),
    LikertItem(
        "mot_trd_03",
        "traditional",
        "positive",
        "My work has to line up with what I think is right, even when nobody would notice the difference.",
    ),
    LikertItem(
        "mot_aes_05",
        "aesthetic",
        "reverse",
        "Time spent on polish is time taken away from the actual work.",
    ),
)

_ITEMS_BY_ID: Mapping[str, LikertItem] = {item.item_id: item for item in LIKERT_ITEMS}


def items_for(motivator: Motivator) -> tuple[LikertItem, ...]:
    """The items belonging to one orientation, in presentation order."""
    return tuple(item for item in LIKERT_ITEMS if item.motivator == motivator)


def interview_payload() -> dict[str, Any]:
    """Exactly what the interview page needs to render this layer — and nothing
    more. Keying and dimension are deliberately withheld: a respondent who can
    see which answer scores which orientation is answering a different
    instrument, and on a values bank that is the difference between what someone
    cares about and what they would like to be seen caring about.
    """
    return {
        "layer": "motivators",
        "scale": [
            {"value": value, "label": label} for value, label in LIKERT_ANCHORS.items()
        ],
        "items": [
            {"item_id": item.item_id, "prompt": item.prompt} for item in LIKERT_ITEMS
        ],
    }


# ── Scoring ───────────────────────────────────────────────────────────────────
#: An orientation needs this many answered items before its score is trustworthy
#: enough to rank. Below it the axis is marked thin and the whole result is
#: incomplete — a ranking with a guessed-at axis in it is worse than no ranking,
#: because the guide states it as fact.
MIN_ITEMS_PER_MOTIVATOR = 3

#: Two adjacent emphases closer than this are reported as effectively level. On
#: the 0-100 within-person scale, 8 points is roughly one scale point on one item
#: of five — well inside the noise of a bank this short.
CLOSE_RANK_MARGIN = 8.0

#: If all six fit inside this spread, nothing is really leading and we say so
#: instead of inventing a top two out of tiebreak order. Same idea as
#: ``disc.py``'s ``LEVEL_PROFILE_SPREAD``, and it fires on the two answer
#: patterns that produce it in practice: a flat middle-of-the-scale run, and
#: agreeing with all 30 items.
LEVEL_PROFILE_SPREAD = 8.0


@dataclass
class MotivatorScore:
    """One orientation's standing for one person.

    ``emphasis`` is *within-person* on a 0-100 scale — the mean of this person's
    answers on this axis, recoded for keying and rescaled. It is not a percentile
    and must never be rendered as one: there is no norm group behind it, so "68
    on Human Impact" only means something next to this person's own other five
    numbers. See the module docstring on why the field is not called
    ``intensity``.
    """

    motivator: Motivator
    emphasis: float
    answered: int
    #: Too few answers to rank honestly (see ``MIN_ITEMS_PER_MOTIVATOR``).
    thin: bool

    @property
    def name(self) -> str:
        return MOTIVATOR_NAMES[self.motivator]

    def as_dict(self) -> dict[str, Any]:
        return {
            "motivator": self.motivator,
            "name": self.name,
            "emphasis": self.emphasis,
            "answered": self.answered,
            "thin": self.thin,
        }


def _join_names(motivators: Sequence[Motivator]) -> str:
    """Join orientation display names into readable English.

    Used wherever a tie has to be reported as a tie: naming every member is the
    only honest option once the ranking between them is tie-break order.
    """
    names = [MOTIVATOR_NAMES[m] for m in motivators]
    if len(names) <= 1:
        return names[0] if names else ""
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + f", and {names[-1]}"


@dataclass
class MotivatorResult:
    """The whole deterministic output of this layer: a ranking, plus how much to
    trust it.

    Consumers should reach for :attr:`top_two` and :attr:`bottom` rather than the
    numbers. The middle of the ranking is the part a 30-item bank cannot resolve,
    and a guide that solemnly reports somebody's fourth-strongest value is
    inventing precision it does not have.
    """

    #: Highest emphasis first; ties broken by :data:`MOTIVATORS`.
    ranked: tuple[MotivatorScore, ...]
    answered: int
    #: Adjacent pairs whose emphases sit inside :data:`CLOSE_RANK_MARGIN` —
    #: report these as "roughly level", never as one outranking the other.
    near_ties: tuple[tuple[Motivator, Motivator], ...]
    #: False when any orientation was answered too thinly to rank.
    is_complete: bool
    item_bank_version: str = ITEM_BANK_VERSION

    @property
    def ranking(self) -> tuple[Motivator, ...]:
        return tuple(entry.motivator for entry in self.ranked)

    @property
    def top_two(self) -> tuple[Motivator, ...]:
        """The two leading orientations — the headline of this layer.

        Positional, not a finding: on a level or incomplete result these two are
        :data:`MOTIVATORS` declaration order rather than anything the person
        expressed. Check :attr:`probe_reason` before putting them in a sentence.
        """
        return self.ranking[:2]

    @property
    def bottom(self) -> Motivator:
        """The orientation that loses when it competes with the others.

        Same caveat as :attr:`top_two`, plus one of its own: even on a result
        that needs no probe, the last two places can be level with each other.
        :attr:`bottom_is_contested` is what says whether "this one comes last"
        is a claim or a coin flip.
        """
        return self.ranking[-1]

    @property
    def top_two_margin(self) -> float:
        """Gap between first and second. Small means the pair is unordered."""
        if len(self.ranked) < 2:
            return 0.0
        return round(self.ranked[0].emphasis - self.ranked[1].emphasis, 1)

    @property
    def bottom_is_contested(self) -> bool:
        """True when which orientation comes *last* is tiebreak order.

        Deliberately independent of :attr:`probe_reason`, which is about the top
        of the ranking: a profile can have fifty points between first and second
        and still have its last two places a rounding error apart. Both the
        headline and the probe's bottom-of-ranking question consult this, because
        "Guiding Framework moves them least" is a sentence about the person that
        they read about themselves.
        """
        if len(self.ranked) < 2:
            return False
        gap = round(self.ranked[-2].emphasis - self.ranked[-1].emphasis, 1)
        return gap < CLOSE_RANK_MARGIN

    @property
    def trailing_group(self) -> tuple[Motivator, ...]:
        """Every orientation sitting within :data:`CLOSE_RANK_MARGIN` of last.

        :attr:`bottom_is_contested` answers "is last place a coin flip" but not
        "between how many". The headline used to widen its closing clause to
        exactly two names whenever the answer was yes, so a respondent with four
        orientations tied at the bottom was told that two specific ones —
        whichever two the tie-break surfaced — moved them least. Naming a subset
        of a tie is the same defect as naming a single winner of one.
        """
        if not self.ranked:
            return ()
        floor = self.ranked[-1].emphasis
        return tuple(
            entry.motivator
            for entry in self.ranked
            if round(entry.emphasis - floor, 1) < CLOSE_RANK_MARGIN
        )

    @property
    def leading_group(self) -> tuple[Motivator, ...]:
        """Every orientation within :data:`CLOSE_RANK_MARGIN` of the leader.

        Three or more means there is no top two to report: :attr:`top_two` would
        be returning declaration order for the second slot and possibly the
        first.
        """
        if not self.ranked:
            return ()
        ceiling = self.ranked[0].emphasis
        return tuple(
            entry.motivator
            for entry in self.ranked
            if round(ceiling - entry.emphasis, 1) < CLOSE_RANK_MARGIN
        )

    def emphasis_for(self, motivator: Motivator) -> float:
        for entry in self.ranked:
            if entry.motivator == motivator:
                return entry.emphasis
        raise KeyError(motivator)

    def rank_of(self, motivator: Motivator) -> int:
        """1-based position in the ranking."""
        return self.ranking.index(motivator) + 1

    @property
    def spread(self) -> float:
        """Distance from the strongest orientation to the weakest."""
        if not self.ranked:
            return 0.0
        return round(self.ranked[0].emphasis - self.ranked[-1].emphasis, 1)

    @property
    def is_level(self) -> bool:
        """True when no orientation is really leading (see
        :data:`LEVEL_PROFILE_SPREAD`).

        A level result is not a broken one — plenty of people genuinely weigh
        these six about equally — but its ``ranking`` is tiebreak order rather
        than a finding, so nothing may present a top two from it.
        """
        return bool(self.ranked) and self.spread < LEVEL_PROFILE_SPREAD

    @property
    def probe_reason(self) -> str | None:
        """Why the ranking needs confirming, or ``None`` if it stands on its own.

        Returned as a stable code so the interview UI and
        :meth:`headline` can both say something true about *this* result rather
        than one hedge covering every case. Checked most-serious first.
        """
        if not self.is_complete:
            return "incomplete"
        if self.is_level:
            return "level_profile"
        if self.top_two_margin < CLOSE_RANK_MARGIN:
            return "top_two_unordered"
        # The leader is clear but the runner-up is level with what follows, so
        # *which* orientation is second is not established.
        leaders = set(self.top_two)
        if any(leaders & {first, second} for first, second in self.near_ties):
            return "runner_up_contested"
        return None

    @property
    def probe_required(self) -> bool:
        """True when the ranking must not be published unconfirmed.

        The probe runs in *every* interview — the point of the conversational
        segment is to hear the ranking in the person's own words. This flag is
        narrower: it marks the results where the item scores alone cannot carry
        the claim, so the draft guide must show the probe answers (or nothing)
        rather than the inferred order. What the probe is allowed to *say* in
        each case is :func:`probe_form`'s job.
        """
        return self.probe_reason is not None

    def headline(self) -> str:
        """One sentence safe to put in front of the person or a colleague.

        The hedge has to match the actual reason for doubt. An earlier version
        appended "their answers put these close together" to anything that
        needed a probe, which read as a flat lie on a profile whose top two were
        fifty points apart because the *third* place was contested.

        The closing clause gets the same treatment for the same reason. It used
        to name one orientation as the one that moves the person least whatever
        the numbers said, so two axes a rounding error apart came out as a
        finding about the lower of the two.
        """
        reason = self.probe_reason
        # An incomplete bank cannot be ranked at all. Unanswered orientations
        # are carried at the neutral midpoint so the arithmetic stays defined,
        # which means a ranking built over them is mostly a ranking of defaults:
        # with one item answered the old sentence still read "Driven most by
        # Practical Return then Understanding (part of this section was left
        # unanswered)", naming two orientations the person never expressed. DISC
        # and the Enneagram both report the count and stop; so does this now.
        if reason == "incomplete":
            total = len(LIKERT_ITEMS)
            share = round(self.answered / total * 100) if total else 0
            return (
                f"Only {self.answered} of {total} items answered ({share}%); not "
                "enough to rank what drives them. Nothing else on the profile is "
                "affected, and this section can be finished at any time."
            )

        # Checked after the incomplete branch above: with nothing answered every
        # orientation sits at the default midpoint, which is "level" only in the
        # arithmetic sense. Saying their answers weigh all six equally would be
        # a claim about answers that do not exist.
        if self.is_level:
            return (
                "No orientation clearly leads — their answers weigh all six about "
                "equally, so this needs a conversation rather than a ranking."
            )

        # Three or more orientations level at the top means there is no leading
        # pair to name — the second slot, and maybe the first, would be
        # declaration order. Say what is true instead of ranking noise.
        if len(self.leading_group) > 2:
            leaders = _join_names(self.leading_group)
            return (
                f"Several orientations came out level at the top — {leaders} sit "
                "within a rounding error of each other — so their answers do not "
                "single out what drives them most."
            )

        # The closing clause is shared by every branch below.
        trailing_now = self.trailing_group
        if len(trailing_now) > 2:
            tail_clause = ""
        elif len(trailing_now) == 2:
            tail_clause = f"; {_join_names(trailing_now)} move them least"
        else:
            tail_clause = f"; {MOTIVATOR_NAMES[self.bottom]} moves them least"

        # The leader is clear but second place is level with what follows, so
        # naming a runner-up would be reporting tie-break order. Saying "X then
        # Y (the second place is a close call)" was the old behavior: it printed
        # the ranking it had just conceded it could not establish, and that
        # sentence is what a refused layer shows its subject.
        if reason == "runner_up_contested":
            return (
                f"Driven most by {MOTIVATOR_NAMES[self.ranking[0]]}; which "
                f"orientation comes second is not settled by these answers"
                f"{tail_clause}."
            )

        first, second = (MOTIVATOR_NAMES[m] for m in self.top_two)
        # "then" asserts an order; only use it when the gap supports one.
        joiner = "then" if self.top_two_margin >= CLOSE_RANK_MARGIN else "and"
        hedge = {
            "top_two_unordered": " (their answers put these two close together)",
            "runner_up_contested": " (the second place is a close call)",
            "incomplete": " (part of this section was left unanswered)",
        }.get(reason or "", "")

        return f"Driven most by {first} {joiner} {second}{hedge}{tail_clause}."

    def as_dict(self) -> dict[str, Any]:
        return {
            "layer": "motivators",
            "item_bank_version": self.item_bank_version,
            "ranking": list(self.ranking),
            "scores": [entry.as_dict() for entry in self.ranked],
            "top_two": list(self.top_two),
            "bottom": self.bottom,
            "top_two_margin": self.top_two_margin,
            "spread": self.spread,
            "near_ties": [list(pair) for pair in self.near_ties],
            "answered": self.answered,
            "is_complete": self.is_complete,
            "is_level": self.is_level,
            "bottom_is_contested": self.bottom_is_contested,
            "probe_required": self.probe_required,
            "probe_reason": self.probe_reason,
            "headline": self.headline(),
            "advisory_notice": ADVISORY_NOTICE,
        }


def _validated(responses: Mapping[str, int]) -> dict[str, int]:
    """Reject unknown ids and out-of-scale values loudly.

    A typo'd item id that silently scored as "unanswered" would quietly shift an
    axis and nobody would ever notice, so this is deliberately strict — the same
    call ``disc.py`` and ``enneagram.py`` make. The cost is that rescoring
    answers stored under an older :data:`ITEM_BANK_VERSION` needs an explicit
    migration rather than happening by accident.

    ``big_five.py`` does not agree — it logs unknown ids and carries on. That is
    a real inconsistency in this package awaiting reconciliation, so do not
    "align" this function down to it on the assumption the looser behaviour was
    the considered one.
    """
    clean: dict[str, int] = {}
    for item_id, value in responses.items():
        if item_id not in _ITEMS_BY_ID:
            raise ValueError(f"unknown motivators item id: {item_id!r}")
        if value is None:
            continue
        if value not in LIKERT_ANCHORS:
            raise ValueError(f"{item_id}: response {value!r} outside the 1-5 scale")
        clean[item_id] = int(value)
    return clean


def score_motivators(responses: Mapping[str, int]) -> MotivatorResult:
    """The one entry point: answers in, ranked motivators layer out.

    ``responses`` maps :class:`LikertItem` ids to 1-5. Missing items are allowed
    and prorated within their axis; unknown ids and out-of-range values raise,
    because a silently dropped answer is a silently wrong ranking.

    Pure and deterministic — same answers in, same result out, tiebreak included.
    An axis with no answers at all sits at the midpoint rather than at zero, so
    "we didn't ask" can never masquerade as a confident "this does not motivate
    them"; :attr:`MotivatorResult.is_complete` is what tells you the difference.
    """
    clean = _validated(responses)

    per_motivator: dict[Motivator, list[int]] = {m: [] for m in MOTIVATORS}
    for item in LIKERT_ITEMS:
        response = clean.get(item.item_id)
        if response is None:
            continue
        per_motivator[item.motivator].append(item.score(response))

    scores: list[MotivatorScore] = []
    for motivator in MOTIVATORS:
        values = per_motivator[motivator]
        if values:
            mean = sum(values) / len(values)
            emphasis = (mean - LIKERT_MIN) / (LIKERT_MAX - LIKERT_MIN) * 100.0
        else:
            emphasis = 50.0
        scores.append(
            MotivatorScore(
                motivator=motivator,
                emphasis=round(emphasis, 1),
                answered=len(values),
                thin=len(values) < MIN_ITEMS_PER_MOTIVATOR,
            )
        )

    ranked = tuple(
        sorted(
            scores,
            key=lambda entry: (-entry.emphasis, MOTIVATORS.index(entry.motivator)),
        )
    )
    near_ties = tuple(
        (first.motivator, second.motivator)
        for first, second in zip(ranked, ranked[1:])
        if first.emphasis - second.emphasis < CLOSE_RANK_MARGIN
    )

    return MotivatorResult(
        ranked=ranked,
        answered=len(clean),
        near_ties=near_ties,
        is_complete=not any(entry.thin for entry in scores),
    )


# ── The confirmation probe ────────────────────────────────────────────────────
# Thirty Likert answers can order six values about as well as a thermometer can
# diagnose a fever: usefully, and not on its own. These four questions are the
# conversational segment that follows the bank. Two of them are deliberately
# about cost — what somebody gave up, and what left them flat despite going well
# — because people describe their values accurately when they are talking about
# a trade they actually made, and aspirationally when asked directly.
#
# The probe runs in every interview, which makes it the most-read prose this
# module produces — and the person reading it is the person it is about. That is
# why there are four *forms* of it rather than one. Each form is allowed to name
# only what the scores behind it can actually support:
#
#   ``confident``            the ranking stands on its own: name the top two in
#                            order, and name the bottom.
#   ``pair_unordered``       two orientations lead but not in any established
#                            order: name the pair, never a first place.
#   ``second_place_hedged``  the leader is clear and the runner-up is not: name
#                            the leader, and ask whether second place belongs to
#                            the orientation we put there.
#   ``open``                 nothing is established — a level profile, or an axis
#                            answered too thinly to place — so no orientation is
#                            named at all and the questions ask from a blank page.
#
# The first cut of this segment had only the confident wording and rendered it
# unconditionally. On an all-midpoint answer set every axis scores 50.0, the
# "ranking" is :data:`MOTIVATORS` declaration order, and that wording told the
# person "your answers put Practical Return and Understanding at the top of what
# drives you at work" and then asked them to account for a bottom-ranked value
# they had never expressed — the same invented claim, about themselves, for every
# flat responder in the company. :attr:`MotivatorResult.probe_reason` existed
# already and nothing consulted it.
#
# So: nothing may be added here that states a position in the ranking without
# going through :func:`probe_form`, and a new :attr:`probe_reason` code without
# an entry in :data:`_FORM_FOR_PROBE_REASON` raises rather than silently falling
# back to the confident wording.

#: Which wording a result has earned. Not a user-facing string — the interview UI
#: gets rendered questions, not a form name — but stable, because a stored probe
#: answer only means something next to the question that was actually asked.
ProbeForm = Literal[
    "confident",
    "pair_unordered",
    "second_place_hedged",
    "open",
]


@dataclass(frozen=True)
class ProbeQuestion:
    """One question in the motivators confirmation segment.

    ``template`` is rendered with the person's own leading and trailing
    orientations, so the conversation starts from what their answers said
    instead of asking them to introspect from a blank page. The ``open`` form's
    templates deliberately use none of those placeholders: when the scores have
    not established a ranking, a blank page is the honest starting point.

    ``question_id`` is what a probe answer is stored under, and it identifies the
    *purpose* rather than the wording — every form's question about the bottom of
    the ranking is ``mot_probe_04``, however it had to be phrased. So an id may
    be reworded across forms and must never be reused for a different question.
    """

    question_id: str
    #: What this question is for. Shown to nobody; it exists so a future editor
    #: does not reword the question into uselessness.
    purpose: str
    template: str

    def render(self, result: MotivatorResult) -> str:
        first, second = (MOTIVATOR_NAMES[m] for m in result.top_two)
        return self.template.format(
            first=first,
            second=second,
            last=MOTIVATOR_NAMES[result.bottom],
        )


# ── Question 1: the top of the ranking, anchored on a real decision ───────────
_TIPPING_POINT_PURPOSE = (
    "Direct confirmation of what leads, anchored on a real decision so the "
    "answer is evidence rather than self-image."
)

_TIPPING_POINT_CONFIDENT = ProbeQuestion(
    question_id="mot_probe_01",
    purpose=_TIPPING_POINT_PURPOSE,
    template=(
        "Your answers put {first} and {second} at the top of what drives you at "
        "work. Think of a recent decision where you had to pick between two "
        "reasonable options — what actually tipped it?"
    ),
)

_TIPPING_POINT_PAIR = ProbeQuestion(
    question_id="mot_probe_01",
    purpose=_TIPPING_POINT_PURPOSE + " Names the pair, in no order.",
    template=(
        "Your answers lean towards {first} and {second}, close enough together "
        "that neither one is clearly ahead. Think of a recent decision where you "
        "had to pick between two reasonable options — what actually tipped it?"
    ),
)

_TIPPING_POINT_LEADER = ProbeQuestion(
    question_id="mot_probe_01",
    purpose=_TIPPING_POINT_PURPOSE + " Names the leader only.",
    template=(
        "Your answers put {first} at the top of what drives you at work. Think "
        "of a recent decision where you had to pick between two reasonable "
        "options — what actually tipped it?"
    ),
)

_TIPPING_POINT_OPEN = ProbeQuestion(
    question_id="mot_probe_01",
    purpose=(
        _TIPPING_POINT_PURPOSE + " Names nothing: on this result the ranking is "
        "tiebreak order, so the question has to start from the person."
    ),
    template=(
        "Your answers here do not settle what drives you most, so we would "
        "rather ask than guess. Think of a recent decision where you had to pick "
        "between two reasonable options — what actually tipped it?"
    ),
)

# ── Question 2: the same in every form, because it names nothing ──────────────
_WHAT_WAS_MISSING = ProbeQuestion(
    question_id="mot_probe_02",
    purpose=(
        "Finds the true leader by absence: what was missing when everything "
        "measurable went right. Strongest single signal in the segment, and the "
        "one question every form can ask unchanged — the person supplies the "
        "value, so there is nothing here for us to get wrong."
    ),
    template=(
        "Tell me about a stretch of work that went well by every outside "
        "measure and still left you flat. What was missing?"
    ),
)

# ── Question 3: the trade between the leaders ─────────────────────────────────
_TRADE_BETWEEN_PAIR = ProbeQuestion(
    question_id="mot_probe_03",
    purpose=(
        "Forces the ordering between the top two, which the item scores often "
        "cannot separate. Safe in both the confident and the unordered case: it "
        "names the pair and asks the person which way it goes."
    ),
    template=(
        "When {first} and {second} pull against each other on the same project, "
        "which one gives way? An example would help more than a rule."
    ),
)

_TRADE_SECOND_PLACE = ProbeQuestion(
    question_id="mot_probe_03",
    purpose=(
        "Tests the second slot rather than the ordering: the leader is clear, "
        "but the runner-up is level with what follows it, so the person is asked "
        "whether it belongs there at all."
    ),
    template=(
        "After {first}, your answers cannot separate {second} from what comes "
        "next. Does {second} belong in second place for you — and when the two "
        "of them pull against each other on a project, which gives way?"
    ),
)

_TRADE_OPEN = ProbeQuestion(
    question_id="mot_probe_03",
    purpose=(
        "The blank-page version of the trade question. Asks for the same thing — "
        "which of two things they care about gives way — without supplying the "
        "two, because this result has not established them."
    ),
    template=(
        "When two things you care about pull against each other on the same "
        "project, which one gives way? An example would help more than a rule."
    ),
)

# ── Question 4: the bottom of the ranking ─────────────────────────────────────
# Which variant is used is decided separately from the top-of-ranking hedging
# (see :func:`probe_form`), because a clear top two says nothing at all about
# whether the last two places are separable.
_BOTTOM_PURPOSE = (
    "Checks the bottom of the ranking — the position that makes pair guides "
    "useful — and gives the person a chance to reject it outright."
)

_BOTTOM_NAMED = ProbeQuestion(
    question_id="mot_probe_04",
    purpose=_BOTTOM_PURPOSE,
    template=(
        "Your answers put {last} last. Does that sound right, and if so, where "
        "does it cost you — what do you have to remind yourself to care about?"
    ),
)

_BOTTOM_HEDGED = ProbeQuestion(
    question_id="mot_probe_04",
    purpose=(
        _BOTTOM_PURPOSE + " Used when the last two places are level, so which of "
        "them is last is tiebreak order rather than a finding."
    ),
    template=(
        "Your answers put {last} at the bottom, but only just — it is level with "
        "the one above it. Does {last} sound like the one that gives way for "
        "you, and if so, where does that cost you?"
    ),
)

_BOTTOM_OPEN = ProbeQuestion(
    question_id="mot_probe_04",
    purpose=(
        _BOTTOM_PURPOSE + " Names nothing, for results where the ranking has no "
        "established bottom to name."
    ),
    template=(
        "Is there something the people around you clearly care about at work "
        "that you have to remind yourself to care about?"
    ),
)


#: The four questions, per form. Always four, always in this order, always the
#: same four ``question_id``s — a form changes what a question may claim, never
#: what it is asking about.
PROBE_FORMS: Mapping[ProbeForm, tuple[ProbeQuestion, ...]] = {
    "confident": (
        _TIPPING_POINT_CONFIDENT,
        _WHAT_WAS_MISSING,
        _TRADE_BETWEEN_PAIR,
        _BOTTOM_NAMED,
    ),
    "pair_unordered": (
        _TIPPING_POINT_PAIR,
        _WHAT_WAS_MISSING,
        _TRADE_BETWEEN_PAIR,
        _BOTTOM_NAMED,
    ),
    "second_place_hedged": (
        _TIPPING_POINT_LEADER,
        _WHAT_WAS_MISSING,
        _TRADE_SECOND_PLACE,
        _BOTTOM_NAMED,
    ),
    "open": (
        _TIPPING_POINT_OPEN,
        _WHAT_WAS_MISSING,
        _TRADE_OPEN,
        _BOTTOM_OPEN,
    ),
}

#: Every :attr:`MotivatorResult.probe_reason` code maps to exactly one form.
#: Deliberately exhaustive rather than defaulted: a new reason code with no
#: wording of its own should raise on the first interview that produces it, not
#: quietly inherit the confident wording — that inheritance is the whole bug this
#: structure exists to prevent.
_FORM_FOR_PROBE_REASON: Mapping[str, ProbeForm] = {
    "incomplete": "open",
    "level_profile": "open",
    "top_two_unordered": "pair_unordered",
    "runner_up_contested": "second_place_hedged",
}


def probe_form_name(result: MotivatorResult) -> ProbeForm:
    """Which wording this result has earned.

    ``"confident"`` only when :attr:`MotivatorResult.probe_reason` is ``None``,
    i.e. when the bank produced a ranking that stands on its own. Raises
    ``KeyError`` on an unmapped reason code, which is the intended behaviour —
    see :data:`_FORM_FOR_PROBE_REASON`.
    """
    reason = result.probe_reason
    if reason is None:
        return "confident"
    return _FORM_FOR_PROBE_REASON[reason]


def probe_form(result: MotivatorResult) -> tuple[ProbeQuestion, ...]:
    """The four probe questions, worded for what this result can honestly claim.

    Two independent decisions: the form covers the top of the ranking, and the
    bottom-of-ranking question is then swapped for its hedged variant when the
    last two places are level with each other. They are separate because they
    fail separately — a fifty-point gap between first and second tells you
    nothing about whether fifth and sixth are distinguishable.
    """
    form = probe_form_name(result)
    questions = PROBE_FORMS[form]
    if form != "open" and result.bottom_is_contested:
        questions = questions[:-1] + (_BOTTOM_HEDGED,)
    return questions


def probe_for(result: MotivatorResult) -> tuple[str, ...]:
    """The probe questions, rendered for this person's ranking.

    The one entry point the interview should call. It exists in this shape
    because the previous one rendered a fixed tuple of confident templates for
    everybody, which stated a fabricated ranking as fact to every person whose
    answers did not produce one.
    """
    return tuple(question.render(result) for question in probe_form(result))

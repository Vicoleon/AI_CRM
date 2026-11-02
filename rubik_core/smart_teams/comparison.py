"""Smart Teams ST-7 — the pairwise comparison engine and the pair guide.

Two scored profiles in, one document out, written TO one of the two people
ABOUT the other. The shapes are fixed by :mod:`comparison_contracts`; the extra
prose screens by :mod:`comparison_prompts`; the document devices are
``guide.py``'s, reused rather than reinvented.

## 1. What this engine is entitled to say, and what it is not

We hold **two self-reports, taken separately, neither person aware of the
other**. No 360, no peer input, no observed meeting, no manager rating, no
history of these two working together. That single fact decides the whole
design:

* A **synergy** is a dovetail between two authored fields — a fact about two
  texts. Producible.
* A **friction** is a collision between two *stated preferences* — also a fact
  about two texts. Producible, at a higher bar.
* A **predicted reaction** ("his directness could hit your approval-sensitive
  side") is a claim about how one person lands on another. **Not producible**,
  and not attempted. The source format this feature is modelled on is full of
  them; reproducing them would be this build's recorded failure mode — a
  document asserting a confidence the data does not carry.
* A **verdict on the pairing** ("approximately 75% synergy", "a very strong
  working pair") is unfalsifiable and is the direct on-ramp from "get me the
  best team" to "rank my employees". Refused by
  ``comparison_contracts.screen_no_ranking`` and
  ``comparison_prompts._PAIRING_VERDICT``, and never asked for.

So the section the source calls "Where you may be challenged" is called
:data:`FRICTION_LABEL` here — *where your stated preferences pull in different
directions* — and every friction it holds names both authored fields it came
from, in the document, not only in a log.

## 2. The joint gate, and the one thing that must never happen

``contracts.layer_of`` returns ``None`` past a closed gate. For two people that
compounds strictly: a dimension may be asserted only when the framework it reads
is reportable for BOTH. :class:`comparison_contracts.PairwiseRead` enforces it as
a type — a read whose joint gate is not ``both_open`` cannot hold a layer at all
— and :func:`read_pair` builds every read through it.

The failure this is all built to prevent, stated plainly: **comparing one real
value against a default manufactures a friction out of a missing answer.** That
is the worst thing this engine can do, worse than saying nothing, because it
tells one colleague to expect trouble from another on the strength of a question
the other never answered.

The second-worst thing is a renderer reading "no friction found" off a dimension
that was never assessed. That is why a refused dimension is
:class:`comparison_contracts.RefusedDimension`, which has no ``frictions``
attribute at all, and why :func:`_assess_dimension` refuses a dimension whenever
ANY framework feeding it is not comparable — an empty
:class:`~comparison_contracts.AssessedDimension` is only produced when every
framework the dimension could have read was readable.

## 3. Where each string in the document comes from

Four provenances, and they are screened differently on purpose:

1. **Authored pair copy** (headings, statements, asks, scripts, cards, notices)
   — written in this module. Screened by
   ``comparison_prompts.screen_pair_prose``, and pinned by
   ``test_every_authored_pair_string_passes_the_pair_screen``.
2. **Authored instrument copy** (the two sentences each claim quotes) — lifted
   verbatim from the instrument modules through an open joint gate. Screened at
   the point it enters, in :func:`_evidence`, with
   ``guide_prompts.screen_prose`` — the same place and the same call
   ``guide._tie_in`` makes for the strengths quote, and for the same reason: a
   span that enters unscreened is a span nothing checks.
3. **Model prose** — exactly ONE string per section, and it is not a sentence
   about either person. The model re-voices the authored *clause* saying why two
   answers are next to each other (:attr:`PairSection.lead`); it is never shown
   a display name or either quoted answer, and both of those render from
   authored copy with their owner attached (:func:`_authored_evidence`). So an
   owner swap is not screened for, it is unavailable. What the model does return
   is screened by ``comparison_prompts.screen_pair_lead`` — the pair rules in
   this document's name grammar, the owner-binding rule, and all four
   fact-containment rules in STRICT mode against the clause alone. Rejected
   strings fall back to the authored clause and are recorded in
   :attr:`PairGuide.fallbacks`. The heading is authored throughout; a dimension
   label is not prose and there is nothing for a model to add to it.
4. **The assembled document** — screened once by
   :func:`generate_pair_into_row`, with two span classes cut out first: the
   advisory notice (which contains the word the advancement pattern matches on,
   exactly as ``guide.generate_into_row`` documents) and the authored instrument
   sentences from provenance 2.

**That second exemption is deliberate and it is the one judgement call in this
module, so here is the whole argument.** The pair-only patterns are written
against sentence *shapes* the instruments legitimately use about a single
person: DISC's feedback copy says "they will hear the first as an attack on
their competence", a strengths theme says "their draft is better than most
people's final", another says "the process says starts outranking judgement".
Every one of those is authored, subject-approved, and already renders verbatim
into personal guides — and every one of them trips a pair pattern, which would
fail an entire pair guide for a sentence we wrote ourselves. The exempted set is
not "quoted text" in general (``guide.py`` removed exactly that exemption, for
good reason: it was hiding an unbounded transcript span from every check). It is
the finite set of instrument strings this document actually cites, each one
individually screened on entry, each one enumerable from
:attr:`PairGuide.authored_spans`. A test pins that the exemption set is exactly
that, and nothing else.

## 4. Consent

A pair guide is readable only when BOTH people have published their own
personal guide. That rule is ``authz.can_view_multi_subject_guide`` and it is
not re-derived here; :func:`published_by_subject` builds the map it takes, from
each subject's OWN ``personal_guide`` row.

Generation is a separate, STRICTER gate — :func:`refuse_generation` — because
generating writes one person's profile content into a cached row somebody else
may later read. Every subject, the requester included, must have published.
See that function's docstring for why the looser alternative was declined.

There is deliberately **no approve, no publish, and no RAG ingest for a pair
guide**:

* ``guide.approve`` compares the approver against ``document.subject_key``,
  which for a pair row is ``"a|b"`` and can never equal one person's address.
  That refusal is now deliberate rather than accidental — :func:`approve_pair`
  exists only to say so and to raise.
* ``published`` therefore stays false forever on a pair row, and readability
  comes from the two personal guides instead. That is the honest arrangement:
  one flag cannot record two consents.
* Nothing here ingests into RAG. ``guide.GUIDE_RAG_SLUG`` is ``"companywide"``
  and retrieval has no ACL in front of it, so a pair guide in the corpus would
  be answerable in Chat to readers ``can_view_multi_subject_guide`` refuses —
  and ``guide.rag_document_text`` would render an ``"a|b"`` subject key as a
  fake single subject besides. :data:`NO_RAG_REASON` states it; there is no
  function to disable.

## Advisory only

Nothing in here may frame either person as more or less capable, higher or lower
performing, or better or worse than the other. Nothing here is wired to
performance review, promotion, or compensation, and the document says so on
every page.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from rubik_core.brand import PRODUCT_NAME
from rubik_core.cost.usage import attribute_to
from rubik_core.database.smart_teams import (
    SmartTeamsDocument,
    SmartTeamsProfile,
    document_cache_keys,
)
from rubik_core.smart_teams import authz, contracts, guide, guide_prompts
from rubik_core.smart_teams import comparison_contracts as cc
from rubik_core.smart_teams import comparison_prompts as cp

# Two private imports from ``guide_prompts``, in one block with one reason: the
# overlap measure below decides whether two authored sentences are talking about
# the same thing, and the containment screen decides whether a model's word came
# from the fact. If those two used different word-folding they would disagree
# about what "the same word" is — the overlap would find a dovetail on a stem the
# screen then treats as novel. Same folding, one definition. ``guide_prompts``
# itself imports two privates from the instruments for the same reason.
from rubik_core.smart_teams.guide_prompts import _CONNECTIVE_WORDS, _stem
from rubik_core.smart_teams.instruments import (
    enneagram,
    motivators,
    strengths,
    type16,
)

logger = logging.getLogger(__name__)

__all__ = [
    "KIND_PAIR",
    "ComparisonError",
    "read_pair",
    "assess_pair",
    "PairSection",
    "PairPanel",
    "PairPanelColumn",
    "PairCard",
    "PairCompletenessLine",
    "PairGuide",
    "build_pair_guide",
    "render_markdown",
    "write_pair_prose",
    "cache_keys_for_pair",
    "current_pair_guide",
    "is_stale_pair",
    "ensure_pair_row",
    "generate_pair_into_row",
    "fail_row",
    "published_by_subject",
    "refuse_generation",
    "can_read_pair",
    "approve_pair",
    "NO_RAG_REASON",
]

#: ``st_documents.kind`` for a pair guide. Imported by the router and the job,
#: never re-typed: it is half of the cache key and the whole of the "is this row
#: mine to act on" check.
KIND_PAIR = "pair_guide"


class ComparisonError(RuntimeError):
    """A pair guide could not be built, generated, or read.

    Raised rather than returned, exactly as ``guide.GuideError`` is: every caller
    is an HTTP handler or a cubie-worker job and both have to tell the difference
    between "wrote a document" and "wrote nothing". A separate class from
    ``GuideError`` so a handler cannot accidentally report a pair refusal with the
    personal guide's copy.
    """


# ── Shared with the personal guide ───────────────────────────────────────────
# Same AI Cost bucket, same model key, same temperature, same attempt budget. A
# second spelling of any of them would open a second "Unattributed"-shaped hole
# in the AI Cost tab or quietly run this feature's one LLM call at a different
# temperature than the sibling it shares a prompt discipline with.

#: The AI Cost bucket. A STRING LITERAL and not ``guide.COST_FEATURE``, with the
#: drift closed by the assert below instead — ``test_smart_teams_conversation``
#: resolves every ``@attribute_to`` argument by parsing this module's AST, and it
#: refuses a key it cannot resolve to a literal or a module-level constant. That
#: refusal is right: a decorator argument nothing can read statically is a
#: decorator argument nobody can check against ``_FEATURE_LABELS``, which is how
#: a feature's spend lands in "Unattributed".
COST_FEATURE = "smart_teams"

assert COST_FEATURE == guide.COST_FEATURE, (
    "comparison.COST_FEATURE has drifted from guide.COST_FEATURE; two spellings "
    "of one bucket is a second Unattributed-shaped hole in the AI Cost tab"
)

PROSE_MODEL_KEY = guide.PROSE_MODEL_KEY
PROSE_TEMPERATURE = guide.PROSE_TEMPERATURE
PROSE_ATTEMPTS = guide.PROSE_ATTEMPTS

#: Section caps, from the measured source format. Ceilings only: the row and
#: section counts are DATA-DRIVEN from 0 up to these, and nothing is ever padded
#: to reach them. Eight rows was one author's supply of overlaps for one specific
#: pair, not a format requirement, and zero rows is a legitimate output.
MAX_SYNERGY_ROWS = 8
MAX_FRICTION_SECTIONS = 6
MAX_CARDS = 3

#: The symmetric panel's bullet bounds. Equal counts on both sides is enforced by
#: :class:`PairPanel`; unequal columns read as "one of them contributes more",
#: which is the judgement this whole feature must not make. Below the floor the
#: panel is omitted rather than shown thin.
PANEL_MIN_BULLETS = 3
PANEL_MAX_BULLETS = 5

#: Why a pair guide is never RAG-ingested. Stated as a constant so the reason
#: travels with the decision instead of living in a review thread.
NO_RAG_REASON = (
    "A pair guide is never put in the RAG corpus. The guide corpus is "
    "companywide and retrieval has no per-document ACL in front of it, so a "
    "pair guide there would be answerable in Chat to readers who are refused "
    "it in the app — including before either person has published."
)


def _fold(email: object) -> str:
    return str(email or "").strip().lower()


# ── Reading two profiles ─────────────────────────────────────────────────────


def read_pair(
    profile_a: SmartTeamsProfile, profile_b: SmartTeamsProfile
) -> dict[str, cc.PairwiseRead]:
    """One :class:`~comparison_contracts.PairwiseRead` per framework, gate applied.

    Built on ``guide.read_layers`` rather than on the raw columns, so an
    off-contract payload written by a seed script or an older deploy is handled
    identically in both documents: logged, and treated as absent. Re-reading the
    columns here would be a second, thinner implementation of that recovery, and
    it would drift.

    ``profile_a`` is the person the guide is written TO. The order is load-bearing
    and is never sorted — ``(A, B)`` and ``(B, A)`` are two different documents,
    exactly as ``database.smart_teams.subject_key_for`` says for this kind.
    """
    email_a = _fold(profile_a.user_email)
    email_b = _fold(profile_b.user_email)
    if not email_a or not email_b:
        raise ComparisonError("a pair comparison needs two identified subjects")
    if email_a == email_b:
        raise ComparisonError(
            f"a pair comparison needs two people; both sides are {email_a!r}"
        )
    layers_a = guide.read_layers(profile_a)
    layers_b = guide.read_layers(profile_b)

    reads: dict[str, cc.PairwiseRead] = {}
    for framework in contracts.FRAMEWORKS:
        read_a = layers_a[framework]
        read_b = layers_b[framework]
        both_open = read_a.reportable and read_b.reportable
        reads[framework] = cc.PairwiseRead(
            framework=framework,
            subject_a=email_a,
            subject_b=email_b,
            verdict_a=read_a.verdict,
            verdict_b=read_b.verdict,
            # The joint gate, applied here and re-asserted by __post_init__: a
            # one-sided read drops the open subject's layer on the floor, so
            # there is no path from this dict to a value we may not compare.
            layer_a=read_a.layer if both_open else None,
            layer_b=read_b.layer if both_open else None,
        )
    return reads


# ── Layer field access ───────────────────────────────────────────────────────
# One accessor per (framework, field) that a rule below actually reads. No loop
# over frameworks and no generic getter: a renamed instrument field has to break
# a test here rather than evaluate falsy and quietly delete a section. Same
# reasoning as ``contracts``' six hand-written verdicts and ``guide.py``'s four
# hand-written candidate builders.
#
# The two leaf readers — ``guide._text`` and ``guide._strings`` — are reached by
# attribute rather than reimplemented. They answer "a non-empty string at this
# path inside a layer dict, or None", and a second version of that would
# eventually disagree with the personal guide about whether a whitespace-only
# field counts as present, which is the kind of difference nobody notices until
# one document has a section the other does not.


def _disc_style(layer: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """The assigned work style's ``as_dict()``, or ``None``."""
    if not isinstance(layer, Mapping):
        return None
    assignment = layer.get("assignment")
    style = assignment.get("style") if isinstance(assignment, Mapping) else None
    return style if isinstance(style, Mapping) else None


def _disc_text(layer: Mapping[str, Any] | None, field_name: str) -> str | None:
    """One single-string DISC field — ``under_stress``, ``feedback``, ``identity``."""
    style = _disc_style(layer)
    return guide._text(style, field_name) if style else None


def _disc_list(layer: Mapping[str, Any] | None, field_name: str) -> tuple[str, ...]:
    """One list-valued DISC field — ``communication``, ``energized_by``, ``drained_by``."""
    style = _disc_style(layer)
    return guide._strings(style, field_name) if style else ()


def _theme_specs(
    layer: Mapping[str, Any] | None, *, limit: int = 3
) -> tuple[strengths.ThemeSpec, ...]:
    """The person's top themes as authored specs, in the extractor's own order.

    The one ordering in this feature that means something — it is the
    instrument's ranking, not a presentation choice — and it is never re-ranked
    here.
    """
    if not isinstance(layer, Mapping):
        return ()
    out: list[strengths.ThemeSpec] = []
    for theme in layer.get("themes") or []:
        if len(out) >= limit:
            break
        if not isinstance(theme, Mapping):
            continue
        theme_id = theme.get("theme_id")
        if not isinstance(theme_id, str):
            continue
        try:
            out.append(strengths.spec_for(theme_id))
        except KeyError:
            logger.error(
                "[smart-teams] comparison: unknown strengths theme %r", theme_id
            )
    return tuple(out)


def _motivator_specs(
    layer: Mapping[str, Any] | None,
) -> tuple[tuple[motivators.MotivatorSpec, ...], motivators.MotivatorSpec | None]:
    """``(top_two_specs, bottom_spec_or_None)``.

    ``bottom`` is used only when the instrument says it is not contested, the
    same rule ``guide._motivator_candidates`` applies: a contested bottom axis is
    a tie-break masquerading as a finding, and a friction built on one would be
    asserting a collision that the scoring itself declined to call.
    """
    if not isinstance(layer, Mapping):
        return ((), None)
    top: list[motivators.MotivatorSpec] = []
    for axis in (layer.get("top_two") or [])[:2]:
        if not isinstance(axis, str):
            continue
        try:
            top.append(motivators.spec_for(axis))  # type: ignore[arg-type]
        except KeyError:
            logger.error("[smart-teams] comparison: unknown motivator axis %r", axis)
    bottom_spec: motivators.MotivatorSpec | None = None
    bottom = layer.get("bottom")
    if (
        isinstance(bottom, str)
        and bottom
        and not bool(layer.get("bottom_is_contested"))
    ):
        try:
            bottom_spec = motivators.spec_for(bottom)  # type: ignore[arg-type]
        except KeyError:
            bottom_spec = None
    return (tuple(top), bottom_spec)


def _enneagram_spec(layer: Mapping[str, Any] | None) -> enneagram.TypeSpec | None:
    """The person's type spec, for its AUTHORED fields only.

    The type label and number are never rendered by a comparison —
    ``Dimension.may_name_derived_type`` is ``False`` for every dimension and
    ``guide_prompts._ENNEAGRAM_ASSERTION`` refuses an asserted type in prose. What
    this is for is ``needs_from_colleagues`` and ``workplace_signature``, which are
    sentences about how to work with somebody and carry no type in them.
    """
    if not isinstance(layer, Mapping):
        return None
    number = layer.get("type_number")
    if not isinstance(number, int):
        return None
    try:
        return enneagram.spec_for(number)
    except KeyError:
        logger.error("[smart-teams] comparison: enneagram type %r has no spec", number)
        return None


#: ``type16.Confidence`` mapped onto ``contracts.Confidence``. A coin-flip letter
#: carries no information about the person, so it maps to ``"none"`` — below both
#: claim floors, which is how a letter that is four coin flips produces neither a
#: synergy nor a friction instead of producing a hedged one.
_AXIS_CONFIDENCE: Mapping[str, contracts.Confidence] = {
    type16.Confidence.CLEAR.value: "high",
    type16.Confidence.LEANING.value: "moderate",
    type16.Confidence.COIN_FLIP.value: "none",
}

assert set(_AXIS_CONFIDENCE) == {c.value for c in type16.Confidence}, (
    "comparison._AXIS_CONFIDENCE has drifted from type16.Confidence; an "
    "unmapped confidence would KeyError mid-document"
)


@dataclass(frozen=True)
class _AxisCall:
    """One person's letter on one four-letter axis, with its authored meaning.

    ``field`` is ``"high_meaning"`` or ``"low_meaning"`` — the ``AxisSpec`` field
    the meaning came from. That is what makes an opposite-letter friction legal
    under :class:`comparison_contracts.Friction`'s two-distinct-operands rule:
    two people on opposite ends of an axis cite two DIFFERENT authored fields, so
    the collision is between two texts rather than one field colliding with
    itself.
    """

    axis: str
    letter: str
    field: str
    meaning: str
    confidence: contracts.Confidence


def _axis_calls(layer: Mapping[str, Any] | None) -> dict[str, _AxisCall]:
    """Each measured axis, keyed by axis name. Coin flips included, marked ``"none"``."""
    if not isinstance(layer, Mapping):
        return {}
    out: dict[str, _AxisCall] = {}
    for entry in layer.get("axes") or []:
        if not isinstance(entry, Mapping):
            continue
        axis = entry.get("axis")
        letter = entry.get("letter")
        confidence = entry.get("confidence")
        if not isinstance(axis, str) or not isinstance(letter, str):
            continue
        try:
            spec = type16.AXIS_SPECS[type16.Axis(axis)]
        except (KeyError, ValueError):
            logger.error("[smart-teams] comparison: unknown type16 axis %r", axis)
            continue
        is_high = letter.upper() == spec.high_letter
        out[axis] = _AxisCall(
            axis=axis,
            letter=letter.upper(),
            field="high_meaning" if is_high else "low_meaning",
            meaning=spec.high_meaning if is_high else spec.low_meaning,
            confidence=_AXIS_CONFIDENCE.get(str(confidence), "none"),
        )
    return out


# ── The overlap measure ──────────────────────────────────────────────────────


#: Shared distinctive stems needed before two authored sentences count as
#: talking about the same thing.
#:
#: A count and not a proportion, for the reason ``guide_prompts`` gives about its
#: own floor: the thing being counted is rare by construction. Two instrument
#: sentences about different subjects share nothing but function words, which are
#: excluded; two about the same subject share the subject's own nouns.
#:
#: Chosen from the measurement in :data:`_OVERLAP_CALIBRATION`, reproducible by
#: ``backend/tests/test_smart_teams_comparison.py
#: ::test_the_overlap_floor_separates_real_dovetails_from_noise``.
MIN_SHARED_STEMS = 2

#: Shortest stem that counts. Three-letter stems after folding are almost all
#: function words that ``_CONNECTIVE_WORDS`` happens not to list.
_MIN_STEM_LENGTH = 4

_OVERLAP_CALIBRATION = """
Measured over the whole real instrument corpus, every ordered pair, with the
folding in ``shared_stems`` — function words and stems under 4 characters
excluded. Counts are pairs at each number of shared stems:

  rule                     pairs   0 shared   1     2    3+
  disc.drained_by  x disc.energized_by     960       933    27    0    0
  disc.communication x disc.drained_by    1440      1394    43    3    0
  disc.feedback    x disc.drained_by       480       465    15    0    0
  disc.under_stress x strengths.overused   448       348    86   12    2
  strengths.ask_for x strengths.definition 756       690    61    5    0

Threshold 1 was rejected on inspection of the hits, not on the counts. Every
sampled 1-shared pair in the first table is a coincidence on a generic noun:
"Open conflict left unresolved in the room" against "Winning over a sceptical
room" shares "room"; "Repeating the same correction to the same person" against
"Being the person others come to when something has to hold" shares "person".
Twenty-seven such pairs is twenty-seven manufactured frictions, which is the
one thing this engine must never produce.

Threshold 2 is the first value at which a sampled hit reads as a real overlap:
"Expect to be pushed on your logic" against "Being pushed to commit before they
have checked" shares ``push`` and ``check``. It is also the value at which the
two 3-shared hits in the fourth table are the only ones that look wrong on
inspection, and they are gated separately by MIN_FRICTION_CONFIDENCE.

**The yield is honestly low, and that is worth stating plainly rather than
discovering later.** At threshold 2 the drain-x-energy and feedback-x-drain
rules do not fire on ANY pair of the seventeen work styles as they are authored
today. They are kept because they are correct rules over a corpus that changes,
not because they are carrying the section. The rules that actually produce
content are the STRUCTURAL ones, which involve no lexical guessing at all: the
motivators axis one person ranks top and the other ranks bottom, the four-letter
axes where the two land on opposite letters, a shared top motivator, and
complementary strengths clusters. A pair guide built mostly from those is the
honest output of two self-reports.

This is a similarity heuristic over two authored texts, not a measurement of two
people. Nothing downstream may report it as a score, and nothing can — there is
no field on Synergy or Friction that could hold one.
"""


def shared_stems(text_a: str, text_b: str) -> tuple[str, ...]:
    """The distinctive word stems two authored sentences have in common.

    Exposed because the count is the whole argument for :data:`MIN_SHARED_STEMS`,
    and a reviewer has to be able to see which words a section turned on. Folded
    with ``guide_prompts._stem`` so this and the containment screen agree about
    what one word is.
    """

    def _distinctive(text: str) -> set[str]:
        out: set[str] = set()
        for match in guide_prompts._WORD.finditer(text or ""):
            stem = _stem(match.group(0))
            if len(stem) < _MIN_STEM_LENGTH or stem in _CONNECTIVE_WORDS:
                continue
            out.add(stem)
        return out

    return tuple(sorted(_distinctive(text_a) & _distinctive(text_b)))


def _overlaps(text_a: str, text_b: str) -> bool:
    return len(shared_stems(text_a, text_b)) >= MIN_SHARED_STEMS


# ── Authored pair copy ───────────────────────────────────────────────────────
# Every string below is authored here, shown to nobody for approval, and makes no
# claim about either person that is not "these two authored sentences say this".
# All of it is screened by ``comparison_prompts.screen_pair_prose`` in the tests.

TITLE_TEMPLATE = "Working With {name_a} & {name_b}"

DECK = (
    "A short guide to working with each other, written from two Work Style "
    "Interviews. It is about what each of you asked for — not about how well "
    "either of you does the work."
)

FRAMING_OPENER = (
    "{name_a}, this is what your answers and {name_b}'s answers say when they "
    "are put next to each other."
)

FRAMING_COUNTS = (
    f"{PRODUCT_NAME} found "
    "{synergies} place(s) where the two sets of answers line up and "
    "{frictions} where they ask for different things."
)

FRAMING_NO_OBSERVATION = (
    "Both of you answered separately, months apart, without the other in mind. "
    "Nothing in here is an observation of the two of you working together, so "
    "read it as a starting point for a conversation rather than as a finding."
)

PROVENANCE_LABEL = "What this is built from"

PROVENANCE_BODY = (
    "Two Work Style Interviews, scored. For each part of the profile, this guide "
    "says something only when that part is reported for BOTH of you — where it is "
    "reported for one, the guide leaves it out rather than comparing one answer "
    "against a blank. The list at the end says which is which."
)

SYNERGY_LABEL = "Where you line up"
SYNERGY_COLUMNS = ("AREA", "WHAT EACH OF YOU SAID")

SYNERGY_NONE = (
    "Nothing in the parts of the profile that are reported for both of you lines "
    "up closely enough to be worth writing down. That is a thin result rather "
    "than a bad one."
)

FRICTION_LABEL = "Where your stated preferences pull in different directions"

#: The authored hedge. It carries the honesty this document owes, and it is
#: authored rather than model-written on purpose: a hedge the model wrote is a
#: hedge the model can drop.
FRICTION_HYPOTHESIS_NOTE = (
    "Each of these is a hypothesis, not a finding. It comes from two answers "
    f"given separately, and {PRODUCT_NAME} has never watched the two of you work. Read "
    "each one as 'this could come up', check it against what actually happens, "
    "and throw out the ones that do not match."
)

FRICTION_NO_RANK_NOTE = (
    "These are in no particular order. Nothing here says which one matters most "
    "— that would be a guess dressed up as a ranking."
)

FRICTION_NONE = (
    "In the parts of the profile reported for both of you, nothing the two of you "
    f"asked for asks for opposite things. That is a real result: {PRODUCT_NAME} looked."
)

CITATION_TEMPLATE = "From {name_a}'s {label_a} and {name_b}'s {label_b}."

#: Used when both sides cite the same kind of answer, which is most of the
#: four-letter rows. "From Alex's answer about X and Blair's answer about X" reads
#: as a mistake; it is not, but a reader cannot tell.
CITATION_SHARED = "From {name_a}'s and {name_b}'s {label}."

ASK_LABEL_A = "Your side"
ASK_LABEL_B = "{name_b}'s side"

SCRIPT_LABEL = "Something to say out loud"

PANEL_LABEL = "What each of you asks for"

PANEL_DISCLAIMER = (
    "Both columns are the same question asked of each of you, and they are the "
    "same length on purpose. This is not a split of responsibility, not a split "
    "of who decides, and not a statement that either list is worth more than the "
    "other."
)

CARDS_LABEL = "Three things worth agreeing on"

CARDS_INTRO = (
    "Each of these reconciles two things the two of you asked for. They are "
    "listed in the order the sections above are, which is not an order of "
    "importance."
)

CLOSING_LABEL = "If you do one thing with this"

CLOSING_BODY = (
    "Pick one line above, say it out loud to each other this week, and find out "
    "whether it is true. That is worth more than the rest of this document, "
    "because the rest of it is two people describing themselves and this is the "
    "two of you checking."
)

COMPLETENESS_LABEL = "What this guide could and could not read"

COMPLETENESS_INTRO = (
    "Each part of the profile is used here only when it is reported for both of "
    "you. Where it is reported for one, this guide says nothing from it — and "
    "says nothing about what the one answer was, either."
)

COMPLETENESS_BOTH = "Reported for both of you, and used here."
COMPLETENESS_ONE = "Reported for one of you, so this guide says nothing from it."
COMPLETENESS_NEITHER = (
    "Not reported for either of you, so this guide says nothing from it."
)
COMPLETENESS_STILL_TO_FINISH = "Still to finish for: {names}."

REFUSALS_LABEL = "What this guide cannot speak to"

REFUSALS_INTRO = (
    f"These are the questions {PRODUCT_NAME} would have answered if both profiles had the "
    "part behind them. They are listed so that a missing section is not read as "
    "'nothing found here'."
)

SHORTFALL_LABEL = "Not enough reported for both of you yet"

SHORTFALL_BODY = (
    f"There is not enough that is reported for BOTH of you for {PRODUCT_NAME} to write a "
    "working-relationship guide, so it has not written one. That is the honest "
    "outcome rather than a failure — a guide built by comparing one person's "
    "answer against a blank would describe a tension neither of you has, and one "
    "of you would read it about the other. The list below is what would change "
    "it."
)


# ── Claim rules ──────────────────────────────────────────────────────────────
# Every rule below is one authored template plus a named field on each side, and
# every one of them is a statement about two texts. There is deliberately no rule
# that predicts a reaction, no rule that grades the pairing, and no rule whose
# remedy falls on one of the two.


def _evidence(
    subject: str, framework: str, field_name: str, text: str | None
) -> cc.Evidence | None:
    """One authored field as :class:`~comparison_contracts.Evidence`, screened.

    Screened HERE, at the point it enters the document, with the same call
    ``guide._tie_in`` makes on the strengths quote and for the same reason: a
    span that enters unchecked is a span nothing checks, and this is the one
    place every instrument string in a pair guide passes through. A string that
    does not pass costs its claim, not the document.

    The generic screen and not the pair screen: the pair patterns are written
    against sentence shapes the instruments legitimately use about ONE person, so
    running them here would refuse our own authored copy. That trade, and the
    exemption it implies for the assembled body, is argued in §3 of the module
    docstring.
    """
    if not text or not str(text).strip():
        return None
    body = str(text).strip()
    reason = guide_prompts.screen_prose(body, max_words=10_000)
    if reason:
        logger.warning(
            "[smart-teams] comparison: dropping %s.%s for %s — it did not pass "
            "the prose screen (%s)",
            framework,
            field_name,
            subject,
            reason,
        )
        return None
    try:
        return cc.Evidence(
            subject_email=subject,
            framework=framework,
            field=field_name,
            text=body,
        )
    except cc.ComparisonError:
        logger.exception(
            "[smart-teams] comparison: %s.%s is not citable evidence",
            framework,
            field_name,
        )
        return None


@dataclass(frozen=True)
class _Candidate:
    """A claim the rules produced, plus everything the renderer needs.

    The claim itself (:attr:`synergy` / :attr:`friction`) is the contract object;
    the rest is presentation the contract deliberately does not carry — a label
    for the table's left column, the human-readable citation, the authored script
    line. Keeping them out of the contract types is what stops a renderer from
    finding a ``severity`` field to sort on.
    """

    dimension: str
    label: str
    statement: str
    evidence: tuple[cc.Evidence, ...]
    confidence: contracts.Confidence
    field_label_a: str
    field_label_b: str
    ask_of_a: str = ""
    ask_of_b: str = ""
    script: str = ""


#: Human-readable names for the authored fields a claim can cite. Used in the
#: in-document citation, so a reader can see which two answers a section came
#: from without being shown a field name.
FIELD_LABELS: Mapping[tuple[str, str], str] = {
    ("disc", "communication"): "answer about how to keep them posted",
    ("disc", "energized_by"): "answer about what gives them energy",
    ("disc", "drained_by"): "answer about what costs them energy",
    ("disc", "under_stress"): "answer about a bad week",
    ("disc", "feedback"): "answer about how to give them feedback",
    ("strengths", "ask_for"): "answer about what to come to them for",
    ("strengths", "definition"): "answer about what they reliably contribute",
    ("strengths", "cluster"): "answer about what kind of work they are strongest in",
    ("strengths", "overused"): "answer about that strength overapplied",
    ("strengths", "under_pressure"): "answer about that strength under pressure",
    ("motivators", "high_read"): "answer about what makes work worth doing",
    ("motivators", "low_read"): "answer about what moves them least",
    ("enneagram", "needs_from_colleagues"): "answer about what helps them most",
    ("type16", "high_meaning"): "answer about where they start from",
    ("type16", "low_meaning"): "answer about where they start from",
}


def _field_label(framework: str, field_name: str) -> str:
    return FIELD_LABELS.get((framework, field_name), f"{framework} answer")


def _candidate(
    *,
    dimension: str,
    label: str,
    statement: str,
    confidence: contracts.Confidence,
    subject_a: str,
    framework_a: str,
    field_a: str,
    text_a: str | None,
    subject_b: str,
    framework_b: str,
    field_b: str,
    text_b: str | None,
    ask_of_a: str = "",
    ask_of_b: str = "",
    script: str = "",
    label_a: str = "",
    label_b: str = "",
) -> _Candidate | None:
    """Assemble a candidate, or ``None`` if either side's evidence did not survive.

    ``label_a`` / ``label_b`` override :data:`FIELD_LABELS` for the in-document
    citation. Only the four-letter rules use them, because that instrument's two
    fields (``high_meaning`` / ``low_meaning``) are the same answer on four
    different questions, and a citation that says "your answer about where you
    start from" under all four is wrong about three of them.
    """
    evidence_a = _evidence(subject_a, framework_a, field_a, text_a)
    evidence_b = _evidence(subject_b, framework_b, field_b, text_b)
    if evidence_a is None or evidence_b is None:
        return None
    return _Candidate(
        dimension=dimension,
        label=label,
        statement=statement,
        evidence=(evidence_a, evidence_b),
        confidence=confidence,
        field_label_a=label_a or _field_label(framework_a, field_a),
        field_label_b=label_b or _field_label(framework_b, field_b),
        ask_of_a=ask_of_a,
        ask_of_b=ask_of_b,
        script=script,
    )


# ── Synergy rules ────────────────────────────────────────────────────────────

SYN_SAME_COMMS = "You each asked for the same kind of update, in your own words."
SYN_SAME_FEEDBACK = "You each described wanting feedback delivered the same way."
SYN_SAME_ENERGY = "The same kind of work shows up on both of your lists as energising."
SYN_SAME_MOTIVATOR = "The same thing makes the work feel worth doing to both of you."
SYN_ASK_MEETS_CONTRIBUTION = (
    "What one of you says to come to them for is what the other says they "
    "reliably contribute."
)
SYN_NEEDS_MET = (
    "What one of you says helps them most is close to what the other asks "
    "colleagues to do."
)
SYN_DIFFERENT_CLUSTERS = (
    "Your strongest themes sit in different kinds of work, so between you the "
    "two of you cover more of the job than either would alone."
)


def _synergy_candidates(
    reads: Mapping[str, cc.PairwiseRead],
) -> list[_Candidate]:
    """Every dovetail the two profiles support, in canonical dimension order.

    Each rule reads ONE framework whose joint gate is open, or two, and every one
    of them is a lexical or structural overlap between two authored fields. No
    rule predicts anything.
    """
    out: list[_Candidate] = []
    disc_read = reads["disc"]
    strengths_read = reads["strengths"]
    motivators_read = reads["motivators"]
    enneagram_read = reads["enneagram"]
    type16_read = reads["type16"]
    subject_a = disc_read.subject_a
    subject_b = disc_read.subject_b

    # 1. communication_pace — the same kind of update, both sides DISC.
    if disc_read.comparable:
        comms_a = _disc_list(disc_read.layer_a, "communication")
        comms_b = _disc_list(disc_read.layer_b, "communication")
        for text_a in comms_a[:3]:
            match = next((t for t in comms_b[:3] if _overlaps(text_a, t)), None)
            if match is None:
                continue
            candidate = _candidate(
                dimension="communication_pace",
                label="How to keep each other posted",
                statement=SYN_SAME_COMMS,
                confidence=disc_read.joint_confidence,
                subject_a=subject_a,
                framework_a="disc",
                field_a="communication",
                text_a=text_a,
                subject_b=subject_b,
                framework_b="disc",
                field_b="communication",
                text_b=match,
            )
            if candidate is not None:
                out.append(candidate)
                break

        # 2. feedback_exchange — the same delivery.
        feedback_a = _disc_text(disc_read.layer_a, "feedback")
        feedback_b = _disc_text(disc_read.layer_b, "feedback")
        if feedback_a and feedback_b and _overlaps(feedback_a, feedback_b):
            candidate = _candidate(
                dimension="feedback_exchange",
                label="Feedback",
                statement=SYN_SAME_FEEDBACK,
                confidence=disc_read.joint_confidence,
                subject_a=subject_a,
                framework_a="disc",
                field_a="feedback",
                text_a=feedback_a,
                subject_b=subject_b,
                framework_b="disc",
                field_b="feedback",
                text_b=feedback_b,
            )
            if candidate is not None:
                out.append(candidate)

        # 3. energy_and_drain — the same fuel.
        energy_a = _disc_list(disc_read.layer_a, "energized_by")
        energy_b = _disc_list(disc_read.layer_b, "energized_by")
        for text_a in energy_a[:2]:
            match = next((t for t in energy_b[:2] if _overlaps(text_a, t)), None)
            if match is None:
                continue
            candidate = _candidate(
                dimension="energy_and_drain",
                label="What gives you both energy",
                statement=SYN_SAME_ENERGY,
                confidence=disc_read.joint_confidence,
                subject_a=subject_a,
                framework_a="disc",
                field_a="energized_by",
                text_a=text_a,
                subject_b=subject_b,
                framework_b="disc",
                field_b="energized_by",
                text_b=match,
            )
            if candidate is not None:
                out.append(candidate)
                break

    # 4. motivator_alignment — a shared top motivator. Structural, not lexical:
    #    the two rankings name the same axis.
    if motivators_read.comparable:
        top_a, _bottom_a = _motivator_specs(motivators_read.layer_a)
        top_b, _bottom_b = _motivator_specs(motivators_read.layer_b)
        names_b = {spec.motivator for spec in top_b}
        shared = next((spec for spec in top_a if spec.motivator in names_b), None)
        if shared is not None:
            candidate = _candidate(
                dimension="motivator_alignment",
                label="What makes the work worth doing",
                statement=SYN_SAME_MOTIVATOR,
                confidence=motivators_read.joint_confidence,
                subject_a=subject_a,
                framework_a="motivators",
                field_a="high_read",
                text_a=shared.high_read,
                subject_b=subject_b,
                framework_b="motivators",
                field_b="high_read",
                text_b=shared.high_read,
            )
            if candidate is not None:
                out.append(candidate)

    # 5. contribution_mix — what one asks for is what the other contributes.
    if strengths_read.comparable:
        themes_a = _theme_specs(strengths_read.layer_a)
        themes_b = _theme_specs(strengths_read.layer_b)
        for ask_spec in themes_a:
            match = next(
                (
                    spec
                    for spec in themes_b
                    if _overlaps(ask_spec.ask_for, spec.definition)
                ),
                None,
            )
            if match is None:
                continue
            candidate = _candidate(
                dimension="contribution_mix",
                label="What to hand each other",
                statement=SYN_ASK_MEETS_CONTRIBUTION,
                confidence=strengths_read.joint_confidence,
                subject_a=subject_a,
                framework_a="strengths",
                field_a="ask_for",
                text_a=ask_spec.ask_for,
                subject_b=subject_b,
                framework_b="strengths",
                field_b="definition",
                text_b=match.definition,
            )
            if candidate is not None:
                out.append(candidate)
                break

        # 5b. contribution_mix, STRUCTURALLY — the two people's leading themes
        #     sit in different clusters. No lexical guessing: the clusters are
        #     the taxonomy's own, and the authored text is
        #     ``strengths.CLUSTER_MEANING`` verbatim, which is what
        #     ``team_contracts.StyleBalance`` reads too. This is the rule that
        #     makes the contribution section produce something on a realistic
        #     pair, given how rarely the lexical rule above fires (see
        #     :data:`_OVERLAP_CALIBRATION`).
        #
        #     A COMPLEMENT, never a gap: "between you, you cover more of the job"
        #     is a fact about two sets of themes. "Neither of you covers
        #     Stewardship" is a judgement about a group's capability and belongs
        #     to nothing this document does.
        lead_a = themes_a[0] if themes_a else None
        lead_b = themes_b[0] if themes_b else None
        if (
            lead_a is not None
            and lead_b is not None
            and lead_a.cluster != lead_b.cluster
        ):
            candidate = _candidate(
                dimension="contribution_mix",
                label="You are strongest in different kinds of work",
                statement=SYN_DIFFERENT_CLUSTERS,
                confidence=strengths_read.joint_confidence,
                subject_a=subject_a,
                framework_a="strengths",
                field_a="cluster",
                text_a=strengths.CLUSTER_MEANING[lead_a.cluster],
                subject_b=subject_b,
                framework_b="strengths",
                field_b="cluster",
                text_b=strengths.CLUSTER_MEANING[lead_b.cluster],
            )
            if candidate is not None:
                out.append(candidate)

    # 6. interpersonal_needs — what one needs, the other already offers. Reads
    #    two frameworks, so BOTH have to be comparable for the pair.
    if enneagram_read.comparable and disc_read.comparable:
        spec_a = _enneagram_spec(enneagram_read.layer_a)
        comms_b = _disc_list(disc_read.layer_b, "communication")
        if spec_a is not None:
            match = next(
                (t for t in comms_b[:3] if _overlaps(spec_a.needs_from_colleagues, t)),
                None,
            )
            if match is not None:
                candidate = _candidate(
                    dimension="interpersonal_needs",
                    label="What helps you most",
                    statement=SYN_NEEDS_MET,
                    confidence=contracts.floor_confidence(
                        enneagram_read.joint_confidence, disc_read.joint_confidence
                    ),
                    subject_a=subject_a,
                    framework_a="enneagram",
                    field_a="needs_from_colleagues",
                    text_a=spec_a.needs_from_colleagues,
                    subject_b=subject_b,
                    framework_b="disc",
                    field_b="communication",
                    text_b=match,
                )
                if candidate is not None:
                    out.append(candidate)

    # 7. the four-letter axes — the same end of the same question. One dimension
    #    per axis, from the AUTHORED meaning fields and never from the code.
    if type16_read.comparable:
        calls_a = _axis_calls(type16_read.layer_a)
        calls_b = _axis_calls(type16_read.layer_b)
        for entry in _AXIS_DIMENSIONS:
            call_a = calls_a.get(entry.axis)
            call_b = calls_b.get(entry.axis)
            if call_a is None or call_b is None or call_a.letter != call_b.letter:
                continue
            confidence = contracts.floor_confidence(
                type16_read.joint_confidence,
                contracts.floor_confidence(call_a.confidence, call_b.confidence),
            )
            # A coin-flip letter carries no information about the person, so it
            # produces neither claim rather than a hedged one.
            if confidence == "none":
                continue
            candidate = _candidate(
                dimension=entry.dimension,
                label=entry.label,
                statement=entry.same_statement,
                confidence=confidence,
                subject_a=subject_a,
                framework_a="type16",
                field_a=call_a.field,
                text_a=call_a.meaning,
                subject_b=subject_b,
                framework_b="type16",
                field_b=call_b.field,
                text_b=call_b.meaning,
                label_a=entry.field_label,
                label_b=entry.field_label,
            )
            if candidate is not None:
                out.append(candidate)
    return out


# ── Friction rules ───────────────────────────────────────────────────────────
# The higher bar, and every one of these carries an ask of BOTH people. The type
# refuses a friction with one ask, which is what closes the source format's
# deference gradient structurally rather than by prompt wording.

FRIC_DRAIN_MEETS_ENERGY = (
    "Work that one of you named as draining is work the other named as energising."
)
FRIC_ASK_MEETS_DRAIN = (
    "What one of you asks colleagues to do is close to what the other named as "
    "costing them energy."
)
FRIC_FEEDBACK_MEETS_DRAIN = (
    "How one of you wants feedback delivered is close to what the other named as "
    "draining."
)
FRIC_STRESS_MEETS_OVERUSE = (
    "What one of you does when a week goes badly is close to what the other "
    "named as the cost of their own strength taken too far."
)
FRIC_TOP_MEETS_BOTTOM = (
    "What makes the work feel worth doing to one of you is the thing that moves "
    "the other least."
)

ASK_TRADE = (
    "Say which of these is genuinely yours to do and which you would rather trade."
)
ASK_NAME_IT = "Say it in the moment rather than afterwards, in one sentence."
ASK_SAY_WHY = "Say why the work matters to you in your own terms, not in theirs."
ASK_SAY_WHAT_YOU_NEED = (
    "Say what you need here before the work starts, not once it is late."
)
ASK_CHECK_FIRST = "Ask which one this is before assuming, once, at the start."

SCRIPT_UPDATE = (
    '"Before we start — do you want the headline first or the background first?"'
)
SCRIPT_DECIDE = '"What are you weighing on this one? Here is what I am weighing."'
SCRIPT_PLAN = '"How settled does this need to be before we start?"'
SCRIPT_EVIDENCE = '"How much of the working do you want with the recommendation?"'
SCRIPT_FEEDBACK = '"When you have something to tell me, how do you want to say it?"'
SCRIPT_BAD_WEEK = '"This is a bad week for me. Here is what that looks like."'
SCRIPT_ENERGY = (
    '"Which part of this would you rather have, and which would you rather not?"'
)
SCRIPT_WORTH = '"Say why this one matters to you. Mine is different and that is fine."'
SCRIPT_CONTRIBUTION = (
    '"What should I be bringing you, and what should you be bringing me?"'
)
SCRIPT_NEEDS = '"What do you need from me on this that you have not asked for?"'

#: One script per dimension, authored, and every one of them sayable by EITHER of
#: the two. That is the test a script has to pass: a line only the reader could
#: say is the source format's deference gradient smuggled back in through a
#: section this spec otherwise keeps.
SCRIPTS: Mapping[str, str] = {
    "communication_pace": SCRIPT_UPDATE,
    "decision_style": SCRIPT_DECIDE,
    "structure_preference": SCRIPT_PLAN,
    "information_depth": SCRIPT_EVIDENCE,
    "feedback_exchange": SCRIPT_FEEDBACK,
    "stress_response": SCRIPT_BAD_WEEK,
    "energy_and_drain": SCRIPT_ENERGY,
    "motivator_alignment": SCRIPT_WORTH,
    "contribution_mix": SCRIPT_CONTRIBUTION,
    "interpersonal_needs": SCRIPT_NEEDS,
}

assert set(SCRIPTS) == set(cc.DIMENSION_KEYS), (
    "comparison.SCRIPTS must cover exactly comparison_contracts.DIMENSION_KEYS; a "
    "dimension with no script renders a section with no way to act on it"
)


@dataclass(frozen=True)
class _AxisDimension:
    """One four-letter axis, the dimension it feeds, and its authored copy.

    Per-axis statements and citation labels rather than one generic pair, because
    four sections all headed "You start from the opposite end of the same
    question" and all cited to "your answer about where you start from" read as a
    rendering bug. They are four different questions.

    The axis is read through its AUTHORED meaning fields only. The four-letter
    code is never rendered by a comparison at all — every
    :class:`comparison_contracts.Dimension` sets ``may_name_derived_type`` false,
    and the qualifier rule makes a bare code the one thing this layer may never
    print.
    """

    axis: str
    dimension: str
    label: str
    same_statement: str
    opposite_statement: str
    field_label: str


_AXIS_DIMENSIONS: tuple[_AxisDimension, ...] = (
    _AxisDimension(
        axis=type16.Axis.EI.value,
        dimension="communication_pace",
        label="Thinking out loud, or arriving with it",
        same_statement=(
            "You each described working through a question the same way — either "
            "out loud or on your own first."
        ),
        opposite_statement=(
            "One of you works a question out by talking; the other works it out "
            "first and brings the conclusion."
        ),
        field_label="answer about thinking out loud or arriving with it",
    ),
    _AxisDimension(
        axis=type16.Axis.NS.value,
        dimension="information_depth",
        label="Where you start from",
        same_statement="You each said you start a problem from the same place.",
        opposite_statement=(
            "One of you starts from the pattern and the possibility; the other "
            "starts from what is concrete and proven."
        ),
        field_label="answer about where a problem starts",
    ),
    _AxisDimension(
        axis=type16.Axis.TF.value,
        dimension="decision_style",
        label="What you weigh",
        same_statement="You each said you weigh the same thing when a call has to be made.",
        opposite_statement=(
            "You each said you weigh something different when a call has to be made."
        ),
        field_label="answer about what they weigh in a decision",
    ),
    _AxisDimension(
        axis=type16.Axis.JP.value,
        dimension="structure_preference",
        label="How settled the plan needs to be",
        same_statement=(
            "You each want about the same amount of plan settled before the work "
            "starts."
        ),
        opposite_statement=(
            "One of you wants the plan settled before the work starts; the other "
            "wants to start and let it take shape."
        ),
        field_label="answer about how settled the plan needs to be",
    ),
)

assert {entry.dimension for entry in _AXIS_DIMENSIONS} <= set(cc.DIMENSION_KEYS), (
    "an axis feeds a dimension that does not exist"
)
assert {entry.axis for entry in _AXIS_DIMENSIONS} == {
    axis.value for axis in type16.Axis
}, (
    "comparison._AXIS_DIMENSIONS has drifted from type16.Axis; an axis with no "
    "dimension is a layer nobody can compare on, and its absence would read as "
    "'nothing found'"
)


def _friction_candidates(
    reads: Mapping[str, cc.PairwiseRead],
) -> list[_Candidate]:
    """Every stated-preference collision the two profiles support.

    Each rule cites two DISTINCT ``(framework, field)`` operands, because
    :class:`comparison_contracts.Friction` requires it — a collision is between
    two preferences, and one field cannot collide with itself. That rule is why
    there is no "you both do the same thing under pressure" friction here: it
    would cite ``disc.under_stress`` on both sides, which is one operand, and
    inventing a second citation to get around the check would be exactly the
    dishonesty the check is for. The nearest honest version — one person's stress
    response against the other's overapplied strength — is rule 4.
    """
    out: list[_Candidate] = []
    disc_read = reads["disc"]
    strengths_read = reads["strengths"]
    motivators_read = reads["motivators"]
    type16_read = reads["type16"]
    subject_a = disc_read.subject_a
    subject_b = disc_read.subject_b

    if disc_read.comparable:
        drained_a = _disc_list(disc_read.layer_a, "drained_by")
        drained_b = _disc_list(disc_read.layer_b, "drained_by")
        energy_a = _disc_list(disc_read.layer_a, "energized_by")
        energy_b = _disc_list(disc_read.layer_b, "energized_by")
        comms_a = _disc_list(disc_read.layer_a, "communication")
        comms_b = _disc_list(disc_read.layer_b, "communication")

        # 1. energy_and_drain — one person's fuel is the other's drain. Both
        #    directions, and the asks follow the direction.
        for (
            text_drain,
            texts_energy,
            drain_is_a,
        ) in ((drained_a, energy_b, True), (drained_b, energy_a, False)):
            found = None
            for candidate_drain in text_drain[:2]:
                match = next(
                    (t for t in texts_energy[:2] if _overlaps(candidate_drain, t)),
                    None,
                )
                if match is not None:
                    found = (candidate_drain, match)
                    break
            if found is None:
                continue
            drain_text, energy_text = found
            candidate = _candidate(
                dimension="energy_and_drain",
                label="One of you is fuelled by what tires the other",
                statement=FRIC_DRAIN_MEETS_ENERGY,
                confidence=disc_read.joint_confidence,
                subject_a=subject_a,
                framework_a="disc",
                field_a="drained_by" if drain_is_a else "energized_by",
                text_a=drain_text if drain_is_a else energy_text,
                subject_b=subject_b,
                framework_b="disc",
                field_b="energized_by" if drain_is_a else "drained_by",
                text_b=energy_text if drain_is_a else drain_text,
                ask_of_a=ASK_TRADE,
                ask_of_b=ASK_TRADE,
                script=SCRIPTS["energy_and_drain"],
            )
            if candidate is not None:
                out.append(candidate)
                break

        # 2. communication_pace — what one asks for costs the other energy.
        for texts_comms, texts_drain, comms_is_a in (
            (comms_a, drained_b, True),
            (comms_b, drained_a, False),
        ):
            found = None
            for candidate_comms in texts_comms[:3]:
                match = next(
                    (t for t in texts_drain[:2] if _overlaps(candidate_comms, t)),
                    None,
                )
                if match is not None:
                    found = (candidate_comms, match)
                    break
            if found is None:
                continue
            comms_text, drain_text = found
            candidate = _candidate(
                dimension="communication_pace",
                label="The update one of you wants is work the other finds costly",
                statement=FRIC_ASK_MEETS_DRAIN,
                confidence=disc_read.joint_confidence,
                subject_a=subject_a,
                framework_a="disc",
                field_a="communication" if comms_is_a else "drained_by",
                text_a=comms_text if comms_is_a else drain_text,
                subject_b=subject_b,
                framework_b="disc",
                field_b="drained_by" if comms_is_a else "communication",
                text_b=drain_text if comms_is_a else comms_text,
                ask_of_a=ASK_SAY_WHAT_YOU_NEED,
                ask_of_b=ASK_SAY_WHAT_YOU_NEED,
                script=SCRIPTS["communication_pace"],
            )
            if candidate is not None:
                out.append(candidate)
                break

        # 3. feedback_exchange — how one wants it said is what drains the other.
        feedback_a = _disc_text(disc_read.layer_a, "feedback")
        feedback_b = _disc_text(disc_read.layer_b, "feedback")
        for feedback, texts_drain, feedback_is_a in (
            (feedback_a, drained_b, True),
            (feedback_b, drained_a, False),
        ):
            if not feedback:
                continue
            match = next((t for t in texts_drain[:2] if _overlaps(feedback, t)), None)
            if match is None:
                continue
            candidate = _candidate(
                dimension="feedback_exchange",
                label="Feedback asks for different things",
                statement=FRIC_FEEDBACK_MEETS_DRAIN,
                confidence=disc_read.joint_confidence,
                subject_a=subject_a,
                framework_a="disc",
                field_a="feedback" if feedback_is_a else "drained_by",
                text_a=feedback if feedback_is_a else match,
                subject_b=subject_b,
                framework_b="disc",
                field_b="drained_by" if feedback_is_a else "feedback",
                text_b=match if feedback_is_a else feedback,
                ask_of_a=ASK_NAME_IT,
                ask_of_b=ASK_NAME_IT,
                script=SCRIPTS["feedback_exchange"],
            )
            if candidate is not None:
                out.append(candidate)
                break

    # 4. stress_response — one person's bad week against the other's overapplied
    #    strength. Two frameworks, so both joint gates must be open.
    if disc_read.comparable and strengths_read.comparable:
        stress_a = _disc_text(disc_read.layer_a, "under_stress")
        stress_b = _disc_text(disc_read.layer_b, "under_stress")
        themes_a = _theme_specs(strengths_read.layer_a)
        themes_b = _theme_specs(strengths_read.layer_b)
        confidence = contracts.floor_confidence(
            disc_read.joint_confidence, strengths_read.joint_confidence
        )
        for stress, themes, stress_is_a in (
            (stress_a, themes_b, True),
            (stress_b, themes_a, False),
        ):
            if not stress:
                continue
            match = next(
                (spec for spec in themes if _overlaps(stress, spec.overused)), None
            )
            if match is None:
                continue
            candidate = _candidate(
                dimension="stress_response",
                label="A bad week pulls in the same direction twice",
                statement=FRIC_STRESS_MEETS_OVERUSE,
                confidence=confidence,
                subject_a=subject_a,
                framework_a="disc" if stress_is_a else "strengths",
                field_a="under_stress" if stress_is_a else "overused",
                text_a=stress if stress_is_a else match.overused,
                subject_b=subject_b,
                framework_b="strengths" if stress_is_a else "disc",
                field_b="overused" if stress_is_a else "under_stress",
                text_b=match.overused if stress_is_a else stress,
                ask_of_a=ASK_NAME_IT,
                ask_of_b=ASK_NAME_IT,
                script=SCRIPTS["stress_response"],
            )
            if candidate is not None:
                out.append(candidate)
                break

    # 5. motivator_alignment — one person's top axis is the other's bottom.
    #    Structural, and the strongest claim in the module: the two rankings name
    #    the same axis at opposite ends.
    if motivators_read.comparable:
        top_a, bottom_a = _motivator_specs(motivators_read.layer_a)
        top_b, bottom_b = _motivator_specs(motivators_read.layer_b)
        for top, bottom, top_is_a in (
            (top_a, bottom_b, True),
            (top_b, bottom_a, False),
        ):
            if bottom is None:
                continue
            match = next(
                (spec for spec in top if spec.motivator == bottom.motivator), None
            )
            if match is None:
                continue
            candidate = _candidate(
                dimension="motivator_alignment",
                label="What makes it worth doing is not the same thing",
                statement=FRIC_TOP_MEETS_BOTTOM,
                confidence=motivators_read.joint_confidence,
                subject_a=subject_a,
                framework_a="motivators",
                field_a="high_read" if top_is_a else "low_read",
                text_a=match.high_read if top_is_a else bottom.low_read,
                subject_b=subject_b,
                framework_b="motivators",
                field_b="low_read" if top_is_a else "high_read",
                text_b=bottom.low_read if top_is_a else match.high_read,
                ask_of_a=ASK_SAY_WHY,
                ask_of_b=ASK_SAY_WHY,
                script=SCRIPTS["motivator_alignment"],
            )
            if candidate is not None:
                out.append(candidate)
                break

    # 6. the four-letter axes — opposite ends. The two sides cite
    #    ``high_meaning`` and ``low_meaning``, which is two distinct operands.
    if type16_read.comparable:
        calls_a = _axis_calls(type16_read.layer_a)
        calls_b = _axis_calls(type16_read.layer_b)
        for entry in _AXIS_DIMENSIONS:
            call_a = calls_a.get(entry.axis)
            call_b = calls_b.get(entry.axis)
            if call_a is None or call_b is None or call_a.letter == call_b.letter:
                continue
            confidence = contracts.floor_confidence(
                type16_read.joint_confidence,
                contracts.floor_confidence(call_a.confidence, call_b.confidence),
            )
            candidate = _candidate(
                dimension=entry.dimension,
                label=entry.label,
                statement=entry.opposite_statement,
                confidence=confidence,
                subject_a=subject_a,
                framework_a="type16",
                field_a=call_a.field,
                text_a=call_a.meaning,
                subject_b=subject_b,
                framework_b="type16",
                field_b=call_b.field,
                text_b=call_b.meaning,
                ask_of_a=ASK_CHECK_FIRST,
                ask_of_b=ASK_CHECK_FIRST,
                script=SCRIPTS[entry.dimension],
                label_a=entry.field_label,
                label_b=entry.field_label,
            )
            if candidate is not None:
                out.append(candidate)
    return out


# ── Assembling the comparison ────────────────────────────────────────────────


def _refusal_note(dimension: cc.Dimension, gate: cc.JointGate) -> str:
    """The authored sentence a refused dimension renders instead of a finding.

    Names the QUESTION, not the framework. "Cubie cannot speak to how settled the
    plan needs to be before work starts" is a sentence a reader can act on;
    "type16 is closed for one of you" is not, and it leaks which instrument is
    missing without saying anything useful.
    """
    if gate == cc.GATE_ONE_REFUSED:
        tail = "One of you has this part of the profile not reported."
    else:
        tail = "Neither of you has this part of the profile reported."
    return f"{dimension.question} {tail}"


def _assess_dimension(
    dimension: cc.Dimension,
    reads: Mapping[str, cc.PairwiseRead],
    synergies: Sequence[cc.Synergy],
    frictions: Sequence[cc.Friction],
) -> cc.DimensionOutcome:
    """One dimension: assessed with what was found, or refused with why.

    The rule, and it is the whole reason this function is not two lines:

    * anything found → assessed. It could only have been found through open joint
      gates.
    * nothing found, and EVERY framework feeding this dimension is comparable →
      assessed and empty. "We looked and nothing collided" is a real answer.
    * nothing found, and any framework feeding it is NOT comparable → refused.

    That third branch is the point. ``Dimension.is_open`` is ``any``-comparable,
    which is right for "could this dimension say anything at all" and wrong here:
    a dimension readable through DISC but not through strengths would report
    "nothing to watch here" while the strengths half was never looked at, and the
    reader would take the reassuring explanation. Absence of a finding and
    absence of an answer are different sentences.
    """
    if synergies or frictions:
        return cc.AssessedDimension(
            dimension=dimension.key,
            synergies=tuple(synergies),
            frictions=tuple(frictions),
        )

    closed = {
        framework: reads[framework].closed_for
        for framework in dimension.frameworks
        if not reads[framework].comparable
    }
    if not closed:
        return cc.AssessedDimension(dimension=dimension.key)

    gates = {reads[framework].gate for framework in closed}
    gate = cc.GATE_ONE_REFUSED if cc.GATE_ONE_REFUSED in gates else cc.GATE_NEITHER_OPEN
    return cc.RefusedDimension(
        dimension=dimension.key,
        gate=gate,
        closed_for=closed,
        note=_refusal_note(dimension, gate),
    )


def assess_pair(
    reads: Mapping[str, cc.PairwiseRead],
) -> tuple[cc.PairComparison, tuple[_Candidate, ...], tuple[_Candidate, ...]]:
    """The whole comparison, plus the renderable candidates behind it.

    Returns the contract object AND the presentation candidates, because the
    contract deliberately does not carry a table label or a script line and the
    renderer needs both. The contract object is what ST-9 consumes; the
    candidates never leave this module.

    Claims that the contract types refuse — a friction under
    ``MIN_FRICTION_CONFIDENCE``, a claim whose evidence did not survive its
    screen — are dropped here with a log line, never softened into a weaker
    claim.
    """
    if not reads:
        raise ComparisonError("assess_pair needs a full set of pairwise reads")
    any_read = next(iter(reads.values()))
    subjects = (any_read.subject_a, any_read.subject_b)

    synergy_candidates = _synergy_candidates(reads)[:MAX_SYNERGY_ROWS]
    friction_candidates = _friction_candidates(reads)[:MAX_FRICTION_SECTIONS]

    kept_synergies: list[_Candidate] = []
    by_dimension_synergy: dict[str, list[cc.Synergy]] = {}
    for candidate in synergy_candidates:
        try:
            claim = cc.Synergy(
                dimension=candidate.dimension,
                subjects=subjects,
                statement=candidate.statement,
                confidence=candidate.confidence,
                evidence=candidate.evidence,
            )
        except cc.ComparisonError as exc:
            logger.info(
                "[smart-teams] comparison: dropping a synergy on %s — %s",
                candidate.dimension,
                exc,
            )
            continue
        by_dimension_synergy.setdefault(candidate.dimension, []).append(claim)
        kept_synergies.append(candidate)

    kept_frictions: list[_Candidate] = []
    by_dimension_friction: dict[str, list[cc.Friction]] = {}
    for candidate in friction_candidates:
        try:
            claim = cc.Friction(
                dimension=candidate.dimension,
                subjects=subjects,
                statement=candidate.statement,
                confidence=candidate.confidence,
                evidence=candidate.evidence,
                ask_of_a=candidate.ask_of_a,
                ask_of_b=candidate.ask_of_b,
            )
        except cc.ComparisonError as exc:
            # The commonest reason by far, and the intended one: the joint
            # confidence is below MIN_FRICTION_CONFIDENCE. A friction derived
            # from a coin-flip letter or a thin transcript is refused outright
            # rather than hedged, which makes some pair guides thinner than the
            # format they are modelled on. That is the deliberate trade.
            logger.info(
                "[smart-teams] comparison: dropping a friction on %s — %s",
                candidate.dimension,
                exc,
            )
            continue
        by_dimension_friction.setdefault(candidate.dimension, []).append(claim)
        kept_frictions.append(candidate)

    outcomes = tuple(
        _assess_dimension(
            dimension,
            reads,
            by_dimension_synergy.get(dimension.key, ()),
            by_dimension_friction.get(dimension.key, ()),
        )
        for dimension in cc.DIMENSIONS
    )
    comparison = cc.PairComparison(
        subject_a=subjects[0],
        subject_b=subjects[1],
        reads=dict(reads),
        outcomes=outcomes,
    )
    return comparison, tuple(kept_synergies), tuple(kept_frictions)


# ── The document model ───────────────────────────────────────────────────────


@dataclass
class PairSection:
    """One synergy row or one friction section, and its provenance.

    ``sources`` is ``((subject, framework, field), ...)`` — the personal guide's
    ``Guideline.source`` is ``(framework, field)`` with no subject, which is
    exactly the field a pair item cannot do without: "whose answer is this" is
    half of every claim here.

    **The body is two strings with different provenances, and keeping them apart
    is the control.** :attr:`lead` is the relationship clause — authored, and the
    one string a model may re-voice. :attr:`evidence` is the two quoted answers
    with their owner attached, authored and never model-written. :attr:`body` is
    the concatenation and is a property rather than a field, so no caller can
    assign a whole body and quietly put a model's words where an owner is named.
    """

    slot_id: str
    kind: str
    dimension: str
    heading: str
    lead: str
    evidence: str
    fact: str
    intent: str
    sources: tuple[tuple[str, str, str], ...]
    citation: str
    authored_spans: tuple[str, ...]
    ask_of_a: str | None = None
    ask_of_b: str | None = None
    script: str | None = None
    model_written: bool = False

    @property
    def body(self) -> str:
        """The rendered body: the clause, then the two owner-attributed answers.

        Read-only on purpose. The owner attribution lives in :attr:`evidence`,
        which is built by :func:`_authored_evidence` and never leaves this
        module, so "the model swapped who said what" has no code path.
        """
        return f"{self.lead} {self.evidence}".strip()

    def __post_init__(self) -> None:
        if self.kind not in {"synergy", "friction"}:
            raise ComparisonError(f"{self.slot_id}: unknown section kind {self.kind!r}")
        cc.dimension_for(self.dimension)
        if not self.sources:
            raise ComparisonError(
                f"{self.slot_id}: a pair section with no sources cites nothing"
            )
        for _subject, framework, _field in self.sources:
            if framework not in contracts.FRAMEWORKS:
                raise ComparisonError(
                    f"{self.slot_id}: source names unknown framework {framework!r}"
                )
        if self.kind == "friction" and not (
            (self.ask_of_a or "").strip() and (self.ask_of_b or "").strip()
        ):
            raise ComparisonError(
                f"{self.slot_id}: a friction section carries an ask of BOTH "
                "people; a one-sided remedy makes one of them the party who "
                "adapts"
            )


@dataclass(frozen=True)
class PairPanelColumn:
    """One side of the symmetric panel."""

    heading: str
    bullets: tuple[str, ...]


@dataclass(frozen=True)
class PairPanel:
    """The two-column "what each of you asks for" panel.

    Both columns come from the SAME authored field on both subjects, and both
    hold the same number of bullets. Neither is a stylistic choice:

    * The source format's panel headed one column with a name and the other with
      "you", and drew the two columns from different content — decision rights on
      one side, contributions on the other — because one of the two people
      outranked the other. That is an org chart with an assessment veneer.
    * Unequal bullet counts read as "one of them contributes more", which is a
      judgement of capability by layout.

    Both invariants are enforced in ``__post_init__``, so a future caller cannot
    build the asymmetric version by accident.
    """

    field_label: str
    disclaimer: str
    left: PairPanelColumn
    right: PairPanelColumn

    def __post_init__(self) -> None:
        if len(self.left.bullets) != len(self.right.bullets):
            raise ComparisonError(
                "the pair panel's two columns must hold the same number of "
                f"bullets; got {len(self.left.bullets)} and "
                f"{len(self.right.bullets)}. Unequal columns read as one person "
                "contributing more"
            )
        if not self.left.bullets:
            raise ComparisonError(
                "an empty pair panel is omitted, not rendered — see PANEL_MIN_BULLETS"
            )


@dataclass(frozen=True)
class PairCard:
    """One numbered discipline, and the two answers it reconciles."""

    number: int
    dimension: str
    title: str
    body: str
    citation: str


@dataclass(frozen=True)
class PairCompletenessLine:
    """One framework's line in the pair provenance section.

    ``gate`` is the joint gate, so the three states get three sentences.
    ``subjects_to_finish`` names WHO still has something to finish — display
    names, resolved by the caller — and never what is behind their closed gate.
    That naming is not new disclosure: each of those people has published a
    personal guide whose own completeness section already says the same thing to
    the whole company.
    """

    framework: str
    label: str
    gate: cc.JointGate
    included: bool
    note: str
    subjects_to_finish: tuple[str, ...] = ()
    sections_to_finish: tuple[str, ...] = ()


@dataclass(frozen=True)
class PairRefusalLine:
    """One dimension this guide could not assess, as a question and a reason.

    ``subjects_to_finish`` carries the same meaning it carries on
    :class:`PairCompletenessLine` — the people who can actually open the gate,
    not everyone whose gate is closed. The two lists are built from one
    definition (``cc.subjects_to_finish``) so that the two sections of a
    document cannot name different people.
    """

    dimension: str
    label: str
    note: str
    subjects_to_finish: tuple[str, ...] = ()


@dataclass
class PairGuide:
    """The whole pair document, assembled and renderable.

    Structured rather than a markdown string for the reason ``guide.PersonalGuide``
    gives: the staleness and consent machinery needs the metadata, and a second
    renderer walking the object again is how two artifacts drift.
    """

    subject_a: str
    subject_b: str
    name_a: str
    name_b: str
    profile_versions: dict[str, int]
    title: str
    deck: str
    framing: str
    provenance: guide.Callout
    synergies: list[PairSection] = field(default_factory=list)
    frictions: list[PairSection] = field(default_factory=list)
    friction_note: str | None = None
    panel: PairPanel | None = None
    cards: list[PairCard] = field(default_factory=list)
    refusals: list[PairRefusalLine] = field(default_factory=list)
    completeness: list[PairCompletenessLine] = field(default_factory=list)
    closing: guide.Callout | None = None
    shortfall: str | None = None
    advisory_notice: str = cc.PAIR_ADVISORY_NOTICE
    #: ``"{slot_id}.lead"`` -> the rejection code that sent that clause back to
    #: authored copy. One key per section rather than two, because the clause is
    #: the only string a model writes in a pair guide. Same device as
    #: ``PersonalGuide.fallbacks`` and kept for the same reason: "the model wrote
    #: none of this" and "the model wrote all of it" are the same document to a
    #: reader and completely different facts about the run.
    fallbacks: dict[str, str] = field(default_factory=dict)

    @property
    def has_content(self) -> bool:
        return bool(self.synergies or self.frictions)

    @property
    def subjects(self) -> tuple[str, str]:
        return (self.subject_a, self.subject_b)

    @property
    def model_written_count(self) -> int:
        return sum(
            1 for section in (*self.synergies, *self.frictions) if section.model_written
        )

    @property
    def sources(self) -> tuple[tuple[str, str, str], ...]:
        return tuple(
            source
            for section in (*self.synergies, *self.frictions)
            for source in section.sources
        )

    @property
    def authored_spans(self) -> tuple[str, ...]:
        """Every INSTRUMENT-authored sentence this document quotes verbatim.

        The exemption set for the assembled-body screen, enumerated rather than
        implied — see §3 of the module docstring. Each of these was screened by
        ``guide_prompts.screen_prose`` in :func:`_evidence` on the way in, and
        each of them is a string from one of the six instrument modules; nothing
        model-written and nothing from a transcript is ever in here.
        """
        spans: list[str] = []
        for section in (*self.synergies, *self.frictions):
            spans.extend(section.authored_spans)
        if self.panel is not None:
            spans.extend(self.panel.left.bullets)
            spans.extend(self.panel.right.bullets)
        return tuple(dict.fromkeys(span for span in spans if span.strip()))


# ── Sections ─────────────────────────────────────────────────────────────────


def _pair_fact(candidate: _Candidate) -> str:
    """The authored material a model-written string for this claim may speak in:
    **the relationship clause, alone.**

    No display name and no quoted answer, and the history is worth recording
    because the obvious construction is the one that failed.

    This function used to return ``f"{statement} {name_a}: {text_a} {name_b}:
    {text_b}"`` — both names and both authored sentences in one string — on the
    argument that containment rules 2-4 search the fact, so anything a body might
    legitimately mention had to be in it. **A single combined fact cannot
    distinguish owners.** Every permutation of "who said which sentence" is
    equally contained in it, so no containment rule can tell a faithful
    restatement from a swap, and the swap is the worst thing this document can
    do. Proven, on real profiles: a model body putting one subject's stated
    stress response onto the other, one inverting both subjects' stated
    preferences, and one putting a subject's decision style onto the other all
    shipped verbatim with ``fallbacks == {}``. Tightening the same fact to strict
    containment does not help — the words are all still in it.

    So the fact is no longer the control over owners. The control is structural:
    the two answers and their owners are rendered by
    :func:`_authored_evidence` and never pass through a model, and the fact is
    narrowed to the clause so that a body naming a person or quoting an answer is
    refused as novel content. Measured: all three of the swaps above are refused
    against this fact.
    """
    return candidate.statement


def _authored_evidence(candidate: _Candidate, *, name_b: str) -> str:
    """The two quoted answers, each with its owner attached. Never model-written.

    The half of a pair section that carries "whose answer is this", which is why
    it is authored here and screened nowhere else: there is no path by which a
    model can reach it. The reader is subject A, so their half is the second
    person; ``name_b`` is the colleague who is not reading.
    """
    evidence_a, evidence_b = candidate.evidence
    return f'Yours: "{evidence_a.text}" {name_b}\'s: "{evidence_b.text}"'


def _citation(candidate: _Candidate, *, name_a: str, name_b: str) -> str:
    """The in-document provenance line for one claim.

    Every section names both authored fields it came from, in the document rather
    than only in a log — that is the format spec's requirement and the thing that
    makes a friction arguable instead of assertive. When both sides cite the same
    kind of answer (most four-letter rows), the shared form is used: "From Alex's
    answer about X and Blair's answer about X" reads as a rendering bug.
    """
    if candidate.field_label_a == candidate.field_label_b:
        return CITATION_SHARED.format(
            name_a=name_a, name_b=name_b, label=candidate.field_label_a
        )
    return CITATION_TEMPLATE.format(
        name_a=name_a,
        label_a=candidate.field_label_a,
        name_b=name_b,
        label_b=candidate.field_label_b,
    )


def _sections_for(
    candidates: Sequence[_Candidate],
    *,
    kind: str,
    name_a: str,
    name_b: str,
) -> list[PairSection]:
    out: list[PairSection] = []
    for index, candidate in enumerate(candidates):
        evidence_a, evidence_b = candidate.evidence
        intent = (
            "two answers that ask for the same thing, one from each of them"
            if kind == "synergy"
            else (
                "two answers that ask for different things, one from each of "
                "them — a tension between two stated preferences and never "
                "something one of them does to the other"
            )
        )
        out.append(
            PairSection(
                slot_id=f"{kind}.{candidate.dimension}.{index}",
                kind=kind,
                dimension=candidate.dimension,
                heading=candidate.label,
                lead=candidate.statement,
                evidence=_authored_evidence(candidate, name_b=name_b),
                fact=_pair_fact(candidate),
                intent=intent,
                sources=(
                    (evidence_a.subject_email, evidence_a.framework, evidence_a.field),
                    (evidence_b.subject_email, evidence_b.framework, evidence_b.field),
                ),
                citation=_citation(candidate, name_a=name_a, name_b=name_b),
                authored_spans=(evidence_a.text, evidence_b.text),
                ask_of_a=candidate.ask_of_a or None,
                ask_of_b=candidate.ask_of_b or None,
                script=candidate.script or None,
            )
        )
    return out


# ── The symmetric panel ──────────────────────────────────────────────────────


def build_panel(
    reads: Mapping[str, cc.PairwiseRead], *, name_a: str, name_b: str
) -> PairPanel | None:
    """The "what each of you asks for" panel, or ``None``.

    ``strengths.ThemeSpec.ask_for`` on BOTH sides — the same question asked of
    each of them. Omitted whole unless the strengths joint gate is open and both
    people have at least :data:`PANEL_MIN_BULLETS` themes: the panel's meaning IS
    its symmetry, so a half-populated one is precisely the one-real-value-against
    -a-default failure this feature must not produce. That is stricter than the
    synergy-row rule on purpose.

    Each bullet is screened on the way in with ``guide_prompts.screen_prose``,
    the same call :func:`_evidence` makes, and a bullet that does not pass is
    dropped BEFORE the equal-count is computed rather than after — dropping it
    afterwards would leave the two columns unequal, which is the one thing this
    section may not be. That screen is not theoretical over this corpus: two
    instrument fields fail it today (an ``enneagram.needs_from_colleagues`` that
    says "the last 20%" and a ``motivators.in_conflict``), and the reason every
    span this document quotes is exempted from the assembled-body screen is that
    each one was checked here or in :func:`_evidence` first. A bullet that
    entered unscreened would break that argument.
    """
    read = reads["strengths"]
    if not read.comparable:
        return None

    def _screened(layer: Mapping[str, Any] | None) -> list[str]:
        out: list[str] = []
        for spec in _theme_specs(layer, limit=PANEL_MAX_BULLETS):
            reason = guide_prompts.screen_prose(spec.ask_for, max_words=10_000)
            if reason:
                logger.warning(
                    "[smart-teams] comparison: dropping the panel bullet for "
                    "strengths theme %r — it did not pass the prose screen (%s)",
                    spec.theme_id,
                    reason,
                )
                continue
            out.append(spec.ask_for)
        return out

    bullets_a = _screened(read.layer_a)
    bullets_b = _screened(read.layer_b)
    count = min(len(bullets_a), len(bullets_b), PANEL_MAX_BULLETS)
    if count < PANEL_MIN_BULLETS:
        return None
    return PairPanel(
        field_label=PANEL_LABEL,
        disclaimer=PANEL_DISCLAIMER,
        left=PairPanelColumn(
            heading=f"WHAT {name_a.upper()} ASKS FOR",
            bullets=tuple(bullets_a[:count]),
        ),
        right=PairPanelColumn(
            heading=f"WHAT {name_b.upper()} ASKS FOR",
            bullets=tuple(bullets_b[:count]),
        ),
    )


# ── Cards ────────────────────────────────────────────────────────────────────

#: One authored discipline per dimension. Emitted only for a dimension that
#: actually produced a friction, in canonical dimension order — a presentation
#: order and explicitly not a ranking, which is why the intro says so and why
#: there is no per-card weight, score, or priority.
#:
#: Note what is NOT here: the source format's "Explicit decision ownership".
#: Decision rights are not in any of the six layers, so a card asserting them
#: would be the source's org chart in card form.
CARD_TITLES: Mapping[str, str] = {
    "communication_pace": "Agree the shape of an update",
    "decision_style": "Say what each of you is weighing",
    "structure_preference": "Settle how much plan is enough",
    "information_depth": "Agree how much working travels with an ask",
    "feedback_exchange": "Say how each of you wants it said",
    "stress_response": "Flag a bad week early, both ways",
    "energy_and_drain": "Trade the parts that cost one of you more",
    "motivator_alignment": "Say out loud why the work matters to each of you",
    "contribution_mix": "Divide the work by what each of you asked for",
    "interpersonal_needs": "Ask for what you need instead of hoping",
}

assert set(CARD_TITLES) == set(cc.DIMENSION_KEYS), (
    "comparison.CARD_TITLES must cover exactly DIMENSION_KEYS; a dimension with "
    "no card title would render a numbered card with no heading"
)

CARD_BODY_TEMPLATE = (
    "{citation} Agree this once, out loud, rather than working around it."
)


def build_cards(frictions: Sequence[PairSection]) -> list[PairCard]:
    """Up to :data:`MAX_CARDS` disciplines, one per dimension that had a friction.

    Deduplicated by dimension and taken in the order the friction sections are
    in, which is canonical dimension order. Nothing is invented to reach three
    and nothing is dropped to look tidier.
    """
    out: list[PairCard] = []
    seen: set[str] = set()
    for section in frictions:
        if len(out) >= MAX_CARDS:
            break
        if section.dimension in seen:
            continue
        seen.add(section.dimension)
        out.append(
            PairCard(
                number=len(out) + 1,
                dimension=section.dimension,
                title=CARD_TITLES[section.dimension],
                body=CARD_BODY_TEMPLATE.format(citation=section.citation),
                citation=section.citation,
            )
        )
    return out


# ── Completeness and refusals ────────────────────────────────────────────────


def completeness_pair(
    reads: Mapping[str, cc.PairwiseRead], *, names: Mapping[str, str]
) -> list[PairCompletenessLine]:
    """One line per framework, with the joint gate's three sentences.

    The only section that reads gate STATE rather than layer content, and the
    only place a closed gate is spoken about at all.

    **No instrument hedge is ever printed here.** That rule is inherited from
    ``guide.completeness`` and it matters more in a pair document, not less: a
    hedge is authored for its own subject's eyes and several of them name the
    result the closed gate refused to report — the motivators hedge names its top
    two and its bottom, the Enneagram hedge names both candidate types. Printing
    one here would publish one person's withheld result to their colleague, under
    a line saying the layer is not included.
    """
    lines: list[PairCompletenessLine] = []
    for framework in contracts.FRAMEWORKS:
        read = reads[framework]
        label = guide.FRAMEWORK_LABELS[framework]
        gate = read.gate
        if gate == cc.GATE_BOTH_OPEN:
            lines.append(
                PairCompletenessLine(
                    framework=framework,
                    label=label,
                    gate=gate,
                    included=True,
                    note=COMPLETENESS_BOTH,
                )
            )
            continue

        note = COMPLETENESS_ONE if gate == cc.GATE_ONE_REFUSED else COMPLETENESS_NEITHER
        unfinished = tuple(names.get(email, email) for email in read.actionable_for)
        if unfinished:
            note = f"{note} {COMPLETENESS_STILL_TO_FINISH.format(names=', '.join(unfinished))}"
        lines.append(
            PairCompletenessLine(
                framework=framework,
                label=label,
                gate=gate,
                included=False,
                note=note,
                subjects_to_finish=unfinished,
                sections_to_finish=(
                    guide._sections_feeding(framework) if unfinished else ()
                ),
            )
        )
    return lines


def refusal_lines(
    comparison: cc.PairComparison, *, names: Mapping[str, str]
) -> list[PairRefusalLine]:
    """The dimensions this guide could not assess, as questions.

    Rendered so that a missing section reads as "we could not look" rather than
    as "we looked and found nothing" — which is the explanation a reader supplies
    for themselves, and always the more interesting one.

    ``subjects_to_finish`` comes from ``cc.subjects_to_finish`` and NOT from the
    refusal's own ``subjects_with_closed_gates``, which is every closed subject
    whatever the reason. Those are the same list only by accident, and printing
    the wider one put an instruction nobody could follow under half the
    dimensions in the document — see that function's docstring.
    """
    out: list[PairRefusalLine] = []
    for refusal in comparison.refusals:
        dimension = cc.dimension_for(refusal.dimension)
        out.append(
            PairRefusalLine(
                dimension=refusal.dimension,
                label=dimension.label,
                note=refusal.note,
                subjects_to_finish=tuple(
                    names.get(email, email)
                    for email in cc.subjects_to_finish(refusal, comparison.reads)
                ),
            )
        )
    return out


def framing_pair(comparison: cc.PairComparison, *, name_a: str, name_b: str) -> str:
    """The opening paragraph. Writable with ZERO open-for-both layers.

    Authored throughout, with one data-derived sentence: the counts. Nothing in
    it is layer-derived, which is what lets the shortfall document carry it
    unchanged, and there is deliberately no thesis — "you are similar in ways
    that can amplify each other's blind spots" is an inference from personally
    knowing two people, and no computation over two profiles yields it.
    """
    return " ".join(
        [
            FRAMING_OPENER.format(name_a=name_a, name_b=name_b),
            FRAMING_COUNTS.format(
                synergies=len(comparison.synergies), frictions=len(comparison.frictions)
            ),
            FRAMING_NO_OBSERVATION,
        ]
    )


# ── The LLM worker ───────────────────────────────────────────────────────────


@attribute_to(COST_FEATURE)
def write_pair_prose(
    slots: Sequence[cp.PairSlot],
    *,
    reader_name: str,
    about_name: str,
    model_key: str = PROSE_MODEL_KEY,
    temperature: float = PROSE_TEMPERATURE,
    attempts: int = PROSE_ATTEMPTS,
) -> dict[str, dict[str, str]]:
    """Re-voice each section's relationship clause. One LLM call. Never raises.

    Returns ``{slot_id: {"lead": ...}}`` holding only clauses that came back AND
    passed ``comparison_prompts.screen_pair_lead``. One key, because the clause
    is the only string a model writes in a pair guide: the two quoted answers and
    their owners are authored, and the heading is a dimension label.

    ``reader_name`` and ``about_name`` are taken for the CALL SHAPE and for the
    screens (via each slot), and they are deliberately not in the prompt — see
    ``comparison_prompts.pair_prose_prompt``. A model that has never been told
    either name cannot attach one to anything.

    ``@attribute_to`` is on THIS function, the one that makes the call, and not
    on :func:`build_pair_guide`: the AI Cost tag is a contextvar and a thread hop
    loses a tag set at the entry point. That is the 75%-"Unattributed" lesson,
    and this is a new feature, so it needs the tag on its worker in the same
    change that adds the worker.
    """
    if not slots:
        return {}
    prompt = cp.pair_prose_prompt(slots)
    client = guide._json_client(model_key, temperature)
    by_id = {slot.slot_id: slot for slot in slots}

    for attempt in range(1, max(1, attempts) + 1):
        try:
            payload = client.invoke_json(prompt, fallback=None, attempts=1)
        except Exception as exc:  # noqa: BLE001 — the authored copy is the floor
            logger.warning(
                "[smart-teams] pair prose call failed (attempt %d): %s", attempt, exc
            )
            continue

        accepted: dict[str, dict[str, str]] = {}
        rejected: list[tuple[str, str]] = []
        entries = payload.get("slots") if isinstance(payload, Mapping) else None
        for entry in entries or []:
            if not isinstance(entry, Mapping):
                continue
            slot_id = entry.get("id")
            if not isinstance(slot_id, str) or slot_id not in by_id:
                logger.info(
                    "[smart-teams] pair prose: dropping unknown slot id %r", slot_id
                )
                continue
            slot = by_id[slot_id]
            text = entry.get("lead")
            reason = cp.screen_pair_lead(text, slot)
            if reason:
                rejected.append((f"{slot_id}.lead", reason))
                logger.warning(
                    "[smart-teams] pair prose: rejected the lead of slot %s (%s)",
                    slot_id,
                    reason,
                )
                continue
            accepted[slot_id] = {"lead": str(text).strip()}

        if rejected and attempt < max(1, attempts):
            logger.info(
                "[smart-teams] pair prose: regenerating after %d rejected "
                "string(s): %s",
                len(rejected),
                ", ".join(f"{part} ({reason})" for part, reason in rejected),
            )
            continue
        return accepted
    return {}


PairProseWriter = Callable[..., Mapping[str, Mapping[str, str]]]


# ── Assembly ─────────────────────────────────────────────────────────────────


def build_pair_guide(
    profile_a: SmartTeamsProfile,
    profile_b: SmartTeamsProfile,
    *,
    name_a: str,
    name_b: str,
    prose_writer: PairProseWriter | None = None,
) -> PairGuide:
    """Assemble one pair guide from two scored profile versions.

    ``profile_a`` is the reader. With no ``prose_writer`` the document is
    entirely authored copy, which is a supported, shippable outcome — every
    sentence would have come from the same authored fields either way and the
    writer only changes the voice of one clause per section.

    **The single place model output is admitted into a pair document, and it is
    one clause per section.** The two quoted answers and the name attached to
    each are assembled by :func:`_authored_evidence` and are not reachable from
    here, so nothing a writer returns can change who said what. What a writer
    does return goes through ``comparison_prompts.screen_pair_lead`` here — the
    pair rules in this document's name grammar, the owner-binding rule, and
    strict fact containment against the clause alone, plus the INTERSECTION of
    the two subjects' open gates — regardless of whether the writer screened
    anything itself. ``guide.build_guide`` learned that the hard way: a stub
    writer reaching the builder without having been through the screen is
    precisely the path a fabricated body took into a published guide.
    """
    reads = read_pair(profile_a, profile_b)
    comparison, synergy_candidates, friction_candidates = assess_pair(reads)
    email_a = comparison.subject_a
    email_b = comparison.subject_b
    names = {email_a: name_a, email_b: name_b}

    synergies = _sections_for(
        synergy_candidates, kind="synergy", name_a=name_a, name_b=name_b
    )
    frictions = _sections_for(
        friction_candidates, kind="friction", name_a=name_a, name_b=name_b
    )
    has_content = bool(synergies or frictions)

    fallbacks: dict[str, str] = {}
    if has_content and prose_writer is not None:
        open_frameworks = comparison.comparable_frameworks
        slots = [
            cp.PairSlot(
                slot_id=section.slot_id,
                intent=section.intent,
                fact=section.fact,
                heading=section.heading,
                # The authored fallback for the one string a model may write.
                body=section.lead,
                reader_name=name_a,
                about_name=name_b,
                open_frameworks=open_frameworks,
            )
            for section in (*synergies, *frictions)
        ]
        by_id = {slot.slot_id: slot for slot in slots}
        try:
            written = prose_writer(slots, reader_name=name_a, about_name=name_b) or {}
        except Exception:  # noqa: BLE001 — authored copy is always available
            logger.exception(
                "[smart-teams] pair guide for %s|%s: prose writer failed; using "
                "authored copy",
                email_a,
                email_b,
            )
            written = {}
        for section in (*synergies, *frictions):
            entry = written.get(section.slot_id)
            if not isinstance(entry, Mapping):
                continue
            slot = by_id[section.slot_id]
            reason = cp.screen_pair_lead(entry.get("lead"), slot)
            if reason:
                fallbacks[f"{section.slot_id}.lead"] = reason
            else:
                section.lead = str(entry.get("lead")).strip()
                section.model_written = True

        for part, reason in sorted(fallbacks.items()):
            logger.warning(
                "[smart-teams] pair guide for %s|%s: %s fell back to authored "
                "copy (%s)",
                email_a,
                email_b,
                part,
                reason,
            )

    return PairGuide(
        subject_a=email_a,
        subject_b=email_b,
        name_a=name_a,
        name_b=name_b,
        profile_versions={
            email_a: int(profile_a.version),
            email_b: int(profile_b.version),
        },
        title=TITLE_TEMPLATE.format(name_a=name_a, name_b=name_b),
        deck=DECK,
        framing=framing_pair(comparison, name_a=name_a, name_b=name_b),
        provenance=guide.Callout(label=PROVENANCE_LABEL, body=PROVENANCE_BODY),
        synergies=synergies,
        frictions=frictions,
        friction_note=(
            f"{FRICTION_HYPOTHESIS_NOTE} {FRICTION_NO_RANK_NOTE}" if frictions else None
        ),
        panel=(
            build_panel(reads, name_a=name_a, name_b=name_b) if has_content else None
        ),
        cards=build_cards(frictions) if has_content else [],
        refusals=refusal_lines(comparison, names=names),
        completeness=completeness_pair(reads, names=names),
        closing=(
            guide.Callout(label=CLOSING_LABEL, body=CLOSING_BODY)
            if has_content
            else None
        ),
        shortfall=None if has_content else SHORTFALL_BODY,
        fallbacks=fallbacks,
    )


# ── Markdown ─────────────────────────────────────────────────────────────────


def _table_safe(text: str) -> str:
    """One markdown table cell: no pipes, no newlines, no runs of whitespace.

    A pipe ends the cell and a newline ends the ROW, so either one in a
    model-written body silently truncates the rest of a section — the kind of
    corruption that renders as a shorter document rather than as an error.
    """
    return " ".join(str(text or "").split()).replace("|", "/")


def render_markdown(pair: PairGuide) -> str:
    """The canonical body. ``st_documents.content_md`` holds exactly this.

    The same five markup shapes ``guide.render_markdown`` emits — ``# title``,
    ``## section``, ``### n. heading``, ``- bullet``, ``> **label** — body`` —
    plus one markdown table for the synergy rows. Kept identical so a future
    Word export can reuse ``guide.export_docx``'s line-oriented parser instead of
    growing a second one; there is no pair .docx endpoint today, and this is why
    adding one would be small.
    """
    out: list[str] = [
        f"# {pair.title}",
        "",
        pair.deck,
        "",
        pair.framing,
        "",
        f"> **{pair.provenance.label}** — {pair.provenance.body}",
        "",
    ]

    if pair.shortfall:
        out += [f"## {SHORTFALL_LABEL}", "", pair.shortfall, ""]

    out += [f"## {SYNERGY_LABEL}", ""]
    if pair.synergies:
        out += [
            f"| {SYNERGY_COLUMNS[0]} | {SYNERGY_COLUMNS[1]} |",
            "| --- | --- |",
        ]
        for section in pair.synergies:
            # A pipe would end the cell early and a newline would end the ROW,
            # silently dropping the rest of a section. A re-voiced clause is the
            # realistic source of both — punctuation is not something any screen
            # refuses — so both are flattened here rather than trusted to the
            # prompt.
            cell = _table_safe(f"{section.body} {section.citation}")
            out.append(f"| {_table_safe(section.heading)} | {cell} |")
        out += [""]
    else:
        out += [SYNERGY_NONE, ""]

    out += [f"## {FRICTION_LABEL}", ""]
    if pair.frictions:
        if pair.friction_note:
            out += [pair.friction_note, ""]
        for index, section in enumerate(pair.frictions, start=1):
            out += [f"### {index}. {section.heading}", "", section.body, ""]
            out += [section.citation, ""]
            out += [
                f"- **{ASK_LABEL_A}** — {section.ask_of_a}",
                f"- **{ASK_LABEL_B.format(name_b=pair.name_b)}** — {section.ask_of_b}",
                "",
            ]
            if section.script:
                out += [f"> **{SCRIPT_LABEL}** — {section.script}", ""]
    else:
        out += [FRICTION_NONE, ""]

    if pair.panel is not None:
        out += [f"## {pair.panel.field_label}", "", pair.panel.disclaimer, ""]
        for column in (pair.panel.left, pair.panel.right):
            out += [f"**{column.heading}**", ""]
            out += [f"- {bullet}" for bullet in column.bullets]
            out += [""]

    if pair.cards:
        out += [f"## {CARDS_LABEL}", "", CARDS_INTRO, ""]
        for card in pair.cards:
            out += [f"### {card.number:02d}. {card.title}", "", card.body, ""]

    if pair.refusals:
        out += [f"## {REFUSALS_LABEL}", "", REFUSALS_INTRO, ""]
        for refusal in pair.refusals:
            out.append(f"- **{refusal.label}** — {refusal.note}")
            if refusal.subjects_to_finish:
                out.append(
                    "  - Still to finish for: " + ", ".join(refusal.subjects_to_finish)
                )
        out += [""]

    out += [f"## {COMPLETENESS_LABEL}", "", COMPLETENESS_INTRO, ""]
    for line in pair.completeness:
        out.append(f"- **{line.label}** — {line.note}")
        if line.sections_to_finish:
            out.append(
                "  - Would be filled in by: "
                + ", ".join(f'"{title}"' for title in line.sections_to_finish)
            )
    out += [""]

    if pair.closing is not None:
        out += [f"> **{pair.closing.label}** — {pair.closing.body}", ""]

    out += ["---", "", f"*{pair.advisory_notice}*", ""]
    return "\n".join(out).strip() + "\n"


# ── Consent ──────────────────────────────────────────────────────────────────


def published_by_subject(db: Session, subjects: Sequence[str]) -> dict[str, bool]:
    """``{email: has a PUBLISHED personal guide}`` for the authz helper.

    Built from each subject's OWN ``personal_guide`` row through
    ``guide.published_guide``, never from the pair row's ``published`` flag —
    which is exactly the mistake ``authz.can_view_multi_subject_guide`` exists to
    prevent: one flag on a shared document records that somebody pressed a
    button, not N consents.
    """
    return {
        _fold(email): guide.published_guide(db, _fold(email)) is not None
        for email in subjects
        if _fold(email)
    }


def can_read_pair(db: Session, viewer: object, subjects: Sequence[str]) -> bool:
    """Whether ``viewer`` may read the pair guide about ``subjects``.

    A thin wrapper, on purpose: the rule is
    ``authz.can_view_multi_subject_guide`` and it is NOT re-derived here. What
    this adds is the one thing the predicate cannot do for itself — building the
    per-subject published map from the database. Refuse with
    ``authz.SHARED_GUIDE_NOT_PUBLISHED_DENIED``.
    """
    folded = [_fold(email) for email in subjects]
    return authz.can_view_multi_subject_guide(
        viewer, folded, published_by_subject(db, folded)
    )


def refuse_generation(db: Session, subjects: Sequence[str]) -> str | None:
    """``None`` if a pair guide may be generated for ``subjects``, else the reason.

    **Every subject — the requester included — must have published their own
    personal guide.** Reading and generating are different questions and this is
    the stricter answer, deliberately:

    * Generating writes one person's profile content into a cached row a third
      colleague may later read. The only consent we have for describing somebody
      company-wide is that they published their own guide, so requiring it before
      the content is written is the honest ordering. Generating first and gating
      the read would leave a row full of somebody's answers sitting behind a
      predicate, which is a worse place for it to be than not existing.
    * ``authz.can_view_multi_subject_guide`` deliberately exempts the VIEWER's own
      publish state, because consent is something you give others rather than
      something you need in order to read about yourself. That is right for
      reading and wrong for writing: the looser rule would let somebody who has
      published nothing commission a document about a colleague who has, which is
      asymmetric in exactly the direction that feels unfair to the person
      described.
    * The looser alternative — only the non-requester must have published —
      produces a cached document readable by one person plus admins, which is a
      cache-invalidation and support problem for no user benefit.

    Whether one employee may generate a pair guide for two OTHER people is
    answered in the router, structurally: there is no route parameter for the
    reader, so the requester is always one of the two.
    """
    folded = [_fold(email) for email in subjects]
    if len(folded) != 2 or not all(folded):
        return "A pair guide needs two identified people."
    if folded[0] == folded[1]:
        return "A pair guide needs two different people."
    for email in folded:
        if not authz.is_employee(email):
            return "Both people in a pair guide have to be employees of the company."
    published = published_by_subject(db, folded)
    missing = [email for email in folded if not published.get(email)]
    if missing:
        return authz.SHARED_GUIDE_NOT_PUBLISHED_DENIED
    return None


def approve_pair(*_args: object, **_kwargs: object) -> None:
    """There is no approval step for a pair guide, and this is why. Always raises.

    ``guide.approve`` compares the approver against ``document.subject_key``,
    which on a pair row is ``"a|b"`` and can never equal one person's address —
    so approval was already impossible. This function exists so that fact is
    DELIBERATE rather than accidental, and so the next person to look for a
    publish path finds the reasoning instead of the absence.

    One ``published`` flag cannot record two consents. The consent that makes a
    pair guide readable is each person having published their OWN guide, which is
    a thing each of them controls alone and can withdraw alone —
    ``authz.can_view_multi_subject_guide`` reads exactly that, and an unpublish by
    either of them takes the pair document out of everybody else's reach without
    anybody needing to remember it exists.
    """
    raise ComparisonError(
        "A pair guide is never approved or published. It becomes readable when "
        "both people have published their own working guide, and stops being "
        "readable when either of them unpublishes."
    )


# ── Persistence ──────────────────────────────────────────────────────────────


def cache_keys_for_pair(
    subject_a: str, subject_b: str, versions: Mapping[str, int]
) -> tuple[str, str]:
    """``document_cache_keys`` for a pair guide. One caller-facing helper.

    Order is PRESERVED, never sorted: ``subject_key_for("pair_guide", …)`` does
    not sort, because a guide written to A about B reads differently reversed and
    must not be served from the other one's cache entry.

    ``document_cache_keys`` raises ``KeyError`` on a missing version rather than
    defaulting to 0 — the documented bug that made a retake produce a
    byte-identical key — so the caller has to supply both.
    """
    email_a = _fold(subject_a)
    email_b = _fold(subject_b)
    return document_cache_keys(
        KIND_PAIR,
        [email_a, email_b],
        {_fold(key): int(value) for key, value in versions.items()},
    )


def current_pair_guide(
    db: Session,
    subject_a: str,
    subject_b: str,
    *,
    versions: Mapping[str, int] | None = None,
) -> SmartTeamsDocument | None:
    """The pair row for this ORDERED pair, optionally for exact profile versions.

    Without ``versions``: the newest row for the pair, whatever it was generated
    from — the read behind "you have an older one, regenerate?". With them: the
    exact cache entry, which is how a caller asks "is there already one for the
    profiles they have now".
    """
    subject_key = f"{_fold(subject_a)}|{_fold(subject_b)}"
    query = db.query(SmartTeamsDocument).filter(
        SmartTeamsDocument.kind == KIND_PAIR,
        func.lower(SmartTeamsDocument.subject_key) == subject_key,
    )
    if versions is not None:
        _subject_key, version_key = cache_keys_for_pair(subject_a, subject_b, versions)
        query = query.filter(SmartTeamsDocument.profile_version_key == version_key)
    return query.order_by(SmartTeamsDocument.created_at.desc()).first()


def is_stale_pair(
    document: SmartTeamsDocument, current_versions: Mapping[str, int | None]
) -> bool:
    """Whether EITHER subject's profile has advanced past what this guide cites.

    A single indexed string comparison, which is the whole reason
    ``profile_version_key`` exists. ``"alex:1|blair:1"`` becomes ``"alex:2|blair:1"``
    the moment Alex retakes, the lookup misses, and the old row stays findable by
    ``subject_key`` so the UI can offer Regenerate.

    A subject with no current profile at all is not staleness: there is nothing
    newer to be stale against, so the answer is ``False`` rather than a guess.
    """
    emails = [part for part in (document.subject_key or "").split("|") if part]
    if len(emails) != 2:
        return False
    resolved: dict[str, int] = {}
    for email in emails:
        version = current_versions.get(email)
        if version is None:
            return False
        resolved[email] = int(version)
    _subject_key, version_key = cache_keys_for_pair(emails[0], emails[1], resolved)
    return (document.profile_version_key or "") != version_key


def ensure_pair_row(
    db: Session,
    profile_a: SmartTeamsProfile,
    profile_b: SmartTeamsProfile,
    *,
    now: datetime | None = None,
) -> guide.GuideRow:
    """Find or create the ``pending`` row for this ordered pair and version pair.

    Idempotent through ``uq_st_document_cache``: the triple
    ``(kind, subject_key, profile_version_key)`` is the cache identity, so a
    double-clicked Generate lands one row. The row exists before its prose does
    because generation runs on cubie-worker.

    ``published`` is written ``False`` and nothing in this feature ever flips it —
    see :func:`approve_pair`.
    """
    email_a = _fold(profile_a.user_email)
    email_b = _fold(profile_b.user_email)
    if not email_a or not email_b:
        raise ComparisonError("cannot generate a pair guide without two subjects")
    if email_a == email_b:
        raise ComparisonError("a pair guide needs two different people")
    versions = {email_a: int(profile_a.version), email_b: int(profile_b.version)}
    subject_key, version_key = cache_keys_for_pair(email_a, email_b, versions)

    existing = (
        db.query(SmartTeamsDocument)
        .filter(
            SmartTeamsDocument.kind == KIND_PAIR,
            SmartTeamsDocument.subject_key == subject_key,
            SmartTeamsDocument.profile_version_key == version_key,
        )
        .one_or_none()
    )
    if existing is not None:
        return guide.GuideRow(document=existing, created=False)

    stamp = now or datetime.utcnow()
    row = SmartTeamsDocument(
        id=uuid.uuid4().hex,
        kind=KIND_PAIR,
        # Ordered, and the model's validator folds in place without sorting.
        subject_emails=[email_a, email_b],
        subject_key=subject_key,
        profile_versions=versions,
        profile_version_key=version_key,
        status="pending",
        published=False,
        rag_ingested=False,
        created_at=stamp,
        updated_at=stamp,
    )
    db.add(row)
    db.flush()
    return guide.GuideRow(document=row, created=True)


def generate_pair_into_row(
    db: Session,
    document: SmartTeamsDocument,
    profile_a: SmartTeamsProfile,
    profile_b: SmartTeamsProfile,
    *,
    name_a: str,
    name_b: str,
    prose_writer: PairProseWriter | None = None,
    now: datetime | None = None,
) -> PairGuide:
    """Build the pair guide and store it. Flips ``pending`` → ``ready``.

    One screen over the assembled body, with two span classes cut out first, and
    both cuts are argued in §3 of the module docstring:

    * the advisory notice, which contains the word the advancement pattern
      matches on — exactly the exemption ``guide.generate_into_row`` documents,
      and for the same reason (screening it rejected literally every guide);
    * the instrument-authored sentences this document quotes, enumerated by
      :attr:`PairGuide.authored_spans` and each screened on entry in
      :func:`_evidence`. Without this cut, one pair pattern firing on DISC's own
      feedback copy ("they will hear the first as an attack") would fail every
      pair guide involving that style, permanently and silently.

    Everything else — every authored pair string, every re-voiced clause, every
    citation — is screened here as well as at the point it was written, and here
    the pair patterns are built with BOTH display names in their subject slots.
    That matters because the document's own grammar names the other person:
    "Blair is the director on this" is the seniority family and a pronoun-anchored
    pattern does not see it.
    """
    if bool(document.published):
        # Unreachable today: nothing publishes a pair row. Kept because the
        # personal guide learned that regenerating over published words publishes
        # wording nobody read, and a future publish path must not find this
        # function willing.
        raise ComparisonError(
            "This pair guide is marked published. Nothing in Smart Teams "
            "publishes a pair guide, so this row needs a look before it is "
            "regenerated."
        )
    pair = build_pair_guide(
        profile_a,
        profile_b,
        name_a=name_a,
        name_b=name_b,
        prose_writer=prose_writer,
    )
    body = render_markdown(pair)

    screenable = body.replace(pair.advisory_notice, "")
    for span in pair.authored_spans:
        screenable = screenable.replace(span, "")
    reason = cp.screen_pair_prose(
        screenable, max_words=20_000, names=(pair.name_a, pair.name_b)
    )
    if reason:
        document.status = "failed"
        document.error = f"assembled pair guide failed the prose screen: {reason}"
        document.updated_at = now or datetime.utcnow()
        raise ComparisonError(document.error)

    document.title = pair.title
    document.content_md = body
    document.status = "ready"
    document.error = None
    document.updated_at = now or datetime.utcnow()
    return pair


def fail_row(db: Session, document: SmartTeamsDocument, message: str) -> None:
    """Record a generation failure on the row, so the UI can say so.

    Same shape as ``guide.fail_row`` and a separate function rather than an
    import, because the pair job must never reach a helper that could be pointed
    at a personal row by a copy-paste.
    """
    document.status = "failed"
    document.error = (message or "")[:500] or "generation failed"
    document.updated_at = datetime.utcnow()

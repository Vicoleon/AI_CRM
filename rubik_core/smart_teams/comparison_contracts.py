"""The shapes a two-person comparison is allowed to have — one definition each.

This module is the pair-guide (ST-7) equivalent of :mod:`contracts`, and it
exists for the same recorded reason: on the previous wave of this build two
agents implementing "the same" concept in parallel invented two shapes for it,
and every company-wide guide read returned 403 while both halves passed their
own tests. ST-9 (Team Builder) consumes ST-7's comparison output, so the shape
decisions are serialized here, before either feature is written.

Nothing here scores, generates, renders, or reads the database. It defines what
a comparison *is*, and — much more importantly — what a comparison is not
allowed to be.

## 1. The compounding gate, which is the whole point of the module

:mod:`contracts` establishes the single-subject rule: a framework payload is an
envelope whose gate is ``reportable``, and a closed gate means *the layer does
not know*. :func:`contracts.layer_of` returns ``None`` past a closed gate,
deliberately.

For two people that rule **compounds, and it compounds in the strict
direction**: a comparison may assert something about a framework only when that
framework is reportable for BOTH subjects. If either person has refused, the
comparison has nothing to say on that dimension, and the honest output is to
say so — not to hedge, and above all not to compare one real value against a
default. A hedged sentence is still a sentence.

"Reportable" and "comparable" are therefore different questions, and this
module keeps them different types. :class:`PairwiseRead` carries the joint gate
as a named tri-state (:data:`JOINT_GATES`) rather than as a Boolean ``and`` of
two flags, because the middle state — *one* subject refused — is the one that
needs its own prose in the completeness block and its own refusal everywhere
else. Collapsing it into "not comparable" loses the difference between "one of
you has not finished this part" and "neither of you has", and those two get
different sentences and different next steps.

The type enforces the compounding rather than trusting the caller to: a
:class:`PairwiseRead` whose gate is not :data:`GATE_BOTH_OPEN` cannot hold a
layer at all. Constructing one with a layer present and a closed joint gate
raises. That is the line the pair generator would otherwise get wrong once and
never notice, because a one-sided comparison reads perfectly fluently.

## 2. Dimensions are derived from the instruments, not invented here

A comparison assesses *dimensions* — communication pace, decision style,
structure preference, motivator alignment, and so on. Every dimension in
:data:`DIMENSIONS` names the instrument fields it reads, and those field names
are checked at import against the real attribute names of the instrument
dataclasses (see :data:`_INSTRUMENT_FIELD_NAMES`). A dimension that cites a
field an instrument does not have fails at import, which is the only way this
stays true through a rename: nothing else in the repo would notice.

Dimensions are *not* a seventh instrument. They are a reading order over the
six layers we already have, and they exist so that a refusal can be scoped to
"we cannot speak to structure preference" instead of the useless "type16 is
closed for one of you".

## 3. Synergy and friction are different claims with different bars

A **synergy** says two authored fields dovetail: what one person asks for is
something the other person's own words say they supply. It is a claim about two
texts.

A **friction** says two stated preferences collide. It is a stronger claim, and
this is not a stylistic opinion — it is what the two claims cost when wrong. A
synergy read wrongly produces a redundant sentence. A friction read wrongly
produces a document that tells one colleague to expect trouble from another,
which is the exact failure mode this feature is one bad release away from.
So the bars differ, and the difference is encoded rather than recommended:

* :data:`MIN_SYNERGY_CONFIDENCE` is ``"low"`` — a weak dovetail is still a
  dovetail, and the sentence is harmless.
* :data:`MIN_FRICTION_CONFIDENCE` is ``"moderate"`` — a friction asserted off a
  coin-flip letter or a thin transcript is refused outright.
* A :class:`Friction` must carry an ask of *both* people. A friction with one
  ask is malformed and raises. The source format this feature is modelled on
  put five of its six remedies on the reader alone, because one of the two
  people in it outranked the other; a deference gradient is a structural
  property of the type, so it is closed structurally.
* Neither claim carries a severity, a rank, or an ordering. Naming one tension
  as "the biggest risk" is an inference two self-reports cannot support, and it
  reads as assigning blame.

Both claims must cite :class:`Evidence` from **both** subjects. A claim about
two people that rests on one person's field is a claim about one person wearing
a comparison's clothes, and :class:`Evidence` additionally refuses to carry a
raw score (:data:`_RAW_VALUE_FIELDS`) — raw scores are subject + support admins only
and may never reach a comparison.

## 4. A refused dimension is a different type, not an empty list

The failure this module is built to prevent, stated plainly: a renderer sees
``outcome.frictions == ()`` and writes "no friction found", when the truth is
that the dimension was never assessed because one person's gate is closed.
Absence of a finding and absence of an answer are the same value in Python and
must not be the same type here.

So there is no single outcome object with an ``assessed`` flag.
:class:`AssessedDimension` has ``synergies`` and ``frictions``.
:class:`RefusedDimension` **does not have those attributes at all** — reaching
for them raises ``AttributeError`` at the point of the mistake, loudly, instead
of rendering a reassuring sentence. :func:`friction_state` is the total
function for renderers that need one, and it returns three values, never two.

A refusal then has a second question after "what could we not assess": *who can
fix it*, and those are not the same set. Closed and finishable differ whenever
the reason is an outcome rather than a gap — a type16 suppressed by its Big Five
source, a quality-flagged sitting, a declined Enneagram. Telling somebody to
finish one of those is an instruction they cannot follow, printed in a document
their colleagues read, so the two sets get two names:
:attr:`RefusedDimension.subjects_with_closed_gates` and
:func:`subjects_to_finish`. The refusals block and the completeness block share
the second one; they did not, and the document contradicted itself.

## 5. The no-ranking screen

:func:`screen_no_ranking` is additive to ``guide_prompts.screen_prose``, not a
replacement for it. It exists because ``guide_prompts._SELF_SUPERLATIVE`` is
first-person-scoped: it catches "I am the strongest" and does not catch "Blair is
better at this than Alex", which is the sentence a two-person document actually
produces. It also catches the compatibility score ("approximately 75% synergy"),
which is the direct on-ramp from "get me the best team" to "rank my employees".
It lives here rather than in ``guide_prompts`` because ST-9's chat report never
goes through the guide screens and needs the same rule.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Mapping, Sequence
from typing import Literal, get_args

from rubik_core.smart_teams import contracts
from rubik_core.smart_teams.instruments import (
    big_five,
    disc,
    enneagram,
    motivators,
    strengths,
    type16,
)

__all__ = [
    "ComparisonError",
    "GATE_BOTH_OPEN",
    "GATE_ONE_REFUSED",
    "GATE_NEITHER_OPEN",
    "JOINT_GATES",
    "JointGate",
    "joint_gate",
    "PairwiseRead",
    "comparable_frameworks",
    "DimensionSource",
    "Dimension",
    "DIMENSIONS",
    "DIMENSION_KEYS",
    "dimension_for",
    "Evidence",
    "Synergy",
    "Friction",
    "MIN_SYNERGY_CONFIDENCE",
    "MIN_FRICTION_CONFIDENCE",
    "AssessedDimension",
    "RefusedDimension",
    "DimensionOutcome",
    "friction_state",
    "subjects_to_finish",
    "FRICTION_FOUND",
    "FRICTION_NONE_FOUND",
    "FRICTION_NOT_ASSESSED",
    "PairComparison",
    "REJECT_CROSS_SUBJECT_RANKING",
    "REJECT_COMPATIBILITY_SCORE",
    "screen_no_ranking",
    "PAIR_ADVISORY_NOTICE",
    "NO_OBSERVATION_NOTICE",
]


class ComparisonError(ValueError):
    """A comparison shape does not match the contract in this module."""


def _fold(email: object) -> str:
    """Lower-case and strip an address. The same fold the database columns use."""
    return str(email or "").strip().lower()


# ── The joint gate ────────────────────────────────────────────────────────────
# Three states out of four combinations, and the asymmetric middle state is the
# reason this is not a Boolean. See §1 of the module docstring.

#: Both subjects' layers are reportable. The only state a comparison may assert
#: from.
GATE_BOTH_OPEN = "both_open"

#: Exactly one subject's layer is reportable. The comparison refuses — and this
#: is the state that gets its own completeness line, because the person who has
#: not finished can do something about it.
GATE_ONE_REFUSED = "one_refused"

#: Neither subject's layer is reportable. Also a refusal, and a different
#: sentence: nobody is waiting on anybody.
GATE_NEITHER_OPEN = "neither_open"

JOINT_GATES: tuple[str, ...] = (GATE_BOTH_OPEN, GATE_ONE_REFUSED, GATE_NEITHER_OPEN)

JointGate = Literal["both_open", "one_refused", "neither_open"]

assert set(get_args(JointGate)) == set(JOINT_GATES), (
    "JointGate and JOINT_GATES disagree; a gate added to one must be added to "
    "the other in the same change"
)


def joint_gate(a: contracts.LayerVerdict, b: contracts.LayerVerdict) -> JointGate:
    """The joint gate for one framework across two subjects.

    All four combinations of the two ``reportable`` flags map here, and they map
    onto three states because *which* of the two refused is not a property of
    the gate — it is a property of the :class:`PairwiseRead`, which knows the
    two email addresses.

    Raises when the two verdicts are about different frameworks: comparing
    somebody's DISC gate against somebody else's Big Five gate is not a
    comparison, it is a bug that would silently open a closed layer.
    """
    if a.framework != b.framework:
        raise ComparisonError(
            f"joint_gate got verdicts for {a.framework!r} and {b.framework!r}; a "
            "joint gate is per-framework and the two sides must agree"
        )
    if a.reportable and b.reportable:
        return GATE_BOTH_OPEN
    if a.reportable or b.reportable:
        return GATE_ONE_REFUSED
    return GATE_NEITHER_OPEN


@dataclasses.dataclass(frozen=True)
class PairwiseRead:
    """For one framework and two subjects: what may be said, and from what.

    ``subject_a`` is the person the document is written *to* and ``subject_b``
    the person it is written *about*. The order is load-bearing and is not
    sorted, exactly as ``database.smart_teams.subject_key_for("pair_guide", …)``
    does not sort: ``(A, B)`` and ``(B, A)`` are two different documents.

    ``layer_a`` / ``layer_b`` hold the instruments' own ``as_dict()`` contents,
    and they are ``None`` unless :attr:`comparable`. That invariant is the
    compounding gate expressed as a type rather than as a convention: with a
    one-sided read there is no way to reach the open subject's layer and write a
    sentence that silently compares it against a default. Build these with
    :meth:`from_payloads`, which applies it for you.

    What this class deliberately does NOT carry is ``hedge``. Each instrument's
    refusal hedge is written for *its own subject's* eyes — the motivators hedge
    names their top two and their bottom axis, the Enneagram hedge names both
    candidate types — so rendering a hedge into a document a colleague reads
    publishes exactly the content the closed gate was protecting. The refusal
    reasons are here (they are machine codes, and each subject's own published
    guide already states them); the prose is not.
    """

    framework: str
    subject_a: str
    subject_b: str
    verdict_a: contracts.LayerVerdict
    verdict_b: contracts.LayerVerdict
    layer_a: Mapping[str, object] | None = None
    layer_b: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if self.framework not in contracts.FRAMEWORKS:
            raise ComparisonError(
                f"unknown framework {self.framework!r}; expected one of "
                f"{contracts.FRAMEWORKS}"
            )
        for name, verdict in (
            ("verdict_a", self.verdict_a),
            ("verdict_b", self.verdict_b),
        ):
            if verdict.framework != self.framework:
                raise ComparisonError(
                    f"{name} is about {verdict.framework!r} but this read is "
                    f"about {self.framework!r}"
                )
        for name, email in (
            ("subject_a", self.subject_a),
            ("subject_b", self.subject_b),
        ):
            if not email or email != _fold(email):
                raise ComparisonError(
                    f"{name} {email!r} is empty or not folded; every Smart Teams "
                    "email column is lower-cased and an unfolded address makes "
                    "one person two people"
                )
        if self.subject_a == self.subject_b:
            raise ComparisonError(
                f"a pairwise read needs two people; both sides are {self.subject_a!r}"
            )
        if self.gate is not GATE_BOTH_OPEN and (
            self.layer_a is not None or self.layer_b is not None
        ):
            raise ComparisonError(
                f"{self.framework}: joint gate is {self.gate!r} and a layer is "
                "still attached. A comparison may only reach layer contents when "
                "the framework is reportable for BOTH subjects — one real value "
                "against a default is the defect this type exists to prevent"
            )

    @property
    def gate(self) -> JointGate:
        """The joint gate. See :func:`joint_gate`."""
        return joint_gate(self.verdict_a, self.verdict_b)

    @property
    def comparable(self) -> bool:
        """Whether this framework may be asserted about the two of them."""
        return self.gate == GATE_BOTH_OPEN

    @property
    def joint_confidence(self) -> contracts.Confidence:
        """The weaker of the two confidences, or ``"none"`` when not comparable.

        The floor, not an average: a comparison is only as firm as its weaker
        side, and averaging would let a high-confidence layer carry a coin-flip
        one into print.
        """
        if not self.comparable:
            return "none"
        return contracts.floor_confidence(
            self.verdict_a.confidence, self.verdict_b.confidence
        )

    @property
    def closed_for(self) -> tuple[str, ...]:
        """The subjects whose gate is closed, in ``(a, b)`` order.

        The completeness block is the one place a closed gate is spoken about,
        and it needs to name who still has something to finish. It must not
        name either person's *value* — that is what :attr:`layer_a` being
        ``None`` enforces.
        """
        closed = []
        if not self.verdict_a.reportable:
            closed.append(self.subject_a)
        if not self.verdict_b.reportable:
            closed.append(self.subject_b)
        return tuple(closed)

    @property
    def actionable_for(self) -> tuple[str, ...]:
        """The closed subjects who could OPEN their gate by finishing something.

        A subset of :attr:`closed_for`, in the same ``(a, b)`` order, and
        deliberately narrower than it: a layer refused for a reason outside
        ``contracts.ACTIONABLE_REASONS`` leaves its subject nothing to do. A
        type16 suppressed by a bad Big Five sitting, a quality-flagged sitting,
        a declined Enneagram — all are closed, none are finishable. This is the
        only list a document may tell somebody to act on, because the
        alternative is an instruction they cannot follow printed in a document
        their colleagues read.

        Per-subject rather than :attr:`is_actionable`, which is true if EITHER
        of them could act and therefore cannot answer "whose name goes on the
        line".
        """
        out: list[str] = []
        if not self.verdict_a.reportable and self.verdict_a.is_actionable:
            out.append(self.subject_a)
        if not self.verdict_b.reportable and self.verdict_b.is_actionable:
            out.append(self.subject_b)
        return tuple(out)

    @property
    def reasons_by_subject(self) -> Mapping[str, tuple[str, ...]]:
        """Refusal reason codes per subject. Machine codes, never prose."""
        return {
            self.subject_a: self.verdict_a.reasons,
            self.subject_b: self.verdict_b.reasons,
        }

    @property
    def is_actionable(self) -> bool:
        """Whether either subject could open this gate by doing something."""
        return self.verdict_a.is_actionable or self.verdict_b.is_actionable

    @classmethod
    def from_payloads(
        cls,
        framework: str,
        subject_a: str,
        payload_a: Mapping[str, object] | None,
        subject_b: str,
        payload_b: Mapping[str, object] | None,
    ) -> PairwiseRead:
        """Build a read from two stored envelopes, applying the joint gate.

        Both payloads go through :func:`contracts.read_verdict` and
        :func:`contracts.layer_of`, so there is no path to layer contents that
        skips the per-subject gate; the joint gate is then applied on top, and
        a one-sided read drops the open subject's layer on the floor. A ``None``
        payload — a framework column that was never written — is treated as
        :func:`contracts.not_taken_envelope`, which is the same handling
        ``guide.read_layers`` gives it.
        """
        verdicts: list[contracts.LayerVerdict] = []
        layers: list[Mapping[str, object] | None] = []
        for payload in (payload_a, payload_b):
            envelope = (
                payload
                if payload is not None
                else contracts.not_taken_envelope(framework)
            )
            verdicts.append(contracts.read_verdict(envelope))
            layers.append(contracts.layer_of(envelope))
        both_open = joint_gate(verdicts[0], verdicts[1]) == GATE_BOTH_OPEN
        return cls(
            framework=framework,
            subject_a=_fold(subject_a),
            subject_b=_fold(subject_b),
            verdict_a=verdicts[0],
            verdict_b=verdicts[1],
            layer_a=layers[0] if both_open else None,
            layer_b=layers[1] if both_open else None,
        )


def comparable_frameworks(reads: Mapping[str, PairwiseRead]) -> frozenset[str]:
    """The frameworks a pair document may NAME — the intersection, never a union.

    This is the value that goes into ``guide_prompts.ProseSlot.open_frameworks``
    for a pair guide, and it is the single highest-leverage line in ST-7.
    ``guide_prompts._screen_slot`` rule 1 refuses any framework named in prose
    that is not in that set, so passing the union — or subject A's own set —
    silently licenses the model to name a layer subject B refused. Passing the
    intersection makes the payload-gate compounding rule mechanical.
    """
    return frozenset(framework for framework, read in reads.items() if read.comparable)


# ── The dimension vocabulary ──────────────────────────────────────────────────
# Derived from the instruments, checked against them at import. See §2.


def _instrument_field_names(*classes: type) -> frozenset[str]:
    """Every attribute name a set of instrument classes can put in a layer dict.

    Dataclass fields plus properties, because several instruments expose
    derived reads (``top_two``, ``bottom``, ``type_display``, ``work_style``) as
    properties and those are what land in ``as_dict()``. Derived rather than
    listed so a rename in an instrument breaks this import instead of quietly
    making a dimension cite a field that no longer exists.
    """
    names: set[str] = set()
    for cls in classes:
        if dataclasses.is_dataclass(cls):
            names.update(field.name for field in dataclasses.fields(cls))
        for attribute, value in vars(cls).items():
            if isinstance(value, property) or type(value).__name__ == "cached_property":
                names.add(attribute)
    return frozenset(names)


_INSTRUMENT_FIELD_NAMES: Mapping[str, frozenset[str]] = {
    "disc": _instrument_field_names(
        disc.DiscResult, disc.WorkStyle, disc.DimensionScore, disc.StyleAssignment
    ),
    "big_five": _instrument_field_names(big_five.BigFiveResult, big_five.DomainScore),
    "type16": _instrument_field_names(
        type16.Type16Result, type16.LetterCall, type16.AxisSpec
    ),
    "strengths": _instrument_field_names(
        strengths.StrengthsRead, strengths.ThemeRead, strengths.ThemeSpec
    ),
    "motivators": _instrument_field_names(
        motivators.MotivatorResult, motivators.MotivatorScore, motivators.MotivatorSpec
    ),
    "enneagram": _instrument_field_names(
        enneagram.EnneagramRead, enneagram.TypeScore, enneagram.TypeSpec
    ),
}

assert set(_INSTRUMENT_FIELD_NAMES) == set(contracts.FRAMEWORKS), (
    "_INSTRUMENT_FIELD_NAMES must cover exactly contracts.FRAMEWORKS"
)

#: Fields that carry a NUMBER rather than an authored sentence. A comparison may
#: never cite one: raw scores and intensities are subject + support admins only, and
#: ``guide_prompts._RAW_DOMAIN_SCORE`` / ``_NUMERIC_VERDICT`` would refuse the
#: prose anyway — this refuses the citation, one layer earlier, where the error
#: message can name the field.
_RAW_VALUE_FIELDS: frozenset[str] = frozenset(
    {
        "score",
        "scores",
        "raw_total",
        "intensity",
        "intensities",
        "emphasis",
        "margin",
        "primary_margin",
        "secondary_margin",
        "top_two_margin",
        "spread",
        "likert_raw",
        "likert_percent",
        "choice_percent",
        "choice_wins",
        "source_score",
        "distance_from_midpoint",
        "neuroticism_equivalent",
        "acquiescence_index",
    }
)


@dataclasses.dataclass(frozen=True)
class DimensionSource:
    """Where one framework's contribution to a dimension comes from.

    ``layer_fields`` are keys reachable inside :func:`contracts.layer_of`'s
    return value. ``spec_fields`` are authored fields on the instrument's own
    spec object, looked up from an id that IS in the layer — the strengths
    ``ask_for`` sentence, for instance, lives on
    :class:`strengths.ThemeSpec` and is found via the layer's ``theme_id``. The
    distinction matters because a spec field is authored copy that is identical
    for everybody with that theme, while a layer field is this person's answer.
    """

    framework: str
    layer_fields: tuple[str, ...] = ()
    spec_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.framework not in contracts.FRAMEWORKS:
            raise ComparisonError(f"unknown framework {self.framework!r}")
        known = _INSTRUMENT_FIELD_NAMES[self.framework]
        for field in (*self.layer_fields, *self.spec_fields):
            if field not in known:
                raise ComparisonError(
                    f"{self.framework}.{field} is not a field or property of that "
                    "instrument's classes; a dimension may not cite a field that "
                    "does not exist"
                )
            if field in _RAW_VALUE_FIELDS:
                raise ComparisonError(
                    f"{self.framework}.{field} is a raw value; a comparison may "
                    "not be sourced from one (subject + support admins only)"
                )
        if not self.layer_fields and not self.spec_fields:
            raise ComparisonError(
                f"{self.framework}: a dimension source with no fields cites "
                "nothing and would license the framework's whole vocabulary"
            )


@dataclasses.dataclass(frozen=True)
class Dimension:
    """One thing a comparison actually assesses.

    ``question`` is the plain-English question the dimension answers, and it is
    what the refusal renders instead of a finding — "Cubie cannot speak to how
    settled the plan needs to be before work starts" is a sentence; "type16 is
    closed" is not.

    ``may_name_derived_type`` is ``False`` for every dimension except none of
    them: it exists so the Enneagram dimension carries the restriction in the
    type rather than in a comment. ``guide_prompts._ENNEAGRAM_ASSERTION`` refuses
    a type assertion in prose and ``type16.find_unqualified_codes`` refuses a
    bare four-letter code, so a dimension sourced from either instrument may use
    the authored *meaning* fields and never the label.
    """

    key: str
    label: str
    question: str
    sources: tuple[DimensionSource, ...]
    #: Whether prose on this dimension may print a derived type name or code.
    #: Always ``False``. A derived type renders only with its qualifier and only
    #: in the guide's own derived-layer section, never inside a comparison.
    may_name_derived_type: bool = False

    def __post_init__(self) -> None:
        if not self.sources:
            raise ComparisonError(f"dimension {self.key!r} has no sources")
        if self.may_name_derived_type:
            raise ComparisonError(
                f"dimension {self.key!r} sets may_name_derived_type; a comparison "
                "never names a derived type or a four-letter code — it uses the "
                "authored meaning fields"
            )
        seen = [source.framework for source in self.sources]
        if len(seen) != len(set(seen)):
            raise ComparisonError(
                f"dimension {self.key!r} lists a framework twice; merge the "
                "field lists instead"
            )

    @property
    def frameworks(self) -> tuple[str, ...]:
        """The frameworks this dimension can be read from, in preference order."""
        return tuple(source.framework for source in self.sources)

    def is_open(self, reads: Mapping[str, PairwiseRead]) -> bool:
        """Whether ANY of this dimension's frameworks is comparable for the pair.

        Any, not all: a dimension with three sources is answerable from one of
        them. What it may never do is answer from a framework that is
        comparable for only one of the two — that is refused one level down, in
        :class:`PairwiseRead`.
        """
        return any(
            framework in reads and reads[framework].comparable
            for framework in self.frameworks
        )


#: The dimension vocabulary. Ten dimensions, each grounded in fields that exist.
#:
#: Ordered as a pair document reads: how you talk to each other, how you decide,
#: how much plan and how much detail you each need, how feedback lands, what
#: happens on a bad week, what fills and empties the tank, what makes the work
#: worth doing, what each of you is for, and what each of you needs from a
#: colleague.
DIMENSIONS: tuple[Dimension, ...] = (
    Dimension(
        key="communication_pace",
        label="Communication pace",
        question="How each of you wants information delivered, and how fast.",
        sources=(
            DimensionSource(
                framework="disc",
                layer_fields=("style", "communication", "identity", "name"),
            ),
            DimensionSource(
                framework="type16",
                layer_fields=("calls", "axis", "letter", "confidence"),
                spec_fields=("high_meaning", "low_meaning"),
            ),
            DimensionSource(
                framework="big_five", layer_fields=("domains", "band", "description")
            ),
        ),
    ),
    Dimension(
        key="decision_style",
        label="Decision style",
        question="What each of you weighs when a call has to be made.",
        sources=(
            DimensionSource(
                framework="type16",
                layer_fields=("calls", "axis", "letter", "confidence"),
                spec_fields=("high_meaning", "low_meaning"),
            ),
            DimensionSource(
                framework="disc",
                layer_fields=("style", "identity", "primary", "secondary"),
            ),
            DimensionSource(
                framework="big_five", layer_fields=("domains", "band", "description")
            ),
        ),
    ),
    Dimension(
        key="structure_preference",
        label="Structure preference",
        question="How settled the plan needs to be before the work starts.",
        sources=(
            DimensionSource(
                framework="type16",
                layer_fields=("calls", "axis", "letter", "confidence"),
                spec_fields=("high_meaning", "low_meaning"),
            ),
            DimensionSource(
                framework="disc",
                layer_fields=("style", "energized_by", "drained_by"),
            ),
            DimensionSource(
                framework="big_five", layer_fields=("domains", "band", "description")
            ),
        ),
    ),
    Dimension(
        key="information_depth",
        label="Information depth",
        question=(
            "How much detail and evidence each of you needs before believing a "
            "recommendation."
        ),
        sources=(
            DimensionSource(
                framework="type16",
                layer_fields=("calls", "axis", "letter", "confidence"),
                spec_fields=("high_meaning", "low_meaning"),
            ),
            DimensionSource(framework="disc", layer_fields=("style", "communication")),
            DimensionSource(
                framework="big_five", layer_fields=("domains", "band", "description")
            ),
        ),
    ),
    Dimension(
        key="feedback_exchange",
        label="Feedback",
        question="How each of you wants feedback given, and how it lands.",
        sources=(
            DimensionSource(framework="disc", layer_fields=("style", "feedback")),
        ),
    ),
    Dimension(
        key="stress_response",
        label="Under pressure",
        question="What each of you does when the week goes badly.",
        sources=(
            DimensionSource(framework="disc", layer_fields=("style", "under_stress")),
            DimensionSource(
                framework="strengths",
                layer_fields=("themes", "theme_id"),
                spec_fields=("under_pressure", "overused"),
            ),
        ),
    ),
    Dimension(
        key="energy_and_drain",
        label="Energy and drain",
        question="What fills each of your tanks, and what empties them.",
        sources=(
            DimensionSource(
                framework="disc",
                layer_fields=("style", "energized_by", "drained_by"),
            ),
        ),
    ),
    Dimension(
        key="motivator_alignment",
        label="Motivator alignment",
        question="What makes the work feel worth doing to each of you.",
        sources=(
            DimensionSource(
                framework="motivators",
                layer_fields=("ranking", "top_two", "bottom", "motivator"),
                spec_fields=("high_read", "low_read", "in_conflict", "definition"),
            ),
        ),
    ),
    Dimension(
        key="contribution_mix",
        label="Contribution mix",
        question="What each of you is most useful for, and what neither covers.",
        sources=(
            DimensionSource(
                framework="strengths",
                layer_fields=("themes", "theme_id", "clusters_covered", "cluster"),
                spec_fields=("definition", "ask_for", "anchors"),
            ),
        ),
    ),
    Dimension(
        key="interpersonal_needs",
        label="What each of you needs from a colleague",
        question="What each of you needs from the person on the other side of the work.",
        sources=(
            DimensionSource(
                framework="enneagram",
                layer_fields=("center", "type_number"),
                spec_fields=("needs_from_colleagues", "workplace_signature"),
            ),
            DimensionSource(
                framework="strengths",
                layer_fields=("themes", "theme_id"),
                spec_fields=("ask_for",),
            ),
        ),
    ),
)

DIMENSION_KEYS: tuple[str, ...] = tuple(d.key for d in DIMENSIONS)

assert len(DIMENSION_KEYS) == len(set(DIMENSION_KEYS)), "duplicate dimension key"

_DIMENSIONS_BY_KEY: Mapping[str, Dimension] = {d.key: d for d in DIMENSIONS}

# Every framework must be reachable from at least one dimension. A layer nobody
# can compare on is a layer that silently never appears in a pair guide, and the
# reader would read its absence as "nothing found".
_SOURCED = {source.framework for d in DIMENSIONS for source in d.sources}
assert _SOURCED == set(contracts.FRAMEWORKS), (
    "every framework must feed at least one dimension; missing "
    f"{sorted(set(contracts.FRAMEWORKS) - _SOURCED)}"
)


def dimension_for(key: str) -> Dimension:
    """The :class:`Dimension` a key names. Raises on an unknown key."""
    try:
        return _DIMENSIONS_BY_KEY[key]
    except KeyError:
        raise ComparisonError(
            f"unknown dimension {key!r}; expected one of {DIMENSION_KEYS}"
        ) from None


# ── Claims ────────────────────────────────────────────────────────────────────

#: A synergy may be asserted down to ``"low"``: a weak dovetail between two
#: authored sentences is still a fact about two sentences, and the sentence it
#: produces costs nothing if it is a stretch.
MIN_SYNERGY_CONFIDENCE: contracts.Confidence = "low"

#: A friction may NOT. See §3 — a friction is a claim that one colleague should
#: expect trouble from another, and the cost of being wrong is not symmetric
#: with a synergy's. A collision derived from a coin-flip letter or a thin
#: transcript is refused rather than softened.
MIN_FRICTION_CONFIDENCE: contracts.Confidence = "moderate"

assert (
    contracts.floor_confidence(MIN_FRICTION_CONFIDENCE, MIN_SYNERGY_CONFIDENCE)
    == MIN_SYNERGY_CONFIDENCE
), (
    "the friction bar must be strictly stronger than the synergy bar; if these "
    "are ever equal, delete one of them and say so"
)


def _meets(confidence: contracts.Confidence, floor: contracts.Confidence) -> bool:
    """Whether ``confidence`` is at or above ``floor``, per the shared ranking."""
    return contracts.floor_confidence(confidence, floor) == floor


@dataclasses.dataclass(frozen=True)
class Evidence:
    """One authored field, from one named subject, that a claim rests on.

    ``text`` is the instrument's own authored sentence, verbatim. It is not
    model-written and it is not a score: the field name is checked against the
    instrument's real attributes and against :data:`_RAW_VALUE_FIELDS`, so a
    caller cannot cite somebody's intensity or domain score as evidence even by
    accident. That check is here rather than in the renderer because this is the
    object ST-9 receives, and ST-9 does not run the guide prose screens.
    """

    subject_email: str
    framework: str
    field: str
    text: str

    def __post_init__(self) -> None:
        if not self.subject_email or self.subject_email != _fold(self.subject_email):
            raise ComparisonError(
                f"Evidence.subject_email {self.subject_email!r} is empty or not folded"
            )
        if self.framework not in contracts.FRAMEWORKS:
            raise ComparisonError(f"unknown framework {self.framework!r}")
        known = _INSTRUMENT_FIELD_NAMES[self.framework]
        if self.field not in known:
            raise ComparisonError(
                f"{self.framework}.{self.field} is not a field of that instrument"
            )
        if self.field in _RAW_VALUE_FIELDS:
            raise ComparisonError(
                f"{self.framework}.{self.field} is a raw value and may not be "
                "cited as evidence in a comparison"
            )
        if not str(self.text).strip():
            raise ComparisonError(
                f"{self.framework}.{self.field}: evidence with no text cites nothing"
            )


def _validated_claim(
    *,
    kind: str,
    dimension: str,
    subjects: tuple[str, str],
    statement: str,
    confidence: contracts.Confidence,
    evidence: Sequence[Evidence],
    floor: contracts.Confidence,
) -> None:
    """The invariants a synergy and a friction share. Raises on a violation."""
    dimension_for(dimension)
    if len(subjects) != 2:
        raise ComparisonError(f"{kind} {dimension!r}: subjects must be a pair")
    for email in subjects:
        if not email or email != _fold(email):
            raise ComparisonError(
                f"{kind} {dimension!r}: subject {email!r} is empty or not folded"
            )
    if subjects[0] == subjects[1]:
        raise ComparisonError(
            f"{kind} {dimension!r}: both subjects are {subjects[0]!r}"
        )
    if not str(statement).strip():
        raise ComparisonError(f"{kind} {dimension!r}: empty statement")
    if confidence not in contracts.CONFIDENCE_VALUES:
        raise ComparisonError(
            f"{kind} {dimension!r}: confidence {confidence!r} is not one of "
            f"{contracts.CONFIDENCE_VALUES}"
        )
    if not _meets(confidence, floor):
        raise ComparisonError(
            f"{kind} {dimension!r}: confidence {confidence!r} is below the "
            f"{kind} floor {floor!r}"
        )
    cited = {item.subject_email for item in evidence}
    missing = [email for email in subjects if email not in cited]
    if missing:
        raise ComparisonError(
            f"{kind} {dimension!r}: no evidence cited for {missing!r}. A claim "
            "about two people rests on a field from each of them; one-sided "
            "evidence is a claim about one person in a comparison's clothes"
        )
    stray = cited - set(subjects)
    if stray:
        raise ComparisonError(
            f"{kind} {dimension!r}: evidence cites {sorted(stray)!r}, who are not "
            "in this pair"
        )


@dataclasses.dataclass(frozen=True)
class Synergy:
    """A dovetail between two authored fields: one asks, the other supplies.

    ``statement`` is a fact about two texts, not a prediction about two people.
    "He supplies possibility; you help make people care about it" is legal
    because both halves are quotable from the two profiles. "He is unlikely to
    be threatened by your point of view" is not, and no type can stop that one —
    it is refused by the prose screens and by the pair prompt. What the type
    stops is the shape underneath: a synergy with evidence from only one of the
    two, or asserted on a dimension nobody can read.
    """

    dimension: str
    subjects: tuple[str, str]
    statement: str
    confidence: contracts.Confidence
    evidence: tuple[Evidence, ...]

    def __post_init__(self) -> None:
        _validated_claim(
            kind="synergy",
            dimension=self.dimension,
            subjects=self.subjects,
            statement=self.statement,
            confidence=self.confidence,
            evidence=self.evidence,
            floor=MIN_SYNERGY_CONFIDENCE,
        )

    @property
    def frameworks(self) -> frozenset[str]:
        """The frameworks this claim names. Must be a subset of the comparable set."""
        return frozenset(item.framework for item in self.evidence)


@dataclasses.dataclass(frozen=True)
class Friction:
    """A collision between two STATED PREFERENCES. Never a predicted feeling.

    A friction is the stronger claim in this module and carries three extra
    obligations, all enforced here:

    1. **A higher confidence bar** — :data:`MIN_FRICTION_CONFIDENCE`.
    2. **Two distinct field operands.** A friction is "these two authored fields
       ask for incompatible things", so it needs a field from each side. One
       field cannot collide with itself.
    3. **An ask of both people.** ``ask_of_a`` and ``ask_of_b`` are both
       required and both non-empty. The format this feature is modelled on put
       five of its six remedies on the reader alone, because one of the two
       people in it outranked the other; that deference gradient is prevented
       structurally rather than by prompt wording, because prompt wording is
       what failed.

    There is deliberately **no severity, rank, or order** field. Naming one
    tension "your biggest risk" is an inference two self-reports cannot support,
    and it reads as assigning blame. There is also no predicted reaction: a
    friction says what two preferences ask for, never what one person will feel
    when the other does something.
    """

    dimension: str
    subjects: tuple[str, str]
    statement: str
    confidence: contracts.Confidence
    evidence: tuple[Evidence, ...]
    ask_of_a: str
    ask_of_b: str

    def __post_init__(self) -> None:
        _validated_claim(
            kind="friction",
            dimension=self.dimension,
            subjects=self.subjects,
            statement=self.statement,
            confidence=self.confidence,
            evidence=self.evidence,
            floor=MIN_FRICTION_CONFIDENCE,
        )
        operands = {(item.framework, item.field) for item in self.evidence}
        if len(operands) < 2:
            raise ComparisonError(
                f"friction {self.dimension!r}: needs two distinct (framework, "
                "field) operands — a collision is between two stated "
                "preferences, and one field cannot collide with itself"
            )
        for name, ask in (("ask_of_a", self.ask_of_a), ("ask_of_b", self.ask_of_b)):
            if not str(ask).strip():
                raise ComparisonError(
                    f"friction {self.dimension!r}: {name} is empty. Every friction "
                    "carries an ask of BOTH people; a one-sided remedy makes one "
                    "of them the party who adapts"
                )

    @property
    def frameworks(self) -> frozenset[str]:
        """The frameworks this claim names."""
        return frozenset(item.framework for item in self.evidence)

    @property
    def asks(self) -> Mapping[str, str]:
        """``{subject_email: ask}`` — both entries always present."""
        return {self.subjects[0]: self.ask_of_a, self.subjects[1]: self.ask_of_b}


# ── Outcomes: assessed, or refused. Two types, on purpose. ────────────────────

FRICTION_FOUND = "found"
FRICTION_NONE_FOUND = "none_found"
FRICTION_NOT_ASSESSED = "not_assessed"

FRICTION_STATES: tuple[str, ...] = (
    FRICTION_FOUND,
    FRICTION_NONE_FOUND,
    FRICTION_NOT_ASSESSED,
)


@dataclasses.dataclass(frozen=True)
class AssessedDimension:
    """A dimension both subjects' gates were open for, with what was found.

    Empty ``frictions`` here is a real answer: we looked and the two sets of
    stated preferences do not collide on this dimension. That is exactly why the
    refused case is a different class — see :class:`RefusedDimension`.
    """

    dimension: str
    synergies: tuple[Synergy, ...] = ()
    frictions: tuple[Friction, ...] = ()

    def __post_init__(self) -> None:
        dimension_for(self.dimension)
        for claim in (*self.synergies, *self.frictions):
            if claim.dimension != self.dimension:
                raise ComparisonError(
                    f"{self.dimension!r} holds a claim about {claim.dimension!r}"
                )

    @property
    def assessed(self) -> bool:
        return True

    @property
    def no_friction_found(self) -> bool:
        """True only when this dimension WAS assessed and nothing collided.

        The property exists so that the sentence "nothing to watch here" has
        exactly one source, and that source is unreachable on a refusal.
        """
        return not self.frictions

    @property
    def has_content(self) -> bool:
        return bool(self.synergies or self.frictions)


@dataclasses.dataclass(frozen=True)
class RefusedDimension:
    """A dimension that could not be assessed, and why — with NO findings.

    This class deliberately has no ``synergies``, no ``frictions``, and no
    ``no_friction_found``. That is the safety property, not an oversight: the
    documented failure is a renderer reading an empty friction list as "no
    friction found" when the truth is "one of you has not published this layer".
    Empty and unknown are the same value in Python, so they are different types
    here, and a renderer that reaches for ``.frictions`` on a refusal gets an
    ``AttributeError`` at the mistake instead of a reassuring sentence in a
    company-visible document.

    ``closed_for`` is ``{framework: (emails,)}``. It names who has a gate closed
    — which the completeness block needs, and which each subject's own published
    guide already states — and never what is behind it.
    """

    dimension: str
    gate: JointGate
    closed_for: Mapping[str, tuple[str, ...]]
    note: str

    def __post_init__(self) -> None:
        dimension = dimension_for(self.dimension)
        if self.gate not in JOINT_GATES:
            raise ComparisonError(
                f"{self.dimension!r}: gate {self.gate!r} is not one of {JOINT_GATES}"
            )
        if self.gate == GATE_BOTH_OPEN:
            raise ComparisonError(
                f"{self.dimension!r}: a refusal cannot carry {GATE_BOTH_OPEN!r}. If "
                "both gates are open the dimension was assessed, and an empty "
                "AssessedDimension is how 'we looked and found nothing' is said"
            )
        allowed = set(dimension.frameworks)
        unknown = [name for name in self.closed_for if name not in allowed]
        if unknown:
            raise ComparisonError(
                f"{self.dimension!r}: closed_for names {unknown!r}, which do not "
                f"feed this dimension (its sources are {sorted(allowed)})"
            )
        if not str(self.note).strip():
            raise ComparisonError(
                f"{self.dimension!r}: a refusal with no note renders as a gap the "
                "reader will explain to themselves"
            )

    @property
    def assessed(self) -> bool:
        return False

    @property
    def has_content(self) -> bool:
        return False

    @property
    def subjects_with_closed_gates(self) -> tuple[str, ...]:
        """Everyone with a closed gate on this dimension, folded and deduplicated.

        NOT the list to print under "still to finish", and it was named
        ``subjects_to_finish`` once, which is how it came to be printed there:
        a closed gate and a finishable gate are different questions, and this
        one answers the first. :func:`subjects_to_finish` answers the second and
        needs the reads to do it.
        """
        out: list[str] = []
        for emails in self.closed_for.values():
            for email in emails:
                if email not in out:
                    out.append(email)
        return tuple(out)


#: A dimension outcome is one of two types, and the caller must branch. There is
#: no third state and no flag-carrying superclass — see the note on
#: :class:`RefusedDimension`.
DimensionOutcome = AssessedDimension | RefusedDimension


def friction_state(outcome: DimensionOutcome) -> str:
    """The total function over :data:`FRICTION_STATES`. Three answers, never two.

    Renderers that want a single expression call this instead of testing
    truthiness on a list. ``FRICTION_NOT_ASSESSED`` and
    ``FRICTION_NONE_FOUND`` are different sentences and different next steps,
    and any code path that cannot tell them apart is the bug.
    """
    if isinstance(outcome, RefusedDimension):
        return FRICTION_NOT_ASSESSED
    if isinstance(outcome, AssessedDimension):
        return FRICTION_FOUND if outcome.frictions else FRICTION_NONE_FOUND
    raise ComparisonError(
        f"{type(outcome).__name__} is not a dimension outcome; expected "
        "AssessedDimension or RefusedDimension"
    )


def subjects_to_finish(
    refusal: RefusedDimension, reads: Mapping[str, PairwiseRead]
) -> tuple[str, ...]:
    """Who could open this refused dimension by finishing something. One definition.

    Shared by the refusals block and the per-framework completeness block,
    because the two used to disagree and the disagreement was visible in the
    document: the completeness line filtered its names through per-subject
    actionability and the refusal line printed
    ``RefusedDimension.subjects_to_finish`` — every closed subject, whatever the
    reason. So the same guide told somebody to go finish a layer the section
    below it knew they could not, most reliably on the four dimensions fed by
    type16, whose gate closes as ``suppressed_by_source`` off a Big Five sitting
    and can only be reopened by redoing Big Five.

    Takes the reads because a refusal deliberately carries no verdicts:
    ``closed_for`` names who is closed and nothing about why. A framework
    missing from ``reads`` contributes nobody rather than raising — the caller
    that has a refusal in hand has already been through
    :class:`PairComparison`, which requires a read per framework, and a
    KeyError here would take down a whole document over a name in a footnote.

    Returns names in ``closed_for`` order, deduplicated: a dimension fed by
    three frameworks can close on all three for the same person.
    """
    out: list[str] = []
    for framework, emails in refusal.closed_for.items():
        read = reads.get(framework)
        if read is None:
            continue
        actionable = read.actionable_for
        for email in emails:
            if email in actionable and email not in out:
                out.append(email)
    return tuple(out)


@dataclasses.dataclass(frozen=True)
class PairComparison:
    """Everything a pair document may be built from, for one ordered pair.

    ``outcomes`` holds **one entry per dimension in :data:`DIMENSIONS`, in
    canonical order, always** — the same rule ``contracts.ScoringOutput``
    applies to frameworks, for the same reason: an omitted dimension is
    indistinguishable from a dimension that failed, and the reader will assume
    the more interesting explanation.

    ``document_floor_met`` mirrors ``guide.py``'s shortfall path. Zero synergies
    and zero frictions across every dimension means the honest document is the
    short one — title, framing, completeness, advisory — and never a thin pair
    padded out with authored filler.
    """

    subject_a: str
    subject_b: str
    reads: Mapping[str, PairwiseRead]
    outcomes: tuple[DimensionOutcome, ...]

    def __post_init__(self) -> None:
        for name, email in (
            ("subject_a", self.subject_a),
            ("subject_b", self.subject_b),
        ):
            if not email or email != _fold(email):
                raise ComparisonError(f"{name} {email!r} is empty or not folded")
        if self.subject_a == self.subject_b:
            raise ComparisonError("a pair comparison needs two people")
        unknown = [name for name in self.reads if name not in contracts.FRAMEWORKS]
        if unknown:
            raise ComparisonError(f"reads names unknown frameworks {unknown!r}")
        missing = [name for name in contracts.FRAMEWORKS if name not in self.reads]
        if missing:
            raise ComparisonError(
                f"reads is missing frameworks {missing!r}; every layer gets a "
                "PairwiseRead, and a layer with no answers gets one built from "
                "contracts.not_taken_envelope rather than being omitted"
            )
        for framework, read in self.reads.items():
            if (read.subject_a, read.subject_b) != (self.subject_a, self.subject_b):
                raise ComparisonError(
                    f"{framework}: read is about "
                    f"({read.subject_a!r}, {read.subject_b!r}) but this comparison "
                    f"is about ({self.subject_a!r}, {self.subject_b!r}); pair order "
                    "is load-bearing and must not be flipped mid-document"
                )
        got = tuple(outcome.dimension for outcome in self.outcomes)
        if got != DIMENSION_KEYS:
            raise ComparisonError(
                f"outcomes must hold every dimension in canonical order; got "
                f"{got!r}, expected {DIMENSION_KEYS!r}"
            )
        for outcome in self.outcomes:
            if not isinstance(outcome, (AssessedDimension, RefusedDimension)):
                raise ComparisonError(
                    f"{type(outcome).__name__} is not a dimension outcome"
                )

    @property
    def subjects(self) -> tuple[str, str]:
        return (self.subject_a, self.subject_b)

    @property
    def comparable_frameworks(self) -> frozenset[str]:
        """The intersection, for ``ProseSlot.open_frameworks``."""
        return comparable_frameworks(self.reads)

    @property
    def synergies(self) -> tuple[Synergy, ...]:
        return tuple(
            claim
            for outcome in self.outcomes
            if isinstance(outcome, AssessedDimension)
            for claim in outcome.synergies
        )

    @property
    def frictions(self) -> tuple[Friction, ...]:
        return tuple(
            claim
            for outcome in self.outcomes
            if isinstance(outcome, AssessedDimension)
            for claim in outcome.frictions
        )

    @property
    def refusals(self) -> tuple[RefusedDimension, ...]:
        return tuple(
            outcome
            for outcome in self.outcomes
            if isinstance(outcome, RefusedDimension)
        )

    @property
    def document_floor_met(self) -> bool:
        """Whether there is enough here to write more than the shortfall document."""
        return bool(self.synergies or self.frictions)


# ── The no-ranking screen ─────────────────────────────────────────────────────

REJECT_CROSS_SUBJECT_RANKING = "cross_subject_ranking"
REJECT_COMPATIBILITY_SCORE = "compatibility_score"

#: One person measured against another. ``guide_prompts._SELF_SUPERLATIVE`` is
#: first-person-scoped and catches none of this, because a two-person document
#: does not say "I am the strongest" — it says "Blair is better at this than
#: Alex", which is the same forbidden claim in the grammar this feature actually
#: produces.
_COMPARATIVE = re.compile(
    r"\b(?:better|worse|stronger|weaker|smarter|sharper|"
    r"more (?:capable|effective|skilled|competent|productive|senior|valuable)|"
    r"less (?:capable|effective|skilled|competent|productive|senior|valuable)|"
    r"higher[ -]performing|lower[ -]performing)"
    r"\b[^.!?]{0,60}\bthan\b",
    re.IGNORECASE,
)

#: Verbs that rank on their own — no "than" needed, because the comparison is in
#: the verb. "He outperforms her on delivery" is the whole forbidden claim in
#: four words.
_RANKING_VERB = re.compile(
    r"\b(?:outperform|outperforms|outperformed|outrank|outranks|outranked|"
    r"outshine|outshines|outshone|outclass|outclasses|outpace|outpaces)\b",
    re.IGNORECASE,
)

#: The graded adjectives that rank one person against others, comparative and
#: superlative. Kept as one alternation so the two patterns below cannot drift
#: apart on which words count.
#:
#: The ``most|least <attribute>`` half was added after the wave-2 verifier
#: measured that "the most experienced of the three" cleared this screen
#: entirely. ST-9's own ``team_prompts._EVALUATIVE_OF_PERSON`` happened to catch
#: it; the pair guide, whose only screen over an assembled body is this one, did
#: not — so the same forbidden claim was refused in one document about two people
#: and allowed in the other.
_GRADED = (
    r"strongest|weakest|best|worst|top|bottom|stronger|weaker|better|worse|"
    r"most capable|least capable|more capable|less capable|sharper|smarter|"
    r"(?:most|least)\s+(?:experienced|qualified|senior|junior|skilled|"
    r"competent|reliable)"
)

#: A graded adjective attached to a person-noun. Scoped that way so "the best
#: approach" and "the best time to raise it" — both legitimate advice — survive.
_SUPERLATIVE_OF_PEOPLE = re.compile(
    rf"\bthe (?:{_GRADED})\b"
    r"(?:\s+\w+){0,2}\s+"
    r"(?:person|people|employee|employees|colleague|colleagues|candidate|candidates|"
    r"member|members|performer|performers|hire)\b",
    re.IGNORECASE,
)

#: The same graded adjective pointed at a set the two subjects belong to —
#: "the stronger communicator of the two", "the best one here". This is the form
#: a pair document reaches for, because it does not need a person-noun to name
#: who lost.
#:
#: The COUNT-scoped alternative ("of the three", "of the 5") is the hole the
#: wave-2 verifier measured: the collective nouns below are what a pair document
#: says, and a team document says the number instead — so "the strongest of the
#: three" walked through a screen that refused "the strongest of the group".
_GRADED_OF_A_SET = re.compile(
    rf"\bthe (?:{_GRADED})\b[^.!?]{{0,40}}"
    r"\b(?:of the two|of you|of us|of them|of the pair|of the group|"
    r"on the team|in the group|in the pair|"
    r"(?:of|among)\s+the\s+(?:two|three|four|five|six|seven|eight|nine|ten|"
    r"\d+))\b",
    re.IGNORECASE,
)

#: Handing the work to somebody. Not a comparison and not a score, so none of
#: the patterns above sees it — and it is the single most actionable forbidden
#: sentence this feature can produce, because a lead can act on "X should own
#: the audit piece" without ever asking why Cubie thinks so.
#:
#: Shared rather than ST-9's alone: ``team_prompts`` already refused "should
#: lead" and the pair guide did not, which meant the pair document about two of
#: the same people was the softer target. The verb list is the same one
#: ``team_prompts._EVALUATIVE_OF_PERSON`` uses.
#:
#: ``team lead`` / ``team leader`` is here as a bare phrase rather than as a
#: verb form, because the noun IS the assignment: "the team lead on this is
#: Casey", "Casey as team lead" and "make Casey the team lead" are one claim in
#: three grammars, and none of them needs the word "should". Neither document
#: has any authored copy that says it — a pair guide and a staffing answer both
#: describe how people work, and Smart Teams does not know who reports to whom —
#: so the blunt form costs nothing here. ``weak(est) link`` is the phrase
#: ``team_contracts``' own docstring names as the failure mode it exists to
#: prevent, and it was not screened anywhere.
_LEADERSHIP_ASSIGNMENT = re.compile(
    r"\b(?:should|ought\s+to|needs?\s+to|has\s+to)\s+"
    r"(?:lead|run|own|head\s+up|drive)\b"
    r"|\b(?:obvious|natural|clear|right)\s+(?:choice|person|one)\s+to\s+"
    r"(?:lead|run|own|head\s+up|drive)\b"
    r"|\bteam\s+lead(?:er)?\b"
    r"|\bweak(?:est)?\s+link\b",
    re.IGNORECASE,
)

#: A compatibility score. "approximately 75% synergy and 25%
#: watch-your-blind-spots" is the sentence that turns a working-relationship
#: guide into a leaderboard, and it is unfalsifiable besides.
_COMPATIBILITY_SCORE = re.compile(
    r"(?:\b\d{1,3}\s*(?:%|percent)\s*(?:of\s+)?"
    r"(?:synergy|compatibility|compatible|fit|match|overlap|alignment)\b)"
    r"|(?:\b(?:synergy|compatibility|fit|match|teamwork|chemistry)\s+"
    r"(?:score|rating|index|grade|percentage|number)\b)",
    re.IGNORECASE,
)

#: Ranking people, said out loud.
_RANK_PEOPLE = re.compile(
    r"\brank(?:s|ed|ing)?\b[^.!?]{0,40}"
    r"\b(?:people|employees|staff|colleagues|team members|candidates|"
    r"the team|them)\b",
    re.IGNORECASE,
)


def screen_no_ranking(text: object) -> str | None:
    """Refuse cross-subject ranking, leadership assignment and compatibility
    scoring. Additive.

    Returns a reason code from this module, or ``None`` when the text is clean.
    Run this **as well as** ``guide_prompts.screen_prose`` on pair prose, and on
    its own in ST-9's chat report, which never touches the guide screens.

    Leadership assignment ("should own this", "the obvious choice to lead",
    "the team lead on this") is reported under
    :data:`REJECT_CROSS_SUBJECT_RANKING` rather than a code of its own. It is not literally a comparison, but it is the ACT a ranking exists to
    justify, and one code keeps the two documents' logs comparable — the reason a
    caller has to act on is the same either way: use the authored wording.

    It is a net, not a guarantee: the durable protection against ranking is that
    no type in :mod:`team_contracts` can carry a per-person score. This catches
    the prose that would describe one anyway.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    if _COMPATIBILITY_SCORE.search(text):
        return REJECT_COMPATIBILITY_SCORE
    for pattern in (
        _COMPARATIVE,
        _RANKING_VERB,
        _SUPERLATIVE_OF_PEOPLE,
        _GRADED_OF_A_SET,
        _LEADERSHIP_ASSIGNMENT,
        _RANK_PEOPLE,
    ):
        if pattern.search(text):
            return REJECT_CROSS_SUBJECT_RANKING
    return None


# ── Authored notices ──────────────────────────────────────────────────────────

#: The advisory notice a pair document carries. The single-subject sentence is
#: reused verbatim from the instrument module — it is the same promise — and one
#: sentence is added, because it is the fact a reader is most likely to assume
#: the other way: this document has never seen these two people work together.
NO_OBSERVATION_NOTICE = (
    "This is built from two separate self-descriptions. Neither of you answered "
    "with the other in mind, and nothing here is an observation of the two of "
    "you actually working together."
)

PAIR_ADVISORY_NOTICE = f"{disc.ADVISORY_NOTICE} {NO_OBSERVATION_NOTICE}"

"""ST-6 — "How to Be Successful Working with {NAME}", built from a scored profile.

The deliverable: one guide per employee, written from their own
Work Style Interview, reviewed and approved by them, and only then visible to
colleagues. This module turns one ``st_profiles`` row into that document, stores
it on ``st_documents``, exports it, and owns the single RAG write this feature is
allowed to make.

## The rule every section here obeys

A framework payload on ``st_profiles`` is an envelope with a gate
(``contracts.KEY_REPORTABLE``). **Every** section that would say something drawn
from a layer reads that layer through :func:`read_layers`, which calls
``contracts.read_verdict`` and ``contracts.layer_of`` — so a refused layer's
contents are unreachable here rather than merely un-recommended. Nothing in this
module subscripts a payload's ``layer`` dict directly.

What a refusal does is decided **per section**, and stated once, here:

| Section | Layer reportable | Layer refused |
|---|---|---|
| Title, deck, advisory notice | no layer involved | unchanged — fixed house copy |
| Framing paragraph | one slot per reportable layer, from authored fields | that layer's slot **drops out**. Zero slots left → a fixed house sentence that names nothing |
| Numbered guidelines | every guideline carries ``source=(framework, field)`` and is refused emission without one | candidates from that layer are **not built**. Fewer than :data:`MIN_GUIDELINES` survive → the section is omitted entirely and the guide becomes the shortfall document below |
| Framework tie-in citations | rendered, with wording gated by ``confidence`` | **omitted entirely.** A hedge in a citation slot reads as "the assessment says something we are being coy about" |
| Working preferences (Big Five) | the five authored per-band descriptions | omitted. The hedge for it is already carried once, in "How complete this is" |
| Derived four-letter layer | the instrument's own ``description``, which carries its qualifier | omitted, same reason |
| Growth edge | rendered from an authored ``under_stress`` / ``overused`` / ``in_conflict`` field, and only at confidence ``high`` or ``moderate`` | **omitted.** No hedge substitute, and the model is never asked to write one |
| How complete this is | "Included." plus the layer's own note, which is about a layer whose contents are published anyway | :data:`PUBLICATION_REASON_PROSE` for the reason code — authored here, first person, naming no axis, type or ranking — plus the interview sections that would fill it in |
| Final thought | fixed house copy | unchanged, but omitted when there are no guidelines — house copy under an empty guide is filler |

So there is exactly **one** place in the document where a refusal is explained:
the completeness section, which is provenance. A refusal is never a finding,
never a citation, and never the body of a guideline.

**A refused layer's own ``hedge`` is not rendered here, and that is a
correction.** It used to be, on the reasoning that the instrument's own sentence
is better because whoever wrote the layer knows what it measures. That is true of
the *subject's* view and false of this one, and several instrument hedges are
themselves findings: a motivators layer refused for ``probe_pending`` hedges by
naming its top two and its bottom motivator, and an unresolved enneagram hedges
by naming both candidate types. Printing either into a company-visible,
RAG-ingested document asserts to colleagues exactly what the closed gate refused
to report — under a line that simultaneously says the layer is not included. The
distinction to carry: "is this layer reportable" and "may this string go into a
document this person's colleagues read" are two different questions. The hedge is
still the right sentence for the subject's own profile read, which is served by
``backend/routers/smart_teams.py`` from the payload and does not come through
here.

**A guide where every layer refused is still a valid document.** It carries the
title, the framing house copy, the completeness section naming every layer and
why, the interview sections that would change the answer, and the advisory
notice — and no guidelines, no tie-ins, no growth edge, no final thought. It
says plainly that the interview did not produce enough signal to write from and
what would fix that. It is never empty and it never invents a sentence to fill
the space.

## What the model does, and what it never sees

The model writes voice, not facts. :func:`write_prose` hands it authored
sentences already selected by the gate (see ``guide_prompts.py``) and takes back
a heading and a body per slot.

**Every model-written string is screened against the one authored fact its slot
was built from**, by ``guide_prompts.screen_slot_heading`` /
``screen_slot_body`` — not by the generic ``screen_prose``, which knows nothing
about provenance and which a model can satisfy while adding a whole sentence.
That is not a hypothetical: a stub writer got "I burned out at my last job after
my divorce", "I have ADHD so keep agendas short" and an asserted Enneagram type
for a subject whose enneagram gate was CLOSED into a published, RAG-ingested
guide, past ``screen_prose`` and past the assembled-body screen. The four
containment rules that close it — closed-gate naming, framework-vocabulary
containment, digit containment, and a novel-content floor — are documented and
calibrated in ``guide_prompts.py``.

A rejection is **not** fatal. The heading and the body of a slot are accepted
independently, and a rejected one falls back to the authored wording it was built
from, recorded on :attr:`PersonalGuide.fallbacks` so the count is visible rather
than silent. A guide that is entirely authored fallback is a correct document,
not a failure — which is also exactly what :func:`build_guide` produces with no
``prose_writer`` at all, and what most tests in
``backend/tests/test_smart_teams_guide.py`` exercise.

**The interview transcript is never read by this module.** Not by the builder,
not by the prompt. The one string here that came out of the person's mouth is
``strengths.ThemeRead.evidence_quote``, taken verbatim from the scored payload so
that its subject can argue with it, and never shown to the model.

:func:`generate_into_row` screens the whole assembled body once more before it is
stored. It has **two** exemptions, both cut out before the screen runs, and both
for the same reason — the screen is calibrated for model prose and neither of
these is model prose:

1. The advisory notice. It is the sentence promising the profile is "never used
   for performance review, promotion, or compensation decisions", so it contains
   the word the advancement pattern matches on, and screening it rejected every
   guide the module could produce. Fixed copy, imported verbatim from
   ``disc.ADVISORY_NOTICE``.
2. **The subject's own verbatim words** — :attr:`PersonalGuide.verbatim_spans`,
   today just the strengths evidence quote. A person who said "I built the
   promotion tracker for the marketing launch" was failing their own guide with
   ``advancement_language`` and had no way to fix it, because the quote comes
   from the scored payload and no endpoint can edit ``content_md``. An ordinary
   thing somebody said about their own work is not a policy violation, and a
   screen calibrated for a model must not be the reason a real person cannot have
   a guide.

Everything else in the body is screened, and the model-written parts were already
screened against their facts.

## Publication, and the two writes it triggers

``st_documents.published`` may only become true when ``approved_at`` is set — a
CHECK constraint enforces it and :func:`approve` is the only writer. Approval
does two independent things, and they are not the same object:

1. **The exported .docx** may be saved to the person's Assets page through
   ``backend.services.generated_docs.save_generated_doc``, which deliberately
   does not RAG-ingest anything.
2. **The guide text** is ingested into the ``companywide`` corpus by
   :func:`ingest_published_guide`, under a filename derived from the subject so
   a regenerate REPLACES rather than accumulates (``RagManager.update_document``
   — Vertex's ``import_files`` creates a new ragFile every time, which is how
   ``ingestion/eos/pipeline.py`` learned this lesson).

Unpublishing reverses both, in that order of urgency: :func:`unpublish` clears
``published`` immediately (which stops every in-app read through
``authz.is_guide_published``), and :func:`purge_guide_vectors` removes the
corpus copy through ``dam.vectorstore.delete_document_vectors``, which tries the
companywide corpus and then the default corpus — the DAM routing asymmetry means
a single-corpus delete leaves the chunks answering in the other one. The purge
runs on cubie-worker, so ``rag_ingested`` stays true until it actually succeeds:
between the two the guide is out of the app and still reachable by Chat, and the
row says so rather than claiming otherwise.

## Advisory only

Nothing here may frame a profile as competence, potential, readiness, promotion,
or fitness for a role.

Precisely what this module can emit, because an earlier version of this paragraph
claimed every sentence came from an authored field and a reviewer reasonably
concluded from it that no restatement check was needed:

* **Authored copy** — instrument fields selected past an open gate, and the fixed
  house copy in this file. Screened at import (``strengths._check_taxonomy``
  runs the advancement pattern over every authored field) and again on assembly.
* **Model-written headings and bodies**, when a ``prose_writer`` is supplied.
  Governed by ``guide_prompts.screen_slot_heading`` / ``screen_slot_body``, which
  refuse a closed-gate framework name, a framework term or digit absent from the
  slot's fact, and a content word from outside the fact, ordinary English, and the
  instruments' own vocabulary. What they do **not** check: whether a faithful-
  looking rearrangement of the fact's own words changes its meaning, and whether
  the tone is right. Both are one paragraph of risk, in a document its subject
  reads and approves before anybody else sees it.
* **The subject's own verbatim words**, once: the strengths evidence quote. Not
  screened (see above), and not model-written. It carries the risk the finding
  log calls out separately — a quote naming a colleague publishes something about
  a person who never took an interview.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from rubik_core.brand import PRODUCT_NAME, TINT_HEX
from rubik_core.cost.usage import attribute_to
from rubik_core.database.smart_teams import (
    SmartTeamsDocument,
    SmartTeamsProfile,
    document_cache_keys,
)
from rubik_core.smart_teams import contracts, guide_prompts
from rubik_core.smart_teams import interview as interview_catalog
from rubik_core.smart_teams.instruments import (
    disc,
    enneagram,
    motivators,
    strengths,
    type16,
)

logger = logging.getLogger(__name__)

#: ``st_documents.kind`` for a personal guide. Imported, never re-typed: it is
#: half of the cache key.
KIND_PERSONAL = "personal_guide"

#: The AI Cost bucket. Same string as ``scoring.COST_FEATURE`` — the
#: ``_FEATURE_LABELS`` entry in ``backend/routers/admin.py`` already exists, and
#: a second spelling would open a second "Unattributed"-shaped hole.
COST_FEATURE = "smart_teams"

#: Model key, resolved to a concrete Gemini id by
#: ``priorities.agents.base._resolve_model_id`` → ``settings.VERTEX_LLM_MODEL_PRO``.
#: A key, never an id: model ids are centralised in ``rubik_core/config.py``.
#: Pro rather than Flash because this is one call per person per profile version
#: — sixty-odd calls for the whole company — and the output is a document that
#: person's colleagues read.
PROSE_MODEL_KEY = "gemini-pro"

#: Rewriting authored sentences into one voice, not composing. Same 0.2 the chat
#: and scoring paths settled on after the hallucination RCA.
PROSE_TEMPERATURE = 0.2

#: Total tries for the prose call, including the regeneration a screen rejection
#: buys. A second failure falls back to authored copy rather than trying again:
#: a model that has written advancement language twice is not one more attempt
#: away from not doing it.
PROSE_ATTEMPTS = 2

#: Guideline bounds. The cap is the source document's ten; the floor is the
#: point below which there is no guide worth publishing. Both are enforced on
#: the *qualified* candidate list — nothing is ever padded to reach the cap, and
#: nothing is invented to reach the floor.
MAX_GUIDELINES = 10
MIN_GUIDELINES = 4

#: Growth-edge candidates need this much confidence as well as an open gate.
_GROWTH_EDGE_CONFIDENCES = frozenset({"high", "moderate"})

#: How each layer is named to a reader. Tie-in citations are labelled by
#: framework rather than by topic so the provenance of a sentence is visible.
FRAMEWORK_LABELS: Mapping[str, str] = {
    "disc": "Work style",
    "big_five": "Working preferences",
    "type16": "Four-letter layer (derived)",
    "strengths": "Strengths",
    "motivators": "What drives me",
    "enneagram": "Motivation pattern",
}

assert set(FRAMEWORK_LABELS) == set(contracts.FRAMEWORKS), (
    "guide.FRAMEWORK_LABELS has drifted from contracts.FRAMEWORKS; a layer with "
    "no label renders as a missing section heading"
)

#: Authored English for a refusal, keyed by ``contracts`` reason code.
#:
#: Written for the SUBJECT ("You chose to leave this one out") and kept for any
#: subject-facing surface that wants it. The completeness section of a guide does
#: NOT use this mapping — see :data:`PUBLICATION_REASON_PROSE`, which says the
#: same things in the first person, because a guide is written as "I" and read by
#: somebody else.
REASON_PROSE: Mapping[str, str] = {
    contracts.REASON_NOT_TAKEN: "This part of the interview has not been taken yet.",
    contracts.REASON_DECLINED: "You chose to leave this one out.",
    contracts.REASON_NO_LAYER_DATA: (
        "This layer did not come back from scoring. An admin can look into it — "
        "nothing you answered is lost."
    ),
    contracts.REASON_INSUFFICIENT_ANSWERS: (
        "Not enough answers here to say anything honestly."
    ),
    contracts.REASON_RETAKE_RECOMMENDED: (
        "The answers here looked rushed, so nothing is reported from them. "
        "Retaking this section is quick."
    ),
    contracts.REASON_QUALITY_FLAGGED: (
        "Something about the answering pattern here means we are not reporting from it."
    ),
    contracts.REASON_FLAT_PROFILE: (
        "The answers came out level, so there is no shape to report."
    ),
    contracts.REASON_UNRESOLVED: "This one did not settle on an answer.",
    contracts.REASON_PROBE_PENDING: (
        "There is a short follow-up left that would settle this."
    ),
    contracts.REASON_THIN_TRANSCRIPT: (
        "The conversation here was short. Adding to it would fill this in."
    ),
    contracts.REASON_COIN_FLIP: (
        "The scores behind this landed in the middle, so a call either way "
        "would be a coin flip."
    ),
    contracts.REASON_SUPPRESSED_BY_SOURCE: (
        "Held back because the answers it is derived from looked rushed."
    ),
}

assert set(REASON_PROSE) == set(contracts.REASON_CODES), (
    "guide.REASON_PROSE has drifted from contracts.REASON_CODES; a reason with "
    "no prose renders as a blank line in somebody's completeness section"
)

#: What a COMPANY-VISIBLE guide says about a refused layer, keyed by reason code.
#: The only thing :func:`completeness` will print for a closed gate.
#:
#: Three properties every line here has to have, and the instrument hedges it
#: replaced had none of them:
#:
#: 1. **It names no result.** No axis, no type, no ranking, no candidate, no
#:    score. A closed gate means the engine refused to report the layer, and a
#:    sentence explaining the refusal by describing what it would have said is
#:    the refusal in name only. This is the defect being fixed: a motivators
#:    hedge for ``probe_pending`` published the top two and the bottom motivator,
#:    and an ``unresolved`` enneagram hedge published both candidate types.
#: 2. **It is first person.** The document is written as "I" throughout and a
#:    reader who is suddenly told about "their answers" can tell the sentence was
#:    written for a different screen.
#: 3. **It is not an apology or a deficiency.** A layer that did not report is a
#:    layer with less signal in it, not a fault in the person, and this section is
#:    read by their colleagues.
PUBLICATION_REASON_PROSE: Mapping[str, str] = {
    contracts.REASON_NOT_TAKEN: "I have not taken this part of the interview yet.",
    contracts.REASON_DECLINED: "I chose to leave this one out.",
    contracts.REASON_NO_LAYER_DATA: (
        "This part did not come back from scoring, so it is not in here."
    ),
    contracts.REASON_INSUFFICIENT_ANSWERS: (
        "I did not answer enough of this part for anything to be said from it."
    ),
    contracts.REASON_RETAKE_RECOMMENDED: (
        "My answers here looked rushed, so nothing is reported from this part. I "
        "can retake it."
    ),
    contracts.REASON_QUALITY_FLAGGED: (
        "Something about how I answered this part means it is not reported here."
    ),
    contracts.REASON_FLAT_PROFILE: (
        "My answers here came out level, so there is no shape to report."
    ),
    contracts.REASON_UNRESOLVED: "This part did not settle on an answer.",
    contracts.REASON_PROBE_PENDING: (
        "There is a short follow-up left that would settle this, so it is not "
        "reported yet."
    ),
    contracts.REASON_THIN_TRANSCRIPT: (
        "The conversation behind this part was short. Adding to it would fill this in."
    ),
    contracts.REASON_COIN_FLIP: (
        "The scores behind this landed in the middle, so a call either way would "
        "be a coin flip and none is made."
    ),
    contracts.REASON_SUPPRESSED_BY_SOURCE: (
        "Held back because the answers it is derived from looked rushed."
    ),
}

assert set(PUBLICATION_REASON_PROSE) == set(contracts.REASON_CODES), (
    "guide.PUBLICATION_REASON_PROSE has drifted from contracts.REASON_CODES; a "
    "reason with no publication-safe prose would fall through to the last-resort "
    "sentence and lose the reason entirely"
)

#: Printed when a layer is refused with no reason code at all. Says nothing about
#: the person, which is the only safe thing to say when we do not know why.
COMPLETENESS_NOT_REPORTED = "Not reported in this version of the profile."


# ── Fixed house copy ─────────────────────────────────────────────────────────
# Every string below is authored here and shown to nobody for approval except
# the person the guide is about. None of it is model-written, none of it is
# personalised, and none of it makes a claim about anybody.

TITLE_TEMPLATE = "How to Be Successful Working with {name}"

DECK = (
    "A short working guide, written from my own Work Style Interview and "
    "approved by me. It is about how I prefer to work — not about how well I "
    "do it."
)

FRAMING_OPENER = (
    "I put this together so we spend less time guessing at each other. "
    "Everything here is something I said about myself."
)

FRAMING_CLOSER = (
    "None of it is a rule, and none of it is the whole picture. If I have got "
    "myself wrong somewhere, tell me — that is a better conversation than "
    "working around it."
)

FRAMING_NO_LAYERS = (
    "I have started the Work Style Interview but not finished enough of it for "
    f"{PRODUCT_NAME} to write from yet, so there is nothing here I can stand behind. "
    "What follows is what is missing rather than what I am like."
)

#: The source format's own name for this box. Was "Where I could use a hand",
#: which is friendlier and is NOT what the two sample guides this format follows
#: call it — matching them is the point, and "GROWTH EDGE" is also what the pair
#: guide refers back to.
GROWTH_EDGE_LABEL = "Growth edge"

GROWTH_EDGE_CLOSER = (
    "Naming this here is not a warning about me. It is the thing worth saying "
    "out loud early, while it is still cheap to sort out."
)

FINAL_THOUGHT_LABEL = "Final thought"

#: Page-1 contents header. The source sets it as a sentence in caps — "HERE ARE
#: 10 THINGS TO KEEP IN MIND" — above a two-column index of the guideline
#: titles, and that index is most of what page 1 looks like. We had the heading
#: as a lower-case count ("7 things to keep in mind") and no index at all.
CONTENTS_TEMPLATE = "Here are {count} {noun} to keep in mind"

#: The sign-off. The source closes with a personal line, a script-styled first
#: name and an em-dashed first name. The personal line is the author's own
#: motto, so it is theirs rather than a format element; what generalises
#: is the em-dashed first name, which is what this writes.
SIGNATURE_TEMPLATE = "— {first_name}"

FINAL_THOUGHT = (
    "I would rather be told something early and plainly than find out later "
    "that we were both being careful. I will bring you the same: what I "
    "actually think, what I have actually done, and where I am stuck. If we "
    "keep that up, the rest of this document is mostly detail."
)

COMPLETENESS_LABEL = "How complete this is"

COMPLETENESS_INTRO = (
    "This guide only says what my answers support. Here is what is behind each "
    "part of it, and what is not."
)

SHORTFALL_LABEL = "Not enough to write from yet"

SHORTFALL_BODY = (
    f"{PRODUCT_NAME} did not have enough from my interview to write a working "
    "guide, so "
    "it has not written one. That is the honest outcome rather than a failure — "
    "a guide assembled out of thin answers would describe somebody who is not "
    "me, and my colleagues would read it. The sections below are the ones that "
    "would change this."
)

TIE_IN_LABEL_TEMPLATE = "{label} tie-in"

#: What the export and the RAG copy both say about what this document is.
PROVENANCE_TEMPLATE = (
    f"Generated by {PRODUCT_NAME} from "
    "{name}'s Work Style Interview (profile version {version}) and approved by "
    "{name}. Advisory only."
)


# ── Reading the profile ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class LayerRead:
    """One framework's gate verdict and, only past an open gate, its contents.

    ``layer`` is ``None`` whenever ``verdict.reportable`` is false, because it
    comes from ``contracts.layer_of``, which returns ``None`` past a closed gate.
    Nothing in this module reads a payload any other way.
    """

    framework: str
    verdict: contracts.LayerVerdict
    layer: Mapping[str, Any] | None

    @property
    def reportable(self) -> bool:
        return self.verdict.reportable and self.layer is not None

    @property
    def confident(self) -> bool:
        """Whether the wording may be flat rather than leaning."""
        return self.verdict.confidence == "high"

    @property
    def strong_enough_for_growth_edge(self) -> bool:
        return self.reportable and self.verdict.confidence in _GROWTH_EDGE_CONFIDENCES


def read_layers(profile: SmartTeamsProfile) -> dict[str, LayerRead]:
    """Every framework on a profile row, gate-checked, in canonical order.

    A NULL column is reported as ``not_taken`` rather than skipped: a reader
    that has to handle both "absent key" and "envelope" has two paths, and one
    of them will forget the gate (``contracts.not_taken_envelope`` says the same
    thing about the writer). A column that fails ``validate_envelope`` is
    reported the same way, loudly — a row written by a seed script or an older
    deploy must not be rendered on a guess.
    """
    out: dict[str, LayerRead] = {}
    for framework in contracts.FRAMEWORKS:
        payload = getattr(profile, framework, None)
        if payload is None:
            payload = contracts.not_taken_envelope(framework)
        try:
            contracts.validate_envelope(payload)
            verdict = contracts.read_verdict(payload)
            layer = contracts.layer_of(payload)
        except contracts.PayloadError as exc:
            logger.error(
                "[smart-teams] guide: %s payload on profile %s is off-contract "
                "(%s); treating the layer as absent",
                framework,
                getattr(profile, "id", "?"),
                exc,
            )
            fallback = contracts.not_taken_envelope(
                framework, reason=contracts.REASON_NO_LAYER_DATA
            )
            verdict = contracts.read_verdict(fallback)
            layer = None
        out[framework] = LayerRead(framework=framework, verdict=verdict, layer=layer)
    return out


def _sections_feeding(framework: str) -> tuple[str, ...]:
    """Interview section titles whose answers feed ``framework``.

    Read from ``interview.SECTIONS`` rather than listed here, so a section that
    changes what it feeds changes this sentence too. This is what lets the
    shortfall document say which questions to go and answer instead of telling
    somebody their profile is incomplete and leaving them to guess.
    """
    return tuple(
        section.title
        for section in interview_catalog.SECTIONS
        if framework in section.feeds
    )


# ── The document model ───────────────────────────────────────────────────────


@dataclass
class Guideline:
    """One numbered guideline, and the field it is allowed to exist because of.

    ``source`` is not decoration. :func:`build_guide` refuses to emit a guideline
    without one, which is the mechanism that keeps the count honest: ten
    guidelines means ten authored fields on reportable layers, not ten paragraphs.
    """

    slot_id: str
    #: ``(framework, field)`` — the provenance of every word in ``body``.
    source: tuple[str, str]
    heading: str
    body: str
    #: The authored sentence(s) this was built from. Kept so a rejected model
    #: rewrite can fall back to it and so a reviewer can diff the two.
    fact: str
    intent: str
    #: Set when the model's wording was used for the heading, the body, or both.
    #: They are accepted independently, so this is "any part of this guideline is
    #: the model's" and not "all of it is"; :attr:`PersonalGuide.fallbacks` is
    #: where the per-part detail lives.
    model_written: bool = False
    tie_in: Callout | None = None

    def __post_init__(self) -> None:
        framework, _field = self.source
        if framework not in contracts.FRAMEWORKS:
            raise ValueError(
                f"guideline {self.slot_id}: source names unknown framework "
                f"{framework!r}"
            )


@dataclass(frozen=True)
class Callout:
    """A labelled aside — a framework tie-in, or the growth edge.

    Both are the same device in the source format, so they are the same object
    here. The label is what tells a reader which one they are looking at.

    ``verbatim`` is the span of ``body`` that came out of the subject's own mouth
    rather than out of an authored field or a model, when any of it did. It exists
    so :func:`generate_into_row` can exclude those words from a screen written for
    model prose; nothing renders it.
    """

    label: str
    body: str
    verbatim: str | None = None


@dataclass(frozen=True)
class CompletenessLine:
    """One framework's line in the provenance section."""

    framework: str
    label: str
    included: bool
    note: str
    #: Interview sections that would change this line, when anything would.
    sections_to_finish: tuple[str, ...] = ()


@dataclass
class PersonalGuide:
    """The whole document, assembled and renderable.

    Deliberately not a markdown string: the Word renderer needs the structure
    (``rubik_core/people/export.py`` documents why — "the same layout written
    twice in two libraries is how the two artifacts drift"), and the staleness
    and approval machinery needs the metadata.
    """

    subject_email: str
    display_name: str
    profile_version: int
    title: str
    deck: str
    framing: str
    guidelines: list[Guideline] = field(default_factory=list)
    growth_edge: Callout | None = None
    working_preferences: list[str] = field(default_factory=list)
    derived_layer: str | None = None
    completeness: list[CompletenessLine] = field(default_factory=list)
    final_thought: str | None = None
    shortfall: str | None = None
    advisory_notice: str = disc.ADVISORY_NOTICE
    #: ``"{slot_id}.heading"`` / ``"{slot_id}.body"`` -> the rejection code that
    #: sent it back to authored copy. Recorded rather than only logged: "the model
    #: wrote none of this guide" and "the model wrote all of it" are the same
    #: document to a reader and completely different facts about the run, and a
    #: rate that is only visible in Cloud Logging is a rate nobody watches.
    fallbacks: dict[str, str] = field(default_factory=dict)

    @property
    def has_content(self) -> bool:
        """Whether anything in here is a statement about the person."""
        return bool(self.guidelines)

    @property
    def sources(self) -> tuple[tuple[str, str], ...]:
        return tuple(g.source for g in self.guidelines)

    @property
    def model_written_count(self) -> int:
        """How many guidelines carry the model's wording rather than ours."""
        return sum(1 for g in self.guidelines if g.model_written)

    @property
    def verbatim_spans(self) -> tuple[str, ...]:
        """Every span in the rendered body that is the subject's own words.

        Provenance, not an exemption. This used to be subtracted from the
        assembled body before screening; it is not any more, because doing so
        removed the only check standing over quoted text. Quotes are screened in
        :func:`_tie_in` at the point they enter. Kept because "which words here
        are the subject's own" is worth being able to answer — a reviewer asking
        why a sentence is in somebody's guide deserves that answer.
        """
        callouts = [g.tie_in for g in self.guidelines if g.tie_in is not None]
        if self.growth_edge is not None:
            callouts.append(self.growth_edge)
        return tuple(
            callout.verbatim
            for callout in callouts
            if callout.verbatim and callout.verbatim.strip()
        )


# ── Candidate guidelines ─────────────────────────────────────────────────────
# One builder per framework. Each returns candidates in a FIXED order that is a
# presentation choice and not a ranking — the only ordering in this document
# that carries meaning is the strengths themes', which is the extractor's own.
#
# There is deliberately no loop over the six frameworks: each builder reads its
# layer's fields by name, so a renamed field breaks a test rather than quietly
# evaluating falsy. Same reasoning as ``contracts``' six hand-written verdicts.


def _text(layer: Mapping[str, Any], *path: str) -> str | None:
    """A non-empty string at ``path`` inside a layer dict, or ``None``."""
    node: Any = layer
    for key in path:
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    if isinstance(node, str) and node.strip():
        return node.strip()
    return None


def _strings(layer: Mapping[str, Any], *path: str) -> tuple[str, ...]:
    node: Any = layer
    for key in path:
        if not isinstance(node, Mapping):
            return ()
        node = node.get(key)
    if not isinstance(node, Sequence) or isinstance(node, str):
        return ()
    return tuple(str(v).strip() for v in node if isinstance(v, str) and v.strip())


def _disc_candidates(read: LayerRead) -> list[Guideline]:
    """Six from DISC: three on communication, then feedback, energy, drain.

    The authored headings are generic on purpose — "How to Keep Me in the Loop"
    is true of whatever the sentence under it says, so a rejected model rewrite
    degrades to something plain rather than to something wrong.
    """
    if not read.reportable:
        return []
    style = read.layer.get("assignment") if read.layer else None
    style = style.get("style") if isinstance(style, Mapping) else None
    if not isinstance(style, Mapping):
        return []

    out: list[Guideline] = []
    comms = _strings(style, "communication")
    headings = (
        "How to Open a Conversation With Me",
        "How to Keep Me in the Loop",
        "When We Disagree",
    )
    intents = (
        "how a colleague should start a conversation with this person",
        "how a colleague should keep this person updated",
        "how a colleague should raise a disagreement with this person",
    )
    for index, sentence in enumerate(comms[:3]):
        out.append(
            Guideline(
                slot_id=f"disc.communication.{index}",
                source=("disc", f"communication[{index}]"),
                heading=headings[index] if index < 3 else "How to Talk to Me",
                body=sentence,
                fact=sentence,
                intent=intents[index] if index < 3 else intents[0],
            )
        )

    feedback = _text(style, "feedback")
    if feedback:
        out.append(
            Guideline(
                slot_id="disc.feedback",
                source=("disc", "feedback"),
                heading="How to Give Me Feedback",
                body=feedback,
                fact=feedback,
                intent="how a colleague should give this person feedback",
            )
        )

    energised = _strings(style, "energized_by")
    if energised:
        joined = " ".join(energised[:2])
        out.append(
            Guideline(
                slot_id="disc.energized_by",
                source=("disc", "energized_by"),
                heading="Point Me at This Kind of Work",
                body=joined,
                fact=joined,
                intent="the kind of work that gives this person energy",
            )
        )

    drained = _strings(style, "drained_by")
    if drained:
        joined = " ".join(drained[:2])
        out.append(
            Guideline(
                slot_id="disc.drained_by",
                source=("disc", "drained_by"),
                heading="What Costs Me the Most Energy",
                body=joined,
                fact=joined,
                intent="what drains this person, so a colleague can avoid it",
            )
        )
    return out


def _enneagram_candidates(read: LayerRead) -> list[Guideline]:
    """One: the authored "what colleagues can actually do" line.

    Looked up through ``enneagram.spec_for``, so the prose is the instrument's
    and our own type labels are the only ones that can appear.
    """
    if not read.reportable:
        return []
    number = read.layer.get("type_number") if read.layer else None
    if not isinstance(number, int):
        return []
    try:
        spec = enneagram.spec_for(number)
    except KeyError:
        logger.error("[smart-teams] guide: enneagram type %r has no spec", number)
        return []
    return [
        Guideline(
            slot_id="enneagram.needs_from_colleagues",
            source=("enneagram", "needs_from_colleagues"),
            heading="What Helps Me Most",
            body=spec.needs_from_colleagues,
            fact=spec.needs_from_colleagues,
            intent="the practical thing colleagues can do for this person",
        )
    ]


def _motivator_candidates(read: LayerRead) -> list[Guideline]:
    """Up to two, and both are guarded against a tie-break masquerading as a rank.

    ``top_two`` is ordered by emphasis with ties broken by the declaration order
    of ``motivators.MOTIVATORS``, so a pair that sits inside
    ``CLOSE_RANK_MARGIN`` is reported by the instrument in ``near_ties``. When
    either of the top two is in one of those pairs, the two are named **together
    and unordered** rather than dropped — the pair is a real finding even when
    the order inside it is not. ``bottom`` is used only when the instrument says
    it is not contested, for the same reason.
    """
    if not read.reportable or read.layer is None:
        return []
    layer = read.layer
    top_two = [m for m in (layer.get("top_two") or []) if isinstance(m, str)]
    near_ties = {
        tuple(pair) for pair in (layer.get("near_ties") or []) if isinstance(pair, list)
    }
    tied_axes = {axis for pair in near_ties for axis in pair}

    out: list[Guideline] = []
    if top_two:
        specs = []
        for axis in top_two[:2]:
            try:
                specs.append(motivators.spec_for(axis))  # type: ignore[arg-type]
            except KeyError:
                logger.error("[smart-teams] guide: unknown motivator axis %r", axis)
        if specs:
            joined = " ".join(spec.high_read for spec in specs)
            names = " and ".join(spec.name for spec in specs)
            unordered = any(axis in tied_axes for axis in top_two[:2])
            heading = (
                f"What Makes the Work Worth It: {names}"
                if not unordered
                else f"Two Things That Make the Work Worth It: {names}"
            )
            out.append(
                Guideline(
                    slot_id="motivators.high_read",
                    source=("motivators", "high_read"),
                    heading=heading,
                    body=joined,
                    fact=joined,
                    intent=(
                        "what makes work feel worth doing to this person; name "
                        "both of these together and do not put one above the "
                        "other"
                        if unordered
                        else "what makes work feel worth doing to this person"
                    ),
                )
            )

    bottom = layer.get("bottom")
    contested = bool(layer.get("bottom_is_contested"))
    if isinstance(bottom, str) and bottom and not contested:
        try:
            spec = motivators.spec_for(bottom)  # type: ignore[arg-type]
        except KeyError:
            spec = None
        if spec is not None:
            out.append(
                Guideline(
                    slot_id="motivators.low_read",
                    source=("motivators", "low_read"),
                    heading="What Moves Me Less Than You Might Expect",
                    body=spec.low_read,
                    fact=spec.low_read,
                    intent=(
                        "what motivates this person least — a weak motive, never "
                        "a shortcoming"
                    ),
                )
            )
    return out


def _strengths_candidates(read: LayerRead, *, limit: int = 3) -> list[Guideline]:
    """Up to three "ask me for" guidelines, in the extractor's own rank order.

    The one place in the document where the ordering means something, because it
    is the instrument's ranking rather than a presentation choice.
    """
    if not read.reportable or read.layer is None:
        return []
    out: list[Guideline] = []
    for index, theme in enumerate(read.layer.get("themes") or []):
        if len(out) >= limit:
            break
        if not isinstance(theme, Mapping):
            continue
        theme_id = theme.get("theme_id")
        if not isinstance(theme_id, str):
            continue
        try:
            spec = strengths.spec_for(theme_id)
        except KeyError:
            logger.error("[smart-teams] guide: unknown strengths theme %r", theme_id)
            continue
        out.append(
            Guideline(
                slot_id=f"strengths.ask_for.{index}",
                source=("strengths", f"ask_for[{theme_id}]"),
                heading=f"Ask Me About {spec.name}",
                body=spec.ask_for,
                fact=spec.ask_for,
                intent=(f"what to come to this person for, on the theme '{spec.name}'"),
            )
        )
    return out


#: Fixed presentation order for the qualified candidates. Not a ranking — the
#: cap has to cut somewhere and cutting by a stable rule beats cutting by
#: whatever order a dict happened to iterate in.
_CANDIDATE_ORDER: tuple[str, ...] = ("disc", "strengths", "enneagram", "motivators")


def candidate_guidelines(layers: Mapping[str, LayerRead]) -> list[Guideline]:
    """Every guideline the profile actually supports, capped, never padded.

    Big Five and the derived four-letter layer contribute nothing here on
    purpose: both describe the person rather than telling a colleague what to
    do, and turning "Structure and follow-through sits clearly toward the higher
    end of this scale" into an instruction would be the generator inventing the
    instruction. They get their own descriptive sections instead.
    """
    builders: Mapping[str, Callable[[LayerRead], list[Guideline]]] = {
        "disc": _disc_candidates,
        "strengths": _strengths_candidates,
        "enneagram": _enneagram_candidates,
        "motivators": _motivator_candidates,
    }
    out: list[Guideline] = []
    for framework in _CANDIDATE_ORDER:
        out.extend(builders[framework](layers[framework]))
    return out[:MAX_GUIDELINES]


# ── Tie-in citations ─────────────────────────────────────────────────────────


#: Word cap for screening the subject's own quote. Higher than the model-prose
#: cap: a person telling a story about their work runs longer than a rewritten
#: guideline, and the cap here exists to stop a runaway payload, not to enforce
#: brevity on somebody's own words.
_QUOTE_MAX_WORDS = 400


def _tie_in(read: LayerRead) -> Callout | None:
    """The citation box for one framework, or ``None``.

    Only ever built for a reportable layer, and its body is the instrument's own
    authored line — ``motivators.headline()`` and ``enneagram.headline()`` are
    stored in the payload precisely because they are the sentence those modules
    are willing to put in front of a colleague, hedges included. Confidence
    gates the *wording* of the DISC line: at ``moderate`` or ``low`` the style
    name leans rather than lands, and the style's ``identity`` sentence — the
    most quotable line in the document — is left out entirely.

    Never carries a ``hedge``. A hedge in a citation slot reads as an assessment
    saying something we are being coy about; hedges live in the completeness
    section, once.
    """
    if not read.reportable or read.layer is None:
        return None
    label = TIE_IN_LABEL_TEMPLATE.format(label=FRAMEWORK_LABELS[read.framework])

    if read.framework == "disc":
        assignment = read.layer.get("assignment")
        style = assignment.get("style") if isinstance(assignment, Mapping) else None
        if not isinstance(style, Mapping):
            return None
        name = _text(style, "name")
        if not name:
            return None
        if read.confident:
            identity = _text(style, "identity")
            body = f"My work-style read came out as {name}."
            if identity:
                body = f"{body} {identity}"
        else:
            body = (
                f"My work-style read leans toward {name}, without landing on it firmly."
            )
        return Callout(label=label, body=body)

    if read.framework in {"motivators", "enneagram"}:
        headline = _text(read.layer, "headline")
        return Callout(label=label, body=headline) if headline else None

    if read.framework == "strengths":
        themes = read.layer.get("themes") or []
        first = themes[0] if themes and isinstance(themes[0], Mapping) else None
        quote = _text(first, "evidence_quote") if first else None
        name = _text(first, "name") if first else None
        if not quote or not name:
            return None
        # The subject's own words, lifted verbatim from the scored payload — the
        # only sentence in this document that came out of their mouth, and the
        # reason this section is arguable rather than assertive.
        #
        # Screened HERE, at the point it enters, and the whole callout is dropped
        # if it does not pass. An earlier version cut every verbatim span out of
        # the assembled screen instead, which fixed the false positive it was
        # written for and removed the control entirely: a quote saying "People
        # call me an INTJ and they are not wrong" published a bare four-letter
        # code for somebody whose type16 gate was CLOSED, and "I scored 40 on
        # agreeableness" published a raw domain score, both into a
        # company-visible RAG-ingested document. Ten such strings were proven to
        # reach it.
        #
        # Dropping one callout is the right cost. The alternative that exemption
        # was avoiding — failing the entire guide, permanently, because somebody
        # mentioned a promotion in their strengths conversation — is far worse
        # than losing one citation box. And this closes the separate risk the old
        # comment flagged and did not address: a quote naming a colleague who
        # consented to nothing trips ``third_party_claim`` and never ships.
        quote_reason = guide_prompts.screen_prose(quote, max_words=_QUOTE_MAX_WORDS)
        if quote_reason:
            logger.info(
                "guide: dropping the strengths tie-in — the subject's quote did "
                "not pass the prose screen (%s). The guideline still renders; "
                "only the citation box is lost.",
                quote_reason,
            )
            return None
        return Callout(
            label=label,
            body=f'{name}, from my own words: "{quote}"',
            verbatim=quote,
        )

    if read.framework == "big_five":
        note = _text(read.layer, "bands_note")
        return Callout(label=label, body=note) if note else None

    if read.framework == "type16":
        # The colleague-safe renderer, for the reason spelled out on
        # :func:`derived_layer_line`: the payload's own ``description`` embeds the
        # raw Big Five domain scores behind any middle-band letter.
        description = type16.colleague_safe_description(read.layer)
        if not description:
            return None
        offenders = type16.find_unqualified_codes(description)
        if offenders:
            # Authored copy should never trip this; if it does, the instrument
            # changed under us and a bare code is exactly what must not ship.
            logger.error(
                "[smart-teams] guide: type16 description carries unqualified "
                "code(s) %s; dropping the tie-in",
                offenders,
            )
            return None
        reason = guide_prompts.screen_prose(description, max_words=10_000)
        if reason:
            logger.error(
                "[smart-teams] guide: type16 description failed the prose screen "
                "(%s); dropping the tie-in",
                reason,
            )
            return None
        return Callout(label=label, body=description)

    return None


def attach_tie_ins(
    guidelines: Sequence[Guideline], layers: Mapping[str, LayerRead]
) -> None:
    """Attach at most one citation per framework, to that framework's first
    guideline.

    Three in ten items in the source document; here it is however many
    reportable layers actually produced a guideline, which is the same rule
    stated without a magic number.
    """
    seen: set[str] = set()
    for guideline in guidelines:
        framework = guideline.source[0]
        if framework in seen:
            continue
        callout = _tie_in(layers[framework])
        if callout is not None:
            guideline.tie_in = callout
            seen.add(framework)


# ── Growth edge ──────────────────────────────────────────────────────────────

#: Candidates in a fixed authoring order, each naming the authored field it
#: reads. The order is a choice about which framing reads best in a document
#: somebody's colleagues see — it is NOT a ranking of severity, and nothing
#: downstream may read it as one. Exactly one is rendered.
_GROWTH_EDGE_SOURCES: tuple[tuple[str, str], ...] = (
    ("strengths", "overused"),
    ("disc", "under_stress"),
    ("enneagram", "under_stress"),
    ("motivators", "in_conflict"),
)


def growth_edge(layers: Mapping[str, LayerRead]) -> Callout | None:
    """The one "where I could use a hand" box, or ``None``.

    The strictest section in the document. A candidate needs an OPEN gate and
    confidence ``high`` or ``moderate``; a ``low``-confidence layer is not
    hedged into this box, it is skipped. If nothing qualifies the box is
    omitted — there is no hedge substitute and the model is never asked to write
    one, because a growth edge composed from thin evidence is the single most
    damaging sentence this feature could publish.
    """
    for framework, _field_name in _GROWTH_EDGE_SOURCES:
        read = layers[framework]
        if not read.strong_enough_for_growth_edge or read.layer is None:
            continue
        body: str | None = None

        if framework == "strengths":
            themes = read.layer.get("themes") or []
            first = themes[0] if themes and isinstance(themes[0], Mapping) else None
            theme_id = first.get("theme_id") if isinstance(first, Mapping) else None
            if isinstance(theme_id, str):
                try:
                    body = strengths.spec_for(theme_id).overused
                except KeyError:
                    body = None
        elif framework == "disc":
            assignment = read.layer.get("assignment")
            style = assignment.get("style") if isinstance(assignment, Mapping) else None
            body = _text(style, "under_stress") if isinstance(style, Mapping) else None
        elif framework == "enneagram":
            number = read.layer.get("type_number")
            if isinstance(number, int):
                try:
                    body = enneagram.spec_for(number).under_stress
                except KeyError:
                    body = None
        elif framework == "motivators":
            top_two = [
                m for m in (read.layer.get("top_two") or []) if isinstance(m, str)
            ]
            if top_two:
                try:
                    body = motivators.spec_for(top_two[0]).in_conflict  # type: ignore[arg-type]
                except KeyError:
                    body = None

        if body:
            return Callout(label=GROWTH_EDGE_LABEL, body=f"{body} {GROWTH_EDGE_CLOSER}")
    return None


# ── Descriptive sections ─────────────────────────────────────────────────────


def working_preferences(read: LayerRead) -> list[str]:
    """The five Big Five lines, each the instrument's authored band description.

    Omitted whole when the layer is refused. Its hedge is already carried once,
    in the completeness section, and a second copy under a heading called
    "Working preferences" would read as content.
    """
    if not read.reportable or read.layer is None:
        return []
    domains = read.layer.get("domains")
    if not isinstance(domains, Mapping):
        return []
    lines: list[str] = []
    for payload in domains.values():
        if not isinstance(payload, Mapping):
            continue
        description = _text(payload, "description")
        if description:
            lines.append(description)
    return lines


def derived_layer_line(read: LayerRead) -> str | None:
    """The four-letter layer's paragraph for a colleague-facing guide, or ``None``.

    ``type16.colleague_safe_description`` and **not** the payload's own
    ``description``. The payload's description is
    ``type16.Type16Result.describe()``, which is correct for the subject's view
    and wrong for this one: its partial-coin-flip branch interpolates the raw Big
    Five domain scores behind the middle-band letters ("40 on people-weighting;
    48 on structure and follow-through"), so a published, RAG-ingested guide
    carried the subject's raw 0-100 scores and every employee could retrieve a
    colleague's through Chat. Raw scores are the subject's and the support admins' alone.
    The same paragraph also switched to the second person inside a first-person
    document, which reads to a colleague as an invitation to go and look them up.

    Two runtime re-checks on the way out, because this is the section with the
    most ways to say something it must not:

    * ``find_unqualified_codes`` — a bare four-letter code is the one thing this
      layer may never render, qualifier or no qualifier.
    * ``guide_prompts.screen_prose`` — which carries the raw-domain-score rule, so
      a future regression in the renderer costs this section rather than
      publishing a score.

    Either one omits the section, which is the documented behaviour for a layer
    that cannot be rendered safely.
    """
    if not read.reportable or read.layer is None:
        return None
    description = type16.colleague_safe_description(read.layer)
    if not description:
        return None
    offenders = type16.find_unqualified_codes(description)
    if offenders:
        logger.error(
            "[smart-teams] guide: refusing the derived-layer section; "
            "unqualified code(s) %s",
            offenders,
        )
        return None
    reason = guide_prompts.screen_prose(description, max_words=10_000)
    if reason:
        logger.error(
            "[smart-teams] guide: refusing the derived-layer section; the "
            "colleague-safe description failed the prose screen (%s)",
            reason,
        )
        return None
    return description


def completeness(layers: Mapping[str, LayerRead]) -> list[CompletenessLine]:
    """One line per framework: included, or why this document does not say.

    The provenance section, and the only place in the document where a refusal is
    explained.

    **A refused layer's own ``hedge`` is never printed here.** The note is
    :data:`PUBLICATION_REASON_PROSE` for the first reason code, and
    :data:`COMPLETENESS_NOT_REPORTED` when there is no reason code at all — never
    a blank, because a blank line next to a layer name reads as something
    withheld. An instrument hedge is authored for the subject's own view, where
    naming a near-tie is helpful, and several of them are findings when they land
    in a document somebody's colleagues read: ``motivators`` hedges by naming its
    top two and its bottom, ``enneagram`` by naming both candidate types. Either
    one asserts, to the whole company, what the closed gate refused to report —
    under a line that says the layer is not included.

    A REPORTABLE layer's note is different and is still the instrument's own: it
    is a note about a layer whose contents this guide is already publishing (the
    Enneagram always carries one, inviting the person to correct it), so it can
    only qualify a finding that is on the page rather than add one that is not.
    """
    lines: list[CompletenessLine] = []
    for framework in contracts.FRAMEWORKS:
        read = layers[framework]
        label = FRAMEWORK_LABELS[framework]
        if read.reportable:
            note = "Included."
            if read.verdict.hedge:
                note = f"Included. {read.verdict.hedge}"
            lines.append(
                CompletenessLine(
                    framework=framework, label=label, included=True, note=note
                )
            )
            continue

        note = None
        for code in read.verdict.reasons:
            note = PUBLICATION_REASON_PROSE.get(code)
            if note:
                break
        if not note:
            note = COMPLETENESS_NOT_REPORTED
        lines.append(
            CompletenessLine(
                framework=framework,
                label=label,
                included=False,
                note=note,
                sections_to_finish=(
                    _sections_feeding(framework) if read.verdict.is_actionable else ()
                ),
            )
        )
    return lines


def framing(layers: Mapping[str, LayerRead], *, has_guidelines: bool) -> str:
    """The opening paragraph, as a slot template — one slot per open gate.

    Never "hand the model the whole profile and ask for a paragraph", which is
    how a closed gate gets papered over. A refused layer's slot is simply not
    built. With no slots at all the paragraph is fixed house copy that names no
    framework and makes no claim.
    """
    if not has_guidelines:
        return FRAMING_NO_LAYERS

    slots: list[str] = []
    disc_read = layers["disc"]
    if disc_read.reportable and disc_read.layer is not None:
        assignment = disc_read.layer.get("assignment")
        style = assignment.get("style") if isinstance(assignment, Mapping) else None
        name = _text(style, "name") if isinstance(style, Mapping) else None
        identity = _text(style, "identity") if isinstance(style, Mapping) else None
        if name and disc_read.confident and identity:
            slots.append(
                f"My work style reads as {name}: {identity[0].lower()}{identity[1:]}"
            )
        elif name:
            slots.append(f"My work style leans toward {name}")

    motivator_read = layers["motivators"]
    if motivator_read.reportable and motivator_read.layer is not None:
        names = []
        for axis in (motivator_read.layer.get("top_two") or [])[:2]:
            if isinstance(axis, str):
                try:
                    names.append(motivators.spec_for(axis).name)  # type: ignore[arg-type]
                except KeyError:
                    continue
        if names:
            slots.append(
                "what makes work feel worth doing to me is "
                + " and ".join(names).lower()
            )

    strengths_read = layers["strengths"]
    if strengths_read.reportable and strengths_read.layer is not None:
        theme_names = [
            _text(theme, "name")
            for theme in (strengths_read.layer.get("themes") or [])[:2]
            if isinstance(theme, Mapping)
        ]
        theme_names = [name for name in theme_names if name]
        if theme_names:
            slots.append(
                "the work I am most useful in is " + " and ".join(theme_names).lower()
            )

    if not slots:
        return f"{FRAMING_OPENER} {FRAMING_CLOSER}"
    middle = "; ".join(slots)
    return f"{FRAMING_OPENER} In short: {middle}. {FRAMING_CLOSER}"


# ── The LLM worker ───────────────────────────────────────────────────────────

#: The prose-writer signature :func:`build_guide` accepts. A callable rather than
#: a hard call so ST-7, an official-upload regeneration, and every test can pass
#: a stub — and so a guide is always buildable with no writer at all.
ProseWriter = Callable[..., Mapping[str, Mapping[str, str]]]


def _json_client(model_key: str, temperature: float):
    """The house JSON-over-Gemini client, imported lazily.

    Mirrors ``scoring._json_client`` deliberately: a function so tests can stub
    it without patching the SDK, lazy so importing this module does not build a
    genai client.
    """
    from rubik_core.priorities.agents.base import LLMJsonClient

    return LLMJsonClient(model_key=model_key, temperature=temperature)


@attribute_to(COST_FEATURE)
def write_prose(
    slots: Sequence[guide_prompts.ProseSlot],
    *,
    display_name: str,
    model_key: str = PROSE_MODEL_KEY,
    temperature: float = PROSE_TEMPERATURE,
    attempts: int = PROSE_ATTEMPTS,
) -> dict[str, dict[str, str]]:
    """Rewrite authored sentences into first-person guidelines. One LLM call.

    Returns ``{slot_id: {"heading": ..., "body": ...}}`` containing only the
    strings that came back **and** passed
    ``guide_prompts.screen_slot_heading`` / ``screen_slot_body`` — the
    fact-scoped screens, not the generic one, so a string that adds a claim the
    slot's authored fact does not contain is not returned. Heading and body are
    kept or dropped independently: a rejected heading is absent from the entry
    and its body may still be present. A slot the model skipped, mislabelled or
    wrote badly is simply absent, and :func:`build_guide` falls back to authored
    wording — so a bad call degrades the voice of a guide and never its content.

    ``@attribute_to`` sits here, on the function that makes the call, not on
    :func:`build_guide`: the AI Cost tag is a contextvar and a thread hop loses
    a tag set at the entry point (the 75%-"Unattributed" lesson).

    A screen rejection is retried once with the same prompt — a model that
    wandered into advancement language on one sample often does not on the next
    — and after that the authored copy wins. Never raises: the caller must be
    able to produce a document whatever Vertex is doing.
    """
    if not slots:
        return {}
    prompt = guide_prompts.prose_prompt(slots, display_name=display_name)
    client = _json_client(model_key, temperature)
    by_id = {slot.slot_id: slot for slot in slots}

    for attempt in range(1, max(1, attempts) + 1):
        try:
            payload = client.invoke_json(prompt, fallback=None, attempts=1)
        except Exception as exc:  # noqa: BLE001 — the authored copy is the floor
            logger.warning(
                "[smart-teams] guide prose call failed (attempt %d): %s",
                attempt,
                exc,
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
                    "[smart-teams] guide prose: dropping unknown slot id %r",
                    slot_id,
                )
                continue
            slot = by_id[slot_id]
            kept: dict[str, str] = {}
            for part, text, screen in (
                ("heading", entry.get("heading"), guide_prompts.screen_slot_heading),
                ("body", entry.get("body"), guide_prompts.screen_slot_body),
            ):
                reason = screen(text, slot)
                if reason:
                    rejected.append((f"{slot_id}.{part}", reason))
                    logger.warning(
                        "[smart-teams] guide prose: rejected %s of slot %s (%s)",
                        part,
                        slot_id,
                        reason,
                    )
                    continue
                kept[part] = str(text).strip()
            if kept:
                accepted[slot_id] = kept

        # Anything the screen refused means the model has demonstrated it will
        # write what it was told not to, so the whole sample is re-drawn rather
        # than partially kept. On the last attempt we keep what passed.
        if rejected and attempt < max(1, attempts):
            logger.info(
                "[smart-teams] guide prose: regenerating after %d rejected "
                "string(s): %s",
                len(rejected),
                ", ".join(f"{part} ({reason})" for part, reason in rejected),
            )
            continue
        return accepted
    return {}


# ── Assembly ─────────────────────────────────────────────────────────────────


def build_guide(
    profile: SmartTeamsProfile,
    *,
    display_name: str,
    prose_writer: ProseWriter | None = None,
) -> PersonalGuide:
    """Assemble one personal guide from one scored profile version.

    With no ``prose_writer`` the guide is entirely authored copy — which is a
    supported, shippable outcome and not a degraded mode: every sentence would
    have come from the same authored fields either way, and the writer only
    changes their voice. The same is true of a guide whose every model-written
    string was rejected: :attr:`PersonalGuide.fallbacks` will list them all and
    the document is still correct.

    **The single place model output is admitted into a document.** Every accepted
    heading and body has been through ``guide_prompts.screen_slot_heading`` /
    ``screen_slot_body`` here, against the authored fact that slot was built
    from and against the set of open gates — regardless of whether the writer
    screened anything itself. There is no other path into ``PersonalGuide``.
    """
    subject_email = (profile.user_email or "").strip().lower()
    layers = read_layers(profile)
    candidates = candidate_guidelines(layers)

    guidelines: list[Guideline] = []
    if len(candidates) >= MIN_GUIDELINES:
        guidelines = candidates
    elif candidates:
        logger.info(
            "[smart-teams] guide for %s: %d qualified guideline(s), below the "
            "floor of %d — writing the shortfall document instead",
            subject_email,
            len(candidates),
            MIN_GUIDELINES,
        )

    fallbacks: dict[str, str] = {}
    if guidelines and prose_writer is not None:
        open_frameworks = frozenset(
            name for name, read in layers.items() if read.reportable
        )
        slots = [
            guide_prompts.ProseSlot(
                slot_id=g.slot_id,
                intent=g.intent,
                fact=g.fact,
                heading=g.heading,
                body=g.body,
                open_frameworks=open_frameworks,
            )
            for g in guidelines
        ]
        by_id = {slot.slot_id: slot for slot in slots}
        try:
            written = prose_writer(slots, display_name=display_name) or {}
        except Exception:  # noqa: BLE001 — authored copy is always available
            logger.exception(
                "[smart-teams] guide for %s: prose writer failed; using authored copy",
                subject_email,
            )
            written = {}
        for guideline in guidelines:
            entry = written.get(guideline.slot_id)
            if not isinstance(entry, Mapping):
                continue
            slot = by_id[guideline.slot_id]
            # Screened again on the way in, and against the slot's own fact.
            # write_prose already did this; this is the belt, because a stub
            # writer (ST-7, a test, a future caller) reaches this function
            # without having been through it — and that is precisely the path a
            # fabricated body took into a published guide.
            #
            # Heading and body are decided independently. Coupling them meant a
            # false positive on a nine-word title-case heading also threw away a
            # perfectly good body, which buys nothing: both fall back to authored
            # copy that was written to be safe on its own.
            heading_reason = guide_prompts.screen_slot_heading(
                entry.get("heading"), slot
            )
            if heading_reason:
                fallbacks[f"{guideline.slot_id}.heading"] = heading_reason
            else:
                guideline.heading = str(entry.get("heading")).strip()
                guideline.model_written = True

            body_reason = guide_prompts.screen_slot_body(entry.get("body"), slot)
            if body_reason:
                fallbacks[f"{guideline.slot_id}.body"] = body_reason
            else:
                guideline.body = str(entry.get("body")).strip()
                guideline.model_written = True

        for part, reason in sorted(fallbacks.items()):
            logger.warning(
                "[smart-teams] guide for %s: %s fell back to authored copy (%s)",
                subject_email,
                part,
                reason,
            )
        if fallbacks:
            logger.info(
                "[smart-teams] guide for %s: %d of %d model-written string(s) "
                "fell back to authored copy",
                subject_email,
                len(fallbacks),
                2 * len(guidelines),
            )

    if guidelines:
        attach_tie_ins(guidelines, layers)

    return PersonalGuide(
        subject_email=subject_email,
        display_name=display_name,
        profile_version=int(profile.version),
        title=TITLE_TEMPLATE.format(name=display_name),
        deck=DECK,
        framing=framing(layers, has_guidelines=bool(guidelines)),
        guidelines=guidelines,
        growth_edge=growth_edge(layers) if guidelines else None,
        working_preferences=(
            working_preferences(layers["big_five"]) if guidelines else []
        ),
        derived_layer=derived_layer_line(layers["type16"]) if guidelines else None,
        completeness=completeness(layers),
        final_thought=FINAL_THOUGHT if guidelines else None,
        shortfall=None if guidelines else SHORTFALL_BODY,
        fallbacks=fallbacks,
    )


# ── Markdown ─────────────────────────────────────────────────────────────────


def _caps(text: str) -> str:
    """A structural heading, in the source format's case.

    The source sets every STRUCTURAL heading in caps — "HERE ARE 10 THINGS TO
    KEEP IN MIND", "ASSESSMENT TIE-IN", "GROWTH EDGE", "FINAL THOUGHT" — and
    leaves the numbered guideline headings in title case. Applied at render time
    rather than baked into the constants so the labels stay reusable as ordinary
    prose elsewhere (the API returns several of them as field names).
    """
    return text.upper()


def _first_name(display_name: str) -> str:
    """The sign-off name. Falls back to the whole string rather than to nothing.

    A single-token name, a name with a middle name, and an empty string all have
    to produce something signable; the source's sign-off is a first name, so
    anything else is better rendered whole than truncated wrongly.
    """
    cleaned = (display_name or "").strip()
    return cleaned.split()[0] if cleaned else cleaned


def render_markdown(guide: PersonalGuide) -> str:
    """The canonical body. ``st_documents.content_md`` holds exactly this.

    Markdown rather than HTML because it is what the person edits before
    approving (``content_md`` is documented as "the editable draft body"), what
    the RAG copy ingests, and what the Word export is rendered from. The section
    order follows the format spec: cover, deck, framing, the numbered
    guidelines with their tie-ins, growth edge, the descriptive layers, the
    completeness table, the final thought, the advisory notice.
    """
    out: list[str] = [f"# {guide.title}", "", guide.deck, "", guide.framing, ""]

    if guide.shortfall:
        out += [f"## {_caps(SHORTFALL_LABEL)}", "", guide.shortfall, ""]

    if guide.guidelines:
        count = len(guide.guidelines)
        noun = "thing" if count == 1 else "things"
        out += [f"## {_caps(CONTENTS_TEMPLATE.format(count=count, noun=noun))}", ""]
        # The page-1 index. Emitted as an ordinary list so it survives the
        # person editing ``content_md`` and reads correctly on the web; the Word
        # export recognises the run that follows this heading and sets it as the
        # source format's two-column grid. Zero-padded because the source is.
        out += [
            f"- **{index:02d}**  {guideline.heading}"
            for index, guideline in enumerate(guide.guidelines, start=1)
        ]
        out += [""]
        for index, guideline in enumerate(guide.guidelines, start=1):
            out += [f"### {index}. {guideline.heading}", "", guideline.body, ""]
            if guideline.tie_in:
                out += [
                    f"> **{guideline.tie_in.label}** — {guideline.tie_in.body}",
                    "",
                ]

    if guide.growth_edge:
        out += [
            f"## {_caps(guide.growth_edge.label)}",
            "",
            guide.growth_edge.body,
            "",
        ]

    if guide.working_preferences:
        out += [f"## {_caps(FRAMEWORK_LABELS['big_five'])}", ""]
        out += [f"- {line}" for line in guide.working_preferences]
        out += [""]

    if guide.derived_layer:
        out += [
            f"## {_caps(FRAMEWORK_LABELS['type16'])}",
            "",
            guide.derived_layer,
            "",
        ]

    if guide.final_thought:
        out += [f"## {_caps(FINAL_THOUGHT_LABEL)}", "", guide.final_thought, ""]
        out += [
            SIGNATURE_TEMPLATE.format(first_name=_first_name(guide.display_name)),
            "",
        ]

    # ── End matter ───────────────────────────────────────────────────────────
    # Below the signature ON PURPOSE, and this is the one place this renderer
    # departs from the source format rather than matching it.
    #
    # Neither source guide has a completeness table or an advisory notice, so
    # "make it exactly the same" would mean deleting both. Both are load-bearing
    # and neither is ours to drop: the completeness table is how a reader learns
    # that a layer was WITHHELD rather than that the person has no such trait —
    # delete it and a half-answered interview reads as a finished portrait — and
    # the advisory notice is the guardrail that keeps a company-visible document
    # about a person out of performance and promotion territory.
    #
    # So they keep their content and lose their prominence: after the sign-off,
    # where the source format puts nothing, which is the closest thing to
    # matching it that does not throw information away.
    out += ["---", ""]
    out += [f"## {_caps(COMPLETENESS_LABEL)}", "", COMPLETENESS_INTRO, ""]
    for line in guide.completeness:
        out.append(f"- **{line.label}** — {line.note}")
        if line.sections_to_finish:
            out.append(
                "  - Would be filled in by: "
                + ", ".join(f'"{title}"' for title in line.sections_to_finish)
            )
    out += [""]

    out += [f"*{guide.advisory_notice}*", ""]
    return "\n".join(out).strip() + "\n"


# ── Word export ──────────────────────────────────────────────────────────────
# Reuses the house docx primitives by importing the private helpers from
# ``deep_prep/docx.py``, which is the established pattern here
# (``xray/docx.py`` imports the same set).
#
# BRAND COLOUR: Cubie violet from ``rubik_core.brand``, through
# ``deep_prep/docx.BRAND``, like every other renderer in this repo. The source
# format's own accent colour is not reproduced.

#: Cubie's off-white tint for the callout fill, the analogue of the source
#: format's tint of its own accent.
_CALLOUT_FILL = TINT_HEX.lstrip("#")


def _shade_cell(cell, fill: str) -> None:
    """Give a table cell a solid background.

    The first ``w:shd`` in this repo. python-docx has no API for cell shading,
    and the tinted callout is the one device in the source format that carries
    the framework citations — a bordered box with no fill reads as a table.
    Written as raw OXML because that is the only way to say it; four attributes,
    no schema guessing.
    """
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    shading = OxmlElement("w:shd")
    shading.set(qn("w:val"), "clear")
    shading.set(qn("w:color"), "auto")
    shading.set(qn("w:fill"), fill)
    cell._tc.get_or_add_tcPr().append(shading)


def _callout_table(doc, callout: Callout, *, include_label: bool = True) -> None:
    """One tinted single-cell box — the citation device from the format spec."""
    from docx.shared import Pt

    from rubik_core.deep_prep.docx import BRAND

    table = doc.add_table(rows=1, cols=1)
    cell = table.rows[0].cells[0]
    _shade_cell(cell, _CALLOUT_FILL)
    paragraph = cell.paragraphs[0]
    # An empty label draws nothing rather than two leading spaces: the growth-edge
    # box already has its own violet heading above it, and a callout recovered
    # from markdown may not have carried a label at all.
    if include_label and callout.label.strip():
        label = paragraph.add_run(f"{callout.label.upper()}  ")
        label.font.bold = True
        label.font.size = Pt(8)
        label.font.color.rgb = BRAND
    body = paragraph.add_run(callout.body)
    body.font.size = Pt(9.5)


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower())
    return cleaned.strip("-") or "guide"


# ── Persistence ──────────────────────────────────────────────────────────────


class GuideError(RuntimeError):
    """A guide could not be generated, approved, published or purged.

    Raised rather than returned: every caller is an HTTP handler or a
    cubie-worker job, and both have to tell the difference between "wrote a
    guide" and "wrote nothing".
    """


@dataclass(frozen=True)
class GuideRow:
    """An ``st_documents`` row plus whether this call created it."""

    document: SmartTeamsDocument
    created: bool


def _fold(email: str | None) -> str:
    return (email or "").strip().lower()


def cache_keys_for(subject_email: str, version: int) -> tuple[str, str]:
    """``document_cache_keys`` for a personal guide. One caller-facing helper so
    the subject/version canonicalisation is never re-derived."""
    email = _fold(subject_email)
    return document_cache_keys(KIND_PERSONAL, [email], {email: int(version)})


def current_guide(
    db: Session, subject_email: str, *, version: int | None = None
) -> SmartTeamsDocument | None:
    """The guide row for this person, optionally for one profile version.

    Without ``version``: the newest row for the subject, whatever it was
    generated from. With it: the exact cache entry, which is how a caller asks
    "is there already a guide for the profile they have now".
    """
    email = _fold(subject_email)
    query = db.query(SmartTeamsDocument).filter(
        SmartTeamsDocument.kind == KIND_PERSONAL,
        func.lower(SmartTeamsDocument.subject_key) == email,
    )
    if version is not None:
        _subject_key, version_key = cache_keys_for(email, version)
        query = query.filter(SmartTeamsDocument.profile_version_key == version_key)
    return query.order_by(SmartTeamsDocument.created_at.desc()).first()


def published_guide(db: Session, subject_email: str) -> SmartTeamsDocument | None:
    """The newest PUBLISHED guide for this person, or ``None``.

    What a colleague reads. Separate from :func:`current_guide` because after a
    retake the newest row is a fresh draft nobody has approved, and serving that
    to the company is exactly the consent failure ``st_documents``' CHECK
    constraint exists to prevent.
    """
    email = _fold(subject_email)
    return (
        db.query(SmartTeamsDocument)
        .filter(
            SmartTeamsDocument.kind == KIND_PERSONAL,
            func.lower(SmartTeamsDocument.subject_key) == email,
            SmartTeamsDocument.published.is_(True),
        )
        .order_by(SmartTeamsDocument.created_at.desc())
        .first()
    )


def is_stale(document: SmartTeamsDocument, current_version: int | None) -> bool:
    """Whether the subject's profile has advanced past what this guide cites.

    A single indexed string comparison, which is the whole reason
    ``profile_version_key`` exists — no JSON diff and no background invalidation
    pass. ``None`` (no current profile at all) is not stale: there is nothing
    newer to be stale against.
    """
    if current_version is None:
        return False
    _subject_key, version_key = cache_keys_for(
        document.subject_key or "", current_version
    )
    return (document.profile_version_key or "") != version_key


def ensure_guide_row(
    db: Session, profile: SmartTeamsProfile, *, now: datetime | None = None
) -> GuideRow:
    """Find or create the ``pending`` row for this profile version.

    Idempotent through ``uq_st_document_cache``: the triple
    ``(kind, subject_key, profile_version_key)`` is the cache identity, so a
    double-clicked Generate lands one row. The row exists before its prose does
    because generation runs on cubie-worker — the endpoint returns 202 with an
    id, and the job fills it in.
    """
    email = _fold(profile.user_email)
    if not email:
        raise GuideError("cannot generate a guide for a profile with no subject")
    version = int(profile.version)
    subject_key, version_key = cache_keys_for(email, version)

    existing = (
        db.query(SmartTeamsDocument)
        .filter(
            SmartTeamsDocument.kind == KIND_PERSONAL,
            SmartTeamsDocument.subject_key == subject_key,
            SmartTeamsDocument.profile_version_key == version_key,
        )
        .one_or_none()
    )
    if existing is not None:
        return GuideRow(document=existing, created=False)

    stamp = now or datetime.utcnow()
    row = SmartTeamsDocument(
        id=uuid.uuid4().hex,
        kind=KIND_PERSONAL,
        subject_emails=[email],
        subject_key=subject_key,
        profile_versions={email: version},
        profile_version_key=version_key,
        status="pending",
        published=False,
        rag_ingested=False,
        created_at=stamp,
        updated_at=stamp,
    )
    db.add(row)
    db.flush()
    return GuideRow(document=row, created=True)


def generate_into_row(
    db: Session,
    document: SmartTeamsDocument,
    profile: SmartTeamsProfile,
    *,
    display_name: str,
    prose_writer: ProseWriter | None = None,
    now: datetime | None = None,
) -> PersonalGuide:
    """Build the guide and store it on ``document``. Flips ``pending`` → ``ready``.

    Refuses to overwrite a PUBLISHED row. The published text is the thing its
    subject consented to; regenerating over it would publish words they never
    read. A retake produces a new cache key and therefore a new row, which is
    the supported path.
    """
    if bool(document.published):
        raise GuideError(
            "This guide is published. Unpublish it before regenerating — the "
            "published wording is what you approved."
        )
    guide = build_guide(profile, display_name=display_name, prose_writer=prose_writer)
    body = render_markdown(guide)

    # One last screen over the assembled body. Cheap, and it catches a string
    # that reached the document through a path that forgot the per-slot screen.
    #
    # ONE span is cut out before screening, and it is not a loophole: the
    # advisory notice. It is the sentence promising this profile is "never used
    # for performance review, promotion, or compensation decisions", so it
    # contains the word the advancement pattern matches on, and screening it
    # rejected literally every guide — the disclaimer that enforces the advisory
    # boundary tripped the check that enforces the advisory boundary. Fixed copy,
    # imported verbatim from ``disc.ADVISORY_NOTICE``.
    #
    # The subject's verbatim quote used to be cut out here too. It is not any
    # more: that exemption removed the qualifier rule and the closed-gate rule
    # from the one span of the document nobody was checking. The quote is now
    # screened where it enters, in ``_tie_in``, and a quote that does not pass
    # costs its callout instead of the whole guide — see the comment there.
    #
    # So everything in the body except the advisory notice is screened, and every
    # model-written string was already screened against its own authored fact in
    # build_guide.
    screenable = body.replace(guide.advisory_notice, "")
    reason = guide_prompts.screen_prose(screenable, max_words=10_000)
    if reason:
        document.status = "failed"
        document.error = f"assembled guide failed the prose screen: {reason}"
        document.updated_at = now or datetime.utcnow()
        raise GuideError(document.error)

    document.title = guide.title
    document.content_md = body
    document.status = "ready"
    document.error = None
    document.updated_at = now or datetime.utcnow()
    return guide


def fail_row(db: Session, document: SmartTeamsDocument, message: str) -> None:
    """Record a generation failure on the row, so the UI can say so."""
    document.status = "failed"
    document.error = (message or "")[:500] or "generation failed"
    document.updated_at = datetime.utcnow()


def approve(
    db: Session,
    document: SmartTeamsDocument,
    *,
    approver_email: str,
    now: datetime | None = None,
) -> list[SmartTeamsDocument]:
    """Approve and publish, and retire whatever the subject published before.

    The ONLY writer of ``published = True`` in this feature, and it sets
    ``approved_at`` in the same assignment — ``ck_st_document_published_needs_
    approval`` refuses the row otherwise, and that constraint is the last line
    between a bug here and somebody's guide being company-visible without their
    consent.

    Returns the previously-published rows it retired. Each one's corpus copy has
    to be purged by the caller: approving version 3 withdraws consent for the
    version-2 wording, and a superseded guide left in the corpus keeps answering
    in Chat.
    """
    if document.status != "ready":
        raise GuideError(
            "This guide is not ready yet. Approving would publish an empty document."
        )
    if not (document.content_md or "").strip():
        raise GuideError("This guide has no body to publish.")
    approver = _fold(approver_email)
    if approver != _fold(document.subject_key):
        # Belt for the router's authz call. A personal guide is one person's
        # consent about themselves and nobody may give it for them — not an
        # admin, not an exec.
        raise GuideError(
            "Only the person a guide is about can approve it for publication."
        )

    stamp = now or datetime.utcnow()
    superseded = [
        row
        for row in db.query(SmartTeamsDocument)
        .filter(
            SmartTeamsDocument.kind == KIND_PERSONAL,
            func.lower(SmartTeamsDocument.subject_key) == approver,
            SmartTeamsDocument.published.is_(True),
        )
        .all()
        if row.id != document.id
    ]
    for row in superseded:
        row.published = False
        row.unpublished_at = stamp
        row.updated_at = stamp

    document.approved_at = stamp
    document.approved_by = approver
    document.published = True
    document.unpublished_at = None
    document.updated_at = stamp
    return superseded


def unpublish(
    db: Session, document: SmartTeamsDocument, *, now: datetime | None = None
) -> None:
    """Retract a published guide. First-class state, never a delete.

    ``approved_at`` is left alone: it is the record that consent was once given,
    and clearing it would make the audit trail claim the guide was never
    approved. ``rag_ingested`` is left alone too — it is still true, and it stays
    true until :func:`purge_guide_vectors` proves otherwise.
    """
    stamp = now or datetime.utcnow()
    document.published = False
    document.unpublished_at = stamp
    document.updated_at = stamp


# ── RAG: the one write this feature makes, and its reversal ──────────────────

#: The corpus an approved guide goes into. Every employee may read every
#: published guide, so companywide is the correct scope and the only one.
GUIDE_RAG_SLUG = "companywide"

#: Marks the ragFile in listings and in a chunk's own text.
GUIDE_RAG_TAG = "smart-teams-guide"


def rag_filename(subject_email: str) -> str:
    """Deterministic basename, keyed on the subject and NOT on the version.

    Deliberate: ``RagManager.update_document`` matches the ragFile to replace by
    its target GCS URI, so a stable name makes a regenerate a replacement.
    Versioning the filename would leave the pre-retake text alive in retrieval —
    the exact defect ``ingestion/eos/pipeline._purge_stale_ragfiles`` documents
    ("Vertex ``import_files`` creates a NEW ragFile every time").
    """
    return f"smart_teams_guide__{_slug(_fold(subject_email)) or 'unknown'}.md"


def rag_document_text(document: SmartTeamsDocument, *, display_name: str) -> str:
    """The text that goes into the corpus: a self-describing header, then the body.

    The header follows ``EosDoc.text``'s shape for the same reason — a retrieved
    chunk has to say what it is and who it is about, because a chunk that reads
    as anonymous advice about "them" is worse than no chunk.
    """
    email = _fold(document.subject_key)
    versions = document.profile_versions or {}
    version = versions.get(email, "?")
    header = "\n".join(
        [
            f"# Working guide: {display_name} ({email})",
            "",
            f"- Kind: {GUIDE_RAG_TAG}",
            f"- Subject: {display_name} <{email}>",
            f"- Profile version: {version}",
            f"- Approved by its subject: {document.approved_at}",
            "- Advisory only: describes working preferences the subject "
            "reported about themselves. Never a measure of performance, "
            "potential, or fitness for a role.",
        ]
    )
    return f"{header}\n\n---\n\n{document.content_md or ''}"


def ingest_published_guide(
    db: Session,
    document: SmartTeamsDocument,
    *,
    display_name: str,
    manager_factory: Callable[[], Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Put an APPROVED, PUBLISHED guide into the companywide corpus.

    Refuses anything else. This is the only RAG write Smart Teams makes: never a
    score, never a transcript, never a draft. ``update_document`` rather than
    ``add_document`` so a regenerate replaces the previous ragFile instead of
    stacking a second copy of a person's description next to it.

    Runs on cubie-worker: a Vertex import serialises behind other operations on
    the same corpus and backs off for minutes, which the CPU-throttled web
    service cannot survive.
    """
    if not bool(document.published) or document.approved_at is None:
        raise GuideError(
            "Only a published, approved guide may be ingested. "
            f"(published={bool(document.published)}, "
            f"approved_at={document.approved_at!r})"
        )
    import tempfile

    if manager_factory is None:
        # FENCE. Defaulting to the real RagManager with no guard meant a
        # workstation run dialled Vertex and wrote synthetic guides into the LIVE
        # companywide corpus — and because the calling job catches the exception,
        # the suite reported green while doing it. Two fake guides for test
        # addresses ended up retrievable in Chat by every employee, rewritten on
        # every run.
        #
        # The discriminator is the RUNTIME, not APP_MODE. APP_MODE resolves to
        # GCP on a developer workstation here (checked, not assumed — the first
        # version of this fence keyed on APP_MODE != GCP and protected nothing at
        # all), and local runs already resolve to the deployed dev database, so
        # "it is only local" was never a defence either.
        #
        # ``running_in_managed_runtime`` accepts only Cloud Run Job markers.
        # A serving revision must not write the shared guide corpus. It is the
        # same primitive the migration guard uses to authorize remote schema
        # changes. A test that wants to exercise this path passes
        # ``manager_factory``; nothing else reaches the corpus from a laptop.
        from rubik_core.database.migration_guard import running_in_managed_runtime

        if not running_in_managed_runtime():
            logger.error(
                "[smart-teams] REFUSING to ingest guide %s: not running inside "
                "a Cloud Run worker Job. This path writes to the live companywide RAG corpus, "
                "which every employee can retrieve from Chat. Pass "
                "manager_factory to exercise it in a test.",
                document.id,
            )
            raise GuideError(
                "guide RAG ingest is refused outside the Cloud Run worker Job — it writes to the "
                "live companywide corpus"
            )

        from rubik_core.rag.manager import RagManager

        manager_factory = RagManager
    manager = manager_factory()

    text = rag_document_text(document, display_name=display_name)
    with tempfile.TemporaryDirectory(prefix="st-guide-") as tmp:
        path = Path(tmp) / rag_filename(document.subject_key or "")
        path.write_text(text, encoding="utf-8")
        result = manager.update_document(GUIDE_RAG_SLUG, path)

    stamp = now or datetime.utcnow()
    document.gcs_uri = result.get("gcs_uri")
    document.rag_ingested = True
    document.rag_ingested_at = stamp
    document.updated_at = stamp
    logger.info(
        "[smart-teams] ingested guide %s for %s into %s (%s)",
        document.id,
        document.subject_key,
        GUIDE_RAG_SLUG,
        document.gcs_uri,
    )
    return result


def purge_guide_vectors(
    db: Session,
    document: SmartTeamsDocument,
    *,
    purge: Callable[..., bool] | None = None,
    now: datetime | None = None,
) -> bool:
    """Remove an unpublished guide's chunks from the corpus. True only if gone.

    Both arguments the DAM routing asymmetry needs are passed explicitly:
    ``source_path`` (the stored ``gcs_uri``) and ``is_company_wide=True``. Omit
    either and ``delete_document_vectors`` skips its first branch entirely —
    ``if slug and source_path and ...`` — and only the default-corpus fallback
    runs, which is precisely where an "unpublished" guide keeps answering in
    Chat.

    ``rag_ingested`` is cleared ONLY on a True return, and this function refuses
    to run against a still-published row. A False return leaves the row saying
    the guide is still in the corpus, which is the truth: the caller must treat
    it as a failure, exactly as ``backend/routers/assets.py`` does before
    marking a document archived.

    Note for whoever reads a passing local test: off-GCP,
    ``delete_document_vectors`` returns True unconditionally without proving
    anything was removed. A local pass is not evidence that the purge works in
    production.
    """
    if bool(document.published):
        raise GuideError(
            "Refusing to purge a published guide's chunks — unpublish it first, "
            "or the app would hide a document that Chat can still quote."
        )
    if not document.rag_ingested:
        return True
    if not document.gcs_uri:
        # Nothing to match on. delete_document_vectors would fall straight
        # through to the default-corpus adapter, which returns False for a
        # missing source_path — so say so here rather than reporting a purge
        # that could not have happened.
        logger.error(
            "[smart-teams] guide %s is RAG-ingested with no gcs_uri; the "
            "corpus copy cannot be located and must be removed by hand",
            document.id,
        )
        return False

    if purge is None:
        from rubik_core.dam.vectorstore import delete_document_vectors

        purge = delete_document_vectors

    removed = bool(
        purge(
            document.id,
            source_path=document.gcs_uri,
            is_company_wide=True,
            also_delete_gcs=True,
        )
    )
    if removed:
        stamp = now or datetime.utcnow()
        document.rag_ingested = False
        document.rag_ingested_at = None
        document.gcs_uri = None
        document.updated_at = stamp
        logger.info("[smart-teams] purged guide %s from the corpus", document.id)
    else:
        logger.error(
            "[smart-teams] guide %s was NOT purged; it stays answerable in "
            "Chat and rag_ingested stays true",
            document.id,
        )
    return removed


# ── Export helpers used by the router ────────────────────────────────────────


@dataclass(frozen=True)
class GuideBlock:
    """One renderable piece of a guide's stored markdown.

    Exists so the Word and PDF exports walk the SAME parse. Both render from
    ``content_md`` rather than from the ``PersonalGuide`` object — the person may
    edit the draft before approving, and a renderer that rebuilt from the profile
    would export words they had already changed — which means both have to read
    that markdown back. Two hand-rolled parsers for one artifact is the drift
    this module already warns about for renderers; the parser is the half that
    would drift silently, because a mis-parsed block renders as *something*.

    ``kind`` is one of: ``title`` (the ``# `` line, already in the masthead),
    ``heading`` (``## ``), ``guideline`` (``### {n}. {title}``), ``para``,
    ``bullets``, ``contents`` (the page-1 index), ``callout`` (``> ``), ``muted``
    (a fully-italic line) and ``rule`` (``---``).
    """

    kind: str
    text: str = ""
    numeral: str = ""
    items: tuple[str, ...] = ()
    callout: Callout | None = None


def parse_guide_blocks(markdown: str) -> list[GuideBlock]:
    """Read stored guide markdown back into renderable blocks.

    The page-1 index and an ordinary bullet list are the same markdown; only
    position tells them apart, so the contents heading arms a flag that the next
    bullet run consumes. That flag is cleared when a run is actually emitted, not
    on every blank line — the markdown puts a blank line between the heading and
    its first entry, and clearing it there was a real bug that rendered the index
    as plain paragraphs.
    """
    blocks: list[GuideBlock] = []
    bullets: list[str] = []
    in_contents = False

    def flush() -> None:
        nonlocal in_contents
        if bullets:
            blocks.append(
                GuideBlock(
                    kind="contents" if in_contents else "bullets",
                    items=tuple(bullets),
                )
            )
            bullets.clear()
            in_contents = False

    for raw in (markdown or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            flush()
            continue
        if line.startswith("# "):
            flush()
            blocks.append(GuideBlock(kind="title", text=line[2:].strip()))
            continue
        if line.startswith("### "):
            flush()
            text = line[4:].strip()
            numbered = _MD_NUMBERED_HEADING.match(text)
            if numbered:
                blocks.append(
                    GuideBlock(
                        kind="guideline",
                        numeral=numbered.group("n"),
                        text=_strip_inline_markdown(numbered.group("title")),
                    )
                )
            else:
                blocks.append(
                    GuideBlock(kind="guideline", text=_strip_inline_markdown(text))
                )
            continue
        if line.startswith("## "):
            flush()
            text = line[3:].strip()
            blocks.append(GuideBlock(kind="heading", text=text))
            in_contents = bool(_MD_CONTENTS_HEADING.match(text))
            continue
        if line.startswith("> "):
            flush()
            blocks.append(
                GuideBlock(kind="callout", callout=_callout_from_markdown(line[2:]))
            )
            continue
        if line.lstrip().startswith("- "):
            bullets.append(line.lstrip()[2:].strip())
            continue
        if line.strip() == "---":
            flush()
            blocks.append(GuideBlock(kind="rule"))
            continue
        flush()
        if line.startswith("*") and line.endswith("*"):
            blocks.append(GuideBlock(kind="muted", text=line.strip("*")))
            continue
        blocks.append(GuideBlock(kind="para", text=line))
    flush()
    return blocks


def split_contents_entry(entry: str) -> tuple[str, str]:
    """``- **01**  Heading`` → ``("01", "Heading")``; anything else → ``("", entry)``."""
    match = _MD_CONTENTS_ENTRY.match(entry)
    if match is None:
        return "", _strip_inline_markdown(entry)
    return match.group("n"), _strip_inline_markdown(match.group("title"))


def _page_field(paragraph) -> None:
    """Append a live ``PAGE`` field. The source footer numbers every page.

    python-docx has no field API, so this is the three-part OXML a Word field
    needs (begin / instrText / end). Without it the footer could only carry a
    frozen number, which is worse than none on a document whose length varies
    with how much of the interview somebody finished.
    """
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = " PAGE "
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    for node in (begin, instr, end):
        run._r.append(node)
    return run


def _masthead_kicker(doc, text: str) -> None:
    """Rewrite the house masthead kicker to the source format's exact wording.

    ``deep_prep/docx._new_doc`` prefixes every kicker with the product name,
    which is right for Deep Prep and X-Ray and is not what the two source guides
    say: theirs read simply "A PERSONAL WORKING GUIDE". Rewritten here rather
    than parameterised in the shared helper, because the prefix is correct for
    every other document type that helper builds.
    """
    if not doc.paragraphs:
        return
    kicker = doc.paragraphs[0]
    if not kicker.runs:
        return
    kicker.runs[0].text = text
    for extra in kicker.runs[1:]:
        extra.text = ""


def _running_footer(doc, display_name: str) -> None:
    """Replace the house confidential footer with the source format's own.

    The source runs ``WORKING WITH JORDAN HAYES   1 / 5`` on every page. The house
    helper writes a confidential line instead, and that line is a real
    obligation on an internal document about a person, so this keeps both: the
    running title on the left, the confidential note beneath it, small.

    Written here rather than in ``deep_prep/docx._new_doc`` deliberately — that
    helper is shared by Deep Prep and X-Ray, and giving every document type a
    per-person running title would be wrong for both.
    """
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    from rubik_core.deep_prep.docx import BRAND, MUTED

    footer = doc.sections[0].footer
    running = footer.paragraphs[0]
    for existing in list(running.runs):
        existing._r.getparent().remove(existing._r)
    running.alignment = WD_ALIGN_PARAGRAPH.LEFT
    label = running.add_run(f"WORKING WITH {(display_name or '').upper()}   ")
    label.font.size = Pt(7.5)
    label.font.bold = True
    label.font.color.rgb = BRAND
    page = _page_field(running)
    page.font.size = Pt(7.5)
    page.font.color.rgb = MUTED

    note = footer.add_paragraph()
    note.alignment = WD_ALIGN_PARAGRAPH.LEFT
    run = note.add_run(
        f"{PRODUCT_NAME} — Confidential. AI-generated; verify before use."
    )
    run.font.size = Pt(7)
    run.font.color.rgb = MUTED


def _contents_grid(doc, entries: list[str]) -> None:
    """The page-1 index, as the source's two-column grid.

    Filled DOWN each column rather than across, which is what the source does:
    01-05 sit in the left column and 06-10 in the right. Reading order is the
    thing being preserved — a reader scanning the left column should get the
    first half of the guide, not every other item.
    """
    from docx.shared import Pt

    from rubik_core.deep_prep.docx import BRAND

    if not entries:
        return
    rows = (len(entries) + 1) // 2
    table = doc.add_table(rows=rows, cols=2)
    table.autofit = True
    for position, entry in enumerate(entries):
        row, column = (position % rows, position // rows)
        cell = table.rows[row].cells[column]
        paragraph = cell.paragraphs[0]
        paragraph.paragraph_format.space_after = Pt(4)
        match = _MD_CONTENTS_ENTRY.match(entry)
        numeral_text, title = (
            (match.group("n"), match.group("title")) if match else ("", entry)
        )
        if numeral_text:
            numeral = paragraph.add_run(f"{numeral_text}  ")
            numeral.font.bold = True
            numeral.font.size = Pt(10)
            numeral.font.color.rgb = BRAND
        run = paragraph.add_run(_strip_inline_markdown(title))
        run.font.size = Pt(9.5)


def export_docx(
    document: SmartTeamsDocument, *, display_name: str
) -> tuple[bytes, str]:
    """The branded Word export: masthead, violet numerals, tinted callouts.

    **The only .docx renderer here, and it renders from the STORED
    ``content_md``** rather than by rebuilding the guide from the profile. That is
    forced, not a preference: ``content_md`` is the editable draft body, so the
    person may revise it before approving, and a renderer that walked the
    ``PersonalGuide`` object again would silently export words they had already
    changed. An earlier pass of this module had both — a structured
    ``render_docx`` and this one — and the structured one could never be used for
    a real export for exactly that reason. Two renderers for one artifact is how
    the two drift (``rubik_core/people/export.py`` says the same thing), so there
    is one.

    **What is built, and what is deferred.** Built: the violet masthead and
    confidential footer from the house helpers, violet section headings, a violet
    hanging numeral per guideline, and a tinted single-cell table for every
    tie-in citation and the growth edge — the ``w:shd`` shading in
    :func:`_shade_cell` is the first in this repo, because a bordered box with no
    fill reads as a table rather than as a citation.

    Deferred, and named rather than half-built: a typographic reproduction of the
    two source PDFs. Those are set in Archivo Narrow and Source Sans 3 at measured
    point sizes with drawn accent bars and a 2-column contents grid, and neither
    font family ships in this repo. There is also **no PDF twin** — the house PDF
    renderer (``deep_prep/pdf.py``) is latin-1 only, so a name outside latin-1
    would render as ``?`` in a document about that person, which is worse than
    having no PDF. Both need the fonts embedded and ``fpdf2`` geometry written
    from the format spec's measurements. The Markdown body is the canonical
    artifact; this .docx is the branded, editable one.

    **The brand colour is Cubie violet**, from ``deep_prep/docx.BRAND``, which
    is what every other renderer in this repo uses.
    """
    body = (document.content_md or "").strip()
    if not body:
        raise GuideError("This guide has no body to export yet.")
    from docx.shared import Pt

    from rubik_core.deep_prep.docx import (
        BRAND,
        _bullets,
        _docx_bytes,
        _heading,
        _muted_para,
        _new_doc,
    )

    email = _fold(document.subject_key)
    versions = document.profile_versions or {}
    doc = _new_doc(
        "A PERSONAL WORKING GUIDE",
        document.title or TITLE_TEMPLATE.format(name=display_name),
        PROVENANCE_TEMPLATE.format(name=display_name, version=versions.get(email, "?")),
    )
    _masthead_kicker(doc, "A PERSONAL WORKING GUIDE")
    _running_footer(doc, display_name)

    for block in parse_guide_blocks(body):
        if block.kind == "title":
            continue  # already the masthead title
        if block.kind == "guideline":
            paragraph = doc.add_paragraph()
            paragraph.paragraph_format.space_before = Pt(10)
            paragraph.paragraph_format.space_after = Pt(2)
            # The format spec's hanging numeral, in violet, with the heading in
            # neutral ink. An unnumbered level-3 heading (nothing writes one
            # today) renders as a plain bold line.
            if block.numeral:
                numeral = paragraph.add_run(f"{block.numeral}  ")
                numeral.font.bold = True
                numeral.font.size = Pt(15)
                numeral.font.color.rgb = BRAND
            run = paragraph.add_run(block.text)
            run.font.bold = True
            run.font.size = Pt(12)
            continue
        if block.kind == "heading":
            _heading(doc, block.text)
            continue
        if block.kind == "callout":
            _callout_table(doc, block.callout)
            continue
        if block.kind == "contents":
            _contents_grid(doc, list(block.items))
            continue
        if block.kind == "bullets":
            _bullets(doc, list(block.items))
            continue
        if block.kind == "rule":
            continue
        if block.kind == "muted":
            _muted_para(doc, block.text)
            continue
        paragraph = doc.add_paragraph(block.text)
        paragraph.runs[0].font.size = Pt(10.5)

    slug = _slug(display_name or email)
    return _docx_bytes(doc), f"how-to-work-with-{slug}.docx"


def export_pdf(document: SmartTeamsDocument, *, display_name: str) -> tuple[bytes, str]:
    """The PDF export — the copy meant to be sent, rather than edited.

    Same source as :func:`export_docx`: the STORED ``content_md``, through the
    same :func:`parse_guide_blocks`. Both exports therefore say the same thing
    about the same guide, which is the only way two renderers of one artifact
    stay honest.

    A PDF is not tamper-proof and this does not claim to be. It is the format the
    two source guides are distributed in, and the one that reads as "here is my
    guide" rather than "here is a draft you may edit".
    """
    body = (document.content_md or "").strip()
    if not body:
        raise GuideError("This guide has no body to export yet.")

    from rubik_core.smart_teams.guide_pdf import render_guide_pdf

    email = _fold(document.subject_key)
    versions = document.profile_versions or {}
    title = document.title or TITLE_TEMPLATE.format(name=display_name)
    data = render_guide_pdf(
        parse_guide_blocks(body),
        title=title,
        display_name=display_name,
        provenance=PROVENANCE_TEMPLATE.format(
            name=display_name, version=versions.get(email, "?")
        ),
    )
    slug = _slug(display_name or email)
    return data, f"how-to-work-with-{slug}.pdf"


#: ``render_markdown`` writes a callout as ``> **{label}** — {body}``. This reads
#: it back apart.
_MD_CALLOUT = re.compile(r"^\*\*(?P<label>[^*]+)\*\*\s*(?:—|-)\s*(?P<body>.+)$")

#: ``render_markdown`` writes a guideline heading as ``### {n}. {heading}``.
_MD_NUMBERED_HEADING = re.compile(r"^(?P<n>\d{1,2})\.\s+(?P<title>.+)$")

#: ``render_markdown`` writes a contents entry as ``- **01**  {heading}``.
_MD_CONTENTS_ENTRY = re.compile(r"^\*\*(?P<n>\d{1,2})\*\*\s+(?P<title>.+)$")

#: The heading the contents grid follows. Matched on the rendered (caps) form
#: with the count and noun left open, because both vary with how many guidelines
#: the person's answers supported.
_MD_CONTENTS_HEADING = re.compile(r"^HERE ARE \d{1,2} THINGS? TO KEEP IN MIND$", re.I)


def _callout_from_markdown(text: str) -> Callout:
    """Turn one blockquote line back into a :class:`Callout`.

    The export renders from the STORED markdown, so it has to undo the one piece
    of inline markup ``render_markdown`` emits. Without this the literal
    asterisks landed in the tinted box — and they were invisible to a test that
    only walked ``doc.paragraphs``, because a callout lives in a table cell.
    A line that does not match keeps its whole text as the body rather than
    losing it: an unlabelled box still says something, a dropped one does not.
    """
    match = _MD_CALLOUT.match(text.strip())
    if match is None:
        return Callout(label="", body=_strip_inline_markdown(text.strip()))
    return Callout(
        label=match.group("label").strip(),
        body=_strip_inline_markdown(match.group("body").strip()),
    )


def _strip_inline_markdown(text: str) -> str:
    """Drop ``**``/``__`` emphasis markers. Word carries weight in runs, not
    asterisks, and ``deep_prep``'s ``_body`` does the same thing for the same
    reason."""
    return text.replace("**", "").replace("__", "")

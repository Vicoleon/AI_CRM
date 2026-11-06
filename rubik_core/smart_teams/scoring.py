"""ST-4 scoring: one interview in, six framework payloads out — refusals intact.

This module is the only place the instrument modules are actually *run*. It takes
a :class:`~rubik_core.smart_teams.contracts.ScoringInput` built from an
``st_interviews`` row and returns a :class:`ScoringReport` whose
:attr:`ScoringReport.output` is ready for the profile writer in
``rubik_core/smart_teams/profile.py``.

## The one behaviour this file exists to get right

A fabricated profile that reads as certain is worse than no profile. Every
instrument in ``instruments/`` already models its own refusal, and
``contracts.verdict_for`` already normalises the six vocabularies into one gate.
So the job here is narrow and mostly negative: **run the instruments, hand their
results to the envelope builder unaltered, and never substitute a value for a
refusal.** Concretely, this module

* does not read a score, a ranking, a type number or a letter code out of a
  result. It never touches ``top_two`` (positional on a level motivator
  profile), ``type_number`` (set on a declined Enneagram read), or
  ``Type16Result.letters`` (contains ``"?"`` for unresolved axes). Every result
  object goes straight into ``envelope_for``, which stores the instrument's own
  ``as_dict()`` verbatim under the layer key and computes the gate from the
  instrument's own fields;
* does not default, backfill, or "repair" anything. A Big Five domain under
  ``MIN_ANSWERED_PER_DOMAIN`` stays absent; a straight-lined DISC sitting stays
  retake-recommended; a contested Enneagram top two stays ``unresolved``;
* writes ``not_taken_envelope`` — never a NULL column and never a score over
  partial data — for a layer whose section the person has not marked complete,
  so "asked, unfinished" and "never asked" stay distinguishable;
* isolates each layer. One framework failing (a Vertex 500, an extraction the
  taxonomy rejects, an instrument raising on an answer set nobody anticipated)
  costs that layer and nothing else: the framework gets an explicitly empty
  envelope and the reason lands in :attr:`ScoringReport.failures`.

## Section completion is the gate on *whether* a layer is scored

``st_interviews.sections_completed`` is the only authority on what is done (see
``interview.py``'s module docstring). A section absent from that list contributes
**no answers at all** here, even if partial answers are sitting in the blob. Two
consequences worth stating because they look like bugs from the outside:

* the two DISC banks are two sections, so a person who finished the Likert bank
  and abandoned the forced-choice pairs is scored on the Likert bank alone —
  DISC tolerates that and reports the reduced completeness through its own
  quality flags;
* ``big_five`` and ``type16`` stand or fall together, because ``type16.derive``
  is arithmetic on a scored ``BigFiveResult`` and cannot be more trustworthy
  than the answers under it.

## The one LLM call

Strengths is the only layer with no deterministic scorer: the taxonomy is read
out of a conversation by :func:`extract_strengths`, which is the worker — the
``@attribute_to`` tag sits on it rather than on :func:`score_answers` because
the tag is a contextvar and does not survive a hand-off to a worker thread. The
model's output is never trusted: ``strengths.validate_extraction`` raises on an
off-taxonomy theme id and drops any theme whose quote is not present verbatim in
one of the interviewee's own turns, so a theme that reaches the payload is
evidenced by construction.

## Advisory only

Nothing this module produces may be wired to performance review, promotion,
compensation, or a judgement about anyone's fitness for a role. The payloads
describe how somebody prefers to work, in their own words, for the purpose of
working together better.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from rubik_core.cost.usage import attribute_to
from rubik_core.database.smart_teams import PROVENANCE_SOURCES
from rubik_core.smart_teams import interview as interview_catalog
from rubik_core.smart_teams.contracts import (
    FRAMEWORKS,
    REASON_NO_LAYER_DATA,
    REASON_NOT_TAKEN,
    LayerOutcome,
    LayerVerdict,
    PayloadError,
    ScoringInput,
    ScoringOutput,
    envelope_for,
    not_taken_envelope,
    read_verdict,
    transcript_from_json,
    verdict_for,
)
from rubik_core.smart_teams.instruments import (
    big_five,
    disc,
    enneagram,
    motivators,
    strengths,
    type16,
)

if TYPE_CHECKING:  # pragma: no cover — the runtime import is inside the function
    from rubik_core.smart_teams.profile import ProfileWrite

logger = logging.getLogger(__name__)

#: The AI Cost bucket for every Smart Teams LLM call. A named constant so the
#: decorator, the label registry and the test that checks they agree all read the
#: same string; the human label lives in ``backend/routers/admin.py``'s
#: ``_FEATURE_LABELS`` and was added in the same change as this decorator.
COST_FEATURE = "smart_teams"

#: Model key for the strengths extraction, resolved to a concrete Gemini id by
#: ``priorities.agents.base._resolve_model_id`` → ``settings.VERTEX_LLM_MODEL_FLASH``.
#: A key rather than an id on purpose: model ids are centralised in
#: ``rubik_core/config.py`` and must never be written out at a call site.
STRENGTHS_MODEL_KEY = "gemini-flash"

#: Extraction is a copy-and-classify job over a transcript, not a creative one.
#: The same 0.2 the chat and triage paths settled on after the hallucination RCA.
STRENGTHS_TEMPERATURE = 0.2

#: Total tries for the extraction call. Transient Vertex 429s and a truncated
#: JSON parse both clear on a second attempt often enough to be worth one retry;
#: past that the layer is reported as failed rather than retried into a timeout.
STRENGTHS_ATTEMPTS = 2

#: Operator-facing failure messages are truncated before they are logged or
#: returned. A Vertex error can carry a slice of the request, and the request
#: here is somebody's interview transcript.
_FAILURE_MESSAGE_CHARS = 300

# ── Failure vocabulary ────────────────────────────────────────────────────────
# Closed, like contracts.REASON_CODES and for the same reason: the completion
# dashboard and the retry path both branch on it, and free text would make them
# string-match on prose.
STAGE_SCORE = "score"
STAGE_DERIVE = "derive"
STAGE_LLM = "llm"
STAGE_VALIDATE = "validate"

FAILURE_STAGES: tuple[str, ...] = (
    STAGE_SCORE,
    STAGE_DERIVE,
    STAGE_LLM,
    STAGE_VALIDATE,
)


class ScoringError(Exception):
    """The scoring run could not start at all.

    Distinct from a :class:`LayerFailure`, which is one layer of six going
    missing while the profile is still written. This is raised when the *input*
    is unusable — an answers blob that fails ``interview.validate_answers``, a
    provenance value outside ``PROVENANCE_SOURCES`` — because scoring a blob we
    cannot parse would produce a profile that silently describes fewer answers
    than the person gave.
    """


@dataclass(frozen=True)
class LayerFailure:
    """Why one framework has no payload this run.

    Operator-facing and deliberately NOT persisted into the framework envelope:
    the envelope is rendered to the person whose profile it is, and "Vertex
    returned 503" is not a sentence anybody should read about themselves. The
    person sees an absent layer with reason ``no_layer_data``; the engineer sees
    this, in the log and on the report the job returns.
    """

    framework: str
    stage: str
    message: str

    def __post_init__(self) -> None:
        if self.framework not in FRAMEWORKS:
            raise PayloadError(f"unknown framework {self.framework!r}")
        if self.stage not in FAILURE_STAGES:
            raise PayloadError(
                f"unknown failure stage {self.stage!r}; add it to FAILURE_STAGES "
                "in the same change"
            )


@dataclass(frozen=True)
class ScoringReport:
    """One scoring run: the payloads, plus what did not work.

    ``output`` is complete by construction — ``ScoringOutput`` raises unless
    every framework has an envelope — so a failed layer is present-and-empty
    rather than missing. That is the whole point of splitting the failure detail
    out into ``failures``: a reader cannot mistake "this layer failed" for "this
    key was never written", and a writer cannot lose five good layers to one bad
    one.
    """

    output: ScoringOutput
    failures: Mapping[str, LayerFailure]
    scored_at: datetime

    @property
    def failed_frameworks(self) -> tuple[str, ...]:
        """Frameworks whose scoring raised, in canonical order."""
        return tuple(name for name in FRAMEWORKS if name in self.failures)

    @property
    def reportable_frameworks(self) -> tuple[str, ...]:
        """Frameworks a guide may assert from. Delegated, never recomputed."""
        return self.output.reportable_frameworks

    @property
    def is_empty(self) -> bool:
        """True when not one layer survived — worth an alert, not a guide."""
        return not self.output.reportable_frameworks


# ── The LLM worker ────────────────────────────────────────────────────────────


def _json_client(model_key: str, temperature: float):
    """The house JSON-over-Gemini client, imported lazily.

    A function rather than an inline construction so tests can substitute a stub
    without patching the SDK, and lazy so importing this module does not pull in
    the priorities package (which builds a genai client on first use).
    """
    from rubik_core.priorities.agents.base import LLMJsonClient

    return LLMJsonClient(model_key=model_key, temperature=temperature)


@attribute_to(COST_FEATURE)
def extract_strengths(
    transcript: strengths.InterviewTranscript,
    *,
    thin: bool | None = None,
    model_key: str = STRENGTHS_MODEL_KEY,
    temperature: float = STRENGTHS_TEMPERATURE,
    attempts: int = STRENGTHS_ATTEMPTS,
) -> strengths.StrengthsRead:
    """Read work-strength themes out of the conversational segment. One LLM call.

    The rubric, the taxonomy block, the injection fence around the transcript
    and the confidence ceiling for a thin transcript all belong to
    ``instruments/strengths.py``; this function is the Vertex round trip between
    ``extraction_prompt`` and ``validate_extraction`` and nothing more. That
    split is what keeps the advisory-language guard in
    ``strengths._check_taxonomy`` (which refuses promotion/potential phrasing at
    import) covering everything the model is shown.

    ``@attribute_to`` sits here, on the function that makes the call, rather than
    on :func:`score_answers`: the AI Cost tag is a contextvar, and a scoring
    run dispatched onto a worker thread would lose a tag set at the entry point.

    Raises whatever the client and the validator raise — a Vertex failure after
    the last attempt, or ``ValueError`` for an off-contract payload (an unknown
    theme id, a duplicate theme, an unparseable confidence).
    :func:`score_answers` catches both and records the layer as failed, so the
    exception never reaches the person's profile.
    """
    is_thin = strengths.transcript_is_thin(transcript) if thin is None else thin
    prompt = strengths.extraction_prompt(transcript, thin=is_thin)
    client = _json_client(model_key, temperature)
    # fallback=None so the client re-raises instead of handing back a shape that
    # would validate as "no themes". A model outage and an interview with no
    # discernible strengths must not look the same in somebody's profile.
    payload = client.invoke_json(prompt, fallback=None, attempts=attempts)
    return strengths.validate_extraction(payload, transcript, thin=is_thin)


#: The extractor signature :func:`score_answers` accepts, so a caller can pass
#: an already-scored read (an official upload, ST-5) or a stub (every test).
StrengthsExtractor = Callable[..., strengths.StrengthsRead]


# ── The run ───────────────────────────────────────────────────────────────────


class _Run:
    """Bookkeeping for one scoring pass. Not part of the public surface.

    A small class rather than six functions threading three dicts between them:
    the ordering constraint (``big_five`` before ``type16``) and the
    "every framework gets exactly one envelope" invariant are much easier to
    read when the accumulation lives in one place.
    """

    def __init__(self, scoring_input: ScoringInput, scored_at: datetime) -> None:
        self.input = scoring_input
        self.scored_at = scored_at
        self.outcomes: dict[str, LayerOutcome] = {}
        self.failures: dict[str, LayerFailure] = {}
        # Filled by load_answers(), which every path calls before scoring. Empty
        # here rather than absent so a step that runs out of order reads as "no
        # sections complete" instead of raising AttributeError.
        self.completed: set[str] = set()
        self.partitioned: dict[str, dict[str, Any]] = {}

    # ── recording ────────────────────────────────────────────────────────────
    def record(self, framework: str, result: object) -> None:
        """Store a scored layer. The gate is computed, never passed in."""
        envelope = envelope_for(framework, result, scored_at=self.scored_at)
        self.outcomes[framework] = LayerOutcome(
            framework=framework,
            envelope=envelope,
            verdict=verdict_for(framework, result),
            result=result,
        )

    def record_absent(
        self, framework: str, *, reason: str, hedge: str | None = None
    ) -> None:
        """Store an explicitly empty layer — the person did not supply it."""
        self.outcomes[framework] = LayerOutcome(
            framework=framework,
            envelope=not_taken_envelope(
                framework, reason=reason, hedge=hedge, scored_at=self.scored_at
            ),
            verdict=_absent_verdict(framework, reason, hedge),
            result=None,
        )

    def record_failure(self, framework: str, stage: str, exc: BaseException) -> None:
        """Store an empty layer and remember why, without touching the others."""
        message = str(exc)[:_FAILURE_MESSAGE_CHARS] or type(exc).__name__
        self.failures[framework] = LayerFailure(
            framework=framework, stage=stage, message=message
        )
        # No hedge: there is no authored instrument sentence for "the scorer
        # raised", and inventing one here would put engineering prose in front
        # of the person whose profile it is.
        self.record_absent(framework, reason=REASON_NO_LAYER_DATA)
        logger.warning(
            "[smart-teams] %s scoring failed at %s for %s: %s",
            framework,
            stage,
            self.input.user_email,
            message,
        )

    # ── answers ──────────────────────────────────────────────────────────────
    def load_answers(self) -> None:
        """Partition and validate the blob once, up front.

        ``partition_answers`` validates as it goes and routes by exact item-id
        set membership, which is what keeps ``disc_fc_*`` out of the DISC Likert
        validator. Its failure is a whole-input failure, not a layer failure:
        an unroutable key means the item banks moved under a stored interview,
        and scoring the remainder would quietly produce a profile from fewer
        answers than the person gave.
        """
        try:
            self.completed = set(
                interview_catalog.normalize_completed(self.input.sections_completed)
            )
            self.partitioned = interview_catalog.partition_answers(self.input.answers)
        except interview_catalog.AnswerError as exc:
            raise ScoringError(f"interview answers are not scoreable: {exc}") from exc

    def answers_for(self, section_key: str) -> dict[str, Any]:
        """This section's answers, or nothing if it is not marked complete.

        The completion check is here, once, rather than at five call sites.
        ``sections_completed`` is the only authority on progress — a half-filled
        grid the person is still editing must not become part of a scored
        profile just because the values reached the database.
        """
        if section_key not in self.completed:
            return {}
        return dict(self.partitioned.get(section_key) or {})

    def completed_any(self, *section_keys: str) -> bool:
        return any(key in self.completed for key in section_keys)


def _absent_verdict(framework: str, reason: str, hedge: str | None) -> LayerVerdict:
    """The verdict that matches ``not_taken_envelope``'s own gate.

    Read back out of the envelope through ``contracts.read_verdict`` rather than
    constructed here, so the stored payload and the in-memory verdict cannot
    disagree — they are literally the same computation.
    """
    return read_verdict(
        not_taken_envelope(framework, reason=reason, hedge=hedge, scored_at=None)
    )


def _strip_likert(answers: Mapping[str, Any]) -> dict[str, int]:
    """Drop the ``None``s. Every instrument tolerates them; dropping is clearer."""
    return {key: value for key, value in answers.items() if value is not None}


# ── Per-layer steps ───────────────────────────────────────────────────────────
# One function per framework group, each responsible for exactly one entry in
# ``run.outcomes``. Called in an order that satisfies contracts.SCORING_ORDER.


def _score_disc(run: _Run) -> None:
    """DISC from its two banks. Either bank alone is enough to attempt a score."""
    likert = _strip_likert(run.answers_for(interview_catalog.SECTION_DISC_LIKERT))
    choices = {
        key: value
        for key, value in run.answers_for(
            interview_catalog.SECTION_DISC_FORCED_CHOICE
        ).items()
        if value is not None
    }
    if not run.completed_any(
        interview_catalog.SECTION_DISC_LIKERT,
        interview_catalog.SECTION_DISC_FORCED_CHOICE,
    ):
        run.record_absent("disc", reason=REASON_NOT_TAKEN)
        return
    try:
        result = disc.score_disc(
            likert, choices, elapsed_seconds=run.input.elapsed_seconds
        )
    except Exception as exc:  # noqa: BLE001 — one layer must not cost the rest
        run.record_failure("disc", STAGE_SCORE, exc)
        return
    run.record("disc", result)


def _score_big_five_and_type16(run: _Run) -> None:
    """Big Five, then the derived letters. They stand or fall together.

    ``type16`` has no items of its own — it restates the same answers — so a
    Big Five bank that was never completed, or that raised, leaves both layers
    absent. Deriving letters from a ``BigFiveResult`` we could not compute is
    not possible, and deriving them from a *default* one would be the exact
    fabrication this module exists to prevent.
    """
    responses = _strip_likert(run.answers_for(interview_catalog.SECTION_BIG_FIVE))
    if interview_catalog.SECTION_BIG_FIVE not in run.completed:
        reason = REASON_NOT_TAKEN
        run.record_absent("big_five", reason=reason)
        run.record_absent("type16", reason=reason)
        return
    try:
        bf_result = big_five.score(responses)
    except Exception as exc:  # noqa: BLE001
        run.record_failure("big_five", STAGE_SCORE, exc)
        run.record_failure("type16", STAGE_DERIVE, exc)
        return
    run.record("big_five", bf_result)
    try:
        derived = type16.derive(bf_result)
    except Exception as exc:  # noqa: BLE001
        run.record_failure("type16", STAGE_DERIVE, exc)
        return
    run.record("type16", derived)


def _score_strengths(run: _Run, extractor: StrengthsExtractor) -> None:
    """The one LLM layer. Everything the model returns is validated or dropped."""
    section = interview_catalog.SECTION_BY_KEY[interview_catalog.SECTION_STRENGTHS]
    stored = run.answers_for(interview_catalog.SECTION_STRENGTHS).get(
        section.transcript_key
    )
    if interview_catalog.SECTION_STRENGTHS not in run.completed or not stored:
        # The instrument authors its own "not yet" sentence; use it rather than
        # writing a second one here.
        run.record_absent(
            "strengths",
            reason=REASON_NOT_TAKEN,
            hedge=strengths.StrengthsRead.not_taken().note,
        )
        return
    try:
        transcript = transcript_from_json(stored)
    except PayloadError as exc:
        run.record_failure("strengths", STAGE_VALIDATE, exc)
        return
    try:
        read = extractor(transcript)
    except ValueError as exc:
        # validate_extraction's contract failures — an off-taxonomy theme id, a
        # duplicate, an unparseable confidence — and also a response the JSON
        # parser could not read at all, which arrives as a ValueError from
        # ``safe_json``. Both mean the model's output was unusable rather than
        # unavailable, which is why they share a stage. The layer is dropped,
        # loudly, not patched up.
        run.record_failure("strengths", STAGE_VALIDATE, exc)
        return
    except Exception as exc:  # noqa: BLE001 — Vertex outage, timeout, quota
        run.record_failure("strengths", STAGE_LLM, exc)
        return
    if not isinstance(read, strengths.StrengthsRead):
        run.record_failure(
            "strengths",
            STAGE_VALIDATE,
            TypeError(
                f"strengths extractor returned {type(read).__name__}, not a "
                "StrengthsRead"
            ),
        )
        return
    run.record("strengths", read)


def _score_motivators(run: _Run) -> None:
    """The six Spranger axes.

    The probe transcript is deliberately NOT consumed here — see the module-level
    note in :func:`score_answers`. ``motivators.py`` exposes ``probe_for`` /
    ``probe_form`` to ASK the follow-up questions and no entry point that takes
    the answers back, so a probe cannot currently resolve a level or unordered
    profile. Those results stay non-reportable, which is the safe direction: the
    alternative would be this module deciding a ranking the instrument refused
    to decide.
    """
    responses = _strip_likert(run.answers_for(interview_catalog.SECTION_MOTIVATORS))
    if interview_catalog.SECTION_MOTIVATORS not in run.completed:
        run.record_absent("motivators", reason=REASON_NOT_TAKEN)
        return
    try:
        result = motivators.score_motivators(responses)
    except Exception as exc:  # noqa: BLE001
        run.record_failure("motivators", STAGE_SCORE, exc)
        return
    run.record("motivators", result)


def _score_enneagram(run: _Run) -> None:
    """Items, then whatever the person said in the confirmation step.

    Precedence is the instrument's, not ours: a decline drops the read entirely
    (``with_decline`` discards the ranking so no future feature can use a
    rejected inference), and a self-identification replaces the inferred type
    while keeping the ranking for a support admin debugging "why did Cubie
    guess a 3". Both transitions are ``EnneagramRead`` methods; this function
    only decides which one the stored answer means.
    """
    section = interview_catalog.SECTION_BY_KEY[
        interview_catalog.SECTION_ENNEAGRAM_CONFIRMATION
    ]
    responses = _strip_likert(run.answers_for(interview_catalog.SECTION_ENNEAGRAM))
    meta = run.answers_for(interview_catalog.SECTION_ENNEAGRAM_CONFIRMATION).get(
        section.meta_key
    )
    scored_items = interview_catalog.SECTION_ENNEAGRAM in run.completed

    if not scored_items and not meta:
        run.record_absent(
            "enneagram",
            reason=REASON_NOT_TAKEN,
            hedge=enneagram.EnneagramRead.not_taken().note,
        )
        return

    try:
        read = (
            enneagram.score_enneagram(responses)
            if scored_items
            else enneagram.EnneagramRead.not_taken()
        )
        if meta:
            read = _apply_enneagram_confirmation(read, meta, scored_items=scored_items)
    except Exception as exc:  # noqa: BLE001
        run.record_failure("enneagram", STAGE_SCORE, exc)
        return
    run.record("enneagram", read)


def _apply_enneagram_confirmation(
    read: enneagram.EnneagramRead,
    meta: Mapping[str, Any],
    *,
    scored_items: bool,
) -> enneagram.EnneagramRead:
    """Fold ``meta:enneagram_confirmation`` into the read.

    The shape is already validated by ``interview._validate_structured`` (a
    declined layer cannot carry a type, a wing must be adjacent to its type), so
    this only has to pick the constructor. An object with neither a decline nor a
    type number is the "confirm what Cubie guessed" answer and changes nothing —
    the inferred read already says that.
    """
    if meta.get("declined"):
        return read.with_decline()
    type_number = meta.get("type_number")
    if type_number is None:
        return read
    wing = meta.get("wing")
    if scored_items:
        return read.with_self_identification(type_number, wing)
    return enneagram.EnneagramRead.self_identified(type_number, wing)


# ── The entry point ───────────────────────────────────────────────────────────


def score_answers(
    scoring_input: ScoringInput,
    *,
    extractor: StrengthsExtractor | None = None,
    scored_at: datetime | None = None,
) -> ScoringReport:
    """Score one interview into six framework payloads.

    ``extractor`` replaces :func:`extract_strengths` — pass a stub in tests (no
    test in this repo may reach Vertex), or pass a function that returns an
    already-validated ``StrengthsRead`` when the layer came from an official
    upload rather than a conversation (ST-5).

    Raises :class:`ScoringError` when the *input* cannot be used at all: an
    answers blob that fails validation, or a provenance value outside
    ``PROVENANCE_SOURCES``. Everything narrower than that — an instrument
    raising, the model failing, an extraction the taxonomy rejects — costs one
    layer and is recorded in :attr:`ScoringReport.failures`.

    What it deliberately does not do:

    * **No skills.** Declared skills are ``st_person_skills`` rows, not a
      profile payload; the ``skills`` section's answers are validated by
      ``partition_answers`` and then left for ST-8.
    * **No probe resolution.** A motivators probe transcript is stored and
      unused — see :func:`_score_motivators`.
    * **No writes.** Persisting the result is ``profile.py``'s job, and keeping
      this function free of a Session is what lets it be tested with a literal.
    """
    if scoring_input.provenance not in PROVENANCE_SOURCES:
        raise ScoringError(
            f"provenance {scoring_input.provenance!r} is not one of "
            f"{PROVENANCE_SOURCES}; it is written to st_profiles.provenance and "
            "read by the guide generator to say where a layer came from"
        )
    run = _Run(scoring_input, scored_at or datetime.utcnow())
    run.load_answers()

    _score_disc(run)
    _score_big_five_and_type16(run)
    _score_strengths(run, extractor or extract_strengths)
    _score_motivators(run)
    _score_enneagram(run)

    # ScoringOutput refuses a missing framework, which is the guard that a new
    # section (or a new early `return` in one of the steps above) cannot silently
    # drop a layer: an omitted layer is indistinguishable from one that failed.
    output = ScoringOutput(
        user_email=scoring_input.user_email,
        outcomes=run.outcomes,
        provenance={name: scoring_input.provenance for name in FRAMEWORKS},
    )
    logger.info(
        "[smart-teams] scored %s: reportable=%s failed=%s",
        scoring_input.user_email,
        list(output.reportable_frameworks),
        list(run.failures),
    )
    return ScoringReport(
        output=output, failures=dict(run.failures), scored_at=run.scored_at
    )


def score_interview(interview_id: str) -> ProfileWrite:
    """Score one interview by id and store it. The dispatch entry point.

    This exists because ``backend/routers/smart_teams.py`` resolves the
    completion hand-off by name — ``scoring.score_interview(interview_id)`` — at
    call time, so that the interview stays answerable while this engine is
    written. Honouring that contract here rather than asking ST-3 to reach into
    ``profile.py`` keeps the router's fallback working in local development,
    where it is the only path that scores anything.

    All it does is open a session and delegate: the scoring is
    :func:`score_answers`, the append-only write and the idempotency guarantee
    are ``profile.score_and_store_interview``, and neither of those wants a
    session it did not open. Imported inside the function because ``profile``
    imports this module at module scope.

    Returns the ``ProfileWrite`` so the cubie-worker job can log whether it
    created a version or found one already there. Raises ``ProfileError`` for an
    unknown or unfinished interview.
    """
    from rubik_core.database.db import SessionLocal
    from rubik_core.smart_teams.profile import score_and_store_interview

    db = SessionLocal()
    try:
        return score_and_store_interview(db, interview_id)
    finally:
        db.close()

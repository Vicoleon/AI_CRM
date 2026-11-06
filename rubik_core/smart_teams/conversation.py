"""The conversational segments of the Work Style Interview — the part that talks.

Three sections in ``interview.SECTIONS`` are conversational, and they are three
different problems wearing the same word:

===========================  =================================================
section                      what actually has to happen
===========================  =================================================
``strengths``                Eight authored story prompts, each with authored
                            follow-ups. A model decides, per answer, whether a
                            follow-up is warranted and which one.
``motivators_probe``         Four questions whose *wording* was already chosen
                            by ``motivators.probe_form`` from what the scores
                            can honestly claim. Same turn machinery, follow-ups
                            drawn from :data:`prompts.NEUTRAL_NUDGES` because a
                            topic-specific follow-up would undo that hedging.
``enneagram_confirmation``   One step, already assembled by
                            ``enneagram.confirmation_step``. **No model call at
                            all** — see :func:`enneagram_presentation`.
===========================  =================================================

Why this is not built on the ADK
--------------------------------
The repo's ADK stack (``agent_runtime.coordinator`` + ``orchestration``) exists
to serve chat: a tool-calling agent whose control flow the model decides, with
its own Cloud SQL session store and a HITL pause. An interview is the opposite
shape. Its questions are already in the repo as frozen data, its state has to
live in ``st_interviews`` regardless (``section_cursor``, ``sections_completed``,
``answers`` — the schema and its CHECK constraints exist), and its output is a
validated JSON contract. Building it on the ADK would add a second session store
that has to agree with the first, and — decisively — the ADK's internal client
does not pass through ``models.get_genai_client``, so ``@attribute_to`` cannot
see its tokens at all. Chat compensates for that by hand-recording usage in the
router. A new surface should not inherit that workaround.

So: one direct structured Gemini call per person-answer, exactly the pattern
``deep_prep/council.py`` and ``xray/agents/base.py`` already use, with the state
machine here in Python where it can be tested without a network.

What the model is allowed to be
-------------------------------
A **selector and a judge**, never an author. It returns an action
(``probe``/``advance``), an index into a list of follow-ups this module handed it,
and at most one sentence of acknowledgement that goes through
``prompts.screen_reflection`` before anybody sees it. Cubie therefore cannot ask
a question that is not in ``instruments/``, which is what makes the prohibition
list in ``prompts.py`` enforceable rather than aspirational.

Termination
-----------
Bounded three ways, and the bound does not depend on the model:

1. At most :data:`MAX_PROBES_PER_QUESTION` follow-ups per scripted question. The
   count is state, not a model output, and when it is exhausted **no model call
   is made** — the segment advances.
2. A follow-up is never repeated inside one question.
3. Any planner failure — transport, timeout, unparseable JSON, an out-of-range
   index — resolves to ``advance``.

So a segment with a dead model still finishes: it degrades to the authored script
read straight through, which is a real interview, just a plainer one. See
:func:`segment_bounds` for the arithmetic and :class:`SegmentHealth` for what a
reader can tell about a degraded run afterwards.

Failure is recorded, never fabricated
-------------------------------------
A failed planning call never invents an answer, never skips a question, and never
marks a question answered. It appends a :class:`TurnFailure` to
:attr:`SegmentState.failures` and logs at WARNING. Two honest caveats about how
far that travels:

* ``SegmentState.failures`` is returned to the caller and is not persisted.
  ``st_interviews`` has no column for it, and the only key namespace that could
  hold it (``meta:<section_key>``) is declared per-section in ``interview.py`` —
  which today declares a shape for ``enneagram_confirmation`` and refuses one for
  anything else, on purpose. Persisting the failure codes needs that one
  declaration; :func:`failure_record` produces the value for it.
* What *is* visible from the persisted transcript alone is
  :class:`SegmentHealth`: which questions were asked, which got no answer, and
  how many follow-ups were offered. A run where the planner never worked shows up
  there as ``probes_offered == 0``, which is consistent with a dead planner and
  is not proof of one — a person who answers every question fully earns no
  follow-ups either. It is reported as the number it is.

Transcript attribution
----------------------
Load-bearing, and the reason nothing here stores a flat string. Turns are
``strengths.Turn`` (re-exported through ``contracts``), and Cubie's questions are
``Speaker.ASSISTANT`` while the person's answers are ``Speaker.INTERVIEWEE``.
``strengths.validate_extraction`` verifies every evidence quote against a single
interviewee turn, so a transcript that lost the distinction would let a
company-visible guide print Cubie's own question back at the person under "In
your words". Acknowledgements are folded into the Cubie turn that carries the
next question or follow-up — they are Cubie's words, they are never quotable, and
folding them keeps one Cubie turn per question-or-probe so the bound in
:func:`segment_bounds` stays arithmetic.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from google.genai import types

from rubik_core.config import settings
from rubik_core.cost.usage import attribute_to
from rubik_core.models import get_genai_client, safe_json
from rubik_core.smart_teams import interview, prompts
from rubik_core.smart_teams.contracts import (
    InterviewTranscript,
    Speaker,
    Turn,
    transcript_from_json,
    transcript_to_json,
)
from rubik_core.smart_teams.instruments import enneagram, motivators, strengths

logger = logging.getLogger(__name__)

#: The AI-Cost bucket. One key for the whole feature, matching the
#: ``_FEATURE_LABELS`` entry in ``backend/routers/admin.py`` — the label and the
#: decorator ship together or the spend lands in "Unattributed".
COST_FEATURE = "smart_teams"

#: The tier for a turn decision. Read from ``config.py`` rather than written out:
#: pinning a model id in a feature module is how a family drifts without any
#: health check noticing. FLASH because this call is on the request path with a
#: person waiting for the next question — the same tier the live chat coordinator
#: and ``xray.MODEL_CHANNEL`` run on.
TURN_MODEL: str = settings.VERTEX_LLM_MODEL_FLASH

#: House convention (chat, Deep Prep, X-Ray all sit at 0.2). The output is a
#: decision plus one sentence; there is nothing here that wants sampling
#: variance. No Google-Search grounding either, and that is deliberate: the only
#: source of truth in this segment is what the person just typed, and a grounded
#: call would invite the model to bring the internet to a conversation about
#: somebody's own work.
TURN_TEMPERATURE = 0.2

#: Follow-ups per scripted question. Two, because the authored probes are written
#: to move an answer from a claim to an incident and a third rarely adds one —
#: and because the whole segment's length is this number times the script.
MAX_PROBES_PER_QUESTION = 2


# ── Failures ──────────────────────────────────────────────────────────────────
#: Stable codes for what went wrong in a planning call. Stable because they are
#: logged, counted, and (once ``interview.py`` declares the meta shape) stored.
FAILURE_TRANSPORT = "planner_transport_error"
FAILURE_UNPARSEABLE = "planner_unparseable_response"
FAILURE_BAD_ACTION = "planner_bad_action"
FAILURE_BAD_PROBE_INDEX = "planner_bad_probe_index"
FAILURE_PROBE_REPEATED = "planner_repeated_probe"
FAILURE_REFLECTION_REJECTED = "reflection_rejected"

FAILURE_CODES: tuple[str, ...] = (
    FAILURE_TRANSPORT,
    FAILURE_UNPARSEABLE,
    FAILURE_BAD_ACTION,
    FAILURE_BAD_PROBE_INDEX,
    FAILURE_PROBE_REPEATED,
    FAILURE_REFLECTION_REJECTED,
)


class ConversationError(RuntimeError):
    """The caller drove the segment state machine into a state it does not have."""


@dataclass(frozen=True)
class TurnFailure:
    """One thing that did not work, on one turn.

    Not an exception: the person is sitting in front of the interview and the
    interview has a correct thing to do without the model (ask the next authored
    question). This is the record that it happened.
    """

    code: str
    question_id: str
    detail: str = ""

    def __post_init__(self) -> None:
        if self.code not in FAILURE_CODES:
            raise ValueError(
                f"{self.code!r} is not a known conversation failure code; add it "
                f"to FAILURE_CODES in the same change ({FAILURE_CODES})"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "question_id": self.question_id,
            "detail": self.detail,
        }


# ── The script ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ScriptedQuestion:
    """One question Cubie may ask, and the follow-ups it may ask after it.

    Both come from ``instruments/``. This type exists so the two conversational
    segments — whose instrument modules expose different shapes for good reasons
    — can share one state machine without either of them learning about the
    other.
    """

    question_id: str
    text: str
    probes: tuple[str, ...]
    #: The instrument author's note on what the question is for. Internal only;
    #: it reaches the planner prompt and never a person.
    purpose: str


@dataclass(frozen=True)
class SegmentScript:
    """The questions for one conversational segment, in order.

    ``section_key`` is an ``interview.SECTIONS`` key, checked here: a segment
    whose answers cannot be stored under a transcript key is a segment whose work
    is thrown away at submit time.
    """

    section_key: str
    questions: tuple[ScriptedQuestion, ...]

    def __post_init__(self) -> None:
        section = interview.SECTION_BY_KEY.get(self.section_key)
        if section is None:
            raise ConversationError(
                f"{self.section_key!r} is not an interview section; the catalog "
                f"has {interview.SECTION_KEYS}"
            )
        if section.kind != "conversational":
            raise ConversationError(
                f"{self.section_key!r} is a {section.kind} section, not a "
                "conversational one"
            )
        if not self.questions:
            raise ConversationError(f"{self.section_key!r}: a segment needs questions")
        ids = [question.question_id for question in self.questions]
        if len(set(ids)) != len(ids):
            raise ConversationError(
                f"{self.section_key!r}: duplicate question ids {ids!r}; a turn's "
                "prompt_id has to name exactly one question"
            )

    @property
    def section(self) -> interview.Section:
        return interview.SECTION_BY_KEY[self.section_key]

    @property
    def answer_key(self) -> str:
        """The ``st_interviews.answers`` key this segment's transcript is stored under."""
        return self.section.transcript_key

    def question_at(self, index: int) -> ScriptedQuestion:
        return self.questions[index]


def strengths_script() -> SegmentScript:
    """The strengths story segment, straight off ``strengths.ELICITATION_SCRIPT``.

    Imported, not re-typed. The prompts, their authored probes and their
    purposes are reviewed content — a copy here would be a second copy to keep
    in step with the taxonomy, and the version that drifted would be the one
    people actually answered.
    """
    return SegmentScript(
        section_key=interview.SECTION_STRENGTHS,
        questions=tuple(
            ScriptedQuestion(
                question_id=prompt.prompt_id,
                text=prompt.question,
                probes=tuple(prompt.probes),
                purpose=prompt.purpose,
            )
            for prompt in strengths.ELICITATION_SCRIPT
        ),
    )


def motivators_probe_script(result: motivators.MotivatorResult) -> SegmentScript:
    """The motivators confirmation probe, worded for what this result can claim.

    Everything load-bearing here happens inside ``motivators.probe_form``, which
    branches on :attr:`MotivatorResult.probe_reason` and raises on a reason code
    with no wording of its own. This function must not second-guess it: on a flat
    or incomplete result the "ranking" is declaration order, and the earlier
    version of that module told every such person their top two values as fact.
    So the questions are taken rendered, in the order given, and the only thing
    added is the neutral follow-up set — ``ProbeQuestion`` ships none, because a
    topic-specific follow-up would smuggle back the claim the wording just
    removed.
    """
    questions = motivators.probe_form(result)
    return SegmentScript(
        section_key=interview.SECTION_MOTIVATORS_PROBE,
        questions=tuple(
            ScriptedQuestion(
                question_id=question.question_id,
                text=question.render(result),
                probes=prompts.NEUTRAL_NUDGES,
                purpose=question.purpose,
            )
            for question in questions
        ),
    )


#: The closing line for a finished segment, by section key. Rendered by the UI,
#: never written into the transcript — see the module docstring.
CLOSING_LINES: Mapping[str, str] = {
    interview.SECTION_STRENGTHS: prompts.STRENGTHS_CLOSING,
    interview.SECTION_MOTIVATORS_PROBE: prompts.MOTIVATORS_PROBE_CLOSING,
}


# ── Bounds ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SegmentBounds:
    """The worst case for one segment, computed from the script.

    Exists so "a segment has to terminate" is a number in a test rather than a
    claim in a comment.
    """

    questions: int
    max_probes_per_question: int

    @property
    def max_assistant_turns(self) -> int:
        """One turn per question, plus its follow-ups. Acknowledgements fold in."""
        return self.questions * (1 + self.max_probes_per_question)

    @property
    def max_answers(self) -> int:
        """At most one answer per Cubie turn."""
        return self.max_assistant_turns

    @property
    def max_planner_calls(self) -> int:
        """No call is made once a question's follow-ups are spent, so this is
        strictly below :attr:`max_answers`: the last answer on a question never
        gets a planning call."""
        return self.questions * self.max_probes_per_question


def segment_bounds(script: SegmentScript) -> SegmentBounds:
    """The termination arithmetic for ``script``."""
    return SegmentBounds(
        questions=len(script.questions),
        max_probes_per_question=MAX_PROBES_PER_QUESTION,
    )


# ── The plan for one turn ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class TurnPlan:
    """What to do after one answer.

    Exactly one of "ask this follow-up" and "move on" — ``probe`` is set if and
    only if ``advance`` is False, checked here rather than trusted, because a
    plan that says both is a plan that silently drops a question.
    """

    advance: bool
    probe: str | None = None
    #: Index of ``probe`` in the question's own probe tuple. Kept so the state
    #: machine can refuse to repeat one.
    probe_index: int | None = None
    acknowledgement: str | None = None
    failure: TurnFailure | None = None

    def __post_init__(self) -> None:
        if self.advance and self.probe is not None:
            raise ConversationError("a TurnPlan cannot both advance and probe")
        if not self.advance and self.probe is None:
            raise ConversationError("a probing TurnPlan needs a probe")


# ── The worker ────────────────────────────────────────────────────────────────
# @attribute_to sits HERE, on the function that makes the model call itself, not
# on submit_answer. The feature is a contextvar: a tag set at an entry point does
# not survive a hand-off to another thread, and the interview router is exactly
# the kind of caller that may end up behind run_in_threadpool. Decorating the
# worker re-establishes the bucket in whichever thread runs it. (The 75%
# "Unattributed" row on the AI Cost tab was this mistake, made once already.)


@attribute_to(COST_FEATURE)
def _plan_turn_raw(
    *,
    segment_title: str,
    question: ScriptedQuestion,
    probes_remaining: int,
    conversation_block: str,
    latest_answer: str,
    model_id: str | None = None,
) -> Any:
    """One planning call. Returns the parsed payload, or raises.

    Deliberately thin and deliberately raising: every judgement about what the
    payload means, and every fallback, lives in :func:`plan_turn` where it can be
    tested without a client. ``response_mime_type`` asks for JSON and
    ``safe_json`` still tolerates a fence — belt and braces, because the
    alternative is a fenced response being reported as "the model returned
    garbage".
    """
    prompt = prompts.turn_prompt(
        segment_title=segment_title,
        question_text=question.text,
        question_purpose=question.purpose,
        probes=question.probes,
        probes_remaining=probes_remaining,
        conversation_block=conversation_block,
        latest_answer=latest_answer,
    )
    from rubik_core.inference_workloads import run_text

    @attribute_to(COST_FEATURE)
    def managed(route):
        response = get_genai_client().models.generate_content(
            model=route.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=prompts.INTERVIEWER_SYSTEM,
                temperature=TURN_TEMPERATURE,
                response_mime_type="application/json",
            ),
        )
        return response.text or ""

    raw = run_text(
        "smart_teams_conversation",
        prompt,
        default_model=model_id or TURN_MODEL,
        managed=managed,
        json_mode=True,
        system_instruction=prompts.INTERVIEWER_SYSTEM,
        temperature=TURN_TEMPERATURE,
    )
    try:
        return safe_json(raw)
    except ValueError:
        raise ValueError("AI turn planner returned invalid JSON") from None


def plan_turn(
    script: SegmentScript,
    question: ScriptedQuestion,
    transcript: InterviewTranscript,
    *,
    latest_answer: str,
    probes_used: tuple[int, ...],
    model_id: str | None = None,
) -> TurnPlan:
    """Decide what happens after one answer, and never raise.

    Every path out of here is a usable plan. A planner that cannot be reached,
    cannot be parsed, or answers outside its contract yields ``advance`` with a
    :class:`TurnFailure` attached, because the alternative — stalling, retrying
    in front of the person, or inventing a follow-up — is worse than a plainer
    interview.

    ``probes_used`` is the indexes already spent on this question. It is both the
    bound and the no-repeats rule, and it is checked here rather than described
    to the model.
    """
    remaining = MAX_PROBES_PER_QUESTION - len(probes_used)
    if remaining <= 0 or not question.probes:
        # No call at all. The bound holds without the model's cooperation, and
        # this is also the cheap path: it removes one Gemini call per question.
        return TurnPlan(advance=True)

    try:
        payload = _plan_turn_raw(
            segment_title=script.section.title,
            question=question,
            probes_remaining=remaining,
            conversation_block=strengths.format_transcript(transcript),
            latest_answer=latest_answer,
            model_id=model_id,
        )
    except ValueError as exc:  # safe_json — the model said something unparseable
        return _failed_plan(FAILURE_UNPARSEABLE, question, exc)
    except Exception as exc:  # noqa: BLE001 — transport, timeout, quota, auth
        return _failed_plan(FAILURE_TRANSPORT, question, exc)

    if not isinstance(payload, Mapping):
        return _failed_plan(
            FAILURE_UNPARSEABLE, question, f"expected an object, got {type(payload)}"
        )

    acknowledgement, rejection = prompts.screen_reflection(
        payload.get("acknowledgement")
    )
    reflection_failure: TurnFailure | None = None
    if rejection is not None and rejection != prompts.REJECT_EMPTY:
        # Not an error the person should feel — the interview simply says nothing
        # this turn. Recorded because a planner whose acknowledgements are always
        # screened out is a planner drifting off its brief.
        logger.warning(
            "[smart_teams] acknowledgement dropped on %s (%s)",
            question.question_id,
            rejection,
        )
        reflection_failure = TurnFailure(
            code=FAILURE_REFLECTION_REJECTED,
            question_id=question.question_id,
            detail=rejection,
        )

    action = str(payload.get("action") or "").strip().lower()
    if action == "advance":
        return TurnPlan(
            advance=True,
            acknowledgement=acknowledgement,
            failure=reflection_failure,
        )
    if action != "probe":
        return _failed_plan(
            FAILURE_BAD_ACTION,
            question,
            f"action {action!r} is not 'probe' or 'advance'",
            acknowledgement=acknowledgement,
        )

    index = payload.get("probe_index")
    if isinstance(index, bool) or not isinstance(index, int):
        return _failed_plan(
            FAILURE_BAD_PROBE_INDEX,
            question,
            f"probe_index {index!r} is not an integer",
            acknowledgement=acknowledgement,
        )
    if not 0 <= index < len(question.probes):
        return _failed_plan(
            FAILURE_BAD_PROBE_INDEX,
            question,
            f"probe_index {index} is outside 0-{len(question.probes) - 1}",
            acknowledgement=acknowledgement,
        )
    if index in probes_used:
        # Asking the same follow-up twice reads as not listening, and it is the
        # one way a bounded segment can still feel like a loop.
        return _failed_plan(
            FAILURE_PROBE_REPEATED,
            question,
            f"probe {index} was already asked on this question",
            acknowledgement=acknowledgement,
        )
    return TurnPlan(
        advance=False,
        probe=question.probes[index],
        probe_index=index,
        acknowledgement=acknowledgement,
        failure=reflection_failure,
    )


def _failed_plan(
    code: str,
    question: ScriptedQuestion,
    detail: object,
    *,
    acknowledgement: str | None = None,
) -> TurnPlan:
    """Advance, with the reason attached. The only fallback this module has."""
    logger.warning(
        "[smart_teams] turn planning fell back to advance on %s: %s (%s)",
        question.question_id,
        code,
        detail,
    )
    return TurnPlan(
        advance=True,
        acknowledgement=acknowledgement,
        failure=TurnFailure(
            code=code, question_id=question.question_id, detail=str(detail)[:400]
        ),
    )


# ── The state machine ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SegmentState:
    """Where one person is inside one conversational segment.

    Frozen, and every transition returns a new instance: the router holds this
    across an HTTP round trip and a mutable version would let a retried submit
    corrupt the turn it was retrying.

    ``turns`` is the whole thing that gets persisted — under
    ``script.answer_key``, via :meth:`answer_value`. Nothing else in here is
    stored today; see the module docstring on ``failures``.
    """

    section_key: str
    turns: tuple[Turn, ...]
    #: Index into ``script.questions`` of the question currently on screen.
    question_index: int
    #: Probe indexes already spent on that question.
    probes_used: tuple[int, ...] = ()
    finished: bool = False
    failures: tuple[TurnFailure, ...] = ()

    @property
    def transcript(self) -> tuple[Turn, ...]:
        """The segment as ``strengths``/``contracts`` want it. Order is load-bearing."""
        return self.turns

    def answer_value(self) -> list[dict[str, Any]]:
        """The value to store under ``script.answer_key``.

        Goes through ``contracts.transcript_to_json``, which is the same
        serializer ``interview.validate_answers`` round-trips on the way in — so
        a state that serializes here is a state the write path accepts.
        """
        return transcript_to_json(self.turns)

    @property
    def awaiting_answer(self) -> bool:
        """True when Cubie has spoken and it is the person's turn."""
        return bool(self.turns) and self.turns[-1].speaker is Speaker.ASSISTANT

    @property
    def pending_question(self) -> str | None:
        """What is on screen right now, or ``None`` when the segment is done."""
        if self.finished or not self.awaiting_answer:
            return None
        return self.turns[-1].text

    @property
    def probes_offered(self) -> int:
        """Follow-ups asked so far, across the whole segment."""
        asked: dict[str, int] = {}
        for turn in self.turns:
            if turn.speaker is Speaker.ASSISTANT and turn.prompt_id:
                asked[turn.prompt_id] = asked.get(turn.prompt_id, 0) + 1
        return sum(max(count - 1, 0) for count in asked.values())

    @property
    def closing_line(self) -> str | None:
        """The sign-off, once there is nothing left to ask."""
        if not self.finished:
            return None
        return CLOSING_LINES.get(self.section_key)


def begin_segment(script: SegmentScript) -> SegmentState:
    """Open the segment on its first authored question."""
    first = script.question_at(0)
    return SegmentState(
        section_key=script.section_key,
        turns=(
            Turn(
                speaker=Speaker.ASSISTANT,
                text=first.text,
                prompt_id=first.question_id,
            ),
        ),
        question_index=0,
    )


def submit_answer(
    script: SegmentScript,
    state: SegmentState,
    answer: str,
    *,
    plan: TurnPlan | None = None,
    model_id: str | None = None,
) -> SegmentState:
    """Record one answer and produce the next state.

    An empty answer is a skip, not an error: the person read the question and had
    nothing to say. No interviewee turn is written for it — a turn with no words
    in it would count as an answer in
    ``strengths.count_substantive_answers`` and would be a sentence nobody said —
    and the segment moves to the next question with no planning call.

    ``plan`` is an injection seam for tests and for a caller that already has a
    plan; leave it out and the planner runs.
    """
    if state.section_key != script.section_key:
        raise ConversationError(
            f"state is for {state.section_key!r}, script is for {script.section_key!r}"
        )
    if state.finished:
        raise ConversationError(
            f"{script.section_key}: the segment is already finished"
        )
    if not state.awaiting_answer:
        raise ConversationError(
            f"{script.section_key}: nothing is on screen to answer — the last turn "
            "is the person's. Call resume_segment() to recover this state."
        )

    question = script.question_at(state.question_index)
    text = (answer or "").strip()
    if not text:
        return _advance(script, state, acknowledgement=None, failure=None)

    turns = state.turns + (
        Turn(speaker=Speaker.INTERVIEWEE, text=text, prompt_id=question.question_id),
    )
    working = replace(state, turns=turns)

    decision = plan or plan_turn(
        script,
        question,
        turns,
        latest_answer=text,
        probes_used=state.probes_used,
        model_id=model_id,
    )

    if decision.advance:
        return _advance(
            script,
            working,
            acknowledgement=decision.acknowledgement,
            failure=decision.failure,
        )

    assert decision.probe is not None  # TurnPlan.__post_init__ guarantees it
    if decision.probe_index is not None and decision.probe_index in state.probes_used:
        # Defence in depth for an injected plan: plan_turn already refuses a
        # repeat, but a caller-supplied plan has not been through it.
        return _advance(
            script,
            working,
            acknowledgement=decision.acknowledgement,
            failure=TurnFailure(
                code=FAILURE_PROBE_REPEATED,
                question_id=question.question_id,
                detail=f"probe {decision.probe_index} was already asked",
            ),
        )
    if len(state.probes_used) >= MAX_PROBES_PER_QUESTION:
        return _advance(
            script,
            working,
            acknowledgement=decision.acknowledgement,
            failure=None,
        )

    return replace(
        working,
        turns=working.turns
        + (
            Turn(
                speaker=Speaker.ASSISTANT,
                text=_utterance(decision.acknowledgement, decision.probe),
                prompt_id=question.question_id,
            ),
        ),
        probes_used=state.probes_used
        + ((decision.probe_index,) if decision.probe_index is not None else ()),
        failures=_appended(state.failures, decision.failure),
    )


def _advance(
    script: SegmentScript,
    state: SegmentState,
    *,
    acknowledgement: str | None,
    failure: TurnFailure | None,
) -> SegmentState:
    """Move to the next authored question, or finish."""
    failures = _appended(state.failures, failure)
    next_index = state.question_index + 1
    if next_index >= len(script.questions):
        return replace(
            state,
            question_index=state.question_index,
            probes_used=(),
            finished=True,
            failures=failures,
        )
    question = script.question_at(next_index)
    return replace(
        state,
        turns=state.turns
        + (
            Turn(
                speaker=Speaker.ASSISTANT,
                # The acknowledgement was earned by the previous answer and is
                # folded into this turn, so its prompt_id names the next
                # question. Harmless: Cubie's words are never quotable and
                # prompt_id is only counted on interviewee turns.
                text=_utterance(acknowledgement, question.text),
                prompt_id=question.question_id,
            ),
        ),
        question_index=next_index,
        probes_used=(),
        failures=failures,
    )


def _utterance(acknowledgement: str | None, question: str) -> str:
    """One Cubie turn: the screened acknowledgement, then the authored question."""
    if not acknowledgement:
        return question
    return f"{acknowledgement} {question}"


def _appended(
    failures: tuple[TurnFailure, ...], failure: TurnFailure | None
) -> tuple[TurnFailure, ...]:
    return failures if failure is None else failures + (failure,)


def resume_segment(script: SegmentScript, stored: object) -> SegmentState:
    """Rebuild the state from a persisted transcript.

    ``stored`` is whatever sits under ``script.answer_key`` in
    ``st_interviews.answers`` — ``None`` or an empty list for a segment nobody has
    started. Reconstruction reads the Cubie turns' ``prompt_id``s, which is why
    every Cubie turn carries one.

    One deliberate simplification: if the stored transcript ends on the person's
    answer — a save that landed between the answer and the planning call — resume
    asks the next authored question rather than re-running the planner. Re-running
    it would spend a model call to reproduce a follow-up whose answer nobody is
    waiting for, and the cost of not doing it is one missed probe.
    """
    if stored is None or stored == []:
        return begin_segment(script)
    turns = transcript_from_json(stored)

    index_by_id = {
        question.question_id: index for index, question in enumerate(script.questions)
    }
    unknown = sorted(
        {
            turn.prompt_id
            for turn in turns
            if turn.prompt_id is not None and turn.prompt_id not in index_by_id
        }
    )
    if unknown:
        raise ConversationError(
            f"{script.section_key}: stored transcript names questions this script "
            f"does not have: {unknown}. The script changed under a live interview; "
            "the person has to redo the segment rather than have their answers "
            "attributed to questions they were not asked."
        )

    assistant_prompts = [
        turn.prompt_id
        for turn in turns
        if turn.speaker is Speaker.ASSISTANT and turn.prompt_id is not None
    ]
    if not assistant_prompts:
        return begin_segment(script)
    current_id = assistant_prompts[-1]
    question_index = index_by_id[current_id]
    probes_used = _recover_probes_used(script.question_at(question_index), turns)

    state = SegmentState(
        section_key=script.section_key,
        turns=turns,
        question_index=question_index,
        probes_used=probes_used,
    )
    if state.awaiting_answer:
        return state
    return _advance(script, state, acknowledgement=None, failure=None)


def _recover_probes_used(
    question: ScriptedQuestion, turns: Sequence[Turn]
) -> tuple[int, ...]:
    """Which follow-up indexes are already spent on ``question``.

    Recovered by matching the stored Cubie turn against the question's own probe
    text, because that is what the transcript actually records — an index is not
    stored anywhere. Matching on ``endswith`` rather than equality is what makes
    it work through a folded acknowledgement, which is prepended.

    A turn that matches nothing still counts against the bound: it is one
    follow-up that was asked, even if a script edit means we can no longer say
    which. Under-counting here would let a re-opened segment exceed
    :data:`MAX_PROBES_PER_QUESTION`, which is the one thing the bound exists to
    stop.
    """
    asked = [
        turn.text.strip()
        for turn in turns
        if turn.speaker is Speaker.ASSISTANT and turn.prompt_id == question.question_id
    ]
    # The first Cubie turn on a question is the question itself, not a probe.
    probe_turns = asked[1:]
    used: list[int] = []
    unmatched = 0
    for text in probe_turns:
        for index, probe in enumerate(question.probes):
            if index not in used and text.endswith(probe.strip()):
                used.append(index)
                break
        else:
            unmatched += 1
    if unmatched:
        spare = [index for index in range(len(question.probes)) if index not in used][
            :unmatched
        ]
        used.extend(spare)
        if len(spare) < unmatched:
            # More probe turns than the script has probes. Saturate rather than
            # silently allow another one.
            used.extend(range(len(question.probes), len(question.probes) + unmatched))
    return tuple(sorted(used))


# ── What a reader can tell afterwards ─────────────────────────────────────────


@dataclass(frozen=True)
class SegmentHealth:
    """What the persisted transcript alone says about how the segment went.

    Derived, never stored: every field is recomputed from the turns, so it cannot
    disagree with them. This is what the profile builder and a support admin
    looking at "why is this strengths section thin" have to work with.
    """

    section_key: str
    questions_in_script: int
    questions_asked: int
    #: Asked and left blank. Distinguishable from "never asked" because the Cubie
    #: turn is in the transcript either way.
    questions_unanswered: tuple[str, ...]
    substantive_answers: int
    interviewee_words: int
    probes_offered: int
    #: ``strengths.transcript_is_thin``'s answer, computed by
    #: :func:`segment_health` and stored rather than recomputed here — the bar
    #: belongs to the instrument, and this type does not get a second opinion on
    #: it. ``validate_extraction`` applies the confidence ceiling regardless of
    #: what is reported here.
    is_thin: bool

    @property
    def no_probes_offered(self) -> bool:
        """Zero follow-ups across the whole segment.

        Consistent with a planner that never worked, and **not proof** of one: a
        person who answers every question with a specific story earns no
        follow-ups either. Reported as the fact it is, so that whoever is looking
        at a thin profile knows to go and check the logs for
        :data:`FAILURE_TRANSPORT`.
        """
        return self.probes_offered == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "section_key": self.section_key,
            "questions_in_script": self.questions_in_script,
            "questions_asked": self.questions_asked,
            "questions_unanswered": list(self.questions_unanswered),
            "substantive_answers": self.substantive_answers,
            "interviewee_words": self.interviewee_words,
            "probes_offered": self.probes_offered,
            "no_probes_offered": self.no_probes_offered,
            "is_thin": self.is_thin,
        }


def segment_health(
    script: SegmentScript, transcript: InterviewTranscript
) -> SegmentHealth:
    """Read a persisted transcript for what it says about its own quality."""
    turns = tuple(transcript)
    asked: list[str] = []
    answered: set[str] = set()
    for turn in turns:
        if turn.prompt_id is None:
            continue
        if turn.speaker is Speaker.ASSISTANT:
            if turn.prompt_id not in asked:
                asked.append(turn.prompt_id)
        else:
            answered.add(turn.prompt_id)
    probes_offered = 0
    for question_id in asked:
        assistant_turns = sum(
            1
            for turn in turns
            if turn.speaker is Speaker.ASSISTANT and turn.prompt_id == question_id
        )
        probes_offered += max(assistant_turns - 1, 0)
    return SegmentHealth(
        section_key=script.section_key,
        questions_in_script=len(script.questions),
        questions_asked=len(asked),
        questions_unanswered=tuple(q for q in asked if q not in answered),
        substantive_answers=strengths.count_substantive_answers(turns),
        interviewee_words=strengths.interviewee_word_count(turns),
        probes_offered=probes_offered,
        is_thin=strengths.transcript_is_thin(turns),
    )


def failure_record(state: SegmentState) -> dict[str, Any]:
    """The failures as a JSON object, ready for a ``meta:`` key.

    Not persisted by anything today: ``interview._validate_structured`` declares
    a structured-answer shape for ``enneagram_confirmation`` and refuses one for
    every other section, on purpose, and adding one is a change to
    ``interview.py``. This is here so that change is a one-liner rather than a
    design conversation, and so the shape is decided once.
    """
    return {
        "section_key": state.section_key,
        "failures": [failure.as_dict() for failure in state.failures],
    }


# ── The Enneagram confirmation: no model, on purpose ──────────────────────────
# enneagram.confirmation_step already assembles the whole step — two candidates
# in plain language, four symmetric options, a footnote that says skipping costs
# nothing. There is nothing left for a model to decide, and putting one in the
# path would mean an LLM standing between a person and the sentence that
# overrides Cubie's guess about them.


@dataclass(frozen=True)
class ConfirmationPresentation:
    """The Enneagram confirmation step, in every state the read can be in.

    ``confirmation_step`` returns ``None`` for two very different reasons — the
    layer is already settled (self-identified, declined, not taken), or the items
    did not produce candidates. Only the first means "ask nothing". The second
    still owes the person a choice, and its copy is
    :attr:`EnneagramRead.note`; falling through silently there leaves them with
    neither a type nor a say in it.
    """

    #: False when the layer is already settled and there is nothing to ask.
    required: bool
    question: str
    #: In presentation order. The answer maps back to a type **by position**, so
    #: the UI must render them in the order it received them — see
    #: ``enneagram.FIRST_OPTION``.
    candidates: tuple[Mapping[str, Any], ...]
    options: tuple[str, ...]
    footnote: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "required": self.required,
            "question": self.question,
            "candidates": [dict(candidate) for candidate in self.candidates],
            "options": list(self.options),
            "footnote": self.footnote,
        }


#: The two options that are always available, whatever the scores did. Dropping
#: either turns an optional layer into a mandatory one.
#:
#: ``PICK_FROM_ALL_OPTION`` rather than ``OTHER_OPTION`` because this set is used
#: exactly when no candidates were shown: "Neither — let me pick from the nine"
#: asks the person to reject two types they were never offered. On an
#: undifferentiated bank there is nothing to be neither of, which is the whole
#: reason no candidates were named.
_ALWAYS_AVAILABLE: tuple[str, ...] = (
    enneagram.PICK_FROM_ALL_OPTION,
    enneagram.DECLINE_OPTION,
)


def enneagram_presentation(
    read: enneagram.EnneagramRead,
) -> ConfirmationPresentation:
    """What to put in front of the person for the Enneagram layer.

    Three cases, all of them real:

    * **Two candidates.** ``confirmation_step`` supplies the question and the
      four options. A narrow top-two margin has already been turned into "your
      answers point at these two about equally, so we would rather ask than
      guess" by ``score_enneagram`` — this function must not restate it as a
      finding, and does not, because the question text comes from there.
    * **No candidates** — too few answers to rank, or a bank that did not
      differentiate at all. The read's own ``note`` is the question, and the two
      standing options are the answer set: pick from the nine, or skip the layer.
    * **Already settled** — self-identified, declined, or not taken. ``required``
      is False and nothing is asked.
    """
    step = enneagram.confirmation_step(read)
    if step is not None:
        return ConfirmationPresentation(
            required=True,
            question=step.question,
            candidates=tuple(candidate.as_dict() for candidate in step.candidates),
            options=tuple(step.options),
            footnote=step.footnote,
        )
    if read.provenance == "unresolved":
        return ConfirmationPresentation(
            required=True,
            question=read.note,
            candidates=(),
            options=_ALWAYS_AVAILABLE,
            footnote=enneagram.CONFIRMATION_FOOTNOTE,
        )
    return ConfirmationPresentation(
        required=False,
        question=read.note,
        candidates=(),
        options=(),
        footnote=enneagram.CONFIRMATION_FOOTNOTE,
    )


def enneagram_answer(
    presentation: ConfirmationPresentation,
    option: str,
    *,
    type_number: int | None = None,
    wing: int | None = None,
    inferred_wing: int | None = None,
) -> dict[str, Any]:
    """Turn a chosen option into the stored ``meta:enneagram_confirmation`` value.

    Returns the blob normalized by ``interview.validate_answers`` — the same
    validator the scoring engine reads through — so a value produced here is a
    value that survives the write path. It raises rather than guessing on
    anything it cannot map, because a mis-mapped option writes down a type the
    person did not choose.

    Declining is a first-class outcome and the simplest path through this
    function: ``{"type_number": None, "wing": None, "declined": True}``, which is
    what ``EnneagramRead.with_decline()`` consumes, and which leaves every other
    layer of the profile intact.

    ``inferred_wing`` is the wing the questionnaire suggested. It is carried over
    only when it is still adjacent to the type the person actually picked —
    choosing the runner-up candidate usually invalidates it, and a wing that is
    not adjacent to its type is refused by the validator anyway.
    """
    if not presentation.required:
        raise ConversationError(
            "the Enneagram layer is already settled for this read; there is no "
            "confirmation answer to record"
        )
    if option not in presentation.options:
        raise ConversationError(
            f"{option!r} was not offered; this presentation offers "
            f"{presentation.options}"
        )

    if option == enneagram.DECLINE_OPTION:
        chosen: int | None = None
        chosen_wing: int | None = None
        declined = True
    else:
        declined = False
        if option in (enneagram.OTHER_OPTION, enneagram.PICK_FROM_ALL_OPTION):
            if type_number is None:
                raise ConversationError(f"{option!r} needs the type the person picked")
            chosen = type_number
            chosen_wing = wing
        else:
            position = presentation.options.index(option)
            if position >= len(presentation.candidates):
                raise ConversationError(
                    f"{option!r} names candidate {position}, but this presentation "
                    f"has {len(presentation.candidates)}"
                )
            chosen = int(presentation.candidates[position]["type_number"])
            chosen_wing = wing if wing is not None else inferred_wing
            if chosen_wing is not None and chosen_wing not in enneagram.adjacent(
                chosen
            ):
                chosen_wing = None

    blob = {"type_number": chosen, "wing": chosen_wing, "declined": declined}
    key = interview.SECTION_BY_KEY[interview.SECTION_ENNEAGRAM_CONFIRMATION].meta_key
    return interview.validate_answers({key: blob})[key]


__all__ = [
    "CLOSING_LINES",
    "COST_FEATURE",
    "FAILURE_BAD_ACTION",
    "FAILURE_BAD_PROBE_INDEX",
    "FAILURE_CODES",
    "FAILURE_PROBE_REPEATED",
    "FAILURE_REFLECTION_REJECTED",
    "FAILURE_TRANSPORT",
    "FAILURE_UNPARSEABLE",
    "MAX_PROBES_PER_QUESTION",
    "TURN_MODEL",
    "TURN_TEMPERATURE",
    "ConfirmationPresentation",
    "ConversationError",
    "ScriptedQuestion",
    "SegmentBounds",
    "SegmentHealth",
    "SegmentScript",
    "SegmentState",
    "TurnFailure",
    "TurnPlan",
    "begin_segment",
    "enneagram_answer",
    "enneagram_presentation",
    "failure_record",
    "motivators_probe_script",
    "plan_turn",
    "resume_segment",
    "segment_bounds",
    "segment_health",
    "strengths_script",
    "submit_answer",
]

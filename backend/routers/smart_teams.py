"""Smart Teams — the Work Style Interview API (ST-3).

The interview is ~29 minutes of scale banks and conversational segments (the
honest figure, computed by ``interview.duration_report()``; plan.md Decision 6
asks for ~25 and the gap is a product call, not a rounding), so nobody finishes
it in one sitting. Every endpoint here is therefore written around resume and
retry rather than around a submit: a save merges one section's answers into the
row, a cursor says where to come back to, and calling anything twice is a no-op
rather than a second interview or a second scoring run.

**Mounted on ``app``, NOT on the copilot-gated ``core`` parent** (see
``backend/main.py`` and the comment block above ``require_smart_teams_access``
in ``backend/auth.py``). A guide is only worth writing if the whole company has
one, and this is the surface that writes it — fencing it behind the copilot
gate would lock out most of the company while the weekly nudge kept asking them
to finish something they cannot open. The gate on the router is the audience.

**The subject is always the authenticated caller.** No endpoint below reads an
email out of a body, a header or a query string, and the one path that names a
subject at all is the support read, which runs ``authz.can_view_raw_profile``
against the path value rather than trusting it. This is the one feature holding
people's private psychometric answers, so an accepted ``user_email`` would be a
plain impersonation primitive over it. Every query folds the stored address with
``func.lower()`` (the functional indexes in ``database/smart_teams.py`` exist
for exactly that) because ``users.email`` is stored verbatim from the Google
token and a person whose token spells them ``JDoe@`` must still find their own
interview.

**Scoring runs on cubie-worker, never here.** Cloud Run throttles the service's
CPU to near-zero the moment the response returns, so a ``BackgroundTask`` doing
six instruments and an LLM extraction is starved silently — the interview would
look finished and no profile would ever appear. ``/complete`` commits the row
first and then dispatches through ``submit_background_work``, which is also why
completion is idempotent: the second call finds no open interview and dispatches
nothing.

Nothing here computes a score or writes ``st_profiles``. That is ST-4's engine,
reached only through the job module named in :data:`SCORING_JOB_MODULE` (and,
off-GCP, through :func:`_score_inline`). Everything in this module is ADVISORY
data collection — collaboration and staffing only, never performance review,
promotion, or compensation.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.auth import require_smart_teams_access
from backend.jobs.launcher import require_automations_http
from rubik_core.auth.registry import UserProfile
from rubik_core.database.db import get_db
from rubik_core.database.smart_teams import (
    SKILL_LEVELS,
    SmartTeamsInterview,
    SmartTeamsProfile,
    SmartTeamsSkill,
)
from rubik_core.smart_teams import conversation as cv
from rubik_core.smart_teams import heatmap as hm
from rubik_core.smart_teams import interview as iv
from rubik_core.smart_teams import profile as st_profile
from rubik_core.smart_teams.authz import RAW_PROFILE_DENIED, can_view_raw_profile
from rubik_core.smart_teams.instruments import (
    big_five,
    disc,
    enneagram,
    motivators,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/smart-teams",
    tags=["smart-teams"],
    dependencies=[Depends(require_smart_teams_access)],
)

#: The cubie-worker module that scores one completed interview into a profile
#: version. ST-4 owns it; this router only ever names it. Kept as a constant so
#: the dispatch call and the report of what was dispatched cannot drift.
SCORING_JOB_MODULE = "backend.jobs.smart_teams_score_job"

#: A completed interview with no profile after this long is reported to the
#: frontend as ``delayed`` rather than ``pending``. Read-only: there is no
#: failure column on ``st_interviews`` to flip, and inventing one here would
#: claim a state the schema does not hold.
#:
#: Imported rather than defined so this router and the nightly ops alert in
#: ``backend/jobs/smart_teams_skill_sweep_job.py`` cannot disagree about when a
#: scoring run stopped being slow and started being broken. Two literals would
#: drift, and the drift would show up as an alert firing at a different moment
#: from the screen that tells somebody their result is missing.
SCORING_STALE_SECONDS = st_profile.SCORING_STALE_SECONDS

#: Per-request write bounds. The largest section is the 50-item Big Five bank,
#: so this leaves room for a client that re-sends a whole section on retry
#: without leaving the door open to a multi-megabyte JSON body.
MAX_ANSWER_KEYS_PER_SAVE = 200
#: One conversational answer. Clipped in the handler and DISCLOSED in
#: ``caveats`` rather than bounded with a pydantic ``max_length``: FastAPI's 422
#: body echoes the rejected value verbatim, and echoing somebody's answer about
#: themselves back through an error payload is the wrong place to learn that.
MAX_TURN_CHARS = 8_000
#: Turns in one section's transcript. A strengths segment is five prompts with
#: probes (it once had eight, and the cap was never the binding
#: constraint either way); anything past this is a client looping.
MAX_TRANSCRIPT_TURNS = 200

#: The advisory promise, **verbatim** from ``instruments/disc.py`` — imported
#: rather than re-typed. It is the sentence that tells a person their answers
#: are never used for performance review, promotion, or compensation, and a
#: paraphrase drifting on this surface would be a different promise from the one
#: every other surface makes. Every response that opens the interview carries
#: it, because a notice the page has to hardcode is a notice that can silently
#: go missing (it already did: the first cut of this router never sent one).
ADVISORY_NOTICE = disc.ADVISORY_NOTICE

#: Who can read what, stated in the terms this router actually enforces:
#: ``authz.can_view_raw_profile`` grants the raw answers to the person and to a
#: support admin and to nobody else — not a manager, not an exec — and
#: ``st_documents`` cannot be ``published`` without an ``approved_at``. Kept next
#: to the advisory notice so the two are rendered together.
PRIVACY_NOTICE = (
    "What you type here stays with you. Your answers are readable by you and, "
    "if you ask for help with the interview, by an admin who supports Smart "
    "Teams — not by your manager and not by executives. Colleagues only ever see the guide "
    "written from your answers, and only after you have approved it. Retaking "
    "the interview starts a new version and leaves the old one behind; nothing "
    "you have already said is thrown away."
)


# ── Request models ────────────────────────────────────────────────────────────


class SectionSaveRequest(BaseModel):
    """One section's answers, as a merge rather than a replacement.

    ``answers`` is keyed the way ``st_interviews.answers`` is keyed — instrument
    item ids for the scale banks, ``transcript:<section>`` / ``meta:<section>`` /
    ``skill:<slug>`` for the rest — and every key must belong to the section in
    the path. Sending the same body twice lands the same row, which is the whole
    point: a save that times out client-side is safe to repeat.

    ``complete`` is a request, not an instruction. The handler completes the
    section only if the answers on the row actually support it (see
    :func:`_section_is_complete`), so a client that mis-tracks its own progress
    cannot skip half a bank and have the cursor move past it.
    """

    answers: dict[str, Any] = Field(default_factory=dict)
    complete: bool = False
    #: Only legal for a section the catalog marks ``optional`` (today: the
    #: motivators probe). Records the section as done with no answers, which is
    #: what ``Section.optional`` describes.
    skip: bool = False


class TurnRequest(BaseModel):
    """One step of a conversational section, for ``POST .../turn``.

    Two shapes in one model because the three conversational sections are two
    different interactions (see ``conversation.py``'s own table):

    * ``answer`` — the person's reply to whatever ``payload.conversation.
      pending_question`` was showing, for the strengths stories and the
      motivators probe. An empty string is a legitimate skip: they read the
      question and had nothing to say, and the runtime records no interviewee
      turn for it rather than writing down a sentence nobody said.
    * ``option`` (plus ``type_number`` / ``wing`` when the option is "let me
      pick from the nine") — the Enneagram confirmation, which makes **no model
      call at all**. The option is mapped back to a type BY POSITION inside
      ``conversation.enneagram_answer``, which is why the client must send the
      option string it was handed rather than a type it inferred itself.
    """

    answer: str | None = None
    option: str | None = None
    type_number: int | None = None
    wing: int | None = None


# ── Serializers ───────────────────────────────────────────────────────────────


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _fold(email: str | None) -> str:
    return (email or "").strip().lower()


def _section_dict(section: iv.Section) -> dict:
    """The catalog record for one section, as the page needs it."""
    return {
        "key": section.key,
        "title": section.title,
        "kind": section.kind,
        "answer_kind": section.answer_kind,
        "blurb": section.blurb,
        "optional": section.optional,
        "feeds": list(section.feeds),
        "item_ids": list(section.item_ids),
        "prompt_ids": list(section.prompt_ids),
        "value_min": section.value_min,
        "value_max": section.value_max,
        "estimated_seconds": section.estimated_seconds,
        "step_count": section.step_count,
    }


class _Unavailable(Exception):
    """A conversational section that cannot run yet, with copy that says why.

    Raised only by the two derived segments — the motivators probe needs the
    motivators bank scored, the Enneagram confirmation needs the Enneagram bank
    scorable. Rendered as ``available: false`` plus this reason on a read, and as
    a 409 carrying the same sentence on a turn. Never as a confident payload: a
    layer we cannot compute has to look uncomputed, because the alternative is a
    page that asks somebody to confirm a type nothing derived.
    """


def _conversation_dict(script: cv.SegmentScript, state: cv.SegmentState) -> dict:
    """One conversational segment's live state, as the page renders it.

    ``transcript`` is ``contracts.transcript_to_json`` — speaker-attributed,
    because ``strengths.validate_extraction`` checks every evidence quote
    against the interviewee's own turns and a flattened transcript would let a
    company-visible guide print Cubie's question back at somebody under "In your
    words". ``SegmentHealth`` is deliberately NOT here: ``is_thin`` is a
    diagnostic for a support admin, not a verdict to show the person who just
    answered.
    """
    return {
        "section_key": state.section_key,
        "transcript": state.answer_value(),
        "pending_question": state.pending_question,
        "awaiting_answer": state.awaiting_answer,
        "finished": state.finished,
        "closing_line": state.closing_line,
        "question_index": state.question_index,
        "questions_total": len(script.questions),
        "probes_offered": state.probes_offered,
        "max_probes_per_question": cv.MAX_PROBES_PER_QUESTION,
    }


def _script_for(
    section: iv.Section, answers: dict, completed: tuple[str, ...]
) -> cv.SegmentScript:
    """The authored question set for a conversational section.

    ``strengths`` is static. The motivators probe is not: its *wording* is chosen
    by ``motivators.probe_form`` from the SCORED result, and that scoring is done
    here, on the fly, from the answers already on the row — the alternative was
    the placeholder this endpoint used to return, which rendered an empty
    section. It is gated on the motivators bank being complete rather than
    scoring whatever is present, because a probe worded from a half-answered bank
    is a question chosen from a ranking the instrument would refuse to state.
    """
    if section.key == iv.SECTION_STRENGTHS:
        return cv.strengths_script()
    if section.key == iv.SECTION_MOTIVATORS_PROBE:
        if iv.SECTION_MOTIVATORS not in completed:
            raise _Unavailable(
                "These follow-up questions are chosen from your Work values "
                "answers. Finish that section first and this one will be ready."
            )
        bank = _section_answers(iv.SECTION_BY_KEY[iv.SECTION_MOTIVATORS], answers)
        try:
            result = motivators.score_motivators(
                {k: v for k, v in bank.items() if v is not None}
            )
            return cv.motivators_probe_script(result)
        except (ValueError, cv.ConversationError) as exc:
            logger.exception(
                "[smart-teams] the motivators probe could not be built: %s", exc
            )
            raise _Unavailable(
                "We can't put this section together from your Work values "
                "answers. An admin can pick this up — nothing you did is lost."
            ) from None
    raise _Unavailable(f"{section.key} is not a scripted conversational segment.")


def _enneagram_read(answers: dict) -> enneagram.EnneagramRead:
    """Score the Enneagram bank on the fly, for the confirmation step.

    This is the chicken-and-egg the placeholder used to punt on: the confirmation
    step reads back an inferred type, the inference needs the bank scored, and
    scoring proper is ST-4's job running AFTER the interview. Resolution: score
    the bank right here, on the request, from the answers already stored on the
    row. It is cheap (pure arithmetic, no model call — see ``conversation.py``'s
    "no model, on purpose" section) and it is the same function ST-4 will run, so
    the type the person is shown is the type that gets written down.

    Nothing is persisted from this scoring. The row keeps the answers; the read
    is recomputed on every request, so a person who goes back and edits the bank
    is confirming against their edited answers rather than a cached guess.

    An empty or thin bank is not an error: ``score_enneagram`` returns an
    ``unresolved`` read whose own ``note`` says there were not enough answers to
    suggest a type, and ``enneagram_presentation`` still offers the person the
    choice. Only an unscorable bank — an item id the instrument no longer has —
    is refused.
    """
    bank = _section_answers(iv.SECTION_BY_KEY[iv.SECTION_ENNEAGRAM], answers)
    try:
        return enneagram.score_enneagram(
            {k: v for k, v in bank.items() if v is not None}
        )
    except ValueError as exc:
        logger.exception("[smart-teams] the Enneagram bank did not score: %s", exc)
        raise _Unavailable(
            "We can't read this section back to you from your answers. An admin "
            "can pick this up — nothing you did is lost."
        ) from None


def _section_payload(
    section: iv.Section,
    db: Session,
    *,
    answers: dict | None = None,
    completed: tuple[str, ...] = (),
) -> dict:
    """Everything a section needs rendered, drawn from the instrument that owns it.

    Every branch stamps ``answer_kind`` — the same value as ``section.
    answer_kind`` — so the client has ONE discriminator to switch on rather than
    having to infer the shape from which keys happen to be present.

    Item text comes from each instrument's own ``interview_payload()`` where one
    exists, because those functions deliberately withhold keying and dimension —
    a respondent who can see which answer scores which trait is answering a
    different instrument. The two banks with no such function (Big Five,
    strengths) are assembled here to the same rule: id and prompt only, never
    ``domain``, ``keyed``, ``purpose`` or ``tends_to_surface``.

    The three conversational sections now carry their live runtime state
    (``conversation`` / ``confirmation``), computed from the answers on the row.
    They used to carry an empty ``prompts`` list and a note saying the data lived
    somewhere else, which rendered as a blank section.
    """
    owned = set(section.item_ids)
    answers = answers or {}

    if section.key == iv.SECTION_DISC_LIKERT:
        payload = disc.interview_payload()
        return {
            "answer_kind": section.answer_kind,
            "scale": payload["scale"],
            "items": [i for i in payload["likert"] if i["item_id"] in owned],
        }
    if section.key == iv.SECTION_DISC_FORCED_CHOICE:
        payload = disc.interview_payload()
        return {
            "answer_kind": section.answer_kind,
            "instruction": payload["forced_choice_prompt"],
            "items": [p for p in payload["forced_choice"] if p["item_id"] in owned],
        }
    if section.key == iv.SECTION_BIG_FIVE:
        return {
            "answer_kind": section.answer_kind,
            "scale": [
                {"value": value, "label": label}
                for value, label in big_five.RESPONSE_LABELS.items()
            ],
            "stem": big_five.ITEM_STEM,
            "items": [
                {"item_id": item.item_id, "prompt": item.text}
                for item in big_five.ITEMS
                if item.item_id in owned
            ],
        }
    if section.key == iv.SECTION_MOTIVATORS:
        payload = motivators.interview_payload()
        return {
            "answer_kind": section.answer_kind,
            "scale": payload["scale"],
            "items": [i for i in payload["items"] if i["item_id"] in owned],
        }
    if section.key == iv.SECTION_ENNEAGRAM:
        payload = enneagram.interview_payload()
        return {
            "answer_kind": section.answer_kind,
            "scale": payload["scale"],
            "items": [i for i in payload["items"] if i["item_id"] in owned],
        }
    if section.kind == "conversational" and section.answer_kind == "transcript":
        base: dict[str, Any] = {
            "answer_kind": section.answer_kind,
            "turn_endpoint": f"/api/smart-teams/interview/sections/{section.key}/turn",
        }
        try:
            script = _script_for(section, answers, completed)
        except _Unavailable as exc:
            return {
                **base,
                "available": False,
                "unavailable_reason": str(exc),
                "prompts": [],
                "conversation": None,
            }
        try:
            state = cv.resume_segment(script, answers.get(section.transcript_key))
        except (cv.ConversationError, ValueError) as exc:
            logger.exception(
                "[smart-teams] %s transcript would not resume: %s", section.key, exc
            )
            return {
                **base,
                "available": False,
                "unavailable_reason": (
                    "This conversation was recorded against an earlier version "
                    "of the questionnaire and can't be picked up where it left "
                    "off. An admin can recover what you already said."
                ),
                "prompts": [],
                "conversation": None,
            }
        return {
            **base,
            "available": True,
            "unavailable_reason": None,
            "prompts": [
                {"prompt_id": question.question_id, "question": question.text}
                for question in script.questions
            ],
            "conversation": _conversation_dict(script, state),
        }
    if section.key == iv.SECTION_ENNEAGRAM_CONFIRMATION:
        base = {
            "answer_kind": section.answer_kind,
            "answer_keys": list(iv.ENNEAGRAM_CONFIRMATION_KEYS),
            "types": [
                {"number": number, "label": enneagram.TYPES[number].label}
                for number in enneagram.TYPE_ORDER
            ],
            "turn_endpoint": f"/api/smart-teams/interview/sections/{section.key}/turn",
        }
        try:
            read = _enneagram_read(answers)
        except _Unavailable as exc:
            return {
                **base,
                "available": False,
                "unavailable_reason": str(exc),
                "confirmation": None,
            }
        presentation = cv.enneagram_presentation(read)
        return {
            **base,
            "available": True,
            "unavailable_reason": None,
            "confirmation": {
                **presentation.as_dict(),
                # Where the read came from, so the page can say "from your
                # answers" instead of implying a settled finding.
                # ``answered_items`` is 0 when the bank is empty, which is
                # exactly when ``question`` is the instrument's "not enough
                # answers to suggest a type" sentence.
                "answered_items": read.answered,
                "provenance": read.provenance,
                "confidence": read.confidence,
                "inferred_wing": read.wing,
            },
        }
    if section.key == iv.SECTION_SKILLS:
        rows = (
            db.query(SmartTeamsSkill)
            .filter(SmartTeamsSkill.is_active.is_(True))
            .order_by(SmartTeamsSkill.cluster, SmartTeamsSkill.name)
            .all()
        )
        return {
            "answer_kind": section.answer_kind,
            "levels": list(SKILL_LEVELS),
            "skills": [
                {"skill_key": row.skill_key, "name": row.name, "cluster": row.cluster}
                for row in rows
            ],
        }
    return {"answer_kind": section.answer_kind}


def _progress_dict(progress: iv.Progress) -> dict:
    return {
        "sections_total": progress.sections_total,
        "sections_done": progress.sections_done,
        "steps_total": progress.steps_total,
        "steps_done": progress.steps_done,
        "seconds_total": progress.seconds_total,
        "seconds_done": progress.seconds_done,
        "percent": progress.percent,
        "minutes_remaining": progress.minutes_remaining,
        "current_section_key": progress.current_section_key,
        "is_complete": progress.is_complete,
    }


def _section_answers(section: iv.Section, answers: dict) -> dict:
    """Just this section's slice of the answers blob.

    Routing is ``Section.owns_key`` — exact set membership for the banks — so
    the ``disc_fc_`` / ``disc_`` prefix overlap cannot leak one DISC bank's
    answers into the other's payload.
    """
    return {key: value for key, value in answers.items() if section.owns_key(key)}


def _notices(state: dict) -> dict:
    """Stamp the two notices onto a state the interview page opens with.

    Separate from :func:`_resume_state` so the cheap poll (``/interview/status``)
    is not carrying two paragraphs of prose on a two-second interval, and a
    function rather than a literal so no branch of ``/interview`` can be the one
    that forgets. ``test_every_interview_entry_point_carries_both_notices``
    pins all of them.
    """
    state["advisory_notice"] = ADVISORY_NOTICE
    state["privacy_notice"] = PRIVACY_NOTICE
    return state


def _resume_state(
    row: SmartTeamsInterview,
    db: Session,
    *,
    include_content: bool = True,
    include_all_answers: bool = False,
) -> dict:
    """The shape every read of one's own interview returns.

    ``answers`` is included only for the raw reads (the subject's own full blob
    and the support read). The ordinary resume response carries the
    current section's slice, because that is what the page renders and because a
    50-item bank the person is not on is noise on the wire.
    """
    answers = dict(row.answers or {})
    completed = iv.normalize_completed(row.sections_completed)
    section = iv.resume_target(row.section_cursor, completed)
    progress = iv.progress(completed, answers, section_cursor=row.section_cursor)

    state: dict[str, Any] = {
        "interview_id": row.id,
        "status": row.status,
        "sections_completed": list(completed),
        "progress": _progress_dict(progress),
        "current_section": _section_dict(section) if section else None,
        # The raw stored cursor and the RESOLVED one. They differ whenever the
        # cursor points at a section that is already in ``sections_completed``
        # (a save that completed the section it was on), and the page has to
        # navigate to the resolved one — ``resume_target`` is the function that
        # decides, and shipping only the raw value made the client re-implement
        # it.
        "section_cursor": row.section_cursor,
        "resume_section_key": section.key if section else None,
        "section_answers": _section_answers(section, answers) if section else {},
        "first_unanswered_item": (
            iv.first_unanswered_item(section, answers) if section else None
        ),
        "started_at": _iso(row.started_at),
        "last_activity_at": _iso(row.last_activity_at),
        "completed_at": _iso(row.completed_at),
        "profile_id": row.profile_id,
        "sections": [_section_dict(s) for s in iv.SECTIONS],
    }
    if section and include_content:
        # Named to match ``payload`` on the section read: one shape, one name.
        # It was ``current_section_content`` here and ``content`` there, which is
        # how a client ends up reading a key that never arrives.
        state["current_section_payload"] = _section_payload(
            section, db, answers=answers, completed=completed
        )
    if include_all_answers:
        state["answers"] = answers
    return state


# ── Row access ────────────────────────────────────────────────────────────────


def _rows_for(db: Session, email: str, *, status: str | None = None) -> list:
    """Every interview row for one folded address, newest activity first.

    ``func.lower`` on the stored column rather than a plain ``==``: the ORM folds
    on write and a CHECK constraint backs it, but the functional indexes exist
    because reads have to fold too, and a row written by a seed script before
    that constraint landed would otherwise be invisible to its own owner.
    """
    query = db.query(SmartTeamsInterview).filter(
        func.lower(SmartTeamsInterview.user_email) == email
    )
    if status is not None:
        query = query.filter(SmartTeamsInterview.status == status)
    return query.order_by(
        SmartTeamsInterview.last_activity_at.desc(),
        SmartTeamsInterview.started_at.desc(),
    ).all()


def _open_interview(db: Session, email: str) -> SmartTeamsInterview | None:
    """The person's one open interview, collapsing a raced duplicate if it exists.

    There is no database uniqueness for "one in_progress row per person" —
    ``ix_st_interviews_user_status`` is an index, not a constraint — so two
    requests that both miss on the read (a double-clicked Start, a retried POST
    on a flaky connection) can both insert. Left alone that is worse than
    untidy: the client keeps writing to whichever row it was handed, so half the
    answers land on a row nothing will ever score.

    The keeper is the row with the most answers, ties broken by the earliest
    start, and the losers are marked ``abandoned`` — an empty duplicate created
    milliseconds later carries nothing, and if the race went the other way the
    row holding the person's work is the one that survives. Loud in the log,
    because a repeat means the race is not a race but a bug in the caller.
    """
    rows = _rows_for(db, email, status="in_progress")
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0]

    def _weight(row: SmartTeamsInterview) -> tuple[int, float]:
        started = row.started_at or datetime.utcnow()
        return (len(row.answers or {}), -started.timestamp())

    keeper = max(rows, key=_weight)
    logger.warning(
        "[smart-teams] %s has %d open interviews; keeping %s and abandoning the rest",
        email,
        len(rows),
        keeper.id,
    )
    for row in rows:
        if row.id != keeper.id:
            row.status = "abandoned"
    db.commit()
    return keeper


def _newest_interview(db: Session, email: str) -> SmartTeamsInterview | None:
    rows = _rows_for(db, email)
    return rows[0] if rows else None


def _current_profile(db: Session, email: str) -> SmartTeamsProfile | None:
    """The person's live profile version, if scoring has produced one.

    ``is_current`` is ``True`` or ``NULL`` and never ``False``, which is what
    makes ``uq_st_profile_one_current`` mean one live row per person — so the
    filter is ``is_(True)`` and not a truthiness test.
    """
    return (
        db.query(SmartTeamsProfile)
        .filter(func.lower(SmartTeamsProfile.user_email) == email)
        .filter(SmartTeamsProfile.is_current.is_(True))
        .order_by(SmartTeamsProfile.version.desc())
        .first()
    )


def _profile_dict(row: SmartTeamsProfile | None) -> dict | None:
    if row is None:
        return None
    return {
        "id": row.id,
        "version": row.version,
        "status": row.status,
        "origin": row.origin,
        "scored_at": _iso(row.scored_at),
        "provenance": row.provenance or {},
    }


def _stale_row_error(row: SmartTeamsInterview | None) -> HTTPException:
    """The 409 for a row whose stored section keys are no longer in the catalog.

    ``normalize_completed`` and ``resume_target`` raise on an unknown key on
    purpose: it means the item banks moved under a live interview, and dropping
    the key would silently score a shorter interview than the person answered.
    Translated to a 409 with copy the person can act on, rather than a 500.
    """
    logger.exception(
        "[smart-teams] interview row %s names a section the catalog no longer has",
        row.id if row is not None else "-",
    )
    return HTTPException(
        status_code=409,
        detail=(
            "Your interview was started against an earlier version of the "
            "questionnaire and can't be resumed. Start a new one — an admin can "
            "recover the answers you already gave."
        ),
    )


# ── Read: resume ──────────────────────────────────────────────────────────────


@router.get("/interview")
def get_interview(
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """Where the caller is in their own interview. Never creates a row.

    "You haven't started" is a 200 with a ``status`` rather than a 404: it is an
    explainable state the page renders as a Start button, not an error.
    """
    email = _fold(user.email)
    row = _open_interview(db, email)
    if row is None:
        newest = _newest_interview(db, email)
        if newest is not None and newest.status == "completed":
            state = _completion_state(db, email, newest)
            state["can_retake"] = True
            return _notices(state)
        return _notices(
            {
                "status": "not_started",
                "sections": [_section_dict(s) for s in iv.SECTIONS],
                "estimated_minutes": iv.duration_report().required_minutes,
                "can_retake": False,
            }
        )
    try:
        return _notices(_resume_state(row, db))
    except iv.AnswerError:
        raise _stale_row_error(row) from None


@router.post("/interview/start")
def start_interview(
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """Start the interview, or hand back the one already in progress.

    Idempotent by construction: an open row is returned as-is, so a double-click
    or a retried POST resumes instead of starting the person over. A completed
    interview is NOT reopened — retaking writes a NEW row so the answers a scored
    profile cites are never edited underneath it — so this returns the completion
    state with ``can_retake: true`` and :func:`retake_interview` is the endpoint
    that acts on it. That split is deliberate: "resume where I was" must never be
    the request that silently starts somebody's interview over.
    """
    email = _fold(user.email)
    row = _open_interview(db, email)
    created = False
    if row is None:
        newest = _newest_interview(db, email)
        if newest is not None and newest.status == "completed":
            state = _completion_state(db, email, newest)
            state["created"] = False
            state["can_retake"] = True
            return _notices(state)
        row = _new_interview_row(db, email)
        created = True

    try:
        state = _resume_state(row, db)
    except iv.AnswerError:
        raise _stale_row_error(row) from None
    state["created"] = created
    state["can_retake"] = False
    return _notices(state)


def _new_interview_row(db: Session, email: str) -> SmartTeamsInterview:
    """Insert one fresh ``in_progress`` row, positioned on the first section."""
    row = SmartTeamsInterview(
        id=uuid.uuid4().hex,
        user_email=email,
        status="in_progress",
        section_cursor=iv.SECTIONS[0].key,
        sections_completed=[],
        answers={},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info("[smart-teams] interview %s started for %s", row.id, email)
    return row


@router.post("/interview/retake", status_code=201)
def retake_interview(
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """Take the interview again: a NEW row, with the old one and its profile intact.

    Without this endpoint every employee got exactly one interview forever, and
    the whole versioning half of the schema was unreachable — ``profile.
    write_profile_version``'s ``version = max + 1``, the ``is_current = NULL``
    retirement of the predecessor, and ``uq_st_profile_one_current`` all only
    ever see version 1. People change teams and change jobs; a work-style guide
    nobody can refresh goes stale and then gets quietly distrusted.

    What it does NOT do is reopen the completed row. The scored profile cites
    those answers as its provenance, so editing them in place would make an
    already-published guide unverifiable. A retake is an append: a second
    ``st_interviews`` row, the old one left ``completed`` with its ``profile_id``
    still pointing at the version it produced, and scoring the new one retires
    the old profile to ``superseded`` / ``is_current = NULL`` by the writer's own
    rules.

    409 when there is nothing to retake, rather than quietly behaving like
    ``/interview/start``: a client that means "start" should say so.
    """
    email = _fold(user.email)
    open_row = _open_interview(db, email)
    if open_row is not None:
        # Already in one. Retaking is not a way to wipe an interview in progress
        # — that is ``/interview/abandon`` — so this resumes instead, and says it
        # created nothing.
        try:
            state = _resume_state(open_row, db)
        except iv.AnswerError:
            raise _stale_row_error(open_row) from None
        state["created"] = False
        state["previous_interview_id"] = None
        state["can_retake"] = False
        return _notices(state)

    newest = _newest_interview(db, email)
    if newest is None or newest.status != "completed":
        raise HTTPException(
            status_code=409,
            detail=(
                "You don't have a finished interview to retake. Start one "
                "instead — anything you'd already answered is still there."
            ),
        )

    row = _new_interview_row(db, email)
    logger.info(
        "[smart-teams] %s is retaking; interview %s follows completed %s",
        email,
        row.id,
        newest.id,
    )
    state = _resume_state(row, db)
    state["created"] = True
    state["previous_interview_id"] = newest.id
    state["can_retake"] = False
    return _notices(state)


@router.get("/interview/sections/{section_key}")
def get_section(
    section_key: str,
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """One section of the caller's own interview — its content and their answers.

    Exists so the page can navigate BACK to a finished section without the
    resume response having to ship all 155 answers on every poll.
    """
    section = iv.SECTION_BY_KEY.get(section_key)
    if section is None:
        raise HTTPException(
            status_code=404, detail="That interview section doesn't exist."
        )

    email = _fold(user.email)
    row = _open_interview(db, email) or _newest_interview(db, email)
    try:
        return _section_read(section, row, db)
    except iv.AnswerError:
        raise _stale_row_error(row) from None


def _section_read(
    section: iv.Section, row: SmartTeamsInterview | None, db: Session
) -> dict:
    """One section as the page renders it. Shared with the turn endpoint.

    ``payload`` (not ``content``) and ``progress`` alongside it: the client
    switches on ``payload.answer_kind`` and draws the header from ``progress``,
    and a section read that shipped neither made the page fetch the resume state
    again just to move a bar.
    """
    answers = dict(row.answers or {}) if row else {}
    completed = iv.normalize_completed(row.sections_completed) if row else ()
    return {
        "interview_id": row.id if row else None,
        "interview_status": row.status if row else None,
        "section": _section_dict(section),
        "payload": _section_payload(section, db, answers=answers, completed=completed),
        "answers": _section_answers(section, answers),
        "progress": _progress_dict(
            iv.progress(
                completed,
                answers,
                section_cursor=row.section_cursor if row else None,
            )
        ),
        "is_completed": section.key in completed,
        "first_unanswered_item": iv.first_unanswered_item(section, answers),
    }


# ── Write: save one section ───────────────────────────────────────────────────


def _known_skill_keys(db: Session) -> frozenset[str] | None:
    """The curated taxonomy slugs, or ``None`` while there is no taxonomy.

    Passing the set is what stops a client inventing skills the Team Builder can
    never match on. But ``st_skills_taxonomy`` is seeded by ST-8, and passing an
    EMPTY set would reject every declaration a person makes with a message about
    a taxonomy that does not exist yet. So: membership is enforced as soon as
    there is something to enforce, and until then keys are checked for shape
    only, with the degraded check named in the log rather than left to be
    discovered.
    """
    keys = {
        key
        for (key,) in db.query(SmartTeamsSkill.skill_key)
        .filter(SmartTeamsSkill.is_active.is_(True))
        .all()
    }
    if not keys:
        logger.warning(
            "[smart-teams] st_skills_taxonomy has no active rows; declared "
            "skills are shape-checked only until ST-8 seeds it"
        )
        return None
    return frozenset(keys)


def _clip_transcript(value: Any, caveats: list[str]) -> Any:
    """Bound a conversational answer, disclosing the clip instead of hiding it.

    Deliberately not a pydantic ``max_length``: FastAPI's 422 body echoes the
    rejected input verbatim, and the rejected input here is what somebody just
    typed about themselves. So the bound lives in the handler, the answer is
    kept rather than refused, and the response says so.
    """
    if not isinstance(value, list):
        return value
    turns = value
    if len(turns) > MAX_TRANSCRIPT_TURNS:
        turns = turns[:MAX_TRANSCRIPT_TURNS]
        caveats.append(
            f"Only the first {MAX_TRANSCRIPT_TURNS} turns of that conversation "
            "were saved."
        )
    clipped: list[Any] = []
    trimmed = False
    for turn in turns:
        if isinstance(turn, dict) and isinstance(turn.get("text"), str):
            text = turn["text"]
            if len(text) > MAX_TURN_CHARS:
                turn = {**turn, "text": text[:MAX_TURN_CHARS]}
                trimmed = True
        clipped.append(turn)
    if trimmed:
        caveats.append(
            f"One or more answers were shortened to {MAX_TURN_CHARS} characters."
        )
    return clipped


def _section_is_complete(
    section: iv.Section, answers: dict, *, requested: bool
) -> bool:
    """Whether this section may be recorded as done.

    A scale bank is done when every one of its items has a value — computed from
    the row, never taken from the client, because a cursor that moves past a
    half-answered bank produces a profile scored on answers the person never
    gave. The other three kinds cannot be counted that way and need the client's
    "done" as well as the evidence:

    * a conversational section needs a non-empty transcript. The agent decides
      when the conversation has run its course; this only refuses to call an
      empty one finished.
    * the Enneagram confirmation needs its ``meta:`` answer present. Declining
      the layer IS that answer (``{"declined": true}``), which is why a decline
      completes the section rather than skipping it.
    * declared skills need nothing but the request. Declaring no skills is a
      legitimate answer, so there is no evidence to require.
    """
    if section.item_ids:
        return all(answers.get(item_id) is not None for item_id in section.item_ids)
    if not requested:
        return False
    if section.answer_kind == "transcript":
        return bool(answers.get(section.transcript_key))
    if section.answer_kind == "structured":
        return answers.get(section.meta_key) is not None
    return True


def _commit_section(
    db: Session,
    row: SmartTeamsInterview,
    section: iv.Section,
    answers: dict,
    *,
    done: bool,
) -> tuple[bool, tuple[str, ...]]:
    """Write one section's answers to the row and move the cursor if it is done.

    Shared by the save and the turn endpoint so there is one place that decides
    where the person lands next. Reassigns ``answers`` and ``sections_completed``
    rather than mutating them: they are plain ``JSON`` columns, so an in-place
    update on the dict SQLAlchemy handed us is not seen as dirty and the write
    silently does nothing.
    """
    completed = list(iv.normalize_completed(row.sections_completed))
    if done and section.key not in completed:
        completed.append(section.key)
    completed_t = iv.normalize_completed(completed)

    row.answers = answers
    row.sections_completed = list(completed_t)
    upcoming = iv.next_section(completed_t) if done else section
    row.section_cursor = upcoming.key if upcoming else None
    row.last_activity_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return done, completed_t


@router.put("/interview/sections/{section_key}")
def save_section(
    section_key: str,
    body: SectionSaveRequest,
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """Merge answers into one section of the caller's own open interview.

    Safe to call as often as a flaky connection needs to: answers are merged by
    key, so a repeated body writes the same values; ``sections_completed`` is
    rebuilt through ``normalize_completed``, which dedupes and reorders, so a
    repeated completion cannot append twice; and a transcript key holds the WHOLE
    conversation for its section, so re-sending it replaces rather than doubles
    the turns.

    The cursor moves only when the section is genuinely finished
    (:func:`_section_is_complete`). Refusals are 4xx and specific: 400 for a key
    that belongs to another section or to no section at all (an item bank moved,
    or a client is inventing ids — either way, loud), 422 for a well-formed key
    carrying a value the instrument does not permit.
    """
    section = iv.SECTION_BY_KEY.get(section_key)
    if section is None:
        raise HTTPException(
            status_code=404, detail="That interview section doesn't exist."
        )

    email = _fold(user.email)
    row = _open_interview(db, email)
    if row is None:
        raise HTTPException(
            status_code=409,
            detail="You don't have an interview in progress. Start one, then save answers.",
        )

    if body.skip and not section.optional:
        raise HTTPException(
            status_code=400,
            detail=f"The {section.title} section can't be skipped.",
        )
    if len(body.answers) > MAX_ANSWER_KEYS_PER_SAVE:
        raise HTTPException(
            status_code=400,
            detail=(
                "That's more answers than one section holds. Save the section "
                "you're on and try again."
            ),
        )

    caveats: list[str] = []
    incoming: dict[str, Any] = {}
    for key, value in body.answers.items():
        if not isinstance(key, str) or not section.owns_key(key):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"'{key}' isn't an answer in the {section.title} section. "
                    "Reload the section and try again."
                ),
            )
        if key == section.transcript_key:
            value = _clip_transcript(value, caveats)
        incoming[key] = value

    known = _known_skill_keys(db) if section.answer_kind == "skill_level" else None
    try:
        validated = iv.validate_answers(incoming, known_skill_keys=known)
    except iv.UnknownAnswerKey as exc:
        logger.warning("[smart-teams] unrouted answer key from %s: %s", email, exc)
        raise HTTPException(
            status_code=400,
            detail=(
                "One of those answers doesn't match this questionnaire. Reload "
                "the section and try again."
            ),
        ) from None
    except iv.AnswerValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except iv.AnswerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    answers = dict(row.answers or {})
    answers.update(validated)

    try:
        section_done, completed_t = _commit_section(
            db,
            row,
            section,
            answers,
            done=body.skip
            or _section_is_complete(section, answers, requested=body.complete),
        )
    except iv.AnswerError:
        raise _stale_row_error(row) from None

    state = _resume_state(row, db)
    state["saved_section"] = section.key
    state["saved_keys"] = sorted(validated)
    state["section_completed"] = section_done
    state["skipped"] = bool(body.skip)
    state["caveats"] = caveats
    state["ready_to_complete"] = iv.is_complete(completed_t)
    return state


# ── Write: one conversational turn ────────────────────────────────────────────


@router.post("/interview/sections/{section_key}/turn")
def take_turn(
    section_key: str,
    body: TurnRequest,
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """Advance one conversational section by one exchange.

    This is the endpoint that makes ``rubik_core/smart_teams/conversation.py``
    reachable. Until it existed, three of the nine sections had no runtime: the
    strengths stories and the motivators probe rendered an empty prompt list, and
    the required Enneagram confirmation had no question at all.

    **Strengths / motivators probe.** The stored transcript is rebuilt with
    ``conversation.resume_segment``, the answer goes through
    ``conversation.submit_answer`` — which is where the one planner call per
    answer happens, bounded to
    :data:`~rubik_core.smart_teams.conversation.MAX_PROBES_PER_QUESTION`
    follow-ups and degrading to the authored script when the model is
    unreachable — and the whole transcript is written back under
    ``transcript:<section_key>``. Speaker attribution survives the round trip
    because the value written is ``SegmentState.answer_value()``, i.e.
    ``contracts.transcript_to_json``: ``strengths.validate_extraction`` checks
    every evidence quote against the interviewee's own turns, so a transcript
    that lost the distinction would let a company-visible guide quote Cubie's
    question back at somebody as their own words.

    The section completes when the RUNTIME says the script is exhausted, not when
    the client says so. That is the one thing the client cannot know: whether the
    planner offered a follow-up depends on a decision taken inside this request.

    **Enneagram confirmation.** No model call, on purpose. The bank is scored on
    the fly (see :func:`_enneagram_read`), ``conversation.enneagram_answer`` maps
    the chosen option back to a type BY POSITION, and the result is stored under
    ``meta:enneagram_confirmation``. Declining is a first-class answer that
    completes the section.

    Not idempotent, and cannot be: a second POST with the same answer is a second
    answer to the next question, exactly as if the person had typed it twice.
    Clients must not retry a turn blindly — read the section back instead.
    """
    section = iv.SECTION_BY_KEY.get(section_key)
    if section is None:
        raise HTTPException(
            status_code=404, detail="That interview section doesn't exist."
        )
    if section.kind != "conversational":
        raise HTTPException(
            status_code=400,
            detail=(
                f"The {section.title} section isn't a conversation. Save its "
                "answers with PUT instead."
            ),
        )

    email = _fold(user.email)
    row = _open_interview(db, email)
    if row is None:
        raise HTTPException(
            status_code=409,
            detail="You don't have an interview in progress. Start one, then answer.",
        )
    answers = dict(row.answers or {})
    try:
        completed = iv.normalize_completed(row.sections_completed)
    except iv.AnswerError:
        raise _stale_row_error(row) from None

    caveats: list[str] = []
    if section.answer_kind == "structured":
        validated, accepted, done = _confirmation_turn(section, answers, body)
    else:
        validated, accepted, done, turn_caveats = _transcript_turn(
            section, answers, completed, body
        )
        caveats.extend(turn_caveats)

    # Also commits when there is nothing new to store but the segment has run
    # out of questions: a conversation that finished must not leave the person
    # parked on a section with nothing left to answer.
    if validated or (done and section.key not in completed):
        answers.update(validated)
        try:
            _, completed_t = _commit_section(db, row, section, answers, done=done)
        except iv.AnswerError:
            raise _stale_row_error(row) from None
    else:
        completed_t = completed

    state = _section_read(section, row, db)
    state["accepted"] = accepted
    state["section_completed"] = section.key in completed_t
    state["sections_completed"] = list(completed_t)
    state["current_section"] = (
        _section_dict(iv.resume_target(row.section_cursor, completed_t))
        if not iv.is_complete(completed_t)
        else None
    )
    state["ready_to_complete"] = iv.is_complete(completed_t)
    state["caveats"] = caveats
    return state


def _transcript_turn(
    section: iv.Section,
    answers: dict,
    completed: tuple[str, ...],
    body: TurnRequest,
) -> tuple[dict, bool, bool, list[str]]:
    """One answer into a scripted segment. Returns (validated, accepted, done, caveats)."""
    caveats: list[str] = []
    try:
        script = _script_for(section, answers, completed)
    except _Unavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    try:
        state = cv.resume_segment(script, answers.get(section.transcript_key))
    except (cv.ConversationError, ValueError) as exc:
        logger.exception("[smart-teams] %s would not resume: %s", section.key, exc)
        raise HTTPException(
            status_code=409,
            detail=(
                "This conversation was recorded against an earlier version of "
                "the questionnaire and can't be picked up where it left off. "
                "An admin can recover what you already said."
            ),
        ) from None

    if state.finished:
        # Nothing left to ask. Reported rather than refused so a client that
        # posted one turn too many gets the finished conversation back instead of
        # an error it has to special-case.
        return {}, False, True, caveats

    text = body.answer or ""
    if len(text) > MAX_TURN_CHARS:
        text = text[:MAX_TURN_CHARS]
        caveats.append(f"Your answer was shortened to {MAX_TURN_CHARS} characters.")
    try:
        state = cv.submit_answer(script, state, text)
    except cv.ConversationError as exc:
        logger.exception("[smart-teams] %s turn refused: %s", section.key, exc)
        raise HTTPException(
            status_code=409,
            detail=(
                "That answer didn't land where we expected it to. Reload the "
                "section — nothing you already said is lost."
            ),
        ) from None

    stored = state.answer_value()
    if len(stored) > MAX_TRANSCRIPT_TURNS:
        # The bound the save path applies, applied here too. A segment cannot
        # reach it — ``segment_bounds`` caps the turns arithmetically — so this
        # firing means the stored transcript arrived from somewhere else.
        stored = stored[:MAX_TRANSCRIPT_TURNS]
        caveats.append(
            f"Only the first {MAX_TRANSCRIPT_TURNS} turns of this conversation "
            "were saved."
        )
    try:
        validated = iv.validate_answers({section.transcript_key: stored})
    except iv.AnswerError as exc:
        logger.exception(
            "[smart-teams] %s transcript did not validate: %s", section.key, exc
        )
        raise HTTPException(
            status_code=422,
            detail=(
                "We couldn't save that answer. An admin can pick this up — "
                "nothing you already said is lost."
            ),
        ) from None
    return validated, True, state.finished, caveats


def _confirmation_turn(
    section: iv.Section, answers: dict, body: TurnRequest
) -> tuple[dict, bool, bool]:
    """The Enneagram confirmation choice. Returns (validated, accepted, done)."""
    try:
        read = _enneagram_read(answers)
    except _Unavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    presentation = cv.enneagram_presentation(read)
    if not presentation.required:
        # Only reachable for a read that is already settled, which this path
        # cannot produce — it scores the bank fresh every time. Refused rather
        # than guessed at, because the alternative is writing down a type nobody
        # was asked about.
        raise HTTPException(
            status_code=409,
            detail="There's nothing to confirm in this section.",
        )
    if not body.option:
        raise HTTPException(
            status_code=400,
            detail=("Pick one of the options shown before saving this section."),
        )
    try:
        blob = cv.enneagram_answer(
            presentation,
            body.option,
            type_number=body.type_number,
            wing=body.wing,
            inferred_wing=read.wing,
        )
    except cv.ConversationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except iv.AnswerError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return {section.meta_key: blob}, True, True


# ── Write: complete, and hand off to cubie-worker ─────────────────────────────


def _score_inline(interview_id: str) -> None:
    """Off-GCP fallback for the scoring dispatch. Never runs on the deployed app.

    ``submit_background_work`` only reaches this when ``APP_MODE`` is not GCP
    (local development) or when the Cloud Run Job launch itself failed, and in
    the second case it has already logged that loudly. It resolves ST-4's entry
    point at call time rather than at import, so this router keeps importing —
    and the interview keeps being answerable — while the scoring engine is still
    being written.

    The expected interface is
    ``rubik_core.smart_teams.scoring.score_interview(interview_id: str)``: load
    the row, build a ``contracts.ScoringInput`` from its answers and
    ``sections_completed``, and write the next ``st_profiles`` version. If ST-4
    lands a different name this logs it and the interview stays ``completed``
    with no profile, which ``/interview/status`` reports as ``delayed`` rather
    than pretending the score is on its way.
    """
    try:
        from rubik_core.smart_teams import scoring  # ST-4
    except ImportError:
        logger.error(
            "[smart-teams] interview %s completed but rubik_core.smart_teams."
            "scoring is not importable — no profile will be written",
            interview_id,
        )
        return
    entry = getattr(scoring, "score_interview", None)
    if entry is None:
        logger.error(
            "[smart-teams] interview %s completed but "
            "rubik_core.smart_teams.scoring.score_interview is missing",
            interview_id,
        )
        return
    try:
        entry(interview_id)
    except Exception:  # noqa: BLE001 — a fallback must not take the request down
        logger.exception("[smart-teams] inline scoring failed for %s", interview_id)


def _completion_state(
    db: Session, email: str, row: SmartTeamsInterview | None = None
) -> dict:
    """What the frontend polls after completion: is there a profile yet?

    ``st_interviews.profile_id`` is the authoritative link once ST-4 sets it,
    but a live current profile row is also proof that scoring finished, so both
    are consulted — the poll should not hang on a link that a scoring engine
    forgot to write back.

    ``delayed`` is a report, not a state transition. Nothing here writes a
    failure to the row, because ``st_interviews`` has no column to write it to
    and claiming otherwise in a response would be a lie about the schema.
    """
    row = row or _newest_interview(db, email)
    profile = _current_profile(db, email)
    if row is None:
        return {"status": "not_started", "scoring": {"status": "not_started"}}

    if row.status == "in_progress":
        scoring_status = "not_started"
    elif profile is not None or row.profile_id:
        scoring_status = "ready"
    else:
        age = (
            datetime.utcnow() - (row.completed_at or datetime.utcnow())
        ).total_seconds()
        scoring_status = "delayed" if age > SCORING_STALE_SECONDS else "pending"

    payload: dict[str, Any] = {
        "status": row.status,
        "interview_id": row.id,
        "completed_at": _iso(row.completed_at),
        "scoring": {
            "status": scoring_status,
            "job_module": SCORING_JOB_MODULE,
        },
        "profile": _profile_dict(profile),
    }
    if scoring_status == "delayed":
        # Say it is probably BROKEN, not merely slow. Scoring is a Cloud Run Job
        # that takes about three minutes; at twenty it is not still working.
        #
        # The old copy here — "hasn't come back yet… we can pick this up" —
        # read as reassurance and cost a real person their result. cubie-worker
        # was running a month-old image with no smart_teams_score_job in it, so
        # every dispatch returned 202 and did nothing. The person waited, the
        # screen stayed calm, and nobody was told anything had failed.
        #
        # The interview id is included because it is the one thing whoever fixes
        # it needs, and asking somebody to find it in a database while their own
        # result is missing is the wrong way round. Re-running is
        # ``python -m backend.jobs.smart_teams_score_job --interview-id <id>``
        # on cubie-worker; that command is not in this payload on purpose, since
        # the reader is the subject rather than the operator.
        payload["scoring"]["message"] = (
            "Your answers are all saved, but scoring has not finished and it has "
            "been long enough that something has probably gone wrong rather than "
            "being slow. Nothing you did is lost and you do not need to take the "
            "interview again. Send an admin this reference and they can re-run it: "
            f"{row.id}."
        )
        payload["scoring"]["looks_stuck"] = True
        payload["scoring"]["stale_after_seconds"] = SCORING_STALE_SECONDS
    return payload


@router.post("/interview/complete", status_code=202)
def complete_interview(
    background_tasks: BackgroundTasks,
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """Close the interview and hand scoring to cubie-worker.

    Exactly one job per interview, and the mechanism is the row rather than a
    lock: the status flips to ``completed`` and is committed BEFORE the dispatch,
    so a retried POST finds no open interview and returns the completion state
    with ``enqueued: false``. The commit-then-dispatch order is also what makes
    the job safe to start — it loads the row it was given.

    Scoring never runs in this process. Cloud Run throttles the service's CPU to
    near-zero once the response returns, so a ``BackgroundTask`` doing six
    instruments and an LLM extraction is starved silently and the person's
    profile simply never appears.
    """
    email = _fold(user.email)
    row = _open_interview(db, email)
    if row is None:
        state = _completion_state(db, email)
        state["enqueued"] = False
        return state

    require_automations_http()

    try:
        completed = list(iv.normalize_completed(row.sections_completed))
    except iv.AnswerError:
        raise _stale_row_error(row) from None

    # An optional section nobody touched is recorded as skipped rather than
    # blocking completion. Whether the motivators probe applies at all is a
    # function of the SCORED motivators result (``probe_reason``), which this
    # request path does not have — so a person could otherwise be held at a
    # question the system has not yet decided to ask them.
    auto_skipped = [
        section.key
        for section in iv.remaining_sections(completed)
        if section.optional and not _section_answers(section, dict(row.answers or {}))
    ]
    completed.extend(auto_skipped)
    completed_t = iv.normalize_completed(completed)

    if not iv.is_complete(completed_t):
        missing = [s.key for s in iv.remaining_sections(completed_t)]
        raise HTTPException(
            status_code=409,
            detail=(
                "There are still sections to finish: "
                + ", ".join(iv.SECTION_BY_KEY[key].title for key in missing)
                + "."
            ),
        )

    row.sections_completed = list(completed_t)
    row.section_cursor = None
    row.status = "completed"
    row.completed_at = datetime.utcnow()
    row.last_activity_at = row.completed_at
    db.commit()
    db.refresh(row)

    from backend.jobs.launcher import submit_background_work

    interview_id = row.id
    executor, execution_name = submit_background_work(
        background_tasks,
        module=SCORING_JOB_MODULE,
        args=["--interview-id", interview_id],
        fallback=lambda: _score_inline(interview_id),
        label=f"smart-teams-score:{email}",
    )
    logger.info(
        "[smart-teams] interview %s completed for %s; scoring dispatched via %s (%s)",
        interview_id,
        email,
        executor,
        execution_name,
    )

    state = _completion_state(db, email, row)
    state["enqueued"] = True
    state["auto_skipped_sections"] = auto_skipped
    state["scoring"]["executor"] = executor
    state["scoring"]["execution_name"] = execution_name
    return state


@router.post("/interview/abandon")
def abandon_interview(
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """Abandon the caller's open interview so they can start again.

    The row is kept and its status set to ``abandoned`` — the answers a person
    already gave are theirs, the completion sweep needs to know they started,
    and a delete would take both. Nothing to abandon is a 200 with a ``status``,
    not a 404: repeating the call is a no-op by design.
    """
    email = _fold(user.email)
    row = _open_interview(db, email)
    if row is None:
        return {"status": "none"}
    row.status = "abandoned"
    row.last_activity_at = datetime.utcnow()
    db.commit()
    logger.info("[smart-teams] interview %s abandoned by %s", row.id, email)
    return {"status": "abandoned", "interview_id": row.id}


@router.get("/interview/status")
def interview_status(
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """Poll: has scoring finished and is there a profile?

    Cheap on purpose — the interview page polls this after completion, so it
    reads two rows and returns no answers.
    """
    return _completion_state(db, _fold(user.email))


# ── Support read (support admins only, by the shared rule) ───────────────────────────


@router.get("/interview/answers")
def get_own_answers(
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """The caller's own full answers blob — everything they typed, nobody else's.

    Separate from the resume read so the ordinary poll does not ship 155 answers
    on every request, and separate from the support read below so that the "own
    data" path never takes a subject from the URL.
    """
    email = _fold(user.email)
    row = _open_interview(db, email) or _newest_interview(db, email)
    if row is None:
        return {"status": "not_started"}
    try:
        return _resume_state(row, db, include_content=False, include_all_answers=True)
    except iv.AnswerError:
        raise _stale_row_error(row) from None


@router.get("/support/interviews/{subject_email}")
def support_read_interview(
    subject_email: str,
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """One person's raw interview, for a support admin debugging it with them.

    The path value names the SUBJECT and is never trusted as the caller:
    ``authz.can_view_raw_profile`` decides, and it grants exactly two viewers —
    the person themselves and a support admin. An exec is refused, a team
    manager is refused, and an admin outside the support set is refused, which is the
    rule that module exists to state once. The refusal copy comes from there too
    (:data:`RAW_PROFILE_DENIED`: it points the reader at the published
    guide they CAN read) so every surface says the same thing.

    403 rather than 404 deliberately, unlike the CRM Sync run reads: that a
    colleague has taken the Work Style Interview is not a secret — the
    completion dashboard is a rollout deliverable — while what they answered is.
    """
    subject = _fold(subject_email)
    if not can_view_raw_profile(user, subject):
        raise HTTPException(status_code=403, detail=RAW_PROFILE_DENIED)

    # Audited, because this is the read that returns somebody's 155 private
    # answers. The guide and pair support reads have logged since they shipped;
    # this one did not, which was survivable only while no screen called it.
    # "Who read whose interview" has to be answerable from the logs.
    logger.info(
        "[smart-teams] support read of %s's interview by %s",
        subject,
        _fold(user.email),
    )
    row = _newest_interview(db, subject)
    if row is None:
        return {"status": "not_started", "subject_email": subject}
    try:
        state = _resume_state(row, db, include_content=False, include_all_answers=True)
    except iv.AnswerError:
        raise _stale_row_error(row) from None
    state["subject_email"] = subject
    state["profile"] = _profile_dict(_current_profile(db, subject))
    return state


# ── The heatmap grid ─────────────────────────────────────────────────────────


@router.get("/heatmap/me")
def own_heatmap(
    user: UserProfile = Depends(require_smart_teams_access),
    db: Session = Depends(get_db),
) -> dict:
    """The caller's own dimensional grid — every layer, WITH the numbers.

    No subject parameter, and there will not be one: this is the caller's own
    profile and nothing else. A colleague's grid is reachable only through the
    pair read in ``smart_teams_comparison``, which applies the consent gate and
    hands back bands rather than intensities.

    ``can_view_raw_profile(user, user.email)`` is true by construction — you are
    your own subject — so this is not a special case for admins, it is the same
    predicate every other read uses, answered for the ordinary caller. It is
    called rather than assumed so that an impersonation identity, which that
    predicate refuses even for its own address, gets bands like everybody else.

    Motivators appear here and nowhere two people are shown together: the
    instrument's ``emphasis`` is within-person, so this grid is the only place
    it means anything.
    """
    subject = _fold(user.email)
    profile = _current_profile(db, subject)
    if profile is None:
        return {
            "state": "not_scored",
            "cells": [],
            "profile_version": None,
            "shows_numbers": False,
            "advisory_notice": ADVISORY_NOTICE,
            "privacy_notice": PRIVACY_NOTICE,
        }

    include_scores = can_view_raw_profile(user, subject)
    envelopes = hm.envelopes_of(profile)
    cells = hm.profile_cells(envelopes, include_scores=include_scores)
    return {
        "state": "ready" if cells else "nothing_reportable",
        "cells": [cell.as_dict() for cell in cells],
        # Their dot on the map, placed from every layer that legitimately
        # places anybody — DISC and Big Five, not DISC alone. Same projection
        # every other surface uses, so a position never moves between screens.
        "style_marker": hm.map_marker(envelopes),
        "profile_version": profile.version,
        # Reported so the page can say WHY there are no numbers, rather than
        # leaving a reader to wonder whether scoring half-failed.
        "shows_numbers": include_scores,
        "advisory_notice": ADVISORY_NOTICE,
        "privacy_notice": PRIVACY_NOTICE,
    }

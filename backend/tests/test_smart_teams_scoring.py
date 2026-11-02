"""ST-4: what the scoring engine must never do, and what the writer must never lose.

Every test here is aimed at a failure that would ship silently.

The refusal tests are the important half. A Smart Teams profile is shown to the
person it describes and then, once they approve a guide, to the whole company —
so a layer that reads as confident when the answers do not support it is not a
cosmetic bug, it is a paragraph about somebody that nobody can point at any
evidence for. The instruments each refuse in their own vocabulary and
``contracts.verdict_for`` normalises them; these tests check that the engine
propagates that refusal rather than flattening it, and that a refused layer's
contents are genuinely unreachable (``contracts.layer_of`` returns ``None``), not
merely accompanied by a hedge some future renderer might skip.

``test_a_healthy_sitting_reports_the_deterministic_layers`` exists to keep the
refusal tests honest. Without it, an engine that refused *everything* would pass
every other test in this file.

The writer tests cover the two races that produce wrong data rather than errors:
two versions claiming the same number (which makes published guides
unauditable, since a guide cites the version it was generated from) and one
sitting producing two profile versions because a retried Cloud Run task scored
it twice. Both are simulated by making the engine's own read stale at the moment
the race would make it stale, so the test exercises the real recovery path
instead of asserting that a lock exists.

No test in this file makes a network call: the strengths extractor is injected
everywhere except the two tests that exercise the real
:func:`~rubik_core.smart_teams.scoring.extract_strengths`, and those substitute
the JSON client.
"""

from __future__ import annotations

import uuid

import pytest

from backend.tests._legacy_brand import legacy_terms
from rubik_core.smart_teams import contracts, scoring
from rubik_core.smart_teams import interview as interview_catalog
from rubik_core.smart_teams.instruments import (
    big_five,
    disc,
    enneagram,
    motivators,
    strengths,
)

SHAY = "shay@acme.example"

# ── Answer-set builders ──────────────────────────────────────────────────────
# Built from the item banks, never from literal ids, so an item bank edit changes
# the fixtures instead of breaking them into a shape that quietly scores less.

_BIG_FIVE_TARGETS = {
    big_five.Domain.EXTRAVERSION: 5,
    big_five.Domain.AGREEABLENESS: 4,
    big_five.Domain.CONSCIENTIOUSNESS: 5,
    big_five.Domain.EMOTIONAL_STABILITY: 2,
    big_five.Domain.OPENNESS: 4,
}

#: Distinct weights per Spranger axis, so the ranking settles and no probe fires.
_MOTIVATOR_WEIGHTS = (5, 3, 4, 2, 1, 2)
_MOTIVATOR_POSITION = {name: i for i, name in enumerate(motivators.MOTIVATORS)}

_STORY = (
    "I like untangling a messy audit file until the story is obvious to "
    "everyone else in the room, and then writing the two sentences that make "
    "it obvious. "
)
_QUOTE = "untangling a messy audit file until the story is obvious"
_ASSISTANT_QUESTION = "What kind of work leaves you with more energy than it took?"


def _keyed(value: int, reverse: bool) -> int:
    """Flip a target answer for a reverse-worded item, on the 1-5 scale."""
    return (disc.LIKERT_MIN + disc.LIKERT_MAX) - value if reverse else value


def _disc_answers(*, flat: int | None = None) -> dict[str, object]:
    """A DISC-dominant sitting, or a straight line when ``flat`` is given."""
    out: dict[str, object] = {}
    for item in disc.LIKERT_ITEMS:
        if flat is not None:
            out[item.item_id] = flat
            continue
        out[item.item_id] = _keyed(
            5 if item.dimension == "D" else 2, item.keying == "reverse"
        )
    for pair in disc.FORCED_CHOICE_PAIRS:
        if flat is not None:
            out[pair.item_id] = "a"
        elif pair.option_a.dimension == "D":
            out[pair.item_id] = "a"
        elif pair.option_b.dimension == "D":
            out[pair.item_id] = "b"
        else:
            # Alternate on the remaining pairs so the position-share check has
            # something other than one column to look at.
            out[pair.item_id] = "a" if pair.option_a.dimension == "I" else "b"
    return out


def _big_five_answers(*, flat: int | None = None) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in big_five.ITEMS:
        if flat is not None:
            out[item.item_id] = flat
            continue
        out[item.item_id] = _keyed(
            _BIG_FIVE_TARGETS[item.domain], item.is_reverse_keyed
        )
    return out


def _motivator_answers(*, flat: int | None = None) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in motivators.LIKERT_ITEMS:
        if flat is not None:
            out[item.item_id] = flat
            continue
        weight = _MOTIVATOR_WEIGHTS[_MOTIVATOR_POSITION[item.motivator]]
        out[item.item_id] = _keyed(weight, item.keying == "reverse")
    return out


def _enneagram_answers(*, flat: int | None = None) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in enneagram.LIKERT_ITEMS:
        if flat is not None:
            out[item.item_id] = flat
            continue
        out[item.item_id] = 5 if item.type_number == 3 else 2
    return out


def _transcript_answer() -> list[dict[str, object]]:
    return contracts.transcript_to_json(
        [
            contracts.Turn(
                speaker=contracts.Speaker.ASSISTANT,
                text=_ASSISTANT_QUESTION,
                prompt_id=strengths.ELICITATION_SCRIPT[0].prompt_id,
            ),
            contracts.Turn(
                speaker=contracts.Speaker.INTERVIEWEE,
                text=_STORY * 6,
                prompt_id=strengths.ELICITATION_SCRIPT[0].prompt_id,
            ),
        ]
    )


def _answers(*, flat: int | None = None) -> dict[str, object]:
    """A whole ``st_interviews.answers`` blob.

    ``flat`` sets every Likert item in every bank to the same value, which is the
    straight-lining / all-midpoint case.
    """
    out: dict[str, object] = {}
    out.update(_disc_answers(flat=flat))
    out.update(_big_five_answers(flat=flat))
    out.update(_motivator_answers(flat=flat))
    out.update(_enneagram_answers(flat=flat))
    out["transcript:strengths"] = _transcript_answer()
    out["meta:enneagram_confirmation"] = {
        "type_number": None,
        "wing": None,
        "declined": False,
    }
    return out


def _input(*, flat: int | None = None, completed=None, email: str = SHAY):
    return contracts.ScoringInput(
        user_email=email,
        answers=_answers(flat=flat),
        sections_completed=(
            interview_catalog.SECTION_KEYS if completed is None else tuple(completed)
        ),
        elapsed_seconds=25 * 60.0,
    )


def _two_themes() -> strengths.StrengthsRead:
    """A reportable strengths read, built through the instrument's own types."""
    ids = list(strengths.THEMES)[:2]
    return strengths.StrengthsRead(
        themes=tuple(
            strengths.ThemeRead(
                theme_id=theme_id,
                confidence=strengths.Confidence.HIGH,
                evidence_quote=_QUOTE,
                why="Named this pattern unprompted, twice.",
            )
            for theme_id in ids
        )
    )


def _stub_extractor(read: strengths.StrengthsRead):
    def extractor(_transcript, **_kwargs):
        return read

    return extractor


# ── Refusal propagation ──────────────────────────────────────────────────────


def test_all_midpoint_answers_refuse_every_layer():
    """Nobody is described from a page of 3s.

    All six instruments have a different reason to refuse this sitting — DISC
    calls a retake on the straight line, Big Five's blocking quality flags take
    the letters down with them, the motivator profile is level, the Enneagram
    top two is contested, and the strengths extractor found nothing — and the
    engine has to carry all six through rather than settling for the most
    convenient one. The ``layer_of`` assertion is the load-bearing one: a hedge
    a renderer might ignore is not a gate, so a refused layer's contents must be
    unreachable.
    """
    report = scoring.score_answers(
        _input(flat=3), extractor=_stub_extractor(strengths.StrengthsRead(themes=()))
    )

    assert report.reportable_frameworks == ()
    assert report.is_empty
    for framework in contracts.FRAMEWORKS:
        outcome = report.output.outcomes[framework]
        assert outcome.verdict.reportable is False, framework
        assert outcome.verdict.confidence == "none", framework
        assert outcome.verdict.reasons, f"{framework} refused without saying why"
        assert contracts.layer_of(outcome.envelope) is None, framework
    # And nothing raised: a refused profile is still a profile.
    assert report.failures == {}


def test_a_straight_lined_sitting_is_flagged_not_just_hedged():
    """The instrument's own careless-responding flags reach the payload.

    A page of 5s is the interview nobody actually took. DISC must flag it at
    ``retake`` severity and the engine must not publish the style name it can
    still technically compute — that name is the most quotable line in the whole
    guide.
    """
    report = scoring.score_answers(
        _input(flat=5), extractor=_stub_extractor(_two_themes())
    )

    disc_outcome = report.output.outcomes["disc"]
    codes = {flag.code for flag in disc_outcome.result.quality.flags}
    assert disc.FLAG_STRAIGHT_LINING in codes
    assert disc_outcome.result.quality.retake_recommended is True
    assert contracts.REASON_RETAKE_RECOMMENDED in disc_outcome.verdict.reasons
    assert disc_outcome.verdict.reportable is False
    # The style is computable and deliberately unreachable.
    assert disc_outcome.result.assignment.style is not None
    assert contracts.layer_of(disc_outcome.envelope) is None

    bf_outcome = report.output.outcomes["big_five"]
    assert bf_outcome.result.quality.has_blocking_flags is True
    assert bf_outcome.verdict.reportable is False
    # type16 is arithmetic on those same answers, so it cannot be trustworthier.
    assert report.output.outcomes["type16"].verdict.reportable is False


def test_a_healthy_sitting_reports_the_deterministic_layers():
    """The control. Without this, "refuse everything" would pass the suite."""
    report = scoring.score_answers(_input(), extractor=_stub_extractor(_two_themes()))

    assert report.failures == {}
    assert set(report.reportable_frameworks) == {
        "disc",
        "big_five",
        "type16",
        "strengths",
        "motivators",
        "enneagram",
    }
    for framework in contracts.FRAMEWORKS:
        outcome = report.output.outcomes[framework]
        assert outcome.verdict.confidence in {"high", "moderate", "low"}
        assert contracts.layer_of(outcome.envelope) is not None
    # The derived four-letter code always carries its qualifier, wherever it is
    # rendered from — the guard is in the value, not in a convention.
    code = report.output.outcomes["type16"].result.code
    assert "(derived from Big Five)" in str(code)


def test_answers_from_an_unfinished_section_are_not_scored():
    """``sections_completed`` is the authority, not the presence of answers.

    The blob here is complete; the person has only submitted the first DISC
    bank. Every other layer must come back ``not_taken`` — scoring a grid
    somebody is still editing puts answers they had not committed to into a
    profile they are then asked to approve.
    """
    report = scoring.score_answers(
        _input(completed=[interview_catalog.SECTION_DISC_LIKERT]),
        extractor=_stub_extractor(_two_themes()),
    )

    for framework in ("big_five", "type16", "motivators", "enneagram", "strengths"):
        outcome = report.output.outcomes[framework]
        assert outcome.verdict.reasons == (contracts.REASON_NOT_TAKEN,), framework
        assert outcome.envelope[contracts.KEY_LAYER] is None, framework
        assert outcome.result is None, framework
    # A NULL column and "asked, not finished" would be indistinguishable; the
    # envelope is written either way.
    assert report.output.as_profile_columns()["big_five"] is not None
    # The bank that WAS submitted is scored, on its own.
    assert report.output.outcomes["disc"].result is not None


def test_a_declined_enneagram_keeps_no_inferred_type():
    """Declining drops the read, so no later feature can use a rejected guess."""
    answers = _answers()
    answers["meta:enneagram_confirmation"] = {
        "type_number": None,
        "wing": None,
        "declined": True,
    }
    report = scoring.score_answers(
        contracts.ScoringInput(
            user_email=SHAY,
            answers=answers,
            sections_completed=interview_catalog.SECTION_KEYS,
        ),
        extractor=_stub_extractor(_two_themes()),
    )
    outcome = report.output.outcomes["enneagram"]
    assert outcome.result.provenance == "declined"
    assert outcome.result.type_number is None
    assert outcome.result.ranked == ()
    assert outcome.verdict.reasons == (contracts.REASON_DECLINED,)


def test_a_self_identified_type_overrides_the_inferred_one():
    """Whatever the person says wins, and says so through provenance."""
    answers = _answers()
    answers["meta:enneagram_confirmation"] = {
        "type_number": 8,
        "wing": 7,
        "declined": False,
    }
    report = scoring.score_answers(
        contracts.ScoringInput(
            user_email=SHAY,
            answers=answers,
            sections_completed=interview_catalog.SECTION_KEYS,
        ),
        extractor=_stub_extractor(_two_themes()),
    )
    result = report.output.outcomes["enneagram"].result
    assert (result.provenance, result.type_number, result.wing) == (
        "self_identified",
        8,
        7,
    )
    # The questionnaire ranking is kept for a Systems admin, not discarded.
    assert result.ranked


# ── Partial failure ──────────────────────────────────────────────────────────


def test_a_model_outage_costs_the_strengths_layer_and_nothing_else():
    """Five deterministic layers must survive a Vertex failure."""

    def boom(_transcript, **_kwargs):
        raise RuntimeError("503 Service Unavailable")

    report = scoring.score_answers(_input(), extractor=boom)

    assert report.failed_frameworks == ("strengths",)
    failure = report.failures["strengths"]
    assert failure.stage == scoring.STAGE_LLM
    assert "503" in failure.message
    assert report.output.outcomes["strengths"].verdict.reportable is False
    # The person sees an absent layer, not an error message about Vertex.
    assert report.output.outcomes["strengths"].envelope[contracts.KEY_HEDGE] is None
    assert set(report.reportable_frameworks) == {
        "disc",
        "big_five",
        "type16",
        "motivators",
        "enneagram",
    }


def test_an_off_contract_extraction_is_a_validate_failure_not_a_layer():
    """A ``ValueError`` from the validator is recorded as a contract failure."""

    def bad(_transcript, **_kwargs):
        raise ValueError("theme_id 'strategic_thinker' is not in the taxonomy")

    report = scoring.score_answers(_input(), extractor=bad)
    assert report.failures["strengths"].stage == scoring.STAGE_VALIDATE
    assert len(report.reportable_frameworks) == 5


def test_an_extractor_returning_the_wrong_type_is_refused():
    """Defence against a future caller passing something that only looks right."""
    report = scoring.score_answers(_input(), extractor=lambda _t, **_k: {"themes": []})
    assert report.failures["strengths"].stage == scoring.STAGE_VALIDATE
    assert report.output.outcomes["strengths"].result is None


def test_off_taxonomy_and_unquoted_themes_never_reach_the_payload(monkeypatch):
    """The real extractor, with a stubbed client, over a hostile response.

    Three things the model can get wrong, in one payload: a theme whose quote
    was never said, a theme whose "quote" is Cubie's own question, and two
    honest ones. Only the honest two may survive — and the survivors are checked
    against the taxonomy, not against the model's self-report.
    """
    real_ids = list(strengths.THEMES)[:4]
    payload = {
        "themes": [
            {
                "theme_id": real_ids[0],
                "confidence": "high",
                "evidence_quote": _QUOTE,
                "why": "Said it unprompted.",
            },
            {
                "theme_id": real_ids[1],
                "confidence": "moderate",
                "evidence_quote": _QUOTE,
                "why": "Same story, different angle.",
            },
            {
                "theme_id": real_ids[2],
                "confidence": "high",
                "evidence_quote": "I am a natural leader of large teams",
                "why": "Invented — this sentence is nowhere in the transcript.",
            },
            {
                "theme_id": real_ids[3],
                "confidence": "high",
                "evidence_quote": _ASSISTANT_QUESTION,
                "why": "Quoted Cubie's own question back as evidence.",
            },
        ]
    }

    class _Client:
        def invoke_json(self, _prompt, *, fallback=None, attempts=1):
            return payload

    monkeypatch.setattr(scoring, "_json_client", lambda *_a, **_k: _Client())
    read = scoring.extract_strengths(
        contracts.transcript_from_json(_transcript_answer())
    )
    assert [theme.theme_id for theme in read.themes] == real_ids[:2]
    assert len(read.themes) == 2
    assert read.is_reportable is True


def test_the_strengths_call_is_attributed_to_the_smart_teams_cost_bucket(monkeypatch):
    """The AI Cost tag has to be live at the moment the model is called.

    Asserting inside the client is the only version of this test that means
    anything: ``@attribute_to`` sets a contextvar, and a tag applied at an entry
    point that later hands work to a thread would pass any assertion made
    outside the call.
    """
    from rubik_core.cost.usage import current_feature

    seen = {}

    class _Client:
        def invoke_json(self, _prompt, *, fallback=None, attempts=1):
            seen["feature"] = current_feature.get()
            return {"themes": []}

    monkeypatch.setattr(scoring, "_json_client", lambda *_a, **_k: _Client())
    scoring.extract_strengths(contracts.transcript_from_json(_transcript_answer()))
    assert seen["feature"] == scoring.COST_FEATURE == "smart_teams"


def test_the_cost_bucket_has_a_label_and_a_centralised_model_id():
    """An unlabelled bucket renders title-cased at the end of the AI Cost tab.

    The model-id half of this test is the standing rule that a Gemini id is
    never written at a call site: the key this module holds has to resolve
    through ``rubik_core/config.py``, and a hardcoded ``"gemini-…"`` string
    would resolve to itself.
    """
    from backend.routers.admin import _FEATURE_LABELS
    from rubik_core.config import settings
    from rubik_core.priorities.agents.base import _resolve_model_id

    assert (
        _FEATURE_LABELS.get(scoring.COST_FEATURE) == "Smart Teams (Work Style Profiles)"
    )
    assert not legacy_terms(_FEATURE_LABELS[scoring.COST_FEATURE])
    assert (
        _resolve_model_id(scoring.STRENGTHS_MODEL_KEY)
        == settings.VERTEX_LLM_MODEL_FLASH
    )


def test_an_unroutable_answer_key_stops_the_run():
    """A whole-input failure, not a per-layer one.

    An answers key no section owns means the item banks moved under a stored
    interview. Scoring the remainder would write a profile built from fewer
    answers than the person gave, and nothing downstream could tell.
    """
    answers = _answers()
    answers["disc_d_99"] = 4
    with pytest.raises(scoring.ScoringError):
        scoring.score_answers(
            contracts.ScoringInput(
                user_email=SHAY,
                answers=answers,
                sections_completed=interview_catalog.SECTION_KEYS,
            ),
            extractor=_stub_extractor(_two_themes()),
        )


# ── The profile writer ───────────────────────────────────────────────────────


@pytest.fixture
def session_factory(tmp_path):
    """A throwaway SQLite database with the real schema and CHECK constraints."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from rubik_core.database.models import Base

    engine = create_engine(f"sqlite:///{tmp_path}/st.db")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def session(session_factory):
    db = session_factory()
    try:
        yield db
    finally:
        db.close()


def _interview(session, *, email: str = SHAY, completed=None, flat=None) -> str:
    from rubik_core.database.smart_teams import SmartTeamsInterview

    row = SmartTeamsInterview(
        id=uuid.uuid4().hex,
        user_email=email,
        status="in_progress",
        sections_completed=list(
            interview_catalog.SECTION_KEYS if completed is None else completed
        ),
        answers=_answers(flat=flat),
    )
    session.add(row)
    session.commit()
    return row.id


def _score(session, interview_id, **kwargs):
    from rubik_core.smart_teams import profile

    return profile.score_and_store_interview(
        session,
        interview_id,
        extractor=_stub_extractor(_two_themes()),
        **kwargs,
    )


def _versions(session, email: str = SHAY):
    from rubik_core.smart_teams import profile

    return [
        (row.version, row.status, row.is_current)
        for row in profile.profile_history(session, email)
    ]


def test_a_retake_writes_version_2_and_leaves_one_current_row(session):
    """The append-only contract, and the True-or-NULL trick that enforces it.

    ``is_current`` on the retired row must be NULL, not ``False``:
    ``uq_st_profile_one_current`` only means "one current row per person"
    because NULLs compare distinct, and a stored ``False`` inverts it into "one
    superseded row per person" — which lets the first retake through and makes
    the second one a 500.
    """
    from rubik_core.database.smart_teams import SmartTeamsProfile
    from rubik_core.smart_teams import profile

    first = _score(session, _interview(session))
    second = _score(session, _interview(session))

    assert (first.version, second.version) == (1, 2)
    assert _versions(session) == [(2, "current", True), (1, "superseded", None)]
    assert profile.current_profile(session, SHAY).version == 2
    assert session.get(SmartTeamsProfile, first.profile.id).superseded_at is not None
    # A third retake proves the retirement did not poison the constraint.
    assert _score(session, _interview(session)).version == 3


def test_a_stale_version_read_retries_instead_of_reusing_a_number(session):
    """The honest simulation of two writers computing ``max(version) + 1``.

    ``_next_version`` is a plain read, so two writers can legitimately compute
    the same number; ``uq_st_profile_user_version`` is what makes that safe and
    the retry is what makes it invisible. Forcing the first read to return the
    number a concurrent writer just took exercises exactly that path — the
    rollback has to undo the retirement too, or the retry writes a second
    current row.
    """
    from rubik_core.smart_teams import profile

    _score(session, _interview(session))  # version 1

    real = profile._next_version
    calls = {"n": 0}

    def stale(db, email):
        calls["n"] += 1
        if calls["n"] == 1:
            return real(db, email) - 1  # the number the other writer just took
        return real(db, email)

    profile._next_version = stale
    try:
        write = _score(session, _interview(session))
    finally:
        profile._next_version = real

    assert calls["n"] == 2, "the collision did not reach the retry loop"
    assert write.version == 2
    assert write.attempts == 2
    assert _versions(session) == [(2, "current", True), (1, "superseded", None)]


def test_scoring_the_same_interview_twice_is_idempotent(session):
    """A retried cubie-worker task must not double somebody's profile history."""
    interview_id = _interview(session)
    first = _score(session, interview_id)
    second = _score(session, interview_id)

    assert first.created is True
    assert second.created is False
    assert second.report is None
    assert second.profile.id == first.profile.id
    assert _versions(session) == [(1, "current", True)]


def test_losing_the_claim_race_returns_the_winner_with_no_second_version(session):
    """The check-then-act window, closed by a conditional UPDATE.

    Two workers can both read ``profile_id IS NULL`` before either commits, so
    the read is not the guarantee — the claim is. Making the idempotency read
    return ``None`` once, after another writer has already claimed the
    interview, reproduces that window exactly: the second worker scores,
    inserts, fails to claim, rolls its insert back, and hands over the winner's
    profile.
    """
    from rubik_core.smart_teams import profile

    interview_id = _interview(session)
    winner = _score(session, interview_id)

    real = profile._claimed_profile
    calls = {"n": 0}

    def stale(db, row):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(db, row)

    profile._claimed_profile = stale
    try:
        loser = _score(session, interview_id)
    finally:
        profile._claimed_profile = real

    assert calls["n"] == 2, "the claim loss did not reach the recovery path"
    assert loser.created is False
    assert loser.profile.id == winner.profile.id
    assert _versions(session) == [(1, "current", True)]


def test_an_unfinished_interview_is_refused_rather_than_half_scored(session):
    """Refusing keeps the interview re-scorable once the person finishes it.

    A version written now would claim ``profile_id`` and make this interview
    permanently un-rescorable, so the answers still to come would never reach
    the profile.
    """
    from rubik_core.database.smart_teams import SmartTeamsInterview
    from rubik_core.smart_teams import profile

    interview_id = _interview(
        session, completed=[interview_catalog.SECTION_DISC_LIKERT]
    )
    with pytest.raises(profile.ProfileError) as exc:
        _score(session, interview_id)
    assert "complete" in str(exc.value)
    assert _versions(session) == []
    assert session.get(SmartTeamsInterview, interview_id).profile_id is None


def test_a_failed_layer_still_writes_a_profile_with_the_rest_intact(session):
    """The row lands with five layers and an explicitly empty sixth."""
    from rubik_core.database.smart_teams import SmartTeamsProfile
    from rubik_core.smart_teams import profile

    def boom(_transcript, **_kwargs):
        raise RuntimeError("deadline exceeded")

    interview_id = _interview(session)
    write = profile.score_and_store_interview(session, interview_id, extractor=boom)

    assert write.created is True
    assert write.report.failed_frameworks == ("strengths",)
    row = session.get(SmartTeamsProfile, write.profile.id)
    assert row.strengths[contracts.KEY_REPORTABLE] is False
    assert row.strengths[contracts.KEY_LAYER] is None
    for framework in ("disc", "big_five", "type16", "motivators", "enneagram"):
        payload = getattr(row, framework)
        contracts.validate_envelope(payload)
        assert payload[contracts.KEY_REPORTABLE] is True, framework
    assert profile.framework_provenance(row) == {
        name: "interview" for name in contracts.FRAMEWORKS
    }


def test_a_draft_version_does_not_dethrone_the_current_one(session):
    """A dry run has to be safe to write next to somebody's live profile."""
    from rubik_core.smart_teams import profile

    live = _score(session, _interview(session))
    report = scoring.score_answers(_input(), extractor=_stub_extractor(_two_themes()))
    draft = profile.write_profile_version(session, report, status=profile.STATUS_DRAFT)

    assert draft.version == 2
    assert draft.profile.is_current is None
    assert profile.current_profile(session, SHAY).id == live.profile.id


def test_mixed_case_email_does_not_split_a_person(session):
    """``users.email`` comes off the Google token unfolded; one human, one row.

    ``ScoringInput`` refuses an unfolded address outright, so this also proves
    the writer folds before it scores rather than after.
    """
    from rubik_core.smart_teams import profile

    interview_id = _interview(session, email="Shay@ACME.example")
    write = _score(session, interview_id)

    assert write.profile.user_email == SHAY
    assert profile.current_profile(session, "SHAY@acme.example").id == (
        write.profile.id
    )
    # A retake typed the other way still lands as version 2 of the same person.
    assert _score(session, _interview(session, email="SHAY@ACME.EXAMPLE")).version == 2


def test_the_router_dispatch_entry_point_scores_by_id(session_factory, monkeypatch):
    """The hand-off ST-3 resolves by name, end to end.

    ``backend/routers/smart_teams.py`` calls
    ``scoring.score_interview(interview_id)`` with nothing but an id and no
    session, resolving it with ``getattr`` at call time. That contract is easy
    to break from this side without anything failing at import — the router
    logs the miss and the interview simply never gets a profile — so it is
    pinned here. The JSON client is stubbed rather than the extractor, so this
    also covers the default ``extract_strengths`` path without a network call.
    """
    from rubik_core.database import db as db_module
    from rubik_core.smart_teams import profile

    class _Client:
        def invoke_json(self, _prompt, *, fallback=None, attempts=1):
            return {"themes": []}

    monkeypatch.setattr(scoring, "_json_client", lambda *_a, **_k: _Client())
    monkeypatch.setattr(db_module, "SessionLocal", session_factory)

    setup = session_factory()
    interview_id = _interview(setup)
    setup.close()

    write = scoring.score_interview(interview_id)
    assert (write.created, write.version) == (True, 1)

    check = session_factory()
    try:
        assert profile.current_profile(check, SHAY).id == write.profile.id
        # An empty extraction is a refusal, not a failure: the model answered.
        assert write.report.failures == {}
        assert "strengths" not in write.report.reportable_frameworks
    finally:
        check.close()

"""The Smart Teams interview and payload contracts — the drift these prevent.

Every test here pins a shape that two modules have to agree on. That is worth
tests of its own because of what happened on the previous build of this feature:
two agents implemented against "the same" concept in parallel, one wrote a
Boolean and the other wrote a predicate expecting a string, every company-wide
guide read returned 403, and both halves passed their own tests. The failures
below are the ones that would be silent otherwise:

* An item id in the catalog that the instrument module does not know is a
  ValueError raised inside a background scoring job, hours after the person
  finished the interview, with nobody watching. The catalog is generated FROM the
  banks, so this test is really asking "did anyone re-type an id".
* A framework with no section is a profile column that is never populated and a
  guide section that is silently always absent.
* A ``next_section`` that can return a section already completed is an interview
  that never ends; one that can skip a section is a profile scored on answers
  nobody gave.
* An answer validator that drops what it does not recognise scores a shorter
  interview than the person sat through.
* A payload whose ``reportable`` gate can be a truthy string is the 403 bug
  again, in the other direction: a refused layer rendered as fact in a document
  its subject is asked to approve.

The duration test exists for a different reason: plan.md promises ~25 minutes and
the authored banks cost more than that. The number is asserted so that a change
to any item bank moves a test rather than moving a promise.
"""

from __future__ import annotations

import pytest

from rubik_core.database.smart_teams import PROFILE_FRAMEWORKS, SKILL_LEVELS
from rubik_core.identifiers import SMART_TEAMS_ASSISTANT_SPEAKER
from rubik_core.smart_teams import contracts as ct
from rubik_core.smart_teams import interview as iv
from rubik_core.smart_teams.instruments import (
    big_five,
    disc,
    enneagram,
    motivators,
    strengths,
    type16,
)

# ── Fixtures: a complete, clean sitting ───────────────────────────────────────


def _disc_likert() -> dict[str, int]:
    return {item.item_id: (4 if not item.reverse else 2) for item in disc.LIKERT_ITEMS}


def _disc_choices() -> dict[str, str]:
    return {
        pair.item_id: ("a" if index % 3 else "b")
        for index, pair in enumerate(disc.FORCED_CHOICE_PAIRS)
    }


def _big_five_answers() -> dict[str, int]:
    return {item.item_id: (4 if item.keyed > 0 else 2) for item in big_five.ITEMS}


def _complete_answers() -> dict[str, object]:
    answers: dict[str, object] = {}
    answers.update(_disc_likert())
    answers.update(_disc_choices())
    answers.update(_big_five_answers())
    answers.update({item.item_id: 4 for item in motivators.LIKERT_ITEMS})
    answers.update(
        {
            item.item_id: (5 if item.type_number == 3 else 2)
            for item in enneagram.LIKERT_ITEMS
        }
    )
    return answers


# ── The catalog's item ids are real ───────────────────────────────────────────


def test_every_catalog_item_id_exists_in_its_instrument_bank():
    """The ids in each section are exactly that instrument bank's ids.

    Compared as sets against the banks' own public tuples. A mismatch means
    somebody re-typed a list instead of deriving it, which is the drift this
    whole contract pass exists to prevent.
    """
    expected = {
        iv.SECTION_DISC_LIKERT: {item.item_id for item in disc.LIKERT_ITEMS},
        iv.SECTION_DISC_FORCED_CHOICE: {
            pair.item_id for pair in disc.FORCED_CHOICE_PAIRS
        },
        iv.SECTION_BIG_FIVE: {item.item_id for item in big_five.ITEMS},
        iv.SECTION_MOTIVATORS: {item.item_id for item in motivators.LIKERT_ITEMS},
        iv.SECTION_ENNEAGRAM: {item.item_id for item in enneagram.LIKERT_ITEMS},
    }
    for section_key, ids in expected.items():
        section = iv.SECTION_BY_KEY[section_key]
        assert set(section.item_ids) == ids, section_key
        assert len(section.item_ids) == len(ids), f"{section_key} has duplicates"

    # Sections with no items must genuinely have none, not an empty re-typing.
    for section in iv.SECTIONS:
        if section.key not in expected:
            assert section.item_ids == (), section.key


def test_instrument_modules_accept_every_id_the_catalog_claims():
    """The real proof: hand each bank exactly the catalog's ids and score.

    All four scored-item modules raise ``ValueError`` on an unknown id, so a
    clean score over the catalog's own partition is the strongest available
    statement that the ids exist. This is the test that would have caught a
    ``disc_fc_*`` pair id routed into the DISC Likert validator.
    """
    parts = iv.partition_answers(_complete_answers())

    disc_result = disc.score_disc(
        parts[iv.SECTION_DISC_LIKERT],
        parts[iv.SECTION_DISC_FORCED_CHOICE],
        elapsed_seconds=900,
    )
    assert disc_result.assignment.style is not None

    bf_result = big_five.score(parts[iv.SECTION_BIG_FIVE])
    assert bf_result.is_complete
    assert type16.derive(bf_result).is_reportable

    assert motivators.score_motivators(parts[iv.SECTION_MOTIVATORS]).answered == len(
        motivators.LIKERT_ITEMS
    )
    assert enneagram.score_enneagram(parts[iv.SECTION_ENNEAGRAM]).answered == len(
        enneagram.LIKERT_ITEMS
    )


def test_strengths_section_prompt_ids_are_the_authored_script():
    section = iv.SECTION_BY_KEY[iv.SECTION_STRENGTHS]
    assert section.prompt_ids == tuple(
        prompt.prompt_id for prompt in strengths.ELICITATION_SCRIPT
    )
    assert len(section.prompt_ids) == strengths.PROMPT_COUNT
    for prompt_id in section.prompt_ids:
        # Raises KeyError on an unknown id, never returns a default.
        assert strengths.prompt_for(prompt_id).question


# ── The catalog covers every framework ────────────────────────────────────────


def test_catalog_covers_every_profile_framework():
    """Every ``st_profiles`` JSON column has a section that feeds it.

    A framework with no section is a column that is never written and a guide
    section that is always mysteriously absent. ``type16`` is the one to watch:
    it has no items of its own and is fed by the Big Five bank.
    """
    covered = {name for section in iv.SECTIONS for name in section.feeds}
    assert covered == set(PROFILE_FRAMEWORKS)
    assert covered == set(ct.FRAMEWORKS)
    assert iv.SECTION_BY_KEY[iv.SECTION_BIG_FIVE].feeds == ("big_five", "type16")
    # The skills section deliberately feeds no profile payload — declared skills
    # are st_person_skills rows, which the Team Builder reads directly.
    assert iv.SECTION_BY_KEY[iv.SECTION_SKILLS].feeds == ()


def test_every_framework_has_a_verdict_adapter():
    """No layer may reach a guide without a publishability branch of its own."""
    for framework in ct.FRAMEWORKS:
        assert framework in ct._VERDICT_ADAPTERS
    with pytest.raises(ct.PayloadError):
        ct.verdict_for("astrology", object())


def test_scoring_order_puts_big_five_before_type16():
    assert ct.SCORING_ORDER.index("big_five") < ct.SCORING_ORDER.index("type16")


# ── next_section is total and terminating ─────────────────────────────────────


def test_next_section_walks_the_catalog_once_and_stops():
    completed: list[str] = []
    seen: list[str] = []
    for _ in range(len(iv.SECTIONS) + 5):
        section = iv.next_section(completed)
        if section is None:
            break
        assert section.key not in completed, "returned an already-completed section"
        seen.append(section.key)
        completed.append(section.key)
    assert seen == list(iv.SECTION_KEYS)
    assert iv.next_section(completed) is None
    assert iv.is_complete(completed)


def test_next_section_is_order_and_duplicate_insensitive():
    """The same progress written two ways resumes at the same place."""
    scrambled = [iv.SECTION_BIG_FIVE, iv.SECTION_DISC_LIKERT, iv.SECTION_DISC_LIKERT]
    assert iv.normalize_completed(scrambled) == (
        iv.SECTION_DISC_LIKERT,
        iv.SECTION_BIG_FIVE,
    )
    assert iv.next_section(scrambled).key == iv.SECTION_DISC_FORCED_CHOICE


def test_normalize_completed_rejects_an_unknown_section_key():
    with pytest.raises(iv.AnswerError):
        iv.normalize_completed(["disc_likert", "phrenology"])


# ── Resume ────────────────────────────────────────────────────────────────────


def test_resume_returns_the_partially_answered_section():
    """The case naive implementations break: mid-section, nothing completed."""
    completed = [iv.SECTION_DISC_LIKERT, iv.SECTION_DISC_FORCED_CHOICE]
    partial = dict(list(_big_five_answers().items())[:10])
    target = iv.resume_target(iv.SECTION_BIG_FIVE, completed)
    assert target.key == iv.SECTION_BIG_FIVE
    landing = iv.first_unanswered_item(target, partial)
    assert landing == big_five.ITEMS[10].item_id


def test_resume_ignores_a_cursor_pointing_at_a_completed_section():
    """A double submit must not re-open a finished section forever."""
    completed = [iv.SECTION_DISC_LIKERT]
    assert (
        iv.resume_target(iv.SECTION_DISC_LIKERT, completed).key
        == iv.SECTION_DISC_FORCED_CHOICE
    )


def test_resume_with_no_cursor_falls_back_to_the_first_incomplete_section():
    assert iv.resume_target(None, []).key == iv.SECTION_KEYS[0]
    assert iv.resume_target("", [iv.SECTION_DISC_LIKERT]).key == (
        iv.SECTION_DISC_FORCED_CHOICE
    )


def test_resume_rejects_a_cursor_the_catalog_does_not_know():
    with pytest.raises(iv.AnswerError):
        iv.resume_target("graphology", [])


def test_a_fully_answered_but_unsubmitted_section_is_not_complete():
    """Completion is an explicit submit, never inferred from answers.

    Inferring it means a person who filled the grid but has not pressed Next
    finds the section closed behind them.
    """
    answers = _big_five_answers()
    section = iv.SECTION_BY_KEY[iv.SECTION_BIG_FIVE]
    assert iv.first_unanswered_item(section, answers) is None
    assert iv.resume_target(iv.SECTION_BIG_FIVE, []).key == iv.SECTION_BIG_FIVE
    assert not iv.is_complete([])


def test_first_unanswered_item_treats_an_explicit_none_as_unanswered():
    answers = _big_five_answers()
    skipped = big_five.ITEMS[3].item_id
    answers[skipped] = None
    section = iv.SECTION_BY_KEY[iv.SECTION_BIG_FIVE]
    assert iv.first_unanswered_item(section, answers) == skipped


# ── The answer envelope ───────────────────────────────────────────────────────


def test_validator_accepts_a_complete_clean_sitting():
    normalized = iv.validate_answers(_complete_answers())
    assert len(normalized) == (
        len(disc.LIKERT_ITEMS)
        + len(disc.FORCED_CHOICE_PAIRS)
        + len(big_five.ITEMS)
        + len(motivators.LIKERT_ITEMS)
        + len(enneagram.LIKERT_ITEMS)
    )


def test_validator_rejects_an_unknown_item_id():
    with pytest.raises(iv.UnknownAnswerKey):
        iv.validate_answers({"disc_d_99": 3})
    with pytest.raises(iv.UnknownAnswerKey):
        iv.validate_answers({"bf_x_01": 3})
    # A near-miss on a reserved namespace is still unrouted, not a silent drop.
    with pytest.raises(iv.UnknownAnswerKey):
        iv.validate_answers({"transcript:motivators": []})


def test_validator_rejects_out_of_range_and_wrongly_typed_scale_values():
    item = disc.LIKERT_ITEMS[0].item_id
    for bad in (0, 6, -1, "4", 3.5):
        with pytest.raises(iv.AnswerValueError):
            iv.validate_answers({item: bad})
    # bool is an int subclass and True would score as a 1.
    with pytest.raises(iv.AnswerValueError):
        iv.validate_answers({item: True})
    # None is a real state: presented and left blank. Every instrument drops it.
    assert iv.validate_answers({item: None}) == {item: None}


def test_validator_normalizes_forced_choice_case_and_rejects_anything_else():
    pair = disc.FORCED_CHOICE_PAIRS[0].item_id
    assert iv.validate_answers({pair: " A "}) == {pair: "a"}
    for bad in ("c", "", 1, True, ["a"]):
        with pytest.raises(iv.AnswerValueError):
            iv.validate_answers({pair: bad})


def test_validator_checks_skill_levels_and_the_curated_taxonomy():
    assert iv.validate_answers({"skill:data_privacy": "strong"}) == {
        "skill:data_privacy": "strong"
    }
    with pytest.raises(iv.AnswerValueError):
        iv.validate_answers({"skill:data_privacy": "guru"})
    with pytest.raises(iv.AnswerValueError):
        iv.validate_answers({"skill:Data Privacy": "strong"})
    # With the taxonomy supplied, an unknown slug is refused: a declared skill
    # the Team Builder cannot match on is worse than no declaration.
    with pytest.raises(iv.AnswerValueError):
        iv.validate_answers(
            {"skill:telepathy": "expert"}, known_skill_keys={"data_privacy"}
        )
    assert set(SKILL_LEVELS) == {"familiar", "working", "strong", "expert"}


def test_validator_round_trips_a_transcript_and_refuses_a_flat_string():
    section = iv.SECTION_BY_KEY[iv.SECTION_STRENGTHS]
    turns = [
        {
            "speaker": SMART_TEAMS_ASSISTANT_SPEAKER,
            "text": "Tell me about a good week.",
            "prompt_id": None,
        },
        {
            "speaker": "interviewee",
            "text": "I rewrote the exception report and people stopped asking me.",
            "prompt_id": "str_01_energising",
        },
    ]
    out = iv.validate_answers({section.transcript_key: turns})
    assert out[section.transcript_key] == turns
    rebuilt = ct.transcript_from_json(out[section.transcript_key])
    assert len(rebuilt) == 2
    assert strengths.interviewee_turns(rebuilt)[0].prompt_id == "str_01_energising"
    # A bare string is refused for the same reason strengths._as_turns refuses
    # it: a flat transcript lets the assistant's own question pass as the
    # person's quote.
    with pytest.raises(iv.AnswerValueError):
        iv.validate_answers({section.transcript_key: "CUBIE: hello"})
    with pytest.raises(iv.AnswerValueError):
        iv.validate_answers(
            {section.transcript_key: [{"speaker": "boss", "text": "x"}]}
        )
    with pytest.raises(iv.AnswerValueError):
        iv.validate_answers(
            {
                section.transcript_key: [
                    {"speaker": SMART_TEAMS_ASSISTANT_SPEAKER, "text": "  "}
                ]
            }
        )


def test_validator_checks_the_enneagram_confirmation_object():
    key = iv.SECTION_BY_KEY[iv.SECTION_ENNEAGRAM_CONFIRMATION].meta_key
    assert iv.validate_answers({key: {"type_number": 3, "wing": 2}}) == {
        key: {"type_number": 3, "wing": 2, "declined": False}
    }
    assert iv.validate_answers({key: {"declined": True}}) == {
        key: {"type_number": None, "wing": None, "declined": True}
    }
    # A wing must neighbour its type, a decline cannot carry a type, and an
    # unknown key means the client and this contract disagree.
    for bad in (
        {"type_number": 3, "wing": 7},
        {"type_number": 3, "declined": True},
        {"wing": 2},
        {"type_number": 12},
        {"type_number": 3, "confidence": "high"},
    ):
        with pytest.raises(iv.AnswerValueError):
            iv.validate_answers({key: bad})


def test_partition_routes_the_two_disc_banks_apart():
    """The prefix trap, closed structurally rather than by comment.

    ``disc_fc_*`` shares the ``disc_`` prefix with the Likert bank, so a
    ``startswith`` split feeds pair ids into ``_validated_likert``, which raises.
    Routing is exact set membership, so the two banks land in two sections.
    """
    parts = iv.partition_answers(_complete_answers())
    likert = parts[iv.SECTION_DISC_LIKERT]
    choices = parts[iv.SECTION_DISC_FORCED_CHOICE]
    assert not any(key.startswith("disc_fc_") for key in likert)
    assert all(key.startswith("disc_fc_") for key in choices)
    assert all(isinstance(value, int) for value in likert.values())
    assert all(value in ("a", "b") for value in choices.values())
    # Every catalog section is a key, even the empty ones, so callers need no
    # .get default.
    assert set(parts) == set(iv.SECTION_KEYS)
    assert parts[iv.SECTION_SKILLS] == {}


# ── Duration ──────────────────────────────────────────────────────────────────


def test_estimated_duration_is_what_the_catalog_claims():
    """Pinned arithmetic, and an honest statement that it still misses the target.

    plan.md Decision 6 says "~25 min". The first cut of the catalog cost 33.1
    minutes on the required path. The strengths elicitation script was then cut
    from eight prompts to five, which took 225
    seconds out and brought the required path to 29.4 minutes — a real
    improvement that still does not reach the target.

    The remaining 263 seconds are ACCEPTED. The Enneagram opt-in was declined,
    so 29.4 minutes is the shipped length and ~25 is superseded.
    ``meets_target`` staying False is this test reporting the honest number
    against the original target — it is not an open task, and the arithmetic
    below is kept only so a future trim moves a test rather than a promise.

    These assertions exist so that trimming a bank moves a test rather than
    moving a promise.
    """
    report = iv.duration_report()
    per_section = report.per_section()

    assert per_section == {
        iv.SECTION_DISC_LIKERT: 168,
        iv.SECTION_DISC_FORCED_CHOICE: 216,
        iv.SECTION_BIG_FIVE: 350,
        iv.SECTION_STRENGTHS: 375,
        iv.SECTION_MOTIVATORS: 210,
        iv.SECTION_MOTIVATORS_PROBE: 300,
        iv.SECTION_ENNEAGRAM: 189,
        iv.SECTION_ENNEAGRAM_CONFIRMATION: 75,
        iv.SECTION_SKILLS: 180,
    }
    assert report.required_seconds == 1763
    assert report.total_seconds == 2063
    assert report.required_minutes == 29.4
    assert report.total_minutes == 34.4
    assert report.target_seconds == 25 * 60
    assert not report.meets_target
    assert report.over_target_seconds == 263

    # The one lever that would close the remaining gap, costed here so the claim
    # in the docstring is arithmetic rather than an assertion of good intent.
    enneagram_cost = (
        per_section[iv.SECTION_ENNEAGRAM]
        + per_section[iv.SECTION_ENNEAGRAM_CONFIRMATION]
    )
    assert enneagram_cost == 264
    assert report.required_seconds - enneagram_cost < report.target_seconds

    # The estimate is derived from the banks, not typed in beside them.
    assert per_section[iv.SECTION_BIG_FIVE] == (
        len(big_five.ITEMS) * iv.SECONDS_PER_LIKERT_ITEM
    )
    assert per_section[iv.SECTION_STRENGTHS] == (
        strengths.PROMPT_COUNT * iv.SECONDS_PER_CONVERSATIONAL_PROMPT
    )
    assert per_section[iv.SECTION_MOTIVATORS_PROBE] == (
        max(len(form) for form in motivators.PROBE_FORMS.values())
        * iv.SECONDS_PER_CONVERSATIONAL_PROMPT
    )


def test_progress_is_time_weighted_and_credits_a_partial_section():
    completed = [iv.SECTION_DISC_LIKERT, iv.SECTION_DISC_FORCED_CHOICE]
    partial = dict(list(_big_five_answers().items())[:25])
    mid = iv.progress(completed, partial, section_cursor=iv.SECTION_BIG_FIVE)
    assert mid.sections_done == 2
    assert mid.current_section_key == iv.SECTION_BIG_FIVE
    assert mid.steps_done == 24 + 24 + 25
    # Half of the Big Five bank's 350s, on top of the two finished banks.
    assert mid.seconds_done == 168 + 216 + 175
    assert 0 < mid.percent < 100
    assert not mid.is_complete

    start = iv.progress([], {})
    assert start.percent == 0
    assert start.current_section_key == iv.SECTION_KEYS[0]

    finished = iv.progress(list(iv.SECTION_KEYS), _complete_answers())
    assert finished.percent == 100
    assert finished.is_complete
    assert finished.current_section_key is None
    assert finished.seconds_remaining == 0


# ── The payload envelope ──────────────────────────────────────────────────────


def _scored_envelopes() -> dict[str, dict]:
    parts = iv.partition_answers(_complete_answers())
    bf_result = big_five.score(parts[iv.SECTION_BIG_FIVE])
    results = {
        "disc": disc.score_disc(
            parts[iv.SECTION_DISC_LIKERT],
            parts[iv.SECTION_DISC_FORCED_CHOICE],
            elapsed_seconds=900,
        ),
        "big_five": bf_result,
        "type16": type16.derive(bf_result),
        "strengths": strengths.StrengthsRead.not_taken(),
        "motivators": motivators.score_motivators(parts[iv.SECTION_MOTIVATORS]),
        "enneagram": enneagram.score_enneagram(parts[iv.SECTION_ENNEAGRAM]),
    }
    return {name: ct.envelope_for(name, result) for name, result in results.items()}


def test_every_framework_envelope_has_the_same_top_level_keys():
    """One shape for six instruments — the reason the gate cannot be missed."""
    for name, envelope in _scored_envelopes().items():
        assert set(envelope) == set(ct.ENVELOPE_KEYS), name
        assert envelope[ct.KEY_FRAMEWORK] == name
        assert envelope[ct.KEY_SCHEMA_VERSION] == ct.PAYLOAD_SCHEMA_VERSION
        assert isinstance(envelope[ct.KEY_REPORTABLE], bool)
        ct.validate_envelope(envelope)


def test_validate_envelope_rejects_a_missing_or_extra_key():
    envelope = _scored_envelopes()["disc"]
    trimmed = {k: v for k, v in envelope.items() if k != ct.KEY_REPORTABLE}
    with pytest.raises(ct.PayloadError):
        ct.validate_envelope(trimmed)
    with pytest.raises(ct.PayloadError):
        ct.validate_envelope({**envelope, "style": "D/I"})
    with pytest.raises(ct.PayloadError):
        ct.validate_envelope({**envelope, ct.KEY_SCHEMA_VERSION: "st-payload-99"})
    with pytest.raises(ct.PayloadError):
        ct.validate_envelope("not a mapping")


def test_a_truthy_string_is_not_a_reportable_gate():
    """The exact shape of the bug this contract pass exists to prevent."""
    envelope = {**_scored_envelopes()["disc"], ct.KEY_REPORTABLE: "published"}
    with pytest.raises(ct.PayloadError):
        ct.read_verdict(envelope)


def test_gate_and_confidence_must_agree():
    envelope = _scored_envelopes()["big_five"]
    with pytest.raises(ct.PayloadError):
        # Refused, but hedged as "moderate" — how a refused layer gets rendered.
        ct.read_verdict(
            {**envelope, ct.KEY_REPORTABLE: False, ct.KEY_CONFIDENCE: "moderate"}
        )
    with pytest.raises(ct.PayloadError):
        ct.read_verdict(
            {**envelope, ct.KEY_REPORTABLE: True, ct.KEY_CONFIDENCE: "none"}
        )
    with pytest.raises(ct.PayloadError):
        ct.read_verdict({**envelope, ct.KEY_REASONS: ["vibes"]})


def test_layer_contents_are_unreachable_past_a_closed_gate():
    refused = ct.envelope_for("strengths", strengths.StrengthsRead.not_taken())
    assert refused[ct.KEY_REPORTABLE] is False
    assert ct.layer_of(refused) is None
    # The layer payload is still STORED — the person's own profile page shows it.
    assert refused[ct.KEY_LAYER]["layer"] == "strengths"


def test_not_taken_envelope_is_distinguishable_from_a_null_column():
    envelope = ct.not_taken_envelope("enneagram", hedge="You skipped this section.")
    ct.validate_envelope(envelope)
    verdict = ct.read_verdict(envelope)
    assert verdict.reportable is False
    assert verdict.reasons == (ct.REASON_NOT_TAKEN,)
    assert verdict.is_actionable
    assert envelope[ct.KEY_LAYER] is None


def test_motivators_positional_top_two_is_gated_behind_the_probe():
    """``top_two`` is declaration order on a level profile, not a finding."""
    level = motivators.score_motivators(
        {item.item_id: 4 for item in motivators.LIKERT_ITEMS}
    )
    assert level.probe_reason == "level_profile"
    envelope = ct.envelope_for("motivators", level)
    assert envelope[ct.KEY_REPORTABLE] is False
    assert envelope[ct.KEY_REASONS] == [ct.REASON_FLAT_PROFILE]
    assert ct.layer_of(envelope) is None
    # The hedge is the module's own authored sentence, not prose invented here.
    assert envelope[ct.KEY_HEDGE] == level.headline()


def test_a_bad_big_five_sitting_closes_both_it_and_type16():
    """type16 is arithmetic on the same answers, so it cannot be more trusted."""
    straight_lined = big_five.score({item.item_id: 5 for item in big_five.ITEMS})
    bf_envelope = ct.envelope_for("big_five", straight_lined)
    assert bf_envelope[ct.KEY_REPORTABLE] is False
    assert bf_envelope[ct.KEY_REASONS] == [ct.REASON_QUALITY_FLAGGED]

    derived = ct.envelope_for("type16", type16.derive(straight_lined))
    assert derived[ct.KEY_REPORTABLE] is False
    assert ct.REASON_SUPPRESSED_BY_SOURCE in derived[ct.KEY_REASONS]


def test_confidence_vocabularies_from_all_three_enums_normalize():
    assert ct.normalize_confidence(type16.Confidence.CLEAR) == "high"
    assert ct.normalize_confidence(type16.Confidence.COIN_FLIP) == "low"
    assert ct.normalize_confidence(strengths.Confidence.MODERATE) == "moderate"
    assert ct.normalize_confidence("none") == "none"
    assert ct.floor_confidence("high", "low") == "low"
    with pytest.raises(ValueError):
        ct.normalize_confidence("fairly sure")


def test_scoring_output_requires_an_envelope_for_every_framework():
    envelopes = _scored_envelopes()
    outcomes = {
        name: ct.LayerOutcome(
            framework=name,
            envelope=envelope,
            verdict=ct.read_verdict(envelope),
        )
        for name, envelope in envelopes.items()
    }
    output = ct.ScoringOutput(
        user_email="bellison@acme.example",
        outcomes=outcomes,
        provenance={name: "interview" for name in ct.FRAMEWORKS},
    )
    assert set(output.as_profile_columns()) == set(PROFILE_FRAMEWORKS)
    assert "disc" in output.reportable_frameworks

    # An omitted layer is indistinguishable from a layer that failed to score.
    with pytest.raises(ct.PayloadError):
        ct.ScoringOutput(
            user_email="bellison@acme.example",
            outcomes={k: v for k, v in outcomes.items() if k != "disc"},
            provenance={},
        )


def test_scoring_input_refuses_an_unfolded_email():
    """st_profiles.user_email has a CHECK constraint requiring lower()."""
    with pytest.raises(ct.PayloadError):
        ct.ScoringInput(user_email="BEllison@acme.example")
    assert ct.ScoringInput(user_email="bellison@acme.example").answers == {}


def test_turn_round_trip_preserves_speaker_and_order():
    turns = (
        ct.Turn(speaker=ct.Speaker.ASSISTANT, text="What drained you?"),
        ct.Turn(
            speaker=ct.Speaker.INTERVIEWEE,
            text="Chasing signatures for a report nobody read.",
            prompt_id="str_02_draining",
        ),
    )
    payload = ct.transcript_to_json(turns)
    assert payload[0]["speaker"] == SMART_TEAMS_ASSISTANT_SPEAKER
    assert ct.transcript_from_json(payload) == turns
    with pytest.raises(ct.PayloadError):
        ct.transcript_from_json("CUBIE: hello")

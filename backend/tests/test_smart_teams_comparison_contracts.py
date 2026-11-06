"""The comparison + Team Builder contracts: the four gate combinations, the
refusal that cannot be read as "no friction", and the absence of any per-person
score.

These are shape tests. They touch no database and no model. Every one of them
guards a defect this build has actually shipped once: a one-sided gate rendered
as a comparison, an empty list read as an answer, and a ranking reaching a
document that had been instructed not to produce one.
"""

from __future__ import annotations

import dataclasses
from datetime import date, timedelta

import pytest

from rubik_core.smart_teams import comparison_contracts as cc
from rubik_core.smart_teams import contracts, skill_taxonomy
from rubik_core.smart_teams import team_contracts as tc
from rubik_core.smart_teams.instruments import disc, strengths

A = "emery@acme.example"
B = "shay@acme.example"


# ── helpers ───────────────────────────────────────────────────────────────────


def _verdict(framework: str, *, reportable: bool) -> contracts.LayerVerdict:
    """A minimal on-contract verdict. Built through the real dataclass so the
    gate/confidence coherence rules in ``contracts`` apply to the fixture too."""
    if reportable:
        return contracts.LayerVerdict(
            framework=framework, reportable=True, confidence="high"
        )
    return contracts.LayerVerdict(
        framework=framework,
        reportable=False,
        confidence="none",
        reasons=(contracts.REASON_DECLINED,),
        hedge="I chose to leave this one out.",
    )


def _envelope(
    framework: str,
    layer: dict | None,
    *,
    reportable: bool,
    reason: str = contracts.REASON_DECLINED,
) -> dict:
    payload = {
        contracts.KEY_SCHEMA_VERSION: contracts.PAYLOAD_SCHEMA_VERSION,
        contracts.KEY_FRAMEWORK: framework,
        contracts.KEY_REPORTABLE: reportable,
        contracts.KEY_CONFIDENCE: "high" if reportable else "none",
        contracts.KEY_REASONS: [] if reportable else [reason],
        contracts.KEY_HEDGE: None if reportable else "I left this one out.",
        contracts.KEY_INSTRUMENT_VERSION: None,
        contracts.KEY_SCORED_AT: "2026-08-20T00:00:00",
        contracts.KEY_LAYER: layer if reportable else None,
    }
    contracts.validate_envelope(payload)
    return payload


def _disc_layer(style_key: str = "D") -> dict:
    style = disc.WORK_STYLES[style_key]
    return {"layer": "disc", "assignment": {"style": style.as_dict()}}


def _evidence(email: str, *, field: str = "feedback") -> cc.Evidence:
    style = disc.WORK_STYLES["D"]
    return cc.Evidence(
        subject_email=email,
        framework="disc",
        field=field,
        text=getattr(style, field) if isinstance(getattr(style, field), str) else "x",
    )


def _read(
    framework: str,
    *,
    a_open: bool,
    b_open: bool,
    b_reason: str = contracts.REASON_DECLINED,
) -> cc.PairwiseRead:
    return cc.PairwiseRead.from_payloads(
        framework,
        A,
        _envelope(framework, _disc_layer(), reportable=a_open),
        B,
        _envelope(framework, _disc_layer(), reportable=b_open, reason=b_reason),
    )


def _all_reads(*, a_open: bool = True, b_open: bool = True) -> dict:
    return {
        framework: _read(framework, a_open=a_open, b_open=b_open)
        for framework in contracts.FRAMEWORKS
    }


# ── 1. The joint gate, all four combinations ──────────────────────────────────


@pytest.mark.parametrize(
    ("a_open", "b_open", "expected"),
    [
        (True, True, cc.GATE_BOTH_OPEN),
        (True, False, cc.GATE_ONE_REFUSED),
        (False, True, cc.GATE_ONE_REFUSED),
        (False, False, cc.GATE_NEITHER_OPEN),
    ],
)
def test_the_joint_gate_is_correct_for_all_four_combinations(a_open, b_open, expected):
    gate = cc.joint_gate(
        _verdict("disc", reportable=a_open), _verdict("disc", reportable=b_open)
    )
    assert gate == expected
    assert (gate == cc.GATE_BOTH_OPEN) == (a_open and b_open)


def test_one_refused_is_not_collapsed_into_neither_open():
    """The middle state is the whole reason this is a tri-state.

    "One of you has not finished this part" and "neither of you has" get
    different completeness prose and different next steps, so they must not be
    the same value.
    """
    one = cc.joint_gate(
        _verdict("disc", reportable=True), _verdict("disc", reportable=False)
    )
    neither = cc.joint_gate(
        _verdict("disc", reportable=False), _verdict("disc", reportable=False)
    )
    assert one != neither
    assert len(set(cc.JOINT_GATES)) == 3


def test_a_joint_gate_across_two_frameworks_is_refused():
    with pytest.raises(cc.ComparisonError):
        cc.joint_gate(
            _verdict("disc", reportable=True), _verdict("big_five", reportable=True)
        )


@pytest.mark.parametrize(
    ("a_open", "b_open"), [(True, False), (False, True), (False, False)]
)
def test_a_one_sided_read_cannot_hold_a_layer(a_open, b_open):
    """The compounding rule as a type: no layer survives a closed joint gate.

    This is the "one real value against a default" defect. With A open and B
    refused, A's real DISC style must be unreachable — otherwise the generator
    writes a comparison against B's absent one.
    """
    read = _read("disc", a_open=a_open, b_open=b_open)
    assert not read.comparable
    assert read.layer_a is None
    assert read.layer_b is None
    assert read.joint_confidence == "none"


def test_both_open_keeps_both_layers():
    read = _read("disc", a_open=True, b_open=True)
    assert read.comparable
    assert read.layer_a is not None and read.layer_b is not None


def test_attaching_a_layer_to_a_closed_joint_gate_raises():
    with pytest.raises(cc.ComparisonError, match="BOTH subjects"):
        cc.PairwiseRead(
            framework="disc",
            subject_a=A,
            subject_b=B,
            verdict_a=_verdict("disc", reportable=True),
            verdict_b=_verdict("disc", reportable=False),
            layer_a=_disc_layer(),
        )


def test_a_pairwise_read_does_not_carry_the_refusal_hedge():
    """A hedge is written for its own subject's eyes and names what the gate hid."""
    fields = {f.name for f in dataclasses.fields(cc.PairwiseRead)}
    assert "hedge" not in fields
    assert "hedge_a" not in fields and "hedge_b" not in fields


def test_closed_for_names_the_person_but_never_the_value():
    read = _read("disc", a_open=True, b_open=False)
    assert read.closed_for == (B,)
    assert read.layer_b is None


def test_comparable_frameworks_is_the_intersection_not_the_union():
    """The value that goes into ProseSlot.open_frameworks."""
    reads = {
        "disc": _read("disc", a_open=True, b_open=True),
        "big_five": _read("big_five", a_open=True, b_open=False),
        "type16": _read("type16", a_open=False, b_open=True),
        "strengths": _read("strengths", a_open=False, b_open=False),
    }
    assert cc.comparable_frameworks(reads) == frozenset({"disc"})


def test_a_missing_payload_reads_as_not_taken_rather_than_raising():
    read = cc.PairwiseRead.from_payloads("motivators", A, None, B, None)
    assert read.gate == cc.GATE_NEITHER_OPEN
    assert contracts.REASON_NOT_TAKEN in read.verdict_a.reasons


# ── 2. A refused dimension cannot be read as "no friction found" ──────────────


def _refusal() -> cc.RefusedDimension:
    return cc.RefusedDimension(
        dimension="feedback_exchange",
        gate=cc.GATE_ONE_REFUSED,
        closed_for={"disc": (B,)},
        note="One of you has not published this part, so there is nothing to compare.",
    )


def test_a_refused_dimension_has_no_frictions_attribute_at_all():
    """The safety property is the ABSENCE of the attribute.

    A renderer doing ``if not outcome.frictions: say("nothing to watch")`` gets
    an AttributeError at the mistake instead of a reassuring sentence in a
    company-visible document.
    """
    refusal = _refusal()
    assert not hasattr(refusal, "frictions")
    assert not hasattr(refusal, "synergies")
    assert not hasattr(refusal, "no_friction_found")
    with pytest.raises(AttributeError):
        refusal.frictions  # noqa: B018 — reaching for it is the tested behaviour


def test_the_two_outcome_types_do_not_share_a_findings_field():
    assessed = {f.name for f in dataclasses.fields(cc.AssessedDimension)}
    refused = {f.name for f in dataclasses.fields(cc.RefusedDimension)}
    assert "frictions" in assessed
    assert not (assessed & refused) - {"dimension"}


def test_friction_state_has_three_answers_never_two():
    empty = cc.AssessedDimension(dimension="feedback_exchange")
    assert cc.friction_state(empty) == cc.FRICTION_NONE_FOUND
    assert empty.no_friction_found is True

    assert cc.friction_state(_refusal()) == cc.FRICTION_NOT_ASSESSED
    assert cc.friction_state(empty) != cc.friction_state(_refusal())
    assert len(set(cc.FRICTION_STATES)) == 3


def test_a_refusal_cannot_claim_both_gates_were_open():
    with pytest.raises(cc.ComparisonError, match="both_open"):
        cc.RefusedDimension(
            dimension="feedback_exchange",
            gate=cc.GATE_BOTH_OPEN,
            closed_for={},
            note="x",
        )


def test_a_refusal_must_carry_a_note():
    with pytest.raises(cc.ComparisonError):
        cc.RefusedDimension(
            dimension="feedback_exchange",
            gate=cc.GATE_ONE_REFUSED,
            closed_for={"disc": (B,)},
            note="   ",
        )


def test_a_refusal_cannot_blame_a_framework_that_does_not_feed_the_dimension():
    with pytest.raises(cc.ComparisonError, match="do not feed"):
        cc.RefusedDimension(
            dimension="feedback_exchange",  # disc-only
            gate=cc.GATE_ONE_REFUSED,
            closed_for={"motivators": (B,)},
            note="x",
        )


# ── 2b. Closed and finishable are two different sets ──────────────────────────


def test_actionable_for_is_narrower_than_closed_for():
    """Both name B. Only one of them means "B can do something about it".

    ``declined`` and ``suppressed_by_source`` are outcomes, not gaps: nothing B
    does reopens them (a suppressed type16 reopens by redoing its Big Five
    SOURCE, which is a different framework's line). ``not_taken`` is a gap.
    """
    gap = _read(
        "motivators", a_open=True, b_open=False, b_reason=contracts.REASON_NOT_TAKEN
    )
    assert gap.closed_for == (B,)
    assert gap.actionable_for == (B,)

    for reason in (
        contracts.REASON_DECLINED,
        contracts.REASON_SUPPRESSED_BY_SOURCE,
        contracts.REASON_QUALITY_FLAGGED,
    ):
        outcome = _read("type16", a_open=True, b_open=False, b_reason=reason)
        assert outcome.closed_for == (B,), reason
        assert outcome.actionable_for == (), reason


def test_a_refusal_never_tells_a_subject_to_finish_what_they_cannot_finish():
    """The shipped defect, at the shape level.

    The refusals block used to print ``RefusedDimension.subjects_to_finish`` —
    every closed subject, whatever the reason — while the completeness block
    below it filtered the same names through per-subject actionability. So one
    document said "Still to finish for: B" under a dimension whose own
    provenance line, four lines later, did not list B at all.
    """
    reads = _all_reads()
    reads["enneagram"] = _read(
        "enneagram", a_open=True, b_open=False, b_reason=contracts.REASON_DECLINED
    )
    refusal = cc.RefusedDimension(
        dimension="interpersonal_needs",
        gate=cc.GATE_ONE_REFUSED,
        closed_for={"enneagram": (B,)},
        note="One of you has this part of the profile not reported.",
    )
    assert refusal.subjects_with_closed_gates == (B,)
    assert cc.subjects_to_finish(refusal, reads) == ()
    assert not hasattr(refusal, "subjects_to_finish"), (
        "the misleading name is what got printed; it must not come back as a "
        "property that answers the other question"
    )


def test_subjects_to_finish_names_a_person_once_across_several_closed_frameworks():
    """communication_pace is fed by three layers. One person, one name."""
    reads = _all_reads()
    reads["big_five"] = _read(
        "big_five",
        a_open=True,
        b_open=False,
        b_reason=contracts.REASON_INSUFFICIENT_ANSWERS,
    )
    reads["type16"] = _read(
        "type16",
        a_open=True,
        b_open=False,
        b_reason=contracts.REASON_SUPPRESSED_BY_SOURCE,
    )
    refusal = cc.RefusedDimension(
        dimension="communication_pace",
        gate=cc.GATE_ONE_REFUSED,
        closed_for={"big_five": (B,), "type16": (B,)},
        note="One of you has this part of the profile not reported.",
    )
    # Named through big_five, which B CAN finish, and not doubled by the type16
    # line, which B cannot.
    assert cc.subjects_to_finish(refusal, reads) == (B,)


def test_subjects_to_finish_skips_a_framework_missing_from_the_reads():
    """A footnote must not take down a document."""
    refusal = cc.RefusedDimension(
        dimension="feedback_exchange",
        gate=cc.GATE_ONE_REFUSED,
        closed_for={"disc": (B,)},
        note="One of you has this part of the profile not reported.",
    )
    assert cc.subjects_to_finish(refusal, {}) == ()


def test_a_pair_comparison_holds_every_dimension_in_canonical_order():
    outcomes = tuple(cc.AssessedDimension(dimension=key) for key in cc.DIMENSION_KEYS)
    comparison = cc.PairComparison(
        subject_a=A, subject_b=B, reads=_all_reads(), outcomes=outcomes
    )
    assert tuple(o.dimension for o in comparison.outcomes) == cc.DIMENSION_KEYS
    assert comparison.document_floor_met is False

    with pytest.raises(cc.ComparisonError, match="canonical order"):
        cc.PairComparison(
            subject_a=A, subject_b=B, reads=_all_reads(), outcomes=outcomes[:-1]
        )


def test_a_pair_comparison_needs_a_read_for_every_framework():
    outcomes = tuple(cc.AssessedDimension(dimension=key) for key in cc.DIMENSION_KEYS)
    reads = _all_reads()
    reads.pop("enneagram")
    with pytest.raises(cc.ComparisonError, match="missing frameworks"):
        cc.PairComparison(subject_a=A, subject_b=B, reads=reads, outcomes=outcomes)


# ── 3. Claims: the friction bar, both asks, and no severity ──────────────────


def test_a_friction_is_refused_below_the_moderate_bar_where_a_synergy_is_not():
    kwargs = dict(
        dimension="feedback_exchange",
        subjects=(A, B),
        statement="You each ask for feedback differently.",
        evidence=(_evidence(A), _evidence(B, field="under_stress")),
    )
    cc.Synergy(confidence="low", **kwargs)  # allowed
    with pytest.raises(cc.ComparisonError, match="below the friction floor"):
        cc.Friction(confidence="low", ask_of_a="a", ask_of_b="b", **kwargs)
    cc.Friction(confidence="moderate", ask_of_a="a", ask_of_b="b", **kwargs)


def test_the_friction_bar_is_strictly_stronger_than_the_synergy_bar():
    assert cc.MIN_FRICTION_CONFIDENCE != cc.MIN_SYNERGY_CONFIDENCE
    assert (
        contracts.floor_confidence(
            cc.MIN_FRICTION_CONFIDENCE, cc.MIN_SYNERGY_CONFIDENCE
        )
        == cc.MIN_SYNERGY_CONFIDENCE
    )


@pytest.mark.parametrize(("ask_a", "ask_b"), [("", "b"), ("a", ""), ("", "")])
def test_a_friction_with_a_one_sided_ask_is_malformed(ask_a, ask_b):
    with pytest.raises(cc.ComparisonError, match="ask of BOTH"):
        cc.Friction(
            dimension="feedback_exchange",
            subjects=(A, B),
            statement="x",
            confidence="high",
            evidence=(_evidence(A), _evidence(B, field="under_stress")),
            ask_of_a=ask_a,
            ask_of_b=ask_b,
        )


def test_a_friction_carries_no_severity_rank_or_order():
    """Ranking one tension above the others reads as assigning blame."""
    fields = {f.name for f in dataclasses.fields(cc.Friction)}
    assert not fields & {"severity", "rank", "order", "priority", "biggest_risk"}


def test_a_claim_needs_evidence_from_both_subjects():
    with pytest.raises(cc.ComparisonError, match="no evidence cited"):
        cc.Synergy(
            dimension="feedback_exchange",
            subjects=(A, B),
            statement="x",
            confidence="high",
            evidence=(_evidence(A),),
        )


def test_a_friction_needs_two_distinct_field_operands():
    with pytest.raises(cc.ComparisonError, match="two distinct"):
        cc.Friction(
            dimension="feedback_exchange",
            subjects=(A, B),
            statement="x",
            confidence="high",
            # Same (framework, field) on both sides: one field cannot collide
            # with itself.
            evidence=(_evidence(A), _evidence(B)),
            ask_of_a="a",
            ask_of_b="b",
        )


def test_evidence_cannot_carry_a_raw_score():
    with pytest.raises(cc.ComparisonError, match="raw value"):
        cc.Evidence(subject_email=A, framework="disc", field="intensity", text="72")


def test_evidence_cannot_cite_a_field_the_instrument_does_not_have():
    with pytest.raises(cc.ComparisonError, match="not a field"):
        cc.Evidence(
            subject_email=A, framework="disc", field="leadership_potential", text="x"
        )


# ── 4. The dimension vocabulary comes from the instruments ────────────────────


def test_every_dimension_source_names_real_instrument_fields():
    """The import-time check, restated as a test so the reason is recorded."""
    for dimension in cc.DIMENSIONS:
        for source in dimension.sources:
            known = cc._INSTRUMENT_FIELD_NAMES[source.framework]
            for field in (*source.layer_fields, *source.spec_fields):
                assert field in known, f"{dimension.key}: {source.framework}.{field}"


def test_every_framework_feeds_at_least_one_dimension():
    sourced = {source.framework for d in cc.DIMENSIONS for source in d.sources}
    assert sourced == set(contracts.FRAMEWORKS)


def test_no_dimension_may_name_a_derived_type():
    assert all(not d.may_name_derived_type for d in cc.DIMENSIONS)
    with pytest.raises(cc.ComparisonError, match="derived type"):
        cc.Dimension(
            key="communication_pace",
            label="x",
            question="x",
            sources=(cc.DimensionSource(framework="disc", layer_fields=("feedback",)),),
            may_name_derived_type=True,
        )


def test_a_dimension_is_open_only_when_a_source_is_comparable_for_both():
    dimension = cc.dimension_for("feedback_exchange")  # disc only
    assert dimension.is_open(_all_reads(a_open=True, b_open=True))
    assert not dimension.is_open(_all_reads(a_open=True, b_open=False))


# ── 5. The no-ranking screen ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("Shay is better at this than Emery.", cc.REJECT_CROSS_SUBJECT_RANKING),
        (
            "Emery is the stronger communicator of the two.",
            cc.REJECT_CROSS_SUBJECT_RANKING,
        ),
        (
            "She is the most capable person on the team.",
            cc.REJECT_CROSS_SUBJECT_RANKING,
        ),
        ("He outperforms her on delivery.", cc.REJECT_CROSS_SUBJECT_RANKING),
        ("Let me rank the candidates for you.", cc.REJECT_CROSS_SUBJECT_RANKING),
        (
            "Your styles are approximately 75% synergy and 25% blind spots.",
            cc.REJECT_COMPATIBILITY_SCORE,
        ),
        ("Their compatibility score is high.", cc.REJECT_COMPATIBILITY_SCORE),
        # Shapes the wave-2 verifier measured walking straight through this
        # screen. A superlative scoped by a COUNT rather than by a collective
        # noun ("of the group" was always caught, "of the three" was not), a
        # leadership designation, and a named weak member. They matter because
        # this is the ONE screen both documents share: a miss here is a sentence
        # a reader sees in the pair guide, where nothing else is behind it.
        ("Dakota is the strongest of the three.", cc.REJECT_CROSS_SUBJECT_RANKING),
        (
            "Dakota is the most experienced of the three.",
            cc.REJECT_CROSS_SUBJECT_RANKING,
        ),
        ("Dakota is the obvious choice to lead.", cc.REJECT_CROSS_SUBJECT_RANKING),
        ("Dakota should lead the team.", cc.REJECT_CROSS_SUBJECT_RANKING),
        ("Dakota is the weakest link.", cc.REJECT_CROSS_SUBJECT_RANKING),
    ],
)
def test_the_no_ranking_screen_refuses_cross_subject_comparison(text, code):
    assert cc.screen_no_ranking(text) == code


@pytest.mark.parametrize(
    "text",
    [
        "Your best approach with him is to lead with the headline.",
        "Give him the answer first, then the supporting evidence.",
        "You each want a different amount of detail before you commit.",
        "Neither of you wants to sit on a decision.",
        "He works better with a day of notice before a change.",
    ],
)
def test_the_no_ranking_screen_permits_legitimate_pair_advice(text):
    assert cc.screen_no_ranking(text) is None


def test_the_generic_self_superlative_screen_would_have_missed_these():
    """Why this screen exists at all, pinned so nobody deletes it as redundant."""
    from rubik_core.smart_teams import guide_prompts

    text = "Shay is better at closing this out than Emery."
    assert guide_prompts.screen_prose(text) is None
    assert cc.screen_no_ranking(text) == cc.REJECT_CROSS_SUBJECT_RANKING


def test_the_pair_advisory_notice_reuses_the_instrument_copy_and_adds_the_gap():
    assert disc.ADVISORY_NOTICE in cc.PAIR_ADVISORY_NOTICE
    assert cc.NO_OBSERVATION_NOTICE in cc.PAIR_ADVISORY_NOTICE


# ── 6. Team Builder: no per-person score, structurally ───────────────────────

_TEAM_TYPES = (
    tc.TeamRequest,
    tc.CoveredSkill,
    tc.TeamCandidate,
    tc.SkillCoverage,
    tc.StyleBalance,
    tc.Shortfall,
    tc.TeamResult,
)


@pytest.mark.parametrize("cls", _TEAM_TYPES, ids=lambda c: c.__name__)
def test_no_team_type_has_a_field_capable_of_carrying_a_per_person_score(cls):
    """Adding ``fit_score`` later must fail HERE, not in a code review.

    The assertion is on the dataclass fields rather than on a rendered string,
    so the test fails at the moment the field is introduced — which is the only
    point at which the fix is cheap.
    """
    for field in dataclasses.fields(cls):
        assert tc._PERSON_METRIC_WORDS.search(field.name) is None, (
            f"{cls.__name__}.{field.name} is score-shaped"
        )


def test_the_score_guard_actually_fires_on_a_score_field():
    """Prove the guard is not vacuous — it has caught nothing so far by design."""
    with pytest.raises(tc.TeamError, match="score-shaped"):

        @tc._forbid_person_metrics
        @dataclasses.dataclass(frozen=True)
        class _Leaderboard:
            user_email: str
            fit_score: float


@pytest.mark.parametrize(
    "name",
    ["score", "fit_score", "match_percent", "rank", "rating", "workload", "capacity"],
)
def test_the_score_guard_covers_the_whole_forbidden_vocabulary(name):
    assert tc._PERSON_METRIC_WORDS.search(name) is not None


def test_a_candidate_carries_sets_and_states_but_no_number_about_the_person():
    candidate = tc.TeamCandidate(
        user_email=A,
        display_name="Emery",
        covers=(
            tc.CoveredSkill(
                skill_key=next(iter(skill_taxonomy.SKILLS)),
                level="strong",
                origin="declared",
            ),
        ),
        availability=tc.AVAILABILITY_PARTIAL,
        back_on=date(2026, 9, 14),
        work_style_key="D",
        clusters=frozenset({strengths.Cluster.MOMENTUM.value}),
    )
    numeric = {
        f.name
        for f in dataclasses.fields(candidate)
        if f.type in {"int", "float", "int | None", "float | None"}
    }
    assert numeric == set(), f"numeric per-person fields: {numeric}"


def test_a_roster_must_be_alphabetical_so_its_order_means_nothing():
    request = tc.TeamRequest(requested_by=A)
    good = (
        tc.TeamCandidate(user_email=A, display_name="Emery"),
        tc.TeamCandidate(user_email=B, display_name="Shay"),
    )
    tc.TeamResult(request=request, candidates=good)
    with pytest.raises(tc.TeamError, match="ranking of people"):
        tc.TeamResult(request=request, candidates=tuple(reversed(good)))


def test_covered_by_must_be_alphabetical_too():
    with pytest.raises(tc.TeamError, match="ranking of the"):
        tc.SkillCoverage(
            skill_key=next(iter(skill_taxonomy.SKILLS)),
            phrase="x",
            required=True,
            covered_by=(B, A),
        )


def test_style_balance_is_a_group_statement_with_no_per_person_breakdown():
    fields = {f.name for f in dataclasses.fields(tc.StyleBalance)}
    assert not fields & {"by_person", "per_person", "styles_by_email", "candidates"}
    balance = tc.StyleBalance(
        clusters_covered=frozenset({strengths.Cluster.MOMENTUM.value}),
        clusters_missing=frozenset({strengths.Cluster.STEWARDSHIP.value}),
        styles_present=("D", "D+I"),
    )
    notes = balance.cluster_notes
    assert set(notes) == {strengths.Cluster.STEWARDSHIP.value}
    assert (
        notes[strengths.Cluster.STEWARDSHIP.value]
        == (strengths.CLUSTER_MEANING[strengths.Cluster.STEWARDSHIP])
    )


def test_there_is_no_workload_or_utilization_field_anywhere():
    for cls in _TEAM_TYPES:
        names = {f.name for f in dataclasses.fields(cls)}
        assert not names & {
            "workload",
            "utilization",
            "capacity",
            "allocation",
            "hours",
        }


# ── 7. Team Builder: an unresolvable skill is reported, never dropped ─────────


def test_an_unresolvable_skill_phrase_is_reported_not_coerced():
    known = next(iter(skill_taxonomy.SKILLS))
    known_phrase = skill_taxonomy.SKILLS[known].name
    asks, unresolved = tc.resolve_skill_phrases([known_phrase, "quantum invoice yoga"])
    assert [ask.skill_key for ask in asks] == [known]
    assert [u.phrase for u in unresolved] == ["quantum invoice yoga"]


def test_a_request_keeps_the_unresolved_phrase_on_the_record():
    request = tc.TeamRequest.from_phrases(
        requested_by=A, required_phrases=["quantum invoice yoga"]
    )
    assert request.skills == ()
    assert [u.phrase for u in request.unresolved_skills] == ["quantum invoice yoga"]


def test_a_result_cannot_silently_omit_an_unresolved_skill():
    request = tc.TeamRequest.from_phrases(
        requested_by=A, required_phrases=["quantum invoice yoga"]
    )
    with pytest.raises(tc.TeamError, match="never dropped from the filter"):
        tc.TeamResult(
            request=request,
            candidates=(tc.TeamCandidate(user_email=A, display_name="Emery"),),
        )
    ok = tc.TeamResult(
        request=request,
        candidates=(tc.TeamCandidate(user_email=A, display_name="Emery"),),
        shortfalls=(
            tc.Shortfall(
                code=tc.SHORTFALL_SKILL_NOT_IN_TAXONOMY,
                phrase="quantum invoice yoga",
                detail=(
                    "Cubie does not track a skill by that name. Say so and offer "
                    "the closest names it does track; do not state that nobody "
                    "has this skill."
                ),
            ),
        ),
    )
    assert ok.presentation_state == tc.STATE_PARTIAL


def test_a_skill_ask_cannot_be_built_on_a_guessed_key():
    with pytest.raises(tc.TeamError, match="not in the ST-8 taxonomy"):
        tc.SkillAsk(skill_key="quantum_invoice_yoga", phrase="quantum invoice yoga")


# ── 8. Team Builder: an unsatisfiable ask is a legitimate answer ──────────────


def _ask(skill_key: str) -> tc.TeamRequest:
    return tc.TeamRequest(
        requested_by=A,
        skills=(tc.SkillAsk(skill_key=skill_key, phrase="the ask", required=True),),
    )


def test_an_uncovered_required_skill_must_carry_a_shortfall():
    key = next(iter(skill_taxonomy.SKILLS))
    request = _ask(key)
    coverage = (tc.SkillCoverage(skill_key=key, phrase="the ask", required=True),)
    with pytest.raises(tc.TeamError, match="carries no shortfall"):
        tc.TeamResult(
            request=request,
            candidates=(tc.TeamCandidate(user_email=A, display_name="Emery"),),
            coverage=coverage,
        )
    result = tc.TeamResult(
        request=request,
        candidates=(tc.TeamCandidate(user_email=A, display_name="Emery"),),
        coverage=coverage,
        shortfalls=(
            tc.Shortfall(
                code=tc.SHORTFALL_NO_ONE_WITH_SKILL,
                skill_key=key,
                detail=(
                    "Nobody has confirmed this skill in Smart Teams. That is not "
                    "the same as nobody at the company having it — do not say that."
                ),
            ),
        ),
    )
    assert result.presentation_state == tc.STATE_PARTIAL
    assert result.is_complete is False
    assert result.uncovered_required == (key,)


def test_nobody_has_published_a_profile_means_no_roster():
    request = tc.TeamRequest(requested_by=A)
    shortfall = tc.Shortfall(
        code=tc.SHORTFALL_NO_PUBLISHED_PROFILES,
        detail="Nobody in scope has published a work-style profile yet.",
    )
    empty = tc.TeamResult(request=request, shortfalls=(shortfall,))
    assert empty.presentation_state == tc.STATE_EMPTY
    with pytest.raises(tc.TeamError, match="roster must be empty"):
        tc.TeamResult(
            request=request,
            candidates=(tc.TeamCandidate(user_email=A, display_name="Emery"),),
            shortfalls=(shortfall,),
        )


def test_a_result_with_any_shortfall_cannot_present_as_complete():
    key = next(iter(skill_taxonomy.SKILLS))
    request = _ask(key)
    result = tc.TeamResult(
        request=request,
        candidates=(tc.TeamCandidate(user_email=A, display_name="Emery"),),
        coverage=(
            tc.SkillCoverage(
                skill_key=key, phrase="the ask", required=True, covered_by=(A,)
            ),
        ),
        shortfalls=(
            tc.Shortfall(
                code=tc.SHORTFALL_SIZE_NOT_MET,
                detail="You asked for three people and Cubie could only propose one.",
            ),
        ),
    )
    assert result.presentation_state == tc.STATE_PARTIAL


def test_a_complete_result_is_reachable():
    key = next(iter(skill_taxonomy.SKILLS))
    request = tc.TeamRequest(
        requested_by=A,
        skills=(tc.SkillAsk(skill_key=key, phrase="the ask"),),
        size=1,
    )
    result = tc.TeamResult(
        request=request,
        candidates=(tc.TeamCandidate(user_email=A, display_name="Emery"),),
        coverage=(
            tc.SkillCoverage(
                skill_key=key, phrase="the ask", required=True, covered_by=(A,)
            ),
        ),
    )
    assert result.presentation_state == tc.STATE_COMPLETE


def test_a_window_past_the_availability_horizon_must_say_so():
    today = date(2026, 8, 20)
    request = tc.TeamRequest(
        requested_by=A,
        starts_on=today,
        ends_on=today + timedelta(days=tc.AVAILABILITY_HORIZON_DAYS + 30),
    )
    assert request.window_beyond_horizon(today=today) is True
    shortfall = tc.Shortfall(
        code=tc.SHORTFALL_WINDOW_BEYOND_HORIZON, detail=tc.HORIZON_NOTE
    )
    with pytest.raises(tc.TeamError, match="unchecked weeks"):
        tc.TeamResult(request=request, shortfalls=(shortfall,))
    tc.TeamResult(
        request=request, shortfalls=(shortfall,), horizon_note=tc.HORIZON_NOTE
    )


def test_unknown_availability_is_not_available():
    assert tc.AVAILABILITY_UNKNOWN != tc.AVAILABILITY_AVAILABLE
    assert len(set(tc.AVAILABILITY_STATES)) == 4
    assert tc.AVAILABILITY_HORIZON_DAYS > 0


def test_a_size_is_never_invented():
    assert tc.TeamRequest(requested_by=A).size is None
    with pytest.raises(tc.TeamError):
        tc.TeamRequest(requested_by=A, size=tc.MAX_TEAM_SIZE + 1)


def test_the_default_style_mix_is_unspecified():
    assert tc.TeamRequest(requested_by=A).style_mix == tc.STYLE_MIX_UNSPECIFIED


def test_the_team_notice_reuses_the_instrument_copy_and_adds_the_no_ranking_line():
    assert disc.ADVISORY_NOTICE in tc.TEAM_ADVISORY_NOTICE
    assert tc.NO_RANKING_NOTICE in tc.TEAM_ADVISORY_NOTICE


def test_the_team_prose_screen_is_the_same_net_as_the_pair_guides():
    text = "Emery is the best person for this."
    assert tc.screen_team_prose(text) == cc.screen_no_ranking(text)
    assert tc.screen_team_prose(text) == cc.REJECT_CROSS_SUBJECT_RANKING

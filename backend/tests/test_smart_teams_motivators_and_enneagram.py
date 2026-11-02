"""What the motivators and Enneagram layers are allowed to say about a person.

Both layers are advisory, thin, and read by the person they describe. So the
tests here are not about arithmetic — they are about claims. Every one of them
pins a sentence the product is *not* allowed to produce:

* **The probe must not state a ranking the answers never produced.** When
  somebody answers all 30 motivator items at the midpoint, every axis scores
  50.0 and the "ranking" is :data:`motivators.MOTIVATORS` declaration order.
  The first cut of the confirmation probe rendered one fixed set of confident
  templates for everybody, so every flat responder in the company was told
  "your answers put Practical Return and Understanding at the top of what drives
  you at work" and then asked to account for a bottom-ranked value they had
  never expressed. The object already carried ``probe_reason``; nothing read it.
  The tests below hold the probe's wording to what each result can support, form
  by form, and separately hold the bottom-of-ranking question — which fails on
  its own, on results whose top two are fifty points apart.

* **Enneagram labels must not collide across layers.** A personal guide prints
  the DISC-style name, the strength themes, and the Enneagram type on one page,
  and a reader has no way to tell which layer a repeated name belongs to. Types
  1, 3 and 9 originally shipped as The Standard-Setter, The Front-Runner and The
  Steady Hand against DISC styles called Standard Setter, Front Runner and
  Steady Hand. Nothing inside ``enneagram.py`` can see the other two modules, so
  this file is where that guarantee actually lives — and it normalises before
  comparing, because "The Standard-Setter" and "Standard Setter" are the same
  name to a reader even though the strings differ.

* **The Enneagram must be willing to say it does not know.** A narrow top-two
  margin has to produce two named candidates and a question, never a pick, and a
  declined layer has to be a complete profile rather than a hole every renderer
  has to remember. Those are the constraints that make shipping a
  weakly-evidenced framework defensible at all, so they are tested rather than
  documented.

No test here asserts a profile predicts performance, fit, or readiness, because
nothing in these modules may claim that.
"""

from __future__ import annotations

import pytest

from rubik_core.smart_teams.instruments import disc, enneagram, motivators, strengths

# ── Response-set builders ─────────────────────────────────────────────────────
# Both banks are keyed by item id, and the motivators bank has reverse-keyed
# items, so "answer every item as if this axis were a 5" means flipping the
# reverse ones on the way in. Building the raw response that way keeps the tests
# written in terms of intent ("economic high, traditional low") rather than in
# terms of keying.


def _motivator_responses(
    levels: dict[motivators.Motivator, int],
    *,
    skip: tuple[motivators.Motivator, ...] = (),
    keep_when_skipped: int = 1,
) -> dict[str, int]:
    """Answers that score every axis at its requested 1-5 level.

    ``skip`` leaves all but ``keep_when_skipped`` items unanswered on those axes,
    which is how an ``is_complete = False`` result gets built.
    """
    responses: dict[str, int] = {}
    seen: dict[motivators.Motivator, int] = dict.fromkeys(levels, 0)
    for item in motivators.LIKERT_ITEMS:
        seen[item.motivator] += 1
        if item.motivator in skip and seen[item.motivator] > keep_when_skipped:
            continue
        level = levels[item.motivator]
        responses[item.item_id] = (
            motivators.LIKERT_MIN + motivators.LIKERT_MAX - level
            if item.reverse
            else level
        )
    return responses


def _flat_motivators(value: int) -> dict[str, int]:
    """The same raw answer on all 30 items — the answer set that started this."""
    return {item.item_id: value for item in motivators.LIKERT_ITEMS}


def _enneagram_responses(high_types: tuple[int, ...], *, high: int = 5, low: int = 1):
    return {
        item.item_id: high if item.type_number in high_types else low
        for item in enneagram.LIKERT_ITEMS
    }


def _enneagram_per_item(levels: dict[int, tuple[int, int, int]]) -> dict[str, int]:
    """One answer per item, per type, for the cases that need a chosen margin.

    Emphasis is centred on the person's own mean and rescaled, so one scale point
    on one item of three moves a type by about 4.2 points. Getting a *moderate*
    read therefore means placing single answers, not whole types.
    """
    responses: dict[str, int] = {}
    for number in enneagram.TYPE_ORDER:
        values = levels.get(number, (1, 1, 1))
        for item, value in zip(enneagram.items_for(number), values, strict=True):
            responses[item.item_id] = value
    return responses


#: Every phrase in the probe bank that asserts a position in the ranking. A
#: result that has not earned one must produce none of them.
_RANKING_CLAIMS = (
    "at the top of what drives you",
    "lean towards",
    "last.",
    "at the bottom",
    "belong in second place",
)


def _all_probe_text(result: motivators.MotivatorResult) -> str:
    return "\n".join(motivators.probe_for(result))


# ── The probe must not invent a ranking ───────────────────────────────────────
@pytest.mark.parametrize("value", [1, 3, 5])
def test_uniform_answers_produce_a_probe_that_claims_nothing(value: int) -> None:
    """The defect this file exists for.

    Answering every item identically — midpoint, all-disagree, or all-agree —
    leaves the six axes level, so the ranking is declaration order. The probe
    must name no orientation and must assert no position: not a top, not a
    bottom, not a pair.
    """
    result = motivators.score_motivators(_flat_motivators(value))

    assert result.is_level is True
    assert result.probe_reason == "level_profile"
    assert motivators.probe_form_name(result) == "open"

    text = _all_probe_text(result)
    for name in motivators.MOTIVATOR_NAMES.values():
        assert name not in text, f"{name!r} named on a level profile"
    for claim in _RANKING_CLAIMS:
        assert claim not in text, f"{claim!r} asserted on a level profile"

    # And the headline it sits next to refuses the ranking too, so the interview
    # and the guide cannot disagree about whether there is a finding.
    assert "clearly leads" in result.headline()


def test_incomplete_result_also_gets_the_open_form() -> None:
    """An unanswered axis defaults to the midpoint, so it can land anywhere in
    the order. Naming a top two off a ranking that contains a placeholder is the
    same fabrication as naming one off a tie."""
    result = motivators.score_motivators(
        _motivator_responses(
            {
                "economic": 5,
                "theoretical": 4,
                "social": 3,
                "individualistic": 2,
                "traditional": 1,
                "aesthetic": 1,
            },
            skip=("social",),
        )
    )

    assert result.is_complete is False
    assert result.probe_reason == "incomplete"
    assert motivators.probe_form_name(result) == "open"

    text = _all_probe_text(result)
    for name in motivators.MOTIVATOR_NAMES.values():
        assert name not in text


def test_confident_wording_only_when_nothing_needs_hedging() -> None:
    """The confident templates are still the right thing to say when the scores
    support them — the fix was to gate them, not to remove them."""
    result = motivators.score_motivators(
        _motivator_responses(
            {
                "economic": 5,
                "theoretical": 4,
                "social": 3,
                "individualistic": 2,
                "traditional": 1,
                "aesthetic": 2,
            }
        )
    )

    assert result.probe_reason is None
    assert motivators.probe_form_name(result) == "confident"

    text = _all_probe_text(result)
    assert "Practical Return and Understanding at the top of what drives you" in text
    assert "Guiding Framework last" in text


def test_unordered_top_two_names_the_pair_but_no_first_place() -> None:
    result = motivators.score_motivators(
        _motivator_responses(
            {
                "economic": 5,
                "theoretical": 5,
                "social": 3,
                "individualistic": 2,
                "traditional": 1,
                "aesthetic": 4,
            }
        )
    )

    assert result.probe_reason == "top_two_unordered"
    assert motivators.probe_form_name(result) == "pair_unordered"

    text = _all_probe_text(result)
    assert "Practical Return and Understanding" in text
    assert "at the top of what drives you" not in text
    assert "neither one is clearly ahead" in text


def test_contested_runner_up_names_the_leader_only() -> None:
    result = motivators.score_motivators(
        _motivator_responses(
            {
                "economic": 5,
                "theoretical": 4,
                "social": 4,
                "individualistic": 2,
                "traditional": 1,
                "aesthetic": 3,
            }
        )
    )

    assert result.probe_reason == "runner_up_contested"
    assert motivators.probe_form_name(result) == "second_place_hedged"

    text = _all_probe_text(result)
    assert "Practical Return at the top of what drives you" in text
    assert "cannot separate Understanding from what comes next" in text


def test_level_bottom_hedges_the_bottom_question_and_the_headline() -> None:
    """The bottom of the ranking fails independently of the top.

    Practical Return leads by fifty points here, so ``probe_reason`` is ``None``
    and the confident form is correct about the top — while the last two places
    are identical, which makes "Form and Craft last" tiebreak order presented as
    a fact about the person.
    """
    result = motivators.score_motivators(
        _motivator_responses(
            {
                "economic": 5,
                "theoretical": 4,
                "social": 3,
                "individualistic": 2,
                "traditional": 1,
                "aesthetic": 1,
            }
        )
    )

    assert result.probe_reason is None
    assert result.bottom_is_contested is True

    text = _all_probe_text(result)
    assert "level with the one above it" in text
    assert "Form and Craft last" not in text
    assert "Guiding Framework last" not in text

    headline = result.headline()
    assert "Guiding Framework and Form and Craft move them least" in headline


def test_every_probe_reason_code_has_wording_of_its_own() -> None:
    """A new ``probe_reason`` that inherited the confident wording is exactly how
    the original defect would come back, so the mapping is exhaustive by
    assertion here and raises rather than defaults in the module."""
    assert set(motivators._FORM_FOR_PROBE_REASON) == {
        "incomplete",
        "level_profile",
        "top_two_unordered",
        "runner_up_contested",
    }
    assert set(motivators.PROBE_FORMS) == {
        "confident",
        "pair_unordered",
        "second_place_hedged",
        "open",
    }


def test_probe_question_ids_are_stable_across_forms() -> None:
    """Answers are stored by question id, so a form may reword a question but
    never renumber it — otherwise a stored answer means something different
    depending on which wording the person happened to get."""
    expected = ("mot_probe_01", "mot_probe_02", "mot_probe_03", "mot_probe_04")
    for name, questions in motivators.PROBE_FORMS.items():
        assert tuple(q.question_id for q in questions) == expected, name


def test_unknown_motivator_item_id_raises() -> None:
    with pytest.raises(ValueError, match="unknown motivators item id"):
        motivators.score_motivators({"mot_not_a_real_item": 3})


def test_motivator_bank_structure_matches_what_the_doc_claims() -> None:
    """The doc states these as measurements ("adjacent-axis repeats 0, longest
    keying run 3"), so they need something keeping them measured. Scoring does
    not read presentation order, which is why nothing else would catch a
    re-sort — and a run-length check, when this bank gets one, will only mean
    something on the order the respondent actually saw."""
    assert len(motivators.LIKERT_ITEMS) == 30
    for motivator in motivators.MOTIVATORS:
        items = motivators.items_for(motivator)
        assert len(items) == 5
        assert sum(1 for item in items if item.reverse) == 2

    axes = [item.motivator for item in motivators.LIKERT_ITEMS]
    assert not any(a == b for a, b in zip(axes, axes[1:])), "adjacent axis repeat"

    longest, run = 1, 1
    keyings = [item.keying for item in motivators.LIKERT_ITEMS]
    for previous, current in zip(keyings, keyings[1:]):
        run = run + 1 if previous == current else 1
        longest = max(longest, run)
    assert longest == 3


def test_enneagram_bank_structure_matches_what_the_doc_claims() -> None:
    assert len(enneagram.LIKERT_ITEMS) == 27
    for number in enneagram.TYPE_ORDER:
        assert len(enneagram.items_for(number)) == 3

    passes = [item.type_number for item in enneagram.LIKERT_ITEMS]
    assert passes[0:9] == list(enneagram.TYPE_ORDER)
    assert passes[9:18] == [5, 9, 4, 8, 3, 7, 2, 6, 1]
    assert passes[18:27] == [6, 2, 7, 3, 8, 4, 9, 5, 1]
    assert not any(a == b for a, b in zip(passes, passes[1:]))

    # No keying field at all, and that is the design: a reverse item on a
    # typology would have to mean "not a 4", which is eight things at once.
    assert not hasattr(enneagram.LIKERT_ITEMS[0], "keying")


# ── Enneagram labels must be readable next to the other layers ────────────────
def _normalised(name: str) -> str:
    """How a reader sees a label: no leading article, no hyphens, no case.

    The original collision was "The Standard-Setter" against "Standard Setter" —
    two different strings and one name, which is why comparing raw labels would
    have passed while the guide still read as if the same layer appeared twice.
    """
    stem = name.strip()
    if stem.lower().startswith("the "):
        stem = stem[4:]
    return stem.replace("-", " ").casefold()


def test_enneagram_labels_collide_with_no_other_layer() -> None:
    """The guarantee ``enneagram.py``'s ``TypeSpec`` docstring points at.

    Scoped to the Enneagram layer on purpose: this file owns that layer's labels,
    so a failure here names the module that has to change. Whether ``disc.py``
    and ``strengths.py`` collide with *each other* is the same class of problem
    and belongs to a check that covers all the instruments together.
    """
    enneagram_labels = {
        _normalised(spec.label): spec.label for spec in enneagram.TYPES.values()
    }
    assert len(enneagram_labels) == 9, "two Enneagram labels normalise the same"

    other_layers = {
        _normalised(style.name): ("disc", style.name)
        for style in disc.WORK_STYLES.values()
    }
    other_layers.update(
        {
            _normalised(theme.name): ("strengths", theme.name)
            for theme in strengths.THEMES.values()
        }
    )

    clashes = [
        (label, other_layers[key])
        for key, label in enneagram_labels.items()
        if key in other_layers
    ]
    assert clashes == [], f"Enneagram labels collide with other layers: {clashes}"


def test_the_three_renamed_types_are_the_renamed_ones() -> None:
    """Pins the rename itself. The retired names are still live DISC styles, so a
    revert here would silently reintroduce the collision."""
    assert enneagram.TYPES[1].label == "The Line-Holder"
    assert enneagram.TYPES[3].label == "The Scorekeeper"
    assert enneagram.TYPES[9].label == "The Peace-Keeper"

    retired = {"The Standard-Setter", "The Front-Runner", "The Steady Hand"}
    assert {spec.label for spec in enneagram.TYPES.values()} & retired == set()


# ── The Enneagram has to be able to say "we don't know" ───────────────────────
def test_narrow_margin_names_two_candidates_and_no_type() -> None:
    """The constraint that makes this layer shippable: a coin flip is reported as
    a coin flip, with both candidates named and the choice handed back."""
    result = enneagram.score_enneagram(_enneagram_responses((1, 2)))

    assert result.provenance == "unresolved"
    assert result.confidence == "low"
    assert result.type_number is None
    assert result.wing is None
    assert result.is_reportable is False
    assert enneagram.guide_section(result) is None
    assert set(result.candidates) == {1, 2}
    assert result.margin is not None and result.margin < enneagram.WEAK_MARGIN

    headline = result.headline()
    assert "inconclusive" in headline
    assert enneagram.TYPES[1].label in headline
    assert enneagram.TYPES[2].label in headline

    step = enneagram.confirmation_step(result)
    assert step is not None
    assert [c.type_number for c in step.candidates] == list(result.candidates)


def test_wide_margin_names_a_type_and_still_offers_the_way_out() -> None:
    result = enneagram.score_enneagram(_enneagram_responses((6,)))

    assert result.provenance == "inferred"
    assert result.confidence == "high"
    assert result.type_number == 6
    assert result.is_reportable is True
    assert result.wing is None, "neighbours were level; no wing may be claimed"
    assert enneagram.guide_section(result) is not None

    step = enneagram.confirmation_step(result)
    assert step is not None
    assert enneagram.DECLINE_OPTION in step.options
    assert enneagram.OTHER_OPTION in step.options


def test_middling_margin_is_named_but_held_loosely() -> None:
    """The row between the two extremes: a type is named, at `moderate`, and the
    guide has to carry the paragraph saying so."""
    result = enneagram.score_enneagram(
        _enneagram_per_item({1: (4, 4, 4), 2: (3, 3, 4)})
    )

    assert result.provenance == "inferred"
    assert result.confidence == "moderate"
    assert result.type_number == 1
    assert result.margin is not None
    assert enneagram.WEAK_MARGIN <= result.margin < enneagram.STRONG_MARGIN

    section = enneagram.guide_section(result)
    assert section is not None
    assert "Held loosely" in section
    assert "worth confirming" in result.headline()


def test_not_taken_claims_nothing() -> None:
    result = enneagram.EnneagramRead.not_taken()

    assert result.confidence == "none"
    assert result.is_reportable is False
    assert enneagram.guide_section(result) is None
    assert enneagram.confirmation_step(result) is None


def test_confirmation_options_do_not_hand_the_person_our_pick() -> None:
    """The candidates are shown in our ranking order, so the option strings are
    the only thing standing between "choose between these two" and "agree with
    us". They have to be positional and symmetric."""
    result = enneagram.score_enneagram(_enneagram_responses((6,)))
    step = enneagram.confirmation_step(result)
    assert step is not None

    assert step.options[:2] == (enneagram.FIRST_OPTION, enneagram.SECOND_OPTION)
    joined = " ".join(step.options).lower()
    assert "that's me" not in joined
    assert "the other one" not in joined
    assert len(step.options) == 4


def test_too_few_answers_is_unresolved_with_no_candidates() -> None:
    sparse = dict(list(_enneagram_responses((6,)).items())[:6])
    result = enneagram.score_enneagram(sparse)

    assert result.provenance == "unresolved"
    assert result.type_number is None
    assert result.candidates == ()
    assert enneagram.confirmation_step(result) is None
    # The layer is not over: the note is the copy for "tell us" / "skip it".
    assert "skip" in result.note.lower()


def test_declined_is_a_complete_outcome() -> None:
    result = enneagram.EnneagramRead.declined()

    assert result.confidence == "none"
    assert result.is_reportable is False
    assert result.type_display is None
    assert result.center is None
    assert enneagram.guide_section(result) is None
    assert enneagram.confirmation_step(result) is None
    assert "opted out" in result.headline()
    assert result.as_dict()["advisory_notice"] == disc.ADVISORY_NOTICE


def test_declining_after_a_read_discards_the_scores() -> None:
    """A rejected inference kept on file is an invitation for some later feature
    to use it."""
    scored = enneagram.score_enneagram(_enneagram_responses((6,)))
    assert scored.ranked != ()

    declined = scored.with_decline()
    assert declined.provenance == "declined"
    assert declined.ranked == ()
    assert declined.type_number is None


def test_self_identification_beats_the_questionnaire() -> None:
    scored = enneagram.score_enneagram(_enneagram_responses((6,)))
    corrected = scored.with_self_identification(4, wing=3)

    assert corrected.provenance == "self_identified"
    assert corrected.type_number == 4
    assert corrected.wing == 3
    assert "self-identified" in corrected.headline().lower()
    # The scored ranking is kept for debugging "why did Cubie guess a 6".
    assert corrected.ranked == scored.ranked


def test_unknown_enneagram_item_id_raises() -> None:
    with pytest.raises(ValueError, match="unknown enneagram item id"):
        enneagram.score_enneagram({"enn_t1_99": 3})

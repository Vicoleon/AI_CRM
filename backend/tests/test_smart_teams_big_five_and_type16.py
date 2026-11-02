"""Big Five and the derived four-letter layer: provenance, arithmetic, refusals.

These two modules shipped without a test file, and the gap was not theoretical.
``type16`` is exactly where the worst defect in this feature lived: a Big Five
domain sitting at precisely 50.0 still selected a full four-letter work-style
portrait, and nudging that domain to 50.1 selected a *different* portrait of the
same person. Twenty percent of the scale sits in that middle band, so it was the
common case rather than an edge, and no test would have caught it because no test
existed. That is what this file is for.

Three things are pinned here.

**Item provenance.** Every item in ``big_five.ITEMS`` claims ``provenance ==
"ipip"``, meaning it comes from the public-domain International Personality Item
Pool rather than being written by us or lifted from a licensed instrument. That
claim is the legal basis for shipping the layer at all, and an item labelled
``ipip`` that was actually invented would be a provenance lie inside a
psychometric instrument. ``CANONICAL_IPIP50_KEY`` below encodes the published
scoring key for Goldberg's 50-item Big-Five Factor Markers, verified against
independent reproductions (see ``docs/smart_teams/instruments/
big_five_and_type16.md`` for the sources and the audit), and the test asserts our
bank agrees with it item for item.

**The arithmetic.** Reverse keying is hand-checked on worked examples, because a
sign error would produce confidently inverted profiles that every other test in
the suite would happily pass.

**The refusals.** A midpoint domain must not yield an asserted letter, a
coin-flip axis must not yield a portrait, and no caller may render a bare
four-letter type without its derived qualifier.
"""

from __future__ import annotations

import pytest

from rubik_core.smart_teams import contracts as ct
from rubik_core.smart_teams.instruments import big_five as bf
from rubik_core.smart_teams.instruments import type16 as t16

# ── the published key we are asserting against ───────────────────────────────

# Goldberg's 50-item IPIP Big-Five Factor Markers, by IPIP item number.
#
# Read as: {ipip_number: (domain, keyed)}. The keying is expressed in the
# direction OUR bank scores, which matters for one factor: the published key
# lists factor IV as Neuroticism, and we score Emotional Stability, so every
# sign on that factor is inverted here relative to a Neuroticism table. Items 9
# and 19 ("Am relaxed most of the time", "Seldom feel blue") are the two
# reverse-scored Neuroticism items and therefore the only two POSITIVE Emotional
# Stability items — that inversion is the single most confusable thing in this
# table and is asserted explicitly below.
#
# The published reverse-keyed sets, for the record: E 6/16/26/36/46,
# A 2/12/22/32, C 8/18/28/38, N 9/19, O 10/20/30.
_E, _A, _C, _ES, _O = (
    bf.Domain.EXTRAVERSION,
    bf.Domain.AGREEABLENESS,
    bf.Domain.CONSCIENTIOUSNESS,
    bf.Domain.EMOTIONAL_STABILITY,
    bf.Domain.OPENNESS,
)
CANONICAL_IPIP50_KEY: dict[int, tuple[bf.Domain, int]] = {
    1: (_E, +1),
    6: (_E, -1),
    11: (_E, +1),
    16: (_E, -1),
    21: (_E, +1),
    26: (_E, -1),
    31: (_E, +1),
    36: (_E, -1),
    41: (_E, +1),
    46: (_E, -1),
    2: (_A, -1),
    7: (_A, +1),
    12: (_A, -1),
    17: (_A, +1),
    22: (_A, -1),
    27: (_A, +1),
    32: (_A, -1),
    37: (_A, +1),
    42: (_A, +1),
    47: (_A, +1),
    3: (_C, +1),
    8: (_C, -1),
    13: (_C, +1),
    18: (_C, -1),
    23: (_C, +1),
    28: (_C, -1),
    33: (_C, +1),
    38: (_C, -1),
    43: (_C, +1),
    48: (_C, +1),
    4: (_ES, -1),
    9: (_ES, +1),
    14: (_ES, -1),
    19: (_ES, +1),
    24: (_ES, -1),
    29: (_ES, -1),
    34: (_ES, -1),
    39: (_ES, -1),
    44: (_ES, -1),
    49: (_ES, -1),
    5: (_O, +1),
    10: (_O, -1),
    15: (_O, +1),
    20: (_O, -1),
    25: (_O, +1),
    30: (_O, -1),
    35: (_O, +1),
    40: (_O, +1),
    45: (_O, +1),
    50: (_O, +1),
}


# ── helpers ──────────────────────────────────────────────────────────────────


def _sitting(**targets: float) -> dict[str, int]:
    """Build a response set that drives each domain toward a target score.

    Answers the extreme in whichever direction the item's keying requires, so a
    target of 100 means "maximally high on this domain" regardless of how many
    of its items are reverse-worded. A target of exactly 50.0 is produced by
    answering the scale midpoint, which is the input that broke ``type16``.
    """
    responses: dict[str, int] = {}
    for item in bf.ITEMS:
        target = targets.get(item.domain.name, 50.0)
        if target == 50.0:
            responses[item.item_id] = 3
        elif target > 50.0:
            responses[item.item_id] = 5 if item.keyed > 0 else 1
        else:
            responses[item.item_id] = 1 if item.keyed > 0 else 5
    return responses


def _score(**targets: float) -> bf.BigFiveResult:
    return bf.score(_sitting(**targets))


def _domain(result: bf.BigFiveResult, domain: bf.Domain) -> float:
    return result.domains[domain].score


# ── 1. provenance: the items really are the public-domain IPIP-50 ────────────


def test_the_bank_is_exactly_fifty_items_ten_per_domain():
    """Ten per domain is the published structure; a gap would skew a score."""
    assert len(bf.ITEMS) == 50
    per_domain: dict[bf.Domain, int] = {}
    for item in bf.ITEMS:
        per_domain[item.domain] = per_domain.get(item.domain, 0) + 1
    assert sorted(per_domain.values()) == [10, 10, 10, 10, 10]
    assert set(per_domain) == set(bf.Domain)


def test_every_item_claiming_ipip_provenance_matches_the_published_key():
    """The provenance claim, checked rather than trusted.

    An item labelled ``ipip`` that was actually written by a model is the worst
    defect available in this module: it is invisible at runtime, it invalidates
    the public-domain argument the layer rests on, and it silently substitutes
    unvalidated wording for validated wording. This asserts the domain
    assignment and keying of all fifty against the published scoring key.
    """
    seen: set[int] = set()
    for item in bf.ITEMS:
        assert item.provenance == bf.Provenance.IPIP, (
            f"{item.item_id} is not marked IPIP; if it was authored in-house it "
            f"must say so, and it must not be listed as a validated item"
        )
        number = item.ipip_number
        assert number in CANONICAL_IPIP50_KEY, (
            f"{item.item_id} claims IPIP #{number}, which is not in the "
            f"published 50-item key"
        )
        assert number not in seen, f"IPIP #{number} used by more than one item"
        seen.add(number)

        expected_domain, expected_keying = CANONICAL_IPIP50_KEY[number]
        assert item.domain == expected_domain, (
            f"{item.item_id} (IPIP #{number}, {item.text!r}) is assigned to "
            f"{item.domain.name}; the published key puts it on "
            f"{expected_domain.name}"
        )
        assert item.keyed == expected_keying, (
            f"{item.item_id} (IPIP #{number}, {item.text!r}) is keyed "
            f"{item.keyed:+d} on {item.domain.name}; the published key says "
            f"{expected_keying:+d}. A sign error here inverts the domain."
        )
    assert seen == set(CANONICAL_IPIP50_KEY), "the bank does not cover all 50"


def test_emotional_stability_is_the_inverted_neuroticism_factor():
    """The one place the published key and our bank deliberately disagree.

    Factor IV is published as Neuroticism with items 9 and 19 reverse-scored. We
    score Emotional Stability, so those two are our only POSITIVE items on the
    factor and the other eight are negative. Getting this backwards would report
    every anxious respondent as unusually steady, which is both wrong and the
    kind of wrong nobody notices in a demo.
    """
    positives = {
        item.ipip_number
        for item in bf.ITEMS
        if item.domain is bf.Domain.EMOTIONAL_STABILITY and item.keyed > 0
    }
    assert positives == {9, 19}
    negatives = {
        item.ipip_number
        for item in bf.ITEMS
        if item.domain is bf.Domain.EMOTIONAL_STABILITY and item.keyed < 0
    }
    assert negatives == {4, 14, 24, 29, 34, 39, 44, 49}


# ── 2. the arithmetic, hand-checked ──────────────────────────────────────────


def test_reverse_keyed_items_invert_and_positive_items_do_not():
    """A worked example per keying direction.

    Verified by hand: on a 1-5 scale a reverse-keyed response inverts as
    ``min + max - response``, so 5 becomes 1 and 1 becomes 5, while a
    positively-keyed response passes through. Asserted through the public scorer
    rather than an internal helper, because a caller can only be hurt by the
    former.
    """
    # bf_e_01 (#1, "Am the life of the party") is positively keyed; bf_e_02
    # (#6, "Don't talk a lot") is reverse-keyed. Answering 5 to both should
    # cancel out rather than compound: one says outgoing, the other says quiet.
    contradictory = {item.item_id: 3 for item in bf.ITEMS}
    contradictory["bf_e_01"] = 5
    contradictory["bf_e_02"] = 5
    balanced = _domain(bf.score(contradictory), bf.Domain.EXTRAVERSION)
    assert balanced == pytest.approx(50.0, abs=0.5), (
        "a positively-keyed 5 and a reverse-keyed 5 are opposite claims and "
        f"must cancel; got {balanced}"
    )

    # Agreeing with the positive item and disagreeing with the reverse item are
    # the SAME claim, so they must compound in the same direction.
    consistent = {item.item_id: 3 for item in bf.ITEMS}
    consistent["bf_e_01"] = 5
    consistent["bf_e_02"] = 1
    assert _domain(bf.score(consistent), bf.Domain.EXTRAVERSION) > balanced


def test_a_fully_polarized_sitting_reaches_the_ends_of_the_scale():
    """If keying is right, driving every item one way saturates the domain."""
    high = _score(EXTRAVERSION=100.0)
    assert _domain(high, bf.Domain.EXTRAVERSION) == pytest.approx(100.0, abs=0.1)
    low = _score(EXTRAVERSION=0.0)
    assert _domain(low, bf.Domain.EXTRAVERSION) == pytest.approx(0.0, abs=0.1)


def test_an_unknown_item_id_raises_rather_than_being_ignored():
    """Silently dropping an unknown id would score a partial bank as complete."""
    with pytest.raises(ValueError, match="unknown Big Five item id"):
        bf.score({"bf_not_a_real_item": 3})


def test_straight_lining_and_neutral_overuse_are_flagged():
    """A rushed bank has to be detectable, or it scores as a real profile."""
    all_neutral = bf.score({item.item_id: 3 for item in bf.ITEMS})
    assert all_neutral.quality.flags, "an all-midpoint bank must raise a flag"

    all_agree = bf.score({item.item_id: 5 for item in bf.ITEMS})
    assert all_agree.quality.flags, "an all-agree bank must raise a flag"


# ── 3. the derived layer refuses where it does not know ──────────────────────


def test_a_domain_at_exactly_the_midpoint_yields_no_portrait():
    """THE regression test for the worst defect this feature has had.

    Extraversion at exactly 50.0 with the other four domains polarized used to
    return a complete four-letter work-style portrait — and moving that one
    domain to 50.1 returned a substantively different portrait of the same
    person. The tie-break direction on a midpoint domain carries no information,
    so any portrait keyed off the resulting letters is a coin flip presented as a
    reading.
    """
    result = _score(
        EXTRAVERSION=50.0,
        AGREEABLENESS=100.0,
        CONSCIENTIOUSNESS=100.0,
        EMOTIONAL_STABILITY=100.0,
        OPENNESS=100.0,
    )
    assert _domain(result, bf.Domain.EXTRAVERSION) == pytest.approx(50.0, abs=0.1)

    derived = t16.derive(result)
    assert derived.coin_flip_axes == (t16.Axis.EI,), (
        "a midpoint domain must register as a coin flip on its axis"
    )

    # THE fix: no portrait. The paragraph is written for four letters together,
    # and one of these four is arbitrary, so there is no paragraph to give.
    assert derived.work_style is None, (
        "a portrait was built on a coin-flip letter — the letter is meaningless, "
        "so the paragraph keyed off it is fabricated"
    )

    # The layer stays reportable ON PURPOSE, and this is the more useful design
    # than withholding everything: the underlying continuous scores are real and
    # worth reading, so what the layer does is mark which letter it does not
    # stand behind. The rendered code lowercases the coin-flip letter and the
    # qualifier says why, which is a stronger guarantee than silence — a reader
    # who sees "iNFJ" is told exactly where the uncertainty is.
    assert derived.is_reportable is True
    rendered = str(derived.code)
    assert rendered.startswith("i"), (
        f"the coin-flip axis must be marked in the rendered code; got {rendered!r}"
    )
    assert t16.COIN_FLIP_NOTE.lower() in rendered.lower(), (
        "the coin-flip case substitutes its own note for the plain derived "
        "qualifier, and that note is what explains the lowercase letter"
    )
    # Whichever note is used, "derived from Big Five" has to survive: it is the
    # clause that keeps this from reading as an MBTI result.
    assert "derived from big five" in rendered.lower()

    # And the caller-facing description says the portrait is absent rather than
    # simply omitting it, which is the difference between a refusal and a gap.
    described = derived.describe()
    assert "no work-style description" in described.lower()

    # contracts is where a renderer asks whether it may assert anything.
    verdict = ct.verdict_for("type16", derived)
    assert verdict.confidence == "low"
    assert "coin_flip" in verdict.reasons


@pytest.mark.parametrize("coin_flip_count", [1, 2, 3, 4])
def test_any_number_of_coin_flip_axes_withholds_the_portrait(coin_flip_count):
    """Not only the all-four case, which is what the original guard checked.

    The original condition withheld the portrait only when every axis was a coin
    flip, so one meaningless letter out of four still produced a confident
    paragraph. Each axis is driven by exactly one domain, so this parametrizes
    over how many domains sit at the midpoint.
    """
    domains = [
        bf.Domain.EXTRAVERSION,
        bf.Domain.OPENNESS,
        bf.Domain.AGREEABLENESS,
        bf.Domain.CONSCIENTIOUSNESS,
    ]
    targets = {domain.name: 100.0 for domain in domains}
    for domain in domains[:coin_flip_count]:
        targets[domain.name] = 50.0
    targets[bf.Domain.EMOTIONAL_STABILITY.name] = 100.0

    derived = t16.derive(_score(**targets))
    assert len(derived.coin_flip_axes) >= coin_flip_count
    assert derived.work_style is None, (
        f"{coin_flip_count} coin-flip axis/axes still produced a portrait"
    )


def test_a_clearly_differentiated_sitting_does_produce_a_qualified_type():
    """The refusals must not be so broad that the layer never says anything.

    A test suite that only proves a module refuses is indistinguishable from one
    testing a module that always refuses.
    """
    derived = t16.derive(
        _score(
            EXTRAVERSION=100.0,
            AGREEABLENESS=100.0,
            CONSCIENTIOUSNESS=100.0,
            EMOTIONAL_STABILITY=100.0,
            OPENNESS=100.0,
        )
    )
    assert derived.coin_flip_axes == ()
    assert derived.is_reportable is True
    assert derived.work_style, "a fully resolved sitting should have a portrait"
    assert derived.code is not None

    rendered = str(derived.code)
    assert t16.DERIVED_QUALIFIER.lower() in rendered.lower(), (
        f"the rendered code {rendered!r} does not carry the derived qualifier. "
        f"MBTI is a trademarked instrument we do not license, and a bare "
        f"four-letter type reads as one."
    )


def test_the_module_never_claims_to_be_mbti():
    """The disclaimer is the licence to ship this layer at all."""
    assert t16.NOT_AN_MBTI_RESULT
    assert "mbti" in t16.NOT_AN_MBTI_RESULT.lower()
    assert t16.WHY_NO_NICKNAMES, (
        "the reason we author our own descriptions instead of borrowing the "
        "popular free-test nicknames should be stated, not assumed"
    )


def test_neuroticism_has_no_letter_and_says_so():
    """Four axes from five domains: the fifth is deliberately unrepresented.

    Emotional Stability has no counterpart among the four letters. Quietly
    dropping it would imply the four letters summarize the whole profile.
    """
    assert t16.DOMAIN_WITHOUT_A_LETTER is bf.Domain.EMOTIONAL_STABILITY
    assert t16.DOMAIN_WITHOUT_A_LETTER_NOTE


# ---------------------------------------------------------------------------
# The keying gap has to mean the same thing in every domain
# ---------------------------------------------------------------------------


def test_the_keying_gap_is_only_blocking_where_both_sides_have_enough_items():
    """A 2/8 domain must not be ~2.5x easier to fail than a 5/5 one.

    The real failure this pins. The first genuine sitting was refused Emotional
    Stability *and* the derived 16-type layer off a single item: IPIP-50 keys that
    domain 2 plus / 8 minus, so one misread positive item moves the plus mean by a
    full 2.0 scale points and clears a flat 1.5 threshold on its own. The same
    misread in Extraversion (5/5) moves it 0.8 and flags nothing. The respondent
    had answered all ten Extraversion items with a perfect 0.00 gap — the
    signature of careful reading — and was told their answers looked rushed.

    So the threshold is unchanged and the *conclusion drawn from it* is what
    depends on the domain: reported always, blocking only where the smaller keying
    side clears :data:`MIN_BALANCED_KEYING_PER_SIDE`. Emotional Stability is the
    only domain in this bank that does not.
    """
    # The pathological case, reconstructed: consistent everywhere, one contradicted
    # positive item in the 2/8 domain.
    answers = {}
    for item in bf.ITEMS:
        # Answer every item as a stable, agreeable, careful respondent would --
        # i.e. always in the direction the item is keyed, so every gap is 0.
        answers[item.item_id] = 5 if item.keyed > 0 else 1
    lone_positive = next(
        i for i in bf.ITEMS_BY_DOMAIN[bf.Domain.EMOTIONAL_STABILITY] if i.keyed > 0
    )
    answers[lone_positive.item_id] = 1  # the single misread item

    quality = bf.response_quality(answers)
    assert quality.worst_keying_gap > bf.MAX_KEYING_GAP, (
        "precondition: this sitting must exceed the threshold, or the test proves "
        "nothing"
    )
    assert quality.worst_keying_gap_domain is bf.Domain.EMOTIONAL_STABILITY
    # Reported, so a reviewer still sees it...
    assert bf.QualityFlag.KEYING_INCONSISTENCY_IMBALANCED in quality.flags
    # ...but it does not withhold the domain or the derived letters.
    assert bf.QualityFlag.KEYING_INCONSISTENCY not in quality.flags
    assert not quality.blocking_flags, (
        "one misread item in the 2/8 domain must not block a layer: "
        f"{sorted(f.value for f in quality.blocking_flags)}"
    )


def test_a_contradiction_in_a_keying_balanced_domain_still_blocks():
    """The other half of the guard: the check must still catch real carelessness.

    Widening the imbalanced case is only defensible if the balanced case is
    untouched, so this fires the same statistic in a 5/5 domain by agreeing with
    both keying sides — the "agreed with both 'Don't talk a lot' and 'Start
    conversations'" pattern the check exists for.
    """
    answers = {i.item_id: 5 if i.keyed > 0 else 1 for i in bf.ITEMS}
    for item in bf.ITEMS_BY_DOMAIN[bf.Domain.EXTRAVERSION]:
        answers[item.item_id] = 5  # agree with every item regardless of keying

    quality = bf.response_quality(answers)
    assert quality.worst_keying_gap_domain is bf.Domain.EXTRAVERSION
    assert bf.QualityFlag.KEYING_INCONSISTENCY in quality.blocking_flags


def test_emotional_stability_is_the_only_domain_the_widening_applies_to():
    """Scope the exemption, so it cannot quietly grow to cover the whole bank.

    If a future edit to the item bank pushed another domain under the threshold,
    this fails and forces the question to be asked deliberately rather than
    inherited from a set comprehension.
    """
    assert bf.KEYING_GAP_UNRELIABLE_DOMAINS == frozenset(
        {bf.Domain.EMOTIONAL_STABILITY}
    )
    for domain in bf.Domain:
        smaller_side = min(bf.keying_balance(domain))
        assert (domain in bf.KEYING_GAP_UNRELIABLE_DOMAINS) == (
            smaller_side < bf.MIN_BALANCED_KEYING_PER_SIDE
        ), f"{domain.value} keyed {bf.keying_balance(domain)} — membership disagrees"


def test_the_imbalanced_flag_is_advisory_and_explains_itself():
    """A flag nobody can act on is worse than no flag: pin the note and the tier."""
    assert bf.QualityFlag.KEYING_INCONSISTENCY_IMBALANCED in bf.ADVISORY_FLAGS
    note = bf.QUALITY_FLAG_NOTES[bf.QualityFlag.KEYING_INCONSISTENCY_IMBALANCED]
    # It has to name the cause, or the reader concludes they were careless.
    assert "2 positive / 8 negative" in note
    assert "Advisory" in note

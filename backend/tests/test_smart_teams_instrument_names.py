"""Cross-layer name invariants for the Work Style Interview instruments.

Two failures this file exists to prevent, both of which already happened once.

**Names colliding across layers.** The five instrument modules were authored in
parallel, each picking its own vocabulary, and three names ended up meaning
three different things: "Steady Hand" was simultaneously a DISC style, an
Enneagram type and a strengths theme. A single guide prints all three layers, so
a collision does not merely read badly — it makes the document self-
contradicting, and no per-module test can see it because each module is
internally consistent. Three separate design docs claimed this check had been
performed while nothing in the repository performed it, which is the other
reason it is here: the claim is now true.

**Drifting into somebody else's trademark.** The legal basis for shipping these
instruments at all is that they are ours, built on public-domain models —
Marston's DISC dimensions, the public-domain IPIP, Spranger's value
orientations. That argument survives only as long as no name matches a licensed
instrument's vocabulary. The blocklists below are therefore written as the thing
we must NOT be: Gallup's 34 CliftonStrengths themes, the VIA Character
Strengths 24, and the 16personalities type nicknames. A generated guide is a
company-visible document that could end up in front of a client or in a dispute,
so "we happened not to pick one" is not a control. This test is the control.

The blocklists are hardcoded rather than fetched: a network dependency in a unit
test is a flake, and these lists do not change.
"""

from __future__ import annotations

import itertools
import re

import pytest

from rubik_core.smart_teams.instruments import (
    disc,
    enneagram,
    motivators,
    strengths,
    type16,
)

# ── the layers, and how to get a display name out of each ────────────────────


def _display_names(specs: dict) -> list[str]:
    """Pull the human-facing label off a spec mapping, whatever it is called.

    The four modules were written independently and settled on ``name`` in some
    places and ``label`` in others. Reading both keeps this test from silently
    passing on an empty set the day one of them is renamed — an empty set is
    trivially disjoint from everything, which is the failure mode that would
    make this file worthless without anyone noticing.
    """
    names = []
    for key, spec in specs.items():
        name = getattr(spec, "name", None) or getattr(spec, "label", None)
        if name is None and isinstance(spec, str):
            name = spec
        assert name, f"no display name on {key!r} — this test cannot check it"
        names.append(str(name))
    return names


def _normalize(name: str) -> str:
    """Fold for comparison: case, the Enneagram's leading "The", and hyphens.

    Hyphens are folded because "Front-Runner" and "Front Runner" would collide
    in a reader's eye and in a search, which is the thing being prevented — a
    purely textual comparison would call them distinct and miss the point.
    """
    return name.lower().removeprefix("the ").replace("-", " ").strip()


LAYERS: dict[str, list[str]] = {
    "disc": _display_names(disc.WORK_STYLES),
    "enneagram": _display_names(enneagram.TYPES),
    "strengths": _display_names(strengths.THEMES),
    "motivators": _display_names(motivators.MOTIVATOR_SPECS),
}


# ── what we must not be ──────────────────────────────────────────────────────

# Gallup CliftonStrengths — all 34 theme names are trademarked and its items
# are licensed. This is the framework Emery's source documents came from, so it
# is the one our taxonomy is most likely to drift toward.
CLIFTON_STRENGTHS_34 = frozenset(
    {
        "achiever",
        "activator",
        "adaptability",
        "analytical",
        "arranger",
        "belief",
        "command",
        "communication",
        "competition",
        "connectedness",
        "consistency",
        "context",
        "deliberative",
        "developer",
        "discipline",
        "empathy",
        "focus",
        "futuristic",
        "harmony",
        "ideation",
        "includer",
        "individualization",
        "input",
        "intellection",
        "learner",
        "maximizer",
        "positivity",
        "relator",
        "responsibility",
        "restorative",
        "self assurance",
        "significance",
        "strategic",
        "woo",
    }
)

# VIA Character Strengths — a separate proprietary framework, and a plausible
# accidental target because its vocabulary is ordinary English virtue words.
VIA_24 = frozenset(
    {
        "appreciation of beauty",
        "bravery",
        "creativity",
        "curiosity",
        "fairness",
        "forgiveness",
        "gratitude",
        "honesty",
        "hope",
        "humility",
        "humor",
        "judgment",
        "kindness",
        "leadership",
        "love",
        "love of learning",
        "perseverance",
        "perspective",
        "prudence",
        "self regulation",
        "social intelligence",
        "spirituality",
        "teamwork",
        "zest",
    }
)

# 16personalities' type nicknames. Not MBTI itself, but the names most people
# mean when they say a four-letter type out loud — and the ones a model
# generating "our own" descriptions reaches for first.
SIXTEEN_PERSONALITIES_NICKNAMES = frozenset(
    {
        "architect",
        "logician",
        "commander",
        "debater",
        "advocate",
        "mediator",
        "protagonist",
        "campaigner",
        "logistician",
        "defender",
        "executive",
        "consul",
        "virtuoso",
        "adventurer",
        "entrepreneur",
        "entertainer",
    }
)

PROPRIETARY = {
    "Gallup CliftonStrengths (34 trademarked themes)": CLIFTON_STRENGTHS_34,
    "VIA Character Strengths (24)": VIA_24,
    "16personalities type nicknames": SIXTEEN_PERSONALITIES_NICKNAMES,
}


# ── 1. the layers do not collide with each other ─────────────────────────────


def test_every_layer_has_names_to_check():
    """Guard the guard: an empty layer would pass every assertion below."""
    expected_minimums = {
        "disc": 8,
        "enneagram": 9,
        "strengths": 20,
        "motivators": 6,
    }
    for layer, minimum in expected_minimums.items():
        assert len(LAYERS[layer]) >= minimum, (
            f"{layer} exposed {len(LAYERS[layer])} names, expected at least "
            f"{minimum} — this test would pass vacuously"
        )


@pytest.mark.parametrize("left,right", list(itertools.combinations(LAYERS, 2)))
def test_layer_name_sets_are_pairwise_disjoint(left, right):
    """One guide prints several layers; a shared name makes it contradict itself.

    The historical failure: "Steady Hand" was a DISC style, an Enneagram type
    and a strengths theme at once, so a single generated guide used it for
    three unrelated claims about the same person.
    """
    left_names = {_normalize(n) for n in LAYERS[left]}
    right_names = {_normalize(n) for n in LAYERS[right]}
    overlap = left_names & right_names
    assert not overlap, (
        f"{left} and {right} both use {sorted(overlap)}. A guide prints both "
        f"layers, so the same words would carry two different meanings in one "
        f"document. Rename in the newer layer."
    )


def test_names_are_unique_within_each_layer():
    """A duplicate inside one layer makes its own scoring ambiguous."""
    for layer, names in LAYERS.items():
        normalized = [_normalize(n) for n in names]
        duplicates = {n for n in normalized if normalized.count(n) > 1}
        assert not duplicates, f"{layer} repeats {sorted(duplicates)}"


# ── 2. no layer drifts into a licensed instrument's vocabulary ───────────────


@pytest.mark.parametrize("framework,blocked", list(PROPRIETARY.items()))
def test_no_layer_uses_a_proprietary_name(framework, blocked):
    """Our legal basis is that these instruments are ours. Keep it true.

    A guide is a company-visible document. Using a licensed instrument's theme
    name inside one is the kind of detail that turns "we built our own on a
    public-domain model" into a claim we cannot defend.
    """
    for layer, names in LAYERS.items():
        hits = {n for n in names if _normalize(n) in blocked}
        assert not hits, (
            f"{layer} uses {sorted(hits)}, which belongs to {framework}. "
            f"Rename it — see docs/smart_teams/plan.md on why the taxonomies "
            f"are authored in-house."
        )


def test_the_derived_type_descriptions_avoid_the_nicknames():
    """type16 must not reach for the names people expect for four-letter types.

    Checked over the prose as well as the labels, because the temptation is to
    write "the Architect type" in a description rather than as a name.
    """
    offenders: list[str] = []
    for code, spec in type16.WORK_STYLE.items():
        blob = " ".join(
            str(value)
            for value in (getattr(spec, "__dict__", None) or {"spec": spec}).values()
        ).lower()
        for nickname in SIXTEEN_PERSONALITIES_NICKNAMES:
            # Word-boundary-ish check: "executive" appears legitimately in
            # ordinary prose ("an executive audience"), so only flag it when
            # it is being used as a type name.
            if f"the {nickname}" in blob:
                offenders.append(f"{code}: 'the {nickname}'")
    assert not offenders, (
        "type16 descriptions use 16personalities nicknames: "
        f"{offenders}. These are theirs, not ours."
    )


# ── 3. the trademark-styling and product-claim traps ─────────────────────────


def test_no_module_uses_the_wiley_disc_trademark_styling_or_claims_mbti():
    """Two specific brand traps, checked across every instrument module.

    Lowercase-i "DiSC" is Wiley's trademark styling for Everything DiSC; we
    build on Marston's public-domain model and write "DISC". And the derived
    four-letter layer must never claim to be MBTI or Myers-Briggs, which are
    proprietary instruments we do not license.
    """
    import inspect

    for module in (disc, enneagram, motivators, strengths, type16):
        source = inspect.getsource(module)
        # Naming the vendor product in order to disclaim it is the correct
        # usage and every instrument docstring does it, so "Everything DiSC"
        # is allowed. Any OTHER lowercase-i occurrence means we have started
        # writing their mark as though it were the model's name.
        stray = source.replace("Everything DiSC", "")
        assert "DiSC" not in stray, (
            f"{module.__name__} uses Wiley's trademark styling 'DiSC' outside "
            f"a disclaimer. We build on Marston's public-domain model — "
            f"write 'DISC'."
        )
        # MBTI and Myers-Briggs may be NAMED, and must be: every module
        # disclaims them, type16 has to explain what it is not, and its
        # correlation evidence cites McCrae & Costa, who measured actual MBTI
        # indices. So this does not require a disclaimer beside every mention
        # (that punished honest citation) and does not ban the words. It hunts
        # the one thing that is forbidden — an unnegated claim that what we
        # produce IS an MBTI result. "are not an MBTI type" is the correct
        # sentence; "is an MBTI type" is the firing offence, and the only
        # reliable difference between them is the negation.
        lowered = source.lower()
        claim_patterns = (
            "an mbti type",
            "is mbti",
            "your mbti",
            "mbti type is",
            "based on the mbti",
            "based on mbti",
            "mbti assessment",
            "we use mbti",
            "official mbti",
            "mbti-certified",
            "our myers-briggs",
            "myers-briggs assessment",
        )
        negations = ("not", "never", "no ", "n't", "nor ", "without")
        for pattern in claim_patterns:
            for match in re.finditer(re.escape(pattern), lowered):
                preceding = lowered[max(0, match.start() - 40) : match.start()]
                if any(word in preceding for word in negations):
                    continue
                context = lowered[max(0, match.start() - 60) : match.end() + 20]
                raise AssertionError(
                    f"{module.__name__} contains an unnegated {pattern!r} "
                    f"(context: ...{context}...). That reads as a claim to be "
                    f"MBTI — a trademarked instrument we do not license. The "
                    f"four-letter layer is DERIVED from Big Five scores and "
                    f"must say so."
                )

        # And the derived layer must never present a bare four-letter type as
        # if it were a finished verdict: the qualifier is the whole defense.
        if module is type16:
            assert "derived" in lowered, (
                "type16 must describe itself as derived — that word is the "
                "difference between arithmetic on Big Five scores and a claim "
                "to be an instrument we do not license."
            )

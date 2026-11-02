"""The heatmap grid's safety properties, which are all about what must NOT appear.

Three failures would each be shipped-and-invisible, so each has a test here:

* a colleague's raw number reaching a screen, when both Help homes promise
  "colleagues never see a number, a percentile, or a raw score";
* a dimension nobody could be compared on rendering as agreement, which is the
  defect ``comparison_contracts.RefusedDimension`` exists to prevent — empty and
  unknown are different facts;
* motivators appearing on a two-person grid, which is a MEASUREMENT error rather
  than a privacy one: ``emphasis`` is within-person, and the instrument says so
  ("a DISC intensity is meant to be comparable across people, and this number is
  explicitly not").

The grids are built from REAL scoring output, through the real instruments, so a
change to a payload shape or a band vocabulary fails here rather than rendering
an empty heatmap in front of somebody.

Advisory only: nothing here ranks anybody, and there is deliberately no
compatibility score to assert on.
"""

from __future__ import annotations

import pytest

from rubik_core.smart_teams import contracts, heatmap, scoring
from rubik_core.smart_teams import interview as interview_catalog
from rubik_core.smart_teams.instruments import big_five, disc, motivators

A = "a@acme.example"
B = "b@acme.example"

# Answer sets are built from the item banks rather than from literal ids, the
# same way `test_smart_teams_scoring.py` does it: an item-bank edit then changes
# these fixtures instead of quietly scoring less of one. Each test module in this
# repo is self-contained, so they are rebuilt here rather than imported.


def _keyed(value: int, reverse: bool) -> int:
    return (disc.LIKERT_MIN + disc.LIKERT_MAX) - value if reverse else value


def _answers(*, mirror: bool = False) -> dict[str, object]:
    """A sitting that scores cleanly on the three dimensional layers.

    ``mirror`` flips every Likert target across the scale, which is what makes
    the second person land in different bands from the first — without that, the
    pair tests would all pass while comparing somebody to themselves.
    """

    def target(value: int) -> int:
        return (disc.LIKERT_MIN + disc.LIKERT_MAX) - value if mirror else value

    out: dict[str, object] = {}
    for item in disc.LIKERT_ITEMS:
        out[item.item_id] = _keyed(
            target(5 if item.dimension == "D" else 2), item.keying == "reverse"
        )
    for pair in disc.FORCED_CHOICE_PAIRS:
        lead = "D" if not mirror else "S"
        if pair.option_a.dimension == lead:
            out[pair.item_id] = "a"
        elif pair.option_b.dimension == lead:
            out[pair.item_id] = "b"
        else:
            out[pair.item_id] = "a" if pair.option_a.dimension == "I" else "b"

    five_targets = {
        big_five.Domain.EXTRAVERSION: 5,
        big_five.Domain.AGREEABLENESS: 4,
        big_five.Domain.CONSCIENTIOUSNESS: 5,
        big_five.Domain.EMOTIONAL_STABILITY: 2,
        big_five.Domain.OPENNESS: 4,
    }
    for item in big_five.ITEMS:
        out[item.item_id] = _keyed(
            target(five_targets[item.domain]), item.is_reverse_keyed
        )

    weights = (5, 3, 4, 2, 1, 2)
    position = {name: i for i, name in enumerate(motivators.MOTIVATORS)}
    for item in motivators.LIKERT_ITEMS:
        out[item.item_id] = _keyed(
            target(weights[position[item.motivator]]), item.keying == "reverse"
        )
    return out


def _envelopes(email: str, *, mirror: bool = False):
    """One person's six stored columns, scored through the real instruments.

    No strengths extractor is passed: that layer fails, which is isolated per
    layer by `ScoringReport.failures` and irrelevant here — strengths is not a
    scale and never becomes a heatmap cell.
    """
    report = scoring.score_answers(
        contracts.ScoringInput(
            user_email=email,
            answers=_answers(mirror=mirror),
            sections_completed=interview_catalog.SECTION_KEYS,
            elapsed_seconds=25 * 60.0,
        )
    )
    return report.output.as_profile_columns()


@pytest.fixture(scope="module")
def scored_a():
    return _envelopes(A)


@pytest.fixture(scope="module")
def scored_b():
    return _envelopes(B, mirror=True)


@pytest.fixture(scope="module")
def unscored():
    return {f: contracts.not_taken_envelope(f) for f in contracts.FRAMEWORKS}


# ── One person ───────────────────────────────────────────────────────────────


def test_a_closed_gate_yields_bands_and_no_numbers(scored_a):
    """The whole privacy contract in one assertion.

    ``include_scores=False`` is what every colleague-facing caller passes, and a
    cell that still carried its intensity would put a number on a screen the
    Help page promises will never show one.
    """
    cells = heatmap.profile_cells(scored_a, include_scores=False)
    assert cells, "a fully scored sitting produced no cells at all"
    assert all(cell.band for cell in cells)
    assert [cell.key for cell in cells if cell.score is not None] == []
    # And the serialized form OMITS the key rather than nulling it: an explicit
    # `"score": null` reads as "we scored them at nothing".
    assert all("score" not in cell.as_dict() for cell in cells)


def test_the_subject_sees_their_own_numbers(scored_a):
    cells = heatmap.profile_cells(scored_a, include_scores=True)
    assert any(cell.score is not None for cell in cells)
    assert all("score" in cell.as_dict() for cell in cells if cell.score is not None)


def test_a_cell_can_never_be_a_number_alone():
    """A score with no band is refused at construction.

    The band is the half a colleague may read. A cell that had only a number
    could not be rendered on any shared surface, so it must not be buildable.
    """
    with pytest.raises(heatmap.HeatmapError, match="band"):
        heatmap.Cell(
            framework="disc",
            key="D",
            name="Dominance",
            band=None,
            band_ordinal=None,
            band_count=4,
            score=71.0,
        )


def test_an_unreportable_layer_contributes_nothing(unscored):
    """Not a gap — the answers did not support saying anything."""
    assert heatmap.profile_cells(unscored, include_scores=True) == []


def test_motivators_are_present_for_one_person_and_marked_within_person(scored_a):
    """They belong on My Guide, where the comparison group is yourself."""
    motivator_cells = [
        c
        for c in heatmap.profile_cells(scored_a, include_scores=False)
        if c.framework == "motivators"
    ]
    assert motivator_cells, "motivators should appear on a single-person grid"
    assert all(cell.cross_person is False for cell in motivator_cells)
    # The ranking is the output: exactly one top and one bottom rung.
    rungs = [cell.band_ordinal for cell in motivator_cells]
    assert rungs.count(4) == 1 and rungs.count(0) == 1


# ── Two people ───────────────────────────────────────────────────────────────


def test_motivators_never_reach_a_two_person_grid(scored_a, scored_b):
    """The measurement rule, not the privacy one.

    ``emphasis`` is within-person with no norm group, so "Sam's 62 against
    Jo's 55" is not a comparison at all. Excluded by allowlist rather than by
    the caller remembering.
    """
    rows = heatmap.pair_rows(A, scored_a, B, scored_b)
    assert {row.framework for row in rows} == heatmap.CROSS_PERSON_FRAMEWORKS
    assert "motivators" not in {row.framework for row in rows}


def test_a_colleagues_numbers_stay_hidden_while_your_own_show(scored_a, scored_b):
    """The two permissions are answered separately, because they differ.

    On Compare the reader may see their own intensities and never their
    colleague's — one flag for both would force a wrong answer to one of them.
    """
    rows = heatmap.pair_rows(
        A,
        scored_a,
        B,
        scored_b,
        include_reader_scores=True,
        include_about_scores=False,
    )
    assert any(row.reader and row.reader.score is not None for row in rows)
    assert [row.key for row in rows if row.about and row.about.score is not None] == []


def test_an_unscored_colleague_is_unknown_and_never_agreement(scored_a, unscored):
    """The one that would be invisible in review.

    A grey cell meaning "we could not look" and a grey cell meaning "you match"
    are opposite findings. If the joint gate is shut this must be unknown for
    every dimension — and the reader's OWN cell has to drop too, because
    ``PairwiseRead`` refuses to hand back one real layer to compare against a
    default.
    """
    rows = heatmap.pair_rows(A, scored_a, B, unscored)
    assert rows, "the grid must still have a row per dimension, not shrink"
    assert {row.overlap for row in rows} == {heatmap.OVERLAP_NOT_COMPARABLE}
    assert all(row.reader is None and row.about is None for row in rows)


def test_the_grid_keeps_a_row_for_a_dimension_nobody_scored(unscored):
    """A silently shorter grid hides the gap it should be showing."""
    rows = heatmap.pair_rows(A, unscored, B, unscored)
    keys = {(row.framework, row.key) for row in rows}
    assert ("disc", "D") in keys
    assert ("big_five", "openness") in keys


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        (2, 2, heatmap.OVERLAP_ALIGNED),
        (2, 3, heatmap.OVERLAP_NEAR),
        (3, 2, heatmap.OVERLAP_NEAR),
        (0, 3, heatmap.OVERLAP_APART),
        (None, 2, heatmap.OVERLAP_NOT_COMPARABLE),
        (2, None, heatmap.OVERLAP_NOT_COMPARABLE),
        (None, None, heatmap.OVERLAP_NOT_COMPARABLE),
    ],
)
def test_overlap_states(a, b, expected):
    assert heatmap.overlap_for(a, b) == expected


def test_two_different_sittings_actually_differ(scored_a, scored_b):
    """Guards the fixtures themselves.

    If the mirrored sitting stopped producing different bands, every assertion
    about drift above would still pass while testing nothing.
    """
    rows = heatmap.pair_rows(A, scored_a, B, scored_b)
    assert any(row.overlap == heatmap.OVERLAP_APART for row in rows)


# ── Ladders stay in step with their instruments ──────────────────────────────


def test_every_band_a_real_sitting_produces_is_on_its_ladder(scored_a, scored_b):
    """A band with no ordinal cannot be coloured, and would render as unknown.

    This is the test that fires if somebody re-bands an instrument without
    touching `heatmap._LADDERS` — the failure mode is a page of grey cells that
    looks like missing data rather than a crash.
    """
    for envelopes in (scored_a, scored_b):
        for cell in heatmap.profile_cells(envelopes, include_scores=False):
            assert cell.band_ordinal is not None, (
                f"{cell.framework}.{cell.key} has band {cell.band!r}, which is not "
                "on that framework's ladder in heatmap._LADDERS"
            )
            assert 0 <= cell.band_ordinal < cell.band_count


# ── The team map ─────────────────────────────────────────────────────────────


def test_every_style_lands_in_the_quadrant_its_letters_imply():
    """The placement follows the instrument's own axis sentence, not a chart.

    ``disc.py``: "agency (getting ahead) and communion (getting along): D and I
    are the high-agency half, I and S the high-communion half". So D is up-left,
    I up-right, S down-right, C down-left, and a blend sits between its two — a
    property of the axes rather than of anybody's product, which is the whole
    reason we may draw this at all.
    """
    from rubik_core.smart_teams.instruments import disc as disc_mod

    expected = {"D": (-1, 1), "I": (1, 1), "S": (1, -1), "C": (-1, -1)}
    for key, (want_x, want_y) in expected.items():
        x, y = heatmap.style_point(key)
        assert (x > 0) == (want_x > 0), f"{key} is on the wrong side of communion"
        assert (y > 0) == (want_y > 0), f"{key} is on the wrong side of agency"

    # A blend leans toward its primary but is pulled off it by the secondary,
    # so D+I sits between D and I — and NOT on top of I+D.
    d = heatmap.style_point("D")
    di = heatmap.style_point("D+I")
    idd = heatmap.style_point("I+D")
    assert di != d, "a blend must not land on its primary's pure point"
    assert di != idd, (
        "D+I and I+D landed on the same point; WORK_STYLES treats them as "
        "genuinely different people and the map must not erase that"
    )
    assert di[0] > d[0], "D+I should sit closer to I than pure D does"

    # Every catalog style places, so nobody silently vanishes from a team map.
    for key in disc_mod.WORK_STYLES:
        assert heatmap.style_point(key) is not None, key


def test_a_level_profile_sits_at_the_centre():
    """All-Rounder has nothing leading it — the centre IS the finding.

    Forcing it to a letter would be the thing ``LEVEL_PROFILE_SPREAD`` exists to
    avoid: inventing a blend out of noise.
    """
    assert heatmap.style_point("BALANCED") == (0.0, 0.0)


def test_the_radius_is_constant_so_it_cannot_carry_intensity():
    """Direction only. A radius that grew with spread would draw "how strongly D
    you are" — the intensity number wearing a picture, on a surface colleagues
    can see."""
    import math

    from rubik_core.smart_teams.instruments import disc as disc_mod

    radii = [
        math.hypot(*heatmap.style_point(key))
        for key in disc_mod.WORK_STYLES
        if key != "BALANCED"
    ]
    # Tolerance covers the 4-dp rounding applied to the stored coordinates —
    # nothing wider. A genuine intensity radius would differ in the first
    # decimal, not the fourth.
    assert all(math.isclose(r, heatmap.STYLE_RADIUS, abs_tol=1e-3) for r in radii), (
        f"styles sit at differing radii {sorted({round(r, 4) for r in radii})}; "
        "the map must encode which way somebody leans, never how much"
    )


def test_an_unplaceable_person_is_none_rather_than_the_centre():
    """None and All-Rounder are different facts and must stay different values.

    A closed DISC layer collapsing onto (0, 0) would silently describe somebody
    as an All-Rounder on the strength of having answered nothing.
    """
    assert heatmap.style_point(None) is None
    assert heatmap.style_point("") is None
    assert heatmap.style_point("NOT_A_STYLE") is None
    assert heatmap.style_marker(None) is None


def test_the_marker_carries_our_own_style_name():
    """The name travels with the point so no screen keeps its own copy of the
    seventeen names — and they are ours, authored in-house."""
    marker = heatmap.style_marker("D+I")
    assert marker["style_key"] == "D+I"
    assert marker["style_name"] == "Front Runner"
    assert marker["primary"] == "D" and marker["secondary"] == "I"
    assert -1 <= marker["x"] <= 1 and -1 <= marker["y"] <= 1


# ── The map places from more than DISC ───────────────────────────────────────


def test_the_map_is_placed_by_disc_AND_big_five(scored_a):
    """Not a DISC-only picture.

    Both layers read the same two axes — agency and communion — so both get a
    vote. ``placed_by`` reports which ones actually contributed, because a dot
    drawn from one open layer and a dot drawn from two are not equally settled.
    """
    marker = heatmap.map_marker(scored_a)
    assert marker is not None
    assert set(marker["placed_by"]) == {"disc", "big_five"}


def test_temperament_actually_moves_the_dot(scored_a):
    """Guards against Big Five being wired in but ignored.

    Blanking the Big Five layer must change where the person lands. If it does
    not, the averaging is broken and the map is DISC-only while claiming
    otherwise — which would pass every other test here.
    """
    both = heatmap.map_point(scored_a)
    disc_only = heatmap.map_point(
        {**scored_a, "big_five": contracts.not_taken_envelope("big_five")}
    )
    assert both is not None and disc_only is not None
    assert both != disc_only, (
        "removing Big Five left the position unchanged; the layer is being "
        "collected and then not used"
    )


def test_the_within_person_layers_never_move_the_dot(scored_a):
    """Motivators and Enneagram cannot place anybody on a shared map.

    ``emphasis`` is within-person with no norm group and Enneagram is a type
    rather than a scale, so neither has a position on these axes. Blanking both
    must leave the dot exactly where it was — the guarantee that a future edit
    cannot quietly fold them in.
    """
    before = heatmap.map_point(scored_a)
    after = heatmap.map_point(
        {
            **scored_a,
            "motivators": contracts.not_taken_envelope("motivators"),
            "enneagram": contracts.not_taken_envelope("enneagram"),
        }
    )
    assert before == after


def test_one_open_layer_still_places_somebody(scored_a):
    """Half a profile is still a position, not a blank."""
    disc_only = heatmap.map_marker(
        {**scored_a, "big_five": contracts.not_taken_envelope("big_five")}
    )
    five_only = heatmap.map_marker(
        {**scored_a, "disc": contracts.not_taken_envelope("disc")}
    )
    assert disc_only and disc_only["placed_by"] == ["disc"]
    assert five_only and five_only["placed_by"] == ["big_five"]


def test_nobody_placeable_is_none_not_the_centre(unscored):
    """ "We could not place them" and "they are balanced" are different facts."""
    assert heatmap.map_point(unscored) is None
    assert heatmap.map_marker(unscored) is None


def test_the_map_radius_stays_constant_however_many_layers_placed(scored_a):
    """Two agreeing layers must not push somebody further out than one.

    A radius that grew with agreement would be back to drawing magnitude, which
    is the number the map exists to avoid.
    """
    import math

    for envelopes in (
        scored_a,
        {**scored_a, "big_five": contracts.not_taken_envelope("big_five")},
        {**scored_a, "disc": contracts.not_taken_envelope("disc")},
    ):
        point = heatmap.map_point(envelopes)
        assert point is not None
        assert math.isclose(math.hypot(*point), heatmap.STYLE_RADIUS, abs_tol=1e-3)


# ── Strengths clusters on the badge ──────────────────────────────────────────


def _with_strengths(envelopes, theme_ids):
    """The same envelopes plus a reportable strengths layer.

    Built through the instrument's own types so the payload shape is the real
    one — a hand-written dict would keep passing after `as_dict` changed.
    """
    from rubik_core.smart_teams.instruments import strengths

    read = strengths.StrengthsRead(
        themes=tuple(
            strengths.ThemeRead(
                theme_id=theme_id,
                confidence=strengths.Confidence.HIGH,
                evidence_quote="untangling a messy file until the story is obvious",
                why="Named this pattern unprompted, twice.",
            )
            for theme_id in theme_ids
        )
    )
    return {**envelopes, "strengths": contracts.envelope_for("strengths", read)}


def test_clusters_come_from_the_instruments_own_answer(scored_a):
    """`clusters_covered` is already on the payload; we read it, not re-derive it.

    Mapping each theme back through `strengths.THEMES` would be a second
    implementation of a settled question, and the two would drift the first time
    a theme changed cluster.
    """
    from rubik_core.smart_teams.instruments import strengths

    # Two themes from two different clusters, so the answer cannot be a constant.
    by_cluster: dict[str, str] = {}
    for theme_id, spec in strengths.THEMES.items():
        by_cluster.setdefault(spec.cluster.value, theme_id)
    picked = list(by_cluster.items())[:2]
    envelopes = _with_strengths(scored_a, [theme_id for _, theme_id in picked])

    found = heatmap.clusters_of(envelopes)
    assert set(found) == {cluster for cluster, _ in picked}


def test_clusters_are_returned_in_the_instruments_order(scored_a):
    """Fixed order, because each cluster owns a fixed slot on the badge ring.

    A frozenset's iteration order would move the slots between requests for the
    same person, and the whole point of fixed slots is that a reader identifies a
    cluster by POSITION rather than only by colour.
    """
    from rubik_core.smart_teams.instruments import strengths

    all_ids = [
        next(t for t, spec in strengths.THEMES.items() if spec.cluster is cluster)
        for cluster in strengths.Cluster
    ]
    found = heatmap.clusters_of(_with_strengths(scored_a, all_ids))
    assert found == list(heatmap.CLUSTER_ORDER)


def test_a_closed_strengths_layer_draws_no_ring_at_all(scored_a):
    """ "We did not read this" and "covers none of the four" must not look alike.

    An empty list means no badge is drawn. Four empty slots would be a claim
    about the person that nobody measured.
    """
    assert heatmap.clusters_of(scored_a) == []  # the fixture has no strengths
    assert heatmap.map_marker(scored_a)["clusters"] == []


def test_clusters_ride_along_the_dot_without_moving_it(scored_a):
    """A cluster is a set, not a direction. It must not shift the position."""
    from rubik_core.smart_teams.instruments import strengths

    theme_id = next(iter(strengths.THEMES))
    before = heatmap.map_point(scored_a)
    after = heatmap.map_point(_with_strengths(scored_a, [theme_id]))
    assert before == after


def test_marker_from_keeps_cluster_order_whatever_it_is_handed(scored_a):
    """Team Builder passes a sorted set; the badge still needs instrument order."""
    marker = heatmap.marker_from("D+I", (0.1, 0.2), ["stewardship", "momentum"])
    assert marker["clusters"] == ["momentum", "stewardship"]
    assert heatmap.marker_from("D+I", None, ["momentum"]) is None

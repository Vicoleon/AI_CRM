"""Where two people's work styles line up, and where they pull apart.

WHAT THIS IS FOR
----------------
Three surfaces want the same picture — one person's own shape (My Guide), a
pair's overlap and drift (Compare), and a group's coverage (Team Builder) — and
all three were previously served only by prose. This module is the one place
that turns stored profile envelopes into grid cells, so the three screens cannot
disagree about what "far apart" means.

THE TWO RULES THAT DECIDE EVERYTHING HERE
-----------------------------------------
**1. Bands cross the person boundary. Numbers do not.**
``disc.intensity_band`` exists so "the guide never has to print a bare number",
and the company-facing promise in both Help homes is that colleagues never see a
number, a percentile or a raw score. So every cell carries a BAND, and
:attr:`Cell.score` is populated only when the caller passed
``authz.can_view_raw_profile`` for that cell's subject — which grants the subject
themselves plus a support admin, and nobody else. On My Guide that predicate is
true by construction (you are your own subject), which is why your own numbers
show there without a second rule.

The gate is a parameter rather than something computed here because this module
has no request: it is reachable from ADK tool code and from the worker, exactly
like ``authz`` itself. Callers pass ``include_scores=can_view_raw_profile(...)``
and nothing here second-guesses them.

**2. Not every layer may be compared across people, and the reason is
measurement, not privacy.**
``motivators.emphasis`` is *within-person* on 0-100 — the module says so twice
and names the field ``emphasis`` rather than ``intensity`` specifically to mark
it: "a DISC intensity is meant to be comparable across people, and this number is
explicitly not". There is no norm group behind it, so "Sam is a 62 on
Understanding" is not a fact about Sam relative to anyone. Putting motivators in
a pair or team grid would therefore be a measurement error that happens to look
like a finding, which is worse than omitting it. :data:`CROSS_PERSON_FRAMEWORKS`
is the allowlist, and :func:`pair_rows` will not build a row outside it.

DISC intensity is in (its module says it is meant to be cross-person). Big Five
is in for a different reason: its bands are fixed fifths of an absolute 0-100
scale (``big_five.BAND_CUTOFFS``), so two people's bands are two readings off the
same measuring stick even though ``BANDS_ARE_NOT_PERCENTILES`` — there is no norm
sample, and none is needed to say both people sat above 80.

**"Not comparable" is not "aligned".**
The single most dangerous cell in a pair grid is the empty one. A dimension where
either person's layer is not reportable must render as unknown, never as
agreement — the same defect ``comparison_contracts.RefusedDimension`` exists to
prevent, where "we looked and found nothing" and "we could not look" are made
different TYPES so a renderer cannot conflate them. Here the gate is enforced by
building every pair row through ``comparison_contracts.PairwiseRead``, whose own
invariant already drops both layers unless both gates are open. That means this
module cannot reach a one-sided layer even by mistake.

ADVISORY ONLY
-------------
Nothing here ranks anybody. There is no compatibility score, no total, no
per-person aggregate, and no ordering of people — a heatmap is one CSS change
away from a leaderboard, and the absences below are deliberate. See
``team_contracts._forbid_person_metrics`` for the same rule stated as code.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Mapping, Sequence
from typing import Any

from rubik_core.smart_teams import comparison_contracts as cc
from rubik_core.smart_teams import contracts
from rubik_core.smart_teams.instruments import (
    big_five,
    disc,
    motivators,
    strengths,
)

__all__ = [
    "CROSS_PERSON_FRAMEWORKS",
    "OVERLAP_ALIGNED",
    "OVERLAP_APART",
    "OVERLAP_NEAR",
    "OVERLAP_NOT_COMPARABLE",
    "Cell",
    "HeatmapError",
    "envelopes_of",
    "PairRow",
    "overlap_for",
    "pair_rows",
    "profile_cells",
    "CLUSTER_ORDER",
    "clusters_of",
    "map_marker",
    "marker_from",
    "map_point",
    "style_marker",
    "style_point",
    "work_style_key_of",
]


class HeatmapError(ValueError):
    """A grid could not be built from what was stored."""


# ── Which layers may appear where ────────────────────────────────────────────

#: Layers whose scale means the same thing for two different people. Anything
#: outside this set is a single-person read only. See rule 2 in the header —
#: motivators is excluded on measurement grounds, not privacy ones.
CROSS_PERSON_FRAMEWORKS: frozenset[str] = frozenset({"disc", "big_five"})

#: Layers this module knows how to turn into dimensional cells at all. ``type16``
#: (a derived four-letter label), ``strengths`` (clusters, which Team Builder
#: already reports at group level) and ``enneagram`` (a type) are not scales and
#: have no place on an axis.
DIMENSIONAL_FRAMEWORKS: frozenset[str] = frozenset({"disc", "big_five", "motivators"})


# ── Band scales ──────────────────────────────────────────────────────────────
#
# Each framework's band vocabulary in order, lowest first. Read from the
# instrument modules rather than restated, so a re-banding there cannot leave a
# stale ladder here — the ordinals below are what the UI colours by, and a ladder
# out of step with its instrument would mis-colour every cell on the row.

_DISC_LADDER: tuple[str, ...] = tuple(label for _, label in disc.INTENSITY_BANDS)
_BIG_FIVE_LADDER: tuple[str, ...] = tuple(
    band.value for _, band in reversed(big_five.BAND_CUTOFFS)
)
#: Motivators have no band vocabulary of their own — the instrument reports a
#: within-person ``emphasis`` and a RANK, because order is the output and level
#: is not. So the ladder here is positional, and it is only ever used on a
#: single-person grid.
_MOTIVATOR_LADDER: tuple[str, ...] = ("lowest", "lower", "middle", "higher", "highest")

_LADDERS: Mapping[str, tuple[str, ...]] = {
    "disc": _DISC_LADDER,
    "big_five": _BIG_FIVE_LADDER,
    "motivators": _MOTIVATOR_LADDER,
}


def _ordinal(framework: str, band: str) -> int | None:
    """Where ``band`` sits on its framework's ladder, or ``None`` if unknown.

    Unknown is returned rather than raised: a profile scored by an older
    instrument version can legitimately carry a band this release has renamed,
    and one stale cell must not 500 a whole page. The renderer shows it as
    unreadable.
    """
    ladder = _LADDERS.get(framework)
    if ladder is None:
        return None
    try:
        return ladder.index(band)
    except ValueError:
        return None


# ── Cells ────────────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class Cell:
    """One person's standing on one dimension.

    ``score`` is the raw 0-100 number and is ``None`` unless the caller held the
    raw-profile gate for this cell's subject. ``band`` is always present when the
    layer was reportable, because the band is what everybody may see.

    There is deliberately no ``rank``, no ``percentile`` and no total.
    """

    framework: str
    key: str
    name: str
    band: str | None
    band_ordinal: int | None
    band_count: int
    score: float | None = None
    #: False for a layer whose scale is within-person (motivators). The UI says
    #: so on the row rather than quietly showing it next to comparable ones.
    cross_person: bool = True

    def __post_init__(self) -> None:
        if self.score is not None and self.band is None:
            raise HeatmapError(
                f"{self.framework}.{self.key} carries a score with no band; a "
                "number may never be the only thing a cell can show, because the "
                "band is the half a colleague is allowed to read"
            )

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "framework": self.framework,
            "key": self.key,
            "name": self.name,
            "band": self.band,
            "band_ordinal": self.band_ordinal,
            "band_count": self.band_count,
            "cross_person": self.cross_person,
        }
        # Omitted, not null, when the gate is closed. A key that is absent cannot
        # be read as "we scored them at nothing".
        if self.score is not None:
            payload["score"] = self.score
        return payload


def _disc_cells(layer: Mapping[str, Any], *, include_scores: bool) -> list[Cell]:
    scores = layer.get("scores")
    if not isinstance(scores, Mapping):
        return []
    out: list[Cell] = []
    for dimension in disc.DIMENSIONS:
        entry = scores.get(dimension)
        if not isinstance(entry, Mapping):
            continue
        band = entry.get("band")
        intensity = entry.get("intensity")
        # `intensity` and `band` are both None when neither bank had anything for
        # this dimension — a measurement never taken, which is not a zero.
        if band is None:
            continue
        out.append(
            Cell(
                framework="disc",
                key=dimension,
                name=str(entry.get("name") or dimension),
                band=str(band),
                band_ordinal=_ordinal("disc", str(band)),
                band_count=len(_DISC_LADDER),
                score=float(intensity)
                if include_scores and intensity is not None
                else None,
            )
        )
    return out


def _big_five_cells(layer: Mapping[str, Any], *, include_scores: bool) -> list[Cell]:
    # `domains` is a MAPPING keyed by domain value, not a list — see
    # `BigFiveRead.as_dict`. Iterated in the enum's canonical order rather than
    # the dict's, so the five rows are in the same order on every screen and for
    # every person regardless of how the JSON happened to serialize.
    domains = layer.get("domains")
    if not isinstance(domains, Mapping):
        return []
    out: list[Cell] = []
    for domain_enum in big_five.Domain:
        entry = domains.get(domain_enum.value)
        if not isinstance(entry, Mapping):
            continue
        band = entry.get("band")
        if band is None:
            continue
        domain = str(entry.get("domain") or domain_enum.value)
        score = entry.get("score")
        out.append(
            Cell(
                framework="big_five",
                key=domain,
                name=str(entry.get("label") or domain.replace("_", " ").title()),
                band=str(band),
                band_ordinal=_ordinal("big_five", str(band)),
                band_count=len(_BIG_FIVE_LADDER),
                score=float(score) if include_scores and score is not None else None,
            )
        )
    return out


def _motivator_cells(layer: Mapping[str, Any], *, include_scores: bool) -> list[Cell]:
    """Motivators as a within-person ORDER, never as a level.

    The instrument is explicit that everybody scores something on all six and
    that the ranking is the output. So the band here is the person's own position
    in their own ordering, and :attr:`Cell.cross_person` is False so no caller
    can put it beside DISC on a two-person grid.
    """
    # `scores` is `MotivatorResult.ranked` serialized — already highest-emphasis
    # first. The key is not `ranked`; the ORDER is the ranking.
    ranked = layer.get("scores")
    if not isinstance(ranked, Sequence) or not ranked:
        return []
    usable = [entry for entry in ranked if isinstance(entry, Mapping)]
    if not usable:
        return []

    out: list[Cell] = []
    last = len(usable) - 1
    for position, entry in enumerate(usable):
        # Map position in the ranking onto the five-rung ladder. With six
        # motivators and five rungs this is intentionally lossy: the instrument
        # supports "top two and bottom one", not a precise sixth place.
        if position == 0:
            rung = 4
        elif position == 1:
            rung = 3
        elif position == last:
            rung = 0
        elif position == last - 1:
            rung = 1
        else:
            rung = 2
        emphasis = entry.get("emphasis")
        out.append(
            Cell(
                framework="motivators",
                key=str(entry.get("motivator") or ""),
                name=str(entry.get("name") or entry.get("motivator") or ""),
                band=_MOTIVATOR_LADDER[rung],
                band_ordinal=rung,
                band_count=len(_MOTIVATOR_LADDER),
                score=float(emphasis)
                if include_scores and emphasis is not None
                else None,
                cross_person=False,
            )
        )
    return out


_BUILDERS = {
    "disc": _disc_cells,
    "big_five": _big_five_cells,
    "motivators": _motivator_cells,
}


def envelopes_of(profile: object) -> dict[str, Mapping[str, Any]]:
    """``{framework: envelope}`` off an ``st_profiles`` row, safely.

    A NULL column becomes ``not_taken_envelope`` and a column that fails
    ``validate_envelope`` becomes one too — the same handling ``guide.read_layers``
    gives them, and for the same reason: a row written by a seed script or an
    older deploy must not be rendered on a guess. One bad column costs its own
    row on the grid, not the whole page.
    """
    out: dict[str, Mapping[str, Any]] = {}
    for framework in contracts.FRAMEWORKS:
        payload = getattr(profile, framework, None)
        if payload is None:
            out[framework] = contracts.not_taken_envelope(framework)
            continue
        try:
            contracts.validate_envelope(payload)
        except Exception:  # noqa: BLE001 — any malformed payload is "not taken"
            out[framework] = contracts.not_taken_envelope(framework)
            continue
        out[framework] = payload
    return out


def profile_cells(
    envelopes: Mapping[str, Mapping[str, Any] | None],
    *,
    include_scores: bool,
) -> list[Cell]:
    """Every dimensional cell for ONE person, from their stored envelopes.

    ``envelopes`` is ``{framework: st_profiles column}``. A column that was never
    written is passed as ``None`` and handled the way ``guide.read_layers``
    handles it, through ``contracts.not_taken_envelope``.

    A layer whose verdict is not reportable contributes nothing — the same gate
    every other reader applies. That is a real answer, not a gap: it means the
    person's answers on that layer did not support saying anything.
    """
    out: list[Cell] = []
    for framework in contracts.FRAMEWORKS:
        if framework not in DIMENSIONAL_FRAMEWORKS:
            continue
        envelope = envelopes.get(framework)
        if envelope is None:
            envelope = contracts.not_taken_envelope(framework)
        verdict = contracts.read_verdict(envelope)
        if not verdict.reportable:
            continue
        layer = contracts.layer_of(envelope)
        if not isinstance(layer, Mapping):
            continue
        out.extend(_BUILDERS[framework](layer, include_scores=include_scores))
    return out


# ── Pair overlap ─────────────────────────────────────────────────────────────

#: Same band. The two of you read the same on this dimension.
OVERLAP_ALIGNED = "aligned"
#: One rung apart — a difference, not a gap.
OVERLAP_NEAR = "near"
#: Two or more rungs apart. This is where the drift is.
OVERLAP_APART = "apart"
#: At least one of you has nothing reportable here. NOT agreement — see the
#: header. Every renderer must show this differently from :data:`OVERLAP_ALIGNED`.
OVERLAP_NOT_COMPARABLE = "not_comparable"


def overlap_for(a: int | None, b: int | None) -> str:
    """Two band ordinals to an overlap state. ``None`` on either side is unknown."""
    if a is None or b is None:
        return OVERLAP_NOT_COMPARABLE
    gap = abs(a - b)
    if gap == 0:
        return OVERLAP_ALIGNED
    if gap == 1:
        return OVERLAP_NEAR
    return OVERLAP_APART


@dataclasses.dataclass(frozen=True)
class PairRow:
    """One dimension, both people's standing on it, and the verdict.

    ``reader`` is the person the page is written to and ``about`` the colleague,
    matching ``PairwiseRead``'s load-bearing argument order. Either may be
    ``None`` when the joint gate is closed, and ``overlap`` is then
    :data:`OVERLAP_NOT_COMPARABLE`.
    """

    framework: str
    key: str
    name: str
    reader: Cell | None
    about: Cell | None
    overlap: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "framework": self.framework,
            "key": self.key,
            "name": self.name,
            "reader": self.reader.as_dict() if self.reader else None,
            "about": self.about.as_dict() if self.about else None,
            "overlap": self.overlap,
        }


def pair_rows(
    reader_email: str,
    reader_envelopes: Mapping[str, Mapping[str, Any] | None],
    about_email: str,
    about_envelopes: Mapping[str, Mapping[str, Any] | None],
    *,
    include_reader_scores: bool = False,
    include_about_scores: bool = False,
) -> list[PairRow]:
    """The overlap grid for one ORDERED pair.

    Built through ``comparison_contracts.PairwiseRead.from_payloads``, which
    applies the per-subject gate and then the joint gate, and drops BOTH layers
    unless both are open. This function therefore cannot reach a one-sided layer
    — the case where a real value would be compared against a default, and the
    reason that class exists.

    The two ``include_*_scores`` flags are separate on purpose. The common case
    on Compare is that the reader may see their OWN numbers and not their
    colleague's, and a single flag would force those two different permissions
    to be answered with one value.
    """
    rows: list[PairRow] = []
    for framework in contracts.FRAMEWORKS:
        if framework not in CROSS_PERSON_FRAMEWORKS:
            continue
        read = cc.PairwiseRead.from_payloads(
            framework,
            reader_email,
            reader_envelopes.get(framework),
            about_email,
            about_envelopes.get(framework),
        )
        build = _BUILDERS[framework]
        # `layer_a`/`layer_b` are None together whenever the joint gate is shut,
        # so this yields an all-unknown row rather than a half-populated one.
        reader_cells = (
            {
                c.key: c
                for c in build(read.layer_a, include_scores=include_reader_scores)
            }
            if read.layer_a is not None
            else {}
        )
        about_cells = (
            {c.key: c for c in build(read.layer_b, include_scores=include_about_scores)}
            if read.layer_b is not None
            else {}
        )
        for key in _dimension_keys(framework):
            mine = reader_cells.get(key)
            theirs = about_cells.get(key)
            rows.append(
                PairRow(
                    framework=framework,
                    key=key,
                    name=(mine or theirs).name if (mine or theirs) else key,
                    reader=mine,
                    about=theirs,
                    overlap=overlap_for(
                        mine.band_ordinal if mine else None,
                        theirs.band_ordinal if theirs else None,
                    ),
                )
            )
    return rows


def _dimension_keys(framework: str) -> tuple[str, ...]:
    """Every dimension a framework has, so a row exists even when nobody scored.

    Driven off the instrument's own vocabulary rather than off what happens to be
    in the payload: a dimension missing from both people must still appear as an
    unknown row, or the grid silently shrinks and the gap stops being visible.
    """
    if framework == "disc":
        return disc.DIMENSIONS
    if framework == "big_five":
        return tuple(domain.value for domain in big_five.Domain)
    if framework == "motivators":
        return tuple(motivators.MOTIVATORS)
    return ()


# ── The team map ─────────────────────────────────────────────────────────────
#
# WHAT THIS IS, AND WHAT IT IS CAREFULLY NOT
# ------------------------------------------
# A position on a circle for one person's working style, so three screens can
# draw where a group clusters and where it is thin. It is derived from Marston's
# public-domain MODEL and from our own seventeen style names — never from a
# vendor's chart.
#
# `disc.py`'s legal-provenance header is the constraint: the model is common
# property, every vendor's instrument is not, and it names one visual thing as
# forbidden outright — Wiley's trademarked lowercase-i styling. So DISC is
# written in full caps everywhere this feeds, the quadrant palette is our own
# brand rather than any vendor's, and the ring vocabulary is our own axis names.
# Do not relabel these rings with a vendor's words.
#
# THE AXES ARE THE INSTRUMENT'S OWN
# ---------------------------------
# `disc.py` states them: "the same pair the interpersonal-circumplex literature
# calls agency (getting ahead) and communion (getting along): D and I are the
# high-agency half, I and S the high-communion half". Every unit vector below
# falls straight out of that sentence and nothing here invents a placement.
#
# DIRECTION ONLY — THE RADIUS CARRIES NO INTENSITY
# ------------------------------------------------
# Every non-balanced style sits at the SAME radius. That is deliberate: letting
# the radius grow with `spread` would draw "how strongly D you are", which is the
# intensity number wearing a picture, on a surface colleagues can see. The map
# answers "which way does this person lean", not "how much". Balanced sits at the
# centre because a level profile genuinely has no direction — see
# `disc.LEVEL_PROFILE_SPREAD`.
#
# It is a description, never a judgement. There is no "ideal" point on this
# circle, no distance-to-ideal, and no ordering of people by where they land —
# the same rule `team_contracts._forbid_person_metrics` enforces in types.

#: Unit vectors on (x = communion, y = agency), read off the axis sentence above.
#: D is high-agency / low-communion, I high on both, S low-agency / high-communion,
#: C low on both. That puts D top-left, I top-right, S bottom-right, C bottom-left
#: — an arrangement that follows from the axes themselves, not from any chart.
_AXIS_UNIT = math.sqrt(0.5)
_DIMENSION_VECTORS: Mapping[str, tuple[float, float]] = {
    "D": (-_AXIS_UNIT, _AXIS_UNIT),
    "I": (_AXIS_UNIT, _AXIS_UNIT),
    "S": (_AXIS_UNIT, -_AXIS_UNIT),
    "C": (-_AXIS_UNIT, -_AXIS_UNIT),
}

#: How far out every placed style sits. Constant by design — see the header.
STYLE_RADIUS = 0.78

#: How much the secondary dimension pulls the blend away from its primary. Below
#: 1.0 because the primary leads: `disc.WORK_STYLES` treats D+I and I+D as
#: genuinely different people, and an equal weighting would land them on the same
#: point and erase that distinction.
_SECONDARY_PULL = 0.55


def style_point(work_style_key: str | None) -> tuple[float, float] | None:
    """A work-style key to an ``(x, y)`` in the unit circle, or ``None``.

    ``None`` for an unknown key and for a person whose DISC layer is closed —
    which the renderer must show as "not placed" rather than dropping them from
    the map. Somebody missing from a team picture reads as though they were not
    on the team.

    ``BALANCED`` is the origin, and that is a real answer rather than a fallback:
    a level profile has nothing leading it, which is the whole reason
    `disc.WORK_STYLES` carries an All-Rounder instead of forcing a letter.
    """
    if not work_style_key:
        return None
    style = disc.WORK_STYLES.get(work_style_key)
    if style is None:
        return None
    if style.primary is None:
        return (0.0, 0.0)  # All-Rounder: no direction, and that is the finding

    px, py = _DIMENSION_VECTORS[style.primary]
    if style.secondary is not None:
        sx, sy = _DIMENSION_VECTORS[style.secondary]
        px, py = px + _SECONDARY_PULL * sx, py + _SECONDARY_PULL * sy

    magnitude = math.hypot(px, py)
    if magnitude == 0:  # opposite pair; no blend in the catalog does this today
        return (0.0, 0.0)
    return (
        round(px / magnitude * STYLE_RADIUS, 4),
        round(py / magnitude * STYLE_RADIUS, 4),
    )


def work_style_key_of(
    envelopes: Mapping[str, Mapping[str, Any] | None],
) -> str | None:
    """The DISC work-style key off a person's stored envelopes, or ``None``.

    Gate-checked like every other read here: a DISC layer that is not reportable
    yields ``None``, so somebody whose answers did not support a style is "not
    placed" rather than placed on a guess.
    """
    envelope = envelopes.get("disc") or contracts.not_taken_envelope("disc")
    if not contracts.read_verdict(envelope).reportable:
        return None
    layer = contracts.layer_of(envelope)
    if not isinstance(layer, Mapping):
        return None
    assignment = layer.get("assignment")
    if not isinstance(assignment, Mapping):
        return None
    style = assignment.get("style")
    if isinstance(style, Mapping) and style.get("key"):
        return str(style["key"])
    return None


def _disc_direction(envelopes: Mapping[str, Any]) -> tuple[float, float] | None:
    """DISC intensities as an (communion, agency) direction, unnormalised.

    Straight off the module's own axis sentence: D and I are the high-agency
    half, I and S the high-communion half. Uses the four intensities rather than
    the style label, so a D+I who is barely ahead of their I lands between the
    two instead of snapping to a bucket.
    """
    cells = {
        cell.key: cell
        for cell in profile_cells(envelopes, include_scores=True)
        if cell.framework == "disc"
    }
    if len(cells) < 4 or any(c.score is None for c in cells.values()):
        # Fall back to the style label when the raw intensities are not all
        # there — a placed dot from four bands beats no dot at all.
        return None
    d, i, sc, c = (cells[k].score for k in ("D", "I", "S", "C"))
    agency = ((d + i) - (sc + c)) / 200.0
    communion = ((i + sc) - (d + c)) / 200.0
    return (communion, agency)


def _big_five_direction(envelopes: Mapping[str, Any]) -> tuple[float, float] | None:
    """Big Five as the SAME two axes, via the two domains that carry them.

    Only Extraversion and Agreeableness are used, and the omission of the other
    three is the point rather than an oversight. Agency and communion are the
    interpersonal plane; Openness, Conscientiousness and Emotional Stability sit
    outside it, and folding them in would push people around the circle on
    dimensions the circle does not represent. They are on the band grid instead,
    where they mean something.

    ``type16`` is deliberately absent too — it is DERIVED from these same Big
    Five domains, so counting both would weight this layer twice.
    """
    cells = {
        cell.key: cell
        for cell in profile_cells(envelopes, include_scores=True)
        if cell.framework == "big_five"
    }
    extraversion = cells.get("extraversion")
    agreeableness = cells.get("agreeableness")
    if extraversion is None or agreeableness is None:
        return None
    if extraversion.score is None or agreeableness.score is None:
        return None
    # 0-100 with 50 the midpoint of the scale, so centre the axis on 50.
    return ((agreeableness.score - 50.0) / 50.0, (extraversion.score - 50.0) / 50.0)


def map_point(envelopes: Mapping[str, Any]) -> tuple[float, float] | None:
    """Where one person sits, from EVERY layer that legitimately places them.

    DISC and Big Five both, averaged — two independent reads of the same two
    axes, which is a better position than either alone. Whichever is reportable
    contributes; if only one is, that one decides.

    The layers that are NOT here, and why, because each absence is a rule:

    * **Motivators** — ``emphasis`` is within-person with no norm group. Two
      people's motivator numbers are not on one scale, so a shared map cannot
      place them against each other. This is the same measurement rule that keeps
      them off the pair grid, not a second opinion about privacy.
    * **Enneagram** — a type, not a scale. It has no position on an axis.
    * **Strengths** — clusters, which is a set rather than a direction. It is
      reported beside the map, never as a coordinate.
    * **type16** — derived from Big Five, so it is already represented.

    ``None`` when neither placing layer is reportable, which the renderer shows
    as "not placed" rather than as the centre. Falls back to the style anchor
    when DISC is reportable but its raw intensities are unavailable.
    """
    parts = [
        direction
        for direction in (_disc_direction(envelopes), _big_five_direction(envelopes))
        if direction is not None
    ]
    if not parts:
        # Nothing dimensional to place from; the style label is the last resort.
        return style_point(work_style_key_of(envelopes))

    x = sum(part[0] for part in parts) / len(parts)
    y = sum(part[1] for part in parts) / len(parts)
    magnitude = math.hypot(x, y)
    if magnitude < 1e-6:
        # Genuinely no lean either way — the same finding as an All-Rounder.
        return (0.0, 0.0)
    return (
        round(x / magnitude * STYLE_RADIUS, 4),
        round(y / magnitude * STYLE_RADIUS, 4),
    )


#: The four work-phase clusters, in the instrument's own order. Order is fixed
#: and load-bearing for the badge: each cluster always occupies the same slot on
#: the ring, so a reader identifies it by POSITION and not only by colour.
CLUSTER_ORDER: tuple[str, ...] = tuple(c.value for c in strengths.Cluster)


def clusters_of(envelopes: Mapping[str, Any]) -> list[str]:
    """The strengths clusters this person's themes cover, past the gate.

    In :data:`CLUSTER_ORDER`, never in set order — an unordered frozenset would
    render the badge slots differently on different requests for the same person.

    Empty when the strengths layer is closed, which the badge draws as no ring at
    all rather than as four empty slots: "we did not read this" and "covers none
    of the four" would otherwise look identical, and only one of them is a
    finding about the person.
    """
    envelope = envelopes.get("strengths") or contracts.not_taken_envelope("strengths")
    if not contracts.read_verdict(envelope).reportable:
        return []
    layer = contracts.layer_of(envelope)
    if not isinstance(layer, Mapping):
        return []
    # `clusters_covered` is the instrument's OWN answer, already on the payload.
    # Re-deriving it by mapping each theme back through `strengths.THEMES` would
    # be a second implementation of a question the layer has already settled, and
    # the two would drift the first time a theme moved cluster.
    covered = layer.get("clusters_covered")
    if not isinstance(covered, Sequence) or isinstance(covered, str):
        return []
    found = {str(cluster) for cluster in covered}
    return [cluster for cluster in CLUSTER_ORDER if cluster in found]


def map_marker(envelopes: Mapping[str, Any]) -> dict[str, Any] | None:
    """One person's dot: position, the style name, and what placed them.

    ``placed_by`` is on the payload so a screen can say "from working style and
    temperament" rather than implying the whole profile decided it — a dot drawn
    from one reportable layer and a dot drawn from two are not equally settled,
    and the reader is entitled to know which they are looking at.
    """
    point = map_point(envelopes)
    if point is None:
        return None
    placed_by = [
        name
        for name, direction in (
            ("disc", _disc_direction(envelopes)),
            ("big_five", _big_five_direction(envelopes)),
        )
        if direction is not None
    ]
    key = work_style_key_of(envelopes)
    style = disc.WORK_STYLES.get(key) if key else None
    return {
        "style_key": key,
        "style_name": style.name if style else None,
        "primary": style.primary if style else None,
        "secondary": style.secondary if style else None,
        "placed_by": placed_by or (["disc"] if key else []),
        # What the badge draws. NOT part of the position — a cluster is a set,
        # not a direction, so it rides along the dot rather than moving it.
        "clusters": clusters_of(envelopes),
        "x": point[0],
        "y": point[1],
    }


def marker_from(
    work_style_key: str | None,
    position: tuple[float, float] | None,
    clusters: Sequence[str] = (),
) -> dict[str, Any] | None:
    """A dot from an already-computed position plus the style label.

    Team Builder computes the position early, off the APPROVED version's row, and
    carries it on the candidate — so this assembles the payload rather than
    recomputing from envelopes it no longer holds. ``None`` position means "not
    placed", which is not the same as the centre.
    """
    if position is None:
        return None
    style = disc.WORK_STYLES.get(work_style_key) if work_style_key else None
    return {
        "style_key": work_style_key,
        "style_name": style.name if style else None,
        "primary": style.primary if style else None,
        "secondary": style.secondary if style else None,
        "placed_by": [],
        "clusters": [c for c in CLUSTER_ORDER if c in set(clusters)],
        "x": round(position[0], 4),
        "y": round(position[1], 4),
    }


def style_marker(work_style_key: str | None) -> dict[str, Any] | None:
    """One person's dot: where it sits, and what the style is CALLED.

    The name travels with the point so no screen has to keep its own copy of the
    seventeen style names — our names, authored in-house, and the thing a reader
    actually needs to make sense of a position.
    """
    point = style_point(work_style_key)
    if point is None:
        return None
    style = disc.WORK_STYLES[work_style_key]  # type: ignore[index]
    return {
        "style_key": style.key,
        "style_name": style.name,
        "primary": style.primary,
        "secondary": style.secondary,
        "x": point[0],
        "y": point[1],
    }

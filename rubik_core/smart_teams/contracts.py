"""The shapes that cross Smart Teams module boundaries — one definition each.

This module exists because of a specific failure on the previous build of this
feature: two agents implementing against "the same" concept in parallel invented
two shapes for it — one wrote a ``published`` Boolean, the other wrote a
predicate expecting the string ``"published"`` — and every company-wide guide
read returned 403 while both halves passed their own tests. Nothing here does
any work that ``instruments/`` already does. It defines the *hand-off*: what a
scored layer looks like once it is sitting in a JSON column, and how a reader
decides whether it may assert anything from it.

## The one rule a guide generator has to follow

Every framework payload in ``st_profiles`` is an **envelope** with the same
top-level keys regardless of which instrument produced it, and one of those keys
is :data:`KEY_REPORTABLE`. If it is ``False``, the layer may not be asserted
from — not hedged, not softened, not "mentioned briefly". Render
:data:`KEY_HEDGE` instead, which carries the instrument's own authored sentence
about why. :func:`read_verdict` is the single call that answers the question, so
downstream code consults one predicate instead of six.

The envelope wraps, rather than replaces, ``result.as_dict()``. That is
deliberate. Each instrument expresses refusal in its own vocabulary — DISC by
``intensity is None``, Big Five by a *missing domain key*, motivators by
``probe_reason``, the Enneagram by ``provenance == "unresolved"``, type16 by
``suppressed_reason``, strengths by too few themes — and those distinctions are
the safety property of the package. Flattening six refusal semantics into one
"score + confidence" record is exactly how a fabricated result reaches a
document that its subject is then asked to approve. So the layer payload is
stored verbatim under :data:`KEY_LAYER` and the envelope adds a *normalized
verdict* on top of it. Detail preserved, gate unmissable.

## Incompatibilities this module resolves

Recon found five real (not stylistic) divergences between the six instrument
modules. Each is resolved here, in one place:

1. **Refusal is expressed six different ways.** Resolved by
   :func:`verdict_for`, which has one hand-written branch per framework reading
   that framework's own fields, and returns a :class:`LayerVerdict`. There is no
   generic "check the confidence field" path, because there is no field all six
   share.
2. **Three different things called ``Confidence``** — a ``type16`` enum
   (``clear``/``leaning``/``coin_flip``), a ``strengths`` enum
   (``high``/``moderate``/``low``), and an ``enneagram`` ``Literal`` that has a
   fourth member ``none``. Resolved by :data:`Confidence`, the four-member
   profile-wide vocabulary, plus :func:`normalize_confidence`, which accepts any
   of the three and maps into it. An import-time assertion pins the vocabulary
   against ``enneagram.Confidence`` so the two cannot drift apart silently.
3. **``big_five.as_dict()`` has no ``"layer"`` key** while the other four scored
   layers do. Resolved by the envelope: :data:`KEY_FRAMEWORK` is always present
   and always authoritative, so no reader has to know which layers self-label.
4. **Result mutability differs** — ``DiscResult`` and ``MotivatorResult`` are
   mutable dataclasses, the rest are frozen (two with ``slots``). Resolved by
   never storing or hashing a result object: :class:`LayerOutcome` carries the
   live object for the duration of one scoring run only, and everything
   persisted is plain JSON.
5. **Quality coverage is asymmetric** (rich in DISC and Big Five, absent in
   motivators and the Enneagram, transcript-shaped in strengths, inherited in
   type16). Not "fixed" here — that would mean inventing checks that do not
   exist. Instead the verdict reason codes are a closed vocabulary
   (:data:`REASON_CODES`), so a layer that *has* no quality signal reports the
   reasons it genuinely has and never an empty-because-unchecked pass.

## What is deliberately NOT here

* No re-typed item ids, thresholds, or framework names. Item banks come from
  ``instruments/``; the framework key names and the vocabulary strings come from
  ``rubik_core.database.smart_teams``, which is where the CHECK constraints that
  enforce them live.
* No provenance field on the envelope. ``st_profiles.provenance`` is a column of
  its own, and two copies of the same fact drift.
* No prose. Every subject-facing hedge sentence is lifted from the instrument
  module that authored it, and this module writes none of its own.

  That is a load-bearing arrangement rather than a tidiness preference, and it
  is worth stating precisely where the guarding actually happens, because an
  earlier version of this bullet overstated it. ``strengths._check_taxonomy``
  runs at import over the *authored* taxonomy — it never saw a runtime string.
  The two hedges that reached people unscreened both got there by being written
  somewhere that check could not reach: ``motivators.headline()`` composed a
  ranking sentence from tie-break order, and ``strengths`` lifted the model's own
  ``notes`` field verbatim onto the refusal surface. Both are now handled where
  they are produced — ``headline()`` refuses per reason, and
  ``validate_extraction`` screens model prose with ``_ADVANCEMENT_PROSE`` and
  hedges in authored copy — so the invariant this bullet describes is true, but
  it is true because of those two fixes and not because of the import-time
  check.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, get_args

from rubik_core.database.smart_teams import PROFILE_FRAMEWORKS
from rubik_core.smart_teams.instruments import (
    big_five,
    disc,
    enneagram,
    motivators,
    strengths,
    type16,
)

# ── Schema version ────────────────────────────────────────────────────────────
#: Stamped into every framework payload. Bump it when the ENVELOPE changes
#: shape — not when an instrument's own ``as_dict()`` gains a key, which is what
#: the per-layer ``item_bank_version`` / ``taxonomy_version`` fields already
#: track. A reader that finds an unfamiliar value must refuse the payload rather
#: than guess: profiles are versioned and never rewritten in place, so old
#: shapes stay readable forever and "guess" means silently mis-rendering
#: somebody's approved guide.
PAYLOAD_SCHEMA_VERSION = "st-payload-1"

#: Every schema version this build can read. Add the old value here when
#: bumping, alongside whatever migration the reader needs.
SUPPORTED_PAYLOAD_SCHEMA_VERSIONS: tuple[str, ...] = (PAYLOAD_SCHEMA_VERSION,)

# ── Framework identity ────────────────────────────────────────────────────────
#: The six layers, in canonical presentation order. Imported, never re-typed:
#: these strings are the ``st_profiles`` column names AND the keys of the
#: provenance map, and a seventh spelling of "big_five" is the bug this file
#: exists to prevent.
FRAMEWORKS: tuple[str, ...] = PROFILE_FRAMEWORKS

Framework = Literal[
    "disc", "big_five", "type16", "strengths", "motivators", "enneagram"
]

# A Literal cannot be built from a runtime tuple, so the two are pinned to each
# other here instead. If somebody adds a framework to the database module and
# not to the Literal, this fails at import rather than at type-check time (which
# nothing in this repo runs) or at render time (which is a wrong guide).
assert set(get_args(Framework)) == set(FRAMEWORKS), (
    "contracts.Framework and database.smart_teams.PROFILE_FRAMEWORKS disagree; "
    "a framework added to one must be added to the other in the same change"
)

#: Order the scoring engine must run in. ``type16`` is a pure restatement of a
#: scored ``BigFiveResult`` and cannot be computed without it, so Big Five has
#: to come first. Derived from :data:`FRAMEWORKS` and asserted rather than
#: re-typed, so a reordering of the canonical list cannot silently break the
#: dependency.
SCORING_ORDER: tuple[str, ...] = FRAMEWORKS
assert SCORING_ORDER.index("big_five") < SCORING_ORDER.index("type16"), (
    "type16 is derived from a scored BigFiveResult; big_five must be scored "
    "first. Reorder PROFILE_FRAMEWORKS or give SCORING_ORDER its own ordering."
)

# ── Confidence ────────────────────────────────────────────────────────────────
#: The profile-wide confidence vocabulary. Four members, because a layer can be
#: *absent* (``none``) as distinct from *weakly evidenced* (``low``) — the
#: Enneagram is the only instrument that already models both, and collapsing
#: them loses the difference between "we did not ask" and "we asked and it did
#: not settle".
Confidence = Literal["high", "moderate", "low", "none"]

CONFIDENCE_VALUES: tuple[str, ...] = get_args(Confidence)

assert set(CONFIDENCE_VALUES) == set(get_args(enneagram.Confidence)), (
    "contracts.Confidence has drifted from enneagram.Confidence; these are the "
    "same four-member vocabulary and one of them is stored in a JSON column"
)

#: How each instrument's own confidence vocabulary maps into
#: :data:`Confidence`. ``type16`` is the interesting one: a "coin flip" axis is
#: not low confidence in the ordinary sense, it is an arithmetic tie on a
#: derived letter, which is the weakest thing this package reports.
_CONFIDENCE_ALIASES: Mapping[str, str] = {
    # type16.Confidence
    "clear": "high",
    "leaning": "moderate",
    "coin_flip": "low",
    # strengths.Confidence / enneagram.Confidence / disc's Literal
    "high": "high",
    "moderate": "moderate",
    "low": "low",
    "none": "none",
}

_CONFIDENCE_RANK: Mapping[str, int] = {"none": 0, "low": 1, "moderate": 2, "high": 3}


def normalize_confidence(value: object) -> Confidence:
    """Map any instrument's confidence into the profile-wide vocabulary.

    Accepts the ``type16`` enum, the ``strengths`` enum, the ``enneagram`` and
    DISC string literals, or a bare string. Raises on anything else: an
    unrecognised confidence must not become "moderate" by default, because
    "moderate" is a value a guide will happily print a paragraph under.
    """
    raw = value.value if hasattr(value, "value") else value
    if not isinstance(raw, str):
        raise ValueError(f"confidence {value!r} is not a string or str-enum member")
    try:
        return _CONFIDENCE_ALIASES[raw]  # type: ignore[return-value]
    except KeyError:
        raise ValueError(
            f"unknown confidence {raw!r}; add it to _CONFIDENCE_ALIASES in the "
            "same change that introduces it"
        ) from None


def floor_confidence(a: Confidence, b: Confidence) -> Confidence:
    """The weaker of two confidences. Used to cap a layer by its source."""
    return a if _CONFIDENCE_RANK[a] <= _CONFIDENCE_RANK[b] else b


# ── Reason codes ──────────────────────────────────────────────────────────────
# A closed vocabulary, on purpose. Reasons are read by the UI (which decides
# whether to offer a retake), by the guide generator (which decides what to say
# instead of the section), and by the admin completion dashboard. A free-text
# reason would make all three string-match on prose. Adding a code is a one-line
# edit HERE, in the same change as the code that emits it.
REASON_NOT_TAKEN = "not_taken"
REASON_DECLINED = "declined"
REASON_NO_LAYER_DATA = "no_layer_data"
REASON_INSUFFICIENT_ANSWERS = "insufficient_answers"
REASON_RETAKE_RECOMMENDED = "retake_recommended"
REASON_QUALITY_FLAGGED = "quality_flagged"
REASON_FLAT_PROFILE = "flat_profile"
REASON_UNRESOLVED = "unresolved"
REASON_PROBE_PENDING = "probe_pending"
REASON_THIN_TRANSCRIPT = "thin_transcript"
REASON_COIN_FLIP = "coin_flip"
REASON_SUPPRESSED_BY_SOURCE = "suppressed_by_source"

REASON_CODES: tuple[str, ...] = (
    REASON_NOT_TAKEN,
    REASON_DECLINED,
    REASON_NO_LAYER_DATA,
    REASON_INSUFFICIENT_ANSWERS,
    REASON_RETAKE_RECOMMENDED,
    REASON_QUALITY_FLAGGED,
    REASON_FLAT_PROFILE,
    REASON_UNRESOLVED,
    REASON_PROBE_PENDING,
    REASON_THIN_TRANSCRIPT,
    REASON_COIN_FLIP,
    REASON_SUPPRESSED_BY_SOURCE,
)

#: Reason codes that mean "the person can fix this by doing something", which is
#: what the interview UI offers a button for. The rest are outcomes, not gaps.
ACTIONABLE_REASONS: frozenset[str] = frozenset(
    {
        REASON_NOT_TAKEN,
        REASON_INSUFFICIENT_ANSWERS,
        REASON_RETAKE_RECOMMENDED,
        REASON_PROBE_PENDING,
        REASON_THIN_TRANSCRIPT,
    }
)

# ── Envelope keys ─────────────────────────────────────────────────────────────
KEY_SCHEMA_VERSION = "schema_version"
KEY_FRAMEWORK = "framework"
KEY_REPORTABLE = "reportable"
KEY_CONFIDENCE = "confidence"
KEY_REASONS = "reasons"
KEY_HEDGE = "hedge"
KEY_INSTRUMENT_VERSION = "instrument_version"
KEY_SCORED_AT = "scored_at"
KEY_LAYER = "layer"

#: Every key an envelope carries, always, for every framework. "Always" is the
#: point: a reader that has to branch on which keys exist is a reader that will
#: eventually forget to branch on :data:`KEY_REPORTABLE`.
ENVELOPE_KEYS: tuple[str, ...] = (
    KEY_SCHEMA_VERSION,
    KEY_FRAMEWORK,
    KEY_REPORTABLE,
    KEY_CONFIDENCE,
    KEY_REASONS,
    KEY_HEDGE,
    KEY_INSTRUMENT_VERSION,
    KEY_SCORED_AT,
    KEY_LAYER,
)


class PayloadError(ValueError):
    """A framework payload does not match the contract in this module."""


# ── The verdict ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LayerVerdict:
    """The normalized "may a guide assert this?" answer for one layer.

    Frozen and hashable, unlike three of the six result objects it is derived
    from, so it is safe to cache, compare, and put in a set.

    ``reportable`` is the gate and it is not advisory. ``confidence`` is how
    firmly to write when the gate is open — it never re-opens a closed gate,
    which is why a non-reportable verdict is pinned to ``"none"``.
    """

    framework: str
    reportable: bool
    confidence: Confidence
    #: Machine codes from :data:`REASON_CODES`, in the order the branch found
    #: them (most-serious first, per each instrument's own precedence).
    reasons: tuple[str, ...] = ()
    #: The instrument's own authored sentence explaining a refusal or hedge, or
    #: ``None`` when the layer is clean. Never written here — always lifted from
    #: the instrument module, so the advisory-language guards in those modules
    #: still apply to it.
    hedge: str | None = None

    def __post_init__(self) -> None:
        if self.framework not in FRAMEWORKS:
            raise PayloadError(
                f"unknown framework {self.framework!r}; expected one of {FRAMEWORKS}"
            )
        if self.confidence not in CONFIDENCE_VALUES:
            raise PayloadError(
                f"{self.framework}: confidence {self.confidence!r} is not one of "
                f"{CONFIDENCE_VALUES}"
            )
        unknown = [code for code in self.reasons if code not in REASON_CODES]
        if unknown:
            raise PayloadError(
                f"{self.framework}: reason codes {unknown!r} are not in "
                "REASON_CODES; add them there in the same change"
            )
        if not self.reportable and self.confidence != "none":
            raise PayloadError(
                f"{self.framework}: a non-reportable layer must carry confidence "
                f"'none', not {self.confidence!r} — a hedged confidence on a "
                "closed gate is how a refused layer gets rendered anyway"
            )
        if self.reportable and self.confidence == "none":
            raise PayloadError(
                f"{self.framework}: confidence 'none' means the layer has no "
                "content; it cannot also be reportable"
            )

    @property
    def is_actionable(self) -> bool:
        """Whether the UI should offer the person a way to improve this layer."""
        return any(code in ACTIONABLE_REASONS for code in self.reasons)


# ── Envelope construction and reading ─────────────────────────────────────────


def _instrument_version(framework: str, result: object) -> str | None:
    """The item-bank / taxonomy version a layer was produced under, if any.

    DISC, Big Five and type16 do not version their banks (type16 has no bank at
    all), so ``None`` is a real answer here, not a missing value.
    """
    for attribute in ("item_bank_version", "taxonomy_version"):
        value = getattr(result, attribute, None)
        if isinstance(value, str):
            return value
    return None


def envelope_for(
    framework: str,
    result: object,
    *,
    scored_at: datetime | None = None,
) -> dict[str, Any]:
    """Build the JSON payload for one ``st_profiles`` framework column.

    ``result`` is the live object from the instrument module —
    ``DiscResult``, ``BigFiveResult``, ``Type16Result``, ``StrengthsRead``,
    ``MotivatorResult`` or ``EnneagramRead``. Its own ``as_dict()`` is stored
    verbatim under :data:`KEY_LAYER`; the verdict is computed by
    :func:`verdict_for` and hoisted to the top level.
    """
    verdict = verdict_for(framework, result)
    as_dict = getattr(result, "as_dict", None)
    if as_dict is None:
        raise PayloadError(
            f"{framework}: {type(result).__name__} has no as_dict(); the stored "
            "payload is the instrument's own dict and cannot be invented here"
        )
    return {
        KEY_SCHEMA_VERSION: PAYLOAD_SCHEMA_VERSION,
        KEY_FRAMEWORK: framework,
        KEY_REPORTABLE: verdict.reportable,
        KEY_CONFIDENCE: verdict.confidence,
        KEY_REASONS: list(verdict.reasons),
        KEY_HEDGE: verdict.hedge,
        KEY_INSTRUMENT_VERSION: _instrument_version(framework, result),
        KEY_SCORED_AT: (scored_at or datetime.utcnow()).isoformat(),
        KEY_LAYER: as_dict(),
    }


def not_taken_envelope(
    framework: str,
    *,
    reason: str = REASON_NOT_TAKEN,
    hedge: str | None = None,
    scored_at: datetime | None = None,
) -> dict[str, Any]:
    """The payload for a layer the person did not supply answers for.

    A framework column could simply be left NULL, and ``st_profiles`` allows
    that. This exists because NULL and "asked, declined" are different facts
    that a person's own profile page has to show differently, and because a
    reader that has to handle NULL *and* an envelope has two code paths where
    one of them will forget the gate. Write this instead of NULL whenever the
    interview reached the section at all.
    """
    verdict = LayerVerdict(
        framework=framework,
        reportable=False,
        confidence="none",
        reasons=(reason,),
        hedge=hedge,
    )
    return {
        KEY_SCHEMA_VERSION: PAYLOAD_SCHEMA_VERSION,
        KEY_FRAMEWORK: framework,
        KEY_REPORTABLE: False,
        KEY_CONFIDENCE: verdict.confidence,
        KEY_REASONS: list(verdict.reasons),
        KEY_HEDGE: verdict.hedge,
        KEY_INSTRUMENT_VERSION: None,
        KEY_SCORED_AT: (scored_at or datetime.utcnow()).isoformat(),
        KEY_LAYER: None,
    }


def validate_envelope(payload: object) -> Mapping[str, Any]:
    """Raise :class:`PayloadError` unless ``payload`` is a well-formed envelope.

    Called by the profile writer before the row is flushed, and by the guide
    generator on read. Both, deliberately: the write side catches a shape bug in
    the change that introduced it, and the read side catches a row written by
    something that predates this module (a seed script, a psql fixup, an older
    deploy) rather than rendering it.
    """
    if not isinstance(payload, Mapping):
        raise PayloadError(f"framework payload must be a mapping, got {type(payload)}")
    missing = [key for key in ENVELOPE_KEYS if key not in payload]
    if missing:
        raise PayloadError(f"framework payload is missing keys {missing!r}")
    extra = [key for key in payload if key not in ENVELOPE_KEYS]
    if extra:
        raise PayloadError(
            f"framework payload has unexpected keys {extra!r}; the layer's own "
            f"fields belong under {KEY_LAYER!r}, not at the top level"
        )
    version = payload[KEY_SCHEMA_VERSION]
    if version not in SUPPORTED_PAYLOAD_SCHEMA_VERSIONS:
        raise PayloadError(
            f"framework payload schema {version!r} is not readable by this "
            f"build (supported: {SUPPORTED_PAYLOAD_SCHEMA_VERSIONS})"
        )
    # Constructing the verdict re-runs every invariant in LayerVerdict, which is
    # where the gate/confidence coherence rules live. Do not duplicate them.
    read_verdict(payload)
    return payload


def read_verdict(payload: Mapping[str, Any]) -> LayerVerdict:
    """The one call a guide generator makes before asserting anything.

    Rebuilding the :class:`LayerVerdict` rather than reading the raw keys is the
    point: the dataclass re-validates the reason codes and the
    gate/confidence coherence, so a payload hand-edited into an incoherent state
    fails here instead of producing a paragraph about a refused layer.
    """
    framework = payload.get(KEY_FRAMEWORK)
    if framework not in FRAMEWORKS:
        raise PayloadError(f"framework payload names unknown layer {framework!r}")
    reportable = payload.get(KEY_REPORTABLE)
    if not isinstance(reportable, bool):
        raise PayloadError(
            f"{framework}: {KEY_REPORTABLE!r} is {reportable!r}, not a bool — a "
            "truthy string here is how a refused layer gets published"
        )
    reasons = payload.get(KEY_REASONS) or []
    if not isinstance(reasons, Sequence) or isinstance(reasons, str):
        raise PayloadError(f"{framework}: {KEY_REASONS!r} must be a list of codes")
    hedge = payload.get(KEY_HEDGE)
    if hedge is not None and not isinstance(hedge, str):
        raise PayloadError(f"{framework}: {KEY_HEDGE!r} must be a string or null")
    return LayerVerdict(
        framework=str(framework),
        reportable=reportable,
        confidence=payload.get(KEY_CONFIDENCE),  # validated in __post_init__
        reasons=tuple(str(code) for code in reasons),
        hedge=hedge,
    )


def layer_of(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The instrument's own ``as_dict()`` from inside an envelope.

    Guarded by :func:`read_verdict` on the way through, so there is no way to
    reach the layer contents without the gate having been evaluated. That is not
    politeness — the 403-for-everyone bug this module exists to prevent was
    exactly a reader reaching content past a predicate it disagreed with.
    """
    verdict = read_verdict(payload)
    if not verdict.reportable:
        return None
    layer = payload.get(KEY_LAYER)
    if layer is not None and not isinstance(layer, Mapping):
        raise PayloadError(f"{verdict.framework}: {KEY_LAYER!r} must be an object")
    return layer


# ── The per-framework verdict adapter ─────────────────────────────────────────
# Six hand-written branches. There is deliberately no loop here: the whole point
# is that each instrument's refusal fields are read by name, so a field that
# gets renamed breaks a test instead of quietly evaluating falsy.


def _disc_verdict(result: disc.DiscResult) -> LayerVerdict:
    """DISC: a style, all four dimensions measured, and no retake advice.

    Precedence is worst-first. ``score_disc`` already floors ``confidence`` to
    ``"low"`` when quality is bad, but that is a hedge, not a gate — a
    retake-recommended sitting must not be published at all, because the style
    name it produces is the single most quotable line in the whole guide.
    """
    reasons: list[str] = []
    intensities = result.intensities
    unmeasured = [dim for dim, value in intensities.items() if value is None]
    if unmeasured:
        reasons.append(REASON_INSUFFICIENT_ANSWERS)
    if result.quality.retake_recommended:
        reasons.append(REASON_RETAKE_RECOMMENDED)
    elif result.quality.flags:
        reasons.append(REASON_QUALITY_FLAGGED)
    assignment = result.assignment
    if assignment.style is None:
        reasons.append(REASON_NO_LAYER_DATA)
    elif assignment.primary is None:
        # The BALANCED style: a real, publishable finding ("no single style
        # dominates"), so this is a hedge reason, not a gate reason.
        reasons.append(REASON_FLAT_PROFILE)

    blocked = bool(unmeasured) or result.quality.retake_recommended
    if assignment.style is None:
        blocked = True
    if blocked:
        return LayerVerdict(
            framework="disc",
            reportable=False,
            confidence="none",
            reasons=tuple(reasons),
            hedge=_first_flag_detail(result.quality),
        )
    return LayerVerdict(
        framework="disc",
        reportable=True,
        confidence=normalize_confidence(assignment.confidence),
        reasons=tuple(reasons),
        hedge=_first_flag_detail(result.quality),
    )


def _first_flag_detail(quality: disc.ResponseQuality) -> str | None:
    """The most serious DISC quality flag's authored explanation.

    DISC is the only instrument whose quality flags carry a per-flag
    subject-facing ``detail``, so it is the only one whose hedge can be lifted
    rather than composed. Retake-severity flags win over warnings.
    """
    for flag in quality.flags:
        if flag.severity == "retake":
            return flag.detail
    return quality.flags[0].detail if quality.flags else None


def _big_five_verdict(result: big_five.BigFiveResult) -> LayerVerdict:
    """Big Five: at least one scored domain, and no blocking quality flag.

    Refusal here is *absence* — a domain under
    ``big_five.MIN_ANSWERED_PER_DOMAIN`` falls out of ``domains`` entirely and
    is not defaulted to 50. The envelope keeps that per-domain nuance in the
    layer payload (``unscored_domains``) and only gates the layer as a whole,
    because four scored domains are genuinely useful and a guide that refuses
    all five over one gap would be worse, not safer.
    """
    reasons: list[str] = []
    quality = result.quality
    if not result.domains:
        return LayerVerdict(
            framework="big_five",
            reportable=False,
            confidence="none",
            reasons=(REASON_NO_LAYER_DATA,),
            hedge=_notes_hedge(quality),
        )
    if quality.has_blocking_flags:
        return LayerVerdict(
            framework="big_five",
            reportable=False,
            confidence="none",
            reasons=(REASON_QUALITY_FLAGGED,),
            hedge=_notes_hedge(quality),
        )
    if not result.is_complete:
        reasons.append(REASON_INSUFFICIENT_ANSWERS)
    if quality.flags:
        # Advisory flags only, by construction: has_blocking_flags returned
        # False above.
        reasons.append(REASON_QUALITY_FLAGGED)
    confidence: Confidence = (
        "high" if result.is_complete and quality.is_confident else "moderate"
    )
    return LayerVerdict(
        framework="big_five",
        reportable=True,
        confidence=confidence,
        reasons=tuple(reasons),
        hedge=_notes_hedge(quality),
    )


def _notes_hedge(quality: big_five.ResponseQuality) -> str | None:
    """Big Five's own authored quality notes, joined, or ``None``."""
    notes = getattr(quality, "notes", None)
    if not notes:
        return None
    if isinstance(notes, str):
        return notes
    return " ".join(str(note) for note in notes) or None


def _type16_verdict(result: type16.Type16Result) -> LayerVerdict:
    """type16: reportable, not all coin flips, and not suppressed upstream.

    This layer inherits its quality from Big Five — ``suppressed_reason`` is set
    from ``bf.ResponseQuality.blocking_flags`` — so a bad Big Five sitting
    closes both gates, which is correct and worth stating: the letters are
    arithmetic on those same answers, so they cannot be more trustworthy than
    the answers.

    Note the display guard is structural, not something this verdict enforces:
    ``Type16Result.code`` is a ``str`` subclass whose *value* contains
    "(derived from Big Five)", so f-strings and ``json.dumps`` carry the
    qualifier automatically. Guide prose that mentions a code must still be run
    through ``type16.find_unqualified_codes``.

    The same goes for the work-style paragraph, and it is worth being explicit
    about the division of labour because this branch used to be blamed for it.
    A layer with *one* coin-flip axis stays reportable here — three of its four
    letters mean something and the code renders the fourth in lowercase with
    ``type16.COIN_FLIP_NOTE`` attached — so this verdict does not, and should
    not, protect the portrait. ``Type16Result.work_style`` does that, by
    returning ``None`` whenever any axis is a coin flip: the sixteen paragraphs
    are keyed on all four letters at once, so one meaningless letter selects a
    meaningless paragraph. ``reasons`` carries :data:`REASON_COIN_FLIP` and
    ``confidence`` floors to ``"low"`` below, which is what the UI reads.
    """
    reasons: list[str] = []
    if result.suppressed_reason:
        reasons.append(REASON_SUPPRESSED_BY_SOURCE)
    if result.unresolved_axes:
        reasons.append(REASON_UNRESOLVED)
    if result.coin_flip_axes:
        reasons.append(REASON_COIN_FLIP)
    if not result.is_reportable or result.is_all_coin_flips:
        return LayerVerdict(
            framework="type16",
            reportable=False,
            confidence="none",
            reasons=tuple(reasons) or (REASON_NO_LAYER_DATA,),
            # ``suppressed_reason`` is None on the two refusals that are not a
            # bad Big Five sitting — an unresolved axis, and four coin flips —
            # and a refused layer with no hedge renders as a generic shrug.
            # ``describe()`` is the instrument's own authored sentence for
            # exactly these cases, and on every refusing path it returns a
            # refusal rather than a portrait (the portrait branch is the last one
            # in that method and requires all four axes clear).
            hedge=result.suppressed_reason or result.describe(),
        )
    weakest: Confidence = "high"
    for call in result.calls:
        weakest = floor_confidence(weakest, normalize_confidence(call.confidence))
    return LayerVerdict(
        framework="type16",
        reportable=True,
        confidence=weakest,
        reasons=tuple(reasons),
        hedge=result.suppressed_reason,
    )


def _strengths_verdict(result: strengths.StrengthsRead) -> LayerVerdict:
    """Strengths: enough themes to be a section, capped by transcript thinness.

    ``validate_extraction`` has already dropped every theme whose quote could
    not be found verbatim in a single interviewee turn, so the themes that reach
    here are evidenced by construction. What is left to decide is whether there
    are enough of them (``MIN_REPORTED_THEMES``) and how firmly to write.
    """
    reasons: list[str] = []
    if result.thin_transcript:
        reasons.append(REASON_THIN_TRANSCRIPT)
    if not result.themes:
        reasons.append(REASON_NO_LAYER_DATA)
    if not result.is_reportable:
        if REASON_NO_LAYER_DATA not in reasons:
            reasons.append(REASON_INSUFFICIENT_ANSWERS)
        return LayerVerdict(
            framework="strengths",
            reportable=False,
            confidence="none",
            reasons=tuple(reasons),
            hedge=result.note,
        )
    strongest: Confidence = "low"
    for theme in result.themes:
        candidate = normalize_confidence(theme.confidence)
        if _CONFIDENCE_RANK[candidate] > _CONFIDENCE_RANK[strongest]:
            strongest = candidate
    if result.thin_transcript:
        strongest = floor_confidence(
            strongest, normalize_confidence(strengths.THIN_TRANSCRIPT_CEILING)
        )
    return LayerVerdict(
        framework="strengths",
        reportable=True,
        confidence=strongest,
        reasons=tuple(reasons),
        hedge=result.note,
    )


#: The refusal sentence for a part-answered motivators section. Written here
#: rather than lifted, for the reason given in the module docstring. Shaped
#: deliberately like the two hedges that already get this case right —
def _motivators_hedge(result: motivators.MotivatorResult, probe_reason: str) -> str:
    """The sentence a refused motivators layer shows its subject.

    Split out of :func:`_motivators_verdict` because it is the whole finding: the
    frontend renders ``hedge`` as the *only* body text when ``reportable`` is
    false, so this string is what a person reads about themselves under a section
    Cubie has just decided it cannot state. Closing the gate is not enough on its
    own — the hedge is rendered *because* the gate is closed, so a hedge that
    names the ranking publishes the ranking anyway, in the one slot guaranteed to
    be read.

    This now delegates to ``headline()`` for every reason, because
    ``motivators.py`` authors an honest sentence for each of them. It did not
    always: an earlier version of this module carried its own
    ``MOTIVATORS_INCOMPLETE_HEDGE`` because ``headline()`` named a top two on a
    part-answered section (one item answered still produced "Driven most by
    Practical Return then Understanding"), and a later round found the same
    defect surviving in two more reasons — ``runner_up_contested`` asserted an
    order with "then" into the exact slot it conceded was unsettled, and a
    three-or-more-way tie at the bottom was reported as two named orientations
    moving the person least. Those are fixed at the source now, where the ranking
    and its ties are actually known, so this module holds no prose of its own and
    the instrument's advisory guards cover every sentence again.
    """
    return result.headline()


def _motivators_verdict(result: motivators.MotivatorResult) -> LayerVerdict:
    """Motivators: publishable only when no probe is outstanding.

    ``top_two`` and ``bottom`` are POSITIONAL, not findings — on a level or
    incomplete result they are simply declaration order. That is the trap this
    branch closes: any ``probe_reason`` at all shuts the gate, so a renderer
    cannot reach ``top_two`` on a profile where it means nothing.

    Closing the gate is not sufficient on its own, though, and assuming it was is
    how this branch shipped broken: the hedge is rendered *because* the gate is
    closed, so a hedge that names the ranking publishes the ranking anyway, in
    the one slot guaranteed to be read. :func:`_motivators_hedge` is what decides
    the sentence, per reason.
    """
    reason_map = {
        "incomplete": REASON_INSUFFICIENT_ANSWERS,
        "level_profile": REASON_FLAT_PROFILE,
        "top_two_unordered": REASON_PROBE_PENDING,
        "runner_up_contested": REASON_PROBE_PENDING,
    }
    probe_reason = result.probe_reason
    if probe_reason is not None:
        code = reason_map.get(probe_reason)
        if code is None:
            # A new probe reason with no mapping is a contract change, not a
            # default. motivators.probe_form_name raises on the same condition.
            raise PayloadError(
                f"motivators: probe_reason {probe_reason!r} has no reason-code "
                "mapping in contracts._motivators_verdict; add both in the same "
                "change"
            )
        return LayerVerdict(
            framework="motivators",
            reportable=False,
            confidence="none",
            reasons=(code,),
            hedge=_motivators_hedge(result, probe_reason),
        )
    reasons: list[str] = []
    if result.bottom_is_contested:
        # Not a gate: the top of the ranking is settled, only the least-weighted
        # axis is contested, and the guide's motivators section leads with the
        # top two.
        reasons.append(REASON_PROBE_PENDING)
    confidence: Confidence = "high" if not result.near_ties else "moderate"
    return LayerVerdict(
        framework="motivators",
        reportable=True,
        confidence=confidence,
        reasons=tuple(reasons),
        hedge=None,
    )


def _enneagram_verdict(result: enneagram.EnneagramRead) -> LayerVerdict:
    """Enneagram: ``is_reportable``, which is provenance AND a type number.

    The module's own docstring says to test ``is_reportable`` rather than
    ``type_number is not None``, because a self-identification that was later
    declined keeps the number and loses the right to be published. This branch
    does exactly that and nothing cleverer.
    """
    provenance_reasons = {
        "declined": REASON_DECLINED,
        "not_taken": REASON_NOT_TAKEN,
        "unresolved": REASON_UNRESOLVED,
    }
    if not result.is_reportable:
        code = provenance_reasons.get(result.provenance, REASON_NO_LAYER_DATA)
        return LayerVerdict(
            framework="enneagram",
            reportable=False,
            confidence="none",
            reasons=(code,),
            hedge=result.note or None,
        )
    confidence = normalize_confidence(result.confidence)
    if confidence == "none":
        # Defensive: is_reportable and confidence "none" would be incoherent,
        # and LayerVerdict would raise. Say so here where the cause is legible.
        raise PayloadError(
            "enneagram: read is reportable but carries confidence 'none'; the "
            "instrument's own invariants have been violated upstream"
        )
    reasons: list[str] = []
    if result.answered and result.answered < enneagram.MIN_ITEMS_ANSWERED:
        reasons.append(REASON_INSUFFICIENT_ANSWERS)
    return LayerVerdict(
        framework="enneagram",
        reportable=True,
        confidence=confidence,
        reasons=tuple(reasons),
        hedge=result.note or None,
    )


_VERDICT_ADAPTERS: Mapping[str, Any] = {
    "disc": _disc_verdict,
    "big_five": _big_five_verdict,
    "type16": _type16_verdict,
    "strengths": _strengths_verdict,
    "motivators": _motivators_verdict,
    "enneagram": _enneagram_verdict,
}

assert set(_VERDICT_ADAPTERS) == set(FRAMEWORKS), (
    "every framework needs a verdict adapter; a missing one means a layer with "
    "no publishability gate, which is the bug this module exists to prevent"
)


def verdict_for(framework: str, result: object) -> LayerVerdict:
    """Normalize one instrument's refusal semantics into a :class:`LayerVerdict`.

    The single place the six vocabularies meet. Every caller that needs to know
    "may I say this out loud" goes through here or through
    :func:`read_verdict` on a stored envelope — never through a raw field.
    """
    try:
        adapter = _VERDICT_ADAPTERS[framework]
    except KeyError:
        raise PayloadError(
            f"unknown framework {framework!r}; expected one of {FRAMEWORKS}"
        ) from None
    return adapter(result)


# ── Transcript turns ──────────────────────────────────────────────────────────
# Re-exported, not redefined. ``strengths.validate_extraction`` verifies every
# quote against a SINGLE interviewee turn, and ``_as_turns`` raises TypeError on
# a bare string precisely so that a flattened transcript cannot let Cubie's own
# question pass as the person's words. A competing turn type here would defeat
# that, so there is not one.
Speaker = strengths.Speaker
Turn = strengths.Turn
InterviewTranscript = strengths.InterviewTranscript

#: JSON shape of one turn as stored in ``st_interviews.answers``. Kept as an
#: explicit key list so the interview validator and the scoring engine agree.
TURN_KEYS: tuple[str, ...] = ("speaker", "text", "prompt_id")


def turn_to_dict(turn: Turn) -> dict[str, Any]:
    """One turn, JSON-ready. ``speaker`` is stored as its string value."""
    return {
        "speaker": turn.speaker.value,
        "text": turn.text,
        "prompt_id": turn.prompt_id,
    }


def turn_from_dict(raw: Mapping[str, Any]) -> Turn:
    """Rebuild a :class:`Turn`, raising loudly on anything malformed.

    Loudly, because the alternative is a transcript that silently loses the
    interviewee's turns and produces a strengths read with no quotable
    evidence — which ``validate_extraction`` would then report as "the model
    invented quotes".
    """
    if not isinstance(raw, Mapping):
        raise PayloadError(f"transcript turn must be an object, got {type(raw)}")
    unknown = [key for key in raw if key not in TURN_KEYS]
    if unknown:
        raise PayloadError(f"transcript turn has unexpected keys {unknown!r}")
    speaker_raw = raw.get("speaker")
    try:
        speaker = Speaker(speaker_raw)
    except ValueError:
        raise PayloadError(
            f"transcript turn speaker {speaker_raw!r} is not one of "
            f"{[s.value for s in Speaker]}"
        ) from None
    text = raw.get("text")
    if not isinstance(text, str) or not text.strip():
        raise PayloadError("transcript turn text must be a non-empty string")
    prompt_id = raw.get("prompt_id")
    if prompt_id is not None and not isinstance(prompt_id, str):
        raise PayloadError("transcript turn prompt_id must be a string or null")
    return Turn(speaker=speaker, text=text, prompt_id=prompt_id)


def transcript_to_json(transcript: InterviewTranscript) -> list[dict[str, Any]]:
    """The whole transcript, JSON-ready, in order. Order is load-bearing."""
    return [turn_to_dict(turn) for turn in transcript]


def transcript_from_json(raw: object) -> tuple[Turn, ...]:
    """Rebuild a transcript from stored JSON, preserving order."""
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise PayloadError(
            "a stored transcript must be a list of turn objects; a bare string "
            "is refused here for the same reason strengths._as_turns refuses it"
        )
    return tuple(turn_from_dict(item) for item in raw)


# ── Scoring engine input and output ───────────────────────────────────────────


@dataclass(frozen=True)
class ScoringInput:
    """Everything the scoring engine needs, and nothing it should not have.

    Built from an ``st_interviews`` row (or from an upload's parsed payload) by
    the caller, so the engine itself never touches a Session and can be tested
    with a literal.

    ``user_email`` is already folded to lower case, because
    ``st_profiles.user_email`` has a CHECK constraint asserting exactly that and
    the two uniqueness invariants on the table are keyed on it.
    """

    user_email: str
    #: The whole ``st_interviews.answers`` blob, unpartitioned. Partitioning is
    #: ``interview.partition_answers``' job — the engine must not re-derive it
    #: by string prefix, because ``disc_fc_*`` shares the ``disc_`` prefix with
    #: the DISC Likert bank and a naive split feeds pair ids into the Likert
    #: validator.
    answers: Mapping[str, Any] = field(default_factory=dict)
    #: Section keys the person explicitly completed, from
    #: ``st_interviews.sections_completed``. A layer whose section was never
    #: completed gets :func:`not_taken_envelope`, not a score over partial data.
    sections_completed: tuple[str, ...] = ()
    #: Wall-clock time spent answering, when the interview tracked it. Feeds
    #: DISC's implausibly-fast check; ``None`` simply skips that check.
    elapsed_seconds: float | None = None
    #: Provenance for the layers this input produces, per
    #: ``database.smart_teams.PROVENANCE_SOURCES``.
    provenance: str = "interview"
    interview_id: str | None = None

    def __post_init__(self) -> None:
        if self.user_email != self.user_email.strip().lower():
            raise PayloadError(
                f"ScoringInput.user_email {self.user_email!r} is not folded; "
                "st_profiles.user_email has a CHECK constraint requiring "
                "lower(), and an unfolded write makes one person two people"
            )


@dataclass(frozen=True)
class LayerOutcome:
    """One framework's result for the duration of one scoring run.

    ``result`` is the live instrument object — three of the six are mutable, and
    two use ``slots``, so it is never persisted, hashed, or copied with
    ``dataclasses.replace``. It is here because ``type16`` needs the scored
    ``BigFiveResult`` and the guide generator sometimes wants the real object
    rather than the JSON.
    """

    framework: str
    envelope: Mapping[str, Any]
    verdict: LayerVerdict
    result: object | None = None


@dataclass(frozen=True)
class ScoringOutput:
    """What the scoring engine hands the profile writer.

    ``as_profile_columns()`` is the whole hand-off: the keys are exactly the six
    ``st_profiles`` JSON column names, so the writer splats it and cannot
    misspell one.
    """

    user_email: str
    outcomes: Mapping[str, LayerOutcome]
    provenance: Mapping[str, str]
    schema_version: str = PAYLOAD_SCHEMA_VERSION

    def __post_init__(self) -> None:
        unknown = [name for name in self.outcomes if name not in FRAMEWORKS]
        if unknown:
            raise PayloadError(f"scoring output names unknown frameworks {unknown!r}")
        missing = [name for name in FRAMEWORKS if name not in self.outcomes]
        if missing:
            raise PayloadError(
                f"scoring output is missing frameworks {missing!r}; every layer "
                "gets an envelope, and a layer with no answers gets "
                "not_taken_envelope() rather than being omitted — an omitted "
                "layer is indistinguishable from a layer that failed to score"
            )

    def as_profile_columns(self) -> dict[str, Any]:
        """``{column_name: envelope}`` for the six ``st_profiles`` JSON columns."""
        return {name: dict(self.outcomes[name].envelope) for name in FRAMEWORKS}

    @property
    def reportable_frameworks(self) -> tuple[str, ...]:
        """The layers a guide may assert from, in canonical order."""
        return tuple(
            name for name in FRAMEWORKS if self.outcomes[name].verdict.reportable
        )

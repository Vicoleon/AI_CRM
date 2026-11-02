"""Reading a measurable's machine-readable goal out of its authored goal text.

The Scorecard colours a weekly cell green or red by comparing the value against
``comparator`` + ``target_value``. **Both** halves are required. A measurable
carrying ``comparator='>'`` with ``target_value=None`` still renders its
authored goal in the Goal column ("> 98%") and then colours nothing — which
reads as "the colouring is broken", not as "this measurable has no target".

That state is easy to reach, because the goal lives in two places:

* ``goal_description`` — the authored label a human reads ("> 98%").
* ``comparator`` + ``target_value`` — the machine pair the colouring needs.

The import template splits them across THREE columns — ``Compare``, ``Goal``
(the bare number) and ``Goal Label`` (the prose) — and a column headed "Goal"
invites the goal, so what actually landed on 18 of the 20 Exec measurables was
the LABEL sitting in ``target_value``: ``comparator='<'`` beside
``target_value='<9.25%'`` where ``0.0925`` belongs. Uncomparable, but non-empty,
so it colours nothing and satisfies every "is the goal set?" check on the way.
A blank ``Goal`` column gets you the same grey by the shorter route.

In all of these the label already *says* what the target is, so we read it back
out rather than leaving the pair unusable.

Parsing is deliberately narrow — an unambiguous comparator-and-number goal
("> 98%", "<10,000 usd", ">75 units", "≥ 3") derives; anything else is left
alone for the UI to flag. Two cases are refused on purpose:

* A percent measurable whose text carries no ``%`` ("< 0.02" vs "< 2"). Storage
  is fractional, so the number could already be the fraction or could be
  percent points, and guessing wrong is a silent 100× error in the direction
  nobody would notice — every cell just goes the wrong colour.
* Text whose comparator disagrees with the comparator already stored. Taking
  the number and pairing it with the *other* direction would colour the whole
  row backwards, which is worse than colouring nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

# The exact-decimal helpers storage itself uses. Sharing them is the point: a
# derived target has to land on the same string a typed one would ("0.0925",
# never "0.09250000000000001"), or the same goal would compare differently
# depending on which way it was entered.
from rubik_core.ingestion.eos.scorecard_xlsx import format_decimal, shift_decimal

# Comparators as stored, plus the shapes a human types instead: the maths
# symbols the app renders back (≥/≤) and the transposed "=>" everyone writes.
_COMPARATOR_ALIASES: dict[str, str] = {
    ">": ">",
    "<": "<",
    "=": "=",
    ">=": ">=",
    "<=": "<=",
    "=>": ">=",
    "=<": "<=",
    "≥": ">=",
    "≤": "<=",
}

_GOAL_RE = re.compile(
    r"""
    ^\s*
    (?P<comparator>>=|<=|=>|=<|≥|≤|>|<|=)?      # optional, longest first
    \s*
    \$?\s*
    (?P<number>-?\d[\d,]*(?:\.\d+)?)
    \s*
    (?P<percent>%)?
    \s*
    (?P<unit>[A-Za-z][A-Za-z.\ ]*)?              # "usd", "units", "hours"
    \s*$
    """,
    re.VERBOSE,
)


def is_usable_target(raw: str | None) -> bool:
    """Can this stored ``target_value`` actually be compared against?

    Mirrors ``parseNumeric`` in ``frontend/src/pages/eos/_utils.ts`` — the
    tolerance for ``$``/``,``/``%`` matters, because a legitimately stored
    "33000" and a legitimately stored "0.98" must both pass while the goal LABEL
    somebody typed into the target field ("<9.25%", "> 1 unit") must not.

    Present-but-unusable is the dangerous state: it satisfies every "is it set?"
    check while colouring nothing, which is how 18 of the 20 Exec measurables sat
    grey with no warning. Treat it as missing everywhere.
    """
    if raw is None:
        return False
    text = str(raw).strip().replace("$", "").replace(",", "").replace("%", "")
    if not text:
        return False
    try:
        float(text)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class ParsedGoal:
    """What a goal label yielded. ``comparator`` is None when the text was just
    a number ("75"); ``target_value`` is the canonical storage string."""

    comparator: str | None
    target_value: str


def parse_goal_text(text: str | None, value_type: str | None) -> ParsedGoal | None:
    """Read an authored goal label into (comparator, canonical target).

    ``value_type`` decides how the number is canonicalised — a percent goal is
    stored as a fraction ("< 8%" → "0.08"), matching ``storage_value`` and the
    page's own percent inputs. Returns None when the text isn't a plain
    comparator-and-number goal, or when a percent goal is ambiguous (see the
    module docstring).
    """
    if not text or not text.strip():
        return None
    match = _GOAL_RE.match(text)
    if match is None:
        return None

    raw_comparator = match.group("comparator")
    comparator = _COMPARATOR_ALIASES.get(raw_comparator) if raw_comparator else None
    number = match.group("number").replace(",", "")
    has_percent_sign = match.group("percent") is not None

    try:
        decimal_value = Decimal(number)
    except InvalidOperation:
        return None

    if value_type == "percent":
        if not has_percent_sign:
            # Ambiguous: "< 2" could mean 2% or the fraction 2. Refuse.
            return None
        target = shift_decimal(number, -2)
    elif has_percent_sign:
        # "> 50%" on a number/currency measurable: the label disagrees with the
        # measurable's own type, so we can't say what to store.
        return None
    else:
        target = format_decimal(decimal_value)

    return ParsedGoal(comparator=comparator, target_value=target)


def derive_goal_fields(
    *,
    goal_description: str | None,
    comparator: str | None,
    target_value: str | None,
    value_type: str | None,
) -> dict[str, str]:
    """The ``comparator`` / ``target_value`` fields the authored goal can fill in.

    Only ever fills a half that is missing OR unusable — a real comparator or a
    numeric target always wins, because the machine pair is what somebody set
    deliberately and the label is prose that may lag behind it.

    "Unusable" is doing real work here: a target holding the goal LABEL
    ("<9.25%" where 0.0925 belongs) is present, so every emptiness check passes,
    yet it can never be compared against. Canonicalising it to the number the
    label already states is a correction, not an overwrite. Returns ``{}`` when
    there is nothing to add.
    """
    has_comparator = bool(comparator and comparator.strip())
    has_target = is_usable_target(target_value)
    if has_comparator and has_target:
        return {}

    parsed = parse_goal_text(goal_description, value_type)
    if parsed is None:
        return {}

    fields: dict[str, str] = {}
    if not has_comparator and parsed.comparator:
        fields["comparator"] = parsed.comparator
    if not has_target:
        # A label pointing the other way than the stored comparator would
        # colour the row backwards — leave it grey and let the UI flag it.
        effective = comparator if has_comparator else parsed.comparator
        if parsed.comparator and effective and parsed.comparator != effective:
            return {}
        # A bare number with no direction anywhere is not a goal yet.
        if effective:
            fields["target_value"] = parsed.target_value
    return fields

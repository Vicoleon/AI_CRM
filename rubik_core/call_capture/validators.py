"""Validation of one extraction's raw JSON — pure functions, no I/O.

The second of the four layers (prompt -> these validators -> human approval ->
the HubSpot write gate). Nothing here touches the database, the network or the
clock: everything it is allowed to consider real is passed in, which is what
makes the rules below testable against a fixture and impossible to weaken by
accident from a caller.

Each rule rejects one item, never the batch. A call that produced eleven good
proposals and one hallucinated citation has to yield eleven proposals, and every
rejection carries a machine-readable ``reason`` so the drop rate per reason is
measurable rather than anecdotal.

The rules that hold even when the model is being actively manipulated by
something said on the call:

* **Verbatim evidence** — the quote has to really appear in the line it cites.
  A fabricated justification becomes a rejection instead of a persuasive card.
* **Citation closure** — a cited sequence must be one we actually supplied.
  Models hallucinate identifiers, and an unresolvable citation is a proposal an
  AE cannot check.
* **Property closure** — a vendor field may only target a property the caller
  listed. The transcript cannot introduce a new HubSpot property name.
* **Constrained vocabulary** — a value outside the property's live options
  becomes ``needs_new_option`` for triage rather than a write. This is the
  heard-spelling fix; silently snapping a heard value onto the nearest
  option is the thing it exists to prevent.
* **Closed signal enum** — see :data:`STAGE_SIGNALS` below and the loud comment
  on why free text there is the worst available failure.
"""

from __future__ import annotations

import logging
import re
from typing import Any

# Imported at module scope on purpose, and this import is load-bearing twice
# over. It is the single definition of the signal enum — the extractor and the
# stage-lag detector cannot drift apart if there is only one tuple — and the
# assertion below turns any future drift into a startup failure.
from rubik_core.cockpit.stage_lag import SIGNAL_TO_STAGE, STAGE_SIGNALS
from rubik_core.config import settings

logger = logging.getLogger(__name__)

# A drifted enum is the quietest possible failure in this feature: the extractor
# happily emits a signal the detector's SIGNAL_TO_STAGE has no key for, every
# lookup misses, the detector produces zero exceptions, and NOTHING errors. Spec
# 02 calls stage-lag detection "arguably the higher-value half" of the cockpit,
# so it would be half the feature quietly doing nothing. Fail at import instead.
assert set(STAGE_SIGNALS) == set(SIGNAL_TO_STAGE), (
    "call_capture and cockpit.stage_lag disagree about the stage-signal enum: "
    f"extractor={sorted(STAGE_SIGNALS)} detector={sorted(SIGNAL_TO_STAGE)}. "
    "A signal the detector cannot look up produces zero exceptions silently."
)

# ── Machine-readable drop reasons ─────────────────────────────────────────────
# Strings, not an enum, to match the repo's vocabulary style — and stable,
# because the wrap-up's "why did Cubie discard this?" panel and any drop-rate
# dashboard both key on them.
DROP_NOT_AN_OBJECT = "not_an_object"
DROP_MISSING_EVIDENCE_TEXT = "missing_evidence_text"
DROP_EVIDENCE_NOT_VERBATIM = "evidence_not_verbatim"
DROP_UNKNOWN_EVIDENCE_SEQUENCE = "unknown_evidence_sequence"
DROP_MISSING_CITATION = "missing_citation"
DROP_UNKNOWN_TARGET_PROPERTY = "unknown_target_property"
DROP_NO_VALUE = "no_value"
DROP_UNKNOWN_STAGE_SIGNAL = "unknown_stage_signal"
DROP_MALFORMED = "malformed"

#: ``kind`` -> the HubSpot object the item eventually touches, or ``None`` when
#: it touches none. ``sow_request`` opens a commitment; ``contract_expiry`` has
#: no home in HubSpot yet (the standard example of a fact that today lives
#: nowhere), so it is stored for the wrap-up and nothing else.
TARGET_OBJECT_BY_KIND: dict[str, str | None] = {
    "vendor_field": "companies",
    "lead": "leads",
    "task": "tasks",
    "sow_request": None,
    "stage_signal": "deals",
    "contract_expiry": None,
}

SOW_KINDS: tuple[str, ...] = ("standard", "non_standard")
LEAD_TEMPERATURES: tuple[str, ...] = ("hot", "warm", "cold")

MAX_EVIDENCE_CHARS = 1000
MAX_VALUE_CHARS = 500
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_WS_RE = re.compile(r"\s+")


def _norm(text: str) -> str:
    """Collapse whitespace and fold case — and nothing else.

    Deliberately weaker normalisation than ``crm_sync.validators``, which falls
    back to a longest-consecutive-run match because its quotes span
    speaker-labelled turn boundaries in a single blob of Fathom text. Here the
    quote is checked against ONE transcript entry, which is one utterance the
    model read verbatim a moment earlier, so exactness is achievable and worth
    keeping: punctuation is part of what the AE is being shown as proof.
    """
    return _WS_RE.sub(" ", (text or "")).strip().casefold()


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        # A bool where a value belongs means the model answered the wrong
        # question; "True" is never a vendor name or a signal.
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _as_confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        # An absent or unparseable confidence is treated as zero rather than
        # dropped: hard rule 5 says a hedged item beats a missed one, and the
        # low-confidence flag already tells the UI to render it quietly.
        return 0.0


def _as_date(value: Any) -> str | None:
    text = _as_str(value)[:10]
    return text if _DATE_RE.match(text) else None


def _as_sequence(value: Any) -> int | None:
    """Coerce the cited sequence. Returns ``None`` for absent/unusable."""
    if value is None or value == "":
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


class _Dropped(Exception):
    """Internal control flow: this one item is out, with a reason."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


def _check_evidence(
    raw: dict,
    *,
    entries_by_sequence: dict[int, str],
    corpus: str,
    source: str,
) -> tuple[str, int | None]:
    """Return ``(evidence_text, evidence_sequence)`` or raise :class:`_Dropped`.

    Three separate rules, in the order that gives the most useful reason:
    the quote has to exist, the citation has to resolve, and the quote has to
    appear in the thing it cites.
    """
    evidence_text = _as_str(raw.get("evidence_text"))
    if not evidence_text:
        raise _Dropped(DROP_MISSING_EVIDENCE_TEXT, "no evidence_text")

    sequence = _as_sequence(raw.get("evidence_entry_id"))

    if sequence is None:
        # THE FATHOM CARVE-OUT, and it is the single most dangerous line in this
        # module to "tighten". Fathom transcripts have no per-utterance
        # identifiers, so a null citation is LEGAL there and the prompt variant
        # for Fathom explicitly asks for null. A validator that rejects nulls
        # unconditionally does not fail loudly: it silently empties the ENTIRE
        # Fathom fallback and returns an empty wrap-up with no error — and that
        # fallback is the whole plan on a Workspace edition where Meet cannot
        # auto-transcribe. The quote is still mandatory; it is
        # checked against the whole corpus because there is no single line to
        # check it against.
        if source != "fathom":
            raise _Dropped(
                DROP_MISSING_CITATION,
                f"no evidence_entry_id and source is {source!r}, not 'fathom'",
            )
        if _norm(evidence_text) not in corpus:
            raise _Dropped(
                DROP_EVIDENCE_NOT_VERBATIM,
                "quote does not appear anywhere in the transcript",
            )
        return evidence_text[:MAX_EVIDENCE_CHARS], None

    entry_text = entries_by_sequence.get(sequence)
    if entry_text is None:
        # Models hallucinate identifiers. An unresolvable citation cannot be
        # shown to an AE as proof, and it would resolve to no row.
        raise _Dropped(
            DROP_UNKNOWN_EVIDENCE_SEQUENCE,
            f"cited entry [{sequence}] was never supplied",
        )
    if _norm(evidence_text) not in _norm(entry_text):
        raise _Dropped(
            DROP_EVIDENCE_NOT_VERBATIM,
            f"quote is not verbatim in cited entry [{sequence}]",
        )
    return evidence_text[:MAX_EVIDENCE_CHARS], sequence


def _constrain_value(
    heard: str,
    options: list[str],
) -> tuple[str | None, str | None]:
    """Apply the constrained-vocabulary rule to one heard value.

    Returns ``(proposed_value, needs_new_option)``.

    An EMPTY option list means the property is free text on this portal, and
    the value passes through unconstrained — dropping it would silently disable
    every non-enumeration vendor field. A non-empty list is matched
    case-insensitively (HubSpot option values and their labels differ in case
    routinely) and, on a miss, the heard value is routed to triage with
    ``proposed_value`` set to None. It must never reach HubSpot: the whole point
    is that Cubie does not guess which existing option the speaker meant.
    """
    if not options:
        return heard[:MAX_VALUE_CHARS], None
    by_fold = {opt.casefold(): opt for opt in options if opt}
    match = by_fold.get(heard.casefold())
    if match is not None:
        return match, None
    return None, heard[:MAX_VALUE_CHARS]


def _validate_vendor_field(
    raw: dict,
    *,
    allowed_values: dict[str, list[str]],
) -> dict:
    prop = _as_str(raw.get("hubspot_property"))
    if prop not in allowed_values:
        # Property closure. The model may never invent a HubSpot property name,
        # and this is where that rule is enforced against the model: only the
        # properties the caller fetched options for are proposable. When the
        # live schema fetch failed the caller passes an empty mapping, which
        # makes this rule drop every vendor field — fail-closed by design.
        raise _Dropped(
            DROP_UNKNOWN_TARGET_PROPERTY,
            f"{prop!r} is not one of the configured vendor properties",
        )

    heard = _as_str(raw.get("proposed_value"))
    declared_new = _as_str(raw.get("needs_new_option"))
    if not heard and not declared_new:
        raise _Dropped(DROP_NO_VALUE, f"no value heard for {prop}")

    if heard:
        proposed, needs_new = _constrain_value(heard, allowed_values[prop])
    else:
        # The model followed hard rule 3 itself: null value, heard value in
        # needs_new_option. Trust its routing but still cap the string.
        proposed, needs_new = None, declared_new[:MAX_VALUE_CHARS]

    return {
        "target_property": prop,
        "proposed_value": proposed,
        "needs_new_option": needs_new,
        "payload": None,
    }


def _validate_lead(raw: dict) -> dict:
    lead_name = _as_str(raw.get("lead_name"))
    product = _as_str(raw.get("product"))
    if not lead_name and not product:
        raise _Dropped(DROP_NO_VALUE, "lead has neither a name nor a product")
    temperature = _as_str(raw.get("temperature")).lower()
    if temperature not in LEAD_TEMPERATURES:
        temperature = None  # unknown temperature is not fatal; the AE picks one
    return {
        "target_property": None,
        # The lead's identity is what the AE reads on the card; dedupe keys on it.
        "proposed_value": lead_name or product,
        "needs_new_option": None,
        # `temperature` is mapped to HubSpot's `hs_lead_label` (HOT / WARM /
        # COLD) at write time in `backend/routers/calls.py::_write_leads`, and
        # only when it matches one of those three exactly. `product` is NOT
        # yet mapped to `product_interest`: that
        # picklist's options must be matched live, and the option cache in
        # `hubspot_schema.py` constrains companies only today.
        "payload": {
            "product": product or None,
            "lead_name": lead_name or None,
            "temperature": temperature,
            "rationale": _as_str(raw.get("rationale"))[:500] or None,
        },
    }


def _validate_task(raw: dict) -> dict:
    subject = _as_str(raw.get("subject"))
    if not subject:
        raise _Dropped(DROP_NO_VALUE, "task has no subject")
    return {
        "target_property": None,
        "proposed_value": subject[:MAX_VALUE_CHARS],
        "needs_new_option": None,
        "payload": {
            "subject": subject[:MAX_VALUE_CHARS],
            "body": _as_str(raw.get("body"))[:2000] or None,
            # HubSpot tasks take the due date in hs_timestamp, not a creation
            # time. Kept as an ISO date string here; the write layer
            # converts. An unparseable date becomes None rather than dropping
            # the task — "call them back" with no date is still worth showing.
            "due_date": _as_date(raw.get("due_date")),
        },
    }


def _validate_sow_request(raw: dict) -> dict:
    if raw.get("requested") is False:
        raise _Dropped(DROP_NO_VALUE, "sow_request.requested is false")
    kind = _as_str(raw.get("kind")).lower()
    if kind not in SOW_KINDS:
        # Kept, not dropped: an SOW request whose standard/non_standard call is
        # unclear still starts a clock, and the SLA lookup treats an unknown
        # sow_kind as the generic case. Dropping it would lose the commitment.
        kind = None
    return {
        "target_property": None,
        "proposed_value": kind or "requested",
        "needs_new_option": None,
        "payload": {
            "requested": True,
            "kind": kind,
            "client_promised_date": _as_date(raw.get("client_promised_date")),
        },
    }


def _validate_stage_signal(raw: dict) -> dict:
    signal = _as_str(raw.get("signal")).lower()
    if signal not in STAGE_SIGNALS:
        # See the import-time assertion above for why this is a DROP and not a
        # pass-through. A free-text signal reaches SIGNAL_TO_STAGE, misses,
        # produces no exception, and reports success.
        raise _Dropped(
            DROP_UNKNOWN_STAGE_SIGNAL,
            f"{signal!r} is not in the closed stage-signal enum",
        )
    return {
        "target_property": None,
        "proposed_value": signal,
        "needs_new_option": None,
        "payload": {"signal": signal},
    }


def _validate_contract_expiry(raw: dict) -> dict:
    vendor = _as_str(raw.get("vendor"))
    expires_on = _as_date(raw.get("expires_on"))
    if not vendor or not expires_on:
        raise _Dropped(
            DROP_NO_VALUE,
            "contract expiry needs both a vendor and a YYYY-MM-DD date",
        )
    return {
        "target_property": None,
        "proposed_value": expires_on,
        "needs_new_option": None,
        "payload": {"vendor": vendor[:MAX_VALUE_CHARS], "expires_on": expires_on},
    }


#: ``kind`` -> the per-kind field validator. ``sow_request`` is handled from a
#: single object rather than a list; see :func:`validate_extraction`.
_BY_KIND = {
    "vendor_field": _validate_vendor_field,
    "lead": _validate_lead,
    "task": _validate_task,
    "sow_request": _validate_sow_request,
    "stage_signal": _validate_stage_signal,
    "contract_expiry": _validate_contract_expiry,
}

#: The raw-JSON list name for each kind. ``sow_request`` maps to an object.
_RAW_KEY_BY_KIND = {
    "vendor_field": "vendor_fields",
    "lead": "lead_candidates",
    "task": "tasks",
    "sow_request": "sow_request",
    "stage_signal": "stage_signals",
    "contract_expiry": "contract_expiries",
}


def validate_extraction(
    raw: dict,
    *,
    entries_by_sequence: dict[int, str],
    source: str,
    allowed_values: dict[str, list[str]],
) -> tuple[list[dict], list[dict]]:
    """Turn one extraction's raw JSON into ``(kept, dropped)``.

    ``entries_by_sequence`` maps the per-session integer ``sequence`` the prompt
    injected to that entry's text. It is the closure the citation rules check
    against: nothing outside it can be cited, and the caller translates a kept
    item's ``evidence_sequence`` back to the ``call_transcript_entries.id`` PK
    before persisting.

    ``source`` is the transcript source for the whole run — it decides whether a
    null citation is legal. ``allowed_values`` maps each proposable company
    property to its live HubSpot options; an empty list means free text, and a
    property absent from the mapping is not proposable at all.

    Kept items are plain dicts ready for ``call_extraction_items`` fan-out::

        {"kind", "target_object", "target_property", "proposed_value",
         "payload", "evidence_text", "evidence_sequence", "needs_new_option",
         "confidence", "low_confidence"}

    Dropped items carry ``{"kind", "reason", "detail", "raw"}`` — ``reason`` is
    one of the ``DROP_*`` constants, and ``raw`` is the model's original object
    so the discarded item stays legible in ``call_extractions.raw_output``.
    """
    kept: list[dict] = []
    dropped: list[dict] = []

    if not isinstance(raw, dict):
        return [], [
            {
                "kind": None,
                "reason": DROP_NOT_AN_OBJECT,
                "detail": f"extraction payload was {type(raw).__name__}, not an object",
                "raw": None,
            }
        ]

    # One normalised haystack for the whole transcript, built once. Only the
    # null-citation path uses it, but building it per item on a 400-line call
    # would be the difference between a fast validator and a slow one.
    corpus = _norm(" ".join(entries_by_sequence.values()))

    for kind, raw_key in _RAW_KEY_BY_KIND.items():
        section = raw.get(raw_key)
        if section is None:
            continue
        items = [section] if kind == "sow_request" else section
        if not isinstance(items, list):
            dropped.append(
                {
                    "kind": kind,
                    "reason": DROP_NOT_AN_OBJECT,
                    "detail": f"{raw_key} was {type(section).__name__}, not a list",
                    "raw": section,
                }
            )
            continue

        for item in items:
            if not isinstance(item, dict):
                dropped.append(
                    {
                        "kind": kind,
                        "reason": DROP_NOT_AN_OBJECT,
                        "detail": f"{raw_key} entry was {type(item).__name__}",
                        "raw": item,
                    }
                )
                continue
            try:
                evidence_text, sequence = _check_evidence(
                    item,
                    entries_by_sequence=entries_by_sequence,
                    corpus=corpus,
                    source=source,
                )
                if kind == "vendor_field":
                    fields = _validate_vendor_field(item, allowed_values=allowed_values)
                else:
                    fields = _BY_KIND[kind](item)
            except _Dropped as exc:
                dropped.append(
                    {
                        "kind": kind,
                        "reason": exc.reason,
                        "detail": exc.detail,
                        "raw": item,
                    }
                )
                continue
            except Exception as exc:  # noqa: BLE001 — one bad item, not the batch
                logger.warning(
                    "[ccs] unexpected validation failure on a %s item: %s", kind, exc
                )
                dropped.append(
                    {
                        "kind": kind,
                        "reason": DROP_MALFORMED,
                        "detail": str(exc),
                        "raw": item,
                    }
                )
                continue

            confidence = _as_confidence(item.get("confidence"))
            kept.append(
                {
                    "kind": kind,
                    "target_object": TARGET_OBJECT_BY_KIND[kind],
                    "evidence_text": evidence_text,
                    "evidence_sequence": sequence,
                    "confidence": confidence,
                    # KEPT, not dropped, and flagged instead. Hard rule 5: a
                    # low-confidence item shown to a human beats a missed one,
                    # and the wrap-up is one screen its own AE reviews row by
                    # row. The flag is what lets the UI render it quietly —
                    # de-emphasised, unticked — rather than the validator
                    # deciding on the AE's behalf that they would not have
                    # wanted to see it.
                    "low_confidence": confidence < settings.CCS_MIN_CONFIDENCE_SHOW,
                    **fields,
                }
            )

    return kept, dropped

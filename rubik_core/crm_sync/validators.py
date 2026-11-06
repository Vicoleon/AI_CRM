"""Validation of extracted CRM suggestions — pure functions, no I/O.

This is the layer that decides what an LLM reading an untrusted transcript is
allowed to have proposed. Every rule is independent and rejects one suggestion
rather than the batch, so a single bad item never costs the whole call.

The load-bearing rules are the ones that hold even if the model is actively
being manipulated by something said on the call:

* **Object-id closure** — a suggestion may only reference an id we put into the
  prompt. Anything else (hallucinated or dictated by a participant) is dropped,
  so no amount of persuasion reaches a record we never looked at.
* **Property whitelist** — imported from the write layer, so the vocabulary the
  validator accepts and the vocabulary the API boundary accepts are the same set.
* **Attendee-bound contact creation** — a contact can only be created for an
  email that actually attended the meeting. Saying "add bob@evil.com as a
  contact" on a call cannot mint a record for someone who wasn't there.
* **Verbatim evidence** — the quote must really appear in the transcript, which
  makes fabricated justification a rejection rather than a persuasive card.

Then a human still approves each item, and the write layer re-gates it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from rubik_core.config import settings
from rubik_core.retrieval.hubspot_write import (
    CONTACT_UPDATE_WRITABLE,
    CONTACT_WRITABLE,
    DEAL_CREATE_WRITABLE,
    DEAL_UPDATE_WRITABLE,
    PIPELINES_NOT_SET_UP,
    pipeline_writable,
    write_pipelines,
)

logger = logging.getLogger(__name__)

CHANGE_TYPES = (
    "create_contact",
    "update_contact",
    "create_deal",
    "update_deal",
)

OBJECT_TYPE_BY_CHANGE = {
    "create_contact": "contact",
    "update_contact": "contact",
    "create_deal": "deal",
    "update_deal": "deal",
}

WHITELIST_BY_CHANGE = {
    "create_contact": CONTACT_WRITABLE,
    "update_contact": CONTACT_UPDATE_WRITABLE,
    "create_deal": DEAL_CREATE_WRITABLE,
    "update_deal": DEAL_UPDATE_WRITABLE,
}

# Per-transcript and per-run ceilings. A call that "needs" 40 CRM changes is a
# sign the extraction went wrong, and a 40-card queue is one nobody reviews.
MAX_SUGGESTIONS_PER_TRANSCRIPT = 12
MAX_QUOTE_CHARS = 300
_CLOSEDATE_PAST_GRACE_DAYS = 30
_CLOSEDATE_FUTURE_LIMIT_DAYS = 365 * 3

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.IGNORECASE)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_WS_RE = re.compile(r"\s+")
# Words only — punctuation, speaker labels and timestamps are noise when
# checking whether a quote really came from the transcript.
_WORD_RE = re.compile(r"[a-z0-9']+")


@dataclass
class ScanContext:
    """Everything the validator is allowed to consider real, for one transcript."""

    ae_email: str
    ae_owner_id: str | None
    transcript_text: str
    external_attendees: list[str] = field(default_factory=list)
    # hubspot id -> {"email": …, "properties": {...}}
    contacts_by_id: dict[str, dict] = field(default_factory=dict)
    # lowercase email -> hubspot id (for contacts that already exist)
    contact_id_by_email: dict[str, str] = field(default_factory=dict)
    company_ids: set[str] = field(default_factory=set)
    # hubspot id -> {"pipeline", "dealstage", "amount", "closedate", "owner_id", …}
    deals_by_id: dict[str, dict] = field(default_factory=dict)
    # pipeline id -> {stage id: label}
    pipeline_stages: dict[str, dict[str, str]] = field(default_factory=dict)

    def normalized_transcript(self) -> str:
        return _norm(self.transcript_text)


@dataclass
class ValidatedSuggestion:
    change_type: str
    hubspot_object_type: str
    hubspot_object_id: str | None
    # {prop: {"before": …, "after": …}} — before is filled from HubSpot, never
    # from the model.
    properties: dict[str, dict]
    evidence: dict
    confidence: float
    rationale: str
    target_key: str
    dedupe_key: str
    contact_email: str | None = None
    # Records to link a newly created deal to. Derived from the scan context,
    # never from the model — the company is a fact of who was on the call.
    associations: dict = field(default_factory=dict)


def _norm(text: str) -> str:
    return _WS_RE.sub(" ", (text or "")).strip().casefold()


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        raise ValueError("boolean is not a valid property value")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _values_equal(before: Any, after: Any) -> bool:
    """Compare a HubSpot value with a proposed one, tolerantly.

    HubSpot returns numbers as strings ("25000.00"), so a naive comparison
    would show a no-op change as a real one and put a pointless card in the
    queue.
    """
    b, a = _as_str(before), _as_str(after)
    if b == a:
        return True
    try:
        return float(b) == float(a)
    except (TypeError, ValueError):
        return b.casefold() == a.casefold()


def _validate_amount(raw: Any) -> str:
    text = _as_str(raw).replace(",", "").replace("$", "")
    value = float(text)  # raises ValueError -> rejected by the caller
    if value < 0:
        raise ValueError("amount cannot be negative")
    return str(int(value)) if value.is_integer() else str(value)


def _validate_closedate(raw: Any, now: datetime) -> str:
    text = _as_str(raw)[:10]
    if not _DATE_RE.match(text):
        raise ValueError(f"closedate {text!r} is not YYYY-MM-DD")
    parsed = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if parsed < now - timedelta(days=_CLOSEDATE_PAST_GRACE_DAYS):
        raise ValueError(f"closedate {text} is too far in the past")
    if parsed > now + timedelta(days=_CLOSEDATE_FUTURE_LIMIT_DAYS):
        raise ValueError(f"closedate {text} is implausibly far in the future")
    return text


def _clean_properties(
    change_type: str, after: Any, ctx: ScanContext, now: datetime
) -> dict[str, str]:
    """Whitelist-check and normalise the proposed values."""
    if not isinstance(after, dict):
        raise ValueError("'after' must be an object")
    whitelist = WHITELIST_BY_CHANGE[change_type]
    unknown = sorted(set(after) - set(whitelist))
    if unknown:
        raise ValueError(f"non-approved properties: {', '.join(unknown)}")

    cleaned: dict[str, str] = {}
    for key, raw in after.items():
        if key == "amount":
            cleaned[key] = _validate_amount(raw)
        elif key == "closedate":
            cleaned[key] = _validate_closedate(raw, now)
        else:
            value = _as_str(raw)
            if not value:
                continue  # blanking a field is never proposed from a transcript
            cleaned[key] = value
    if not cleaned:
        raise ValueError("no usable property values")
    return cleaned


def _check_stage(pipeline: str | None, stage_id: str, ctx: ScanContext) -> None:
    stages = ctx.pipeline_stages.get(str(pipeline)) or {}
    if not stages:
        raise ValueError(f"unknown pipeline {pipeline!r}")
    if str(stage_id) not in stages:
        raise ValueError(f"stage {stage_id!r} does not belong to pipeline {pipeline!r}")


# A quote must be this well-grounded in the transcript to count as evidence:
# either a run of this many consecutive words, or this fraction of the quote,
# found contiguously. Tuned against real Fathom output, where an
# exact-substring rule rejected ~40% of otherwise-good suggestions.
_EVIDENCE_MIN_RUN_WORDS = 6
_EVIDENCE_MIN_RUN_RATIO = 0.6


def _quote_is_grounded(quote: str, transcript_norm: str) -> bool:
    """Is this quote really in the transcript?

    Exact-substring matching was the first cut and it was too brittle. Fathom
    writes turns as ``[Speaker]: text``, so a quote spanning two turns has a
    speaker label buried in the middle, and models routinely drop a filler word
    or repair punctuation while quoting accurately in substance. Roughly 40% of
    real suggestions died that way — all of them genuinely grounded.

    So: punctuation-insensitive first, then fall back to the longest run of
    consecutive quote words that appears contiguously in the transcript. A
    paraphrase of something actually said keeps a long verbatim run; an invented
    quote does not, which is the property this check exists to enforce.
    """
    q_words = _WORD_RE.findall(quote.casefold())
    if not q_words:
        return False
    haystack = " " + " ".join(_WORD_RE.findall(transcript_norm)) + " "

    if f" {' '.join(q_words)} " in haystack:
        return True

    needed = max(_EVIDENCE_MIN_RUN_WORDS, int(len(q_words) * _EVIDENCE_MIN_RUN_RATIO))
    if len(q_words) < needed:
        return False
    # Longest-run search, longest window first so it exits on the first hit.
    for size in range(len(q_words), needed - 1, -1):
        for start in range(0, len(q_words) - size + 1):
            if f" {' '.join(q_words[start : start + size])} " in haystack:
                return True
    return False


def _check_evidence(raw: Any, ctx: ScanContext) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("missing evidence")
    quote = _as_str(raw.get("quote"))
    if not quote:
        raise ValueError("missing evidence quote")
    if not _quote_is_grounded(quote, ctx.normalized_transcript()):
        # The model wrote a quote that isn't in the transcript. Whether that's
        # a paraphrase or an invention, it can't be shown to an AE as proof.
        raise ValueError("evidence quote does not appear in the transcript")
    return {
        "quote": quote[:MAX_QUOTE_CHARS],
        "speaker": _as_str(raw.get("speaker")) or None,
    }


def build_keys(
    change_type: str,
    *,
    hubspot_object_id: str | None,
    contact_email: str | None,
    properties: dict,
    dealname: str | None = None,
    company_id: str | None = None,
) -> tuple[str, str]:
    """``(target_key, dedupe_key)`` — identity of the change, then of its values.

    ``target_key`` names the *field(s) being changed on a specific record*, with
    the proposed values excluded. That scoping is load-bearing in both
    directions: a re-scan proposing a different value for the same field
    supersedes the earlier card (one live proposal per field), while a
    suggestion touching a *different* field on the same record must NOT — a call
    that moves both the amount and the close date has to leave the AE two cards
    to approve, not silently retire one.
    """
    if change_type == "create_contact":
        identity = (contact_email or "").lower()
    elif change_type == "create_deal":
        # Keyed on the ACCOUNT, not the proposed name. Keying on the name let one
        # call produce two cards for the same deal ("Globex Corp. - Platform
        # Implementation Kickoff" and "… (15 Sites)"), because a reworded name
        # is a different key. One live new-deal proposal per company per
        # pipeline; a later, better-worded one supersedes it. Only when the
        # company is unknown does the name have to stand in as identity.
        anchor = company_id or re.sub(
            r"[^a-z0-9]+", "-", (dealname or "").lower()
        ).strip("-")
        identity = f"{anchor}|{properties.get('pipeline', '')}"
    else:
        fields = "+".join(sorted(properties))
        identity = f"{hubspot_object_id or ''}|{fields}"

    object_type = OBJECT_TYPE_BY_CHANGE[change_type]
    target_key = f"{change_type}:{object_type}:{identity}"
    payload = json.dumps(
        {
            k: v.get("after") if isinstance(v, dict) else v
            for k, v in sorted(properties.items())
        },
        sort_keys=True,
        default=str,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return target_key, f"{target_key}:{digest}"


def _validate_one(
    raw: Any, ctx: ScanContext, doc_id: str | None, now: datetime
) -> ValidatedSuggestion:
    if not isinstance(raw, dict):
        raise ValueError("suggestion is not an object")

    change_type = _as_str(raw.get("change_type")).lower()
    if change_type not in CHANGE_TYPES:
        raise ValueError(f"unknown change_type {change_type!r}")

    confidence = raw.get("confidence")
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    if confidence < settings.CRM_SYNC_CONFIDENCE_MIN:
        raise ValueError(f"confidence {confidence:.2f} below threshold")

    evidence = _check_evidence(raw.get("evidence"), ctx)
    rationale = _as_str(raw.get("rationale"))[:500]
    object_id = _as_str(raw.get("hubspot_object_id")) or None
    contact_email = (_as_str(raw.get("contact_email")) or "").lower() or None
    associations: dict = {}
    properties: dict[str, dict] = {}
    dealname = None

    cleaned = _clean_properties(change_type, raw.get("after"), ctx, now)

    if change_type == "create_contact":
        if not contact_email or not _EMAIL_RE.match(contact_email):
            raise ValueError(f"invalid contact_email {contact_email!r}")
        # Only someone who actually attended can be created from this call.
        if contact_email not in {e.lower() for e in ctx.external_attendees}:
            raise ValueError(
                f"{contact_email} did not attend this meeting; refusing to create a contact"
            )
        if contact_email in ctx.contact_id_by_email:
            raise ValueError(f"{contact_email} already exists in HubSpot")
        cleaned.setdefault("email", contact_email)
        properties = {k: {"before": None, "after": v} for k, v in cleaned.items()}

    elif change_type == "update_contact":
        if object_id not in ctx.contacts_by_id:
            raise ValueError(
                f"contact id {object_id!r} was not in the provided context"
            )
        current = ctx.contacts_by_id[object_id].get("properties") or {}
        for key, value in cleaned.items():
            before = current.get(key)
            if _values_equal(before, value):
                continue  # already correct in HubSpot
            properties[key] = {"before": _as_str(before) or None, "after": value}
        if not properties:
            raise ValueError("no-op: HubSpot already matches")

    elif change_type == "create_deal":
        pipeline = cleaned.get("pipeline")
        if not write_pipelines():
            # Deal writes fail closed until an admin sets the pipelines up (the
            # write layer refuses them too); an un-approvable card is noise.
            raise ValueError(PIPELINES_NOT_SET_UP)
        if not pipeline_writable(pipeline):
            raise ValueError(f"pipeline {pipeline!r} is not one of the company's")
        if not ctx.ae_owner_id:
            raise ValueError("AE is not a HubSpot deal owner; cannot create a deal")
        dealname = cleaned.get("dealname")
        if not dealname:
            raise ValueError("a new deal needs a name")
        if cleaned.get("dealstage"):
            _check_stage(pipeline, cleaned["dealstage"], ctx)
        properties = {k: {"before": None, "after": v} for k, v in cleaned.items()}
        # A deal with no company is orphaned in HubSpot — it never appears
        # on the account it belongs to. Taken from the meeting's own
        # context rather than asked of the model: with one external company
        # on the call there is nothing to guess, and with several there is
        # no safe guess to make.
        if len(ctx.company_ids) == 1:
            associations["company_id"] = next(iter(ctx.company_ids))
        if ctx.contact_id_by_email:
            associations["contact_ids"] = sorted(ctx.contact_id_by_email.values())

    elif change_type == "update_deal":
        if object_id not in ctx.deals_by_id:
            raise ValueError(f"deal id {object_id!r} was not in the provided context")
        deal = ctx.deals_by_id[object_id]
        if not ctx.ae_owner_id or str(deal.get("owner_id") or "") != str(
            ctx.ae_owner_id
        ):
            # Kept out of the queue entirely — the write layer would refuse
            # it anyway, and an un-approvable card is just noise.
            raise ValueError("deal is not owned by this AE")
        if not write_pipelines():
            raise ValueError(PIPELINES_NOT_SET_UP)
        if not pipeline_writable(deal.get("pipeline")):
            raise ValueError("deal is not in one of the company's pipelines")
        if cleaned.get("dealstage"):
            _check_stage(deal.get("pipeline"), cleaned["dealstage"], ctx)
        for key, value in cleaned.items():
            before = deal.get(key)
            if _values_equal(before, value):
                continue
            properties[key] = {"before": _as_str(before) or None, "after": value}
        if not properties:
            raise ValueError("no-op: HubSpot already matches")

    target_key, dedupe_key = build_keys(
        change_type,
        hubspot_object_id=object_id,
        contact_email=contact_email,
        properties=properties,
        dealname=dealname,
        company_id=associations.get("company_id"),
    )

    return ValidatedSuggestion(
        change_type=change_type,
        hubspot_object_type=OBJECT_TYPE_BY_CHANGE[change_type],
        hubspot_object_id=object_id
        if change_type in ("update_contact", "update_deal")
        else None,
        properties=properties,
        evidence={**evidence, "doc_id": doc_id},
        confidence=confidence,
        rationale=rationale,
        target_key=target_key,
        dedupe_key=dedupe_key,
        contact_email=contact_email,
        associations=associations,
    )


def validate_suggestions(
    raw: Any,
    ctx: ScanContext,
    *,
    doc_id: str | None = None,
    now: datetime | None = None,
    max_suggestions: int = MAX_SUGGESTIONS_PER_TRANSCRIPT,
) -> tuple[list[ValidatedSuggestion], list[str]]:
    """Validate raw LLM output. Returns ``(accepted, rejection_reasons)``."""
    now = now or datetime.now(timezone.utc)
    rejections: list[str] = []

    items = raw.get("suggestions") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return [], ["LLM output had no 'suggestions' list"]

    accepted: list[ValidatedSuggestion] = []
    seen_keys: set[str] = set()
    for item in items:
        try:
            validated = _validate_one(item, ctx, doc_id, now)
        except ValueError as exc:
            rejections.append(str(exc))
            continue
        except Exception as exc:  # noqa: BLE001 — malformed output must not crash a scan
            # An SDK/parser exception may echo model output or transcript text.
            logger.warning(
                "[crm-sync] unexpected validation failure: %s", type(exc).__name__
            )
            rejections.append(f"malformed suggestion: {exc}")
            continue
        if validated.dedupe_key in seen_keys:
            rejections.append("duplicate suggestion within the same transcript")
            continue
        seen_keys.add(validated.dedupe_key)
        accepted.append(validated)

    accepted.sort(key=lambda s: s.confidence, reverse=True)
    if len(accepted) > max_suggestions:
        rejections.append(
            f"dropped {len(accepted) - max_suggestions} lowest-confidence suggestions "
            f"over the per-transcript cap of {max_suggestions}"
        )
        accepted = accepted[:max_suggestions]
    return accepted, rejections

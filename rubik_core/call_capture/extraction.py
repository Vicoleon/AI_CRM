"""Run the extractor over one captured call and persist what it proposed.

One direct, structured Gemini call per transcript chunk through
``get_genai_client()`` — no ADK and no ``response_schema``. Both would be wrong
here for the same reason twice over: the ADK Runner does not go through
``rubik_core.models.get_genai_client``, so ``@attribute_to`` cannot see a single
ADK token and an ADK extractor would report $0 spend forever; and
``response_schema`` appears nowhere in this repo, whose canonical pattern is
prompt-stated shape -> ``response_mime_type="application/json"`` as a belt ->
tolerant ``safe_json()`` -> **all** validation in Python afterwards.

The pipeline, in order, and every step is a layer the next one does not trust:

1. ``prompts.py`` states the shape and fences the transcript as untrusted data.
2. ``validators.py`` re-checks every citation, quote, property and enum value
   against what we actually supplied.
3. This module persists proposals with the live HubSpot value beside each one,
   so the AE approves a diff grounded in fetched state rather than in the
   model's claim about it.
4. A human accepts individual items, and ``retrieval/hubspot_write.py`` gates
   them again at the API boundary.

**Call scripts add a fifth thing this run reports: what the call did NOT
cover.** When the AE attached product checklists at start time, the prompt names
the facts those checklists expect and the model reports the ones it can quote an
answer for. Absence is never asked of the model — it is computed in
``scripts.coverage_for`` from the checklist itself, so a fact nobody discussed
reaches the AE as the question they should have asked rather than as silence.
The whole path is additive: an unscripted call gets the same prompt bytes, the
same validators and the same ``raw_output`` keys it got before this existed.

**Re-runnability is a feature.** ``call_extractions`` stores the raw output, the
model id, the prompt version and the schema version, so a bad prompt release is
both traceable and re-runnable against the stored entries. Re-extraction marks
any still-pending proposal from an earlier run ``superseded`` rather than
editing it, which keeps the model's accuracy measurable.

**Three outcomes, three different shapes**, because the one thing this feature
cannot afford is a total failure that reads as a quiet call:

* **no signal** — every chunk answered, nothing survived validation. A
  ``CallExtraction`` row with ``raw_output["chunks_failed"] == 0`` and no items;
  the session advances to ``extracted`` and the AE gets an empty wrap-up they
  can close in seconds. Normal, not an error.
* **partial failure** — some chunks answered and some raised. The row is
  persisted with the usable proposals and ``0 < chunks_failed < chunks_total``,
  so a degraded wrap-up is legible as degraded rather than as complete.
* **total failure** — EVERY chunk raised (Vertex 429s for ten minutes, or
  ``CCS_EXTRACTION_MODEL`` pinned to a retired id). :class:`ExtractionFailed` is
  raised: no row is written, the session's status is left where it was, and the
  sweep's retry gate picks it up next pass. Returning an empty extraction here
  instead would tell twelve AEs the AI found nothing on twelve calls while the
  operator's report said everything worked — the silent zero this repo has been
  bitten by before.

This function makes network calls (Gemini, HubSpot) while holding the ``db``
Session it was handed. A caller in a request path must therefore open its own
short ``SessionLocal()`` inside ``run_in_threadpool`` rather than passing a
``Depends(get_db)`` session straight through — dev Cloud SQL is
``db-custom-1-3840`` with max-instances 3 and has been exhausted once already.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from rubik_core.ai_providers.registry import (
    IncompatibleModelRoute,
    ModelRoute,
    resolve_model_route,
)
from rubik_core.ai_providers.runtime import (
    ProviderRequestError,
    generate_text_on_route,
    parse_json_response,
)
from rubik_core.call_capture import prompts, validators
from rubik_core.company.profile import get_company_profile
from rubik_core.config import settings
from rubik_core.cost.usage import attribute_to
from rubik_core.database.call_capture import (
    CallExtraction,
    CallExtractionItem,
    CallSession,
    CallTranscriptEntry,
)
from rubik_core.models import get_genai_client, safe_json
from rubik_core.sales.model_config import model_for

logger = logging.getLogger(__name__)

#: AI-cost feature key. Registered in ``_FEATURE_LABELS``
#: (``backend/routers/admin.py``, owned by the wiring agent) as
#: "Sales Call Capture" in the same change that added this module — without the
#: label the spend renders as a title-cased slug at the bottom of the AI Cost
#: tab, which is how two live keys already ended up unlabelled.
COST_FEATURE = "call_capture_extraction"

#: Drop reason for a ``script_coverage`` entry naming a fact nobody attached.
#: Its own code rather than ``DROP_UNKNOWN_TARGET_PROPERTY``, because the two
#: tune in opposite directions: an unknown property means the vendor-field
#: config is wrong, while a stream of unknown fact keys means the model is
#: inventing checklist items and the CALL SCRIPT block is not being read.
DROP_UNKNOWN_FACT_KEY = "unknown_fact_key"

#: The ``dropped`` bucket label for script-coverage entries. Not one of
#: ``ITEM_KINDS`` on purpose — nothing here becomes a ``call_extraction_items``
#: row (see :func:`script_fact_items`) — but ``raw_output["dropped"]`` groups by
#: this string and an unlabelled bucket is one nobody can tune against.
SCRIPT_FACT_KIND = "script_fact"

#: Rough character budget per model call. Deliberately a character count and
#: not a token count: there is no tokeniser in this repo, and a 4:1 estimate
#: that errs small costs one extra call while erring large costs a truncated
#: transcript with no error. ~120k chars is comfortably inside the PRO context
#: window while leaving room for the system prompt and the output contract.
_CHUNK_CHARS = 120_000

#: Lines of overlap between chunks. A vendor field is usually established over
#: two or three turns ("who do you use for that?" / "BSI"), so a hard cut can
#: leave the answer in one chunk and the question in the next, and neither
#: chunk has enough to propose anything. Overlap costs tokens; a missed vendor
#: field costs the feature its point.
_CHUNK_OVERLAP_LINES = 12


class ExtractionFailed(RuntimeError):
    """Every chunk of one call failed at the model boundary — nothing was read.

    Deliberately an exception rather than an empty return value. An extraction
    that proposes nothing is a real, common, *successful* outcome, so a total
    model failure that returns one is invisible: the row exists, the session
    reads ``extracted``, the wrap-up opens blank, the sweep logs
    ``extractions_run`` and nothing ever re-extracts the call. Raising puts the
    run in the caller's failure bucket, leaves ``call_sessions.status`` where it
    was — ``transcript_ready``, which is exactly what the sweep's retry gate
    looks for — and writes no row, so the next pass re-runs against the stored
    transcript entries. Re-extraction is cheap and must stay possible.

    ``chunks_failed`` / ``chunks_total`` are carried so a caller can report a
    dedicated bucket ("the model was down") separately from a bad transcript,
    and ``errors`` carries the distinct messages for the log line.
    """

    def __init__(
        self,
        *,
        session_id: str,
        chunks_total: int,
        chunks_failed: int,
        errors: list[str],
    ) -> None:
        self.session_id = session_id
        self.chunks_total = chunks_total
        self.chunks_failed = chunks_failed
        self.errors = list(errors)
        detail = "; ".join(dict.fromkeys(self.errors)) or "no detail"
        super().__init__(
            f"extraction produced nothing for session {session_id}: "
            f"all {chunks_failed} of {chunks_total} chunk(s) failed at the "
            f"model boundary ({detail})"
        )


def _vendor_properties() -> list[str]:
    """The company properties this run asks the model to fill.

    Workspace HubSpot settings decide them (default / extended vendor fields: the
    company's own vendor fields, and *which* matter is the sales director's
    call, so answering it is a config change rather than a deploy). The write
    layer's ``company_writable()`` is the outer fence: a configured name it
    would refuse (a HubSpot built-in that drives automation, X-Ray's research
    link) is dropped here rather than proposed and then rejected at approval
    time.
    """
    from rubik_core.retrieval.hubspot_write import (
        company_writable,
        configured_vendor_fields,
    )

    writable = company_writable()
    kept, rejected = [], []
    for name in configured_vendor_fields():
        (kept if name in writable else rejected).append(name)
    if rejected:
        logger.warning(
            "[ccs] ignoring vendor field(s) the write layer refuses: %s",
            ", ".join(sorted(set(rejected))),
        )
    return kept


def _transcript_source(entries: list[CallTranscriptEntry]) -> str:
    """Which prompt variant this run gets.

    ``fathom`` only when EVERY entry is Fathom-sourced. A mixed session has at
    least one citable entry, and the Fathom variant tells the model to null out
    every citation — which would throw away the citations it could have made.
    Meet and ``manual`` entries both carry real sequences, so both cite.
    """
    sources = {e.source for e in entries}
    return "fathom" if sources == {"fathom"} else "meet"


def _render_lines(entries: list[CallTranscriptEntry]) -> list[str]:
    """One prompt line per entry: ``[<sequence>] Speaker: text``.

    The bracketed number is the per-session integer ``sequence``, not the row's
    uuid4-hex PK. A 32-char hex repeated across hundreds
    of lines is expensive in tokens and easy for a model to corrupt by a
    character; the validator translates the cited sequence back to the PK.
    """
    return [
        f"[{e.sequence}] {e.speaker_name or e.speaker_email or 'Unknown'}: {e.text}"
        for e in entries
    ]


def _chunk(lines: list[str]) -> list[list[str]]:
    """Split rendered lines into overlapping chunks under the char budget."""
    chunks: list[list[str]] = []
    current: list[str] = []
    size = 0
    for line in lines:
        # A single line over budget cannot be split without breaking the
        # citation contract (half an utterance is not the utterance the model
        # would be citing), so it goes through whole and over budget. Meet caps
        # an entry's text at 10K words, so this is bounded.
        if current and size + len(line) > _CHUNK_CHARS:
            chunks.append(current)
            # Carry the overlap forward only while it still leaves room to make
            # progress. Without the half-budget ceiling, twelve very long
            # entries could form an overlap that is itself over budget, and the
            # loop would then emit overlap-only chunks forever.
            overlap = current[-_CHUNK_OVERLAP_LINES:]
            if sum(len(x) for x in overlap) > _CHUNK_CHARS // 2:
                overlap = []
            current = overlap
            size = sum(len(x) for x in current)
        current.append(line)
        size += len(line)
    if current:
        chunks.append(current)
    return chunks


@attribute_to(COST_FEATURE)
def _extract_chunk(
    *,
    system: str,
    prompt: str,
    model_id: str,
    route: ModelRoute | None = None,
) -> Any:
    """One JSON-returning model call. Returns the parsed payload, or raises.

    ``@attribute_to`` sits HERE, on the function that calls Vertex, not on
    ``run_extraction`` and not on a router. ``current_feature`` is a contextvar:
    a tag set at an entry point does not survive a hand-off to a worker thread,
    and this call runs behind ``run_in_threadpool`` from the wrap-up path and
    inside a Cloud Run Job from the sweep. Decorating the worker re-establishes
    the bucket in whichever thread runs it. (That mistake produced the 75%
    "Unattributed" row on the AI Cost tab once already, when the AI-Priorities
    batch lost its tag at a thread hop.)

    ``response_mime_type`` asks for JSON and ``safe_json`` still tolerates a
    fence — belt and braces, because the alternative is a fenced response being
    reported as "the model returned garbage".
    """
    if route is not None:
        if route.provider == "vertex_managed":
            # The native managed path does not pass through the adapter's
            # revalidation. Client routes are checked by generate_text_on_route.
            current = resolve_model_route(COST_FEATURE, "json", default_model=model_id)
            if current != route:
                raise IncompatibleModelRoute(
                    "Workspace AI route changed during extraction"
                )
        else:
            response = generate_text_on_route(
                route,
                prompt,
                json_mode=True,
                system_instruction=system,
                temperature=0.2,
            )
            try:
                parsed = parse_json_response(response)
            except RuntimeError:
                raise ProviderRequestError(
                    "Selected AI provider returned invalid JSON"
                ) from None
            if not isinstance(parsed, dict):
                raise ProviderRequestError(
                    "Selected AI provider returned invalid JSON object"
                )
            return parsed

    from google.genai import types

    from rubik_core.gemini_tuning import generation_config_for

    config = types.GenerateContentConfig(
        system_instruction=system,
        temperature=0.2,
        response_mime_type="application/json",
    )
    response = get_genai_client().models.generate_content(
        model=model_id,
        contents=prompt,
        # Carries the right thinking knob for whichever family CCS_EXTRACTION_MODEL
        # pins. 2.5 Pro cannot disable thinking and gets the config back
        # untouched; a 3.x pin needs thinking_level, and a numeric budget is
        # silently ignored there.
        config=generation_config_for(model_id, config),
    )
    return safe_json(response.text or "")


def _model_id() -> str:
    """The pinned model id for extraction.

    PRO rather than FLASH because a missed vendor field or a miscited quote
    costs more than the tokens do at this call volume. Never a ``-latest``
    alias: the alias hides which family answered, and comparing two extractions
    across a silent family change is worse than having no comparison.
    """
    return model_for("call_extraction")


def _allowed_values(properties: list[str]) -> dict[str, list[str]]:
    """Live HubSpot enumeration options for the vendor properties.

    Fetched ONCE per run and passed down — account-wide lookups per record are
    what burn the rate limit. A failure returns an empty mapping, which makes
    the validator drop every vendor-field item: fail-closed is right here,
    because proposing a free-text value into an enumeration property is exactly
    the heard-spelling pollution the schema cache exists to prevent. The
    rest of the wrap-up — leads, tasks, the SOW request, stage signals — is
    unaffected.
    """
    if not properties:
        return {}
    from rubik_core.call_capture import hubspot_schema

    try:
        return hubspot_schema.allowed_values_for(properties)
    except Exception as exc:  # noqa: BLE001 — a schema miss must not lose the call
        logger.warning(
            "[ccs] vendor property options unavailable (%s); "
            "vendor fields will be skipped for this extraction",
            exc,
        )
        return {}


def _current_company_values(
    company_id: str | None, properties: list[str]
) -> dict[str, str | None]:
    """Read the company's CURRENT values for the proposable properties.

    One search call for all of them, not one per field. These populate
    ``current_value`` and ``is_overwrite``, which is what drives the mandatory
    "warning: this changes an existing value" UI — the feature never
    blind-overwrites a non-null HubSpot value, and a column that is
    NULL because nobody read it looks identical to one that is NULL because the
    field is genuinely empty.
    """
    if not company_id or not properties:
        return {}
    from rubik_core.retrieval.hubspot_api import HubSpotAPIClient
    from rubik_core.retrieval.hubspot_write import _with_backoff
    from rubik_core.tenancy.hubspot_config import get_hubspot_config

    if not get_hubspot_config().pipeline_ids:
        return {}

    try:
        client = HubSpotAPIClient()
        if str(company_id) not in client.fully_owned_deal_associations(
            "companies", [str(company_id)]
        ):
            return {}
        results = _with_backoff(
            client.search_all,
            "companies",
            [
                {
                    "propertyName": "hs_object_id",
                    "operator": "EQ",
                    "value": str(company_id),
                }
            ],
            list(properties),
        )
    except Exception as exc:  # noqa: BLE001
        # Not fatal, but it must be visible: every vendor field on this call
        # will now read as "currently empty", so is_overwrite is False and the
        # AE sees no change warning. Say so rather than letting the wrap-up look
        # confident about state it never read.
        logger.warning(
            "[ccs] could not read current HubSpot values for company %s (%s); "
            "overwrite warnings will be missing on this wrap-up",
            company_id,
            exc,
        )
        return {}
    if not results:
        return {}
    record = next(
        (row for row in results if str(row.get("id")) == str(company_id)), None
    )
    if record is None:
        return {}
    props = record.get("properties") or {}
    return {name: (props.get(name) or None) for name in properties}


def _merge(chunk_results: list[list[dict]]) -> list[dict]:
    """Merge per-chunk kept items, keeping the most confident duplicate.

    Deduped on ``(kind, target_property, proposed_value)``. The
    overlap between chunks is there precisely so a fact spanning a cut is seen
    twice, which means duplicates are the expected case, not an anomaly. The
    higher-confidence copy wins because it is the one that saw more of the
    surrounding conversation.
    """
    best: dict[tuple, dict] = {}
    for items in chunk_results:
        for item in items:
            key = (
                item["kind"],
                item.get("target_property"),
                item.get("proposed_value"),
            )
            existing = best.get(key)
            if existing is None or item["confidence"] > existing["confidence"]:
                best[key] = item
    return list(best.values())


# ── Call scripts: what this call was SUPPOSED to establish ───────────────────
#
# The idea the whole checklist rests on: if something is not asked or not
# covered on the call, no amount of automation afterwards recovers it. A
# perfect extractor over an incomplete conversation still produces an incomplete
# HubSpot record, and the AE cannot tell, because the wrap-up looks clean.
#
# Everything below is ADDITIVE. A call with no attached script takes the exact
# path it took before this code existed: the prompt is byte-identical (see
# prompts.py), no coverage is validated, and no script key is written to
# ``raw_output``. There is no empty-coverage state.


def _script_sections(db, call_session: CallSession) -> list[dict]:
    """The product checklists this call was started against — ``[]`` when none.

    Read from ``call_session_scripts`` by ``script_id``, NOT by re-resolving the
    product's newest approved script. The AE reads the panel before the call and
    the wrap-up grades the call against what they read; a script approved
    between the call and the wrap-up must not retroactively add a gap for a
    question nobody could have known to ask.

    **Fail-soft, and this is the one place in the module that swallows a
    database error on purpose.** The extraction is the valuable artifact and the
    checklist is an overlay on it. A missing table on an un-migrated environment,
    or a script row deleted out from under a session, must cost the call its
    coverage block — not its vendor fields, its leads and its SOW clock.
    """
    try:
        from rubik_core.call_capture import scripts as call_scripts
        from rubik_core.database.call_scripts import CallScript, CallSessionScript

        attached = (
            db.execute(
                select(CallSessionScript)
                .where(CallSessionScript.call_session_id == call_session.id)
                .order_by(CallSessionScript.attached_at)
            )
            .scalars()
            .all()
        )
        if not attached:
            return []
        by_id = {
            script.id: script
            for script in db.execute(
                select(CallScript)
                .options(selectinload(CallScript.items))
                .where(CallScript.id.in_([row.script_id for row in attached]))
            )
            .scalars()
            .all()
        }
        return [
            call_scripts.script_payload(by_id[row.script_id])
            for row in attached
            if row.script_id in by_id
        ]
    except Exception as exc:  # noqa: BLE001 — a checklist miss must not lose the call
        logger.warning(
            "[ccs] could not load the call scripts for session %s (%s); "
            "this wrap-up will report no coverage",
            call_session.id,
            exc,
        )
        return []


def _product_keys(db) -> list[str]:
    """The workspace's scripted product keys; ``[]`` on any failure.

    Fail-soft for the same reason as :func:`_script_sections`: the keys only
    steer how a lead names its product, and losing them must not lose the call.
    """
    try:
        from rubik_core.call_capture import scripts as call_scripts

        return call_scripts.active_product_keys(db)
    except Exception as exc:  # noqa: BLE001 — a naming hint must not lose the call
        logger.warning(
            "[ccs] could not read the scripted product keys (%s)",
            type(exc).__name__,
        )
        return []


def _fact_index(script_sections: list[dict]) -> dict[str, str]:
    """``fact_key -> product_key`` across the attached checklists. First wins.

    Two products legitimately share a fact key — ``commencement_date`` belongs to
    every engagement the company sells — and that is not an ambiguity to resolve here:
    ``coverage_for`` matches per section, so one answer ticks the box on every
    checklist that asked for it, which is the right answer. The mapping exists
    only to tag the stored fact with A product, and the first attached one is as
    good as any.
    """
    index: dict[str, str] = {}
    for section in script_sections:
        product_key = str(section.get("product_key") or "").strip().lower()
        for item in section.get("items") or []:
            fact_key = str(item.get("fact_key") or "").strip().lower()
            if fact_key and fact_key not in index:
                index[fact_key] = product_key
    return index


def _validate_script_coverage(
    raw: Any,
    *,
    fact_index: dict[str, str],
    entries_by_sequence: dict[int, str],
    corpus: str,
    source: str,
) -> tuple[list[dict], list[dict]]:
    """Check one chunk's ``script_coverage`` the way every other item is checked.

    The evidence rule is ``validators._check_evidence`` ITSELF, called rather
    than restated. It carries the Fathom carve-out — a null citation is legal
    only when the transcript has no per-utterance identifiers — and that
    validator's own comment calls it "the single most dangerous line in this
    module to tighten". A second copy of it here would drift, and the drift would
    show up as an empty Fathom coverage block with no error. (A private helper
    across modules in the same package, precedent: ``backend/routers/calls.py``
    imports ``validators._constrain_value`` for the same reason — one definition
    of a rule the UI and the extractor must agree on.)

    **Fact-key closure**, the counterpart to the vendor-field property closure:
    a key that is not on the checklist we injected is discarded. Coverage is
    matched by exact key, so an invented one could only ever tick nothing — but
    it would still be stored, and a stored fact nobody asked for is the kind of
    thing that later reads as evidence the call covered something.

    ``product_key`` is taken from ``fact_index``, never from the model's own
    ``product_key`` field. The fact key already determines the product; asking
    the model for it twice only creates a way for the two to disagree.
    """
    kept: list[dict] = []
    dropped: list[dict] = []
    if not isinstance(raw, dict):
        return kept, dropped
    section = raw.get("script_coverage")
    if section is None:
        return kept, dropped
    if not isinstance(section, list):
        dropped.append(
            {
                "kind": SCRIPT_FACT_KIND,
                "reason": validators.DROP_NOT_AN_OBJECT,
                "detail": f"script_coverage was {type(section).__name__}, not a list",
                "raw": section,
            }
        )
        return kept, dropped

    for entry in section:
        if not isinstance(entry, dict):
            dropped.append(
                {
                    "kind": SCRIPT_FACT_KIND,
                    "reason": validators.DROP_NOT_AN_OBJECT,
                    "detail": f"script_coverage entry was {type(entry).__name__}",
                    "raw": entry,
                }
            )
            continue
        fact_key = validators._as_str(entry.get("fact_key")).lower()
        if fact_key not in fact_index:
            dropped.append(
                {
                    "kind": SCRIPT_FACT_KIND,
                    "reason": DROP_UNKNOWN_FACT_KEY,
                    "detail": f"{fact_key!r} is not on any checklist this call attached",
                    "raw": entry,
                }
            )
            continue
        try:
            evidence_text, sequence = validators._check_evidence(
                entry,
                entries_by_sequence=entries_by_sequence,
                corpus=corpus,
                source=source,
            )
        except validators._Dropped as exc:
            dropped.append(
                {
                    "kind": SCRIPT_FACT_KIND,
                    "reason": exc.reason,
                    "detail": exc.detail,
                    "raw": entry,
                }
            )
            continue
        except Exception as exc:  # noqa: BLE001 — one bad fact, not the batch
            logger.warning(
                "[ccs] unexpected script-coverage validation failure (%s)",
                type(exc).__name__,
            )
            dropped.append(
                {
                    "kind": SCRIPT_FACT_KIND,
                    "reason": validators.DROP_MALFORMED,
                    "detail": type(exc).__name__,
                    "raw": entry,
                }
            )
            continue
        kept.append(
            {
                "fact_key": fact_key,
                "product_key": fact_index[fact_key],
                # A covered fact with no value is a real outcome — "yes, they
                # need the initial risk assessment" is the whole answer — so an
                # empty value must NOT unmark the fact as discussed.
                "value": validators._as_str(entry.get("value"))[
                    : validators.MAX_VALUE_CHARS
                ]
                or None,
                "evidence_text": evidence_text,
                "evidence_sequence": sequence,
                "confidence": validators._as_confidence(entry.get("confidence")),
            }
        )
    return kept, dropped


def _merge_script_facts(chunk_results: list[list[dict]]) -> list[dict]:
    """One entry per ``fact_key``, keeping the most confident chunk's answer.

    Same rule as :func:`_merge` and for the same reason: chunks overlap so a
    fact spanning a cut is seen twice, and the copy that saw more of the
    surrounding conversation is the one to keep. Keyed on the fact alone, not on
    (fact, value) — two different values for one fact is a disagreement to
    resolve, not two findings to show the AE.
    """
    best: dict[str, dict] = {}
    for facts in chunk_results:
        for fact in facts:
            key = fact["fact_key"]
            existing = best.get(key)
            if existing is None or fact["confidence"] > existing["confidence"]:
                best[key] = fact
    return list(best.values())


def script_fact_items(extraction: CallExtraction | None) -> list[dict]:
    """This run's script facts, shaped like extraction items for ``scripts.py``.

    **Why these are not rows in ``call_extraction_items``.** That table's
    ``kind`` is CHECK-constrained to ``ITEM_KINDS`` and a script fact is not one
    of them: it is not a proposed HubSpot write, nothing approves it, and nothing
    writes it anywhere. Adding a seventh kind would put "did the call cover
    this?" into the approval queue's vocabulary and give the wrap-up a group of
    items with no decision to make. So they live in ``raw_output``, which is
    already this run's record of everything it read.

    The returned dicts are duck-typed to what ``scripts.coverage_for`` and
    ``scripts.unscripted_products`` already read — both accept a Mapping — so the
    wrap-up hands them straight over beside the persisted rows with no adapter.
    ``status`` is stated explicitly because both functions filter on it.

    Tolerant of a row written by any earlier version of this module: a missing
    or malformed ``script_coverage`` yields ``[]`` rather than raising, because
    this is read on the wrap-up screen an AE is waiting for.
    """
    raw = getattr(extraction, "raw_output", None)
    if not isinstance(raw, dict):
        return []
    facts = raw.get("script_coverage")
    if not isinstance(facts, list):
        return []
    out: list[dict] = []
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        fact_key = str(fact.get("fact_key") or "").strip().lower()
        if not fact_key:
            continue
        out.append(
            {
                "fact_key": fact_key,
                "proposed_value": fact.get("value"),
                "evidence_text": fact.get("evidence_text"),
                "evidence_entry_id": fact.get("evidence_entry_id"),
                "confidence": fact.get("confidence"),
                "status": "proposed",
                "payload": {
                    "product": fact.get("product_key"),
                    "fact_key": fact_key,
                },
            }
        )
    return out


def _supersede_pending(db, call_session: CallSession) -> int:
    """Retire still-pending proposals from an earlier run of the same call.

    ``superseded`` rather than deleted or overwritten: an AE who already
    accepted or rejected an item keeps that decision (only ``proposed`` rows are
    touched), and the earlier row stays readable next to the new one so a prompt
    change can be evaluated against what it replaced.
    """
    rows = (
        db.execute(
            select(CallExtractionItem).where(
                CallExtractionItem.call_session_id == call_session.id,
                CallExtractionItem.status == "proposed",
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.status = "superseded"
    return len(rows)


def _open_provisional_sow_commitment(
    db,
    call_session: CallSession,
    item: CallExtractionItem,
) -> None:
    """Start the SOW clock from when the client asked, not from the wrap-up.

    The commitment opens here, ``provisional=True`` — the clock runs but
    escalation is disarmed, so an unreviewed AI guess never pages the sales
    manager while the clock also does not wait for a human to get round to the
    wrap-up. ``confirm()`` is deliberately NOT called here: at extraction time
    nothing has been accepted. That happens in the wrap-up approval path.

    Imported lazily. ``cockpit.commitment_engine`` reaches back into the call
    tables for evidence, so a module-level import here would close a cycle
    between the two packages.

    Safe to reach on a re-extraction: ``open_commitment`` dedupes on the
    commitments table's own partial unique constraints — the first caller
    creates the row, a later one attaches its evidence and the earliest
    ``opened_at`` wins — so this does not have to know whether it has run
    before, and must not try to guess.

    The open runs in a SAVEPOINT. The commitment is a best-effort SIDE EFFECT of
    extraction, never a precondition for it, and swallowing a database error on
    the outer transaction does not achieve that: two overlapping sweeps racing
    for the same deal lose the partial unique constraint, ``IntegrityError``
    poisons the Session, and every subsequent flush fails — so catching it here
    would discard the whole extraction, including the items that were already
    valid and already flushed. ``begin_nested()`` undoes the commitment insert
    and nothing else.
    """
    from rubik_core.cockpit import commitment_engine

    payload = item.payload or {}
    promised_at = _parse_date(payload.get("client_promised_date"))
    try:
        with db.begin_nested():
            commitment = commitment_engine.open_commitment(
                db,
                kind="sow",
                owner_email=call_session.ae_email,
                company_id=call_session.hubspot_company_id,
                deal_id=call_session.hubspot_deal_id,
                # When the client asked — the whole reason this opens in
                # extraction rather than on approval.
                opened_at=call_session.ended_at,
                source="call",
                # The transcript ENTRY, not the session: the evidence is one
                # sentence somebody said, and a session id does not resolve to
                # it.
                evidence_ref=item.evidence_entry_id,
                evidence_text=item.evidence_text,
                sow_kind=payload.get("kind"),
                # Passed IN, not set afterwards: breach_at is computed from
                # (opened_at, sla_hours, promised_at) at open, so assigning
                # promised_at after the fact leaves breach_at stale.
                promised_at=promised_at,
                provisional=True,
            )
    except Exception as exc:  # noqa: BLE001 — a wrap-up without a clock beats no wrap-up
        # The savepoint is already rolled back by the context manager, so the
        # Session is usable and the extraction rows survive.
        logger.warning(
            "[ccs] could not open a provisional SOW commitment for session %s (%s)",
            call_session.id,
            type(exc).__name__,
        )
        return
    logger.info(
        "[ccs] opened provisional SOW commitment %s from session %s",
        getattr(commitment, "id", "?"),
        call_session.id,
    )


def _parse_date(value: Any) -> datetime | None:
    """``YYYY-MM-DD`` -> naive datetime, or ``None``.

    Naive because every DateTime column in this repo is naive
    (``rg 'timezone=True'`` returns zero hits) and mixing an aware value into a
    naive column is a comparison that raises at the worst possible moment.
    """
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def run_extraction(db, call_session: CallSession) -> CallExtraction | None:
    """Extract proposals from one call's stored transcript entries.

    Returns the persisted :class:`CallExtraction`, or ``None`` when there was
    nothing to extract — a suppressed (confidential) call, or a session with no
    transcript entries. Both are first-class outcomes, not errors: "nothing to
    wrap up" is normal, and conflating it with a
    failure fills the ops bucket with normal behaviour.

    Raises :class:`ExtractionFailed` when every chunk failed at the model
    boundary. That is the one case that must NOT look like a quiet call: no row
    is written and ``call_session.status`` is left untouched, so the caller
    counts it in its failure bucket and the session is retried next pass. A
    partial failure still returns an extraction, with
    ``raw_output["chunks_failed"]`` non-zero. See the module docstring.

    Rows are added and flushed; **the caller commits.** A partially written
    extraction is worse than none, and the caller is the only thing that knows
    whether the surrounding sweep succeeded.
    """
    from rubik_core.call_capture.provenance import session_source_visible

    if not session_source_visible(call_session):
        raise RuntimeError("Call session CRM source is unavailable")
    if call_session.capture_consent == "suppressed":
        # A confidential call has no entries, no asset and no participants
        # either. Returning early is belt and braces.
        logger.info("[ccs] session %s is suppressed; no extraction", call_session.id)
        return None

    entries = (
        db.execute(
            select(CallTranscriptEntry)
            .where(CallTranscriptEntry.call_session_id == call_session.id)
            .order_by(CallTranscriptEntry.sequence)
        )
        .scalars()
        .all()
    )
    if not entries:
        logger.info(
            "[ccs] session %s has no transcript entries; nothing to extract",
            call_session.id,
        )
        return None

    source = _transcript_source(entries)
    vendor_properties = _vendor_properties()
    allowed_values = _allowed_values(vendor_properties)
    # Only ask about properties we actually have options for; the validator
    # enforces the same closure, and asking about a property whose items are
    # about to be dropped wastes the model's attention on the rest of the call.
    proposable = list(allowed_values)

    # The checklists the AE attached before the call. Empty for every call
    # nobody scripted, which keeps the prompt, the contract and raw_output
    # exactly as they were.
    script_sections = _script_sections(db, call_session)
    fact_index = _fact_index(script_sections)
    # The company the call was held by, and the product keys its own call
    # scripts use, so a lead names a product the way a checklist does.
    profile = get_company_profile(db)
    product_keys = _product_keys(db)

    lines = _render_lines(entries)
    chunks = _chunk(lines)
    entries_by_sequence = {e.sequence: e.text for e in entries}
    entry_id_by_sequence = {e.sequence: e.id for e in entries}
    default_model_id = _model_id()
    route = resolve_model_route(COST_FEATURE, "json", default_model=default_model_id)
    model_id = route.model

    # One normalised haystack for the whole call, built once and only when a
    # checklist is attached. ``validate_extraction`` builds its own privately;
    # the Fathom null-citation path is the only thing that reads it.
    corpus = (
        validators._norm(" ".join(entries_by_sequence.values())) if fact_index else ""
    )

    raw_chunks: list[Any] = []
    kept_per_chunk: list[list[dict]] = []
    script_facts_per_chunk: list[list[dict]] = []
    dropped: list[dict] = []
    chunk_errors: list[str] = []
    last_error: Exception | None = None
    started = time.monotonic()

    for index, chunk_lines in enumerate(chunks, start=1):
        if not session_source_visible(call_session):
            raise RuntimeError("Call session CRM source is unavailable")
        label = f"part {index} of {len(chunks)}" if len(chunks) > 1 else None
        system, prompt = prompts.extraction_prompt(
            transcript_block="\n".join(chunk_lines),
            source=source,
            allowed_values=allowed_values,
            stage_signals=validators.STAGE_SIGNALS,
            company_name=(call_session.attrs or {}).get("company_name"),
            ae_email=call_session.ae_email,
            call_date=(
                call_session.ended_at.date().isoformat()
                if call_session.ended_at
                else None
            ),
            chunk_label=label,
            script_sections=script_sections,
            profile=profile,
            product_keys=product_keys,
        )
        try:
            raw = _extract_chunk(
                system=system, prompt=prompt, model_id=model_id, route=route
            )
        except IncompatibleModelRoute:
            # A mid-run route switch invalidates the complete extraction. Do
            # not persist a row assembled from different provider policies.
            raise
        except Exception as exc:  # noqa: BLE001
            # One failed chunk of several still leaves a usable wrap-up, so the
            # failure is recorded in raw_output and the run continues. The count
            # is kept because the ALL-failed case is decided below and is a
            # different outcome entirely — see ExtractionFailed.
            # SDK and parser errors can echo transcript text on both managed
            # Vertex and client-owned routes. Persist only bounded diagnostics.
            safe_error = (
                "Selected AI provider request failed"
                if isinstance(exc, ProviderRequestError)
                else type(exc).__name__
            )
            logger.warning(
                "[ccs] extraction chunk %s/%s failed for session %s: %s",
                index,
                len(chunks),
                call_session.id,
                safe_error,
            )
            raw_chunks.append({"error": safe_error, "chunk": index})
            chunk_errors.append(safe_error)
            # Do not retain an SDK exception as the eventual cause: its
            # traceback may embed model output or request content.
            last_error = RuntimeError(safe_error)
            continue

        raw_chunks.append(raw)
        kept, chunk_dropped = validators.validate_extraction(
            raw,
            entries_by_sequence=entries_by_sequence,
            source=source,
            allowed_values=allowed_values,
        )
        kept_per_chunk.append(kept)
        dropped.extend(chunk_dropped)

        if fact_index:
            facts, facts_dropped = _validate_script_coverage(
                raw,
                fact_index=fact_index,
                entries_by_sequence=entries_by_sequence,
                corpus=corpus,
                source=source,
            )
            script_facts_per_chunk.append(facts)
            dropped.extend(facts_dropped)

    latency_ms = int((time.monotonic() - started) * 1000)

    if chunks and len(chunk_errors) == len(chunks):
        # NOTHING was read. Persisting an empty extraction here would be
        # indistinguishable from a call with no signal on it, and the session
        # would advance to "extracted" and never be re-extracted. Raise instead:
        # no row, no status change, and the caller's retry gate still sees
        # "transcript_ready" next pass.
        logger.error(
            "[ccs] extraction TOTAL FAILURE for session %s: all %s chunk(s) "
            "failed at the model boundary (model=%s); leaving status=%s for retry",
            call_session.id,
            len(chunks),
            model_id,
            call_session.status,
        )
        raise ExtractionFailed(
            session_id=call_session.id,
            chunks_total=len(chunks),
            chunks_failed=len(chunk_errors),
            errors=chunk_errors,
        ) from last_error

    if chunk_errors:
        # Partial: a usable wrap-up, but a demonstrably incomplete one. Said out
        # loud here and recorded on the row below, because an AE reading a
        # wrap-up cannot tell a quiet stretch of call from a chunk that never
        # reached the model.
        logger.warning(
            "[ccs] extraction for session %s is PARTIAL: %s of %s chunk(s) failed",
            call_session.id,
            len(chunk_errors),
            len(chunks),
        )

    items = _merge(kept_per_chunk)
    script_facts = _merge_script_facts(script_facts_per_chunk)
    if not session_source_visible(call_session):
        raise RuntimeError("Call session CRM source is unavailable")

    # The model's unedited JSON, plus the run's own inputs and every discarded
    # item with its reason. A validator drop that is not legible here is a drop
    # nobody can tune against.
    raw_output: dict[str, Any] = {
        "source": source,
        "chunks": raw_chunks,
        "dropped": dropped,
        "vendor_properties": proposable,
        # Zero here is the load-bearing value: it is what separates "the call
        # really had no signal on it" from "part of the call was never read". A
        # reader that cannot make that distinction reports both as a clean empty
        # wrap-up.
        "chunks_total": len(chunks),
        "chunks_failed": len(chunk_errors),
    }
    if route.provider != "vertex_managed":
        raw_output["ai_provider"] = route.provider
        raw_output["billing_owner"] = route.billing_owner
    if script_sections:
        # Added only for a scripted call. An unscripted one gets neither key —
        # not an empty list — so "this call had no checklist" and "this call had
        # a checklist and covered none of it" stay distinguishable, and a run
        # from before scripts existed reads identically to one after.
        raw_output["script_products"] = [
            section["product_key"] for section in script_sections
        ]
        raw_output["script_coverage"] = [
            {
                "fact_key": fact["fact_key"],
                "product_key": fact["product_key"],
                "value": fact["value"],
                "evidence_text": fact["evidence_text"],
                # sequence -> the transcript row's uuid PK, the same translation
                # the persisted items get, so the wrap-up
                # can link a covered fact to the utterance that covered it.
                "evidence_entry_id": (
                    entry_id_by_sequence.get(fact["evidence_sequence"])
                    if fact["evidence_sequence"] is not None
                    else None
                ),
                "evidence_sequence": fact["evidence_sequence"],
                "confidence": fact["confidence"],
            }
            for fact in script_facts
        ]

    extraction = CallExtraction(
        id=uuid.uuid4().hex,
        call_session_id=call_session.id,
        model=model_id,
        prompt_version=settings.CCS_PROMPT_VERSION or prompts.PROMPT_VERSION,
        schema_version=prompts.SCHEMA_VERSION,
        raw_output=raw_output,
        latency_ms=latency_ms,
    )
    db.add(extraction)
    db.flush()

    superseded = _supersede_pending(db, call_session)
    current_values = _current_company_values(
        call_session.hubspot_company_id, proposable
    )

    sow_items: list[CallExtractionItem] = []
    for item in items:
        kind = item["kind"]
        target_object = item["target_object"]
        target_id = None
        if target_object == "companies":
            target_id = call_session.hubspot_company_id
        elif target_object == "deals":
            target_id = call_session.hubspot_deal_id

        current_value = None
        is_overwrite = False
        if kind == "vendor_field" and item.get("proposed_value") is not None:
            current_value = current_values.get(item["target_property"])
            is_overwrite = (
                current_value is not None and current_value != item["proposed_value"]
            )

        row = CallExtractionItem(
            id=uuid.uuid4().hex,
            extraction_id=extraction.id,
            call_session_id=call_session.id,
            kind=kind,
            target_object=target_object,
            target_id=target_id,
            target_property=item.get("target_property"),
            proposed_value=item.get("proposed_value"),
            current_value=current_value,
            payload=item.get("payload"),
            evidence_text=item["evidence_text"],
            # sequence -> uuid PK. None for a Fathom item
            # with no citation, which the validator has already established is
            # legal only for that source.
            evidence_entry_id=entry_id_by_sequence.get(item["evidence_sequence"])
            if item["evidence_sequence"] is not None
            else None,
            needs_new_option=item.get("needs_new_option"),
            confidence=item["confidence"],
            is_overwrite=is_overwrite,
            status="proposed",
        )
        db.add(row)
        if kind == "sow_request":
            sow_items.append(row)
    db.flush()

    # After the flush, so evidence_entry_id is a real persisted FK by the time
    # the commitment records it as its evidence reference.
    for row in sow_items:
        _open_provisional_sow_commitment(db, call_session, row)

    # Advance the session's own state, but never backwards: a re-extraction of
    # an already-wrapped call must not reopen it in the AE's queue.
    if call_session.status in ("ended", "transcript_ready"):
        call_session.status = "extracted"

    logger.info(
        "[ccs] session %s extracted: %s proposed, %s dropped, %s superseded, "
        "%s script fact(s) over %s checklist(s), %s chunk(s) (%s failed), %sms",
        call_session.id,
        len(items),
        len(dropped),
        superseded,
        len(script_facts),
        len(script_sections),
        len(chunks),
        len(chunk_errors),
        latency_ms,
    )
    return extraction

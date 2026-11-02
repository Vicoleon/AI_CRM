"""Call Scripts — resolving them, and reporting what a call did not cover.

The engine behind the call checklists. ``rubik_core/database/call_scripts.py``
is the columns; this module is every read and write over them, and one pure
function that turns a script plus an extraction into the wrap-up's gap list.

The rule the whole feature rests on: if something is not asked or not covered on
the call, no automation afterwards can recover it. A perfect extractor over an
incomplete conversation still produces an incomplete HubSpot record, and the AE
cannot tell, because the wrap-up looks clean. :func:`coverage_for` is the
function that makes it not look clean — **absence is the product here, not an
error case**.

**Nothing in this module authors content.** No product list, no fact keys, no
questions. The acceptance criterion is that a sales lead adds a product
script through a form, with no deploy, and a module that ships a default
vocabulary is an engineer authoring their content — accurate for one quarter and
wrong forever after. Every product key this file knows about arrives from the
database or from the caller.

**Three reads, three write-shapes, and one pure function.** The pure one is
:func:`coverage_for`: no session, no I/O, no clock. It is where the feature's
judgement lives, so it is the part that has to be testable without a database
and without an extraction run.

**What this module does NOT do: swallow errors.** ``scan_day`` is documented as
a pure read that never raises, and the script block on a calendar row is
additive and fail-soft — but that wrapper belongs to ``scan_day``, which is the
only place that knows the day still has to render. A helper here that returned
``[]`` on a database error would make a broken script table look exactly like an
AE who has not picked a product yet, on every screen, forever. These functions
are total over ordinary conditions (no row, no items, empty input) and let a
real failure propagate to a caller that can say so.

**Email and key folding.** ``users.email`` is stored verbatim from the Google ID
token, so every identity value is folded on the way in and on the way out.
:func:`_fold` is character-for-character ``backend/routers/calls.py::_fold``
rather than an import of it (``backend`` imports ``rubik_core``, never the
reverse) and rather than ``graph.keys.normalize_email`` (which returns ``None``
for anything without an ``@``, turning a lookup key into a null one). The model
layer's ``@validates`` hooks fold with the same two operations, so writer and
reader cannot drift. Product keys and fact keys are folded for the same reason
and a bigger one: a casing mismatch on a join key does not raise, it reports a
fact as *not discussed* when it was discussed, which is the one false alarm that
teaches an AE to ignore the gap list.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from rubik_core.database.call_scripts import (
    CallScript,
    CompanyProductDefault,
    ScriptItem,
)

logger = logging.getLogger(__name__)

#: Extraction items in these states are invisible to coverage and to the
#: unscripted-product signal. ``superseded`` is a proposal from an EARLIER run
#: of the same call (``extraction._supersede_pending``) — counting it would mark
#: a fact covered from a retired guess, with a stale value beside it.
#: ``rejected`` is the AE saying in as many words that the proposal is wrong;
#: reporting the fact as covered anyway would overrule the only human who
#: listened to the call. Everything else counts, including ``proposed``, because
#: the wrap-up is computed before anybody has decided anything.
COVERAGE_IGNORED_STATUSES: frozenset[str] = frozenset({"superseded", "rejected"})

#: Shape gate for a product key arriving from the MODEL rather than from the
#: database. The extraction prompt asks for one of a closed product enum, but
#: ``validators._validate_lead`` deliberately does not enforce that enum (it
#: keeps whatever was heard so nothing is silently dropped) and does not cap the
#: string. So a hallucinated sentence can reach :func:`unscripted_products`, and
#: an unfiltered one becomes *"split 'they mentioned wanting a review of their
#: whole account base' into its own lead?"* — a wrong prompt of that kind is
#: worse than none, because it is the AE's first impression of a feature whose
#: entire value is being trusted about what a call missed. A product key is one
#: to four short slug-ish words; anything else produces no signal at all.
_MAX_PRODUCT_KEY_CHARS = 64
_PRODUCT_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*(?: [a-z0-9_-]+){0,3}$")


# ── Folding and shape ─────────────────────────────────────────────────────────


def _fold(email: str | None) -> str:
    """Mirror of ``backend/routers/calls.py::_fold`` — see the module docstring."""
    return (email or "").strip().lower()


def _fold_key(value: Any) -> str:
    """Fold a product key or fact key the way the model layer stores it.

    Exactly ``strip().lower()``, which is what the ``@validates`` hooks on
    ``CallScript.product_key``, ``ScriptItem.fact_key`` and
    ``CallSessionScript.product_key`` apply on every write. A reader that
    normalized *differently* from the writer — slugifying spaces, stripping
    punctuation — would silently stop matching rows a human can see in the
    table, which is worse than not normalizing at all.

    Non-strings fold to ``""`` rather than to ``str(value)``: a stray ``True``
    or ``12`` in a model payload is a wrong answer, not a key.
    """
    if not isinstance(value, str):
        return ""
    return value.strip().lower()


def _get(obj: Any, name: str) -> Any:
    """Read ``name`` off an ORM row or a plain mapping.

    :func:`coverage_for` is the one function in this feature that has to be
    unit-testable with no database at all, so it accepts either ``ScriptItem`` /
    ``CallExtractionItem`` rows or the dicts a test (or a future in-memory
    caller) hands it. Nothing else here is duck-typed.
    """
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _payload_value(item: Any, name: str) -> Any:
    """Read ``name`` from an extraction item's JSON payload, if it has one."""
    payload = _get(item, "payload")
    if isinstance(payload, Mapping):
        return payload.get(name)
    return None


def _model_product_key(value: Any) -> str | None:
    """Fold a product key the MODEL emitted, or ``None`` when it is not one.

    See :data:`_PRODUCT_KEY_RE`. This gate exists only on the extraction side —
    a key that came out of ``call_scripts`` is authored by a human and is taken
    as given.
    """
    key = _fold_key(value)
    if not key or len(key) > _MAX_PRODUCT_KEY_CHARS:
        return None
    if not _PRODUCT_KEY_RE.match(key):
        return None
    return key


def _ordered_keys(values: Iterable[Any] | None) -> list[str]:
    """Fold, drop the empties, dedupe — and keep the caller's order.

    Order is not cosmetic: it is the order the AE picked the products in, and
    the panel renders its sections in that order.
    """
    out: list[str] = []
    for value in values or []:
        key = _fold_key(value)
        if key and key not in out:
            out.append(key)
    return out


# ── Resolving scripts ─────────────────────────────────────────────────────────


def active_script_for(db: Any, product_key: str | None) -> CallScript | None:
    """The newest APPROVED, active version of one product's script, or ``None``.

    **Approval, not activity, is what makes a script live**: an AI-generated
    script never ships as authoritative. Scripts are mined from the
    historic transcript corpus by a batch job, so ``source="mined"`` rows land in
    this table written by a machine with ``is_active`` already true — that column
    is the author's on/off switch and costs nothing to set. ``approved_by`` is a
    named human's signature. A reader that gated on ``is_active`` alone would put
    a model's guess in front of an AE as the company's own checklist, and would do it
    invisibly: the panel renders, the questions look plausible, and the first
    sign of trouble is an SOW built on a fact nobody at the company agreed to ask for.
    So a product with nothing but drafts resolves to ``None`` — no panel, which
    is the honest answer — and an unapproved v3 never outranks an approved v2.

    The equality on ``product_key`` is deliberately NOT wrapped in
    ``func.lower()``: ``ck_call_scripts_product_key_lower`` makes the stored
    value already folded, so the plain comparison is equivalent AND can use
    ``ix_call_scripts_product_active``, which a ``lower()`` expression cannot on
    Postgres.

    Items are eager-loaded because every caller of this function immediately
    renders them (:func:`script_payload`) or matches against them
    (:func:`coverage_for`), and the lazy load would be a second round trip while
    an AE waits for the calendar screen.
    """
    key = _fold_key(product_key)
    if not key:
        return None
    return (
        db.execute(
            select(CallScript)
            .options(selectinload(CallScript.items))
            .where(
                CallScript.product_key == key,
                CallScript.is_active.is_(True),
                CallScript.approved_by.isnot(None),
            )
            .order_by(CallScript.version.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )


def active_product_keys(db: Any) -> list[str]:
    """Every product key with a live (approved, active) script, sorted.

    The call-capture extraction offers these as the keys a lead's ``product``
    should use, so a lead and a checklist about the same product join. Empty
    until somebody approves a script, which is the default for a new
    workspace.
    """
    rows = db.execute(
        select(CallScript.product_key)
        .where(
            CallScript.is_active.is_(True),
            CallScript.approved_by.isnot(None),
        )
        .distinct()
    ).all()
    return sorted({_fold_key(key) for (key,) in rows if _fold_key(key)})


def scripts_for_products(
    db: Any, product_keys: Sequence[str] | None
) -> list[CallScript]:
    """The live script for each requested product, in the order requested.

    ONE query for N products rather than N calls to :func:`active_script_for` —
    this runs inside the calendar day scan, once per row, and the AE is waiting.
    The newest approved version per product is picked in Python from the rows the
    single read returned; version counts per product are in the low single
    digits (a script is forked on approval-edit, not on every keystroke).

    Products with no approved script are simply absent from the result. That is
    the normal early state — the tables ship EMPTY and the product list is
    not finished — and it must not be an error: the AE still gets their call
    screen, just without a checklist for that product.
    """
    keys = _ordered_keys(product_keys)
    if not keys:
        return []
    rows = (
        db.execute(
            select(CallScript)
            .options(selectinload(CallScript.items))
            .where(
                CallScript.product_key.in_(keys),
                CallScript.is_active.is_(True),
                CallScript.approved_by.isnot(None),
            )
            .order_by(CallScript.version)
        )
        .scalars()
        .all()
    )
    best: dict[str, CallScript] = {}
    for row in rows:
        key = _fold_key(row.product_key)
        current = best.get(key)
        if current is None or (row.version or 0) >= (current.version or 0):
            best[key] = row
    return [best[key] for key in keys if key in best]


def script_payload(script: CallScript) -> dict:
    """One panel SECTION: the product, and the facts this call has to surface.

    The shape the calendar row's ``scripts.sections[]`` block is pinned to. It
    carries ``prompt_text`` (what the AE reads) and ``fact_key`` (what the
    extractor and :func:`coverage_for` match on) as separate values because they
    change independently — a sales lead rewording a question must not silently
    retarget the extractor.

    ``required`` defaults to ``True`` for an item that never says: the column
    default is ``True`` for the same reason, and the asymmetry is the point. A
    wrongly-required fact shows a gap the AE can dismiss in a second; a wrongly-
    optional one never shows a gap at all, which is the failure this feature
    exists to remove.
    """
    return {
        "script_id": script.id,
        "product_key": _fold_key(script.product_key),
        "name": script.name or _fold_key(script.product_key),
        "items": [
            {
                "fact_key": _fold_key(item.fact_key),
                "prompt_text": item.prompt_text or "",
                "required": True if item.required is None else bool(item.required),
                "help_text": item.help_text or None,
            }
            for item in (script.items or [])
        ],
    }


# ── Remembering the AE's pick per client ──────────────────────────────────────


def remembered_products(
    db: Any, ae_email: str | None, company_id: str | None
) -> list[str]:
    """What this AE picked for this client last time — ``[]`` when nothing yet.

    This is the whole of the "self-service" feel and it is deliberately cheap.
    The better idea — read the products off the lead — is not buildable: there
    is no HubSpot **Leads read** anywhere in this repo (leads are write-only
    through ``_write_leads``, itself gated off pending a permission the
    workspace still has to grant). The weekly call with the same client
    defaults to the same products every week, and the AE only touches the control
    when something changes.

    Read through ``func.lower()`` to match ``ix_call_company_product_defaults_
    email_lower``, which exists precisely so this fold does not degrade to a
    sequential scan on Postgres.

    The returned keys are NOT filtered against what has an approved script.
    "What the AE picked" and "what the system can show a checklist for" are two
    different facts and the panel needs both — a remembered product whose script
    was retired should still show as selected, with no section, rather than
    silently unpicking itself.
    """
    email = _fold(ae_email)
    company = (company_id or "").strip()
    if not email or not company:
        return []
    row = (
        db.execute(
            select(CompanyProductDefault).where(
                func.lower(CompanyProductDefault.ae_email) == email,
                CompanyProductDefault.hubspot_company_id == company,
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        return []
    stored = row.product_keys
    if not isinstance(stored, list):
        # A hand-edited row, or a writer that bypassed this module. Not worth
        # raising over — but say so, because the AE's picks silently stopped
        # being remembered and nothing else would ever mention it.
        logger.warning(
            "[ccs] call_company_product_defaults %s holds %s, not a list; "
            "ignoring the remembered products",
            row.id,
            type(stored).__name__,
        )
        return []
    return _ordered_keys(stored)


def remember_products(
    db: Any,
    ae_email: str | None,
    company_id: str | None,
    product_keys: Sequence[str] | None,
) -> None:
    """Upsert this AE's pick for this client. Flushes; the caller commits.

    **An empty ``product_keys`` CLEARS the remembered pick.** That is the right
    semantics for an AE who deselected everything, and a trap for a caller that
    passes the request field's default: ``POST /api/calls/start`` must skip this
    call entirely when the client omitted ``product_keys``, or an older UI (or a
    retry that drops the field) silently wipes a pick the AE made last week. The
    router knows the difference between *omitted* and *empty*; this function
    cannot, so the decision lives there and the rule is written here.

    A missing AE or company id is a no-op rather than an error: the ad-hoc call
    path has no company binding at all (``call_sessions.hubspot_company_id`` is
    nullable for exactly that reason), and there is nothing to key a memory on.
    Writing a row under ``""`` would collide every AE's ad-hoc picks into one
    shared row.

    The stored list is REASSIGNED, never appended to. SQLAlchemy does not track
    in-place mutation of a plain ``JSON`` column, so ``row.product_keys.append``
    emits no UPDATE and the write vanishes with no error.
    """
    email = _fold(ae_email)
    company = (company_id or "").strip()
    if not email or not company:
        logger.debug(
            "[ccs] not remembering products: ae=%r company=%r", ae_email, company_id
        )
        return
    keys = _ordered_keys(product_keys)
    row = (
        db.execute(
            select(CompanyProductDefault).where(
                func.lower(CompanyProductDefault.ae_email) == email,
                CompanyProductDefault.hubspot_company_id == company,
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        row = CompanyProductDefault(
            id=uuid.uuid4().hex,
            ae_email=email,
            hubspot_company_id=company,
            product_keys=keys,
        )
        db.add(row)
    else:
        row.product_keys = keys  # reassign — see the docstring
    db.flush()


# ── Coverage: what the call actually surfaced, and what it did not ────────────


def _better(current: Any, candidate: Any) -> Any:
    """Keep the more confident of two extraction items matching one fact.

    Extraction already dedupes across chunk overlaps on
    ``(kind, target_property, proposed_value)`` and keeps the most confident
    copy, so two *surviving* items can still land on the same fact with two
    different values ("60 hours" and "about 60 hours each"). Preferring the
    higher confidence is the same rule ``extraction._merge`` applies, so the
    value shown in the gap list is the one the wrap-up shows elsewhere. Ties and
    missing confidences keep the first seen, which is transcript order.
    """
    if current is None:
        return candidate
    current_conf = _get(current, "confidence")
    candidate_conf = _get(candidate, "confidence")
    if isinstance(current_conf, (int, float)) and isinstance(
        candidate_conf, (int, float)
    ):
        return candidate if candidate_conf > current_conf else current
    return current


def _target_property(hubspot_target: Any) -> str:
    """The property half of a script item's ``"object.property"`` target.

    The OBJECT half is deliberately discarded. HubSpot's object names are
    written both ways in this codebase and in the CRM UI — ``company`` and
    ``companies``, ``deal`` and ``deals`` — and a script authored by a sales lead
    through a form will use whichever spelling they type. Matching on the object would
    turn that into "not discussed" for a fact the call plainly covered, which is
    the false alarm that costs the gap list its credibility. Two different
    objects carrying the same property name on one call is a far smaller and far
    less damaging collision.
    """
    target = _fold_key(hubspot_target)
    if not target:
        return ""
    return target.rsplit(".", 1)[-1].strip()


def _extraction_value(item: Any) -> Any:
    """What to show beside a covered fact.

    ``edited_value`` first: if the AE corrected the proposal before approving it,
    their correction is the fact, not the model's first guess. ``None`` is a
    legitimate answer for a matched item — a vendor value outside the property's
    live options comes back with ``proposed_value=None`` and the heard string in
    ``needs_new_option`` — and it must NOT flip the fact to *not discussed*: it
    was discussed, and only the CRM's vocabulary is missing.
    """
    edited = _get(item, "edited_value")
    if edited not in (None, ""):
        return edited
    return _get(item, "proposed_value")


def coverage_for(
    items: Iterable[Any] | None, extraction_items: Iterable[Any] | None
) -> list[dict]:
    """Per script item: was this fact surfaced, with what value, on whose words.

    **PURE** — no session, no network, no clock. One script's items in, one list
    of dicts out, in the order given (``CallScript.items`` is already ordered by
    ``sequence``). This is the reason the feature exists:

    ::

        ✓ hours per session · 60   "about 60 hours each"
        ✗ rate                     not discussed

    **An unmatched fact is a FINDING, not missing data.** Every script item
    always produces a row. There is no "unknown" state and nothing is dropped:
    the whole point is that the AE sees the two gaps at the wrap-up rather than
    a sales lead discovering them three days later while trying to draft the SOW.

    Two ways an extraction item can claim a fact, both exact, neither fuzzy:

    1. an explicit ``fact_key`` — on the item or in its JSON payload. Nothing
       emits one **today**: ``call_extraction_items`` has no such column and the
       prose output contract in ``prompts.py`` does not ask for one. It is
       matched first so that the day the contract gains script facts (a new
       block in ``_OUTPUT_CONTRACT`` plus a ``PROMPT_VERSION`` / ``SCHEMA_VERSION``
       bump) coverage improves with no change here.
    2. the script item's ``hubspot_target`` property against the extraction
       item's ``target_property``. This one WORKS TODAY against the shipped
       extractor: a script fact targeting ``company.current_vendor`` is satisfied
       by the vendor-field proposal for ``current_vendor``, quote and citation
       included.

    No text similarity, no "the model probably meant this" match. A wrong ✓ is
    strictly worse than a ✗ the AE can see and dismiss: the ✓ tells them the call
    covered something it did not, which is the exact failure the checklist was
    added to prevent.
    """
    by_fact: dict[str, Any] = {}
    by_property: dict[str, Any] = {}
    for item in extraction_items or []:
        if _fold_key(_get(item, "status")) in COVERAGE_IGNORED_STATUSES:
            continue
        fact = _fold_key(_get(item, "fact_key")) or _fold_key(
            _payload_value(item, "fact_key")
        )
        if fact:
            by_fact[fact] = _better(by_fact.get(fact), item)
        prop = _fold_key(_get(item, "target_property"))
        if prop:
            by_property[prop] = _better(by_property.get(prop), item)

    out: list[dict] = []
    for item in items or []:
        fact = _fold_key(_get(item, "fact_key"))
        match = by_fact.get(fact) if fact else None
        if match is None:
            prop = _target_property(_get(item, "hubspot_target"))
            if prop:
                match = by_property.get(prop)
        required = _get(item, "required")
        out.append(
            {
                "fact_key": fact,
                "prompt_text": _get(item, "prompt_text") or "",
                "required": True if required is None else bool(required),
                "found": match is not None,
                "value": _extraction_value(match) if match is not None else None,
                "evidence_entry_id": (
                    _get(match, "evidence_entry_id") if match is not None else None
                ),
            }
        )
    return out


# ── The valuable one: a product discussed with no script loaded ───────────────


def _live_fact_key_products(db: Any, fact_keys: Sequence[str]) -> dict[str, set[str]]:
    """``fact_key -> {product_key}`` across every approved, active script.

    Versions are not deduped: two approved versions of one product share a
    ``product_key``, so they collapse into the same set entry and cannot make a
    fact look ambiguous on their own.
    """
    rows = db.execute(
        select(CallScript.product_key, ScriptItem.fact_key)
        .join(ScriptItem, ScriptItem.script_id == CallScript.id)
        .where(
            ScriptItem.fact_key.in_(list(fact_keys)),
            CallScript.is_active.is_(True),
            CallScript.approved_by.isnot(None),
        )
    ).all()
    index: dict[str, set[str]] = {}
    for product_key, fact_key in rows:
        index.setdefault(_fold_key(fact_key), set()).add(_fold_key(product_key))
    return index


def unscripted_products(
    db: Any,
    extraction_items: Iterable[Any] | None,
    attached_product_keys: Sequence[str] | None,
) -> list[str]:
    """Products the call discussed that this call was never scripted for.

    This is the sentence the whole multi-product design is for: *"onboarding
    support was discussed but this call is only scripted for the annual review —
    split it into its own lead?"* The product rule is that lead↔deal is 1:1, so
    a call surfacing three products should produce three leads — and AEs often
    do not split them. That exact miss, failing to split leads into multiple
    products, is the failure this workstream exists to fix. Turning it into one
    click at the wrap-up is worth more than the checklist itself.

    **How an extraction item is decided to "belong to" a product — honestly,
    because the data does not simply say.** ``call_extraction_items`` has no
    product column. Two signals, in order, and nothing else:

    1. ``payload["product"]``, which ``validators._validate_lead`` writes for
       every **lead candidate** from the closed product enum in the extraction
       prompt. This is a real, shipped, explicit signal — the model naming a
       product it heard interest in — and it carries the ``evidence_text`` that
       justified it. It is also the *right* population: a vendor field carries no
       product and produces nothing here, which is correct, because "they use
       Northwind for payroll" is firmographic data about a happy incumbent, not
       a lead to split out. Because it comes from the model rather than from a
       human, it goes through :data:`_PRODUCT_KEY_RE` first.
    2. a ``fact_key`` on the item that belongs to **exactly one** approved,
       active script. A fact_key shared by two products, or known to none, yields
       NOTHING — no guess, no "closest" product. (Nothing emits fact keys today;
       see :func:`coverage_for` signal 1.)

    That is the entire classifier, and it is deliberately narrow. A wrong *"split
    this into a new lead"* prompt is worse than no prompt: it is an AE being told
    to create a lead for a product nobody discussed, at the end of a call, by a
    feature asking to be trusted about what it heard. Everything with no explicit
    product and no unambiguous fact key contributes nothing, silently and on
    purpose.

    An approved script is NOT required for a product to be reported. It is
    reported because it was *discussed* and *not attached* — splitting the lead
    is worth doing whether or not a sales lead has authored that product's checklist
    yet, and requiring a script would mute the feature exactly while the tables
    are still filling up, which is now.

    ``db`` is touched only when signal 2 has something to resolve, so on today's
    extractions this function performs ZERO queries.
    """
    attached = set(_ordered_keys(attached_product_keys))
    candidates: list[str] = []
    unresolved_facts: list[str] = []

    for item in extraction_items or []:
        if _fold_key(_get(item, "status")) in COVERAGE_IGNORED_STATUSES:
            continue
        key = _model_product_key(_payload_value(item, "product"))
        if key:
            if key not in candidates:
                candidates.append(key)
            continue
        fact = _fold_key(_get(item, "fact_key")) or _fold_key(
            _payload_value(item, "fact_key")
        )
        if fact and fact not in unresolved_facts:
            unresolved_facts.append(fact)

    if unresolved_facts and db is not None:
        index = _live_fact_key_products(db, unresolved_facts)
        for fact in unresolved_facts:
            products = index.get(fact) or set()
            if len(products) != 1:
                # Zero: a fact nobody scripted. Two or more: a fact several
                # products share ("commencement_date"). Neither is evidence
                # about WHICH product came up, so neither produces a prompt.
                continue
            key = next(iter(products))
            if key and key not in candidates:
                candidates.append(key)

    return [key for key in candidates if key not in attached]

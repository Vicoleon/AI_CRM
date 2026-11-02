"""Rewrite ONE section of a Meeting Prep brief, on the reader's request.

The full brief is one Gemini call that writes every section at once
(``generate_meeting_prep_analysis`` in ``backend/routers/home.py``). This is
the narrow sibling: the reader points at one section ("shorter", "focus on
pricing", or just "try again") and Cubie rewrites that section alone, from the
same research the brief was built from, while the rest of the brief stays as
it is.

Three things this module holds on to, because they are what make a rewrite
safe to put next to a hand-checked brief:

* **Same grounding rules, same persona.** The system prompt carries the full
  brief's GROUNDING RULES word for word (a drift test pins them against
  ``home.py``); the rules and per-section clauses for the newer signal blocks
  come from the same ``signals.prompt_parts`` the full brief uses; and the
  persona follows ``is_battle_brief`` exactly as the full brief does. A rewrite
  must never be a looser model than the original.
* **The instruction is a style request, never a licence.** "Add their budget"
  when the sources hold no budget produces a section that says the budget is
  not in the records — not a number.
* **Every block and clause is conditional on its data.** Rows saved before a
  source existed (no ``signal_blocks``, no ``prior_meetings_block``), Deep
  Prep rows (only ``title`` + ``past_meetings_report``) and a missing bundle
  all still work; a missing block adds neither a header nor a clause that
  points at it.

The worker returns the section already normalized to the shape
``MeetingPrepResponse`` stores (``normalize_section_value``) plus the sources
the model says it relied on. Persisting the new version is the caller's job
(the version chain), as is appending ``sources`` to ``source_evidence``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from rubik_core.brand import PRODUCT_NAME
from rubik_core.company.profile import (
    CompanyProfile,
    company_context_block,
    company_name_or_default,
    get_company_profile,
)
from rubik_core.cost.usage import attribute_to
from rubik_core.meeting_prep import signals
from rubik_core.meeting_prep.sections import (
    SECTIONS,
    SectionSpec,
    get_section,
    is_battle_brief,
    normalize_instruction,
    normalize_section_value,
    stakeholders_from_value,
)

logger = logging.getLogger(__name__)

# What the reader sees when the rewrite fails. Reader-safe on purpose: the real
# error is logged, never surfaced.
MSG_UNAVAILABLE = f"{PRODUCT_NAME} couldn't rewrite this section right now — try again."
MSG_UNUSABLE = f"{PRODUCT_NAME} returned a section it could not use — try again."

MAX_SOURCES = 20
_MAX_SOURCE_CHARS = 500

# Hard bound on the model call. The page gives up after 180s
# (GENERATION_TIMEOUT_MS in frontend/src/api/meetingPrep.ts); stopping well
# before that means the server never saves a version after the reader has been
# told their current one is unchanged.
REWRITE_TIMEOUT_S = 150.0

# How much of each OTHER section the prompt carries. Enough for the rewrite to
# stay consistent with the rest of the brief and not repeat it; not the whole
# brief a second time.
_OTHER_PROSE_CHARS = 1_200
_OTHER_ITEM_CHARS = 300
_OTHER_ITEMS_MAX = 12

# A section that may legitimately come back empty: "nothing outstanding" is a
# true answer for what the reader owes. Any other empty rewrite is a model
# glitch, and replacing a written section with nothing is never what was asked.
_EMPTY_OK = frozenset({"open_items_you_owe"})

_STAKEHOLDER_FIELDS = ("name", "email", "role", "context")


class RewriteError(Exception):
    """The rewrite failed. ``str(exc)`` is safe to show the reader."""


@dataclass(frozen=True)
class RewriteResult:
    # Already normalized: prose → str, list → list[str], stakeholders → the
    # JSON-encoded array string the page has always received.
    value: str | list[str]
    sources: list[str] = field(default_factory=list)


# ─── Grounding rules ───────────────────────────────────────────────────────
# Word for word from generate_meeting_prep_analysis (backend/routers/home.py),
# one string per persona. test_meeting_prep_rewrite.py fails if they drift.

BATTLE_GROUNDING_RULES = """## GROUNDING RULES (these override the persona — a false fact is worse than a thin brief)
- Every FACT about the client, the relationship, or its history must be traceable to the RESEARCH DATA below. Never state whether a past bid, proposal, deal, or engagement was won, lost, renewed, active, or pending unless a source explicitly says so.
- Do NOT infer outcomes or backstory from tone or phrasing. "Better positioned this time" or "a deeper analysis" is NOT evidence that a prior effort failed — if the record does not state an outcome, do not assert one.
- Transcript excerpts are dated; treat them as historical. Never present something said in a past meeting as the current state of the relationship.
- Keep your strategic judgment in the editorial fields (recommended_positioning, likely_objections, discovery_questions, talking_points, next_steps). The factual fields (executive_summary, client_profile, recent_interactions, past_meeting_context) must contain only what the sources support.
- When the data is insufficient for a field, say so plainly (e.g. "No record of a prior engagement in the available data") instead of inventing. An honest gap is useful; a confident fabrication destroys trust.
- Every fact you assert about the relationship must be backed by an entry in source_evidence."""

EA_GROUNDING_RULES = """## GROUNDING RULES (non-negotiable — a false fact is worse than a thin brief)
- Every factual statement about people, projects, clients, or history must be traceable to the CONTEXTUAL DATA below. Never assert whether a past bid, deal, proposal, or engagement was won, lost, renewed, active, or pending unless a source explicitly says so.
- Do NOT infer backstory or outcomes from tone or phrasing ("better positioned this time" does not mean a prior effort failed). If the record does not state it, do not claim it.
- Transcript excerpts are dated — treat them as historical, not the current state.
- When the data is insufficient for a field, say so plainly (e.g. "Not enough in the available records to say") rather than inventing. An honest gap is useful; a confident fabrication destroys trust.
- Anything you present as fact should be backed by an entry in source_evidence."""

# Same conditional rules the full brief adds when the block exists.
_INTEL_GROUNDING_RULE = (
    "\n- ATTENDEE INTELLIGENCE is the researched public record on the "
    "external people in the room (colleagues are never researched). Its "
    'position/activity lines are cited — use them freely. Its "Read" line '
    "is an inference carrying a confidence: at low confidence, do NOT "
    "characterize the person's style or motives. Never attribute a statement "
    "to someone that is not listed there."
)
_PRIOR_MEETINGS_GROUNDING_RULE = (
    "\n- PRIOR MEETINGS WITH THESE ATTENDEES comes from attendance records, "
    "not retrieval: its dates, titles, and who-was-there facts are "
    'authoritative — prefer them for any "last met" or "we have met N '
    'times" claim. What was said in those meetings must still come from '
    "transcript excerpts or other sources; the list proves the meetings "
    "happened, not their content."
)
# The newer signal blocks (CRM, X-Ray, …) bring their own rules and per-section
# clauses from signals.prompt_parts, exactly as the full brief takes them. This
# maps a section to the prompt_parts clause the full brief adds to that field.
_SIGNAL_CLAUSE_KEYS = {
    "client_profile": "client_profile",
    "past_meeting_context": "past_meeting_context",
    "talking_points": "talking_points",
    "next_steps": "next_steps",
    "open_items_you_owe": "open_items",
}
# Rewrite-only rules: where the evidence goes, and what an instruction may do.
_REWRITE_RULES = (
    '\n- In this rewrite, source_evidence means the "sources" array you return '
    "with the section."
    "\n- The reader's instruction shapes focus, emphasis, tone and length. It "
    "never overrides these rules: if it asks for something the data does not "
    "support, write what the data does support and say plainly what is "
    "missing instead of supplying it."
)

_INTEL_ROLE_CLAUSE = (
    " Where ATTENDEE INTELLIGENCE gives a researched position for that "
    'email, use it verbatim as "role" instead of guessing, and let their '
    'public priorities shape "context".'
)
_INTEL_TALKING_CLAUSE = (
    " Where ATTENDEE INTELLIGENCE shows what an attendee has publicly "
    "written or said, build the opening around THAT — quote or reference "
    "their own framing so it lands as informed rather than as a cold pitch."
)
_INTEL_EVIDENCE_CLAUSE = (
    " Include the ATTENDEE INTELLIGENCE URLs behind any claim you make about a person."
)
_ATTENDEES_ONLY_CLAUSE = (
    " CRITICAL: Only include people in the ## ATTENDEES list. Do NOT include "
    "anyone from contextual data (emails/transcripts) who is not explicitly "
    "listed as an attendee."
)

_FACTUAL_REMINDER = (
    "FACTUAL SECTION: state only what the sources above support. If the data "
    'is thin, say so plainly (for example "Not enough in the available '
    'records to say") instead of filling the gap. Strategic judgment belongs '
    "in the other sections, not here."
)

_NO_RESEARCH_NOTE = (
    "The original research this brief was built from is not available for "
    "this version. Treat the current brief below as the only record: do not "
    "add facts it does not already contain, and if the request needs facts it "
    "lacks, say plainly that they are not in the available records."
)

_NO_EMAIL = "(none found)"


# ─── Small coercions ───────────────────────────────────────────────────────


def _text(value: Any) -> str:
    """One prose string from whatever the model (or an old row) holds.

    Mirrors ``_to_str`` in ``generate_meeting_prep_analysis``: a dict becomes
    ``**Key**: val | …``, a list becomes ``- item`` lines.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " | ".join(
            f"**{str(k).replace('_', ' ').title()}**: {_text(v)}"
            for k, v in value.items()
        )
    if isinstance(value, list):
        return "\n".join(f"- {_text(item)}" for item in value)
    return str(value)


_LIST_MARKERS = re.compile(r"\n\d+\.\s+|\n-\s+|^\d+\.\s+|^-\s+")
_LEADING_MARKER = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")


def _items(value: Any) -> list[str]:
    """A list section from whatever the model (or an old row) holds.

    A single string is split the way the full brief splits it (numbered or
    dashed markers after a newline); failing that, one item per line.
    """
    if value is None:
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in _LIST_MARKERS.split(value) if p.strip()]
        if len(parts) > 1:
            return parts
        return [
            _LEADING_MARKER.sub("", line).strip()
            for line in value.splitlines()
            if line.strip()
        ]
    if isinstance(value, list):
        return [_text(item) for item in value]
    return [_text(value)]


def _stakeholder_rows(value: Any) -> Any:
    """Hand ``normalize_section_value`` rows it can judge.

    Non-string field values (a number, a null) become strings; a bare name
    becomes a row. Anything still malformed is left for the normalizer to
    reject. People who are not on the invite are NOT filtered here — the prompt
    forbids them, and the person can delete a row by hand.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.strip() else []
        except ValueError:
            return value
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return value
    rows: list[Any] = []
    for item in value:
        if isinstance(item, str):
            rows.append({"name": item})
        elif isinstance(item, dict):
            rows.append(
                {
                    f: (
                        item.get(f)
                        if item.get(f) is None or isinstance(item.get(f), str)
                        else str(item.get(f))
                    )
                    for f in _STAKEHOLDER_FIELDS
                }
            )
        else:
            rows.append(item)
    return rows


def _coerce(spec: SectionSpec, value: Any) -> Any:
    if spec.kind == "prose":
        return _text(value)
    if spec.kind == "list":
        return _items(value)
    return _stakeholder_rows(value)


def _clean_sources(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = raw.splitlines()
    if not isinstance(raw, list):
        raw = [raw]
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = _LEADING_MARKER.sub("", _text(item)).strip()
        if not text:
            continue
        text = text[:_MAX_SOURCE_CHARS]
        if text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= MAX_SOURCES:
            break
    return out


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _parse_model_json(text: str | None) -> Any:
    raw = (
        (text or "")
        .strip()
        .removeprefix("```json")
        .removeprefix("```")
        .removesuffix("```")
        .strip()
    )
    # strict=False: with google_search on there is no JSON mode, and prose
    # written as "- " lines often arrives with raw newlines inside the string.
    try:
        return json.loads(raw, strict=False)
    except ValueError:
        pass
    # The model sometimes wraps the object in prose ("Here is the rewrite:" …
    # "Note: based on {email}"). Decode the first object and ignore what
    # follows it, braces included.
    start = raw.find("{")
    if start >= 0:
        try:
            return json.JSONDecoder(strict=False).raw_decode(raw, start)[0]
        except ValueError:
            pass
    raise RewriteError(MSG_UNUSABLE)


# ─── Prompt pieces ─────────────────────────────────────────────────────────


def _bundle_attendees(bundle: dict[str, Any] | None) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for a in (bundle or {}).get("attendees") or []:
        if not isinstance(a, dict):
            continue
        email = str(a.get("email") or "").strip()
        name = str(a.get("name") or a.get("display_name") or "").strip()
        if email or name:
            out.append((name or email, email))
    return out


def _brief_attendees(brief: dict[str, Any]) -> list[tuple[str, str]]:
    """Fallback when the bundle kept no invite: the people the brief already
    listed (the full brief was itself restricted to the invite)."""
    out: list[tuple[str, str]] = []
    for row in stakeholders_from_value(brief.get("stakeholder_map")):
        if row["name"] or row["email"]:
            out.append((row["name"] or row["email"], row["email"]))
    return out


def _signal_blocks(bundle: dict[str, Any] | None) -> dict[str, str]:
    """The stored signal blocks, non-empty, in ``signals.BLOCK_ORDER``.

    Only the known blocks: the full brief prompts with exactly those, so a key
    outside the order never reached it and does not reach the rewrite either.
    """
    raw = (bundle or {}).get("signal_blocks")
    if not isinstance(raw, dict):
        return {}
    return {
        name: raw[name].strip()
        for name in signals.BLOCK_ORDER
        if isinstance(raw.get(name), str) and raw[name].strip()
    }


def _render_signal_block(name: str, text: str) -> str:
    if text.startswith("#"):
        return text
    # The full brief's blocks carry their own heading; one that does not still
    # needs a label the grounding rules can point at.
    return f"## {name.replace('_', ' ').upper()}\n{text}"


# The catalog columns the PRODUCT KNOWLEDGE block reads (the full brief and this
# rewrite both), under the keys the bundle stores them as. The catalog is the
# workspace's own upload (``retrieval/product_catalog.py``), with the generic
# columns name, description and category. Prices stay out: a prep brief is no
# place for the model to start quoting.
_PRODUCT_COLUMNS = (
    ("name", "name"),
    ("description", "description"),
    ("category", "category"),
)
# How each detail is labelled in the block ("" = the line's own lead).
_PRODUCT_LINE_LABELS = (("category", "Category"),)


def product_details_for_bundle(rows: list[dict] | None) -> list[dict[str, str]]:
    """Catalog rows as the bundle's ``matched_product_details`` entries."""
    return [
        {key: str((row or {}).get(col) or "").strip() for key, col in _PRODUCT_COLUMNS}
        for row in rows or []
        if isinstance(row, dict)
    ]


def product_knowledge_lines(rows: list[dict] | None) -> list[str]:
    """The PRODUCT KNOWLEDGE lines for catalog rows (full brief)."""
    return _product_lines(product_details_for_bundle(rows))


def _product_lines(products: list[dict[str, str]]) -> list[str]:
    """One product per lead line; a missing detail is left out rather than
    printed as an empty label (or as "None")."""
    lines: list[str] = []
    for p in products:
        name = p.get("name") or ""
        if not name:
            continue
        description = p.get("description") or ""
        lines.append(f"- {name}: {description}" if description else f"- {name}")
        for key, label in _PRODUCT_LINE_LABELS:
            if p.get(key):
                lines.append(f"  * {label}: {p[key]}")
    return lines


def _matched_products(bundle: dict[str, Any]) -> list[dict[str, str]]:
    """The products matched to the meeting, as ``{name, description,
    category}``.

    Rows carry them in ``matched_product_details``. A row saved with only the
    names, or with details in another shape, is looked up in the catalog
    again — fail-soft: a name the catalog no longer has, or a catalog that
    will not load, still yields the name.
    """
    details = bundle.get("matched_product_details")
    if isinstance(details, list) and details:
        rows = [d for d in details if isinstance(d, dict)]
        if all("category" in d for d in rows):
            return [
                {key: str(d.get(key) or "").strip() for key, _ in _PRODUCT_COLUMNS}
                for d in rows
                if str(d.get("name") or "").strip()
            ]
        names = [str(d.get("name") or "").strip() for d in rows]
    else:
        names = [str(p).strip() for p in (bundle.get("matched_products") or []) if p]
    names = [n for n in names if n]
    if not names:
        return []
    try:
        from rubik_core.retrieval.product_catalog import get_product_catalog

        catalog = get_product_catalog()
        rows = [catalog.get_product_details(n) or {} for n in names]
    except Exception:  # noqa: BLE001 — the names alone still help
        logger.warning("Product catalog lookup failed for a section rewrite")
        rows = [{} for _ in names]
    return [
        {
            **{key: str(row.get(col) or "").strip() for key, col in _PRODUCT_COLUMNS},
            "name": name,
        }
        for name, row in zip(names, rows)
    ]


#: The heading of the product block in the full brief and in a rewrite.
PRODUCT_KNOWLEDGE_HEADING = "## PRODUCT KNOWLEDGE"


def _product_knowledge_block(products: list[dict[str, str]]) -> str:
    """The full brief's PRODUCT KNOWLEDGE block, the same lines."""
    return "\n".join([PRODUCT_KNOWLEDGE_HEADING, *_product_lines(products)])


def battle_persona(profile: CompanyProfile) -> str:
    """Who writes the sales battle brief: an analyst at the workspace's company.

    The same words open the full brief (``backend/routers/home.py``) and a
    one-section rewrite. With no profile saved the analyst works at "the
    company".
    """
    return f"You are an Elite Strategic Sales Analyst at {company_name_or_default(profile)}."


def about_company(profile: CompanyProfile) -> str:
    """The profile block plus a blank line, or ``""`` with no profile saved."""
    block = company_context_block(profile)
    return f"{block}\n" if block else ""


def _render_other_section(spec: SectionSpec, value: Any) -> str:
    if spec.kind == "prose":
        return _clip(_text(value), _OTHER_PROSE_CHARS)
    if spec.kind == "list":
        items = [i.strip() for i in _items(value) if i.strip()]
        lines = [f"- {_clip(i, _OTHER_ITEM_CHARS)}" for i in items[:_OTHER_ITEMS_MAX]]
        if len(items) > _OTHER_ITEMS_MAX:
            lines.append(f"- … ({len(items) - _OTHER_ITEMS_MAX} more)")
        return "\n".join(lines)
    lines = []
    for row in stakeholders_from_value(value):
        who = row["name"] or row["email"]
        if not who:
            continue
        email = f" <{row['email']}>" if row["name"] and row["email"] else ""
        role = f" — {row['role']}" if row["role"] else ""
        ctx = f": {row['context']}" if row["context"] else ""
        lines.append(_clip(f"- {who}{email}{role}{ctx}", _OTHER_ITEM_CHARS))
    return "\n".join(lines)


def _render_current_value(spec: SectionSpec, value: Any) -> str:
    if spec.kind == "prose":
        text = _text(value).strip()
    elif spec.kind == "list":
        text = "\n".join(f"- {i.strip()}" for i in _items(value) if i.strip())
    else:
        rows = stakeholders_from_value(value)
        text = json.dumps(rows, ensure_ascii=False) if rows else ""
    return text or "(empty)"


def _value_shape(spec: SectionSpec, *, battle: bool, has_intel: bool) -> str:
    if spec.kind == "prose":
        return (
            '"value": a single string of prose (short "- " lines are fine where '
            "a list reads better). Not an array."
        )
    if spec.kind == "list":
        return (
            '"value": a JSON ARRAY of separate strings, one item per element. '
            "Do NOT return one long string with line breaks."
        )
    return (
        '"value": a JSON ARRAY of objects, one per person in the ## ATTENDEES '
        'section. Each object MUST have keys: "name" (string), "email" '
        '(string), "role" (string, their title/function), "context" (string, '
        + (
            "1-2 sentences on their agenda and influence for THIS meeting)."
            if battle
            else "1-2 sentences on what they care about and their dynamic in "
            "this meeting)."
        )
        + (_INTEL_ROLE_CLAUSE if has_intel else "")
        + _ATTENDEES_ONLY_CLAUSE
    )


def build_rewrite_prompt(
    *,
    spec: SectionSpec,
    brief: dict[str, Any],
    context_bundle: dict[str, Any] | None,
    battle: bool,
    is_internal: bool,
    instruction: str,
    personalization_block: str = "",
    wgll_context: str = "",
) -> tuple[str, str]:
    """``(system_instruction, prompt)`` for one section rewrite.

    Pure, apart from one product-catalog read for a battle-brief row saved
    before the product details were kept with it.
    """
    from rubik_core.people.prompt import format_intel_block_from_dicts

    bundle = context_bundle if isinstance(context_bundle, dict) else None
    b = bundle or {}

    intel_block = format_intel_block_from_dicts(b.get("attendee_intel") or None)
    has_intel = bool(intel_block)
    prior_meetings_block = str(b.get("prior_meetings_block") or "").strip()
    signal_blocks = _signal_blocks(bundle)
    # The same rules and section clauses the full brief takes from these blocks.
    sig = signals.prompt_parts(signal_blocks, battle_brief=battle)

    # ── System instruction: same persona and rules as the full brief ──
    conditional_rules = (
        (_INTEL_GROUNDING_RULE if has_intel else "")
        + (_PRIOR_MEETINGS_GROUNDING_RULE if prior_meetings_block else "")
        + sig["grounding"]
        + _REWRITE_RULES
    )
    if battle:
        profile = get_company_profile()
        system = (
            f"{battle_persona(profile)} You prepared a "
            '"Battle Brief" for an upcoming meeting with a prospect or client, '
            "and the reader has asked you to rewrite ONE section of it.\n"
            "Focus on GAP SELLING, identifying their pain points, and "
            "positioning our solutions as the definitive cure. Provide a "
            "strong Point of View (POV).\n\n"
            f"{about_company(profile)}{BATTLE_GROUNDING_RULES}{conditional_rules}"
        )
    else:
        system = (
            "You are a World-Class Executive Assistant. You prepared a "
            "productivity brief for an internal meeting or 1:1, and the reader "
            "has asked you to rewrite ONE section of it.\n"
            "Focus on operational efficiency, project status, relationship "
            "management, and ensuring no balls are dropped.\n"
            "Be concise, actionable, and observant of past commitments.\n\n"
            f"{EA_GROUNDING_RULES}{conditional_rules}"
        )
    if personalization_block:
        system = f"{system}\n\n{personalization_block}"
    if wgll_context:
        system = f"{system}\n\n{wgll_context}"

    # ── Prompt ──
    label = spec.label(is_internal=is_internal)
    title = str(b.get("title") or brief.get("title") or "this meeting")
    parts: list[str] = [f"Rewrite ONE section of the briefing for: {title}"]

    meeting_lines = ["## MEETING CONTEXT"]
    if b.get("description"):
        meeting_lines.append(f"- Description: {b['description']}")
    if battle and b.get("location"):
        meeting_lines.append(f"- Location: {b['location']}")
    when = b.get("start") or brief.get("meeting_time")
    if when:
        meeting_lines.append(f"- Time: {when}")
    if b.get("focus_on"):
        meeting_lines.append(f"- Focus Area: {b['focus_on']}")
    if len(meeting_lines) > 1:
        if when:
            meeting_lines.append(
                "\nWhen you reference the meeting time, copy the Time value "
                "above verbatim; never reformat a time from an ISO/offset string."
            )
        parts.append("\n".join(meeting_lines))

    attendees = _bundle_attendees(bundle)
    attendees_note = ""
    if not attendees:
        attendees = _brief_attendees(brief)
        if attendees:
            attendees_note = (
                "(The invite was not saved with this version; these are the "
                "people the brief already lists.)"
            )
    if attendees:
        lines = ["## ATTENDEES"]
        lines += [
            f"- {name} <{email}>" if email else f"- {name}" for name, email in attendees
        ]
        if attendees_note:
            lines.append(attendees_note)
        parts.append("\n".join(lines))
    else:
        parts.append(
            "## ATTENDEES\n(The attendee list was not saved with this version.)"
        )

    data_header = "## RESEARCH DATA" if battle else "## CONTEXTUAL DATA"
    if bundle is None:
        parts.append(f"{data_header}\n{_NO_RESEARCH_NOTE}")
    else:
        research: list[str] = []
        if prior_meetings_block:
            research.append(prior_meetings_block)
        past = str(b.get("past_meetings_report") or "").strip()
        if past:
            research.append(past)
        email_block = str(b.get("email_block") or "").strip()
        if email_block and email_block != _NO_EMAIL:
            research.append(f"### RECENT EMAIL WITH THESE PEOPLE\n{email_block}")
        drive = str(b.get("drive_context") or "").strip()
        if drive:
            research.append(drive)
        # Battle brief only, as in the full brief: the catalog is internal, so
        # web search cannot stand in for it.
        products = _matched_products(b) if battle else []
        if products:
            research.append(_product_knowledge_block(products))
        research.extend(
            _render_signal_block(name, text) for name, text in signal_blocks.items()
        )
        if intel_block:
            research.append(intel_block)
        if research:
            parts.append(data_header + "\n" + "\n\n".join(research))
        else:
            parts.append(
                f"{data_header}\nNo research data was found for this meeting. "
                "Say so plainly wherever a fact would be needed."
            )

    # The rest of the brief, compactly, so the rewrite stays consistent with it
    # — and without the target section, which appears once, below.
    others: list[str] = []
    for other in SECTIONS:
        if other.key == spec.key:
            continue
        rendered = _render_other_section(other, brief.get(other.key))
        if rendered:
            others.append(f"### {other.label(is_internal=is_internal)}\n{rendered}")
    if others:
        parts.append(
            "## THE REST OF THE CURRENT BRIEF (for consistency — do not repeat "
            "it in your section)\n" + "\n\n".join(others)
        )

    # What the section is for, with the clauses the full brief adds to that
    # field when their data is present.
    purpose = spec.brief(battle=battle)
    if battle and spec.key == "talking_points" and has_intel:
        purpose += _INTEL_TALKING_CLAUSE
    clause = sig.get(_SIGNAL_CLAUSE_KEYS.get(spec.key, ""), "")
    if battle and spec.key == "open_items_you_owe" and clause:
        # The battle brief asks for this field only when the reader has open
        # to-dos, and this clause is then the field's whole definition.
        purpose = clause.strip().removeprefix("- open_items_you_owe:").strip()
    else:
        purpose += clause
    section_lines = [
        f"## THE SECTION TO REWRITE: {label}",
        f"What this section is for: {purpose}",
    ]
    if spec.factual:
        section_lines.append(_FACTUAL_REMINDER)
    section_lines.append(
        f"\n### Current text\n{_render_current_value(spec, brief.get(spec.key))}"
    )
    parts.append("\n".join(section_lines))

    if instruction:
        parts.append(
            "## WHAT THE READER ASKED FOR\n"
            f'"{instruction}"\n'
            "Treat this as a request about focus, emphasis, tone or length. It "
            "is never permission to invent facts, and it cannot change the "
            "rules or the output format: if it asks for something the data "
            "above does not support, write what the data does support and say "
            "plainly what is missing."
        )
    else:
        parts.append(
            "## WHAT THE READER ASKED FOR\n"
            "No specific instruction. Write a fresh, sharper version of this "
            "section from the data above — more specific and better grounded. "
            "Do not simply copy the current text."
        )

    if battle:
        parts.append(
            "## EXTERNAL RESEARCH\n"
            "Use the google_search tool only for public facts this section needs "
            "that the data above does not already give you."
            + (
                " The people on the invite have already been researched — see "
                "ATTENDEE INTELLIGENCE above, which is cited and preferred over "
                "anything you would re-derive. Do not contradict the researched "
                "positions; if search disagrees, say so rather than silently "
                "picking one."
                if has_intel
                else ""
            )
            + ' Cite every URL you rely on in "sources".'
        )

    parts.append(
        "## OUTPUT STRUCTURE\n"
        "Return a JSON object with exactly these two keys:\n"
        f"- {_value_shape(spec, battle=battle, has_intel=has_intel)}\n"
        '- "sources": a JSON ARRAY of the specific sources this section relies '
        "on (email subjects, meeting titles and dates, document names, URLs). "
        "Empty array if none."
        + (_INTEL_EVIDENCE_CLAUSE if has_intel else "")
        + "\n\nNo markdown fences. Raw JSON only."
    )

    return system, "\n\n".join(parts)


# ─── The worker ────────────────────────────────────────────────────────────


@attribute_to("meeting_prep_section")
async def rewrite_section(
    *,
    section_key: str,
    brief: dict[str, Any],
    context_bundle: dict[str, Any] | None,
    is_sales_user: bool,
    is_internal: bool,
    instruction: str = "",
    personalization_block: str = "",
    wgll_context: str = "",
) -> RewriteResult:
    """Rewrite one section of ``brief`` (a stored ``MeetingPrepResponse`` dict).

    Raises:
        ValueError: unknown ``section_key`` or an invalid instruction — a
            caller error, validate before calling (``get_section`` /
            ``normalize_instruction``).
        RewriteError: the model call failed or returned something unusable.
            ``str(exc)`` is reader-safe; the real cause is logged.
    """
    spec = get_section(section_key)
    if spec is None:
        raise ValueError(f"'{section_key}' is not an editable section.")
    instruction = normalize_instruction(instruction)
    battle = is_battle_brief(is_sales_user=is_sales_user, is_internal=is_internal)

    system, prompt = build_rewrite_prompt(
        spec=spec,
        brief=brief or {},
        context_bundle=context_bundle,
        battle=battle,
        is_internal=is_internal,
        instruction=instruction,
        personalization_block=personalization_block,
        wgll_context=wgll_context,
    )

    from google.genai import types

    from rubik_core.ai_providers.registry import resolve_model_route
    from rubik_core.ai_providers.runtime import (
        generate_grounded_text_on_route,
        generate_text_on_route,
    )
    from rubik_core.config import settings
    from rubik_core.models import get_genai_client

    config_kwargs: dict[str, Any] = {
        "system_instruction": system,
        "temperature": 0.2,
    }
    if battle:
        # Same rule as the full brief: web research only for a sales reader
        # meeting people outside the company. Search grounding cannot be combined with
        # a JSON response type, so the text is parsed by hand below.
        config_kwargs["tools"] = [types.Tool(google_search=types.GoogleSearch())]
    else:
        config_kwargs["response_mime_type"] = "application/json"

    try:
        route = await asyncio.to_thread(
            resolve_model_route,
            "meeting_prep_rewrite_grounded" if battle else "meeting_prep_rewrite",
            "grounding" if battle else "json",
            default_model=settings.VERTEX_LLM_MODEL_PRO,
        )
        if route.provider == "vertex_managed":
            res = await asyncio.wait_for(
                get_genai_client().aio.models.generate_content(
                    model=route.model,
                    contents=prompt,
                    config=types.GenerateContentConfig(**config_kwargs),
                ),
                REWRITE_TIMEOUT_S,
            )
            text = res.text or ""
        elif battle:
            text = (
                await asyncio.wait_for(
                    asyncio.to_thread(
                        generate_grounded_text_on_route,
                        route,
                        prompt,
                        system_instruction=system,
                        temperature=0.2,
                    ),
                    REWRITE_TIMEOUT_S,
                )
            ).text
        else:
            text = (
                await asyncio.wait_for(
                    asyncio.to_thread(
                        generate_text_on_route,
                        route,
                        prompt,
                        json_mode=True,
                        system_instruction=system,
                        temperature=0.2,
                    ),
                    REWRITE_TIMEOUT_S,
                )
            ).text
    except asyncio.TimeoutError as exc:
        logger.warning(
            "Meeting prep section rewrite timed out after %ss (%s)",
            REWRITE_TIMEOUT_S,
            section_key,
        )
        raise RewriteError(MSG_UNAVAILABLE) from exc
    except Exception:  # noqa: BLE001 — any model failure is a retry for the reader
        logger.warning("Meeting prep section rewrite failed (%s)", section_key)
        raise RewriteError(MSG_UNAVAILABLE) from None

    try:
        data = _parse_model_json(text)
    except RewriteError:
        logger.warning(
            "Meeting prep section rewrite returned unparseable output (%s, %d chars)",
            section_key,
            len(text or ""),
        )
        raise

    if isinstance(data, dict):
        if "value" in data:
            raw_value = data["value"]
        elif section_key in data:
            raw_value = data[section_key]
        else:
            logger.warning(
                "Meeting prep section rewrite returned no value (%s, keys=%s)",
                section_key,
                sorted(data)[:10],
            )
            raise RewriteError(MSG_UNUSABLE)
        raw_sources = data.get("sources", data.get("source_evidence"))
    elif isinstance(data, list) and spec.kind != "prose":
        raw_value, raw_sources = data, None
    else:
        raise RewriteError(MSG_UNUSABLE)

    try:
        value = normalize_section_value(section_key, _coerce(spec, raw_value))
    except ValueError as exc:
        logger.warning(
            "Meeting prep section rewrite returned an invalid value (%s): %s",
            section_key,
            exc,
        )
        raise RewriteError(MSG_UNUSABLE) from exc

    empty = (
        not value if spec.kind != "stakeholders" else not stakeholders_from_value(value)
    )
    if empty and section_key not in _EMPTY_OK:
        logger.warning("Meeting prep section rewrite came back empty (%s)", section_key)
        raise RewriteError(MSG_UNUSABLE)

    return RewriteResult(value=value, sources=_clean_sources(raw_sources))

"""The brief and content shapes, and the validators that fence the model.

Everything the Proposal Generator stores is one of two JSON documents:

* the **brief** — structured facts: who the proposal is for, the service, the
  scope items, the outcomes the prospect stated, pricing lines, notes, open
  questions. Filled by the model from the transcript, then corrected and
  confirmed by the AE.
* the **content** — the prose of one generated version, keyed to the brief's
  item ids.

Both pass through here on every write, whether the writer is the model or the
AE, so a malformed payload from either becomes a well-formed one or a dropped
item — never a 500 and never a document with a hole in it.

THE FENCE. The adjustment reviewers made on every hand-made draft was to strip
anything the AI had added to the scope. :func:`normalize_content` makes that
structurally impossible rather than a matter of prompt discipline: the content
may only carry a ``scope`` or ``outcomes`` entry whose id exists in the brief,
extras are discarded, gaps are filled from the brief's own words, and pricing
is not part of the content at all — the renderer reads it from the brief. A
model that wants to add a service has nowhere to put it.

Desired outcomes carry a verbatim transcript quote. :func:`normalize_brief`
checks each quote against the transcript when one is supplied and drops (or,
for an AE-authored brief, unmarks) the ones that are not really there — the
same rule ``rubik_core/call_capture/validators.py`` applies to evidence.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

BRIEF_SCHEMA_VERSION = "proposal-brief-v2"

MAX_SCOPE_ITEMS = 8
MAX_OUTCOMES = 6
MAX_PRICING_LINES = 6
MAX_NOTES = 6
MAX_OPEN_QUESTIONS = 10
MAX_QUOTE_CHARS = 300
MAX_SHORT_CHARS = 200
MAX_LONG_CHARS = 2500

#: ``dropped`` reason codes — a closed set so the caller can log a histogram.
DROP_NO_TITLE = "no_title"
DROP_NO_OUTCOME = "no_outcome"
DROP_QUOTE_NOT_IN_TRANSCRIPT = "quote_not_in_transcript"
DROP_NO_DESCRIPTION = "no_description"
DROP_OVER_CAP = "over_cap"
DROP_UNKNOWN_ID = "unknown_id"


# ── Small coercions ──────────────────────────────────────────────────────────


def _s(value: Any, limit: int = MAX_SHORT_CHARS) -> str:
    """A stripped string, or ``""``. Numbers are stringified; nothing raises."""
    if value is None:
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # A price the model returned as a number rather than a string.
        text = (
            f"{value:,.2f}".rstrip("0").rstrip(".")
            if isinstance(value, float)
            else f"{value:,}"
        )
    else:
        text = str(value)
    text = text.strip()
    return text[:limit].rstrip() if limit else text


def _list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "yes", "1"):
            return True
        if low in ("false", "no", "0"):
            return False
    return default


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _fresh_id(prefix: str, taken: set[str]) -> str:
    n = 1
    while f"{prefix}{n}" in taken:
        n += 1
    ident = f"{prefix}{n}"
    taken.add(ident)
    return ident


def _take_id(raw: Any, prefix: str, taken: set[str]) -> str:
    """Keep the model's id when it is sane and unused, else mint one."""
    ident = _s(raw, 24)
    if ident and re.fullmatch(r"[A-Za-z0-9_-]{1,24}", ident) and ident not in taken:
        taken.add(ident)
        return ident
    return _fresh_id(prefix, taken)


_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]")


def _fold(text: str) -> str:
    """Whitespace- and punctuation-insensitive form for verbatim checks."""
    return _WS.sub(" ", _PUNCT.sub("", text.lower())).strip()


def quote_in_transcript(quote: str, transcript_text: str) -> bool:
    """True when ``quote`` occurs verbatim (modulo whitespace and punctuation)."""
    q = _fold(quote)
    if len(q) < 8:
        return False
    return q in _fold(transcript_text)


# ── Money ────────────────────────────────────────────────────────────────────

_MONEY = re.compile(
    r"(?<![\w.])\$?\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?(?![\d,])"
)


def money_value(text: str | None) -> float | None:
    """Parse the FIRST dollar figure out of a pricing string, or ``None``.

    ``"$5,500"`` → 5500; ``"$225/hr"`` → 225; ``"45 to 60 hours at $225"`` → 45
    (which is why savings are only computed when BOTH cells parse to a plain
    figure and the standard one is larger — a rate-per-hour line never gets a
    "SAVE" badge).
    """
    if not text:
        return None
    m = _MONEY.search(text)
    if not m:
        return None
    whole = m.group(1).replace(",", "")
    cents = m.group(2) or "0"
    try:
        return float(f"{whole}.{cents}")
    except ValueError:
        return None


def format_money(value: float) -> str:
    if abs(value - round(value)) < 0.005:
        return f"${int(round(value)):,}"
    return f"${value:,.2f}"


def savings_label(line: dict) -> str | None:
    """``"SAVE $1,000"`` when a line quotes a standard rate above the proposal price."""
    std = money_value(line.get("standard_rate"))
    prop = money_value(line.get("proposal_price"))
    if std is None or prop is None or std <= prop:
        return None
    both = (line.get("standard_rate") or "") + " " + (line.get("proposal_price") or "")
    # A per-unit rate ("$225/hr") beside a total is not a saving.
    if re.search(r"/\s*(hr|hour|seat|user|unit|month|mo|yr|year)", both, re.I):
        return None
    # Nor is a compound price ("$3,500 Setup + $1,625 Monthly", "$77 / $85"):
    # its first figure is not its total.
    for cell in (line.get("standard_rate"), line.get("proposal_price")):
        if len(_MONEY.findall(cell or "")) != 1:
            return None
    if re.search(r"\b(monthly|annual|annually|per|each|setup|base)\b", both, re.I):
        return None
    return f"SAVE {format_money(std - prop)}"


# ── The brief ────────────────────────────────────────────────────────────────


def empty_brief() -> dict:
    return {
        "schema_version": BRIEF_SCHEMA_VERSION,
        "date": date.today().isoformat(),
        "ref_code": "",
        "prepared_for": {"name": "", "title": "", "company": ""},
        "prepared_by": {"name": "", "title": "", "email": ""},
        "service_title": "",
        "engagement_summary": "",
        "scope_items": [],
        "desired_outcomes": [],
        "pricing": [],
        "notes": [],
        "timeline": "",
        "open_questions": [],
        "template": {"key": ""},
    }


def normalize_template(raw: Any) -> dict:
    """``{"key": ...}`` with a key that is not template-shaped cleared.

    Accepts the stored dict, or a bare key string (the brief prompt returns
    ``template_key``). Whether a template with that key EXISTS is a database
    question, answered where the brief meets the database (``service.py``)
    and again when the document is rendered: a template deleted since the
    brief was written simply does not print.
    """
    from rubik_core.proposals.templates import is_key

    if isinstance(raw, str):
        raw = {"key": raw}
    src = raw if isinstance(raw, dict) else {}
    key = _s(src.get("key"), 64)
    return {"key": key if is_key(key) else ""}


def _person(raw: Any, keys: tuple[str, ...]) -> dict:
    src = raw if isinstance(raw, dict) else {}
    return {k: _s(src.get(k)) for k in keys}


def normalize_brief(
    raw: Any,
    *,
    transcript_text: str | None = None,
    keep_unquoted_outcomes: bool = False,
) -> tuple[dict, list[dict]]:
    """Coerce a brief from the model or the AE into the canonical shape.

    Returns ``(brief, dropped)``, where ``dropped`` lists
    ``{"section", "reason", "text"}`` for every item that did not survive.

    ``transcript_text`` turns on the verbatim check for outcome quotes. With
    ``keep_unquoted_outcomes`` False (the model's extraction) an unverifiable
    quote drops the outcome — the rule is that Desired Outcomes are goals
    the prospect actually stated. With it True (the AE's own edits, or a
    revision the AE asked for) the outcome is kept, its quote blanked and its
    ``confirmed`` cleared, so the AE's words are never thrown away but a quote
    that is not really in the call is never printed as one.
    """
    src = raw if isinstance(raw, dict) else {}
    # A model that wrapped its answer.
    if "brief" in src and isinstance(src["brief"], dict) and "service_title" not in src:
        src = src["brief"]

    out = empty_brief()
    dropped: list[dict] = []

    out["date"] = _s(src.get("date"), 10) or out["date"]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", out["date"]):
        out["date"] = date.today().isoformat()
    out["ref_code"] = _s(src.get("ref_code"), 40)
    out["prepared_for"] = _person(src.get("prepared_for"), ("name", "title", "company"))
    out["prepared_by"] = _person(src.get("prepared_by"), ("name", "title", "email"))
    out["service_title"] = _s(src.get("service_title"))
    out["engagement_summary"] = _s(src.get("engagement_summary"), MAX_LONG_CHARS)
    out["timeline"] = _s(src.get("timeline"), MAX_LONG_CHARS)

    taken: set[str] = set()
    for item in _list(src.get("scope_items")):
        if not isinstance(item, dict):
            item = {"title": item}
        title = _s(item.get("title"))
        if not title:
            dropped.append(
                {"section": "scope_items", "reason": DROP_NO_TITLE, "text": ""}
            )
            continue
        if len(out["scope_items"]) >= MAX_SCOPE_ITEMS:
            dropped.append(
                {"section": "scope_items", "reason": DROP_OVER_CAP, "text": title}
            )
            continue
        out["scope_items"].append(
            {
                "id": _take_id(item.get("id"), "s", taken),
                "title": title,
                "detail": _s(item.get("detail"), MAX_LONG_CHARS),
                "quote": _s(item.get("quote"), MAX_QUOTE_CHARS),
                "confirmed": _bool(item.get("confirmed"), True),
            }
        )

    taken = set()
    for item in _list(src.get("desired_outcomes")):
        if not isinstance(item, dict):
            item = {"outcome": item}
        outcome = _s(item.get("outcome"), MAX_LONG_CHARS)
        if not outcome:
            dropped.append(
                {"section": "desired_outcomes", "reason": DROP_NO_OUTCOME, "text": ""}
            )
            continue
        quote = _s(item.get("quote"), MAX_QUOTE_CHARS)
        confirmed = _bool(item.get("confirmed"), True)
        if transcript_text is not None and not (
            quote and quote_in_transcript(quote, transcript_text)
        ):
            if not keep_unquoted_outcomes:
                dropped.append(
                    {
                        "section": "desired_outcomes",
                        "reason": DROP_QUOTE_NOT_IN_TRANSCRIPT,
                        "text": outcome,
                    }
                )
                continue
            quote = ""
            confirmed = False
        if len(out["desired_outcomes"]) >= MAX_OUTCOMES:
            dropped.append(
                {
                    "section": "desired_outcomes",
                    "reason": DROP_OVER_CAP,
                    "text": outcome,
                }
            )
            continue
        out["desired_outcomes"].append(
            {
                "id": _take_id(item.get("id"), "o", taken),
                "outcome": outcome,
                "quote": quote,
                "entry_sequence": _int_or_none(item.get("entry_sequence")),
                "confirmed": confirmed,
            }
        )

    taken = set()
    for item in _list(src.get("pricing")):
        if not isinstance(item, dict):
            item = {"description": item}
        description = _s(item.get("description"), MAX_LONG_CHARS)
        if not description:
            dropped.append(
                {"section": "pricing", "reason": DROP_NO_DESCRIPTION, "text": ""}
            )
            continue
        if len(out["pricing"]) >= MAX_PRICING_LINES:
            dropped.append(
                {"section": "pricing", "reason": DROP_OVER_CAP, "text": description}
            )
            continue
        proposal_price = _s(item.get("proposal_price"), 60)
        out["pricing"].append(
            {
                "id": _take_id(item.get("id"), "p", taken),
                "description": description,
                "standard_rate": _s(item.get("standard_rate"), 60),
                "proposal_price": proposal_price,
                # An empty price is by definition unconfirmed, whatever the flag said.
                "confirmed": bool(proposal_price)
                and _bool(item.get("confirmed"), True),
            }
        )

    out["notes"] = [
        n for n in (_s(x, MAX_LONG_CHARS) for x in _list(src.get("notes"))) if n
    ][:MAX_NOTES]
    out["open_questions"] = [
        q
        for q in (_s(x, MAX_LONG_CHARS) for x in _list(src.get("open_questions")))
        if q
    ][:MAX_OPEN_QUESTIONS]

    out["template"] = normalize_template(
        src["template"] if "template" in src else src.get("template_key")
    )

    return out, dropped


def brief_is_complete(brief: dict) -> list[str]:
    """What still stops this brief from producing a sendable document.

    Advisory — the router shows these and the AE decides. A proposal with no
    price is sometimes exactly what a first-touch document should be.
    """
    gaps: list[str] = []
    if not brief.get("service_title"):
        gaps.append("The proposal needs a service title.")
    if not (brief.get("prepared_for") or {}).get("company"):
        gaps.append("Who is this for? The prospect's company is blank.")
    if not brief.get("scope_items"):
        gaps.append("There are no scope items — nothing for the proposal to describe.")
    if any(not line.get("proposal_price") for line in brief.get("pricing") or []):
        gaps.append("A pricing line has no proposal price.")
    return gaps


# ── The content ──────────────────────────────────────────────────────────────

#: A currency amount in prose: "$5,500", "€1.200", "USD 900", "900 dollars".
_PROSE_PRICE = re.compile(
    r"[$€£]\s?\d|\b(?:USD|EUR|GBP)\s?\d|\d[\d,.]*\s?(?:USD|EUR|GBP|dollars?)\b",
    re.IGNORECASE,
)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def _drop_priced_sentences(text: str) -> tuple[str, int]:
    """``text`` without the sentences that state a price, and how many went.

    Pricing never passes through AI prose (spec §4.2.5): the only figures a
    prospect sees are printed from the confirmed brief by the renderer. A
    model that writes "the review costs $5,500" anyway loses that sentence
    here, whole, rather than leaving "the review costs ." behind.
    """
    if not text or not _PROSE_PRICE.search(text):
        return text, 0
    kept_paragraphs: list[str] = []
    dropped = 0
    for paragraph in re.split(r"\n\s*\n", text):
        sentences = _SENTENCE_END.split(paragraph.strip())
        kept = [s for s in sentences if not _PROSE_PRICE.search(s)]
        dropped += len(sentences) - len(kept)
        if kept:
            kept_paragraphs.append(" ".join(kept))
    return "\n\n".join(kept_paragraphs), dropped


def empty_content(brief: dict) -> dict:
    return {
        "document_title": "Services Proposal",
        "headline": brief.get("service_title") or "Services Proposal",
        "executive_summary": "",
        "scope_intro": "",
        "scope": [],
        "outcomes_intro": "",
        "outcomes": [],
        "pricing_intro": "",
        "notes": [],
        "closing": "",
    }


def _keyed(raw: Any, id_field: str = "id") -> dict[str, dict]:
    out: dict[str, dict] = {}
    for item in _list(raw):
        if isinstance(item, dict):
            ident = _s(item.get(id_field), 24)
            if ident and ident not in out:
                out[ident] = item
    return out


def normalize_content(raw: Any, brief: dict) -> tuple[dict, dict]:
    """Coerce generated prose into the canonical shape, fenced to ``brief``.

    Returns ``(content, fence)`` where ``fence`` reports what the fence did:
    ``{"dropped_scope": [...ids], "dropped_outcomes": [...ids],
    "filled_scope": [...ids], "filled_outcomes": [...ids], "notes_reset": bool,
    "priced_sentences": int}``. Sentences of model prose that state a price
    are dropped (see :func:`_drop_priced_sentences`).
    The caller logs it; a model that keeps trying to add items is a prompt
    problem worth seeing.
    """
    src = raw if isinstance(raw, dict) else {}
    if "content" in src and isinstance(src["content"], dict) and "scope" not in src:
        src = src["content"]

    out = empty_content(brief)
    fence = {
        "dropped_scope": [],
        "dropped_outcomes": [],
        "filled_scope": [],
        "filled_outcomes": [],
        "notes_reset": False,
        "priced_sentences": 0,
    }

    def prose(value: Any) -> str:
        text, dropped = _drop_priced_sentences(_s(value, MAX_LONG_CHARS))
        fence["priced_sentences"] += dropped
        return text

    out["document_title"] = _s(src.get("document_title")) or out["document_title"]
    out["headline"] = _s(src.get("headline")) or out["headline"]
    out["executive_summary"] = prose(src.get("executive_summary"))
    out["scope_intro"] = prose(src.get("scope_intro"))
    out["outcomes_intro"] = prose(src.get("outcomes_intro"))
    out["pricing_intro"] = prose(src.get("pricing_intro"))
    out["closing"] = prose(src.get("closing"))

    # Scope: one entry per brief item, brief order, brief ids. Extras vanish.
    by_id = _keyed(src.get("scope"))
    brief_ids = {item["id"] for item in brief.get("scope_items") or []}
    fence["dropped_scope"] = sorted(set(by_id) - brief_ids)
    for item in brief.get("scope_items") or []:
        given = by_id.get(item["id"]) or {}
        heading = _s(given.get("heading")) or item["title"]
        body = prose(given.get("body"))
        if not body:
            body = item.get("detail") or ""
            fence["filled_scope"].append(item["id"])
        out["scope"].append({"id": item["id"], "heading": heading, "body": body})

    by_id = _keyed(src.get("outcomes"))
    brief_ids = {item["id"] for item in brief.get("desired_outcomes") or []}
    fence["dropped_outcomes"] = sorted(set(by_id) - brief_ids)
    for item in brief.get("desired_outcomes") or []:
        given = by_id.get(item["id"]) or {}
        heading = _s(given.get("heading")) or item["outcome"]
        body = prose(given.get("body"))
        if not body:
            body = item["outcome"]
            fence["filled_outcomes"].append(item["id"])
        out["outcomes"].append({"id": item["id"], "heading": heading, "body": body})

    # Notes: the model may REWORD the brief's notes, one for one. A different
    # count means it added or merged some, and the brief's own words win.
    brief_notes = list(brief.get("notes") or [])
    given_notes = [
        n for n in (_s(x, MAX_LONG_CHARS) for x in _list(src.get("notes"))) if n
    ]
    if brief_notes and len(given_notes) == len(brief_notes):
        # A figure in a note prints in the AE's confirmed words, never in the
        # model's rewording of them.
        out["notes"] = [
            theirs if _PROSE_PRICE.search(mine) else mine
            for mine, theirs in zip(given_notes, brief_notes, strict=True)
        ]
    else:
        out["notes"] = brief_notes
        fence["notes_reset"] = bool(given_notes) and given_notes != brief_notes

    return out, fence

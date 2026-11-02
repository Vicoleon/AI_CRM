"""Turn "send Jordan the SOW Thursday" into title / who / when.

One cheap Flash call (``@attribute_to("todos")`` so it lands in its own AI
Cost row), with a deterministic fallback that handles the date words people
actually type, so a Vertex hiccup degrades to "we kept your words as the
title" and never to an error. Nothing here writes; the caller decides.
"""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from rubik_core.cost.usage import attribute_to

logger = logging.getLogger(__name__)

_WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
_WEEKDAY_ABBR = {w[:3]: i for i, w in enumerate(_WEEKDAYS)}
_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
_MONTH_DAY_RE = re.compile(
    r"\b(?:on\s+|by\s+)?("
    + "|".join(m[:3] + r"[a-z]*" for m in _MONTHS)
    + r")\s+(\d{1,2})(?:st|nd|rd|th)?\b",
    re.IGNORECASE,
)


def resolve_due_phrase(text: str, *, today: date) -> tuple[date | None, str]:
    """Deterministic date words → a calendar day. Returns ``(due, remainder)``
    where ``remainder`` is the text with the date phrase removed."""
    t = (text or "").strip()
    low = t.lower()

    m = _ISO_RE.search(low)
    if m:
        try:
            due = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return due, _strip(t, m.start(), m.end())
        except ValueError:
            pass

    for phrase, delta in (
        ("day after tomorrow", 2),
        ("tomorrow", 1),
        ("today", 0),
        ("tonight", 0),
    ):
        idx = low.find(phrase)
        if idx >= 0:
            return today + timedelta(days=delta), _strip(t, idx, idx + len(phrase))

    m = re.search(
        r"\b(?:by\s+|on\s+|this\s+|next\s+)?(mon|tue|wed|thu|fri|sat|sun)[a-z]*\b", low
    )
    if m:
        target = _WEEKDAY_ABBR[m.group(1)]
        ahead = (target - today.weekday()) % 7
        if ahead == 0 or "next" in m.group(0):
            ahead = ahead or 7
            if "next" in m.group(0) and ahead < 7:
                ahead += 7
        return today + timedelta(days=ahead), _strip(t, m.start(), m.end())

    idx = low.find("next week")
    if idx >= 0:
        ahead = (7 - today.weekday()) % 7 or 7  # next Monday
        return today + timedelta(days=ahead), _strip(t, idx, idx + len("next week"))
    idx = low.find("end of week")
    if idx >= 0 or "eow" in low.split():
        ahead = (4 - today.weekday()) % 7
        i = idx if idx >= 0 else low.find("eow")
        n = len("end of week") if idx >= 0 else 3
        return today + timedelta(days=ahead), _strip(t, i, i + n)

    m = _MONTH_DAY_RE.search(low)
    if m:
        month = next(
            (
                i + 1
                for i, name in enumerate(_MONTHS)
                if name.startswith(m.group(1)[:3])
            ),
            None,
        )
        if month:
            try:
                due = date(today.year, month, int(m.group(2)))
                if due < today - timedelta(days=30):
                    due = date(today.year + 1, month, int(m.group(2)))
                return due, _strip(t, m.start(), m.end())
            except ValueError:
                pass
    return None, t


def _strip(text: str, start: int, end: int) -> str:
    out = (text[:start] + " " + text[end:]).strip()
    out = re.sub(r"\s+", " ", out)
    out = re.sub(r"\s+(by|on|for|before|until)\s*$", "", out, flags=re.IGNORECASE)
    return out.strip(" ,.-—")


def fallback_parse(text: str, *, today: date) -> dict[str, Any]:
    due, title = resolve_due_phrase(text, today=today)
    counterparty = None
    m = re.search(
        r"\b(?:for|to|with)\s+([A-Z][a-zA-Z'\-]+(?:\s+[A-Z][a-zA-Z'\-]+)?)\b", title
    )
    if m:
        counterparty = m.group(1)
    return {
        "title": (title or text).strip()[:300] or text.strip()[:300],
        "detail": None,
        "due_local": due.isoformat() if due else None,
        "counterparty_label": counterparty,
        "counterparty_kind": "person" if counterparty else None,
        "object_label": None,
        "object_kind": None,
        "parser": "fallback",
    }


_PROMPT = """You turn one line a person typed into a structured to-do. Reply with JSON only.

Today is {today} ({weekday}), timezone {tz}.

Text: {text}

Return exactly this shape:
{{
  "title": "imperative noun phrase, the thing to do, without the date words (max 12 words)",
  "detail": "anything else worth keeping from the text, or null",
  "due_local": "YYYY-MM-DD or null — resolve 'Friday', 'tomorrow', 'end of month' etc. relative to today; a bare weekday means the NEXT one",
  "counterparty_label": "who is waiting for this (a person's name or a company), or null when it is just for the writer",
  "counterparty_kind": "person" | "company" | "deal" | null,
  "object_label": "the thing it is about — a document, a deal, a meeting — or null",
  "object_kind": "document" | "deal" | "event" | "thread" | null
}}
Never invent a date that is not implied. Never put the person's name in the title if it is the counterparty ("Send Sam the deck" → title "Send the Q3 deck", counterparty "Sam")."""


@attribute_to("todos")
def parse_todo_text(text: str, *, today: date, tz_name: str) -> dict[str, Any]:
    """Structured read of a typed to-do. LLM first, deterministic on failure."""
    text = (text or "").strip()
    if not text:
        raise ValueError("nothing to parse")
    fallback = fallback_parse(text, today=today)
    try:
        from rubik_core.priorities.agents.base import LLMJsonClient

        client = LLMJsonClient(model_key="gemini-flash-lite", temperature=0.0)
        raw = client.invoke_json(
            _PROMPT.format(
                today=today.isoformat(),
                weekday=_WEEKDAYS[today.weekday()].title(),
                tz=tz_name,
                text=text.replace("\n", " ")[:1000],
            ),
            fallback=None,
            attempts=2,
        )
    except Exception:  # noqa: BLE001 — degrade to the deterministic read
        logger.warning("promises: LLM parse failed, using fallback", exc_info=True)
        return fallback
    if not isinstance(raw, dict):
        return fallback
    out = dict(fallback)
    title = (raw.get("title") or "").strip()
    if title:
        out["title"] = title[:300]
    out["detail"] = (raw.get("detail") or None) or None
    due = raw.get("due_local")
    if isinstance(due, str) and _ISO_RE.fullmatch(due.strip()):
        out["due_local"] = due.strip()
    elif due in (None, "", "null"):
        # Trust the model's "no date" only when the fallback saw none either;
        # a fallback date means the words were plainly there.
        out["due_local"] = fallback["due_local"]
    cp = (raw.get("counterparty_label") or "").strip() or None
    out["counterparty_label"] = cp
    kind = raw.get("counterparty_kind")
    out["counterparty_kind"] = (
        kind
        if (cp and kind in ("person", "company", "deal"))
        else ("person" if cp else None)
    )
    obj = (raw.get("object_label") or "").strip() or None
    out["object_label"] = obj
    okind = raw.get("object_kind")
    out["object_kind"] = (
        okind if (obj and okind in ("document", "deal", "event", "thread")) else None
    )
    out["parser"] = "llm"
    return out


def resolve_counterparty(db: Session, label: str | None) -> dict[str, Any] | None:
    """A person label → the company user it names, when exactly one matches.

    Matches a full name, a first name, or an email prefix against ``users``
    (case-folded). Ambiguous or unknown → ``None`` and the label stays as typed.
    """
    label = (label or "").strip()
    if not label:
        return None
    try:
        from rubik_core.database.models import User

        low = label.lower()
        if "@" in low:
            row = (
                db.query(User)
                .filter(
                    func.lower(User.email) == low, User.is_platform_support.is_(False)
                )
                .first()
            )
            return _person(row) if row else None
        rows = (
            db.query(User)
            .filter(func.lower(User.name) == low, User.is_platform_support.is_(False))
            .all()
        )
        if len(rows) == 1:
            return _person(rows[0])
        if not rows:
            rows = (
                db.query(User)
                .filter(
                    func.lower(User.name).like(f"{low} %"),
                    User.is_platform_support.is_(False),
                )
                .all()
            )
            if len(rows) != 1:
                rows = (
                    db.query(User)
                    .filter(
                        func.lower(User.email).like(f"{low}@%"),
                        User.is_platform_support.is_(False),
                    )
                    .all()
                )
            if len(rows) == 1:
                return _person(rows[0])
    except Exception:  # noqa: BLE001 — resolution is a nicety, never a blocker
        logger.debug(
            "promises: counterparty lookup failed for %r", label, exc_info=True
        )
    return None


def _person(row) -> dict[str, Any]:
    return {
        "counterparty_kind": "person",
        "counterparty_id": (row.email or "").strip().lower(),
        "counterparty_label": row.name or row.email,
    }

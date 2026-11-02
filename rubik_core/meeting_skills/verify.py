"""Deterministic checks on the model's cited answer. No model call.

Grounded or dropped: every item must cite a transcript line and quote it, and
the quote must really be in that line (or the line either side, since a model
can be one off). Then, per item:

* **owner** — must be someone who spoke or was invited; otherwise the item
  stays but goes under "Owner not clear" instead of a guessed name;
* **due date** — every word of it must appear in a cited quote; otherwise the
  date is dropped (the item stays);
* **numbers** — every figure in the item must appear in a cited quote;
  otherwise the item is marked unverified.

Nothing is silently kept: what was dropped, and why, is returned for the run
log and summarised to the person.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from rubik_core.meeting_skills.normalize import NormalizedMeeting, Utterance

MIN_QUOTE_CHARS = 4
MAX_QUOTE_CHARS = 400

FLAG_OWNER_UNCLEAR = "owner_unclear"
FLAG_DUE_DROPPED = "due_dropped"
FLAG_UNVERIFIED_NUMBER = "unverified_number"

_NUMBER = re.compile(r"\d[\d,.]*\d|\d")
_WORD = re.compile(r"[a-z0-9]+")
_DUE_FILLER = frozenset(
    {"by", "the", "on", "of", "end", "eod", "before", "until", "at", "in"}
)
_OWNER_FILLER = frozenset({"the", "and", "team", "mr", "ms", "mrs", "dr"})


def norm(text: str) -> str:
    """Lower-case, quotes and dashes folded, punctuation gone, spaces collapsed."""
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


def _digits(text: str) -> str:
    return re.sub(r"[,\s]", "", text)


@dataclass(frozen=True)
class Cite:
    meeting_no: int
    utterance: Utterance
    quote: str


@dataclass
class VerifiedItem:
    label: str
    text: str
    owner: str = ""
    due: str = ""
    topic: str = ""
    kind: str = ""
    cites: list[Cite] = field(default_factory=list)
    flags: set[str] = field(default_factory=set)


@dataclass
class Verified:
    sections: dict[str, list[VerifiedItem]]
    dropped: list[dict] = field(default_factory=list)
    kept: int = 0
    flagged: int = 0

    def report(self) -> dict:
        return {
            "kept": self.kept,
            "flagged": self.flagged,
            "dropped": self.dropped,
        }


def _find_quote(meeting: NormalizedMeeting, idx: int, quote: str) -> Utterance | None:
    """The utterance holding ``quote`` at ``idx`` or one either side, or None."""
    q = norm(quote)
    if len(q) < MIN_QUOTE_CHARS:
        return None
    lines = meeting.utterances
    by_idx = {u.idx: u for u in lines}
    for candidate in (idx, idx - 1, idx + 1):
        u = by_idx.get(candidate)
        if u is None:
            continue
        if q in norm(u.text):
            return u
        nxt = by_idx.get(candidate + 1)
        if nxt is not None and q in norm(f"{u.text} {nxt.text}"):
            return u
    return None


def _cites(item: dict, meetings: list[NormalizedMeeting]) -> list[Cite]:
    out: list[Cite] = []
    seen: set[tuple[int, int]] = set()
    for ev in item.get("evidence") or []:
        if not isinstance(ev, dict):
            continue
        try:
            m = int(ev.get("m") or 1)
            u = int(ev.get("u"))
        except (TypeError, ValueError):
            continue
        quote = str(ev.get("quote") or "").strip().strip('"').strip()
        if not (1 <= m <= len(meetings)) or len(quote) > MAX_QUOTE_CHARS:
            continue
        found = _find_quote(meetings[m - 1], u, quote)
        if found is None or (m, found.idx) in seen:
            continue
        seen.add((m, found.idx))
        out.append(Cite(meeting_no=m, utterance=found, quote=quote))
    return out


def _known_people(meetings: list[NormalizedMeeting], cites: list[Cite]) -> set[str]:
    """Name tokens of everyone who spoke in, or was invited to, a cited meeting."""
    tokens: set[str] = set()
    for m in {c.meeting_no for c in cites} or {1}:
        meeting = meetings[m - 1]
        for name in meeting.speakers:
            tokens.update(t for t in _WORD.findall(norm(name)) if len(t) >= 2)
        for email in meeting.attendees:
            local = email.split("@", 1)[0]
            tokens.update(t for t in re.split(r"[._\-+]", local.lower()) if len(t) >= 2)
            tokens.add(email.lower())
    return tokens


def _owner_ok(owner: str, people: set[str]) -> bool:
    if "@" in owner:
        return owner.strip().lower() in people
    words = [w for w in _WORD.findall(norm(owner)) if w not in _OWNER_FILLER]
    return bool(words) and any(w in people for w in words)


def _due_ok(due: str, quotes: str) -> bool:
    words = [w for w in _WORD.findall(norm(due)) if w not in _DUE_FILLER]
    return bool(words) and all(w in quotes.split() for w in words)


def _numbers_ok(text: str, quotes: str) -> bool:
    haystack = _digits(quotes)
    return all(_digits(n) in haystack for n in _NUMBER.findall(text or ""))


def verify(
    sections_out: list[dict],
    meetings: list[NormalizedMeeting],
    section_keys: list[str],
) -> Verified:
    """Check the model's ``sections_out`` against ``meetings``.

    ``section_keys`` is the template's own order; a section the model invented
    is dropped whole, a section it skipped comes back empty.
    """
    result = Verified(sections={key: [] for key in section_keys})
    for section in sections_out or []:
        if not isinstance(section, dict):
            continue
        key = str(section.get("key") or "")
        if key not in result.sections:
            for item in section.get("items") or []:
                result.dropped.append(
                    {
                        "section": key,
                        "text": str((item or {}).get("text", ""))[:160],
                        "reason": "unknown_section",
                    }
                )
            continue
        for item in section.get("items") or []:
            if not isinstance(item, dict):
                continue
            text = " ".join(str(item.get("text") or "").split())
            label = " ".join(str(item.get("label") or "").split())
            if not text:
                continue
            cites = _cites(item, meetings)
            if not cites:
                result.dropped.append(
                    {"section": key, "text": text[:160], "reason": "no_source"}
                )
                continue
            quotes = " ".join(norm(c.quote) for c in cites)
            verified = VerifiedItem(
                label=label,
                text=text,
                topic=" ".join(str(item.get("topic") or "").split()),
                kind=str(item.get("kind") or ""),
                cites=cites,
            )
            owner = " ".join(str(item.get("owner") or "").split())
            if owner:
                if _owner_ok(owner, _known_people(meetings, cites)):
                    verified.owner = owner
                else:
                    verified.flags.add(FLAG_OWNER_UNCLEAR)
            due = " ".join(str(item.get("due") or "").split())
            if due:
                if _due_ok(due, quotes):
                    verified.due = due
                else:
                    verified.flags.add(FLAG_DUE_DROPPED)
            if not _numbers_ok(f"{label} {text}", " ".join(c.quote for c in cites)):
                verified.flags.add(FLAG_UNVERIFIED_NUMBER)
            result.sections[key].append(verified)
            result.kept += 1
            if verified.flags:
                result.flagged += 1
    return result

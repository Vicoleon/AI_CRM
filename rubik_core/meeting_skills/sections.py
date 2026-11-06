"""A Template's sections: title, what goes in it, and how it is laid out.

Cubie writes a template from whatever the person asks for, so the sections
arrive as text the chat model wrote — one section per line::

    Overview (paragraph): who met, why, and the outcome in 2-4 sentences.
    Pain points: each pain, who stated it, in their words.
    Budget and timeline: only what was actually said.
    Action items (checklist): grouped by owner, a due date only if one was said.

``parse_sections`` turns that into the stored shape and refuses anything it
cannot read rather than guessing. A JSON list of ``{"title", "instruction",
"format"}`` is accepted too.

Formats:

* ``paragraph`` — flowing sentences, each cited;
* ``bullets`` — ``- **Short label**: one or two factual sentences``;
* ``topics`` — a heading per topic, bullets under each;
* ``checklist`` — ``- [ ]`` items grouped under their owner.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

FORMAT_PARAGRAPH = "paragraph"
FORMAT_BULLETS = "bullets"
FORMAT_TOPICS = "topics"
FORMAT_CHECKLIST = "checklist"
FORMATS = (FORMAT_PARAGRAPH, FORMAT_BULLETS, FORMAT_TOPICS, FORMAT_CHECKLIST)

MAX_SECTIONS = 12
MAX_TITLE_CHARS = 80
MAX_INSTRUCTION_CHARS = 600

_FORMAT_ALIASES = {
    "paragraph": FORMAT_PARAGRAPH,
    "prose": FORMAT_PARAGRAPH,
    "text": FORMAT_PARAGRAPH,
    "summary": FORMAT_PARAGRAPH,
    "bullets": FORMAT_BULLETS,
    "bullet": FORMAT_BULLETS,
    "list": FORMAT_BULLETS,
    "bulleted": FORMAT_BULLETS,
    "topics": FORMAT_TOPICS,
    "topic": FORMAT_TOPICS,
    "per topic": FORMAT_TOPICS,
    "by topic": FORMAT_TOPICS,
    "checklist": FORMAT_CHECKLIST,
    "checkboxes": FORMAT_CHECKLIST,
    "to-dos": FORMAT_CHECKLIST,
    "todos": FORMAT_CHECKLIST,
    "owner checklist": FORMAT_CHECKLIST,
    "by owner": FORMAT_CHECKLIST,
    "tasks": FORMAT_CHECKLIST,
}

_LINE = re.compile(
    r"^\s*(?:#{1,4}\s*|[-*•]\s*|\d{1,2}[.)]\s*)?"
    r"(?P<title>[^:(\n]{1,120}?)\s*"
    r"(?:\((?P<fmt>[^)]{1,30})\))?\s*"
    r"(?::|—|–|\s-\s)\s*(?P<instruction>\S.*)$"
)
_CHECKLIST_TITLE = re.compile(
    r"\b(action items?|next steps?|to-?dos?|follow[- ]ups?|tasks?|commitments)\b",
    re.IGNORECASE,
)
_PARAGRAPH_TITLE = re.compile(r"^(overview|summary|tl;?dr|executive summary)\b", re.I)


class SectionsError(ValueError):
    """The sections could not be read. The message says why, for the model."""


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    instruction: str
    format: str = FORMAT_BULLETS
    required: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


def _format_for(title: str, fmt: str | None) -> str:
    if fmt:
        named = _FORMAT_ALIASES.get(fmt.strip().lower())
        if named:
            return named
    if _CHECKLIST_TITLE.search(title):
        return FORMAT_CHECKLIST
    if _PARAGRAPH_TITLE.search(title.strip()):
        return FORMAT_PARAGRAPH
    return FORMAT_BULLETS


def _from_rows(rows: list[tuple[str, str, str | None]]) -> list[Section]:
    if not rows:
        raise SectionsError(
            "No sections found. Give one section per line, as 'Title: what goes in it'."
        )
    if len(rows) > MAX_SECTIONS:
        raise SectionsError(f"A template has at most {MAX_SECTIONS} sections.")
    out: list[Section] = []
    seen: set[str] = set()
    for i, (title, instruction, fmt) in enumerate(rows, start=1):
        title = " ".join((title or "").split())
        instruction = " ".join((instruction or "").split())
        if not title:
            raise SectionsError(f"Section {i} has no title.")
        if len(title) > MAX_TITLE_CHARS:
            raise SectionsError(
                f"Section {i}'s title is longer than {MAX_TITLE_CHARS} characters."
            )
        if not instruction:
            raise SectionsError(f"Section '{title}' does not say what goes in it.")
        if len(instruction) > MAX_INSTRUCTION_CHARS:
            raise SectionsError(
                f"Section '{title}' has more than {MAX_INSTRUCTION_CHARS} characters "
                "of instruction; keep it to what goes in the section."
            )
        if title.lower() in seen:
            raise SectionsError(f"Two sections are called '{title}'.")
        seen.add(title.lower())
        out.append(
            Section(
                key=f"s{i}",
                title=title,
                instruction=instruction,
                format=_format_for(title, fmt),
            )
        )
    return out


def parse_sections(raw: str | list | None) -> list[Section]:
    """The sections in ``raw`` (lines or JSON). Raises :class:`SectionsError`."""
    if isinstance(raw, list):
        items = raw
    else:
        text = (raw or "").strip()
        if not text:
            raise SectionsError("No sections given.")
        if text.startswith("["):
            try:
                items = json.loads(text)
            except ValueError as exc:
                raise SectionsError(f"The sections are not valid JSON: {exc}") from exc
        else:
            rows: list[tuple[str, str, str | None]] = []
            for line in text.splitlines():
                if not line.strip():
                    continue
                match = _LINE.match(line)
                if not match:
                    raise SectionsError(
                        f"Could not read this section line: {line.strip()[:120]!r}. "
                        "Write each as 'Title: what goes in it'."
                    )
                rows.append(
                    (
                        match.group("title"),
                        match.group("instruction"),
                        match.group("fmt"),
                    )
                )
            return _from_rows(rows)
    rows = []
    for item in items:
        if not isinstance(item, dict):
            raise SectionsError("Each section must have a title and an instruction.")
        rows.append(
            (
                str(item.get("title") or ""),
                str(item.get("instruction") or item.get("description") or ""),
                item.get("format"),
            )
        )
    return _from_rows(rows)


def sections_from_json(stored: list[dict] | None) -> list[Section]:
    """Stored sections (already validated) back into objects."""
    out = []
    for i, item in enumerate(stored or [], start=1):
        fmt = item.get("format") or FORMAT_BULLETS
        out.append(
            Section(
                key=item.get("key") or f"s{i}",
                title=item.get("title") or f"Section {i}",
                instruction=item.get("instruction") or "",
                format=fmt if fmt in FORMATS else FORMAT_BULLETS,
                required=bool(item.get("required", True)),
            )
        )
    return out


def sections_to_json(sections: list[Section]) -> list[dict]:
    return [s.to_dict() for s in sections]


def describe_sections(sections: list[Section]) -> str:
    """One line per section, as the person approves them."""
    return "\n".join(
        f"{i}. {s.title} ({s.format}) — {s.instruction}"
        for i, s in enumerate(sections, start=1)
    )


#: The shape used when nobody named one: Plaud's general meeting summary.
DEFAULT_SECTIONS: list[Section] = _from_rows(
    [
        (
            "Overview",
            "2-4 sentences: who met, why, and what came out of it.",
            "paragraph",
        ),
        (
            "Topics",
            "A heading per topic discussed, then the key points under it.",
            "topics",
        ),
        ("Decisions", "Only decisions explicitly made in the meeting.", "bullets"),
        (
            "Risks and open questions",
            "Concerns raised and questions left unanswered.",
            "bullets",
        ),
        (
            "Action items",
            "Grouped by owner; a due date only if someone said one.",
            "checklist",
        ),
    ]
)

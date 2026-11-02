"""Lay out what survived verification, in the template's order, with citations.

House style (Plaud's): a heading per section; bullets as ``- **Short label**:
one or two factual sentences``; action items as ``- [ ]`` checkboxes grouped
under their owner, with a due date only when one was said. Every point ends in
``[n]``, and ``citations`` carries the speaker, the time and the quote for each
``n`` — the Chat page shows them under Sources, one card per meeting.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rubik_core.brand import PRODUCT_NAME
from rubik_core.meeting_skills.normalize import (
    TIMING_APPROXIMATE,
    NormalizedMeeting,
)
from rubik_core.meeting_skills.sections import (
    FORMAT_CHECKLIST,
    FORMAT_PARAGRAPH,
    FORMAT_TOPICS,
    Section,
)
from rubik_core.meeting_skills.verify import (
    FLAG_OWNER_UNCLEAR,
    FLAG_UNVERIFIED_NUMBER,
    Verified,
    VerifiedItem,
)

#: The Sources card title prefix. ChatSources groups by this path and shows its
#: last segment as the title, so a meeting's quotes fold into one card.
SOURCE_PREFIX = "Meeting transcripts/"
_UNCLEAR_OWNER = "Owner not clear"


@dataclass
class Rendered:
    markdown: str
    citations: list[dict] = field(default_factory=list)


class _Citer:
    def __init__(self, meetings: list[NormalizedMeeting]):
        self.meetings = meetings
        self.citations: list[dict] = []
        self._numbers: dict[tuple[int, int], int] = {}

    def marks(self, item: VerifiedItem) -> str:
        numbers = []
        for cite in item.cites:
            key = (cite.meeting_no, cite.utterance.idx)
            if key not in self._numbers:
                n = len(self._numbers) + 1
                self._numbers[key] = n
                meeting = self.meetings[cite.meeting_no - 1]
                stamp = meeting.stamp(cite.utterance)
                who = cite.utterance.speaker + (f" · {stamp}" if stamp else "")
                title = (meeting.title or "Meeting").replace("/", "-").strip()
                self.citations.append(
                    {
                        "source_path": f"{SOURCE_PREFIX}{title}",
                        "page": None,
                        "chunk_index": n,
                        "snippet": f"[{n}] {who} — “{cite.quote}”",
                    }
                )
            numbers.append(self._numbers[key])
        return "".join(f"[{n}]" for n in sorted(set(numbers)))


def _suffix(item: VerifiedItem) -> str:
    return (
        " *(figure not stated in the quote — check it)*"
        if (FLAG_UNVERIFIED_NUMBER in item.flags)
        else ""
    )


def _bullet(item: VerifiedItem, citer: _Citer) -> str:
    label = f"**{item.label}**: " if item.label else ""
    return f"- {label}{item.text} {citer.marks(item)}{_suffix(item)}"


def _section(section: Section, items: list[VerifiedItem], citer: _Citer) -> str:
    out = [f"## {section.title}"]
    if not items:
        out.append(f"_No {section.title.lower()} recorded._")
        return "\n".join(out)
    if section.format == FORMAT_PARAGRAPH:
        out.append(
            " ".join(f"{i.text} {citer.marks(i)}{_suffix(i)}".strip() for i in items)
        )
    elif section.format == FORMAT_TOPICS:
        groups: dict[str, list[VerifiedItem]] = {}
        for item in items:
            groups.setdefault(item.topic or "Other", []).append(item)
        for topic, group in groups.items():
            out.append(f"### {topic}")
            out.extend(_bullet(i, citer) for i in group)
    elif section.format == FORMAT_CHECKLIST:
        groups = {}
        for item in items:
            owner = (
                item.owner
                if (item.owner and FLAG_OWNER_UNCLEAR not in item.flags)
                else _UNCLEAR_OWNER
            )
            groups.setdefault(owner, []).append(item)
        ordered = sorted(groups, key=lambda o: (o == _UNCLEAR_OWNER, o.lower()))
        for owner in ordered:
            out.append(f"**{owner}**" if owner == _UNCLEAR_OWNER else f"**@{owner}**")
            for item in groups[owner]:
                due = f" — due {item.due}" if item.due else ""
                out.append(f"- [ ] {item.text}{due} {citer.marks(item)}{_suffix(item)}")
    else:
        out.extend(_bullet(i, citer) for i in items)
    return "\n".join(out)


def _header(meetings: list[NormalizedMeeting], heading: str) -> str:
    def when(m: NormalizedMeeting) -> str:
        return f" — {m.started_at:%a %b %d, %Y}" if m.started_at else ""

    lines = [f"# {heading}"] if heading else []
    if len(meetings) == 1:
        lines.append(f"**{meetings[0].title}**{when(meetings[0])}")
    else:
        lines.append(f"From {len(meetings)} meetings:")
        lines.extend(f"- {m.title}{when(m)}" for m in meetings)
    return "\n".join(lines)


def render(
    verified: Verified,
    sections: list[Section],
    meetings: list[NormalizedMeeting],
    *,
    heading: str = "",
) -> Rendered:
    citer = _Citer(meetings)
    parts = [_header(meetings, heading)]
    parts.extend(_section(s, verified.sections.get(s.key, []), citer) for s in sections)
    notes = ["Every point cites the transcript line it came from (see Sources)."]
    if verified.dropped:
        n = len(verified.dropped)
        notes.append(
            f"{n} point{'s' if n != 1 else ''} the transcript did not support "
            f"{'were' if n != 1 else 'was'} left out."
        )
    if any(m.timing == TIMING_APPROXIMATE for m in meetings):
        notes.append(
            "Times marked ~ are approximate: Google's transcript marks the time "
            "every few minutes."
        )
    if any(m.truncated for m in meetings):
        notes.append(
            f"A transcript was longer than {PRODUCT_NAME} reads; its end was not read."
        )
    parts.append("_" + " ".join(notes) + "_")
    return Rendered(
        markdown="\n\n".join(p for p in parts if p), citations=citer.citations
    )

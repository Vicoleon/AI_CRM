"""A stored transcript, as numbered lines a Skill can cite.

Cubie keeps each meeting transcript as the text of the Google Doc it came from
(``transcript_sync`` exports the Meet transcript / "Notes by Gemini" Doc from
the person's Drive every 15 minutes), so this module reads that text back into
utterances: ``[n] Speaker (time): words``. A cited fact names an utterance
number and quotes it, and ``verify`` checks the quote is really there.

Sources and how much timing each carries:

* **Notes by Gemini** — Gemini's notes tab (kept as ``outline``, never as
  evidence), its footer, then the "📖 Transcript" tab: ``Name: words`` lines
  under bare ``00:12:34`` markers every few minutes. Times are the marker the
  line falls under, so they are approximate (``timing == "approximate"``).
* **Meet transcript Doc** (Meet Recordings folder) — a ``<title> - Transcript``
  header, an attendee list, then the same markers and lines.
* **Call Capture** — a ``Sales call transcript`` header, then ``Speaker:
  words``. The loader swaps in the stored per-line entries, which carry exact
  millisecond offsets (``timing == "exact"``).
* **Fathom** — ``[Speaker]: words`` with no times, then Fathom's own summary
  under ``## …(from Fathom)`` headings (kept as ``outline``).

A speaker is whoever the source names. Nothing here guesses a name: an
unnamed speaker stays as the source wrote it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime

TIMING_EXACT = "exact"
TIMING_APPROXIMATE = "approximate"
TIMING_NONE = "none"

SOURCE_GEMINI = "gemini_notes"
SOURCE_MEET = "meet_transcript"
SOURCE_CALL_CAPTURE = "call_capture"
SOURCE_FATHOM = "fathom"
SOURCE_PLAIN = "plain"

_TS_LINE = re.compile(r"^\s*(\d{1,2}):(\d{2}):(\d{2})\s*$")
_GEMINI_TAB = re.compile(r"^\s*📖\s*transcript\s*$", re.IGNORECASE)
_TRANSCRIPT_TITLE = re.compile(r"\s-\s+transcript\s*$", re.IGNORECASE)
_GEMINI_FOOTER = re.compile(r"^\s*you\s+should\s+review\s+gemini", re.IGNORECASE)
_TRANSCRIPT_END = re.compile(
    r"^\s*(?:transcription\s+ended\s+after|this\s+editable\s+transcript\s+was\s+"
    r"computer\s+generated)",
    re.IGNORECASE,
)
_FATHOM_NOTES = re.compile(r"^\s*#{1,4}\s+.*\(from fathom\)\s*$", re.IGNORECASE)
_CCS_FIRST_LINE = "sales call transcript"
_CONFERENCE = re.compile(r"^Conference:\s*(conferenceRecords/\S+)\s*$", re.MULTILINE)
_SPEAKER_LINE = re.compile(
    r"^\s*(?P<open>\[)?(?P<speaker>[^\[\]:\n]{1,60}?)(?(open)\])\s*:\s+(?P<text>\S.*)$"
)
# Words that open a notes line ("Summary: …") rather than name a speaker.
_NOT_A_SPEAKER = frozenset(
    {
        "summary",
        "details",
        "note",
        "notes",
        "action items",
        "next steps",
        "suggested next steps",
        "decisions",
        "attendees",
        "invited",
        "transcript",
        "agenda",
        "date",
        "ae",
        "speakers",
        "conference",
        "http",
        "https",
    }
)


@dataclass(frozen=True)
class Utterance:
    idx: int
    speaker: str
    text: str
    start_ms: int | None = None


@dataclass
class NormalizedMeeting:
    doc_id: str
    title: str
    started_at: datetime | None
    #: Participant emails (the meeting_participants ACL), lower-cased.
    attendees: list[str]
    utterances: list[Utterance]
    #: The source's own notes (Gemini's summary, Fathom's action items). Read
    #: as context only: a cited fact must come from an utterance.
    outline: str = ""
    source: str = SOURCE_PLAIN
    timing: str = TIMING_NONE
    content_hash: str = ""
    #: Set when a conference record is named in the text (Call Capture).
    conference_record: str | None = None
    #: Lines dropped because the text was longer than the reading budget.
    truncated: bool = False
    speakers: list[str] = field(default_factory=list)

    def stamp(self, utterance: Utterance) -> str:
        """The time an utterance is cited with, or "" when the source has none."""
        if utterance.start_ms is None or self.timing == TIMING_NONE:
            return ""
        text = format_ms(utterance.start_ms)
        return f"~{text}" if self.timing == TIMING_APPROXIMATE else text


def format_ms(ms: int) -> str:
    seconds = max(0, int(ms)) // 1000
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", "ignore")).hexdigest()


def _plausible_speaker(name: str) -> bool:
    name = name.strip()
    if not name or not name[0].isalpha():
        return False
    if name.lower() in _NOT_A_SPEAKER:
        return False
    return len(name.split()) <= 6


def _first_index(lines: list[str], pattern: re.Pattern, start: int = 0, stop=None):
    end = len(lines) if stop is None else min(stop, len(lines))
    for i in range(start, end):
        if pattern.search(lines[i]):
            return i
    return None


def parse_transcript(text: str) -> tuple[list[Utterance], str, str, str]:
    """``(utterances, outline, source, timing)`` for a stored transcript's text.

    Pure: no I/O, so every source format has a golden test.
    """
    lines = (text or "").splitlines()
    if not lines:
        return [], "", SOURCE_PLAIN, TIMING_NONE

    source = SOURCE_PLAIN
    outline_lines: list[str] = []
    start = 0

    gemini_tab = _first_index(lines, _GEMINI_TAB)
    if gemini_tab is not None:
        source = SOURCE_GEMINI
        footer = _first_index(lines, _GEMINI_FOOTER, 0, gemini_tab)
        outline_lines = lines[: footer if footer is not None else gemini_tab]
        start = gemini_tab + 1
    elif lines[0].strip().lower() == _CCS_FIRST_LINE:
        source = SOURCE_CALL_CAPTURE
        blank = next((i for i, ln in enumerate(lines) if not ln.strip()), None)
        start = (blank + 1) if blank is not None else 1
    else:
        title_line = _first_index(lines, _TRANSCRIPT_TITLE, 0, 15)
        if title_line is not None:
            source = SOURCE_MEET
            start = title_line + 1

    # A Meet/Gemini transcript's dialogue begins at its first time marker; the
    # lines before it are the date, the title and the attendee list.
    if source in (SOURCE_GEMINI, SOURCE_MEET):
        first_marker = _first_index(lines, _TS_LINE, start)
        if first_marker is not None:
            start = first_marker

    utterances: list[Utterance] = []
    current_ms: int | None = None
    saw_marker = False
    fathom_seen = False
    for i in range(start, len(lines)):
        line = lines[i]
        if _FATHOM_NOTES.match(line):
            # Fathom's own summary follows the dialogue: context, not evidence.
            outline_lines.extend(lines[i:])
            fathom_seen = True
            break
        if _TRANSCRIPT_END.match(line):
            break
        marker = _TS_LINE.match(line)
        if marker:
            h, m, s = (int(g) for g in marker.groups())
            current_ms = ((h * 60 + m) * 60 + s) * 1000
            saw_marker = True
            continue
        if not line.strip():
            continue
        spoken = _SPEAKER_LINE.match(line)
        if spoken and _plausible_speaker(spoken.group("speaker")):
            if spoken.group("open") and source == SOURCE_PLAIN:
                source = SOURCE_FATHOM
            utterances.append(
                Utterance(
                    idx=len(utterances) + 1,
                    speaker=spoken.group("speaker").strip(),
                    text=spoken.group("text").strip(),
                    start_ms=current_ms,
                )
            )
        elif utterances:
            # A wrapped line belongs to whoever was speaking.
            last = utterances[-1]
            utterances[-1] = Utterance(
                idx=last.idx,
                speaker=last.speaker,
                text=f"{last.text} {line.strip()}",
                start_ms=last.start_ms,
            )
    if fathom_seen and source == SOURCE_PLAIN:
        source = SOURCE_FATHOM

    timing = TIMING_APPROXIMATE if saw_marker else TIMING_NONE
    outline = "\n".join(ln for ln in outline_lines if ln.strip()).strip()
    return utterances, outline, source, timing


def conference_record_in(text: str) -> str | None:
    """The ``conferenceRecords/…`` id a Call Capture transcript names, if any."""
    match = _CONFERENCE.search(text or "")
    return match.group(1) if match else None


def normalize(
    *,
    doc_id: str,
    title: str,
    text: str,
    attendees: list[str],
    started_at: datetime | None = None,
    truncated: bool = False,
) -> NormalizedMeeting:
    utterances, outline, source, timing = parse_transcript(text)
    return NormalizedMeeting(
        doc_id=doc_id,
        title=title,
        started_at=started_at,
        attendees=sorted({(a or "").strip().lower() for a in attendees if a}),
        utterances=utterances,
        outline=outline,
        source=source,
        timing=timing,
        content_hash=content_hash(text),
        conference_record=conference_record_in(text)
        if source == SOURCE_CALL_CAPTURE
        else None,
        truncated=truncated,
        speakers=sorted({u.speaker for u in utterances}),
    )


def with_exact_entries(
    meeting: NormalizedMeeting, entries: list[tuple[str, str, int | None]]
) -> NormalizedMeeting:
    """Replace a Call Capture meeting's lines with its stored per-line entries
    (speaker, text, start offset in ms), which carry exact times."""
    utterances = [
        Utterance(
            idx=i + 1, speaker=(speaker or "Unknown speaker"), text=text, start_ms=ms
        )
        for i, (speaker, text, ms) in enumerate(
            e for e in entries if (e[1] or "").strip()
        )
    ]
    if not utterances:
        return meeting
    meeting.utterances = utterances
    meeting.speakers = sorted({u.speaker for u in utterances})
    has_times = any(u.start_ms is not None for u in utterances)
    meeting.timing = TIMING_EXACT if has_times else TIMING_NONE
    return meeting


def numbered_transcript(meeting: NormalizedMeeting, number: int, max_chars: int) -> str:
    """The transcript as the model reads it: ``[M<number>:<idx>] (time) Speaker: words``.

    Cut at ``max_chars`` on a line boundary, and ``meeting.truncated`` is set
    when anything was left out, so the answer can say it read only part.
    """
    out: list[str] = []
    used = 0
    for u in meeting.utterances:
        stamp = meeting.stamp(u)
        line = (
            f"[M{number}:{u.idx}]"
            + (f" ({stamp})" if stamp else "")
            + (f" {u.speaker}: {u.text}")
        )
        if used + len(line) + 1 > max_chars:
            meeting.truncated = True
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)

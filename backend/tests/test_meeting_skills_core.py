"""Meeting Skills — reading a transcript, checking the model, laying it out.

The invariants:

* every transcript source Cubie stores reads back as numbered lines with the
  speaker the source named, and the source's own notes never become evidence;
* grounded or dropped: an item whose quote is not in the cited line is gone,
  an owner nobody heard of is not printed, a date nobody said is removed, a
  figure that is not in the quote is marked;
* the write-up keeps the template's order and every point carries its [n].
"""

from __future__ import annotations

from datetime import datetime

import pytest

from rubik_core.meeting_skills import normalize as nz
from rubik_core.meeting_skills.render import render
from rubik_core.meeting_skills.sections import (
    DEFAULT_SECTIONS,
    FORMAT_CHECKLIST,
    FORMAT_PARAGRAPH,
    FORMAT_TOPICS,
    SectionsError,
    parse_sections,
)
from rubik_core.meeting_skills.verify import (
    FLAG_DUE_DROPPED,
    FLAG_OWNER_UNCLEAR,
    FLAG_UNVERIFIED_NUMBER,
    verify,
)

# A "Notes by Gemini" Doc exported as text/plain with transcription on: the
# notes tab, Gemini's footer, the transcript tab with its time markers, and the
# transcript's own closing lines.
GEMINI_EXPORT = """Notes
Oct 1, 2026
Acme weekly
Invited Pat Doe Blair Ellison
Summary
Acme is waiting on budget approval.

Suggested next steps
* Blair Ellison will send the revised SOW by Friday.

You should review Gemini's notes to make sure they're accurate. Get tips and learn how Gemini takes notes
Please provide feedback about using Gemini to take notes in a short survey.
📖 Transcript
Oct 1, 2026
Acme weekly - Transcript
00:00:00

Blair Ellison: Morning Pat, thanks for joining.
Pat Doe: Happy to. We are still waiting on budget approval for the renewal.
Blair Ellison: Understood. I will send the revised SOW by Friday.
00:05:12

Pat Doe: The quote should be priced per seat, around 45 dollars each.
Blair Ellison: Got it, 45 dollars per seat.
Transcription ended after 00:09:30

This editable transcript was computer generated and might contain errors.
"""

MEET_DOC = """Acme weekly (2026-10-01 10:00 GMT-5) - Transcript
Attendees
Blair Ellison, Pat Doe
Transcript
00:00:00

Blair Ellison: Hello everyone.
Pat Doe: Hi there.
"""

CCS_TEXT = """Sales call transcript
AE: bellison@acme.example
Date: 2026-10-01 15:00 UTC
Conference: conferenceRecords/abc-123
Speakers: Blair Ellison, Pat Doe

Blair Ellison: Hello.
Pat Doe: Hi, we need pricing.
"""

FATHOM_TEXT = """[Blair Ellison]: Hello.
[Pat Doe]: We need pricing for the renewal.

## Action Items & Next Steps (from Fathom)
- Blair to send pricing
"""

ATTENDEES = ["bellison@acme.example", "pat.doe@acmecorp.example"]


def _gemini_meeting() -> nz.NormalizedMeeting:
    return nz.normalize(
        doc_id="d" * 64,
        title="Acme weekly",
        text=GEMINI_EXPORT,
        attendees=ATTENDEES,
        started_at=datetime(2026, 10, 1, 15, 0),
    )


# ── reading transcripts ─────────────────────────────────────────────────────


def test_a_gemini_doc_reads_as_its_transcript_tab_with_approximate_times():
    m = _gemini_meeting()

    assert m.source == nz.SOURCE_GEMINI
    assert m.timing == nz.TIMING_APPROXIMATE
    assert [u.speaker for u in m.utterances] == [
        "Blair Ellison",
        "Pat Doe",
        "Blair Ellison",
        "Pat Doe",
        "Blair Ellison",
    ]
    assert [u.start_ms for u in m.utterances] == [0, 0, 0, 312_000, 312_000]
    assert m.stamp(m.utterances[3]) == "~00:05:12"
    # Gemini's own notes are context, never evidence…
    assert "budget approval" in m.outline
    assert "Suggested next steps" in m.outline
    # …and neither the notes, the footer nor the closing lines are dialogue.
    spoken = " ".join(u.text for u in m.utterances)
    assert "Gemini" not in spoken
    assert "computer generated" not in spoken
    assert "Summary" not in spoken


def test_a_meet_transcript_doc_skips_its_header_and_attendee_list():
    utterances, outline, source, timing = nz.parse_transcript(MEET_DOC)
    assert source == nz.SOURCE_MEET
    assert timing == nz.TIMING_APPROXIMATE
    assert [(u.speaker, u.text) for u in utterances] == [
        ("Blair Ellison", "Hello everyone."),
        ("Pat Doe", "Hi there."),
    ]
    assert outline == ""


def test_a_call_capture_transcript_names_its_conference_and_skips_the_header():
    m = nz.normalize(doc_id="c" * 64, title="Call", text=CCS_TEXT, attendees=[])
    assert m.source == nz.SOURCE_CALL_CAPTURE
    assert m.conference_record == "conferenceRecords/abc-123"
    assert [u.speaker for u in m.utterances] == ["Blair Ellison", "Pat Doe"]


def test_call_capture_entries_bring_exact_times():
    m = nz.normalize(doc_id="c" * 64, title="Call", text=CCS_TEXT, attendees=[])
    m = nz.with_exact_entries(
        m, [("Blair Ellison", "Hello.", 1_500), ("Pat Doe", "Hi.", 4_200)]
    )
    assert m.timing == nz.TIMING_EXACT
    assert m.stamp(m.utterances[1]) == "00:00:04"


def test_a_fathom_transcript_keeps_its_summary_out_of_the_dialogue():
    utterances, outline, source, timing = nz.parse_transcript(FATHOM_TEXT)
    assert source == nz.SOURCE_FATHOM
    assert timing == nz.TIMING_NONE
    assert [u.speaker for u in utterances] == ["Blair Ellison", "Pat Doe"]
    assert "Blair to send pricing" in outline
    assert all("send pricing" not in u.text for u in utterances)


def test_a_wrapped_line_belongs_to_whoever_was_speaking():
    utterances, *_ = nz.parse_transcript(
        "Blair Ellison: The first half of a long thought\nthat wrapped onto a second line.\nPat Doe: Ok."
    )
    assert utterances[0].text == (
        "The first half of a long thought that wrapped onto a second line."
    )
    assert len(utterances) == 2


def test_the_transcript_the_model_reads_is_numbered_and_timed():
    m = _gemini_meeting()
    text = nz.numbered_transcript(m, 1, 10_000)
    assert text.splitlines()[3] == (
        "[M1:4] (~00:05:12) Pat Doe: The quote should be priced per seat, "
        "around 45 dollars each."
    )
    short = nz.numbered_transcript(m, 1, 120)
    assert m.truncated is True
    assert short.count("\n") < 4


# ── template sections ───────────────────────────────────────────────────────


def test_sections_written_as_lines_keep_their_order_and_layout():
    sections = parse_sections(
        "Overview (paragraph): who met and why.\n"
        "Pain points: each pain and who stated it.\n"
        "Topics (by topic): a heading per topic.\n"
        "Action items: grouped by owner."
    )
    assert [s.title for s in sections] == [
        "Overview",
        "Pain points",
        "Topics",
        "Action items",
    ]
    assert [s.format for s in sections] == [
        FORMAT_PARAGRAPH,
        "bullets",
        FORMAT_TOPICS,
        # Named like a task list, so it lays out as one without being told.
        FORMAT_CHECKLIST,
    ]
    assert [s.key for s in sections] == ["s1", "s2", "s3", "s4"]


def test_sections_can_come_as_json():
    sections = parse_sections(
        '[{"title": "Budget", "instruction": "Only if stated.", "format": "bullets"}]'
    )
    assert sections[0].title == "Budget"


@pytest.mark.parametrize(
    "raw, message",
    [
        ("", "No sections"),
        ("just some words without a colon", "Could not read"),
        ("A: one\nA: two", "Two sections"),
        ("\n".join(f"S{i}: x" for i in range(13)), "at most 12"),
    ],
)
def test_unreadable_sections_are_refused_not_guessed(raw, message):
    with pytest.raises(SectionsError, match=message):
        parse_sections(raw)


# ── verification ────────────────────────────────────────────────────────────


def _item(text, u, quote, **extra):
    return {
        "label": extra.pop("label", ""),
        "text": text,
        "evidence": [{"m": 1, "u": u, "quote": quote}],
        **extra,
    }


def test_an_item_whose_quote_is_not_in_the_transcript_is_dropped():
    m = _gemini_meeting()
    out = verify(
        [
            {
                "key": "s1",
                "items": [
                    _item(
                        "Pat is waiting on budget.",
                        2,
                        "still waiting on budget approval",
                    ),
                    _item(
                        "They signed yesterday.", 2, "we signed the contract yesterday"
                    ),
                    {"text": "No evidence at all.", "evidence": []},
                ],
            }
        ],
        [m],
        ["s1"],
    )
    assert [i.text for i in out.sections["s1"]] == ["Pat is waiting on budget."]
    assert [d["reason"] for d in out.dropped] == ["no_source", "no_source"]


def test_a_cite_one_line_off_is_corrected_not_dropped():
    m = _gemini_meeting()
    out = verify(
        [
            {
                "key": "s1",
                "items": [
                    _item("Budget pending.", 3, "still waiting on budget approval")
                ],
            }
        ],
        [m],
        ["s1"],
    )
    assert out.sections["s1"][0].cites[0].utterance.idx == 2


def test_owners_dates_and_figures_must_come_from_the_transcript():
    m = _gemini_meeting()
    out = verify(
        [
            {
                "key": "s5",
                "items": [
                    _item(
                        "Send the revised SOW.",
                        3,
                        "I will send the revised SOW by Friday",
                        owner="Blair Ellison",
                        due="Friday",
                    ),
                    _item(
                        "Send the revised SOW.",
                        3,
                        "I will send the revised SOW by Friday",
                        owner="Bob Smith",
                        due="October 30",
                    ),
                    _item(
                        "Priced at 50 dollars per seat.",
                        4,
                        "priced per seat, around 45 dollars each",
                    ),
                    _item(
                        "Priced at 45 dollars per seat.",
                        4,
                        "priced per seat, around 45 dollars each",
                    ),
                ],
            }
        ],
        [m],
        ["s5"],
    )
    good, invented, wrong_figure, right_figure = out.sections["s5"]
    assert (good.owner, good.due, good.flags) == ("Blair Ellison", "Friday", set())
    assert invented.owner == "" and FLAG_OWNER_UNCLEAR in invented.flags
    assert invented.due == "" and FLAG_DUE_DROPPED in invented.flags
    assert FLAG_UNVERIFIED_NUMBER in wrong_figure.flags
    assert FLAG_UNVERIFIED_NUMBER not in right_figure.flags
    assert out.flagged == 2


def test_an_owner_known_only_by_their_invite_still_counts():
    m = _gemini_meeting()
    out = verify(
        [
            {
                "key": "s1",
                "items": [
                    _item(
                        "Follow up.", 2, "still waiting on budget approval", owner="Pat"
                    )
                ],
            }
        ],
        [m],
        ["s1"],
    )
    assert out.sections["s1"][0].owner == "Pat"


def test_a_section_the_model_invented_is_dropped_whole():
    m = _gemini_meeting()
    out = verify(
        [
            {
                "key": "s99",
                "items": [_item("Extra.", 2, "still waiting on budget approval")],
            }
        ],
        [m],
        ["s1"],
    )
    assert out.sections == {"s1": []}
    assert out.dropped[0]["reason"] == "unknown_section"


def test_a_quote_may_run_across_two_lines():
    m = _gemini_meeting()
    out = verify(
        [
            {
                "key": "s1",
                "items": [
                    _item(
                        "Blair closed and Pat replied.",
                        1,
                        "thanks for joining. Pat Doe Happy to",
                    )
                ],
            }
        ],
        [m],
        ["s1"],
    )
    # "Pat Doe" is the speaker label, not part of the spoken text, so this
    # quote is NOT in the transcript.
    assert out.sections["s1"] == []
    out = verify(
        [
            {
                "key": "s1",
                "items": [
                    _item("Blair thanked Pat.", 1, "thanks for joining. Happy to")
                ],
            }
        ],
        [m],
        ["s1"],
    )
    assert len(out.sections["s1"]) == 1


# ── rendering ───────────────────────────────────────────────────────────────


def test_the_write_up_follows_the_template_and_cites_every_point():
    m = _gemini_meeting()
    verified = verify(
        [
            {
                "key": "s1",
                "items": [
                    _item(
                        "Pat said budget approval is pending.",
                        2,
                        "still waiting on budget approval",
                    )
                ],
            },
            {
                "key": "s5",
                "items": [
                    _item(
                        "Send the revised SOW.",
                        3,
                        "I will send the revised SOW by Friday",
                        owner="Blair Ellison",
                        due="Friday",
                    ),
                    _item(
                        "Confirm pricing.",
                        4,
                        "priced per seat, around 45 dollars each",
                        owner="Bob Smith",
                    ),
                ],
            },
            {
                "key": "s2",
                "items": [_item("Invented.", 2, "nothing like this was said")],
            },
        ],
        [m],
        [s.key for s in DEFAULT_SECTIONS],
    )
    out = render(verified, DEFAULT_SECTIONS, [m])
    md = out.markdown

    headings = [ln for ln in md.splitlines() if ln.startswith("## ")]
    assert headings == [f"## {s.title}" for s in DEFAULT_SECTIONS]
    assert "Pat said budget approval is pending. [1]" in md
    assert "**@Blair Ellison**\n- [ ] Send the revised SOW. — due Friday [2]" in md
    # An owner nobody heard of is not printed as one.
    assert "Bob Smith" not in md
    assert "**Owner not clear**\n- [ ] Confirm pricing. [3]" in md
    assert "_No decisions recorded._" in md
    assert "1 point the transcript did not support was left out." in md
    assert "approximate" in md
    assert [c["chunk_index"] for c in out.citations] == [1, 2, 3]
    assert out.citations[1]["snippet"] == (
        "[2] Blair Ellison · ~00:00:00 — “I will send the revised SOW by Friday”"
    )
    # One Sources card per meeting.
    assert {c["source_path"] for c in out.citations} == {
        "Meeting transcripts/Acme weekly"
    }

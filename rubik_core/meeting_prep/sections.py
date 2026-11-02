"""The sections of a Meeting Prep brief that a person can edit or have rewritten.

One registry, read by three places that used to keep their own copies of the
same list: the version endpoints (which keys may be edited and what shape a
value must have), the one-section rewrite prompt (what the section is FOR, in
each persona), and the frontend labels (mirrored in ``MeetingPrep.tsx``).

What is deliberately NOT here:

* ``attendee_intel`` — cited research copied verbatim from the research step;
  the model never rewrites it (UC-47), so neither does a person through Cubie.
* ``source_evidence`` — what Cubie actually cited. A section rewrite appends
  its own sources; nobody edits the list by hand.
* ``recommended_products`` / ``partial_errors`` / ``title`` — derived from the
  catalog, the fetchers and the calendar, not written by the model.

Persona: a brief is either the sales "battle brief" (a Sales reader meeting
people outside the company) or the executive-assistant brief (everything else). The
labels follow ``is_internal`` the way the page always has; the section
descriptions follow the persona, because that is what the original prompt
asked each field to contain.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

SectionKind = Literal["prose", "list", "stakeholders"]

# Hard caps on what a person can save into one section. Generous for a human
# (a long section is ~3k characters) but bounded, so a pasted document cannot
# bloat every later version of the brief.
MAX_PROSE_CHARS = 12_000
MAX_LIST_ITEMS = 40
MAX_ITEM_CHARS = 2_000
MAX_STAKEHOLDERS = 40
MAX_STAKEHOLDER_FIELD_CHARS = 1_000
# A rewrite instruction ("shorter", "focus on pricing") is a sentence or two.
MAX_INSTRUCTION_CHARS = 1_000

_STAKEHOLDER_FIELDS = ("name", "email", "role", "context")


@dataclass(frozen=True)
class SectionSpec:
    key: str
    kind: SectionKind
    external_label: str
    internal_label: str
    # What the section must contain, per persona — the same wording the
    # full-brief prompt uses, so a one-section rewrite aims at the same target.
    battle_brief: str
    ea_brief: str
    # Whether the section's content is factual (must come from the sources) or
    # editorial (Cubie's judgment). Rewrites of factual sections get the
    # stricter grounding reminder.
    factual: bool

    def label(self, *, is_internal: bool) -> str:
        return self.internal_label if is_internal else self.external_label

    def brief(self, *, battle: bool) -> str:
        return self.battle_brief if battle else self.ea_brief


SECTIONS: tuple[SectionSpec, ...] = (
    SectionSpec(
        key="executive_summary",
        kind="prose",
        external_label="Executive Summary",
        internal_label="Executive Summary",
        battle_brief=(
            "A grounded summary of what the provided data actually establishes "
            "about this meeting and relationship. State only facts present in "
            "the sources; do not infer deal history or outcomes. If little is "
            "known, say so plainly."
        ),
        ea_brief=(
            "A grounded summary of this meeting's purpose, based on the calendar "
            "entry and the contextual data provided. State only what the sources "
            "support; do not infer history, outcomes, or stakes that are not "
            "evidenced. If little is known, say so plainly."
        ),
        factual=True,
    ),
    SectionSpec(
        key="meeting_objective",
        kind="prose",
        external_label="Meeting Objective",
        internal_label="Meeting Objective",
        battle_brief="The single most important outcome we want from this meeting.",
        ea_brief="The intended goal for this session.",
        factual=False,
    ),
    SectionSpec(
        key="client_profile",
        kind="prose",
        external_label="Client Profile",
        internal_label="Relationship Context",
        battle_brief=(
            "Analysis of the company's current state, scale, and market position."
        ),
        ea_brief="Summary of the reader's history with these individuals.",
        factual=True,
    ),
    SectionSpec(
        key="stakeholder_map",
        kind="stakeholders",
        external_label="Stakeholder Map",
        internal_label="Roles & Dynamics",
        battle_brief=(
            "One entry per person on the invite: name, email, role (their "
            "title/function) and context (1-2 sentences on their agenda and "
            "influence for THIS meeting). Only people on the invite."
        ),
        ea_brief=(
            "One entry per person on the invite: name, email, role (their "
            "title/function) and context (1-2 sentences on what they care about "
            "and their dynamic in this meeting). Only people on the invite."
        ),
        factual=False,
    ),
    SectionSpec(
        key="recent_interactions",
        kind="prose",
        external_label="Recent Interactions",
        internal_label="Recent Interactions",
        battle_brief=("Summary of the most important recent emails or touchpoints."),
        ea_brief="Summary of the last few emails or messages.",
        factual=True,
    ),
    SectionSpec(
        key="past_meeting_context",
        kind="prose",
        external_label="Past Meeting Context",
        internal_label="Past Meeting Context",
        battle_brief="Insights from previous transcripts or documents.",
        ea_brief="What was discussed or decided the last time these people met.",
        factual=True,
    ),
    SectionSpec(
        key="product_fit_analysis",
        kind="prose",
        external_label="Product Fit Analysis",
        internal_label="Project/Status Context",
        battle_brief="How our solutions map to their specific pain points.",
        ea_brief="Ongoing projects or blockers.",
        factual=False,
    ),
    SectionSpec(
        key="recommended_positioning",
        kind="prose",
        external_label="Recommended Positioning",
        internal_label="Communication Style",
        battle_brief="How to frame the conversation (the point of view).",
        ea_brief="Advice on how to approach this specific person or group.",
        factual=False,
    ),
    SectionSpec(
        key="likely_objections",
        kind="prose",
        external_label="Likely Objections",
        internal_label="Potential Friction",
        battle_brief=("3-5 specific pushbacks they might give and how to handle each."),
        ea_brief="Anticipated pushback or difficult topics.",
        factual=False,
    ),
    SectionSpec(
        key="suggested_agenda",
        kind="list",
        external_label="Suggested Agenda",
        internal_label="Suggested Agenda",
        battle_brief="A proposed 30-60 minute flow, one step per item.",
        ea_brief="Structured flow for the meeting, one step per item.",
        factual=False,
    ),
    SectionSpec(
        key="talking_points",
        kind="list",
        external_label="Talking Points",
        internal_label="Talking Points",
        battle_brief=(
            "Specific talk tracks for the opening, the problem statement and "
            'the "why us" part.'
        ),
        ea_brief="Specific items to bring up or status updates to give.",
        factual=False,
    ),
    SectionSpec(
        key="open_items_you_owe",
        kind="list",
        external_label="What You Owe Them",
        internal_label="What You Owe Them",
        battle_brief=(
            "Things the reader still owes an attendee: unanswered email threads "
            "and open commitments in the sources. Name the thread or commitment "
            "and what is being waited on. Never invent an obligation."
        ),
        ea_brief=(
            "Things the reader still owes an attendee: unanswered email threads "
            "and open commitments in the sources. Name the thread or commitment "
            "and what is being waited on. Never invent an obligation."
        ),
        factual=True,
    ),
    SectionSpec(
        key="discovery_questions",
        kind="list",
        external_label="Discovery Questions",
        internal_label="Key Questions",
        battle_brief="5-7 deep, non-obvious questions.",
        ea_brief="Items the reader needs clarity on.",
        factual=False,
    ),
    SectionSpec(
        key="next_steps",
        kind="list",
        external_label="Next Steps",
        internal_label="Next Steps",
        battle_brief="3-5 concrete action items to propose at the end.",
        ea_brief="3-5 concrete action items to leave with.",
        factual=False,
    ),
)

_BY_KEY: dict[str, SectionSpec] = {s.key: s for s in SECTIONS}
SECTION_KEYS: frozenset[str] = frozenset(_BY_KEY)


def get_section(key: str) -> SectionSpec | None:
    return _BY_KEY.get(key)


def is_battle_brief(*, is_sales_user: bool, is_internal: bool) -> bool:
    """The sales battle brief is a Sales reader meeting people outside the company."""
    return bool(is_sales_user) and not bool(is_internal)


# ─── Value shapes ──────────────────────────────────────────────────────────


def stakeholders_from_value(raw: Any) -> list[dict[str, str]]:
    """Parse the stored ``stakeholder_map`` (a JSON-encoded array string, as
    the page has always received it) or an already-decoded list into clean
    ``{name, email, role, context}`` rows. Unparseable input → ``[]``."""
    data = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw) if raw.strip() else []
        except ValueError:
            return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        out.append({f: str(item.get(f) or "").strip() for f in _STAKEHOLDER_FIELDS})
    return out


def normalize_section_value(
    key: str, value: Any, *, is_internal: bool = False
) -> str | list[str]:
    """Validate a person's (or the rewrite model's) value for one section and
    return it in the shape ``MeetingPrepResponse`` stores.

    * prose → ``str`` (stripped, capped)
    * list → ``list[str]`` (blank items dropped; a single string is split on
      newlines so a textarea round-trips)
    * stakeholders → a JSON-encoded array string of ``{name,email,role,context}``

    Raises ``ValueError`` with a reader-safe message on an unknown key or a
    value of the wrong shape / size. ``is_internal`` is the brief's own flag:
    the message names the section the way the reader's page does ("Key
    Questions" on an internal brief, "Discovery Questions" on a client one).
    """
    spec = get_section(key)
    if spec is None:
        raise ValueError(f"'{key}' is not an editable section.")
    label = spec.label(is_internal=is_internal)

    if spec.kind == "prose":
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise ValueError(f"{label} must be text.")
        text = value.strip()
        if len(text) > MAX_PROSE_CHARS:
            raise ValueError(
                f"{label} is too long "
                f"({len(text)} of at most {MAX_PROSE_CHARS} characters)."
            )
        return text

    if spec.kind == "list":
        if value is None:
            value = []
        if isinstance(value, str):
            value = value.splitlines()
        if not isinstance(value, list):
            raise ValueError(f"{label} must be a list of items.")
        items: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError(f"Every item in {label} must be text.")
            text = item.strip()
            if not text:
                continue
            if len(text) > MAX_ITEM_CHARS:
                raise ValueError(
                    f"An item in {label} is too long "
                    f"({len(text)} of at most {MAX_ITEM_CHARS} characters)."
                )
            items.append(text)
        if len(items) > MAX_LIST_ITEMS:
            raise ValueError(
                f"{label} has too many items "
                f"({len(items)} of at most {MAX_LIST_ITEMS})."
            )
        return items

    # stakeholders
    if value is None:
        value = []
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.strip() else []
        except ValueError as exc:
            raise ValueError("The stakeholder list could not be read.") from exc
    if not isinstance(value, list):
        raise ValueError("The stakeholder list must be a list of people.")
    rows: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Each stakeholder must have a name, role and context.")
        row: dict[str, str] = {}
        for f in _STAKEHOLDER_FIELDS:
            v = item.get(f)
            if v is not None and not isinstance(v, str):
                raise ValueError(f"A stakeholder's {f} must be text.")
            text = (v or "").strip()
            if len(text) > MAX_STAKEHOLDER_FIELD_CHARS:
                raise ValueError(
                    f"A stakeholder's {f} is too long "
                    f"({len(text)} of at most {MAX_STAKEHOLDER_FIELD_CHARS} characters)."
                )
            row[f] = text
        if not any(row.values()):
            continue
        rows.append(row)
    if len(rows) > MAX_STAKEHOLDERS:
        raise ValueError(
            f"Too many stakeholders ({len(rows)} of at most {MAX_STAKEHOLDERS})."
        )
    return json.dumps(rows)


def normalize_instruction(instruction: Any) -> str:
    """A rewrite instruction: optional, plain text, one or two sentences."""
    if instruction is None:
        return ""
    if not isinstance(instruction, str):
        raise ValueError("The instruction must be text.")
    text = instruction.strip()
    if len(text) > MAX_INSTRUCTION_CHARS:
        raise ValueError(
            f"The instruction is too long "
            f"({len(text)} of at most {MAX_INSTRUCTION_CHARS} characters)."
        )
    return text

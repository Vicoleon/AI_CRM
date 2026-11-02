"""The Work Style Interview's structural contract — sections, resume, answers.

``st_interviews`` stores three things and nothing else: a ``section_cursor``, a
``sections_completed`` list, and one flat ``answers`` blob keyed by item id. That
is a deliberately thin schema, and it only works if exactly one module decides
what those three fields *mean*. This is that module. ST-3 (the interview page and
router), ST-4 (the scoring engine) and ST-8 (the skills section) all read the
catalog below rather than each deciding for themselves which items belong to
which step.

## The section catalog

:data:`SECTIONS` is an ordered, frozen tuple. Every section names the item ids it
owns, and those ids are **pulled from the instrument modules** — not re-typed —
so a change to an item bank is a change to the catalog automatically. Nothing in
this file contains a literal item id.

Two fields describe a section, and they answer different questions:

* ``kind`` — how the section is *presented*: a quick-fire ``scale_bank``, a
  ``conversational`` segment, or the ``skills_declaration``. This is what the
  front end switches on.
* ``answer_kind`` — what a stored value *is*: a Likert integer, an ``"a"``/
  ``"b"`` choice, a transcript, a skill level, or a small structured object.
  This is what the validator switches on.

Keeping them separate is the fix for a real trap. The DISC forced-choice bank is
a scale bank by presentation but its values are strings, not integers, and the
two DISC banks must never be merged: ``disc.score_disc`` takes **two** maps, and
feeding a ``disc_fc_*`` id into the Likert validator raises. Because each bank is
its own section owning its own exact id set, routing is set membership rather
than a ``startswith("disc_")`` prefix test — the trap is structurally absent
rather than commented about.

## Resume — the part naive implementations get wrong

``sections_completed`` is the **only** authority on what is done. It is appended
to by an explicit submit, never inferred from the presence of answers, because a
half-filled grid that auto-advances is how somebody loses a section they thought
they were still editing.

``section_cursor`` is a **hint**: the section the person was last looking at. It
is never authoritative for progress. A partially-answered section — the case that
breaks naive resume — is exactly the state where ``section_cursor`` names a
section that is *not* in ``sections_completed`` and whose answers are already
sitting in ``answers``. :func:`resume_target` returns that section, and
:func:`first_unanswered_item` says where inside it to land.

The one guard that matters: if ``section_cursor`` names a section that IS in
``sections_completed`` (a double-submit, or a cursor written before the completion
was appended), the cursor is ignored and the person resumes at the first
incomplete section instead. Trusting the cursor there re-opens a finished section
forever, which reads to the user as an interview that will not end.

:func:`next_section` is total and terminating by construction: it scans a finite
tuple and returns the first key absent from a set. Feeding its result back as
"now completed" walks the catalog in exactly ``len(SECTIONS)`` steps and then
returns ``None``.

## The answers envelope

One flat JSON object. Four key namespaces, each with one permitted value type:

===========================  ==================================================
key shape                    value
===========================  ==================================================
``<item_id>``                ``int`` within that instrument's own scale bounds,
                             or ``"a"``/``"b"`` for a forced-choice pair, or
                             ``None``
``transcript:<section_key>`` ``list`` of turn objects (see
                             ``contracts.turn_from_dict``), or ``None``
``skill:<skill_key>``        one of ``database.smart_teams.SKILL_LEVELS``, or
                             ``None``
``meta:<section_key>``       a small object whose keys this module declares
===========================  ==================================================

``None`` means "presented and left blank" — every instrument module tolerates and
drops ``None``, so it is a real answer state, not a corruption. Anything else is
rejected loudly by :func:`validate_answers`: an unknown key means an item bank
changed without the stored answers being migrated, and a value outside the scale
means a client sent something the scorer would raise on later, in a background
job, where nobody is watching.

## Duration

Honest arithmetic, not the plan's target. plan.md Decision 6 says "~25 min"; the
authored banks do not fit in 25 minutes and :func:`duration_report` says so in
numbers. See :data:`TARGET_TOTAL_SECONDS` and the note attached to it — this is
called out rather than smoothed over because a 25-minute promise on a
33-minute interview is an abandonment problem, and the levers for closing the gap
are product decisions, not contract decisions.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, get_args

from rubik_core.brand import PRODUCT_NAME
from rubik_core.database.smart_teams import SKILL_LEVELS
from rubik_core.smart_teams.contracts import (
    FRAMEWORKS,
    PayloadError,
    transcript_from_json,
    transcript_to_json,
)
from rubik_core.smart_teams.instruments import (
    big_five,
    disc,
    enneagram,
    motivators,
    strengths,
)

# ── Vocabularies ──────────────────────────────────────────────────────────────
#: How a section is presented. plan.md Decision 6: "quick-fire scale banks +
#: conversational AI sections", plus the declared-skills step ST-8 needs.
SectionKind = Literal["scale_bank", "conversational", "skills_declaration"]

#: What a stored answer value is. Separate from ``SectionKind`` on purpose — see
#: the module docstring.
AnswerKind = Literal["likert", "choice_ab", "transcript", "skill_level", "structured"]

#: Answer-key namespaces. A key with no prefix is an instrument item id.
PREFIX_TRANSCRIPT = "transcript:"
PREFIX_SKILL = "skill:"
PREFIX_META = "meta:"
RESERVED_PREFIXES: tuple[str, ...] = (PREFIX_TRANSCRIPT, PREFIX_SKILL, PREFIX_META)

#: Skill keys are curated slugs (``st_skills_taxonomy.skill_key``). Validated by
#: shape here and, when the caller supplies the taxonomy, by membership too.
_SKILL_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_]*$")

#: The two legal forced-choice values, taken from ``disc.Choice`` rather than
#: written out, so a change to the literal reaches this validator.
CHOICE_VALUES: tuple[str, ...] = get_args(disc.Choice)


class AnswerError(ValueError):
    """An answers blob does not match the envelope contract in this module."""


class UnknownAnswerKey(AnswerError):
    """A key in ``st_interviews.answers`` belongs to no section.

    Raised rather than dropped. A key nobody claims means one of two things —
    an item bank changed without migrating the stored answers, or a client is
    inventing ids — and both are worth failing on. Dropping it would score a
    silently shorter interview.
    """


class AnswerValueError(AnswerError):
    """A value is not the type or range its section permits."""


# ── Duration model ────────────────────────────────────────────────────────────
# Per-item rates, applied uniformly so the total is reproducible and testable.
# These are estimates of answering time only: no intro screens, no reading of
# the advisory notice, no pauses. Sources of the numbers: the published
# administration time for the IPIP-50 is 5-10 minutes, i.e. 6-12s per item, and
# we sit near the fast end of that because the banks render as grids with
# several items visible at once. DISC's own ``MIN_SECONDS_PER_ITEM = 2.0`` is
# the *implausibly fast* floor for a quality flag, not a target.
SECONDS_PER_LIKERT_ITEM = 7
#: Forced-choice pairs cost more: two options to read instead of one statement.
SECONDS_PER_FORCED_CHOICE_ITEM = 9
#: One scripted question, a follow-up probe or two, and a typed answer.
SECONDS_PER_CONVERSATIONAL_PROMPT = 75
#: Picking levels for a handful of skills off a curated list.
SKILLS_DECLARATION_SECONDS = 180

#: plan.md Decision 6's target, in seconds. NOT what the catalog costs — see
#: :func:`duration_report`, which reports the real figure. Kept here so the
#: gap is a number anyone can print rather than a thing to rediscover.
TARGET_TOTAL_SECONDS = 25 * 60


# ── The section record ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Section:
    """One step of the interview.

    Frozen and hashable: sections are used as dict keys in the progress
    computation and handed straight to the router's response serializer.
    """

    key: str
    title: str
    kind: SectionKind
    answer_kind: AnswerKind
    #: The framework payloads this section's answers feed, per
    #: ``contracts.FRAMEWORKS``. The Big Five bank feeds two — ``type16`` is a
    #: pure restatement of the same answers and has no items of its own — and
    #: the skills section feeds none, because declared skills live in
    #: ``st_person_skills``, not in a profile payload.
    feeds: tuple[str, ...]
    #: The exact item ids this section owns. Empty for sections whose answers
    #: are not per-item. Membership here is the routing rule.
    item_ids: tuple[str, ...] = ()
    #: Scripted prompt ids, for conversational sections that have them.
    prompt_ids: tuple[str, ...] = ()
    #: Inclusive scale bounds for ``likert`` sections, from the instrument's own
    #: constants. ``None`` for every other answer kind.
    value_min: int | None = None
    value_max: int | None = None
    estimated_seconds: int = 0
    #: A section the runtime may legitimately skip. The motivators probe only
    #: fires when ``MotivatorResult.probe_reason`` is set; skipping it still
    #: appends the key to ``sections_completed`` so that :func:`next_section`
    #: stays a pure function of the catalog and the completed set.
    optional: bool = False
    #: One line for the UI, explaining why we ask. Advisory framing only.
    blurb: str = ""

    def __post_init__(self) -> None:
        unknown = [name for name in self.feeds if name not in FRAMEWORKS]
        if unknown:
            raise ValueError(f"{self.key}: unknown frameworks {unknown!r} in feeds")
        if self.answer_kind == "likert" and (
            self.value_min is None or self.value_max is None
        ):
            raise ValueError(f"{self.key}: a likert section needs scale bounds")

    @property
    def step_count(self) -> int:
        """How many discrete things the person does here, for the progress bar."""
        if self.item_ids:
            return len(self.item_ids)
        if self.prompt_ids:
            return len(self.prompt_ids)
        return 1

    @property
    def transcript_key(self) -> str:
        """The ``answers`` key holding this section's transcript."""
        return f"{PREFIX_TRANSCRIPT}{self.key}"

    @property
    def meta_key(self) -> str:
        """The ``answers`` key holding this section's structured answer."""
        return f"{PREFIX_META}{self.key}"

    def owns_key(self, key: str) -> bool:
        """Whether ``key`` in an answers blob belongs to this section."""
        if self.answer_kind == "skill_level":
            return key.startswith(PREFIX_SKILL)
        if self.answer_kind == "transcript":
            return key == self.transcript_key
        if self.answer_kind == "structured":
            return key == self.meta_key
        return key in self._item_id_set

    @property
    def _item_id_set(self) -> frozenset[str]:
        return frozenset(self.item_ids)


# ── Section keys ──────────────────────────────────────────────────────────────
# Named constants because these strings are written into ``section_cursor`` and
# ``sections_completed`` in the database and read by three other modules. A typo
# in a literal is a section that can never be completed.
SECTION_DISC_LIKERT = "disc_likert"
SECTION_DISC_FORCED_CHOICE = "disc_forced_choice"
SECTION_BIG_FIVE = "big_five"
SECTION_STRENGTHS = "strengths"
SECTION_MOTIVATORS = "motivators"
SECTION_MOTIVATORS_PROBE = "motivators_probe"
SECTION_ENNEAGRAM = "enneagram"
SECTION_ENNEAGRAM_CONFIRMATION = "enneagram_confirmation"
SECTION_SKILLS = "skills"

#: Keys permitted inside ``meta:enneagram_confirmation``. The confirmation step
#: can end three ways — confirm/correct the inferred type, pick from the nine, or
#: decline the layer entirely — and ``EnneagramRead`` has a constructor for each
#: (``self_identified``, ``with_decline``). This object is what tells the scorer
#: which one to call.
ENNEAGRAM_CONFIRMATION_KEYS: tuple[str, ...] = ("type_number", "wing", "declined")


def _prompt_ids(script: Iterable[Any]) -> tuple[str, ...]:
    return tuple(prompt.prompt_id for prompt in script)


#: The interview, in the order it is presented.
#:
#: Ordering rationale — the two DISC banks open because agreement statements are
#: the easiest thing to start on and the forced-choice pairs keep attention up
#: while it is still cheap. The 50-item Big Five bank goes third, while the
#: person is still fresh, because it is the scoring backbone: every other
#: deterministic layer degrades gracefully but ``type16`` is nothing without it.
#: The strengths conversation follows as a deliberate change of gear before the
#: two short banks and their conversational tails. Skills last, because it is the
#: only section a person can usefully come back and edit later.
#:
#: This order is a UX decision and is independent of ``contracts.SCORING_ORDER``,
#: which exists because ``type16`` must be derived after ``big_five`` is scored.
SECTIONS: tuple[Section, ...] = (
    Section(
        key=SECTION_DISC_LIKERT,
        title="How you work",
        kind="scale_bank",
        answer_kind="likert",
        feeds=("disc",),
        item_ids=tuple(item.item_id for item in disc.LIKERT_ITEMS),
        value_min=disc.LIKERT_MIN,
        value_max=disc.LIKERT_MAX,
        estimated_seconds=len(disc.LIKERT_ITEMS) * SECONDS_PER_LIKERT_ITEM,
        blurb="How much each statement sounds like you at work.",
    ),
    Section(
        key=SECTION_DISC_FORCED_CHOICE,
        title="This or that",
        kind="scale_bank",
        answer_kind="choice_ab",
        feeds=("disc",),
        item_ids=tuple(pair.item_id for pair in disc.FORCED_CHOICE_PAIRS),
        estimated_seconds=(
            len(disc.FORCED_CHOICE_PAIRS) * SECONDS_PER_FORCED_CHOICE_ITEM
        ),
        blurb="Two things that are both fine. Pick the one more like you.",
    ),
    Section(
        key=SECTION_BIG_FIVE,
        title="Working preferences",
        kind="scale_bank",
        answer_kind="likert",
        # One bank, two payloads: type16 restates these same answers as letters.
        feeds=("big_five", "type16"),
        item_ids=tuple(item.item_id for item in big_five.ITEMS),
        value_min=big_five.RESPONSE_MIN,
        value_max=big_five.RESPONSE_MAX,
        estimated_seconds=len(big_five.ITEMS) * SECONDS_PER_LIKERT_ITEM,
        blurb="The longest stretch, and the one the rest of the profile leans on.",
    ),
    Section(
        key=SECTION_STRENGTHS,
        title="What you are good at",
        kind="conversational",
        answer_kind="transcript",
        feeds=("strengths",),
        prompt_ids=_prompt_ids(strengths.ELICITATION_SCRIPT),
        estimated_seconds=(strengths.PROMPT_COUNT * SECONDS_PER_CONVERSATIONAL_PROMPT),
        blurb="A conversation, in your own words. Specific stories beat summaries.",
    ),
    Section(
        key=SECTION_MOTIVATORS,
        title="What drives you",
        kind="scale_bank",
        answer_kind="likert",
        feeds=("motivators",),
        item_ids=tuple(item.item_id for item in motivators.LIKERT_ITEMS),
        value_min=motivators.LIKERT_MIN,
        value_max=motivators.LIKERT_MAX,
        estimated_seconds=len(motivators.LIKERT_ITEMS) * SECONDS_PER_LIKERT_ITEM,
        blurb="Six things people work for. These weigh them against each other.",
    ),
    Section(
        key=SECTION_MOTIVATORS_PROBE,
        title="A couple of follow-ups",
        kind="conversational",
        answer_kind="transcript",
        feeds=("motivators",),
        # No static prompt ids: which probe form fires depends on the scored
        # result (motivators.probe_form_name branches on probe_reason), so the
        # questions are not knowable before the motivators bank is scored. The
        # estimate below uses the longest form, read from the module, so a new
        # form cannot lengthen the interview without changing this number.
        prompt_ids=(),
        estimated_seconds=(
            max(len(form) for form in motivators.PROBE_FORMS.values())
            * SECONDS_PER_CONVERSATIONAL_PROMPT
        ),
        optional=True,
        blurb="Only asked when the scales did not settle what drives you most.",
    ),
    Section(
        key=SECTION_ENNEAGRAM,
        title="What you steer away from",
        kind="scale_bank",
        answer_kind="likert",
        feeds=("enneagram",),
        item_ids=tuple(item.item_id for item in enneagram.LIKERT_ITEMS),
        value_min=enneagram.LIKERT_MIN,
        value_max=enneagram.LIKERT_MAX,
        estimated_seconds=len(enneagram.LIKERT_ITEMS) * SECONDS_PER_LIKERT_ITEM,
        blurb="The softest-evidence part of the profile, and skippable.",
    ),
    Section(
        key=SECTION_ENNEAGRAM_CONFIRMATION,
        title="Does this sound like you?",
        kind="conversational",
        answer_kind="structured",
        feeds=("enneagram",),
        # Structurally one step: enneagram.confirmation_step() returns a single
        # ConfirmationStep with its candidates and options already assembled.
        estimated_seconds=SECONDS_PER_CONVERSATIONAL_PROMPT,
        blurb=f"Whatever you say here overrides what {PRODUCT_NAME} guessed.",
    ),
    Section(
        key=SECTION_SKILLS,
        title="What you can do",
        kind="skills_declaration",
        answer_kind="skill_level",
        # Declared skills are rows in st_person_skills, not a profile payload.
        feeds=(),
        estimated_seconds=SKILLS_DECLARATION_SECONDS,
        blurb="Skills you would be happy to be asked about on a project.",
    ),
)

SECTION_KEYS: tuple[str, ...] = tuple(section.key for section in SECTIONS)
SECTION_BY_KEY: Mapping[str, Section] = {section.key: section for section in SECTIONS}

# ── Import-time catalog guards ────────────────────────────────────────────────
# These are assertions rather than tests because the failure they catch makes
# every downstream read wrong, and a module that cannot be trusted should refuse
# to import rather than serve a half-catalog. The same properties are covered by
# backend/tests/test_smart_teams_interview_contract.py, which explains WHY each
# one matters.

if len(SECTION_BY_KEY) != len(SECTIONS):
    raise RuntimeError("SECTIONS has duplicate keys; section keys are stored ids")

_ALL_ITEM_IDS: list[str] = [
    item_id for section in SECTIONS for item_id in section.item_ids
]
if len(set(_ALL_ITEM_IDS)) != len(_ALL_ITEM_IDS):
    raise RuntimeError(
        "an item id appears in two sections; routing is set membership, so a "
        "shared id would be scored twice or claimed by the wrong instrument"
    )

_COVERED_FRAMEWORKS = {name for section in SECTIONS for name in section.feeds}
if _COVERED_FRAMEWORKS != set(FRAMEWORKS):
    raise RuntimeError(
        "the section catalog does not cover every framework: missing "
        f"{sorted(set(FRAMEWORKS) - _COVERED_FRAMEWORKS)}"
    )

ITEM_ID_TO_SECTION: Mapping[str, str] = {
    item_id: section.key for section in SECTIONS for item_id in section.item_ids
}


# ── Duration ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DurationReport:
    """The interview's length, computed rather than asserted.

    ``required_seconds`` excludes optional sections; ``total_seconds`` includes
    them. ``over_target_seconds`` is the honest gap against plan.md's ~25 minute
    target and is positive today — see :func:`duration_report`.
    """

    required_seconds: int
    total_seconds: int
    target_seconds: int

    @property
    def required_minutes(self) -> float:
        return round(self.required_seconds / 60, 1)

    @property
    def total_minutes(self) -> float:
        return round(self.total_seconds / 60, 1)

    @property
    def over_target_seconds(self) -> int:
        """How far the required path exceeds the target. Negative means under."""
        return self.required_seconds - self.target_seconds

    @property
    def meets_target(self) -> bool:
        return self.over_target_seconds <= 0

    def per_section(self) -> dict[str, int]:
        return {section.key: section.estimated_seconds for section in SECTIONS}


def duration_report() -> DurationReport:
    """Compute the interview's estimated length from the catalog.

    The number this returns still does **not** meet plan.md Decision 6's ~25
    minute target, and that is reported rather than hidden. The banks as authored
    are 24 + 24 DISC items, 50 Big Five items, 30 motivators items and 27
    Enneagram items — 155 responses — plus a six-prompt conversational load. At
    the rates above that is roughly 29 minutes on the required path and roughly
    34 when the motivators probe fires.

    One lever has been pulled. The strengths script was cut from eight prompts to
    five, which removed 225 seconds and took the required path from
    33.1 to 29.4 minutes. ``MIN_SUBSTANTIVE_PROMPTS`` is 4, so five still clears
    the thinness bar — though 4 of 5 is a tighter bar in practice than 4 of 8
    was, and ``docs/smart_teams/instruments/strengths.md`` records what the cut
    cost.

    The remaining gap is 263 seconds, and the levers are still product decisions
    that belong to a human rather than to this module. Making the Enneagram layer
    opt-in would remove its bank and its confirmation step — 264 seconds, one
    second more than the gap — or the interview can be split across two sittings,
    leaning on the resume machinery that already exists. Silently trimming an
    item bank here would change what every instrument measures.
    """
    required = sum(
        section.estimated_seconds for section in SECTIONS if not section.optional
    )
    total = sum(section.estimated_seconds for section in SECTIONS)
    return DurationReport(
        required_seconds=required,
        total_seconds=total,
        target_seconds=TARGET_TOTAL_SECONDS,
    )


# ── Cursor and resume ─────────────────────────────────────────────────────────


def normalize_completed(sections_completed: object) -> tuple[str, ...]:
    """Clean ``st_interviews.sections_completed`` into catalog order.

    Deduplicates (a retried submit appends twice) and reorders to the catalog,
    so that two rows describing the same progress compare equal. Raises on a key
    the catalog does not know: that means the catalog changed under a live
    interview, and the person needs to be told to start the affected section
    again rather than have it silently dropped from their profile.
    """
    if sections_completed is None:
        return ()
    if isinstance(sections_completed, str) or not isinstance(
        sections_completed, Sequence
    ):
        raise AnswerError(
            "sections_completed must be a list of section keys, got "
            f"{type(sections_completed)}"
        )
    seen = set()
    for key in sections_completed:
        if key not in SECTION_BY_KEY:
            raise AnswerError(
                f"sections_completed names unknown section {key!r}; the catalog "
                f"has {SECTION_KEYS}"
            )
        seen.add(key)
    return tuple(key for key in SECTION_KEYS if key in seen)


def next_section(sections_completed: object) -> Section | None:
    """The first section not yet completed, or ``None`` when the interview is done.

    Total and terminating: a scan of a finite tuple against a set. Optional
    sections are returned like any other — whether to render one or auto-complete
    it is a runtime decision (the motivators probe depends on a scored result),
    and keeping that decision out of here is what makes this function pure.
    """
    done = set(normalize_completed(sections_completed))
    for section in SECTIONS:
        if section.key not in done:
            return section
    return None


def resume_target(
    section_cursor: str | None, sections_completed: object
) -> Section | None:
    """Where to put the person when they come back.

    The cursor wins when it is honest — set, known, and not already completed —
    because that is the partially-answered section they were in the middle of.
    Otherwise the first incomplete section wins.

    The completed-cursor case is the one that matters: a cursor pointing at a
    finished section (a double submit, or a save that landed after the completion
    append) would otherwise re-open it every single time, which reads as an
    interview that never ends. It is ignored, silently, because it is a benign
    race rather than a corruption.
    """
    done = set(normalize_completed(sections_completed))
    if section_cursor:
        section = SECTION_BY_KEY.get(section_cursor)
        if section is None:
            raise AnswerError(
                f"section_cursor {section_cursor!r} is not a known section; the "
                f"catalog has {SECTION_KEYS}"
            )
        if section.key not in done:
            return section
    return next_section(sections_completed)


def is_complete(sections_completed: object) -> bool:
    """Whether every section in the catalog has been completed."""
    return next_section(sections_completed) is None


def remaining_sections(sections_completed: object) -> tuple[Section, ...]:
    """Sections still to do, in presentation order."""
    done = set(normalize_completed(sections_completed))
    return tuple(section for section in SECTIONS if section.key not in done)


def first_unanswered_item(section: Section, answers: Mapping[str, Any]) -> str | None:
    """The item id to scroll to inside a partially-answered scale bank.

    ``None`` either because the section has no items or because every one of
    them has a value. A key present with value ``None`` counts as unanswered:
    the person saw it and skipped it, and resume should land there.
    """
    for item_id in section.item_ids:
        if answers.get(item_id) is None:
            return item_id
    return None


# ── Answer validation ─────────────────────────────────────────────────────────


def answer_key_section(key: str) -> Section:
    """Which section owns an ``answers`` key. Raises when nothing does."""
    if key.startswith(PREFIX_SKILL):
        for section in SECTIONS:
            if section.answer_kind == "skill_level":
                return section
    elif key.startswith(PREFIX_TRANSCRIPT):
        target = key[len(PREFIX_TRANSCRIPT) :]
        section = SECTION_BY_KEY.get(target)
        if section is not None and section.answer_kind == "transcript":
            return section
    elif key.startswith(PREFIX_META):
        target = key[len(PREFIX_META) :]
        section = SECTION_BY_KEY.get(target)
        if section is not None and section.answer_kind == "structured":
            return section
    else:
        section_key = ITEM_ID_TO_SECTION.get(key)
        if section_key is not None:
            return SECTION_BY_KEY[section_key]
    raise UnknownAnswerKey(
        f"answers key {key!r} belongs to no interview section; an unrouted key "
        "means an item bank changed without the stored answers being migrated"
    )


def _validate_likert(section: Section, key: str, value: Any) -> int | None:
    if value is None:
        return None
    # bool is a subclass of int, and True would sail through a range check and
    # score as a 1. Reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, int):
        raise AnswerValueError(
            f"{key}: {value!r} is not an integer on the "
            f"{section.value_min}-{section.value_max} scale"
        )
    assert section.value_min is not None and section.value_max is not None
    if not section.value_min <= value <= section.value_max:
        raise AnswerValueError(
            f"{key}: {value!r} is outside the {section.value_min}-"
            f"{section.value_max} scale for {section.key}"
        )
    return value


def _validate_choice(key: str, value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AnswerValueError(f"{key}: forced-choice value must be a string")
    folded = value.strip().lower()
    if folded not in CHOICE_VALUES:
        raise AnswerValueError(f"{key}: {value!r} is not one of {CHOICE_VALUES}")
    return folded


def _validate_skill(
    key: str, value: Any, known_skill_keys: frozenset[str] | None
) -> str | None:
    slug = key[len(PREFIX_SKILL) :]
    if not _SKILL_KEY_PATTERN.match(slug):
        raise AnswerValueError(
            f"{key}: {slug!r} is not a skill slug (lower case, digits, underscores)"
        )
    if known_skill_keys is not None and slug not in known_skill_keys:
        raise AnswerValueError(
            f"{key}: {slug!r} is not in the curated skills taxonomy; declared "
            "skills must resolve to an st_skills_taxonomy row so the Team "
            "Builder can match on them"
        )
    if value is None:
        return None
    if not isinstance(value, str) or value not in SKILL_LEVELS:
        raise AnswerValueError(f"{key}: {value!r} is not one of {SKILL_LEVELS}")
    return value


def _validate_transcript(key: str, value: Any) -> list[dict[str, Any]] | None:
    if value is None:
        return None
    try:
        turns = transcript_from_json(value)
    except PayloadError as exc:
        raise AnswerValueError(f"{key}: {exc}") from None
    return transcript_to_json(turns)


def _validate_structured(section: Section, key: str, value: Any) -> Any:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise AnswerValueError(f"{key}: expected an object, got {type(value)}")
    if section.key != SECTION_ENNEAGRAM_CONFIRMATION:
        raise AnswerValueError(
            f"{key}: no structured-answer shape is declared for {section.key}; "
            "declare one in interview.py in the same change that adds it"
        )
    unknown = [name for name in value if name not in ENNEAGRAM_CONFIRMATION_KEYS]
    if unknown:
        raise AnswerValueError(
            f"{key}: unexpected keys {unknown!r}; permitted keys are "
            f"{ENNEAGRAM_CONFIRMATION_KEYS}"
        )
    declined = value.get("declined", False)
    if not isinstance(declined, bool):
        raise AnswerValueError(f"{key}: 'declined' must be a bool")
    type_number = value.get("type_number")
    wing = value.get("wing")
    for name, number in (("type_number", type_number), ("wing", wing)):
        if number is None:
            continue
        if isinstance(number, bool) or not isinstance(number, int):
            raise AnswerValueError(f"{key}: {name} must be an integer or null")
        if number not in enneagram.TYPE_ORDER:
            raise AnswerValueError(
                f"{key}: {name} {number!r} is not an Enneagram type; expected "
                f"one of {enneagram.TYPE_ORDER}"
            )
    if declined and type_number is not None:
        raise AnswerValueError(
            f"{key}: a declined Enneagram layer cannot also carry a type; "
            "EnneagramRead.with_decline() drops the number for exactly this "
            "reason"
        )
    if wing is not None and type_number is None:
        raise AnswerValueError(f"{key}: a wing without a type number is meaningless")
    if wing is not None and wing not in enneagram.adjacent(type_number):
        raise AnswerValueError(
            f"{key}: wing {wing} is not adjacent to type {type_number} "
            f"({enneagram.adjacent(type_number)})"
        )
    return {
        "type_number": type_number,
        "wing": wing,
        "declined": declined,
    }


def validate_answers(
    answers: object,
    *,
    known_skill_keys: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Validate and normalize a whole ``st_interviews.answers`` blob.

    Returns the normalized mapping — forced choices folded to lower case,
    transcripts round-tripped through the turn contract, structured answers
    filled out to their full key set. Call it on every write, and on read before
    scoring: the write side catches a bad client, and the read side catches a row
    written by a seed script or a psql session.

    Raises :class:`UnknownAnswerKey` for a key no section owns and
    :class:`AnswerValueError` for a value a section does not permit. Both are
    loud on purpose. The alternative — dropping what it does not recognise — is
    how an interview scores as if the person answered less than they did, in a
    background job, with nobody watching.

    ``known_skill_keys`` is the curated ``st_skills_taxonomy`` slug set. Omit it
    and skill keys are checked for shape only; supply it and an unknown skill is
    rejected, which is what the router should do.
    """
    if not isinstance(answers, Mapping):
        raise AnswerError(f"answers must be a mapping, got {type(answers)}")
    known = frozenset(known_skill_keys) if known_skill_keys is not None else None
    normalized: dict[str, Any] = {}
    for key, value in answers.items():
        if not isinstance(key, str):
            raise AnswerError(f"answers keys must be strings, got {key!r}")
        section = answer_key_section(key)
        if section.answer_kind == "likert":
            normalized[key] = _validate_likert(section, key, value)
        elif section.answer_kind == "choice_ab":
            normalized[key] = _validate_choice(key, value)
        elif section.answer_kind == "skill_level":
            normalized[key] = _validate_skill(key, value, known)
        elif section.answer_kind == "transcript":
            normalized[key] = _validate_transcript(key, value)
        else:
            normalized[key] = _validate_structured(section, key, value)
    return normalized


def partition_answers(
    answers: Mapping[str, Any],
    *,
    validate: bool = True,
) -> dict[str, dict[str, Any]]:
    """Split one flat answers blob into ``{section_key: {answer_key: value}}``.

    This is the step the scoring engine must not improvise. Routing is exact set
    membership against each section's own item ids, so the ``disc_fc_*`` /
    ``disc_*`` prefix collision cannot bite: the two DISC banks are two
    sections, and ``disc.score_disc`` gets its two maps from two dictionary
    lookups rather than from a string test.

    Sections with no answers appear as empty dicts, so a caller can tell "no
    answers" from "no such section" without a ``.get`` default.
    """
    source = validate_answers(answers) if validate else dict(answers)
    partitioned: dict[str, dict[str, Any]] = {key: {} for key in SECTION_KEYS}
    for key, value in source.items():
        partitioned[answer_key_section(key).key][key] = value
    return partitioned


# ── Progress ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Progress:
    """What the interview page's header needs, computed in one place.

    ``percent`` is time-weighted rather than section-weighted, because nine
    sections range from 75 seconds to ten minutes and a bar that jumps an even
    ninth per section lies to the person about how much is left.
    """

    sections_total: int
    sections_done: int
    steps_total: int
    steps_done: int
    seconds_total: int
    seconds_done: int
    percent: int
    current_section_key: str | None
    is_complete: bool

    @property
    def seconds_remaining(self) -> int:
        return max(self.seconds_total - self.seconds_done, 0)

    @property
    def minutes_remaining(self) -> int:
        """Rounded up — a progress bar that under-promises is the kinder error."""
        return -(-self.seconds_remaining // 60)


def progress(
    sections_completed: object,
    answers: Mapping[str, Any] | None = None,
    *,
    section_cursor: str | None = None,
) -> Progress:
    """Compute interview progress for the UI.

    Completed sections contribute their whole estimate. The section in progress
    contributes pro rata by answered items, which is why ``answers`` is worth
    passing: without it a person half-way through the 50-item Big Five bank sees
    the same bar they saw when they started it.
    """
    done_keys = normalize_completed(sections_completed)
    done = set(done_keys)
    answers = answers or {}

    steps_total = sum(section.step_count for section in SECTIONS)
    seconds_total = sum(section.estimated_seconds for section in SECTIONS)

    steps_done = 0
    seconds_done = 0
    for section in SECTIONS:
        if section.key in done:
            steps_done += section.step_count
            seconds_done += section.estimated_seconds
            continue
        if not section.item_ids:
            continue
        answered = sum(
            1 for item_id in section.item_ids if answers.get(item_id) is not None
        )
        if not answered:
            continue
        steps_done += answered
        seconds_done += int(
            section.estimated_seconds * answered / len(section.item_ids)
        )

    current = resume_target(section_cursor, done_keys)
    percent = int(round(100 * seconds_done / seconds_total)) if seconds_total else 0
    return Progress(
        sections_total=len(SECTIONS),
        sections_done=len(done),
        steps_total=steps_total,
        steps_done=steps_done,
        seconds_total=seconds_total,
        seconds_done=seconds_done,
        percent=min(percent, 100),
        current_section_key=current.key if current else None,
        is_complete=current is None,
    )

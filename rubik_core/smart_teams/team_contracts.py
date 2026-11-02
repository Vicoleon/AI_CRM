"""The Team Builder request and result shapes — and why they cannot rank people.

ST-9 asks Cubie to assemble a project team. This module defines what the ask
looks like once parsed, what the answer looks like, and how an ask that cannot
be satisfied is represented. It does no work: no database, no LLM, no matching.
It is the contract, written before either half is implemented, because the
recorded failure on the previous wave of this build was a frontend and a router
drifting into two different APIs while every save failed silently.

## 1. The design constraint that shaped every type here

"Get me the best team" is one short step from "rank my employees", and that step
has to be **structurally impossible, not discouraged**. Smart Teams is advisory
only: nothing it produces may frame a person as more or less capable, higher or
lower performing, or better or worse than a colleague. A prompt instruction
saying so is not a guarantee — this build has already shipped documents that
violated instructions the prompt contained.

So the guarantee is in the types, three ways:

**(a) There is no per-person number, anywhere.** :class:`TeamCandidate` has no
score, no rating, no rank, no match percentage, no "fit". It carries a *set* of
the requested skills the person has confirmed (:attr:`TeamCandidate.covers`), an
availability *state*, and the work-style key and strength clusters their own
published profile carries. A set is not an ordering and a state is not a grade.
A renderer cannot leak a quality score because there is no field holding one,
and it cannot compute one either without inventing the arithmetic itself in
plain sight. This is enforced at import by :func:`_forbid_person_metrics`, which
walks the dataclass field names and raises on anything score-shaped — so adding
``fit_score`` in six months breaks the import, not a code review.

**(b) Roster order carries no meaning, and cannot.** ``candidates`` is required
to be sorted by email address, checked in ``__post_init__``. Ordering is the
oldest way to smuggle a ranking past a type system: a "roster" whose first entry
is the strongest is a ranking with the column heading removed. An alphabetical
roster has nothing to read into it, and any caller that sorted by merit gets an
exception instead of a leaderboard.

**(c) Judgements are about the GROUP, never a member.** Coverage
(:class:`SkillCoverage`) says whether the *work* is covered and by whom; balance
(:class:`StyleBalance`) says whether the *group* is all one style and which
clusters it is short on. Both are properties of a set of people. Neither has a
per-person breakdown, which is what turns "this group is short on Stewardship"
into "Alex is the weak link".

Two adjacent decisions, for the record. There is **no workload or utilization
field** — it was deferred out of v1, and a "capacity" number is a per-person
metric wearing a scheduling hat, so :func:`_forbid_person_metrics` refuses that
vocabulary too. And skill *level* (``familiar`` / ``working`` / ``strong`` /
``expert``) is carried per skill inside :class:`CoveredSkill` because a project
lead reasonably wants to know whether the person who covers pricing analysis is
familiar with it or expert in it — but level is a self-declared property of one
skill, never summed, averaged, or compared across people, and there is no field
in which a caller could put the sum.

## 2. Inputs, and what they are allowed to be

Team Builder v1 reads exactly three things (``docs/smart_teams/plan.md`` §5):

* **CONFIRMED skills only** — ``skills.confirmed_skills_for``, which filters
  ``confirmed.is_(True)`` and returns evidence-free DTOs. Never
  ``source != "inferred"``: a denial tombstone is ``confirmed = False``, which
  that predicate would let through.
* **Published profile layers** — for style balance, read through
  ``contracts.layer_of`` like everything else. An unpublished profile
  contributes nothing and is reported as such, not defaulted.
* **PTO / OOO** — ``rubik_core.availability``. The materialised table, not a
  live calendar impersonation per candidate; and only out to
  :data:`AVAILABILITY_HORIZON_DAYS`, past which the honest answer is
  :data:`AVAILABILITY_UNKNOWN` rather than "available".

Skills resolve against the ST-8 taxonomy via ``skill_taxonomy.resolve``, which
returns ``None`` for a phrase outside the vocabulary and never coerces onto a
neighbour. An unresolved phrase becomes an :class:`UnresolvedSkill` and a
:data:`SHORTFALL_SKILL_NOT_IN_TAXONOMY` entry — it is **reported**, never
dropped. Dropping it silently returns a team matched on fewer skills than the
requester asked for, presented as if it met the ask, which is the
confidently-wrong answer this whole module is written against.

## 3. An unsatisfiable ask is a legitimate answer

Four of them are, and each gets its own code so the renderer can say the true
thing: nobody has that skill confirmed; not enough people do; the people who do
are out for the window; nobody has published a profile at all. A
:class:`TeamResult` that carries any :class:`Shortfall` cannot report itself
complete — :attr:`TeamResult.presentation_state` derives from the shortfall
list, and ``__post_init__`` refuses a result whose required skills are uncovered
without a matching shortfall on the record. A partial team presented as complete
is the specific outcome that is forbidden, so it is the specific thing the
constructor checks.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from typing import Literal, get_args

from rubik_core.availability import pto
from rubik_core.database.smart_teams import SKILL_LEVELS
from rubik_core.smart_teams import comparison_contracts, skill_taxonomy
from rubik_core.smart_teams.instruments import disc, strengths

__all__ = [
    "TeamError",
    "AVAILABILITY_AVAILABLE",
    "AVAILABILITY_PARTIAL",
    "AVAILABILITY_OUT",
    "AVAILABILITY_UNKNOWN",
    "AVAILABILITY_STATES",
    "AVAILABILITY_HORIZON_DAYS",
    "STYLE_MIX_COMPLEMENTARY",
    "STYLE_MIX_ALIGNED",
    "STYLE_MIX_UNSPECIFIED",
    "STYLE_MIX_VALUES",
    "MAX_TEAM_SIZE",
    "SkillAsk",
    "UnresolvedSkill",
    "resolve_skill_phrases",
    "TeamRequest",
    "CoveredSkill",
    "TeamCandidate",
    "SkillCoverage",
    "StyleBalance",
    "Shortfall",
    "SHORTFALL_CODES",
    "SHORTFALL_SKILL_NOT_IN_TAXONOMY",
    "SHORTFALL_NO_ONE_WITH_SKILL",
    "SHORTFALL_NOT_ENOUGH_WITH_SKILL",
    "SHORTFALL_EVERYONE_OUT",
    "SHORTFALL_NO_PUBLISHED_PROFILES",
    "SHORTFALL_SIZE_NOT_MET",
    "SHORTFALL_WINDOW_BEYOND_HORIZON",
    "TeamResult",
    "STATE_COMPLETE",
    "STATE_PARTIAL",
    "STATE_EMPTY",
    "TEAM_ADVISORY_NOTICE",
    "NO_RANKING_NOTICE",
    "screen_team_prose",
]


class TeamError(ValueError):
    """A Team Builder shape does not match the contract in this module."""


def _fold(email: object) -> str:
    return str(email or "").strip().lower()


# ── The anti-score guard ──────────────────────────────────────────────────────
# Import-time, so a field added later fails the import rather than a review.

#: Field-name vocabulary that would let a per-person judgement onto a type here.
#: Ranking words, grading words, and the workload words deferred out of v1 — a
#: "capacity" or "utilization" number is a per-person metric in scheduling
#: clothes, and it sorts people just as well as a score does.
_PERSON_METRIC_WORDS = re.compile(
    r"(?:^|_)(?:score|scores|rank|ranking|rating|rate|grade|percentile|points|"
    r"stars|tier|weight|weighting|fit|fitness|match|suitability|quality|"
    r"strength_of|best|top|utilization|utilisation|workload|capacity|"
    r"allocation|availability_pct|load|billable|hours)(?:$|_)",
    re.IGNORECASE,
)


def _forbid_person_metrics(cls: type) -> type:
    """Refuse any field name that could carry a per-person judgement.

    Applied to every type in this module that describes a person or a group of
    them. The check is on names rather than types because the leak is semantic:
    ``int`` is fine for a calendar day count and catastrophic for a "match out
    of 10", and the name is the only place the difference is visible.

    Deliberately blunt. If a future field trips it and is genuinely innocent,
    rename the field — do not widen this pattern. Every widening is one more
    place a leaderboard can grow.
    """
    for field in dataclasses.fields(cls):
        if _PERSON_METRIC_WORDS.search(field.name):
            raise TeamError(
                f"{cls.__name__}.{field.name} is score-shaped. Team Builder is "
                "advisory: no type here may carry a per-person judgement, a "
                "ranking, or a workload number (workload is out of scope for "
                "v1). If the field is innocent, rename it; do not widen the "
                "guard in team_contracts._PERSON_METRIC_WORDS."
            )
    return cls


# ── Availability ──────────────────────────────────────────────────────────────

#: Free for the whole requested window, as far as the OOO table knows.
AVAILABILITY_AVAILABLE = "available"
#: Out for part of the window. A fact about a calendar, not a deduction.
AVAILABILITY_PARTIAL = "out_part_of_window"
#: Out for the whole window.
AVAILABILITY_OUT = "out_for_window"
#: We do not know — the window runs past the horizon we can see, or this person
#: has no OOO data. Never collapsed into "available": "we did not check" and
#: "they are free" are the same value to a renderer and opposite facts to a
#: project lead.
AVAILABILITY_UNKNOWN = "unknown"

AVAILABILITY_STATES: tuple[str, ...] = (
    AVAILABILITY_AVAILABLE,
    AVAILABILITY_PARTIAL,
    AVAILABILITY_OUT,
    AVAILABILITY_UNKNOWN,
)

Availability = Literal["available", "out_part_of_window", "out_for_window", "unknown"]

assert set(get_args(Availability)) == set(AVAILABILITY_STATES), (
    "Availability and AVAILABILITY_STATES disagree"
)

#: How far ahead availability can be known at all. Imported from the PTO module
#: rather than retyped: it is that module's forward look-around window, and a
#: second copy of "45" here would drift the day somebody tunes the detector.
#:
#: REVIEW: this reads a private name. ``pto._DEFAULT_FORWARD_DAYS`` should be
#: promoted to a public constant; importing the private is still strictly better
#: than duplicating the number, which is the drift this pass exists to stop.
AVAILABILITY_HORIZON_DAYS: int = pto._DEFAULT_FORWARD_DAYS

#: The sentence a result must carry when the requested window runs past the
#: horizon. Authored, because "we checked and they are free" and "we cannot see
#: that far" must not read the same.
HORIZON_NOTE = (
    f"Time off is only visible about {AVAILABILITY_HORIZON_DAYS} days ahead, so "
    "any part of the project window past that has not been checked — treat "
    "those weeks as unknown rather than clear."
)


# ── The request ───────────────────────────────────────────────────────────────

#: The requester wants a spread of working styles.
STYLE_MIX_COMPLEMENTARY = "complementary"
#: The requester wants people who work the same way. A legitimate ask — a
#: two-week sprint with one deliverable often wants one pace, not four.
STYLE_MIX_ALIGNED = "aligned"
#: They did not say. The default, and it must stay the default: guessing
#: "complementary" would invent a requirement and then report a shortfall
#: against it.
STYLE_MIX_UNSPECIFIED = "unspecified"

STYLE_MIX_VALUES: tuple[str, ...] = (
    STYLE_MIX_COMPLEMENTARY,
    STYLE_MIX_ALIGNED,
    STYLE_MIX_UNSPECIFIED,
)

#: A ceiling on the requested size. Not a policy about team sizes — a guard on
#: a chat-parsed integer, so "give me a team of 500" is a refusal instead of a
#: query that reads the whole roster into one chat turn.
MAX_TEAM_SIZE = 12

_CLUSTER_VALUES: frozenset[str] = frozenset(
    cluster.value for cluster in strengths.Cluster
)

_WORK_STYLE_KEYS: frozenset[str] = frozenset(disc.WORK_STYLES)


@dataclasses.dataclass(frozen=True)
class SkillAsk:
    """One requested skill, resolved to a taxonomy key.

    ``phrase`` is kept alongside the key because the result echoes the ask back
    the way the requester phrased it. Echoing the resolved key instead puts
    ``post_close_qc`` in front of the model, which repeats it to the user as if
    it were English.
    """

    skill_key: str
    phrase: str
    required: bool = True

    def __post_init__(self) -> None:
        if self.skill_key not in skill_taxonomy.SKILLS:
            raise TeamError(
                f"{self.skill_key!r} is not in the ST-8 taxonomy; an unrecognised "
                "ask is an UnresolvedSkill, never a SkillAsk with a guessed key"
            )
        if not str(self.phrase).strip():
            raise TeamError(f"{self.skill_key}: SkillAsk needs the phrase asked for")

    @property
    def name(self) -> str:
        """The taxonomy's display name for this skill."""
        return skill_taxonomy.SKILLS[self.skill_key].name


@dataclasses.dataclass(frozen=True)
class UnresolvedSkill:
    """A requested phrase that is not in the taxonomy. Reported, never dropped.

    ``suggestions`` are taxonomy keys a search offered, and they are offered to
    the *requester* to pick from — they are never applied. Coercing a near-miss
    onto a neighbouring skill is how somebody ends up staffed onto work they
    have never touched, and it is the rule ``skill_taxonomy.resolve`` already
    states for its own callers.
    """

    phrase: str
    suggestions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not str(self.phrase).strip():
            raise TeamError("UnresolvedSkill needs the phrase that failed to resolve")
        unknown = [key for key in self.suggestions if key not in skill_taxonomy.SKILLS]
        if unknown:
            raise TeamError(
                f"UnresolvedSkill suggestions {unknown!r} are not taxonomy keys"
            )


def resolve_skill_phrases(
    phrases: Iterable[str], *, required: bool = True
) -> tuple[tuple[SkillAsk, ...], tuple[UnresolvedSkill, ...]]:
    """Split requested phrases into resolved asks and reportable failures.

    Exact resolution only, via ``skill_taxonomy.resolve``. No fuzzy fallback:
    the function that would supply one is ``skill_taxonomy.search``, which needs
    a database session and belongs to the caller that has one — it can attach
    suggestions to the :class:`UnresolvedSkill` afterwards. What this function
    guarantees is that nothing resolves to a key the requester did not name.
    """
    asks: list[SkillAsk] = []
    unresolved: list[UnresolvedSkill] = []
    seen: set[str] = set()
    for raw in phrases or ():
        phrase = str(raw or "").strip()
        if not phrase:
            continue
        key = skill_taxonomy.resolve(phrase)
        if key is None:
            if phrase.lower() not in {u.phrase.lower() for u in unresolved}:
                unresolved.append(UnresolvedSkill(phrase=phrase))
            continue
        if key in seen:
            continue
        seen.add(key)
        asks.append(SkillAsk(skill_key=key, phrase=phrase, required=required))
    return tuple(asks), tuple(unresolved)


@_forbid_person_metrics
@dataclasses.dataclass(frozen=True)
class TeamRequest:
    """A parsed Team Builder ask.

    ``requested_by`` comes from the session, never from a tool parameter — the
    Smart Teams surfaces take their caller only from ``get_current_user``,
    because this is the feature holding people's private interview answers and
    an accepted ``user_email`` would be a plain impersonation primitive over it.
    It is stored here so the result can be scoped and audited, not so it can be
    supplied.

    ``size`` is ``None`` when the requester did not say, and that stays ``None``:
    inventing a default size manufactures a
    :data:`SHORTFALL_SIZE_NOT_MET` against a requirement nobody stated.
    """

    requested_by: str
    skills: tuple[SkillAsk, ...] = ()
    unresolved_skills: tuple[UnresolvedSkill, ...] = ()
    size: int | None = None
    starts_on: date | None = None
    ends_on: date | None = None
    style_mix: str = STYLE_MIX_UNSPECIFIED
    desired_clusters: frozenset[str] = frozenset()
    raw_ask: str = ""

    def __post_init__(self) -> None:
        if not self.requested_by or self.requested_by != _fold(self.requested_by):
            raise TeamError(
                f"requested_by {self.requested_by!r} is empty or not folded"
            )
        if self.style_mix not in STYLE_MIX_VALUES:
            raise TeamError(
                f"style_mix {self.style_mix!r} is not one of {STYLE_MIX_VALUES}"
            )
        if self.size is not None and not 1 <= self.size <= MAX_TEAM_SIZE:
            raise TeamError(
                f"size {self.size!r} is outside 1..{MAX_TEAM_SIZE}; a chat-parsed "
                "integer needs a ceiling"
            )
        if self.starts_on and self.ends_on and self.ends_on < self.starts_on:
            raise TeamError(
                f"window ends {self.ends_on} before it starts {self.starts_on}"
            )
        unknown = sorted(self.desired_clusters - _CLUSTER_VALUES)
        if unknown:
            raise TeamError(
                f"desired_clusters {unknown!r} are not strengths clusters "
                f"({sorted(_CLUSTER_VALUES)})"
            )
        keys = [ask.skill_key for ask in self.skills]
        if len(keys) != len(set(keys)):
            raise TeamError("the same skill is asked for twice")

    @property
    def required_skill_keys(self) -> tuple[str, ...]:
        return tuple(ask.skill_key for ask in self.skills if ask.required)

    @property
    def optional_skill_keys(self) -> tuple[str, ...]:
        return tuple(ask.skill_key for ask in self.skills if not ask.required)

    @property
    def has_window(self) -> bool:
        return self.starts_on is not None or self.ends_on is not None

    def window_beyond_horizon(self, *, today: date) -> bool:
        """Whether the requested window runs past what availability can see.

        ``True`` obliges the result to carry :data:`HORIZON_NOTE` and a
        :data:`SHORTFALL_WINDOW_BEYOND_HORIZON` entry. The point is not that the
        request is invalid — a Q4 project is a perfectly good ask — but that
        "nobody is out" would be a claim about weeks nobody looked at.
        """
        if self.ends_on is None:
            return False
        return (self.ends_on - today).days > AVAILABILITY_HORIZON_DAYS

    @classmethod
    def from_phrases(
        cls,
        *,
        requested_by: str,
        required_phrases: Sequence[str] = (),
        optional_phrases: Sequence[str] = (),
        size: int | None = None,
        starts_on: date | None = None,
        ends_on: date | None = None,
        style_mix: str = STYLE_MIX_UNSPECIFIED,
        desired_clusters: Iterable[str] = (),
        raw_ask: str = "",
    ) -> TeamRequest:
        """Build a request from free-text skill phrases, reporting every failure.

        The chat tool's parameters are flat scalars (every registered ADK tool in
        this codebase takes only ``str``/``int``), so the phrases arrive as
        free text and land here. Unresolved phrases from both lists are kept
        together: the requester cares that "reg z audit" was not understood, not
        which of their two lists it was in.
        """
        required, unresolved_required = resolve_skill_phrases(
            required_phrases, required=True
        )
        optional, unresolved_optional = resolve_skill_phrases(
            optional_phrases, required=False
        )
        required_keys = {ask.skill_key for ask in required}
        optional = tuple(ask for ask in optional if ask.skill_key not in required_keys)
        return cls(
            requested_by=_fold(requested_by),
            skills=(*required, *optional),
            unresolved_skills=(*unresolved_required, *unresolved_optional),
            size=size,
            starts_on=starts_on,
            ends_on=ends_on,
            style_mix=style_mix,
            desired_clusters=frozenset(desired_clusters),
            raw_ask=raw_ask,
        )


# ── Candidates ────────────────────────────────────────────────────────────────


@_forbid_person_metrics
@dataclasses.dataclass(frozen=True)
class CoveredSkill:
    """One requested skill this person has CONFIRMED, with its declared level.

    Built from ``skills.ConfirmedSkill``, which is already the evidence-free
    projection — no rationale, no meeting titles, no transcript excerpts. Level
    is carried because "familiar with pricing analysis" and "expert in pricing
    analysis" are different answers to a staffing question. It is a property of
    one skill and it is never summed, averaged, or compared between people:
    there is no field anywhere in this module that could hold the total.
    """

    skill_key: str
    level: str | None
    origin: str

    def __post_init__(self) -> None:
        if self.skill_key not in skill_taxonomy.SKILLS:
            raise TeamError(f"{self.skill_key!r} is not in the ST-8 taxonomy")
        if self.level is not None and self.level not in SKILL_LEVELS:
            raise TeamError(
                f"{self.skill_key}: level {self.level!r} is not one of {SKILL_LEVELS}"
            )

    @property
    def name(self) -> str:
        return skill_taxonomy.SKILLS[self.skill_key].name


@_forbid_person_metrics
@dataclasses.dataclass(frozen=True)
class TeamCandidate:
    """One person in a proposed group. Deliberately unrankable.

    Every field is either an identity, a set, or a state:

    * ``covers`` — the requested skills they have confirmed. A ``tuple`` of
      :class:`CoveredSkill`, keyed by skill, in taxonomy order. Not a count, not
      a percentage of the ask.
    * ``availability`` / ``back_on`` — a state from
      :data:`AVAILABILITY_STATES` and a calendar date. ``back_on`` is a date
      rather than a day count on purpose: "back on the 14th" is useful and
      "out 6 days" is a number that sorts people.
    * ``work_style_key`` / ``clusters`` — what their own published profile says,
      or ``None`` / empty when that layer is closed. These feed
      :class:`StyleBalance`, which is a group statement; they are never
      described here as better or worse suited to anything.

    What is absent is the design: no score, no rank, no match percentage, no
    "fit", no seniority, no workload, no utilisation. :func:`_forbid_person_metrics`
    enforces the absence at import, so a later field named ``fit_score`` breaks
    the module rather than reaching a renderer that would happily print it.
    """

    user_email: str
    display_name: str
    covers: tuple[CoveredSkill, ...] = ()
    availability: str = AVAILABILITY_UNKNOWN
    back_on: date | None = None
    work_style_key: str | None = None
    #: ``(x, y)`` on the style map, or ``None`` for "not placed". A DIRECTION at
    #: a fixed radius — it says which way somebody leans and deliberately not how
    #: far, so it is not an intensity in coordinate clothing. Named ``position``
    #: rather than ``point``/``score`` because it describes rather than judges,
    #: which is the line :func:`_forbid_person_metrics` draws.
    map_position: tuple[float, float] | None = None
    clusters: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.user_email or self.user_email != _fold(self.user_email):
            raise TeamError(f"user_email {self.user_email!r} is empty or not folded")
        if not str(self.display_name).strip():
            raise TeamError(f"{self.user_email}: a candidate needs a display name")
        if self.availability not in AVAILABILITY_STATES:
            raise TeamError(
                f"{self.user_email}: availability {self.availability!r} is not one "
                f"of {AVAILABILITY_STATES}"
            )
        if self.availability == AVAILABILITY_AVAILABLE and self.back_on is not None:
            raise TeamError(
                f"{self.user_email}: back_on is set on an available candidate; a "
                "return date only means something when they are out"
            )
        keys = [item.skill_key for item in self.covers]
        if len(keys) != len(set(keys)):
            raise TeamError(f"{self.user_email}: the same skill is covered twice")
        if (
            self.work_style_key is not None
            and self.work_style_key not in _WORK_STYLE_KEYS
        ):
            raise TeamError(
                f"{self.user_email}: work_style_key {self.work_style_key!r} is not "
                "one of disc.WORK_STYLES"
            )
        unknown = sorted(self.clusters - _CLUSTER_VALUES)
        if unknown:
            raise TeamError(f"{self.user_email}: unknown clusters {unknown!r}")

    @property
    def covered_keys(self) -> frozenset[str]:
        return frozenset(item.skill_key for item in self.covers)

    @property
    def is_out_for_window(self) -> bool:
        return self.availability == AVAILABILITY_OUT


# ── Group-level judgements ────────────────────────────────────────────────────


@_forbid_person_metrics
@dataclasses.dataclass(frozen=True)
class SkillCoverage:
    """Whether the WORK is covered, and by whom. A group fact, per skill.

    ``covered_by`` is a tuple of email addresses **in roster order**, which is
    alphabetical (see :class:`TeamResult`). It is not a preference order and
    there is no "primary" holder of a skill: the moment one of three people who
    cover contract review is listed first for a reason, the result has ranked
    them.
    """

    skill_key: str
    phrase: str
    required: bool
    covered_by: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.skill_key not in skill_taxonomy.SKILLS:
            raise TeamError(f"{self.skill_key!r} is not in the ST-8 taxonomy")
        for email in self.covered_by:
            if not email or email != _fold(email):
                raise TeamError(
                    f"{self.skill_key}: covered_by has an unfolded address {email!r}"
                )
        if tuple(sorted(self.covered_by)) != self.covered_by:
            raise TeamError(
                f"{self.skill_key}: covered_by must be in roster (alphabetical) "
                "order. An order chosen for any other reason is a ranking of the "
                "people who hold this skill"
            )

    @property
    def covered(self) -> bool:
        return bool(self.covered_by)

    @property
    def name(self) -> str:
        return skill_taxonomy.SKILLS[self.skill_key].name


@_forbid_person_metrics
@dataclasses.dataclass(frozen=True)
class StyleBalance:
    """What the GROUP's working styles add up to. Never a per-person read.

    ``clusters_missing`` is the useful half, and ``note`` is where it becomes a
    sentence: ``strengths.CLUSTER_MEANING`` already says what a team short on
    each cluster costs ("a team short on this ships confident answers to the
    wrong question"), and that authored copy is what belongs here rather than
    anything a model writes about the individuals.

    ``all_one_style`` answers the one question the requester actually asked when
    they said "complementary". ``styles_present`` is a sorted multiset of DISC
    work-style keys — sorted so it cannot be zipped back onto the roster and
    read as a per-person list.

    There is no per-person mapping in this class, and that is the point: "this
    group is five Momentum and no Stewardship" is a sentence a manager can act
    on; the same fact broken out by name is a set of labels stuck to people.
    """

    clusters_covered: frozenset[str] = frozenset()
    clusters_missing: frozenset[str] = frozenset()
    styles_present: tuple[str, ...] = ()
    all_one_style: bool = False
    profiles_missing_for: int = 0
    note: str = ""

    def __post_init__(self) -> None:
        for name, values in (
            ("clusters_covered", self.clusters_covered),
            ("clusters_missing", self.clusters_missing),
        ):
            unknown = sorted(values - _CLUSTER_VALUES)
            if unknown:
                raise TeamError(f"{name} has unknown clusters {unknown!r}")
        overlap = sorted(self.clusters_covered & self.clusters_missing)
        if overlap:
            raise TeamError(f"clusters {overlap!r} are both covered and missing")
        unknown_styles = sorted(set(self.styles_present) - _WORK_STYLE_KEYS)
        if unknown_styles:
            raise TeamError(f"styles_present has unknown styles {unknown_styles!r}")
        if tuple(sorted(self.styles_present)) != self.styles_present:
            raise TeamError(
                "styles_present must be sorted; an unsorted multiset can be "
                "read back as a per-person list"
            )
        if self.profiles_missing_for < 0:
            raise TeamError("profiles_missing_for cannot be negative")
        if self.all_one_style and len(set(self.styles_present)) > 1:
            raise TeamError(
                f"all_one_style is set but {sorted(set(self.styles_present))!r} "
                "styles are present"
            )

    @property
    def cluster_notes(self) -> Mapping[str, str]:
        """``{missing cluster: what a team short on it costs}``, authored copy.

        Read from ``strengths.CLUSTER_MEANING`` rather than written here, so the
        one sentence about a Stewardship gap is the same sentence everywhere it
        appears.
        """
        return {
            cluster.value: strengths.CLUSTER_MEANING[cluster]
            for cluster in strengths.Cluster
            if cluster.value in self.clusters_missing
        }


# ── Shortfalls: the ways an ask legitimately cannot be met ────────────────────

#: A requested phrase is not in the taxonomy at all. Reported so the requester
#: can rephrase; never silently dropped from the filter.
SHORTFALL_SKILL_NOT_IN_TAXONOMY = "skill_not_in_taxonomy"
#: A real skill nobody has confirmed. Note what this does NOT mean: nobody at
#: the company can do it. It means nobody has confirmed it in Smart Teams.
SHORTFALL_NO_ONE_WITH_SKILL = "no_one_with_skill"
#: Fewer people confirm it than the request needs.
SHORTFALL_NOT_ENOUGH_WITH_SKILL = "not_enough_with_skill"
#: The people who confirm it are out for the requested window.
SHORTFALL_EVERYONE_OUT = "everyone_out"
#: Nobody in scope has a published profile, so there is nothing to assemble
#: from. Distinct from "no skills": the interview has not been taken.
SHORTFALL_NO_PUBLISHED_PROFILES = "no_published_profiles"
#: A size was asked for and fewer people can be proposed.
SHORTFALL_SIZE_NOT_MET = "size_not_met"
#: The window runs past what availability can see.
SHORTFALL_WINDOW_BEYOND_HORIZON = "window_beyond_horizon"

SHORTFALL_CODES: tuple[str, ...] = (
    SHORTFALL_SKILL_NOT_IN_TAXONOMY,
    SHORTFALL_NO_ONE_WITH_SKILL,
    SHORTFALL_NOT_ENOUGH_WITH_SKILL,
    SHORTFALL_EVERYONE_OUT,
    SHORTFALL_NO_PUBLISHED_PROFILES,
    SHORTFALL_SIZE_NOT_MET,
    SHORTFALL_WINDOW_BEYOND_HORIZON,
)

#: Shortfalls that mean there is nothing to propose at all. A result carrying
#: one of these must have an empty roster — a candidate list alongside "nobody
#: has published a profile" is two contradictory answers in one payload.
_EMPTY_ROSTER_CODES: frozenset[str] = frozenset({SHORTFALL_NO_PUBLISHED_PROFILES})


@_forbid_person_metrics
@dataclasses.dataclass(frozen=True)
class Shortfall:
    """One reason the ask was not fully met, with the sentence to say about it.

    ``detail`` is written for the model that renders the answer: it states the
    fact and forbids the over-reading. "Nobody has confirmed contract review in Smart Teams" must not
    become "the company has nobody who can review contracts", and the only place that
    instruction can live is next to the fact.
    """

    code: str
    detail: str
    skill_key: str | None = None
    phrase: str | None = None

    def __post_init__(self) -> None:
        if self.code not in SHORTFALL_CODES:
            raise TeamError(
                f"shortfall code {self.code!r} is not one of {SHORTFALL_CODES}; add "
                "it there in the same change that emits it"
            )
        if not str(self.detail).strip():
            raise TeamError(f"{self.code}: a shortfall with no detail says nothing")
        if self.skill_key is not None and self.skill_key not in skill_taxonomy.SKILLS:
            raise TeamError(f"{self.code}: {self.skill_key!r} is not a taxonomy key")
        if self.code == SHORTFALL_SKILL_NOT_IN_TAXONOMY:
            if self.skill_key is not None:
                raise TeamError(
                    "skill_not_in_taxonomy cannot carry a taxonomy key — the whole "
                    "point is that the phrase did not resolve to one"
                )
            if not str(self.phrase or "").strip():
                raise TeamError(
                    "skill_not_in_taxonomy must carry the phrase that failed, or "
                    "the requester cannot tell which part of their ask was dropped"
                )
        if (
            self.code
            in {
                SHORTFALL_NO_ONE_WITH_SKILL,
                SHORTFALL_NOT_ENOUGH_WITH_SKILL,
                SHORTFALL_EVERYONE_OUT,
            }
            and self.skill_key is None
        ):
            raise TeamError(f"{self.code} must name the skill it is about")


# ── The result ────────────────────────────────────────────────────────────────

STATE_COMPLETE = "complete"
STATE_PARTIAL = "partial"
STATE_EMPTY = "empty"

RESULT_STATES: tuple[str, ...] = (STATE_COMPLETE, STATE_PARTIAL, STATE_EMPTY)


@_forbid_person_metrics
@dataclasses.dataclass(frozen=True)
class TeamResult:
    """A proposed group, what it covers, how it balances, and what is missing.

    Three structural guarantees, all checked in ``__post_init__``:

    1. **The roster is alphabetical.** Ordering is the last place a ranking can
       hide once the score fields are gone, so an unsorted roster raises. There
       is also no ``alternates`` list ranked below the roster for the same
       reason — a second tier is a ranking with two buckets.
    2. **An uncovered required skill must be on the record.** If a required
       :class:`SkillCoverage` has nobody covering it, a matching
       :class:`Shortfall` must exist. A partial team presented as complete is
       the forbidden outcome, so it is the checked one.
    3. **A no-profiles result has no roster.** See :data:`_EMPTY_ROSTER_CODES`.

    :attr:`presentation_state` is derived, not stored. A caller cannot set
    ``complete`` on a result that carries shortfalls, because there is no field
    to set.
    """

    request: TeamRequest
    candidates: tuple[TeamCandidate, ...] = ()
    coverage: tuple[SkillCoverage, ...] = ()
    balance: StyleBalance = dataclasses.field(default_factory=StyleBalance)
    shortfalls: tuple[Shortfall, ...] = ()
    horizon_note: str | None = None

    def __post_init__(self) -> None:
        emails = [candidate.user_email for candidate in self.candidates]
        if len(emails) != len(set(emails)):
            raise TeamError("the same person appears twice in the roster")
        if sorted(emails) != emails:
            raise TeamError(
                "candidates must be sorted by email address. Roster order in this "
                "result carries no meaning by construction; an order chosen for "
                "any other reason is a ranking of people, which Team Builder must "
                "be structurally incapable of producing"
            )
        keys = [item.skill_key for item in self.coverage]
        if len(keys) != len(set(keys)):
            raise TeamError("the same skill appears twice in coverage")
        asked = {ask.skill_key for ask in self.request.skills}
        stray = sorted(set(keys) - asked)
        if stray:
            raise TeamError(f"coverage reports {stray!r}, which were not asked for")
        codes = {item.code for item in self.shortfalls}
        flagged = {
            item.skill_key for item in self.shortfalls if item.skill_key is not None
        }
        for item in self.coverage:
            if item.required and not item.covered and item.skill_key not in flagged:
                raise TeamError(
                    f"{item.skill_key} is required and uncovered but carries no "
                    "shortfall. Every gap is on the record or the result reads as "
                    "a complete team"
                )
        for item in self.coverage:
            unknown = sorted(set(item.covered_by) - set(emails))
            if unknown:
                raise TeamError(
                    f"{item.skill_key}: covered_by names {unknown!r}, who are not "
                    "in the roster"
                )
        if codes & _EMPTY_ROSTER_CODES and self.candidates:
            raise TeamError(
                f"{sorted(codes & _EMPTY_ROSTER_CODES)!r} means there is nothing to "
                "assemble from, so the roster must be empty"
            )
        unresolved_phrases = {
            skill.phrase.strip().lower() for skill in self.request.unresolved_skills
        }
        reported = {
            (item.phrase or "").strip().lower()
            for item in self.shortfalls
            if item.code == SHORTFALL_SKILL_NOT_IN_TAXONOMY
        }
        unreported = sorted(unresolved_phrases - reported)
        if unreported:
            raise TeamError(
                f"the request did not resolve {unreported!r} and the result does "
                "not report it. An unrecognised skill is told to the requester, "
                "never dropped from the filter"
            )
        if SHORTFALL_WINDOW_BEYOND_HORIZON in codes and not self.horizon_note:
            raise TeamError(
                "a window past the availability horizon must carry the note "
                "saying so; otherwise unchecked weeks read as clear weeks"
            )

    @property
    def presentation_state(self) -> str:
        """``complete`` / ``partial`` / ``empty``. Derived, never stored.

        ``complete`` requires a roster, every required skill covered, the
        requested size met if one was given, and no shortfalls at all. Anything
        less is ``partial``, and a renderer that reports a partial result as a
        finished team is contradicting this property rather than filling a gap
        in it.
        """
        if not self.candidates:
            return STATE_EMPTY
        if self.shortfalls:
            return STATE_PARTIAL
        if any(item.required and not item.covered for item in self.coverage):
            return STATE_PARTIAL
        if self.request.size is not None and len(self.candidates) != self.request.size:
            return STATE_PARTIAL
        return STATE_COMPLETE

    @property
    def is_complete(self) -> bool:
        return self.presentation_state == STATE_COMPLETE

    @property
    def uncovered_required(self) -> tuple[str, ...]:
        return tuple(
            item.skill_key
            for item in self.coverage
            if item.required and not item.covered
        )

    @property
    def emails(self) -> tuple[str, ...]:
        return tuple(candidate.user_email for candidate in self.candidates)


# ── Authored notices, and the prose screen ────────────────────────────────────

#: Every rendering of a team result carries this. It is not decoration: the
#: single most likely misreading of a staffing suggestion is that Cubie assessed
#: who is best, and the answer has to say out loud that it did not.
NO_RANKING_NOTICE = (
    "This is a suggestion about coverage and availability, not an assessment of "
    "anybody. It compares what the work needs against what people have said "
    "about themselves, and it does not rate, rank, or compare colleagues."
)

TEAM_ADVISORY_NOTICE = f"{disc.ADVISORY_NOTICE} {NO_RANKING_NOTICE}"


def screen_team_prose(text: object) -> str | None:
    """Refuse ranking language in a team answer. Same net as the pair guide's.

    Delegates to ``comparison_contracts.screen_no_ranking`` rather than
    restating the patterns, so the pair guide and the Team Builder answer cannot
    drift into two different definitions of a forbidden claim. ST-9's chat
    report never passes through ``guide_prompts``, so this is the only screen in
    front of it and it must be called explicitly.
    """
    return comparison_contracts.screen_no_ranking(text)

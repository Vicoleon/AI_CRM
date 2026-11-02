"""Typed artifacts for attendee (person-level) intelligence — UC-47.

One `AttendeeIntel` per person on a meeting invite: what their LinkedIn-facing
professional record says (`PersonProfile`), what they have been posting and
engaging with lately (`ActivityItem`), and the read on how to talk to them
(`PersonaRead`).

The split is deliberate and load-bearing for grounding: `PersonProfile` and
`ActivityItem` are **observations** — every one carries a URL, and the research
step drops anything it cannot cite (cite-or-drop, same contract as the X-Ray web
agent). `PersonaRead` is the only **inference** in the bundle, and it is fenced
into its own object with an explicit `confidence` so the UI can label it as a
read rather than a fact, and so a thin evidence base produces a visibly
low-confidence card instead of a confident invention.

These cross module boundaries (research → store → router → frontend), so every
object round-trips through `as_dict()` / `from_dict()` — the store persists them
as JSON and the API hands the same shape to React.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _s(value: Any, limit: int = 400) -> str:
    """Coerce an LLM/provider value to a bounded string. Never raises."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(v) for v in value if v is not None)
    return str(value).strip()[:limit]


def _slist(value: Any, limit: int = 10, item_limit: int = 300) -> list[str]:
    """Coerce to a bounded list of bounded strings. Tolerates a single string."""
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        items = [value]
    out: list[str] = []
    for item in items:
        text = _s(item, item_limit)
        if text:
            out.append(text)
    return out[:limit]


@dataclass
class ActivityItem:
    """One piece of public activity — a post, comment, article, talk, mention.

    ``url`` is mandatory at the service boundary: an activity item without a
    link is unverifiable by the reader, and an unverifiable "they recently
    posted about X" is exactly the kind of plausible fabrication that costs a
    sales rep credibility in the room. `AttendeeIntel.from_dict` keeps whatever
    it is given (so a cached row survives), but the research pipeline drops
    uncited items before they ever get here.
    """

    kind: str = "post"  # post | comment | article | talk | mention | other
    posted_at: str = ""  # best-effort, provider-shaped ("2026-07", "3w ago")
    summary: str = ""  # what they said, in their framing
    theme: str = ""  # the recurring topic this belongs to
    url: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "posted_at": self.posted_at,
            "summary": self.summary,
            "theme": self.theme,
            "url": self.url,
        }

    @classmethod
    def from_dict(cls, data: Any) -> ActivityItem:
        if not isinstance(data, dict):
            return cls(summary=_s(data))
        return cls(
            kind=_s(data.get("kind") or "post", 40),
            posted_at=_s(data.get("posted_at") or data.get("date"), 60),
            summary=_s(data.get("summary") or data.get("text"), 600),
            theme=_s(data.get("theme"), 120),
            url=_s(data.get("url") or data.get("link"), 600),
        )


@dataclass
class PersonProfile:
    """The professional record — position, employer, trajectory."""

    name: str = ""
    linkedin_url: str = ""
    headline: str = ""  # the LinkedIn headline, verbatim where available
    current_title: str = ""
    current_company: str = ""
    tenure: str = ""  # "since 2023", "4 yrs" — provider-shaped
    location: str = ""
    career_summary: str = ""  # 1-3 sentences of trajectory
    credentials: list[str] = field(default_factory=list)  # certs, boards, degrees

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "linkedin_url": self.linkedin_url,
            "headline": self.headline,
            "current_title": self.current_title,
            "current_company": self.current_company,
            "tenure": self.tenure,
            "location": self.location,
            "career_summary": self.career_summary,
            "credentials": self.credentials,
        }

    @classmethod
    def from_dict(cls, data: Any) -> PersonProfile:
        if not isinstance(data, dict):
            return cls()
        return cls(
            name=_s(data.get("name"), 200),
            linkedin_url=_s(data.get("linkedin_url") or data.get("profile_url"), 600),
            headline=_s(data.get("headline"), 400),
            current_title=_s(data.get("current_title") or data.get("title"), 240),
            current_company=_s(data.get("current_company") or data.get("company"), 240),
            tenure=_s(data.get("tenure"), 120),
            location=_s(data.get("location"), 160),
            career_summary=_s(data.get("career_summary"), 800),
            credentials=_slist(data.get("credentials"), 8, 160),
        )

    def is_empty(self) -> bool:
        """True when nothing usable was found — drives the 'no profile' UI."""
        return not any(
            (
                self.linkedin_url,
                self.headline,
                self.current_title,
                self.career_summary,
            )
        )


@dataclass
class PersonaRead:
    """How to talk to this person. Inference, not fact — hence ``confidence``.

    ``confidence`` is ``high`` | ``medium`` | ``low``, and the synthesis prompt
    is instructed to return ``low`` whenever the evidence is a headline and
    nothing else. The UI renders the band, so a rep can tell a read built on
    twelve posts from a read built on a job title.
    """

    communication_style: str = ""  # how they write/speak, evidenced
    decision_style: str = ""  # how they appear to evaluate and decide
    priorities: list[str] = field(default_factory=list)  # what they push publicly
    posts_about: list[str] = field(default_factory=list)  # recurring themes
    conversation_openers: list[str] = field(default_factory=list)
    avoid: list[str] = field(default_factory=list)  # what will land badly
    confidence: str = "low"  # high | medium | low
    basis: str = ""  # one line naming what the read rests on

    def as_dict(self) -> dict[str, Any]:
        return {
            "communication_style": self.communication_style,
            "decision_style": self.decision_style,
            "priorities": self.priorities,
            "posts_about": self.posts_about,
            "conversation_openers": self.conversation_openers,
            "avoid": self.avoid,
            "confidence": self.confidence,
            "basis": self.basis,
        }

    @classmethod
    def from_dict(cls, data: Any) -> PersonaRead:
        if not isinstance(data, dict):
            return cls()
        confidence = _s(data.get("confidence"), 20).lower()
        if confidence not in ("high", "medium", "low"):
            confidence = "low"
        return cls(
            communication_style=_s(data.get("communication_style"), 600),
            decision_style=_s(data.get("decision_style"), 600),
            priorities=_slist(data.get("priorities"), 6),
            posts_about=_slist(data.get("posts_about"), 8, 160),
            conversation_openers=_slist(data.get("conversation_openers"), 5),
            avoid=_slist(data.get("avoid"), 5),
            confidence=confidence,
            basis=_s(data.get("basis"), 400),
        )

    def is_empty(self) -> bool:
        return not any(
            (
                self.communication_style,
                self.decision_style,
                self.priorities,
                self.posts_about,
                self.conversation_openers,
            )
        )


@dataclass
class AttendeeIntel:
    """Everything we know about one attendee, plus how we came to know it.

    ``sources`` are the citation URLs behind the whole bundle; ``provider``
    names which source produced it (``web_search`` / ``linkedin_api`` /
    ``cache``) so a thin card can be explained rather than just look broken.
    ``error`` is set instead of raising — one unresearchable attendee must
    never sink a briefing.
    """

    email: str = ""
    name: str = ""
    profile: PersonProfile = field(default_factory=PersonProfile)
    activity: list[ActivityItem] = field(default_factory=list)
    persona: PersonaRead = field(default_factory=PersonaRead)
    sources: list[str] = field(default_factory=list)
    provider: str = ""
    researched_at: str = ""  # ISO-8601 UTC
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "name": self.name,
            "profile": self.profile.as_dict(),
            "activity": [a.as_dict() for a in self.activity],
            "persona": self.persona.as_dict(),
            "sources": self.sources,
            "provider": self.provider,
            "researched_at": self.researched_at,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Any) -> AttendeeIntel:
        if not isinstance(data, dict):
            return cls()
        return cls(
            email=_s(data.get("email"), 320).lower(),
            name=_s(data.get("name"), 200),
            profile=PersonProfile.from_dict(data.get("profile")),
            activity=[ActivityItem.from_dict(a) for a in (data.get("activity") or [])][
                :15
            ],
            persona=PersonaRead.from_dict(data.get("persona")),
            sources=_slist(data.get("sources"), 20, 600),
            provider=_s(data.get("provider"), 40),
            researched_at=_s(data.get("researched_at"), 40),
            error=_s(data.get("error"), 400),
        )

    def has_content(self) -> bool:
        """True when this card is worth rendering / worth putting in a prompt."""
        return bool(
            self.activity or not self.profile.is_empty() or not self.persona.is_empty()
        )


@dataclass
class RawEvidence:
    """What a source returns before synthesis: text, citations, provenance.

    Sources deliberately do NOT produce an `AttendeeIntel` themselves — a
    licensed API returns structured JSON while grounded search returns prose,
    and forcing both into the final shape at the source would put extraction
    logic in two places. They hand back evidence; `persona.py` does the one
    extraction pass.
    """

    text: str = ""
    citations: list[str] = field(default_factory=list)
    provider: str = ""
    structured: dict[str, Any] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not (self.text.strip() or self.structured)

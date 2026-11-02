"""Source contract + selection for attendee intelligence.

A source answers one question: *what public evidence exists about this person?*
It returns `RawEvidence` (text and/or structured payload + citation URLs) and
never the finished `AttendeeIntel` — synthesis happens once, in ``persona.py``,
so the two sources cannot drift in how they interpret what they found.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Protocol

from rubik_core.people import config
from rubik_core.people.schemas import RawEvidence

logger = logging.getLogger(__name__)

# Generic mailbox local-parts that identify a shared inbox, not a person.
# Researching "info@acme.com" wastes a grounded call and, worse, invents a
# persona for a distribution list.
_ROLE_LOCALS = frozenset(
    {
        "info",
        "sales",
        "support",
        "help",
        "hello",
        "contact",
        "admin",
        "billing",
        "accounting",
        "ap",
        "ar",
        "noreply",
        "no-reply",
        "donotreply",
        "notifications",
        "team",
        "office",
        "marketing",
        "careers",
        "hr",
        "legal",
        "compliance",
        "invoices",
        "orders",
        "webmaster",
        "postmaster",
        "abuse",
        "security",
    }
)

# Calendar rooms/resources land in `attendees` looking like people.
_RESOURCE_HINTS = ("resource.calendar.google.com", "calendar.google.com")


@dataclass
class PersonQuery:
    """Who we are researching, and the hints that disambiguate them.

    ``company_hint`` matters more than it looks: "Pat Lee, VP" is thousands
    of people, while "Pat Lee at Globex" is one. It is derived from the
    email domain, so it is available for every external attendee without any
    extra lookup.
    """

    email: str
    name: str = ""
    company_hint: str = ""
    title_hint: str = ""

    @property
    def domain(self) -> str:
        return self.email.split("@")[-1].lower() if "@" in self.email else ""

    def display_name(self) -> str:
        """The best human name we have — falls back to a titled local-part."""
        if self.name.strip():
            return self.name.strip()
        local = self.email.split("@")[0] if "@" in self.email else self.email
        return re.sub(r"[._\-+]+", " ", local).strip().title()

    def is_researchable(self) -> bool:
        """False for shared mailboxes, calendar resources and junk entries.

        Fail-closed on anything that does not look like one human being: an
        invented persona for `billing@` is worse than a missing card.
        """
        email = (self.email or "").strip().lower()
        if "@" not in email or email.count("@") != 1:
            return False
        local, _, domain = email.partition("@")
        if not local or not domain or "." not in domain:
            return False
        if any(hint in domain for hint in _RESOURCE_HINTS):
            return False
        # Strip a +suffix before the role check so "sales+acme@" is still caught.
        base_local = local.split("+", 1)[0]
        if base_local in _ROLE_LOCALS:
            return False
        return True


class PersonSource(Protocol):
    """Anything that can produce public evidence about a person."""

    name: str

    def fetch(self, query: PersonQuery) -> RawEvidence:  # pragma: no cover - protocol
        ...


@dataclass
class SourcePlan:
    """The ordered sources to try for one run, best fidelity first."""

    sources: list[PersonSource] = field(default_factory=list)

    def names(self) -> list[str]:
        return [s.name for s in self.sources]


def resolve_source() -> SourcePlan:
    """Pick the sources available in this environment, best fidelity first.

    The licensed API is tried first when a key exists and grounded search is
    always kept as the fallback, so a provider outage or an out-of-credit
    account degrades to a thinner card instead of an empty one.
    """
    from rubik_core.people.sources.linkedin_api import LinkedInApiSource
    from rubik_core.people.sources.web_search import WebSearchSource

    sources: list[PersonSource] = []
    if config.linkedin_api_key():
        sources.append(LinkedInApiSource())
    sources.append(WebSearchSource())
    return SourcePlan(sources=sources)

"""Evidence sources for attendee intelligence.

Two, in preference order:

* ``linkedin_api`` — a licensed LinkedIn-data API (Proxycurl-compatible).
  Structured profile + post payloads, the highest fidelity available. Inert
  unless ``LINKEDIN_DATA_API_KEY`` is configured.
* ``web_search`` — Gemini with Google Search grounding over the public web,
  including public LinkedIn profiles and posts Google has indexed. The default,
  and the reason this feature needs no new vendor to ship.

There is deliberately no third option that logs into linkedin.com and parses
HTML. See ``docs/uc47_attendee_intelligence.md``.
"""

from rubik_core.people.sources.base import PersonQuery, resolve_source
from rubik_core.people.sources.linkedin_api import LinkedInApiSource
from rubik_core.people.sources.web_search import WebSearchSource

__all__ = [
    "LinkedInApiSource",
    "PersonQuery",
    "WebSearchSource",
    "resolve_source",
]

"""UC-47 — attendee intelligence: who is in the room, and how to talk to them.

Meeting prep knew a meeting's attendees as an email and a display name. This
package turns each external attendee into a researched person: their position and
career trajectory, the posts and talks they have actually published, and a read on
their communication and decision style built from that evidence — so a rep opens
with something the person said last month instead of a generic pleasantry.

Layout:

* ``schemas`` — the typed bundle (`AttendeeIntel`) that crosses every boundary.
* ``sources/`` — where evidence comes from: grounded public-web search (default),
  or a licensed LinkedIn-data API when a key is configured.
* ``persona`` — the prompts, and the single place model JSON becomes a bundle.
* ``store`` — the shared, TTL'd cache (`attendee_profiles`).
* ``service`` — ``research_attendees()``, the entry point meeting prep calls.
* ``prompt`` — rendering the bundle into a prompt block.

The one design decision worth knowing up front: **nothing here scrapes
linkedin.com.** Grounded search reads what Google has already indexed of public
profiles and posts, and the optional API path is licensed. Rationale and the
per-field grounding rules are in ``docs/uc47_attendee_intelligence.md``.
"""

from rubik_core.people.schemas import (
    ActivityItem,
    AttendeeIntel,
    PersonaRead,
    PersonProfile,
)
from rubik_core.people.service import AttendeeIntelResult, research_attendees

__all__ = [
    "ActivityItem",
    "AttendeeIntel",
    "AttendeeIntelResult",
    "PersonProfile",
    "PersonaRead",
    "research_attendees",
]

"""Canonical node identity for the relationship graph.

Every node key is ``f"{kind}:{id}"`` — the same convention as
``ChannelSignal.entity_key`` (``rubik_core/priorities/models.py``), so priorities
signals join against the graph for free. The node vocabulary IS ``EntityKind``
— the constants below are its values, not a second vocabulary (see the note at
the end of this docstring for why they are spelled out rather than imported).

Normalization lives here and ONLY here. Two writers that lowercase an email
differently silently create two nodes for one person, which is the failure mode
that makes a knowledge graph worse than no graph at all — so every writer must
route through these helpers rather than formatting keys itself.

Keyspace warning (carried from ``backend/signals/correlator.py``): a transcript's
meeting id and a Google Calendar ``event_id`` are DIFFERENT keyspaces. TRANSCRIPT
and EVENT nodes are never merged or cross-keyed.

Why the kind strings are spelled out below instead of imported from
``EntityKind``: importing ``rubik_core.priorities.models`` initializes the whole
``priorities`` package (its ``__init__`` pulls in the orchestrator and every
channel agent), and the calendar agent is itself a graph WRITER — so a module-
scope import here would close an import cycle the moment that writer lands.
These constants are the same vocabulary, bound late; ``test_graph_keys.py``
asserts each one equals its ``EntityKind`` member, so a divergence fails a test
rather than rotting silently.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # hints only — never imported at runtime (see module docstring)
    from rubik_core.priorities.models import EntityKind

KIND_PERSON = "person"
KIND_COMPANY = "company"
KIND_DEAL = "deal"
KIND_EVENT = "event"
KIND_TRANSCRIPT = "transcript"

# Mail hosts we must never fold into a "company" node: a gmail.com address says
# nothing about an employer, and a company node for it would connect unrelated
# people into one fictitious organization.
#
# The same list decides whether a calendar attendee's domain is worth a HubSpot
# company lookup (``call_capture/domain_match.py``), so a miss here is not merely
# a bad graph node — it is a CRM search for "cox.net" that comes back with
# somebody else's company and gets prefilled into a sales wrap-up. The US ISP
# tail below is there because many small-business contacts still use it.
_GENERIC_EMAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "hotmail.com",
        "outlook.com",
        "live.com",
        "aol.com",
        "icloud.com",
        "me.com",
        "msn.com",
        "protonmail.com",
        "proton.me",
        "comcast.net",
        "verizon.net",
        "sbcglobal.net",
        # US ISP / legacy consumer tail.
        "att.net",
        "cox.net",
        "bellsouth.net",
        "charter.net",
        "earthlink.net",
        "mac.com",
        "ymail.com",
        # Free international / privacy hosts seen on client invites.
        "gmx.com",
        "zoho.com",
        "fastmail.com",
        "yandex.com",
        "qq.com",
        "163.com",
    }
)


def entity_key(kind: EntityKind | str, entity_id: str) -> str:
    """``"{kind}:{id}"`` for an already-normalized id. Raises on an empty id.

    Accepts an ``EntityKind`` or its plain string value, so callers that already
    hold the enum (priorities code) and callers that must not import it (this
    package's own helpers) share one implementation.
    """
    kind_value = getattr(kind, "value", kind)
    ident = (entity_id or "").strip()
    if not ident:
        raise ValueError(f"empty entity_id for kind {kind_value}")
    return f"{kind_value}:{ident}"


def parse_key(key: str) -> tuple[str, str]:
    """Split ``"person:a@b.com"`` → ``("person", "a@b.com")``.

    Splits on the FIRST colon only — ids may contain colons (some Google
    Calendar event ids do).
    """
    kind, _, ident = (key or "").partition(":")
    return kind, ident


def normalize_email(email: str | None) -> str | None:
    """Lowercased, trimmed address, or None when it isn't one."""
    value = (email or "").strip().lower()
    if not value or "@" not in value:
        return None
    return value


def normalize_domain(domain: str | None) -> str | None:
    """Lowercased registrable-ish domain, or None.

    Accepts a bare domain, a URL, or an email address. Strips scheme, path, any
    leading ``www.``, and a trailing dot.
    """
    value = (domain or "").strip().lower()
    if not value:
        return None
    if "@" in value:
        value = value.rsplit("@", 1)[-1]
    if "//" in value:
        value = value.split("//", 1)[-1]
    value = value.split("/", 1)[0].split("?", 1)[0].strip().strip(".")
    if value.startswith("www."):
        value = value[4:]
    if not value or "." not in value:
        return None
    return value


def person_key(email: str | None) -> str | None:
    """PERSON node for an email address. None when the address is unusable."""
    normalized = normalize_email(email)
    return entity_key(KIND_PERSON, normalized) if normalized else None


def company_key(domain: str | None) -> str | None:
    """COMPANY node for an email domain. None for generic mail hosts.

    A company node keyed on gmail.com would merge every unrelated freelancer
    into one organization, so those are dropped rather than normalized.
    """
    normalized = normalize_domain(domain)
    if not normalized or normalized in _GENERIC_EMAIL_DOMAINS:
        return None
    return entity_key(KIND_COMPANY, normalized)


def company_key_for_email(email: str | None) -> str | None:
    """COMPANY node implied by an email address's domain."""
    return company_key(normalize_email(email))


def deal_key(deal_id: str | int | None) -> str | None:
    """DEAL node for a HubSpot deal id."""
    ident = str(deal_id or "").strip()
    return entity_key(KIND_DEAL, ident) if ident else None


def transcript_key(doc_id: str | None) -> str | None:
    """TRANSCRIPT node for a transcript ``doc_id`` (never a calendar event id)."""
    ident = (doc_id or "").strip()
    return entity_key(KIND_TRANSCRIPT, ident) if ident else None


def event_key(event_id: str | None) -> str | None:
    """EVENT node for a Google Calendar event id (never a transcript id)."""
    ident = (event_id or "").strip()
    return entity_key(KIND_EVENT, ident) if ident else None


def is_generic_email_domain(domain: str | None) -> bool:
    """True for consumer mail hosts that must never become COMPANY nodes."""
    normalized = normalize_domain(domain)
    return bool(normalized and normalized in _GENERIC_EMAIL_DOMAINS)

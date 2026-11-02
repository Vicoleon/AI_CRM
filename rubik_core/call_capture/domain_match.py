"""Which company is this calendar meeting with? — composition, not new policy.

The calendar call-capture screen's primary job is to bind a meeting to the right
HubSpot company *before* the call, because that binding is what lets the wrap-up
write to the right CRM record without asking. This module is the one place that
turns a raw Google Calendar event into the domains worth asking HubSpot about.

It deliberately owns **no lists of its own**. Four blocklists already existed in
this repo before this feature and had already diverged from one another; a fifth
would be the bug, not the fix. So:

* consumer mail hosts → ``graph.keys.is_generic_email_domain`` (the ISP tail
  ``att.net``/``cox.net``/… was added *there*, with the rest, not here);
* reserved/placeholder addresses → ``database.repository.is_placeholder_email``;
* calendar rooms and resources → ``people.sources.base._RESOURCE_HINTS``;
* role mailboxes → ``people.sources.base.PersonQuery.is_researchable``.

The one rule that is easy to get wrong: **a role mailbox does not suppress the
company match.** ``sales@acme.com`` still says Acme — Acme is who the meeting is
with. ``_ROLE_LOCALS`` answers a different question ("is there a *person* here
worth researching?"), and the two decisions are kept apart on purpose:
``is_company_matchable`` never consults it.

The second rule: **a blocked domain suppresses the MATCH, never the ROW.** A
client dialling in from ``gmail.com`` is still an external attendee; the screen
renders "attendee on a personal address — pick the company", never "internal"
and never "no external attendees". That is why ``external_domains_for_event``
returns only *matchable* domains while the caller keeps deciding externality
from ``own_domains`` — an empty list here means "nothing to look up", not
"nobody external".
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from hashlib import sha256

from rubik_core.database.repository import is_placeholder_email
from rubik_core.graph.keys import (
    is_generic_email_domain,
    normalize_domain,
    normalize_email,
)
from rubik_core.people.sources.base import _RESOURCE_HINTS
from rubik_core.retrieval.hubspot_api import (
    DomainMatch,
    HubSpotAPIClient,
    HubSpotAPIError,
    _workspace_deal_pipelines,
)
from rubik_core.tenancy.context import NoWorkspaceContext, current_workspace
from rubik_core.tenancy.secrets import workspace_secret
from rubik_core.xray.fetchers import _with_backoff

logger = logging.getLogger(__name__)

# Google's own pseudo-domains. The `resource` flag on an attendee catches most
# rooms, but NOT `*@group.calendar.google.com` (a secondary calendar invited as
# an attendee) and not a Google Group — both arrive looking like people, and both
# would become a HubSpot search for a domain no client will ever own.
# `_RESOURCE_HINTS` is folded in so the room list stays single-sourced.
_PSEUDO_DOMAIN_SUFFIXES: tuple[str, ...] = tuple(
    sorted({*_RESOURCE_HINTS, "calendar.google.com", "googlegroups.com"})
)

# (workspace, domain) -> (verdict, resolved_at, read-token fingerprint).
# Positive matches are always checked live: both the company's deal ownership
# and the attendee domain can change without this process knowing. A MISS is
# cached for minutes only, because the natural response to "no company
# for globex.example" is for the AE to go and create it, and a 24h negative
# entry would hide their own work from them. An `error` is never cached at all:
# remembering a 429 as "no company" is exactly the silent wrong answer this
# whole design exists to prevent.
_MATCH_CACHE: dict[tuple[str, str], tuple[DomainMatch, datetime, str]] = {}
_NEGATIVE_TTL_S = 15 * 60


def clear_match_cache() -> None:
    """Drop this workspace's verdicts, or all entries at process/test reset."""
    try:
        workspace = current_workspace()
    except NoWorkspaceContext:
        _MATCH_CACHE.clear()
        return
    for key in [key for key in _MATCH_CACHE if key[0] == workspace]:
        _MATCH_CACHE.pop(key, None)


def own_domains(ae_email: str | None = None) -> frozenset[str]:
    """Domains that mean "us", never a client.

    All verified workspace domains and the caller's own email domain. The
    caller's domain protects a local draft if control data is temporarily
    missing; verified secondary domains remain internal for every rep.
    """
    from rubik_core.company.domain import workspace_domains

    out = set(workspace_domains())
    mine = normalize_domain(ae_email)  # accepts a full address
    if mine:
        out.add(mine)
    return frozenset(out)


def is_company_matchable(domain: str | None, *, own: frozenset[str]) -> bool:
    """Is a HubSpot company lookup for ``domain`` worth doing — and safe?

    False for anything that cannot identify an organization: our own domains,
    consumer mail hosts, reserved/example domains, and Google's calendar
    pseudo-domains. Every one of those, searched against HubSpot, risks a
    confident match on somebody else's company record.

    Deliberately does NOT look at the mailbox local-part: ``sales@acme.com`` is
    still Acme.
    """
    normalized = normalize_domain(domain)
    if not normalized:
        return False
    if normalized in own:
        return False
    if is_generic_email_domain(normalized):
        return False
    if is_placeholder_email(f"nobody@{normalized}"):
        return False
    return not _is_pseudo_domain(normalized)


def external_domains_for_event(event: dict, *, ae_email: str) -> tuple[list[str], bool]:
    """``(matchable external domains, attendees_truncated)`` for one raw event.

    Takes the RAW Google event on purpose: ``attendees[].resource`` lives on the
    attendee object, not on the address, so a list of email strings has already
    thrown away the only reliable way to tell a conference room from a client. A
    room address read as a company is a wrong CRM write.

    The second element is Google's ``attendeesOmitted``, which this repo handled
    nowhere before. Google sets it when it truncates a large invite, so the one
    external attendee we can see may not be the only one — the caller must force
    the company decision to *ambiguous* when it is True rather than binding the
    meeting to whoever survived the truncation.

    An empty list does NOT mean "internal meeting": personal-address and
    role-only invites are still external. Externality is the caller's call, from
    ``own_domains``; this returns only what is worth asking HubSpot about.
    """
    own = own_domains(ae_email)
    me = normalize_email(ae_email)
    domains: set[str] = set()
    for attendee in event.get("attendees") or []:
        if not isinstance(attendee, dict):
            continue
        if attendee.get("resource") is True:
            continue
        email = normalize_email(attendee.get("email"))
        if not email:
            continue
        if attendee.get("self") is True or (me and email == me):
            continue
        domain = normalize_domain(email)
        if not is_company_matchable(domain, own=own):
            continue
        if domain:  # narrowing only — is_company_matchable already proved it
            domains.add(domain)
    return sorted(domains), bool(event.get("attendeesOmitted"))


def resolve_domain_matches(
    domains: Sequence[str],
    *,
    ae_email: str | None = None,
    client: HubSpotAPIClient | None = None,
) -> dict[str, DomainMatch]:
    """Cached, blocklist-gated ``domain -> DomainMatch`` for a day's meetings.

    Returns an entry for every *matchable* domain asked for. A domain that is
    not matchable is simply absent — the caller renders that as company match
    ``"skipped"`` (use ``is_company_matchable`` to tell "skipped" from
    "not_found"), and it never reaches HubSpot at all.

    A HubSpot outage yields ``DomainMatch.error`` per domain, which the screen
    shows as "couldn't check" and never caches. Missing workspace context raises
    before any cached verdict or CRM request can be used.
    """
    workspace = current_workspace()
    own = own_domains(ae_email)
    valid_domains: list[str] = []
    for raw in domains or []:
        domain = normalize_domain(raw)
        if not domain or domain in valid_domains:
            continue
        if not is_company_matchable(domain, own=own):
            continue
        valid_domains.append(domain)

    if not valid_domains:
        return {}
    try:
        pipeline_ids = sorted(_workspace_deal_pipelines())
    except Exception:  # noqa: BLE001 — a cached miss is not proof when policy is unavailable
        return {
            domain: DomainMatch(
                domain=domain, error="HubSpot workspace pipelines unavailable"
            )
            for domain in valid_domains
        }
    try:
        token = workspace_secret("hubspot", "read_token")
    except Exception:  # noqa: BLE001 — a failed control/secret read is unavailable
        token = None
    if not isinstance(token, str) or not token.strip():
        return {
            domain: DomainMatch(
                domain=domain, error="HubSpot read credential unavailable"
            )
            for domain in valid_domains
        }
    token = token.strip()
    if client is not None and getattr(client, "token", None) != token:
        return {
            domain: DomainMatch(
                domain=domain,
                error="HubSpot client credential does not match workspace",
            )
            for domain in valid_domains
        }
    token_hash = sha256((token + "\0" + "\0".join(pipeline_ids)).encode()).hexdigest()

    now = datetime.now(timezone.utc)
    out: dict[str, DomainMatch] = {}
    to_fetch: list[str] = []
    for domain in valid_domains:
        cached = _cached(workspace, domain, token_hash, now)
        if cached is not None:
            out[domain] = cached
        else:
            to_fetch.append(domain)

    if not to_fetch:
        return out

    try:
        hubspot = client or HubSpotAPIClient(token=token)
    except Exception as exc:  # noqa: BLE001 — same contract as the fetch below
        logger.info("HubSpot not configured for domain matching: %s", exc)
        message = str(exc)[:200]
        return {**out, **{d: DomainMatch(domain=d, error=message) for d in to_fetch}}

    fetched = _fetch_with_backoff(hubspot, to_fetch)
    for domain, match in fetched.items():
        out[domain] = match
        _remember(workspace, domain, match, token_hash, now)
    # Defensive: a client that somehow omitted a domain must not read as a miss.
    for domain in to_fetch:
        out.setdefault(
            domain, DomainMatch(domain=domain, error="no verdict returned by HubSpot")
        )
    return out


def _fetch_with_backoff(
    client: HubSpotAPIClient, domains: list[str]
) -> dict[str, DomainMatch]:
    """``companies_for_domains`` with the repo's existing 429/5xx backoff.

    ``companies_for_domains`` never raises, which is right for the screen but
    means a rate-limit can't be seen by ``_with_backoff``. So we re-raise the
    batch's own error message for it to judge (it decides transience, no second
    copy of that logic here) and hand back the last attempt's verdicts if the
    retries are exhausted — errors intact, so the row says why.
    """
    last: dict[str, DomainMatch] = {}

    def _call() -> dict[str, DomainMatch]:
        result = client.companies_for_domains(domains)
        last.clear()
        last.update(result)
        failure = next((m.error for m in result.values() if m.error), None)
        if failure:
            raise HubSpotAPIError(failure)
        return result

    try:
        return _with_backoff(_call)
    except Exception as exc:  # noqa: BLE001 — see below; the breadth is the point
        # Deliberately broad, and NOT the usual over-catch. ``resolve_domain_matches``
        # documents "never raises", and the whole reason it can promise that is this
        # handler: a CRM enrichment failure must degrade one row's company block to
        # "couldn't check", never blank the AE's whole day. Catching only
        # HubSpotAPIError kept that promise for the failures we predicted and broke it
        # for every other one — a transport error escaping httpx, a client stub in a
        # test, a bug in this module. Those are exactly the cases where an unhandled
        # exception would take out ``GET /scheduled`` with a 500, and a 500 here is a
        # blank calendar that an AE reads as "no calls today".
        if last:
            return last
        message = str(exc)[:200]
        logger.warning(
            "HubSpot domain lookup failed for %d domains: %s", len(domains), message
        )
        return {d: DomainMatch(domain=d, error=message) for d in domains}


def _cached(
    workspace: str, domain: str, token_hash: str, now: datetime
) -> DomainMatch | None:
    key = (workspace, domain)
    hit = _MATCH_CACHE.get(key)
    if hit is None:
        return None
    match, resolved_at, cached_token_hash = hit
    if match.candidates:
        _MATCH_CACHE.pop(key, None)
        return None
    if cached_token_hash != token_hash:
        _MATCH_CACHE.pop(key, None)
        return None
    if (now - resolved_at).total_seconds() > _NEGATIVE_TTL_S:
        _MATCH_CACHE.pop(key, None)
        return None
    return match


def _remember(
    workspace: str, domain: str, match: DomainMatch, token_hash: str, now: datetime
) -> None:
    if match.error or match.candidates:
        return  # failure or positive match requires a fresh CRM check
    _MATCH_CACHE[(workspace, domain)] = (match, now, token_hash)


def _is_pseudo_domain(domain: str) -> bool:
    return any(
        domain == suffix or domain.endswith(f".{suffix}")
        for suffix in _PSEUDO_DOMAIN_SUFFIXES
    )

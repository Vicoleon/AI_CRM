"""Expand Google Group invitees on a calendar event into the people behind them.

Why this exists. An L10's calendar invite can carry a Google Group (an
"Exec Team" group, say). Google Calendar's UI shows the group expanded,
but the Calendar API returns a group as ONE attendee carrying the group's
address and never lists its members. The L10 import copied that attendee
list verbatim into ``eos_l10_meetings.roster_emails``, and the in-meeting
Check-in picker is scoped to that roster, so anyone invited only through
the group could not be picked. Sync re-read the same unexpanded group,
which is why it never healed on its own.

This module asks the Admin Directory API, under the same domain-wide
delegation the Workspace role sync already uses (``admin.directory.group
.readonly``), for the members of each address that is not already a known
user, and replaces the group with its people. It is deliberately
best-effort: Directory unavailable, scope not granted, a 5xx, a nested
group too deep — every failure leaves THAT address exactly as the calendar
gave it and the import proceeds, which is today's behavior. It never raises.

Kill switch: ``settings.EOS_L10_EXPAND_GROUP_INVITEES``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from rubik_core.config import settings

logger = logging.getLogger(__name__)

GROUP_SCOPES = ["https://www.googleapis.com/auth/admin.directory.group.readonly"]

#: A group inside a group is a real pattern (team groups nested in an
#: all-hands group); deeper than this is not a meeting roster.
MAX_DEPTH = 2
_PAGE_SIZE = 200


@dataclass
class GroupExpansion:
    """What :func:`expand_group_invitees` did, for logging and tests."""

    roster: list[str]
    #: group address -> the member emails that replaced it
    expanded: dict[str, list[str]] = field(default_factory=dict)
    #: addresses asked about (not groups, or failed) — kept as given
    probed: list[str] = field(default_factory=list)
    #: addresses whose lookup FAILED (not "not a group") — kept as given
    failed: list[str] = field(default_factory=list)


def _build_service():
    """Directory API client delegated to the current workspace admin subject."""
    from rubik_core.tools._workspace_auth import (
        load_workspace_credentials,
        workspace_dwd_subject,
    )

    subject = workspace_dwd_subject()
    if not subject:
        return None
    creds = load_workspace_credentials(GROUP_SCOPES)
    if creds is None:
        return None
    from googleapiclient.discovery import build

    return build(
        "admin",
        "directory_v1",
        credentials=creds.with_subject(subject),
        cache_discovery=False,
    )


def _http_status(exc: BaseException) -> int | None:
    """Status code of a googleapiclient ``HttpError`` (``exc.resp.status``) or
    of anything that quacks like one; ``None`` for other exceptions."""
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _direct_members(service, group_email: str) -> list[dict] | None:
    """Direct members of ``group_email`` across every page, or ``None`` when
    the Directory says it is not a group (404 — which is also what a plain
    user's or an outside guest's address gets). Other errors propagate."""
    members: list[dict] = []
    token: str | None = None
    while True:
        request = service.members().list(
            groupKey=group_email, maxResults=_PAGE_SIZE, pageToken=token
        )
        try:
            page = request.execute() or {}
        except Exception as exc:  # noqa: BLE001 — classify, re-raise the rest
            if _http_status(exc) == 404:
                return None
            raise
        members.extend(page.get("members") or [])
        token = page.get("nextPageToken")
        if not token:
            return members


def _people_in(
    service,
    group_email: str,
    *,
    depth: int,
    visited: set[str],
) -> list[str] | None:
    """Emails of the PEOPLE in a group, nested groups included up to
    ``MAX_DEPTH``. ``None`` when ``group_email`` is not a group."""
    members = _direct_members(service, group_email)
    if members is None:
        return None
    visited.add(group_email)
    people: list[str] = []
    for m in members:
        email = (m.get("email") or "").strip().lower()
        kind = (m.get("type") or "USER").upper()
        if not email:
            continue
        if kind == "USER":
            # A suspended account is an ex-employee; not someone to pick.
            if (m.get("status") or "ACTIVE").upper() == "SUSPENDED":
                continue
            people.append(email)
        elif kind == "GROUP":
            if email in visited:
                continue
            if depth >= MAX_DEPTH:
                logger.info(
                    "[l10-group-invitees] %s nests %s beyond depth %d; kept as an address",
                    group_email,
                    email,
                    MAX_DEPTH,
                )
                people.append(email)
                continue
            nested = _people_in(service, email, depth=depth + 1, visited=visited)
            people.extend(nested if nested is not None else [email])
        # CUSTOMER ("everyone in the domain") is not a roster; dropped.
    return people


def expand_group_invitees(
    emails: Iterable[str],
    *,
    skip: Iterable[str] = (),
    service_factory: Callable[[], object | None] | None = None,
) -> GroupExpansion:
    """Return ``emails`` with every Google Group replaced by its members.

    Order is preserved (members take the group's slot), everything is
    lower-cased and de-duplicated. Addresses in ``skip`` — the caller passes
    the emails of known users — are never asked about, so a normal import
    costs one Directory call per unknown address, not per attendee. Never
    raises: on any failure the offending address is kept as given.
    """
    normalized: list[str] = []
    seen: set[str] = set()
    for e in emails or []:
        key = (e or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            normalized.append(key)
    result = GroupExpansion(roster=list(normalized))

    if not settings.EOS_L10_EXPAND_GROUP_INVITEES:
        return result
    skip_keys = {(s or "").strip().lower() for s in skip}
    candidates = [e for e in normalized if e not in skip_keys]
    if not candidates:
        return result

    factory = service_factory or _build_service
    try:
        service = factory()
    except Exception as exc:  # noqa: BLE001 — never fail the import
        logger.warning("[l10-group-invitees] Directory client unavailable: %s", exc)
        result.failed.extend(candidates)
        return result
    if service is None:
        logger.info(
            "[l10-group-invitees] Directory not configured; roster kept as given"
        )
        return result

    expanded_roster: list[str] = []
    out_seen: set[str] = set()

    def _push(email: str) -> None:
        if email not in out_seen:
            out_seen.add(email)
            expanded_roster.append(email)

    for e in normalized:
        if e not in skip_keys:
            result.probed.append(e)
            try:
                people = _people_in(service, e, depth=0, visited=set())
            except Exception as exc:  # noqa: BLE001 — keep the address, move on
                logger.warning(
                    "[l10-group-invitees] lookup failed for %s; kept as given: %s",
                    e,
                    exc,
                )
                result.failed.append(e)
                people = None
            if people is not None:
                result.expanded[e] = people
                for p in people:
                    _push(p)
                continue
        _push(e)

    result.roster = expanded_roster
    if result.expanded:
        logger.info(
            "[l10-group-invitees] expanded %s",
            {g: len(ms) for g, ms in result.expanded.items()},
        )
    return result

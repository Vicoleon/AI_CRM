"""One HubSpot index per morning run, shared by every AE (README §4.3).

The scan asks HubSpot the same few questions for every AE — is this address a
contact, is this domain a client, has anything been logged on this record since
that email — so the answers are fetched ONCE per run, for everyone, and looked
up in memory. A per-message HubSpot read would be thousands of calls a morning
and would 429 long before the last AE.

What the index holds:

* **Owners** — ``owner_directory()``, AE email → HubSpot owner id. An AE whose
  Workspace address matches no owner gets coverage ``no_owner``: the
  record-centric checks still run, their HubSpot-task leg does not.
* **Contacts** for external addresses AEs exchanged mail with, searched
  ``email IN [...]`` in chunks. Only contacts linked to a live owned deal or
  wholly owned company enter the index.
* **Client domains** — for addresses that are not contacts,
  ``companies_for_domains``. A company counts as a client only if it has a deal
  in one of the workspace's own configured pipelines:
  the portal also hosts a client's own pipeline, and a vendor emailing an AE is
  not a client loose end. An ambiguous domain (two companies claim it) is not a
  match — a human would have to pick, so the scan does not guess.
* **The engagement index** — emails, calls, meetings and notes in the portal
  with ``hs_timestamp`` since ``window_start``, searched in 7-day slices, then
  kept only after an owned-deal or wholly owned company association is proved:
  ``"contact:123" | "company:9" | "deal:77" → [Engagement]``. A slice that
  comes back at the 5,000-record search cap marks the index ``partial`` and
  records WHICH window was cut, so an "unlogged" finding is refused only where
  missing evidence could be the cap's fault.
* **AE tasks** — each owner's not-completed tasks due 3+ days ago (and not
  older than the max age) after the same ownership proof. Open ``hstask:``
  ids are revalidated before their status, due date or owner is read.
* **AE open deals** — for the Slack leg: the companies on each AE's open
  deals are the names a Slack message is matched against.

**Any failure is HubSpot "unavailable" for everyone** — no unlogged findings
and no HubSpot-based closures this run. An empty owner directory counts as a
failure: ``owner_directory`` swallows its own errors and returns ``{}``, and a
portal with no owners is not a state this company is ever in.

Every read retries 429 / 5xx / transport errors (``rhythm_checks._retry``),
with one extra, longer pause for a 429 that outlasts it — the run is a
background job and waiting two seconds is cheaper than losing the morning.

When the shared portal returns activities whose ownership cannot be proved,
the index marks itself partial and declines negative "nothing logged" claims.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

ENGAGEMENT_TYPES: tuple[str, ...] = ("emails", "calls", "meetings", "notes")
SLICE_DAYS = 7
#: Values per ``IN`` filter. HubSpot accepts 100; half leaves headroom for
#: long addresses in the request body.
IN_CHUNK = 50
#: deal → company association type 5 is "Primary".
_PRIMARY_DEAL_COMPANY = 5


class HubSpotIndexError(RuntimeError):
    """The index could not be built; HubSpot is "unavailable" this run."""


@dataclass(frozen=True)
class Engagement:
    """One logged activity. ``at`` is aware UTC."""

    at: datetime
    type: str  # emails | calls | meetings | notes
    id: str
    # An INCOMING_EMAIL is the client's own message, logged. It shows the
    # thread is tracked (enough for "unlogged"), but it is not the AE
    # following up, so the open-loop check ignores it.
    incoming: bool = False

    def evidence(self) -> dict:
        """Ids and a timestamp only — what ``close_evidence`` may hold."""
        return {
            "type": self.type,
            "id": self.id,
            "at": self.at.astimezone(timezone.utc)
            .replace(tzinfo=None)
            .isoformat(timespec="seconds"),
        }


@dataclass
class Contact:
    id: str
    email: str
    name: str = ""
    company_ids: list[str] = field(default_factory=list)
    deal_ids: list[str] = field(default_factory=list)


@dataclass
class Company:
    id: str
    name: str = ""
    domain: str = ""
    deal_ids: list[str] = field(default_factory=list)  # own-pipeline deals only


@dataclass
class Deal:
    id: str
    name: str = ""
    owner_id: str = ""
    pipeline: str = ""
    company_ids: list[str] = field(default_factory=list)


@dataclass
class Task:
    id: str
    owner_id: str
    subject: str
    due: datetime | None  # aware UTC
    contact_ids: list[str] = field(default_factory=list)
    company_ids: list[str] = field(default_factory=list)
    deal_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Match:
    """What HubSpot knows about one external address."""

    kind: str  # "contact" | "domain"
    contact_id: str | None
    company_id: str | None
    deal_id: str | None
    label: str | None  # the contact's name, when it is a contact
    company_label: str | None
    records: tuple[str, ...]  # engagement-index keys to check


@dataclass
class HubSpotIndex:
    status: str = "ok"  # ok | partial | unavailable
    window_start: datetime | None = None
    owners: dict[str, str] = field(default_factory=dict)  # email → owner id
    contacts: dict[str, Contact] = field(default_factory=dict)  # email → Contact
    companies: dict[str, Company] = field(default_factory=dict)
    domain_company: dict[str, str] = field(default_factory=dict)  # client domains
    deals: dict[str, Deal] = field(default_factory=dict)  # own pipelines only
    engagements: dict[str, list[Engagement]] = field(default_factory=dict)
    # Windows (start, end) whose search hit the record cap.
    capped: list[tuple[datetime, datetime]] = field(default_factory=list)
    # Some portal activities cannot be tied to a live owned deal or a wholly
    # owned company. Such omissions must not become "nothing was logged".
    scope_limited: bool = False
    failed_domains: set[str] = field(default_factory=set)
    tasks: dict[str, list[Task]] = field(default_factory=dict)  # owner id → tasks
    task_states: dict[str, str] = field(default_factory=dict)  # open|completed
    # For tracked (open ``hstask:``) tasks: the due date and owner HubSpot holds
    # NOW, so a task moved to next week or reassigned leaves the AE's list.
    task_due: dict[str, datetime | None] = field(default_factory=dict)
    task_owner: dict[str, str] = field(default_factory=dict)
    owner_open_deals: dict[str, list[str]] = field(default_factory=dict)
    error: str | None = None

    # ── availability ────────────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        return self.status != "unavailable"

    @classmethod
    def unavailable(cls, error: str) -> HubSpotIndex:
        return cls(status="unavailable", error=error)

    def owner_id(self, email: str) -> str | None:
        return self.owners.get((email or "").strip().lower())

    def evidence_may_be_missing(self, since: datetime) -> bool:
        """True when a capped or unprovable activity may hide log evidence."""
        return self.scope_limited or any(end > since for _start, end in self.capped)

    # ── lookups ─────────────────────────────────────────────────────────────

    def company_name(self, company_id: str | None) -> str | None:
        if not company_id:
            return None
        company = self.companies.get(str(company_id))
        return (company.name or None) if company else None

    def resolve(self, address: str) -> Match | None:
        """A contact, or someone at a client company — else None."""
        addr = (address or "").strip().lower()
        if not addr or "@" not in addr:
            return None
        contact = self.contacts.get(addr)
        if contact is not None:
            records = [f"contact:{contact.id}"]
            deal_ids: list[str] = [d for d in contact.deal_ids if d in self.deals]
            client_company: str | None = None
            for cid in contact.company_ids:
                records.append(f"company:{cid}")
                company = self.companies.get(cid)
                if company and company.deal_ids:
                    client_company = client_company or cid
                    deal_ids.extend(company.deal_ids)
            deal_ids = list(dict.fromkeys(deal_ids))
            records.extend(f"deal:{d}" for d in deal_ids)
            company_id = client_company or (
                contact.company_ids[0] if contact.company_ids else None
            )
            return Match(
                kind="contact",
                contact_id=contact.id,
                company_id=company_id,
                deal_id=deal_ids[0] if deal_ids else None,
                label=contact.name or None,
                company_label=self.company_name(company_id),
                records=tuple(dict.fromkeys(records)),
            )
        domain = addr.rsplit("@", 1)[1]
        cid = self.domain_company.get(domain)
        if cid is None:
            return None
        company = self.companies.get(cid)
        deal_ids = list(company.deal_ids) if company else []
        return Match(
            kind="domain",
            contact_id=None,
            company_id=cid,
            deal_id=deal_ids[0] if deal_ids else None,
            label=None,
            company_label=self.company_name(cid),
            records=tuple([f"company:{cid}", *(f"deal:{d}" for d in deal_ids)]),
        )

    def first_engagement(
        self,
        records: Iterable[str],
        since: datetime,
        *,
        strictly_after: bool = False,
        include_incoming: bool = True,
    ) -> Engagement | None:
        """The earliest engagement on any of ``records`` at (or after) ``since``."""
        best: Engagement | None = None
        for key in dict.fromkeys(records):
            for eng in self.engagements.get(key, ()):
                if not include_incoming and eng.incoming:
                    continue
                if eng.at < since or (strictly_after and eng.at == since):
                    continue
                if best is None or eng.at < best.at:
                    best = eng
        return best

    def ae_companies(self, owner_id: str | None) -> list[tuple[str, str, list[str]]]:
        """``(company_id, name, [deal ids])`` on the AE's open deals."""
        if not owner_id:
            return []
        by_company: dict[str, list[str]] = {}
        for did in self.owner_open_deals.get(owner_id, []):
            deal = self.deals.get(did)
            for cid in deal.company_ids if deal else []:
                by_company.setdefault(cid, []).append(did)
        return [
            (cid, self.company_name(cid) or "", dids)
            for cid, dids in by_company.items()
            if self.company_name(cid)
        ]


# ── Helpers ──────────────────────────────────────────────────────────────────


def _ms(dt: datetime) -> str:
    aware = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return str(int(aware.timestamp() * 1000))


def parse_hs_time(value: Any) -> datetime | None:
    """HubSpot sends ``hs_timestamp`` as ISO-8601 or as epoch milliseconds."""
    if value in (None, ""):
        return None
    text = str(value).strip()
    if text.isdigit():
        try:
            return datetime.fromtimestamp(int(text) / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


_HTTP_STATUS = re.compile(r"HTTP (\d{3})")


def error_label(exc: BaseException) -> str:
    """``HubSpotAPIError (HTTP 503)`` — the type and status, nothing else.

    A HubSpot error body can echo the request (a search's email filter values,
    i.e. client addresses), so the message itself is never logged here.
    """
    match = _HTTP_STATUS.search(str(exc))
    return type(exc).__name__ + (f" (HTTP {match.group(1)})" if match else "")


def _chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _allowed_pipelines() -> set[str]:
    from rubik_core.tenancy.hubspot_config import get_hubspot_config

    return set(get_hubspot_config().pipeline_ids)


def _strict_company_ids(company_ids: Iterable[str]) -> set[str]:
    """Only companies whose entire live deal set belongs to this workspace.

    Company-level names, domains and engagements mix every deal in a HubSpot
    company. The ordinary association helper filters foreign deal IDs, so it
    cannot itself prove that a company is not mixed. X-Ray's complete live
    association scan supplies that stricter proof.
    """
    from rubik_core.xray.fetchers import visible_company_ids

    return visible_company_ids(list(company_ids))


def _reader(retry: Callable[..., Any] | None) -> Callable[..., Any]:
    """``rhythm_checks._retry`` plus one longer pause for a stubborn 429."""
    if retry is None:
        from rubik_core.sales.rhythm_checks import _retry as retry

    def read(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return retry(fn, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — re-raised unless it is a 429
            if "429" not in str(exc):
                raise
            time.sleep(2.0)
            return retry(fn, *args, **kwargs)

    return read


# ── Building ─────────────────────────────────────────────────────────────────


def build_index(
    client: Any,
    *,
    owner_emails: Iterable[str],
    addresses: Iterable[str],
    window_start: datetime,
    now: datetime,
    today: date,
    task_grace_days: int,
    max_age_days: int,
    task_lookback_days: int = 30,
    open_task_ids: Iterable[str] = (),
    internal_domains: Iterable[str] = (),
    public_domains: Iterable[str] = (),
    retry: Callable[..., Any] | None = None,
) -> HubSpotIndex:
    """Build the run's index. Never raises: a failure is an ``unavailable``
    index carrying the error type, and the scan treats it accordingly."""
    try:
        return _build(
            client,
            owner_emails=[e.strip().lower() for e in owner_emails if e],
            addresses=sorted({a.strip().lower() for a in addresses if a and "@" in a}),
            window_start=window_start,
            now=now,
            today=today,
            task_grace_days=task_grace_days,
            max_age_days=max_age_days,
            task_lookback_days=task_lookback_days,
            open_task_ids=[str(t) for t in dict.fromkeys(open_task_ids) if t],
            internal=set(internal_domains),
            public=set(public_domains),
            read=_reader(retry),
        )
    except Exception as exc:  # noqa: BLE001 — "could not look" is never "all clear"
        logger.warning(
            "[loop-closure] HubSpot index unavailable this run: %s", error_label(exc)
        )
        return HubSpotIndex.unavailable(type(exc).__name__)


def _build(
    client: Any,
    *,
    owner_emails: list[str],
    addresses: list[str],
    window_start: datetime,
    now: datetime,
    today: date,
    task_grace_days: int,
    max_age_days: int,
    task_lookback_days: int,
    open_task_ids: list[str],
    internal: set[str],
    public: set[str],
    read: Callable[..., Any],
) -> HubSpotIndex:
    index = HubSpotIndex(window_start=window_start)
    allowed = _allowed_pipelines()
    if not allowed:
        raise HubSpotIndexError(
            "No HubSpot deal pipelines configured for this workspace"
        )

    # 1. Owners.
    directory = client.owner_directory() or {}
    if not directory:
        raise HubSpotIndexError("HubSpot owner directory came back empty")
    index.owners = {
        (info.get("email") or "").strip().lower(): str(oid)
        for oid, info in directory.items()
        if (info.get("email") or "").strip().lower() in owner_emails
    }
    roster_owner_ids = sorted(
        {index.owners[e] for e in owner_emails if e in index.owners}
    )

    external = [
        a
        for a in addresses
        if a.rsplit("@", 1)[1] not in internal and a not in owner_emails
    ]

    # 2. Contact candidates for every external address seen. Keep names local
    # until live deal/company ownership is proved below.
    contact_candidates: dict[str, Contact] = {}
    for chunk in _chunks(external, IN_CHUNK):
        rows = read(
            client.search_all,
            "contacts",
            [{"propertyName": "email", "operator": "IN", "values": chunk}],
            ["email", "firstname", "lastname"],
        )
        for row in rows or []:
            props = row.get("properties") or {}
            email = (props.get("email") or "").strip().lower()
            if email not in chunk or not row.get("id"):
                continue
            name = f"{props.get('firstname') or ''} {props.get('lastname') or ''}"
            contact_candidates[email] = Contact(
                id=str(row["id"]), email=email, name=name.strip()
            )
    contact_ids = [c.id for c in contact_candidates.values()]
    if contact_ids:
        try:
            strict_contacts = read(
                client.fully_owned_deal_associations, "contacts", contact_ids
            )
        except Exception:  # noqa: BLE001 — do not admit unproven contacts
            logger.warning(
                "[loop-closure] contact ownership check failed", exc_info=True
            )
            strict_contacts = {}
        if len(strict_contacts) != len(set(contact_ids)):
            index.scope_limited = True
        contact_candidates = {
            email: contact
            for email, contact in contact_candidates.items()
            if contact.id in strict_contacts
        }
        contact_ids = [c.id for c in contact_candidates.values()]
    if contact_ids:
        to_companies = read(
            client.batch_associations, "contacts", "companies", contact_ids
        )
        to_deals = read(client.batch_associations, "contacts", "deals", contact_ids)
        for contact in contact_candidates.values():
            contact.company_ids = [str(x) for x in to_companies.get(contact.id, [])]
            contact.deal_ids = [str(x) for x in to_deals.get(contact.id, [])]

    # 3. Client domains for addresses that are not contacts.
    domains = sorted(
        {
            a.rsplit("@", 1)[1]
            for a in external
            if a not in contact_candidates and a.rsplit("@", 1)[1] not in public
        }
    )
    domain_candidates: dict[str, str] = {}
    if domains:
        for dom, match in (client.companies_for_domains(domains) or {}).items():
            if getattr(match, "error", None):
                index.failed_domains.add(dom)
            elif getattr(match, "matched", False):
                domain_candidates[dom] = str(match.candidates[0].hubspot_company_id)

    # 4. Which companies have deals in our pipelines; deal pipelines and names.
    company_ids = sorted(
        {cid for c in contact_candidates.values() for cid in c.company_ids}
        | set(domain_candidates.values())
    )
    company_deals: dict[str, list[str]] = {}
    if company_ids:
        company_deals = {
            k: [str(x) for x in v]
            for k, v in read(
                client.batch_associations, "companies", "deals", company_ids
            ).items()
        }
    deal_ids = sorted(
        {d for c in contact_candidates.values() for d in c.deal_ids}
        | {d for ds in company_deals.values() for d in ds}
    )
    _read_deals(index, client, read, deal_ids, allowed)
    strict_companies = _strict_company_ids(company_ids)
    if len(strict_companies) != len(company_ids):
        index.scope_limited = True
    for cid in strict_companies:
        index.companies[cid] = Company(
            id=cid,
            deal_ids=[d for d in company_deals.get(cid, []) if d in index.deals],
        )
    for contact in contact_candidates.values():
        contact.deal_ids = [did for did in contact.deal_ids if did in index.deals]
        contact.company_ids = [
            cid for cid in contact.company_ids if cid in strict_companies
        ]
        if contact.deal_ids or contact.company_ids:
            index.contacts[contact.email] = contact
        else:
            index.scope_limited = True
    index.domain_company = {
        dom: cid
        for dom, cid in domain_candidates.items()
        if index.companies.get(cid) and index.companies[cid].deal_ids
    }

    # 5. The AEs' open deals (the Slack leg's company names).
    for chunk in _chunks(roster_owner_ids, IN_CHUNK):
        filters: list[dict] = [
            {"propertyName": "hubspot_owner_id", "operator": "IN", "values": chunk},
            {"propertyName": "hs_is_closed", "operator": "EQ", "value": "false"},
        ]
        filters.append(
            {
                "propertyName": "pipeline",
                "operator": "IN",
                "values": sorted(allowed),
            }
        )
        rows = read(
            client.search_all,
            "deals",
            filters,
            ["dealname", "hubspot_owner_id", "pipeline"],
        )
        open_ids = []
        for row in rows or []:
            props = row.get("properties") or {}
            did = str(row.get("id") or "")
            pipeline = str(props.get("pipeline") or "")
            if not did or pipeline not in allowed:
                continue
            owner = str(props.get("hubspot_owner_id") or "")
            if owner not in chunk:
                continue
            index.deals.setdefault(
                did,
                Deal(
                    id=did,
                    name=props.get("dealname") or "",
                    owner_id=owner,
                    pipeline=pipeline,
                ),
            )
            index.owner_open_deals.setdefault(owner, []).append(did)
            open_ids.append(did)
        if open_ids:
            links = read(
                client.batch_associations,
                "deals",
                "companies",
                open_ids,
                prefer_type_id=_PRIMARY_DEAL_COMPANY,
            )
            linked_company_ids = {
                str(cid) for values in links.values() for cid in values
            }
            strict_open_companies = _strict_company_ids(linked_company_ids)
            if len(strict_open_companies) != len(linked_company_ids):
                index.scope_limited = True
            for did in open_ids:
                index.deals[did].company_ids = [
                    str(cid)
                    for cid in links.get(did, [])
                    if str(cid) in strict_open_companies
                ]
                for cid in index.deals[did].company_ids:
                    index.companies.setdefault(cid, Company(id=cid))

    # 6. Overdue tasks owned by roster AEs, and the state of tracked ones.
    tz = _business_tz()
    due_before = datetime.combine(
        today - timedelta(days=max(0, task_grace_days) - 1), datetime.min.time(), tz
    )
    # New task loose ends only for tasks that went overdue recently; tasks
    # already on the list are re-read by id below, however old.
    due_after = now - timedelta(days=min(task_lookback_days, max_age_days))
    task_company_ids: set[str] = set()
    for chunk in _chunks(roster_owner_ids, IN_CHUNK):
        rows = read(
            client.search_all,
            "tasks",
            [
                {"propertyName": "hubspot_owner_id", "operator": "IN", "values": chunk},
                {
                    "propertyName": "hs_task_status",
                    "operator": "NEQ",
                    "value": "COMPLETED",
                },
                {
                    "propertyName": "hs_timestamp",
                    "operator": "LT",
                    "value": _ms(due_before),
                },
                {
                    "propertyName": "hs_timestamp",
                    "operator": "GTE",
                    "value": _ms(due_after),
                },
            ],
            ["hs_task_subject", "hs_timestamp", "hs_task_status", "hubspot_owner_id"],
        )
        task_candidates: dict[str, dict[str, Any]] = {}
        for row in rows or []:
            props = row.get("properties") or {}
            tid = str(row.get("id") or "")
            if not tid or (props.get("hs_task_status") or "") == "COMPLETED":
                continue
            if str(props.get("hubspot_owner_id") or "") not in chunk:
                continue
            task_candidates[tid] = props
        if task_candidates:
            ids = list(task_candidates)
            try:
                strict_tasks = read(client.fully_owned_deal_associations, "tasks", ids)
            except Exception:  # noqa: BLE001 — task subjects can be client-private
                logger.warning(
                    "[loop-closure] task ownership check failed", exc_info=True
                )
                strict_tasks = {}
            if len(strict_tasks) != len(ids):
                index.scope_limited = True
            task_candidates = {
                tid: props
                for tid, props in task_candidates.items()
                if tid in strict_tasks
            }
            ids = list(task_candidates)
        if task_candidates:
            to_contacts = read(client.batch_associations, "tasks", "contacts", ids)
            to_companies = read(client.batch_associations, "tasks", "companies", ids)
            to_deals = read(client.batch_associations, "tasks", "deals", ids)
            linked_deal_ids = sorted(
                {str(did) for values in to_deals.values() for did in values}
            )
            _read_deals(index, client, read, linked_deal_ids, allowed)
            task_company_candidates = {
                str(cid) for values in to_companies.values() for cid in values
            }
            strict_task_companies = _strict_company_ids(task_company_candidates)
            task_contact_candidates = {
                str(cid) for values in to_contacts.values() for cid in values
            }
            try:
                strict_task_contacts = (
                    set(
                        read(
                            client.fully_owned_deal_associations,
                            "contacts",
                            sorted(task_contact_candidates),
                        )
                    )
                    if task_contact_candidates
                    else set()
                )
            except Exception:  # noqa: BLE001 — unknown contact association denies the task
                logger.warning(
                    "[loop-closure] task contact ownership check failed",
                    exc_info=True,
                )
                strict_task_contacts = set()
            for tid, props in task_candidates.items():
                if any(
                    str(cid) not in strict_task_companies
                    for cid in to_companies.get(tid, [])
                ) or any(
                    str(cid) not in strict_task_contacts
                    for cid in to_contacts.get(tid, [])
                ):
                    index.scope_limited = True
                    continue
                owned_deals = [
                    str(did) for did in to_deals.get(tid, []) if str(did) in index.deals
                ]
                owned_companies = [
                    str(cid)
                    for cid in to_companies.get(tid, [])
                    if str(cid) in strict_task_companies
                ]
                if not owned_deals and not owned_companies:
                    index.scope_limited = True
                    continue
                task = Task(
                    id=tid,
                    owner_id=str(props.get("hubspot_owner_id") or ""),
                    subject=(props.get("hs_task_subject") or "").strip(),
                    due=parse_hs_time(props.get("hs_timestamp")),
                    contact_ids=[
                        str(cid)
                        for cid in to_contacts.get(tid, [])
                        if any(c.id == str(cid) for c in index.contacts.values())
                    ],
                    company_ids=owned_companies,
                    deal_ids=owned_deals,
                )
                task_company_ids.update(task.company_ids)
                index.tasks.setdefault(task.owner_id, []).append(task)
    if open_task_ids:
        # Old tenant rows can outlive a portal/pipeline change. Prove the task
        # still belongs to an owned deal or wholly owned company before even
        # reading its subject, status or owner.
        try:
            strict_open_tasks = read(
                client.fully_owned_deal_associations, "tasks", open_task_ids
            )
        except Exception:  # noqa: BLE001 — unknown is not a completed task
            logger.warning(
                "[loop-closure] tracked task ownership check failed", exc_info=True
            )
            strict_open_tasks = {}
        if len(strict_open_tasks) != len(open_task_ids):
            index.scope_limited = True
        open_task_ids = [tid for tid in open_task_ids if tid in strict_open_tasks]
    if open_task_ids:
        open_to_deals = read(client.batch_associations, "tasks", "deals", open_task_ids)
        open_to_contacts = read(
            client.batch_associations, "tasks", "contacts", open_task_ids
        )
        open_to_companies = read(
            client.batch_associations, "tasks", "companies", open_task_ids
        )
        _read_deals(
            index,
            client,
            read,
            sorted({str(d) for values in open_to_deals.values() for d in values}),
            allowed,
        )
        strict_open_task_companies = _strict_company_ids(
            {str(c) for values in open_to_companies.values() for c in values}
        )
        open_contact_candidates = {
            str(cid) for values in open_to_contacts.values() for cid in values
        }
        try:
            strict_open_task_contacts = (
                set(
                    read(
                        client.fully_owned_deal_associations,
                        "contacts",
                        sorted(open_contact_candidates),
                    )
                )
                if open_contact_candidates
                else set()
            )
        except Exception:  # noqa: BLE001 — never re-read a mixed task
            logger.warning(
                "[loop-closure] tracked task contact ownership check failed",
                exc_info=True,
            )
            strict_open_task_contacts = set()
        proven_open_task_ids = [
            tid
            for tid in open_task_ids
            if all(
                str(c) in strict_open_task_companies
                for c in open_to_companies.get(tid, [])
            )
            and all(
                str(c) in strict_open_task_contacts
                for c in open_to_contacts.get(tid, [])
            )
            and (
                any(str(d) in index.deals for d in open_to_deals.get(tid, []))
                or any(
                    str(c) in strict_open_task_companies
                    for c in open_to_companies.get(tid, [])
                )
            )
        ]
        if len(proven_open_task_ids) != len(open_task_ids):
            index.scope_limited = True
        rows = read(
            client.batch_read,
            "tasks",
            proven_open_task_ids,
            ["hs_task_status", "hs_timestamp", "hubspot_owner_id"],
        )
        for row in rows or []:
            props = row.get("properties") or {}
            tid = str(row.get("id"))
            status = (props.get("hs_task_status") or "").strip().upper()
            index.task_states[tid] = "completed" if status == "COMPLETED" else "open"
            # Only what HubSpot actually returned: a missing value decides
            # nothing (an older reply shape, or a property the token cannot read).
            if "hs_timestamp" in props:
                index.task_due[tid] = parse_hs_time(props.get("hs_timestamp"))
            owner = str(props.get("hubspot_owner_id") or "").strip()
            if owner:
                index.task_owner[tid] = owner
        # Asked for and not returned = deleted (or archived) in HubSpot.
        for tid in proven_open_task_ids:
            index.task_states.setdefault(tid, "missing")

    # 7. Company names for everything the index refers to.
    for cid in task_company_ids:
        index.companies.setdefault(cid, Company(id=cid))
    named = sorted(index.companies)
    if named:
        rows = read(client.batch_read, "companies", named, ["name", "domain"])
        for row in rows or []:
            props = row.get("properties") or {}
            company = index.companies.get(str(row.get("id")))
            if company is not None:
                company.name = (props.get("name") or "").strip()
                company.domain = (props.get("domain") or "").strip().lower()

    # 8. The engagement index, in 7-day slices.
    _read_engagements(
        index, client, read, window_start=window_start, now=now, allowed=allowed
    )

    if index.capped or index.failed_domains or index.scope_limited:
        index.status = "partial"
    return index


def _business_tz():
    from rubik_core.tenancy.workspace_time import workspace_time_zone

    return workspace_time_zone()


def _read_deals(
    index: HubSpotIndex,
    client: Any,
    read: Callable[..., Any],
    deal_ids: list[str],
    allowed: set[str],
) -> None:
    if not deal_ids or not allowed:
        return
    rows = read(
        client.batch_read,
        "deals",
        deal_ids,
        ["dealname", "pipeline", "hubspot_owner_id"],
    )
    for row in rows or []:
        props = row.get("properties") or {}
        did = str(row.get("id") or "")
        pipeline = str(props.get("pipeline") or "")
        if not did or pipeline not in allowed:
            continue
        prior = index.deals.get(did)
        index.deals[did] = Deal(
            id=did,
            name=props.get("dealname") or "",
            owner_id=str(props.get("hubspot_owner_id") or ""),
            pipeline=pipeline,
            company_ids=list(prior.company_ids) if prior else [],
        )


def _read_engagements(
    index: HubSpotIndex,
    client: Any,
    read: Callable[..., Any],
    *,
    window_start: datetime,
    now: datetime,
    allowed: set[str],
) -> None:
    cap = int(getattr(client, "MAX_RECORDS", 5000) or 5000)
    found: dict[str, dict[str, Engagement]] = {}
    for etype in ENGAGEMENT_TYPES:
        props = ["hs_timestamp"] + (["hs_email_direction"] if etype == "emails" else [])
        by_id: dict[str, Engagement] = {}
        start = window_start
        while start < now:
            end = min(start + timedelta(days=SLICE_DAYS), now)
            rows = read(
                client.search_all,
                etype,
                [
                    {
                        "propertyName": "hs_timestamp",
                        "operator": "GTE",
                        "value": _ms(start),
                    },
                    {
                        "propertyName": "hs_timestamp",
                        "operator": "LT",
                        "value": _ms(end),
                    },
                ],
                props,
            )
            rows = rows or []
            if len(rows) >= cap:
                index.capped.append((start, end))
                logger.warning(
                    "[loop-closure] HubSpot %s search hit the %d-record cap for one "
                    "7-day slice; the index is partial",
                    etype,
                    cap,
                )
            for row in rows:
                eid = str(row.get("id") or "")
                p = row.get("properties") or {}
                at = parse_hs_time(p.get("hs_timestamp"))
                if not eid or at is None or not (window_start <= at < now):
                    continue
                by_id[eid] = Engagement(
                    at=at,
                    type=etype,
                    id=eid,
                    incoming=(p.get("hs_email_direction") or "") == "INCOMING_EMAIL",
                )
            start = end
        found[etype] = by_id

    known_contact_ids = {contact.id for contact in index.contacts.values()}
    for etype, by_id in found.items():
        ids = sorted(by_id)
        if not ids:
            continue
        try:
            strict_activities = read(client.fully_owned_deal_associations, etype, ids)
        except Exception:  # noqa: BLE001 — a partial activity scan is unknown
            logger.warning(
                "[loop-closure] %s ownership check failed", etype, exc_info=True
            )
            strict_activities = {}
        if len(strict_activities) != len(ids):
            index.scope_limited = True
        ids = [eid for eid in ids if eid in strict_activities]
        if not ids:
            continue
        to_deals = read(client.batch_associations, etype, "deals", ids)
        to_companies = read(client.batch_associations, etype, "companies", ids)
        to_contacts = read(client.batch_associations, etype, "contacts", ids)
        _read_deals(
            index,
            client,
            read,
            sorted({str(d) for values in to_deals.values() for d in values}),
            allowed,
        )
        strict_companies = _strict_company_ids(
            {str(c) for values in to_companies.values() for c in values}
        )
        activity_contact_candidates = {
            str(cid) for values in to_contacts.values() for cid in values
        }
        try:
            strict_contacts = (
                set(
                    read(
                        client.fully_owned_deal_associations,
                        "contacts",
                        sorted(activity_contact_candidates),
                    )
                )
                if activity_contact_candidates
                else set()
            )
        except Exception:  # noqa: BLE001 — mixed activity evidence stays unknown
            logger.warning(
                "[loop-closure] activity contact ownership check failed",
                exc_info=True,
            )
            strict_contacts = set()
        for eid in ids:
            eng = by_id[eid]
            if any(
                str(cid) not in strict_companies for cid in to_companies.get(eid, [])
            ) or any(
                str(cid) not in strict_contacts for cid in to_contacts.get(eid, [])
            ):
                index.scope_limited = True
                continue
            owned_deals = [
                str(did) for did in to_deals.get(eid, []) if str(did) in index.deals
            ]
            owned_companies = [
                str(cid)
                for cid in to_companies.get(eid, [])
                if str(cid) in strict_companies
            ]
            if not owned_deals and not owned_companies:
                # A portal-wide search found an activity whose workspace
                # ownership cannot be proved. It may be a client's company-
                # only log, so suppress negative "unlogged" findings.
                index.scope_limited = True
                continue
            # The most specific association wins. A call logged on Bob's
            # contact and a shared deal is not proof that Jane was contacted.
            # Using all three keys would silently close every person's loop
            # at that company after one colleague's call.
            specific_contacts = [
                str(cid)
                for cid in to_contacts.get(eid, [])
                if str(cid) in known_contact_ids
            ]
            if specific_contacts:
                for cid in specific_contacts:
                    index.engagements.setdefault(f"contact:{cid}", []).append(eng)
            elif owned_deals:
                for did in owned_deals:
                    index.engagements.setdefault(f"deal:{did}", []).append(eng)
            else:
                for cid in owned_companies:
                    if cid in index.companies:
                        index.engagements.setdefault(f"company:{cid}", []).append(eng)
    for key in index.engagements:
        index.engagements[key].sort(key=lambda e: e.at)

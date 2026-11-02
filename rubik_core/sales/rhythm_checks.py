"""The sales daily-rhythm checks — the HubSpot questions behind a rep's day.

Sales asked for the "Daily Operational AI Prompt Checklist" (a Google Doc of
eleven prompts in three routines) to live inside Cubie. Eight of those prompts
are HubSpot questions, and this module answers them:

    morning     closing_focus              deals closing this month that need action today
    mid-day     waiting_on_decision_maker  late-stage deals where the client owes the reply
                close_date_slippage        close date pushed 3+ times, or 7 days of silence
                single_threaded            open deals over $30k with one contact or none
                reengage_lost              deals lost 12–24 months ago to timing or budget
    end of day  missing_fields             records touched today missing a key field
                overdue                    past-due HubSpot tasks and close dates
                activity_today             what was logged today, for the manager recap

The other three (inbox triage, promises made, meeting prep) are answered by
tools Ask Cubie already has — Gmail, Slack, My To-Dos, the calendar.

**Two consumers, one definition.** Ask Cubie reaches these through
``HubSpotAPIRetriever`` (the chat model narrates the rows), and the Sales
Cockpit's My Day "Daily rhythm" section reaches them through
``backend/routers/sales_rhythm.py`` (rows become worklist items). Both call
:func:`run_check`, so the chat answer and the card can never disagree about
which deals qualify. CRM answers are read live because a shared-portal record
can leave the workspace's pipelines between requests.

Rules this module keeps:

* **Arithmetic, not AI.** No model call happens here, so nothing in this module
  adds to Admin → AI Cost. The AI is the chat model reading the rows.
* **Every result carries its denominator.** ``scanned`` is what the rows are out
  of ("3 of 41 open deals in a proposal or contract stage"); a count with no
  denominator is never published.
* **"Could not look" is never an empty list.** A failed read sets
  ``available=False`` with a named reason, or ``notes`` when only half of a
  check ran — never an empty result that reads as "all clear".
* **The workspace's own pipelines only.** The portal may host a client's pipeline;
  every deal search carries the workspace's configured pipeline filter.
* **Logged activity only.** Every date here is what HubSpot recorded. An email
  that was never logged does not exist as far as these checks can tell, and the
  copy says so rather than implying otherwise.
* **The deal AND its client.** A rep who emails someone at the client who is
  not on the deal logs it on that person's company, never on the deal, so a
  "how long since contact" reads the client company's date too and says which
  is which (:func:`silence`). The deal's own date is never hidden.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from rubik_core.brand import PRODUCT_NAME
from rubik_core.cockpit.business_hours import pin_business_time_zone
from rubik_core.config import settings
from rubik_core.retrieval.hubspot_api import (
    _OPEN,
    HubSpotAPIError,
    _add_months,
    _fmt_money,
    _ms,
    _num,
    _parse_hs_datetime,
    _plausible_date,
    _today,
    _tz,
)
from rubik_core.sales import rhythm_signals
from rubik_core.tenancy.context import current_workspace
from rubik_core.tenancy.hubspot_config import get_hubspot_config

logger = logging.getLogger(__name__)

# ── Thresholds (the checklist's own numbers) ─────────────────────────────────

#: "waiting on a response from the decision-maker for more than 3 business days"
WAITING_BUSINESS_DAYS = 3
#: "the estimated close date has been pushed more than twice" → three or more.
SLIPPAGE_MIN_PUSHES = 3
#: "zero activity has been logged in the last 7 days"
QUIET_DAYS = 7
#: "opportunities valued over $30k that only have a single contact"
SINGLE_THREAD_MIN_AMOUNT = 30_000
#: "deals lost in the last 12-24 months due to timing or budget"
REENGAGE_MONTHS = (12, 24)
#: How many lost deals get the (association + contact) walk. Bounded because
#: each needs two extra reads; ranked by amount first, so the cap drops the
#: smallest deals, never an arbitrary slice.
REENGAGE_MAX_CANDIDATES = 60

#: Deal stages that mean "the proposal is with the client". The company names
#: them in workspace HubSpot settings (stage ids or labels). Until it does,
#: these words in a stage LABEL stand in: a company that runs several
#: pipelines has stage ids that differ while the names agree. A drafting stage
#: is left out on purpose: there WE owe the client the document, so nobody is
#: waiting on them.
LATE_STAGE_KEYWORDS: tuple[str, ...] = (
    "proposal",
    "sow sent",
    "legal review",
    "negotiat",
    "red line",
    "contract",
    "sign-off",
    "sign off",
    "verbal",
)
_LATE_STAGE_EXCLUDE: tuple[str, ...] = ("closed", "lost", "drafting", "hold")

#: Reason codes for an unavailable result. A closed set: the My Day client has
#: a sentence for each (``frontend/src/api/salesRhythm.ts``).
CRM_UNAVAILABLE = "crm_unavailable"
CRM_READ_FAILED = "crm_read_failed"
NO_HUBSPOT_OWNER = "no_hubspot_owner"
#: The owner matched, but HubSpot gave no USER id for them, so "records you
#: touched today" (keyed on user ids) cannot be answered.
NO_HUBSPOT_USER = "no_hubspot_user"


# ── Result types ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RhythmScope:
    """Whose records a check reads.

    ``owner_ids`` None means every owner — the team view. The team is the whole
    company's sales book (every deal owner in its own pipelines): HubSpot has no
    reliable smaller grouping to offer.
    """

    owner_ids: tuple[str, ...] | None
    user_ids: tuple[str, ...] | None
    team: bool
    label: str
    #: The rep's OWN mailbox, set only when the scope is the signed-in caller's
    #: own deals. Its Gmail and Calendar then count as contact
    #: (``rhythm_signals``). None for the team and for a named colleague —
    #: those views never read anyone's email or calendar.
    mailbox: str | None = None

    @property
    def cache_key(self) -> str:
        if self.team:
            return "team"
        # The same owner read as "you" (with your mailbox) and as a named
        # colleague (without) are different answers and must not share a slot.
        base = ",".join(self.owner_ids or ())
        return f"{base}|mail" if self.mailbox else base


@dataclass
class CheckRow:
    """One record a check flagged, with the plain-language reasons why."""

    record_id: str
    object_type: (
        str  # deal | contact | task | call | email | meeting | note | stage_change
    )
    title: str
    reasons: list[str]
    amount: float | None = None
    owner_id: str | None = None
    owner_name: str | None = None
    url: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class CheckResult:
    """A check's answer. ``scanned`` is the denominator the rows are out of."""

    check: str
    rows: list[CheckRow]
    scanned: int
    available: bool = True
    reason: str | None = None
    notes: list[str] = field(default_factory=list)
    #: True when part of the check could not run (a history or contact read
    #: failed). The rows are still worth showing — ``notes`` says what is
    #: missing — and the next request reads the source again.
    partial: bool = False
    #: Which sources this answer actually counted: always "hubspot", plus
    #: "gmail" / "calendar" when the rep's own mailbox or calendar was read.
    sources: tuple[str, ...] = ("hubspot",)
    #: True when contact logged on each deal's client company counted, not
    #: only contact logged on the deal itself (:func:`client_contacts`).
    client_records: bool = False


def _unavailable(check: str, reason: str, note: str | None = None) -> CheckResult:
    return CheckResult(
        check=check,
        rows=[],
        scanned=0,
        available=False,
        reason=reason,
        notes=[note] if note else [],
    )


# ── Small helpers ────────────────────────────────────────────────────────────


def _retry(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call a HubSpot read, retrying a 429 / 5xx / transport blip twice.

    The search API allows a handful of requests per second per portal, and a
    mid-day tab fans out several checks; a single 429 should cost half a
    second, not the block.
    """
    delay = 0.5
    for attempt in range(3):
        try:
            return fn(*args, **kwargs)
        except HubSpotAPIError as exc:
            msg = str(exc)
            transient = "429" in msg or "HTTP 5" in msg or "transport" in msg.lower()
            if not transient or attempt == 2:
                raise
            time.sleep(delay)
            delay *= 2
    raise AssertionError("unreachable")  # pragma: no cover


def _allowed_pipelines() -> list[str]:
    return list(get_hubspot_config().pipeline_ids)


def _pipeline_filter() -> list[dict]:
    allowed = _allowed_pipelines()
    if not allowed:
        raise HubSpotAPIError("Workspace deal pipelines are not configured")
    return [{"propertyName": "pipeline", "operator": "IN", "values": allowed}]


def _owner_filter(scope: RhythmScope, prop: str = "hubspot_owner_id") -> list[dict]:
    if scope.owner_ids is None:
        return []
    ids = list(scope.owner_ids)
    if len(ids) == 1:
        return [{"propertyName": prop, "operator": "EQ", "value": ids[0]}]
    return [{"propertyName": prop, "operator": "IN", "values": ids}]


def record_url(object_type: str, record_id: str | None) -> str | None:
    """The HubSpot page for a record, or None when the portal id is unset."""
    portal = get_hubspot_config().portal_id
    if not portal or not record_id:
        return None
    if object_type == "task":
        # A task has no stable standalone record page; the rep's task queue is
        # where it is actioned.
        return f"https://app.hubspot.com/tasks/{portal}/view/all"
    path = {"deal": "deal", "contact": "contact", "company": "company"}.get(object_type)
    if not path:
        return None
    return f"https://app.hubspot.com/contacts/{portal}/{path}/{record_id}"


def _local_day_bounds(day: date) -> tuple[int, int]:
    """Epoch-ms [start, end) of a calendar day in the HubSpot portal timezone."""
    return _ms(day), _ms(day + timedelta(days=1))


def months_back(d: date, n: int) -> date:
    """The same calendar day ``n`` months before ``d``, clamped to month end.

    Not ``_add_months``, which snaps to the FIRST of the month: "lost 12–24
    months ago" on the 31st must not quietly mean "lost 12–25 months ago".
    """
    m = d.month - 1 - n
    year, month = d.year + m // 12, m % 12 + 1
    last = (date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)).day
    return date(year, month, min(d.day, last))


def _local_date(value: Any) -> date | None:
    """A HubSpot datetime as a calendar day in the portal's timezone."""
    dt = _parse_hs_datetime(value)
    if dt is None:
        return None
    return dt.astimezone(_tz()).date()


def _fmt_day(value: Any) -> str:
    d = _local_date(value)
    return f"{d:%b} {d.day}, {d.year}" if d else "—"


def _contact_name(props: dict) -> str:
    name = f"{props.get('firstname') or ''} {props.get('lastname') or ''}".strip()
    return name or (props.get("email") or "").strip() or "(unnamed contact)"


def _has_role(props: dict, role: str) -> bool:
    raw = str(props.get("hs_buying_role") or "")
    return role in {r.strip().upper() for r in raw.split(";") if r.strip()}


def _strip_html(text: str | None, limit: int = 200) -> str:
    plain = re.sub(r"<[^>]+>", " ", text or "")
    plain = re.sub(r"\s+", " ", plain).strip()
    return plain if len(plain) <= limit else plain[: limit - 1].rstrip() + "…"


# ── Directory (owners + stage labels), cached ────────────────────────────────

_DIR_TTL_SECONDS = 1800
_dir_cache: dict[tuple[str, bytes], tuple[float, dict, dict]] = {}
_dir_lock = threading.Lock()
_DIR_CACHE_MAX = 128


def _client_fingerprint(client) -> bytes | None:
    """Hash the active token so a rotated HubSpot account cannot reuse rows."""
    token = getattr(client, "token", None)
    if not isinstance(token, str) or not token.strip():
        return None
    return hashlib.sha256(token.strip().encode("utf-8")).digest()


def directory(client) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """``(owner_directory, stage_labels)``, cached for 30 minutes.

    Both are account-wide lookups that rarely change; fetching them per check
    is how a mid-day tab would spend its rate limit on the same two answers.
    Both swallow their own errors into ``{}``, so the pair is cached only when
    BOTH came back: a failed stage read cached for half an hour would leave
    "waiting on the client" unavailable long after HubSpot recovered.
    """
    fingerprint = _client_fingerprint(client)
    key = (current_workspace(), fingerprint) if fingerprint is not None else None
    if key is not None:
        with _dir_lock:
            hit = _dir_cache.get(key)
            if hit and time.monotonic() - hit[0] < _DIR_TTL_SECONDS:
                return hit[1], hit[2]
    owners = client.owner_directory()
    stages = client.deal_stage_labels()
    if key is not None and owners and stages:
        with _dir_lock:
            now = time.monotonic()
            for old, entry in list(_dir_cache.items()):
                if now - entry[0] >= _DIR_TTL_SECONDS:
                    _dir_cache.pop(old, None)
            while len(_dir_cache) >= _DIR_CACHE_MAX:
                _dir_cache.pop(next(iter(_dir_cache)))
            _dir_cache[key] = (now, owners, stages)
    return owners, stages


def owner_names(client) -> dict[str, str]:
    owners, _ = directory(client)
    return {oid: info.get("name") or "" for oid, info in owners.items()}


# ── Scope resolution ─────────────────────────────────────────────────────────


def resolve_scope(
    client,
    caller_email: str | None,
    *,
    team: bool = False,
    owner_id: str | None = None,
    own_mailbox: bool = False,
) -> RhythmScope | None:
    """The scope for a caller, or None when "mine" cannot be resolved.

    ``team`` wins over everything else. ``owner_id`` (a named rep the chat
    resolved) wins over the caller. None means the caller is not a HubSpot
    owner — never silently widened to the team, because "your deals" answered
    with everybody's deals is a confident wrong answer.

    ``own_mailbox`` attaches ``caller_email`` as the scope's mailbox, so the
    checks also read that person's Gmail and Calendar. Pass it ONLY when
    ``caller_email`` is the authenticated person themselves and the answer goes
    back to them (My Day, Ask Cubie). Never for a view of someone else — an
    admin impersonating a rep must stay HubSpot-only — so the default is no
    mailbox: forgetting the flag costs the email signal, never someone's inbox.
    """
    if team:
        return RhythmScope(owner_ids=None, user_ids=None, team=True, label="your team")
    owners, _ = directory(client)
    if not owners:
        # The portal always has owners, and ``owner_directory`` swallows its
        # own errors into {} — so empty means HubSpot could not be read. Saying
        # "you are not a HubSpot owner" here would blame the rep for an outage.
        raise HubSpotAPIError("HubSpot owner directory could not be read")
    if owner_id:
        info = owners.get(str(owner_id)) or {}
        user_id = info.get("user_id") or ""
        return RhythmScope(
            owner_ids=(str(owner_id),),
            user_ids=(user_id,) if user_id else None,
            team=False,
            label=info.get("name") or "this rep",
        )
    want = (caller_email or "").strip().lower()
    if not want:
        return None
    for oid, info in owners.items():
        if (info.get("email") or "").lower() == want:
            user_id = info.get("user_id") or ""
            return RhythmScope(
                owner_ids=(str(oid),),
                user_ids=(user_id,) if user_id else None,
                team=False,
                label="you",
                mailbox=want if own_mailbox else None,
            )
    return None


# ── Shared reads ─────────────────────────────────────────────────────────────


def _open_deals(
    client, scope: RhythmScope, properties: list[str], extra: Sequence[dict] = ()
) -> list[dict]:
    filters = [_OPEN, *_owner_filter(scope), *_pipeline_filter(), *extra]
    return _retry(client.search_all, "deals", filters, properties)


def _row_owner(names: dict[str, str], props: dict) -> tuple[str | None, str | None]:
    oid = str(props.get("hubspot_owner_id") or "") or None
    return oid, (names.get(oid) if oid else None)


#: The signal provider — the rep's own Gmail and Calendar. A module attribute
#: so tests swap in canned touches without any Google client.
SIGNALS = rhythm_signals.collect
DAY_ACTIVITY = rhythm_signals.day_activity


def _deal_contact_emails(client, deal_ids: list[str]) -> dict[str, list[str]]:
    """``{deal: [contact email, ...]}`` from HubSpot. Raises HubSpotAPIError."""
    assoc = _retry(client.batch_associations, "deals", "contacts", deal_ids)
    ids = sorted({c for v in assoc.values() for c in v})
    emails = {
        str(r.get("id")): (r.get("properties") or {}).get("email") or ""
        for r in _retry(client.batch_read, "contacts", ids, ["email"])
    }
    return {d: [emails[c] for c in cids if emails.get(c)] for d, cids in assoc.items()}


def _touches(
    client,
    scope: RhythmScope,
    deal_ids: list[str],
    notes: list[str],
    *,
    gmail: bool = True,
    calendar: bool = True,
) -> tuple[dict[str, rhythm_signals.Touch], frozenset[str], bool]:
    """The rep's own email/meeting touches per deal.

    Returns ``(touches, sources_read, complete)``. Empty and complete for the
    team and a named colleague: those views count only HubSpot. For your own
    deals, a source that could not be read adds a note, is left out of
    ``sources_read`` (so the wording falls back to "logged") and makes the
    answer incomplete, never "no contact".
    """
    if not scope.mailbox or not deal_ids:
        return {}, frozenset(), True
    try:
        emails = _deal_contact_emails(client, deal_ids)
    except HubSpotAPIError:
        logger.warning("[rhythm] deal contacts unreadable for signals")
        notes.append(
            "The contacts on these deals could not be read from HubSpot, so your "
            "email and calendar were not checked this time."
        )
        return {}, frozenset(), False
    own = scope.mailbox.rsplit("@", 1)[-1]
    terms = {d: rhythm_signals.match_terms(e, own) for d, e in emails.items()}
    report = SIGNALS(scope.mailbox, terms, gmail=gmail, calendar=calendar)
    notes.extend(report.notes)
    read = frozenset(
        name
        for name, wanted, ok in (
            ("gmail", gmail, report.gmail_ok),
            ("calendar", calendar, report.calendar_ok),
        )
        if wanted and ok
    )
    complete = (not gmail or report.gmail_ok) and (not calendar or report.calendar_ok)
    return report.touches, read, complete


def _latest(*values: datetime | None) -> datetime | None:
    seen = [v for v in values if v is not None]
    return max(seen) if seen else None


def _contact_phrase(read: frozenset[str]) -> str:
    """'client contact' once the rep's email counted, else 'logged client contact'."""
    return "client contact" if "gmail" in read else "logged client contact"


def _sources(read: frozenset[str]) -> tuple[str, ...]:
    return ("hubspot", *sorted(read))


def close_date_pushes(history: list[dict] | None) -> int:
    """How many times a close date moved LATER, by calendar day.

    Compares consecutive values oldest-first. A move earlier (pulled in) is not
    a push, a same-day edit is not a push, and the first value is the starting
    point rather than a change. HubSpot's history order is not relied on: the
    entries are sorted by their own timestamps.
    """
    if not history:
        return 0
    entries: list[tuple[datetime, date]] = []
    for entry in history:
        when = _parse_hs_datetime(entry.get("timestamp"))
        value = _local_date(entry.get("value"))
        if when is None or value is None:
            continue
        entries.append((when, value))
    entries.sort(key=lambda e: e[0])
    pushes = 0
    for (_, before), (_, after) in zip(entries, entries[1:], strict=False):
        if after > before:
            pushes += 1
    return pushes


def _closedate_history(client, deal_ids: list[str]) -> dict[str, list[dict]]:
    rows = _retry(
        client.batch_read,
        "deals",
        deal_ids,
        ["closedate"],
        properties_with_history=["closedate"],
    )
    out: dict[str, list[dict]] = {}
    for row in rows:
        hist = (row.get("propertiesWithHistory") or {}).get("closedate") or []
        out[str(row.get("id"))] = hist
    return out


def _configured_late_stages() -> frozenset[str]:
    """The workspace's configured stage ids or labels, folded."""
    return frozenset(
        stage.casefold() for stage in get_hubspot_config().sales_rhythm_late_stages
    )


def _late_stage_ids(stage_labels: dict[str, str]) -> list[str]:
    configured = _configured_late_stages()
    ids = []
    for sid, label in stage_labels.items():
        low = (label or "").strip().lower()
        if configured:
            if str(sid).strip().lower() in configured or low in configured:
                ids.append(str(sid))
            continue
        if any(x in low for x in _LATE_STAGE_EXCLUDE):
            continue
        if any(k in low for k in LATE_STAGE_KEYWORDS):
            ids.append(str(sid))
    return sorted(ids)


# ── The client's own last contact ────────────────────────────────────────────

#: HubSpot's v4 association type for a deal's PRIMARY company.
PRIMARY_COMPANY_TYPE_ID = 5

#: How a deal's silence reads once its client's last contact is known.
CONTACT_OK = "ok"  # the deal itself was contacted inside the window
QUIET = "quiet"  # no recent contact on the deal, nor on its client
DEAL_ONLY = "deal_only"  # the deal is quiet, but its client heard from us recently


@dataclass(frozen=True)
class ClientContact:
    """A deal's client company, and when HubSpot last logged contact with it."""

    company_id: str
    name: str
    last_contact: datetime | None


@dataclass(frozen=True)
class Silence:
    """How long a deal has gone without contact, read from the deal and its client."""

    state: str
    #: Days since the newest contact on the deal or its client (None: never).
    days: int | None
    #: The row's wording; None when the deal is not quiet.
    reason: str | None


def client_contacts(client, deal_ids: Sequence[str]) -> dict[str, ClientContact]:
    """``{deal: ClientContact}`` for the client company behind each deal.

    A deal's own "Last Contacted" moves only when the email, call or meeting
    is attached to that deal. Email someone at the client who is not on the
    deal and HubSpot logs it on that person and their company instead, so the
    deal reads as quiet while the client is hearing from us: a deal can say
    "no contact in 82 days" when the rep emailed the client about it 13
    days earlier. The company's "Last Contacted"
    catches that. It does not prove THIS deal was discussed (a client can
    have several deals open), so it is shown beside the deal's date, never
    instead of it.

    Reads each deal's primary company; a deal with no primary label reads
    every company it has, the most recently contacted winning. A deal with no
    company is left out. Raises ``HubSpotAPIError``.
    """
    wanted = [str(d) for d in dict.fromkeys(str(d) for d in deal_ids if d)]
    if not wanted:
        return {}
    assoc = _retry(
        client.batch_associations,
        "deals",
        "companies",
        wanted,
        prefer_type_id=PRIMARY_COMPANY_TYPE_ID,
    )
    company_ids = sorted({c for ids in assoc.values() for c in ids})
    if not company_ids:
        return {}
    companies = {
        str(r.get("id")): r.get("properties") or {}
        for r in _retry(
            client.batch_read,
            "companies",
            company_ids,
            ["name", "notes_last_contacted"],
        )
    }
    out: dict[str, ClientContact] = {}
    for did in wanted:
        for cid in assoc.get(did, []):
            props = companies.get(cid)
            if props is None:
                continue
            found = ClientContact(
                company_id=cid,
                name=(props.get("name") or "").strip() or "The client",
                last_contact=_parse_hs_datetime(
                    _plausible_date(props.get("notes_last_contacted"))
                ),
            )
            best = out.get(did)
            if best is None or (
                found.last_contact is not None
                and (
                    best.last_contact is None or found.last_contact > best.last_contact
                )
            ):
                out[did] = found
    return out


def _ago(days: int) -> str:
    if days <= 0:
        return "today"
    return "yesterday" if days == 1 else f"{days} days ago"


def silence(
    deal_last: datetime | None,
    client: ClientContact | None,
    *,
    now: datetime,
    quiet_days: int,
    phrase: str,
) -> Silence:
    """Whether a deal has gone quiet, worded from the deal AND its client.

    Which deals are flagged does not change: a deal is quiet when the deal
    itself has had no contact in ``quiet_days``. What changes is how it reads
    and how much it weighs. When the client company was contacted more
    recently than the deal, the row leads with the client's number and gives
    the deal's own beside it, so "82 days" never stands alone when the client
    heard from us 13 days ago. When that client contact falls inside the
    window, the state is ``DEAL_ONLY``: a lighter flag that ranks after deals
    whose whole client has gone quiet.
    """
    deal_days = (now - deal_last).days if deal_last else None
    if deal_days is not None and deal_days < quiet_days:
        return Silence(CONTACT_OK, deal_days, None)
    client_last = client.last_contact if client else None
    client_days = max((now - client_last).days, 0) if client_last else None
    # Compared in whole days: a client email a few hours newer than the deal's
    # own would otherwise read "no contact in 82 days (none on this deal
    # itself in 82 days)".
    if (
        client is None
        or client_days is None
        or (deal_days is not None and client_days >= deal_days)
    ):
        return Silence(
            QUIET,
            deal_days,
            f"no {phrase} on record"
            if deal_days is None
            else f"no {phrase} in {deal_days} days",
        )
    if client_days >= quiet_days:
        on_deal = (
            "none ever on this deal"
            if deal_days is None
            else f"none on this deal itself in {deal_days} days"
        )
        return Silence(
            QUIET, client_days, f"no {phrase} in {client_days} days ({on_deal})"
        )
    on_deal = (
        f"this deal has no {phrase} on record"
        if deal_days is None
        else f"no {phrase} on this deal in {deal_days} days"
    )
    return Silence(
        DEAL_ONLY,
        client_days,
        f"{client.name} was contacted {_ago(client_days)}, but {on_deal}",
    )


def _quiet_clients(
    client,
    last_by_deal: dict[str, datetime | None],
    *,
    now: datetime,
    quiet_days: int,
    notes: list[str],
) -> tuple[dict[str, ClientContact], bool]:
    """The client contacts of the deals that are quiet on their own.

    Only those deals need the read: a deal contacted inside the window is fine
    whatever its client's date says. Returns ``(contacts, read_ok)``. A failed
    read keeps the deal's own wording but adds a note saying the client
    records were not counted, so the answer never passes for the whole story.
    """
    quiet = [
        did
        for did, last in last_by_deal.items()
        if last is None or (now - last).days >= quiet_days
    ]
    if not quiet:
        return {}, True
    try:
        return client_contacts(client, quiet), True
    except HubSpotAPIError as exc:
        from rubik_core.logsafe import safe_error

        # safe_error, never exc_info: these are authenticated httpx calls, and
        # httpx quotes an illegal header value (a token with a stray newline)
        # verbatim in its message.
        logger.warning("[rhythm] client company read failed: %s", safe_error(exc))
        notes.append(
            "The client company records could not be read from HubSpot, so "
            "contact logged on the client but not on the deal is not counted "
            "this time."
        )
        return {}, False


# ── Loss classification (re-engagement) ──────────────────────────────────────

_BUDGET_RE = re.compile(
    r"\b(budget|price|pricing|priced|cost|costs|expensive|afford|cheaper|"
    r"half the price|funds|funding|liquid|liquidity|spend)\b",
    re.IGNORECASE,
)
_TIMING_RE = re.compile(
    r"\b(this year|next year|for now|at this time|right now|not now|in the future|"
    r"revisit|later|wait|waiting|timing|postpone|delay|delayed|on hold|hold off|"
    r"until|till|next quarter|get back to)\b",
    re.IGNORECASE,
)
# A reason that rules re-engagement out even when it also mentions timing.
_GONE_RE = re.compile(
    r"\b(acquired|merger|merged|exiting|winding down|wind down|out of business|"
    r"no longer (doing|in|operating|offering|trading)|closing (the|their) doors)\b",
    re.IGNORECASE,
)


def classify_loss(loss_reason: str | None, note: str | None) -> str | None:
    """``"budget"``, ``"timing"`` or None for one lost deal.

    HubSpot holds two fields: ``loss_reason``, a dropdown with no "Timing"
    option (Pricing, Unresponsive, Competitor, Not Interested, …), and
    ``closed_lost_reason``, the rep's free-text note ("They will pass this
    year", "Over budget", "revisit in September"). Budget is the Pricing option
    or a budget word in the note; timing is a timing phrase in the note. A note
    that says the client was acquired or left the business rules the deal out
    whatever else it says.
    """
    reason = (loss_reason or "").strip().lower()
    text = (note or "").strip()
    if reason.startswith("acquired") or (text and _GONE_RE.search(text)):
        return None
    if reason == "pricing" or (text and _BUDGET_RE.search(text)):
        return "budget"
    if text and _TIMING_RE.search(text):
        return "timing"
    return None


# ── The checks ───────────────────────────────────────────────────────────────


def closing_focus(
    client,
    scope: RhythmScope,
    *,
    today: date | None = None,
    quiet_days: int = QUIET_DAYS,
    now: datetime | None = None,
) -> CheckResult:
    """Open deals closing this month that need action now, most at risk first.

    A deal needs action when its close date is on a clock nobody is working:
    no logged client contact in a week, no next step booked, a close date that
    has already moved, or one that has already passed. Ranked by how many of
    those are true, then by amount — the "which 3 deals" (or the team's 10).
    A quiet deal whose client company was contacted inside the week still
    qualifies, worded as such (:func:`silence`), but weighs less than one
    whose whole client has gone quiet.
    """
    check = "closing_focus"
    partial = False
    today = today or _today()
    start = today.replace(day=1)
    end = _add_months(today, 1)
    deals = _open_deals(
        client,
        scope,
        [
            "dealname",
            "amount",
            "closedate",
            "dealstage",
            "hubspot_owner_id",
            "notes_last_contacted",
            "notes_next_activity_date",
        ],
        extra=[
            {"propertyName": "closedate", "operator": "GTE", "value": str(_ms(start))},
            {"propertyName": "closedate", "operator": "LT", "value": str(_ms(end))},
        ],
    )
    notes: list[str] = []
    history: dict[str, list[dict]] = {}
    if deals:
        try:
            history = _closedate_history(client, [str(d.get("id")) for d in deals])
        except HubSpotAPIError:
            logger.warning("[rhythm] closing_focus history read failed", exc_info=True)
            partial = True
            notes.append(
                "HubSpot's close-date history could not be read, so "
                "'close date already moved' is not counted this time."
            )
    names = owner_names(client)
    now = now or datetime.now(timezone.utc)
    touches, read, complete = _touches(
        client, scope, [str(d.get("id")) for d in deals], notes
    )
    partial = partial or not complete
    phrase = _contact_phrase(read)
    last_by_deal: dict[str, datetime | None] = {}
    for d in deals:
        p = d.get("properties") or {}
        did = str(d.get("id") or p.get("hs_object_id") or "")
        touch = touches.get(did)
        last_by_deal[did] = _latest(
            _parse_hs_datetime(_plausible_date(p.get("notes_last_contacted"))),
            touch.last_contact if touch else None,
        )
    clients, clients_ok = _quiet_clients(
        client, last_by_deal, now=now, quiet_days=quiet_days, notes=notes
    )
    partial = partial or not clients_ok
    ranked: list[tuple[int, int, float, str, CheckRow]] = []
    for d in deals:
        p = d.get("properties") or {}
        did = str(d.get("id") or p.get("hs_object_id") or "")
        touch = touches.get(did)
        problems: list[str] = []
        close = _local_date(p.get("closedate"))
        if close and close < today:
            problems.append(f"close date {_fmt_day(p.get('closedate'))} has passed")
        quiet = silence(
            last_by_deal.get(did),
            clients.get(did),
            now=now,
            quiet_days=quiet_days,
            phrase=phrase,
        )
        if quiet.reason:
            problems.append(quiet.reason)
        nxt = _parse_hs_datetime(_plausible_date(p.get("notes_next_activity_date")))
        booked = (nxt is not None and nxt >= now) or bool(touch and touch.next_meeting)
        if not booked:
            problems.append("no next step booked")
        pushes = close_date_pushes(history.get(did))
        if pushes:
            problems.append(
                f"close date already moved {pushes} time{'s' if pushes != 1 else ''}"
            )
        if not problems:
            continue
        oid, owner = _row_owner(names, p)
        amount = _num(p, "amount")
        reasons = [f"closes {_fmt_day(p.get('closedate'))}", *problems]
        lighter = 1 if quiet.state == DEAL_ONLY else 0
        ranked.append(
            (
                len(problems) - lighter,
                lighter,
                amount or 0.0,
                p.get("closedate") or "",
                CheckRow(
                    record_id=did,
                    object_type="deal",
                    title=p.get("dealname") or "Unnamed deal",
                    reasons=reasons,
                    amount=amount,
                    owner_id=oid,
                    owner_name=owner,
                    url=record_url("deal", did),
                    extras={"closedate": (p.get("closedate") or "")[:10]},
                ),
            )
        )
    # Most problems first; a deal whose client was contacted this week weighs
    # less than one whose whole client has gone quiet; then the biggest deal.
    ranked.sort(key=lambda t: (-t[0], -t[1], -t[2], t[3]))
    return CheckResult(
        check=check,
        rows=[r for *_, r in ranked],
        scanned=len(deals),
        notes=notes,
        partial=partial,
        sources=_sources(read),
        client_records=clients_ok,
    )


def waiting_on_decision_maker(
    client, scope: RhythmScope, *, now: datetime | None = None
) -> CheckResult:
    """Late-stage deals where the client owes the reply, for 3+ business days.

    "Our last touch" is the newer of HubSpot's logged contact
    (``notes_last_contacted``) and, on your own deals, the last email you sent
    the decision-maker. "Their reply" is the newest of HubSpot's logged reply
    (``hs_sales_email_last_replied``) and, on your own deals, their last email
    to you or a meeting with them. A deal qualifies when our last touch is
    newer than their reply and at least three business days old (the
    cockpit's own business-hours clock, so a Friday email is not "late" on
    Monday morning).

    The decision-maker is the contact HubSpot marks with the Decision Maker
    buying role. When nobody is marked, every contact on the deal stands in and
    the row says so — that is a gap worth fixing, not a reason to hide the deal.
    """
    from rubik_core.cockpit.business_hours import business_hours_between

    check = "waiting_on_decision_maker"
    now = now or datetime.now(timezone.utc)
    _, stage_labels = directory(client)
    late = _late_stage_ids(stage_labels)
    if not late:
        return _unavailable(
            check,
            CRM_READ_FAILED,
            f"HubSpot's deal stages could not be read, so {PRODUCT_NAME} cannot tell "
            "which deals are in a proposal or contract stage.",
        )
    deals = _open_deals(
        client,
        scope,
        ["dealname", "amount", "dealstage", "hubspot_owner_id", "notes_last_contacted"],
        extra=[{"propertyName": "dealstage", "operator": "IN", "values": late}],
    )
    if not deals:
        return CheckResult(check=check, rows=[], scanned=0)

    day_hours = float(getattr(settings, "COCKPIT_BUSINESS_DAY_HOURS", 8) or 8)
    threshold = WAITING_BUSINESS_DAYS * day_hours
    deal_ids = [str(d.get("id")) for d in deals]
    try:
        assoc = _retry(client.batch_associations, "deals", "contacts", deal_ids)
        contact_ids = sorted({c for ids in assoc.values() for c in ids})
        contacts = {
            str(r.get("id")): r.get("properties") or {}
            for r in _retry(
                client.batch_read,
                "contacts",
                contact_ids,
                [
                    "firstname",
                    "lastname",
                    "email",
                    "jobtitle",
                    "hs_buying_role",
                    "hs_sales_email_last_replied",
                ],
            )
        }
    except HubSpotAPIError:
        logger.warning("[rhythm] waiting-on contact read failed", exc_info=True)
        return _unavailable(
            check,
            CRM_READ_FAILED,
            "HubSpot answered, but the contacts on these deals could not be read, "
            f"so {PRODUCT_NAME} cannot tell who owes the reply.",
        )

    # Whose reply we are waiting for: the decision-maker(s), else everyone on it.
    people_by_deal: dict[str, tuple[list[dict], list[dict], list[dict]]] = {}
    for did in deal_ids:
        people = [contacts[c] for c in assoc.get(did, []) if c in contacts]
        deciders = [c for c in people if _has_role(c, "DECISION_MAKER")]
        people_by_deal[did] = (people, deciders, deciders or people)

    notes: list[str] = []
    partial = False
    read: frozenset[str] = frozenset()
    touches: dict[str, rhythm_signals.Touch] = {}
    if scope.mailbox:
        # Matched on the exact addresses of the people whose reply we await —
        # an email from someone else at the client is not the decision-maker
        # answering.
        own = scope.mailbox.rsplit("@", 1)[-1]
        terms = {
            did: rhythm_signals.exact_terms((c.get("email") for c in watch), own)
            for did, (_, _, watch) in people_by_deal.items()
        }
        report = SIGNALS(scope.mailbox, terms)
        touches = report.touches
        notes.extend(report.notes)
        partial = not report.complete
        read = frozenset(
            n
            for n, ok in (("gmail", report.gmail_ok), ("calendar", report.calendar_ok))
            if ok
        )
    else:
        notes.append(
            "A reply only counts if HubSpot logged it (Recent Sales Email Replied "
            "Date); this view does not read anyone's email, so a reply that never "
            "reached HubSpot will not show here."
        )

    names = owner_names(client)
    ranked: list[tuple[float, float, CheckRow]] = []
    for d in deals:
        p = d.get("properties") or {}
        did = str(d.get("id"))
        people, deciders, watch = people_by_deal.get(did, ([], [], []))
        touch = touches.get(did)
        ours = _latest(
            _parse_hs_datetime(_plausible_date(p.get("notes_last_contacted"))),
            touch.last_out if touch else None,
        )
        if ours is None:
            # Never contacted is a different problem (the slippage check's).
            continue
        theirs = _latest(
            *(
                _parse_hs_datetime(
                    _plausible_date(c.get("hs_sales_email_last_replied"))
                )
                for c in watch
            ),
            touch.last_in if touch else None,
            # A meeting counts as their answer only if a client invitee accepted.
            touch.last_meeting_accepted if touch else None,
        )
        if theirs is not None and theirs >= ours:
            continue  # they answered (or met with us) since — the ball is with us
        hours = business_hours_between(ours, now)
        if hours < threshold:
            continue
        marked = bool(deciders)
        biz_days = int(hours // day_hours)
        stage = stage_labels.get(str(p.get("dealstage") or ""), "a late stage")
        since = "no reply since" if "gmail" in read else "no reply logged since"
        reasons = [
            f"in {stage}; your last contact was {_fmt_day(ours.isoformat())} "
            f"({biz_days} business days, {since})"
        ]
        if marked:
            who = ", ".join(_contact_name(c) for c in deciders[:2])
            reasons.append(f"decision-maker: {who}")
        elif people:
            reasons.append(
                "no contact on this deal is marked Decision Maker in HubSpot"
            )
        else:
            reasons.append("no contacts on this deal in HubSpot")
        lead = (deciders or people or [{}])[0]
        oid, owner = _row_owner(names, p)
        amount = _num(p, "amount")
        ranked.append(
            (
                hours,
                amount or 0.0,
                CheckRow(
                    record_id=did,
                    object_type="deal",
                    title=p.get("dealname") or "Unnamed deal",
                    reasons=reasons,
                    amount=amount,
                    owner_id=oid,
                    owner_name=owner,
                    url=record_url("deal", did),
                    extras={
                        "contact_name": _contact_name(lead) if lead else "",
                        "contact_email": (lead.get("email") or "") if lead else "",
                        "business_days": biz_days,
                        "decision_maker_marked": marked,
                    },
                ),
            )
        )
    ranked.sort(key=lambda t: (-t[0], -t[1]))
    return CheckResult(
        check=check,
        rows=[r for *_, r in ranked],
        scanned=len(deals),
        notes=notes,
        partial=partial,
        sources=_sources(read),
    )


def close_date_slippage(
    client,
    scope: RhythmScope,
    *,
    quiet_days: int = QUIET_DAYS,
    now: datetime | None = None,
) -> CheckResult:
    """Open deals whose close date moved out 3+ times, or with 7 days of silence.

    The two halves are the checklist's own OR. The push count comes from
    HubSpot's close-date history; when that read fails the silence half still
    runs, and the result says only half the check ran rather than presenting
    it as the whole answer. Silence is worded from the deal and its client
    company (:func:`silence`); a deal whose client was contacted inside the
    window ranks after the deals whose whole client has gone quiet.
    """
    check = "close_date_slippage"
    partial = False
    deals = _open_deals(
        client,
        scope,
        ["dealname", "amount", "closedate", "hubspot_owner_id", "notes_last_contacted"],
    )
    notes: list[str] = []
    history: dict[str, list[dict]] = {}
    if deals:
        try:
            history = _closedate_history(client, [str(d.get("id")) for d in deals])
        except HubSpotAPIError:
            logger.warning("[rhythm] slippage history read failed", exc_info=True)
            partial = True
            notes.append(
                "HubSpot's close-date history could not be read, so only the "
                f"{quiet_days}-day silence half of this check ran."
            )
    names = owner_names(client)
    now = now or datetime.now(timezone.utc)
    touches, read, complete = _touches(
        client, scope, [str(d.get("id")) for d in deals], notes
    )
    partial = partial or not complete
    phrase = _contact_phrase(read)
    last_by_deal: dict[str, datetime | None] = {}
    for d in deals:
        p = d.get("properties") or {}
        did = str(d.get("id"))
        touch = touches.get(did)
        last_by_deal[did] = _latest(
            _parse_hs_datetime(_plausible_date(p.get("notes_last_contacted"))),
            touch.last_contact if touch else None,
        )
    clients, clients_ok = _quiet_clients(
        client, last_by_deal, now=now, quiet_days=quiet_days, notes=notes
    )
    partial = partial or not clients_ok
    #: Inside one push bucket: whole client quiet, then deal-only, then contacted.
    tier = {QUIET: 0, DEAL_ONLY: 1, CONTACT_OK: 2}
    ranked: list[tuple[int, int, int, CheckRow]] = []
    for d in deals:
        p = d.get("properties") or {}
        did = str(d.get("id"))
        pushes = close_date_pushes(history.get(did))
        quiet = silence(
            last_by_deal.get(did),
            clients.get(did),
            now=now,
            quiet_days=quiet_days,
            phrase=phrase,
        )
        if pushes < SLIPPAGE_MIN_PUSHES and not quiet.reason:
            continue
        reasons: list[str] = []
        if pushes >= SLIPPAGE_MIN_PUSHES:
            reasons.append(
                f"close date moved later {pushes} times "
                f"(now {_fmt_day(p.get('closedate'))})"
            )
        if quiet.reason:
            reasons.append(quiet.reason)
        oid, owner = _row_owner(names, p)
        ranked.append(
            (
                pushes if pushes >= SLIPPAGE_MIN_PUSHES else 0,
                tier[quiet.state],
                10_000 if quiet.days is None else quiet.days,
                CheckRow(
                    record_id=did,
                    object_type="deal",
                    title=p.get("dealname") or "Unnamed deal",
                    reasons=reasons,
                    amount=_num(p, "amount"),
                    owner_id=oid,
                    owner_name=owner,
                    url=record_url("deal", did),
                    extras={"pushes": pushes, "quiet_days": quiet.days},
                ),
            )
        )
    ranked.sort(key=lambda t: (-t[0], t[1], -t[2]))
    return CheckResult(
        check=check,
        rows=[r for *_, r in ranked],
        scanned=len(deals),
        notes=notes,
        partial=partial,
        sources=_sources(read),
        client_records=clients_ok,
    )


def single_threaded(
    client, scope: RhythmScope, *, min_amount: float = SINGLE_THREAD_MIN_AMOUNT
) -> CheckResult:
    """Open deals worth $30k+ with one contact or none — one email away from dark.

    Filtered server-side on HubSpot's ``num_associated_contacts`` count, so the
    search returns only the deals that qualify. The denominator is every open
    deal at or over the threshold; the lone contact's name is added when the
    association read succeeds.
    """
    check = "single_threaded"
    big = [
        {
            "propertyName": "amount",
            "operator": "GTE",
            "value": str(int(min_amount)),
        }
    ]
    total = _retry(
        client.count, "deals", [_OPEN, *_owner_filter(scope), *_pipeline_filter(), *big]
    )
    deals = _open_deals(
        client,
        scope,
        ["dealname", "amount", "closedate", "dealstage", "hubspot_owner_id"],
        extra=[
            *big,
            {
                "propertyName": "num_associated_contacts",
                "operator": "LTE",
                "value": "1",
            },
        ],
    )
    lone: dict[str, str] = {}
    partial = False
    if deals:
        try:
            assoc = _retry(
                client.batch_associations,
                "deals",
                "contacts",
                [str(d.get("id")) for d in deals],
            )
            ids = sorted({c for v in assoc.values() for c in v})
            people = {
                str(r.get("id")): _contact_name(r.get("properties") or {})
                for r in _retry(
                    client.batch_read,
                    "contacts",
                    ids,
                    ["firstname", "lastname", "email"],
                )
            }
            for did, cids in assoc.items():
                lone[did] = people.get(cids[0], "") if cids else ""
        except HubSpotAPIError:
            logger.info(
                "[rhythm] single-thread contact names unavailable", exc_info=True
            )
            partial = True
    names = owner_names(client)
    rows: list[CheckRow] = []
    for d in sorted(
        deals, key=lambda d: -(_num(d.get("properties") or {}, "amount") or 0)
    ):
        p = d.get("properties") or {}
        did = str(d.get("id"))
        if did in lone:
            reason = (
                f"only contact: {lone[did]}" if lone[did] else "no contacts on the deal"
            )
        else:
            reason = "one contact or none on the deal"
        oid, owner = _row_owner(names, p)
        rows.append(
            CheckRow(
                record_id=did,
                object_type="deal",
                title=p.get("dealname") or "Unnamed deal",
                reasons=[reason],
                amount=_num(p, "amount"),
                owner_id=oid,
                owner_name=owner,
                url=record_url("deal", did),
                extras={"contact_name": lone.get(did, "")},
            )
        )
    return CheckResult(
        check=check,
        rows=rows,
        scanned=total,
        partial=partial,
        notes=(
            ["The contact names could not be read from HubSpot this time."]
            if partial
            else []
        ),
    )


def reengage_lost(
    client, scope: RhythmScope, *, today: date | None = None
) -> CheckResult:
    """Deals lost 12–24 months ago to timing or budget, whose contact is likely still there.

    "Still at the company" is HubSpot's best evidence, never a certainty: the
    contact's email has not hard-bounced, and they are still linked to the
    deal's company. HubSpot cannot confirm employment, and the result says so.
    One row per contact (their biggest lost deal), so a client who passed on
    three audits gets one check-in, not three.
    """
    check = "reengage_lost"
    today = today or _today()
    # [24 months ago, 12 months ago], both days included.
    newest = months_back(today, REENGAGE_MONTHS[0]) + timedelta(days=1)
    oldest = months_back(today, REENGAGE_MONTHS[1])
    lost = _retry(
        client.search_all,
        "deals",
        [
            {"propertyName": "hs_is_closed", "operator": "EQ", "value": "true"},
            {"propertyName": "hs_is_closed_won", "operator": "EQ", "value": "false"},
            {"propertyName": "closedate", "operator": "GTE", "value": str(_ms(oldest))},
            {"propertyName": "closedate", "operator": "LT", "value": str(_ms(newest))},
            *_owner_filter(scope),
            *_pipeline_filter(),
        ],
        [
            "dealname",
            "amount",
            "closedate",
            "hubspot_owner_id",
            "loss_reason",
            "closed_lost_reason",
        ],
    )
    candidates: list[tuple[dict, str]] = []
    for d in lost:
        p = d.get("properties") or {}
        kind = classify_loss(p.get("loss_reason"), p.get("closed_lost_reason"))
        if kind:
            candidates.append((d, kind))
    candidates.sort(key=lambda t: -(_num(t[0].get("properties") or {}, "amount") or 0))
    notes = [
        "'Still at the company' is HubSpot's best evidence (their email has not "
        "hard-bounced and they are still linked to the company); HubSpot cannot "
        "confirm employment, so glance at LinkedIn before you write."
    ]
    if len(candidates) > REENGAGE_MAX_CANDIDATES:
        notes.append(
            f"Checked the {REENGAGE_MAX_CANDIDATES} largest of the "
            f"{len(candidates)} deals lost over timing or budget; the smaller "
            "ones were not checked for a reachable contact."
        )
        candidates = candidates[:REENGAGE_MAX_CANDIDATES]
    if not candidates:
        return CheckResult(check=check, rows=[], scanned=len(lost), notes=notes)

    deal_ids = [str(d.get("id")) for d, _ in candidates]
    try:
        to_contacts = _retry(client.batch_associations, "deals", "contacts", deal_ids)
        to_companies = _retry(client.batch_associations, "deals", "companies", deal_ids)
        contact_ids = sorted({c for v in to_contacts.values() for c in v})
        company_ids = sorted({c for v in to_companies.values() for c in v})
        contacts = {
            str(r.get("id")): r.get("properties") or {}
            for r in _retry(
                client.batch_read,
                "contacts",
                contact_ids,
                [
                    "firstname",
                    "lastname",
                    "email",
                    "jobtitle",
                    "hs_buying_role",
                    "hs_email_hard_bounce_reason_enum",
                    "associatedcompanyid",
                ],
            )
        }
        companies = {
            str(r.get("id")): (r.get("properties") or {}).get("name") or ""
            for r in _retry(client.batch_read, "companies", company_ids, ["name"])
        }
    except HubSpotAPIError:
        logger.warning("[rhythm] re-engagement contact read failed", exc_info=True)
        return _unavailable(
            check,
            CRM_READ_FAILED,
            "HubSpot answered, but the contacts on these lost deals could not be "
            f"read, so {PRODUCT_NAME} cannot tell who is still there.",
        )

    names = owner_names(client)
    by_contact: dict[str, CheckRow] = {}
    for d, kind in candidates:
        p = d.get("properties") or {}
        did = str(d.get("id"))
        deal_companies = to_companies.get(did, [])
        people = [(c, contacts[c]) for c in to_contacts.get(did, []) if c in contacts]
        # The decision-maker first, then anyone, as the person to write to.
        people.sort(key=lambda cp: not _has_role(cp[1], "DECISION_MAKER"))
        chosen: tuple[str, dict] | None = None
        for cid, c in people:
            if not (c.get("email") or "").strip():
                continue
            if c.get("hs_email_hard_bounce_reason_enum"):
                continue  # their address bounces — very likely gone
            company = str(c.get("associatedcompanyid") or "")
            if deal_companies and company and company not in deal_companies:
                continue  # linked to a different company now — they moved
            chosen = (cid, c)
            break
        if chosen is None:
            continue
        cid, c = chosen
        company = str(c.get("associatedcompanyid") or "")
        linked = bool(deal_companies and company and company in deal_companies)
        company_name = next(
            (companies[x] for x in deal_companies if companies.get(x)), ""
        )
        note_text = (p.get("closed_lost_reason") or "").strip()
        why = "budget" if kind == "budget" else "timing"
        reason = f"lost {_fmt_day(p.get('closedate'))} over {why}"
        if note_text:
            reason += f' — "{_strip_html(note_text, 90)}"'
        title_bits = [x for x in (c.get("jobtitle"), company_name) if x]
        contact_line = f"contact: {_contact_name(c)}" + (
            f" ({', '.join(title_bits)})" if title_bits else ""
        )
        oid, owner = _row_owner(names, p)
        row = CheckRow(
            record_id=did,
            object_type="deal",
            title=p.get("dealname") or "Unnamed deal",
            reasons=[
                reason,
                contact_line,
                "email still delivering, still linked to the company"
                if linked
                else "email still delivering (no company link on record to check)",
            ],
            amount=_num(p, "amount"),
            owner_id=oid,
            owner_name=owner,
            url=record_url("deal", did),
            extras={
                "contact_id": cid,
                "contact_name": _contact_name(c),
                "contact_email": c.get("email") or "",
                "company_name": company_name,
                "loss_kind": kind,
                "lost_on": (p.get("closedate") or "")[:10],
                "other_deals": [],
            },
        )
        held = by_contact.get(cid)
        if held is None:
            by_contact[cid] = row
        else:
            held.extras["other_deals"].append(row.title)
    rows = sorted(by_contact.values(), key=lambda r: -(r.amount or 0))
    return CheckResult(check=check, rows=rows, scanned=len(lost), notes=notes)


def missing_fields(
    client, scope: RhythmScope, *, day: date | None = None
) -> CheckResult:
    """Contacts and open deals touched today that are missing a key field.

    "Touched" is HubSpot's own record of who created or last updated a record
    today (``hs_created_by_user_id`` / ``hs_updated_by_user_id``) — a different
    id from the owner id, which is why the scope carries user ids.

    Fields checked: a contact's Job title and Buying role; a deal's next step,
    amount and close date. Persona is the checklist's field; Cubie reads Buying
    role in its place. HubSpot has no "Next steps" field, so a booked next
    activity is what counts as the deal's next step.
    """
    check = "missing_fields"
    if not scope.team and not scope.user_ids:
        return _unavailable(
            check,
            NO_HUBSPOT_USER,
            f"{PRODUCT_NAME} could not find your HubSpot user id, so it cannot tell "
            "which records you touched today.",
        )
    day = day or _today()
    start, end = _local_day_bounds(day)

    def groups(created_at: str, modified_at: str) -> list[list[dict]]:
        window_c = [
            {"propertyName": created_at, "operator": "GTE", "value": str(start)},
            {"propertyName": created_at, "operator": "LT", "value": str(end)},
        ]
        window_m = [
            {"propertyName": modified_at, "operator": "GTE", "value": str(start)},
            {"propertyName": modified_at, "operator": "LT", "value": str(end)},
        ]
        if scope.user_ids:
            by_c = _ids_filter("hs_created_by_user_id", scope.user_ids)
            by_m = _ids_filter("hs_updated_by_user_id", scope.user_ids)
        else:  # the team: anyone, as long as a person (not a workflow) did it
            by_c = [
                {"propertyName": "hs_created_by_user_id", "operator": "HAS_PROPERTY"}
            ]
            by_m = [
                {"propertyName": "hs_updated_by_user_id", "operator": "HAS_PROPERTY"}
            ]
        return [[*window_c, *by_c], [*window_m, *by_m]]

    contacts = _retry(
        client.search_all_groups,
        "contacts",
        groups("createdate", "lastmodifieddate"),
        [
            "firstname",
            "lastname",
            "email",
            "jobtitle",
            "hs_buying_role",
            "hubspot_owner_id",
        ],
    )
    deal_groups = [
        [*g, _OPEN, *_pipeline_filter()]
        for g in groups("createdate", "hs_lastmodifieddate")
    ]
    deals = _retry(
        client.search_all_groups,
        "deals",
        deal_groups,
        [
            "dealname",
            "amount",
            "closedate",
            "notes_next_activity_date",
            "hubspot_owner_id",
        ],
    )
    notes = [
        "'Touched today' means created by you today, or last updated by you with "
        f"a change today. {PRODUCT_NAME} reads Buying role in place of Persona. "
        "HubSpot has no 'Next steps' field, so a booked next activity counts as "
        "the deal's next step."
    ]
    cap = int(getattr(client, "MAX_RECORDS", 5000))
    capped = len(contacts) >= cap or len(deals) >= cap
    if capped:
        notes.append(
            f"HubSpot returned the first {cap} records touched today; any beyond "
            "that were not checked."
        )
    # The touched-contact search is portal-wide, even when its user id matches
    # a workspace rep. A contact's name/email may be linked to another deal.
    contact_ids = [str(row.get("id")) for row in contacts if row.get("id")]
    owned_contacts = _retry(
        client.fully_owned_deal_associations, "contacts", contact_ids
    )
    contacts = [row for row in contacts if owned_contacts.get(str(row.get("id")))]
    names = owner_names(client)
    now = datetime.now(timezone.utc)
    rows: list[CheckRow] = []
    seen: set[str] = set()
    for c in contacts:
        cid = str(c.get("id"))
        if cid in seen:
            continue
        seen.add(cid)
        p = c.get("properties") or {}
        gaps = []
        if not (p.get("jobtitle") or "").strip():
            gaps.append("no job title")
        if not (p.get("hs_buying_role") or "").strip():
            gaps.append("no buying role")
        if gaps:
            oid, owner = _row_owner(names, p)
            rows.append(
                CheckRow(
                    record_id=cid,
                    object_type="contact",
                    title=_contact_name(p),
                    reasons=gaps,
                    owner_id=oid,
                    owner_name=owner,
                    url=record_url("contact", cid),
                )
            )

    def _booked_in_hubspot(props: dict) -> bool:
        nxt = _parse_hs_datetime(_plausible_date(props.get("notes_next_activity_date")))
        return nxt is not None and nxt >= now

    # On your own deals an upcoming meeting with the client is a booked next
    # step too; only the deals HubSpot shows nothing for are checked.
    unbooked = [
        str(d.get("id"))
        for d in deals
        if not _booked_in_hubspot(d.get("properties") or {})
    ]
    # Calendar only: "is a meeting booked" needs no mailbox search.
    touches, read, complete = _touches(
        client, scope, list(dict.fromkeys(unbooked)), notes, gmail=False
    )
    deal_seen: set[str] = set()
    for d in deals:
        did = str(d.get("id"))
        if did in deal_seen:
            continue
        deal_seen.add(did)
        p = d.get("properties") or {}
        gaps = []
        touch = touches.get(did)
        if not _booked_in_hubspot(p) and not (touch and touch.next_meeting):
            gaps.append("no next step booked")
        if _num(p, "amount") is None:
            gaps.append("no amount")
        if not p.get("closedate"):
            gaps.append("no close date")
        if gaps:
            oid, owner = _row_owner(names, p)
            rows.append(
                CheckRow(
                    record_id=did,
                    object_type="deal",
                    title=p.get("dealname") or "Unnamed deal",
                    reasons=gaps,
                    amount=_num(p, "amount"),
                    owner_id=oid,
                    owner_name=owner,
                    url=record_url("deal", did),
                )
            )
    rows.sort(key=lambda r: (-len(r.reasons), r.object_type != "deal", r.title.lower()))
    return CheckResult(
        check=check,
        rows=rows,
        scanned=len(seen) + len(deal_seen),
        notes=notes,
        partial=capped or not complete,
        sources=_sources(read),
    )


def _ids_filter(prop: str, ids: Sequence[str]) -> list[dict]:
    ids = [i for i in ids if i]
    if len(ids) == 1:
        return [{"propertyName": prop, "operator": "EQ", "value": ids[0]}]
    return [{"propertyName": prop, "operator": "IN", "values": list(ids)}]


def overdue(
    client,
    scope: RhythmScope,
    *,
    today: date | None = None,
    include_tasks: bool = True,
) -> CheckResult:
    """Past-due HubSpot tasks and open deals whose close date has passed.

    The team view leaves tasks out (``include_tasks=False``): a list of
    colleagues' overdue to-dos is a view of people, while a list of deals past
    their close date is a view of the pipeline.
    """
    check = "overdue"
    today = today or _today()
    start, _ = _local_day_bounds(today)
    rows: list[tuple[int, CheckRow]] = []
    names = owner_names(client)
    scanned = 0
    if include_tasks:
        open_tasks = [
            {"propertyName": "hs_task_status", "operator": "NEQ", "value": "COMPLETED"},
            *_owner_filter(scope),
        ]
        # A portal-wide task count reveals another workspace's backlog. Read
        # the complete open set through the strict association proof, then
        # count and select overdue tasks from those owned records only.
        tasks = _retry(
            client.search_all_owned,
            "tasks",
            open_tasks,
            [
                "hs_task_subject",
                "hs_timestamp",
                "hs_task_status",
                "hs_task_priority",
                "hubspot_owner_id",
            ],
        )
        scanned += len(tasks)
        for t in tasks:
            p = t.get("properties") or {}
            tid = str(t.get("id"))
            due = _local_date(p.get("hs_timestamp"))
            if due is None or due >= today:
                continue
            days = (today - due).days
            oid, owner = _row_owner(names, p)
            rows.append(
                (
                    days,
                    CheckRow(
                        record_id=tid,
                        object_type="task",
                        title=p.get("hs_task_subject") or "Untitled task",
                        reasons=[
                            f"task due {_fmt_day(p.get('hs_timestamp'))} ({days} day{'s' if days != 1 else ''} overdue)"
                        ],
                        owner_id=oid,
                        owner_name=owner,
                        url=record_url("task", tid),
                    ),
                )
            )
    scanned += _retry(
        client.count, "deals", [_OPEN, *_owner_filter(scope), *_pipeline_filter()]
    )
    deals = _open_deals(
        client,
        scope,
        ["dealname", "amount", "closedate", "hubspot_owner_id"],
        extra=[{"propertyName": "closedate", "operator": "LT", "value": str(start)}],
    )
    for d in deals:
        p = d.get("properties") or {}
        did = str(d.get("id"))
        close = _local_date(p.get("closedate"))
        days = (today - close).days if close else 0
        oid, owner = _row_owner(names, p)
        rows.append(
            (
                days,
                CheckRow(
                    record_id=did,
                    object_type="deal",
                    title=p.get("dealname") or "Unnamed deal",
                    reasons=[
                        f"close date {_fmt_day(p.get('closedate'))} passed {days} day{'s' if days != 1 else ''} ago"
                    ],
                    amount=_num(p, "amount"),
                    owner_id=oid,
                    owner_name=owner,
                    url=record_url("deal", did),
                ),
            )
        )
    rows.sort(key=lambda t: -t[0])
    return CheckResult(check=check, rows=[r for _, r in rows], scanned=scanned)


#: Engagement objects read for the end-of-day recap, with the properties that
#: say what happened. Bodies are trimmed to a sentence: the recap needs the
#: gist, and a full email body does not belong in a manager update.
_ACTIVITY_PROPS: dict[str, list[str]] = {
    "calls": [
        "hs_call_title",
        "hs_call_disposition",
        "hs_call_direction",
        "hs_call_body",
    ],
    "emails": ["hs_email_subject", "hs_email_direction"],
    "meetings": ["hs_meeting_title", "hs_meeting_outcome", "hs_meeting_body"],
    "notes": ["hs_note_body"],
}
_ACTIVITY_KIND = {
    "calls": "call",
    "emails": "email",
    "meetings": "meeting",
    "notes": "note",
}


def activity_today(
    client, scope: RhythmScope, *, day: date | None = None
) -> CheckResult:
    """What was logged today: calls, emails, meetings, notes, and deal stage moves.

    The raw material for the end-of-day manager update — the chat model turns
    it into bullets. Each activity is labelled with the deal it was logged
    against where HubSpot has that link. An object type that cannot be read
    (the private-app token may lack a scope) is named in ``notes`` rather than
    silently missing from the recap.
    """
    check = "activity_today"
    day = day or _today()
    start, end = _local_day_bounds(day)
    window = [
        {"propertyName": "hs_timestamp", "operator": "GTE", "value": str(start)},
        {"propertyName": "hs_timestamp", "operator": "LT", "value": str(end)},
    ]
    notes: list[str] = []
    failed: list[str] = []
    found: list[tuple[str, dict]] = []
    for obj, props in _ACTIVITY_PROPS.items():
        try:
            for row in _retry(
                client.search_all_owned,
                obj,
                [*window, *_owner_filter(scope)],
                ["hs_timestamp", *props],
            ):
                found.append((obj, row))
        except HubSpotAPIError:
            logger.info("[rhythm] %s unreadable for the recap", obj, exc_info=True)
            failed.append(obj)
            notes.append(
                f"Logged {obj} could not be read from HubSpot, so they are not in this recap."
            )

    deal_for: dict[str, str] = {}
    by_obj: dict[str, list[str]] = {}
    for obj, row in found:
        by_obj.setdefault(obj, []).append(str(row.get("id")))
    deal_names: dict[str, str] = {}
    try:
        for obj, ids in by_obj.items():
            for src, deals in _retry(
                client.batch_associations, obj, "deals", ids
            ).items():
                if deals:
                    deal_for[f"{obj}:{src}"] = deals[0]
        wanted = sorted(set(deal_for.values()))
        deal_names = {
            str(r.get("id")): (r.get("properties") or {}).get("dealname") or ""
            for r in _retry(client.batch_read, "deals", wanted, ["dealname"])
        }
    except HubSpotAPIError:
        logger.info("[rhythm] recap deal labels unavailable", exc_info=True)
        failed.append("deal labels")

    rows: list[CheckRow] = []
    for obj, row in found:
        p = row.get("properties") or {}
        rid = str(row.get("id"))
        kind = _ACTIVITY_KIND[obj]
        if obj == "calls":
            title = p.get("hs_call_title") or "Call"
            detail = [
                x
                for x in (
                    p.get("hs_call_disposition"),
                    _strip_html(p.get("hs_call_body")),
                )
                if x
            ]
        elif obj == "emails":
            title = p.get("hs_email_subject") or "Email"
            direction = (p.get("hs_email_direction") or "").upper()
            detail = ["received" if direction.startswith("INCOMING") else "sent"]
        elif obj == "meetings":
            title = p.get("hs_meeting_title") or "Meeting"
            detail = [
                x
                for x in (
                    (p.get("hs_meeting_outcome") or "").replace("_", " ").lower()
                    or None,
                    _strip_html(p.get("hs_meeting_body")),
                )
                if x
            ]
        else:
            title = "Note"
            detail = [_strip_html(p.get("hs_note_body"))]
        deal = deal_names.get(deal_for.get(f"{obj}:{rid}", ""), "")
        if deal:
            detail.insert(0, f"on {deal}")
        rows.append(
            CheckRow(
                record_id=rid,
                object_type=kind,
                title=title,
                reasons=detail,
                extras=(
                    {"direction": "received"}
                    if obj == "emails"
                    and (p.get("hs_email_direction") or "")
                    .upper()
                    .startswith("INCOMING")
                    else {}
                ),
            )
        )

    # Stage moves: deals touched today whose dealstage history has an entry today.
    try:
        _, stage_labels = directory(client)
        touched = _open_or_closed_touched_today(client, scope, start, end)
        if touched:
            hist_rows = _retry(
                client.batch_read,
                "deals",
                [str(d.get("id")) for d in touched],
                ["dealname", "dealstage"],
                properties_with_history=["dealstage"],
            )
            for r in hist_rows:
                hist = (r.get("propertiesWithHistory") or {}).get("dealstage") or []
                moves = _moves_today(hist, start, end)
                if not moves:
                    continue
                before, after = moves
                p = r.get("properties") or {}
                rows.append(
                    CheckRow(
                        record_id=str(r.get("id")),
                        object_type="stage_change",
                        title=p.get("dealname") or "Unnamed deal",
                        reasons=[
                            f"moved from {stage_labels.get(before, before or 'no stage')} "
                            f"to {stage_labels.get(after, after)}"
                        ],
                    )
                )
    except HubSpotAPIError:
        logger.info("[rhythm] recap stage history unavailable", exc_info=True)
        failed.append("stage changes")
        notes.append(
            "Deal stage changes could not be read from HubSpot, so they are not in this recap."
        )

    # Your own day also includes what never reached HubSpot: emails you sent
    # to people outside the company, and meetings you had with them. Subjects and
    # titles only; anything already logged (same title) is not listed twice.
    read: frozenset[str] = frozenset()
    if scope.mailbox:
        # Only what YOU sent or held: a logged INCOMING email with the same
        # subject is the client's message, not yours.
        logged = {
            re.sub(r"\W+", " ", r.title).strip().lower()
            for r in rows
            if r.object_type == "meeting"
            or (r.object_type == "email" and r.extras.get("direction") != "received")
        }
        day_start = datetime.fromtimestamp(start / 1000, tz=timezone.utc)
        day_end = datetime.fromtimestamp(end / 1000, tz=timezone.utc)
        act = DAY_ACTIVITY(scope.mailbox, day_start, day_end)
        notes.extend(act.notes)
        if not act.complete:
            failed.append("your email or calendar")
        read = frozenset(
            n
            for n, ok in (("gmail", act.gmail_ok), ("calendar", act.calendar_ok))
            if ok
        )
        for i, mail in enumerate(act.emails):
            if re.sub(r"\W+", " ", mail["subject"]).strip().lower() in logged:
                continue
            rows.append(
                CheckRow(
                    record_id=f"gmail-{i}",
                    object_type="email",
                    title=mail["subject"],
                    reasons=[
                        "sent to " + ", ".join(mail["to"][:3]),
                        "from your Gmail, not logged in HubSpot",
                    ],
                )
            )
        for i, meet in enumerate(act.meetings):
            if re.sub(r"\W+", " ", meet["title"]).strip().lower() in logged:
                continue
            rows.append(
                CheckRow(
                    record_id=f"calendar-{i}",
                    object_type="meeting",
                    title=meet["title"],
                    reasons=[
                        "with " + ", ".join(meet["with"][:3]),
                        "from your calendar, not logged in HubSpot",
                    ],
                )
            )

    if (
        not rows
        and all(o in failed for o in _ACTIVITY_PROPS)
        and "stage changes" in failed
    ):
        # Every read failed: that is "could not look", never "nothing logged".
        return _unavailable(
            check,
            CRM_READ_FAILED,
            "HubSpot could not be read, so today's activity is unknown — not empty.",
        )
    return CheckResult(
        check=check,
        rows=rows,
        scanned=len(rows),
        notes=notes,
        partial=bool(failed),
        sources=_sources(read),
    )


def _open_or_closed_touched_today(
    client, scope: RhythmScope, start: int, end: int
) -> list[dict]:
    return _retry(
        client.search_all,
        "deals",
        [
            {
                "propertyName": "hs_lastmodifieddate",
                "operator": "GTE",
                "value": str(start),
            },
            {
                "propertyName": "hs_lastmodifieddate",
                "operator": "LT",
                "value": str(end),
            },
            *_owner_filter(scope),
            *_pipeline_filter(),
        ],
        ["dealname"],
    )


def _moves_today(history: list[dict], start: int, end: int) -> tuple[str, str] | None:
    """(stage before today, stage now) when the stage changed today, else None."""
    entries = []
    for e in history:
        when = _parse_hs_datetime(e.get("timestamp"))
        if when is None:
            continue
        entries.append((int(when.timestamp() * 1000), str(e.get("value") or "")))
    entries.sort()
    today_moves = [v for ts, v in entries if start <= ts < end]
    if not today_moves:
        return None
    before = ""
    for ts, v in entries:
        if ts < start:
            before = v
    after = today_moves[-1]
    if after == before:
        return None
    return before, after


# ── Registry and the one entry point ─────────────────────────────────────────

CHECKS: dict[str, Callable[..., CheckResult]] = {
    "closing_focus": closing_focus,
    "waiting_on_decision_maker": waiting_on_decision_maker,
    "close_date_slippage": close_date_slippage,
    "single_threaded": single_threaded,
    "reengage_lost": reengage_lost,
    "missing_fields": missing_fields,
    "overdue": overdue,
    "activity_today": activity_today,
}


def clear_cache() -> None:
    """Drop cached directory labels (tests, and nothing else)."""
    with _dir_lock:
        _dir_cache.clear()


@pin_business_time_zone
def run_check(name: str, client, scope: RhythmScope, **kwargs: Any) -> CheckResult:
    """Run one check against live CRM ownership. Never raises.

    A HubSpot failure becomes an unavailable result with a reason — the caller
    decides how to say "Cubie could not look", but it can never mistake the
    failure for an empty list.
    """
    fn = CHECKS[name]
    try:
        config = get_hubspot_config()
    except Exception:  # noqa: BLE001 — unavailable control config is not an empty answer
        logger.exception("[rhythm] workspace config unavailable")
        return _unavailable(
            name, CRM_UNAVAILABLE, "Workspace CRM settings are unavailable."
        )
    if not config.pipeline_ids:
        return _unavailable(
            name, CRM_UNAVAILABLE, "Workspace deal pipelines are not configured."
        )
    try:
        result = fn(client, scope, **kwargs)
    except HubSpotAPIError:
        logger.warning("[rhythm] %s failed", name, exc_info=True)
        return _unavailable(
            name,
            CRM_READ_FAILED,
            "HubSpot answered, but the search behind this check did not finish.",
        )
    except Exception:  # noqa: BLE001 — a check must never take a page down
        logger.exception("[rhythm] %s crashed", name)
        return _unavailable(
            name, CRM_READ_FAILED, "This check hit an unexpected error."
        )
    return result


# ── Chat formatting ──────────────────────────────────────────────────────────

#: Per check: (heading, what ``scanned`` counts, what an empty list means).
CHAT_COPY: dict[str, tuple[str, str, str]] = {
    "closing_focus": (
        "deals closing this month that need action today",
        "open deals closing this month",
        "Every deal closing this month has a recent client contact and a next step booked.",
    ),
    "waiting_on_decision_maker": (
        "late-stage deals waiting on the client for 3+ business days",
        "open deals in a proposal, SOW, legal or contract stage",
        "No proposal-stage deal has been waiting on the client for 3+ business days.",
    ),
    "close_date_slippage": (
        "deals whose close date keeps moving, or that have gone quiet",
        "open deals",
        "No open deal has had its close date pushed 3+ times or gone 7 days without a logged contact.",
    ),
    "single_threaded": (
        f"open deals of {_fmt_money(SINGLE_THREAD_MIN_AMOUNT)}+ with only one contact",
        f"open deals of {_fmt_money(SINGLE_THREAD_MIN_AMOUNT)} or more",
        f"Every open deal of {_fmt_money(SINGLE_THREAD_MIN_AMOUNT)}+ has at least two contacts.",
    ),
    "reengage_lost": (
        "lost deals worth a check-in (lost 12–24 months ago to timing or budget)",
        "deals lost 12–24 months ago",
        "No deal lost 12–24 months ago over timing or budget has a contact who is likely still there.",
    ),
    "missing_fields": (
        "records touched today that are missing a key field",
        "contacts and open deals created or updated today",
        "Every contact and deal touched today has its key fields filled in.",
    ),
    "overdue": (
        "overdue tasks and past-due close dates",
        "open tasks and open deals",
        "Nothing is past due — no overdue tasks and no open deal with a passed close date.",
    ),
    "activity_today": (
        "today's logged activity",
        "activities and stage moves logged today",
        "Nothing has been logged in HubSpot today yet — no calls, emails, meetings, notes or stage moves.",
    ),
}


def chat_copy(
    check: str,
    *,
    min_amount: float | None = None,
    quiet_days: int | None = None,
    own: bool = False,
) -> tuple[str, str, str]:
    """:data:`CHAT_COPY` with any threshold the question changed written in.

    "Single-threaded deals over $50k" must not come back headed "$30,000+":
    a heading that names the wrong threshold reads as the wrong answer.
    """
    heading, noun, empty = CHAT_COPY.get(check, (check, "records", "Nothing found."))
    if own and check == "activity_today":
        return (
            "today's activity (HubSpot, plus your own sent email and calendar)",
            "activities, emails, meetings and stage moves from today",
            "Nothing today yet — nothing logged in HubSpot, no email sent to a client "
            "and no client meeting on your calendar.",
        )
    if own and check == "close_date_slippage":
        empty = (
            "No open deal has had its close date pushed 3+ times or gone "
            f"{quiet_days or QUIET_DAYS} days without client contact."
        )
    if (
        check == "single_threaded"
        and min_amount
        and min_amount != SINGLE_THREAD_MIN_AMOUNT
    ):
        m = _fmt_money(min_amount)
        return (
            f"open deals of {m}+ with only one contact",
            f"open deals of {m} or more",
            f"Every open deal of {m}+ has at least two contacts.",
        )
    if (
        check in ("close_date_slippage", "closing_focus")
        and quiet_days
        and quiet_days != QUIET_DAYS
    ):
        contact = "client contact" if own else "a logged contact"
        if check == "close_date_slippage":
            empty = (
                "No open deal has had its close date pushed 3+ times or gone "
                f"{quiet_days} days without {contact}."
            )
        return (
            heading + f" (quiet = {quiet_days}+ days without {contact})",
            noun,
            empty,
        )
    return heading, noun, empty


#: Checks whose answer changes when unlogged emails and meetings count.
_CONTACT_CHECKS = frozenset(
    {
        "closing_focus",
        "waiting_on_decision_maker",
        "close_date_slippage",
        "missing_fields",
    }
)


def _provenance(result: CheckResult, scope: RhythmScope) -> str:
    """The closing line: which sources this answer actually counted."""
    check = result.check
    uses_signals = check in _CONTACT_CHECKS or check == "activity_today"
    if scope.mailbox and uses_signals:
        read = set(result.sources) - {"hubspot"}
        wanted = {"calendar"} if check == "missing_fields" else {"gmail", "calendar"}
        missing = wanted - read
        names = {"gmail": "Gmail", "calendar": "calendar"}
        if not read:
            return (
                "(This time it counted only what is logged in HubSpot — your "
                + " and ".join(names[m] for m in sorted(missing))
                + " could not be read.)"
            )
        counted = " and ".join(names[r] for r in sorted(read))
        logged = (
            "what is logged in HubSpot, on the deal and on the client's company record,"
            if result.client_records
            else "what is logged in HubSpot"
        )
        line = f"(Counts {logged} plus your own {counted}"
        if missing:
            line += (
                "; your "
                + " and ".join(names[m] for m in sorted(missing))
                + " could not be read this time"
            )
        if "gmail" in wanted:
            line += (
                ". A phone call only counts if it is logged in HubSpot or on your "
                "calendar"
            )
        return line + ".)"
    if check in _CONTACT_CHECKS:
        view = "Team view" if scope.team else "This view"
        if result.client_records:
            # Not "unlogged email": contact logged on the client's company
            # record DOES count here, so the caveat must not send the reader
            # looking for email nobody logged.
            return (
                f"({view}: counts only what is logged in HubSpot, on the deal and "
                "on the client's company record, so an email logged against anyone "
                "at the client counts. It does not read anyone's email or "
                "calendar, so contact never logged in HubSpot cannot count.)"
            )
        return (
            f"({view}: counts only what is logged in HubSpot. It does not read "
            "anyone's email or calendar, so a deal a rep has been emailing without "
            "logging it can show as quiet here.)"
        )
    return "(Reflects what is logged in HubSpot.)"


def format_for_chat(
    result: CheckResult,
    scope: RhythmScope,
    *,
    limit: int = 20,
    copy: tuple[str, str, str] | None = None,
) -> str:
    """A CRM context block the chat model narrates. Denominator first, always."""
    heading, noun, empty = copy or chat_copy(result.check, own=bool(scope.mailbox))
    who = (
        "scope: your team — every deal owner, the company's own pipelines"
        if scope.team
        else f"owner: {scope.label}"
    )
    head = f"--- HubSpot CRM (live) — {heading} ({who}) ---"
    if not result.available:
        why = " ".join(result.notes) or "HubSpot could not be read for this check."
        return f"{head}\nI couldn't run this check: {why}"
    lines = [head]
    if result.check == "activity_today":
        lines.append(f"{len(result.rows)} {noun}.")
    else:
        lines.append(f"{len(result.rows)} of {result.scanned} {noun} qualify.")
    if not result.rows:
        lines.append(empty)
    shown = result.rows[:limit]
    for i, row in enumerate(shown, 1):
        bits = [row.title]
        if row.amount is not None:
            bits.append(_fmt_money(row.amount))
        bits.extend(row.reasons)
        if scope.team and row.owner_name:
            bits.append(f"owner {row.owner_name}")
        if row.url:
            bits.append(row.url)
        others = row.extras.get("other_deals") if row.extras else None
        if others:
            bits.append(f"also lost: {', '.join(others[:3])}")
        lines.append(f"{i}. " + " | ".join(str(b) for b in bits if b))
    if len(result.rows) > len(shown):
        lines.append(f"…and {len(result.rows) - len(shown)} more.")
    for note in result.notes:
        lines.append(f"Note: {note}")
    lines.append(_provenance(result, scope))
    return "\n".join(lines)

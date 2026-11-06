"""The detection and closure rules — pure functions, no I/O (README §4.4–4.5).

Everything here takes already-fetched data (parsed Gmail threads, the run's
HubSpot index, Slack messages, promise rows) and returns decisions, so every
threshold, grace period and "could not look" rule is unit-tested without
Google, Slack, HubSpot or a model. ``scan.py`` fetches; ``store.py`` writes;
this module only decides.

The rules, in one place:

* **unlogged (email)** — a thread with a message SENT BY THE AE to an external
  address that resolves to a HubSpot contact (or to a client domain), older
  than ``LOG_GRACE_BUSINESS_DAYS``, with no engagement on the contact / its
  company / its deals at or after ``sent − 1 day``. ``opened_at`` is the
  earliest such message. The day of slack on the engagement side forgives a
  call logged the evening before the follow-up email.
* **unlogged (Slack)** — the AE's own Slack message names a company on one of
  their open deals (normalised name match, legal suffixes stripped, at
  least 4 characters), the judge says it reports a client conversation, and
  nothing is logged on the company or its deals at or after ``message − 1
  day``. The same log grace applies, so a call mentioned at 4pm is not flagged
  before the AE had a working day to log it.
* **open loop** — the thread's newest message is from an external, non-bulk
  person with the AE in **To**, received ``OPEN_LOOP_BUSINESS_DAYS`` or more
  business days ago (the cockpit clock), with no later AE message and no
  follow-up logged on the contact / company since. The judge then decides
  whether it really expects a reply.
* **todo** — heard promises (``HEARD_SOURCES``, still suggested / open /
  slipping, confidence ≥ 0.75) ``TODO_GRACE_DAYS`` past their date, or undated
  and said ``TODO_UNDATED_DAYS`` ago; and the AE's HubSpot tasks that many days
  overdue. One whose date later moves inside the grace closes ``rescheduled``
  (not counted as fixed) — only when the date moved later: a raised grace
  leaves an item flagged under the old one open, and ``views.listed`` hides it
  until it crosses the new line. A task reassigned to another owner closes
  ``task_gone``.

Three invariants every function here keeps:

1. **"Could not look" decides nothing.** An unavailable HubSpot index yields no
   unlogged finding and no HubSpot-based closure; an unreadable mailbox yields
   no "replied". A capped (partial) index refuses an unlogged finding only
   where the missing evidence could be the cap's fault.
2. **Nothing is flagged older than the max age.** It would be created and
   aged out in the same breath.
3. **Message text never leaves through ``detail``.** Candidates carry ids,
   flags and timestamps there; the subject goes to ``title`` (owner-only).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from rubik_core.loop_closure.gmail_reader import SKIPPED_LABELS, Message, Thread
from rubik_core.loop_closure.hubspot_index import HubSpotIndex, Match

#: Heard promises below this confidence never become a loose end. Higher than
#: the listener's own write bar (0.7): accusing someone of breaking a promise
#: they never made is worse than missing one.
MIN_PROMISE_CONFIDENCE = 0.75
#: How far back the AE's own Slack messages are read (README §4.4).
SLACK_DAYS = 7
#: Shortest normalised company name a Slack message may match on.
MIN_COMPANY_NAME = 4
#: How many engagement-index keys an item remembers for its closure check.
MAX_RECORDS = 20

_ENGAGEMENT_SLACK = timedelta(days=1)

# Statuses copied from rubik_core/promises/store.py (HEARD_SOURCES,
# UNKEPT_CANDIDATE_STATUSES) and database/promises.py so this module stays
# import-light; test_loop_closure_detect pins them to the originals.
HEARD_SOURCES: tuple[str, ...] = ("heard_transcript", "heard_email", "heard_slack")
UNKEPT_STATUSES: tuple[str, ...] = ("suggested", "open", "slipping")
KEPT_STATUSES: tuple[str, ...] = ("kept", "evidence_found")
WITHDRAWN_STATUSES: tuple[str, ...] = ("dropped", "handed_off")

_EXTRA_AUTOMATED: tuple[str, ...] = ("postmaster", "do_not_reply", "mailer")

#: Legal-entity forms, stripped from the end of a company name before
#: matching. Generic forms only, from any country; a word that says what a
#: company does, or one industry's charter letters, stays part of the name.
_LEGAL_SUFFIXES = frozenset(
    {
        "inc",
        "incorporated",
        "llc",
        "ltd",
        "limited",
        "corp",
        "corporation",
        "co",
        "company",
        "plc",
        "lp",
        "llp",
        "pllc",
        "pc",
        "pty",
        "gmbh",
        "ag",
        "sa",
        "sarl",
        "srl",
        "bv",
        "nv",
    }
)
_NON_WORD = re.compile(r"[^a-z0-9]+")


# ── Shapes ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Rules:
    """Everything a rule needs to know about this run and this AE."""

    owner: str
    now: datetime  # aware UTC
    today: date  # business-timezone calendar day
    lookback_days: int = 14
    open_loop_business_days: int = 2
    log_grace_business_days: int = 1
    todo_grace_days: int = 5
    todo_undated_days: int = 5
    task_lookback_days: int = 30
    max_age_days: int = 60
    internal_domains: tuple[str, ...] = ()
    mutes: tuple[str, ...] = ()

    @classmethod
    def from_thresholds(
        cls,
        owner: str,
        *,
        now: datetime,
        today: date,
        limits: dict,
        mutes: Iterable[str] = (),
    ) -> Rules:
        return cls(
            owner=(owner or "").strip().lower(),
            now=now,
            today=today,
            lookback_days=limits["lookback_days"],
            open_loop_business_days=limits["open_loop_business_days"],
            log_grace_business_days=limits["log_grace_business_days"],
            todo_grace_days=limits["todo_grace_days"],
            todo_undated_days=limits["todo_undated_days"],
            task_lookback_days=limits.get("task_lookback_days", 30),
            max_age_days=limits["max_age_days"],
            internal_domains=tuple(limits["internal_domains"]),
            mutes=tuple(m.strip().lower() for m in mutes if m),
        )

    @property
    def oldest_allowed(self) -> datetime:
        return self.now - timedelta(days=self.max_age_days)

    @property
    def oldest_task(self) -> datetime:
        """The oldest due date a NEW task loose end may have (TASK_LOOKBACK_DAYS)."""
        return self.now - timedelta(
            days=min(self.task_lookback_days, self.max_age_days)
        )


@dataclass
class Candidate:
    """Something that should be on the AE's list, as the rules see it today."""

    kind: str
    source: str
    source_key: str
    title: str
    opened_at: datetime  # aware UTC
    counterparty_email: str | None = None
    counterparty_label: str | None = None
    company_label: str | None = None
    hubspot_contact_id: str | None = None
    hubspot_company_id: str | None = None
    hubspot_deal_id: str | None = None
    thread_id: str | None = None
    message_id: str | None = None
    link: str | None = None
    headline: str | None = None
    detail: dict = field(default_factory=dict)
    # Unlogged email: the send time of every unlogged AE message in the thread
    # (the re-open rule reads these). Never persisted.
    event_times: tuple[datetime, ...] = ()
    # Needs the judge's yes before it may be created (open loops, Slack).
    needs_judge: bool = False
    # What the judge reads (a Thread or a Slack message). Never persisted.
    context: Any = None


@dataclass
class Stats:
    """Counts a rule pass reports into coverage (never content)."""

    partial_skipped: int = 0


# ── Small predicates ─────────────────────────────────────────────────────────


def as_aware(value: datetime) -> datetime:
    """Naive values are UTC by repo convention."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def domain_of(address: str) -> str:
    addr = (address or "").strip().lower()
    return addr.rsplit("@", 1)[1] if "@" in addr else ""


def is_internal(address: str, internal_domains: Iterable[str]) -> bool:
    dom = domain_of(address)
    return bool(dom) and any(
        dom == d or dom.endswith("." + d) for d in internal_domains if d
    )


def is_automated(address: str) -> bool:
    """A robot sender: the open-loops sweep's patterns plus a few bounce names."""
    from backend.services.open_loops import _is_automated

    addr = (address or "").strip().lower()
    if not addr or "@" not in addr:
        return True
    local = addr.split("@", 1)[0]
    return _is_automated(addr) or any(p in local for p in _EXTRA_AUTOMATED)


def is_muted(address: str, mutes: Iterable[str]) -> bool:
    """``jane@acme.com`` mutes that address; ``@acme.com`` mutes the domain."""
    addr = (address or "").strip().lower()
    if not addr:
        return False
    dom = "@" + domain_of(addr)
    return any(m == addr or m == dom for m in mutes)


def is_external_party(address: str, rules: Rules) -> bool:
    """A real person outside the company whom this AE has not muted."""
    addr = (address or "").strip().lower()
    return (
        bool(addr)
        and addr != rules.owner
        and not is_internal(addr, rules.internal_domains)
        and not is_automated(addr)
        and not is_muted(addr, rules.mutes)
    )


def business_days_between(a: datetime, b: datetime) -> float:
    """Business days elapsed, on the cockpit's one org-wide clock."""
    from rubik_core.cockpit.business_hours import business_hours_between
    from rubik_core.config import settings

    day_hours = float(getattr(settings, "COCKPIT_BUSINESS_DAY_HOURS", 8) or 8)
    return business_hours_between(as_aware(a), as_aware(b)) / day_hours


def normalize_company(name: str) -> str:
    """``"Acme Holdings, LLC"`` → ``"acme holdings"``; ``""`` when too short."""
    # Dots go first so dotted initials ("S.A.", "L.L.C.") become one token.
    raw = (name or "").lower().replace("&", " and ").replace(".", "")
    words = _NON_WORD.sub(" ", raw).split()
    if words and words[0] == "the":
        words = words[1:]
    while words and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    text = " ".join(words)
    return text if len(text) >= MIN_COMPANY_NAME else ""


def _records_of(match: Match, *, people_only: bool = False) -> list[str]:
    keys = list(match.records)
    if people_only:
        keys = [k for k in keys if k.startswith(("contact:", "company:"))]
    return keys[:MAX_RECORDS]


def _iso(dt: datetime) -> str:
    return (
        as_aware(dt)
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
        .isoformat(timespec="seconds")
    )


def real_messages(thread: Thread) -> list[Message]:
    """The thread's sent or received mail, oldest first — never a draft.

    ``gmail_reader.parse_thread`` already drops drafts, trash and spam; this is
    the second guard for a thread built any other way, because an unsent draft
    read as mail closes a loop as "replied" and flags an unsent email as
    unlogged.
    """
    return [m for m in thread.messages if not SKIPPED_LABELS.intersection(m.labels)]


def _crm_name(match: Match | None) -> dict:
    """``{"crm_contact_name": …}`` when the name came from a HubSpot contact.

    ``counterparty_label`` may fall back to a mail-header display name or a raw
    address; the team view may show only a name HubSpot holds, so the CRM name
    is kept apart (``views._who`` reads it).
    """
    if match is not None and match.kind == "contact" and match.label:
        return {"crm_contact_name": str(match.label)[:200]}
    return {}


# ── unlogged: email ──────────────────────────────────────────────────────────


def unlogged_email_candidates(
    threads: dict[str, Thread],
    index: HubSpotIndex,
    rules: Rules,
    stats: Stats | None = None,
) -> list[Candidate]:
    """One candidate per thread whose AE emails to a client are not logged."""
    if not index.available:
        return []
    stats = stats if stats is not None else Stats()
    window = rules.now - timedelta(days=rules.lookback_days)
    out: list[Candidate] = []
    for thread in threads.values():
        unlogged: list[tuple[Any, str, Match]] = []
        for msg in real_messages(thread):
            if msg.from_email != rules.owner or msg.at is None:
                continue
            if msg.at < window or msg.at < rules.oldest_allowed:
                continue
            if business_days_between(msg.at, rules.now) < rules.log_grace_business_days:
                continue
            matched = [
                (addr, m)
                for addr in dict.fromkeys((*msg.to, *msg.cc))
                if is_external_party(addr, rules)
                for m in [index.resolve(addr)]
                if m is not None
            ]
            if not matched:
                continue
            since = msg.at - _ENGAGEMENT_SLACK
            records = [k for _, m in matched for k in m.records]
            if index.first_engagement(records, since) is not None:
                continue  # logged
            if index.evidence_may_be_missing(since):
                stats.partial_skipped += 1
                continue
            # Prefer a named contact over a domain match for "who".
            matched.sort(key=lambda am: am[1].kind != "contact")
            unlogged.append((msg, matched[0][0], matched[0][1]))
        if not unlogged:
            continue
        first, addr, match = unlogged[0]
        records: list[str] = []
        for _, _, m in unlogged:
            records.extend(m.records)
        out.append(
            Candidate(
                kind="unlogged",
                source="gmail",
                source_key=f"gmail:{thread.id}",
                title=(thread.subject or "(no subject)")[:300],
                opened_at=first.at,
                counterparty_email=addr,
                counterparty_label=(match.label or first.name_for(addr) or addr)[:200],
                company_label=(match.company_label or None),
                hubspot_contact_id=match.contact_id,
                hubspot_company_id=match.company_id,
                hubspot_deal_id=match.deal_id,
                thread_id=thread.id,
                message_id=first.id,
                link=thread.link,
                detail={
                    "match": match.kind,
                    "messages": len(unlogged),
                    "records": list(dict.fromkeys(records))[:MAX_RECORDS],
                    **_crm_name(match),
                },
                event_times=tuple(m.at for m, _, _ in unlogged),
            )
        )
    return out


# ── open loops ───────────────────────────────────────────────────────────────


def open_loop_candidates(
    threads: dict[str, Thread], index: HubSpotIndex, rules: Rules
) -> list[Candidate]:
    """Threads where a client wrote last, directly to the AE, and waits."""
    out: list[Candidate] = []
    for thread in threads.values():
        messages = real_messages(thread)
        if not messages:
            continue
        last = messages[-1]
        if last.at is None or last.from_email == rules.owner or last.bulk:
            continue
        if not is_external_party(last.from_email, rules):
            continue
        if rules.owner not in last.to:
            continue  # only Cc'd, or reached through a list alias
        if last.at < rules.oldest_allowed:
            continue
        if business_days_between(last.at, rules.now) < rules.open_loop_business_days:
            continue
        if any(
            m.from_email == rules.owner and m.at is not None and m.at > last.at
            for m in messages
        ):
            continue
        match = index.resolve(last.from_email) if index.available else None
        if match is not None:
            follow_up = index.first_engagement(
                _records_of(match, people_only=True),
                last.at,
                strictly_after=True,
                include_incoming=False,
            )
            if follow_up is not None:
                continue  # answered another way, and logged
        label = (match.label if match else None) or last.from_name or last.from_email
        out.append(
            Candidate(
                kind="open_loop",
                source="gmail",
                source_key=f"gmail:{thread.id}:{last.id}",
                title=(thread.subject or "(no subject)")[:300],
                opened_at=last.at,
                counterparty_email=last.from_email,
                counterparty_label=label[:200],
                company_label=match.company_label if match else None,
                hubspot_contact_id=match.contact_id if match else None,
                hubspot_company_id=match.company_id if match else None,
                hubspot_deal_id=match.deal_id if match else None,
                thread_id=thread.id,
                message_id=last.id,
                link=thread.link,
                detail={
                    "match": match.kind if match else "none",
                    "records": _records_of(match, people_only=True) if match else [],
                    **_crm_name(match),
                },
                needs_judge=True,
                context=thread,
            )
        )
    return out


# ── unlogged: Slack ──────────────────────────────────────────────────────────


def slack_time(message: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(message.ts), tz=timezone.utc)
    except (TypeError, ValueError, AttributeError, OverflowError, OSError):
        return None


def slack_candidates(
    messages: Iterable[Any],
    companies: Iterable[tuple[str, str, list[str]]],
    index: HubSpotIndex,
    rules: Rules,
    stats: Stats | None = None,
) -> list[Candidate]:
    """The AE's own Slack messages that name a company on one of their open
    deals and have nothing logged since. The judge decides whether each one
    really reports a client conversation."""
    if not index.available:
        return []
    stats = stats if stats is not None else Stats()
    names = [
        (cid, name, deal_ids, norm)
        for cid, name, deal_ids in companies
        for norm in [normalize_company(name)]
        if norm
    ]
    if not names:
        return []
    window = rules.now - timedelta(days=SLACK_DAYS)
    out: list[Candidate] = []
    seen: set[str] = set()
    for msg in messages:
        at = slack_time(msg)
        if at is None or at < window:
            continue
        if business_days_between(at, rules.now) < rules.log_grace_business_days:
            continue
        key = f"slack:{getattr(msg, 'channel_id', '')}:{getattr(msg, 'ts', '')}"
        if key in seen:
            continue
        text = " ".join(_NON_WORD.sub(" ", (msg.text or "").lower()).split())
        hits = [n for n in names if re.search(rf"\b{re.escape(n[3])}\b", text)]
        if not hits:
            continue
        cid, name, deal_ids, _norm = max(hits, key=lambda n: len(n[3]))
        records = [f"company:{cid}", *(f"deal:{d}" for d in deal_ids)]
        since = at - _ENGAGEMENT_SLACK
        if index.first_engagement(records, since) is not None:
            continue
        if index.evidence_may_be_missing(since):
            stats.partial_skipped += 1
            continue
        seen.add(key)
        out.append(
            Candidate(
                kind="unlogged",
                source="slack",
                source_key=key,
                title=" ".join((msg.text or "").split())[:300],
                opened_at=at,
                company_label=name[:200],
                hubspot_company_id=cid,
                hubspot_deal_id=deal_ids[0] if deal_ids else None,
                link=getattr(msg, "permalink", None) or getattr(msg, "app_link", None),
                detail={
                    "match": "company_name",
                    "channel_id": getattr(msg, "channel_id", None),
                    "records": records[:MAX_RECORDS],
                },
                needs_judge=True,
                context=msg,
            )
        )
    return out


# ── todo ─────────────────────────────────────────────────────────────────────


def _local_today(tz_name: str | None, now: datetime, fallback: date) -> date:
    from zoneinfo import ZoneInfo

    try:
        return now.astimezone(ZoneInfo(tz_name)).date() if tz_name else fallback
    except Exception:  # noqa: BLE001 — an unknown zone falls back to the org day
        return fallback


def _local_midnight(day: date, tz_name: str | None) -> datetime:
    from zoneinfo import ZoneInfo

    try:
        tz = ZoneInfo(tz_name) if tz_name else timezone.utc
    except Exception:  # noqa: BLE001
        tz = timezone.utc
    return datetime.combine(day, datetime.min.time(), tz).astimezone(timezone.utc)


def promise_said_at(promise: Any) -> datetime | None:
    ref = getattr(promise, "source_ref", None) or {}
    raw = ref.get("at") if isinstance(ref, dict) else None
    if raw:
        try:
            return as_aware(datetime.fromisoformat(str(raw).replace("Z", "+00:00")))
        except ValueError:
            pass
    created = getattr(promise, "created_at", None)
    return as_aware(created) if created else None


def promise_anchor(promise: Any) -> datetime | None:
    """The instant a promise's age counts from: local midnight of its date, or
    when it was said if it had none (None when neither is known)."""
    due = getattr(promise, "due_local", None)
    if due is not None:
        return _local_midnight(due, getattr(promise, "timezone", None))
    return promise_said_at(promise)


def date_moved_later(flagged: datetime, current: datetime | None) -> bool:
    """Whether a to-do's date now falls on a later business-timezone day than
    the one it was flagged with (snoozed, or re-dated later).

    Only a date moved later makes ``rescheduled``. A to-do that is inside the
    grace only because the grace was RAISED (3 → 5 days, say) was not
    rescheduled: it stays open, the page leaves it out until it is the new
    grace days overdue (``views.listed``), and it never shows "Reopened".
    """
    if current is None:
        return False
    from rubik_core.cockpit.business_hours import _business_tz

    tz = _business_tz()
    return as_aware(current).astimezone(tz).date() > (
        as_aware(flagged).astimezone(tz).date()
    )


def promise_due(
    promise: Any,
    *,
    now: datetime,
    today: date,
    grace_days: int,
    undated_days: int,
) -> tuple[str, datetime | None]:
    """``("due", opened_at)`` when a promise is past its grace days today,
    ``("not_due", None)`` when it positively is not (its date is still inside
    the grace, or it was said too recently), ``("unknown", None)`` when an
    undated promise carries no time it was said.

    The one rule both the finder and the closure re-check read, so a promise
    snoozed or re-dated into the future stops being a loose end the same way
    it would never have become one.
    """
    due = getattr(promise, "due_local", None)
    tz_name = getattr(promise, "timezone", None)
    if due is not None:
        local_today = _local_today(tz_name, now, today)
        if (local_today - due).days < grace_days:
            return "not_due", None
        return "due", _local_midnight(due, tz_name)
    said = promise_said_at(promise)
    if said is None:
        return "unknown", None
    if (now - said).days < undated_days:
        return "not_due", None
    return "due", said


def promise_candidates(promises: Iterable[Any], rules: Rules) -> list[Candidate]:
    """Heard promises past their date (or undated and old enough)."""
    out: list[Candidate] = []
    for p in promises:
        if getattr(p, "source", None) not in HEARD_SOURCES:
            continue
        if getattr(p, "status", None) not in UNKEPT_STATUSES:
            continue
        if float(getattr(p, "confidence", 0) or 0) < MIN_PROMISE_CONFIDENCE:
            continue
        due = getattr(p, "due_local", None)
        state, opened = promise_due(
            p,
            now=rules.now,
            today=rules.today,
            grace_days=rules.todo_grace_days,
            undated_days=rules.todo_undated_days,
        )
        if state != "due" or opened is None:
            continue
        if opened < rules.oldest_allowed:
            continue
        cp = (getattr(p, "counterparty_id", None) or "").strip().lower()
        ref = getattr(p, "source_ref", None) or {}
        out.append(
            Candidate(
                kind="todo",
                source="promise",
                source_key=f"promise:{p.id}",
                title=(p.title or "A promise")[:300],
                opened_at=opened,
                counterparty_email=cp if "@" in cp else None,
                counterparty_label=(getattr(p, "counterparty_label", None) or None),
                link=(ref.get("link") if isinstance(ref, dict) else None) or None,
                detail={"promise_id": str(p.id), "dated": due is not None},
            )
        )
    return out


def task_candidates(
    tasks: Iterable[Any], index: HubSpotIndex, rules: Rules
) -> list[Candidate]:
    """The AE's HubSpot tasks ``TODO_GRACE_DAYS`` or more overdue, that went
    overdue within ``TASK_LOOKBACK_DAYS`` (an older one is backlog, not a slip)."""
    from rubik_core.cockpit.business_hours import _business_tz
    from rubik_core.sales.rhythm_checks import record_url

    if not index.available:
        return []
    tz = _business_tz()
    out: list[Candidate] = []
    for task in tasks:
        if task.due is None or task.due < rules.oldest_task:
            continue
        if (rules.today - task.due.astimezone(tz).date()).days < rules.todo_grace_days:
            continue
        company_id = task.company_ids[0] if task.company_ids else None
        out.append(
            Candidate(
                kind="todo",
                source="hubspot_task",
                source_key=f"hstask:{task.id}",
                title=(task.subject or "HubSpot task")[:300],
                opened_at=task.due,
                company_label=index.company_name(company_id),
                hubspot_contact_id=task.contact_ids[0] if task.contact_ids else None,
                hubspot_company_id=company_id,
                hubspot_deal_id=task.deal_ids[0] if task.deal_ids else None,
                link=record_url("task", task.id),
                detail={"task_id": task.id},
            )
        )
    return out


# ── Closure (every open item, every run) ─────────────────────────────────────


@dataclass
class ClosureContext:
    """What one AE's closure re-check can see this run."""

    now: datetime  # aware UTC
    max_age_days: int
    index: HubSpotIndex
    owner: str
    gmail_ok: bool
    threads: dict[str, Thread] = field(default_factory=dict)
    promises: dict[str, Any] = field(default_factory=dict)  # id → Promise
    # The business-timezone day (None → the UTC day of ``now``) and the todo
    # grace rules, for "rescheduled": the same thresholds the finder uses.
    today: date | None = None
    todo_grace_days: int = 5
    todo_undated_days: int = 5
    # A colleague's later message on the thread answers the client too.
    internal_domains: tuple[str, ...] = ()
    # The AE's HubSpot owner id (None = unknown: "reassigned" is undecided).
    owner_id: str | None = None

    @property
    def day(self) -> date:
        return self.today or as_aware(self.now).date()


def item_records(item: Any) -> list[str]:
    """The engagement-index keys an item is checked against."""
    detail = item.detail if isinstance(getattr(item, "detail", None), dict) else {}
    keys = [str(k) for k in (detail.get("records") or []) if k]
    for prefix, value in (
        ("contact", item.hubspot_contact_id),
        ("company", item.hubspot_company_id),
        ("deal", item.hubspot_deal_id),
    ):
        if value:
            keys.append(f"{prefix}:{value}")
    return list(dict.fromkeys(keys))


def closure_for(item: Any, ctx: ClosureContext) -> tuple[str, dict] | None:
    """``(close_reason, evidence)`` when the item is closed, else None.

    A real closure wins over ``aged_out``: an item that was fixed on day 61 is
    reported as fixed. A source that could not be read decides nothing.
    """
    found = _detected_closure(item, ctx)
    if found is not None:
        return found
    opened = as_aware(item.opened_at)
    if opened < ctx.now - timedelta(days=ctx.max_age_days):
        return "aged_out", {"type": "age", "at": _iso(ctx.now)}
    return None


def _detected_closure(item: Any, ctx: ClosureContext) -> tuple[str, dict] | None:
    opened = as_aware(item.opened_at)
    index = ctx.index
    if item.kind == "unlogged":
        if not index.available:
            return None
        eng = index.first_engagement(item_records(item), opened - _ENGAGEMENT_SLACK)
        return ("logged_in_hubspot", eng.evidence()) if eng else None

    if item.kind == "open_loop":
        if ctx.gmail_ok and item.thread_id:
            thread = ctx.threads.get(item.thread_id)
            if thread is not None:
                messages = real_messages(thread)
                inbound_at = opened
                for m in messages:
                    if m.id == item.message_id and m.at is not None:
                        inbound_at = m.at
                later = [m for m in messages if m.at is not None and m.at > inbound_at]
                for m in later:
                    if m.from_email == ctx.owner:
                        return "replied", {
                            "type": "gmail_message",
                            "id": m.id,
                            "at": _iso(m.at),
                        }
                # A colleague answered the client on the thread (a manager on
                # Cc replying to all). Detection would not flag a thread whose
                # newest message is internal, so closure must not keep it open
                # either. The client must be a recipient: an internal-only note
                # on the thread ("can you take this?") answers nobody.
                client = (getattr(item, "counterparty_email", None) or "").lower()
                for m in later:
                    if (
                        m.from_email
                        and is_internal(m.from_email, ctx.internal_domains)
                        and not is_automated(m.from_email)
                        and not m.bulk
                        and (not client or client in (*m.to, *m.cc))
                    ):
                        return "replied", {
                            "type": "gmail_message_internal",
                            "id": m.id,
                            "at": _iso(m.at),
                        }
        if index.available:
            people = [
                k for k in item_records(item) if k.startswith(("contact:", "company:"))
            ]
            eng = index.first_engagement(
                people, opened, strictly_after=True, include_incoming=False
            )
            if eng is not None:
                return "logged_follow_up", eng.evidence()
        return None

    if item.kind == "todo" and item.source == "promise":
        pid = item.source_key.split(":", 1)[1] if ":" in item.source_key else ""
        promise = ctx.promises.get(pid)
        if promise is None:
            # Gone from the ledger entirely: nobody owes it any more.
            return "promise_withdrawn", {
                "type": "promise",
                "id": pid,
                "at": _iso(ctx.now),
            }
        status = getattr(promise, "status", None)
        if status in KEPT_STATUSES:
            at = getattr(promise, "kept_at", None) or getattr(
                promise, "updated_at", None
            )
            return "promise_kept", {
                "type": "promise",
                "id": pid,
                "at": _iso(at or ctx.now),
            }
        if status in WITHDRAWN_STATUSES:
            at = getattr(promise, "dropped_at", None) or getattr(
                promise, "updated_at", None
            )
            return "promise_withdrawn", {
                "type": "promise",
                "id": pid,
                "at": _iso(at or ctx.now),
            }
        if status in UNKEPT_STATUSES:
            # Snoozed or re-dated (promises/store.snooze, move_due): still owed,
            # but no longer past its grace days. It leaves the list as
            # "rescheduled" — never counted as fixed — and the same row re-opens
            # if a later scan finds it past its new date (store.apply_candidate).
            state, _ = promise_due(
                promise,
                now=ctx.now,
                today=ctx.day,
                grace_days=ctx.todo_grace_days,
                undated_days=ctx.todo_undated_days,
            )
            if state == "not_due" and date_moved_later(opened, promise_anchor(promise)):
                due = getattr(promise, "due_local", None)
                evidence: dict[str, Any] = {
                    "type": "promise",
                    "id": pid,
                    "at": _iso(ctx.now),
                }
                if due is not None:
                    evidence["due"] = due.isoformat()
                return "rescheduled", evidence
        return None

    if item.kind == "todo" and item.source == "hubspot_task":
        if not index.available:
            return None
        tid = item.source_key.split(":", 1)[1] if ":" in item.source_key else ""
        state = index.task_states.get(tid)
        if state == "completed":
            return "task_completed", {"type": "tasks", "id": tid, "at": _iso(ctx.now)}
        if state == "missing":
            return "task_gone", {"type": "tasks", "id": tid, "at": _iso(ctx.now)}
        if state == "open":
            # Handed to someone else in HubSpot: off this AE's list (the new
            # owner's own scan flags it for them if it is still overdue).
            new_owner = index.task_owner.get(tid)
            if ctx.owner_id and new_owner and new_owner != ctx.owner_id:
                return "task_gone", {
                    "type": "tasks",
                    "id": tid,
                    "at": _iso(ctx.now),
                    "reassigned": True,
                }
            # Moved to a later date: no longer the grace days overdue.
            due_at = index.task_due.get(tid)
            if due_at is not None and date_moved_later(opened, due_at):
                from rubik_core.cockpit.business_hours import _business_tz

                due_day = due_at.astimezone(_business_tz()).date()
                if (ctx.day - due_day).days < ctx.todo_grace_days:
                    return "rescheduled", {
                        "type": "tasks",
                        "id": tid,
                        "at": _iso(ctx.now),
                        "due": due_day.isoformat(),
                    }
    return None


def reopen_after(item: Any) -> datetime | None:
    """The instant after which a NEW unlogged AE email re-opens a closed
    unlogged-email item (README §3).

    For ``logged_in_hubspot`` that is the engagement that closed it, not the
    scan that noticed: an email sent after that activity but before the next scan
    (a weekend, a missed morning) is new and unlogged, and anchoring on
    ``closed_at`` would lose it. Everything else anchors on ``closed_at``.
    """
    evidence = item.close_evidence if isinstance(item.close_evidence, dict) else {}
    if item.close_reason == "logged_in_hubspot" and evidence.get("at"):
        try:
            return as_aware(datetime.fromisoformat(str(evidence["at"])))
        except ValueError:
            pass
    return as_aware(item.closed_at) if item.closed_at else None

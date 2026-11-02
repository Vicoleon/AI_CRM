"""One AE's Gmail, read once per morning — METADATA ONLY.

Two lanes over ``LOOP_CLOSURE_LOOKBACK_DAYS`` (README §4.2): the inbox, minus
chats and the promotions / social / forums tabs, and the Sent folder. Up to
300 threads each (more is coverage ``partial``), de-duplicated by thread id,
drafts / trash / spam dropped, every thread hydrated with
``format="metadata"`` — headers and Gmail's ~200-character snippet, never a
body. That is enough to know who wrote to whom and when, which is everything
the rules in ``detect.py`` need; the snippet only ever reaches the judge's
prompt and the AE's own screen.

The parsed shape is two small frozen dataclasses rather than raw API dicts, so
``detect.py`` stays pure and its tests build threads by hand.

**A client that cannot be built is not an empty mailbox.** ``GmailClient``
returns a client with ``service = None`` when delegation fails, and
``search_threads`` then raises — the caller turns that into coverage
``gmail: unavailable``, no Gmail findings and NO Gmail-based closures for that
AE this run. Reading "could not look" as "nothing there" is the one mistake
this feature must not make.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import getaddresses, parseaddr
from typing import Any

#: Threads per lane. ``threads.list`` returns at most 500 per page and the list
#: is newest first, so a busy inbox pushes the threads an open loop needs (a
#: client message two business days old) below the newest few dozen. 300 per
#: lane covers about two weeks for a heavy inbox; a lane that still has more
#: past the cap is reported as coverage ``gmail: partial``, never as ``ok``.
#: Hydration is batched (``_hydrate_threads``, 50 per round trip).
MAX_THREADS_PER_LANE = 300
#: ``threads.list`` page size ceiling (Gmail API).
_LIST_PAGE_MAX = 500

#: Messages that were never sent or that the owner threw away. A DRAFT is
#: ``From:`` the AE with an ``internalDate`` of when it was last saved, so
#: counting it as mail would read an unsent reply as "replied" and an unsent
#: email to a client as an unlogged sent one.
SKIPPED_LABELS: frozenset[str] = frozenset({"DRAFT", "TRASH", "SPAM"})

_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class Message:
    """One message's metadata. ``at`` is aware UTC (``internalDate``)."""

    id: str
    thread_id: str
    from_email: str
    from_name: str
    to: tuple[str, ...]
    cc: tuple[str, ...]
    subject: str
    at: datetime | None
    snippet: str
    labels: tuple[str, ...] = ()
    # Bulk / list / auto-reply mail, read from the headers metadata carries
    # (List-Id, List-Unsubscribe, Precedence, Auto-Submitted). Such a message
    # never opens a loop: nobody is waiting on a reply to a newsletter.
    bulk: bool = False
    # Display names by address, from From/To/Cc — for counterparty labels.
    names: tuple[tuple[str, str], ...] = ()

    def name_for(self, address: str) -> str:
        for addr, name in self.names:
            if addr == address:
                return name
        return ""


@dataclass
class Thread:
    """A thread's messages, oldest first."""

    id: str
    messages: list[Message] = field(default_factory=list)

    @property
    def subject(self) -> str:
        for m in self.messages:
            if m.subject:
                return m.subject
        return ""

    @property
    def link(self) -> str:
        return thread_link(self.id)


def thread_link(thread_id: str) -> str:
    """The owner's own Gmail link for a thread (``u/0`` is their mailbox)."""
    return f"https://mail.google.com/mail/u/0/#all/{thread_id}"


def _bulk(headers: dict[str, str]) -> bool:
    if headers.get("list-id") or headers.get("list-unsubscribe"):
        return True
    if headers.get("precedence", "").strip().lower() in {"bulk", "list", "junk"}:
        return True
    auto = headers.get("auto-submitted", "").strip().lower()
    return bool(auto) and auto != "no"


def parse_message(raw: dict[str, Any], thread_id: str) -> Message | None:
    """A ``format=metadata`` message → :class:`Message`, or None if unusable."""
    # Shared with the open-loops sweep so both read headers and dates alike.
    from backend.services.open_loops import _headers, _msg_datetime

    headers = _headers(raw)
    mid = str(raw.get("id") or "")
    if not mid:
        return None
    from_name, from_addr = parseaddr(headers.get("from", ""))
    to_pairs = getaddresses([headers.get("to", "")])
    cc_pairs = getaddresses([headers.get("cc", "")])
    names: dict[str, str] = {}
    for name, addr in [(from_name, from_addr), *to_pairs, *cc_pairs]:
        a = (addr or "").strip().lower()
        if a and name and a not in names:
            names[a] = name.strip().strip('"')
    return Message(
        id=mid,
        thread_id=thread_id,
        from_email=(from_addr or "").strip().lower(),
        from_name=(from_name or "").strip().strip('"'),
        to=tuple(a.strip().lower() for _, a in to_pairs if a and "@" in a),
        cc=tuple(a.strip().lower() for _, a in cc_pairs if a and "@" in a),
        subject=_WS.sub(" ", headers.get("subject", "")).strip(),
        at=_msg_datetime(raw, headers),
        snippet=_WS.sub(" ", raw.get("snippet") or "").strip(),
        labels=tuple(raw.get("labelIds") or ()),
        bulk=_bulk(headers),
        names=tuple(names.items()),
    )


def parse_thread(raw: dict[str, Any]) -> Thread | None:
    """A ``format=metadata`` thread → :class:`Thread`, oldest first.

    Drafts, trash and spam are dropped here, so every reader (the morning
    gather and the closure re-read alike) sees only mail that was really sent
    or received. A thread with nothing else left is None.
    """
    tid = str(raw.get("id") or "")
    if not tid:
        return None
    msgs = [
        m
        for m in (parse_message(r, tid) for r in raw.get("messages") or [])
        if m and not SKIPPED_LABELS.intersection(m.labels)
    ]
    if not msgs:
        return None
    floor = datetime.min.replace(tzinfo=timezone.utc)
    msgs.sort(key=lambda m: m.at or floor)
    return Thread(id=tid, messages=msgs)


def lanes(lookback_days: int) -> tuple[str, str]:
    """The two Gmail searches, verbatim from README §4.2."""
    days = max(1, int(lookback_days))
    return (
        f"in:inbox newer_than:{days}d -in:chats -category:promotions "
        "-category:social -category:forums",
        f"in:sent newer_than:{days}d -in:chats",
    )


def _lane_raws(client: Any, query: str, cap: int) -> tuple[list[dict], bool]:
    """``(raw threads, capped)`` for one lane, newest first, at most ``cap``.

    Pages through ``threads.list`` when the client offers ``list_thread_page``
    (``GmailClient`` does), so the cap is a real thread count rather than one
    page; ``capped`` is True when Gmail still had more past it. A client without
    paging (a test double) answers through ``search_threads``, and reaching the
    cap is then read as "there may be more".
    """
    page = getattr(client, "list_thread_page", None)
    if not callable(page):
        raws = list(client.search_threads(query=query, max_results=cap) or [])
        return raws, len(raws) >= cap
    ids: list[str] = []
    token: str | None = None
    while len(ids) < cap:
        batch, token = page(
            query=query,
            max_results=min(_LIST_PAGE_MAX, cap - len(ids)),
            page_token=token,
        )
        before = len(ids)
        for tid in batch or []:
            if tid and tid not in ids:
                ids.append(tid)
        # No token, or a page that added nothing new (never loop on a repeat).
        if not token or len(ids) == before:
            break
    capped = bool(token) and len(ids) >= cap
    return list(client._hydrate_threads(ids[:cap]) or []), capped


def gather_all(
    client: Any, *, lookback_days: int, max_per_lane: int = MAX_THREADS_PER_LANE
) -> tuple[dict[str, Thread], list[str]]:
    """``(threads, capped lanes)`` — both lanes, de-duplicated by thread id.

    Raises when Gmail cannot be read — the caller decides what "unavailable"
    means, never this function. A non-empty second value means a lane had more
    threads in the window than ``max_per_lane``: the read is PARTIAL, and the
    caller must say so rather than report an all-clear.
    """
    seen: dict[str, Thread] = {}
    capped: list[str] = []
    for query in lanes(lookback_days):
        raws, cut = _lane_raws(client, query, max_per_lane)
        if cut:
            capped.append(query)
        for raw in raws:
            tid = str((raw or {}).get("id") or "")
            if not tid or tid in seen:
                continue
            thread = parse_thread(raw)
            if thread is not None:
                seen[tid] = thread
    return seen, capped


def gather(
    client: Any, *, lookback_days: int, max_per_lane: int = MAX_THREADS_PER_LANE
) -> dict[str, Thread]:
    """Both lanes, de-duplicated by thread id (see :func:`gather_all`)."""
    return gather_all(client, lookback_days=lookback_days, max_per_lane=max_per_lane)[0]


def reread(client: Any, thread_ids: Iterable[str]) -> dict[str, Thread]:
    """Re-read specific threads (metadata) — the open-loop closure check for
    items whose thread has left the lookback window.

    Uses the client's batched hydration, the same call ``search_threads`` makes
    for every hit. If the batch fails, each thread is read on its own and one
    that cannot be read (deleted, or moved out of reach) is simply absent: for
    that item "replied" is then undecided this run, which is the honest answer,
    and the rest of the mailbox still counts as read.
    """
    ids = [t for t in dict.fromkeys(thread_ids) if t]
    if not ids:
        return {}
    try:
        raws = client._hydrate_threads(ids) or []
    except Exception:  # noqa: BLE001 — fall back to one thread at a time
        raws = []
        for tid in ids:
            try:
                raws.extend(client._hydrate_threads([tid]) or [])
            except Exception:  # noqa: BLE001 — one unreadable thread is absent
                continue
    out: dict[str, Thread] = {}
    for raw in raws:
        thread = parse_thread(raw or {})
        if thread is not None:
            out[thread.id] = thread
    return out

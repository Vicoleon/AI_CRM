"""Gmail channel agent — UC-22 thread-aware extractor.

Reads the user's recent mail across three lanes — the inbox, *all*
recent mail (so threads that skip the inbox via filters/auto-categories
are still seen), and sent mail on a wider window — groups messages into
Gmail threads, and emits one ``ChannelSignal`` per thread with reply-lag
attributes that downstream agents need to detect "I sent something and
they never replied" situations.

This agent is a **pure extractor** (UC-22) — it does no judgment about
which threads are "important". The decision-maker (UC-24) does that
across all channels using the entity-tagged signals.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from rubik_core.priorities.agents.base import BaseChannelAgent
from rubik_core.priorities.models import (
    ChannelDigest,
    ChannelKind,
    ChannelSignal,
    EntityKind,
)
from rubik_core.tools.addressing import classify_addressing, strongest_addressing

logger = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────────────


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _extract_email(header_value: str) -> str | None:
    """Pull the bare email out of a "Name <addr@x.com>" header value."""
    if not header_value:
        return None
    match = _EMAIL_RE.search(header_value)
    return match.group(0).lower() if match else None


def _header(msg: dict, name: str) -> str:
    """Read a header value from a Gmail message dict, case-insensitive."""
    headers = msg.get("payload", {}).get("headers", []) or []
    target = name.lower()
    for h in headers:
        if (h.get("name") or "").lower() == target:
            return h.get("value") or ""
    return ""


def _lower_headers(msg: dict) -> dict[str, str]:
    """Every header on a Gmail message, keyed by lower-cased name.

    Shape expected by :func:`rubik_core.tools.addressing.classify_addressing`.
    """
    out: dict[str, str] = {}
    for h in msg.get("payload", {}).get("headers", []) or []:
        name = (h.get("name") or "").lower()
        if name:
            out[name] = h.get("value") or ""
    return out


def _ms_to_dt(internal_date_ms: str | int | None) -> datetime | None:
    if internal_date_ms is None:
        return None
    try:
        return datetime.fromtimestamp(int(internal_date_ms) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


# ── Agent ────────────────────────────────────────────────────────────────────


_READER_MESSAGE_KINDS = ("ask", "answer", "fyi")


class GmailChannelAgent(BaseChannelAgent):
    channel = ChannelKind.GMAIL
    # The LLM enrichment dict for this agent is keyed by Gmail thread id;
    # use the more meaningful name in the prompt + response.
    signal_attrs_key = "thread_attrs"
    # build_prompt's OUTPUT section, and nothing else. `direction`,
    # `addressed_to_reader` and every other thread fact are computed below.
    model_attr_keys = frozenset(
        {
            "asks_of_reader",
            "request_owner",
            "reader_last_message_kind",
            "mentioned_dates",
            "mentioned_amounts",
        }
    )

    def _model_may_write(self, sig, key: str, value: Any) -> bool:
        """``reader_last_message_kind`` is the one computed field the model re-reads.

        parse_signals stamps a structural default when the reader spoke last:
        "answer" when their message follows somebody else's, "ask" when they
        opened the thread and nobody has written back. The documented
        refinement is on a REPLY only: the model may re-read an "answer" as
        "ask" (the reply itself poses a question) or "fyi" (it asks nothing).

        A structural "ask" is not re-readable, and that is the point.
        `_reader_closed_the_loop` hard-drops follow-up and deadline cards on
        "answer"/"fyi", and the decision-maker's WHO SPOKE LAST rule leaves
        them out too, so letting the model turn an unanswered ask into either
        one is exactly how an email's text could hide "they never came back
        to you". Fenced this way the model can only ever UN-hide a
        thread through this field: an "answer" is already closed, "fyi" keeps
        it closed, and "ask" reopens it.
        """
        if key == "reader_last_message_kind":
            return (
                sig.attrs.get("direction") == "i_sent_last"
                and sig.attrs.get(key) == "answer"
                and value in _READER_MESSAGE_KINDS
            )
        return super()._model_may_write(sig, key, value)

    # Lookback windows — kept generous because no-reply situations are
    # often a few days old by the time they matter.
    #
    # SENT was 14 days, which silently put a ceiling on the whole "they never
    # came back to you" feature: the typical case is "it has been three weeks
    # since you engaged the client", and a 14-day window cannot see it.
    # Widening costs nothing — MAX_THREADS_PER_QUERY already caps the fetch at
    # 25 threads, so a longer window changes WHICH 25 come back, not how many.
    # 30 days also matches how non-sales teams count a silent request: in
    # 30-day units, not in days.
    INBOX_LOOKBACK_DAYS = 7
    SENT_LOOKBACK_DAYS = 30
    MAX_THREADS_PER_QUERY = 25
    MAX_SIGNALS_OUT = 20

    # Of MAX_SIGNALS_OUT, how many slots are held for the STALEST threads the
    # reader spoke last on. See the selection comment in parse_signals.
    STALE_RESERVED_SLOTS = 6

    def run(self) -> ChannelDigest:
        # Preserve work-mail signals when an optional OAuth mailbox expires,
        # while making the missing input visible in the run's channel audit.
        self._additional_mailbox_errors: list[str] = []
        self._provider_review_pending: list[str] = []
        self._provider_unavailable = False
        digest = super().run()
        if self._additional_mailbox_errors:
            digest.private_gmail_context = True
        if self._additional_mailbox_errors and not digest.error:
            digest.error = (
                "partial_fetch_failed: reconnect additional Gmail mailbox in Settings"
            )
        if self._provider_unavailable and not digest.error:
            digest.error = "personal_mail_paused: workspace AI provider unavailable"
        elif self._provider_review_pending and not digest.error:
            digest.error = "personal_mail_paused: review the new workspace AI provider in Settings → Email"
        return digest

    def fetch_signals(self, *, include_connected_accounts: bool = True) -> list[dict]:
        from rubik_core.priorities.agents._google_auth import (
            ChannelAuthError,
            is_auth_error,
            require_live_client,
        )
        from rubik_core.tenancy.context import NoWorkspaceContext, current_workspace
        from rubik_core.tenancy.gmail_accounts import (
            PersonalMailConsentGuard,
            list_accounts,
            multi_gmail_enabled,
        )
        from rubik_core.tools.gmail import GmailClient

        self._additional_mailbox_errors = []
        self._provider_review_pending = []
        self._provider_unavailable = False
        self.personal_mail_guard = None
        self.client.route_guard = None
        mailboxes: list[tuple[str | None, str]] = [(None, self.user_email)]
        approved_accounts: list[str] = []
        approved_fingerprint: str | None = None
        try:
            if include_connected_accounts and multi_gmail_enabled():
                for row in list_accounts(self.user_email):
                    if row["status"] != "connected":
                        continue
                    if row.get("ai_processing_unavailable"):
                        self._provider_unavailable = True
                        continue
                    if row.get("ai_processing_review_required"):
                        self._provider_review_pending.append(row["id"])
                        continue
                    fingerprint = row.get("ai_provider_fingerprint")
                    if not isinstance(fingerprint, str) or len(fingerprint) != 64:
                        self._provider_review_pending.append(row["id"])
                        continue
                    if (
                        approved_fingerprint is not None
                        and fingerprint != approved_fingerprint
                    ):
                        self._provider_review_pending.append(row["id"])
                        continue
                    approved_fingerprint = fingerprint
                    approved_accounts.append(row["id"])
                    mailboxes.append((row["id"], row["email"]))
                if approved_accounts and approved_fingerprint is not None:
                    self.personal_mail_guard = PersonalMailConsentGuard(
                        current_workspace(),
                        self.user_email,
                        tuple(approved_accounts),
                        approved_fingerprint,
                    )
                    self.client.route_guard = self.personal_mail_guard.assert_route
        except NoWorkspaceContext:
            # Unit callers without a selected workspace retain the historical
            # work-mail-only behavior; live requests/Jobs always bind context.
            pass

        # First-seen thread envelope per id (carries top-level fields), plus a
        # per-thread union of its messages keyed by message id. A thread that
        # surfaces in more than one lane must keep EVERY message — and thus the
        # true newest one — not just whichever lane's copy happened to carry
        # more messages. (See ``_message_key`` for why we union by id.)
        threads_by_id: dict[str, dict] = {}
        msgs_by_thread: dict[str, dict[str, dict]] = {}

        for account_id, mailbox_email in mailboxes:
            try:
                client = (
                    GmailClient(user_email=self.user_email)
                    if account_id is None
                    else GmailClient(user_email=self.user_email, account_id=account_id)
                )
                require_live_client(client, channel="gmail", user_email=mailbox_email)
            except Exception as exc:
                if account_id is None:
                    raise
                # A revoked extra mailbox is marked for reconnection by its
                # credential resolver. Keep the work mailbox usable and never
                # substitute DWD for this account.
                logger.warning(
                    "Additional Gmail mailbox unavailable for %s: %s",
                    self.user_email,
                    type(exc).__name__,
                )
                self._additional_mailbox_errors.append(account_id)
                continue
            queries_succeeded = 0
            for query in self._search_queries():
                try:
                    threads = (
                        client.search_threads(
                            query=query, max_results=self.MAX_THREADS_PER_QUERY
                        )
                        or []
                    )
                except Exception as exc:  # noqa: BLE001
                    if is_auth_error(exc):
                        if account_id is None:
                            raise ChannelAuthError(
                                f"gmail fetch auth failure for {self.user_email}"
                            ) from None
                        logger.warning(
                            "Additional Gmail mailbox query denied for %s: %s",
                            self.user_email,
                            type(exc).__name__,
                        )
                        self._additional_mailbox_errors.append(account_id)
                        break
                    logger.warning(
                        "Gmail query failed for %s: %s",
                        self.user_email,
                        type(exc).__name__,
                    )
                    if account_id and account_id not in self._additional_mailbox_errors:
                        self._additional_mailbox_errors.append(account_id)
                    continue
                queries_succeeded += 1
                for t in threads:
                    tid = t.get("id")
                    if not tid:
                        continue
                    key = f"{account_id}:{tid}" if account_id else tid
                    threads_by_id.setdefault(
                        key,
                        {
                            **t,
                            "_gmail_account_id": account_id,
                            "_gmail_mailbox": mailbox_email,
                        },
                    )
                    bucket = msgs_by_thread.setdefault(key, {})
                    for m in t.get("messages") or []:
                        # Union, don't overwrite-with-fewer: later lanes only
                        # ADD messages from the same mailbox and thread.
                        bucket.setdefault(self._message_key(m), m)
            if (
                account_id
                and queries_succeeded == 0
                and account_id not in self._additional_mailbox_errors
            ):
                self._additional_mailbox_errors.append(account_id)

        merged: list[dict] = []
        for key, envelope in threads_by_id.items():
            thread = dict(envelope)
            thread["messages"] = list(msgs_by_thread.get(key, {}).values())
            merged.append(thread)
        return merged

    def _search_queries(self) -> list[str]:
        """The Gmail search lanes fed into the union merge.

        Three lanes on purpose:

        * ``in:inbox`` — a guaranteed floor so current inbox threads are always
          covered even on a high-volume mailbox where the broad lane below is
          crowded out by the ``MAX_THREADS_PER_QUERY`` recency cap.
        * ``in:anywhere`` (minus chats/spam/trash/drafts) — the fix for the
          inbox-scoping gap: a thread that skips the inbox (a filter, an
          auto-category, or a manual archive) is invisible to ``in:inbox``, so
          a thread you're awaiting a reply on — the kind you only find via
          search — was missed or went stale. Chats/spam/trash/drafts are never
          a real awaiting-reply signal, so they're excluded.
        * ``in:sent`` on a wider window — catches "I sent and they never
          replied" even when the last activity predates the inbound windows.
        """
        return [
            f"in:inbox newer_than:{self.INBOX_LOOKBACK_DAYS}d",
            (
                f"in:anywhere newer_than:{self.INBOX_LOOKBACK_DAYS}d "
                "-in:chats -in:spam -in:trash -in:drafts"
            ),
            f"in:sent newer_than:{self.SENT_LOOKBACK_DAYS}d",
        ]

    @staticmethod
    def _message_key(msg: dict) -> str:
        """Stable per-message identity for the cross-lane union dedup.

        Real Gmail messages always carry a globally-unique ``id`` (identical no
        matter which query surfaced the thread), so that's the dedup key. Fall
        back to a content composite only for hydration shapes / tests that omit
        the id, so two copies of the same message still collapse to one.
        """
        mid = msg.get("id")
        if mid:
            return str(mid)
        return "|".join(
            (
                str(msg.get("internalDate", "")),
                _header(msg, "From"),
                (msg.get("snippet") or "")[:60],
            )
        )

    def parse_signals(self, raw_items: list) -> list[ChannelSignal]:
        signals: list[ChannelSignal] = []

        for thread in raw_items:
            tid = thread.get("id")
            account_id = thread.get("_gmail_account_id")
            mailbox_email = (thread.get("_gmail_mailbox") or self.user_email).lower()
            user_domain = mailbox_email.split("@", 1)[-1]
            signal_id = f"{account_id}:{tid}" if account_id else tid
            msgs = thread.get("messages", []) or []
            if not tid or not msgs:
                continue

            # Sort by internalDate so reply-direction math is reliable.
            msgs_sorted = sorted(msgs, key=lambda m: int(m.get("internalDate", 0)))
            last_msg = msgs_sorted[-1]
            last_dt = _ms_to_dt(last_msg.get("internalDate"))
            now = datetime.now(timezone.utc)
            idle_days = (
                int((now - last_dt).total_seconds() // 86_400) if last_dt else None
            )

            # Collect every email address that appeared in the thread, and —
            # separately — how the READER was addressed on each message. To vs
            # Cc is the difference between "you must do this" and "you are being
            # kept informed while somebody else does it": an "ACTION NEEDED"
            # email addressed to a colleague became a Cc reader's own
            # high-urgency deadline because this extractor lumped To and Cc
            # together.
            sender_emails: list[str] = []
            recipient_emails: list[str] = []
            to_emails: list[str] = []
            addressing_seen: list[str] = []
            for m in msgs_sorted:
                s = _extract_email(_header(m, "From"))
                if s and s not in sender_emails:
                    sender_emails.append(s)
                for header_name in ("To", "Cc"):
                    raw = _header(m, header_name)
                    for chunk in raw.split(","):
                        e = _extract_email(chunk)
                        if not e:
                            continue
                        if e not in recipient_emails:
                            recipient_emails.append(e)
                        if header_name == "To" and e not in to_emails:
                            to_emails.append(e)
                addressing_seen.append(
                    classify_addressing(_lower_headers(m), mailbox_email)
                )

            # Newest message = how the thread stands right now; strongest across
            # the thread = whether it was ever the reader's to answer at all.
            addressing_last = addressing_seen[-1] if addressing_seen else "list"
            addressing_best = strongest_addressing(addressing_seen)

            # Direction: who sent the LAST message in the thread.
            last_from = _extract_email(_header(last_msg, "From")) or ""
            me = mailbox_email
            if last_from == me:
                direction = "i_sent_last"
            elif last_from:
                direction = "they_sent_last"
            else:
                direction = "mixed"

            # Did the reader ever speak here, and what did their last message
            # DO? `direction=i_sent_last` alone is ambiguous: it is true both
            # for "I asked and they went quiet" (a real follow-up) and for "they
            # asked, I answered" (nothing owed). The decision-maker turned the
            # second into "you went quiet — confirm with stakeholders" and
            # re-opened an answered "ACTION NEEDED by 8/6" as a deadline; the
            # reader rated both "I already responded to this". The default here
            # is structural — a message that follows somebody else's is a
            # reply, so an answer; a thread the reader opened that nobody has
            # responded to is an ask — and the extractor LLM may promote a
            # reply that itself poses a question to `ask` (see build_prompt).
            froms = [_extract_email(_header(m, "From")) or "" for m in msgs_sorted]
            reader_indexes = [i for i, f in enumerate(froms) if f == me]
            reader_replied = bool(reader_indexes)
            reader_last_message_kind: str | None = None
            messages_since_reader_reply: int | None = None
            if reader_indexes:
                last_reader_idx = reader_indexes[-1]
                messages_since_reader_reply = len(msgs_sorted) - 1 - last_reader_idx
                if direction == "i_sent_last":
                    someone_else_before = any(
                        f and f != me for f in froms[:last_reader_idx]
                    )
                    reader_last_message_kind = (
                        "answer" if someone_else_before else "ask"
                    )

            external_domains = sorted(
                {
                    addr.split("@")[-1]
                    for addr in sender_emails + recipient_emails
                    if "@" in addr and addr.split("@")[-1] != user_domain
                }
            )

            has_attachments = any(
                any(
                    part.get("filename")
                    for part in (m.get("payload", {}).get("parts") or [])
                )
                for m in msgs_sorted
            )

            subject = _header(last_msg, "Subject") or "(no subject)"
            snippet = (last_msg.get("snippet") or "")[:300]
            label_ids = last_msg.get("labelIds", []) or []

            # The thread is anchored at the FIRST external participant when
            # picking an entity to join on — that's the person the decision
            # maker will most likely correlate with HubSpot deals or
            # upcoming meetings. If everyone is internal, fall back to the
            # last sender (whoever the user is talking to most recently).
            external_emails = [
                a
                for a in sender_emails + recipient_emails
                if "@" in a and a.split("@")[-1] != user_domain and a != mailbox_email
            ]
            entity_email = (
                external_emails[0]
                if external_emails
                else (last_from if last_from != mailbox_email else None)
            )

            attrs: dict[str, Any] = {
                "gmail_mailbox": mailbox_email,
                "gmail_account_id": account_id,
                "gmail_thread_id": tid,
                "personal_gmail": bool(account_id),
                "direction": direction,
                # True when at least one message in the thread is the reader's.
                "reader_replied": reader_replied,
                # Only set when direction=i_sent_last: "ask" (the reader is
                # waiting on someone), "answer" (they closed someone else's
                # loop) or "fyi" (LLM-assigned: shared info, asked nothing).
                "reader_last_message_kind": reader_last_message_kind,
                # Messages from others AFTER the reader's last one (0 when the
                # reader spoke last; None when they never spoke).
                "messages_since_reader_reply": messages_since_reader_reply,
                # "direct" = reader is on To:, "cc" = only Cc'd, "list" = never
                # individually addressed (distribution list / bulk / bcc).
                "addressed_to_reader": addressing_best,
                "addressed_to_reader_last_message": addressing_last,
                # Who the newest message is actually asking — lets the
                # decision-maker say "X owes this", not "you owe this".
                "to_recipients": to_emails[:8],
                "thread_idle_days": idle_days,
                "message_count": len(msgs_sorted),
                "external_domains": external_domains,
                "external_count": len(external_emails),
                "has_attachments": has_attachments,
                "is_unread": "UNREAD" in label_ids,
                "sender_emails": sender_emails,
                "recipient_emails": recipient_emails,
                # Date strings the LLM will be asked to enrich with
                # extracted mentions of dates/dollar amounts; leave empty
                # here so the prompt knows what to populate.
                "mentioned_dates": [],
                "mentioned_amounts": [],
                "last_from": last_from,
            }

            signals.append(
                ChannelSignal(
                    id=signal_id,
                    channel=ChannelKind.GMAIL,
                    title=subject[:160],
                    snippet=snippet,
                    link=f"https://mail.google.com/mail/?authuser={quote(mailbox_email, safe='')}#inbox/{tid}",
                    timestamp=last_dt.isoformat() if last_dt else None,
                    entity_kind=EntityKind.THREAD,
                    entity_id=signal_id,
                    entity_label=entity_email or subject[:80],
                    attrs=attrs,
                )
            )

        # Selection, not just a sort. Freshest-first alone is wrong here: it
        # truncates at MAX_SIGNALS_OUT, so on a busy mailbox the thread that
        # has been silent longest — the one a follow-up card exists to surface
        # — was always the first discarded. That made "Bob never came back to
        # you" structurally unreachable for exactly the people who need it,
        # while the agent's own docstring above promised the opposite.
        #
        # So: hold a few slots for the stalest threads the READER spoke last
        # on (direction == "i_sent_last" — the shape of an unanswered ask),
        # then fill the remainder freshest-first as before.
        def _idle(sig) -> float:
            return sig.attrs.get("thread_idle_days") or 0

        signals.sort(key=_idle)
        awaiting_reply = [
            s for s in signals if s.attrs.get("direction") == "i_sent_last"
        ]
        reserved = sorted(awaiting_reply, key=_idle, reverse=True)[
            : self.STALE_RESERVED_SLOTS
        ]
        reserved_ids = {id(s) for s in reserved}
        freshest = [s for s in signals if id(s) not in reserved_ids][
            : self.MAX_SIGNALS_OUT - len(reserved)
        ]

        # Best-first, the BaseChannelAgent.parse_signals contract: the
        # summarizer deals channels round-robin in this order ahead of the
        # decision-maker's 60-signal cap. This list used to be sorted by idle
        # days ascending, which parked exactly the threads reserved above at
        # the END — the first ones cut on a busy day. Alternate the pools
        # instead, freshest ask then stalest silence, so any prefix Gmail is
        # given carries both.
        ranked: list[ChannelSignal] = []
        for i in range(max(len(freshest), len(reserved))):
            if i < len(freshest):
                ranked.append(freshest[i])
            if i < len(reserved):
                ranked.append(reserved[i])

        # A busy work inbox can fill all 20 slots ahead of a connected
        # personal mailbox. Reserve one recent signal from each mailbox with
        # activity, then use the historical ranking for the remaining slots.
        # Work-only runs keep their exact prior ordering and signal IDs.
        mailbox_keys = {sig.attrs.get("gmail_account_id") for sig in signals}
        if len(mailbox_keys) <= 1:
            return ranked
        account_order = [None] if None in mailbox_keys else []
        account_order.extend(sorted(key for key in mailbox_keys if key is not None))
        mailbox_floor = [
            next(
                sig
                for sig in signals
                if sig.attrs.get("gmail_account_id") == account_id
            )
            for account_id in account_order[: self.MAX_SIGNALS_OUT]
        ]
        floor_ids = {id(sig) for sig in mailbox_floor}
        return (mailbox_floor + [sig for sig in ranked if id(sig) not in floor_ids])[
            : self.MAX_SIGNALS_OUT
        ]

    def build_prompt(self, signals: list[ChannelSignal]) -> str:
        thread_lines = []
        for s in signals[: self.MAX_SIGNALS_OUT]:
            attrs = s.attrs
            thread_lines.append(
                "- id={id}\n"
                "  mailbox: {mailbox}\n"
                "  direction: {direction}; idle_days={idle}; messages={n}\n"
                "  reader_last_message_kind: {reader_kind}\n"
                "  addressed_to_reader: {addressing}\n"
                "  subject: {title}\n"
                "  externals: {ext}\n"
                "  last_message_snippet: {snippet}".format(
                    id=s.id,
                    mailbox=attrs.get("gmail_mailbox") or self.user_email,
                    direction=attrs.get("direction"),
                    reader_kind=attrs.get("reader_last_message_kind") or "n/a",
                    idle=attrs.get("thread_idle_days"),
                    n=attrs.get("message_count"),
                    addressing=attrs.get("addressed_to_reader") or "?",
                    title=s.title,
                    ext=", ".join(attrs.get("external_domains") or [])
                    or "(internal only)",
                    snippet=s.snippet[:300],
                )
            )
        thread_block = "\n\n".join(thread_lines) if thread_lines else "(no threads)"

        return (
            # NOTE: this prompt is a pure extractor. No judgment vocabulary
            # ("important", "urgent", "flag") — those words are forbidden
            # by ``test_channel_prompts_contain_no_judgment_words``.
            "You are the Gmail channel extractor. Read the thread metadata "
            "below and return a structured JSON observation. You do NOT "
            "decide what matters — the decision-maker downstream does that.\n\n"
            "## WHOSE MAILBOX THIS IS\n"
            f"You are reading {self.user_email}'s connected mailboxes. Each thread names "
            "its mailbox; keep personal mailbox information attributed to that mailbox. "
            "Each thread's "
            "`addressed_to_reader` says how they were addressed: `direct` (on "
            "To:), `cc` (only copied), or `list` (never individually addressed "
            "— reached them via a distribution list or bcc).\n\n"
            "## YOUR TASK\n"
            "For each thread, populate the enrichment fields from the "
            "last-message snippet:\n"
            "  - asks_of_reader: true only when the snippet asks THIS reader to "
            "do something. A message whose request names someone else "
            '("Ana, please issue the invoice") is false even when it says '
            '"ACTION NEEDED" — being copied on it is not being asked.\n'
            "  - request_owner: the name/email the snippet addresses the "
            "request to, verbatim, or null when it names no one.\n"
            "  - reader_last_message_kind: ONLY for threads whose direction is "
            "i_sent_last — the snippet is then the reader's OWN message. "
            "`ask` when that message poses a question or requests something "
            "from the other party that they have not yet answered; `answer` "
            "when it responds to a request, confirms, approves, declines or "
            "delivers what was asked; `fyi` when it shares information without "
            "asking for anything. The thread line shows the structural default. "
            "When it is `answer` (the reader replied to somebody), override it "
            "only when the snippet clearly says otherwise (a reply that ends in "
            "a question is `ask`). When it is `ask` (the reader opened the "
            "thread and nobody has written back), return `ask`. Omit the field "
            "for every other direction.\n"
            "  - mentioned_dates: for EVERY date the snippet refers to, output "
            "an object resolved against the TEMPORAL CONTEXT date above: "
            '{"raw": "next Tuesday", "resolved": "2026-06-30", "tense": '
            '"future"}. Resolve relative phrases ("Friday EOD", "tomorrow") '
            "to an absolute ISO date; set tense to past/today/future relative "
            "to today. If you cannot resolve it, use resolved=null, "
            'tense="unknown".\n'
            "  - mentioned_amounts: dollar/percentage figures referenced.\n\n"
            # `has_decision_verb` used to be asked for here as well. Nothing
            # downstream read it (it is not in ATTRS_PREVIEW_KEYS), so it
            # was output tokens paid for on every thread of every run.
            "## OUTPUT (raw JSON only)\n"
            "{\n"
            '  "summary": "1 factual sentence describing what was in the user\'s recent Gmail activity.",\n'
            '  "thread_attrs": {\n'
            '    "<thread_id>": {\n'
            '      "asks_of_reader": true | false,\n'
            '      "request_owner": "..." | null,\n'
            '      "reader_last_message_kind": "ask" | "answer" | "fyi",\n'
            '      "mentioned_dates": [{"raw": "...", "resolved": "YYYY-MM-DD|null", "tense": "past|today|future|unknown"}],\n'
            '      "mentioned_amounts": ["..."]\n'
            "    }\n"
            "  }\n"
            "}\n\n"
            f"## THREADS\n{thread_block}\n"
        )

    # ── Internals ────────────────────────────────────────────────────────

    def _user_domain(self) -> str:
        if "@" in self.user_email:
            return self.user_email.split("@", 1)[1].lower()
        return ""

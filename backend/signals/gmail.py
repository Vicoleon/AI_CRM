"""Gmail SignalSource — reshapes raw Gmail threads into UC-17 Signals.

Reuses `backend.services.gmail_service.GmailService` for OAuth + thread
listing; no new Gmail client lives here. The correlator (UC-17.7) joins
Gmail signals to HubSpot deals on `entity_refs.contact_email`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any

from backend.signals._lookback import widen_for_weekend
from backend.signals.types import Signal

logger = logging.getLogger(__name__)


AWAITING_REPLY_HOURS = 48


_NEWSLETTER_LOCAL_PARTS: tuple[str, ...] = (
    "noreply",
    "no-reply",
    "donotreply",
    "do-not-reply",
    "mailer-daemon",
    "notifications",
    "notification",
    "newsletter",
    "bounces",
    "automated",
)


@dataclass
class _MessageView:
    """Lightweight per-message projection of a Gmail metadata-format message."""

    message_id: str
    from_email: str
    from_name: str
    subject: str
    date: datetime
    is_unread: bool
    has_list_unsubscribe: bool


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _parse_date(raw: str) -> datetime:
    if not raw:
        return datetime.now(timezone.utc)
    try:
        dt = parsedate_to_datetime(raw)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def _headers(msg: dict[str, Any]) -> dict[str, str]:
    return {
        (h.get("name") or "").lower(): h.get("value") or ""
        for h in msg.get("payload", {}).get("headers", [])
    }


def _project_message(msg: dict[str, Any]) -> _MessageView:
    h = _headers(msg)
    name, addr = parseaddr(h.get("from", ""))
    return _MessageView(
        message_id=msg.get("id", ""),
        from_email=(addr or "").lower(),
        from_name=name or "",
        subject=h.get("subject", "(no subject)"),
        date=_parse_date(h.get("date", "")),
        is_unread="UNREAD" in (msg.get("labelIds") or []),
        has_list_unsubscribe=bool(h.get("list-unsubscribe")),
    )


def _is_automated_sender(view: _MessageView) -> bool:
    """Newsletter / no-reply heuristic.

    Triggers on:
      - presence of `List-Unsubscribe` header (RFC 2369; bulk/marketing mail), OR
      - sender local-part matching common automated patterns (noreply@, etc).
    """
    if view.has_list_unsubscribe:
        return True
    local = view.from_email.split("@", 1)[0] if "@" in view.from_email else ""
    local = local.lower()
    return any(p in local for p in _NEWSLETTER_LOCAL_PARTS)


def _is_external(from_email: str, user_email: str) -> bool:
    if not from_email or "@" not in from_email:
        return False
    user_domain = user_email.split("@", 1)[1].lower() if "@" in user_email else ""
    return from_email.split("@", 1)[1].lower() != user_domain


def _last_external_view(
    messages: list[_MessageView], user_email: str
) -> _MessageView | None:
    for v in reversed(messages):
        if _is_external(v.from_email, user_email):
            return v
    return None


def _maybe_widen_to_friday_eob(
    since: datetime,
    *,
    now: datetime,
    tz_name: str | None = None,
) -> tuple[datetime, bool]:
    """Back-compat shim over `widen_for_weekend`. Preserved so the existing
    test imports keep working; new sources should import the shared helper
    from `backend.signals._lookback` directly."""
    return widen_for_weekend(since, now=now, user_tz=tz_name)


# ─── Signal builders ──────────────────────────────────────────────────────────


def _unread_severity(sender_view: _MessageView) -> str:
    if _is_automated_sender(sender_view):
        return "low"
    return "medium"


def _build_summary(prefix: str, sender_email: str, subject: str) -> str:
    raw = f"{prefix} from {sender_email} — '{subject}'"
    if len(raw) <= 140:
        return raw
    return raw[:137] + "..."


def _unread_thread_signal(
    *,
    user_email: str,
    thread_id: str,
    last_external: _MessageView,
    last_message_at: datetime,
    widened: bool,
) -> Signal:
    return Signal(
        user_email=user_email,
        source="gmail",
        signal_type="unread_thread",
        summary=_build_summary(
            "Unread thread", last_external.from_email, last_external.subject
        ),
        occurred_at=last_external.date,
        entity_refs={
            k: v
            for k, v in {
                "thread_id": thread_id,
                "contact_email": last_external.from_email,
            }.items()
            if v
        },
        severity_hint=_unread_severity(last_external),
        payload={
            "thread_id": thread_id,
            "last_message_at": last_message_at.isoformat(),
            "sender_email": last_external.from_email,
            "sender_name": last_external.from_name,
            "subject": last_external.subject,
            "is_unread": True,
            "has_list_unsubscribe": last_external.has_list_unsubscribe,
            "lookback_widened_to_friday": widened,
        },
    )


def _awaiting_reply_signal(
    *,
    user_email: str,
    thread_id: str,
    last_rep: _MessageView,
    last_external: _MessageView | None,
    widened: bool,
    now: datetime,
) -> Signal:
    counterparty = last_external.from_email if last_external else "(unknown)"
    subject = last_rep.subject
    return Signal(
        user_email=user_email,
        source="gmail",
        signal_type="awaiting_reply",
        summary=_build_summary("Awaiting reply", counterparty, subject),
        occurred_at=last_rep.date,
        entity_refs={
            k: v
            for k, v in {
                "thread_id": thread_id,
                "contact_email": last_external.from_email if last_external else None,
            }.items()
            if v
        },
        severity_hint="medium",
        payload={
            "thread_id": thread_id,
            "last_message_at": last_rep.date.isoformat(),
            "sender_email": last_rep.from_email,
            "sender_name": last_rep.from_name,
            "subject": subject,
            "is_unread": False,
            "lookback_widened_to_friday": widened,
            "hours_since_rep_message": round(
                (now - last_rep.date).total_seconds() / 3600,
                1,
            ),
        },
    )


# ─── SignalSource ─────────────────────────────────────────────────────────────


class GmailSignalSource:
    """Implements `SignalSource`. Pulls recent threads via `GmailService` and
    reshapes them into `unread_thread` / `awaiting_reply` Signals.

    The judge (UC-17.6) decides which signals become priorities; we emit the
    superset and let it filter.
    """

    name = "gmail"

    def __init__(
        self,
        gmail_service: Any | None = None,
        *,
        tz_name: str | None = None,
        now: datetime | None = None,
    ):
        self._svc = gmail_service
        self._tz_name = tz_name
        self._now = now

    def _service_for(self, user_email: str) -> Any | None:
        if self._svc is not None:
            return self._svc
        try:
            from backend.services.gmail_service import GmailService

            return GmailService(user_email=user_email)
        except Exception as e:
            logger.warning(
                "[signals.gmail] GmailService init failed for %s: %s", user_email, e
            )
            return None

    def fetch(self, user_email: str, *, since: datetime) -> list[Signal]:
        svc = self._service_for(user_email)
        if svc is None:
            return []

        now = self._now or datetime.now(timezone.utc)
        effective_since, widened = _maybe_widen_to_friday_eob(
            since,
            now=now,
            tz_name=self._tz_name,
        )

        try:
            threads = svc.list_threads_for_signals(since=effective_since)
        except Exception as e:
            logger.warning("[signals.gmail] list_threads_for_signals failed: %s", e)
            return []

        signals: list[Signal] = []
        rep = user_email.lower()
        awaiting_cutoff = now - timedelta(hours=AWAITING_REPLY_HOURS)

        for thread in threads:
            thread_id = thread.get("id") or ""
            raw_msgs = thread.get("messages") or []
            if not raw_msgs:
                continue
            views = [_project_message(m) for m in raw_msgs]
            views.sort(key=lambda v: v.date)
            last = views[-1]

            if last.from_email == rep:
                if last.date < awaiting_cutoff:
                    last_external = _last_external_view(views, rep)
                    signals.append(
                        _awaiting_reply_signal(
                            user_email=rep,
                            thread_id=thread_id,
                            last_rep=last,
                            last_external=last_external,
                            widened=widened,
                            now=now,
                        )
                    )
                continue

            if not _is_external(last.from_email, rep):
                continue

            if last.is_unread:
                signals.append(
                    _unread_thread_signal(
                        user_email=rep,
                        thread_id=thread_id,
                        last_external=last,
                        last_message_at=last.date,
                        widened=widened,
                    )
                )

        logger.info(
            "[signals.gmail] user=%s emitted=%d (since=%s, widened=%s)",
            rep,
            len(signals),
            effective_since.isoformat(),
            widened,
        )
        return signals

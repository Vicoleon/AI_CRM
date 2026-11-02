"""Slack SignalSource — reshapes UC-15 SlackService output into UC-17 Signals.

Reuses `backend.services.slack_service.SlackService`; no new Slack client lives
here. The correlator (UC-17.7) joins Slack signals to HubSpot deals via
channel-name → company fuzzy match (handled there), so `entity_refs` only
includes `contact_email` when the sender's email is explicitly resolvable
(rare for internal Slack — common for Slack Connect).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from backend.signals._lookback import widen_for_weekend
from backend.signals.types import Signal

logger = logging.getLogger(__name__)


# Heuristic: channel-name substrings that imply a customer-facing context, used
# to bump mention severity to "high". Light by design — the judge (UC-17.6)
# does the real ranking. TODO(UC-17 follow-up): replace with a config-driven
# list owned by Sales ops, mirroring DEPRECATED_STAGE_PATTERNS in hubspot.py.
CUSTOMER_FACING_CHANNEL_PATTERNS: tuple[str, ...] = (
    "deals-",
    "deal-",
    "customers-",
    "customer-",
    "client-",
    "clients-",
    "account-",
    "accounts-",
    "shared-",  # Slack Connect convention
    "ext-",  # common external-channel convention
)


def _is_customer_facing(channel_name: str | None) -> bool:
    if not channel_name:
        return False
    name = channel_name.lower().lstrip("#")
    return any(p in name for p in CUSTOMER_FACING_CHANNEL_PATTERNS)


def _is_dm_channel(channel_id: str | None) -> bool:
    return bool(channel_id) and channel_id.startswith("D")


def _truncate(text: str, limit: int) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _ts_to_datetime(ts: str) -> datetime:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def _mention_severity(*, channel_name: str | None, is_dm: bool) -> str:
    if is_dm:
        return "high"
    if _is_customer_facing(channel_name):
        return "high"
    return "medium"


def _build_summary(
    *, sender: str, channel_name: str | None, is_dm: bool, text: str
) -> str:
    where = (
        "DM" if is_dm else f"#{channel_name}" if channel_name else "(unknown channel)"
    )
    snippet = _truncate(text, 60)
    raw = f"@-mentioned by {sender} in {where} — '{snippet}'"
    if len(raw) <= 140:
        return raw
    return raw[:137] + "..."


def _mention_signal(
    *,
    user_email: str,
    msg: Any,
    is_dm: bool,
    widened: bool,
) -> Signal:
    sender = msg.user_name or msg.user_id or "(unknown)"
    return Signal(
        user_email=user_email,
        source="slack",
        signal_type="mention",
        summary=_build_summary(
            sender=sender,
            channel_name=msg.channel_name,
            is_dm=is_dm,
            text=msg.text,
        ),
        occurred_at=_ts_to_datetime(msg.ts),
        entity_refs={
            k: v
            for k, v in {
                "channel_id": msg.channel_id,
                "message_ts": msg.ts,
            }.items()
            if v
        },
        severity_hint=_mention_severity(
            channel_name=msg.channel_name,
            is_dm=is_dm,
        ),
        payload={
            "channel_id": msg.channel_id,
            "channel_name": msg.channel_name,
            "message_ts": msg.ts,
            "sender_user_id": msg.user_id,
            "sender_display_name": msg.user_name,
            "text": _truncate(msg.text, 200),
            "permalink": msg.permalink,
            "is_dm": is_dm,
            "lookback_widened_to_friday": widened,
        },
    )


# ─── SignalSource ─────────────────────────────────────────────────────────────


class SlackSignalSource:
    """Implements `SignalSource`. Pulls @-mentions (and DMs) via SlackService
    and reshapes them into `mention` Signals.

    Minimum-viable signal set per UC-17.3: only `mention` ships. Pinned items
    and unread-channel-burst signals are deferred — SlackService doesn't
    cleanly expose those today and this subtask is forbidden from adding new
    Slack API surface. TODO(UC-17 follow-up): once SlackService grows pins /
    unread-count helpers, emit `pinned_action_item` and `unread_channel_burst`
    from here.
    """

    name = "slack"

    def __init__(
        self,
        slack_service: Any | None = None,
        *,
        tz_name: str | None = None,
        now: datetime | None = None,
    ):
        self._svc = slack_service
        self._tz_name = tz_name
        self._now = now

    def _service_for(self, user_email: str) -> Any | None:
        if self._svc is not None:
            return self._svc
        try:
            from backend.services.slack_service import (
                SlackNotConnectedError,
                SlackService,
            )

            try:
                return SlackService(user_email=user_email)
            except SlackNotConnectedError:
                return None
        except Exception as e:
            logger.warning(
                "[signals.slack] SlackService init failed for %s: %s", user_email, e
            )
            return None

    def fetch(self, user_email: str, *, since: datetime) -> list[Signal]:
        svc = self._service_for(user_email)
        if svc is None:
            return []

        # No Slack user_id resolved means we can't form `<@uid>` queries and
        # everything downstream short-circuits to empty anyway. Bail cleanly.
        try:
            slack_uid = svc._slack_user_id()
        except Exception as e:
            logger.warning(
                "[signals.slack] _slack_user_id failed for %s: %s", user_email, e
            )
            return []
        if not slack_uid:
            logger.info(
                "[signals.slack] no Slack user_id resolved for %s; skipping", user_email
            )
            return []

        now = self._now or datetime.now(timezone.utc)
        effective_since, widened = widen_for_weekend(
            since,
            now=now,
            user_tz=self._tz_name,
        )

        try:
            mentions = svc.get_mentions(since=effective_since)
        except Exception as e:
            logger.warning("[signals.slack] get_mentions failed: %s", e)
            mentions = []

        signals: list[Signal] = []
        rep = user_email.lower()
        for m in mentions:
            is_dm = _is_dm_channel(m.channel_id)
            signals.append(
                _mention_signal(
                    user_email=rep,
                    msg=m,
                    is_dm=is_dm,
                    widened=widened,
                )
            )

        logger.info(
            "[signals.slack] user=%s emitted=%d (since=%s, widened=%s)",
            rep,
            len(signals),
            effective_since.isoformat(),
            widened,
        )
        return signals

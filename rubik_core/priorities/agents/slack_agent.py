"""Slack channel agent — UC-22 / UC-24 Slack signal extractor.

Pulls @-mentions, direct messages (DMs), and discussions in customer-facing
and shared channels from SlackService, emitting typed `ChannelSignal`s with
`channel=ChannelKind.SLACK`.

The LLM enrichment extracts:
  - Factual one-sentence summary of Slack activity.
  - The dates each message mentions, into `slack_attrs`.

It used to ask for `action_required`, `ask_summary` and `urgency_hint` per
message as well, and seeded heuristic versions of them here. Nothing downstream
read any of the three (none is in decision_maker.ATTRS_PREVIEW_KEYS), so they
were output tokens paid for on every message and are gone. `urgency_hint` was
also judgment vocabulary in an extractor prompt, which UC-22 forbids.

This agent is a pure extractor: it observes and structures Slack activity;
the cross-channel summarizer and decision-maker perform ranking and synthesis.

One deterministic filter lives here rather than in the decision-maker's prompt:
a message the reader has ALREADY ANSWERED never becomes a signal. "Answered"
means the reader posted in the message's thread after it, or — for a DM —
posted anything later on that DM's timeline. The commonest "not useful"
rating is that complaint ("I had answered that already"), and a prompt
rule is the wrong tool for a fact we can check.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.signals._lookback import widen_for_weekend
from backend.signals.slack import _is_customer_facing
from rubik_core.priorities.agents.base import BaseChannelAgent
from rubik_core.priorities.models import ChannelKind, ChannelSignal, EntityKind

logger = logging.getLogger(__name__)


def _ts_to_iso(ts: str | None) -> str | None:
    if not ts:
        return None
    try:
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        return dt.isoformat()
    except (TypeError, ValueError):
        return None


class SlackChannelAgent(BaseChannelAgent):
    channel = ChannelKind.SLACK
    signal_attrs_key = "slack_attrs"
    # build_prompt's INSTRUCTIONS; `is_dm`, `customer_facing` and the rest are
    # computed in parse_signals.
    model_attr_keys = frozenset({"mentioned_dates"})

    LOOKBACK_HOURS = 24
    MAX_SIGNALS_OUT = 20

    def fetch_signals(self) -> list[dict[str, Any]]:
        """Pull recent Slack mentions, DMs, and customer channel messages."""
        try:
            from backend.services.slack_service import (
                SlackAPIError,
                SlackNotConnectedError,
                SlackService,
            )
        except Exception as exc:
            logger.warning(
                "[priorities.slack_agent] SlackService import failed: %s", exc
            )
            return []

        try:
            svc = SlackService(user_email=self.user_email)
        except Exception as exc:
            logger.warning(
                "[priorities.slack_agent] SlackService init failed for %s: %s",
                self.user_email,
                exc,
            )
            return []

        if not svc.is_connected:
            return []

        slack_uid = ""
        try:
            slack_uid = svc._slack_user_id()
        except Exception as exc:
            logger.warning(
                "[priorities.slack_agent] failed to resolve slack_uid for %s: %s",
                self.user_email,
                exc,
            )
            return []

        now = datetime.now(timezone.utc)
        since = now - timedelta(hours=self.LOOKBACK_HOURS)
        # No temporal context: the company's default zone (widen_for_weekend).
        tz_name = self.temporal.tz_name if self.temporal else None
        effective_since, _ = widen_for_weekend(since, now=now, user_tz=tz_name)

        collected: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        # Reader's own message timestamps per conversation. Own messages are
        # never signals, but they are the evidence that an earlier ask in the
        # same DM was answered — see ``_drop_answered``.
        own_ts_by_channel: dict[str, list[float]] = {}

        def _add_msg(
            m: Any,
            *,
            is_dm: bool = False,
            is_mention: bool = False,
            channel_name: str | None = None,
        ) -> None:
            ch_id = getattr(m, "channel_id", None) or ""
            ts = getattr(m, "ts", None) or ""
            user_id = getattr(m, "user_id", None) or ""
            if not ch_id or not ts:
                return
            # Don't create signals for own messages — but remember WHEN the
            # reader spoke, so an ask they already answered can be dropped.
            if slack_uid and user_id == slack_uid:
                try:
                    own_ts_by_channel.setdefault(ch_id, []).append(float(ts))
                except (TypeError, ValueError):
                    pass
                return
            key = f"{ch_id}:{ts}"
            if key in seen_keys:
                return
            seen_keys.add(key)
            collected.append(
                {
                    "channel_id": ch_id,
                    "channel_name": getattr(m, "channel_name", None)
                    or channel_name
                    or "",
                    "ts": ts,
                    "user_id": user_id,
                    "user_name": getattr(m, "user_name", None) or user_id or "someone",
                    "text": getattr(m, "text", "") or "",
                    "permalink": getattr(m, "permalink", None),
                    "app_link": getattr(m, "app_link", None)
                    or svc._app_link(ch_id, ts),
                    "is_dm": is_dm or ch_id.startswith("D"),
                    "is_mention": is_mention or getattr(m, "is_mention", False),
                    "thread_ts": getattr(m, "thread_ts", None),
                    "reply_count": getattr(m, "reply_count", None),
                }
            )

        def _finish(*, fetch_threads: bool = True) -> list[dict[str, Any]]:
            return self._drop_answered(
                collected[: self.MAX_SIGNALS_OUT],
                svc=svc,
                slack_uid=slack_uid,
                own_ts_by_channel=own_ts_by_channel,
                fetch_threads=fetch_threads,
            )

        # 1. Fetch @-mentions
        try:
            mentions = svc.get_mentions(since=effective_since, limit=15)
            for m in mentions:
                _add_msg(m, is_mention=True)
                if len(collected) >= self.MAX_SIGNALS_OUT:
                    break
        except (SlackNotConnectedError, SlackAPIError) as exc:
            if getattr(exc, "error", None) == "ratelimited":
                logger.warning(
                    "[priorities.slack_agent] Slack API rate-limited during mentions fetch for %s",
                    self.user_email,
                )
                return _finish(fetch_threads=False)
            logger.info(
                "[priorities.slack_agent] mentions fetch skipped for %s: %s",
                self.user_email,
                exc,
            )
        except Exception as exc:
            logger.warning(
                "[priorities.slack_agent] mentions fetch failed for %s: %s",
                self.user_email,
                exc,
            )

        # 2. Fetch recent DMs (bounded to top 5)
        if len(collected) < self.MAX_SIGNALS_OUT:
            try:
                dm_threads = svc.get_dm_threads(limit=5)
                for dm in dm_threads:
                    ch_id = dm.get("channel_id")
                    if not ch_id:
                        continue
                    counterparty = dm.get("counterparty_name") or "DM"
                    try:
                        msgs = svc.get_channel_messages(
                            ch_id, since=effective_since, limit=8, skip_permalink=True
                        )
                        for m in msgs:
                            _add_msg(m, is_dm=True, channel_name=counterparty)
                            if len(collected) >= self.MAX_SIGNALS_OUT:
                                break
                    except SlackAPIError as exc:
                        if exc.error == "ratelimited":
                            logger.warning(
                                "[priorities.slack_agent] Slack rate-limited during DM scan for %s",
                                self.user_email,
                            )
                            return _finish(fetch_threads=False)
                        logger.debug(
                            "[priorities.slack_agent] dm channel %s scan skipped: %s",
                            ch_id,
                            exc.error,
                        )
                    except Exception as exc:
                        logger.debug(
                            "[priorities.slack_agent] dm channel %s scan failed: %s",
                            ch_id,
                            exc,
                        )
                    if len(collected) >= self.MAX_SIGNALS_OUT:
                        break
            except Exception as exc:
                logger.debug(
                    "[priorities.slack_agent] DM threads fetch failed for %s: %s",
                    self.user_email,
                    exc,
                )

        # 3. Fetch recent customer-facing / shared channels (bounded to top 5)
        if len(collected) < self.MAX_SIGNALS_OUT:
            try:
                channels = svc.list_member_channels(
                    types="public_channel,private_channel", max_channels=15
                )
                customer_channels = [
                    ch
                    for ch in channels
                    if ch.get("id")
                    and _is_customer_facing((ch.get("name") or "").lower())
                ][:5]
                for ch in customer_channels:
                    ch_name = ch.get("name") or ""
                    ch_id = ch["id"]
                    try:
                        msgs = svc.get_channel_messages(
                            ch_id, since=effective_since, limit=8, skip_permalink=True
                        )
                        for m in msgs:
                            _add_msg(m, is_dm=False, channel_name=ch_name)
                            if len(collected) >= self.MAX_SIGNALS_OUT:
                                break
                    except SlackAPIError as exc:
                        if exc.error == "ratelimited":
                            logger.warning(
                                "[priorities.slack_agent] Slack rate-limited during channel scan for %s",
                                self.user_email,
                            )
                            return _finish(fetch_threads=False)
                        logger.debug(
                            "[priorities.slack_agent] channel %s scan skipped: %s",
                            ch_name,
                            exc.error,
                        )
                    except Exception as exc:
                        logger.debug(
                            "[priorities.slack_agent] channel %s scan failed: %s",
                            ch_name,
                            exc,
                        )
                    if len(collected) >= self.MAX_SIGNALS_OUT:
                        break
            except Exception as exc:
                logger.debug(
                    "[priorities.slack_agent] member channels fetch failed for %s: %s",
                    self.user_email,
                    exc,
                )

        return _finish()

    def _drop_answered(
        self,
        items: list[dict[str, Any]],
        *,
        svc: Any,
        slack_uid: str,
        own_ts_by_channel: dict[str, list[float]],
        fetch_threads: bool = True,
    ) -> list[dict[str, Any]]:
        """Remove messages the reader has already answered.

        Two independent checks, either one is enough:

        * **Thread** — the reader posted a reply in the message's thread after
          it. The parent is the message's own ``thread_ts`` when it is itself
          a threaded reply, otherwise the message when it has replies (or when
          the source could not say — search matches never carry a count).
          Known reply-less parents cost no API call.
        * **DM timeline** — the message is a DM and the reader posted anything
          in that DM afterwards. In a two-person conversation a later message
          from the reader is a reply; in a channel it usually is not, so the
          timeline check is deliberately DM-only.

        ``fetch_threads=False`` keeps the DM check but skips the thread fetch —
        used on the rate-limited exits so we never stack more calls on a 429.
        A fetch that fails is treated as "unknown", and unknown keeps the
        message: dropping a real ask is worse than repeating a handled one.
        """
        if not slack_uid or not items:
            return items

        kept: list[dict[str, Any]] = []
        dropped = 0
        for item in items:
            ch_id = item.get("channel_id") or ""
            ts_raw = item.get("ts") or ""
            try:
                ts = float(ts_raw)
            except (TypeError, ValueError):
                kept.append(item)
                continue

            answered_via: str | None = None

            if item.get("is_dm") and any(
                own > ts for own in own_ts_by_channel.get(ch_id, ())
            ):
                answered_via = "dm"

            if answered_via is None and fetch_threads:
                thread_ts = item.get("thread_ts")
                reply_count = item.get("reply_count")
                if thread_ts and thread_ts != ts_raw:
                    parent = thread_ts
                elif reply_count is None or (
                    isinstance(reply_count, int) and reply_count > 0
                ):
                    parent = ts_raw
                else:
                    parent = None
                if parent and self._reader_replied_in_thread(
                    svc, ch_id, parent, after_ts=ts, slack_uid=slack_uid
                ):
                    answered_via = "thread"

            if answered_via:
                dropped += 1
                logger.debug(
                    "[priorities.slack_agent] dropping %s:%s for %s — reader already replied (%s)",
                    ch_id,
                    ts_raw,
                    self.user_email,
                    answered_via,
                )
                continue
            kept.append(item)

        if dropped:
            logger.info(
                "[priorities.slack_agent] %d of %d Slack message(s) dropped for %s: reader already replied",
                dropped,
                len(items),
                self.user_email,
            )
        return kept

    @staticmethod
    def _reader_replied_in_thread(
        svc: Any, channel_id: str, thread_ts: str, *, after_ts: float, slack_uid: str
    ) -> bool:
        """True when ``slack_uid`` posted in the thread after ``after_ts``.

        Uses ``SlackService._fetch_thread`` (``conversations.replies``, cached
        per instance). Any failure or unexpected shape reads as False so a
        transport hiccup never suppresses a real ask.
        """
        try:
            replies = svc._fetch_thread(channel_id, thread_ts)
        except Exception as exc:  # noqa: BLE001 — unknown must not drop the ask
            logger.debug(
                "[priorities.slack_agent] thread fetch failed for %s/%s: %s",
                channel_id,
                thread_ts,
                exc,
            )
            return False
        if not isinstance(replies, list):
            return False
        for reply in replies:
            if not isinstance(reply, dict):
                continue
            if (reply.get("user") or "") != slack_uid:
                continue
            try:
                if float(reply.get("ts") or 0) > after_ts:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def parse_signals(self, raw_items: list[dict[str, Any]]) -> list[ChannelSignal]:
        signals: list[ChannelSignal] = []

        for item in raw_items:
            ch_id = item.get("channel_id") or ""
            ts = item.get("ts") or ""
            sig_id = f"slack::{ch_id}::{ts}"
            text = (item.get("text") or "").strip()
            if not text:
                continue

            is_dm = bool(item.get("is_dm"))
            is_mention = bool(item.get("is_mention"))
            ch_name = item.get("channel_name") or ""
            user_name = item.get("user_name") or "someone"
            customer_facing = _is_customer_facing(ch_name)

            if is_dm:
                title = f"Slack DM from {user_name}"
                entity_kind = EntityKind.PERSON
                entity_id = item.get("user_id") or user_name.lower()
                entity_label = user_name
            elif is_mention:
                where = f"#{ch_name}" if ch_name else "Slack"
                title = f"Slack mention by {user_name} in {where}"
                entity_kind = EntityKind.COMPANY if customer_facing else None
                entity_id = ch_name if customer_facing else (ch_id or None)
                entity_label = f"#{ch_name}" if ch_name else user_name
            else:
                title = (
                    f"Slack message in #{ch_name}"
                    if ch_name
                    else f"Slack message in {ch_id}"
                )
                entity_kind = EntityKind.COMPANY if customer_facing else None
                entity_id = ch_name if customer_facing else (ch_id or None)
                entity_label = f"#{ch_name}" if ch_name else ch_id

            timestamp_iso = _ts_to_iso(ts)
            link = item.get("app_link") or item.get("permalink")

            attrs: dict[str, Any] = {
                "channel_id": ch_id,
                "channel_name": ch_name,
                "message_ts": ts,
                "sender_user_id": item.get("user_id"),
                "sender_user_name": user_name,
                "is_dm": is_dm,
                "is_mention": is_mention,
                "customer_facing": customer_facing,
                "text": text[:300],
                "mentioned_dates": [],
            }

            signals.append(
                ChannelSignal(
                    id=sig_id,
                    channel=ChannelKind.SLACK,
                    title=title[:160],
                    snippet=text[:300],
                    link=link,
                    timestamp=timestamp_iso,
                    entity_kind=entity_kind,
                    entity_id=entity_id,
                    entity_label=entity_label,
                    attrs=attrs,
                )
            )

        return signals

    def build_prompt(self, signals: list[ChannelSignal]) -> str:
        msg_lines = []
        for s in signals:
            attrs = s.attrs
            msg_lines.append(
                "- id={id}\n"
                "  title: {title}\n"
                "  sender: {sender}\n"
                "  channel: {channel}\n"
                "  is_dm: {is_dm}  is_mention: {is_mention}  customer_facing: {customer_facing}\n"
                "  text: {text}".format(
                    id=s.id,
                    title=s.title,
                    sender=attrs.get("sender_user_name"),
                    channel=attrs.get("channel_name") or attrs.get("channel_id"),
                    is_dm=attrs.get("is_dm"),
                    is_mention=attrs.get("is_mention"),
                    customer_facing=attrs.get("customer_facing"),
                    text=attrs.get("text", "")[:300],
                )
            )
        msg_block = "\n".join(msg_lines)

        return (
            "You are the Slack Channel Extractor. Your task is to extract factual "
            "observations from recent Slack conversations, direct messages, and mentions.\n\n"
            "MESSAGES:\n"
            f"{msg_block}\n\n"
            "INSTRUCTIONS:\n"
            "1. Output a JSON object with two top-level keys:\n"
            '   - "summary": A concise, factual one-sentence description of recent Slack activity '
            '(e.g., "Received 2 mentions in customer channels and 1 DM regarding onboarding."). '
            "Do NOT include conversational filler or recommendations.\n"
            '   - "slack_attrs": A dictionary keyed by message `id` containing:\n'
            '     * "mentioned_dates": list of date strings mentioned in the text (e.g. ["2026-06-25", "tomorrow at 3pm"]), if any\n\n'
            "Return valid JSON only."
        )

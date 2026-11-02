"""HubSpot channel agent — UC-22.

Wraps the existing ``backend/signals/hubspot.py`` HubspotSignalSource
without duplicating queries. Emits one ``ChannelSignal`` per deal
(deduped across the source's stale / stage-stuck / new-product /
market-trigger probes) tagged with ``entity_kind=DEAL`` so the
decision-maker can join it with Gmail threads, calendar events, and
any other channel that touches the same deal or contact.

This agent is a pure extractor — no judgment, no urgency, no
"flag". HubSpot data is already structured, so the LLM call mostly
just composes the one-sentence ``summary``; the per-signal attrs are
populated deterministically from the BigQuery rows.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from rubik_core.priorities.agents.base import BaseChannelAgent
from rubik_core.priorities.models import ChannelKind, ChannelSignal, EntityKind

logger = logging.getLogger(__name__)


class HubSpotChannelAgent(BaseChannelAgent):
    channel = ChannelKind.HUBSPOT

    # The decision-maker rarely benefits from more than a few dozen
    # deals — bounded to keep token cost predictable.
    MAX_SIGNALS_OUT = 30

    # Default lookback window. The underlying probes do their own
    # bounding too; ``since`` is forwarded for protocol conformance.
    FETCH_LOOKBACK_DAYS = 30

    def fetch_signals(self) -> list:
        try:
            from backend.signals.hubspot import HubspotSignalSource
        except Exception as exc:  # noqa: BLE001 — module is optional
            logger.warning("HubSpot signal source unavailable: %s", exc)
            return []

        since = datetime.now(timezone.utc) - timedelta(days=self.FETCH_LOOKBACK_DAYS)
        try:
            source = HubspotSignalSource()
            return source.fetch(self.user_email, since=since) or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("HubSpot fetch failed for %s: %s", self.user_email, exc)
            return []

    def parse_signals(self, raw_items: list) -> list[ChannelSignal]:
        # raw_items is a list of backend.signals.types.Signal objects. A
        # single deal can appear under multiple signal_types (stale +
        # stage_stuck, etc.) — we collapse them into one ChannelSignal
        # per deal_id with the union of attrs.
        by_deal_id: dict[str, ChannelSignal] = {}
        no_deal_id: list[ChannelSignal] = []

        for src_sig in raw_items:
            refs = getattr(src_sig, "entity_refs", {}) or {}
            payload = getattr(src_sig, "payload", {}) or {}
            sig_type = getattr(src_sig, "signal_type", "") or ""
            summary = getattr(src_sig, "summary", "") or ""
            severity = getattr(src_sig, "severity_hint", "medium") or "medium"
            timestamp = _signal_timestamp(src_sig, payload)

            deal_id = refs.get("deal_id") or payload.get("deal_id")
            contact_email = (refs.get("contact_email") or "").lower() or None
            company_name = refs.get("company_name") or payload.get("company_name")
            company_domain = (refs.get("company_domain") or "").lower() or None
            stage = payload.get("stage") or refs.get("stage")

            attrs: dict[str, Any] = {
                # Signal-type bag — preserves WHICH probes flagged this deal.
                # The decision-maker reads this list, not the raw probe rows.
                "signal_types": [sig_type] if sig_type else [],
                "severity_hint": severity,
                "stage": stage,
                "deal_amount": payload.get("deal_amount") or payload.get("amount"),
                "owner_email": (payload.get("owner_email") or "").lower() or None,
                "contact_email": contact_email,
                "company_name": company_name,
                "company_domain": company_domain,
                "stage_age_days": payload.get("stage_age_days"),
                "days_since_engagement": payload.get("days_since_engagement"),
                "last_engagement_at": payload.get("last_engagement_at"),
                # Present only when the client's company record was contacted
                # more recently than the deal itself (an email to someone at
                # the client who is not on the deal is logged there). Beside
                # the deal's own date above, never instead of it.
                "client_days_since_engagement": payload.get(
                    "client_days_since_engagement"
                ),
                "client_last_engagement_at": payload.get("client_last_engagement_at"),
                "client_name": payload.get("client_name"),
                "close_date": payload.get("close_date"),
                # Negative = the date has already passed. The plate rules rank
                # deal cards by value and nearness, and tell the model whether
                # the date has slipped — all of which is unreadable unless the
                # attrs preview renders these two.
                "days_to_close": payload.get("days_to_close"),
                "close_date_passed": bool(payload.get("close_date_passed")),
                # True when the deal has been silent long enough that the CRM
                # row is more likely out of date than actionable (see
                # backend/signals/hubspot.py DEAL_PRESUMED_DEAD_DAYS). The
                # decision-maker must not read this as urgency.
                "presumed_dead": bool(payload.get("presumed_dead")),
                # Carry the original probe payload for the decision-maker
                # to cite without us re-deriving everything here.
                "probe_payload": payload,
            }

            link = payload.get("hubspot_url") or refs.get("hubspot_url")

            sig = ChannelSignal(
                id=str(deal_id) if deal_id else f"hubspot::{sig_type}::{id(src_sig)}",
                channel=ChannelKind.HUBSPOT,
                # _live_rows sets company_name=None for every deal ("deal
                # names already carry the client"), so without
                # deal_name every HubSpot card was titled the literal string
                # "HubSpot deal" — and the Settings → Handled priorities row
                # read "Close date at risk · deal <an id>", which no AE
                # would recognise as their own account.
                title=(
                    company_name
                    or payload.get("deal_name")
                    or contact_email
                    or "HubSpot deal"
                )[:160],
                snippet=summary[:300],
                link=link,
                timestamp=timestamp,
                entity_kind=EntityKind.DEAL if deal_id else None,
                entity_id=str(deal_id) if deal_id else None,
                entity_label=company_name or payload.get("deal_name") or contact_email,
                attrs=attrs,
            )

            if deal_id is None:
                no_deal_id.append(sig)
                continue

            key = str(deal_id)
            existing = by_deal_id.get(key)
            if existing is None:
                by_deal_id[key] = sig
            else:
                # Merge: union the signal_types and prefer the higher
                # severity hint between the two probes that flagged this deal.
                existing.attrs["signal_types"] = sorted(
                    set(
                        (existing.attrs.get("signal_types") or [])
                        + (sig.attrs.get("signal_types") or [])
                    )
                )
                if _severity_rank(sig.attrs.get("severity_hint")) > _severity_rank(
                    existing.attrs.get("severity_hint")
                ):
                    existing.attrs["severity_hint"] = sig.attrs["severity_hint"]
                # If EITHER probe thinks the record is dead, treat it as dead —
                # and re-apply the cap, because the max() above would otherwise
                # let a second probe's "high" undo the age ceiling.
                if sig.attrs.get("presumed_dead"):
                    existing.attrs["presumed_dead"] = True
                if existing.attrs.get("presumed_dead"):
                    existing.attrs["severity_hint"] = "low"
                # The deal's newest real activity, whichever probe saw it.
                # Keeping the first probe's let the order the probes ran in
                # decide whether a later client contact counted as new
                # evidence against a dismissal.
                existing.timestamp = _newest_timestamp(
                    existing.timestamp, sig.timestamp
                )
                # Take the longer / non-empty stage_age_days etc.
                for k in (
                    "stage",
                    "deal_amount",
                    "owner_email",
                    "stage_age_days",
                    "days_since_engagement",
                    "last_engagement_at",
                    "client_days_since_engagement",
                    "client_last_engagement_at",
                    "client_name",
                    "close_date",
                    "days_to_close",
                    "close_date_passed",
                    "contact_email",
                    "company_domain",
                ):
                    if not existing.attrs.get(k) and sig.attrs.get(k):
                        existing.attrs[k] = sig.attrs[k]
                # Concatenate snippets so the LLM sees both reasons.
                if sig.snippet and sig.snippet not in (existing.snippet or ""):
                    existing.snippet = (
                        (existing.snippet + " | " + sig.snippet)[:300]
                        if existing.snippet
                        else sig.snippet
                    )

        merged = list(by_deal_id.values()) + no_deal_id

        # Stable sort: highest severity first, then most-recently-engaged.
        merged.sort(
            key=lambda s: (
                -_severity_rank(s.attrs.get("severity_hint")),
                -(s.attrs.get("days_since_engagement") or 0),
            )
        )
        return merged[: self.MAX_SIGNALS_OUT]

    def build_prompt(self, signals: list[ChannelSignal]) -> str:
        lines = []
        for s in signals:
            attrs = s.attrs
            client_idle = attrs.get("client_days_since_engagement")
            lines.append(
                "- deal_id={did}; stage={stage}; types={types}; "
                "stage_age_days={stage_age}; idle_days={idle}{client}\n"
                "    presumed_dead: {dead}\n"
                "    company: {company} (contact: {contact})\n"
                "    snippet: {snippet}".format(
                    did=s.entity_id,
                    stage=attrs.get("stage"),
                    types=",".join(attrs.get("signal_types") or []) or "-",
                    stage_age=attrs.get("stage_age_days"),
                    idle=attrs.get("days_since_engagement"),
                    # idle_days is the deal's own; say when the client is not
                    # as quiet as its deal, or "idle 82 days" reaches the digest.
                    client=(
                        f"; client_idle_days={client_idle}"
                        if client_idle is not None
                        else ""
                    ),
                    dead=bool(attrs.get("presumed_dead")),
                    company=attrs.get("company_name") or s.entity_label,
                    contact=attrs.get("contact_email") or "-",
                    snippet=s.snippet[:200],
                )
            )
        block = "\n".join(lines) if lines else "(no deals)"

        return (
            # Extractor only — no judgment vocabulary.
            "You are the HubSpot channel extractor. Read the deal rows "
            "below and return a structured JSON observation. You do NOT "
            "decide which deals matter — the decision-maker downstream "
            "does that.\n\n"
            "## OUTPUT (raw JSON only)\n"
            "{\n"
            '  "summary": "1 factual sentence describing the user\'s open deal landscape today.",\n'
            '  "signal_attrs": {}\n'
            "}\n\n"
            "(``signal_attrs`` may stay empty — HubSpot rows are already "
            "fully structured. The deterministic Python side has populated "
            "every attr; the LLM call only summarizes.)\n\n"
            f"## DEALS\n{block}\n"
        )


def _severity_rank(s: str | None) -> int:
    return {"low": 0, "medium": 1, "high": 2}.get(s or "medium", 1)


def _signal_timestamp(src_sig: Any, payload: dict) -> str | None:
    """When something last happened on the deal, or None when nothing did.

    The probe's ``activity_at`` when it carries one, even a None one.
    ``occurred_at`` falls back to "now" for a deal with no logged activity,
    and priorities memory reads this timestamp as the moment something
    happened: a dismissed card citing that deal beat its own dismissal on
    every run. A signal from a source that predates ``activity_at`` keeps its
    ``occurred_at``.
    """
    if "activity_at" in payload:
        value = payload.get("activity_at")
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value) if value else None
    occurred = getattr(src_sig, "occurred_at", None)
    return occurred.isoformat() if isinstance(occurred, datetime) else None


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _newest_timestamp(current: str | None, other: str | None) -> str | None:
    """The later of two signal timestamps; None only when neither is a time.

    Compared as datetimes, not strings: two probes can carry different UTC
    offsets for the same deal.
    """
    current_at, other_at = _parse_timestamp(current), _parse_timestamp(other)
    if other_at is None:
        return current
    if current_at is None or other_at > current_at:
        return other
    return current

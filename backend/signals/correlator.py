"""UC-17.7 — Cross-source Signal correlator.

The correlator turns a flat list of `Signal`s (from HubSpot, Gmail, Slack,
Calendar, Transcript) into `CorrelationGroup`s, each representing one
underlying entity (a deal, a contact, a company, etc.). The judge (UC-17.6)
then ranks groups instead of individual signals, so cross-source context
travels together.

Grouping is tiered — strongest key wins, weaker keys only get to claim
signals the strongest key didn't already place:

  1. HubSpot `deal_id` — explicit, authoritative. One group per unique deal.
  2. Non-HubSpot signals with `contact_email` matching a deal's contact_email
     fold into that deal's group.
  3. Remaining non-HubSpot signals with `contact_email` group by email.
  4. Slack signals (which lack contact_email) fuzzy-match `channel_name`
     against HubSpot company names; fall back to channel_id grouping.
  5. Calendar/transcript signals with `company_name` hint fuzzy-match to a
     HubSpot company; fall back to event_id / transcript_id grouping.
  6. Anything left → singleton orphan group.

Correlation is per-user — signals from different `user_email` never join.

correlation_id is deterministic when the group has a stable entity reference
(`deal:<id>`, `contact:<email>`, `company:<slug>`, `channel:<id>`,
`transcript:<id>`); for orphans it's `orphan:<signal_id>`. Determinism means
re-running the correlator on the same input emits the same correlation_ids,
which is what downstream `agent_recommendations.correlation_id` wants.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from typing import Literal

from backend.signals.types import Signal

FUZZY_MATCH_THRESHOLD = 0.85

# Slack channel-name prefixes that imply a customer-facing context. Mirrors
# `slack.CUSTOMER_FACING_CHANNEL_PATTERNS` but used in stripping-mode here: we
# peel these off the channel name to recover a company-name hint.
SLACK_COMPANY_PREFIXES: tuple[str, ...] = (
    "deals-",
    "deal-",
    "customers-",
    "customer-",
    "client-",
    "clients-",
    "account-",
    "accounts-",
    "shared-",
    "ext-",
)


PrimaryEntityKind = Literal[
    "deal",
    "contact",
    "company",
    "thread",
    "channel",
    "transcript",
    "event",
    "orphan",
]


@dataclass
class CorrelationGroup:
    """A bundle of Signals that all point at the same underlying entity.

    `primary_entity_kind` + `primary_entity_ref` answer "what is this group
    about?" — the judge uses them for context framing and the UI uses them
    for evidence-badge linking.
    """

    correlation_id: str
    user_email: str
    primary_entity_kind: PrimaryEntityKind
    primary_entity_ref: str
    signals: list[Signal] = field(default_factory=list)

    @property
    def sources(self) -> list[str]:
        return sorted({s.source for s in self.signals})

    @property
    def signal_types(self) -> list[str]:
        return sorted({s.signal_type for s in self.signals})

    @property
    def latest_signal_at(self) -> datetime:
        return max(s.occurred_at for s in self.signals)

    def to_dict(self) -> dict:
        return {
            "correlation_id": self.correlation_id,
            "user_email": self.user_email,
            "primary_entity_kind": self.primary_entity_kind,
            "primary_entity_ref": self.primary_entity_ref,
            "sources": self.sources,
            "signal_types": self.signal_types,
            "latest_signal_at": self.latest_signal_at.isoformat(),
            "signals": [s.to_dict() for s in self.signals],
        }


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _normalize_company(name: str) -> str:
    return (name or "").strip().lower().replace(" ", "-")


def _strip_channel_prefix(channel_name: str) -> str:
    name = (channel_name or "").lower().lstrip("#")
    for pre in SLACK_COMPANY_PREFIXES:
        if name.startswith(pre):
            return name[len(pre) :]
    return name


def _fuzzy_match(needle: str, haystack: list[str]) -> str | None:
    """Return the best match in `haystack` (≥ threshold) or None.

    Uses difflib.SequenceMatcher — stdlib, no extra deps. Comparison is on
    normalized lowercased company-slug form.
    """
    if not needle or not haystack:
        return None
    nd = (needle or "").lower().strip()
    if not nd:
        return None
    best: tuple[float, str] | None = None
    for h in haystack:
        hl = (h or "").lower().strip()
        if not hl:
            continue
        if nd == hl or nd in hl or hl in nd:
            return h
        ratio = SequenceMatcher(None, nd, hl).ratio()
        if ratio >= FUZZY_MATCH_THRESHOLD and (best is None or ratio > best[0]):
            best = (ratio, h)
    return best[1] if best else None


# ─── Correlator ──────────────────────────────────────────────────────────────


class Correlator:
    """Builds `CorrelationGroup`s from a list of `Signal`s.

    Optional `email_to_deal` / `company_to_deal` let an orchestrator inject
    pre-resolved HubSpot ownership (e.g. from a richer live lookup). Without
    them, lookups are derived from the HubSpot signals in the input batch —
    which is the common case today.
    """

    def __init__(
        self,
        email_to_deal: dict[str, str] | None = None,
        company_to_deal: dict[str, str] | None = None,
    ):
        self._extra_email_to_deal = {
            (k or "").lower(): v for k, v in (email_to_deal or {}).items()
        }
        self._extra_company_to_deal = {
            _normalize_company(k): v for k, v in (company_to_deal or {}).items()
        }

    def correlate(self, signals: list[Signal]) -> list[CorrelationGroup]:
        by_user: dict[str, list[Signal]] = {}
        for s in signals:
            by_user.setdefault((s.user_email or "").lower(), []).append(s)
        out: list[CorrelationGroup] = []
        for user, user_signals in by_user.items():
            out.extend(self._correlate_user(user, user_signals))
        return out

    def _correlate_user(
        self,
        user_email: str,
        signals: list[Signal],
    ) -> list[CorrelationGroup]:
        # Stable iteration order — caller-provided ordering wins for ties.
        signals = list(signals)

        # Tier 1 — group HubSpot signals by deal_id and seed lookup tables.
        deal_groups: dict[str, CorrelationGroup] = {}
        email_to_deal: dict[str, str] = dict(self._extra_email_to_deal)
        company_to_deal: dict[str, str] = dict(self._extra_company_to_deal)
        placed: set[str] = set()

        for sig in signals:
            if sig.source != "hubspot":
                continue
            deal_id = sig.entity_refs.get("deal_id")
            if not deal_id:
                continue
            deal_id = str(deal_id)
            grp = deal_groups.get(deal_id)
            if grp is None:
                grp = CorrelationGroup(
                    correlation_id=f"deal:{deal_id}",
                    user_email=user_email,
                    primary_entity_kind="deal",
                    primary_entity_ref=deal_id,
                )
                deal_groups[deal_id] = grp
            grp.signals.append(sig)
            placed.add(sig.signal_id)
            contact = (sig.entity_refs.get("contact_email") or "").lower()
            if contact:
                email_to_deal.setdefault(contact, deal_id)
            company = sig.entity_refs.get("company_name")
            if company:
                company_to_deal.setdefault(_normalize_company(company), deal_id)

        # Tier 2 — pull non-HubSpot signals into existing deal groups via
        # contact_email. Tracks deal-keyed company hints from other sources too,
        # so calendar/transcript signals can later seed channel matching.
        for sig in signals:
            if sig.signal_id in placed or sig.source == "hubspot":
                continue
            contact = (sig.entity_refs.get("contact_email") or "").lower()
            if not contact:
                continue
            deal_id = email_to_deal.get(contact)
            if deal_id and deal_id in deal_groups:
                deal_groups[deal_id].signals.append(sig)
                placed.add(sig.signal_id)

        # Tier 5a — calendar/transcript with company_name hint → fuzzy match
        # to a known HubSpot company. Run BEFORE Slack tier so a deal-confirmed
        # company seed populated here can help Slack matching. We deliberately
        # do NOT join transcript signals to calendar signals on `event_id` —
        # transcript.event_id is a Fathom meeting_id, calendar.event_id is a
        # Google Calendar event_id; the keyspaces don't overlap.
        hubspot_company_keys = list(company_to_deal.keys())
        for sig in signals:
            if sig.signal_id in placed:
                continue
            if sig.source not in ("calendar", "transcript"):
                continue
            company_hint = sig.entity_refs.get("company_name")
            if not company_hint:
                continue
            match = _fuzzy_match(
                _normalize_company(company_hint),
                hubspot_company_keys,
            )
            if match:
                deal_id = company_to_deal.get(match)
                if deal_id and deal_id in deal_groups:
                    deal_groups[deal_id].signals.append(sig)
                    placed.add(sig.signal_id)

        # Tier 4 — Slack channel_name fuzzy match. Strip customer-facing
        # prefixes ("deals-acme" → "acme") and fuzzy-match the residual.
        for sig in signals:
            if sig.signal_id in placed or sig.source != "slack":
                continue
            channel_name = (sig.payload or {}).get("channel_name") or ""
            stripped = _strip_channel_prefix(channel_name)
            if not stripped:
                continue
            match = _fuzzy_match(
                _normalize_company(stripped),
                hubspot_company_keys,
            )
            if match:
                deal_id = company_to_deal.get(match)
                if deal_id and deal_id in deal_groups:
                    deal_groups[deal_id].signals.append(sig)
                    placed.add(sig.signal_id)

        # Tier 3 — remaining non-HubSpot signals with contact_email group by email.
        contact_groups: dict[str, CorrelationGroup] = {}
        for sig in signals:
            if sig.signal_id in placed:
                continue
            contact = (sig.entity_refs.get("contact_email") or "").lower()
            if not contact:
                continue
            grp = contact_groups.get(contact)
            if grp is None:
                grp = CorrelationGroup(
                    correlation_id=f"contact:{contact}",
                    user_email=user_email,
                    primary_entity_kind="contact",
                    primary_entity_ref=contact,
                )
                contact_groups[contact] = grp
            grp.signals.append(sig)
            placed.add(sig.signal_id)

        # Tier 4b — Slack signals that didn't match a deal: group by channel_id.
        channel_groups: dict[str, CorrelationGroup] = {}
        for sig in signals:
            if sig.signal_id in placed or sig.source != "slack":
                continue
            channel_id = sig.entity_refs.get("channel_id")
            if not channel_id:
                continue
            grp = channel_groups.get(channel_id)
            if grp is None:
                grp = CorrelationGroup(
                    correlation_id=f"channel:{channel_id}",
                    user_email=user_email,
                    primary_entity_kind="channel",
                    primary_entity_ref=channel_id,
                )
                channel_groups[channel_id] = grp
            grp.signals.append(sig)
            placed.add(sig.signal_id)

        # Tier 5b — transcripts that still aren't placed group by transcript_id.
        transcript_groups: dict[str, CorrelationGroup] = {}
        for sig in signals:
            if sig.signal_id in placed or sig.source != "transcript":
                continue
            tid = sig.entity_refs.get("transcript_id")
            if not tid:
                continue
            grp = transcript_groups.get(tid)
            if grp is None:
                grp = CorrelationGroup(
                    correlation_id=f"transcript:{tid}",
                    user_email=user_email,
                    primary_entity_kind="transcript",
                    primary_entity_ref=tid,
                )
                transcript_groups[tid] = grp
            grp.signals.append(sig)
            placed.add(sig.signal_id)

        # Tier 5c — calendar leftovers group by event_id: the fallback for a
        # meeting signal that carried no contact_email/company to join on in
        # the earlier tiers.
        event_groups: dict[str, CorrelationGroup] = {}
        for sig in signals:
            if sig.signal_id in placed or sig.source != "calendar":
                continue
            eid = sig.entity_refs.get("event_id")
            if not eid:
                continue
            grp = event_groups.get(eid)
            if grp is None:
                grp = CorrelationGroup(
                    correlation_id=f"event:{eid}",
                    user_email=user_email,
                    primary_entity_kind="event",
                    primary_entity_ref=eid,
                )
                event_groups[eid] = grp
            grp.signals.append(sig)
            placed.add(sig.signal_id)

        # Tier 6 — anything still unplaced becomes a singleton orphan.
        orphan_groups: list[CorrelationGroup] = []
        for sig in signals:
            if sig.signal_id in placed:
                continue
            # Gmail awaiting_reply with no contact_email: use thread_id when
            # available so re-runs are stable; otherwise fall back to signal_id.
            thread_id = sig.entity_refs.get("thread_id")
            if sig.source == "gmail" and thread_id:
                orphan_groups.append(
                    CorrelationGroup(
                        correlation_id=f"thread:{thread_id}",
                        user_email=user_email,
                        primary_entity_kind="thread",
                        primary_entity_ref=thread_id,
                        signals=[sig],
                    )
                )
            else:
                orphan_groups.append(
                    CorrelationGroup(
                        correlation_id=f"orphan:{sig.signal_id}",
                        user_email=user_email,
                        primary_entity_kind="orphan",
                        primary_entity_ref=sig.signal_id,
                        signals=[sig],
                    )
                )
            placed.add(sig.signal_id)

        return [
            *deal_groups.values(),
            *contact_groups.values(),
            *channel_groups.values(),
            *transcript_groups.values(),
            *event_groups.values(),
            *orphan_groups,
        ]


def correlate(signals: list[Signal]) -> list[CorrelationGroup]:
    """Convenience wrapper around `Correlator().correlate()`."""
    return Correlator().correlate(signals)

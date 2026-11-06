"""Inherited channel — open work a departed teammate left to this user.

Reads ``offboard_items`` (via :mod:`rubik_core.offboarding.store`) for records
where this run's user is the successor. Fully deterministic: the items were
already inventoried/extracted by the offboard sweep, so there is nothing for a
channel LLM to add — ``run()`` is overridden to skip the extractor call
entirely, making the channel free for the ~everyone who isn't currently a
successor.

Signal contract:

- ``entity_kind = OFFBOARD_ITEM``, ``entity_id = offboard_items.id`` — so the
  dismiss/resolve memory keys on the ITEM, never on the underlying
  thread/deal (see the EntityKind.OFFBOARD_ITEM note on read-filter
  poisoning). The home router's dismiss/resolve endpoints write the same ids
  back to ``offboard_items.status``.
- ``attrs`` carries the departed person's name, the original entity reference
  and the source links, so the decision-maker can attribute the card
  ("[Inherited from John Doe]") and correlate it with the forwarded thread /
  transferred doc now visible in the successor's own channels.
"""

from __future__ import annotations

import logging

from rubik_core.priorities.agents.base import BaseChannelAgent
from rubik_core.priorities.models import (
    ChannelDigest,
    ChannelKind,
    ChannelSignal,
    EntityKind,
)

logger = logging.getLogger(__name__)


class InheritedChannelAgent(BaseChannelAgent):
    channel = ChannelKind.INHERITED

    def fetch_signals(self) -> list:
        from rubik_core.database.db import SessionLocal
        from rubik_core.offboarding import store

        with SessionLocal() as db:
            pairs = store.open_items_for_successor(db, self.user_email)
            # Detach plain dicts — the session closes before parse_signals.
            return [
                {
                    "item_id": item.id,
                    "kind": item.kind,
                    "label": item.label,
                    "detail": item.detail,
                    "due": item.due,
                    "urgency": item.urgency,
                    "orig_entity_kind": item.entity_kind,
                    "orig_entity_id": item.entity_id,
                    "source_links": item.source_links or [],
                    "departed_name": record.user_name,
                    "departed_email": record.user_email,
                    "offboarded_at": record.offboarded_at.date().isoformat(),
                }
                for item, record in pairs
            ]

    def parse_signals(self, raw_items: list) -> list[ChannelSignal]:
        signals: list[ChannelSignal] = []
        for raw in raw_items:
            due = f" (due {raw['due']})" if raw.get("due") else ""
            signals.append(
                ChannelSignal(
                    id=f"offboard_item:{raw['item_id']}",
                    channel=self.channel,
                    title=f"[Inherited from {raw['departed_name']}] {raw['label']}",
                    snippet=(raw.get("detail") or "")[:400] + due,
                    link=(raw.get("source_links") or [None])[0],
                    timestamp=None,
                    entity_kind=EntityKind.OFFBOARD_ITEM,
                    entity_id=str(raw["item_id"]),
                    entity_label=raw["label"],
                    attrs={
                        "inherited": True,
                        "departed_name": raw["departed_name"],
                        "departed_email": raw["departed_email"],
                        "offboarded_at": raw["offboarded_at"],
                        "item_kind": raw["kind"],
                        "due": raw.get("due"),
                        "urgency_hint": raw.get("urgency"),
                        "orig_entity_kind": raw.get("orig_entity_kind"),
                        "orig_entity_id": raw.get("orig_entity_id"),
                        "source_links": raw.get("source_links") or [],
                    },
                )
            )
        return signals

    def build_prompt(self, signals: list) -> str:  # pragma: no cover — run() skips it
        raise NotImplementedError("inherited channel is deterministic")

    def run(self) -> ChannelDigest:
        """Deterministic digest — no LLM call, ever.

        Items are already structured; an extractor pass would only add cost
        and a failure mode. Mirrors ``base.run``'s resilience contract: any
        fetch failure returns an errored digest, never raises.
        """
        try:
            raw = self.fetch_signals()
            signals = self.parse_signals(raw)
        except Exception as exc:  # noqa: BLE001 — pipeline must stay alive
            logger.warning("inherited channel failed for %s: %s", self.user_email, exc)
            return ChannelDigest(channel=self.channel, error=f"fetch_failed: {exc}")
        if not signals:
            return ChannelDigest(channel=self.channel, summary="")
        names = sorted({s.attrs.get("departed_name", "?") for s in signals})
        summary = (
            f"{len(signals)} open item(s) inherited from departed "
            f"teammate(s): {', '.join(names)}."
        )
        return ChannelDigest(channel=self.channel, summary=summary, signals=signals)

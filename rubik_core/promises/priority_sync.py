"""Keep the AI Priorities card for a promise in step with the row.

Mirrors ``backend/routers/eos/_priority_sync.py``: the memory key is
``(user, EntityKind.PROMISE, promises.id, <kind>)`` for BOTH kinds a promise
card can carry — ``PROMISE_DUE`` (a To-Do coming due) and ``PROMISE_UNKEPT``
(a heard promise with no sign it was kept) — and those values MUST match what
``PromisesChannelAgent`` stamps, or the card never clears. Best-effort — a
memory failure never blocks the ledger write.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def sync_promise_priority(
    *, promise_id: str, closed: bool, actor: str, via: str
) -> None:
    """``closed=True`` when the promise left the open states (kept / dropped /
    handed off); ``False`` when it came back (reopened / kept denied)."""
    try:
        from rubik_core.priorities import memory as priority_memory
        from rubik_core.priorities.models import EntityKind, PriorityKind

        for kind in (PriorityKind.PROMISE_DUE, PriorityKind.PROMISE_UNKEPT):
            if closed:
                priority_memory.resolve_entity_everywhere(
                    entity_kind=EntityKind.PROMISE,
                    entity_id=str(promise_id),
                    priority_kind=kind,
                    evidence={"kind": f"promise_{via}"},
                    actor=actor,
                )
            else:
                priority_memory.reopen_entity_everywhere(
                    entity_kind=EntityKind.PROMISE,
                    entity_id=str(promise_id),
                    priority_kind=kind,
                    actor=actor,
                )
    except Exception:  # noqa: BLE001 — memory failures never block the mutation
        logger.warning(
            "promise priority sync failed for %s (closed=%s)",
            promise_id,
            closed,
            exc_info=True,
        )

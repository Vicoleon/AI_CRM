"""Auto-clear AI-Priorities cards when the underlying EOS entity is completed.

Mirrors the meeting-prep auto-resolve (``_resolve_prep_debt`` in
``backend/routers/home.py``): when a To-Do is marked done, an Issue solved, a
Rock completed, or a Milestone completed, the matching AI-Priorities card is
resolved for EVERY user who was shown it (the owner AND any manager/exec whose
team-scoped briefing surfaced it — see ``priority_memory.resolve_entity_everywhere``).
Reversing the completion reopens it.

All hooks are best-effort: a priority-memory hiccup logs a warning and never
blocks the EOS mutation. The priority_kind values here MUST match what the
``EosChannelAgent`` stamps and the orchestrator re-attaches, or the key won't
line up and the card won't clear.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _resolve(entity_kind, entity_id, priority_kind, actor: str, evidence: dict) -> None:
    try:
        from rubik_core.priorities import memory as priority_memory

        priority_memory.resolve_entity_everywhere(
            entity_kind=entity_kind,
            entity_id=str(entity_id),
            priority_kind=priority_kind,
            evidence=evidence,
            actor=actor,
        )
    except Exception:  # noqa: BLE001 — memory failures never block the mutation
        logger.warning(
            "EOS priority auto-resolve failed for %s:%s",
            entity_kind,
            entity_id,
            exc_info=True,
        )


def _reopen(entity_kind, entity_id, priority_kind, actor: str) -> None:
    try:
        from rubik_core.priorities import memory as priority_memory

        priority_memory.reopen_entity_everywhere(
            entity_kind=entity_kind,
            entity_id=str(entity_id),
            priority_kind=priority_kind,
            actor=actor,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "EOS priority auto-reopen failed for %s:%s",
            entity_kind,
            entity_id,
            exc_info=True,
        )


def sync_todo_priority(*, todo_id, was_done: bool, is_done: bool, actor: str) -> None:
    """Completing a to-do clears its card; re-opening brings it back."""
    from rubik_core.priorities.models import EntityKind, PriorityKind

    if is_done and not was_done:
        _resolve(
            EntityKind.TODO,
            todo_id,
            PriorityKind.EOS_TODO_DUE,
            actor,
            {"kind": "eos_todo_done"},
        )
    elif was_done and not is_done:
        _reopen(EntityKind.TODO, todo_id, PriorityKind.EOS_TODO_DUE, actor)


def sync_issue_priority(
    *, issue_id, was_solved: bool, is_solved: bool, actor: str
) -> None:
    """Solving an IDS issue clears its card; re-opening brings it back."""
    from rubik_core.priorities.models import EntityKind, PriorityKind

    if is_solved and not was_solved:
        _resolve(
            EntityKind.ISSUE,
            issue_id,
            PriorityKind.EOS_ISSUE_OPEN,
            actor,
            {"kind": "eos_issue_solved"},
        )
    elif was_solved and not is_solved:
        _reopen(EntityKind.ISSUE, issue_id, PriorityKind.EOS_ISSUE_OPEN, actor)


def sync_rock_priority(*, rock_id, status, actor: str) -> None:
    """A COMPLETED (done) Rock clears its attention card.

    Only ``DONE`` is terminal, so only it durably resolves. off_track↔on_track
    and staleness are RECURRING conditions — durably resolving them would
    suppress a legitimate future resurface (the Rock going stale/off-track
    again), so we leave those to the next generation, which re-evaluates the
    live state. Re-opening a done Rock revives any card we resolved.
    """
    from rubik_core.database.eos._base import RockStatus
    from rubik_core.priorities.models import EntityKind, PriorityKind

    if status == RockStatus.DONE:
        _resolve(
            EntityKind.ROCK,
            rock_id,
            PriorityKind.EOS_ROCK_ATTENTION,
            actor,
            {"kind": "eos_rock_done"},
        )
    else:
        _reopen(EntityKind.ROCK, rock_id, PriorityKind.EOS_ROCK_ATTENTION, actor)


def sync_milestone_priority(*, milestone_id, completed: bool, actor: str) -> None:
    """Completing a milestone clears its card; un-completing brings it back."""
    from rubik_core.priorities.models import EntityKind, PriorityKind

    if completed:
        _resolve(
            EntityKind.MILESTONE,
            milestone_id,
            PriorityKind.EOS_MILESTONE_DUE,
            actor,
            {"kind": "eos_milestone_completed"},
        )
    else:
        _reopen(
            EntityKind.MILESTONE, milestone_id, PriorityKind.EOS_MILESTONE_DUE, actor
        )

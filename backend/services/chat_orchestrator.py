"""Build the chat orchestrator — the one place that assembles a chat turn.

Extracted from ``backend/routers/chat.py`` so that a background job can run
the same agent, with the same context blocks and the same feature gates, as a
live chat request. A Cloud Run Job importing a FastAPI router to get at a
private helper is how the two paths drift apart.

Two callers today:

* the chat router, for a live turn (``headless=None``);
* ``rubik_core/scheduled_tasks/runner.py``, replaying a stored prompt with
  nobody present (``headless=HeadlessRun(...)``).

Every context block is best-effort: alignment, WGLL and personalization each
degrade to "" rather than failing the turn, which is the behaviour the chat
router already relied on.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from rubik_core.agent_runtime.coordinator import HeadlessRun
from rubik_core.auth.registry import UserProfile

logger = logging.getLogger(__name__)


def build_chat_orchestrator(
    user: UserProfile,
    db: Session,
    timezone: str = "UTC",
    *,
    source_session_id: str | None = None,
    headless: HeadlessRun | None = None,
):
    """Return an :class:`ADKOrchestrator` for ``user``.

    ``timezone`` is the IANA zone the turn is dated in — the browser's for a
    live request, the task's stored zone for a scheduled run.

    ``source_session_id`` is recorded on any task created during this turn, so
    a schedule can be traced back to the conversation that asked for it.

    ``headless`` marks an unattended run: it strips the confirmation-gated
    tools and the scheduling tools, drops the sub-agents, and tells the model
    to finish the job without asking anything. See
    :class:`rubik_core.agent_runtime.coordinator.HeadlessRun`.
    """
    from backend.services.user_context import build_chat_personalization_context
    from rubik_core.agents.alignment_context import build_alignment_context
    from rubik_core.feedback.wgll import build_wgll_context
    from rubik_core.orchestration.adk_orchestrator import ADKOrchestrator

    alignment = build_alignment_context(user.email, db)
    # WGLL loop — fold recent user ratings on chat answers into the coordinator
    # prompt. Safe no-op ("") when disabled or no ratings exist.
    wgll = build_wgll_context(db, surface="chat", user_email=user.email)
    # Per-user personalization (AI Personalization settings): role/focus +
    # answer verbosity + reply language. Safe no-op ("") when nothing is set.
    try:
        user_context = build_chat_personalization_context(user, db)
    except Exception:  # noqa: BLE001 — personalization must never break a turn
        user_context = ""

    return ADKOrchestrator(
        user=user,
        timezone=timezone,
        alignment_context=alignment,
        wgll_context=wgll,
        user_context=user_context,
        scheduled_tasks_tools=_scheduled_tasks_tools(
            user, db, timezone, source_session_id=source_session_id
        )
        if headless is None
        else None,
        todo_tools=_todo_tools(user, db, timezone) if headless is None else None,
        # Kept on a headless run: running a saved skill is read-only (plus a
        # Gmail draft at most), and "every Monday run /weekly-digest" is how a
        # routine on a clock works. The coordinator drops the armed save and
        # delete tools on a headless run.
        meeting_skills_tools=_meeting_skills_tools(
            user, db, source_session_id=source_session_id
        ),
        headless=headless,
    )


def _meeting_skills_tools(
    user: UserProfile, db: Session, *, source_session_id: str | None = None
):
    """The Meeting Skills tool container, or ``None`` when this user lacks the
    feature. Same fail-closed shape as ``_scheduled_tasks_tools``."""
    from rubik_core.config import settings

    if not settings.MEETING_SKILLS_ENABLED:
        return None
    try:
        from rubik_core.features import feature_allows

        if not feature_allows("meeting_skills", user, db):
            return None
        from rubik_core.tools.meeting_skills_tools import MeetingSkillChatTools

        return MeetingSkillChatTools(
            user_email=user.email, source_session_id=source_session_id
        )
    except Exception:  # noqa: BLE001 — no meeting skills beats a broken turn
        logger.exception(
            "meeting_skills: gate resolution failed for %s — tools withheld",
            getattr(user, "email", "?"),
        )
        return None


def _todo_tools(user: UserProfile, db: Session, timezone: str):
    """The My To-Dos tool container, or ``None`` when this user lacks the
    feature. Same fail-closed shape as ``_scheduled_tasks_tools``."""
    from rubik_core.config import settings

    if not settings.TODOS_ENABLED:
        return None
    try:
        from rubik_core.features import feature_allows

        if not feature_allows("todos", user, db):
            return None
        from rubik_core.tools.todo_tools import TodoChatTools

        return TodoChatTools(user_email=user.email, user_timezone=timezone)
    except Exception:  # noqa: BLE001 — no to-do tools beats a broken turn
        logger.exception(
            "todos: gate resolution failed for %s — tools withheld",
            getattr(user, "email", "?"),
        )
        return None


def _scheduled_tasks_tools(
    user: UserProfile,
    db: Session,
    timezone: str,
    *,
    source_session_id: str | None,
):
    """The scheduling tool container, or ``None`` when this user can't schedule.

    Gated here rather than inside the container so the coordinator never even
    describes the capability to someone who lacks it — being told Cubie can
    schedule things and then refused is worse than not being offered.

    Fails CLOSED: a gate that cannot be resolved means no tools this turn,
    which costs the user a feature for one turn rather than granting one.
    """
    from rubik_core.config import settings

    if not settings.SCHEDULED_TASKS_ENABLED:
        return None
    try:
        from rubik_core.features import feature_allows

        if not feature_allows("scheduled_tasks", user, db):
            return None
        from rubik_core.tools.scheduled_tasks_tools import ScheduledTaskChatTools

        return ScheduledTaskChatTools(
            user_email=user.email,
            user_timezone=timezone,
            source_session_id=source_session_id,
        )
    except Exception:  # noqa: BLE001 — no scheduling beats a broken turn
        logger.exception(
            "scheduled_tasks: gate resolution failed for %s — tools withheld",
            getattr(user, "email", "?"),
        )
        return None

"""Build a text-free feedback signal for model prompts.

Old rated answers and comments can repeat CRM facts after deal access changes.
Until feedback rows have revocable source lineage, only recent rating counts
may enter a new prompt. The user's written feedback remains available to the
feedback and Insights surfaces through their normal access controls.
"""

from __future__ import annotations

import logging

from sqlalchemy import desc
from sqlalchemy.orm import Session

from rubik_core.config import settings
from rubik_core.database.models import AssistantFeedback

logger = logging.getLogger(__name__)

_MAX_PER_SIDE = 4


def build_wgll_context(
    db: Session,
    *,
    surface: str,
    user_email: str,
    max_per_side: int = _MAX_PER_SIDE,
) -> str:
    """Return bounded per-user rating counts without old answer/comment text."""
    if not settings.CUBIE_WGLL_FEEDBACK_ENABLED:
        return ""

    if not user_email:
        # No user to scope to → refuse rather than fall back to an org-wide pull.
        return ""

    try:
        useful = (
            db.query(AssistantFeedback.id)
            .filter(
                AssistantFeedback.user_email == user_email,
                AssistantFeedback.surface == surface,
                AssistantFeedback.rating == "useful",
            )
            .order_by(desc(AssistantFeedback.updated_at))
            .limit(max_per_side)
            .all()
        )
        not_useful = (
            db.query(AssistantFeedback.id)
            .filter(
                AssistantFeedback.user_email == user_email,
                AssistantFeedback.surface == surface,
                AssistantFeedback.rating == "not_useful",
            )
            .order_by(desc(AssistantFeedback.updated_at))
            .limit(max_per_side)
            .all()
        )
    except Exception:
        logger.exception("WGLL context build failed for surface=%s", surface)
        return ""

    if not useful and not not_useful:
        return ""

    noun = {
        "priorities": "priorities",
        "chat": "chat answers",
        "meeting_prep": "meeting briefings",
        "xray": "customer reviews",
    }.get(surface, "assistant answers")
    return (
        "\n## USER FEEDBACK\n"
        f"For this user's recent {noun}, {len(useful)} were rated "
        f"useful and {len(not_useful)} were rated not useful. Keep the answer "
        "grounded and actionable. No old answer or comment text is available.\n"
    )

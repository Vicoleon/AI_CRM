"""
Nightly Intelligence Pipeline — Custom Tools.

Tools that the nightly pipeline stages can invoke to persist artifacts.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def save_home_card(
    user_email: str,
    category: str,
    content_md: str,
    priority: int = 5,
    pipeline_stage: str = "validator",
) -> str:
    """Save or update a HomeCard in the database for the user's dashboard.

    This tool persists an intelligence card that will appear on the user's
    home feed. If a card already exists for the same user + date + category,
    it will be updated (upsert behaviour) to keep the pipeline idempotent.

    Args:
        user_email: The email of the target user.
        category: Card type — one of: daily_briefing, alert, insight, crm_change.
        content_md: Markdown-formatted content for the card.
        priority: Priority score from 0 (low) to 10 (critical). Default 5.
        pipeline_stage: Which pipeline stage produced this card.

    Returns:
        A confirmation message with the card ID.
    """
    from rubik_core.database.db import SessionLocal
    from rubik_core.database.models import HomeCard

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        with SessionLocal() as db:
            # Check for existing card (upsert logic)
            existing = (
                db.query(HomeCard)
                .filter(
                    HomeCard.user_email == user_email,
                    HomeCard.date == today_str,
                    HomeCard.category == category,
                )
                .first()
            )

            if existing:
                existing.content_md = content_md
                existing.priority = priority
                existing.pipeline_stage = pipeline_stage
                existing.created_at = datetime.utcnow()
                card_id = existing.id
                db.commit()
                logger.info(
                    "Updated HomeCard %s for %s (category=%s, priority=%d)",
                    card_id,
                    user_email,
                    category,
                    priority,
                )
                return f"Updated existing card {card_id} for {user_email} ({category})"
            else:
                card_id = str(uuid.uuid4())
                card = HomeCard(
                    id=card_id,
                    user_email=user_email,
                    date=today_str,
                    category=category,
                    pipeline_stage=pipeline_stage,
                    content_md=content_md,
                    priority=priority,
                )
                db.add(card)
                db.commit()
                logger.info(
                    "Created HomeCard %s for %s (category=%s, priority=%d)",
                    card_id,
                    user_email,
                    category,
                    priority,
                )
                return f"Created new card {card_id} for {user_email} ({category})"
    except Exception as e:
        logger.error("save_home_card failed: %s", e)
        return f"Error saving card: {str(e)}"

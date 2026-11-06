"""HomeCard persistence for the UC-20 multi-agent priorities pipeline.

Lives alongside (not replacing) main's ``backend/agents/overnight.py``
orchestrator. Main's pipeline writes ``AgentRecommendation`` rows via
its plugin system; this module writes ``HomeCard`` rows with the
``daily_briefing`` category and the structured priorities bundle in
``metadata_json`` so the AI Priorities endpoint can read them back
without re-running the BookWriter-style pipeline.

The schema lookup (`(user_email, date, category)` ordered by
``created_at DESC``) matches the legacy daily-briefing read path.
"""

from __future__ import annotations

import logging
import uuid

from rubik_core.database.db import SessionLocal
from rubik_core.database.models import HomeCard
from rubik_core.priorities.cached_visibility import verified_bundle_deals
from rubik_core.priorities.models import PrioritiesBundle
from rubik_core.priorities.orchestrator import bundle_to_metadata

logger = logging.getLogger(__name__)

DAILY_BRIEFING_CATEGORY = "daily_briefing"
# The Friday look-ahead run persists under its own category so the read path
# can style/merge it separately and the daily-briefing cache never sees it.
NEXT_WEEK_CATEGORY = "next_week_report"


def persist_bundle(
    user_email: str,
    date_str: str,
    bundle: PrioritiesBundle,
    *,
    category: str = DAILY_BRIEFING_CATEGORY,
    extra_metadata: dict | None = None,
) -> str:
    """Write a HomeCard row for the given bundle and return its id.

    ``category`` defaults to the daily briefing; the next-week run passes
    ``NEXT_WEEK_CATEGORY``. ``extra_metadata`` (e.g. ``report_kind`` +
    ``week_start``/``week_end``) is merged into the bundle metadata so the
    read path can own visibility without a schema change.
    """
    is_next_week = category == NEXT_WEEK_CATEGORY
    metadata = bundle_to_metadata(bundle)
    if extra_metadata:
        metadata.update(extra_metadata)
    if verified_bundle_deals(metadata) is None:
        raise ValueError("priority bundle lacks current workspace CRM provenance")
    content_md = bundle_to_markdown(bundle, date_str, next_week=is_next_week)
    card_id = str(uuid.uuid4())

    with SessionLocal() as db:
        card = HomeCard(
            id=card_id,
            user_email=user_email,
            date=date_str,
            category=category,
            content_md=content_md,
            metadata_json=metadata,
            priority=100,
        )
        db.add(card)
        db.commit()
    return card_id


def bundle_to_markdown(
    bundle: PrioritiesBundle, date_str: str, *, next_week: bool = False
) -> str:
    """Render a bundle as the markdown body of a HomeCard."""
    lines: list[str] = []
    if next_week:
        lines.append("### AI Priorities — Next week look-ahead")
    else:
        lines.append(f"### AI Priorities — {date_str}")
    lines.append("")
    if bundle.headline:
        lines.append(f"_{bundle.headline}_")
        lines.append("")

    if not bundle.priorities:
        lines.append("No high-priority items surfaced today across your channels.")
        return "\n".join(lines)

    for i, p in enumerate(bundle.priorities, 1):
        urgency_tag = {"high": "[HIGH]", "medium": "[MED]", "low": "[LOW]"}.get(
            p.urgency.lower(), "[MED]"
        )
        channels = ", ".join(c.value for c in p.source_channels) or "—"
        lines.append(f"**{i}. {urgency_tag} {p.title}**")
        if p.why_it_matters:
            lines.append(f"   - {p.why_it_matters}")
        if p.suggested_action:
            lines.append(f"   - _Next step:_ {p.suggested_action}")
        lines.append(f"   - _Sources:_ {channels}")
        for link in p.source_links[:3]:
            lines.append(f"     - {link}")
        lines.append("")
    return "\n".join(lines)

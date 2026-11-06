"""Email action item extraction — LLM-filtered priority items from unread Gmail.

Surfaces only the unread emails that genuinely need the user's personal attention
today, with a one-line summary per item. Used by the daily briefing's email card.
"""

from __future__ import annotations

import json as _json
import logging

from pydantic import BaseModel

from backend.services.gmail_service import ParsedMessage
from rubik_core.cost.usage import attribute_to

logger = logging.getLogger(__name__)


class ActionItem(BaseModel):
    text: str
    thread_id: str
    summary: str = ""


_PROMPT_PREFIX = (
    "You are a senior executive assistant filtering emails for a busy professional.\n\n"
    "## YOUR TASK\n"
    "Review the emails below and extract ONLY the action items that genuinely require the "
    "user's personal attention today. For each one, also write a single short sentence that "
    "summarizes the email so the user can decide whether to open it.\n\n"
    "## STRICT FILTERING RULES\n"
    "IGNORE and do NOT create action items for:\n"
    "- Auto-generated meeting notes or summaries (e.g. 'Notes from X meeting', Gemini summaries)\n"
    "- FYI all-team announcements or broadcast emails (e.g. 'please find attached minutes')\n"
    "- Routine calendar invitations unless they require an explicit RSVP decision by today\n"
    "- Marketing, newsletters, or product updates\n"
    "- Automated notifications that don't need a response (e.g. standard CI pass)\n"
    "- Informational copies where the user is only Cc'd and not asked to do anything\n\n"
    "ONLY extract action items for:\n"
    "- Direct requests from a real person asking the user to do something specific\n"
    "- System failures or errors requiring investigation (broken builds, production issues)\n"
    "- Decisions or approvals only the user can make\n"
    "- Time-sensitive items with a clear deadline\n\n"
    "## OUTPUT FORMAT\n"
    "Return ONLY a raw JSON list of objects. Each object MUST have 'text', 'thread_id', and 'summary' keys.\n"
    "- 'text': the action the user must take (imperative, <= 80 chars)\n"
    "- 'summary': one sentence summarizing the email content (<= 140 chars)\n"
    "- 'thread_id': the exact Thread ID from the input\n"
    'Example: [{"text": "Confirm Q3 budget", "thread_id": "123abc", '
    '"summary": "Sarah needs your sign-off on the $50k Q3 marketing spend by Friday."}]\n'
    "If there are no genuine action items, return exactly: []\n\n"
)


@attribute_to("email")
async def extract_action_items(messages: list[ParsedMessage]) -> list[ActionItem]:
    """Filter unread emails to high-priority action items via Gemini 2.5 Pro.

    Returns an empty list when there is nothing actionable, or on any LLM error
    (logged) — the caller should treat extraction as best-effort.
    """
    from rubik_core.config import settings
    from rubik_core.models import get_genai_client

    if not messages:
        return []

    email_text_parts: list[str] = []
    for m in messages[:8]:  # cap at 8 to stay within token budget
        snippet = (m.snippet or "")[:300]
        addressed = "directly (To)" if m.addressing == "direct" else "Cc'd"
        email_text_parts.append(
            f"Thread ID: {m.thread_id}\nAddressed to user: {addressed}\n"
            f"From: {m.sender}\nSubject: {m.subject}\nSnippet: {snippet}"
        )

    if not email_text_parts:
        return []

    prompt = _PROMPT_PREFIX + "\n\n---\n\n".join(email_text_parts)

    client = get_genai_client()
    try:
        response = await client.aio.models.generate_content(
            model=settings.VERTEX_LLM_MODEL_PRO,
            contents=prompt,
        )
        raw = (response.text or "").strip()
        raw = (
            raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        )
        if not raw or raw.upper() == "NONE" or raw == "[]":
            return []
        data = _json.loads(raw)
        if not isinstance(data, list):
            return []
        items: list[ActionItem] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            text = entry.get("text")
            thread_id = entry.get("thread_id")
            if not text or not thread_id:
                continue
            items.append(
                ActionItem(
                    text=str(text),
                    thread_id=str(thread_id),
                    summary=str(entry.get("summary") or ""),
                )
            )
        return items
    except Exception:
        logger.error("Action item extraction failed")
        return []

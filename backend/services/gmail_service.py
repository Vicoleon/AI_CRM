"""GmailService — thin domain wrapper around `rubik_core.tools.gmail.GmailClient`.

Scopes summaries to the requesting user's work mailbox by default. An
explicit extra account is resolved by owner and workspace before any Google
request. NEVER exposes another user's mail, even to admins.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel

from rubik_core.cost.usage import attribute_to
from rubik_core.tools.addressing import Addressing as _Addressing
from rubik_core.tools.addressing import (
    classify_addressing,
    focus_sort_key,
)
from rubik_core.tools.gmail import GmailClient

logger = logging.getLogger(__name__)

# Verbs that, when paired with the user's name, strongly suggest a request
# directed at them personally.
_REQUEST_VERBS = (
    "can you",
    "could you",
    "would you",
    "please",
    "need you to",
    "needs you to",
    "want you to",
    "we need you",
    "let me know",
    "get back to me",
    "review",
    "approve",
    "sign off",
    "confirm",
)


# ─── Recipient addressing classification ────────────────────────────────────
# Moved to `rubik_core.tools.addressing` so the AI-Priorities Gmail extractor
# can share ONE implementation with this surface (rubik_core must not import
# backend). Re-exported here because callers already import these names from
# this module. See `get_unread(focus=True)` for how "list" mail is dropped.
Addressing = _Addressing


class ParsedMessage(BaseModel):
    account_id: str | None = None
    thread_id: str
    message_id: str
    sender: str
    subject: str
    snippet: str
    date: str
    is_unread: bool
    # How the requesting user was addressed: "direct" (To), "cc", or "list"
    # (distribution-list / bulk). Drives focus filtering + ordering.
    addressing: Addressing = "direct"


class EmailSummary(BaseModel):
    account_id: str | None = None
    thread_id: str
    message_id: str
    sender: str
    subject: str
    summary: str
    action_required: bool
    date: str


def _to_gmail_after(since: datetime) -> str:
    """Gmail's `after:` operator takes YYYY/MM/DD (no time)."""
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    return since.astimezone(timezone.utc).strftime("%Y/%m/%d")


def _parse_thread(
    thread: dict[str, Any], user_email: str = "", account_id: str | None = None
) -> ParsedMessage | None:
    """Parse the most recent message in a thread into a `ParsedMessage`.

    ``user_email`` is needed to classify how the user was addressed (To / Cc /
    distribution-list). The Gmail `metadata` format returns all headers, so
    To/Cc/List-* are available without re-fetching the body.
    """
    messages = thread.get("messages") or []
    if not messages:
        return None
    msg = messages[-1]
    headers = {
        h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])
    }
    label_ids = msg.get("labelIds") or []
    return ParsedMessage(
        account_id=account_id,
        thread_id=thread.get("id", ""),
        message_id=msg.get("id", ""),
        sender=headers.get("from", ""),
        subject=headers.get("subject", "(no subject)"),
        snippet=(msg.get("snippet") or "")[:400],
        date=headers.get("date", ""),
        is_unread="UNREAD" in label_ids,
        addressing=classify_addressing(headers, user_email),
    )


class GmailService:
    """High-level Gmail operations scoped to a single user's mailbox."""

    def __init__(self, user_email: str, *, account_id: str | None = None):
        if not user_email:
            raise ValueError("GmailService requires a user_email")
        self.owner_email = user_email
        self.account_id = account_id
        self._client = (
            GmailClient(user_email=user_email, account_id=account_id)
            if account_id is not None
            else GmailClient(user_email=user_email)
        )
        self.user_email = (
            self._client.user_email if account_id is not None else user_email
        )
        if not self._client.service:
            raise RuntimeError(f"Failed to initialize Gmail client for {user_email}")

    # ─── Read operations ─────────────────────────────────────────────────

    def get_unread(
        self,
        since: datetime | None = None,
        max_results: int = 25,
        focus: bool = True,
    ) -> list[ParsedMessage]:
        """Return parsed metadata for the user's unread **inbox** threads.

        Scoped to `in:inbox` so messages the user's own Gmail filters have
        archived / skipped-inbox / routed to a folder (e.g. a busy distribution
        list they auto-archive) never surface — Cubie mirrors the real inbox.

        When ``focus`` is True (the default for personal-attention views), drops
        distribution-list / bulk mail and orders direct (To:) above Cc:. We
        over-fetch first so dropping list mail doesn't starve the result.

        `since` constrains to messages received on or after the given datetime
        (Gmail's `after:` resolution is one day).
        """
        query = "is:unread in:inbox"
        if since is not None:
            query = f"{query} after:{_to_gmail_after(since)}"
        fetch_n = min(max_results * 2, 30) if focus else max_results
        threads = self._client.search_threads(query=query, max_results=fetch_n)
        messages = [
            m
            for m in (
                _parse_thread(t, self.user_email, self.account_id) for t in threads
            )
            if m is not None
        ]
        if not focus:
            return messages
        kept = [m for m in messages if m.addressing != "list"]
        kept.sort(key=lambda m: focus_sort_key(m.addressing, m.is_unread))
        return kept[:max_results]

    def search(self, query: str, max_results: int = 10) -> list[ParsedMessage]:
        threads = self._client.search_threads(query=query, max_results=max_results)
        return [
            m
            for m in (
                _parse_thread(t, self.user_email, self.account_id) for t in threads
            )
            if m is not None
        ]

    def list_threads_for_signals(
        self,
        since: datetime,
        max_results: int = 50,
    ) -> list[dict[str, Any]]:
        """Raw thread dicts (metadata format) for UC-17 signal extraction.

        Returns Gmail's hydrated thread shape (`{id, messages: [...]}`), not
        the trimmed `ParsedMessage` — signal extraction needs per-message
        headers to detect awaiting-reply and List-Unsubscribe.
        """
        query = f"after:{_to_gmail_after(since)}"
        return self._client.search_threads(query=query, max_results=max_results)

    def get_thread(self, thread_id: str) -> dict[str, Any]:
        return self._client.get_thread(thread_id=thread_id)

    # ─── Write operations ────────────────────────────────────────────────

    def mark_as_read(self, message_id: str) -> dict[str, Any]:
        """Remove the UNREAD label from a single message.

        Requires the workspace admin to have granted the `gmail.modify` scope
        in the Domain-Wide Delegation grant. If absent, Google returns 403 and
        we surface a clean error instead of crashing.
        """
        from googleapiclient.errors import HttpError

        try:
            return (
                self._client.service.users()
                .messages()
                .modify(
                    userId="me",
                    id=message_id,
                    body={"removeLabelIds": ["UNREAD"]},
                )
                .execute()
            )
        except HttpError as e:
            if e.resp.status in (401, 403):
                raise PermissionError(
                    "mark_as_read requires the gmail.modify scope to be granted "
                    "to the service account in Workspace Admin → Domain-wide delegation."
                ) from e
            raise

    # ─── LLM-backed summarization ────────────────────────────────────────

    @attribute_to("email")
    async def summarize_messages(
        self,
        messages: list[ParsedMessage],
        user_first_name: str = "",
    ) -> list[EmailSummary]:
        """Produce a one-sentence summary + action_required flag per message.

        Uses a single batched LLM call to stay cheap. Falls back to a
        snippet-based summary on any LLM error so the endpoint never 500s.
        """
        if not messages:
            return []

        user_first_name = (user_first_name or "").strip()
        prompt_blocks = []
        for i, m in enumerate(messages):
            addressed = "directly (To)" if m.addressing == "direct" else "Cc'd"
            prompt_blocks.append(
                f"[{i}] thread_id={m.thread_id}\n"
                f"Addressed to user: {addressed}\n"
                f"From: {m.sender}\n"
                f"Subject: {m.subject}\n"
                f"Snippet: {m.snippet}"
            )
        joined = "\n\n---\n\n".join(prompt_blocks)

        prompt = (
            "You are an executive assistant triaging a busy professional's inbox.\n"
            "For each email below, return ONE sentence (max 20 words) describing what it is "
            "and whether the user must personally respond.\n\n"
            f"User's first name: {user_first_name or '(unknown)'}\n\n"
            "An email is `action_required: true` ONLY if a real person directly asks the user "
            "(by name, 'you', or implied) to do, decide, or reply to something. Newsletters, "
            "FYI broadcasts, and automated notifications are NEVER action_required. "
            "Mail where the user is only Cc'd is rarely action_required unless they are "
            "explicitly asked to weigh in.\n\n"
            'Output ONLY a JSON object with an "items" array, one object per email, '
            "in input order, with keys:\n"
            "  index (int), summary (string), action_required (bool).\n\n"
            f"{joined}"
        )

        selected_provider_failed = False
        try:
            from rubik_core.config import settings
            from rubik_core.inference_workloads import run_text_async
            from rubik_core.models import get_genai_client

            async def managed(route):
                response = await get_genai_client().aio.models.generate_content(
                    model=route.model,
                    contents=prompt,
                )
                return response.text or ""

            raw = await run_text_async(
                "gmail_summaries",
                prompt,
                default_model=settings.VERTEX_LLM_MODEL_FLASH,
                managed=managed,
                json_mode=True,
            )
            raw = raw.strip()
            raw = (
                raw.removeprefix("```json")
                .removeprefix("```")
                .removesuffix("```")
                .strip()
            )
            data = json.loads(raw) if raw else []
            if isinstance(data, dict):
                data = data.get("items")
            if not isinstance(data, list):
                raise ValueError("AI summary shape is invalid")
            by_index = {
                int(d["index"]): d for d in data if isinstance(d, dict) and "index" in d
            }
        except Exception:
            from rubik_core.ai_providers.registry import model_routes

            try:
                selected = model_routes().get("gmail_summaries") or {}
                selected_provider_failed = selected.get("provider") not in (
                    None,
                    "vertex_managed",
                )
            except Exception:
                selected_provider_failed = True
            logger.warning("Gmail AI summarization failed")
            by_index = {}

        results: list[EmailSummary] = []
        for i, m in enumerate(messages):
            llm = by_index.get(i)
            if llm:
                summary = (llm.get("summary") or "").strip() or m.snippet[:160]
                action_required = bool(llm.get("action_required"))
            else:
                summary = (
                    "AI summary unavailable; original preview: " + m.snippet[:120]
                    if selected_provider_failed
                    else m.snippet[:160]
                )
                action_required = (
                    False
                    if selected_provider_failed
                    else self._heuristic_action_required(m, user_first_name)
                )
            results.append(
                EmailSummary(
                    account_id=self.account_id,
                    thread_id=m.thread_id,
                    message_id=m.message_id,
                    sender=m.sender,
                    subject=m.subject,
                    summary=summary,
                    action_required=action_required,
                    date=m.date,
                )
            )
        return results

    @staticmethod
    def _heuristic_action_required(msg: ParsedMessage, user_first_name: str) -> bool:
        """Backup detector for action_required when the LLM call fails."""
        haystack = f"{msg.subject}\n{msg.snippet}".lower()
        name = (user_first_name or "").lower()
        name_hit = (
            bool(name) and re.search(rf"\b{re.escape(name)}\b", haystack) is not None
        )
        verb_hit = any(v in haystack for v in _REQUEST_VERBS)
        return name_hit and verb_hit

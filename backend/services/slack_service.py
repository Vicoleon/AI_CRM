"""SlackService — UC-15 wrapper around the Slack Web API.

Scopes all reads strictly to the requesting user's installed Slack workspace
via their stored OAuth access_token (xoxp-…). NEVER exposes another user's
Slack content. Method calls degrade gracefully with `SlackNotConnectedError`
when the user hasn't completed OAuth, so callers (chat tools, briefing
service) can surface a "Connect Slack" CTA instead of crashing.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from pydantic import BaseModel
from sqlalchemy import select

from rubik_core.brand import PRODUCT_NAME
from rubik_core.control.db import control_session
from rubik_core.control.models import SlackInstallation
from rubik_core.control.repository import domain_belongs_to_workspace, slack_route_for
from rubik_core.cost.usage import attribute_to
from rubik_core.database.db import SessionLocal
from rubik_core.database.models import SlackCredential
from rubik_core.tenancy.context import current_workspace
from rubik_core.tenancy.secrets import workspace_secret
from rubik_core.tenancy.threads import ContextThreadPoolExecutor

logger = logging.getLogger(__name__)

SLACK_API_BASE = "https://slack.com/api"
DEFAULT_TIMEOUT = 10.0

# Channel-membership / system messages that shouldn't count toward "unread" —
# Slack's own unread badge ignores these too.
NOISE_SUBTYPES = frozenset(
    {
        "channel_join",
        "channel_leave",
        "group_join",
        "group_leave",
        "channel_topic",
        "channel_purpose",
        "channel_name",
        "channel_archive",
        "channel_unarchive",
    }
)


class SlackNotConnectedError(RuntimeError):
    """Raised when the user has no SlackCredential row. Callers should catch
    this and show a "Connect Slack" CTA rather than 500'ing."""


class SlackAPIError(RuntimeError):
    """Slack returned `ok: false`. The `.error` attribute mirrors Slack's
    error code (e.g. `invalid_auth`, `channel_not_found`)."""

    def __init__(self, error: str, response: dict[str, Any] | None = None):
        # Slack error codes are short identifiers. Do not let a malformed API
        # response carry a token or response body into exception text/UI logs.
        safe_error = (
            error
            if isinstance(error, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error)
            else "unknown_error"
        )
        super().__init__(safe_error)
        self.error = safe_error
        self.response = response or {}


def credential_domain_active(cred: SlackCredential | None) -> bool:
    """Recheck the OAuth hosted domain; aliases never supply this proof."""
    domain = getattr(cred, "hosted_domain", None)
    if not isinstance(domain, str) or not domain:
        return False
    try:
        return domain_belongs_to_workspace(current_workspace(), domain)
    except Exception:  # noqa: BLE001 - control outage cannot authorize a token
        return False


class SlackMessage(BaseModel):
    channel_id: str
    channel_name: str | None = None
    ts: str
    user_id: str | None = None
    user_name: str | None = None
    text: str
    permalink: str | None = None  # https://… web URL (fallback)
    app_link: str | None = None  # slack://… deep link (opens desktop app)
    is_mention: bool = False
    # Parent-thread ts when this message is a threaded reply; equals ``ts`` (or
    # None) for a top-level message. A reply whose ``thread_ts != ts`` is NOT on
    # the conversation's main timeline, so its read-state is invisible to the
    # channel ``last_read`` cursor — see ``_collect_unread_mentions``.
    thread_ts: str | None = None
    # Number of threaded replies under this message when it is a thread parent.
    # ``conversations.history`` only stamps ``reply_count`` on parents that have
    # replies, so ``0`` there means "known to have none". ``None`` means the
    # source did not say (``search.messages`` matches never carry it) — a
    # caller that needs to know has to fetch the thread.
    reply_count: int | None = None


class SlackService:
    """High-level Slack operations scoped to a single user's installation."""

    def __init__(self, user_email: str):
        if not user_email:
            raise ValueError("SlackService requires a user_email")
        self.user_email = user_email
        self._cred: SlackCredential | None = self._load_credential()
        self._user_cache: dict[str, str] = {}
        self._channel_cache: dict[str, str] = {}
        self._conv_meta_cache: dict[str, dict[str, Any]] = {}
        # (channel_id, thread_ts) → conversations.replies messages, cached so a
        # thread carrying several mentions is fetched once.
        self._thread_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}

    @property
    def is_connected(self) -> bool:
        cred = self._active_credential()
        self._cred = cred
        return bool(cred and cred.access_token)

    def _active_credential(self) -> SlackCredential | None:
        # A service can outlive disconnect. Never trust its cached token;
        # database or control failure denies use as well.
        try:
            return self._load_credential()
        except Exception:  # noqa: BLE001 - unavailable proof denies token use
            return None

    def _load_credential(self) -> SlackCredential | None:
        db = SessionLocal()
        try:
            cred = (
                db.query(SlackCredential)
                .filter(SlackCredential.user_email == self.user_email)
                .one_or_none()
            )
            if cred is None or not credential_domain_active(cred):
                return None
            try:
                installed = workspace_has_slack_team(cred.slack_team_id)
            except Exception:  # noqa: BLE001 - unavailable control data denies use
                installed = False
            if not installed:
                # Pre-Phase-2 rows have no signed installation and must be
                # reconnected before their personal token can be used.
                return None
            return cred
        finally:
            db.close()

    def _require_token(self) -> str:
        # Keep the checked credential in a local variable: concurrent name
        # lookups may refresh self._cred on another worker thread.
        cred = self._active_credential()
        self._cred = cred
        if cred is None or not cred.access_token:
            raise SlackNotConnectedError(
                f"Slack is not connected for {self.user_email}. "
                "Direct the user to Settings → Slack to authorize."
            )
        return cred.access_token

    # ─── Low-level Slack Web API call ────────────────────────────────────

    def _call(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Invoke a Slack Web API method. Raises SlackAPIError on `ok: false`."""
        token = self._require_token()
        url = f"{SLACK_API_BASE}/{method}"
        try:
            resp = httpx.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                params=params or {},
                timeout=DEFAULT_TIMEOUT,
            )
            data = resp.json()
        except httpx.HTTPError as e:
            logger.warning("Slack API HTTP error on %s: %s", method, type(e).__name__)
            raise SlackAPIError("http_error") from e

        if not data.get("ok"):
            error = SlackAPIError(data.get("error", "unknown_error"), data)
            logger.warning("Slack API error on %s: %s", method, error.error)
            raise error
        return data

    # ─── Lookup helpers (cached per service instance) ────────────────────

    def _resolve_user_name(self, user_id: str) -> str:
        if not user_id:
            return ""
        if user_id in self._user_cache:
            return self._user_cache[user_id]
        try:
            data = self._call("users.info", {"user": user_id})
            profile = data.get("user", {}).get("profile", {})
            name = (
                profile.get("display_name")
                or profile.get("real_name")
                or data.get("user", {}).get("name")
                or user_id
            )
        except SlackAPIError:
            name = user_id
        self._user_cache[user_id] = name
        return name

    def _resolve_channel_name(self, channel_id: str) -> str:
        if not channel_id:
            return ""
        if channel_id in self._channel_cache:
            return self._channel_cache[channel_id]
        try:
            data = self._call("conversations.info", {"channel": channel_id})
            name = data.get("channel", {}).get("name") or channel_id
        except SlackAPIError:
            name = channel_id
        self._channel_cache[channel_id] = name
        return name

    def _resolve_channel_id(self, channel: str) -> str:
        """Accept either a channel ID (C…) or a `#name` and return the ID.

        Tries two Slack APIs in order:
          1. `users.conversations` — channels the authed user is a member of,
             including private. This is the authoritative source for "the
             user's own Slack" and works around a `conversations.list` quirk
             where private channels are silently dropped from the response.
          2. `conversations.list` — fallback for public channels the user
             isn't a member of but can still read with `channels:history`.

        Match is case-insensitive since Slack lowercases names but the LLM
        may pass mixed case.
        """
        cleaned = channel.lstrip("#").strip()
        if not cleaned:
            raise SlackAPIError("channel_not_found")
        if cleaned.startswith(("C", "G", "D")) and cleaned.isalnum():
            return cleaned
        target = cleaned.lower()

        def _scan(method: str, types: str) -> str | None:
            cursor = ""
            for _ in range(5):
                params: dict[str, Any] = {
                    "limit": 200,
                    "exclude_archived": "true",
                    "types": types,
                }
                if cursor:
                    params["cursor"] = cursor
                data = self._call(method, params)
                for ch in data.get("channels", []):
                    if (ch.get("name") or "").lower() == target:
                        return ch["id"]
                cursor = data.get("response_metadata", {}).get("next_cursor", "")
                if not cursor:
                    return None
            return None

        # Primary: channels the user is in (public + private). `mpim` excluded
        # because group-DM IDs aren't user-namable channels.
        hit = _scan("users.conversations", "public_channel,private_channel")
        if hit:
            return hit
        # Fallback: public channels they're not a member of.
        hit = _scan("conversations.list", "public_channel")
        if hit:
            return hit
        raise SlackAPIError("channel_not_found")

    def _permalink(self, channel_id: str, ts: str) -> str | None:
        try:
            data = self._call(
                "chat.getPermalink", {"channel": channel_id, "message_ts": ts}
            )
            return data.get("permalink")
        except SlackAPIError:
            return None

    def _app_link(self, channel_id: str, ts: str = "") -> str | None:
        """Build a `slack://` URL that opens the desktop app at this message.

        macOS/Windows Slack registers the `slack://` scheme; clicking the URL
        deep-links straight to the channel/message instead of bouncing through
        the browser. Falls back to None when team_id is unknown so callers
        can degrade to the web permalink.
        """
        team_id = self._cred.slack_team_id if self._cred else ""
        if not (team_id and channel_id):
            return None
        if ts:
            return f"slack://channel?team={team_id}&id={channel_id}&message={ts}"
        return f"slack://channel?team={team_id}&id={channel_id}"

    def _slack_user_id(self) -> str:
        return self._cred.slack_user_id if self._cred else ""

    # Public read-only views for callers outside this module (the open-loops
    # sweep in backend/services/open_loops.py). They exist so nobody reaches
    # for the underscored helpers from another file.
    def slack_user_id(self) -> str:
        """The connected user's own Slack member id (``""`` when unknown)."""
        return self._slack_user_id()

    def resolve_user_name(self, user_id: str) -> str:
        """Display name for a Slack member id; falls back to the id. Cached."""
        return self._resolve_user_name(user_id)

    def get_thread_replies(self, channel_id: str, thread_ts: str) -> list[SlackMessage]:
        """Every message in one thread, oldest first, parent included.

        ``conversations.replies`` on a message that is not a thread returns
        just that message, so this is safe to call on any ts. No permalink
        round-trip per reply — callers link to the parent.
        """
        raw = self._fetch_thread(channel_id, thread_ts)
        if not raw:
            return []
        channel_name = self._resolve_channel_name(channel_id)
        self._prefetch_names(
            self._resolve_user_name,
            ((m.get("user") or m.get("bot_id") or "") for m in raw),
        )
        msgs = [
            self._parse_message(m, channel_id, channel_name, skip_permalink=True)
            for m in raw
            if m.get("subtype") not in NOISE_SUBTYPES
        ]
        msgs.sort(key=lambda m: float(m.ts or 0))
        return msgs

    def _prefetch_names(self, resolver, ids) -> None:
        """Warm a name cache by resolving DISTINCT ids concurrently.

        ``resolver`` is ``_resolve_user_name`` / ``_resolve_channel_name`` — both
        are idempotent and cache-backed, so this just front-loads the HTTP
        round-trips that would otherwise happen serially (an N+1) while parsing
        messages. Best-effort: a failed lookup falls back to the id, same as the
        inline path. Concurrency is capped so we don't trip Slack's rate limiter.
        """
        distinct = {i for i in ids if i}
        if not distinct:
            return
        if len(distinct) == 1:
            resolver(next(iter(distinct)))
            return
        with ContextThreadPoolExecutor(max_workers=min(8, len(distinct))) as ex:
            list(ex.map(resolver, distinct))

    def _fetch_unread_channel(
        self, ch: dict[str, Any], cutoff_ts: float, per_channel_limit: int
    ) -> tuple[dict[str, Any], list[dict[str, Any]] | None]:
        """Blocking fetch of one channel's unread slice — messages since
        the later of ``last_read`` and the time cutoff, minus the user's own.
        Returns ``(ch, filtered | None)``; None means error/empty. Pure read with
        no shared mutable state, so it's safe to fan out across threads."""
        ch_id = ch["id"]
        try:
            last_read_ts = float(ch.get("last_read") or 0)
        except (TypeError, ValueError):
            last_read_ts = 0.0
        oldest_ts = max(last_read_ts, cutoff_ts)
        try:
            data = self._call(
                "conversations.history",
                {
                    "channel": ch_id,
                    "oldest": f"{oldest_ts:.6f}",
                    "limit": per_channel_limit,
                },
            )
        except SlackAPIError as e:
            logger.info(f"Skipping {ch.get('name')} ({ch_id}): {e.error}")
            return ch, None
        raw = data.get("messages", []) or []
        slack_uid = self._slack_user_id()
        filtered = [m for m in raw if m.get("user") != slack_uid]
        return ch, (filtered or None)

    # ─── Public read API ─────────────────────────────────────────────────

    def get_channel_messages(
        self,
        channel: str,
        since: datetime | None = None,
        limit: int = 50,
        skip_permalink: bool = False,
    ) -> list[SlackMessage]:
        """Return recent messages in a channel.

        `channel` accepts either a channel ID (``C0123…``) or a ``#name``.
        `since` constrains to messages on/after the given UTC datetime.
        `skip_permalink=True` suppresses the per-message ``chat.getPermalink``
        round-trip (~400ms each) — callers that already build an ``app_link``
        deep-link should pass it to keep bulk scans fast.
        """
        channel_id = self._resolve_channel_id(channel)
        params: dict[str, Any] = {"channel": channel_id, "limit": min(limit, 200)}
        if since is not None:
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
            params["oldest"] = f"{since.timestamp():.6f}"
        data = self._call("conversations.history", params)
        channel_name = self._resolve_channel_name(channel_id)
        msgs = data.get("messages", [])
        # Warm author names concurrently so _parse_message's per-message
        # users.info resolves are cache hits, not a serial N+1.
        self._prefetch_names(
            self._resolve_user_name,
            ((m.get("user") or m.get("bot_id") or "") for m in msgs),
        )
        return [
            self._parse_message(
                m, channel_id, channel_name, skip_permalink=skip_permalink
            )
            for m in msgs
        ]

    def get_mentions(
        self,
        since: datetime | None = None,
        limit: int = 30,
    ) -> list[SlackMessage]:
        """Return messages that @-mention the connected user.

        Slack's `search.messages` requires the `search:read` user scope.
        Without it, returns an empty list (the manifest grants it by default).
        """
        slack_uid = self._slack_user_id()
        if not slack_uid:
            return []
        query = f"<@{slack_uid}>"
        if since is not None:
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
            query += f" after:{since.strftime('%Y-%m-%d')}"
        try:
            data = self._call(
                "search.messages",
                {"query": query, "count": min(limit, 100), "sort": "timestamp"},
            )
        except SlackAPIError as e:
            if e.error in ("missing_scope", "not_allowed_token_type"):
                logger.info(
                    f"Slack search scope missing for {self.user_email}; "
                    "user needs to re-authorize after manifest update."
                )
                return []
            raise
        return [
            self._parse_search_match(m)
            for m in data.get("messages", {}).get("matches", [])
        ]

    def search_messages(
        self,
        query: str,
        limit: int = 20,
        *,
        sort: str = "score",
        raise_on_scope: bool = False,
    ) -> list[SlackMessage]:
        """Free-text search across the user's accessible Slack content.

        ``sort="timestamp"`` returns newest first — what a "what did I say
        this week" read wants; the default ranks by relevance.

        A token without ``search:read`` answers ``[]`` by default (the chat
        reads degrade quietly). ``raise_on_scope=True`` re-raises that
        ``SlackAPIError`` instead, for a caller that must tell "could not
        search" from "found nothing" (the Loose Ends scan's coverage)."""
        try:
            data = self._call(
                "search.messages",
                {
                    "query": query,
                    "count": min(limit, 100),
                    "sort": "timestamp" if sort == "timestamp" else "score",
                },
            )
        except SlackAPIError as e:
            if e.error in ("missing_scope", "not_allowed_token_type"):
                if raise_on_scope:
                    raise
                return []
            raise
        matches = data.get("messages", {}).get("matches", [])
        # The "give me a brief of my conversations with X" path. Warm
        # author + channel names concurrently so _parse_search_match doesn't pay
        # a serial users.info / conversations.info per result. Mirror that
        # method's conditions — it only resolves when the match has no inline
        # ``username`` / channel ``name`` — so we don't add wasted round-trips.
        self._prefetch_names(
            self._resolve_user_name,
            (m.get("user", "") for m in matches if not m.get("username")),
        )
        self._prefetch_names(
            self._resolve_channel_name,
            (
                (m.get("channel") or {}).get("id", "")
                for m in matches
                if not (m.get("channel") or {}).get("name")
            ),
        )
        return [self._parse_search_match(m) for m in matches]

    @attribute_to("slack")
    async def summarize_channel_activity(
        self,
        channel: str,
        hours_back: int = 24,
        max_messages: int = 100,
    ) -> dict[str, Any]:
        """Produce an executive summary of recent activity in a channel.

        Pulls up to `max_messages` from the last `hours_back` hours and asks
        an LLM to extract: key topics, decisions, action items, and notable
        questions. Falls back to a chronological raw list on LLM failure so
        the chat tool never returns nothing useful.
        """
        from datetime import timedelta

        since = datetime.now(timezone.utc) - timedelta(hours=hours_back)
        # This summary cites messages via app_link (line ~345 uses
        # `m.app_link or m.permalink`), so the per-message chat.getPermalink
        # round-trip is pure waste here — up to max_messages (100) × ~400ms.
        # skip_permalink=True keeps the single-channel summary under the timeout.
        messages = self.get_channel_messages(
            channel=channel, since=since, limit=max_messages, skip_permalink=True
        )
        channel_label = (
            f"#{messages[0].channel_name}"
            if messages and messages[0].channel_name
            else channel
        )
        if not messages:
            return {
                "channel": channel_label,
                "hours_back": hours_back,
                "message_count": 0,
                "summary_md": f"No activity in {channel_label} in the last {hours_back}h.",
            }

        # Build a compact transcript for the LLM with [Mn] citation tags so
        # the summary can deep-link back to source messages.
        transcript_lines: list[str] = []
        citation_map: dict[str, str] = {}
        channel_id_for_link = messages[0].channel_id if messages else ""
        for idx, m in enumerate(messages, start=1):
            who = m.user_name or m.user_id or "(unknown)"
            text = m.text.replace("\n", " ").strip()
            if not text:
                continue
            tag = f"M{idx}"
            link = m.app_link or m.permalink or ""
            if link:
                citation_map[tag] = link
            transcript_lines.append(f"[{tag}] {who}: {text[:500]}")
        transcript = "\n".join(transcript_lines)

        prompt = (
            "You are summarizing a Slack channel for a busy professional who "
            "wants the SIGNAL, not the transcript. They already have Slack — "
            "they're asking you because they don't want to read it all.\n\n"
            f"Channel: {channel_label}\n"
            f"Window: last {hours_back} hours ({len(messages)} messages)\n\n"
            "Output structured markdown with up to four sections — OMIT any "
            "section that has nothing meaningful in it:\n"
            "  **Key topics** — 1-3 bullets, each one short sentence\n"
            "  **Decisions / announcements** — what was decided or shared\n"
            "  **Action items / asks** — who is asked to do what (use names)\n"
            "  **Questions still open** — unanswered asks worth surfacing\n\n"
            "CITATIONS — REQUIRED:\n"
            "- Every transcript line begins with a tag like [M14]. End each "
            "  bullet you write with the tag(s) of the source message(s), "
            '  e.g. "...needs review by Friday [M14]".\n'
            "- Cite the SPECIFIC message(s) the bullet summarizes — never "
            "  invent tags, never reuse a tag from a different bullet.\n"
            "- Tags appear inside square brackets, with the M prefix and a "
            "  number, nothing else.\n\n"
            "Rules:\n"
            '- Be concise. No preamble, no "here\'s what I found".\n'
            "- Use real participant names, not @user IDs.\n"
            "- Skip greetings, thank-yous, and small-talk.\n"
            "- If literally nothing important happened, say so in one line.\n\n"
            "Transcript:\n"
            f"{transcript}"
        )

        try:
            from rubik_core.config import settings
            from rubik_core.gemini_tuning import generation_config_for
            from rubik_core.inference_workloads import run_text_async
            from rubik_core.models import get_genai_client

            # Slack summary on the LITE tier (item 11): summarization, not
            # reasoning — LITE is materially faster than FLASH, and Slack
            # catch-up was the slowest chat tool (24-43s in a timing test).
            # Per-channel fetch is already parallel (gather + to_thread above).
            # generation_config_for keeps that speed on the Gemini 3.x LITE tier,
            # which thinks by default unless asked for "minimal".
            async def managed(route):
                response = await get_genai_client().aio.models.generate_content(
                    model=route.model,
                    contents=prompt,
                    config=generation_config_for(route.model),
                )
                return response.text or ""

            summary_md = (
                await run_text_async(
                    "slack_summaries",
                    prompt,
                    default_model=settings.VERTEX_LLM_MODEL_LITE,
                    managed=managed,
                )
            ).strip()
            if not summary_md:
                raise RuntimeError("empty LLM response")
        except Exception:
            logger.warning("Slack summarization failed")
            # Fallback: chronological raw, just so the user gets something
            summary_md = (
                "_LLM summary unavailable — showing raw activity:_\n"
                + "\n".join(f"- {line}" for line in transcript_lines[:20])
            )

        # Replace [Mn] tags with markdown links to the source messages.
        # No header to link in single-channel mode (the chat tool prepends
        # its own header), so per_channel is empty.
        summary_md = self._render_citations(
            summary_md,
            citation_map,
            per_channel=[
                {
                    "channel_name": channel_label.lstrip("#"),
                    "app_link": self._app_link(channel_id_for_link)
                    if channel_id_for_link
                    else None,
                }
            ],
        )

        return {
            "channel": channel_label,
            "hours_back": hours_back,
            "message_count": len(messages),
            "summary_md": summary_md,
        }

    def list_member_channels(
        self,
        types: str = "public_channel,private_channel",
        max_channels: int = 200,
    ) -> list[dict[str, Any]]:
        """List channels the auth user is a member of, with `last_read` cursors.

        Filters out group DMs (mpim) by default — those are usually noise for
        a "what did I miss" summary. Caller can pass `types="..."` to opt in.
        """
        out: list[dict[str, Any]] = []
        cursor = ""
        while len(out) < max_channels:
            params: dict[str, Any] = {
                "limit": min(200, max_channels - len(out)),
                "exclude_archived": "true",
                "types": types,
            }
            if cursor:
                params["cursor"] = cursor
            data = self._call("users.conversations", params)
            for ch in data.get("channels", []):
                if ch.get("is_mpim") and "mpim" not in types:
                    continue
                out.append(
                    {
                        "id": ch.get("id"),
                        "name": ch.get("name"),
                        "is_private": bool(ch.get("is_private")),
                        "last_read": ch.get("last_read"),
                    }
                )
            cursor = data.get("response_metadata", {}).get("next_cursor", "")
            if not cursor:
                break
        return out

    @attribute_to("slack")
    async def summarize_unread_across_channels(
        self,
        hours_back: int = 24,
        per_channel_limit: int = 30,
        max_channels: int = 40,
    ) -> dict[str, Any]:
        """Cross-channel "what did I miss" summary.

        For every channel the user is in, fetch messages since the later of
        (a) `last_read` and (b) `now - hours_back`. Skip channels with no new
        messages. Send the combined transcript to an LLM with a structured
        prompt that produces a per-channel highlight list — so the user can
        skim the digest instead of opening Slack.
        """
        from datetime import timedelta

        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
        cutoff_ts = cutoff.timestamp()

        channels = self.list_member_channels()[:max_channels]

        # Fetch every channel's unread slice CONCURRENTLY. The old serial
        # loop (one blocking conversations.history per channel, up to 40 of them)
        # was the main reason "what did I miss on Slack" blew past the 90s chat
        # timeout. `_call` stays synchronous; we fan it out across threads,
        # semaphore-capped so we don't trip Slack's rate limiter.
        sem = asyncio.Semaphore(8)

        async def _fetch(ch: dict[str, Any]):
            async with sem:
                return await asyncio.to_thread(
                    self._fetch_unread_channel, ch, cutoff_ts, per_channel_limit
                )

        fetched = await asyncio.gather(*[_fetch(ch) for ch in channels])

        # Warm the author-name cache concurrently too, so the assembly below is
        # all cache hits instead of a serial users.info per message (the N+1).
        self._prefetch_names(
            self._resolve_user_name,
            (
                (m.get("user") or m.get("bot_id") or "")
                for ch, filtered in fetched
                if filtered
                for m in filtered
            ),
        )

        per_channel: list[dict[str, Any]] = []
        total_messages = 0
        # Global citation map: { "M1": "slack://…msg=…", "M2": …, … }
        # The LLM receives transcripts annotated with [Mn] tags; the
        # post-processor swaps [Mn] → markdown link to the source message.
        citation_map: dict[str, str] = {}
        msg_counter = [0]  # mutable so closures can bump it

        # Assembly stays SERIAL and in channel order, so [Mn] tag numbering and
        # the citation map are deterministic (identical to the sequential path).
        for ch, filtered in fetched:
            if not filtered:
                continue
            ch_id = ch["id"]
            transcript_lines = []
            for m in filtered:
                user_id = m.get("user") or m.get("bot_id") or ""
                who = self._resolve_user_name(user_id) if user_id else "(unknown)"
                text = (m.get("text") or "").replace("\n", " ").strip()
                if not text:
                    continue
                msg_counter[0] += 1
                tag = f"M{msg_counter[0]}"
                ts = m.get("ts", "")
                msg_link = self._app_link(ch_id, ts) or ""
                if msg_link:
                    citation_map[tag] = msg_link
                # Prefix each line with [Mn] so the LLM can cite it.
                transcript_lines.append(f"[{tag}] {who}: {text[:400]}")
            if not transcript_lines:
                continue
            per_channel.append(
                {
                    "channel_id": ch_id,
                    "channel_name": ch.get("name"),
                    "is_private": ch.get("is_private"),
                    "message_count": len(filtered),
                    "transcript": "\n".join(transcript_lines),
                    "app_link": self._app_link(ch_id),
                    "permalink": None,  # web fallback isn't needed for channel-level
                }
            )
            total_messages += len(filtered)

        if not per_channel:
            return {
                "hours_back": hours_back,
                "channel_count": 0,
                "message_count": 0,
                "summary_md": f"No unread Slack activity in the last {hours_back}h.",
                "channels": [],
            }

        # Build a single batched prompt — Gemini Flash handles ~1M tokens, so
        # 40 channels × 30 messages × 400 chars is comfortably within budget.
        sections = []
        for ch in per_channel:
            label = f"#{ch['channel_name']}"
            sections.append(
                f"--- {label} ({ch['message_count']} messages) ---\n{ch['transcript']}"
            )
        joined = "\n\n".join(sections)

        # Build the explicit channel list for the LLM so it knows which
        # sections to produce. Without this it tends to drop channels.
        channel_list_md = "\n".join(
            f"  {i + 1}. #{ch['channel_name']} ({ch['message_count']} msg)"
            for i, ch in enumerate(per_channel)
        )

        prompt = (
            "You are giving a busy professional their Slack catch-up. They "
            "have unread messages across multiple channels and don't want to "
            "open Slack to triage. Your job is to digest EVERY channel — "
            "they decide what's noise, not you.\n\n"
            f"Window: last {hours_back} hours\n"
            f"Channels with activity: {len(per_channel)} ({total_messages} messages)\n\n"
            "Channels you MUST produce a section for, in this order:\n"
            f"{channel_list_md}\n\n"
            "Output structured markdown. For each of the channels listed "
            "above, produce one section in this exact format:\n\n"
            "### #channel-name\n"
            "- 1-4 bullets covering what was actually discussed. Lead with "
            "decisions, action items, and asks. Then announcements. Then "
            "the rest. Use real participant names, not @user IDs.\n"
            "- If the channel really had nothing of substance (e.g. only a "
            'thank-you or a bot ping), one bullet is fine: "_Light '
            'activity — only a [thank-you / bot notification / etc]._" — '
            "but still include the section.\n\n"
            "CITATIONS — REQUIRED:\n"
            "- Every transcript line begins with a tag like [M14]. End each "
            "  bullet you write with the tag(s) of the source message(s), "
            '  e.g. "...needs review by Friday [M14]" or "...green-lit '
            '  the v3 spec [M22] [M23]".\n'
            "- Cite the SPECIFIC message(s) the bullet summarizes — never "
            "  invent tags, never reuse a tag from a different bullet.\n"
            "- Tags appear inside square brackets, with the M prefix and a "
            "  number, nothing else.\n\n"
            "Rules:\n"
            "- Be concise per bullet, but DON'T drop channels. If the "
            "  channels-list above has 10 entries, your output has 10 "
            "  sections. The user wants comprehensive coverage.\n"
            "- Quote concrete details (numbers, dates, names, links) "
            "  when they appear — don't paraphrase them away.\n"
            '- Skip your own preamble ("Here\'s the catch-up…"). Start '
            "  directly with the first ### header.\n\n"
            f"{joined}"
        )

        try:
            from rubik_core.config import settings
            from rubik_core.gemini_tuning import generation_config_for
            from rubik_core.inference_workloads import run_text_async
            from rubik_core.models import get_genai_client

            # Slack summary on the LITE tier (item 11): summarization, not
            # reasoning — LITE is materially faster than FLASH, and Slack
            # catch-up was the slowest chat tool (24-43s in a timing test).
            # Per-channel fetch is already parallel (gather + to_thread above).
            # generation_config_for keeps that speed on the Gemini 3.x LITE tier,
            # which thinks by default unless asked for "minimal".
            async def managed(route):
                response = await get_genai_client().aio.models.generate_content(
                    model=route.model,
                    contents=prompt,
                    config=generation_config_for(route.model),
                )
                return response.text or ""

            summary_md = (
                await run_text_async(
                    "slack_summaries",
                    prompt,
                    default_model=settings.VERTEX_LLM_MODEL_LITE,
                    managed=managed,
                )
            ).strip()
            if not summary_md:
                raise RuntimeError("empty LLM response")
        except Exception:
            logger.warning("Cross-channel summarization failed")
            # Fallback: just list channels with counts.
            summary_md = (
                "_LLM summary unavailable — channels with unread:_\n"
                + "\n".join(
                    f"- #{ch['channel_name']}: {ch['message_count']} messages"
                    for ch in per_channel
                )
            )

        # Post-process: replace [Mn] tags with markdown links to the source
        # message, and turn ### #channel-name headers into links to the
        # channel itself. Both use slack:// so the desktop app opens.
        summary_md = self._render_citations(summary_md, citation_map, per_channel)

        return {
            "hours_back": hours_back,
            "channel_count": len(per_channel),
            "message_count": total_messages,
            "summary_md": summary_md,
            # Lightweight per-channel metadata so the caller (briefing card,
            # frontend) can render direct app-links without re-querying.
            "channels": [
                {
                    "name": ch["channel_name"],
                    "id": ch["channel_id"],
                    "message_count": ch["message_count"],
                    "app_link": ch["app_link"],
                }
                for ch in per_channel
            ],
        }

    @staticmethod
    def _render_citations(
        summary_md: str,
        citation_map: dict[str, str],
        per_channel: list[dict[str, Any]],
    ) -> str:
        """Replace `[Mn]` tags with markdown links and link channel headers.

        Tag replacement: `[M14]` → `[↗](slack://…)`. Multiple consecutive
        tags become multiple links. Tags the LLM hallucinated (no entry in
        citation_map) are stripped silently so the user never sees a dead
        anchor. Channel headers get linked using each channel's app_link.
        """
        import re

        def _replace_tag(match: re.Match) -> str:
            tag = match.group(1)
            link = citation_map.get(tag)
            if not link:
                return ""  # drop hallucinated tags rather than leak [M99]
            return f" [↗]({link})"

        # Match [M<digits>] anywhere; allow optional surrounding whitespace
        # to absorb so we don't end up with double spaces after stripping.
        out = re.sub(r"\s*\[(M\d+)\]", _replace_tag, summary_md)

        # Build a name → app_link map for header linking. Lowercased on both
        # sides since LLMs sometimes Title-Case channel names.
        link_by_name = {
            (ch["channel_name"] or "").lower(): ch["app_link"]
            for ch in per_channel
            if ch.get("app_link")
        }

        def _link_header(match: re.Match) -> str:
            prefix = match.group(1)  # "### " or similar
            name = match.group(2)
            link = link_by_name.get(name.lower())
            if not link:
                return match.group(0)  # leave untouched
            return f"{prefix}[#{name}]({link})"

        out = re.sub(
            r"^(#{1,4}\s*)#([a-zA-Z0-9_\-]+)", _link_header, out, flags=re.MULTILINE
        )
        return out

    def get_dm_threads(self, limit: int = 20) -> list[dict[str, Any]]:
        """List the user's DM / group-DM conversations (channel IDs + names).

        Uses ``users.conversations`` (the caller's OWN conversations) rather than
        ``conversations.list``: the latter omits 1:1 ``im``s for user tokens, so
        only group DMs surfaced. Counterparty names are warmed concurrently."""
        data = self._call(
            "users.conversations",
            {"types": "im,mpim", "limit": min(limit, 100)},
        )
        chans = data.get("channels", [])
        # Warm counterparty names concurrently (only ims carry `user`).
        self._prefetch_names(
            self._resolve_user_name, (ch.get("user") for ch in chans if ch.get("user"))
        )
        out: list[dict[str, Any]] = []
        for ch in chans:
            counterparty_id = ch.get("user")
            out.append(
                {
                    "channel_id": ch.get("id"),
                    "is_im": ch.get("is_im", False),
                    "is_mpim": ch.get("is_mpim", False),
                    "counterparty_id": counterparty_id,
                    "counterparty_name": self._resolve_user_name(counterparty_id)
                    if counterparty_id
                    else None,
                    "last_read": ch.get("last_read"),
                }
            )
        return out

    def _conversation_meta(self, channel_id: str) -> dict[str, Any]:
        """``conversations.info`` → the calling user's ``last_read`` cursor.

        Returns ``{"last_read": str|None}``, cached per instance. ``last_read``
        is the authoritative read cursor: ``conversations.info`` reliably returns
        it, whereas (a) ``users.conversations`` returns it as ``None`` (which made
        the first cut over-count), and (b) ``conversations.info`` does NOT return
        ``unread_count``/``unread_count_display`` at all (always ``None``) — so
        unread is counted from history relative to this cursor, not read off the
        channel object."""
        if channel_id in self._conv_meta_cache:
            return self._conv_meta_cache[channel_id]
        meta: dict[str, Any] = {"last_read": None}
        try:
            data = self._call("conversations.info", {"channel": channel_id})
            meta["last_read"] = (data.get("channel") or {}).get("last_read")
        except SlackAPIError as e:
            logger.info(f"conversations.info failed for {channel_id}: {e.error}")
        self._conv_meta_cache[channel_id] = meta
        return meta

    def _unread_in_conversation(
        self, channel_id: str, last_read: str | None, *, limit: int = 60
    ) -> tuple[int, str | None, str | None]:
        """Count messages after ``last_read`` and return
        ``(count, latest_ts, latest_text)`` for the newest unread message.

        Excludes the user's own messages and channel/system noise — mirrors what
        Slack's unread badge counts. ``last_read`` must come from
        ``conversations.info``. With no cursor we return 0 rather than guess
        (better to under- than over-count, per the "I don't have that many"
        feedback)."""
        if not last_read:
            return 0, None, None
        try:
            lr = float(last_read)
        except (TypeError, ValueError):
            return 0, None, None
        # Prefer `oldest=last_read` (server-side trim), but some conversations
        # (notably DMs/mpims) reject the cursor with `invalid_ts_oldest`. On any
        # API error, retry fetching the recent slice and trim by ts in code — the
        # `ts > lr` filter below makes both paths equivalent.
        try:
            data = self._call(
                "conversations.history",
                {
                    "channel": channel_id,
                    "oldest": last_read,
                    "inclusive": "false",
                    "limit": limit,
                },
            )
        except SlackAPIError:
            try:
                data = self._call(
                    "conversations.history", {"channel": channel_id, "limit": limit}
                )
            except SlackAPIError:
                return 0, None, None
        me = self._slack_user_id()
        count = 0
        latest_ts: str | None = None
        latest_text: str | None = None
        for m in data.get("messages", []) or []:  # newest first
            if m.get("user") == me or m.get("subtype") in NOISE_SUBTYPES:
                continue
            try:
                if float(m.get("ts", "0")) <= lr:
                    continue
            except ValueError:
                continue
            if latest_ts is None:
                latest_ts, latest_text = m.get("ts"), m.get("text")
            count += 1
        return count, latest_ts, latest_text

    async def _collect_unread_dms(
        self, *, dm_limit: int, sem: asyncio.Semaphore
    ) -> list[dict[str, Any]]:
        """Unread DM / group-DM threads with a latest-message preview, newest
        first. Backs the ``get_activity_overview`` counts/feed."""
        try:
            dm_threads = await asyncio.to_thread(self.get_dm_threads, dm_limit)
        except SlackAPIError as e:
            logger.info(f"Slack DM list failed for {self.user_email}: {e.error}")
            return []

        async def _dm_unread(dm: dict[str, Any]):
            cid = dm["channel_id"]
            async with sem:
                last_read = (await asyncio.to_thread(self._conversation_meta, cid))[
                    "last_read"
                ]
                count, ts, text = await asyncio.to_thread(
                    self._unread_in_conversation, cid, last_read
                )
            return dm, count, ts, text

        dm_candidates = [d for d in dm_threads if d.get("channel_id")]
        dm_fetched = (
            await asyncio.gather(*[_dm_unread(d) for d in dm_candidates])
            if dm_candidates
            else []
        )
        unread_dms: list[dict[str, Any]] = []
        for dm, count, ts, text in dm_fetched:
            if count <= 0:
                continue
            unread_dms.append(
                {
                    "channel_id": dm["channel_id"],
                    "counterparty_name": dm.get("counterparty_name")
                    or ("Group message" if dm.get("is_mpim") else "Direct message"),
                    "unread_count": count,
                    "is_group": bool(dm.get("is_mpim")),
                    "app_link": self._app_link(dm["channel_id"]),
                    "ts": ts or "",
                    "preview": (text or "")[:280],
                }
            )
        unread_dms.sort(key=lambda d: d.get("ts") or "", reverse=True)
        return unread_dms

    def _fetch_thread(self, channel_id: str, thread_ts: str) -> list[dict[str, Any]]:
        """``conversations.replies`` for one thread; ``[]`` on error.

        Cached per instance so a thread carrying several mentions is fetched
        once. Pure read with no shared mutable state beyond the cache, so it's
        safe to fan out across threads."""
        key = (channel_id, thread_ts)
        if key in self._thread_cache:
            return self._thread_cache[key]
        try:
            data = self._call(
                "conversations.replies",
                {"channel": channel_id, "ts": thread_ts, "limit": 200},
            )
            msgs = data.get("messages", []) or []
        except SlackAPIError as e:
            logger.info(
                f"conversations.replies failed for {channel_id}/{thread_ts}: {e.error}"
            )
            msgs = []
        self._thread_cache[key] = msgs
        return msgs

    @staticmethod
    def _thread_reply_is_read(
        thread_msgs: list[dict[str, Any]], mention_ts: str, me: str
    ) -> bool:
        """Best-effort read check for a mention that lives in a thread reply.

        Slack tracks per-thread read state separately from a conversation's
        ``last_read`` and exposes NO per-thread read cursor to user tokens, so a
        non-broadcast reply's ts is forever greater than ``last_read`` (it never
        lands on the main timeline). We instead treat the reply as read on
        positive engagement by the user — proof they saw it:
          * they reacted to the mention message, or
          * they posted their own reply at/after the mention's ts.
        Absent either signal we can't confirm, so we return False (keep showing
        it) rather than hide a genuinely-unread mention."""
        if not me:
            return False
        try:
            mention_f: float | None = float(mention_ts)
        except (TypeError, ValueError):
            mention_f = None
        for msg in thread_msgs:
            # The user's own reply at/after the mention → they were in the thread.
            if msg.get("user") == me and mention_f is not None:
                try:
                    if float(msg.get("ts", "0")) >= mention_f:
                        return True
                except (TypeError, ValueError):
                    pass
            # A reaction by the user on the mention message itself.
            if msg.get("ts") == mention_ts:
                for reaction in msg.get("reactions") or []:
                    if me in (reaction.get("users") or []):
                        return True
        return False

    async def _collect_unread_mentions(
        self, *, mention_lookback_days: int, mention_limit: int, sem: asyncio.Semaphore
    ) -> list[SlackMessage]:
        """@-mentions in the lookback window, filtered to UNREAD only.

        For a top-level message a mention is unread when its ts is newer than its
        conversation's ``last_read`` cursor. That cursor tracks only the main
        timeline, though — a non-broadcast THREAD reply never appears there, so
        its ts stays permanently above ``last_read`` and it would otherwise be
        stuck as "unread" forever even after the user reads the thread. For those
        we fall back to a thread-aware engagement check (see
        ``_thread_reply_is_read``). Conversation metas and thread fetches not
        already cached are resolved concurrently. Backs the
        ``get_activity_overview`` counts/feed.
        """
        from datetime import timedelta

        cutoff = datetime.now(timezone.utc) - timedelta(days=mention_lookback_days)
        try:
            raw_mentions = await asyncio.to_thread(
                self.get_mentions, cutoff, mention_limit
            )
        except SlackAPIError as e:
            logger.info(f"Slack mentions fetch failed for {self.user_email}: {e.error}")
            return []

        missing = sorted(
            {
                m.channel_id
                for m in raw_mentions
                if m.channel_id and m.channel_id not in self._conv_meta_cache
            }
        )

        async def _meta_only(cid: str):
            async with sem:
                return await asyncio.to_thread(self._conversation_meta, cid)

        if missing:
            await asyncio.gather(*[_meta_only(c) for c in missing])

        def _cursor_says_read(m: SlackMessage) -> bool:
            """True when the conversation's main-timeline read cursor already
            covers this ts (reliable for top-level messages AND broadcasts)."""
            last_read = self._conv_meta_cache.get(m.channel_id, {}).get("last_read")
            try:
                return last_read is not None and float(m.ts) <= float(last_read)
            except (TypeError, ValueError):
                return False

        def _is_thread_reply(m: SlackMessage) -> bool:
            return bool(m.thread_ts) and m.thread_ts != m.ts

        # Only thread replies the cursor DIDN'T already clear need the extra
        # (more expensive) thread fetch. Dedup by thread so a thread with several
        # mentions is fetched once, and fan the fetches out concurrently.
        threads_to_load = sorted(
            {
                (m.channel_id, m.thread_ts)
                for m in raw_mentions
                if _is_thread_reply(m)
                and not _cursor_says_read(m)
                and (m.channel_id, m.thread_ts) not in self._thread_cache
            }
        )

        async def _load_thread(key: tuple[str, str]):
            async with sem:
                await asyncio.to_thread(self._fetch_thread, key[0], key[1])

        if threads_to_load:
            await asyncio.gather(*[_load_thread(k) for k in threads_to_load])

        me = self._slack_user_id()
        unread_mentions: list[SlackMessage] = []
        for m in raw_mentions:
            if _cursor_says_read(m):
                continue  # already read (main timeline)
            if _is_thread_reply(m):
                thread_msgs = self._thread_cache.get(
                    (m.channel_id, m.thread_ts or ""), []
                )
                if self._thread_reply_is_read(thread_msgs, m.ts, me):
                    continue  # read via reaction / own reply in the thread
            unread_mentions.append(m)
        return unread_mentions

    async def get_activity_overview(
        self,
        *,
        mention_lookback_days: int = 30,
        max_channels: int = 80,
        mention_limit: int = 40,
        dm_limit: int = 30,
    ) -> dict[str, Any]:
        """Read-only snapshot of the user's Slack inbox for the dashboard.

        Everything here is scoped to what the user has NOT read yet:
          * channels with a non-zero Slack unread badge,
          * UNREAD @-mentions (read mentions are dropped via ``last_read``),
          * UNREAD DM/group-DM threads (+ a latest-message preview).

        Backs the homepage strip and the Productivity → Slack tab. No LLM, so
        it's cheap on a dashboard load; per-conversation calls fan out
        concurrently (semaphore-capped). Unread is counted from history relative
        to each conversation's ``last_read`` (Slack's ``conversations.info`` does
        NOT expose an unread count) — that's what fixes both the inflated counts
        (no real cursor) and the "all caught up" false negative.
        """
        sem = asyncio.Semaphore(8)

        # ── Channels with real unread (history since last_read) ──────────
        async def _unread_channels() -> list[dict[str, Any]]:
            # A thread, not inline: a blocking Slack call here stalls every
            # other request on the instance, the Priorities load included.
            channels = await asyncio.to_thread(
                self.list_member_channels, max_channels=max_channels
            )

            async def _ch_unread(ch: dict[str, Any]):
                cid = ch["id"]
                async with sem:
                    last_read = (await asyncio.to_thread(self._conversation_meta, cid))[
                        "last_read"
                    ]
                    count, _, _ = await asyncio.to_thread(
                        self._unread_in_conversation, cid, last_read
                    )
                return ch, count

            ch_fetched = (
                await asyncio.gather(*[_ch_unread(ch) for ch in channels])
                if channels
                else []
            )
            out: list[dict[str, Any]] = []
            for ch, count in ch_fetched:
                if count <= 0:
                    continue
                ch_id = ch["id"]
                out.append(
                    {
                        "id": ch_id,
                        "name": ch.get("name"),
                        "is_private": bool(ch.get("is_private")),
                        "unread_count": count,
                        "app_link": self._app_link(ch_id),
                    }
                )
            out.sort(key=lambda c: c["unread_count"], reverse=True)
            return out

        # ── Unread DMs and @-mentions (shared with the AI inbox digest) ──
        # Channels and DMs touch disjoint conversations, so they run side by
        # side under the one semaphore (same peak load on Slack). Mentions wait
        # for both: they reuse the read cursors those two already fetched.
        dm_task = asyncio.ensure_future(
            self._collect_unread_dms(dm_limit=dm_limit, sem=sem)
        )
        try:
            unread_channels = await _unread_channels()
        except BaseException:
            dm_task.cancel()
            raise
        unread_dms = await dm_task
        unread_mentions = await self._collect_unread_mentions(
            mention_lookback_days=mention_lookback_days,
            mention_limit=mention_limit,
            sem=sem,
        )

        return {
            "channels": unread_channels,
            "mentions": [
                {
                    "channel_id": m.channel_id,
                    "channel_name": m.channel_name,
                    "user_name": m.user_name,
                    "text": (m.text or "")[:280],
                    "ts": m.ts,
                    "permalink": m.permalink,
                    "app_link": m.app_link,
                }
                for m in unread_mentions
            ],
            "dms": unread_dms,
        }

    @attribute_to("slack")
    async def summarize_channel_unread(
        self,
        channel_id: str,
        *,
        context_messages: int = 6,
        max_messages: int = 40,
    ) -> dict[str, Any]:
        """Concise, on-demand summary of ONE channel's unread messages.

        Backs the hover popover on the Productivity \u2192 Slack tab: reads the
        unread messages plus a few preceding ones for context (NOT the whole
        channel history) and returns a short markdown digest \u2014 what's being
        discussed and anything that needs the user. Generated lazily, per channel,
        only when the user hovers a chip. Falls back to a plain recent list on LLM
        error so the popover is never empty.
        """
        last_read = (await asyncio.to_thread(self._conversation_meta, channel_id))[
            "last_read"
        ]
        messages = await asyncio.to_thread(
            lambda: self.get_channel_messages(
                channel=channel_id, limit=max_messages, skip_permalink=True
            )
        )
        # conversations.history returns newest-first; read it chronologically.
        messages = list(reversed(messages))
        me = self._slack_user_id()

        def _is_unread(m: SlackMessage) -> bool:
            if last_read is None:
                return True  # no read cursor \u2192 treat the recent slice as unread
            try:
                return float(m.ts) > float(last_read)
            except (TypeError, ValueError):
                return False

        unread = [m for m in messages if _is_unread(m) and m.user_id != me]
        # A few messages before last_read, just so the unread thread makes sense.
        context = [m for m in messages if not _is_unread(m)][-context_messages:]
        selected = context + unread
        channel_label = (
            f"#{messages[0].channel_name}"
            if messages and messages[0].channel_name
            else channel_id
        )

        transcript_lines: list[str] = []
        for m in selected:
            text = (m.text or "").replace("\n", " ").strip()
            if not text:
                continue
            who = m.user_name or m.user_id or "(unknown)"
            transcript_lines.append(f"{who}: {text[:400]}")
        if not transcript_lines:
            return {
                "summary_md": "",
                "unread_count": len(unread),
                "channel": channel_label,
            }

        transcript = "\n".join(transcript_lines)
        prompt = (
            "Summarize what's being discussed in this Slack channel for a busy "
            "professional who hasn't read it yet. Focus on the "
            f"{len(unread)} unread message(s) at the end; the earlier lines are "
            "prior context \u2014 use them only so the summary makes sense.\n\n"
            f"Channel: {channel_label}\n\n"
            "Output 1-3 short markdown bullets: what's happening and anything that "
            "needs the user (asks, decisions, deadlines). Use real names. No header, "
            "no preamble. If it's only small-talk, say so in one line.\n\n"
            f"Conversation:\n{transcript}"
        )

        try:
            from rubik_core.config import settings
            from rubik_core.gemini_tuning import generation_config_for
            from rubik_core.inference_workloads import run_text_async
            from rubik_core.models import get_genai_client

            # Slack summary on the LITE tier (item 11): summarization, not
            # reasoning — LITE is materially faster than FLASH, and Slack
            # catch-up was the slowest chat tool (24-43s in a timing test).
            # Per-channel fetch is already parallel (gather + to_thread above).
            # generation_config_for keeps that speed on the Gemini 3.x LITE tier,
            # which thinks by default unless asked for "minimal".
            async def managed(route):
                response = await get_genai_client().aio.models.generate_content(
                    model=route.model,
                    contents=prompt,
                    config=generation_config_for(route.model),
                )
                return response.text or ""

            summary_md = (
                await run_text_async(
                    "slack_summaries",
                    prompt,
                    default_model=settings.VERTEX_LLM_MODEL_LITE,
                    managed=managed,
                )
            ).strip()
            if not summary_md:
                raise RuntimeError("empty LLM response")
        except Exception:
            logger.warning("Slack channel summarization failed")
            # Fallback: the most recent lines verbatim, so the popover isn't empty.
            summary_md = (
                "_AI summary unavailable — showing raw activity:_\n"
                + "\n".join(f"- {line}" for line in transcript_lines[-8:])
            )

        return {
            "summary_md": summary_md,
            "unread_count": len(unread),
            "channel": channel_label,
        }

    # ─── Parsing helpers ─────────────────────────────────────────────────

    def _parse_message(
        self,
        m: dict[str, Any],
        channel_id: str,
        channel_name: str,
        *,
        skip_permalink: bool = False,
    ) -> SlackMessage:
        user_id = m.get("user") or m.get("bot_id") or ""
        text = m.get("text", "")
        slack_uid = self._slack_user_id()
        is_mention = bool(slack_uid) and f"<@{slack_uid}>" in text
        ts = m.get("ts", "")
        return SlackMessage(
            channel_id=channel_id,
            channel_name=channel_name,
            ts=ts,
            user_id=user_id,
            user_name=self._resolve_user_name(user_id) if user_id else None,
            text=text,
            permalink=None if skip_permalink else self._permalink(channel_id, ts),
            app_link=self._app_link(channel_id, ts),
            is_mention=is_mention,
            thread_ts=m.get("thread_ts"),
            reply_count=int(m.get("reply_count") or 0),
        )

    def _parse_search_match(self, m: dict[str, Any]) -> SlackMessage:
        channel_block = m.get("channel", {}) or {}
        channel_id = channel_block.get("id", "")
        channel_name = channel_block.get("name") or self._resolve_channel_name(
            channel_id
        )
        user_id = m.get("user", "")
        text = m.get("text", "")
        slack_uid = self._slack_user_id()
        ts = m.get("ts", "")
        permalink = m.get("permalink")
        # ``search.messages`` matches don't reliably carry ``thread_ts`` on the
        # object, but a threaded reply's permalink always does
        # (``…?thread_ts=…&cid=…``). Prefer the field, fall back to the permalink.
        thread_ts = m.get("thread_ts") or self._thread_ts_from_permalink(permalink)
        return SlackMessage(
            channel_id=channel_id,
            channel_name=channel_name,
            ts=ts,
            user_id=user_id,
            user_name=m.get("username")
            or (self._resolve_user_name(user_id) if user_id else None),
            text=text,
            permalink=permalink,
            app_link=self._app_link(channel_id, ts),
            is_mention=bool(slack_uid) and f"<@{slack_uid}>" in text,
            thread_ts=thread_ts,
        )

    @staticmethod
    def _thread_ts_from_permalink(permalink: str | None) -> str | None:
        """Pull the ``thread_ts`` query param out of a message permalink.

        Slack permalinks for threaded replies look like
        ``https://…/archives/C…/p123…?thread_ts=169…&cid=C…``. Returns None for
        top-level messages (no ``thread_ts``) or an unparseable URL."""
        if not permalink or "thread_ts=" not in permalink:
            return None
        from urllib.parse import parse_qs, urlparse

        try:
            values = parse_qs(urlparse(permalink).query).get("thread_ts")
        except ValueError:
            return None
        return values[0] if values else None


# ─── OAuth helpers (used by the router; live here to keep Slack code together)


def exchange_code_for_token(
    code: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str,
) -> dict[str, Any]:
    """Exchange the OAuth `code` for access tokens. Returns Slack's raw payload.

    Slack's `oauth.v2.access` returns both the workspace bot token (``xoxb-…``
    in ``access_token``) and the per-user token (``xoxp-…`` in
    ``authed_user.access_token``). We persist both so we can act on behalf
    of the user (search, mentions) and on behalf of the workspace bot
    (channel listings, future event-subscription posts).
    """
    resp = httpx.post(
        f"{SLACK_API_BASE}/oauth.v2.access",
        data={
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=DEFAULT_TIMEOUT,
    )
    data = resp.json()
    if not data.get("ok"):
        raise SlackAPIError(data.get("error", "oauth_failed"), data)
    return data


# ─── Outbound bot client ─────────────────────────────────────────────
#
# SlackService above is per-user and READ-only (xoxp- user token). Outbound
# delivery — DMing a rep their AI Priorities as the Cubie bot — uses the
# workspace BOT token (xoxb-) captured during OAuth in the workspace connector
# secret store. Needs the bot scopes chat:write,
# im:write, users:read.email (see backend/routers/slack.py BOT_SCOPES).

_BOT_MAX_RETRIES = 3


def workspace_has_slack_team(team_id: str | None = None) -> bool:
    """Whether the selected workspace owns a signed installation for a team."""
    return _workspace_slack_binding(team_id) is not None


def _workspace_slack_binding(team_id: str | None = None) -> tuple[str, str, str] | None:
    """Return the exact app, team, and signing ref bound to this workspace."""
    slug = current_workspace()
    with control_session() as db:
        statement = select(SlackInstallation).where(
            SlackInstallation.workspace_slug == slug
        )
        if team_id:
            statement = statement.where(SlackInstallation.team_id == team_id)
        row = db.scalar(statement)
        if row is None:
            return None
        return row.app_id, row.team_id, row.signing_secret_ref


def get_workspace_bot_token(team_id: str | None = None) -> str | None:
    """Return only the selected workspace's bound bot secret.

    A supplied team ID must belong to a registered installation in this
    workspace. A different team's ID cannot select or reveal a bot token.
    """
    current_workspace()
    if not workspace_has_slack_team(team_id):
        return None
    return workspace_secret("slack", "bot_token")


class SlackBotClient:
    """Workspace-bot-scoped Slack client for OUTBOUND posting.

    Posts AS the Cubie bot using the xoxb- workspace token — e.g. the overnight
    agent DMing reps their AI Priorities. Separate from SlackService,
    which is per-user and read-only. ``is_available`` is False when no bot token
    is stored yet (app not installed); callers should check it and skip delivery
    rather than raise.
    """

    def __init__(
        self,
        bot_token: str | None = None,
        team_id: str | None = None,
        *,
        timeout: float | None = None,
        max_retries: int | None = None,
    ):
        # An explicit token is used only while OAuth verifies an installation
        # that has not yet been committed. Runtime clients keep the exact
        # control binding so a cached instance cannot outlive its installation.
        self._workspace_binding: tuple[str, str, str, str] | None = None
        if bot_token:
            self._token = bot_token
        else:
            slug = current_workspace()
            binding = _workspace_slack_binding(team_id)
            self._workspace_binding = (slug, *binding) if binding else None
            self._token = workspace_secret("slack", "bot_token") if binding else None
        # Per-request budget. The defaults are the long-standing ones; a caller
        # racing a deadline (Slack gives a button click 3 s) passes a short
        # ``timeout`` and ``max_retries=1``. chat.postMessage is NOT
        # idempotent, so a post that timed out must never be sent again.
        self._timeout = DEFAULT_TIMEOUT if timeout is None else float(timeout)
        self._max_retries = (
            _BOT_MAX_RETRIES if max_retries is None else max(1, int(max_retries))
        )
        self._email_to_uid: dict[str, str | None] = {}
        # Cached full-workspace member sweep (one users.list pass fills both).
        # None until first requested; see _member_sweep.
        self._deleted_emails: set[str] | None = None
        self._member_emails: set[str] | None = None

    @property
    def is_available(self) -> bool:
        if not self._token:
            return False
        if self._workspace_binding is None:
            return True  # OAuth's explicit token, before installation exists.
        slug, app_id, team_id, signing_ref = self._workspace_binding
        try:
            if current_workspace() != slug:
                return False
            route = slack_route_for(app_id, team_id)
            return bool(
                route is not None
                and route.workspace_slug == slug
                and route.signing_secret_ref == signing_ref
                and workspace_secret("slack", "bot_token") == self._token
            )
        except Exception:  # noqa: BLE001 - unavailable control/secret denies use
            return False

    def _require_token(self) -> str:
        if not self.is_available:
            raise SlackNotConnectedError(
                f"No Slack bot token available — install the {PRODUCT_NAME} Slack app to "
                "the workspace first."
            )
        return self._token

    def _api(
        self,
        method: str,
        *,
        http_method: str = "post",
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> dict[str, Any]:
        """Call a Slack Web API method with the bot token.

        Retries transient rate limits (HTTP 429 / ``ratelimited``), honoring
        Slack's ``Retry-After`` header. Raises SlackAPIError on a non-retryable
        ``ok: false`` or after exhausting retries.
        """
        url = f"{SLACK_API_BASE}/{method}"
        last_err = "unknown_error"
        attempts = self._max_retries if max_retries is None else max(1, max_retries)
        request_timeout = self._timeout if timeout is None else timeout
        for attempt in range(attempts):
            token = self._require_token()
            headers = {"Authorization": f"Bearer {token}"}
            # No point waiting after the last attempt: it only eats the
            # caller's budget before the same error is raised.
            last = attempt + 1 >= attempts
            try:
                if http_method == "get":
                    resp = httpx.get(
                        url,
                        headers=headers,
                        params=params or {},
                        timeout=request_timeout,
                    )
                else:
                    resp = httpx.post(
                        url,
                        headers=headers,
                        json=json_body or {},
                        timeout=request_timeout,
                    )
            except httpx.HTTPError as e:
                # "timeout" is its own code so a caller can tell "Slack never
                # answered, the post may have gone through" from a refusal.
                last_err = (
                    "timeout" if isinstance(e, httpx.TimeoutException) else "http_error"
                )
                logger.warning(
                    "Slack bot HTTP error on %s (attempt %d): %s",
                    method,
                    attempt + 1,
                    type(e).__name__,
                )
                if not last:
                    time.sleep(1 + attempt)
                continue

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", "1") or "1")
                logger.info(f"Slack rate-limited on {method}; retry in {retry_after}s")
                if not last:
                    time.sleep(retry_after)
                last_err = "ratelimited"
                continue

            data = resp.json()
            if data.get("ok"):
                return data
            error = SlackAPIError(data.get("error", "unknown_error"), data)
            last_err = error.error
            if error.error == "ratelimited":
                if not last:
                    time.sleep(int(resp.headers.get("Retry-After", "1") or "1"))
                continue
            logger.warning("Slack bot API error on %s: %s", method, error.error)
            raise error
        raise SlackAPIError(last_err)

    def lookup_user_id(self, email: str) -> str | None:
        """Map a company email → Slack user ID via ``users.lookupByEmail``.

        Requires the ``users:read.email`` bot scope. Returns None when the email
        isn't a workspace member (so the caller can skip that rep rather than
        fail the whole batch). Cached per client instance.
        """
        if not email:
            return None
        if email in self._email_to_uid:
            return self._email_to_uid[email]
        try:
            data = self._api(
                "users.lookupByEmail", http_method="get", params={"email": email}
            )
            uid = data.get("user", {}).get("id")
        except SlackAPIError as e:
            if e.error in ("users_not_found", "missing_scope"):
                logger.info(f"Slack user lookup failed for {email}: {e.error}")
                uid = None
            else:
                raise
        self._email_to_uid[email] = uid
        return uid

    def _member_sweep(self) -> tuple[set[str], set[str]]:
        """``(all_member_emails, deleted_member_emails)`` from ONE ``users.list`` pass.

        Both sets come off the same paginated sweep, cached per client instance so
        a batch pays it once (rate-limit friendly). Requires the ``users:read``
        bot scope (a prerequisite of ``users:read.email``, already granted).

        Best-effort: on a mid-sweep API error it returns what it collected rather
        than raising, so a Slack hiccup can never block a run. That partial result
        is why ``all_member_emails`` must only ever be used to EXPLAIN something,
        never to drop a recipient — see :meth:`member_emails`.
        """
        if self._member_emails is not None and self._deleted_emails is not None:
            return self._member_emails, self._deleted_emails
        members: set[str] = set()
        deleted: set[str] = set()
        cursor = ""
        # A hard page cap so a pathological cursor loop can't run forever; 50
        # pages × 200 = 10k members, far above any real company workspace.
        for _ in range(50):
            params: dict[str, Any] = {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            try:
                data = self._api("users.list", http_method="get", params=params)
            except SlackAPIError as e:
                logger.warning(
                    "[offboard] Slack users.list failed (%s) — using %d found so far",
                    e.error,
                    len(deleted),
                )
                break
            for m in data.get("members", []) or []:
                email = ((m.get("profile") or {}).get("email") or "").strip().lower()
                if not email:
                    continue
                members.add(email)
                if m.get("deleted"):
                    deleted.add(email)
            cursor = (data.get("response_metadata") or {}).get("next_cursor", "") or ""
            if not cursor:
                break
        self._member_emails = members
        self._deleted_emails = deleted
        return members, deleted

    def deleted_member_emails(self) -> set[str]:
        """Lower-cased emails of DEACTIVATED (``deleted:true``) workspace members.

        Backs the offboarded-user gate: Slack marks a deactivated member
        ``deleted:true`` in ``users.list``, and that member's email won't resolve
        to an active DM target — so an ex-employee is dropped from the priorities
        roster and delivery instead of accruing empty bundles + false "no bundle"
        alerts.
        """
        return self._member_sweep()[1]

    def member_emails(self) -> set[str]:
        """Lower-cased emails of EVERY workspace member (active or deactivated).

        Used to EXPLAIN a delivery skip, never to cause one: an address absent
        here cannot be DM'd at all, which is what a stale or typo'd roster row
        looks like (``jane@acme.example`` alongside the real
        ``jdoe@acme.example`` — one had been logged as a daily "generation
        gap" for weeks). Because the sweep is best-effort and
        may return a PARTIAL roster, absence is not proof of anything: callers
        must treat an empty result as "unknown" and fall back to their
        unqualified behaviour rather than dropping anyone. Never gate delivery,
        generation or a DM on this.
        """
        return self._member_sweep()[0]

    def get_user_email(self, user_id: str) -> str | None:
        """Map a Slack user ID → their company email via ``users.info``.

        The reverse of :meth:`lookup_user_id`. Backs the slash-command identity
        resolution: Slack sends only a ``user_id`` on a slash command / modal
        submit, and we need the email to find the Cubie user. Requires the
        ``users:read.email`` bot scope (already granted). Returns None when the
        user has no visible email or the lookup fails, so the caller can fall
        back (e.g. to the SlackCredential table) or show a friendly error.
        """
        if not user_id:
            return None
        try:
            data = self._api("users.info", http_method="get", params={"user": user_id})
        except SlackAPIError as e:
            logger.info(f"Slack users.info failed for {user_id}: {e.error}")
            return None
        profile = (data.get("user", {}) or {}).get("profile", {}) or {}
        return profile.get("email")

    def find_channel(self, name_or_id: str) -> dict[str, Any] | None:
        """Look up one channel the bot can see, by id or by name.

        ``name_or_id`` is a channel id (``C0123ABCD`` / ``G0123ABCD``) or a bare
        name without the ``#`` (``sales``). Returns ``{"id", "name",
        "is_member", "is_private", "is_archived"}``, or None when the bot can
        see no such channel — a private channel the bot was never invited to is
        invisible to it, exactly like a typo. Backs Admin → Loose Ends, which
        checks the digest channel and the bot's membership before the post is
        switched on.

        By id: ``conversations.info``, which answers for an archived channel
        too (and an archived channel still lists its members), so the caller
        must read ``is_archived`` — Admin → Loose Ends refuses to switch the
        post on there. By name: ``conversations.list`` paged (archived channels
        left out), public and private; a token without ``groups:read`` gets
        ``missing_scope`` for the private half, so that retries with public
        channels only rather than failing the lookup. Any other Slack error
        raises :class:`SlackAPIError`.
        """
        wanted = (name_or_id or "").strip().lstrip("#")
        if not wanted:
            return None

        def _shape(ch: dict[str, Any]) -> dict[str, Any]:
            return {
                "id": ch.get("id"),
                "name": ch.get("name") or "",
                "is_member": bool(ch.get("is_member")),
                "is_private": bool(ch.get("is_private")),
                "is_archived": bool(ch.get("is_archived")),
            }

        if re.fullmatch(r"[CG][A-Z0-9]{6,}", wanted):
            try:
                data = self._api(
                    "conversations.info",
                    http_method="get",
                    params={"channel": wanted},
                )
            except SlackAPIError as e:
                if e.error == "channel_not_found":
                    return None
                raise
            channel = data.get("channel") or {}
            return _shape(channel) if channel.get("id") else None

        name = wanted.lower()

        def _scan(types: str) -> dict[str, Any] | None:
            cursor = ""
            # 20 pages × 1000 = 20k channels, far above any company workspace.
            for _ in range(20):
                params: dict[str, Any] = {
                    "types": types,
                    "exclude_archived": "true",
                    "limit": 1000,
                }
                if cursor:
                    params["cursor"] = cursor
                data = self._api("conversations.list", http_method="get", params=params)
                for ch in data.get("channels", []) or []:
                    if (ch.get("name") or "").lower() == name:
                        return _shape(ch)
                cursor = (data.get("response_metadata") or {}).get(
                    "next_cursor", ""
                ) or ""
                if not cursor:
                    break
            return None

        try:
            return _scan("public_channel,private_channel")
        except SlackAPIError as e:
            if e.error != "missing_scope":
                raise
        return _scan("public_channel")

    def open_view(self, trigger_id: str, view: dict[str, Any]) -> dict[str, Any]:
        """Open a modal via ``views.open``. Needs a fresh ``trigger_id`` (Slack
        expires them ~3s after the slash command / interaction that minted it),
        so this call gets a short timeout and never retries a spent trigger."""
        return self._api(
            "views.open",
            json_body={"trigger_id": trigger_id, "view": view},
            timeout=2.0,
            max_retries=1,
        )

    def open_dm(self, user_id: str) -> str:
        """Open (or reuse) the bot's DM channel with a user. Needs ``im:write``."""
        data = self._api("conversations.open", json_body={"users": user_id})
        return data["channel"]["id"]

    def post_message(
        self,
        channel: str,
        *,
        text: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
        thread_ts: str | None = None,
        unfurl_links: bool = False,
        unfurl_media: bool = False,
    ) -> dict[str, Any]:
        """Post to a channel/DM. ``text`` doubles as the notification fallback
        when ``blocks`` are supplied — always pass it for accessibility.
        ``thread_ts`` replies inside that message's thread instead of on the
        channel timeline (the KB watcher announces its publications there).

        Link previews are OFF unless a caller asks. A preview makes Slack's
        servers fetch the URL, so a link a hijacked model was tricked into
        writing (``https://evil/?d=<secret>``) would leak its query string with
        nobody clicking. Both flags are always sent: Slack's own defaults
        preview media links for bot posts."""
        body: dict[str, Any] = {
            "channel": channel,
            "unfurl_links": unfurl_links,
            "unfurl_media": unfurl_media,
        }
        if text is not None:
            body["text"] = text
        if blocks is not None:
            body["blocks"] = blocks
        if thread_ts is not None:
            body["thread_ts"] = thread_ts
        return self._api("chat.postMessage", json_body=body)

    def send_dm(
        self,
        recipient_email: str,
        *,
        text: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
        unfurl_links: bool = False,
        unfurl_media: bool = False,
    ) -> dict[str, Any] | None:
        """DM a rep by email: resolve email→ID, open the DM, post.

        Returns the ``chat.postMessage`` payload on success, or None when the
        rep can't be resolved (not in the workspace) — a per-recipient skip, not
        a batch failure. Raises SlackAPIError on a real Slack failure so the
        caller can record/alert (don't swallow it).
        """
        if not (text or blocks):
            raise ValueError("send_dm requires text or blocks")
        uid = self.lookup_user_id(recipient_email)
        if not uid:
            logger.info(f"Skipping Slack DM — {recipient_email} not found in workspace")
            return None
        channel = self.open_dm(uid)
        result = self.post_message(
            channel,
            text=text,
            blocks=blocks,
            unfurl_links=unfurl_links,
            unfurl_media=unfurl_media,
        )
        logger.info(f"Slack DM delivered to {recipient_email} (ts={result.get('ts')})")
        return result

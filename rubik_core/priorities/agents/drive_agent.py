"""Drive channel agent — UC-29 recent-activity feed.

Replaces the UC-22 stub. Pulls files the user touched in the last
``days_back`` window via ``GoogleDriveClient.list_recent_files`` and
emits one ``ChannelSignal`` per file with ``entity_kind=DOCUMENT``.
"""

from __future__ import annotations

import logging
from typing import Any

from rubik_core.priorities.agents.base import BaseChannelAgent
from rubik_core.priorities.models import ChannelKind, ChannelSignal, EntityKind

logger = logging.getLogger(__name__)


class DriveChannelAgent(BaseChannelAgent):
    channel = ChannelKind.DRIVE

    # Window the agent watches. Kept narrow so the channel agent stays a
    # cheap extractor — broader history is what the RAG indexes are for.
    LOOKBACK_DAYS = 7

    def fetch_signals(self) -> list[dict]:
        from rubik_core.priorities.agents._google_auth import require_live_client
        from rubik_core.tools.drive import GoogleDriveClient

        client = GoogleDriveClient(user_email=self.user_email)
        # Deactivated / DWD-broken account → service=None → surface as a fetch
        # failure instead of a silent empty Drive (see _google_auth).
        require_live_client(client, channel="drive", user_email=self.user_email)
        return client.list_recent_files(
            days_back=self.LOOKBACK_DAYS,
            max_results=self.config.drive_max_files,
        )

    def parse_signals(self, raw_items: list) -> list[ChannelSignal]:
        signals: list[ChannelSignal] = []
        user_email_lower = self.user_email.lower()
        for f in raw_items:
            file_id = f.get("id") or ""
            if not file_id:
                continue

            last_modifier = (
                (f.get("lastModifyingUser") or {}).get("emailAddress") or ""
            ).lower()
            owners = [
                (o.get("emailAddress") or "").lower() for o in (f.get("owners") or [])
            ]
            owner_set = {o for o in owners if o}
            user_owns = user_email_lower in owner_set
            mime = f.get("mimeType") or ""
            shared = bool(f.get("shared"))
            viewed = bool(f.get("viewedByMe"))

            attrs: dict[str, Any] = {
                "mime_type": mime,
                "modified_time_iso": f.get("modifiedTime"),
                "created_time_iso": f.get("createdTime"),
                "owners": owners,
                "owned_by_user": user_owns,
                "last_modifier_email": last_modifier,
                "modified_by_user": last_modifier == user_email_lower,
                "shared": shared,
                "viewed_by_me": viewed,
                "viewed_by_me_time_iso": f.get("viewedByMeTime"),
            }

            signals.append(
                ChannelSignal(
                    id=file_id,
                    channel=ChannelKind.DRIVE,
                    title=(f.get("name") or "(untitled)")[:160],
                    snippet="",
                    link=f.get("webViewLink"),
                    timestamp=f.get("modifiedTime") or f.get("createdTime"),
                    entity_kind=EntityKind.DOCUMENT,
                    entity_id=file_id,
                    entity_label=f.get("name") or "(untitled)",
                    attrs=attrs,
                )
            )
        return signals

    def build_prompt(self, signals: list[ChannelSignal]) -> str:
        file_lines = []
        for s in signals[: self.config.drive_max_files]:
            attrs = s.attrs
            file_lines.append(
                "- id={id}\n"
                "  name: {name}\n"
                "  mime: {mime}\n"
                "  modified: {modified}  by: {modifier}\n"
                "  shared: {shared}  owned_by_user: {owned}".format(
                    id=s.id,
                    name=s.title,
                    mime=attrs.get("mime_type"),
                    modified=attrs.get("modified_time_iso"),
                    modifier=attrs.get("last_modifier_email") or "?",
                    shared=attrs.get("shared"),
                    owned=attrs.get("owned_by_user"),
                )
            )
        block = "\n".join(file_lines) if file_lines else "(no recent files)"

        return (
            # Pure extractor — UC-22 regression test forbids judgment
            # vocabulary in any channel prompt.
            "You are the Drive channel extractor. Read the recent file "
            "list and return a structured JSON observation. You do NOT "
            "decide which files matter — the decision-maker downstream "
            "does that.\n\n"
            # Summary only. This prompt used to ask for per-file
            # `mentioned_topics` / `prep_keywords`, and parse_signals seeded
            # both under a comment claiming the decision-maker correlated
            # Drive with meetings through them. It never did: neither key is
            # in decision_maker.ATTRS_PREVIEW_KEYS, so the model's answer was
            # output tokens nothing read.
            "## OUTPUT (raw JSON only)\n"
            "{\n"
            '  "summary": "1 factual sentence on the user\'s recent Drive activity."\n'
            "}\n\n"
            f"## RECENT FILES\n{block}\n"
        )

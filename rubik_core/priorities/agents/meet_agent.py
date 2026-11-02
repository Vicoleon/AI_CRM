"""Meet channel agent — UC-22 pure extractor.

Lists recent Google Meet transcripts from the user's "Meet Recordings"
Drive folder and emits one ``ChannelSignal`` per transcript with
``entity_kind=TRANSCRIPT``. The LLM enrichment lifts the user's
commitments and the unanswered questions out of the transcript text —
observation only; no judgment about which transcripts matter.

The decision-maker (UC-24) is what correlates "commitment in
transcript" with "no outbound email since" to produce the
commitment-debt priority.
"""

from __future__ import annotations

import logging
from typing import Any

from rubik_core.priorities.agents.base import BaseChannelAgent
from rubik_core.priorities.models import ChannelKind, ChannelSignal, EntityKind

logger = logging.getLogger(__name__)


class MeetChannelAgent(BaseChannelAgent):
    channel = ChannelKind.MEET
    # build_prompt's OUTPUT section; both are empty placeholders until filled.
    model_attr_keys = frozenset({"commitments_made_by_user", "open_questions"})

    def fetch_signals(self) -> list[dict]:
        from rubik_core.priorities.agents._google_auth import require_live_client
        from rubik_core.tools.drive import GoogleDriveClient

        client = GoogleDriveClient(user_email=self.user_email)
        # Deactivated / DWD-broken account → service=None → surface as a fetch
        # failure instead of a silent "no transcripts" (see _google_auth). Meet
        # transcripts are read through the Drive client, so this is a Drive auth.
        require_live_client(client, channel="meet", user_email=self.user_email)
        folder_id = client.get_meet_recordings_folder_id()
        if not folder_id:
            return []
        return (
            client.list_transcripts(
                folder_id=folder_id, limit=self.config.meet_max_results
            )
            or []
        )

    def parse_signals(self, raw_items: list) -> list[ChannelSignal]:
        signals: list[ChannelSignal] = []
        capped = raw_items[: self.config.meet_max_results]
        for item in capped:
            file_id = item.get("id", "")
            attrs: dict[str, Any] = {
                "mime_type": item.get("mimeType"),
                "modified_time_iso": item.get("modifiedTime"),
                "created_time_iso": item.get("createdTime"),
                # Populated by the LLM enrichment below.
                "commitments_made_by_user": [],
                "open_questions": [],
            }
            signals.append(
                ChannelSignal(
                    id=file_id,
                    channel=ChannelKind.MEET,
                    title=item.get("name", "(untitled meeting)")[:160],
                    snippet="",  # hydrated for the top few below
                    link=f"https://drive.google.com/file/d/{file_id}/view"
                    if file_id
                    else None,
                    timestamp=item.get("createdTime") or item.get("modifiedTime"),
                    entity_kind=EntityKind.TRANSCRIPT,
                    entity_id=file_id,
                    entity_label=item.get("name") or "(untitled meeting)",
                    attrs=attrs,
                )
            )

        # Hydrate snippet bodies for the top 2 transcripts so the LLM
        # enrichment has something to chew on. Full transcript text would
        # blow the per-channel token budget.
        if signals:
            try:
                from rubik_core.tools.drive import GoogleDriveClient

                client = GoogleDriveClient(user_email=self.user_email)
                for sig in signals[:2]:
                    try:
                        text = client.export_document_text(
                            sig.id, sig.attrs.get("mime_type")
                        )
                        if text:
                            sig.snippet = text[:1500]
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "Meet transcript hydrate failed for %s: %s", sig.id, exc
                        )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Meet hydrate setup failed: %s", exc)

        return signals

    def build_prompt(self, signals: list[ChannelSignal]) -> str:
        meeting_lines = []
        for s in signals:
            preview = s.snippet or "(no transcript text available)"
            meeting_lines.append(
                "- id={id}\n"
                "  title: {title}\n"
                "  when: {ts}\n"
                "  transcript_preview: {prev}".format(
                    id=s.id,
                    title=s.title,
                    ts=s.timestamp or "?",
                    prev=preview[:1200],
                )
            )
        block = "\n".join(meeting_lines) if meeting_lines else "(no recent meetings)"

        return (
            # No judgment vocabulary — extractor only.
            "You are the Meet channel extractor. Read the recent Google "
            "Meet transcripts and return a structured JSON observation. "
            "You do NOT decide which meetings matter — the decision-maker "
            "downstream does that.\n\n"
            # Two fields, because two is what the decision-maker renders
            # (decision_maker.ATTRS_PREVIEW_KEYS). `decisions_recorded` and
            # `participants_mentioned` used to be asked for too; nothing
            # downstream ever read either one.
            "## YOUR TASK\n"
            "For each transcript with text available, extract:\n"
            "  - commitments_made_by_user: explicit promises the USER made "
            '("I\'ll send the deck", "Let me follow up Tuesday")\n'
            "  - open_questions: questions raised that did NOT receive an "
            "answer in the transcript\n\n"
            "## OUTPUT (raw JSON only)\n"
            "{\n"
            '  "summary": "1 factual sentence describing what the user committed to / owns from recent meetings.",\n'
            '  "signal_attrs": {\n'
            '    "<transcript_id>": {\n'
            '      "commitments_made_by_user": ["..."],\n'
            '      "open_questions": ["..."]\n'
            "    }\n"
            "  }\n"
            "}\n\n"
            f"## RECENT MEETINGS\n{block}\n"
        )
